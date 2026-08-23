"""TC-STAGE5-017: 분할·재조립과 최대 payload.

요구사항: `STAGE5-REQ-17` (`stage5_service_integration_baseline.md` §8.3, §11.1)
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
from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.link import LinkAnalyzeRequest
from vdeter.common.contracts.service import (
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.identifiers import LinkId, PayloadFormat

OPAQUE = PayloadFormat("test.stage0.opaque")
AIS = PayloadFormat("ais.authentication_integrity.scheme1")
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


class Stage5FragmentationTest(unittest.TestCase):
    def test_TC_STAGE5_017_ais_84_byte_wire_uses_three_fragments(self):
        from pathlib import Path

        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            roc = group.by_station()[ROC_STATION_ID.value]
            application = (
                Path(__file__).resolve().parents[1]
                / "vectors"
                / "presentation"
                / "gv_stage5_pres_ais1_001.application.bin"
            ).read_bytes()
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(ROC_STATION_ID),
                    payload_format=AIS,
                    payload=application,
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            wire = ship_a._pending_presentation[accepted.value.transfer_id.value].data
            self.assertEqual(84, len(wire))
            # 분석 record가 Link ID 11에서 84-byte encoded의 3-fragment 분할을 요구한다.
            analyzed = ship_a._link.analyze_transfer(
                LinkAnalyzeRequest(
                    transfer_id=accepted.value.transfer_id,
                    route=RouteAddresses(
                        original_source=OriginalSource(MASS_A_STATION_ID),
                        original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
                    ),
                    ack_required=False,
                    presentation_data=wire,
                )
            )
            self.assertTrue(analyzed.is_ok, getattr(analyzed, "error", None))
            link11 = next(
                item for item in analyzed.value.profiles if item.link_id == LinkId(11)
            )
            self.assertEqual(3, link11.data_session_fragment_count)
            self.assertEqual(1, link11.minimum_tdma_frame_count)
            for _ in range(400):
                drive_stage5_group(clock, group, 1)
                state = ship_a.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value.state
                if state is TransferState.COMPLETED:
                    break
            self.assertIs(TransferState.COMPLETED, state)
            self.assertEqual([application], received_payloads(roc))
        finally:
            group.stop()

    def test_TC_STAGE5_017_missing_fragments_never_deliver_partial_data(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            roc = group.by_station()[ROC_STATION_ID.value]
            medium = ship_a.radio._medium
            payload = bytes(range(256)) * 2
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(ROC_STATION_ID),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok)
            # 첫 fragment들이 나간 뒤 A→ROC 경로를 영구 차단해 재조립을 불가능하게 한다.
            for _ in range(400):
                drive_stage5_group(clock, group, 1)
                context = ship_a.stage4.mac.state.transfer(accepted.value.transfer_id)
                if context is not None and context.first_tx_accepted:
                    break
            with medium._lock:
                for link in tuple(medium._links):
                    if (
                        link.source_endpoint_id == "stage5-ship-a"
                        and link.target_endpoint_id == "stage5-roc"
                    ):
                        medium._links.remove(link)
            for _ in range(900):
                drive_stage5_group(clock, group, 1)
                snapshot = ship_a.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value
                if snapshot.state is TransferState.FAILED:
                    break
            # 송신은 유한하게 실패로 닫히고, 일부 fragment만으로는 어떤 data도
            # Service에 전달되지 않는다.
            self.assertIs(TransferState.FAILED, snapshot.state)
            self.assertEqual([], received_payloads(roc))
        finally:
            group.stop()

    def test_TC_STAGE5_017_maximum_payload_1873_fragments_134_frames(self):
        clock, group = _group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            roc = group.by_station()[ROC_STATION_ID.value]
            payload = bytes(range(256)) * 256  # 65,536 bytes 초과 방지: 아래에서 자른다.
            payload = payload[:65_531]
            self.assertEqual(65_531, len(payload))
            accepted = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(ROC_STATION_ID),
                    payload_format=OPAQUE,
                    payload=payload,
                    ack_required=False,
                )
            )
            self.assertTrue(accepted.is_ok, getattr(accepted, "error", None))
            # encoded 65,535 + 4-byte Segment header = 65,539 bytes → 1,873 fragment,
            # frame당 최대 14 fragment → 최소 134 TDMA frame.
            presentation = ship_a._pending_presentation[accepted.value.transfer_id.value]
            self.assertEqual(65_535, len(presentation.data))
            analyzed = ship_a._link.analyze_transfer(
                LinkAnalyzeRequest(
                    transfer_id=accepted.value.transfer_id,
                    route=RouteAddresses(
                        original_source=OriginalSource(MASS_A_STATION_ID),
                        original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
                    ),
                    ack_required=False,
                    presentation_data=b"\x0f\xff\xff\xff" + payload,
                )
            )
            self.assertTrue(analyzed.is_ok, getattr(analyzed, "error", None))
            link11 = next(
                item for item in analyzed.value.profiles if item.link_id == LinkId(11)
            )
            self.assertEqual(1_873, link11.data_session_fragment_count)
            self.assertEqual(134, link11.minimum_tdma_frame_count)
            for _ in range(6_000):
                drive_stage5_group(clock, group, 1)
                state = ship_a.status(
                    TransferStatusRequest(accepted.value.transfer_id)
                ).value.state
                if state in (TransferState.COMPLETED, TransferState.FAILED):
                    break
            self.assertIs(TransferState.COMPLETED, state)
            received = received_payloads(roc)
            self.assertEqual(1, len(received))
            # digest가 아니라 길이·bytes 동등 연산으로 판정한다.
            self.assertEqual(len(payload), len(received[0]))
            self.assertEqual(payload, received[0])
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
