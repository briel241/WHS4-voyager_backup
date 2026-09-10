"""Stage 8 supervisor engine handshake·인증·deadline 시험(명세 §5, §7; REQ-04, REQ-08, REQ-09, REQ-19).

fake clock로 sleep 없이 deadline을 검증한다. socket 불필요(engine 순수 state machine).
"""

from __future__ import annotations

import unittest

from vdeter.stage8._supervisor.engine import EngineState, SupervisorEngine
from vdeter.stage8.poc_api import protocol
from vdeter.stage8.poc_api.errors import PocErrorCode
from vdeter.stage8.poc_api.messages import build_message, parse_envelope
from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
from vdeter.stage8.testing import FakeActionSubmitProvider, FakeClock, build_manifest

TOKEN = bytes(range(32))
TOKEN_HEX = TOKEN.hex()


def make_engine(*, manifest=None, server=None, provider=None):
    return SupervisorEngine(
        session_id="s1",
        workload_id="w1",
        run_id="r1",
        scenario_id="SC-VDE-006",
        expected_token=TOKEN,
        manifest=manifest or build_manifest(),
        server_config=server or ServerCapabilityConfig(),
        provider=provider or FakeActionSubmitProvider(),
    )


def hello_message(*, token_hex=TOKEN_HEX, run_id="r1", session="s1", workload="w1",
                  epoch=0, connect_kind="initial", caps=("observe_packet", "replay_packet"),
                  triggers=("SCENARIO_STARTED", "PACKET_OBSERVED"), required=(),
                  last_completed=0, unresolved=()):
    manifest = build_manifest(requested_capabilities=caps, requested_triggers=triggers,
                              required_capabilities=required)
    body = {
        "token": token_hex,
        "run_id": run_id,
        "client_nonce": "nonce",
        "protocol_minor_min": 0,
        "protocol_minor_max": 0,
        "connect_kind": connect_kind,
        "manifest": manifest.as_hello_value(),
        "requested_capabilities": list(caps) + list(required),
        "requested_triggers": list(triggers),
        "last_completed_sequence": last_completed,
        "unresolved_request_ids": list(unresolved),
    }
    return build_message("HELLO", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0, body=body)


def armed_message(*, session="s1", workload="w1", epoch=1):
    return build_message("ARMED", session_id=session, workload_id=workload,
                         connection_epoch=epoch, protocol_minor=0, body={"client_nonce": "x"})


class HandshakeTest(unittest.TestCase):
    def test_S8_POC_API_REQ_09_happy_handshake_to_running(self):
        clock = FakeClock()
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        out = eng.receive(parse_envelope(hello_message()), clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.WELCOME, protocol.START])
        self.assertEqual(eng.state, EngineState.AWAIT_ARMED)
        self.assertEqual(eng.connection_epoch, 1)
        welcome_body = out[0]["body"]
        self.assertEqual(sorted(welcome_body["capabilities"]), ["observe_packet", "replay_packet"])
        out2 = eng.receive(parse_envelope(armed_message()), clock.monotonic())
        self.assertEqual(out2, [])
        self.assertEqual(eng.state, EngineState.RUNNING)

    def test_S8_POC_API_REQ_04_token_mismatch_auth_failed(self):
        clock = FakeClock()
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        out = eng.receive(parse_envelope(hello_message(token_hex=("11" * 32))), clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.state, EngineState.ABORTED)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_AUTH_FAILED)

    def test_run_id_mismatch_auth_failed(self):
        clock = FakeClock()
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        eng.receive(parse_envelope(hello_message(run_id="wrong")), clock.monotonic())
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_AUTH_FAILED)

    def test_session_binding_mismatch_protocol_error(self):
        clock = FakeClock()
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        eng.receive(parse_envelope(hello_message(session="other")), clock.monotonic())
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_PROTOCOL_ERROR)

    def test_hello_deadline_aborts(self):
        clock = FakeClock(1000.0)
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        clock.advance(3.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.state, EngineState.ABORTED)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_PROTOCOL_ERROR)

    def test_S8_POC_API_REQ_19_armed_deadline_callback_timeout(self):
        clock = FakeClock(1000.0)
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        eng.receive(parse_envelope(hello_message()), clock.monotonic())
        self.assertEqual(eng.state, EngineState.AWAIT_ARMED)
        clock.advance(3.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_CALLBACK_TIMEOUT)

    def test_S8_POC_API_REQ_09_no_run_before_armed(self):
        # ARMED 전 action 제출은 RUNNING이 아니므로 거부(POC_NOT_RUNNING).
        clock = FakeClock()
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        eng.receive(parse_envelope(hello_message()), clock.monotonic())
        submit = build_message("ACTION_SUBMIT", session_id="s1", workload_id="w1",
                               connection_epoch=1, protocol_minor=0,
                               body={"request_id": "a", "action": {"action_kind": "REPLAY_PACKET",
                                                                    "target": "ROC", "schedule": {}, "payload": {}}})
        out = eng.receive(parse_envelope(submit), clock.monotonic())
        self.assertEqual(out[0]["body"]["admission"], "rejected")
        self.assertEqual(out[0]["body"]["error_code"], "POC_NOT_RUNNING")

    def test_capability_unavailable_fail_closed(self):
        clock = FakeClock()
        manifest = build_manifest(
            requested_capabilities=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL),
            required_capabilities=(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL,),
        )
        eng = make_engine(manifest=manifest, server=ServerCapabilityConfig())
        eng.start_handshake(clock.monotonic())
        out = eng.receive(
            parse_envelope(hello_message(
                caps=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL),
                required=(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL,))),
            clock.monotonic(),
        )
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
