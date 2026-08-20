"""Stage 0 경계를 보존해 확장한 Stage 1 Link 공개 DTO."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..addressing import RouteAddresses
from ..errors import Correlation
from ..exceptions import InvalidValueError
from ..identifiers import LinkId, PacketId, SequentialId, SessionHandle, StationId, TransferId
from ..time import TimePoint
from .mac import TransmissionPlan
from .message import (
    BulletinBoardBody,
    DecodedMessage,
    FragmentRef,
    MessageEncodeRequest,
    TbbCandidateKey,
)


def _int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidValueError(f"{name}: {low}..{high} 정수여야 합니다")
    return value


def _tuple(value: object, name: str, item_type: type, *, nonempty: bool = False) -> tuple:
    if not isinstance(value, tuple) or (nonempty and not value) or any(
        not isinstance(item, item_type) for item in value
    ):
        qualifier = "비어 있지 않은 " if nonempty else ""
        raise InvalidValueError(f"{name}: {qualifier}{item_type.__name__} tuple이어야 합니다")
    return value


class TransferKind(str, Enum):
    SHORT_DATA = "SHORT_DATA"
    DATA_SESSION = "DATA_SESSION"


@dataclass(frozen=True, slots=True)
class LinkAnalyzeRequest:
    transfer_id: TransferId
    route: RouteAddresses
    ack_required: bool
    presentation_data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("transfer_id: TransferId여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if not isinstance(self.ack_required, bool):
            raise InvalidValueError("ack_required: bool이어야 합니다")
        if not isinstance(self.presentation_data, bytes):
            raise InvalidValueError("presentation_data: bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class LinkTransferProfileRequirement:
    link_id: LinkId
    short_data_eligible: bool
    data_session_fragment_count: int
    minimum_tdma_frame_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        if not isinstance(self.short_data_eligible, bool):
            raise InvalidValueError("short_data_eligible: bool이어야 합니다")
        _int(self.data_session_fragment_count, "data_session_fragment_count", 1, 65539)
        _int(self.minimum_tdma_frame_count, "minimum_tdma_frame_count", 1, 65539)


@dataclass(frozen=True, slots=True)
class TransferRequirement:
    transfer_id: TransferId
    route: RouteAddresses
    ack_required: bool
    presentation_size: int
    candidate: TransferKind
    sequential_id: SequentialId | None = None
    profiles: tuple[LinkTransferProfileRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("transfer_id: TransferId여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if not isinstance(self.ack_required, bool):
            raise InvalidValueError("ack_required: bool이어야 합니다")
        _int(self.presentation_size, "presentation_size", 1, 65535)
        if not isinstance(self.candidate, TransferKind):
            raise InvalidValueError("candidate: TransferKind여야 합니다")
        if self.sequential_id is not None and not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        _tuple(self.profiles, "profiles", LinkTransferProfileRequirement)


@dataclass(frozen=True, slots=True)
class LinkBuildRequest:
    plan: TransmissionPlan
    presentation_data: bytes
    sequential_id: SequentialId | None = None
    retransmission_number: int | None = None
    session_frame_index: int | None = None
    frame_fragment_count: int | None = None
    fragment_position_in_frame: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TransmissionPlan):
            raise InvalidValueError("plan: TransmissionPlan이어야 합니다")
        if not isinstance(self.presentation_data, bytes) or not 1 <= len(self.presentation_data) <= 65535:
            raise InvalidValueError("presentation_data: 1..65535 bytes여야 합니다")
        if self.sequential_id is not None and not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        if self.retransmission_number is not None:
            _int(self.retransmission_number, "retransmission_number", 0, 254)
        if self.session_frame_index is not None:
            _int(self.session_frame_index, "session_frame_index", 0, (1 << 32) - 1)
        if self.frame_fragment_count is not None:
            _int(self.frame_fragment_count, "frame_fragment_count", 1, 14)
        if self.fragment_position_in_frame is not None:
            _int(self.fragment_position_in_frame, "fragment_position_in_frame", 1, 14)
        if self.frame_fragment_count is not None and self.fragment_position_in_frame is not None:
            if self.fragment_position_in_frame > self.frame_fragment_count:
                raise InvalidValueError("fragment_position_in_frame이 frame count를 넘습니다")


@dataclass(frozen=True, slots=True)
class BuiltPacket:
    packet_id: PacketId
    transfer_id: TransferId
    link_id: LinkId
    packet: bytes
    session_handle: SessionHandle | None = None
    sequential_id: SequentialId | None = None
    fragment: FragmentRef | None = None
    segment_offset: int | None = None
    segment_length: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("packet_id: PacketId여야 합니다")
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("transfer_id: TransferId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("packet: bytes여야 합니다")
        if self.session_handle is not None and not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("session_handle: SessionHandle이어야 합니다")
        if self.sequential_id is not None and not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        if self.fragment is not None and not isinstance(self.fragment, FragmentRef):
            raise InvalidValueError("fragment: FragmentRef여야 합니다")
        if self.segment_offset is not None:
            _int(self.segment_offset, "segment_offset", 0, 65538)
        if self.segment_length is not None:
            _int(self.segment_length, "segment_length", 1, 65539)
        if (self.segment_offset is None) != (self.segment_length is None):
            raise InvalidValueError("segment_offset과 segment_length는 함께 지정해야 합니다")


@dataclass(frozen=True, slots=True)
class LinkDecodeRequest:
    packet_id: PacketId
    link_id: LinkId
    received_at: TimePoint
    packet: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("received_at: TimePoint여야 합니다")
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("packet: bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class LinkPacketEncodeRequest:
    link_id: LinkId
    messages: tuple[MessageEncodeRequest, ...]
    correlation: Correlation

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        _tuple(self.messages, "messages", MessageEncodeRequest, nonempty=True)
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class EncodedPacket:
    packet_id: PacketId
    link_id: LinkId
    packet: bytes
    message_types: tuple[int, ...]
    padding_size: int
    crc: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("packet: bytes여야 합니다")
        if not isinstance(self.message_types, tuple) or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255
            for item in self.message_types
        ):
            raise InvalidValueError("message_types: uint8 tuple이어야 합니다")
        _int(self.padding_size, "padding_size", 0, 698)
        if not isinstance(self.crc, bytes) or len(self.crc) != 4:
            raise InvalidValueError("crc: 4 bytes여야 합니다")
        if len(self.packet) < 4 or self.packet[-4:] != self.crc:
            raise InvalidValueError("crc는 packet의 마지막 4 bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class TbbEncodeRequest:
    source_id: StationId
    control_station_id: int
    service_capability: int
    bulletin_board_version: int
    bulletin_board: BulletinBoardBody
    correlation: Correlation

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, StationId) or self.source_id.value == 0:
            raise InvalidValueError("source_id: 0이 아닌 StationId여야 합니다")
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.service_capability, "service_capability", 0, 2)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)
        if not isinstance(self.bulletin_board, BulletinBoardBody):
            raise InvalidValueError("bulletin_board: BulletinBoardBody여야 합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class TbbEncodedPacketSet:
    packets: tuple[EncodedPacket, ...]
    certificate_fingerprint: bytes
    authentication_sequence: bytes

    def __post_init__(self) -> None:
        _tuple(self.packets, "packets", EncodedPacket, nonempty=True)
        if not 1 <= len(self.packets) <= 6:
            raise InvalidValueError("packets: 1..6개여야 합니다")
        if not isinstance(self.certificate_fingerprint, bytes) or len(self.certificate_fingerprint) != 6:
            raise InvalidValueError("certificate_fingerprint: 6 bytes여야 합니다")
        if not isinstance(self.authentication_sequence, bytes) or len(self.authentication_sequence) != 64:
            raise InvalidValueError("authentication_sequence: 64 bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class DataFragmentRef:
    session_frame_index: int
    position_in_frame: int
    count_in_frame: int
    global_fragment_number: int

    def __post_init__(self) -> None:
        _int(self.session_frame_index, "session_frame_index", 0, (1 << 32) - 1)
        _int(self.position_in_frame, "position_in_frame", 1, 14)
        _int(self.count_in_frame, "count_in_frame", 1, 14)
        if self.position_in_frame > self.count_in_frame:
            raise InvalidValueError("position_in_frame이 count_in_frame을 넘습니다")
        _int(self.global_fragment_number, "global_fragment_number", 0, 255)


@dataclass(frozen=True, slots=True)
class LinkFragmentProcessRequest:
    packet_id: PacketId
    link_id: LinkId
    received_at: TimePoint
    session_handle: SessionHandle
    session_frame_index: int
    route: RouteAddresses
    message: DecodedMessage

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId) or not isinstance(self.link_id, LinkId):
            raise InvalidValueError("packet_id/link_id 형식이 잘못되었습니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("received_at: TimePoint여야 합니다")
        if not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("session_handle: SessionHandle이어야 합니다")
        _int(self.session_frame_index, "session_frame_index", 0, (1 << 32) - 1)
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if not isinstance(self.message, DecodedMessage) or self.message.message_type not in (74, 75, 76):
            raise InvalidValueError("message: Type 74/75/76 DecodedMessage여야 합니다")


class FragmentProcessStatus(str, Enum):
    INCOMPLETE = "INCOMPLETE"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class FragmentProcessResult:
    status: FragmentProcessStatus
    session_handle: SessionHandle
    fragment: DataFragmentRef
    route: RouteAddresses
    sequential_id: SequentialId | None
    presentation_data: bytes | None
    packet_ids: tuple[PacketId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, FragmentProcessStatus):
            raise InvalidValueError("status: FragmentProcessStatus여야 합니다")
        if not isinstance(self.session_handle, SessionHandle) or not isinstance(self.fragment, DataFragmentRef):
            raise InvalidValueError("session_handle/fragment 형식이 잘못되었습니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if self.sequential_id is not None and not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        if self.presentation_data is not None and not isinstance(self.presentation_data, bytes):
            raise InvalidValueError("presentation_data: bytes여야 합니다")
        _tuple(self.packet_ids, "packet_ids", PacketId, nonempty=True)
        complete = self.status is FragmentProcessStatus.COMPLETE
        if complete != (self.sequential_id is not None and self.presentation_data is not None):
            raise InvalidValueError("COMPLETE에서만 sequential_id와 presentation_data가 필요합니다")


class ReassemblyControlKind(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    ABORT = "ABORT"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class LinkReassemblyControlRequest:
    kind: ReassemblyControlKind
    session_handle: SessionHandle
    route: RouteAddresses | None
    correlation: Correlation

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReassemblyControlKind) or not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("kind/session_handle 형식이 잘못되었습니다")
        if self.route is not None and not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if self.kind is ReassemblyControlKind.OPEN and self.route is None:
            raise InvalidValueError("OPEN에는 route가 필요합니다")
        if self.kind in (ReassemblyControlKind.ABORT, ReassemblyControlKind.TIMEOUT) and self.route is not None:
            raise InvalidValueError("ABORT/TIMEOUT에는 route를 지정하지 않습니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class ReassemblyControlResult:
    kind: ReassemblyControlKind
    session_handle: SessionHandle
    active: bool
    discarded_fragment_count: int
    discarded_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReassemblyControlKind) or not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("kind/session_handle 형식이 잘못되었습니다")
        if not isinstance(self.active, bool):
            raise InvalidValueError("active: bool이어야 합니다")
        _int(self.discarded_fragment_count, "discarded_fragment_count", 0, (1 << 31) - 1)
        _int(self.discarded_bytes, "discarded_bytes", 0, (1 << 32) - 1)


class TransferPacketOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    DISCARDED = "DISCARDED"


@dataclass(frozen=True, slots=True)
class TransferPacketProgress:
    packet_id: PacketId
    outcome: TransferPacketOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId) or not isinstance(self.outcome, TransferPacketOutcome):
            raise InvalidValueError("packet_id/outcome 형식이 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class TransferProgressSnapshot:
    transfer_id: TransferId
    sequential_id: SequentialId
    segment_size: int
    committed_prefix_bytes: int
    remaining_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId) or not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("transfer_id/sequential_id 형식이 잘못되었습니다")
        _int(self.segment_size, "segment_size", 4, 65539)
        _int(self.committed_prefix_bytes, "committed_prefix_bytes", 0, 65539)
        _int(self.remaining_bytes, "remaining_bytes", 0, 65539)
        if self.committed_prefix_bytes + self.remaining_bytes != self.segment_size:
            raise InvalidValueError("committed_prefix_bytes + remaining_bytes가 segment_size와 같아야 합니다")


class TransferControlKind(str, Enum):
    PROGRESS = "PROGRESS"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    CANCEL = "CANCEL"
    ABORT_SESSION = "ABORT_SESSION"


@dataclass(frozen=True, slots=True)
class LinkTransferControlRequest:
    kind: TransferControlKind
    transfer_id: TransferId | None
    session_handle: SessionHandle | None
    packet_progress: tuple[TransferPacketProgress, ...]
    correlation: Correlation

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransferControlKind):
            raise InvalidValueError("kind: TransferControlKind여야 합니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("transfer_id: TransferId여야 합니다")
        if self.session_handle is not None and not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("session_handle: SessionHandle이어야 합니다")
        _tuple(self.packet_progress, "packet_progress", TransferPacketProgress)
        packet_ids = tuple(item.packet_id for item in self.packet_progress)
        if len(set(packet_ids)) != len(packet_ids):
            raise InvalidValueError("packet_progress에 중복 packet_id가 있습니다")
        if self.kind is TransferControlKind.PROGRESS:
            if self.transfer_id is None or not self.packet_progress:
                raise InvalidValueError("PROGRESS에는 transfer_id와 packet_progress가 필요합니다")
        elif self.kind is TransferControlKind.ABORT_SESSION:
            if self.transfer_id is not None or self.session_handle is None or self.packet_progress:
                raise InvalidValueError("ABORT_SESSION field 조합이 잘못되었습니다")
        elif self.transfer_id is None or self.packet_progress:
            raise InvalidValueError("terminal control에는 transfer_id와 빈 packet_progress가 필요합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class LinkTransferControlResult:
    kind: TransferControlKind
    progress: tuple[TransferProgressSnapshot, ...]
    released_transfer_ids: tuple[TransferId, ...]
    released_sequential_ids: tuple[SequentialId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransferControlKind):
            raise InvalidValueError("kind: TransferControlKind여야 합니다")
        _tuple(self.progress, "progress", TransferProgressSnapshot)
        _tuple(self.released_transfer_ids, "released_transfer_ids", TransferId)
        _tuple(self.released_sequential_ids, "released_sequential_ids", SequentialId)


class TbbAssemblyControlKind(str, Enum):
    ABORT = "ABORT"
    TIMEOUT = "TIMEOUT"
    REPLACE = "REPLACE"


@dataclass(frozen=True, slots=True)
class TbbAssemblyControlRequest:
    kind: TbbAssemblyControlKind
    target: TbbCandidateKey
    replacement: TbbCandidateKey | None
    correlation: Correlation

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TbbAssemblyControlKind) or not isinstance(self.target, TbbCandidateKey):
            raise InvalidValueError("kind/target 형식이 잘못되었습니다")
        if self.replacement is not None and not isinstance(self.replacement, TbbCandidateKey):
            raise InvalidValueError("replacement: TbbCandidateKey여야 합니다")
        if (self.kind is TbbAssemblyControlKind.REPLACE) != (self.replacement is not None):
            raise InvalidValueError("REPLACE에서만 replacement가 필요합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class TbbAssemblyControlResult:
    kind: TbbAssemblyControlKind
    discarded_candidates: tuple[TbbCandidateKey, ...]
    discarded_fragment_count: int
    discarded_bytes: int
    active_candidate_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TbbAssemblyControlKind):
            raise InvalidValueError("kind: TbbAssemblyControlKind여야 합니다")
        _tuple(self.discarded_candidates, "discarded_candidates", TbbCandidateKey)
        _int(self.discarded_fragment_count, "discarded_fragment_count", 0, (1 << 31) - 1)
        _int(self.discarded_bytes, "discarded_bytes", 0, (1 << 32) - 1)
        _int(self.active_candidate_count, "active_candidate_count", 0, 64)
