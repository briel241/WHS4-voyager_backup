"""Interrupted commit keeps operator control and exposes known outcome identity."""
import unittest

from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind
from vdeter.stage8.shared_medium.provider import SharedMediumActionProvider
from vdeter.stage8.shared_medium import testing as TD


class InterruptedCorrelationTest(unittest.TestCase):
    def test_all_operator_interrupts_keep_identity_in_structured_cause(self):
        for kind in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(kind=kind):
                original = kind("during commit")
                class Sink(TD.CountingRadioSink):
                    def commit_group(self, *args, **kwargs):
                        super().commit_group(*args, **kwargs)
                        raise original
                sink = Sink()
                provider = SharedMediumActionProvider(radio_sink=sink,
                    rx_readiness=TD.ReadyReadiness(), arbiter=TD.FakeArbiter(), allow_rf_off_defaults=True)
                with self.assertRaises(kind) as raised:
                    provider.submit(ActionDescriptor(kind=ActionKind.REPLAY_PACKET,
                                    target="ROC", payload={"packet_hex": "00"}), request_id="r")
                self.assertIs(raised.exception, original)
                cause = raised.exception.__cause__
                self.assertIsInstance(cause, PocError)
                self.assertEqual(cause.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
                parent = provider.parent_for("r")
                self.assertEqual(cause.detail["broadcast_parent_id"], parent.parent_id)
                self.assertEqual(cause.detail["transfer_id"], parent.transfer_id)
                self.assertEqual(provider.active_group_count, 1)
                self.assertEqual(sink.commit_calls, 1)
                provider.cleanup()


if __name__ == "__main__":
    unittest.main()
