"""MAC 계층 내부 model (`stage0_baseline.md` §4.1.1, §8; `stage4_mac_baseline.md` §6).

공개 경계 DTO는 `common.contracts.mac`과 `common.contracts.mac_execution`에 있다
(decision record `DR-STAGE0-001`, `DR-STAGE4-MAC-001`).

Stage 4 runtime 상태는 작고 유한한 immutable snapshot이며 memory에만 둔다. profile이나
config에 쓰지 않고 endpoint 재시작 시 복원하지 않는다. `MacStateRepository.commit()`은
검증이 끝난 snapshot reference를 한 번 교체하는 all-or-nothing operation이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from ..common.addressing import RouteAddresses
from ..common.contracts.mac_execution import (
    AckReplayContext,
    MacCommandEnvelope,
    RxDeliveryProcedure,
    TimerPurpose,
)
from ..common.contracts.service import TransferState
from ..common.exceptions import InvalidValueError, VdeterInternalError
from ..common.identifiers import (
    LinkId,
    PhysicalChannel,
    SessionHandle,
    StationId,
    TransferId,
)
from ..common.time import TimePoint
from .tbb import CanonicalStage4TbbProfile, SlotMap

# --- Stage 0 호환 model (동결 stub이 계속 사용한다) ---


@dataclass(frozen=True, slots=True)
class MacPlanningDefaults:
    """설정으로 주입되는 계획 기본값. 규범 profile 값이 아니다."""

    link_id: LinkId
    tx_channel: PhysicalChannel
    rx_channel: PhysicalChannel
    transmit_lead_nanoseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("MacPlanningDefaults.link_id: LinkId여야 합니다")
        for name in ("tx_channel", "rx_channel"):
            if not isinstance(getattr(self, name), PhysicalChannel):
                raise InvalidValueError(f"MacPlanningDefaults.{name}: PhysicalChannel이어야 합니다")
        value = self.transmit_lead_nanoseconds
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidValueError(
                "MacPlanningDefaults.transmit_lead_nanoseconds: 0 이상의 정수여야 합니다"
            )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """활성 Data Session의 runtime 상태 snapshot. wire field가 아니다."""

    session_handle: SessionHandle
    transfer_id: TransferId | None
    opened_at: TimePoint
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("SessionRecord.session_handle: SessionHandle이어야 합니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("SessionRecord.transfer_id: TransferId여야 합니다")
        if not isinstance(self.opened_at, TimePoint):
            raise InvalidValueError("SessionRecord.opened_at: TimePoint여야 합니다")
        if not isinstance(self.active, bool):
            raise InvalidValueError("SessionRecord.active: bool이어야 합니다")

    def closed(self) -> "SessionRecord":
        return SessionRecord(
            session_handle=self.session_handle,
            transfer_id=self.transfer_id,
            opened_at=self.opened_at,
            active=False,
        )


# --- Stage 4 단순 유한 상한 (`DR-STAGE4-MAC-012`) ---


class Stage4Limits:
    """PoC 상한. 초과는 축출이 아니라 구조화 거부로 처리한다."""

    LOCAL_TRANSFER_FIFO = 16
    INGRESS_EVENTS = 64
    COMMAND_OUTBOX = 64
    COMMAND_BATCH = 64
    PENDING_DEPENDENCY_TOTAL = 96
    PENDING_DEPENDENCY_RX = 68
    PENDING_DEPENDENCY_NON_CLEANUP_NON_RX = 20
    PENDING_DEPENDENCY_CLEANUP_RESERVE = 8
    ACTIVE_TIMERS = 32
    RECENT_TERMINAL_IDS = 256
    RX_DELIVERY_LEDGER = 32
    OUTSTANDING_RESOURCE_REQUEST_PER_MASS = 1
    ACTIVE_DATA_SESSION = 1
    MAX_RESOURCE_REQUEST_ATTEMPTS = 3
    MAX_SHORT_DATA_RETRANSMISSION = 3
    MAX_FRAGMENT_RETRANSMISSION = 3
    ACK_LEDGER_FRAMES = 3

    #: Radio scheduling horizon 밖이라 대기 중인 송신 의도 상한.
    SCHEDULED_TX_INTENTS = 32

    #: current/next frame에서 horizon 진입을 기다리는 RX window key 상한.
    DEFERRED_RX_WINDOWS = 68

    #: one-frame lookahead의 정상 RX pending 최대치.
    RX_WINDOWS_IDLE_MASS = 20
    RX_WINDOWS_IDLE_ROC = 19
    RX_WINDOWS_ACTIVE_MASS = 34
    RX_WINDOWS_ACTIVE_ROC = 33


class PendingCommandClass(str, Enum):
    """96 pending dependency quota의 세 class."""

    RX = "RX"
    NON_CLEANUP_NON_RX = "NON_CLEANUP_NON_RX"
    CLEANUP = "CLEANUP"


class EndpointLifecycle(str, Enum):
    INITIAL = "INITIAL"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class LcState(str, Enum):
    """`FREE -> RESERVED -> ACTIVE -> FREE` (`DR-STAGE4-MAC-009`)."""

    FREE = "FREE"
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    EXTERNAL_RESERVED = "EXTERNAL_RESERVED"


class TransferRole(str, Enum):
    LOCAL_SHORT_DATA = "LOCAL_SHORT_DATA"
    LOCAL_DATA_SESSION = "LOCAL_DATA_SESSION"
    RELAY_SECOND_HOP = "RELAY_SECOND_HOP"


class TransferPhase(str, Enum):
    """MAC 내부 진행 단계. 공개 `TransferState`와 1:1이 아니다."""

    QUEUED = "QUEUED"
    AWAITING_RESOURCE = "AWAITING_RESOURCE"
    BUILDING = "BUILDING"
    TRANSMITTING = "TRANSMITTING"
    WAITING_ACK = "WAITING_ACK"
    TERMINAL = "TERMINAL"


class DeliveryRecordState(str, Enum):
    PENDING_DELIVERY = "PENDING_DELIVERY"
    LIVE = "LIVE"


class AckPendingState(str, Enum):
    ACK_NONE = "ACK_NONE"
    ACK_PENDING = "ACK_PENDING"
    ACK_SENT = "ACK_SENT"


# --- immutable map helper (tuple-of-pairs) ---


def map_get(entries: tuple, key, default=None):
    for entry_key, value in entries:
        if entry_key == key:
            return value
    return default


def map_put(entries: tuple, key, value) -> tuple:
    """key가 있으면 자리를 유지하며 교체하고, 없으면 끝에 추가한다."""

    for index, (entry_key, _) in enumerate(entries):
        if entry_key == key:
            return entries[:index] + ((key, value),) + entries[index + 1 :]
    return entries + ((key, value),)


def map_remove(entries: tuple, key) -> tuple:
    return tuple(entry for entry in entries if entry[0] != key)


def map_has(entries: tuple, key) -> bool:
    return any(entry_key == key for entry_key, _ in entries)


# --- Stage 4 runtime record ---


@dataclass(frozen=True, slots=True)
class ActiveTbb:
    """활성 TBB snapshot 하나. Link 후보 cache를 중복하지 않는다."""

    fingerprint: str
    bulletin_board_version: int
    source_id: StationId
    control_station_id: int
    valid_from_nanoseconds: int
    valid_until_nanoseconds: int
    activation_nanoseconds: int
    slot_map: SlotMap

    def is_active_at(self, utc_nanoseconds: int) -> bool:
        return (
            self.activation_nanoseconds <= utc_nanoseconds < self.valid_until_nanoseconds
        )


@dataclass(frozen=True, slots=True)
class TbbCandidateContext:
    """MAC이 보존하는 단일 재획득 candidate context."""

    source_id_value: int
    control_station_id: int
    bulletin_board_version: int
    timer_id: str
    deadline_frame_ordinal: int
    first_fragment_frame_ordinal: int

    @property
    def key(self) -> tuple:
        return (self.source_id_value, self.control_station_id, self.bulletin_board_version)


@dataclass(frozen=True, slots=True)
class TbbBroadcastState:
    """ROC의 BBSC 순환 방송 진행 상태."""

    next_fragment_index: int = 0
    fragment_count: int = 0
    pending_command_id: str | None = None
    encoded_packets: tuple = ()
    certificate_fingerprint: bytes | None = None


@dataclass(frozen=True, slots=True)
class Type0Lease:
    """endpoint-local 공통 monotonic lease (`DR-STAGE4-MAC-005`)."""

    source_id_value: int
    media_access_priority_level: int
    selection_interval: int
    short_data_limit: int
    system_status: int
    started_monotonic_nanoseconds: int
    lease_seconds: int
    timer_id: str

    @property
    def deadline_monotonic_nanoseconds(self) -> int:
        return self.started_monotonic_nanoseconds + self.lease_seconds * 1_000_000_000

    @property
    def canonical_fields(self) -> tuple:
        return (
            self.media_access_priority_level,
            self.selection_interval,
            self.short_data_limit,
            self.system_status,
        )


@dataclass(frozen=True, slots=True)
class LcAllocation:
    """station·session별 use map 항목. 현재 frame과 다음 세 frame까지 유지한다."""

    logical_channel_tx: int
    logical_channel_rx: int
    owner_station_id_value: int
    peer_station_id_value: int
    state: LcState
    activation_frame_ordinal: int
    last_valid_frame_ordinal: int
    tdma_channel: int
    issued_by_local: bool

    @property
    def key(self) -> tuple:
        return (self.logical_channel_tx, self.logical_channel_rx, self.owner_station_id_value)


@dataclass(frozen=True, slots=True)
class OutstandingResourceRequest:
    """MASS 송신-side outstanding Type 90 context."""

    transfer_id: TransferId
    attempt_count: int
    timer_id: str | None
    route: RouteAddresses
    terminal_capabilities: int
    priority: int
    physical_channel_name: str
    last_tx_command_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceRequestCandidate:
    """닫힌 epoch 안의 MASS별 후보 record 하나."""

    node_source_id_value: int
    received_at_nanoseconds: int
    physical_slot_index: int
    route: RouteAddresses
    terminal_capabilities: int
    priority: int
    duplicate_count: int = 1
    conflicting: bool = False

    @property
    def order_key(self) -> tuple:
        return (
            self.received_at_nanoseconds,
            self.physical_slot_index,
            self.node_source_id_value,
        )


@dataclass(frozen=True, slots=True)
class ResourceRequestEpoch:
    """ROC의 frame 단위 request epoch (`DR-STAGE4-MAC-007`)."""

    minute_start_nanoseconds: int
    tdma_frame_index: int
    candidates: tuple[ResourceRequestCandidate, ...] = ()
    open_rx_command_ids: tuple[str, ...] = ()
    closed: bool = False

    @property
    def key(self) -> tuple:
        return (self.minute_start_nanoseconds, self.tdma_frame_index)


@dataclass(frozen=True, slots=True)
class RequestChain:
    """ROC 수신-side request chain. MASS별로 분리한다."""

    node_source_id_value: int
    route: RouteAddresses
    terminal_capabilities: int
    priority: int
    logical_channel_tx: int
    logical_channel_rx: int
    activation_frame: int
    activation_resync_used: bool = False
    data_observed: bool = False


@dataclass(frozen=True, slots=True)
class TransferContext:
    """송신 transfer 하나의 MAC 진행 상태."""

    transfer_id: TransferId
    role: TransferRole
    phase: TransferPhase
    route: RouteAddresses
    ack_required: bool
    presentation_size: int
    public_state: TransferState
    session_handle: SessionHandle | None = None
    link_id: LinkId | None = None
    physical_channel_name: str | None = None
    retransmission_number: int = 0
    session_frame_index: int = 0
    logical_channel_tx: int | None = None
    logical_channel_rx: int | None = None
    ack_timer_id: str | None = None
    pending_packet_ids: tuple[str, ...] = ()
    frame_ledger: tuple = ()
    absent_ack_frames: int = 0
    total_fragments: int = 0
    next_fragment_index: int = 0
    current_frame_fragment_count: int = 0
    building_frame_packet_ids: tuple[str, ...] = ()
    retry_fragment_keys: tuple[tuple[int, int], ...] = ()
    relay_source_transfer_id: str | None = None
    first_tx_accepted: bool = False


@dataclass(frozen=True, slots=True)
class RxFrameReception:
    """수신 Data Session 최근 frame의 position 관측 상태."""

    session_frame_index: int
    frame_fragment_count: int
    received_positions: tuple[int, ...] = ()
    terminal_observed: bool = False

    @property
    def complete(self) -> bool:
        return self.received_positions == tuple(
            range(1, self.frame_fragment_count + 1)
        )


@dataclass(frozen=True, slots=True)
class RxSessionContext:
    """수신 Data Session context. Link `OPEN` 성공 뒤에만 fragment를 넘긴다."""

    session_handle: SessionHandle
    route: RouteAddresses
    peer_station_id_value: int
    logical_channel_rx: int
    opened: bool = False
    open_command_id: str | None = None
    session_frame_index: int = 0
    last_frame_ordinal: int = 0
    pending_packet_id: object = None
    pending_link_id: object = None
    pending_received_at: object = None
    pending_message: object = None
    seen_packet_id_values: tuple[str, ...] = ()
    seen_fragment_keys: tuple[tuple, ...] = ()
    frame_receptions: tuple[RxFrameReception, ...] = ()


@dataclass(frozen=True, slots=True)
class RelayRecord:
    """ROC bounded relay FIFO의 한 항목."""

    relay_transfer_id: TransferId
    original_route: RouteAddresses
    second_hop_route: RouteAddresses
    presentation_data: bytes
    analyze_command_id: str | None = None
    admitted: bool = False
    first_hop_ack_completed: bool = False
    first_hop_ledger_key: bytes | None = None
    requirement: object = None
    second_hop_started: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryLedgerRecord:
    """`RecentRxDeliveryLedger`의 한 record (`DR-STAGE4-MAC-010`)."""

    key: bytes
    procedure: RxDeliveryProcedure
    state: DeliveryRecordState
    payload_length: int
    payload_digest: bytes
    first_received_frame_ordinal: int
    completion_frame_ordinal: int | None = None
    expires_after_frame: int | None = None
    highest_retransmission_number: int | None = None
    ack_state: AckPendingState = AckPendingState.ACK_NONE
    replay_context: AckReplayContext | None = None
    replay_command_id: str | None = None
    last_replay_scheduled_frame: int | None = None
    pending_delivery_key: tuple | None = None

    def order_key(self) -> tuple:
        anchor = (
            self.first_received_frame_ordinal
            if self.state is DeliveryRecordState.PENDING_DELIVERY
            else self.completion_frame_ordinal
        )
        return (anchor if anchor is not None else 0, self.key)


@dataclass(frozen=True, slots=True)
class ScheduledTxIntent:
    """Radio scheduling horizon 밖이라 아직 command로 내보내지 않은 송신 의도.

    slot 선택은 이미 끝났고 protocol 가시 동작(선택한 slot, 난수, SI 의미)은 바뀌지 않는다.
    horizon 안에 들어오면 그대로 build/encode command를 만든다.
    """

    intent_id: str
    kind: str
    transmit_at_nanoseconds: int
    channel_name: str
    correlation_transfer_id: str | None = None
    build_request: object = None
    encode_request: object = None
    ledger_key: bytes | None = None
    resource_chain_station_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ("BUILD", "ENCODE"):
            raise InvalidValueError("ScheduledTxIntent.kind는 BUILD 또는 ENCODE여야 합니다")
        if (self.build_request is None) == (self.encode_request is None):
            raise InvalidValueError("ScheduledTxIntent: build/encode request 중 정확히 하나가 필요합니다")
        if (
            not isinstance(self.resource_chain_station_ids, tuple)
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item <= 0
                for item in self.resource_chain_station_ids
            )
            or len(set(self.resource_chain_station_ids))
            != len(self.resource_chain_station_ids)
        ):
            raise InvalidValueError(
                "ScheduledTxIntent.resource_chain_station_ids: unique positive int tuple이어야 합니다"
            )


@dataclass(frozen=True, slots=True)
class PendingCommandRecord:
    """실행 중인 dependency command 하나."""

    command_id: str
    command_class: PendingCommandClass
    payload_kind: str
    accepted: bool = False
    request_key: tuple = ()
    held_result: object = None
    held_terminal: object = None


@dataclass(frozen=True, slots=True)
class TimerRecord:
    timer_id: str
    purpose: TimerPurpose
    deadline_monotonic_nanoseconds: int


@dataclass(frozen=True, slots=True)
class ShortDataFrameCounter:
    """`(physical_channel, minute_start_utc.nanoseconds, tdma_frame_index)` 별 RAC 송신 수."""

    physical_channel_name: str
    minute_start_nanoseconds: int
    tdma_frame_index: int
    count: int

    @property
    def key(self) -> tuple:
        return (
            self.physical_channel_name,
            self.minute_start_nanoseconds,
            self.tdma_frame_index,
        )


@dataclass(frozen=True, slots=True)
class MacState:
    """검증이 끝난 immutable MAC 상태 snapshot.

    모든 collection은 tuple이므로 snapshot 사이에 공유되는 mutable 구조가 없다.
    """

    lifecycle: EndpointLifecycle = EndpointLifecycle.INITIAL
    active_tbb: ActiveTbb | None = None
    tbb_candidate: TbbCandidateContext | None = None
    tbb_broadcast: TbbBroadcastState = field(default_factory=TbbBroadcastState)
    tbb_expiry_timer_id: str | None = None
    type0_lease: Type0Lease | None = None
    type0_transmitted: bool = False
    type0_tx_command_id: str | None = None
    lc_allocations: tuple[LcAllocation, ...] = ()
    outstanding_request: OutstandingResourceRequest | None = None
    request_epochs: tuple[ResourceRequestEpoch, ...] = ()
    request_chains: tuple[RequestChain, ...] = ()
    transfers: tuple[TransferContext, ...] = ()
    local_fifo: tuple[str, ...] = ()
    relay_records: tuple[RelayRecord, ...] = ()
    rx_sessions: tuple[RxSessionContext, ...] = ()
    delivery_ledger: tuple[DeliveryLedgerRecord, ...] = ()
    pending_commands: tuple[PendingCommandRecord, ...] = ()
    timers: tuple[TimerRecord, ...] = ()
    recent_terminal_ids: tuple[str, ...] = ()
    short_data_counters: tuple[ShortDataFrameCounter, ...] = ()
    planned_rx_windows: tuple[tuple, ...] = ()
    planned_frame_ordinals: tuple[int, ...] = ()
    seeded: bool = False
    stop_drain_timer_id: str | None = None
    stop_cleanup_commands: tuple[MacCommandEnvelope, ...] = ()
    delivery_pending: tuple[tuple, ...] = ()
    tx_intents: tuple[ScheduledTxIntent, ...] = ()
    deferred_rx_windows: tuple[tuple, ...] = ()

    def evolve(self, **changes) -> "MacState":
        """검증이 끝난 새 snapshot을 만든다. 현재 snapshot은 바꾸지 않는다."""

        return replace(self, **changes)

    # --- 조회 helper ---

    def transfer(self, transfer_id: TransferId) -> TransferContext | None:
        for item in self.transfers:
            if item.transfer_id.value == transfer_id.value:
                return item
        return None

    def transfer_by_value(self, transfer_id_value: str) -> TransferContext | None:
        for item in self.transfers:
            if item.transfer_id.value == transfer_id_value:
                return item
        return None

    def with_transfer(self, context: TransferContext) -> "MacState":
        replaced = False
        items = []
        for item in self.transfers:
            if item.transfer_id.value == context.transfer_id.value:
                items.append(context)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(context)
        return self.evolve(transfers=tuple(items))

    def without_transfer(self, transfer_id: TransferId) -> "MacState":
        return self.evolve(
            transfers=tuple(
                item for item in self.transfers if item.transfer_id.value != transfer_id.value
            )
        )

    def pending_command(self, command_id: str) -> PendingCommandRecord | None:
        for item in self.pending_commands:
            if item.command_id == command_id:
                return item
        return None

    def with_pending_command(self, record: PendingCommandRecord) -> "MacState":
        replaced = False
        items = []
        for item in self.pending_commands:
            if item.command_id == record.command_id:
                items.append(record)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(record)
        return self.evolve(pending_commands=tuple(items))

    def without_pending_command(self, command_id: str) -> "MacState":
        return self.evolve(
            pending_commands=tuple(
                item for item in self.pending_commands if item.command_id != command_id
            )
        )

    def pending_count(self, command_class: PendingCommandClass) -> int:
        return sum(1 for item in self.pending_commands if item.command_class is command_class)

    def timer(self, timer_id: str) -> TimerRecord | None:
        for item in self.timers:
            if item.timer_id == timer_id:
                return item
        return None

    def with_timer(self, record: TimerRecord) -> "MacState":
        replaced = False
        items = []
        for item in self.timers:
            if item.timer_id == record.timer_id:
                items.append(record)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(record)
        return self.evolve(timers=tuple(items))

    def without_timer(self, timer_id: str) -> "MacState":
        return self.evolve(
            timers=tuple(item for item in self.timers if item.timer_id != timer_id)
        )

    def request_chain(self, node_source_id_value: int) -> RequestChain | None:
        for item in self.request_chains:
            if item.node_source_id_value == node_source_id_value:
                return item
        return None

    def with_request_chain(self, chain: RequestChain) -> "MacState":
        replaced = False
        items = []
        for item in self.request_chains:
            if item.node_source_id_value == chain.node_source_id_value:
                items.append(chain)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(chain)
        return self.evolve(request_chains=tuple(items))

    def without_request_chain(self, node_source_id_value: int) -> "MacState":
        return self.evolve(
            request_chains=tuple(
                item
                for item in self.request_chains
                if item.node_source_id_value != node_source_id_value
            )
        )

    def epoch(self, key: tuple) -> ResourceRequestEpoch | None:
        for item in self.request_epochs:
            if item.key == key:
                return item
        return None

    def with_epoch(self, epoch: ResourceRequestEpoch) -> "MacState":
        replaced = False
        items = []
        for item in self.request_epochs:
            if item.key == epoch.key:
                items.append(epoch)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(epoch)
        return self.evolve(request_epochs=tuple(items))

    def ledger_record(self, key: bytes) -> DeliveryLedgerRecord | None:
        for item in self.delivery_ledger:
            if item.key == key:
                return item
        return None

    def with_ledger_record(self, record: DeliveryLedgerRecord) -> "MacState":
        replaced = False
        items = []
        for item in self.delivery_ledger:
            if item.key == record.key:
                items.append(record)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(record)
        return self.evolve(delivery_ledger=tuple(items))

    def without_ledger_record(self, key: bytes) -> "MacState":
        return self.evolve(
            delivery_ledger=tuple(item for item in self.delivery_ledger if item.key != key)
        )

    def rx_session(self, session_handle_value: str) -> RxSessionContext | None:
        for item in self.rx_sessions:
            if item.session_handle.value == session_handle_value:
                return item
        return None

    def rx_session_for_channel(self, logical_channel_rx: int) -> RxSessionContext | None:
        for item in self.rx_sessions:
            if item.logical_channel_rx == logical_channel_rx:
                return item
        return None

    def with_rx_session(self, context: RxSessionContext) -> "MacState":
        replaced = False
        items = []
        for item in self.rx_sessions:
            if item.session_handle.value == context.session_handle.value:
                items.append(context)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(context)
        return self.evolve(rx_sessions=tuple(items))

    def without_rx_session(self, session_handle_value: str) -> "MacState":
        return self.evolve(
            rx_sessions=tuple(
                item
                for item in self.rx_sessions
                if item.session_handle.value != session_handle_value
            )
        )

    def relay_record(self, relay_transfer_id_value: str) -> RelayRecord | None:
        for item in self.relay_records:
            if item.relay_transfer_id.value == relay_transfer_id_value:
                return item
        return None

    def with_relay_record(self, record: RelayRecord) -> "MacState":
        replaced = False
        items = []
        for item in self.relay_records:
            if item.relay_transfer_id.value == record.relay_transfer_id.value:
                items.append(record)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(record)
        return self.evolve(relay_records=tuple(items))

    def without_relay_record(self, relay_transfer_id_value: str) -> "MacState":
        return self.evolve(
            relay_records=tuple(
                item
                for item in self.relay_records
                if item.relay_transfer_id.value != relay_transfer_id_value
            )
        )

    def active_data_session_count(self) -> int:
        return sum(
            1
            for item in self.transfers
            if item.role is not TransferRole.LOCAL_SHORT_DATA
            and item.phase is not TransferPhase.TERMINAL
        ) + len(self.rx_sessions)

    def with_tx_intent(self, intent: ScheduledTxIntent) -> "MacState":
        return self.evolve(tx_intents=self.tx_intents + (intent,))

    def without_tx_intent(self, intent_id: str) -> "MacState":
        return self.evolve(
            tx_intents=tuple(item for item in self.tx_intents if item.intent_id != intent_id)
        )

    def with_deferred_rx_window(self, key: tuple) -> "MacState":
        if key in self.deferred_rx_windows:
            return self
        if len(self.deferred_rx_windows) >= Stage4Limits.DEFERRED_RX_WINDOWS:
            return self
        return self.evolve(deferred_rx_windows=self.deferred_rx_windows + (key,))

    def without_deferred_rx_window(self, key: tuple) -> "MacState":
        return self.evolve(
            deferred_rx_windows=tuple(
                item for item in self.deferred_rx_windows if item != key
            )
        )

    def with_recent_terminal(self, identifier: str) -> "MacState":
        if identifier in self.recent_terminal_ids:
            return self
        items = self.recent_terminal_ids + (identifier,)
        if len(items) > Stage4Limits.RECENT_TERMINAL_IDS:
            items = items[len(items) - Stage4Limits.RECENT_TERMINAL_IDS :]
        return self.evolve(recent_terminal_ids=items)

    def short_data_counter(self, key: tuple) -> ShortDataFrameCounter | None:
        for item in self.short_data_counters:
            if item.key == key:
                return item
        return None

    def with_short_data_counter(self, counter: ShortDataFrameCounter) -> "MacState":
        replaced = False
        items = []
        for item in self.short_data_counters:
            if item.key == counter.key:
                items.append(counter)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(counter)
        # counter는 frame 단위 유한 상태다. 최근 두 frame 분량만 유지한다.
        if len(items) > 8:
            items = sorted(items, key=lambda entry: entry.key)[-8:]
        return self.evolve(short_data_counters=tuple(items))


INITIAL_MAC_STATE = MacState()


class MacStateRepository:
    """immutable snapshot 하나만 보유하는 all-or-nothing state repository.

    `commit(next_state)`은 검증이 끝난 snapshot reference를 한 번 교체한다. 교체 전 validation
    실패나 주입 예외는 기존 state를 그대로 남기고, 교체 뒤에는 예외를 내지 않는다.
    """

    __slots__ = ("_state", "_commit_hook", "_commit_count")

    def __init__(self, state: MacState | None = None) -> None:
        self._state = state if state is not None else INITIAL_MAC_STATE
        if not isinstance(self._state, MacState):
            raise InvalidValueError("MacStateRepository: MacState가 필요합니다")
        self._commit_hook = None
        self._commit_count = 0

    @property
    def state(self) -> MacState:
        return self._state

    @property
    def commit_count(self) -> int:
        return self._commit_count

    def set_commit_hook(self, hook) -> None:
        """시험이 commit 실패를 주입하는 경로. 운영 wiring은 사용하지 않는다."""

        self._commit_hook = hook

    def commit(self, next_state: MacState) -> None:
        if not isinstance(next_state, MacState):
            raise InvalidValueError("MacStateRepository.commit: MacState가 필요합니다")
        hook = self._commit_hook
        if hook is not None:
            # 교체 전에 실행한다. hook 예외는 기존 state를 그대로 남긴다.
            hook(next_state)
        self._state = next_state
        self._commit_count += 1

    def replace_for_restart(self, state: MacState) -> None:
        if not isinstance(state, MacState):
            raise VdeterInternalError("MacStateRepository.replace_for_restart: MacState가 필요합니다")
        self._state = state
        self._commit_hook = None


@dataclass(frozen=True, slots=True)
class MacRuntimeProfile:
    """endpoint 조립 시 주입하는 Stage 4 실행 profile."""

    canonical_tbb: CanonicalStage4TbbProfile
    deployment_fingerprint: str
    local_station_id: StationId
    is_control: bool
    transmit_lead_nanoseconds: int
    receive_lead_nanoseconds: int
    channel_switch_gap_nanoseconds: int
    sample_rate_hz: int
    #: Radio가 수락하는 가장 먼 미래 송수신 시각까지의 거리. 0이면 상한이 없다.
    schedule_horizon_nanoseconds: int = 0
    #: `DR-STAGE6-MR-001` §13.2.2: 할당 DC/DSCH의 immutable Link ID. 기본 11은 legacy와
    #: byte-exact 동일 경로다. BBSC/RAC/ASC/Short Data 제어는 항상 Link 11이다.
    data_link_id: int = 11
    #: 이 배치의 lower/upper Physical Channel alias. 기본값은 동결 Stage 4 25 kHz alias다.
    lower_channel: PhysicalChannel = PhysicalChannel("VDETER_LOWER_25_TEST")
    upper_channel: PhysicalChannel = PhysicalChannel("VDETER_UPPER_25_TEST")

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_tbb, CanonicalStage4TbbProfile):
            raise InvalidValueError("MacRuntimeProfile.canonical_tbb가 잘못되었습니다")
        if not isinstance(self.local_station_id, StationId):
            raise InvalidValueError("MacRuntimeProfile.local_station_id가 잘못되었습니다")
        if not isinstance(self.is_control, bool):
            raise InvalidValueError("MacRuntimeProfile.is_control: bool이어야 합니다")
        for name in (
            "transmit_lead_nanoseconds",
            "receive_lead_nanoseconds",
            "channel_switch_gap_nanoseconds",
            "schedule_horizon_nanoseconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidValueError(f"MacRuntimeProfile.{name}: 0 이상의 정수여야 합니다")
        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or self.sample_rate_hz <= 0
        ):
            raise InvalidValueError("MacRuntimeProfile.sample_rate_hz: 양의 정수여야 합니다")
        if self.data_link_id not in (11, 17, 19):
            raise InvalidValueError("MacRuntimeProfile.data_link_id: 11, 17 또는 19여야 합니다")
        for name in ("lower_channel", "upper_channel"):
            if not isinstance(getattr(self, name), PhysicalChannel):
                raise InvalidValueError(f"MacRuntimeProfile.{name}: PhysicalChannel이어야 합니다")
        if self.lower_channel.name == self.upper_channel.name:
            raise InvalidValueError("MacRuntimeProfile: lower/upper alias가 같을 수 없습니다")


__all__ = (
    "ActiveTbb",
    "AckPendingState",
    "DeliveryLedgerRecord",
    "DeliveryRecordState",
    "EndpointLifecycle",
    "INITIAL_MAC_STATE",
    "LcAllocation",
    "LcState",
    "MacPlanningDefaults",
    "MacRuntimeProfile",
    "MacState",
    "MacStateRepository",
    "OutstandingResourceRequest",
    "PendingCommandClass",
    "PendingCommandRecord",
    "RelayRecord",
    "RequestChain",
    "ResourceRequestCandidate",
    "ResourceRequestEpoch",
    "RxSessionContext",
    "ScheduledTxIntent",
    "SessionRecord",
    "ShortDataFrameCounter",
    "Stage4Limits",
    "TbbBroadcastState",
    "TbbCandidateContext",
    "TimerRecord",
    "TransferContext",
    "TransferPhase",
    "TransferRole",
    "Type0Lease",
    "map_get",
    "map_has",
    "map_put",
    "map_remove",
)
