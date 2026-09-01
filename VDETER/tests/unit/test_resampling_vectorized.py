"""`decimate_slot` 벡터 경로가 참조 구현과 같은 결과를 내는지 고정한다.

참조 이중 루프는 창당 2,048x97 = 198,656회 스칼라 복소 곱누산이라 Pi 5에서
39.5ms가 걸렸고, Stage 7 수신 파이프라인의 최대 단일 비용이었다. numpy
convolve로 같은 연산을 하면 2.1ms다.

합산 순서가 달라 부동소수 반올림 차이가 생긴다. 여기서 고정하는 것은
"차이가 반올림 수준을 넘지 않는다"와 "검증·조기탈출 계약이 그대로다"이다.

numpy가 없는 환경에서는 벡터 경로가 존재하지 않으므로 해당 시험을 건너뛴다
(참조 구현만으로도 동작해야 한다는 것이 계약이다).
"""

from __future__ import annotations

import math
import random
import unittest

from vdeter.phy import resampling

_HAS_NUMPY = resampling._numpy is not None


def _noise(seed: int, count: int, scale: float = 1e-3):
    rng = random.Random(seed)
    return tuple(
        complex(rng.gauss(0.0, scale), rng.gauss(0.0, scale))
        for _ in range(count)
    )


def _burst(seed: int, count: int):
    """잡음 바닥 위에 큰 진폭 구간이 있는 창 (버스트 모사)."""

    values = list(_noise(seed, count))
    for index in range(count // 3, count // 3 + 512):
        values[index] = values[index] * 100.0 + complex(0.05, -0.05)
    return tuple(values)


class DecimateEquivalence(unittest.TestCase):
    def _assert_close(self, reference, produced, label):
        self.assertEqual(len(reference), len(produced), label)
        worst = 0.0
        scale = max((abs(v) for v in reference), default=0.0)
        for want, got in zip(reference, produced):
            worst = max(worst, abs(want - got))
        # 반올림 수준을 넘지 않아야 한다. 배정밀도 eps는 2.2e-16이고
        # 신호 크기 대비 상대 오차로 본다 (Pi 5 실측 최대 1.5e-18).
        limit = max(scale, 1e-30) * 1e-12
        self.assertLessEqual(
            worst, limit,
            f"{label}: 최대 오차 {worst:.3e} > 허용 {limit:.3e}")

    @unittest.skipUnless(_HAS_NUMPY, "numpy 없음 — 벡터 경로가 존재하지 않는다")
    def test_matches_reference_on_noise(self):
        for seed in (1, 7, 23):
            values = _noise(seed, resampling.DEVICE_SLOT_SAMPLES)
            self._assert_close(
                resampling._decimate_slot_reference(values),
                resampling._decimate_slot_vectorized(values),
                f"noise seed={seed}")

    @unittest.skipUnless(_HAS_NUMPY, "numpy 없음")
    def test_matches_reference_on_burst(self):
        values = _burst(3, resampling.DEVICE_SLOT_SAMPLES)
        self._assert_close(
            resampling._decimate_slot_reference(values),
            resampling._decimate_slot_vectorized(values),
            "burst")

    @unittest.skipUnless(_HAS_NUMPY, "numpy 없음")
    def test_matches_reference_at_the_edges(self):
        """군지연 보상 때문에 앞뒤 경계는 zero-extension 구간을 탄다."""

        count = resampling.DEVICE_SLOT_SAMPLES
        values = [0j] * count
        values[0] = complex(1.0, -1.0)
        values[1] = complex(0.5, 0.25)
        values[-1] = complex(-1.0, 1.0)
        values[-2] = complex(0.25, -0.5)
        values = tuple(values)
        self._assert_close(
            resampling._decimate_slot_reference(values),
            resampling._decimate_slot_vectorized(values),
            "edges")

    def test_all_zero_window_returns_zeros(self):
        values = (0j,) * resampling.DEVICE_SLOT_SAMPLES
        produced = resampling.decimate_slot(values)
        self.assertEqual(resampling.NATIVE_SLOT_SAMPLES, len(produced))
        self.assertTrue(all(value == 0j for value in produced))

    def test_wrong_length_raises_value_error(self):
        with self.assertRaises(ValueError):
            resampling.decimate_slot((0j,) * (resampling.DEVICE_SLOT_SAMPLES - 1))

    def test_non_finite_raises_value_error(self):
        values = [complex(0.1, 0.1)] * resampling.DEVICE_SLOT_SAMPLES
        values[10] = complex(math.inf, 0.0)
        with self.assertRaises(ValueError):
            resampling.decimate_slot(tuple(values))

    def test_public_entry_point_uses_the_vector_path_when_available(self):
        """decimate_slot이 참조와 같은 값을 내는지 (경로 무관) 확인한다."""

        values = _noise(11, resampling.DEVICE_SLOT_SAMPLES)
        self._assert_close(
            resampling._decimate_slot_reference(values),
            resampling.decimate_slot(values),
            "public")


if __name__ == "__main__":
    unittest.main()
