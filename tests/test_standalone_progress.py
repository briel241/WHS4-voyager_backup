"""진행 표시·시나리오 추적(presentation) 시험 — 판정 무관, 표준 라이브러리.

필수 시험(사용자 지정 12항): TTY 같은 줄 갱신, 20칸 유니코드 바, ASCII
fallback, non-TTY 줄 로그(CR·ANSI 없음), 단계 변경/30초 heartbeat, 진행률
후퇴 없음, estimated 표기, waiting, exact에는 estimated 없음, 시나리오 구간
퍼센트 바 없음, run all [n/4], transfer·ACK 출력, 실패 계층/reason/evidence,
secret 미출력, 표시 on/off와 무관한 동일 판정.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import progress as pg  # noqa: E402


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TtyStream(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fileno")


def _reporter(*, tty, unicode=True, sink=None, clock=None, evidence=None):
    clock = clock or Clock()
    stream = TtyStream() if tty else io.StringIO()
    reporter = pg.ProgressReporter(sink, stream=stream, tty=tty,
                                   unicode=unicode, scope="BRINGUP",
                                   clock=clock, wallclock=lambda: 1_800_000_000,
                                   evidence_log=evidence, ticker=False)
    return reporter, stream, clock


def test_bar_is_exactly_twenty_cells_unicode_and_ascii():
    for fraction, filled in ((0.0, 0), (0.48, 10), (0.7, 14), (1.0, 20)):
        bar = pg.render_bar(fraction, unicode=True)
        assert bar[0] == "[" and bar[-1] == "]" and len(bar) == 22
        assert bar.count("█") == filled and bar.count("░") == 20 - filled
        ascii_bar = pg.render_bar(fraction, unicode=False)
        assert ascii_bar.count("#") == filled and ascii_bar.count("-") == 20 - filled
        assert "█" not in ascii_bar and "░" not in ascii_bar


def test_tty_updates_same_line_and_finalizes_on_phase_change():
    reporter, stream, clock = _reporter(tty=True)
    reporter.phase("Endpoint startup", mode=pg.MODE_EXACT, fraction=0.0, ready="0/3")
    reporter.update(fraction=2 / 3, ready="2/3")
    text = stream.getvalue()
    assert "\r" in text and "\n" not in text          # 같은 줄 덮어쓰기
    assert "\x1b" not in text                          # ANSI 없음
    assert "ready=2/3" in text and " 66%" in text and "estimated" not in text
    reporter.phase("Host-time acquisition", mode=pg.MODE_WAITING, hold_fraction=0.2)
    text = stream.getvalue()
    assert text.count("\n") == 1                       # 이전 줄 확정
    finalized = text.split("\n")[0].split("\r")[-1]
    assert "Endpoint startup" in finalized and " 66%" in finalized
    last = text.split("\n")[-1]
    assert "progress n/a" in last and "%" not in last.split("]")[1].split("Host")[0]
    reporter.finish("FAILED")
    assert "FAILED" in stream.getvalue().split("\n")[-2]
    reporter.close()


def test_ascii_fallback_when_stream_cannot_encode_unicode():
    class LatinStream(io.StringIO):
        encoding = "latin-1"

        def isatty(self):
            return True

    stream = LatinStream()
    reporter = pg.ProgressReporter(None, stream=stream, tty=True, ticker=False)
    assert reporter.unicode is False
    reporter.phase("x", mode=pg.MODE_ESTIMATED, fraction=0.48)
    assert "[##########----------]" in stream.getvalue()
    assert "48% estimated" in stream.getvalue()
    reporter.close()


def test_non_tty_writes_plain_lines_without_cr_or_ansi_and_heartbeats():
    lines = []
    reporter, stream, clock = _reporter(tty=False, sink=lines.append)
    reporter.phase("Host-time acquisition", mode=pg.MODE_WAITING, endpoint="MASS-B")
    assert lines
    first = lines[-1]
    assert first[4] == "-" and first.endswith("Z") is False and "T" in first[:20]
    assert "[BRINGUP] progress n/a Host-time acquisition endpoint=MASS-B elapsed=00:00" in first
    assert "\r" not in first and "\x1b" not in first
    # 같은 값 반복은 억제된다.
    reporter.update(endpoint="MASS-B")
    reporter.tick()
    assert len(lines) == 1
    # 30초 무이벤트 → heartbeat 줄(경과시간 갱신).
    clock.advance(31)
    reporter.tick()
    assert len(lines) == 2 and "elapsed=00:31" in lines[-1] and "heartbeat=1" in lines[-1]
    # 의미 있는 변화(관측값) → 새 줄 (최소 간격 뒤).
    clock.advance(6)
    reporter.update(observations=184)
    assert len(lines) == 3 and "observations=184" in lines[-1]
    # 단계 변경 → 새 줄, 퍼센트·estimated 표기.
    reporter.phase("RF-time PRE validation", mode=pg.MODE_ESTIMATED, fraction=0.48)
    assert "48% estimated RF-time PRE validation" in lines[-1]
    assert stream.getvalue() == ""  # non-TTY는 stream에 바를 그리지 않는다
    reporter.close()


def test_percent_never_goes_backwards_and_waiting_holds():
    reporter, stream, clock = _reporter(tty=True)
    reporter.phase("A", mode=pg.MODE_ESTIMATED, fraction=0.5)
    reporter.update(fraction=0.3)                 # 뒤로 가려는 값
    assert " 50% estimated" in stream.getvalue().split("\r")[-1]
    reporter.phase("B", mode=pg.MODE_WAITING, hold_fraction=0.1)
    reporter.update(events=5)
    last = stream.getvalue().split("\r")[-1]
    assert "progress n/a" in last and "events=5" in last
    # 단조는 단계 안에서만: 새 단계는 앞 단계의 50%에 눌어붙지 않고 자기 값부터.
    reporter.phase("C", mode=pg.MODE_ESTIMATED, fraction=0.1)
    assert " 10% estimated" in stream.getvalue().split("\r")[-1]
    reporter.update(fraction=0.05)
    assert " 10% estimated" in stream.getvalue().split("\r")[-1]
    reporter.finish("done")
    reporter.close()


def test_exact_has_no_estimated_and_estimated_is_marked():
    assert pg.status_column(pg.MODE_EXACT, 70).strip() == "70%"
    assert pg.status_column(pg.MODE_ESTIMATED, 48).strip() == "48% estimated"
    assert pg.status_column(pg.MODE_WAITING, None).strip() == "progress n/a"
    assert pg.status_column(pg.MODE_EXACT, 100).strip() == "100%"
    assert len(pg.status_column(pg.MODE_ESTIMATED, 5)) == pg.STATUS_COLUMN_WIDTH


def test_evidence_log_mirrors_progress_and_scrubs_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "presentation" / "progress.log"
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append,
                                            evidence=log)
        reporter.phase("Preflight", mode=pg.MODE_EXACT, fraction=0.0)
        reporter.log("key material -----BEGIN EC PRIVATE KEY----- MIGH... "
                     "password=hunter2 end")
        reporter.finish("done")
        text = log.read_text("utf-8")
        assert "Preflight" in text and "[BRINGUP]" in text
        joined = "\n".join(lines)
        assert "BEGIN EC PRIVATE KEY" not in joined and "hunter2" not in joined
        assert "<redacted>" in joined
        reporter.close()


def _console_lines():
    return [
        "[coordinator] MASS-A 연결됨", "[coordinator] MASS-B 연결됨",
        "[coordinator] ROC 연결됨",
        "[coordinator] prepare/configure/arm_rx COMPLETED",
        "[guard:MASS-ROC-SESSION] progress #20 terminal:acquire_host_time",
        "[guard:MASS-ROC-SESSION] progress #21 terminal:acquire_host_time",
        "[coordinator] acquire_host_time COMPLETED",
        "[guard:MASS-ROC-SESSION] progress #64 terminal:validate_rf_time",
        "[coordinator] validate_rf_time PRE COMPLETED",
        "[guard:MASS-ROC-SESSION] progress #95 terminal:prepare_active_bridge",
        "[coordinator] active bridge COMMITTED",
        "[coordinator] validate_rf_time POST COMPLETED — HOST_TIME_LOCKED",
        "[guard:MASS-ROC-SESSION] progress #133 terminal:open_service_admission",
        "[coordinator] publish_service_start COMPLETED — RUNNING",
    ]


def test_bringup_tracker_phases_exact_waiting_estimated_ready():
    with tempfile.TemporaryDirectory() as tmp:
        console = Path(tmp) / "coordinator-console.log"
        console.write_text("", encoding="utf-8")
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append)
        tracker = pg.BringupTracker(reporter, console_log=console, clock=clock)
        assert "Endpoint startup" in lines[-1] and "ready=0/3" in lines[-1]

        def feed(n):
            with open(console, "a", encoding="utf-8") as handle:
                for line in _console_lines()[n:n + 1]:
                    handle.write(line + "\n")
            clock.advance(6)
            tracker.poll()

        assert "] 0% Endpoint startup" in lines[-1] and "overall=0%" in lines[-1]
        feed(0); feed(1)
        # 바의 퍼센트는 이 단계 자체(2/3=66%), 전체 진행률은 overall 필드.
        assert "] 66% Endpoint startup" in lines[-1] and "ready=2/3" in lines[-1]
        assert "estimated" not in lines[-1] and "overall=0%" in lines[-1]
        feed(2); feed(3)
        startup_done = [l for l in lines if "Endpoint startup" in l][-1]
        assert "] 100% Endpoint startup" in startup_done and "ready=3/3" in startup_done
        assert "progress n/a Host-time acquisition" in lines[-1]
        feed(4); feed(5)
        assert "events=2" in lines[-1] and "progress n/a" in lines[-1]
        feed(6)
        assert "RF-time PRE validation" in lines[-1] and "estimated" in lines[-1]
        assert "] 0% estimated RF-time PRE" in lines[-1]
        clock.advance(200); tracker.poll()
        pre = [l for l in lines if "RF-time PRE" in l][-1]
        # 200s/457s → 이 단계 43%; overall=(15+448+0.43*457)/1392 ≈ 47%.
        assert "] 43% estimated RF-time PRE" in pre and "overall=47%" in pre
        feed(8); feed(9)
        pre_done = [l for l in lines if "RF-time PRE" in l][-1]
        assert "] 100% RF-time PRE" in pre_done
        assert "Active bridge commit" in lines[-1]
        feed(10)   # active bridge COMMITTED → RF-time POST 검증 시작
        assert "] 0% estimated RF-time POST validation" in lines[-1]
        clock.advance(100); tracker.poll()
        assert "] 21% estimated RF-time POST validation" in lines[-1]
        feed(11); feed(12); feed(13)
        tracker.ready()
        assert "Service session READY" in lines[-1] and "100%" in lines[-1]
        assert "total=" in lines[-1] and "overall=100%" in lines[-1]
        # 퍼센트는 같은 단계(label) 안에서 뒤로 가지 않는다; overall도 단조.
        by_label, overalls = {}, []
        for line in lines:
            if "] " not in line:
                continue
            rest = line.split("] ", 1)[1]
            token = rest.split(" ")[0]
            label = rest.split(" elapsed=")[0]
            for key in (" ready=", " events=", " overall=", " paths=", " total="):
                label = label.split(key)[0]
            label = label.split(" ", 1)[1] if token.endswith("%") else label
            if token.endswith("%"):
                by_label.setdefault(label, []).append(int(token[:-1]))
            if " overall=" in line:
                overalls.append(int(line.split(" overall=")[1].split("%")[0]))
        assert all(v == sorted(v) for v in by_label.values()), by_label
        assert overalls == sorted(overalls), overalls
        reporter.close()


def test_bringup_bar_starts_at_zero_after_preflight_on_same_reporter():
    """Pi 실측 결함 재현: PREFLIGHT 게이트가 100%로 끝난 같은 reporter에서
    bring-up 첫 줄이 100%로 시작했다(전역 단조 클램프). 단계별 단조여야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        console = Path(tmp) / "coordinator-console.log"
        console.write_text("", encoding="utf-8")
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append)
        gates = pg.GateProgress(reporter, 5)
        for name in ("installation manifest", "active reference", "identity",
                     "preflight / B210 discovery", "critical comparison"):
            gates.gate(name)
        gates.done()
        assert "100% Preflight" in lines[-1]
        # 게이트 줄은 각각 자기 과업: 시작 0% → 확정 100% (전체 위치는 gate=n/N).
        finals = [l for l in lines if "Preflight" in l and "100%" in l]
        assert len(finals) == 5 and all("gate=" in l for l in finals), lines
        assert not any(("20%" in l or "40%" in l) and "Preflight" in l for l in lines)
        pg.BringupTracker(reporter, console_log=console, clock=clock)
        assert "] 0% Endpoint startup" in lines[-1] and "ready=0/3" in lines[-1]
        reporter.close()


