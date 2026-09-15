"""RF-off regressions for acquire interruptions and expired-session teardown."""

import sys

import unittest

from vdeter.stage8.execution.lease import LeaseError, MassBLease, MassBRole
from vdeter.stage8.execution.runner import WorkloadRunner, WorkloadSpec
from vdeter.stage8.execution.session import Deadline, ResidentSession, SessionExpired, SessionState
from vdeter.stage8.execution.snapshot import Snapshot
from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict
from vdeter.stage8.testing.client_runner import RecordingCallbacks
from vdeter.stage8.testing.execution_fakes import FakeTxExecutor, LoopbackConnection
from vdeter.stage8.testing.fakes import FakeClock, build_manifest


def make_runner(lease):
    manifest = build_manifest()
    connection = LoopbackConnection(
        callbacks=RecordingCallbacks(), manifest=manifest, clock=FakeClock())
    snapshot = Snapshot("/s", "/s/p.py", "/s/p.poc.toml", "ab" * 32,
                        "cd" * 32, "ef" * 32, manifest, "py", "3.12", 1, 2)
    return WorkloadRunner(connection, lease, run_id="r", workload_id="mine",
                          scenario_id="SC-VDE-006", snapshot=snapshot,
                          tx_executor=FakeTxExecutor())


def expired_session(deadline):
    clock = FakeClock(0.0)
    session = ResidentSession(clock)
    session.start()
    if deadline != Deadline.BRINGUP:
        session.complete_bringup()
    if deadline == Deadline.WORKLOAD:
        session.begin_workload()
    clock.set({Deadline.BRINGUP: 2701, Deadline.READY_IDLE: 2701,
               Deadline.WORKLOAD: 901, Deadline.SESSION_ABSOLUTE: 7201}[deadline])
    with unittest.TestCase().assertRaises(SessionExpired) as caught:
        session.check_deadlines()
    assert caught.exception.deadline == deadline
    return session, clock


