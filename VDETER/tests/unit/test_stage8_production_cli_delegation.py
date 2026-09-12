import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from apps.standalone import run_cmd
from apps.standalone.local_session import LocalCoordinatorSession


class ProductionCliDelegationTests(unittest.TestCase):
    def test_real_session_declares_coordinator_ownership(self):
        self.assertIs(True, LocalCoordinatorSession.poc_workloads_in_coordinator)

    def test_production_poc_uses_same_coordinator_path_without_rf_off_flag(self):
        session = NS(poc_workloads_in_coordinator=True)
        context = {"rf_off_software": False}
        with patch.object(run_cmd, "_run_workload_rf_off_poc", return_value={"from": "coordinator"}) as delegate, \
                patch.object(run_cmd, "_prepare_poc") as local:
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0,
                                           poc_ctx=context)
        self.assertEqual({"from": "coordinator"}, result)
        delegate.assert_called_once()
        local.assert_not_called()

    def test_poc_off_never_delegates_to_poc(self):
        session = Mock(poc_workloads_in_coordinator=True)
        session.wait_result.return_value = {"status": "PASS", "quiescence": {"pass": True}}
        with patch.object(run_cmd, "_run_workload_rf_off_poc") as delegate:
            run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0, poc_ctx=None)
        delegate.assert_not_called()
