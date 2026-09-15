"""Adversarial lifecycle regressions from the independent Stage 5 audit (RF off)."""

from dataclasses import replace

import pytest

from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.types import ActionDescriptor, ActionKind, PhysicalChannel
from vdeter.poc.shared_medium import testing as TD
from vdeter.poc.shared_medium.provider import SharedMediumActionProvider, NextSafeSlotScheduler
from vdeter.poc.shared_medium.radiation import AtomicBroadcast, BroadcastError, ParentOutcome


def action():
    return ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC", payload={"packet_hex": "00"})


def provider(arbiter=None, sink=None, readiness=None, scheduler=None):
    return SharedMediumActionProvider(
        radio_sink=sink or TD.CountingRadioSink(), rx_readiness=readiness or TD.ReadyReadiness(),
        waveform_factory=TD.CountingWaveformFactory(), arbiter=arbiter,
        scheduler=scheduler or NextSafeSlotScheduler(),
    )


@pytest.mark.parametrize("error", [PocError(PocErrorCode.POC_RADIO_RESOURCE_CONFLICT, "after reserve"),
                                  KeyboardInterrupt("after reserve"), SystemExit("after reserve")])
def test_reserve_side_effect_is_tracked_for_every_exception(error):
    class Arbiter(TD.FakeArbiter):
        def reserve(self, **kw):
            super().reserve(**kw)
            raise error

    arb = Arbiter()
    prov = provider(arbiter=arb)
    with pytest.raises(type(error)) as raised:
        prov.submit(action(), request_id="r")
    if isinstance(error, PocError):
        assert raised.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN
    assert prov.unresolved_ownership() == ("lower:slot-1",)
    prov.cleanup()
    assert arb.owner_by_key == {}


def test_precommit_rollback_release_failure_inhibits():
    class Arbiter(TD.FakeArbiter):
        def release(self, **kw):
            raise RuntimeError("release failed")

    prov = provider(arbiter=Arbiter(), readiness=TD.SelectiveReadiness(not_ready_endpoints=("ROC",)))
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="r")
    assert raised.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN
    assert prov.unresolved_ownership() == ("lower:slot-1",)


def test_unknown_commit_cannot_be_retried_or_overwrite_correlation():
    sink = TD.CountingRadioSink(fail_on_commit=True)
    prov = provider(arbiter=TD.FakeArbiter(), sink=sink)
    with pytest.raises(PocError):
        prov.submit(action(), request_id="r")
    original = prov.parent_for("r")
    for request_id in ("r", "new"):
        with pytest.raises(PocError) as raised:
            prov.submit(action(), request_id=request_id)
        assert raised.value.code == PocErrorCode.POC_TX_OUTCOME_UNKNOWN
    assert sink.commit_calls == 1
    assert prov.parent_for("r") is original


def test_repeated_resolve_cannot_hide_release_failure():
    class Arbiter(TD.FakeArbiter):
        def release(self, **kw):
            raise RuntimeError("release failed")

    prov = provider(arbiter=Arbiter())
    prov.submit(action(), request_id="r")
    terminals = {c.child_id: "COMPLETED" for c in prov.parent_for("r").children}
    for _ in range(2):
        with pytest.raises(PocError) as raised:
            prov.resolve_outcome("r", terminals)
        assert raised.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN


def test_mixed_unknown_terminal_keys_are_structured():
    prov = provider(arbiter=TD.FakeArbiter())
    prov.submit(action(), request_id="r")
    with pytest.raises(PocError) as raised:
        prov.resolve_outcome("r", {1: "COMPLETED", "ghost": "COMPLETED"})
    assert raised.value.code == PocErrorCode.POC_PROTOCOL_ERROR
    assert prov.active_group_count == 1
    assert prov._reservations == {"r": "lower:slot-1"}


