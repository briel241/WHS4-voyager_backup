"""Stage 7 affine time bridge 수치 시험 (`stage7` §13.2, §13.3)."""

import unittest
from fractions import Fraction

from vdeter.b210_system.time_bridge import (
    BracketObservation,
    Stage7TimeBridgeError,
    ceil_pos,
    corrections_from_roc,
    directed_path_median,
    fit_affine,
    median_even_fraction,
    pairwise_offsets,
    rf_drift_envelope_ppm,
    rne,
    select_utc_anchor,
    triangle_closure_ns,
    verify_round_trip,
)


class TC_STAGE7_RNE(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(rne(7, 2), 4)   # 3.5 → 4 (even)
        self.assertEqual(rne(5, 2), 2)   # 2.5 → 2 (even)
        self.assertEqual(rne(-5, 2), -2)  # -2.5 → -2 (even)
        self.assertEqual(rne(-7, 2), -4)  # -3.5 → -4 (even)
        self.assertEqual(rne(1, 3), 0)
        self.assertEqual(rne(2, 3), 1)
        self.assertEqual(rne(-1, 3), 0)
        self.assertEqual(rne(-2, 3), -1)
        self.assertEqual(rne(10, 5), 2)

    def test_negative_denominator_normalized(self):
        self.assertEqual(rne(5, -2), -2)
        self.assertEqual(rne(-5, -2), 2)

    def test_ceil_pos(self):
        self.assertEqual(ceil_pos(Fraction(0)), 0)
        self.assertEqual(ceil_pos(Fraction(1, 3)), 1)
        self.assertEqual(ceil_pos(Fraction(6, 3)), 2)
        with self.assertRaises(Stage7TimeBridgeError):
            ceil_pos(Fraction(-1, 3))


def _observations(beta=Fraction(1_000_001, 1_000_000), alpha_ns=123_456, count=17,
                  period_ns=1_000_000_000, width_ns=2_000, jitter=None):
    """정확한 affine 관계 `m = alpha + beta*h`의 합성 bracket."""

    observations = []
    for index in range(count):
        h = 5_000_000_000 + index * period_ns
        m_exact = alpha_ns + int(beta * h)
        offset = 0 if jitter is None else jitter[index % len(jitter)]
        m_mid = m_exact + offset
        observations.append(
            BracketObservation(
                m_before_ns=m_mid - width_ns // 2,
                hardware_ns=h,
                m_after_ns=m_mid + width_ns - width_ns // 2,
            )
        )
    return observations


class TC_STAGE7_AFFINE_FIT(unittest.TestCase):
    def test_exact_affine_recovers_slope(self):
        result = fit_affine(_observations())
        # 1 ppm drift를 정확히 복원한다 (v0.8.15: |d|<=25ppm, u_beta<=1ppm).
        self.assertEqual(result.d_ppm_exact, Fraction(1))
        self.assertLessEqual(result.u_beta_ppm, Fraction(1))
        self.assertEqual(result.observation_count, 17)

    def test_round_trip_within_1ns(self):
        result = fit_affine(_observations())
        start = result.coefficients.forward(5_000_000_000)
        verify_round_trip(result.coefficients, start, start + 120_000_000_000)

    def test_quarter_ns_correction_round_trip(self):
        result = fit_affine(_observations())
        start = result.coefficients.forward(5_000_000_000)
        # §13.3: correction은 0.25 ns 단위까지 생길 수 있다.
        verify_round_trip(
            result.coefficients, start, start + 120_000_000_000, c_num=1, c_den=4
        )
        verify_round_trip(
            result.coefficients, start, start + 120_000_000_000, c_num=-3, c_den=4
        )

    def test_too_few_observations(self):
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(_observations(count=7))

    def test_short_span_rejected(self):
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(_observations(count=8, period_ns=200_000_000))

    def test_wide_bracket_rejected(self):
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(_observations(width_ns=2_000_001))

    def test_excessive_drift_rejected(self):
        # v0.8.15: |d| 한계는 25 ppm이다.
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(_observations(beta=Fraction(1_000_026, 1_000_000)))

    def test_nonmonotonic_hardware_rejected(self):
        observations = _observations()
        broken = list(observations)
        broken[3] = BracketObservation(
            m_before_ns=broken[3].m_before_ns,
            hardware_ns=broken[2].hardware_ns,
            m_after_ns=broken[3].m_after_ns,
        )
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(broken)

    def test_residual_envelope_enforced(self):
        # ±width/2보다 큰 jitter는 envelope 밖이다.
        with self.assertRaises(Stage7TimeBridgeError):
            fit_affine(_observations(width_ns=100, jitter=(0, 30_000, -30_000)))

    def test_forward_inverse_formulas(self):
        result = fit_affine(_observations())
        coefficients = result.coefficients
        h = 12_345_678_901
        m = coefficients.forward(h)
        back = coefficients.inverse(m)
        self.assertLessEqual(abs(back - h), 1)


class TC_STAGE7_UTC_ANCHOR(unittest.TestCase):
    def test_smallest_bracket_selected(self):
        brackets = [(100 + i, 10_000 + i, 100 + i + 50 + i * 10) for i in range(8)]
        anchor = select_utc_anchor(brackets)
        self.assertEqual(anchor.bracket_width_ns, 50)
        self.assertEqual(anchor.utc_label(anchor.host_raw_anchor_ns), anchor.host_utc_anchor_ns)
        self.assertEqual(
            anchor.label_to_raw(anchor.host_utc_anchor_ns + 777), anchor.host_raw_anchor_ns + 777
        )

    def test_exactly_eight_required(self):
        with self.assertRaises(Stage7TimeBridgeError):
            select_utc_anchor([(0, 0, 10)] * 7)

    def test_wide_anchor_rejected(self):
        brackets = [(0, 0, 3_000_000)] * 8
        with self.assertRaises(Stage7TimeBridgeError):
            select_utc_anchor(brackets)


class TC_STAGE7_RF_RATIONALS(unittest.TestCase):
    def test_median_even(self):
        self.assertEqual(median_even_fraction([1, 2, 3]), Fraction(2))
        self.assertEqual(median_even_fraction([1, 2, 3, 4]), Fraction(5, 2))

    def test_directed_path_median_half_ns(self):
        values = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
        self.assertEqual(directed_path_median(values), Fraction(29, 2))
        # v0.9.5: 결손 ≤1 허용 — 9개 값은 홀수 median, 8개는 거부.
        self.assertEqual(directed_path_median(values[:9]), Fraction(14))
        with self.assertRaises(Stage7TimeBridgeError):
            directed_path_median(values[:8])

    def test_quarter_ns_offsets(self):
        # D_ij가 0.5 ns 단위를 가지므로 O/P는 0.25 ns 단위까지 생긴다.
        d_forward = Fraction(29, 2)   # 14.5 ns
        d_reverse = Fraction(21, 2)   # 10.5 ns
        offsets = pairwise_offsets(d_forward, d_reverse)
        self.assertEqual(offsets.o_ij, Fraction(2))
        self.assertEqual(offsets.p_ij, Fraction(25, 2))
        offsets = pairwise_offsets(Fraction(29, 2), Fraction(10))
        self.assertEqual(offsets.o_ij, Fraction(9, 4))   # 2.25 ns
        self.assertEqual(offsets.p_ij, Fraction(49, 4))  # 12.25 ns

    def test_corrections_from_roc(self):
        corrections = corrections_from_roc(Fraction(9, 4), Fraction(-3, 2))
        self.assertEqual(corrections["ROC"], (0, 1))
        self.assertEqual(corrections["MASS-A"], (-9, 4))
        self.assertEqual(corrections["MASS-B"], (3, 2))

    def test_triangle_closure(self):
        # O_AB = O_RB - O_RA이면 closure 0.
        self.assertEqual(
            triangle_closure_ns(Fraction(-15, 4), Fraction(9, 4), Fraction(-3, 2)),
            Fraction(0),
        )
        self.assertEqual(
            triangle_closure_ns(Fraction(0), Fraction(9, 4), Fraction(-3, 2)),
            Fraction(15, 4),
        )

    def test_b_rf_ppm(self):
        # native 76,800 sample/s → q_rx = ceil(5e8/76800) = 6511 ns.
        d_values = [0, 100, 200, 300, 400, 500, 600, 700, 800, 1000]
        envelope = rf_drift_envelope_ppm(d_values, 76_800, 60_000_000_000)
        expected = ceil_pos(Fraction((1000 + 2 * 6511) * 1_000_000, 60_000_000_000))
        self.assertEqual(envelope, Fraction(expected))
        # wideband 307,200 → q_rx = ceil(5e8/307200) = 1628.
        envelope = rf_drift_envelope_ppm(d_values, 307_200, 60_000_000_000)
        expected = ceil_pos(Fraction((1000 + 2 * 1628) * 1_000_000, 60_000_000_000))
        self.assertEqual(envelope, Fraction(expected))


if __name__ == "__main__":
    unittest.main()
