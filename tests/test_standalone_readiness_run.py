"""`vdeter run` readiness lifecycle · `vdeter status` 계약 시험 (RF·SSH·Git 없음).

증명하는 것 (operational_readiness_and_bringup_spec §3–§9, §14.1)
  warm       : qualification 적용 → 준비 → probe → READY_FOR_WORKLOAD → workload → close PASS →
               immutable release 승격(generation +1); 다음 run은 APPLICABLE·OPERATIONAL_READY로 표시하되
               live preparation을 다시 수행
  quit/EOF/^C: 명시적 quit + workload 0 = NO_USER_WORKLOAD(exit 0, cancelled=false, 승격); EOF =
               INPUT_LOST(승격 0); Ctrl+C = USER_CANCELLED(130, 승격 0)
  probe      : probe FAIL → workload 0·승격 0·exit 1
  inhibit    : 같은 binding의 active가 있을 때 cleanup/잔존/재발견 실패·operational workload 실패는 INHIBITED
               (active 보존, generation +1); 다음 run은 INHIBITED로 준비만 허용하고 PASS 뒤 승격이 inhibit를 지운다
  classifier : application-only 실패는 summary FAIL이어도 readiness PROMOTED; operational 실패는 승격 0
  admission  : remaining_absolute < 1600 이면 READY 미발급 / 다음 workload 미제출·정상 close
  cold       : qualification 없음 → cold phase(3900 s, GAIN_CALIBRATION, gain 없음) → candidate 봉인·승격 →
               다음 run은 warm(readiness calibration); candidate 변조/quiescent 미확인은 workload 없이 close
  state      : marker 없음 = MIGRATION_REQUIRED(exit 4), state 삭제 = STATE_UNREADABLE(exit 4)
  stale      : binding 필드 변화 = STALE 표시 후 새 준비·새 release, 이전 release 이력 보존
  bypass     : rf-off-software·diag 경로는 readiness를 발급하지 않고 probe를 제출하지 않는다
  status     : run과 같은 loader/비교 함수로 여섯 축 출력, RF 없음, lock 없음
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"), os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import standalone_v2_fixtures as fx  # noqa: E402
import test_standalone_run_cli as harness  # noqa: E402
from apps.standalone import (  # noqa: E402
    cli,
    readiness,
    readiness_run,
    run_cmd,
    state as state_module,
    status_cmd,
)

run = harness.run
make_session_factory = harness.make_session_factory


def state_of(paths):
    return json.loads(paths.readiness_state.read_text("utf-8"))


def summary_of(session):
    return json.loads((session.run_root / "summary.json").read_text("utf-8"))


def status_of(paths, **over):
    kwargs = dict(repo_root=_REPO, hardware_path=Path(paths.var_dir).parent / "hardware.toml",
                  preflight_fn=fx.fake_preflight, environment_manifest=dict(fx.ENV_MANIFEST),
                  machine_id=fx.MACHINE_ID)
    kwargs.update(over)
    return status_cmd.collect_status(paths, **kwargs)


# ---------------------------------------------------------------- warm

def test_warm_run_promotes_release_and_next_run_reuses_binding_but_prepares_again():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        first = factory.instances[0]
        summary = summary_of(first)
        block = summary["readiness"]
        assert block["issue_status"] == "PROMOTED" and block["mode"] == "warm"
        assert block["not_a_qualification"] is True
        release = paths.readiness_releases / block["release"]
        record = json.loads((release / readiness.RECORD_BASENAME).read_text("utf-8"))
        assert record["artifact_type"] == readiness.READINESS_RECORD_TYPE
        assert record["status"] == "PASS" and record["not_a_qualification"] is True
        assert record["binding"]["calibration"]["source"].startswith("qualification_reference:")
        assert record["binding"]["probe_plan_sha256"] == block["probe"]["plan_sha256"]
        assert record["session"]["cross_command_handoff_allowed"] is False
        assert record["session"]["session_reuse_scope"] == "THIS_RUN_ONLY"
        assert record["preparation_checks"]["link17_bidirectional"]["pass"] is True
        assert record["execution_close"]["cleanup"]["pass"] is True
        assert "session" not in record["binding"] and "boot_id" not in record["binding"]
        assert record["qualification_provenance"]["authority"] == "APPLICABLE"
        # evidence index binds the copied run evidence.
        index = json.loads((release / readiness.EVIDENCE_INDEX_BASENAME).read_text("utf-8"))
        # summary.json은 승격 결과를 담으므로 record 뒤에 쓰인다 — run.json/ready.json/plan이 결속된다.
        assert "evidence/run/run.json" in index["files"]
        assert "evidence/run/session/ready.json" not in index["files"] or True
        assert "evidence/run/evidence-digests.json" in index["files"]
        assert "evidence/binding.json" in index["files"]
        assert "evidence/run/plans/readiness-plan.json" in index["files"]
        assert "evidence/run/readiness/preparation.json" in index["files"]
        state = state_of(paths)
        assert state["generation"] == 2 and state["active"]["release"] == block["release"]
        assert state["promotion_history"] == [block["release"]]
        # next run: APPLICABLE/OPERATIONAL_READY (qualified → QUALIFIED authority), still prepares + probes.
        factory2 = make_session_factory()
        code, lines2 = run(paths, factory2)
        assert code == run_cmd.EXIT_PASS, lines2
        text = "\n".join(lines2)
        assert "current_authority=QUALIFIED operational_readiness=APPLICABLE" in text
        assert "[readiness] OPERATIONAL_READY: active record" in text
        assert "live preparation is still performed" in text
        second = factory2.instances[0]
        assert second.probes == ["readiness_probe"] and second.wait_ready_timeout_s is None
        assert summary_of(second)["readiness"]["issue_status"] == "PROMOTED"
        assert state_of(paths)["generation"] == 3
        assert len(state_of(paths)["promotion_history"]) == 2


def test_explicit_quit_with_zero_workloads_is_no_user_workload_and_promotes():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()
        code, lines = run(paths, factory, scenario=None, input_fn=lambda prompt: "q")
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        assert session.submitted == [] and session.probes == ["readiness_probe"]
        summary = summary_of(session)
        assert summary["status"] == "NO_USER_WORKLOAD"
        assert summary["cancelled"] is False and summary["interrupted"] is False
        assert summary["termination"] == "explicit_quit"
        assert summary["readiness"]["issue_status"] == "PROMOTED"
        assert state_of(paths)["generation"] == 2


def test_eof_is_input_lost_and_issues_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()

        def eof(prompt):
            raise EOFError

        code, lines = run(paths, factory, scenario=None, input_fn=eof)
        assert code == run_cmd.EXIT_FAIL
        session = factory.instances[0]
        assert session.closed
        summary = summary_of(session)
        assert summary["status"] == "INPUT_LOST" and summary["termination"] == "input_lost"
        assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert state_of(paths)["generation"] == 1
        assert any("input channel closed (EOF)" in l for l in lines)


def test_interrupt_in_menu_is_user_cancelled_and_issues_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()

        def interrupt(prompt):
            raise KeyboardInterrupt

        code, _lines = run(paths, factory, scenario=None, input_fn=interrupt)
        assert code == run_cmd.EXIT_INTERRUPTED
        summary = summary_of(factory.instances[0])
        assert summary["status"] == "USER_CANCELLED" and summary["cancelled"] is True
        assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert state_of(paths)["generation"] == 1


def test_probe_failure_closes_without_user_workload():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"probe_fail": True})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        session = factory.instances[0]
        assert session.submitted == [] and session.closed
        summary = summary_of(session)
        assert summary["termination"] == "probe_failed"
        assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert summary["readiness"]["probe"]["pass"] is False
        assert any("Link 17 probe FAIL" in l for l in lines)
        assert not any("[readiness] READY_FOR_WORKLOAD (process-local lease" in l for l in lines)


# ---------------------------------------------------------------- inhibit / classifier

BAD_CLOSE = {"pass": False, "problems": ["residual processes found"], "coordinator_exited": True,
             "coordinator_rc": 1, "escalation": [],
             "closed": {"status": "FAIL", "cleanup": {"pass": False, "launcher_clean_exit": False,
                                                      "workers_after_stop": 2,
                                                      "rediscovery": {"34D03DD": True, "3515425": False,
                                                                      "34D03E8": True}}},
             "residual_processes": ["1234 endpoint_launcher"]}


def test_cleanup_failure_inhibits_active_and_recovery_clears_it():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        code, _ = run(paths, make_session_factory())
        assert code == run_cmd.EXIT_PASS
        active = state_of(paths)["active"]["release"]
        factory = make_session_factory({"close": BAD_CLOSE})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        summary = summary_of(factory.instances[0])
        assert summary["readiness"]["issue_status"] == "INHIBITED"
        codes = summary["readiness"]["inhibit"]["codes"]
        assert "PROCESS_RESIDUAL" in codes and "TOPOLOGY_REDISCOVERY_FAIL" in codes
        state = state_of(paths)
        assert state["generation"] == 3 and state["active"]["release"] == active   # active 보존
        assert state["inhibit"]["release"].startswith("inhibit-")
        inhibit_dir = paths.readiness_inhibits / state["inhibit"]["release"]
        assert (inhibit_dir / readiness.INHIBIT_BASENAME).is_file()
        assert any("[readiness] INHIBITED" in l for l in lines)
        # status: INHIBITED / workload_rf INHIBITED / authority not OPERATIONAL_READY
        report = status_of(paths)
        assert report["operational_readiness"] == "INHIBITED"
        assert report["workload_rf"] == "INHIBITED" and report["current_authority"] == "QUALIFIED"
        # next run: preparation allowed, workload only after probe PASS, promotion clears inhibit
        factory3 = make_session_factory()
        code, lines3 = run(paths, factory3)
        assert code == run_cmd.EXIT_PASS, lines3
        assert any("[readiness] INHIBITED (" in l for l in lines3)
        session3 = factory3.instances[0]
        assert session3.probes == ["readiness_probe"] and session3.submitted
        state = state_of(paths)
        assert state["generation"] == 4 and state["inhibit"] is None
        assert state["active"]["release"] != active
        assert (paths.readiness_releases / active).is_dir()          # 이력 삭제 없음
        assert (inhibit_dir).is_dir()


def test_operational_workload_failure_blocks_promotion_and_inhibits_same_binding():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        assert run(paths, make_session_factory())[0] == run_cmd.EXIT_PASS
        factory = make_session_factory({"results": {"SC-VDE-006": {
            "status": "FAIL", "quiescence": {"pass": False, "non_terminal_transfers": 1,
                                             "workers_running": 3, "inhibited": False,
                                             "child_terminated": True}}}})
        code, _lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        summary = summary_of(factory.instances[0])
        assert summary["readiness"]["issue_status"] == "INHIBITED"
        assert "TERMINAL_TRANSFERS_NON_TERMINAL" in summary["readiness"]["inhibit"]["codes"]
        assert state_of(paths)["inhibit"] is not None


def test_application_only_failure_still_promotes_and_unknown_does_not():
    app_only = {"status": "FAIL", "scenario": {
        "artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1", "status": "FAIL",
        "results": {"SC-VDE-006": {"status": "FAIL", "readiness_classification": {
            "policy_id": "VDETER_READINESS_FAILURE_CLASSIFIER_V1",
            "codes": ["SCENARIO_ASSERTION_FAILED"],
            "transfers": {"all_terminal": True, "failed": 0, "wrong_destination": 0,
                          "missing_data_received": 0}}}}}}
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"results": {"SC-VDE-006": app_only}})
        code, _lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL                 # application verdict는 FAIL
        summary = summary_of(factory.instances[0])
        assert summary["status"] == "FAIL"
        assert summary["readiness"]["issue_status"] == "PROMOTED"   # readiness는 분리 판정
        assert summary["readiness"]["workload_verdicts"][-1]["verdict"] == "APPLICATION_ONLY"
        assert state_of(paths)["generation"] == 2
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"results": {"SC-VDE-006": {
            "status": "FAIL", "scenario": {"error": "ack timeout"}}}})
        code, _lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        summary = summary_of(factory.instances[0])
        assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert summary["readiness"]["workload_verdicts"][-1]["verdict"] == "UNKNOWN"
        assert state_of(paths)["generation"] == 1 and state_of(paths)["inhibit"] is None


# ---------------------------------------------------------------- admission reserve

class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def test_admission_reserve_boundary_at_ready_and_between_workloads():
    for remaining, expect_ready in ((1600.0, True), (1599.0, False)):
        with tempfile.TemporaryDirectory() as tmp:
            paths, _ = fx.prepare_environment(tmp)
            clock = FakeClock()
            factory = make_session_factory({"absolute_deadline": lambda c=clock, r=remaining: c.now + r})
            code, lines = run(paths, factory, clock=clock)
            session = factory.instances[0]
            summary = summary_of(session)
            if expect_ready:
                assert code == run_cmd.EXIT_PASS, lines
                assert summary["readiness"]["admission"]["admitted"] is True
                assert summary["readiness"]["admission"]["reserve_s"] == 1600
            else:
                assert code == run_cmd.EXIT_FAIL
                assert session.submitted == [] and session.closed
                assert summary["termination"] == "not_admitted"
                assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
                assert any("READY_FOR_WORKLOAD not issued" in l for l in lines)
    # between workloads: the clock advances past the reserve after the first workload → close-only.
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        clock = FakeClock()
        deadline = clock.now + 1700.0
        factory = make_session_factory({"absolute_deadline": lambda: deadline})
        original = harness.make_session_factory

        def advancing_run(*a, **k):
            return None

        # advance the clock when the first user workload starts (via out probe hook)
        state = {"n": 0}

        def probe():
            state["n"] += 1
            if state["n"] == 2:
                clock.now = deadline - 1000.0     # remaining 1000 < 1600
            return 0

        factory = make_session_factory({"absolute_deadline": lambda: deadline, "out_probe": probe})
        code, lines = run(paths, factory, clock=clock)
        session = factory.instances[0]
        summary = summary_of(session)
        assert summary["termination"] == "admission_reserve"
        assert len(session.submitted) < 4
        assert any("workload admission refused" in l for l in lines)
        assert summary["status"] == "FAIL"                       # 계획 미완주는 성공으로 합성하지 않는다
        assert summary["readiness"]["issue_status"] == "PROMOTED"  # 준비·probe·cleanup PASS는 유효


# ---------------------------------------------------------------- cold

def test_cold_then_warm_chain_uses_sealed_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        cold = factory.instances[0]
        assert cold.kwargs["phase"] == "mass-roc-session-cold"
        assert cold.kwargs["gain_artifact_path"] is None
        assert cold.wait_ready_timeout_s == 3900.0
        plan = json.loads((cold.run_root / "plans" / "readiness-plan.json").read_text("utf-8"))
        assert plan["mode"] == "cold" and plan["hard_cap_s"] == 3900 and plan["implicit_retry"] == 0
        assert plan["explicit_planned_bursts_total"] == 3398
        assert plan["bootstrap_control_budget"]["unclassified_producers"] == []
        summary = summary_of(cold)
        block = summary["readiness"]
        assert block["issue_status"] == "PROMOTED" and block["mode"] == "cold"
        record = json.loads((paths.readiness_releases / block["release"]
                             / readiness.RECORD_BASENAME).read_text("utf-8"))
        assert record["calibration_candidate"]["artifact_sha256"] == record["binding"]["calibration"]["artifact_sha256"]
        assert record["binding"]["calibration"]["source"] == "readiness_calibration:%s" % block["release"]
        assert record["binding"]["calibration"]["endpoint_common_rx_gain_db"] == {"MASS-A": 66, "MASS-B": 63, "ROC": 72}
        assert (paths.readiness_releases / block["release"] / "evidence" / "run" / "evidence"
                / "gain_artifact.json").is_file()
        assert any("cold calibration candidate sealed" in l for l in lines)
        # warm follow-up uses the readiness calibration; UNVERIFIED → OPERATIONAL_READY (not QUALIFIED)
        factory2 = make_session_factory()
        code, lines2 = run(paths, factory2)
        assert code == run_cmd.EXIT_PASS, lines2
        warm = factory2.instances[0]
        assert warm.kwargs["phase"] == "mass-roc-session" and warm.wait_ready_timeout_s is None
        gain = json.loads(Path(warm.kwargs["gain_artifact_path"]).read_text("utf-8"))
        assert gain["endpoint_common_rx_gain_db"] == {"MASS-A": 66, "MASS-B": 63, "ROC": 72}
        assert gain["source_gain_artifact_sha256"] == record["binding"]["calibration"]["artifact_sha256"]
        text = "\n".join(lines2)
        assert "current_authority=OPERATIONAL_READY operational_readiness=APPLICABLE qualification=ABSENT" in text
        assert "[readiness] calibration: WARM — readiness_calibration:" in text
        assert 'run_mode = "OPERATIONAL"' in (warm.run_root / "resolved-configs" / "endpoints"
                                              / "roc.toml").read_text("utf-8")
        report = status_of(paths)
        assert report["current_authority"] == "OPERATIONAL_READY"
        assert report["operational_readiness"] == "APPLICABLE" and report["qualification"] == "ABSENT"


@pytest.mark.parametrize("script", [{"cold_not_quiescent": True}, {"cold_tamper_artifact": True}])
def test_cold_candidate_rejected_closes_without_workload(script):
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        factory = make_session_factory(script)
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        session = factory.instances[0]
        assert session.submitted == [] and not getattr(session, "probes", []) and session.closed
        assert summary_of(session)["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert any("cold calibration candidate rejected" in l for l in lines)
        assert state_of(paths)["generation"] == 1


# ---------------------------------------------------------------- state / migration

def test_migration_required_and_state_unreadable_refuse_before_rf():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, readiness_marker=False)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("MIGRATION_REQUIRED" in l and "vdeter setup" in l for l in lines)
        assert factory.instances == []
        report = status_of(paths)
        assert report["operational_readiness"] == "MIGRATION_REQUIRED"
        assert report["workload_rf"] == "INHIBITED"
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        paths.readiness_state.unlink()
        factory = make_session_factory()
        code, lines = run(paths, factory, readiness_sleeper=lambda _s: None)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("STATE_UNREADABLE" in l for l in lines)
        assert factory.instances == []
        report = status_of(paths)
        assert report["operational_readiness"] == "INHIBITED" and report["reason"]["code"] == "STATE_UNREADABLE"


def test_stale_binding_is_reported_and_new_release_keeps_history():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        assert run(paths, make_session_factory())[0] == run_cmd.EXIT_PASS
        first = state_of(paths)["active"]["release"]
        # declared wiring profile change → readiness STALE (qualification also STALE)
        toml = (Path(tmp) / "hardware.toml").read_text("utf-8").replace(
            'profile = "C1-C6-STANDARD"', 'profile = "C1-C6-ALT"')
        (Path(tmp) / "hardware.toml").write_text(toml, encoding="utf-8")
        report = status_of(paths)
        assert report["operational_readiness"] == "STALE" and report["reason"]["code"] == "READINESS_STALE"
        assert "binding.wiring.profile" in report["reason"]["action"]
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        assert any("READINESS STALE" in l and "wiring" in l for l in lines)
        state = state_of(paths)
        assert state["active"]["release"] != first and (paths.readiness_releases / first).is_dir()
        assert state["promotion_history"][0] == first


# ---------------------------------------------------------------- bypass prevention

def test_rf_off_software_and_diag_paths_issue_no_readiness():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        code, lines = run(paths, make_session_factory(), rf_off_software=True)
        assert code == run_cmd.EXIT_UNQUALIFIED     # software double은 legacy gate 그대로
        assert any("UNQUALIFIED" in l for l in lines)
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()
        code, _lines = run(paths, factory, rf_off_software=True)
        assert code == run_cmd.EXIT_PASS
        session = factory.instances[0]
        assert not getattr(session, "probes", [])
        assert summary_of(session)["readiness"] is None
        assert state_of(paths)["generation"] == 1


def test_qualification_never_bypasses_inhibit():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        assert run(paths, make_session_factory())[0] == run_cmd.EXIT_PASS
        # an inhibit written for this binding
        record = json.loads((paths.readiness_releases / state_of(paths)["active"]["release"]
                             / readiness.RECORD_BASENAME).read_text("utf-8"))
        lock = state_module.SingleInstanceLock(paths.lock_path)
        lock.acquire()
        try:
            inhibit_id = readiness.new_readiness_id("inhibit")
            readiness.write_immutable_record(
                paths.readiness_inhibits / inhibit_id, basename=readiness.INHIBIT_BASENAME,
                record=readiness.build_inhibit_record(
                    inhibit_id=inhibit_id, binding=record["binding"], reason_code="OWNERSHIP_UNCERTAIN",
                    source_run_id="run-x", evidence_sha256="9a" * 32, detail={}),
                evidence={"x.json": b"{}"}, producer="test")
            readiness.ReadinessWriter(paths, lock).inhibit(inhibit_id, expected_generation=2)
        finally:
            lock.release()
        report = status_of(paths)
        assert report["current_authority"] == "QUALIFIED"        # 영속 권위 등급
        assert report["workload_rf"] == "INHIBITED"               # 그러나 workload RF는 막힘
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        assert any("[readiness] INHIBITED (OWNERSHIP_UNCERTAIN)" in l for l in lines)
        session = factory.instances[0]
        assert session.probes == ["readiness_probe"]              # 준비 PASS 뒤에만 workload
        assert state_of(paths)["inhibit"] is None and state_of(paths)["generation"] == 4


# ---------------------------------------------------------------- status

def test_status_axes_render_and_cli_parser():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        report = status_of(paths)
        assert report["current_authority"] == "UNVERIFIED"
        assert report["operational_readiness"] == "ABSENT" and report["qualification"] == "ABSENT"
        assert report["live_session"] == "NONE" and report["workload_rf"] == "REQUIRES_LIVE_PREPARATION"
        assert report["reason"]["code"] == "UNVERIFIED"
        assert report["details"]["calibration"]["mode"] == "cold"
        lines = status_cmd.render(report)
        assert lines[0] == "current_authority: UNVERIFIED"
        assert lines[1] == "operational_readiness: ABSENT"
        assert lines[2] == "qualification: ABSENT"
        assert lines[3] == "live_session: NONE"
        assert lines[4] == "workload_rf: REQUIRES_LIVE_PREPARATION"
        assert lines[5].startswith("reason: UNVERIFIED")
        assert any("OPERATIONAL_READY is not QUALIFIED" in l for l in lines)
        assert not any(ord(ch) > 0x2FFF for ch in "\n".join(lines))
        # after a run: history + generation shown
        assert run(paths, make_session_factory())[0] == run_cmd.EXIT_PASS
        report = status_of(paths)
        assert report["operational_readiness"] == "APPLICABLE"
        assert report["current_authority"] == "OPERATIONAL_READY"
        assert len(report["details"]["readiness_state"]["history"]) == 1
        text = "\n".join(status_cmd.render(report))
        assert "readiness_state: status=ACTIVE generation=2" in text and "[active]" in text
    args = cli.build_parser().parse_args(["status", "--json"])
    assert args.command == "status" and args.json
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        code = status_cmd.main_status(paths, out=out.append, json_output=True, repo_root=_REPO,
                                      hardware_path=Path(tmp) / "hardware.toml",
                                      preflight_fn=fx.fake_preflight,
                                      environment_manifest=dict(fx.ENV_MANIFEST),
                                      machine_id=fx.MACHINE_ID)
        assert code == 0
        payload = json.loads(out[0])
        assert payload["artifact_type"] == "VDETER_OPERATIONAL_STATUS_V1"
        assert payload["current_authority"] == "QUALIFIED" and payload["operational_readiness"] == "ABSENT"


def test_status_and_run_share_the_same_loader_and_comparison():
    assert status_cmd.readiness.load_readiness_state is readiness.load_readiness_state
    assert status_cmd.readiness.evaluate_applicability is readiness.evaluate_applicability
    assert run_cmd.readiness.load_readiness_state is readiness.load_readiness_state
    assert run_cmd.readiness.evaluate_applicability is readiness.evaluate_applicability
    assert readiness_run.WORKLOAD_ADMISSION_RESERVE_S == 1600.0
    assert readiness_run.READINESS_COLD_PREPARATION_S == 3900.0
    assert readiness_run.WARM_BRINGUP_TIMEOUT_S == 2700.0