def test_trigger_cleanup_failure_retries_the_trigger_owner_not_arbiter():
    class Scheduler(NextSafeSlotScheduler):
        failing = True

        def clear_triggers(self):
            if self.failing:
                raise RuntimeError("trigger cleanup failed")
            super().clear_triggers()

    scheduler = Scheduler()
    scheduler.register_trigger("o", slot=0)
    arb = TD.FakeArbiter()
    prov = provider(arbiter=arb, scheduler=scheduler)
    for _ in range(2):
        with pytest.raises(PocError):
            prov.cleanup()
    assert arb.release_calls == []
    scheduler.failing = False
    prov.cleanup()
    assert scheduler.trigger_count == 0
    assert prov.unresolved_ownership() == ()


def test_cleanup_interrupt_preserves_remaining_keys_and_cleans_other_resources():
    class Arbiter(TD.FakeArbiter):
        failing = True

        def release(self, **kw):
            if self.failing:
                raise KeyboardInterrupt("release interrupted")
            super().release(**kw)

    arb = Arbiter()
    prov = provider(arbiter=arb)
    prov.submit(action(), request_id="r")
    with pytest.raises(KeyboardInterrupt):
        prov.cleanup()
    assert prov.unresolved_ownership() == ("lower:slot-1",)
    arb.failing = False
    prov.cleanup()
    assert arb.owner_by_key == {}


def test_two_requests_cannot_share_one_arbiter_reservation():
    arb = TD.FakeArbiter()
    sink = TD.CountingRadioSink()
    prov = provider(arbiter=arb, sink=sink)
    prov.submit(action(), request_id="one")
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="two")
    assert raised.value.code == PocErrorCode.POC_RADIO_RESOURCE_CONFLICT
    assert sink.commit_calls == 1


def broadcast_parent():
    sink = TD.CountingRadioSink()
    broadcast = AtomicBroadcast(radio_sink=sink, rx_readiness=TD.ReadyReadiness())
    parent = broadcast.build_parent(source_station="MASS-B", channel=PhysicalChannel.LOWER,
        payload=b"a", scheduled_sample=8192, scheduled_hw_timestamp_ns=8192,
        slot_key="slot-1", addressed_destination="ROC")
    return broadcast, parent, sink


@pytest.mark.parametrize("field,value", [("scheduled_hw_timestamp_ns", 9), ("samples", (0j,)),
    ("tx_lane", 9), ("rx_station", "MASS-B"), ("child_id", "invalid-child")])
def test_transmit_rechecks_the_actual_single_burst_identity(field, value):
    broadcast, parent, sink = broadcast_parent()
    child = replace(parent.children[0], **{field: value})
    malformed = replace(parent, children=(child, parent.children[1]))
    with pytest.raises(BroadcastError):
        broadcast.transmit(malformed)
    assert sink.prepare_calls == sink.commit_calls == 0


def test_resolved_parent_cannot_transmit_again():
    broadcast, parent, sink = broadcast_parent()
    broadcast.transmit(parent)
    broadcast.resolve_outcome(parent.parent_id, {c.child_id: "COMPLETED" for c in parent.children})
    with pytest.raises(BroadcastError):
        broadcast.transmit(parent)
    assert sink.commit_calls == 1


def test_commit_unknown_is_in_evidence_without_a_success_result():
    from vdeter.poc.shared_medium.observation import observe_scene

    prov = provider(sink=TD.CountingRadioSink(fail_on_commit=True))
    with pytest.raises(PocError):
        prov.submit(action(), request_id="r")
    parent = prov.parent_for("r")
    data = prov.build_evidence([parent], observe_scene([])).as_json_value()
    assert data["broadcast_infrastructure_result"]["parent_outcomes"] == {
        parent.parent_id: ParentOutcome.OUTCOME_UNKNOWN.value}


@pytest.mark.parametrize("with_arbiter", [False, True])
def test_component_cleanup_recovers_without_a_resource_key(with_arbiter):
    class Scheduler(NextSafeSlotScheduler):
        failing = True

        def clear_triggers(self):
            if self.failing:
                raise RuntimeError("cleanup failed")
            super().clear_triggers()

    scheduler = Scheduler()
    prov = provider(arbiter=TD.FakeArbiter() if with_arbiter else None, scheduler=scheduler)
    with pytest.raises(PocError):
        prov.cleanup()
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="blocked")
    assert raised.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN
    scheduler.failing = False
    prov.cleanup()
    assert prov.unresolved_ownership() == ()


