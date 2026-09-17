"""Type 90/4 자원 절차와 Duplex Logical Channel 수명주기.

`DR-STAGE4-MAC-007`(요청·할당), `008`(Duplex LC와 registry), `009`(기본 LC 수명주기)를
구현한다. MAC은 Link ID profile 값을 사설 표로 복제하지 않고 registry에 질의한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..common.addressing import (
    Address,
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from ..common.contracts.mac_execution import MacFailureReason, MacReasonCode
from ..common.contracts.message import ResourceAllocationBody, ResourceRequestBody
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import LinkId, PhysicalChannel, StationId
from ..common.results import Ok, internal_error
from .models import LcAllocation, LcState
from .tbb import SlotFunction, SlotMap

#: 완료 profile이 지원하는 `terminal_capabilities`: M.2092 version 2, simplified bit 0.
SUPPORTED_TERMINAL_CAPABILITIES = 2
TERMINAL_CAPABILITY_VERSION_MASK = 0x0F
TERMINAL_CAPABILITY_SIMPLIFIED_BIT = 0x10

#: canonical 거부 Type 4.
REJECTION_LOGICAL_CHANNEL = 255
CANONICAL_TDMA_FRAME_DELAY = 1
CANONICAL_SESSION_ID = 0
CANONICAL_CQI = 0
CANONICAL_PRIORITY = 0

#: Type 4가 활성화한 LC pair를 use map에 유지하는 frame 수 (현재 frame + 다음 세 frame).
LC_LEASE_FRAMES = 3

#: Stage 4 Duplex pair를 만드는 TDMA channel 범위.
DUPLEX_TDMA_CHANNELS = (1, 2, 3, 4, 5)


class Direction(str, Enum):
    """Data Session 송신 방향."""

    SHIP_TO_ROC = "SHIP_TO_ROC"
    ROC_TO_SHIP = "ROC_TO_SHIP"


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.MAC,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class DuplexPair:
    """방향별 `(Tx, Rx)` Logical Channel 쌍과 그 Physical Channel."""

    tdma_channel: int
    logical_channel_tx: int
    logical_channel_rx: int
    tx_physical_channel: PhysicalChannel
    rx_physical_channel: PhysicalChannel

    @property
    def logical_channels(self) -> tuple[int, int]:
        return (self.logical_channel_tx, self.logical_channel_rx)


def _slot_of(
    slot_map: SlotMap,
    function: SlotFunction,
    tdma_channel: int,
    physical_channel: PhysicalChannel,
):
    for slot in slot_map.slots:
        if (
            slot.function is function
            and slot.tdma_channel == tdma_channel
            and slot.physical_channel.name == physical_channel.name
        ):
            return slot
    return None


def duplex_pair(
    slot_map: SlotMap,
    direction: Direction,
    tdma_channel: int,
    *,
    lower: PhysicalChannel,
    upper: PhysicalChannel,
) -> DuplexPair | None:
    """`Tx`는 DC, `Rx`는 Type 13·renewal Type 4를 받는 DSCH다.

    Ship→ROC는 `(Tx=lower DC, Rx=upper DSCH)`, ROC→Ship은 `(Tx=upper DC, Rx=lower DSCH)`다.
    두 LC는 같은 TDMA channel에 속하고 Duplex의 서로 다른 upper/lower PC를 사용한다.
    """

    if direction is Direction.SHIP_TO_ROC:
        tx_channel, rx_channel = lower, upper
    else:
        tx_channel, rx_channel = upper, lower
    tx_slot = _slot_of(slot_map, SlotFunction.DC, tdma_channel, tx_channel)
    rx_slot = _slot_of(slot_map, SlotFunction.DSCH, tdma_channel, rx_channel)
    if tx_slot is None or rx_slot is None:
        return None
    return DuplexPair(
        tdma_channel=tdma_channel,
        logical_channel_tx=tx_slot.logical_channel,
        logical_channel_rx=rx_slot.logical_channel,
        tx_physical_channel=tx_channel,
        rx_physical_channel=rx_channel,
    )


def _occupied_logical_channels(
    allocations: tuple[LcAllocation, ...], frame_ordinal: int
) -> frozenset:
    occupied = set()
    for allocation in allocations:
        if allocation.last_valid_frame_ordinal < frame_ordinal:
            continue
        if allocation.state is LcState.FREE:
            continue
        occupied.add(allocation.logical_channel_tx)
        occupied.add(allocation.logical_channel_rx)
    return frozenset(occupied)


def select_free_duplex_pair(
    *,
    slot_map: SlotMap,
    direction: Direction,
    allocations: tuple[LcAllocation, ...],
    frame_ordinal: int,
    lower: PhysicalChannel,
    upper: PhysicalChannel,
) -> DuplexPair | None:
    """두 LC가 모두 `FREE`인 후보를 TDMA channel 1..5 오름차순으로 검사한다.

    난수나 collection iteration 순서로 pair를 고르지 않는다.
    """

    occupied = _occupied_logical_channels(allocations, frame_ordinal)
    for tdma_channel in DUPLEX_TDMA_CHANNELS:
        pair = duplex_pair(slot_map, direction, tdma_channel, lower=lower, upper=upper)
        if pair is None:
            continue
        if pair.logical_channel_tx in occupied or pair.logical_channel_rx in occupied:
            continue
        return pair
    return None


def prune_expired_allocations(
    allocations: tuple[LcAllocation, ...], frame_ordinal: int
) -> tuple[LcAllocation, ...]:
    """세 frame 동안 renewal이 없으면 `FREE`로 전환한다."""

    return tuple(
        item for item in allocations if item.last_valid_frame_ordinal >= frame_ordinal
    )


def allocation_state_at(allocation: LcAllocation, frame_ordinal: int) -> LcState:
    """`F+d` 시작에 `ACTIVE`가 되며 그 전에는 데이터를 송신하지 않는다."""

    if allocation.state is LcState.EXTERNAL_RESERVED:
        return LcState.EXTERNAL_RESERVED
    if frame_ordinal < allocation.activation_frame_ordinal:
        return LcState.RESERVED
    if frame_ordinal > allocation.last_valid_frame_ordinal:
        return LcState.FREE
    return LcState.ACTIVE


def upsert_allocation(
    allocations: tuple[LcAllocation, ...], allocation: LcAllocation
) -> tuple[LcAllocation, ...]:
    """같은 ROC·station·LC pair·activation frame의 중복 할당은 idempotent다."""

    items = []
    replaced = False
    for item in allocations:
        if item.key == allocation.key:
            items.append(allocation)
            replaced = True
        else:
            items.append(item)
    if not replaced:
        items.append(allocation)
    return tuple(items)


def remove_allocation(
    allocations: tuple[LcAllocation, ...], key: tuple
) -> tuple[LcAllocation, ...]:
    return tuple(item for item in allocations if item.key != key)


def conflicting_allocation(
    allocations: tuple[LcAllocation, ...],
    pair: tuple[int, int],
    owner_station_id_value: int,
    frame_ordinal: int,
) -> LcAllocation | None:
    """자신의 active/reserved pair와 다른 station 할당이 겹치는지 검사한다."""

    wanted = set(pair)
    for allocation in allocations:
        if (allocation.state is not LcState.EXTERNAL_RESERVED
                and allocation.owner_station_id_value == owner_station_id_value):
            continue
        if allocation.last_valid_frame_ordinal < frame_ordinal:
            continue
        if wanted & {allocation.logical_channel_tx, allocation.logical_channel_rx}:
            return allocation
    return None


def find_allocation_for_rx(
    allocations: tuple[LcAllocation, ...],
    logical_channel_rx: int,
    frame_ordinal: int,
) -> LcAllocation | None:
    for allocation in allocations:
        if allocation.logical_channel_rx != logical_channel_rx:
            continue
        if allocation.last_valid_frame_ordinal < frame_ordinal:
            continue
        return allocation
    return None


def find_allocation_for_tx(
    allocations: tuple[LcAllocation, ...],
    logical_channel_tx: int,
    frame_ordinal: int,
) -> LcAllocation | None:
    for allocation in allocations:
        if allocation.state in (LcState.EXTERNAL_RESERVED, LcState.FREE):
            continue
        if allocation.logical_channel_tx != logical_channel_tx:
            continue
        if allocation.last_valid_frame_ordinal < frame_ordinal:
            continue
        return allocation
    return None


def externally_reserved(allocations, logical_channel: int, frame_ordinal: int) -> bool:
    """Heard reservations block new PoC use; they never authorize transmission.

    Reservations protect the interval before activation as well as the active
    lease. Expired entries do not block even before the next prune tick.
    """
    return any(
        item.state is LcState.EXTERNAL_RESERVED
        and item.last_valid_frame_ordinal >= frame_ordinal
        and logical_channel in (item.logical_channel_tx, item.logical_channel_rx)
        for item in allocations
    )


# --- Type 90 검증 ---


def validate_terminal_capabilities(value: int):
    """version bits가 정확히 2이고 simplified bit가 0인 요청만 지원한다."""

    if value & TERMINAL_CAPABILITY_SIMPLIFIED_BIT:
        return _error(
            ErrorCode.UNSUPPORTED_REQUEST,
            ErrorCategory.UNSUPPORTED,
            "simplified mode terminal capability는 완료 profile에서 지원하지 않습니다",
            mac_reason=MacFailureReason.FEATURE_NOT_SUPPORTED.value,
            reason_code=MacReasonCode.UNSUPPORTED_TERMINAL_CAPABILITY,
            terminal_capabilities=value,
        )
    if (value & TERMINAL_CAPABILITY_VERSION_MASK) != SUPPORTED_TERMINAL_CAPABILITIES:
        return _error(
            ErrorCode.UNSUPPORTED_REQUEST,
            ErrorCategory.UNSUPPORTED,
            "M.2092 version 2 terminal capability만 지원합니다",
            mac_reason=MacFailureReason.FEATURE_NOT_SUPPORTED.value,
            reason_code=MacReasonCode.UNSUPPORTED_TERMINAL_CAPABILITY,
            terminal_capabilities=value,
        )
    return Ok(value)


def build_resource_request_body(
    *,
    original_source: StationId,
    node_source: StationId,
    node_destination: Address,
    original_destination: Address,
) -> ResourceRequestBody:
    """canonical Type 90. `priority=0`, version 2·non-simplified capability."""

    return ResourceRequestBody(
        original_source_id=original_source,
        node_source_id=node_source,
        node_destination=node_destination,
        original_destination=original_destination,
        priority=CANONICAL_PRIORITY,
        terminal_capabilities=SUPPORTED_TERMINAL_CAPABILITIES,
    )


def build_allocation_body(
    *,
    source_id: StationId,
    destination: Address,
    logical_channel_tx: int,
    logical_channel_rx: int,
    link_id: LinkId,
    tdma_frame_delay: int = CANONICAL_TDMA_FRAME_DELAY,
) -> ResourceAllocationBody:
    """최초 정상 Type 4의 canonical 값."""

    return ResourceAllocationBody(
        source_id=source_id,
        destination=destination,
        logical_channel_tx=logical_channel_tx,
        logical_channel_rx=logical_channel_rx,
        link_id=link_id,
        tdma_frame_delay=tdma_frame_delay,
        session_id=CANONICAL_SESSION_ID,
        cqi=CANONICAL_CQI,
    )


def build_rejection_body(
    *, source_id: StationId, destination: Address
) -> ResourceAllocationBody:
    """canonical 거부 `255/255`, Link ID 11, delay 1, session 0, CQI 0."""

    return ResourceAllocationBody(
        source_id=source_id,
        destination=destination,
        logical_channel_tx=REJECTION_LOGICAL_CHANNEL,
        logical_channel_rx=REJECTION_LOGICAL_CHANNEL,
        link_id=LinkId(11),
        tdma_frame_delay=CANONICAL_TDMA_FRAME_DELAY,
        session_id=CANONICAL_SESSION_ID,
        cqi=CANONICAL_CQI,
    )


def is_rejection(body: ResourceAllocationBody) -> bool:
    """수신 시 Stage 1 DTO가 허용하는 다른 합법 잔여값도 거부 의미로 허용한다."""

    return (
        body.logical_channel_tx == REJECTION_LOGICAL_CHANNEL
        and body.logical_channel_rx == REJECTION_LOGICAL_CHANNEL
    )


def reissue_frame_delay(
    *, activation_frame: int, transmit_frame: int, resync_used: bool
):
    """retry Type 4 재발행의 `tdma_frame_delay` (`DR-STAGE4-MAC-007`).

    `A > T`이면 `A-T`로 재발행하고 `A`를 바꾸지 않는다. `A <= T`이고 resync를 아직 쓰지
    않았으면 새 activation을 정확히 `T+1`로 한 번 재설정한다. 이미 resync를 쓴 뒤 다시
    `A <= T`이면 activation을 더 미루지 않는다.
    """

    if activation_frame > transmit_frame:
        delay = activation_frame - transmit_frame
        if delay > 255:
            return None
        return (delay, activation_frame, resync_used)
    if not resync_used:
        return (1, transmit_frame + 1, True)
    return None


def allocation_from_type4(
    *,
    body: ResourceAllocationBody,
    owner_station_id_value: int,
    peer_station_id_value: int,
    transmit_frame_ordinal: int,
    tdma_channel: int,
    issued_by_local: bool,
    external: bool = False,
) -> LcAllocation:
    """관측하거나 발행한 Type 4의 LC pair를 use map 항목으로 만든다."""

    activation = transmit_frame_ordinal + body.tdma_frame_delay
    return LcAllocation(
        logical_channel_tx=body.logical_channel_tx,
        logical_channel_rx=body.logical_channel_rx,
        owner_station_id_value=owner_station_id_value,
        peer_station_id_value=peer_station_id_value,
        state=LcState.EXTERNAL_RESERVED if external else LcState.RESERVED,
        activation_frame_ordinal=activation,
        last_valid_frame_ordinal=activation + LC_LEASE_FRAMES,
        tdma_channel=tdma_channel,
        issued_by_local=issued_by_local,
    )


def renewed_allocation(allocation: LcAllocation, frame_ordinal: int) -> LcAllocation:
    """renewal을 송수신한 시점의 현재 frame과 다음 세 frame까지 내부 예약한다."""

    return LcAllocation(
        logical_channel_tx=allocation.logical_channel_tx,
        logical_channel_rx=allocation.logical_channel_rx,
        owner_station_id_value=allocation.owner_station_id_value,
        peer_station_id_value=allocation.peer_station_id_value,
        state=LcState.ACTIVE,
        activation_frame_ordinal=allocation.activation_frame_ordinal,
        last_valid_frame_ordinal=max(
            allocation.last_valid_frame_ordinal, frame_ordinal + LC_LEASE_FRAMES
        ),
        tdma_channel=allocation.tdma_channel,
        issued_by_local=allocation.issued_by_local,
    )


def data_session_route(
    *,
    original_source: StationId,
    original_destination: Address,
    node_source: StationId,
    node_destination: Address,
) -> RouteAddresses:
    return RouteAddresses(
        original_source=OriginalSource(original_source),
        original_destination=OriginalDestination(original_destination),
        node_source=NodeSource(node_source),
        node_destination=NodeDestination(node_destination),
    )


def one_hop_route(source: StationId, destination: Address) -> RouteAddresses:
    """Short Data의 Node=Original 한-hop route."""

    return RouteAddresses(
        original_source=OriginalSource(source),
        original_destination=OriginalDestination(destination),
        node_source=NodeSource(source),
        node_destination=NodeDestination(destination),
    )


def addressed(station_id: StationId) -> Address:
    return Addressed(station_id)


__all__ = (
    "CANONICAL_CQI",
    "CANONICAL_PRIORITY",
    "CANONICAL_SESSION_ID",
    "CANONICAL_TDMA_FRAME_DELAY",
    "DUPLEX_TDMA_CHANNELS",
    "Direction",
    "DuplexPair",
    "LC_LEASE_FRAMES",
    "REJECTION_LOGICAL_CHANNEL",
    "SUPPORTED_TERMINAL_CAPABILITIES",
    "addressed",
    "allocation_from_type4",
    "allocation_state_at",
    "build_allocation_body",
    "build_rejection_body",
    "build_resource_request_body",
    "conflicting_allocation",
    "data_session_route",
    "duplex_pair",
    "find_allocation_for_rx",
    "find_allocation_for_tx",
    "is_rejection",
    "one_hop_route",
    "prune_expired_allocations",
    "reissue_frame_delay",
    "remove_allocation",
    "renewed_allocation",
    "select_free_duplex_pair",
    "upsert_allocation",
    "validate_terminal_capabilities",
)
