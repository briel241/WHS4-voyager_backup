"""Provider failures before ACTION_RESULT must survive the real engine/runner seam."""
import json
import unittest

from vdeter.stage8.execution.runner import scenario_started, WorkloadRunner, WorkloadSpec
from vdeter.stage8.execution.lease import MassBLease
from vdeter.stage8.execution.verdict import ActionExecution, PocInfrastructure, WorkloadResult, ScenarioVerdict
from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.testing.client_runner import RecordingCallbacks
from vdeter.stage8.testing.execution_fakes import FakeTxExecutor, LoopbackConnection
from vdeter.stage8.testing.fakes import build_manifest
from .test_stage8_exec_runner import _run, _snap


class FailingProvider:
    def __init__(self, code=PocErrorCode.POC_TX_OUTCOME_UNKNOWN, detail=None):
        self.code = code
        self.detail = detail if detail is not None else {
            "broadcast_parent_id": "parent-uncertain", "transfer_id": "tx-uncertain"}
        self.calls = 0

    def submit(self, action, *, request_id):
        self.calls += 1
        raise PocError(self.code, "provider could not confirm commit", detail=self.detail)


class UnknownSubmissionTest(unittest.TestCase):
    def case(self, provider=None):
        provider = provider or FailingProvider()
        def actions(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC", payload={"packet_hex": "00"})]
            return []
        executor = FakeTxExecutor()
        result, conn, lease = _run(RecordingCallbacks(event_action_fn=actions),
                                  provider=provider, executor=executor,
                                  steps=(scenario_started(),))
        return result, conn, lease, provider, executor

    def test_unknown_before_accepted_result_retains_parent_and_attempt(self):
        result, conn, lease, provider, executor = self.case()
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        self.assertEqual(result.infrastructure_error_code, "POC_TX_OUTCOME_UNKNOWN")
        self.assertEqual(result.attacks_requested, 1)
        attack = result.attacks[0]
        self.assertEqual(attack.action_execution, ActionExecution.TX_UNKNOWN)
        self.assertEqual((attack.broadcast_parent_id, attack.transfer_id),
                         ("parent-uncertain", "tx-uncertain"))
        self.assertEqual((attack.action_kind, attack.target, attack.physical_channel),
                         ("REPLAY_PACKET", "ROC", "lower"))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(conn.server.evidence["provider_submit_calls"], 1)
        self.assertEqual(conn.server.evidence["actions_accepted"], 0)
        self.assertEqual(result.evidence["uncertain_submissions"][0]["admission"], "unknown")
        lease.assert_returned()

    def test_server_binds_request_without_mutating_provider_error_detail(self):
        detail = {"broadcast_parent_id": "p", "transfer_id": "t",
                  "request_id": "wrong-provider-request", "action_attempt": {"request_id": "wrong"}}
        result, conn, *_ = self.case(FailingProvider(detail=detail))
        self.assertEqual(result.attacks_requested, 1)
        self.assertNotIn(result.attacks[0].request_id, ("wrong", "wrong-provider-request"))
        self.assertEqual(detail["action_attempt"], {"request_id": "wrong"})
        self.assertEqual(result.attacks[0].request_id,
                         conn.server.fatal_error.detail["action_attempt"]["request_id"])

    def test_roundtrip_preserves_unknown_without_duplicate_or_payload_export(self):
        result, conn, *_ = self.case()
        restored = WorkloadResult.from_json_value(json.loads(json.dumps(result.as_json_value())))
        self.assertEqual(restored.attacks_requested, 1)
        self.assertEqual(restored.attacks[0].transfer_id, "tx-uncertain")
        self.assertEqual(len(restored.evidence["uncertain_submissions"]), 1)
        self.assertNotIn("packet_hex", json.dumps(restored.evidence["uncertain_submissions"]))
        self.assertEqual(conn.server.evidence["actions_accepted"], 0)

    def test_prefire_rejection_is_not_unknown_and_counts_provider_attempt(self):
        result, conn, _, provider, executor = self.case(
            FailingProvider(PocErrorCode.POC_RADIO_RESOURCE_CONFLICT))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.attacks_requested, 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.REJECTED_BEFORE_TX)
        self.assertEqual(conn.server.evidence["provider_submit_calls"], 1)
        self.assertEqual(result.evidence.get("uncertain_submissions", []), [])
        self.assertEqual(executor.calls, 0)

    def test_missing_or_malformed_ids_never_fabricate_confirmation(self):
        for detail in ({}, {"broadcast_parent_id": [], "transfer_id": 42}):
            with self.subTest(detail=detail):
                result, *_ = self.case(FailingProvider(detail=detail))
                self.assertEqual(result.attacks_requested, 1)
                self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)
                self.assertIsNone(result.attacks[0].broadcast_parent_id)
                self.assertIsNone(result.attacks[0].transfer_id)

    def test_interrupted_provider_keeps_original_interrupt_and_known_ids(self):
        for kind in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(kind=kind):
                original = kind("commit interrupted")
                class InterruptedProvider:
                    def submit(self, action, *, request_id):
                        raise original from PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN, detail={
                            "broadcast_parent_id": "p-interrupt", "transfer_id": "t-interrupt"})
                def actions(event, ctx):
                    return [ctx.replay(target="ROC")] if event.type == "SCENARIO_STARTED" else []
                manifest = build_manifest()
                conn = LoopbackConnection(callbacks=RecordingCallbacks(event_action_fn=actions),
                                          manifest=manifest, provider=InterruptedProvider())
                lease, executor = MassBLease(), FakeTxExecutor()
                runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w", scenario_id="SC-VDE-006",
                                        snapshot=_snap(manifest), tx_executor=executor)
                with self.assertRaises(kind) as raised:
                    runner.run(WorkloadSpec(steps=(scenario_started(),)))
                self.assertIs(raised.exception, original)
                result = runner.finish(ScenarioVerdict.PASS)
                self.assertEqual(result.infrastructure_error_code, "POC_TX_OUTCOME_UNKNOWN")
                self.assertIn("EXCEPTION:" + kind.__name__, result.evidence["failure_codes"])
                self.assertEqual(result.attacks_requested, 1)
                self.assertEqual(result.attacks[0].transfer_id, "t-interrupt")
                self.assertEqual(len(result.evidence["uncertain_submissions"]), 1)
                self.assertEqual(executor.calls, 0)
                self.assertEqual(conn.server.evidence["provider_submit_calls"], 1)
                lease.assert_returned()


if __name__ == "__main__":
    unittest.main()
