"""Atomic broadcast transmission: parent-to-2-child fan-out, exactly-once submit, cleanup
(명세 §6, §5.1, WSM-REQ-01/03/04, S8-D005/D007/D014).

핵심 불변:
- 단일 논리 burst는 정확히 하나의 physical parent를 갖는다.
- parent waveform은 한 번 생성하고 두 child가 bytes·samples·channel·gain·start timestamp·
  payload digest를 공유한다(fan-out으로 waveform·RF submit 중복 0).
- 두 child의 admission은 하나의 atomic prepare/commit barrier로 처리한다. 하나라도 준비·
  접수되지 않으면 어느 child도 RF로 commit하지 않는다(반쪽 방사·순차 fallback 금지).
- required receiver RX window가 준비되지 않으면 RF 전에 fail-closed한다. 다른 channel에
  tuned된 비주소 endpoint는 required가 아니며 NOT_TUNED로 non-blocking이다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..poc_api.codec import sha256_hex
from ..poc_api.types import PhysicalChannel
from . import constants, topology
from .iq import IqBurst


class BroadcastError(RuntimeError):
    """shared-medium 방사 계층의 구조화 오류(fail-closed)."""


class BroadcastNotReady(BroadcastError):
    """required receiver RX window 미준비 -> RF 전 fail-closed(§6.2)."""


class BroadcastAtomicityError(BroadcastError):
    """단일 parent·fan-out child 불변 위반(§6.1)."""


class SiblingCollisionError(BroadcastError):
    """같은 parent의 fan-out child가 같은 collision group에 들어감(§7.3, WSM-REQ-12)."""


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
    reason: str = ""


class ReferenceWaveformFactory:
    """RF-off 참조 waveform factory. decoder 참조 modem으로 baseband IQ를 만든다.

    실장비에서는 frozen PHY(vdeter.phy.transmitter.Link11Phy)가 이 역할을 대신한다(Stage 6).
    """

    def encode(self, *, source_station: str, channel, payload: bytes) -> IqBurst:
        from .decoder import encode_payload

        return IqBurst(channel=channel, start_sample=0, samples=encode_payload(payload))


class AtomicBroadcast:
    """송신자별 두 child로 fan-out하고 atomic barrier로 RF commit한다.

    RF critical path(commit)에서는 JSON render·disk write·flush·blocking을 하지 않는다.
    evidence는 bounded memory dataclass로만 만든다(직렬화는 상위에서 quiescent 후).
    """

    def __init__(
        self,
        *,
        radio_sink,
        rx_readiness,
        waveform_factory=None,
        arbiter=None,
    ) -> None:
        self._sink = radio_sink
        self._readiness = rx_readiness
        self._factory = waveform_factory or ReferenceWaveformFactory()
        self._arbiter = arbiter
        #: 활성 fan-out group(cleanup 잔존 0 검증용).
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
        """RF commit 전에 RX window가 준비돼야 하는 required receiver 집합(§6.2).

        addressed destination + 정상 role상 이 channel을 감시하는 자연 TUNED endpoint +
        협상된 dual-channel POC_ACTOR MASS-B. 다른 channel에 tuned된 비주소 endpoint는 제외.
        """

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

    # --- atomic commit -----------------------------------------------------
    def transmit(self, parent: ParentTransmission, *, dual_channel_active: bool = False) -> BroadcastResult:
        """atomic prepare->commit. 두 child를 exactly-once submit하거나 어느 것도 보내지 않는다."""

        if parent.parent_id in self._active_groups:
            raise BroadcastAtomicityError("같은 parent를 두 번 transmit할 수 없습니다(exactly-once)")

        # 1) readiness barrier (실패 시 RF 전 fail-closed, submit 0).
        self.prepare(parent, dual_channel_active=dual_channel_active)

        # 2) atomic commit: 두 child를 각각 정확히 한 번 submit.
        self._active_groups[parent.parent_id] = parent
        submitted = []
        try:
            for child in parent.children:
                self._sink.submit_child(child)
                submitted.append(child.child_id)
        except Exception:
            self._active_groups.pop(parent.parent_id, None)
            raise
        if len(submitted) != constants.FANOUT_CHILDREN:
            self._active_groups.pop(parent.parent_id, None)
            raise BroadcastAtomicityError("두 child가 모두 submit되지 않았습니다")
        return BroadcastResult(
            parent=parent,
            committed=True,
            submitted_child_ids=tuple(submitted),
            reason="COMMITTED",
        )

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
    "ReadinessRequest",
    "ReadinessDecision",
    "ChildPath",
    "ParentTransmission",
    "BroadcastResult",
    "ReferenceWaveformFactory",
    "AtomicBroadcast",
)