def test_qualify_percent_is_per_phase_and_overall_is_a_field():
    """Pi 실측 결함 재현: RR-P1 종료 시 phase 가중치가 두 번 더해져 99%가 되고
    이후 RR-P2-17·P3·suite·builder 줄이 전부 99%에 고정됐다. 각 줄의 퍼센트는
    그 phase 자체(0→100%), 사다리 전체는 overall 필드여야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append)
        qp = pg.QualifyProgress(reporter, clock=clock)

        def start(phase, index):
            root = Path(tmp) / phase
            root.mkdir()
            (root / "coordinator-console.log").write_text("", encoding="utf-8")
            qp.phase_started(phase, index, 4, root)

        start("rr-p0", 1)
        assert "] 0% estimated RR-P0 1/4" in lines[-1] and "overall=0%" in lines[-1]
        qp.phase_finished("rr-p0", "PASS")
        assert "] 100% RR-P0 1/4" in lines[-1] and "result=PASS" in lines[-1]
        start("rr-p1", 2)
        assert "] 0% estimated RR-P1 2/4" in lines[-1]
        clock.advance(pg.QUALIFY_WEIGHTS["rr-p1"] * 0.5); qp.tick()
        # 이 phase 50%; overall=(20+0.5*3395)/6796 ≈ 25%.
        assert "] 50% estimated RR-P1 2/4" in lines[-1] and "overall=25%" in lines[-1]
        qp.phase_finished("rr-p1", "PASS")
        assert "] 100% RR-P1 2/4" in lines[-1] and "overall=50%" in lines[-1]
        start("rr-p2-17", 3)
        assert "] 0% estimated RR-P2-17 3/4" in lines[-1] and "overall=50%" in lines[-1]
        assert not any("99%" in l for l in lines)
        qp.phase_finished("rr-p2-17", "PASS")
        start("rr-p3-fault", 4)
        assert "] 0% estimated RR-P3-FAULT 4/4" in lines[-1] and "overall=84%" in lines[-1]
        # estimated(경과) → exact(subcase) 전환 때 overall이 뒤로 가지 않는다
        # (경과 60s ≈ 85%, 첫 subcase 1/6 ≈ 84%: 후퇴 대신 85% 유지).
        clock.advance(60); qp.tick()
        assert "overall=85%" in lines[-1]
        console = Path(tmp) / "rr-p3-fault" / "coordinator-console.log"
        with open(console, "a", encoding="utf-8") as h:
            h.write("[coordinator] fault stale-generation-command: PASS {}\n")
        clock.advance(6); qp.tick()
        assert "subcases=1/6" in lines[-1] and "overall=85%" in lines[-1]
        qp.phase_finished("rr-p3-fault", "PASS")
        log = Path(tmp) / "unit-contract.log"
        log.write_text("", encoding="utf-8")
        qp.bundle_started()
        qp.suite_started("unit-contract.log", log)
        assert "] 0% Software suite 1/2" in lines[-1] and "overall=86%" in lines[-1]
        qp.suite_finished("unit-contract.log", True)
        assert "] 100% Software suite 1/2" in lines[-1]
        qp.builder_started()
        assert "] 0% estimated Final builder" in lines[-1]
        qp.promoted("qual-x")
        assert "] 100% QUALIFIED" in lines[-1] and "overall=100%" in lines[-1]
        reporter.close()


def test_tty_finish_does_not_duplicate_evidence_lines():
    """TTY에서 단계 확정 줄이 값 변화 없이 evidence에 두 번 기록되지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "progress.log"
        reporter, stream, clock = _reporter(tty=True, evidence=log)
        gates = pg.GateProgress(reporter, 2)
        gates.gate("a"); gates.gate("b"); gates.done()
        reporter.close()
        text = log.read_text("utf-8").splitlines()
        assert len(text) == len(set(text)), text
        assert any("100% Preflight · b" in l for l in text)


