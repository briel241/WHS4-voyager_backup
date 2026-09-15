"""RR-P1 TC-004 RAW_PHY RX 슬롯 정렬 회귀 시험 (Phase I B안, 2026-09-04).

확정된 결함: active bridge 설치 뒤 실행되는 RAW_PHY phase에서 RX 캡처 슬롯
격자가 endpoint별 RF correction을 적용하지 않아(=PRE_CORRECTION, (0,1)), 수신
endpoint의 correction 크기만큼 burst가 슬롯 앞쪽으로 밀려 복호에 실패했다.
4차 ladder(qual-20260904-105849)에서 MASS-A correction이 +210.6µs로 커져 C1
(ROC→MASS-A)·C6(MASS-B→MASS-A)가 100/100 무복호로 FAIL.

수정: RAW_PHY를 correction 적용 phase로 포함해 TX 방사와 RX 캡처가 rf-time
POST_CORRECTION과 동일한 시각 기준(`_host_raw_to_hardware` + active_correction)을
재사용하도록 했다. 이 시험은 실제 `_rf_time_correction`·`_host_raw_to_hardware`
·`_plan_times_to_hardware` 코드를 그대로 호출한다(재구현 아님).

판정 완화·재시도가 아니라 시각 변환의 부호·이중적용·전 경로 정렬을 고정한다.
"""

from __future__ import annotations

import types
import unittest
from fractions import Fraction

from vdeter.stage7.pi_runtime import Stage7PiOperations
from vdeter.stage7.rf_time import PATH_ENDPOINTS
from vdeter.stage7.time_bridge import AffineCoefficients

#: 4차 ladder 실측 correction (coordinator console): ROC 기준 c_R=0,
#: MASS-A = 421137/2 ns = +210568.5 ns(+210.6µs), MASS-B = 190427/4 ns.
CORR_MASS_A = (421137, 2)
CORR_MASS_B = (190427, 4)
CORR_ROC = (0, 1)
CORRECTIONS = {"ROC": CORR_ROC, "MASS-A": CORR_MASS_A, "MASS-B": CORR_MASS_B}

#: beta=1·alpha=0 fit — inverse_corrected(m,c)=h0+(m-m0-c)로 정확해 부호·크기를
#: 정수로 단언할 수 있다(실측 MASS-A epoch 사용). 반올림(rne)은 정수 입력에서
#: 무손실이다.
_H0 = 242589375364
_M0 = 844188766717592
CLEAN_FIT = AffineCoefficients(
    h0_ns=_H0, m0_ns=_M0, alpha_num=0, alpha_den=1, beta_num=1, beta_den=1
)

#: 현실적 ppm 오차 fit(beta≈1-6.9e-6) — 반올림 잔차가 1ns 이하임을 확인한다.
PPM_FIT = AffineCoefficients(
    h0_ns=_H0, m0_ns=_M0, alpha_num=0, alpha_den=1,
    beta_num=330255766284658936402673787,
    beta_den=330258033515692255432753324,
)


def _rt(fit, correction):
    """실제 pi_runtime 메서드를 그대로 실행하는 최소 self.

    중첩 self-호출(`_host_raw_to_hardware`→`_rf_time_correction`,
    `_plan_times_to_hardware`→`_host_raw_to_hardware`)이 실제 코드로
    해결되도록 실제 메서드를 stub에 bind한다(재구현 아님)."""
    stub = types.SimpleNamespace()
    stub.provisional_fit = types.SimpleNamespace(coefficients=fit)
    stub.active_correction = correction
    stub._CORRECTED_TIME_PHASES = Stage7PiOperations._CORRECTED_TIME_PHASES
    for name in ("_rf_time_correction", "_host_raw_to_hardware",
                 "_plan_times_to_hardware"):
        setattr(stub, name,
                types.MethodType(getattr(Stage7PiOperations, name), stub))
    return stub

def _corr(stub, phase):
    return stub._rf_time_correction(phase)

def _h2h(stub, host_ns, phase):
    return stub._host_raw_to_hardware(host_ns, phase)

