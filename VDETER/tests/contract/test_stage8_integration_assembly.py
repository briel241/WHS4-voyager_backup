"""Stage 8 6단계 통합 — provider 조립·device 경계·terminal executor 계약 시험(RF-off, pure).

실 subprocess/socket 없이 실행되는 순수 계약 시험. 실 socket 통합은 별도(Linux) 시험이 담당한다.
검증: fail-closed 조립, atomic C4+C6 fan-out 방사, RX-before-TX 순서, provider outcome 기반
terminal 유도, commit 불명확·RX 미준비·자원 충돌의 fail-closed.
"""

from __future__ import annotations

import os
import sys
import unittest

from vdeter.poc.execution.verdict import ActionExecution, TargetEffect
from vdeter.poc.integration.assembly import DeviceSeams, assemble_provider
from vdeter.poc.integration.rf_off import assemble_rf_off_provider, rf_off_terminal_source
from vdeter.poc.integration.device_boundary import SoftwareGroupTx, GroupTxRadioSink
from vdeter.poc.integration.tx_executor import ProductionTxExecutor
from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.types import ActionDescriptor, ActionKind


def _replay(target="ROC", packet_hex="deadbeefcafe", **kw):
    return ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target=target,
                            payload={"packet_hex": packet_hex}, **kw)


def _result(request_id, submission):
    return {
        "request_id": request_id,
        "admission": "accepted",
        "physical_channel": submission.physical_channel.value,
        "broadcast_parent_id": submission.broadcast_parent_id,
        "transfer_id": submission.transfer_id,
    }


class AssemblyFailClosedTest(unittest.TestCase):
    def test_group_tx_boundary_requires_stage7_methods(self):
        # group_tx가 retune_tx_lo/submit_tx_group을 제공하지 않으면 device 경계 생성이 실패한다.
        with self.assertRaises(ValueError):
            GroupTxRadioSink(group_tx=object())

    def test_assemble_provider_fail_closed_on_missing_seam(self):
        # scheduler 누락 → for_production이 fail-closed(조용한 RF-off 기본값 금지).
        seams = DeviceSeams(group_tx=SoftwareGroupTx(), arbiter=_MinimalArbiter(),
                            scheduler=None, rx_readiness=_ReadyRx(),
                            waveform_factory=_Wf())
        with self.assertRaises(ValueError):
            assemble_provider(seams)


