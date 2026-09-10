"""Atomic broadcast transmission: parent-to-2-child fan-out, atomic group commit, terminal
취합, cleanup (명세 §6, §5.1, WSM-REQ-01/03/04, S8-D005/D007/D014).

핵심 불변:
- 단일 논리 burst는 정확히 하나의 physical parent를 갖는다.
- parent waveform은 한 번 생성하고 두 child가 bytes·samples·channel·gain·start timestamp·
  payload digest를 공유한다(fan-out으로 waveform·RF submit 중복 0).
- 두 child의 방사는 sink의 **atomic 2단계**(prepare_group -> commit_group)로 처리한다. per-child
  순차 제출 경로가 없으므로 "한 child만 방사"가 구조적으로 불가능하다. prepare 실패 시 아무 것도
  방사하지 않고, commit 결과가 불명확하면 parent를 성공으로 만들지 않고 OUTCOME_UNKNOWN이다.
- required receiver RX window가 준비되지 않으면 RF 전에 fail-closed한다. 다른 channel에 tuned된
  비주소 endpoint는 required가 아니며 NOT_TUNED로 non-blocking이다.
- 두 child의 terminal(COMPLETED/FAILED/UNKNOWN)을 취합해야 parent outcome이 확정된다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from ..poc_api.codec import sha256_hex
from ..poc_api.types import PhysicalChannel
from . import constants, topology
from .iq import IqBurst


class BroadcastError(RuntimeError):
    """shared-medium 방사 계층의 구조화 오류(fail-closed)."""


class BroadcastNotReady(BroadcastError):
    """required receiver RX window 미준비 -> RF 전 fail-closed(§6.2)."""


class BroadcastAtomicityError(BroadcastError):
    """단일 parent·fan-out child 불변 위반 또는 group commit 불명확(§6.1)."""


class SiblingCollisionError(BroadcastError):
    """같은 parent의 fan-out child가 같은 collision group에 들어감(§7.3, WSM-REQ-12)."""


class BroadcastCapacityError(BroadcastError):
    """동시 활성 broadcast group 상한 초과(§10 bounded)."""


class ChildTerminal(str, Enum):
    """child path의 최종 상태."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ParentOutcome(str, Enum):
    """parent transmission의 취합 결과(§6.1, §9.1)."""

    PENDING = "PENDING"                 # commit됨, child terminal 미취합
    COMPLETED = "COMPLETED"             # 두 child 모두 COMPLETED
    FAILED = "FAILED"                   # UNKNOWN 없이 하나 이상 FAILED
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"  # 하나라도 UNKNOWN(성공으로 만들지 않음)
    REJECTED_BEFORE_TX = "REJECTED_BEFORE_TX"  # RF 전 거부(prepare/readiness 실패)


@dataclass(frozen=True, slots=True)
class ScheduledSlot:
    """기존 MAC scheduler가 상대 schedule에서 계산한 실제 slot(§10). PoC가 직접 만들지 않는다."""

    scheduled_sample: int
    scheduled_hw_timestamp_ns: int
    slot_key: str


@dataclass(frozen=True, slots=True)
class ReadinessRequest:
    endpoint: str
    channel: PhysicalChannel
    slot_key: str
    scheduled_sample: int
    scheduled_hw_timestamp_ns: int
    clock_id: int
    bridge_generation: int
    correction_revision: int


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    ready: bool
    reason: str = ""
    proof: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroupAdmission:
    """prepare_group이 돌려주는 admission handle. commit_group이 이걸로 atomic 방사한다."""

    group_id: str
    children: tuple


@dataclass(frozen=True, slots=True)
class ChildPath:
    """fan-out child 하나. parent field를 공유하고 cable/lane/receiver만 다르다."""

    child_id: str
    parent_id: str
    transfer_id: str
    cable: str
    tx_station: str
    tx_lane: int
    rx_station: str
    rx_lane: int
    channel: PhysicalChannel
    payload_digest: str
    waveform_sha256: str
    scheduled_sample: int
    scheduled_hw_timestamp_ns: int
    samples: tuple

    def as_evidence(self) -> dict:
        return {
            "child_id": self.child_id,
            "parent_id": self.parent_id,
            "transfer_id": self.transfer_id,
            "cable": self.cable,
            "tx_station": self.tx_station,
            "tx_lane": self.tx_lane,
            "rx_station": self.rx_station,
            "rx_lane": self.rx_lane,
            "channel": self.channel.value,
            "payload_digest": self.payload_digest,
            "waveform_sha256": self.waveform_sha256,
            "scheduled_sample": self.scheduled_sample,
            "scheduled_hw_timestamp_ns": self.scheduled_hw_timestamp_ns,
            "sample_count": len(self.samples),
        }


