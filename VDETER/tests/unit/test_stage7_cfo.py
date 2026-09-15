"""Stage 7 CFO estimator 시험 (`stage7` §9.3).

numeric profile `STAGE7_NUMPY_FFT3072_BINARY64_V1`은 NumPy를 결속하므로 NumPy가
없는 환경에서는 skip한다 (Pi 실행환경 numpy 1.26.4에서 실행).
"""

import hashlib
import math
import struct
import unittest

try:
    import numpy  # noqa: F401

    HAS_NUMPY = True
except Exception:  # pragma: no cover - 환경 의존
    HAS_NUMPY = False

from vdeter.b210_system.cfo import (
    BLOCK_SAMPLES,
    BLOCKS_PER_BURST,
    PROBE_AMPLITUDE,
    PROBE_SAMPLES,
    CfoError,
    estimate_burst,
    fit_path,
    generate_probe_vector,
    median_even_binary64,
)


def _tone_burst(cfo_hz: float, amplitude: float = 0.125) -> bytes:
    """`f_tone+cfo` Hz tone 1초 (307,200 sample) cf32le."""

    out = bytearray()
    pack = struct.Struct("<2f").pack
    frequency = 25_000.0 + cfo_hz
    for n in range(BLOCK_SAMPLES * BLOCKS_PER_BURST):
        phase = 2.0 * math.pi * frequency * n / 307_200.0
        out += pack(amplitude * math.cos(phase), amplitude * math.sin(phase))
    return bytes(out)


class TC_STAGE7_PROBE_VECTOR(unittest.TestCase):
    def test_probe_vector_shape_and_first_sample(self):
        probe = generate_probe_vector()
        self.assertEqual(len(probe), PROBE_SAMPLES * 8)
        real, imag = struct.unpack("<2f", probe[:8])
        self.assertEqual(real, PROBE_AMPLITUDE)
        self.assertEqual(imag, 0.0)

    def test_probe_vector_deterministic_digest(self):
        first = hashlib.sha256(generate_probe_vector()).hexdigest()
        second = hashlib.sha256(generate_probe_vector()).hexdigest()
        self.assertEqual(first, second)


@unittest.skipUnless(HAS_NUMPY, "NumPy 결속 환경(Pi)에서 실행")
class TC_STAGE7_CFO_ESTIMATOR(unittest.TestCase):
    def test_zero_cfo(self):
        estimate = estimate_burst(_tone_burst(0.0))
        self.assertEqual(estimate.valid_block_count, BLOCKS_PER_BURST)
        self.assertLess(abs(estimate.f_k_hz), 0.01)
        self.assertGreaterEqual(estimate.u_k_hz, 10.0)

    def test_positive_and_negative_cfo(self):
        for cfo in (137.0, -512.0, 1799.0, -1799.0):
            estimate = estimate_burst(_tone_burst(cfo))
            self.assertLess(abs(estimate.f_k_hz - cfo), 0.05, cfo)

    def test_clipping_blocks_invalid(self):
        burst = bytearray(_tone_burst(100.0))
        # 앞 11개 block을 clipping시켜 유효 89개로 만든다 → 측정 실패.
        for block in range(11):
            offset = block * BLOCK_SAMPLES * 8
            struct.pack_into("<2f", burst, offset, 1.5, 0.0)
        with self.assertRaises(CfoError):
            estimate_burst(bytes(burst))

    def test_boundary_90_valid_blocks(self):
        burst = bytearray(_tone_burst(100.0))
        for block in range(10):
            offset = block * BLOCK_SAMPLES * 8
            struct.pack_into("<2f", burst, offset, 1.5, 0.0)
        estimate = estimate_burst(bytes(burst))
        self.assertEqual(estimate.valid_block_count, 90)

    def test_low_amplitude_rms_gate(self):
        # RMS -36 dBFS 미만은 invalid → 전 block invalid → 실패.
        with self.assertRaises(CfoError):
            estimate_burst(_tone_burst(0.0, amplitude=0.001))


@unittest.skipUnless(HAS_NUMPY, "NumPy 결속 환경(Pi)에서 실행")
class TC_STAGE7_CFO_FIT(unittest.TestCase):
    def _estimates(self, a=100.0, b=0.5, u=10.0):
        from vdeter.b210_system.cfo import BurstEstimate

        t_values = [-63.0 + 7.0 * i for i in range(10)]
        estimates = [
            BurstEstimate(
                f_k_hz=a + b * t, u_k_hz=u, valid_block_count=100, block_estimates=()
            )
            for t in t_values
        ]
        return estimates, t_values

    def test_linear_fit_exact(self):
        estimates, t_values = self._estimates()
        fit = fit_path(estimates, t_values, 120.0)
        self.assertAlmostEqual(fit.a_hz, 100.0, places=9)
        self.assertAlmostEqual(fit.b_hz_per_s, 0.5, places=9)
        self.assertAlmostEqual(fit.r_max_hz, 10.0, places=9)
        self.assertAlmostEqual(fit.b_cfo_hz, 100.0 + 0.5 * 120.0 + 10.0, places=9)
        self.assertTrue(fit.passed)

    def test_bound_boundary(self):
        estimates, t_values = self._estimates(a=1789.0, b=0.0, u=10.0)
        fit = fit_path(estimates, t_values, 120.0)
        self.assertAlmostEqual(fit.b_cfo_hz, 1799.0, places=9)
        self.assertTrue(fit.passed)
        estimates, t_values = self._estimates(a=1791.0, b=0.0, u=10.0)
        fit = fit_path(estimates, t_values, 120.0)
        self.assertFalse(fit.passed)

    def test_short_span_rejected(self):
        from vdeter.b210_system.cfo import BurstEstimate

        t_values = [-45.0 + 5.0 * i for i in range(10)]
        estimates = [
            BurstEstimate(f_k_hz=0.0, u_k_hz=10.0, valid_block_count=100, block_estimates=())
            for _ in t_values
        ]
        with self.assertRaises(CfoError):
            fit_path(estimates, t_values, 120.0)


class TC_STAGE7_MEDIAN_EVEN(unittest.TestCase):
    def test_even_median_is_binary64_midpoint(self):
        self.assertEqual(median_even_binary64([1.0, 2.0]), 1.5)
        self.assertEqual(median_even_binary64([1.0, 2.0, 3.0]), 2.0)


if __name__ == "__main__":
    unittest.main()