def test_bringup_failure_lines_have_layer_reason_and_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "run-x"
        (run_root / "evidence").mkdir(parents=True)
        (run_root / "evidence" / "MASS-B-runtime.json").write_text(json.dumps({
            "endpoint_id": "MASS-B", "operations_evidence": [
                {"kind": "host_time_restarts", "restarts": [
                    {"discarded": 148, "reason": "bracket width"},
                    {"discarded": 77, "reason": "bracket width"}]}]}))
        detail = pg.host_time_detail_from_evidence(run_root)
        assert detail == {"endpoint": "MASS-B", "restart_count": 2,
                          "observations_discarded": 225, "reason": "bracket width"}
        lines = pg.bringup_failure_lines(
            phase_label="Host-time acquisition",
            finalize_summary={"closed": {"cleanup": {"pass": True},
                                         "duration_s": 600.0},
                              "residual_processes": []},
            classification={"transient_host_time": True, "reasons": []},
            console_text="MASS-B host affine fit FAIL", run_root=run_root,
            attempt=1, max_attempts=2, declared_serials=["a", "b", "c"],
            usb_observed={"a": {}, "b": {}, "c": {}}, host_time_detail=detail)
        joined = "\n".join(lines)
        for needle in ("layer=ORCHESTRATION", "endpoint=MASS-B",
                       "reason=HOST_AFFINE_FIT_FAILED",
                       "observations_discarded=225", "restart_count=2",
                       "elapsed=10:00", "cleanup=PASS", "residual_processes=0",
                       "b210_rediscovered=3/3", "retry_eligible=true",
                       "attempt=1/2", "evidence=%s" % run_root):
            assert needle in joined, needle


