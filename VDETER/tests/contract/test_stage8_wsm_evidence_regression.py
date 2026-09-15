"""RF-off evidence regressions: retained proof, correlation, model boundary, bounds."""

import json
import pytest

from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.limits import MAX_JSON_NESTING, MAX_WIRE_PACKET_BYTES
from vdeter.poc.api.types import ActionDescriptor, ActionKind, PhysicalChannel
from vdeter.poc.shared_medium import evidence as E
from vdeter.poc.shared_medium import observation as O
from vdeter.poc.shared_medium import radiation as R
from vdeter.poc.shared_medium import testing as T
from vdeter.poc.shared_medium.provider import SharedMediumActionProvider


def parent(ab, *, source="ROC", channel=PhysicalChannel.UPPER, start=0, payload=b"proof"):
    return ab.build_parent(
        source_station=source, channel=channel, payload=payload,
        scheduled_sample=start, scheduled_hw_timestamp_ns=start,
        slot_key=f"slot-{start}", addressed_destination="MASS-A" if source == "ROC" else "ROC",
    )


def test_empty_and_populated_exports_identify_emulation_and_clipping_boundary():
    ab = R.AtomicBroadcast(radio_sink=T.CountingRadioSink(), rx_readiness=T.ReadyReadiness())
    p = parent(ab)
    for value in (E.SharedMediumEvidence(), E.build_evidence([p], O.observe_scene([p]))):
        boundary = value.as_json_value()["model_boundary"]
        assert boundary["model"] == "WIRED_SHARED_MEDIUM_EMULATION"
        assert boundary["collision_policy"] == "LOCAL_DECODED_WINDOW_OVERLAP_DROP_V1"
        assert boundary["undecoded_interference_detection"] is False
        assert boundary["rf_off_scene_model"] == "TX_INTERVAL_DROP_V1"
        assert boundary["virtual_clipping"] == "NOT_IMPLEMENTED_REMOVED_V1_4"
        assert boundary["single_antenna_adc_equivalence_proven"] is False
        assert boundary["analog_adc_saturation_proven"] is False


def test_actual_nested_readiness_proof_survives_prepare_and_both_snapshot_boundaries():
    decisions = {}

    class Readiness(T.ReadyReadiness):
        def window_ready(self, request):
            decision = super().window_ready(request)
            decision.proof.update(window_id=f"software-window-{request.endpoint}",
                                  lanes={"accepted": [0, 1]})
            decisions[request.endpoint] = decision
            return decision

    class MutatingSink(T.CountingRadioSink):
        def prepare_group(self, **kwargs):
            for decision in decisions.values():
                decision.proof["lanes"]["accepted"].clear()
            return super().prepare_group(**kwargs)

    ab = R.AtomicBroadcast(radio_sink=MutatingSink(), rx_readiness=Readiness())
    p = parent(ab)
    result = ab.transmit(p)
    ev = E.build_evidence([p], O.observe_scene([p]), broadcast_results=[result])
    exported = ev.as_json_value()
    for row in exported["delivery_matrix"]:
        readiness = row["readiness"]
        assert readiness["state"] == "REQUIRED_READY"
        assert readiness["ready"] is True
        assert readiness["reason"] == "READY"
        assert readiness["proof"] == {
            "endpoint": row["endpoint"], "channel": row["channel"], "slot_key": p.slot_key,
            "clock_id": p.clock_id, "bridge_generation": p.bridge_generation,
            "correction_revision": p.correction_revision,
            "window_id": f"software-window-{row['endpoint']}", "lanes": {"accepted": [0, 1]},
        }
        assert row["parent_id"] == p.parent_id
        readiness["proof"]["lanes"]["accepted"].clear()
    for decision in result.readiness_proofs.values():
        decision.proof["lanes"]["accepted"].clear()
    assert all(row["readiness"]["proof"]["lanes"]["accepted"] == [0, 1]
               for row in ev.as_json_value()["delivery_matrix"])


