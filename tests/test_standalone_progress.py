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
    assert "waiting" in last and "%" not in last.split("]")[1].split("Host")[0]
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
    assert "[BRINGUP] waiting Host-time acquisition endpoint=MASS-B elapsed=00:00" in first
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
    assert "waiting" in last and "events=5" in last
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
    assert pg.status_column(pg.MODE_WAITING, None).strip() == "waiting"
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
        assert "waiting Host-time acquisition" in lines[-1]
        feed(4); feed(5)
        assert "events=2" in lines[-1] and "waiting" in lines[-1]
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


def _service_event(**fields):
    base = {"t_mono": 100.0, "event_type": "TRANSFER_STATE_CHANGED",
            "transfer_id": None, "transfer_state": None, "error_code": None}
    base.update(fields)
    return json.dumps(base, ensure_ascii=False) + "\n"


def test_scenario_tracer_prints_transfer_ack_and_no_percent_bar():
    with tempfile.TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "run-1"
        events = run_root / "evidence" / "service_events"
        events.mkdir(parents=True)
        for ep in ("MASS-A", "MASS-B", "ROC"):
            (events / f"{ep}.jsonl").write_text("", encoding="utf-8")
        lines = []
        tracer = pg.ScenarioTracer(lines.append, run_root=run_root,
                                   clock=Clock(), wallclock=lambda: 1_800_000_000)
        tracer.start_workload(index=2, total=4, scenario_id="SC-VDE-006",
                              certificate_case="normal",
                              workload_id="run-1-sc006-002", run_id="run-1")
        # 블록 앞 빈 줄 + 헤더 + key=value 한 줄씩 (세로 블록).
        assert lines[0] == "" and lines[1] == "[2/4] SC-VDE-006"
        assert lines[2:5] == ["  time=2027-01-15T08:00:00Z", "  run_id=run-1",
                              "  workload_id=run-1-sc006-002"]
        tid = "8f73c821-0000-4000-8000-000000000001"
        with open(events / "ROC.jsonl", "a", encoding="utf-8") as h:
            h.write(_service_event(transfer_id=tid, transfer_state="QUEUED", t_mono=100.0))
            h.write(_service_event(transfer_id=tid, transfer_state="TRANSMITTING", t_mono=103.5))
            h.write(_service_event(transfer_id=tid, transfer_state="WAITING_ACK", t_mono=104.0))
        with open(events / "MASS-A.jsonl", "a", encoding="utf-8") as h:
            h.write(_service_event(event_type="DATA_RECEIVED", t_mono=104.2,
                                   payload_sha256="9b28d774" + "0" * 56,
                                   payload_len=29,
                                   payload_format="sc_vde.route_control.command",
                                   source=222222222, original_source=222222222,
                                   original_destination=111111111))
        with open(events / "ROC.jsonl", "a", encoding="utf-8") as h:
            h.write(_service_event(event_type="DATA_RECEIVED", t_mono=110.0,
                                   payload_sha256="2b657b49" + "0" * 56,
                                   payload_len=3,
                                   payload_format="sc_vde.route_control.ack",
                                   source=111111111, original_source=111111111,
                                   original_destination=222222222))
            h.write(_service_event(transfer_id=tid, transfer_state="COMPLETED", t_mono=112.0))
        tracer.poll()
        joined = "\n".join(lines)
        # 세로 블록: 헤더 한 줄 + 들여쓴 필드 한 줄씩. 가로 한 줄 형식은 없다.
        assert "[TX] QUEUED\n  endpoint=ROC\n  transfer_id=8f73c821…\n" in joined
        assert ("[TX] TRANSMITTING\n  endpoint=ROC\n  transfer_id=8f73c821…\n"
                "  since_queued=3.500s derived=true\n") in joined
        assert ("[RX] DATA_RECEIVED\n  endpoint=MASS-A\n"
                "  payload_format=sc_vde.route_control.command\n"
                "  application_bytes=29\n  payload_sha256=9b28d7740000…\n"
                "  src=ROC(222222222)\n  dst=MASS-A(111111111)\n") in joined
        # COMPLETED after WAITING_ACK = MAC ACK 수신; latency는 WAITING_ACK(104)→
        # COMPLETED(112) 전이 8 s. 응용 .ack payload(110)는 별개 [RX]로 보인다.
        assert ("[TX] COMPLETED\n  endpoint=ROC\n  transfer_id=8f73c821…\n"
                "  since_queued=12.000s derived=true\n  ack=received\n"
                "  ack_source=transfer_state derived=true\n"
                "  ack_latency=8.000s derived=true") in joined
        assert "completed_without_observed_ack_payload" not in joined
        assert not any(l.startswith("[TX]") and "endpoint=" in l for l in lines)
        # 블록 사이는 빈 줄 하나.
        assert "  transfer_id=8f73c821…\n\n[TX] TRANSMITTING\n" in joined
        assert "\n\n\n" not in joined
        # 종료 시 workload trace·result 상세.
        tracer.on_trace_row({"event": "submit", "step": "006-steering-command",
                             "t_utc": "2026-09-05T12:25:14Z", "transfer_id": tid,
                             "sender": "ROC", "receiver": "MASS-A",
                             "source_station": 222222222,
                             "destination_station": 111111111,
                             "payload_format": "sc_vde.route_control.command",
                             "payload_len": 29, "payload_sha256": "9b28d774" + "0" * 56,
                             "ack_required": True})
        tracer.on_trace_row({"event": "scenario_result", "status": "PASS",
                             "steps": [{"name": "006-steering-transfer", "status": "PASS",
                                        "state": "COMPLETED", "transfer_id": tid,
                                        "received_checks": {"byte_exact": True,
                                                            "payload_sha256": True}}],
                             "counters": {"duplicates": 0, "mismatched_ack": 0,
                                          "transfers_completed": 1,
                                          "transfers_failed": 0,
                                          "wrong_destination": 0}})
        # 판정 블록은 종료 요약과 함께 나온다 — 실시간 흐름 중간에 끼지 않음.
        assert not any(l.startswith("[STEP 1/1]") for l in lines)
        tracer.finish_workload({"status": "PASS", "wall_clock_s": 75.2,
                                "quiescence": {"pass": True, "non_terminal_transfers": 0,
                                               "transfers_observed": 5}},
                               evidence_dir=run_root / "workloads" / "x")
        joined = "\n".join(lines)
        assert ("[STEP] 006-steering-command  submit\n  time=2026-09-05T12:25:14Z\n"
                "  transfer_id=8f73c821…\n  src=ROC(222222222)\n  dst=MASS-A(111111111)\n"
                "  payload_format=sc_vde.route_control.command\n  application_bytes=29\n"
                "  payload_sha256=9b28d7740000…\n  ack_required=true\n") in joined
        assert ("[STEP 1/1] 006-steering-transfer  PASS\n  transfer_id=8f73c821…\n"
                "  state=COMPLETED\n  byte_exact=true\n  payload_sha256=true\n") in joined
        assert ("\n\n  counters: transfers_completed=1 transfers_failed=0\n"
                "            wrong_destination=0 mismatched_ack=0 duplicates=0\n"
                "  result=PASS") in joined
        assert "  result=PASS  elapsed=" in joined and "wall_clock_s=75.2" in joined
        assert "  quiescence=PASS  non_terminal_transfers=0  transfers_observed=5" in joined
        assert "  workers_running=-  launchers_alive=-  faults=-" not in joined
        assert "evidence=" in joined
        # 시나리오 구간에는 퍼센트 바가 없다.
        assert "%" not in joined.replace("100%", "")  # 퍼센트 문자 자체가 없다
        assert "█" not in joined and "[#" not in joined


