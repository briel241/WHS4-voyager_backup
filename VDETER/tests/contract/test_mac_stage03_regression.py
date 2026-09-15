"""TC-MAC-018: frozen Stage 0-3 surface and Link 11 runtime compatibility."""

from __future__ import annotations

import unittest

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    ROC_STATION_ID,
    build_stage4_software_group,
    drive_stage4_group,
    start_stage4_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import SubmitTransferRequest, TransferState, TransferStatusRequest
from vdeter.common.events import InternalEventType
from vdeter.common.identifiers import PayloadFormat


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage4-test:lower-bound"


class Stage03MacRegressionTest(unittest.TestCase):
    def test_TC_MAC_018_frozen_service_dto_and_link11_phy_radio_path_remain_compatible(self):
        self.assertEqual(
            ("destination", "payload_format", "payload", "ack_required"),
            SubmitTransferRequest.__slots__,
        )
        clock, endpoints = build_stage4_software_group(
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        mass = next(
            endpoint for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        accepted = mass.submit(SubmitTransferRequest(
            destination=Addressed(ROC_STATION_ID),
            payload_format=PayloadFormat("test.stage0.opaque"),
            payload=b"stage03-regression",
            ack_required=False,
        ))
        self.assertTrue(accepted.is_ok)
        mass.stage4.pump()
        drive_stage4_group(clock, endpoints, 50)
        self.assertIs(
            TransferState.COMPLETED,
            mass.status(TransferStatusRequest(accepted.value.transfer_id)).value.state,
        )
        events = mass.dependencies.internal_event_sink.events()
        packets = [event.payload for event in events if event.event_type is InternalEventType.PACKET_BUILT]
        self.assertTrue(any(packet.link_id.value == 11 for packet in packets))
        self.assertTrue(any(event.event_type is InternalEventType.BURST_COMPLETED for event in events))


if __name__ == "__main__":
    unittest.main()