def _plan(stub, host_values, phase="PRE_CORRECTION"):
    return stub._plan_times_to_hardware(
        None, {"scheduled_host_raw_ns": list(host_values)}, phase
    )


class RfTimeCorrectionPhaseTest(unittest.TestCase):
    """계약: correction은 active bridge 뒤 phase(POST/RAW_PHY)에만, 그 외는 (0,1)."""

    def test_raw_phy_applies_active_correction_like_post(self):
        stub = _rt(CLEAN_FIT, CORR_MASS_A)
        self.assertEqual(_corr(stub, "RAW_PHY"), CORR_MASS_A)
        self.assertEqual(_corr(stub, "POST_CORRECTION"), CORR_MASS_A)

    def test_pre_and_prebridge_phases_stay_uncorrected(self):
        stub = _rt(CLEAN_FIT, CORR_MASS_A)
        self.assertEqual(_corr(stub, "PRE_CORRECTION"), (0, 1))
        # bridge 이전(active_correction 미설정)은 phase 불문 (0,1).
        pre_bridge = _rt(CLEAN_FIT, None)
        for phase in ("PRE_CORRECTION", "POST_CORRECTION", "RAW_PHY"):
            self.assertEqual(_corr(pre_bridge, phase), (0, 1))


class SlotGridSignAndMagnitudeTest(unittest.TestCase):
    """RX 슬롯 hardware 시각의 부호·크기(+210.6µs 사례)."""

    def test_positive_correction_moves_window_earlier_by_correction(self):
        # beta=1: uncorrected - corrected == +c (양의 correction → 창이 c만큼
        # 이른 hardware 시각으로 이동). c_A=+210568.5ns.
        stub = _rt(CLEAN_FIT, CORR_MASS_A)
        T = _M0 + 30_000_000_000
        uncorrected = _h2h(stub, T, "PRE_CORRECTION")
        corrected = _h2h(stub, T, "RAW_PHY")
        shift = uncorrected - corrected
        # 421137/2 = 210568.5 → rne는 짝수쪽 210568.
        self.assertEqual(shift, 210568)
        self.assertGreater(shift, 0)

    def test_negative_correction_moves_window_later_symmetrically(self):
        stub = _rt(CLEAN_FIT, (-CORR_MASS_A[0], CORR_MASS_A[1]))
        T = _M0 + 30_000_000_000
        shift = _h2h(stub, T, "PRE_CORRECTION") - _h2h(stub, T, "RAW_PHY")
        self.assertEqual(shift, -210568)


class TxRxUseIdenticalConversionTest(unittest.TestCase):
    """TX 방사 격자와 RX 캡처 격자가 같은 함수·같은 phase로 동일 결과."""

    def test_plan_grid_equals_pointwise_host_to_hardware_raw_phy(self):
        stub = _rt(PPM_FIT, CORR_MASS_A)
        hosts = [_M0 + k * 120_000_000 for k in range(8)]
        rx_grid = _plan(stub, hosts, "RAW_PHY")           # RX 캡처 격자
        tx_grid = tuple(_h2h(stub, h, "RAW_PHY") for h in hosts)  # TX 방사(validate_rf_time)
        self.assertEqual(rx_grid, tx_grid)

    def test_default_plan_phase_is_uncorrected(self):
        # gain search·CFO 경로(기본 phase) 회귀 방지: correction 미적용.
        stub = _rt(PPM_FIT, CORR_MASS_A)
        hosts = [_M0 + k * 120_000_000 for k in range(4)]
        self.assertEqual(_plan(stub, hosts),
                         tuple(_h2h(stub, h, "PRE_CORRECTION") for h in hosts))


