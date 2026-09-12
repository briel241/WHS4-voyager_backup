"""Stage 8 통합 — R12/R13 실 child/socket regression(RF-off, Linux).

Codex DEADLINE-REVIEW.md(기준 0f47ecb) R12·R13 재현을 현재 작업본에서 차단하는지 실 subprocess child·
실 Unix SOCK_SEQPACKET·실 SupervisorConnection·실 wire codec으로 검증한다.

- R12: on_finish 예산(§11, CALLBACK_ON_FINISH_DEADLINE_S=2.0초)을 제어 loop(engine FINISHING deadline)이
       직접 강제. 초과(2.3초)→POC_CALLBACK_TIMEOUT·ABORTED, 예산 내(0.5초)→COMPLETED. 잔존 0.
- R13: timeout ERROR가 실 wire를 건넌다(detail 정수 ms, float 0). on_event hang→ACK grace timeout ERROR가
       실제 socket으로 송신되고(CodecError 없음) child가 그것을 받아 종료. 잔존 0.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

_LINUX = (hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET")
          and hasattr(os, "getuid") and sys.platform != "win32")
_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))

if _LINUX:
    from vdeter.stage8._supervisor.child import SubprocessChildSpawner
    from vdeter.stage8._supervisor.clock import RealMonotonicClock
    from vdeter.stage8._supervisor.engine import SERVER_EVENT_ACK_GRACE_S
    from vdeter.stage8.execution.lease import MassBLease
    from vdeter.stage8.execution.runner import WorkloadRunner
    from vdeter.stage8.execution.snapshot import create_snapshot
    from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict
    from vdeter.stage8.integration.rf_off import assemble_rf_off_provider
    from vdeter.stage8.integration.supervisor_connection import SupervisorConnection
    from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
    from vdeter.stage8.poc_api.codec import decode_wire_packet
    from vdeter.stage8.poc_api.limits import CALLBACK_ON_FINISH_DEADLINE_S
    from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
    from vdeter.stage8.testing.execution_fakes import write_poc_package


def _poc(on_event_body="        return []", on_finish_body="        return None"):
    return (
        "import time\n"
        "class Poc:\n"
        "    def on_start(self, context):\n        return []\n"
        "    def on_event(self, event, context):\n" + on_event_body + "\n"
        "    def on_finish(self, context):\n" + on_finish_body + "\n"
    ).encode("utf-8")


_SLOW_FINISH = _poc(on_finish_body="        time.sleep(2.3)\n        return None")      # 예산 초과
_FAST_FINISH = _poc(on_finish_body="        time.sleep(0.5)\n        return None")      # 예산 내
_HANG_EVENT = _poc(on_event_body="        time.sleep(1.0)\n        return []")           # ACK grace 초과


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class _Base(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-dl-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-dl-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-dl-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))
        self._conns = []
        self.addCleanup(self._recover_all)

    def _recover_all(self):
        for conn in self._conns:
            try:
                if conn.residuals()["child_alive"]:
                    conn._terminate_child()
                conn._drain_child_output()
                conn._teardown_socket()
            except BaseException:  # noqa: BLE001
                pass

    def _connection(self, workload_id, script, base):
        write_poc_package(self.poc, base, poc_id=base, script=script)
        snap = create_snapshot(self.poc, base, dest_dir=os.path.join(self.snap_root, workload_id),
                               scenario_id="SC-VDE-006")
        provider, seams = assemble_rf_off_provider()
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.stage8.execution.child_main"],
            extra_env={
                "VDETER_POC_SNAPSHOT_SCRIPT": snap.script_path,
                "VDETER_POC_ENTRYPOINT": snap.descriptor.entrypoint,
                "VDETER_POC_HELLO": json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": workload_id,
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
            })
        conn = SupervisorConnection(
            provider=provider, spawner=spawner, session_id="s1", workload_id=workload_id,
            run_id="r1", scenario_id="SC-VDE-006", manifest=snap.descriptor,
            server_config=ServerCapabilityConfig(), clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, workload_id))
        self._conns.append(conn)
        return conn, provider, snap

    def _runner(self, conn, provider, snap, workload_id):
        return WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id=workload_id,
                              scenario_id="SC-VDE-006", snapshot=snap,
                              tx_executor=ProductionTxExecutor(provider))

    def _assert_no_residue(self, conn):
        res = conn.residuals()
        self.assertFalse(res["child_alive"], res)
        self.assertFalse(res["socket_file_exists"], res)
        self.assertFalse(res["workload_dir_exists"], res)


class R12FinishDeadlineTest(_Base):
    def test_r12_on_finish_over_budget_is_callback_timeout_not_completed(self):
        conn, provider, snap = self._connection("w12a", _SLOW_FINISH, "slowfin")
        runner = self._runner(conn, provider, snap, "w12a")
        self.assertTrue(runner.arm() and conn.running)
        started = time.monotonic()
        result = runner.finish(ScenarioVerdict.PASS)
        elapsed = time.monotonic() - started
        # 2.3초 on_finish는 예산(2.0초) 초과 → 성공으로 분류하지 않는다(POC_ABORT).
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        self.assertEqual(result.infrastructure_error_code, "POC_CALLBACK_TIMEOUT")
        err = conn.engine.fatal_error
        self.assertIsNotNone(err)
        self.assertEqual(err.detail, {"deadline_ms": int(round(CALLBACK_ON_FINISH_DEADLINE_S * 1000))})
        # 실패는 일반 pump timeout(5초)이 아니라 예산 시점에 확정된다(정리 포함해도 5초 미만).
        self.assertLess(elapsed, 5.0)
        self._assert_no_residue(conn)  # callback이 늦게 돌아와도 종료 escalation·정리 완주

    def test_r12_on_finish_within_budget_completes(self):
        conn, provider, snap = self._connection("w12b", _FAST_FINISH, "fastfin")
        runner = self._runner(conn, provider, snap, "w12b")
        self.assertTrue(runner.arm() and conn.running)
        result = runner.finish(ScenarioVerdict.PASS)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertIsNone(result.infrastructure_error_code)
        self._assert_no_residue(conn)


class R13TimeoutErrorWireTest(_Base):
    def test_r13_ack_grace_timeout_error_crosses_real_wire_and_child_aborts(self):
        conn, provider, snap = self._connection("w13a", _HANG_EVENT, "hangev")
        conn.start()
        self.assertTrue(conn.running)
        sent_types = []
        original_send = conn._server.send_packet

        def send(packet):
            sent_types.append(decode_wire_packet(packet)["type"])
            return original_send(packet)
        conn._server.send_packet = send
        # on_event가 1.0초 hang → 서버 ACK grace(0.3초) 초과 → POC_CALLBACK_TIMEOUT ERROR.
        conn.emit_event("SCENARIO_STARTED", {})  # 예외(CodecError) 없이 돌아와야 한다
        deadline = time.monotonic() + 3.0
        while conn.engine.fatal_error is None and time.monotonic() < deadline:
            conn.pump()
        err = conn.engine.fatal_error
        self.assertIsNotNone(err)
        self.assertEqual(err.code.value, "POC_CALLBACK_TIMEOUT")
        self.assertEqual(err.detail, {"grace_ms": int(round(SERVER_EVENT_ACK_GRACE_S * 1000))})
        self.assertIn("ERROR", sent_types, sent_types)          # 구조화된 실패가 실 wire를 건넜다
        self.assertNotIn("detail_encoding_error", json.dumps(err.detail))
        # child는 hang이 끝난 뒤 ERROR를 읽고 스스로 종료한다(§14 POC_ABORT).
        exit_deadline = time.monotonic() + 3.0
        while conn.residuals()["child_alive"] and time.monotonic() < exit_deadline:
            time.sleep(0.05)
        report = conn.close()
        self.assertFalse(report["child_alive"])
        self._assert_no_residue(conn)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class P11WorkloadDeadlineDuringExecutionTest(_Base):
    """P11: workload **진행 중** 권위 deadline 만료가 감지되어 살아 있는 child를 종료한다.

    이전에는 `ResidentPocRuntime.enforce_deadlines` 호출자가 시험뿐이었고, 세션 loop은 workload
    handler를 동기 호출해 실행 중에는 만료를 볼 수 없었다. 이제 각 scenario step 전에 권위 deadline을
    확인하는 guard가 돌고, 만료 시 남은 step 없이(신규 action 0) 정리하고 child를 종료한다.
    중복 timer를 만들지 않고 기존 `ResidentSession` 모델만 조회한다. fake clock으로 경계를 시험한다.
    """

    def test_workload_deadline_expiry_during_steps_terminates_child(self):
        from vdeter.stage8.execution.limits import SessionPolicy, Stage7Timeouts
        from vdeter.stage8.execution.session import ResidentSession, SessionExpired
        from vdeter.stage8.execution.runner import WorkloadRunner, scenario_started
        from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict
        from vdeter.stage8.integration.entry import PocWorkload
        from vdeter.stage8.integration.runtime import ResidentPocRuntime
        from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
        from vdeter.stage8.testing.fakes import FakeClock

        conn, provider, snap = self._connection("w11", _FAST_FINISH, "p11poc")
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id="w11",
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(provider))
        # workload 상한 1초인 권위 정책을 주입하고 fake clock으로 만료를 만든다(상한 완화 아님).
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock,
                                  policy=SessionPolicy(stage7=Stage7Timeouts(workload_s=1.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        self.assertTrue(runner.arm() and conn.running)
        runtime.begin_workload()
        # step이 여러 개인 workload. 첫 step 뒤 시계를 상한 너머로 밀어 실행 중 만료를 만든다.
        workload = PocWorkload(runner, conn, provider, None,
                               [scenario_started(), scenario_started(), scenario_started()])
        calls = {"n": 0}

        def guard():
            calls["n"] += 1
            if calls["n"] == 2:
                clock.set(2.0)          # workload 상한(1초) 초과 시점으로 전진
            session.check_deadlines()   # 권위 모델 조회(중복 timer 없음)

        with self.assertRaises(SessionExpired):
            workload.run(ScenarioVerdict.PASS, guard=guard)
        # 만료를 성공으로 덮지 않는다: 결과는 ABORTED이고 남은 step은 전달되지 않았다.
        self.assertEqual(runner.finish(ScenarioVerdict.PASS).poc_infrastructure,
                         PocInfrastructure.ABORTED)
        self.assertLess(calls["n"], 3, "만료 뒤 남은 step을 전달하면 안 된다")
        # 살아 있던 child·socket·workload dir이 정리됐다(잔존 0).
        self._assert_no_residue(conn)
