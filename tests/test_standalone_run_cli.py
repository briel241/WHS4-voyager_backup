"""`vdeter run`/`results`/CLI 계약 시험 (v2, RF·SSH·Git 없음).

증명하는 것 (apps/standalone/README.md §1·§8·§10·§11, 결정 5·13·14·16·17·21)
  게이트 연결  : installation manifest 적재·재측정 → active 포인터 적재 →
                 identity 정합 → 필수 preflight → critical 대조(digest·4종
                 버전 포함) → UNQUALIFIED 차단이 실제 실행 경로에서 연결
  세션 실행    : 세션 1회 수립 + workload N개, resolved config가 reference
                 gain으로 OPERATIONAL 생성, identity root가 세션에 전달
  재시도       : host-time 일시 실패로 분류된 bring-up만 새 run-id로 1회,
                 비일시 신호는 재시도 없음, 두 시도 evidence 보존
  출력         : 영어, bring-up 진행 표시(non-TTY 줄 로그), 시나리오 구간은
                 [n/N] 순서 + 상세 추적(퍼센트 바 없음), RESULT 줄;
                 표시 on/off와 무관하게 판정·evidence 동일
  results/CLI  : 목록·요약 재열람, 하위 명령 파싱(--no-progress), qualify 확인

가짜인 것은 coordinator 세션뿐이다 — config 생성은 실제 template·loader,
reference는 실제 발급기·검증기·포인터 결속 경로를 그대로 탄다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
    cli,
    installation_manifest,
    local_session,
    qualify_cmd,
    results_cmd,
    run_cmd,
    state,
)

ENV_MANIFEST = fx.ENV_MANIFEST
OBSERVED = fx.OBSERVED
USB_ALL = fx.USB_ALL
GAIN = fx.GAIN
fake_preflight = fx.fake_preflight


# ---------------------------------------------------------------- 가짜 세션

def make_session_factory(script=None):
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
            self.run_root.mkdir(parents=True, exist_ok=True)
            self.console_log_path.write_text(
                "[coordinator] MASS-A 연결됨\n[coordinator] MASS-B 연결됨\n"
                "[coordinator] ROC 연결됨\n"
                "[coordinator] prepare/configure/arm_rx COMPLETED\n",
                encoding="utf-8")

        def wait_ready(self, *, timeout_s=None, on_stage=None):
            if on_stage:
                on_stage("acquire_host_time COMPLETED")
            index = len(instances) - 1
            interrupts = script.get("bringup_interrupt") or []
            if index < len(interrupts) and interrupts[index]:
                raise KeyboardInterrupt
            fails = script.get("bringup_fail_console") or []
            text = fails[index] if index < len(fails) else None
            if text:
                self.console_log_path.write_text(text, encoding="utf-8")
                raise local_session.LocalSessionError(
                    "bring-up 중 coordinator가 종료했습니다 (rc=1)")
            with open(self.console_log_path, "a", encoding="utf-8") as handle:
                handle.write("[coordinator] acquire_host_time COMPLETED\n"
                             "[coordinator] validate_rf_time PRE COMPLETED\n"
                             "[coordinator] validate_rf_time POST COMPLETED\n"
                             "[coordinator] publish_service_start COMPLETED — RUNNING\n")
            return {"run_id": self.kwargs["run_id"]}

        def submit_workload(self, scenario_id, certificate_case="normal", *,
                            on_settle=None):
            self.submitted.append((scenario_id, certificate_case))
            return "%s-%s-%03d" % (self.kwargs["run_id"],
                                   scenario_id.lower(), len(self.submitted))

        def wait_result(self, workload_id, *, timeout_s=None, on_trace=None,
                        on_poll=None):
            if on_poll:
                on_poll()
            if on_trace:
                on_trace({"event": "submit", "step": "s1", "sender": "ROC",
                          "receiver": "MASS-A", "transfer_id": "8f73c821-x",
                          "payload_format": "route", "payload_len": 353,
                          "payload_sha256": "9b" * 32,
                          "source_station": 222222222,
                          "destination_station": 111111111,
                          "ack_required": True, "t_utc": "2026-09-05T00:00:00Z"})
                on_trace({"event": "scenario_result", "status": "PASS",
                          "scenario": self.submitted[-1][0],
                          "steps": [{"name": "s1", "status": "PASS",
                                     "transfer_id": "8f73c821-x",
                                     "state": "COMPLETED",
                                     "received_checks": {"byte_exact": True}}],
                          "counters": {"transfers_completed": 1,
                                       "transfers_failed": 0,
                                       "wrong_destination": 0,
                                       "mismatched_ack": 0, "duplicates": 0}})
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
                raise KeyboardInterrupt
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


def run(paths, factory, **overrides):
    lines = []
    kwargs = dict(
        scenario="all", certificate_case=None, session_factory=factory,
        preflight_fn=fake_preflight, usb_probe=lambda: dict(USB_ALL),
        environment_manifest=dict(ENV_MANIFEST), repo_root=_REPO,
        hardware_path=Path(paths.var_dir).parent / "hardware.toml",
        out=lines.append, input_fn=lambda prompt: "q",
        progress_ticker=False,
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
    record = fx.repo_installation_record()
    digests = installation_manifest.digests_of(record)
    reference = {"schema_version": 2, "standalone_critical": {
        **digests, "platform_profile": "pi",
        "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
        "endpoints": fx.endpoints_critical(),
        "identity_fingerprint": "abc123", "library_versions": {
            "gnuradio": "3.10", "uhd": "4.6", "numpy": "2.5.2",
            "cryptography": "50.0.0"},
        "gain": GAIN}}
    base = dict(installation=record, hardware=fx.HARDWARE,
                preflight=fake_preflight(), identity_fingerprint="abc123",
                environment_manifest=dict(ENV_MANIFEST))
    assert run_cmd.compare_critical(reference, **base)["pass"]

    def mismatch(**changes):
        kwargs = dict(base)
        kwargs.update(changes)
        return run_cmd.compare_critical(reference, **kwargs)["mismatches"]

    # 영역별 digest 불일치는 어느 영역이 바뀌었는지와 조치를 말한다.
    for domain, needle in (("rf", "vdeter qualify"), ("operation", "qualify"),
                           ("tooling", "verification bundle")):
        changed = json.loads(json.dumps(record))
        changed["domains"][domain]["sha256"] = "0" * 64
        changed["installation_sha256"] = "1" * 64
        found = mismatch(installation=changed)
        assert any(domain in m and needle in m for m in found), (domain, found)
    hardware = json.loads(json.dumps(fx.HARDWARE))
    hardware["platform_profile"] = "generic-pc"
    assert any("platform_profile" in m for m in mismatch(hardware=hardware))
    hardware = json.loads(json.dumps(fx.HARDWARE))
    hardware["wiring"]["profile"] = "OTHER"
    assert any("wiring profile" in m for m in mismatch(hardware=hardware))
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
    # v1(legacy git 결속) reference는 v2 run에서 거부된다.
    legacy = json.loads(json.dumps(reference))
    legacy["schema_version"] = 1
    assert any("schema v1" in m for m in run_cmd.compare_critical(
        legacy, **base)["mismatches"])


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
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("UNQUALIFIED" in line and "vdeter qualify" in line
                   for line in lines)
        assert factory.instances == []  # RF 세션은 만들어지지 않았다

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()

        def bad_preflight():
            report = fake_preflight()
            report["pass"] = False
            report["checks"]["b210"]["pass"] = False
            report["failures"] = ["MASS-B: USB 속도 등급 불일치"]
            return report

        code, lines = run(paths, factory, preflight_fn=bad_preflight)
        assert code == run_cmd.EXIT_HARDWARE
        assert factory.instances == []

        env = dict(ENV_MANIFEST)
        env["uhd_version"] = "4.7"
        code, lines = run(paths, factory, environment_manifest=env)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("uhd version" in line for line in lines)
        assert factory.instances == []

        # installation manifest 변조 → 적재 거부 → UNQUALIFIED (실검증 연결).
        path = (installation_manifest.installation_dir(paths)
                / "installation-manifest.json")
        payload = json.loads(path.read_text("utf-8"))
        payload["files"]["apps/standalone/run_cmd.py"]["sha256"] = "00" * 32
        path.write_text(json.dumps(payload), encoding="utf-8")
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("tampered" in line for line in lines)
        assert factory.instances == []


def test_run_noninteractive_all_pass_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        paths, record = fx.prepare_environment(tmp)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        assert session.kwargs["identity_root"] == paths.identity_dir
        assert session.kwargs["phase"] == "mass-roc-session"
        # coordinator baseline 인자는 커밋 상수 pin (Markdown 미독).
        from apps.standalone import provenance
        assert session.kwargs["baseline_sha256"] == provenance.STAGE7_BASELINE_PIN["sha256"]
        gain = json.loads(Path(session.kwargs["gain_artifact_path"]
                               ).read_text("utf-8"))
        assert gain["endpoint_common_rx_gain_db"]["ROC"] == 75
        assert gain["source_gain_artifact_sha256"] == GAIN["artifact_sha256"]
        mass_b = (session.run_root / "resolved-configs" / "endpoints"
                  / "mass-b.toml").read_text("utf-8")
        assert 'run_mode = "OPERATIONAL"' in mass_b
        assert f'deployment_manifest_sha256 = "{record["installation_sha256"]}"' in mass_b
        git_state = json.loads((session.run_root / "resolved-configs"
                                / "git_state.json").read_text("utf-8"))
        assert git_state["rr_phase"] == "rr-p2-17"
        assert git_state["installation_sha256"] == record["installation_sha256"]
        assert (session.run_root / "resolved-configs"
                / "installation-state.json").is_file()
        assert session.submitted == list(run_cmd.DEFAULT_BATCH_PLAN)
        assert session.closed

        text = "\n".join(lines)
        assert "QUALIFIED (reference" in text
        assert "SESSION READY" in text
        # bring-up 진행 표시(non-TTY 줄 로그)와 시나리오 순서·추적 출력.
        assert "[BRINGUP]" in text and "Endpoint startup" in text
        assert "Service session READY" in text
        assert "[1/4] SC-VDE-005" in text and "[4/4] SC-VDE-001 forged" in text
        assert "[STEP] s1  submit" in text and "[STEP 1/1] s1  PASS" in text
        assert "result: PASS · 87.3 s · quiescence OK" in text
        assert "Closed — cleanup PASS, residual processes 0" in text
        # non-TTY: carriage return·ANSI 오염 없음, 시나리오 구간 진행바 없음.
        assert "\r" not in text and "\x1b" not in text
        after_ready = text.split("SESSION READY", 1)[1]
        assert "[BRINGUP]" not in after_ready and "█" not in after_ready
        result_lines = [l for l in lines if l.startswith("RESULT ")]
        assert result_lines[-1].startswith(
            "RESULT overall=PASS scenarios=4/4 run-id=")
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["status"] == "PASS"
        assert summary["scenarios_passed"] == 4
        run_json = json.loads((session.run_root / "run.json").read_text("utf-8"))
        assert run_json["installation_sha256"] == record["installation_sha256"]
        assert "deploy_manifest_sha256" not in run_json
        # presentation evidence (additive) — progress·console 로그 보존.
        assert (session.run_root / "presentation" / "progress.log").is_file()
        assert (session.run_root / "presentation" / "console.log").is_file()
        # 영어 출력 계약 — 한글이 없다 (·, … 같은 기호는 허용).
        assert not any(ord(ch) > 0x2FFF for ch in text)


def test_progress_toggle_keeps_judgement_and_evidence_identical():
    """--no-progress 여부와 무관하게 summary·run.json 판정이 같다."""
    summaries = []
    for progress in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            paths, _ = fx.prepare_environment(tmp)
            factory = make_session_factory()
            code, lines = run(paths, factory, progress=progress)
            assert code == run_cmd.EXIT_PASS
            session = factory.instances[0]
            summary = json.loads((session.run_root / "summary.json"
                                  ).read_text("utf-8"))
            summaries.append({
                "status": summary["status"],
                "scenarios_passed": summary["scenarios_passed"],
                "scenarios_run": summary["scenarios_run"],
                "scenarios_planned": summary["scenarios_planned"],
                "close_pass": summary["close"]["pass"],
                # run-id 유래 workload_id·시각은 실행마다 다르므로 판정 필드만.
                "workloads": [(w["scenario_id"], w["certificate_case"],
                               w["status"], w["quiescence"]["pass"])
                              for w in summary["workloads"]]})
            text = "\n".join(lines)
            assert "\r" not in text and "\x1b" not in text
            assert "[1/4] SC-VDE-005" in text
    assert summaries[0] == summaries[1]


def test_run_retry_only_for_transient_host_time_failure():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "[coordinator] phase 실패: MASS-B host affine fit FAIL", None]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        assert len(factory.instances) == 2
        first, second = factory.instances
        assert first.closed and first.run_root != second.run_root
        assert first.run_root.is_dir()
        assert any("Retrying once with a new run-id" in l for l in lines)
        assert any(l.strip() == "attempt 2/2" for l in lines)
        text = "\n".join(lines)
        # bring-up 실패 상세 블록.
        assert "layer=ORCHESTRATION" in text
        assert "reason=HOST_AFFINE_FIT_FAILED" in text
        assert "retry_eligible=true" in text and "attempt=1/2" in text
        assert "evidence=%s" % first.run_root in text
        summary = json.loads((second.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert [a["bringup"] for a in summary["attempts"]] == ["FAILED", "READY"]

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "SERVICE_BINDING_REJECTED: baseline SHA-256 불일치"]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 1
        assert any(l.startswith("No retry:") for l in lines)
        assert any("retry_eligible=false" in l for l in lines)
        assert any(l.startswith("RESULT overall=FAIL scenarios=0/4")
                   for l in lines)

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": ["MASS-A host affine fit FAIL",
                                     "MASS-A host affine fit FAIL"]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 2


def test_run_interactive_menu_and_workload_failure_stops_batch():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()
        answers = iter(["2", "1", "forged", "q"])
        code, lines = run(paths, factory, scenario=None,
                          input_fn=lambda prompt: next(answers))
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        assert session.submitted == [("SC-VDE-005", "normal"),
                                     ("SC-VDE-001", "forged")]
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interactive"] is True and summary["status"] == "PASS"
        assert not any(l.startswith("RESULT ") for l in lines)
        text = "\n".join(lines)
        assert "[1/4]" not in text  # 개별 시나리오에는 [n/N] 없음
        assert "SC-VDE-001 forged" in text

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"results": {
            "SC-VDE-006": {"status": "FAIL",
                           "scenario": {"error": "ack timeout"}}}})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        session = factory.instances[0]
        assert session.submitted == [("SC-VDE-005", "normal"),
                                     ("SC-VDE-006", "normal")]
        text = "\n".join(lines)
        assert "reason: ack timeout" in text
        assert "[FAIL] layer=MAC" in text and "error=ack timeout" in text
        assert "evidence=" in text
        assert any(l.startswith("RESULT overall=FAIL scenarios=1/4")
                   for l in lines)


def test_run_interrupt_closes_session_and_exits_130():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory()

        def interrupting_input(prompt):
            raise KeyboardInterrupt

        code, lines = run(paths, factory, scenario=None,
                          input_fn=interrupting_input)
        assert code == run_cmd.EXIT_INTERRUPTED
        session = factory.instances[0]
        assert session.closed
        assert any("Interrupt — closing session" in l for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted"] is True


def test_run_interrupt_during_bringup_closes_and_exits_130():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"bringup_interrupt": [True]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_INTERRUPTED
        assert len(factory.instances) == 1
        session = factory.instances[0]
        assert session.closed
        assert any("Interrupt during bring-up" in l for l in lines)
        assert any("INTERRUPTED" in l and "[BRINGUP]" in l for l in lines)
        assert any("Closed — cleanup PASS, residual processes 0" in l
                   for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted"] is True
        assert summary["interrupted_during"] == "bring-up"
        assert summary["status"] == "FAIL" and summary["scenarios_run"] == 0
        assert summary["close"]["pass"] is True
        assert summary["attempts"][0]["bringup"] == "INTERRUPTED"
        assert any(l.startswith("RESULT overall=FAIL scenarios=0/4")
                   and "interrupted during bring-up" in l for l in lines)

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({"bringup_interrupt": [True],
                                        "close_raises_interrupt": True})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_INTERRUPTED
        session = factory.instances[0]
        assert session.escalated
        assert any("Second interrupt" in l for l in lines)
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["interrupted_during"] == "bring-up"


def test_run_never_invokes_git():
    """대상 기계 실행 경로는 git 명령을 호출하지 않는다."""
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
            paths, _ = fx.prepare_environment(tmp)
            code, _lines = run(paths, make_session_factory())
            assert code == run_cmd.EXIT_PASS
    finally:
        subprocess.run, subprocess.Popen = original_run, original_popen
    assert not any(os.path.basename(str(c[0])).startswith("git") for c in calls)


# ---------------------------------------------------------------- results·CLI

def test_results_list_and_show():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        lines = []
        assert results_cmd.main_results(paths, out=lines.append) == 0
        assert any("No runs recorded" in l for l in lines)

        factory = make_session_factory()
        run(paths, factory)
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
    assert args.case == "both" and args.no_retry and not args.no_progress
    assert parser.parse_args(["run", "--no-progress"]).no_progress
    assert parser.parse_args(["run"]).scenario is None
    assert parser.parse_args(["setup", "--new-identity"]).new_identity
    assert parser.parse_args(["setup", "--no-launcher", "--no-progress"]).no_launcher
    assert parser.parse_args(["qualify", "--yes", "--no-progress"]).no_progress
    assert parser.parse_args(["results", "run-x"]).run_id == "run-x"
    for argv in (["--help"], ["setup", "--help"], ["qualify", "--help"],
                 ["run", "--help"], ["results", "--help"]):
        try:
            parser.parse_args(argv)
            raise AssertionError("--help should exit")
        except SystemExit as exc:
            assert exc.code == 0

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        lines = []
        assert cli.main(["results"], paths=paths, out=lines.append) == 0
        lines = []
        code = cli.main(["qualify"], paths=paths, out=lines.append,
                        input_fn=lambda prompt: "no")
        assert code == cli.EXIT_CONFIG
        assert any("aborted (no RF action taken)" in l for l in lines)
        # 확인 프롬프트는 [y/N] 관례: 빈 입력·n·no 는 중단, y·yes 만 진행.
        from unittest import mock
        from apps.standalone import qualify_cmd
        for answer in ("", "n", "N", "NO"):
            lines = []
            code = cli.main(["qualify"], paths=paths, out=lines.append,
                            input_fn=lambda prompt, _a=answer: _a)
            assert code == cli.EXIT_CONFIG
            assert any("aborted (no RF action taken)" in l for l in lines)
        for answer in ("y", "Y", "yes"):
            prompts, lines = [], []

            def _input(prompt, _a=answer):
                prompts.append(prompt)
                return _a
            with mock.patch.object(
                    qualify_cmd, "run_qualify",
                    side_effect=qualify_cmd.QualifyError("stub (no RF)")):
                code = cli.main(["qualify"], paths=paths, out=lines.append,
                                input_fn=_input)
            assert prompts == ["proceed with RF qualification? [y/N] > "]
            assert code == cli.EXIT_CONFIG
            assert not any("aborted" in l for l in lines)
            assert any("qualify refused: stub (no RF)" in l for l in lines)


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: standalone run/results/cli 계약 (v2)")
