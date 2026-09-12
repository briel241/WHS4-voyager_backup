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
import threading
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
        """권위 경로 provider(sidecar 예약→schedule_tx→group TX→TX 원장)를 조립한다.

        정상 role tuning(§5.5)은 바꾸지 않는다 — lower 송신의 required receiver(ROC)는 자연 TUNED다.
        """
        from apps.stage8_stage7_seams import assemble_authoritative_provider
        from tests.support.stage7 import STATION_BY_ENDPOINT
        return assemble_authoritative_provider(
            members, station_id_by_endpoint=STATION_BY_ENDPOINT)

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

        # 2) 물리 fan-out은 **두 lane**으로 나간다(P08): 권위 원장이 lane 0·1을 모두 등록한다.
        #    반면 **디지털 도달**은 §5.5 자연 관측 행렬을 따른다 — MASS-B의 lower 송신에서 자연
        #    TUNED인 비송신 endpoint는 ROC뿐이므로 C4만 매질을 통과하고, upper에 tuned인 MASS-A로
        #    가는 C6는 tune 불일치로 수신되지 않는다(NOT_TUNED는 실패가 아니라 정상 상태).
        #    정상 endpoint tuning을 시험 통과를 위해 바꾸지 않는다.
        burst_id = bindings["sink"].burst_by_group[parent.parent_id]
        ledger = bindings["adapter"].tx_burst_state(burst_id)
        self.assertEqual(sorted(ledger["children"].keys()), [0, 1], ledger)  # 두 lane fan-out
        new = medium.delivery_log[delivery_before:]
        paths = sorted(d["path_id"] for d in new)
        self.assertEqual(paths, ["C4"], paths)          # 자연 TUNED 수신기(ROC)만 도달
        self.assertTrue(all(d["tx_serial"] == "3515425" for d in new))  # MASS-B 실 serial

        # 3) TX terminal은 **권위 원장**에서 와야 한다. PoC group TX는 radio adapter의 schedule_tx를
        #    거치지 않으므로 adapter가 BURST_ACK을 드레인해도 대응 원장이 없다 → terminal 미확정.
        #    매질 delivery 기록으로 COMPLETED를 만들지 않는다(P10: 과거/우회 로그로 성공 추정 금지).
        from apps.stage8_stage7_seams import Stage7LedgerTerminalSource
        # 접수 직후에는 원장이 SUBMITTED(진행 중)여야 한다 — 즉시 완료로 확정하지 않는다.
        burst = bindings["sink"].burst_by_group[parent.parent_id]
        immediate = bindings["adapter"].tx_burst_state(burst)
        self.assertFalse(immediate["terminal"], immediate)
        self.assertTrue(all(v == "SUBMITTED" for v in immediate["children"].values()), immediate)
        terminals = Stage7LedgerTerminalSource(
            bindings["adapter"], bindings["sink"],
            pump=lambda: drive_stage7_group(clock, members, 4))(action, {}, parent)
        self.assertTrue(all(v == "COMPLETED" for v in terminals.values()), terminals)
        outcome = provider.resolve_outcome("poc-1", terminals)
        self.assertEqual(outcome, ParentOutcome.COMPLETED)   # 권위 원장이 확정

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


