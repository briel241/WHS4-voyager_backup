"""Persistent service session의 fail-closed 판정과 child 회수 계약."""

import inspect
import subprocess

from apps import stage7_coordinator_main as coordinator_main


class _FakeProcess:
    def __init__(self, *, terminate_exits=True, kill_exits=True):
        self.returncode = None
        self.terminate_exits = terminate_exits
        self.kill_exits = kill_exits
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        if self.terminate_calls and not self.kill_calls:
            if not self.terminate_exits:
                raise subprocess.TimeoutExpired("fake-child", timeout)
            self.returncode = -15
        elif self.kill_calls:
            if not self.kill_exits:
                raise subprocess.TimeoutExpired("fake-child", timeout)
            self.returncode = -9
        return self.returncode


def test_empty_workload_set_is_never_pass():
    status, error = coordinator_main._service_session_terminal_status(
        [], service_stop_rc=0, endpoint_stop_completed=True)
    assert status == "FAIL"
    assert "하나도" in error


def test_all_workloads_and_stop_must_pass():
    workloads = [{"status": "PASS"}, {"status": "PASS"}]
    assert coordinator_main._service_session_terminal_status(
        workloads, service_stop_rc=0, endpoint_stop_completed=True) == (
            "PASS", None)


def test_workload_failure_is_terminal():
    status, error = coordinator_main._service_session_terminal_status(
        [{"status": "PASS"}, {"status": "FAIL"}],
        service_stop_rc=0,
        endpoint_stop_completed=True,
    )
    assert status == "FAIL"
    assert "workload" in error


def test_service_stop_failure_is_terminal():
    status, error = coordinator_main._service_session_terminal_status(
        [{"status": "PASS"}], service_stop_rc=7,
        endpoint_stop_completed=False)
    assert status == "FAIL"
    assert "rc=7" in error
    assert "STOP 미완료" in error


def test_bounded_terminate_reaps_cooperative_child():
    child = _FakeProcess()
    result = coordinator_main._terminate_process_bounded(
        child, terminate_timeout_s=0.1, kill_timeout_s=0.1)
    assert result == {
        "terminate_sent": True,
        "kill_sent": False,
        "exit_code": -15,
        "error": None,
    }
    assert child.terminate_calls == 1
    assert child.kill_calls == 0


def test_bounded_terminate_escalates_to_kill():
    child = _FakeProcess(terminate_exits=False)
    result = coordinator_main._terminate_process_bounded(
        child, terminate_timeout_s=0.1, kill_timeout_s=0.1)
    assert result["terminate_sent"] is True
    assert result["kill_sent"] is True
    assert result["exit_code"] == -9
    assert result["error"] is None
    assert child.terminate_calls == 1
    assert child.kill_calls == 1


def test_bounded_terminate_reports_unreaped_child():
    child = _FakeProcess(terminate_exits=False, kill_exits=False)
    result = coordinator_main._terminate_process_bounded(
        child, terminate_timeout_s=0.1, kill_timeout_s=0.1)
    assert result["terminate_sent"] is True
    assert result["kill_sent"] is True
    assert result["exit_code"] is None
    assert "kill 이후" in result["error"]


def test_coordinator_enforces_same_900_second_workload_bound_as_host():
    assert coordinator_main.SESSION_WORKLOAD_TIMEOUT_S == 900.0


def test_workload_loop_wires_timeout_shutdown_cleanup_and_terminal_judge():
    source = inspect.getsource(coordinator_main.run_mass_roc_session)
    for required in (
        "SESSION_WORKLOAD_TIMEOUT_S",
        "SESSION_SHUTDOWN",
        "_terminate_process_bounded(child)",
        "_service_session_terminal_status(",
        "endpoint_stop_completed",
    ):
        assert required in source
