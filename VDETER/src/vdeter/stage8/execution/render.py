"""저부하 console 투영과 종료 후 bounded 상세 보고(실행 명세 §12, `S8-POC-REQ-17/18`).

- `render_rf_console`: RF 활성 구간의 저부하 투영 — 상태 전이와 action terminal만 한 줄로.
  packet마다 block을 만들거나 flush하지 않는다(§12.1). runner는 이를 per-event로 호출하지
  않는다(호출은 terminal 뒤). 이 함수는 문자열만 만들고 write/flush를 하지 않는다.
- `render_detailed`: workload terminal·quiescence 뒤 같은 model로 세로형 상세 보고(§12.2).
  없는 값은 추정하지 않고 `not recorded`로 표시한다. 항목 수는 attack 수(≤32)에 비례해 bounded.
- `render_results`: `vdeter results`가 저장 evidence(WorkloadResult 직렬화)만 읽어 run 시점과
  같은 renderer로 재생한다(§12.3). 입력 dict를 수정하지 않는다(read-only).

renderer 오류는 이미 확정된 verdict를 바꾸지 않는다(§12.2). 이 module은 순수 함수다.
"""

from __future__ import annotations

import copy

from .lease import MASS_B_SERIAL
from .verdict import ActionExecution, WorkloadResult

NOT_RECORDED = "not recorded"


def render_rf_console(result: WorkloadResult) -> list:
    """RF 활성 중 저부하 투영 줄 목록(§12.1, §16.2). 상태 전이·terminal만."""

    lines = [
        f"POC VALIDATED {result.poc_id}@{result.poc_version}",
        f"POC ARMED attacker={result.attacker_endpoint} scenario={result.scenario_id}",
    ]
    for i, attack in enumerate(result.attacks, start=1):
        tag = f"{i:04d}"
        lines.append(f"POC attack={tag} SCHEDULED target={attack.target}")
        lines.append(f"POC attack={tag} {attack.action_execution.value}")
    lines.append(f"POC TERMINAL infrastructure={result.poc_infrastructure.value}")
    return lines


def _attacker_line(result: WorkloadResult) -> str:
    if result.attacker_endpoint == "MASS-B":
        return f"{result.attacker_endpoint} ({MASS_B_SERIAL})"
    return result.attacker_endpoint or NOT_RECORDED


def render_detailed(result: WorkloadResult) -> str:
    """종료 후 세로형 상세 보고(§12.2). bounded, 없는 값은 not recorded."""

    lines = [
        "[POC RUN]",
        f"  run: {result.run_id}",
        f"  workload: {result.workload_id}",
        f"  poc: {result.poc_id}@{result.poc_version}",
        f"  attacker: {_attacker_line(result)}",
        f"  scenario: {result.scenario_id}",
        f"  script_sha256: {result.script_sha256 or NOT_RECORDED}",
        f"  manifest_sha256: {result.manifest_sha256 or NOT_RECORDED}",
        f"  binding_digest: {result.binding_digest or NOT_RECORDED}",
        f"  interpreter: {result.interpreter or NOT_RECORDED}",
        f"  infrastructure: {result.poc_infrastructure.value}",
    ]
    if result.infrastructure_error_code:
        lines.append(f"  infrastructure_error: {result.infrastructure_error_code}")

    for i, attack in enumerate(result.attacks, start=1):
        lines.append("")
        lines.append(f"[ATTACK {i:04d}]")
        lines.append(f"  request_id: {attack.request_id}")
        lines.append(f"  trigger: {attack.trigger}")
        lines.append(f"  source_observation: {attack.source_observation_id or NOT_RECORDED}")
        lines.append(f"  action: {attack.action_kind}")
        lines.append(f"  target: {attack.target}")
        lines.append(f"  physical_channel: {attack.physical_channel or NOT_RECORDED}")
        lines.append(f"  schedule: {_fmt_schedule(attack.schedule)}")
        lines.append(f"  tx_result: {attack.action_execution.value}")
        lines.append(f"  target_effect: {attack.target_effect.value}")
        lines.append(f"  reason: {attack.reason or NOT_RECORDED}")

    lines.append("")
    lines.append("[FINAL]")
    lines.append(f"  scenario_verdict: {result.scenario_verdict.value}")
    lines.append(f"  poc_infrastructure: {result.poc_infrastructure.value}")
    lines.append(f"  attacks_requested: {result.attacks_requested}")
    lines.append(f"  attacks_completed: {result.attacks_completed}")
    lines.append(f"  effects_observed: {result.effects_observed}")
    return "\n".join(lines)


def _fmt_schedule(schedule: dict) -> str:
    if not schedule:
        return NOT_RECORDED
    return ", ".join(f"{k}={schedule[k]}" for k in sorted(schedule))


def render_results(evidence_value: dict) -> str:
    """저장 evidence dict를 read-only로 재렌더한다(§12.3). 입력을 수정하지 않는다."""

    safe = copy.deepcopy(evidence_value)
    result = WorkloadResult.from_json_value(safe)
    return render_detailed(result)


__all__ = (
    "NOT_RECORDED",
    "render_rf_console",
    "render_detailed",
    "render_results",
)
