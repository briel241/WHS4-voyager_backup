"""MASS/ROC application handler를 Service event 계약에 연결하는 공개 adapter."""

from __future__ import annotations

from collections.abc import Callable

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.events import EventPublished, ServiceEvent
from ..common.exceptions import InvalidValueError
from ..common.results import Ok, internal_error

ServiceEventHandler = Callable[[ServiceEvent], None]


class HandlerServiceEventSink:
    """`ServiceEventSink`을 application handler 호출로 연결한다.

    이 adapter는 event를 받은 호출 흐름에서 handler를 즉시 실행한다. 따라서 handler는
    event를 application queue에 넘기거나 짧은 상태 갱신만 수행해야 한다. 장시간 PKI 검증,
    파일·DB I/O 같은 업무는 handler가 별도 worker에 위임한다.
    """

    __slots__ = ("_handler",)

    def __init__(self, handler: ServiceEventHandler) -> None:
        if not callable(handler):
            raise InvalidValueError("HandlerServiceEventSink.handler: callable이어야 합니다")
        self._handler = handler

    def publish(self, event: ServiceEvent):
        """event를 handler에 전달하고 정확한 `EventPublished` 수락 결과를 반환한다."""

        if not isinstance(event, ServiceEvent):
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "HandlerServiceEventSink에는 ServiceEvent만 전달할 수 있습니다",
            )

        try:
            self._handler(event)
        except Exception as exc:  # noqa: BLE001 - handler 예외를 sink 전달 실패로 투영한다.
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "Service event handler 실행에 실패했습니다",
                retryable=True,
                details=Details.of(exception_type=type(exc).__name__),
            )

        return Ok(EventPublished(event_id=event.event_id, delivered=True))


__all__ = ("HandlerServiceEventSink", "ServiceEventHandler")
