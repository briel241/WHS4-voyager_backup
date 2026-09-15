"""Stage 4 MAC 가산 실행 계약 DTO (`stage4_mac_baseline.md` §DR-STAGE4-MAC-012).

이 module은 Stage 0의 동결 `MacPort` DTO를 바꾸지 않는 **가산** 계약이다. `MacPort`의 네
메서드와 반환 DTO는 그대로 유지하고, 여기의 event/command envelope는 clock/timer,
Link·PHY dependency 결과와 실행 command만 표현한다.

MAC에는 Radio sample, `BasebandBurst`, `RadioRxRequest`가 노출되지 않는다.
`InvokePhyCommand`는 Packet bytes, Link ID, Physical Channel과 UTC TX 시각 또는 UTC RX
window만 전달한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..addressing import RouteAddresses
from ..errors import Correlation, InternalProtocolError
from ..exceptions import InvalidValueError
from ..identifiers import LinkId, PacketId, PhysicalChannel, SessionHandle, TransferId
from ..time import ClockDomain, SyncStatus, TimePoint
from .link import (
    BuiltPacket,
    EncodedPacket,
    FragmentProcessResult,
    LinkAnalyzeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    LinkTransferControlResult,
    ReassemblyControlResult,
    TbbAssemblyControlRequest,
    TbbAssemblyControlResult,
    TbbEncodeRequest,
    TbbEncodedPacketSet,
    TransferRequirement,
)
from .mac import TransmissionPlan
from .message import AcknowledgeNackBody
from .service import TransferState


# --- 공통 validation helper ---


def _int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidValueError(f"{name}: {low}..{high} 정수여야 합니다")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidValueError(f"{name}: 비어 있지 않은 문자열이어야 합니다")
    if not value.isascii() or not value.isprintable():
        raise InvalidValueError(f"{name}: 출력 가능한 ASCII여야 합니다")
    return value


def _clock(point: object, name: str, domain: ClockDomain) -> TimePoint:
    if not isinstance(point, TimePoint):
        raise InvalidValueError(f"{name}: TimePoint여야 합니다")
    if point.clock_domain is not domain:
        raise InvalidValueError(f"{name}: {domain.value} domain이어야 합니다")
    return point


def _correlation(value: object, name: str) -> Correlation:
    if not isinstance(value, Correlation):
        raise InvalidValueError(f"{name}: Correlation이어야 합니다")
    return value


# --- 폐쇄형 enum ---


class MacFailureReason(str, Enum):
    """MAC 내부 실패 분류 (`DR-STAGE4-MAC-012`, `015`). 정확히 11개다."""

    EVENT_INVALID = "EVENT_INVALID"
    COMMAND_RESULT_UNMATCHED = "COMMAND_RESULT_UNMATCHED"
    SLOT_MISSED = "SLOT_MISSED"
    TIME_UNAVAILABLE = "TIME_UNAVAILABLE"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    LC_CONFLICT = "LC_CONFLICT"
    ACK_TIMEOUT = "ACK_TIMEOUT"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    SESSION_ABORTED = "SESSION_ABORTED"
    FEATURE_NOT_SUPPORTED = "FEATURE_NOT_SUPPORTED"


class MacReasonCode:
    """`InternalProtocolError.details.reason_code`의 최소 안정 code 집합."""

    TBB_VERSION_SWITCH_NOT_SUPPORTED = "TBB_VERSION_SWITCH_NOT_SUPPORTED"
    TBB_EXPIRED = "TBB_EXPIRED"
    TBB_EXPIRED_DURING_SESSION = "TBB_EXPIRED_DURING_SESSION"
    TBB_FIXTURE_MISMATCH = "TBB_FIXTURE_MISMATCH"
    UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"
    UNSUPPORTED_TERMINAL_CAPABILITY = "UNSUPPORTED_TERMINAL_CAPABILITY"
    UNSUPPORTED_RETRANSMISSION_NUMBER = "UNSUPPORTED_RETRANSMISSION_NUMBER"
    UNEXPECTED_CONTROL_STATION = "UNEXPECTED_CONTROL_STATION"
    TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED = "TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED"
    CONTROL_STATION_OUT_OF_SERVICE = "CONTROL_STATION_OUT_OF_SERVICE"
    MASS_BROADCAST_NOT_SUPPORTED = "MASS_BROADCAST_NOT_SUPPORTED"
    DATA_SESSION_BROADCAST_NOT_SUPPORTED = "DATA_SESSION_BROADCAST_NOT_SUPPORTED"
    RX_SEQUENTIAL_CONFLICT = "RX_SEQUENTIAL_CONFLICT"
    RX_DEDUP_CAPACITY = "RX_DEDUP_CAPACITY"
    ACK_REPLAY_RESOURCE_EXPIRED = "ACK_REPLAY_RESOURCE_EXPIRED"
    RX_LOOKAHEAD_UNSATISFIED = "RX_LOOKAHEAD_UNSATISFIED"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    ENDPOINT_STOPPING = "ENDPOINT_STOPPING"
    STATE_COMMIT_FAILED = "STATE_COMMIT_FAILED"
    OUTBOX_RESERVATION_INVALID = "OUTBOX_RESERVATION_INVALID"

    ALL = (
        TBB_VERSION_SWITCH_NOT_SUPPORTED,
        TBB_EXPIRED,
        TBB_EXPIRED_DURING_SESSION,
        TBB_FIXTURE_MISMATCH,
        UNSUPPORTED_PROFILE,
        UNSUPPORTED_TERMINAL_CAPABILITY,
        UNSUPPORTED_RETRANSMISSION_NUMBER,
        UNEXPECTED_CONTROL_STATION,
        TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED,
        CONTROL_STATION_OUT_OF_SERVICE,
        MASS_BROADCAST_NOT_SUPPORTED,
        DATA_SESSION_BROADCAST_NOT_SUPPORTED,
        RX_SEQUENTIAL_CONFLICT,
        RX_DEDUP_CAPACITY,
        ACK_REPLAY_RESOURCE_EXPIRED,
        RX_LOOKAHEAD_UNSATISFIED,
        RESOURCE_BUSY,
        ENDPOINT_STOPPING,
        STATE_COMMIT_FAILED,
        OUTBOX_RESERVATION_INVALID,
    )


class TimerPurpose(str, Enum):
    FRAME_BOUNDARY = "FRAME_BOUNDARY"
    RESOURCE_RESPONSE_TIMEOUT = "RESOURCE_RESPONSE_TIMEOUT"
    SHORT_DATA_ACK_TIMEOUT = "SHORT_DATA_ACK_TIMEOUT"
    DATA_ACK_TIMEOUT = "DATA_ACK_TIMEOUT"
    LEASE_EXPIRY = "LEASE_EXPIRY"
    TBB_ASSEMBLY_TIMEOUT = "TBB_ASSEMBLY_TIMEOUT"
    TBB_EXPIRY = "TBB_EXPIRY"
    LC_EXPIRY = "LC_EXPIRY"
    RETRY = "RETRY"
    STOP_DRAIN = "STOP_DRAIN"


class MacCompletionReason(str, Enum):
    """terminal `COMPLETED`에만 사용하는 폐쇄형 내부 완료 사유."""

    FINAL_PEER_ACKNOWLEDGED = "FINAL_PEER_ACKNOWLEDGED"
    ACK_NOT_REQUIRED_TX_COMPLETED = "ACK_NOT_REQUIRED_TX_COMPLETED"
    ACCEPTED_BY_ROC_FOR_RELAY = "ACCEPTED_BY_ROC_FOR_RELAY"


class AckProcedure(str, Enum):
    SHORT_DATA_92 = "SHORT_DATA_92"
    DATA_SESSION = "DATA_SESSION"


class AckSlotFunction(str, Enum):
    RAC = "RAC"
    ASC = "ASC"
    DSCH = "DSCH"


class ServiceAreaMode(str, Enum):
    INSIDE_CONFIGURED = "INSIDE_CONFIGURED"


class RxDeliveryProcedure(str, Enum):
    """`RecentRxDeliveryLedger` key의 canonical ASCII procedure 값."""

    SHORT_DATA_92 = "SHORT_DATA_92"
    SHORT_DATA_93 = "SHORT_DATA_93"
    DATA_SESSION = "DATA_SESSION"


# --- PHY 계획 (Radio 객체를 노출하지 않는다) ---


@dataclass(frozen=True, slots=True)
class PhyTransmitPlan:
    packet_id: PacketId
    packet: bytes
    link_id: LinkId
    channel: PhysicalChannel
    transmit_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyTransmitPlan.packet_id: PacketId여야 합니다")
        if not isinstance(self.packet, bytes) or not self.packet:
            raise InvalidValueError("PhyTransmitPlan.packet: 비어 있지 않은 bytes여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("PhyTransmitPlan.link_id: LinkId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyTransmitPlan.channel: PhysicalChannel이어야 합니다")
        _clock(self.transmit_at, "PhyTransmitPlan.transmit_at", ClockDomain.UTC)


@dataclass(frozen=True, slots=True)
class PhyReceiveWindowPlan:
    channel: PhysicalChannel
    start_at: TimePoint
    duration_nanoseconds: int
    allowed_link_ids: tuple[LinkId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyReceiveWindowPlan.channel: PhysicalChannel이어야 합니다")
        _clock(self.start_at, "PhyReceiveWindowPlan.start_at", ClockDomain.UTC)
        _int(self.duration_nanoseconds, "PhyReceiveWindowPlan.duration_nanoseconds", 1, 1 << 62)
        if not isinstance(self.allowed_link_ids, tuple) or not self.allowed_link_ids or any(
            not isinstance(item, LinkId) for item in self.allowed_link_ids
        ):
            raise InvalidValueError(
                "PhyReceiveWindowPlan.allowed_link_ids: 비어 있지 않은 LinkId tuple이어야 합니다"
            )


# --- command payload ---


@dataclass(frozen=True, slots=True)
class BuildPacketFromTransferCommand:
    """`PublishTransmissionPlanCommand`가 담는 Stage 4 build-only 요청."""

    transfer_id: TransferId
    plan: TransmissionPlan
    retransmission_number: int | None = None
    session_frame_index: int | None = None
    frame_fragment_count: int | None = None
    fragment_position_in_frame: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError(
                "BuildPacketFromTransferCommand.transfer_id: TransferId여야 합니다"
            )
        if not isinstance(self.plan, TransmissionPlan):
            raise InvalidValueError(
                "BuildPacketFromTransferCommand.plan: TransmissionPlan이어야 합니다"
            )
        if self.plan.transfer_id != self.transfer_id:
            raise InvalidValueError("BuildPacketFromTransferCommand: plan과 transfer_id가 다릅니다")
        if self.retransmission_number is not None:
            _int(self.retransmission_number, "retransmission_number", 0, 3)
        if self.session_frame_index is not None:
            _int(self.session_frame_index, "session_frame_index", 0, (1 << 32) - 1)
        if self.frame_fragment_count is not None:
            _int(self.frame_fragment_count, "frame_fragment_count", 1, 14)
        if self.fragment_position_in_frame is not None:
            _int(self.fragment_position_in_frame, "fragment_position_in_frame", 1, 14)
        session_fields = (
            self.session_frame_index,
            self.frame_fragment_count,
            self.fragment_position_in_frame,
        )
        if any(value is None for value in session_fields) and any(
            value is not None for value in session_fields
        ):
            raise InvalidValueError(
                "BuildPacketFromTransferCommand: Data Session frame field는 함께 지정해야 합니다"
            )
        if self.frame_fragment_count is not None and self.fragment_position_in_frame is not None:
            if self.fragment_position_in_frame > self.frame_fragment_count:
                raise InvalidValueError(
                    "BuildPacketFromTransferCommand: fragment_position_in_frame이 count를 넘습니다"
                )
        if self.retransmission_number is not None and self.session_frame_index is not None:
            raise InvalidValueError(
                "BuildPacketFromTransferCommand: Short Data와 Data Session field를 함께 쓸 수 없습니다"
            )


@dataclass(frozen=True, slots=True)
class PublishTransmissionPlanCommand:
    request: BuildPacketFromTransferCommand

    def __post_init__(self) -> None:
        if not isinstance(self.request, BuildPacketFromTransferCommand):
            raise InvalidValueError(
                "PublishTransmissionPlanCommand.request: BuildPacketFromTransferCommand여야 합니다"
            )


LINK_COMMAND_REQUEST_TYPES = (
    LinkAnalyzeRequest,
    LinkPacketEncodeRequest,
    TbbEncodeRequest,
    LinkFragmentProcessRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    TbbAssemblyControlRequest,
)

#: 원 Link request 종류 → 허용 성공 result variant (`DR-STAGE4-MAC-012` 표).
LINK_REQUEST_TO_RESULT_TYPE = {
    LinkAnalyzeRequest: TransferRequirement,
    LinkPacketEncodeRequest: EncodedPacket,
    TbbEncodeRequest: TbbEncodedPacketSet,
    LinkFragmentProcessRequest: FragmentProcessResult,
    LinkReassemblyControlRequest: ReassemblyControlResult,
    LinkTransferControlRequest: LinkTransferControlResult,
    TbbAssemblyControlRequest: TbbAssemblyControlResult,
}

LINK_RESULT_PAYLOAD_TYPES = (
    TransferRequirement,
    BuiltPacket,
    EncodedPacket,
    TbbEncodedPacketSet,
    FragmentProcessResult,
    ReassemblyControlResult,
    LinkTransferControlResult,
    TbbAssemblyControlResult,
)


@dataclass(frozen=True, slots=True)
class InvokeLinkCommand:
    request: object

    def __post_init__(self) -> None:
        if not isinstance(self.request, LINK_COMMAND_REQUEST_TYPES):
            raise InvalidValueError("InvokeLinkCommand.request: LinkCommandRequest여야 합니다")


@dataclass(frozen=True, slots=True)
class InvokePhyCommand:
    request: object

    def __post_init__(self) -> None:
        if not isinstance(self.request, (PhyTransmitPlan, PhyReceiveWindowPlan)):
            raise InvalidValueError(
                "InvokePhyCommand.request: PhyTransmitPlan 또는 PhyReceiveWindowPlan이어야 합니다"
            )

    @property
    def is_transmit(self) -> bool:
        return isinstance(self.request, PhyTransmitPlan)


@dataclass(frozen=True, slots=True)
class ScheduleWakeupCommand:
    timer_id: str
    deadline_monotonic: TimePoint
    purpose: TimerPurpose
    replaces_timer_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.timer_id, "ScheduleWakeupCommand.timer_id")
        _clock(
            self.deadline_monotonic,
            "ScheduleWakeupCommand.deadline_monotonic",
            ClockDomain.MONOTONIC,
        )
        if not isinstance(self.purpose, TimerPurpose):
            raise InvalidValueError("ScheduleWakeupCommand.purpose: TimerPurpose여야 합니다")
        if self.replaces_timer_id is not None:
            _text(
                self.replaces_timer_id,
                "ScheduleWakeupCommand.replaces_timer_id",
            )
            if self.replaces_timer_id == self.timer_id:
                raise InvalidValueError(
                    "ScheduleWakeupCommand.replaces_timer_id는 새 timer_id와 달라야 합니다"
                )


@dataclass(frozen=True, slots=True)
class CancelWakeupCommand:
    timer_id: str

    def __post_init__(self) -> None:
        _text(self.timer_id, "CancelWakeupCommand.timer_id")


@dataclass(frozen=True, slots=True)
class PublishTransferStateCommand:
    transfer_id: TransferId
    state: TransferState
    error: InternalProtocolError | None = None
    completion_reason: MacCompletionReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("PublishTransferStateCommand.transfer_id: TransferId여야 합니다")
        if not isinstance(self.state, TransferState):
            raise InvalidValueError("PublishTransferStateCommand.state: TransferState여야 합니다")
        if self.error is not None and not isinstance(self.error, InternalProtocolError):
            raise InvalidValueError(
                "PublishTransferStateCommand.error: InternalProtocolError여야 합니다"
            )
        if self.completion_reason is not None and not isinstance(
            self.completion_reason, MacCompletionReason
        ):
            raise InvalidValueError(
                "PublishTransferStateCommand.completion_reason: MacCompletionReason이어야 합니다"
            )
        if self.state is TransferState.COMPLETED and self.completion_reason is None:
            raise InvalidValueError("COMPLETED에는 completion_reason이 필요합니다")
        if self.state is not TransferState.COMPLETED and self.completion_reason is not None:
            raise InvalidValueError("completion_reason은 COMPLETED에만 사용합니다")
        if self.state is TransferState.FAILED and self.error is None:
            raise InvalidValueError("FAILED에는 error가 필요합니다")


@dataclass(frozen=True, slots=True)
class DeliverPresentationDataCommand:
    """command로 시작한 비동기 Data Session reassembly 완료 전달에만 사용한다."""

    session_handle: SessionHandle
    route: RouteAddresses
    presentation_data: bytes
    received_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("DeliverPresentationDataCommand.session_handle이 필요합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("DeliverPresentationDataCommand.route: RouteAddresses여야 합니다")
        if not isinstance(self.presentation_data, bytes) or not self.presentation_data:
            raise InvalidValueError(
                "DeliverPresentationDataCommand.presentation_data가 비어 있습니다"
            )
        _clock(self.received_at, "DeliverPresentationDataCommand.received_at", ClockDomain.UTC)


MAC_COMMAND_PAYLOAD_TYPES = (
    PublishTransmissionPlanCommand,
    InvokeLinkCommand,
    InvokePhyCommand,
    ScheduleWakeupCommand,
    CancelWakeupCommand,
    PublishTransferStateCommand,
    DeliverPresentationDataCommand,
)

#: `DRAINING`에서도 수락하는 cleanup allowlist command payload 종류.
CLEANUP_COMMAND_PAYLOAD_TYPES = (
    CancelWakeupCommand,
    InvokeLinkCommand,
    PublishTransferStateCommand,
)


@dataclass(frozen=True, slots=True)
class MacCommandEnvelope:
    command_id: str
    causation_event_id: str
    correlation: Correlation
    payload: object

    def __post_init__(self) -> None:
        _text(self.command_id, "MacCommandEnvelope.command_id")
        _text(self.causation_event_id, "MacCommandEnvelope.causation_event_id")
        _correlation(self.correlation, "MacCommandEnvelope.correlation")
        if not isinstance(self.payload, MAC_COMMAND_PAYLOAD_TYPES):
            raise InvalidValueError("MacCommandEnvelope.payload: MacCommandPayload여야 합니다")


MAC_COMMAND_BATCH_MAX = 64


@dataclass(frozen=True, slots=True)
class MacCommandBatch:
    batch_id: str
    causation_event_id: str
    commands: tuple[MacCommandEnvelope, ...]

    def __post_init__(self) -> None:
        _text(self.batch_id, "MacCommandBatch.batch_id")
        _text(self.causation_event_id, "MacCommandBatch.causation_event_id")
        if not isinstance(self.commands, tuple) or not self.commands or any(
            not isinstance(item, MacCommandEnvelope) for item in self.commands
        ):
            raise InvalidValueError(
                "MacCommandBatch.commands: 비어 있지 않은 envelope tuple이어야 합니다"
            )
        if len(self.commands) > MAC_COMMAND_BATCH_MAX:
            raise InvalidValueError(
                f"MacCommandBatch.commands: 최대 {MAC_COMMAND_BATCH_MAX}개입니다"
            )
        identifiers = tuple(item.command_id for item in self.commands)
        if len(set(identifiers)) != len(identifiers):
            raise InvalidValueError("MacCommandBatch: command_id가 중복되었습니다")
        if any(item.causation_event_id != self.causation_event_id for item in self.commands):
            raise InvalidValueError(
                "MacCommandBatch: 모든 command의 causation_event_id가 같아야 합니다"
            )

    def command_ids(self) -> tuple[str, ...]:
        return tuple(item.command_id for item in self.commands)


@dataclass(frozen=True, slots=True)
class MacOutboxReservation:
    """sink가 생성하는 불변 내부 token. 실행 권한을 부여하지 않는다."""

    reservation_id: str
    batch_id: str
    command_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.reservation_id, "MacOutboxReservation.reservation_id")
        _text(self.batch_id, "MacOutboxReservation.batch_id")
        if not isinstance(self.command_ids, tuple) or not self.command_ids or any(
            not isinstance(item, str) or not item for item in self.command_ids
        ):
            raise InvalidValueError("MacOutboxReservation.command_ids가 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class MacBatchReceipt:
    """commit이 끝난 결과를 알리는 식별 receipt. command payload를 담지 않는다."""

    batch_id: str | None = None
    command_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.batch_id is not None:
            _text(self.batch_id, "MacBatchReceipt.batch_id")
        if not isinstance(self.command_ids, tuple) or any(
            not isinstance(item, str) or not item for item in self.command_ids
        ):
            raise InvalidValueError("MacBatchReceipt.command_ids가 잘못되었습니다")
        if (self.batch_id is None) != (not self.command_ids):
            raise InvalidValueError(
                "MacBatchReceipt: command가 없으면 (None, ()), 있으면 batch_id와 nonempty tuple입니다"
            )


EMPTY_MAC_BATCH_RECEIPT = MacBatchReceipt()


# --- event payload ---


@dataclass(frozen=True, slots=True)
class ClockAdvancedEvent:
    sync_status: SyncStatus

    def __post_init__(self) -> None:
        if not isinstance(self.sync_status, SyncStatus):
            raise InvalidValueError("ClockAdvancedEvent.sync_status: SyncStatus여야 합니다")


@dataclass(frozen=True, slots=True)
class WakeupFiredEvent:
    timer_id: str
    purpose: TimerPurpose
    scheduled_for_monotonic: TimePoint

    def __post_init__(self) -> None:
        _text(self.timer_id, "WakeupFiredEvent.timer_id")
        if not isinstance(self.purpose, TimerPurpose):
            raise InvalidValueError("WakeupFiredEvent.purpose: TimerPurpose여야 합니다")
        _clock(
            self.scheduled_for_monotonic,
            "WakeupFiredEvent.scheduled_for_monotonic",
            ClockDomain.MONOTONIC,
        )


@dataclass(frozen=True, slots=True)
class LinkResultEvent:
    command_id: str
    result: object

    def __post_init__(self) -> None:
        _text(self.command_id, "LinkResultEvent.command_id")
        if not isinstance(self.result, LINK_RESULT_PAYLOAD_TYPES):
            raise InvalidValueError("LinkResultEvent.result: LinkResultPayload여야 합니다")


@dataclass(frozen=True, slots=True)
class PhyTxAcceptedEvent:
    command_id: str
    packet_id: PacketId
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        _text(self.command_id, "PhyTxAcceptedEvent.command_id")
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyTxAcceptedEvent.packet_id: PacketId여야 합니다")
        _clock(self.accepted_at, "PhyTxAcceptedEvent.accepted_at", ClockDomain.MONOTONIC)


@dataclass(frozen=True, slots=True)
class PhyTxCompletedEvent:
    command_id: str
    packet_id: PacketId
    completed_at: TimePoint

    def __post_init__(self) -> None:
        _text(self.command_id, "PhyTxCompletedEvent.command_id")
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyTxCompletedEvent.packet_id: PacketId여야 합니다")
        _clock(self.completed_at, "PhyTxCompletedEvent.completed_at", ClockDomain.MONOTONIC)


@dataclass(frozen=True, slots=True)
class PhyTxFailedEvent:
    command_id: str
    packet_id: PacketId
    error: InternalProtocolError

    def __post_init__(self) -> None:
        _text(self.command_id, "PhyTxFailedEvent.command_id")
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyTxFailedEvent.packet_id: PacketId여야 합니다")
        if not isinstance(self.error, InternalProtocolError):
            raise InvalidValueError("PhyTxFailedEvent.error: InternalProtocolError여야 합니다")


@dataclass(frozen=True, slots=True)
class PhyRxWindowAcceptedEvent:
    command_id: str
    channel: PhysicalChannel
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        _text(self.command_id, "PhyRxWindowAcceptedEvent.command_id")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyRxWindowAcceptedEvent.channel: PhysicalChannel이어야 합니다")
        _clock(self.accepted_at, "PhyRxWindowAcceptedEvent.accepted_at", ClockDomain.MONOTONIC)


@dataclass(frozen=True, slots=True)
class PhyRxWindowClosedEvent:
    command_id: str
    channel: PhysicalChannel
    closed_at: TimePoint

    def __post_init__(self) -> None:
        _text(self.command_id, "PhyRxWindowClosedEvent.command_id")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyRxWindowClosedEvent.channel: PhysicalChannel이어야 합니다")
        _clock(self.closed_at, "PhyRxWindowClosedEvent.closed_at", ClockDomain.MONOTONIC)


@dataclass(frozen=True, slots=True)
class CommandCompletedEvent:
    command_id: str

    def __post_init__(self) -> None:
        _text(self.command_id, "CommandCompletedEvent.command_id")


@dataclass(frozen=True, slots=True)
class DependencyFailedEvent:
    command_id: str
    error: InternalProtocolError

    def __post_init__(self) -> None:
        _text(self.command_id, "DependencyFailedEvent.command_id")
        if not isinstance(self.error, InternalProtocolError):
            raise InvalidValueError("DependencyFailedEvent.error: InternalProtocolError여야 합니다")


@dataclass(frozen=True, slots=True)
class PresentationDeliveryResultEvent:
    """동결 `MacActionSet.DELIVER_PRESENTATION_DATA` 결과. command 결과가 아니다."""

    packet_id: PacketId
    delivery_index: int
    delivered_at_monotonic: TimePoint
    error: InternalProtocolError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PresentationDeliveryResultEvent.packet_id: PacketId여야 합니다")
        _int(
            self.delivery_index,
            "PresentationDeliveryResultEvent.delivery_index",
            0,
            (1 << 31) - 1,
        )
        _clock(
            self.delivered_at_monotonic,
            "PresentationDeliveryResultEvent.delivered_at_monotonic",
            ClockDomain.MONOTONIC,
        )
        if self.error is not None and not isinstance(self.error, InternalProtocolError):
            raise InvalidValueError(
                "PresentationDeliveryResultEvent.error: InternalProtocolError여야 합니다"
            )


@dataclass(frozen=True, slots=True)
class CancelTransferEvent:
    transfer_id: TransferId

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("CancelTransferEvent.transfer_id: TransferId여야 합니다")


@dataclass(frozen=True, slots=True)
class EndpointStoppingEvent:
    reason_code: str

    def __post_init__(self) -> None:
        _text(self.reason_code, "EndpointStoppingEvent.reason_code")


MAC_EVENT_PAYLOAD_TYPES = (
    ClockAdvancedEvent,
    WakeupFiredEvent,
    LinkResultEvent,
    PhyTxAcceptedEvent,
    PhyTxCompletedEvent,
    PhyTxFailedEvent,
    PhyRxWindowAcceptedEvent,
    PhyRxWindowClosedEvent,
    CommandCompletedEvent,
    DependencyFailedEvent,
    PresentationDeliveryResultEvent,
    CancelTransferEvent,
    EndpointStoppingEvent,
)

#: command 결과 event payload. envelope의 `causation_command_id`가 반드시 있어야 한다.
COMMAND_RESULT_PAYLOAD_TYPES = (
    LinkResultEvent,
    PhyTxAcceptedEvent,
    PhyTxCompletedEvent,
    PhyTxFailedEvent,
    PhyRxWindowAcceptedEvent,
    PhyRxWindowClosedEvent,
    CommandCompletedEvent,
    DependencyFailedEvent,
)


@dataclass(frozen=True, slots=True)
class MacEventEnvelope:
    event_id: str
    occurred_at_utc: TimePoint
    observed_at_monotonic: TimePoint
    ingress_sequence: int
    correlation: Correlation
    payload: object
    causation_command_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.event_id, "MacEventEnvelope.event_id")
        _clock(self.occurred_at_utc, "MacEventEnvelope.occurred_at_utc", ClockDomain.UTC)
        _clock(
            self.observed_at_monotonic,
            "MacEventEnvelope.observed_at_monotonic",
            ClockDomain.MONOTONIC,
        )
        _int(self.ingress_sequence, "MacEventEnvelope.ingress_sequence", 0, 1 << 62)
        _correlation(self.correlation, "MacEventEnvelope.correlation")
        if not isinstance(self.payload, MAC_EVENT_PAYLOAD_TYPES):
            raise InvalidValueError("MacEventEnvelope.payload: MacEventPayload여야 합니다")
        if self.causation_command_id is not None:
            _text(self.causation_command_id, "MacEventEnvelope.causation_command_id")
        if isinstance(self.payload, COMMAND_RESULT_PAYLOAD_TYPES):
            if self.causation_command_id is None:
                raise InvalidValueError(
                    "MacEventEnvelope: command 결과 event에는 causation_command_id가 필요합니다"
                )
            if self.causation_command_id != self.payload.command_id:
                raise InvalidValueError(
                    "MacEventEnvelope: causation_command_id와 payload.command_id가 다릅니다"
                )
        elif self.causation_command_id is not None:
            raise InvalidValueError(
                "MacEventEnvelope: clock·wakeup·외부 event에는 causation_command_id가 없어야 합니다"
            )

    def order_key(self) -> tuple:
        """`(observed_at_monotonic, ingress_sequence, event_id UTF-8)` 총순서 key."""

        return (
            self.observed_at_monotonic.total_nanoseconds(),
            self.ingress_sequence,
            self.event_id.encode("utf-8"),
        )


# --- ACK replay context ---


@dataclass(frozen=True, slots=True)
class AckReplayContext:
    """LIVE delivery record가 보존하는 재-ACK 계획 (`DR-STAGE4-MAC-010`)."""

    ack_procedure: AckProcedure
    route: RouteAddresses
    link_id: LinkId
    physical_channel: PhysicalChannel
    slot_function: AckSlotFunction
    logical_channel: int | None
    ack_body: AcknowledgeNackBody
    resource_last_frame_ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.ack_procedure, AckProcedure):
            raise InvalidValueError("AckReplayContext.ack_procedure: AckProcedure여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("AckReplayContext.route: RouteAddresses여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("AckReplayContext.link_id: LinkId여야 합니다")
        if not isinstance(self.physical_channel, PhysicalChannel):
            raise InvalidValueError("AckReplayContext.physical_channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.slot_function, AckSlotFunction):
            raise InvalidValueError("AckReplayContext.slot_function: AckSlotFunction이어야 합니다")
        if self.logical_channel is not None:
            _int(self.logical_channel, "AckReplayContext.logical_channel", 0, 255)
        if not isinstance(self.ack_body, AcknowledgeNackBody):
            raise InvalidValueError("AckReplayContext.ack_body: AcknowledgeNackBody여야 합니다")
        _int(
            self.resource_last_frame_ordinal,
            "AckReplayContext.resource_last_frame_ordinal",
            0,
            1 << 62,
        )
        if self.ack_procedure is AckProcedure.DATA_SESSION:
            if self.slot_function is not AckSlotFunction.DSCH or self.logical_channel is None:
                raise InvalidValueError(
                    "AckReplayContext: Data Session ACK는 고정 DSCH Logical Channel을 사용합니다"
                )
        elif self.logical_channel is not None:
            raise InvalidValueError(
                "AckReplayContext: Short Data ACK는 replay 시점에 slot을 고르므로 LC가 없습니다"
            )


#: Stage 4 실행 계약이 정의하는 모든 가산 DTO. TC-MAC-001이 불변성을 검사한다.
STAGE4_EXECUTION_DTO_TYPES = (
    PhyTransmitPlan,
    PhyReceiveWindowPlan,
    BuildPacketFromTransferCommand,
    PublishTransmissionPlanCommand,
    InvokeLinkCommand,
    InvokePhyCommand,
    ScheduleWakeupCommand,
    CancelWakeupCommand,
    PublishTransferStateCommand,
    DeliverPresentationDataCommand,
    MacCommandEnvelope,
    MacCommandBatch,
    MacOutboxReservation,
    MacBatchReceipt,
    ClockAdvancedEvent,
    WakeupFiredEvent,
    LinkResultEvent,
    PhyTxAcceptedEvent,
    PhyTxCompletedEvent,
    PhyTxFailedEvent,
    PhyRxWindowAcceptedEvent,
    PhyRxWindowClosedEvent,
    CommandCompletedEvent,
    DependencyFailedEvent,
    PresentationDeliveryResultEvent,
    CancelTransferEvent,
    EndpointStoppingEvent,
    MacEventEnvelope,
    AckReplayContext,
)
