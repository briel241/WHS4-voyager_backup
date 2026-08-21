"""TC-PHY11-008..010: ideal, opaque deterministic and Stage 1 boundary loopbacks."""

from __future__ import annotations

import struct
import unittest

from tests.support.phy11 import (
    CHANNEL,
    FIRST_SAMPLE_AT,
    canonical_packet,
    load_json,
    opaque_packets,
    operational_phy,
    vector_samples,
)
from vdeter.common.contracts.message import MediaAccessControlBody
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest, PhyRecoveryStatus
from vdeter.common.identifiers import DeterministicIdSource, LinkId, StationId
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.builder import build_packet
from vdeter.link.packet.crc import has_zero_residue
from vdeter.link.packet.parser import parse_packet
from vdeter.phy.burst.framing import bits_to_bytes
from vdeter.phy.burst.link_id import detect
from vdeter.phy.burst.rrc import from_test_iq_int16_le, to_test_iq_int16_le
from vdeter.phy.fec.puncturing import rate_recover
from vdeter.phy.fec.turbo import decode as turbo_decode
from vdeter.phy.modulation.pi4_qpsk import soft_demap
from vdeter.phy.receiver import Phy11Receiver
from vdeter.phy.scrambling import descramble_llrs
from vdeter.phy.transmitter import Phy11Transmitter
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


class Phy11LoopbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = canonical_packet()
        cls.stages = load_json("tx_stages_v1.json")
        cls.transmitter = Phy11Transmitter()
        cls.receiver = Phy11Receiver()

    @staticmethod
    def _float_from_bits(value: int) -> float:
        return struct.unpack("<d", struct.pack("<Q", value))[0]

    @staticmethod
    def _float_bits(value: float) -> int:
        return struct.unpack("<Q", struct.pack("<d", value))[0]

    def test_TC_PHY11_008_ideal_inverse_vector_to_packet(self):
        inverse = self.stages["ideal_rx_inverse"]
        body_symbols = tuple(complex(real, imag) for real, imag in self.stages["body_symbols_iq"])
        noise_variance = self._float_from_bits(inverse["noise_variance_f64_bits"])
        link_llrs = soft_demap(body_symbols[27:43], 27, noise_variance)
        self.assertEqual(inverse["link_llr_f64_bits"], [self._float_bits(value) for value in link_llrs])
        self.assertEqual(inverse["expected_link_id"], detect(link_llrs))
        data_llrs = soft_demap(body_symbols[43:480], 43, noise_variance)
        self.assertEqual(inverse["data_llr_f64_bits"], [self._float_bits(value) for value in data_llrs])
        descrambled = descramble_llrs(data_llrs)
        self.assertEqual(inverse["descrambled_llr_f64_bits"], [self._float_bits(value) for value in descrambled])
        data_rows, tail_rows = rate_recover(descrambled)
        self.assertEqual(
            inverse["rate_recovered_data_f64_bits"],
            [[self._float_bits(value) for value in row] for row in data_rows],
        )
        self.assertEqual(
            inverse["rate_recovered_tail_f64_bits"],
            [[self._float_bits(value) for value in row] for row in tail_rows],
        )
        information_bits = turbo_decode(descrambled)
        self.assertEqual(inverse["expected_information_bits"], "".join(map(str, information_bits)))
        self.assertEqual(self.packet, bits_to_bytes(information_bits))

        result = self.receiver.decode(vector_samples("rx_samples_v1/ideal.cf32le"))
        self.assertEqual(self.packet, result.packet)
        self.assertEqual(11, result.link_id)
        self.assertTrue(has_zero_residue(result.packet))

    def test_TC_PHY11_009_fixed_and_256_deterministic_packet_loopback(self):
        packets = (self.packet,) + opaque_packets(256)
        for index, packet in enumerate(packets):
            with self.subTest(packet_index=index):
                samples = self.transmitter.encode(packet).samples
                projected = from_test_iq_int16_le(to_test_iq_int16_le(samples))
                recovered = self.receiver.decode(projected)
                self.assertEqual(packet, recovered.packet)

    def test_TC_PHY11_010_stage1_build_phy_loopback_link_decode(self):
        messages = build_message_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        profiles = build_link_profile_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        self.assertTrue(messages.freeze().is_ok)
        self.assertTrue(profiles.freeze().is_ok)
        body = MediaAccessControlBody(StationId(0x01020304), 0, 0x101, 0x7F, 20)
        built = build_packet(LinkId(11), (body,), messages, profiles)
        self.assertTrue(built.is_ok, getattr(built, "error", None))

        phy = operational_phy()
        packet_id = DeterministicIdSource(1010).new_packet_id()
        encoded = phy.encode_packet(PhyEncodeRequest(
            packet_id=packet_id,
            link_id=LinkId(11),
            channel=CHANNEL,
            transmit_at=FIRST_SAMPLE_AT,
            packet=built.value.packet,
        ))
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        decoded = phy.decode_samples(PhyDecodeRequest(
            channel=CHANNEL,
            samples=encoded.value.samples,
            allowed_link_ids=(LinkId(11),),
        ))
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertIs(PhyRecoveryStatus.RECOVERED, decoded.value.status)
        self.assertEqual(1, len(decoded.value.packets))
        parsed = parse_packet(LinkId(11), decoded.value.packets[0].packet, messages, profiles)
        self.assertTrue(parsed.is_ok, getattr(parsed, "error", None))
        self.assertEqual(body, parsed.value.messages[0].body)


if __name__ == "__main__":
    unittest.main()
