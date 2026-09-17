"""peer credential 확인(명세 §4.3).

Linux `SO_PEERCRED`로 peer PID/UID/GID를 읽고 VDETER 실행 UID와 supervisor가 spawn한
exact child PID에 결속한다. 불일치는 `POC_AUTH_FAILED`(연결 전 abort).
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from ..api.errors import PocError, PocErrorCode

_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


@dataclass(frozen=True, slots=True)
class PeerCredential:
    pid: int
    uid: int
    gid: int


def read_peer_credential(connection: socket.socket) -> PeerCredential:
    """`SO_PEERCRED`에서 (pid, uid, gid)."""

    data = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", data)
    return PeerCredential(pid=pid, uid=uid, gid=gid)


def verify_peer(
    peer: PeerCredential, *, expected_uid: int, expected_pid: int
) -> None:
    """peer UID와 child PID를 둘 다 대조한다. 불일치는 `POC_AUTH_FAILED`.

    이 검사는 같은 UID 악성 코드를 격리하는 OS sandbox가 아니라, 다른 사용자·다른
    workload의 우발적·비인가 연결을 막는 session binding이다(§4.3).
    """

    if peer.uid != expected_uid:
        raise PocError(
            PocErrorCode.POC_AUTH_FAILED,
            "peer UID 불일치",
            detail={"expected_uid": expected_uid, "actual_uid": peer.uid},
        )
    if peer.pid != expected_pid:
        raise PocError(
            PocErrorCode.POC_AUTH_FAILED,
            "peer PID 불일치",
            detail={"expected_pid": expected_pid, "actual_pid": peer.pid},
        )


__all__ = ("PeerCredential", "read_peer_credential", "verify_peer")
