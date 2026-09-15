"""Stage 5 shared-medium: 고정 상수·common-LO tuning·capability gate·topology 단위시험.

RF-off 요구 매핑: 1(fan-out 행렬), 3(sender별 TUNED/NOT_TUNED), 5(target->channel),
12(capability fail-closed), production 경계 값이 G1·명세에서만 유도됨.
"""

from __future__ import annotations

import unittest

from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.types import PhysicalChannel
from vdeter.poc.shared_medium import constants as C
from vdeter.poc.shared_medium import topology as T
from vdeter.poc.shared_medium import tuning as TU


class G1DerivedConstantsTest(unittest.TestCase):
    def test_common_lo_and_dsp_values(self):
        self.assertEqual(C.COMMON_RF_LO_HZ, 2_449_537_500)
        self.assertEqual(C.LOWER_DSP_HZ, 2_300_000)
        self.assertEqual(C.UPPER_DSP_HZ, -2_300_000)
        self.assertEqual(C.OUTPUT_RATE_SPS, 307_200)
        self.assertEqual(C.ANALOG_RX_BANDWIDTH_HZ, 4_700_000)

    def test_effective_center_is_lo_minus_dsp(self):
        # effective = RF LO - DSP (G1 실측 규약). 재추정하지 않는다.
        self.assertEqual(C.LOWER_EFFECTIVE_CENTER_HZ, C.COMMON_RF_LO_HZ - C.LOWER_DSP_HZ)
        self.assertEqual(C.UPPER_EFFECTIVE_CENTER_HZ, C.COMMON_RF_LO_HZ - C.UPPER_DSP_HZ)
        self.assertEqual(C.LOWER_EFFECTIVE_CENTER_HZ, 2_447_237_500)
        self.assertEqual(C.UPPER_EFFECTIVE_CENTER_HZ, 2_451_837_500)
        self.assertLess(C.LOWER_EFFECTIVE_CENTER_HZ, C.UPPER_EFFECTIVE_CENTER_HZ)

    def test_effective_centers_match_existing_stage7_rf_tune(self):
        # 교차검증: G1 유도 effective center가 기존 Stage 7 configuration의 RF tune과 일치.
        from vdeter.stage7 import configuration as CFG

        self.assertEqual(C.LOWER_EFFECTIVE_CENTER_HZ, CFG.LOWER_RF_TUNE_HZ)
        self.assertEqual(C.UPPER_EFFECTIVE_CENTER_HZ, CFG.UPPER_RF_TUNE_HZ)


class CommonLoTuningPlanTest(unittest.TestCase):
    def test_plan_derived_from_constants(self):
        plan = TU.derive_dual_channel_plan()
        self.assertEqual(plan.common_rf_lo_hz, C.COMMON_RF_LO_HZ)
        self.assertEqual(plan.lower_dsp_hz, C.LOWER_DSP_HZ)
        self.assertEqual(plan.upper_dsp_hz, C.UPPER_DSP_HZ)
        self.assertEqual(plan.effective_center_hz(PhysicalChannel.LOWER), C.LOWER_EFFECTIVE_CENTER_HZ)
        self.assertEqual(plan.effective_center_hz(PhysicalChannel.UPPER), C.UPPER_EFFECTIVE_CENTER_HZ)
        self.assertEqual(plan.output_rate_sps, 307_200)
        self.assertEqual(plan.analog_rx_bandwidth_hz, 4_700_000)


class CapabilityGateTest(unittest.TestCase):
    """WSM-REQ-12: dual-channel은 설치가 지원할 때만 광고, 미지원 required면 fail-closed."""

    def test_not_advertised_when_not_installed(self):
        self.assertEqual(TU.advertised_capabilities(dual_channel_installed=False), frozenset())

    def test_advertised_when_installed(self):
        self.assertIn(
            TU.DUAL_CHANNEL_CAPABILITY,
            TU.advertised_capabilities(dual_channel_installed=True),
        )

    def test_required_but_unsupported_fails_closed(self):
        with self.assertRaises(PocError) as ctx:
            TU.resolve_dual_channel(required=True, dual_channel_installed=False)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)
        self.assertEqual(
            ctx.exception.detail.get("reason"), TU.BROADCAST_CAPABILITY_UNAVAILABLE
        )

    def test_not_required_returns_none_no_fallback(self):
        self.assertIsNone(TU.resolve_dual_channel(required=False, dual_channel_installed=False))

    def test_required_and_supported_returns_plan(self):
        plan = TU.resolve_dual_channel(required=True, dual_channel_installed=True)
        self.assertEqual(plan.common_rf_lo_hz, C.COMMON_RF_LO_HZ)


class TopologyTest(unittest.TestCase):
    def test_sender_fanout_cables(self):
        self.assertEqual(T.fanout_cables("ROC"), ("C1", "C3"))
        self.assertEqual(T.fanout_cables("MASS-A"), ("C2", "C5"))
        self.assertEqual(T.fanout_cables("MASS-B"), ("C4", "C6"))

    def test_physical_reach_two_non_sending_endpoints(self):
        self.assertEqual(set(T.physical_reach("ROC")), {"MASS-A", "MASS-B"})
        self.assertEqual(set(T.physical_reach("MASS-A")), {"ROC", "MASS-B"})
        self.assertEqual(set(T.physical_reach("MASS-B")), {"ROC", "MASS-A"})

    def test_target_to_canonical_channel(self):
        self.assertEqual(T.attack_channel("ROC"), PhysicalChannel.LOWER)
        self.assertEqual(T.attack_channel("MASS-A"), PhysicalChannel.UPPER)

    def test_role_tuning_unchanged(self):
        self.assertEqual(T.role_rx_channel("ROC"), PhysicalChannel.LOWER)
        self.assertEqual(T.role_rx_channel("MASS-A"), PhysicalChannel.UPPER)
        self.assertEqual(T.role_rx_channel("MASS-B"), PhysicalChannel.UPPER)

    def test_natural_tuned_matrix_matches_spec_5_5(self):
        self.assertEqual(T.natural_tuned("ROC", PhysicalChannel.UPPER), frozenset({"MASS-A", "MASS-B"}))
        self.assertEqual(T.natural_tuned("MASS-A", PhysicalChannel.LOWER), frozenset({"ROC"}))
        self.assertEqual(T.natural_tuned("MASS-A", PhysicalChannel.UPPER), frozenset({"MASS-B"}))
        self.assertEqual(T.natural_tuned("MASS-B", PhysicalChannel.LOWER), frozenset({"ROC"}))
        self.assertEqual(T.natural_tuned("MASS-B", PhysicalChannel.UPPER), frozenset({"MASS-A"}))

    def test_dual_channel_mass_b_observes_both_only_when_active(self):
        self.assertEqual(
            T.receiver_tuned_channels("MASS-B", dual_channel_active=False),
            frozenset({PhysicalChannel.UPPER}),
        )
        self.assertEqual(
            T.receiver_tuned_channels("MASS-B", dual_channel_active=True),
            frozenset({PhysicalChannel.LOWER, PhysicalChannel.UPPER}),
        )

    def test_dual_channel_does_not_change_roc_massa_tuning(self):
        for active in (False, True):
            self.assertEqual(
                T.receiver_tuned_channels("ROC", dual_channel_active=active),
                frozenset({PhysicalChannel.LOWER}),
            )
            self.assertEqual(
                T.receiver_tuned_channels("MASS-A", dual_channel_active=active),
                frozenset({PhysicalChannel.UPPER}),
            )


if __name__ == "__main__":
    unittest.main()
