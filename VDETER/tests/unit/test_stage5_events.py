"""TC-STAGE5-006: bounded Service event publisher와 backpressure.

요구사항: `STAGE5-REQ-06` (`stage5_service_integration_baseline.md` §5.7, §9.4,
`DR-STAGE5-004`, `DR-STAGE5-011`, `DR-STAGE5-024`, `DR-STAGE5-028`)
"""

from __future__ import annotations

import unittest

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import EndpointHealth, ReceivedApplicationData, TransferState
from vdeter.common.errors import ServiceError, ServiceErrorCategory
from vdeter.common.events import EventPublished, EventSeverity, ServiceEventType
from vdeter.common.identifiers import DeterministicIdSource, PayloadFormat, StationId
from vdeter.common.results import Err, Ok, internal_error
from vdeter.common.errors import ErrorCategory, ErrorCode, Layer
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.service.models import Stage5ServicePolicy
from vdeter.service.publisher import Stage5EventPublisher

NOW = TimePoint.from_nanoseconds(1_000_000_000, ClockDomain.UTC, "stage5-test-utc")

PUBLIC_ERROR = ServiceError(
    code="TRANSFER_FAILED",
    category=ServiceErrorCategory.PROTOCOL,
    message="stage5 test error",
)


class _ScriptedSink:
    """전달 결과를 script로 제어하는 시험 sink."""

    def __init__(self):
        self.mode = "ok"
        self.published = []

    def publish(self, event):
        self.published.append(event)
        if self.mode == "ok":
            return Ok(EventPublished(event_id=event.event_id, delivered=True))
        if self.mode == "not_delivered":
            return Ok(EventPublished(event_id=event.event_id, delivered=False))
        if self.mode == "wrong_id":
            return Ok(EventPublished(event_id="0" * 8 + "-0000-4000-8000-000000000000", delivered=True))
        if self.mode == "raise":
            raise RuntimeError("sink crash")
        return internal_error(
            Layer.RUNTIME, ErrorCode.INTERNAL_ERROR, ErrorCategory.INTERNAL, "sink error"
        )


def _publisher(policy=None):
    sink = _ScriptedSink()
    publisher = Stage5EventPublisher(
        policy or Stage5ServicePolicy(), sink, DeterministicIdSource(99)
    )
    return publisher, sink


def _transfer_id(id_source=None):
    return (id_source or DeterministicIdSource(7)).new_transfer_id()


def _received(size: int) -> ReceivedApplicationData:
    return ReceivedApplicationData(
        original_source=StationId(111111111),
        original_destination=Addressed(StationId(333333333)),
        payload_format=PayloadFormat("test.stage0.opaque"),
        payload=bytes(size),
        received_at=NOW,
    )


class Stage5PublisherReservationTest(unittest.TestCase):
    def test_TC_STAGE5_006_lifecycle_reserves_health2_error1_stopped2(self):
        publisher, _sink = _publisher()
        self.assertTrue(publisher.reserve_lifecycle().is_ok)
        # health 2 + ERROR 1 + 현재 run SERVICE_STOPPED 1 = cleanup 4, STARTED 1 = general 1
        self.assertEqual(4, publisher.cleanup_occupancy())
        self.assertEqual(1, publisher.general_occupancy())

    def test_TC_STAGE5_006_transfer_reserves_two_state_tokens_and_terminal(self):
        publisher, _sink = _publisher()
        publisher.reserve_lifecycle()
        ids = DeterministicIdSource(11)
        keys = []
        for _ in range(8):
            transfer = ids.new_transfer_id()
            keys.append(transfer.value)
            self.assertTrue(publisher.reserve_transfer(transfer.value).is_ok)
        # 일반: STARTED 1 + 8×2 state tokens = 17, cleanup: 4 + 8 terminal = 12 (13/16 안)
        self.assertEqual(17, publisher.general_occupancy())
        self.assertEqual(12, publisher.cleanup_occupancy())
        self.assertLessEqual(publisher.cleanup_occupancy() + 1, 16)

    def test_TC_STAGE5_006_general_capacity_240_is_enforced(self):
        policy = Stage5ServicePolicy()
        publisher, _sink = _publisher(policy)
        for index in range(240):
            self.assertTrue(
                publisher.reserve_incoming(f"rx-{index}", 1).is_ok, index
            )
        self.assertEqual(240, publisher.general_occupancy())
        blocked = publisher.reserve_incoming("rx-overflow", 1)
        self.assertTrue(blocked.is_err)
        self.assertEqual(ErrorCode.SERVICE_RESOURCE_UNAVAILABLE, blocked.error.code)
        # cleanup reserve는 여전히 사용할 수 있다 (총 256 = 240 + 16).
        self.assertTrue(publisher.reserve_lifecycle().is_err)  # STARTED general slot이 없다.

    def test_TC_STAGE5_006_payload_budget_16mib_is_enforced(self):
        publisher, _sink = _publisher()
        budget = Stage5ServicePolicy().event_payload_budget_bytes
        self.assertTrue(publisher.reserve_incoming("rx-1", budget).is_ok)
        blocked = publisher.reserve_incoming("rx-2", 1)
        self.assertTrue(blocked.is_err)
        publisher.release_incoming("rx-1")
        publisher.release_incoming("rx-1")  # idempotent 해제
        self.assertEqual(0, publisher.payload_occupancy_bytes())
        self.assertTrue(publisher.reserve_incoming("rx-2", 1).is_ok)

    def test_TC_STAGE5_006_data_received_uses_reservation_or_general_slot(self):
        publisher, sink = _publisher()
        self.assertTrue(publisher.reserve_incoming("rx-1", 100).is_ok)
        self.assertEqual(1, publisher.general_occupancy())
        admitted = publisher.admit_data_received(_received(80), NOW, "rx-1")
        self.assertTrue(admitted.is_ok)
        publisher.pump()
        self.assertEqual(0, publisher.general_occupancy())
        self.assertEqual(0, publisher.payload_occupancy_bytes())
        self.assertEqual(ServiceEventType.DATA_RECEIVED, sink.published[-1].event_type)


