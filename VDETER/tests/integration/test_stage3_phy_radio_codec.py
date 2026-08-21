"""TC-RADIO-014 additive Stage 2 PHY ↔ Stage 3 cf32le operating path."""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest, PhyRecoveryStatus
from vdeter.common.contracts.radio import SAMPLE_FORMAT_CF32LE
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy.transmitter import Link11Phy
from vdeter.profiles.link_phy import build_phy_profile_registry
from vdeter.profiles.models import RegistryScope


class PhyRadioCodecTest(unittest.TestCase):
    def test_TC_RADIO_014_cf32le_round_trip_preserves_stage2_detector(self):
        registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        self.assertTrue(registry.freeze().is_ok)
        ids = DeterministicIdSource(0x3301)
        phy = Link11Phy(registry, TIME_SOURCE, ids, sample_format=SAMPLE_FORMAT_CF32LE)
        packet = canonical_packet()
        encoded = phy.encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(11), CHANNEL, FIRST_SAMPLE_AT, packet,
        ))
        self.assertTrue(encoded.is_ok, encoded)
        self.assertEqual(SAMPLE_FORMAT_CF32LE, encoded.value.samples.sample_format)
        self.assertEqual(2048 * 8, len(encoded.value.samples.data))
        decoded = phy.decode_samples(PhyDecodeRequest(
            CHANNEL, encoded.value.samples, (LinkId(11),),
        ))
        self.assertTrue(decoded.is_ok, decoded)
        self.assertEqual(PhyRecoveryStatus.RECOVERED, decoded.value.status)
        self.assertEqual(packet, decoded.value.packets[0].packet)


if __name__ == "__main__":
    unittest.main()
