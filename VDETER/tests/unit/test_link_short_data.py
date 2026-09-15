"""TC-LINK-007: Type 92/93 Short Data build·decode와 profile 경계."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.message import ShortDataAckBody
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import LinkId, SequentialId, StationId
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.crc import crc_bytes
from vdeter.link.packet.parser import parse_packet
from vdeter.link.short_data.codec import build_short_data_packet, decode_short_data_message
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


SOURCE = StationId(0x01020304)
DESTINATION = Addressed(StationId(0x05060708))
DIRECT_ROUTE = RouteAddresses(
    original_source=OriginalSource(SOURCE),
    original_destination=OriginalDestination(DESTINATION),
)
BROADCAST_ROUTE = RouteAddresses(
    original_source=OriginalSource(SOURCE),
    original_destination=OriginalDestination(Broadcast()),
)


class ShortDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = build_message_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        cls.profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        cls.messages.freeze()
        cls.profiles.freeze()

    def _build(
        self,
        link_id: int,
        route: RouteAddresses,
        ack_required: bool,
        retransmission_number: int | None,
        payload: bytes,
        sequential_id: int = 0x1234,
    ):
        return build_short_data_packet(
            LinkId(link_id),
            route,
            ack_required,
            retransmission_number,
            SequentialId(sequential_id),
            payload,
            self.messages,
            self.profiles,
        )

    def _decode(self, link_id: int, packet: bytes):
        parsed = parse_packet(LinkId(link_id), packet, self.messages, self.profiles)
        self.assertTrue(parsed.is_ok, getattr(parsed, "error", None))
        self.assertEqual(1, len(parsed.value.messages))
        return decode_short_data_message(parsed.value.messages[0])

    def test_TC_LINK_007_ack_and_no_ack_profile_capacity_boundaries(self):
        capacities = {11: (33, 34), 17: (213, 214), 19: (681, 682)}
        for link_id, (ack_capacity, no_ack_capacity) in capacities.items():
            for ack_required, capacity in ((True, ack_capacity), (False, no_ack_capacity)):
                with self.subTest(link_id=link_id, ack_required=ack_required, boundary="maximum"):
                    payload = bytes((index & 0xFF for index in range(capacity)))
                    retransmission = 254 if ack_required else None
                    built = self._build(
                        link_id,
                        DIRECT_ROUTE,
                        ack_required,
                        retransmission,
                        payload,
                    )
                    self.assertTrue(built.is_ok, getattr(built, "error", None))
                    self.assertEqual((92 if ack_required else 93,), built.value.message_types)
                    decoded = self._decode(link_id, built.value.packet)
                    self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                    self.assertEqual(DIRECT_ROUTE, decoded.value.route)
                    self.assertEqual(SequentialId(0x1234), decoded.value.sequential_id)
                    self.assertEqual(payload, decoded.value.presentation_data)
                with self.subTest(link_id=link_id, ack_required=ack_required, boundary="over"):
                    rejected = self._build(
                        link_id,
                        DIRECT_ROUTE,
                        ack_required,
                        0 if ack_required else None,
                        bytes(capacity + 1),
                    )
                    self.assertEqual(ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED, rejected.error.code)

    def test_TC_LINK_007_broadcast_is_no_ack_only(self):
        rejected = self._build(11, BROADCAST_ROUTE, True, 0, b"payload")
        self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, rejected.error.code)

        built = self._build(11, BROADCAST_ROUTE, False, None, b"payload")
        self.assertTrue(built.is_ok)
        decoded = self._decode(11, built.value.packet)
        self.assertEqual(BROADCAST_ROUTE, decoded.value.route)
        self.assertEqual(b"payload", decoded.value.presentation_data)

    def test_TC_LINK_007_profile_precedence_and_source_zero_are_structured(self):
        unknown_profile = self._build(63, DIRECT_ROUTE, False, None, b"")
        self.assertEqual(ErrorCode.LINK_PROFILE_NOT_FOUND, unknown_profile.error.code)

        zero_source = RouteAddresses(
            original_source=OriginalSource(StationId(0)),
            original_destination=OriginalDestination(DESTINATION),
        )
        rejected = self._build(11, zero_source, False, None, b"payload")
        self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, rejected.error.code)

    def test_TC_LINK_007_retransmission_number_ownership_and_low_level_255(self):
        for value in (0, 254):
            with self.subTest(value=value):
                built = self._build(11, DIRECT_ROUTE, True, value, b"payload")
                decoded = self._decode(11, built.value.packet)
                self.assertEqual(value, decoded.value.body.retransmission_number)

        for ack_required, value in ((True, None), (True, 255), (False, 0)):
            with self.subTest(ack_required=ack_required, value=value):
                rejected = self._build(11, DIRECT_ROUTE, ack_required, value, b"payload")
                self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, rejected.error.code)

        segment = bytes.fromhex("00000001aa")
        body = ShortDataAckBody(SOURCE, 0, DESTINATION, 255, segment)
        codec = self.messages.lookup(92).value.codec
        encoded = codec.encode(body)
        self.assertTrue(encoded.is_ok)
        self.assertEqual(body, codec.decode(encoded.value).value)

    def test_TC_LINK_007_segment_is_validated_before_presentation_delivery(self):
        packet = bytearray(self._build(11, DIRECT_ROUTE, False, None, b"payload").value.packet)
        packet[14:16] = b"\x00\x08"
        packet[-4:] = crc_bytes(bytes(packet[:-4]))

        parsed = parse_packet(LinkId(11), bytes(packet), self.messages, self.profiles)
        self.assertTrue(parsed.is_ok)
        decoded = decode_short_data_message(parsed.value.messages[0])
        self.assertEqual(ErrorCode.LINK_SEGMENT_LENGTH_INVALID, decoded.error.code)


if __name__ == "__main__":
    unittest.main()
