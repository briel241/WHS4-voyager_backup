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
    # RF-off 시험 harness는 stub scheduler 기본값 사용을 명시 허용한다(production은 fail-closed).
    kw.setdefault("allow_rf_off_defaults", True)
    prov = SharedMediumActionProvider(
        radio_sink=sink, rx_readiness=readiness, waveform_factory=factory, **kw
    )
    return prov, sink, factory


def _action(target, *, kind=ActionKind.REPLAY_PACKET, payload=None, schedule=None,
            source_observation_id=None):
    return ActionDescriptor(
        kind=kind,
        target=target,
        source_observation_id=source_observation_id,
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
        # readiness 실패는 RF 방사 전 거부 -> PocError(POC_RADIO_RESOURCE_CONFLICT)로 변환(Stage 3 계약).
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="nr")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
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
        # 과거 trigger(slot 2)에 결속된 요청은 현재(slot 100) 기준 과거 -> 이동 없이 거부.
        sched = NextSafeSlotScheduler(current_slot=100, lead_slots=1)
        sched.register_trigger("obs-old", slot=2)  # VDETER가 제공한 과거 trigger
        prov, sink, _f = _provider(scheduler=sched)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", source_observation_id="obs-old",
                                schedule={"kind": "after_slots", "slots": 1}), request_id="past")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)
        self.assertEqual(sink.submit_calls, 0)  # 이동하지 않음

    def test_insufficient_lead_is_deadline_missed(self):
        sched = NextSafeSlotScheduler(current_slot=10, lead_slots=2)
        sched.register_trigger("obs-1", slot=10)
        prov, sink, _f = _provider(scheduler=sched)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", source_observation_id="obs-1",
                                schedule={"kind": "after_slots", "slots": 1}),
                        request_id="lead")  # slot 11 < earliest 12
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_DEADLINE_MISSED)

    def test_client_trigger_slot_is_rejected(self):
        # 반응형 기준시각을 PoC가 조작할 수 없다: trigger_slot 등 field는 거부한다(finding 1).
        sched = NextSafeSlotScheduler(current_slot=10, lead_slots=1)
        prov, sink, _f = _provider(scheduler=sched)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule={"kind": "after_slots", "slots": 1, "trigger_slot": 1000000}),
                        request_id="evil")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)

    def test_reactive_uses_vdeter_registered_trigger(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        sched.register_trigger("obs-42", slot=10)  # VDETER가 관측 event에서 등록
        prov, _s, _f = _provider(scheduler=sched)
        prov.submit(_action("ROC", source_observation_id="obs-42",
                            schedule={"kind": "after_slots", "slots": 2}), request_id="r")
        self.assertEqual(prov.parent_for("r").slot_key, "slot-12")  # trigger(10)+2, client 조작 불가

    def test_reactive_without_registered_trigger_rejected(self):
        prov, sink, _f = _provider(scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", source_observation_id="unknown",
                                schedule={"kind": "after_slots", "slots": 2}), request_id="notrig")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)

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


class CommitUnknownErrorContractTest(unittest.TestCase):
    """finding 2: group commit 불명확은 Stage 3 PocError(POC_TX_OUTCOME_UNKNOWN)로 변환·correlation 보존."""

    def test_commit_unknown_maps_to_session_inhibit_pocerror(self):
        from vdeter.stage8.poc_api.errors import Severity

        sink = TD.CountingRadioSink(fail_on_commit=True)
        prov, _s, _f = _provider(sink=sink)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="u")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
        self.assertEqual(ctx.exception.severity, Severity.SESSION_INHIBIT)
        # correlation 보존: request->parent 결속이 남는다.
        parent = prov.parent_for("u")
        self.assertIsNotNone(parent)
        self.assertEqual(ctx.exception.detail.get("broadcast_parent_id"), parent.parent_id)
        # in-flight group이 남아 cleanup으로 회수된다.
        self.assertEqual(prov.active_group_count, 1)
        prov.cleanup()
        self.assertEqual(prov.active_group_count, 0)

    def test_staging_failure_maps_to_request_rejected(self):
        sink = TD.CountingRadioSink(fail_on_prepare=True)
        prov, _s, _f = _provider(sink=sink)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="s")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        self.assertEqual(sink.submit_calls, 0)


