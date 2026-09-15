"""TC-ARCH-007, 008, 024, 025: 구조화된 오류, event projection과 logging redaction.

요구사항: `ARCH-REQ-06`, `ARCH-REQ-07`
"""

from __future__ import annotations

import io
import json
import unittest

from tests.support import dto_catalog
from tests.support.fixtures import build_fake_endpoint, load_fake_configuration

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    ServiceStartRequest,
    SubmitTransferRequest,
    TransferState,
)
from vdeter.common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
)
from vdeter.common.events import (
    EventSeverity,
    InternalEvent,
    InternalEventType,
    ServiceEventType,
)
from vdeter.common.exceptions import InvalidValueError, VdeterInternalError
from vdeter.common.identifiers import PayloadFormat, StationId
from vdeter.common.logging import (
    JsonlLogger,
    LogContext,
    REDACTED_MARKER,
    payload_digest,
    redact_mapping,
)
from vdeter.common.results import internal_error, try_call
from vdeter.runtime.endpoint import build_endpoint
from vdeter.runtime.projection import PublicErrorCode, to_service_error
from vdeter.runtime.wiring import build_dependencies


class StructuredErrorTest(unittest.TestCase):
    """TC-ARCH-007: 예상 오류가 Result로 반환되고 raw exception이 경계를 넘지 않는다."""

    def setUp(self):
        self.endpoint, self.dependencies = build_fake_endpoint()

    def test_TC_ARCH_007_layer_ports_return_err_for_invalid_input(self):
        endpoint = self.endpoint
        cases = (
            ("presentation", endpoint._presentation.encode_payload, object()),
            ("presentation", endpoint._presentation.decode_payload, object()),
            ("link", endpoint._link.analyze_transfer, object()),
            ("link", endpoint._link.build_packet, object()),
            ("link", endpoint._link.decode_packet, object()),
            ("mac", endpoint._mac.submit_requirement, object()),
            ("mac", endpoint._mac.handle_recovered_packet, object()),
            ("mac", endpoint._mac.handle_message, object()),
            ("mac", endpoint._mac.control_session, object()),
            ("phy", endpoint._phy.encode_packet, object()),
            ("phy", endpoint._phy.decode_samples, object()),
            ("radio", endpoint.radio.configure, object()),
            ("radio", endpoint.radio.start, object()),
            ("radio", endpoint.radio.stop, object()),
            ("radio", endpoint.radio.schedule_tx, object()),
            ("radio", endpoint.radio.request_rx, object()),
            ("radio", endpoint.radio.status, object()),
        )
        for layer, call, argument in cases:
            with self.subTest(layer=layer, call=call.__name__):
                result = call(argument)
                self.assertTrue(result.is_err)
                self.assertIsInstance(result.error, InternalProtocolError)
                self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)

    def test_TC_ARCH_007_service_port_returns_service_error(self):
        result = self.endpoint.submit(object())
        self.assertTrue(result.is_err)
        self.assertEqual(PublicErrorCode.INVALID_REQUEST, result.error.code)

    def test_TC_ARCH_007_service_event_sink_failure_is_not_ignored(self):
        class FailingServiceEventSink:
            def publish(self, event):
                return internal_error(
                    Layer.SERVICE,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "injected service event delivery failure",
                )

        configuration = load_fake_configuration()
        dependencies = build_dependencies(
            configuration, service_event_sink=FailingServiceEventSink()
        )
        self.assertTrue(dependencies.is_ok)
        endpoint = build_endpoint(dependencies.value)
        self.assertTrue(endpoint.is_ok)

        started = endpoint.value.start(
            ServiceStartRequest(
                local_station_id=configuration.endpoint.station_id,
                role=configuration.endpoint.role,
            )
        )
        self.assertTrue(started.is_err)
        self.assertEqual(PublicErrorCode.INTERNAL_ERROR, started.error.code)
        self.assertEqual("UNHEALTHY", endpoint.value.health.value)

    def test_TC_ARCH_007_unsupported_request_is_structured(self):
        self.endpoint.start(
            ServiceStartRequest(
                local_station_id=self.dependencies.configuration.endpoint.station_id,
                role=self.dependencies.configuration.endpoint.role,
            )
        )
        result = self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("unregistered.format"),
                payload=b"x",
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(PublicErrorCode.UNSUPPORTED_REQUEST, result.error.code)

    def test_TC_ARCH_007_try_call_converts_validation_exception(self):
        def factory():
            raise InvalidValueError("잘못된 값")

        result = try_call(Layer.LINK, ErrorCode.INVALID_VALUE, factory)
        self.assertTrue(result.is_err)
        self.assertEqual(Layer.LINK, result.error.layer)

    def test_TC_ARCH_007_programming_error_is_not_masked(self):
        def factory():
            raise VdeterInternalError("깨진 invariant")

        with self.assertRaises(VdeterInternalError):
            try_call(Layer.LINK, ErrorCode.INTERNAL_ERROR, factory)

    def test_TC_ARCH_007_details_reject_non_portable_values(self):
        with self.assertRaises(InvalidValueError):
            Details.of(value=1.5)
        with self.assertRaises(InvalidValueError):
            Details.of(payload=b"secret")
        with self.assertRaises(InvalidValueError):
            Details.of(private_key="x")
        with self.assertRaises(InvalidValueError):
            Details((("BadKey", 1),))


