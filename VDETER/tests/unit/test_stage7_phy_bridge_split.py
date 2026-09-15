"""`complete_window` 분리와 native reset sink의 계약.

Stage 7 decode worker는 창 조립(펌프 소유 상태)과 리샘플(옮길 수 있는 계산)을
따로 실행해야 한다. 그래서 `complete_window`를 두 단계로 나눴다. 이 시험이
고정하는 것은 **분리가 동치라는 것**과, sink를 두지 않으면 기존 동작이 한
글자도 변하지 않는다는 것이다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    RadioRxWindowCompleted,
    RadioSamplesReceived,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.phy import resampling
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry
from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge

GENERATION = 1
DURATION_NS = 26_666_666
MONO = TimePoint(10, 0, ClockDomain.MONOTONIC, "split-test-monotonic")


def _phy() -> MultiRatePhy:
    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok
    return MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x71))


class _RecordingPhy:
    """native reset 호출을 기록하는 wrapper."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.reset_calls = []

    def decode_samples(self, request):
        return self.inner.decode_samples(request)

    def encode_packet(self, request):
        return self.inner.encode_packet(request)

    def reset_stream(self, channel=None) -> None:
        self.reset_calls.append(channel)
        self.inner.reset_stream(channel)


class BridgeSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        phy = _phy()
        encoded = phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0x81).new_packet_id(),
                link_id=LinkId(11),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=canonical_packet(),
            )
        )
        assert encoded.is_ok, encoded.error
        converted = RuntimePhyRadioBridge(phy).to_device_burst(encoded.value)
        assert converted.is_ok, converted.error
        cls.device_data = converted.value.samples.data

    def _bridge(self):
        bridge = RuntimePhyRadioBridge(_RecordingPhy(_phy()))
        bridge.begin_generation(GENERATION)
        return bridge

    def _fill(self, bridge):
        admitted = bridge.admit_window(
            CHANNEL, FIRST_SAMPLE_AT, DURATION_NS, (11,)
        )
        self.assertTrue(admitted.is_ok, getattr(admitted, "error", None))
        block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            sample_count=len(self.device_data) // 8,
            data=self.device_data,
            first_sample_at=FIRST_SAMPLE_AT,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        observed = bridge.observe_samples(
            RadioSamplesReceived(
                CHANNEL, block, MONO, SampleContinuity.CONTIGUOUS
            )
        )
        self.assertTrue(observed.is_ok, getattr(observed, "error", None))

    def _completion(self):
        return RadioRxWindowCompleted(
            configure_generation=GENERATION,
            channel=CHANNEL,
            requested_start_at=FIRST_SAMPLE_AT,
            requested_duration_nanoseconds=DURATION_NS,
            expected_sample_count=resampling.DEVICE_SLOT_SAMPLES,
            received_sample_count=resampling.DEVICE_SLOT_SAMPLES,
            completed_at=MONO,
        )

    # --- 분리 동치 ---------------------------------------------------------

    def test_split_is_equivalent_to_the_composed_call(self):
        """complete_window == complete_device_window + to_native_decode_request."""

        composed_bridge = self._bridge()
        self._fill(composed_bridge)
        composed = composed_bridge.complete_window(self._completion())
        self.assertTrue(composed.is_ok, getattr(composed, "error", None))

        split_bridge = self._bridge()
        self._fill(split_bridge)
        device = split_bridge.complete_device_window(self._completion())
        self.assertTrue(device.is_ok, getattr(device, "error", None))
        split = split_bridge.to_native_decode_request(device.value)
        self.assertTrue(split.is_ok, getattr(split, "error", None))

        self.assertEqual(composed.value, split.value)

    def test_device_stage_stops_before_resampling(self):
        """분리 1단계는 device rate·8,192 sample 그대로여야 한다."""

        bridge = self._bridge()
        self._fill(bridge)
        device = bridge.complete_device_window(self._completion())
        self.assertTrue(device.is_ok, getattr(device, "error", None))
        request = device.value
        self.assertIsInstance(request, PhyDecodeRequest)
        self.assertEqual(
            resampling.DEVICE_SAMPLE_RATE_HZ, request.samples.sample_rate_hz
        )
        self.assertEqual(
            resampling.DEVICE_SLOT_SAMPLES, request.samples.sample_count
        )
        self.assertEqual(self.device_data, request.samples.data)

    def test_device_stage_consumes_the_ledger_entry_exactly_once(self):
        """장부 정리는 1단계에 있다 — 재호출은 결속 실패여야 한다."""

        bridge = self._bridge()
        self._fill(bridge)
        first = bridge.complete_device_window(self._completion())
        self.assertTrue(first.is_ok, getattr(first, "error", None))
        again = bridge.complete_device_window(self._completion())
        self.assertTrue(again.is_err)

    # --- native reset sink -------------------------------------------------

    def test_without_sink_reset_reaches_the_native_phy(self):
        """기본 동작 불변 — sink를 두지 않으면 native PHY를 직접 부른다."""

        bridge = self._bridge()
        bridge.reset_stream(CHANNEL)
        self.assertEqual([CHANNEL], bridge._native_phy.reset_calls)

    def test_sink_replaces_the_native_reset_and_keeps_ledger_ownership(self):
        """sink가 있으면 PHY reset은 위임되고, ledger는 그대로 bridge가 비운다."""

        bridge = self._bridge()
        seen = []
        bridge.set_native_reset_sink(seen.append)

        bridge.reset_stream(CHANNEL)
        self.assertEqual([CHANNEL], seen)
        self.assertEqual([], bridge._native_phy.reset_calls,
                         "sink가 있는데 native PHY를 직접 불렀다")

        self._fill(bridge)
        self.assertTrue(bridge.ledger_snapshot())
        bridge.reset_stream(None)
        self.assertEqual([CHANNEL, None], seen)
        self.assertFalse(bridge.ledger_snapshot(),
                         "channel=None reset이 ledger를 비우지 않았다")

    def test_invariant_reset_path_also_goes_through_the_sink(self):
        """_reset_native(무결성 위반 경로)도 sink를 거쳐야 순서가 보존된다."""

        bridge = self._bridge()
        seen = []
        bridge.set_native_reset_sink(seen.append)
        before = bridge._native_reset_count
        bridge._reset_native(CHANNEL)
        self.assertEqual([CHANNEL], seen)
        self.assertEqual(before + 1, bridge._native_reset_count,
                         "펌프측 reset 카운터는 계속 bridge가 센다")


if __name__ == "__main__":
    unittest.main()
