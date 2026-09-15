"""`PocSupervisor`: engine을 실제 transport·clock·spawner로 구동하는 production 진입점.

VDETER runtime(PoC workload)만 이 class를 생성한다. 생성 자체로는 socket·thread·
process가 생기지 않고, `start()`에서만 자원이 생긴다(기능 OFF 구조적 0). 종료는
finish → 자발 close → `SIGTERM` → `SIGKILL`의 bounded escalation으로 하고 모든 terminal
경로에서 process·socket·queue·lease 잔존을 검사한다(§15).
"""

from __future__ import annotations

import os
import selectors
import threading

from ..api.codec import CodecError, decode_wire_packet, encode_wire_packet
from ..api.errors import PocError, PocErrorCode
from ..api.limits import (
    CLOSE_VOLUNTARY_WAIT_S,
    FINISH_WAIT_S,
    HELLO_DEADLINE_S,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_TOTAL_DEADLINE_S,
    SIGTERM_WAIT_S,
)
from ..api.messages import parse_envelope
from .capture import WorkloadCapture
from .child import SIGKILL, SIGTERM
from .clock import RealMonotonicClock
from .credentials import read_peer_credential, verify_peer
from .engine import EngineState, SupervisorEngine
from . import socket_dir as sd
from .token import SecretRedactor, make_token_fd, new_token, token_hex
from .transport import OversizedPacket, SeqPacketServer