def test_scenario_tracer_prints_step_pairs_from_trace_and_falls_back_to_session_result():
    """실측 workload trace의 scenario_result.steps는 [name, status] 쌍이다
    (dict 아님) — 예전 코드는 이를 건너뛰어 [STEP n/N] 줄이 전혀 나오지 않았다."""
    with tempfile.TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "run-3"
        (run_root / "evidence" / "service_events").mkdir(parents=True)
        lines = []
        tracer = pg.ScenarioTracer(lines.append, run_root=run_root, clock=Clock())
        tracer.start_workload(index=1, total=4, scenario_id="SC-VDE-005",
                              certificate_case="normal", workload_id="w5",
                              run_id="run-3")
        tracer.on_trace_row({"event": "scenario_result", "status": "PASS",
                             "steps": [["005-command-transfer", "PASS"],
                                       ["005-route-applied", "PASS"],
                                       ["005-mass-b-clean", "PASS"]],
                             "counters": {"transfers_completed": 2,
                                          "transfers_failed": 0,
                                          "wrong_destination": 0,
                                          "mismatched_ack": 0, "duplicates": 0}})
        assert not any(l.startswith("[STEP 1/3]") for l in lines)   # 종료 시 출력
        # trace의 쌍 steps가 있으면 session result의 dict steps보다 우선, 한 번만.
        tracer.finish_workload({"status": "PASS", "wall_clock_s": 18.4,
                                "quiescence": {"pass": True},
                                "scenario": {"results": {"SC-VDE-005": {
                                    "steps": [{"name": "005-command-transfer",
                                               "status": "PASS"}]}}}},
                               evidence_dir=run_root / "workloads" / "w5")
        joined = "\n".join(lines)
        # 판정 줄 목록은 붙여서 한 블록, 앞에만 빈 줄, 이어서 요약 블록.
        assert ("\n\n[STEP 1/3] 005-command-transfer  PASS\n"
                "[STEP 2/3] 005-route-applied     PASS\n"
                "[STEP 3/3] 005-mass-b-clean      PASS\n\n  counters:"
                " transfers_completed=2 transfers_failed=0\n"
                "            wrong_destination=0 mismatched_ack=0 duplicates=0\n"
                "  result=PASS") in joined
        assert joined.count("[STEP 1/") == 1
        # trace에 scenario_result가 없었던 workload는 session result로 출력.
        lines.clear()
        tracer.start_workload(index=2, total=4, scenario_id="SC-VDE-006",
                              certificate_case="normal", workload_id="w6",
                              run_id="run-3")
        tracer.finish_workload({"status": "PASS", "wall_clock_s": 80.6,
                                "quiescence": {"pass": True},
                                "scenario": {"results": {"SC-VDE-006": {
                                    "steps": [{"name": "006-steering-transfer",
                                               "status": "PASS",
                                               "state": "COMPLETED",
                                               "transfer_id": "32d95f97-1"}]}}}},
                               evidence_dir=None)
        joined = "\n".join(lines)
        assert ("[STEP 1/1] 006-steering-transfer  PASS\n  transfer_id=32d95f97…\n"
                "  state=COMPLETED") in joined