def test_menu_input_ignores_arrow_key_escape_sequences():
    from apps.standalone import run_cmd
    assert run_cmd._clean_choice("\x1b[A") == ""
    # Pi 실측 입력(Alt+S, Delete, Alt+a): CSI 시퀀스와 Alt+문자(ESC+문자)는 모두
    # 버려 빈 입력으로 취급 → 메뉴만 다시 표시, RF 동작 없음.
    assert run_cmd._clean_choice("\x1bS\x1b[3~\x1ba") == ""
    assert run_cmd._clean_choice("\x1b[Aa") == "a"
    assert run_cmd._clean_choice("  2 \n") == "2"
    assert run_cmd._clean_choice("q") == "q"


def test_close_notes_explain_rc1_and_cleanup():
    """close 보충 설명: workload FAIL에 따른 rc=1은 설계 동작, cleanup PASS 명시."""
    from apps.standalone import run_cmd
    notes = run_cmd.close_notes({
        "coordinator_rc": 1,
        "closed": {"status": "FAIL", "error": "하나 이상의 workload가 FAIL",
                   "cleanup": {"pass": True, "launcher_clean_exit": True,
                               "rediscovery": {"a": True, "b": True, "c": True},
                               "workers_after_stop": 0}}})
    assert notes[0].startswith("coordinator verdict FAIL because a workload failed")
    assert "cleanup PASS — launcher clean exit true, B210 rediscovered 3/3, workers after stop 0" in notes[1]
    assert run_cmd.close_notes({"coordinator_rc": 0, "closed": {}}) == []


