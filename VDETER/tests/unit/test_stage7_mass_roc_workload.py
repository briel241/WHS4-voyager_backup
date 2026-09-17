"""Stage 7 coordinator의 mass-roc workload 연결부 단위 시험 (Phase B).

RF 사다리 없이 잠글 수 있는 연결점만 대상으로 한다:
- `--workload mass-roc`의 phase 제한 (rr-p2-17/19 전용)
- 기존 RR workload 경로 불변 (binding-first·RR controller spawn 명령)
- 새 MASS/ROC controller spawn command 조립
- `run_mass_roc`의 controller 주입·`mass-roc-events/final-result.json` 결과
  경로·mass_roc evidence 기록·RR TC evidence 미기록
- workload 실패 시 RuntimeError 전파 (coordinator exit code 1의 기제)
"""

import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest

import apps.coordinator_main as coordinator_main


class _FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs

    def poll(self):
        return 0


class _FakeSubprocess:
    """coordinator module의 `subprocess` 참조를 대신한다 (spawn 조립 검증)."""

    TimeoutExpired = subprocess.TimeoutExpired
    STDOUT = subprocess.STDOUT

    def __init__(self):
        self.spawned = []

    def Popen(self, command, **kwargs):  # noqa: N802 - subprocess API 모사
        process = _FakePopen(command, **kwargs)
        self.spawned.append(process)
        return process

    @staticmethod
    def close_log_handles(process):
        """spawn이 연 log handle을 닫는다 — Windows tmpdir 정리를 위해."""
        handle = process.kwargs.get("stdout")
        if handle is not None and hasattr(handle, "close"):
            handle.close()


class _FakeCoordinator:
    _channels = {}


class TC_WORKLOAD_ARGS(unittest.TestCase):
    def setUp(self):
        # 판정 표면을 바꾸는 디버그 env가 시험 결과를 오염시키지 않게 비운다.
        self._saved_env = {}
        for key in ("STAGE7_SVCDBG_ALLOW_RX_READY", "STAGE7_SCENARIO_FILTER"):
            self._saved_env[key] = os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

    def test_mass_roc_workload_rejected_outside_service_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                coordinator_main.main([
                    "--run-id", "x", "--phase", "p0", "--run-root", tmp,
                    "--workload", "mass-roc",
                ])
        self.assertIn("mass-roc workload", str(caught.exception))

    def test_rr_workload_still_binding_first(self):
        """기존 RR workload(기본값)는 workload 분기와 무관하게 결속 검증이
        먼저다 — mass-roc 추가가 RR 경로의 진입 순서를 바꾸지 않았다."""
        with tempfile.TemporaryDirectory() as tmp:
            configs = os.path.join(tmp, "configs")
            os.makedirs(configs)
            with self.assertRaises(SystemExit) as caught:
                coordinator_main.main([
                    "--run-id", "x", "--phase", "rr-p2-17", "--run-root", tmp,
                    "--configs-dir", configs,
                ])
        self.assertIn("RR_BINDING_REJECTED", str(caught.exception))


class TC_CONTROLLER_SPAWN(unittest.TestCase):
    def _with_fake_subprocess(self, call):
        fake = _FakeSubprocess()
        original = coordinator_main.subprocess
        coordinator_main.subprocess = fake
        try:
            result = call()
        finally:
            coordinator_main.subprocess = original
        return fake, result

    def test_mass_roc_controller_command_assembly(self):
        with tempfile.TemporaryDirectory() as run_root:
            fake, process = self._with_fake_subprocess(
                lambda: coordinator_main._spawn_mass_roc_controller(
                    "run-77", run_root, "/cfg-dir", "a" * 64,
                    "SC-VDE-006", "forged",
                )
            )
            self.assertEqual(len(fake.spawned), 1)
            command = process.command
            self.assertEqual(command[0], sys.executable)
            # repo-root apps/의 MASS/ROC controller를 가리킨다 (RR과 별개 파일).
            script = command[1].replace("\\", "/")
            self.assertTrue(script.endswith("apps/mass_roc_workload_controller.py"),
                            script)
            self.assertNotIn("scenario_controller.py", script)

            def value_of(flag):
                return command[command.index(flag) + 1]

            self.assertEqual(value_of("--run-id"), "run-77")
            self.assertEqual(value_of("--run-root"), run_root)
            self.assertEqual(value_of("--test-plan-sha"), "a" * 64)
            self.assertEqual(
                value_of("--fixtures-dir").replace("\\", "/"),
                "/cfg-dir/fixtures")
            self.assertEqual(value_of("--scenario-id"), "SC-VDE-006")
            self.assertEqual(value_of("--certificate-case"), "forged")
            # controller log가 RR log와 다른 파일에 쌓인다.
            self.assertTrue(os.path.isfile(
                os.path.join(run_root, "logs", "mass-roc-controller.log")))
            _FakeSubprocess.close_log_handles(process)

    def test_rr_controller_command_unchanged(self):
        with tempfile.TemporaryDirectory() as run_root:
            fake, process = self._with_fake_subprocess(
                lambda: coordinator_main._spawn_controller(
                    "run-88", run_root, "/cfg-dir", "b" * 64, "scenario",
                )
            )
            self.assertEqual(len(fake.spawned), 1)
            command = process.command
            script = command[1].replace("\\", "/")
            self.assertTrue(script.endswith("apps/scenario_controller.py"),
                            script)

            def value_of(flag):
                return command[command.index(flag) + 1]

            self.assertEqual(value_of("--run-id"), "run-88")
            self.assertEqual(value_of("--mode"), "scenario")
            self.assertEqual(value_of("--soak-seconds"), "600")
            self.assertEqual(value_of("--min-transfers"), "10")
            _FakeSubprocess.close_log_handles(process)

    def test_rr_service_phase_defaults_preserved(self):
        """`_run_rr_service_phase`의 기본값이 RR 계약 그대로다 — controller
        주입이 없으면 기존 RR controller·기존 결과 경로를 쓴다."""
        parameters = inspect.signature(
            coordinator_main._run_rr_service_phase).parameters
        self.assertIsNone(parameters["spawn_controller"].default)
        self.assertEqual(
            parameters["result_relpath"].default.replace("\\", "/"),
            "evidence/scenario_result.json")


