"""SharedMediumActionProvider — Stage 3 `ActionSubmitProvider` seam의 G1-호환 production 구현
(명세 §5.4, §9.4, §10, POC-D12/D14/D23, WSM-REQ-13; 연결 API §9.4).

논리 target action을 canonical Physical Channel과 단일 parent transmission으로 유도하고,
C4+C6 atomic fan-out으로 방사한다. client는 논리 target과 상대 schedule만 싣고, frequency·lane·
gain·waveform·hardware timestamp·slot·동시 lower+upper TX를 직접 지정할 수 없다.

scheduling: PoC는 NEXT_SAFE_SLOT / after_slots / after_ms만 표현하고, 실제 hardware 시각은
기존 MAC scheduler(:class:`SlotScheduler`)가 계산한다. 과거·닫힌·lead 부족 slot은 이동하지 않고
`POC_DEADLINE_MISSED`로 거부한다. 동일 request_id 재요청은 provider/RF submit을 반복하지 않는다.

정상 scenario/infra TX가 PoC TX보다 우선한다: 같은 자원이 겹치면 정상 TX를 취소·재예약하지 않고
PoC action만 `POC_RADIO_RESOURCE_CONFLICT`로 거부한다. PoC 자원 예약은 terminal·cleanup에서 release한다.
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
from .radiation import AtomicBroadcast, ScheduledSlot

#: PoC가 schedule에 직접 실을 수 없는 timing field(hardware 시각·slot은 MAC이 계산).
FORBIDDEN_SCHEDULE_FIELDS = frozenset(
    {
        "scheduled_sample",
        "scheduled_hw_timestamp_ns",
        "slot_key",
        "hardware_timestamp",
        "hw_time",
        "transmit_at",
        "device_time",
    }
)

#: 허용 상대 schedule 표현(§10.2).
SCHEDULE_KINDS = frozenset({"NEXT_SAFE_SLOT", "after_slots", "after_ms"})

#: MAC slot grid(기존 Stage 7 harness와 동일 규약: 8192 samples/slot).
_SLOT_SAMPLES = 8192


@dataclass(frozen=True, slots=True)
class _CachedSubmission:
    submission: ProviderSubmission
    resource_key: str | None


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

    실장비에서는 기존 Stage 7 MAC scheduler가 이 역할을 한다. 여기서는 monotonic slot 커서와
    고정 grid로 NEXT_SAFE_SLOT / after_slots / after_ms를 계산하고, 과거·닫힌·lead 부족 slot은
    이동하지 않고 `POC_DEADLINE_MISSED`로 거부한다(§10). PoC가 hardware 시각을 만들지 않는다.

    시험은 이 계약을 만족하는 fake scheduler(closed slot·lead 강제)로 대체한다.
    """

    def __init__(self, *, current_slot: int = 0, lead_slots: int = 1, samples_per_slot: int = _SLOT_SAMPLES,
                 samples_per_ms: int = 307, closed_slots=()) -> None:
        self._current = int(current_slot)
        self._lead = int(lead_slots)
        self._slot_samples = int(samples_per_slot)
        self._samples_per_ms = int(samples_per_ms)
        self._closed = set(closed_slots)

    def resolve(self, action: ActionDescriptor, *, request_id: str) -> ScheduledSlot:
        schedule = action.schedule or {}
        kind = schedule.get("kind", "NEXT_SAFE_SLOT")
        if kind not in SCHEDULE_KINDS:
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, f"schedule.kind 미지원: {kind!r}")
        earliest = self._current + self._lead
        trigger_slot = int(schedule.get("trigger_slot", self._current))
        if kind == "NEXT_SAFE_SLOT":
            slot = earliest
        elif kind == "after_slots":
            slot = trigger_slot + int(schedule.get("slots", 0))
        else:  # after_ms
            offset_slots = (int(schedule.get("ms", 0)) * self._samples_per_ms) // self._slot_samples
            slot = trigger_slot + offset_slots
        if slot < earliest:
            raise PocError(
                PocErrorCode.POC_DEADLINE_MISSED,
                f"요청 slot {slot}이 earliest {earliest} 이전(과거/lead 부족) — 이동하지 않음",
            )
        if slot in self._closed:
            raise PocError(
                PocErrorCode.POC_DEADLINE_MISSED, f"slot {slot}이 닫힘 — 이동하지 않음"
            )
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
        #: request_id -> _CachedSubmission (exactly-once 경계).
        self._cache: dict = {}
        #: request_id -> parent (correlation·cleanup).
        self._parents: dict = {}

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

        # PoC는 hardware 시각을 만들지 않는다: MAC scheduler가 slot을 계산(과거·닫힘·lead 부족은 DEADLINE_MISSED).
        slot = self._scheduler.resolve(action, request_id=request_id)
        if not isinstance(slot, ScheduledSlot):
            raise PocError(PocErrorCode.POC_ACTION_SCHEMA_INVALID, "scheduler가 ScheduledSlot을 반환하지 않음")

        resource_key = f"{channel.value}:{slot.slot_key}"
        reserved = True
        if self._arbiter is not None:
            reserved = bool(self._arbiter.reserve(resource_key=resource_key, owner="POC"))
            if not reserved:
                raise PocError(
                    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT,
                    f"자원 {resource_key}를 정상 TX가 점유 중",
                )

        try:
            parent = self._broadcast.build_parent(
                source_station=constants.ATTACKER_STATION,
                channel=channel,
                payload=packet,
                scheduled_sample=slot.scheduled_sample,
                scheduled_hw_timestamp_ns=slot.scheduled_hw_timestamp_ns,
                slot_key=slot.slot_key,
                addressed_destination=target,
            )
            self._broadcast.transmit(parent)
        except Exception:
            if self._arbiter is not None and reserved:
                self._arbiter.release(resource_key=resource_key, owner="POC")
            raise

        submission = ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=parent.parent_id,
            transfer_id=parent.transfer_id,
        )
        self._cache[request_id] = _CachedSubmission(
            submission=submission, resource_key=resource_key if self._arbiter is not None else None
        )
        self._parents[request_id] = parent
        return submission

    # --- terminal 취합 + 자원 회수 -----------------------------------------
    def resolve_outcome(self, request_id: str, terminals: dict):
        """parent outcome을 확정하고 PoC 자원 예약을 release한다(§6.1, §10)."""

        parent = self._parents.get(request_id)
        if parent is None:
            raise PocError(PocErrorCode.POC_NOT_RUNNING, f"알 수 없는 request_id: {request_id}")
        outcome = self._broadcast.resolve_outcome(parent.parent_id, terminals)
        self._release_reservation(request_id)
        return outcome

    def _release_reservation(self, request_id: str) -> None:
        cached = self._cache.get(request_id)
        if self._arbiter is not None and cached is not None and cached.resource_key is not None:
            self._arbiter.release(resource_key=cached.resource_key, owner="POC")
            self._cache[request_id] = _CachedSubmission(submission=cached.submission, resource_key=None)

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
        """모든 PoC 자원 예약을 release하고 fan-out group·cache 잔존을 0으로 정리한다."""

        if self._arbiter is not None:
            for cached in self._cache.values():
                if cached.resource_key is not None:
                    self._arbiter.release(resource_key=cached.resource_key, owner="POC")
        self._broadcast.cleanup()
        self._cache.clear()
        self._parents.clear()

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
                    f"schedule에 직접 지정할 수 없는 field: {key!r} (hardware 시각은 MAC이 계산)",
                )


__all__ = (
    "FORBIDDEN_SCHEDULE_FIELDS",
    "SCHEDULE_KINDS",
    "NextSafeSlotScheduler",
    "SharedMediumActionProvider",
)
