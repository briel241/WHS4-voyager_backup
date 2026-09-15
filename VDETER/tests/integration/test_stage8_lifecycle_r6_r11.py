"""Stage 8 통합 — R6/R8/R10/R11 lifecycle negative/positive regression(RF-off, 실 child·socket, Linux).

Codex 읽기전용 검토(0f47ecb) REPORT.md R6, CONTINUATION-REPORT.md R8/R10/R11의 재현을 **현재 작업본**에서
차단하는지 실 subprocess child·실 Unix SOCK_SEQPACKET socket·실 SupervisorConnection으로 검증한다.
장비 경계(RF)만 software double. 순수 모델·loopback으로 대체하지 않는다.

- R6 : close 예산에 실 teardown 시간 포함(700초 주입 거부), close 예외에도 session cleanup·원형 예외 보존.
- R8 : FINISH/CLOSE 송신 실패·인터럽트에도 child 종료·socket teardown·provider cleanup 완주,
       RF 소유권 불확실(SESSION_INHIBIT) 유지(메모리 정리로 반환 완료 선언 금지), 재진입.
- R10: 같은 PID 재접속(epoch+1, event replay, provider 중복 호출 0), 복구 불가 시 ABORT+cleanup.
- R11: tee 유계(entry/byte), accepted correlation 보존, 변조/중복 덮어쓰기 금지, overflow 요약, cleanup 구분.
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
    from vdeter.stage8.execution.lease import MassBLease
    from vdeter.stage8.execution.limits import SessionPolicy, Stage7Timeouts
    from vdeter.stage8.execution.runner import WorkloadRunner, observation_step
    from vdeter.stage8.execution.session import Deadline, ResidentSession, SessionExpired
    from vdeter.stage8.execution.snapshot import create_snapshot
    from vdeter.stage8.execution.verdict import PocInfrastructure, ScenarioVerdict
    from vdeter.stage8.integration.rf_off import assemble_rf_off_provider, rf_off_terminal_source
    from vdeter.stage8.integration.runtime import ResidentPocRuntime
    from vdeter.stage8.integration.supervisor_connection import SupervisorConnection
    from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
    from vdeter.stage8.poc_api.messages import build_message
    from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
    from vdeter.stage8.poc_api.protocol import ACTION_RESULT, ACTION_SUBMIT
    from vdeter.stage8.testing.execution_fakes import write_poc_package
    from vdeter.stage8.testing.fakes import FakeClock, make_observation

_IDLE = (
    "class Poc:\n"
    "    def on_start(self, context):\n        return []\n"
    "    def on_event(self, event, context):\n        return []\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")

_REACTIVE = (
    "class Poc:\n"
    "    def on_start(self, context):\n        return []\n"
    "    def on_event(self, event, context):\n"
    "        if event.type == 'PACKET_OBSERVED':\n"
    "            return [context.replay(target='ROC', source_observation_id=event.body.get('observation_id'),\n"
    "                                   payload={'packet_hex': 'deadbeef'})]\n"
    "        return []\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class _Base(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-lc-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-lc-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-lc-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))
        self._conns = []
        self.addCleanup(self._recover_all)

    def _recover_all(self):
        # 시험 자체가 자원을 회수한다(잔존 child/socket/dir 0). 판정과 별개의 최후 방어.
        for conn in self._conns:
            try:
                if conn.residuals()["child_alive"]:
                    conn._terminate_child()
                conn._drain_child_output()
                conn._teardown_socket()
            except BaseException:  # noqa: BLE001
                pass

    def _connection(self, workload_id, script=_IDLE, base="lc"):
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
        return conn, provider, seams, snap

    def _assert_no_residue(self, conn):
        res = conn.residuals()
        self.assertFalse(res["child_alive"], res)
        self.assertFalse(res["socket_file_exists"], res)
        self.assertFalse(res["workload_dir_exists"], res)


# ---------------------------------------------------------------------------
# R6: close 예산·예외 보존
# ---------------------------------------------------------------------------
class _SlowTeardown:
    """실 connection의 close를 감싸 teardown에 걸린 시간을 fake clock으로 주입한다."""

    def __init__(self, inner, clock, teardown_s):
        self._inner = inner
        self._clock = clock
        self._teardown_s = float(teardown_s)

    def close(self):
        self._clock.advance(self._teardown_s)  # 실 teardown 동안 흐른 시간(주입)
        return self._inner.close()


class _RaisingClose:
    def __init__(self, inner, exc):
        self._inner = inner
        self._exc = exc

    def close(self):
        self._inner.close()  # 실 teardown은 수행
        raise self._exc


class R6CloseBudgetTest(_Base):
    def test_r6_700s_teardown_exceeds_close_budget_and_is_rejected(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock, policy=SessionPolicy(
            stage7=Stage7Timeouts(workload_s=900.0, close_s=600.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn, *_ = self._connection("w6a")
        conn.start()
        self.assertTrue(conn.running)
        runtime.begin_workload()
        runtime.end_workload()
        # close 시작 시각은 teardown **전에** 기록되므로 700초 teardown은 close 예산(600초)을 초과한다.
        with self.assertRaises(SessionExpired) as ctx:
            runtime.close(_SlowTeardown(conn, clock, 700.0))
        self.assertEqual(ctx.exception.deadline, Deadline.CLOSE)
        self._assert_no_residue(conn)  # 예산 초과 판정과 무관하게 실 child는 정리됨

    def test_r6_teardown_within_budget_passes(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock, policy=SessionPolicy(
            stage7=Stage7Timeouts(workload_s=900.0, close_s=600.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn, *_ = self._connection("w6b")
        conn.start()
        runtime.begin_workload()
        runtime.end_workload()
        report = runtime.close(_SlowTeardown(conn, clock, 100.0))
        self.assertFalse(report["child_alive"])
        self._assert_no_residue(conn)

    def test_r6_close_exception_preserves_original_and_still_cleans_session(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock)
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn, *_ = self._connection("w6c")
        conn.start()
        runtime.begin_workload()
        runtime.end_workload()
        boom = RuntimeError("injected close failure")
        with self.assertRaises(RuntimeError) as ctx:
            runtime.close(_RaisingClose(conn, boom))
        self.assertIs(ctx.exception, boom)          # 원형 예외 보존(대체·삼킴 없음)
        self.assertEqual(runtime.state, "CLOSED")   # 예외에도 session cleanup 완료
        self._assert_no_residue(conn)

    def test_r6_begin_close_is_idempotent_no_budget_extension(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock, policy=SessionPolicy(
            stage7=Stage7Timeouts(close_s=10.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        session.begin_close()
        clock.advance(8.0)
        session.begin_close()          # 재호출로 시작점이 옮겨지면 안 된다
        clock.advance(5.0)             # 총 13초 > 10초
        with self.assertRaises(SessionExpired) as ctx:
            session.complete_close()
        self.assertEqual(ctx.exception.deadline, Deadline.CLOSE)


# ---------------------------------------------------------------------------
# R8: 종료 중 통신 오류·인터럽트에도 OS/provider 정리 완주, 소유권 불확실 유지
# ---------------------------------------------------------------------------
class R8ShutdownCleanupTest(_Base):
    def _armed_runner(self, workload_id):
        conn, provider, seams, snap = self._connection(workload_id)
        calls = []
        original = provider.cleanup

        def cleanup():
            calls.append(True)
            return original()
        provider.cleanup = cleanup
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id=workload_id,
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(provider))
        self.assertTrue(runner.arm() and conn.running)
        return conn, runner, calls

    def test_r8_finish_close_send_failure_still_cleans_os_and_provider(self):
        conn, runner, calls = self._armed_runner("w8a")

        def fail_send(messages):
            raise BrokenPipeError("injected FINISH/CLOSE send failure")
        conn._send_all = fail_send
        result = runner.finish(ScenarioVerdict.PASS)
        # fail-closed 판정 유지 + OS 자원 정리 완주 + provider cleanup 호출 + 소유권 불확실 유지.
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        self._assert_no_residue(conn)
        self.assertGreaterEqual(len(calls), 1, "provider cleanup must run despite send failure")
        self.assertEqual(result.infrastructure_error_code, "POC_RADIO_OWNERSHIP_UNCERTAIN")
        # 메모리 정리만으로 RF 반환 완료를 선언하지 않는다: cleanup report에 error correlation.
        self.assertIn("POC_RADIO_OWNERSHIP_UNCERTAIN", result.evidence["failure_codes"])

    def test_r8_close_workload_records_protocol_error_and_continues(self):
        conn, *_ = self._connection("w8b")
        conn.start()
        self.assertTrue(conn.running)

        def fail_send(messages):
            raise ConnectionResetError("injected")
        conn._send_all = fail_send
        report = conn.close_workload(infrastructure="COMPLETED")  # 예외로 전파되지 않는다
        self.assertTrue(report["protocol_error"])                 # 원래 오류 보존
        self.assertIn(report["steps"][-1], ("VOLUNTARY", "SIGTERM", "SIGKILL"))
        self._assert_no_residue(conn)

    def test_r8_interrupt_during_close_reraised_after_cleanup(self):
        conn, *_ = self._connection("w8c")
        conn.start()
        self.assertTrue(conn.running)

        def interrupt_send(messages):
            raise KeyboardInterrupt()
        conn._send_all = interrupt_send
        with self.assertRaises(KeyboardInterrupt):
            conn.close()  # 정리를 마친 뒤 원형 인터럽트 재전달
        self._assert_no_residue(conn)  # 인터럽트에도 child/socket/dir 정리됨

    def test_r8_cleanup_step_exception_does_not_skip_remaining(self):
        conn, *_ = self._connection("w8d")
        conn.start()
        original_drain = conn._drain_child_output

        def bad_drain():
            raise OSError("injected drain failure")
        conn._drain_child_output = bad_drain
        report = conn.close_workload()
        self.assertIn("EXCEPTION:OSError", report["cleanup_errors"])  # 삼키지 않고 보존
        conn._drain_child_output = original_drain
        # drain 실패에도 socket teardown·child 종료는 계속됐다.
        self.assertFalse(conn.residuals()["socket_file_exists"])
        self.assertFalse(conn.residuals()["child_alive"])

    def test_r8_reentrant_close_is_safe(self):
        conn, runner, calls = self._armed_runner("w8e")
        conn._send_all = lambda m: (_ for _ in ()).throw(BrokenPipeError("x"))
        r1 = runner.finish(ScenarioVerdict.PASS)
        r2 = runner.finish(ScenarioVerdict.PASS)  # 재진입: 같은 결과, 중복 terminal/close 없음
        self.assertIs(r1, r2)
        self._assert_no_residue(conn)


# ---------------------------------------------------------------------------
# R10: 같은 PID 재접속 / 복구 불가 cleanup
# ---------------------------------------------------------------------------
class R10ReconnectTest(_Base):
    def _pump_until(self, conn, pred, timeout=6.0):
        """pump를 먼저 돌려 단절 감지→RECONNECTING→reconnect_accept를 구동한 뒤 pred를 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conn.pump()
            if pred():
                return True
        return pred()

    def _reconnected_or_aborted(self, conn):
        return (conn.engine.evidence["reconnects"] >= 1
                or conn.engine.state.value == "ABORTED")

    def test_r10_same_pid_reconnect_epoch_increment_no_provider_duplicate(self):
        conn, provider, seams, snap = self._connection("w10a", script=_REACTIVE, base="react")
        # runner.arm()이 실 child spawn·start·ARMED를 수행한다(이중 start 금지).
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id="w10a",
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(
                                    provider, terminal_source=rf_off_terminal_source()))
        self.assertTrue(runner.arm())
        self.assertTrue(conn.running)
        self.assertIsNone(conn.residuals()["child_exit_code"])  # 살아있음
        child_pid = conn._child.pid
        # 단절 전 실 action 1건을 처리해 provider 호출 1로 만든다(재접속 뒤 중복 호출 0 검증).
        runner.deliver(observation_step(make_observation(source_station="ROC",
                                                         destination_station="MASS-A")))
        calls_before = conn.engine.evidence["provider_submit_calls"]
        self.assertEqual(calls_before, 1)
        epoch0 = conn.engine.connection_epoch
        # 일시 단절: 연결 socket만 끊고 listening server는 유지.
        conn._server.connection.shutdown(socket.SHUT_RDWR)
        self.assertTrue(self._pump_until(conn, lambda: self._reconnected_or_aborted(conn)))
        self.assertEqual(conn.engine.state.value, "RUNNING",
                         "same-PID reconnect must restore RUNNING")
        self.assertEqual(conn.engine.connection_epoch, epoch0 + 1)
        self.assertEqual(conn.engine.evidence["reconnects"], 1)
        self.assertEqual(conn._child.pid, child_pid)                 # process 재시작 아님
        self.assertTrue(conn.residuals()["child_alive"])
        # 재접속이 이미 resolved된 action을 다시 제출하게 하지 않는다(provider 중복 호출 0).
        self.assertEqual(conn.engine.evidence["provider_submit_calls"], calls_before)
        # 정상 종료까지.
        result = runner.finish(ScenarioVerdict.PASS)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self._assert_no_residue(conn)

    def test_r10_event_delivery_resumes_after_reconnect(self):
        conn, *_ = self._connection("w10b")
        conn.start()
        self.assertTrue(conn.running)
        # 단절 → 같은 PID 재접속(resume) 뒤 이벤트 전달이 재개된다.
        conn._server.connection.shutdown(socket.SHUT_RDWR)
        self.assertTrue(self._pump_until(conn, lambda: self._reconnected_or_aborted(conn)))
        self.assertEqual(conn.engine.state.value, "RUNNING")
        self.assertEqual(conn.engine.evidence["reconnects"], 1)
        conn.emit_event("SCENARIO_MARK", {"n": 1})
        conn.pump()
        # 재접속 뒤 큐잉된 이벤트가 전달돼 event_queue가 0으로 비워진다(deadline 초기화 없음).
        self.assertEqual(int(conn.engine.residual_snapshot().get("event_queue", 0)), 0)
        conn.close()
        self._assert_no_residue(conn)

    def test_r10_unrecoverable_disconnect_aborts_and_cleans(self):
        conn, *_ = self._connection("w10c")
        conn.start()
        self.assertTrue(conn.running)
        # child를 먼저 죽여 재접속이 불가능하게 만든 뒤 단절 → 2초·3회 안에 복구 실패 → ABORT.
        from signal import SIGKILL
        conn._child.send_signal(SIGKILL)
        conn._child.wait(timeout=2.0)
        try:
            conn._server.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.assertTrue(self._pump_until(conn, lambda: conn.engine.state.value == "ABORTED",
                                         timeout=8.0))
        err = conn.infrastructure_error
        self.assertIsNotNone(err)
        self.assertIn(err.code.value, ("POC_CONNECTION_LOST", "POC_PROCESS_EXITED"))
        report = conn.close()
        self.assertFalse(report["child_alive"])
        self._assert_no_residue(conn)

    def test_r10_normal_close_is_not_treated_as_reconnect(self):
        conn, *_ = self._connection("w10d")
        conn.start()
        self.assertTrue(conn.running)
        report = conn.close()   # 정상 CLOSE: 재접속 시도 없이 종료
        self.assertEqual(conn.engine.evidence["reconnects"], 0)
        self.assertIn(report["steps"][-1], ("VOLUNTARY", "SIGTERM", "SIGKILL"))
        self._assert_no_residue(conn)


