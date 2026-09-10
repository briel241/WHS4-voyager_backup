"""client 측 Unix domain `SOCK_SEQPACKET` transport(public; 명세 §4.1).

외부 PoC SDK(child)가 이 module로 연결한다. server 측 bind/listen/accept와 peercred는
production private(`_supervisor.transport`)에 있다. packet boundary가 message boundary이며
길이 prefix·개행 framing을 쓰지 않는다. 수신 buffer보다 큰 packet의 `MSG_TRUNC`는 부분
parse하지 않고 oversized로 거부한다.
"""

from __future__ import annotations

import socket

from .errors import PocError, PocErrorCode
from .limits import MAX_WIRE_PACKET_BYTES

_MSG_TRUNC = getattr(socket, "MSG_TRUNC", 0)


class OversizedPacket(Exception):
    """수신 datagram이 wire 상한을 넘었다(MSG_TRUNC)."""


def require_seqpacket() -> None:
    if not hasattr(socket, "SOCK_SEQPACKET") or not hasattr(socket, "AF_UNIX"):
        raise PocError(
            PocErrorCode.POC_TRANSPORT_UNAVAILABLE,
            "이 플랫폼은 AF_UNIX/SOCK_SEQPACKET을 지원하지 않습니다(stream fallback 금지)",
        )


class SeqPacketEndpoint:
    """연결된 `SOCK_SEQPACKET` socket 하나를 감싼 send/recv endpoint."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock

    def fileno(self) -> int:
        return self._socket.fileno()

    def send_packet(self, data: bytes) -> None:
        self._socket.sendall(data)

    def recv_packet(self, timeout: float | None = None) -> bytes | None:
        self._socket.settimeout(timeout)
        try:
            data, _anc, flags, _addr = self._socket.recvmsg(MAX_WIRE_PACKET_BYTES)
        except socket.timeout:
            return None
        if flags & _MSG_TRUNC:
            raise OversizedPacket(f"datagram이 {MAX_WIRE_PACKET_BYTES} byte를 넘습니다")
        if not data:
            raise ConnectionError("peer가 연결을 닫았습니다")
        return data

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def connect(path: str) -> SeqPacketEndpoint:
    """client로 socket path에 연결한다."""

    require_seqpacket()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.connect(path)
    return SeqPacketEndpoint(sock)


def connected_pair() -> tuple[SeqPacketEndpoint, SeqPacketEndpoint]:
    """시험용 in-process 연결 쌍(`socket.socketpair`, AF_UNIX/SOCK_SEQPACKET).

    실제 Linux `SOCK_SEQPACKET`이며 packet boundary를 보존한다.
    """

    require_seqpacket()
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    return SeqPacketEndpoint(a), SeqPacketEndpoint(b)


__all__ = (
    "OversizedPacket",
    "require_seqpacket",
    "SeqPacketEndpoint",
    "connect",
    "connected_pair",
)
