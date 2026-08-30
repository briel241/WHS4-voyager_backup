"""Stage 7 endpoint (`stage7` §15, §16.1).

`Stage7Endpoint`는 frozen `Stage5Endpoint`를 복사하거나 source를 수정하지 않고
상속해 public `VdeService` 구현과 Stage 4/5 protocol 동작을 재사용하되,
behavioral override surface를 `start()` gate, `submit/cancel/status/stop`의 얇은
public-admission guard, `_validate_effective_graph()`와
`_reset_stage5_runtime_locked()`로 닫는다.
"""

from __future__ import annotations

from threading import Condition, RLock

from ..common.contracts.service import (
    CancelTransferRequest,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferStatusRequest,
)
from ..common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
    ServiceError,
    ServiceErrorCategory,
)
from ..common.events import (
    EventSeverity,
    ServiceEvent,
    ServiceEventType,
)
from ..common.results import Err, Ok, internal_error
from ..common.serialization import to_reference_json
from ..runtime.projection import PublicErrorCode, to_service_error
from ..runtime.stage5 import Stage5Endpoint
from .canonical_json import canonical_sha256, encode_canonical_json, sha256_hex
from .errors import Stage7PrivateErrorCode
from .records import Stage7ServiceStartIntentRecord


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_dto_digest(dto) -> str:
    """frozen `to_reference_json` 결과의 §16 canonical bytes SHA-256."""

    return sha256_hex(encode_canonical_json(_jsonable(to_reference_json(dto))))


class Stage7PublicStartGate:
    """application sink 앞의 start-event gate (§15).

    staged transaction이 열린 동안 inherited start와 administrative rollback이
    생성한 모든 public lifecycle/health event를 보류한다. transfer event를
    합성·삭제하거나 payload를 보지 않는다.
    """

    __slots__ = (
        "_application_sink",
        "_lock",
        "_mode",
        "_held",
        "_generated_digests",
        "_withheld_digests",
        "_discarded_digests",
        "_projected_digests",
    )

    _HELD_TYPES = frozenset(
        {
            ServiceEventType.SERVICE_STARTED,
            ServiceEventType.SERVICE_STOPPED,
            ServiceEventType.ENDPOINT_HEALTH_CHANGED,
        }
    )

    def __init__(self, application_sink) -> None:
        self._application_sink = application_sink
        self._lock = RLock()
        self._mode = "OPEN"  # OPEN | HELD
        self._held = []
        self._generated_digests = []
        self._withheld_digests = []
        self._discarded_digests = []
        self._projected_digests = []

    @property
    def application_sink(self):
        return self._application_sink

    def begin_staged_transaction(self) -> None:
        with self._lock:
            self._mode = "HELD"
            self._held = []

    def publish(self, event: ServiceEvent):
        from ..common.events import EventPublished

        with self._lock:
            if self._mode == "HELD" and event.event_type in self._HELD_TYPES:
                self._held.append(event)
                self._generated_digests.append(canonical_dto_digest(event))
                self._withheld_digests.append(canonical_dto_digest(event))
                return Ok(EventPublished(event_id=event.event_id))
        return self._application_sink.publish(event)

    def open_and_flush(self):
        """publish 성공: 보류된 event를 원래 순서·timestamp/ID 그대로 한 번 공개한다."""

        with self._lock:
            held, self._held = self._held, []
            self._mode = "OPEN"
        for event in held:
            self._application_sink.publish(event)
        return Ok(len(held))

    def discard_and_project(self, id_source, time_source, public_error: ServiceError):
        """publish 전 ABORT/failure: 내부 event를 공개하지 않고 §15의 단일
        start-failure 투영 `STARTING→STOPPED→ERROR`만 한 번 공개한다."""

        from ..common.contracts.service import EndpointHealth

        with self._lock:
            held, self._held = self._held, []
            self._mode = "OPEN"
            for event in held:
                self._discarded_digests.append(canonical_dto_digest(event))
        now = time_source.now_utc()
        projection = (
            ServiceEvent(
                event_id=id_source.new_event_id(),
                event_type=ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                occurred_at=now,
                severity=EventSeverity.INFO,
                details=Details.of(health=EndpointHealth.STARTING.value),
            ),
            ServiceEvent(
                event_id=id_source.new_event_id(),
                event_type=ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                occurred_at=now,
                severity=EventSeverity.INFO,
                details=Details.of(health=EndpointHealth.STOPPED.value),
            ),
            ServiceEvent(
                event_id=id_source.new_event_id(),
                event_type=ServiceEventType.ERROR,
                occurred_at=now,
                severity=EventSeverity.ERROR,
                error=public_error,
            ),
        )
        for event in projection:
            self._projected_digests.append(canonical_dto_digest(event))
            self._application_sink.publish(event)
        return Ok(len(held))

    def audit_artifact(self) -> dict:
        with self._lock:
            return {
                "artifact_type": "STAGE7_START_GATE_EVENT_SETS_V1",
                "generated": tuple(self._generated_digests),
                "withheld": tuple(self._withheld_digests),
                "discarded": tuple(self._discarded_digests),
                "projected": tuple(self._projected_digests),
            }

    def audit_sha256(self) -> str:
        return canonical_sha256(_jsonable(self.audit_artifact()))


