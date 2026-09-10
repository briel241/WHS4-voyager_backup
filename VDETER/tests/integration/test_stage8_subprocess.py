"""Stage 8 PoC supervisor 실제 subprocess child 통합 시험(SO_PEERCRED·stdout capture·TERM/KILL).

명세 §4.3, §13, §15.2; REQ-04, REQ-21, REQ-25, REQ-26. Linux 전용(WSL). 별도 process로
실제 SO_PEERCRED PID 결속·bounded stdout capture·강제 종료 escalation을 검증한다.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest

_LINUX = hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET") and hasattr(os, "getuid")

_HARNESS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "support", "poc_child_harness.py")
)

if _LINUX:
    from vdeter.stage8._supervisor.child import SubprocessChildSpawner
    from vdeter.stage8._supervisor.supervisor import PocSupervisor
    from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
    from vdeter.stage8.testing import FakeActionSubmitProvider, build_manifest
    from vdeter.stage8.testing.fakes import make_observation


def _spawner(mode):
    return SubprocessChildSpawner(
        [sys.executable, _HARNESS],
        extra_env={
            "VDETER_POC_MODE": mode,
            "VDETER_POC_SESSION": "s1",
            "VDETER_POC_WORKLOAD": "w1",
            "VDETER_POC_RUN_ID": "r1",
            "VDETER_POC_SCENARIO": "SC-VDE-006",
        },
    )


@unittest.skipUnless(_LINUX, "Linux SOCK_SEQPACKET 전용")
class SupervisorSubprocessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vdeter-poc-subp-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _make(self, mode, provider=None):
        return PocSupervisor(
            session_id="s1", workload_id="w1", run_id="r1", scenario_id="SC-VDE-006",
            manifest=build_manifest(), server_config=ServerCapabilityConfig(),
            provider=provider or FakeActionSubmitProvider(), spawner=_spawner(mode),
            socket_root=os.path.join(self.tmp, "root"),
        )

    def test_S8_POC_API_REQ_04_real_subprocess_peercred_and_cleanup(self):
        provider = FakeActionSubmitProvider()
        sup = self._make("action", provider=provider)
        sup.start()
        self.assertNotEqual(sup.expected_pid, os.getpid())  # 실제 별도 process
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=12.0))
        sup.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup.run_until(lambda: provider.submit_calls >= 1, timeout=12.0))
        rid = provider.submitted_request_ids[0]
        sup.provide_action_terminal(rid, action_execution="TX_COMPLETED")
        report = sup.close_workload()
        res = report["residual"]
        self.assertFalse(res["child_alive"])
        self.assertIsNotNone(res["child_exit_code"])
        self.assertFalse(res["socket_file_exists"])
        self.assertFalse(res["workload_dir_exists"])

    def test_S8_POC_API_REQ_21_stdout_output_limit_aborts(self):
        sup = self._make("spam_stdout")
        sup.start()
        ok = sup.run_until(
            lambda: sup.engine.fatal_error is not None
            and sup.engine.fatal_error.code.value == "POC_OUTPUT_LIMIT_EXCEEDED",
            timeout=12.0,
        )
        self.assertTrue(ok, "stdout 누적 상한 초과가 abort로 이어져야 함")
        self.assertTrue(sup.capture.overflowed)
        report = sup.close_workload()
        self.assertFalse(report["residual"]["child_alive"])

    def test_S8_POC_API_REQ_25_sigterm_then_sigkill_escalation(self):
        sup = self._make("ignore_sigterm")
        sup.start()
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=12.0))
        report = sup.close_workload()
        self.assertTrue(report["forced_kill"], "SIGTERM 무시 시 SIGKILL로 escalate해야 함")
        self.assertFalse(report["residual"]["child_alive"])
        self.assertFalse(report["residual"]["socket_file_exists"])


if __name__ == "__main__":
    unittest.main()
