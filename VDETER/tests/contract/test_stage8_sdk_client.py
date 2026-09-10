"""Stage 8 client SDK state machine 시험(명세 §7.3, §8.2, §9; REQ-11, REQ-12, REQ-14, REQ-16).

fake clock 없이 now를 인자로 넘겨 deadline·ACK 타이밍을 검증한다. socket 불필요.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api import protocol
from vdeter.stage8.poc_api.codec import canonical_sha256
from vdeter.stage8.poc_api.errors import PocErrorCode
from vdeter.stage8.poc_api.messages import build_message, parse_envelope
from vdeter.stage8.poc_api.sdk import ClientState, PocContext, SdkEngine
from vdeter.stage8.testing import RecordingCallbacks, build_manifest, hello_payload

TOKEN_HEX = bytes(range(32)).hex()


def _srv(msg_type, body, *, epoch=1):
    return build_message(msg_type, session_id="s1", workload_id="w1",
                         connection_epoch=epoch, protocol_minor=0, body=body)


def welcome(epoch=1):
    return _srv(protocol.WELCOME, {
        "connection_epoch": epoch,
        "protocol_minor": 0,
        "capabilities": ["observe_packet", "replay_packet"],
        "triggers": ["SCENARIO_STARTED", "PACKET_OBSERVED"],
        "server_nonce": "n",
        "limits": {},
    }, epoch=epoch)


def start(epoch=1):
    return _srv(protocol.START, {"scenario_id": "SC-VDE-006"}, epoch=epoch)


def event(seq, etype, body=None, epoch=1, event_id=None, payload_digest=None):
    body = body or {}
    return _srv(protocol.EVENT, {
        "sequence": seq, "event_id": event_id or f"ev{seq}", "event_type": etype,
        "payload_digest": payload_digest if payload_digest is not None else canonical_sha256(body),
        "body": body,
    }, epoch=epoch)


def action_result(request_id, admission="accepted", epoch=1):
    return _srv(protocol.ACTION_RESULT, {
        "request_id": request_id, "admission": admission,
        "physical_channel": "lower", "broadcast_parent_id": "p", "transfer_id": "t",
        "error_code": None,
    }, epoch=epoch)


def action_terminal(request_id, epoch=1):
    return _srv(protocol.ACTION_TERMINAL, {
        "request_id": request_id, "transfer_id": "t", "action_execution": "TX_COMPLETED",
        "target_effect": None, "physical_channel": "lower", "broadcast_parent_id": "p",
    }, epoch=epoch)


def make_client(callbacks=None):
    callbacks = callbacks or RecordingCallbacks()
    context = PocContext(session_id="s1", workload_id="w1", scenario_id="SC-VDE-006")
    engine = SdkEngine(callbacks, context)
    engine.build_hello(hello_payload(TOKEN_HEX, "r1", build_manifest()))
    return engine, callbacks


def to_armed(engine):
    engine.receive(parse_envelope(welcome()), 0.0)
    engine.receive(parse_envelope(start()), 0.0)


class SdkClientTest(unittest.TestCase):
    def test_welcome_start_armed(self):
        engine, callbacks = make_client()
        engine.receive(parse_envelope(welcome()), 0.0)
        self.assertEqual(engine.state, ClientState.WELCOMED)
        self.assertEqual(engine.negotiation.capabilities, frozenset({"observe_packet", "replay_packet"}))
        out = engine.receive(parse_envelope(start()), 0.0)
        self.assertTrue(callbacks.started)
        self.assertEqual(out[0]["type"], protocol.ARMED)
        self.assertEqual(engine.state, ClientState.ARMED)

    def test_S8_POC_API_REQ_12_dedup_replayed_event(self):
        engine, callbacks = make_client()
        to_armed(engine)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        self.assertEqual(len(callbacks.events), 1)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)  # replay
        self.assertEqual(len(callbacks.events), 1)  # callback 재호출 없음

    def test_event_sequence_gap_aborts(self):
        engine, _ = make_client()
        to_armed(engine)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        out = engine.receive(parse_envelope(event(3, "SCENARIO_STARTED")), 0.0)  # skip 2
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(engine.aborted_error.code, PocErrorCode.POC_EVENT_REPLAY_GAP)

    def test_ack_one_shot_after_50ms(self):
        engine, _ = make_client()
        to_armed(engine)
        out = engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        self.assertEqual([m for m in out if m["type"] == protocol.EVENT_ACK], [])  # 즉시 ACK 없음
        out = engine.tick(0.06)  # 50 ms 뒤 one-shot
        self.assertEqual(out[0]["type"], protocol.EVENT_ACK)
        self.assertEqual(out[0]["body"]["cumulative_sequence"], 1)

    def test_ack_batch_16(self):
        engine, _ = make_client()
        to_armed(engine)
        last = []
        for seq in range(1, 17):
            last = engine.receive(parse_envelope(event(seq, "STEP_STARTED")), 0.0)
        acks = [m for m in last if m["type"] == protocol.EVENT_ACK]
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["body"]["cumulative_sequence"], 16)

    def test_S8_POC_API_REQ_14_action_submit_assigns_request_id(self):
        def act(ev, ctx):
            return [ctx.replay(target="ROC", source_observation_id=ev.body.get("observation_id"))]

        engine, _ = make_client(RecordingCallbacks(event_action_fn=act))
        to_armed(engine)
        out = engine.receive(parse_envelope(event(1, "PACKET_OBSERVED", {"observation_id": "obs1"})), 0.0)
        submits = [m for m in out if m["type"] == protocol.ACTION_SUBMIT]
        self.assertEqual(len(submits), 1)
        rid = submits[0]["body"]["request_id"]
        self.assertTrue(rid)
        self.assertIn(rid, engine.unresolved)
        self.assertEqual(submits[0]["body"]["action"]["target"], "ROC")

    def test_S8_POC_API_REQ_16_unresolved_blocks_new_action(self):
        def act(ev, ctx):
            return [ctx.replay(target="ROC")]

        engine, _ = make_client(RecordingCallbacks(event_action_fn=act))
        to_armed(engine)
        out1 = engine.receive(parse_envelope(event(1, "PACKET_OBSERVED", {"o": 1})), 0.0)
        rid = next(m for m in out1 if m["type"] == protocol.ACTION_SUBMIT)["body"]["request_id"]
        # 아직 미해결인 동안 두 번째 event의 action은 제출하지 않는다.
        out2 = engine.receive(parse_envelope(event(2, "PACKET_OBSERVED", {"o": 2})), 0.0)
        self.assertEqual([m for m in out2 if m["type"] == protocol.ACTION_SUBMIT], [])
        # 해결(result+terminal) 뒤 다음 action 제출.
        engine.receive(parse_envelope(action_result(rid)), 0.0)
        out3 = engine.receive(parse_envelope(action_terminal(rid)), 0.0)
        self.assertEqual(len([m for m in out3 if m["type"] == protocol.ACTION_SUBMIT]), 1)

    def test_S8_POC_API_REQ_14_resend_unresolved_same_id_body(self):
        def act(ev, ctx):
            return [ctx.replay(target="ROC")]

        engine, _ = make_client(RecordingCallbacks(event_action_fn=act))
        to_armed(engine)
        out1 = engine.receive(parse_envelope(event(1, "PACKET_OBSERVED", {"o": 1})), 0.0)
        submit = next(m for m in out1 if m["type"] == protocol.ACTION_SUBMIT)
        resent = engine.resend_unresolved()
        self.assertEqual(len(resent), 1)
        self.assertEqual(resent[0]["body"]["request_id"], submit["body"]["request_id"])
        self.assertEqual(resent[0]["body"]["action"], submit["body"]["action"])

    def test_callback_exception_aborts(self):
        engine, _ = make_client(RecordingCallbacks(raise_on={"PACKET_OBSERVED"}))
        to_armed(engine)
        out = engine.receive(parse_envelope(event(1, "PACKET_OBSERVED")), 0.0)
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(engine.aborted_error.code, PocErrorCode.POC_CALLBACK_EXCEPTION)

    def test_on_start_returning_actions_is_protocol_error(self):
        context = PocContext(session_id="s1", workload_id="w1", scenario_id="SC-VDE-006")
        descriptor = context.replay(target="ROC")
        engine, _ = make_client(RecordingCallbacks(start_actions=[descriptor]))
        engine.receive(parse_envelope(welcome()), 0.0)
        out = engine.receive(parse_envelope(start()), 0.0)
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(engine.aborted_error.code, PocErrorCode.POC_PROTOCOL_ERROR)

    def test_S8_POC_API_REQ_13_wrong_payload_digest_new_event_aborts(self):
        engine, callbacks = make_client()
        to_armed(engine)
        out = engine.receive(parse_envelope(event(1, "SCENARIO_STARTED", payload_digest="bogus")), 0.0)
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(engine.aborted_error.code, PocErrorCode.POC_EVENT_REPLAY_GAP)
        self.assertEqual(len(callbacks.events), 0)  # callback 미실행

    def test_S8_POC_API_REQ_13_replay_different_body_same_sequence_aborts(self):
        engine, callbacks = make_client()
        to_armed(engine)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        self.assertEqual(len(callbacks.events), 1)
        # 같은 sequence를 다른 event_id·다른 body(자체 digest는 정상)로 재전송 → 모순 abort.
        out = engine.receive(
            parse_envelope(event(1, "SCENARIO_STARTED", body={"x": 1}, event_id="tampered")), 0.0)
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(engine.aborted_error.code, PocErrorCode.POC_EVENT_REPLAY_GAP)
        self.assertEqual(len(callbacks.events), 1)  # 재호출 없음

    def test_replay_same_event_ok(self):
        # 동일 sequence·동일 event_id·동일 body 재전송은 정상(dedup, re-ACK).
        engine, callbacks = make_client()
        to_armed(engine)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        out = engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        self.assertNotEqual(engine.state, ClientState.ABORTED)
        self.assertEqual(len(callbacks.events), 1)
        self.assertTrue(any(m["type"] == protocol.EVENT_ACK for m in out))

    def test_finish_close_flow(self):
        engine, callbacks = make_client()
        to_armed(engine)
        engine.receive(parse_envelope(event(1, "SCENARIO_STARTED")), 0.0)
        out = engine.receive(parse_envelope(_srv(protocol.FINISH, {"cumulative_sequence": 1})), 0.0)
        self.assertTrue(callbacks.finished)
        self.assertEqual(out[0]["type"], protocol.FINISH_RESULT)
        engine.receive(parse_envelope(_srv(protocol.CLOSE, {"infrastructure": "COMPLETED"})), 0.0)
        self.assertEqual(engine.state, ClientState.CLOSED)


if __name__ == "__main__":
    unittest.main()
