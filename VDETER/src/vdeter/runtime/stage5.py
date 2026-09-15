"""Stage 5 Service 전체 통합 runtime (`stage5_service_integration_baseline.md`).

`Stage5Endpoint`는 동결 `VdeService` 공개 계약(`start/stop/submit/cancel/status`)을
유지하면서 Stage 5의 lifecycle, 2단계 admission transaction, TBB-aware Service FIFO,
terminal 보관, bounded event publisher와 §9.5 오류 projection을 구현한다.

Stage 0~4 동결 계층(`LinkDataPlane`, `Stage4Mac`, `Link11Phy`, `SoftwareRadioAdapter`)의
알고리즘은 재설계하지 않고 그대로 사용한다. Stage 0 legacy 경로와 Stage 4 경로를 같은
transfer에 중복 실행하지 않는다.
"""

from __future__ import annotations

from threading import RLock

from ..common.addressing import (
    Address,
    AddressKind,
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from dataclasses import replace as dataclass_replace

from ..common.contracts.link import (
    LinkAnalyzeRequest,
    LinkTransferControlRequest,
    TransferControlKind,
    TransferKind,
)
from ..common.contracts.mac_execution import CancelTransferEvent
from ..common.contracts.presentation import PresentationDecodeRequest, PresentationEncodeRequest
from ..common.contracts.radio import RadioConfigureRequest, RadioStartRequest, RadioStopRequest
from ..common.contracts.service import (
    CancelAccepted,
    CancelTransferRequest,
    EndpointHealth,
    ReceivedApplicationData,
    ServiceRole,
    ServiceStartRequest,
    ServiceStarted,
    ServiceStopped,
    ServiceStopRequest,
    SubmitTransferRequest,
    TERMINAL_TRANSFER_STATES,
    TransferAccepted,
    TransferState,
    TransferStatusRequest,
    TransferStatusSnapshot,
)
from ..common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
    ServiceError,
    ServiceErrorCategory,
)
from ..common.events import EventSeverity, ServiceEventType
from ..common.results import Err, Ok, internal_error
from ..service.models import Stage5ServicePolicy, TransferRecord
from ..service.publisher import Stage5EventPublisher
from .endpoint import Stage4Support, VdeTerEndpoint, _service_boundary
from .projection import PublicErrorCode, to_service_error

#: Type 93 Short Data의 encoded Presentation data 한도 (`stage5` §4.3).
SHORT_DATA_NOACK_ENCODED_LIMIT = 34
#: VDE Segment header 크기. Link 분석의 segment_size = header + encoded 길이.
SEGMENT_HEADER_BYTES = 4
#: `_set_transfer_state`가 받는 비공개(relay 등) transfer 상태의 bounded 내부 기록 상한.
INTERNAL_STATE_NOTE_LIMIT = 64


def _service_layer_error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    correlation: Correlation = Correlation(),
    **details,
):
    return internal_error(
        Layer.SERVICE,
        code,
        category,
        message,
        retryable=retryable,
        correlation=correlation,
        details=Details.of(**details) if details else Details(),
    )


class _PendingDispatch:
    """Service FIFO의 한 항목. 아직 MAC에 dispatch되지 않은 accepted transfer."""

    __slots__ = ("transfer_id", "destination", "ack_required", "accepted_at_nanoseconds")

    def __init__(self, transfer_id, destination, ack_required, accepted_at_nanoseconds):
        self.transfer_id = transfer_id
        self.destination = destination
        self.ack_required = ack_required
        self.accepted_at_nanoseconds = accepted_at_nanoseconds


class Stage5Support(Stage4Support):
    """Stage 4 dispatcher에 Stage 5 Service FIFO dispatch와 event pump hook을 결합한다."""

    __slots__ = ("_service_hooks_active", "_pump_gate")

    def __init__(self, endpoint, mac, outbox, ingress, health, lock) -> None:
        super().__init__(endpoint, mac, outbox, ingress, health, lock)
        self._service_hooks_active = False
        from threading import Lock

        self._pump_gate = Lock()

    def pump(self) -> None:
        # 같은 thread의 중첩 pump와 다른 thread의 동시 pump를 모두 원자적으로 거른다.
        # 실행 중인 pump가 ingress/outbox에 남은 작업을 이어서 소진한다 (§9.6).
        if not self._pump_gate.acquire(blocking=False):
            return
        try:
            super().pump()
            self._run_service_hooks()
        finally:
            self._pump_gate.release()

    def _run_service_hooks(self) -> None:
        if self._service_hooks_active:
            return
        endpoint = self._endpoint
        if not isinstance(endpoint, Stage5Endpoint):
            return
        self._service_hooks_active = True
        try:
            for _ in range(64):
                endpoint._sync_rx_reservations()
                progressed = endpoint._dispatch_ready_transfers()
                if not progressed:
                    break
                Stage4Support.pump(self)
            endpoint._sync_rx_reservations()
        finally:
            self._service_hooks_active = False
        endpoint._pump_public_events()

    def _execute_delivery(self, command, payload) -> None:
        """결정적 Presentation 구조 오류는 재시도하지 않고 즉시 실패로 되돌린다."""

        delivered = self._endpoint._deliver_presentation_bytes(
            payload.route, payload.presentation_data, payload.received_at
        )
        if delivered.is_ok:
            self._consumer_attempts.pop(command.command_id, None)
            self._complete(command)
            return
        error = delivered.error
        deterministic = error.category in (
            ErrorCategory.VALIDATION,
            ErrorCategory.PROTOCOL,
            ErrorCategory.UNSUPPORTED,
        )
        attempts = self._consumer_attempts.get(command.command_id, 0) + 1
        self._consumer_attempts[command.command_id] = attempts
        from .mac_commands import MAX_CONSUMER_ATTEMPTS

        if deterministic or attempts >= MAX_CONSUMER_ATTEMPTS:
            self._consumer_attempts.pop(command.command_id, None)
            self._fail(command, error)
            return
        self._retry_consumer_command(command)