def test_interrupted_terminal_release_preserves_recovery_key():
    class Arbiter(TD.FakeArbiter):
        failing = True

        def release(self, **kw):
            if self.failing:
                raise KeyboardInterrupt("release interrupted")
            super().release(**kw)

    arb = Arbiter()
    prov = provider(arbiter=arb)
    prov.submit(action(), request_id="r")
    with pytest.raises(KeyboardInterrupt):
        prov.resolve_outcome("r", {c.child_id: "COMPLETED" for c in prov.parent_for("r").children})
    assert prov.unresolved_ownership() == ("lower:slot-1",)
    arb.failing = False
    prov.cleanup()
    assert arb.owner_by_key == {}


@pytest.mark.parametrize("result", [None, 1, "false"])
def test_malformed_reserve_result_never_commits(result):
    class Arbiter(TD.FakeArbiter):
        def reserve(self, **kw):
            super().reserve(**kw)
            return result

    arb = Arbiter()
    sink = TD.CountingRadioSink()
    prov = provider(arbiter=arb, sink=sink)
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="r")
    assert raised.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN
    assert sink.commit_calls == 0
    prov.cleanup()
    assert arb.owner_by_key == {}


def test_official_broadcast_reaches_both_authorized_public_receivers():
    from vdeter.poc.shared_medium.observation import observe_scene

    broadcast, _, _ = broadcast_parent()
    parent = broadcast.build_parent(source_station="ROC", channel=PhysicalChannel.UPPER,
        payload=b"broadcast", scheduled_sample=8192, scheduled_hw_timestamp_ns=8192,
        slot_key="slot-1", official_broadcast=True)
    scene = observe_scene([parent])
    assert {row.endpoint for row in scene.rows if row.public_delivery} == {"MASS-A", "MASS-B"}
    assert scene.poc_private_observations == ()


def test_exported_evidence_cannot_mutate_retained_evidence():
    from vdeter.poc.shared_medium.evidence import SharedMediumEvidence

    evidence = SharedMediumEvidence(broadcast_infrastructure_result={"nested": {"result": "FAILED"}},
                                   delivery_matrix=({"endpoint": "ROC"},))
    exported = evidence.as_json_value()
    exported["broadcast_infrastructure_result"]["nested"]["result"] = "PASS"
    exported["delivery_matrix"][0]["endpoint"] = "changed"
    assert evidence.as_json_value()["broadcast_infrastructure_result"]["nested"]["result"] == "FAILED"
    assert evidence.as_json_value()["delivery_matrix"][0]["endpoint"] == "ROC"


@pytest.mark.parametrize("schedule", [{"kind": "after_slots", "slots": 0}, {"kind": "after_ms", "ms": 0}])
def test_reaction_cannot_transmit_in_the_trigger_slot(schedule):
    scheduler = NextSafeSlotScheduler()
    scheduler.register_trigger("o", slot=10)
    prov = provider(scheduler=scheduler)
    with pytest.raises(PocError) as raised:
        prov.submit(replace(action(), source_observation_id="o", schedule=schedule), request_id="r")
    assert raised.value.code == PocErrorCode.POC_DEADLINE_MISSED


def test_provider_retention_obeys_existing_workload_action_bound():
    from vdeter.poc.api.limits import MAX_WORKLOAD_ACTION_REQUESTS

    sink = TD.CountingRadioSink()
    prov = provider(sink=sink)
    for index in range(MAX_WORKLOAD_ACTION_REQUESTS):
        request_id = str(index)
        prov.submit(action(), request_id=request_id)
        prov.resolve_outcome(request_id, {c.child_id: "COMPLETED" for c in prov.parent_for(request_id).children})
    cached = prov.submit(action(), request_id="0")
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="overflow")
    assert raised.value.code == PocErrorCode.POC_ACTION_LIMIT_EXCEEDED
    assert sink.commit_calls == MAX_WORKLOAD_ACTION_REQUESTS
    assert len(prov._parents) == MAX_WORKLOAD_ACTION_REQUESTS
    assert cached.broadcast_parent_id == prov.parent_for("0").parent_id
    prov.cleanup()
    prov.submit(action(), request_id="new-workload")


