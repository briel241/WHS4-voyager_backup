"""server 측 Unix domain `SOCK_SEQPACKET` transport(private; 명세 §4.1).

bind/listen/accept와 `SO_PEERCRED` peer 확인은 supervisor(server)만 쓴다. I/O(send/recv,
MSG_TRUNC 거부)는 public `api.transport.SeqPacketEndpoint`를 재사용한다.
"""

from __future__ import annotations

import os
import socket

from ..api.errors import PocError, PocErrorCode
from ..api.limits import SOCKET_MODE
from ..api.transport import OversizedPacket, SeqPacketEndpoint, require_seqpacket
from .credentials import PeerCredential, read_peer_credential


class SeqPacketServer:
    """workload socket 하나. accept 후 단일 peer(§4.1)."""

    def __init__(self, path: str) -> None:
        require_seqpacket()
        self.path = path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._listener.bind(path)
        os.chmod(path, SOCKET_MODE)
        self._listener.listen(1)
        self._raw_conn: socket.socket | None = None
        self._endpoint: SeqPacketEndpoint | None = None

    def fileno(self) -> int:
        if self._raw_conn is not None:
            return self._raw_conn.fileno()
        return self._listener.fileno()

    def accept(self, timeout: float | None = None) -> PeerCredential:
        self._listener.settimeout(timeout)
        connection, _addr = self._listener.accept()
        self._listener.settimeout(None)
        self._raw_conn = connection
        self._endpoint = SeqPacketEndpoint(connection)
        return read_peer_credential(connection)

    @property
    def connection(self):
        return self._raw_conn

    def recv_packet(self, timeout: float | None = None) -> bytes | None:
        if self._endpoint is None:
            raise PocError(PocErrorCode.POC_TRANSPORT_UNAVAILABLE, "연결이 없습니다")
        return self._endpoint.recv_packet(timeout=timeout)

    def send_packet(self, data: bytes) -> None:
        if self._endpoint is None:
            raise PocError(PocErrorCode.POC_TRANSPORT_UNAVAILABLE, "연결이 없습니다")
        self._endpoint.send_packet(data)

    def close_connection(self) -> None:
        if self._raw_conn is not None:
            try:
                self._raw_conn.close()
            except OSError:
                pass
            self._raw_conn = None
            self._endpoint = None

    def close(self) -> None:
        self.close_connection()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass


__all__ = ("OversizedPacket", "SeqPacketServer")
