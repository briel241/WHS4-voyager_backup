"""TC-STAGE5-003: submit 2단계 admission transaction과 상한.

요구사항: `STAGE5-REQ-03` (`stage5_service_integration_baseline.md` §5.4, §9.6,
`DR-STAGE5-002`, `DR-STAGE5-017`, `DR-STAGE5-021`)
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    build_stage5_software_group,
    drive_stage5_group,
    start_stage5_group,
)
from vdeter.common.addressing import Addressed, Broadcast
from vdeter.common.contracts.service import (
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import DeterministicIdSource, PayloadFormat, TransferId

OPAQUE = PayloadFormat("test.stage0.opaque")
X509 = PayloadFormat("x509.certificate_report")
AIS = PayloadFormat("ais.authentication_integrity.scheme1")


def _started_group(**kwargs):
    clock, group = build_stage5_software_group(**kwargs)
    started = group.start()
    if started.is_err:
        raise AssertionError(started.error)
    return clock, group


class _EncodeProxy:
    """Phase B를 제어하기 위한 시험용 Presentation Adapter proxy.

    production 정상 graph에는 들어가지 않으며, 시작이 끝난 endpoint에 시험이 주입하는
    내부 관찰 지점이다.
    """

    def __init__(self, inner, *, hold_event=None, mutate=False):
        self._inner = inner
        self._hold_event = hold_event
        self._mutate = mutate
        self.encode_calls = 0

    def registry_fingerprint(self):
        return self._inner.registry_fingerprint()

    def encoded_size_bound(self, payload_format, application_length):
        return self._inner.encoded_size_bound(payload_format, application_length)

    def encode_payload(self, request):
        self.encode_calls += 1
        if self._hold_event is not None:
            self._hold_event.wait(timeout=10)
        result = self._inner.encode_payload(request)
        if self._mutate and result.is_ok:
            from dataclasses import replace

            return type(result)(replace(result.value, data=result.value.data + b"\x00"))
        return result

    def decode_payload(self, request):
        return self._inner.decode_payload(request)


class Stage5SubmitBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock, cls.group = _started_group()
        cls.ship_a = cls.group.by_station()[MASS_A_STATION_ID.value]
        cls.roc = cls.group.by_station()[ROC_STATION_ID.value]

    @classmethod
    def tearDownClass(cls):
        cls.group.stop()

    def _submit(self, endpoint, payload, *, destination=None, payload_format=OPAQUE, ack=False):
        return endpoint.submit(
            SubmitTransferRequest(
                destination=destination or Addressed(MASS_B_STATION_ID),
                payload_format=payload_format,
                payload=payload,
                ack_required=ack,
            )
        )

    def test_TC_STAGE5_003_empty_payload_is_invalid(self):
        result = self._submit(self.ship_a, b"")
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)

    def test_TC_STAGE5_003_65531_boundary(self):
        over = self._submit(self.ship_a, bytes(65_532))
        self.assertTrue(over.is_err)
        self.assertEqual("INVALID_REQUEST", over.error.code)
        at_limit = self._submit(self.ship_a, bytes(65_531))
        self.assertTrue(at_limit.is_ok, getattr(at_limit, "error", None))
        self.assertIs(TransferState.QUEUED, at_limit.value.state)
        cancel_result = self.ship_a.cancel(
            __import__("vdeter.common.contracts.service", fromlist=["CancelTransferRequest"])
            .CancelTransferRequest(at_limit.value.transfer_id)
        )
        self.assertTrue(cancel_result.is_ok)

    def test_TC_STAGE5_003_broadcast_ack_is_rejected_by_contract(self):
        with self.assertRaises(InvalidValueError):
            SubmitTransferRequest(
                destination=Broadcast(),
                payload_format=OPAQUE,
                payload=b"\x01",
                ack_required=True,
            )

    def test_TC_STAGE5_003_broadcast_over_34_encoded_is_unsupported(self):
        # opaque 31 bytes -> encoded 35 > 34: broadcast Data Session은 미지원.
        over = self._submit(self.roc, bytes(31), destination=Broadcast())
        self.assertTrue(over.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", over.error.code)
        # opaque 30 bytes -> encoded 34: Type 93 broadcast 한도 안.
        ok = self._submit(self.roc, bytes(30), destination=Broadcast())
        self.assertTrue(ok.is_ok, getattr(ok, "error", None))
        from vdeter.common.contracts.service import CancelTransferRequest

        self.roc.cancel(CancelTransferRequest(ok.value.transfer_id))

    def test_TC_STAGE5_003_mass_broadcast_is_unsupported(self):
        result = self._submit(self.ship_a, b"\x01", destination=Broadcast())
        self.assertTrue(result.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)

    def test_TC_STAGE5_003_self_destination_is_invalid(self):
        result = self._submit(self.ship_a, b"\x01", destination=Addressed(MASS_A_STATION_ID))
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)

    def test_TC_STAGE5_003_non_member_destination_is_unsupported(self):
        from vdeter.common.identifiers import StationId

        result = self._submit(self.ship_a, b"\x01", destination=Addressed(StationId(999999999)))
        self.assertTrue(result.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)

    def test_TC_STAGE5_003_unknown_and_reserved_formats_are_unsupported(self):
        for key in ("electronic_chart.data", "no.such.format"):
            with self.subTest(payload_format=key):
                result = self._submit(self.ship_a, b"\x01", payload_format=PayloadFormat(key))
                self.assertTrue(result.is_err)
                self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)

    def test_TC_STAGE5_003_ais_requires_exact_82_bytes(self):
        result = self._submit(self.ship_a, bytes(81), payload_format=AIS)
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)

    def test_TC_STAGE5_003_encode_error_rolls_back_without_public_id(self):
        events_before = len(self.ship_a.dependencies.service_event_sink.events())
        nonterminal_before = self.ship_a._nonterminal_count_locked()
        result = self._submit(self.ship_a, b"\xff" * 20, payload_format=X509)
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)
        self.assertIsNone(result.error.transfer_id)
        self.assertEqual(nonterminal_before, self.ship_a._nonterminal_count_locked())
        state_events = [
            event
            for event in self.ship_a.dependencies.service_event_sink.events()[events_before:]
            if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
        ]
        self.assertEqual([], state_events)


class Stage5SubmitQueueLimitTest(unittest.TestCase):
    def test_TC_STAGE5_003_eight_nonterminal_limit_including_queue(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            accepted = []
            # TBB 획득 전이므로 모두 Service FIFO에서 QUEUED로 남는다.
            for index in range(8):
                result = ship_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_B_STATION_ID),
                        payload_format=OPAQUE,
                        payload=bytes((index + 1,)),
                        ack_required=False,
                    )
                )
                self.assertTrue(result.is_ok, getattr(result, "error", None))
                self.assertIs(TransferState.QUEUED, result.value.state)
                accepted.append(result.value)
            ninth = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x09",
                    ack_required=False,
                )
            )
            self.assertTrue(ninth.is_err)
            self.assertEqual("RESOURCE_UNAVAILABLE", ninth.error.code)
            self.assertTrue(ninth.error.retryable)
            # queue ordinal: accepted_at 오름차순, 동률이면 transfer_id 오름차순.
            fifo = [entry.transfer_id.value for entry in ship_a._service_fifo]
            ordered = sorted(
                ship_a._service_fifo,
                key=lambda entry: (entry.accepted_at_nanoseconds, entry.transfer_id.value),
            )
            self.assertEqual([entry.transfer_id.value for entry in ordered], fifo)
        finally:
            group.stop()

    def test_TC_STAGE5_003_provisional_reservation_counts_toward_limit(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            hold = threading.Event()
            proxy = _EncodeProxy(ship_a._presentation, hold_event=hold)
            ship_a._presentation = proxy
            blocked_result = {}

            def blocked_submit():
                blocked_result["result"] = ship_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_B_STATION_ID),
                        payload_format=OPAQUE,
                        payload=b"\x01",
                        ack_required=False,
                    )
                )

            worker = threading.Thread(target=blocked_submit)
            worker.start()
            for _ in range(200):
                if proxy.encode_calls:
                    break
                threading.Event().wait(0.01)
            # provisional 1개가 상한에 포함되므로 7개까지만 추가 수락된다.
            for index in range(7):
                result = ship_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_B_STATION_ID),
                        payload_format=OPAQUE,
                        payload=bytes((index + 2,)),
                        ack_required=False,
                    )
                )
                self.assertTrue(result.is_ok, getattr(result, "error", None))
            ninth = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x0a",
                    ack_required=False,
                )
            )
            self.assertTrue(ninth.is_err)
            self.assertEqual("RESOURCE_UNAVAILABLE", ninth.error.code)
            hold.set()
            worker.join(timeout=10)
            self.assertTrue(blocked_result["result"].is_ok)
        finally:
            group.stop()


class Stage5SubmitTransactionTest(unittest.TestCase):
    def test_TC_STAGE5_003_phase_c_serialization_orders_admission(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            hold = threading.Event()
            slow_proxy = _EncodeProxy(ship_a._presentation, hold_event=hold)
            real = ship_a._presentation
            ship_a._presentation = slow_proxy
            slow_result = {}

            def slow_submit():
                slow_result["result"] = ship_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_B_STATION_ID),
                        payload_format=OPAQUE,
                        payload=b"\x51",
                        ack_required=False,
                    )
                )

            worker = threading.Thread(target=slow_submit)
            worker.start()
            for _ in range(200):
                if slow_proxy.encode_calls:
                    break
                threading.Event().wait(0.01)
            # 앞선 provisional encode가 뒤의 정상 Phase C를 blocking하지 않는다.
            ship_a._presentation = real
            fast = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x52",
                    ack_required=False,
                )
            )
            self.assertTrue(fast.is_ok)
            hold.set()
            worker.join(timeout=10)
            slow = slow_result["result"]
            self.assertTrue(slow.is_ok)
            # Phase C commit 직렬화가 admission 순서를 정한다. fast의 Phase C가 먼저
            # commit됐으므로 accepted_at이 더 이르거나 같고, 같은 공개 시각이면
            # transfer_id 오름차순 tie-break로 deterministic FIFO를 만든다 (§9.6).
            self.assertLessEqual(
                fast.value.accepted_at.total_nanoseconds(),
                slow.value.accepted_at.total_nanoseconds(),
            )
            fifo = [entry.transfer_id.value for entry in ship_a._service_fifo]
            expected = sorted(
                (
                    (fast.value.accepted_at.total_nanoseconds(), fast.value.transfer_id.value),
                    (slow.value.accepted_at.total_nanoseconds(), slow.value.transfer_id.value),
                )
            )
            self.assertEqual([item[1] for item in expected], fifo)
        finally:
            group.stop()

    def test_TC_STAGE5_003_actual_length_mismatch_rolls_back(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            ship_a._presentation = _EncodeProxy(ship_a._presentation, mutate=True)
            result = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x01\x02",
                    ack_required=False,
                )
            )
            self.assertTrue(result.is_err)
            self.assertEqual("INVALID_REQUEST", result.error.code)
            self.assertIsNone(result.error.transfer_id)
            self.assertEqual(0, ship_a._nonterminal_count_locked())
        finally:
            group.stop()

    def test_TC_STAGE5_003_stop_during_encode_rolls_back_without_id(self):
        clock, group = _started_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        hold = threading.Event()
        proxy = _EncodeProxy(ship_a._presentation, hold_event=hold)
        ship_a._presentation = proxy
        blocked_result = {}

        def blocked_submit():
            blocked_result["result"] = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x61",
                    ack_required=False,
                )
            )

        worker = threading.Thread(target=blocked_submit)
        worker.start()
        for _ in range(200):
            if proxy.encode_calls:
                break
            threading.Event().wait(0.01)
        # encode 중 stop이 admission을 닫고 generation을 무효화한다.
        stopped = ship_a.stop(ServiceStopRequest(reason="stage5 test stop"))
        self.assertTrue(stopped.is_ok, getattr(stopped, "error", None))
        hold.set()
        worker.join(timeout=10)
        result = blocked_result["result"]
        self.assertTrue(result.is_err)
        self.assertEqual("SERVICE_NOT_STARTED", result.error.code)
        self.assertIsNone(result.error.transfer_id)
        # 소비된 provisional UUID는 재사용되지 않는다.
        consumed = set(ship_a._issued_transfer_ids)
        next_id = ship_a.dependencies.id_source.new_transfer_id()
        self.assertNotIn(next_id.value, consumed)
        group.stop()

    def test_TC_STAGE5_003_stop_releases_provisional_event_tokens_immediately(self):
        """stop은 codec 종료를 기다리지 않고 provisional token을 해제한다 (§5.4)."""

        clock, group = _started_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        hold = threading.Event()
        proxy = _EncodeProxy(ship_a._presentation, hold_event=hold)
        ship_a._presentation = proxy
        worker = threading.Thread(
            target=lambda: ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x62",
                    ack_required=False,
                )
            )
        )
        worker.start()
        for _ in range(200):
            if proxy.encode_calls:
                break
            threading.Event().wait(0.01)
        # encode가 아직 차단된 상태: provisional reservation이 occupancy를 점유한다.
        self.assertGreaterEqual(ship_a.publisher.general_occupancy(), 2)
        self.assertGreaterEqual(ship_a.publisher.cleanup_occupancy(), 1)
        stopped = ship_a.stop(ServiceStopRequest(reason="token release"))
        self.assertTrue(stopped.is_ok, getattr(stopped, "error", None))
        # codec이 아직 반환하지 않았어도 token은 이미 해제돼 새 실행의 capacity를
        # 잠식하지 않는다.
        self.assertEqual(0, ship_a.publisher.general_occupancy())
        self.assertEqual(0, ship_a.publisher.cleanup_occupancy())
        hold.set()
        worker.join(timeout=10)
        # 뒤늦은 Phase C rollback의 이중 해제는 idempotent하다.
        self.assertEqual(0, ship_a.publisher.general_occupancy())
        self.assertEqual(0, ship_a.publisher.cleanup_occupancy())
        group.stop()

    def test_TC_STAGE5_003_repeated_id_is_rejected_atomically_without_id(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            first = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x71",
                    ack_required=False,
                )
            )
            self.assertTrue(first.is_ok)
            fixed = first.value.transfer_id
            with patch.object(
                DeterministicIdSource, "new_transfer_id", lambda self: TransferId(fixed.value)
            ):
                result = ship_a.submit(
                    SubmitTransferRequest(
                        destination=Addressed(MASS_B_STATION_ID),
                        payload_format=OPAQUE,
                        payload=b"\x72",
                        ack_required=False,
                    )
                )
            self.assertTrue(result.is_err)
            self.assertEqual("INTERNAL_ERROR", result.error.code)
            self.assertIsNone(result.error.transfer_id)
            # 기존 record는 훼손되지 않는다.
            status = ship_a.status(TransferStatusRequest(fixed))
            self.assertTrue(status.is_ok)
            self.assertIs(TransferState.QUEUED, status.value.state)
        finally:
            group.stop()

    def test_TC_STAGE5_003_event_tokens_reserved_per_accepted_transfer(self):
        clock, group = _started_group()
        try:
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            general_before = ship_a.publisher.general_occupancy()
            cleanup_before = ship_a.publisher.cleanup_occupancy()
            result = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=b"\x81",
                    ack_required=False,
                )
            )
            self.assertTrue(result.is_ok)
            # 일반 state-chain 2 token 중 1개는 QUEUED event로 materialize된 뒤 즉시
            # 전달·반환됐다. terminal token 1개는 cleanup reserve에 남는다.
            self.assertEqual(general_before + 2, ship_a.publisher.general_occupancy())
            self.assertEqual(cleanup_before + 1, ship_a.publisher.cleanup_occupancy())
        finally:
            group.stop()


class Stage5SubmitDispatchBoundaryTest(unittest.TestCase):
    def test_TC_STAGE5_003_short_data_33_34_boundary_selects_procedure(self):
        clock, group = build_stage5_software_group()
        try:
            start_stage5_group(clock, group)
            ship_a = group.by_station()[MASS_A_STATION_ID.value]
            from vdeter.mac.models import TransferRole

            # encoded 33 bytes (ACK 있음 한도): Type 92 Short Data.
            short = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=bytes(29),
                    ack_required=True,
                )
            )
            self.assertTrue(short.is_ok)
            drive_stage5_group(clock, group, 4)
            context = ship_a.stage4.mac.state.transfer(short.value.transfer_id)
            self.assertIsNotNone(context)
            self.assertIs(TransferRole.LOCAL_SHORT_DATA, context.role)
            for _ in range(120):
                drive_stage5_group(clock, group, 1)
                status = ship_a.status(TransferStatusRequest(short.value.transfer_id))
                if status.value.state is TransferState.COMPLETED:
                    break
            # encoded 34 bytes (ACK 있음 한도 초과): addressed Data Session.
            session = ship_a.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_STATION_ID),
                    payload_format=OPAQUE,
                    payload=bytes(30),
                    ack_required=True,
                )
            )
            self.assertTrue(session.is_ok)
            drive_stage5_group(clock, group, 4)
            context = ship_a.stage4.mac.state.transfer(session.value.transfer_id)
            self.assertIsNotNone(context)
            self.assertIs(TransferRole.LOCAL_DATA_SESSION, context.role)
        finally:
            group.stop()


if __name__ == "__main__":
    unittest.main()