def test_composite_metrics_correlate_collision_sources_and_distinct_sample_windows():
    ab = R.AtomicBroadcast(radio_sink=T.CountingRadioSink(), rx_readiness=T.ReadyReadiness())
    parents = [parent(ab, source="MASS-A", channel=PhysicalChannel.LOWER, payload=b"first"),
               parent(ab, source="MASS-B", channel=PhysicalChannel.LOWER, start=1, payload=b"second"),
               parent(ab, source="MASS-A", channel=PhysicalChannel.LOWER, start=4096, payload=b"later")]
    scene = O.observe_scene(parents)
    values = E.build_evidence(parents, scene).as_json_value()
    for row in values["delivery_matrix"]:
        if row["tuning_state"] == "NOT_TUNED":
            assert row["interval_evidence"] is None
            assert row["readiness"] is None
            continue
        metrics = row["interval_evidence"]
        p = next(p for p in parents if p.parent_id == row["parent_id"])
        assert metrics["model"] == "TX_INTERVAL_DROP_V1"
        assert metrics["simulation_only"] is True
        assert metrics["start_sample"] <= p.scheduled_sample < metrics["end_sample"]
        assert p.parent_id in metrics["parent_ids"]
        if p.scheduled_sample < 4096:
            assert row["decode_status"] == "MODEL_COLLISION_DROP"
            assert row["public_delivery"] is False
            assert row["decoded_payload_digest"] is None
        else:
            assert row["public_delivery"] is True
        assert "pre_clip_peak" not in metrics
    json.dumps(values, allow_nan=False)
    assert '"samples"' not in json.dumps(values)
    assert '"payload"' not in json.dumps(values)


def test_readiness_is_absent_without_a_result_and_empty_proof_stays_empty():
    ab = R.AtomicBroadcast(radio_sink=T.CountingRadioSink(), rx_readiness=T.SelectiveReadiness())
    p = parent(ab)
    scene = O.observe_scene([p])
    assert all(row["readiness"] is None for row in E.build_evidence([p], scene).as_json_value()["delivery_matrix"])
    result = ab.transmit(p)
    exported = E.build_evidence([p], scene, broadcast_results=[result]).as_json_value()
    assert all(row["readiness"]["proof"] == {} for row in exported["delivery_matrix"])
    assert "window_id" not in json.dumps(exported)


@pytest.mark.parametrize("proof", [
    {"samples": [1.0, 2.0]}, {"nested": {"raw_iq": b"secret"}},
    {"x": float("nan")}, {"x": "x" * MAX_WIRE_PACKET_BYTES},
])
def test_unsafe_or_unbounded_new_proof_cannot_reach_commit(proof):
    class Readiness:
        def window_ready(self, request):
            return R.ReadinessDecision(True, "READY", proof)

    sink = T.CountingRadioSink()
    ab = R.AtomicBroadcast(radio_sink=sink, rx_readiness=Readiness())
    with pytest.raises(R.BroadcastNotReady, match="proof"):
        ab.transmit(parent(ab))
    assert sink.prepare_calls == sink.commit_calls == 0


def test_deep_proof_is_rejected_without_recursing_indefinitely():
    nested = {}
    for _ in range(MAX_JSON_NESTING + 1):
        nested = {"child": nested}
    test_unsafe_or_unbounded_new_proof_cannot_reach_commit(nested)


def test_proof_and_composite_export_obey_existing_delivery_cap(monkeypatch):
    ab = R.AtomicBroadcast(radio_sink=T.CountingRadioSink(), rx_readiness=T.ReadyReadiness())
    p = parent(ab)
    result = ab.transmit(p)
    scene = O.observe_scene([p])
    monkeypatch.setattr(E, "MAX_DELIVERY_MATRIX_ENTRIES", 1)
    exported = E.build_evidence([p], scene, broadcast_results=[result]).as_json_value()
    assert len(exported["delivery_matrix"]) == 1
    assert exported["delivery_matrix"][0]["readiness"]["proof"]
    assert exported["delivery_matrix"][0]["interval_evidence"]["parent_ids"]
    assert exported["broadcast_infrastructure_result"]["delivery_matrix_dropped_rows"] == 1
    assert exported["broadcast_infrastructure_result"]["delivery_matrix_cap"] == 1


def test_generator_results_retain_both_child_and_readiness_evidence():
    ab = R.AtomicBroadcast(radio_sink=T.CountingRadioSink(), rx_readiness=T.ReadyReadiness())
    p = parent(ab)
    result = ab.transmit(p)
    exported = E.build_evidence([p], O.observe_scene([p]),
                                broadcast_results=(r for r in [result])).as_json_value()
    infra = exported["broadcast_infrastructure_result"]
    assert infra["submitted_child_count"] == 2
    assert infra["parent_outcomes"][p.parent_id] == "PENDING"
    assert all(row["readiness"]["proof"] for row in exported["delivery_matrix"])