class EventProjectionTest(unittest.TestCase):
    """TC-ARCH-008, 024: 접수 후 event의 correlation 보존과 공개 projection."""

    def setUp(self):
        self.endpoint, self.dependencies = build_fake_endpoint()
        configuration = self.dependencies.configuration.endpoint
        self.endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )

    def _submit(self):
        return self.endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"stage0 payload",
            )
        )

    def test_TC_ARCH_008_completion_events_carry_correlation(self):
        accepted = self._submit()
        self.assertTrue(accepted.is_ok)
        transfer_id = accepted.value.transfer_id
        self.endpoint.radio.complete_scheduled_tx()

        internal = self.dependencies.internal_event_sink.events()
        planned = [
            event
            for event in internal
            if event.event_type is InternalEventType.TRANSMISSION_PLANNED
        ]
        self.assertTrue(planned)
        self.assertEqual(transfer_id, planned[0].correlation.transfer_id)

        burst_events = [
            event
            for event in internal
            if event.event_type is InternalEventType.BURST_ENCODED
        ]
        self.assertTrue(burst_events)
        self.assertIsNotNone(burst_events[0].correlation.packet_id)
        self.assertIsNotNone(burst_events[0].correlation.burst_id)

        service_events = self.dependencies.service_event_sink.events()
        states = [
            event.transfer_state
            for event in service_events
            if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
        ]
        self.assertIn(TransferState.QUEUED, states)
        self.assertIn(TransferState.COMPLETED, states)

    def test_TC_ARCH_008_service_events_expose_only_public_fields(self):
        self._submit()
        self.endpoint.radio.complete_scheduled_tx()
        for event in self.dependencies.service_event_sink.events():
            self.assertFalse(hasattr(event, "correlation"))
            for key in event.details.keys():
                self.assertNotIn(key, ("session_handle", "packet_id", "burst_id"))

    def test_TC_ARCH_024_internal_error_preserves_all_correlation(self):
        error = dto_catalog.INTERNAL_ERROR
        for name in Correlation.FIELD_NAMES:
            self.assertIsNotNone(getattr(error.correlation, name))

    def test_TC_ARCH_024_service_projection_removes_internal_identifiers(self):
        public = to_service_error(dto_catalog.INTERNAL_ERROR)
        self.assertEqual(dto_catalog.TRANSFER_ID, public.transfer_id)
        self.assertFalse(hasattr(public, "correlation"))
        self.assertFalse(hasattr(public, "layer"))
        encoded = json.dumps(
            {
                "code": public.code,
                "message": public.message,
                "details": public.details.as_dict(),
            },
            ensure_ascii=False,
        )
        for secret in (
            dto_catalog.SESSION_HANDLE.value,
            dto_catalog.PACKET_ID.value,
            dto_catalog.BURST_ID.value,
            str(dto_catalog.SEQUENTIAL_ID.value),
        ):
            self.assertNotIn(secret, encoded)

    def test_TC_ARCH_024_mapped_error_message_cannot_leak_internal_identifiers(self):
        secret = dto_catalog.SESSION_HANDLE.value
        error = InternalProtocolError(
            layer=Layer.MAC,
            code=ErrorCode.INVALID_REQUEST,
            category=ErrorCategory.VALIDATION,
            message=f"invalid session {secret}",
            correlation=Correlation(session_handle=dto_catalog.SESSION_HANDLE),
        )
        public = to_service_error(error)
        self.assertEqual(PublicErrorCode.INVALID_REQUEST, public.code)
        self.assertNotIn(secret, public.message)

    def test_TC_ARCH_024_details_reject_internal_correlation_keys(self):
        for key in (
            "session_handle",
            "sequential_id",
            "packet_id",
            "fragment_id",
            "fragment_index",
            "fragment_count",
            "burst_id",
        ):
            with self.subTest(key=key):
                with self.assertRaises(InvalidValueError):
                    Details.of(**{key: "internal"})

    def test_TC_ARCH_024_unmapped_internal_code_is_generalized(self):
        error = InternalProtocolError(
            layer=Layer.MAC,
            code="SOME_FUTURE_MAC_CODE",
            category=ErrorCategory.PROTOCOL,
            message="내부 전용 상세 정보",
            correlation=Correlation(session_handle=dto_catalog.SESSION_HANDLE),
        )
        public = to_service_error(error)
        self.assertEqual(PublicErrorCode.INTERNAL_ERROR, public.code)
        self.assertNotIn("내부 전용 상세 정보", public.message)

    def test_TC_ARCH_024_internal_log_keeps_correlation(self):
        from vdeter.common.logging import record_from_internal_event

        context = LogContext(endpoint_id="ep", role="SHIP", code_version="0.1.0")
        event = InternalEvent(
            event_id="event-1",
            event_type=InternalEventType.LAYER_ERROR,
            occurred_at=dto_catalog.UTC,
            severity=EventSeverity.ERROR,
            layer=Layer.LINK,
            correlation=dto_catalog.CORRELATION,
            error=dto_catalog.INTERNAL_ERROR,
        )
        record = record_from_internal_event(context, event, "vdeter")
        for key in (
            "transfer_id",
            "session_handle",
            "sequential_id",
            "packet_id",
            "burst_id",
            "fragment_index",
            "fragment_count",
        ):
            self.assertIn(key, record)


