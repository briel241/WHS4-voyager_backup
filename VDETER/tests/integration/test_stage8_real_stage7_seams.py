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
        from tests.support.stage7 import (build_stage7_software_group, drive_stage7_group,
                                          start_stage7_group)
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
        from tests.support.stage7 import drive_stage7_group
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

        # 1) 실 frozen PHY 변조 + 권위 native→device 변환(P05): parent waveform은 실 Phy11Transmitter
        #    native 출력(2,048 @76.8 kS/s)을 기존 정상 경로와 같은 `interpolate_slot`으로 4배 보간한
        #    device 표본(8,192 @307.2 kS/s = 26.666… ms)이어야 한다. native를 그대로 실으면 device
        #    rate에서 6.666… ms짜리 잘린 burst가 된다.
        from vdeter.phy import resampling
        native = tuple(Phy11Transmitter().encode(bytes(range(54))).samples)
        self.assertEqual(len(native), resampling.NATIVE_SLOT_SAMPLES)
        expected = resampling.interpolate_slot(native)
        self.assertEqual(tuple(parent.waveform.samples), tuple(expected))
        self.assertEqual(len(parent.waveform.samples), resampling.DEVICE_SLOT_SAMPLES)

        # 2) 실 worker group-TX + 실 매질 fan-out: C4(→ROC)+C6(→MASS-A) 도달.
        new = medium.delivery_log[delivery_before:]
        paths = sorted(d["path_id"] for d in new)
        self.assertEqual(paths, ["C4", "C6"], paths)
        self.assertTrue(all(d["tx_serial"] == "3515425" for d in new))  # MASS-B 실 serial

        # 3) TX terminal은 **권위 원장**에서 와야 한다. PoC group TX는 radio adapter의 schedule_tx를
        #    거치지 않으므로 adapter가 BURST_ACK을 드레인해도 대응 원장이 없다 → terminal 미확정.
        #    매질 delivery 기록으로 COMPLETED를 만들지 않는다(P10: 과거/우회 로그로 성공 추정 금지).
        terminals = stage7_terminal_source(members["MASS-B"].endpoint.dependencies.radio.lease.worker,
                                   pump=lambda: drive_stage7_group(clock, members, 4))(action, {}, parent)
        self.assertTrue(all(v == "UNKNOWN" for v in terminals.values()), terminals)
        outcome = provider.resolve_outcome("poc-1", terminals)
        self.assertEqual(outcome, ParentOutcome.OUTCOME_UNKNOWN)   # 성공 추정 금지(SESSION_INHIBIT 경로)

        # 4) target effect는 **application 수신 증거**로만 판정한다. 물리 도달은 효과가 아니다.
        #    합성 packet은 정상 복호·주소 수락을 통과하지 못하므로 INDETERMINATE가 정직한 결과다.
        effect, reason = stage7_target_response(members)(action, {}, parent)
        self.assertEqual(effect, TargetEffect.INDETERMINATE, reason)

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
        from tests.support.stage7 import (build_stage7_software_group, drive_stage7_group,
                                          start_stage7_group)
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
        terminal_source = stage7_terminal_source(members["MASS-B"].endpoint.dependencies.radio.lease.worker,
                                   pump=lambda: drive_stage7_group(clock, members, 4))
        target_response = stage7_target_response(members)
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
        # 실 방사는 일어나지만 TX terminal 권위 경로 미결속 → TX_UNKNOWN·ABORTED(P10 미완, 정직 표기).
        self.assertEqual(paths, ["C4", "C6"], (paths, result.poc_infrastructure))
        self.assertEqual(len(result.attacks), 1)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)


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
                                    terminal_source=stage7_terminal_source(members["MASS-B"].endpoint.dependencies.radio.lease.worker,
                                   pump=lambda: drive_stage7_group(clock, members, 4)),
                                    target_response=stage7_target_response(members)))
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
        # 실 방사는 일어난다(실 worker→실 매질 C4+C6). 그러나 TX terminal은 권위 원장에서 오지 않아
        # 미확정이므로 성공으로 추정하지 않는다 → TX_UNKNOWN·ABORTED(SESSION_INHIBIT). P10 미완의
        # 정직한 표현이다(이전의 COMPLETED는 매질 delivery 로그 휴리스틱 산물이었다).
        self.assertEqual(radiated, ["C4", "C6"], (radiated, result.poc_infrastructure))
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.TX_UNKNOWN)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)