class ForProductionTest(unittest.TestCase):
    """production 조립은 scheduler·frozen PHY waveform factory를 필수로 fail-closed한다(제안)."""

    def test_requires_scheduler(self):
        with self.assertRaises(ValueError):
            SharedMediumActionProvider.for_production(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                scheduler=None, waveform_factory=TD.CountingWaveformFactory(),
            )

    def test_requires_waveform_factory(self):
        with self.assertRaises(ValueError):
            SharedMediumActionProvider.for_production(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                scheduler=NextSafeSlotScheduler(), waveform_factory=None,
            )

    def test_both_present_builds_provider(self):
        prov = SharedMediumActionProvider.for_production(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
            waveform_factory=TD.CountingWaveformFactory(),
            arbiter=TD.FakeArbiter(),
        )
        prov.submit(_action("ROC"), request_id="p")
        self.assertEqual(prov.parent_for("p").slot_key, "slot-1")


class _BoomFactory:
    """parent waveform 생성에서 실패하는 factory(자원 누수 시험용)."""

    def encode(self, *, source_station, channel, payload):
        raise RuntimeError("waveform factory boom")


class ReservationLeakTest(unittest.TestCase):
    """finding 1: parent 생성 실패에서도 예약 누수 0 + PocError 변환."""

    def test_build_parent_failure_releases_reservation_and_maps_pocerror(self):
        arbiter = TD.FakeArbiter()
        prov, sink, _f = _provider(factory=_BoomFactory(), arbiter=arbiter)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        # 예약·correlation 잔존 0, 방사 0.
        self.assertEqual([k for k, v in arbiter.owner_by_key.items() if v == "POC"], [])
        self.assertIsNone(prov.parent_for("r"))
        self.assertEqual(sink.submit_calls, 0)

    def test_packet_resolver_failure_holds_no_reservation(self):
        arbiter = TD.FakeArbiter()
        prov, sink, _f = _provider(arbiter=arbiter)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", payload={}), request_id="np")  # packet 내용 없음
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(arbiter.owner_by_key, {})
        self.assertEqual(sink.submit_calls, 0)


class AfterMsCausalityTest(unittest.TestCase):
    """finding 2: after_ms는 trigger hardware timestamp + N ms 이후 첫 slot(올림)이어야 한다."""

    def test_after_ms_zero_is_causal_to_trigger_timestamp(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        sched.register_trigger("obs", slot=10, hw_timestamp_ns=85920)
        prov, _s, _f = _provider(scheduler=sched)
        prov.submit(_action("ROC", source_observation_id="obs",
                            schedule={"kind": "after_ms", "ms": 0}), request_id="c")
        parent = prov.parent_for("c")
        self.assertGreaterEqual(parent.scheduled_hw_timestamp_ns, 85920)  # 과거로 가지 않는다
        self.assertEqual(parent.slot_key, "slot-11")  # ceil(85920/8192)=11

    def test_after_ms_offset_is_applied_from_timestamp(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1, samples_per_ms=307)
        sched.register_trigger("obs", slot=0, hw_timestamp_ns=0)
        prov, _s, _f = _provider(scheduler=sched)
        prov.submit(_action("ROC", source_observation_id="obs",
                            schedule={"kind": "after_ms", "ms": 100}), request_id="m")
        parent = prov.parent_for("m")
        self.assertGreaterEqual(parent.scheduled_hw_timestamp_ns, 100 * 307)

    def test_after_slots_still_uses_trigger_slot(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        sched.register_trigger("obs", slot=10, hw_timestamp_ns=85920)
        prov, _s, _f = _provider(scheduler=sched)
        prov.submit(_action("ROC", source_observation_id="obs",
                            schedule={"kind": "after_slots", "slots": 2}), request_id="s")
        self.assertEqual(prov.parent_for("s").slot_key, "slot-12")  # trigger slot 10 + 2


class ScheduleValidationTest(unittest.TestCase):
    """finding 3: schedule의 key·type·범위를 먼저 검증해 POC_ACTION_SCHEMA_INVALID로 구조화한다."""

    def _expect_invalid(self, schedule, request_id, *, obs="obs"):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        sched.register_trigger(obs, slot=5)
        prov, sink, _f = _provider(scheduler=sched)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", source_observation_id=obs, schedule=schedule),
                        request_id=request_id)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)

    def test_non_integer_slots_rejected(self):
        self._expect_invalid({"kind": "after_slots", "slots": "bad"}, "s1")

    def test_non_integer_ms_rejected(self):
        self._expect_invalid({"kind": "after_ms", "ms": "bad"}, "m1")

    def test_float_and_bool_rejected(self):
        self._expect_invalid({"kind": "after_slots", "slots": 1.5}, "s2")
        self._expect_invalid({"kind": "after_slots", "slots": True}, "s3")

    def test_negative_and_out_of_range_rejected(self):
        self._expect_invalid({"kind": "after_slots", "slots": -5}, "s4")
        self._expect_invalid({"kind": "after_ms", "ms": 10_000_000}, "m2")

    def test_missing_required_offset_rejected(self):
        self._expect_invalid({"kind": "after_slots"}, "s5")
        self._expect_invalid({"kind": "after_ms"}, "m3")

    def test_unknown_schedule_key_rejected(self):
        self._expect_invalid({"kind": "NEXT_SAFE_SLOT", "surprise": 1}, "u1")
        self._expect_invalid({"kind": "after_slots", "slots": 1, "ms": 5}, "u2")

    def test_non_dict_schedule_rejected(self):
        prov, sink, _f = _provider()
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC", schedule=["not", "a", "dict"]), request_id="nd")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(sink.submit_calls, 0)