# ---------------------------------------------------------------------------
# R11: tee 유계·correlation 보존·변조 방지·overflow 요약·cleanup 구분
# ---------------------------------------------------------------------------
class R11BoundedTeeTest(_Base):
    def _submit(self, conn, rid, action):
        msg = build_message(ACTION_SUBMIT, session_id="s1", workload_id=conn.engine.workload_id,
                            connection_epoch=conn.engine.connection_epoch, protocol_minor=0,
                            body={"request_id": rid, "action": action})
        conn._handle_inbound(json.dumps(msg).encode())

    def test_r11_unique_rejected_requests_bounded_with_overflow_summary(self):
        conn, *_ = self._connection("w11a")
        conn.start()
        self.assertTrue(conn.running)
        conn._server.send_packet = lambda packet: None  # socket 배압 회피용(엔진·tee·parser는 실제)
        for i in range(1000):
            self._submit(conn, f"request-{i}", {"action_kind": "INVALID_KIND", "target": "ROC"})
        self.assertLessEqual(len(conn._submitted), 32)
        self.assertLessEqual(len(conn._results), 32)
        self.assertLessEqual(len(conn._new_results), 32)
        self.assertEqual(conn.engine.evidence["provider_submit_calls"], 0)
        self.assertEqual(conn.engine.state.value, "RUNNING")
        ov = conn.tee_overflow
        self.assertGreaterEqual(ov["submitted_overflow"], 900)   # 유실을 숨기지 않는 요약
        self.assertGreaterEqual(ov["results_overflow"], 900)
        tee = conn.evidence()["tee"]
        self.assertEqual(tee["max_entries"], 32)
        self.assertLessEqual(tee["bytes"], tee["max_bytes"])
        conn.close()
        self._assert_no_residue(conn)

    def test_r11_same_id_tamper_does_not_overwrite_first_metadata(self):
        conn, *_ = self._connection("w11b")
        conn.start()
        conn._server.send_packet = lambda packet: None
        self._submit(conn, "dup-1", {"action_kind": "REPLAY_PACKET", "target": "ROC", "v": 1})
        first = dict(conn._submitted["dup-1"])
        self._submit(conn, "dup-1", {"action_kind": "REPLAY_PACKET", "target": "MASS-A", "v": 2})
        self.assertEqual(conn._submitted["dup-1"], first)          # first-writer-wins
        self.assertEqual(conn.tee_overflow["submitted_duplicate"], 1)
        conn.close()
        self._assert_no_residue(conn)

    def test_r11_accepted_correlation_never_evicted(self):
        conn, provider, seams, snap = self._connection("w11c", script=_REACTIVE, base="react")
        # runner.arm()이 start를 수행한다(이중 start 금지).
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id="w11c",
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(
                                    provider, terminal_source=rf_off_terminal_source()))
        self.assertTrue(runner.arm())
        runner.deliver(observation_step(make_observation(source_station="ROC",
                                                         destination_station="MASS-A")))
        accepted = list(conn._accepted)
        self.assertEqual(len(accepted), 1)
        rid = accepted[0]
        self.assertIn(rid, conn._results)
        conn._server.send_packet = lambda packet: None
        # accepted 뒤 거부 요청을 상한 이상 밀어 넣어도 accepted correlation은 남는다.
        for i in range(200):
            self._submit(conn, f"junk-{i}", {"action_kind": "INVALID_KIND", "target": "ROC"})
        self.assertIn(rid, conn._results)
        self.assertEqual(conn._results[rid]["admission"], "accepted")
        self.assertIn(rid, conn._accepted)
        self.assertGreaterEqual(conn.tee_overflow["results_overflow"], 100)  # 초과는 요약으로
        # finish: 상한을 넘는 미처리 거부 결과는 runner 자체 action 상한(32)의 fail-closed에 걸린다
        # (ABORTED는 runner의 정당한 fail-closed이지 tee 결함이 아님). 어느 쪽이든 잔존 0.
        result = runner.finish(ScenarioVerdict.PASS)
        self.assertIn(result.poc_infrastructure,
                      (PocInfrastructure.COMPLETED, PocInfrastructure.ABORTED))
        # cleanup 뒤에도 accepted correlation 기록은 폐기되지 않는다(unresolved/accepted 보존).
        self.assertIn(rid, conn._accepted)
        self.assertIn(rid, conn._results)
        self._assert_no_residue(conn)