def test_provider_default_evidence_preserves_successful_proof_until_cleanup():
    sink = T.CountingRadioSink()
    provider = SharedMediumActionProvider(radio_sink=sink, rx_readiness=T.ReadyReadiness(),
                                         allow_rf_off_defaults=True)
    action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                              schedule={"kind": "NEXT_SAFE_SLOT"}, payload={"packet_hex": "0102"})
    submitted = provider.submit(action, request_id="proof-action")
    assert provider.submit(action, request_id="proof-action") == submitted
    assert sink.commit_calls == 1
    p = provider.parent_for("proof-action")
    scene = O.observe_scene([p])
    provider.resolve_outcome("proof-action", {c.child_id: R.ChildTerminal.COMPLETED for c in p.children})
    ev = provider.build_evidence((x for x in [p]), scene)
    exported = ev.as_json_value()
    assert exported["broadcast_infrastructure_result"]["submitted_child_count"] == 2
    assert exported["broadcast_infrastructure_result"]["parent_outcomes"][p.parent_id] == "COMPLETED"
    roc = next(row for row in exported["delivery_matrix"] if row["endpoint"] == "ROC")
    assert roc["readiness"]["proof"]["slot_key"] == p.slot_key
    assert roc["interval_evidence"]["model"] == "TX_INTERVAL_DROP_V1"
    omitted = provider.build_evidence([p], scene, broadcast_results=()).as_json_value()
    assert all(row["readiness"] is None for row in omitted["delivery_matrix"])
    provider.cleanup()
    cleared = provider.build_evidence([p], scene).as_json_value()
    assert cleared["broadcast_infrastructure_result"]["submitted_child_count"] == 0
    assert all(row["readiness"] is None for row in cleared["delivery_matrix"])
    assert next(row for row in ev.as_json_value()["delivery_matrix"]
                if row["endpoint"] == "ROC")["readiness"] == roc["readiness"]


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("partial_commit", [False, True])
def test_unknown_commit_keeps_readiness_without_inventing_rx_or_committed_children(error_type, partial_commit):
    class FailingCommit(T.CountingRadioSink):
        def commit_group(self, admission):
            self.commit_calls += 1
            if partial_commit:
                self.committed_child_ids.append(admission.children[0].child_id)
            raise error_type("commit outcome unknown")

    sink = FailingCommit()
    provider = SharedMediumActionProvider(radio_sink=sink, rx_readiness=T.ReadyReadiness(),
                                         allow_rf_off_defaults=True)
    action = ActionDescriptor(kind=ActionKind.REPLAY_PACKET, target="ROC",
                              schedule={"kind": "NEXT_SAFE_SLOT"}, payload={"packet_hex": "0102"})
    with pytest.raises(PocError if error_type is RuntimeError else error_type):
        provider.submit(action, request_id="uncertain-proof")
    p = provider.parent_for("uncertain-proof")
    # No RX observations: readiness must not depend on a synthetic successful scene.
    empty_scene = O.observe_scene([])
    ev = provider.build_evidence([p], empty_scene)
    exported = ev.as_json_value()
    infra = exported["broadcast_infrastructure_result"]
    assert infra["parent_outcomes"][p.parent_id] == "OUTCOME_UNKNOWN"
    assert infra["committed_parent_ids"] == []
    assert infra["submitted_child_count"] == 0
    assert exported["delivery_matrix"] == []
    proofs = infra["readiness_by_parent"][p.parent_id]
    assert proofs["ROC"]["proof"]["slot_key"] == p.slot_key
    assert sink.submit_calls == int(partial_commit)
    row_ev = provider.build_evidence([p], O.observe_scene([p])).as_json_value()
    assert next(row for row in row_ev["delivery_matrix"] if row["endpoint"] == "ROC")["readiness"] == proofs["ROC"]
    proofs["ROC"]["proof"].clear()
    assert ev.as_json_value()["broadcast_infrastructure_result"]["readiness_by_parent"][p.parent_id]["ROC"]["proof"]
    active_copy = provider._broadcast.readiness_of(p.parent_id)
    active_copy["ROC"].proof.clear()
    assert provider._broadcast.readiness_of(p.parent_id)["ROC"].proof
    with pytest.raises(PocError) as again:
        provider.submit(action, request_id="uncertain-proof")
    assert again.value.code == PocErrorCode.POC_TX_OUTCOME_UNKNOWN
    assert sink.commit_calls == 1
    provider.cleanup()
    assert provider.build_evidence([p], empty_scene).as_json_value()["broadcast_infrastructure_result"]["readiness_by_parent"] == {}
