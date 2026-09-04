"""단독 RR 자격 orchestrator 계약 시험 (v2, RF·Git·binding 없음).

증명하는 것 (apps/standalone/README.md §7, 결정 20, clone-bootstrap)
  사다리     : P0→P1(+gain)→candidate→P2-17→P3-fault→verification bundle→
               final builder→전체 PASS 시에만 원자적 승격. 단계 실패 시
               기존 active 유지.
  발급       : reference v2가 **실제 검증기**(apps.stage7_profile.load_reference)
               를 통과하고, final builder는 **실물 도구**를 installation digest
               provider 주입으로 실행한다.
  installation audit : git 없는 설치 감사가 재측정 대조로 정직 판정.
  evidence reuse(v2) : 이전 release reference의 파일 결속 대비 rf(파일+심볼)·
               operation byte-identical일 때만 승격, tooling 변경은 bundle
               재실행, 결속 불일치·bundle 실패는 거부.

가짜인 것은 **RF phase 실행뿐**이다 — 주입 phase_runner가 coordinator의
evidence 계약(write_rr_evidence 스키마·index·원본 결속·gain artifact)을
그대로 재현해 실물 final builder의 전 검사를 통과시킨다.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
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
from apps.standalone import (  # noqa: E402
    identity,
    installation_manifest,
    provenance,
    qualify_cmd,
    state,
)

Done = fx.Done
HARDWARE = fx.HARDWARE
ENV_MANIFEST = fx.ENV_MANIFEST
fake_preflight = fx.fake_preflight
write_hardware_toml = fx.write_hardware_toml


def fabricating_phase_runner(context: dict) -> int:
    """coordinator의 evidence 계약을 재현하는 가짜 RR phase 실행기."""
    run_root = Path(context["run_root"])
    phase = context["phase"]
    run_id = context["run_id"]
    evidence = run_root / "evidence"
    research = evidence / "research_ready"
    research.mkdir(parents=True, exist_ok=True)
    (evidence / f"coordinator-{phase}.json").write_text("{}")
    for member in ("MASS-A", "MASS-B", "ROC"):
        (evidence / f"{member}-runtime.json").write_text("{}")
    with open(run_root / "coordinator-console.log", "a", encoding="utf-8") as h:
        h.write("[coordinator] MASS-A 연결됨\n")

    index = {"profile": "RESEARCH_READY_MINIMAL", "entries": {}}
    for tc in qualify_cmd.PHASE_EXPECTED_TCS[phase]:
        payload = {
            "artifact_type": "STAGE7_RESEARCH_READY_EVIDENCE_V1",
            "profile": "RESEARCH_READY_MINIMAL",
            "tc_id": tc,
            "baseline_version": provenance.STAGE7_BASELINE_PIN["version"],
            "baseline_sha256": context["baseline_sha256"],
            "git_commit": None,
            "git_dirty": None,
            "hardware": {"usb_devices": "X"},
            "run_id": run_id,
            "stage7_diag_env": "0",
            "lifecycle_generation": 1,
            "counters": {"retries": 0, "duplicates": 0},
            "status": "PASS",
            "detail": {},
        }
        data = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                          indent=1).encode()
        (research / f"{tc}.json").write_bytes(data)
        index["entries"][tc] = {
            "path": f"evidence/research_ready/{tc}.json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "status": "PASS",
        }
    (research / "index.json").write_bytes(
        json.dumps(index, ensure_ascii=True, sort_keys=True,
                   indent=1).encode())

    if phase == "rr-p1":
        artifact = {
            "artifact_type": "STAGE7_GAIN_ARTIFACT_V1",
            "run_id": run_id,
            "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69,
                                           "ROC": 75},
        }
        artifact_bytes = json.dumps(artifact, ensure_ascii=True,
                                    sort_keys=True, indent=1).encode()
        (evidence / "gain_artifact.json").write_bytes(artifact_bytes)
        gain_input = {
            "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69,
                                           "ROC": 75},
            "source_gain_artifact_sha256": hashlib.sha256(
                artifact_bytes).hexdigest(),
        }
        (evidence / "gain_artifact_input.json").write_bytes(
            json.dumps(gain_input, ensure_ascii=True, sort_keys=True,
                       indent=1).encode())
    return 0


def prepare_environment(tmp) -> state.StatePaths:
    paths, _record = fx.prepare_environment(tmp, qualified=False)
    return paths


def run(paths, tmp, **overrides):
    kwargs = dict(
        hardware_path=Path(tmp) / "hardware.toml",
        repo_root=_REPO,
        phase_runner=fabricating_phase_runner,
        suite_runner=lambda argv, **kw: Done(0),
        preflight_fn=fake_preflight,
        residual_probe=lambda: [],
        environment_manifest=dict(ENV_MANIFEST),
        stamp="20260904-000000",
    )
    kwargs.update(overrides)
    return qualify_cmd.run_qualify(paths, **kwargs)


def _real_verify(paths):
    return installation_manifest.verify_installation(
        Path(_REPO), installation_manifest.load_installation(paths))


# ---------------------------------------------------------------- 단위

def test_local_installation_audit_honest_verdicts():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        record = installation_manifest.load_installation(paths)
        verify = _real_verify(paths)
        audit = qualify_cmd.local_installation_audit(record, verify)
        assert audit["audit_type"] == qualify_cmd.INSTALLATION_AUDIT_TYPE
        assert audit["all_unchanged"] is True
        assert audit["installation_verified"] is True
        assert audit["installation_sha256"] == record["installation_sha256"]
        assert "head_commit" not in audit          # git 결속 없음
        assert audit["files"] and all(f["unchanged"] for f in audit["files"])
        assert all("rf" in record["files"][f["path"]]["domains"]
                   for f in audit["files"])         # rf 영역 파일만
        broken = json.loads(json.dumps(verify))
        broken["pass"] = False
        broken["current_record"]["files"]["VDETER/src/vdeter/stage7/pi_runtime.py"]["sha256"] = "0" * 64
        audit2 = qualify_cmd.local_installation_audit(record, broken)
        assert audit2["all_unchanged"] is False
        assert audit2["installation_verified"] is False


def test_identity_consistency_four_way():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        created = identity.ensure_identity(paths)
        check = identity.check_identity_consistency(paths)
        assert check["pass"], check["errors"]
        assert check["fingerprint"] == created["fingerprint"]
        files = identity.identity_paths(paths)
        files["fingerprint"].write_text("deadbeef0000\n", encoding="utf-8")
        tampered = identity.check_identity_consistency(paths)
        assert not tampered["pass"]
        assert any("불일치" in e for e in tampered["errors"])


def test_promote_candidate_pointer_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        (paths.qualification_releases / "rel-a").mkdir()
        (paths.qualification_releases / "rel-a" / "old.json").write_text(
            "old", encoding="utf-8")
        state.write_json_atomic(paths.active_pointer, {"release": "rel-a"})

        (paths.qualification_candidate
         / qualify_cmd.REFERENCE_BASENAME).write_text("new", encoding="utf-8")
        (paths.qualification_candidate
         / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            "ab" * 32 + "\n", encoding="utf-8")
        result = qualify_cmd.promote_candidate(paths, release_id="rel-b")
        pointer = json.loads(paths.active_pointer.read_bytes())
        assert pointer["release"] == "rel-b"
        assert pointer["previous_release"] == "rel-a"
        assert result["reference_sha256"] == "ab" * 32
        assert (paths.qualification_releases / "rel-a"
                / "old.json").read_text() == "old"
        assert (paths.qualification_releases / "rel-b"
                / qualify_cmd.REFERENCE_BASENAME).read_text() == "new"
        assert not any(paths.qualification_candidate.iterdir())
        (paths.qualification_candidate
         / qualify_cmd.REFERENCE_BASENAME).write_text("x", encoding="utf-8")
        (paths.qualification_candidate
         / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            "cd" * 32 + "\n", encoding="utf-8")
        try:
            qualify_cmd.promote_candidate(paths, release_id="rel-b")
            raise AssertionError("release 중복이 거부되지 않았다")
        except qualify_cmd.QualifyError:
            pass
        assert json.loads(
            paths.active_pointer.read_bytes())["release"] == "rel-b"


# ---------------------------------------------------------------- e2e

def test_qualify_end_to_end_promotes_real_validated_reference_without_git():
    original_run, original_popen = subprocess.run, subprocess.Popen
    calls = []

    def guard(argv, *a, **k):
        calls.append(list(argv))
        return original_run(argv, *a, **k)

    class GuardPopen(original_popen):
        def __init__(self, argv, *a, **k):
            calls.append(list(argv))
            super().__init__(argv, *a, **k)

    subprocess.run, subprocess.Popen = guard, GuardPopen
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths = prepare_environment(tmp)
            record = installation_manifest.load_installation(paths)
            report = run(paths, tmp)
            assert report["status"] == "QUALIFIED", (
                report["errors"], report.get("consistency_problems"))
            assert report["final_status"] == "RESEARCH_READY"
            assert report["consistency_problems"] == []
            assert list(report["phases"]) == list(qualify_cmd.LADDER_PHASES)

            active = qualify_cmd.load_active_reference(paths)
            assert active is not None
            assert active["schema_version"] == 2
            assert active["qualification_method"] == "full_ladder"
            assert "qualified_source_commit" not in active
            inst = active["installation"]
            assert inst["installation_sha256"] == record["installation_sha256"]
            assert inst["rf_manifest_sha256"] == record["domains"]["rf"]["sha256"]
            assert inst["files"] and all(
                "non_binding" not in e["domains"] for e in inst["files"].values())
            assert inst["rf_critical_symbols"] == record["rf_critical_symbols"]
            tc = active["qualification"]["tc_results"]
            assert tc["TC-STAGE7-RR-007"]["status"] == "PASS"
            assert set(active["qualification"]["resolved_config_sha256"]) == set(
                qualify_cmd.LADDER_PHASES)
            assert set(active["qualification"]["evidence_sha256"]) == set(
                qualify_cmd.LADDER_PHASES)
            assert "evidence/research_ready/TC-STAGE7-RR-002.json" in \
                active["qualification"]["evidence_sha256"]["rr-p1"]
            critical = active["standalone_critical"]
            assert critical["installation_sha256"] == record["installation_sha256"]
            assert critical["wiring"] == {"profile": "C1-C6-STANDARD",
                                          "approved": True}
            assert critical["gain"]["endpoint_common_rx_gain_db"]["ROC"] == 75
            assert critical["identity_fingerprint"] == identity.\
                load_identity_manifest(paths)["certificate_fingerprint"]
            assert critical["library_versions"]["cryptography"] == "50.0.0"
            assert "baseline_sha256" not in critical   # 판정값 아님
            assert active["provenance"]["stage7_baseline"]["version"] == "0.9.23"
            assert active["provenance"]["note"]
            assert not any(paths.qualification_candidate.iterdir())
            pointer = json.loads(paths.active_pointer.read_bytes())
            assert pointer["release"] == "qual-20260904-000000"
            release = paths.qualification_releases / pointer["release"]
            final = json.loads((release / "final-result.json").read_bytes())
            assert final["final_status"] == "RESEARCH_READY"
            audit = json.loads((release / "verification"
                                / "frozen-hash-audit.json").read_bytes())
            assert audit["installation_sha256"] == record["installation_sha256"]
            assert "head_commit" not in audit
    finally:
        subprocess.run, subprocess.Popen = original_run, original_popen
    assert not any(os.path.basename(str(c[0])).startswith("git") for c in calls)


def test_phase_run_mode_mapping_is_fixed():
    assert qualify_cmd.PHASE_RUN_MODE == {
        "rr-p0": "RF_OFF_QUALIFICATION",
        "rr-p1": "GAIN_CALIBRATION",
        "rr-p2-17": "OPERATIONAL",
        "rr-p3-fault": "OPERATIONAL",
    }
    assert set(qualify_cmd.PHASE_RUN_MODE) == set(qualify_cmd.LADDER_PHASES)
    assert qualify_cmd.GAIN_CONSUMING_PHASES == ("rr-p2-17", "rr-p3-fault")
    from vdeter.stage7.configuration import RUN_MODES
    assert set(qualify_cmd.PHASE_RUN_MODE.values()) <= set(RUN_MODES)


def test_rr_p1_gain_calibration_config_passes_real_loader():
    from apps.standalone import local_config
    record = fx.repo_installation_record()
    for phase, expected in (("rr-p1", "GAIN_CALIBRATION"),
                            ("rr-p0", "RF_OFF_QUALIFICATION")):
        with tempfile.TemporaryDirectory() as tmp:
            generated = local_config.generate_run_configs(
                repo_root=_REPO, run_root=Path(tmp) / "r", run_id="r",
                rr_phase=phase, hardware=HARDWARE, installation=record,
                installation_verified=True,
                run_mode=qualify_cmd.PHASE_RUN_MODE[phase], gain_input=None,
                environment_manifest=dict(ENV_MANIFEST))
            for endpoint in ("mass-a", "mass-b", "roc"):
                text = (Path(generated["configs_dir"]) / "endpoints"
                        / f"{endpoint}.toml").read_text("utf-8")
                assert f'run_mode = "{expected}"' in text, (phase, endpoint)


def test_ladder_excludes_link19_and_builder_ready_without_rr006():
    assert qualify_cmd.LADDER_PHASES == ("rr-p0", "rr-p1", "rr-p2-17",
                                         "rr-p3-fault")
    builder = qualify_cmd._load_module(
        Path(_REPO) / "VDETER" / "tools" / "stage7_rr_final_builder.py",
        "stage7_rr_final_builder_policy_check")
    assert "TC-STAGE7-RR-006" in builder.DEFERRED_TCS
    assert set(builder.REQUIRED_TCS) == {
        "TC-STAGE7-RR-001", "TC-STAGE7-RR-002", "TC-STAGE7-RR-003",
        "TC-STAGE7-RR-004", "TC-STAGE7-RR-005", "TC-STAGE7-RR-007"}
    produced = {tc for tcs in qualify_cmd.PHASE_EXPECTED_TCS.values()
                for tc in tcs}
    assert produced == set(builder.REQUIRED_TCS)

    phases_called = []

    def recording_runner(context):
        phases_called.append(context["phase"])
        return fabricating_phase_runner(context)

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp, phase_runner=recording_runner)
        assert report["status"] == "QUALIFIED", report["errors"]
        assert phases_called == ["rr-p0", "rr-p1", "rr-p2-17", "rr-p3-fault"]
        reference = qualify_cmd.load_active_reference(paths)
        assert "rr-p2-19" not in reference["qualification"]["phase_run_ids"]
        limitation = reference["limitations"]
        assert limitation["link_19_implementation"] == "present"
        assert limitation["link_19_standalone_qualification"] == "not exercised"
        assert limitation["physical_usb_detach_recovery"] == "not exercised"
        pointer = json.loads(paths.active_pointer.read_bytes())
        final = json.loads((paths.qualification_releases / pointer["release"]
                            / "final-result.json").read_bytes())
        assert final["deferred_follow_up"]["items"]["TC-STAGE7-RR-006"] == "MISSING"


def _coordinator_module():
    return qualify_cmd._load_module(
        Path(_REPO) / "VDETER" / "apps" / "stage7_coordinator_main.py",
        "stage7_coordinator_main_p3_policy_check")


def test_rr_p3_fault_runs_exactly_six_software_subcases():
    import ast
    source = (Path(_REPO) / "VDETER" / "apps"
              / "stage7_coordinator_main.py").read_text("utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "run_rr_p3_fault")
    recorded = []
    constants = set()
    body = function.body[1:] if (
        function.body and isinstance(function.body[0], ast.Expr)
        and isinstance(getattr(function.body[0], "value", None), ast.Constant)
        and isinstance(function.body[0].value.value, str)) else function.body
    executable_nodes = [n for stmt in body for n in ast.walk(stmt)]
    for node in executable_nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            constants.add(node.value)
    for node in executable_nodes:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "record" and node.args
                and isinstance(node.args[0], ast.Constant)):
            recorded.append(node.args[0].value)
    coordinator = _coordinator_module()
    assert sorted(set(recorded)) == sorted(
        coordinator.P3_FAULT_REQUIRED_SUBCASES)
    assert len(coordinator.P3_FAULT_REQUIRED_SUBCASES) == 6
    assert "usb-detach" not in recorded
    joined = " ".join(constants)
    assert "USER-ACTION" not in joined
    assert "STAGE7_P3_USB_DETACH_WAIT_S" not in joined
    assert coordinator.P3_FAULT_EXCLUDED_SUBCASES == ("usb-detach",)


def test_rr_p3_fault_status_six_pass_or_fail():
    coordinator = _coordinator_module()
    names = coordinator.P3_FAULT_REQUIRED_SUBCASES
    all_pass = [{"name": n, "status": "PASS"} for n in names]
    assert coordinator._p3_fault_status(all_pass) == "PASS"
    for index in range(len(names)):
        one_fail = [dict(s) for s in all_pass]
        one_fail[index]["status"] = "FAIL"
        assert coordinator._p3_fault_status(one_fail) == "FAIL", names[index]
    assert coordinator._p3_fault_status(all_pass[:-1]) == "FAIL"


def test_qualify_ladder_applies_run_modes_and_gain_flow():
    contexts = []

    def recording_runner(context):
        contexts.append(dict(context))
        return fabricating_phase_runner(context)

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp, phase_runner=recording_runner)
        assert report["status"] == "QUALIFIED", report["errors"]
        by_phase = {c["phase"]: c for c in contexts}
        assert list(by_phase) == list(qualify_cmd.LADDER_PHASES)
        for phase, expected in qualify_cmd.PHASE_RUN_MODE.items():
            text = (Path(by_phase[phase]["configs_dir"]) / "endpoints"
                    / "mass-a.toml").read_text("utf-8")
            assert f'run_mode = "{expected}"' in text, phase
            # coordinator --baseline-sha256 인자는 커밋 상수 pin.
            assert by_phase[phase]["baseline_sha256"] == \
                provenance.STAGE7_BASELINE_PIN["sha256"]
        assert by_phase["rr-p0"]["gain_artifact"] is None
        assert by_phase["rr-p1"]["gain_artifact"] is None
        p1_gain = (Path(by_phase["rr-p1"]["run_root"]) / "evidence"
                   / "gain_artifact_input.json")
        assert p1_gain.is_file()
        for phase in qualify_cmd.GAIN_CONSUMING_PHASES:
            assert Path(by_phase[phase]["gain_artifact"]) == p1_gain, phase


def test_qualify_phase_failure_preserves_active_pointer():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        (paths.qualification_releases / "rel-prev").mkdir()
        state.write_json_atomic(paths.active_pointer,
                                {"release": "rel-prev"})

        def failing_runner(context):
            if context["phase"] == "rr-p2-17":
                return 1
            return fabricating_phase_runner(context)

        report = run(paths, tmp, phase_runner=failing_runner)
        assert report["status"] == "FAIL"
        assert any("기존 active reference 유지" in e for e in report["errors"])
        assert list(report["phases"]) == ["rr-p0", "rr-p1", "rr-p2-17"]
        assert json.loads(
            paths.active_pointer.read_bytes())["release"] == "rel-prev"


def test_qualify_residual_and_timeout_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp, residual_probe=lambda: ["888 launcher"])
        assert report["status"] == "FAIL"
        assert any("정리 미확인" in e for e in report["errors"])
        assert not paths.active_pointer.exists()

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)

        def timing_out_runner(context):
            if context["phase"] == "rr-p0":
                return {"rc": None, "timed_out": True,
                        "escalation": ["term-group", "kill-group"]}
            raise AssertionError("timeout 후 다음 phase가 시작됐다")

        report = run(paths, tmp, phase_runner=timing_out_runner)
        assert report["status"] == "FAIL"
        assert any("timeout" in e for e in report["errors"])


def test_qualify_refuses_missing_library_versions_before_p0():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        calls = []

        def recording_runner(context):
            calls.append(context["phase"])
            return fabricating_phase_runner(context)

        broken_env = dict(ENV_MANIFEST)
        broken_env["uhd_version"] = None
        try:
            run(paths, tmp, phase_runner=recording_runner,
                environment_manifest=broken_env)
            raise AssertionError("라이브러리 부재가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "uhd" in str(exc)
        assert calls == []


def test_verification_suite_receives_timeout_and_v2_audit():
    seen = []

    def suite_runner(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return Done(0)

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        record = installation_manifest.load_installation(paths)
        out = Path(tmp) / "verification"
        bundle = qualify_cmd.write_verification_bundle(
            Path(_REPO), out, record, verify_report=_real_verify(paths),
            runner=suite_runner)
        assert seen == [qualify_cmd.UNIT_CONTRACT_TIMEOUT_S,
                        qualify_cmd.INTEGRATION_TIMEOUT_S]
        assert bundle["frozen_ok"] and bundle["software_ok"]
        audit = json.loads((out / "frozen-hash-audit.json").read_text("utf-8"))
        assert audit["installation_sha256"] == record["installation_sha256"]
        assert audit["installation_verified"] is True


def test_verification_bundle_deselects_only_governance_from_unit_contract():
    seen = []

    def suite_runner(argv, **kwargs):
        seen.append(list(argv))
        return Done(0)

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        record = installation_manifest.load_installation(paths)
        out = Path(tmp) / "verification"
        qualify_cmd.write_verification_bundle(
            Path(_REPO), out, record, verify_report=_real_verify(paths),
            runner=suite_runner)
        unit_argv, integ_argv = seen
        assert unit_argv.count("--deselect") == len(qualify_cmd.GOVERNANCE_DESELECT)
        assert "--ignore" not in unit_argv
        for nodeid, reason in qualify_cmd.GOVERNANCE_DESELECT:
            assert nodeid in unit_argv and reason
        assert any(str(Path(_REPO) / t) in unit_argv
                   for t in qualify_cmd.UNIT_CONTRACT_TARGETS)
        assert "--deselect" not in integ_argv
        assert "-k" in integ_argv and "stage7" in integ_argv
        rec = json.loads(
            (out / "governance-deselected.json").read_text(encoding="utf-8"))
        assert rec["artifact_type"] == "STAGE7_STANDALONE_GOVERNANCE_DESELECT_V1"
        assert [d["nodeid"] for d in rec["deselected"]] == [
            n for n, _ in qualify_cmd.GOVERNANCE_DESELECT]


def test_run_child_bounded_reaps_on_timeout():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        result = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "child.log",
            timeout_s=1.0, term_wait_s=5.0, kill_wait_s=5.0)
        assert result["timed_out"] is True
        assert result["rc"] is not None
        assert result["escalation"]
        assert result["cleanup_anomaly"] is False
    # on_wait(진행 표시 폴링)이 있어도 timeout 의미는 같다.
    ticks = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        result = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "child.log",
            timeout_s=1.5, term_wait_s=5.0, kill_wait_s=5.0,
            on_wait=lambda: ticks.append(1), wait_poll_s=0.3)
        assert result["timed_out"] is True and ticks


def test_run_child_bounded_reaps_grandchildren_posix():
    import unittest
    if os.name != "posix":
        raise unittest.SkipTest("POSIX 전용 — Pi V2에서 실행")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        orphan_parent = (
            "import subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,"
            "signal.SIG_IGN);time.sleep(60)']);"
            "print(p.pid,flush=True)"
        )
        result = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", orphan_parent],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "a.log",
            timeout_s=30.0, term_wait_s=2.0, kill_wait_s=5.0,
            natural_group_wait_s=1.0)
        assert result["rc"] == 0
        assert result["cleanup_anomaly"] is True
        assert result["group_cleared"] is True
        grandchild_pid = int(
            (Path(tmp) / "a.log").read_text().strip().splitlines()[-1])
        try:
            os.kill(grandchild_pid, 0)
            raise AssertionError(f"손자 {grandchild_pid}가 아직 살아 있다")
        except ProcessLookupError:
            pass


def minimal_valid_reference() -> dict:
    """v1 reference(과거 evidence 조회용)도 검증기를 통과한다 (포인터 결속 시험)."""
    return {
        "artifact_type": "STAGE7_QUALIFICATION_REFERENCE",
        "schema_version": 1,
        "qualified_source_commit": "e" * 40,
        "qualification": {
            "run_id": "rel-test",
            "tc_results": {
                "TC-STAGE7-RR-002": {"status": "PASS"},
                "TC-STAGE7-RR-003": {"status": "PASS"},
                "TC-STAGE7-RR-004": {"status": "PASS"},
            },
        },
        "qualification_critical_manifest": {
            "strict_files": {"apps/x.py": "ab" * 32},
        },
    }


def test_active_pointer_binds_reference_sha_and_v1_stays_readable():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        qualify_cmd.write_reference(paths.qualification_candidate,
                                    minimal_valid_reference())
        qualify_cmd.promote_candidate(paths, release_id="rel-1")
        loaded = qualify_cmd.load_active_reference(paths)
        assert loaded is not None and loaded["schema_version"] == 1
        release = paths.qualification_releases / "rel-1"
        forged = minimal_valid_reference()
        forged["qualification"]["run_id"] = "forged"
        encoded = json.dumps(forged, ensure_ascii=True, sort_keys=True,
                             indent=1).encode()
        (release / qualify_cmd.REFERENCE_BASENAME).write_bytes(encoded)
        (release / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            hashlib.sha256(encoded).hexdigest() + "\n", encoding="utf-8")
        assert qualify_cmd.load_active_reference(paths) is None
        pointer = json.loads(paths.active_pointer.read_bytes())
        pointer["reference_sha256"] = "not-a-sha"
        paths.active_pointer.write_text(json.dumps(pointer), encoding="utf-8")
        assert qualify_cmd.load_active_pointer(paths) is None


def test_qualify_group_uncleared_and_cleanup_anomaly_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp, phase_runner=lambda c: {
            "rc": 0, "timed_out": False,
            "escalation": ["term-group", "group-survived-kill"],
            "group_cleared": False, "cleanup_anomaly": False})
        assert report["status"] == "FAIL"
        assert any("group 소멸 미확인" in e for e in report["errors"])
        assert not paths.active_pointer.exists()
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)

        def anomaly_runner(context):
            fabricating_phase_runner(context)
            return {"rc": 0, "timed_out": False,
                    "escalation": ["post-exit-group-residue"],
                    "group_cleared": True, "cleanup_anomaly": True}

        report = run(paths, tmp, phase_runner=anomaly_runner)
        assert report["status"] == "FAIL"
        assert any("cleanup_anomaly" in e for e in report["errors"])


def test_qualify_refuses_identity_mismatch_and_missing_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        files = identity.identity_paths(paths)
        files["fingerprint"].write_text("deadbeef0000\n", encoding="utf-8")
        try:
            run(paths, tmp)
            raise AssertionError("identity 불일치가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "identity" in str(exc)

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        paths.ensure_var()
        identity.ensure_identity(paths)
        write_hardware_toml(Path(tmp) / "hardware.toml")
        try:
            run(paths, tmp)  # installation manifest 없음
            raise AssertionError("manifest 부재가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "vdeter setup" in str(exc)


def test_qualify_refuses_unapproved_wiring():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        write_hardware_toml(Path(tmp) / "hardware.toml", approved=False)
        try:
            run(paths, tmp)
            raise AssertionError("미승인 배선이 거부되지 않았다")
        except Exception as exc:  # noqa: BLE001 — ConfigGenerationError
            assert "approved" in str(exc)


#: run_id 형식 계약(loader)에 맞는 이전 사다리 stamp/id.
PRIOR_STAMP = "20260901-000000"
PRIOR_ID = "qual-" + PRIOR_STAMP


# ------------------------------- evidence 재사용 (v2 — 이전 release reference 결속)

def _seed_prior_release(paths, tmp, *, mutate=None) -> str:
    """전체 사다리로 release qual-PRIOR를 만들고(RF evidence + v2 reference),
    필요하면 그 reference의 결속 파일 map/심볼을 변형해 '이전 RF evidence가
    다른 설치에서 나왔다'를 모델링한다. active 포인터는 제거한다."""
    report = run(paths, tmp, stamp=PRIOR_STAMP)
    assert report["status"] == "QUALIFIED", report["errors"]
    if mutate is not None:
        fx.rewrite_release_reference(paths, PRIOR_ID, mutate)
    paths.active_pointer.unlink()
    return PRIOR_ID


