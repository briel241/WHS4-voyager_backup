"""Client ↔ Service 공개 경계 DTO (`stage0_baseline.md` §5.3, 상위 명세 §7.1).

이 module의 DTO에는 Message Type, VPFI, Link ID, wire Session ID, `session_handle`,
`SequentialId`, Packet/fragment ID, `burst_id`, slot, 재전송 횟수, FEC 또는 Radio 장비
객체를 포함하지 않는다. Client에는 end-to-end original 주소만 노출한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..addressing import Address, AddressKind
from ..errors import ServiceError
from ..exceptions import InvalidValueError
from ..identifiers import PayloadFormat, StationId, TransferId
from ..time import TimePoint


class ServiceRole(str, Enum):
    SHIP = "SHIP"
    CONTROL = "CONTROL"


class TransferState(str, Enum):
    """상위 명세 §7.1.3의 공개 전송 상태."""

    QUEUED = "QUEUED"
    TRANSMITTING = "TRANSMITTING"
    WAITING_ACK = "WAITING_ACK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EndpointHealth(str, Enum):
    """runtime health. protocol 진행 상태와 혼합하지 않는다."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    STOPPED = "STOPPED"


TERMINAL_TRANSFER_STATES = frozenset(
    {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class ServiceStartRequest:
    local_station_id: StationId
    role: ServiceRole
    config_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.local_station_id, StationId):
            raise InvalidValueError("ServiceStartRequest.local_station_id: StationId여야 합니다")
        if self.local_station_id.value == 0:
            raise InvalidValueError(
                "ServiceStartRequest.local_station_id: broadcast 예약값 0을 사용할 수 없습니다"
            )
        if not isinstance(self.role, ServiceRole):
            raise InvalidValueError("ServiceStartRequest.role: ServiceRole이어야 합니다")
        if not isinstance(self.config_ref, str):
            raise InvalidValueError("ServiceStartRequest.config_ref: 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class ServiceStarted:
    endpoint_id: str
    role: ServiceRole
    local_station_id: StationId
    health: EndpointHealth
    started_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id:
            raise InvalidValueError("ServiceStarted.endpoint_id가 비어 있습니다")
        if not isinstance(self.role, ServiceRole):
            raise InvalidValueError("ServiceStarted.role: ServiceRole이어야 합니다")
        if not isinstance(self.local_station_id, StationId):
            raise InvalidValueError("ServiceStarted.local_station_id: StationId여야 합니다")
        if not isinstance(self.health, EndpointHealth):
            raise InvalidValueError("ServiceStarted.health: EndpointHealth여야 합니다")
        if not isinstance(self.started_at, TimePoint):
            raise InvalidValueError("ServiceStarted.started_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class ServiceStopRequest:
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise InvalidValueError("ServiceStopRequest.reason: 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class ServiceStopped:
    endpoint_id: str
    health: EndpointHealth
    stopped_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id:
            raise InvalidValueError("ServiceStopped.endpoint_id가 비어 있습니다")
        if not isinstance(self.health, EndpointHealth):
            raise InvalidValueError("ServiceStopped.health: EndpointHealth여야 합니다")
        if not isinstance(self.stopped_at, TimePoint):
            raise InvalidValueError("ServiceStopped.stopped_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class SubmitTransferRequest:
    """송신 요청. Broadcast에는 ACK를 요청할 수 없다 (상위 명세 §7.1.1)."""

    destination: Address
    payload_format: PayloadFormat
    payload: bytes
    ack_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.destination, Address):
            raise InvalidValueError("SubmitTransferRequest.destination: Address여야 합니다")
        if not isinstance(self.payload_format, PayloadFormat):
            raise InvalidValueError("SubmitTransferRequest.payload_format: PayloadFormat이어야 합니다")
        if not isinstance(self.payload, bytes):
            raise InvalidValueError("SubmitTransferRequest.payload: bytes여야 합니다")
        if not isinstance(self.ack_required, bool):
            raise InvalidValueError("SubmitTransferRequest.ack_required: bool이어야 합니다")
        if self.destination.kind is AddressKind.BROADCAST and self.ack_required:
            raise InvalidValueError("SubmitTransferRequest: broadcast에는 ACK를 요청할 수 없습니다")


@dataclass(frozen=True, slots=True)
class TransferAccepted:
    """요청 접수 결과. 실제 무선 전송 완료를 의미하지 않는다."""

    transfer_id: TransferId
    state: TransferState
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("TransferAccepted.transfer_id: TransferId여야 합니다")
        if not isinstance(self.state, TransferState):
            raise InvalidValueError("TransferAccepted.state: TransferState여야 합니다")
        if not isinstance(self.accepted_at, TimePoint):
            raise InvalidValueError("TransferAccepted.accepted_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class CancelTransferRequest:
    transfer_id: TransferId

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("CancelTransferRequest.transfer_id: TransferId여야 합니다")


@dataclass(frozen=True, slots=True)
class CancelAccepted:
    """취소 요청 접수. 이미 송신된 데이터의 회수를 보장하지 않는다."""

    transfer_id: TransferId
    state: TransferState
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("CancelAccepted.transfer_id: TransferId여야 합니다")
        if not isinstance(self.state, TransferState):
            raise InvalidValueError("CancelAccepted.state: TransferState여야 합니다")
        if not isinstance(self.accepted_at, TimePoint):
            raise InvalidValueError("CancelAccepted.accepted_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class TransferStatusRequest:
    transfer_id: TransferId

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("TransferStatusRequest.transfer_id: TransferId여야 합니다")


@dataclass(frozen=True, slots=True)
class TransferStatusSnapshot:
    transfer_id: TransferId
    state: TransferState
    updated_at: TimePoint
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("TransferStatusSnapshot.transfer_id: TransferId여야 합니다")
        if not isinstance(self.state, TransferState):
            raise InvalidValueError("TransferStatusSnapshot.state: TransferState여야 합니다")
        if not isinstance(self.updated_at, TimePoint):
            raise InvalidValueError("TransferStatusSnapshot.updated_at: TimePoint여야 합니다")
        if self.error is not None and not isinstance(self.error, ServiceError):
            raise InvalidValueError("TransferStatusSnapshot.error: ServiceError여야 합니다")


@dataclass(frozen=True, slots=True)
class ReceivedApplicationData:
    """Client에 전달하는 수신 통지. end-to-end original 주소만 포함한다."""

    original_source: StationId
    original_destination: Address
    payload_format: PayloadFormat
    payload: bytes
    received_at: TimePoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.original_source, StationId):
            raise InvalidValueError("ReceivedApplicationData.original_source: StationId여야 합니다")
        if not isinstance(self.original_destination, Address):
            raise InvalidValueError("ReceivedApplicationData.original_destination: Address여야 합니다")
        if not isinstance(self.payload_format, PayloadFormat):
            raise InvalidValueError("ReceivedApplicationData.payload_format 형식이 잘못되었습니다")
        if not isinstance(self.payload, bytes):
            raise InvalidValueError("ReceivedApplicationData.payload: bytes여야 합니다")
        if self.received_at is not None and not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("ReceivedApplicationData.received_at: TimePoint여야 합니다")
