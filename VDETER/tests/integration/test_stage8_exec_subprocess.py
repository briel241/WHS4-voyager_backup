"""Stage 4 실제 subprocess 통합 시험(실행 명세 §5.1, §5.3, §5.4, §6.4, §15.3).

Stage 3 `PocSupervisor`(실 socket·실 process)로 Stage 4 child launcher(`execution.child_main`)를
띄워 **immutable snapshot의 Python entrypoint를 실제 child에서 로드·실행**한다. loopback이
아니라 실제 process/socket 경로를 검증한다.

검증: entrypoint class 로딩·실행, 실 socket 위 reactive action, workload마다 새 process,
snapshot A 실행 중 원본 수정 격리, callback 예외(process 오류) fail-closed, 모든 종료 경로
cleanup 잔존 0. Linux(WSL) 전용.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

_LINUX = (hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET")
          and hasattr(os, "getuid") and sys.platform != "win32")

if _LINUX:
    from vdeter.stage8._supervisor.child import SubprocessChildSpawner
    from vdeter.stage8._supervisor.supervisor import PocSupervisor
    from vdeter.stage8.execution.child_main import (
        ENV_ENTRYPOINT,
        ENV_HELLO,
        ENV_SNAPSHOT_SCRIPT,
    )
    from vdeter.stage8.execution.snapshot import create_snapshot
    from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
    from vdeter.stage8.poc_api.types import ProviderSubmission, TARGET_TO_PHYSICAL_CHANNEL
    from vdeter.stage8.testing.execution_fakes import write_poc_package
    from vdeter.stage8.testing.fakes import make_observation


def _script(version):
    return (
        "class Poc:\n"
        f"    VERSION = {version!r}\n"
        "    def on_start(self, context):\n"
        "        return []\n"
        "    def on_event(self, event, context):\n"
        "        if event.type == 'PACKET_OBSERVED':\n"
        "            return [context.replay(target='ROC',\n"
        "                                   source_observation_id=event.body.get('observation_id'),\n"
        "                                   payload={'marker': self.VERSION})]\n"
        "        return []\n"
        "    def on_finish(self, context):\n"
        "        return None\n"
    ).encode("utf-8")


_SCRIPT_CRASH = (
    "class Poc:\n"
    "    def on_start(self, context):\n"
    "        return []\n"
    "    def on_event(self, event, context):\n"
    "        raise RuntimeError('poc crash on event')\n"
    "    def on_finish(self, context):\n"
    "        return None\n"
).encode("utf-8")


class _RecordingProvider:
    """server 측 provider. 제출된 descriptor(payload marker 포함)를 기록한다."""

    def __init__(self):
        self.descriptors = []
        self.request_ids = []

    def derive_physical_channel(self, target):
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action, *, request_id):
        self.descriptors.append(action)
        self.request_ids.append(request_id)
        ch = self.derive_physical_channel(action.target)
        return ProviderSubmission(ch, f"p-{request_id[:6]}", f"t-{request_id[:6]}")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class SubprocessExecutionTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-subp-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-subp-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-subp-sock-")
        self.addCleanup(lambda: shutil.rmtree(self.poc, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.snap_root, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.sock_root, ignore_errors=True))

    def _supervisor(self, snap, provider, workload_id):
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.stage8.execution.child_main"],
            extra_env={
                ENV_SNAPSHOT_SCRIPT: snap.script_path,
                ENV_ENTRYPOINT: snap.descriptor.entrypoint,
                ENV_HELLO: json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1",
                "VDETER_POC_WORKLOAD": workload_id,
                "VDETER_POC_RUN_ID": "r1",
                "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src"
                ) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
        )
        return PocSupervisor(
            session_id="s1", workload_id=workload_id, run_id="r1", scenario_id="SC-VDE-006",
            manifest=snap.descriptor, server_config=ServerCapabilityConfig(),
            provider=provider, spawner=spawner,
            socket_root=os.path.join(self.sock_root, workload_id),
        )

    def _make_snapshot(self, base, version, dest):
        write_poc_package(self.poc, base, poc_id=base.replace("_", "-"), script=_script(version))
        return create_snapshot(self.poc, base, dest_dir=os.path.join(self.snap_root, dest),
                               scenario_id="SC-VDE-006")

    def test_real_child_loads_entrypoint_and_runs_action(self):
        snap = self._make_snapshot("replay_a", "A", "w-a")
        provider = _RecordingProvider()
        sup = self._supervisor(snap, provider, "w-a")
        sup.start()
        self.assertNotEqual(sup.expected_pid, os.getpid())  # 실제 별도 process
        # ARMED 도달 = child가 snapshot entrypoint를 로드하고 on_start를 실행했다.
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=15.0),
                        "child가 ARMED에 도달해야 함(entrypoint 로드·on_start 실행)")
        sup.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup.run_until(lambda: len(provider.descriptors) >= 1, timeout=15.0),
                        "child on_event가 replay action을 제출해야 함")
        # 실제 child에서 실행된 snapshot의 code가 payload marker를 남긴다.
        self.assertEqual(provider.descriptors[0].payload.get("marker"), "A")
        self.assertGreaterEqual(sup.engine.evidence["provider_submit_calls"], 1)
        # accepted action의 terminal을 확정한다(runtime이 RF 결과를 반환하는 자리, RF-off fake).
        sup.provide_action_terminal(provider.request_ids[0], action_execution="TX_COMPLETED")
        report = sup.close_workload()
        res = report["residual"]
        self.assertFalse(res["child_alive"])
        self.assertIsNotNone(res["child_exit_code"])
        self.assertFalse(res["socket_file_exists"])
        self.assertFalse(res["workload_dir_exists"])
        # 지속 자원(process/socket/workload_dir/lease)과 queue/inflight는 0이어야 한다.
        self.assertEqual(res["engine"]["event_queue"], 0)
        self.assertEqual(res["engine"]["inflight_actions"], 0)
        # replay_ring은 종료 시 child가 아직 ACK하지 않은 delivered event를 담을 수 있고
        # (실 child ACK 타이밍), engine 객체 폐기와 함께 사라지는 유계 버퍼다(§8.2, §10.2).
        self.assertLessEqual(res["engine"]["replay_ring"], 1)

    def test_per_workload_new_process(self):
        snap = self._make_snapshot("replay_p", "A", "w1")
        p1, p2 = _RecordingProvider(), _RecordingProvider()
        sup1 = self._supervisor(snap, p1, "w1")
        sup1.start()
        self.assertTrue(sup1.run_until(lambda: sup1.engine.state.value == "RUNNING", timeout=15.0))
        pid1 = sup1.expected_pid
        sup1.close_workload()
        sup2 = self._supervisor(snap, p2, "w2")
        sup2.start()
        self.assertTrue(sup2.run_until(lambda: sup2.engine.state.value == "RUNNING", timeout=15.0))
        pid2 = sup2.expected_pid
        sup2.close_workload()
        self.assertNotEqual(pid1, pid2)  # workload마다 새 process(§5.4)
        self.assertFalse(sup1.residuals()["child_alive"])
        self.assertFalse(sup2.residuals()["child_alive"])

    def test_snapshot_isolation_across_original_edit(self):
        # snapshot A를 child A가 실행한 뒤 원본을 수정 → child B는 새 snapshot(B)을 실행.
        snap_a = self._make_snapshot("iso", "A", "w-a")
        pa = _RecordingProvider()
        sup_a = self._supervisor(snap_a, pa, "w-a")
        sup_a.start()
        self.assertTrue(sup_a.run_until(lambda: sup_a.engine.state.value == "RUNNING", timeout=15.0))
        sup_a.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup_a.run_until(lambda: len(pa.descriptors) >= 1, timeout=15.0))
        sup_a.close_workload()
        self.assertEqual(pa.descriptors[0].payload.get("marker"), "A")

        # 원본 수정 후 다음 workload의 새 snapshot(B).
        os.chmod(os.path.join(self.poc, "iso.py"), 0o600)
        with open(os.path.join(self.poc, "iso.py"), "wb") as fh:
            fh.write(_script("A2"))
        snap_b = create_snapshot(self.poc, "iso", dest_dir=os.path.join(self.snap_root, "w-b"),
                                 scenario_id="SC-VDE-006")
        self.assertNotEqual(snap_a.script_sha256, snap_b.script_sha256)
        pb = _RecordingProvider()
        sup_b = self._supervisor(snap_b, pb, "w-b")
        sup_b.start()
        self.assertTrue(sup_b.run_until(lambda: sup_b.engine.state.value == "RUNNING", timeout=15.0))
        sup_b.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup_b.run_until(lambda: len(pb.descriptors) >= 1, timeout=15.0))
        sup_b.close_workload()
        self.assertEqual(pb.descriptors[0].payload.get("marker"), "A2")  # child B는 B code 실행

    def test_workload_deadline_drives_close_of_real_child(self):
        # session workload-deadline 만료가 살아 있는 실 child의 close→종료→cleanup을 구동함을
        # 보인다(enforcement mechanism, 짧은 cap 주입). 900초 literal과 비협조 child의
        # SIGKILL-at-900s는 Stage 6 hard gate로 이관(spec §5.2: 900초는 기존 Stage 7 세션 상한).
        from vdeter.stage8.execution.limits import SessionPolicy, Stage7Timeouts
        from vdeter.stage8.execution.session import Deadline, ResidentSession, SessionExpired
        from vdeter.stage8.testing.fakes import FakeClock

        snap = self._make_snapshot("idle_poc", "A", "w-d")  # on_event []: child는 ARMED 후 idle 유지
        provider = _RecordingProvider()
        sup = self._supervisor(snap, provider, "w-d")
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock,
                                  policy=SessionPolicy(stage7=Stage7Timeouts(workload_s=1.0)))
        session.start()
        clock.set(0.5)
        session.complete_bringup()
        sup.start()
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=15.0))
        session.begin_workload()
        # child는 살아 idle 상태(연결 유지). deadline이 아니라 child 스스로 끝난 게 아님을 확인.
        self.assertTrue(sup.residuals()["child_alive"])
        # session workload cap(1초) 초과 → WORKLOAD deadline 만료.
        clock.set(2.0)
        with self.assertRaises(SessionExpired) as ctx:
            session.check_deadlines()
        self.assertEqual(ctx.exception.deadline, Deadline.WORKLOAD)
        # deadline 만료 → 실 child를 close(CLOSE→SIGTERM→SIGKILL). 협조 child라 voluntary 종료.
        report = sup.close_workload()
        res = report["residual"]
        self.assertFalse(res["child_alive"])       # deadline-driven close가 실 child를 종료
        self.assertFalse(res["socket_file_exists"])
        self.assertFalse(res["workload_dir_exists"])

    def test_interrupt_paths_leave_no_real_residual(self):
        """실 child/socket이 살아 있는 중 인터럽트 → 재전달·lease 반환·잔존 0(§13, REQ-19/20/27).

        loopback matrix(test_stage8_exec_runner)가 결과 분류를 덮고, 이 시험은 **실제**
        process/socket/workload-dir 잔존이 0인지 확인한다.
        """
        from vdeter.stage8.execution.lease import MassBLease
        from vdeter.stage8.execution.runner import (
            WorkloadRunner,
            WorkloadSpec,
            observation_step,
        )
        from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict
        from vdeter.stage8.testing.execution_fakes import FakeTxExecutor

        class _SupConn:
            """실 PocSupervisor 위의 WorkloadConnection(시험 전용) + 인터럽트 주입."""

            def __init__(self, sup, at, exc):
                self._sup = sup
                self._at = at
                self._exc = exc
                self._armed = False
                self.close_calls = 0

            def _maybe(self, point):
                if self._at == point:
                    raise self._exc

            def start(self):
                self._sup.start()
                self._sup.run_until(lambda: self._sup.engine.state.value == "RUNNING",
                                    timeout=15.0)
                self._armed = self._sup.engine.state.value == "RUNNING"
                self._maybe("after_start")  # child가 살아 있는 상태에서 인터럽트

            @property
            def armed(self):
                return self._armed

            @property
            def running(self):
                return self._armed

            def emit_event(self, event_type, body):
                self._maybe("emit")
                self._sup.emit_event(event_type, body)

            def emit_observation(self, record):
                self._maybe("emit")
                self._sup.emit_observation(record)

            def pump(self):
                self._sup.pump_once(timeout=0.05)

            def poll_new_results(self):
                return []

            def submitted_action(self, request_id):
                return None

            def provide_terminal(self, request_id, *, action_execution, target_effect):
                pass

            def client_idle(self):
                return True

            def finish(self):
                self._maybe("finish")

            def close(self):
                self.close_calls += 1
                report = self._sup.close_workload()
                self._maybe("close_after")  # teardown 뒤 인터럽트
                return report

            @property
            def infrastructure_error(self):
                return self._sup.engine.fatal_error if self._sup.engine else None

            def armed_reached(self):
                return self._armed

            def evidence(self):
                return {}

        cases = [("after_start", KeyboardInterrupt), ("emit", KeyboardInterrupt),
                 ("emit", SystemExit), ("finish", GeneratorExit),
                 ("close_after", KeyboardInterrupt)]
        for at, exc_type in cases:
            with self.subTest(stage=at, interrupt=exc_type.__name__):
                snap = self._make_snapshot("intr_%s_%s" % (at, exc_type.__name__.lower()),
                                           "A", "w-%s-%s" % (at, exc_type.__name__.lower()))
                sup = self._supervisor(snap, _RecordingProvider(),
                                       "w-%s-%s" % (at, exc_type.__name__.lower()))
                conn = _SupConn(sup, at, exc_type())
                lease = MassBLease()
                runner = WorkloadRunner(conn, lease, run_id="r1", workload_id="w1",
                                        scenario_id="SC-VDE-006", snapshot=snap,
                                        tx_executor=FakeTxExecutor())
                obs = make_observation(source_station="ROC", destination_station="MASS-A")
                spec = WorkloadSpec(steps=(observation_step(obs),),
                                    scenario_verdict=ScenarioVerdict.PASS)
                with self.assertRaises(exc_type):
                    runner.run(spec)
                # 실 자원 잔존 0
                res = sup.residuals()
                self.assertFalse(res["child_alive"], "child가 살아남음")
                self.assertFalse(res["socket_file_exists"], "socket 파일 잔존")
                self.assertFalse(res["workload_dir_exists"], "workload dir 잔존")
                lease.assert_returned()
                self.assertEqual(conn.close_calls, 1)
                result = runner._finalize(ScenarioVerdict.PASS, raised=None)
                self.assertIsNotNone(result)
                self.assertNotEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)

    def test_child_callback_exception_fail_closed(self):
        # snapshot on_event가 예외 → 실제 child SDK가 abort → server infra 오류 + cleanup.
        write_poc_package(self.poc, "crashy", poc_id="crashy", script=_SCRIPT_CRASH)
        snap = create_snapshot(self.poc, "crashy", dest_dir=os.path.join(self.snap_root, "w-c"),
                               scenario_id="SC-VDE-006")
        provider = _RecordingProvider()
        sup = self._supervisor(snap, provider, "w-c")
        sup.start()
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=15.0))
        sup.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        ok = sup.run_until(
            lambda: sup.engine.fatal_error is not None
            and sup.engine.fatal_error.code.value == "POC_CALLBACK_EXCEPTION",
            timeout=15.0,
        )
        self.assertTrue(ok, "child callback 예외가 POC_CALLBACK_EXCEPTION으로 fail-closed되어야 함")
        report = sup.close_workload()
        self.assertFalse(report["residual"]["child_alive"])
        self.assertFalse(report["residual"]["socket_file_exists"])


if __name__ == "__main__":
    unittest.main()
