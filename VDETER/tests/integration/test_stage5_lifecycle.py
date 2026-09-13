"""TC-STAGE5-002: group/start/rollback/stop/restart lifecycle.

요구사항: `STAGE5-REQ-02` (`stage5_service_integration_baseline.md` §4.1, §5.2, §5.3,
§8.1, §8.4, `DR-STAGE5-016`, `DR-STAGE5-024`)
"""

from __future__ import annotations

import unittest

from tests.support.stage4 import (
    ROLE_BY_STATION,
    build_test_signer,
    crypto_dependencies,
    default_type0_override,
    deployment_profile,
)
from tests.support.stage5 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    MEMBER_STATION_IDS,
    ROC_STATION_ID,
    STATION_ENDPOINT_IDS,
    build_stage5_medium,
    build_stage5_registries,
    build_stage5_software_group,
    stage5_configuration,
)
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    EndpointHealth,
    ServiceRole,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.errors import ErrorCode
from vdeter.common.events import (
    EventPublished,
    RecordingInternalEventSink,
    ServiceEventType,
)
from vdeter.common.identifiers import DeterministicIdSource, PayloadFormat
from vdeter.common.random_source import SeededRandomSource
from vdeter.common.results import Ok
from vdeter.common.time import TestTimeSource
from vdeter.radio.software import SoftwareRadioAdapter
from vdeter.radio.software.faults import SoftwareRadioFault
from vdeter.runtime.wiring import EndpointDependencies, InternalEventBus, build_stage5_group
from vdeter.security.dependencies import E2eSecurityDependencies

OPAQUE = PayloadFormat("test.stage0.opaque")


class _SwitchableSink:
    """전달 성공 여부를 시험이 제어하는 sink."""

    def __init__(self):
        self.failing = False
        self.published = []

    def publish(self, event):
        self.published.append(event)
        if self.failing:
            return Ok(EventPublished(event_id=event.event_id, delivered=False))
        return Ok(EventPublished(event_id=event.event_id, delivered=True))


def _start_request(endpoint, **kwargs):
    configuration = endpoint.dependencies.configuration.endpoint
    return ServiceStartRequest(
        local_station_id=configuration.station_id, role=configuration.role, **kwargs
    )


class Stage5GroupCompositionTest(unittest.TestCase):
    def _build_group_with_roles(self, role_overrides):
        registries = build_stage5_registries()
        from tests.support.stage4 import (
            RUN_START_MONOTONIC_NANOSECONDS,
            RUN_START_UTC_NANOSECONDS,
        )

        clock = TestTimeSource(
            RUN_START_UTC_NANOSECONDS,
            RUN_START_MONOTONIC_NANOSECONDS,
            utc_clock_id="stage5-utc",
            monotonic_clock_id="stage5-monotonic",
        )
        medium = build_stage5_medium()
        signer, certificate_der, fingerprint = build_test_signer()
        descriptors = []
        crypto_by_station = {}
        for index, station_id in enumerate(MEMBER_STATION_IDS):
            role = role_overrides.get(station_id.value, ROLE_BY_STATION[station_id.value])
            endpoint_id = STATION_ENDPOINT_IDS[station_id.value]
            configuration = stage5_configuration(station_id, role, endpoint_id)
            internal = InternalEventBus(RecordingInternalEventSink())
            dependencies = EndpointDependencies(
                configuration=configuration,
                time_source=clock,
                random_source=SeededRandomSource(800 + index),
                id_source=DeterministicIdSource(5000 + index),
                radio=SoftwareRadioAdapter(
                    clock,
                    DeterministicIdSource(5100 + index),
                    internal,
                    endpoint_id=endpoint_id,
                    medium=medium,
                ),
                registries=registries,
                internal_event_sink=internal,
                service_event_sink=_SwitchableSink(),
            )
            deployment = deployment_profile(
                expected_fingerprint=fingerprint, type0_override=default_type0_override()
            )
            descriptors.append((dependencies, deployment))
            crypto_by_station[station_id.value] = crypto_dependencies(
                signer, certificate_der, is_control=role is ServiceRole.CONTROL
            )
        return build_stage5_group(
            tuple(descriptors), crypto_by_station=crypto_by_station,
            e2e_security_by_station={
                item[0].configuration.endpoint.station_id.value: E2eSecurityDependencies.disabled()
                for item in descriptors
            },
        )

    def test_TC_STAGE5_002_group_requires_exactly_one_control_two_ships(self):
        two_controls = self._build_group_with_roles(
            {MASS_A_STATION_ID.value: ServiceRole.CONTROL}
        )
        self.assertTrue(two_controls.is_err)
        three_ships = self._build_group_with_roles(
            {ROC_STATION_ID.value: ServiceRole.SHIP}
        )
        self.assertTrue(three_ships.is_err)

    def test_TC_STAGE5_002_group_start_order_ships_then_roc(self):
        clock, group = build_stage5_software_group()
        started = group.start()
        self.assertTrue(started.is_ok, getattr(started, "error", None))
        order = [
            endpoint.dependencies.configuration.endpoint.endpoint_id
            for endpoint in started.value
        ]
        self.assertEqual(["stage5-ship-a", "stage5-ship-b", "stage5-roc"], order)
        group.stop()


