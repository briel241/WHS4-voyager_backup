"""Stage 8 event sequence·누적 ACK·replay ring·overflow·NOT_TUNED 시험
(명세 §8, §10; REQ-10, REQ-11, REQ-17, REQ-18, REQ-29). fake clock 사용, socket 불필요.
"""

from __future__ import annotations

import unittest

from vdeter.stage8._supervisor.engine import SERVER_EVENT_ACK_GRACE_S, EngineState
from vdeter.stage8.poc_api import protocol
from vdeter.stage8.poc_api.codec import canonical_sha256
from vdeter.stage8.poc_api.errors import PocErrorCode
from vdeter.stage8.poc_api.messages import parse_envelope
from vdeter.stage8.poc_api.types import PhysicalChannel, TuningState
from vdeter.stage8.testing import FakeClock
from vdeter.stage8.testing.fakes import make_observation
from vdeter.stage8.testing.server_harness import ack_message, drive_to_running, make_engine


def running():
    clock = FakeClock(1000.0)
    eng = make_engine()
    drive_to_running(eng, clock)
    return eng, clock


class EventPipelineTest(unittest.TestCase):
    def test_S8_POC_API_REQ_10_sequence_and_digest(self):
        eng, clock = running()
        eng.enqueue_event("SCENARIO_STARTED", {"scenario": "SC-VDE-006"}, clock.monotonic())
        eng.enqueue_event("STEP_STARTED", {"step": 1}, clock.monotonic())
        out = eng.drain(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.EVENT, protocol.EVENT])
        self.assertEqual([m["body"]["sequence"] for m in out], [1, 2])
        self.assertEqual(out[0]["body"]["payload_digest"], canonical_sha256({"scenario": "SC-VDE-006"}))
        self.assertNotEqual(out[0]["body"]["event_id"], out[1]["body"]["event_id"])

    def test_S8_POC_API_REQ_10_cumulative_ack_evicts_ring(self):
        eng, clock = running()
        for i in range(3):
            eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic())
        eng.drain(clock.monotonic())
        self.assertEqual(eng.residual_snapshot()["replay_ring"], 3)
        eng.receive(parse_envelope(ack_message(2)), clock.monotonic())
        self.assertEqual(eng.residual_snapshot()["replay_ring"], 1)
        eng.receive(parse_envelope(ack_message(3)), clock.monotonic())
        self.assertEqual(eng.residual_snapshot()["replay_ring"], 0)

    def test_S8_POC_API_REQ_11_ack_grace_deadline_detects_hang(self):
        eng, clock = running()
        eng.enqueue_event("STEP_STARTED", {}, clock.monotonic())
        eng.drain(clock.monotonic())
        clock.advance(SERVER_EVENT_ACK_GRACE_S + 0.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_CALLBACK_TIMEOUT)

    def test_S8_POC_API_REQ_17_queue_overflow_by_count(self):
        eng, clock = running()
        for i in range(256):
            self.assertEqual(eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic()), [])
        out = eng.enqueue_event("STEP_STARTED", {"i": 256}, clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_EVENT_QUEUE_OVERFLOW)
        self.assertEqual(eng.state, EngineState.ABORTED)

    def test_S8_POC_API_REQ_17_queue_overflow_by_bytes(self):
        eng, clock = running()
        pad = "a" * (16 * 1024)
        overflowed_at = None
        for i in range(256):
            out = eng.enqueue_event("STEP_STARTED", {"i": i, "pad": pad}, clock.monotonic())
            if out:
                overflowed_at = i
                break
        self.assertIsNotNone(overflowed_at)
        self.assertLess(overflowed_at, 256)  # count 상한 전에 byte 상한
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_EVENT_QUEUE_OVERFLOW)

    def test_S8_POC_API_REQ_18_replay_ring_overflow(self):
        eng, clock = running()
        for i in range(256):
            eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic())
        eng.drain(clock.monotonic())
        self.assertEqual(eng.residual_snapshot()["replay_ring"], 256)
        eng.enqueue_event("STEP_STARTED", {"i": 256}, clock.monotonic())
        out = eng.drain(clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_EVENT_QUEUE_OVERFLOW)

    def test_S8_POC_API_REQ_29_not_tuned_no_event(self):
        eng, clock = running()
        rec = make_observation(tuning_state=TuningState.NOT_TUNED,
                               physical_channel=PhysicalChannel.UPPER, source_station="ROC")
        out = eng.enqueue_observation(rec, clock.monotonic())
        self.assertEqual(out, [])
        self.assertEqual(eng.evidence["not_tuned_count"], 1)
        self.assertEqual(eng.drain(clock.monotonic()), [])  # event 미생성
        self.assertFalse(eng.evidence["delivery_matrix"][-1]["emitted_event"])

    def test_S8_POC_API_REQ_29_tuned_emits_packet_observed(self):
        eng, clock = running()
        rec = make_observation(tuning_state=TuningState.TUNED_OBSERVER,
                               physical_channel=PhysicalChannel.LOWER)
        eng.enqueue_observation(rec, clock.monotonic())
        out = eng.drain(clock.monotonic())
        self.assertEqual(out[0]["body"]["event_type"], "PACKET_OBSERVED")
        self.assertEqual(out[0]["body"]["body"]["physical_channel"], "lower")
        self.assertEqual(out[0]["body"]["body"]["tuning_state"], "TUNED_OBSERVER")
        self.assertEqual(eng.evidence["tuned_observer_count"], 1)

    def test_S8_POC_API_REQ_18_burst_producer_nonblocking(self):
        eng, clock = running()
        for i in range(200):
            self.assertEqual(eng.enqueue_event("STEP_STARTED", {"i": i}, clock.monotonic()), [])
        self.assertEqual(eng.evidence["event_queue_high_water"], 200)


if __name__ == "__main__":
    unittest.main()
