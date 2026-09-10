"""workload 인증 token 생성·비교·FD 전달·redaction(명세 §5).

- workload마다 CSPRNG로 256-bit token을 만든다.
- command line이나 일반 env에 넣지 않고 inherited read-only FD로 한 번 전달한다.
- 비교는 constant-time이다.
- token 원문은 console·error·exception·evidence·argv에 기록하지 않는다.
"""

from __future__ import annotations

import hmac
import os
import secrets

from ..poc_api.limits import TOKEN_BYTES

#: evidence·로그에서 secret을 가린 표시.
REDACTED = "<redacted>"


def new_token() -> bytes:
    """CSPRNG 256-bit token."""

    return secrets.token_bytes(TOKEN_BYTES)


def token_hex(token: bytes) -> str:
    return token.hex()


def constant_time_equals(a: bytes, b: bytes) -> bool:
    """constant-time 비교(§5.2)."""

    return hmac.compare_digest(bytes(a), bytes(b))


def make_token_fd(token: bytes) -> int:
    """token을 담은 inherited read-only pipe read end FD를 만든다(§5.1).

    write end에 token을 쓰고 닫은 뒤 read end FD만 반환한다. 반환 FD는 상속 가능
    (`set_inheritable(True)`)하며 child에게 FD number만 env로 넘긴다. 호출자는 child
    spawn 뒤 이 FD를 닫아야 한다.
    """

    read_fd, write_fd = os.pipe()
    try:
        written = os.write(write_fd, token)
        if written != len(token):
            raise OSError("token FD write가 불완전합니다")
    finally:
        os.close(write_fd)
    os.set_inheritable(read_fd, True)
    return read_fd


class SecretRedactor:
    """등록된 secret(token 원문 등)을 문자열에서 가린다(§5.2).

    evidence·오류 문자열을 만들 때 통과시켜 token 원문 유출을 막는다.
    """

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def register(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: str) -> str:
        out = text
        for secret in self._secrets:
            if secret:
                out = out.replace(secret, REDACTED)
        return out


__all__ = (
    "REDACTED",
    "new_token",
    "token_hex",
    "constant_time_equals",
    "make_token_fd",
    "SecretRedactor",
)
