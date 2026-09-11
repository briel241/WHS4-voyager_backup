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
import json
import os
import tempfile

from .lease import MASS_B_SERIAL
from .verdict import ActionExecution, WorkloadResult

NOT_RECORDED = "not recorded"


class ResultWriteError(Exception):
    """evidence 확정 저장 실패(fail-closed). 확정된 evidence는 덮어쓰지 않는다(§12.3/§12.4)."""


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


def save_result(result: WorkloadResult, path: str) -> str:
    """workload 결과를 evidence 파일로 **확정 저장**한다(§12.4). terminal·quiescence 뒤 1회.

    계약(고정):

    - **확정 1회, 덮어쓰기 금지**. 대상이 이미 존재하면(regular file·symlink·디렉터리 무관)
      내용이 같든 다르든 :class:`ResultWriteError`로 거부한다. 이미 확정된 evidence는 불변이며
      read-only 재출력만 허용된다(§12.3: content·SHA-256·size·mtime 불변).
    - **symlink 목적지 거부**: `lexists`로 링크 자체를 탐지하고, 최종 확정도 `os.link`라
      링크를 따라가 다른 파일을 건드리지 않는다.
    - **atomic**: 같은 디렉터리의 임시 파일에 전량 기록(short write 검사)·fsync한 뒤
      `os.link`로 대상 이름을 원자적으로 만든다. 중간 실패 시 대상 경로에 **부분 JSON이
      남지 않는다**(임시 파일만 정리되고 대상은 생성되지 않음).
    - RF 활성 구간에서 호출하지 않는다(§12.1: per-event write 금지).

    반환은 확정한 절대 경로.
    """

    payload = (json.dumps(result.as_json_value(), ensure_ascii=False,
                          sort_keys=True, indent=2) + "\n").encode("utf-8")
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    if os.path.lexists(target):
        raise ResultWriteError(
            f"이미 확정된 evidence를 덮어쓸 수 없습니다: {os.path.basename(target)}")

    fd, tmp = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=directory)
    try:
        try:
            view = memoryview(payload)
            total = 0
            while total < len(view):
                written = os.write(fd, view[total:])
                if written <= 0:
                    raise ResultWriteError("evidence write가 진행되지 않음(short write)")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)
        # 원자적 확정: 대상이 이미 있으면 FileExistsError(덮어쓰지 않음).
        os.link(tmp, target)
    except FileExistsError:
        raise ResultWriteError(
            f"이미 확정된 evidence를 덮어쓸 수 없습니다: {os.path.basename(target)}")
    except OSError as exc:
        raise ResultWriteError(f"evidence 확정 실패({exc.__class__.__name__})")
    finally:
        try:
            os.unlink(tmp)  # 성공·실패 모두 임시 파일은 남기지 않는다
        except OSError:
            pass
    return target


def load_result(path: str) -> WorkloadResult:
    """저장 evidence 파일을 **읽기 전용**으로 불러온다(§12.3). 파일을 수정하지 않는다."""

    with open(path, encoding="utf-8") as fh:
        return WorkloadResult.from_json_value(json.load(fh))


def render_results_file(path: str) -> str:
    """`vdeter results <run-id>` 경계: 저장 evidence만 읽어 같은 renderer로 재생한다(§12.3).

    evidence의 content·SHA-256·size·mtime을 바꾸지 않는다(열기 mode가 read-only이며 쓰기
    경로가 없다). run 시점과 동일한 model(:class:`WorkloadResult`)·renderer를 쓴다.
    """

    return render_detailed(load_result(path))


__all__ = (
    "NOT_RECORDED",
    "render_rf_console",
    "render_detailed",
    "render_results",
    "ResultWriteError",
    "save_result",
    "load_result",
    "render_results_file",
)
