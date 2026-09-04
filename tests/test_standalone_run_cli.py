"""Phase I W6 — `vdeter run`/`results`/CLI 계약 시험 (RF·SSH 없음).

증명하는 것 (apps/standalone/README.md §1·§8·§10·§11, 결정 5·13·14·16·17·21)
  게이트 연결  : deploy manifest 실검증 → active 포인터 적재 → identity 정합 →
                 필수 preflight → critical 대조(4종 버전 포함) → UNQUALIFIED
                 차단이 **실제 실행 경로에서** 연결돼 있다 (이관 조건 종결)
  세션 실행    : 세션 1회 수립 + workload N개, resolved config가 reference
                 gain으로 OPERATIONAL 생성, identity root가 세션에 전달
  재시도       : host-time 일시 실패로 분류된 bring-up만 새 run-id로 1회,
                 비일시 신호는 재시도 없음, 두 시도 evidence 보존
  출력         : 영어, trace 발생 즉시 workload 기준 타임스탬프, RESULT 줄
  results/CLI  : 목록·요약 재열람, 하위 명령 파싱, qualify 확인 프롬프트

가짜인 것은 coordinator 세션뿐이다 — config 생성은 실제 template·loader,
reference는 실제 검증기(load_reference)·포인터 결속 경로를 그대로 탄다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (
    cli,
    deploy_manifest,
    identity,
    local_session,
    qualify_cmd,
    results_cmd,
    run_cmd,
    state,
)

BINDING = {
    "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
    "source_head": "f" * 40,
    "source_branch": "feature/phase-i",
    "source_worktree_dirty": False,
    "baseline_version": "0.9.23",
    "baseline_sha256": "2a" * 32,
    "frozen_files": {"src/vdeter/config/models.py": "ab" * 32},
}

ENV_MANIFEST = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": "unit-test",
    "usb_devices": "X",
    "numpy_version": "2.5.2",
    "cryptography_version": "50.0.0",
    "uhd_version": "4.6",
    "gnuradio_version": "3.10",
}

OBSERVED = {
    "MASS-A": {"serial": "34D03DD", "usb_path": "3-1",
               "speed_mbps": 5000, "speed_class": "super"},
    "MASS-B": {"serial": "3515425", "usb_path": "4-2",
               "speed_mbps": 480, "speed_class": "high"},
    "ROC": {"serial": "34D03E8", "usb_path": "5-1",
            "speed_mbps": 5000, "speed_class": "super"},
}
USB_ALL = {info["serial"]: {"usb_path": info["usb_path"],
                            "speed_mbps": info["speed_mbps"]}
           for info in OBSERVED.values()}

GAIN = {
    "artifact_sha256": "ab" * 32,
    "artifact_input_sha256": "cd" * 32,
    "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69, "ROC": 75},
    "source_run_id": "qual-x-rr-p1",
}


def fake_preflight():
    return {"pass": True, "failures": [],
            "checks": {"b210": {"pass": True,
                                "observed_by_role": dict(OBSERVED)}}}


def write_hardware_toml(path: Path) -> None:
    path.write_text("\n".join([
        "schema_version = 1", 'platform_profile = "pi"',
        '[endpoints."MASS-A"]', 'serial = "34D03DD"',
        '[endpoints."MASS-B"]', 'serial = "3515425"',
        'external_power_asset = "AT2024T"',
        '[endpoints."ROC"]', 'serial = "34D03E8"',
        "[wiring]", 'profile = "C1-C6-STANDARD"', "approved = true", "",
    ]), encoding="utf-8")


def prepare_environment(tmp, *, qualified=True) -> tuple:
    """setup+qualify가 끝난 기계 상태를 재현한다 (RF 없이)."""
    paths = state.StatePaths(tmp)
    paths.root = Path(_REPO)  # template·vdeter 실물
    paths.ensure_var()
    ident = identity.ensure_identity(paths)
    write_hardware_toml(Path(tmp) / "hardware.toml")
    binding_path = Path(tmp) / "standalone-binding.json"
    binding_path.write_text(json.dumps(BINDING), encoding="utf-8")
    written = deploy_manifest.write_manifest(
        _REPO, paths.var_dir / "deploy-manifest.json")
    if qualified:
        reference = qualify_cmd.issue_reference(
            ladder_id="qual-test", binding=BINDING,
            deploy_sha256=written["sha256"],
            deploy_files={e["path"]: e["sha256"]
                          for e in written["manifest"]["files"]},
            tc_results={"TC-STAGE7-RR-002": "PASS",
                        "TC-STAGE7-RR-003": "PASS",
                        "TC-STAGE7-RR-004": "PASS"},
            endpoints_observed={role: {"serial": i["serial"],
                                       "speed_class": i["speed_class"]}
                                for role, i in OBSERVED.items()},
            platform_profile="pi",
            identity_fingerprint=ident["fingerprint"],
            gain=GAIN, environment_manifest=ENV_MANIFEST,
            environment_manifest_sha256="ef" * 32,
            phase_run_ids={}, final_status="RESEARCH_READY")
        qualify_cmd.write_reference(paths.qualification_candidate, reference)
        qualify_cmd.promote_candidate(paths, release_id="qual-test")
    return paths, binding_path


# ---------------------------------------------------------------- 가짜 세션

def make_session_factory(script=None):
    """script: {"bringup_fail_console": [text|None per instance],
               "results": {scenario: dict}}"""
    script = script or {}
    instances = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.run_root = Path(kwargs["run_root"])
            self.console_log_path = self.run_root / "coordinator-console.log"
            self.started = False
            self.closed = False
            self.escalated = False
            self.submitted = []
            instances.append(self)

        def start(self):
            self.started = True

        def wait_ready(self, *, timeout_s=None, on_stage=None):
            if on_stage:
                on_stage("acquire_host_time COMPLETED")
            index = len(instances) - 1
            interrupts = script.get("bringup_interrupt") or []
            if index < len(interrupts) and interrupts[index]:
                raise KeyboardInterrupt  # bring-up 도중 Ctrl+C/SIGTERM
            fails = script.get("bringup_fail_console") or []
            text = fails[index] if index < len(fails) else None
            if text:
                self.console_log_path.write_text(text, encoding="utf-8")
                raise local_session.LocalSessionError(
                    "bring-up 중 coordinator가 종료했습니다 (rc=1)")
            return {"run_id": self.kwargs["run_id"]}

        def submit_workload(self, scenario_id, certificate_case="normal", *,
                            on_settle=None):
            self.submitted.append((scenario_id, certificate_case))
            return "%s-%s-%03d" % (self.kwargs["run_id"],
                                   scenario_id.lower(), len(self.submitted))

        def wait_result(self, workload_id, *, timeout_s=None, on_trace=None):
            if on_trace:
                on_trace({"event": "submit", "sender": "ROC",
                          "receiver": "MASS-A", "payload_format": "route",
                          "payload_len": 353})
                on_trace({"event": "scenario_result", "status": "PASS",
                          "scenario": self.submitted[-1][0],
                          "steps": [["s1", "PASS"], ["s2", "PASS"]]})
            scenario = self.submitted[-1][0]
            override = (script.get("results") or {}).get(scenario)
            result = {"status": "PASS", "quiescence": {"pass": True},
                      "wall_clock_s": 87.3, "scenario": {}}
            if override:
                result.update(override)
            return result

        def close(self):
            if script.get("close_raises_interrupt") and not getattr(
                    self, "_close_interrupted", False):
                self._close_interrupted = True
                raise KeyboardInterrupt  # close 중 두 번째 Ctrl+C
            self.closed = True
            return {"pass": True, "problems": [],
                    "closed": {"status": "PASS", "cleanup": {"pass": True}},
                    "residual_processes": [], "coordinator_rc": 0}

        def escalate(self):
            self.escalated = True

        def finalize(self):
            return self.close()

    FakeSession.instances = instances
    return FakeSession


def run(paths, binding_path, factory, **overrides):
    lines = []
    kwargs = dict(
        scenario="all", certificate_case=None, session_factory=factory,
        preflight_fn=fake_preflight, usb_probe=lambda: dict(USB_ALL),
        environment_manifest=dict(ENV_MANIFEST), repo_root=_REPO,
        binding_path=binding_path,
        hardware_path=Path(paths.var_dir).parent / "hardware.toml",
        out=lines.append, input_fn=lambda prompt: "q",
    )
    kwargs.update(overrides)
    code = run_cmd.execute(paths, **kwargs)
    return code, lines


# ---------------------------------------------------------------- 순수 함수

def test_render_trace_english_with_workload_stamp():
    line = run_cmd.render_trace_event(
        {"event": "submit", "sender": "ROC", "receiver": "MASS-A",
         "payload_format": "route", "payload_len": 353}, 4.2)
    assert line.startswith("  [ 0:04] ROC -> MASS-A")
    assert "submit" in line and "353 B" in line
    verdict = run_cmd.render_trace_event(
        {"event": "scenario_result", "status": "FAIL", "scenario": "SC-VDE-005",
         "steps": [["a", "PASS"], ["b", "FAIL"]]}, 80.0)
    assert "[ 1:20]" in verdict and "verdict   FAIL (2 steps)  failed: b" in verdict
    assert run_cmd.render_trace_event({"event": "other"}, 1.0) is None
    assert run_cmd.stamp(1275) == "[21:15]"


def test_compare_critical_detects_each_mismatch():
    reference = {"standalone_critical": {
        "deploy_manifest_sha256": "aa" * 32, "platform_profile": "pi",
        "endpoints": {role: {"serial": i["serial"],
                             "speed_class": i["speed_class"]}
                      for role, i in OBSERVED.items()},
        "identity_fingerprint": "abc123", "library_versions": {
            "gnuradio": "3.10", "uhd": "4.6", "numpy": "2.5.2",
            "cryptography": "50.0.0"},
        "gain": GAIN}}
    base = dict(deploy_sha256="aa" * 32, hardware={"platform_profile": "pi"},
                preflight=fake_preflight(), identity_fingerprint="abc123",
                environment_manifest=dict(ENV_MANIFEST))
    assert run_cmd.compare_critical(reference, **base)["pass"]

    def mismatch(**changes):
        kwargs = dict(base)
        kwargs.update(changes)
        return run_cmd.compare_critical(reference, **kwargs)["mismatches"]

    assert any("deploy manifest" in m for m in mismatch(deploy_sha256="bb" * 32))
    assert any("platform_profile" in m
               for m in mismatch(hardware={"platform_profile": "generic-pc"}))
    assert any("identity fingerprint" in m
               for m in mismatch(identity_fingerprint="zzz"))
    env = dict(ENV_MANIFEST)
    env["uhd_version"] = "4.7"
    assert any("uhd version" in m for m in mismatch(environment_manifest=env))
    swapped = fake_preflight()
    swapped["checks"]["b210"]["observed_by_role"]["MASS-B"] = {
        "serial": "3515425", "speed_class": "super"}
    assert any("USB speed class" in m for m in mismatch(preflight=swapped))
    no_gain = json.loads(json.dumps(reference))
    no_gain["standalone_critical"]["gain"] = {}
    assert any("gain" in m for m in run_cmd.compare_critical(
        no_gain, **base)["mismatches"])


def test_classify_bringup_failure_conservative():
    ok_summary = {"closed": {"cleanup": {"pass": True}},
                  "residual_processes": []}
    serials = [i["serial"] for i in OBSERVED.values()]
    good = run_cmd.classify_bringup_failure(
        "[coordinator] phase 실패: MASS-B host affine fit FAIL",
        ok_summary, USB_ALL, serials)
    assert good["transient_host_time"] is True

    def not_transient(console=None, summary=None, usb=None):
        return run_cmd.classify_bringup_failure(
            console if console is not None else
            "MASS-B host affine fit FAIL",
            summary if summary is not None else ok_summary,
            usb if usb is not None else USB_ALL, serials)

    assert not not_transient(console="SERVICE_BINDING_REJECTED: x"
                             )["transient_host_time"]
    assert not not_transient(console="some other error")["transient_host_time"]
    assert not not_transient(summary={"closed": {"cleanup": {"pass": False}},
                                      "residual_processes": []}
                             )["transient_host_time"]
    assert not not_transient(summary={"closed": {"cleanup": {"pass": True}},
                                      "residual_processes": ["x"]}
                             )["transient_host_time"]
    partial = dict(USB_ALL)
    partial.pop("3515425")
    reasons = not_transient(usb=partial)["reasons"]
    assert any("not rediscovered" in r for r in reasons)


def test_plan_for():
    assert len(run_cmd.plan_for("all", None)) == 4
    assert run_cmd.plan_for("SC-VDE-001", "both") == [
        ("SC-VDE-001", "normal"), ("SC-VDE-001", "forged")]
    assert run_cmd.plan_for("SC-VDE-005", None) == [("SC-VDE-005", "normal")]
    assert run_cmd.plan_for(None, None) == []
    try:
        run_cmd.plan_for("SC-VDE-999", None)
        raise AssertionError("unknown scenario accepted")
    except run_cmd.RunRefusal as exc:
        assert exc.code == run_cmd.EXIT_CONFIG


# ---------------------------------------------------------------- 게이트 연결

def test_run_refuses_unqualified_hardware_and_critical_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp, qualified=False)
        factory = make_session_factory()
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("UNQUALIFIED" in line and "vdeter qualify" in line
                   for line in lines)
        assert factory.instances == []  # RF 세션은 만들어지지 않았다

    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory()

        def bad_preflight():
            report = fake_preflight()
            report["pass"] = False
            report["checks"]["b210"]["pass"] = False
            report["failures"] = ["MASS-B: USB 속도 등급 불일치"]
            return report

        code, lines = run(paths, binding_path, factory,
                          preflight_fn=bad_preflight)
        assert code == run_cmd.EXIT_HARDWARE
        assert factory.instances == []

        # critical 대조: 라이브러리 버전 하나만 달라도 UNQUALIFIED.
        env = dict(ENV_MANIFEST)
        env["uhd_version"] = "4.7"
        code, lines = run(paths, binding_path, factory,
                          environment_manifest=env)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("uhd version" in line for line in lines)
        assert factory.instances == []

        # deploy tree 변조 → manifest 검증 실패 → UNQUALIFIED (실검증 연결).
        manifest, _ = deploy_manifest.load_manifest(
            paths.var_dir / "deploy-manifest.json")
        manifest["files"][0]["sha256"] = "00" * 32
        (paths.var_dir / "deploy-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("Deployment tree differs" in line for line in lines)


def test_run_noninteractive_all_pass_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory()
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        # 세션 계약: identity root 전달·서비스 phase·reference gain 파일.
        assert session.kwargs["identity_root"] == paths.identity_dir
        assert session.kwargs["phase"] == "mass-roc-session"
        gain = json.loads(Path(session.kwargs["gain_artifact_path"]
                               ).read_text("utf-8"))
        assert gain["endpoint_common_rx_gain_db"]["ROC"] == 75
        assert gain["source_gain_artifact_sha256"] == GAIN["artifact_sha256"]
        # resolved config는 실제 loader를 통과했고 OPERATIONAL로 생성됐다.
        mass_b = (session.run_root / "resolved-configs" / "endpoints"
                  / "mass-b.toml").read_text("utf-8")
        assert 'run_mode = "OPERATIONAL"' in mass_b
        git_state = json.loads((session.run_root / "resolved-configs"
                                / "git_state.json").read_text("utf-8"))
        assert git_state["rr_phase"] == "rr-p2-17"
        assert session.submitted == list(run_cmd.DEFAULT_BATCH_PLAN)
        assert session.closed

        text = "\n".join(lines)
        assert "QUALIFIED (reference" in text
        assert "SESSION READY" in text
        assert "[ 0:00] ROC -> MASS-A" in text  # 실시간 trace 줄
        assert "result: PASS · 87.3 s · quiescence OK" in text
        assert "Closed — cleanup PASS, residual processes 0" in text
        result_lines = [l for l in lines if l.startswith("RESULT ")]
        assert result_lines[-1].startswith(
            "RESULT overall=PASS scenarios=4/4 run-id=")
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["status"] == "PASS"
        assert summary["scenarios_passed"] == 4
        assert (session.run_root / "run.json").is_file()
        # 영어 출력 계약 — 한글이 없다.
        assert not any(ord(ch) > 0x2FFF for ch in text)


def test_run_retry_only_for_transient_host_time_failure():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "[coordinator] phase 실패: MASS-B host affine fit FAIL", None]})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_PASS, lines
        assert len(factory.instances) == 2
        first, second = factory.instances
        assert first.closed and first.run_root != second.run_root
        assert first.run_root.is_dir()  # 두 시도 evidence 모두 보존
        assert any("Retrying once with a new run-id" in l for l in lines)
        summary = json.loads((second.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert [a["bringup"] for a in summary["attempts"]] == ["FAILED", "READY"]

    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "SERVICE_BINDING_REJECTED: baseline SHA-256 불일치"]})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 1  # 재시도 없음
        assert any(l.startswith("No retry:") for l in lines)
        assert any(l.startswith("RESULT overall=FAIL scenarios=0/4")
                   for l in lines)

    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": ["MASS-A host affine fit FAIL",
                                     "MASS-A host affine fit FAIL"]})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 2  # 최대 1회 재시도 후 종료


def test_run_interactive_menu_and_workload_failure_stops_batch():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory()
        answers = iter(["2", "1", "forged", "q"])
        code, lines = run(paths, binding_path, factory, scenario=None,
                          input_fn=lambda prompt: next(answers))
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        assert session.submitted == [("SC-VDE-005", "normal"),
                                     ("SC-VDE-001", "forged")]
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interactive"] is True and summary["status"] == "PASS"
        assert not any(l.startswith("RESULT ") for l in lines)  # 대화형은 없음

    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({"results": {
            "SC-VDE-006": {"status": "FAIL",
                           "scenario": {"error": "ack timeout"}}}})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_FAIL
        session = factory.instances[0]
        assert session.submitted == [("SC-VDE-005", "normal"),
                                     ("SC-VDE-006", "normal")]  # 실패 후 중단
        assert any("reason: ack timeout" in l for l in lines)
        assert any(l.startswith("RESULT overall=FAIL scenarios=1/4")
                   for l in lines)


def test_run_interrupt_closes_session_and_exits_130():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory()

        def interrupting_input(prompt):
            raise KeyboardInterrupt

        code, lines = run(paths, binding_path, factory, scenario=None,
                          input_fn=interrupting_input)
        assert code == run_cmd.EXIT_INTERRUPTED
        session = factory.instances[0]
        assert session.closed  # shutdown→cleanup 경로를 탔다
        assert any("Interrupt — closing session" in l for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted"] is True


def test_run_interrupt_during_bringup_closes_and_exits_130():
    """bring-up(start/wait_ready) 중 Ctrl+C — 전파 대신 정상 close, 재시도
    없음, cleanup·잔존·B210 반환 결과를 evidence에 기록, exit 130."""
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({"bringup_interrupt": [True]})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_INTERRUPTED
        assert len(factory.instances) == 1  # 인터럽트는 재시도하지 않는다
        session = factory.instances[0]
        assert session.closed  # shutdown 마커→cleanup 경로를 실제로 탔다
        assert any("Interrupt during bring-up" in l for l in lines)
        assert any("Closed — cleanup PASS, residual processes 0" in l
                   for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted"] is True
        assert summary["interrupted_during"] == "bring-up"
        assert summary["status"] == "FAIL" and summary["scenarios_run"] == 0
        assert summary["close"]["pass"] is True  # cleanup 결과가 evidence에
        assert summary["attempts"][0]["bringup"] == "INTERRUPTED"
        assert any(l.startswith("RESULT overall=FAIL scenarios=0/4")
                   and "interrupted during bring-up" in l for l in lines)

    # close 중 두 번째 Ctrl+C → 즉시 에스컬레이션(TERM/KILL) + finalize 판정.
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        factory = make_session_factory({"bringup_interrupt": [True],
                                        "close_raises_interrupt": True})
        code, lines = run(paths, binding_path, factory)
        assert code == run_cmd.EXIT_INTERRUPTED
        session = factory.instances[0]
        assert session.escalated
        assert any("Second interrupt" in l for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted_during"] == "bring-up"


# ---------------------------------------------------------------- results·CLI

def test_results_list_and_show():
    with tempfile.TemporaryDirectory() as tmp:
        paths, binding_path = prepare_environment(tmp)
        lines = []
        assert results_cmd.main_results(paths, out=lines.append) == 0
        assert any("No runs recorded" in l for l in lines)

        factory = make_session_factory()
        run(paths, binding_path, factory)
        run_id = factory.instances[0].kwargs["run_id"]
        lines = []
        results_cmd.main_results(paths, out=lines.append)
        assert any(run_id in l and "PASS" in l and "4/4" in l for l in lines)
        lines = []
        assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
        text = "\n".join(lines)
        assert "status:      PASS" in text and "SC-VDE-001   forged" in text
        assert results_cmd.main_results(paths, "run-nope",
                                        out=lines.append) == 2


def test_cli_parser_and_dispatch_without_rf():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "all", "--case", "both", "--no-retry"])
    assert args.command == "run" and args.scenario == "all"
    assert args.case == "both" and args.no_retry
    assert parser.parse_args(["run"]).scenario is None
    assert parser.parse_args(["setup", "--new-identity"]).new_identity
    assert parser.parse_args(["results", "run-x"]).run_id == "run-x"

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        lines = []
        assert cli.main(["results"], paths=paths, out=lines.append) == 0
        # qualify는 확인 프롬프트에서 'yes'가 아니면 RF 없이 중단한다.
        lines = []
        code = cli.main(["qualify"], paths=paths, out=lines.append,
                        input_fn=lambda prompt: "no")
        assert code == cli.EXIT_CONFIG
        assert any("aborted (no RF action taken)" in l for l in lines)


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase I W6 standalone run/results/cli 계약")
