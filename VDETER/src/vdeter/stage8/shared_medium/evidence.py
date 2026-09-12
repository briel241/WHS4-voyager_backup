"""판정 분리·상관·redaction evidence (명세 §9.1, §9.2, WSM-REQ-10/16).

네 결과를 서로 재계산하지 않고 분리 기록한다: scenario_result, broadcast_infrastructure_result,
poc_result, delivery_matrix. parent와 child path를 correlation하고 secret(raw payload·key)을
남기지 않는다(payload_digest만). 이 evidence 조립은 RF critical path 밖에서 수행한다(bounded
memory dataclass만 만들고 직렬화는 quiescent 후).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from .constants import MAX_DELIVERY_MATRIX_ENTRIES


@dataclass(frozen=True, slots=True)
class SharedMediumEvidence:
    scenario_result: dict = field(default_factory=dict)
    broadcast_infrastructure_result: dict = field(default_factory=dict)
    poc_result: dict = field(default_factory=dict)
    delivery_matrix: tuple = ()

    def as_json_value(self) -> dict:
        return deepcopy({
            "model_boundary": {
                "model": "WIRED_SHARED_MEDIUM_EMULATION",
                "collision_policy": "LOCAL_DECODED_WINDOW_OVERLAP_DROP_V1",
                "collision_scope": "CO_DECODED_LANES_IN_ONE_COMPLETED_RX_WINDOW",
                "undecoded_interference_detection": False,
                "rf_off_scene_model": "TX_INTERVAL_DROP_V1",
                "virtual_clipping": "NOT_IMPLEMENTED_REMOVED_V1_4",
                "single_antenna_adc_equivalence_proven": False,
                "analog_adc_saturation_proven": False,
            },
            "scenario_result": dict(self.scenario_result),
            "broadcast_infrastructure_result": dict(self.broadcast_infrastructure_result),
            "poc_result": dict(self.poc_result),
            "delivery_matrix": list(self.delivery_matrix),
        })


def build_evidence(parents, scene, *, broadcast_results=(), outcomes=None, readiness_by_parent=None,
                   unresolved_ownership=(), scenario_result=None, poc_result=None):
    """네 판정을 분리 기록한 :class:`SharedMediumEvidence`를 만든다(RF 밖 조립).

    delivery_matrix의 각 row는 parent_id로 broadcast parent evidence와 correlation된다.
    `outcomes`(parent_id -> ParentOutcome)를 주면 최초 PENDING BroadcastResult 대신 그 확정 결과를
    기록한다(terminal 취합 반영). provider.outcomes_by_parent()를 넘길 수 있다.
    """

    broadcast_results = tuple(broadcast_results)
    parent_evidence = [p.as_evidence() for p in parents]
    committed = tuple(r.parent.parent_id for r in broadcast_results if r.committed)
    submitted_children = tuple(
        cid for r in broadcast_results for cid in r.submitted_child_ids
    )
    # bounded evidence(§10, WSM-REQ-17): delivery_matrix 직렬화 상한을 적용하고 초과분은
    # dropped counter/truncated flag로만 남긴다(무제한 증가 방지).
    results_by_parent = {r.parent.parent_id: r for r in broadcast_results}
    proofs_by_parent = {pid: result.readiness_proofs for pid, result in results_by_parent.items()}
    proofs_by_parent.update(readiness_by_parent or {})

    def readiness_value(decision):
        return {
            "state": "REQUIRED_READY" if decision.ready else "NOT_READY",
            "ready": decision.ready,
            "reason": decision.reason,
            "proof": deepcopy(decision.proof),
        }

    # Readiness exists before commit and does not require a successful RX row.
    readiness_evidence = {
        parent["parent_id"]: {endpoint: readiness_value(decision)
                              for endpoint, decision in proofs_by_parent[parent["parent_id"]].items()}
        for parent in parent_evidence if parent["parent_id"] in proofs_by_parent
    }
    delivery_matrix = []
    for row in scene.rows[:MAX_DELIVERY_MATRIX_ENTRIES]:
        row_evidence = row.as_evidence()
        row_evidence["readiness"] = deepcopy(readiness_evidence.get(row.parent_id, {}).get(row.endpoint))
        delivery_matrix.append(row_evidence)
    delivery_matrix = tuple(delivery_matrix)
    serialization_dropped = len(scene.rows) - len(delivery_matrix)
    dropped_total = serialization_dropped + getattr(scene, "dropped_rows", 0)
    outcomes = outcomes or {}

    def _effective(result):
        # 확정된 outcome(terminal 취합)을 우선하고, 없으면 BroadcastResult의 최초 값을 쓴다.
        value = outcomes.get(result.parent.parent_id, result.outcome)
        return getattr(value, "value", str(value))

    parent_outcomes = {r.parent.parent_id: _effective(r) for r in broadcast_results}
    # A failed commit has no successful BroadcastResult, but its unknown outcome is evidence.
    parent_outcomes.update({pid: getattr(value, "value", str(value))
                            for pid, value in outcomes.items()})

    infra = {
        "parents": parent_evidence,
        "committed_parent_ids": list(committed),
        "parent_outcomes": parent_outcomes,
        "readiness_by_parent": readiness_evidence,
        "submitted_child_ids": list(submitted_children),
        "submitted_child_count": len(submitted_children),
        "not_tuned_counter": scene.not_tuned_counter,
        "poc_private_observation_ids": [o.observation_id for o in scene.poc_private_observations],
        "delivery_matrix_truncated": bool(dropped_total) or getattr(scene, "delivery_matrix_truncated", False),
        "delivery_matrix_dropped_rows": dropped_total,
        "delivery_matrix_cap": MAX_DELIVERY_MATRIX_ENTRIES,
        # release 실패로 소유권이 불확실한 자원(§ fault-injection): 식별자를 evidence에 보존한다.
        "unresolved_ownership": sorted(unresolved_ownership),
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
