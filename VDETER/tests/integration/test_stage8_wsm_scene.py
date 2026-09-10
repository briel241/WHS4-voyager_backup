"""Stage 5 shared-medium: end-to-end scene 통합시험 (fan-out·tuned·collision·evidence).

RF-off 요구 매핑: 1(fan-out 행렬), 2(addressed 무관 물리 도달), 3(sender별 TUNED/NOT_TUNED),
4(NOT_TUNED no-bytes/event/error), 10(같은 채널·시간 중첩 합성), 11(다른 채널 비충돌),
13(정상 vs PoC 분리), 14(임의 packet 합성 0), 16(parent/path evidence 상관·redaction).
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel, TuningState
from vdeter.stage8.shared_medium import evidence as EV
from vdeter.stage8.shared_medium import observation as O
from vdeter.stage8.shared_medium import radiation as R
from vdeter.stage8.shared_medium import testing as TD
from vdeter.stage8.shared_medium import topology as T


def _ab():
    return R.AtomicBroadcast(
        radio_sink=TD.CountingRadioSink(),
        rx_readiness=TD.ReadyReadiness(),
        waveform_factory=R.ReferenceWaveformFactory(),
    )


def _parent(ab, sender, channel, payload, *, addressed=None, sample=0, slot="s"):
    return ab.build_parent(
        source_station=sender,
        channel=channel,
        payload=payload,
        scheduled_sample=sample,
        scheduled_hw_timestamp_ns=1000,
        slot_key=slot,
        addressed_destination=addressed,
    )


class NaturalMatrixSceneTest(unittest.TestCase):
    CASES = [
        ("ROC", PhysicalChannel.UPPER, "MASS-A"),
        ("MASS-A", PhysicalChannel.LOWER, "ROC"),
        ("MASS-A", PhysicalChannel.UPPER, "MASS-B"),
        ("MASS-B", PhysicalChannel.LOWER, "ROC"),
        ("MASS-B", PhysicalChannel.UPPER, "MASS-A"),
    ]

    def test_tuned_matrix_matches_spec_5_5(self):
        for sender, channel, addressed in self.CASES:
            ab = _ab()
            parent = _parent(ab, sender, channel, b"PKT-" + sender.encode(), addressed=addressed)
            scene = O.observe_scene([parent])
            self.assertTrue(
                O.matches_natural_matrix(parent, scene),
                f"{sender}/{channel.value}: tuned={sorted(scene.tuned_endpoints(parent.parent_id))} "
                f"expected={sorted(T.natural_tuned(sender, channel))}",
            )

    def test_physical_reach_independent_of_addressing(self):
        # addressed=None이어도 비송신 두 endpoint 모두 물리 도달(행 존재).
        for sender, channel, _addr in self.CASES:
            ab = _ab()
            parent = _parent(ab, sender, channel, b"NOADDR", addressed=None)
            scene = O.observe_scene([parent])
            reached = {r.endpoint for r in scene.rows_for(parent.parent_id) if r.physical_reach}
            self.assertEqual(reached, set(T.physical_reach(sender)))


class NotTunedBehaviorTest(unittest.TestCase):
    """WSM-REQ-4: NOT_TUNED는 실패가 아니며 bytes·event·error를 만들지 않는다."""

    def test_not_tuned_endpoint_has_no_bytes_event_error(self):
        # MASS-A -> ROC(lower): MASS-B는 role upper라 NOT_TUNED.
        ab = _ab()
        parent = _parent(ab, "MASS-A", PhysicalChannel.LOWER, b"PKT-LOWER", addressed="ROC")
        scene = O.observe_scene([parent])  # 예외 없이 완료(no-error)
        mass_b_rows = [r for r in scene.rows if r.endpoint == "MASS-B"]
        self.assertEqual(len(mass_b_rows), 1)
        row = mass_b_rows[0]
        self.assertEqual(row.tuning_state, TuningState.NOT_TUNED.value)
        self.assertTrue(row.physical_reach)          # 물리 도달은 했다
        self.assertIsNone(row.decode_status)         # decode 안 함
        self.assertIsNone(row.decoded_payload_digest)  # bytes 합성 0
        self.assertIsNone(row.observation_id)        # event 합성 0
        self.assertFalse(row.public_delivery)
        self.assertGreaterEqual(scene.not_tuned_counter, 1)
        self.assertEqual(scene.poc_private_observations, ())


class AddressAndPrivateObservationTest(unittest.TestCase):
    def test_addressed_target_public_delivery(self):
        ab = _ab()
        parent = _parent(ab, "MASS-B", PhysicalChannel.LOWER, b"ATTACK-ROC", addressed="ROC")
        scene = O.observe_scene([parent])
        roc = [r for r in scene.rows if r.endpoint == "ROC"][0]
        self.assertEqual(roc.tuning_state, TuningState.TUNED_OBSERVER.value)
        self.assertEqual(roc.decode_status, "RECOVERED")
        self.assertTrue(roc.public_delivery)  # 주소 대상만 public
        self.assertIsNone(roc.observation_id)  # 주소 대상은 PoC-private event 아님

    def test_mass_b_dual_channel_private_observation(self):
        # MASS-A -> ROC(lower). dual-channel POC_ACTOR MASS-B가 lower 추가 관측(§5.5).
        ab = _ab()
        parent = _parent(ab, "MASS-A", PhysicalChannel.LOWER, b"OBSERVE-ME", addressed="ROC")
        scene_off = O.observe_scene([parent], dual_channel_active=False)
        scene_on = O.observe_scene([parent], dual_channel_active=True)
        # dual off: MASS-B는 NOT_TUNED, private observation 없음.
        self.assertNotIn("MASS-B", scene_off.tuned_endpoints(parent.parent_id))
        self.assertEqual(scene_off.poc_private_observations, ())
        # dual on: MASS-B가 lower TUNED, 비주소 -> PoC-private observation 1건, public 아님.
        self.assertIn("MASS-B", scene_on.tuned_endpoints(parent.parent_id))
        self.assertEqual(len(scene_on.poc_private_observations), 1)
        mass_b = [r for r in scene_on.rows if r.endpoint == "MASS-B"][0]
        self.assertEqual(mass_b.decode_status, "RECOVERED")
        self.assertFalse(mass_b.public_delivery)
        self.assertIsNotNone(mass_b.observation_id)

    def test_dual_channel_does_not_change_roc_massa(self):
        ab = _ab()
        parent = _parent(ab, "MASS-A", PhysicalChannel.LOWER, b"OBSERVE-ME", addressed="ROC")
        on = O.observe_scene([parent], dual_channel_active=True)
        roc = [r for r in on.rows if r.endpoint == "ROC"][0]
        self.assertEqual(roc.tuning_state, TuningState.TUNED_OBSERVER.value)
        self.assertTrue(roc.public_delivery)


class SceneCollisionTest(unittest.TestCase):
    """WSM-REQ-5/7/11/14: 같은 채널·시간이 겹치는 두 송신만 충돌; decoder가 결과 결정."""

    def test_same_channel_time_overlap_collides(self):
        ab = _ab()
        p_roc = _parent(ab, "ROC", PhysicalChannel.UPPER, b"WSM-ROC-UP-0001", addressed="MASS-A", sample=0)
        p_ma = _parent(ab, "MASS-A", PhysicalChannel.UPPER, b"WSM-MSA-UP-0002", addressed="MASS-B", sample=0)
        scene = O.observe_scene([p_roc, p_ma])
        # MASS-B는 upper에서 두 arrival을 받아 충돌(다른 payload -> decode 실패, packet 합성 0).
        mass_b = [r for r in scene.rows if r.endpoint == "MASS-B"]
        self.assertEqual(len(mass_b), 2)
        for row in mass_b:
            self.assertEqual(row.tuning_state, TuningState.TUNED_OBSERVER.value)
            self.assertEqual(row.collision_input_count, 2)
            self.assertNotEqual(row.decode_status, "RECOVERED")
            self.assertIsNone(row.decoded_payload_digest)  # 임의 packet/payload 합성 0
            self.assertIsNone(row.observation_id)

    def test_different_channel_not_collided(self):
        ab = _ab()
        p_roc = _parent(ab, "ROC", PhysicalChannel.UPPER, b"UPPERPKT", addressed="MASS-A", sample=0)
        p_mb = _parent(ab, "MASS-B", PhysicalChannel.LOWER, b"LOWERPKT", addressed="ROC", sample=0)
        scene = O.observe_scene([p_roc, p_mb])
        # MASS-A는 p_roc(upper, TUNED)와 p_mb(lower, NOT_TUNED)를 받는다: 서로 충돌 아님.
        mass_a = [r for r in scene.rows if r.endpoint == "MASS-A"]
        by_channel = {r.channel: r for r in mass_a}
        self.assertEqual(by_channel[PhysicalChannel.UPPER].collision_input_count, 1)
        self.assertEqual(by_channel[PhysicalChannel.UPPER].decode_status, "RECOVERED")
        self.assertEqual(by_channel[PhysicalChannel.LOWER].tuning_state, TuningState.NOT_TUNED.value)


class EvidenceCorrelationTest(unittest.TestCase):
    def test_delivery_matrix_correlates_to_parent_and_redacts_payload(self):
        ab = _ab()
        parent = _parent(ab, "MASS-B", PhysicalChannel.LOWER, b"SECRET-PAYLOAD-XY", addressed="ROC")
        result = ab.transmit(parent)
        scene = O.observe_scene([parent])
        ev = EV.build_evidence([parent], scene, broadcast_results=[result])
        j = ev.as_json_value()
        parent_ids = {p["parent_id"] for p in j["broadcast_infrastructure_result"]["parents"]}
        for row in j["delivery_matrix"]:
            self.assertIn(row["parent_id"], parent_ids)  # path<->parent correlation
        # redaction: raw payload가 evidence에 없다(digest만).
        blob = repr(j)
        self.assertNotIn("SECRET-PAYLOAD-XY", blob)
        self.assertIn(parent.payload_digest, blob)
        self.assertEqual(j["broadcast_infrastructure_result"]["submitted_child_count"], 2)


class DeliveryMatrixCapTest(unittest.TestCase):
    """WSM-REQ-17 bounded: delivery matrix가 무제한 증가하지 않는다(observe_scene + evidence)."""

    def test_observe_scene_caps_rows_with_dropped_counter(self):
        import unittest.mock as mock

        # 작은 상한으로 강제: 2 parent(ROC upper) -> 4 row > cap 2 -> dropped 2.
        with mock.patch.object(O, "MAX_DELIVERY_MATRIX_ENTRIES", 2):
            parents = []
            for i in range(2):
                ab = _ab()
                parents.append(_parent(ab, "ROC", PhysicalChannel.UPPER, b"PKT%d" % i, addressed="MASS-A"))
            scene = O.observe_scene(parents)
            self.assertEqual(len(scene.rows), 2)
            self.assertTrue(scene.delivery_matrix_truncated)
            self.assertEqual(scene.dropped_rows, 2)

    def test_build_evidence_caps_serialized_matrix(self):
        import unittest.mock as mock

        ab = _ab()
        parent = _parent(ab, "ROC", PhysicalChannel.UPPER, b"PKT", addressed="MASS-A")
        result = ab.transmit(parent)
        scene = O.observe_scene([parent])  # 2 rows
        with mock.patch.object(EV, "MAX_DELIVERY_MATRIX_ENTRIES", 1):
            j = EV.build_evidence([parent], scene, broadcast_results=[result]).as_json_value()
        self.assertEqual(len(j["delivery_matrix"]), 1)
        infra = j["broadcast_infrastructure_result"]
        self.assertTrue(infra["delivery_matrix_truncated"])
        self.assertEqual(infra["delivery_matrix_dropped_rows"], 1)


if __name__ == "__main__":
    unittest.main()
