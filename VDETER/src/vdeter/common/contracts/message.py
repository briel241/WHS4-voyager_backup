"""Stage 1 Link Message와 TBB의 immutable 공개 DTO.

이 module은 wire byte를 소유하지 않는다. 각 field의 수치 범위와 Message body/type 관계만
고정하며 실제 encode/decode 오류는 Link codec 경계에서 구조화한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..addressing import Address, RouteAddresses
from ..errors import Correlation
from ..exceptions import InvalidValueError
from ..identifiers import LinkId, PacketId, SequentialId, SessionHandle, StationId
from ..time import TimePoint


def _int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidValueError(f"{name}: {low}..{high} 정수여야 합니다")
    return value


def _bytes(value: object, name: str, *, size: int | None = None, nonempty: bool = False) -> bytes:
    if not isinstance(value, bytes):
        raise InvalidValueError(f"{name}: bytes여야 합니다")
    if size is not None and len(value) != size:
        raise InvalidValueError(f"{name}: 정확히 {size} bytes여야 합니다")
    if nonempty and not value:
        raise InvalidValueError(f"{name}: 비어 있을 수 없습니다")
    return value


def _source(value: object, name: str = "source_id") -> StationId:
    if not isinstance(value, StationId) or value.value == 0:
        raise InvalidValueError(f"{name}: 0이 아닌 StationId여야 합니다")
    return value


def _address(value: object, name: str, *, broadcast: bool = True) -> Address:
    if not isinstance(value, Address):
        raise InvalidValueError(f"{name}: Address여야 합니다")
    if not broadcast and value.is_broadcast:
        raise InvalidValueError(f"{name}: broadcast를 허용하지 않습니다")
    return value


def _session_zero(value: object) -> None:
    _int(value, "session_id", 0, 0)


@dataclass(frozen=True, slots=True)
class FragmentRef:
    index: int
    count: int | None = None

    def __post_init__(self) -> None:
        _int(self.index, "FragmentRef.index", 0, (1 << 31) - 1)
        if self.count is not None:
            _int(self.count, "FragmentRef.count", 1, (1 << 31) - 1)
            if self.index >= self.count:
                raise InvalidValueError("FragmentRef: index가 count 범위를 벗어났습니다")


@dataclass(frozen=True, slots=True)
class MediaAccessControlBody:
    source_id: StationId
    media_access_priority_level: int
    random_access_selection_interval: int
    short_data_message_limit: int
    system_status: int

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.media_access_priority_level, "media_access_priority_level", 0, 0)
        _int(self.random_access_selection_interval, "random_access_selection_interval", 0, 511)
        _int(self.short_data_message_limit, "short_data_message_limit", 0, 127)
        if self.system_status not in (0, 10, 20, 30) or isinstance(self.system_status, bool):
            raise InvalidValueError("system_status: 0, 10, 20, 30 중 하나여야 합니다")


@dataclass(frozen=True, slots=True)
class ResourceAllocationBody:
    source_id: StationId
    destination: Address
    logical_channel_tx: int
    logical_channel_rx: int
    link_id: LinkId
    tdma_frame_delay: int
    session_id: int
    cqi: int

    def __post_init__(self) -> None:
        _source(self.source_id)
        _address(self.destination, "destination")
        _int(self.logical_channel_tx, "logical_channel_tx", 0, 255)
        _int(self.logical_channel_rx, "logical_channel_rx", 0, 255)
        if not isinstance(self.link_id, LinkId) or self.link_id.value not in (11, 17, 19):
            raise InvalidValueError("link_id: 운영 지원값 11, 17, 19 중 하나여야 합니다")
        _int(self.tdma_frame_delay, "tdma_frame_delay", 1, 255)
        _session_zero(self.session_id)
        _int(self.cqi, "cqi", 0, 255)


@dataclass(frozen=True, slots=True)
class AcknowledgeNackBody:
    source_id: StationId
    destination: Address
    session_id: int
    ack_nack_masks: tuple[int, int, int]
    cqi: int
    acm_or_edn: int
    power_setting: int

    def __post_init__(self) -> None:
        _source(self.source_id)
        _address(self.destination, "destination", broadcast=False)
        _session_zero(self.session_id)
        if not isinstance(self.ack_nack_masks, tuple) or len(self.ack_nack_masks) != 3:
            raise InvalidValueError("ack_nack_masks: uint16 세 개의 tuple이어야 합니다")
        for value in self.ack_nack_masks:
            _int(value, "ack_nack_masks item", 0, 65535)
        _int(self.cqi, "cqi", 0, 255)
        _int(self.acm_or_edn, "acm_or_edn", 0, 3)
        _int(self.power_setting, "power_setting", 0, 0)


@dataclass(frozen=True, slots=True)
class TbbStartFragmentBody:
    source_id: StationId
    control_station_id: int
    service_capability: int
    bulletin_board_version: int
    number_of_fragments: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.service_capability, "service_capability", 0, 2)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)
        _int(self.number_of_fragments, "number_of_fragments", 1, 6)
        _bytes(self.fragment_payload, "fragment_payload", nonempty=True)
        if len(self.fragment_payload) > 38:
            raise InvalidValueError("fragment_payload: Type 20 상한 38 bytes를 넘습니다")


@dataclass(frozen=True, slots=True)
class TbbContinuationFragmentBody:
    source_id: StationId
    control_station_id: int
    bulletin_board_version: int
    fragment_number: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)
        _int(self.fragment_number, "fragment_number", 1, 5)
        _bytes(self.fragment_payload, "fragment_payload", nonempty=True)
        if len(self.fragment_payload) > 39:
            raise InvalidValueError("fragment_payload: Type 21 상한 39 bytes를 넘습니다")


@dataclass(frozen=True, slots=True)
class TbbEndFragmentBody:
    source_id: StationId
    control_station_id: int
    bulletin_board_version: int
    fragment_number: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)
        _int(self.fragment_number, "fragment_number", 1, 5)
        _bytes(self.fragment_payload, "fragment_payload", nonempty=True)
        if len(self.fragment_payload) > 39:
            raise InvalidValueError("fragment_payload: Type 22 상한 39 bytes를 넘습니다")


def _validate_data_common(body: object) -> None:
    _source(getattr(body, "source_id"))
    _session_zero(getattr(body, "session_id"))
    _address(getattr(body, "destination"), "destination")
    _int(getattr(body, "frame_fragment_count"), "frame_fragment_count", 1, 14)
    _int(getattr(body, "global_fragment_number"), "global_fragment_number", 0, 255)
    _bytes(getattr(body, "fragment_payload"), "fragment_payload", nonempty=True)


@dataclass(frozen=True, slots=True)
class DataStartFragmentBody:
    source_id: StationId
    session_id: int
    destination: Address
    frame_fragment_count: int
    global_fragment_number: int
    continue_resource_usage: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _validate_data_common(self)
        _int(self.continue_resource_usage, "continue_resource_usage", 0, 1)


@dataclass(frozen=True, slots=True)
class DataContinuationFragmentBody:
    source_id: StationId
    session_id: int
    destination: Address
    frame_fragment_count: int
    fragment_position_in_frame: int
    global_fragment_number: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _validate_data_common(self)
        _int(self.fragment_position_in_frame, "fragment_position_in_frame", 2, 13)
        if self.fragment_position_in_frame >= self.frame_fragment_count:
            raise InvalidValueError("fragment_position_in_frame: 마지막 위치보다 작아야 합니다")


@dataclass(frozen=True, slots=True)
class DataEndFragmentBody:
    source_id: StationId
    session_id: int
    destination: Address
    frame_fragment_count: int
    global_fragment_number: int
    continue_resource_usage: int
    fragment_payload: bytes

    def __post_init__(self) -> None:
        _validate_data_common(self)
        if self.frame_fragment_count < 2:
            raise InvalidValueError("DataEndFragmentBody.frame_fragment_count: 2..14여야 합니다")
        _int(self.continue_resource_usage, "continue_resource_usage", 0, 1)


@dataclass(frozen=True, slots=True)
class PaddingBody:
    pass


@dataclass(frozen=True, slots=True)
class ResourceRequestBody:
    original_source_id: StationId
    node_source_id: StationId
    node_destination: Address
    original_destination: Address
    priority: int
    terminal_capabilities: int

    def __post_init__(self) -> None:
        _source(self.original_source_id, "original_source_id")
        _source(self.node_source_id, "node_source_id")
        _address(self.node_destination, "node_destination")
        _address(self.original_destination, "original_destination")
        _int(self.priority, "priority", 0, 0)
        capabilities = _int(self.terminal_capabilities, "terminal_capabilities", 0, (1 << 32) - 1)
        if capabilities & ~0x1F or (capabilities & 0x0F) not in (0, 1, 2):
            raise InvalidValueError("terminal_capabilities: version 0..2와 bit 4만 허용합니다")


@dataclass(frozen=True, slots=True)
class ShortDataAckBody:
    source_id: StationId
    session_id: int
    destination: Address
    retransmission_number: int
    segment: bytes

    def __post_init__(self) -> None:
        _source(self.source_id)
        _session_zero(self.session_id)
        _address(self.destination, "destination", broadcast=False)
        _int(self.retransmission_number, "retransmission_number", 0, 255)
        _bytes(self.segment, "segment", nonempty=True)


@dataclass(frozen=True, slots=True)
class ShortDataNoAckBody:
    source_id: StationId
    session_id: int
    destination: Address
    segment: bytes

    def __post_init__(self) -> None:
        _source(self.source_id)
        _session_zero(self.session_id)
        _address(self.destination, "destination")
        _bytes(self.segment, "segment", nonempty=True)


class LogicalChannelFunction(str, Enum):
    BULLETIN_BOARD = "BULLETIN_BOARD"
    RANDOM_ACCESS = "RANDOM_ACCESS"
    ANNOUNCEMENT_SIGNALLING = "ANNOUNCEMENT_SIGNALLING"
    DATA = "DATA"
    DATA_SIGNALLING = "DATA_SIGNALLING"
    RANGING = "RANGING"


class PhysicalChannelBandwidth(str, Enum):
    KHZ_25 = "KHZ_25"
    KHZ_100 = "KHZ_100"


@dataclass(frozen=True, slots=True)
class LogicalChannelDefinition:
    function: LogicalChannelFunction
    repeat: bool
    duration: int

    def __post_init__(self) -> None:
        if not isinstance(self.function, LogicalChannelFunction):
            raise InvalidValueError("function: LogicalChannelFunction이어야 합니다")
        if not isinstance(self.repeat, bool):
            raise InvalidValueError("repeat: bool이어야 합니다")
        _int(self.duration, "duration", 0, 255)


@dataclass(frozen=True, slots=True)
class LogicalChannelGroup:
    tdma_channel_index: int
    definitions: tuple[LogicalChannelDefinition, ...]

    def __post_init__(self) -> None:
        _int(self.tdma_channel_index, "tdma_channel_index", 0, 5)
        if not isinstance(self.definitions, tuple) or len(self.definitions) > 63 or any(
            not isinstance(item, LogicalChannelDefinition) for item in self.definitions
        ):
            raise InvalidValueError("definitions: 0..63 LogicalChannelDefinition tuple이어야 합니다")


@dataclass(frozen=True, slots=True)
class PhysicalChannelDefinition:
    channel_number: int
    frequency: int
    bandwidth: PhysicalChannelBandwidth
    transmit_enabled: bool
    random_access_selection_interval: int
    short_data_message_limit: int
    logical_channel_groups: tuple[LogicalChannelGroup, ...]

    def __post_init__(self) -> None:
        _int(self.channel_number, "channel_number", 0, 255)
        _int(self.frequency, "frequency", 0, 4095)
        if not isinstance(self.bandwidth, PhysicalChannelBandwidth):
            raise InvalidValueError("bandwidth: 지원되는 PhysicalChannelBandwidth여야 합니다")
        if not isinstance(self.transmit_enabled, bool):
            raise InvalidValueError("transmit_enabled: bool이어야 합니다")
        _int(self.random_access_selection_interval, "random_access_selection_interval", 0, 511)
        _int(self.short_data_message_limit, "short_data_message_limit", 0, 127)
        groups = self.logical_channel_groups
        if not isinstance(groups, tuple) or len(groups) != 6 or any(
            not isinstance(group, LogicalChannelGroup) for group in groups
        ):
            raise InvalidValueError("logical_channel_groups: 정확히 6개 group이어야 합니다")
        if tuple(group.tdma_channel_index for group in groups) != tuple(range(6)):
            raise InvalidValueError("logical_channel_groups: index 0..5 오름차순이어야 합니다")


@dataclass(frozen=True, slots=True)
class ControlStationServiceArea:
    longitude_1_tenth_minutes: int
    latitude_1_tenth_minutes: int
    longitude_2_tenth_minutes: int
    latitude_2_tenth_minutes: int
    priority: int

    def __post_init__(self) -> None:
        _int(self.longitude_1_tenth_minutes, "longitude_1_tenth_minutes", -108000, 108000)
        _int(self.latitude_1_tenth_minutes, "latitude_1_tenth_minutes", -54000, 54000)
        _int(self.longitude_2_tenth_minutes, "longitude_2_tenth_minutes", -108000, 108000)
        _int(self.latitude_2_tenth_minutes, "latitude_2_tenth_minutes", -54000, 54000)
        _int(self.priority, "priority", 0, 3)


@dataclass(frozen=True, slots=True)
class BulletinBoardBody:
    start_time: int
    validity: int
    tdma_frame_size: int
    physical_channels: tuple[PhysicalChannelDefinition, ...]
    modulation_coding_protocol: int
    service_area: ControlStationServiceArea

    def __post_init__(self) -> None:
        _int(self.start_time, "start_time", 0, (1 << 32) - 1)
        _int(self.validity, "validity", 0, 64800)
        if self.tdma_frame_size not in (3, 5, 15) or isinstance(self.tdma_frame_size, bool):
            raise InvalidValueError("tdma_frame_size: 3, 5, 15 중 하나여야 합니다")
        if not isinstance(self.physical_channels, tuple) or not self.physical_channels or len(
            self.physical_channels
        ) > 256 or any(
            not isinstance(item, PhysicalChannelDefinition) for item in self.physical_channels
        ):
            raise InvalidValueError("physical_channels: 1..256 PhysicalChannelDefinition tuple이어야 합니다")
        _int(self.modulation_coding_protocol, "modulation_coding_protocol", 0, 0)
        if not isinstance(self.service_area, ControlStationServiceArea):
            raise InvalidValueError("service_area: ControlStationServiceArea여야 합니다")


@dataclass(frozen=True, slots=True)
class VerifiedTbb:
    source_id: StationId
    control_station_id: int
    service_capability: int
    bulletin_board_version: int
    bulletin_board: BulletinBoardBody
    certificate_fingerprint: bytes
    authentication_sequence: bytes
    fragment_count: int
    packet_ids: tuple[PacketId, ...]
    received_at: TimePoint

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.service_capability, "service_capability", 0, 2)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)
        if not isinstance(self.bulletin_board, BulletinBoardBody):
            raise InvalidValueError("bulletin_board: BulletinBoardBody여야 합니다")
        _bytes(self.certificate_fingerprint, "certificate_fingerprint", size=6)
        _bytes(self.authentication_sequence, "authentication_sequence", size=64)
        _int(self.fragment_count, "fragment_count", 1, 6)
        if not isinstance(self.packet_ids, tuple) or len(self.packet_ids) != self.fragment_count or any(
            not isinstance(item, PacketId) for item in self.packet_ids
        ):
            raise InvalidValueError("packet_ids: fragment_count와 같은 길이의 PacketId tuple이어야 합니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("received_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class TbbCandidateKey:
    source_id: StationId
    control_station_id: int
    bulletin_board_version: int

    def __post_init__(self) -> None:
        _source(self.source_id)
        _int(self.control_station_id, "control_station_id", 0, 255)
        _int(self.bulletin_board_version, "bulletin_board_version", 0, 65535)


class TbbAssemblyStatus(str, Enum):
    STARTED = "STARTED"
    INCOMPLETE = "INCOMPLETE"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True, slots=True)
class TbbAssemblyUpdate:
    key: TbbCandidateKey
    status: TbbAssemblyStatus
    received_fragment_count: int
    expected_fragment_count: int
    packet_ids: tuple[PacketId, ...]
    updated_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.key, TbbCandidateKey):
            raise InvalidValueError("key: TbbCandidateKey여야 합니다")
        if not isinstance(self.status, TbbAssemblyStatus):
            raise InvalidValueError("status: TbbAssemblyStatus여야 합니다")
        _int(self.received_fragment_count, "received_fragment_count", 1, 6)
        _int(self.expected_fragment_count, "expected_fragment_count", 1, 6)
        if self.received_fragment_count > self.expected_fragment_count:
            raise InvalidValueError("received_fragment_count가 expected 값을 넘을 수 없습니다")
        if not isinstance(self.packet_ids, tuple) or len(self.packet_ids) != self.received_fragment_count or any(
            not isinstance(item, PacketId) for item in self.packet_ids
        ):
            raise InvalidValueError("packet_ids: 수신 fragment 수와 같은 PacketId tuple이어야 합니다")
        if not isinstance(self.updated_at, TimePoint):
            raise InvalidValueError("updated_at: TimePoint여야 합니다")


MessageBody = (
    MediaAccessControlBody
    | ResourceAllocationBody
    | AcknowledgeNackBody
    | TbbStartFragmentBody
    | TbbContinuationFragmentBody
    | TbbEndFragmentBody
    | DataStartFragmentBody
    | DataContinuationFragmentBody
    | DataEndFragmentBody
    | PaddingBody
    | ResourceRequestBody
    | ShortDataAckBody
    | ShortDataNoAckBody
)

MESSAGE_TYPE_TO_BODY_TYPE: dict[int, type] = {
    0: MediaAccessControlBody,
    4: ResourceAllocationBody,
    13: AcknowledgeNackBody,
    20: TbbStartFragmentBody,
    21: TbbContinuationFragmentBody,
    22: TbbEndFragmentBody,
    74: DataStartFragmentBody,
    75: DataContinuationFragmentBody,
    76: DataEndFragmentBody,
    81: PaddingBody,
    90: ResourceRequestBody,
    92: ShortDataAckBody,
    93: ShortDataNoAckBody,
}
BODY_TYPE_TO_MESSAGE_TYPE = {value: key for key, value in MESSAGE_TYPE_TO_BODY_TYPE.items()}


@dataclass(frozen=True, slots=True)
class MessageEncodeRequest:
    body: MessageBody
    correlation: Correlation

    def __post_init__(self) -> None:
        if type(self.body) not in BODY_TYPE_TO_MESSAGE_TYPE:
            raise InvalidValueError("body: 지원되는 MessageBody여야 합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("correlation: Correlation이어야 합니다")


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    message_type: int
    length: int
    route: RouteAddresses | None = None
    session_handle_candidate: SessionHandle | None = None
    sequential_id: SequentialId | None = None
    fragment: FragmentRef | None = None
    body: MessageBody = field(kw_only=True)
    presentation_data: bytes | None = None

    def __post_init__(self) -> None:
        _int(self.message_type, "message_type", 0, 255)
        _int(self.length, "length", 1, 65535)
        expected = MESSAGE_TYPE_TO_BODY_TYPE.get(self.message_type)
        if expected is None or type(self.body) is not expected:
            raise InvalidValueError("message_type과 body type이 일치하지 않습니다")
        if self.message_type == 81 and self.length != 1:
            raise InvalidValueError("Type 81 length는 1이어야 합니다")
        if self.route is not None and not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("route: RouteAddresses여야 합니다")
        if self.session_handle_candidate is not None and not isinstance(
            self.session_handle_candidate, SessionHandle
        ):
            raise InvalidValueError("session_handle_candidate: SessionHandle이어야 합니다")
        if self.sequential_id is not None and not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        if self.fragment is not None and not isinstance(self.fragment, FragmentRef):
            raise InvalidValueError("fragment: FragmentRef여야 합니다")
        if self.presentation_data is not None and not isinstance(self.presentation_data, bytes):
            raise InvalidValueError("presentation_data: bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class DecodedMessages:
    packet_id: PacketId
    link_id: LinkId
    received_at: TimePoint
    messages: tuple[DecodedMessage, ...] = ()
    verified_tbbs: tuple[VerifiedTbb, ...] = ()
    tbb_assembly_updates: tuple[TbbAssemblyUpdate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("link_id: LinkId여야 합니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("received_at: TimePoint여야 합니다")
        if not isinstance(self.messages, tuple) or any(
            not isinstance(item, DecodedMessage) for item in self.messages
        ):
            raise InvalidValueError("messages: DecodedMessage tuple이어야 합니다")
        if not isinstance(self.verified_tbbs, tuple) or any(
            not isinstance(item, VerifiedTbb) for item in self.verified_tbbs
        ):
            raise InvalidValueError("verified_tbbs: VerifiedTbb tuple이어야 합니다")
        if not isinstance(self.tbb_assembly_updates, tuple) or any(
            not isinstance(item, TbbAssemblyUpdate) for item in self.tbb_assembly_updates
        ):
            raise InvalidValueError("tbb_assembly_updates: TbbAssemblyUpdate tuple이어야 합니다")
