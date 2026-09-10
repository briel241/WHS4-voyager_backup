"""resident session deadline fake-clock 경계 시험(실행 명세 §5.2, `S8-POC-REQ-26`). platform 무관.

session absolute / READY idle / workload / close 경계와 "PoC 활동으로 연장 금지"를 검증하고,
기존 Stage 7 코드 상한(bring-up 2700·workload 900·close 600)이 무변경임을 diff-audit한다.
"""

from __future__ import annotations

import os
import re
import unittest

from vdeter.stage8.execution.limits import (
    READY_IDLE_TIMEOUT_S,
    SESSION_ABSOLUTE_DEADLINE_S,
    STAGE7_BRINGUP_TIMEOUT_S,
    STAGE7_CLOSE_TIMEOUT_S,
    STAGE7_WORKLOAD_TIMEOUT_S,
    SessionPolicy,
)
from vdeter.stage8.execution.session import (
    Deadline,
    ResidentSession,
    SessionExpired,
    SessionState,
    SessionStateError,
)
from vdeter.stage8.testing.fakes import FakeClock


def _ready_session(clock):
    s = ResidentSession(clock=clock)
    s.start()
    s.complete_bringup()
    return s


class NewPolicyLimitValuesTest(unittest.TestCase):
    def test_new_policy_values(self):
        self.assertEqual(SESSION_ABSOLUTE_DEADLINE_S, 7200.0)
        self.assertEqual(READY_IDLE_TIMEOUT_S, 2700.0)

    def test_stage7_mirror_values(self):
        self.assertEqual(STAGE7_BRINGUP_TIMEOUT_S, 2700.0)
        self.assertEqual(STAGE7_WORKLOAD_TIMEOUT_S, 900.0)
        self.assertEqual(STAGE7_CLOSE_TIMEOUT_S, 600.0)


class Stage7ConstantsUnchangedTest(unittest.TestCase):
    """기존 Stage 7 코드 상한이 무변경인지 authoritative 파일에서 직접 확인(§5.2)."""

    def _find_apps(self):
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.normpath(os.path.join(here, "..", "..", ".."))
        return os.path.join(root, "apps", "stage7_session.py")

    def test_apps_stage7_constants_match_mirror(self):
        path = self._find_apps()
        if not os.path.isfile(path):
            self.skipTest("apps/stage7_session.py not found in this checkout")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        pairs = {
            "SESSION_BRINGUP_TIMEOUT_S": STAGE7_BRINGUP_TIMEOUT_S,
            "WORKLOAD_TIMEOUT_S": STAGE7_WORKLOAD_TIMEOUT_S,
            "SESSION_CLOSE_TIMEOUT_S": STAGE7_CLOSE_TIMEOUT_S,
        }
        for name, expected in pairs.items():
            m = re.search(rf"^{name}\s*=\s*([0-9.]+)", text, re.M)
            self.assertIsNotNone(m, f"{name} not found")
            self.assertEqual(float(m.group(1)), expected,
                             f"{name} changed in apps/stage7_session.py")


class SessionAbsoluteDeadlineTest(unittest.TestCase):
    def test_absolute_boundary(self):
        # absolute만 격리하려면 idle이 먼저 트리거되지 않게 ready_since를 최근으로 둔다.
        clock = FakeClock(0.0)
        s = ResidentSession(clock=clock)
        s.state = SessionState.READY
        s.started_at = 0.0
        s.ready_since = 5000.0  # idle = 2200s < 2700, absolute만 검증
        clock.set(SESSION_ABSOLUTE_DEADLINE_S)  # 7200 정확히: > 아님 → OK
        s.check_deadlines()
        clock.set(SESSION_ABSOLUTE_DEADLINE_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.SESSION_ABSOLUTE)

    def test_absolute_counts_bringup_time(self):
        # bring-up에 쓴 시간도 2시간에서 차감된다(§5.2).
        clock = FakeClock(0.0)
        s = ResidentSession(clock=clock)
        s.start()
        clock.set(100.0)
        s.complete_bringup()
        # 이제 절대 remaining = 7200 - 100.
        self.assertAlmostEqual(s.remaining_session_absolute(clock.monotonic()),
                               SESSION_ABSOLUTE_DEADLINE_S - 100.0)


