"""Stage 7 Unix-domain `SOCK_SEQPACKET` transport (`stage7` §16).

Linux local IPC 전용이다. socket root는
`/run/user/<uid>/vdeter-stage7/runtime/<run-id>/`이고 directory mode `0700`,
endpoint socket mode `0600`이다. endpoint는 `SO_PEERCRED`의 UID와 Runtime
Coordinator가 직접 spawn해 기록한 PID를 모두 확인하고 하나라도 다르면 message를
처리하지 않는다.
"""

from __future__ import annotations

import os
import socket
import struct

from .ipc import MAX_RECORD_BYTES, decode_message, encode_message

_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


def runtime_socket_root(run_id: str) -> str:
    uid = os.getuid()  # type: ignore[attr-defined]
    return f"/run/user/{uid}/vdeter-stage7/runtime/{run_id}"


def testing_socket_root(run_id: str) -> str:
    uid = os.getuid()  # type: ignore[attr-defined]
    return f"/run/user/{uid}/vdeter-stage7/testing/{run_id}"


def ensure_socket_root(root: str) -> None:
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)


def peer_credentials(connection: socket.socket) -> tuple[int, int]:
    """(pid, uid) — Linux `SO_PEERCRED`."""

    data = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", data)
    return pid, uid


class SeqPacketServer:
    """endpoint-side socket 하나 (accept 후 단일 peer)."""

    def __init__(self, path: str) -> None:
        if len(path.encode("utf-8")) + 1 > 108:
            raise ValueError("Unix socket path가 108 byte를 넘습니다")
        self.path = path
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._listener.bind(path)
        os.chmod(path, 0o600)
        self._listener.listen(1)
        self._connection = None
        self._peer = None

    def accept(self, *, expected_pid: int | None, expected_uid: int | None, timeout=None):
        self._listener.settimeout(timeout)
        connection, _address = self._listener.accept()
        pid, uid = peer_credentials(connection)
        if expected_uid is not None and uid != expected_uid:
            connection.close()
            raise PermissionError(f"peer UID 불일치: {uid} != {expected_uid}")
        if expected_pid is not None and pid != expected_pid:
            connection.close()
            raise PermissionError(f"peer PID 불일치: {pid} != {expected_pid}")
        self._connection = connection
        self._peer = (pid, uid)
        return self._peer

    @property
    def peer(self):
        return self._peer

    def recv_message(self, timeout=None) -> dict | None:
        self._connection.settimeout(timeout)
        try:
            data = self._connection.recv(MAX_RECORD_BYTES)
        except socket.timeout:
            return None
        if not data:
            raise ConnectionError("peer가 연결을 닫았습니다")
        return decode_message(data)

    def send_message(self, message: dict) -> None:
        self._connection.sendall(encode_message(message))

    def close(self) -> None:
        for handle in (self._connection, self._listener):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class SeqPacketClient:
    """coordinator-side connection 하나."""

    def __init__(self, path: str, *, expected_uid: int | None = None) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._socket.connect(path)
        self.path = path
        self.peer = None

    def request(self, message: dict, *, timeout=None) -> dict:
        self._socket.settimeout(timeout)
        self._socket.sendall(encode_message(message))
        data = self._socket.recv(MAX_RECORD_BYTES)
        if not data:
            raise ConnectionError("peer가 연결을 닫았습니다")
        return decode_message(data)

    def send(self, message: dict) -> None:
        self._socket.sendall(encode_message(message))

    def recv(self, timeout=None) -> dict | None:
        self._socket.settimeout(timeout)
        try:
            data = self._socket.recv(MAX_RECORD_BYTES)
        except socket.timeout:
            return None
        if not data:
            raise ConnectionError("peer가 연결을 닫았습니다")
        return decode_message(data)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


__all__ = (
    "SeqPacketClient",
    "SeqPacketServer",
    "ensure_socket_root",
    "peer_credentials",
    "runtime_socket_root",
    "testing_socket_root",
)
