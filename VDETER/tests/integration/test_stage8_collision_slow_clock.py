"""Same collision path with a deliberately slower device clock, not retries."""
import sys
import unittest
from tests.integration import test_stage8_collision_socket as _fixture


@unittest.skipUnless(sys.platform.startswith("linux"), "real Unix TEST sockets")
class SlowClockCollisionTest(unittest.TestCase):
    time_acceleration = 0.5
    _stop_child = _fixture.CollisionSocketTests._stop_child
    setUp = _fixture.CollisionSocketTests.setUp
    _wait_for_model = _fixture.CollisionSocketTests._wait_for_model
    test_overhear_and_overlap_at_half_speed = _fixture.CollisionSocketTests.test_real_phy_overhear_survives_local_gate


@unittest.skipUnless(sys.platform.startswith("linux"), "real Unix TEST sockets")
class StalledClockActivationTest(unittest.TestCase):
    # Two 160 ms device ticks exceed the unchanged 1 s wall barrier budget.
    # Expected behavior is rejection, not enlarging production timeouts.
    time_acceleration = 0.125
    _stop_child = _fixture.CollisionSocketTests._stop_child
    setUp = _fixture.CollisionSocketTests.setUp

    def test_stalled_clock_fails_closed_and_returns_activation_resources(self):
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.stage8_launcher_seams import LauncherDeviceClient
        from apps.stage8_collision_exchange import CollisionExchange
        from vdeter.poc.api.errors import PocError, Severity
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        exchange = CollisionExchange(clients)
        try:
            exchange.start()
            self.assertFalse(hasattr(exchange, "thread"))
            exchange.poll()
        finally:
            exchange.close()
        self.assertFalse(exchange.enabled)
        self.assertTrue(exchange.evidence["cleanup_complete"])