def test_menu_input_ignores_arrow_key_escape_sequences():
    from apps.standalone import run_cmd
    assert run_cmd._clean_choice("\x1b[A") == ""
    # Pi 실측 입력(Alt+S, Delete, Alt+a): CSI 시퀀스와 Alt+문자(ESC+문자)는 모두
    # 버려 빈 입력으로 취급 → 메뉴만 다시 표시, RF 동작 없음.
    assert run_cmd._clean_choice("\x1bS\x1b[3~\x1ba") == ""
    assert run_cmd._clean_choice("\x1b[Aa") == "a"
    assert run_cmd._clean_choice("  2 \n") == "2"
    assert run_cmd._clean_choice("q") == "q"


def test_failure_layer_uses_observed_service_errors_and_close_notes_explain_rc1():
    """실측 run-20260905-044555 재현: forged 인증서가 MASS-A PHY에서
    LinkIdDetectionError로 4회 복호 실패 → ROC transfer TRANSFER_TIMEOUT.
    결과 error 문자열이 비어 있어도 관측 ERROR로 layer=PHY여야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "run-f"
        events = run_root / "evidence" / "service_events"
        events.mkdir(parents=True)
        for ep in ("MASS-A", "MASS-B", "ROC"):
            (events / f"{ep}.jsonl").write_text("", encoding="utf-8")
        lines = []
        tracer = pg.ScenarioTracer(lines.append, run_root=run_root, clock=Clock())
        tracer.start_workload(index=4, total=4, scenario_id="SC-VDE-001",
                              certificate_case="forged", workload_id="w4",
                              run_id="run-f")
        with open(events / "MASS-A.jsonl", "a", encoding="utf-8") as h:
            for _ in range(4):
                h.write(json.dumps({
                    "t_mono": 100.0, "event_type": "ERROR", "transfer_id": None,
                    "transfer_state": None, "error_code": "TRANSFER_FAILED",
                    "error_message": "전송 처리에 실패했습니다",
                    "error_details": "Details(entries=(('reason', 'LinkIdDetectionError'),))"}) + "\n")
        with open(events / "ROC.jsonl", "a", encoding="utf-8") as h:
            h.write(_service_event(transfer_id="83b7bc2c-1", transfer_state="FAILED",
                                   t_mono=177.0, error_code="TRANSFER_TIMEOUT"))
        tracer.poll()
        joined = "\n".join(lines)
        assert "[ERR]\n  endpoint=MASS-A\n  error_code=TRANSFER_FAILED\n  reason=LinkIdDetectionError\n" in joined
        tracer.finish_workload({
            "status": "FAIL", "wall_clock_s": 318.7,
            "quiescence": {"pass": True, "non_terminal_transfers": 0},
            "scenario": {"error": None, "results": {"SC-VDE-001[forged]": {
                "status": "FAIL", "reason": "MASS-A가 인증서 DATA_RECEIVED를 받지 못함",
                "steps": [{"name": "001-transfer", "status": "FAIL",
                           "state": "FAILED", "transfer_id": "83b7bc2c-1"}]}}}},
            evidence_dir=run_root / "workloads" / "w4")
        joined = "\n".join(lines)
        assert "[FAIL] layer=PHY" in joined
        assert "observed_errors=MASS-A:LinkIdDetectionError×4" in joined
        assert "failed_step=001-transfer state=FAILED" in joined
    # close 보충 설명: workload FAIL에 따른 rc=1은 설계 동작, cleanup PASS 명시.
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


def test_scenario_tracer_failure_block_has_layer_reason_and_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        run_root = Path(tmp) / "run-2"
        (run_root / "evidence" / "service_events").mkdir(parents=True)
        lines = []
        tracer = pg.ScenarioTracer(lines.append, run_root=run_root, clock=Clock())
        tracer.start_workload(index=1, total=1, scenario_id="SC-VDE-005",
                              certificate_case="normal", workload_id="w1",
                              run_id="run-2")
        assert lines[0] == "" and lines[1] == "SC-VDE-005"   # 단일 시나리오는 [n/N] 없음
        tracer.finish_workload({
            "status": "FAIL", "wall_clock_s": 12.0,
            "quiescence": {"pass": False, "non_terminal_transfers": 1},
            "scenario": {"error": "ack timeout", "results": {"SC-VDE-005": {
                "result": {"status": "FAIL", "reason": "ack timeout",
                           "steps": [{"name": "005-command-transfer",
                                      "status": "FAIL", "state": "WAITING_ACK",
                                      "transfer_id": "b3ecf409-1"}]}}}}},
            evidence_dir=run_root / "workloads" / "w1")
        joined = "\n".join(lines)
        assert "[FAIL] layer=MAC" in joined
        assert "error=ack timeout" in joined
        assert "failed_step=005-command-transfer state=WAITING_ACK" in joined
        assert "evidence=" in joined and "elapsed_since_workload_start" in joined


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
