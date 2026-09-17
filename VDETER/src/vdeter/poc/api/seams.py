"""Stage 4·5가 구현할 public seam(Protocol) 정의.

Stage 3은 이 seam들의 계약과 type만 고정하고, 실제 RF submit·scheduler·observation·
child process는 fake/test double로 대체한다(명세 §19, roadmap §3). Stage 5는 이후
G1-호환 production provider를 `ActionSubmitProvider`에 연결한다. common LO·DSP·analog
bandwidth·UHD 호출 자체는 Stage 3에서 구현하지 않는다.

`typing.Protocol`을 쓰므로 구현체는 명시적 상속 없이 duck-typing으로 만족한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import (
    ActionDescriptor,
    ObservationRecord,
    PhysicalChannel,
    ProviderSubmission,
)


@runtime_checkable
class MonotonicClock(Protocol):
    """deadline 측정용 monotonic clock. real sleep 대신 fake clock을 우선 쓴다(§18)."""

    def monotonic(self) -> float:
        ...


@runtime_checkable
class ActionSubmitProvider(Protocol):
    """논리 target action → canonical Physical Channel·parent transfer 유도 seam(§9.2, §9.4).

    - `derive_physical_channel`은 논리 target에서 canonical channel을 유도한다(ROC=lower,
      MASS-A=upper). client는 channel을 직접 고를 수 없다.
    - `submit`은 admission을 실제로 수행하고 canonical channel·parent id·transfer id를
      반환한다. server는 request_id별로 정확히 한 번만 호출하고 재요청은 cache로 답한다.

    Stage 3은 이 계약만 정의한다. 실제 scheduler·radio adapter·UHD 호출은 Stage 5·6의
    production provider가 담당한다.
    """

    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        ...

    def submit(self, action: ActionDescriptor, *, request_id: str) -> ProviderSubmission:
        ...


@runtime_checkable
class ObservationProvider(Protocol):
    """packet observation을 내는 seam(§8.4). Stage 3은 fake event provider만 쓴다.

    provider는 `NOT_TUNED` 관측도 반환할 수 있으며, 이 경우 supervisor는 event를
    만들지 않고 counter/state로만 기록한다. 실제 RF packet 수신·decode는 Stage 3에서
    구현하지 않는다.
    """

    def poll(self) -> ObservationRecord | None:
        ...


@runtime_checkable
class ChildHandle(Protocol):
    """spawn된 child process handle. signal·wait·reap seam."""

    @property
    def pid(self) -> int:
        ...

    def poll_exit(self) -> int | None:
        """종료했으면 exit code(또는 음수 signal), 아니면 None."""
        ...

    def send_signal(self, signum: int) -> None:
        ...

    def wait(self, timeout: float | None = None) -> int | None:
        ...

    def reap(self) -> int | None:
        ...


@runtime_checkable
class ChildSpawner(Protocol):
    """PoC child process 생성 seam. supervisor가 만든 read-only token FD를 넘긴다(§5.1)."""

    def spawn(self, *, token_fd: int, socket_path: str, env: dict) -> ChildHandle:
        ...


__all__ = (
    "MonotonicClock",
    "ActionSubmitProvider",
    "ObservationProvider",
    "ChildHandle",
    "ChildSpawner",
)
