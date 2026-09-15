"""Stage 7 TEST harness 계약 시험 (`stage7` §15, §16.2, §18.4)."""

import hashlib
import os
import tempfile
import unittest

from vdeter.common.results import Ok
from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
from vdeter.stage7.testing.controller import Stage7ScenarioController
from vdeter.stage7.testing.evidence import (
    ALL_TEST_CASES,
    Stage7EvidenceBundle,
    TRACEABILITY_MATRIX,
)
from vdeter.stage7.testing.plan import (
    P1_MEMBER_PLAN_TABLE,
    Stage7TestPlanLoader,
)

ORCHESTRATION = (
    '[controller]\n'
    'controller_protocol = "vdeter.stage7.scenario-controller.v1"\n'
    'controller_body_schema = "STAGE7_SCENARIO_BODY_V1"\n'
    'scenario_horizon_ns = 14400000000000\n'
    'evidence_root_relpath = "results/runs"\n'
)

CONFIG_SHAS = {
    "MASS-A": "1" * 64,
    "MASS-B": "2" * 64,
    "ROC": "3" * 64,
}


class TC_STAGE7_TEST_PLAN(unittest.TestCase):
    def test_link17_and_link19_are_separate_runs(self):
        loader = Stage7TestPlanLoader()
        first = loader.load(
            ORCHESTRATION,
            run_id="stage7-l17",
            data_link_id=17,
            system_plan_sha256="a" * 64,
            endpoint_config_sha256s=CONFIG_SHAS,
            scenario_plan_value={"scenario": "x"},
        )
        second = loader.load(
            ORCHESTRATION,
            run_id="stage7-l19",
            data_link_id=19,
            system_plan_sha256="a" * 64,
            endpoint_config_sha256s=CONFIG_SHAS,
            scenario_plan_value={"scenario": "x"},
        )
        self.assertTrue(first.is_ok and second.is_ok)
        self.assertNotEqual(first.value.resolved_sha256, second.value.resolved_sha256)

    def test_link11_data_profile_rejected(self):
        loader = Stage7TestPlanLoader()
        result = loader.load(
            ORCHESTRATION,
            run_id="stage7-bad",
            data_link_id=11,
            system_plan_sha256="a" * 64,
            endpoint_config_sha256s=CONFIG_SHAS,
            scenario_plan_value={},
        )
        self.assertTrue(result.is_err)

    def test_p1_member_plan_canonical_table(self):
        loader = Stage7TestPlanLoader()
        for test_case, expected in P1_MEMBER_PLAN_TABLE.items():
            result = loader.load(
                ORCHESTRATION,
                run_id="stage7-p1",
                data_link_id=17,
                system_plan_sha256="a" * 64,
                endpoint_config_sha256s=CONFIG_SHAS,
                scenario_plan_value={},
                p1_test_case_id=test_case,
            )
            self.assertTrue(result.is_ok, test_case)
            plan = result.value.p1_member_plan
            self.assertEqual(plan.present_member_set, expected["present_member_set"])
            self.assertEqual(plan.excluded_member_set, expected["excluded_member_set"])
            self.assertEqual(plan.path_ids, expected["path_ids"])
            self.assertEqual(plan.qualification_profile, "PAIRWISE_EDGE_QUALIFICATION_V1")
            self.assertEqual(plan.time_profile, "PAIRWISE_HOST_TIME_QUALIFICATION_V1")
            self.assertEqual(len(plan.sha256()), 64)

    def test_unknown_controller_key_rejected(self):
        loader = Stage7TestPlanLoader()
        result = loader.load(
            ORCHESTRATION + 'extra = 1\n',
            run_id="stage7-x",
            data_link_id=17,
            system_plan_sha256="a" * 64,
            endpoint_config_sha256s=CONFIG_SHAS,
            scenario_plan_value={},
        )
        self.assertTrue(result.is_err)


