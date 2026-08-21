"""TC-LINK-005, 006, 015: Packet 용량·padding·CRC와 원자적 오류 precedence."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import Addressed, Broadcast
from vdeter.common.contracts.message import (
    MediaAccessControlBody,
    PaddingBody,
    ResourceAllocationBody,
    ShortDataNoAckBody,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import LinkId, StationId
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.builder import build_packet
from vdeter.link.packet.crc import crc32, crc_bytes, has_zero_residue
from vdeter.link.packet.parser import parse_packet
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


SOURCE = StationId(0x01020304)
DESTINATION = Addressed(StationId(0x05060708))
TYPE0 = MediaAccessControlBody(SOURCE, 0, 0x101, 0x7F, 20)
TYPE4 = ResourceAllocationBody(SOURCE, DESTINATION, 0xFF, 2, LinkId(11), 1, 0, 0xAA)


class LinkPacketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = build_message_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        cls.profiles = build_link_profile_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        cls.messages.freeze()
        cls.profiles.freeze()

    def _build(self, link_id: int, bodies):
        return build_packet(LinkId(link_id), tuple(bodies), self.messages, self.profiles)

    def _parse(self, link_id: int, packet: bytes):
        return parse_packet(LinkId(link_id), packet, self.messages, self.profiles)

    def test_TC_LINK_006_official_crc_check_and_zero_residue(self):
        self.assertEqual(0x0376E6E7, crc32(b"123456789"))
        self.assertEqual(bytes.fromhex("0376e6e7"), crc_bytes(b"123456789"))
        self.assertTrue(has_zero_residue(b"123456789" + bytes.fromhex("0376e6e7")))

    def test_TC_LINK_005_link_profiles_build_exact_fixed_packets(self):
        expected_sizes = {11: (54, 50), 17: (234, 230), 19: (702, 698)}
        for link_id, (packet_size, message_region_size) in expected_sizes.items():
            with self.subTest(link_id=link_id):
                built = self._build(link_id, (TYPE0,))
                self.assertTrue(built.is_ok, getattr(built, "error", None))
                self.assertEqual(packet_size, len(built.value.packet))
                self.assertEqual(message_region_size - 12, built.value.padding_size)
                self.assertEqual(bytes((0x51,)) * built.value.padding_size, built.value.packet[12:-4])
                parsed = self._parse(link_id, built.value.packet)
                self.assertTrue(parsed.is_ok, getattr(parsed, "error", None))
                self.assertEqual(TYPE0, parsed.value.messages[0].body)

        expected_link11 = bytes.fromhex(
            "00000c010203040001017f14"
            "5151515151515151515151515151515151515151515151515151515151515151515151515151"
            "f8dbb227"
        )
        self.assertEqual(expected_link11, self._build(11, (TYPE0,)).value.packet)

    def test_TC_LINK_005_multiple_messages_preserve_order(self):
        built = self._build(11, (TYPE0, TYPE4))
        self.assertTrue(built.is_ok)
        self.assertEqual((0, 4), built.value.message_types)
        self.assertEqual(50 - 12 - 17, built.value.padding_size)
        parsed = self._parse(11, built.value.packet)
        self.assertEqual((TYPE0, TYPE4), tuple(item.body for item in parsed.value.messages))

    def test_TC_LINK_005_maximum_message_count_boundary(self):
        maximums = {11: 4, 17: 19, 19: 58}
        for link_id, maximum in maximums.items():
            with self.subTest(link_id=link_id, boundary="maximum"):
                built = self._build(link_id, (TYPE0,) * maximum)
                self.assertTrue(built.is_ok, getattr(built, "error", None))
                self.assertEqual(maximum, len(self._parse(link_id, built.value.packet).value.messages))
            with self.subTest(link_id=link_id, boundary="over"):
                rejected = self._build(link_id, (TYPE0,) * (maximum + 1))
                self.assertEqual(ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED, rejected.error.code)

    def test_TC_LINK_005_zero_padding_and_payload_0x51_are_not_misread(self):
        exact = ShortDataNoAckBody(SOURCE, 0, Broadcast(), bytes(range(38)))
        exact_packet = self._build(11, (exact,))
        self.assertEqual(0, exact_packet.value.padding_size)
        self.assertEqual(exact, self._parse(11, exact_packet.value.packet).value.messages[0].body)

        embedded = ShortDataNoAckBody(SOURCE, 0, Broadcast(), b"\x00\x51\x51\x01")
        embedded_packet = self._build(11, (embedded,))
        parsed = self._parse(11, embedded_packet.value.packet)
        self.assertTrue(parsed.is_ok)
        self.assertEqual(embedded, parsed.value.messages[0].body)

    def test_TC_LINK_005_empty_padding_and_capacity_violations_are_rejected(self):
        self.assertEqual(
            ErrorCode.LINK_PACKET_CONTENT_INVALID,
            self._build(11, ()).error.code,
        )
        self.assertEqual(
            ErrorCode.LINK_PACKET_CONTENT_INVALID,
            self._build(11, (PaddingBody(),)).error.code,
        )
        too_large = ShortDataNoAckBody(SOURCE, 0, Broadcast(), bytes(39))
        self.assertEqual(
            ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED,
            self._build(11, (too_large,)).error.code,
        )
        all_padding_region = bytes((0x51,)) * 50
        all_padding = all_padding_region + crc_bytes(all_padding_region)
        self.assertEqual(
            ErrorCode.LINK_PACKET_CONTENT_INVALID,
            self._parse(11, all_padding).error.code,
        )

    def test_TC_LINK_005_invalid_padding_after_boundary_is_rejected(self):
        packet = bytearray(self._build(11, (TYPE0,)).value.packet)
        packet[20] = 0x52
        packet[-4:] = crc_bytes(bytes(packet[:-4]))
        result = self._parse(11, bytes(packet))
        self.assertEqual(ErrorCode.LINK_PADDING_INVALID, result.error.code)

    def test_TC_LINK_006_message_or_crc_bit_error_fails_before_decode(self):
        original = self._build(11, (TYPE0,)).value.packet
        for index in (5, len(original) - 1):
            with self.subTest(byte_index=index):
                corrupted = bytearray(original)
                corrupted[index] ^= 0x01
                result = self._parse(11, bytes(corrupted))
                self.assertEqual(ErrorCode.LINK_CRC_MISMATCH, result.error.code)

    def test_TC_LINK_015_validation_precedence_is_deterministic(self):
        valid = self._build(11, (TYPE0,)).value.packet
        self.assertEqual(
            ErrorCode.LINK_PROFILE_NOT_FOUND,
            parse_packet(LinkId(63), b"", self.messages, self.profiles).error.code,
        )
        self.assertEqual(
            ErrorCode.LINK_PACKET_LENGTH_INVALID,
            self._parse(11, valid[:-1]).error.code,
        )

        invalid_length_and_crc = bytearray(valid)
        invalid_length_and_crc[1:3] = b"\x00\x00"
        invalid_length_and_crc[-1] ^= 1
        self.assertEqual(
            ErrorCode.LINK_CRC_MISMATCH,
            self._parse(11, bytes(invalid_length_and_crc)).error.code,
        )

        invalid_length_and_crc[-4:] = crc_bytes(bytes(invalid_length_and_crc[:-4]))
        self.assertEqual(
            ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
            self._parse(11, bytes(invalid_length_and_crc)).error.code,
        )

        invalid_field_and_padding = bytearray(valid)
        invalid_field_and_padding[3:7] = bytes(4)
        invalid_field_and_padding[20] = 0x52
        invalid_field_and_padding[-4:] = crc_bytes(bytes(invalid_field_and_padding[:-4]))
        self.assertEqual(
            ErrorCode.LINK_PADDING_INVALID,
            self._parse(11, bytes(invalid_field_and_padding)).error.code,
        )

    def test_TC_LINK_015_packet_rejection_is_atomic(self):
        packet = bytearray(self._build(11, (TYPE0, TYPE4)).value.packet)
        second_message_offset = 12
        packet[second_message_offset + 3 : second_message_offset + 7] = bytes(4)
        packet[-4:] = crc_bytes(bytes(packet[:-4]))
        result = self._parse(11, bytes(packet))
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, result.error.code)
        self.assertFalse(hasattr(result, "value"))

        unknown = bytearray(self._build(11, (TYPE0,)).value.packet)
        unknown[0:3] = bytes.fromhex("fa0003")
        unknown[3:] = bytes((0x51,)) * (50 - 3) + crc_bytes(bytes.fromhex("fa0003") + bytes((0x51,)) * (50 - 3))
        result = self._parse(11, bytes(unknown))
        self.assertEqual(ErrorCode.LINK_CODEC_NOT_REGISTERED, result.error.code)


if __name__ == "__main__":
    unittest.main()
