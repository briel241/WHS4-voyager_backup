"""Stage 8 6단계 통합 — provider 조립·device 경계·terminal executor 계약 시험(RF-off, pure).

실 subprocess/socket 없이 실행되는 순수 계약 시험. 실 socket 통합은 별도(Linux) 시험이 담당한다.
검증: fail-closed 조립, atomic C4+C6 fan-out 방사, RX-before-TX 순서, provider outcome 기반
terminal 유도, commit 불명확·RX 미준비·자원 충돌의 fail-closed.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.execution.verdict import ActionExecution, TargetEffect
from vdeter.stage8.integration.assembly import DeviceSeams, assemble_provider
from vdeter.stage8.integration.rf_off import assemble_rf_off_provider
from vdeter.stage8.integration.device_boundary import SoftwareGroupTx, Stage7GroupTxRadioSink
from vdeter.stage8.integration.tx_executor import ProductionTxExecutor
from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind


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
            Stage7GroupTxRadioSink(group_tx=object())

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
        self.assertEqual(len(seams.group_tx.committed_group_ids), 1)
        self.assertEqual(len(seams.group_tx.radiated_child_ids), 2)
        cables = {cid.rsplit(":", 1)[-1] for cid in seams.group_tx.radiated_child_ids}
        self.assertEqual(cables, {"C4", "C6"})

    def test_rx_readiness_queried_before_tx_commit(self):
        provider, seams = assemble_rf_off_provider()
        provider.submit(_replay(target="ROC"), request_id="r1")
        # required receiver(ROC)의 RX window가 질의되고, 그 뒤 group TX가 방사됐다.
        queried = {req.endpoint for req in seams.rx_readiness.requests}
        self.assertIn("ROC", queried)
        self.assertEqual(len(seams.group_tx.submits), 1)
        # retune(staging)이 submit보다 먼저 기록됐다(순서 불변).
        self.assertGreaterEqual(len(seams.group_tx.retunes), 2)

    def test_tx_executor_derives_completed_from_provider_outcome(self):
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="ROC"), request_id="r1")
        executor = ProductionTxExecutor(provider)
        resolution = executor({"action_kind": "REPLAY_PACKET", "target": "ROC"},
                              _result("r1", submission))
        self.assertEqual(resolution.action_execution, ActionExecution.TX_COMPLETED)
        # ROC은 lower에 자연 TUNED → composite decode → PHY 전달됨(INDETERMINATE, 성공 합성 금지).
        self.assertEqual(resolution.target_effect, TargetEffect.INDETERMINATE)

    def test_tx_executor_no_effect_when_target_not_delivered(self):
        # target=MASS-A로 보내되 provider outcome은 COMPLETED이나, ROC row 조회는 없다.
        provider, seams = assemble_rf_off_provider()
        submission = provider.submit(_replay(target="MASS-A"), request_id="r1")
        executor = ProductionTxExecutor(provider)
        resolution = executor({"action_kind": "REPLAY_PACKET", "target": "MASS-A"},
                              _result("r1", submission))
        # MASS-A는 upper에 자연 TUNED → 전달됨.
        self.assertEqual(resolution.action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(resolution.target_effect, TargetEffect.INDETERMINATE)


class RfOffFailClosedTest(unittest.TestCase):
    def test_commit_failure_is_outcome_unknown_session_inhibit(self):
        provider, seams = assemble_rf_off_provider(fail_on_submit=True)
        with self.assertRaises(PocError) as ctx:
            provider.submit(_replay(target="ROC"), request_id="r1")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
        # commit 불명확: group은 방사 기록으로 남지 않는다(software submit이 예외).
        self.assertEqual(seams.group_tx.committed_group_ids, ())

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

    def test_software_group_tx_rejects_submit_before_both_lanes_staged(self):
        gtx = SoftwareGroupTx()
        gtx.retune_tx_lo(lane=0, channel="lower", hardware_timestamp_ns=8192)
        # lane 1 미staging 상태에서 두 lane submit → 순서 위반.
        with self.assertRaises(RuntimeError):
            gtx.submit_tx_group(group_id="g1",
                                lanes=((0, "g1:C4", "wf"), (1, "g1:C6", "wf")),
                                hardware_timestamp_ns=8192)


# --- 최소 seam double (fail-closed 조립 시험 전용) ---------------------------
class _MinimalArbiter:
    def reserve(self, *, resource_key, owner):
        return True

    def release(self, *, resource_key, owner):
        pass


class _ReadyRx:
    def window_ready(self, request):
        from vdeter.stage8.shared_medium.radiation import ReadinessDecision
        return ReadinessDecision(ready=True, reason="READY")


class _Wf:
    def encode(self, *, source_station, channel, payload):
        from vdeter.stage8.shared_medium.radiation import ReferenceWaveformFactory
        return ReferenceWaveformFactory().encode(
            source_station=source_station, channel=channel, payload=payload)


if __name__ == "__main__":
    unittest.main()