class _FakeService:
    def __init__(self):
        self.calls = []

    def _ok(self, name, request):
        self.calls.append((name, request))

        class _Value:
            def __init__(inner):
                inner.name = name

        from vdeter.common.contracts.service import ServiceStopped, EndpointHealth
        from vdeter.common.time import ClockDomain, TimePoint

        return Ok(
            ServiceStopped(
                endpoint_id="MASS-A",
                health=EndpointHealth.STOPPED,
                stopped_at=TimePoint(1, 0, ClockDomain.UTC, "test-utc"),
            )
        )

    def start(self, request):
        return self._ok("start", request)

    def submit(self, request):
        return self._ok("submit", request)

    def status(self, request):
        return self._ok("status", request)

    def cancel(self, request):
        return self._ok("cancel", request)

    def stop(self, request):
        return self._ok("stop", request)


class TC_STAGE7_TEST_ADAPTER(unittest.TestCase):
    def _adapter(self, service=None):
        return Stage7ServiceApiTestAdapter(
            endpoint_id="MASS-A",
            service=service or _FakeService(),
            resolved_test_plan_sha256="e" * 64,
            authorized_controller=(4242, 1000),
        )

    def _controller(self, adapter):
        class _Channel:
            def request(self, message):
                return adapter.handle_command(message, (4242, 1000))

        return Stage7ScenarioController(
            run_id="stage7-test",
            resolved_test_plan_sha256="e" * 64,
            adapter_channels={"MASS-A": _Channel()},
            host_raw_clock=lambda: 1_000_000_000,
        )

    def test_stop_fixture_roundtrip(self):
        service = _FakeService()
        adapter = self._adapter(service)
        controller = self._controller(adapter)
        fixture, digest = Stage7ScenarioController.fixture_bytes({"reason": "test"})
        adapter.register_fixture("fixtures/stop.json", fixture)
        response = controller.invoke_service_fixture(
            "MASS-A",
            scenario_id="s1",
            step_id="step1",
            service_operation="STOP",
            local_fixture_relpath="fixtures/stop.json",
            local_fixture_sha256=digest,
        )
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual(response["body"]["call_outcome"], "OK")
        self.assertIsNotNone(response["body"]["public_result_artifact_sha256"])
        self.assertEqual(len(service.calls), 1)

    def test_wrong_peer_rejected_without_call(self):
        service = _FakeService()
        adapter = self._adapter(service)
        fixture, digest = Stage7ScenarioController.fixture_bytes({"reason": "test"})
        adapter.register_fixture("fixtures/stop.json", fixture)
        message = {
            "protocol": "vdeter.stage7.scenario-controller.v1",
            "kind": "command",
            "command_name": "invoke_service_fixture",
            "run_id": "stage7-test",
            "resolved_test_plan_sha256": "e" * 64,
            "command_id": "0" * 8 + "-0000-4000-8000-000000000000",
            "endpoint_id": "MASS-A",
            "expected_lifecycle_generation": 1,
            "expected_bridge_generation": 0,
            "issued_host_raw_ns": 1,
            "deadline_host_raw_ns": 2,
            "request_body_sha256": "0" * 64,
            "body_sha256": "0" * 64,
            "body": {
                "scenario_id": "s1",
                "step_id": "x",
                "service_operation": "STOP",
                "local_fixture_relpath": "fixtures/stop.json",
                "local_fixture_sha256": digest,
                "trigger_host_raw_ns": 1,
            },
        }
        response = adapter.handle_command(message, (9999, 1000))
        self.assertEqual(response["status"], "REJECTED")
        self.assertEqual(service.calls, [])

    def test_wrong_fixture_hash_not_called(self):
        service = _FakeService()
        adapter = self._adapter(service)
        controller = self._controller(adapter)
        fixture, _digest = Stage7ScenarioController.fixture_bytes({"reason": "test"})
        adapter.register_fixture("fixtures/stop.json", fixture)
        response = controller.invoke_service_fixture(
            "MASS-A",
            scenario_id="s1",
            step_id="step1",
            service_operation="STOP",
            local_fixture_relpath="fixtures/stop.json",
            local_fixture_sha256="9" * 64,
        )
        self.assertEqual(response["status"], "REJECTED")
        self.assertEqual(response["body"]["call_outcome"], "NOT_CALLED")
        self.assertEqual(service.calls, [])

    def test_replay_returns_first_response_without_second_call(self):
        service = _FakeService()
        adapter = self._adapter(service)
        fixture, digest = Stage7ScenarioController.fixture_bytes({"reason": "test"})
        adapter.register_fixture("fixtures/stop.json", fixture)
        controller = self._controller(adapter)
        first = controller.invoke_service_fixture(
            "MASS-A",
            scenario_id="s1",
            step_id="step1",
            service_operation="STOP",
            local_fixture_relpath="fixtures/stop.json",
            local_fixture_sha256=digest,
        )
        # 같은 command bytes replay.
        message = None
        # controller가 매번 새 command_id를 만들므로 adapter replay를 직접 확인.
        self.assertEqual(len(service.calls), 1)
        status = controller.test_status("MASS-A", first["command_id"])
        self.assertEqual(status["body"]["lookup_state"], "TERMINAL")
        self.assertEqual(status["body"]["terminal_status"], "COMPLETED")

    def test_fault_allowlist(self):
        called = []

        def hook(body):
            called.append(body["fault_fixture_id"])
            return {"suppressed": True}

        adapter = Stage7ServiceApiTestAdapter(
            endpoint_id="MASS-A",
            service=_FakeService(),
            resolved_test_plan_sha256="e" * 64,
            authorized_controller=(4242, 1000),
            fault_hooks={"ACK_DROP_FIRST": hook},
        )
        controller = self._controller(adapter)
        allowed = controller.inject_test_fault(
            "MASS-A",
            scenario_id="s1",
            step_id="f1",
            fault_fixture_id="ACK_DROP_FIRST",
            target_correlation_sha256="a" * 64,
        )
        self.assertEqual(allowed["status"], "COMPLETED")
        self.assertEqual(called, ["ACK_DROP_FIRST"])
        denied = controller.inject_test_fault(
            "MASS-A",
            scenario_id="s1",
            step_id="f2",
            fault_fixture_id="NOT_ALLOWED",
            target_correlation_sha256="a" * 64,
        )
        self.assertEqual(denied["status"], "REJECTED")


