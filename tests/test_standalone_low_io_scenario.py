"""R2 — READY 이후 저 I/O 시나리오 경로 계약 (spy/mock, RF·SSH 없음).

증명하는 것 (2026-09-06 R1 선택 롤백):
  - setup/preflight/bring-up 진행 표시는 유지되고, SESSION READY에서 progress
    reporter가 닫히며 ticker 스레드가 남지 않는다
  - READY 이후 workload 실행 중 ScenarioTracer 생성 0회, on_poll 전달 0회,
    service_events/presentation 파일 open·write 0회 (builtins.open·io.open spy)
  - 실제 LocalCoordinatorSession.wait_result가 여는 파일은 workload trace와
    result JSON뿐이다 (88fa6df 계약)
  - controller의 transfer 경계 presentation flush는 없고, TraceWriter.flush는
    버퍼 overflow(emit)·close 수명주기에서만 불린다
  - production 소스에 ScenarioTracer·on_poll 참조가 없다
"""

from __future__ import annotations

import builtins
import io
import os
import sys
import tempfile
import threading
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import standalone_v2_fixtures as fx  # noqa: E402
import test_mass_roc_stage7_backend as backend  # noqa: E402
import test_standalone_local_session as ls  # noqa: E402
import test_standalone_run_cli as rc  # noqa: E402
from apps import stage7_trace  # noqa: E402
from apps.standalone import local_session, progress as pg, run_cmd  # noqa: E402


class _OpenSpy:
    """builtins.open·io.open을 감싸 (경로, 모드, READY 이후 여부)를 기록한다."""

    def __init__(self, phase: dict) -> None:
        self.phase = phase
        self.records = []
        self._real = builtins.open

    def __enter__(self):
        real = self._real

        def spy(file, mode="r", *args, **kwargs):
            self.records.append((str(file), str(mode), bool(self.phase.get("ready"))))
            return real(file, mode, *args, **kwargs)

        builtins.open = spy
        io.open = spy
        return self

    def __exit__(self, *exc):
        builtins.open = self._real
        io.open = self._real
        return False

    def after_ready(self):
        return [(p, m) for p, m, ready in self.records if ready]


def _execute_with_spies(paths, factory, *, progress_ticker: bool):
    lines, reporters = [], []
    phase = {"ready": False, "reporter_closed_at_ready": None,
             "presentation_snapshot": None, "run_root": None}

    def snapshot_presentation():
        inst = factory.instances[0]
        pres = Path(inst.run_root) / "presentation"
        return {p.name: p.stat().st_size for p in pres.glob("*")} if pres.is_dir() else {}

    def out(text):
        lines.append(text)
        if "SESSION READY" in text:
            # bring-up의 마지막 줄. 이 줄 자체는 mirror(console.log)에 남는 것이
            # 계약이고(README: presentation은 bring-up 구간만), 그 다음 줄부터가
            # READY 이후 구간이다 — 스냅샷과 open 계측은 다음 줄에서 시작한다.
            phase["ready_line_seen"] = True
            phase["reporter_closed_at_ready"] = reporters[-1]._closed if reporters else None
        elif phase.get("ready_line_seen") and not phase["ready"]:
            phase["ready"] = True
            phase["presentation_snapshot"] = snapshot_presentation()

    original_make = pg.make_reporter

    def make_reporter(**kwargs):
        reporter = original_make(**kwargs)
        reporters.append(reporter)
        return reporter

    pg.make_reporter = make_reporter
    try:
        with _OpenSpy(phase) as spy:
            code = run_cmd.execute(
                paths, scenario="all", certificate_case=None,
                session_factory=factory, preflight_fn=fx.fake_preflight,
                usb_probe=lambda: dict(fx.USB_ALL),
                environment_manifest=dict(fx.ENV_MANIFEST), repo_root=rc._REPO,
                hardware_path=Path(paths.var_dir).parent / "hardware.toml",
                out=out, input_fn=lambda prompt: "q",
                progress_ticker=progress_ticker)
    finally:
        pg.make_reporter = original_make
    phase["presentation_after"] = snapshot_presentation()
    return code, lines, spy, phase, reporters


def test_ready_after_phase_has_no_tracer_polling_or_presentation_io():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = rc.make_session_factory()
        code, lines, spy, phase, reporters = _execute_with_spies(
            paths, factory, progress_ticker=True)
        assert code == run_cmd.EXIT_PASS, lines
        text = "\n".join(lines)

        # 1) setup/preflight/bring-up 진행 표시는 유지된다.
        assert "[PREFLIGHT]" in text and "[BRINGUP]" in text
        assert "Service session READY" in text and "SESSION READY" in text

        # 2) READY 시점에 reporter가 닫혔고 ticker 스레드가 남지 않는다.
        assert phase["reporter_closed_at_ready"] is True
        reporter = reporters[-1]
        assert reporter._closed is True
        ticker = reporter._ticker
        if ticker is not None:
            ticker.join(timeout=5.0)
            assert not ticker.is_alive(), "progress ticker thread survived READY"
        assert not [t for t in threading.enumerate() if t.name == "vdeter-progress"]

        # 3) ScenarioTracer는 존재하지도, 생성되지도 않는다.
        assert not hasattr(pg, "ScenarioTracer")
        # 4) on_poll은 wait_result에 전달되지 않는다 (fake가 기록).
        session = factory.instances[0]
        assert session.polls == [None] * len(run_cmd.DEFAULT_BATCH_PLAN)
        assert session.submitted == list(run_cmd.DEFAULT_BATCH_PLAN)

        # 5)·6) READY 이후 열린 파일: summary.json(원자 발행)뿐 —
        #       service_events·presentation·console/progress 로그 없음.
        after = spy.after_ready()
        assert after, "READY 이후 open 기록이 없다 — spy가 동작하지 않았다"
        offenders = [(p, m) for p, m in after
                     if "service_events" in p.replace("\\", "/")
                     or "presentation" in p.replace("\\", "/")
                     or p.endswith(("console.log", "progress.log", "run.log"))]
        assert offenders == [], offenders
        names = {os.path.basename(p) for p, _m in after}
        assert all(n.startswith("summary.json") for n in names), names
        # presentation 디렉터리는 READY 이후 크기·구성 변화가 없다.
        assert phase["presentation_snapshot"] is not None
        assert phase["presentation_after"] == phase["presentation_snapshot"]

        # 8)·9) 결과 요약·evidence·판정 계약은 그대로다.
        assert "result: PASS · 87.3 s · quiescence OK" in text
        assert "Closed — cleanup PASS, residual processes 0" in text
        assert (Path(session.run_root) / "summary.json").is_file()
        assert "[STEP" not in text and "[TX]" not in text and "[RX]" not in text


