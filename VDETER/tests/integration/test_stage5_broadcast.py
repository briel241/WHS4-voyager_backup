"""TC-STAGE5-015: broadcast/addressed destination 격리.

요구사항: `STAGE5-REQ-15` (`stage5_service_integration_baseline.md` §4.3, §8.3)
"""

from __future__ import annotations

import unittest

from tests.support.stage4 import Type0OverrideProfile
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    received_payloads,
    start_stage5_group,
)
from vdeter.common.addressing import Addressed, Broadcast
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat

OPAQUE = PayloadFormat("test.stage0.opaque")
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)


class Stage5BroadcastIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = build_stage5_software_group(type0_override=FAST_TYPE0)
        start_stage5_group(cls.clock, cls.group)
        cls.by_station = cls.group.by_station()
        cls.roc = cls.by_station[ROC_STATION_ID.value]
        cls.ship_a = cls.by_station[MASS_A_STATION_ID.value]
        cls.ship_b = cls.by_station[MASS_B_STATION_ID.value]

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    def test_TC_STAGE5_015_roc_type93_broadcast_reaches_both_ships_once(self):
        payload = b"\xb0\x00\xff\xc3\x28"
        accepted = self.roc.submit(
            SubmitTransferRequest(
                destination=Broadcast(),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=False,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        for _ in range(280):
            drive_stage5_group(self.clock, self.group, 1)
            state = self.roc.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value.state
            done = state is TransferState.COMPLETED
            if (
                done
                and received_payloads(self.ship_a).count(payload) == 1
                and received_payloads(self.ship_b).count(payload) == 1
            ):
                break
        self.assertIs(TransferState.COMPLETED, state)
        # 두 Ship에 각각 정확히 1회 전달된다. duplicate 억제 확인을 위해 더 진행한다.
        drive_stage5_group(self.clock, self.group, 30)
        self.assertEqual(1, received_payloads(self.ship_a).count(payload))
        self.assertEqual(1, received_payloads(self.ship_b).count(payload))
        # broadcast destination이 그대로 보존된다.
        for endpoint in (self.ship_a, self.ship_b):
            event = [
                item
                for item in endpoint.dependencies.service_event_sink.events()
                if item.event_type is ServiceEventType.DATA_RECEIVED
                and item.received_data.payload == payload
            ][-1]
            self.assertTrue(event.received_data.original_destination.is_broadcast)
            self.assertEqual(ROC_STATION_ID, event.received_data.original_source)

    def test_TC_STAGE5_015_mass_broadcast_is_rejected_without_wire_work(self):
        for ship in (self.ship_a, self.ship_b):
            with self.subTest(
                ship=ship.dependencies.configuration.endpoint.endpoint_id
            ):
                nonterminal_before = ship._nonterminal_count_locked()
                result = ship.submit(
                    SubmitTransferRequest(
                        destination=Broadcast(),
                        payload_format=OPAQUE,
                        payload=b"\x01",
                        ack_required=False,
                    )
                )
                self.assertTrue(result.is_err)
                self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)
                self.assertIsNone(result.error.transfer_id)
                self.assertEqual(nonterminal_before, ship._nonterminal_count_locked())

    def test_TC_STAGE5_015_addressed_data_is_not_delivered_to_third_station(self):
        payload = b"\xad\x01\x02\x03"
        roc_before = len(received_payloads(self.roc))
        b_before = len(received_payloads(self.ship_b))
        accepted = self.ship_a.submit(
            SubmitTransferRequest(
                destination=Addressed(MASS_B_STATION_ID),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=False,
            )
        )
        self.assertTrue(accepted.is_ok)
        for _ in range(280):
            drive_stage5_group(self.clock, self.group, 1)
            if received_payloads(self.ship_b)[b_before:].count(payload) == 1:
                break
        self.assertEqual([payload], received_payloads(self.ship_b)[b_before:])
        # 같은 UPPER 채널을 수신하는 제3 endpoint에는 전달되지 않는다.
        self.assertEqual(roc_before, len(received_payloads(self.roc)))


if __name__ == "__main__":
    unittest.main()
