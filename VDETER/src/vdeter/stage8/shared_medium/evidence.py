"""판정 분리·상관·redaction evidence (명세 §9.1, §9.2, WSM-REQ-10/16).

네 결과를 서로 재계산하지 않고 분리 기록한다: scenario_result, broadcast_infrastructure_result,
poc_result, delivery_matrix. parent와 child path를 correlation하고 secret(raw payload·key)을
남기지 않는다(payload_digest만). 이 evidence 조립은 RF critical path 밖에서 수행한다(bounded
memory dataclass만 만들고 직렬화는 quiescent 후).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SharedMediumEvidence:
    scenario_result: dict = field(default_factory=dict)
    broadcast_infrastructure_result: dict = field(default_factory=dict)
    poc_result: dict = field(default_factory=dict)
    delivery_matrix: tuple = ()

    def as_json_value(self) -> dict:
        return {
            "scenario_result": dict(self.scenario_result),
            "broadcast_infrastructure_result": dict(self.broadcast_infrastructure_result),
            "poc_result": dict(self.poc_result),
            "delivery_matrix": list(self.delivery_matrix),
        }


def build_evidence(parents, scene, *, broadcast_results=(), scenario_result=None, poc_result=None):
    """네 판정을 분리 기록한 :class:`SharedMediumEvidence`를 만든다(RF 밖 조립).

    delivery_matrix의 각 row는 parent_id로 broadcast parent evidence와 correlation된다.
    """

    parent_evidence = [p.as_evidence() for p in parents]
    committed = tuple(r.parent.parent_id for r in broadcast_results if r.committed)
    submitted_children = tuple(
        cid for r in broadcast_results for cid in r.submitted_child_ids
    )
    delivery_matrix = tuple(row.as_evidence() for row in scene.rows)

    infra = {
        "parents": parent_evidence,
        "committed_parent_ids": list(committed),
        "submitted_child_ids": list(submitted_children),
        "submitted_child_count": len(submitted_children),
        "not_tuned_counter": scene.not_tuned_counter,
        "poc_private_observation_ids": [o.observation_id for o in scene.poc_private_observations],
    }
    return SharedMediumEvidence(
        scenario_result=dict(scenario_result or {}),
        broadcast_infrastructure_result=infra,
        poc_result=dict(poc_result or {}),
        delivery_matrix=delivery_matrix,
    )


__all__ = (
    "SharedMediumEvidence",
    "build_evidence",
)
