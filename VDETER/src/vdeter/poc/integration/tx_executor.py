"""accepted action의 terminal을 **실제 provider outcome**에서 유도하는 production TxExecutor.

Stage 4 runner는 accepted action마다 `TxExecutor((action, result) -> TxResolution)`를 호출해
action_execution과 target_effect를 나눠 확정한다(§11.1). 이 executor는 **실제 terminal/수신 증거
source를 필수로 연결**하고, 없으면 성공을 합성하지 않는다.

- **action_execution**: `terminal_source`가 공급한 실제 child terminal을 `provider.resolve_outcome`으로
  취합한다. `terminal_source`가 없거나 None을 돌려주면 terminal 미확정으로 두고(두 child terminal=None
  → provider가 OUTCOME_UNKNOWN) `TX_UNKNOWN`이 된다. **accepted·submit 반환만으로 RF 완료·예약 반환을
  확정하지 않는다.** COMPLETED->TX_COMPLETED / FAILED->TX_FAILED / OUTCOME_UNKNOWN/PENDING->TX_UNKNOWN.
  실장비에서는 실 RF terminal 콜백이, RF-off 시험에서는 장비 double이 terminal_source를 **명시적으로**
  주입한다. resolve_outcome 자체가 SESSION_INHIBIT을 내면 성공으로 추정하지 않고 TX_UNKNOWN.
- **target_effect**: `target_response`가 공급한 **실제 수신/application 증거**로만 판정한다. 미주입이면
  `INDETERMINATE`(실 수신 증거 없음). 송신 parent의 software `observe_scene` 모델을 실제 수신 증거로
  쓰지 않으며 `EFFECT_OBSERVED`를 합성하지 않는다(§9.1/§11.1). 실 target 효과·충돌·수신 품질은 실장비.

이 executor는 실 RF atomicity를 증명하지 않는다. software 성공 terminal은 시험 경계(장비 double)에서만
명시 공급된다.
"""

from __future__ import annotations

from ..execution.runner import TxResolution
from ..execution.verdict import ActionExecution, TargetEffect
from ..api.errors import PocError
from ..shared_medium.radiation import ParentOutcome

_EXECUTION_BY_OUTCOME = {
    ParentOutcome.COMPLETED: ActionExecution.TX_COMPLETED,
    ParentOutcome.FAILED: ActionExecution.TX_FAILED,
    ParentOutcome.OUTCOME_UNKNOWN: ActionExecution.TX_UNKNOWN,
    ParentOutcome.PENDING: ActionExecution.TX_UNKNOWN,
    ParentOutcome.REJECTED_BEFORE_TX: ActionExecution.REJECTED_BEFORE_TX,
}


class ProductionTxExecutor:
    """accepted action terminal을 provider outcome에서 유도한다(성공 합성 금지, fail-closed).

    - `terminal_source(action, result, parent) -> {child_id: terminal} | None`: 실제 child terminal
      공급자. **필수 연결이 아니면 미확정 → TX_UNKNOWN.** production은 실 RF terminal, RF-off 시험은
      장비 double이 명시 주입. terminal 값은 "COMPLETED"/"FAILED"/"UNKNOWN"/None.
    - `target_response(action, result, parent) -> (TargetEffect, reason)`: 실제 수신/application 증거.
      미주입이면 INDETERMINATE(실 수신 증거 없음).
    """

    def __init__(self, provider, *, terminal_source=None, target_response=None) -> None:
        self._provider = provider
        self._terminal_source = terminal_source
        self._target_response = target_response

    def __call__(self, action: dict, result: dict) -> TxResolution:
        request_id = result.get("request_id")
        parent = self._provider.parent_for(request_id)
        if parent is None:
            # accepted인데 parent correlation이 없으면 성공으로 추정하지 않는다.
            return TxResolution(ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                                "parent correlation missing")
        # 실제 terminal source가 없으면 두 child terminal을 None(미보고)으로 두어 provider가
        # OUTCOME_UNKNOWN을 내게 한다 — accepted/submit만으로 COMPLETED를 만들지 않는다.
        terminals = None
        if self._terminal_source is not None:
            terminals = self._terminal_source(action, result, parent)
        if not isinstance(terminals, dict):
            terminals = {child.child_id: None for child in parent.children}
        try:
            outcome = self._provider.resolve_outcome(request_id, terminals)
        except PocError as error:
            # OUTCOME_UNKNOWN·OWNERSHIP_UNCERTAIN 등 SESSION_INHIBIT: 성공 추정 금지.
            return TxResolution(ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                                error.code.value)
        execution = _EXECUTION_BY_OUTCOME.get(outcome, ActionExecution.TX_UNKNOWN)
        if execution != ActionExecution.TX_COMPLETED:
            return TxResolution(execution, TargetEffect.INDETERMINATE, outcome.value)
        effect, reason = self._target_effect(action, result, parent)
        return TxResolution(execution, effect, reason)

    def _target_effect(self, action: dict, result: dict, parent) -> tuple:
        # 실제 수신/application 증거로만 판정한다. software 송신 모델은 실 수신 증거가 아니다.
        if self._target_response is not None:
            return self._target_response(action, result, parent)
        return (TargetEffect.INDETERMINATE,
                "no real target receive evidence (real RX/application response deferred to Stage 6)")


__all__ = ("ProductionTxExecutor",)