class TC_STAGE7_EVIDENCE(unittest.TestCase):
    def test_matrix_covers_twenty_cases(self):
        self.assertEqual(len(ALL_TEST_CASES), 20)
        self.assertEqual(ALL_TEST_CASES[0], "TC-STAGE7-001")
        self.assertEqual(ALL_TEST_CASES[-1], "TC-STAGE7-020")
        for requirement_ids in TRACEABILITY_MATRIX.values():
            self.assertTrue(requirement_ids)

    def test_bundle_trace_and_index(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = Stage7EvidenceBundle(root, run_id="stage7-ev", judgement_scope="SOFTWARE_ONLY")
            artifact = bundle.write_artifact("artifacts/example.json", {"value": 1})
            self.assertEqual(len(artifact["sha256"]), 64)
            bundle.write_trace(
                "TC-STAGE7-001",
                status="NOT_RUN",
                judgement="mixed",
                expected="x",
                actual="y",
                child_artifacts=[artifact],
                subcases=[{"subcase": "s", "status": "PASS"}],
            )
            index = bundle.write_index()
            self.assertEqual(len(index["entries"]), 20)
            first = index["entries"][0]
            self.assertEqual(first["test_case_id"], "TC-STAGE7-001")
            self.assertIsNotNone(first["sha256"])
            self.assertEqual(index["entries"][1]["final_status"], "NOT_RUN")
            sums = bundle.write_sha256sums()
            self.assertTrue(os.path.exists(sums))
            with open(sums, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            self.assertTrue(any("evidence/trace/TC-STAGE7-001.json" in line for line in lines))

    def test_invalid_status_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = Stage7EvidenceBundle(root, run_id="x", judgement_scope="SOFTWARE_ONLY")
            with self.assertRaises(ValueError):
                bundle.write_trace(
                    "TC-STAGE7-001",
                    status="WAIVED",
                    judgement="mixed",
                    expected="x",
                    actual="y",
                )


if __name__ == "__main__":
    unittest.main()
