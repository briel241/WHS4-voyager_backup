"""Stage 4 MAC coordinator (`stage4_mac_baseline.md` §6, `DR-STAGE4-MAC-001`).

`MacPort`와 `MacExecutionPort`는 모두 이 coordinator와 하나의 state repository에 위임한다.
coordinator는 `(result, next_state, batch_or_none)`을 계산할 뿐 command를 직접 실행하거나
event를 발행하지 않는다.

거대한 전역 enum을 만들지 않고 TBB·access·resource·transfer·ACK controller가 각자
procedure-local 상태를 갖도록 나눈다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..common.addressing import (
    Address,
    AddressKind,
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from ..common.contracts.link import (
    BuiltPacket,
    EncodedPacket,
    FragmentProcessResult,
    FragmentProcessStatus,
    LinkAnalyzeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    ReassemblyControlKind,
    ReassemblyControlResult,
    TbbAssemblyControlKind,
    TbbAssemblyControlRequest,
    TbbAssemblyControlResult,
    TbbEncodeRequest,
    TbbEncodedPacketSet,
    TransferControlKind,
    TransferKind,
    TransferPacketOutcome,
    TransferPacketProgress,
    TransferRequirement,
)
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
from ..common.contracts.mac_execution import (
    AckProcedure,
    AckSlotFunction,
    EMPTY_MAC_BATCH_RECEIPT,
    MacBatchReceipt,
    WakeupFiredEvent,
    BuildPacketFromTransferCommand,
    CancelTransferEvent,
    CancelWakeupCommand,
    ClockAdvancedEvent,
    CommandCompletedEvent,
    DeliverPresentationDataCommand,
    DependencyFailedEvent,
    EndpointStoppingEvent,
    InvokeLinkCommand,
    InvokePhyCommand,
    LINK_REQUEST_TO_RESULT_TYPE,
    LinkResultEvent,
    MacCommandBatch,
    MacCommandEnvelope,
    MacEventEnvelope,
    MacCompletionReason,
    MacFailureReason,
    MacReasonCode,
    PhyReceiveWindowPlan,
    PhyRxWindowAcceptedEvent,
    PhyRxWindowClosedEvent,
    PhyTransmitPlan,
    PhyTxAcceptedEvent,
    PhyTxCompletedEvent,
    PhyTxFailedEvent,
    PresentationDeliveryResultEvent,
    PublishTransferStateCommand,
    PublishTransmissionPlanCommand,
    RxDeliveryProcedure,
    ScheduleWakeupCommand,
    TimerPurpose,
)
from ..common.contracts.message import (
    AcknowledgeNackBody,
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
    MediaAccessControlBody,
    MessageEncodeRequest,
    ResourceAllocationBody,
    ResourceRequestBody,
    ShortDataAckBody,
    ShortDataNoAckBody,
    TbbAssemblyStatus,
    TbbCandidateKey,
    VerifiedTbb,
)
from ..common.contracts.phy import PhyRecoveryStatus, RecoveredPacketSet
from ..common.contracts.service import TERMINAL_TRANSFER_STATES, TransferState
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import LinkId, PacketId, PhysicalChannel, SessionHandle, StationId
from ..common.results import Ok, internal_error
from . import ack as ack_module
from . import resources as resource_module
from . import routing as routing_module
from . import transfers as transfer_module
from .access import (
    ActiveSelectionInterval,
    SlotCandidate,
    active_selection_interval,
    active_short_data_limit,
    blocks_new_admission,
    enumerate_function_slots,
    incremented_counter,
    lease_seconds,
    select_random_access_slot,
    short_data_budget_available,
    short_data_counter_key,
)
from .deployment import (
    MacDeploymentProfile,
    OUT_OF_SERVICE_STATUS,
)
from .execution import command_class_of
from .models import (
    ActiveTbb,
    AckPendingState,
    DeliveryLedgerRecord,
    DeliveryRecordState,
    EndpointLifecycle,
    LcState,
    MacRuntimeProfile,
    MacState,
    OutstandingResourceRequest,
    PendingCommandClass,
    PendingCommandRecord,
    RelayRecord,
    RequestChain,
    ResourceRequestCandidate,
    ResourceRequestEpoch,
    RxFrameReception,
    RxSessionContext,
    ScheduledTxIntent,
    Stage4Limits,
    TbbBroadcastState,
    TbbCandidateContext,
    TimerRecord,
    TransferContext,
    TransferPhase,
    TransferRole,
    Type0Lease,
)
from .tbb import (
    STAGE4_BBSC_BOOTSTRAP_V1,
    UPPER_CHANNEL,
    WIDEBAND_BOOTSTRAP_ID,
    SlotFunction,
    SlotMap,
    build_bootstrap_slot_map,
    received_tbb_fingerprint,
)
from .tdma import (
    EPOCH_2000_UTC_NANOSECONDS,
    FRAME_NANOSECONDS,
    HEXSLOT_NANOSECONDS,
    frame_ordinal_at_nanoseconds,
    frame_ordinal_start_nanoseconds,
    next_frame_start_nanoseconds,
    position_at_nanoseconds,
    rx_windows_for_frame,
    samples_for_duration,
    utc_point,
)

#: TBB 조립 timeout은 첫 fragment frame ordinal의 `+13` frame 시작에 arm한다.
TBB_ASSEMBLY_TIMEOUT_FRAMES = 13

#: `DRAINING` 뒤 남은 dependency를 정리할 때까지의 유한 기한.
STOP_DRAIN_NANOSECONDS = FRAME_NANOSECONDS * 2

#: Data Session 한 frame이 담는 최대 fragment 수 (DC slot 14개).
FRAGMENTS_PER_FRAME = 14

#: Link가 재조립 session context를 폐기하는 오류 code
#: (`link/data_session/reassembly.py`의 `_discard_error` 경로 + UNKNOWN_SESSION).
#: 이 code가 오면 MAC도 자기 RX session을 버려야 다음 DataStart가 재OPEN한다.
_REASSEMBLY_DISCARD_CODES = frozenset({
    ErrorCode.LINK_DATA_ROUTE_MISMATCH,
    ErrorCode.LINK_FRAGMENT_CONFLICT,
    ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
    ErrorCode.LINK_INVALID_MESSAGE_FIELD,
    ErrorCode.LINK_REASSEMBLY_CAPACITY_EXHAUSTED,
    ErrorCode.LINK_REASSEMBLY_INCOMPLETE,
    ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
    ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
    ErrorCode.LINK_SESSION_CONFLICT,
    ErrorCode.LINK_UNKNOWN_SESSION,
    ErrorCode.LINK_UNSUPPORTED_SEGMENT_TYPE,
})

#: 어떤 유효 Type 13도 이 frame 수 동안 오지 않으면 Session을 실패 종료한다.
ACK_ABSENCE_LIMIT = 3

#: `AckSlotFunction` -> slot map function.
_ACK_SLOT_FUNCTIONS = {
    AckSlotFunction.RAC: SlotFunction.RAC,
    AckSlotFunction.ASC: SlotFunction.ASC,
    AckSlotFunction.DSCH: SlotFunction.DSCH,
}


@dataclass(frozen=True, slots=True)
class CoordinatorPlan:
    """`(result, next_state, batch_or_none)`."""

    result: object
    next_state: MacState
    batch: MacCommandBatch | None


def _details(reason: MacFailureReason, reason_code: str | None = None, **extra) -> Details:
    values = dict(extra)
    values["mac_reason"] = reason.value
    if reason_code is not None:
        values["reason_code"] = reason_code
    return Details.of(**values)


def _mac_error(
    code: str,
    category: ErrorCategory,
    message: str,
    reason: MacFailureReason,
    reason_code: str | None = None,
    *,
    correlation: Correlation = Correlation(),
    **extra,
):
    return internal_error(
        Layer.MAC,
        code,
        category,
        message,
        correlation=correlation,
        details=_details(reason, reason_code, **extra),
    )


def _event_invalid(message: str, **extra):
    return _mac_error(
        ErrorCode.INVALID_REQUEST,
        ErrorCategory.VALIDATION,
        message,
        MacFailureReason.EVENT_INVALID,
        **extra,
    )


def _unmatched(message: str, **extra):
    return _mac_error(
        ErrorCode.INVALID_REQUEST,
        ErrorCategory.PROTOCOL,
        message,
        MacFailureReason.COMMAND_RESULT_UNMATCHED,
        None,
        **extra,
    )


class MacCoordinator:
    """event routing과 controller 조합. 거대한 상태 enum을 만들지 않는다."""

    __slots__ = (
        "_profile",
        "_deployment",
        "_time_source",
        "_random_source",
        "_id_source",
        "_link_profiles",
        "_phy_profiles",
        "_repository",
        "_bootstrap",
        "_observer_channel_names",
        "_rx_planner_paused",
    )

    def __init__(
        self,
        *,
        profile: MacRuntimeProfile,
        deployment: MacDeploymentProfile,
        time_source,
        random_source,
        id_source,
        link_profile_registry,
        phy_profile_registry,
        repository,
    ) -> None:
        self._profile = profile
        self._deployment = deployment
        self._time_source = time_source
        self._random_source = random_source
        self._id_source = id_source
        self._link_profiles = link_profile_registry
        self._phy_profiles = phy_profile_registry
        self._repository = repository
        self._observer_channel_names = ()
        self._rx_planner_paused = False
        self._bootstrap = (
            STAGE4_BBSC_BOOTSTRAP_V1
            if profile.upper_channel.name == UPPER_CHANNEL.name
            else build_bootstrap_slot_map(
                upper=profile.upper_channel, map_id=WIDEBAND_BOOTSTRAP_ID
            )
        )

    # --- 관찰 ---

    @property
    def profile(self) -> MacRuntimeProfile:
        return self._profile

    @property
    def deployment(self) -> MacDeploymentProfile:
        return self._deployment

    @property
    def state(self) -> MacState:
        return self._repository.state

    @property
    def is_control(self) -> bool:
        return self._profile.is_control

    @property
    def local_station_id(self) -> StationId:
        return self._profile.local_station_id

    # --- 공통 helper ---

    def _now_utc_nanoseconds(self) -> int:
        return self._time_source.now_utc().total_nanoseconds()

    def _now_monotonic_nanoseconds(self) -> int:
        return self._time_source.now_monotonic().total_nanoseconds()

    def _utc(self, nanoseconds: int):
        return utc_point(nanoseconds, self._time_source.now_utc())

    def _monotonic(self, nanoseconds: int):
        from ..common.time import ClockDomain, TimePoint

        reference = self._time_source.now_monotonic()
        return TimePoint.from_nanoseconds(
            nanoseconds, ClockDomain.MONOTONIC, reference.clock_id
        )

    def active_slot_map(self, state: MacState) -> SlotMap:
        """활성 TBB가 있고 아직 유효하면 그 slot map, 아니면 bootstrap이다."""

        active = state.active_tbb
        if active is None:
            return self._bootstrap
        if not active.is_active_at(self._now_utc_nanoseconds()):
            return self._bootstrap
        return active.slot_map

    def _receive_channel(self) -> PhysicalChannel:
        return routing_module.receive_physical_channel(
            self.is_control,
            lower=self._profile.lower_channel,
            upper=self._profile.upper_channel,
        )

    def _selection_interval(
        self, state: MacState, reference_nanoseconds: int
    ) -> ActiveSelectionInterval:
        return active_selection_interval(
            lease=state.type0_lease,
            slot_map=self.active_slot_map(state),
            reference_utc_nanoseconds=reference_nanoseconds,
        )

    def _link_id_for(self, link_id_value: int = 11):
        """registry로 Link ID를 선택한다. MAC은 profile 값을 사설 표로 복제하지 않는다."""

        from ..profiles.models import SupportStatus

        link_found = self._link_profiles.lookup(link_id_value)
        if link_found.is_err:
            return transfer_module.unsupported_profile_error(link_id_value)
        phy_found = self._phy_profiles.lookup(link_id_value)
        if phy_found.is_err:
            return transfer_module.unsupported_profile_error(link_id_value)
        if phy_found.value.support_status is not SupportStatus.SUPPORTED:
            return transfer_module.unsupported_profile_error(link_id_value)
        return Ok(LinkId(link_id_value))

    def _new_command(self, causation_event_id: str, correlation: Correlation, payload):
        return MacCommandEnvelope(
            command_id=self._id_source.new_event_id(),
            causation_event_id=causation_event_id,
            correlation=correlation,
            payload=payload,
        )

    def _batch(self, causation_event_id: str, commands: list):
        """command가 없으면 batch를 만들지 않는다."""

        if not commands:
            return None
        return MacCommandBatch(
            batch_id=self._id_source.new_event_id(),
            causation_event_id=causation_event_id,
            commands=tuple(commands),
        )

    def _register_pending(
        self, state: MacState, commands: list, *, request_keys: dict | None = None
    ):
        """batch를 pending dependency quota에 원자 검사하고 등록한다."""

        keys = request_keys or {}
        totals = {
            PendingCommandClass.RX: state.pending_count(PendingCommandClass.RX),
            PendingCommandClass.NON_CLEANUP_NON_RX: state.pending_count(
                PendingCommandClass.NON_CLEANUP_NON_RX
            ),
            PendingCommandClass.CLEANUP: state.pending_count(PendingCommandClass.CLEANUP),
        }
        limits = {
            PendingCommandClass.RX: Stage4Limits.PENDING_DEPENDENCY_RX,
            PendingCommandClass.NON_CLEANUP_NON_RX: (
                Stage4Limits.PENDING_DEPENDENCY_NON_CLEANUP_NON_RX
            ),
            PendingCommandClass.CLEANUP: Stage4Limits.PENDING_DEPENDENCY_CLEANUP_RESERVE,
        }
        added = []
        for command in commands:
            command_class = command_class_of(command)
            totals[command_class] += 1
            added.append((command, command_class, self._request_key_of(command)))
        if sum(totals.values()) > Stage4Limits.PENDING_DEPENDENCY_TOTAL:
            return _mac_error(
                ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                ErrorCategory.RESOURCE,
                "pending dependency 총 상한 96을 초과했습니다",
                MacFailureReason.RESOURCE_UNAVAILABLE,
                None,
                pending_total=sum(totals.values()),
            )
        for command_class, limit in limits.items():
            if totals[command_class] > limit:
                return _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "pending dependency class quota를 초과했습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    None,
                    pending_class=command_class.value,
                    pending_class_count=totals[command_class],
                    pending_class_limit=limit,
                )
        next_state = state
        for command, command_class, request_key in added:
            next_state = next_state.with_pending_command(
                PendingCommandRecord(
                    command_id=command.command_id,
                    command_class=command_class,
                    payload_kind=type(command.payload).__name__,
                    request_key=keys.get(command.command_id, request_key),
                )
            )
        return Ok(next_state)

    @staticmethod
    def _request_key_of(command: MacCommandEnvelope) -> tuple:
        """command 결과 상관 검증용 request key. PHY event의 channel·packet_id를 고정한다."""

        payload = command.payload
        if isinstance(payload, InvokePhyCommand):
            request = payload.request
            if isinstance(request, PhyReceiveWindowPlan):
                return (
                    "RX",
                    request.channel.name,
                    request.start_at.total_nanoseconds(),
                )
            return ("TX", request.packet_id.value, request.channel.name)
        if isinstance(payload, InvokeLinkCommand):
            return ("LINK", type(payload.request).__name__)
        if isinstance(payload, PublishTransmissionPlanCommand):
            request = payload.request
            return (
                "BUILD",
                request.transfer_id.value,
                request.session_frame_index,
                request.frame_fragment_count,
                request.fragment_position_in_frame,
            )
        if isinstance(payload, ScheduleWakeupCommand):
            return ("TIMER", payload.timer_id)
        if isinstance(payload, CancelWakeupCommand):
            return ("TIMER_CANCEL", payload.timer_id)
        return ()

    def _finish(self, state: MacState, commands: list, causation_event_id: str, result):
        """pending 등록과 batch 생성을 함께 수행한다."""

        state = self._continue_stop_cleanup(state, commands, causation_event_id)
        if not commands:
            return CoordinatorPlan(result=result, next_state=state, batch=None)
        registered = self._register_pending(state, commands)
        if registered.is_err:
            return CoordinatorPlan(result=registered, next_state=self.state, batch=None)
        batch = self._batch(causation_event_id, commands)
        return CoordinatorPlan(result=result, next_state=registered.value, batch=batch)

    @staticmethod
    def _continue_stop_cleanup(
        state: MacState, commands: list, causation_event_id: str
    ) -> MacState:
        """Dispatch terminal cleanup in cleanup-reserve-sized waves."""

        if (
            state.lifecycle is not EndpointLifecycle.DRAINING
            or state.stop_drain_timer_id is not None
        ):
            return state
        available = (
            Stage4Limits.PENDING_DEPENDENCY_CLEANUP_RESERVE
            - state.pending_count(PendingCommandClass.CLEANUP)
        )
        if state.stop_cleanup_commands and available > 0:
            selected = state.stop_cleanup_commands[:available]
            commands.extend(
                replace(command, causation_event_id=causation_event_id)
                for command in selected
            )
            return state.evolve(
                stop_cleanup_commands=state.stop_cleanup_commands[len(selected) :]
            )
        if (
            not state.stop_cleanup_commands
            and state.pending_count(PendingCommandClass.CLEANUP) == 0
        ):
            return state.evolve(lifecycle=EndpointLifecycle.STOPPED)
        return state

    # --- RX window 계획 (`DR-STAGE4-MAC-012`) ---

    def required_rx_slots(self, state: MacState, frame_ordinal: int) -> tuple:
        """이 endpoint가 감시해야 하는 slot 집합.

        MASS는 receive-side upper PC의 모든 ASC·RAC·DSCH와 자신에게 `ACTIVE`로 할당된 DC를,
        ROC는 receive-side lower PC의 모든 RAC·DSCH와 할당된 DC를 감시한다. bootstrap BBSC
        수신은 활성 TBB와 무관하게 유지한다.
        """

        slot_map = self.active_slot_map(state)
        receive_channel = self._receive_channel()
        receive_names = {receive_channel.name, *self._observer_channel_names}
        wanted: list = []
        seen = set()

        def add(slot):
            key = slot.key
            if key in seen:
                return
            seen.add(key)
            wanted.append(slot)

        for slot in self._bootstrap.slots:
            if slot.function is SlotFunction.BBSC and not self.is_control:
                add(slot)
        if self.is_control:
            functions = (SlotFunction.RAC, SlotFunction.DSCH)
        else:
            functions = (SlotFunction.BBSC, SlotFunction.ASC, SlotFunction.RAC, SlotFunction.DSCH)
        for slot in slot_map.slots:
            if slot.physical_channel.name not in receive_names:
                continue
            if slot.function in functions:
                add(slot)
        for allocation in state.lc_allocations:
            if allocation.state is LcState.EXTERNAL_RESERVED and not self._observer_channel_names:
                continue
            if allocation.last_valid_frame_ordinal < frame_ordinal:
                continue
            if frame_ordinal < allocation.activation_frame_ordinal:
                continue
            if not self._observer_channel_names and allocation.peer_station_id_value != self.local_station_id.value:
                # 이 endpoint가 수신자인 할당만 assigned DC를 연다.
                if allocation.owner_station_id_value != self.local_station_id.value:
                    continue
            for slot in slot_map.slots:
                if (
                    slot.function is SlotFunction.DC
                    and slot.physical_channel.name in receive_names
                    and slot.tdma_channel == allocation.tdma_channel
                ):
                    add(slot)
        return tuple(wanted)

    def _rx_window_commands(
        self,
        state: MacState,
        frame_ordinal: int,
        causation_event_id: str,
        commands: list,
        errors: list | None = None,
    ) -> MacState:
        """현재/다음 frame의 새 required set과 저장 set의 차집합만 새 batch로 만든다."""

        if self._rx_planner_paused:
            return state
        if errors is None:
            errors = []
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return state
        data_link_id = self._link_id_for(self._profile.data_link_id)
        if data_link_id.is_err:
            return state
        slots = self.required_rx_slots(state, frame_ordinal)
        windows = rx_windows_for_frame(slots, frame_ordinal)
        planned = set(state.planned_rx_windows)
        lead = self._profile.receive_lead_nanoseconds
        now = self._now_utc_nanoseconds()
        next_state = state
        added = []
        for window in windows:
            if window.key in planned:
                continue
            if window.start_nanoseconds < now + lead:
                if window.key in next_state.deferred_rx_windows:
                    next_state = next_state.without_deferred_rx_window(window.key)
                    errors.append(self._slot_missed_error("RX_WINDOW", window_key=window.key))
                continue
            if not self._within_schedule_horizon(window.end_nanoseconds, now):
                if len(next_state.deferred_rx_windows) >= Stage4Limits.DEFERRED_RX_WINDOWS:
                    errors.append(self._rx_intent_capacity_error(window.key))
                    continue
                next_state = next_state.with_deferred_rx_window(window.key)
                next_state, armed = self._arm_horizon_entry(
                    next_state,
                    entry_utc_nanoseconds=(
                        window.end_nanoseconds
                        - self._profile.schedule_horizon_nanoseconds
                    ),
                    causation_event_id=causation_event_id,
                    commands=commands,
                    errors=errors,
                )
                if not armed:
                    next_state = next_state.without_deferred_rx_window(window.key)
                continue
            window_link_id = self._window_intent_for(
                window, link_id.value, data_link_id.value
            )
            expected_link_id = (
                data_link_id.value
                if window.slot_function in (SlotFunction.DC, SlotFunction.DSCH)
                else link_id.value
            )
            observed_value = getattr(window_link_id, "value", window_link_id)
            expected_value = getattr(expected_link_id, "value", expected_link_id)
            if observed_value != expected_value:
                # §13.2: slot function과 window intent의 mapping 위반은 window 계획을
                # 만들기 전에 거부하며 Link 11 fallback은 없다.
                errors.append(
                    self._window_intent_mapping_error(
                        window, observed_value, expected_value
                    )
                )
                continue
            plan = PhyReceiveWindowPlan(
                channel=PhysicalChannel(window.channel_name),
                start_at=self._utc(window.start_nanoseconds),
                duration_nanoseconds=window.duration_nanoseconds,
                allowed_link_ids=(window_link_id,),
            )
            commands.append(
                self._new_command(
                    causation_event_id, Correlation(), InvokePhyCommand(request=plan)
                )
            )
            next_state = next_state.without_deferred_rx_window(window.key)
            added.append(window.key)
        if added:
            next_state = next_state.evolve(
                planned_rx_windows=next_state.planned_rx_windows + tuple(added)
            )
        # 현재 frame보다 두 frame 이상 지난 window key는 유지하지 않는다.
        retained = tuple(
            key for key in next_state.planned_rx_windows if key[0] >= frame_ordinal - 1
        )
        retained_deferred = tuple(
            key for key in next_state.deferred_rx_windows if key[0] >= frame_ordinal - 1
        )
        if retained != next_state.planned_rx_windows or retained_deferred != next_state.deferred_rx_windows:
            next_state = next_state.evolve(
                planned_rx_windows=retained,
                deferred_rx_windows=retained_deferred,
            )
        return next_state

    def _within_schedule_horizon(self, end_nanoseconds: int, now_nanoseconds: int) -> bool:
        """Radio가 수락하는 가장 먼 미래 시각 안인지 검사한다.

        Runtime이 Radio capability로 제공한 상한이며, MAC은 이보다 먼 계획을 Radio에
        내보내지 않는다. 상한이 0이면 제한이 없다.
        """

        horizon = self._profile.schedule_horizon_nanoseconds
        if horizon <= 0:
            return True
        return end_nanoseconds - now_nanoseconds <= horizon

    def _window_intent_for(
        self, window, control_link_id: int, data_link_id: int
    ) -> int:
        """§13.2 slot-function window intent 결정 seam.

        BBSC/RAC/ASC는 Link 11, 할당 DC/DSCH는 실행 `data_link_id`다. test-only
        nonconforming 주입은 이 seam을 우회할 수 있으며, 호출자의 mapping guard가
        계획 생성 전에 `CONFIG_SEMANTIC_INVALID`로 거부한다.
        """

        return (
            data_link_id
            if window.slot_function in (SlotFunction.DC, SlotFunction.DSCH)
            else control_link_id
        )

    @staticmethod
    def _window_intent_mapping_error(window, observed: int, expected: int):
        return _mac_error(
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            ErrorCategory.VALIDATION,
            "RX window 허용 집합이 slot function의 §13.2 mapping과 다릅니다",
            MacFailureReason.EVENT_INVALID,
            None,
            procedure="RX_WINDOW_INTENT",
            slot_function=str(getattr(window, "slot_function", None)),
            observed_link_id=observed,
            expected_link_id=expected,
            window_channel=window.channel_name,
        )

    @staticmethod
    def _slot_missed_error(procedure: str, **details):
        return _mac_error(
            ErrorCode.MAC_TRANSFER_FAILED,
            ErrorCategory.RESOURCE,
            "선택한 slot/window가 Radio 최소 lead time 전에 제출되지 못했습니다",
            MacFailureReason.SLOT_MISSED,
            None,
            procedure=procedure,
            **details,
        )

    @staticmethod
    def _scheduled_intent_capacity_error(intent: ScheduledTxIntent):
        return _mac_error(
            ErrorCode.MAC_RESOURCE_UNAVAILABLE,
            ErrorCategory.RESOURCE,
            "예약 송신 의도 상한 32개에 도달했습니다",
            MacFailureReason.RESOURCE_UNAVAILABLE,
            MacReasonCode.RESOURCE_BUSY,
            scheduled_tx_intents=Stage4Limits.SCHEDULED_TX_INTENTS,
            rejected_intent_id=intent.intent_id,
            procedure=intent.kind,
        )

    @staticmethod
    def _rx_intent_capacity_error(window_key: tuple):
        return _mac_error(
            ErrorCode.MAC_RESOURCE_UNAVAILABLE,
            ErrorCategory.RESOURCE,
            "horizon 대기 RX window 상한에 도달했습니다",
            MacFailureReason.RESOURCE_UNAVAILABLE,
            MacReasonCode.RESOURCE_BUSY,
            deferred_rx_windows=Stage4Limits.DEFERRED_RX_WINDOWS,
            rejected_window_key=window_key,
        )

    @staticmethod
    def _horizon_timer_capacity_error():
        return _mac_error(
            ErrorCode.MAC_RESOURCE_UNAVAILABLE,
            ErrorCategory.RESOURCE,
            "horizon 진입 wakeup을 등록할 active timer 여유가 없습니다",
            MacFailureReason.RESOURCE_UNAVAILABLE,
            MacReasonCode.RESOURCE_BUSY,
            active_timer_limit=Stage4Limits.ACTIVE_TIMERS,
        )

    def _arm_horizon_entry(
        self,
        state: MacState,
        *,
        entry_utc_nanoseconds: int,
        causation_event_id: str,
        commands: list,
        errors: list,
    ) -> tuple[MacState, bool]:
        """가장 이른 horizon 진입 wakeup 하나를 유지한다."""

        if self._profile.schedule_horizon_nanoseconds <= 0:
            return state, True
        now_utc = self._now_utc_nanoseconds()
        now_monotonic = self._now_monotonic_nanoseconds()
        deadline = now_monotonic + max(0, entry_utc_nanoseconds - now_utc)
        existing = next(
            (timer for timer in state.timers if timer.purpose is TimerPurpose.RETRY),
            None,
        )
        if existing is not None and existing.deadline_monotonic_nanoseconds <= deadline:
            return state, True
        timer_id = f"horizon-{entry_utc_nanoseconds}"
        if existing is not None and existing.timer_id == timer_id:
            # 같은 horizon 진입의 wakeup이 이미 있다. UTC 재보정으로 환산
            # deadline만 달라진 경우이며, 자기 교체는 command 계약이
            # (`replaces_timer_id != timer_id`), 같은 id의 다른 deadline
            # 재사용은 endpoint가 거부한다. 기존 스케줄을 유지한다 —
            # 진입 시각 자체가 같으므로 의미 불변이다.
            return state, True
        if existing is None and len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            errors.append(self._horizon_timer_capacity_error())
            return state, False
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.RETRY,
                    replaces_timer_id=(existing.timer_id if existing is not None else None),
                ),
            )
        )
        next_state = state
        if existing is not None:
            next_state = next_state.without_timer(existing.timer_id)
        return next_state.with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.RETRY,
                deadline_monotonic_nanoseconds=deadline,
            )
        ), True

    @staticmethod
    def _without_unscheduled_marker(
        state: MacState, intent: ScheduledTxIntent
    ) -> MacState:
        next_state = state
        if next_state.type0_tx_command_id == intent.intent_id:
            next_state = next_state.evolve(type0_tx_command_id=None)
        if intent.ledger_key is not None:
            record = next_state.ledger_record(intent.ledger_key)
            if record is not None and record.replay_command_id == intent.intent_id:
                next_state = next_state.with_ledger_record(
                    replace(record, replay_command_id=None)
                )
        return next_state

    def _defer_or_emit(
        self,
        state: MacState,
        *,
        intent: ScheduledTxIntent,
        causation_event_id: str,
        commands: list,
        errors: list,
    ) -> MacState:
        """horizon 안이면 즉시 command를 만들고, 밖이면 의도만 보존한다.

        slot 선택은 이미 끝났으므로 선택한 slot, 난수 소비와 SI 의미는 바뀌지 않는다.
        """

        now = self._now_utc_nanoseconds()
        end = intent.transmit_at_nanoseconds + HEXSLOT_NANOSECONDS
        if not self._within_schedule_horizon(end, now):
            if len(state.tx_intents) >= Stage4Limits.SCHEDULED_TX_INTENTS:
                rejected = self._scheduled_intent_capacity_error(intent)
                if intent.correlation_transfer_id is not None:
                    context = state.transfer_by_value(intent.correlation_transfer_id)
                    if context is not None:
                        return self._terminate_transfer(
                            state,
                            context.transfer_id,
                            rejected.error,
                            causation_event_id,
                            commands,
                        )
                errors.append(rejected)
                return self._without_unscheduled_marker(state, intent)
            deferred = state.with_tx_intent(intent)
            deferred, armed = self._arm_horizon_entry(
                deferred,
                entry_utc_nanoseconds=(
                    end - self._profile.schedule_horizon_nanoseconds
                ),
                causation_event_id=causation_event_id,
                commands=commands,
                errors=errors,
            )
            if not armed:
                deferred = deferred.without_tx_intent(intent.intent_id)
                if intent.correlation_transfer_id is not None:
                    context = deferred.transfer_by_value(intent.correlation_transfer_id)
                    if context is not None:
                        failure = errors.pop()
                        return self._terminate_transfer(
                            deferred,
                            context.transfer_id,
                            failure.error,
                            causation_event_id,
                            commands,
                        )
                deferred = self._without_unscheduled_marker(deferred, intent)
            return deferred
        return self._emit_intent(state, intent, causation_event_id, commands)

    def _emit_intent(
        self, state: MacState, intent: ScheduledTxIntent, causation_event_id: str, commands: list
    ) -> MacState:
        correlation = Correlation()
        if intent.correlation_transfer_id is not None:
            context = state.transfer_by_value(intent.correlation_transfer_id)
            if context is not None:
                correlation = Correlation(transfer_id=context.transfer_id)
        if intent.kind == "BUILD":
            command = self._new_command(
                causation_event_id,
                correlation,
                PublishTransmissionPlanCommand(request=intent.build_request),
            )
            commands.append(command)
            key = ("TX_TIME", command.command_id)
            return state.evolve(
                delivery_pending=tuple(
                    item for item in state.delivery_pending if item[0] != key
                )
                + ((key, intent.transmit_at_nanoseconds),)
            )
        command = self._new_command(
            causation_event_id, correlation, InvokeLinkCommand(request=intent.encode_request)
        )
        commands.append(command)
        next_state = state.evolve(
            delivery_pending=state.delivery_pending
            + (
                (("CTRL_TIME", command.command_id), intent.transmit_at_nanoseconds),
                (("CTRL_CHANNEL", command.command_id), intent.channel_name),
            )
        )
        if intent.resource_chain_station_ids:
            next_state = next_state.evolve(
                delivery_pending=next_state.delivery_pending
                + (
                    (
                        ("RESOURCE_BATCH", command.command_id),
                        intent.resource_chain_station_ids,
                    ),
                )
            )
        if state.type0_tx_command_id == intent.intent_id:
            next_state = next_state.evolve(type0_tx_command_id=command.command_id)
        if intent.ledger_key is not None:
            record = next_state.ledger_record(intent.ledger_key)
            if record is not None:
                next_state = next_state.with_ledger_record(
                    DeliveryLedgerRecord(
                        key=record.key,
                        procedure=record.procedure,
                        state=record.state,
                        payload_length=record.payload_length,
                        payload_digest=record.payload_digest,
                        first_received_frame_ordinal=record.first_received_frame_ordinal,
                        completion_frame_ordinal=record.completion_frame_ordinal,
                        expires_after_frame=record.expires_after_frame,
                        highest_retransmission_number=record.highest_retransmission_number,
                        ack_state=record.ack_state,
                        replay_context=record.replay_context,
                        replay_command_id=command.command_id,
                        last_replay_scheduled_frame=record.last_replay_scheduled_frame,
                        pending_delivery_key=record.pending_delivery_key,
                    )
                )
        if intent.correlation_transfer_id is not None:
            outstanding = next_state.outstanding_request
            if (
                outstanding is not None
                and outstanding.transfer_id.value == intent.correlation_transfer_id
                and outstanding.last_tx_command_id is None
            ):
                next_state = next_state.evolve(
                    outstanding_request=OutstandingResourceRequest(
                        transfer_id=outstanding.transfer_id,
                        attempt_count=outstanding.attempt_count,
                        timer_id=outstanding.timer_id,
                        route=outstanding.route,
                        terminal_capabilities=outstanding.terminal_capabilities,
                        priority=outstanding.priority,
                        physical_channel_name=outstanding.physical_channel_name,
                        last_tx_command_id=command.command_id,
                    )
                )
        return next_state

    def _release_due_tx_intents(
        self, state: MacState, causation_event_id: str, commands: list, errors: list
    ) -> MacState:
        """horizon 안에 들어온 송신 의도를 command로 내보내고, 지난 slot은 SLOT_MISSED로 닫는다."""

        if not state.tx_intents:
            return state
        now = self._now_utc_nanoseconds()
        lead = self._profile.transmit_lead_nanoseconds
        next_state = state
        for intent in sorted(state.tx_intents, key=lambda item: item.transmit_at_nanoseconds):
            if intent.transmit_at_nanoseconds < now + lead:
                # 고른 slot을 지나쳤으면 다음 slot으로 자동 이동하지 않고 확정 실패한다.
                next_state = next_state.without_tx_intent(intent.intent_id)
                if intent.correlation_transfer_id is not None:
                    context = next_state.transfer_by_value(intent.correlation_transfer_id)
                    if context is not None:
                        next_state = self._terminate_transfer(
                            next_state,
                            context.transfer_id,
                            transfer_module.slot_missed_error(intent.kind).error,
                            causation_event_id,
                            commands,
                        )
                else:
                    errors.append(self._slot_missed_error(intent.kind))
                continue
            if not self._within_schedule_horizon(
                intent.transmit_at_nanoseconds + HEXSLOT_NANOSECONDS, now
            ):
                continue
            next_state = self._emit_intent(
                next_state.without_tx_intent(intent.intent_id),
                intent,
                causation_event_id,
                commands,
            )
        return next_state

    def plan_frame_windows(
        self, state: MacState, causation_event_id: str, commands: list, errors: list
    ) -> MacState:
        """현재와 다음 frame의 RX window를 one-frame lookahead로 계획한다."""

        now = self._now_utc_nanoseconds()
        current = frame_ordinal_at_nanoseconds(now)
        next_state = state
        for frame_ordinal in (current, current + 1):
            next_state = self._rx_window_commands(
                next_state, frame_ordinal, causation_event_id, commands, errors
            )
        return next_state

    # --- endpoint lifecycle ---

    def begin_running(self, causation_event_id: str) -> CoordinatorPlan:
        """`RUNNING` 진입. Radio lead time을 만족하는 첫 future TDMA frame을 seed한다."""

        state = self.state
        if state.lifecycle is EndpointLifecycle.RUNNING:
            return CoordinatorPlan(result=Ok(state), next_state=state, batch=None)
        commands: list = []
        errors: list = []
        now = self._now_utc_nanoseconds()
        if self.is_control and now >= self._profile.canonical_tbb.valid_until_nanoseconds():
            return CoordinatorPlan(
                result=transfer_module.tbb_expired_error(during_session=False),
                next_state=state,
                batch=None,
            )
        current_frame = frame_ordinal_at_nanoseconds(now)
        seeded_state = state.evolve(lifecycle=EndpointLifecycle.RUNNING, seeded=True)
        required = tuple(
            self.required_rx_slots(seeded_state, ordinal)
            for ordinal in (current_frame, current_frame + 1)
        )
        planned = self.plan_frame_windows(seeded_state, causation_event_id, commands, errors)
        # horizon 밖이라 아직 내보내지 않은 window는 lead time 실패가 아니다. 다음
        # clock/frame event에서 horizon 안에 들어오면 그대로 계획한다.
        lead = self._profile.receive_lead_nanoseconds
        now_utc = self._now_utc_nanoseconds()
        schedulable = any(
            window.start_nanoseconds >= now_utc + lead
            for ordinal, slots in zip((current_frame, current_frame + 1), required)
            for window in rx_windows_for_frame(slots, ordinal)
        )
        if any(required) and not commands and not schedulable:
            # 감시해야 할 slot이 있는데 lead time·channel-switch gap 때문에 one-frame
            # lookahead로 필수 기본 window를 하나도 만들 수 없으면 readiness를 거부한다.
            return CoordinatorPlan(
                result=_mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "one-frame lookahead에서 필수 기본 RX window를 만들 수 없습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    MacReasonCode.RX_LOOKAHEAD_UNSATISFIED,
                ),
                next_state=state,
                batch=None,
            )
        planned = self._schedule_frame_boundary(planned, causation_event_id, commands)
        if self.is_control:
            planned = self._start_tbb_broadcast(planned, causation_event_id, commands)
        result = errors[0] if errors else Ok(planned)
        return self._finish(planned, commands, causation_event_id, result)

    def _schedule_frame_boundary(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        now_utc = self._now_utc_nanoseconds()
        now_monotonic = self._now_monotonic_nanoseconds()
        boundary_utc = next_frame_start_nanoseconds(now_utc)
        deadline = now_monotonic + (boundary_utc - now_utc)
        timer_id = f"frame-{frame_ordinal_at_nanoseconds(boundary_utc)}"
        if state.timer(timer_id) is not None:
            return state
        if len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            return state
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.FRAME_BOUNDARY,
                ),
            )
        )
        return state.with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.FRAME_BOUNDARY,
                deadline_monotonic_nanoseconds=deadline,
            )
        )

    # --- TBB 방송 (`DR-STAGE4-MAC-004`) ---

    def _start_tbb_broadcast(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """ROC가 canonical fixture로 Type 20→21→22 순환 방송을 준비한다."""

        canonical = self._profile.canonical_tbb
        now = self._now_utc_nanoseconds()
        if now >= canonical.valid_until_nanoseconds():
            # 시작 시 이미 만료된 fixture는 방송하지 않는다.
            return state
        if state.tbb_broadcast.pending_command_id is not None:
            return state
        if state.tbb_broadcast.encoded_packets:
            return state
        command = self._new_command(
            causation_event_id,
            Correlation(),
            InvokeLinkCommand(
                request=TbbEncodeRequest(
                    source_id=canonical.source_id,
                    control_station_id=canonical.control_station_id,
                    service_capability=canonical.service_capability,
                    bulletin_board_version=canonical.bulletin_board_version,
                    bulletin_board=canonical.bulletin_board,
                    correlation=Correlation(),
                )
            ),
        )
        commands.append(command)
        return state.evolve(
            tbb_broadcast=TbbBroadcastState(pending_command_id=command.command_id)
        )

    def _next_bbsc_transmit(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """다음 미래 BBSC에서 한 fragment Packet을 송신한다."""

        broadcast = state.tbb_broadcast
        if not broadcast.encoded_packets:
            return state
        if broadcast.pending_command_id is not None:
            # 한 fragment Packet씩 순환한다. 진행 중인 송신이 끝나기 전에 다시 계획하지 않는다.
            return state
        canonical = self._profile.canonical_tbb
        now = self._now_utc_nanoseconds()
        valid_until = canonical.valid_until_nanoseconds()
        if now >= valid_until:
            return state
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return state
        # 각 encoded fingerprint를 Packet 송신 전에 다시 확인한다.
        if broadcast.certificate_fingerprint != canonical.expected_certificate_fingerprint:
            return state
        lead = self._profile.transmit_lead_nanoseconds
        search_end = now + FRAME_NANOSECONDS * 3
        schedule_horizon = self._profile.schedule_horizon_nanoseconds
        if schedule_horizon > 0:
            # Radio가 수락하는 가장 먼 시각 안의 BBSC만 지금 계획한다. 남은 기회는 다음
            # clock/frame event에서 horizon 안에 들어올 때 계획한다.
            search_end = min(search_end, now + schedule_horizon - HEXSLOT_NANOSECONDS)
        candidates = enumerate_function_slots(
            slot_map=self._bootstrap,
            function=SlotFunction.BBSC,
            physical_channel=self._profile.upper_channel,
            from_utc_nanoseconds=now + lead,
            until_utc_nanoseconds=min(search_end, valid_until),
            transmit=True,
        )
        if not candidates:
            return state
        slot = candidates[0]
        index = broadcast.next_fragment_index % len(broadcast.encoded_packets)
        packet = broadcast.encoded_packets[index]
        plan = PhyTransmitPlan(
            packet_id=packet.packet_id,
            packet=packet.packet,
            link_id=link_id.value,
            channel=self._profile.upper_channel,
            transmit_at=self._utc(slot.start_nanoseconds),
        )
        command = self._new_command(
            causation_event_id,
            Correlation(packet_id=packet.packet_id),
            InvokePhyCommand(request=plan),
        )
        commands.append(command)
        return state.evolve(
            tbb_broadcast=TbbBroadcastState(
                next_fragment_index=(index + 1) % len(broadcast.encoded_packets),
                fragment_count=broadcast.fragment_count,
                pending_command_id=command.command_id,
                encoded_packets=broadcast.encoded_packets,
                certificate_fingerprint=broadcast.certificate_fingerprint,
            )
        )

    def _apply_verified_tbb(
        self, state: MacState, verified: VerifiedTbb, causation_event_id: str, commands: list
    ):
        """수신 TBB 검증 순서를 고정한다 (`DR-STAGE4-MAC-004`).

        station ID → (활성 없음) 기대 fingerprint → (활성 있음) 동일 fingerprint / 다른
        version / 같은 version 다른 fingerprint 순으로 판정한다.
        """

        canonical = self._profile.canonical_tbb
        if (
            verified.source_id.value != canonical.source_id.value
            or verified.control_station_id != canonical.control_station_id
        ):
            return state, routing_module.unexpected_control_station_error(
                "구성된 ROC·Control station과 다른 TBB입니다",
                observed_source_id=verified.source_id.value,
                observed_control_station_id=verified.control_station_id,
            )
        _, fingerprint = received_tbb_fingerprint(
            verified, canonical.semantic_fixture_id, schema=canonical.semantic_schema
        )
        active = state.active_tbb
        if active is None:
            if fingerprint != canonical.canonical_tbb_fingerprint:
                return state, routing_module.feature_not_supported_error(
                    "수신 TBB의 semantic fingerprint가 기대값과 다릅니다",
                    MacReasonCode.TBB_FIXTURE_MISMATCH,
                )
        else:
            if fingerprint == active.fingerprint:
                return state, None
            if verified.bulletin_board_version != active.bulletin_board_version:
                return state, routing_module.feature_not_supported_error(
                    "활성 TBB와 다른 version으로 교체할 수 없습니다",
                    MacReasonCode.TBB_VERSION_SWITCH_NOT_SUPPORTED,
                )
            return state, routing_module.feature_not_supported_error(
                "같은 version인데 semantic fingerprint가 다릅니다",
                MacReasonCode.TBB_FIXTURE_MISMATCH,
            )

        next_state, error = self._activate_canonical_tbb(
            state,
            fingerprint=fingerprint,
            bulletin_board_version=verified.bulletin_board_version,
            source_id=verified.source_id,
            control_station_id=verified.control_station_id,
        )
        if error is not None:
            return state, error
        candidate = state.tbb_candidate
        if candidate is not None:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(),
                    CancelWakeupCommand(timer_id=candidate.timer_id),
                )
            )
            next_state = next_state.without_timer(candidate.timer_id)
        return next_state, None

    def _activate_canonical_tbb(
        self,
        state: MacState,
        *,
        fingerprint: str,
        bulletin_board_version: int,
        source_id: StationId,
        control_station_id: int,
    ):
        """수신 적용과 ROC self-activation이 같은 다음-frame 경계를 사용하게 한다."""

        canonical = self._profile.canonical_tbb
        now = self._now_utc_nanoseconds()
        valid_from = canonical.valid_from_nanoseconds()
        valid_until = canonical.valid_until_nanoseconds()
        activation = next_frame_start_nanoseconds(max(now, valid_from))
        if activation >= valid_until:
            return state, transfer_module.tbb_expired_error(during_session=False)
        return (
            state.evolve(
                active_tbb=ActiveTbb(
                    fingerprint=fingerprint,
                    bulletin_board_version=bulletin_board_version,
                    source_id=source_id,
                    control_station_id=control_station_id,
                    valid_from_nanoseconds=valid_from,
                    valid_until_nanoseconds=valid_until,
                    activation_nanoseconds=activation,
                    slot_map=canonical.slot_map,
                ),
                tbb_candidate=None,
            ),
            None,
        )

    def _handle_tbb_assembly_updates(
        self, state: MacState, updates: tuple, causation_event_id: str, commands: list
    ) -> MacState:
        """canonical key의 첫 `STARTED/INCOMPLETE`에 13-frame timeout을 arm한다."""

        canonical = self._profile.canonical_tbb
        next_state = state
        for update in updates:
            key = (
                update.key.source_id.value,
                update.key.control_station_id,
                update.key.bulletin_board_version,
            )
            expected_key = (
                canonical.source_id.value,
                canonical.control_station_id,
                canonical.bulletin_board_version,
            )
            if update.status is TbbAssemblyStatus.VERIFIED:
                current = next_state.tbb_candidate
                if current is not None and current.key == key:
                    commands.append(
                        self._new_command(
                            causation_event_id,
                            Correlation(),
                            CancelWakeupCommand(timer_id=current.timer_id),
                        )
                    )
                    next_state = next_state.without_timer(current.timer_id).evolve(
                        tbb_candidate=None
                    )
                continue
            if key != expected_key:
                # 구성과 다른 source/control 또는 다른 version은 기다리지 않고 정리한다.
                commands.append(
                    self._new_command(
                        causation_event_id,
                        Correlation(),
                        InvokeLinkCommand(
                            request=TbbAssemblyControlRequest(
                                kind=TbbAssemblyControlKind.ABORT,
                                target=update.key,
                                replacement=None,
                                correlation=Correlation(),
                            )
                        ),
                    )
                )
                continue
            if update.status not in (TbbAssemblyStatus.STARTED, TbbAssemblyStatus.INCOMPLETE):
                continue
            if next_state.tbb_candidate is not None and next_state.tbb_candidate.key == key:
                # duplicate·추가 fragment는 deadline을 연장하지 않는다.
                continue
            if next_state.tbb_candidate is not None:
                continue
            frame_ordinal = frame_ordinal_at_nanoseconds(
                update.updated_at.total_nanoseconds()
            )
            deadline_frame = frame_ordinal + TBB_ASSEMBLY_TIMEOUT_FRAMES
            deadline_utc = frame_ordinal_start_nanoseconds(deadline_frame)
            now_utc = self._now_utc_nanoseconds()
            deadline_monotonic = self._now_monotonic_nanoseconds() + max(
                deadline_utc - now_utc, 0
            )
            timer_id = f"tbb-assembly-{key[0]}-{key[1]}-{key[2]}"
            if len(next_state.timers) >= Stage4Limits.ACTIVE_TIMERS:
                # Link already owns the partial candidate.  If MAC cannot own its
                # mandatory timeout, explicitly abort it instead of leaking an
                # unbounded candidate behind a silently dropped timer.
                commands.append(
                    self._new_command(
                        causation_event_id,
                        Correlation(),
                        InvokeLinkCommand(
                            request=TbbAssemblyControlRequest(
                                kind=TbbAssemblyControlKind.ABORT,
                                target=update.key,
                                replacement=None,
                                correlation=Correlation(),
                            )
                        ),
                    )
                )
                continue
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(),
                    ScheduleWakeupCommand(
                        timer_id=timer_id,
                        deadline_monotonic=self._monotonic(deadline_monotonic),
                        purpose=TimerPurpose.TBB_ASSEMBLY_TIMEOUT,
                    ),
                )
            )
            next_state = next_state.with_timer(
                TimerRecord(
                    timer_id=timer_id,
                    purpose=TimerPurpose.TBB_ASSEMBLY_TIMEOUT,
                    deadline_monotonic_nanoseconds=deadline_monotonic,
                )
            ).evolve(
                tbb_candidate=TbbCandidateContext(
                    source_id_value=key[0],
                    control_station_id=key[1],
                    bulletin_board_version=key[2],
                    timer_id=timer_id,
                    deadline_frame_ordinal=deadline_frame,
                    first_fragment_frame_ordinal=frame_ordinal,
                )
            )
        return next_state

    # --- Type 0 (`DR-STAGE4-MAC-005`) ---

    def _start_type0_lease(
        self,
        state: MacState,
        body: MediaAccessControlBody,
        started_monotonic: int,
        causation_event_id: str,
        commands: list,
    ):
        """유효 Type 0을 적용해 240..480초 공통 lease를 시작한다."""

        existing = state.type0_lease
        canonical = (
            body.media_access_priority_level,
            body.random_access_selection_interval,
            body.short_data_message_limit,
            body.system_status,
        )
        if existing is not None and existing.source_id_value == body.source_id.value:
            if existing.canonical_fields == canonical:
                # idempotent duplicate. 난수를 다시 뽑거나 deadline을 연장하지 않는다.
                return state, None
            return state, routing_module.feature_not_supported_error(
                "lease 중 Type 0 override 값 교체는 지원하지 않습니다",
                MacReasonCode.TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED,
            )
        if len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            # A lease without its expiry timer would violate the bounded-state
            # contract and could leave Type 0 policy active indefinitely.  Reject
            # before consuming randomness so the caller can retry deterministically.
            return state, transfer_module.resource_busy_error()
        seconds = lease_seconds(self._random_source)
        timer_id = f"type0-lease-{started_monotonic}"
        deadline = started_monotonic + seconds * 1_000_000_000
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.LEASE_EXPIRY,
                ),
            )
        )
        lease = Type0Lease(
            source_id_value=body.source_id.value,
            media_access_priority_level=body.media_access_priority_level,
            selection_interval=body.random_access_selection_interval,
            short_data_limit=body.short_data_message_limit,
            system_status=body.system_status,
            started_monotonic_nanoseconds=started_monotonic,
            lease_seconds=seconds,
            timer_id=timer_id,
        )
        next_state = state.evolve(type0_lease=lease).with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.LEASE_EXPIRY,
                deadline_monotonic_nanoseconds=deadline,
            )
        )
        return next_state, None

    def _plan_type0_transmission(
        self, state: MacState, causation_event_id: str, commands: list, errors: list
    ) -> MacState:
        """ROC가 고정 TBB 활성화 뒤 첫 사용 가능한 upper ASC에서 Type 0을 한 번 송신한다."""

        override = self._deployment.type0_override
        if override is None or state.type0_transmitted:
            return state
        if state.type0_tx_command_id is not None:
            return state
        active = state.active_tbb
        now = self._now_utc_nanoseconds()
        if active is None or not active.is_active_at(now):
            return state
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return state
        lead = self._profile.transmit_lead_nanoseconds
        candidates = enumerate_function_slots(
            slot_map=self.active_slot_map(state),
            function=SlotFunction.ASC,
            physical_channel=self._profile.upper_channel,
            from_utc_nanoseconds=now + lead,
            until_utc_nanoseconds=now + lead + FRAME_NANOSECONDS * 2,
            transmit=True,
        )
        if not candidates:
            return state
        slot = candidates[0]
        body = MediaAccessControlBody(
            source_id=self._deployment.roc_station_id,
            media_access_priority_level=0,
            random_access_selection_interval=override.selection_interval,
            short_data_message_limit=override.short_data_limit,
            system_status=override.system_status,
        )
        intent = ScheduledTxIntent(
            intent_id=self._id_source.new_event_id(),
            kind="ENCODE",
            transmit_at_nanoseconds=slot.start_nanoseconds,
            channel_name=self._profile.upper_channel.name,
            encode_request=LinkPacketEncodeRequest(
                link_id=link_id.value,
                messages=(MessageEncodeRequest(body=body, correlation=Correlation()),),
                correlation=Correlation(),
            ),
        )
        prepared = state.evolve(type0_tx_command_id=intent.intent_id)
        return self._defer_or_emit(
            prepared,
            intent=intent,
            causation_event_id=causation_event_id,
            commands=commands,
            errors=errors,
        )

    # --- Service transfer 접수 (`MacPort.submit_requirement`) ---

    def submit_requirement(
        self, requirement: TransferRequirement, causation_event_id: str
    ) -> CoordinatorPlan:
        state = self.state
        if not isinstance(requirement, TransferRequirement):
            return CoordinatorPlan(
                result=_event_invalid("TransferRequirement가 필요합니다"),
                next_state=state,
                batch=None,
            )
        correlation = Correlation(transfer_id=requirement.transfer_id)
        if state.lifecycle is EndpointLifecycle.DRAINING:
            return CoordinatorPlan(
                result=transfer_module.endpoint_stopping_error(),
                next_state=state,
                batch=None,
            )
        if len(state.local_fifo) >= Stage4Limits.LOCAL_TRANSFER_FIFO:
            return CoordinatorPlan(
                result=transfer_module.local_fifo_full_error(), next_state=state, batch=None
            )
        if blocks_new_admission(state.type0_lease):
            return CoordinatorPlan(
                result=transfer_module.control_station_out_of_service_error(),
                next_state=state,
                batch=None,
            )
        active = state.active_tbb
        now = self._now_utc_nanoseconds()
        if active is None or not active.is_active_at(now):
            return CoordinatorPlan(
                result=transfer_module.tbb_expired_error(during_session=False)
                if active is not None
                else _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "활성 TBB가 없어 데이터 자원을 계획할 수 없습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    MacReasonCode.TBB_EXPIRED,
                    correlation=correlation,
                ),
                next_state=state,
                batch=None,
            )

        if requirement.candidate is TransferKind.SHORT_DATA:
            return self._submit_short_data(state, requirement, causation_event_id)
        return self._submit_data_session(state, requirement, causation_event_id)

    def _submit_short_data(
        self, state: MacState, requirement: TransferRequirement, causation_event_id: str
    ) -> CoordinatorPlan:
        correlation = Correlation(transfer_id=requirement.transfer_id)
        placement = routing_module.resolve_short_data_direction(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            route=requirement.route,
            ack_required=requirement.ack_required,
            lower=self._profile.lower_channel,
            upper=self._profile.upper_channel,
        )
        if placement.is_err:
            return CoordinatorPlan(result=placement, next_state=state, batch=None)
        placement = placement.value
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return CoordinatorPlan(result=link_id, next_state=state, batch=None)
        if requirement.ack_required and any(
            item.role is TransferRole.LOCAL_DATA_SESSION
            and item.phase is not TransferPhase.TERMINAL
            for item in state.transfers
        ):
            # ACK-required Short Data와 active Data Session은 겹치지 않는다. FIFO에서 기다린다.
            queued = state.with_transfer(
                TransferContext(
                    transfer_id=requirement.transfer_id,
                    role=TransferRole.LOCAL_SHORT_DATA,
                    phase=TransferPhase.QUEUED,
                    route=requirement.route,
                    ack_required=requirement.ack_required,
                    presentation_size=requirement.presentation_size,
                    public_state=TransferState.QUEUED,
                    link_id=link_id.value,
                    physical_channel_name=placement.tx_physical_channel.name,
                )
            ).evolve(local_fifo=state.local_fifo + (requirement.transfer_id.value,))
            return CoordinatorPlan(
                result=Ok(
                    RequirementAccepted(
                        transfer_id=requirement.transfer_id,
                        accepted_at=self._time_source.now_utc(),
                    )
                ),
                next_state=queued,
                batch=None,
            )

        commands: list = []
        planned = self._plan_short_data_transmission(
            state,
            requirement=requirement,
            placement=placement,
            link_id=link_id.value,
            retransmission_number=0 if requirement.ack_required else None,
            causation_event_id=causation_event_id,
            commands=commands,
        )
        if planned.is_err:
            return CoordinatorPlan(result=planned, next_state=state, batch=None)
        next_state = planned.value
        return self._finish(
            next_state,
            commands,
            causation_event_id,
            Ok(
                RequirementAccepted(
                    transfer_id=requirement.transfer_id,
                    accepted_at=self._time_source.now_utc(),
                )
            ),
        )

    def _plan_short_data_transmission(
        self,
        state: MacState,
        *,
        requirement: TransferRequirement,
        placement,
        link_id: LinkId,
        retransmission_number: int | None,
        causation_event_id: str,
        commands: list,
    ):
        """허용 RAC/ASC slot을 골라 Stage 4 build-only route를 시작한다."""

        now = self._now_utc_nanoseconds()
        slot_map = self.active_slot_map(state)
        channel = placement.tx_physical_channel
        interval = self._selection_interval(state, now)
        lead = self._profile.transmit_lead_nanoseconds
        occupied = frozenset()
        if placement.tx_slot_function is SlotFunction.RAC:
            limit = active_short_data_limit(
                lease=state.type0_lease, slot_map=slot_map
            )
            selected = select_random_access_slot(
                slot_map=slot_map,
                physical_channel=channel,
                from_utc_nanoseconds=now + lead,
                interval=interval,
                random_source=self._random_source,
                occupied_starts=occupied,
            )
            if selected.is_err:
                return selected
            slot = selected.value
            counter_key = short_data_counter_key(channel, slot.start_nanoseconds)
            if not short_data_budget_available(
                counters=state.short_data_counters, key=counter_key, limit=limit
            ):
                return _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "이 TDMA frame의 Physical Channel Short Data limit을 초과했습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    None,
                    short_data_limit=limit,
                )
            state = state.with_short_data_counter(
                incremented_counter(state.short_data_counters, counter_key)
            )
        else:
            candidates = enumerate_function_slots(
                slot_map=slot_map,
                function=placement.tx_slot_function,
                physical_channel=channel,
                from_utc_nanoseconds=now + lead,
                until_utc_nanoseconds=now + lead + FRAME_NANOSECONDS * 2,
                transmit=True,
            )
            if not candidates:
                return _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "사용 가능한 ASC slot이 없습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                )
            slot = candidates[0]

        plan = TransmissionPlan(
            transfer_id=requirement.transfer_id,
            link_id=link_id,
            channel=channel,
            transmit_at=self._utc(slot.start_nanoseconds),
            procedure=TransmissionProcedure.SHORT_DATA,
            route=requirement.route,
            session_handle=None,
        )
        build_request = BuildPacketFromTransferCommand(
            transfer_id=requirement.transfer_id,
            plan=plan,
            retransmission_number=retransmission_number,
        )
        intent = ScheduledTxIntent(
            intent_id=self._id_source.new_event_id(),
            kind="BUILD",
            transmit_at_nanoseconds=slot.start_nanoseconds,
            channel_name=channel.name,
            correlation_transfer_id=requirement.transfer_id.value,
            build_request=build_request,
        )
        context = state.transfer(requirement.transfer_id)
        if context is None:
            context = TransferContext(
                transfer_id=requirement.transfer_id,
                role=TransferRole.LOCAL_SHORT_DATA,
                phase=TransferPhase.BUILDING,
                route=requirement.route,
                ack_required=requirement.ack_required,
                presentation_size=requirement.presentation_size,
                public_state=TransferState.QUEUED,
                link_id=link_id,
                physical_channel_name=channel.name,
                retransmission_number=retransmission_number or 0,
            )
        else:
            context = TransferContext(
                transfer_id=context.transfer_id,
                role=context.role,
                phase=TransferPhase.BUILDING,
                route=context.route,
                ack_required=context.ack_required,
                presentation_size=context.presentation_size,
                public_state=context.public_state,
                session_handle=context.session_handle,
                link_id=link_id,
                physical_channel_name=channel.name,
                retransmission_number=retransmission_number or 0,
                session_frame_index=context.session_frame_index,
                logical_channel_tx=context.logical_channel_tx,
                logical_channel_rx=context.logical_channel_rx,
                ack_timer_id=context.ack_timer_id,
                pending_packet_ids=context.pending_packet_ids,
                frame_ledger=context.frame_ledger,
                absent_ack_frames=context.absent_ack_frames,
                relay_source_transfer_id=context.relay_source_transfer_id,
                first_tx_accepted=context.first_tx_accepted,
            )
        prepared = state.with_transfer(context)
        intent_errors: list = []
        return Ok(
            self._defer_or_emit(
                prepared,
                intent=intent,
                causation_event_id=causation_event_id,
                commands=commands,
                errors=intent_errors,
            )
        )

    def _submit_data_session(
        self, state: MacState, requirement: TransferRequirement, causation_event_id: str
    ) -> CoordinatorPlan:
        plan = routing_module.resolve_data_session_plan(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            route=requirement.route,
            lower=self._profile.lower_channel,
            upper=self._profile.upper_channel,
        )
        if plan.is_err:
            return CoordinatorPlan(result=plan, next_state=state, batch=None)
        plan = plan.value
        transfer_route = requirement.route
        if plan.relay_required:
            transfer_route = RouteAddresses(
                original_source=requirement.route.original_source,
                original_destination=requirement.route.original_destination,
                node_source=NodeSource(self.local_station_id),
                node_destination=NodeDestination(Addressed(plan.first_hop_peer)),
            )
        link_id = self._link_id_for(self._profile.data_link_id)
        if link_id.is_err:
            return CoordinatorPlan(result=link_id, next_state=state, batch=None)
        link_requirement = next(
            (
                item
                for item in requirement.profiles
                if item.link_id.value == link_id.value.value
            ),
            None,
        )
        if link_requirement is None:
            return CoordinatorPlan(
                result=transfer_module.unsupported_profile_error(link_id.value.value),
                next_state=state,
                batch=None,
            )
        if (
            state.active_data_session_count() >= Stage4Limits.ACTIVE_DATA_SESSION
            or state.outstanding_request is not None
        ):
            queued = state.with_transfer(
                TransferContext(
                    transfer_id=requirement.transfer_id,
                    role=TransferRole.LOCAL_DATA_SESSION,
                    phase=TransferPhase.QUEUED,
                    route=transfer_route,
                    ack_required=requirement.ack_required,
                    presentation_size=requirement.presentation_size,
                    public_state=TransferState.QUEUED,
                    link_id=link_id.value,
                    physical_channel_name=plan.request_physical_channel.name,
                    total_fragments=link_requirement.data_session_fragment_count,
                )
            ).evolve(local_fifo=state.local_fifo + (requirement.transfer_id.value,))
            return CoordinatorPlan(
                result=Ok(
                    RequirementAccepted(
                        transfer_id=requirement.transfer_id,
                        accepted_at=self._time_source.now_utc(),
                    )
                ),
                next_state=queued,
                batch=None,
            )

        commands: list = []
        context = TransferContext(
            transfer_id=requirement.transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.AWAITING_RESOURCE,
            route=transfer_route,
            ack_required=requirement.ack_required,
            presentation_size=requirement.presentation_size,
            public_state=TransferState.QUEUED,
            link_id=link_id.value,
            physical_channel_name=plan.request_physical_channel.name,
            total_fragments=link_requirement.data_session_fragment_count,
        )
        next_state = state.with_transfer(context)
        started = self._start_resource_request(
            next_state,
            transfer_id=requirement.transfer_id,
            session_plan=plan,
            route=transfer_route,
            causation_event_id=causation_event_id,
            commands=commands,
        )
        if started.is_err:
            return CoordinatorPlan(result=started, next_state=state, batch=None)
        return self._finish(
            started.value,
            commands,
            causation_event_id,
            Ok(
                RequirementAccepted(
                    transfer_id=requirement.transfer_id,
                    accepted_at=self._time_source.now_utc(),
                )
            ),
        )

    def _start_resource_request(
        self,
        state: MacState,
        *,
        transfer_id,
        session_plan,
        route: RouteAddresses,
        causation_event_id: str,
        commands: list,
        attempt_count: int = 0,
    ):
        """MASS는 허용 RAC에서 Type 90을, ROC는 ASC에서 `[Type 90, Type 4]`를 보낸다."""

        now = self._now_utc_nanoseconds()
        slot_map = self.active_slot_map(state)
        channel = session_plan.request_physical_channel
        lead = self._profile.transmit_lead_nanoseconds
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return link_id
        data_link_id = self._link_id_for(self._profile.data_link_id)
        if data_link_id.is_err:
            return data_link_id

        if session_plan.request_slot_function is SlotFunction.RAC:
            interval = self._selection_interval(state, now)
            selected = select_random_access_slot(
                slot_map=slot_map,
                physical_channel=channel,
                from_utc_nanoseconds=now + lead,
                interval=interval,
                random_source=self._random_source,
                occupied_starts=frozenset(),
            )
            if selected.is_err:
                return selected
            slot = selected.value
            body = resource_module.build_resource_request_body(
                original_source=route.original_source.station_id,
                node_source=self.local_station_id,
                node_destination=Addressed(self._deployment.roc_station_id),
                original_destination=route.original_destination.address,
            )
            messages = (MessageEncodeRequest(body=body, correlation=Correlation()),)
        else:
            candidates = enumerate_function_slots(
                slot_map=slot_map,
                function=SlotFunction.ASC,
                physical_channel=channel,
                from_utc_nanoseconds=now + lead,
                until_utc_nanoseconds=now + lead + FRAME_NANOSECONDS * 2,
                transmit=True,
            )
            if not candidates:
                return _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "사용 가능한 ASC slot이 없습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                )
            slot = candidates[0]
            transmit_frame = slot.frame_ordinal
            pair = resource_module.select_free_duplex_pair(
                slot_map=slot_map,
                direction=session_plan.direction,
                allocations=state.lc_allocations,
                frame_ordinal=transmit_frame,
                lower=self._profile.lower_channel,
                upper=self._profile.upper_channel,
            )
            if pair is None:
                return transfer_module.resource_busy_error()
            announcement = resource_module.build_resource_request_body(
                original_source=route.original_source.station_id,
                node_source=self.local_station_id,
                node_destination=route.original_destination.address,
                original_destination=route.original_destination.address,
            )
            allocation = resource_module.build_allocation_body(
                source_id=self._deployment.roc_station_id,
                destination=route.original_destination.address,
                logical_channel_tx=pair.logical_channel_tx,
                logical_channel_rx=pair.logical_channel_rx,
                link_id=data_link_id.value,
            )
            messages = (
                MessageEncodeRequest(body=announcement, correlation=Correlation()),
                MessageEncodeRequest(body=allocation, correlation=Correlation()),
            )
            state = state.evolve(
                lc_allocations=resource_module.upsert_allocation(
                    state.lc_allocations,
                    resource_module.allocation_from_type4(
                        body=allocation,
                        owner_station_id_value=self.local_station_id.value,
                        peer_station_id_value=session_plan.first_hop_peer.value,
                        transmit_frame_ordinal=transmit_frame,
                        tdma_channel=pair.tdma_channel,
                        issued_by_local=True,
                    ),
                )
            )
            context = state.transfer(transfer_id)
            if context is not None:
                state = state.with_transfer(
                    self._advanced_context(
                        context,
                        phase=TransferPhase.AWAITING_RESOURCE,
                        link_id=data_link_id.value,
                        physical_channel_name=pair.tx_physical_channel.name,
                        logical_channel_tx=pair.logical_channel_tx,
                        logical_channel_rx=pair.logical_channel_rx,
                    )
                )

        correlation = Correlation(transfer_id=transfer_id)
        intent = ScheduledTxIntent(
            intent_id=self._id_source.new_event_id(),
            kind="ENCODE",
            transmit_at_nanoseconds=slot.start_nanoseconds,
            channel_name=channel.name,
            correlation_transfer_id=transfer_id.value,
            encode_request=LinkPacketEncodeRequest(
                link_id=link_id.value, messages=messages, correlation=correlation
            ),
        )
        prepared = state.evolve(
            outstanding_request=OutstandingResourceRequest(
                transfer_id=transfer_id,
                attempt_count=attempt_count,
                timer_id=None,
                route=route,
                terminal_capabilities=resource_module.SUPPORTED_TERMINAL_CAPABILITIES,
                priority=resource_module.CANONICAL_PRIORITY,
                physical_channel_name=channel.name,
                last_tx_command_id=None,
            )
        )
        intent_errors: list = []
        return Ok(
            self._defer_or_emit(
                prepared,
                intent=intent,
                causation_event_id=causation_event_id,
                commands=commands,
                errors=intent_errors,
            )
        )

    # --- Session 수명 (`MacPort.control_session`) ---

    def control_session(self, request: SessionControlRequest) -> CoordinatorPlan:
        state = self.state
        if not isinstance(request, SessionControlRequest):
            return CoordinatorPlan(
                result=_event_invalid("SessionControlRequest가 필요합니다"),
                next_state=state,
                batch=None,
            )
        if request.kind is SessionControlKind.OPEN:
            handle = request.session_handle or self._id_source.new_session_handle()
            if state.rx_session(handle.value) is not None:
                return CoordinatorPlan(
                    result=_event_invalid("이미 열린 session_handle입니다"),
                    next_state=state,
                    batch=None,
                )
            return CoordinatorPlan(
                result=Ok(
                    SessionControlResult(
                        kind=request.kind, session_handle=handle, active=True
                    )
                ),
                next_state=state,
                batch=None,
            )
        handle = request.session_handle
        record = state.rx_session(handle.value)
        active = record is not None
        if request.kind is SessionControlKind.KEEP:
            return CoordinatorPlan(
                result=Ok(
                    SessionControlResult(
                        kind=request.kind, session_handle=handle, active=active
                    )
                ),
                next_state=state,
                batch=None,
            )
        return CoordinatorPlan(
            result=Ok(
                SessionControlResult(kind=request.kind, session_handle=handle, active=False)
            ),
            next_state=state.without_rx_session(handle.value),
            batch=None,
        )

    # --- 수신 Packet 판정 (`MacPort.handle_recovered_packet`) ---

    def handle_recovered_packet(self, recovered: RecoveredPacketSet) -> CoordinatorPlan:
        state = self.state
        if not isinstance(recovered, RecoveredPacketSet):
            return CoordinatorPlan(
                result=_event_invalid("RecoveredPacketSet이 필요합니다"),
                next_state=state,
                batch=None,
            )
        if recovered.status is PhyRecoveryStatus.NO_BURST_DETECTED or not recovered.packets:
            return CoordinatorPlan(
                result=_mac_error(
                    ErrorCode.UNSUPPORTED_REQUEST,
                    ErrorCategory.PROTOCOL,
                    "복원된 Packet이 없습니다",
                    MacFailureReason.EVENT_INVALID,
                    None,
                    channel=recovered.channel.name,
                    status=recovered.status.value,
                ),
                next_state=state,
                batch=None,
            )
        packet = recovered.packets[0]
        link_id = self._link_id_for(packet.link_id.value)
        if link_id.is_err:
            return CoordinatorPlan(
                result=Ok(
                    PacketDecodeAction(
                        packet=packet,
                        accepted=False,
                        reason_code=MacReasonCode.UNSUPPORTED_PROFILE,
                    )
                ),
                next_state=state,
                batch=None,
            )
        return CoordinatorPlan(
            result=Ok(PacketDecodeAction(packet=packet, accepted=True, reason_code=None)),
            next_state=state,
            batch=None,
        )

    # --- decoded Message 처리 (`MacPort.handle_message`) ---

    def handle_message(
        self, message_input: MacMessageInput, causation_event_id: str
    ) -> CoordinatorPlan:
        state = self.state
        if not isinstance(message_input, MacMessageInput):
            return CoordinatorPlan(
                result=_event_invalid("MacMessageInput이 필요합니다"),
                next_state=state,
                batch=None,
            )
        decoded = message_input.decoded
        channel = message_input.channel
        commands: list = []
        actions: list = []
        errors: list = []
        next_state = state

        next_state = self._handle_tbb_assembly_updates(
            next_state, decoded.tbb_assembly_updates, causation_event_id, commands
        )
        for verified in decoded.verified_tbbs:
            next_state, error = self._apply_verified_tbb(
                next_state, verified, causation_event_id, commands
            )
            if error is not None:
                errors.append(error)
                # 구조화 거부는 상태를 바꾸지 않고 evidence로 남긴다.
                continue
            next_state = self._plan_type0_transmission(
                next_state, causation_event_id, commands, errors
            )
            next_state = self.plan_frame_windows(
                next_state, causation_event_id, commands, errors
            )

        for index, message in enumerate(decoded.messages):
            handled = self._handle_decoded_message(
                next_state,
                message=message,
                decoded=decoded,
                channel=channel,
                delivery_index=len(actions),
                causation_event_id=causation_event_id,
                commands=commands,
                actions=actions,
                errors=errors,
            )
            next_state = handled

        result = errors[0] if errors else Ok(MacActionSet(actions=tuple(actions)))
        return self._finish(
            next_state, commands, causation_event_id, result
        )

    def _handle_decoded_message(
        self,
        state: MacState,
        *,
        message,
        decoded,
        channel: PhysicalChannel,
        delivery_index: int,
        causation_event_id: str,
        commands: list,
        actions: list,
        errors: list,
    ) -> MacState:
        body = message.body
        if isinstance(body, MediaAccessControlBody):
            return self._receive_type0(
                state, body, causation_event_id, commands, errors=errors
            )
        if isinstance(body, ResourceRequestBody):
            return self._receive_type90(
                state,
                body,
                decoded=decoded,
                channel=channel,
                causation_event_id=causation_event_id,
                errors=errors,
            )
        if isinstance(body, ResourceAllocationBody):
            return self._receive_type4(
                state,
                body,
                decoded=decoded,
                causation_event_id=causation_event_id,
                commands=commands,
                errors=errors,
            )
        if isinstance(body, AcknowledgeNackBody):
            return self._receive_type13(
                state, body, decoded=decoded, channel=channel,
                causation_event_id=causation_event_id, commands=commands,
            )
        if isinstance(body, (ShortDataAckBody, ShortDataNoAckBody)):
            return self._receive_short_data(
                state,
                message=message,
                body=body,
                decoded=decoded,
                channel=channel,
                delivery_index=delivery_index,
                causation_event_id=causation_event_id,
                commands=commands,
                actions=actions,
                errors=errors,
            )
        if isinstance(
            body, (DataStartFragmentBody, DataContinuationFragmentBody, DataEndFragmentBody)
        ):
            return self._receive_data_fragment(
                state,
                message=message,
                body=body,
                decoded=decoded,
                channel=channel,
                causation_event_id=causation_event_id,
                commands=commands,
                errors=errors,
            )
        actions.append(
            MacAction(
                kind=MacActionKind.NO_ACTION,
                packet_id=decoded.packet_id,
                details=Details.of(message_type=message.message_type),
            )
        )
        return state

    def _receive_type0(
        self,
        state: MacState,
        body: MediaAccessControlBody,
        causation_event_id: str,
        commands: list,
        *,
        errors: list | None = None,
    ) -> MacState:
        """MASS는 구성된 ROC와 다른 Source ID의 Type 0을 적용하지 않는다."""

        if self.is_control:
            return state
        if body.source_id.value != self._deployment.roc_station_id.value:
            if errors is not None:
                errors.append(
                    routing_module.unexpected_control_station_error(
                        "configured ROC와 다른 Source ID의 Type 0입니다",
                        observed_source_id=body.source_id.value,
                        observed_control_station_id=self._deployment.tbb_control_station_id,
                    )
                )
            return state
        if body.media_access_priority_level != 0:
            return state
        next_state, error_result = self._start_type0_lease(
            state,
            body,
            self._now_monotonic_nanoseconds(),
            causation_event_id,
            commands,
        )
        if error_result is not None and errors is not None:
            errors.append(error_result)
        return next_state

    def _receive_type90(
        self,
        state: MacState,
        body: ResourceRequestBody,
        *,
        decoded,
        channel,
        causation_event_id: str,
        errors: list | None = None,
    ) -> MacState:
        """유효 Type 90을 닫히지 않은 frame epoch 후보에 기록만 하고 Type 4를 즉시 만들지 않는다."""

        if not self.is_control:
            return state
        validated = routing_module.validate_incoming_resource_request(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            original_source=body.original_source_id,
            node_source=body.node_source_id,
            node_destination=body.node_destination,
            original_destination=body.original_destination,
        )
        if validated.is_err:
            if errors is not None:
                errors.append(validated)
            return state
        capability = resource_module.validate_terminal_capabilities(body.terminal_capabilities)
        if capability.is_err:
            if errors is not None:
                errors.append(capability)
            return state
        received = decoded.received_at.total_nanoseconds()
        position = position_at_nanoseconds(received)
        key = (position.minute_start_nanoseconds, position.tdma_frame_index)
        epoch = state.epoch(key)
        if epoch is None:
            epoch = ResourceRequestEpoch(
                minute_start_nanoseconds=position.minute_start_nanoseconds,
                tdma_frame_index=position.tdma_frame_index,
            )
        if epoch.closed:
            # 닫힌 epoch를 다시 열지 않고 stale/unmatched evidence로 기록한다.
            return state
        route = resource_module.data_session_route(
            original_source=body.original_source_id,
            original_destination=body.original_destination,
            node_source=body.node_source_id,
            node_destination=body.node_destination,
        )
        candidates = list(epoch.candidates)
        for index, candidate in enumerate(candidates):
            if candidate.node_source_id_value != body.node_source_id.value:
                continue
            same = (
                candidate.route == route
                and candidate.terminal_capabilities == body.terminal_capabilities
                and candidate.priority == body.priority
            )
            candidates[index] = ResourceRequestCandidate(
                node_source_id_value=candidate.node_source_id_value,
                received_at_nanoseconds=candidate.received_at_nanoseconds,
                physical_slot_index=candidate.physical_slot_index,
                route=candidate.route,
                terminal_capabilities=candidate.terminal_capabilities,
                priority=candidate.priority,
                duplicate_count=candidate.duplicate_count + 1,
                conflicting=candidate.conflicting or not same,
            )
            return state.with_epoch(
                ResourceRequestEpoch(
                    minute_start_nanoseconds=epoch.minute_start_nanoseconds,
                    tdma_frame_index=epoch.tdma_frame_index,
                    candidates=tuple(candidates),
                    open_rx_command_ids=epoch.open_rx_command_ids,
                    closed=epoch.closed,
                )
            )
        candidates.append(
            ResourceRequestCandidate(
                node_source_id_value=body.node_source_id.value,
                received_at_nanoseconds=received,
                physical_slot_index=position.physical_slot_index,
                route=route,
                terminal_capabilities=body.terminal_capabilities,
                priority=body.priority,
            )
        )
        return state.with_epoch(
            ResourceRequestEpoch(
                minute_start_nanoseconds=epoch.minute_start_nanoseconds,
                tdma_frame_index=epoch.tdma_frame_index,
                candidates=tuple(candidates),
                open_rx_command_ids=epoch.open_rx_command_ids,
                closed=epoch.closed,
            )
        )

    def _receive_type4(
        self,
        state: MacState,
        body: ResourceAllocationBody,
        *,
        decoded,
        causation_event_id: str,
        commands: list,
        errors: list | None = None,
    ) -> MacState:
        """자신의 Type 4는 LC 쌍을 즉시 `RESERVED`로 만들고, 구성 밖 station은 보수 예약한다."""

        received = decoded.received_at.total_nanoseconds()
        transmit_frame = frame_ordinal_at_nanoseconds(received)
        if body.source_id.value != self._deployment.roc_station_id.value:
            if errors is not None:
                errors.append(
                    routing_module.unexpected_control_station_error(
                        "configured ROC와 다른 Source ID의 Type 4입니다",
                        observed_source_id=body.source_id.value,
                        observed_control_station_id=self._deployment.tbb_control_station_id,
                    )
                )
            external = resource_module.allocation_from_type4(
                body=body,
                owner_station_id_value=body.source_id.value,
                peer_station_id_value=0,
                transmit_frame_ordinal=transmit_frame,
                tdma_channel=0,
                issued_by_local=False,
                external=True,
            )
            return state.evolve(
                lc_allocations=resource_module.upsert_allocation(
                    state.lc_allocations, external
                )
            )
        link_id = self._link_id_for(body.link_id.value)
        if link_id.is_err:
            if errors is not None:
                errors.append(link_id)
            return state
        destination = body.destination
        if (
            destination.kind is not AddressKind.ADDRESSED
            or destination.station_id.value != self.local_station_id.value
        ):
            # A valid grant to another station updates occupancy only. In
            # particular it must not consume our outstanding Type 90 request,
            # open a transfer, send an ACK, or grant us a TX allocation.
            if destination.kind is not AddressKind.ADDRESSED or resource_module.is_rejection(body):
                return state
            slot_map = self.active_slot_map(state)
            tx_slot = slot_map.slot_for_logical_channel(body.logical_channel_tx)
            rx_slot = slot_map.slot_for_logical_channel(body.logical_channel_rx)
            if tx_slot is None or rx_slot is None:
                return state
            external = resource_module.allocation_from_type4(
                body=body,
                owner_station_id_value=body.source_id.value,
                peer_station_id_value=destination.station_id.value,
                transmit_frame_ordinal=transmit_frame,
                tdma_channel=tx_slot.tdma_channel,
                issued_by_local=False,
                external=True,
            )
            return state.evolve(lc_allocations=resource_module.upsert_allocation(
                state.lc_allocations, external))
        outstanding = state.outstanding_request
        if outstanding is None:
            if resource_module.is_rejection(body):
                return state
            slot_map = self.active_slot_map(state)
            slot = slot_map.slot_for_logical_channel(body.logical_channel_tx)
            if slot is None:
                return state
            allocation = resource_module.allocation_from_type4(
                body=body,
                owner_station_id_value=body.source_id.value,
                peer_station_id_value=self.local_station_id.value,
                transmit_frame_ordinal=transmit_frame,
                tdma_channel=slot.tdma_channel,
                issued_by_local=False,
            )
            conflict = resource_module.conflicting_allocation(
                state.lc_allocations,
                (body.logical_channel_tx, body.logical_channel_rx),
                body.source_id.value,
                transmit_frame,
            )
            if conflict is not None:
                if errors is not None:
                    errors.append(transfer_module.lc_conflict_error())
                return state
            return state.evolve(
                lc_allocations=resource_module.upsert_allocation(
                    state.lc_allocations, allocation
                )
            )
        if resource_module.is_rejection(body):
            next_state = state.evolve(outstanding_request=None)
            if outstanding.timer_id is not None:
                commands.append(
                    self._new_command(
                        causation_event_id,
                        Correlation(transfer_id=outstanding.transfer_id),
                        CancelWakeupCommand(timer_id=outstanding.timer_id),
                    )
                )
                next_state = next_state.without_timer(outstanding.timer_id)
            return self._terminate_transfer(
                next_state,
                outstanding.transfer_id,
                _mac_error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "ROC가 자원 요청을 거부했습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    MacReasonCode.RESOURCE_BUSY,
                ),
                causation_event_id,
                commands,
            )

        slot_map = self.active_slot_map(state)
        tdma_channel = 0
        slot = slot_map.slot_for_logical_channel(body.logical_channel_tx)
        if slot is not None:
            tdma_channel = slot.tdma_channel
        allocation = resource_module.allocation_from_type4(
            body=body,
            owner_station_id_value=self.local_station_id.value,
            peer_station_id_value=body.source_id.value,
            transmit_frame_ordinal=transmit_frame,
            tdma_channel=tdma_channel,
            issued_by_local=False,
        )
        conflict = resource_module.conflicting_allocation(
            state.lc_allocations,
            (body.logical_channel_tx, body.logical_channel_rx),
            self.local_station_id.value,
            transmit_frame,
        )
        next_state = state.evolve(outstanding_request=None)
        if outstanding.timer_id is not None:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(transfer_id=outstanding.transfer_id),
                    CancelWakeupCommand(timer_id=outstanding.timer_id),
                )
            )
            next_state = next_state.without_timer(outstanding.timer_id)
        if conflict is not None:
            return self._terminate_transfer(
                next_state,
                outstanding.transfer_id,
                transfer_module.lc_conflict_error(),
                causation_event_id,
                commands,
            )
        next_state = next_state.evolve(
            lc_allocations=resource_module.upsert_allocation(
                next_state.lc_allocations, allocation
            )
        )
        context = next_state.transfer(outstanding.transfer_id)
        if context is None:
            return next_state
        session_handle = self._id_source.new_session_handle()
        return next_state.with_transfer(
            TransferContext(
                transfer_id=context.transfer_id,
                role=context.role,
                phase=TransferPhase.BUILDING,
                route=context.route,
                ack_required=context.ack_required,
                presentation_size=context.presentation_size,
                public_state=context.public_state,
                session_handle=session_handle,
                link_id=body.link_id,
                physical_channel_name=context.physical_channel_name,
                logical_channel_tx=body.logical_channel_tx,
                logical_channel_rx=body.logical_channel_rx,
                total_fragments=context.total_fragments,
                next_fragment_index=context.next_fragment_index,
                current_frame_fragment_count=context.current_frame_fragment_count,
            )
        )

    def _receive_type13(
        self, state: MacState, body: AcknowledgeNackBody, *, decoded, channel, causation_event_id: str, commands: list
    ) -> MacState:
        """Short Data ACK와 Data Session ACK/NACK를 상관한다."""

        destination = body.destination
        if (
            destination.kind is not AddressKind.ADDRESSED
            or destination.station_id.value != self.local_station_id.value
        ):
            return state
        peer = body.source_id.value
        for context in state.transfers:
            peer_address = (
                context.route.node_destination.address
                if context.route.node_destination is not None
                else context.route.original_destination.address
            )
            if (
                peer_address.kind is not AddressKind.ADDRESSED
                or peer_address.station_id.value != peer
            ):
                continue
            if context.role is TransferRole.LOCAL_SHORT_DATA:
                if context.phase is not TransferPhase.WAITING_ACK:
                    continue
                if not ack_module.is_full_ack(body):
                    continue
                return self._complete_transfer(
                    state,
                    context.transfer_id,
                    MacCompletionReason.FINAL_PEER_ACKNOWLEDGED,
                    causation_event_id,
                    commands,
                )
            if (
                context.role is not TransferRole.LOCAL_DATA_SESSION
                or context.phase is TransferPhase.TERMINAL
                or not context.frame_ledger
            ):
                continue
            return self._apply_session_ack(
                state, context, body, causation_event_id, commands
            )
        return state

    def _apply_session_ack(
        self, state: MacState, context: TransferContext, body: AcknowledgeNackBody, causation_event_id: str, commands: list
    ) -> MacState:
        """rolling ACK ledger를 갱신하고 필요한 선택 재전송을 계획한다."""

        entries = tuple(context.frame_ledger)
        if not entries:
            return state
        eligible = tuple(
            entry
            for entry in entries
            if not any(packet_id in context.pending_packet_ids for packet_id in entry.packet_ids)
            and not any(
                frame_index == entry.session_frame_index
                for frame_index, _position in context.retry_fragment_keys
            )
        )
        if not eligible:
            return state
        latest = eligible[-1]
        updated_entries = []
        progress = []
        retries = []
        for entry in entries:
            if entry not in eligible:
                updated_entries.append(entry)
                continue
            mask_index = ack_module.mask_index_for_frame(
                latest.session_frame_index, entry.session_frame_index
            )
            if mask_index is None:
                updated_entries.append(entry)
                continue
            mask = body.ack_nack_masks[mask_index]
            nack_positions = frozenset(
                ack_module.nack_positions_from_mask(mask, entry.frame_fragment_count)
            )
            flags = list(entry.confirmed_positions)
            counts = list(entry.retransmission_counts)
            for position in range(1, entry.frame_fragment_count + 1):
                index = position - 1
                if flags[index]:
                    continue
                packet_id = PacketId(entry.packet_ids[index])
                if position in nack_positions:
                    if not transfer_module.may_retry_fragment(counts[index]):
                        return self._terminate_transfer(
                            state,
                            context.transfer_id,
                            transfer_module.retry_exhausted_error(
                                resource_request=False
                            ).error,
                            causation_event_id,
                            commands,
                        )
                    progress.append(
                        TransferPacketProgress(
                            packet_id=packet_id,
                            outcome=TransferPacketOutcome.DISCARDED,
                        )
                    )
                    counts[index] += 1
                    retries.append((entry, position))
                    continue
                progress.append(
                    TransferPacketProgress(
                        packet_id=packet_id,
                        outcome=TransferPacketOutcome.SUCCESS,
                    )
                )
                flags[index] = True
            updated_entries.append(
                ack_module.SessionFrameLedgerEntry(
                    session_frame_index=entry.session_frame_index,
                    frame_ordinal=entry.frame_ordinal,
                    frame_fragment_count=entry.frame_fragment_count,
                    packet_ids=entry.packet_ids,
                    retransmission_counts=tuple(counts),
                    fragment_confirmed=tuple(flags),
                    confirmed=all(flags),
                )
            )
        if progress:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(transfer_id=context.transfer_id),
                    InvokeLinkCommand(
                        request=LinkTransferControlRequest(
                            kind=TransferControlKind.PROGRESS,
                            transfer_id=context.transfer_id,
                            session_handle=context.session_handle,
                            packet_progress=tuple(progress),
                            correlation=Correlation(transfer_id=context.transfer_id),
                        )
                    ),
                )
            )
        updated = self._advanced_context(
            context,
            frame_ledger=ack_module.trimmed_ledger(tuple(updated_entries)),
            absent_ack_frames=0,
        )
        next_state = state.with_transfer(updated)
        if retries:
            planned = self._plan_data_fragment_retries(
                next_state,
                context=updated,
                retries=tuple(retries),
                causation_event_id=causation_event_id,
                commands=commands,
            )
            if planned.is_err:
                return self._terminate_transfer(
                    state,
                    context.transfer_id,
                    planned.error,
                    causation_event_id,
                    commands,
                )
            next_state = planned.value
            updated = next_state.transfer(context.transfer_id)
        if (
            updated.next_fragment_index >= updated.total_fragments
            and not updated.pending_packet_ids
            and not updated.retry_fragment_keys
            and updated.frame_ledger
            and all(entry.confirmed for entry in updated.frame_ledger)
        ):
            completion_reason = MacCompletionReason.FINAL_PEER_ACKNOWLEDGED
            destination = context.route.original_destination.address
            if (
                destination.kind is AddressKind.ADDRESSED
                and destination.station_id.value != body.source_id.value
            ):
                completion_reason = MacCompletionReason.ACCEPTED_BY_ROC_FOR_RELAY
            return self._complete_transfer(
                next_state,
                context.transfer_id,
                completion_reason,
                causation_event_id,
                commands,
            )
        return next_state

    def _receive_short_data(
        self,
        state: MacState,
        *,
        message,
        body,
        decoded,
        channel: PhysicalChannel,
        delivery_index: int,
        causation_event_id: str,
        commands: list,
        actions: list,
        errors: list | None = None,
    ) -> MacState:
        """Type 92/93 수신. bounded delivery ledger를 통과해야 전달한다."""

        ack_required = isinstance(body, ShortDataAckBody)
        if ack_required:
            checked = transfer_module.validate_received_retransmission_number(
                body.retransmission_number
            )
            if checked.is_err:
                # `4..254`와 `255`는 Type 93으로 바꾸거나 전달·ACK하지 않는다.
                if errors is not None:
                    errors.append(checked)
                return state
        destination = body.destination
        broadcast = destination.kind is AddressKind.BROADCAST
        if not broadcast and destination.station_id.value != self.local_station_id.value:
            return state
        if broadcast and ack_required:
            return state
        sender = body.source_id
        if not self._deployment.is_member(sender):
            return state
        route = message.route or resource_module.one_hop_route(sender, destination)
        sequential_id = message.sequential_id
        payload = message.presentation_data
        if sequential_id is None or payload is None:
            return state

        procedure = (
            RxDeliveryProcedure.SHORT_DATA_92 if ack_required else RxDeliveryProcedure.SHORT_DATA_93
        )
        key = ack_module.delivery_key(
            procedure, route.original_source.station_id, route.original_destination.address, sequential_id
        )
        digest = ack_module.payload_digest(payload)
        received = decoded.received_at.total_nanoseconds()
        frame_ordinal = frame_ordinal_at_nanoseconds(received)
        ledger = ack_module.prune_ledger(state.delivery_ledger, frame_ordinal)
        record = None
        for item in ledger:
            if item.key == key:
                record = item
                break

        retransmission = body.retransmission_number if ack_required else None
        if record is not None:
            if not ack_module.matches_payload(record, len(payload), digest):
                if errors is not None:
                    errors.append(ack_module.sequential_conflict_error())
                return state
            updated = ack_module.with_observed_retransmission(record, retransmission)
            # 동일 key·length·digest는 Service payload 전달을 억제한다.
            if (
                updated.state is DeliveryRecordState.LIVE
                and updated.replay_context is not None
                and updated.replay_command_id is None
            ):
                updated = replace(updated, ack_state=AckPendingState.ACK_PENDING)
            return state.evolve(delivery_ledger=ledger).with_ledger_record(updated)

        if not ack_module.ledger_has_capacity(ledger):
            if errors is not None:
                errors.append(ack_module.capacity_error())
            return state.evolve(delivery_ledger=ledger)

        pending_key = (decoded.packet_id.value, delivery_index)
        pending = ack_module.new_pending_record(
            key=key,
            procedure=procedure,
            payload_length=len(payload),
            digest=digest,
            first_received_frame_ordinal=frame_ordinal,
            highest_retransmission_number=retransmission,
            pending_delivery_key=pending_key,
        )
        actions.append(
            MacAction(
                kind=MacActionKind.DELIVER_PRESENTATION_DATA,
                packet_id=decoded.packet_id,
                session_handle=None,
                presentation_data=payload,
                route=route,
                details=Details.of(message_type=message.message_type),
            )
        )
        next_state = state.evolve(delivery_ledger=ledger).with_ledger_record(pending)
        return next_state.evolve(
            delivery_pending=next_state.delivery_pending
            + ((pending_key, key, channel.name, sender.value, broadcast),)
        )

    def _receive_data_fragment(
        self,
        state: MacState,
        *,
        message,
        body,
        decoded,
        channel: PhysicalChannel,
        causation_event_id: str,
        commands: list,
        errors: list | None = None,
    ) -> MacState:
        """수신 Type 74/75/76은 ACTIVE Rx DC·peer·route가 맞을 때만 Link로 넘긴다."""

        if decoded.link_id.value != self._profile.data_link_id:
            if errors is not None:
                errors.append(transfer_module.unsupported_profile_error(decoded.link_id.value))
            return state
        received = decoded.received_at.total_nanoseconds()
        frame_ordinal = frame_ordinal_at_nanoseconds(received)
        position = position_at_nanoseconds(received)
        slot_map = self.active_slot_map(state)
        slot = slot_map.slot_at(channel, position.tdma_channel, position.slot_in_frame)
        if slot is None or slot.function is not SlotFunction.DC:
            if errors is not None:
                errors.append(_event_invalid("Data Session fragment was received outside an assigned DC"))
            return state
        destination = body.destination
        if (
            destination.kind is not AddressKind.ADDRESSED
            or destination.station_id.value != self.local_station_id.value
        ):
            if errors is not None:
                errors.append(_event_invalid("Data Session destination does not match the local endpoint"))
            return state
        if body.session_id != 0:
            if errors is not None:
                errors.append(_event_invalid("Stage 4 Data Session ID must be zero"))
            return state
        peer = body.source_id
        if not self._deployment.is_member(peer):
            if errors is not None:
                errors.append(
                    routing_module.unexpected_control_station_error(
                        "Data Session peer is outside the configured group",
                        peer_station_id=peer.value,
                    )
                )
            return state
        allocation = None
        participants = {self.local_station_id.value, peer.value}
        for item in state.lc_allocations:
            if item.tdma_channel != position.tdma_channel:
                continue
            if item.logical_channel_tx != slot.logical_channel:
                continue
            if resource_module.allocation_state_at(item, frame_ordinal) is not LcState.ACTIVE:
                continue
            if {item.owner_station_id_value, item.peer_station_id_value} != participants:
                continue
            allocation = item
            break
        if allocation is None:
            if errors is not None:
                errors.append(
                    _event_invalid(
                        "No ACTIVE Rx DC allocation matches the Data Session fragment",
                        logical_channel=slot.logical_channel,
                    )
                )
            return state
        if message.route is not None:
            route_source = (
                message.route.node_source.station_id
                if message.route.node_source is not None
                else message.route.original_source.station_id
            )
            route_destination = (
                message.route.node_destination.address
                if message.route.node_destination is not None
                else message.route.original_destination.address
            )
            if (
                route_source.value != peer.value
                or route_destination.kind is not AddressKind.ADDRESSED
                or route_destination.station_id.value != self.local_station_id.value
            ):
                if errors is not None:
                    errors.append(
                        _event_invalid(
                            "Data Session fragment route does not match the allocation peer/local route"
                        )
                    )
                return state
        chain = state.request_chain(peer.value) if self.is_control else None
        route = (
            chain.route
            if chain is not None
            else message.route
            or resource_module.data_session_route(
                original_source=peer,
                original_destination=destination,
                node_source=peer,
                node_destination=destination,
            )
        )
        fragment_position = (
            body.fragment_position_in_frame
            if isinstance(body, DataContinuationFragmentBody)
            else (body.frame_fragment_count if isinstance(body, DataEndFragmentBody) else 1)
        )
        terminal_observed = (
            isinstance(body, DataEndFragmentBody)
            or body.frame_fragment_count == 1
        )
        fragment_key = (
            body.global_fragment_number,
            body.frame_fragment_count,
            fragment_position,
            body.fragment_payload,
        )
        session = state.rx_session_for_channel(slot.logical_channel)
        if session is None:
            if not isinstance(body, DataStartFragmentBody):
                return state
            handle = self._id_source.new_session_handle()
            command = self._new_command(
                causation_event_id,
                Correlation(session_handle=handle),
                InvokeLinkCommand(
                    request=LinkReassemblyControlRequest(
                        kind=ReassemblyControlKind.OPEN,
                        session_handle=handle,
                        route=route,
                        correlation=Correlation(session_handle=handle),
                    )
                ),
            )
            commands.append(command)
            # `DR-STAGE5-029`: request chain은 data 관측 뒤 중복 Type 90을 stale
            # evidence로만 기록하도록 설계됐지만 `data_observed`가 세워지지 않아 후속
            # inbound session이 영구히 거부됐다. 첫 Data start fragment 관측 시점에
            # 설계된 flag를 세운다.
            chain = state.request_chain(peer.value)
            if chain is not None and not chain.data_observed:
                state = state.with_request_chain(replace(chain, data_observed=True))
            return state.with_rx_session(
                RxSessionContext(
                    session_handle=handle,
                    route=route,
                    peer_station_id_value=peer.value,
                    logical_channel_rx=slot.logical_channel,
                    opened=False,
                    open_command_id=command.command_id,
                    last_frame_ordinal=frame_ordinal,
                    pending_packet_id=decoded.packet_id,
                    pending_link_id=decoded.link_id,
                    pending_received_at=decoded.received_at,
                    pending_message=message,
                    seen_packet_id_values=(decoded.packet_id.value,),
                    seen_fragment_keys=(fragment_key,),
                    frame_receptions=(
                        RxFrameReception(
                            session_frame_index=0,
                            frame_fragment_count=body.frame_fragment_count,
                            received_positions=(fragment_position,),
                            terminal_observed=terminal_observed,
                        ),
                    ),
                )
            )
        if not session.opened:
            # OPEN 성공 전에는 fragment를 넘기지 않는다.
            return state
        if (
            decoded.packet_id.value in session.seen_packet_id_values
            or fragment_key in session.seen_fragment_keys
        ):
            return state
        target_frame_index, frame_receptions = self._record_rx_position(
            session,
            frame_fragment_count=body.frame_fragment_count,
            position=fragment_position,
            terminal_observed=terminal_observed,
        )
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(session_handle=session.session_handle, packet_id=decoded.packet_id),
                InvokeLinkCommand(
                    request=LinkFragmentProcessRequest(
                        packet_id=decoded.packet_id,
                        link_id=decoded.link_id,
                        received_at=decoded.received_at,
                        session_handle=session.session_handle,
                        session_frame_index=target_frame_index,
                        route=session.route,
                        message=message,
                    )
                ),
            )
        )
        return state.with_rx_session(
            replace(
                session,
                last_frame_ordinal=frame_ordinal,
                seen_packet_id_values=(
                    session.seen_packet_id_values + (decoded.packet_id.value,)
                )[-64:],
                seen_fragment_keys=(session.seen_fragment_keys + (fragment_key,))[-64:],
                frame_receptions=frame_receptions,
            )
        )

    @staticmethod
    def _record_rx_position(
        session: RxSessionContext,
        *,
        frame_fragment_count: int,
        position: int,
        terminal_observed: bool,
    ) -> tuple[int, tuple[RxFrameReception, ...]]:
        """늦은 retry position을 최근 미완성 frame에 귀속하고 최근 3개만 보존한다."""

        target = session.session_frame_index
        retry_candidates = tuple(
            record
            for record in session.frame_receptions
            if record.terminal_observed
            and not record.complete
            and record.frame_fragment_count == frame_fragment_count
            and position not in record.received_positions
        )
        if retry_candidates:
            target = min(item.session_frame_index for item in retry_candidates)
        records = list(session.frame_receptions)
        existing = next(
            (item for item in records if item.session_frame_index == target), None
        )
        if existing is None:
            replacement = RxFrameReception(
                session_frame_index=target,
                frame_fragment_count=frame_fragment_count,
                received_positions=(position,),
                terminal_observed=terminal_observed,
            )
            records.append(replacement)
        else:
            replacement = RxFrameReception(
                session_frame_index=existing.session_frame_index,
                frame_fragment_count=existing.frame_fragment_count,
                received_positions=tuple(
                    sorted(set(existing.received_positions + (position,)))
                ),
                terminal_observed=existing.terminal_observed or terminal_observed,
            )
            records = [replacement if item is existing else item for item in records]
        records.sort(key=lambda item: item.session_frame_index)
        return target, tuple(records[-3:])

    # --- terminal helper ---

    def _terminate_transfer(
        self, state: MacState, transfer_id, error, causation_event_id: str, commands: list
    ) -> MacState:
        context = state.transfer(transfer_id)
        if context is None or context.phase is TransferPhase.TERMINAL:
            return state
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(transfer_id=transfer_id),
                InvokeLinkCommand(
                    request=LinkTransferControlRequest(
                        kind=TransferControlKind.FAIL,
                        transfer_id=transfer_id,
                        session_handle=None,
                        packet_progress=(),
                        correlation=Correlation(transfer_id=transfer_id),
                    )
                ),
            )
        )
        if context.relay_source_transfer_id is None:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(transfer_id=transfer_id),
                    PublishTransferStateCommand(
                        transfer_id=transfer_id,
                        state=TransferState.FAILED,
                        error=error.error if hasattr(error, "error") else error,
                    ),
                )
            )
        return state.with_transfer(
            self._terminal_context(context, TransferState.FAILED)
        ).evolve(
            local_fifo=tuple(
                item for item in state.local_fifo if item != transfer_id.value
            )
        )

    def _complete_transfer(
        self,
        state: MacState,
        transfer_id,
        reason: MacCompletionReason,
        causation_event_id: str,
        commands: list,
    ) -> MacState:
        context = state.transfer(transfer_id)
        if context is None or context.phase is TransferPhase.TERMINAL:
            return state
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(transfer_id=transfer_id),
                InvokeLinkCommand(
                    request=LinkTransferControlRequest(
                        kind=TransferControlKind.COMPLETE,
                        transfer_id=transfer_id,
                        session_handle=None,
                        packet_progress=(),
                        correlation=Correlation(transfer_id=transfer_id),
                    )
                ),
            )
        )
        if context.relay_source_transfer_id is None:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(transfer_id=transfer_id),
                    PublishTransferStateCommand(
                        transfer_id=transfer_id,
                        state=TransferState.COMPLETED,
                        error=None,
                        completion_reason=reason,
                    ),
                )
            )
        next_state = state.with_transfer(
            self._terminal_context(context, TransferState.COMPLETED)
        ).evolve(
            local_fifo=tuple(item for item in state.local_fifo if item != transfer_id.value)
        )
        if context.ack_timer_id is not None:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(transfer_id=transfer_id),
                    CancelWakeupCommand(timer_id=context.ack_timer_id),
                )
            )
            next_state = next_state.without_timer(context.ack_timer_id)
        return next_state

    @staticmethod
    def _terminal_context(context: TransferContext, public_state: TransferState) -> TransferContext:
        return TransferContext(
            transfer_id=context.transfer_id,
            role=context.role,
            phase=TransferPhase.TERMINAL,
            route=context.route,
            ack_required=context.ack_required,
            presentation_size=context.presentation_size,
            public_state=public_state,
            session_handle=context.session_handle,
            link_id=context.link_id,
            physical_channel_name=context.physical_channel_name,
            retransmission_number=context.retransmission_number,
            session_frame_index=context.session_frame_index,
            logical_channel_tx=context.logical_channel_tx,
            logical_channel_rx=context.logical_channel_rx,
            ack_timer_id=None,
            pending_packet_ids=(),
            frame_ledger=context.frame_ledger,
            absent_ack_frames=context.absent_ack_frames,
            total_fragments=context.total_fragments,
            next_fragment_index=context.next_fragment_index,
            current_frame_fragment_count=context.current_frame_fragment_count,
            building_frame_packet_ids=context.building_frame_packet_ids,
            retry_fragment_keys=context.retry_fragment_keys,
            relay_source_transfer_id=context.relay_source_transfer_id,
            first_tx_accepted=context.first_tx_accepted,
        )

    @staticmethod
    def _advanced_context(context: TransferContext, **changes) -> TransferContext:
        values = dict(
            transfer_id=context.transfer_id,
            role=context.role,
            phase=context.phase,
            route=context.route,
            ack_required=context.ack_required,
            presentation_size=context.presentation_size,
            public_state=context.public_state,
            session_handle=context.session_handle,
            link_id=context.link_id,
            physical_channel_name=context.physical_channel_name,
            retransmission_number=context.retransmission_number,
            session_frame_index=context.session_frame_index,
            logical_channel_tx=context.logical_channel_tx,
            logical_channel_rx=context.logical_channel_rx,
            ack_timer_id=context.ack_timer_id,
            pending_packet_ids=context.pending_packet_ids,
            frame_ledger=context.frame_ledger,
            absent_ack_frames=context.absent_ack_frames,
            total_fragments=context.total_fragments,
            next_fragment_index=context.next_fragment_index,
            current_frame_fragment_count=context.current_frame_fragment_count,
            building_frame_packet_ids=context.building_frame_packet_ids,
            retry_fragment_keys=context.retry_fragment_keys,
            relay_source_transfer_id=context.relay_source_transfer_id,
            first_tx_accepted=context.first_tx_accepted,
        )
        values.update(changes)
        return TransferContext(**values)

    # --- MacExecutionPort event routing (`DR-STAGE4-MAC-012`) ---

    def handle_event(self, envelope: MacEventEnvelope) -> CoordinatorPlan:
        state = self.state
        if not isinstance(envelope, MacEventEnvelope):
            return CoordinatorPlan(
                result=_event_invalid("MacEventEnvelope가 필요합니다"),
                next_state=state,
                batch=None,
            )
        if envelope.event_id in state.recent_terminal_ids:
            # 축출 전 recent set의 ID 재입력은 unmatched이며 상태를 바꾸지 않는다.
            return CoordinatorPlan(
                result=_unmatched("이미 소비된 event ID입니다"), next_state=state, batch=None
            )
        payload = envelope.payload
        commands: list = []
        handler = self._EVENT_HANDLERS.get(type(payload))
        if handler is None:
            return CoordinatorPlan(
                result=_event_invalid("알 수 없는 MacEventPayload입니다"),
                next_state=state,
                batch=None,
            )
        outcome = handler(self, state, envelope, commands)
        if isinstance(outcome, CoordinatorPlan):
            return outcome
        next_state = self._continue_stop_cleanup(
            outcome, commands, envelope.event_id
        )
        if not commands:
            return CoordinatorPlan(
                result=Ok(EMPTY_MAC_BATCH_RECEIPT), next_state=next_state, batch=None
            )
        registered = self._register_pending(next_state, commands)
        if registered.is_err:
            return CoordinatorPlan(result=registered, next_state=self.state, batch=None)
        batch = self._batch(envelope.event_id, commands)
        return CoordinatorPlan(
            result=Ok(
                MacBatchReceipt(batch_id=batch.batch_id, command_ids=batch.command_ids())
            ),
            next_state=registered.value,
            batch=batch,
        )

    def _match_pending(self, state: MacState, envelope: MacEventEnvelope):
        """envelope의 `causation_command_id`, payload의 `command_id`와 pending ID가 같아야 한다."""

        command_id = envelope.causation_command_id
        if command_id is None:
            return None
        record = state.pending_command(command_id)
        if record is None:
            return None
        if getattr(envelope.payload, "command_id", None) != command_id:
            return None
        return record

    # --- clock/timer ---

    def _on_clock_advanced(self, state: MacState, envelope, commands: list):
        if state.lifecycle is EndpointLifecycle.INITIAL:
            return state
        errors: list = []
        next_state = self._expire_state(state, envelope.event_id, commands)
        next_state = self._release_due_tx_intents(
            next_state, envelope.event_id, commands, errors
        )
        next_state = self._advance_data_sessions(
            next_state, envelope.event_id, commands, errors
        )
        next_state = self.plan_frame_windows(
            next_state, envelope.event_id, commands, errors
        )
        next_state = self._plan_pending_acks(
            next_state, envelope.event_id, commands, errors=errors
        )
        if errors:
            return self._finish(
                next_state, commands, envelope.event_id, errors[0]
            )
        if self.is_control:
            next_state = self._plan_type0_transmission(
                next_state, envelope.event_id, commands, errors
            )
            next_state = self._next_bbsc_transmit(next_state, envelope.event_id, commands)
        if errors:
            return self._finish(next_state, commands, envelope.event_id, errors[0])
        return next_state

    def _schedule_data_session_ack(
        self,
        state: MacState,
        *,
        session: RxSessionContext,
        observed_nanoseconds: int,
        causation_event_id: str,
        commands: list,
        errors: list | None = None,
    ) -> MacState:
        """최근 세 수신 frame의 missing position을 Type 13 mask 2/1/0으로 예약한다."""

        if errors is None:
            errors = []

        frame = frame_ordinal_at_nanoseconds(observed_nanoseconds)
        allocation = next(
            (
                item
                for item in state.lc_allocations
                if item.logical_channel_tx == session.logical_channel_rx
                and item.activation_frame_ordinal <= frame <= item.last_valid_frame_ordinal
                and item.state is not LcState.EXTERNAL_RESERVED
            ),
            None,
        )
        if allocation is None:
            return state
        slot_map = self.active_slot_map(state)
        ack_slot = slot_map.slot_for_logical_channel(allocation.logical_channel_rx)
        if ack_slot is None or ack_slot.function is not SlotFunction.DSCH:
            return state
        link_id = self._link_id_for(self._profile.data_link_id)
        if link_id.is_err:
            return state
        now = self._now_utc_nanoseconds()
        candidates = tuple(
            item
            for item in enumerate_function_slots(
                slot_map=slot_map,
                function=SlotFunction.DSCH,
                physical_channel=ack_slot.physical_channel,
                from_utc_nanoseconds=max(now + self._profile.transmit_lead_nanoseconds, observed_nanoseconds),
                until_utc_nanoseconds=frame_ordinal_start_nanoseconds(
                    allocation.last_valid_frame_ordinal + 1
                ),
                transmit=True,
            )
            if item.logical_channel == allocation.logical_channel_rx
        )
        if not candidates:
            return state
        terminal_frames = tuple(
            item for item in session.frame_receptions if item.terminal_observed
        )
        if not terminal_frames:
            return state
        latest_frame_index = max(
            item.session_frame_index for item in terminal_frames
        )
        masks = [0, 0, 0]
        for reception in terminal_frames:
            mask_index = ack_module.mask_index_for_frame(
                latest_frame_index, reception.session_frame_index
            )
            if mask_index is None:
                continue
            missing = tuple(
                position
                for position in range(1, reception.frame_fragment_count + 1)
                if position not in reception.received_positions
            )
            masks[mask_index] = ack_module.build_frame_mask(
                missing, reception.frame_fragment_count
            )
        intent = ScheduledTxIntent(
            intent_id=self._id_source.new_event_id(),
            kind="ENCODE",
            transmit_at_nanoseconds=candidates[0].start_nanoseconds,
            channel_name=ack_slot.physical_channel.name,
            encode_request=LinkPacketEncodeRequest(
                link_id=link_id.value,
                messages=(
                    MessageEncodeRequest(
                        body=ack_module.build_ack_body(
                            source_id=self.local_station_id,
                            destination=Addressed(StationId(session.peer_station_id_value)),
                            masks=tuple(masks),
                        ),
                        correlation=Correlation(session_handle=session.session_handle),
                    ),
                ),
                correlation=Correlation(session_handle=session.session_handle),
            ),
        )
        return self._defer_or_emit(
            state,
            intent=intent,
            causation_event_id=causation_event_id,
            commands=commands,
            errors=errors,
        )

    def _advance_data_sessions(
        self, state: MacState, causation_event_id: str, commands: list, errors: list
    ) -> MacState:
        """ACTIVE DC의 다음 frame에 최대 14개 Data Session fragment build를 계획한다."""

        now = self._now_utc_nanoseconds()
        lead = self._profile.transmit_lead_nanoseconds
        next_state = state
        for context in state.transfers:
            if (
                context.role is not TransferRole.LOCAL_DATA_SESSION
                or context.phase is TransferPhase.TERMINAL
                or context.session_handle is None
                or context.logical_channel_tx is None
                or context.total_fragments <= context.next_fragment_index
                or context.current_frame_fragment_count
                or context.pending_packet_ids
                or context.retry_fragment_keys
            ):
                continue
            allocation = resource_module.find_allocation_for_tx(
                next_state.lc_allocations,
                context.logical_channel_tx,
                frame_ordinal_at_nanoseconds(now),
            )
            if allocation is None:
                continue
            target_frame = max(
                frame_ordinal_at_nanoseconds(now), allocation.activation_frame_ordinal
            )
            channel = PhysicalChannel(context.physical_channel_name)
            slot_map = self.active_slot_map(next_state)
            while target_frame <= allocation.last_valid_frame_ordinal:
                frame_start = frame_ordinal_start_nanoseconds(target_frame)
                candidates = tuple(
                    item
                    for item in enumerate_function_slots(
                        slot_map=slot_map,
                        function=SlotFunction.DC,
                        physical_channel=channel,
                        from_utc_nanoseconds=frame_start,
                        until_utc_nanoseconds=frame_start + FRAME_NANOSECONDS,
                        transmit=True,
                    )
                    if item.logical_channel == context.logical_channel_tx
                )
                if candidates and candidates[0].start_nanoseconds >= now + lead:
                    break
                target_frame += 1
            if target_frame > allocation.last_valid_frame_ordinal:
                continue
            remaining = context.total_fragments - context.next_fragment_index
            count = min(FRAGMENTS_PER_FRAME, remaining)
            selected = candidates[:count]
            if len(selected) != count:
                continue
            prepared_context = self._advanced_context(
                context,
                phase=TransferPhase.BUILDING,
                current_frame_fragment_count=count,
                next_fragment_index=context.next_fragment_index + count,
            )
            next_state = next_state.with_transfer(prepared_context)
            for position, slot in enumerate(selected, start=1):
                plan = TransmissionPlan(
                    transfer_id=context.transfer_id,
                    link_id=context.link_id,
                    channel=channel,
                    transmit_at=self._utc(slot.start_nanoseconds),
                    procedure=TransmissionProcedure.DATA_SESSION,
                    route=context.route,
                    session_handle=context.session_handle,
                )
                request = BuildPacketFromTransferCommand(
                    transfer_id=context.transfer_id,
                    plan=plan,
                    session_frame_index=context.session_frame_index,
                    frame_fragment_count=count,
                    fragment_position_in_frame=position,
                )
                next_state = self._defer_or_emit(
                    next_state,
                    intent=ScheduledTxIntent(
                        intent_id=self._id_source.new_event_id(),
                        kind="BUILD",
                        transmit_at_nanoseconds=slot.start_nanoseconds,
                        channel_name=channel.name,
                        correlation_transfer_id=context.transfer_id.value,
                        build_request=request,
                    ),
                    causation_event_id=causation_event_id,
                    commands=commands,
                    errors=errors,
                )
        return next_state

    def _plan_data_fragment_retries(
        self,
        state: MacState,
        *,
        context: TransferContext,
        retries: tuple[tuple[ack_module.SessionFrameLedgerEntry, int], ...],
        causation_event_id: str,
        commands: list,
    ):
        """NACK position만 원 frame identity로 가장 이른 다음 ACTIVE DC에 재계획한다."""

        if not retries:
            return Ok(state)
        now = self._now_utc_nanoseconds()
        lead = self._profile.transmit_lead_nanoseconds
        current_frame = frame_ordinal_at_nanoseconds(now)
        allocation = resource_module.find_allocation_for_tx(
            state.lc_allocations, context.logical_channel_tx, current_frame
        )
        if allocation is None:
            return transfer_module.slot_missed_error("DATA_SESSION_RETRY")
        slot_map = self.active_slot_map(state)
        channel = PhysicalChannel(context.physical_channel_name)
        candidates = []
        target_frame = max(current_frame, allocation.activation_frame_ordinal)
        while (
            target_frame <= allocation.last_valid_frame_ordinal
            and len(candidates) < len(retries)
        ):
            frame_start = frame_ordinal_start_nanoseconds(target_frame)
            candidates.extend(
                item
                for item in enumerate_function_slots(
                    slot_map=slot_map,
                    function=SlotFunction.DC,
                    physical_channel=channel,
                    from_utc_nanoseconds=frame_start,
                    until_utc_nanoseconds=frame_start + FRAME_NANOSECONDS,
                    transmit=True,
                )
                if item.logical_channel == context.logical_channel_tx
                and item.start_nanoseconds >= now + lead
            )
            target_frame += 1
        if len(candidates) < len(retries):
            return transfer_module.slot_missed_error("DATA_SESSION_RETRY")

        retry_keys = tuple(
            (entry.session_frame_index, position) for entry, position in retries
        )
        next_state = state.with_transfer(
            self._advanced_context(
                context,
                phase=TransferPhase.BUILDING,
                retry_fragment_keys=context.retry_fragment_keys + retry_keys,
            )
        )
        intent_errors: list = []
        for (entry, position), slot in zip(retries, candidates):
            plan = TransmissionPlan(
                transfer_id=context.transfer_id,
                link_id=context.link_id,
                channel=channel,
                transmit_at=self._utc(slot.start_nanoseconds),
                procedure=TransmissionProcedure.DATA_SESSION,
                route=context.route,
                session_handle=context.session_handle,
            )
            request = BuildPacketFromTransferCommand(
                transfer_id=context.transfer_id,
                plan=plan,
                session_frame_index=entry.session_frame_index,
                frame_fragment_count=entry.frame_fragment_count,
                fragment_position_in_frame=position,
            )
            next_state = self._defer_or_emit(
                next_state,
                intent=ScheduledTxIntent(
                    intent_id=self._id_source.new_event_id(),
                    kind="BUILD",
                    transmit_at_nanoseconds=slot.start_nanoseconds,
                    channel_name=channel.name,
                    correlation_transfer_id=context.transfer_id.value,
                    build_request=request,
                ),
                causation_event_id=causation_event_id,
                commands=commands,
                errors=intent_errors,
            )
        return Ok(next_state)

    def _on_wakeup_fired(self, state: MacState, envelope, commands: list):
        payload = envelope.payload
        record = state.timer(payload.timer_id)
        if record is None or record.purpose is not payload.purpose:
            # 이미 취소된 timer의 늦은 wakeup은 상태를 부활시키지 않는다.
            return state
        next_state = state.without_timer(payload.timer_id)
        purpose = payload.purpose
        if purpose is TimerPurpose.FRAME_BOUNDARY:
            errors: list = []
            next_state = self._on_data_ack_timeout(
                next_state, envelope.event_id, commands
            )
            next_state = self._expire_state(next_state, envelope.event_id, commands)
            next_state = self._drive_local_work(
                next_state, envelope.event_id, commands
            )
            next_state = self._release_due_tx_intents(
                next_state, envelope.event_id, commands, errors
            )
            next_state = self._advance_data_sessions(
                next_state, envelope.event_id, commands, errors
            )
            next_state = self.plan_frame_windows(
                next_state, envelope.event_id, commands, errors
            )
            next_state = self._plan_pending_acks(
                next_state, envelope.event_id, commands, errors=errors
            )
            if self.is_control:
                next_state = self._plan_type0_transmission(
                    next_state, envelope.event_id, commands, errors
                )
                next_state = self._next_bbsc_transmit(next_state, envelope.event_id, commands)
            next_state = self._schedule_frame_boundary(
                next_state, envelope.event_id, commands
            )
            if errors:
                return self._finish(next_state, commands, envelope.event_id, errors[0])
            return next_state
        if purpose is TimerPurpose.RETRY:
            errors: list = []
            next_state = self._release_due_tx_intents(
                next_state, envelope.event_id, commands, errors
            )
            next_state = self.plan_frame_windows(
                next_state, envelope.event_id, commands, errors
            )
            if errors:
                return self._finish(next_state, commands, envelope.event_id, errors[0])
            return next_state
        if purpose is TimerPurpose.RESOURCE_RESPONSE_TIMEOUT:
            return self._on_resource_timeout(next_state, envelope.event_id, commands)
        if purpose is TimerPurpose.SHORT_DATA_ACK_TIMEOUT:
            return self._on_short_data_ack_timeout(next_state, envelope.event_id, commands)
        if purpose is TimerPurpose.DATA_ACK_TIMEOUT:
            return self._on_data_ack_timeout(next_state, envelope.event_id, commands)
        if purpose is TimerPurpose.LEASE_EXPIRY:
            # lease 만료 시 SI·Short Data limit·system status override를 함께 제거한다.
            return next_state.evolve(type0_lease=None)
        if purpose is TimerPurpose.TBB_ASSEMBLY_TIMEOUT:
            return self._on_tbb_assembly_timeout(next_state, envelope.event_id, commands)
        if purpose is TimerPurpose.TBB_EXPIRY:
            return self._abort_on_tbb_expiry(next_state, envelope.event_id, commands)
        if purpose is TimerPurpose.STOP_DRAIN:
            return self._on_stop_drain(next_state, envelope.event_id, commands)
        return next_state

    def _expire_state(self, state: MacState, causation_event_id: str, commands: list) -> MacState:
        """만료한 LC 예약, delivery ledger record와 TBB 유효구간을 정리한다."""

        now = self._now_utc_nanoseconds()
        frame_ordinal = frame_ordinal_at_nanoseconds(now)
        next_state = state.evolve(
            lc_allocations=resource_module.prune_expired_allocations(
                state.lc_allocations, frame_ordinal
            ),
            delivery_ledger=ack_module.prune_ledger(state.delivery_ledger, frame_ordinal),
        )
        active = next_state.active_tbb
        if active is not None and now >= active.valid_until_nanoseconds:
            next_state = self._abort_on_tbb_expiry(next_state, causation_event_id, commands)
        return next_state

    def _abort_on_tbb_expiry(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """만료 시 아직 terminal이 아닌 transfer를 실패시키고 Link·LC를 남기지 않는다."""

        next_state = state.evolve(active_tbb=None, lc_allocations=())
        for context in state.transfers:
            if context.phase is TransferPhase.TERMINAL:
                continue
            next_state = self._terminate_transfer(
                next_state,
                context.transfer_id,
                transfer_module.tbb_expired_error(during_session=True),
                causation_event_id,
                commands,
            )
        for session in state.rx_sessions:
            commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(session_handle=session.session_handle),
                    InvokeLinkCommand(
                        request=LinkReassemblyControlRequest(
                            kind=ReassemblyControlKind.TIMEOUT,
                            session_handle=session.session_handle,
                            route=None,
                            correlation=Correlation(session_handle=session.session_handle),
                        )
                    ),
                )
            )
            next_state = next_state.without_rx_session(session.session_handle.value)
        return next_state

    def _on_tbb_assembly_timeout(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """deadline까지 검증되지 않으면 `TIMEOUT` 뒤 local candidate/timer를 제거한다."""

        candidate = state.tbb_candidate
        if candidate is None:
            return state
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(),
                InvokeLinkCommand(
                    request=TbbAssemblyControlRequest(
                        kind=TbbAssemblyControlKind.TIMEOUT,
                        target=TbbCandidateKey(
                            source_id=StationId(candidate.source_id_value),
                            control_station_id=candidate.control_station_id,
                            bulletin_board_version=candidate.bulletin_board_version,
                        ),
                        replacement=None,
                        correlation=Correlation(),
                    )
                ),
            )
        )
        # Link cleanup 결과가 성공하기 전에는 local ownership을 해제하지 않는다.
        return state

    def _on_resource_timeout(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """Type 90 timeout. `attempt_count<3`이면 다음 활성 SI의 허용 RAC에서 재송신한다."""

        outstanding = state.outstanding_request
        if outstanding is None:
            return state
        if not transfer_module.may_retry_resource_request(outstanding.attempt_count):
            return self._terminate_transfer(
                state.evolve(outstanding_request=None),
                outstanding.transfer_id,
                transfer_module.retry_exhausted_error(resource_request=True),
                causation_event_id,
                commands,
            )
        context = state.transfer(outstanding.transfer_id)
        if context is None:
            return state.evolve(outstanding_request=None)
        session_plan = routing_module.resolve_data_session_plan(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            route=context.route,
        )
        if session_plan.is_err:
            return self._terminate_transfer(
                state.evolve(outstanding_request=None),
                outstanding.transfer_id,
                session_plan.error,
                causation_event_id,
                commands,
            )
        restarted = self._start_resource_request(
            state.evolve(outstanding_request=None),
            transfer_id=outstanding.transfer_id,
            session_plan=session_plan.value,
            route=context.route,
            causation_event_id=causation_event_id,
            commands=commands,
            attempt_count=outstanding.attempt_count + 1,
        )
        if restarted.is_err:
            return self._terminate_transfer(
                state.evolve(outstanding_request=None),
                outstanding.transfer_id,
                restarted.error,
                causation_event_id,
                commands,
            )
        return restarted.value

    def _on_short_data_ack_timeout(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """ACK-required Type 92는 최초 송신 뒤 최대 3회 재송신한다."""

        for context in state.transfers:
            if context.role is not TransferRole.LOCAL_SHORT_DATA:
                continue
            if context.phase is not TransferPhase.WAITING_ACK:
                continue
            if not transfer_module.may_retry_short_data(context.retransmission_number):
                return self._terminate_transfer(
                    state,
                    context.transfer_id,
                    transfer_module.retry_exhausted_error(resource_request=False).error,
                    causation_event_id,
                    commands,
                )
            placement = routing_module.resolve_short_data_direction(
                profile=self._deployment,
                local_station_id=self.local_station_id,
                route=context.route,
                ack_required=True,
            )
            if placement.is_err:
                return self._terminate_transfer(
                    state, context.transfer_id, placement.error, causation_event_id, commands
                )
            requirement = TransferRequirement(
                transfer_id=context.transfer_id,
                route=context.route,
                ack_required=True,
                presentation_size=context.presentation_size,
                candidate=TransferKind.SHORT_DATA,
            )
            planned = self._plan_short_data_transmission(
                state,
                requirement=requirement,
                placement=placement.value,
                link_id=context.link_id,
                retransmission_number=context.retransmission_number + 1,
                causation_event_id=causation_event_id,
                commands=commands,
            )
            if planned.is_err:
                return self._terminate_transfer(
                    state, context.transfer_id, planned.error, causation_event_id, commands
                )
            return planned.value
        return state

    def _on_data_ack_timeout(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """1~2 frame 부재는 ACK로 간주하고, 3 frame 연속 부재는 `ACK_TIMEOUT`으로 닫는다."""

        next_state = state
        for context in state.transfers:
            if context.role is not TransferRole.LOCAL_DATA_SESSION:
                continue
            if context.phase is TransferPhase.TERMINAL:
                continue
            eligible = tuple(
                entry
                for entry in context.frame_ledger
                if not entry.confirmed
                and not any(
                    packet_id in context.pending_packet_ids
                    for packet_id in entry.packet_ids
                )
            )
            if not eligible:
                continue
            absent = context.absent_ack_frames + 1
            if absent < ACK_ABSENCE_LIMIT:
                next_state = next_state.with_transfer(
                    self._advanced_context(context, absent_ack_frames=absent)
                )
                continue
            next_state = self._terminate_transfer(
                next_state,
                context.transfer_id,
                transfer_module.ack_timeout_error().error,
                causation_event_id,
                commands,
            )
        return next_state

    def _on_stop_drain(self, state: MacState, causation_event_id: str, commands: list) -> MacState:
        cleanup_commands = []
        """`STOP_DRAIN` 만료 시 남은 dependency와 nonterminal transfer를 terminal 처리한다."""

        next_state = state
        for context in state.transfers:
            if context.phase is TransferPhase.TERMINAL:
                continue
            next_state = self._terminate_transfer(
                next_state,
                context.transfer_id,
                transfer_module.endpoint_stopping_error().error,
                causation_event_id,
                cleanup_commands,
            )
        for session in state.rx_sessions:
            cleanup_commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(session_handle=session.session_handle),
                    InvokeLinkCommand(
                        request=LinkReassemblyControlRequest(
                            kind=ReassemblyControlKind.ABORT,
                            session_handle=session.session_handle,
                            route=None,
                            correlation=Correlation(session_handle=session.session_handle),
                        )
                    ),
                )
            )
            next_state = next_state.without_rx_session(session.session_handle.value)
        for timer in state.timers:
            cleanup_commands.append(
                self._new_command(
                    causation_event_id,
                    Correlation(),
                    CancelWakeupCommand(timer_id=timer.timer_id),
                )
            )
            next_state = next_state.without_timer(timer.timer_id)
        for record in state.pending_commands:
            next_state = next_state.with_recent_terminal(record.command_id)
        return next_state.evolve(
            lifecycle=EndpointLifecycle.DRAINING,
            pending_commands=(),
            stop_drain_timer_id=None,
            stop_cleanup_commands=tuple(cleanup_commands),
        )

    # --- Link 결과 ---

    def _on_link_result(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending command와 상관되지 않는 Link 결과입니다"),
                next_state=state,
                batch=None,
            )
        result = envelope.payload.result
        expected = self._expected_link_result_type(record)
        if expected is not None and type(result) is not expected:
            # correlation이 같아도 request와 다른 성공 variant는 상태를 바꾸지 않는다.
            return CoordinatorPlan(
                result=_unmatched(
                    "원 Link request와 다른 성공 result variant입니다",
                    expected_result=expected.__name__,
                    observed_result=type(result).__name__,
                ),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        return self._apply_link_result(next_state, record, result, envelope, commands)

    @staticmethod
    def _expected_link_result_type(record: PendingCommandRecord):
        if record.payload_kind == "PublishTransmissionPlanCommand":
            return BuiltPacket
        if record.payload_kind != "InvokeLinkCommand":
            return None
        if not record.request_key or record.request_key[0] != "LINK":
            return None
        for request_type, result_type in LINK_REQUEST_TO_RESULT_TYPE.items():
            if request_type.__name__ == record.request_key[1]:
                return result_type
        return None

    def _apply_link_result(
        self, state: MacState, record, result, envelope, commands: list
    ) -> MacState | CoordinatorPlan:
        if isinstance(result, TbbEncodedPacketSet):
            canonical = self._profile.canonical_tbb
            if result.certificate_fingerprint != canonical.expected_certificate_fingerprint:
                # 실행 중 방어 검사 실패는 Packet을 송신하지 않고 닫는다.
                return state.evolve(tbb_broadcast=TbbBroadcastState())
            next_state, activation_error = self._activate_canonical_tbb(
                state,
                fingerprint=canonical.canonical_tbb_fingerprint,
                bulletin_board_version=canonical.bulletin_board_version,
                source_id=canonical.source_id,
                control_station_id=canonical.control_station_id,
            )
            if activation_error is not None:
                return state.evolve(tbb_broadcast=TbbBroadcastState())
            next_state = next_state.evolve(
                tbb_broadcast=TbbBroadcastState(
                    next_fragment_index=0,
                    fragment_count=len(result.packets),
                    pending_command_id=None,
                    encoded_packets=result.packets,
                    certificate_fingerprint=result.certificate_fingerprint,
                )
            )
            return self._next_bbsc_transmit(next_state, envelope.event_id, commands)

        if isinstance(result, BuiltPacket):
            return self._on_packet_built(state, record, result, envelope, commands)

        if isinstance(result, EncodedPacket):
            return self._on_control_packet_encoded(state, record, result, envelope, commands)

        if isinstance(result, TbbAssemblyControlResult):
            if result.kind is TbbAssemblyControlKind.TIMEOUT:
                return state.evolve(tbb_candidate=None)
            return state

        if isinstance(result, ReassemblyControlResult):
            if result.kind is ReassemblyControlKind.OPEN:
                session = state.rx_session(result.session_handle.value)
                if session is None:
                    return state
                if session.pending_message is not None:
                    commands.append(
                        self._new_command(
                            envelope.event_id,
                            Correlation(
                                session_handle=session.session_handle,
                                packet_id=session.pending_packet_id,
                            ),
                            InvokeLinkCommand(
                                request=LinkFragmentProcessRequest(
                                    packet_id=session.pending_packet_id,
                                    link_id=session.pending_link_id,
                                    received_at=session.pending_received_at,
                                    session_handle=session.session_handle,
                                    session_frame_index=session.session_frame_index,
                                    route=session.route,
                                    message=session.pending_message,
                                )
                            ),
                        )
                    )
                return state.with_rx_session(
                    RxSessionContext(
                        session_handle=session.session_handle,
                        route=session.route,
                        peer_station_id_value=session.peer_station_id_value,
                        logical_channel_rx=session.logical_channel_rx,
                        opened=True,
                        open_command_id=None,
                        session_frame_index=session.session_frame_index,
                        last_frame_ordinal=session.last_frame_ordinal,
                        seen_packet_id_values=session.seen_packet_id_values,
                        seen_fragment_keys=session.seen_fragment_keys,
                        frame_receptions=session.frame_receptions,
                    )
                )
            closing_session = state.rx_session(result.session_handle.value)
            next_state = state.without_rx_session(result.session_handle.value)
            if closing_session is not None:
                # `DR-STAGE5-029`: session 종료와 함께 그 peer의 request chain을 닫아
                # 단일 active Data Session slot을 다음 inbound 요청에 되돌린다.
                next_state = next_state.without_request_chain(
                    closing_session.peer_station_id_value
                )
            for relay in next_state.relay_records:
                next_state = self._maybe_start_relay_second_hop(
                    next_state,
                    relay_transfer_id=relay.relay_transfer_id,
                    causation_event_id=envelope.event_id,
                    commands=commands,
                )
            return next_state

        if isinstance(result, FragmentProcessResult):
            if result.status is FragmentProcessStatus.COMPLETE:
                return self._on_reassembly_complete(state, result, envelope, commands)
            if result.status is FragmentProcessStatus.INCOMPLETE:
                session = state.rx_session(result.session_handle.value)
                if session is None:
                    return state
                reception = next(
                    (
                        item
                        for item in session.frame_receptions
                        if item.session_frame_index == result.fragment.session_frame_index
                    ),
                    None,
                )
                if reception is None or not reception.terminal_observed:
                    return state
                frame = frame_ordinal_at_nanoseconds(self._now_utc_nanoseconds())
                advanced_session = replace(
                    session,
                    session_frame_index=max(
                        session.session_frame_index,
                        result.fragment.session_frame_index + 1,
                    ),
                    last_frame_ordinal=frame,
                )
                next_state = state.with_rx_session(advanced_session)
                allocation = next(
                    (
                        item
                        for item in next_state.lc_allocations
                        if item.logical_channel_tx == session.logical_channel_rx
                        and item.last_valid_frame_ordinal >= frame
                    ),
                    None,
                )
                if allocation is not None:
                    next_state = next_state.evolve(
                        lc_allocations=resource_module.upsert_allocation(
                            next_state.lc_allocations,
                            resource_module.renewed_allocation(allocation, frame),
                        )
                    )
                ack_errors: list = []
                next_state = self._schedule_data_session_ack(
                    next_state,
                    session=advanced_session,
                    observed_nanoseconds=self._now_utc_nanoseconds(),
                    causation_event_id=envelope.event_id,
                    commands=commands,
                    errors=ack_errors,
                )
                if ack_errors:
                    return self._finish(
                        next_state, commands, envelope.event_id, ack_errors[0]
                    )
                return next_state
            return state

        if isinstance(result, TransferRequirement):
            return self._on_relay_analysis(state, record, result, envelope, commands)

        return state

    def _on_packet_built(
        self, state: MacState, record, packet, envelope, commands: list
    ) -> MacState:
        """Stage 4 build-only branch 결과를 받아 `InvokePhyCommand`로 이어 간다."""

        context = state.transfer(packet.transfer_id)
        if context is None or context.phase is TransferPhase.TERMINAL:
            return state
        plan_time = None
        key = ("TX_TIME", record.command_id)
        for item in state.delivery_pending:
            if item[0] == key:
                plan_time = item[1]
                break
        if plan_time is None:
            return state
        plan = PhyTransmitPlan(
            packet_id=packet.packet_id,
            packet=packet.packet,
            link_id=packet.link_id,
            channel=PhysicalChannel(context.physical_channel_name),
            transmit_at=self._utc(plan_time),
        )
        commands.append(
            self._new_command(
                envelope.event_id,
                Correlation(transfer_id=packet.transfer_id, packet_id=packet.packet_id),
                InvokePhyCommand(request=plan),
            )
        )
        next_context = self._advanced_context(
            context,
            phase=TransferPhase.TRANSMITTING,
            pending_packet_ids=context.pending_packet_ids + (packet.packet_id.value,),
        )
        if (
            context.role is TransferRole.LOCAL_DATA_SESSION
            and len(record.request_key) == 5
            and record.request_key[2] is not None
        ):
            session_frame_index = record.request_key[2]
            frame_fragment_count = record.request_key[3]
            fragment_position = record.request_key[4]
            retry_key = (session_frame_index, fragment_position)
            existing = next(
                (
                    entry
                    for entry in context.frame_ledger
                    if entry.session_frame_index == session_frame_index
                ),
                None,
            )
            if retry_key in context.retry_fragment_keys and existing is not None:
                packet_ids = list(existing.packet_ids)
                packet_ids[fragment_position - 1] = packet.packet_id.value
                replacement = ack_module.SessionFrameLedgerEntry(
                    session_frame_index=existing.session_frame_index,
                    frame_ordinal=existing.frame_ordinal,
                    frame_fragment_count=existing.frame_fragment_count,
                    packet_ids=tuple(packet_ids),
                    retransmission_counts=existing.retransmission_counts,
                    fragment_confirmed=existing.confirmed_positions,
                    confirmed=existing.confirmed,
                )
                next_context = self._advanced_context(
                    next_context,
                    frame_ledger=tuple(
                        replacement if item is existing else item
                        for item in context.frame_ledger
                    ),
                )
            else:
                next_context = self._advanced_context(
                    next_context,
                    building_frame_packet_ids=(
                        context.building_frame_packet_ids + (packet.packet_id.value,)
                    ),
                )
                if fragment_position == frame_fragment_count:
                    packet_ids = next_context.building_frame_packet_ids
                    entry = ack_module.SessionFrameLedgerEntry(
                        session_frame_index=session_frame_index,
                        frame_ordinal=frame_ordinal_at_nanoseconds(plan_time),
                        frame_fragment_count=frame_fragment_count,
                        packet_ids=packet_ids,
                        retransmission_counts=(0,) * frame_fragment_count,
                        fragment_confirmed=(False,) * frame_fragment_count,
                    )
                    next_context = self._advanced_context(
                        next_context,
                        frame_ledger=ack_module.trimmed_ledger(
                            context.frame_ledger + (entry,)
                        ),
                        building_frame_packet_ids=(),
                    )
        return state.evolve(
            delivery_pending=tuple(item for item in state.delivery_pending if item[0] != key)
        ).with_transfer(next_context)

    def _on_control_packet_encoded(
        self, state: MacState, record, packet, envelope, commands: list
    ) -> MacState:
        """Type 0/4/13/90 제어 Packet을 계획한 slot에서 송신한다."""

        time_key = ("CTRL_TIME", record.command_id)
        channel_key = ("CTRL_CHANNEL", record.command_id)
        slot_time = None
        channel_name = None
        for item in state.delivery_pending:
            if item[0] == time_key:
                slot_time = item[1]
            elif item[0] == channel_key:
                channel_name = item[1]
        if slot_time is None or channel_name is None:
            return state
        plan = PhyTransmitPlan(
            packet_id=packet.packet_id,
            packet=packet.packet,
            link_id=packet.link_id,
            channel=PhysicalChannel(channel_name),
            transmit_at=self._utc(slot_time),
        )
        command = self._new_command(
            envelope.event_id,
            Correlation(packet_id=packet.packet_id),
            InvokePhyCommand(request=plan),
        )
        commands.append(command)
        resource_batch = next(
            (
                item
                for item in state.delivery_pending
                if item[0] == ("RESOURCE_BATCH", record.command_id)
            ),
            None,
        )
        next_state = state.evolve(
            delivery_pending=tuple(
                item
                for item in state.delivery_pending
                if item[0]
                not in (
                    time_key,
                    channel_key,
                    ("RESOURCE_BATCH", record.command_id),
                )
            )
        )
        if resource_batch is not None:
            next_state = next_state.evolve(
                delivery_pending=next_state.delivery_pending
                + ((("RESOURCE_BATCH", command.command_id), resource_batch[1]),)
            )
        if state.type0_tx_command_id == record.command_id:
            next_state = next_state.evolve(type0_tx_command_id=command.command_id)
        outstanding = next_state.outstanding_request
        if outstanding is not None and outstanding.last_tx_command_id == record.command_id:
            next_state = next_state.evolve(
                outstanding_request=OutstandingResourceRequest(
                    transfer_id=outstanding.transfer_id,
                    attempt_count=outstanding.attempt_count,
                    timer_id=outstanding.timer_id,
                    route=outstanding.route,
                    terminal_capabilities=outstanding.terminal_capabilities,
                    priority=outstanding.priority,
                    physical_channel_name=outstanding.physical_channel_name,
                    last_tx_command_id=command.command_id,
                )
            )
        for item in next_state.delivery_ledger:
            if item.replay_command_id == record.command_id:
                next_state = next_state.with_ledger_record(
                    DeliveryLedgerRecord(
                        key=item.key,
                        procedure=item.procedure,
                        state=item.state,
                        payload_length=item.payload_length,
                        payload_digest=item.payload_digest,
                        first_received_frame_ordinal=item.first_received_frame_ordinal,
                        completion_frame_ordinal=item.completion_frame_ordinal,
                        expires_after_frame=item.expires_after_frame,
                        highest_retransmission_number=item.highest_retransmission_number,
                        ack_state=item.ack_state,
                        replay_context=item.replay_context,
                        replay_command_id=command.command_id,
                        last_replay_scheduled_frame=item.last_replay_scheduled_frame,
                        pending_delivery_key=item.pending_delivery_key,
                    )
                )
                break
        return next_state

    def _on_reassembly_complete(
        self, state: MacState, result, envelope, commands: list
    ) -> MacState:
        """`COMPLETE` 뒤 idempotent CLOSE를 먼저 완료하고 delivery를 준비한다."""

        commands.append(
            self._new_command(
                envelope.event_id,
                Correlation(session_handle=result.session_handle),
                InvokeLinkCommand(
                    request=LinkReassemblyControlRequest(
                        kind=ReassemblyControlKind.CLOSE,
                        session_handle=result.session_handle,
                        route=None,
                        correlation=Correlation(session_handle=result.session_handle),
                    )
                ),
            )
        )
        session = state.rx_session(result.session_handle.value)
        if session is None or result.sequential_id is None or result.presentation_data is None:
            return state
        now = self._now_utc_nanoseconds()
        frame = frame_ordinal_at_nanoseconds(now)
        ledger = ack_module.prune_ledger(state.delivery_ledger, frame)
        key = ack_module.delivery_key(
            RxDeliveryProcedure.DATA_SESSION,
            result.route.original_source.station_id,
            result.route.original_destination.address,
            result.sequential_id,
        )
        digest = ack_module.payload_digest(result.presentation_data)
        allocation = next(
            (
                item
                for item in state.lc_allocations
                if item.logical_channel_tx == session.logical_channel_rx
                and item.last_valid_frame_ordinal >= frame
            ),
            None,
        )
        replay = None
        link_id = self._link_id_for(self._profile.data_link_id)
        if allocation is not None and link_id.is_ok:
            ack_slot = self.active_slot_map(state).slot_for_logical_channel(
                allocation.logical_channel_rx
            )
            if ack_slot is not None and ack_slot.function is SlotFunction.DSCH:
                replay = ack_module.build_data_session_replay_context(
                    route=resource_module.one_hop_route(
                        self.local_station_id,
                        Addressed(StationId(session.peer_station_id_value)),
                    ),
                    link_id=link_id.value,
                    physical_channel=ack_slot.physical_channel,
                    logical_channel=allocation.logical_channel_rx,
                    ack_body=ack_module.build_ack_body(
                        source_id=self.local_station_id,
                        destination=Addressed(StationId(session.peer_station_id_value)),
                    ),
                    resource_last_frame_ordinal=allocation.last_valid_frame_ordinal,
                )
        existing = next((item for item in ledger if item.key == key), None)
        next_state = state.evolve(delivery_ledger=ledger)
        if existing is not None:
            if not ack_module.matches_payload(existing, len(result.presentation_data), digest):
                return self._finish(
                    next_state,
                    commands,
                    envelope.event_id,
                    ack_module.sequential_conflict_error(),
                )
            if existing.state is DeliveryRecordState.LIVE and replay is not None:
                next_state = next_state.with_ledger_record(
                    replace(
                        existing,
                        ack_state=AckPendingState.ACK_PENDING,
                        replay_context=replay,
                        replay_command_id=None,
                    )
                )
            return next_state
        if not ack_module.ledger_has_capacity(ledger):
            return self._finish(
                next_state,
                commands,
                envelope.event_id,
                ack_module.capacity_error(),
            )
        is_relay = (
            self.is_control
            and result.route.original_destination.address.kind is AddressKind.ADDRESSED
            and result.route.original_destination.address.station_id.value
            != self.local_station_id.value
        )
        if is_relay:
            if len(next_state.relay_records) >= Stage4Limits.LOCAL_TRANSFER_FIFO:
                return self._finish(
                    next_state,
                    commands,
                    envelope.event_id,
                    transfer_module.resource_busy_error(),
                )
            relay_transfer_id = self._id_source.new_transfer_id()
            second_hop_route = routing_module.relay_second_hop_route(
                original_route=result.route,
                roc_station_id=self.local_station_id,
            )
            analyze = self._new_command(
                envelope.event_id,
                Correlation(transfer_id=relay_transfer_id),
                InvokeLinkCommand(
                    request=LinkAnalyzeRequest(
                        transfer_id=relay_transfer_id,
                        route=second_hop_route,
                        ack_required=True,
                        presentation_data=result.presentation_data,
                    )
                ),
            )
            commands.append(analyze)
            pending = ack_module.new_pending_record(
                key=key,
                procedure=RxDeliveryProcedure.DATA_SESSION,
                payload_length=len(result.presentation_data),
                digest=digest,
                first_received_frame_ordinal=frame,
                highest_retransmission_number=None,
                pending_delivery_key=None,
            )
            live = ack_module.promoted_live_record(
                pending,
                completion_frame_ordinal=frame,
                ack_state=(
                    AckPendingState.ACK_PENDING
                    if replay is not None
                    else AckPendingState.ACK_NONE
                ),
                replay_context=replay,
            )
            return next_state.with_ledger_record(live).with_relay_record(
                RelayRecord(
                    relay_transfer_id=relay_transfer_id,
                    original_route=result.route,
                    second_hop_route=second_hop_route,
                    presentation_data=result.presentation_data,
                    analyze_command_id=analyze.command_id,
                    first_hop_ledger_key=key,
                )
            )
        delivery = self._new_command(
            envelope.event_id,
            Correlation(session_handle=result.session_handle),
            DeliverPresentationDataCommand(
                session_handle=result.session_handle,
                route=result.route,
                presentation_data=result.presentation_data,
                received_at=self._time_source.now_utc(),
            ),
        )
        commands.append(delivery)
        pending_key = ("SESSION_DELIVERY", delivery.command_id)
        pending = ack_module.new_pending_record(
            key=key,
            procedure=RxDeliveryProcedure.DATA_SESSION,
            payload_length=len(result.presentation_data),
            digest=digest,
            first_received_frame_ordinal=frame,
            highest_retransmission_number=None,
            pending_delivery_key=pending_key,
        )
        next_state = next_state.with_ledger_record(pending)
        return next_state.evolve(
            delivery_pending=next_state.delivery_pending
            + ((pending_key, key, replay),)
        )

    def _on_relay_analysis(
        self, state: MacState, record, requirement, envelope, commands: list
    ) -> MacState:
        """분석 결과를 private relay-admission state에 commit한다.

        이 commit 순간에만 분석-only Link record의 cleanup 소유권이 Runtime에서 relay MAC으로
        넘어간다.
        """

        relay = state.relay_record(requirement.transfer_id.value)
        if relay is None:
            return state
        next_state = state.with_relay_record(
            RelayRecord(
                relay_transfer_id=relay.relay_transfer_id,
                original_route=relay.original_route,
                second_hop_route=relay.second_hop_route,
                presentation_data=relay.presentation_data,
                analyze_command_id=None,
                admitted=True,
                first_hop_ack_completed=relay.first_hop_ack_completed,
                first_hop_ledger_key=relay.first_hop_ledger_key,
                requirement=requirement,
                second_hop_started=relay.second_hop_started,
            )
        )
        return self._maybe_start_relay_second_hop(
            next_state,
            relay_transfer_id=requirement.transfer_id,
            causation_event_id=envelope.event_id,
            commands=commands,
        )

    def _maybe_start_relay_second_hop(
        self,
        state: MacState,
        *,
        relay_transfer_id,
        causation_event_id: str,
        commands: list,
    ) -> MacState:
        relay = state.relay_record(relay_transfer_id.value)
        if (
            relay is None
            or not relay.admitted
            or not relay.first_hop_ack_completed
            or relay.second_hop_started
            or relay.requirement is None
            or state.outstanding_request is not None
            or any(session.route == relay.original_route for session in state.rx_sessions)
        ):
            return state
        requirement = relay.requirement
        link_requirement = next(
            (
                item
                for item in requirement.profiles
                if item.link_id.value == self._profile.data_link_id
            ),
            None,
        )
        if link_requirement is None:
            return state
        destination = relay.second_hop_route.original_destination.address.station_id
        context = TransferContext(
            transfer_id=relay.relay_transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.AWAITING_RESOURCE,
            route=relay.second_hop_route,
            ack_required=True,
            presentation_size=requirement.presentation_size,
            public_state=TransferState.QUEUED,
            link_id=LinkId(self._profile.data_link_id),
            physical_channel_name=self._profile.upper_channel.name,
            total_fragments=link_requirement.data_session_fragment_count,
            relay_source_transfer_id=relay.relay_transfer_id.value,
        )
        prepared = state.with_transfer(context)
        started = self._start_resource_request(
            prepared,
            transfer_id=relay.relay_transfer_id,
            session_plan=routing_module.DataSessionPlan(
                direction=resource_module.Direction.ROC_TO_SHIP,
                relay_required=False,
                first_hop_peer=destination,
                final_destination=destination,
                request_physical_channel=self._profile.upper_channel,
                request_slot_function=SlotFunction.ASC,
            ),
            route=relay.second_hop_route,
            causation_event_id=causation_event_id,
            commands=commands,
        )
        if started.is_err:
            return state
        return started.value.with_relay_record(
            replace(relay, second_hop_started=True)
        )

    # --- PHY 결과 ---

    def _on_phy_tx_accepted(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending TX command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        if record.request_key and record.request_key[0] == "TX":
            if record.request_key[1] != envelope.payload.packet_id.value:
                return CoordinatorPlan(
                    result=_unmatched("TX event의 packet_id가 pending request와 다릅니다"),
                    next_state=state,
                    batch=None,
                )
        next_state = state.with_pending_command(
            PendingCommandRecord(
                command_id=record.command_id,
                command_class=record.command_class,
                payload_kind=record.payload_kind,
                accepted=True,
                request_key=record.request_key,
            )
        )
        packet_id_value = envelope.payload.packet_id.value
        for context in next_state.transfers:
            if packet_id_value not in context.pending_packet_ids:
                continue
            if context.first_tx_accepted:
                break
            if context.relay_source_transfer_id is None:
                commands.append(
                    self._new_command(
                        envelope.event_id,
                        Correlation(transfer_id=context.transfer_id),
                        PublishTransferStateCommand(
                            transfer_id=context.transfer_id, state=TransferState.TRANSMITTING
                        ),
                    )
                )
            next_state = next_state.with_transfer(
                self._advanced_context(
                    context, first_tx_accepted=True, public_state=TransferState.TRANSMITTING
                )
            )
            break
        return next_state

    def _on_phy_tx_completed(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending TX command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        next_state = self._complete_resource_batch(next_state, record.command_id)
        packet_id_value = envelope.payload.packet_id.value
        completed_monotonic = envelope.payload.completed_at.total_nanoseconds()
        completed_replay_key = next(
            (
                item.key
                for item in state.delivery_ledger
                if item.replay_command_id == record.command_id
            ),
            None,
        )

        if state.type0_tx_command_id == record.command_id:
            next_state = self._on_type0_transmitted(
                next_state, completed_monotonic, envelope.event_id, commands
            )
        outstanding = next_state.outstanding_request
        if (
            outstanding is not None
            and outstanding.timer_id is None
            and outstanding.last_tx_command_id == record.command_id
        ):
            if self.is_control:
                context = next_state.transfer(outstanding.transfer_id)
                if context is not None:
                    next_state = next_state.with_transfer(
                        self._advanced_context(
                            context,
                            phase=TransferPhase.BUILDING,
                            session_handle=self._id_source.new_session_handle(),
                        )
                    ).evolve(outstanding_request=None)
            else:
                next_state = self._arm_resource_timeout(
                    next_state, outstanding, completed_monotonic, envelope.event_id, commands
                )
        for context in next_state.transfers:
            if packet_id_value not in context.pending_packet_ids:
                continue
            remaining = tuple(
                item for item in context.pending_packet_ids if item != packet_id_value
            )
            if context.role is TransferRole.LOCAL_DATA_SESSION:
                if context.phase is TransferPhase.TERMINAL:
                    break
                retry_key = None
                for entry in context.frame_ledger:
                    for position, ledger_packet_id in enumerate(entry.packet_ids, start=1):
                        if ledger_packet_id != packet_id_value:
                            continue
                        candidate = (entry.session_frame_index, position)
                        if candidate in context.retry_fragment_keys:
                            retry_key = candidate
                        break
                    if retry_key is not None:
                        break
                if retry_key is not None:
                    retry_keys = tuple(
                        item for item in context.retry_fragment_keys if item != retry_key
                    )
                    phase = TransferPhase.TRANSMITTING
                    if not remaining and not retry_keys:
                        phase = (
                            TransferPhase.BUILDING
                            if context.next_fragment_index < context.total_fragments
                            else TransferPhase.WAITING_ACK
                        )
                    next_state = next_state.with_transfer(
                        self._advanced_context(
                            context,
                            phase=phase,
                            pending_packet_ids=remaining,
                            retry_fragment_keys=retry_keys,
                        )
                    )
                    break
                phase = TransferPhase.TRANSMITTING
                public_state = context.public_state
                frame_count = context.current_frame_fragment_count
                session_frame_index = context.session_frame_index
                if not remaining:
                    frame_count = 0
                    session_frame_index += 1
                    if context.next_fragment_index >= context.total_fragments:
                        phase = TransferPhase.WAITING_ACK
                        public_state = TransferState.WAITING_ACK
                        if context.relay_source_transfer_id is None:
                            commands.append(
                                self._new_command(
                                    envelope.event_id,
                                    Correlation(transfer_id=context.transfer_id),
                                    PublishTransferStateCommand(
                                        transfer_id=context.transfer_id,
                                        state=TransferState.WAITING_ACK,
                                    ),
                                )
                            )
                    else:
                        phase = TransferPhase.BUILDING
                        frame = frame_ordinal_at_nanoseconds(
                            self._now_utc_nanoseconds()
                        )
                        allocation = resource_module.find_allocation_for_tx(
                            next_state.lc_allocations,
                            context.logical_channel_tx,
                            frame,
                        )
                        if allocation is not None:
                            next_state = next_state.evolve(
                                lc_allocations=resource_module.upsert_allocation(
                                    next_state.lc_allocations,
                                    resource_module.renewed_allocation(allocation, frame),
                                )
                            )
                next_state = next_state.with_transfer(
                    self._advanced_context(
                        context,
                        phase=phase,
                        public_state=public_state,
                        pending_packet_ids=remaining,
                        current_frame_fragment_count=frame_count,
                        session_frame_index=session_frame_index,
                    )
                )
                break
            commands.append(
                self._new_command(
                    envelope.event_id,
                    Correlation(transfer_id=context.transfer_id),
                    InvokeLinkCommand(
                        request=LinkTransferControlRequest(
                            kind=TransferControlKind.PROGRESS,
                            transfer_id=context.transfer_id,
                            session_handle=None,
                            packet_progress=(
                                TransferPacketProgress(
                                    packet_id=envelope.payload.packet_id,
                                    outcome=TransferPacketOutcome.SUCCESS,
                                ),
                            ),
                            correlation=Correlation(transfer_id=context.transfer_id),
                        )
                    ),
                )
            )
            if context.ack_required:
                next_state = next_state.with_transfer(
                    self._advanced_context(
                        context,
                        phase=TransferPhase.WAITING_ACK,
                        public_state=TransferState.WAITING_ACK,
                        pending_packet_ids=remaining,
                    )
                )
                commands.append(
                    self._new_command(
                        envelope.event_id,
                        Correlation(transfer_id=context.transfer_id),
                        PublishTransferStateCommand(
                            transfer_id=context.transfer_id, state=TransferState.WAITING_ACK
                        ),
                    )
                )
                next_state = self._arm_short_data_ack_timeout(
                    next_state, context, completed_monotonic, envelope.event_id, commands
                )
            else:
                next_state = next_state.with_transfer(
                    self._advanced_context(context, pending_packet_ids=remaining)
                )
                if not remaining:
                    next_state = self._complete_transfer(
                        next_state,
                        context.transfer_id,
                        MacCompletionReason.ACK_NOT_REQUIRED_TX_COMPLETED,
                        envelope.event_id,
                        commands,
                    )
            break
        next_state = self._release_replay_command(next_state, record.command_id)
        if self.is_control and state.tbb_broadcast.pending_command_id == record.command_id:
            broadcast = next_state.tbb_broadcast
            next_state = next_state.evolve(
                tbb_broadcast=TbbBroadcastState(
                    next_fragment_index=broadcast.next_fragment_index,
                    fragment_count=broadcast.fragment_count,
                    pending_command_id=None,
                    encoded_packets=broadcast.encoded_packets,
                    certificate_fingerprint=broadcast.certificate_fingerprint,
                )
            )
            next_state = self._next_bbsc_transmit(next_state, envelope.event_id, commands)
        if completed_replay_key is not None:
            relay = next(
                (
                    item
                    for item in next_state.relay_records
                    if item.first_hop_ledger_key == completed_replay_key
                ),
                None,
            )
            if relay is not None:
                next_state = next_state.with_relay_record(
                    replace(relay, first_hop_ack_completed=True)
                )
                next_state = self._maybe_start_relay_second_hop(
                    next_state,
                    relay_transfer_id=relay.relay_transfer_id,
                    causation_event_id=envelope.event_id,
                    commands=commands,
                )
        return next_state

    def _release_broadcast_command(self, state: MacState, command_id: str) -> MacState:
        """실패한 TBB fragment 송신은 방송 순환을 멈추지 않고 다음 BBSC 기회를 다시 연다."""

        broadcast = state.tbb_broadcast
        if broadcast.pending_command_id != command_id:
            return state
        return state.evolve(
            tbb_broadcast=TbbBroadcastState(
                next_fragment_index=broadcast.next_fragment_index,
                fragment_count=broadcast.fragment_count,
                pending_command_id=None,
                encoded_packets=broadcast.encoded_packets,
                certificate_fingerprint=broadcast.certificate_fingerprint,
            )
        )

    def _release_replay_command(self, state: MacState, command_id: str) -> MacState:
        """재-ACK command가 terminal이 되면 record당 동시 replay 제한을 푼다."""

        for item in state.delivery_ledger:
            if item.replay_command_id != command_id:
                continue
            return state.with_ledger_record(
                DeliveryLedgerRecord(
                    key=item.key,
                    procedure=item.procedure,
                    state=item.state,
                    payload_length=item.payload_length,
                    payload_digest=item.payload_digest,
                    first_received_frame_ordinal=item.first_received_frame_ordinal,
                    completion_frame_ordinal=item.completion_frame_ordinal,
                    expires_after_frame=item.expires_after_frame,
                    highest_retransmission_number=item.highest_retransmission_number,
                    ack_state=AckPendingState.ACK_SENT,
                    replay_context=item.replay_context,
                    replay_command_id=None,
                    last_replay_scheduled_frame=item.last_replay_scheduled_frame,
                    pending_delivery_key=item.pending_delivery_key,
                )
            )
        return state

    @staticmethod
    def _resource_batch_station_ids(state: MacState, command_id: str) -> tuple[int, ...]:
        marker_key = ("RESOURCE_BATCH", command_id)
        marker = next(
            (item for item in state.delivery_pending if item[0] == marker_key), None
        )
        return () if marker is None else marker[1]

    def _complete_resource_batch(self, state: MacState, command_id: str) -> MacState:
        marker_key = ("RESOURCE_BATCH", command_id)
        return state.evolve(
            delivery_pending=tuple(
                item for item in state.delivery_pending if item[0] != marker_key
            )
        )

    def _rollback_resource_batch(self, state: MacState, command_id: str) -> MacState:
        station_ids = self._resource_batch_station_ids(state, command_id)
        next_state = self._complete_resource_batch(state, command_id)
        if not station_ids:
            return next_state
        for station_id in station_ids:
            next_state = next_state.without_request_chain(station_id)
        return next_state.evolve(
            lc_allocations=tuple(
                allocation
                for allocation in next_state.lc_allocations
                if not (
                    allocation.issued_by_local
                    and allocation.owner_station_id_value in station_ids
                )
            )
        )

    def _on_type0_transmitted(
        self, state: MacState, completed_monotonic: int, causation_event_id: str, commands: list
    ) -> MacState:
        """ROC는 Type 0 Packet의 실제 PHY 완료시각을 lease 시작점으로 사용한다."""

        override = self._deployment.type0_override
        if override is None:
            return state
        body = MediaAccessControlBody(
            source_id=self._deployment.roc_station_id,
            media_access_priority_level=0,
            random_access_selection_interval=override.selection_interval,
            short_data_message_limit=override.short_data_limit,
            system_status=override.system_status,
        )
        next_state, _ = self._start_type0_lease(
            state, body, completed_monotonic, causation_event_id, commands
        )
        return next_state.evolve(type0_transmitted=True, type0_tx_command_id=None)

    def _arm_resource_timeout(
        self,
        state: MacState,
        outstanding,
        completed_monotonic: int,
        causation_event_id: str,
        commands: list,
    ) -> MacState:
        interval = self._selection_interval(state, self._now_utc_nanoseconds())
        deadline = transfer_module.response_deadline_nanoseconds(
            tx_completed_monotonic_nanoseconds=completed_monotonic, interval=interval
        )
        timer_id = f"resource-{outstanding.transfer_id.value}-{outstanding.attempt_count}"
        if len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            return self._terminate_transfer(
                state.evolve(outstanding_request=None),
                outstanding.transfer_id,
                transfer_module.resource_busy_error(),
                causation_event_id,
                commands,
            )
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(transfer_id=outstanding.transfer_id),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.RESOURCE_RESPONSE_TIMEOUT,
                ),
            )
        )
        return state.with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.RESOURCE_RESPONSE_TIMEOUT,
                deadline_monotonic_nanoseconds=deadline,
            )
        ).evolve(
            outstanding_request=OutstandingResourceRequest(
                transfer_id=outstanding.transfer_id,
                attempt_count=outstanding.attempt_count,
                timer_id=timer_id,
                route=outstanding.route,
                terminal_capabilities=outstanding.terminal_capabilities,
                priority=outstanding.priority,
                physical_channel_name=outstanding.physical_channel_name,
                last_tx_command_id=outstanding.last_tx_command_id,
            )
        )

    def _arm_short_data_ack_timeout(
        self,
        state: MacState,
        context,
        completed_monotonic: int,
        causation_event_id: str,
        commands: list,
    ) -> MacState:
        interval = self._selection_interval(state, self._now_utc_nanoseconds())
        deadline = transfer_module.response_deadline_nanoseconds(
            tx_completed_monotonic_nanoseconds=completed_monotonic, interval=interval
        )
        timer_id = f"sd-ack-{context.transfer_id.value}-{context.retransmission_number}"
        if len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            return self._terminate_transfer(
                state,
                context.transfer_id,
                transfer_module.resource_busy_error(),
                causation_event_id,
                commands,
            )
        commands.append(
            self._new_command(
                causation_event_id,
                Correlation(transfer_id=context.transfer_id),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.SHORT_DATA_ACK_TIMEOUT,
                ),
            )
        )
        refreshed = state.transfer(context.transfer_id) or context
        return state.with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.SHORT_DATA_ACK_TIMEOUT,
                deadline_monotonic_nanoseconds=deadline,
            )
        ).with_transfer(self._advanced_context(refreshed, ack_timer_id=timer_id))

    def _on_phy_tx_failed(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending TX command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        next_state = self._rollback_resource_batch(next_state, record.command_id)
        next_state = self._release_broadcast_command(next_state, record.command_id)
        next_state = self._release_replay_command(next_state, record.command_id)
        if next_state.type0_tx_command_id == record.command_id:
            next_state = next_state.evolve(type0_tx_command_id=None)
        packet_id_value = envelope.payload.packet_id.value
        for context in next_state.transfers:
            if packet_id_value in context.pending_packet_ids:
                # PHY·Radio dependency 실패는 protocol retry로 숨기지 않는다.
                return self._terminate_transfer(
                    next_state,
                    context.transfer_id,
                    envelope.payload.error,
                    envelope.event_id,
                    commands,
                )
        return next_state

    def _on_phy_rx_accepted(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending RX command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        if record.request_key and record.request_key[0] == "RX":
            if record.request_key[1] != envelope.payload.channel.name:
                return CoordinatorPlan(
                    result=_unmatched("RX event의 channel이 pending request와 다릅니다"),
                    next_state=state,
                    batch=None,
                )
        return state.with_pending_command(
            PendingCommandRecord(
                command_id=record.command_id,
                command_class=record.command_class,
                payload_kind=record.payload_kind,
                accepted=True,
                request_key=record.request_key,
            )
        )

    def _on_phy_rx_closed(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending RX command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        errors: list = []
        next_state = self._maybe_close_epoch(
            next_state, record, envelope.event_id, commands, errors
        )
        if errors:
            return self._finish(next_state, commands, envelope.event_id, errors[0])
        return next_state

    def _maybe_close_epoch(
        self, state: MacState, record, causation_event_id: str, commands: list, errors: list
    ) -> MacState:
        """계획된 모든 RAC RX window가 terminal이 된 뒤 epoch를 한 번 닫는다."""

        if not self.is_control:
            return state
        if not record.request_key or record.request_key[0] != "RX":
            return state
        position = position_at_nanoseconds(record.request_key[2])
        key = (position.minute_start_nanoseconds, position.tdma_frame_index)
        epoch = state.epoch(key)
        if epoch is None or epoch.closed:
            return state
        receive_channel = self._receive_channel()
        slot_map = self.active_slot_map(state)
        for pending in state.pending_commands:
            if pending.command_class is not PendingCommandClass.RX:
                continue
            if not pending.request_key or pending.request_key[0] != "RX":
                continue
            if pending.request_key[1] != receive_channel.name:
                continue
            other = position_at_nanoseconds(pending.request_key[2])
            if (other.minute_start_nanoseconds, other.tdma_frame_index) != key:
                continue
            slot = slot_map.slot_at(receive_channel, other.tdma_channel, other.slot_in_frame)
            if slot is not None and slot.function is SlotFunction.RAC:
                return state
        return self._close_epoch(state, epoch, causation_event_id, commands, errors)

    def _close_epoch(
        self,
        state: MacState,
        epoch,
        causation_event_id: str,
        commands: list,
        errors: list | None = None,
    ) -> MacState:
        """동일 retry 결합·충돌 분류 뒤 남은 후보를 결정적으로 정렬·할당한다."""

        if errors is None:
            errors = []
        next_state = state.with_epoch(
            ResourceRequestEpoch(
                minute_start_nanoseconds=epoch.minute_start_nanoseconds,
                tdma_frame_index=epoch.tdma_frame_index,
                candidates=epoch.candidates,
                open_rx_command_ids=epoch.open_rx_command_ids,
                closed=True,
            )
        )
        response_base_state = next_state
        if not epoch.candidates:
            return next_state
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return next_state
        data_link_id = self._link_id_for(self._profile.data_link_id)
        if data_link_id.is_err:
            return next_state
        now = self._now_utc_nanoseconds()
        lead = self._profile.transmit_lead_nanoseconds
        slot_map = self.active_slot_map(next_state)
        asc = enumerate_function_slots(
            slot_map=slot_map,
            function=SlotFunction.ASC,
            physical_channel=self._profile.upper_channel,
            from_utc_nanoseconds=now + lead,
            until_utc_nanoseconds=now + lead + FRAME_NANOSECONDS * 2,
            transmit=True,
        )
        if not asc:
            return next_state
        slot = asc[0]
        transmit_frame = slot.frame_ordinal
        ordered = sorted(epoch.candidates, key=lambda item: item.order_key)
        messages = []
        resource_chain_station_ids = []
        for candidate in ordered:
            destination = Addressed(StationId(candidate.node_source_id_value))
            rejection = MessageEncodeRequest(
                body=resource_module.build_rejection_body(
                    source_id=self._deployment.roc_station_id, destination=destination
                ),
                correlation=Correlation(),
            )
            if candidate.conflicting:
                messages.append(rejection)
                continue
            chain = next_state.request_chain(candidate.node_source_id_value)
            if chain is not None:
                if chain.data_observed:
                    # data 관측 뒤의 중복 Type 90은 stale evidence로만 기록한다.
                    continue
                reissue = resource_module.reissue_frame_delay(
                    activation_frame=chain.activation_frame,
                    transmit_frame=transmit_frame,
                    resync_used=chain.activation_resync_used,
                )
                if reissue is None:
                    messages.append(rejection)
                    next_state = next_state.without_request_chain(
                        candidate.node_source_id_value
                    )
                    continue
                delay, activation, resync = reissue
                messages.append(
                    MessageEncodeRequest(
                        body=resource_module.build_allocation_body(
                            source_id=self._deployment.roc_station_id,
                            destination=destination,
                            logical_channel_tx=chain.logical_channel_tx,
                            logical_channel_rx=chain.logical_channel_rx,
                            link_id=data_link_id.value,
                            tdma_frame_delay=delay,
                        ),
                        correlation=Correlation(),
                    )
                )
                resource_chain_station_ids.append(candidate.node_source_id_value)
                next_state = next_state.with_request_chain(
                    RequestChain(
                        node_source_id_value=chain.node_source_id_value,
                        route=chain.route,
                        terminal_capabilities=chain.terminal_capabilities,
                        priority=chain.priority,
                        logical_channel_tx=chain.logical_channel_tx,
                        logical_channel_rx=chain.logical_channel_rx,
                        activation_frame=activation,
                        activation_resync_used=resync,
                        data_observed=chain.data_observed,
                    )
                )
                continue
            if len(next_state.request_chains) >= Stage4Limits.ACTIVE_DATA_SESSION:
                messages.append(rejection)
                continue
            pair = resource_module.select_free_duplex_pair(
                slot_map=slot_map,
                direction=resource_module.Direction.SHIP_TO_ROC,
                allocations=next_state.lc_allocations,
                frame_ordinal=transmit_frame,
                lower=self._profile.lower_channel,
                upper=self._profile.upper_channel,
            )
            if pair is None:
                messages.append(rejection)
                continue
            body = resource_module.build_allocation_body(
                source_id=self._deployment.roc_station_id,
                destination=destination,
                logical_channel_tx=pair.logical_channel_tx,
                logical_channel_rx=pair.logical_channel_rx,
                link_id=data_link_id.value,
            )
            messages.append(MessageEncodeRequest(body=body, correlation=Correlation()))
            resource_chain_station_ids.append(candidate.node_source_id_value)
            next_state = next_state.with_request_chain(
                RequestChain(
                    node_source_id_value=candidate.node_source_id_value,
                    route=candidate.route,
                    terminal_capabilities=candidate.terminal_capabilities,
                    priority=candidate.priority,
                    logical_channel_tx=pair.logical_channel_tx,
                    logical_channel_rx=pair.logical_channel_rx,
                    activation_frame=transmit_frame + resource_module.CANONICAL_TDMA_FRAME_DELAY,
                )
            ).evolve(
                lc_allocations=resource_module.upsert_allocation(
                    next_state.lc_allocations,
                    resource_module.allocation_from_type4(
                        body=body,
                        owner_station_id_value=candidate.node_source_id_value,
                        peer_station_id_value=self.local_station_id.value,
                        transmit_frame_ordinal=transmit_frame,
                        tdma_channel=pair.tdma_channel,
                        issued_by_local=True,
                    ),
                )
            )
        if not messages:
            return next_state
        intent = ScheduledTxIntent(
            intent_id=self._id_source.new_event_id(),
            kind="ENCODE",
            transmit_at_nanoseconds=slot.start_nanoseconds,
            channel_name=self._profile.upper_channel.name,
            encode_request=LinkPacketEncodeRequest(
                link_id=link_id.value, messages=tuple(messages), correlation=Correlation()
            ),
            resource_chain_station_ids=tuple(resource_chain_station_ids),
        )
        error_count = len(errors)
        scheduled = self._defer_or_emit(
            next_state,
            intent=intent,
            causation_event_id=causation_event_id,
            commands=commands,
            errors=errors,
        )
        if len(errors) != error_count:
            return response_base_state
        return scheduled

    # --- 나머지 event ---

    def _on_command_completed(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        pending_key = ("SESSION_DELIVERY", record.command_id)
        marker = next(
            (item for item in state.delivery_pending if item[0] == pending_key), None
        )
        if marker is None:
            return next_state
        _, ledger_key, replay = marker
        next_state = next_state.evolve(
            delivery_pending=tuple(
                item for item in next_state.delivery_pending if item[0] != pending_key
            )
        )
        ledger_record = next_state.ledger_record(ledger_key)
        if ledger_record is None:
            return next_state
        completion_frame = frame_ordinal_at_nanoseconds(
            envelope.occurred_at_utc.total_nanoseconds()
        )
        return next_state.with_ledger_record(
            ack_module.promoted_live_record(
                ledger_record,
                completion_frame_ordinal=completion_frame,
                ack_state=(
                    AckPendingState.ACK_PENDING
                    if replay is not None
                    else AckPendingState.ACK_NONE
                ),
                replay_context=replay,
            )
        )

    def _on_dependency_failed(self, state: MacState, envelope, commands: list):
        record = self._match_pending(state, envelope)
        if record is None:
            return CoordinatorPlan(
                result=_unmatched("pending command와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        next_state = state.without_pending_command(record.command_id).with_recent_terminal(
            record.command_id
        )
        next_state = self._rollback_resource_batch(next_state, record.command_id)
        next_state = self._release_broadcast_command(next_state, record.command_id)
        next_state = self._release_replay_command(next_state, record.command_id)
        if next_state.type0_tx_command_id == record.command_id:
            next_state = next_state.evolve(type0_tx_command_id=None)
        next_state = next_state.evolve(
            delivery_pending=tuple(
                item
                for item in next_state.delivery_pending
                if item[0]
                not in (("CTRL_TIME", record.command_id), ("CTRL_CHANNEL", record.command_id))
            )
        )
        pending_key = ("SESSION_DELIVERY", record.command_id)
        marker = next(
            (item for item in state.delivery_pending if item[0] == pending_key), None
        )
        if marker is not None:
            next_state = next_state.evolve(
                delivery_pending=tuple(
                    item for item in next_state.delivery_pending if item[0] != pending_key
                )
            ).without_ledger_record(marker[1])
        transfer_id = envelope.correlation.transfer_id
        if transfer_id is not None:
            # 원 dependency code를 보존하고 fallback을 중복 발행하지 않는다.
            return self._terminate_transfer(
                next_state, transfer_id, envelope.payload.error, envelope.event_id, commands
            )
        # 재조립 실패는 Link가 session context를 이미 폐기한다
        # (`link/data_session/reassembly.py` `_discard_error` → `_contexts.pop`).
        # 그런데 이 경로에는 transfer_id가 없어 위 분기를 타지 않고, RX session은
        # 논리채널로 조회되므로(`rx_session_for_channel`) MAC이 죽은 handle을
        # 계속 재사용해 이후 모든 fragment가 LINK_UNKNOWN_SESSION이 된다.
        # 단발 실행에서는 프로세스가 곧 죽어 보이지 않았지만, 상주 세션에서는
        # workload 1의 오류 1건이 workload 2~4의 수신을 조용히 무효화한다.
        # session을 여기서 버리면 다음 DataStart가 정상적으로 재OPEN한다.
        session_handle = getattr(envelope.correlation, "session_handle", None)
        error = getattr(envelope.payload, "error", None)
        if session_handle is not None and error is not None:
            if getattr(error, "code", None) in _REASSEMBLY_DISCARD_CODES:
                next_state = next_state.without_rx_session(session_handle.value)
        return next_state

    def _on_presentation_delivery_result(self, state: MacState, envelope, commands: list):
        """Service `DATA_RECEIVED` sink 수락 뒤에만 LIVE로 올리고 ACK를 계획한다."""

        payload = envelope.payload
        pending_key = (payload.packet_id.value, payload.delivery_index)
        entry = None
        for item in state.delivery_pending:
            if item[0] == pending_key:
                entry = item
                break
        if entry is None:
            return CoordinatorPlan(
                result=_unmatched("pending delivery key와 상관되지 않습니다"),
                next_state=state,
                batch=None,
            )
        _, ledger_key, _channel_name, sender_value, _broadcast = entry
        record = state.ledger_record(ledger_key)
        next_state = state.evolve(
            delivery_pending=tuple(
                item for item in state.delivery_pending if item[0] != pending_key
            )
        )
        if record is None:
            return next_state
        if payload.error is not None:
            # 실패 result는 pending record를 제거하고 ACK하지 않는다.
            return next_state.without_ledger_record(ledger_key)
        completion_frame = frame_ordinal_at_nanoseconds(
            envelope.occurred_at_utc.total_nanoseconds()
        )
        if record.procedure is RxDeliveryProcedure.SHORT_DATA_92:
            replay = self._build_short_data_replay(
                record=record, sender_value=sender_value, completion_frame=completion_frame
            )
            promoted = ack_module.promoted_live_record(
                record,
                completion_frame_ordinal=completion_frame,
                ack_state=AckPendingState.ACK_PENDING,
                replay_context=replay,
            )
        else:
            promoted = ack_module.promoted_live_record(
                record,
                completion_frame_ordinal=completion_frame,
                ack_state=AckPendingState.ACK_NONE,
                replay_context=None,
            )
        return next_state.with_ledger_record(promoted)

    def _build_short_data_replay(self, *, record, sender_value: int, completion_frame: int):
        """Short Data ACK replay context. 자원 경계는 ledger expiry와 정확히 같다."""

        placement = routing_module.receive_short_data_placement(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            sender_station_id=StationId(sender_value),
            broadcast=False,
            lower=self._profile.lower_channel,
            upper=self._profile.upper_channel,
        )
        if placement.is_err:
            return None
        placement = placement.value
        if placement.ack_slot_function is None:
            return None
        link_id = self._link_id_for(11)
        if link_id.is_err:
            return None
        expires = ack_module.expiry_frame_for(
            record.procedure, completion_frame, record.highest_retransmission_number
        )
        return ack_module.build_short_data_replay_context(
            route=resource_module.one_hop_route(
                self.local_station_id, Addressed(StationId(sender_value))
            ),
            link_id=link_id.value,
            physical_channel=placement.ack_physical_channel,
            slot_function=placement.ack_slot_function,
            ack_body=ack_module.build_ack_body(
                source_id=self.local_station_id,
                destination=Addressed(StationId(sender_value)),
            ),
            resource_last_frame_ordinal=expires,
        )

    def _plan_pending_acks(
        self,
        state: MacState,
        causation_event_id: str,
        commands: list,
        *,
        errors: list | None = None,
    ) -> MacState:
        """`ACK_PENDING` record의 가장 이른 ACK slot command를 예약한다."""

        pending = tuple(
            record
            for record in state.delivery_ledger
            if record.ack_state is AckPendingState.ACK_PENDING
            and record.replay_context is not None
        )
        if not pending:
            return state
        active = state.active_tbb
        if active is None:
            failed = replace(
                pending[0],
                ack_state=AckPendingState.ACK_SENT,
                replay_command_id=None,
            )
            if errors is not None:
                errors.append(ack_module.replay_resource_expired_error())
            return state.with_ledger_record(failed)
        now = self._now_utc_nanoseconds()
        current_frame = frame_ordinal_at_nanoseconds(now)
        tbb_last_frame = (
            active.valid_until_nanoseconds - 1 - EPOCH_2000_UTC_NANOSECONDS
        ) // FRAME_NANOSECONDS
        link_id = self._link_id_for(11)
        if link_id.is_err:
            failed = replace(
                pending[0],
                ack_state=AckPendingState.ACK_SENT,
                replay_command_id=None,
            )
            if errors is not None:
                errors.append(ack_module.replay_resource_expired_error())
            return state.with_ledger_record(failed)
        lead = self._profile.transmit_lead_nanoseconds
        next_state = state
        for record in state.delivery_ledger:
            if record.ack_state is not AckPendingState.ACK_PENDING:
                continue
            context = record.replay_context
            if context is None:
                continue
            if not ack_module.may_schedule_replay(record, current_frame):
                continue
            horizon = ack_module.effective_replay_horizon(context, tbb_last_frame)
            if record.expires_after_frame is not None:
                horizon = min(horizon, record.expires_after_frame)
            if current_frame > horizon:
                next_state = next_state.with_ledger_record(
                    replace(
                        record,
                        ack_state=AckPendingState.ACK_SENT,
                        replay_command_id=None,
                    )
                )
                if errors is not None:
                    errors.append(ack_module.replay_resource_expired_error())
                break
            function = _ACK_SLOT_FUNCTIONS[context.slot_function]
            candidates = tuple(
                item
                for item in enumerate_function_slots(
                    slot_map=self.active_slot_map(next_state),
                    function=function,
                    physical_channel=context.physical_channel,
                    from_utc_nanoseconds=now + lead,
                    until_utc_nanoseconds=frame_ordinal_start_nanoseconds(horizon + 1),
                    transmit=True,
                )
                if context.logical_channel is None
                or item.logical_channel == context.logical_channel
            )
            if not candidates:
                next_state = next_state.with_ledger_record(
                    replace(
                        record,
                        ack_state=AckPendingState.ACK_SENT,
                        replay_command_id=None,
                    )
                )
                if errors is not None:
                    errors.append(ack_module.replay_resource_expired_error())
                break
            slot = candidates[0]
            record_link_id = self._link_id_for(context.link_id.value)
            if record_link_id.is_err:
                next_state = next_state.with_ledger_record(
                    replace(
                        record,
                        ack_state=AckPendingState.ACK_SENT,
                        replay_command_id=None,
                    )
                )
                if errors is not None:
                    errors.append(ack_module.replay_resource_expired_error())
                break
            intent = ScheduledTxIntent(
                intent_id=self._id_source.new_event_id(),
                kind="ENCODE",
                transmit_at_nanoseconds=slot.start_nanoseconds,
                channel_name=context.physical_channel.name,
                encode_request=LinkPacketEncodeRequest(
                    link_id=record_link_id.value,
                    messages=(
                        MessageEncodeRequest(body=context.ack_body, correlation=Correlation()),
                    ),
                    correlation=Correlation(),
                ),
                ledger_key=record.key,
            )
            prepared = next_state.with_ledger_record(
                DeliveryLedgerRecord(
                    key=record.key,
                    procedure=record.procedure,
                    state=record.state,
                    payload_length=record.payload_length,
                    payload_digest=record.payload_digest,
                    first_received_frame_ordinal=record.first_received_frame_ordinal,
                    completion_frame_ordinal=record.completion_frame_ordinal,
                    expires_after_frame=record.expires_after_frame,
                    highest_retransmission_number=record.highest_retransmission_number,
                    ack_state=record.ack_state,
                    replay_context=record.replay_context,
                    replay_command_id=intent.intent_id,
                    last_replay_scheduled_frame=current_frame,
                    pending_delivery_key=record.pending_delivery_key,
                )
            )
            next_state = self._defer_or_emit(
                prepared,
                intent=intent,
                causation_event_id=causation_event_id,
                commands=commands,
                errors=errors if errors is not None else [],
            )
        return next_state

    def _drive_local_work(
        self, state: MacState, causation_event_id: str, commands: list
    ) -> MacState:
        """FIFO head의 admission 조건이 열렸을 때 로컬 작업을 정확히 하나 시작한다."""

        if not state.local_fifo or state.outstanding_request is not None:
            return state
        transfer_id_value = state.local_fifo[0]
        context = state.transfer_by_value(transfer_id_value)
        if context is None or context.phase is TransferPhase.TERMINAL:
            return state.evolve(local_fifo=state.local_fifo[1:])
        if context.phase is not TransferPhase.QUEUED:
            return state.evolve(local_fifo=state.local_fifo[1:])

        if context.role is TransferRole.LOCAL_SHORT_DATA:
            if context.ack_required and any(
                item.role is TransferRole.LOCAL_DATA_SESSION
                and item.phase is not TransferPhase.TERMINAL
                for item in state.transfers
            ):
                return state
            placement = routing_module.resolve_short_data_direction(
                profile=self._deployment,
                local_station_id=self.local_station_id,
                route=context.route,
                ack_required=context.ack_required,
            )
            if placement.is_err:
                return self._terminate_transfer(
                    state,
                    context.transfer_id,
                    placement.error,
                    causation_event_id,
                    commands,
                )
            requirement = TransferRequirement(
                transfer_id=context.transfer_id,
                route=context.route,
                ack_required=context.ack_required,
                presentation_size=context.presentation_size,
                candidate=TransferKind.SHORT_DATA,
            )
            planned = self._plan_short_data_transmission(
                state.evolve(local_fifo=state.local_fifo[1:]),
                requirement=requirement,
                placement=placement.value,
                link_id=context.link_id,
                retransmission_number=0 if context.ack_required else None,
                causation_event_id=causation_event_id,
                commands=commands,
            )
            if planned.is_err:
                return self._terminate_transfer(
                    state,
                    context.transfer_id,
                    planned.error,
                    causation_event_id,
                    commands,
                )
            return planned.value

        if any(
            item.role is TransferRole.LOCAL_DATA_SESSION
            and item.phase is not TransferPhase.TERMINAL
            and item.transfer_id.value != context.transfer_id.value
            for item in state.transfers
        ):
            return state
        session_plan = routing_module.resolve_data_session_plan(
            profile=self._deployment,
            local_station_id=self.local_station_id,
            route=context.route,
        )
        if session_plan.is_err:
            return self._terminate_transfer(
                state,
                context.transfer_id,
                session_plan.error,
                causation_event_id,
                commands,
            )
        prepared = state.evolve(local_fifo=state.local_fifo[1:]).with_transfer(
            self._advanced_context(context, phase=TransferPhase.AWAITING_RESOURCE)
        )
        started = self._start_resource_request(
            prepared,
            transfer_id=context.transfer_id,
            session_plan=session_plan.value,
            route=context.route,
            causation_event_id=causation_event_id,
            commands=commands,
        )
        if started.is_err:
            return self._terminate_transfer(
                state,
                context.transfer_id,
                started.error,
                causation_event_id,
                commands,
            )
        return started.value

    def _on_cancel_transfer(self, state: MacState, envelope, commands: list):
        transfer_id = envelope.payload.transfer_id
        context = state.transfer(transfer_id)
        if context is None or context.phase is TransferPhase.TERMINAL:
            return state
        commands.append(
            self._new_command(
                envelope.event_id,
                Correlation(transfer_id=transfer_id),
                InvokeLinkCommand(
                    request=LinkTransferControlRequest(
                        kind=TransferControlKind.CANCEL,
                        transfer_id=transfer_id,
                        session_handle=None,
                        packet_progress=(),
                        correlation=Correlation(transfer_id=transfer_id),
                    )
                ),
            )
        )
        commands.append(
            self._new_command(
                envelope.event_id,
                Correlation(transfer_id=transfer_id),
                PublishTransferStateCommand(
                    transfer_id=transfer_id, state=TransferState.CANCELLED
                ),
            )
        )
        return state.with_transfer(self._terminal_context(context, TransferState.CANCELLED))

    def _on_endpoint_stopping(self, state: MacState, envelope, commands: list):
        """`EndpointStoppingEvent` 뒤 endpoint는 `DRAINING`이 된다."""

        if state.lifecycle in (EndpointLifecycle.DRAINING, EndpointLifecycle.STOPPED):
            return state
        canonical_deadline = self._now_monotonic_nanoseconds() + STOP_DRAIN_NANOSECONDS
        replaced_timer = None
        if len(state.timers) >= Stage4Limits.ACTIVE_TIMERS:
            # 가장 늦은 기존 timer를 lifecycle deadline으로 원자 교체한다. 그
            # deadline이 더 이르면 drain도 그 시각에 끝내므로 다른 모든 기존
            # timer deadline은 그대로 처리할 기회를 유지한다.
            replaced_timer = max(
                state.timers,
                key=lambda item: (
                    item.deadline_monotonic_nanoseconds,
                    item.timer_id,
                ),
            )
        deadline = canonical_deadline
        if replaced_timer is not None:
            deadline = min(deadline, replaced_timer.deadline_monotonic_nanoseconds)
        timer_id = "stop-drain"
        commands.append(
            self._new_command(
                envelope.event_id,
                Correlation(),
                ScheduleWakeupCommand(
                    timer_id=timer_id,
                    deadline_monotonic=self._monotonic(deadline),
                    purpose=TimerPurpose.STOP_DRAIN,
                    replaces_timer_id=(
                        replaced_timer.timer_id if replaced_timer is not None else None
                    ),
                ),
            )
        )
        next_state = state
        if replaced_timer is not None:
            next_state = next_state.without_timer(replaced_timer.timer_id)
        return next_state.evolve(
            lifecycle=EndpointLifecycle.DRAINING, stop_drain_timer_id=timer_id
        ).with_timer(
            TimerRecord(
                timer_id=timer_id,
                purpose=TimerPurpose.STOP_DRAIN,
                deadline_monotonic_nanoseconds=deadline,
            )
        )


MacCoordinator._EVENT_HANDLERS = {
    ClockAdvancedEvent: MacCoordinator._on_clock_advanced,
    WakeupFiredEvent: MacCoordinator._on_wakeup_fired,
    LinkResultEvent: MacCoordinator._on_link_result,
    PhyTxAcceptedEvent: MacCoordinator._on_phy_tx_accepted,
    PhyTxCompletedEvent: MacCoordinator._on_phy_tx_completed,
    PhyTxFailedEvent: MacCoordinator._on_phy_tx_failed,
    PhyRxWindowAcceptedEvent: MacCoordinator._on_phy_rx_accepted,
    PhyRxWindowClosedEvent: MacCoordinator._on_phy_rx_closed,
    CommandCompletedEvent: MacCoordinator._on_command_completed,
    DependencyFailedEvent: MacCoordinator._on_dependency_failed,
    PresentationDeliveryResultEvent: MacCoordinator._on_presentation_delivery_result,
    CancelTransferEvent: MacCoordinator._on_cancel_transfer,
    EndpointStoppingEvent: MacCoordinator._on_endpoint_stopping,
}


__all__ = ("CoordinatorPlan", "MacCoordinator", "TBB_ASSEMBLY_TIMEOUT_FRAMES")