@dataclass(frozen=True, slots=True)
class ParentTransmission:
    """단일 논리 burst의 물리 parent. 두 child로 fan-out된다."""

    parent_id: str
    transfer_id: str
    source_station: str
    channel: PhysicalChannel
    addressed_destination: str | None
    official_broadcast: bool
    payload: bytes
    payload_digest: str
    waveform_sha256: str
    waveform: IqBurst
    scheduled_sample: int
    scheduled_hw_timestamp_ns: int
    slot_key: str
    clock_id: int
    bridge_generation: int
    correction_revision: int
    children: tuple

    def as_evidence(self) -> dict:
        return {
            "parent_id": self.parent_id,
            "transfer_id": self.transfer_id,
            "source_station": self.source_station,
            "channel": self.channel.value,
            "addressed_destination": self.addressed_destination,
            "official_broadcast": self.official_broadcast,
            "payload_digest": self.payload_digest,
            "waveform_sha256": self.waveform_sha256,
            "scheduled_sample": self.scheduled_sample,
            "scheduled_hw_timestamp_ns": self.scheduled_hw_timestamp_ns,
            "slot_key": self.slot_key,
            "clock_id": self.clock_id,
            "bridge_generation": self.bridge_generation,
            "correction_revision": self.correction_revision,
            "children": [c.as_evidence() for c in self.children],
        }


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    parent: ParentTransmission
    committed: bool
    submitted_child_ids: tuple
    outcome: ParentOutcome = ParentOutcome.PENDING
    reason: str = ""


class ReferenceWaveformFactory:
    """RF-off 참조 waveform factory. decoder 참조 modem으로 baseband IQ를 만든다.

    실장비에서는 frozen PHY(vdeter.phy.transmitter.Link11Phy)가 이 역할을 대신한다(Stage 6).
    """

    def encode(self, *, source_station: str, channel, payload: bytes) -> IqBurst:
        from .decoder import encode_payload

        return IqBurst(channel=channel, start_sample=0, samples=encode_payload(payload))


