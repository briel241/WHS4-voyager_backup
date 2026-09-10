"""SharedMediumActionProvider — Stage 3 `ActionSubmitProvider` seam의 G1-호환 production 구현
(명세 §5.4, §9.4, §10, POC-D12/D14/D23, WSM-REQ-13; 연결 API §9.4, §14).

논리 target action을 canonical Physical Channel과 단일 parent transmission으로 유도하고, C4+C6
atomic fan-out으로 방사한다. client는 논리 target과 상대 schedule만 싣고, frequency·lane·gain·
waveform·hardware timestamp·slot·**trigger 기준시각**·동시 lower+upper TX를 직접 지정할 수 없다.

scheduling: PoC는 NEXT_SAFE_SLOT / after_slots / after_ms만 표현하고, trigger 기준 slot/timestamp는
**VDETER가 관측 event에서 제공**한다(client의 trigger_slot 등은 거부). 실제 hardware 시각은 기존
MAC scheduler(:class:`SlotScheduler`)가 계산하며, 과거·닫힌·lead 부족 slot은 이동하지 않고
`POC_DEADLINE_MISSED`로 거부한다.

오류는 Stage 3 오류 계약(`PocError`)으로만 표출한다: group commit 불명확은
`POC_TX_OUTCOME_UNKNOWN`(SESSION_INHIBIT)으로 변환하고 request→parent correlation과 cleanup을
보존한다. RF 방사 전 거부(readiness·staging·capacity)는 `POC_RADIO_RESOURCE_CONFLICT`로 자원을
반환한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..poc_api.errors import PocError, PocErrorCode
from ..poc_api.types import (
    ActionDescriptor,
    FORBIDDEN_ACTION_FIELDS,
    PhysicalChannel,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
)
from . import constants, tuning
from .radiation import (
    AtomicBroadcast,
    BroadcastCapacityError,
    BroadcastError,
    BroadcastNotAdmitted,
    BroadcastNotReady,
    BroadcastOutcomeUnknown,
    ParentOutcome,
    ScheduledSlot,
)

#: PoC가 schedule에 직접 실을 수 없는 field(hardware 시각·slot·trigger 기준시각은 VDETER/MAC이 결정).
FORBIDDEN_SCHEDULE_FIELDS = frozenset(
    {
        "scheduled_sample",
        "scheduled_hw_timestamp_ns",
        "slot_key",
        "hardware_timestamp",
        "hw_time",
        "transmit_at",
        "device_time",
        "trigger_slot",
        "trigger_hw_timestamp_ns",
        "trigger_time",
        "trigger_timestamp",
    }
)

#: 허용 상대 schedule 표현(§10.2).
SCHEDULE_KINDS = frozenset({"NEXT_SAFE_SLOT", "after_slots", "after_ms"})

#: MAC slot grid(기존 Stage 7 harness와 동일 규약: 8192 samples/slot).
_SLOT_SAMPLES = 8192


@dataclass(frozen=True, slots=True)
class _CachedSubmission:
    submission: ProviderSubmission


def _default_packet_resolver(action: ActionDescriptor, request_id: str) -> bytes:
    payload = action.payload or {}
    if "packet_hex" in payload:
        try:
            return bytes.fromhex(str(payload["packet_hex"]))
        except ValueError as error:
            raise PocError(
                PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"packet_hex 형식 오류: {error}"
            ) from error
    if "packet_bytes" in payload and isinstance(payload["packet_bytes"], (bytes, bytearray)):
        return bytes(payload["packet_bytes"])
    raise PocError(
        PocErrorCode.POC_ACTION_SCHEMA_INVALID,
        "attack action에 packet 내용(packet_hex/packet_bytes)이 필요합니다",
    )


class NextSafeSlotScheduler:
    """RF-off 기본 MAC scheduler stub. 상대 schedule을 결정적 slot으로 계산한다.

    실장비에서는 기존 Stage 7 MAC scheduler가 이 역할을 한다. 반응형(after_slots/after_ms)의 trigger
    기준 slot은 **VDETER가 register_trigger로 등록한 관측 event**에서만 온다(client가 조작 불가).
    과거·닫힌·lead 부족 slot은 이동하지 않고 `POC_DEADLINE_MISSED`로 거부한다(§10).

    시험은 이 계약을 만족하는 fake scheduler(closed slot·lead·trigger 등록)로 대체한다.
    """

    def __init__(self, *, current_slot: int = 0, lead_slots: int = 1, samples_per_slot: int = _SLOT_SAMPLES,
                 samples_per_ms: int = 307, closed_slots=(), triggers=None) -> None:
        self._current = int(current_slot)
        self._lead = int(lead_slots)
        self._slot_samples = int(samples_per_slot)
        self._samples_per_ms = int(samples_per_ms)
        self._closed = set(closed_slots)
        #: observation_id -> (trigger_slot, trigger_hw_timestamp_ns). VDETER가 등록한다.
        self._triggers = dict(triggers or {})

    def register_trigger(self, observation_id: str, *, slot: int, hw_timestamp_ns: int | None = None) -> None:
        """VDETER가 관측 event를 낼 때 그 trigger 기준 slot/timestamp를 등록한다(client 아님)."""

        self._triggers[observation_id] = (
            int(slot), int(hw_timestamp_ns if hw_timestamp_ns is not None else slot * self._slot_samples)
        )

    def resolve(self, action: ActionDescriptor, *, request_id: str) -> ScheduledSlot:
        schedule = action.schedule or {}
        kind = schedule.get("kind", "NEXT_SAFE_SLOT")
        if kind not in SCHEDULE_KINDS:
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"schedule.kind 미지원: {kind!r}")
        earliest = self._current + self._lead
        if kind == "NEXT_SAFE_SLOT":
            slot = earliest
        else:
            # 반응형: trigger 기준은 VDETER가 등록한 관측에서만 얻는다(client trigger 무시·거부).
            obs = action.source_observation_id
            if obs is None or obs not in self._triggers:
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                    "반응형 schedule에는 VDETER가 제공한 유효한 trigger 관측(source_observation_id)이 필요합니다",
                )
            trigger_slot, _trigger_ns = self._triggers[obs]
            if kind == "after_slots":
                slot = trigger_slot + int(schedule.get("slots", 0))
            else:  # after_ms
                slot = trigger_slot + (int(schedule.get("ms", 0)) * self._samples_per_ms) // self._slot_samples
        if slot < earliest:
            raise PocError(
                PocErrorCode.POC_DEADLINE_MISSED,
                f"요청 slot {slot}이 earliest {earliest} 이전(과거/lead 부족) — 이동하지 않음",
            )
        if slot in self._closed:
            raise PocError(PocErrorCode.POC_DEADLINE_MISSED, f"slot {slot}이 닫힘 — 이동하지 않음")
        return ScheduledSlot(
            scheduled_sample=slot * self._slot_samples,
            scheduled_hw_timestamp_ns=slot * self._slot_samples,
            slot_key=f"slot-{slot}",
        )


class SharedMediumActionProvider:
    """`ActionSubmitProvider` 계약(derive_physical_channel + submit)의 production 구현."""

    def __init__(
        self,
        *,
        radio_sink,
        rx_readiness,
        scheduler=None,
        waveform_factory=None,
        arbiter=None,
        dual_channel_installed: bool = False,
        packet_resolver=None,
    ) -> None:
        self._broadcast = AtomicBroadcast(
            radio_sink=radio_sink,
            rx_readiness=rx_readiness,
            waveform_factory=waveform_factory,
            arbiter=arbiter,
        )
        self._arbiter = arbiter
        self._scheduler = scheduler or NextSafeSlotScheduler()
        self._dual_channel_installed = bool(dual_channel_installed)
        self._packet_resolver = packet_resolver or _default_packet_resolver
        self._cache: dict = {}          # request_id -> _CachedSubmission (exactly-once, 성공만)
        self._parents: dict = {}        # request_id -> parent (correlation, 시도 시점부터)
        self._reservations: dict = {}   # request_id -> resource_key (자원 반환용)
        self._outcomes: dict = {}       # parent_id -> ParentOutcome (evidence 반영)

    @classmethod
    def for_production(cls, *, radio_sink, rx_readiness, scheduler, waveform_factory,
                       arbiter=None, dual_channel_installed: bool = False, packet_resolver=None):
        """production 조립용 fail-closed 생성자.

        Stage 6에서 실물 MAC scheduler와 frozen PHY waveform factory 주입을 빠뜨리면 RF-off stub이
        조용히 동작하지 않도록, scheduler와 waveform_factory를 **필수 인자**로 강제한다.
        """

        if scheduler is None:
            raise ValueError("production provider에는 실물 MAC scheduler가 필수입니다(fail-closed)")
        if waveform_factory is None:
            raise ValueError("production provider에는 frozen PHY waveform factory가 필수입니다(fail-closed)")
        return cls(
            radio_sink=radio_sink, rx_readiness=rx_readiness, scheduler=scheduler,
            waveform_factory=waveform_factory, arbiter=arbiter,
            dual_channel_installed=dual_channel_installed, packet_resolver=packet_resolver,
        )

    # --- ActionSubmitProvider 계약 -----------------------------------------
    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        if target not in TARGET_TO_PHYSICAL_CHANNEL:
            raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action: ActionDescriptor, *, request_id: str) -> ProviderSubmission:
        """논리 target action을 단일 parent·C4+C6 fan-out으로 방사한다(exactly-once)."""

        if request_id in self._cache:
            return self._cache[request_id].submission

        target = action.target
        if target not in constants.ATTACK_ALLOWED_TARGETS:
            raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")

        self._reject_forbidden_fields(action)

        channel = self.derive_physical_channel(target)
        packet = self._packet_resolver(action, request_id)

        # PoC는 hardware 시각·trigger 기준을 만들지 않는다: MAC scheduler가 slot을 계산.
        slot = self._scheduler.resolve(action, request_id=request_id)
        if not isinstance(slot, ScheduledSlot):
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, "scheduler가 ScheduledSlot을 반환하지 않음")

        resource_key = f"{channel.value}:{slot.slot_key}"
        if self._arbiter is not None:
            if not self._arbiter.reserve(resource_key=resource_key, owner="POC"):
                raise PocError(
                    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, f"자원 {resource_key}를 정상 TX가 점유 중"
                )
            self._reservations[request_id] = resource_key

        parent = self._broadcast.build_parent(
            source_station=constants.ATTACKER_STATION,
            channel=channel,
            payload=packet,
            scheduled_sample=slot.scheduled_sample,
            scheduled_hw_timestamp_ns=slot.scheduled_hw_timestamp_ns,
            slot_key=slot.slot_key,
            addressed_destination=target,
        )
        # correlation을 방사 시도 시점부터 보존한다(OUTCOME_UNKNOWN 시 evidence·cleanup 위해).
        self._parents[request_id] = parent

        try:
            self._broadcast.transmit(parent)
        except BroadcastOutcomeUnknown as error:
            # commit 불명확 -> SESSION_INHIBIT. 자원 해제하지 않고(불확실) correlation 보존.
            self._outcomes[parent.parent_id] = ParentOutcome.OUTCOME_UNKNOWN
            raise PocError(
                PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                f"group commit 불명확 (parent {parent.parent_id})",
                detail={"broadcast_parent_id": parent.parent_id, "transfer_id": parent.transfer_id},
            ) from error
        except (BroadcastNotReady, BroadcastNotAdmitted, BroadcastCapacityError) as error:
            # RF 방사 전 거부(안전): 자원 반환하고 PoC action만 거부(정상 TX 유지).
            self._release_reservation(request_id)
            self._parents.pop(request_id, None)
            raise PocError(
                PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, f"RF 방사 전 거부: {error}"
            ) from error
        except BroadcastError as error:
            self._release_reservation(request_id)
            self._parents.pop(request_id, None)
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, f"broadcast 구조 오류: {error}") from error
        except PocError:
            self._release_reservation(request_id)
            self._parents.pop(request_id, None)
            raise

        submission = ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=parent.parent_id,
            transfer_id=parent.transfer_id,
        )
        self._cache[request_id] = _CachedSubmission(submission=submission)
        return submission

    # --- terminal 취합 + 자원 회수 -----------------------------------------
    def resolve_outcome(self, request_id: str, terminals: dict):
        """parent outcome을 확정하고 PoC 자원 예약을 release한다(§6.1, §10). evidence에 반영."""

        parent = self._parents.get(request_id)
        if parent is None:
            raise PocError(PocErrorCode.POC_NOT_RUNNING, f"알 수 없는 request_id: {request_id}")
        outcome = self._broadcast.resolve_outcome(parent.parent_id, terminals)
        self._outcomes[parent.parent_id] = outcome
        self._release_reservation(request_id)
        return outcome

    def outcomes_by_parent(self) -> dict:
        """parent_id -> ParentOutcome. evidence 조립에 넘겨 최신 결과를 반영한다."""

        return dict(self._outcomes)

    def _release_reservation(self, request_id: str) -> None:
        key = self._reservations.pop(request_id, None)
        if self._arbiter is not None and key is not None:
            self._arbiter.release(resource_key=key, owner="POC")

    # --- capability ---------------------------------------------------------
    def advertised_capabilities(self) -> frozenset:
        return tuning.advertised_capabilities(dual_channel_installed=self._dual_channel_installed)

    def prepare_dual_channel(self, *, required: bool):
        return tuning.resolve_dual_channel(
            required=required, dual_channel_installed=self._dual_channel_installed
        )

    # --- correlation / cleanup ---------------------------------------------
    def parent_for(self, request_id: str):
        return self._parents.get(request_id)

    @property
    def active_group_count(self) -> int:
        return self._broadcast.active_group_count

    def cleanup(self) -> None:
        """모든 PoC 자원 예약을 release하고 fan-out group·cache·correlation 잔존을 0으로 정리한다."""

        if self._arbiter is not None:
            for key in list(self._reservations.values()):
                self._arbiter.release(resource_key=key, owner="POC")
        self._broadcast.cleanup()
        self._cache.clear()
        self._parents.clear()
        self._reservations.clear()
        self._outcomes.clear()

    # --- 내부 ---------------------------------------------------------------
    def _reject_forbidden_fields(self, action: ActionDescriptor) -> None:
        payload = action.payload or {}
        for key in payload:
            if key in FORBIDDEN_ACTION_FIELDS:
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"금지된 RF field 요청: {key!r}"
                )
        schedule = action.schedule or {}
        for key in schedule:
            if key in FORBIDDEN_ACTION_FIELDS or key in FORBIDDEN_SCHEDULE_FIELDS:
                raise PocError(
                    PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                    f"schedule에 직접 지정할 수 없는 field: {key!r} (hardware 시각·trigger 기준은 VDETER가 결정)",
                )


__all__ = (
    "FORBIDDEN_SCHEDULE_FIELDS",
    "SCHEDULE_KINDS",
    "NextSafeSlotScheduler",
    "SharedMediumActionProvider",
)
