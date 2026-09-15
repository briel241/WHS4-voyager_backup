"""§13.5 결정적 sample-rate 변환 경계 단위시험.

요구사항: `stage6_phy_common_baseline.md` §13.5 (`DR-STAGE6-MR-001`) — exact count,
결정성, DC/주파수 응답, 군지연 보상, slot 격리, NaN/Inf 거부, 상태 무누출.
"""

from __future__ import annotations

import cmath
import hashlib
import math
import struct
import unittest

from vdeter.phy.resampling import (
    DEVICE_SAMPLE_RATE_HZ,
    DEVICE_SLOT_SAMPLES,
    NATIVE_SAMPLE_RATE_HZ,
    NATIVE_SLOT_SAMPLES,
    PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES,
    PROTOTYPE_TAP_BINARY64_BITS,
    PROTOTYPE_TAP_COUNT,
    PROTOTYPE_TAP_SHA256,
    PROTOTYPE_TAPS,
    RATE_FACTOR,
    decimate_slot,
    interpolate_slot,
)


def _tone(frequency_hz: float, sample_rate_hz: int, count: int) -> tuple[complex, ...]:
    step = 2.0 * math.pi * frequency_hz / sample_rate_hz
    return tuple(cmath.exp(1j * step * index) for index in range(count))


class PrototypeIdentityTest(unittest.TestCase):
    def test_frozen_tap_identity_and_symmetry(self):
        self.assertEqual(PROTOTYPE_TAP_COUNT, len(PROTOTYPE_TAPS))
        self.assertEqual(97, PROTOTYPE_TAP_COUNT)
        self.assertEqual(48, PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES)
        raw = b"".join(struct.pack("<d", value) for value in PROTOTYPE_TAPS)
        self.assertEqual(PROTOTYPE_TAP_SHA256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            tuple(PROTOTYPE_TAP_BINARY64_BITS), tuple(reversed(PROTOTYPE_TAP_BINARY64_BITS))
        )

    def test_stopband_attenuation_is_below_minus_87_db(self):
        for fraction in (0.31, 0.4, 0.5, 0.75, 1.0):
            omega = math.pi * fraction
            response = abs(
                sum(
                    tap * cmath.exp(-1j * omega * index)
                    for index, tap in enumerate(PROTOTYPE_TAPS)
                )
            ) / math.fsum(PROTOTYPE_TAPS)
            self.assertLess(response, 10 ** (-87 / 20), msg=f"omega={fraction}*pi")


class ArithmeticOrderIdentityTest(unittest.TestCase):
    """`TC-PHY-MULTI-011`: §13.5 산술 순서·정규화 파생값과 impulse vector hash."""

    def test_branch_dc_gains_match_the_baseline_exactly(self):
        # branch 정규화 뒤 같은 index 순서로 다시 더한 nominal DC 이득 (§13.5).
        expected = (1.0, 1.0000000000000004, 1.0, 1.0000000000000002)
        for phase, expected_gain in enumerate(expected):
            raw = [
                RATE_FACTOR * PROTOTYPE_TAPS[index]
                for index in range(phase, PROTOTYPE_TAP_COUNT, RATE_FACTOR)
            ]
            scale = 0.0
            for value in raw:
                scale += value
            gain = 0.0
            for value in raw:
                gain += value / scale
            self.assertEqual(expected_gain, gain, msg=f"branch {phase}")

    def test_decimation_gain_and_energy_match_the_baseline_exactly(self):
        scale = 0.0
        for value in PROTOTYPE_TAPS:
            scale += value
        gain = 0.0
        energy = 0.0
        for value in PROTOTYPE_TAPS:
            normalized = value / scale
            gain += normalized
            energy += normalized * normalized
        self.assertEqual(0.9999999999999998, gain)
        self.assertEqual(0.23928822132142086, energy)

    def test_interpolation_impulse_vector_hash_is_exact(self):
        from vdeter.phy.burst.rrc import to_cf32le

        stimulus = [0j] * NATIVE_SLOT_SAMPLES
        stimulus[1024] = 1 + 0j
        digest = hashlib.sha256(to_cf32le(interpolate_slot(stimulus))).hexdigest()
        self.assertEqual(
            "e83069a99ab9d1aac8199512cad7353db4d0ba2ddc5a1cfaf130455ac63f3d4c",
            digest,
        )

    def test_decimation_impulse_vector_hash_is_exact(self):
        from vdeter.phy.burst.rrc import to_cf32le

        stimulus = [0j] * DEVICE_SLOT_SAMPLES
        stimulus[4096] = 1 + 0j
        digest = hashlib.sha256(to_cf32le(decimate_slot(stimulus))).hexdigest()
        self.assertEqual(
            "6c0d0965ddba3d2dfcd0742615c0e987d98f2fde7ecf8e256e5ca976bc54ac8c",
            digest,
        )


