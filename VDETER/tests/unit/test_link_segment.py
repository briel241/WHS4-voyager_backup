"""TC-LINK-008, 011: VDE Segment wire와 Sequential ID allocator."""

from __future__ import annotations

import unittest

from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import DeterministicIdSource, SequentialId
from vdeter.link.segment.allocator import SEQUENTIAL_ID_CAPACITY, SequentialIdAllocator
from vdeter.link.segment.codec import decode_segment, encode_unsigned_segment


class VdeSegmentCodecTest(unittest.TestCase):
    def test_TC_LINK_008_type_zero_reference_vector_round_trip(self):
        encoded = encode_unsigned_segment(SequentialId(0x1234), b"\x00\x51\xff")
        self.assertTrue(encoded.is_ok)
        self.assertEqual(bytes.fromhex("123400030051ff"), encoded.value)

        decoded = decode_segment(encoded.value)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(SequentialId(0x1234), decoded.value.sequential_id)
        self.assertEqual(b"\x00\x51\xff", decoded.value.presentation_data)

    def test_TC_LINK_008_payload_and_sequential_id_boundaries(self):
        empty_wire = decode_segment(bytes.fromhex("00000000"))
        self.assertTrue(empty_wire.is_ok)
        self.assertEqual(b"", empty_wire.value.presentation_data)

        payload = bytes((index & 0xFF for index in range(65535)))
        maximum = encode_unsigned_segment(SequentialId(16383), payload)
        self.assertTrue(maximum.is_ok)
        self.assertEqual(bytes.fromhex("3fffffff"), maximum.value[:4])
        self.assertEqual(payload, decode_segment(maximum.value).value.presentation_data)

        empty_send = encode_unsigned_segment(SequentialId(0), b"")
        self.assertEqual(ErrorCode.LINK_SEGMENT_LENGTH_INVALID, empty_send.error.code)
        oversized = encode_unsigned_segment(SequentialId(0), bytes(65536))
        self.assertEqual(ErrorCode.LINK_SEGMENT_LENGTH_INVALID, oversized.error.code)

    def test_TC_LINK_008_signed_and_future_types_are_distinguished(self):
        cases = ((1, "SIGNED_UNSUPPORTED"), (2, "FUTURE_USE"), (3, "FUTURE_USE"))
        for segment_type, expected_class in cases:
            with self.subTest(segment_type=segment_type):
                header = ((segment_type << 14) | 7).to_bytes(2, "big") + b"\x00\x00"
                result = decode_segment(header)
                self.assertEqual(ErrorCode.LINK_UNSUPPORTED_SEGMENT_TYPE, result.error.code)
                self.assertEqual(segment_type, result.error.details.get("segment_type"))
                self.assertEqual(expected_class, result.error.details.get("segment_class"))

    def test_TC_LINK_008_declared_and_actual_length_must_match(self):
        for wire in (b"", b"\x00\x00\x00", bytes.fromhex("00000002aa"), bytes.fromhex("00000001aabb")):
            with self.subTest(wire=wire.hex()):
                result = decode_segment(wire)
                self.assertEqual(ErrorCode.LINK_SEGMENT_LENGTH_INVALID, result.error.code)


class SequentialIdAllocatorTest(unittest.TestCase):
    def test_TC_LINK_011_stable_allocation_and_caller_value_rejection(self):
        source = DeterministicIdSource(1101)
        allocator = SequentialIdAllocator()
        transfer = source.new_transfer_id()

        first = allocator.reserve_for_transfer(transfer)
        repeated = allocator.reserve_for_transfer(transfer)
        self.assertEqual(SequentialId(0), first.value)
        self.assertEqual(first.value, repeated.value)
        self.assertEqual(1, allocator.active_count)

        rejected = allocator.reserve_for_transfer(source.new_transfer_id(), SequentialId(9))
        self.assertEqual(ErrorCode.LINK_SEQUENTIAL_ID_CALLER_FORBIDDEN, rejected.error.code)
        self.assertEqual(1, allocator.active_count)

    def test_TC_LINK_011_increment_wrap_skip_exhaustion_and_terminal_reuse(self):
        source = DeterministicIdSource(1102)
        allocator = SequentialIdAllocator()
        owners = []

        for expected in range(SEQUENTIAL_ID_CAPACITY):
            transfer = source.new_transfer_id()
            owners.append(transfer)
            allocated = allocator.reserve_for_transfer(transfer)
            self.assertEqual(expected, allocated.value.value)

        retry = allocator.reserve_for_transfer(owners[123])
        self.assertTrue(retry.is_ok)
        self.assertEqual(SequentialId(123), retry.value)

        waiting = source.new_transfer_id()
        exhausted = allocator.reserve_for_transfer(waiting)
        self.assertEqual(ErrorCode.LINK_SEQUENTIAL_ID_EXHAUSTED, exhausted.error.code)
        self.assertTrue(exhausted.error.retryable)
        self.assertEqual(SEQUENTIAL_ID_CAPACITY, allocator.active_count)

        released_zero = allocator.release_after_terminal(owners[0])
        self.assertEqual(SequentialId(0), released_zero.value)
        wrapped = allocator.reserve_for_transfer(waiting)
        self.assertEqual(SequentialId(0), wrapped.value)

        released_five = allocator.release_after_terminal(owners[5])
        self.assertEqual(SequentialId(5), released_five.value)
        skipped = allocator.reserve_for_transfer(source.new_transfer_id())
        self.assertEqual(SequentialId(5), skipped.value)
        self.assertEqual(SEQUENTIAL_ID_CAPACITY, allocator.active_count)

        restarted = SequentialIdAllocator()
        self.assertEqual(
            SequentialId(0),
            restarted.reserve_for_transfer(source.new_transfer_id()).value,
        )


if __name__ == "__main__":
    unittest.main()
