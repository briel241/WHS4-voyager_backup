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
    readiness_probe,
    results_cmd,
    run_cmd,
    state,
)

PROBE = readiness_probe.PROBE_SCENARIO_ID


def fake_probe_result(workload_id, run_id, *, fail=False):
    """coordinator가 readiness probe workload에 대해 돌려주는 result (producer 계약 형태)."""
    transfers = []
    for entry in readiness_probe.PROBE_VECTOR:
        transfers.append({
            "transfer_id": "%s-%s" % (workload_id, entry["step"]), "step": entry["step"],
            "final_transfer_state": "COMPLETED" if not fail else "FAILED",
            "payload_sha256": entry["payload_sha256"], "payload_len": entry["payload_len"],
            "payload_format": entry["payload_format"], "wrong_destination": 0,
            "received": {"valid": not fail, "checks": {"byte_exact": not fail}}})
    body = {"scenario_id": PROBE, "status": "FAIL" if fail else "PASS", "reason": None,
            "steps": [{"name": e["step"], "status": "FAIL" if fail else "PASS"}
                      for e in readiness_probe.PROBE_VECTOR],
            "transfers": transfers,
            "counters": {"transfers_completed": 0 if fail else 2,
                         "transfers_failed": 2 if fail else 0, "wrong_destination": 0,
                         "mismatched_ack": 0, "duplicates": 0},
            "readiness_classification": {
                "policy_id": "VDETER_READINESS_FAILURE_CLASSIFIER_V1",
                "codes": ["TRANSFER_FAILED"] if fail else [],
                "transfers": {"all_terminal": True, "failed": 2 if fail else 0,
                              "wrong_destination": 0, "missing_data_received": 0}},
            "not_a_scenario": True}
    return {"status": "FAIL" if fail else "PASS",
            "quiescence": {"pass": True, "non_terminal_transfers": 0, "workers_running": 3,
                           "launchers_alive": 3, "faults": 0, "inhibited": False,
                           "child_terminated": True},
            "session_health_before": {"expected_launchers": 3},
            "workload_kind": "readiness_probe", "wall_clock_s": 41.2,
            "scenario": {"artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1",
                         "scenario_id": PROBE, "workload_id": workload_id,
                         "status": "FAIL" if fail else "PASS", "error": None,
                         "cleanup_errors": [], "results": {PROBE: body}}}

ENV_MANIFEST = fx.ENV_MANIFEST
OBSERVED = fx.OBSERVED
USB_ALL = fx.USB_ALL
GAIN = fx.GAIN
fake_preflight = fx.fake_preflight


# ---------------------------------------------------------------- 가짜 세션

SECRET_PAYLOAD_MARKER = "U0VDUkVULVBBWUxPQUQtTUFSS0VS"   # "SECRET-PAYLOAD-MARKER" base64


def _fake_tid(run_id: str, n: int) -> str:
    return "%s-8f73c821-%03d" % (run_id, n)


def _fake_base(n: int) -> float:
    return 1000000.0 + 100.0 * n


