"""TC-MAC-014: addressed MASS-A -> ROC -> MASS-B Data Session relay."""

from __future__ import annotations

import unittest

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
    build_stage4_software_group,
    drive_stage4_group,
    start_stage4_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.identifiers import PayloadFormat


PAYLOAD_FORMAT = PayloadFormat("test.stage0.opaque")


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage4-test:lower-bound"


class Stage4ThreeEndpointRelayTest(unittest.TestCase):
    def test_TC_MAC_014_relay_preserves_original_route_and_delivers_once(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        by_station = {
            endpoint.dependencies.configuration.endpoint.station_id.value: endpoint
            for endpoint in endpoints
        }
        mass_a = by_station[MASS_A_STATION_ID.value]
        roc = by_station[ROC_STATION_ID.value]
        mass_b = by_station[MASS_B_STATION_ID.value]
        payload = bytes(range(256)) * 3

        accepted = mass_a.submit(SubmitTransferRequest(
            destination=Addressed(MASS_B_STATION_ID),
            payload_format=PAYLOAD_FORMAT,
            payload=payload,
            ack_required=True,
        ))
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        mass_a.stage4.pump()
        for _ in range(240):
            drive_stage4_group(clock, endpoints, 1)
            received = [
                event.received_data.payload
                for event in mass_b.dependencies.service_event_sink.events()
                if event.received_data is not None
            ]
            if received == [payload]:
                break

        source_status = mass_a.status(
            TransferStatusRequest(accepted.value.transfer_id)
        )
        self.assertIs(
            TransferState.COMPLETED,
            source_status.value.state,
            (
                source_status.value,
                mass_a.stage4.mac.state.transfer(accepted.value.transfer_id),
                roc.stage4.mac.state.relay_records,
                roc.stage4.mac.state.rx_sessions,
                roc.stage4.mac.state.request_chains,
            ),
        )
        received = [
            event.received_data.payload
            for event in mass_b.dependencies.service_event_sink.events()
            if event.received_data is not None
        ]
        self.assertEqual([payload], received)
        self.assertFalse(any(
            event.received_data is not None
            for event in roc.dependencies.service_event_sink.events()
        ))
        self.assertEqual(1, len(roc.stage4.mac.state.relay_records))
        relay = roc.stage4.mac.state.relay_records[0]
        self.assertTrue(relay.admitted)
        self.assertTrue(relay.first_hop_ack_completed)
        self.assertTrue(relay.second_hop_started)
        self.assertEqual(MASS_A_STATION_ID, relay.second_hop_route.original_source.station_id)
        self.assertEqual(
            MASS_B_STATION_ID,
            relay.second_hop_route.original_destination.address.station_id,
        )
        self.assertEqual(ROC_STATION_ID, relay.second_hop_route.node_source.station_id)
        self.assertEqual(
            MASS_B_STATION_ID,
            relay.second_hop_route.node_destination.address.station_id,
        )

    def test_TC_MAC_014_lost_first_hop_ack_replays_without_duplicate_relay(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=30, short_data_limit=4, system_status=0
            ),
            with_test_payload_format=True,
            random_source_factory=lambda _index: _LowerBoundRandom(),
        )
        start_stage4_group(clock, endpoints)
        by_station = {
            endpoint.dependencies.configuration.endpoint.station_id.value: endpoint
            for endpoint in endpoints
        }
        mass_a = by_station[MASS_A_STATION_ID.value]
        roc = by_station[ROC_STATION_ID.value]
        mass_b = by_station[MASS_B_STATION_ID.value]
        medium = mass_a.radio._medium
        payload = bytes(range(128)) * 6
        accepted = mass_a.submit(
            SubmitTransferRequest(
                destination=Addressed(MASS_B_STATION_ID),
                payload_format=PAYLOAD_FORMAT,
                payload=payload,
                ack_required=True,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        mass_a.stage4.pump()
        for _ in range(160):
            drive_stage4_group(clock, endpoints, 1)
            source = mass_a.stage4.mac.state.transfer(accepted.value.transfer_id)
            if source is not None and source.logical_channel_tx is not None:
                break
        self.assertIsNotNone(source.logical_channel_tx, source)
        with medium._lock:
            blocked_link = next(
                link
                for link in medium._links
                if link.source_endpoint_id == "stage4-roc"
                and link.target_endpoint_id == "stage4-mass-a"
            )
            medium._links.remove(blocked_link)

        for _ in range(420):
            drive_stage4_group(clock, endpoints, 1)
            source = mass_a.stage4.mac.state.transfer(accepted.value.transfer_id)
            if source is not None and (
                source.retransmission_number >= 1 or source.absent_ack_frames >= 1
            ):
                break
        self.assertTrue(
            source.retransmission_number >= 1 or source.absent_ack_frames >= 1,
            source,
        )
        self.assertLessEqual(len(roc.stage4.mac.state.relay_records), 1)

        with medium._lock:
            medium._links.append(blocked_link)
        for _ in range(420):
            drive_stage4_group(clock, endpoints, 1)
            status = mass_a.status(TransferStatusRequest(accepted.value.transfer_id))
            received = [
                event.received_data.payload
                for event in mass_b.dependencies.service_event_sink.events()
                if event.received_data is not None
            ]
            if status.value.state is TransferState.COMPLETED and received == [payload]:
                break
        self.assertIs(TransferState.COMPLETED, status.value.state)
        self.assertEqual([payload], received)
        self.assertEqual(1, len(roc.stage4.mac.state.relay_records))


if __name__ == "__main__":
    unittest.main()
