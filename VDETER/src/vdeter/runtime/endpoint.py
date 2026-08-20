"""Stack Runtime endpoint (`stage0_baseline.md` §4.1, §5.3, §6.3).

Runtime은 계층을 조립하고 결과·event를 다음 인접 Port로 전달한다. Message 형식, 자원
할당, 상태 전이, profile 값 또는 DSP 규칙을 구현하지 않는다. 하위 계층은 상위 concrete
구현을 직접 호출하지 않고 `InternalEvent`를 Runtime에 전달하며, Runtime이 event 종류와
이미 정의된 결과 DTO에 따라 다음 Port를 호출한다.

송신 경로
```text
Client → Service → Presentation → Link analysis → MAC planning
       → Link packet build → PHY encode → Radio timed TX
```

수신 경로
```text
Radio samples → PHY recovery → MAC receive decision → Link packet decode
              → MAC message handling → Presentation decode → Service event → Client
```
"""

from __future__ import annotations

from functools import wraps

from ..common.addressing import OriginalDestination, OriginalSource, RouteAddresses
from ..common.contracts.link import LinkAnalyzeRequest, LinkBuildRequest, LinkDecodeRequest
from ..common.contracts.mac import MacActionKind, MacMessageInput
from ..common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from ..common.contracts.presentation import PresentationDecodeRequest, PresentationEncodeRequest
from ..common.contracts.radio import (
    RadioConfigureRequest,
    RadioRxRequest,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStopRequest,
    RadioTxRequest,
)
from ..common.contracts.service import (
    CancelAccepted,
    CancelTransferRequest,
    EndpointHealth,
    ReceivedApplicationData,
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
)
from ..common.events import (
    EventSeverity,
    InternalEvent,
    InternalEventType,
    ServiceEvent,
    ServiceEventType,
)
from ..common.results import Err, Ok, internal_error
from ..service.models import TransferRecord
from .projection import PublicErrorCode, to_service_error
from .wiring import EndpointDependencies, build_stage0_layers

#: Runtime이 다음 Port 호출로 route하는 event 종류. 그 밖의 event는 기록만 한다.
ROUTED_EVENT_TYPES = frozenset(
    {
        InternalEventType.TRANSMISSION_PLANNED,
        InternalEventType.BURST_COMPLETED,
        InternalEventType.SAMPLES_RECEIVED,
    }
)


