"""Stage 4 TBB fixture, semantic fingerprint와 slot map (`DR-STAGE4-MAC-004`).

MAC은 TBB wire bytes, 서명 또는 fragment 조립을 다루지 않는다. Link가 조립·검증을 마친
`VerifiedTbb`만 이 module의 semantic projection으로 판정하고, ROC는 같은 profile에서
`TbbEncodeRequest`를 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from ..common.contracts.message import (
    BulletinBoardBody,
    ControlStationServiceArea,
    LogicalChannelDefinition,
    LogicalChannelFunction,
    LogicalChannelGroup,
    PhysicalChannelBandwidth,
    PhysicalChannelDefinition,
    VerifiedTbb,
)
from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId, PhysicalChannel, StationId
from .deployment import (
    MacDeploymentProfile,
    STAGE4_TBB_FIXTURE_ID,
    Stage4TbbFixtureProfile,
    canonical_json_bytes,
    sha256_lowercase_hex,
)
from .tdma import (
    FRAME_NANOSECONDS,
    HEXSLOTS_PER_FRAME,
    NANOSECONDS_PER_MINUTE,
    NANOSECONDS_PER_SECOND,
    TDMA_CHANNEL_COUNT,
)

TBB_SEMANTIC_SCHEMA = "VDETER-STAGE4-TBB-SEMANTIC-V1"

#: 2000-01-01 00:00:00 UTC 이후 초로 표현하는 wire `start_time`의 기준 (Table 43).
TBB_START_TIME_EPOCH_SECONDS = 946_684_800

#: `validity`는 1분 common-frame 개수다.
VALIDITY_UNIT_NANOSECONDS = NANOSECONDS_PER_MINUTE

STAGE4_BOOTSTRAP_ID = "STAGE4_BBSC_BOOTSTRAP_V1"

LOWER_CHANNEL = PhysicalChannel("VDETER_LOWER_25_TEST")
UPPER_CHANNEL = PhysicalChannel("VDETER_UPPER_25_TEST")
STAGE4_LINK_ID = LinkId(11)

#: `DR-STAGE6-MR-001` §13.2.1: 2-PC 100 kHz 통합 fixture 식별자와 alias.
WIDEBAND_FIXTURE_ID = "VDETER_WIDEBAND_100_TBB_V3"
WIDEBAND_BOOTSTRAP_ID = "WIDEBAND_BBSC_BOOTSTRAP_V1"
WIDEBAND_LOWER_CHANNEL = PhysicalChannel("VDETER_LOWER_100_TEST")
WIDEBAND_UPPER_CHANNEL = PhysicalChannel("VDETER_UPPER_100_TEST")
WIDEBAND_TBB_SEMANTIC_SCHEMA = "VDETER-WIDEBAND-TBB-SEMANTIC-V2"
WIDEBAND_DEPLOYMENT_SCHEMA = "VDETER-WIDEBAND-DEPLOYMENT-V2"

#: canonical fixture의 고정 wire 값 (`DR-STAGE4-MAC-004` 표).
FIXTURE_SERVICE_CAPABILITY = 2
FIXTURE_TDMA_FRAME_SIZE = 15
FIXTURE_MODULATION_CODING_PROTOCOL = 0
FIXTURE_BULLETIN_BOARD_VERSION = 1
FIXTURE_SELECTION_INTERVAL_HEXSLOTS = 235
FIXTURE_SHORT_DATA_LIMIT = 4
LOWER_CHANNEL_NUMBER = 0
LOWER_FREQUENCY_CODE = 1284
UPPER_CHANNEL_NUMBER = 1
UPPER_FREQUENCY_CODE = 2284

#: M.2092-2 §A4-4.21의 official default. 물리 slot 수이며 hexslot이 아니다.
OFFICIAL_DEFAULT_SELECTION_INTERVAL_PHYSICAL_SLOTS = 235

_FUNCTION_WIRE_CODE = {
    LogicalChannelFunction.BULLETIN_BOARD: 0,
    LogicalChannelFunction.RANDOM_ACCESS: 1,
    LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING: 2,
    LogicalChannelFunction.DATA: 3,
    LogicalChannelFunction.DATA_SIGNALLING: 4,
    LogicalChannelFunction.RANGING: 5,
}
_BANDWIDTH_WIRE_CODE = {
    PhysicalChannelBandwidth.KHZ_25: 0,
    PhysicalChannelBandwidth.KHZ_100: 2,
}


class SlotFunction(str, Enum):
    """Stage 4가 slot 계획에 사용하는 logical channel function 별칭."""

    BBSC = "BBSC"
    RAC = "RAC"
    ASC = "ASC"
    DC = "DC"
    DSCH = "DSCH"
    RANGING = "RANGING"


_FUNCTION_TO_SLOT_FUNCTION = {
    LogicalChannelFunction.BULLETIN_BOARD: SlotFunction.BBSC,
    LogicalChannelFunction.RANDOM_ACCESS: SlotFunction.RAC,
    LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING: SlotFunction.ASC,
    LogicalChannelFunction.DATA: SlotFunction.DC,
    LogicalChannelFunction.DATA_SIGNALLING: SlotFunction.DSCH,
    LogicalChannelFunction.RANGING: SlotFunction.RANGING,
}


@dataclass(frozen=True, slots=True)
class LogicalChannelSlot:
    """slot map의 한 항목. `logical_channel`은 두 PC를 펼친 전역 fixture 번호다."""

    physical_channel: PhysicalChannel
    tdma_channel: int
    slot_in_frame: int
    function: SlotFunction
    logical_channel: int
    transmit_enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.physical_channel, PhysicalChannel):
            raise InvalidValueError("LogicalChannelSlot.physical_channel이 잘못되었습니다")
        for name, high in (("tdma_channel", 5), ("slot_in_frame", 14), ("logical_channel", 255)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
                raise InvalidValueError(f"LogicalChannelSlot.{name}: 0..{high} 정수여야 합니다")
        if not isinstance(self.function, SlotFunction):
            raise InvalidValueError("LogicalChannelSlot.function: SlotFunction이어야 합니다")
        if not isinstance(self.transmit_enabled, bool):
            raise InvalidValueError("LogicalChannelSlot.transmit_enabled: bool이어야 합니다")

    @property
    def key(self) -> tuple:
        return (self.physical_channel.name, self.tdma_channel, self.slot_in_frame)


@dataclass(frozen=True, slots=True)
class SlotMap:
    """한 TDMA frame의 slot 배치. 활성 TBB 또는 bootstrap에서 파생한다."""

    map_id: str
    slots: tuple[LogicalChannelSlot, ...]
    selection_interval_hexslots: int
    short_data_limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.map_id, str) or not self.map_id:
            raise InvalidValueError("SlotMap.map_id가 비어 있습니다")
        if not isinstance(self.slots, tuple) or any(
            not isinstance(item, LogicalChannelSlot) for item in self.slots
        ):
            raise InvalidValueError("SlotMap.slots: LogicalChannelSlot tuple이어야 합니다")
        keys = tuple(item.key for item in self.slots)
        if len(set(keys)) != len(keys):
            raise InvalidValueError("SlotMap.slots: 같은 (PC, channel, slot)이 중복되었습니다")
        for name, high in (
            ("selection_interval_hexslots", 511),
            ("short_data_limit", 127),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
                raise InvalidValueError(f"SlotMap.{name}: 0..{high} 정수여야 합니다")

    def slots_for(
        self,
        *,
        physical_channel: PhysicalChannel | None = None,
        function: SlotFunction | None = None,
        transmit: bool | None = None,
    ) -> tuple[LogicalChannelSlot, ...]:
        return tuple(
            slot
            for slot in self.slots
            if (physical_channel is None or slot.physical_channel.name == physical_channel.name)
            and (function is None or slot.function is function)
            and (transmit is None or slot.transmit_enabled is transmit)
        )

    def slot_at(
        self, physical_channel: PhysicalChannel, tdma_channel: int, slot_in_frame: int
    ) -> LogicalChannelSlot | None:
        for slot in self.slots:
            if slot.key == (physical_channel.name, tdma_channel, slot_in_frame):
                return slot
        return None

    def slot_for_logical_channel(self, logical_channel: int) -> LogicalChannelSlot | None:
        for slot in self.slots:
            if slot.logical_channel == logical_channel:
                return slot
        return None

    def transmit_enabled_channels(self) -> tuple[PhysicalChannel, ...]:
        seen = []
        for slot in self.slots:
            if slot.transmit_enabled and all(
                item.name != slot.physical_channel.name for item in seen
            ):
                seen.append(slot.physical_channel)
        return tuple(seen)


def _definition(function: LogicalChannelFunction, duration: int) -> LogicalChannelDefinition:
    return LogicalChannelDefinition(function=function, repeat=False, duration=duration)


def _group(index: int, *definitions: LogicalChannelDefinition) -> LogicalChannelGroup:
    return LogicalChannelGroup(tdma_channel_index=index, definitions=tuple(definitions))


def _lower_physical_channel() -> PhysicalChannelDefinition:
    groups = [
        _group(
            0,
            _definition(LogicalChannelFunction.RANDOM_ACCESS, 14),
            _definition(LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING, 1),
        )
    ]
    for index in range(1, TDMA_CHANNEL_COUNT):
        groups.append(
            _group(
                index,
                _definition(LogicalChannelFunction.DATA, 14),
                _definition(LogicalChannelFunction.DATA_SIGNALLING, 1),
            )
        )
    return PhysicalChannelDefinition(
        channel_number=LOWER_CHANNEL_NUMBER,
        frequency=LOWER_FREQUENCY_CODE,
        bandwidth=PhysicalChannelBandwidth.KHZ_25,
        transmit_enabled=True,
        random_access_selection_interval=FIXTURE_SELECTION_INTERVAL_HEXSLOTS,
        short_data_message_limit=FIXTURE_SHORT_DATA_LIMIT,
        logical_channel_groups=tuple(groups),
    )


def _upper_physical_channel() -> PhysicalChannelDefinition:
    groups = [
        _group(
            0,
            _definition(LogicalChannelFunction.BULLETIN_BOARD, 1),
            _definition(LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING, 7),
            _definition(LogicalChannelFunction.RANDOM_ACCESS, 7),
        )
    ]
    for index in range(1, TDMA_CHANNEL_COUNT):
        groups.append(
            _group(
                index,
                _definition(LogicalChannelFunction.DATA, 14),
                _definition(LogicalChannelFunction.DATA_SIGNALLING, 1),
            )
        )
    return PhysicalChannelDefinition(
        channel_number=UPPER_CHANNEL_NUMBER,
        frequency=UPPER_FREQUENCY_CODE,
        bandwidth=PhysicalChannelBandwidth.KHZ_25,
        transmit_enabled=True,
        random_access_selection_interval=FIXTURE_SELECTION_INTERVAL_HEXSLOTS,
        short_data_message_limit=FIXTURE_SHORT_DATA_LIMIT,
        logical_channel_groups=tuple(groups),
    )


def build_fixture_bulletin_board(fixture: Stage4TbbFixtureProfile) -> BulletinBoardBody:
    """`STAGE4_25K_TEST_TBB_V1`의 canonical `BulletinBoardBody`."""

    if not isinstance(fixture, Stage4TbbFixtureProfile):
        raise InvalidValueError("build_fixture_bulletin_board: Stage4TbbFixtureProfile이 필요합니다")
    return BulletinBoardBody(
        start_time=fixture.start_time,
        validity=fixture.validity,
        tdma_frame_size=FIXTURE_TDMA_FRAME_SIZE,
        physical_channels=(_lower_physical_channel(), _upper_physical_channel()),
        modulation_coding_protocol=FIXTURE_MODULATION_CODING_PROTOCOL,
        service_area=fixture.service_area,
    )


def build_wideband_bulletin_board(fixture: Stage4TbbFixtureProfile) -> BulletinBoardBody:
    """`VDETER_WIDEBAND_100_TBB_V3`: base LC 배치를 보존하고 bandwidth wire 값만 100 kHz다.

    §13.2.1: base 두 PC의 LC tuple·RA/Short Data 파라미터·mobile Tx flag를 semantic-exact
    보존하고 Physical Channel bandwidth(`0`→`2`)와 alias/setting만 바꾼다.
    """

    base = build_fixture_bulletin_board(fixture)
    channels = tuple(
        replace(definition, bandwidth=PhysicalChannelBandwidth.KHZ_100)
        for definition in base.physical_channels
    )
    return replace(base, physical_channels=channels)


#: fixture PC 순서(index) → Stage 4 Physical Channel 이름.
_PHYSICAL_CHANNEL_BY_INDEX = (LOWER_CHANNEL, UPPER_CHANNEL)

#: wideband fixture PC 순서(index) → 100 kHz Physical Channel 이름.
WIDEBAND_PHYSICAL_CHANNEL_BY_INDEX = (WIDEBAND_LOWER_CHANNEL, WIDEBAND_UPPER_CHANNEL)


def slot_map_from_bulletin_board(
    board: BulletinBoardBody,
    map_id: str,
    *,
    physical_channels: tuple[PhysicalChannel, ...] = _PHYSICAL_CHANNEL_BY_INDEX,
) -> SlotMap:
    """TBB의 logical channel 정의를 전역 LC 번호가 붙은 frame slot map으로 펼친다.

    LC 번호는 PC 순서 → TDMA channel 순서 → 정의 순서로 이어지는 전역 fixture 번호다.
    """

    if not isinstance(board, BulletinBoardBody):
        raise InvalidValueError("slot_map_from_bulletin_board: BulletinBoardBody가 필요합니다")
    if len(board.physical_channels) > len(physical_channels):
        raise InvalidValueError(
            "slot_map_from_bulletin_board: Physical Channel 이름이 부족합니다"
        )
    slots: list[LogicalChannelSlot] = []
    logical_channel = 0
    for pc_index, definition in enumerate(board.physical_channels):
        channel_name = physical_channels[pc_index]
        for group in definition.logical_channel_groups:
            slot_in_frame = 0
            for entry in group.definitions:
                function = _FUNCTION_TO_SLOT_FUNCTION[entry.function]
                for _ in range(entry.duration):
                    if slot_in_frame >= HEXSLOTS_PER_FRAME:
                        raise InvalidValueError(
                            "slot_map_from_bulletin_board: TDMA channel duration 합이 15를 넘습니다"
                        )
                    slots.append(
                        LogicalChannelSlot(
                            physical_channel=channel_name,
                            tdma_channel=group.tdma_channel_index,
                            slot_in_frame=slot_in_frame,
                            function=function,
                            logical_channel=logical_channel,
                            transmit_enabled=definition.transmit_enabled,
                        )
                    )
                    slot_in_frame += 1
                logical_channel += 1
            if slot_in_frame != HEXSLOTS_PER_FRAME:
                raise InvalidValueError(
                    "slot_map_from_bulletin_board: TDMA channel duration 합이 정확히 15가 아닙니다"
                )
    interval = board.physical_channels[0].random_access_selection_interval
    limit = board.physical_channels[0].short_data_message_limit
    return SlotMap(
        map_id=map_id,
        slots=tuple(slots),
        selection_interval_hexslots=interval,
        short_data_limit=limit,
    )


def build_bootstrap_slot_map(
    upper: PhysicalChannel = UPPER_CHANNEL, map_id: str = STAGE4_BOOTSTRAP_ID
) -> SlotMap:
    """BBSC bootstrap: upper PC TDMA channel 0 slot 0의 BBSC 한 slot만 연다.

    RAC/ASC/DC/DSCH 또는 데이터 송신 권한을 부여하지 않는다. ROC만 이 slot에서 TBB를
    송신하며 MASS는 계속 수신한다. 기본값은 동결 `STAGE4_BBSC_BOOTSTRAP_V1`이다.
    """

    return SlotMap(
        map_id=map_id,
        slots=(
            LogicalChannelSlot(
                physical_channel=upper,
                tdma_channel=0,
                slot_in_frame=0,
                function=SlotFunction.BBSC,
                logical_channel=0,
                transmit_enabled=True,
            ),
        ),
        selection_interval_hexslots=0,
        short_data_limit=0,
    )


STAGE4_BBSC_BOOTSTRAP_V1 = build_bootstrap_slot_map()


# --- semantic fingerprint ---


def _physical_channel_value(definition: PhysicalChannelDefinition) -> dict:
    return {
        "bandwidth": _BANDWIDTH_WIRE_CODE[definition.bandwidth],
        "channel_number": definition.channel_number,
        "frequency": definition.frequency,
        "logical_channel_groups": [
            {
                "definitions": [
                    {
                        "duration": entry.duration,
                        "function": _FUNCTION_WIRE_CODE[entry.function],
                        "repeat": bool(entry.repeat),
                    }
                    for entry in group.definitions
                ],
                "tdma_channel_index": group.tdma_channel_index,
            }
            for group in definition.logical_channel_groups
        ],
        "random_access_selection_interval": definition.random_access_selection_interval,
        "short_data_message_limit": definition.short_data_message_limit,
        "transmit_enabled": bool(definition.transmit_enabled),
    }


def _service_area_value(area: ControlStationServiceArea) -> dict:
    return {
        "latitude_1_tenth_minutes": area.latitude_1_tenth_minutes,
        "latitude_2_tenth_minutes": area.latitude_2_tenth_minutes,
        "longitude_1_tenth_minutes": area.longitude_1_tenth_minutes,
        "longitude_2_tenth_minutes": area.longitude_2_tenth_minutes,
        "priority": area.priority,
    }


def tbb_semantic_value(
    *,
    fixture_id: str,
    source_id: StationId,
    control_station_id: int,
    service_capability: int,
    bulletin_board_version: int,
    bulletin_board: BulletinBoardBody,
    certificate_fingerprint: bytes,
    schema: str = TBB_SEMANTIC_SCHEMA,
) -> dict:
    """`VDETER-STAGE4-TBB-SEMANTIC-V1` canonical JSON object.

    `authentication_sequence`, fragment 수·Packet ID와 수신시각은 전송·검증 metadata이므로
    제외한다.
    """

    return {
        "bulletin_board": {
            "modulation_coding_protocol": bulletin_board.modulation_coding_protocol,
            "physical_channels": [
                _physical_channel_value(item) for item in bulletin_board.physical_channels
            ],
            "service_area": _service_area_value(bulletin_board.service_area),
            "start_time": bulletin_board.start_time,
            "tdma_frame_size": bulletin_board.tdma_frame_size,
            "validity": bulletin_board.validity,
        },
        "bulletin_board_version": bulletin_board_version,
        "certificate_fingerprint": certificate_fingerprint.hex(),
        "control_station_id": control_station_id,
        "fixture_id": fixture_id,
        "schema": schema,
        "service_capability": service_capability,
        "source_id": source_id.value,
    }


def tbb_semantic_fingerprint(value: dict) -> tuple[bytes, str]:
    """canonical UTF-8 bytes와 SHA-256 lowercase hex를 함께 반환한다."""

    canonical = canonical_json_bytes(value)
    return canonical, sha256_lowercase_hex(canonical)


@dataclass(frozen=True, slots=True)
class CanonicalStage4TbbProfile:
    """fixture·배치 값을 결합한 불변 TBB semantic profile."""

    fixture: Stage4TbbFixtureProfile
    source_id: StationId
    control_station_id: int
    service_capability: int
    bulletin_board_version: int
    bulletin_board: BulletinBoardBody
    expected_certificate_fingerprint: bytes
    canonical_tbb_fingerprint: str
    canonical_bytes: bytes
    slot_map: SlotMap
    #: `DR-STAGE6-MR-001`: 수신 projection에 쓰는 semantic identity. 기본은 legacy V1이다.
    semantic_fixture_id: str = STAGE4_TBB_FIXTURE_ID
    semantic_schema: str = TBB_SEMANTIC_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.fixture, Stage4TbbFixtureProfile):
            raise InvalidValueError("CanonicalStage4TbbProfile.fixture가 잘못되었습니다")
        if not isinstance(self.source_id, StationId) or self.source_id.value == 0:
            raise InvalidValueError("CanonicalStage4TbbProfile.source_id가 잘못되었습니다")
        if not isinstance(self.bulletin_board, BulletinBoardBody):
            raise InvalidValueError("CanonicalStage4TbbProfile.bulletin_board가 잘못되었습니다")
        if (
            not isinstance(self.canonical_tbb_fingerprint, str)
            or len(self.canonical_tbb_fingerprint) != 64
        ):
            raise InvalidValueError("CanonicalStage4TbbProfile.canonical_tbb_fingerprint가 잘못되었습니다")
        if not isinstance(self.slot_map, SlotMap):
            raise InvalidValueError("CanonicalStage4TbbProfile.slot_map이 잘못되었습니다")

    def valid_from_nanoseconds(self) -> int:
        """`start_time`(2000-01-01 이후 초)의 UTC 나노초."""

        return (
            TBB_START_TIME_EPOCH_SECONDS + self.fixture.start_time
        ) * NANOSECONDS_PER_SECOND

    def valid_until_nanoseconds(self) -> int:
        """`[valid_from, valid_until)`의 제외 경계."""

        return self.valid_from_nanoseconds() + self.fixture.validity * VALIDITY_UNIT_NANOSECONDS

    def last_valid_frame_ordinal(self) -> int:
        """`valid_until` 직전 frame ordinal (`floor((valid_until-1)/2.4s)`)."""

        from .tdma import EPOCH_2000_UTC_NANOSECONDS

        return (
            self.valid_until_nanoseconds() - 1 - EPOCH_2000_UTC_NANOSECONDS
        ) // FRAME_NANOSECONDS


def build_canonical_tbb_profile(
    deployment: MacDeploymentProfile,
) -> CanonicalStage4TbbProfile:
    """배치 profile에서 canonical TBB profile과 fingerprint를 만든다.

    `Stage4TbbFixtureProfile`만의 일부 field를 hash하지 않고 두 station ID, 고정 PC·LC map과
    provisioned certificate fingerprint를 함께 결합한다.
    """

    if not isinstance(deployment, MacDeploymentProfile):
        raise InvalidValueError("build_canonical_tbb_profile: MacDeploymentProfile이 필요합니다")
    board = build_fixture_bulletin_board(deployment.tbb_fixture)
    value = tbb_semantic_value(
        fixture_id=deployment.tbb_fixture.fixture_id,
        source_id=deployment.roc_station_id,
        control_station_id=deployment.tbb_control_station_id,
        service_capability=FIXTURE_SERVICE_CAPABILITY,
        bulletin_board_version=FIXTURE_BULLETIN_BOARD_VERSION,
        bulletin_board=board,
        certificate_fingerprint=deployment.expected_tbb_certificate_fingerprint,
    )
    canonical, fingerprint = tbb_semantic_fingerprint(value)
    return CanonicalStage4TbbProfile(
        fixture=deployment.tbb_fixture,
        source_id=deployment.roc_station_id,
        control_station_id=deployment.tbb_control_station_id,
        service_capability=FIXTURE_SERVICE_CAPABILITY,
        bulletin_board_version=FIXTURE_BULLETIN_BOARD_VERSION,
        bulletin_board=board,
        expected_certificate_fingerprint=deployment.expected_tbb_certificate_fingerprint,
        canonical_tbb_fingerprint=fingerprint,
        canonical_bytes=canonical,
        slot_map=slot_map_from_bulletin_board(board, STAGE4_TBB_FIXTURE_ID),
    )


def build_wideband_canonical_tbb_profile(
    deployment: MacDeploymentProfile,
) -> CanonicalStage4TbbProfile:
    """`VDETER_WIDEBAND_100_TBB_V3` canonical TBB profile (`DR-STAGE6-MR-001` §13.2.1).

    base fixture field를 그대로 상속하고 board의 bandwidth wire 값과 semantic
    schema/fixture-id, slot map alias만 wideband 값으로 바꾼다.
    """

    if not isinstance(deployment, MacDeploymentProfile):
        raise InvalidValueError(
            "build_wideband_canonical_tbb_profile: MacDeploymentProfile이 필요합니다"
        )
    board = build_wideband_bulletin_board(deployment.tbb_fixture)
    value = tbb_semantic_value(
        fixture_id=WIDEBAND_FIXTURE_ID,
        source_id=deployment.roc_station_id,
        control_station_id=deployment.tbb_control_station_id,
        service_capability=FIXTURE_SERVICE_CAPABILITY,
        bulletin_board_version=FIXTURE_BULLETIN_BOARD_VERSION,
        bulletin_board=board,
        certificate_fingerprint=deployment.expected_tbb_certificate_fingerprint,
        schema=WIDEBAND_TBB_SEMANTIC_SCHEMA,
    )
    canonical, fingerprint = tbb_semantic_fingerprint(value)
    return CanonicalStage4TbbProfile(
        fixture=deployment.tbb_fixture,
        source_id=deployment.roc_station_id,
        control_station_id=deployment.tbb_control_station_id,
        service_capability=FIXTURE_SERVICE_CAPABILITY,
        bulletin_board_version=FIXTURE_BULLETIN_BOARD_VERSION,
        bulletin_board=board,
        expected_certificate_fingerprint=deployment.expected_tbb_certificate_fingerprint,
        canonical_tbb_fingerprint=fingerprint,
        canonical_bytes=canonical,
        slot_map=slot_map_from_bulletin_board(
            board,
            WIDEBAND_FIXTURE_ID,
            physical_channels=WIDEBAND_PHYSICAL_CHANNEL_BY_INDEX,
        ),
        semantic_fixture_id=WIDEBAND_FIXTURE_ID,
        semantic_schema=WIDEBAND_TBB_SEMANTIC_SCHEMA,
    )


def received_tbb_fingerprint(
    verified: VerifiedTbb, fixture_id: str, schema: str = TBB_SEMANTIC_SCHEMA
) -> tuple[bytes, str]:
    """수신 `VerifiedTbb`의 semantic projection.

    wire에 없는 schema tag와 `fixture_id`만 기대 local profile에서 넣고, 그 밖의 모든 값은
    반드시 실제 `VerifiedTbb`에서 읽는다. 기대 고정값으로 수신 field를 덮어쓰지 않는다.
    """

    if not isinstance(verified, VerifiedTbb):
        raise InvalidValueError("received_tbb_fingerprint: VerifiedTbb가 필요합니다")
    value = tbb_semantic_value(
        fixture_id=fixture_id,
        source_id=verified.source_id,
        control_station_id=verified.control_station_id,
        service_capability=verified.service_capability,
        bulletin_board_version=verified.bulletin_board_version,
        bulletin_board=verified.bulletin_board,
        certificate_fingerprint=verified.certificate_fingerprint,
        schema=schema,
    )
    return tbb_semantic_fingerprint(value)


__all__ = (
    "CanonicalStage4TbbProfile",
    "FIXTURE_BULLETIN_BOARD_VERSION",
    "FIXTURE_SELECTION_INTERVAL_HEXSLOTS",
    "FIXTURE_SERVICE_CAPABILITY",
    "FIXTURE_SHORT_DATA_LIMIT",
    "FIXTURE_TDMA_FRAME_SIZE",
    "LOWER_CHANNEL",
    "LogicalChannelSlot",
    "OFFICIAL_DEFAULT_SELECTION_INTERVAL_PHYSICAL_SLOTS",
    "STAGE4_BBSC_BOOTSTRAP_V1",
    "STAGE4_BOOTSTRAP_ID",
    "STAGE4_LINK_ID",
    "STAGE4_TBB_FIXTURE_ID",
    "SlotFunction",
    "SlotMap",
    "TBB_SEMANTIC_SCHEMA",
    "TBB_START_TIME_EPOCH_SECONDS",
    "UPPER_CHANNEL",
    "VALIDITY_UNIT_NANOSECONDS",
    "WIDEBAND_BOOTSTRAP_ID",
    "WIDEBAND_DEPLOYMENT_SCHEMA",
    "WIDEBAND_FIXTURE_ID",
    "WIDEBAND_LOWER_CHANNEL",
    "WIDEBAND_PHYSICAL_CHANNEL_BY_INDEX",
    "WIDEBAND_TBB_SEMANTIC_SCHEMA",
    "WIDEBAND_UPPER_CHANNEL",
    "build_bootstrap_slot_map",
    "build_canonical_tbb_profile",
    "build_wideband_bulletin_board",
    "build_wideband_canonical_tbb_profile",
    "build_fixture_bulletin_board",
    "received_tbb_fingerprint",
    "slot_map_from_bulletin_board",
    "tbb_semantic_fingerprint",
    "tbb_semantic_value",
)
