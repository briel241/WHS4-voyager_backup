"""Stage 8 6단계(전반부) — production 실행 경로 end-to-end 통합 시험(RF-off, Linux 전용).

Stage 4 `WorkloadRunner`를 **실 `SupervisorConnection`**(실 subprocess child·실 Unix socket·SDK·
engine)으로 구동하고, ACTION_SUBMIT을 **실 Stage 5 `SharedMediumActionProvider`**(RF-off software
device 경계)로 종단시킨다. loopback fake가 아니라 실 process/socket/provider 경로다. 장비 경계만
software harness다.

검증:
- PoC OFF 구조적 0(import만으로 자원 0).
- 선제 action(SCENARIO_STARTED)과 반응형 action(PACKET_OBSERVED)이 실 child에서 제출되어
  Stage 5 provider로 C4+C6 atomic fan-out 방사되고, terminal이 provider outcome에서 유도된다.
- 반응형 observation_id가 scheduler trigger로 결속된다(after_slots reactive).
- 정상 수락·submit 전 자원 충돌·callback crash·모든 종료 경로 cleanup 잔존 0.
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
    from vdeter.poc._supervisor.child import SubprocessChildSpawner
    from vdeter.poc._supervisor.clock import RealMonotonicClock
    from vdeter.poc.execution.child_main import (
        ENV_ENTRYPOINT, ENV_HELLO, ENV_SNAPSHOT_SCRIPT,
    )
    from vdeter.poc.execution.lease import MassBLease
    from vdeter.poc.execution.runner import (
        WorkloadRunner, WorkloadSpec, observation_step, scenario_started,
    )
    from vdeter.poc.execution.snapshot import create_snapshot
    from vdeter.poc.execution.verdict import (
        ActionExecution, PocInfrastructure, ScenarioVerdict, TargetEffect,
    )
    from vdeter.poc.integration.rf_off import assemble_rf_off_provider, rf_off_terminal_source
    from vdeter.poc.integration.supervisor_connection import SupervisorConnection
    from vdeter.poc.integration.tx_executor import ProductionTxExecutor
    from vdeter.poc.api.negotiation import ServerCapabilityConfig
    from vdeter.poc.testing.execution_fakes import write_poc_package
    from vdeter.poc.testing.fakes import make_observation


# 반응형: PACKET_OBSERVED에 replay(ROC)를 NEXT_SAFE_SLOT로 제출.
_REACTIVE_REPLAY = (
    "class Poc:\n"
    "    def on_start(self, context):\n"
    "        return []\n"
    "    def on_event(self, event, context):\n"
    "        if event.type == 'PACKET_OBSERVED':\n"
    "            return [context.replay(target='ROC',\n"
    "                                   source_observation_id=event.body.get('observation_id'),\n"
    "                                   payload={'packet_hex': 'deadbeefcafe'})]\n"
    "        return []\n"
    "    def on_finish(self, context):\n"
    "        return None\n"
).encode("utf-8")

# 선제: SCENARIO_STARTED에 replay(MASS-A)를 제출(반응형 아님, trigger 불필요).
_PREEMPTIVE_REPLAY = (
    "class Poc:\n"
    "    def on_start(self, context):\n"
    "        return []\n"
    "    def on_event(self, event, context):\n"
    "        if event.type == 'SCENARIO_STARTED':\n"
    "            return [context.replay(target='MASS-A',\n"
    "                                   payload={'packet_hex': 'a1b2c3d4'})]\n"
    "        return []\n"
    "    def on_finish(self, context):\n"
    "        return None\n"
).encode("utf-8")

# 반응형 after_slots(trigger 등록 필요): observation_id를 scheduler trigger로 결속해야 통과.
_REACTIVE_AFTER_SLOTS = (
    "class Poc:\n"
    "    def on_start(self, context):\n"
    "        return []\n"
    "    def on_event(self, event, context):\n"
    "        if event.type == 'PACKET_OBSERVED':\n"
    "            return [context.replay(target='ROC',\n"
    "                                   source_observation_id=event.body.get('observation_id'),\n"
    "                                   schedule={'kind': 'after_slots', 'slots': 2},\n"
    "                                   payload={'packet_hex': 'deadbeef'})]\n"
    "        return []\n"
    "    def on_finish(self, context):\n"
    "        return None\n"
).encode("utf-8")

# callback crash(§11.2 fail-closed).
_CRASH = (
    "class Poc:\n"
    "    def on_start(self, context):\n"
    "        return []\n"
    "    def on_event(self, event, context):\n"
    "        raise RuntimeError('poc crash on event')\n"
    "    def on_finish(self, context):\n"
    "        return None\n"
).encode("utf-8")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class ProductionRealPathTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-int-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-int-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-int-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))

    def _snapshot(self, base, script, dest):
        write_poc_package(self.poc, base, poc_id=base.replace("_", "-"), script=script)
        return create_snapshot(self.poc, base, dest_dir=os.path.join(self.snap_root, dest),
                               scenario_id="SC-VDE-006")

    def _chain(self, snap, workload_id, *, seam_kwargs=None, target_response=None):
        provider, seams = assemble_rf_off_provider(**(seam_kwargs or {}))
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.poc.execution.child_main"],
            extra_env={
                ENV_SNAPSHOT_SCRIPT: snap.script_path,
                ENV_ENTRYPOINT: snap.descriptor.entrypoint,
                ENV_HELLO: json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": workload_id,
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src"
                ) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
        )
        conn = SupervisorConnection(
            provider=provider, spawner=spawner,
            session_id="s1", workload_id=workload_id, run_id="r1", scenario_id="SC-VDE-006",
            manifest=snap.descriptor, server_config=ServerCapabilityConfig(),
            clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, workload_id),
        )
        lease = MassBLease()
        # R3: RF-off 종단은 장비 double의 child terminal source를 **명시적으로** 주입한다.
        runner = WorkloadRunner(conn, lease, run_id="r1", workload_id=workload_id,
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(
                                    provider, terminal_source=rf_off_terminal_source(),
                                    target_response=target_response))
        return provider, seams, conn, lease, runner

    def _assert_clean(self, conn, lease):
        res = conn.residuals()
        self.assertFalse(res["child_alive"])
        self.assertFalse(res["socket_file_exists"])
        self.assertFalse(res["workload_dir_exists"])
        self.assertEqual(res["engine"]["inflight_actions"], 0)
        self.assertEqual(lease.role.value, "NORMAL")
        self.assertIsNone(lease.holder)

    def test_reactive_replay_full_chain(self):
        snap = self._snapshot("reactive", _REACTIVE_REPLAY, "w1")
        provider, seams, conn, lease, runner = self._chain(snap, "w1")
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 1)
        attack = result.attacks[0]
        self.assertEqual(attack.action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(attack.target, "ROC")
        self.assertEqual(attack.physical_channel, "lower")
        # 실제 provider가 C4+C6 atomic fan-out을 software group TX로 방사했다.
        self.assertEqual(seams.group_tx.committed_groups, 1)
        self.assertEqual(seams.group_tx.radiated_children, 2)
        self._assert_clean(conn, lease)

    def test_preemptive_replay_full_chain(self):
        snap = self._snapshot("preempt", _PREEMPTIVE_REPLAY, "w2")
        provider, seams, conn, lease, runner = self._chain(snap, "w2")
        result = runner.run(WorkloadSpec(steps=[scenario_started()]))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(result.attacks[0].target, "MASS-A")
        self.assertEqual(result.attacks[0].physical_channel, "upper")
        self.assertEqual(seams.group_tx.committed_groups, 1)
        self._assert_clean(conn, lease)

    def test_reactive_after_slots_binds_observation_trigger(self):
        # after_slots는 VDETER가 등록한 trigger(observation_id) 없이는 SCHEMA_INVALID로 거부된다.
        # SupervisorConnection이 observation_id를 정수 slot trigger로 등록하므로 accepted 되어야 한다.
        snap = self._snapshot("aftr", _REACTIVE_AFTER_SLOTS, "w3")
        provider, seams, conn, lease, runner = self._chain(snap, "w3")
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(seams.group_tx.committed_groups, 1)
        self._assert_clean(conn, lease)

    def test_resource_conflict_rejects_without_radiation(self):
        # 정상 TX가 lower:slot-1 점유 → PoC replay(ROC=lower) submit 전 거부, 방사 0.
        snap = self._snapshot("conf", _REACTIVE_REPLAY, "w4")
        provider, seams, conn, lease, runner = self._chain(
            snap, "w4", seam_kwargs={"normal_reserved": ("lower:slot-1",)})
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        # PoC action은 REJECTED_BEFORE_TX, 그러나 infrastructure는 정상(§11.1: 거부는 실패 아님).
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.REJECTED_BEFORE_TX)
        self.assertEqual(seams.group_tx.submits, [])
        self._assert_clean(conn, lease)

    def test_callback_crash_fail_closed_cleanup(self):
        snap = self._snapshot("crash", _CRASH, "w5")
        provider, seams, conn, lease, runner = self._chain(snap, "w5")
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        # child callback 예외 → infrastructure ABORTED/ERROR, 방사 0, cleanup 잔존 0.
        self.assertIn(result.poc_infrastructure,
                      (PocInfrastructure.ABORTED, PocInfrastructure.ERROR))
        self.assertEqual(seams.group_tx.submits, [])
        self._assert_clean(conn, lease)

    def test_commit_unknown_session_inhibit_through_real_chain(self):
        # group commit이 불명확(software submit 실패) → POC_TX_OUTCOME_UNKNOWN(SESSION_INHIBIT).
        # 실 child가 replay를 제출하고 provider가 commit 불명확을 내면 workload는 성공으로 추정하지
        # 않고 fail-closed로 종결한다(§11.2, §14). 방사(commit) 기록 0, cleanup 잔존 0.
        snap = self._snapshot("cunk", _REACTIVE_REPLAY, "w6")
        provider, seams, conn, lease, runner = self._chain(
            snap, "w6", seam_kwargs={"fail_on_submit": True})
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        self.assertIn(result.poc_infrastructure,
                      (PocInfrastructure.ABORTED, PocInfrastructure.ERROR))
        self.assertEqual(result.infrastructure_error_code, "POC_TX_OUTCOME_UNKNOWN")
        self.assertEqual(seams.group_tx.committed_groups, 0)  # 방사 성공 기록 0
        self._assert_clean(conn, lease)

    def test_not_tuned_observation_makes_no_event_or_action(self):
        # NOT_TUNED 관측은 PACKET_OBSERVED event·bytes를 만들지 않는다(§8.4). child는 반응 없음.
        from vdeter.poc.api.types import TuningState
        snap = self._snapshot("nt", _REACTIVE_REPLAY, "w7")
        provider, seams, conn, lease, runner = self._chain(snap, "w7")
        obs = make_observation(source_station="ROC", destination_station="MASS-A",
                               tuning_state=TuningState.NOT_TUNED)
        result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 0)          # event 없음 → action 없음
        self.assertEqual(seams.group_tx.submits, [])       # 방사 0
        self._assert_clean(conn, lease)


if __name__ == "__main__":
    unittest.main()
