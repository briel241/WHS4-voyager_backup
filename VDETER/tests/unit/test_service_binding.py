"""MASS/ROC handler용 공개 Service event adapter 시험."""

from __future__ import annotations

import unittest

from vdeter.common.errors import ErrorCode
from vdeter.common.events import EventSeverity, ServiceEvent, ServiceEventType
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.service import HandlerServiceEventSink


def _event(event_id: str = "service-event-1") -> ServiceEvent:
    return ServiceEvent(
        event_id=event_id,
        event_type=ServiceEventType.SERVICE_STARTED,
        occurred_at=TimePoint(1, 0, ClockDomain.UTC, "binding-test-utc"),
        severity=EventSeverity.INFO,
    )


class HandlerServiceEventSinkTest(unittest.TestCase):
    def test_publish_calls_handler_and_acknowledges_exact_event(self):
        received = []
        sink = HandlerServiceEventSink(received.append)
        event = _event()

        result = sink.publish(event)

        self.assertTrue(result.is_ok)
        self.assertEqual([event], received)
        self.assertEqual(event.event_id, result.value.event_id)
        self.assertTrue(result.value.delivered)

    def test_non_service_event_is_rejected(self):
        sink = HandlerServiceEventSink(lambda _event: None)

        result = sink.publish(object())

        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)

    def test_handler_exception_is_delivery_failure(self):
        def failing_handler(_event):
            raise RuntimeError("application failure")

        result = HandlerServiceEventSink(failing_handler).publish(_event())

        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.INTERNAL_ERROR, result.error.code)
        self.assertTrue(result.error.retryable)
        self.assertEqual("RuntimeError", result.error.details.get("exception_type"))

    def test_handler_must_be_callable(self):
        with self.assertRaises(InvalidValueError):
            HandlerServiceEventSink(None)


if __name__ == "__main__":
    unittest.main()
