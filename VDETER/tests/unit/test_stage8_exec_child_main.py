"""child launcher client loop 시험(실행 명세 §5.1, §5.2). platform 무관(fake transport+clock).

child_main.run_client이 자체 30초 수명 제한을 두지 않고 server CLOSE·연결 손실·최후 방어
ceiling으로만 종료함을 검증한다(workload 900초 상한의 실제 강제는 workload를 소유한
기존 Stage 7 resident session의 몫이며 Stage 6에서 연결된다).
"""

from __future__ import annotations

import unittest

from vdeter.stage8.execution import child_main
from vdeter.stage8.poc_api.codec import encode_wire_packet
from vdeter.stage8.poc_api.messages import parse_envelope
from vdeter.stage8.testing.client_runner import RecordingCallbacks
from vdeter.stage8.testing.fakes import build_manifest
from vdeter.stage8.testing.server_harness import (
    DEFAULT_TOKEN,
    armed_message,
    hello_message,
    make_engine,
)


def _mint_server_messages():
    """실제 SupervisorEngine으로 유효한 WELCOME/START/FINISH/CLOSE envelope를 만든다."""

    engine = make_engine()  # session s1 / workload w1 / run r1 / scenario SC-VDE-006
    engine.start_handshake(0.0)
    welcome_start = engine.receive(parse_envelope(hello_message()), 0.0)  # [WELCOME, START]
    engine.receive(parse_envelope(armed_message()), 0.0)  # ARMED → RUNNING
    close = engine.force_close(0.0)  # [CLOSE]
    encoded = [encode_wire_packet(m) for m in welcome_start]
    close_bytes = [encode_wire_packet(m) for m in close]
    return encoded, close_bytes


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class _FakeTransport:
    """scripted recv를 돌려주고 매 recv마다 clock을 dt만큼 진행시키는 fake transport."""

    def __init__(self, script, clock, dt):
        self._script = list(script)
        self._clock = clock
        self._dt = dt
        self.sent = []
        self.closed = False

    def send_packet(self, data):
        self.sent.append(data)

    def recv_packet(self, timeout=None):
        self._clock.t += self._dt
        if not self._script:
            return None
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def _run(script, clock, *, dt=5.0, safety_timeout_s=child_main.CHILD_SAFETY_TIMEOUT_S):
    transport = _FakeTransport(script, clock, dt)
    rc = child_main.run_client(
        RecordingCallbacks(), transport,
        token=DEFAULT_TOKEN, run_id="r1", session_id="s1", workload_id="w1",
        scenario_id="SC-VDE-006", manifest_hello=build_manifest().as_hello_value(),
        poll=0.0, now_fn=clock.now, safety_timeout_s=safety_timeout_s,
    )
    return rc, transport


class ChildLifetimeTest(unittest.TestCase):
    def test_survives_past_30s_and_exits_on_close(self):
        welcome_start, close_bytes = _mint_server_messages()
        # WELCOME, START, 그리고 30초를 훌쩍 넘긴 뒤에야 CLOSE. 사이에는 recv None(대기).
        clock = _Clock()
        script = welcome_start + [None] * 12 + close_bytes  # dt=5 → CLOSE 시점 ~70초 > 30
        rc, transport = _run(script, clock, dt=5.0)
        self.assertEqual(rc, 0)  # server CLOSE로 정상 종료
        self.assertTrue(transport.closed)
        self.assertGreater(clock.t, 30.0)  # 30초 넘겨서도 생존해 CLOSE를 처리했다

    def test_exits_on_connection_loss(self):
        welcome_start, _ = _mint_server_messages()
        clock = _Clock()
        script = welcome_start + [ConnectionError("peer closed")]
        rc, transport = _run(script, clock, dt=5.0)
        self.assertEqual(rc, 1)  # CLOSED 아님
        self.assertTrue(transport.closed)
        self.assertLess(clock.t, 30.0)  # 연결 손실 시 즉시 종료(수명 제한과 무관)

    def test_exits_at_safety_ceiling_when_server_vanishes(self):
        welcome_start, _ = _mint_server_messages()
        clock = _Clock()
        # CLOSE도 연결손실도 없이 무한 대기 → 최후 방어 ceiling(작게 주입)에서만 종료.
        script = welcome_start + [None] * 1000
        rc, transport = _run(script, clock, dt=5.0, safety_timeout_s=100.0)
        self.assertEqual(rc, 1)
        self.assertTrue(transport.closed)
        self.assertGreater(clock.t, 100.0)   # ceiling 넘겨서 종료
        self.assertLess(clock.t, 200.0)      # 무한 루프 아님

    def test_no_hardcoded_30s_literal(self):
        # 회귀 방지: 30초 리터럴 제거 확인(safety ceiling은 session absolute 기반).
        self.assertGreater(child_main.CHILD_SAFETY_TIMEOUT_S, 7200.0)


class ChildEntrypointNegativeTest(unittest.TestCase):
    """entrypoint 로딩·env 오류의 fail-closed 경로(§6.4, §11.2)."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="child-ep-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def _write(self, name, text):
        import os
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path

    def test_loads_and_instantiates_entrypoint(self):
        path = self._write("ok.py", "class Poc:\n    MARK = 'loaded'\n")
        obj = child_main.load_entrypoint(path, "ok.py:Poc")
        self.assertEqual(obj.MARK, "loaded")

    def test_missing_class_rejected(self):
        path = self._write("nocls.py", "class Other:\n    pass\n")
        with self.assertRaises(AttributeError):
            child_main.load_entrypoint(path, "nocls.py:Poc")

    def test_malformed_entrypoint_rejected(self):
        path = self._write("m.py", "class Poc:\n    pass\n")
        with self.assertRaises(ValueError):
            child_main.load_entrypoint(path, "m.py")  # ':' 없음

    def test_module_raising_on_import_propagates(self):
        path = self._write("boom.py", "raise RuntimeError('module boom')\n")
        with self.assertRaises(RuntimeError):
            child_main.load_entrypoint(path, "boom.py:Poc")

    def test_missing_file_rejected(self):
        import os
        with self.assertRaises((FileNotFoundError, ImportError, OSError)):
            child_main.load_entrypoint(os.path.join(self.dir, "absent.py"), "absent.py:Poc")

    def test_main_with_empty_env_returns_nonzero(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(child_main.main(), 2)  # env 오류 fail-closed


if __name__ == "__main__":
    unittest.main()