class RfOffFanOutTest(unittest.TestCase):
    def test_accepted_action_radiates_two_lanes_single_group(self):
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="ROC"), request_id="r1")
        self.assertEqual(submission.physical_channel.value, "lower")  # ROC target = lower
        # 단일 group commit, 두 fan-out child(C4+C6) 방사.
        self.assertEqual(seams.group_tx.committed_groups, 1)
        self.assertEqual(seams.group_tx.radiated_children, 2)
        # C4=lane0(→ROC), C6=lane1(→MASS-A): worker burst는 lane 좌표로 방사된다.
        self.assertEqual(set(seams.group_tx.radiated_lanes), {0, 1})

    def test_rx_readiness_queried_before_tx_commit(self):
        provider, seams = assemble_rf_off_provider()
        provider.submit(_replay(target="ROC"), request_id="r1")
        # required receiver(ROC)의 RX window가 질의되고, 그 뒤 group TX가 방사됐다.
        queried = {req.endpoint for req in seams.rx_readiness.requests}
        self.assertIn("ROC", queried)
        self.assertEqual(len(seams.group_tx.submits), 1)
        # 두 lane 공유 TX LO retune(staging) 1회가 submit보다 먼저 기록됐다(순서 불변).
        self.assertEqual(len(seams.group_tx.retunes), 1)
        retune_time = seams.group_tx.retunes[0][1]
        submit_time = seams.group_tx.submits[0][0][2]  # (lane, data, hw_time)
        # P03: timed retune은 TX 시각과 **같은 시각이 아니라 앞선 시각**에 발행한다(기존 radio_adapter
        # 규약: hardware_start - _RETUNE_LEAD_NS). 같은 시각이면 LO 정착 전에 방사하게 된다.
        # RF-off stub 좌표는 실시간이 아니라 TX 시각이 수천 ns라 lead가 0으로 clamp된다(실 경로는 200 ms).
        self.assertLessEqual(retune_time, submit_time)  # staging이 commit보다 앞선다(순서 불변)

    def test_tx_executor_derives_completed_from_provider_outcome(self):
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="ROC"), request_id="r1")
        # R3: production은 성공 terminal을 합성하지 않는다. RF-off 시험은 장비 double의 terminal
        # source를 **명시적으로** 주입해야 TX_COMPLETED가 나온다(미주입 시 TX_UNKNOWN).
        executor = ProductionTxExecutor(provider, terminal_source=rf_off_terminal_source())
        resolution = executor({"action_kind": "REPLAY_PACKET", "target": "ROC"},
                              _result("r1", submission))
        self.assertEqual(resolution.action_execution, ActionExecution.TX_COMPLETED)
        # ROC은 lower에 자연 TUNED → composite decode → PHY 전달됨(INDETERMINATE, 성공 합성 금지).
        self.assertEqual(resolution.target_effect, TargetEffect.INDETERMINATE)

    def test_tx_executor_no_effect_when_target_not_delivered(self):
        # target=MASS-A로 보내되 provider outcome은 COMPLETED이나, ROC row 조회는 없다.
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="MASS-A"), request_id="r1")
        executor = ProductionTxExecutor(provider, terminal_source=rf_off_terminal_source())
        resolution = executor({"action_kind": "REPLAY_PACKET", "target": "MASS-A"},
                              _result("r1", submission))
        # MASS-A는 upper에 자연 TUNED → 전달됨.
        self.assertEqual(resolution.action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(resolution.target_effect, TargetEffect.INDETERMINATE)

    def test_tx_executor_unknown_without_terminal_source(self):
        # R3: terminal_source 미주입이면 accepted·submit 반환만으로 성공을 만들지 않는다.
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="ROC"), request_id="r1")
        executor = ProductionTxExecutor(provider)  # 실 terminal source 없음(production 기본)
        resolution = executor({"action_kind": "REPLAY_PACKET", "target": "ROC"},
                              _result("r1", submission))
        self.assertEqual(resolution.action_execution, ActionExecution.TX_UNKNOWN)
        self.assertEqual(resolution.target_effect, TargetEffect.INDETERMINATE)


class RfOffFailClosedTest(unittest.TestCase):
    def test_commit_failure_is_outcome_unknown_session_inhibit(self):
        provider, seams = assemble_rf_off_provider(fail_on_submit=True)
        with self.assertRaises(PocError) as ctx:
            provider.submit(_replay(target="ROC"), request_id="r1")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
        # commit 불명확: group은 방사 기록으로 남지 않는다(software submit이 예외).
        self.assertEqual(seams.group_tx.committed_groups, 0)

    def test_rx_not_ready_rejects_before_radiation(self):
        provider, seams = assemble_rf_off_provider(not_ready_endpoints=("ROC",))
        with self.assertRaises(PocError) as ctx:
            provider.submit(_replay(target="ROC"), request_id="r1")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        # RF 방사 전 fail-closed: retune·submit 0.
        self.assertEqual(seams.group_tx.submits, [])

    def test_resource_conflict_when_normal_tx_holds_slot(self):
        # 정상 TX가 lower:slot-1을 점유 → PoC action 거부(정상 우선), 방사 0.
        provider, seams = assemble_rf_off_provider(normal_reserved=("lower:slot-1",))
        with self.assertRaises(PocError) as ctx:
            provider.submit(_replay(target="ROC"), request_id="r1")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        self.assertEqual(seams.group_tx.submits, [])

    def test_software_group_tx_matches_real_worker_contract(self):
        # 실 Stage 7 worker signature: retune_tx_lo(target_hz, command_time_ns),
        # submit_tx_group([(lane, data, hardware_time_ns), ...]).
        gtx = SoftwareGroupTx()
        # retune(staging) 전 submit → TX 순서 위반.
        with self.assertRaises(RuntimeError):
            gtx.submit_tx_group([(0, b"d0", 8192), (1, b"d1", 8192)])
        gtx.retune_tx_lo(2_447_237_500, 8192)
        gtx.submit_tx_group([(0, b"d0", 8192), (1, b"d1", 8192)])
        self.assertEqual(gtx.committed_groups, 1)
        self.assertEqual(gtx.radiated_children, 2)
        self.assertEqual(set(gtx.radiated_lanes), {0, 1})
        # 같은 group 중복 submit → exactly-once 위반.
        with self.assertRaises(RuntimeError):
            gtx.submit_tx_group([(0, b"d0", 8192), (1, b"d1", 8192)])
        # 서로 다른 hardware time의 burst 혼재 → 거부.
        gtx2 = SoftwareGroupTx()
        gtx2.retune_tx_lo(2_447_237_500, 8192)
        with self.assertRaises(RuntimeError):
            gtx2.submit_tx_group([(0, b"d0", 8192), (1, b"d1", 9000)])


