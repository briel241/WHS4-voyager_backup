"""Tuned observation과 delivery matrix (명세 §5.5, §7, §8, §9, WSM-REQ-04/05/07/08/10/14).

물리 도달(fan-out)과 tuned observation/decode를 분리한다:
- 물리 신호는 비송신 두 endpoint에 항상 도달한다(addressed 무관).
- receiver가 해당 Physical Channel을 듣고 있으면 TUNED_OBSERVER, 아니면 NOT_TUNED.
- NOT_TUNED는 실패가 아니며 packet bytes·payload·성공 event를 합성하지 않고 counter/state로만
  기록한다.
- 같은 channel·시간이 겹치는 arrival은 MODEL_COLLISION_DROP; 합성·클리핑하지 않는다.
- fan-out sibling(같은 parent)끼리는 같은 collision group에 들어갈 수 없다.
- 주소 대상만 public delivery를 일으키고, 비주소 tuned MASS-B POC_ACTOR는 PoC-private
  관측 event만 만든다.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from ..poc_api.codec import sha256_hex
from ..poc_api.types import ObservationRecord, PhysicalChannel, TuningState
from . import constants, topology
from .constants import MAX_DELIVERY_MATRIX_ENTRIES
from .decoder import DecodeStatus, decode
from .iq import IqBurst, group_collisions
from .radiation import ParentTransmission, SiblingCollisionError


@dataclass(frozen=True, slots=True)
class Arrival:
    """한 receiver에 물리적으로 도달한 parent 한 건(fan-out 결과)."""

    parent_id: str
    source_station: str
    endpoint: str
    channel: PhysicalChannel
    addressed: bool
    official_broadcast: bool
    payload_digest: str
    burst: IqBurst


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    """endpoint별 물리 관측·decode·주소 수락 결과(§9.2 delivery matrix)."""

    parent_id: str
    source_station: str
    endpoint: str
    channel: PhysicalChannel
    physical_reach: bool
    tuning_state: str
    decode_status: str | None
    decoded_payload_digest: str | None
    addressed: bool
    public_delivery: bool
    observation_id: str | None
    collision_input_count: int
    overlap_sample_count: int
    interval_evidence: dict | None = None

    def as_evidence(self) -> dict:
        return {
            "parent_id": self.parent_id,
            "source_station": self.source_station,
            "endpoint": self.endpoint,
            "channel": self.channel.value,
            "physical_reach": self.physical_reach,
            "tuning_state": self.tuning_state,
            "decode_status": self.decode_status,
            "decoded_payload_digest": self.decoded_payload_digest,
            "addressed": self.addressed,
            "public_delivery": self.public_delivery,
            "observation_id": self.observation_id,
            "collision_input_count": self.collision_input_count,
            "overlap_sample_count": self.overlap_sample_count,
            "interval_evidence": deepcopy(self.interval_evidence),
        }


@dataclass(frozen=True, slots=True)
class SceneResult:
    rows: tuple
    poc_private_observations: tuple
    not_tuned_counter: int
    delivery_matrix_truncated: bool = False
    dropped_rows: int = 0

    def tuned_endpoints(self, parent_id: str) -> frozenset:
        return frozenset(
            r.endpoint for r in self.rows
            if r.parent_id == parent_id and r.tuning_state == TuningState.TUNED_OBSERVER.value
        )

    def rows_for(self, parent_id: str) -> tuple:
        return tuple(r for r in self.rows if r.parent_id == parent_id)


def build_arrivals(parents) -> dict:
    """parent들의 fan-out child로부터 receiver별 물리 arrival을 만든다(addressed 무관 도달)."""

    by_endpoint: dict = {}
    for parent in parents:
        seen_receivers = {}
        for child in parent.children:
            # 같은 parent의 두 child는 서로 다른 receiver로 간다(sibling 자기충돌 불가 근거).
            if child.rx_station in seen_receivers:
                raise SiblingCollisionError(
                    f"parent {parent.parent_id}의 두 child가 같은 receiver {child.rx_station}로 도달"
                )
            seen_receivers[child.rx_station] = child.cable
            arrival = Arrival(
                parent_id=parent.parent_id,
                source_station=parent.source_station,
                endpoint=child.rx_station,
                channel=parent.channel,
                addressed=(child.rx_station == parent.addressed_destination),
                official_broadcast=parent.official_broadcast,
                payload_digest=parent.payload_digest,
                burst=IqBurst(
                    channel=parent.channel,
                    start_sample=parent.scheduled_sample,
                    samples=parent.waveform.samples,
                ),
            )
            by_endpoint.setdefault(child.rx_station, []).append(arrival)
    return by_endpoint


def observe_scene(parents, *, dual_channel_active: bool = False) -> SceneResult:
    """물리 arrival을 tuned observation·collision·delivery matrix로 평가한다."""

    arrivals_by_endpoint = build_arrivals(parents)
    rows = []
    poc_private = []
    not_tuned_counter = 0
    dropped_rows = 0

    def add_row(row):
        nonlocal dropped_rows
        if len(rows) < MAX_DELIVERY_MATRIX_ENTRIES:
            rows.append(row)  # bounded evidence(§10): 상한 초과분은 dropped counter로만.
        else:
            dropped_rows += 1

    for endpoint, arrivals in arrivals_by_endpoint.items():
        tuned_channels = topology.receiver_tuned_channels(
            endpoint, dual_channel_active=dual_channel_active
        )
        # channel별로 분리한다(다른 channel은 충돌로 묶지 않는다).
        by_channel: dict = {}
        for arrival in arrivals:
            by_channel.setdefault(arrival.channel, []).append(arrival)

        for channel, channel_arrivals in by_channel.items():
            if channel not in tuned_channels:
                # NOT_TUNED: 물리 도달했으나 관측 안 함. bytes·event·error 없음. counter만.
                for arrival in channel_arrivals:
                    not_tuned_counter += 1
                    add_row(
                        DeliveryRow(
                            parent_id=arrival.parent_id,
                            source_station=arrival.source_station,
                            endpoint=endpoint,
                            channel=channel,
                            physical_reach=True,
                            tuning_state=TuningState.NOT_TUNED.value,
                            decode_status=None,
                            decoded_payload_digest=None,
                            addressed=arrival.addressed,
                            public_delivery=False,
                            observation_id=None,
                            collision_input_count=0,
                            overlap_sample_count=0,
                        )
                    )
                continue

            # RF-off scene model. Same receiver/channel + interval overlap
            # drops BOTH, even identical or very unequal-amplitude waveforms.
            # Production must NOT use these planned parents as RX evidence.
            groups = group_collisions([a.burst for a in channel_arrivals])
            index = {id(a.burst): a for a in channel_arrivals}
            for group in groups:
                parent_ids = [index[id(b)].parent_id for b in group]
                if len(set(parent_ids)) != len(parent_ids):
                    raise SiblingCollisionError("duplicate parent at receiver")
                collided = len(group) > 1
                interval_evidence = {
                    "model": "TX_INTERVAL_DROP_V1", "simulation_only": True,
                    "rf_collision_measured": False,
                    "decision": "MODEL_COLLISION_DROP" if collided else "NO_MODEL_COLLISION",
                    "start_sample": min(b.start_sample for b in group),
                    "end_sample": max(b.end_sample for b in group),
                    "parent_ids": parent_ids,
                }
                result = None if collided else decode(group[0].samples)
                digest = sha256_hex(result.payload) if result and result.recovered and result.payload is not None else None
                for burst in group:
                    arrival = index[id(burst)]
                    decoded_here = digest is not None and digest == arrival.payload_digest
                    observation_id = None
                    public_delivery = decoded_here and (arrival.addressed or arrival.official_broadcast)
                    if decoded_here and not public_delivery and endpoint == constants.STATION_MASS_B:
                        observation_id = f"obs-{arrival.parent_id}"
                        poc_private.append(ObservationRecord(
                            physical_channel=channel, tuning_state=TuningState.TUNED_OBSERVER,
                            observation_id=observation_id, source_station=arrival.source_station,
                            destination_station=_addressed_dest(parents, arrival.parent_id) or "",
                            device_hw_timestamp_ns=_scheduled_ns(parents, arrival.parent_id),
                            host_mono_timestamp_ns=_scheduled_ns(parents, arrival.parent_id),
                            body={"payload_digest": arrival.payload_digest, "simulation_only": True,
                                  "device_time_source": "RF_OFF_PLANNED_SAMPLE_NOT_MEASURED_RX"}))
                    add_row(DeliveryRow(
                        parent_id=arrival.parent_id, source_station=arrival.source_station,
                        endpoint=endpoint, channel=channel, physical_reach=True,
                        tuning_state=TuningState.TUNED_OBSERVER.value,
                        decode_status="MODEL_COLLISION_DROP" if collided else result.status.value,
                        decoded_payload_digest=digest if decoded_here else None,
                        addressed=arrival.addressed, public_delivery=public_delivery,
                        observation_id=observation_id, collision_input_count=len(group),
                        overlap_sample_count=max(0, min(b.end_sample for b in group) - max(b.start_sample for b in group)) if collided else 0,
                        interval_evidence=interval_evidence))

    return SceneResult(
        rows=tuple(rows),
        poc_private_observations=tuple(poc_private),
        not_tuned_counter=not_tuned_counter,
        delivery_matrix_truncated=dropped_rows > 0,
        dropped_rows=dropped_rows,
    )


def _addressed_dest(parents, parent_id: str):
    for p in parents:
        if p.parent_id == parent_id:
            return p.addressed_destination
    return None


def _scheduled_ns(parents, parent_id: str) -> int:
    for p in parents:
        if p.parent_id == parent_id:
            return p.scheduled_hw_timestamp_ns
    return 0


def matches_natural_matrix(parent: ParentTransmission, scene: SceneResult) -> bool:
    """이 parent의 TUNED endpoint 집합이 §5.5 자연 관측 행렬과 일치하는지."""

    expected = topology.natural_tuned(parent.source_station, parent.channel)
    return scene.tuned_endpoints(parent.parent_id) == expected


__all__ = (
    "Arrival",
    "DeliveryRow",
    "SceneResult",
    "build_arrivals",
    "observe_scene",
    "matches_natural_matrix",
)