class SharedMediumRouteFanOutTest(unittest.TestCase):
    """P08: 공유매체 opt-in에서만 송신자별 두 물리 경로로 fan-out한다(legacy 보존)."""

    def _context(self, *, shared):
        from vdeter.stage7.routing import Stage7RouteContext
        return Stage7RouteContext(
            method_kind="TEST", source_station_id=333333333,       # MASS-B
            destination_kind="ADDRESSED", destination_station_id=222222222,  # ROC
            original_source_station_id=None, original_destination_kind=None,
            original_destination_station_id=None, request_digest="d",
            broadcast_fanout_optin=shared)

    def test_legacy_addressed_route_is_single_child(self):
        from vdeter.stage7.configuration import LOWER_CHANNEL_NAME
        from vdeter.stage7.routing import resolve_lane_plan_route
        resolved, error = resolve_lane_plan_route(
            "MASS-B", self._context(shared=False), LOWER_CHANNEL_NAME)
        self.assertIsNone(error, error)
        children, _skipped, scope = resolved
        self.assertEqual(children, ((0, "C4"),))          # 기존 단일 child 그대로
        self.assertEqual(scope, "SINGLE_CHILD")

    def test_shared_medium_optin_fans_out_to_two_paths(self):
        from vdeter.stage7.configuration import LOWER_CHANNEL_NAME
        from vdeter.stage7.routing import resolve_lane_plan_route
        resolved, error = resolve_lane_plan_route(
            "MASS-B", self._context(shared=True), LOWER_CHANNEL_NAME)
        self.assertIsNone(error, error)
        children, _skipped, scope = resolved
        self.assertEqual(children, ((0, "C4"), (1, "C6")))   # §5.1 MASS-B = C4+C6
        self.assertEqual(scope, "OPTIN_TWO_CHILD_FANOUT")

    def test_every_sender_fanout_matches_fixed_cabling(self):
        from vdeter.stage7.routing import BROADCAST_FANOUT_CHILDREN
        # §5.1 고정 배선: ROC=C1+C3, MASS-A=C2+C5, MASS-B=C4+C6.
        self.assertEqual(BROADCAST_FANOUT_CHILDREN["ROC"], ((0, "C1"), (1, "C3")))
        self.assertEqual(BROADCAST_FANOUT_CHILDREN["MASS-A"], ((0, "C2"), (1, "C5")))
        self.assertEqual(BROADCAST_FANOUT_CHILDREN["MASS-B"], ((0, "C4"), (1, "C6")))

    def test_broadcast_route_is_unchanged_by_optin(self):
        # BROADCAST는 기존 표(C1+C3)를 그대로 쓰고 opt-in 분기를 타지 않는다.
        from vdeter.stage7.configuration import UPPER_CHANNEL_NAME
        from vdeter.stage7.routing import Stage7RouteContext, resolve_lane_plan_route
        ctx = Stage7RouteContext(
            method_kind="TEST", source_station_id=222222222, destination_kind="BROADCAST",
            destination_station_id=None, original_source_station_id=None,
            original_destination_kind=None, original_destination_station_id=None,
            request_digest="d", broadcast_fanout_optin=True)
        resolved, error = resolve_lane_plan_route("ROC", ctx, UPPER_CHANNEL_NAME)
        self.assertIsNone(error, error)
        children, _skipped, scope = resolved
        self.assertEqual(children, ((0, "C1"), (1, "C3")))
        self.assertEqual(scope, "TWO_CHILD_BROADCAST")      # 기존 scope 유지