class JsonlLoggingTest(unittest.TestCase):
    """TC-ARCH-025: JSONL 필수 field와 payload·secret·sample redaction."""

    def _logger(self):
        stream = io.StringIO()
        context = LogContext(
            endpoint_id="stage0-fake-ship",
            role="SHIP",
            code_version="0.1.0",
            config_fingerprint="sha256:abc",
            registry_fingerprints=(("payload_format", "sha256:def"),),
            run_id="run-1",
            test_case_id="TC-ARCH-025",
            seed=20260819,
            test_clock_id="test-utc",
        )
        return JsonlLogger(stream, context), stream

    def test_TC_ARCH_025_record_has_required_fields(self):
        logger, stream = self._logger()
        logger.log(
            timestamp=dto_catalog.UTC,
            severity=EventSeverity.INFO,
            layer=Layer.RUNTIME,
            event_code="ENDPOINT_STARTED",
            message="started",
            correlation=dto_catalog.CORRELATION,
            details=Details.of(reason="stage0"),
        )
        line = stream.getvalue().strip()
        record = json.loads(line)
        for key in (
            "schema_version",
            "timestamp",
            "severity",
            "logger",
            "layer",
            "event_code",
            "message",
            "endpoint_id",
            "role",
            "code_version",
            "config_fingerprint",
            "registry_fingerprints",
            "run_id",
            "test_case_id",
            "seed",
            "test_clock_id",
            "transfer_id",
            "session_handle",
            "sequential_id",
            "packet_id",
            "burst_id",
        ):
            self.assertIn(key, record)
        self.assertEqual(
            {"seconds", "nanoseconds", "clock_domain", "clock_id"}, set(record["timestamp"])
        )

    def test_TC_ARCH_025_jsonl_is_one_object_per_line(self):
        logger, stream = self._logger()
        for index in range(3):
            logger.log(
                timestamp=dto_catalog.UTC,
                severity=EventSeverity.INFO,
                layer=Layer.RUNTIME,
                event_code=f"EVENT_{index}",
                message="x",
            )
        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(3, len(lines))
        for line in lines:
            json.loads(line)

    def test_TC_ARCH_025_payload_secret_and_samples_are_redacted(self):
        redacted = redact_mapping(
            {
                "payload": "application data",
                "private_key": "key material",
                "samples": [1, 2, 3],
                "certificate": "cert body",
                "payload_size": 12,
                "raw": b"\x00\x01",
            }
        )
        self.assertEqual(REDACTED_MARKER, redacted["payload"])
        self.assertEqual(REDACTED_MARKER, redacted["private_key"])
        self.assertEqual(REDACTED_MARKER, redacted["samples"])
        self.assertEqual(REDACTED_MARKER, redacted["certificate"])
        self.assertEqual(12, redacted["payload_size"])
        self.assertEqual(2, redacted["raw"]["length"])
        self.assertTrue(redacted["raw"]["digest"].startswith("sha256:"))

    def test_TC_ARCH_025_payload_digest_is_stable(self):
        self.assertEqual(payload_digest(b"abc"), payload_digest(b"abc"))
        self.assertNotEqual(payload_digest(b"abc"), payload_digest(b"abd"))

    def test_TC_ARCH_025_details_cannot_carry_payload_keys(self):
        with self.assertRaises(InvalidValueError):
            Details.of(samples=(1, 2))


if __name__ == "__main__":
    unittest.main()
