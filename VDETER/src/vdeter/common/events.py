"""비동기 event와 event sink (`stage0_baseline.md` §5.2, §6.3).

내부 계층은 `InternalEvent`로 가능한 모든 correlation을 보존하고, Service 경계에서는
Runtime의 projection을 거친 `ServiceEvent`만 Client에 전달한다. event 전달 매체(callback,
queue, stream, RPC)는 단계 0에서 정하지 않으며 이 module은 단방향 전달 의미만 정의한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .contracts.link import BuiltPacket, TransferRequirement
from .contracts.mac import MacActionSet, PacketDecodeAction, TransmissionPlan
from .contracts.message import DecodedMessages
from .contracts.phy import BasebandBurst, PhyTxCompleted, RecoveredPacketSet
from .contracts.presentation import ApplicationData, PresentationData
from .contracts.radio import (
    RadioRxWindowCompleted,
    RadioSamplesReceived,
    RadioStatusSnapshot,
    RadioTxCompleted,
)
from .contracts.service import ReceivedApplicationData, TransferState
from .errors import (
    Correlation,
    Details,
    EMPTY_CORRELATION,
    EMPTY_DETAILS,
    InternalProtocolError,
    Layer,
    ServiceError,
)
from .exceptions import InvalidValueError
from .identifiers import TransferId
from .time import TimePoint
from .results import InternalResult


class EventSeverity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class InternalEventType(str, Enum):
    """단계 0이 사용하는 내부 event 종류."""

    ENDPOINT_STATE_CHANGED = "ENDPOINT_STATE_CHANGED"
    TRANSFER_ACCEPTED = "TRANSFER_ACCEPTED"
    TRANSFER_STATE_CHANGED = "TRANSFER_STATE_CHANGED"
    PRESENTATION_ENCODED = "PRESENTATION_ENCODED"
    TRANSFER_REQUIREMENT_READY = "TRANSFER_REQUIREMENT_READY"
    TRANSMISSION_PLANNED = "TRANSMISSION_PLANNED"
    PACKET_BUILT = "PACKET_BUILT"
    BURST_ENCODED = "BURST_ENCODED"
    BURST_SCHEDULED = "BURST_SCHEDULED"
    BURST_COMPLETED = "BURST_COMPLETED"
    SAMPLES_RECEIVED = "SAMPLES_RECEIVED"
    PACKETS_RECOVERED = "PACKETS_RECOVERED"
    PACKET_DECODE_REQUESTED = "PACKET_DECODE_REQUESTED"
    MESSAGES_DECODED = "MESSAGES_DECODED"
    MAC_ACTIONS_READY = "MAC_ACTIONS_READY"
    APPLICATION_DATA_READY = "APPLICATION_DATA_READY"
    RADIO_STATUS_CHANGED = "RADIO_STATUS_CHANGED"
    RADIO_RX_WINDOW_COMPLETED = "RADIO_RX_WINDOW_COMPLETED"
    LAYER_ERROR = "LAYER_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ServiceEventType(str, Enum):
    """Client에 노출하는 공개 event 종류."""

    SERVICE_STARTED = "SERVICE_STARTED"
    SERVICE_STOPPED = "SERVICE_STOPPED"
    ENDPOINT_HEALTH_CHANGED = "ENDPOINT_HEALTH_CHANGED"
    TRANSFER_STATE_CHANGED = "TRANSFER_STATE_CHANGED"
    DATA_RECEIVED = "DATA_RECEIVED"
    ERROR = "ERROR"


#: `InternalEvent.payload`로 전달할 수 있는 공개 DTO. Runtime routing이 사용한다.
INTERNAL_EVENT_PAYLOAD_TYPES = (
    PresentationData,
    TransferRequirement,
    TransmissionPlan,
    BuiltPacket,
    BasebandBurst,
    PhyTxCompleted,
    RadioTxCompleted,
    RadioSamplesReceived,
    RadioStatusSnapshot,
    RadioRxWindowCompleted,
    RecoveredPacketSet,
    PacketDecodeAction,
    DecodedMessages,
    MacActionSet,
    ApplicationData,
)


@dataclass(frozen=True, slots=True)
class InternalEvent:
    """계층과 Runtime 사이의 비동기 통지. 내부 correlation 전체를 보존한다."""

    event_id: str
    event_type: InternalEventType
    occurred_at: TimePoint
    severity: EventSeverity
    layer: Layer
    correlation: Correlation = field(default=EMPTY_CORRELATION)
    payload: object = None
    error: InternalProtocolError | None = None
    details: Details = field(default=EMPTY_DETAILS)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise InvalidValueError("InternalEvent.event_id: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.event_type, InternalEventType):
            raise InvalidValueError("InternalEvent.event_type: InternalEventType이어야 합니다")
        if not isinstance(self.occurred_at, TimePoint):
            raise InvalidValueError("InternalEvent.occurred_at: TimePoint여야 합니다")
        if not isinstance(self.severity, EventSeverity):
            raise InvalidValueError("InternalEvent.severity: EventSeverity여야 합니다")
        if not isinstance(self.layer, Layer):
            raise InvalidValueError("InternalEvent.layer: Layer여야 합니다")
        if not isinstance(self.correlation, Correlation):
            raise InvalidValueError("InternalEvent.correlation: Correlation이어야 합니다")
        if self.payload is not None and not isinstance(self.payload, INTERNAL_EVENT_PAYLOAD_TYPES):
            raise InvalidValueError(
                "InternalEvent.payload: 승인된 공개 DTO만 담을 수 있습니다 "
                f"({type(self.payload).__name__})"
            )
        if self.error is not None and not isinstance(self.error, InternalProtocolError):
            raise InvalidValueError("InternalEvent.error: InternalProtocolError여야 합니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("InternalEvent.details: Details여야 합니다")


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    """Client 공개 event. 내부 correlation과 계층 private 상태를 포함하지 않는다."""

    event_id: str
    event_type: ServiceEventType
    occurred_at: TimePoint
    severity: EventSeverity
    transfer_id: TransferId | None = None
    transfer_state: TransferState | None = None
    received_data: ReceivedApplicationData | None = None
    error: ServiceError | None = None
    details: Details = field(default=EMPTY_DETAILS)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise InvalidValueError("ServiceEvent.event_id: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.event_type, ServiceEventType):
            raise InvalidValueError("ServiceEvent.event_type: ServiceEventType이어야 합니다")
        if not isinstance(self.occurred_at, TimePoint):
            raise InvalidValueError("ServiceEvent.occurred_at: TimePoint여야 합니다")
        if not isinstance(self.severity, EventSeverity):
            raise InvalidValueError("ServiceEvent.severity: EventSeverity여야 합니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("ServiceEvent.transfer_id: TransferId여야 합니다")
        if self.transfer_state is not None and not isinstance(self.transfer_state, TransferState):
            raise InvalidValueError("ServiceEvent.transfer_state: TransferState여야 합니다")
        if self.received_data is not None and not isinstance(
            self.received_data, ReceivedApplicationData
        ):
            raise InvalidValueError("ServiceEvent.received_data 형식이 잘못되었습니다")
        if self.error is not None and not isinstance(self.error, ServiceError):
            raise InvalidValueError("ServiceEvent.error: ServiceError여야 합니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("ServiceEvent.details: Details여야 합니다")


@dataclass(frozen=True, slots=True)
class EventPublished:
    """event 전달 접수 결과."""

    event_id: str
    delivered: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise InvalidValueError("EventPublished.event_id: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.delivered, bool):
            raise InvalidValueError("EventPublished.delivered: bool이어야 합니다")


@runtime_checkable
class InternalEventSink(Protocol):
    """내부 event의 단방향 전달 경계. 전송 매체는 정의하지 않는다."""

    def publish(self, event: InternalEvent) -> InternalResult[EventPublished]:
        """`InternalResult[EventPublished]`를 반환한다."""


@runtime_checkable
class ServiceEventSink(Protocol):
    """Service 공개 event의 단방향 전달 경계."""

    def publish(self, event: ServiceEvent) -> InternalResult[EventPublished]:
        """`InternalResult[EventPublished]`를 반환한다."""


class RecordingInternalEventSink:
    """시험과 runtime 조립에 사용하는 in-memory 내부 event sink."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events = []

    def publish(self, event: InternalEvent):
        from .results import Ok, internal_error
        from .errors import ErrorCategory, ErrorCode

        if not isinstance(event, InternalEvent):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "InternalEventSink에는 InternalEvent만 전달할 수 있습니다",
            )
        self._events.append(event)
        return Ok(EventPublished(event_id=event.event_id))

    def events(self) -> tuple[InternalEvent, ...]:
        return tuple(self._events)

    def events_of(self, event_type: InternalEventType) -> tuple:
        return tuple(event for event in self._events if event.event_type is event_type)

    def clear(self) -> None:
        self._events.clear()


class RecordingServiceEventSink:
    """시험과 runtime 조립에 사용하는 in-memory 공개 event sink."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events = []

    def publish(self, event: ServiceEvent):
        from .results import Ok, internal_error
        from .errors import ErrorCategory, ErrorCode

        if not isinstance(event, ServiceEvent):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "ServiceEventSink에는 ServiceEvent만 전달할 수 있습니다",
            )
        self._events.append(event)
        return Ok(EventPublished(event_id=event.event_id))

    def events(self) -> tuple:
        return tuple(self._events)

    def events_of(self, event_type: ServiceEventType) -> tuple:
        return tuple(event for event in self._events if event.event_type is event_type)

    def clear(self) -> None:
        self._events.clear()
