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
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from ..poc_api.codec import sha256_hex
from ..poc_api.limits import MAX_JSON_NESTING, MAX_WIRE_PACKET_BYTES
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


class BroadcastNotAdmitted(BroadcastError):
    """group staging(prepare_group) 실패 — RF 방사 전 거부(안전, TX 0)."""


class BroadcastOutcomeUnknown(BroadcastError):
    """group commit 결과 불명확 — TX가 발생했는지 확실치 않음(§6.1, SESSION_INHIBIT 대상)."""


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
    readiness_proofs: dict = field(default_factory=dict)


def _snapshot_readiness(decision: ReadinessDecision) -> ReadinessDecision:
    """Copy only bounded JSON proof metadata before staging; never render JSON here.

    Reuse the existing wire byte/depth limits. The byte budget is a conservative
    escaped-JSON upper bound, including container punctuation. Raw signal/payload
    fields are not readiness evidence and fail closed before any sink operation.
    """

    remaining = MAX_WIRE_PACKET_BYTES

    def copy_value(value, depth=1):
        nonlocal remaining
        if depth > MAX_JSON_NESTING:
            raise BroadcastNotReady("readiness proof exceeds JSON nesting limit")
        remaining -= 2  # separators/container delimiters
        if isinstance(value, str):
            remaining -= 2 + 12 * len(value)  # includes escaped Unicode pairs
        elif value is None or isinstance(value, bool):
            remaining -= 5
        elif isinstance(value, int):
            remaining -= 1 + value.bit_length()  # safe decimal upper bound
        elif isinstance(value, float) and isfinite(value):
            remaining -= 32
        elif not isinstance(value, (dict, list, tuple)):
            raise BroadcastNotReady("readiness proof contains non-JSON metadata")
        if remaining < 0:
            raise BroadcastNotReady("readiness proof exceeds wire byte limit")
        if isinstance(value, dict):
            copied = {}
            for key, item in value.items():
                if (not isinstance(key, str) or key.lower() in
                        {"samples", "iq", "raw_iq", "cf32le", "payload", "raw_payload"}):
                    raise BroadcastNotReady("readiness proof contains raw signal/payload or invalid key")
                copied[copy_value(key, depth)] = copy_value(item, depth + 1)
            return copied
        if isinstance(value, (list, tuple)):
            return [copy_value(item, depth + 1) for item in value]
        return value

    if not isinstance(decision.proof, dict) or not isinstance(decision.reason, str):
        raise BroadcastNotReady("readiness proof/reason has invalid metadata type")
    return ReadinessDecision(decision.ready, copy_value(decision.reason), copy_value(decision.proof))


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
        self._factory = ReferenceWaveformFactory() if waveform_factory is None else waveform_factory
        self._arbiter = arbiter
        self._max_active_groups = int(max_active_groups)
        #: parent_id -> {"parent": ParentTransmission, "outcome": ParentOutcome}
        #: commit됐지만 아직 terminal 미확정인 in-flight group만 담는다(resolve/cleanup에서 제거).
        self._active_groups: dict = {}
        #: parent_id -> ParentOutcome. resolve_outcome이 확정한 결과를 persist한다(evidence 반영).
        self._resolved: dict = {}

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
        if (parent.waveform.channel != parent.channel
                or parent.waveform.start_sample != parent.scheduled_sample
                or parent.waveform.sha256 != parent.waveform_sha256
                or sha256_hex(parent.payload) != parent.payload_digest):
            raise BroadcastAtomicityError("parent waveform/payload identity가 일치하지 않습니다")
        expected_cables = set(topology.fanout_cables(parent.source_station))
        seen_cables = set()
        seen_receivers = set()
        for child in parent.children:
            if child.cable not in expected_cables:
                raise BroadcastAtomicityError("child cable이 송신자 fan-out 경로가 아닙니다")
            if (child.tx_station, child.tx_lane, child.rx_station, child.rx_lane) != constants.CABLE_PATHS[child.cable]:
                raise BroadcastAtomicityError("child station/lane이 고정 cable 경로와 다릅니다")
            if child.child_id != f"{parent.parent_id}:{child.cable}":
                raise BroadcastAtomicityError("child identity가 parent/cable과 다릅니다")
            if (child.scheduled_hw_timestamp_ns != parent.scheduled_hw_timestamp_ns
                    or child.samples != parent.waveform.samples):
                raise BroadcastAtomicityError("child 실제 timestamp/samples가 parent와 다릅니다")
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
            if not isinstance(decision, ReadinessDecision) or not isinstance(decision.ready, bool):
                raise BroadcastNotReady(f"required receiver {endpoint} readiness 응답 형식 오류")
            if not decision.ready:
                raise BroadcastNotReady(
                    f"required receiver {endpoint} RX window 미준비: {decision.reason}"
                )
        return proofs

    # --- atomic group commit ----------------------------------------------
    def transmit(self, parent: ParentTransmission, *, dual_channel_active: bool = False) -> BroadcastResult:
        """atomic 2단계 방사(prepare_group -> commit_group). 두 child를 함께 방사하거나 아무 것도 안 한다."""

        self._assert_single_burst_invariant(parent)
        if parent.parent_id in self._active_groups or parent.parent_id in self._resolved:
            raise BroadcastAtomicityError("같은 parent를 두 번 transmit할 수 없습니다(exactly-once)")
        if len(self._active_groups) >= self._max_active_groups:
            raise BroadcastCapacityError(
                f"활성 broadcast group {len(self._active_groups)}가 상한 {self._max_active_groups}를 넘습니다"
            )

        # 1) readiness barrier (실패 시 RF 전 fail-closed, 방사 0).
        readiness_proofs = {
            endpoint: _snapshot_readiness(decision)
            for endpoint, decision in self.prepare(
                parent, dual_channel_active=dual_channel_active
            ).items()
        }

        # 2) group staging: 두 child를 함께 admission. 실패 시 RF 방사 전 거부(안전, TX 0).
        try:
            admission = self._sink.prepare_group(group_id=parent.parent_id, children=parent.children)
        except Exception as error:
            raise BroadcastNotAdmitted(
                f"group staging 거부 -> RF 방사 전 fail-closed (parent {parent.parent_id}): {error}"
            ) from error

        # 3) atomic commit: 하나의 호출로 두 child를 같은 hardware time에 방사(all-or-nothing).
        self._active_groups[parent.parent_id] = {
            "parent": parent, "outcome": ParentOutcome.PENDING,
            "readiness_proofs": readiness_proofs,
        }
        try:
            self._sink.commit_group(admission)
        except BaseException as error:
            # commit에 진입한 뒤의 어떤 실패(Exception·BaseException)도 TX outcome은 불명확하다.
            # 성공으로 만들지 않고 OUTCOME_UNKNOWN으로 남기며 in-flight group을 유지해 correlation·
            # cleanup을 보존한다(§6.1, SESSION_INHIBIT 경로). BaseException(인터럽트)은 PocError로
            # 바꾸지 않고 상태만 남긴 채 그대로 전파한다(부분 방사를 잃지 않음).
            self._active_groups[parent.parent_id]["outcome"] = ParentOutcome.OUTCOME_UNKNOWN
            self._resolved[parent.parent_id] = ParentOutcome.OUTCOME_UNKNOWN
            if isinstance(error, Exception):
                raise BroadcastOutcomeUnknown(
                    f"group commit 결과 불명확 -> OUTCOME_UNKNOWN (parent {parent.parent_id}): {error}"
                ) from error
            raise

        return BroadcastResult(
            parent=parent,
            committed=True,
            submitted_child_ids=tuple(c.child_id for c in parent.children),
            outcome=ParentOutcome.PENDING,
            reason="COMMITTED",
            readiness_proofs=readiness_proofs,
        )

    # --- terminal 취합 ------------------------------------------------------
    def resolve_outcome(self, parent_id: str, terminals: dict) -> ParentOutcome:
        """두 child terminal을 취합해 parent outcome을 확정한다(§6.1).

        - 모든 child COMPLETED -> COMPLETED.
        - 하나라도 UNKNOWN(또는 terminal 누락) -> OUTCOME_UNKNOWN(성공 아님).
        - UNKNOWN 없이 하나라도 FAILED -> FAILED.
        COMPLETED/FAILED parent는 in-flight에서 제거하고 UNKNOWN은 cleanup까지 보존한다.
        """

        # 입력 검증 우선: 잘못된 terminal은 상태 변경 없이 구조화 오류로 거부한다
        # (parent를 성공 처리하거나 in-flight에서 내리지 않는다; raw 예외 누출 금지).
        # (a) container type: dict가 아니면 거부(list 등 → AttributeError 금지).
        if terminals is None:
            terminals = {}
        if not isinstance(terminals, dict):
            raise BroadcastError(
                f"terminals는 dict여야 합니다(받은 값 type={type(terminals).__name__})"
            )

        entry = self._active_groups.get(parent_id)
        if entry is None:
            raise BroadcastError(f"알 수 없거나 이미 확정된 parent: {parent_id}")
        parent = entry["parent"]

        # (b) child id 집합 검증: parent에 없는 child id는 무시하지 않고 거부한다.
        valid_ids = {c.child_id for c in parent.children}
        unknown_ids = [cid for cid in terminals if cid not in valid_ids]
        if unknown_ids:
            raise BroadcastError(f"parent {parent_id}에 없는 child id: {unknown_ids!r}")

        # (c) terminal 값 검증/coerce: 잘못된 값은 BroadcastError.
        coerced = {}
        for cid, value in terminals.items():
            if value is None:
                continue
            try:
                coerced[cid] = ChildTerminal(value)
            except (TypeError, ValueError) as error:
                raise BroadcastError(
                    f"알 수 없는 child terminal 값: {value!r} (child {cid})"
                ) from error
        seen = {coerced[c.child_id] for c in parent.children if c.child_id in coerced}
        # 키가 없거나 값이 None이면 terminal 미보고 → OUTCOME_UNKNOWN.
        missing = any(terminals.get(c.child_id) is None for c in parent.children)
        if missing or ChildTerminal.UNKNOWN in seen:
            outcome = ParentOutcome.OUTCOME_UNKNOWN
        elif ChildTerminal.FAILED in seen:
            outcome = ParentOutcome.FAILED
        else:
            outcome = ParentOutcome.COMPLETED
        entry["outcome"] = outcome
        # Preserve uncertain groups and their ownership until cleanup, just as for commit failure.
        self._resolved[parent_id] = outcome
        if outcome != ParentOutcome.OUTCOME_UNKNOWN:
            self._active_groups.pop(parent_id, None)
        return outcome

    def outcome_of(self, parent_id: str) -> ParentOutcome | None:
        """확정된 결과(우선) 또는 in-flight PENDING/OUTCOME_UNKNOWN을 돌려준다."""

        if parent_id in self._resolved:
            return self._resolved[parent_id]
        entry = self._active_groups.get(parent_id)
        return entry["outcome"] if entry else None

    def is_resolved(self, parent_id: str) -> bool:
        """parent가 이미 확정(resolve/commit-unknown)됐는지."""

        return parent_id in self._resolved

    def readiness_of(self, parent_id: str) -> dict:
        """Snapshot pre-commit proof even when commit never returns a result.

        Stored within the already bounded active group, not a new history/cache.
        This is metadata only: it proves readiness, not RF commit or reception.
        """
        entry = self._active_groups.get(parent_id)
        return deepcopy(entry.get("readiness_proofs", {})) if entry else {}

    # --- cleanup -----------------------------------------------------------
    def release(self, parent_id: str) -> None:
        self._active_groups.pop(parent_id, None)

    def cleanup(self) -> None:
        """fan-out group·collision buffer·observer lease·dual-tune 잔존 0으로 정리."""

        self._active_groups.clear()
        self._resolved.clear()

    @property
    def active_group_count(self) -> int:
        return len(self._active_groups)


__all__ = (
    "BroadcastError",
    "BroadcastNotReady",
    "BroadcastAtomicityError",
    "SiblingCollisionError",
    "BroadcastCapacityError",
    "BroadcastNotAdmitted",
    "BroadcastOutcomeUnknown",
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
