"""Stage 4·5와 외부 PoC SDK가 사용하는 public type 정의.

명세 §6~§9의 message body 의미를 dataclass로 고정한다. 이 type들은 production
private 구현을 참조하지 않는다(공개 계약만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PhysicalChannel(str, Enum):
    """송신 Physical Channel(§8.4, §9.4). server/runtime이 논리 target에서 유도한다."""

    LOWER = "lower"
    UPPER = "upper"


class TuningState(str, Enum):
    """receiver의 tuning/observation 상태(§8.4)."""

    TUNED_OBSERVER = "TUNED_OBSERVER"
    NOT_TUNED = "NOT_TUNED"


#: 논리 target → canonical Physical Channel(§9.4, `POC-D23`). ROC=lower, MASS-A=upper.
TARGET_TO_PHYSICAL_CHANNEL = {
    "ROC": PhysicalChannel.LOWER,
    "MASS-A": PhysicalChannel.UPPER,
}


class ActionKind(str, Enum):
    """허용 action 의미(실행 명세 §9.1). 실제 RF encode/submit은 Stage 3 범위 밖이다."""

    REPLAY_PACKET = "REPLAY_PACKET"
    MUTATE_PACKET = "MUTATE_PACKET"
    FORGE_PACKET = "FORGE_PACKET"
    DUPLICATE_PACKET = "DUPLICATE_PACKET"


#: `ACTION_SUBMIT` body에서 거부하는 RF 관련 field(§9.4). client는 논리 target만 싣는다.
FORBIDDEN_ACTION_FIELDS = frozenset(
    {
        "frequency",
        "center_frequency",
        "rf_lane",
        "lane",
        "gain",
        "sample_rate",
        "bandwidth",
        "analog_bandwidth",
        "waveform",
        "raw_iq",
        "iq",
        "lo",
        "lo_freq",
        "dsp_freq",
        "antenna",
        "physical_channel",
        "channels",
        "simultaneous_channels",
    }
)


class AdmissionResult(str, Enum):
    """`ACTION_RESULT`의 admission 구분(§9.2, Stage 3 계약: accepted/rejected/unknown)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CACHED = "cached"


@dataclass(frozen=True, slots=True)
class Negotiation:
    """`WELCOME`의 협상 결과(§7.2). reconnect는 같은 값만 복원하며 확대·변경하지 않는다."""

    protocol_minor: int
    capabilities: frozenset
    triggers: frozenset
    connection_epoch: int

    def as_json_value(self) -> dict:
        return {
            "protocol_minor": self.protocol_minor,
            "capabilities": sorted(self.capabilities),
            "triggers": sorted(self.triggers),
            "connection_epoch": self.connection_epoch,
        }


@dataclass(frozen=True, slots=True)
class PocManifestDescriptor:
    """HELLO가 제출하고 server가 협상에 쓰는 manifest 요약(§7.1, 실행 명세 §6.3).

    Stage 3은 catalog·snapshot(Stage 4)을 구현하지 않는다. 이 descriptor는 이미
    검증된 workload의 협상 입력이며, script/manifest SHA-256과 요청 capability/trigger,
    required capability만 담는다.
    """

    poc_id: str
    version: str
    entrypoint: str
    compatible_scenarios: tuple
    attacker_endpoint: str
    allowed_targets: tuple
    requested_capabilities: tuple
    requested_triggers: tuple
    required_capabilities: tuple
    script_sha256: str
    manifest_sha256: str
    max_actions_requested: int

    def as_hello_value(self) -> dict:
        return {
            "poc_id": self.poc_id,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "compatible_scenarios": list(self.compatible_scenarios),
            "attacker_endpoint": self.attacker_endpoint,
            "allowed_targets": list(self.allowed_targets),
            "requested_capabilities": list(self.requested_capabilities),
            "requested_triggers": list(self.requested_triggers),
            "required_capabilities": list(self.required_capabilities),
            "script_sha256": self.script_sha256,
            "manifest_sha256": self.manifest_sha256,
            "max_actions_requested": self.max_actions_requested,
        }