class RealWorkerInterfaceTest(unittest.TestCase):
    """GroupTxRadioSink가 **실제 Stage 7 worker(double)**의 group TX 계약에 결속됨을 확인한다.

    실 worker interface(retune_tx_lo(target_hz, command_time_ns)·submit_tx_group(bursts))에 그대로
    바인딩되어 방사됨을 보인다(§4: 실제 group TX 인터페이스에 맞춘 prepare/commit 경계). 실 UHD/B210·
    실 RF atomicity·frozen PHY 샘플 포맷·cross-process coordinator 결선은 Stage 6.
    """

    def _real_worker(self):
        _tests = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # VDETER/tests
        if _tests not in sys.path:
            sys.path.insert(0, _tests)
        from support.stage7 import Stage7FakeMedium, Stage7FakeMediumWorker
        medium = Stage7FakeMedium(host_raw_clock=lambda: 0)
        # 세 endpoint worker를 medium에 등록한다(수신자 ROC·MASS-A가 있어야 fan-out이 도달).
        workers = {
            serial: Stage7FakeMediumWorker("serial=%s" % serial, "fc32", "sc16", medium=medium)
            for serial in ("34D03E8", "34D03DD", "3515425")  # ROC, MASS-A, MASS-B
        }
        return medium, workers["3515425"]  # MASS-B = 공격 송신자

    def test_signature_matches_real_worker(self):
        import inspect
        _tests = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _tests not in sys.path:
            sys.path.insert(0, _tests)
        from support.stage7 import Stage7FakeMediumWorker as W
        self.assertEqual(list(inspect.signature(W.retune_tx_lo).parameters)[1:],
                         ["target_hz", "command_time_ns"])
        self.assertEqual(list(inspect.signature(W.submit_tx_group).parameters)[1:],
                         ["bursts"])

    def test_adapter_radiates_through_real_worker_double(self):
        from vdeter.poc.integration.device_boundary import GroupTxRadioSink
        from vdeter.poc.api.types import PhysicalChannel
        from vdeter.poc.shared_medium.radiation import (
            AtomicBroadcast, ReferenceWaveformFactory,
        )
        from vdeter.poc.shared_medium.testing import ReadyReadiness
        medium, worker = self._real_worker()
        sink = GroupTxRadioSink(group_tx=worker)
        broadcast = AtomicBroadcast(radio_sink=sink, rx_readiness=ReadyReadiness(),
                                    waveform_factory=ReferenceWaveformFactory())
        parent = broadcast.build_parent(
            source_station="MASS-B", channel=PhysicalChannel.LOWER, payload=b"deadbeef",
            scheduled_sample=8192, scheduled_hw_timestamp_ns=8192, slot_key="slot-1",
            addressed_destination="ROC")
        admission = sink.prepare_group(group_id=parent.parent_id, children=parent.children)
        sink.commit_group(admission)
        # adapter가 실 worker의 retune_tx_lo(선택 채널)와 submit_tx_group(2 burst)를 호출했다.
        self.assertEqual(worker.tx_lo_hz, 2_447_237_500)  # lower 채널 TX LO
        transmitted = len(medium.delivery_log) + len(medium.tune_mismatch_log)
        self.assertEqual(transmitted, 2)  # 두 fan-out child burst가 worker→medium으로 방사


# --- 최소 seam double (fail-closed 조립 시험 전용) ---------------------------
class _MinimalArbiter:
    def reserve(self, *, resource_key, owner):
        return True

    def release(self, *, resource_key, owner):
        pass


class _ReadyRx:
    def window_ready(self, request):
        from vdeter.poc.shared_medium.radiation import ReadinessDecision
        return ReadinessDecision(ready=True, reason="READY")


class _Wf:
    def encode(self, *, source_station, channel, payload):
        from vdeter.poc.shared_medium.radiation import ReferenceWaveformFactory
        return ReferenceWaveformFactory().encode(
            source_station=source_station, channel=channel, payload=payload)


if __name__ == "__main__":
    unittest.main()
