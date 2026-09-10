"""SharedMediumActionProvider — Stage 3 `ActionSubmitProvider` seam의 G1-호환 production 구현
(명세 §5.4, §9.4, POC-D23, WSM-REQ-13; 연결 API §9.4).

논리 target action을 canonical Physical Channel과 단일 parent transmission으로 유도하고,
C4+C6 atomic fan-out으로 방사한다. client는 논리 target만 싣고 frequency·lane·gain·waveform·
동시 lower+upper TX를 요청할 수 없다. 동일 request_id 재요청은 provider/RF submit을 반복하지
않고 cache로 답한다(exactly-once).

정상 scenario/infra TX가 PoC TX보다 우선한다: 같은 자원이 겹치면 정상 TX를 취소·재예약하지
않고 PoC action만 `POC_RADIO_RESOURCE_CONFLICT`로 거부한다.
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
from .radiation import AtomicBroadcast


@dataclass(frozen=True, slots=True)
class _CachedSubmission:
    submission: ProviderSubmission
    canonical_body_keys: tuple


def _default_packet_resolver(action: ActionDescriptor, request_id: str) -> bytes:
    """action.payload의 논리 packet 내용을 bytes로 해석한다(RF param 아님).

    packet_hex(우선) 또는 packet_bytes(latin-1)에서 읽는다. 둘 다 없으면 schema 오류.
    """

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


def _default_scheduler(action: ActionDescriptor, request_id: str) -> dict:
    """action.schedule에서 방사 timing 좌표를 유도한다(정확한 slot 계산은 상위 scheduler).

    Stage 5 RF-off 모델에서는 논리 schedule을 결정적 좌표로 매핑한다. missed deadline 자동
    이동·완화는 하지 않는다(§10).
    """

    schedule = action.schedule or {}
    return {
        "scheduled_sample": int(schedule.get("scheduled_sample", 0)),
        "scheduled_hw_timestamp_ns": int(schedule.get("scheduled_hw_timestamp_ns", 0)),
        "slot_key": str(schedule.get("slot_key", f"slot-{request_id}")),
    }


class SharedMediumActionProvider:
    """`ActionSubmitProvider` 계약(derive_physical_channel + submit)의 production 구현."""

    def __init__(
        self,
        *,
        radio_sink,
        rx_readiness,
        waveform_factory=None,
        arbiter=None,
        dual_channel_installed: bool = False,
        packet_resolver=None,
        scheduler=None,
    ) -> None:
        self._broadcast = AtomicBroadcast(
            radio_sink=radio_sink,
            rx_readiness=rx_readiness,
            waveform_factory=waveform_factory,
            arbiter=arbiter,
        )
        self._arbiter = arbiter
        self._dual_channel_installed = bool(dual_channel_installed)
        self._packet_resolver = packet_resolver or _default_packet_resolver
        self._scheduler = scheduler or _default_scheduler
        #: request_id -> _CachedSubmission (exactly-once 경계).
        self._cache: dict = {}
        #: request_id -> parent (correlation·cleanup).
        self._parents: dict = {}

    # --- ActionSubmitProvider 계약 -----------------------------------------
    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        """논리 target → canonical Physical Channel(ROC=lower, MASS-A=upper)."""

        if target not in TARGET_TO_PHYSICAL_CHANNEL:
            raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action: ActionDescriptor, *, request_id: str) -> ProviderSubmission:
        """논리 target action을 단일 parent·C4+C6 fan-out으로 방사한다(exactly-once)."""

        # 0) exactly-once: 동일 request_id 재요청은 factory/sink를 다시 호출하지 않는다.
        if request_id in self._cache:
            return self._cache[request_id].submission

        # 1) 논리 target 검증(ROC/MASS-A 하나).
        target = action.target
        if target not in constants.ATTACK_ALLOWED_TARGETS:
            raise PocError(PocErrorCode.POC_TARGET_DENIED, f"허용되지 않은 target: {target!r}")

        # 2) forbidden RF field 재-guard(§9.4). client는 논리 target만 싣는다.
        self._reject_forbidden_fields(action)

        # 3) canonical channel 유도(한 action = 한 채널; 동시 lower+upper 불가).
        channel = self.derive_physical_channel(target)

        # 4) 논리 packet 내용·schedule 좌표 해석.
        packet = self._packet_resolver(action, request_id)
        sched = self._scheduler(action, request_id)

        # 5) 정상 TX 우선: 자원 예약 실패 = 정상 TX 점유 → PoC만 거부(정상 TX 취소 안 함).
        resource_key = f"{channel.value}:{sched['slot_key']}"
        reserved = True
        if self._arbiter is not None:
            reserved = bool(self._arbiter.reserve(resource_key=resource_key, owner="POC"))
            if not reserved:
                raise PocError(
                    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT,
                    f"자원 {resource_key}를 정상 TX가 점유 중",
                )

        try:
            # 6) 단일 parent + C4+C6 fan-out. 공격자 = MASS-B 고정. target = addressed dest.
            parent = self._broadcast.build_parent(
                source_station=constants.ATTACKER_STATION,
                channel=channel,
                payload=packet,
                scheduled_sample=sched["scheduled_sample"],
                scheduled_hw_timestamp_ns=sched["scheduled_hw_timestamp_ns"],
                slot_key=sched["slot_key"],
                addressed_destination=target,
            )
            # 7) atomic prepare/commit. readiness 실패는 fail-closed(BroadcastNotReady 전파).
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
            submission=submission,
            canonical_body_keys=tuple(sorted((action.payload or {}).keys())),
        )
        self._parents[request_id] = parent
        return submission

    # --- capability ---------------------------------------------------------
    def advertised_capabilities(self) -> frozenset:
        return tuning.advertised_capabilities(dual_channel_installed=self._dual_channel_installed)

    def prepare_dual_channel(self, *, required: bool):
        """required dual-channel 관측을 해소하거나 fail-closed(WSM-REQ-12)."""

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
        self._broadcast.cleanup()
        self._cache.clear()
        self._parents.clear()

    # --- 내부 ---------------------------------------------------------------
    def _reject_forbidden_fields(self, action: ActionDescriptor) -> None:
        for container in (action.payload or {}, action.schedule or {}):
            for key in container:
                if key in FORBIDDEN_ACTION_FIELDS:
                    raise PocError(
                        PocErrorCode.POC_ACTION_SCHEMA_INVALID,
                        f"금지된 RF field 요청: {key!r}",
                    )


__all__ = (
    "SharedMediumActionProvider",
)
