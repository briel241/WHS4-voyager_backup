"""stdout/stderr의 저부하 bounded capture(명세 §13).

- stream별 최대 64 KiB만 보존(앞 32 KiB + 뒤 32 KiB, 중간 생략 byte 수 기록).
- 실제 drain한 stdout+stderr 합산이 256 KiB를 넘으면 overflow로 표시(호출자가
  `POC_OUTPUT_LIMIT_EXCEEDED`로 abort).
- UTF-8 decode 불가 byte는 손실 없이 보존(base64).
- RF 중 터미널 전달·파일 flush·line별 render를 하지 않는다(이 buffer는 memory-only).
"""

from __future__ import annotations

import base64

from ..poc_api.limits import (
    STDIO_RETAIN_HEAD_BYTES,
    STDIO_RETAIN_TAIL_BYTES,
    STDIO_TOTAL_DRAIN_LIMIT_BYTES,
)


class BoundedStream:
    """한 stream의 bounded capture. 앞/뒤를 보존하고 중간 생략을 센다."""

    def __init__(self, head_bytes: int = STDIO_RETAIN_HEAD_BYTES,
                 tail_bytes: int = STDIO_RETAIN_TAIL_BYTES) -> None:
        self._head_limit = head_bytes
        self._tail_limit = tail_bytes
        self._head = bytearray()
        self._tail = bytearray()
        self.emitted = 0  # 실제 drain한 총 byte
        self.discarded = 0  # 중간 생략 byte

    def feed(self, data: bytes) -> None:
        self.emitted += len(data)
        data = bytes(data)
        if len(self._head) < self._head_limit:
            take = min(self._head_limit - len(self._head), len(data))
            self._head += data[:take]
            data = data[take:]
        if not data:
            return
        # 남은 byte는 tail에 넣고 마지막 tail_limit byte만 보존(중간 생략 계수).
        self._tail += data
        if len(self._tail) > self._tail_limit:
            overflow = len(self._tail) - self._tail_limit
            self.discarded += overflow
            del self._tail[:overflow]

    @property
    def retained(self) -> int:
        return len(self._head) + len(self._tail)

    def retained_bytes(self) -> bytes:
        return bytes(self._head) + bytes(self._tail)

    def as_evidence(self) -> dict:
        raw = self.retained_bytes()
        try:
            text = raw.decode("utf-8")
            text_field = {"text": text}
        except UnicodeDecodeError:
            text_field = {"base64": base64.b64encode(raw).decode("ascii")}
        return {
            "emitted_bytes": self.emitted,
            "retained_bytes": self.retained,
            "discarded_bytes": self.discarded,
            "truncated": self.discarded > 0,
            **text_field,
        }


class WorkloadCapture:
    """workload의 stdout+stderr capture와 누적 drain 상한(§13.2)."""

    def __init__(self, total_limit: int = STDIO_TOTAL_DRAIN_LIMIT_BYTES) -> None:
        self.stdout = BoundedStream()
        self.stderr = BoundedStream()
        self._total_limit = total_limit
        self.overflowed = False

    def feed_stdout(self, data: bytes) -> None:
        self.stdout.feed(data)
        self._check_overflow()

    def feed_stderr(self, data: bytes) -> None:
        self.stderr.feed(data)
        self._check_overflow()

    def _check_overflow(self) -> None:
        if self.stdout.emitted + self.stderr.emitted > self._total_limit:
            self.overflowed = True

    @property
    def total_emitted(self) -> int:
        return self.stdout.emitted + self.stderr.emitted

    def as_evidence(self) -> dict:
        return {
            "stdout": self.stdout.as_evidence(),
            "stderr": self.stderr.as_evidence(),
            "total_emitted_bytes": self.total_emitted,
            "total_limit_bytes": self._total_limit,
            "overflowed": self.overflowed,
        }


__all__ = ("BoundedStream", "WorkloadCapture")
