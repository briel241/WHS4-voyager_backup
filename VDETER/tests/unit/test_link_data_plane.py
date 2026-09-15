"""TC-LINK-030: public Message encode/build path ownership."""

from __future__ import annotations

import unittest

from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkPacketEncodeRequest,
)
from vdeter.common.contracts.mac import TransmissionPlan, TransmissionProcedure
from vdeter.common.contracts.message import (
    AcknowledgeNackBody,
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
    MediaAccessControlBody,
    MessageEncodeRequest,
    PaddingBody,
    ResourceAllocationBody,
    ResourceRequestBody,
    ShortDataAckBody,
    ShortDataNoAckBody,
    TbbContinuationFragmentBody,
    TbbEndFragmentBody,
    TbbStartFragmentBody,
)
from vdeter.common.errors import Correlation, ErrorCode
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PhysicalChannel,
    StationId,
)
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.link.data_plane import LinkDataPlane
from vdeter.link.interface import LinkPort
from vdeter.link.messages.builtins import build_message_registry
from vdeter.profiles.link_phy import build_link_profile_registry
from vdeter.profiles.models import RegistryScope


SOURCE = StationId(0x01020304)
DESTINATION = Addressed(StationId(0x05060708))
ROUTE = RouteAddresses(
    OriginalSource(SOURCE), OriginalDestination(DESTINATION)
)
UTC = TimePoint(10, 0, ClockDomain.UTC, "test.utc")


def _bodies():
    return {
        0: MediaAccessControlBody(SOURCE, 0, 1, 2, 20),
        4: ResourceAllocationBody(
            SOURCE, DESTINATION, 1, 2, LinkId(11), 1, 0, 3
        ),
        13: AcknowledgeNackBody(
            SOURCE, DESTINATION, 0, (1, 2, 3), 4, 0, 0
        ),
        20: TbbStartFragmentBody(SOURCE, 1, 2, 3, 2, b"a"),
        21: TbbContinuationFragmentBody(SOURCE, 1, 3, 1, b"b"),
        22: TbbEndFragmentBody(SOURCE, 1, 3, 1, b"c"),
        74: DataStartFragmentBody(SOURCE, 0, DESTINATION, 3, 0, 1, b"d"),
        75: DataContinuationFragmentBody(
            SOURCE, 0, DESTINATION, 3, 2, 1, b"e"
        ),
        76: DataEndFragmentBody(SOURCE, 0, DESTINATION, 3, 2, 0, b"f"),
        81: PaddingBody(),
        90: ResourceRequestBody(
            SOURCE, StationId(0x11121314), Broadcast(), DESTINATION, 0, 2
        ),
        92: ShortDataAckBody(SOURCE, 0, DESTINATION, 0, b"g"),
        93: ShortDataNoAckBody(SOURCE, 0, Broadcast(), b"h"),
    }


class LinkDataPlaneTest(unittest.TestCase):
    def setUp(self):
        self.ids = DeterministicIdSource(30030)
        messages = build_message_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        profiles = build_link_profile_registry(
            RegistryScope.OPERATIONAL, "stage1-link-0.5.3"
        ).value
        messages.freeze()
        profiles.freeze()
        self.link = LinkDataPlane(self.ids, messages, profiles)

    @staticmethod
    def _request(*bodies):
        correlation = Correlation()
        return LinkPacketEncodeRequest(
            LinkId(11),
            tuple(MessageEncodeRequest(body, correlation) for body in bodies),
            correlation,
        )

    def test_TC_LINK_030_general_control_types_encode_and_decode(self):
        self.assertIsInstance(self.link, LinkPort)
        bodies = _bodies()
        for message_type in (0, 4, 13, 90):
            with self.subTest(message_type=message_type):
                encoded = self.link.encode_packet(
                    self._request(bodies[message_type])
                )
                self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
                self.assertEqual((message_type,), encoded.value.message_types)
                decoded = self.link.decode_packet(
                    LinkDecodeRequest(
                        encoded.value.packet_id,
                        encoded.value.link_id,
                        UTC,
                        encoded.value.packet,
                    )
                )
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(bodies[message_type], decoded.value.messages[0].body)

    def test_TC_LINK_030_owned_types_reject_general_encode_path_atomically(self):
        bodies = _bodies()
        issued_before = self.ids.issued_count()
        for message_type in (20, 21, 22, 74, 75, 76, 81, 92, 93):
            with self.subTest(message_type=message_type):
                rejected = self.link.encode_packet(
                    self._request(bodies[message_type])
                )
                self.assertEqual(
                    ErrorCode.LINK_MESSAGE_BUILD_PATH_REQUIRED,
                    rejected.error.code,
                )
                self.assertEqual(message_type, rejected.error.details.get("message_type"))
        self.assertEqual(issued_before, self.ids.issued_count())

        mixed = self.link.encode_packet(
            self._request(bodies[0], bodies[92], bodies[13])
        )
        self.assertEqual(
            ErrorCode.LINK_MESSAGE_BUILD_PATH_REQUIRED, mixed.error.code
        )
        self.assertEqual(1, mixed.error.details.get("message_index"))
        self.assertEqual(issued_before, self.ids.issued_count())

    def test_public_short_data_build_and_decode_enriches_segment(self):
        payload = b"public-short-data"
        transfer_id = self.ids.new_transfer_id()
        analyzed = self.link.analyze_transfer(
            LinkAnalyzeRequest(transfer_id, ROUTE, False, payload)
        )
        self.assertTrue(analyzed.is_ok, getattr(analyzed, "error", None))
        plan = TransmissionPlan(
            transfer_id,
            LinkId(11),
            PhysicalChannel("test-short"),
            UTC,
            TransmissionProcedure.SHORT_DATA,
            ROUTE,
        )
        built = self.link.build_packet(LinkBuildRequest(plan, payload))
        self.assertTrue(built.is_ok, getattr(built, "error", None))
        decoded = self.link.decode_packet(
            LinkDecodeRequest(
                built.value.packet_id,
                built.value.link_id,
                UTC,
                built.value.packet,
            )
        )
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(payload, decoded.value.messages[0].presentation_data)
        self.assertEqual(
            built.value.sequential_id, decoded.value.messages[0].sequential_id
        )
        self.assertEqual(ROUTE, decoded.value.messages[0].route)


if __name__ == "__main__":
    unittest.main()