class RealUnitsAndConversionTest(unittest.TestCase):
    """P03(시간 좌표·lead·retune)·P05(native→device 변환) 회귀 고정. 장치·socket 불필요."""

    def _scheduler(self, now_ns, *, tx_lo_hz=None):
        from types import SimpleNamespace
        from apps.stage8_stage7_seams import Stage7HwTimeScheduler
        worker = SimpleNamespace(hardware_time_ns=lambda: now_ns, tx_lo_hz=tx_lo_hz)
        return Stage7HwTimeScheduler(worker)

    def _action(self):
        from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind
        return ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                                payload={"packet_hex": bytes(range(54)).hex()})

    def test_p03_slot_uses_real_device_slot_duration_not_sample_count(self):
        # 수정 전: hardware ns를 8192(sample 수)로 나눠 lead가 5,632 ns였다.
        # 수정 후: slot 경계는 8192/307,200 s = 26,666,666 ns.
        from apps.stage8_stage7_seams import DEVICE_SLOT_NS, hw_ns_to_slot, slot_to_hw_ns
        from vdeter.phy import resampling
        self.assertEqual(DEVICE_SLOT_NS,
                         resampling.DEVICE_SLOT_SAMPLES * 10**9 // resampling.DEVICE_SAMPLE_RATE_HZ)
        self.assertEqual(DEVICE_SLOT_NS, 26_666_666)
        # 정수 정밀 환산: slot→ns→slot 왕복이 어긋나지 않는다(누적 내림 오차 없음).
        for slot in (0, 1, 45, 1000, 123456):
            self.assertEqual(hw_ns_to_slot(slot_to_hw_ns(slot)), slot)

    def test_p03_sample_index_and_hardware_ns_are_distinct_units(self):
        slot = self._scheduler(1_000_000_000).resolve(self._action(), request_id="u1")
        from vdeter.phy import resampling
        from apps.stage8_stage7_seams import slot_to_hw_ns
        index = slot.scheduled_sample // resampling.DEVICE_SLOT_SAMPLES
        self.assertNotEqual(slot.scheduled_sample, slot.scheduled_hw_timestamp_ns)
        self.assertEqual(slot.scheduled_sample, index * resampling.DEVICE_SLOT_SAMPLES)
        self.assertEqual(slot.scheduled_hw_timestamp_ns, slot_to_hw_ns(index))

    def test_p03_lead_meets_authoritative_minimum(self):
        from apps.stage8_stage7_seams import _RETUNE_LEAD_NS, _TX_LEAD_NS
        from vdeter.stage8.shared_medium import constants
        from vdeter.stage8.poc_api.types import PhysicalChannel
        now = 1_000_000_000
        # TX LO 미확정 → 보수적으로 retune lead(200 ms) 요구.
        slot = self._scheduler(now).resolve(self._action(), request_id="u2")
        self.assertGreaterEqual(slot.scheduled_hw_timestamp_ns - now, _RETUNE_LEAD_NS)
        # 이미 대상 채널에 tune돼 있으면 retune 불필요 → 기본 TX lead(100 ms) 이상.
        center = constants.CHANNEL_EFFECTIVE_CENTER_HZ[PhysicalChannel.LOWER]
        from types import SimpleNamespace
        from apps.stage8_stage7_seams import Stage7HwTimeScheduler
        worker = SimpleNamespace(hardware_time_ns=lambda: now, tx_lo_hz=center)
        sched = Stage7HwTimeScheduler(worker, current_channel=PhysicalChannel.LOWER)
        slot2 = sched.resolve(self._action(), request_id="u3")
        self.assertGreaterEqual(slot2.scheduled_hw_timestamp_ns - now, _TX_LEAD_NS)
        # lead가 5,632 ns 같은 값으로 되돌아가지 않는다(회귀 고정).
        self.assertGreater(slot2.scheduled_hw_timestamp_ns - now, 1_000_000)

    def test_p03_retune_is_issued_before_tx_not_at_tx_time(self):
        from vdeter.stage8.integration.device_boundary import (RETUNE_LEAD_NS,
                                                               Stage7GroupTxRadioSink)
        from vdeter.stage8.poc_api.types import PhysicalChannel
        from vdeter.stage8.shared_medium.radiation import ChildPath

        class _Spy:
            def __init__(self):
                self.retunes = []
                self.submits = []

            def retune_tx_lo(self, target_hz, command_time_ns):
                self.retunes.append((target_hz, command_time_ns))

            def submit_tx_group(self, bursts):
                self.submits.append(tuple(bursts))

        spy = _Spy()
        tx_ns = 5_000_000_000  # 실시간 좌표(실 scheduler 산출 범위)
        samples = (0j,) * 8
        children = tuple(
            ChildPath(child_id=f"p:{cable}", parent_id="p", transfer_id="t", cable=cable,
                      tx_station="MASS-B", tx_lane=lane, rx_station=rx, rx_lane=1,
                      channel=PhysicalChannel.LOWER, payload_digest="d", waveform_sha256="w",
                      scheduled_sample=0, scheduled_hw_timestamp_ns=tx_ns, samples=samples)
            for cable, lane, rx in (("C4", 0, "ROC"), ("C6", 1, "MASS-A")))
        sink = Stage7GroupTxRadioSink(group_tx=spy)
        sink.commit_group(sink.prepare_group(group_id="p", children=children))
        self.assertEqual(spy.retunes[0][1], tx_ns - RETUNE_LEAD_NS)   # TX보다 200 ms 앞섬
        self.assertLess(spy.retunes[0][1], spy.submits[0][0][2])       # staging < 방사

    def test_p05_waveform_is_device_rate_with_authoritative_conversion(self):
        from apps.stage8_stage7_seams import Link11WaveformFactory
        from vdeter.phy import resampling
        from vdeter.phy.transmitter import Phy11Transmitter
        from vdeter.stage8.poc_api.types import PhysicalChannel
        payload = bytes(range(54))
        burst = Link11WaveformFactory().encode(source_station="MASS-B",
                                               channel=PhysicalChannel.LOWER, payload=payload)
        native = tuple(Phy11Transmitter().encode(payload).samples)
        self.assertEqual(len(native), resampling.NATIVE_SLOT_SAMPLES)          # 2,048 native
        self.assertEqual(len(burst.samples), resampling.DEVICE_SLOT_SAMPLES)   # 8,192 device
        self.assertEqual(tuple(burst.samples), tuple(resampling.interpolate_slot(native)))
        # device rate에서 정확히 한 slot 길이(26.666… ms). 보간 없이 실으면 6.666… ms였다.
        duration_ms = len(burst.samples) / resampling.DEVICE_SAMPLE_RATE_HZ * 1000
        self.assertAlmostEqual(duration_ms, 26.6667, places=3)

    def test_p05_oversized_payload_is_rejected_not_truncated(self):
        from apps.stage8_stage7_seams import Link11WaveformFactory
        from vdeter.stage8.poc_api.types import PhysicalChannel
        with self.assertRaises(ValueError):
            Link11WaveformFactory().encode(source_station="MASS-B",
                                           channel=PhysicalChannel.LOWER, payload=bytes(200))