@dataclass(frozen=True, slots=True)
class Event:
    """server가 SDK로 전달하는 논리 event(§8.1). reconnect replay에서도 동일하다."""

    sequence: int
    event_id: str
    type: str
    body: dict = field(default_factory=dict)
    payload_digest: str = ""

    def as_json_value(self) -> dict:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.type,
            "payload_digest": self.payload_digest,
            "body": dict(self.body),
        }


@dataclass(frozen=True, slots=True)
class ActionDescriptor:
    """callback이 반환하는 action 요청(§9, 실행 명세 §9). 논리 target만 담는다.

    `request_id`는 SDK가 전송 전에 부여한다(§9.1). RF 관련 field는 담을 수 없다(§9.4).
    """

    kind: ActionKind
    target: str
    source_observation_id: str | None = None
    schedule: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)

    def as_body(self) -> dict:
        body = {
            "action_kind": ActionKind(self.kind).value,
            "target": self.target,
            "schedule": dict(self.schedule),
            "payload": dict(self.payload),
        }
        if self.source_observation_id is not None:
            body["source_observation_id"] = self.source_observation_id
        return body


@dataclass(frozen=True, slots=True)
class ActionResult:
    """`ACTION_RESULT`(§9.2). accepted/rejected/cached와 선택 canonical channel·parent 기록."""

    request_id: str
    admission: AdmissionResult
    physical_channel: PhysicalChannel | None = None
    broadcast_parent_id: str | None = None
    transfer_id: str | None = None
    error_code: str | None = None

    def as_json_value(self) -> dict:
        return {
            "request_id": self.request_id,
            "admission": AdmissionResult(self.admission).value,
            "physical_channel": (
                self.physical_channel.value if self.physical_channel is not None else None
            ),
            "broadcast_parent_id": self.broadcast_parent_id,
            "transfer_id": self.transfer_id,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ActionTerminal:
    """accepted action의 최종 상태(§6.3 `ACTION_TERMINAL`)."""

    request_id: str
    transfer_id: str | None
    action_execution: str
    target_effect: str | None = None
    physical_channel: PhysicalChannel | None = None
    broadcast_parent_id: str | None = None

    def as_json_value(self) -> dict:
        return {
            "request_id": self.request_id,
            "transfer_id": self.transfer_id,
            "action_execution": self.action_execution,
            "target_effect": self.target_effect,
            "physical_channel": (
                self.physical_channel.value if self.physical_channel is not None else None
            ),
            "broadcast_parent_id": self.broadcast_parent_id,
        }


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    """ActionProvider seam이 반환하는 admission 결과(§9.2, §9.4).

    Stage 3은 fake provider로만 채운다. Stage 5가 G1-호환 production provider를
    연결하면 같은 계약으로 canonical Physical Channel과 parent transmission identity를
    반환한다.
    """

    physical_channel: PhysicalChannel
    broadcast_parent_id: str
    transfer_id: str


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """관측 provider가 내는 한 건의 관측(§8.4, 실행 명세 §8.2).

    `tuning_state == NOT_TUNED`이면 supervisor는 `PACKET_OBSERVED` event나 bytes를
    만들지 않고 evidence counter/state로만 기록한다(§8.4). `NOT_TUNED`는
    transport/connection 오류가 아니다.
    """

    physical_channel: PhysicalChannel
    tuning_state: TuningState
    observation_id: str
    source_station: str
    destination_station: str
    device_hw_timestamp_ns: int
    host_mono_timestamp_ns: int
    body: dict = field(default_factory=dict)

    def as_event_body(self) -> dict:
        return {
            "physical_channel": self.physical_channel.value,
            "tuning_state": self.tuning_state.value,
            "observation_id": self.observation_id,
            "source_station": self.source_station,
            "destination_station": self.destination_station,
            "device_hw_timestamp_ns": self.device_hw_timestamp_ns,
            "host_mono_timestamp_ns": self.host_mono_timestamp_ns,
            **dict(self.body),
        }


__all__ = (
    "PhysicalChannel",
    "TuningState",
    "TARGET_TO_PHYSICAL_CHANNEL",
    "ActionKind",
    "FORBIDDEN_ACTION_FIELDS",
    "AdmissionResult",
    "Negotiation",
    "PocManifestDescriptor",
    "Event",
    "ActionDescriptor",
    "ActionResult",
    "ActionTerminal",
    "ProviderSubmission",
    "ObservationRecord",
)
