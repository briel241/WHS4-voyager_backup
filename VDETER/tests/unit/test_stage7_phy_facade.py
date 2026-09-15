"""Stage 7 진단 facade exact-equivalence·ingress 시험 (`stage7` §9.3, §15.1.2)."""

import unittest

from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    RadioSamplesReceived,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PhysicalChannel,
)
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.phy import wideband
from vdeter.phy.burst.rrc import to_cf32le
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry
from vdeter.b210_system.phy_facade import (
    Stage7DiagnosticPhyFacade,
    Stage7LaneRxIngress,
    Stage7PhyDiagnosticSink,
)
from vdeter.b210_system.routing import Stage7RouteSidecar

UPPER = PhysicalChannel("VDETER_UPPER_100_TEST")
UTC = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "stage7-facade-utc")
HW_CLOCK_ID = "stage7-b210-hardware-v1:run-1:MASS-A:1"


class _StaticTime:
    def __init__(self):
        self._utc = UTC
        self._mono = TimePoint(10, 0, ClockDomain.MONOTONIC, "stage7-facade-mono")

    def now_utc(self):
        return self._utc

    def now_monotonic(self):
        return self._mono


def _registry():
    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok
    return registry


def _l17_packet() -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")


def _l17_slot_block() -> SampleBlock:
    stages = wideband.encode_slot(17, _l17_packet())
    return SampleBlock(
        sample_format=SAMPLE_FORMAT_CF32LE,
        sample_rate_hz=307_200,
        sample_count=wideband.SLOT_SAMPLES,
        data=to_cf32le(stages.samples),
        first_sample_at=UTC,
        continuity=SampleContinuity.CONTIGUOUS,
    )


def _decode_request() -> PhyDecodeRequest:
    return PhyDecodeRequest(
        channel=UPPER,
        samples=_l17_slot_block(),
        allowed_link_ids=(LinkId(17),),
    )


def _sink(lane_index=0):
    sink = Stage7PhyDiagnosticSink(lane_index=lane_index, lane_generation=0)
    key = ("VDETER_UPPER_100_TEST", "clock", 0, 26_666_666)
    sink.begin_window(key, 1, 5_000_000_000)
    return sink


