"""단계 0 MAC Stub (`stage0_baseline.md` §5.5).

이 구현은 interface 연결을 시험하기 위한 **test scope** stub이며 VDE-TER MAC 절차가
아니다. 다음을 수행하지 않으며 수행했다고 주장하지 않는다.

- UTC 동기, Hexslot/TDMA frame/slot 계산, RX window와 guard interval
- TBB, RAC, Resource Request/Allocation, Logical Channel 할당
- Data Session 상태 전이, ACK/NACK, timeout과 재전송

stub은 설정으로 주입된 Link ID·채널·lead time으로 최소 `TransmissionPlan`을 만들고,
수신 Packet과 Message를 그대로 다음 계층으로 넘기는 action만 만든다. 실제 구현은 단계 4
MAC 기준선에서 이 Port 계약 안에 작성한다.
"""

from __future__ import annotations

from ..common.addressing import RouteAddresses
from ..common.contracts.link import TransferRequirement
from ..common.contracts.mac import (
    MacAction,
    MacActionKind,
    MacActionSet,
    MacMessageInput,
    PacketDecodeAction,
    RequirementAccepted,
    SessionControlKind,
    SessionControlRequest,
    SessionControlResult,
    TransmissionPlan,
    TransmissionProcedure,
)
from ..common.contracts.phy import PhyRecoveryStatus, RecoveredPacketSet
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.events import (
    EventSeverity,
    InternalEvent,
    InternalEventType,
)
from ..common.identifiers import IdSource
from ..common.results import Ok, internal_error
from ..common.time import TimeSource
from .models import MacPlanningDefaults, SessionRecord


class StubMac:
    """test scope MAC stub. 계획과 수신 판정을 최소한으로만 표현한다."""

    __slots__ = ("_time_source", "_id_source", "_defaults", "_event_sink", "_rx_route", "_sessions")

    def __init__(
        self,
        time_source: TimeSource,
        id_source: IdSource,
        defaults: MacPlanningDefaults,
        event_sink,
        rx_route: RouteAddresses,
    ) -> None:
        self._time_source = time_source
        self._id_source = id_source
        self._defaults = defaults
        self._event_sink = event_sink
        # 단계 0 stub은 수신 Message에서 주소를 복원하지 않는다. 시험 wiring이 주입한
        # route를 그대로 사용하며 이는 protocol 동작이 아니다.
        self._rx_route = rx_route
        self._sessions = {}

    # --- 송신 ---

    def submit_requirement(self, requirement: TransferRequirement):
        if not isinstance(requirement, TransferRequirement):
            return internal_error(
                Layer.MAC,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "TransferRequirement가 필요합니다",
            )
        correlation = Correlation(transfer_id=requirement.transfer_id)
        now = self._time_source.now_utc()
        plan = TransmissionPlan(
            transfer_id=requirement.transfer_id,
            link_id=self._defaults.link_id,
            channel=self._defaults.tx_channel,
            transmit_at=now.shifted(self._defaults.transmit_lead_nanoseconds),
            procedure=TransmissionProcedure.SHORT_DATA,
            route=requirement.route,
            session_handle=None,
        )
        published = self._event_sink.publish(
            InternalEvent(
                event_id=self._id_source.new_event_id(),
                event_type=InternalEventType.TRANSMISSION_PLANNED,
                occurred_at=now,
                severity=EventSeverity.INFO,
                layer=Layer.MAC,
                correlation=correlation,
                payload=plan,
                details=Details.of(link_id=plan.link_id.value, channel=plan.channel.name),
            )
        )
        if published.is_err:
            return published
        return Ok(RequirementAccepted(transfer_id=requirement.transfer_id, accepted_at=now))

    # --- 수신 ---

    def handle_recovered_packet(self, recovered: RecoveredPacketSet):
        if not isinstance(recovered, RecoveredPacketSet):
            return internal_error(
                Layer.MAC,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RecoveredPacketSet이 필요합니다",
            )
        if recovered.status is PhyRecoveryStatus.NO_BURST_DETECTED or not recovered.packets:
            return internal_error(
                Layer.MAC,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.PROTOCOL,
                "복원된 Packet이 없습니다",
                details=Details.of(channel=recovered.channel.name, status=recovered.status.value),
            )
        packet = recovered.packets[0]
        return Ok(PacketDecodeAction(packet=packet, accepted=True, reason_code=None))

    def handle_message(self, message_input: MacMessageInput):
        if not isinstance(message_input, MacMessageInput):
            return internal_error(
                Layer.MAC,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "MacMessageInput이 필요합니다",
            )
        decoded = message_input.decoded
        actions = []
        for message in decoded.messages:
            if message.presentation_data is None:
                actions.append(
                    MacAction(
                        kind=MacActionKind.NO_ACTION,
                        packet_id=decoded.packet_id,
                        details=Details.of(message_type=message.message_type),
                    )
                )
                continue
            actions.append(
                MacAction(
                    kind=MacActionKind.DELIVER_PRESENTATION_DATA,
                    packet_id=decoded.packet_id,
                    session_handle=None,
                    presentation_data=message.presentation_data,
                    route=message.route or self._rx_route,
                    details=Details.of(message_type=message.message_type),
                )
            )
        return Ok(MacActionSet(actions=tuple(actions)))

    # --- 로컬 Session 수명 ---

    def control_session(self, request: SessionControlRequest):
        if not isinstance(request, SessionControlRequest):
            return internal_error(
                Layer.MAC,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "SessionControlRequest가 필요합니다",
            )
        now = self._time_source.now_utc()
        if request.kind is SessionControlKind.OPEN:
            handle = request.session_handle or self._id_source.new_session_handle()
            if handle.value in self._sessions:
                return internal_error(
                    Layer.MAC,
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "이미 열린 session_handle입니다",
                    correlation=Correlation(session_handle=handle),
                )
            self._sessions[handle.value] = SessionRecord(
                session_handle=handle, transfer_id=request.transfer_id, opened_at=now
            )
            return Ok(
                SessionControlResult(kind=request.kind, session_handle=handle, active=True)
            )

        handle = request.session_handle
        record = self._sessions.get(handle.value)
        if record is None:
            return internal_error(
                Layer.MAC,
                ErrorCode.UNKNOWN_TRANSFER,
                ErrorCategory.RESOURCE,
                "알 수 없는 session_handle입니다",
                correlation=Correlation(session_handle=handle),
            )
        if request.kind is SessionControlKind.KEEP:
            return Ok(
                SessionControlResult(kind=request.kind, session_handle=handle, active=record.active)
            )
        self._sessions[handle.value] = record.closed()
        return Ok(SessionControlResult(kind=request.kind, session_handle=handle, active=False))

    # --- 시험 관찰용 ---

    def active_session_count(self) -> int:
        return sum(1 for record in self._sessions.values() if record.active)

    def known_session_count(self) -> int:
        return len(self._sessions)