class NoDoubleApplicationTest(unittest.TestCase):
    """correction은 정확히 한 번만 적용된다."""

    def test_single_application_matches_inverse_corrected(self):
        stub = _rt(PPM_FIT, CORR_MASS_A)
        T = _M0 + 15_000_000_000
        once = _h2h(stub, T, "RAW_PHY")
        self.assertEqual(once, PPM_FIT.inverse_corrected(T, *CORR_MASS_A))

    def test_double_application_would_differ(self):
        # 이중 적용(2c)과 정확히 correction 공칭값(421137/2=210568.5ns)만큼
        # 다름을 두 fit 모두에서 확인해 회귀를 잡는다(반올림·ppm 허용 ±1).
        nominal = Fraction(*CORR_MASS_A)  # 210568.5 ns
        for label, fit in (("clean", CLEAN_FIT), ("ppm", PPM_FIT)):
            with self.subTest(fit=label):
                stub = _rt(fit, CORR_MASS_A)
                T = _M0 + 15_000_000_000
                once = _h2h(stub, T, "RAW_PHY")
                twice = fit.inverse_corrected(
                    T, 2 * CORR_MASS_A[0], CORR_MASS_A[1])
                self.assertNotEqual(once, twice)
                # 공칭 c 근방(반올림 ±0.5 + ppm 스케일 ~1.5ns) — 두 배(2c)나
                # 영(0)이 아님을 확인.
                self.assertLessEqual(abs((once - twice) - nominal), 3)


class AllPathsAlignmentTest(unittest.TestCase):
    """C1~C6 전 경로: 수정 후 burst 도달과 RX 창 시작이 정렬(잔차 0),
    수정 전 잔차는 수신 endpoint correction으로만 결정(송신·타 경로 무영향)."""

    def _residual(self, receiver, phase):
        """수신 endpoint 창 시작 대비 burst 도달의 offset(ns).

        모델(beta=1): TX가 correction된 시각에 방사하면 burst는 host 시각 T에
        발생한다. RX의 실제 RF 수신 시각 = inverse_corrected(T, c_R). 창 시작 =
        _host_raw_to_hardware(T, phase). 잔차 = 도달 - 창시작."""
        c_R = CORRECTIONS[receiver]
        stub = _rt(CLEAN_FIT, c_R)
        T = _M0 + 40_000_000_000
        arrival = CLEAN_FIT.inverse_corrected(T, *c_R)
        window = _h2h(stub, T, phase)
        return arrival - window

    def test_raw_phy_aligns_every_path(self):
        for path, (_tx, rx) in PATH_ENDPOINTS.items():
            with self.subTest(path=path, receiver=rx):
                self.assertEqual(self._residual(rx, "RAW_PHY"), 0)

    def test_uncorrected_residual_only_from_receiver_correction(self):
        expected = {
            "C1": -210568,  # recv MASS-A
            "C2": 0,        # recv ROC (c=0) — 수정 전에도 정렬, 부작용 없음
            "C3": -47607,   # recv MASS-B (190427/4 = 47606.75 → rne 47607)
            "C4": 0,        # recv ROC
            "C5": -47607,   # recv MASS-B
            "C6": -210568,  # recv MASS-A
        }
        for path, (_tx, rx) in PATH_ENDPOINTS.items():
            with self.subTest(path=path, receiver=rx):
                self.assertEqual(self._residual(rx, "PRE_CORRECTION"),
                                 expected[path])

    def test_roc_receiving_paths_unchanged_by_fix(self):
        # ROC 수신 경로(C2·C4)는 수정 전후 모두 잔차 0 — 회귀 없음 증명.
        for path in ("C2", "C4"):
            rx = PATH_ENDPOINTS[path][1]
            self.assertEqual(self._residual(rx, "PRE_CORRECTION"), 0)
            self.assertEqual(self._residual(rx, "RAW_PHY"), 0)


class PpmFitResidualTest(unittest.TestCase):
    """현실적 ppm fit에서도 수정 후 정렬 잔차가 반올림(≤1ns) 이내."""

    def test_raw_phy_residual_within_rounding(self):
        for receiver, c_R in CORRECTIONS.items():
            with self.subTest(receiver=receiver):
                stub = _rt(PPM_FIT, c_R)
                T = _M0 + 40_000_000_000
                arrival = PPM_FIT.inverse_corrected(T, *c_R)
                window = _h2h(stub, T, "RAW_PHY")
                self.assertLessEqual(abs(arrival - window), 1)


if __name__ == "__main__":
    unittest.main()