class PocSupervisor:
    def __init__(
        self,
        *,
        session_id: str,
        workload_id: str,
        run_id: str,
        scenario_id: str,
        manifest,
        server_config,
        provider,
        spawner,
        clock=None,
        socket_root: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._workload_id = workload_id
        self._run_id = run_id
        self._scenario_id = scenario_id
        self._manifest = manifest
        self._server_config = server_config
        self._provider = provider
        self._spawner = spawner
        self._clock = clock or RealMonotonicClock()
        self._socket_root = socket_root

        self._token = b""
        self.redactor = SecretRedactor()
        self.workload_socket: sd.WorkloadSocket | None = None
        self._server: SeqPacketServer | None = None
        self._child = None
        self.expected_pid: int | None = None
        self.expected_uid: int | None = None
        self.capture = WorkloadCapture()
        self._enqueue_lock = threading.Lock()
        self._selector = selectors.DefaultSelector()
        self._registered = False
        self._stream_keys: dict = {}
        self.engine: SupervisorEngine | None = None
        self.started = False
        self.closed = False

    # --- setup --------------------------------------------------------------
    def start(self) -> None:
        """socket 생성 → child spawn → accept → cred 검증 → handshake 시작(§5.1, §4.3)."""

        uid = sd.current_uid()
        self.expected_uid = uid
        root = self._socket_root or sd.select_socket_root(uid)
        self.workload_socket = sd.create_workload_socket(root, uid)
        self._server = SeqPacketServer(self.workload_socket.socket_path)

        self._token = new_token()
        self.redactor.register(token_hex(self._token))
        token_fd = make_token_fd(self._token)
        try:
            self._child = self._spawner.spawn(
                token_fd=token_fd,
                socket_path=self.workload_socket.socket_path,
                env={},
            )
        finally:
            try:
                os.close(token_fd)
            except OSError:
                pass
        self.expected_pid = self._child.pid

        peer = self._server.accept(timeout=HELLO_DEADLINE_S)
        verify_peer(peer, expected_uid=uid, expected_pid=self.expected_pid)

        self.engine = SupervisorEngine(
            session_id=self._session_id,
            workload_id=self._workload_id,
            run_id=self._run_id,
            scenario_id=self._scenario_id,
            expected_token=self._token,
            manifest=self._manifest,
            server_config=self._server_config,
            provider=self._provider,
        )
        self.engine.start_handshake(self._clock.monotonic())
        self._register_connection()
        self._register_child_streams()
        self.started = True

    def _register_connection(self) -> None:
        if self._registered:
            try:
                self._selector.unregister(self._server.connection)
            except (KeyError, ValueError):
                pass
            self._registered = False
        conn = self._server.connection
        if conn is not None:
            self._selector.register(conn, selectors.EVENT_READ, data="conn")
            self._registered = True

    def _register_child_streams(self) -> None:
        # child stdout/stderr를 non-blocking으로 pump loop에서 drain한다(§13, pipe deadlock 방지).
        for tag, stream in (
            ("stdout", getattr(self._child, "stdout", None)),
            ("stderr", getattr(self._child, "stderr", None)),
        ):
            if stream is None:
                continue
            try:
                os.set_blocking(stream.fileno(), False)
                self._selector.register(stream, selectors.EVENT_READ, data=tag)
                self._stream_keys[tag] = stream
            except (OSError, ValueError):
                pass

    def _read_child_stream(self, tag: str, stream) -> None:
        try:
            data = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            return
        except (OSError, ValueError):
            self._unregister_stream(tag, stream)
            return
        if data == b"":
            self._unregister_stream(tag, stream)
            return
        if tag == "stdout":
            self.capture.feed_stdout(data)
        else:
            self.capture.feed_stderr(data)
        if self.capture.overflowed and self.engine.fatal_error is None:
            self.engine.fatal_error = PocError(
                PocErrorCode.POC_OUTPUT_LIMIT_EXCEEDED, "stdout/stderr 누적 상한 초과"
            )
            self.engine.state = EngineState.ABORTED

    def _unregister_stream(self, tag: str, stream) -> None:
        try:
            self._selector.unregister(stream)
        except (KeyError, ValueError):
            pass
        self._stream_keys.pop(tag, None)

    # --- producer 측 bounded enqueue ---------------------------------------
    def emit_event(self, event_type: str, body: dict) -> None:
        with self._enqueue_lock:
            self.engine.enqueue_event(event_type, body, self._clock.monotonic())

    def emit_observation(self, record) -> None:
        with self._enqueue_lock:
            self.engine.enqueue_observation(record, self._clock.monotonic())

    def provide_action_terminal(self, request_id: str, *, action_execution: str,
                                target_effect: str | None = None) -> None:
        with self._enqueue_lock:
            out = self.engine.provide_action_terminal(
                request_id, action_execution=action_execution, target_effect=target_effect
            )
        self._send_all(out)

    # --- I/O loop -----------------------------------------------------------
    def _send_all(self, messages) -> None:
        for message in messages:
            self._server.send_packet(encode_wire_packet(message))

    def _handle_inbound(self, data: bytes) -> None:
        now = self._clock.monotonic()
        try:
            obj = decode_wire_packet(data)
            parsed = parse_envelope(obj)
        except (CodecError, PocError) as error:
            # strict JSON/envelope 위반은 protocol error로 유계 변환한다(§6.1, §14).
            self.engine.fatal_error = PocError(PocErrorCode.POC_PROTOCOL_ERROR, str(error))
            self.engine.state = EngineState.ABORTED
            return
        self._send_all(self.engine.receive(parsed, now))

    def pump_once(self, timeout: float = 0.05) -> str:
        """한 iteration: stdout/stderr drain + 수신 처리 → tick → event drain."""

        if self.engine.state in (EngineState.RUNNING,):
            self._send_all(self.engine.drain(self._clock.monotonic()))
        next_deadline = self.engine.next_deadline(self._clock.monotonic())
        select_timeout = timeout if next_deadline is None else min(timeout, next_deadline)
        for key, _mask in self._selector.select(timeout=select_timeout):
            if key.data == "conn":
                self._read_connection()
            else:
                self._read_child_stream(key.data, key.fileobj)
        self._send_all(self.engine.tick(self._clock.monotonic()))
        if self.engine.state in (EngineState.RUNNING,):
            self._send_all(self.engine.drain(self._clock.monotonic()))
        return self.engine.state.value

    def _read_connection(self) -> None:
        try:
            data = self._server.recv_packet(timeout=0.0)
        except OversizedPacket:
            self.engine.fatal_error = PocError(PocErrorCode.POC_PROTOCOL_ERROR, "oversized packet")
            self.engine.state = EngineState.ABORTED
            return
        except ConnectionError:
            self._unregister_connection()
            self._server.close_connection()
            self.engine.begin_reconnect_window(self._clock.monotonic())
            return
        if data is not None:
            self._handle_inbound(data)

    def _unregister_connection(self) -> None:
        if self._registered and self._server is not None and self._server.connection is not None:
            try:
                self._selector.unregister(self._server.connection)
            except (KeyError, ValueError):
                pass
        self._registered = False

    def reconnect_accept(self) -> bool:
        """disconnect 뒤 같은 child PID의 재접속을 총 2초·최대 3회로 수락한다(§12.2).

        새 process·다른 PID·새 token 협상은 허용하지 않는다(peer PID 재검증). 시간·횟수
        안에 복구하지 못하면 `POC_CONNECTION_LOST`로 abort한다. process를 재시작하지 않는다.
        """

        engine = self.engine
        if engine is None or engine.state != EngineState.RECONNECTING:
            return False
        deadline = self._clock.monotonic() + RECONNECT_TOTAL_DEADLINE_S
        for _attempt in range(RECONNECT_MAX_ATTEMPTS):
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                break
            try:
                peer = self._server.accept(timeout=remaining)
            except OSError:
                continue
            try:
                verify_peer(peer, expected_uid=self.expected_uid, expected_pid=self.expected_pid)
            except PocError:
                # 다른 PID/UID의 연결은 거부하고 재시도(process 재시작 금지).
                self._server.close_connection()
                continue
            self._register_connection()
            return True
        engine.fatal_error = PocError(PocErrorCode.POC_CONNECTION_LOST,
                                      "reconnect 2초·3회 안에 복구 실패")
        engine.state = EngineState.ABORTED
        return False

    def run_until(self, predicate, *, timeout: float, poll: float = 0.02) -> bool:
        """predicate가 참이거나 timeout까지 pump. predicate 만족 여부 반환."""

        deadline = self._clock.monotonic() + timeout
        while self._clock.monotonic() < deadline:
            if predicate():
                return True
            if self.engine.state == EngineState.RECONNECTING and not self._registered:
                self.reconnect_accept()  # 같은 PID 재접속 수락(§12.2). 성공 뒤 HELLO는 pump가 읽는다.
            else:
                self.pump_once(timeout=poll)
            if self.engine.state in (EngineState.ABORTED, EngineState.CLOSED):
                break
        return predicate()

    # --- 종료·cleanup escalation (§15.2) ------------------------------------
    @staticmethod
    def _error_code(error: BaseException) -> str:
        return error.code.value if isinstance(error, PocError) else f"EXCEPTION:{type(error).__name__}"

    def close_workload(self, *, infrastructure: str = "COMPLETED") -> dict:
        """finish → 자발 close → TERM → KILL 순서. 잔존 검사 결과를 돌려준다.

        R8: **protocol finish 성공 여부와 OS 자원 정리를 분리**한다. FINISH/CLOSE 송신이
        실패(BrokenPipe·ConnectionError 등)해도 child 종료·socket teardown·engine mark_closed는
        **항상** 시도한다. 한 정리 단계의 예외도 나머지 정리를 막지 않는다. 원래 protocol
        오류는 `report["protocol_error"]`로, 정리 단계 예외는 `report["cleanup_errors"]`로
        보존한다(삼켜서 완료로 위장하지 않는다). KeyboardInterrupt/SystemExit은 가능한 정리를
        마친 뒤 원형 그대로 재전달한다(§15.2). 메모리 정리만으로 RF 반환 완료를 선언하지 않는다.
        """

        report = {"forced_kill": False, "steps": [], "protocol_error": None, "cleanup_errors": []}
        engine = self.engine
        interrupt: list[BaseException] = []  # 첫 인터럽트만 보존해 정리 후 재전달

        def guard(fn, *, default=None, record_protocol=False):
            """한 단계를 실행하되 예외/인터럽트를 보존하고 나머지 정리를 잇는다."""
            try:
                return fn()
            except (KeyboardInterrupt, SystemExit) as exc:
                if not interrupt:
                    interrupt.append(exc)  # 정리 후 원형 재전달(첫 인터럽트 보존)
                return default
            except Exception as exc:  # noqa: BLE001 - 단계 오류 보존, 나머지 정리 계속
                if record_protocol and report["protocol_error"] is None:
                    report["protocol_error"] = self._error_code(exc)
                else:
                    report["cleanup_errors"].append(self._error_code(exc))
                return default

        # --- protocol finish/close (실패해도 OS 정리로 진행) ---
        def _protocol_close():
            if engine is not None and engine.state == EngineState.RUNNING:
                self._send_all(engine.begin_finish(self._clock.monotonic()))
                report["steps"].append("FINISH")
                self.run_until(lambda: engine.state != EngineState.FINISHING, timeout=FINISH_WAIT_S)
            if engine is not None and engine.state in (EngineState.FINISHING, EngineState.RUNNING):
                # FINISH_RESULT를 못 받았으면 on_finish 생략하고 CLOSE로 진행(§15.2).
                self._send_all(engine.force_close(self._clock.monotonic(), infrastructure=infrastructure))
                report["steps"].append("FORCE_CLOSE")
            elif engine is not None and engine.state == EngineState.CLOSING:
                report["steps"].append("CLOSE")

        guard(_protocol_close, record_protocol=True)

        # --- OS 자원 정리(항상 시도, 각 단계 독립) ---
        step = guard(self._terminate_child, default="CHILD_TERM_ERROR")
        report["steps"].append(step)
        if step == "SIGKILL":
            report["forced_kill"] = True
        guard(self._drain_child_output)
        guard(self._teardown_socket)
        if engine is not None:
            guard(engine.mark_closed)
        self.closed = True
        report["residual"] = self.residuals()
        if interrupt:
            report["interrupt"] = self._error_code(interrupt[0])
            raise interrupt[0]  # 가능한 정리를 마친 뒤 원래 인터럽트 재전달
        return report

    def _terminate_child(self) -> str:
        child = self._child
        if child is None:
            return "NO_CHILD"
        if child.poll_exit() is not None:
            child.reap()
            return "VOLUNTARY"
        if child.wait(timeout=CLOSE_VOLUNTARY_WAIT_S) is not None:
            return "VOLUNTARY"
        child.send_signal(SIGTERM)
        if child.wait(timeout=SIGTERM_WAIT_S) is not None:
            return "SIGTERM"
        child.send_signal(SIGKILL)
        child.wait(timeout=SIGTERM_WAIT_S)
        return "SIGKILL"

    def _drain_child_output(self) -> None:
        child = self._child
        if child is None:
            return
        for tag, stream, feeder in (
            ("stdout", getattr(child, "stdout", None), self.capture.feed_stdout),
            ("stderr", getattr(child, "stderr", None), self.capture.feed_stderr),
        ):
            if stream is None:
                continue
            self._unregister_stream(tag, stream)
            for _ in range(4096):  # bounded: child 종료 후 잔여 pipe를 비운다
                try:
                    data = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    break
                except (OSError, ValueError):
                    break
                if data == b"":
                    break
                feeder(data)
            try:
                stream.close()
            except OSError:
                pass

    def _teardown_socket(self) -> None:
        if self._registered and self._server is not None and self._server.connection is not None:
            try:
                self._selector.unregister(self._server.connection)
            except (KeyError, ValueError):
                pass
            self._registered = False
        if self._server is not None:
            self._server.close()
        if self.workload_socket is not None:
            sd.cleanup_workload_socket(self.workload_socket)
        try:
            self._selector.close()
        except Exception:  # noqa: BLE001
            pass

    def residuals(self) -> dict:
        """모든 terminal 경로 뒤 잔존 검사(§15.1.10, REQ-26)."""

        child_exit = self._child.poll_exit() if self._child is not None else None
        socket_exists = (
            self.workload_socket is not None and os.path.lexists(self.workload_socket.socket_path)
        )
        dir_exists = (
            self.workload_socket is not None and os.path.lexists(self.workload_socket.workload_dir)
        )
        engine_residual = self.engine.residual_snapshot() if self.engine is not None else {}
        return {
            "child_alive": self._child is not None and child_exit is None,
            "child_exit_code": child_exit,
            "socket_file_exists": bool(socket_exists),
            "workload_dir_exists": bool(dir_exists),
            "engine": engine_residual,
        }

    def evidence(self) -> dict:
        return {
            "socket": self.workload_socket.evidence() if self.workload_socket else {},
            "peer": {"expected_uid": self.expected_uid, "expected_pid": self.expected_pid},
            "engine": self.engine.evidence if self.engine else {},
            "stdio": self.capture.as_evidence(),
            "connection_epoch": self.engine.connection_epoch if self.engine else 0,
            "fatal_error": (
                self.engine.fatal_error.as_json_value()
                if self.engine and self.engine.fatal_error else None
            ),
        }


__all__ = ("PocSupervisor",)
