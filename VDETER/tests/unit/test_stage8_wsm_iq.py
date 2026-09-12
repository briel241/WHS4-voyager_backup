"""Stage 5 shared-medium: IQ 표현·비충돌 복사·frontend·참조 decoder 단위시험.

RF-off 요구 매핑: 5(같은 채널·시간 중첩 IQ 비충돌 복사), 10, 11(다른 채널·비중첩 비충돌),
14(payload/packet 임의 비충돌 복사 0), 15(frozen decoder 결과 유지), 5-hard-gate(단일 burst 동일성),
17(bounded resource).
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium import decoder as D
from vdeter.stage8.shared_medium import iq as IQ

P1 = b"WSM-POS-ROC-0001"
P3 = b"WSM-NEG-ALT--003"


class DecoderRoundtripTest(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        r = D.decode(D.encode_payload(P1))
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, P1)

    def test_empty_payload_roundtrip(self):
        r = D.decode(D.encode_payload(b""))
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, b"")

    def test_single_burst_axis_peak_within_full_scale(self):
        burst = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        self.assertLess(max(max(abs(x.real), abs(x.imag)) for x in burst.samples), 1.0)

    def test_too_short_is_no_burst(self):
        self.assertEqual(D.decode((0j,) * 4).status, D.DecodeStatus.NO_BURST_DETECTED)


class Cf32leSerializationTest(unittest.TestCase):
    def test_roundtrip_bytes(self):
        # cf32le는 float32이므로 bytes 왕복이 안정적인지(byte-pinning 불변)를 검증한다.
        data = IQ.to_cf32le(D.encode_payload(P1))
        self.assertEqual(IQ.to_cf32le(IQ.from_cf32le(data)), data)

    def test_bytes_per_sample(self):
        samples = D.encode_payload(P1)
        self.assertEqual(len(IQ.to_cf32le(samples)), len(samples) * IQ.BYTES_PER_SAMPLE)

    def test_sha256_stable(self):
        samples = D.encode_payload(P1)
        self.assertEqual(IQ.sha256_cf32le(samples), IQ.sha256_cf32le(D.encode_payload(P1)))


class SingleBurstHardGateTest(unittest.TestCase):
    """WSM-G3 hard gate(§11.1-5): 비겹침 단일 burst는 비충돌 복사 전후 IQ·decode가 동일해야 한다."""

    def test_single_burst_byte_identical_through_synthesis(self):
        burst = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.render_noncolliding([burst])
        self.assertEqual(composite.cf32le, burst.cf32le)  # byte-identical
        self.assertEqual(composite.dropped_input_count, 0)
        self.assertEqual(composite.overlap_sample_count, 0)

    def test_single_burst_decode_unchanged_through_synthesis(self):
        burst = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.render_noncolliding([burst])
        before = D.decode(burst.samples)
        after = D.decode(composite.samples)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.payload, before.payload)
        self.assertEqual(after.payload, P1)


class ConservativeDropTest(unittest.TestCase):
    def test_overlap_drops_entire_both_bursts(self):
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (0.3+0j, 0.3+0j))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 1, (0.1+0j, 0.1+0j))
        result = IQ.render_noncolliding([a, b])
        self.assertEqual(result.samples, (0j, 0j, 0j))
        self.assertEqual(result.dropped_input_count, 2)

    def test_strong_signal_is_not_captured(self):
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (100+100j,))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 0, (0.0001+0j,))
        self.assertEqual(IQ.render_noncolliding([a, b]).samples, (0j,))

    def test_adjacent_no_overlap_preserves_both(self):
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (0.125+0.125j,))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 1, (0.25+0.25j,))
        self.assertEqual(IQ.render_noncolliding([a, b]).samples, a.samples + b.samples)


class CollisionGroupingTest(unittest.TestCase):
    def test_same_channel_overlap_is_one_group(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P3)
        self.assertEqual(len(IQ.group_collisions([a, b])), 1)

    def test_different_channel_is_not_collision(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.UPPER, 0, P3)
        self.assertEqual(len(IQ.group_collisions([a, b])), 2)

    def test_non_overlap_same_channel_is_not_collision(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, len(a.samples) + 10, P3)
        self.assertFalse(IQ.time_overlaps(a, b))
        self.assertEqual(len(IQ.group_collisions([a, b])), 2)

    def test_transitive_overlap_is_one_group(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 40, P3)
        c = D.build_burst(PhysicalChannel.LOWER, 80, P1)
        self.assertEqual(len(IQ.group_collisions([a, b, c])), 1)

    def test_mixed_channel_synthesis_rejected(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.UPPER, 0, P3)
        with self.assertRaises(IQ.IqError):
            IQ.render_noncolliding([a, b])


class CollisionDecodeTest(unittest.TestCase):
    """v1.4: collided inputs never reach a successful synthetic decode."""

    def test_identical_overlap_is_also_dropped(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.render_noncolliding([a, b])
        self.assertGreater(composite.dropped_input_count, 0)
        r = D.decode(composite.samples)
        self.assertNotEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertIsNone(r.payload)

    def test_different_overlap_fails_decode_no_synthetic_packet(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P3)
        r = D.decode(IQ.render_noncolliding([a, b]).samples)
        self.assertNotEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertIsNone(r.payload)  # payload를 임의로 비충돌 복사하지 않는다


class BoundedResourceTest(unittest.TestCase):
    def test_collision_group_cap(self):
        from vdeter.stage8.shared_medium.constants import MAX_COLLISION_GROUP_BURSTS

        bursts = [
            IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.1, 0.0),))
            for _ in range(MAX_COLLISION_GROUP_BURSTS + 1)
        ]
        with self.assertRaises(IQ.IqError):
            IQ.render_noncolliding(bursts)


if __name__ == "__main__":
    unittest.main()