def test_real_session_wait_result_opens_only_trace_and_result():
    with tempfile.TemporaryDirectory() as tmp:
        ft = ls.FakeTime()
        session = ls.make_session(tmp, ft)
        session.start()
        wid = session.submit_workload("SC-VDE-001", "forged")
        trace_path = session.workload_trace_path(wid)
        result_path = session.results_dir / (wid + ".json")
        rows = []

        def hook():
            if ft.sleep_count == 1:
                ls.append_bytes(trace_path, b'{"event": "submit", "n": 1}\n')
            elif ft.sleep_count == 2:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                ls.state.write_json_atomic(result_path,
                                           {"workload_id": wid, "status": "PASS"})

        ft.on_sleep.append(hook)
        phase = {"ready": True}
        try:
            with _OpenSpy(phase) as spy:
                result = session.wait_result(wid, timeout_s=60.0, on_trace=rows.append)
        finally:
            session._close_console_handle()
        assert result["status"] == "PASS" and [r["n"] for r in rows] == [1]
        # 읽기로 연 파일은 workload trace와 result JSON뿐 (hook의 쓰기 제외).
        read_paths = {os.path.abspath(p) for p, m in spy.after_ready() if "r" in m and "+" not in m}
        allowed = {os.path.abspath(str(trace_path)), os.path.abspath(str(result_path))}
        assert read_paths <= allowed, read_paths - allowed
        assert not [p for p, _m in spy.after_ready() if "service_events" in p or "presentation" in p]
        # on_poll 인자는 더 이상 존재하지 않는다.
        try:
            session.wait_result(wid, timeout_s=1.0, on_poll=lambda: None)
        except TypeError:
            pass
        else:
            raise AssertionError("wait_result still accepts on_poll")
        session._close_console_handle()


def _run_with_trace_spy(scenario: str):
    """backend 하네스 + 실제 TraceWriter: flush 호출자 이름을 기록한다."""

    callers = []
    original_flush = stage7_trace.TraceWriter.flush

    def spy_flush(self):
        callers.append(sys._getframe(1).f_code.co_name)
        return original_flush(self)

    stage7_trace.TraceWriter.flush = spy_flush
    try:
        with tempfile.TemporaryDirectory() as run_root:
            _services, controller = backend.build_harness(run_root)
            trace_path = os.path.join(run_root, "workload-trace.jsonl")
            trace = stage7_trace.TraceWriter(trace_path, session_id="r2", workload_id="w")
            controller._trace = trace
            if scenario == "005":
                status = controller.run_sc_vde_005().to_dict()["status"]
            elif scenario == "006":
                status = controller.run_sc_vde_006(standalone=True)["result"]["status"]
            else:
                status = controller.run_sc_vde_001(certificate_case=scenario).to_dict()["status"]
            during = list(callers)
            trace.close()
            closed_lines = open(trace_path, "r", encoding="utf-8").read().splitlines()
    finally:
        stage7_trace.TraceWriter.flush = original_flush
    return status, during, list(callers), closed_lines


def test_controller_has_no_transfer_boundary_flush_but_lifecycle_flush_stays():
    for scenario in ("005", "006", "normal", "forged"):
        status, during, all_callers, closed_lines = _run_with_trace_spy(scenario)
        assert status == "PASS", (scenario, status)
        # transfer 경계(_deliver_command)의 presentation flush는 없다.
        assert "_deliver_command" not in during, (scenario, during)
        # 시나리오 중 flush는 버퍼 overflow(emit)에서만 가능하다.
        assert set(during) <= {"emit"}, (scenario, during)
        # 수명주기 flush(close)는 그대로 살아 있고 trace가 파일에 남는다.
        assert all_callers[len(during):] == ["close"], (scenario, all_callers)
        assert any('"event": "trace_closed"' in line for line in closed_lines)
        assert any('"event": "scenario' in line or '"event": "transfer' in line
                   or '"event": "submit' in line for line in closed_lines)


def test_production_sources_have_no_tracer_or_on_poll_references():
    sources = [
        Path(_REPO) / "apps" / "standalone" / "run_cmd.py",
        Path(_REPO) / "apps" / "standalone" / "local_session.py",
        Path(_REPO) / "apps" / "standalone" / "progress.py",
        Path(_REPO) / "apps" / "stage7_mass_roc_controller.py",
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "ScenarioTracer" not in text, path
        assert "on_poll" not in text, path
    assert "flush()" not in (Path(_REPO) / "apps" / "stage7_mass_roc_controller.py").read_text(
        encoding="utf-8").split("class MassRocScenarioController", 1)[1].split("def run_scenarios", 1)[0]
