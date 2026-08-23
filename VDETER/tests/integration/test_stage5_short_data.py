"""TC-STAGE5-013: one-hop Short Data 전체 방향.

요구사항: `STAGE5-REQ-13`, `STAGE5-REQ-19`
(`stage5_service_integration_baseline.md` §4.3, §8.2, §11.2, `DR-STAGE5-027`)
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

#: 접근 slot 대기를 짧게 만드는 시험용 Type 0 override (Stage 4 시험과 동일).
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat

OPAQUE = PayloadFormat("test.stage0.opaque")

#: `0x00`, `0xff`, 비UTF-8 byte를 포함한 대표 payload (encoded 33 bytes 이하).
PAYLOADS = {
    "type92": b"\x00\xff\xc3\x28" + bytes(range(20)),
    "type93": b"\xff\x00\xed\xa0\x80" + bytes(range(24, 48)),
}


class Stage5ShortDataMatrixTest(unittest.TestCase):
    """지원 방향 matrix. Ship A/B를 바꾼 역방향도 같은 parameterized case로 수행한다."""

    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = build_stage5_software_group(type0_override=FAST_TYPE0)
        start_stage5_group(cls.clock, cls.group)
        cls.by_station = cls.group.by_station()

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    def _run_direction(self, source_station, destination_station, *, ack_required):
        source = self.by_station[source_station.value]
        destination = self.by_station[destination_station.value]
        others = [
            endpoint
            for endpoint in self.group.endpoints
            if endpoint not in (source, destination)
        ]
        marker = bytes(
            (source_station.value % 251, destination_station.value % 251, ack_required)
        )
        payload = marker + PAYLOADS["type92" if ack_required else "type93"][:24]
        before = len(received_payloads(destination))
        others_before = {id(item): len(received_payloads(item)) for item in others}
        accepted = source.submit(
            SubmitTransferRequest(
                destination=Addressed(destination_station),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=ack_required,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        for _ in range(280):
            drive_stage5_group(self.clock, self.group, 1)
            state = source.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value.state
            if state is TransferState.COMPLETED:
                break
        self.assertIs(TransferState.COMPLETED, state)
        received = received_payloads(destination)[before:]
        # 목적지에만 정확히 1회, 길이와 모든 byte가 같게 전달된다.
        self.assertEqual([payload], received)
        for item in others:
            self.assertEqual(
                others_before[id(item)], len(received_payloads(item)),
                "addressed data가 목적지 외 endpoint에 전달되었습니다",
            )
        # source/destination 정확성: one-hop은 Original=Node이며 공개 event는
        # original 주소만 노출한다.
        event = [
            item
            for item in destination.dependencies.service_event_sink.events()
            if item.event_type is ServiceEventType.DATA_RECEIVED
            and item.received_data.payload == payload
        ][-1]
        self.assertEqual(source_station, event.received_data.original_source)
        self.assertEqual(
            destination_station, event.received_data.original_destination.station_id
        )
        # ACK 의미 구분: Type 92 완료는 목적지 admission 뒤 matching ACK 수신이고
        # Type 93 완료는 로컬 송신 완료다. 두 경우 모두 상태 chain은 동결 순서를 따른다.
        states = [
            item.transfer_state
            for item in source.dependencies.service_event_sink.events()
            if item.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
            and item.transfer_id == accepted.value.transfer_id
        ]
        if ack_required:
            self.assertEqual(
                [
                    TransferState.QUEUED,
                    TransferState.TRANSMITTING,
                    TransferState.WAITING_ACK,
                    TransferState.COMPLETED,
                ],
                states,
            )
        else:
            self.assertEqual(
                [
                    TransferState.QUEUED,
                    TransferState.TRANSMITTING,
                    TransferState.COMPLETED,
                ],
                states,
            )

    def test_TC_STAGE5_013_ship_a_to_ship_b_type92_and_93(self):
        self._run_direction(MASS_A_STATION_ID, MASS_B_STATION_ID, ack_required=True)
        self._run_direction(MASS_A_STATION_ID, MASS_B_STATION_ID, ack_required=False)

    def test_TC_STAGE5_013_ship_b_to_ship_a_reverse(self):
        self._run_direction(MASS_B_STATION_ID, MASS_A_STATION_ID, ack_required=True)
        self._run_direction(MASS_B_STATION_ID, MASS_A_STATION_ID, ack_required=False)

    def test_TC_STAGE5_013_ship_to_roc_type92_and_93(self):
        self._run_direction(MASS_A_STATION_ID, ROC_STATION_ID, ack_required=True)
        self._run_direction(MASS_B_STATION_ID, ROC_STATION_ID, ack_required=False)

    def test_TC_STAGE5_013_roc_to_ship_type92_and_93(self):
        self._run_direction(ROC_STATION_ID, MASS_A_STATION_ID, ack_required=True)
        self._run_direction(ROC_STATION_ID, MASS_B_STATION_ID, ack_required=False)


class Stage5Type93CompletionSemanticsTest(unittest.TestCase):
    def test_TC_STAGE5_013_type93_completed_means_local_tx_not_receipt(self):
        clock, group = build_stage5_software_group(type0_override=FAST_TYPE0)
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            medium = ship_a.radio._medium
            with medium._lock:
                blocked = [
                    link
                    for link in medium._links
                    if link.source_endpoint_id == "stage5-ship-a"
                    and link.target_endpoint_id == "stage5-ship-b"
                ]
                for link in blocked:
                    medium._links.remove(link)
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x93\x00\xff",
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok)
            for _ in range(120):
                drive_stage5_group(clock, group, 1)
                state = ship_a.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value.state
                if state is TransferState.COMPLETED:
                    break
            # 로컬 PHY/Radio 송신 완료로 COMPLETED이지만 어느 수신 endpoint의
            # 수신·Service 전달을 뜻하지 않는다.
            self.assertIs(TransferState.COMPLETED, state)
            self.assertNotIn(b"\x93\x00\xff", received_payloads(ship_b))
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
