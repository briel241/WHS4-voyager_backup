"""Stage 5 shared-medium: atomic broadcast·single-burst invariant·fan-out·cleanup 계약시험.

RF-off 요구 매핑: 1(fan-out), 8(action1->parent1->child2), 9(중복 submit 0),
16(parent/path correlation), 18(cleanup 잔존 0), + single-burst invariant hard gate,
atomic barrier fail-closed(§6.2), sibling 자기충돌 금지(12).
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium import constants as C
from vdeter.stage8.shared_medium import observation as O
from vdeter.stage8.shared_medium import radiation as R
from vdeter.stage8.shared_medium import testing as TD


def _broadcast(sink=None, readiness=None, factory=None):
    return R.AtomicBroadcast(
        radio_sink=sink or TD.CountingRadioSink(),
        rx_readiness=readiness or TD.ReadyReadiness(),
        waveform_factory=factory or TD.CountingWaveformFactory(),
    )


def _parent(ab, sender="MASS-B", channel=PhysicalChannel.LOWER, addressed="ROC"):
    return ab.build_parent(
        source_station=sender,
        channel=channel,
        payload=b"WSM-PARENT-0001",
        scheduled_sample=0,
        scheduled_hw_timestamp_ns=1000,
        slot_key="slot-1",
        addressed_destination=addressed,
    )


class SingleBurstInvariantTest(unittest.TestCase):
    """독립 contract: 단일 논리 burst = 하나의 physical parent, 정확히 2 child."""

    def test_exactly_one_parent_two_children(self):
        ab = _broadcast()
        parent = _parent(ab)
        self.assertEqual(len(parent.children), C.FANOUT_CHILDREN)
        self.assertEqual(len(parent.children), 2)

    def test_children_share_parent_digest_time_channel(self):
        ab = _broadcast()
        parent = _parent(ab)
        for child in parent.children:
            self.assertEqual(child.parent_id, parent.parent_id)
            self.assertEqual(child.transfer_id, parent.transfer_id)
            self.assertEqual(child.payload_digest, parent.payload_digest)
            self.assertEqual(child.waveform_sha256, parent.waveform_sha256)
            self.assertEqual(child.scheduled_sample, parent.scheduled_sample)
            self.assertEqual(child.channel, parent.channel)

    def test_children_distinct_cables_and_receivers(self):
        ab = _broadcast()
        parent = _parent(ab)
        self.assertEqual({c.cable for c in parent.children}, {"C4", "C6"})
        self.assertEqual({c.rx_station for c in parent.children}, {"ROC", "MASS-A"})

    def test_waveform_generated_once_for_fanout(self):
        factory = TD.CountingWaveformFactory()
        ab = _broadcast(factory=factory)
        _parent(ab)
        self.assertEqual(factory.encode_calls, 1)  # fan-out이 waveform을 재생성하지 않는다


class ExactlyOnceSubmitTest(unittest.TestCase):
    def test_transmit_submits_each_child_once(self):
        sink = TD.CountingRadioSink()
        ab = _broadcast(sink=sink)
        parent = _parent(ab)
        result = ab.transmit(parent)
        self.assertTrue(result.committed)
        self.assertEqual(sink.submit_calls, 2)
        self.assertEqual(sink.duplicate_child_submits, 0)
        self.assertEqual(len(sink.submitted_parents[parent.parent_id]), 2)

    def test_double_transmit_same_parent_rejected(self):
        ab = _broadcast()
        parent = _parent(ab)
        ab.transmit(parent)
        with self.assertRaises(R.BroadcastAtomicityError):
            ab.transmit(parent)


class AtomicBarrierTest(unittest.TestCase):
    """§6.2: required receiver 미준비면 RF 전 fail-closed(submit 0)."""

    def test_required_receiver_not_ready_fails_closed(self):
        sink = TD.CountingRadioSink()
        ab = _broadcast(sink=sink, readiness=TD.SelectiveReadiness(not_ready_endpoints={"ROC"}))
        parent = _parent(ab, addressed="ROC")
        with self.assertRaises(R.BroadcastNotReady):
            ab.transmit(parent)
        self.assertEqual(sink.submit_calls, 0)  # 어느 child도 submit되지 않음
        self.assertEqual(ab.active_group_count, 0)

    def test_non_target_other_channel_endpoint_not_required(self):
        # MASS-B -> ROC(lower): ROC만 required; MASS-A(upper role)는 NOT_TUNED이라 non-blocking.
        ab = _broadcast()
        parent = _parent(ab, addressed="ROC")
        required = ab.required_receivers(parent)
        self.assertIn("ROC", required)
        self.assertNotIn("MASS-A", required)

    def test_prepare_group_failure_fires_no_child(self):
        # group staging 실패 -> 어느 child도 방사되지 않는다(atomic).
        sink = TD.CountingRadioSink(fail_on_prepare=True)
        ab = _broadcast(sink=sink)
        parent = _parent(ab)
        with self.assertRaises(RuntimeError):
            ab.transmit(parent)
        self.assertEqual(sink.commit_calls, 0)
        self.assertEqual(sink.submit_calls, 0)         # 방사 child 0
        self.assertEqual(ab.active_group_count, 0)

    def test_commit_group_failure_marks_outcome_unknown_no_partial(self):
        # commit 결과 불명확 -> 성공으로 만들지 않고 OUTCOME_UNKNOWN, 부분 방사 없음(§6.1).
        sink = TD.CountingRadioSink(fail_on_commit=True)
        ab = _broadcast(sink=sink)
        parent = _parent(ab)
        with self.assertRaises(R.BroadcastAtomicityError):
            ab.transmit(parent)
        self.assertEqual(sink.committed_child_ids, [])  # 한쪽만 방사되는 partial 상태 없음
        self.assertEqual(ab.outcome_of(parent.parent_id), R.ParentOutcome.OUTCOME_UNKNOWN)

    def test_no_per_child_submit_api(self):
        # RadioBurstSink에는 per-child submit 경로가 없다(구조적으로 partial 불가).
        sink = TD.CountingRadioSink()
        self.assertFalse(hasattr(sink, "submit_child"))
        ab = _broadcast(sink=sink)
        ab.transmit(_parent(ab))
        self.assertEqual(sink.commit_calls, 1)  # 하나의 atomic group commit
        self.assertEqual(sink.submit_calls, 2)  # 두 child 함께 방사


class ParentTerminalAggregationTest(unittest.TestCase):
    """§6.1: 두 child terminal을 취합해야 parent outcome이 확정된다."""

    def _committed(self):
        ab = _broadcast()
        parent = _parent(ab)
        result = ab.transmit(parent)
        self.assertEqual(result.outcome, R.ParentOutcome.PENDING)  # commit 직후엔 미확정
        return ab, parent

    def test_all_completed_is_completed(self):
        ab, parent = self._committed()
        terminals = {c.child_id: R.ChildTerminal.COMPLETED for c in parent.children}
        self.assertEqual(ab.resolve_outcome(parent.parent_id, terminals), R.ParentOutcome.COMPLETED)
        self.assertEqual(ab.active_group_count, 0)

    def test_one_unknown_is_outcome_unknown(self):
        ab, parent = self._committed()
        c0, c1 = parent.children
        terminals = {c0.child_id: R.ChildTerminal.COMPLETED, c1.child_id: R.ChildTerminal.UNKNOWN}
        self.assertEqual(ab.resolve_outcome(parent.parent_id, terminals), R.ParentOutcome.OUTCOME_UNKNOWN)

    def test_missing_terminal_is_outcome_unknown(self):
        ab, parent = self._committed()
        c0, _c1 = parent.children
        terminals = {c0.child_id: R.ChildTerminal.COMPLETED}  # 한 child terminal 누락
        self.assertEqual(ab.resolve_outcome(parent.parent_id, terminals), R.ParentOutcome.OUTCOME_UNKNOWN)

    def test_failed_without_unknown_is_failed(self):
        ab, parent = self._committed()
        c0, c1 = parent.children
        terminals = {c0.child_id: R.ChildTerminal.COMPLETED, c1.child_id: R.ChildTerminal.FAILED}
        self.assertEqual(ab.resolve_outcome(parent.parent_id, terminals), R.ParentOutcome.FAILED)


class CapacityTest(unittest.TestCase):
    """§10 bounded: 동시 활성 broadcast group 상한 적용."""

    def test_max_active_broadcast_groups_enforced(self):
        ab = R.AtomicBroadcast(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=R.ReferenceWaveformFactory(), max_active_groups=2,
        )
        p1 = _parent(ab); p2 = _parent(ab); p3 = _parent(ab)
        ab.transmit(p1); ab.transmit(p2)
        self.assertEqual(ab.active_group_count, 2)
        with self.assertRaises(R.BroadcastCapacityError):
            ab.transmit(p3)
        # 하나 resolve하면 자리가 나서 다시 방사 가능.
        ab.resolve_outcome(p1.parent_id, {c.child_id: R.ChildTerminal.COMPLETED for c in p1.children})
        ab.transmit(p3)
        self.assertEqual(ab.active_group_count, 2)


class CleanupTest(unittest.TestCase):
    def test_cleanup_leaves_zero_active_groups(self):
        ab = _broadcast()
        p1 = _parent(ab)
        ab.transmit(p1)
        self.assertEqual(ab.active_group_count, 1)
        ab.cleanup()
        self.assertEqual(ab.active_group_count, 0)

    def test_release_removes_group(self):
        ab = _broadcast()
        parent = _parent(ab)
        ab.transmit(parent)
        ab.release(parent.parent_id)
        self.assertEqual(ab.active_group_count, 0)


class SiblingSelfCollisionGuardTest(unittest.TestCase):
    """WSM-REQ-12: fan-out sibling(같은 parent)은 같은 collision group에 들어갈 수 없다."""

    def test_build_arrivals_rejects_two_children_to_same_receiver(self):
        ab = _broadcast()
        parent = _parent(ab)
        # 인위적으로 두 child를 같은 receiver로 바꾼 parent를 만든다(불변 위반 상황을 강제).
        bad_children = (
            parent.children[0],
            R.ChildPath(
                child_id=parent.children[1].child_id,
                parent_id=parent.parent_id,
                transfer_id=parent.transfer_id,
                cable=parent.children[1].cable,
                tx_station=parent.children[1].tx_station,
                tx_lane=parent.children[1].tx_lane,
                rx_station=parent.children[0].rx_station,  # 같은 receiver로 강제
                rx_lane=parent.children[1].rx_lane,
                channel=parent.channel,
                payload_digest=parent.payload_digest,
                waveform_sha256=parent.waveform_sha256,
                scheduled_sample=parent.scheduled_sample,
                scheduled_hw_timestamp_ns=parent.scheduled_hw_timestamp_ns,
                samples=parent.waveform.samples,
            ),
        )
        bad_parent = R.ParentTransmission(
            parent_id=parent.parent_id,
            transfer_id=parent.transfer_id,
            source_station=parent.source_station,
            channel=parent.channel,
            addressed_destination=parent.addressed_destination,
            official_broadcast=parent.official_broadcast,
            payload=parent.payload,
            payload_digest=parent.payload_digest,
            waveform_sha256=parent.waveform_sha256,
            waveform=parent.waveform,
            scheduled_sample=parent.scheduled_sample,
            scheduled_hw_timestamp_ns=parent.scheduled_hw_timestamp_ns,
            slot_key=parent.slot_key,
            clock_id=parent.clock_id,
            bridge_generation=parent.bridge_generation,
            correction_revision=parent.correction_revision,
            children=bad_children,
        )
        with self.assertRaises(R.SiblingCollisionError):
            O.build_arrivals([bad_parent])

    def test_normal_fanout_never_sibling_collides(self):
        # 정상 build_parent는 서로 다른 receiver로 가므로 sibling 자기충돌이 불가능하다.
        ab = _broadcast()
        parent = _parent(ab)
        arrivals = O.build_arrivals([parent])
        for endpoint, items in arrivals.items():
            parent_ids = [a.parent_id for a in items]
            self.assertEqual(len(parent_ids), len(set(parent_ids)))


if __name__ == "__main__":
    unittest.main()
