"""TC-STAGE5-016: ACK/NACK, timeout과 bounded retry.

요구사항: `STAGE5-REQ-16` (`stage5_service_integration_baseline.md` §5.4, §8.2, §8.3,
§9.1, `DR-STAGE5-012`)
"""

from __future__ import annotations

import unittest

from tests.support.stage4 import Type0OverrideProfile
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
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


def _remove_links(medium, source_id, target_id):
    removed = []
    with medium._lock:
        for link in tuple(medium._links):
            if (
                link.source_endpoint_id == source_id
                and link.target_endpoint_id == target_id
            ):
                medium._links.remove(link)
                removed.append(link)
    return removed


def _restore_links(medium, links):
    with medium._lock:
        medium._links.extend(links)


class Stage5AckRetryTest(unittest.TestCase):
    def test_TC_STAGE5_016_lost_ack_retries_then_completes_without_duplicate(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            medium = ship_a.radio._medium
            payload = b"\x16\x00\xff\x01"
            # B→A 방향을 막아 Type 13 ACK를 유실시킨다. B는 데이터를 수신한다.
            blocked = _remove_links(medium, "stage5-ship-b", "stage5-ship-a")
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=True,
                )
            )
            self.assertTrue(accepted.is_ok)
            transfer_id = accepted.value.transfer_id
            retried = False
            for _ in range(420):
                drive_stage5_group(clock, group, 1)
                context = ship_a.stage4.mac.state.transfer(transfer_id)
                if context is not None and context.retransmission_number >= 1:
                    retried = True
                    break
            self.assertTrue(retried, "bounded retry가 관측되지 않았습니다")
            # WAITING_ACK에서 재전송으로 TRANSMITTING으로 되돌아온 전이가 공개된다.
            states = [
                event.transfer_state
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
                and event.transfer_id == transfer_id
            ]
            self.assertIn(TransferState.WAITING_ACK, states)
            # ACK 경로 회복 뒤 완료된다.
            _restore_links(medium, blocked)
            for _ in range(420):
                drive_stage5_group(clock, group, 1)
                state = ship_a.status(TransferStatusRequest(transfer_id)).value.state
                if state is TransferState.COMPLETED:
                    break
            self.assertIs(TransferState.COMPLETED, state)
            # 재전송 duplicate는 ledger로 억제돼 B에는 정확히 1회 전달된다 (§8.3).
            drive_stage5_group(clock, group, 30)
            self.assertEqual(1, received_payloads(ship_b).count(payload))
        finally:
            group.stop()

    def test_TC_STAGE5_016_short_data_retry_exhaustion_is_terminal_failure(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            medium = ship_a.radio._medium
            # A→B 자체를 막아 데이터와 ACK 모두 도달하지 못하게 한다.
            _remove_links(medium, "stage5-ship-a", "stage5-ship-b")
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x16\x02",
                    ack_required=True,
                )
            )
            self.assertTrue(accepted.is_ok)
            transfer_id = accepted.value.transfer_id
            for _ in range(900):
                drive_stage5_group(clock, group, 1)
                snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
                if snapshot.state is TransferState.FAILED:
                    break
            self.assertIs(TransferState.FAILED, snapshot.state)
            # Type 92 최대 재전송 소진은 동결 Stage 4 projection대로
            # `TRANSFER_FAILED`다 (RETRY_EXHAUSTED, TC-MAC-015 고정 매핑).
            self.assertIsNotNone(snapshot.error)
            self.assertEqual("TRANSFER_FAILED", snapshot.error.code)
            # terminal 실패는 TRANSFER_STATE_CHANGED(FAILED) 하나로 전달되며 같은
            # 원인의 별도 ERROR event를 중복 생성하지 않는다 (`DR-STAGE5-015`).
            terminal_events = [
                event
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
                and event.transfer_id == transfer_id
                and event.transfer_state is TransferState.FAILED
            ]
            self.assertEqual(1, len(terminal_events))
            self.assertIsNotNone(terminal_events[0].error)
            duplicate_errors = [
                event
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.ERROR
                and event.transfer_id == transfer_id
            ]
            self.assertEqual([], duplicate_errors)
        finally:
            group.stop()

    def test_TC_STAGE5_016_session_three_frame_timeout_projects_transfer_timeout(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            roc_station = next(
                station
                for station in group.by_station()
                if station not in (MASS_A_STATION_ID.value, MASS_B_STATION_ID.value)
            )
            roc = group.by_station()[roc_station]
            medium = ship_a.radio._medium
            payload = bytes(range(200))
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(
                        roc.dependencies.configuration.endpoint.station_id
                    ),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok)
            transfer_id = accepted.value.transfer_id
            # 자원 할당 뒤 ROC→A 경로를 막아 Type 13 frame feedback을 전부 유실시킨다.
            for _ in range(400):
                drive_stage5_group(clock, group, 1)
                context = ship_a.stage4.mac.state.transfer(transfer_id)
                if context is not None and context.first_tx_accepted:
                    break
            _remove_links(medium, "stage5-roc", "stage5-ship-a")
            for _ in range(900):
                drive_stage5_group(clock, group, 1)
                snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
                if snapshot.state is TransferState.FAILED:
                    break
            self.assertIs(TransferState.FAILED, snapshot.state)
            # Data Session의 3-frame absent-ACK deadline은 `TRANSFER_TIMEOUT/TIMEOUT`
            # retryable로 projection된다 (§9.5).
            self.assertIsNotNone(snapshot.error)
            self.assertEqual("TRANSFER_TIMEOUT", snapshot.error.code)
            self.assertEqual("TIMEOUT", snapshot.error.category.value)
            self.assertTrue(snapshot.error.retryable)
        finally:
            group.stop()

    def test_TC_STAGE5_016_session_frame_feedback_is_not_service_delivery(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            roc = group.by_station()[
                next(
                    station
                    for station in group.by_station()
                    if station not in (MASS_A_STATION_ID.value, MASS_B_STATION_ID.value)
                )
            ]
            # 21 fragment → 2 TDMA frame: frame 0의 Type 13이 최종 재조립·전달보다
            # 먼저 도착한다.
            payload = bytes(range(256)) * 3
            payload = payload[:700]
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(
                        roc.dependencies.configuration.endpoint.station_id
                    ),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok)
            saw_frame_feedback_before_delivery = False
            for _ in range(400):
                drive_stage5_group(clock, group, 1)
                delivered = payload in received_payloads(roc)
                decoded_13 = any(
                    message.message_type == 13
                    for event in ship_a.dependencies.internal_event_sink.events_of(
                        __import__(
                            "vdeter.common.events", fromlist=["InternalEventType"]
                        ).InternalEventType.MESSAGES_DECODED
                    )
                    for message in event.payload.messages
                )
                if decoded_13 and not delivered:
                    saw_frame_feedback_before_delivery = True
                if (
                    ship_a.status(TransferStatusRequest(accepted.value.transfer_id)).value.state
                    is TransferState.COMPLETED
                ):
                    break
            # Data Session Type 13 mask는 frame 수신 feedback이며 Service delivery와
            # 분리된다: 최소 한 frame ACK가 application 전달 전에 관측된다.
            self.assertTrue(saw_frame_feedback_before_delivery)
            self.assertEqual(1, received_payloads(roc).count(payload))
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
