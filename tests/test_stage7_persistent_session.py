"""Phase G — bounded persistent session·workload 격리·계측 계약 시험 (RF 없음).

증명하는 것 (§14)
  session lifecycle : B210 acquire·endpoint START·time-lock·admission·STOP이
                      session당 각 1회, workload 수와 무관
  application 격리   : workload마다 새 controller/agent/TrustStore, event
                      baseline 분리, step/scenario ID namespace 분리
  quiescence        : 실패 시 다음 workload 거부·session fail-closed
  cleanup           : 예외·EOF·bring-up 실패에서도 close()가 돈다
  계측              : 계측 유무가 판정을 바꾸지 않음, ID 결속, 비밀 미기록
  qualification 불변: service/session 경로가 CFO/gain/raw-PHY를 발행하지 않음
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import apps.scenario_runner as runner
import apps.stage7_profile as stage7_profile
import apps.stage7_run as stage7_run
import apps.stage7_session as stage7_session
import apps.stage7_trace as stage7_trace

COORDINATOR = os.path.join(_REPO, "VDETER", "apps", "stage7_coordinator_main.py")
CONTROLLER = os.path.join(_REPO, "apps", "stage7_mass_roc_controller.py")
PHASE_G = os.path.join(_REPO, "VDETER", "results", "phase-g")


def _profile_path() -> str:
    candidates = sorted(os.path.join(PHASE_G, n) for n in os.listdir(PHASE_G)
                        if n.startswith("phase-g-")) if os.path.isdir(PHASE_G) else []
    if not candidates:
        raise AssertionError("phase-g reference 디렉터리가 없다")
    return os.path.join(candidates[-1], "stage7-profile.json")


# ---------------------------------------------------------------- 계측 모듈

def test_trace_binds_ids_and_buffers():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.jsonl")
        trace = stage7_trace.TraceWriter(path, session_id="s1", workload_id="w1")
        trace.state("NEW")
        trace.emit("submit", transfer_id="abc")
        assert not os.path.exists(path), "flush 전에 파일을 쓰면 안 된다"
        trace.flush()
        records = stage7_trace.read_trace(path)
        assert [r["event"] for r in records] == ["state", "submit"]
        for record in records:
            assert record["session_id"] == "s1" and record["workload_id"] == "w1"
            assert "t_monotonic_s" in record and "t_utc" in record
            assert record["seq"] >= 1


def test_trace_scrubs_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.jsonl")
        trace = stage7_trace.TraceWriter(path, session_id="s1")
        trace.emit("x", private_key_sha256="a" * 64, token="t",
                   pem="-----BEGIN EC PRIVATE KEY-----\nabc\n",
                   nested={"password": "p", "ok": 1})
        trace.flush()
        text = open(path, encoding="utf-8").read()
        assert "BEGIN EC PRIVATE KEY" not in text
        assert "a" * 64 not in text
        assert "<redacted>" in text
        record = stage7_trace.read_trace(path)[0]
        assert record["nested"]["ok"] == 1


def test_trace_diagnostic_level_gated():
    with tempfile.TemporaryDirectory() as tmp:
        quiet = stage7_trace.TraceWriter(os.path.join(tmp, "q.jsonl"),
                                         session_id="s", diagnostics=False)
        loud = stage7_trace.TraceWriter(os.path.join(tmp, "l.jsonl"),
                                        session_id="s", diagnostics=True)
        for trace in (quiet, loud):
            trace.emit("normal")
            trace.emit("deep", detail_level="diagnostic")
            trace.flush()
        assert len(stage7_trace.read_trace(os.path.join(tmp, "q.jsonl"))) == 1
        assert len(stage7_trace.read_trace(os.path.join(tmp, "l.jsonl"))) == 2


def test_null_trace_is_inert():
    trace = stage7_trace.NullTrace()
    trace.state("NEW")
    trace.emit("x", a=1)
    trace.flush()
    trace.close()


# ------------------------------------------------- session lifecycle (mock SSH)

class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _FakePi:
    """원격 session 디렉터리를 흉내내는 최소 상태기계."""

    def __init__(self, *, ready=True, quiescence_pass=True, workload_status="PASS"):
        self.files = {}
        self.requests = {}
        self.results = {}
        self.ready = ready
        self.quiescence_pass = quiescence_pass
        self.workload_status = workload_status
        self.shutdown = False
        self.closed_written = False
        self.commands = []
        self.bringup_count = 0
        self.stop_count = 0

    def __call__(self, command, **kwargs):
        script = command[-1] if command and command[0] == "ssh" else " ".join(command)
        self.commands.append(script)
        if command[0] == "scp":
            return _FakeCompleted("")
        if "--phase mass-roc-session" in script:
            self.bringup_count += 1
            return _FakeCompleted("spawned")
        if script.startswith("cat ") and "ready.json" in script:
            if not self.ready:
                return _FakeCompleted("")
            return _FakeCompleted(json.dumps({
                "artifact_type": "STAGE7_SERVICE_SESSION_READY_V1",
                "rf_time_paths": ["C1", "C2", "C3", "C4", "C5", "C6"],
                "phase_elapsed_s": {"rf_time_post": 1240.0},
                "health_baseline": {"faults": 0},
            }))
        if "requests/" in script and script.startswith("mkdir"):
            payload = script.split("\n")[2]
            request = json.loads(payload) if payload.startswith("{") else {}
            # heredoc 본문에서 요청 JSON을 뽑는다.
            for line in script.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    request = json.loads(line)
            workload_id = request["workload_id"]
            self.requests[workload_id] = request
            self.results[workload_id] = {
                "artifact_type": "STAGE7_SERVICE_WORKLOAD_RESULT_V1",
                "workload_id": workload_id,
                "scenario_id": request["scenario_id"],
                "certificate_case": request["certificate_case"],
                "status": self.workload_status,
                "child_exit_code": 0 if self.workload_status == "PASS" else 1,
                "quiescence": {"pass": self.quiescence_pass,
                               "non_terminal_transfers": 0},
                "session_health_before": {"pass": True},
            }
            return _FakeCompleted("")
        if script.startswith("cat ") and "results/" in script:
            name = script.split("results/")[1].split()[0].replace(".json", "")
            result = self.results.get(name)
            return _FakeCompleted(json.dumps(result) if result else "")
        if "touch" in script and "shutdown" in script:
            self.shutdown = True
            self.stop_count += 1
            return _FakeCompleted("")
        if "done.rc" in script and script.startswith("test -f"):
            return _FakeCompleted("yes" if self.shutdown else "no")
        if script.startswith("cat ") and "done.rc" in script:
            return _FakeCompleted("0" if self.shutdown else "")
        if script.startswith("cat ") and "closed.json" in script:
            if not self.shutdown:
                return _FakeCompleted("")
            self.closed_written = True
            return _FakeCompleted(json.dumps({
                "artifact_type": "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1",
                "status": "PASS",
                "cleanup": {"pass": True, "workers_after_stop": 0,
                            "rediscovery": {"34D03DD": True, "3515425": True,
                                            "34D03E8": True}},
                "hardware_lifecycle": {"b210_acquire": 1, "endpoint_start": 1,
                                       "endpoint_stop": 1},
            }))
        return _FakeCompleted("")


def _profile_with_stubs(monkey_dir):
    profile = stage7_profile.load_profile(_profile_path())
    profile["evidence_root"] = os.path.relpath(monkey_dir, _REPO)
    return profile


def _session(tmp, pi, **kwargs):
    profile = _profile_with_stubs(tmp)
    # preflight/config 준비는 별도 시험이 담당한다 — 여기서는 lifecycle만 본다.
    import apps.stage7_service_layer as layer
    original_verify = layer.verify_remote_environment
    original_prepare = layer.prepare_service_config
    layer.verify_remote_environment = lambda p, runner=None: {
        "critical_manifest": {"pass": True}, "usb_topology": {"pass": True},
        "trust": {"pass": True}, "throttled": {"value": "throttled=0x0"},
        "function_bound": {}, "competing_processes": {"observed": []}}
    layer.prepare_service_config = lambda p, run_id, logs, runner=None: {
        "local_dir": tmp,
        "metadata": {"run_id": run_id, "git_dirty": False, "git_commit": "abc"}}
    try:
        return stage7_session.open_service_session(
            profile, runner=pi, evidence_root=os.path.relpath(tmp, _REPO),
            **kwargs)
    finally:
        layer.verify_remote_environment = original_verify
        layer.prepare_service_config = original_prepare


def test_hardware_lifecycle_runs_once_for_many_workloads():
    with tempfile.TemporaryDirectory(dir=_REPO) as tmp:
        pi = _FakePi()
        session = _session(tmp, pi)
        assert session.status == "READY"
        assert pi.bringup_count == 1
        for scenario, case in (("SC-VDE-005", "normal"), ("SC-VDE-006", "normal"),
                               ("SC-VDE-001", "normal"), ("SC-VDE-001", "forged")):
            result = session.run_workload(scenario, case)
            assert result["status"] == "PASS"
        # workload 4회에도 bring-up은 1회, STOP은 close에서 1회.
        assert pi.bringup_count == 1
        assert pi.stop_count == 0
        session.close()
        assert pi.stop_count == 1
        summary = session.summary()
        assert len(summary["workload_ids"]) == 4
        assert len(set(summary["workload_ids"])) == 4, "workload ID가 겹친다"


def test_workload_ids_bound_to_session_and_unique():
    with tempfile.TemporaryDirectory(dir=_REPO) as tmp:
        pi = _FakePi()
        session = _session(tmp, pi)
        first = session.run_workload("SC-VDE-005", "normal")
        second = session.run_workload("SC-VDE-005", "normal")
        assert first["workload_id"] != second["workload_id"]
        for result in (first, second):
            assert result["workload_id"].startswith(session.session_id)
        session.close()


def test_quiescence_failure_blocks_next_workload_and_closes():
    with tempfile.TemporaryDirectory(dir=_REPO) as tmp:
        pi = _FakePi(quiescence_pass=False)
        session = _session(tmp, pi)
        result = session.run_workload("SC-VDE-005", "normal")
        assert not result["quiescence"]["pass"]
        assert session.status == "QUIESCENCE_FAIL"
        try:
            session.run_workload("SC-VDE-006", "normal")
            raise AssertionError("quiescence 실패 후 workload가 실행됐다")
        except stage7_run.ServiceSessionError as exc:
            assert "READY" in str(exc)
        session.close()
        assert pi.stop_count == 1, "cleanup이 수행되지 않았다"


def test_session_trace_records_state_transitions():
    with tempfile.TemporaryDirectory(dir=_REPO) as tmp:
        pi = _FakePi()
        session = _session(tmp, pi)
        session.run_workload("SC-VDE-005", "normal")
        session.close()
        records = stage7_trace.read_trace(session.trace.path)
        states = [r.get("state") for r in records if r["event"] == "state"]
        for expected in ("NEW", "VALIDATING", "STARTING", "TIME_LOCKING",
                         "SERVICE_ADMISSION", "READY", "WORKLOAD_RUNNING",
                         "QUIESCING", "STOPPING"):
            assert expected in states, f"{expected} 전이가 없다: {states}"
        assert states[-1] in ("CLOSED", "FAILED")
        # 모든 event에 session ID가 결속된다.
        assert all(r["session_id"] == session.session_id for r in records)
        # 마지막 계측이 flush됐다.
        assert records[-1]["event"] in ("state", "trace_closed")


def test_bringup_failure_still_cleans_up_and_records():
    with tempfile.TemporaryDirectory(dir=_REPO) as tmp:
        pi = _FakePi(ready=False)
        # bring-up이 끝나지 않도록 timeout을 짧게 만든다.
        original = stage7_session.SESSION_BRINGUP_TIMEOUT_S
        stage7_session.SESSION_BRINGUP_TIMEOUT_S = 0.2
        try:
            try:
                _session(tmp, pi)
                raise AssertionError("bring-up 실패가 통과했다")
            except stage7_run.ServiceSessionError as exc:
                assert "bring-up" in str(exc)
        finally:
            stage7_session.SESSION_BRINGUP_TIMEOUT_S = original
        # 실패 경로에서도 shutdown(=cleanup)이 요청됐다.
        assert pi.stop_count == 1


# --------------------------------------------------- application 격리 (source)

def _controller_source():
    with io.open(CONTROLLER, encoding="utf-8") as handle:
        return handle.read()


def test_workload_namespaces_step_and_scenario_ids():
    source = _controller_source()
    assert 'scenario_id=f"mass-roc:{self._workload_id}"' in source
    assert 'step_id=f"{self._workload_id}/{step_id}"' in source


def test_event_log_supports_baseline_isolation():
    from apps.stage7_mass_roc_controller import EventLog
    with tempfile.TemporaryDirectory() as tmp:
        events_dir = os.path.join(tmp, "service_events")
        os.makedirs(events_dir)
        path = os.path.join(events_dir, "MASS-A.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": "DATA_RECEIVED",
                                     "payload_sha256": "old"}) + "\n")
        # baseline 없이 읽으면 이전 event가 보인다.
        first = EventLog(tmp, events_dir=events_dir)
        first.poll()
        assert len(first.records["MASS-A"]) == 1
        # baseline을 잡으면 이전 event는 보이지 않는다.
        second = EventLog(tmp, events_dir=events_dir, start_at_end=True)
        second.poll()
        assert second.records["MASS-A"] == [], "stale event가 섞였다"
        assert second.baseline["MASS-A"] > 0
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": "DATA_RECEIVED",
                                     "payload_sha256": "new"}) + "\n")
        second.poll()
        assert [r["payload_sha256"] for r in second.records["MASS-A"]] == ["new"]


def test_controller_session_lifecycle_skips_start_stop():
    source = _controller_source()
    tree = ast.parse(source)
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.unparse(main)
    assert "session_owned = arguments.lifecycle == 'session'" in body
    assert "if not session_owned:" in body, "session lifecycle에서 STOP을 건너뛰지 않는다"
    assert "_own_endpoint_lifecycle_start" in body
    # workload child는 새 agent/TrustStore를 만든다 (공유 금지).
    assert "_default_agents" in source


def test_scenarios_create_fresh_application_state():
    """005/006/001이 매 실행 새 agent·TrustStore를 만든다 (source 계약)."""
    source = _controller_source()
    tree = ast.parse(source)
    controller = next(n for n in tree.body
                      if isinstance(n, ast.ClassDef)
                      and n.name == "MassRocScenarioController")
    methods = {n.name: ast.unparse(n) for n in controller.body
               if isinstance(n, ast.FunctionDef)}
    assert "_default_agents()" in methods["run_sc_vde_005"]
    assert "_default_agents()" in methods["run_sc_vde_006"]
    assert "TrustStore()" in methods["run_sc_vde_001"]
    # 006 standalone은 같은 child 안에서 자체 precondition을 만든다.
    assert "run_sc_vde_005" in methods["run_sc_vde_006"]


# ------------------------------------------------ qualification 불변 (session)

def test_session_phase_issues_no_qualification_commands():
    with io.open(COORDINATOR, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    session_fn = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "run_mass_roc_session")
    barriers = [n.args[0].value for n in ast.walk(session_fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run_barrier" and n.args
                and isinstance(n.args[0], ast.Constant)]
    assert "start_cfo" not in barriers
    assert "calibrate_rf" not in barriers
    assert "acquire_host_time" in barriers
    assert "validate_rf_time" in barriers
    assert "stop" in barriers
    # docstring은 "무엇을 하지 않는지"를 설명하므로 금지어 검사에서 뺀다.
    statements = list(session_fn.body)
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]
    body = "\n".join(ast.unparse(node) for node in statements)
    assert "_run_rf_qualification" not in body
    assert "write_rr_evidence" not in body
    assert "research_ready" not in body
    assert "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1" in body


def test_session_phase_requires_full_c1_c6_and_rejects_empty_summary():
    with io.open(COORDINATOR, encoding="utf-8") as handle:
        source = handle.read()
    assert "SERVICE_RF_TIME_PATHS = P2_PATHS" in source
    assert 'SERVICE_RF_TIME_PAIRS = ("RA", "RB", "AB")' in source
    # all([]) 공허참 방지: required path set을 명시 검사한다.
    assert "PRE_CORRECTION summary 누락" in source
    assert "POST_CORRECTION summary 누락" in source


def test_menu_parser_has_verbose_and_diagnostics():
    parser = runner.build_parser()
    args = parser.parse_args(["menu", "--transport", "stage7",
                              "--profile", "p.json"])
    assert args.verbose is False and args.diagnostics is False
    args = parser.parse_args(["menu", "--transport", "stage7",
                              "--profile", "p.json", "--verbose",
                              "--diagnostics"])
    assert args.verbose is True and args.diagnostics is True


def test_legacy_paths_unchanged():
    """기존 direct 대화형·비대화형 run all 계약은 그대로다."""
    parser = runner.build_parser()
    assert parser.parse_args(["menu"]).transport == "direct"
    assert runner.main(["run", "SC-VDE-005", "--no-save",
                        "--now", "1760000000"]) == 0
    assert runner.main(["run", "all", "--transport", "stage7"]) == 2
    assert callable(stage7_run.run_stage7)


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase G persistent session 계약")