class Stage5StartStopSemanticsTest(unittest.TestCase):
    def test_TC_STAGE5_002_started_and_stopped_share_commit_utc_with_events(self):
        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        started = ship_a.start(_start_request(ship_a))
        self.assertTrue(started.is_ok)
        self.assertIs(EndpointHealth.RUNNING, started.value.health)
        events = ship_a.dependencies.service_event_sink.events()
        types = [event.event_type for event in events]
        self.assertEqual(
            [
                ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                ServiceEventType.SERVICE_STARTED,
            ],
            types,
        )
        self.assertEqual("STARTING", events[0].details.get("health"))
        self.assertEqual("STOPPED", events[0].details.get("previous_health"))
        self.assertEqual("RUNNING", events[1].details.get("health"))
        started_event = events[2]
        self.assertEqual(started.value.started_at, started_event.occurred_at)
        self.assertEqual(
            "stage5-ship-a", started_event.details.get("endpoint_id")
        )
        again = ship_a.start(_start_request(ship_a))
        self.assertTrue(again.is_err)
        self.assertEqual("SERVICE_ALREADY_STARTED", again.error.code)

        stopped = ship_a.stop(ServiceStopRequest(reason="tc-stage5-002"))
        self.assertTrue(stopped.is_ok)
        self.assertIs(EndpointHealth.STOPPED, stopped.value.health)
        events = ship_a.dependencies.service_event_sink.events()
        self.assertIs(ServiceEventType.SERVICE_STOPPED, events[-1].event_type)
        self.assertEqual(stopped.value.stopped_at, events[-1].occurred_at)
        repeated = ship_a.stop(ServiceStopRequest())
        self.assertTrue(repeated.is_err)
        self.assertEqual("SERVICE_NOT_STARTED", repeated.error.code)

    def test_TC_STAGE5_002_nonempty_config_ref_is_unsupported(self):
        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        result = ship_a.start(_start_request(ship_a, config_ref="file://external.toml"))
        self.assertTrue(result.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", result.error.code)
        self.assertIs(EndpointHealth.STOPPED, ship_a.health)
        self.assertEqual((), ship_a.dependencies.service_event_sink.events())

    def test_TC_STAGE5_002_mismatched_identity_is_rejected_before_events(self):
        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        result = ship_a.start(
            ServiceStartRequest(
                local_station_id=ROC_STATION_ID, role=ServiceRole.SHIP
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)
        self.assertEqual((), ship_a.dependencies.service_event_sink.events())


class Stage5StartRollbackTest(unittest.TestCase):
    def test_TC_STAGE5_002_group_start_failure_rolls_back_started_ships(self):
        fault = SoftwareRadioFault(
            operation="configure", error_code=ErrorCode.RADIO_DEVICE_NOT_FOUND
        )
        clock, group = build_stage5_software_group(
            radio_faults_by_endpoint={"stage5-roc": (fault,)}
        )
        result = group.start()
        self.assertTrue(result.is_err)
        for endpoint in group.endpoints:
            with self.subTest(
                endpoint=endpoint.dependencies.configuration.endpoint.endpoint_id
            ):
                self.assertIs(EndpointHealth.STOPPED, endpoint.health)
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        # 역순 rollback으로 이미 시작한 Ship이 정지됐다.
        stopped_events = [
            event
            for event in ship_a.dependencies.service_event_sink.events()
            if event.event_type is ServiceEventType.SERVICE_STOPPED
        ]
        self.assertEqual(1, len(stopped_events))
        # 실패한 start()의 역순 rollback이 끝난 뒤에는 다시 start()할 수 있다.
        retry = group.start()
        self.assertTrue(retry.is_ok, getattr(retry, "error", None))
        group.stop()

    def test_TC_STAGE5_002_failed_endpoint_start_emits_error_not_started_event(self):
        fault = SoftwareRadioFault(
            operation="configure", error_code=ErrorCode.RADIO_DEVICE_NOT_FOUND
        )
        clock, group = build_stage5_software_group(
            radio_faults_by_endpoint={"stage5-ship-a": (fault,)}
        )
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        result = ship_a.start(_start_request(ship_a))
        self.assertTrue(result.is_err)
        self.assertEqual("DEVICE_ERROR", result.error.code)
        types = [
            event.event_type for event in ship_a.dependencies.service_event_sink.events()
        ]
        self.assertNotIn(ServiceEventType.SERVICE_STARTED, types)
        self.assertIn(ServiceEventType.ERROR, types)
        # rollback이 정상 완료돼 STOPPED이고 다시 시작할 수 있다.
        self.assertIs(EndpointHealth.STOPPED, ship_a.health)
        retry = ship_a.start(_start_request(ship_a))
        self.assertTrue(retry.is_ok, getattr(retry, "error", None))
        ship_a.stop(ServiceStopRequest())


class Stage5RadioStopTimeoutTest(unittest.TestCase):
    def test_TC_STAGE5_002_radio_stop_timeout_keeps_draining_until_retry(self):
        fault = SoftwareRadioFault(
            operation="stop", error_code=ErrorCode.RADIO_STOP_TIMEOUT
        )
        clock, group = build_stage5_software_group(
            radio_faults_by_endpoint={"stage5-ship-a": (fault,)}
        )
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        self.assertTrue(ship_a.start(_start_request(ship_a)).is_ok)
        queued = ship_a.submit(
            SubmitTransferRequest(
                destination=Addressed(MASS_B_STATION_ID),
                payload_format=OPAQUE,
                payload=b"\x01",
                ack_required=False,
            )
        )
        self.assertTrue(queued.is_ok)
        failed_stop = ship_a.stop(ServiceStopRequest(reason="tc-stage5-002-timeout"))
        self.assertTrue(failed_stop.is_err)
        # 내부 RADIO_STOP_TIMEOUT은 공개 DEVICE_ERROR/DEVICE retryable이다.
        self.assertEqual("DEVICE_ERROR", failed_stop.error.code)
        self.assertEqual("DEVICE", failed_stop.error.category.value)
        self.assertTrue(failed_stop.error.retryable)
        self.assertIsNone(failed_stop.error.transfer_id)
        self.assertIs(EndpointHealth.DEGRADED, ship_a.health)
        types = [
            event.event_type for event in ship_a.dependencies.service_event_sink.events()
        ]
        self.assertNotIn(ServiceEventType.SERVICE_STOPPED, types)
        # admission을 다시 열지 않는다: submit은 거부, status/cancel은 허용.
        blocked = ship_a.submit(
            SubmitTransferRequest(
                destination=Addressed(MASS_B_STATION_ID),
                payload_format=OPAQUE,
                payload=b"\x02",
                ack_required=False,
            )
        )
        self.assertTrue(blocked.is_err)
        self.assertEqual("RESOURCE_UNAVAILABLE", blocked.error.code)
        status = ship_a.status(TransferStatusRequest(queued.value.transfer_id))
        self.assertTrue(status.is_ok)
        restart_blocked = ship_a.start(_start_request(ship_a))
        self.assertTrue(restart_blocked.is_err)
        self.assertEqual("SERVICE_ALREADY_STARTED", restart_blocked.error.code)
        # cleanup 목적의 stop 재시도가 같은 종료 작업을 이어간다.
        retried = ship_a.stop(ServiceStopRequest(reason="tc-stage5-002-retry"))
        self.assertTrue(retried.is_ok, getattr(retried, "error", None))
        self.assertIs(EndpointHealth.STOPPED, ship_a.health)
        events = ship_a.dependencies.service_event_sink.events()
        self.assertIs(ServiceEventType.SERVICE_STOPPED, events[-1].event_type)
        # 끝나지 않은 transfer는 endpoint-stopping FAILED로 닫혔다.
        failed_terminal = [
            event
            for event in events
            if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
            and event.transfer_state is TransferState.FAILED
        ]
        self.assertEqual(1, len(failed_terminal))
        self.assertIsNotNone(failed_terminal[0].error)


class Stage5EventLifecycleAcrossRunsTest(unittest.TestCase):
    def _build_with_switchable_sinks(self):
        sinks = {}

        def factory(endpoint_id):
            sinks[endpoint_id] = _SwitchableSink()
            return sinks[endpoint_id]

        clock, group = build_stage5_software_group(service_sink_factory=factory)
        return clock, group, sinks

    def test_TC_STAGE5_002_lifecycle_fifo_and_net_coalescing_under_sink_failure(self):
        clock, group, sinks = self._build_with_switchable_sinks()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        sink = sinks["stage5-ship-a"]
        sink.failing = True
        started = ship_a.start(_start_request(ship_a))
        # event 전달 실패는 authoritative 시작 자체를 되돌리지 않는다.
        self.assertTrue(started.is_ok, getattr(started, "error", None))
        self.assertTrue(ship_a.publisher.delivery_failed)
        sink.failing = False
        ship_a.notify_sink_recovered()
        delivered = [
            (event.event_type, event.details.get("previous_health"), event.details.get("health"))
            for event in sink.published
            if getattr(event, "event_type", None) is not None
        ]
        # 첫 health event가 제출된 뒤 두 번째 token이 net transition을 보존한다.
        health_rows = [
            row for row in delivered if row[0] is ServiceEventType.ENDPOINT_HEALTH_CHANGED
        ]
        self.assertEqual("STOPPED", health_rows[0][1])
        final = health_rows[-1]
        self.assertEqual("RUNNING", final[2])
        started_rows = [
            row for row in delivered if row[0] is ServiceEventType.SERVICE_STARTED
        ]
        self.assertTrue(started_rows)
        ship_a.stop(ServiceStopRequest())

    def test_TC_STAGE5_002_two_pending_stops_block_third_run_without_mutation(self):
        clock, group, sinks = self._build_with_switchable_sinks()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        sink = sinks["stage5-ship-a"]
        # run 1 정상 시작 후 sink를 실패 상태로 만들고 stop → SERVICE_STOPPED pending.
        self.assertTrue(ship_a.start(_start_request(ship_a)).is_ok)
        sink.failing = True
        self.assertTrue(ship_a.stop(ServiceStopRequest(reason="run1")).is_ok)
        # run 2 시작·정지 → 두 번째 SERVICE_STOPPED pending.
        run2 = ship_a.start(_start_request(ship_a))
        self.assertTrue(run2.is_ok, getattr(run2, "error", None))
        self.assertTrue(ship_a.stop(ServiceStopRequest(reason="run2")).is_ok)
        # 세 번째 run은 STARTING commit 전에 동기 RESOURCE_UNAVAILABLE로 거부된다.
        refused = ship_a.start(_start_request(ship_a))
        self.assertTrue(refused.is_err)
        self.assertEqual("RESOURCE_UNAVAILABLE", refused.error.code)
        self.assertIs(EndpointHealth.STOPPED, ship_a.health)
        # 복구 뒤 run 순서가 보존된다: run1 SERVICE_STOPPED가 run2 SERVICE_STARTED보다 앞.
        sink.failing = False
        ship_a.notify_sink_recovered()
        types = [event.event_type for event in sink.published if event.event_id]
        delivered_types = []
        seen = set()
        for event in sink.published:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            delivered_types.append(event.event_type)
        first_stop = delivered_types.index(ServiceEventType.SERVICE_STOPPED)
        second_start = (
            first_stop
            + delivered_types[first_stop:].index(ServiceEventType.SERVICE_STARTED)
        )
        self.assertLess(first_stop, second_start)
        # 복구 뒤 새 run을 시작할 수 있다.
        recovered = ship_a.start(_start_request(ship_a))
        self.assertTrue(recovered.is_ok, getattr(recovered, "error", None))
        ship_a.stop(ServiceStopRequest())

    def test_TC_STAGE5_002_concurrent_start_admits_exactly_one_caller(self):
        """동시 start()는 직렬화돼 정확히 하나만 상태를 전이시킨다 (§9.6)."""

        import threading

        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        results = []
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait(timeout=10)
            results.append(ship_a.start(_start_request(ship_a)))

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        outcomes = sorted(
            "OK" if item.is_ok else item.error.code for item in results
        )
        self.assertEqual(["OK", "SERVICE_ALREADY_STARTED"], outcomes)
        started_events = [
            event
            for event in ship_a.dependencies.service_event_sink.events()
            if event.event_type is ServiceEventType.SERVICE_STARTED
        ]
        self.assertEqual(1, len(started_events))
        ship_a.stop(ServiceStopRequest())

    def test_TC_STAGE5_002_concurrent_stop_runs_cleanup_exactly_once(self):
        """동시 stop()은 Radio 종료·cleanup을 중복 실행하지 않는다 (§9.6)."""

        import threading

        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        self.assertTrue(ship_a.start(_start_request(ship_a)).is_ok)
        results = []
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait(timeout=10)
            results.append(ship_a.stop(ServiceStopRequest(reason="race")))

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        outcomes = sorted(
            "OK" if item.is_ok else item.error.code for item in results
        )
        self.assertEqual(["OK", "SERVICE_NOT_STARTED"], outcomes)
        stopped_events = [
            event
            for event in ship_a.dependencies.service_event_sink.events()
            if event.event_type is ServiceEventType.SERVICE_STOPPED
        ]
        self.assertEqual(1, len(stopped_events))
        self.assertIs(EndpointHealth.STOPPED, ship_a.health)

    def test_TC_STAGE5_002_event_ids_are_never_reused_across_restart(self):
        clock, group = build_stage5_software_group()
        ship_a = group.by_station()[MASS_A_STATION_ID.value]
        self.assertTrue(ship_a.start(_start_request(ship_a)).is_ok)
        self.assertTrue(ship_a.stop(ServiceStopRequest()).is_ok)
        self.assertTrue(ship_a.start(_start_request(ship_a)).is_ok)
        self.assertTrue(ship_a.stop(ServiceStopRequest()).is_ok)
        ids = [event.event_id for event in ship_a.dependencies.service_event_sink.events()]
        self.assertGreaterEqual(len(ids), 8)
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