def test_missing_terminal_retains_ownership_until_cleanup():
    arb = TD.FakeArbiter()
    prov = provider(arbiter=arb)
    prov.submit(action(), request_id="r")
    parent = prov.parent_for("r")
    assert prov.resolve_outcome("r", {parent.children[0].child_id: "COMPLETED"}) == ParentOutcome.OUTCOME_UNKNOWN
    assert arb.owner_by_key == {"lower:slot-1": "POC"}
    assert prov.active_group_count == 1
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id="new")
    assert raised.value.code == PocErrorCode.POC_TX_OUTCOME_UNKNOWN
    prov.cleanup()
    assert arb.owner_by_key == {}


@pytest.mark.parametrize("ready", ["false", 1, object()])
def test_readiness_requires_an_explicit_boolean_true(ready):
    from vdeter.poc.shared_medium.radiation import ReadinessDecision

    class Readiness:
        def window_ready(self, request):
            return ReadinessDecision(ready=ready)

    sink = TD.CountingRadioSink()
    prov = provider(sink=sink, readiness=Readiness())
    with pytest.raises(PocError):
        prov.submit(action(), request_id="r")
    assert sink.commit_calls == 0


@pytest.mark.parametrize("request_id", [[], {}, None, "", 1])
def test_malformed_request_id_is_structured_before_any_seam(request_id):
    sink = TD.CountingRadioSink()
    prov = provider(sink=sink)
    with pytest.raises(PocError) as raised:
        prov.submit(action(), request_id=request_id)
    assert raised.value.code == PocErrorCode.POC_ACTION_SCHEMA_INVALID
    assert sink.prepare_calls == 0


def test_request_id_cannot_reuse_a_different_action_or_mutated_body():
    sink = TD.CountingRadioSink()
    prov = provider(sink=sink)
    original = action()
    prov.submit(original, request_id="r")
    original.payload["packet_hex"] = "01"
    with pytest.raises(PocError) as raised:
        prov.submit(original, request_id="r")
    assert raised.value.code == PocErrorCode.POC_REQUEST_ID_CONFLICT
    assert sink.commit_calls == 1


def test_internal_binary_payload_duplicate_uses_canonical_identity():
    sink = TD.CountingRadioSink()
    prov = provider(sink=sink)
    original = replace(action(), payload={"packet_bytes": bytearray(b"abc")})
    first = prov.submit(original, request_id="r")
    assert prov.submit(replace(original, payload={"packet_bytes": b"abc"}), request_id="r") == first
    original.payload["packet_bytes"][0] = 0
    with pytest.raises(PocError) as raised:
        prov.submit(original, request_id="r")
    assert raised.value.code == PocErrorCode.POC_REQUEST_ID_CONFLICT
    assert sink.commit_calls == 1


@pytest.mark.parametrize("name,bad", [("radio_sink", None), ("radio_sink", object()),
    ("rx_readiness", None), ("rx_readiness", object()), ("scheduler", object()),
    ("waveform_factory", object()), ("arbiter", None), ("arbiter", object())])
def test_production_constructor_rejects_missing_or_malformed_owners(name, bad):
    kwargs = dict(radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
        scheduler=NextSafeSlotScheduler(), waveform_factory=TD.CountingWaveformFactory(),
        arbiter=TD.FakeArbiter())
    kwargs[name] = bad
    with pytest.raises(ValueError):
        SharedMediumActionProvider.for_production(**kwargs)