def fake_trace_rows(workload_id, run_id, n, scenario, case) -> list:
    """coordinator workload trace 형식(완료 evidence)의 행 — 시각은 t_monotonic_s
    (workload 기준)·t_utc·t_mono(data_received)만."""
    tid, base = _fake_tid(run_id, n), _fake_base(n)
    payload_format = ("x509.certificate_report" if scenario == "SC-VDE-001"
                      else "route")
    return [
        {"event": "workload_begin", "seq": 1, "scenario_id": scenario,
         "certificate_case": case, "workload_id": workload_id,
         "t_monotonic_s": 0.0001, "t_utc": "2026-09-05T00:00:00Z"},
        {"event": "submit", "seq": 2, "step": "s1", "sender": "ROC",
         "receiver": "MASS-A", "transfer_id": tid,
         "payload_format": payload_format, "payload_len": 353,
         "payload_sha256": "9b" * 32, "source_station": 222222222,
         "destination_station": 111111111, "ack_required": True,
         "t_monotonic_s": 0.084, "t_utc": "2026-09-05T00:00:00Z"},
        {"event": "transfer_terminal", "seq": 3, "step": "s1", "transfer_id": tid,
         "terminal_state": "COMPLETED", "t_monotonic_s": 14.61,
         "t_utc": "2026-09-05T00:00:14Z"},
        {"event": "data_received", "seq": 4, "step": "s1", "receiver": "MASS-A",
         "original_source": 222222222, "original_destination": 111111111,
         "present": True, "t_mono": base + 14.61, "t_monotonic_s": 14.6103,
         "t_utc": "2026-09-05T00:00:14Z"},
        {"event": "scenario_result", "seq": 5, "status": "PASS",
         "scenario": scenario,
         # controller와 같은 (name, status) 쌍 형식
         "steps": [["s1", "PASS"]],
         "counters": {"transfers_completed": 1, "transfers_failed": 0,
                      "wrong_destination": 0, "mismatched_ack": 0,
                      "duplicates": 0},
         "t_monotonic_s": 14.62, "t_utc": "2026-09-05T00:00:14Z"},
        {"event": "workload_end", "seq": 6, "status": "PASS", "error": None,
         "cleanup_errors": [], "t_monotonic_s": 15.69,
         "t_utc": "2026-09-05T00:00:15Z"},
    ]