def test_bringup_stage_lines_are_english():
    from apps.standalone import run_cmd
    assert run_cmd.english_stage_line("MASS-A 연결됨") == "MASS-A connected"
    assert run_cmd.english_stage_line("acquire_host_time COMPLETED") == \
        "acquire_host_time COMPLETED"


def test_qualify_progress_exact_faults_and_pytest_percent():
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append)
        qp = pg.QualifyProgress(reporter, clock=clock)
        run_root = Path(tmp) / "rr-p3"
        run_root.mkdir()
        console = run_root / "coordinator-console.log"
        console.write_text("", encoding="utf-8")
        qp.phase_started("rr-p3-fault", 4, 4, run_root)
        assert "RR-P3-FAULT 4/4" in lines[-1] and "estimated" in lines[-1]
        with open(console, "a", encoding="utf-8") as h:
            h.write("[coordinator] fault stale-generation-command: PASS {}\n")
        clock.advance(6); qp.tick()
        assert "subcases=1/6" in lines[-1] and "estimated" not in lines[-1]
        with open(console, "a", encoding="utf-8") as h:
            h.write("[coordinator] fault worker-kill: PASS {}\n")
        clock.advance(6); qp.tick()
        assert "subcases=2/6" in lines[-1] and "estimated" not in lines[-1]
        # 5초 안에 연속 변화가 오면 마지막 상태가 간격 뒤 tick에서 나온다.
        with open(console, "a", encoding="utf-8") as h:
            h.write("[coordinator] fault coordinator-ipc-loss: PASS {}\n")
            h.write("[coordinator] fault restart-generation-fencing: PASS {}\n")
        clock.advance(6); qp.tick()
        clock.advance(6); qp.tick()
        assert "subcases=4/6" in lines[-1]
        qp.phase_finished("rr-p3-fault", "PASS")
        assert "result=PASS" in lines[-1]
        # software suite: pytest -q 퍼센트 → exact.
        log = Path(tmp) / "unit-contract.log"
        log.write_text("", encoding="utf-8")
        qp.bundle_started()
        qp.suite_started("unit-contract.log", log)
        assert "Software suite 1/2" in lines[-1]
        with open(log, "a", encoding="utf-8") as h:
            h.write("........ [ 62%]\n")
        clock.advance(6); qp.tick()
        assert "tests=62%" in lines[-1] and "estimated" not in lines[-1]
        qp.suite_finished("unit-contract.log", True)
        assert "tests=100%" in lines[-1]
        reporter.close()


