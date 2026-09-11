"""Stage 8 PoC provider seams를 **실 Stage 7 software group 객체**에 결속해 RF-off 종단 검증.

`SharedMediumActionProvider.for_production`(no stub)을 실행 중 software group의 실 MASS-B endpoint
객체(worker·frozen PHY·hardware clock·route sidecar·수신기)에 바인딩하고, PoC action 하나를 제출해
각 hop이 실 객체를 거치는지 확인한다. 하드웨어 경계(B210 device I/O)만 fake medium worker.

물리 실측 대기(코드 결속 완료, 실장비에서만 확인): 실 RF group atomicity, service-tuned 수신기
동시 도달(WSM-G1 common-LO — 여기서는 RF-off 정합 주입), device-rate 샘플 포맷.
"""

from __future__ import annotations

import os
import socket
import sys
import unittest

_LINUX = (hasattr(socket, "AF_UNIX") and hasattr(os, "getuid") and sys.platform != "win32")
_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SRC = os.path.normpath(os.path.join(_REPO, "VDETER", "src"))
for _p in (_REPO, os.path.join(_REPO, "VDETER"), _SRC, os.path.join(_REPO, "VDETER", "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@unittest.skipUnless(_LINUX, "실 Stage 7 software group(POSIX) 전용")
class RealStage7SeamsTest(unittest.TestCase):
    def _group(self):
        from tests.support.stage7 import build_stage7_software_group, start_stage7_group
        clock, medium, plan, coordinator, members, resolved = build_stage7_software_group()
        start_stage7_group(clock, medium, coordinator, members, resolved)
        return clock, medium, members

    def _assemble(self, members, medium, *, channel):
        from apps.stage8_stage7_seams import assemble_stage7_provider
        from vdeter.stage8.shared_medium import constants
        # WSM-G1 common-LO 정합 주입(RF-off): service-tuned 수신 동시 도달은 실장비.
        center = constants.CHANNEL_EFFECTIVE_CENTER_HZ[channel]
        for ep in ("ROC", "MASS-A"):
            members[ep].endpoint.dependencies.radio.lease.worker.rx_lo_hz = center
        provider, bindings = assemble_stage7_provider(members)
        return provider, bindings

    def test_poc_action_flows_through_real_mac_phy_group_tx_and_receive(self):
        from apps.stage8_stage7_seams import stage7_target_response, stage7_terminal_source
        from vdeter.phy.transmitter import Phy11Transmitter
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind, PhysicalChannel
        from vdeter.stage8.shared_medium.radiation import ParentOutcome
        from vdeter.stage8.execution.verdict import TargetEffect

        clock, medium, members = self._group()
        channel = PhysicalChannel.LOWER  # target ROC
        provider, bindings = self._assemble(members, medium, channel=channel)

        packet_hex = bytes(range(54)).hex()
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": packet_hex})

        delivery_before = len(medium.delivery_log)
        submission = provider.submit(action, request_id="poc-1")     # 실 seam 전 구간 구동
        parent = provider.parent_for("poc-1")
        self.assertIsNotNone(parent)

        # 1) 실 frozen PHY 변조: parent waveform sample이 실 Phy11Transmitter 출력과 동일.
        expected = tuple(Phy11Transmitter().encode(bytes(range(54))).samples)
        self.assertEqual(tuple(parent.waveform.samples), expected)

        # 2) 실 worker group-TX + 실 매질 fan-out: C4(→ROC)+C6(→MASS-A) 도달.
        new = medium.delivery_log[delivery_before:]
        paths = sorted(d["path_id"] for d in new)
        self.assertEqual(paths, ["C4", "C6"], paths)
        self.assertTrue(all(d["tx_serial"] == "3515425" for d in new))  # MASS-B 실 serial

        # 3) 실 매질 delivery로 child terminal 확정 → resolve_outcome COMPLETED.
        terminals = stage7_terminal_source(medium, tx_serial="3515425")(action, {}, parent)
        outcome = provider.resolve_outcome("poc-1", terminals)
        self.assertEqual(outcome, ParentOutcome.COMPLETED)

        # 4) 실 수신 evidence로 target effect: 논리 target ROC 도달 → EFFECT_OBSERVED.
        effect, reason = stage7_target_response(
            medium, tx_serial="3515425",
            station_to_endpoint={"ROC": "ROC", "MASS-A": "MASS-A"})(action, {}, parent)
        self.assertEqual(effect, TargetEffect.EFFECT_OBSERVED, reason)

    def test_real_readiness_fail_closed_without_common_lo(self):
        # 수신기 RX LO가 송신 채널에 정합하지 않으면 실 rx_readiness가 RF 전 fail-closed(방사 0).
        from vdeter.stage8.poc_api.errors import PocError
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind
        from apps.stage8_stage7_seams import assemble_stage7_provider

        clock, medium, members = self._group()
        # required 수신기(ROC)의 RX LO를 채널 center와 다른 값으로 강제(common-LO 미정합).
        members["ROC"].endpoint.dependencies.radio.lease.worker.rx_lo_hz = 1  # 명백한 미정합
        provider, _ = assemble_stage7_provider(members)
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        with self.assertRaises(PocError):  # RX_LO_NOT_COMMON → BroadcastNotReady → PocError
            provider.submit(action, request_id="poc-nr")
        # RF 방사 전 거부: 매질 delivery 0.
        self.assertEqual(len([d for d in medium.delivery_log if d.get("tx_serial") == "3515425"]), 0)

    def test_real_arbiter_rejects_when_normal_tx_holds_lane(self):
        # 정상 TX가 lane을 mute(점유)하면 실 arbiter가 예약 거부(정상 TX 우선).
        from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind, PhysicalChannel
        from vdeter.stage8.shared_medium import constants

        clock, medium, members = self._group()
        provider, _ = self._assemble(members, medium, channel=PhysicalChannel.LOWER)
        mass_b = members["MASS-B"].endpoint.dependencies.radio.lease.worker
        mass_b.set_tx_mute(0, True)  # 정상 서비스 TX가 lane 0 점유
        action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                  payload={"packet_hex": bytes(range(54)).hex()})
        with self.assertRaises(PocError) as ctx:
            provider.submit(action, request_id="poc-conflict")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET·Stage7 software group 전용")
class RealSeamsWithRealChildTest(unittest.TestCase):
    """capstone: 실 PoC child(SDK·socket) + SupervisorConnection(provider=**실 Stage 7 seams**)로,
    child가 실행 중 관측에 반응해 제출한 REPLAY action이 실 frozen PHY·실 worker group-TX·실 conducted
    매질을 거쳐 ROC/MASS-A에 도달하고, 실 매질 delivery로 terminal이 COMPLETED가 되는지 확인한다.

    supervisor·provider는 software group과 같은 프로세스(연결 §3.1: supervisor in runtime), 연구자
    PoC 코드만 별도 child. 하드웨어 경계(B210 device I/O)만 fake medium worker. 매질 delivery는
    submit_tx_group에서 동기 발생하므로 별도 시간 pump 없이 terminal/receive evidence를 얻는다.
    """

    _REACTIVE = (
        "class Poc:\n"
        "    def on_start(self, context):\n        return []\n"
        "    def on_event(self, event, context):\n"
        "        if event.type == 'PACKET_OBSERVED':\n"
        "            return [context.replay(target='ROC',\n"
        "                                   source_observation_id=event.body.get('observation_id'),\n"
        "                                   payload={'packet_hex': (bytes(range(54))).hex()})]\n"
        "        return []\n"
        "    def on_finish(self, context):\n        return None\n"
    ).encode("utf-8")

    def setUp(self):
        import tempfile
        self.poc = tempfile.mkdtemp(prefix="poc-rs-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-rs-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-rs-sock-")
        import shutil
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))

    def test_real_child_action_radiates_through_real_stage7_seams(self):
        import json
        from tests.support.stage7 import build_stage7_software_group, start_stage7_group
        from vdeter.stage8._supervisor.child import SubprocessChildSpawner
        from vdeter.stage8._supervisor.clock import RealMonotonicClock
        from vdeter.stage8.execution.child_main import ENV_ENTRYPOINT, ENV_HELLO, ENV_SNAPSHOT_SCRIPT
        from vdeter.stage8.execution.lease import MassBLease
        from vdeter.stage8.execution.runner import WorkloadRunner, WorkloadSpec, observation_step
        from vdeter.stage8.execution.snapshot import create_snapshot
        from vdeter.stage8.execution.verdict import ActionExecution, PocInfrastructure, ScenarioVerdict
        from vdeter.stage8.integration.supervisor_connection import SupervisorConnection
        from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
        from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
        from vdeter.stage8.poc_api.types import PhysicalChannel
        from vdeter.stage8.shared_medium import constants
        from vdeter.stage8.testing.execution_fakes import write_poc_package
        from vdeter.stage8.testing.fakes import make_observation
        from apps.stage8_stage7_seams import (
            assemble_stage7_provider, stage7_target_response, stage7_terminal_source)

        clock, medium, plan, coordinator, members, resolved = build_stage7_software_group()
        start_stage7_group(clock, medium, coordinator, members, resolved)
        center = constants.CHANNEL_EFFECTIVE_CENTER_HZ[PhysicalChannel.LOWER]
        for ep in ("ROC", "MASS-A"):
            members[ep].endpoint.dependencies.radio.lease.worker.rx_lo_hz = center
        provider, _b = assemble_stage7_provider(members)  # 실 Stage 7 seams

        write_poc_package(self.poc, "rs", poc_id="rs", script=self._REACTIVE)
        snap = create_snapshot(self.poc, "rs", dest_dir=os.path.join(self.snap_root, "w1"),
                               scenario_id="SC-VDE-006")
        src = os.path.join(_REPO, "VDETER", "src")
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.stage8.execution.child_main"],
            extra_env={
                ENV_SNAPSHOT_SCRIPT: snap.script_path, ENV_ENTRYPOINT: snap.descriptor.entrypoint,
                ENV_HELLO: json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": "w1",
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", ""),
            })
        conn = SupervisorConnection(
            provider=provider, spawner=spawner, session_id="s1", workload_id="w1",
            run_id="r1", scenario_id="SC-VDE-006", manifest=snap.descriptor,
            server_config=ServerCapabilityConfig(), clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, "w1"))
        # tx terminal·target을 **실 conducted 매질 delivery**에서 공급(합성 아님).
        terminal_source = stage7_terminal_source(medium, tx_serial="3515425")
        target_response = stage7_target_response(
            medium, tx_serial="3515425", station_to_endpoint={"ROC": "ROC", "MASS-A": "MASS-A"})
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id="w1",
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(
                                    provider, terminal_source=terminal_source,
                                    target_response=target_response))
        delivery_before = len(medium.delivery_log)
        obs = make_observation(source_station="ROC", destination_station="MASS-A",
                               device_hw_timestamp_ns=2 * 8192)
        try:
            result = runner.run(WorkloadSpec(steps=[observation_step(obs)]))
        finally:
            if conn.residuals().get("child_alive"):
                conn._terminate_child()
            conn._drain_child_output(); conn._teardown_socket()
        # 실 child의 반응형 REPLAY가 실 seams를 거쳐 실 매질로 방사됨.
        new = medium.delivery_log[delivery_before:]
        paths = sorted(d["path_id"] for d in new if d["tx_serial"] == "3515425")
        self.assertEqual(paths, ["C4", "C6"], (paths, result.poc_infrastructure))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(len(result.attacks), 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_COMPLETED)


