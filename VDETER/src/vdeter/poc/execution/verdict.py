"""scenario·PoC infrastructure·attack outcome의 **독립** 판정 모델(실행 명세 §11).

세 축을 합산하지 않고 별도로 기록한다(`S8-POC-REQ-15`). 공격 거부·무효는 scenario
verdict를 재계산하지 않으며(`S8-POC-REQ-16`), 오류를 성공으로 추정하지 않는다.
각 attack record는 action execution과 target effect를 다시 구분한다(§11.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class ScenarioVerdict(str, Enum):
    """기존 시나리오 판정(§11.1). PoC 존재 여부와 무관하게 결정된다."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class PocInfrastructure(str, Enum):
    """package·process·API·callback 실행 건전성(§11.1)."""

    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    ERROR = "ERROR"


class ActionExecution(str, Enum):
    """action의 실행(전송) 구분(§11.1). scheduler RF-전 거부와 실제 전송을 나눈다."""

    REJECTED_BEFORE_TX = "REJECTED_BEFORE_TX"
    TX_COMPLETED = "TX_COMPLETED"
    TX_FAILED = "TX_FAILED"
    TX_UNKNOWN = "TX_UNKNOWN"


class TargetEffect(str, Enum):
    """target 반응 구분(§11.1). target 보안계층 거부는 infra 실패가 아니다."""

    REJECTED_BY_TARGET = "REJECTED_BY_TARGET"
    NO_EFFECT = "NO_EFFECT"
    EFFECT_OBSERVED = "EFFECT_OBSERVED"
    INDETERMINATE = "INDETERMINATE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class AttackRecord:
    """공격 하나의 결과(§11.1, §12.2). execution과 target effect를 분리 기록한다."""

    request_id: str
    action_kind: str
    target: str
    trigger: str
    source_observation_id: str | None
    schedule: dict
    action_execution: ActionExecution
    target_effect: TargetEffect
    physical_channel: str | None = None
    broadcast_parent_id: str | None = None
    transfer_id: str | None = None
    reason: str | None = None

    def as_json_value(self) -> dict:
        return {
            "request_id": self.request_id,
            "action_kind": self.action_kind,
            "target": self.target,
            "trigger": self.trigger,
            "source_observation_id": self.source_observation_id,
            "schedule": dict(self.schedule),
            "action_execution": ActionExecution(self.action_execution).value,
            "target_effect": TargetEffect(self.target_effect).value,
            "physical_channel": self.physical_channel,
            "broadcast_parent_id": self.broadcast_parent_id,
            "transfer_id": self.transfer_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    """한 workload의 세 독립 결과 + evidence 요약(§11, §12.4).

    이 model이 renderer(§12.2)와 `vdeter results` read-only 재출력(§12.3)의 단일
    입력이다. 직렬화(:meth:`as_json_value`)와 복원(:meth:`from_json_value`)이
    항등이어야 read-only 재출력이 원본과 동일하다.
    """

    run_id: str
    workload_id: str
    scenario_id: str
    poc_id: str | None
    poc_version: str | None
    attacker_endpoint: str | None
    script_sha256: str | None
    manifest_sha256: str | None
    binding_digest: str | None
    interpreter: str | None
    scenario_verdict: ScenarioVerdict
    poc_infrastructure: PocInfrastructure
    attacks: tuple = ()
    infrastructure_error_code: str | None = None
    lifecycle_transitions: tuple = ()
    evidence: dict = field(default_factory=dict)

    # --- 파생 집계(재계산 없이 표시용) ---------------------------------------
    @property
    def attacks_requested(self) -> int:
        return len(self.attacks)

    @property
    def attacks_completed(self) -> int:
        return sum(1 for a in self.attacks
                   if a.action_execution == ActionExecution.TX_COMPLETED)

    @property
    def effects_observed(self) -> int:
        return sum(1 for a in self.attacks
                   if a.target_effect == TargetEffect.EFFECT_OBSERVED)

    def as_json_value(self) -> dict:
        return {
            "run_id": self.run_id,
            "workload_id": self.workload_id,
            "scenario_id": self.scenario_id,
            "poc_id": self.poc_id,
            "poc_version": self.poc_version,
            "attacker_endpoint": self.attacker_endpoint,
            "script_sha256": self.script_sha256,
            "manifest_sha256": self.manifest_sha256,
            "binding_digest": self.binding_digest,
            "interpreter": self.interpreter,
            "scenario_verdict": ScenarioVerdict(self.scenario_verdict).value,
            "poc_infrastructure": PocInfrastructure(self.poc_infrastructure).value,
            "infrastructure_error_code": self.infrastructure_error_code,
            "lifecycle_transitions": list(self.lifecycle_transitions),
            "attacks": [a.as_json_value() for a in self.attacks],
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_json_value(cls, value: dict) -> "WorkloadResult":
        attacks = tuple(
            AttackRecord(
                request_id=a["request_id"],
                action_kind=a["action_kind"],
                target=a["target"],
                trigger=a["trigger"],
                source_observation_id=a.get("source_observation_id"),
                schedule=dict(a.get("schedule") or {}),
                action_execution=ActionExecution(a["action_execution"]),
                target_effect=TargetEffect(a["target_effect"]),
                physical_channel=a.get("physical_channel"),
                broadcast_parent_id=a.get("broadcast_parent_id"),
                transfer_id=a.get("transfer_id"),
                reason=a.get("reason"),
            )
            for a in value.get("attacks", [])
        )
        return cls(
            run_id=value["run_id"],
            workload_id=value["workload_id"],
            scenario_id=value["scenario_id"],
            poc_id=value.get("poc_id"),
            poc_version=value.get("poc_version"),
            attacker_endpoint=value.get("attacker_endpoint"),
            script_sha256=value.get("script_sha256"),
            manifest_sha256=value.get("manifest_sha256"),
            binding_digest=value.get("binding_digest"),
            interpreter=value.get("interpreter"),
            scenario_verdict=ScenarioVerdict(value["scenario_verdict"]),
            poc_infrastructure=PocInfrastructure(value["poc_infrastructure"]),
            attacks=attacks,
            infrastructure_error_code=value.get("infrastructure_error_code"),
            lifecycle_transitions=tuple(value.get("lifecycle_transitions", [])),
            evidence=dict(value.get("evidence") or {}),
        )


__all__ = (
    "ScenarioVerdict",
    "PocInfrastructure",
    "ActionExecution",
    "TargetEffect",
    "AttackRecord",
    "WorkloadResult",
)