def test_progress_toggle_does_not_change_outcome_of_run_flow():
    """표시 on/off와 무관하게 판정·evidence가 동일 — run 흐름 시험은
    test_standalone_run_cli.test_progress_toggle_keeps_judgement_and_evidence_identical
    이 progress on/off 두 경로로 실행한다."""
    source = (Path(_REPO) / "tests" / "test_standalone_run_cli.py").read_text("utf-8")
    assert "def test_progress_toggle_keeps_judgement_and_evidence_identical" in source


def _event_fmt(stage_line):
    return "  [ev] " + stage_line


def _ordering_positions(lines):
    """(완료 100% 줄, 이벤트 줄, 다음 단계 0% 줄)의 인덱스."""
    done = [i for i, l in enumerate(lines) if "100% RF-time POST validation" in l or
            ("RF-time POST validation" in l and "100%" in l)]
    event = [i for i, l in enumerate(lines) if "[ev] validate_rf_time POST COMPLETED" in l]
    nxt = [i for i, l in enumerate(lines) if "Service startup" in l and ("0%" in l or "estimated" in l)]
    return done, event, nxt


def test_bringup_completion_event_order_non_tty_and_dedup():
    """완료 이벤트는 '해당 단계 100% 확정 → 이벤트 → 다음 단계 0%' 순서이며,
    콘솔 tail과 on_stage 두 경로로 같은 줄이 와도 이벤트는 한 번만 난다."""
    with tempfile.TemporaryDirectory() as tmp:
        console = Path(tmp) / "coordinator-console.log"
        console.write_text("", encoding="utf-8")
        lines = []
        reporter, stream, clock = _reporter(tty=False, sink=lines.append)
        tracker = pg.BringupTracker(reporter, console_log=console, clock=clock,
                                    event_formatter=_event_fmt)
        with open(console, "a", encoding="utf-8") as handle:
            for line in _console_lines()[:11]:
                handle.write(line + "\n")
        clock.advance(6); tracker.poll()
        assert "RF-time POST validation" in lines[-1]
        # (a) on_stage가 먼저 오는 경우
        clock.advance(6)
        tracker.on_stage_line("validate_rf_time POST COMPLETED — HOST_TIME_LOCKED")
        with open(console, "a", encoding="utf-8") as handle:
            handle.write("[coordinator] validate_rf_time POST COMPLETED — HOST_TIME_LOCKED\n")
        clock.advance(6); tracker.poll()   # tail이 같은 줄을 보지만 중복 처리하지 않는다
        done, event, nxt = _ordering_positions(lines)
        assert len(event) == 1, lines
        assert done and nxt and done[-1] < event[0] < nxt[0], lines
        # (b) 콘솔 tail이 먼저 오는 경우 (service 완료 → READY 확정 → 이벤트)
        with open(console, "a", encoding="utf-8") as handle:
            handle.write("[coordinator] publish_service_start COMPLETED — RUNNING\n")
        clock.advance(6); tracker.poll()
        tracker.on_stage_line("publish_service_start COMPLETED — RUNNING")
        ready_idx = [i for i, l in enumerate(lines) if "Service session READY" in l and "100%" in l]
        ev2 = [i for i, l in enumerate(lines) if "[ev] publish_service_start COMPLETED" in l]
        assert len(ev2) == 1 and ready_idx and ready_idx[-1] < ev2[0], lines
        tracker.ready()   # fallback은 중복 출력하지 않는다
        assert len([l for l in lines if "Service session READY" in l and "100%" in l]) == 1
        reporter.close()