def fake_scenario_result(workload_id, run_id, n, scenario, case) -> dict:
    """result JSON의 `scenario` (= mass-roc-events/final-result.json) 축약형."""
    tid = _fake_tid(run_id, n)
    checks = {"byte_exact": True, "original_destination": True,
              "original_source": True, "payload_format": True,
              "payload_len": True, "payload_sha256": True}
    transfer_step = {"name": "s1", "status": "PASS", "state": "COMPLETED",
                     "transfer_id": tid, "received_checks": checks}
    if scenario == "SC-VDE-001" and case == "forged":
        steps = [{"name": "001-report-built", "status": "PASS"}, transfer_step,
                 {"name": "001-forged-rejected", "status": "PASS",
                  "reason": "bad-signature", "trust_store_size": 0},
                 {"name": "001-not-stored", "status": "PASS"}]
    elif scenario == "SC-VDE-001":
        steps = [{"name": "001-report-built", "status": "PASS"}, transfer_step,
                 {"name": "001-verified-stored", "status": "PASS",
                  "reason": "ok", "trust_store_size": 1},
                 {"name": "001-idempotent-store", "status": "PASS"},
                 {"name": "001-resend-ok", "status": "PASS"}]
    else:
        steps = [transfer_step]
    label = scenario + ("[%s]" % case if scenario == "SC-VDE-001" else "")
    return {"artifact_type": "STAGE7_MASS_ROC_SCENARIO_RESULT_V1",
            "scenario_id": scenario, "workload_id": workload_id, "status": "PASS",
            "error": None, "cleanup_errors": [],
            "results": {label: {
                "scenario_id": label, "status": "PASS", "reason": None,
                "counters": {"transfers_completed": 1, "transfers_failed": 0,
                             "wrong_destination": 0, "mismatched_ack": 0,
                             "duplicates": 0},
                "steps": steps,
                "transfers": [{"transfer_id": tid, "step": "s1",
                               "final_transfer_state": "COMPLETED",
                               "wrong_destination": 0,
                               "received": {"valid": True, "checks": checks}}]}}}


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
            self.polls = []
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
            self.wait_ready_timeout_s = timeout_s
            if on_stage:
                on_stage("MASS-A 연결됨")   # coordinator 원문(한국어) → 표시는 영어
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
            # 완료된 evidence 흉내: endpoint service_events는 bring-up에서 미리
            # 기록해 두고(READY 이후 fake가 파일을 쓰지 않도록), workload n의
            # transfer_id·t_mono 기준과 짝을 맞춘다 (payload 원문은 절대 표시되면 안 된다).
            events_dir = self.run_root / "evidence" / "service_events"
            events_dir.mkdir(parents=True, exist_ok=True)
            roc, mass_a = [], []
            for n in range(1, 5):
                tid, base = _fake_tid(self.kwargs["run_id"], n), _fake_base(n)
                for offset, state in ((6.5, "QUEUED"), (9.5, "TRANSMITTING"),
                                      (10.0, "WAITING_ACK"), (21.0, "COMPLETED")):
                    roc.append({"t_mono": base + offset, "event_type": "TRANSFER_STATE_CHANGED",
                                "transfer_id": tid, "transfer_state": state, "error_code": None})
                mass_a.append({"t_mono": base + 14.61, "event_type": "DATA_RECEIVED",
                               "transfer_id": None, "payload_sha256": "9b" * 32,
                               "payload_len": 353,
                               "payload_base64": SECRET_PAYLOAD_MARKER})
            for name, rows in (("ROC", roc), ("MASS-A", mass_a)):
                (events_dir / (name + ".jsonl")).write_bytes(
                    "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))
            # coordinator가 남기는 workload trace 파일도 같은 행으로 미리 기록한다
            # (batch 계획의 workload id는 결정적) — results 재열람이 읽는 evidence.
            for n, (scenario_id, case) in enumerate(run_cmd.DEFAULT_BATCH_PLAN, 1):
                wid = "%s-%s-%03d" % (self.kwargs["run_id"], scenario_id.lower(), n)
                wdir = self.run_root / "workloads" / wid
                wdir.mkdir(parents=True, exist_ok=True)
                (wdir / "workload-trace.jsonl").write_bytes("".join(
                    json.dumps(r) + "\n" for r in fake_trace_rows(
                        wid, self.kwargs["run_id"], n, scenario_id, case)).encode("utf-8"))
            ready = {"run_id": self.kwargs["run_id"],
                     "artifact_type": "STAGE7_SERVICE_SESSION_READY_V1",
                     "judgement_scope": "OPERATIONAL_SERVICE",
                     "ready_at_utc": "2026-09-05T00:00:00Z",
                     "phase_elapsed_s": {"prepare_configure_arm": 14.0, "acquire_host_time": 400.0,
                                         "rf_time_pre": 857.0, "active_bridge": 857.1,
                                         "rf_time_post": 1314.0, "service_running": 1326.0},
                     "health_baseline": {"bridge_generation": 0,
                                         "active_clock_id": "stage7-pi-host-affine-v1:x:0"},
                     "readiness_mode": "cold" if self.kwargs.get("phase") == "mass-roc-session-cold"
                     else "warm"}
            if "absolute_deadline" in script:
                ready["session_absolute_deadline_monotonic_s"] = script["absolute_deadline"]()
            if self.kwargs.get("phase") == "mass-roc-session-cold":
                # coordinator cold path가 봉인하는 calibration candidate 흉내 (실 schema).
                import hashlib as _hashlib
                evidence = self.run_root / "evidence"
                evidence.mkdir(parents=True, exist_ok=True)
                artifact = json.dumps({"artifact_type": "STAGE7_GAIN_ARTIFACT_V1",
                                       "run_id": self.kwargs["run_id"],
                                       "endpoint_common_rx_gain_db": {"MASS-A": 66, "MASS-B": 63, "ROC": 72},
                                       "lane_candidates": {}, "gain_plan_sha256": "77" * 32},
                                      ensure_ascii=True, sort_keys=True, indent=1).encode()
                artifact_sha = _hashlib.sha256(artifact).hexdigest()
                gain_input = json.dumps({"endpoint_common_rx_gain_db": {"MASS-A": 66, "MASS-B": 63, "ROC": 72},
                                         "source_gain_artifact_sha256": artifact_sha},
                                        ensure_ascii=True, sort_keys=True, indent=1).encode()
                (evidence / "gain_artifact.json").write_bytes(artifact)
                (evidence / "gain_artifact_input.json").write_bytes(gain_input)
                ready["calibration"] = {
                    "artifact_sha256": artifact_sha,
                    "artifact_input_sha256": _hashlib.sha256(gain_input).hexdigest(),
                    "endpoint_common_rx_gain_db": {"MASS-A": 66, "MASS-B": 63, "ROC": 72},
                    "gain_plan_sha256": "77" * 32, "cfo_plan_sha256": "88" * 32,
                    "quiescent": not script.get("cold_not_quiescent"),
                    "host_time_attempts": 2, "calibration_mode": "ADAPTIVE_SEARCH_AND_SEAL"}
                if script.get("cold_tamper_artifact"):
                    (evidence / "gain_artifact.json").write_bytes(artifact + b"\n")
            return ready

        supports_workload_kind = True

        def submit_workload(self, scenario_id, certificate_case="normal", *,
                            on_settle=None, workload_kind=None):
            self.kinds = getattr(self, "kinds", [])
            self.kinds.append(workload_kind)
            if scenario_id == PROBE:
                # readiness 내부 probe — user workload 목록(`submitted`)과 분리해 기록한다.
                self.probes = getattr(self, "probes", [])
                self.probes.append(workload_kind)
                return "%s-readiness-link17-probe-%03d" % (self.kwargs["run_id"], len(self.probes))
            self.submitted.append((scenario_id, certificate_case))
            # I/O 격리 검증용: submit 시점의 out 길이 (이전 workload 블록이 끝난 뒤인지).
            probe = script.get("out_probe")
            self.submit_out_lengths = getattr(self, "submit_out_lengths", [])
            if probe:
                self.submit_out_lengths.append(probe())
            return "%s-%s-%03d" % (self.kwargs["run_id"],
                                   scenario_id.lower(), len(self.submitted))

        def wait_result(self, workload_id, *, timeout_s=None, on_trace=None,
                        on_poll=None):
            if workload_id.split("-readiness-link17-probe-")[0] != workload_id:
                return fake_probe_result(workload_id, self.kwargs["run_id"],
                                         fail=bool(script.get("probe_fail")))
            # R1: READY 이후 폴링 훅은 실행 경로에 없어야 한다 — 받은 값을 기록만
            # 하고(호출하지 않음) 시험이 None임을 단언한다.
            self.polls.append(on_poll)
            scenario, case = self.submitted[-1]
            n = len(self.submitted)
            self.trace_out_lengths = getattr(self, "trace_out_lengths", [])
            if on_trace:
                for row in fake_trace_rows(workload_id, self.kwargs["run_id"], n,
                                           scenario, case):
                    on_trace(row)
                    # 실시간 출력 금지 검증용: 이 시점의 out 길이를 기록한다.
                    probe = script.get("out_probe")
                    if probe:
                        self.trace_out_lengths.append(probe())
            override = (script.get("results") or {}).get(scenario)
            result = {"status": "PASS", "quiescence": {"pass": True,
                                                       "non_terminal_transfers": 0,
                                                       "workers_running": 3,
                                                       "launchers_alive": 3,
                                                       "faults": 0, "inhibited": False},
                      "session_health_before": {"expected_launchers": 3},
                      "wall_clock_s": 87.3,
                      "scenario": fake_scenario_result(workload_id, self.kwargs["run_id"],
                                                       n, scenario, case)}
            if override:
                result.update(override)
            probe = script.get("out_probe")
            self.result_out_lengths = getattr(self, "result_out_lengths", [])
            if probe:
                self.result_out_lengths.append(probe())
            return result

        def close(self):
            if script.get("close_raises_interrupt") and not getattr(
                    self, "_close_interrupted", False):
                self._close_interrupted = True
                raise KeyboardInterrupt
            self.closed = True
            # `close`(dict)를 주면 그 finalize 요약을 그대로 돌려준다 — coordinator가
            # rc≠0으로 끝나는 종료(예: 빈 측정 fail-closed)를 재현하기 위한 seam.
            scripted = script.get("close")
            if scripted is not None:
                return json.loads(json.dumps(scripted))
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
        progress_ticker=False, machine_id=fx.MACHINE_ID, boot_id="boot-test",
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
        # readiness 계약: qualification이 없어도 거부하지 않는다 — UNVERIFIED에서 cold
        # CALIBRATION_PREPARATION → readiness preparation을 한 session에서 수행한다.
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        factory = make_session_factory()
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_PASS, lines
        text = "\n".join(lines)
        assert "current_authority=UNVERIFIED operational_readiness=ABSENT qualification=ABSENT" in text
        assert "[readiness] calibration: COLD" in text
        assert "OPERATIONAL_READY is not QUALIFIED" in text
        session = factory.instances[0]
        assert session.kwargs["phase"] == "mass-roc-session-cold"
        assert session.kwargs["gain_artifact_path"] is None
        assert session.wait_ready_timeout_s == 3900.0
        assert 'run_mode = "GAIN_CALIBRATION"' in (
            session.run_root / "resolved-configs" / "endpoints" / "mass-b.toml").read_text("utf-8")

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
        # qualification은 STALE(거부 아님); 라이브러리가 달라 그 gain은 calibration source로도
        # 부적합 → cold preparation. 세션은 만들어진다.
        assert code == run_cmd.EXIT_PASS, lines
        assert any("[qualification] STALE" in line and "uhd version" in line for line in lines)
        assert any("qualification=STALE" in line for line in lines)
        assert factory.instances and factory.instances[-1].kwargs["phase"] == "mass-roc-session-cold"
        factory = make_session_factory()

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
        # R1: READY 이후 wait_result에 폴링 훅(on_poll)이 전달되지 않는다.
        assert session.polls == [None] * len(run_cmd.DEFAULT_BATCH_PLAN)

        text = "\n".join(lines)
        assert "current_authority=QUALIFIED operational_readiness=ABSENT qualification=APPLICABLE" in text
        assert "[readiness] calibration: WARM — qualification_reference:" in text
        assert "[readiness] Link 17 probe PASS" in text
        assert "[readiness] READY_FOR_WORKLOAD" in text
        assert "[readiness] PROMOTED" in text
        assert session.probes == ["readiness_probe"] and session.kinds[0] == "readiness_probe"
        assert all(kind == "user" for kind in session.kinds[1:])
        assert "SESSION READY" in text
        assert "MASS-A connected" in text and "연결됨" not in text
        # bring-up 진행 표시(non-TTY 줄 로그)와 READY 이후 저 I/O 시나리오 출력
        # (88fa6df와 같은 running/trace 행/result 요약 — 실시간 세로 블록 없음).
        assert "[BRINGUP]" in text and "Endpoint startup" in text
        assert "Service session READY" in text
        assert "running SC-VDE-005" in text and "running SC-VDE-001 (forged)" in text
        # 결과 확정 후 전송 단위 세로 상세 블록(scenario_report)이 기본 출력이고,
        # RF 활성 구간(submit→result)의 실시간 trace 행·세로 블록은 없다.
        assert "[1/4] SC-VDE-005 — PASS" in text and "[4/4] SC-VDE-001 [forged] — PASS" in text
        assert "[SCENARIO] SC-VDE-005 — PASS" in text and "[TRANSFER 1/1] s1" in text
        assert "rendered_transfers_total=1 (source=trace-count)" in text
        assert "path=ROC(222222222) -> MASS-A(111111111)" in text
        assert "payload_format=route" in text and "application_bytes=353" in text
        assert "+00.084s  SUBMITTED" in text and "+14.610s  DATA_RECEIVED by MASS-A" in text
        assert "QUEUED -> TRANSMITTING -> WAITING_ACK -> COMPLETED" in text
        assert "security_result=PASS (forged certificate correctly rejected)" in text
        assert "verification_reason=bad-signature" in text and "verification_reason=ok" in text
        assert "ROC -> MASS-A    submit" not in text and "verdict   PASS" not in text
        assert "[STEP" not in text and "[TX]" not in text and "[RX]" not in text
        assert SECRET_PAYLOAD_MARKER not in text and "payload_base64" not in text
        assert "result: PASS · 87.3 s · quiescence OK" in text
        assert "Closed — cleanup PASS, residual processes 0" in text
        # non-TTY: carriage return·ANSI 오염 없음, 시나리오 구간 진행바 없음.
        assert "\r" not in text and "\x1b" not in text
        after_ready = text.split("SESSION READY", 1)[1]
        assert "[BRINGUP]" not in after_ready and "█" not in after_ready
        result_lines = [l for l in lines if l.startswith("RESULT ")]
        assert result_lines[-1].startswith(
            "RESULT overall=PASS scenarios=4/4 run-id=")
        assert result_lines[-1].endswith(
            "authority=QUALIFIED readiness=PROMOTED qualification=QUALIFIED")
        summary = json.loads((session.run_root / "summary.json"
                              ).read_text("utf-8"))
        assert summary["status"] == "PASS"
        assert summary["scenarios_passed"] == 4
        assert summary["readiness"]["issue_status"] == "PROMOTED"
        assert summary["readiness"]["mode"] == "warm"
        assert summary["cancelled"] is False and summary["termination"] == "plan_complete"
        state = json.loads(paths.readiness_state.read_text("utf-8"))
        assert state["generation"] == 2 and state["active"]["release"] == summary["readiness"]["release"]
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
            assert "running SC-VDE-005" in text     # READY 이후 저 I/O 출력 (R1)
    assert summaries[0] == summaries[1]


def test_run_retry_only_for_transient_host_time_failure():
    """readiness 경로(명세 §5-7): host-time 일시 실패로 분류되더라도 **재시도하지 않는다**
    (warm/cold implicit retry 0). legacy 분류(`classify_bringup_failure`)는 설명 줄에만 남고
    어떤 값이어도 두 번째 bring-up을 만들지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "[coordinator] phase 실패: MASS-B host affine fit FAIL", None]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL, lines
        assert len(factory.instances) == 1
        first = factory.instances[0]
        assert first.closed and first.run_root.is_dir()
        assert not any("Retrying once with a new run-id" in l for l in lines)
        assert not any(l.strip() == "attempt 2/2" for l in lines)
        text = "\n".join(lines)
        # bring-up 실패 상세 블록은 그대로 — 판정만 '재시도 없음'이다.
        assert "layer=ORCHESTRATION" in text
        assert "reason=HOST_AFFINE_FIT_FAILED" in text
        assert "attempt=1/1" in text
        assert "No retry: the readiness path never retries a bring-up" in text
        assert "[readiness] NOT PROMOTED" in text
        assert "evidence=%s" % first.run_root in text
        summary = json.loads((first.run_root / "summary.json").read_text("utf-8"))
        assert [a["bringup"] for a in summary["attempts"]] == ["FAILED"]
        assert summary["readiness"]["issue_status"] == "NOT_PROMOTED"
        assert summary["readiness"]["termination"] == "bringup_failed"
        # host-time 실패는 inhibit 사유가 아니다 — state는 generation 1 그대로.
        assert json.loads(paths.readiness_state.read_text("utf-8"))["generation"] == 1

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": [
                "SERVICE_BINDING_REJECTED: baseline SHA-256 불일치"]})
        code, lines = run(paths, factory)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 1
        assert any(l.startswith("No retry:") for l in lines)
        assert any(l.startswith("RESULT overall=FAIL scenarios=0/4")
                   for l in lines)

    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = make_session_factory({
            "bringup_fail_console": ["MASS-A host affine fit FAIL"]})
        code, lines = run(paths, factory, retry=False)
        assert code == run_cmd.EXIT_FAIL
        assert len(factory.instances) == 1
        assert any("--no-retry has no effect" in l for l in lines)


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
        assert "[1/4]" not in text  # READY 이후 [n/N]·세로 블록 없음 (R1)
        assert "running SC-VDE-001 (forged)" in text

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
        assert "result: FAIL" in text and "reason: ack timeout" in text
        assert "[FAIL] layer=" not in text          # 실시간 판정 블록 없음 (R1)
        assert "Evidence: " in text
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
        assert any(results_cmd.LIST_HINT == l for l in lines)
        lines = []
        assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
        text = "\n".join(lines)
        assert ("RUN %s — PASS" % run_id) in text and "SC-VDE-001   forged  PASS" in text
        # 저장 evidence 재열람도 run과 같은 renderer(scenario_report)를 쓴다.
        assert "[4/4] SC-VDE-001 [forged] — PASS" in text
        assert "security_result=PASS (forged certificate correctly rejected)" in text
        assert "[BINDING]" in text and "[BRING-UP]" in text and "[CLOSE-CLEANUP]" in text
        assert SECRET_PAYLOAD_MARKER not in text and "payload_base64" not in text
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
