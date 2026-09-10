"""Stage 5 shared-medium: SharedMediumActionProvider(ActionSubmitProvider seam) 계약시험.

RF-off 요구 매핑: 5(target->canonical channel), 6(forbidden RF field 거부),
7(동시 lower+upper 금지), 8(action1->parent1->child2), 9(중복 provider/RF submit 0),
13(정상 TX 우선·PoC만 거부), capability gate.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.poc_api.seams import ActionSubmitProvider
from vdeter.stage8.poc_api.types import ActionDescriptor, ActionKind, PhysicalChannel
from vdeter.stage8.shared_medium import testing as TD
from vdeter.stage8.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider


def _provider(**kw):
    sink = kw.pop("sink", None) or TD.CountingRadioSink()
    factory = kw.pop("factory", None) or TD.CountingWaveformFactory()
    readiness = kw.pop("readiness", None) or TD.ReadyReadiness()
    prov = SharedMediumActionProvider(
        radio_sink=sink, rx_readiness=readiness, waveform_factory=factory, **kw
    )
    return prov, sink, factory


def _action(target, *, kind=ActionKind.REPLAY_PACKET, payload=None, schedule=None):
    return ActionDescriptor(
        kind=kind,
        target=target,
        payload=payload if payload is not None else {"packet_hex": "48454c4c4f"},
        schedule=schedule if schedule is not None else {"kind": "NEXT_SAFE_SLOT"},
    )


class SeamComplianceTest(unittest.TestCase):
    def test_satisfies_action_submit_provider_protocol(self):
        prov, _s, _f = _provider()
        self.assertIsInstance(prov, ActionSubmitProvider)  # runtime_checkable duck-typing


class TargetToChannelTest(unittest.TestCase):
    def test_roc_lower_massa_upper(self):
        prov, _s, _f = _provider()
        self.assertEqual(prov.derive_physical_channel("ROC"), PhysicalChannel.LOWER)
        self.assertEqual(prov.derive_physical_channel("MASS-A"), PhysicalChannel.UPPER)

    def test_submit_records_canonical_channel_and_parent(self):
        prov, _s, _f = _provider()
        sub = prov.submit(_action("ROC"), request_id="r1")
        self.assertEqual(sub.physical_channel, PhysicalChannel.LOWER)
        self.assertTrue(sub.broadcast_parent_id)
        self.assertTrue(sub.transfer_id)

    def test_unknown_target_denied(self):
        prov, _s, _f = _provider()
        for bad in ("MASS-B", "BROADCAST", "unknown"):
            with self.assertRaises(PocError) as ctx:
                prov.submit(_action(bad), request_id=f"rq-{bad}")
            self.assertEqual(ctx.exception.code, PocErrorCode.POC_TARGET_DENIED)


class ForbiddenFieldTest(unittest.TestCase):
    """WSM-REQ-6/§9.4: arbitrary frequency/lane/gain/waveform/동시 lower+upper 거부."""

    def test_rejects_each_forbidden_field(self):
        prov, sink, _f = _provider()
        for field in ("frequency", "gain", "rf_lane", "waveform", "sample_rate",
                      "bandwidth", "physical_channel", "raw_iq", "simultaneous_channels"):
            with self.assertRaises(PocError) as ctx:
                prov.submit(
                    _action("ROC", payload={"packet_hex": "00", field: 1}),
                    request_id=f"rq-{field}",
                )
            self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)  # 거부된 요청은 RF submit 0

    def test_single_channel_per_action(self):
        # 한 action은 하나의 논리 target -> 하나의 channel. 동시 lower+upper TX 경로가 없다.
        prov, _s, _f = _provider()
        sub = prov.submit(_action("ROC"), request_id="c1")
        self.assertEqual(sub.physical_channel, PhysicalChannel.LOWER)
        sub2 = prov.submit(_action("MASS-A"), request_id="c2")
        self.assertEqual(sub2.physical_channel, PhysicalChannel.UPPER)
        # 두 요청은 별개 parent이며 한 action이 두 채널을 동시에 쓰지 않는다.
        self.assertNotEqual(sub.broadcast_parent_id, sub2.broadcast_parent_id)


class ExactlyOnceTest(unittest.TestCase):
    def test_same_request_id_reuses_result_no_resubmit(self):
        prov, sink, factory = _provider()
        act = _action("ROC")
        sub1 = prov.submit(act, request_id="idem")
        sub2 = prov.submit(act, request_id="idem")
        self.assertEqual(sub1.broadcast_parent_id, sub2.broadcast_parent_id)
        self.assertEqual(sink.submit_calls, 2)   # child 2개 그대로(재submit 없음)
        self.assertEqual(factory.encode_calls, 1)  # waveform 재생성 없음
        self.assertEqual(sink.duplicate_child_submits, 0)

    def test_one_action_one_parent_two_children(self):
        prov, sink, _f = _provider()
        sub = prov.submit(_action("MASS-A"), request_id="one")
        self.assertEqual(len(sink.submitted_parents[sub.broadcast_parent_id]), 2)
        self.assertEqual(sink.submit_calls, 2)


class PriorityTest(unittest.TestCase):
    """WSM/POC-D14: 정상 TX가 자원을 점유하면 PoC만 거부하고 정상 TX를 취소하지 않는다."""

    def test_poc_rejected_on_resource_conflict(self):
        # 기본 NEXT_SAFE_SLOT scheduler -> slot-1. 정상 TX가 lower:slot-1 점유 시 PoC 거부.
        arbiter = TD.FakeArbiter(normal_reserved={"lower:slot-1"})
        prov, sink, _f = _provider(arbiter=arbiter)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="conf")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        self.assertEqual(sink.submit_calls, 0)
        # 정상 예약이 유지된다(PoC가 취소하지 않는다).
        self.assertEqual(arbiter.owner_by_key["lower:slot-1"], "NORMAL")

    def test_poc_reserves_free_resource(self):
        arbiter = TD.FakeArbiter()
        prov, sink, _f = _provider(arbiter=arbiter)
        prov.submit(_action("ROC"), request_id="ok")
        self.assertEqual(sink.submit_calls, 2)
        self.assertEqual(arbiter.owner_by_key["lower:slot-1"], "POC")


class CapabilityTest(unittest.TestCase):
    def test_dual_channel_not_advertised_by_default(self):
        prov, _s, _f = _provider()
        self.assertEqual(prov.advertised_capabilities(), frozenset())

    def test_required_dual_channel_fails_closed_when_not_installed(self):
        prov, _s, _f = _provider(dual_channel_installed=False)
        with self.assertRaises(PocError) as ctx:
            prov.prepare_dual_channel(required=True)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)

    def test_dual_channel_available_when_installed(self):
        prov, _s, _f = _provider(dual_channel_installed=True)
        self.assertIn("observe_dual_physical_channel", prov.advertised_capabilities())
        plan = prov.prepare_dual_channel(required=True)
        self.assertEqual(plan.common_rf_lo_hz, 2_449_537_500)


class ReadinessFailClosedTest(unittest.TestCase):
    def test_provider_fails_closed_and_releases_arbiter(self):
        arbiter = TD.FakeArbiter()
        prov, sink, _f = _provider(
            arbiter=arbiter, readiness=TD.SelectiveReadiness(not_ready_endpoints={"ROC"})
        )
        from vdeter.stage8.shared_medium.radiation import BroadcastNotReady

        with self.assertRaises(BroadcastNotReady):
            prov.submit(_action("ROC"), request_id="nr")
        self.assertEqual(sink.submit_calls, 0)
        self.assertNotIn("lower:slot-1", arbiter.owner_by_key)  # 예약 해제됨


class SchedulingContractTest(unittest.TestCase):
    """§10, POC-D12: PoC는 상대 schedule만; hardware 시각은 MAC이 계산; 과거/닫힘/lead 부족은 DEADLINE_MISSED."""

    def test_forbidden_timing_fields_rejected(self):
        prov, sink, _f = _provider()
        for field in ("scheduled_hw_timestamp_ns", "scheduled_sample", "slot_key",
                      "hardware_timestamp", "transmit_at"):
            with self.assertRaises(PocError) as ctx:
                prov.submit(_action("ROC", schedule={"kind": "NEXT_SAFE_SLOT", field: 123}),
                            request_id=f"rq-{field}")
            self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)

    def test_unknown_schedule_kind_rejected(self):
        prov, _s, _f = _provider()
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule={"kind": "whenever"}), request_id="k")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)

    def test_next_safe_slot_computed_by_scheduler(self):
        prov, _s, _f = _provider(scheduler=NextSafeSlotScheduler(current_slot=5, lead_slots=2))
        prov.submit(_action("ROC", schedule={"kind": "NEXT_SAFE_SLOT"}), request_id="r")
        self.assertEqual(prov.parent_for("r").slot_key, "slot-7")  # 5+2, MAC이 계산

    def test_after_slots_into_past_is_deadline_missed(self):
        prov, sink, _f = _provider(scheduler=NextSafeSlotScheduler(current_slot=10, lead_slots=1))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule={"kind": "after_slots", "slots": -5, "trigger_slot": 10}),
                        request_id="past")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)
        self.assertEqual(sink.submit_calls, 0)  # 이동하지 않음

    def test_insufficient_lead_is_deadline_missed(self):
        prov, sink, _f = _provider(scheduler=NextSafeSlotScheduler(current_slot=10, lead_slots=2))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule={"kind": "after_slots", "slots": 1, "trigger_slot": 10}),
                        request_id="lead")  # slot 11 < earliest 12
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)

    def test_closed_slot_is_deadline_missed(self):
        prov, sink, _f = _provider(
            scheduler=NextSafeSlotScheduler(current_slot=10, lead_slots=1, closed_slots={11})
        )
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule={"kind": "NEXT_SAFE_SLOT"}), request_id="closed")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)
        self.assertEqual(sink.submit_calls, 0)


class ArbiterReleaseTest(unittest.TestCase):
    """자원 반환: terminal·cleanup에서 PoC 예약을 release한다."""

    def test_cleanup_releases_all_reservations(self):
        arbiter = TD.FakeArbiter()
        prov, _s, _f = _provider(arbiter=arbiter, scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1))
        prov.submit(_action("ROC"), request_id="a")
        self.assertIn("lower:slot-1", arbiter.owner_by_key)
        prov.cleanup()
        self.assertEqual([k for k, v in arbiter.owner_by_key.items() if v == "POC"], [])

    def test_resolve_outcome_releases_reservation(self):
        from vdeter.stage8.shared_medium.radiation import ChildTerminal, ParentOutcome

        arbiter = TD.FakeArbiter()
        prov, _s, _f = _provider(arbiter=arbiter)
        prov.submit(_action("MASS-A"), request_id="rq")
        parent = prov.parent_for("rq")
        terminals = {c.child_id: ChildTerminal.COMPLETED for c in parent.children}
        outcome = prov.resolve_outcome("rq", terminals)
        self.assertEqual(outcome, ParentOutcome.COMPLETED)
        self.assertEqual([k for k, v in arbiter.owner_by_key.items() if v == "POC"], [])


if __name__ == "__main__":
    unittest.main()