class Stage4FiveFixRegressionTest(unittest.TestCase):
    def test_acquire_interrupt_after_ownership_returns_own_lease(self):
        for interrupt_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(interrupt_type=interrupt_type.__name__):
                interrupt = interrupt_type("original acquire interruption")

                class InterruptedLease(MassBLease):
                    def acquire(self, workload_id):
                        super().acquire(workload_id)
                        raise interrupt

                lease = InterruptedLease()
                runner = make_runner(lease)
                with unittest.TestCase().assertRaises(interrupt_type) as caught:
                    runner.run(WorkloadSpec())
                assert caught.exception is interrupt
                lease.assert_returned()
                result = runner.finish(ScenarioVerdict.PASS)
                assert result.poc_infrastructure == PocInfrastructure.ERROR
                assert "LEASE_RELEASED" in result.lifecycle_transitions


    def test_acquire_interrupt_between_field_assignments_is_rolled_back(self):
        for partial_state in ("holder_only", "role_and_holder"):
            with self.subTest(partial_state=partial_state):
                lease = MassBLease()
                interrupt = KeyboardInterrupt("inside acquire")
                fired = False

                def trace(frame, event, arg):
                    nonlocal fired
                    if frame.f_code is MassBLease.acquire.__code__ and event == "line":
                        partial = (lease.holder == "mine" if partial_state == "holder_only"
                                   else lease.role == MassBRole.POC_ACTOR)
                        if partial and not fired:
                            fired = True
                            raise interrupt
                    return trace

                previous = sys.gettrace()
                try:
                    sys.settrace(trace)
                    with unittest.TestCase().assertRaises(KeyboardInterrupt) as caught:
                        lease.acquire("mine")
                finally:
                    sys.settrace(previous)
                assert fired
                assert caught.exception is interrupt
                lease.assert_returned()


    def test_failed_acquire_never_releases_another_workload(self):
        lease = MassBLease()
        lease.acquire("other")
        runner = make_runner(lease)
        result = runner.run(WorkloadSpec())
        assert result.poc_infrastructure == PocInfrastructure.ERROR
        assert lease.role == MassBRole.POC_ACTOR
        assert lease.holder == "other"
        assert lease.history == [("ACQUIRE", "other")]


    def test_interrupt_at_runner_acquire_return_boundary_preserves_identity(self):
        lease = MassBLease()
        runner = make_runner(lease)
        interrupt = KeyboardInterrupt("between acquire return and flag assignment")

        def trace(frame, event, arg):
            if (frame.f_code is WorkloadRunner._arm_once.__code__ and event == "line"
                    and lease.holder == "mine" and not runner._lease_acquired):
                raise interrupt
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            with unittest.TestCase().assertRaises(KeyboardInterrupt) as caught:
                runner.run(WorkloadSpec())
        finally:
            sys.settrace(previous)
        assert caught.exception is interrupt
        lease.assert_returned()


    def test_original_acquire_interrupt_survives_release_interrupt(self):
        original = KeyboardInterrupt("original")

        class InterruptedLease(MassBLease):
            def acquire(self, workload_id):
                super().acquire(workload_id)
                raise original

            def release(self, workload_id=None):
                raise SystemExit("secondary")

        lease = InterruptedLease()
        runner = make_runner(lease)
        with unittest.TestCase().assertRaises(KeyboardInterrupt) as caught:
            runner.run(WorkloadSpec())
        assert caught.exception is original
        lease.assert_returned()
        assert "LEASE_FORCE_RELEASED" in runner.finish(ScenarioVerdict.PASS).lifecycle_transitions


    def test_expired_session_close_deadline_remains_enforced(self):
        for deadline in (Deadline.BRINGUP, Deadline.READY_IDLE, Deadline.WORKLOAD):
            for check in ("check_deadlines", "complete_close"):
                with self.subTest(deadline=deadline, check=check):
                    session, clock = expired_session(deadline)
                    session.lease.acquire("mine")
                    session.begin_close()
                    close_start = clock.monotonic()
                    assert session.closing_started_at == close_start
                    assert session.state == SessionState.EXPIRED
                    clock.set(close_start + 600)
                    session.begin_close()
                    assert session.closing_started_at == close_start
                    session.check_deadlines()
                    clock.set(close_start + 600.001)
                    with unittest.TestCase().assertRaises(SessionExpired) as caught:
                        getattr(session, check)()
                    assert caught.exception.deadline == Deadline.CLOSE
                    assert session.state == SessionState.EXPIRED
                    assert session.expired_deadline == deadline
                    assert session.evidence()["expired_deadline"] == deadline.value
                    if check == "check_deadlines":
                        with unittest.TestCase().assertRaises(SessionExpired):
                            session.complete_close()
                    session.lease.assert_returned()


    def test_absolute_expiry_is_not_renewed_by_begin_close(self):
        session, clock = expired_session(Deadline.SESSION_ABSOLUTE)
        session.lease.acquire("mine")
        session.begin_close()
        close_start = clock.monotonic()
        with unittest.TestCase().assertRaises(SessionExpired) as caught:
            session.complete_close()
        assert caught.exception.deadline == Deadline.SESSION_ABSOLUTE
        assert session.started_at == 0
        session.lease.assert_returned()
        clock.set(close_start + 601)
        with unittest.TestCase().assertRaises(SessionExpired) as caught:
            session.check_deadlines()
        assert caught.exception.deadline == Deadline.CLOSE
        assert session.expired_deadline == Deadline.SESSION_ABSOLUTE


    def test_expired_session_successful_cleanup_preserves_original_expiry(self):
        for deadline in (Deadline.BRINGUP, Deadline.READY_IDLE, Deadline.WORKLOAD):
            with self.subTest(deadline=deadline):
                session, clock = expired_session(deadline)
                session.lease.acquire("mine")
                session.begin_close()
                close_start = session.closing_started_at
                clock.set(close_start + 600)
                session.complete_close()
                session.begin_close()
                assert session.closing_started_at == close_start
                assert session.state == SessionState.EXPIRED
                assert session.expired_deadline == deadline
                assert session.remaining_session_absolute() == 7200 - clock.monotonic()
                session.lease.assert_returned()


    def test_inconsistent_preexisting_holder_cannot_be_overwritten(self):
        lease = MassBLease(holder="other")
        with unittest.TestCase().assertRaises(LeaseError):
            lease.acquire("mine")
        assert lease.holder == "other"


if __name__ == "__main__":
    unittest.main()