class ReadyIdleDeadlineTest(unittest.TestCase):
    def test_idle_boundary(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        clock.set(1000.0 + READY_IDLE_TIMEOUT_S)
        s.check_deadlines()  # 경계 OK
        clock.set(1000.0 + READY_IDLE_TIMEOUT_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.READY_IDLE)

    def test_idle_resets_after_workload(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        # 2000초 idle
        clock.set(3000.0)
        s.check_deadlines()
        s.begin_workload()
        clock.set(3100.0)
        s.end_workload()  # 정상 workload 뒤 idle timer 재시작
        # 다시 2000초 idle OK (누적 아님)
        clock.set(3100.0 + 2000.0)
        s.check_deadlines()
        self.assertEqual(s.state, SessionState.READY)

    def test_control_plane_activity_does_not_extend_idle(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        clock.set(1000.0 + 1000.0)
        s.note_control_plane_activity()  # rescan/menu: 연장 금지
        # idle은 여전히 1000초부터 측정 → 2700 초과에서 만료
        clock.set(1000.0 + READY_IDLE_TIMEOUT_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.READY_IDLE)


class ControlPlaneStateTest(unittest.TestCase):
    """rescan/menu는 READY에서만 허용한다(§6.1: 진행 중 workload에 영향 금지)."""

    def test_allowed_in_ready(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.note_control_plane_activity()  # 예외 없음
        self.assertEqual(s.state, SessionState.READY)

    def test_rejected_while_workload_running(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.begin_workload()
        with self.assertRaises(SessionStateError):
            s.note_control_plane_activity()
        # 거부돼도 진행 중 workload 상태는 그대로다.
        self.assertEqual(s.state, SessionState.RUNNING)

    def test_rejected_before_bringup(self):
        clock = FakeClock(0.0)
        s = ResidentSession(clock=clock)
        s.start()
        with self.assertRaises(SessionStateError):
            s.note_control_plane_activity()


class WorkloadDeadlineTest(unittest.TestCase):
    def test_workload_boundary(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.begin_workload()
        clock.set(1000.0 + STAGE7_WORKLOAD_TIMEOUT_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.WORKLOAD)

    def test_poc_activity_does_not_extend_workload(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.begin_workload()
        # 여러 번 deadline 확인(action 제출 모델) 해도 연장 안 됨
        for t in (1100.0, 1500.0, 1800.0):
            clock.set(t)
            s.check_deadlines()
        clock.set(1000.0 + STAGE7_WORKLOAD_TIMEOUT_S + 1.0)
        with self.assertRaises(SessionExpired):
            s.check_deadlines()


class BringupCloseDeadlineTest(unittest.TestCase):
    def test_bringup_boundary(self):
        clock = FakeClock(0.0)
        s = ResidentSession(clock=clock)
        s.start()
        clock.set(STAGE7_BRINGUP_TIMEOUT_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.complete_bringup()
        self.assertEqual(ctx.exception.deadline, Deadline.BRINGUP)

    def test_close_boundary(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.begin_close()
        clock.set(1000.0 + STAGE7_CLOSE_TIMEOUT_S + 0.001)
        with self.assertRaises(SessionExpired) as ctx:
            s.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.CLOSE)

    def test_close_returns_lease(self):
        clock = FakeClock(1000.0)
        s = _ready_session(clock)
        s.lease.acquire("w-1")
        s.lease.release("w-1")
        s.begin_close()
        s.complete_close()
        self.assertEqual(s.state, SessionState.CLOSED)
        s.lease.assert_returned()


class MultiWorkloadResidentTest(unittest.TestCase):
    def test_many_workloads_within_absolute(self):
        clock = FakeClock(0.0)
        s = ResidentSession(clock=clock)
        s.start()
        clock.set(60.0)
        s.complete_bringup()
        t = 60.0
        for _ in range(5):
            t += 10.0
            clock.set(t)
            s.begin_workload()
            t += 100.0
            clock.set(t)
            s.end_workload()
        self.assertEqual(s.workloads_run, 5)
        self.assertEqual(s.state, SessionState.READY)


if __name__ == "__main__":
    unittest.main()
