"""TC-STAGE5-019: 수신 실패·상태·오류 event.

요구사항: `STAGE5-REQ-17`, `STAGE5-REQ-19`
(`stage5_service_integration_baseline.md` §6.1.1, §8.3, §9.5)
"""

from __future__ import annotations

import unittest

from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    received_payloads,
)
from vdeter.common.addressing import (
    Addressed,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.service import SubmitTransferRequest
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat
from vdeter.common.time import ClockDomain, TimePoint

RECEIVED_AT = TimePoint.from_nanoseconds(1_000_000_000, ClockDomain.UTC, "stage5-utc")


def _route(destination_station=MASS_A_STATION_ID):
    return RouteAddresses(
        original_source=OriginalSource(MASS_B_STATION_ID),
        original_destination=OriginalDestination(Addressed(destination_station)),
    )


class Stage5RxFailureTest(unittest.TestCase):
    """검증·재조립을 마친 Presentation bytes 경계의 수신 실패 주입.

    `_deliver_presentation_bytes`는 Stage 4 delivery gate가 사용하는 내부 관찰
    지점이며 공개 Service API가 아니다 (`stage5` §8.5).
    """

    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = build_stage5_software_group()
        started = cls.group.start()
        if started.is_err:
            raise AssertionError(started.error)
        cls.ship_a = cls.group.by_station()[MASS_A_STATION_ID.value]

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    def _deliver(self, data: bytes, destination_station=MASS_A_STATION_ID):
        before_data = len(received_payloads(self.ship_a))
        before_events = len(self.ship_a.dependencies.service_event_sink.events())
        self.ship_a._last_rx_error_signature = None  # 각 case를 독립 관찰한다.
        result = self.ship_a._deliver_presentation_bytes(
            _route(destination_station), data, RECEIVED_AT
        )
        new_events = self.ship_a.dependencies.service_event_sink.events()[before_events:]
        delivered = received_payloads(self.ship_a)[before_data:]
        return result, delivered, new_events

    def _assert_rejected(self, data, expected_public_code, case):
        result, delivered, events = self._deliver(data)
        self.assertTrue(result.is_err, case)
        self.assertEqual([], delivered, case)
        errors = [
            event for event in events if event.event_type is ServiceEventType.ERROR
        ]
        self.assertEqual(1, len(errors), (case, [e.event_type for e in events]))
        self.assertIsNone(errors[0].transfer_id, case)
        self.assertEqual(expected_public_code, errors[0].error.code, case)
        self.assertFalse(
            any(event.event_type is ServiceEventType.DATA_RECEIVED for event in events),
            case,
        )

    def test_TC_STAGE5_019_common_truncation_zero_and_one_byte(self):
        for data in (b"", b"\x0f"):
            with self.subTest(size=len(data)):
                self._assert_rejected(data, "TRANSFER_FAILED", f"truncated-{len(data)}")

    def test_TC_STAGE5_019_opaque_prefix_boundaries_2_3_4_bytes(self):
        for data in (b"\x0f\xff", b"\x0f\xff\xff", b"\x0f\xff\xff\xff"):
            with self.subTest(size=len(data)):
                self._assert_rejected(data, "TRANSFER_FAILED", f"opaque-{len(data)}")

    def test_TC_STAGE5_019_x509_structure_errors(self):
        cases = {
            "truncated-header": b"\x00\x01\x01",
            "reserved-type": b"\x00\x01\x01" + bytes(6) + b"\x03" + b"\x00\x01\x30",
            "length-overrun": b"\x00\x01\x01" + bytes(6) + b"\x00" + b"\x00\xff" + b"\x30\x00",
        }
        for case, data in cases.items():
            with self.subTest(case=case):
                self._assert_rejected(data, "TRANSFER_FAILED", case)

    def test_TC_STAGE5_019_ais_header_and_length_errors(self):
        base = bytes((0x00, 0x07, 0x04))
        cases = {
            "truncated-22bit-header": b"\x00\x07",
            "wrong-length-83": base + bytes(80),
            "misaligned-85": base + bytes(82),
        }
        for case, data in cases.items():
            with self.subTest(case=case):
                self._assert_rejected(data, "TRANSFER_FAILED", case)

    def test_TC_STAGE5_019_unknown_wire_key_is_unsupported(self):
        result, delivered, events = self._deliver(b"\x12\x34\x56\x78")
        self.assertTrue(result.is_err)
        self.assertEqual([], delivered)
        errors = [e for e in events if e.event_type is ServiceEventType.ERROR]
        self.assertEqual(1, len(errors))
        self.assertEqual("UNSUPPORTED_REQUEST", errors[0].error.code)
        self.assertIsNone(errors[0].transfer_id)

    def test_TC_STAGE5_019_unknown_vpfi7_message_format_is_unsupported(self):
        data = b"\x00\x07" + bytes(((2 << 2),)) + bytes(81)
        result, delivered, events = self._deliver(data)
        self.assertTrue(result.is_err)
        errors = [e for e in events if e.event_type is ServiceEventType.ERROR]
        self.assertEqual("UNSUPPORTED_REQUEST", errors[0].error.code)

    def test_TC_STAGE5_019_other_destination_data_is_not_delivered(self):
        wire = b"\x0f\xff\xff\xff\x01\x02\x03"
        result, delivered, events = self._deliver(
            wire, destination_station=ROC_STATION_ID
        )
        self.assertTrue(result.is_err)
        self.assertEqual([], delivered)
        self.assertFalse(
            any(event.event_type is ServiceEventType.DATA_RECEIVED for event in events)
        )

    def test_TC_STAGE5_019_valid_data_still_delivers_after_failures(self):
        wire = b"\x0f\xff\xff\xff\xaa\xbb"
        result, delivered, events = self._deliver(wire)
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual([b"\xaa\xbb"], delivered)
        data_events = [
            event for event in events if event.event_type is ServiceEventType.DATA_RECEIVED
        ]
        self.assertEqual(1, len(data_events))
        # `received_at`은 Service admission UTC이며 event의 `occurred_at`과 같다.
        self.assertEqual(
            data_events[0].occurred_at, data_events[0].received_data.received_at
        )


class Stage5RxReservationFailureTest(unittest.TestCase):
    def test_TC_STAGE5_019_incoming_session_reservation_failure_degrades_endpoint(self):
        """수신 session slot 예약 실패는 조용히 무시되지 않는다 (§9.4)."""

        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        try:
            from vdeter.common.contracts.service import EndpointHealth
            from vdeter.common.events import ServiceEventType
            from vdeter.common.identifiers import SessionHandle
            from vdeter.mac.models import RxSessionContext

            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            # 일반 event 영역을 소진시켜 예약이 실패할 수밖에 없게 만든다.
            for index in range(ship_a.policy.event_general_capacity):
                if ship_a.publisher.reserve_incoming(f"fill-{index}", 1).is_err:
                    break
            # MAC state에 수신 session이 수락된 상황을 주입한다 (내부 관찰 지점).
            session = RxSessionContext(
                session_handle=SessionHandle("3c9a7d6e-5b4f-4c3d-8e2f-1a0b9c8d7e6f"),
                route=_route(),
                peer_station_id_value=MASS_B_STATION_ID.value,
                logical_channel_rx=2,
            )
            ship_a.stage4.mac.repository.commit(
                ship_a.stage4.mac.state.with_rx_session(session)
            )
            ship_a._sync_rx_reservations()
            # 구조화 ERROR + DEGRADED로 알리고 예약 목록에는 넣지 않는다.
            self.assertIs(EndpointHealth.DEGRADED, ship_a.health)
            self.assertNotIn(
                session.session_handle.value, ship_a._rx_reserved_sessions
            )
            errors = [
                event
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.ERROR
            ]
            self.assertEqual(1, len(errors))
            self.assertEqual("RESOURCE_UNAVAILABLE", errors[0].error.code)
            # 반복 sync가 같은 session으로 ERROR를 중복 생성하지 않는다.
            ship_a._sync_rx_reservations()
            errors_after = [
                event
                for event in ship_a.dependencies.service_event_sink.events()
                if event.event_type is ServiceEventType.ERROR
            ]
            self.assertEqual(1, len(errors_after))
            # DEGRADED에서 신규 submit은 거부된다 (§9.7).
            blocked = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=PayloadFormat("test.stage0.opaque"),
                    payload=b"\x01",
                    ack_required=False,
                )
            )
            self.assertTrue(blocked.is_err)
            self.assertEqual("RESOURCE_UNAVAILABLE", blocked.error.code)
            # capacity 회복 뒤 다음 sync에서 예약이 성공한다.
            for index in range(ship_a.policy.event_general_capacity):
                ship_a.publisher.release_incoming(f"fill-{index}")
            ship_a._sync_rx_reservations()
            self.assertIn(session.session_handle.value, ship_a._rx_reserved_sessions)
        finally:
            group.stop()


class Stage5RxDeliveryFailureBackpressureTest(unittest.TestCase):
    def test_TC_STAGE5_019_queue_saturation_rejects_delivery_without_silent_loss(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok)
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            # 일반 event 영역을 가득 채운다 (backpressure 없는 traffic 모사).
            policy = ship_a.policy
            for index in range(policy.event_general_capacity):
                reserved = ship_a.publisher.reserve_incoming(f"fill-{index}", 1)
                if reserved.is_err:
                    break
            result = ship_a._deliver_presentation_bytes(
                _route(), b"\x0f\xff\xff\xff\x01", RECEIVED_AT
            )
            # 조용한 유실이나 성공 전달로 기록하지 않는다: 전달 실패 + DEGRADED.
            self.assertTrue(result.is_err)
            self.assertEqual([], received_payloads(ship_a))
            from vdeter.common.contracts.service import EndpointHealth

            self.assertIs(EndpointHealth.DEGRADED, ship_a.health)
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
