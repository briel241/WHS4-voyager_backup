"""P12: 수신 창 경계의 실제 합성 입력(명세 §7.1~§7.3).

단일 입력 보존 · 부분/전체 overlap · NOT_TUNED · 중복 합산 방지 · 단일 decode 경계를 고정한다.
결과는 결정적 근사 모델이며 실 ADC 포화를 증명하지 않는다 — evidence가 그렇게 표시하는지도 본다.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api.types import PhysicalChannel
from vdeter.stage8.shared_medium import frontend
from vdeter.stage8.shared_medium.iq import to_cf32le
from vdeter.stage8.shared_medium.rx_boundary import (
    MAX_WINDOW_ARRIVALS,
    RxArrival,
    RxBoundaryError,
    compose_rx_window,
)

_RATE = 307_200
_COUNT = 64
_NS_PER_SAMPLE = 1_000_000_000 / _RATE
_CENTER = 2_447_237_500


def _tone(value: complex, count: int = _COUNT) -> bytes:
    return to_cf32le([value] * count)


def _arrival(arrival_id, *, at_ns, data, center=_CENTER):
    return RxArrival(arrival_id=arrival_id, receiver_hw_ns=at_ns,
                     effective_center_hz=center, data=data)


def _compose(arrivals, *, start_ns=0, count=_COUNT, center=_CENTER):
    return compose_rx_window(arrivals=arrivals, window_start_ns=start_ns,
                             sample_count=count, sample_rate_hz=_RATE,
                             tuned_center_hz=center, channel=PhysicalChannel.LOWER)


class SingleInputTest(unittest.TestCase):
    def test_single_aligned_input_is_preserved_byte_identically(self):
        """단일 입력은 합산·clip 없이 그대로다 — 없는 왜곡을 만들지 않는다."""
        data = _tone(0.5 + 0.25j)
        result = _compose([_arrival("a", at_ns=0, data=data)])
        self.assertEqual(result.data, data)
        self.assertEqual(result.input_count, 1)
        self.assertEqual(result.overlap_sample_count, 0)
        self.assertEqual(result.clip_count, 0)

    def test_no_arrival_yields_silence_not_a_fabricated_receive(self):
        result = _compose([])
        self.assertEqual(result.data, b"\x00" * (_COUNT * 8))
        self.assertEqual(result.input_count, 0)


class OverlapTest(unittest.TestCase):
    def test_full_overlap_sums_linearly(self):
        """완전히 겹치면 복소 선형 합산이다(§7.1)."""
        result = _compose([_arrival("a", at_ns=0, data=_tone(0.1 + 0.0j)),
                           _arrival("b", at_ns=0, data=_tone(0.2 + 0.0j))])
        self.assertEqual(result.input_count, 2)
        self.assertEqual(result.overlap_sample_count, _COUNT)
        # 0.1 + 0.2 = 0.3 (full-scale 1.0 미만이라 clip 없음)
        self.assertEqual(result.clip_count, 0)
        self.assertAlmostEqual(result.post_clip_peak, 0.3, places=5)

    def test_partial_overlap_sums_only_the_overlapping_samples(self):
        """부분 겹침은 겹친 구간만 합산하고 나머지는 단일 입력 그대로다."""
        half = _COUNT // 2
        shift_ns = int(half * _NS_PER_SAMPLE)
        result = _compose([_arrival("a", at_ns=0, data=_tone(0.1 + 0.0j)),
                           _arrival("b", at_ns=shift_ns, data=_tone(0.2 + 0.0j))])
        self.assertEqual(result.input_count, 2)
        self.assertEqual(result.overlap_sample_count, half)

    def test_sum_beyond_full_scale_is_clipped_not_normalised(self):
        """full-scale을 넘으면 고정 virtual frontend로 포화시킨다 — 자동 정규화 금지(§7.2)."""
        result = _compose([_arrival("a", at_ns=0, data=_tone(0.8 + 0.0j)),
                           _arrival("b", at_ns=0, data=_tone(0.8 + 0.0j))])
        self.assertGreater(result.clip_count, 0)
        self.assertAlmostEqual(result.pre_clip_peak, 1.6, places=5)
        self.assertAlmostEqual(result.post_clip_peak, frontend.FULL_SCALE, places=6)

    def test_non_overlapping_arrival_outside_the_window_is_rejected(self):
        far = int(_COUNT * 4 * _NS_PER_SAMPLE)
        result = _compose([_arrival("a", at_ns=0, data=_tone(0.1 + 0.0j)),
                           _arrival("late", at_ns=far, data=_tone(0.2 + 0.0j))])
        self.assertEqual(result.input_count, 1)
        self.assertIn(("late", "OUTSIDE_WINDOW"), result.rejected)


class ChannelSeparationTest(unittest.TestCase):
    def test_other_channel_arrival_is_not_tuned_and_never_summed(self):
        """다른 채널 IQ는 섞지 않는다. NOT_TUNED는 수신 실패가 아니라 정상 상태다(§5.5)."""
        result = _compose([_arrival("same", at_ns=0, data=_tone(0.1 + 0.0j)),
                           _arrival("other", at_ns=0, data=_tone(0.9 + 0.0j),
                                    center=_CENTER + 4_600_000)])
        self.assertEqual(result.input_count, 1)
        self.assertEqual(result.not_tuned_count, 1)
        self.assertIn(("other", "NOT_TUNED"), result.rejected)
        self.assertEqual(result.clip_count, 0)

    def test_untuned_lane_receives_nothing(self):
        result = compose_rx_window(
            arrivals=[_arrival("a", at_ns=0, data=_tone(0.5 + 0.0j))],
            window_start_ns=0, sample_count=_COUNT, sample_rate_hz=_RATE,
            tuned_center_hz=None, channel=PhysicalChannel.LOWER)
        self.assertEqual(result.input_count, 0)
        self.assertEqual(result.not_tuned_count, 1)


class DuplicateAndBoundsTest(unittest.TestCase):
    def test_same_arrival_is_not_summed_twice(self):
        """같은 도달이 두 번 들어와도 한 번만 합산된다(중복 합산 금지)."""
        data = _tone(0.3 + 0.0j)
        result = _compose([_arrival("dup", at_ns=0, data=data),
                           _arrival("dup", at_ns=0, data=data)])
        self.assertEqual(result.input_count, 1)
        self.assertEqual(result.data, data)            # 0.6이 아니라 0.3 그대로
        self.assertIn(("dup", "DUPLICATE"), result.rejected)

    def test_arrival_count_is_bounded(self):
        arrivals = [_arrival(f"a{i}", at_ns=0, data=_tone(0.01 + 0.0j))
                    for i in range(MAX_WINDOW_ARRIVALS + 2)]
        with self.assertRaises(RxBoundaryError):
            _compose(arrivals)


class EvidenceTest(unittest.TestCase):
    def test_evidence_marks_the_model_boundary(self):
        """모델 결과를 실 RF 수신으로 주장하지 않는다."""
        result = _compose([_arrival("a", at_ns=0, data=_tone(0.9 + 0.0j)),
                           _arrival("b", at_ns=0, data=_tone(0.9 + 0.0j))])
        evidence = result.as_evidence()
        self.assertEqual(evidence["model"], "WIRED_SHARED_MEDIUM_EMULATION")
        self.assertEqual(evidence["virtual_clipping"], "deterministic_approximation")
        self.assertFalse(evidence["analog_adc_saturation_proven"])
        self.assertEqual(evidence["frontend_version"], frontend.VIRTUAL_FRONTEND_VERSION)
        # 소수는 문자열로 싣는다(canonical JSON은 float를 거부한다).
        self.assertIsInstance(evidence["pre_clip_peak"], str)
        self.assertGreater(evidence["clip_count"], 0)


if __name__ == "__main__":
    unittest.main()
