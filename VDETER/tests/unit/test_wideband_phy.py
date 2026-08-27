"""wideband Link 17·19 PHY 체인과 multi-rate facade 단위시험.

요구사항: `stage6_phy_link17_baseline.md` §5~§9, `stage6_phy_link19_baseline.md` §5~§9,
`stage6_phy_common_baseline.md` §13.1~§13.5 (`DR-STAGE6-MR-001`).
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy import wideband
from vdeter.phy.burst.link_id import CODEWORDS
from vdeter.phy.burst.rrc import to_cf32le
from vdeter.phy.fec.interleaver import PERMUTATION_1BASED, general_permutation_zero_based
from vdeter.phy.modulation import qam16
from vdeter.phy.multilink import MultiRatePhy
from vdeter.phy.receiver import LinkIdNotAllowed, UnsupportedDetectedLinkId
from vdeter.profiles.link_phy import build_combined_phy_profile_registry
from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge


def _packet_for(link_id: int) -> bytes:
    size = wideband.PROFILES[link_id].packet_bytes
    return bytes((index * 7 + link_id) % 256 for index in range(size))


def _multirate_phy() -> MultiRatePhy:
    registry = build_combined_phy_profile_registry().value
    frozen = registry.freeze()
    assert frozen.is_ok
    return MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x61))


def _encode(phy: MultiRatePhy, link_id: int, packet: bytes):
    return phy.encode_packet(
        PhyEncodeRequest(
            packet_id=DeterministicIdSource(0x51 + link_id).new_packet_id(),
            link_id=LinkId(link_id),
            channel=CHANNEL,
            transmit_at=FIRST_SAMPLE_AT,
            packet=packet,
        )
    )


class WidebandConstantsTest(unittest.TestCase):
    def test_link_id_codewords_match_the_baselines(self):
        self.assertEqual(
            "10000111001101110010010011100101",
            "".join(str(bit) for bit in CODEWORDS[17]),
        )
        self.assertEqual(
            "10001111010010000010010000011010",
            "".join(str(bit) for bit in CODEWORDS[19]),
        )

    def test_general_interleaver_reproduces_frozen_link11_permutation(self):
        general = general_permutation_zero_based(432, 2, 216, (127, 191, 241, 5, 83, 109, 107, 179))
        self.assertEqual(tuple(value - 1 for value in PERMUTATION_1BASED), general)

    def test_channel_bit_counts(self):
        for link_id, expected in ((17, 3_754), (19, 7_508)):
            stages = wideband.encode_slot(link_id, _packet_for(link_id))
            self.assertEqual(expected, len(stages.channel_bits), msg=f"link {link_id}")
            self.assertEqual(1_920, len(stages.body_symbols))
            self.assertEqual(8_192, len(stages.samples))

    def test_qam16_constellation_is_gray_unit_energy(self):
        points = list(qam16.CONSTELLATION.values())
        energy = sum(abs(point) ** 2 for point in points) / len(points)
        self.assertAlmostEqual(1.0, energy, places=12)
        # Gray: 인접 level 간 label이 정확히 한 bit 다르다.
        for axis in (0, 2):
            ordered = ["00", "01", "11", "10"]
            for left, right in zip(ordered, ordered[1:]):
                difference = sum(a != b for a, b in zip(left, right))
                self.assertEqual(1, difference)

    def test_qam16_map_demap_round_trip(self):
        bits = tuple(int(bit) for bit in "1011000111100100")
        symbols = qam16.map_bits(bits)
        llrs = qam16.soft_demap(symbols, 1e-3)
        hard = tuple(0 if value >= 0.0 else 1 for value in llrs)
        self.assertEqual(bits, hard)

    def test_tx_scale_keeps_radio_full_scale(self):
        for link_id in (17, 19):
            stages = wideband.encode_slot(link_id, _packet_for(link_id))
            peak = max(
                max(abs(value.real), abs(value.imag)) for value in stages.samples
            )
            self.assertLess(peak, 1.0, msg=f"link {link_id}")
            self.assertTrue(all(value == 0j for value in stages.samples[-256:]))


class WidebandLoopbackTest(unittest.TestCase):
    def test_link17_direct_chain_round_trip(self):
        packet = _packet_for(17)
        stages = wideband.encode_slot(17, packet)
        result = wideband.WidebandReceiver().decode(stages.samples, frozenset((17,)))
        self.assertEqual(packet, result.packet)
        self.assertEqual(17, result.link_id)
        self.assertAlmostEqual(0.0, result.cfo_hz, delta=1.0)

    def test_link19_direct_chain_round_trip(self):
        packet = _packet_for(19)
        stages = wideband.encode_slot(19, packet)
        result = wideband.WidebandReceiver().decode(stages.samples, frozenset((19,)))
        self.assertEqual(packet, result.packet)
        self.assertEqual(19, result.link_id)

    def test_wrong_packet_length_is_rejected(self):
        for link_id in (17, 19):
            with self.assertRaises(ValueError):
                wideband.encode_slot(link_id, b"\x00" * 10)


class MultiRateFacadeTest(unittest.TestCase):
    def test_link11_round_trip_through_device_rate(self):
        phy = _multirate_phy()
        bridge = RuntimePhyRadioBridge(phy)
        packet = canonical_packet()
        encoded = _encode(phy, 11, packet)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(76_800, encoded.value.samples.sample_rate_hz)
        converted = bridge.to_device_burst(encoded.value)
        self.assertTrue(converted.is_ok, getattr(converted, "error", None))
        burst = converted.value
        self.assertEqual(307_200, burst.samples.sample_rate_hz)
        self.assertEqual(8_192, burst.samples.sample_count)
        prepared = bridge.to_native_decode_request(
            PhyDecodeRequest(
                channel=CHANNEL, samples=burst.samples, allowed_link_ids=(LinkId(11),)
            )
        )
        self.assertTrue(prepared.is_ok, getattr(prepared, "error", None))
        decoded = phy.decode_samples(prepared.value)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        recovered = decoded.value.packets[0]
        self.assertEqual(packet, bytes(recovered.packet))
        self.assertEqual(11, recovered.link_id.value)
        # §13.5 지연 보상: slot 시작 timestamp가 그대로 복원된다.
        self.assertEqual(
            FIRST_SAMPLE_AT.total_nanoseconds(),
            decoded.value.received_at.total_nanoseconds(),
        )

    def test_wideband_links_round_trip_and_keep_timestamps(self):
        phy = _multirate_phy()
        for link_id in (17, 19):
            packet = _packet_for(link_id)
            encoded = _encode(phy, link_id, packet)
            self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
            decoded = phy.decode_samples(
                PhyDecodeRequest(
                    channel=CHANNEL,
                    samples=encoded.value.samples,
                    allowed_link_ids=(LinkId(link_id),),
                )
            )
            self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
            recovered = decoded.value.packets[0]
            self.assertEqual(packet, bytes(recovered.packet), msg=f"link {link_id}")
            self.assertEqual(link_id, recovered.link_id.value)
            self.assertEqual(
                FIRST_SAMPLE_AT.total_nanoseconds(),
                decoded.value.received_at.total_nanoseconds(),
            )

    def test_mixed_rate_family_window_is_rejected(self):
        phy = _multirate_phy()
        bridge = RuntimePhyRadioBridge(phy)
        encoded = _encode(phy, 17, _packet_for(17))
        rejected = bridge.to_native_decode_request(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=encoded.value.samples,
                allowed_link_ids=(LinkId(11), LinkId(17)),
            )
        )
        self.assertTrue(rejected.is_err)
        self.assertEqual("CONFIG_SEMANTIC_INVALID", rejected.error.code)

    def test_public_phy_accepts_native_link11_rate(self):
        phy = _multirate_phy()
        native = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=76_800,
            sample_count=2_048,
            data=b"\x00" * (2_048 * 8),
            first_sample_at=FIRST_SAMPLE_AT,
        )
        result = phy.decode_samples(
            PhyDecodeRequest(channel=CHANNEL, samples=native, allowed_link_ids=(LinkId(11),))
        )
        self.assertTrue(result.is_ok)
        self.assertEqual("NO_BURST_DETECTED", result.value.status.value)

    def test_no_cross_link_fallback_between_17_and_19(self):
        phy = _multirate_phy()
        encoded = _encode(phy, 17, _packet_for(17))
        rejected = phy.decode_samples(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=encoded.value.samples,
                allowed_link_ids=(LinkId(19),),
            )
        )
        self.assertTrue(rejected.is_err)
        self.assertEqual("PHY_LINK_ID_NOT_ALLOWED", rejected.error.code)

    def test_link11_burst_is_not_detected_in_a_wideband_window(self):
        phy = _multirate_phy()
        bridge = RuntimePhyRadioBridge(phy)
        encoded = _encode(phy, 11, canonical_packet())
        converted = bridge.to_device_burst(encoded.value)
        self.assertTrue(converted.is_ok, getattr(converted, "error", None))
        prepared = bridge.to_native_decode_request(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=converted.value.samples,
                allowed_link_ids=(LinkId(17),),
            )
        )
        self.assertTrue(prepared.is_ok, getattr(prepared, "error", None))
        result = phy.decode_samples(prepared.value)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual("NO_BURST_DETECTED", result.value.status.value)
        self.assertEqual((), result.value.packets)

    def test_zero_windows_report_no_burst_on_both_families(self):
        phy = _multirate_phy()
        bridge = RuntimePhyRadioBridge(phy)
        zero = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=307_200,
            sample_count=8_192,
            data=b"\x00" * (8_192 * 8),
            first_sample_at=FIRST_SAMPLE_AT,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        for allowed in ((LinkId(11),), (LinkId(17),), (LinkId(19),)):
            prepared = bridge.to_native_decode_request(
                PhyDecodeRequest(channel=CHANNEL, samples=zero, allowed_link_ids=allowed)
            )
            self.assertTrue(prepared.is_ok)
            result = phy.decode_samples(prepared.value)
            self.assertTrue(result.is_ok)
            self.assertEqual("NO_BURST_DETECTED", result.value.status.value)


if __name__ == "__main__":
    unittest.main()
