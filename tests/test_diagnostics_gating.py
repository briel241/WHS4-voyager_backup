"""진단 스위치 게이트 계약 — 무엇이 `STAGE7_DIAG` 로 달라지고 무엇이 달라지지 않는가.

3-1(진단·계측 정리)로 생긴 **제한적 동작 변경**을 잠근다.

증명하는 것
  분류    : coordinator trace 에서 진단 전용은 선택적 이벤트 3종뿐이고, 판정 원인·요약
            이벤트는 OFF 에서도 남는다 (`session-coordinator-trace.jsonl` 을 통째로
            켜거나 끄지 않는다)
  비게이트: 단계 시각 적재·guard 진행 통지·보고 주기 갱신은 게이트 **밖**에 그대로 있다
  전달    : 부모에 적용된 진단 설정이 workload controller argv 로 이어지고, 자식 CLI 가
            그 옵션을 실제로 받는다 (실 subprocess 로 확인)
  수집    : launcher 의 선택적 적재(`_cost_add`·`_win_trace`·`_submit_utc`·`_c_stamp`)가
            소비자와 같은 조건(`_DIAG`) 아래에서만 돈다
  기록    : run.json·summary.json 의 `diagnostics` 는 **요청값이 아니라 적용값**이다

부정 대조도 함께 둔다 — 검사가 비어 있지 않음을 같은 파일에서 보인다. 부정 대조는
production 파일을 건드리지 않고 **메모리 안 사본**만 변형한다.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output  # noqa: E402

ensure_utf8_output()

COORDINATOR = Path(_REPO) / "VDETER" / "apps" / "coordinator_main.py"
LAUNCHER = Path(_REPO) / "VDETER" / "apps" / "endpoint_launcher.py"
RUN_CMD = Path(_REPO) / "apps" / "standalone" / "run_cmd.py"
WORKLOAD_CONTROLLER = Path(_REPO) / "apps" / "mass_roc_workload_controller.py"

#: 진단 전용으로 내린 **선택적** coordinator 이벤트. 소비자가 없는 주기·표식 자료다.
OPTIONAL_EVENTS = {"phase_mark", "intent_wait", "tbb_wait"}

#: OFF 에서도 반드시 남아야 하는 이벤트. 판정 원인이거나 이미 소비자가 있다.
#: `rf_time_pre/post_summary` 는 진행바(`apps/standalone/progress.py`)가 읽는다.
ESSENTIAL_EVENTS = {
    "session_phase_begin", "session_ready", "session_closed",
    "admission_open", "intents_complete", "intent_failed",
    "service_start_committed", "tbb_complete", "tbb_failed",
    "service_published", "rf_time_pre_summary", "rf_time_post_summary",
    "workload_health_gate", "workload_child_spawn", "workload_child_abort",
    "workload_child_reaped", "workload_result", "session_exception",
    "launcher_cleanup", "lifecycle_proxy_spawn", "lifecycle_proxy_abort",
    "lifecycle_proxy_reaped", "lifecycle_proxy_reap_timeout",
}

#: launcher 의 선택적 적재 구조. 소비자가 전부 `_DIAG` 아래에 있다.
OPTIONAL_STORES = ("_win_trace", "_submit_utc", "_c_stamp")


# ------------------------------------------------------------------ AST 보조

def _parents(tree):
    table = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            table[child] = node
    return table


def _guard_tests(table, node):
    """`node` 를 감싸는 `if` 조건들을 안쪽부터 돌려준다 (else 절은 제외)."""
    out = []
    cur = node
    while cur in table:
        parent = table[cur]
        if isinstance(parent, ast.If) and any(cur is s for s in parent.body):
            out.append(ast.unparse(parent.test))
        cur = parent
    return out


def _scopes(tree):
    return [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))]


def _enclosing(scopes, line):
    return {name for start, end, name in scopes if start <= line <= end}


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _emitted_events(source):
    """`emit("<name>", …)` → {이벤트: {diagnostic 여부, …}}."""
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "emit"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        diagnostic = any(
            kw.arg == "detail_level"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "diagnostic"
            for kw in node.keywords)
        found.setdefault(node.args[0].value, set()).add(diagnostic)
    return found


def _diagnostic_only(source):
    return {name for name, flags in _emitted_events(source).items()
            if flags == {True}}


def _ungated_collection(source):
    """`_DIAG` 아래가 아닌 선택적 적재 지점 → [(줄, 대상)].

    `_cost_wrap` 안의 래퍼는 제외한다 — 그 래퍼 자체가 `STAGE7_DIAG=1` 일 때만
    설치되며, 이는 3-1 이전부터 그랬다.
    """
    tree = ast.parse(source)
    table = _parents(tree)
    scopes = _scopes(tree)
    bad = []
    for node in ast.walk(tree):
        target = None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_cost_add"):
            target = "_cost_add"
        elif isinstance(node, ast.Assign):
            for goal in node.targets:
                if (isinstance(goal, ast.Subscript)
                        and _base_name(goal.value) in OPTIONAL_STORES):
                    target = _base_name(goal.value)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("clear", "pop")
              and _base_name(node.func.value) in OPTIONAL_STORES):
            target = _base_name(node.func.value)
        if target is None:
            continue
        if "_cost_wrap" in _enclosing(scopes, node.lineno):
            continue
        if not any("_DIAG" in test for test in _guard_tests(table, node)):
            bad.append((node.lineno, target))
    return sorted(set(bad))


def _drop_one_diag_gate(source, marker):
    """`marker` 가 든 `if _DIAG:` 블록 하나를 없앤 **사본**을 만든다 (부정 대조용).

    production 파일은 건드리지 않는다. 대상 게이트가 사라지면 그 자리에서
    실패하므로, 코드가 바뀌어 marker 를 못 찾으면 시험이 먼저 깨진다.
    """
    lines = source.splitlines(True)
    hits = [i for i, line in enumerate(lines) if marker in line]
    assert hits, f"marker 를 찾지 못했다: {marker!r}"
    index = hits[0]
    gates = [i for i in range(index) if lines[i].strip() == "if _DIAG:"]
    assert gates, f"{marker!r} 위에 `if _DIAG:` 가 없다"
    gate = gates[-1]
    indent = len(lines[gate]) - len(lines[gate].lstrip())
    out = list(lines[:gate])
    cursor = gate + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line[4:] if line.startswith(" " * (indent + 4)) else line)
        cursor += 1
    out.extend(lines[cursor:])
    return "".join(out)


# ------------------------------------------------- A. TraceWriter 게이트 동작

def test_trace_writer_keeps_essential_events_when_diagnostics_is_off():
    """한 파일 안에서 필수는 남고 선택만 빠진다 — 파일을 통째로 끄지 않는다."""
    from apps.service_trace import TraceWriter

    with tempfile.TemporaryDirectory() as tmp:
        # `close()` 가 남기는 `trace_closed` 는 어느 쪽에도 있다 (계측기 자체 기록).
        for diagnostics, expected in (
                (False, {"session_closed", "trace_closed"}),
                (True, {"session_closed", "trace_closed", "phase_mark"})):
            path = os.path.join(tmp, f"trace-{diagnostics}.jsonl")
            writer = TraceWriter(path, session_id="run-1",
                                 diagnostics=diagnostics)
            writer.emit("session_closed", status="PASS")
            writer.emit("phase_mark", detail_level="diagnostic", phase="raw_phy")
            writer.close()
            events = {json.loads(line)["event"]
                      for line in Path(path).read_text(
                          encoding="utf-8").splitlines() if line.strip()}
            assert events == expected, (diagnostics, events)


# ------------------------------------------------- B. coordinator 이벤트 분류

def test_only_the_three_optional_coordinator_events_are_diagnostic_only():
    source = COORDINATOR.read_text(encoding="utf-8")
    assert _diagnostic_only(source) == OPTIONAL_EVENTS


def test_essential_coordinator_events_survive_diagnostics_off():
    source = COORDINATOR.read_text(encoding="utf-8")
    emitted = _emitted_events(source)
    missing = sorted(ESSENTIAL_EVENTS - set(emitted))
    assert not missing, f"필수 이벤트가 사라졌다: {missing}"
    downgraded = sorted(name for name in ESSENTIAL_EVENTS
                        if emitted[name] != {False})
    assert not downgraded, f"필수 이벤트가 진단 전용이 됐다: {downgraded}"


def test_event_classification_catches_an_essential_event_downgraded():
    """부정 대조: 필수 이벤트를 진단 전용으로 내리면 위 검사가 잡아야 한다."""
    source = COORDINATOR.read_text(encoding="utf-8")
    victim = 'emit("rf_time_pre_summary",'
    assert victim in source
    mutated = source.replace(
        victim, 'emit("rf_time_pre_summary", detail_level="diagnostic",', 1)
    ast.parse(mutated)  # 변형본이 여전히 유효한 Python 이어야 대조가 성립한다
    assert "rf_time_pre_summary" in _diagnostic_only(mutated)


# ------------------------------------- C. 상태·통지·주기 갱신은 게이트 밖

def test_phase_mark_keeps_state_and_progress_outside_the_gate():
    """`mark()` 는 통지·시각 적재를 그대로 하고 이벤트만 선택이 된다."""
    tree = ast.parse(COORDINATOR.read_text(encoding="utf-8"))
    sessions = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name == "run_mass_roc_session"]
    assert len(sessions) == 1
    marks = [n for n in ast.walk(sessions[0])
             if isinstance(n, ast.FunctionDef) and n.name == "mark"]
    assert len(marks) == 1, [n.lineno for n in marks]
    body = [ast.unparse(stmt) for stmt in marks[0].body]
    assert not any(isinstance(stmt, ast.If) for stmt in marks[0].body), body
    assert body[0] == "guard.check()"
    assert body[1] == "guard.progress(name)"
    assert body[2].startswith("marks[name] = round(")
    assert body[3].startswith("emit('phase_mark'")


@pytest.mark.parametrize("variable, period", [("next_report", "5.0"),
                                              ("next_tbb_report", "10.0")])
def test_wait_report_period_is_updated_outside_the_gate(variable, period):
    """주기 보고 이벤트만 선택이 되고, 다음 보고 시각 갱신은 그대로다."""
    tree = ast.parse(COORDINATOR.read_text(encoding="utf-8"))
    blocks = [n for n in ast.walk(tree)
              if isinstance(n, ast.If)
              and ast.unparse(n.test) == f"waited >= {variable}"]
    assert len(blocks) == 1, [n.lineno for n in blocks]
    body = [ast.unparse(stmt) for stmt in blocks[0].body]
    assert len(body) == 2, body
    assert body[0].startswith("emit(") and "detail_level='diagnostic'" in body[0]
    assert body[1] == f"{variable} = waited + {period}"


# --------------------------------------------- D. 자식으로의 설정 전달

def _spawn_argv(monkeypatch, env_value, spawner=None):
    """실제 spawn 함수가 만드는 argv 를 잡는다 (프로세스는 띄우지 않는다)."""
    import apps.coordinator_main as coordinator_main

    seen = {}

    class _FakePopen:
        def __init__(self, command, **kwargs):
            seen["command"] = list(command)
            for handle in kwargs.values():
                if hasattr(handle, "close"):
                    handle.close()

    monkeypatch.setattr(coordinator_main.subprocess, "Popen", _FakePopen)
    if env_value is None:
        monkeypatch.delenv("STAGE7_DIAG", raising=False)
    else:
        monkeypatch.setenv("STAGE7_DIAG", env_value)
    call = spawner or coordinator_main._spawn_mass_roc_controller
    with tempfile.TemporaryDirectory() as run_root:
        call("run-1", run_root, os.path.join(run_root, "cfg"), "a" * 64,
             "SC-VDE-005", "normal")
    return seen["command"]


def test_workload_controller_gets_the_applied_diagnostics_option(monkeypatch):
    assert "--diagnostics" in _spawn_argv(monkeypatch, "1")
    assert "--diagnostics" not in _spawn_argv(monkeypatch, "0")
    assert "--diagnostics" not in _spawn_argv(monkeypatch, None)


def test_forwarding_check_rejects_a_spawner_that_omits_the_option(monkeypatch):
    """부정 대조: 전달을 빠뜨린 구현이면 같은 검사가 실패해야 한다."""
    import apps.coordinator_main as coordinator_main

    def omitting_spawn(run_id, run_root, configs_dir, test_plan_sha,
                       scenario_id, certificate_case):
        logs = os.path.join(run_root, "logs")
        os.makedirs(logs, exist_ok=True)
        handle = open(os.path.join(logs, "x.log"), "ab")  # noqa: SIM115
        return coordinator_main.subprocess.Popen(
            [sys.executable, "-c", "pass", "--run-id", run_id],
            stdout=handle, stderr=subprocess.STDOUT)

    assert "--diagnostics" not in _spawn_argv(monkeypatch, "1", omitting_spawn)


def test_workload_controller_cli_really_accepts_the_option():
    """실 subprocess: 자식이 `--diagnostics` 를 실제로 받는다 (장치 접근 없음)."""
    completed = subprocess.run(
        [sys.executable, str(WORKLOAD_CONTROLLER), "--help"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", cwd=_REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONIOENCODING": "utf-8"})
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "--diagnostics" in completed.stdout


# ------------------------------------------- E. launcher 선택적 수집 게이트

def test_launcher_optional_collection_runs_only_under_diag():
    assert _ungated_collection(LAUNCHER.read_text(encoding="utf-8")) == []


def test_launcher_gate_check_catches_a_removed_gate():
    """부정 대조: 게이트 하나를 지운 사본이면 같은 검사가 잡아야 한다."""
    source = LAUNCHER.read_text(encoding="utf-8")
    mutated = _drop_one_diag_gate(source, "self._submit_utc[job_id] = _utc_ns()")
    ast.parse(mutated)
    found = _ungated_collection(mutated)
    assert any(target == "_submit_utc" for _, target in found), found


def test_launcher_dispatch_and_backpressure_stay_unconditional():
    """수집만 게이트했고 dispatch·완료·역압 카운터는 건드리지 않았다."""
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    table = _parents(tree)
    guarded = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(goal, ast.Subscript)
                and _base_name(goal.value) == "_inflight"
                for goal in node.targets):
            guarded.append((node.lineno,
                            _guard_tests(table, node)))
        if isinstance(node, ast.AugAssign) and isinstance(
                node.target, ast.Subscript) and _base_name(
                    node.target.value) == "_inflight":
            guarded.append((node.lineno, _guard_tests(table, node)))
    assert guarded, "in-flight 카운터 갱신 지점을 찾지 못했다"
    for line, tests in guarded:
        assert not any("_DIAG" in test for test in tests), (line, tests)


# ------------------------------------------- F. 적용 상태의 evidence 기록

@pytest.mark.parametrize("option, ambient, applied", [
    (False, None, False),   # 생략 = OFF
    (False, "1", False),    # 상위 환경 ON 은 상속되지 않는다 (정식 run 오염 방지)
    (False, "0", False),
    (True, None, True),
    (True, "0", True),      # 옵션이 단일 출처 — 상위 환경이 이기지 않는다
    (True, "1", True),
])
def test_child_diagnostics_comes_from_the_option_alone(monkeypatch, option,
                                                       ambient, applied):
    """옵션 × 상위 환경 조합에서 현재 우선순위가 보존된다(회귀 방지).

    기록되는 값은 요청값이 아니라 **이 표의 적용값**이다 — 자식 env 를 만드는 같은
    함수에서 읽기 때문이다.
    """
    from apps.standalone.local_session import build_child_env

    if ambient is None:
        monkeypatch.delenv("STAGE7_DIAG", raising=False)
    else:
        monkeypatch.setenv("STAGE7_DIAG", ambient)
    monkeypatch.setenv("STAGE7_SVCDBG_PROBE", "1")   # 다른 ambient 디버그 스위치
    env = build_child_env(diagnostics=option)
    assert (env.get("STAGE7_DIAG") == "1") is applied
    # 옵션으로 넣은 것 말고 어떤 STAGE7_* 도 자식에게 새지 않는다.
    assert [key for key in env if key.startswith("STAGE7_")] == (
        ["STAGE7_DIAG"] if applied else [])


def test_qualify_ladder_never_turns_diagnostics_on():
    """자격 사다리는 `diagnostics` 를 넘기지 않는다 — 재현성 때문이며 현행 유지다."""
    source = (Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    runner = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "default_phase_runner")
    body = ast.unparse(runner)
    assert "build_child_env(" in body
    assert "diagnostics" not in body, body


def test_run_records_read_the_applied_value_from_the_single_source():
    """`run.json`·`summary.json` 의 `diagnostics` 는 `build_child_env` 에서 온다."""
    tree = ast.parse(RUN_CMD.read_text(encoding="utf-8"))
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "diagnostics"):
                values.append(ast.unparse(value))
    assert len(values) == 3, values  # run.json 1 + summary.json 2
    for expression in values:
        assert "build_child_env(diagnostics=diagnostics)" in expression
        assert "STAGE7_DIAG" in expression


# ------------------------------------- G. 실 세션 ON/OFF 비교 (Linux 전용)

def _real_session(diagnostics):
    """실 `LocalCoordinatorSession` + 실 coordinator 프로세스(`--rf-off-software`).

    fake 세션이 아니다. 하드웨어 경계만 명시적 RF-off double 이며 장치를 열지 않는다.
    """
    import test_standalone_run_cli as harness
    from apps.standalone.local_session import LocalCoordinatorSession

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = harness.fx.prepare_environment(tmp)
        code, lines = harness.run(paths, LocalCoordinatorSession,
                                  scenario="SC-VDE-005", rf_off_software=True,
                                  diagnostics=diagnostics)
        summaries = sorted(Path(paths.runs_dir).glob("*/summary.json"))
        assert summaries, chr(10).join(lines)
        summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
        traces = sorted(summaries[-1].parent.rglob(
            "session-coordinator-trace.jsonl"))
        events = []
        for trace in traces:
            events += [json.loads(row) for row
                       in trace.read_text(encoding="utf-8").splitlines()
                       if row.strip()]
        return {"code": code, "summary": summary, "lines": lines,
                "trace_files": len(traces),
                "events": {record["event"] for record in events}}


@pytest.mark.skipif(sys.platform != "linux",
                    reason="실 coordinator 프로세스·SOCK_SEQPACKET 전용")
def test_real_session_on_and_off_agree_on_verdict_and_essential_records():
    """같은 입력을 진단 OFF/ON 으로 실행해 **판정과 필수 기록이 같은지** 본다.

    허용되는 차이는 선택적 이벤트의 유무뿐이다. trace 파일 자체는 양쪽에서 생긴다.
    시간값은 비교하지 않는다(실행마다 다르다).
    """
    off = _real_session(False)
    on = _real_session(True)

    assert off["code"] == on["code"], (off["code"], on["code"])
    assert off["summary"]["status"] == on["summary"]["status"]
    assert ([w["status"] for w in off["summary"]["workloads"]]
            == [w["status"] for w in on["summary"]["workloads"]])
    # 적용 상태가 산출물에 남는다 (요청값이 아니라 적용값).
    assert off["summary"]["diagnostics"] is False
    assert on["summary"]["diagnostics"] is True

    # 이 경로는 coordinator trace 를 만들지 않는다 — `--rf-off-software` 는 자격
    # 결속된 `coordinator_main.main()` 을 **의도적으로** 거치지 않기 때문이다
    # (`local_session.build_argv`). 그 사실 자체를 고정해 둔다: 나중에 이 경로가
    # coordinator trace 를 쓰기 시작하면 여기서 드러난다.
    assert off["trace_files"] == on["trace_files"] == 0, (off["trace_files"],
                                                          on["trace_files"])
    # coordinator trace 의 ON/OFF 차이는 production 함수 `_open_session_trace` 로
    # 따로 확인한다(아래) — 실 coordinator 세션은 장치를 요구하므로 여기서 하지 않는다.


@pytest.mark.parametrize("diagnostics", [False, True])
def test_coordinator_session_trace_follows_the_applied_setting(tmp_path,
                                                               monkeypatch,
                                                               diagnostics):
    """production 함수 `_open_session_trace` 가 실행에 적용된 env 를 그대로 따른다.

    자격 결속 대상 함수라 우회 구현을 만들지 않고 **그대로 호출**한다. 파일은 양쪽에서
    생기고, 필수 이벤트는 양쪽에 남으며, 선택 이벤트만 ON 에서 기록된다.
    """
    import apps.coordinator_main as coordinator_main

    monkeypatch.setenv("STAGE7_DIAG", "1" if diagnostics else "0")
    run_root = tmp_path / ("run-on" if diagnostics else "run-off")
    run_root.mkdir()
    trace = coordinator_main._open_session_trace(str(run_root), "run-1")
    trace.emit("session_closed", status="PASS")
    for event in sorted(OPTIONAL_EVENTS):
        trace.emit(event, detail_level="diagnostic")
    trace.close()

    path = run_root / "session-coordinator-trace.jsonl"
    assert path.exists(), sorted(run_root.iterdir())
    events = {json.loads(row)["event"] for row
              in path.read_text(encoding="utf-8").splitlines() if row.strip()}
    assert "session_closed" in events, sorted(events)
    assert (OPTIONAL_EVENTS <= events) is diagnostics, sorted(events)
    assert not (OPTIONAL_EVENTS & events) or diagnostics