class Stage5Endpoint(VdeTerEndpoint):
    """Stage 5 Service 의미를 구현하는 endpoint. 공개 method 모양은 동결 계약 그대로다."""

    __slots__ = (
        "_policy",
        "_publisher",
        "_service_lock",
        "_lifecycle_lock",
        "_run_generation",
        "_admission_open",
        "_stop_pending",
        "_service_fifo",
        "_dispatched",
        "_dispatching",
        "_terminal_meta",
        "_issued_transfer_ids",
        "_provisional",
        "_internal_state_notes",
        "_rx_reserved_sessions",
        "_rx_reservation_alerted",
        "_delivery_degraded",
        "_crypto_dependencies",
        "_wideband_profile",
        "_last_rx_error_signature",
    )

    def __init__(self, dependencies, policy: Stage5ServicePolicy | None = None) -> None:
        super().__init__(dependencies)
        self._policy = policy or Stage5ServicePolicy()
        self._publisher = Stage5EventPublisher(
            self._policy, dependencies.service_event_sink, dependencies.id_source
        )
        self._publisher.on_delivery_failed = self._on_event_delivery_failed
        self._publisher.on_queue_drained = self._on_event_queue_drained
        self._service_lock = RLock()
        # 공개 lifecycle 전이(start/stop)의 직렬화 경계 (§9.6). repository lock과 달리
        # 하위 계층 시작·종료 전체를 한 호출씩 차례로 실행한다.
        self._lifecycle_lock = RLock()
        self._run_generation = 0
        self._admission_open = False
        self._stop_pending = False
        self._service_fifo = []
        self._dispatched = set()
        self._dispatching = set()
        self._terminal_meta = {}
        self._issued_transfer_ids = set()
        self._provisional = {}
        self._internal_state_notes = {}
        self._rx_reserved_sessions = set()
        self._rx_reservation_alerted = set()
        self._delivery_degraded = False
        self._crypto_dependencies = None
        self._wideband_profile = None
        self._last_rx_error_signature = None

    # --- 관찰 (시험·진단; 공개 Service 계약이 아니다) ---

    @property
    def policy(self) -> Stage5ServicePolicy:
        return self._policy

    @property
    def publisher(self) -> Stage5EventPublisher:
        return self._publisher

    def notify_sink_recovered(self) -> None:
        """binding별 sink 복구 신호를 publisher로 전달한다."""

        self._publisher.notify_sink_recovered()

    # --- lifecycle helpers ---

    def _now_utc(self):
        return self._time().now_utc()

    def _now_monotonic_ns(self) -> int:
        return self._time().now_monotonic().total_nanoseconds()

    def _is_started(self) -> bool:
        return self._started or self._stop_pending

    def _commit_health(self, health: EndpointHealth, occurred_at=None) -> None:
        """health를 commit하고 event를 admission한다.

        `occurred_at`은 §5.2·§5.3의 "commit과 같은 공개 UTC 시각" 요구를 위해 caller가
        `started_at`/`stopped_at`과 동일한 시각 객체를 공유할 때 전달한다.
        """

        previous = self._health
        if previous is health:
            return
        self._health = health
        admitted = self._publisher.admit_health_changed(
            occurred_at if occurred_at is not None else self._now_utc(), previous, health
        )
        if admitted.is_err:
            self._record_internal_error(admitted.error)

    def _pump_public_events(self) -> None:
        self._publisher.pump()

    def _on_event_delivery_failed(self) -> None:
        if self._health is EndpointHealth.RUNNING:
            self._delivery_degraded = True
            self._commit_health(EndpointHealth.DEGRADED)

    def _on_event_queue_drained(self) -> None:
        if (
            self._delivery_degraded
            and self._health is EndpointHealth.DEGRADED
            and self._started
            and not self._stop_pending
        ):
            self._delivery_degraded = False
            self._commit_health(EndpointHealth.RUNNING)

    # --- start (`stage5` §5.2, §8.1) ---

    @_service_boundary
    def start(self, request: ServiceStartRequest):
        if not isinstance(request, ServiceStartRequest):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.INVALID_REQUEST,
                        ErrorCategory.VALIDATION,
                        "ServiceStartRequest가 필요합니다",
                    ).error
                )
            )
        # 동시 start()는 여기서 직렬화된다. 두 번째 호출은 첫 호출의 전이가 끝난 뒤
        # 상태를 보고 `SERVICE_ALREADY_STARTED`로 거부된다 (§5.2, §9.6).
        with self._lifecycle_lock:
            return self._start_serialized(request)

    def _start_serialized(self, request: ServiceStartRequest):
        with self._service_lock:
            if self._is_started():
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.SERVICE_ALREADY_STARTED,
                            ErrorCategory.VALIDATION,
                            "이미 시작된 Service입니다",
                        ).error
                    )
                )
            if self._health is EndpointHealth.UNHEALTHY:
                # invariant/cleanup 실패로 안전한 재시작이 불가능한 endpoint다.
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.INTERNAL_ERROR,
                            ErrorCategory.INTERNAL,
                            "UNHEALTHY endpoint는 다시 시작할 수 없습니다",
                        ).error
                    )
                )
            endpoint_config = self._dependencies.configuration.endpoint
            if (
                request.role is not endpoint_config.role
                or request.local_station_id != endpoint_config.station_id
            ):
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.INVALID_REQUEST,
                            ErrorCategory.VALIDATION,
                            "시작 요청이 endpoint 설정과 일치하지 않습니다",
                        ).error
                    )
                )
            if request.config_ref:
                # non-empty config_ref의 파일·URI·원격 lookup은 외부 binding이며 Stage 5는
                # configured resolver를 제공하지 않는다.
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.UNSUPPORTED_REQUEST,
                            ErrorCategory.UNSUPPORTED,
                            "config_ref resolver가 구성되지 않았습니다",
                            reason="config_ref_resolver_missing",
                        ).error
                    )
                )
            graph = self._validate_effective_graph()
            if graph is not None:
                return Err(to_service_error(graph.error))
            reserved = self._publisher.reserve_lifecycle()
            if reserved.is_err:
                # `STARTING` commit 전의 동기 거부. health와 event를 바꾸지 않는다.
                return Err(to_service_error(reserved.error))
            # STARTING commit + event admission
            self._run_generation += 1
            self._commit_health(EndpointHealth.STARTING)
        self._pump_public_events()

        started = self._start_components()
        if started is not None:
            return started

        with self._service_lock:
            self._started = True
            self._admission_open = True
            self._delivery_degraded = False
            started_at = self._now_utc()
            self._commit_health(EndpointHealth.RUNNING, started_at)
            admitted = self._publisher.admit_service_started(
                started_at, self._dependencies.configuration.endpoint.endpoint_id
            )
            if admitted.is_err:
                self._record_internal_error(admitted.error)
            result = Ok(
                ServiceStarted(
                    endpoint_id=self._dependencies.configuration.endpoint.endpoint_id,
                    role=self._dependencies.configuration.endpoint.role,
                    local_station_id=self._dependencies.configuration.endpoint.station_id,
                    health=self._health,
                    started_at=started_at,
                )
            )
        self._pump_public_events()
        return result

    def _start_components(self):
        """§8.1 4~6: Radio, RX 경로, MAC RUNNING 전환. 실패 시 역순 rollback."""

        rollback = []
        configured = self._radio.configure(
            RadioConfigureRequest(configuration=self._dependencies.configuration.radio)
        )
        if configured.is_err:
            return self._fail_start_rollback(configured.error, rollback)
        radio_started = self._radio.start(RadioStartRequest(reason="stage5 endpoint start"))
        if radio_started.is_err:
            return self._fail_start_rollback(radio_started.error, rollback)
        rollback.append(self._rollback_radio)
        if self._stage4 is None:
            return self._fail_start_rollback(
                _service_layer_error(
                    ErrorCode.UNSUPPORTED_REQUEST,
                    ErrorCategory.UNSUPPORTED,
                    "Stage 5 정상 profile에는 Stage 4 MAC 실행 지원이 필요합니다",
                ).error,
                rollback,
            )
        ready = self._stage4.mac.begin_running()
        if ready.is_err:
            return self._fail_start_rollback(ready.error, rollback)
        self._stage4.pump()
        return None

    def _rollback_radio(self) -> None:
        try:
            self._radio.stop(RadioStopRequest(reason="stage5 start rollback"))
        except Exception:  # noqa: BLE001 - best-effort rollback
            pass

    def _fail_start_rollback(self, error: InternalProtocolError, rollback):
        self._record_internal_error(error)
        rollback_failed = False
        for step in reversed(rollback):
            try:
                step()
            except Exception:  # noqa: BLE001
                rollback_failed = True
        with self._service_lock:
            self._started = False
            self._admission_open = False
            if rollback_failed:
                self._commit_health(EndpointHealth.UNHEALTHY)
            else:
                self._commit_health(EndpointHealth.STOPPED)
                self._publisher.release_service_started()
            public = to_service_error(error)
            admitted = self._publisher.admit_error(self._now_utc(), public)
            if admitted.is_err:
                self._record_internal_error(admitted.error)
        self._pump_public_events()
        return Err(public)

    # --- stop (`stage5` §5.3, §8.4) ---

    @_service_boundary
    def stop(self, request: ServiceStopRequest):
        if not isinstance(request, ServiceStopRequest):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.INVALID_REQUEST,
                        ErrorCategory.VALIDATION,
                        "ServiceStopRequest가 필요합니다",
                    ).error
                )
            )
        # 동시 stop()은 여기서 직렬화된다. 두 번째 호출은 Radio 종료·cleanup을 중복
        # 실행하지 않고, 첫 호출 완료 뒤 `SERVICE_NOT_STARTED`를 받는다 (§5.3, §9.6).
        with self._lifecycle_lock:
            return self._stop_serialized(request)

    def _stop_serialized(self, request: ServiceStopRequest):
        with self._service_lock:
            if not self._is_started():
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.SERVICE_NOT_STARTED,
                            ErrorCategory.VALIDATION,
                            "시작되지 않은 Service입니다",
                        ).error
                    )
                )
            first_attempt = not self._stop_pending
            # 1. 신규 admission을 원자적으로 닫고 진행 중인 Phase C를 무효화한다.
            self._admission_open = False
            self._run_generation += 1
            self._stop_pending = True
            self._started = False
        if first_attempt and self._stage4 is not None:
            # 2~3. Stage 4 STOP_DRAIN과 동결 bounded drain.
            self._stage4.begin_stop(request.reason or "service_stop")
        # 4. Radio 종료.
        stopped = self._radio.stop(RadioStopRequest(reason=request.reason or "service_stop"))
        if stopped.is_err:
            with self._service_lock:
                self._commit_health(EndpointHealth.DEGRADED)
            self._pump_public_events()
            # lifecycle stop은 전송 deadline이 아니므로 TIMEOUT을 `TRANSFER_TIMEOUT`으로
            # 바꾸지 않는다. `RADIO_STOP_TIMEOUT`의 공개 결과는
            # `DEVICE_ERROR/DEVICE, retryable=true, transfer_id=None`이다 (`stage5` §5.3).
            public = to_service_error(stopped.error)
            if stopped.error.code == ErrorCode.RADIO_STOP_TIMEOUT:
                public = ServiceError(
                    code=public.code,
                    category=public.category,
                    message=public.message,
                    retryable=True,
                    transfer_id=None,
                    details=public.details,
                )
            return Err(public)
        if self._stage4 is not None:
            # 5. Stage 4 finish_stop 의미로 남은 timer·retry·Link context를 닫는다.
            self._stage4.finish_stop()
        with self._service_lock:
            # 6. 끝나지 않은 transfer를 endpoint-stopping 오류로 FAILED 처리한다.
            stopping_error = ServiceError(
                code=PublicErrorCode.TRANSFER_FAILED,
                category=ServiceErrorCategory.RESOURCE,
                message="endpoint가 정지되어 전송이 완료되지 못했습니다",
                retryable=False,
            )
            for key, record in list(self._transfers.items()):
                if record.state not in TERMINAL_TRANSFER_STATES:
                    self._commit_terminal_locked(record, TransferState.FAILED, stopping_error)
            # 수신 reservation 해제 (idempotent).
            for handle in tuple(self._rx_reserved_sessions):
                self._publisher.release_incoming(handle)
            self._rx_reserved_sessions.clear()
            # 7. STOPPED commit → health event → SERVICE_STOPPED admission.
            stopped_at = self._now_utc()
            self._commit_health(EndpointHealth.STOPPED, stopped_at)
            admitted = self._publisher.admit_service_stopped(
                stopped_at, self._dependencies.configuration.endpoint.endpoint_id
            )
            if admitted.is_err:
                self._record_internal_error(admitted.error)
            # 8. 실행별 repository와 runtime snapshot 정리.
            self._publisher.release_lifecycle()
            self._reset_stage5_runtime_locked()
            self._stop_pending = False
            self._delivery_degraded = False
            result = Ok(
                ServiceStopped(
                    endpoint_id=self._dependencies.configuration.endpoint.endpoint_id,
                    health=self._health,
                    stopped_at=stopped_at,
                )
            )
        self._pump_public_events()
        return result

    def _close_admission(self) -> None:
        """group 종료가 세 endpoint의 신규 admission을 먼저 닫을 때 사용한다."""

        with self._service_lock:
            if self._started:
                self._admission_open = False

    def _reset_stage5_runtime_locked(self) -> None:
        """stop 8단계: 실행별 상태를 제거하고 실제 계층을 새 실행용으로 재조립한다."""

        self._transfers.clear()
        self._terminal_meta.clear()
        self._service_fifo.clear()
        self._dispatched.clear()
        self._dispatching.clear()
        # stop은 codec 종료를 기다리지 않는다 (§5.4). 아직 Phase C에 도달하지 못한
        # provisional transfer의 publisher token을 지금 해제해 새 실행의 event
        # capacity가 줄지 않게 한다. 뒤늦게 끝난 encode의 Phase C rollback이 수행하는
        # `release_transfer`는 idempotent하므로 이중 해제는 안전하다.
        for provisional_key in tuple(self._provisional):
            self._publisher.release_transfer(provisional_key)
        self._provisional.clear()
        self._pending_presentation.clear()
        self._burst_transfers.clear()
        self._internal_state_notes.clear()
        self._rx_reservation_alerted.clear()
        self._last_rx_error_signature = None
        from .wiring import build_stage4_layers

        _stub_presentation, link, phy = build_stage4_layers(
            self._dependencies, self._crypto_dependencies
        )
        # Presentation Adapter와 endpoint 수명 security collaborator는 stop/start에도 유지한다.
        self._link = link
        self._phy = phy
        if self._stage4 is not None:
            self._stage4.reset_for_restart()
            self._mac = self._stage4.mac

    # --- submit (`stage5` §5.4, §9.6) ---

    @_service_boundary
    def submit(self, request: SubmitTransferRequest):
        # lock 전에는 내부 상태를 읽지 않는 DTO type 검사만 수행한다.
        if not isinstance(request, SubmitTransferRequest):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.INVALID_REQUEST,
                        ErrorCategory.VALIDATION,
                        "SubmitTransferRequest가 필요합니다",
                    ).error
                )
            )
        # --- Phase A: lock/reserve ---
        with self._service_lock:
            rejected = self._admission_gate_locked()
            if rejected is not None:
                return rejected
            generation = self._run_generation
            registry_fingerprint = self._presentation.registry_fingerprint()
            payload_length = len(request.payload)
            if payload_length < 1:
                return self._reject(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "빈 payload는 수락하지 않습니다",
                )
            if payload_length > self._policy.max_application_bytes:
                return self._reject(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "application payload가 65,531-byte 상한을 넘습니다",
                    payload_size=payload_length,
                    max_payload_bytes=self._policy.max_application_bytes,
                )
            bound = self._presentation.encoded_size_bound(
                request.payload_format, payload_length
            )
            if bound.is_err:
                return Err(to_service_error(bound.error))
            encoded_bound = bound.value
            if encoded_bound > self._policy.max_encoded_bytes:
                return self._reject(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "encoded Presentation data가 65,535-byte 상한을 넘습니다",
                    payload_size=encoded_bound,
                )
            route_rejection = self._route_admission_check_locked(
                request.destination, request.ack_required, encoded_bound
            )
            if route_rejection is not None:
                return route_rejection
            self._evict_terminal_records_locked(self._now_monotonic_ns())
            nonterminal = self._nonterminal_count_locked()
            if nonterminal >= self._policy.max_nonterminal_transfers:
                return self._reject(
                    ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "endpoint nonterminal transfer 상한에 도달했습니다",
                    retryable=True,
                    limit_name="max_nonterminal_transfers",
                    limit_value=self._policy.max_nonterminal_transfers,
                    current_value=nonterminal,
                )
            transfer_id = self._dependencies.id_source.new_transfer_id()
            if transfer_id.value in self._issued_transfer_ids:
                return self._reject(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "transfer ID가 endpoint 수명 안에서 재사용되었습니다",
                )
            reserved = self._publisher.reserve_transfer(transfer_id.value)
            if reserved.is_err:
                return Err(to_service_error(reserved.error))
            self._issued_transfer_ids.add(transfer_id.value)
            self._provisional[transfer_id.value] = generation

        # --- Phase B: unlock/encode (codec을 lock 안에서 호출하지 않는다) ---
        endpoint_config = self._dependencies.configuration.endpoint
        route = RouteAddresses(
            original_source=OriginalSource(endpoint_config.station_id),
            original_destination=OriginalDestination(request.destination),
        )
        encoded = self._presentation.encode_payload(
            PresentationEncodeRequest(
                transfer_id=transfer_id,
                route=route,
                payload_format=request.payload_format,
                application_payload=request.payload,
                ack_required=request.ack_required,
            )
        )

        # --- Phase C: lock/revalidate/commit ---
        with self._service_lock:
            token_generation = self._provisional.pop(transfer_id.value, None)
            valid = (
                token_generation == generation
                and self._admission_open
                and self._started
                and self._health is EndpointHealth.RUNNING
                and self._presentation.registry_fingerprint() == registry_fingerprint
            )
            if not valid:
                self._publisher.release_transfer(transfer_id.value)
                return self._reject(
                    ErrorCode.SERVICE_NOT_STARTED,
                    ErrorCategory.VALIDATION,
                    "admission이 encode 중 닫혀 요청을 rollback했습니다",
                )
            if encoded.is_err:
                self._publisher.release_transfer(transfer_id.value)
                self._record_internal_error(encoded.error)
                public = to_service_error(encoded.error)
                # 외부 transfer_id를 남기지 않는 admission 전 오류 하나만 반환한다.
                return Err(
                    ServiceError(
                        code=public.code,
                        category=public.category,
                        message=public.message,
                        retryable=public.retryable,
                        transfer_id=None,
                        details=public.details,
                    )
                )
            actual_length = len(encoded.value.data)
            if (
                not 1 <= actual_length <= self._policy.max_encoded_bytes
                or actual_length != encoded_bound
            ):
                self._publisher.release_transfer(transfer_id.value)
                return self._reject(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "encode 결과 길이가 결정적 예상 크기와 다릅니다",
                    payload_size=actual_length,
                )
            route_rejection = self._route_admission_check_locked(
                request.destination, request.ack_required, actual_length
            )
            if route_rejection is not None:
                self._publisher.release_transfer(transfer_id.value)
                return route_rejection
            now = self._now_utc()
            record = TransferRecord(
                transfer_id=transfer_id, state=TransferState.QUEUED, updated_at=now
            )
            self._transfers[transfer_id.value] = record
            self._pending_presentation[transfer_id.value] = encoded.value
            entry = _PendingDispatch(
                transfer_id,
                request.destination,
                request.ack_required,
                now.total_nanoseconds(),
            )
            self._service_fifo.append(entry)
            self._service_fifo.sort(
                key=lambda item: (item.accepted_at_nanoseconds, item.transfer_id.value)
            )
            admitted = self._publisher.admit_transfer_state(
                transfer_id.value, transfer_id, TransferState.QUEUED, now
            )
            if admitted.is_err:
                self._record_internal_error(admitted.error)
            result = Ok(
                TransferAccepted(
                    transfer_id=transfer_id, state=TransferState.QUEUED, accepted_at=now
                )
            )
        self._pump_public_events()
        if self._stage4 is not None:
            self._stage4.pump()
        return result

    def _admission_gate_locked(self):
        if not self._is_started():
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.SERVICE_NOT_STARTED,
                        ErrorCategory.VALIDATION,
                        "Service가 시작되지 않았습니다",
                    ).error
                )
            )
        if (
            not self._admission_open
            or self._health is not EndpointHealth.RUNNING
        ):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
                        ErrorCategory.RESOURCE,
                        "endpoint가 신규 submit을 수락할 수 없는 상태입니다",
                        retryable=True,
                        reason="admission_closed",
                    ).error
                )
            )
        return None

    def _reject(
        self,
        code: str,
        category: ErrorCategory,
        message: str,
        *,
        retryable: bool = False,
        **details,
    ):
        error = _service_layer_error(
            code, category, message, retryable=retryable, **details
        ).error
        self._record_internal_error(error)
        return Err(to_service_error(error))

    def _nonterminal_count_locked(self) -> int:
        active = sum(
            1
            for record in self._transfers.values()
            if record.state not in TERMINAL_TRANSFER_STATES
        )
        return active + len(self._provisional)

    def _route_admission_check_locked(
        self, destination: Address, ack_required: bool, encoded_length: int
    ):
        """§4.3의 지원 route 경계를 deterministic encoded 크기로 선검증한다."""

        endpoint_config = self._dependencies.configuration.endpoint
        deployment = self._stage4.mac.coordinator.deployment if self._stage4 else None
        if destination.kind is AddressKind.BROADCAST:
            if endpoint_config.role is not ServiceRole.CONTROL:
                return self._reject(
                    ErrorCode.UNSUPPORTED_REQUEST,
                    ErrorCategory.UNSUPPORTED,
                    "MASS-originated broadcast는 지원하지 않습니다",
                    reason="broadcast_requires_control_station",
                )
            if encoded_length > SHORT_DATA_NOACK_ENCODED_LIMIT:
                return self._reject(
                    ErrorCode.UNSUPPORTED_REQUEST,
                    ErrorCategory.UNSUPPORTED,
                    "broadcast Data Session은 지원하지 않습니다",
                    reason="broadcast_data_session_unsupported",
                    payload_size=encoded_length,
                )
            return None
        station = destination.station_id
        if station == endpoint_config.station_id:
            return self._reject(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "자기 자신을 목적지로 지정할 수 없습니다",
            )
        if deployment is not None and not deployment.is_member(station):
            return self._reject(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "배치 profile 밖의 station으로는 전송할 수 없습니다",
                reason="destination_not_deployment_member",
            )
        return None

    # --- Service FIFO dispatch (`stage5` §5.4) ---

    def _dispatch_ready_transfers(self) -> bool:
        if self._stage4 is None:
            return False
        progressed = False
        while True:
            with self._service_lock:
                if (
                    not self._started
                    or self._stop_pending
                    or not self._service_fifo
                ):
                    return progressed
                mac_state = self._stage4.mac.state
                active = mac_state.active_tbb
                now_utc_ns = self._now_utc().total_nanoseconds()
                if active is None or not active.is_active_at(now_utc_ns):
                    # 활성 TBB가 없으면 Service FIFO에서 `QUEUED`로 기다린다.
                    return progressed
                entry = self._service_fifo[0]
                record = self._transfers.get(entry.transfer_id.value)
                if record is None or record.state in TERMINAL_TRANSFER_STATES:
                    self._service_fifo.pop(0)
                    progressed = True
                    continue
                presentation = self._pending_presentation.get(entry.transfer_id.value)
                # lock 밖의 Link/MAC 호출 동안 cancel()이 이 transfer를 Service-owned
                # `QUEUED`로 오인하지 않도록 in-flight dispatch를 먼저 표시한다 (§9.2).
                self._dispatching.add(entry.transfer_id.value)
            if presentation is None:
                with self._service_lock:
                    self._dispatching.discard(entry.transfer_id.value)
                    self._remove_fifo_entry_locked(entry.transfer_id)
                self._fail_transfer(
                    entry.transfer_id,
                    _service_layer_error(
                        ErrorCode.INTERNAL_ERROR,
                        ErrorCategory.INTERNAL,
                        "dispatch할 Presentation data가 없습니다",
                        correlation=Correlation(transfer_id=entry.transfer_id),
                    ).error,
                )
                progressed = True
                continue
            analysis_route = self._analysis_route(entry, presentation)
            analyzed = self._link.analyze_transfer(
                LinkAnalyzeRequest(
                    transfer_id=entry.transfer_id,
                    route=analysis_route,
                    ack_required=entry.ack_required,
                    presentation_data=presentation.data,
                )
            )
            if analyzed.is_err:
                with self._service_lock:
                    self._dispatching.discard(entry.transfer_id.value)
                    self._remove_fifo_entry_locked(entry.transfer_id)
                self._fail_transfer(entry.transfer_id, analyzed.error)
                progressed = True
                continue
            requirement = self._select_link11_candidate(analyzed.value)
            accepted = self._mac.submit_requirement(requirement)
            if accepted.is_err:
                error = accepted.error
                if error.code == ErrorCode.MAC_RESOURCE_UNAVAILABLE:
                    # transient: 같은 분석 결과를 보존한 채 다음 자원 변화에서 재제출한다.
                    with self._service_lock:
                        self._dispatching.discard(entry.transfer_id.value)
                    return progressed
                # permanent: Stage 1 DR-038 분석-only cleanup 뒤 terminal FAILED.
                self._link.control_transfer(
                    LinkTransferControlRequest(
                        kind=TransferControlKind.FAIL,
                        transfer_id=entry.transfer_id,
                        session_handle=None,
                        packet_progress=(),
                        correlation=Correlation(transfer_id=entry.transfer_id),
                    )
                )
                with self._service_lock:
                    self._dispatching.discard(entry.transfer_id.value)
                    self._remove_fifo_entry_locked(entry.transfer_id)
                self._fail_transfer(entry.transfer_id, error)
                progressed = True
                continue
            cancelled_during_dispatch = False
            with self._service_lock:
                self._dispatching.discard(entry.transfer_id.value)
                self._remove_fifo_entry_locked(entry.transfer_id)
                self._dispatched.add(entry.transfer_id.value)
                latest = self._transfers.get(entry.transfer_id.value)
                if latest is not None and latest.state in TERMINAL_TRANSFER_STATES:
                    # MAC 제출과 경쟁한 cancel/실패가 먼저 terminal을 commit했다. 방금
                    # 만들어진 MAC context를 즉시 동결 cancel 절차로 정리한다 (§9.2).
                    cancelled_during_dispatch = True
            if cancelled_during_dispatch:
                self._stage4.enqueue(
                    CancelTransferEvent(transfer_id=entry.transfer_id),
                    correlation=Correlation(transfer_id=entry.transfer_id),
                    priority=True,
                )
            progressed = True

    def _remove_fifo_entry_locked(self, transfer_id) -> None:
        self._service_fifo = [
            item for item in self._service_fifo if item.transfer_id != transfer_id
        ]

    def _supported_link_id_values(self) -> set:
        return {
            link_id.value
            for link_id in self._dependencies.configuration.endpoint.link.supported_link_ids
        }

    def _select_link11_candidate(self, requirement):
        """endpoint가 실제 사용하는 Link ID의 Short Data 용량으로 후보를 확정한다.

        Stage 1 분석은 registry의 모든 SUPPORTED Link profile(11·17·19)로 eligibility를
        계산하지만 Stage 5 profile은 Link ID 11만 송신한다. §4.3의 encoded 33/34 경계는
        Link ID 11 용량이므로, 지원 Link ID에서 Short Data가 불가능하면 addressed Data
        Session으로 확정한다 (`DR-STAGE5-010`). 동결 Link·MAC 알고리즘은 바꾸지 않는다.
        """

        if requirement.candidate is not TransferKind.SHORT_DATA:
            return requirement
        supported = self._supported_link_id_values()
        eligible = any(
            item.short_data_eligible
            for item in requirement.profiles
            if item.link_id.value in supported
        )
        if eligible:
            return requirement
        return dataclass_replace(requirement, candidate=TransferKind.DATA_SESSION)

    def _short_data_eligible_on_supported_links(self, entry, presentation) -> bool:
        message_type = 92 if entry.ack_required else 93
        segment_size = SEGMENT_HEADER_BYTES + len(presentation.data)
        supported = self._supported_link_id_values()
        return any(
            dict(profile.message_payload_capacities).get(message_type, 0) >= segment_size
            for profile in self._dependencies.registries.link_profile.entries()
            if profile.link_id.value in supported
        )

    def _analysis_route(self, entry: _PendingDispatch, presentation) -> RouteAddresses:
        endpoint_config = self._dependencies.configuration.endpoint
        route = RouteAddresses(
            original_source=OriginalSource(endpoint_config.station_id),
            original_destination=OriginalDestination(entry.destination),
        )
        if self._stage4 is None or entry.destination.kind is not AddressKind.ADDRESSED:
            return route
        deployment = self._stage4.mac.coordinator.deployment
        short_eligible = self._short_data_eligible_on_supported_links(entry, presentation)
        if (
            not short_eligible
            and endpoint_config.station_id.value != deployment.roc_station_id.value
            and entry.destination.station_id.value != deployment.roc_station_id.value
            and deployment.is_member(entry.destination.station_id)
        ):
            # 큰 Ship-to-Ship addressed 전송은 ROC 경유 Data Session이다 (§4.3).
            return RouteAddresses(
                original_source=route.original_source,
                original_destination=route.original_destination,
                node_source=NodeSource(endpoint_config.station_id),
                node_destination=NodeDestination(Addressed(deployment.roc_station_id)),
            )
        return route

    # --- status (`stage5` §5.5) ---

    @_service_boundary
    def status(self, request: TransferStatusRequest):
        if not isinstance(request, TransferStatusRequest):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.INVALID_REQUEST,
                        ErrorCategory.VALIDATION,
                        "TransferStatusRequest가 필요합니다",
                    ).error
                )
            )
        with self._service_lock:
            if not self._is_started():
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.SERVICE_NOT_STARTED,
                            ErrorCategory.VALIDATION,
                            "Service가 시작되지 않았습니다",
                        ).error
                    )
                )
            self._evict_terminal_records_locked(self._now_monotonic_ns())
            record = self._transfers.get(request.transfer_id.value)
            if record is None:
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.UNKNOWN_TRANSFER,
                            ErrorCategory.VALIDATION,
                            "알 수 없는 transfer_id입니다",
                            correlation=Correlation(transfer_id=request.transfer_id),
                        ).error
                    )
                )
            return Ok(
                TransferStatusSnapshot(
                    transfer_id=record.transfer_id,
                    state=record.state,
                    updated_at=record.updated_at,
                    error=record.error if record.state is TransferState.FAILED else None,
                )
            )

    # --- cancel (`stage5` §5.6, §9.2) ---

    @_service_boundary
    def cancel(self, request: CancelTransferRequest):
        if not isinstance(request, CancelTransferRequest):
            return Err(
                to_service_error(
                    _service_layer_error(
                        ErrorCode.INVALID_REQUEST,
                        ErrorCategory.VALIDATION,
                        "CancelTransferRequest가 필요합니다",
                    ).error
                )
            )
        with self._service_lock:
            if not self._is_started():
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.SERVICE_NOT_STARTED,
                            ErrorCategory.VALIDATION,
                            "Service가 시작되지 않았습니다",
                        ).error
                    )
                )
            key = request.transfer_id.value
            record = self._transfers.get(key)
            if record is None:
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.UNKNOWN_TRANSFER,
                            ErrorCategory.VALIDATION,
                            "알 수 없는 transfer_id입니다",
                            correlation=Correlation(transfer_id=request.transfer_id),
                        ).error
                    )
                )
            if record.state in TERMINAL_TRANSFER_STATES:
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.INVALID_REQUEST,
                            ErrorCategory.VALIDATION,
                            "이미 종료된 전송은 취소할 수 없습니다",
                            correlation=Correlation(transfer_id=request.transfer_id),
                        ).error
                    )
                )
            queued_result = None
            if key not in self._dispatched and key not in self._dispatching:
                # Service-owned `QUEUED`: 하위 송신을 시작하지 않고 cleanup 뒤 CANCELLED.
                # dispatch가 진행 중인 transfer는 MAC-owned 경로로 처리한다.
                self._remove_fifo_entry_locked(request.transfer_id)
                committed = self._commit_terminal_locked(
                    record, TransferState.CANCELLED, None
                )
                if committed.is_err:
                    return Err(to_service_error(committed.error))
                terminal = self._transfers[key]
                queued_result = Ok(
                    CancelAccepted(
                        transfer_id=request.transfer_id,
                        state=TransferState.CANCELLED,
                        accepted_at=terminal.updated_at,
                    )
                )
        if queued_result is not None:
            # sink 호출은 repository lock 밖에서 수행한다 (§9.6).
            self._pump_public_events()
            return queued_result
        # MAC-owned: 동결 `CancelTransferEvent`로 MAC cancel과 Link cleanup을 함께 처리한다.
        if self._stage4 is not None:
            enqueued = self._stage4.enqueue(
                CancelTransferEvent(transfer_id=request.transfer_id),
                correlation=Correlation(transfer_id=request.transfer_id),
                priority=True,
            )
            if enqueued.is_err:
                return Err(to_service_error(enqueued.error))
            self._stage4.pump()
        with self._service_lock:
            record = self._transfers.get(key)
            if record is None:
                return Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.UNKNOWN_TRANSFER,
                            ErrorCategory.VALIDATION,
                            "취소 처리 중 transfer record가 사라졌습니다",
                            correlation=Correlation(transfer_id=request.transfer_id),
                        ).error
                    )
                )
            if record.state is TransferState.CANCELLED:
                result = Ok(
                    CancelAccepted(
                        transfer_id=request.transfer_id,
                        state=TransferState.CANCELLED,
                        accepted_at=record.updated_at,
                    )
                )
            elif record.state in TERMINAL_TRANSFER_STATES:
                # 완료·실패가 먼저 원자 commit됐다. 최초 terminal commit이 승리한다.
                result = Err(
                    to_service_error(
                        _service_layer_error(
                            ErrorCode.INVALID_REQUEST,
                            ErrorCategory.VALIDATION,
                            "이미 종료된 전송은 취소할 수 없습니다",
                            correlation=Correlation(transfer_id=request.transfer_id),
                        ).error
                    )
                )
            else:
                # MAC context가 이미 없거나 재큐 상태다. Service 측에서 terminal commit한다.
                self._remove_fifo_entry_locked(request.transfer_id)
                committed = self._commit_terminal_locked(
                    record, TransferState.CANCELLED, None
                )
                if committed.is_err:
                    return Err(to_service_error(committed.error))
                terminal = self._transfers[key]
                result = Ok(
                    CancelAccepted(
                        transfer_id=request.transfer_id,
                        state=TransferState.CANCELLED,
                        accepted_at=terminal.updated_at,
                    )
                )
        self._pump_public_events()
        return result

    # --- 상태 commit (`stage5` §8.2, §9.1, §9.3) ---

    def _set_transfer_state(self, transfer_id, state: TransferState, error=None):
        with self._service_lock:
            key = transfer_id.value
            record = self._transfers.get(key)
            if record is None:
                # accepted 공개 transfer가 아니다 (예: ROC 내부 relay ID). 공개 record와
                # event를 만들지 않고 bounded 내부 기록만 남긴다.
                self._internal_state_notes[key] = state
                if len(self._internal_state_notes) > INTERNAL_STATE_NOTE_LIMIT:
                    oldest = next(iter(self._internal_state_notes))
                    self._internal_state_notes.pop(oldest, None)
                return Ok(False)
            if record.state in TERMINAL_TRANSFER_STATES:
                # 최초 terminal commit만 유효하다. 중복 PublishTransferStateCommand는
                # 같은 결과로 인식해 idempotent하게 완료한다.
                return Ok(False)
            if state in TERMINAL_TRANSFER_STATES:
                committed = self._commit_terminal_locked(record, state, error)
                if committed.is_err:
                    return committed
                return Ok(True)
            now = self._now_utc()
            self._transfers[key] = record.with_state(state, now, None)
            admitted = self._publisher.admit_transfer_state(key, transfer_id, state, now)
            if admitted.is_err:
                self._record_internal_error(admitted.error)
            return Ok(True)

    def _commit_terminal_locked(self, record, state: TransferState, error):
        key = record.transfer_id.value
        if state is TransferState.FAILED and error is None:
            error = ServiceError(
                code=PublicErrorCode.TRANSFER_FAILED,
                category=ServiceErrorCategory.PROTOCOL,
                message="전송 처리에 실패했습니다",
                retryable=False,
                transfer_id=record.transfer_id,
            )
        if state is not TransferState.FAILED:
            error = None
        now = self._now_utc()
        self._transfers[key] = record.with_state(state, now, error)
        self._terminal_meta[key] = self._now_monotonic_ns()
        self._dispatched.discard(key)
        self._pending_presentation.pop(key, None)
        self._remove_fifo_entry_locked(record.transfer_id)
        self._detach_transfer_bursts(record.transfer_id)
        admitted = self._publisher.admit_transfer_state(
            key, record.transfer_id, state, now, error
        )
        if admitted.is_err:
            self._record_internal_error(admitted.error)
        self._evict_terminal_records_locked(self._now_monotonic_ns())
        return Ok(True)

    def _fail_transfer(self, transfer_id, error: InternalProtocolError) -> None:
        self._record_internal_error(error)
        public = to_service_error(error)
        if public.transfer_id is None:
            public = ServiceError(
                code=public.code,
                category=public.category,
                message=public.message,
                retryable=public.retryable,
                transfer_id=transfer_id,
                details=public.details,
            )
        with self._service_lock:
            record = self._transfers.get(transfer_id.value)
            if record is None or record.state in TERMINAL_TRANSFER_STATES:
                return
            self._commit_terminal_locked(record, TransferState.FAILED, public)
        self._pump_public_events()

    def _evict_terminal_records_locked(self, now_monotonic_ns: int) -> None:
        """15분/최대 1,024개 terminal retention (`stage5` §9.3)."""

        ttl = self._policy.terminal_retention_nanoseconds
        expired = [
            key
            for key, committed in self._terminal_meta.items()
            if now_monotonic_ns - committed >= ttl
        ]
        for key in expired:
            self._terminal_meta.pop(key, None)
            self._transfers.pop(key, None)
        overflow = len(self._terminal_meta) - self._policy.terminal_retention_max_records
        if overflow > 0:
            ordered = sorted(self._terminal_meta.items(), key=lambda item: (item[1], item[0]))
            for key, _committed in ordered[:overflow]:
                self._terminal_meta.pop(key, None)
                self._transfers.pop(key, None)

    # --- 수신 경로 (`stage5` §8.3) ---

    def _deliver_presentation_bytes(self, route, presentation_data, received_at):
        application = self._presentation.decode_payload(
            PresentationDecodeRequest(
                route=route,
                data=presentation_data,
                received_at=received_at,
            )
        )
        if application.is_err:
            self._record_internal_error(application.error)
            self._emit_rx_error(application.error)
            self._pump_public_events()
            return application
        data = application.value
        destination = data.route.original_destination.address
        local_station = self._dependencies.configuration.endpoint.station_id
        if (
            destination.kind is AddressKind.ADDRESSED
            and destination.station_id != local_station
        ):
            error = _service_layer_error(
                ErrorCode.PRESENTATION_STRUCTURE_INVALID,
                ErrorCategory.PROTOCOL,
                "다른 station 대상 data는 Service에 전달하지 않습니다",
            ).error
            self._record_internal_error(error)
            return Err(error)
        with self._service_lock:
            admission_utc = self._now_utc()
            received = ReceivedApplicationData(
                original_source=data.route.original_source.station_id,
                original_destination=destination,
                payload_format=data.payload_format,
                payload=data.application_payload,
                received_at=admission_utc,
            )
            reservation_key = None
            if len(presentation_data) > SHORT_DATA_NOACK_ENCODED_LIMIT:
                reservation_key = self._match_rx_reservation_locked(data.route)
            admitted = self._publisher.admit_data_received(
                received, admission_utc, reservation_key
            )
            if reservation_key is not None:
                self._rx_reserved_sessions.discard(reservation_key)
            if admitted.is_err:
                # backpressure를 줄 수 없는 traffic이 capacity를 소진했다. 조용한 유실이나
                # 성공 전달로 기록하지 않고 endpoint를 DEGRADED로 만든다 (`stage5` §8.3).
                self._record_internal_error(admitted.error)
                self._emit_rx_error(admitted.error)
                if self._health is EndpointHealth.RUNNING:
                    # capacity 포화 DEGRADED는 sink 전달 실패와 달리 queue drain으로
                    # 자동 복구하지 않는다 (`stage5` §8.3, §9.4).
                    self._commit_health(EndpointHealth.DEGRADED)
                result = Err(admitted.error)
            else:
                result = Ok(received)
        self._pump_public_events()
        return result

    def _match_rx_reservation_locked(self, route) -> str | None:
        if not self._rx_reserved_sessions or self._stage4 is None:
            return None
        for session in self._stage4.mac.state.rx_sessions:
            handle = session.session_handle.value
            if handle in self._rx_reserved_sessions and session.route == route:
                return handle
        # active session이 이미 정리됐다면 남은 예약 하나를 소비한다 (endpoint당 1 session).
        return next(iter(self._rx_reserved_sessions))

    def _sync_rx_reservations(self) -> None:
        """incoming Data Session 수락·정리에 따라 DATA_RECEIVED slot을 예약·해제한다."""

        if self._stage4 is None:
            return
        degraded = False
        with self._service_lock:
            current = {
                session.session_handle.value
                for session in self._stage4.mac.state.rx_sessions
            }
            for handle in current - self._rx_reserved_sessions:
                reserved = self._publisher.reserve_incoming(
                    handle, self._policy.max_application_bytes
                )
                if reserved.is_ok:
                    self._rx_reserved_sessions.add(handle)
                    self._rx_reservation_alerted.discard(handle)
                elif handle not in self._rx_reservation_alerted:
                    # §9.4: 수신 session의 DATA_RECEIVED slot을 예약할 수 없으면 조용히
                    # 넘어가지 않는다. 구조화 ERROR와 DEGRADED로 알리고(신규 submit은
                    # §9.7에 따라 거부됨) 다음 sync에서 capacity 회복 시 재시도한다.
                    # 전달 자체는 delivery-time gate가 계속 차단한다.
                    self._rx_reservation_alerted.add(handle)
                    self._record_internal_error(reserved.error)
                    self._emit_rx_error(reserved.error)
                    if self._health is EndpointHealth.RUNNING:
                        self._commit_health(EndpointHealth.DEGRADED)
                        degraded = True
            for handle in tuple(self._rx_reserved_sessions - current):
                # abort·timeout·stop cleanup: 채워지지 않은 slot을 idempotent하게 해제한다.
                self._publisher.release_incoming(handle)
                self._rx_reserved_sessions.discard(handle)
            self._rx_reservation_alerted &= current
        if degraded:
            self._pump_public_events()

    def _emit_rx_error(self, error: InternalProtocolError) -> None:
        """특정 송신과 연결되지 않은 RX 오류를 `transfer_id=None`인 ERROR로 알린다."""

        signature = (error.code, error.details.get("data_size"))
        if signature == self._last_rx_error_signature:
            return
        self._last_rx_error_signature = signature
        public = to_service_error(error)
        public = ServiceError(
            code=public.code,
            category=public.category,
            message=public.message,
            retryable=public.retryable,
            transfer_id=None,
            details=public.details,
        )
        admitted = self._publisher.admit_error(self._now_utc(), public)
        if admitted.is_err:
            # 예약된 ERROR가 pending이다. spill 없이 원인 계층의 bounded 상태가 남는다.
            self._record_internal_error(admitted.error)

    # --- 상위 클래스 잔여 emit 경로 ---

    def _report_internal_error(self, error: InternalProtocolError) -> None:
        self._record_internal_error(error)
        if (
            error.details.get("status") in ("NO_BURST_DETECTED", "RECOVERED")
            and error.details.get("mac_reason") == "EVENT_INVALID"
        ):
            # 유휴 RX window의 "복원된 Packet 없음"은 수신 부재이지 endpoint 오류가
            # 아니다. 보호된 내부 event에만 남기고 공개 ERROR를 만들지 않는다.
            return
        if error.code == ErrorCode.LINK_TBB_CANDIDATE_NOT_FOUND:
            # 주기 재방송 중간에 합류한 TBB continuation fragment는 내부 재획득 절차의
            # 정상 경과다. TBB 절차는 Service API 밖의 내부 절차이므로 (`stage5` §5.2)
            # 공개 ERROR로 projection하지 않는다.
            return
        public = to_service_error(error)
        admitted = self._publisher.admit_error(
            self._now_utc(), public, public.transfer_id
        )
        if admitted.is_err:
            self._record_internal_error(admitted.error)
        self._pump_public_events()

    def _programming_failure(self, operation: str, caught: Exception):
        error = InternalProtocolError(
            layer=Layer.RUNTIME,
            code=ErrorCode.INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            message=f"{operation} 처리 중 programming error가 발생했습니다",
            details=Details.of(operation=operation, exception_type=type(caught).__name__),
        )
        try:
            self._record_internal_error(error)
        except Exception:  # noqa: BLE001
            pass
        try:
            with self._service_lock:
                self._commit_health(EndpointHealth.UNHEALTHY)
                public = to_service_error(error)
                self._publisher.admit_error(self._now_utc(), public)
            self._pump_public_events()
        except Exception:  # noqa: BLE001
            pass
        return Err(to_service_error(error))

    def _emit_service_event(
        self,
        event_type: ServiceEventType,
        *,
        severity: EventSeverity,
        transfer_id=None,
        transfer_state=None,
        received_data=None,
        error=None,
        details: Details = Details(),
    ):
        """상위 클래스 경로 호환용. Stage 5 경로는 전용 helper를 사용한다."""

        if event_type is ServiceEventType.ERROR:
            admitted = self._publisher.admit_error(self._now_utc(), error, transfer_id)
            self._pump_public_events()
            return admitted
        return Ok(True)

    # --- graph 검증 (`stage5` §4.4) ---

    def _validate_effective_graph(self):
        from ..link.data_plane import LinkDataPlane
        from ..mac.interface import Stage4Mac
        from ..phy.multilink import MultiRatePhy
        from ..phy.transmitter import Link11Phy
        from ..presentation.adapter import RegistryPresentationAdapter
        from ..radio.software import SoftwareRadioAdapter

        checks = (
            ("presentation", self._presentation, RegistryPresentationAdapter),
            ("link", self._link, LinkDataPlane),
            ("mac", self._mac, Stage4Mac),
            ("phy", self._phy, (Link11Phy, MultiRatePhy)),
            ("radio", self._radio, SoftwareRadioAdapter),
        )
        for layer_name, instance, expected in checks:
            if not isinstance(instance, expected):
                return _service_layer_error(
                    ErrorCode.UNSUPPORTED_REQUEST,
                    ErrorCategory.UNSUPPORTED,
                    "Stage 5 정상 profile과 다른 유효 dependency graph입니다",
                    reason="stage5_graph_mismatch",
                    operation=layer_name,
                )
        if self._stage4 is None:
            return _service_layer_error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "Stage 4 MAC 실행 지원이 연결되지 않았습니다",
                reason="stage5_graph_mismatch",
                operation="mac_execution",
            )
        return None


__all__ = ["Stage5Endpoint", "Stage5Support", "SHORT_DATA_NOACK_ENCODED_LIMIT"]
