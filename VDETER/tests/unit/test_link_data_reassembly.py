"""TC-LINK-010, 012, 016, 018, 024, 026, 031: Data Session reassembly."""

from __future__ import annotations

import dataclasses
import unittest

from vdeter.common.addressing import (
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    FragmentProcessStatus,
    LinkFragmentProcessRequest,
    LinkReassemblyControlRequest,
    ReassemblyControlKind,
)
from vdeter.common.contracts.mac import MacAction, MacActionKind
from vdeter.common.contracts.message import (
    DataContinuationFragmentBody,
    DataStartFragmentBody,
    DecodedMessage,
)
from vdeter.common.errors import Correlation, ErrorCode
from vdeter.common.identifiers import DeterministicIdSource, LinkId, SequentialId, StationId
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.link.data_session.fragmentation import create_data_session_tx_context
from vdeter.link.data_session.reassembly import DataSessionReassembler
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.parser import parse_packet
from vdeter.link.segment.codec import encode_unsigned_segment
from vdeter.link.short_data.codec import build_short_data_packet, decode_short_data_message
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


ORIGINAL_SOURCE = StationId(0x01020304)
ORIGINAL_DESTINATION = Addressed(StationId(0x05060708))
NODE_SOURCE = StationId(0x11121314)
NODE_DESTINATION = Addressed(StationId(0x15161718))
DIRECT_ROUTE = RouteAddresses(
    OriginalSource(ORIGINAL_SOURCE),
    OriginalDestination(ORIGINAL_DESTINATION),
)
NODE_ROUTE = RouteAddresses(
    OriginalSource(ORIGINAL_SOURCE),
    OriginalDestination(ORIGINAL_DESTINATION),
    NodeSource(NODE_SOURCE),
    NodeDestination(NODE_DESTINATION),
)
RECEIVED_AT = TimePoint(1, 0, ClockDomain.UTC, "test.utc")
CAPACITIES = {11: 35, 17: 215, 19: 683}


class DataReassemblyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = build_message_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        cls.profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        cls.messages.freeze()
        cls.profiles.freeze()
        cls.ids = DeterministicIdSource(1010)

    def _open(self, reassembler, handle, route=DIRECT_ROUTE):
        return reassembler.control(
            LinkReassemblyControlRequest(
                ReassemblyControlKind.OPEN,
                handle,
                route,
                Correlation(session_handle=handle),
            )
        )

    def _control(self, reassembler, kind, handle, route=None):
        return reassembler.control(
            LinkReassemblyControlRequest(
                kind,
                handle,
                route,
                Correlation(session_handle=handle),
            )
        )

    def _tx_context(self, payload, route=DIRECT_ROUTE, sequential_id=0x1234):
        handle = self.ids.new_session_handle()
        created = create_data_session_tx_context(
            self.ids.new_transfer_id(),
            handle,
            route,
            SequentialId(sequential_id),
            payload,
        )
        self.assertTrue(created.is_ok, getattr(created, "error", None))
        return handle, created.value

    def _build_all(self, context, link_id, frame_limit=14):
        outputs = []
        frame = 0
        capacity = CAPACITIES[link_id]
        while not context.complete:
            remaining_fragments = (context.remaining_bytes + capacity - 1) // capacity
            count = min(frame_limit, remaining_fragments)
            for position in range(1, count + 1):
                built = context.build_fragment(
                    LinkId(link_id),
                    frame,
                    count,
                    position,
                    self.messages,
                    self.profiles,
                )
                self.assertTrue(built.is_ok, getattr(built, "error", None))
                outputs.append((link_id, built.value))
            frame += 1
        return outputs

    def _request(self, handle, route, link_id, output, *, packet_id=None, message=None):
        if message is None:
            parsed = parse_packet(
                LinkId(link_id), output.packet.packet, self.messages, self.profiles
            )
            self.assertTrue(parsed.is_ok, getattr(parsed, "error", None))
            message = parsed.value.messages[0]
        return LinkFragmentProcessRequest(
            packet_id or self.ids.new_packet_id(),
            LinkId(link_id),
            RECEIVED_AT,
            handle,
            output.fragment.session_frame_index,
            route,
            message,
        )

    def _process_all(self, reassembler, handle, route, outputs):
        result = None
        for link_id, output in outputs:
            result = reassembler.process(self._request(handle, route, link_id, output))
        return result

    def test_TC_LINK_010_global_wrap_reordering_duplicate_and_completion(self):
        payload = bytes((index & 0xFF for index in range(35 * 256 + 1 - 4)))
        handle, context = self._tx_context(payload)
        outputs = self._build_all(context, 11, frame_limit=1)
        self.assertEqual(257, len(outputs))
        self.assertEqual(255, outputs[255][1].fragment.global_fragment_number)
        self.assertEqual(0, outputs[256][1].fragment.global_fragment_number)

        reassembler = DataSessionReassembler()
        self.assertTrue(self._open(reassembler, handle).is_ok)
        second_request = self._request(handle, DIRECT_ROUTE, *outputs[1])
        first_second = reassembler.process(second_request)
        self.assertEqual(FragmentProcessStatus.INCOMPLETE, first_second.value.status)
        duplicate = reassembler.process(
            self._request(handle, DIRECT_ROUTE, *outputs[1])
        )
        self.assertEqual(FragmentProcessStatus.DUPLICATE_IGNORED, duplicate.value.status)

        first = reassembler.process(self._request(handle, DIRECT_ROUTE, *outputs[0]))
        self.assertEqual(FragmentProcessStatus.INCOMPLETE, first.value.status)
        result = self._process_all(reassembler, handle, DIRECT_ROUTE, outputs[2:])
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(FragmentProcessStatus.COMPLETE, result.value.status)
        self.assertEqual(SequentialId(0x1234), result.value.sequential_id)
        self.assertEqual(payload, result.value.presentation_data)
        self.assertEqual(257, len(result.value.packet_ids))
        self.assertEqual(0, reassembler.active_count)

    def test_TC_LINK_010_conflict_and_global_number_error_discard_context(self):
        handle, context = self._tx_context(bytes(66))
        outputs = self._build_all(context, 11, frame_limit=2)
        reassembler = DataSessionReassembler()
        self._open(reassembler, handle)

        request = self._request(handle, DIRECT_ROUTE, *outputs[0])
        accepted = reassembler.process(request)
        self.assertEqual(FragmentProcessStatus.INCOMPLETE, accepted.value.status)
        changed_body = dataclasses.replace(
            request.message.body,
            fragment_payload=request.message.body.fragment_payload[:-1] + b"\xff",
        )
        changed_message = dataclasses.replace(request.message, body=changed_body)
        conflict = reassembler.process(
            self._request(handle, DIRECT_ROUTE, *outputs[0], message=changed_message)
        )
        self.assertEqual(ErrorCode.LINK_FRAGMENT_CONFLICT, conflict.error.code)
        self.assertEqual(0, reassembler.active_count)

        handle2, context2 = self._tx_context(b"payload")
        output = self._build_all(context2, 11, frame_limit=1)[0]
        self._open(reassembler, handle2)
        parsed = parse_packet(LinkId(11), output[1].packet.packet, self.messages, self.profiles)
        bad_body = dataclasses.replace(parsed.value.messages[0].body, global_fragment_number=1)
        bad_message = dataclasses.replace(parsed.value.messages[0], body=bad_body)
        bad_number = reassembler.process(
            self._request(handle2, DIRECT_ROUTE, *output, message=bad_message)
        )
        self.assertEqual(ErrorCode.LINK_FRAGMENT_NUMBER_INVALID, bad_number.error.code)
        self.assertEqual(0, reassembler.active_count)

        unknown = reassembler.process(
            self._request(handle2, DIRECT_ROUTE, *output)
        )
        self.assertEqual(ErrorCode.LINK_UNKNOWN_SESSION, unknown.error.code)

    def test_TC_LINK_012_maximum_payload_and_context_capacity(self):
        payload = bytes((index & 0xFF for index in range(65535)))
        handle, context = self._tx_context(payload, sequential_id=16383)
        outputs = self._build_all(context, 19)
        self.assertEqual(96, len(outputs))
        reassembler = DataSessionReassembler()
        self._open(reassembler, handle)
        completed = self._process_all(reassembler, handle, DIRECT_ROUTE, outputs)
        self.assertEqual(FragmentProcessStatus.COMPLETE, completed.value.status)
        self.assertEqual(SequentialId(16383), completed.value.sequential_id)
        self.assertEqual(payload, completed.value.presentation_data)

        bounded = DataSessionReassembler()
        handles = [self.ids.new_session_handle() for _ in range(65)]
        for item in handles[:64]:
            self.assertTrue(self._open(bounded, item).is_ok)
        exhausted = self._open(bounded, handles[64])
        self.assertEqual(ErrorCode.LINK_REASSEMBLY_CAPACITY_EXHAUSTED, exhausted.error.code)
        self.assertEqual(64, bounded.active_count)
        self._control(bounded, ReassemblyControlKind.ABORT, handles[0])
        self.assertTrue(self._open(bounded, handles[64]).is_ok)
        self.assertEqual(64, bounded.active_count)
        self.assertEqual(0, DataSessionReassembler().active_count)

    def test_TC_LINK_024_complete_close_and_incomplete_controls(self):
        handle, context = self._tx_context(b"complete")
        output = self._build_all(context, 11)[0]
        reassembler = DataSessionReassembler()
        self._open(reassembler, handle)
        completed = reassembler.process(self._request(handle, DIRECT_ROUTE, *output))
        self.assertEqual(FragmentProcessStatus.COMPLETE, completed.value.status)
        closed = self._control(reassembler, ReassemblyControlKind.CLOSE, handle)
        self.assertTrue(closed.is_ok)
        self.assertFalse(closed.value.active)

        handle2, context2 = self._tx_context(bytes(66))
        outputs = self._build_all(context2, 11, frame_limit=2)
        self._open(reassembler, handle2)
        reassembler.process(self._request(handle2, DIRECT_ROUTE, *outputs[0]))
        incomplete_close = self._control(reassembler, ReassemblyControlKind.CLOSE, handle2)
        self.assertEqual(ErrorCode.LINK_REASSEMBLY_INCOMPLETE, incomplete_close.error.code)
        repeated = self._control(reassembler, ReassemblyControlKind.CLOSE, handle2)
        self.assertTrue(repeated.is_ok)

        for kind in (ReassemblyControlKind.ABORT, ReassemblyControlKind.TIMEOUT):
            with self.subTest(kind=kind):
                handle3, context3 = self._tx_context(bytes(66))
                output3 = self._build_all(context3, 11, frame_limit=2)[0]
                self._open(reassembler, handle3)
                reassembler.process(self._request(handle3, DIRECT_ROUTE, *output3))
                discarded = self._control(reassembler, kind, handle3)
                self.assertEqual(1, discarded.value.discarded_fragment_count)
                self.assertGreater(discarded.value.discarded_bytes, 0)
                self.assertEqual(0, self._control(reassembler, kind, handle3).value.discarded_bytes)

    def test_TC_LINK_026_direct_node_and_route_mismatch_are_atomic(self):
        for route, expected_source, expected_destination in (
            (DIRECT_ROUTE, ORIGINAL_SOURCE, ORIGINAL_DESTINATION),
            (NODE_ROUTE, NODE_SOURCE, NODE_DESTINATION),
        ):
            with self.subTest(route=route):
                handle, context = self._tx_context(b"route", route)
                output = self._build_all(context, 11)[0]
                self.assertEqual(expected_source, output[1].body.source_id)
                self.assertEqual(expected_destination, output[1].body.destination)
                reassembler = DataSessionReassembler()
                self._open(reassembler, handle, route)
                completed = reassembler.process(self._request(handle, route, *output))
                self.assertEqual(b"route", completed.value.presentation_data)

        incomplete_route = RouteAddresses(
            OriginalSource(ORIGINAL_SOURCE),
            OriginalDestination(ORIGINAL_DESTINATION),
            NodeSource(NODE_SOURCE),
            None,
        )
        reassembler = DataSessionReassembler()
        incomplete_handle = self.ids.new_session_handle()
        rejected_open = self._open(reassembler, incomplete_handle, incomplete_route)
        self.assertEqual(ErrorCode.LINK_DATA_ROUTE_INCOMPLETE, rejected_open.error.code)

        handle, direct_context = self._tx_context(b"direct")
        direct_output = self._build_all(direct_context, 11)[0]
        _node_handle, node_context = self._tx_context(b"direct", NODE_ROUTE)
        node_output = self._build_all(node_context, 11)[0]
        self._open(reassembler, handle, DIRECT_ROUTE)
        mismatch = reassembler.process(
            self._request(handle, DIRECT_ROUTE, *node_output)
        )
        self.assertEqual(ErrorCode.LINK_DATA_ROUTE_MISMATCH, mismatch.error.code)
        self.assertEqual(1, reassembler.active_count)
        correct = reassembler.process(
            self._request(handle, DIRECT_ROUTE, *direct_output)
        )
        self.assertEqual(FragmentProcessStatus.COMPLETE, correct.value.status)

    def test_TC_LINK_031_empty_early_continue_and_excess_boundaries(self):
        empty_type74 = bytes.fromhex("4a000f010203040005060708010001")
        empty = self.messages.lookup(74).value.codec.decode(empty_type74)
        self.assertEqual(ErrorCode.LINK_DATA_FRAGMENT_PAYLOAD_EMPTY, empty.error.code)

        segment = encode_unsigned_segment(SequentialId(1), b"abc").value
        cases = (
            (
                DataStartFragmentBody(ORIGINAL_SOURCE, 0, ORIGINAL_DESTINATION, 1, 0, 0, segment[:5]),
                ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
            ),
            (
                DataStartFragmentBody(ORIGINAL_SOURCE, 0, ORIGINAL_DESTINATION, 1, 0, 1, segment),
                ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
            ),
            (
                DataStartFragmentBody(ORIGINAL_SOURCE, 0, ORIGINAL_DESTINATION, 1, 0, 0, segment + b"x"),
                ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
            ),
        )
        for body, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                handle = self.ids.new_session_handle()
                reassembler = DataSessionReassembler()
                self._open(reassembler, handle)
                message = DecodedMessage(74, 15 + len(body.fragment_payload), body=body)
                request = LinkFragmentProcessRequest(
                    self.ids.new_packet_id(),
                    LinkId(11),
                    RECEIVED_AT,
                    handle,
                    0,
                    DIRECT_ROUTE,
                    message,
                )
                result = reassembler.process(request)
                self.assertEqual(expected_code, result.error.code)
                self.assertEqual(0, reassembler.active_count)

        handle = self.ids.new_session_handle()
        reassembler = DataSessionReassembler()
        self._open(reassembler, handle)
        start = DataStartFragmentBody(
            ORIGINAL_SOURCE, 0, ORIGINAL_DESTINATION, 3, 0, 1, segment[:1]
        )
        middle = DataContinuationFragmentBody(
            ORIGINAL_SOURCE, 0, ORIGINAL_DESTINATION, 3, 2, 1, segment[1:]
        )
        for body, message_type in ((start, 74), (middle, 75)):
            message = DecodedMessage(message_type, 15 + len(body.fragment_payload), body=body)
            request = LinkFragmentProcessRequest(
                self.ids.new_packet_id(),
                LinkId(11),
                RECEIVED_AT,
                handle,
                0,
                DIRECT_ROUTE,
                message,
            )
            result = reassembler.process(request)
        self.assertEqual(ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY, result.error.code)

    def test_TC_LINK_016_018_round_trip_and_delivery_envelopes(self):
        payload = b"fragmentation-to-reassembly"
        handle, context = self._tx_context(payload, NODE_ROUTE)
        outputs = self._build_all(context, 11, frame_limit=2)
        reassembler = DataSessionReassembler()
        self._open(reassembler, handle, NODE_ROUTE)
        completed = self._process_all(reassembler, handle, NODE_ROUTE, outputs)
        self.assertEqual(payload, completed.value.presentation_data)
        data_action = MacAction(
            MacActionKind.DELIVER_PRESENTATION_DATA,
            packet_id=completed.value.packet_ids[-1],
            session_handle=handle,
            presentation_data=completed.value.presentation_data,
            route=completed.value.route,
        )
        self.assertEqual(payload, data_action.presentation_data)

        short_packet = build_short_data_packet(
            LinkId(11),
            DIRECT_ROUTE,
            False,
            None,
            SequentialId(9),
            b"short",
            self.messages,
            self.profiles,
        ).value
        parsed = parse_packet(LinkId(11), short_packet.packet, self.messages, self.profiles)
        short = decode_short_data_message(parsed.value.messages[0]).value
        short_action = MacAction(
            MacActionKind.DELIVER_PRESENTATION_DATA,
            packet_id=self.ids.new_packet_id(),
            presentation_data=short.presentation_data,
            route=short.route,
        )
        self.assertEqual(b"short", short_action.presentation_data)

        raw_data = parse_packet(
            LinkId(11), outputs[0][1].packet.packet, self.messages, self.profiles
        ).value.messages[0]
        self.assertIsNotNone(raw_data.fragment)
        self.assertIsNone(raw_data.fragment.count)
        self.assertIsNone(raw_data.route)
        self.assertIsNone(raw_data.presentation_data)


if __name__ == "__main__":
    unittest.main()