class AtomicBroadcast:
    """송신자별 두 child로 fan-out하고 atomic group barrier로 RF commit한다.

    RF critical path(prepare/commit)에서는 JSON render·disk write·flush·blocking을 하지 않는다.
    evidence는 bounded memory dataclass로만 만든다(직렬화는 상위에서 quiescent 후).
    """

    def __init__(
        self,
        *,
        radio_sink,
        rx_readiness,
        waveform_factory=None,
        arbiter=None,
        max_active_groups: int = constants.MAX_ACTIVE_BROADCAST_GROUPS,
    ) -> None:
        self._sink = radio_sink
        self._readiness = rx_readiness
        self._factory = waveform_factory or ReferenceWaveformFactory()
        self._arbiter = arbiter
        self._max_active_groups = int(max_active_groups)
        #: parent_id -> {"parent": ParentTransmission, "outcome": ParentOutcome}
        #: commit됐지만 아직 terminal 미확정인 in-flight group만 담는다(resolve/cleanup에서 제거).
        self._active_groups: dict = {}

    # --- parent/child 생성 -------------------------------------------------
    def build_parent(
        self,
        *,
        source_station: str,
        channel: PhysicalChannel,
        payload: bytes,
        scheduled_sample: int,
        scheduled_hw_timestamp_ns: int,
        slot_key: str,
        addressed_destination=None,
        official_broadcast: bool = False,
        transfer_id=None,
        parent_id=None,
        clock_id: int = 1,
        bridge_generation: int = 1,
        correction_revision: int = 1,
    ) -> ParentTransmission:
        if source_station not in constants.SENDER_FANOUT_CABLES:
            raise BroadcastError(f"알 수 없는 송신자: {source_station!r}")
        parent_id = parent_id or f"bp-{uuid.uuid4().hex[:12]}"
        transfer_id = transfer_id or f"tx-{uuid.uuid4().hex[:12]}"

        # waveform은 정확히 한 번 생성한다(single-burst invariant).
        waveform = self._factory.encode(
            source_station=source_station, channel=channel, payload=payload
        )
        if waveform.channel != channel:
            raise BroadcastAtomicityError("waveform channel이 parent channel과 다릅니다")
        samples = waveform.samples
        waveform_sha256 = waveform.sha256
        payload_digest = sha256_hex(bytes(payload))

        cables = topology.fanout_cables(source_station)
        if len(cables) != constants.FANOUT_CHILDREN:
            raise BroadcastAtomicityError("fan-out child 수가 2가 아닙니다")

        children = []
        for cable in cables:
            tx_station, tx_lane, rx_station, rx_lane = constants.CABLE_PATHS[cable]
            if tx_station != source_station:
                raise BroadcastAtomicityError(f"{cable}의 TX가 송신자와 다릅니다")
            children.append(
                ChildPath(
                    child_id=f"{parent_id}:{cable}",
                    parent_id=parent_id,
                    transfer_id=transfer_id,
                    cable=cable,
                    tx_station=tx_station,
                    tx_lane=tx_lane,
                    rx_station=rx_station,
                    rx_lane=rx_lane,
                    channel=channel,
                    payload_digest=payload_digest,
                    waveform_sha256=waveform_sha256,
                    scheduled_sample=scheduled_sample,
                    scheduled_hw_timestamp_ns=scheduled_hw_timestamp_ns,
                    samples=samples,
                )
            )
        parent = ParentTransmission(
            parent_id=parent_id,
            transfer_id=transfer_id,
            source_station=source_station,
            channel=channel,
            addressed_destination=addressed_destination,
            official_broadcast=official_broadcast,
            payload=bytes(payload),
            payload_digest=payload_digest,
            waveform_sha256=waveform_sha256,
            waveform=IqBurst(channel=channel, start_sample=scheduled_sample, samples=samples),
            scheduled_sample=scheduled_sample,
            scheduled_hw_timestamp_ns=scheduled_hw_timestamp_ns,
            slot_key=slot_key,
            clock_id=clock_id,
            bridge_generation=bridge_generation,
            correction_revision=correction_revision,
            children=tuple(children),
        )
        self._assert_single_burst_invariant(parent)
        return parent

    def _assert_single_burst_invariant(self, parent: ParentTransmission) -> None:
        """단일 parent·fan-out child 불변(§6.1)을 hard-assert한다."""

        if len(parent.children) != constants.FANOUT_CHILDREN:
            raise BroadcastAtomicityError("parent당 child가 정확히 2개가 아닙니다")
        seen_cables = set()
        seen_receivers = set()
        for child in parent.children:
            if child.parent_id != parent.parent_id:
                raise BroadcastAtomicityError("child parent_id가 parent와 다릅니다")
            if child.transfer_id != parent.transfer_id:
                raise BroadcastAtomicityError("child transfer_id가 parent와 다릅니다")
            if child.payload_digest != parent.payload_digest:
                raise BroadcastAtomicityError("child payload_digest가 parent와 다릅니다")
            if child.waveform_sha256 != parent.waveform_sha256:
                raise BroadcastAtomicityError("child waveform_sha256가 parent와 다릅니다")
            if child.scheduled_sample != parent.scheduled_sample:
                raise BroadcastAtomicityError("child scheduled_sample이 parent와 다릅니다")
            if child.channel != parent.channel:
                raise BroadcastAtomicityError("child channel이 parent와 다릅니다")
            seen_cables.add(child.cable)
            seen_receivers.add(child.rx_station)
        if len(seen_cables) != constants.FANOUT_CHILDREN:
            raise BroadcastAtomicityError("child cable이 서로 달라야 합니다")
        if len(seen_receivers) != constants.FANOUT_CHILDREN:
            raise BroadcastAtomicityError("child receiver가 서로 달라야 합니다(비송신 endpoint 둘)")

    # --- required receiver / readiness ------------------------------------
    def required_receivers(self, parent: ParentTransmission, *, dual_channel_active: bool = False) -> tuple:
        """RF commit 전에 RX window가 준비돼야 하는 required receiver 집합(§6.2)."""

        required = set()
        if parent.addressed_destination is not None:
            required.add(parent.addressed_destination)
        for endpoint in topology.natural_tuned(parent.source_station, parent.channel):
            required.add(endpoint)
        if dual_channel_active:
            if constants.STATION_MASS_B in topology.physical_reach(parent.source_station):
                required.add(constants.STATION_MASS_B)
        required.discard(parent.source_station)
        reachable = set(topology.physical_reach(parent.source_station))
        return tuple(sorted(required & reachable))

    def prepare(self, parent: ParentTransmission, *, dual_channel_active: bool = False) -> dict:
        """required receiver readiness를 검증한다. 하나라도 미준비면 BroadcastNotReady."""

        proofs = {}
        for endpoint in self.required_receivers(parent, dual_channel_active=dual_channel_active):
            request = ReadinessRequest(
                endpoint=endpoint,
                channel=parent.channel,
                slot_key=parent.slot_key,
                scheduled_sample=parent.scheduled_sample,
                scheduled_hw_timestamp_ns=parent.scheduled_hw_timestamp_ns,
                clock_id=parent.clock_id,
                bridge_generation=parent.bridge_generation,
                correction_revision=parent.correction_revision,
            )
            decision = self._readiness.window_ready(request)
            proofs[endpoint] = decision
            if not decision.ready:
                raise BroadcastNotReady(
                    f"required receiver {endpoint} RX window 미준비: {decision.reason}"
                )
        return proofs

    # --- atomic group commit ----------------------------------------------
    def transmit(self, parent: ParentTransmission, *, dual_channel_active: bool = False) -> BroadcastResult:
        """atomic 2단계 방사(prepare_group -> commit_group). 두 child를 함께 방사하거나 아무 것도 안 한다."""

        if parent.parent_id in self._active_groups:
            raise BroadcastAtomicityError("같은 parent를 두 번 transmit할 수 없습니다(exactly-once)")
        if len(self._active_groups) >= self._max_active_groups:
            raise BroadcastCapacityError(
                f"활성 broadcast group {len(self._active_groups)}가 상한 {self._max_active_groups}를 넘습니다"
            )

        # 1) readiness barrier (실패 시 RF 전 fail-closed, 방사 0).
        self.prepare(parent, dual_channel_active=dual_channel_active)

        # 2) group staging: 두 child를 함께 admission. 실패 시 아무 것도 방사되지 않는다.
        admission = self._sink.prepare_group(group_id=parent.parent_id, children=parent.children)

        # 3) atomic commit: 하나의 호출로 두 child를 같은 hardware time에 방사(all-or-nothing).
        self._active_groups[parent.parent_id] = {"parent": parent, "outcome": ParentOutcome.PENDING}
        try:
            self._sink.commit_group(admission)
        except Exception as error:
            # commit 결과가 불명확하다: 성공으로 만들지 않고 OUTCOME_UNKNOWN으로 남긴다(§6.1).
            self._active_groups[parent.parent_id]["outcome"] = ParentOutcome.OUTCOME_UNKNOWN
            raise BroadcastAtomicityError(
                f"group commit 결과 불명확 -> OUTCOME_UNKNOWN (parent {parent.parent_id}): {error}"
            ) from error

        return BroadcastResult(
            parent=parent,
            committed=True,
            submitted_child_ids=tuple(c.child_id for c in parent.children),
            outcome=ParentOutcome.PENDING,
            reason="COMMITTED",
        )

    # --- terminal 취합 ------------------------------------------------------
    def resolve_outcome(self, parent_id: str, terminals: dict) -> ParentOutcome:
        """두 child terminal을 취합해 parent outcome을 확정한다(§6.1).

        - 모든 child COMPLETED -> COMPLETED.
        - 하나라도 UNKNOWN(또는 terminal 누락) -> OUTCOME_UNKNOWN(성공 아님).
        - UNKNOWN 없이 하나라도 FAILED -> FAILED.
        확정된 parent는 in-flight active group에서 제거한다.
        """

        entry = self._active_groups.get(parent_id)
        if entry is None:
            raise BroadcastError(f"알 수 없거나 이미 확정된 parent: {parent_id}")
        parent = entry["parent"]
        seen = {ChildTerminal(t) for t in (terminals.get(c.child_id) for c in parent.children)
                if t is not None}
        missing = any(c.child_id not in terminals for c in parent.children)
        if missing or ChildTerminal.UNKNOWN in seen:
            outcome = ParentOutcome.OUTCOME_UNKNOWN
        elif ChildTerminal.FAILED in seen:
            outcome = ParentOutcome.FAILED
        else:
            outcome = ParentOutcome.COMPLETED
        entry["outcome"] = outcome
        # 확정된 parent는 in-flight에서 내린다(자원 회수는 상위 provider가 arbiter로).
        self._active_groups.pop(parent_id, None)
        return outcome

    def outcome_of(self, parent_id: str) -> ParentOutcome | None:
        entry = self._active_groups.get(parent_id)
        return entry["outcome"] if entry else None

    # --- cleanup -----------------------------------------------------------
    def release(self, parent_id: str) -> None:
        self._active_groups.pop(parent_id, None)

    def cleanup(self) -> None:
        """fan-out group·collision buffer·observer lease·dual-tune 잔존 0으로 정리."""

        self._active_groups.clear()

    @property
    def active_group_count(self) -> int:
        return len(self._active_groups)


__all__ = (
    "BroadcastError",
    "BroadcastNotReady",
    "BroadcastAtomicityError",
    "SiblingCollisionError",
    "BroadcastCapacityError",
    "ChildTerminal",
    "ParentOutcome",
    "ScheduledSlot",
    "ReadinessRequest",
    "ReadinessDecision",
    "GroupAdmission",
    "ChildPath",
    "ParentTransmission",
    "BroadcastResult",
    "ReferenceWaveformFactory",
    "AtomicBroadcast",
)
