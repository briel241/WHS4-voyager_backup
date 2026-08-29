"""Stage 7 §13.3 RF timestamp 판정 수치 시험."""

import unittest
from fractions import Fraction

from vdeter.stage7.rf_time import (
    Stage7RfTimeError,
    build_rf_time_measurement_summary,
    check_fanout_same_window,
    evaluate_pre_correction_set,
)

SPAN = 63_000_000_000
BASE = {
    "run_id": "stage7-rfts-unit",
    "validation_phase": "PRE_CORRECTION",
    "radio_generation": 1,
    "lane_generation": 1,
    "bridge_generation": None,
    "clock_id": "stage7-pi-host-affine-provisional-v1:stage7-rfts-unit:1",
    "scheduled_first_ns": 10_000_000_000,
    "scheduled_last_ns": 10_000_000_000 + SPAN,
    "phy_sample_rate_hz": 307_200,
    "decode_count": 10,
    "timestamps_continuous": True,
    "windows_in_slot": True,
    "raw_artifact_sha256": "0" * 64,
}


def _summary(path_id: str, d_values):
    return build_rf_time_measurement_summary(
        path_id=path_id, d_values_ns=d_values, **BASE
    )


class TC_STAGE7_RF_TIME_SUMMARY(unittest.TestCase):
    def test_pass_summary_median_exact_rational(self):
        values = [100, 105, 96, 101, 99, 104, 98, 103, 97, 102]
        summary = _summary("C1", values)
        self.assertEqual(summary["outcome"], "PASS", summary["failure_reasons"])
        # 정렬 뒤 d[4]=100, d[5]=101 → D=201/2.
        self.assertEqual((summary["D_num"], summary["D_den"]), (201, 2))
        self.assertEqual(summary["spread_ns"], 9)
        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(summary["source_endpoint_id"], "ROC")
        self.assertEqual(summary["receiver_endpoint_id"], "MASS-A")
        # B_rf: (9+2*1628)*1e6/63e9 → ceil → 1 ppm.
        self.assertEqual(summary["B_rf_ppm"], 1)

    def test_spread_gate_fails(self):
        values = [0, 60_000, 1, 2, 3, 4, 5, 6, 7, 8]
        summary = _summary("C2", values)
        self.assertEqual(summary["outcome"], "FAIL")
        self.assertIn("spread", summary["failure_reasons"])

    def test_short_span_fails(self):
        arguments = dict(BASE)
        arguments["scheduled_last_ns"] = arguments["scheduled_first_ns"] + 59_000_000_000
        summary = build_rf_time_measurement_summary(
            path_id="C3", d_values_ns=list(range(10)), **arguments
        )
        self.assertEqual(summary["outcome"], "FAIL")
        self.assertIn("scheduled_span", summary["failure_reasons"])

    def test_decode_gate_fails(self):
        arguments = dict(BASE)
        arguments["decode_count"] = 9
        summary = build_rf_time_measurement_summary(
            path_id="C4", d_values_ns=list(range(10)), **arguments
        )
        self.assertEqual(summary["outcome"], "FAIL")
        self.assertIn("decode_10_of_10", summary["failure_reasons"])


class TC_STAGE7_RF_TIME_SET(unittest.TestCase):
    @staticmethod
    def _set(d_by_path):
        return {
            path: _summary(path, [d + k for k in range(10)])
            for path, d in d_by_path.items()
        }

    def test_full_set_corrections_and_closure(self):
        # D는 각 path의 (d[4]+d[5])/2 = d+4.5.
        summaries = self._set({"C1": 100, "C2": 60, "C3": 200, "C4": 80, "C5": 150, "C6": 110})
        result = evaluate_pre_correction_set(summaries)
        # O_RA=(104.5-64.5)/2=20, O_RB=(204.5-84.5)/2=60, O_AB=(154.5-114.5)/2=20.
        self.assertEqual(result["offsets"]["RA"], {"O_num": 20, "O_den": 1, "P_num": 169, "P_den": 2})
        self.assertEqual(result["offsets"]["RB"]["O_num"], 60)
        # closure=|20-(60-20)|=20 ≤ 50000.
        self.assertEqual(result["triangle_closure_ns_num"], 20)
        self.assertEqual(result["corrections"]["ROC"], (0, 1))
        self.assertEqual(result["corrections"]["MASS-A"], (-20, 1))
        self.assertEqual(result["corrections"]["MASS-B"], (-60, 1))

    def test_closure_limit_enforced(self):
        summaries = self._set(
            {"C1": 0, "C2": 0, "C3": 900_000, "C4": 0, "C5": 0, "C6": 0}
        )
        with self.assertRaises(Stage7RfTimeError):
            evaluate_pre_correction_set(summaries)

    def test_p1_pair_subset(self):
        summaries = self._set({"C3": 30, "C4": 10})
        result = evaluate_pre_correction_set(summaries, present_pairs=("RB",))
        self.assertEqual(result["corrections"], {"ROC": (0, 1), "MASS-B": (-10, 1)})
        self.assertNotIn("triangle_closure_ns_num", result)

    def test_missing_path_rejected(self):
        summaries = self._set({"C1": 0})
        with self.assertRaises(Stage7RfTimeError):
            evaluate_pre_correction_set(summaries, present_pairs=("RA",))

    def test_fail_summary_rejected_in_set(self):
        summaries = self._set({"C1": 0, "C2": 0})
        summaries["C2"] = _summary("C2", [0, 60_000] + list(range(8)))
        with self.assertRaises(Stage7RfTimeError):
            evaluate_pre_correction_set(summaries, present_pairs=("RA",))

    def test_fanout_same_window_gate(self):
        self.assertTrue(check_fanout_same_window(1_000_000, 1_040_000))
        self.assertFalse(check_fanout_same_window(0, 300_000))


if __name__ == "__main__":
    unittest.main()
