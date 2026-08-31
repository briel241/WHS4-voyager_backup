"""Persistent-session TEST adapter sequential peer reconnect regression."""

import threading

from apps.stage7_endpoint_launcher import _serve_test_adapter_peers


class _Connection:
    def __init__(self, server):
        self._server = server
        self.closed = False

    def close(self):
        self.closed = True
        self._server.peer_active = False


class _SequentialServer:
    def __init__(self, peers, stop_event):
        self.peers = [list(messages) for messages in peers]
        self.stop_event = stop_event
        self.accept_calls = []
        self.responses = []
        self.connections = []
        self.peer_active = False
        self._connection = None
        self._peer = None
        self._current = None

    def accept(self, *, expected_pid, expected_uid, timeout):
        assert not self.peer_active, "busy peer 중 두 번째 peer를 accept했다"
        if not self.peers:
            self.stop_event.set()
            raise OSError("listener closed for test")
        self.accept_calls.append((expected_pid, expected_uid, timeout))
        self.peer_active = True
        self._current = self.peers.pop(0)
        self._connection = _Connection(self)
        self.connections.append(self._connection)
        self._peer = (len(self.connections), expected_uid)

    def recv_message(self, timeout):
        assert self.peer_active
        if self._current:
            return self._current.pop(0)
        raise ConnectionError("peer EOF")

    def send_message(self, message):
        assert self.peer_active
        self.responses.append(message)


class _Adapter:
    def __init__(self):
        self.calls = []

    def handle_command(self, message, *, peer):
        self.calls.append((message, peer))
        return {"reply": message["step"]}


def test_test_adapter_accepts_session_start_workload_and_stop_sequentially():
    stop_event = threading.Event()
    server = _SequentialServer(
        [
            [{"step": "session-start"}],
            [{"step": "workload-001"}],
            [{"step": "session-stop"}],
        ],
        stop_event,
    )
    adapter = _Adapter()

    _serve_test_adapter_peers(
        server, adapter, expected_uid=1234, stop_event=stop_event)

    assert [call[1] for call in server.accept_calls] == [1234, 1234, 1234]
    assert all(call[0] is None for call in server.accept_calls)
    assert [call[0]["step"] for call in adapter.calls] == [
        "session-start", "workload-001", "session-stop"]
    assert all(call[1] == (1234,) for call in adapter.calls)
    assert server.responses == [
        {"reply": "session-start"},
        {"reply": "workload-001"},
        {"reply": "session-stop"},
    ]
    assert all(connection.closed for connection in server.connections)
    assert server._connection is None and server._peer is None


def test_test_adapter_stop_before_accept_exits_without_opening_peer():
    stop_event = threading.Event()
    stop_event.set()
    server = _SequentialServer([], stop_event)

    _serve_test_adapter_peers(
        server, _Adapter(), expected_uid=1234, stop_event=stop_event)

    assert server.accept_calls == []


def test_test_adapter_stop_during_active_peer_closes_it_without_next_accept():
    stop_event = threading.Event()
    server = _SequentialServer(
        [[{"step": "session-stop"}], [{"step": "must-not-be-accepted"}]],
        stop_event,
    )

    class _StoppingAdapter(_Adapter):
        def handle_command(self, message, *, peer):
            response = super().handle_command(message, peer=peer)
            stop_event.set()
            return response

    _serve_test_adapter_peers(
        server, _StoppingAdapter(), expected_uid=1234, stop_event=stop_event)

    assert len(server.accept_calls) == 1
    assert server.responses == [{"reply": "session-stop"}]
    assert server.connections[0].closed
    assert server._connection is None and server._peer is None


def test_adapter_internal_oserror_is_not_misclassified_as_peer_eof():
    stop_event = threading.Event()
    server = _SequentialServer([[{"step": "workload-001"}]], stop_event)

    class _FailingAdapter(_Adapter):
        def handle_command(self, message, *, peer):
            raise OSError("application evidence write failed")

    try:
        _serve_test_adapter_peers(
            server, _FailingAdapter(), expected_uid=1234,
            stop_event=stop_event)
    except OSError as error:
        assert "evidence write failed" in str(error)
    else:
        raise AssertionError("adapter 내부 OSError가 peer EOF로 숨겨졌다")

    assert len(server.accept_calls) == 1
    assert server.connections[0].closed