def test_bringup_completion_event_order_tty():
    """TTY도 같은 논리 순서: 확정 줄(개행) → 이벤트 줄 → 다음 단계 바."""
    with tempfile.TemporaryDirectory() as tmp:
        console = Path(tmp) / "coordinator-console.log"
        console.write_text("", encoding="utf-8")
        reporter, stream, clock = _reporter(tty=True)
        tracker = pg.BringupTracker(reporter, console_log=console, clock=clock,
                                    event_formatter=_event_fmt)
        with open(console, "a", encoding="utf-8") as handle:
            for line in _console_lines()[:11]:
                handle.write(line + "\n")
        clock.advance(6); tracker.poll()
        clock.advance(6)
        tracker.on_stage_line("validate_rf_time POST COMPLETED — HOST_TIME_LOCKED")
        text = stream.getvalue()
        i_event = text.find("[ev] validate_rf_time POST COMPLETED")
        i_done = text.rfind("100%", 0, i_event)             # 확정된 100% 줄
        i_next = text.find("Service startup", i_event)
        assert 0 <= i_done < i_event < i_next, text[-600:]
        assert "RF-time POST validation" in text[i_done:i_event]
        assert text[i_done:i_event].count("\n") >= 1      # 확정 줄은 개행으로 닫힌다
        assert "waiting" not in text and "progress n/a" in text
        reporter.close()
