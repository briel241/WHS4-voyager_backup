"""Stage 8 연결 API 시험용 public test double.

fake clock·fake provider·fake observation·thread child spawner와 client runner를 담는다.
Stage 4·5 branch도 이 double로 public 계약을 검증할 수 있다(private 구현 미의존).
"""

from __future__ import annotations

from .fakes import (
    FakeActionSubmitProvider,
    FakeClock,
    FakeObservationProvider,
    ThreadChildHandle,
    ThreadChildSpawner,
    build_manifest,
    hello_payload,
)
from .client_runner import ClientRunner, RecordingCallbacks

__all__ = (
    "FakeClock",
    "FakeActionSubmitProvider",
    "FakeObservationProvider",
    "ThreadChildHandle",
    "ThreadChildSpawner",
    "build_manifest",
    "hello_payload",
    "ClientRunner",
    "RecordingCallbacks",
)