class N3AcceptedReplayTee(_Base):
    """N3: accepted/unknown replay가 pending result 상한을 우회하던 문제. 같은 request를 반복 전달해도
    _new_results는 rid별 1개로 coalesce하고 provider는 정확히 1회만 호출한다(멱등성·correlation 보존)."""

    def _accepted_submit(self, conn, rid="same-request", extra=None):
        action = {"action_kind": "REPLAY_PACKET", "target": "ROC", "payload": {"packet_hex": "0102"}}
        if extra:
            action.update(extra)
        msg = build_message(ACTION_SUBMIT, session_id="s1", workload_id=conn.engine.workload_id,
                            connection_epoch=conn.engine.connection_epoch, protocol_minor=0,
                            body={"request_id": rid, "action": action})
        conn._handle_inbound(json.dumps(msg).encode())

    def test_n3_repeated_accepted_replay_coalesces_provider_once(self):
        conn, provider, seams, snap = self._connection("wn3a", script=_REACTIVE, base="react")
        conn.start()
        self.assertTrue(conn.running)
        conn._server.send_packet = lambda packet: None  # 배압 회피(엔진·tee·parser는 실제)
        for _ in range(1000):
            self._accepted_submit(conn)
        info = {"new_results": len(conn._new_results),
                "cached_requests": conn.engine.residual_snapshot()["cached_requests"],
                "provider_calls": conn.engine.evidence["provider_submit_calls"],
                "coalesced": conn.tee_overflow["results_coalesced"]}
        # 멱등성: engine cache 1, provider 정확히 1회. 큐는 rid 1개로 유계(1000 미보관).
        self.assertEqual(info["cached_requests"], 1, info)
        self.assertEqual(info["provider_calls"], 1, info)
        self.assertLessEqual(info["new_results"], 32, info)
        self.assertEqual(info["new_results"], 1, info)
        self.assertGreaterEqual(info["coalesced"], 990, info)
        # accepted correlation은 보존된다(삭제 아님).
        self.assertIn("same-request", conn._results)
        self.assertIn("same-request", conn._accepted)
        conn.close()
        self._assert_no_residue(conn)

    def test_n3_poll_resets_pending_then_single_redeliver(self):
        # poll로 drain하면 pending이 초기화되고, 이후 같은 accepted가 다시 정확히 1회만 큐에 오른다
        # (poll 사이 반복은 다시 coalesce). runner의 _processed가 재제출 없이 중복을 흡수한다.
        conn, provider, seams, snap = self._connection("wn3b", script=_REACTIVE, base="react")
        conn.start()
        conn._server.send_packet = lambda packet: None
        for _ in range(10):
            self._accepted_submit(conn)
        first = conn.poll_new_results()
        self.assertEqual(len(first), 1)
        for _ in range(10):
            self._accepted_submit(conn)
        second = conn.poll_new_results()
        self.assertEqual(len(second), 1)                     # poll 뒤 다시 1개(중복 아님)
        self.assertEqual(conn.engine.evidence["provider_submit_calls"], 1)  # 여전히 1회
        conn.close()
        self._assert_no_residue(conn)

    def test_n3_contradictory_replay_is_fail_closed(self):
        # 같은 rid에 다른 결과 body가 오면 성공으로 덮지 않고 fail-closed 카운트(방어적; 정상 engine은
        # cache로 동일 body를 돌려주므로 발생하지 않는다). _observe_outbound를 직접 구동해 검증.
        conn, provider, seams, snap = self._connection("wn3c")
        conn.start()
        base = {"type": ACTION_RESULT, "body": {"request_id": "rid-x", "admission": "accepted",
                                                "physical_channel": "lower", "error_code": None}}
        conn._observe_outbound(base)
        self.assertIn("rid-x", conn._results)
        prior = dict(conn._results["rid-x"])
        contradictory = {"type": ACTION_RESULT, "body": {"request_id": "rid-x", "admission": "accepted",
                                                         "physical_channel": "upper", "error_code": None}}
        conn._observe_outbound(contradictory)
        self.assertEqual(conn._results["rid-x"], prior)      # 원 결과 보존(덮어쓰기 없음)
        self.assertEqual(conn.tee_overflow["contradictory_result"], 1)
        conn.close()
        self._assert_no_residue(conn)

    def test_n3_unknown_replay_coalesces_and_preserves_correlation(self):
        # unknown admission replay도 coalesce하고 correlation(_results)을 유지한다. 직접 구동.
        conn, provider, seams, snap = self._connection("wn3d")
        conn.start()
        msg = {"type": ACTION_RESULT, "body": {"request_id": "rid-u", "admission": "unknown",
                                               "physical_channel": "lower", "error_code": None}}
        for _ in range(500):
            conn._observe_outbound(dict(msg))
        self.assertEqual(len(conn._new_results), 1)          # 1개로 coalesce
        self.assertGreaterEqual(conn.tee_overflow["results_coalesced"], 499)
        self.assertIn("rid-u", conn._results)                # unknown correlation 보존
        conn.close()
        self._assert_no_residue(conn)

    def test_n3_distinct_accepted_preserved_within_engine_cap(self):
        # 서로 다른 accepted rid는 correlation을 위해 각각 1회 전달·보존된다(engine cap 32 이내).
        conn, provider, seams, snap = self._connection("wn3e")
        conn.start()
        for i in range(20):
            conn._observe_outbound({"type": ACTION_RESULT, "body": {
                "request_id": f"rid-{i}", "admission": "accepted",
                "physical_channel": "lower", "error_code": None}})
        self.assertEqual(len(conn._new_results), 20)
        self.assertEqual(len(conn._accepted), 20)
        conn.close()
        self._assert_no_residue(conn)


if __name__ == "__main__":
    unittest.main()
