"""TC-LINK-002..004: 13개 Message 기준 vector와 오류 경계."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import Addressed, Broadcast
from vdeter.common.contracts.message import (
    AcknowledgeNackBody,
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
    DecodedMessage,
    MediaAccessControlBody,
    PaddingBody,
    ResourceAllocationBody,
    ResourceRequestBody,
    ShortDataAckBody,
    ShortDataNoAckBody,
    TbbContinuationFragmentBody,
    TbbEndFragmentBody,
    TbbStartFragmentBody,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import LinkId, StationId
from vdeter.common.serialization import from_reference_json, to_reference_json
from vdeter.link.messages.builtins import build_message_registry
from vdeter.profiles.models import RegistryScope


SOURCE = StationId(0x01020304)
DESTINATION = Addressed(StationId(0x05060708))


def _vectors():
    return (
        (0, MediaAccessControlBody(SOURCE, 0, 0x101, 0x7F, 20), "00000c010203040001017f14"),
        (4, ResourceAllocationBody(SOURCE, DESTINATION, 0xFF, 2, LinkId(11), 1, 0, 0xAA), "0400110102030405060708ff020b0100aa"),
        (13, AcknowledgeNackBody(SOURCE, DESTINATION, 0, (1, 0x0203, 0xFFFF), 0x80, 3, 0), "0d001501020304050607080000010203ffff800300"),
        (20, TbbStartFragmentBody(SOURCE, 2, 2, 0x1234, 2, b"\xaa"), "14000d010203040202123402aa"),
        (21, TbbContinuationFragmentBody(SOURCE, 2, 0x1234, 1, b"\xbb"), "15000c0102030402123401bb"),
        (22, TbbEndFragmentBody(SOURCE, 2, 0x1234, 1, b"\xcc"), "16000c0102030402123401cc"),
        (74, DataStartFragmentBody(SOURCE, 0, DESTINATION, 3, 9, 1, b"\xdd"), "4a0010010203040005060708030901dd"),
        (75, DataContinuationFragmentBody(SOURCE, 0, DESTINATION, 3, 2, 10, b"\xee"), "4b001001020304000506070803020aee"),
        (76, DataEndFragmentBody(SOURCE, 0, DESTINATION, 3, 11, 0, b"\xff"), "4c0010010203040005060708030b00ff"),
        (81, PaddingBody(), "51"),
        (90, ResourceRequestBody(SOURCE, StationId(0x11121314), Broadcast(), DESTINATION, 0, 0x12), "5a0018010203041112131400000000050607080000000012"),
        (92, ShortDataAckBody(SOURCE, 0, DESTINATION, 3, b"\xab"), "5c000e01020304000506070803ab"),
        (93, ShortDataNoAckBody(SOURCE, 0, Broadcast(), b"\xab"), "5d000d010203040000000000ab"),
    )


class MessageCodecVectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_message_registry(RegistryScope.OPERATIONAL).value
        cls.registry.freeze()

    def test_TC_LINK_002_all_thirteen_reference_vectors_round_trip(self):
        for message_type, body, expected_hex in _vectors():
            with self.subTest(message_type=message_type):
                codec = self.registry.lookup(message_type).value.codec
                encoded = codec.encode(body)
                self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
                self.assertEqual(bytes.fromhex(expected_hex), encoded.value)
                decoded = codec.decode(encoded.value)
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(body, decoded.value)

    def test_TC_LINK_003_length_source_and_reserved_session_errors_are_stable(self):
        codec0 = self.registry.lookup(0).value.codec
        source_zero = bytes.fromhex("00000c000000000000000000")
        self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, codec0.decode(source_zero).error.code)
        reserved_priority = bytes.fromhex("00000c010203040101017f14")
        self.assertEqual(ErrorCode.LINK_RESERVED_FIELD_NONZERO, codec0.decode(reserved_priority).error.code)
        wrong_length = bytes.fromhex("00000b010203040001017f14")
        self.assertEqual(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, codec0.decode(wrong_length).error.code)
        type4_wrong_profile = bytes.fromhex("0400100102030405060708ff020b0100")
        self.assertEqual(ErrorCode.LINK_UNSUPPORTED_MESSAGE_PROFILE, self.registry.lookup(4).value.codec.decode(type4_wrong_profile).error.code)
        type93_nonzero_session = bytes.fromhex("5d000d010203040100000000ab")
        self.assertEqual(ErrorCode.LINK_RESERVED_FIELD_NONZERO, self.registry.lookup(93).value.codec.decode(type93_nonzero_session).error.code)

    def test_TC_LINK_004_big_endian_and_capability_numeric_bits(self):
        codec = self.registry.lookup(90).value.codec
        normal = ResourceRequestBody(SOURCE, StationId(0x11121314), Broadcast(), DESTINATION, 0, 2)
        simplified = ResourceRequestBody(SOURCE, StationId(0x11121314), Broadcast(), DESTINATION, 0, 0x12)
        self.assertEqual(b"\x00\x00\x00\x02", codec.encode(normal).value[-4:])
        self.assertEqual(b"\x00\x00\x00\x12", codec.encode(simplified).value[-4:])

    def test_TC_LINK_003_message_body_reference_json_uses_stable_tag(self):
        message = DecodedMessage(message_type=93, length=13, body=ShortDataNoAckBody(SOURCE, 0, Broadcast(), b"\xab"))
        encoded = to_reference_json(message)
        self.assertEqual("TYPE_93", encoded["body"]["kind"])
        self.assertEqual(message, from_reference_json(DecodedMessage, encoded))


if __name__ == "__main__":
    unittest.main()
