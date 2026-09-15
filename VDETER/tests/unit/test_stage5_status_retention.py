"""TC-STAGE5-004: 상태·조회·terminal 보관.

요구사항: `STAGE5-REQ-04` (`stage5_service_integration_baseline.md` §5.5, §9.1, §9.3,
`DR-STAGE5-003`, `DR-STAGE5-023`)
"""

from __future__ import annotations

import unittest

from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    start_stage5_group,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    CancelTransferRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat
from vdeter.common.time import ClockDomain
from vdeter.service.models import Stage5ServicePolicy

OPAQUE = PayloadFormat("test.stage0.opaque")
FIFTEEN_MINUTES_NS = 15 * 60 * 1_000_000_000


def _submit(endpoint, payload=b"\x01", *, ack=False):
    return endpoint.submit(
        SubmitTransferRequest(
            destination=Addressed(MASS_B_STATION_ID),
            payload_format=OPAQUE,
            payload=payload,
            ack_required=ack,
        )
    )


class Stage5StatusStateTest(unittest.TestCase):
    def test_TC_STAGE5_004_requirement_accepted_keeps_queued_until_phy_tx(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            first = _submit(ship_a, b"\x11", ack=True)
            second = _submit(ship_a, b"\x12", ack=True)
            self.assertTrue(first.is_ok and second.is_ok)
            drive_stage5_group(clock, group, 2)
            # 두 요청 모두 MAC RequirementAccepted까지 도달했지만, 실제 PHY 송신 수락
            # 전에는 여전히 QUEUED다 (`DR-STAGE5-023`).
            self.assertIsNotNone(ship_a.stage4.mac.state.transfer(first.value.transfer_id))
            self.assertIsNotNone(ship_a.stage4.mac.state.transfer(second.value.transfer_id))
            self.assertIs(
                TransferState.QUEUED,
                ship_a.status(TransferStatusRequest(first.value.transfer_id)).value.state,
            )
            first_state = TransferState.QUEUED
            for _ in range(80):
                drive_stage5_group(clock, group, 1)
                first_state = ship_a.status(
                    TransferStatusRequest(first.value.transfer_id)
                ).value.state
                if first_state is not TransferState.QUEUED:
                    break
            second_state = ship_a.status(
                TransferStatusRequest(second.value.transfer_id)
            ).value.state
            # 첫 요청은 첫 PhyTxAcceptedEvent 뒤 TRANSMITTING 이상으로 전이하고,
            # 두 번째 ACK-required Short Data는 MAC FIFO에서 기다리며
            # RequirementAccepted만으로는 QUEUED를 유지한다.
            self.assertIn(
                first_state,
                (TransferState.TRANSMITTING, TransferState.WAITING_ACK, TransferState.COMPLETED),
            )
            self.assertIs(TransferState.QUEUED, second_state)
        finally:
            group.stop()

    def test_TC_STAGE5_004_public_state_sequence_and_waiting_ack_gate(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x21", ack=True)
            for _ in range(150):
                drive_stage5_group(clock, group, 1)
                state = ship_a.status(TransferStatusRequest(accepted.value.transfer_id)).value.state
                if state is TransferState.COMPLETED:
                    break
            states = [
                event.transfer_state
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
                and event.transfer_id == accepted.value.transfer_id
            ]
            # WAITING_ACK는 승인된 ACK deadline이 arm된 뒤에만, TRANSMITTING은 첫
            # PhyTxAcceptedEvent 뒤에만 나타난다.
            self.assertEqual(
                [
                    TransferState.QUEUED,
                    TransferState.TRANSMITTING,
                    TransferState.WAITING_ACK,
                    TransferState.COMPLETED,
                ],
                states,
            )
        finally:
            group.stop()

    def test_TC_STAGE5_004_status_snapshot_error_field_rules(self):
        clock, group = build_stage5_software_group()
        try:
            started = group.start()
            self.assertTrue(started.is_ok)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x31")
            snapshot = ship_a.status(TransferStatusRequest(accepted.value.transfer_id)).value
            self.assertIs(TransferState.QUEUED, snapshot.state)
            self.assertIsNone(snapshot.error)
            self.assertIs(ClockDomain.UTC, snapshot.updated_at.clock_domain)
            cancelled = ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
            self.assertTrue(cancelled.is_ok)
            snapshot = ship_a.status(TransferStatusRequest(accepted.value.transfer_id)).value
            self.assertIs(TransferState.CANCELLED, snapshot.state)
            self.assertIsNone(snapshot.error)
        finally:
            group.stop()

    def test_TC_STAGE5_004_unknown_and_stopped_service_lookup(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        accepted = _submit(ship_a, b"\x41")
        from vdeter.common.identifiers import TransferId

        unknown = ship_a.status(
            TransferStatusRequest(TransferId("0b7c6a5d-4e3f-4a2b-9c8d-7e6f5a4b3c2d"))
        )
        self.assertTrue(unknown.is_err)
        self.assertEqual("UNKNOWN_TRANSFER", unknown.error.code)
        group.stop()
        stopped = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertTrue(stopped.is_err)
        self.assertEqual("SERVICE_NOT_STARTED", stopped.error.code)


class Stage5TerminalRetentionTest(unittest.TestCase):
    def test_TC_STAGE5_004_ttl_eviction_uses_monotonic_15_minutes(self):
        clock, group = build_stage5_software_group()
        try:
            started = group.start()
            self.assertTrue(started.is_ok)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x51")
            ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
            clock.advance(FIFTEEN_MINUTES_NS - 1_000_000_000)
            still = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
            self.assertTrue(still.is_ok)
            clock.advance(2_000_000_000)
            gone = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
            self.assertTrue(gone.is_err)
            self.assertEqual("UNKNOWN_TRANSFER", gone.error.code)
        finally:
            group.stop()

    def test_TC_STAGE5_004_count_eviction_removes_oldest_terminal_first(self):
        policy = Stage5ServicePolicy(terminal_retention_max_records=2)
        clock, group = build_stage5_software_group(policy=policy)
        try:
            started = group.start()
            self.assertTrue(started.is_ok)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            terminals = []
            for index in range(3):
                accepted = _submit(ship_a, bytes((0x60 + index,)))
                ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
                terminals.append(accepted.value.transfer_id)
                clock.advance(1_000_000)
            # nonterminal record는 이 정책으로 제거되지 않는다.
            queued = _submit(ship_a, b"\x6f")
            oldest = ship_a.status(TransferStatusRequest(terminals[0]))
            self.assertTrue(oldest.is_err)
            self.assertEqual("UNKNOWN_TRANSFER", oldest.error.code)
            for kept in terminals[1:]:
                self.assertTrue(ship_a.status(TransferStatusRequest(kept)).is_ok)
            self.assertTrue(ship_a.status(TransferStatusRequest(queued.value.transfer_id)).is_ok)
        finally:
            group.stop()

    def test_TC_STAGE5_004_wall_clock_jump_does_not_evict(self):
        clock, group = build_stage5_software_group()
        try:
            started = group.start()
            self.assertTrue(started.is_ok)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x71")
            ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
            # TTL은 monotonic 기준이므로 UTC wall-clock 점프는 eviction을 만들지 않는다.
            clock.advance_utc(2 * FIFTEEN_MINUTES_NS)
            still = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
            self.assertTrue(still.is_ok, getattr(still, "error", None))
        finally:
            group.stop()

    def test_TC_STAGE5_004_stop_clears_all_records(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        accepted = _submit(ship_a, b"\x81")
        ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
        group.stop()
        restart = group.start()
        self.assertTrue(restart.is_ok, getattr(restart, "error", None))
        lookup = ship_a.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertTrue(lookup.is_err)
        self.assertEqual("UNKNOWN_TRANSFER", lookup.error.code)
        group.stop()


if __name__ == "__main__":
    unittest.main()
