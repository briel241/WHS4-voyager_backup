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
        self.recv_timeouts = []
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
        self.recv_timeouts.append(timeout)
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
    # accept는 유계여야 stop_event를 실제로 관측한다 — teardown의 close()가
    # blocking accept를 깨운다는 전제는 Linux에서 성립하지 않는다.
    assert all(call[2] == 5.0 for call in server.accept_calls)
    assert all(timeout == 5.0 for timeout in server.recv_timeouts)
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


def test_adapter_internal_error_isolates_the_peer_not_the_listener(capsys):
    """어댑터 내부 오류는 그 피어만 끊고, 리스너는 다음 피어를 계속 받는다.

    fail-close의 단위가 리스너가 되면 상주 세션에서는 재앙이다: 이 소켓에
    START 대행·workload 4개·STOP 대행이 차례로 붙으므로, workload 2에서
    스레드가 죽으면 이후 전부가 300초 타임아웃으로 무너지고 bring-up
    25~30분까지 통째로 버려진다. 피어 단위로 격리하되, 오류가 정상 EOF로
    묻히지 않도록 종류와 메시지를 반드시 남긴다.
    """
    stop_event = threading.Event()
    server = _SequentialServer(
        [[{"step": "workload-001"}], [{"step": "session-stop"}]], stop_event)

    class _FailingOnceAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.seen = 0

        def handle_command(self, message, *, peer):
            self.seen += 1
            if self.seen == 1:
                raise OSError("application evidence write failed")
            stop_event.set()
            return {"reply": message["step"]}

    adapter = _FailingOnceAdapter()
    # 예외가 밖으로 새지 않아야 한다 — 새면 스레드가 죽고 리스너가 방치된다.
    _serve_test_adapter_peers(
        server, adapter, expected_uid=1234, stop_event=stop_event)

    # 실패한 피어의 연결만 닫히고, 다음 피어를 정상 accept했다.
    assert len(server.accept_calls) == 2
    assert server.connections[0].closed
    assert server.responses == [{"reply": "session-stop"}]
    assert server._connection is None and server._peer is None

    # 조용히 삼키지 않는다 — 종류와 메시지가 stderr에 남는다.
    captured = capsys.readouterr().err
    assert "OSError" in captured
    assert "evidence write failed" in captured
