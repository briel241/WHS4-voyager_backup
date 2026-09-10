"""Stage 8 action 멱등성·논리 target·금지 field·상한·in-flight 시험
(명세 §9, §10; REQ-14, REQ-15, REQ-17, REQ-28). fake provider·clock, socket 불필요.
"""

from __future__ import annotations

import unittest

from vdeter.stage8._supervisor.engine import EngineState
from vdeter.stage8.poc_api import protocol
from vdeter.stage8.poc_api.errors import PocErrorCode
from vdeter.stage8.poc_api.limits import MAX_ACTION_DESCRIPTOR_BYTES
from vdeter.stage8.poc_api.messages import parse_envelope
from vdeter.stage8.testing import FakeActionSubmitProvider, FakeClock
from vdeter.stage8.testing.server_harness import (
    action_submit_message,
    drive_to_running,
    make_engine,
)


def running(provider=None):
    clock = FakeClock(1000.0)
    provider = provider if provider is not None else FakeActionSubmitProvider()
    eng = make_engine(provider=provider)
    drive_to_running(eng, clock)
    return eng, clock, provider


def submit(eng, clock, request_id, **kw):
    return eng.receive(parse_envelope(action_submit_message(request_id, **kw)), clock.monotonic())


class ActionTest(unittest.TestCase):
    def test_S8_POC_API_REQ_28_logical_target_accepted_channel_recorded(self):
        eng, clock, provider = running()
        out = submit(eng, clock, "a", target="ROC")
        body = out[0]["body"]
        self.assertEqual(body["admission"], "accepted")
        self.assertEqual(body["physical_channel"], "lower")  # ROC=lower
        self.assertIsNotNone(body["broadcast_parent_id"])
        self.assertIsNotNone(body["transfer_id"])
        self.assertEqual(provider.submit_calls, 1)
        out2 = submit(eng, clock, "b", target="MASS-A")
        self.assertEqual(out2[0]["body"]["error_code"], "POC_ATTACK_INFLIGHT")  # in-flight 1

    def test_S8_POC_API_REQ_14_idempotent_resend_provider_called_once(self):
        eng, clock, provider = running()
        msg = action_submit_message("a", target="ROC")
        out1 = eng.receive(parse_envelope(msg), clock.monotonic())
        out2 = eng.receive(parse_envelope(msg), clock.monotonic())  # 같은 id·body 재전송
        self.assertEqual(out1[0]["body"]["admission"], "accepted")
        self.assertEqual(out2[0]["body"]["admission"], "accepted")  # cached 반환
        self.assertEqual(out1[0]["body"]["transfer_id"], out2[0]["body"]["transfer_id"])
        self.assertEqual(provider.submit_calls, 1)  # RF submit 재호출 없음

    def test_S8_POC_API_REQ_15_same_id_different_body_conflict(self):
        eng, clock, provider = running()
        eng.receive(parse_envelope(action_submit_message("a", target="ROC")), clock.monotonic())
        out = eng.receive(parse_envelope(action_submit_message("a", target="MASS-A")), clock.monotonic())
        self.assertEqual(out[0]["type"], protocol.ERROR)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_REQUEST_ID_CONFLICT)

    def test_S8_POC_API_REQ_28_forbidden_rf_fields_rejected(self):
        forbidden_payloads = [
            {"frequency": 2450000000},
            {"gain": 40},
            {"waveform": "bpsk"},
            {"raw_iq": [0, 1]},
            {"sample_rate": 307200},
            {"bandwidth": 4700000},
            {"lane": 0},
            {"antenna": "TX/RX"},
            {"simultaneous_channels": ["lower", "upper"]},
        ]
        for i, payload in enumerate(forbidden_payloads):
            with self.subTest(payload=payload):
                eng, clock, _ = running()
                out = submit(eng, clock, f"a{i}", target="ROC", payload=payload)
                self.assertEqual(out[0]["body"]["admission"], "rejected")
                self.assertEqual(out[0]["body"]["error_code"], "POC_ACTION_SCHEMA_INVALID")
                self.assertEqual(eng.state, EngineState.RUNNING)  # request 하나만 거부

    def test_forbidden_physical_channel_field_rejected(self):
        eng, clock, _ = running()
        out = submit(eng, clock, "a", target="ROC", payload={"physical_channel": "upper"})
        self.assertEqual(out[0]["body"]["error_code"], "POC_ACTION_SCHEMA_INVALID")

    def test_target_denied(self):
        eng, clock, _ = running()
        out = submit(eng, clock, "a", target="MASS-B")
        self.assertEqual(out[0]["body"]["error_code"], "POC_TARGET_DENIED")

    def test_action_too_large(self):
        eng, clock, _ = running()
        pad = "z" * (MAX_ACTION_DESCRIPTOR_BYTES + 10)
        out = submit(eng, clock, "a", target="ROC", payload={"blob": pad})
        self.assertEqual(out[0]["body"]["error_code"], "POC_ACTION_TOO_LARGE")

    def test_invalid_kind_schema_invalid(self):
        eng, clock, _ = running()
        out = submit(eng, clock, "a", kind="BOGUS_ACTION", target="ROC")
        self.assertEqual(out[0]["body"]["error_code"], "POC_ACTION_SCHEMA_INVALID")

    def test_S8_POC_API_REQ_17_action_limit_32(self):
        eng, clock, provider = running()
        for i in range(32):
            out = submit(eng, clock, f"a{i}", target="ROC")
            self.assertEqual(out[0]["body"]["admission"], "accepted", i)
            eng.provide_action_terminal(f"a{i}", action_execution="TX_COMPLETED")
        out = submit(eng, clock, "a32", target="ROC")
        self.assertEqual(out[0]["body"]["error_code"], "POC_ACTION_LIMIT_EXCEEDED")

    def test_inflight_one_then_terminal_allows_next(self):
        eng, clock, _ = running()
        self.assertEqual(submit(eng, clock, "a", target="ROC")[0]["body"]["admission"], "accepted")
        self.assertEqual(submit(eng, clock, "b", target="ROC")[0]["body"]["error_code"],
                         "POC_ATTACK_INFLIGHT")
        term = eng.provide_action_terminal("a", action_execution="TX_COMPLETED",
                                           target_effect="REJECTED_BY_TARGET")
        self.assertEqual(term[0]["type"], protocol.ACTION_TERMINAL)
        self.assertEqual(term[0]["body"]["action_execution"], "TX_COMPLETED")
        self.assertEqual(submit(eng, clock, "c", target="ROC")[0]["body"]["admission"], "accepted")

    def test_wrong_binding_action_rejected_fail_closed(self):
        # 잘못된 session/workload/epoch ACTION_SUBMIT는 승인·provider 호출 없이 fail-closed(§6.2).
        from vdeter.stage8._supervisor.engine import EngineState as ES
        from vdeter.stage8.testing.server_harness import action_submit_message
        for kw in ({"session": "wrong"}, {"workload": "wrong"}, {"epoch": 999}):
            with self.subTest(kw=kw):
                eng, clock, provider = running()
                out = eng.receive(parse_envelope(action_submit_message("a", target="ROC", **kw)),
                                  clock.monotonic())
                self.assertEqual(out[0]["type"], protocol.ERROR)
                self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_PROTOCOL_ERROR)
                self.assertEqual(eng.state, ES.ABORTED)
                self.assertEqual(provider.submit_calls, 0)  # provider 미호출

    def test_S8_POC_API_REQ_24_provider_reject_not_infra_failure(self):
        provider = FakeActionSubmitProvider(reject_reason=PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        eng, clock, provider = running(provider=provider)
        out = submit(eng, clock, "a", target="ROC")
        self.assertEqual(out[0]["body"]["admission"], "rejected")
        self.assertEqual(out[0]["body"]["error_code"], "POC_RADIO_RESOURCE_CONFLICT")
        self.assertEqual(eng.state, EngineState.RUNNING)  # PoC·scenario 계속


if __name__ == "__main__":
    unittest.main()
