"""TC-LINK-009: Data Session frame 구성과 Link ID별 fragmentation 경계."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import (
    Addressed,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.message import (
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import DeterministicIdSource, LinkId, SequentialId, StationId
from vdeter.link.data_session.fragmentation import create_data_session_tx_context
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.packet.parser import parse_packet
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


class DataFragmentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = build_message_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        cls.profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        cls.messages.freeze()
        cls.profiles.freeze()
        cls.ids = DeterministicIdSource(9009)

    def _context(self, payload: bytes, route: RouteAddresses = DIRECT_ROUTE):
        result = create_data_session_tx_context(
            self.ids.new_transfer_id(),
            self.ids.new_session_handle(),
            route,
            SequentialId(0x1234),
            payload,
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        return result.value

    def _build(self, context, link_id: int, frame: int, count: int, position: int):
        return context.build_fragment(
            LinkId(link_id),
            frame,
            count,
            position,
            self.messages,
            self.profiles,
        )

    def test_TC_LINK_009_one_two_and_fourteen_fragment_frames(self):
        capacity = 35
        for count in (1, 2, 14):
            with self.subTest(count=count):
                context = self._context(bytes(capacity * count - 4))
                outputs = [self._build(context, 11, 0, count, position).value for position in range(1, count + 1)]
                self.assertTrue(context.complete)
                self.assertIsInstance(outputs[0].body, DataStartFragmentBody)
                self.assertEqual(1 if count > 1 else 0, outputs[0].body.continue_resource_usage)
                if count > 1:
                    self.assertIsInstance(outputs[-1].body, DataEndFragmentBody)
                    self.assertEqual(0, outputs[-1].body.continue_resource_usage)
                for index, output in enumerate(outputs):
                    expected_type = 74 if index == 0 else 76 if index == count - 1 else 75
                    self.assertEqual((expected_type,), output.packet.message_types)
                    parsed = parse_packet(LinkId(11), output.packet.packet, self.messages, self.profiles)
                    self.assertEqual(output.body, parsed.value.messages[0].body)

    def test_TC_LINK_009_multiple_frames_continue_and_offsets(self):
        context = self._context(bytes(35 * 3 + 5 - 4))
        outputs = [
            self._build(context, 11, 0, 2, 1).value,
            self._build(context, 11, 0, 2, 2).value,
            self._build(context, 11, 1, 2, 1).value,
            self._build(context, 11, 1, 2, 2).value,
        ]
        self.assertEqual((0, 1, 2, 3), tuple(item.fragment.global_fragment_number for item in outputs))
        self.assertEqual((0, 35, 70, 105), tuple(item.segment_offset for item in outputs))
        self.assertEqual((35, 35, 35, 5), tuple(item.segment_length for item in outputs))
        self.assertEqual(1, outputs[1].body.continue_resource_usage)
        self.assertEqual(1, outputs[2].body.continue_resource_usage)
        self.assertEqual(0, outputs[3].body.continue_resource_usage)
        self.assertTrue(outputs[3].segment_complete)

    def test_TC_LINK_009_link_id_payload_capacity_boundaries(self):
        capacities = {11: 35, 17: 215, 19: 683}
        for link_id, capacity in capacities.items():
            with self.subTest(link_id=link_id):
                context = self._context(bytes(capacity + 1 - 4))
                first = self._build(context, link_id, 0, 2, 1).value
                second = self._build(context, link_id, 0, 2, 2).value
                self.assertEqual(capacity, first.segment_length)
                self.assertEqual(1, second.segment_length)
                self.assertEqual(1, first.body.continue_resource_usage)
                self.assertEqual(0, second.body.continue_resource_usage)

    def test_TC_LINK_009_retry_route_and_plan_conflicts(self):
        context = self._context(bytes(66), NODE_ROUTE)
        first = self._build(context, 11, 0, 2, 1)
        retry = self._build(context, 11, 0, 2, 1)
        self.assertEqual(first.value, retry.value)
        self.assertEqual(NODE_SOURCE, first.value.body.source_id)
        self.assertEqual(NODE_DESTINATION, first.value.body.destination)

        changed_count = self._build(context, 11, 0, 3, 2)
        self.assertEqual(ErrorCode.LINK_FRAGMENT_CONFLICT, changed_count.error.code)
        skipped_position = self._build(context, 11, 0, 2, 2)
        self.assertTrue(skipped_position.is_ok)
        changed_profile = self._build(context, 17, 1, 1, 1)
        self.assertEqual(ErrorCode.LINK_TRANSFER_PROFILE_SWITCH_UNSAFE, changed_profile.error.code)

        incomplete_route = RouteAddresses(
            OriginalSource(ORIGINAL_SOURCE),
            OriginalDestination(ORIGINAL_DESTINATION),
            NodeSource(NODE_SOURCE),
            None,
        )
        rejected = create_data_session_tx_context(
            self.ids.new_transfer_id(),
            self.ids.new_session_handle(),
            incomplete_route,
            SequentialId(1),
            b"payload",
        )
        self.assertEqual(ErrorCode.LINK_DATA_ROUTE_INCOMPLETE, rejected.error.code)

    def test_TC_LINK_009_announced_count_cannot_exceed_remaining_fragments(self):
        context = self._context(bytes(31))
        rejected = self._build(context, 11, 0, 2, 1)
        self.assertEqual(ErrorCode.LINK_FRAGMENT_COUNT_INVALID, rejected.error.code)


if __name__ == "__main__":
    unittest.main()