class TC_STAGE7_FACADE_EQUIVALENCE(unittest.TestCase):
    def test_frozen_and_facade_results_exact_equal(self):
        registry = _registry()
        frozen = MultiRatePhy(registry, _StaticTime(), DeterministicIdSource(0xF0))
        facade = Stage7DiagnosticPhyFacade(
            registry,
            _StaticTime(),
            DeterministicIdSource(0xF0),
            diagnostic_sink=_sink(),
        )
        left = frozen.decode_samples(_decode_request())
        right = facade.decode_samples(_decode_request())
        self.assertTrue(left.is_ok and right.is_ok)
        self.assertEqual(left.value.status, right.value.status)
        self.assertEqual(len(left.value.packets), 1)
        self.assertEqual(
            [(p.packet_id, p.link_id, p.channel, p.received_at, p.packet) for p in left.value.packets],
            [(p.packet_id, p.link_id, p.channel, p.received_at, p.packet) for p in right.value.packets],
        )
        records = facade.diagnostic_sink.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.link_id, 17)
        self.assertEqual(record.lane_index, 0)
        self.assertEqual(record.phy_attempt_index, 0)
        self.assertTrue(-2200.0 <= record.cfo_hz <= 2200.0)
        self.assertIsNotNone(record.packet_digest)

    def test_unbound_window_refuses_recovery(self):
        registry = _registry()
        sink = Stage7PhyDiagnosticSink(lane_index=0, lane_generation=0)
        facade = Stage7DiagnosticPhyFacade(
            registry, _StaticTime(), DeterministicIdSource(0xF1), diagnostic_sink=sink
        )
        result = facade.decode_samples(_decode_request())
        self.assertTrue(result.is_err)
        details = result.error.details.as_dict()
        self.assertEqual(details.get("stage7_private_code"), "PHY_DIAGNOSTIC_UNAVAILABLE")

    def test_two_lane_interleave_isolated(self):
        registry = _registry()
        lane0 = Stage7DiagnosticPhyFacade(
            registry, _StaticTime(), DeterministicIdSource(0xA0), diagnostic_sink=_sink(0)
        )
        lane1 = Stage7DiagnosticPhyFacade(
            registry, _StaticTime(), DeterministicIdSource(0xA1), diagnostic_sink=_sink(1)
        )
        first = lane0.decode_samples(_decode_request())
        second = lane1.decode_samples(_decode_request())
        third = lane0.decode_samples(_decode_request())
        self.assertTrue(first.is_ok and second.is_ok and third.is_ok)
        self.assertEqual(len(first.value.packets), 1)
        self.assertEqual(len(second.value.packets), 1)
        self.assertEqual(len(third.value.packets), 1)
        self.assertEqual(len(lane0.diagnostic_sink.records()), 2)
        self.assertEqual(len(lane1.diagnostic_sink.records()), 1)
        for record in lane0.diagnostic_sink.records():
            self.assertEqual(record.lane_index, 0)
        for record in lane1.diagnostic_sink.records():
            self.assertEqual(record.lane_index, 1)

    def test_tx_encode_does_not_touch_rx_streams(self):
        registry = _registry()
        facade = Stage7DiagnosticPhyFacade(
            registry, _StaticTime(), DeterministicIdSource(0xB0), diagnostic_sink=_sink()
        )
        ids = DeterministicIdSource(0xB1)
        before = dict(facade._rx_streams)
        result = facade.encode_packet(
            PhyEncodeRequest(
                packet_id=ids.new_packet_id(),
                link_id=LinkId(17),
                channel=UPPER,
                transmit_at=TimePoint(1_700_000_100, 0, ClockDomain.UTC, "stage7-facade-utc"),
                packet=_l17_packet(),
            )
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(dict(facade._rx_streams), before)

    def test_tx_route_lookup_enforced_with_sidecar(self):
        registry = _registry()
        sidecar = Stage7RouteSidecar()
        facade = Stage7DiagnosticPhyFacade(
            registry,
            _StaticTime(),
            DeterministicIdSource(0xB2),
            diagnostic_sink=_sink(),
            route_sidecar=sidecar,
        )
        ids = DeterministicIdSource(0xB3)
        packet_id = ids.new_packet_id()
        request = PhyEncodeRequest(
            packet_id=packet_id,
            link_id=LinkId(17),
            channel=UPPER,
            transmit_at=TimePoint(1_700_000_100, 0, ClockDomain.UTC, "stage7-facade-utc"),
            packet=_l17_packet(),
        )
        rejected = facade.encode_packet(request)
        self.assertTrue(rejected.is_err)
        self.assertEqual(
            rejected.error.details.as_dict().get("stage7_private_code"),
            "LANE_ROUTE_CONTEXT_MISSING",
        )
        # sidecar에 route entry를 만든 뒤에는 encode와 Burst 결속이 성공한다.
        pending = sidecar.begin_pending(
            "build_packet",
            "0" * 64,
            {
                "source": 111_111_111,
                "destination": ("ADDRESSED", 222_222_222),
                "original_source": None,
                "original_destination": None,
            },
        ).value
        sidecar.promote_packets(pending, (packet_id,), link_id=17)
        result = facade.encode_packet(request)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        burst = result.value
        bound = sidecar.reserve_burst(burst.burst_id.value, "MASS-A", "VDETER_LOWER_100_TEST")
        # lower channel로는 MASS-A→ROC가 canonical이다.
        self.assertTrue(bound.is_ok)


def _hw(seconds, nanoseconds):
    return TimePoint(seconds, nanoseconds, ClockDomain.RADIO_HARDWARE, HW_CLOCK_ID)


def _received(count, hw_point):
    block = SampleBlock(
        sample_format=SAMPLE_FORMAT_CF32LE,
        sample_rate_hz=307_200,
        sample_count=count,
        data=b"\x00" * (count * 8),
        first_sample_at=UTC,
        continuity=SampleContinuity.CONTIGUOUS,
    )
    return RadioSamplesReceived(
        channel=UPPER,
        samples=block,
        hardware_received_at=hw_point,
        continuity=SampleContinuity.CONTIGUOUS,
    )


def _ingress():
    return Stage7LaneRxIngress(
        run_id="run-1",
        endpoint_id="MASS-A",
        b210_serial="34D03DD",
        lane_index=0,
        lane_generation=0,
        radio_generation=1,
        expected_hardware_clock_id=HW_CLOCK_ID,
    )


WINDOW1 = ("VDETER_UPPER_100_TEST", "clock", 0, 26_666_666)
WINDOW2 = ("VDETER_UPPER_100_TEST", "clock", 26_666_666, 26_666_666)


class TC_STAGE7_INGRESS(unittest.TestCase):
    def _admit(self, ingress, key=WINDOW1):
        result = ingress.admit_window(
            key,
            timing_bridge_kind="PROVISIONAL",
            timing_clock_id="stage7-pi-host-affine-provisional-v1:run-1:0",
            acquisition_attempt=0,
            bridge_generation=None,
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))

    def test_clean_window_promotes_link11_quarter_projection(self):
        ingress = _ingress()
        self._admit(ingress)
        observed = ingress.observe_block(WINDOW1, _received(8192, _hw(5, 0)))
        self.assertTrue(observed.is_ok)
        promoted = ingress.promote_window(WINDOW1, 11)
        self.assertTrue(promoted.is_ok, getattr(promoted, "error", None))
        segment, epoch_id = promoted.value
        self.assertEqual(segment.device_sample_count, 8192)
        self.assertEqual(segment.phy_sample_count, 2048)
        self.assertEqual(segment.phy_sample_rate_hz, 76_800)
        self.assertEqual(segment.phy_stream_offset, 0)
        self.assertEqual(segment.first_sample_hardware_ns, 5_000_000_000)
        self.assertEqual(epoch_id, 1)

    def test_wideband_identity_projection(self):
        ingress = _ingress()
        self._admit(ingress)
        ingress.observe_block(WINDOW1, _received(8192, _hw(5, 0)))
        segment, _ = ingress.promote_window(WINDOW1, 17).value
        self.assertEqual(segment.phy_sample_count, 8192)
        self.assertEqual(segment.phy_sample_rate_hz, 307_200)

    def test_multi_block_continuity_and_offsets(self):
        ingress = _ingress()
        self._admit(ingress)
        # 8,192 = 4,096 + 4,096; 두 번째 block 시작은 t0 + 4096/307200 s.
        self.assertTrue(ingress.observe_block(WINDOW1, _received(4096, _hw(5, 0))).is_ok)
        next_ns = 5_000_000_000 + round(4096 * 1_000_000_000 / 307_200)
        self.assertTrue(
            ingress.observe_block(WINDOW1, _received(4096, _hw(5, next_ns - 5_000_000_000))).is_ok
        )
        promoted = ingress.promote_window(WINDOW1, 17)
        self.assertTrue(promoted.is_ok, getattr(promoted, "error", None))

    def test_gap_invalidates_epoch(self):
        ingress = _ingress()
        self._admit(ingress)
        ingress.observe_block(WINDOW1, _received(4096, _hw(5, 0)))
        # 1 ms gap → epoch invalid, 이후 promote 실패.
        ingress.observe_block(WINDOW1, _received(4096, _hw(5, 100_000_000)))
        promoted = ingress.promote_window(WINDOW1, 17)
        self.assertTrue(promoted.is_err)

    def test_wrong_hardware_clock_rejected(self):
        ingress = _ingress()
        self._admit(ingress)
        wrong = TimePoint(5, 0, ClockDomain.RADIO_HARDWARE, "other-clock")
        result = ingress.observe_block(WINDOW1, _received(4096, wrong))
        self.assertTrue(result.is_err)
        self.assertEqual(
            result.error.details.as_dict().get("stage7_private_code"),
            "RX_LANE_CONTEXT_INVALID",
        )

    def test_wrong_domain_rejected(self):
        ingress = _ingress()
        self._admit(ingress)
        wrong = TimePoint(5, 0, ClockDomain.UTC, HW_CLOCK_ID)
        result = ingress.observe_block(WINDOW1, _received(4096, wrong))
        self.assertTrue(result.is_err)

    def test_incomplete_window_promote_fails(self):
        ingress = _ingress()
        self._admit(ingress)
        ingress.observe_block(WINDOW1, _received(4096, _hw(5, 0)))
        promoted = ingress.promote_window(WINDOW1, 17)
        self.assertTrue(promoted.is_err)

    def test_consecutive_windows_share_epoch(self):
        ingress = _ingress()
        self._admit(ingress, WINDOW1)
        self._admit(ingress, WINDOW2)
        ingress.observe_block(WINDOW1, _received(8192, _hw(5, 0)))
        first, epoch1 = ingress.promote_window(WINDOW1, 17).value
        next_ns = round(8192 * 1_000_000_000 / 307_200)
        ingress.observe_block(WINDOW2, _received(8192, _hw(5, next_ns)))
        second, epoch2 = ingress.promote_window(WINDOW2, 17).value
        self.assertEqual(epoch1, epoch2)
        self.assertEqual(second.device_stream_offset, 8192)
        self.assertEqual(second.phy_stream_offset, 8192)

    def test_timing_identity_combinations(self):
        ingress = _ingress()
        bad = ingress.admit_window(
            WINDOW1,
            timing_bridge_kind="NONE",
            timing_clock_id="x",
            acquisition_attempt=None,
            bridge_generation=None,
        )
        self.assertTrue(bad.is_err)
        good = ingress.admit_window(
            WINDOW1,
            timing_bridge_kind="NONE",
            timing_clock_id=None,
            acquisition_attempt=None,
            bridge_generation=None,
        )
        self.assertTrue(good.is_ok)
        active_bad = ingress.admit_window(
            WINDOW2,
            timing_bridge_kind="ACTIVE",
            timing_clock_id="stage7-pi-host-affine-v1:run-1:0",
            acquisition_attempt=1,
            bridge_generation=0,
        )
        self.assertTrue(active_bad.is_err)


if __name__ == "__main__":
    unittest.main()
