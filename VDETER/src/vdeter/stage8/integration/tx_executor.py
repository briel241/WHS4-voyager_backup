"""accepted action의 terminal을 **실제 provider outcome**에서 유도하는 production TxExecutor.

Stage 4 runner는 accepted action마다 `TxExecutor((action, result) -> TxResolution)`를 호출해
action_execution과 target_effect를 나눠 확정한다(§11.1). RF-off 시험에서는 fake effect 모델을
썼지만, production 통합에서는 이 executor가:

- **action_execution**: `provider.resolve_outcome(request_id, terminals)`로 parent/child terminal을
  취합한다. accepted가 되었다는 것은 group commit이 성공했다는 뜻이므로(commit 불명확이면
  submit이 이미 SESSION_INHIBIT을 냄) RF-off에서는 두 child가 COMPLETED다. 취합 결과를
  COMPLETED->TX_COMPLETED / FAILED->TX_FAILED / OUTCOME_UNKNOWN->TX_UNKNOWN으로 매핑한다.
  resolve_outcome 자체가 SESSION_INHIBIT(OUTCOME_UNKNOWN·OWNERSHIP_UNCERTAIN)을 내면 성공으로
  추정하지 않고 TX_UNKNOWN으로 남긴다(runner가 그 뒤 fail-closed).
- **target_effect**: shared_medium `observe_scene`의 delivery matrix로 addressed target이 실제
  composite를 PHY decode·public delivery했는지 본다. decode됨 -> INDETERMINATE(전달됨; 실 target
  application/security 수락·거부 반응은 **Stage 6 실장비**). decode 안 됨 -> NO_EFFECT.
  **EFFECT_OBSERVED를 합성하지 않는다**(§9.1/§11.1: 실 target 반응 evidence 없이 성공 추정 금지).

optional `target_response(action, delivered) -> (TargetEffect, reason)`로 실 target 보안 반응
모델을 주입할 수 있으나 기본은 보수적(INDETERMINATE/NO_EFFECT)이다.
"""

from __future__ import annotations

from ..execution.runner import TxResolution
from ..execution.verdict import ActionExecution, TargetEffect
from ..poc_api.errors import PocError
from ..shared_medium.observation import observe_scene
from ..shared_medium.radiation import ChildTerminal, ParentOutcome

_EXECUTION_BY_OUTCOME = {
    ParentOutcome.COMPLETED: ActionExecution.TX_COMPLETED,
    ParentOutcome.FAILED: ActionExecution.TX_FAILED,
    ParentOutcome.OUTCOME_UNKNOWN: ActionExecution.TX_UNKNOWN,
    ParentOutcome.PENDING: ActionExecution.TX_UNKNOWN,
    ParentOutcome.REJECTED_BEFORE_TX: ActionExecution.REJECTED_BEFORE_TX,
}


class ProductionTxExecutor:
    """provider outcome + delivery matrix로 accepted action terminal을 유도한다(fake 아님).

    `child_terminals`는 RF-off에서 두 fan-out child의 terminal을 어떻게 볼지 정한다. 기본은
    COMPLETED(software group TX가 두 lane을 방사 완료). 실장비에서는 실제 RF terminal 콜백이
    이 자리를 대신한다(Stage 6).
    """

    def __init__(self, provider, *, target_response=None,
                 child_terminals: str = ChildTerminal.COMPLETED.value) -> None:
        self._provider = provider
        self._target_response = target_response
        self._child_terminals = child_terminals

    def __call__(self, action: dict, result: dict) -> TxResolution:
        request_id = result.get("request_id")
        parent = self._provider.parent_for(request_id)
        if parent is None:
            # accepted인데 parent correlation이 없으면 성공으로 추정하지 않는다.
            return TxResolution(ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                                "parent correlation missing")
        terminals = {child.child_id: self._child_terminals for child in parent.children}
        try:
            outcome = self._provider.resolve_outcome(request_id, terminals)
        except PocError as error:
            # OUTCOME_UNKNOWN·OWNERSHIP_UNCERTAIN 등 SESSION_INHIBIT: 성공 추정 금지.
            return TxResolution(ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                                error.code.value)
        execution = _EXECUTION_BY_OUTCOME.get(outcome, ActionExecution.TX_UNKNOWN)
        if execution != ActionExecution.TX_COMPLETED:
            return TxResolution(execution, TargetEffect.INDETERMINATE, outcome.value)
        effect, reason = self._target_effect(action, parent)
        return TxResolution(execution, effect, reason)

    def _target_effect(self, action: dict, parent) -> tuple:
        target = parent.addressed_destination
        try:
            scene = observe_scene([parent])
            row = next((r for r in scene.rows
                        if r.endpoint == target and r.parent_id == parent.parent_id), None)
            delivered = bool(row is not None and row.public_delivery)
        except Exception:  # noqa: BLE001 - delivery 평가 실패가 terminal 경로를 막지 않는다
            return TargetEffect.INDETERMINATE, "delivery evaluation unavailable"
        if self._target_response is not None:
            return self._target_response(action, delivered)
        if not delivered:
            return TargetEffect.NO_EFFECT, "target did not decode composite (RF-off PHY)"
        # PHY decode·전달됨. 실 target application/security 수락·거부는 Stage 6 실장비.
        return (TargetEffect.INDETERMINATE,
                "delivered to target PHY; application/security response deferred to Stage 6")


__all__ = ("ProductionTxExecutor",)