class ConstructorFailClosedTest(unittest.TestCase):
    """추가 제안: 일반 생성자도 RF-off 기본값을 명시 허용하지 않으면 생성 실패."""

    def test_missing_scheduler_without_optin_raises(self):
        with self.assertRaises(ValueError):
            SharedMediumActionProvider(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                waveform_factory=TD.CountingWaveformFactory(),  # scheduler 누락
            )

    def test_missing_waveform_factory_without_optin_raises(self):
        with self.assertRaises(ValueError):
            SharedMediumActionProvider(
                radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
                scheduler=NextSafeSlotScheduler(),  # waveform_factory 누락
            )

    def test_explicit_optin_allows_rf_off_defaults(self):
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            allow_rf_off_defaults=True,
        )
        prov.submit(_action("ROC"), request_id="ok")
        self.assertEqual(prov.parent_for("ok").slot_key, "slot-1")

    def test_both_injected_needs_no_optin(self):
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
            waveform_factory=TD.CountingWaveformFactory(),
        )
        prov.submit(_action("ROC"), request_id="inj")
        self.assertEqual(prov.parent_for("inj").slot_key, "slot-1")


class TriggerRegistryTest(unittest.TestCase):
    """finding A: trigger registry는 workload당 bounded·cleanup되어야 하고 VDETER hook으로만 등록된다."""

    def test_registry_is_bounded_with_eviction(self):
        from vdeter.stage8.shared_medium.constants import MAX_TRIGGER_REGISTRY

        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        for i in range(MAX_TRIGGER_REGISTRY + 500):
            sched.register_trigger(f"obs{i}", slot=1)
        self.assertLessEqual(sched.trigger_count, MAX_TRIGGER_REGISTRY)

    def test_clear_triggers(self):
        sched = NextSafeSlotScheduler()
        sched.register_trigger("o", slot=1)
        self.assertEqual(sched.trigger_count, 1)
        sched.clear_triggers()
        self.assertEqual(sched.trigger_count, 0)

    def test_constructor_initial_triggers_respect_cap(self):
        from vdeter.stage8.shared_medium.constants import MAX_TRIGGER_REGISTRY

        init = {f"o{i}": (1, 1) for i in range(MAX_TRIGGER_REGISTRY + 50)}
        sched = NextSafeSlotScheduler(triggers=init)
        self.assertLessEqual(sched.trigger_count, MAX_TRIGGER_REGISTRY)

    def test_provider_cleanup_clears_scheduler_triggers(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        prov, _s, _f = _provider(scheduler=sched)
        prov.register_observation_trigger("obs-1", slot=5)
        self.assertEqual(sched.trigger_count, 1)
        prov.cleanup()
        self.assertEqual(sched.trigger_count, 0)  # workload cleanup 시 잔존 0

    def test_register_observation_trigger_hook_enables_reactive(self):
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        prov, _s, _f = _provider(scheduler=sched)
        prov.register_observation_trigger("obs-42", slot=10)
        prov.submit(_action("ROC", source_observation_id="obs-42",
                            schedule={"kind": "after_slots", "slots": 2}), request_id="r")
        self.assertEqual(prov.parent_for("r").slot_key, "slot-12")


class ResolveIdempotencyTest(unittest.TestCase):
    """finding C: 재-resolve/unknown resolve는 raw 예외가 아니라 PocError/저장결과여야 한다."""

    def test_double_resolve_returns_same_outcome_no_raw(self):
        from vdeter.stage8.shared_medium.radiation import ChildTerminal, ParentOutcome

        prov, _s, _f = _provider()
        prov.submit(_action("ROC"), request_id="r1")
        parent = prov.parent_for("r1")
        terms = {c.child_id: ChildTerminal.COMPLETED for c in parent.children}
        first = prov.resolve_outcome("r1", terms)
        second = prov.resolve_outcome("r1", terms)  # 재-resolve: raw 예외 없이 idempotent
        self.assertEqual(first, ParentOutcome.COMPLETED)
        self.assertEqual(second, ParentOutcome.COMPLETED)

    def test_resolve_unknown_request_is_pocerror(self):
        prov, _s, _f = _provider()
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("nope", {})
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_NOT_RUNNING)


