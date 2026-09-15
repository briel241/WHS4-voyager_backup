"""`RESEARCH_READY_MINIMAL` 프로파일 단위시험 (§17A).

- PhaseGuard deadline/stall fail-fast와 implicit-retry 부재
- adaptive gain 탐색의 결정적 coarse→fine 선택
- RR evidence artifact schema
- `calibration_mode` additive enum
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_coordinator_app():
    spec = importlib.util.spec_from_file_location(
        "stage7_coordinator_main_for_test",
        os.path.join(VDETER_ROOT, "apps", "stage7_coordinator_main.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PhaseGuardTest(unittest.TestCase):
    def test_deadline_exceeded_raises(self):
        app = _load_coordinator_app()
        guard = app.PhaseGuard("t", deadline_s=-0.001, stall_s=999)
        with self.assertRaises(RuntimeError) as ctx:
            guard.check()
        self.assertIn("PHASE_DEADLINE_EXCEEDED", str(ctx.exception))

    def test_stall_raises_and_progress_resets(self):
        app = _load_coordinator_app()
        guard = app.PhaseGuard("t", deadline_s=999, stall_s=0.05)
        time.sleep(0.1)
        with self.assertRaises(RuntimeError) as ctx:
            guard.check()
        self.assertIn("PHASE_PROGRESS_STALLED", str(ctx.exception))
        guard.progress()
        guard.check()  # progress 직후에는 통과

    def test_no_implicit_retry_counter(self):
        app = _load_coordinator_app()
        guard = app.PhaseGuard("t", deadline_s=999, stall_s=999)
        guard.progress("a")
        guard.progress("b")
        self.assertEqual(guard.progress_count, 2)


class RrEvidenceTest(unittest.TestCase):
    def test_artifact_and_index_schema(self):
        import tempfile

        app = _load_coordinator_app()
        with tempfile.TemporaryDirectory() as root:
            path = app.write_rr_evidence(
                root, "TC-STAGE7-RR-001", run_id="run-x",
                metadata={"baseline_version": "0.9.0", "baseline_sha256": "ab" * 32,
                          "git_commit": "deadbeef", "git_dirty": True,
                          "hardware": {"usb_devices": "..."}},
                started_utc="2026-08-27T00:00:00Z", ended_utc="2026-08-27T00:01:00Z",
                duration_s=60.0, counters={"cycles": 1, "retries": 0},
                status="PASS", detail={"x": 1}, hardware={"usb_devices": "..."},
            )
            with open(path, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
            # 0.9.20 재현성 3필드(stage7_diag_env 등)와 lifecycle_generation
            # 포함 — 재현 조건이 artifact에 기계 기록되는지의 회귀 방지.
            for key in ("profile", "baseline_version", "baseline_sha256",
                        "git_commit", "git_dirty", "hardware", "run_id",
                        "started_utc", "ended_utc", "duration_s", "counters",
                        "status", "scope", "stage7_diag_env",
                        "rx_energy_gate_env", "source_gain_artifact_sha256",
                        "svcdbg_allow_rx_ready_env", "scenario_filter_env",
                        "lifecycle_generation"):
                self.assertIn(key, data)
            self.assertEqual(data["profile"], "RESEARCH_READY_MINIMAL")
            self.assertEqual(data["counters"]["retries"], 0)
            self.assertEqual(data["lifecycle_generation"], 1)
            self.assertIn("full qualification", data["scope"])
            index_path = os.path.join(
                root, "evidence", "research_ready", "index.json"
            )
            with open(index_path, "rb") as handle:
                index = json.loads(handle.read().decode("utf-8"))
            self.assertIn("TC-STAGE7-RR-001", index["entries"])
            self.assertEqual(index["entries"]["TC-STAGE7-RR-001"]["status"], "PASS")

    def test_invalid_status_rejected(self):
        import tempfile

        app = _load_coordinator_app()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(AssertionError):
                app.write_rr_evidence(
                    root, "TC-STAGE7-RR-001", run_id="r", metadata={},
                    started_utc="", ended_utc="", duration_s=0.0, counters={},
                    status="VERIFIED", detail={}, hardware={},
                )


class _AdaptiveStub:
    """`_gain_rx_adaptive`가 사용하는 표면만 가진 스텁.

    실제 `_gain_rx_run` 계약을 그대로 흉내낸다: step 목록을 순서대로
    걷고 첫 통과에서 조기 중단하며, `deadline_host_raw_ns`가 지났으면
    잔여 step을 캡처하지 않는다 (창 소실 없이 결정적)."""

    def __init__(self, script):
        self.evidence = []
        self._script = dict(script)
        self.measured = []

    def _gain_rx_run(self, worker, stack, lane, path, steps, mode, digest,
                     judge_from=0, deadline_host_raw_ns=None):
        from vdeter.stage7 import hw_operations

        summaries = []
        for step in steps:
            if (
                deadline_host_raw_ns is not None
                and hw_operations.read_monotonic_raw_ns() >= deadline_host_raw_ns
            ):
                break
            gain_db = int(step["gain_db"])
            self.measured.append(gain_db)
            outcome = self._script.get(gain_db, "fail")
            if outcome == "pass":
                summaries.append(
                    {"gain_db": gain_db, "window": True, "accepted": 10}
                )
                return gain_db, False, summaries
            if outcome == "overload":
                summaries.append(
                    {"gain_db": gain_db, "window": True, "overload": True,
                     "accepted": 0}
                )
                return None, True, summaries
            summaries.append(
                {"gain_db": gain_db, "window": True, "accepted": 0}
            )
        return None, False, summaries


def _adaptive_path(deadline_ns=0):
    coarse = [0, 12, 24, 36, 48, 60, 72, 75]
    return {
        "path_id": "C1",
        "warmup_count": 3,
        "candidate_bursts": 10,
        "fine_step_db": 3,
        "deadline_host_raw_ns": deadline_ns,
        "coarse_windows": [
            {"gain_db": g, "scheduled_host_raw_ns": [i * 100 + k for k in range(13)]}
            for i, g in enumerate(coarse)
        ],
        "fine_window": {"scheduled_host_raw_ns": [9000 + k for k in range(13)]},
        "scheduled_host_raw_ns": list(range(0, 9013)),
    }


class AdaptiveGainTest(unittest.TestCase):
    def _run(self, script, deadline_ns=0):
        from vdeter.stage7.pi_runtime import Stage7PiOperations

        stub = _AdaptiveStub(script)
        path = _adaptive_path(deadline_ns=deadline_ns)
        result = Stage7PiOperations._gain_rx_adaptive(
            stub, None, None, 0, path, "ADAPTIVE_SEARCH_AND_SEAL", "d" * 64
        )
        return stub, result

    def test_coarse_then_fine_minimum(self):
        stub, (g_lane, overload, _) = self._run(
            {60: "pass", 57: "pass"}
        )
        self.assertEqual(g_lane, 57)
        self.assertFalse(overload)
        # coarse 0..60(6회) + fine 57(1회) = 결정적 7 창, 반복 없음
        self.assertEqual(stub.measured, [0, 12, 24, 36, 48, 60, 57])

    def test_fine_fail_keeps_coarse_candidate(self):
        stub, (g_lane, overload, _) = self._run({60: "pass"})
        self.assertEqual(g_lane, 60)
        self.assertEqual(stub.measured, [0, 12, 24, 36, 48, 60, 57])

    def test_terminal_75_pass_after_72_fail(self):
        # v0.9.1 C항: 72 실패 후 75 terminal candidate 검사. 이미 실패한
        # 72는 fine에서 재측정하지 않는다.
        stub, (g_lane, overload, _) = self._run({75: "pass"})
        self.assertEqual(g_lane, 75)
        self.assertFalse(overload)
        self.assertEqual(stub.measured, [0, 12, 24, 36, 48, 60, 72, 75])

    def test_terminal_75_metric_fail_fails_lane(self):
        stub, (g_lane, overload, _) = self._run({})
        self.assertIsNone(g_lane)
        self.assertFalse(overload)
        self.assertEqual(stub.measured, [0, 12, 24, 36, 48, 60, 72, 75])

    def test_overload_at_terminal_75_fails_lane(self):
        stub, (g_lane, overload, _) = self._run({75: "overload"})
        self.assertIsNone(g_lane)
        self.assertTrue(overload)
        self.assertEqual(stub.measured[-1], 75)

    def test_overload_fails_lane_without_raising_gain(self):
        stub, (g_lane, overload, _) = self._run({24: "overload"})
        self.assertIsNone(g_lane)
        self.assertTrue(overload)
        self.assertEqual(stub.measured, [0, 12, 24])

    def test_no_candidate_fails(self):
        stub, (g_lane, overload, _) = self._run({})
        self.assertIsNone(g_lane)
        self.assertFalse(overload)
        self.assertEqual(len(stub.measured), 8)

    def test_deadline_fail_fast_before_first_window(self):
        from vdeter.stage7 import hw_operations

        original = hw_operations.read_monotonic_raw_ns
        hw_operations.read_monotonic_raw_ns = lambda: 100
        try:
            stub, (g_lane, overload, _) = self._run({0: "pass"}, deadline_ns=1)
        finally:
            hw_operations.read_monotonic_raw_ns = original
        self.assertIsNone(g_lane)
        self.assertEqual(stub.measured, [])
        self.assertTrue(
            any(e.get("kind") == "gain_adaptive_deadline" for e in stub.evidence)
        )

    def test_plan_schedule_has_read_margin_between_windows(self):
        # rr-p1 run a(2026-08-27) 실측 결함의 회귀 방지: 창 사이 여유가
        # 없으면 창당 IQ 읽기+RPC가 다음 창 시작을 침범해 창이 교대로
        # 소실된다. plan은 창 사이 ≥3s, fine 창 앞 ≥25s 여유를 보장해야
        # 하고 TX train은 fine 창 끝까지 연속으로 덮어야 한다.
        module = _load_coordinator_app()
        paths = (
            ("C1", "ROC", "MASS-A", 0, 0),
            ("C4", "MASS-B", "ROC", 1, 1),
        )
        channel_by_role = {"upper": {}, "lower": {}}
        gain_paths, next_start = module.build_adaptive_gain_paths(
            paths, 1_000_000_000_000, 3, 99, channel_by_role
        )
        self.assertEqual(len(gain_paths), 2)
        for entry in gain_paths:
            spacing = (
                240_000_000 if entry["tx_endpoint_id"] == "MASS-B"
                else 120_000_000
            )
            windows = entry["coarse_windows"]
            self.assertEqual(
                [w["gain_db"] for w in windows],
                [0, 12, 24, 36, 48, 60, 72, 75],
            )
            for w in windows:
                self.assertEqual(len(w["scheduled_host_raw_ns"]), 13)
            for before, after in zip(windows, windows[1:]):
                gap = (
                    after["scheduled_host_raw_ns"][0]
                    - before["scheduled_host_raw_ns"][-1] - spacing
                )
                self.assertGreaterEqual(gap, 3_000_000_000)
            fine = entry["fine_window"]["scheduled_host_raw_ns"]
            self.assertEqual(len(fine), 13)
            fine_gap = (
                fine[0] - windows[-1]["scheduled_host_raw_ns"][-1] - spacing
            )
            self.assertGreaterEqual(fine_gap, 25_000_000_000)
            train = entry["scheduled_host_raw_ns"]
            self.assertEqual(train[0], windows[0]["scheduled_host_raw_ns"][0])
            self.assertEqual(train[-1], fine[-1])
            self.assertEqual(entry["deadline_host_raw_ns"], 99)
        self.assertGreater(next_start, 1_000_000_000_000)


class RevalWindowJudgmentTest(unittest.TestCase):
    """v0.9.2 재검증 관용 판정 (사용자 승인 2026-08-27)."""

    def _judge(self, fails, wrong, tol):
        from vdeter.stage7.pi_runtime import evaluate_reval_window

        return evaluate_reval_window(fails, wrong, tol)

    def test_tolerance_zero_requires_perfection(self):
        self.assertTrue(self._judge([], 0, 0))
        self.assertFalse(self._judge([7], 0, 0))

    def test_isolated_failures_within_tolerance_pass(self):
        self.assertTrue(self._judge([7], 0, 2))
        self.assertTrue(self._judge([7, 40], 0, 2))

    def test_three_failures_fail(self):
        self.assertFalse(self._judge([7, 40, 90], 0, 2))

    def test_consecutive_failures_fail_even_within_tolerance(self):
        self.assertFalse(self._judge([40, 41], 0, 2))

    def test_wrong_bytes_always_fail(self):
        self.assertFalse(self._judge([], 1, 2))
        self.assertFalse(self._judge([7], 1, 2))


class _FakeClient:
    """SocketChannel용 스크립트 클라이언트: recv가 순서대로 항목을 돌려준다."""

    def __init__(self, items):
        self._items = list(items)
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout=0.0):
        if not self._items:
            return None
        item = self._items.pop(0)
        return item


def _msg(name="calibrate_rf", command_id="cid-1"):
    return {"command_name": name, "command_id": command_id}


class ChannelGuardTest(unittest.TestCase):
    """v0.9.1 E항: barrier 내부 deadline/stall 강제와 emergency 우회."""

    def _channel(self, items):
        app = _load_coordinator_app()
        return app, app.SocketChannel(_FakeClient(items))

    def test_unresponsive_channel_hits_phase_deadline(self):
        app, channel = self._channel([])
        guard = app.PhaseGuard("t", deadline_s=-0.001, stall_s=999)
        channel.attach_guard(guard)
        with self.assertRaises(RuntimeError) as ctx:
            channel.request(_msg())
        self.assertIn("PHASE_DEADLINE_EXCEEDED", str(ctx.exception))

    def test_event_only_channel_hits_stall_after_events_stop(self):
        events = [{"kind": "event", "n": i} for i in range(3)]
        app, channel = self._channel(events)
        guard = app.PhaseGuard("t", deadline_s=999, stall_s=0.05)
        channel.attach_guard(guard)
        with self.assertRaises(RuntimeError) as ctx:
            channel.request(_msg())
        self.assertIn("PHASE_PROGRESS_STALLED", str(ctx.exception))
        # 이벤트 3개는 progress로 집계된 뒤 stall이어야 한다.
        self.assertGreaterEqual(guard.progress_count, 3)

    def test_terminal_missing_channel_stalls(self):
        # 다른 command_id의 response만 오는 channel — terminal 누락.
        app, channel = self._channel(
            [{"kind": "response", "command_id": "other"}]
        )
        guard = app.PhaseGuard("t", deadline_s=999, stall_s=0.05)
        channel.attach_guard(guard)
        with self.assertRaises(RuntimeError):
            channel.request(_msg())

    def test_normal_response_returns_and_counts_progress(self):
        app, channel = self._channel(
            [{"kind": "event"}, {"kind": "response", "command_id": "cid-1"}]
        )
        guard = app.PhaseGuard("t", deadline_s=999, stall_s=999)
        channel.attach_guard(guard)
        response = channel.request(_msg())
        self.assertEqual(response["command_id"], "cid-1")
        self.assertGreaterEqual(guard.progress_count, 2)

    def test_emergency_inhibit_bypasses_tripped_guard(self):
        app, channel = self._channel(
            [{"kind": "response", "command_id": "cid-1"}]
        )
        guard = app.PhaseGuard("t", deadline_s=-0.001, stall_s=999)
        channel.attach_guard(guard)
        response = channel.request(_msg(name="emergency_inhibit"))
        self.assertEqual(response["command_id"], "cid-1")

    def test_no_guard_legacy_path_still_works(self):
        app, channel = self._channel(
            [{"kind": "response", "command_id": "cid-1"}]
        )
        response = channel.request(_msg())
        self.assertEqual(response["command_id"], "cid-1")


class CalibrationModeEnumTest(unittest.TestCase):
    def test_adaptive_mode_in_schema(self):
        from vdeter.stage7 import ipc

        schema = dict(ipc.COMMAND_BODY_FIELDS)["calibrate_rf"]
        mode_rule = dict(schema)["calibration_mode"]
        self.assertIn("ADAPTIVE_SEARCH_AND_SEAL", mode_rule)
        self.assertIn("SEARCH_AND_SEAL", mode_rule)
        self.assertIn("APPLY_AND_REVERIFY", mode_rule)


class RrBindingGateTest(unittest.TestCase):
    """v0.9.1 I항: profile/baseline/phase/run/link mismatch negative."""

    class _Args:
        def __init__(self, phase, run_id="run-x", baseline_sha256="ab" * 32):
            self.phase = phase
            self.run_id = run_id
            self.baseline_sha256 = baseline_sha256

    def _meta(self, **over):
        base = {
            "qualification_profile": "RESEARCH_READY_MINIMAL",
            "rr_phase": "rr-p1", "run_id": "run-x",
            "baseline_sha256": "ab" * 32, "implicit_retry": 0,
            "data_link_id": None,
            # 0.9.22: deadline 결속은 생성뿐 아니라 대조된다
            "phase_deadlines_s": {"rr-p1": 3600, "rr-p2-17": 4200},
        }
        base.update(over)
        return base

    def test_rr_phase_requires_rr_profile(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1"), self._meta(qualification_profile=None)
            )

    def test_full_phase_rejects_rr_profile(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(self._Args("p1"), self._meta())

    def test_baseline_sha_mismatch_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1"), self._meta(baseline_sha256="cd" * 32)
            )

    def test_phase_mismatch_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1"), self._meta(rr_phase="rr-p0")
            )

    def test_run_mismatch_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1", run_id="run-y"), self._meta()
            )

    def test_link_mismatch_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p2-17"),
                self._meta(rr_phase="rr-p2-17", data_link_id=19),
            )

    def test_valid_rr_binding_passes(self):
        app = _load_coordinator_app()
        app.validate_rr_binding(self._Args("rr-p1"), self._meta())  # no raise

    def test_missing_baseline_sha_arg_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1", baseline_sha256=None), self._meta()
            )

    # --- 0.9.22 결속 실효화 (R2 감사) ---

    def test_phase_deadline_mismatch_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1"),
                self._meta(phase_deadlines_s={"rr-p1": 2400}),
            )

    def test_phase_deadline_missing_rejected(self):
        app = _load_coordinator_app()
        with self.assertRaises(SystemExit):
            app.validate_rr_binding(
                self._Args("rr-p1"), self._meta(phase_deadlines_s={})
            )

    def test_debug_switches_rejected_for_rr(self):
        app = _load_coordinator_app()
        for env in ("STAGE7_SVCDBG_ALLOW_RX_READY", "STAGE7_SCENARIO_FILTER"):
            os.environ[env] = "1"
            try:
                with self.assertRaises(SystemExit):
                    app.validate_rr_binding(self._Args("rr-p1"), self._meta())
            finally:
                del os.environ[env]


class RawPhyFakePassGuardTest(unittest.TestCase):
    """v0.9.1 D항: raw PHY를 실제 실행하지 않고 counter만으로 RR-004 PASS 방지.

    RR-004 판정식은 (a) raw_phy phase mark 존재, (b) RX summary 6개 실재,
    (c) 전부 PASS, (d) wrong_bytes 합 0을 모두 요구한다. 여기서는 그 판정식을
    직접 검증한다.
    """

    P2_PATHS_LEN = 6

    def _rr004_pass(self, *, phase_marks, raw_summaries, status="PASS", error=None):
        raw_rx_count = sum(
            1 for s in raw_summaries
            if s.get("summary_type") == "STAGE7_RAW_PHY_SUMMARY_V1"
        )
        return (
            status == "PASS"
            and "raw_phy" in phase_marks
            and raw_rx_count == self.P2_PATHS_LEN
            and all(s.get("outcome") == "PASS" for s in raw_summaries)
            and sum(int(s.get("wrong_bytes", 0)) for s in raw_summaries) == 0
        )

    def test_counter_only_without_raw_summaries_is_not_pass(self):
        # phase mark도 없고 summary도 없음 → PASS 불가.
        self.assertFalse(self._rr004_pass(phase_marks={}, raw_summaries=[]))

    def test_phase_mark_without_summaries_is_not_pass(self):
        self.assertFalse(self._rr004_pass(
            phase_marks={"raw_phy": 1.0}, raw_summaries=[]))

    def test_six_pass_summaries_is_pass(self):
        summaries = [
            {"summary_type": "STAGE7_RAW_PHY_SUMMARY_V1", "outcome": "PASS",
             "wrong_bytes": 0} for _ in range(6)
        ]
        self.assertTrue(self._rr004_pass(
            phase_marks={"raw_phy": 1.0}, raw_summaries=summaries))

    def test_any_wrong_bytes_blocks_pass(self):
        summaries = [
            {"summary_type": "STAGE7_RAW_PHY_SUMMARY_V1", "outcome": "PASS",
             "wrong_bytes": 0} for _ in range(5)
        ] + [{"summary_type": "STAGE7_RAW_PHY_SUMMARY_V1", "outcome": "PASS",
              "wrong_bytes": 1}]
        self.assertFalse(self._rr004_pass(
            phase_marks={"raw_phy": 1.0}, raw_summaries=summaries))

    def test_five_summaries_blocks_pass(self):
        summaries = [
            {"summary_type": "STAGE7_RAW_PHY_SUMMARY_V1", "outcome": "PASS",
             "wrong_bytes": 0} for _ in range(5)
        ]
        self.assertFalse(self._rr004_pass(
            phase_marks={"raw_phy": 1.0}, raw_summaries=summaries))


class RrFinalBuilderTest(unittest.TestCase):
    def _builder(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "stage7_rr_final_builder_for_test",
            os.path.join(VDETER_ROOT, "tools", "stage7_rr_final_builder.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    #: 시험용 고정 HEAD. 빌더에 같은 값을 주입해 "audit head_commit이 현행
    #: HEAD와 일치하면 통과, 아니면 거부"라는 논리를 그대로 검증한다. 실제
    #: git을 읽으면 `.git`이 없는 배포본 같은 환경에서 시험이 아예 못 돈다
    #: (기존: `AssertionError: 시험 환경에 git HEAD 필요`).
    FIXED_HEAD = "a1b2c3d4" * 5

    def _current_head(self):
        return self.FIXED_HEAD

    def _head_provider(self):
        return lambda: self.FIXED_HEAD

    def _assert_blocked(self, result, keyword):
        # 0.9.22 R2 보강: NOT_READY만 보면 "겨냥 검사가 죽고 무관한
        # 문제가 동반돼도 통과"하는 약한 단언 — 겨냥 메시지를 함께 요구.
        self.assertNotEqual(result["final_status"], "RESEARCH_READY")
        self.assertTrue(
            any(keyword in p for p in result["consistency_problems"]),
            f"겨냥 검사 메시지 {keyword!r} 부재: "
            f"{result['consistency_problems']}",
        )

    def _verification(self, base, *, all_unchanged=True):
        # 0.9.22 계약: builder는 검증 묶음(frozen-hash-audit + 로그)을
        # 필수 결속하고 all_unchanged를 실검사한다 — 픽스처도 정합하게.
        vroot = os.path.join(base, "verification")
        os.makedirs(vroot, exist_ok=True)
        with open(os.path.join(vroot, "frozen-hash-audit.json"), "w") as handle:
            # 도구 산출물 형태 (0.9.22 R2 보강: builder가 구조·자기결속도
            # 검사 — 손으로 쓴 한 줄 JSON은 거부되고, head_commit은 현행
            # HEAD 일치·worktree_dirty=false까지 요구되므로 실측값 사용)
            json.dump({
                "all_unchanged": all_unchanged,
                "head_commit": self._current_head(),
                "worktree_dirty": False,
                "files": [{"file": "src/x.py", "unchanged": all_unchanged}],
            }, handle)
        for log in ("unit-contract.log", "integration-stage7.log"):
            with open(os.path.join(vroot, log), "w") as handle:
                handle.write("fixture log\n")
        return vroot

    def _make_evidence(self, root, statuses, *, profile="RESEARCH_READY_MINIMAL",
                       baseline="0.9.1", sha="ab" * 32, retries=0, hardware=None):
        # 0.9.21 강화 계약 반영: artifact_type·tc_id·run_id 필수 + index.json
        # entries의 sha256 상호 대조가 실검사되므로 픽스처도 정합하게 만든다.
        import hashlib as _hashlib
        os.makedirs(root, exist_ok=True)
        # 0.9.22 원본 결속 계약: root 상위에 coordinator ≥1 + runtime ≥3
        # 원본이 있어야 READY 가능 — 픽스처도 규약대로 만든다.
        parent = os.path.dirname(root)
        with open(os.path.join(parent, "coordinator-fixture.json"),
                  "w") as handle:
            json.dump({"fixture": True}, handle)
        for endpoint in ("ROC", "MASS-A", "MASS-B"):
            with open(os.path.join(parent, f"{endpoint}-runtime.json"),
                      "w") as handle:
                json.dump({"fixture": endpoint}, handle)
        entries = {}
        for tc, status in statuses.items():
            payload = {
                "artifact_type": "STAGE7_RESEARCH_READY_EVIDENCE_V1",
                "tc_id": tc, "run_id": "rr-fixture",
                "profile": profile, "baseline_version": baseline,
                "baseline_sha256": sha, "status": status,
                "counters": {"retries": retries},
                "hardware": hardware or {"usb": "x"},
                "lifecycle_generation": 1,
            }
            path = os.path.join(root, f"{tc}.json")
            with open(path, "w") as handle:
                json.dump(payload, handle)
            with open(path, "rb") as handle:
                entries[tc] = {
                    "sha256": _hashlib.sha256(handle.read()).hexdigest(),
                    "status": status,
                }
        with open(os.path.join(root, "index.json"), "w") as handle:
            json.dump({"profile": profile, "entries": entries}, handle)

    def _all_pass(self):
        s = {tc: "PASS" for tc in (
            "TC-STAGE7-RR-001", "TC-STAGE7-RR-002", "TC-STAGE7-RR-003",
            "TC-STAGE7-RR-004", "TC-STAGE7-RR-005", "TC-STAGE7-RR-006",
            "TC-STAGE7-RR-007", "TC-STAGE7-RR-008-link17", "TC-STAGE7-RR-008-link19",
        )}
        return s

    def test_tampered_missing_tc_id_blocks_ready(self):
        # 0.9.21 fail-closed 증명: tc_id 제거 변조 → 통과 불가
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            path = os.path.join(root, "TC-STAGE7-RR-005.json")
            with open(path) as handle:
                payload = json.load(handle)
            del payload["tc_id"]
            with open(path, "w") as handle:
                json.dump(payload, handle)
            self._reindex(root)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "tc_id")

    def test_tampered_artifact_missing_from_index_blocks_ready(self):
        # fail-closed 증명: index 미등재 artifact → 통과 불가 (역방향 대조)
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            index_path = os.path.join(root, "index.json")
            with open(index_path) as handle:
                index = json.load(handle)
            del index["entries"]["TC-STAGE7-RR-005"]
            with open(index_path, "w") as handle:
                json.dump(index, handle)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "미등재")

    def test_tampered_banned_counter_aliases_block_ready(self):
        # fail-closed 증명: 실측 별칭 counter 위반 → 통과 불가
        import tempfile

        app = self._builder()
        for key in ("wrong_bytes_total", "underflow_count",
                    "overflow_count", "late_command_count"):
            with tempfile.TemporaryDirectory() as base:
                root = os.path.join(base, "rr-fixture")
                self._make_evidence(root, self._all_pass())
                path = os.path.join(root, "TC-STAGE7-RR-004.json")
                with open(path) as handle:
                    payload = json.load(handle)
                payload["counters"][key] = 1
                with open(path, "w") as handle:
                    json.dump(payload, handle)
                self._reindex(root)
                result = app.build(
                    [root], os.path.join(base, "final-result.json"),
                    software_ok=True, frozen_hash_ok=True,
                    verification_root=self._verification(base),
                    head_provider=self._head_provider(),
                )
                self._assert_blocked(result, "금지 counter")
                self.assertTrue(any(
                    key in p for p in result["consistency_problems"]
                ), f"counter {key} 변조 메시지 부재")

    def _reindex(self, root):
        # 변조 후 sha 재계산 — sha 불일치가 아닌 '내용 규칙' 검사를 겨냥
        import hashlib as _hashlib
        index_path = os.path.join(root, "index.json")
        with open(index_path) as handle:
            index = json.load(handle)
        for tc in list(index["entries"].keys()):
            path = os.path.join(root, f"{tc}.json")
            with open(path, "rb") as handle:
                index["entries"][tc]["sha256"] = _hashlib.sha256(
                    handle.read()
                ).hexdigest()
        with open(index_path, "w") as handle:
            json.dump(index, handle)

    def test_all_pass_yields_research_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertEqual(result["final_status"], "RESEARCH_READY")

    def test_missing_tc_blocks_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            statuses = self._all_pass()
            del statuses["TC-STAGE7-RR-005"]
            self._make_evidence(root, statuses)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertNotEqual(result["final_status"], "RESEARCH_READY")
            self.assertIn("TC-STAGE7-RR-005", result["missing"])

    def test_one_fail_blocks_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            statuses = self._all_pass()
            statuses["TC-STAGE7-RR-004"] = "FAIL"
            self._make_evidence(root, statuses)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertEqual(result["final_status"], "RESEARCH_NOT_READY")

    def test_software_not_ok_blocks_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=False, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertNotEqual(result["final_status"], "RESEARCH_READY")

    def test_mixed_profile_blocks_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            # 프로파일이 다른 artifact 하나를 오염 주입.
            with open(os.path.join(root, "TC-STAGE7-RR-002.json"), "w") as handle:
                json.dump({
                    "profile": "FULL_QUALIFICATION", "baseline_version": "0.9.1",
                    "baseline_sha256": "ab" * 32, "status": "PASS",
                    "counters": {"retries": 0}, "hardware": {"usb": "x"},
                }, handle)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertNotEqual(result["final_status"], "RESEARCH_READY")

    def test_retry_nonzero_blocks_ready(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass(), retries=1)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertNotEqual(result["final_status"], "RESEARCH_READY")

    def test_hardware_blocked_status(self):
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            statuses = self._all_pass()
            statuses["TC-STAGE7-RR-007"] = "HARDWARE_BLOCKED"
            self._make_evidence(root, statuses)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertEqual(result["final_status"], "HARDWARE_BLOCKED")

    # --- 0.9.22 fail-closed 증명 (R1-A 적대 감사 시정) ---

    def test_tampered_unknown_status_blocks_ready(self):
        # 공격 6: "FAILED"/"fail" 등 거부목록 밖 문자열이 READY로 탈출 금지
        import tempfile

        app = self._builder()
        for forged in ("FAILED", "fail", "GREEN", "INCONCLUSIVE"):
            with tempfile.TemporaryDirectory() as base:
                root = os.path.join(base, "rr-fixture")
                statuses = self._all_pass()
                statuses["TC-STAGE7-RR-007"] = forged
                self._make_evidence(root, statuses)
                result = app.build(
                    [root], os.path.join(base, "final-result.json"),
                    software_ok=True, frozen_hash_ok=True,
                    verification_root=self._verification(base),
                    head_provider=self._head_provider(),
                )
                self._assert_blocked(result, "허용목록")

    def test_retries_inflation_blocks_ready(self):
        # 공격 4: retries 부풀리기로 duplicates 허용치 확장 금지 (상한 2)
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            path = os.path.join(root, "TC-STAGE7-RR-005.json")
            with open(path) as handle:
                payload = json.load(handle)
            payload["counters"] = {"retries": 50, "duplicates": 50}
            with open(path, "w") as handle:
                json.dump(payload, handle)
            self._reindex(root)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "retries > 상한")

    def test_run_id_path_mismatch_blocks_ready(self):
        # 공격 3: 다른 run의 artifact를 복사한 root 위장 금지 —
        # run_id("rr-fixture")가 경로 성분에 없으면 거부.
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-other")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "run 디렉터리와 불일치")

    def test_run_id_forged_to_path_noise_blocks_ready(self):
        # R2-A 실측 우회의 회귀 고정: run_id를 경로에 항상 존재하는
        # "evidence"/"research_ready"로 재작성해도 거부되어야 한다.
        import tempfile

        app = self._builder()
        for forged in ("evidence", "research_ready"):
            with tempfile.TemporaryDirectory() as base:
                root = os.path.join(base, "rr-fixture", "evidence",
                                    "research_ready")
                self._make_evidence(root, self._all_pass())
                for tc in self._all_pass():
                    path = os.path.join(root, f"{tc}.json")
                    with open(path) as handle:
                        payload = json.load(handle)
                    payload["run_id"] = forged
                    with open(path, "w") as handle:
                        json.dump(payload, handle)
                self._reindex(root)
                result = app.build(
                    [root], os.path.join(base, "final-result.json"),
                    software_ok=True, frozen_hash_ok=True,
                    verification_root=self._verification(base),
                    head_provider=self._head_provider(),
                )
                self._assert_blocked(result, "run 디렉터리와 불일치")

    def test_missing_verification_root_blocks_ready(self):
        # 공격 5: 증언 플래그만으로 READY 금지 — 검증 묶음 결속 필수
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "검증 로그 미결속")

    def test_verification_audit_false_blocks_ready(self):
        # 공격 5: frozen-hash-audit all_unchanged=false와 증언 플래그의
        # 모순은 기계 검출되어야 한다.
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base, all_unchanged=False),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "all_unchanged")

    def test_handwritten_audit_blocks_ready(self):
        # R2-A 실측: 한 줄 `{"all_unchanged": true}` 위조 audit — 구조·
        # 자기결속 검사로 거부되어야 한다.
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            vroot = self._verification(base)
            with open(os.path.join(vroot, "frozen-hash-audit.json"),
                      "w") as handle:
                handle.write('{"all_unchanged": true}')
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=vroot,
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "files 목록")
            self._assert_blocked(result, "head_commit")

    def test_stale_head_audit_blocks_ready(self):
        # 사용자 지적(2026-08-30): head_commit 형식만 보면 코드가 바뀐
        # 뒤의 낡은 검증 묶음이 통과 — 현행 HEAD 불일치는 거부되어야.
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            vroot = self._verification(base)
            audit_path = os.path.join(vroot, "frozen-hash-audit.json")
            with open(audit_path) as handle:
                audit = json.load(handle)
            audit["head_commit"] = "0" * 40
            with open(audit_path, "w") as handle:
                json.dump(audit, handle)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=vroot,
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "현행 HEAD와 불일치")

    def test_dirty_worktree_audit_blocks_ready(self):
        # 오염 트리에서 만든 감사(또는 필드 부재)는 결속 거부
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            vroot = self._verification(base)
            audit_path = os.path.join(vroot, "frozen-hash-audit.json")
            with open(audit_path) as handle:
                audit = json.load(handle)
            audit["worktree_dirty"] = True
            with open(audit_path, "w") as handle:
                json.dump(audit, handle)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=vroot,
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "worktree_dirty")

    def test_nonint_retries_counted_as_problem_not_crash(self):
        # R2-A 실측: retries="abc"가 traceback으로 죽던 것 — 문제 계상으로
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            path = os.path.join(root, "TC-STAGE7-RR-005.json")
            with open(path) as handle:
                payload = json.load(handle)
            payload["counters"]["retries"] = "abc"
            with open(path, "w") as handle:
                json.dump(payload, handle)
            self._reindex(root)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "retries 값 비정상")

    def test_verification_shas_bound_into_final(self):
        # 결속 실증: 검증 로그 sha가 최종 체인에 실제 포함되는가
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertEqual(result["final_status"], "RESEARCH_READY")
            bound = result["verification_evidence_sha256"]
            for fname in ("frozen-hash-audit.json", "unit-contract.log",
                          "integration-stage7.log"):
                self.assertIn(fname, bound)
                self.assertEqual(len(bound[fname]), 64)

    def test_missing_lifecycle_generation_blocks_ready(self):
        # 사용자 지적(2026-08-30): §17A.6 lineage/generation 일관성의 실검사
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            path = os.path.join(root, "TC-STAGE7-RR-005.json")
            with open(path) as handle:
                payload = json.load(handle)
            del payload["lifecycle_generation"]
            with open(path, "w") as handle:
                json.dump(payload, handle)
            self._reindex(root)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "lifecycle_generation 부재")

    def test_mixed_lifecycle_generation_blocks_ready(self):
        # 같은 root(=같은 run) 안의 1/99 혼재는 lineage 위반
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            path = os.path.join(root, "TC-STAGE7-RR-004.json")
            with open(path) as handle:
                payload = json.load(handle)
            payload["lifecycle_generation"] = 99
            with open(path, "w") as handle:
                json.dump(payload, handle)
            self._reindex(root)
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "혼재")

    def test_missing_source_originals_blocks_ready(self):
        # 원본(coordinator/runtime) 부재 root는 무음 누락이 아니라 거부
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root = os.path.join(base, "rr-fixture")
            self._make_evidence(root, self._all_pass())
            os.remove(os.path.join(base, "MASS-B-runtime.json"))
            result = app.build(
                [root], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self.assertNotEqual(result["final_status"], "RESEARCH_READY")
            self.assertTrue(any(
                "원본 evidence 부재" in p
                for p in result["consistency_problems"]
            ))

    def test_duplicate_tc_across_roots_blocks_ready(self):
        # last-wins 모호성 금지: 같은 TC가 복수 root에 있으면 거부
        import tempfile

        app = self._builder()
        with tempfile.TemporaryDirectory() as base:
            root_a = os.path.join(base, "a", "rr-fixture")
            root_b = os.path.join(base, "b", "rr-fixture")
            self._make_evidence(root_a, self._all_pass())
            self._make_evidence(root_b, {"TC-STAGE7-RR-001": "PASS"})
            result = app.build(
                [root_a, root_b], os.path.join(base, "final-result.json"),
                software_ok=True, frozen_hash_ok=True,
                verification_root=self._verification(base),
                head_provider=self._head_provider(),
            )
            self._assert_blocked(result, "복수 root")


if __name__ == "__main__":
    unittest.main()
