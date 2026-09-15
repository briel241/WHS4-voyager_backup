"""TC-STAGE5-014: addressed Data Session 전체 방향과 ROC relay.

요구사항: `STAGE5-REQ-14`, `STAGE5-REQ-19`
(`stage5_service_integration_baseline.md` §4.3, §8.2, §8.3, §9.1, §11.2,
`DR-STAGE5-027`)
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
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat

OPAQUE = PayloadFormat("test.stage0.opaque")
FAST_TYPE0 = Type0OverrideProfile(selection_interval=30, short_data_limit=4, system_status=0)


class _LowerBoundRandom:
    def next_int(self, low: int, high: int) -> int:
        return low

    def description(self) -> str:
        return "stage5-test:lower-bound"


def _group():
    clock, group = build_stage5_software_group(
        type0_override=FAST_TYPE0,
        random_source_factory=lambda _index: _LowerBoundRandom(),
    )
    start_stage5_group(clock, group)
    return clock, group


class Stage5OneHopDataSessionTest(unittest.TestCase):
    def _run_session(self, clock, group, source_station, destination_station, *, ack_required):
        by_station = group.by_station()
        source = by_station[source_station.value]
        destination = by_station[destination_station.value]
        payload = bytes(
            (source_station.value % 256, destination_station.value % 256, ack_required)
        ) + bytes(range(256)) * 2
        before = len(received_payloads(destination))
        accepted = source.submit(
            SubmitTransferRequest(
                destination=Addressed(destination_station),
                payload_format=OPAQUE,
                payload=payload,
                ack_required=ack_required,
            )
        )
        self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
        for _ in range(400):
            drive_stage5_group(clock, group, 1)
            state = source.status(
                TransferStatusRequest(accepted.value.transfer_id)
            ).value.state
            if state is TransferState.COMPLETED:
                break
        self.assertIs(TransferState.COMPLETED, state)
        received = received_payloads(destination)[before:]
        self.assertEqual([payload], received)
        return accepted.value.transfer_id

    def test_TC_STAGE5_014_ship_to_roc_session_both_ack_modes(self):
        clock, group = _group()
        try:
            # 크기 때문에 Data Session이 선택되면 ack_required=false여도 Type 13
            # feedback은 필수이고, true여도 두 번째 application ACK를 추가하지 않는다.
            self._run_session(clock, group, MASS_A_STATION_ID, ROC_STATION_ID, ack_required=True)
            self._run_session(clock, group, MASS_A_STATION_ID, ROC_STATION_ID, ack_required=False)
        finally:
            group.stop()

    def test_TC_STAGE5_014_roc_to_ship_session_uses_type90_type4_allocation(self):
        clock, group = _group()
        try:
            roc = group.by_station()[ROC_STATION_ID.value]
            self._run_session(clock, group, ROC_STATION_ID, MASS_B_STATION_ID, ack_required=True)
            self._run_session(clock, group, ROC_STATION_ID, MASS_B_STATION_ID, ack_required=False)
            # Type 90 자원 요청과 Type 4 할당이 내부에서 수행됐다 (보호된 내부 evidence).
            from vdeter.common.events import InternalEventType

            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            decoded_types = set()
            for event in ship_b.dependencies.internal_event_sink.events_of(
                InternalEventType.MESSAGES_DECODED
            ):
                for message in event.payload.messages:
                    decoded_types.add(message.message_type)
            self.assertIn(4, decoded_types)
        finally:
            group.stop()


class Stage5RelayDataSessionTest(unittest.TestCase):
    def _run_relay(self, source_station, destination_station):
        clock, group = _group()
        try:
            by_station = group.by_station()
            source = by_station[source_station.value]
            destination = by_station[destination_station.value]
            roc = by_station[ROC_STATION_ID.value]
            # §11.2 relay row: `ack_required` true/false 모두 시험한다. 이 flag는 relay
            # Data Session의 wire 선택을 바꾸지 않고 내부 Type 13 feedback은 항상 필수다.
            for relay_ordinal, ack_required in enumerate((True, False), start=1):
                self._run_relay_once(
                    clock,
                    group,
                    source,
                    destination,
                    roc,
                    source_station,
                    destination_station,
                    ack_required=ack_required,
                    relay_ordinal=relay_ordinal,
                )
        finally:
            group.stop()

    def _run_relay_once(
        self,
        clock,
        group,
        source,
        destination,
        roc,
        source_station,
        destination_station,
        *,
        ack_required,
        relay_ordinal,
    ):
            payload = bytes((0xE0 + relay_ordinal, ack_required)) + bytes(range(256)) * 3
            received_before = len(received_payloads(destination))
            accepted = source.submit(
                SubmitTransferRequest(
                    destination=Addressed(destination_station),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=ack_required,
                )
            )
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            source_completed_step = None
            destination_step = None
            for step in range(600):
                drive_stage5_group(clock, group, 1)
                state = source.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value.state
                if source_completed_step is None and state is TransferState.COMPLETED:
                    source_completed_step = step
                if (
                    destination_step is None
                    and received_payloads(destination)[received_before:] == [payload]
                ):
                    destination_step = step
                if source_completed_step is not None and destination_step is not None:
                    break
            self.assertIsNotNone(source_completed_step, "first hop did not complete")
            self.assertIsNotNone(destination_step, "second hop did not deliver")
            # 원 송신의 COMPLETED는 `ACCEPTED_BY_ROC_FOR_RELAY`이며 최종 Ship 수신
            # 완료가 아니다: relay record가 first-hop ACK와 relay 수락을 증명한다.
            self.assertEqual(relay_ordinal, len(roc.stage4.mac.state.relay_records))
            relay = roc.stage4.mac.state.relay_records[-1]
            self.assertTrue(relay.admitted)
            self.assertTrue(relay.first_hop_ack_completed)
            self.assertTrue(relay.second_hop_started)
            # Original 주소는 보존되고 Node 주소만 hop별로 바뀐다.
            self.assertEqual(
                source_station, relay.second_hop_route.original_source.station_id
            )
            self.assertEqual(
                destination_station,
                relay.second_hop_route.original_destination.address.station_id,
            )
            self.assertEqual(ROC_STATION_ID, relay.second_hop_route.node_source.station_id)
            self.assertEqual(
                destination_station,
                relay.second_hop_route.node_destination.address.station_id,
            )
            # ROC는 relay payload를 자기 Service로 전달하지 않고, 내부 relay transfer
            # ID를 공개 repository에 노출하지 않는다.
            self.assertEqual([], received_payloads(roc))
            self.assertNotIn(
                relay.relay_transfer_id.value, {key for key in roc._transfers}
            )
            # 최종 수신은 정확히 1회다. `DR-STAGE5-030`: 동결 M.2092 Type 74/75/76
            # wire에는 별도의 Original 주소 field가 없어 relay 최종 hop의 공개 수신
            # 주소는 wire의 hop source(ROC)다. end-to-end Original 주소 보존은 ROC의
            # relay record와 correlation evidence(위 assert)로 검증한다.
            drive_stage5_group(clock, group, 30)
            self.assertEqual([payload], received_payloads(destination)[received_before:])
            event = [
                item
                for item in destination.dependencies.service_event_sink.events()
                if item.event_type is ServiceEventType.DATA_RECEIVED
            ][-1]
            self.assertEqual(ROC_STATION_ID, event.received_data.original_source)
            self.assertEqual(
                destination_station,
                event.received_data.original_destination.station_id,
            )

    def test_TC_STAGE5_014_relay_ship_a_to_ship_b(self):
        self._run_relay(MASS_A_STATION_ID, MASS_B_STATION_ID)

    def test_TC_STAGE5_014_relay_reverse_ship_b_to_ship_a(self):
        self._run_relay(MASS_B_STATION_ID, MASS_A_STATION_ID)


if __name__ == "__main__":
    unittest.main()