class ExactCountAndValidationTest(unittest.TestCase):
    def test_exact_output_counts(self):
        up = interpolate_slot((0.5 + 0.25j,) * NATIVE_SLOT_SAMPLES)
        self.assertEqual(DEVICE_SLOT_SAMPLES, len(up))
        down = decimate_slot((0.5 + 0.25j,) * DEVICE_SLOT_SAMPLES)
        self.assertEqual(NATIVE_SLOT_SAMPLES, len(down))

    def test_wrong_length_is_rejected(self):
        for count in (0, 1, NATIVE_SLOT_SAMPLES - 1, NATIVE_SLOT_SAMPLES + 1, DEVICE_SLOT_SAMPLES):
            if count != NATIVE_SLOT_SAMPLES:
                with self.assertRaises(ValueError):
                    interpolate_slot((0j,) * count)
        for count in (0, NATIVE_SLOT_SAMPLES, DEVICE_SLOT_SAMPLES - 1, DEVICE_SLOT_SAMPLES + 4):
            if count != DEVICE_SLOT_SAMPLES:
                with self.assertRaises(ValueError):
                    decimate_slot((0j,) * count)

    def test_non_finite_samples_are_rejected(self):
        for poison in (complex(float("nan"), 0.0), complex(0.0, float("inf")), complex(float("-inf"), 0.0)):
            bad_native = [0j] * NATIVE_SLOT_SAMPLES
            bad_native[1000] = poison
            with self.assertRaises(ValueError):
                interpolate_slot(bad_native)
            bad_device = [0j] * DEVICE_SLOT_SAMPLES
            bad_device[5000] = poison
            with self.assertRaises(ValueError):
                decimate_slot(bad_device)

    def test_all_zero_shortcut_is_exact(self):
        self.assertEqual((0j,) * DEVICE_SLOT_SAMPLES, interpolate_slot((0j,) * NATIVE_SLOT_SAMPLES))
        self.assertEqual((0j,) * NATIVE_SLOT_SAMPLES, decimate_slot((0j,) * DEVICE_SLOT_SAMPLES))


class DeterminismAndStateTest(unittest.TestCase):
    def _stimulus_native(self) -> tuple[complex, ...]:
        return tuple(
            complex(math.sin(0.01 * index), math.cos(0.017 * index)) * 0.3
            for index in range(NATIVE_SLOT_SAMPLES)
        )

    def test_repeat_invocation_is_bit_identical(self):
        stimulus = self._stimulus_native()
        first = interpolate_slot(stimulus)
        second = interpolate_slot(stimulus)
        self.assertEqual(first, second)
        down_first = decimate_slot(first)
        down_second = decimate_slot(first)
        self.assertEqual(down_first, down_second)

    def test_no_state_leaks_between_slots(self):
        # slot A를 먼저 처리해도 slot B의 결과는 B 단독 처리와 bit-identical이다.
        slot_a = self._stimulus_native()
        slot_b = tuple(value * (0.5 - 0.25j) for value in reversed(slot_a))
        b_alone = interpolate_slot(slot_b)
        interpolate_slot(slot_a)
        b_after_a = interpolate_slot(slot_b)
        self.assertEqual(b_alone, b_after_a)
        device_a = interpolate_slot(slot_a)
        down_alone = decimate_slot(device_a)
        decimate_slot(interpolate_slot(slot_b))
        self.assertEqual(down_alone, decimate_slot(device_a))


