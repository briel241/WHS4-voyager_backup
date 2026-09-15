"""TC-MAC-017: three-endpoint Stage 4 vertical Software Radio path."""

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


class Stage4VerticalPathTest(unittest.TestCase):
    def test_TC_MAC_017_bootstrap_to_service_delivery_uses_link11_end_to_end(self):
        clock, endpoints = build_stage4_software_group(
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        self.assertEqual(3, len(endpoints))
        self.assertEqual(1, sum(
            endpoint.stage4.mac.coordinator.is_control for endpoint in endpoints
        ))
        tbb_fingerprints = {
            endpoint.stage4.mac.state.active_tbb.fingerprint for endpoint in endpoints
        }
        deployment_fingerprints = {
            endpoint.stage4.mac.coordinator.profile.deployment_fingerprint
            for endpoint in endpoints
        }
        self.assertEqual((1, 1), (len(tbb_fingerprints), len(deployment_fingerprints)))

        by_station = {
            endpoint.dependencies.configuration.endpoint.station_id.value: endpoint
            for endpoint in endpoints
        }
        mass = by_station[MASS_A_STATION_ID.value]
        roc = by_station[ROC_STATION_ID.value]
        payload = b"stage4 vertical path"
        accepted = mass.submit(SubmitTransferRequest(
            destination=Addressed(ROC_STATION_ID),
            payload_format=PayloadFormat("test.stage0.opaque"),
            payload=payload,
            ack_required=False,
        ))
        self.assertTrue(accepted.is_ok)
        mass.stage4.pump()
        drive_stage4_group(clock, endpoints, 60)
        self.assertIs(
            TransferState.COMPLETED,
            mass.status(TransferStatusRequest(accepted.value.transfer_id)).value.state,
        )
        self.assertIn(payload, [
            event.received_data.payload
            for event in roc.dependencies.service_event_sink.events()
            if event.received_data is not None
        ])
        built = [
            event.payload
            for event in mass.dependencies.internal_event_sink.events()
            if event.event_type is InternalEventType.PACKET_BUILT
        ]
        self.assertTrue(built)
        self.assertTrue(all(packet.link_id.value == 11 for packet in built))
        event_types = {
            event.event_type for event in mass.dependencies.internal_event_sink.events()
        }
        self.assertTrue({
            InternalEventType.TRANSMISSION_PLANNED,
            InternalEventType.PACKET_BUILT,
            InternalEventType.BURST_SCHEDULED,
            InternalEventType.BURST_COMPLETED,
        }.issubset(event_types))


if __name__ == "__main__":
    unittest.main()
