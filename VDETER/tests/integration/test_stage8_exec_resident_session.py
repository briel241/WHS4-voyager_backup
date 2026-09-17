"""resident session 통합 시험(실행 명세 §5.2, §5.3, §5.4, §13, §15.3).

baseline `none` → PoC A → 원본 수정·rescan → PoC B, snapshot 불변, invalid 거부 후 session
건전, workload마다 cleanup 잔존 0, MASS-B lease 배타·반환을 end-to-end로 검증한다.
catalog/snapshot이 POSIX 전용이라 Windows에서는 skip한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_POSIX = hasattr(os, "getuid") and sys.platform != "win32"

if _POSIX:
    from vdeter.poc.execution.catalog import scan_catalog
    from vdeter.poc.execution.selection import build_menu, rescan_summary
    from vdeter.poc.execution.session import ResidentSession, SessionState
    from vdeter.poc.execution.snapshot import create_snapshot, verify_snapshot
    from vdeter.poc.execution.runner import (
        WorkloadRunner,
        WorkloadSpec,
        observation_step,
        scenario_started,
    )
    from vdeter.poc.execution.verdict import PocInfrastructure, ScenarioVerdict
    from vdeter.poc.api.negotiation import ServerCapabilityConfig
    from vdeter.poc.testing.client_runner import RecordingCallbacks
    from vdeter.poc.testing.fakes import FakeClock, make_observation
    from vdeter.poc.testing.execution_fakes import (
        FakeTxExecutor,
        LoopbackConnection,
        ScenarioPriorityProvider,
        write_poc_package,
    )

SCRIPT_A = b"class Poc:\n    VERSION = 'A'\n"
SCRIPT_A2 = b"class Poc:\n    VERSION = 'A2'\n"


def _reactive(event, ctx):
    if event.type == "PACKET_OBSERVED":
        return [ctx.replay(target="ROC", source_observation_id=event.body.get("observation_id"))]
    return []


@unittest.skipUnless(_POSIX, "resident 통합 시험은 POSIX 전용")
class ResidentSessionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.poc = tempfile.mkdtemp(prefix="poc-res-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="snap-res-")
        self.clock = FakeClock(1000.0)
        self.session = ResidentSession(clock=self.clock)
        self.session.start()
        self.clock.advance(30.0)
        self.session.complete_bringup()

    def tearDown(self):
        shutil.rmtree(self.poc, ignore_errors=True)
        shutil.rmtree(self.snap_root, ignore_errors=True)

    def _run_poc_workload(self, base, wid, *, steps, manifest_scenario="SC-VDE-006"):
        """catalog→select→snapshot→loopback→runner로 한 PoC workload 실행."""

        self.session.note_control_plane_activity()
        catalog = scan_catalog(self.poc)
        menu = build_menu(catalog, manifest_scenario)
        entry = catalog.valid_by_id().get(base.replace("_", "-"))
        self.assertIsNotNone(entry, "package가 catalog valid에 없음")
        snap = create_snapshot(self.poc, base, dest_dir=os.path.join(self.snap_root, wid),
                               scenario_id=manifest_scenario)
        self.session.begin_workload()
        conn = LoopbackConnection(callbacks=RecordingCallbacks(event_action_fn=_reactive),
                                  manifest=snap.descriptor, clock=self.clock,
                                  provider=ScenarioPriorityProvider(),
                                  workload_id=wid)
        runner = WorkloadRunner(conn, self.session.lease, run_id="run-res", workload_id=wid,
                                scenario_id=manifest_scenario, snapshot=snap,
                                tx_executor=FakeTxExecutor())
        result = runner.run(WorkloadSpec(steps=tuple(steps),
                                         scenario_verdict=ScenarioVerdict.PASS))
        self.session.end_workload()
        return result, snap

    def test_full_resident_flow(self):
        # 1) package A
        write_poc_package(self.poc, "replay_attack", poc_id="replay-attack", script=SCRIPT_A)

        # 2) baseline 'none' workload — lease·connection 없음.
        self.session.begin_workload()
        self.assertFalse(self.session.lease.is_leased)
        self.session.end_workload()
        self.assertEqual(self.session.workloads_run, 1)

        # 3) PoC A workload
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result_a, snap_a = self._run_poc_workload(
            "replay_attack", "w-a",
            steps=(scenario_started(), observation_step(obs)))
        self.assertEqual(result_a.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result_a.attacks_requested, 1)
        # cleanup 잔존 0.
        eng = result_a.evidence["cleanup"]["engine"]
        self.assertEqual(eng["event_queue"], 0)
        self.assertEqual(eng["replay_ring"], 0)
        self.assertEqual(eng["inflight_actions"], 0)
        # lease 반환됨.
        self.session.lease.assert_returned()

        # 4) 원본 수정 → snapshot A 불변, rescan changed 감지.
        prev_catalog = scan_catalog(self.poc)
        os.chmod(os.path.join(self.poc, "replay_attack.py"), 0o600)
        with open(os.path.join(self.poc, "replay_attack.py"), "wb") as fh:
            fh.write(SCRIPT_A2)
        self.assertTrue(verify_snapshot(snap_a))  # 실행했던 snapshot A는 그대로
        curr_catalog = scan_catalog(self.poc)
        summary = rescan_summary(prev_catalog, curr_catalog)
        self.assertEqual(summary.changed_count, 1)
        self.assertEqual(summary.changed[0].poc_id, "replay-attack")

        # 5) 다음 workload는 새 snapshot B(변경 반영).
        obs2 = make_observation(source_station="ROC", destination_station="MASS-A")
        result_b, snap_b = self._run_poc_workload(
            "replay_attack", "w-b",
            steps=(scenario_started(), observation_step(obs2)))
        self.assertEqual(result_b.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertNotEqual(snap_a.script_sha256, snap_b.script_sha256)  # 다음 workload에서만 반영
        self.assertTrue(verify_snapshot(snap_a))  # A는 여전히 불변

        # 6) invalid package 추가 → menu에서 제외, session 건전 유지.
        write_poc_package(self.poc, "broken", poc_id="broken",
                          manifest_text="schema = \"wrong\"\n")
        catalog3 = scan_catalog(self.poc)
        menu3 = build_menu(catalog3, "SC-VDE-006")
        self.assertIn("broken", {e.name for e in menu3.excluded})
        self.assertNotIn("broken", menu3.selectable_ids())
        self.assertEqual(self.session.state, SessionState.READY)

        # 7) session close → lease 정상 반환.
        self.session.begin_close()
        self.session.complete_close()
        self.assertEqual(self.session.state, SessionState.CLOSED)
        self.session.lease.assert_returned()
        self.assertEqual(self.session.workloads_run, 3)

    def test_lease_exclusive_across_workloads(self):
        write_poc_package(self.poc, "replay_attack", poc_id="replay-attack", script=SCRIPT_A)
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        for wid in ("w1", "w2", "w3"):
            result, _ = self._run_poc_workload(
                "replay_attack", wid, steps=(scenario_started(), observation_step(obs)))
            self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
            self.session.lease.assert_returned()  # 매 workload 뒤 반환
        # lease history: 3 acquire / 3 release.
        kinds = [h[0] for h in self.session.lease.history]
        self.assertEqual(kinds.count("ACQUIRE"), 3)
        self.assertEqual(kinds.count("RELEASE"), 3)


if __name__ == "__main__":
    unittest.main()
