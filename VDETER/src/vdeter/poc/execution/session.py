"""resident session lifecycle와 deadline·lease 관리(실행 명세 §5.2, §5.4, `S8-POC-REQ-26/27`).

한 번 bring-up한 session에서 여러 workload를 순차 실행한다. 새 session daemon을 만들지
않고 기존 Stage 7 resident session을 재사용하는 것을 모델링한다(RF-off).

두 출처의 상한을 함께 적용한다(§5.2, `limits.SessionPolicy`).

- 기존 Stage 7 코드 상한(불변): bring-up 2700초·workload 900초·close 600초.
- Stage 8 신규 정책 상한: session absolute 7200초·READY idle 2700초.

어느 상한도 PoC 활동(rescan·menu render·callback·action 제출)으로 연장·재무장하지 않는다.
READY idle timer는 **정상 workload 완료 뒤에만** 다시 시작한다(§5.2). session absolute는
결코 reset하지 않는다. deadline은 monotonic clock으로만 측정한다(busy-wait 없음, §3.3).

workload 단위 격리(§5.4): workload마다 lease를 새로 잡고 반환하며, Python state·queue·
request namespace를 다음 workload로 넘기지 않는다(runner·connection이 workload마다 새로 생성).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..api.seams import MonotonicClock
from .lease import MassBLease
from .limits import SessionPolicy


class SessionState(str, Enum):
    INIT = "INIT"
    BRINGUP = "BRINGUP"
    READY = "READY"
    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class Deadline(str, Enum):
    """초과할 수 있는 상한 종류(§5.2)."""

    SESSION_ABSOLUTE = "SESSION_ABSOLUTE"
    READY_IDLE = "READY_IDLE"
    BRINGUP = "BRINGUP"
    WORKLOAD = "WORKLOAD"
    CLOSE = "CLOSE"


class SessionExpired(Exception):
    """상한 초과(fail-closed). 어느 deadline인지 담는다."""

    def __init__(self, deadline: Deadline, detail: str = "") -> None:
        self.deadline = deadline
        super().__init__(f"{deadline.value}: {detail}" if detail else deadline.value)


class SessionStateError(Exception):
    """현재 session 상태에서 허용되지 않는 조작(§6.1: rescan/menu는 READY에서만)."""


@dataclass
class ResidentSession:
    """resident session의 순수 상태·deadline 모델(§5.2). 실제 bring-up/RF는 Stage 6."""

    clock: MonotonicClock
    lease: MassBLease = field(default_factory=MassBLease)
    policy: SessionPolicy = field(default_factory=SessionPolicy)
    state: SessionState = SessionState.INIT

    started_at: float | None = None       # coordinator 시작(절대 deadline 기준)
    bringup_started_at: float | None = None
    ready_since: float | None = None       # 마지막으로 READY에 진입한 시각(idle 기준)
    workload_started_at: float | None = None
    closing_started_at: float | None = None
    workloads_run: int = 0
    expired_deadline: Deadline | None = None  # first expiry survives cleanup deadlines

    # --- 시작 / bring-up ----------------------------------------------------
    def start(self) -> None:
        """coordinator 시작. session absolute deadline의 기준점을 잡는다(§5.2)."""

        if self.state != SessionState.INIT:
            raise SessionExpired(Deadline.SESSION_ABSOLUTE, "이미 시작된 session")
        self.started_at = self.clock.monotonic()
        self.bringup_started_at = self.started_at
        self.state = SessionState.BRINGUP

    def complete_bringup(self) -> None:
        """bring-up 완료 → READY. bring-up 상한(2700초)과 absolute를 확인한다(§5.2)."""

        if self.state != SessionState.BRINGUP:
            raise SessionExpired(Deadline.BRINGUP, "BRINGUP 상태가 아님")
        now = self.clock.monotonic()
        self._check_absolute(now)
        if now - self.bringup_started_at > self.policy.service.bringup_s:
            self._expire(Deadline.BRINGUP, "bring-up 상한 초과")
        self._enter_ready(now)

    def _enter_ready(self, now: float) -> None:
        self.state = SessionState.READY
        self.ready_since = now

    # --- deadline 계산(읽기 전용; timer 없음) --------------------------------
    def remaining_session_absolute(self, now: float | None = None) -> float:
        now = self.clock.monotonic() if now is None else now
        if self.started_at is None:
            return self.policy.session_absolute_s
        return self.policy.session_absolute_s - (now - self.started_at)

    def remaining_ready_idle(self, now: float | None = None) -> float:
        now = self.clock.monotonic() if now is None else now
        if self.state != SessionState.READY or self.ready_since is None:
            return self.policy.ready_idle_s
        return self.policy.ready_idle_s - (now - self.ready_since)

    def _expire(self, deadline: Deadline, detail: str) -> None:
        if self.expired_deadline is None:
            self.expired_deadline = deadline
        self.state = SessionState.EXPIRED
        raise SessionExpired(deadline, detail)

    def _check_close(self, now: float) -> None:
        if (self.state != SessionState.CLOSED and self.closing_started_at is not None
                and now - self.closing_started_at > self.policy.service.close_s):
            self._expire(Deadline.CLOSE, "close 상한 초과")

    def _check_absolute(self, now: float) -> None:
        if self.started_at is None:
            return
        if now - self.started_at > self.policy.session_absolute_s:
            self._expire(Deadline.SESSION_ABSOLUTE, "session absolute 상한 초과")

    def check_deadlines(self, now: float | None = None) -> None:
        """현재 상태에서 적용되는 상한을 확인한다(§5.2). 초과 시 :class:`SessionExpired`.

        menu render·rescan에서 호출해도 timer를 재무장하지 않는다(읽기 전용 확인).
        """

        now = self.clock.monotonic() if now is None else now
        self._check_close(now)
        self._check_absolute(now)
        if self.state == SessionState.BRINGUP and self.bringup_started_at is not None:
            if now - self.bringup_started_at > self.policy.service.bringup_s:
                self._expire(Deadline.BRINGUP, "bring-up 상한 초과")
        if self.state == SessionState.READY and self.ready_since is not None:
            if now - self.ready_since > self.policy.ready_idle_s:
                self._expire(Deadline.READY_IDLE, "READY idle 상한 초과")
        if self.state == SessionState.RUNNING and self.workload_started_at is not None:
            if now - self.workload_started_at > self.policy.service.workload_s:
                self._expire(Deadline.WORKLOAD, "workload 상한 초과")

    # --- workload 경계 ------------------------------------------------------
    def begin_workload(self) -> None:
        """READY에서 workload를 시작한다(§5.2, §5.4). deadline을 먼저 확인한다."""

        if self.state != SessionState.READY:
            raise SessionExpired(Deadline.SESSION_ABSOLUTE, "READY 상태가 아님")
        now = self.clock.monotonic()
        self.check_deadlines(now)  # absolute + idle
        self.state = SessionState.RUNNING
        self.workload_started_at = now
        # idle timer는 workload 시작으로 소비된다(연장 아님). ready_since는 종료 시 재설정.
        self.ready_since = None

    def end_workload(self) -> None:
        """workload 완료 → READY(§5.2). workload 상한을 확인하고 idle timer를 다시 시작한다."""

        if self.state != SessionState.RUNNING:
            raise SessionExpired(Deadline.WORKLOAD, "RUNNING 상태가 아님")
        now = self.clock.monotonic()
        self._check_absolute(now)
        if now - self.workload_started_at > self.policy.service.workload_s:
            self._expire(Deadline.WORKLOAD, "workload 상한 초과")
        self.workloads_run += 1
        self.workload_started_at = None
        self._enter_ready(now)  # 정상 workload 뒤 idle timer 다시 시작(§5.2)

    # --- rescan / menu (읽기 전용; 상한 연장 없음) ---------------------------
    def note_control_plane_activity(self, *, require_ready: bool = True) -> None:
        """rescan·menu render 같은 control-plane 활동. deadline을 확인만 하고 연장하지 않는다.

        - idle timer(ready_since)와 absolute를 **건드리지 않는다**(§5.2: PoC 활동으로 연장 금지).
        - rescan/menu는 `READY` 상태에서만 허용한다(§6.1). 진행 중 workload(RUNNING)에서
          호출하면 :class:`SessionStateError`로 거부해 실행 중 workload에 영향을 주지 않는다.
        """

        if require_ready and self.state != SessionState.READY:
            raise SessionStateError(
                f"rescan/menu는 READY에서만 허용합니다(현재 {self.state.value})")
        self.check_deadlines()

    # --- close --------------------------------------------------------------
    def begin_close(self) -> None:
        if self.state == SessionState.CLOSED or self.closing_started_at is not None:
            return
        self.closing_started_at = self.clock.monotonic()
        if self.state != SessionState.EXPIRED:
            self.state = SessionState.CLOSING

    def complete_close(self) -> None:
        try:
            if self.state != SessionState.CLOSED:
                now = self.clock.monotonic()
                self._check_close(now)
                self._check_absolute(now)
        finally:
            # Expiry must not bypass lease cleanup (§13.9).
            self.lease.release()
            self.lease.assert_returned()
        if self.state != SessionState.EXPIRED:
            self.state = SessionState.CLOSED

    def evidence(self) -> dict:
        return {
            "state": self.state.value,
            "expired_deadline": (self.expired_deadline.value
                                 if self.expired_deadline is not None else None),
            "workloads_run": self.workloads_run,
            "policy": {
                "session_absolute_s": self.policy.session_absolute_s,
                "ready_idle_s": self.policy.ready_idle_s,
                "bringup_s": self.policy.service.bringup_s,
                "workload_s": self.policy.service.workload_s,
                "close_s": self.policy.service.close_s,
            },
            "lease": self.lease.as_evidence(),
        }


__all__ = (
    "SessionState",
    "Deadline",
    "SessionExpired",
    "SessionStateError",
    "ResidentSession",
)
