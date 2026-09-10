"""Stage 8 reconnect·replay·gap·unresolved 복구·capability 불변 시험
(명세 §8.3, §9.3, §12; REQ-12, REQ-13, REQ-16, REQ-20, REQ-27). fake clock, socket 불필요.
"""

from __future__ import annotations

import unittest

from vdeter.stage8._supervisor.engine import EngineState
from vdeter.stage8.poc_api import protocol
from vdeter.stage8.poc_api.errors import PocErrorCode
from vdeter.stage8.poc_api.messages import parse_envelope
from vdeter.stage8.testing import FakeActionSubmitProvider, FakeClock
from vdeter.stage8.testing.server_harness import (
    ack_message,
    action_submit_message,
    drive_to_running,
    hello_message,
    make_engine,
)


def running(provider=None):
    clock = FakeClock(1000.0)
    provider = provider if provider is not None else FakeActionSubmitProvider()
    eng = make_engine(provider=provider)
    drive_to_running(eng, clock)
    return eng, clock, provider


class ReconnectTest(unittest.TestCase):
    def test_S8_POC_API_REQ_12_reconnect_replays_unacked(self):
        eng, clock, _ = running()
        for i in range(3):
            eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic())
        eng.drain(clock.monotonic())
        eng.receive(parse_envelope(ack_message(1)), clock.monotonic())  # ring keeps 2,3
        eng.begin_reconnect_window(clock.monotonic())
        self.assertEqual(eng.state, EngineState.RECONNECTING)
        out = eng.receive(
            parse_envelope(hello_message(connect_kind="reconnect", last_completed=1)),
            clock.monotonic(),
        )
        self.assertEqual(out[0]["type"], protocol.WELCOME)
        self.assertEqual(out[0]["body"]["connection_epoch"], 2)
        replayed = [m["body"]["sequence"] for m in out if m["type"] == protocol.EVENT]
        self.assertEqual(replayed, [2, 3])
        self.assertEqual(eng.connection_epoch, 2)
        self.assertEqual(eng.state, EngineState.RUNNING)
        self.assertEqual(eng.evidence["reconnects"], 1)

    def test_S8_POC_API_REQ_13_replay_gap_aborts(self):
        eng, clock, _ = running()
        for i in range(5):
            eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic())
        eng.drain(clock.monotonic())
        eng.receive(parse_envelope(ack_message(3)), clock.monotonic())  # 1..3 evicted, ring=4,5
        eng.begin_reconnect_window(clock.monotonic())
        out = eng.receive(
            parse_envelope(hello_message(connect_kind="reconnect", last_completed=1)),
            clock.monotonic(),
        )
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_EVENT_REPLAY_GAP)

    def test_S8_POC_API_REQ_16_unresolved_resent_from_cache(self):
        eng, clock, _ = running()
        eng.receive(parse_envelope(action_submit_message("a", target="ROC")), clock.monotonic())
        eng.provide_action_terminal("a", action_execution="TX_COMPLETED")
        eng.begin_reconnect_window(clock.monotonic())
        out = eng.receive(
            parse_envelope(hello_message(connect_kind="reconnect", last_completed=0, unresolved=["a"])),
            clock.monotonic(),
        )
        types = [m["type"] for m in out]
        self.assertIn(protocol.WELCOME, types)
        self.assertIn(protocol.ACTION_RESULT, types)
        self.assertIn(protocol.ACTION_TERMINAL, types)
        result = next(m for m in out if m["type"] == protocol.ACTION_RESULT)
        self.assertEqual(result["body"]["request_id"], "a")

    def test_S8_POC_API_REQ_27_reconnect_capability_change_rejected(self):
        eng, clock, _ = running()  # 최초 caps = observe_packet, replay_packet
        eng.begin_reconnect_window(clock.monotonic())
        out = eng.receive(
            parse_envelope(hello_message(connect_kind="reconnect", caps=("observe_packet",))),
            clock.monotonic(),
        )
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_PROTOCOL_ERROR)


if __name__ == "__main__":
    unittest.main()