def _fake_builder(status="RESEARCH_READY"):
    module = types.SimpleNamespace()

    def build(evidence_roots, out_path, *, software_ok, frozen_hash_ok,
              verification_root=None, head_provider=None):
        result = {"final_status": status, "consistency_problems": []}
        Path(out_path).write_text(json.dumps(result), encoding="utf-8")
        return result
    module.build = build
    return module


def _reuse(paths, tmp, prior_release_id, **overrides):
    kwargs = dict(
        prior_release_id=prior_release_id,
        reason="governance bundle-scope fix (qualification tooling only)",
        hardware_path=Path(tmp) / "hardware.toml", repo_root=_REPO,
        suite_runner=lambda argv, **kw: Done(0),
        preflight_fn=fake_preflight, residual_probe=lambda: [],
        environment_manifest=dict(ENV_MANIFEST),
        final_builder_module=_fake_builder())
    kwargs.update(overrides)
    return qualify_cmd.run_qualify_reuse(paths, **kwargs)


def _set_prior_file_sha(target, value="0" * 64):
    def mutate(reference):
        files = reference["installation"]["files"]
        assert target in files, f"{target} reference 결속에 없음"
        files[target]["sha256"] = value
    return mutate


def _bump_prior_symbol(symbol):
    def mutate(reference):
        reference["installation"]["rf_critical_symbols"][
            "apps/standalone/qualify_cmd.py"][symbol] = ["MUT", "MUT"]
    return mutate


