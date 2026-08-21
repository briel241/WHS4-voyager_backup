"""TC-LINK-021, 022, 033: Table 47/48 logical TBB wire 경계."""

from __future__ import annotations

import unittest

from vdeter.common.contracts.message import (
    BulletinBoardBody,
    ControlStationServiceArea,
    LogicalChannelDefinition,
    LogicalChannelFunction,
    LogicalChannelGroup,
    PhysicalChannelBandwidth,
    PhysicalChannelDefinition,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.exceptions import InvalidValueError
from vdeter.link.tbb.wire import decode_bulletin_board_payload, encode_bulletin_board_payload, encode_unsigned_bulletin_board


def _groups(definitions=()):
    return (LogicalChannelGroup(0, tuple(definitions)),) + tuple(LogicalChannelGroup(index, ()) for index in range(1, 6))


def _channel(number=1, groups=None):
    return PhysicalChannelDefinition(
        channel_number=number,
        frequency=2,
        bandwidth=PhysicalChannelBandwidth.KHZ_25,
        transmit_enabled=True,
        random_access_selection_interval=3,
        short_data_message_limit=4,
        logical_channel_groups=_groups() if groups is None else groups,
    )


def _body(channels):
    return BulletinBoardBody(
        start_time=0x01020304,
        validity=5,
        tdma_frame_size=15,
        physical_channels=tuple(channels),
        modulation_coding_protocol=0,
        service_area=ControlStationServiceArea(0, 0, 0, 0, 0),
    )


class TbbLogicalWireTest(unittest.TestCase):
    def test_TC_LINK_021_table47_n_is_last_zero_based_index(self):
        one = encode_unsigned_bulletin_board(_body((_channel(),))).value
        two = encode_unsigned_bulletin_board(_body((_channel(1), _channel(2)))).value
        self.assertEqual(0, one[7])
        self.assertEqual(1, two[7])

    def test_TC_LINK_021_022_canonical_empty_group_vector_round_trip(self):
        body = _body((_channel(),))
        expected_unsigned = bytes.fromhex(
            "01020304 0005 0f 00 01 0021 0184 0000000000 00 000000000000000000"
        )
        self.assertEqual(expected_unsigned, encode_unsigned_bulletin_board(body).value)
        fingerprint = bytes.fromhex("010203040506")
        signature = bytes(range(64))
        encoded = encode_bulletin_board_payload(body, fingerprint, signature).value
        decoded = decode_bulletin_board_payload(encoded)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual((body, fingerprint, signature), decoded.value)

    def test_TC_LINK_022_future_bandwidth_and_undefined_function_are_rejected(self):
        body = _body((_channel(),))
        encoded = bytearray(encode_bulletin_board_payload(body, bytes(6), bytes(64)).value)
        encoded[10] |= 0x02
        self.assertEqual(ErrorCode.LINK_UNSUPPORTED_MESSAGE_PROFILE, decode_bulletin_board_payload(bytes(encoded)).error.code)

        definition = LogicalChannelDefinition(LogicalChannelFunction.BULLETIN_BOARD, False, 1)
        function_encoded = bytearray(encode_bulletin_board_payload(_body((_channel(groups=_groups((definition,))),)), bytes(6), bytes(64)).value)
        function_encoded[13] |= 0x03
        self.assertEqual(ErrorCode.LINK_INVALID_MESSAGE_FIELD, decode_bulletin_board_payload(bytes(function_encoded)).error.code)

    def test_TC_LINK_033_lc_count_zero_one_sixty_three_and_sixty_four(self):
        for count in (0, 1, 63):
            definitions = tuple(LogicalChannelDefinition(LogicalChannelFunction.DATA, False, index) for index in range(count))
            body = _body((_channel(groups=_groups(definitions)),))
            encoded = encode_bulletin_board_payload(body, bytes(6), bytes(64))
            self.assertTrue(encoded.is_ok, f"count={count}")
            self.assertEqual(body, decode_bulletin_board_payload(encoded.value).value[0])
        with self.assertRaises(InvalidValueError):
            LogicalChannelGroup(0, tuple(LogicalChannelDefinition(LogicalChannelFunction.DATA, False, 0) for _ in range(64)))


if __name__ == "__main__":
    unittest.main()
