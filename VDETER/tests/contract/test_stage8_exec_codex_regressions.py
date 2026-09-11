"""Independent boundary regressions: absence, malformed returns and partial cleanup."""
import unittest

from vdeter.stage8.execution.lease import MassBLease
from vdeter.stage8.execution.runner import WorkloadRunner, WorkloadSpec, scenario_started
from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict, ActionExecution
from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.testing.execution_fakes import FakeTxExecutor
from .test_stage8_exec_fault_matrix import SNAPSHOT, _FaultConn


class RunnerBoundaryTest(unittest.TestCase):
    def make(self, conn=None, lease=None):
        conn = conn or _FaultConn(None, RuntimeError)
        lease = lease or MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=SNAPSHOT,
                                tx_executor=FakeTxExecutor())
        return runner, conn, lease

    def run_case(self, conn, *, steps=(), lease=None):
        runner, conn, lease = self.make(conn, lease)
        result = runner.run(WorkloadSpec(steps=steps, scenario_verdict=ScenarioVerdict.PASS))
        self.assertNotEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertIs(result, runner.finish(ScenarioVerdict.PASS))
        self.assertEqual(conn.close_calls, 1)
        lease.assert_returned()
        return result

    def test_empty_workload_query_interrupt_is_rethrown_after_cleanup(self):
        for fault in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(fault=fault):
                runner, conn, lease = self.make(_FaultConn("infrastructure_error", fault))
                with self.assertRaises(fault):
                    runner.run(WorkloadSpec())
                result = runner.finish(ScenarioVerdict.PASS)
                self.assertNotEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
                self.assertEqual(conn.close_calls, 1)
                lease.assert_returned()

    def test_not_armed_is_error(self):
        class Conn(_FaultConn):
            def start(self):
                pass
        result = self.run_case(Conn(None, RuntimeError))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ERROR)

    def test_never_idle_is_not_completed(self):
        class Conn(_FaultConn):
            def poll_new_results(self):
                return []
            def client_idle(self):
                return False
        result = self.run_case(Conn(None, RuntimeError), steps=(scenario_started(),))
        self.assertNotIn("QUIESCENCE", result.lifecycle_transitions)

    def test_close_malformed_return_is_result_not_raw_exception(self):
        for value in (None, [], "closed", 1, {}, {"engine": {}}, {"engine": None}):
            with self.subTest(value=value):
                class Conn(_FaultConn):
                    def close(self):
                        self.close_calls += 1
                        return value
                self.run_case(Conn(None, RuntimeError))

    def test_close_explicit_residue_is_not_success(self):
        for value in ({"child_alive": True}, {"engine": {"inflight_actions": 1}},
                      {"socket_file_exists": True}, {"workload_dir_exists": True}):
            with self.subTest(value=value):
                class Conn(_FaultConn):
                    def close(self):
                        self.close_calls += 1
                        return value
                result = self.run_case(Conn(None, RuntimeError))
                self.assertNotIn("CLOSED", result.lifecycle_transitions)

    def test_close_exception_does_not_fabricate_forced_kill(self):
        result = self.run_case(_FaultConn("close", RuntimeError))
        self.assertNotIn("forced_kill", result.evidence["cleanup"])

    def test_late_query_interrupt_is_not_swallowed(self):
        class Conn(_FaultConn):
            @property
            def infrastructure_error(self):
                if self.close_calls:
                    raise KeyboardInterrupt()
                return None
        runner, conn, lease = self.make(Conn(None, RuntimeError))
        with self.assertRaises(KeyboardInterrupt):
            runner.run(WorkloadSpec())
        self.assertEqual(runner.finish(ScenarioVerdict.PASS).poc_infrastructure,
                         PocInfrastructure.ABORTED)
        lease.assert_returned()

    def test_original_interrupt_survives_second_interrupt_during_close(self):
        original = KeyboardInterrupt()
        class Conn(_FaultConn):
            def start(self):
                raise original
            def close(self):
                self.close_calls += 1
                raise SystemExit(0)
        runner, conn, lease = self.make(Conn(None, RuntimeError))
        with self.assertRaises(KeyboardInterrupt) as caught:
            runner.run(WorkloadSpec())
        self.assertIs(caught.exception, original)
        self.assertEqual(conn.close_calls, 1)
        lease.assert_returned()

    def test_lease_release_failure_is_recorded(self):
        class Lease(MassBLease):
            def release(self, workload_id=None):
                raise RuntimeError("release failed")
        result = self.run_case(_FaultConn(None, RuntimeError), lease=Lease())
        self.assertEqual(result.infrastructure_error_code,
                         PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN.value)

    def test_other_workload_lease_is_preserved(self):
        lease = MassBLease()
        lease.acquire("other")
        runner, conn, _ = self.make(lease=lease)
        result = runner.run(WorkloadSpec())
        self.assertEqual(lease.holder, "other")
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ERROR)

    def test_deliver_after_finish_does_not_emit(self):
        runner, conn, lease = self.make()
        runner.run(WorkloadSpec())
        with self.assertRaises(PocError):
            runner.deliver(scenario_started())
        with self.assertRaises(PocError):
            runner.arm()
        lease.assert_returned()

    def test_not_running_does_not_drop_event_as_success(self):
        class Conn(_FaultConn):
            @property
            def running(self):
                return False
        self.run_case(Conn(None, RuntimeError), steps=(scenario_started(),))

    def test_malformed_results_never_reach_executor(self):
        for value in (None, {}, [None], [{"request_id": "r", "admission": "typo"}],
                      [{"admission": "accepted"}], [{}] * 33):
            with self.subTest(value=value):
                class Conn(_FaultConn):
                    def poll_new_results(self):
                        return value
                self.run_case(Conn(None, RuntimeError), steps=(scenario_started(),))

    def test_duplicate_result_in_batch_executes_once(self):
        class Conn(_FaultConn):
            def poll_new_results(self):
                values = super().poll_new_results()
                return values + values
        runner, conn, _ = self.make(Conn(None, RuntimeError))
        executor = FakeTxExecutor()
        runner._executor = executor
        result = runner.run(WorkloadSpec(steps=(scenario_started(),)))
        self.assertEqual(executor.calls, 1)
        self.assertEqual(result.attacks_requested, 1)

    def test_cleanup_more_severe_error_retains_original_code(self):
        class Conn(_FaultConn):
            def close(self):
                self.close_calls += 1
                raise PocError(PocErrorCode.POC_DEVICE_HANDLE_UNCERTAIN)
        result = self.run_case(Conn("start", RuntimeError))
        self.assertEqual(result.infrastructure_error_code,
                         PocErrorCode.POC_DEVICE_HANDLE_UNCERTAIN.value)
        self.assertIn("EXCEPTION:RuntimeError", result.evidence["failure_codes"])

    def test_incremental_delivery_failure_cannot_become_completed(self):
        runner, conn, lease = self.make(_FaultConn("emit_event", RuntimeError))
        runner.arm()
        with self.assertRaises(RuntimeError):
            runner.deliver(scenario_started())
        result = runner.finish(ScenarioVerdict.PASS)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        self.assertNotIn("WORKLOAD_TERMINAL", result.lifecycle_transitions)
        lease.assert_returned()

    def test_invalid_accepted_binding_does_not_execute(self):
        for change in ({"transfer_id": ""}, {"broadcast_parent_id": None},
                       {"physical_channel": "upper"}):
            with self.subTest(change=change):
                class Conn(_FaultConn):
                    def poll_new_results(self):
                        values = super().poll_new_results()
                        for item in values:
                            item.update(change)
                        return values
                runner, _, _ = self.make(Conn(None, RuntimeError))
                executor = FakeTxExecutor()
                runner._executor = executor
                result = runner.run(WorkloadSpec(steps=(scenario_started(),)))
                self.assertEqual(executor.calls, 0)
                self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
                self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)

    def test_executor_exception_retains_parent_and_unknown_terminal(self):
        runner, _, _ = self.make()
        def broken(action, result):
            raise RuntimeError("lost terminal")
        runner._executor = broken
        result = runner.run(WorkloadSpec(steps=(scenario_started(),)))
        self.assertEqual(result.attacks[0].broadcast_parent_id, "p")
        self.assertEqual(result.attacks[0].transfer_id, "t")
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)
        self.assertEqual(result.infrastructure_error_code, "POC_TX_OUTCOME_UNKNOWN")

    def test_callback_timeout_preserves_already_accepted_action(self):
        from vdeter.stage8.testing.execution_fakes import LoopbackConnection
        from vdeter.stage8.testing.fakes import FakeClock, build_manifest
        from vdeter.stage8.testing.client_runner import RecordingCallbacks
        clock = FakeClock()
        class Callbacks(RecordingCallbacks):
            def on_event(self, event, context):
                clock.advance(2)
                return [context.replay(target="ROC")]
        conn = LoopbackConnection(callbacks=Callbacks(), manifest=build_manifest(),
                                  clock=clock, auto_ack=False)
        runner, _, lease = self.make(conn)
        executor = FakeTxExecutor()
        runner._executor = executor
        result = runner.run(WorkloadSpec(steps=(scenario_started(),)))
        self.assertEqual(executor.calls, 0)
        self.assertEqual(result.attacks_requested, 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)
        self.assertTrue(result.attacks[0].broadcast_parent_id)
        self.assertTrue(result.attacks[0].transfer_id)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        lease.assert_returned()


if __name__ == "__main__":
    unittest.main()