class Stage5PublisherDeliveryTest(unittest.TestCase):
    def _start_run(self, publisher):
        publisher.reserve_lifecycle()

    def test_TC_STAGE5_006_fifo_order_and_severity(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        # 각 health commit 뒤 pump하면 coalesce 없이 FIFO로 STARTING 뒤 RUNNING이다.
        publisher.admit_health_changed(NOW, EndpointHealth.STOPPED, EndpointHealth.STARTING)
        publisher.pump()
        publisher.admit_health_changed(NOW, EndpointHealth.STARTING, EndpointHealth.RUNNING)
        publisher.pump()
        publisher.admit_service_started(NOW, "stage5-test")
        publisher.pump()
        types = [event.event_type for event in sink.published]
        self.assertEqual(
            [
                ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                ServiceEventType.SERVICE_STARTED,
            ],
            types,
        )
        self.assertEqual(EventSeverity.INFO, sink.published[0].severity)
        # DEGRADED는 WARNING, UNHEALTHY는 CRITICAL이다.
        publisher.admit_health_changed(NOW, EndpointHealth.RUNNING, EndpointHealth.DEGRADED)
        publisher.pump()
        self.assertEqual(EventSeverity.WARNING, sink.published[-1].severity)
        publisher.admit_health_changed(NOW, EndpointHealth.DEGRADED, EndpointHealth.UNHEALTHY)
        publisher.pump()
        self.assertEqual(EventSeverity.CRITICAL, sink.published[-1].severity)

    def test_TC_STAGE5_006_failure_results_and_head_retry_with_same_id(self):
        for mode in ("not_delivered", "wrong_id", "err", "raise"):
            with self.subTest(mode=mode):
                publisher, sink = _publisher()
                self._start_run(publisher)
                sink.mode = mode
                publisher.admit_health_changed(
                    NOW, EndpointHealth.STOPPED, EndpointHealth.STARTING
                )
                publisher.pump()
                # 최초 publish + 같은 event_id의 자동 재시도 3회 = 4회 시도.
                self.assertEqual(4, len(sink.published))
                self.assertEqual(1, len({event.event_id for event in sink.published}))
                self.assertTrue(publisher.delivery_failed)
                self.assertEqual(1, publisher.pending_count())
                # 실패한 head를 앞질러 전달하지 않는다.
                publisher.admit_service_started(NOW, "stage5-test")
                publisher.pump()
                self.assertEqual(4, len(sink.published))
                # 복구 신호에서 같은 event_id로 재개한다.
                failed_event_id = sink.published[0].event_id
                sink.mode = "ok"
                publisher.notify_sink_recovered()
                self.assertEqual(failed_event_id, sink.published[4].event_id)
                self.assertEqual(0, publisher.pending_count())

    def test_TC_STAGE5_006_degraded_callback_and_recovery(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        observed = []
        publisher.on_delivery_failed = lambda: observed.append("failed")
        publisher.on_queue_drained = lambda: observed.append("drained")
        sink.mode = "err"
        publisher.admit_service_started(NOW, "stage5-test")
        publisher.pump()
        self.assertEqual(["failed"], observed)
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        self.assertIn("drained", observed)

    def test_TC_STAGE5_006_state_chain_two_tokens_preserve_inflight_transition(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        transfer = _transfer_id()
        publisher.reserve_transfer(transfer.value)
        sink.mode = "err"
        publisher.admit_transfer_state(transfer.value, transfer, TransferState.QUEUED, NOW)
        publisher.pump()  # head 시도·실패 → delivery-failed
        self.assertTrue(publisher.delivery_failed)
        # 첫 event가 제출돼 변경할 수 없는 동안 두 번째 token이 최신 상태를 보존한다.
        self.assertTrue(
            publisher.admit_transfer_state(
                transfer.value, transfer, TransferState.TRANSMITTING, NOW
            ).is_ok
        )
        # 뒤이은 nonterminal 전이는 두 번째 token에 coalesce된다.
        self.assertTrue(
            publisher.admit_transfer_state(
                transfer.value, transfer, TransferState.WAITING_ACK, NOW
            ).is_ok
        )
        self.assertEqual(2, publisher.pending_count())
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        states = [
            event.transfer_state
            for event in sink.published
            if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
            and event.transfer_state is not None
        ]
        # 실패 4회 + 복구 후 같은 ID의 QUEUED 재전달, 뒤이어 coalesce된 최신 상태.
        self.assertEqual([TransferState.QUEUED] * 5 + [TransferState.WAITING_ACK], states)

    def test_TC_STAGE5_006_terminal_and_data_and_error_are_never_coalesced(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        transfer = _transfer_id()
        publisher.reserve_transfer(transfer.value)
        sink.mode = "err"
        publisher.admit_transfer_state(transfer.value, transfer, TransferState.QUEUED, NOW)
        publisher.pump()
        publisher.admit_data_received(_received(4), NOW)
        publisher.admit_error(NOW, PUBLIC_ERROR)
        publisher.admit_transfer_state(
            transfer.value, transfer, TransferState.COMPLETED, NOW
        )
        pending_before = publisher.pending_count()
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        delivered_types = [event.event_type for event in sink.published[4:]]
        self.assertEqual(pending_before, len(delivered_types))
        self.assertIn(ServiceEventType.DATA_RECEIVED, delivered_types)
        self.assertIn(ServiceEventType.ERROR, delivered_types)
        self.assertIn(ServiceEventType.TRANSFER_STATE_CHANGED, delivered_types)

    def test_TC_STAGE5_006_terminal_consumes_reserved_token_and_releases_chain(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        transfer = _transfer_id()
        publisher.reserve_transfer(transfer.value)
        general_before = publisher.general_occupancy()
        cleanup_before = publisher.cleanup_occupancy()
        publisher.admit_transfer_state(
            transfer.value, transfer, TransferState.COMPLETED, NOW
        )
        publisher.pump()
        # state-chain 2 token(일반)과 terminal token(cleanup)이 모두 해제됐다.
        self.assertEqual(general_before - 2, publisher.general_occupancy())
        self.assertEqual(cleanup_before - 1, publisher.cleanup_occupancy())

    def test_TC_STAGE5_006_second_error_while_pending_is_held_upstream(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        sink.mode = "err"
        first = publisher.admit_error(NOW, PUBLIC_ERROR)
        self.assertTrue(first.is_ok)
        publisher.pump()
        second = publisher.admit_error(NOW, PUBLIC_ERROR)
        # 두 번째 무제한 spill queue를 만들지 않는다.
        self.assertTrue(second.is_err)
        self.assertEqual(ErrorCode.SERVICE_RESOURCE_UNAVAILABLE, second.error.code)
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        # 전달 뒤 token이 갱신돼 다음 ERROR를 수락한다.
        self.assertTrue(publisher.admit_error(NOW, PUBLIC_ERROR).is_ok)

    def test_TC_STAGE5_006_health_net_transition_coalescing(self):
        publisher, sink = _publisher()
        self._start_run(publisher)
        sink.mode = "err"
        publisher.admit_health_changed(NOW, EndpointHealth.STOPPED, EndpointHealth.STARTING)
        publisher.pump()  # head 고착
        publisher.admit_health_changed(NOW, EndpointHealth.STARTING, EndpointHealth.RUNNING)
        publisher.admit_health_changed(NOW, EndpointHealth.RUNNING, EndpointHealth.DEGRADED)
        self.assertEqual(2, publisher.pending_count())
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        health_events = [
            event
            for event in sink.published
            if event.event_type is ServiceEventType.ENDPOINT_HEALTH_CHANGED
        ]
        final = health_events[-1]
        # net transition: 최초 event의 previous_health와 최신 commit의 health.
        self.assertEqual("STARTING", final.details.get("previous_health"))
        self.assertEqual("DEGRADED", final.details.get("health"))

    def test_TC_STAGE5_006_two_run_service_stopped_and_third_run_refusal(self):
        publisher, sink = _publisher()
        sink.mode = "err"
        # run 1
        self.assertTrue(publisher.reserve_lifecycle().is_ok)
        publisher.admit_service_stopped(NOW, "stage5-test")
        publisher.release_lifecycle()
        publisher.pump()
        # run 2: 이전 run의 SERVICE_STOPPED가 pending이어도 예약할 수 있다.
        self.assertTrue(publisher.reserve_lifecycle().is_ok)
        publisher.admit_service_stopped(NOW, "stage5-test")
        publisher.release_lifecycle()
        # run 3: SERVICE_STOPPED slot pair(2)가 소진돼 거부한다.
        refused = publisher.reserve_lifecycle()
        self.assertTrue(refused.is_err)
        self.assertEqual(ErrorCode.SERVICE_RESOURCE_UNAVAILABLE, refused.error.code)
        # 전달이 재개되면 다음 run을 예약할 수 있다.
        sink.mode = "ok"
        publisher.notify_sink_recovered()
        self.assertTrue(publisher.reserve_lifecycle().is_ok)

    def test_TC_STAGE5_006_concurrent_pump_is_single_flight(self):
        """동시 pump()가 중복 전달·예외 없이 FIFO를 정확히 한 번씩 소진한다 (§9.6)."""

        import threading
        import time

        publisher, _unused = _publisher()

        class _SlowSink:
            def __init__(self):
                self.delivered = []
                self.lock = threading.Lock()

            def publish(self, event):
                time.sleep(0.001)
                with self.lock:
                    self.delivered.append(event.event_id)
                return Ok(EventPublished(event_id=event.event_id, delivered=True))

        slow_sink = _SlowSink()
        publisher._sink = slow_sink
        publisher.reserve_lifecycle()
        id_source = DeterministicIdSource(41)
        admission_lock = threading.Lock()
        errors = []
        admitted = []

        def worker():
            try:
                for _ in range(15):
                    # bounded cleanup reserve(16)가 가득 차면 정당한 backpressure다.
                    # pump로 전달을 진행시키며 재시도한다.
                    for _attempt in range(400):
                        with admission_lock:
                            transfer = id_source.new_transfer_id()
                            reserved = publisher.reserve_transfer(transfer.value)
                            if reserved.is_ok:
                                self.assertTrue(
                                    publisher.admit_transfer_state(
                                        transfer.value,
                                        transfer,
                                        TransferState.COMPLETED,
                                        NOW,
                                    ).is_ok
                                )
                                admitted.append(transfer.value)
                        publisher.pump()
                        if reserved.is_ok:
                            break
                        time.sleep(0.001)
                    else:
                        raise AssertionError("reservation retry budget exhausted")
            except Exception as error:  # noqa: BLE001 - 시험 판정용
                errors.append(repr(error))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        publisher.pump()
        self.assertEqual([], errors)
        self.assertEqual(0, publisher.pending_count())
        # 각 event_id는 정확히 한 번 전달된다.
        self.assertEqual(len(slow_sink.delivered), len(set(slow_sink.delivered)))
        self.assertEqual(len(admitted), len(slow_sink.delivered))

    def test_TC_STAGE5_006_sink_reentrant_admission_does_not_deadlock(self):
        """sink callback이 전달 중 새 event를 admission해도 교착·재귀 없이 이어진다."""

        publisher, _unused = _publisher()

        class _ReentrantSink:
            def __init__(self, owner_publisher):
                self.publisher = owner_publisher
                self.delivered = []
                self.injected = False

            def publish(self, event):
                self.delivered.append(event.event_type)
                if not self.injected:
                    self.injected = True
                    # 전달 도중의 재진입 admission: single-flight pump가 요청만 남기고
                    # 반환한 뒤 실행 중인 pump가 이어받아야 한다.
                    admitted = self.publisher.admit_data_received(
                        _received(3), NOW
                    )
                    assert admitted.is_ok, admitted
                    self.publisher.pump()
                return Ok(EventPublished(event_id=event.event_id, delivered=True))

        reentrant = _ReentrantSink(publisher)
        publisher._sink = reentrant
        publisher.reserve_lifecycle()
        self.assertTrue(publisher.admit_service_started(NOW, "stage5-test").is_ok)
        publisher.pump()
        self.assertEqual(0, publisher.pending_count())
        self.assertEqual(
            [ServiceEventType.SERVICE_STARTED, ServiceEventType.DATA_RECEIVED],
            reentrant.delivered,
        )

    def test_TC_STAGE5_006_event_ids_are_unique_for_endpoint_lifetime(self):
        publisher, sink = _publisher()
        publisher.reserve_lifecycle()
        publisher.admit_health_changed(NOW, EndpointHealth.STOPPED, EndpointHealth.STARTING)
        publisher.admit_service_started(NOW, "stage5-test")
        publisher.admit_service_stopped(NOW, "stage5-test")
        publisher.release_lifecycle()
        publisher.reserve_lifecycle()
        publisher.admit_health_changed(NOW, EndpointHealth.STOPPED, EndpointHealth.STARTING)
        publisher.pump()
        ids = [event.event_id for event in sink.published]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