class ResponseTest(unittest.TestCase):
    def test_impulse_lands_at_compensated_position(self):
        # native index p의 impulse는 지연 보상 뒤 device index 4p에서 최대 응답을 갖는다.
        position = 700
        stimulus = [0j] * NATIVE_SLOT_SAMPLES
        stimulus[position] = 1 + 0j
        up = interpolate_slot(stimulus)
        peak_index = max(range(DEVICE_SLOT_SAMPLES), key=lambda index: abs(up[index]))
        self.assertEqual(RATE_FACTOR * position, peak_index)
        self.assertAlmostEqual(1.0, abs(up[peak_index]), delta=1e-3)
        # 대칭 linear-phase: 좌우 응답이 켤레 대칭 크기를 갖는다.
        for offset in (1, 2, 3, 5, 9):
            self.assertAlmostEqual(
                abs(up[peak_index - offset]), abs(up[peak_index + offset]), delta=1e-12
            )

    def test_decimated_impulse_is_delay_compensated(self):
        position = 3200
        stimulus = [0j] * DEVICE_SLOT_SAMPLES
        stimulus[position] = 1 + 0j
        down = decimate_slot(stimulus)
        peak_index = max(range(NATIVE_SLOT_SAMPLES), key=lambda index: abs(down[index]))
        self.assertEqual(position // RATE_FACTOR, peak_index)

    def test_passband_tone_preserves_amplitude_and_frequency(self):
        frequency = 5_000.0
        stimulus = _tone(frequency, NATIVE_SAMPLE_RATE_HZ, NATIVE_SLOT_SAMPLES)
        up = interpolate_slot(stimulus)
        expected = _tone(frequency, DEVICE_SAMPLE_RATE_HZ, DEVICE_SLOT_SAMPLES)
        # slot 경계 과도 영역을 제외한 중앙에서 이상 tone과 비교한다.
        error = max(abs(up[index] - expected[index]) for index in range(400, 7600))
        self.assertLess(error, 2e-4)

    def test_stopband_tone_is_suppressed_by_decimation(self):
        frequency = 100_000.0  # native Nyquist(38.4 kHz) 밖 -> 데시메이션에서 제거돼야 한다.
        stimulus = _tone(frequency, DEVICE_SAMPLE_RATE_HZ, DEVICE_SLOT_SAMPLES)
        down = decimate_slot(stimulus)
        residual = max(abs(value) for value in down[200:1800])
        self.assertLess(residual, 1e-4)

    def test_round_trip_passband_error_is_small(self):
        stimulus = tuple(
            0.25 * cmath.exp(1j * 2.0 * math.pi * 9_600.0 * index / NATIVE_SAMPLE_RATE_HZ)
            + 0.25 * cmath.exp(-1j * 2.0 * math.pi * 4_000.0 * index / NATIVE_SAMPLE_RATE_HZ)
            for index in range(NATIVE_SLOT_SAMPLES)
        )
        back = decimate_slot(interpolate_slot(stimulus))
        error = max(abs(back[index] - stimulus[index]) for index in range(200, 1848))
        self.assertLess(error, 5e-4)


class SlotIsolationTest(unittest.TestCase):
    def test_guard_tail_stays_exact_zero(self):
        # 마지막 64 native samples(guard)가 0이면 보간 출력 index>=7984는 exact 0이다.
        stimulus = [complex(0.4, -0.2)] * (NATIVE_SLOT_SAMPLES - 64) + [0j] * 64
        up = interpolate_slot(stimulus)
        self.assertTrue(all(value == 0j for value in up[7984:]))
        self.assertEqual(DEVICE_SLOT_SAMPLES, len(up))

    def test_active_response_never_extends_past_slot(self):
        stimulus = [0j] * NATIVE_SLOT_SAMPLES
        stimulus[NATIVE_SLOT_SAMPLES - 65] = 1 + 0j  # 마지막 active sample 직전
        up = interpolate_slot(stimulus)
        self.assertEqual(DEVICE_SLOT_SAMPLES, len(up))
        # 응답은 4*(2048-65)+48 = 7980까지이고 slot 끝(8191)을 넘지 않는다.
        self.assertTrue(all(value == 0j for value in up[7981:]))


if __name__ == "__main__":
    unittest.main()