def _not_started_error():
    return ServiceError(
        code=PublicErrorCode.SERVICE_NOT_STARTED,
        category=ServiceErrorCategory.VALIDATION,
        message="Service가 시작되지 않았습니다",
        retryable=False,
    )


def _invalid_request_error(message: str):
    return to_service_error(
        internal_error(
            Layer.SERVICE,
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            message,
        ).error
    )


class Stage7Endpoint(Stage5Endpoint):
    """schema v4 Stage 7 endpoint (`stage7` §15)."""

    __slots__ = (
        "_stage7_resolved",
        "_gate_lock",
        "_gate_condition",
        "public_service_published",
        "_admission_record",
        "_pending_start",
        "_start_terminal",
        "_intent_notifier",
        "_start_gate",
        "_stage7_restart_factory",
        "_stage7_graph",
        "_lifecycle_generation",
        "_bridge_generation",
        "_host_raw_clock",
        "_slot_reserved",
        "_tx_inhibited",
    )

    def __init__(self, dependencies, policy=None, *, resolved=None) -> None:
        super().__init__(dependencies, policy)
        self._stage7_resolved = resolved
        self._gate_lock = RLock()
        self._gate_condition = Condition(self._gate_lock)
        self.public_service_published = False
        self._admission_record = None
        self._pending_start = None
        self._start_terminal = None
        self._intent_notifier = None
        self._start_gate = None
        self._stage7_restart_factory = None
        self._stage7_graph = None
        self._lifecycle_generation = 0
        self._bridge_generation = None
        self._host_raw_clock = None
        self._slot_reserved = False
        self._tx_inhibited = False

    # --- 조립 seam (builder 전용) ---------------------------------------------

    def bind_stage7(
        self,
        *,
        start_gate: Stage7PublicStartGate,
        graph: dict,
        restart_factory=None,
        intent_notifier=None,
        host_raw_clock=None,
    ) -> None:
        self._start_gate = start_gate
        self._stage7_graph = graph
        self._stage7_restart_factory = restart_factory
        self._intent_notifier = intent_notifier
        self._host_raw_clock = host_raw_clock

    def set_lifecycle_generation(self, generation: int) -> None:
        self._lifecycle_generation = generation

    def set_bridge_generation(self, generation) -> None:
        self._bridge_generation = generation

    @property
    def start_gate(self):
        return self._start_gate

    @property
    def stage7_graph(self):
        return self._stage7_graph

    # --- Service admission window (§16.1) --------------------------------------

    def install_service_admission(
        self,
        *,
        intent_window_id: str,
        service_start_deadline_host_raw_ns: int,
        tbb_publish_deadline_host_raw_ns: int,
    ):
        with self._gate_lock:
            self._admission_record = {
                "intent_window_id": intent_window_id,
                "service_start_deadline_host_raw_ns": service_start_deadline_host_raw_ns,
                "tbb_publish_deadline_host_raw_ns": tbb_publish_deadline_host_raw_ns,
            }
        return Ok(None)

    def discard_service_admission(self) -> None:
        with self._gate_lock:
            self._admission_record = None
            self._pending_start = None
            self._slot_reserved = False

    def pending_intent_digest(self):
        with self._gate_lock:
            if self._pending_start is None:
                return None
            return self._pending_start["digest"]

    # --- start gate (§15) -------------------------------------------------------

    def start(self, request: ServiceStartRequest):
        if not isinstance(request, ServiceStartRequest):
            return Err(_invalid_request_error("ServiceStartRequest가 필요합니다"))
        endpoint_config = self._dependencies.configuration.endpoint
        with self._gate_lock:
            if self.public_service_published:
                return Err(
                    to_service_error(
                        internal_error(
                            Layer.SERVICE,
                            ErrorCode.SERVICE_ALREADY_STARTED,
                            ErrorCategory.VALIDATION,
                            "이미 시작된 Service입니다",
                        ).error
                    )
                )
            if self._pending_start is not None:
                # 같은 endpoint의 동시 후속 start(): 별도 slot/event 없이 첫
                # 호출의 terminal까지 직렬화한다 (§15).
                while self._pending_start is not None and self._start_terminal is None:
                    self._gate_condition.wait()
                return self._follower_result_locked()
            if self._start_terminal is not None:
                return self._follower_result_locked()
            if self._tx_inhibited:
                return Err(
                    to_service_error(
                        internal_error(
                            Layer.SERVICE,
                            ErrorCode.INTERNAL_ERROR,
                            ErrorCategory.INTERNAL,
                            "TX-inhibited endpoint는 start할 수 없습니다",
                        ).error
                    )
                )
            if (
                request.role is not endpoint_config.role
                or request.local_station_id != endpoint_config.station_id
            ):
                return Err(_invalid_request_error("시작 요청이 endpoint 설정과 일치하지 않습니다"))
            if request.config_ref:
                return Err(
                    to_service_error(
                        internal_error(
                            Layer.SERVICE,
                            ErrorCode.UNSUPPORTED_REQUEST,
                            ErrorCategory.UNSUPPORTED,
                            "config_ref resolver가 구성되지 않았습니다",
                        ).error
                    )
                )
            graph = self._validate_effective_graph()
            if graph is not None:
                return Err(to_service_error(graph.error))
            admission = self._admission_record
            if admission is None:
                return Err(_not_started_error_result())
            now_raw = self._now_host_raw_ns()
            if now_raw is None or now_raw >= admission["service_start_deadline_host_raw_ns"]:
                return Err(
                    ServiceError(
                        code=PublicErrorCode.DEVICE_ERROR,
                        category=ServiceErrorCategory.DEVICE,
                        message="Service-start intent cutoff이 지났습니다",
                        retryable=True,
                    )
                )
            record = Stage7ServiceStartIntentRecord(
                intent_window_id=admission["intent_window_id"],
                service_start_deadline_host_raw_ns=admission[
                    "service_start_deadline_host_raw_ns"
                ],
                endpoint_id=endpoint_config.endpoint_id,
                local_station_id=endpoint_config.station_id.value,
                role=endpoint_config.role.value,
                config_ref=request.config_ref,
            )
            # one-entry intent-notification slot reserve (§15).
            if self._slot_reserved:
                return self._slot_invariant_failure_locked()
            self._slot_reserved = True
            digest = record.digest()
            self._pending_start = {
                "request": request,
                "record": record,
                "digest": digest,
                "issued_host_raw_ns": now_raw,
            }
            event = {
                "event_type": "STATE_CHANGED",
                "endpoint_state": "SERVICE_START_PENDING",
                "lifecycle_generation": self._lifecycle_generation,
                "bridge_generation": self._bridge_generation,
                "artifact_sha256": digest,
                "issued_host_raw_ns": now_raw,
            }
            notifier = self._intent_notifier
            deadline_ns = admission["tbb_publish_deadline_host_raw_ns"]
        # gate lock을 놓은 뒤에만 immutable source-less intent event를 전달한다.
        sent = False
        if notifier is not None:
            try:
                sent = bool(notifier(event))
            except Exception:  # noqa: BLE001
                sent = False
        if not sent:
            failure = ServiceError(
                code=PublicErrorCode.DEVICE_ERROR,
                category=ServiceErrorCategory.DEVICE,
                message="start-intent event 전달이 실패했습니다",
                retryable=True,
            )
            self.fail_pending_start(failure, inhibit=True)
            with self._gate_lock:
                return self._follower_result_locked()
        # 첫 호출은 publish 또는 terminal failure까지 조건에서 기다린다.
        with self._gate_lock:
            while self._start_terminal is None:
                remaining = None
                now_raw = self._now_host_raw_ns()
                if now_raw is not None:
                    remaining = max(0.0, (deadline_ns + 30_000_000_000 - now_raw) / 1e9)
                if not self._gate_condition.wait(timeout=remaining):
                    failure = ServiceError(
                        code=PublicErrorCode.DEVICE_ERROR,
                        category=ServiceErrorCategory.DEVICE,
                        message="start publish deadline이 만료되었습니다",
                        retryable=True,
                    )
                    self._start_terminal = ("err", failure)
                    self._pending_start = None
                    self._slot_reserved = False
                    self._gate_condition.notify_all()
                    break
            kind, value = self._start_terminal
            if kind == "ok":
                return Ok(value)
            return Err(value)

    def _follower_result_locked(self):
        if self.public_service_published:
            return Err(
                to_service_error(
                    internal_error(
                        Layer.SERVICE,
                        ErrorCode.SERVICE_ALREADY_STARTED,
                        ErrorCategory.VALIDATION,
                        "이미 시작된 Service입니다",
                    ).error
                )
            )
        if self._start_terminal is not None:
            kind, value = self._start_terminal
            if kind == "ok":
                return Err(
                    to_service_error(
                        internal_error(
                            Layer.SERVICE,
                            ErrorCode.SERVICE_ALREADY_STARTED,
                            ErrorCategory.VALIDATION,
                            "이미 시작된 Service입니다",
                        ).error
                    )
                )
            return Err(value)
        return Err(_not_started_error())

    def _slot_invariant_failure_locked(self):
        # 정상 동시 경합이 아닌 slot 불변식 실패: INTERNAL_ERROR 한 번 투영,
        # TX inhibit, group stop 요청 (§15).
        self._tx_inhibited = True
        error = to_service_error(
            internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "intent-notification slot 불변식이 실패했습니다",
            ).error
        )
        return Err(error)

    def _now_host_raw_ns(self):
        clock = self._host_raw_clock
        if clock is None:
            return None
        try:
            return int(clock())
        except Exception:  # noqa: BLE001
            return None

    # --- Runtime private handlers (§15, §16.1) ----------------------------------

    def prepare_service_start(self, expected_digest: str):
        with self._gate_lock:
            pending = self._pending_start
            if pending is None or pending["digest"] != expected_digest:
                return internal_error(
                    Layer.RUNTIME,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "intent digest가 local pending record와 다릅니다",
                    details=Details.of(
                        stage7_private_code=Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID.value
                    ),
                )
        reserved = self._publisher.reserve_lifecycle()
        if reserved.is_err:
            return reserved
        return Ok(None)

    def abort_service_start(self) -> None:
        self._publisher.release_lifecycle()

    def commit_service_start_staged(self):
        """COMMIT: §15 public gate 뒤에서 보관한 동일 request로 frozen
        `super().start()`를 정확히 한 번 실행한다."""

        with self._gate_lock:
            pending = self._pending_start
            if pending is None:
                return internal_error(
                    Layer.RUNTIME,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "pending start가 없습니다",
                )
            request = pending["request"]
            gate = self._start_gate
        if gate is not None:
            gate.begin_staged_transaction()
        result = Stage5Endpoint.start(self, request)
        # RX_LOOKAHEAD_UNSATISFIED는 기동 순간이 프레임 경계의 ~100ms lead
        # 불운 구간에 걸린 위상 복권이다 (svcdbg23 실측 — run별 무작위
        # endpoint 기동 거부의 정체). frozen start는 실패 시 자체 rollback을
        # 마치므로, 위상이 바뀌도록 짧게 대기 후 같은 request로 제한
        # 재시도한다 (§16.1 staged 의미 불변 — 성공은 여전히 정확히 한 번).
        retries = 0
        while (
            result.is_err
            and "RX_LOOKAHEAD_UNSATISFIED" in str(result.error)
            and retries < 3
        ):
            retries += 1
            import time as _time

            _time.sleep(0.3)
            with self._gate_lock:
                self._start_terminal = None
            if gate is not None:
                gate.begin_staged_transaction()
            result = Stage5Endpoint.start(self, request)
        if result.is_err:
            return result
        staged = result.value
        staged_digest = canonical_dto_digest(staged)
        with self._gate_lock:
            if self._pending_start is not None:
                self._pending_start["staged_result"] = staged
                self._pending_start["staged_digest"] = staged_digest
        return Ok(
            {
                "staged_service_started_sha256": staged_digest,
                "staged_public_event_set_sha256": self._staged_event_set_digest(),
                "result": staged,
            }
        )

    def _staged_event_set_digest(self) -> str:
        gate = self._start_gate
        if gate is None:
            return sha256_hex(encode_canonical_json([]))
        with gate._lock:
            events = tuple(gate._held)
        array = [_jsonable(to_reference_json(event)) for event in events]
        return sha256_hex(encode_canonical_json(array))

    def publish_service_start_commit(self):
        """common publish 시각의 COMMIT: 보류한 result/event와 normal admission을
        함께 공개한다."""

        with self._gate_lock:
            pending = self._pending_start
            if pending is None or "staged_result" not in pending:
                return internal_error(
                    Layer.RUNTIME,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "staged start 없이 publish할 수 없습니다",
                )
            staged = pending["staged_result"]
            gate = self._start_gate
            self.public_service_published = True
            self._start_terminal = ("ok", staged)
            self._pending_start = None
            self._slot_reserved = False
            self._gate_condition.notify_all()
        if gate is not None:
            gate.open_and_flush()
        return Ok(staged)

    def rollback_staged_start(self, public_error: ServiceError):
        """publish 전 ABORT/member failure: inherited stop과 단일 failure 투영."""

        with self._gate_lock:
            pending = self._pending_start
            staged = None if pending is None else pending.get("staged_result")
            gate = self._start_gate
        if staged is not None:
            try:
                Stage5Endpoint.stop(self, ServiceStopRequest(reason="stage7 staged rollback"))
            except Exception:  # noqa: BLE001
                pass
        if gate is not None:
            gate.discard_and_project(
                self._dependencies.id_source, self._time(), public_error
            )
        self.fail_pending_start(public_error)
        return Ok(None)

    def fail_pending_start(self, public_error: ServiceError, *, inhibit: bool = False) -> None:
        with self._gate_lock:
            if inhibit:
                self._tx_inhibited = True
            if self._start_terminal is None:
                self._start_terminal = ("err", public_error)
            self._pending_start = None
            self._slot_reserved = False
            self._gate_condition.notify_all()

    def administrative_stop(self, reason: str = "stage7 administrative stop"):
        """Runtime Coordinator rollback/stop 전용 private handler (guard 우회)."""

        with self._gate_lock:
            self.public_service_published = False
        return Stage5Endpoint.stop(self, ServiceStopRequest(reason=reason))

    # --- public-admission guard (§15) -------------------------------------------

    def _guard_public(self):
        with self._gate_lock:
            if not self.public_service_published:
                return Err(_not_started_error())
        return None

    def submit(self, request: SubmitTransferRequest):
        if not isinstance(request, SubmitTransferRequest):
            return Err(_invalid_request_error("SubmitTransferRequest가 필요합니다"))
        rejected = self._guard_public()
        if rejected is not None:
            return rejected
        return Stage5Endpoint.submit(self, request)

    def cancel(self, request: CancelTransferRequest):
        if not isinstance(request, CancelTransferRequest):
            return Err(_invalid_request_error("CancelTransferRequest가 필요합니다"))
        rejected = self._guard_public()
        if rejected is not None:
            return rejected
        return Stage5Endpoint.cancel(self, request)

    def status(self, request: TransferStatusRequest):
        if not isinstance(request, TransferStatusRequest):
            return Err(_invalid_request_error("TransferStatusRequest가 필요합니다"))
        rejected = self._guard_public()
        if rejected is not None:
            return rejected
        return Stage5Endpoint.status(self, request)

    def stop(self, request: ServiceStopRequest):
        if not isinstance(request, ServiceStopRequest):
            return Err(_invalid_request_error("ServiceStopRequest가 필요합니다"))
        rejected = self._guard_public()
        if rejected is not None:
            return rejected
        with self._gate_lock:
            self.public_service_published = False
            self._start_terminal = None
        return Stage5Endpoint.stop(self, request)

    # --- graph 검증 (§15) --------------------------------------------------------

    def _validate_effective_graph(self):
        from ..link.data_plane import LinkDataPlane
        from ..mac.interface import Stage4Mac
        from ..presentation.adapter import RegistryPresentationAdapter
        from .phy_facade import Stage7DiagnosticPhyFacade
        from .radio_adapter import Stage7B210RadioAdapter
        from .routing import Stage7RouteAwareLinkPort
        from .support import Stage7Stage5Support

        def mismatch(operation):
            return internal_error(
                Layer.SERVICE,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "Stage 7 profile과 다른 유효 dependency graph입니다",
                details=Details.of(reason="stage7_graph_mismatch", operation=operation),
            )

        if not isinstance(self._presentation, RegistryPresentationAdapter):
            return mismatch("presentation")
        if not isinstance(self._link, Stage7RouteAwareLinkPort):
            return mismatch("link")
        if not isinstance(self._link.delegate, LinkDataPlane):
            return mismatch("link_delegate")
        if not isinstance(self._mac, Stage4Mac):
            return mismatch("mac")
        if not isinstance(self._phy, Stage7DiagnosticPhyFacade):
            return mismatch("phy")
        if not isinstance(self._radio, Stage7B210RadioAdapter):
            return mismatch("radio")
        if self._stage4 is None or not isinstance(self._stage4, Stage7Stage5Support):
            return mismatch("mac_execution")
        graph = self._stage7_graph
        if graph is None:
            return mismatch("stage7_graph_binding")
        rf_a = graph.get("rf_a_stack")
        rf_b = graph.get("rf_b_stack")
        if rf_a is None or rf_b is None or rf_a is rf_b:
            return mismatch("two_lane_binding")
        # canonical `_phy`/`_phy_radio_bridge`는 RF-A stack과 object-identity exact.
        if self._phy is not rf_a.facade or self._phy_radio_bridge is not rf_a.bridge:
            return mismatch("rf_a_canonical_identity")
        if rf_a.facade is rf_b.facade or rf_a.bridge is rf_b.bridge:
            return mismatch("lane_isolation")
        if (
            self._stage7_resolved is None
            or self._stage7_resolved.source_schema_version != 4
        ):
            return mismatch("schema_v4_fingerprint")
        return None

    # --- restart factory seam (§15) ----------------------------------------------

    def _reset_stage5_runtime_locked(self) -> None:
        super()._reset_stage5_runtime_locked()
        factory = self._stage7_restart_factory
        if factory is None:
            return
        try:
            factory.rebuild(self)
        except Exception as error:  # noqa: BLE001
            from ..common.contracts.service import EndpointHealth

            self._tx_inhibited = True
            self._commit_health(EndpointHealth.UNHEALTHY)
            self._record_internal_error(
                internal_error(
                    Layer.RUNTIME,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "Stage7RestartFactory가 실패해 재시작을 거부합니다",
                    details=Details.of(reason=type(error).__name__),
                ).error
            )


def _not_started_error_result():
    return _not_started_error()


__all__ = (
    "Stage7Endpoint",
    "Stage7PublicStartGate",
    "canonical_dto_digest",
)
