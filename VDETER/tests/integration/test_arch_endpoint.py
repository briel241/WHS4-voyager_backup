"""TC-ARCH-027: Fake/Stub 빈 endpoint의 lifecycle과 TX·RX 양방향 route.

요구사항: `ARCH-REQ-01..07`
"""

from __future__ import annotations

import unittest

from tests.support.fixtures import build_fake_endpoint

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    CancelTransferRequest,
    EndpointHealth,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import InternalEventType, ServiceEventType
from vdeter.common.identifiers import PayloadFormat, StationId

PAYLOAD_FORMAT = PayloadFormat("test.stage0.opaque")
PEER = StationId(222222222)


class EmptyEndpointTest(unittest.TestCase):
    """단계 0 빈 endpoint의 조립·시작·종료와 양방향 route."""

    def setUp(self):
        self.endpoint, self.dependencies = build_fake_endpoint()
        self.configuration = self.dependencies.configuration.endpoint

    def _start(self):
        return self.endpoint.start(
            ServiceStartRequest(
                local_station_id=self.configuration.station_id,
                role=self.configuration.role,
                config_ref="test",
            )
        )

    def test_TC_ARCH_027_dependencies_are_injected(self):
        dependencies = self.dependencies
        for name in (
            "time_source",
            "random_source",
            "id_source",
            "radio",
            "registries",
            "internal_event_sink",
            "service_event_sink",
        ):
            with self.subTest(dependency=name):
                self.assertIsNotNone(getattr(dependencies, name))

    def test_TC_ARCH_027_registries_are_frozen_at_start(self):
        registries = self.dependencies.registries
        for name in (
            "payload_format",
            "message_codec",
            "message_profile",
            "link_profile",
            "phy_profile",
        ):
            registry = getattr(registries, name)
            with self.subTest(registry=name):
                frozen = getattr(registry, "is_frozen", None)
                self.assertTrue(frozen)
        fingerprints = dict(registries.fingerprints())
        self.assertEqual(5, len(fingerprints))
        self.assertEqual(5, len(set(fingerprints.values())))

    def test_TC_ARCH_027_start_and_stop(self):
        self.assertIs(EndpointHealth.STOPPED, self.endpoint.health)
        started = self._start()
        self.assertTrue(started.is_ok, started.error if started.is_err else "")
        self.assertIs(EndpointHealth.RUNNING, started.value.health)

        again = self._start()
        self.assertTrue(again.is_err)
        self.assertEqual("SERVICE_ALREADY_STARTED", again.error.code)

        stopped = self.endpoint.stop(ServiceStopRequest(reason="test"))
        self.assertTrue(stopped.is_ok)
        self.assertIs(EndpointHealth.STOPPED, stopped.value.health)
        self.assertTrue(self.endpoint.stop(ServiceStopRequest()).is_err)

    def test_TC_ARCH_027_same_endpoint_restart_discards_runtime_state(self):
        from vdeter.common.contracts.mac import SessionControlKind, SessionControlRequest

        self.assertTrue(self._start().is_ok)
        session = self.endpoint._mac.control_session(
            SessionControlRequest(kind=SessionControlKind.OPEN)
        )
        accepted = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER),
                payload_format=PAYLOAD_FORMAT,
                payload=b"restart-state",
            )
        )
        self.assertTrue(session.is_ok and accepted.is_ok)
        self.assertTrue(self.endpoint.stop(ServiceStopRequest(reason="restart")).is_ok)
        self.assertTrue(self._start().is_ok)
        self.assertEqual(0, self.endpoint._mac.known_session_count())
        status = self.endpoint.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertTrue(status.is_err)
        self.assertEqual("UNKNOWN_TRANSFER", status.error.code)
        self.assertEqual((), self.endpoint.radio.scheduled_bursts())

    def test_TC_ARCH_027_programming_error_sets_unhealthy_and_is_generalized(self):
        from dataclasses import replace

        from tests.support.fixtures import load_fake_configuration
        from vdeter.runtime.endpoint import build_endpoint
        from vdeter.runtime.projection import PublicErrorCode
        from vdeter.runtime.wiring import build_dependencies

        class ExplodingRadio:
            def configure(self, request):
                raise RuntimeError("private device failure")

            def start(self, request):
                raise AssertionError("unreachable")

            def stop(self, request):
                raise AssertionError("unreachable")

            def schedule_tx(self, request):
                raise AssertionError("unreachable")

            def request_rx(self, request):
                raise AssertionError("unreachable")

            def status(self, request):
                raise AssertionError("unreachable")

        dependencies = build_dependencies(load_fake_configuration()).value
        endpoint = build_endpoint(replace(dependencies, radio=ExplodingRadio())).value
        configuration = dependencies.configuration.endpoint
        result = endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id,
                role=configuration.role,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(PublicErrorCode.INTERNAL_ERROR, result.error.code)
        self.assertNotIn("private device failure", result.error.message)
        self.assertEqual(EndpointHealth.UNHEALTHY, endpoint.health)
        self.assertTrue(
            dependencies.internal_event_sink.events_of(InternalEventType.LAYER_ERROR)
        )

    def test_TC_ARCH_027_start_failure_rolls_back_started_radio(self):
        from dataclasses import replace

        from tests.support.fixtures import load_fake_configuration
        from vdeter.common.errors import ErrorCategory, ErrorCode, Layer
        from vdeter.common.results import internal_error
        from vdeter.runtime.endpoint import build_endpoint
        from vdeter.runtime.wiring import build_dependencies

        class RxFailingRadio:
            def __init__(self, delegate):
                self.delegate = delegate
                self.stop_called = False

            def configure(self, request):
                return self.delegate.configure(request)

            def start(self, request):
                return self.delegate.start(request)

            def stop(self, request):
                self.stop_called = True
                return self.delegate.stop(request)

            def schedule_tx(self, request):
                return self.delegate.schedule_tx(request)

            def request_rx(self, request):
                return internal_error(
                    Layer.RADIO,
                    ErrorCode.RADIO_UNKNOWN_CHANNEL,
                    ErrorCategory.DEVICE,
                    "injected RX failure",
                )

            def status(self, request):
                return self.delegate.status(request)

        dependencies = build_dependencies(load_fake_configuration()).value
        radio = RxFailingRadio(dependencies.radio)
        endpoint = build_endpoint(replace(dependencies, radio=radio)).value
        configuration = dependencies.configuration.endpoint
        result = endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id,
                role=configuration.role,
            )
        )
        self.assertTrue(result.is_err)
        self.assertTrue(radio.stop_called)
        self.assertEqual(EndpointHealth.UNHEALTHY, endpoint.health)

    def test_TC_ARCH_027_submit_before_start_is_rejected(self):
        result = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=b"x"
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual("SERVICE_NOT_STARTED", result.error.code)

    def test_TC_ARCH_027_transmit_route_passes_every_layer(self):
        self._start()
        accepted = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER),
                payload_format=PAYLOAD_FORMAT,
                payload=b"stage0 transmit",
            )
        )
        self.assertTrue(accepted.is_ok)
        self.endpoint.radio.complete_scheduled_tx()

        types = [event.event_type for event in self.dependencies.internal_event_sink.events()]
        for expected in (
            InternalEventType.TRANSMISSION_PLANNED,
            InternalEventType.PACKET_BUILT,
            InternalEventType.BURST_ENCODED,
            InternalEventType.BURST_SCHEDULED,
            InternalEventType.BURST_COMPLETED,
        ):
            with self.subTest(event=expected.value):
                self.assertIn(expected, types)

        status = self.endpoint.status(TransferStatusRequest(accepted.value.transfer_id))
        self.assertTrue(status.is_ok)
        self.assertIs(TransferState.COMPLETED, status.value.state)

    def test_TC_ARCH_027_receive_route_passes_every_layer(self):
        self._start()
        payload = b"stage0 receive"
        accepted = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=payload
            )
        )
        self.assertTrue(accepted.is_ok)
        scheduled = self.endpoint.radio.scheduled_bursts()
        self.assertEqual(1, len(scheduled))

        injected = self.endpoint.radio.inject_rx_samples(
            self.configuration.mac.rx_channel, scheduled[0].samples
        )
        self.assertTrue(injected.is_ok)

        types = [event.event_type for event in self.dependencies.internal_event_sink.events()]
        for expected in (
            InternalEventType.SAMPLES_RECEIVED,
            InternalEventType.PACKETS_RECOVERED,
            InternalEventType.PACKET_DECODE_REQUESTED,
            InternalEventType.MESSAGES_DECODED,
            InternalEventType.MAC_ACTIONS_READY,
            InternalEventType.APPLICATION_DATA_READY,
        ):
            with self.subTest(event=expected.value):
                self.assertIn(expected, types)

        received = [
            event
            for event in self.dependencies.service_event_sink.events()
            if event.event_type is ServiceEventType.DATA_RECEIVED
        ]
        self.assertEqual(1, len(received))
        data = received[0].received_data
        self.assertEqual(payload, data.payload)
        self.assertEqual(PAYLOAD_FORMAT, data.payload_format)
        self.assertEqual(PEER, data.original_source)

    def test_TC_ARCH_027_service_events_have_no_internal_identifiers(self):
        self._start()
        self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=b"x"
            )
        )
        scheduled = self.endpoint.radio.scheduled_bursts()
        self.endpoint.radio.complete_scheduled_tx()
        self.endpoint.radio.inject_rx_samples(
            self.configuration.mac.rx_channel, scheduled[0].samples
        )
        for event in self.dependencies.service_event_sink.events():
            with self.subTest(event=event.event_type.value):
                self.assertFalse(hasattr(event, "correlation"))
                if event.received_data is not None:
                    fields = set(event.received_data.__slots__)
                    for forbidden in ("session_handle", "packet_id", "burst_id", "link_id"):
                        self.assertNotIn(forbidden, fields)

    def test_TC_ARCH_027_cancel_and_status(self):
        self._start()
        accepted = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=b"x"
            )
        )
        transfer_id = accepted.value.transfer_id
        cancelled = self.endpoint.cancel(CancelTransferRequest(transfer_id))
        self.assertTrue(cancelled.is_ok)
        self.assertIs(TransferState.CANCELLED, cancelled.value.state)

        again = self.endpoint.cancel(CancelTransferRequest(transfer_id))
        self.assertTrue(again.is_err)

        unknown = self.endpoint.status(
            TransferStatusRequest(self.dependencies.id_source.new_transfer_id())
        )
        self.assertTrue(unknown.is_err)
        self.assertEqual("UNKNOWN_TRANSFER", unknown.error.code)

    def test_TC_ARCH_027_late_completion_does_not_overwrite_cancelled(self):
        self._start()
        accepted = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=b"cancel-race"
            )
        )
        self.assertTrue(accepted.is_ok)
        transfer_id = accepted.value.transfer_id

        cancelled = self.endpoint.cancel(CancelTransferRequest(transfer_id))
        self.assertTrue(cancelled.is_ok)
        self.assertTrue(self.endpoint.radio.complete_scheduled_tx().is_ok)

        status = self.endpoint.status(TransferStatusRequest(transfer_id))
        self.assertTrue(status.is_ok)
        self.assertIs(TransferState.CANCELLED, status.value.state)
        completed = [
            event
            for event in self.dependencies.service_event_sink.events()
            if event.transfer_id == transfer_id
            and event.transfer_state is TransferState.COMPLETED
        ]
        self.assertEqual([], completed)

    def test_TC_ARCH_027_payload_limit_is_enforced(self):
        self._start()
        oversize = b"x" * (self.configuration.service.max_payload_bytes + 1)
        result = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(PEER), payload_format=PAYLOAD_FORMAT, payload=oversize
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual("INVALID_REQUEST", result.error.code)

    def test_TC_ARCH_027_runtime_does_not_implement_protocol_rules(self):
        """Runtime module이 protocol 규칙을 중복 구현하지 않는다."""

        import inspect

        from vdeter.runtime import endpoint as endpoint_module
        from vdeter.runtime import wiring as wiring_module

        for module in (endpoint_module, wiring_module):
            source = inspect.getsource(module).lower()
            for token in (
                "def _crc",
                "def _fec",
                "def _modulate",
                "def _interleave",
                "def _scramble",
                "def _build_message",
                "def _select_slot",
            ):
                with self.subTest(module=module.__name__, token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
