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

from ..common.addressing import (
    AddressKind,
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from ..common.contracts.link import (
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    TransferControlKind,
    TransferRequirement,
    TbbAssemblyControlRequest,
    TbbEncodeRequest,
)
from ..common.contracts.mac import MacActionKind, MacMessageInput
from ..common.contracts.mac_execution import (
    CancelWakeupCommand,
    ClockAdvancedEvent,
    CommandCompletedEvent,
    DeliverPresentationDataCommand,
    DependencyFailedEvent,
    EndpointStoppingEvent,
    InvokeLinkCommand,
    InvokePhyCommand,
    LinkResultEvent,
    MacCommandBatch,
    MacFailureReason,
    MacReasonCode,
    PhyReceiveWindowPlan,
    PhyRxWindowAcceptedEvent,
    PhyRxWindowClosedEvent,
    PhyTxAcceptedEvent,
    PhyTxCompletedEvent,
    PresentationDeliveryResultEvent,
    PublishTransferStateCommand,
    PublishTransmissionPlanCommand,
    ScheduleWakeupCommand,
    TimerPurpose,
    WakeupFiredEvent,
)
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
from ..common.time import ClockDomain, TimePoint
from ..service.models import TransferRecord
from .mac_commands import MAX_CONSUMER_ATTEMPTS
from .projection import PublicErrorCode, to_service_error
from .wiring import EndpointDependencies, build_stage0_layers

#: Runtime이 다음 Port 호출로 route하는 event 종류. 그 밖의 event는 기록만 한다.
#: `LAYER_ERROR`는 wideband bridge ledger가 Radio RX terminal을 결속할 때만 소비하고
#: 그 밖의 경우는 기존과 같이 기록만 한다 (`stage6_phy_common_baseline.md` §13.2).
ROUTED_EVENT_TYPES = frozenset(
    {
        InternalEventType.TRANSMISSION_PLANNED,
        InternalEventType.BURST_COMPLETED,
        InternalEventType.SAMPLES_RECEIVED,
        InternalEventType.RADIO_RX_WINDOW_COMPLETED,
        InternalEventType.LAYER_ERROR,
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
        "_phy_radio_bridge",
        "_radio",
        "_health",
        "_transfers",
        "_pending_presentation",
        "_burst_transfers",
        "_started",
        "_stage4",
    )

    def __init__(self, dependencies: EndpointDependencies) -> None:
        self._dependencies = dependencies
        self._radio = dependencies.radio
        self._presentation, self._link, self._mac, self._phy = build_stage0_layers(
            dependencies, dependencies.internal_event_sink
        )
        self._phy_radio_bridge = None
        # 하위 계층은 상위 concrete 구현을 직접 호출하지 않는다. event bus가 내부 event를
        # 기록한 뒤 Runtime router에만 전달한다. binding은 조립 시 한 번만 한다.
        dependencies.internal_event_sink.bind_router(self)
        self._health = EndpointHealth.STOPPED
        self._transfers = {}
        self._pending_presentation = {}
        self._burst_transfers = {}
        self._started = False
        # Stage 4 실행 지원은 wiring이 주입한다. None이면 Stage 0 legacy route만 사용한다.
        self._stage4 = None

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
        if self._phy_radio_bridge is not None:
            applied = getattr(configured.value, "applied_settings", None)
            self._phy_radio_bridge.begin_generation(
                applied.configure_generation if applied is not None else 1
            )
            radio_config = self._dependencies.configuration.radio
            limits = radio_config.resource_limits
            if limits is not None:
                rx_channels = sum(
                    1 for binding in radio_config.channel_bindings if binding.rx_enabled
                )
                self._phy_radio_bridge.set_buffer_budget(
                    rx_channels * limits.rx_window_max_count * 65_536
                )
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
        if self._stage4 is not None:
            self._stage4.begin_stop(request.reason or "service_stop")
        stopped = self._radio.stop(RadioStopRequest(reason=request.reason))
        if stopped.is_err:
            self._health = EndpointHealth.DEGRADED
            return Err(to_service_error(stopped.error))
        if self._stage4 is not None:
            self._stage4.finish_stop()
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

        analysis_route = route
        if self._stage4 is not None:
            deployment = self._stage4.mac.coordinator.deployment
            message_type = 92 if request.ack_required else 93
            segment_size = 4 + len(encoded.value.data)
            short_eligible = any(
                dict(profile.message_payload_capacities).get(message_type, 0)
                >= segment_size
                for profile in self._dependencies.registries.link_profile.entries()
            )
            if (
                not short_eligible
                and endpoint_config.station_id.value != deployment.roc_station_id.value
                and request.destination.kind is AddressKind.ADDRESSED
                and request.destination.station_id.value != deployment.roc_station_id.value
                and deployment.is_member(request.destination.station_id)
            ):
                analysis_route = RouteAddresses(
                    original_source=route.original_source,
                    original_destination=route.original_destination,
                    node_source=NodeSource(endpoint_config.station_id),
                    node_destination=NodeDestination(Addressed(deployment.roc_station_id)),
                )
        analyzed = self._link.analyze_transfer(
            LinkAnalyzeRequest(
                transfer_id=transfer_id,
                route=analysis_route,
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
            # Stage 4 build-only branch와 Stage 0 legacy branch를 동시에 실행하지 않는다.
            if self._stage4 is not None and self._stage4.owns_transmission_event(
                event.event_id
            ):
                self._stage4.route_stage4_transmission(event)
                return
            self._route_transmission_plan(event)
        elif event.event_type is InternalEventType.BURST_COMPLETED:
            if self._stage4 is not None and self._stage4.on_burst_completed(event):
                return
            self._route_burst_completed(event)
        elif event.event_type is InternalEventType.SAMPLES_RECEIVED:
            if self._stage4 is not None:
                self._stage4.on_samples_received(event)
                return
            self._route_samples_received(event)
        elif event.event_type is InternalEventType.RADIO_RX_WINDOW_COMPLETED:
            if self._stage4 is not None:
                self._stage4.on_rx_window_completed(event)
        elif event.event_type is InternalEventType.LAYER_ERROR:
            if self._stage4 is not None and self._phy_radio_bridge is not None:
                self._stage4.on_layer_error(event)

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
        if self._phy_radio_bridge is not None:
            converted = self._phy_radio_bridge.to_device_burst(burst)
            if converted.is_err:
                self._fail_transfer(transfer_id, converted.error)
                return
            burst = converted.value
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
        decode_request = PhyDecodeRequest(
            channel=received.channel,
            samples=received.samples,
            allowed_link_ids=endpoint_config.link.supported_link_ids,
        )
        if self._phy_radio_bridge is not None:
            prepared = self._phy_radio_bridge.to_native_decode_request(decode_request)
            if prepared.is_err:
                self._report_internal_error(prepared.error)
                return
            if prepared.value is None:
                return
            decode_request = prepared.value
        recovered = self._phy.decode_samples(decode_request)
        if recovered.is_err:
            self._report_internal_error(recovered.error)
            return
        self._record(
            InternalEventType.PACKETS_RECOVERED, Layer.PHY, payload=recovered.value
        )

        self._deliver_recovered(recovered.value, stage4=None)

    def _deliver_recovered(self, recovered_set, *, stage4=None) -> None:
        """동결 `MacPort` 수신 경로. Stage 4도 같은 Port를 통해 재진입한다."""

        decode_action = self._mac.handle_recovered_packet(recovered_set)
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
            if stage4 is not None:
                stage4.report_error(handled.error)
            else:
                self._report_internal_error(handled.error)
            return
        self._record(
            InternalEventType.MAC_ACTIONS_READY, Layer.MAC, payload=handled.value
        )

        for delivery_index, mac_action in enumerate(handled.value.actions):
            if mac_action.kind is not MacActionKind.DELIVER_PRESENTATION_DATA:
                continue
            # Runtime은 같은 `MacMessageInput.decoded.received_at`을 Presentation/Service
            # 수신시각으로 함께 사용한다.
            delivered = self._deliver_presentation_bytes(
                mac_action.route, mac_action.presentation_data, decoded.value.received_at
            )
            # Opt-in diagnostic receipt: after the actual application sink has
            # accepted, never inferred from PHY decode or transmission success.
            receipt = getattr(stage4, "note_application_delivery", None)
            if delivered.is_ok and callable(receipt):
                receipt(decoded.value.packet_id)
            if stage4 is None:
                if delivered.is_err:
                    self._report_internal_error(delivered.error)
                continue
            # ACK-required Type 92의 peer ACK는 Service sink 수락 확인 뒤에만 계획한다.
            stage4.enqueue(
                PresentationDeliveryResultEvent(
                    packet_id=decoded.value.packet_id,
                    delivery_index=delivery_index,
                    delivered_at_monotonic=self._time().now_monotonic(),
                    error=None if delivered.is_ok else delivered.error,
                ),
                priority=True,
            )

    def _deliver_presentation_bytes(self, route, presentation_data, received_at):
        """Presentation decode와 Service `DATA_RECEIVED` sink 수락까지 확인한다."""

        application = self._presentation.decode_payload(
            PresentationDecodeRequest(
                route=route,
                data=presentation_data,
                received_at=received_at,
            )
        )
        if application.is_err:
            return application
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
        published = self._emit_service_event(
            ServiceEventType.DATA_RECEIVED,
            severity=EventSeverity.INFO,
            received_data=received,
        )
        if published.is_err:
            return published
        return Ok(received)

    # --- Stage 4 실행 지원 주입 ---

    def attach_stage4(self, support) -> None:
        """Stage 4 dispatcher를 한 번 연결한다. Stage 0 legacy route는 그대로 유지한다."""

        self._stage4 = support

    @property
    def stage4(self):
        return self._stage4

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
        retained_native_phy = self._phy if self._phy_radio_bridge is not None else None
        presentation, link, legacy_mac, phy = build_stage0_layers(
            self._dependencies, self._dependencies.internal_event_sink
        )
        self._presentation, self._link, self._phy = presentation, link, phy
        if retained_native_phy is not None:
            self._phy = retained_native_phy
            self._phy_radio_bridge.reset_stream()
        if self._stage4 is None:
            self._mac = legacy_mac
            return
        self._stage4.reset_for_restart()
        self._mac = self._stage4.mac

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
        """시작 시 설정된 RX channel의 수신 구간을 요청한다.

        Stage 4에서는 MAC이 TBB slot map에 따라 RX window를 소유하므로 Stage 0 startup
        window를 만들지 않는다.
        """

        if self._stage4 is not None:
            return None
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


# --- Stage 4 실행 지원 (`DR-STAGE4-MAC-001`, `012`) ---


class Stage4Support:
    """Stage 4 command dispatcher, ingress 재진입과 build-only route 상태.

    Stage 0 legacy route를 대체하지 않는다. `TRANSMISSION_PLANNED` event ID가 아래의
    pending map/active set에 있을 때만 Stage 4 build-only branch를 선택한다.
    """

    __slots__ = (
        "_endpoint",
        "mac",
        "outbox",
        "ingress",
        "health",
        "lock",
        "pending_transmission_plans",
        "stage4_transmission_event_ids",
        "recent_transmission_event_ids",
        "_burst_commands",
        "_rx_commands",
        "_timers",
        "_consumer_attempts",
        "_pumping",
        "_build_results",
        "_relay_analysis_cleanup",
    )

    def __init__(self, endpoint, mac, outbox, ingress, health, lock) -> None:
        self._endpoint = endpoint
        self.mac = mac
        self.outbox = outbox
        self.ingress = ingress
        self.health = health
        self.lock = lock
        # bounded private map. 공개 DTO를 바꾸지 않으며 64-command outbox 상한 안에 포함된다.
        self.pending_transmission_plans = {}
        self.stage4_transmission_event_ids = set()
        self.recent_transmission_event_ids = []
        self._burst_commands = {}
        self._rx_commands = {}
        self._timers = {}
        self._consumer_attempts = {}
        self._pumping = False
        self._build_results = {}
        self._relay_analysis_cleanup = {}

    # --- Stage 4 route 판정 ---

    def owns_transmission_event(self, event_id: str) -> bool:
        return (
            event_id in self.pending_transmission_plans
            or event_id in self.stage4_transmission_event_ids
        )

    def _retire_transmission_event(self, event_id: str) -> None:
        self.stage4_transmission_event_ids.discard(event_id)
        self.recent_transmission_event_ids.append(event_id)
        if len(self.recent_transmission_event_ids) > 256:
            del self.recent_transmission_event_ids[
                0 : len(self.recent_transmission_event_ids) - 256
            ]

    # --- ingress 재진입 ---

    def enqueue(self, payload, *, correlation=Correlation(), causation_command_id=None, priority=False):
        result = self.ingress.enqueue(
            payload,
            correlation=correlation,
            causation_command_id=causation_command_id,
            priority=priority,
        )
        return result

    def report_error(self, error: InternalProtocolError) -> None:
        """Stage 4 오류를 bounded health log와 기존 Runtime error 경로에 함께 남긴다."""

        reason_code = error.details.get("reason_code", "")
        self.health.record(error.code, reason_code, error.details)
        self._endpoint._report_internal_error(error)

    def _record_endpoint_error(self, error: InternalProtocolError) -> None:
        """Record endpoint-only MAC evidence without a public Service error."""

        reason_code = error.details.get("reason_code", "")
        self.health.record(error.code, reason_code, error.details)
        self._endpoint._record_internal_error(error)

    def _cleanup_relay_analysis(self, command_id: str) -> bool:
        request = self._relay_analysis_cleanup.get(command_id)
        if request is None:
            return True
        cleaned = self._endpoint._link.control_transfer(request)
        if cleaned.is_err:
            self.health.record(
                cleaned.error.code,
                MacFailureReason.DEPENDENCY_FAILED.value,
                Details.of(
                    cleanup="relay_analysis",
                    transfer_id_value=request.transfer_id.value,
                ),
            )
            self._endpoint._report_internal_error(cleaned.error)
            return False
        self._relay_analysis_cleanup.pop(command_id, None)
        return True

    def _cleanup_all_relay_analyses(self) -> None:
        for command_id in tuple(self._relay_analysis_cleanup):
            self._cleanup_relay_analysis(command_id)

    def begin_stop(self, reason_code: str) -> None:
        """새 work를 닫기 전에 accepted dependency가 DRAINING으로 수렴하게 한다."""

        self.enqueue(
            EndpointStoppingEvent(reason_code=reason_code or "service_stop"),
            priority=True,
        )
        self.pump()
        self.outbox.set_draining(True)

    def finish_stop(self) -> None:
        """Radio 정지 뒤 stop-drain의 cleanup allowlist를 즉시 끝낸다."""

        timer = next(
            (
                (timer_id, deadline)
                for timer_id, (deadline, purpose) in self._timers.items()
                if purpose is TimerPurpose.STOP_DRAIN
            ),
            None,
        )
        if timer is None:
            self._rx_commands.clear()
            self._burst_commands.clear()
            self._cleanup_all_relay_analyses()
            return
        timer_id, deadline = timer
        self.enqueue(
            WakeupFiredEvent(
                timer_id=timer_id,
                purpose=TimerPurpose.STOP_DRAIN,
                scheduled_for_monotonic=TimePoint.from_nanoseconds(
                    deadline,
                    ClockDomain.MONOTONIC,
                    self._endpoint._time().now_monotonic().clock_id,
                ),
            ),
            priority=True,
        )
        del self._timers[timer_id]
        self.pump()
        # Radio는 이 호출 전에 이미 정지했다. close/completion callback이 누락된
        # window/burst correlation은 더 이상 terminal event를 만들 수 없으므로
        # STOP_DRAIN commit 뒤 Runtime side ownership도 함께 해제한다.
        self._rx_commands.clear()
        self._burst_commands.clear()
        self._cleanup_all_relay_analyses()

    def reset_for_restart(self) -> None:
        """stop이 끝난 Stage 4 private state를 새 실행의 INITIAL snapshot으로 바꾼다."""

        self._cleanup_all_relay_analyses()
        self.mac.repository.replace_for_restart(type(self.mac.state)())
        self.outbox.clear()
        self.ingress.clear()
        self.health.clear()
        self.pending_transmission_plans.clear()
        self.stage4_transmission_event_ids.clear()
        self.recent_transmission_event_ids.clear()
        self._burst_commands.clear()
        self._rx_commands.clear()
        self._timers.clear()
        self._consumer_attempts.clear()
        self._build_results.clear()
        # Failed Link cleanup still owns live Stage 1 analysis state.  Keep the
        # record across restart so a later stop/retry can release it; clearing the
        # bookkeeping here would falsely report that ownership as discharged.
        self._pumping = False

    def pump(self) -> None:
        """READY batch 실행과 ingress event 처리를 번갈아 진행한다.

        dispatcher도 같은 endpoint lock을 획득해야 dequeue하므로 state commit과 lock 해제
        전에 batch를 관측·실행할 수 없다.
        """

        if self._pumping:
            return
        self._pumping = True
        try:
            for _ in range(4096):
                ready = self.outbox.take_ready()
                if ready:
                    for command in ready:
                        self._execute(command)
                    continue
                self.ingress.release_held()
                event = self.ingress.pop_next()
                if event is None:
                    return
                handled = self.mac.handle_event(event)
                relay_command_id = None
                if (
                    isinstance(event.payload, LinkResultEvent)
                    and isinstance(event.payload.result, TransferRequirement)
                    and event.payload.command_id in self._relay_analysis_cleanup
                ):
                    relay_command_id = event.payload.command_id
                if handled.is_err:
                    self._record_endpoint_error(handled.error)
                    if (
                        relay_command_id is not None
                        and handled.error.details.get("mac_reason")
                        != MacFailureReason.COMMAND_RESULT_UNMATCHED.value
                    ):
                        self._cleanup_relay_analysis(relay_command_id)
                elif relay_command_id is not None:
                    relay = self.mac.state.relay_record(
                        event.payload.result.transfer_id.value
                    )
                    if relay is not None and relay.admitted:
                        # matching result commit이 끝난 순간부터 cleanup은 relay MAC 소유다.
                        self._relay_analysis_cleanup.pop(relay_command_id, None)
        finally:
            self._pumping = False

    def advance_clock(self) -> None:
        """`ClockAdvancedEvent`를 넣고 결과를 소진한다."""

        self.enqueue(ClockAdvancedEvent(sync_status=self._endpoint._time().sync_status()))
        self.pump()

    def fire_due_timers(self) -> None:
        """deadline에 도달한 timer의 `WakeupFiredEvent`를 넣는다."""

        now = self._endpoint._time().now_monotonic().total_nanoseconds()
        for timer_id, record in sorted(self._timers.items()):
            deadline, purpose = record
            if deadline > now:
                continue
            self.enqueue(
                WakeupFiredEvent(
                    timer_id=timer_id,
                    purpose=purpose,
                    scheduled_for_monotonic=TimePoint.from_nanoseconds(
                        deadline,
                        ClockDomain.MONOTONIC,
                        self._endpoint._time().now_monotonic().clock_id,
                    ),
                ),
                priority=True,
            )
            del self._timers[timer_id]
            break
        self.pump()

    def has_due_timer(self) -> bool:
        now = self._endpoint._time().now_monotonic().total_nanoseconds()
        return any(deadline <= now for deadline, _ in self._timers.values())

    # --- command 실행 ---

    def _fail(self, command, error) -> None:
        self.enqueue(
            DependencyFailedEvent(command_id=command.command_id, error=error),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def _complete(self, command) -> None:
        self.enqueue(
            CommandCompletedEvent(command_id=command.command_id),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def _execute(self, command) -> None:
        payload = command.payload
        try:
            if isinstance(payload, PublishTransmissionPlanCommand):
                self._execute_build(command, payload)
            elif isinstance(payload, InvokeLinkCommand):
                self._execute_link(command, payload)
            elif isinstance(payload, InvokePhyCommand):
                self._execute_phy(command, payload)
            elif isinstance(payload, ScheduleWakeupCommand):
                requested = (
                    payload.deadline_monotonic.total_nanoseconds(),
                    payload.purpose,
                )
                if payload.replaces_timer_id is not None:
                    # Coordinator state와 같은 원자 경계로 기존 active timer를
                    # 교체한다. 별도 Cancel command를 만들지 않아 포화 상태에서도
                    # active timer 수와 pending cleanup quota를 늘리지 않는다.
                    self._timers.pop(payload.replaces_timer_id, None)
                existing = self._timers.get(payload.timer_id)
                if existing is not None and existing != requested:
                    self._fail(
                        command,
                        self._endpoint._error(
                            Layer.RUNTIME,
                            ErrorCode.INVALID_REQUEST,
                            "active timer ID was reused with a different deadline or purpose",
                            category=ErrorCategory.VALIDATION,
                            details=Details.of(
                                mac_reason=MacFailureReason.EVENT_INVALID.value,
                                timer_id=payload.timer_id,
                            ),
                        ),
                    )
                    return
                self._timers[payload.timer_id] = requested
                self._complete(command)
            elif isinstance(payload, CancelWakeupCommand):
                self._timers.pop(payload.timer_id, None)
                self._complete(command)
            elif isinstance(payload, PublishTransferStateCommand):
                self._execute_transfer_state(command, payload)
            elif isinstance(payload, DeliverPresentationDataCommand):
                self._execute_delivery(command, payload)
            else:
                self._fail(command, self._endpoint._error(
                    Layer.RUNTIME, ErrorCode.UNSUPPORTED_REQUEST, "알 수 없는 MAC command입니다"
                ))
        except Exception as error:  # noqa: BLE001 - dispatcher 경계에서 구조화한다.
            self._fail(command, self._endpoint._error(
                Layer.RUNTIME,
                ErrorCode.INTERNAL_ERROR,
                "MAC command 실행 중 오류가 발생했습니다",
                category=ErrorCategory.INTERNAL,
                details=Details.of(exception_type=type(error).__name__),
            ))

    def _execute_build(self, command, payload) -> None:
        """Stage 4 build-only route를 정확히 한 번 실행한다."""

        request = payload.request
        event_id = self._endpoint._dependencies.id_source.new_event_id()
        self.pending_transmission_plans[event_id] = (command.command_id, request)
        self.stage4_transmission_event_ids.add(event_id)
        published = self._endpoint._dependencies.internal_event_sink.publish(
            InternalEvent(
                event_id=event_id,
                event_type=InternalEventType.TRANSMISSION_PLANNED,
                occurred_at=self._endpoint._time().now_utc(),
                severity=EventSeverity.INFO,
                layer=Layer.MAC,
                correlation=command.correlation,
                payload=request.plan,
                details=Details.of(
                    link_id=request.plan.link_id.value, channel=request.plan.channel.name
                ),
            )
        )
        if published.is_err:
            # event publish 실패면 map entry를 제거하고 Link를 호출하지 않는다.
            self.pending_transmission_plans.pop(event_id, None)
            self._retire_transmission_event(event_id)
            self._fail(command, published.error)
            return
        outcome = self._build_results.pop(command.command_id, None)
        self._retire_transmission_event(event_id)
        if outcome is None:
            self._fail(command, self._endpoint._error(
                Layer.RUNTIME,
                ErrorCode.INTERNAL_ERROR,
                "Stage 4 build-only branch가 결과를 만들지 못했습니다",
                category=ErrorCategory.INTERNAL,
            ))
            return
        ok, value = outcome
        if not ok:
            self._fail(command, value)
            return
        self.enqueue(
            LinkResultEvent(command_id=command.command_id, result=value),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def route_stage4_transmission(self, event) -> None:
        """Stage 4 build-only branch. `PhyPort`/`RadioAdapter`를 호출하지 않는다."""

        entry = self.pending_transmission_plans.pop(event.event_id, None)
        if entry is None:
            # 이미 소비된 Stage 4 중복이므로 어떤 branch도 실행하지 않는다.
            self.health.record(
                ErrorCode.INVALID_REQUEST,
                MacReasonCode.OUTBOX_RESERVATION_INVALID,
                Details.of(reason="duplicate_stage4_transmission_event"),
            )
            return
        command_id, request = entry
        presentation = self._endpoint._pending_presentation.get(request.transfer_id.value)
        relay = self.mac.state.relay_record(request.transfer_id.value)
        presentation_data = (
            presentation.data
            if presentation is not None
            else relay.presentation_data
            if relay is not None
            else None
        )
        if presentation_data is None:
            self._build_results[command_id] = (
                False,
                self._endpoint._error(
                    Layer.RUNTIME,
                    ErrorCode.UNKNOWN_TRANSFER,
                    "전송 계획에 대응하는 Presentation data가 없습니다",
                    correlation=Correlation(transfer_id=request.transfer_id),
                ),
            )
            return
        built = self._endpoint._link.build_packet(
            LinkBuildRequest(
                plan=request.plan,
                presentation_data=presentation_data,
                sequential_id=None,
                retransmission_number=request.retransmission_number,
                session_frame_index=request.session_frame_index,
                frame_fragment_count=request.frame_fragment_count,
                fragment_position_in_frame=request.fragment_position_in_frame,
            )
        )
        if built.is_err:
            self._build_results[command_id] = (False, built.error)
            return
        packet = built.value
        self._endpoint._record(
            InternalEventType.PACKET_BUILT,
            Layer.LINK,
            payload=packet,
            correlation=Correlation(
                transfer_id=request.transfer_id, packet_id=packet.packet_id
            ),
        )
        self._build_results[command_id] = (True, packet)

    def _execute_link(self, command, payload) -> None:
        request = payload.request
        link = self._endpoint._link
        method = {
            LinkAnalyzeRequest: link.analyze_transfer,
            LinkPacketEncodeRequest: link.encode_packet,
            TbbEncodeRequest: link.encode_tbb,
            LinkFragmentProcessRequest: link.process_fragment,
            LinkReassemblyControlRequest: link.control_reassembly,
            LinkTransferControlRequest: link.control_transfer,
            TbbAssemblyControlRequest: link.control_tbb_assembly,
        }.get(type(request))
        if method is None:
            self._fail(command, self._endpoint._error(
                Layer.RUNTIME, ErrorCode.UNSUPPORTED_REQUEST, "알 수 없는 Link request입니다"
            ))
            return
        result = method(request)
        if result.is_err:
            # 성공 variant를 합성하지 않고 원 오류를 보존한다.
            self._fail(command, result.error)
            return
        relay = (
            self.mac.state.relay_record(request.transfer_id.value)
            if isinstance(request, LinkAnalyzeRequest)
            else None
        )
        if relay is not None and relay.analyze_command_id == command.command_id:
            self._relay_analysis_cleanup[command.command_id] = (
                LinkTransferControlRequest(
                    kind=TransferControlKind.FAIL,
                    transfer_id=request.transfer_id,
                    session_handle=None,
                    packet_progress=(),
                    correlation=Correlation(transfer_id=request.transfer_id),
                )
            )
        enqueued = self.enqueue(
            LinkResultEvent(command_id=command.command_id, result=result.value),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )
        if enqueued.is_err:
            self.report_error(enqueued.error)
            if (
                command.command_id in self._relay_analysis_cleanup
                and not self.ingress.has_held_causation(command.command_id)
            ):
                self._cleanup_relay_analysis(command.command_id)

    def _execute_phy(self, command, payload) -> None:
        request = payload.request
        if isinstance(request, PhyReceiveWindowPlan):
            self._execute_rx(command, request)
            return
        self._execute_tx(command, request)

    def _execute_tx(self, command, request) -> None:
        """TX dispatcher: 기존 PHY encode 뒤 기존 Radio TX까지 실행한다."""

        encoded = self._endpoint._phy.encode_packet(
            PhyEncodeRequest(
                packet_id=request.packet_id,
                link_id=request.link_id,
                channel=request.channel,
                transmit_at=request.transmit_at,
                packet=request.packet,
            )
        )
        if encoded.is_err:
            self._fail(command, encoded.error)
            return
        burst = encoded.value
        if self._endpoint._phy_radio_bridge is not None:
            converted = self._endpoint._phy_radio_bridge.to_device_burst(burst)
            if converted.is_err:
                self._fail(command, converted.error)
                return
            burst = converted.value
        self._endpoint._record(
            InternalEventType.BURST_ENCODED,
            Layer.PHY,
            payload=burst,
            correlation=Correlation(packet_id=request.packet_id, burst_id=burst.burst_id),
        )
        scheduled = self._endpoint._radio.schedule_tx(
            RadioTxRequest(
                burst_id=burst.burst_id,
                channel=burst.channel,
                transmit_at=burst.transmit_at,
                samples=burst.samples,
            )
        )
        if scheduled.is_err:
            self._fail(command, scheduled.error)
            return
        self._burst_commands[burst.burst_id.value] = (command, request.packet_id)
        self._endpoint._record(
            InternalEventType.BURST_SCHEDULED,
            Layer.RADIO,
            correlation=Correlation(packet_id=request.packet_id, burst_id=burst.burst_id),
        )
        # `PhyTxAcceptedEvent`는 Radio가 송신을 접수한 뒤에만 만든다.
        self.enqueue(
            PhyTxAcceptedEvent(
                command_id=command.command_id,
                packet_id=request.packet_id,
                accepted_at=self._endpoint._time().now_monotonic(),
            ),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def _execute_rx(self, command, request) -> None:
        """RX dispatcher: 기존 `RadioAdapter.request_rx()`로 변환한다.

        wideband bridge 경로에서는 §13.2에 따라 device-rate window intent가 정확히
        `(11,)`, `(17,)`, `(19,)` 중 하나인지와 MAC plan 시간·expected count 정합을
        ledger entry·Radio 요청을 만들기 전에 검증한다. `start_at`과
        `duration_nanoseconds`는 frozen Stage 4 MAC plan 값을 그대로 복사한다.
        """

        sample_rate = self._endpoint._dependencies.configuration.endpoint.phy.sample_rate_hz
        bridge = self._endpoint._phy_radio_bridge
        canonical_allowed = None
        if bridge is not None:
            intent = bridge.validate_window_intent(
                getattr(request, "allowed_link_ids", ())
            )
            if intent.is_err:
                # §13.2 위반은 command 실패와 별도로 health/internal evidence에 남긴다.
                self.report_error(intent.error)
                self._fail(command, intent.error)
                return
            canonical_allowed = intent.value
            timing = bridge.validate_window_timing(
                request.duration_nanoseconds, sample_rate
            )
            if timing.is_err:
                self.report_error(timing.error)
                self._fail(command, timing.error)
                return
        accepted = self._endpoint._radio.request_rx(
            RadioRxRequest(
                channel=request.channel,
                start_at=request.start_at,
                duration_nanoseconds=request.duration_nanoseconds,
                sample_rate_hz=sample_rate,
            )
        )
        if accepted.is_err:
            self._fail(command, accepted.error)
            return
        if bridge is not None:
            admitted = bridge.admit_window(
                request.channel,
                request.start_at,
                request.duration_nanoseconds,
                canonical_allowed,
            )
            if admitted.is_err:
                self.report_error(admitted.error)
        key = (request.channel.name, request.start_at.total_nanoseconds())
        self._rx_commands[key] = command
        self.enqueue(
            PhyRxWindowAcceptedEvent(
                command_id=command.command_id,
                channel=request.channel,
                accepted_at=self._endpoint._time().now_monotonic(),
            ),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def _execute_transfer_state(self, command, payload) -> None:
        """Runtime 소비자가 수락할 때까지 같은 command ID로 최대 3회 실행한다."""

        attempts = self._consumer_attempts.get(command.command_id, 0) + 1
        self._consumer_attempts[command.command_id] = attempts
        error = None if payload.error is None else to_service_error(payload.error)
        published = self._endpoint._set_transfer_state(
            payload.transfer_id, payload.state, error
        )
        if published.is_err:
            if attempts >= MAX_CONSUMER_ATTEMPTS:
                self._consumer_attempts.pop(command.command_id, None)
                self._fail(command, published.error)
                return
            self._retry_consumer_command(command)
            return
        self._consumer_attempts.pop(command.command_id, None)
        self._complete(command)

    def _execute_delivery(self, command, payload) -> None:
        attempts = self._consumer_attempts.get(command.command_id, 0) + 1
        self._consumer_attempts[command.command_id] = attempts
        delivered = self._endpoint._deliver_presentation_bytes(
            payload.route, payload.presentation_data, payload.received_at
        )
        if delivered.is_err:
            if attempts >= MAX_CONSUMER_ATTEMPTS:
                self._consumer_attempts.pop(command.command_id, None)
                self._fail(command, delivered.error)
                return
            self._retry_consumer_command(command)
            return
        self._consumer_attempts.pop(command.command_id, None)
        self._complete(command)

    def _retry_consumer_command(self, command) -> None:
        """같은 command ID를 새 batch로 예약·공개해 bounded consumer retry를 실행한다."""

        batch = MacCommandBatch(
            batch_id=self._endpoint._dependencies.id_source.new_event_id(),
            causation_event_id=command.causation_event_id,
            commands=(command,),
        )
        reservation = self.outbox.submit(batch)
        if reservation.is_err:
            self._consumer_attempts.pop(command.command_id, None)
            self._fail(command, reservation.error)
            return
        self.outbox.publish(reservation.value)

    # --- Radio 결과 결속 ---

    def on_burst_completed(self, event) -> bool:
        burst_id = event.correlation.burst_id
        if burst_id is None:
            return False
        entry = self._burst_commands.pop(burst_id.value, None)
        if entry is None:
            return False
        command, packet_id = entry
        self.enqueue(
            PhyTxCompletedEvent(
                command_id=command.command_id,
                packet_id=packet_id,
                completed_at=self._endpoint._time().now_monotonic(),
            ),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )
        self.pump()
        return True

    def on_rx_window_completed(self, event) -> bool:
        payload = event.payload
        bridge = self._endpoint._phy_radio_bridge
        if bridge is not None:
            # §13.2: 정상 completion에서만 buffered window를 native로 변환해 PHY를
            # 정확히 한 번 호출한다. Packet 전달은 window closed event보다 앞선다.
            prepared = bridge.complete_window(payload)
            if prepared.is_err:
                self.report_error(prepared.error)
            elif prepared.value is not None:
                self._decode_and_deliver(prepared.value)
        key = (payload.channel.name, payload.requested_start_at.total_nanoseconds())
        command = self._rx_commands.pop(key, None)
        if command is None:
            if bridge is not None:
                self.pump()
                return True
            return False
        self.enqueue(
            PhyRxWindowClosedEvent(
                command_id=command.command_id,
                channel=payload.channel,
                closed_at=self._endpoint._time().now_monotonic(),
            ),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )
        self.pump()
        return True

    def _decode_and_deliver(self, decode_request) -> None:
        recovered = self._endpoint._phy.decode_samples(decode_request)
        if recovered.is_err:
            self.report_error(recovered.error)
            return
        self._endpoint._record(
            InternalEventType.PACKETS_RECOVERED, Layer.PHY, payload=recovered.value
        )
        self._endpoint._deliver_recovered(recovered.value, stage4=self)

    def on_layer_error(self, event) -> bool:
        """Radio RX terminal·overflow LAYER_ERROR만 ledger에 반영한다."""

        bridge = self._endpoint._phy_radio_bridge
        if bridge is None or event.layer is not Layer.RADIO or event.error is None:
            return False
        error_code = event.error.code
        from .phy_bridge import RX_TERMINAL_ERROR_CODES

        if (
            error_code != ErrorCode.RADIO_RX_OVERFLOW
            and error_code not in RX_TERMINAL_ERROR_CODES
        ):
            return False
        details = event.details.as_dict() if event.details is not None else {}
        if event.error.details is not None:
            for key, value in event.error.details.as_dict().items():
                details.setdefault(key, value)
        recorded = bridge.record_rx_fault(error_code, details)
        if recorded.is_err:
            self.report_error(recorded.error)
        return True

    def on_samples_received(self, event) -> bool:
        """window의 sample을 기존 PHY decode와 동결 `MacPort` 경로로 보낸다.

        wideband bridge 경로(§13.2)에서는 sample event 도착 즉시 decode하지 않고
        accepted RX-window ledger에 terminal event까지 보관한다. 실제 decode는
        `on_rx_window_completed`의 정상 completion에서 정확히 한 번 수행한다.
        legacy 25 kHz 경로(bridge 없음)는 기존 즉시-decode 동작을 그대로 유지한다.
        """

        received = event.payload
        bridge = self._endpoint._phy_radio_bridge
        if bridge is not None:
            observed = bridge.observe_samples(received)
            if observed.is_err:
                self.report_error(observed.error)
            return True
        endpoint_config = self._endpoint._dependencies.configuration.endpoint
        window_key = (
            received.channel.name,
            received.samples.first_sample_at.total_nanoseconds(),
        )
        window_command = self._rx_commands.get(window_key)
        allowed = endpoint_config.link.supported_link_ids
        if window_command is not None:
            plan_allowed = getattr(
                getattr(window_command.payload, "request", None), "allowed_link_ids", None
            )
            if plan_allowed:
                allowed = plan_allowed
        decode_request = PhyDecodeRequest(
            channel=received.channel,
            samples=received.samples,
            allowed_link_ids=allowed,
        )
        recovered = self._endpoint._phy.decode_samples(decode_request)
        if recovered.is_err:
            self.report_error(recovered.error)
            return True
        self._endpoint._record(
            InternalEventType.PACKETS_RECOVERED, Layer.PHY, payload=recovered.value
        )
        self._endpoint._deliver_recovered(recovered.value, stage4=self)
        self.pump()
        return True


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


__all__ = [
    "PublicErrorCode",
    "ROUTED_EVENT_TYPES",
    "Stage4Support",
    "VdeTerEndpoint",
    "build_endpoint",
]
