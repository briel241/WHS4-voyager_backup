"""TC-STAGE5-005: cancel과 terminal 경쟁.

요구사항: `STAGE5-REQ-05` (`stage5_service_integration_baseline.md` §5.6, §9.2,
`DR-STAGE5-007`, `DR-STAGE5-013`, `DR-STAGE5-025`)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

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
from vdeter.common.errors import ErrorCategory, ErrorCode, Layer
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat, TransferId
from vdeter.common.results import internal_error
from vdeter.runtime.stage5 import Stage5Support

OPAQUE = PayloadFormat("test.stage0.opaque")


def _submit(endpoint, payload=b"\x01", *, ack=True):
    return endpoint.submit(
        SubmitTransferRequest(
            destination=Addressed(MASS_B_STATION_ID),
            payload_format=OPAQUE,
            payload=payload,
            ack_required=ack,
        )
    )


def _terminal_events(endpoint, transfer_id):
    return [
        event
        for event in endpoint.dependencies.service_event_sink.events()
        if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
        and event.transfer_id == transfer_id
        and event.transfer_state
        in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED)
    ]


class Stage5CancelQueuedTest(unittest.TestCase):
    def test_TC_STAGE5_005_queued_cancel_is_atomic_and_clean(self):
        clock, group = build_stage5_software_group()
        try:
            started = group.start()
            self.assertTrue(started.is_ok)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x11")
            transfer_id = accepted.value.transfer_id
            cancelled = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(cancelled.is_ok)
            self.assertIs(TransferState.CANCELLED, cancelled.value.state)
            snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
            # `CancelAccepted.accepted_at`은 terminal commit과 같은 UTC 시각이다.
            self.assertEqual(snapshot.updated_at, cancelled.value.accepted_at)
            # 하위 송신을 시작하지 않았다: MAC context와 pending presentation이 없다.
            self.assertIsNone(ship_a.stage4.mac.state.transfer(transfer_id))
            self.assertNotIn(transfer_id.value, ship_a._pending_presentation)
            self.assertEqual(1, len(_terminal_events(ship_a, transfer_id)))
        finally:
            group.stop()

    def test_TC_STAGE5_005_unknown_and_not_started_cancel(self):
        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        ghost = TransferId("0b7c6a5d-4e3f-4a2b-9c8d-7e6f5a4b3c2d")
        before_start = ship_a.cancel(CancelTransferRequest(ghost))
        self.assertTrue(before_start.is_err)
        # 정지 상태면 ID lookup보다 먼저 SERVICE_NOT_STARTED를 반환한다.
        self.assertEqual("SERVICE_NOT_STARTED", before_start.error.code)
        started = group.start()
        self.assertTrue(started.is_ok)
        try:
            unknown = ship_a.cancel(CancelTransferRequest(ghost))
            self.assertTrue(unknown.is_err)
            self.assertEqual("UNKNOWN_TRANSFER", unknown.error.code)
        finally:
            group.stop()


class Stage5CancelActiveTest(unittest.TestCase):
    def _drive_until_transmitting(self, clock, group, endpoint, transfer_id):
        for _ in range(120):
            drive_stage5_group(clock, group, 1)
            state = endpoint.status(TransferStatusRequest(transfer_id)).value.state
            if state in (TransferState.TRANSMITTING, TransferState.WAITING_ACK):
                return state
        raise AssertionError("transfer did not reach an active state")

    def test_TC_STAGE5_005_active_cancel_uses_mac_and_link_cleanup(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            # Data Session이면 취소 시점에 active context가 확실히 남아 있다.
            accepted = _submit(ship_a, bytes(range(200)), ack=True)
            transfer_id = accepted.value.transfer_id
            self._drive_until_transmitting(clock, group, ship_a, transfer_id)
            cancelled = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(cancelled.is_ok, getattr(cancelled, "error", None))
            self.assertIs(TransferState.CANCELLED, cancelled.value.state)
            # MAC context가 terminal로 닫혔고 반복 취소는 INVALID_REQUEST다.
            from vdeter.mac.models import TransferPhase

            context = ship_a.stage4.mac.state.transfer(transfer_id)
            self.assertIsNotNone(context)
            self.assertIs(TransferPhase.TERMINAL, context.phase)
            again = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(again.is_err)
            self.assertEqual("INVALID_REQUEST", again.error.code)
            # 늦게 도착하는 중복 PublishTransferStateCommand는 idempotent하게 흡수돼
            # terminal event는 정확히 한 번이다.
            drive_stage5_group(clock, group, 20)
            self.assertEqual(1, len(_terminal_events(ship_a, transfer_id)))
            snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
            self.assertIs(TransferState.CANCELLED, snapshot.state)
        finally:
            group.stop()

    def test_TC_STAGE5_005_completed_transfer_wins_cancel_race(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, b"\x22", ack=True)
            transfer_id = accepted.value.transfer_id
            for _ in range(150):
                drive_stage5_group(clock, group, 1)
                if (
                    ship_a.status(TransferStatusRequest(transfer_id)).value.state
                    is TransferState.COMPLETED
                ):
                    break
            result = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(result.is_err)
            self.assertEqual("INVALID_REQUEST", result.error.code)
            # 최초 terminal commit이 승리하고 상태는 부활하지 않는다.
            snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
            self.assertIs(TransferState.COMPLETED, snapshot.state)
            self.assertEqual(1, len(_terminal_events(ship_a, transfer_id)))
        finally:
            group.stop()

    def test_TC_STAGE5_005_cancel_during_dispatch_never_leaves_live_mac_work(self):
        """dispatch의 lock 밖 구간과 경쟁한 cancel 뒤 무선 작업이 남지 않는다 (§9.2)."""

        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            real_mac = ship_a._mac
            test_case = self

            class _RaceInjectingMac:
                """첫 submit_requirement 직전에 cancel()을 끼워 넣는 시험 proxy."""

                def __init__(self):
                    self.injected = False
                    self.cancel_result = None

                def submit_requirement(self, requirement):
                    if not self.injected:
                        self.injected = True
                        self.cancel_result = ship_a.cancel(
                            CancelTransferRequest(requirement.transfer_id)
                        )
                    return real_mac.submit_requirement(requirement)

                def __getattr__(self, name):
                    return getattr(real_mac, name)

            proxy = _RaceInjectingMac()
            ship_a._mac = proxy
            try:
                accepted = _submit(ship_a, b"\x33", ack=True)
                self.assertTrue(accepted.is_ok)
                transfer_id = accepted.value.transfer_id
                drive_stage5_group(clock, group, 4)
            finally:
                ship_a._mac = real_mac
            self.assertTrue(proxy.injected)
            cancel_result = proxy.cancel_result
            self.assertIsNotNone(cancel_result)
            self.assertTrue(cancel_result.is_ok, getattr(cancel_result, "error", None))
            self.assertIs(TransferState.CANCELLED, cancel_result.value.state)
            # 취소가 이겼으므로 공개 상태는 CANCELLED로 남고 부활하지 않는다.
            drive_stage5_group(clock, group, 120)
            snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
            self.assertIs(TransferState.CANCELLED, snapshot.state)
            # 경쟁 중 만들어진 MAC context는 즉시 동결 cancel 절차로 정리된다.
            from vdeter.mac.models import TransferPhase

            context = ship_a.stage4.mac.state.transfer(transfer_id)
            if context is not None:
                self.assertIs(TransferPhase.TERMINAL, context.phase)
            # 목적지에는 어떤 data도 전달되지 않는다.
            from tests.support.stage5 import received_payloads

            self.assertNotIn(b"\x33", received_payloads(ship_b))
            self.assertEqual(1, len(_terminal_events(ship_a, transfer_id)))
        finally:
            group.stop()

    def test_TC_STAGE5_005_failed_cancel_reservation_keeps_nonterminal_state(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = _submit(ship_a, bytes(range(200)), ack=True)
            transfer_id = accepted.value.transfer_id
            self._drive_until_transmitting(clock, group, ship_a, transfer_id)
            blocked = internal_error(
                Layer.RUNTIME,
                ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
                ErrorCategory.RESOURCE,
                "시험 주입: cancel event 예약 실패",
                retryable=True,
            )
            with patch.object(Stage5Support, "enqueue", lambda *args, **kwargs: blocked):
                result = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(result.is_err)
            # reservation 실패면 기존 nonterminal 상태를 유지한다.
            snapshot = ship_a.status(TransferStatusRequest(transfer_id)).value
            self.assertIn(
                snapshot.state, (TransferState.TRANSMITTING, TransferState.WAITING_ACK)
            )
            # 이후 정상 cancel은 성공한다.
            retried = ship_a.cancel(CancelTransferRequest(transfer_id))
            self.assertTrue(retried.is_ok, getattr(retried, "error", None))
            self.assertIs(TransferState.CANCELLED, retried.value.state)
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