def test_production_preserves_falsey_injected_dependencies():
    class Scheduler(NextSafeSlotScheduler):
        def __bool__(self):
            return False

    class Factory(TD.CountingWaveformFactory):
        def __bool__(self):
            return False

    class Resolver:
        calls = 0

        def __bool__(self):
            return False

        def __call__(self, action, request_id):
            self.calls += 1
            return b"injected"

    factory, resolver = Factory(), Resolver()
    prov = SharedMediumActionProvider.for_production(
        radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
        scheduler=Scheduler(current_slot=10), waveform_factory=factory,
        arbiter=TD.FakeArbiter(), packet_resolver=resolver)
    prov.submit(action(), request_id="r")
    assert prov.parent_for("r").slot_key == "slot-11"
    assert prov.parent_for("r").payload == b"injected"
    assert factory.encode_calls == resolver.calls == 1


def test_production_scheduler_requires_trigger_registration_and_cleanup():
    class ResolveOnly:
        resolve = NextSafeSlotScheduler().resolve

    with pytest.raises(ValueError):
        SharedMediumActionProvider.for_production(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            scheduler=ResolveOnly(), waveform_factory=TD.CountingWaveformFactory(),
            arbiter=TD.FakeArbiter())


@pytest.mark.parametrize("seam", ["factory", "readiness"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_prefire_operator_interrupt_survives_a_second_release_failure(seam, interrupt_type):
    interruption = interrupt_type("original operator interruption")

    class Arbiter(TD.FakeArbiter):
        failing = True

        def release(self, **kw):
            if self.failing:
                raise RuntimeError("secondary release failure")
            super().release(**kw)

    class InterruptedSeam:
        def encode(self, **kw):
            raise interruption

        def window_ready(self, request):
            raise interruption

    arb = Arbiter()
    prov = SharedMediumActionProvider(
        radio_sink=TD.CountingRadioSink(), scheduler=NextSafeSlotScheduler(), arbiter=arb,
        waveform_factory=InterruptedSeam() if seam == "factory" else TD.CountingWaveformFactory(),
        rx_readiness=InterruptedSeam() if seam == "readiness" else TD.ReadyReadiness())
    with pytest.raises(interrupt_type) as raised:
        prov.submit(action(), request_id="r")
    # 어느 seam이든 원형 그대로 전파되고 correlation을 남기지 않는다.
    assert raised.value is interruption
    assert prov.parent_for("r") is None

    if seam == "factory":
        # `encode`는 자원 예약 **앞**에서 돈다(`submit` 1절: 자원 확보 전 검증). 따라서 여기서
        # 중단되면 **잡은 자원이 없다**. 잡은 적 없는 식별자를 "소유권 불확실"로 적으면 실제보다
        # 넓게 억제하는 것이므로 그렇게 단언하지 않고, reserve가 아예 호출되지 않았음을 확인한다.
        assert arb.reserve_calls == []
        assert prov.unresolved_ownership() == ()
        # 같은 seam이 계속 같은 중단을 올린다 — 이는 소유권 억제가 아니라 같은 실패의 재발생이다.
        # 둘을 구분해 단언한다(SESSION_INHIBIT으로 오인하지 않는다).
        with pytest.raises(interrupt_type) as again:
            prov.submit(action(), request_id="next")
        assert again.value is interruption
        # release가 여전히 실패하도록 둔 채 정리한다 — 회수할 자원이 없으므로 조용히 끝나야 한다.
        prov.cleanup()
        assert arb.release_calls == []
        assert arb.owner_by_key == {}
        return

    # `window_ready`는 자원 예약 **뒤**에 돈다. release까지 실패했으므로 소유권이 불확실하게
    # 남고(fail-closed) 다음 submit이 SESSION_INHIBIT된다. 회수되면 조용히 성공한다.
    assert arb.reserve_calls == [("lower:slot-1", "POC")]
    assert prov.unresolved_ownership() == ("lower:slot-1",)
    with pytest.raises(PocError) as inhibited:
        prov.submit(action(), request_id="next")
    assert inhibited.value.code == PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN
    arb.failing = False
    prov.cleanup()
    assert arb.owner_by_key == {}
