"""TC-PHY17-010/TC-PHY19-010: Stage 1 Link build → PHY → Link decode 직접 체인.

요구사항: link §14 — 실제 Stage 1 builder가 만든 Link Packet(wire CRC 포함)을
PHY loopback으로 통과시켜 byte-exact 복원하고, 복원 bytes를 Stage 1 parser가
CRC zero-residue·Message 배치까지 그대로 수락하는지 고정한다. PHY는 Packet을
opaque bytes로만 다루며 CRC를 재계산하지 않는다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE
from vdeter.common.contracts.message import MediaAccessControlBody
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId, StationId
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.builder import build_packet
from vdeter.link.packet.crc import has_zero_residue
from vdeter.link.packet.parser import parse_packet
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import (
    build_combined_phy_profile_registry,
    build_link_profile_registry,
)
from vdeter.profiles.models import RegistryScope


class _LinkChainMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        cls.messages = build_message_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        assert cls.messages.freeze().is_ok
        cls.link_profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        assert cls.link_profiles.freeze().is_ok
        phy_registry = build_combined_phy_profile_registry().value
        assert phy_registry.freeze().is_ok
        cls.phy = MultiRatePhy(phy_registry, TIME_SOURCE, DeterministicIdSource(0xC7))

    def test_link_build_phy_loopback_link_parse_round_trip(self):
        type0 = MediaAccessControlBody(StationId(0x01020304), 0, 0x101, 0x7F, 20)
        built = build_packet(
            LinkId(self.link_id), (type0,), self.messages, self.link_profiles
        )
        self.assertTrue(built.is_ok, getattr(built, "error", None))
        packet = built.value.packet
        self.assertTrue(has_zero_residue(packet))

        encoded = self.phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0xC8).new_packet_id(),
                link_id=LinkId(self.link_id),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=packet,
            )
        )
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        block = encoded.value.samples
        self.phy.reset_stream()
        decoded = self.phy.decode_samples(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=block.sample_rate_hz,
                    sample_count=block.sample_count,
                    data=block.data,
                    first_sample_at=FIRST_SAMPLE_AT,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=(LinkId(self.link_id),),
            )
        )
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        recovered = bytes(decoded.value.packets[0].packet)
        # PHY는 opaque 보존: bytes와 CRC residue가 그대로다.
        self.assertEqual(packet, recovered)
        self.assertTrue(has_zero_residue(recovered))
        parsed = parse_packet(
            LinkId(self.link_id), recovered, self.messages, self.link_profiles
        )
        self.assertTrue(parsed.is_ok, getattr(parsed, "error", None))
        types = [body.message_type for body in parsed.value.messages]
        self.assertIn(0, types)


class Link17ChainTest(_LinkChainMixin, unittest.TestCase):
    """TC-PHY17-010 direct chain."""

    link_id = 17


class Link19ChainTest(_LinkChainMixin, unittest.TestCase):
    """TC-PHY19-010 direct chain."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
