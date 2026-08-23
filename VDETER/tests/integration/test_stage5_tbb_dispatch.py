"""TC-STAGE5-012: TBB 수신·적용과 자원 절차 내부화.

요구사항: `STAGE5-REQ-12` (`stage5_service_integration_baseline.md` §4.2, §8.1, §8.2,
`DR-STAGE5-009`)
"""

from __future__ import annotations

import unittest

from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    received_payloads,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    CancelTransferRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.identifiers import PayloadFormat

OPAQUE = PayloadFormat("test.stage0.opaque")

ONE_HOUR_NS = 61 * 60 * 1_000_000_000


class Stage5TbbDispatchTest(unittest.TestCase):
    def _blocked_roc_medium(self, group):
        medium = group.endpoints[0].radio._medium
        removed = []
        with medium._lock:
            for link in tuple(medium._links):
                if link.source_endpoint_id == "stage5-roc":
                    medium._links.remove(link)
                    removed.append(link)
        return medium, removed

    def test_TC_STAGE5_012_submit_before_tbb_stays_queued_without_timeout(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok, getattr(started, "error", None))
        try:
            # ROC 송신 경로를 막아 TBB 획득을 지연시킨다.
            medium, removed = self._blocked_roc_medium(group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            first = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x11",
                    ack_required=True,
                )
            )
            second = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x12",
                    ack_required=True,
                )
            )
            self.assertTrue(first.is_ok and second.is_ok)
            # TBB가 없어도 이미 수락한 요청을 MAC에 제출해 즉시 실패시키지 않는다.
            drive_stage5_group(clock, group, 40)
            self.assertIsNone(ship_a.stage4.mac.state.active_tbb)
            for accepted in (first, second):
                state = ship_a.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value.state
                self.assertIs(TransferState.QUEUED, state)
                self.assertIsNone(
                    ship_a.stage4.mac.state.transfer(accepted.value.transfer_id)
                )
            # TBB 적용(자원 회복 event) 뒤 accepted_at 순서로 FIFO dispatch된다.
            with medium._lock:
                medium._links.extend(removed)
            acquired = False
            for _ in range(120):
                drive_stage5_group(clock, group, 1)
                if ship_a.stage4.mac.state.active_tbb is not None:
                    acquired = True
                    break
            self.assertTrue(acquired, "TBB reacquisition did not happen")
            for _ in range(200):
                drive_stage5_group(clock, group, 1)
                states = [
                    ship_a.status(TransferStatusRequest(item.value.transfer_id)).value.state
                    for item in (first, second)
                ]
                if all(state is TransferState.COMPLETED for state in states):
                    break
            self.assertEqual(
                [TransferState.COMPLETED, TransferState.COMPLETED], states
            )
            ship_b = group.by_station()[MASS_B_STATION_ID.value]
            self.assertEqual([b"\x11", b"\x12"], received_payloads(ship_b))
        finally:
            group.stop()

    def test_TC_STAGE5_012_tbb_acquisition_uses_type20_21_22_and_fingerprint(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        try:
            drive_stage5_group(clock, group, 80)
            fingerprints = set()
            for endpoint in group.endpoints:
                active = endpoint.stage4.mac.state.active_tbb
                self.assertIsNotNone(
                    active,
                    endpoint.dependencies.configuration.endpoint.endpoint_id,
                )
                fingerprints.add(
                    endpoint.stage4.mac.coordinator.profile.canonical_tbb.canonical_tbb_fingerprint
                )
            # 세 endpoint는 동일 TBB semantic fingerprint를 공유한다.
            self.assertEqual(1, len(fingerprints))
            # Ship은 Type 20/21/22 수신·검증·조립으로 TBB를 획득했다 (보호된 내부 evidence).
            from vdeter.common.events import InternalEventType

            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            assembly_updates = 0
            verified = 0
            for event in ship_a.dependencies.internal_event_sink.events_of(
                InternalEventType.MESSAGES_DECODED
            ):
                assembly_updates += len(event.payload.tbb_assembly_updates)
                verified += len(event.payload.verified_tbbs)
            # Type 20(start)/21(continuation)/22(end) fragment들이 조립되고 서명 검증된
            # TBB 하나가 적용됐다.
            self.assertGreaterEqual(assembly_updates, 3)
            self.assertGreaterEqual(verified, 1)
        finally:
            group.stop()

    def test_TC_STAGE5_012_expired_tbb_returns_submit_to_service_queue(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        try:
            drive_stage5_group(clock, group, 80)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            self.assertIsNotNone(ship_a.stage4.mac.state.active_tbb)
            # 유효구간(60분)을 지나면 TBB는 비활성이다. MAC 알고리즘은 바꾸지 않고
            # Service FIFO가 QUEUED를 유지해야 한다.
            clock.advance(ONE_HOUR_NS)
            now = clock.now_utc().total_nanoseconds()
            self.assertFalse(ship_a.stage4.mac.state.active_tbb.is_active_at(now))
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x21",
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok)
            # dispatch gate가 비활성 TBB를 보고 MAC 제출 없이 QUEUED를 유지한다.
            ship_a.stage4.pump()
            state = ship_a.status(TransferStatusRequest(accepted.value.transfer_id)).value.state
            self.assertIs(TransferState.QUEUED, state)
            self.assertIsNone(ship_a.stage4.mac.state.transfer(accepted.value.transfer_id))
            # 별도 Service queue timeout은 없다. Client cancel로만 끝난다.
            cancelled = ship_a.cancel(CancelTransferRequest(accepted.value.transfer_id))
            self.assertTrue(cancelled.is_ok)
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
