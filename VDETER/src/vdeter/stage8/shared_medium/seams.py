"""Stage 5 내부 seam(Protocol). 실제 UHD/adapter는 Stage 6에서 연결하고, RF-off에서는
spy/fake로 대체한다. duck-typing(`typing.Protocol`)이므로 명시적 상속이 필요 없다.

- `WaveformFactory` : parent waveform을 **정확히 한 번** 생성한다(single-burst invariant).
- `RadioBurstSink`  : child path를 RF로 submit한다. RF-off에서는 submit 횟수를 세는 spy.
- `RxReadinessProvider` : required receiver의 RX window readiness를 반환한다(§6.2).
- `ResourceArbiter` : 정상 TX 우선 자원 예약. PoC는 겹치면 거부되고 정상 TX를 취소하지 않는다.

이 module은 radiation을 import하지 않는다(순환 방지). 인자 type은 duck-typed다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WaveformFactory(Protocol):
    """parent 논리 burst의 waveform(IqBurst)을 생성한다. fan-out과 무관하게 한 번만 호출한다."""

    def encode(self, *, source_station: str, channel, payload: bytes):
        """:class:`~vdeter.stage8.shared_medium.iq.IqBurst`를 반환한다."""
        ...


@runtime_checkable
class RadioBurstSink(Protocol):
    """child path 하나를 RF로 submit한다(Stage 6에서 실제 radio adapter). exactly-once로 호출된다."""

    def submit_child(self, child) -> None:
        ...


@runtime_checkable
class RxReadinessProvider(Protocol):
    """required receiver의 RX window readiness(§6.2). ready=False면 RF 전 fail-closed한다."""

    def window_ready(self, request) -> "ReadinessDecision":
        ...


@runtime_checkable
class ResourceArbiter(Protocol):
    """자원(slot/lane/time) 예약. 정상 scenario/infra TX가 PoC보다 우선한다(POC-D14).

    `reserve`가 False를 돌려주면 정상 TX가 이미 점유한 것이며, PoC action만 거부한다.
    정상 TX를 취소·재예약하지 않는다.
    """

    def reserve(self, *, resource_key: str, owner: str) -> bool:
        ...

    def release(self, *, resource_key: str, owner: str) -> None:
        ...


__all__ = (
    "WaveformFactory",
    "RadioBurstSink",
    "RxReadinessProvider",
    "ResourceArbiter",
)
