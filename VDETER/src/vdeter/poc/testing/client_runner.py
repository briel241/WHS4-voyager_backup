"""client(SDK)를 transport 위에서 구동하는 시험용 runner와 recording callback.

fake-clock 정밀 시험은 `SdkEngine`을 직접 구동하고, 이 runner는 실제 socket round-trip
(thread child·subprocess harness)에서 client 흐름을 돌린다.
"""

from __future__ import annotations

import time

from ..api.codec import decode_wire_packet, encode_wire_packet
from ..api.messages import parse_envelope
from ..api.sdk import ClientState, PocContext, SdkEngine


class RecordingCallbacks:
    """event를 기록하고 선택적으로 action descriptor를 반환하는 callback.

    `event_action_fn(event, context) -> list`로 반응형 action을 만든다. `raise_on`에
    담긴 event type에서 예외를 던져 callback 오류 경로를 시험한다.
    """

    def __init__(self, *, start_actions=None, event_action_fn=None,
                 raise_on=frozenset(), sleep_on_event: float = 0.0) -> None:
        self.started = False
        self.finished = False
        self.events = []
        self._start_actions = list(start_actions or [])
        self._event_action_fn = event_action_fn
        self._raise_on = set(raise_on)
        self._sleep_on_event = sleep_on_event

    def on_start(self, context: PocContext):
        self.started = True
        return list(self._start_actions)

    def on_event(self, event, context: PocContext):
        self.events.append(event)
        if event.type in self._raise_on:
            raise RuntimeError(f"callback raised on {event.type}")
        if self._sleep_on_event:
            time.sleep(self._sleep_on_event)
        if self._event_action_fn is not None:
            return self._event_action_fn(event, context) or []
        return []

    def on_finish(self, context: PocContext):
        self.finished = True
        return None


class ClientRunner:
    def __init__(self, transport, engine: SdkEngine) -> None:
        self.transport = transport
        self.engine = engine

    def _send(self, messages) -> None:
        for message in messages:
            self.transport.send_packet(encode_wire_packet(message))

    def send_hello(self, manifest_hello: dict, *, connect_kind: str = "initial") -> None:
        self._send([self.engine.build_hello(manifest_hello, connect_kind=connect_kind)])

    def pump_once(self, timeout: float = 0.05) -> str:
        data = self.transport.recv_packet(timeout=timeout)
        now = time.monotonic()
        if data is not None:
            parsed = parse_envelope(decode_wire_packet(data))
            self._send(self.engine.receive(parsed, now))
        self._send(self.engine.tick(now))
        return self.engine.state.value

    def run(self, *, timeout: float, poll: float = 0.02) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump_once(timeout=poll)
            if self.engine.state in (ClientState.CLOSED, ClientState.ABORTED):
                break
        return self.engine.state.value


__all__ = ("ClientRunner", "RecordingCallbacks")
