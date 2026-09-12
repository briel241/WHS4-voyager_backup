"""Normal ACK traffic with pre-MAC collision hold; real sockets, fake B210.

One submission, no retry helper and no timer/lead changes. This is not a Pi
timing qualification or three independent hardware launcher processes.
"""
import base64
import hashlib
import sys
import time
import unittest

from tests.integration.test_stage8_collision_socket import CollisionSocketTests as _Harness


@unittest.skipUnless(sys.platform.startswith("linux"), "real Unix TEST sockets")
class CollisionAckTests(unittest.TestCase):
    _stop_child = _Harness._stop_child
    setUp = _Harness.setUp

    def test_normal_ack_completes_with_collision_hold_active(self):
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.stage8_launcher_seams import LauncherDeviceClient
        from apps.stage8_collision_exchange import CollisionExchange

        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        exchange = CollisionExchange(clients)
        try:
            exchange.start()
            payload = b"stage8-ack-with-model-hold"
            result = self.controller.invoke_service_inline(
                "ROC", scenario_id="collision-ack", step_id="only-submission", service_operation="SUBMIT",
                request={"destination": 111111111, "payload_format": "test.stage0.opaque",
                         "payload_base64": base64.b64encode(payload).decode("ascii"),
                         "payload_sha256": hashlib.sha256(payload).hexdigest(), "ack_required": True})
            self.assertEqual("OK", result["body"]["call_outcome"], result)
            transfer_id = result["body"]["transfer_id"]
            deadline = time.monotonic() + 75
            state = None
            while time.monotonic() < deadline:
                exchange.observations()  # consume bounded actor events, never synthesize ACK
                status = self.controller.invoke_service_inline(
                    "ROC", scenario_id="collision-ack", step_id="status", service_operation="STATUS",
                    request={"transfer_id": transfer_id})
                self.assertEqual("OK", status["body"]["call_outcome"], status)
                state = status["body"]["transfer_state"]
                if state in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                time.sleep(0.5)
            self.assertEqual("COMPLETED", state, exchange.evidence)
            self.assertIsNone(exchange.error)
            self.assertTrue(any(r["receiver"] == "MASS-A" for r in exchange.application_receipts))
        finally:
            exchange.close()


# Importing a TestCase as a fixture must not collect its three tests again.
del _Harness