def test_evidence_reuse_promotes_on_tooling_only_change():
    """qualify_cmd(governance/bundle 부분 = tooling)만 변경 → RF 재사용 허용."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha("apps/standalone/qualify_cmd.py"))
        report = _reuse(paths, tmp, prior)
        assert report["status"] == "QUALIFIED", report["errors"]
        assert report["qualification_method"] == "evidence_reuse"
        ev = report["reuse_evaluation"]
        assert ev["eligible"] and ev["tooling_changed"]
        assert ev["rf_files_identical"] and ev["rf_symbols_identical"]
        assert ev["operation_identical"]
        active = qualify_cmd.load_active_reference(paths)
        assert active["schema_version"] == 2
        assert active["qualification_method"] == "evidence_reuse"
        assert set(active["binding_domains"]) == {
            "rf_evidence", "operation", "qualification_tool"}
        assert active["binding_domains"]["rf_evidence"]["prior_ladder_id"] == PRIOR_ID
        er = active["evidence_reuse"]
        assert er["reason"] and er["prior_release_id"] == PRIOR_ID
        assert er["rf_symbols_identical"] and er["verification"]["software_ok"]
        assert active["qualification"]["run_id"] == PRIOR_ID  # RF 출처
        assert report["release_id"] == PRIOR_ID + "-reuse"


def test_evidence_reuse_refused_on_rr_p1_run_mode_change():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_bump_prior_symbol("PHASE_RUN_MODE"))
        try:
            _reuse(paths, tmp, prior)
            raise AssertionError("run mode 변경인데 재사용이 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "RF 심볼" in str(exc) or "핵심" in str(exc)
        assert qualify_cmd.load_active_reference(paths) is None


def test_evidence_reuse_refused_on_rf_core_file_change():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha(
                "VDETER/src/vdeter/stage7/pi_runtime.py"))
        try:
            _reuse(paths, tmp, prior)
            raise AssertionError("rf 핵심 변경인데 재사용이 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "핵심" in str(exc) or "rf" in str(exc)
        assert qualify_cmd.load_active_reference(paths) is None


def test_evidence_reuse_refused_on_operation_change():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha("apps/standalone/cli.py"))
        try:
            _reuse(paths, tmp, prior)
            raise AssertionError("operation 변경인데 재사용이 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "operation" in str(exc) or "핵심" in str(exc)
        assert qualify_cmd.load_active_reference(paths) is None


def test_evidence_reuse_qualification_test_change_requires_bundle_only():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha(
                "VDETER/tests/unit/test_multirate_phy.py"))
        report = _reuse(paths, tmp, prior)
        assert report["status"] == "QUALIFIED"
        assert report["reuse_evaluation"]["tooling_changed"] is True
        assert report["reuse_evaluation"]["rf_identical"] is True
        assert report["verification"]["software_ok"] is True
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha(
                "VDETER/tests/unit/test_multirate_phy.py"))
        try:
            _reuse(paths, tmp, prior, suite_runner=lambda argv, **kw: Done(1))
            raise AssertionError("bundle 실패인데 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "software" in str(exc) or "verification" in str(exc)
        assert qualify_cmd.load_active_reference(paths) is None


def test_evidence_reuse_refused_when_config_binding_or_evidence_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(paths, tmp)
        for phase in qualify_cmd.LADDER_PHASES:
            cfg = (paths.runs_dir / f"{PRIOR_ID}-{phase}"
                   / "resolved-configs" / "endpoints")
            for toml_path in cfg.glob("*.toml"):
                toml_path.write_text(
                    f'deployment_manifest_sha256 = "{"1" * 64}"\n',
                    encoding="utf-8")
        try:
            _reuse(paths, tmp, prior)
            raise AssertionError("config 결속 불일치인데 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "결속" in str(exc)
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(paths, tmp)
        target = (paths.runs_dir / f"{PRIOR_ID}-rr-p0" / "evidence"
                  / "coordinator-rr-p0.json")
        target.write_text('{"tampered": true}', encoding="utf-8")
        try:
            _reuse(paths, tmp, prior)
            raise AssertionError("evidence 교체인데 승격됐다")
        except qualify_cmd.QualifyError as exc:
            assert "변조" in str(exc) or "다릅니다" in str(exc)


def test_evidence_reuse_requires_v2_prior_reference():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        qualify_cmd.write_reference(paths.qualification_candidate,
                                    minimal_valid_reference())
        qualify_cmd.promote_candidate(paths, release_id="legacy-v1")
        paths.active_pointer.unlink()
        try:
            _reuse(paths, tmp, "legacy-v1")
            raise AssertionError("v1 reference로 재사용이 진행됐다")
        except qualify_cmd.QualifyError as exc:
            assert "v2" in str(exc)


# ---------------- critical endpoint 스키마 {serial, speed_class}

def test_critical_endpoints_common_function_schema_and_metadata():
    critical, observations = qualify_cmd.critical_endpoints_from_preflight(
        fake_preflight())
    for role, entry in critical.items():
        assert set(entry) == {"serial", "speed_class"}, (role, entry)
        assert entry["speed_class"] in qualify_cmd.CRITICAL_SPEED_CLASSES
        assert set(observations[role]) == {"usb_path", "speed_mbps"}
    assert critical["MASS-A"] == {"serial": "34D03DD", "speed_class": "super"}
    assert observations["MASS-B"] == {"usb_path": "4-2", "speed_mbps": 480}


def test_critical_endpoints_refuse_missing_or_unqualified_speed_class():
    for bad in (None, "other", "unknown", ""):
        pf = fake_preflight()
        pf["checks"]["b210"]["observed_by_role"]["ROC"]["speed_class"] = bad
        try:
            qualify_cmd.critical_endpoints_from_preflight(pf)
            raise AssertionError(f"speed_class={bad!r}가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "ROC" in str(exc) and "speed_class" in str(exc)


def _issue_kwargs(**over):
    base = dict(
        ladder_id="qual-X", installation=fx.repo_installation_record(),
        tc_results={"TC-STAGE7-RR-001": "PASS"},
        endpoints_observed={"MASS-A": {"serial": "34D03DD",
                                       "speed_class": "super"}},
        platform_profile="pi", wiring=HARDWARE["wiring"],
        identity_fingerprint="abc",
        gain={"endpoint_common_rx_gain_db": {"MASS-A": 69}},
        environment_manifest=dict(ENV_MANIFEST),
        environment_manifest_sha256="e" * 64, phase_run_ids={})
    base.update(over)
    return base


def test_issue_reference_refuses_bad_endpoints_or_unapproved_wiring():
    for endpoints in (
        {"MASS-A": {"serial": "34D03DD"}},
        {"MASS-A": {"serial": "34D03DD", "speed_class": "other"}},
        {},
    ):
        try:
            qualify_cmd.issue_reference(
                **_issue_kwargs(endpoints_observed=endpoints))
            raise AssertionError(f"{endpoints}가 발급 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "critical endpoint" in str(exc)
    try:
        qualify_cmd.issue_reference(**_issue_kwargs(
            wiring={"profile": "C1-C6-STANDARD", "approved": False}))
        raise AssertionError("미승인 배선으로 발급됐다")
    except qualify_cmd.QualifyError as exc:
        assert "approved" in str(exc)
    ref = qualify_cmd.issue_reference(**_issue_kwargs())
    assert ref["schema_version"] == 2
    assert ref["standalone_critical"]["endpoints"]["MASS-A"] == {
        "serial": "34D03DD", "speed_class": "super"}
    assert ref["provenance"]["git"] is None   # 기본: git 미읽음
    assert "qualified_source_commit" not in ref


def _compare_critical_against(paths, reference):
    from apps.standalone import run_cmd
    record = installation_manifest.load_installation(paths)
    fingerprint = identity.check_identity_consistency(paths)["fingerprint"]
    return run_cmd.compare_critical(
        reference, installation=record, hardware=HARDWARE,
        preflight=fake_preflight(), identity_fingerprint=fingerprint,
        environment_manifest=dict(ENV_MANIFEST))


def test_full_ladder_and_reuse_references_pass_real_compare_critical():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp)
        assert report["status"] == "QUALIFIED"
        active = qualify_cmd.load_active_reference(paths)
        cmp = _compare_critical_against(paths, active)
        assert cmp["pass"], cmp["mismatches"]
        obs = active["endpoint_observations"]["endpoints"]
        assert obs["MASS-A"] == {"usb_path": "3-1", "speed_mbps": 5000}
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha("apps/standalone/qualify_cmd.py"))
        report = _reuse(paths, tmp, prior)
        assert report["status"] == "QUALIFIED"
        active = qualify_cmd.load_active_reference(paths)
        cmp = _compare_critical_against(paths, active)
        assert cmp["pass"], cmp["mismatches"]


def test_reuse_supersede_keeps_previous_release_and_records_it():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        prior = _seed_prior_release(
            paths, tmp, mutate=_set_prior_file_sha("apps/standalone/qualify_cmd.py"))
        first = _reuse(paths, tmp, prior, release_id=PRIOR_ID + "-r1")
        assert first["status"] == "QUALIFIED"
        old_sha = first["reference"]["sha256"]
        second = _reuse(
            paths, tmp, prior, release_id=PRIOR_ID + "-r2",
            supersedes={"reference_sha256": old_sha, "release": PRIOR_ID + "-r1",
                        "reason": "issuance bug"})
        assert second["status"] == "QUALIFIED"
        assert second["reference"]["previous_release"] == PRIOR_ID + "-r1"
        assert (paths.qualification_releases / (PRIOR_ID + "-r1")
                / qualify_cmd.REFERENCE_BASENAME).is_file()
        assert (paths.qualification_releases / PRIOR_ID
                / qualify_cmd.REFERENCE_BASENAME).is_file()
        active = qualify_cmd.load_active_reference(paths)
        assert active["evidence_reuse"]["supersedes"]["reference_sha256"] == old_sha
        assert active["qualification"]["run_id"] == PRIOR_ID


def test_qualify_progress_hooks_do_not_change_outcome():
    """진행 표시(QualifyProgress) 유무와 무관하게 사다리 결과·reference 동일."""
    from apps.standalone import progress as pg
    results = []
    for with_progress in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            paths = prepare_environment(tmp)
            lines = []
            progress = None
            reporter = None
            if with_progress:
                reporter = pg.ProgressReporter(lines.append, tty=False,
                                               ticker=False)
                progress = pg.QualifyProgress(reporter)
            report = run(paths, tmp, progress=progress)
            if reporter is not None:
                reporter.close()
                text = "\n".join(lines)
                assert "RR-P0 1/4" in text and "RR-P3-FAULT 4/4" in text
                assert "Software suite 1/2" in text and "Final builder" in text
                assert "QUALIFIED · active reference promoted" in text
                assert "estimated" in text
                assert "\r" not in text and "\x1b" not in text
            active = qualify_cmd.load_active_reference(paths)
            results.append((report["status"], report["final_status"],
                            active["installation"]["installation_sha256"],
                            active["qualification"]["tc_results"]))
    assert results[0] == results[1]


if __name__ == "__main__":
    import unittest as _unittest
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            try:
                value()
            except _unittest.SkipTest as reason:
                print(f"  {name} SKIP ({reason})")
                continue
            print(f"  {name} OK")
    print("PASS: standalone qualify 계약 (v2)")