def _service_boundary(method):
    """Service 공개 method에서 programming error가 raw exception으로 새지 않게 한다."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # programming/invariant failure만 이 경계까지 도달한다.
            return self._programming_failure(method.__name__, error)

    return guarded


class VdeTerEndpoint:
    """`VdeService` 공개 계약을 구현하는 단계 0 endpoint.

    단계 0에서는 Fake/Stub 계층을 조립해 요청과 event가 모든 계층을 통과하는지만
    검증한다. protocol 적합성을 의미하지 않는다.
    """

    __slots__ = (
        "_dependencies",
        "_presentation",
        "_link",
        "_mac",
        "_phy",
        "_radio",
        "_health",
        "_transfers",
        "_pending_presentation",
        "_burst_transfers",
        "_started",
    )

    def __init__(self, dependencies: EndpointDependencies) -> None:
        self._dependencies = dependencies
        self._radio = dependencies.radio
        self._presentation, self._link, self._mac, self._phy = build_stage0_layers(
            dependencies, dependencies.internal_event_sink
        )
        # 하위 계층은 상위 concrete 구현을 직접 호출하지 않는다. event bus가 내부 event를
        # 기록한 뒤 Runtime router에만 전달한다. binding은 조립 시 한 번만 한다.
        dependencies.internal_event_sink.bind_router(self)
        self._health = EndpointHealth.STOPPED
        self._transfers = {}
        self._pending_presentation = {}
        self._burst_transfers = {}
        self._started = False

    # --- 관찰 ---

    @property
    def health(self) -> EndpointHealth:
        return self._health

    @property
    def dependencies(self) -> EndpointDependencies:
        return self._dependencies

    @property
    def radio(self):
        return self._radio

    # --- 내부 event routing (계층 → event bus → Runtime) ---

    def route_event(self, event: InternalEvent) -> None:
        """event bus가 기록을 마친 내부 event를 다음 인접 Port로 전달한다.

        Runtime은 결과를 route할 뿐 protocol field, 상태 전이, profile 값 또는 DSP 규칙을
        복제하지 않는다.
        """

        if not isinstance(event, InternalEvent):
            return
        if event.event_type in ROUTED_EVENT_TYPES:
            try:
                self._route(event)
            except Exception as error:
                self._programming_failure("route_event", error)

    # --- Service 공개 계약 ---

    @_service_boundary
    def start(self, request: ServiceStartRequest):
        if not isinstance(request, ServiceStartRequest):
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.INVALID_REQUEST, "ServiceStartRequest가 필요합니다"
                    )
                )
            )
        if self._started:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.SERVICE_ALREADY_STARTED,
                        "이미 시작된 Service입니다",
                    )
                )
            )
        endpoint_config = self._dependencies.configuration.endpoint
        if request.role is not endpoint_config.role or request.local_station_id != endpoint_config.station_id:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.INVALID_REQUEST,
                        "시작 요청이 endpoint 설정과 일치하지 않습니다",
                    )
                )
            )
        self._health = EndpointHealth.STARTING

        configured = self._radio.configure(
            RadioConfigureRequest(configuration=self._dependencies.configuration.radio)
        )
        if configured.is_err:
            return self._fail_start(configured.error)
        started = self._radio.start(RadioStartRequest(reason="endpoint start"))
        if started.is_err:
            return self._fail_start(started.error)

        rx_requested = self._request_rx_window()
        if rx_requested is not None and rx_requested.is_err:
            return self._fail_start(rx_requested.error)

        self._started = True
        self._health = EndpointHealth.RUNNING
        now = self._time().now_utc()
        published = self._emit_service_event(
            ServiceEventType.SERVICE_STARTED,
            severity=EventSeverity.INFO,
            details=Details.of(endpoint_id=endpoint_config.endpoint_id),
        )
        if published.is_err:
            return self._fail_start(published.error)
        return Ok(
            ServiceStarted(
                endpoint_id=endpoint_config.endpoint_id,
                role=endpoint_config.role,
                local_station_id=endpoint_config.station_id,
                health=self._health,
                started_at=now,
            )
        )

    @_service_boundary
    def stop(self, request: ServiceStopRequest):
        if not isinstance(request, ServiceStopRequest):
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.INVALID_REQUEST, "ServiceStopRequest가 필요합니다"
                    )
                )
            )
        if not self._started:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.SERVICE_NOT_STARTED, "시작되지 않은 Service입니다"
                    )
                )
            )
        stopped = self._radio.stop(RadioStopRequest(reason=request.reason))
        if stopped.is_err:
            self._health = EndpointHealth.DEGRADED
            return Err(to_service_error(stopped.error))
        self._started = False
        self._health = EndpointHealth.STOPPED
        self._reset_runtime_state()
        published = self._emit_service_event(
            ServiceEventType.SERVICE_STOPPED, severity=EventSeverity.INFO
        )
        if published.is_err:
            return Err(to_service_error(published.error))
        return Ok(
            ServiceStopped(
                endpoint_id=self._dependencies.configuration.endpoint.endpoint_id,
                health=self._health,
                stopped_at=self._time().now_utc(),
            )
        )

    @_service_boundary
    def submit(self, request: SubmitTransferRequest):
        if not isinstance(request, SubmitTransferRequest):
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.INVALID_REQUEST, "SubmitTransferRequest가 필요합니다"
                    )
                )
            )
        if not self._started:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.SERVICE_NOT_STARTED, "Service가 시작되지 않았습니다"
                    )
                )
            )
        endpoint_config = self._dependencies.configuration.endpoint
        limit = endpoint_config.service.max_payload_bytes
        if len(request.payload) > limit:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.INVALID_REQUEST,
                        "구성된 최대 payload 크기를 초과했습니다",
                        details=Details.of(payload_size=len(request.payload), max_payload_bytes=limit),
                    )
                )
            )
        active = sum(
            1 for record in self._transfers.values() if record.state not in TERMINAL_TRANSFER_STATES
        )
        if active >= endpoint_config.service.max_concurrent_transfers:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.INVALID_REQUEST,
                        "동시 송신 한도를 초과했습니다",
                        details=Details.of(reason="max_concurrent_transfers"),
                    )
                )
            )

        transfer_id = self._dependencies.id_source.new_transfer_id()
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
        if encoded.is_err:
            # 접수 전 거부다. 전송 record를 만들지 않는다.
            self._record_internal_error(encoded.error)
            return Err(to_service_error(encoded.error))

        now = self._time().now_utc()
        self._transfers[transfer_id.value] = TransferRecord(
            transfer_id=transfer_id, state=TransferState.QUEUED, updated_at=now
        )
        self._pending_presentation[transfer_id.value] = encoded.value
        queued_event = self._emit_transfer_state(transfer_id, TransferState.QUEUED)
        if queued_event.is_err:
            self._abort_transfer_after_event_failure(transfer_id, queued_event.error)
            return Err(to_service_error(queued_event.error))

        analyzed = self._link.analyze_transfer(
            LinkAnalyzeRequest(
                transfer_id=transfer_id,
                route=route,
                ack_required=request.ack_required,
                presentation_data=encoded.value.data,
            )
        )
        if analyzed.is_err:
            self._fail_transfer(transfer_id, analyzed.error)
            return Ok(TransferAccepted(transfer_id=transfer_id, state=TransferState.QUEUED, accepted_at=now))

        transmitting = self._set_transfer_state(transfer_id, TransferState.TRANSMITTING)
        if transmitting.is_err:
            self._abort_transfer_after_event_failure(transfer_id, transmitting.error)
            return Err(to_service_error(transmitting.error))
        accepted = self._mac.submit_requirement(analyzed.value)
        if accepted.is_err:
            self._fail_transfer(transfer_id, accepted.error)
        return Ok(
            TransferAccepted(transfer_id=transfer_id, state=TransferState.QUEUED, accepted_at=now)
        )

    @_service_boundary
    def cancel(self, request: CancelTransferRequest):
        if not isinstance(request, CancelTransferRequest):
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.INVALID_REQUEST, "CancelTransferRequest가 필요합니다"
                    )
                )
            )
        record = self._transfers.get(request.transfer_id.value)
        if record is None:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.UNKNOWN_TRANSFER,
                        "알 수 없는 transfer_id입니다",
                        correlation=Correlation(transfer_id=request.transfer_id),
                    )
                )
            )
        if record.state in TERMINAL_TRANSFER_STATES:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.INVALID_REQUEST,
                        "이미 종료된 전송은 취소할 수 없습니다",
                        correlation=Correlation(transfer_id=request.transfer_id),
                    )
                )
            )
        cancelled = self._set_transfer_state(request.transfer_id, TransferState.CANCELLED)
        self._pending_presentation.pop(request.transfer_id.value, None)
        self._detach_transfer_bursts(request.transfer_id)
        if cancelled.is_err:
            return Err(to_service_error(cancelled.error))
        return Ok(
            CancelAccepted(
                transfer_id=request.transfer_id,
                state=TransferState.CANCELLED,
                accepted_at=self._time().now_utc(),
            )
        )

    @_service_boundary
    def status(self, request: TransferStatusRequest):
        if not isinstance(request, TransferStatusRequest):
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE, ErrorCode.INVALID_REQUEST, "TransferStatusRequest가 필요합니다"
                    )
                )
            )
        record = self._transfers.get(request.transfer_id.value)
        if record is None:
            return Err(
                to_service_error(
                    self._error(
                        Layer.SERVICE,
                        ErrorCode.UNKNOWN_TRANSFER,
                        "알 수 없는 transfer_id입니다",
                        correlation=Correlation(transfer_id=request.transfer_id),
                    )
                )
            )
        return Ok(
            TransferStatusSnapshot(
                transfer_id=record.transfer_id,
                state=record.state,
                updated_at=record.updated_at,
                error=record.error,
            )
        )

    def radio_status(self):
        """진단용 Radio 상태 조회. 공개 Service 계약이 아니다."""

        return self._radio.status(RadioStatusRequest())

    # --- event routing ---

    def _route(self, event: InternalEvent) -> None:
        if event.event_type is InternalEventType.TRANSMISSION_PLANNED:
            self._route_transmission_plan(event)
        elif event.event_type is InternalEventType.BURST_COMPLETED:
            self._route_burst_completed(event)
        elif event.event_type is InternalEventType.SAMPLES_RECEIVED:
            self._route_samples_received(event)

    def _route_transmission_plan(self, event: InternalEvent) -> None:
        plan = event.payload
        transfer_id = plan.transfer_id
        presentation = self._pending_presentation.get(transfer_id.value)
        if presentation is None:
            self._fail_transfer(
                transfer_id,
                self._error(
                    Layer.RUNTIME,
                    ErrorCode.UNKNOWN_TRANSFER,
                    "전송 계획에 대응하는 Presentation data가 없습니다",
                    correlation=Correlation(transfer_id=transfer_id),
                ),
            )
            return

        built = self._link.build_packet(
            LinkBuildRequest(plan=plan, presentation_data=presentation.data, sequential_id=None)
        )
        if built.is_err:
            self._fail_transfer(transfer_id, built.error)
            return
        packet = built.value
        self._record(
            InternalEventType.PACKET_BUILT,
            Layer.LINK,
            payload=packet,
            correlation=Correlation(transfer_id=transfer_id, packet_id=packet.packet_id),
        )

        encoded = self._phy.encode_packet(
            PhyEncodeRequest(
                packet_id=packet.packet_id,
                link_id=packet.link_id,
                channel=plan.channel,
                transmit_at=plan.transmit_at,
                packet=packet.packet,
            )
        )
        if encoded.is_err:
            self._fail_transfer(transfer_id, encoded.error)
            return
        burst = encoded.value
        self._burst_transfers[burst.burst_id.value] = transfer_id
        self._record(
            InternalEventType.BURST_ENCODED,
            Layer.PHY,
            payload=burst,
            correlation=Correlation(
                transfer_id=transfer_id, packet_id=packet.packet_id, burst_id=burst.burst_id
            ),
        )

        scheduled = self._radio.schedule_tx(
            RadioTxRequest(
                burst_id=burst.burst_id,
                channel=burst.channel,
                transmit_at=burst.transmit_at,
                samples=burst.samples,
            )
        )
        if scheduled.is_err:
            self._fail_transfer(transfer_id, scheduled.error)
            return
        self._record(
            InternalEventType.BURST_SCHEDULED,
            Layer.RADIO,
            correlation=Correlation(transfer_id=transfer_id, burst_id=burst.burst_id),
        )

    def _route_burst_completed(self, event: InternalEvent) -> None:
        burst_id = event.correlation.burst_id
        if burst_id is None:
            return
        transfer_id = self._burst_transfers.pop(burst_id.value, None)
        if transfer_id is None:
            return
        self._pending_presentation.pop(transfer_id.value, None)
        record = self._transfers.get(transfer_id.value)
        if record is None or record.state in TERMINAL_TRANSFER_STATES:
            return
        completed = self._set_transfer_state(transfer_id, TransferState.COMPLETED)
        if completed.is_err:
            self._handle_event_sink_failure(completed.error)

    def _route_samples_received(self, event: InternalEvent) -> None:
        received = event.payload
        endpoint_config = self._dependencies.configuration.endpoint
        recovered = self._phy.decode_samples(
            PhyDecodeRequest(
                channel=received.channel,
                samples=received.samples,
                allowed_link_ids=endpoint_config.link.supported_link_ids,
            )
        )
        if recovered.is_err:
            self._report_internal_error(recovered.error)
            return
        self._record(
            InternalEventType.PACKETS_RECOVERED, Layer.PHY, payload=recovered.value
        )

        decode_action = self._mac.handle_recovered_packet(recovered.value)
        if decode_action.is_err:
            self._report_internal_error(decode_action.error)
            return
        action = decode_action.value
        self._record(
            InternalEventType.PACKET_DECODE_REQUESTED,
            Layer.MAC,
            payload=action,
            correlation=Correlation(packet_id=action.packet.packet_id),
        )
        if not action.accepted:
            return

        decoded = self._link.decode_packet(
            LinkDecodeRequest(
                packet_id=action.packet.packet_id,
                link_id=action.packet.link_id,
                received_at=action.packet.received_at,
                packet=action.packet.packet,
            )
        )
        if decoded.is_err:
            self._report_internal_error(decoded.error)
            return
        self._record(
            InternalEventType.MESSAGES_DECODED,
            Layer.LINK,
            payload=decoded.value,
            correlation=Correlation(packet_id=decoded.value.packet_id),
        )

        handled = self._mac.handle_message(
            MacMessageInput(decoded=decoded.value, channel=action.packet.channel)
        )
        if handled.is_err:
            self._report_internal_error(handled.error)
            return
        self._record(
            InternalEventType.MAC_ACTIONS_READY, Layer.MAC, payload=handled.value
        )

        for mac_action in handled.value.actions:
            if mac_action.kind is not MacActionKind.DELIVER_PRESENTATION_DATA:
                continue
            self._deliver_presentation_data(mac_action, action.packet.received_at)

    def _deliver_presentation_data(self, mac_action, received_at) -> None:
        application = self._presentation.decode_payload(
            PresentationDecodeRequest(
                route=mac_action.route,
                data=mac_action.presentation_data,
                received_at=received_at,
            )
        )
        if application.is_err:
            self._report_internal_error(application.error)
            return
        data = application.value
        self._record(
            InternalEventType.APPLICATION_DATA_READY, Layer.PRESENTATION, payload=data
        )
        destination = data.route.original_destination.address
        received = ReceivedApplicationData(
            original_source=data.route.original_source.station_id,
            original_destination=destination,
            payload_format=data.payload_format,
            payload=data.application_payload,
            received_at=data.received_at,
        )
        self._emit_service_event(
            ServiceEventType.DATA_RECEIVED,
            severity=EventSeverity.INFO,
            received_data=received,
        )

    # --- 상태와 event helper ---

    def _time(self):
        return self._dependencies.time_source

    def _error(
        self,
        layer: Layer,
        code: str,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.VALIDATION,
        correlation: Correlation = Correlation(),
        details: Details = Details(),
    ) -> InternalProtocolError:
        error = InternalProtocolError(
            layer=layer,
            code=code,
            category=category,
            message=message,
            correlation=correlation,
            details=details,
        )
        self._record_internal_error(error)
        return error

    def _record_internal_error(self, error: InternalProtocolError):
        published = self._dependencies.internal_event_sink.publish(
            InternalEvent(
                event_id=self._dependencies.id_source.new_event_id(),
                event_type=InternalEventType.LAYER_ERROR,
                occurred_at=self._time().now_utc(),
                severity=EventSeverity.ERROR,
                layer=error.layer,
                correlation=error.correlation,
                error=error,
            )
        )
        if published.is_err:
            self._handle_event_sink_failure(published.error)
        return published

    def _report_internal_error(self, error: InternalProtocolError) -> None:
        """내부 오류를 내부 log에 남기고 공개 오류 event로 projection한다."""

        self._record_internal_error(error)
        published = self._emit_service_event(
            ServiceEventType.ERROR,
            severity=EventSeverity.ERROR,
            error=to_service_error(error),
        )
        if published.is_err:
            self._handle_event_sink_failure(published.error)

    def _record(
        self,
        event_type: InternalEventType,
        layer: Layer,
        *,
        payload=None,
        correlation: Correlation = Correlation(),
        details: Details = Details(),
    ):
        published = self._dependencies.internal_event_sink.publish(
            InternalEvent(
                event_id=self._dependencies.id_source.new_event_id(),
                event_type=event_type,
                occurred_at=self._time().now_utc(),
                severity=EventSeverity.INFO,
                layer=layer,
                correlation=correlation,
                payload=payload,
                details=details,
            )
        )
        if published.is_err:
            self._handle_event_sink_failure(published.error)
        return published

    def _set_transfer_state(self, transfer_id, state: TransferState, error=None):
        record = self._transfers.get(transfer_id.value)
        now = self._time().now_utc()
        if record is not None and record.state in TERMINAL_TRANSFER_STATES:
            # 완료·실패·취소는 최종 상태다. 늦게 도착한 Radio/MAC event가 이를
            # 덮어쓰지 못하도록 idempotent no-op으로 처리한다.
            return Ok(False)
        if record is None:
            record = TransferRecord(transfer_id=transfer_id, state=state, updated_at=now, error=error)
        else:
            record = record.with_state(state, now, error)
        self._transfers[transfer_id.value] = record
        return self._emit_transfer_state(transfer_id, state, error)

    def _emit_transfer_state(self, transfer_id, state: TransferState, error=None):
        internal = self._dependencies.internal_event_sink.publish(
            InternalEvent(
                event_id=self._dependencies.id_source.new_event_id(),
                event_type=InternalEventType.TRANSFER_STATE_CHANGED,
                occurred_at=self._time().now_utc(),
                severity=EventSeverity.INFO,
                layer=Layer.SERVICE,
                correlation=Correlation(transfer_id=transfer_id),
                details=Details.of(state=state.value),
            )
        )
        if internal.is_err:
            self._handle_event_sink_failure(internal.error)
            return internal
        return self._emit_service_event(
            ServiceEventType.TRANSFER_STATE_CHANGED,
            severity=EventSeverity.INFO,
            transfer_id=transfer_id,
            transfer_state=state,
            error=error,
        )

    def _fail_transfer(self, transfer_id, error: InternalProtocolError) -> None:
        self._record_internal_error(error)
        public = to_service_error(error)
        self._pending_presentation.pop(transfer_id.value, None)
        self._detach_transfer_bursts(transfer_id)
        record = self._transfers.get(transfer_id.value)
        if record is not None and record.state in TERMINAL_TRANSFER_STATES:
            return
        now = self._time().now_utc()
        if record is None:
            record = TransferRecord(
                transfer_id=transfer_id, state=TransferState.FAILED, updated_at=now, error=public
            )
        else:
            record = record.with_state(TransferState.FAILED, now, public)
        self._transfers[transfer_id.value] = record
        published = self._emit_transfer_state(transfer_id, TransferState.FAILED, public)
        if published.is_err:
            self._handle_event_sink_failure(published.error)

    def _abort_transfer_after_event_failure(self, transfer_id, error: InternalProtocolError) -> None:
        """동기 Service event 전달 실패 뒤 추가 event 없이 transfer를 종료한다."""

        public = to_service_error(error)
        now = self._time().now_utc()
        record = self._transfers.get(transfer_id.value)
        if record is not None and record.state not in TERMINAL_TRANSFER_STATES:
            self._transfers[transfer_id.value] = record.with_state(
                TransferState.FAILED, now, public
            )
        self._pending_presentation.pop(transfer_id.value, None)
        self._detach_transfer_bursts(transfer_id)

    def _detach_transfer_bursts(self, transfer_id) -> None:
        """취소·실패한 transfer에 대한 늦은 Radio 완료 event 연결을 제거한다."""

        stale = [
            burst_id
            for burst_id, mapped_transfer in self._burst_transfers.items()
            if mapped_transfer == transfer_id
        ]
        for burst_id in stale:
            self._burst_transfers.pop(burst_id, None)

    def _handle_event_sink_failure(self, error: InternalProtocolError) -> None:
        """event 전달 실패를 endpoint health에 반영한다.

        실패한 sink로 다시 오류 event를 발행하면 재귀 실패가 되므로 여기서는 health만
        변경하고, 동기 Service method는 원래 publish 오류를 호출자에게 반환한다.
        """

        if self._health is EndpointHealth.RUNNING:
            self._health = EndpointHealth.DEGRADED
        elif self._health is EndpointHealth.STARTING:
            self._health = EndpointHealth.UNHEALTHY

    def _fail_start(self, error: InternalProtocolError):
        # configure/start 일부가 성공한 뒤 후속 단계가 실패했을 수 있으므로 장치 실행을
        # best-effort로 되돌린다. 원래 실패 원인은 아래의 구조화된 결과로 보존한다.
        try:
            self._radio.stop(RadioStopRequest(reason="endpoint start rollback"))
        except Exception:
            pass
        self._started = False
        self._health = EndpointHealth.UNHEALTHY
        self._record_internal_error(error)
        self._emit_service_event(
            ServiceEventType.ENDPOINT_HEALTH_CHANGED,
            severity=EventSeverity.ERROR,
            error=to_service_error(error),
            details=Details.of(health=self._health.value),
        )
        return Err(to_service_error(error))

    def _programming_failure(self, operation: str, caught: Exception):
        """깨진 invariant를 endpoint failure로 승격하고 공개 오류로 일반화한다."""

        self._health = EndpointHealth.UNHEALTHY
        error = InternalProtocolError(
            layer=Layer.RUNTIME,
            code=ErrorCode.INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            message=f"{operation} 처리 중 programming error가 발생했습니다",
            details=Details.of(
                operation=operation,
                exception_type=type(caught).__name__,
            ),
        )
        # event sink 자체가 깨진 경우에도 Service 경계 밖으로 원래 exception을 다시 내보내지
        # 않는다. health 전환과 공개 오류 반환은 항상 유지한다.
        try:
            self._record_internal_error(error)
        except Exception:
            pass
        try:
            self._emit_service_event(
                ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                severity=EventSeverity.ERROR,
                error=to_service_error(error),
                details=Details.of(health=self._health.value),
            )
        except Exception:
            pass
        return Err(to_service_error(error))

    def _reset_runtime_state(self) -> None:
        """stop 뒤 동일 endpoint를 다시 시작해도 이전 실행 상태를 복원하지 않는다."""

        self._transfers.clear()
        self._pending_presentation.clear()
        self._burst_transfers.clear()
        self._presentation, self._link, self._mac, self._phy = build_stage0_layers(
            self._dependencies, self._dependencies.internal_event_sink
        )

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
        published = self._dependencies.service_event_sink.publish(
            ServiceEvent(
                event_id=self._dependencies.id_source.new_event_id(),
                event_type=event_type,
                occurred_at=self._time().now_utc(),
                severity=severity,
                transfer_id=transfer_id,
                transfer_state=transfer_state,
                received_data=received_data,
                error=error,
                details=details,
            )
        )
        if published.is_err:
            self._handle_event_sink_failure(published.error)
        return published

    def _request_rx_window(self):
        """시작 시 설정된 RX channel의 수신 구간을 요청한다."""

        endpoint_config = self._dependencies.configuration.endpoint
        now = self._time().now_utc()
        return self._radio.request_rx(
            RadioRxRequest(
                channel=endpoint_config.mac.rx_channel,
                start_at=now.shifted(endpoint_config.mac.transmit_lead_nanoseconds),
                duration_nanoseconds=max(endpoint_config.mac.transmit_lead_nanoseconds, 1),
                sample_rate_hz=endpoint_config.phy.sample_rate_hz,
            )
        )


def build_endpoint(dependencies: EndpointDependencies):
    """의존성으로 endpoint를 만든다. 계층 조립 외의 protocol 처리는 하지 않는다."""

    if not isinstance(dependencies, EndpointDependencies):
        return internal_error(
            Layer.RUNTIME,
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "EndpointDependencies가 필요합니다",
        )
    return Ok(VdeTerEndpoint(dependencies))


__all__ = ["VdeTerEndpoint", "build_endpoint", "PublicErrorCode", "ROUTED_EVENT_TYPES"]