@unittest.skipUnless(_LINUX, "real subprocess/socket + Stage7 software group only")
class RealScenarioObservationTest(unittest.TestCase):
    """Real scenario observation -> PoC reaction (real event, real hardware time).

    A real ROC->MASS-A service transfer is driven; MASS-B really overhears it via C3 (real
    conducted medium, real raw_ns). That real observation (source=ROC, real hw-time) is fed to a
    real PoC child, which reacts with REPLAY radiated through the real Stage 7 seams. Per connection
    section 8.3 causality, the reaction is scheduled for the next safe slot after the observation;
    the reactive slot basis is derived from the real observation hardware time (not a counter)."""

    def setUp(self):
        import shutil
        import tempfile
        self.poc = tempfile.mkdtemp(prefix="poc-so-src-")
        os.chmod(self.poc, 0o700)
        self.snap_root = tempfile.mkdtemp(prefix="poc-so-snap-")
        self.sock_root = tempfile.mkdtemp(prefix="poc-so-sock-")
        for d in (self.poc, self.snap_root, self.sock_root):
            self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))

    _REACTIVE = (
        "class Poc:\n"
        "    def on_start(self, context):\n        return []\n"
        "    def on_event(self, event, context):\n"
        "        if event.type == 'PACKET_OBSERVED':\n"
        "            return [context.replay(target='ROC',\n"
        "                                   source_observation_id=event.body.get('observation_id'),\n"
        "                                   payload={'packet_hex': (bytes(range(54))).hex()})]\n"
        "        return []\n"
        "    def on_finish(self, context):\n        return None\n"
    ).encode("utf-8")

    def test_real_roc_transfer_overheard_by_mass_b_drives_poc_reaction(self):
        import json
        from tests.support.stage7 import (build_stage7_software_group, start_stage7_group,
                                          drive_stage7_group, STATION_BY_ENDPOINT)
        from vdeter.common.addressing import Addressed
        from vdeter.common.contracts.service import SubmitTransferRequest
        from vdeter.common.identifiers import PayloadFormat, StationId
        from vdeter.stage8._supervisor.child import SubprocessChildSpawner
        from vdeter.stage8._supervisor.clock import RealMonotonicClock
        from vdeter.stage8.execution.child_main import ENV_ENTRYPOINT, ENV_HELLO, ENV_SNAPSHOT_SCRIPT
        from vdeter.stage8.execution.lease import MassBLease
        from vdeter.stage8.execution.runner import WorkloadRunner, WorkloadSpec, observation_step
        from vdeter.stage8.execution.snapshot import create_snapshot
        from vdeter.stage8.execution.verdict import ActionExecution, PocInfrastructure
        from vdeter.stage8.integration.supervisor_connection import SupervisorConnection
        from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
        from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig
        from vdeter.stage8.poc_api.types import ObservationRecord, PhysicalChannel, TuningState
        from vdeter.stage8.shared_medium import constants
        from vdeter.stage8.testing.execution_fakes import write_poc_package
        from apps.stage8_stage7_seams import (assemble_stage7_provider, stage7_target_response,
                                              stage7_terminal_source)

        clock, medium, plan, coordinator, members, resolved = build_stage7_software_group()
        start_stage7_group(clock, medium, coordinator, members, resolved)

        before = len(medium.delivery_log)
        roc = members["ROC"].endpoint
        opaque = PayloadFormat("test.stage0.opaque")
        res = roc.submit(SubmitTransferRequest(
            destination=Addressed(StationId(STATION_BY_ENDPOINT["MASS-A"])),
            payload_format=opaque, payload=b"scenario-broadcast", ack_required=False))
        self.assertTrue(res.is_ok, getattr(res, "error", None))
        for _ in range(0, 400, 50):
            drive_stage7_group(clock, members, 50)
        overheard = [d for d in medium.delivery_log[before:] if d["path_id"] == "C3"]
        self.assertTrue(overheard, "MASS-B must overhear ROC via C3")
        real_hw_ns = int(overheard[0]["raw_ns"])

        center = constants.CHANNEL_EFFECTIVE_CENTER_HZ[PhysicalChannel.LOWER]
        for ep in ("ROC", "MASS-A"):
            members[ep].endpoint.dependencies.radio.lease.worker.rx_lo_hz = center
        provider, _b = assemble_stage7_provider(members)

        write_poc_package(self.poc, "so", poc_id="so", script=self._REACTIVE)
        snap = create_snapshot(self.poc, "so", dest_dir=os.path.join(self.snap_root, "w1"),
                               scenario_id="SC-VDE-006")
        src = os.path.join(_REPO, "VDETER", "src")
        spawner = SubprocessChildSpawner(
            [sys.executable, "-m", "vdeter.stage8.execution.child_main"],
            extra_env={
                ENV_SNAPSHOT_SCRIPT: snap.script_path, ENV_ENTRYPOINT: snap.descriptor.entrypoint,
                ENV_HELLO: json.dumps(snap.descriptor.as_hello_value()),
                "VDETER_POC_SESSION": "s1", "VDETER_POC_WORKLOAD": "w1",
                "VDETER_POC_RUN_ID": "r1", "VDETER_POC_SCENARIO": "SC-VDE-006",
                "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", ""),
            })
        conn = SupervisorConnection(
            provider=provider, spawner=spawner, session_id="s1", workload_id="w1",
            run_id="r1", scenario_id="SC-VDE-006", manifest=snap.descriptor,
            server_config=ServerCapabilityConfig(), clock=RealMonotonicClock(),
            socket_root=os.path.join(self.sock_root, "w1"))
        runner = WorkloadRunner(conn, MassBLease(), run_id="r1", workload_id="w1",
                                scenario_id="SC-VDE-006", snapshot=snap,
                                tx_executor=ProductionTxExecutor(
                                    provider,
                                    terminal_source=stage7_terminal_source(medium, tx_serial="3515425"),
                                    target_response=stage7_target_response(
                                        medium, tx_serial="3515425",
                                        station_to_endpoint={"ROC": "ROC", "MASS-A": "MASS-A"})))
        observation = ObservationRecord(
            physical_channel=PhysicalChannel.UPPER, tuning_state=TuningState.TUNED_OBSERVER,
            observation_id="obs-real-c3", source_station="ROC", destination_station="MASS-A",
            device_hw_timestamp_ns=real_hw_ns, host_mono_timestamp_ns=real_hw_ns,
            body={"cable": "C3", "digest": overheard[0]["digest"]})
        radiate_before = len(medium.delivery_log)
        try:
            result = runner.run(WorkloadSpec(steps=[observation_step(observation)]))
        finally:
            if conn.residuals().get("child_alive"):
                conn._terminate_child()
            conn._drain_child_output()
            conn._teardown_socket()
        radiated = sorted(d["path_id"] for d in medium.delivery_log[radiate_before:]
                          if d["tx_serial"] == "3515425")
        self.assertEqual(radiated, ["C4", "C6"], (radiated, result.poc_infrastructure))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_COMPLETED)