class _ReleaseBoomArbiter(TD.FakeArbiter):
    """release()가 항상 예외를 던지는 arbiter(자원 반환 실패 시험용)."""

    def release(self, *, resource_key, owner):
        raise RuntimeError("release boom")


class CleanupRobustnessTest(unittest.TestCase):
    """finding 1: arbiter release 실패가 cleanup 전체를 중단시키면 안 되고 SESSION_INHIBIT로 승격."""

    def test_cleanup_release_failure_still_clears_and_inhibits(self):
        arb = _ReleaseBoomArbiter()
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=TD.CountingWaveformFactory(), arbiter=arb,
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
        )
        prov.submit(_action("ROC"), request_id="r")
        with self.assertRaises(PocError) as ctx:
            prov.cleanup()
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        # 잔존 0: release 실패에도 나머지 정리는 finally에서 완료된다.
        self.assertEqual(prov._reservations, {})
        self.assertEqual(prov.active_group_count, 0)
        self.assertIsNone(prov.parent_for("r"))

    def test_resolve_outcome_release_failure_inhibits_no_raw(self):
        from vdeter.stage8.shared_medium.radiation import ChildTerminal

        arb = _ReleaseBoomArbiter()
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=TD.CountingWaveformFactory(), arbiter=arb,
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
        )
        prov.submit(_action("ROC"), request_id="r")
        parent = prov.parent_for("r")
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("r", {c.child_id: ChildTerminal.COMPLETED for c in parent.children})
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)


class ChildTerminalValidationTest(unittest.TestCase):
    """finding 2: 잘못된 child terminal은 raw ValueError가 아니라 PocError, parent 성공/해제 없음."""

    def test_bogus_terminal_is_pocerror_not_success(self):
        arb = TD.FakeArbiter()
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=TD.CountingWaveformFactory(), arbiter=arb,
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
        )
        prov.submit(_action("ROC"), request_id="r")
        parent = prov.parent_for("r")
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("r", {parent.children[0].child_id: "BOGUS",
                                       parent.children[1].child_id: "COMPLETED"})
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        # parent를 성공/확정 처리하지 않고, reservation도 해제하지 않는다.
        self.assertFalse(prov._broadcast.is_resolved(parent.parent_id))
        self.assertEqual(prov._broadcast.active_group_count, 1)
        self.assertIn("r", prov._reservations)

    def test_none_terminal_is_outcome_unknown(self):
        from vdeter.stage8.shared_medium.radiation import ChildTerminal, ParentOutcome

        prov, _s, _f = _provider()
        prov.submit(_action("ROC"), request_id="r")
        parent = prov.parent_for("r")
        outcome = prov.resolve_outcome(
            "r", {parent.children[0].child_id: ChildTerminal.COMPLETED,
                  parent.children[1].child_id: None},
        )
        self.assertEqual(outcome, ParentOutcome.OUTCOME_UNKNOWN)


class ProviderEvidenceTest(unittest.TestCase):
    """finding B: provider.build_evidence는 outcomes 인자 없이도 확정 outcome을 반영한다."""

    def test_build_evidence_reflects_resolved_outcome_without_explicit_outcomes(self):
        from vdeter.stage8.shared_medium import observation as O
        from vdeter.stage8.shared_medium.radiation import ChildTerminal

        sink = TD.CountingRadioSink()
        prov = SharedMediumActionProvider(
            radio_sink=sink, rx_readiness=TD.ReadyReadiness(),
            scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1),
            waveform_factory=TD.CountingWaveformFactory(),
        )
        prov.submit(_action("ROC"), request_id="e1")
        parent = prov.parent_for("e1")
        # terminal resolve 후 evidence 조립: outcomes 인자 없이도 provider가 확정 결과를 주입한다.
        prov.resolve_outcome("e1", {c.child_id: ChildTerminal.COMPLETED for c in parent.children})
        scene = O.observe_scene([parent])
        from vdeter.stage8.shared_medium.radiation import BroadcastResult, ParentOutcome
        pending_result = BroadcastResult(
            parent=parent, committed=True,
            submitted_child_ids=tuple(c.child_id for c in parent.children),
            outcome=ParentOutcome.PENDING,
        )
        j = prov.build_evidence([parent], scene, broadcast_results=[pending_result]).as_json_value()
        self.assertEqual(
            j["broadcast_infrastructure_result"]["parent_outcomes"][parent.parent_id], "COMPLETED"
        )


if __name__ == "__main__":
    unittest.main()
