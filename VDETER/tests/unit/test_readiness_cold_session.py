"""readiness cold path — coordinator 측 RF-off 계약 시험 (operational_readiness spec §5.1.1, §5.2).

증명하는 것 (장치·RF 없음, fake coordinator)
  순서      : start_cfo → acquire_host_time(attempt 1) → calibrate_rf(ADAPTIVE_SEARCH_AND_SEAL) →
              candidate durable(gain_artifact/input fsync) → quiescent → 반환 summary.
              세션 본체는 그 뒤 acquire_host_time(attempt 2)로 **새** host fit을 쓴다.
  plan      : cfo_plan = RR_CFO_PATHS 3 path × 10 burst(6.8 s, 75 s 간격, 40 dB, deadline 420 s);
              gain_plan = build_adaptive_gain_paths + 재검증 6 × (3 + 100), deadline 1200 s;
              explicit planned burst 30 + 2630 + 618 + 120 = 3398 (= standalone plan 계산과 일치)
  fail-closed: calibrate FAIL·common gain 부족·inhibited/fault → RuntimeError(재시도 없음), artifact 미봉인
  main()    : cold phase는 --gain-artifact를 거부, warm session은 요구; cold guard/deadline 값·ready.json
              필드는 source 구조로 확인
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import tempfile

import pytest

from apps import coordinator_main as cm


class _FakeResolved:
    def __init__(self):
        self.source_gain_artifact_sha256 = None


class _FakeCoordinator:
    """run_barrier 호출을 기록하고 스크립트된 결과를 돌려준다."""

    def __init__(self, *, calibrate_pass=True, common=None, host_fit_pass=True,
                 inhibited=False, faults=0):
        self.calls = []
        self._run_id = "run-cold-test"
        self.inhibited = inhibited
        self._faults = [object()] * faults
        self._calibrate_pass = calibrate_pass
        self._common = common if common is not None else {"MASS-A": 66, "MASS-B": 63, "ROC": 72}
        self._host_fit_pass = host_fit_pass

    def faults(self):
        return list(self._faults)

    def run_barrier(self, command_name, bodies, **kwargs):
        self.calls.append((command_name, bodies, kwargs))
        members = kwargs.get("members") or list(bodies)
        if command_name == "acquire_host_time":
            return {m: {"body": {"result": {"outcome": "PASS" if self._host_fit_pass else "FAIL",
                                            "alpha_num": 1, "alpha_den": 1,
                                            "beta_num": 1, "beta_den": 1}}} for m in members}
        if command_name == "calibrate_rf":
            return {m: {"body": {"result": {"passed": self._calibrate_pass,
                                            "endpoint_common_rx_gain_db": self._common.get(m),
                                            "lane_candidates": {"0": self._common.get(m)}}}}
                    for m in members}
        return {m: {"body": {"result": None}} for m in members}


@pytest.fixture(autouse=True)
def _rf_off_hardware_seams(monkeypatch):
    """CLOCK_MONOTONIC_RAW(POSIX 전용)와 PHY probe vector 생성을 RF-off double로 바꾼다."""
    from vdeter.b210_system import hw_operations
    clock = {"ns": 1_000_000_000_000}

    def fake_clock():
        clock["ns"] += 1_000_000
        return clock["ns"]

    monkeypatch.setattr(hw_operations, "read_monotonic_raw_ns", fake_clock)
    monkeypatch.setattr(hw_operations, "probe_vector_sha256", lambda: "ab" * 32)


def _run(coordinator, run_root, marks=None, emits=None):
    marks = marks if marks is not None else []
    emits = emits if emits is not None else []
    resolved = {m: _FakeResolved() for m in cm.ENDPOINT_IDS}
    channel_by_role = {"upper": "VDETER_UPPER_100_TEST", "lower": "VDETER_LOWER_100_TEST"}
    return cm._run_readiness_cold_calibration(
        coordinator, resolved, None, run_root, present=list(cm.ENDPOINT_IDS),
        paths=cm.P2_PATHS, channel_by_role=channel_by_role,
        mark=marks.append, emit=lambda event, **fields: emits.append((event, fields)))


def test_cold_calibration_order_plans_and_candidate_are_the_rr_p1_contract():
    with tempfile.TemporaryDirectory() as tmp:
        coordinator = _FakeCoordinator()
        marks, emits = [], []
        summary = _run(coordinator, tmp, marks, emits)
        # 순서: CFO → host fit(attempt 1) → calibrate. 그 뒤 세션 본체가 attempt 2를 요청한다.
        assert [c[0] for c in coordinator.calls] == ["start_cfo", "acquire_host_time", "calibrate_rf"]
        assert marks == ["start_cfo", "calibration_host_time", "calibrate_rf",
                         "calibration_sealed", "calibration_quiescent"]
        cfo_bodies = coordinator.calls[0][1]
        assert set(cfo_bodies) == set(cm.ENDPOINT_IDS)
        assert cfo_bodies["ROC"]["path_ids"] == ["C1", "C3", "C5"]
        assert coordinator.calls[0][2] == {"members": list(cm.ENDPOINT_IDS), "concurrent_commit": True}
        host_bodies = coordinator.calls[1][1]
        assert all(b["acquisition_attempt"] == 1 and b["max_acquisition_ns"] == 600_000_000_000
                   for b in host_bodies.values())
        cal_bodies = coordinator.calls[2][1]
        assert all(b["calibration_mode"] == "ADAPTIVE_SEARCH_AND_SEAL"
                   and b["operational_config_sha256"] is None
                   and b["path_ids"] == ["C1", "C2", "C3", "C4", "C5", "C6"]
                   for b in cal_bodies.values())
        # plans: RR-P1과 같은 CFO/gain schedule.
        cfo = json.load(open(os.path.join(tmp, "plans", "cfo_plan.json")))
        assert cfo["probe_tx_gain_db"] == 40 and cfo["probe_rx_gain_db"] == 40
        assert [p["path_id"] for p in cfo["paths"]] == ["C1", "C3", "C5"]
        starts = [p["scheduled_host_raw_ns"][0] for p in cfo["paths"]]
        assert all(len(p["scheduled_host_raw_ns"]) == 10 for p in cfo["paths"])
        assert starts[1] - starts[0] == 75_000_000_000 and starts[2] - starts[1] == 75_000_000_000
        assert all(b - a == 6_800_000_000 for p in cfo["paths"]
                   for a, b in zip(p["scheduled_host_raw_ns"], p["scheduled_host_raw_ns"][1:]))
        gain = json.load(open(os.path.join(tmp, "plans", "gain_plan.json")))
        train = {p["path_id"]: len(p["scheduled_host_raw_ns"]) for p in gain["paths"]}
        assert train == {"C1": 501, "C2": 501, "C3": 501, "C4": 313, "C5": 501, "C6": 313}
        assert all(p["coarse_gains"] == [0, 12, 24, 36, 48, 60, 72, 75] and p["warmup_count"] == 3
                   and p["candidate_bursts"] == 10 and p["fine_step_db"] == 3 for p in gain["paths"])
        reval = gain["revalidation_paths"]
        assert [p["path_id"] for p in reval] == ["C1", "C2", "C3", "C4", "C5", "C6"]
        assert all(len(p["scheduled_host_raw_ns"]) == 103 and p["warmup_count"] == 3
                   and p["reval_max_no_decode"] == cm.REVAL_MAX_NO_DECODE for p in reval)
        explicit = (30 + sum(train.values()) + sum(len(p["scheduled_host_raw_ns"]) for p in reval)
                    + 6 * 10 * 2)
        assert explicit == 3398
        # gain phase deadline 1200 s (20분) — plan에 결속.
        deadline = gain["paths"][0]["deadline_host_raw_ns"]
        first = cfo["paths"][0]["scheduled_host_raw_ns"][0]
        assert 0 < deadline - first < 21 * 60_000_000_000
        # candidate durable: artifact + input, schema = RR-P1 gain artifact.
        artifact = open(os.path.join(tmp, "evidence", "gain_artifact.json"), "rb").read()
        gain_input = json.loads(open(os.path.join(tmp, "evidence", "gain_artifact_input.json"), "rb").read())
        assert json.loads(artifact)["artifact_type"] == "STAGE7_GAIN_ARTIFACT_V1"
        assert json.loads(artifact)["readiness_calibration_candidate"] is True
        assert gain_input["source_gain_artifact_sha256"] == hashlib.sha256(artifact).hexdigest()
        assert summary["artifact_sha256"] == hashlib.sha256(artifact).hexdigest()
        assert summary["endpoint_common_rx_gain_db"] == {"MASS-A": 66, "MASS-B": 63, "ROC": 72}
        assert summary["quiescent"] is True and summary["host_time_attempts"] == 2
        assert summary["gain_plan_sha256"] == hashlib.sha256(
            open(os.path.join(tmp, "plans", "gain_plan.json"), "rb").read()).hexdigest()
        assert [e[0] for e in emits] == ["calibration_quiescence"]


def test_cold_plan_totals_match_standalone_readiness_plan():
    from apps.standalone import readiness_run
    plan = readiness_run.readiness_plan("cold", readiness_id="ready-20260917T000000Z-00000000",
                                        run_id="run-x", calibration=None, preparation_cap_s=3900)
    assert plan["explicit_planned_bursts_total"] == 3398
    assert plan["adaptive_gain"]["train_bursts_120ms"] == 501
    assert plan["adaptive_gain"]["train_bursts_240ms"] == 313
    assert plan["gain_phase_deadline_s"] == 1200 and plan["cfo"]["operation_deadline_s"] == 420
    assert plan["hard_cap_s"] == 3900 and plan["implicit_retry"] == 0


@pytest.mark.parametrize("kind", ["calibrate_fail", "missing_common", "host_fit_fail", "inhibited", "fault"])
def test_cold_calibration_fail_closed_without_retry(kind):
    with tempfile.TemporaryDirectory() as tmp:
        if kind == "calibrate_fail":
            coordinator = _FakeCoordinator(calibrate_pass=False)
        elif kind == "missing_common":
            coordinator = _FakeCoordinator(common={"MASS-A": 66, "ROC": 72})
        elif kind == "host_fit_fail":
            coordinator = _FakeCoordinator(host_fit_pass=False)
        elif kind == "inhibited":
            coordinator = _FakeCoordinator(inhibited=True)
        else:
            coordinator = _FakeCoordinator(faults=1)
        with pytest.raises(RuntimeError):
            _run(coordinator, tmp)
        names = [c[0] for c in coordinator.calls]
        assert names.count("start_cfo") == 1 and names.count("calibrate_rf") <= 1
        if kind in ("calibrate_fail", "missing_common", "host_fit_fail"):
            assert not os.path.exists(os.path.join(tmp, "evidence", "gain_artifact.json"))


def test_session_uses_cold_guard_fresh_fit_and_ready_deadline():
    source = inspect.getsource(cm.run_mass_roc_session)
    assert 'PhaseGuard("READINESS_COLD_PREPARATION"' in source
    assert "deadline_s=READINESS_COLD_PREPARATION_S" in source
    assert cm.READINESS_COLD_PREPARATION_S == 3900.0
    assert cm.SESSION_DEADLINE_S == 7200 and cm.SESSION_IDLE_TIMEOUT_S == 2700
    assert cm.SESSION_WORKLOAD_TIMEOUT_S == 900.0
    assert '"acquisition_attempt": host_time_attempt' in source
    assert "host_time_attempt = 2" in source
    assert 'provisional-v1:{coordinator._run_id}:{host_time_attempt}' in source
    assert '"session_absolute_deadline_monotonic_s": guard._deadline' in source
    assert '"calibration": calibration_summary' in source
    assert "cold_guard = None" in source        # READY 뒤 잔여 예산은 CLI가 강제
    # cold deadline은 channel guard로 강제되고(모든 barrier 요청/대기), READY에서 session guard로 복귀한다.
    assert "channel.attach_guard(cold_guard)" in source
    assert "channel.attach_guard(guard)" in source
    tree = ast.parse(source)
    cold_mark = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cold_mark")
    assert "cold_guard.check()" in ast.unparse(cold_mark)
    mark = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "mark")
    assert not any(isinstance(stmt, ast.If) for stmt in mark.body)   # 승인된 mark() 구조 불변
    # workload_kind pass-through (readiness probe / user 구분).
    assert 'request.get("workload_kind", "user")' in source
    assert '"workload_kind": workload_kind' in source


def test_main_refuses_gain_artifact_for_cold_and_requires_it_for_warm(monkeypatch, tmp_path):
    # validate_rr_binding 이전에 gain artifact 규칙이 걸린다 — SystemExit 메시지로 확인.
    with pytest.raises(SystemExit) as cold:
        cm.main(["--run-id", "r", "--phase", "mass-roc-session-cold", "--run-root", str(tmp_path),
                 "--configs-dir", str(tmp_path), "--gain-artifact", str(tmp_path / "g.json"),
                 "--baseline-sha256", "0" * 64])
    assert "mass-roc-session-cold" in str(cold.value) and "gain-artifact" in str(cold.value)
    with pytest.raises(SystemExit) as warm:
        cm.main(["--run-id", "r", "--phase", "mass-roc-session", "--run-root", str(tmp_path),
                 "--configs-dir", str(tmp_path), "--baseline-sha256", "0" * 64])
    assert "mass-roc-session" in str(warm.value) and "gain-artifact" in str(warm.value)


def test_binding_gate_alias_applies_the_unchanged_service_rules_to_cold():
    import argparse
    args = argparse.Namespace(phase="mass-roc-session-cold", run_id="run-x",
                              baseline_sha256="ab" * 32)
    aliased = cm._binding_gate_arguments(args)
    assert aliased.phase == "mass-roc-session" and aliased.run_id == "run-x"
    assert cm._binding_gate_arguments(argparse.Namespace(phase="rr-p1")).phase == "rr-p1"
    metadata = {"qualification_profile": "RESEARCH_READY_MINIMAL", "rr_phase": "rr-p2-17",
                "run_id": "run-x", "baseline_sha256": "ab" * 32, "implicit_retry": 0}
    cm.validate_rr_binding(aliased, metadata)      # 통과 — 규칙은 service session과 동일
    with pytest.raises(SystemExit):
        cm.validate_rr_binding(aliased, dict(metadata, implicit_retry=1))
    with pytest.raises(SystemExit):
        cm.validate_rr_binding(aliased, dict(metadata, rr_phase="rr-p1"))
    with pytest.raises(SystemExit):
        cm.validate_rr_binding(aliased, dict(metadata, baseline_sha256="00" * 32))
    # qualified `validate_rr_binding` 자체는 cold phase 이름을 모른다 (byte-identical 보존).
    import inspect
    assert "mass-roc-session-cold" not in inspect.getsource(cm.validate_rr_binding)
    assert "_binding_gate_arguments(arguments)" in inspect.getsource(cm.main)
