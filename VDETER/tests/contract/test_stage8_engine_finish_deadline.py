"""Stage 8 engine — R12 on_finish 예산 경계(fake clock) + R13 timeout ERROR wire 인코딩(실 codec).

Codex DEADLINE-REVIEW.md(기준 0f47ecb):
- R12: FINISHING 상태에 deadline이 없어 on_finish 2초 초과가 COMPLETED로 분류됐다. 이제 `begin_finish`가
  `CALLBACK_ON_FINISH_DEADLINE_S` 예산을 세우고 `tick`/`next_deadline`/`_on_finish_result`가 강제한다.
  경계: 예산 미만·정확히 예산 = 허용, 초과 = POC_CALLBACK_TIMEOUT(POC_ABORT). late FINISH_RESULT도 거부.
- R13: timeout ERROR detail의 float(deadline_s/grace_s)가 canonical codec(float 금지)에서 CodecError를 냈다.
  이제 정수 ms(deadline_ms/grace_ms)만 내고, `_fail`은 비정규 detail을 명시적 encoding 오류 요약으로
  대체해 ERROR가 반드시 wire를 건너게 한다(codec 완화 없음). socket 불필요.
"""

from __future__ import annotations

import unittest

from vdeter.poc._supervisor.engine import SERVER_EVENT_ACK_GRACE_S, EngineState
from vdeter.poc.api import protocol
from vdeter.poc.api.codec import CodecError, decode_wire_packet, encode_wire_packet
from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.limits import (
    ARMED_DEADLINE_S,
    CALLBACK_ON_FINISH_DEADLINE_S,
    HELLO_DEADLINE_S,
)
from vdeter.poc.api.messages import parse_envelope
from vdeter.poc.testing import FakeClock
from vdeter.poc.testing.server_harness import (
    ack_message,
    drive_to_running,
    hello_message,
    make_engine,
)


def _ms(seconds):
    return int(round(seconds * 1000))


def running():
    clock = FakeClock(1000.0)
    eng = make_engine()
    drive_to_running(eng, clock)
    return eng, clock


def finish_result_message(cumulative=0):
    msg = ack_message(cumulative)                 # 같은 binding(session/workload/epoch/minor)
    msg["type"] = protocol.FINISH_RESULT
    msg["body"] = {"cumulative_sequence": cumulative}
    return msg


def roundtrip(message):
    """실 wire codec으로 encode→decode. float 등 비정규 값이면 CodecError."""
    return decode_wire_packet(encode_wire_packet(message))


class R12FinishBudgetTest(unittest.TestCase):
    def _begin(self):
        eng, clock = running()
        out = eng.begin_finish(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.FINISH])
        self.assertEqual(eng.state, EngineState.FINISHING)
        return eng, clock

    def test_finish_result_below_budget_closes(self):
        eng, clock = self._begin()
        clock.advance(CALLBACK_ON_FINISH_DEADLINE_S - 0.1)
        self.assertEqual(eng.tick(clock.monotonic()), [])
        out = eng.receive(parse_envelope(finish_result_message()), clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.CLOSE])
        self.assertEqual(eng.state, EngineState.CLOSING)
        self.assertIsNone(eng.fatal_error)

    def test_finish_result_at_exact_budget_is_allowed(self):
        eng, clock = self._begin()
        clock.advance(CALLBACK_ON_FINISH_DEADLINE_S)   # <= 2.0초는 명세 §11 허용 범위
        self.assertEqual(eng.tick(clock.monotonic()), [])
        out = eng.receive(parse_envelope(finish_result_message()), clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.CLOSE])
        self.assertEqual(eng.state, EngineState.CLOSING)

    def test_tick_over_budget_aborts_with_callback_timeout(self):
        eng, clock = self._begin()
        clock.advance(CALLBACK_ON_FINISH_DEADLINE_S + 0.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.ERROR])
        self.assertEqual(eng.state, EngineState.ABORTED)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_CALLBACK_TIMEOUT)
        self.assertEqual(eng.fatal_error.detail, {"deadline_ms": _ms(CALLBACK_ON_FINISH_DEADLINE_S)})
        body = roundtrip(out[0])["body"]                # R13: 실 wire 통과
        self.assertEqual(body["code"], "POC_CALLBACK_TIMEOUT")
        self.assertEqual(body["detail"], {"deadline_ms": 2000})

    def test_late_finish_result_after_budget_is_rejected(self):
        eng, clock = self._begin()
        clock.advance(CALLBACK_ON_FINISH_DEADLINE_S + 0.5)   # tick 없이 늦게 도착한 FINISH_RESULT
        out = eng.receive(parse_envelope(finish_result_message()), clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.ERROR])
        self.assertEqual(eng.state, EngineState.ABORTED)
        self.assertEqual(eng.fatal_error.code, PocErrorCode.POC_CALLBACK_TIMEOUT)
        roundtrip(out[0])

    def test_next_deadline_wakes_serve_loop_at_finish_budget(self):
        eng, clock = self._begin()
        self.assertAlmostEqual(eng.next_deadline(clock.monotonic()), CALLBACK_ON_FINISH_DEADLINE_S)
        clock.advance(1.5)
        self.assertAlmostEqual(eng.next_deadline(clock.monotonic()), CALLBACK_ON_FINISH_DEADLINE_S - 1.5)

    def test_force_close_clears_finish_budget(self):
        eng, clock = self._begin()
        eng.force_close(clock.monotonic(), infrastructure="ABORTED")
        clock.advance(CALLBACK_ON_FINISH_DEADLINE_S + 5.0)
        self.assertEqual(eng.tick(clock.monotonic()), [])   # CLOSING에서는 finish 예산 판정 없음
        self.assertEqual(eng.state, EngineState.CLOSING)

    def test_budget_not_extended_by_generic_pump_timeout(self):
        # 예산은 begin_finish 시점에 고정된다: 이후 어떤 호출도 deadline을 뒤로 미루지 않는다.
        eng, clock = self._begin()
        d0 = eng._finish_deadline
        clock.advance(1.0)
        eng.tick(clock.monotonic())
        eng.next_deadline(clock.monotonic())
        self.assertEqual(eng._finish_deadline, d0)


