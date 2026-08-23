"""TC-STAGE5-001: 공개 API·DTO·Port contract와 정상 조립.

요구사항: `STAGE5-REQ-01`, `STAGE5-REQ-19`
(`stage5_service_integration_baseline.md` §2.2, §4.4, §5.1, `DR-STAGE5-018`)
"""

from __future__ import annotations

import unittest
from dataclasses import fields

from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    start_stage5_group,
    received_payloads,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    EndpointHealth,
    ReceivedApplicationData,
    SubmitTransferRequest,
    TransferStatusRequest,
    TransferState,
)
from vdeter.common.events import ServiceEvent, ServiceEventType
from vdeter.common.identifiers import PayloadFormat
from vdeter.link.data_plane import LinkDataPlane
from vdeter.link.stub import StubLink
from vdeter.mac.interface import Stage4Mac
from vdeter.phy.transmitter import Link11Phy
from vdeter.presentation.adapter import RegistryPresentationAdapter
from vdeter.presentation.stub import StubPresentationAdapter
from vdeter.radio.software import SoftwareRadioAdapter
from vdeter.runtime.stage5 import Stage5Endpoint
from vdeter.service.interface import VdeService

OPAQUE = PayloadFormat("test.stage0.opaque")


class Stage5PublicContractTest(unittest.TestCase):
    def test_TC_STAGE5_001_endpoint_satisfies_frozen_service_protocol(self):
        clock, group = build_stage5_software_group()
        endpoint = group.endpoints[0]
        self.assertIsInstance(endpoint, VdeService)
        for method in ("start", "stop", "submit", "cancel", "status"):
            self.assertTrue(callable(getattr(endpoint, method)))
        # prototype 이름의 alias facade를 추가하지 않는다.
        for forbidden in ("open", "close", "send", "get_status", "on_message"):
            self.assertFalse(hasattr(endpoint, forbidden), forbidden)

    def test_TC_STAGE5_001_submit_request_has_only_four_business_fields(self):
        names = [field.name for field in fields(SubmitTransferRequest)]
        self.assertEqual(
            ["destination", "payload_format", "payload", "ack_required"], names
        )

    def test_TC_STAGE5_001_public_dtos_do_not_carry_internal_protocol_fields(self):
        for dto in (SubmitTransferRequest, ReceivedApplicationData, ServiceEvent):
            names = {field.name for field in fields(dto)}
            for forbidden in (
                "link_id",
                "message_type",
                "vpfi",
                "session_id",
                "session_handle",
                "sequential_id",
                "fragment",
                "packet",
                "crc",
                "fec",
                "slot",
                "retransmission",
                "radio",
                "burst_id",
            ):
                self.assertFalse(
                    any(forbidden in name for name in names), (dto.__name__, forbidden)
                )

    def test_TC_STAGE5_001_normal_graph_uses_real_layers_only(self):
        clock, group = build_stage5_software_group()
        for endpoint in group.endpoints:
            with self.subTest(endpoint=endpoint.dependencies.configuration.endpoint.endpoint_id):
                self.assertIsInstance(endpoint, Stage5Endpoint)
                self.assertIsInstance(endpoint._presentation, RegistryPresentationAdapter)
                self.assertIsInstance(endpoint._link, LinkDataPlane)
                self.assertIsInstance(endpoint._mac, Stage4Mac)
                self.assertIsInstance(endpoint._phy, Link11Phy)
                self.assertIsInstance(endpoint.radio, SoftwareRadioAdapter)

    def test_TC_STAGE5_001_stub_injection_rejects_group_start_before_rf(self):
        cases = {
            "presentation": lambda endpoint: setattr(
                endpoint,
                "_presentation",
                StubPresentationAdapter(endpoint.dependencies.registries.payload_format),
            ),
            "link": lambda endpoint: setattr(
                endpoint,
                "_link",
                StubLink(
                    endpoint.dependencies.id_source,
                    __import__(
                        "vdeter.link.models", fromlist=["LinkCapabilities"]
                    ).LinkCapabilities(
                        endpoint.dependencies.configuration.endpoint.link.supported_link_ids
                    ),
                ),
            ),
        }
        for layer_name, inject in cases.items():
            with self.subTest(layer=layer_name):
                clock, group = build_stage5_software_group()
                victim = group.endpoints[0]
                inject(victim)
                result = group.start()
                self.assertTrue(result.is_err)
                self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)
                # RF/protocol 작업 전에 거부됐다: 어떤 endpoint도 시작되지 않았다.
                for endpoint in group.endpoints:
                    self.assertIs(EndpointHealth.STOPPED, endpoint.health)
                    self.assertEqual(
                        (), endpoint.dependencies.service_event_sink.events()
                    )

    def test_TC_STAGE5_001_fake_radio_rejects_group_build(self):
        from vdeter.radio.fake import FakeRadioAdapter
        clock, group = build_stage5_software_group()
        victim = group.endpoints[0]
        victim._radio = FakeRadioAdapter(
            victim.dependencies.time_source,
            victim.dependencies.id_source,
            victim.dependencies.internal_event_sink,
        )
        result = group.start()
        self.assertTrue(result.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)

    def test_TC_STAGE5_001_client_needs_no_internal_identifiers_end_to_end(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            payload = b"\x00\xff\x80"
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=True,
                )
            )
            self.assertTrue(accepted.is_ok)
            self.assertIs(TransferState.QUEUED, accepted.value.state)
            for _ in range(150):
                drive_stage5_group(clock, group, 1)
                status = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
                if status.value.state is TransferState.COMPLETED:
                    break
            self.assertEqual([payload], received_payloads(ship_b))
            received_events = [
                event
                for event in ship_b.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.DATA_RECEIVED
            ]
            self.assertEqual(1, len(received_events))
            event = received_events[0]
            # DATA_RECEIVED에는 wire/internal correlation field가 없다.
            self.assertIsNone(event.transfer_id)
            self.assertIsNone(event.transfer_state)
            self.assertIsNone(event.error)
            self.assertTrue(event.details.is_empty())
            data = event.received_data
            self.assertEqual(MASS_A_STATION_ID, data.original_source)
            self.assertEqual(MASS_B_STATION_ID, data.original_destination.station_id)
            self.assertEqual(OPAQUE, data.payload_format)
            self.assertEqual(payload, data.payload)
            self.assertEqual(event.occurred_at, data.received_at)
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