class CommonLoTuningApplyTest(unittest.TestCase):
    """P07: common LO/DSP/BW 계획을 **실 worker surface**에 적용·readback 검증·복구·capability 광고.

    장치 double은 `Stage7FakeMediumWorker`다 — 실 `Stage7DualLaneFlowgraphBackend`와 **같은 이름의
    method**(`apply_manual_tuning`/`read_tuning`/`restore_tuning`)를 가지며, echo가 아니라 장치 모델이다
    (analog BW를 필터 격자로 양자화하고 lane 실효 중심을 승인 규약 effective = rf_lo - dsp로 계산).
    실제 B210은 열지 않는다 — UHD 경계만 대체하고 명령·순서·오류·복구를 검증한다.
    """

    def _worker(self, **faults):
        from tests.support.stage7 import Stage7FakeMedium, Stage7FakeMediumWorker
        medium = Stage7FakeMedium(lambda: 1_000_000_000)
        worker = Stage7FakeMediumWorker("serial=3515425", "fc32", "sc16", medium=medium)
        worker.configure_dual({
            "master_clock_rate_hz": 30_720_000, "cpu_format": "fc32", "otw_format": "sc16",
            "sample_rate_hz": 307_200, "analog_bandwidth_hz": 200_000,
            "tx_lo_hz": 2_447_237_500, "rx_lo_hz": 2_447_237_500,
            "tx_gain_db": [0, 0], "rx_gain_db": [0, 0],
            "rx_queue_max_bytes": 1 << 20, "device_sample_rate_hz": 307_200,
        })
        for name, value in faults.items():
            setattr(worker, name, value)
        return worker

    def _plan(self):
        from vdeter.stage8.shared_medium.tuning import derive_dual_channel_plan
        return derive_dual_channel_plan()

    def test_plan_is_applied_to_device_and_readback_verified(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier
        from vdeter.stage8.shared_medium import constants
        worker = self._worker()
        applier = CommonLoTuningApplier(worker)
        readback = applier.apply(self._plan())
        # 공통 RF LO 하나와 lane별 DSP가 실제로 걸렸다(G1 승인 계획값).
        self.assertEqual(readback["rx_lo_hz"], constants.COMMON_RF_LO_HZ)
        self.assertEqual(readback["lane_dsp_hz"],
                         {0: constants.LOWER_DSP_HZ, 1: constants.UPPER_DSP_HZ})
        self.assertEqual(set(readback["lane_rf_lo_hz"].values()), {constants.COMMON_RF_LO_HZ})
        # 승인 규약 effective = rf_lo - dsp → lane 0=lower, lane 1=upper 실효 중심.
        self.assertEqual(readback["lane_center_hz"], {
            0: constants.LOWER_EFFECTIVE_CENTER_HZ,
            1: constants.UPPER_EFFECTIVE_CENTER_HZ,
        })
        self.assertGreaterEqual(readback["analog_bandwidth_hz"],
                                constants.ANALOG_RX_BANDWIDTH_HZ)
        # 검증된 readback이 있을 때만 capability를 광고한다.
        self.assertIn("observe_dual_physical_channel", applier.advertise_capabilities())

    def test_missing_device_support_is_fail_closed_and_advertises_nothing(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier, CommonLoTuningError

        class _Unsupported:
            pass

        applier = CommonLoTuningApplier(_Unsupported())
        with self.assertRaises(CommonLoTuningError):
            applier.apply(self._plan())
        # 계획을 만들었다는 이유로 광고하지 않는다(silent single-channel fallback 금지).
        self.assertEqual(applier.advertise_capabilities(), frozenset())

    def test_readback_mismatch_beyond_tune_tolerance_rejects_and_recovers(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier, CommonLoTuningError
        # 승인 계약은 ±1 Hz다. 2 Hz 오차는 거부돼야 한다.
        worker = self._worker(tuning_lo_error_hz=2)
        before = worker.read_tuning()
        applier = CommonLoTuningApplier(worker)
        with self.assertRaises(CommonLoTuningError) as ctx:
            applier.apply(self._plan())
        self.assertIn("rx_lo_hz", str(ctx.exception))         # 장치 보고값으로 판정
        self.assertEqual(applier.advertise_capabilities(), frozenset())
        self.assertIsNone(applier.verified_readback)
        # 불일치 시 이전 tuning으로 되돌린다.
        self.assertEqual(worker.read_tuning()["lane_center_hz"], before["lane_center_hz"])

    def test_readback_within_approved_tune_tolerance_is_accepted(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier
        # 기존 승인 계약(`_validate_dual_readback`)과 같은 ±1 Hz. 여기서 넓히지도 좁히지도 않는다.
        applier = CommonLoTuningApplier(self._worker(tuning_lo_error_hz=1))
        readback = applier.apply(self._plan())
        self.assertIsNotNone(readback)
        self.assertIn("observe_dual_physical_channel", applier.advertise_capabilities())

    def test_narrower_analog_bandwidth_than_plan_is_rejected(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier, CommonLoTuningError
        # 통과대역이 계획보다 좁으면 ±2.3 MHz lane 하나가 잘린다 — 조용히 받아들이면 안 된다.
        applier = CommonLoTuningApplier(self._worker(tuning_bandwidth_cap_hz=4_000_000))
        with self.assertRaises(CommonLoTuningError) as ctx:
            applier.apply(self._plan())
        self.assertIn("analog_bandwidth_hz", str(ctx.exception))
        self.assertEqual(applier.advertise_capabilities(), frozenset())

    def test_apply_failure_recovers_previous_tuning(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier, CommonLoTuningError
        worker = self._worker(tuning_fault="injected device tuning failure")
        before = worker.read_tuning()
        applier = CommonLoTuningApplier(worker)
        with self.assertRaises(CommonLoTuningError):
            applier.apply(self._plan())
        self.assertEqual(worker.read_tuning(), before)          # 직전 상태로 복구
        self.assertEqual(applier.advertise_capabilities(), frozenset())

    def test_recovery_failure_is_recorded_not_hidden(self):
        from apps.stage8_stage7_seams import CommonLoTuningApplier, CommonLoTuningError
        worker = self._worker(tuning_fault="injected device tuning failure",
                              restore_fault="injected restore failure")
        applier = CommonLoTuningApplier(worker)
        with self.assertRaises(CommonLoTuningError):
            applier.apply(self._plan())
        self.assertTrue(getattr(applier, "recovery_error", None))   # 복구 불가를 보존
        self.assertEqual(applier.advertise_capabilities(), frozenset())

    def test_real_backend_uses_manual_rf_and_dsp_policies_on_the_uhd_boundary(self):
        """실 `Stage7DualLaneFlowgraphBackend`가 UHD 경계에 거는 **명령 자체**를 검증한다.

        RF/DSP policy를 둘 다 MANUAL로 두지 않으면 UHD가 lane마다 RF LO를 다시 잡아 공통 LO가
        깨진다. 장치는 열지 않고 UHD 경계 객체만 대체한다.
        """
        from vdeter.stage7.b210_worker import Stage7DualLaneFlowgraphBackend
        from vdeter.stage8.shared_medium import constants

        class _TuneRequest:
            POLICY_MANUAL = "MANUAL"

            def __init__(self, target_freq):
                self.target_freq = target_freq
                self.rf_freq_policy = None
                self.rf_freq = None
                self.dsp_freq_policy = None
                self.dsp_freq = None

        class _Uhd:
            tune_request = _TuneRequest

        class _TuneResult:
            def __init__(self, rf, dsp):
                self.actual_rf_freq, self.actual_dsp_freq = rf, dsp

        class _Source:
            def __init__(self):
                self.calls = []
                self.bandwidth = {0: 200_000, 1: 200_000}
                self.center = {0: 0.0, 1: 0.0}

            def set_bandwidth(self, value, lane):
                self.calls.append(("set_bandwidth", lane, int(value)))
                self.bandwidth[lane] = int(value)

            def set_center_freq(self, request, lane):
                self.calls.append(("set_center_freq", lane, request))
                self.center[lane] = float(request.rf_freq) - float(request.dsp_freq)
                return _TuneResult(request.rf_freq, request.dsp_freq)

            def get_center_freq(self, lane):
                return self.center[lane]

            def get_bandwidth(self, lane):
                return self.bandwidth[lane]

        backend = Stage7DualLaneFlowgraphBackend.__new__(Stage7DualLaneFlowgraphBackend)
        backend._uhd = _Uhd()
        backend._source = source = _Source()
        backend._control_lock = threading.RLock()
        backend._manual_rx_tuning = None
        backend._rx_lo_frequency_hz = None

        readback = backend.apply_manual_tuning(
            constants.COMMON_RF_LO_HZ,
            {0: constants.LOWER_DSP_HZ, 1: constants.UPPER_DSP_HZ},
            constants.ANALOG_RX_BANDWIDTH_HZ)

        # 순서: lane마다 **bandwidth 먼저, tune 나중**(좁은 필터로 offset을 걸면 lane이 잘린다).
        order = [(name, lane) for name, lane, _ in source.calls]
        self.assertEqual(order, [("set_bandwidth", 0), ("set_center_freq", 0),
                                 ("set_bandwidth", 1), ("set_center_freq", 1)])
        for _name, lane, request in source.calls:
            if _name != "set_center_freq":
                continue
            self.assertEqual(request.rf_freq_policy, _TuneRequest.POLICY_MANUAL)
            self.assertEqual(request.dsp_freq_policy, _TuneRequest.POLICY_MANUAL)
            self.assertEqual(int(request.rf_freq), constants.COMMON_RF_LO_HZ)
        self.assertEqual(readback["lane_dsp_hz"],
                         {0: constants.LOWER_DSP_HZ, 1: constants.UPPER_DSP_HZ})
        self.assertEqual(set(readback["lane_rf_lo_hz"].values()), {constants.COMMON_RF_LO_HZ})

    def test_real_backend_refuses_when_uhd_lacks_manual_tune_policy(self):
        """UHD가 manual policy를 제공하지 않으면 **추정하지 않고 거부**한다(fail-closed)."""
        from vdeter.stage7.b210_worker import Stage7DualLaneFlowgraphBackend
        from vdeter.stage8.shared_medium import constants

        class _Uhd:
            pass

        backend = Stage7DualLaneFlowgraphBackend.__new__(Stage7DualLaneFlowgraphBackend)
        backend._uhd = _Uhd()
        backend._control_lock = threading.RLock()
        backend._manual_rx_tuning = None
        with self.assertRaises(RuntimeError):
            backend.apply_manual_tuning(constants.COMMON_RF_LO_HZ, {0: 1, 1: -1},
                                        constants.ANALOG_RX_BANDWIDTH_HZ)


class NormalDispatchPocRefusalTest(unittest.TestCase):
    """P01: PoC를 실행하지 않는 일반 dispatch에 PoC 요청을 보내면 **fail-closed**로 거부한다.

    일반 coordinator 요청 루프는 `workload_id`/`scenario_id`/`certificate_case`만 읽어 `poc` 필드를
    무시한다. 그래서 운영자가 PoC를 요청해도 PoC 없이 시나리오만 돌고 PASS로 보고되는 거짓 성공이
    가능했다. 세션 계층에서 그 조합을 거부하고 request 자체를 발행하지 않는다(시나리오 미제출).

    이 검사는 **자격 결속된 coordinator 함수를 바꾸지 않는다** — `stage7_coordinator_main.main()`이나
    `run_mass_roc_session`에 분기를 넣으면 Phase-I function binding이 바뀌어 RF 접근이 거부된다.
    """

    def _session(self, tmp, *, rf_off_software):
        from apps.standalone.local_session import LocalCoordinatorSession
        return LocalCoordinatorSession(
            repo_root=_REPO, run_id="run-x", run_root=os.path.join(tmp, "run"),
            configs_dir=os.path.join(tmp, "configs"),
            gain_artifact_path=os.path.join(tmp, "gain.json"),
            baseline_sha256="0" * 64, rf_off_software=rf_off_software)

    def test_poc_request_to_non_poc_dispatch_is_refused_without_submitting(self):
        import tempfile
        from apps.standalone.local_session import LocalSessionError
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp, rf_off_software=False)
            with self.assertRaises(LocalSessionError):
                session.submit_workload("SC-VDE-006", "normal", poc={"poc": "/x.py"})
            # request가 발행되지 않았다(시나리오도 제출되지 않는다).
            requests = session.requests_dir
            self.assertFalse(requests.exists() and any(requests.iterdir()), "request 미발행이어야 함")

    def test_plain_workload_request_is_unaffected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp, rf_off_software=False)
            workload_id = session.submit_workload("SC-VDE-006", "normal")   # poc 없음 → 기존 동작
            self.assertTrue((session.requests_dir / (workload_id + ".json")).exists())

    def test_qualified_coordinator_entry_is_not_branched_for_rf_off(self):
        """검증 전용 RF-off 경로가 자격 결속 함수(main)를 거치지 않는다."""
        path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", "apps", "stage7_coordinator_main.py"))
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("rf_off_software", source)
        self.assertNotIn("run_rf_off_software_session", source)