class R13TimeoutErrorWireTest(unittest.TestCase):
    def test_hello_deadline_error_encodes_with_integer_ms(self):
        clock = FakeClock(1000.0)
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        clock.advance(HELLO_DEADLINE_S + 0.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.ERROR])
        body = roundtrip(out[0])["body"]
        self.assertEqual(body["code"], "POC_PROTOCOL_ERROR")
        self.assertEqual(body["detail"], {"deadline_ms": _ms(HELLO_DEADLINE_S)})

    def test_armed_deadline_error_encodes_with_integer_ms(self):
        clock = FakeClock(1000.0)
        eng = make_engine()
        eng.start_handshake(clock.monotonic())
        eng.receive(parse_envelope(hello_message()), clock.monotonic())
        self.assertEqual(eng.state, EngineState.AWAIT_ARMED)
        clock.advance(ARMED_DEADLINE_S + 0.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.ERROR])
        body = roundtrip(out[0])["body"]
        self.assertEqual(body["code"], "POC_CALLBACK_TIMEOUT")
        self.assertEqual(body["detail"], {"deadline_ms": _ms(ARMED_DEADLINE_S)})

    def test_event_ack_grace_error_encodes_with_integer_ms(self):
        eng, clock = running()
        eng.enqueue_event("SCENARIO_STARTED", {"scenario": "SC-VDE-006"}, clock.monotonic())
        eng.drain(clock.monotonic())
        clock.advance(SERVER_EVENT_ACK_GRACE_S + 0.001)
        out = eng.tick(clock.monotonic())
        self.assertEqual([m["type"] for m in out], [protocol.ERROR])
        body = roundtrip(out[0])["body"]
        self.assertEqual(body["code"], "POC_CALLBACK_TIMEOUT")
        self.assertEqual(body["detail"], {"grace_ms": _ms(SERVER_EVENT_ACK_GRACE_S)})

    def test_every_generated_timeout_detail_is_integer_valued(self):
        for detail in ({"deadline_ms": _ms(HELLO_DEADLINE_S)}, {"deadline_ms": _ms(ARMED_DEADLINE_S)},
                       {"grace_ms": _ms(SERVER_EVENT_ACK_GRACE_S)},
                       {"deadline_ms": _ms(CALLBACK_ON_FINISH_DEADLINE_S)}):
            for value in detail.values():
                self.assertIsInstance(value, int)
                self.assertNotIsInstance(value, bool)

    def test_codec_still_rejects_floats(self):
        # R13 remedy: codec을 완화하지 않는다.
        with self.assertRaises(CodecError):
            encode_wire_packet({"type": "X", "body": {"grace_s": 0.3}})

    def test_fail_guard_keeps_code_and_records_nonencodable_detail(self):
        eng, clock = running()
        out = eng._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "probe", detail={"bad": 1.5}))
        body = roundtrip(out[0])["body"]                      # 반드시 wire를 건넌다
        self.assertEqual(body["code"], "POC_PROTOCOL_ERROR")
        self.assertEqual(body["severity"], "POC_ABORT")
        self.assertEqual(body["detail"]["detail_keys"], ["bad"])
        self.assertIn("detail_encoding_error", body["detail"])
        self.assertEqual(eng.fatal_error.detail, {"bad": 1.5})  # 로컬 원본 detail은 보존(삼키지 않음)
        self.assertEqual(eng.state, EngineState.ABORTED)


if __name__ == "__main__":
    unittest.main()
