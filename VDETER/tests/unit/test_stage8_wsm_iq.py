"""Stage 5 shared-medium: IQ 표현·합성·frontend·참조 decoder 단위시험.

RF-off 요구 매핑: 5(같은 채널·시간 중첩 IQ 합성), 10, 11(다른 채널·비중첩 비충돌),
14(payload/packet 임의 합성 0), 15(frozen decoder 결과 유지), 5-hard-gate(단일 burst 동일성),
17(bounded resource).
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium import decoder as D
from vdeter.stage8.shared_medium import frontend as F
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
        self.assertLess(F.axis_peak(burst.samples), F.FULL_SCALE)

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
    """WSM-G3 hard gate(§11.1-5): 비겹침 단일 burst는 합성 전후 IQ·decode가 동일해야 한다."""

    def test_single_burst_byte_identical_through_synthesis(self):
        burst = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.synthesize_same_channel([burst])
        self.assertEqual(composite.cf32le, burst.cf32le)  # byte-identical
        self.assertEqual(composite.clip_count, 0)
        self.assertEqual(composite.overlap_sample_count, 0)

    def test_single_burst_decode_unchanged_through_synthesis(self):
        burst = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.synthesize_same_channel([burst])
        before = D.decode(burst.samples)
        after = D.decode(composite.samples)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.payload, before.payload)
        self.assertEqual(after.payload, P1)


class LinearSumAndClipTest(unittest.TestCase):
    def test_overlap_is_linear_sum(self):
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.3, -0.2),))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.4, 0.1),))
        composite = IQ.synthesize_same_channel([a, b])
        self.assertEqual(composite.samples[0], complex(0.3, -0.2) + complex(0.4, 0.1))
        self.assertEqual(composite.overlap_sample_count, 1)
        self.assertEqual(composite.clip_count, 0)

    def test_composite_clips_per_axis_when_exceeding_full_scale(self):
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.8, 0.8),))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.8, 0.8),))
        composite = IQ.synthesize_same_channel([a, b])
        self.assertEqual(composite.samples[0], complex(F.FULL_SCALE, F.FULL_SCALE))
        self.assertEqual(composite.clip_count, 1)
        self.assertAlmostEqual(composite.pre_clip_peak, 1.6, places=5)
        self.assertAlmostEqual(composite.post_clip_peak, 1.0, places=5)

    def test_no_normalization_when_within_full_scale(self):
        # binary-exact 값으로 선형 합산이 정규화/scaling 없이 그대로임을 검증한다.
        a = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.125, 0.125),))
        b = IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.25, 0.25),))
        composite = IQ.synthesize_same_channel([a, b])
        self.assertEqual(composite.samples[0], complex(0.375, 0.375))
        self.assertEqual(composite.clip_count, 0)


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
            IQ.synthesize_same_channel([a, b])


class CollisionDecodeTest(unittest.TestCase):
    """WSM-REQ-07/15: 충돌 성공·실패는 frozen decoder가 composite로 결정한다."""

    def test_identical_overlap_captures_same_payload(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        composite = IQ.synthesize_same_channel([a, b])
        self.assertGreater(composite.clip_count, 0)
        r = D.decode(composite.samples)
        self.assertEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertEqual(r.payload, P1)

    def test_different_overlap_fails_decode_no_synthetic_packet(self):
        a = D.build_burst(PhysicalChannel.LOWER, 0, P1)
        b = D.build_burst(PhysicalChannel.LOWER, 0, P3)
        r = D.decode(IQ.synthesize_same_channel([a, b]).samples)
        self.assertNotEqual(r.status, D.DecodeStatus.RECOVERED)
        self.assertIsNone(r.payload)  # payload를 임의로 합성하지 않는다


class BoundedResourceTest(unittest.TestCase):
    def test_collision_group_cap(self):
        from vdeter.stage8.shared_medium.constants import MAX_COLLISION_GROUP_BURSTS

        bursts = [
            IQ.IqBurst(PhysicalChannel.LOWER, 0, (complex(0.1, 0.0),))
            for _ in range(MAX_COLLISION_GROUP_BURSTS + 1)
        ]
        with self.assertRaises(IQ.IqError):
            IQ.synthesize_same_channel(bursts)


if __name__ == "__main__":
    unittest.main()
