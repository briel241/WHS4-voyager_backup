"""Multi-rate PHY/Runtime bridge conflict and result-policy tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from vdeter.common.contracts.phy import BasebandBurst, PhyDecodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import BurstId, LinkId, PacketId, PhysicalChannel
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.phy import resampling, wideband
from vdeter.phy.burst.rrc import to_cf32le, validate_cf32le_full_scale
from vdeter.phy.receiver import FecDecodeFailed, ReceiverCoreResult
from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge
from tests.support.wideband import build_wideband_software_group


CHANNEL = PhysicalChannel("VDETER_UPPER_100_TEST")
UTC = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "multirate-test-utc")
PACKET_ID = PacketId("00000000-0000-4000-8000-000000000001")
BURST_ID = BurstId("00000000-0000-4000-8000-000000000002")


class _NativePhy:
    def __init__(self) -> None:
        self.resets = []

    def reset_stream(self, channel=None) -> None:
        self.resets.append(channel)


def _block(
    count: int = resampling.DEVICE_SLOT_SAMPLES,
    *,
    continuity: SampleContinuity = SampleContinuity.CONTIGUOUS,
    value: complex = 0j,
) -> SampleBlock:
    return SampleBlock(
        sample_format=SAMPLE_FORMAT_CF32LE,
        sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
        sample_count=count,
        data=to_cf32le((value,) * count),
        first_sample_at=UTC,
        continuity=continuity,
    )


def _result(
    epoch: float,
    packet: bytes = b"ok",
    *,
    score: float = 0.8,
    correlation: float = 0.9,
) -> ReceiverCoreResult:
    return ReceiverCoreResult(
        packet=packet,
        link_id=17,
        training_epoch_samples=epoch,
        rho_hat=1.0,
        correlation=correlation,
        peak_ratio=2.0,
        cfo_hz=0.0,
        gain_magnitude=1.0,
        noise_variance=1e-3,
        fec_codeword_score=score,
    )


class FullScaleContractTest(unittest.TestCase):
    def test_binary32_unit_circle_rejects_diagonal_counterexample(self):
        with self.assertRaises(ValueError):
            validate_cf32le_full_scale(to_cf32le((0.8 + 0.8j,)))
        validate_cf32le_full_scale(to_cf32le((0.5 + 0.5j,)))

    def test_runtime_bridge_projects_full_scale_failure_to_phy_invariant(self):
        bridge = RuntimePhyRadioBridge(_NativePhy())
        burst = BasebandBurst(
            burst_id=BURST_ID,
            packet_id=PACKET_ID,
            link_id=LinkId(17),
            channel=CHANNEL,
            transmit_at=UTC,
            samples=_block(value=0.8 + 0.8j),
        )
        converted = bridge.to_device_burst(burst)
        self.assertTrue(converted.is_err)
        self.assertEqual(ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, converted.error.code)


class RuntimeBridgeRxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.native = _NativePhy()
        self.bridge = RuntimePhyRadioBridge(self.native)

    def _request(self, block, allowed=(LinkId(11),)) -> PhyDecodeRequest:
        return PhyDecodeRequest(CHANNEL, block, allowed)

    def test_empty_or_mixed_device_family_is_rejected_before_phy(self):
        empty = self.bridge.to_native_decode_request(self._request(_block(), ()))
        mixed = self.bridge.to_native_decode_request(
            self._request(_block(), (LinkId(11), LinkId(17)))
        )
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, empty.error.code)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, mixed.error.code)

    def test_device_intent_must_be_exactly_one_supported_link(self):
        # §13.2: device-rate 허용 집합은 정확히 (11,), (17,), (19,) 중 하나다.
        same_family_pair = self.bridge.to_native_decode_request(
            self._request(_block(), (LinkId(17), LinkId(19)))
        )
        self.assertEqual(
            ErrorCode.CONFIG_SEMANTIC_INVALID, same_family_pair.error.code
        )
        unsupported = self.bridge.to_native_decode_request(
            self._request(_block(), (LinkId(63),))
        )
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, unsupported.error.code)
        # 순서·중복만 다른 tuple은 같은 canonical (17,)이므로 허용된다.
        duplicated = self.bridge.to_native_decode_request(
            self._request(_block(), (LinkId(17), LinkId(17)))
        )
        self.assertTrue(duplicated.is_ok)

    def test_short_or_gapped_radio_window_is_discarded_and_resets_state(self):
        short = self.bridge.to_native_decode_request(
            self._request(_block(resampling.DEVICE_SLOT_SAMPLES - 1))
        )
        gapped = self.bridge.to_native_decode_request(
            self._request(_block(continuity=SampleContinuity.GAP_BEFORE))
        )
        self.assertTrue(short.is_ok)
        self.assertIsNone(short.value)
        self.assertTrue(gapped.is_ok)
        self.assertIsNone(gapped.value)
        self.assertEqual([CHANNEL, CHANNEL], self.native.resets)

    def test_only_complete_link11_window_is_decimated_to_native_phy(self):
        prepared = self.bridge.to_native_decode_request(self._request(_block()))
        self.assertTrue(prepared.is_ok)
        self.assertEqual(resampling.NATIVE_SAMPLE_RATE_HZ, prepared.value.samples.sample_rate_hz)
        self.assertEqual(resampling.NATIVE_SLOT_SAMPLES, prepared.value.samples.sample_count)

    def test_wideband_native_rate_is_passed_without_conversion(self):
        request = self._request(_block(), (LinkId(17),))
        prepared = self.bridge.to_native_decode_request(request)
        self.assertTrue(prepared.is_ok)
        self.assertIs(request, prepared.value)


class NativePhyBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.clock, group = build_wideband_software_group(data_link_id=17)
        cls.endpoint = group._endpoints[0]
        cls.phy = cls.endpoint._phy

    def test_public_phy_keeps_link11_native_and_runtime_bridge_converts_it(self):
        from vdeter.common.contracts.phy import PhyEncodeRequest

        encoded = self.phy.encode_packet(
            PhyEncodeRequest(
                self.endpoint._dependencies.id_source.new_packet_id(),
                LinkId(11),
                CHANNEL,
                self.clock.now_utc(),
                bytes(54),
            )
        )
        self.assertTrue(encoded.is_ok)
        self.assertEqual(
            resampling.NATIVE_SAMPLE_RATE_HZ,
            encoded.value.samples.sample_rate_hz,
        )
        converted = self.endpoint._phy_radio_bridge.to_device_burst(encoded.value)
        self.assertTrue(converted.is_ok)
        self.assertEqual(
            resampling.DEVICE_SAMPLE_RATE_HZ,
            converted.value.samples.sample_rate_hz,
        )

    def test_global_precedence_clock_then_format_then_rate_then_finite(self):
        monotonic = TimePoint(1, 0, ClockDomain.MONOTONIC, "wrong-clock")
        wrong_clock = SampleBlock(
            SAMPLE_FORMAT_TEST_IQ_INT16_LE,
            1,
            0,
            b"",
            monotonic,
            SampleContinuity.UNKNOWN,
        )
        result = self.phy.decode_samples(
            PhyDecodeRequest(CHANNEL, wrong_clock, (LinkId(17),))
        )
        self.assertEqual(ErrorCode.CLOCK_DOMAIN_MISMATCH, result.error.code)

        wrong_rate_with_nan = SampleBlock(
            SAMPLE_FORMAT_CF32LE,
            123_456,
            1,
            to_cf32le((complex(float("nan"), 0.0),)),
            UTC,
            SampleContinuity.CONTIGUOUS,
        )
        result = self.phy.decode_samples(
            PhyDecodeRequest(CHANNEL, wrong_rate_with_nan, (LinkId(17),))
        )
        self.assertEqual(ErrorCode.PHY_SAMPLE_RATE_MISMATCH, result.error.code)

        supported_rate_with_nan = SampleBlock(
            SAMPLE_FORMAT_CF32LE,
            resampling.DEVICE_SAMPLE_RATE_HZ,
            1,
            to_cf32le((complex(float("nan"), 0.0),)),
            UTC,
            SampleContinuity.CONTIGUOUS,
        )
        result = self.phy.decode_samples(
            PhyDecodeRequest(CHANNEL, supported_rate_with_nan, (LinkId(17),))
        )
        self.assertEqual(ErrorCode.PHY_SYNCHRONIZATION_FAILED, result.error.code)

    def test_allowed_set_change_resets_native_receive_identity(self):
        empty = SampleBlock(
            SAMPLE_FORMAT_CF32LE,
            resampling.DEVICE_SAMPLE_RATE_HZ,
            0,
            b"",
            UTC,
            SampleContinuity.CONTIGUOUS,
        )
        self.phy.decode_samples(PhyDecodeRequest(CHANNEL, empty, (LinkId(17),)))
        with patch.object(type(self.phy), "reset_stream", autospec=True) as reset:
            self.phy.decode_samples(PhyDecodeRequest(CHANNEL, empty, (LinkId(19),)))
        reset.assert_called_once_with(self.phy, CHANNEL)


class CandidateResultPolicyTest(unittest.TestCase):
    def test_success_wins_over_failed_candidate(self):
        receiver = wideband.WidebandReceiver()
        success = _result(128.0)
        with patch.object(
            wideband,
            "acquire_candidates",
            return_value=(object(), object()),
        ), patch.object(
            receiver,
            "_decode_candidate",
            side_effect=(success, FecDecodeFailed("bad candidate")),
        ):
            decoded = receiver.decode_all((1 + 0j,) * wideband.SLOT_SAMPLES)
        self.assertEqual((success,), decoded)

    def test_multiple_successes_are_returned_in_burst_time_order(self):
        receiver = wideband.WidebandReceiver()
        # 서로 겹치지 않는 두 burst interval: [0, 8192)와 [8372, 16564).
        late = _result(8_500.0, b"late")
        early = _result(128.0, b"early")
        with patch.object(
            wideband,
            "acquire_candidates",
            return_value=(object(), object()),
        ), patch.object(
            receiver,
            "_decode_candidate",
            side_effect=(late, early),
        ):
            decoded = receiver.decode_all((1 + 0j,) * wideband.SLOT_SAMPLES)
        self.assertEqual((early, late), decoded)

    def test_overlapping_candidates_form_one_cluster_with_one_winner(self):
        # §7.6: 같은 물리 burst를 가리키는 겹친 성공 후보는 (Q desc, C desc, epoch asc)로
        # 하나만 선택한다.
        weak = _result(500.0, b"weak", score=0.5)
        strong = _result(128.0, b"strong", score=0.9)
        winners = wideband.select_burst_clusters(
            [weak, strong], slot_samples=wideband.SLOT_SAMPLES
        )
        self.assertEqual((strong,), winners)

    def test_exact_three_way_tie_cluster_is_synchronization_failure(self):
        receiver = wideband.WidebandReceiver()
        tied_a = _result(128.0, b"aa")
        tied_b = _result(128.0, b"bb")
        with patch.object(
            wideband,
            "acquire_candidates",
            return_value=(object(), object()),
        ), patch.object(
            receiver,
            "_decode_candidate",
            side_effect=(tied_a, tied_b),
        ):
            with self.assertRaises(wideband.SynchronizationFailed):
                receiver.decode_all((1 + 0j,) * wideband.SLOT_SAMPLES)

    def test_tie_failed_cluster_does_not_block_other_success_cluster(self):
        # exact tie로 실패한 cluster가 있어도 다른 성공 cluster는 반환된다.
        tied_a = _result(128.0, b"aa")
        tied_b = _result(128.0, b"bb")
        separate = _result(9_000.0, b"separate")
        winners = wideband.select_burst_clusters(
            [tied_a, tied_b, separate], slot_samples=wideband.SLOT_SAMPLES
        )
        self.assertEqual((separate,), winners)


if __name__ == "__main__":
    unittest.main()
