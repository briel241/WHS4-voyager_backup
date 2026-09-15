"""TC-PHY-MULTI-010: Runtime bridge accepted RX-window ledger terminal 계약.

요구사항: `stage6_phy_common_baseline.md` §13.2 (`DR-STAGE6-MR-001`) — sample event는
terminal event까지 보관하고, 정상 completion에서만 PHY를 정확히 한 번 호출하며,
timeout/cancellation/incomplete/overflow/gap과 중복·비결속·ambiguous sample은 PHY 호출
없이 buffer 폐기·native reset·internal invariant로 처리한다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    RadioRxWindowCompleted,
    RadioSamplesReceived,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.phy import resampling
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry
from vdeter.runtime.phy_bridge import (
    INTEGRITY_INVALID,
    RuntimePhyRadioBridge,
)

GENERATION = 1
DURATION_NS = 26_666_666  # round-half-up으로 정확히 8,192 device samples
MONO = TimePoint(10, 0, ClockDomain.MONOTONIC, "ledger-test-monotonic")


def _multirate_phy() -> MultiRatePhy:
    registry = build_combined_phy_profile_registry().value
    frozen = registry.freeze()
    assert frozen.is_ok
    return MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x71))


def _link17_packet() -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")


class _CountingPhy:
    """decode 호출 횟수를 세는 native PHY wrapper."""

    def __init__(self, inner: MultiRatePhy) -> None:
        self.inner = inner
        self.decode_calls = 0
        self.reset_calls = []

    def decode_samples(self, request):
        self.decode_calls += 1
        return self.inner.decode_samples(request)

    def encode_packet(self, request):
        return self.inner.encode_packet(request)

    def reset_stream(self, channel=None) -> None:
        self.reset_calls.append(channel)
        self.inner.reset_stream(channel)


class LedgerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        phy = _multirate_phy()
        # control 정상 sample: Link 11 canonical slot의 §13.5 보간 결과.
        encoded11 = phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0x81).new_packet_id(),
                link_id=LinkId(11),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=canonical_packet(),
            )
        )
        assert encoded11.is_ok, encoded11.error
        seed_bridge = RuntimePhyRadioBridge(phy)
        converted = seed_bridge.to_device_burst(encoded11.value)
        assert converted.is_ok, converted.error
        cls.control_device_data = converted.value.samples.data
        # data 정상 sample: Link 17 canonical slot (native == device rate).
        encoded17 = phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0x82).new_packet_id(),
                link_id=LinkId(17),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=_link17_packet(),
            )
        )
        assert encoded17.is_ok, encoded17.error
        cls.data_device_data = encoded17.value.samples.data

    def setUp(self) -> None:
        self.phy = _CountingPhy(_multirate_phy())
        self.bridge = RuntimePhyRadioBridge(self.phy)
        self.bridge.begin_generation(GENERATION)

    # --- helpers -----------------------------------------------------------

    def _admit(self, start=FIRST_SAMPLE_AT, allowed=(11,), duration=DURATION_NS):
        admitted = self.bridge.admit_window(CHANNEL, start, duration, tuple(allowed))
        self.assertTrue(admitted.is_ok, getattr(admitted, "error", None))
        return start

    def _samples(self, data: bytes, epoch, continuity=SampleContinuity.CONTIGUOUS):
        count = len(data) // 8
        block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            sample_count=count,
            data=data,
            first_sample_at=epoch,
            continuity=continuity,
        )
        return RadioSamplesReceived(CHANNEL, block, MONO, continuity)

    def _completion(self, start=FIRST_SAMPLE_AT, duration=DURATION_NS):
        return RadioRxWindowCompleted(
            configure_generation=GENERATION,
            channel=CHANNEL,
            requested_start_at=start,
            requested_duration_nanoseconds=duration,
            expected_sample_count=resampling.DEVICE_SLOT_SAMPLES,
            received_sample_count=resampling.DEVICE_SLOT_SAMPLES,
            completed_at=MONO,
        )

    def _fault_details(self, start=FIRST_SAMPLE_AT, duration=DURATION_NS):
        return {
            "configure_generation": GENERATION,
            "channel": CHANNEL.name,
            "requested_start_seconds": start.seconds,
            "requested_start_nanoseconds": start.nanoseconds,
            "requested_duration_nanoseconds": duration,
        }


class LedgerTerminalTest(LedgerFixture):
    def test_TC_PHY_MULTI_010_1_control_success_decodes_exactly_once(self):
        self._admit(allowed=(11,))
        observed = self.bridge.observe_samples(
            self._samples(self.control_device_data, FIRST_SAMPLE_AT)
        )
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))
        # terminal 전 PHY 호출 0회.
        self.assertEqual(0, self.phy.decode_calls)
        prepared = self.bridge.complete_window(self._completion())
        self.assertTrue(prepared.is_ok, getattr(prepared, "error", None))
        request = prepared.value
        self.assertEqual(resampling.NATIVE_SAMPLE_RATE_HZ, request.samples.sample_rate_hz)
        self.assertEqual(resampling.NATIVE_SLOT_SAMPLES, request.samples.sample_count)
        decoded = self.phy.decode_samples(request)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(1, self.phy.decode_calls)
        self.assertEqual(canonical_packet(), bytes(decoded.value.packets[0].packet))
        self.assertEqual(11, decoded.value.packets[0].link_id.value)
        self.assertEqual((), self.bridge.ledger_snapshot())

    def test_TC_PHY_MULTI_010_2_data_success_decodes_exactly_once(self):
        self._admit(allowed=(17,))
        observed = self.bridge.observe_samples(
            self._samples(self.data_device_data, FIRST_SAMPLE_AT)
        )
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))
        self.assertEqual(0, self.phy.decode_calls)
        prepared = self.bridge.complete_window(self._completion())
        self.assertTrue(prepared.is_ok, getattr(prepared, "error", None))
        request = prepared.value
        # Link 17·19는 무변환 pass-through다.
        self.assertEqual(resampling.DEVICE_SAMPLE_RATE_HZ, request.samples.sample_rate_hz)
        self.assertEqual(resampling.DEVICE_SLOT_SAMPLES, request.samples.sample_count)
        decoded = self.phy.decode_samples(request)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(1, self.phy.decode_calls)
        self.assertEqual(_link17_packet(), bytes(decoded.value.packets[0].packet))
        self.assertEqual(17, decoded.value.packets[0].link_id.value)

    def test_TC_PHY_MULTI_010_3_timeout_discards_and_resets_without_phy(self):
        self._admit(allowed=(11,))
        recorded = self.bridge.record_rx_fault(
            ErrorCode.RADIO_RX_TIMEOUT, self._fault_details()
        )
        self.assertTrue(recorded.is_ok)
        self.assertEqual(0, self.phy.decode_calls)
        self.assertEqual(1, self.bridge.native_reset_count)
        self.assertEqual((), self.bridge.ledger_snapshot())

    def test_TC_PHY_MULTI_010_4_incomplete_after_partial_control_samples(self):
        self._admit(allowed=(11,))
        prefix = self.control_device_data[: 4_096 * 8]
        observed = self.bridge.observe_samples(self._samples(prefix, FIRST_SAMPLE_AT))
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))
        recorded = self.bridge.record_rx_fault(
            ErrorCode.RADIO_RX_WINDOW_INCOMPLETE, self._fault_details()
        )
        self.assertTrue(recorded.is_ok)
        self.assertEqual(0, self.phy.decode_calls)
        self.assertEqual(1, self.bridge.native_reset_count)
        self.assertEqual((), self.bridge.ledger_snapshot())

    def test_TC_PHY_MULTI_010_5_overflow_is_invalid_not_terminal(self):
        self._admit(allowed=(11,))
        prefix = self.control_device_data[: 2_048 * 8]
        observed = self.bridge.observe_samples(self._samples(prefix, FIRST_SAMPLE_AT))
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))
        # nonterminal overflow: terminal count는 늘지 않고 invalid로 한 번 전이한다.
        recorded = self.bridge.record_rx_fault(
            ErrorCode.RADIO_RX_OVERFLOW, self._fault_details()
        )
        self.assertTrue(recorded.is_ok)
        snapshot = self.bridge.ledger_snapshot()
        self.assertEqual(1, len(snapshot))
        self.assertEqual(INTEGRITY_INVALID, snapshot[0]["integrity_state"])
        self.assertIsNone(snapshot[0]["terminal_state"])
        self.assertEqual(0, snapshot[0]["buffered_bytes"])
        self.assertEqual(1, self.bridge.native_reset_count)
        # 이후 GAP_BEFORE sample은 진단 count만 남기고 추가 reset이 없다.
        gap_epoch = FIRST_SAMPLE_AT.shifted(6_666_667)
        gap = self._samples(
            self.control_device_data[2_048 * 8 : 6_144 * 8],
            gap_epoch,
            SampleContinuity.GAP_BEFORE,
        )
        observed = self.bridge.observe_samples(gap)
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))
        snapshot = self.bridge.ledger_snapshot()
        self.assertEqual(0, snapshot[0]["buffered_bytes"])
        self.assertEqual(4_096, snapshot[0]["diagnostic_sample_count"])
        self.assertEqual(1, self.bridge.native_reset_count)
        # 후속 incomplete terminal에서도 추가 reset·PHY 호출이 없다.
        recorded = self.bridge.record_rx_fault(
            ErrorCode.RADIO_RX_WINDOW_INCOMPLETE, self._fault_details()
        )
        self.assertTrue(recorded.is_ok)
        self.assertEqual(1, self.bridge.native_reset_count)
        self.assertEqual(0, self.phy.decode_calls)
        self.assertEqual((), self.bridge.ledger_snapshot())

    def test_TC_PHY_MULTI_010_6_cancellation_discards_without_phy(self):
        self._admit(allowed=(11,))
        recorded = self.bridge.record_rx_fault(
            ErrorCode.RADIO_OPERATION_CANCELLED, self._fault_details()
        )
        self.assertTrue(recorded.is_ok)
        self.assertEqual(0, self.phy.decode_calls)
        self.assertEqual(1, self.bridge.native_reset_count)
        self.assertEqual((), self.bridge.ledger_snapshot())

    def test_TC_PHY_MULTI_010_7_duplicate_sample_after_terminal_is_invariant(self):
        self._admit(allowed=(17,))
        event = self._samples(self.data_device_data, FIRST_SAMPLE_AT)
        self.assertTrue(self.bridge.observe_samples(event).is_ok)
        prepared = self.bridge.complete_window(self._completion())
        self.assertTrue(prepared.is_ok)
        self.assertIsNotNone(prepared.value)
        # 같은 sample event 재전달: internal invariant, 추가 PHY 호출 없음.
        duplicate = self.bridge.observe_samples(event)
        self.assertTrue(duplicate.is_err)
        self.assertEqual(
            ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, duplicate.error.code
        )
        self.assertEqual(0, self.phy.decode_calls)

    def test_TC_PHY_MULTI_010_8_unmatched_channel_or_time_is_invariant(self):
        self._admit(allowed=(11,))
        outside = self._samples(
            self.control_device_data, FIRST_SAMPLE_AT.shifted(DURATION_NS + 1_000)
        )
        observed = self.bridge.observe_samples(outside)
        self.assertTrue(observed.is_err)
        self.assertEqual(
            ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, observed.error.code
        )
        self.assertEqual(0, self.phy.decode_calls)

    def test_TC_PHY_MULTI_010_9_ambiguous_overlapping_windows_are_invariant(self):
        # Stage 3가 방지해야 하는 중복 interval 두 개를 bridge 단위 seam에 주입한다.
        self._admit(start=FIRST_SAMPLE_AT, allowed=(11,))
        self._admit(start=FIRST_SAMPLE_AT.shifted(1_000), allowed=(11,))
        inside_both = self._samples(
            self.control_device_data, FIRST_SAMPLE_AT.shifted(2_000)
        )
        observed = self.bridge.observe_samples(inside_both)
        self.assertTrue(observed.is_err)
        self.assertEqual(
            ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, observed.error.code
        )
        self.assertEqual(0, self.phy.decode_calls)

    def test_TC_PHY_MULTI_010_10_empty_intent_rejected_before_ledger_and_radio(self):
        rejected = self.bridge.validate_window_intent(())
        self.assertTrue(rejected.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, rejected.error.code)
        # ledger entry는 만들어지지 않았다.
        self.assertEqual((), self.bridge.ledger_snapshot())
        # 정합: 정상 intent는 canonical tuple로 수락된다.
        for allowed, canonical in (((11,), (11,)), ((17, 17), (17,)), ((19,), (19,))):
            accepted = self.bridge.validate_window_intent(
                tuple(LinkId(value) for value in allowed)
            )
            self.assertTrue(accepted.is_ok)
            self.assertEqual(canonical, accepted.value)


class LedgerTimingValidationTest(LedgerFixture):
    def test_plan_time_and_expected_count_must_agree(self):
        # §13.2 item 4: 26,666,666/26,666,667 ns는 정확히 8,192 samples가 된다.
        for duration in (26_666_666, 26_666_667):
            checked = self.bridge.validate_window_timing(
                duration, resampling.DEVICE_SAMPLE_RATE_HZ
            )
            self.assertTrue(checked.is_ok, msg=str(duration))
            self.assertEqual(resampling.DEVICE_SLOT_SAMPLES, checked.value)
        mismatch = self.bridge.validate_window_timing(
            13_333_333, resampling.DEVICE_SAMPLE_RATE_HZ
        )
        self.assertTrue(mismatch.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, mismatch.error.code)
        wrong_rate = self.bridge.validate_window_timing(26_666_666, 76_800)
        self.assertTrue(wrong_rate.is_err)
        self.assertEqual(ErrorCode.CONFIG_SEMANTIC_INVALID, wrong_rate.error.code)


if __name__ == "__main__":
    unittest.main()
