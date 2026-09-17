"""Stage 8 6단계(전반부) 통합 — 진입 driver + resident runtime deadline 결속 시험(RF-off, Linux).

검증:
- entry.select_and_snapshot(catalog/direct) + run_poc_workload로 scenario 선택→snapshot→실행 종단.
- PoC none(baseline)은 child·provider 자원 0.
- snapshot A 실행 후 원본 수정 → snapshot B는 새 process·새 code.
- resident session deadline 만료가 **살아있는 실 child**의 CLOSE→SIGTERM→SIGKILL을 구동(잔존 0).
  비협조(SIGTERM 무시) child는 SIGKILL로 escalate. 시간은 fake clock으로 단축(900초 wall-clock은 Stage 6).
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
_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
_HARNESS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "support",
                                         "poc_child_harness.py"))

if _LINUX:
    from vdeter.poc._supervisor.child import SubprocessChildSpawner
    from vdeter.poc._supervisor.clock import RealMonotonicClock
    from vdeter.poc.execution.limits import SessionPolicy, ServiceSessionTimeouts
    from vdeter.poc.execution.runner import observation_step, scenario_started
    from vdeter.poc.execution.session import Deadline, ResidentSession
    from vdeter.poc.execution.verdict import ActionExecution, PocInfrastructure
    from vdeter.poc.integration.rf_off import (
        assemble_rf_off_provider, rf_off_seams, rf_off_terminal_source,
    )
    from vdeter.poc.integration.entry import (
        run_poc_workload, select_and_snapshot,
    )
    from vdeter.poc.integration.runtime import ResidentPocRuntime
    from vdeter.poc.integration.supervisor_connection import SupervisorConnection
    from vdeter.poc.api.negotiation import ServerCapabilityConfig
    from vdeter.poc.testing.execution_fakes import write_poc_package
    from vdeter.poc.testing.fakes import FakeClock, build_manifest, make_observation


def _reactive(marker):
    return (
        "class Poc:\n"
        f"    M = {marker!r}\n"
        "    def on_start(self, context):\n        return []\n"
        "    def on_event(self, event, context):\n"
        "        if event.type == 'PACKET_OBSERVED':\n"
        "            return [context.replay(target='ROC', source_observation_id=event.body.get('observation_id'),\n"
        "                                   payload={'packet_hex': 'deadbeef', 'marker': self.M})]\n"
        "        return []\n"
        "    def on_finish(self, context):\n        return None\n"
    ).encode("utf-8")


_IDLE = (
    "class Poc:\n"
    "    def on_start(self, context):\n        return []\n"
    "    def on_event(self, event, context):\n        return []\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class EntryDriverTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-ent-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-ent-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-ent-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))

    def test_catalog_select_and_run_full_chain(self):
        write_poc_package(self.poc, "cat_replay", poc_id="cat-replay", script=_reactive("A"))
        snap = select_and_snapshot(scenario_id="SC-VDE-006", poc_dir=self.poc, poc_id="cat-replay",
                                   dest_dir=os.path.join(self.snap_root, "w1"))
        self.assertIsNotNone(snap)
        result, seams = run_poc_workload(
            snapshot=snap, steps=[observation_step(make_observation(source_station="ROC",
                                                                    destination_station="MASS-A"))],
            scenario_id="SC-VDE-006", run_id="r1", workload_id="w1", session_id="s1",
            socket_root=self.sock_root, src_path=_SRC, device_seams=rf_off_seams(),
            terminal_source=rf_off_terminal_source())
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(seams.group_tx.committed_groups, 1)

    def test_baseline_none_creates_no_snapshot(self):
        write_poc_package(self.poc, "cat_replay", poc_id="cat-replay", script=_reactive("A"))
        snap = select_and_snapshot(scenario_id="SC-VDE-006", poc_dir=self.poc, poc_id="none",
                                   dest_dir=os.path.join(self.snap_root, "w1"))
        self.assertIsNone(snap)  # PoC none = 자원 0(구조적 0)

    def test_incompatible_poc_not_selectable(self):
        write_poc_package(self.poc, "other", poc_id="other",
                          script=_reactive("A"), compatible_scenarios=("SC-VDE-001",))
        from vdeter.poc.execution.selection import SelectionError
        with self.assertRaises(SelectionError):
            select_and_snapshot(scenario_id="SC-VDE-006", poc_dir=self.poc, poc_id="other",
                                dest_dir=os.path.join(self.snap_root, "w1"))

    def test_snapshot_isolation_across_original_edit(self):
        write_poc_package(self.poc, "iso", poc_id="iso", script=_reactive("A"))
        snap_a = select_and_snapshot(scenario_id="SC-VDE-006", poc_dir=self.poc, poc_id="iso",
                                     dest_dir=os.path.join(self.snap_root, "wa"))
        res_a, _ = run_poc_workload(
            snapshot=snap_a, steps=[observation_step(make_observation(source_station="ROC",
                                                                      destination_station="MASS-A"))],
            scenario_id="SC-VDE-006", run_id="r1", workload_id="wa", session_id="s1",
            socket_root=self.sock_root, src_path=_SRC, device_seams=rf_off_seams(),
            terminal_source=rf_off_terminal_source())
        self.assertEqual(res_a.poc_infrastructure, PocInfrastructure.COMPLETED)
        # 원본 수정 → 새 snapshot B(다른 script sha).
        os.chmod(os.path.join(self.poc, "iso.py"), 0o600)
        with open(os.path.join(self.poc, "iso.py"), "wb") as fh:
            fh.write(_reactive("B"))
        snap_b = select_and_snapshot(scenario_id="SC-VDE-006", poc_dir=self.poc, poc_id="iso",
                                     dest_dir=os.path.join(self.snap_root, "wb"))
        self.assertNotEqual(snap_a.script_sha256, snap_b.script_sha256)
        res_b, _ = run_poc_workload(
            snapshot=snap_b, steps=[observation_step(make_observation(source_station="ROC",
                                                                      destination_station="MASS-A"))],
            scenario_id="SC-VDE-006", run_id="r1", workload_id="wb", session_id="s1",
            socket_root=self.sock_root, src_path=_SRC, device_seams=rf_off_seams(),
            terminal_source=rf_off_terminal_source())
        self.assertEqual(res_b.poc_infrastructure, PocInfrastructure.COMPLETED)


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class ResidentRuntimeDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-rt-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-rt-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-rt-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))

    def _idle_connection(self, workload_id):
        write_poc_package(self.poc, "idle", poc_id="idle", script=_IDLE)
        from vdeter.poc.execution.snapshot import create_snapshot
        snap = create_snapshot(self.poc, "idle", dest_dir=os.path.join(self.snap_root, workload_id),
                               scenario_id="SC-VDE-006")
        provider, seams = assemble_rf_off_provider()
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.poc.execution.child_main"],
            extra_env={
                "VDETER_POC_SNAPSHOT_SCRIPT": snap.script_path,
                "VDETER_POC_ENTRYPOINT": snap.descriptor.entrypoint,
                "VDETER_POC_HELLO": json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": workload_id,
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
            })
        return SupervisorConnection(
            provider=provider, spawner=spawner, session_id="s1", workload_id=workload_id,
            run_id="r1", scenario_id="SC-VDE-006", manifest=snap.descriptor,
            server_config=ServerCapabilityConfig(), clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, workload_id))

    def _harness_connection(self, workload_id):
        provider, _ = assemble_rf_off_provider()
        spawner = SubprocessChildSpawner(
            [sys.executable, _HARNESS],
            extra_env={
                "VDETER_POC_MODE": "ignore_sigterm",
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": workload_id,
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
            })
        return SupervisorConnection(
            provider=provider, spawner=spawner, session_id="s1", workload_id=workload_id,
            run_id="r1", scenario_id="SC-VDE-006", manifest=build_manifest(),
            server_config=ServerCapabilityConfig(), clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, workload_id))

    def test_workload_deadline_drives_real_child_close(self):
        # 짧은 workload cap(1초)을 주입해 fake clock으로 만료 → 살아있는 협조 child가 종료된다.
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock,
                                  policy=SessionPolicy(service=ServiceSessionTimeouts(workload_s=1.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn = self._idle_connection("wd")
        conn.start()
        self.assertTrue(conn.running)
        runtime.begin_workload()
        self.assertTrue(conn.residuals()["child_alive"])  # child는 살아 idle 유지
        clock.set(2.0)  # workload cap(1초) 초과
        deadline, report = runtime.enforce_deadlines(conn)
        self.assertEqual(deadline, Deadline.WORKLOAD)
        self.assertFalse(report["child_alive"])           # deadline-driven close가 실 child 종료
        self.assertFalse(report["socket_file_exists"])
        self.assertEqual(report["engine"]["inflight_actions"], 0)

    def test_noncooperative_child_escalates_to_sigkill(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock,
                                  policy=SessionPolicy(service=ServiceSessionTimeouts(workload_s=1.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn = self._harness_connection("wk")
        conn.start()
        self.assertTrue(conn.run_until(lambda: conn.engine.state.value == "RUNNING", timeout=12.0))
        runtime.begin_workload()
        clock.set(2.0)
        deadline, report = runtime.enforce_deadlines(conn)
        self.assertEqual(deadline, Deadline.WORKLOAD)
        self.assertTrue(report["forced_kill"], "SIGTERM 무시 child는 SIGKILL로 escalate해야 함")
        self.assertFalse(report["child_alive"])
        self.assertFalse(report["socket_file_exists"])

    def test_ready_idle_and_absolute_expiry_are_distinct(self):
        # READY idle(2700) 만료: bring-up 후 요청 없이 idle 초과.
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock)
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        clock.set(2700.0 + 1.0)
        deadline, _ = runtime.enforce_deadlines(_NoChild())
        self.assertEqual(deadline, Deadline.READY_IDLE)

    def test_expired_close_budget_applies_and_does_not_extend(self):
        clock = FakeClock(0.0)
        session = ResidentSession(clock=clock,
                                  policy=SessionPolicy(service=ServiceSessionTimeouts(workload_s=1.0, close_s=1.0)))
        runtime = ResidentPocRuntime(session)
        runtime.bring_up()
        conn = self._idle_connection("wc")
        conn.start()
        runtime.begin_workload()
        clock.set(2.0)
        deadline, _ = runtime.enforce_deadlines(conn)
        self.assertEqual(deadline, Deadline.WORKLOAD)
        # EXPIRED 뒤 close 예산(1초) 적용. begin_close 후 close_s 초과면 CLOSE deadline.
        session.begin_close()
        clock.set(2.0 + 1.0 + 0.5)  # close 시작 뒤 close_s(1초) 초과
        from vdeter.poc.execution.session import SessionExpired
        with self.assertRaises(SessionExpired) as ctx:
            session.complete_close()
        self.assertEqual(ctx.exception.deadline, Deadline.CLOSE)


class _NoChild:
    """child 없는 connection stub(READY idle 등 child-무관 deadline 검증용)."""

    def close(self):
        return {"child_alive": False, "socket_file_exists": False, "workload_dir_exists": False,
                "engine": {"event_queue": 0, "replay_ring": 0, "inflight_actions": 0},
                "forced_kill": False, "steps": []}


if __name__ == "__main__":
    unittest.main()