class TC_RUN_MASS_ROC(unittest.TestCase):
    def _run(self, *, scenario_status, rc, expect_raise):
        captured = {}
        spawn_calls = []

        def fake_service_phase(coordinator, resolved, plan, run_root, **kwargs):
            captured.update(kwargs)
            return {}, {"status": scenario_status, "counters": {}}, rc

        def fake_spawn(run_id, run_root, configs_dir, test_plan_sha,
                       scenario_id, certificate_case):
            spawn_calls.append({
                "run_id": run_id, "configs_dir": configs_dir,
                "test_plan_sha": test_plan_sha, "scenario_id": scenario_id,
                "certificate_case": certificate_case,
            })
            return _FakePopen(["fake"])

        original_phase = coordinator_main._run_rr_service_phase
        original_spawn = coordinator_main._spawn_mass_roc_controller
        coordinator_main._run_rr_service_phase = fake_service_phase
        coordinator_main._spawn_mass_roc_controller = fake_spawn
        try:
            with tempfile.TemporaryDirectory() as run_root:
                call = lambda: coordinator_main.run_mass_roc(  # noqa: E731
                    _FakeCoordinator(), {}, None, run_root,
                    run_id="mr-1",
                    metadata={"git_commit": "c" * 8,
                              "baseline_sha256": "d" * 64},
                    link_id=17, gain_artifact_path=None,
                    configs_dir="/cfg", test_plan_sha="e" * 64,
                    scenario_id="SC-VDE-005", certificate_case="both",
                )
                if expect_raise:
                    with self.assertRaises(RuntimeError):
                        call()
                    outcome = None
                else:
                    outcome = call()
                    # 주입된 spawn_controller는 patch 범위 안에서 검증한다 —
                    # 밖에서 부르면 실제 process가 뜬다.
                    spawner = captured["spawn_controller"]
                    self.assertTrue(callable(spawner))
                    spawner()
                evidence_path = os.path.join(
                    run_root, "evidence", "mass_roc", "coordinator_result.json")
                with open(evidence_path, "r", encoding="utf-8") as handle:
                    evidence = json.load(handle)
                rr_evidence_dir = os.path.join(
                    run_root, "evidence", "research_ready")
                rr_evidence_absent = not os.path.exists(rr_evidence_dir)
        finally:
            coordinator_main._run_rr_service_phase = original_phase
            coordinator_main._spawn_mass_roc_controller = original_spawn
        return captured, spawn_calls, outcome, evidence, rr_evidence_absent

    def test_wiring_result_path_and_evidence_on_pass(self):
        captured, spawn_calls, outcome, evidence, rr_absent = self._run(
            scenario_status="PASS", rc=0, expect_raise=False)
        # 결과 수집 경로가 mass-roc controller의 final-result.json이다.
        self.assertEqual(
            captured["result_relpath"].replace("\\", "/"),
            "mass-roc-events/final-result.json")
        self.assertEqual(captured["mode"], "scenario")
        # 주입된 spawn이 MASS/ROC controller를 인자 그대로 띄운다
        # (_run이 patch 범위 안에서 호출해 기록).
        self.assertEqual(spawn_calls, [{
            "run_id": "mr-1", "configs_dir": "/cfg",
            "test_plan_sha": "e" * 64, "scenario_id": "SC-VDE-005",
            "certificate_case": "both",
        }])
        self.assertEqual(outcome,
                         {"mass_roc": "PASS", "link": 17,
                          "scenario_id": "SC-VDE-005"})
        # mass_roc 전용 evidence만 쓰고 RR TC evidence는 만들지 않는다.
        self.assertEqual(evidence["artifact_type"],
                         "STAGE7_MASS_ROC_COORDINATOR_RESULT_V1")
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["scenario_id"], "SC-VDE-005")
        self.assertEqual(evidence["certificate_case"], "both")
        self.assertEqual(evidence["link_id"], 17)
        self.assertEqual(evidence["baseline_sha256"], "d" * 64)
        self.assertTrue(rr_absent)

    def test_failure_raises_and_records_fail(self):
        """workload 실패는 RuntimeError로 전파된다 — main()이 exit code 1로
        바꾸는 기제 — 그리고 evidence에 FAIL로 남는다."""
        _captured, _spawns, _outcome, evidence, rr_absent = self._run(
            scenario_status="FAIL", rc=1, expect_raise=True)
        self.assertEqual(evidence["status"], "FAIL")
        self.assertTrue(rr_absent)


if __name__ == "__main__":
    unittest.main()
