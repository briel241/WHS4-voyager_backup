"""Stage 4 bounded command outbox와 ingress (`DR-STAGE4-MAC-001`, `012`).

Runtime은 endpoint마다 bounded outbox를 소유한 sink를 MAC facade에 주입한다. sink는 command
실행, event publish, callback 또는 MAC 재호출을 동기 수행하지 않으며 `submit()`이 반환한
reservation은 dispatcher에 보이지 않는다.

`publish()`와 `abort()`는 예외를 내지 않는 total operation이다. 잘못되거나 이미
publish/abort된 reservation은 다른 record를 건드리지 않고 endpoint health evidence만 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

from ..common.contracts.mac_execution import (
    CLEANUP_COMMAND_PAYLOAD_TYPES,
    CancelWakeupCommand,
    InvokeLinkCommand,
    InvokePhyCommand,
    MacCommandBatch,
    MacCommandEnvelope,
    MacEventEnvelope,
    MacFailureReason,
    MacOutboxReservation,
    MacReasonCode,
    PublishTransferStateCommand,
)
from ..common.contracts.link import (
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    ReassemblyControlKind,
    TbbAssemblyControlRequest,
    TransferControlKind,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..mac.execution import command_class_of, is_cleanup_command
from ..mac.models import PendingCommandClass, Stage4Limits

#: `PublishTransferStateCommand`와 `DeliverPresentationDataCommand`의 bounded 재시도 상한.
MAX_CONSUMER_ATTEMPTS = 3

#: endpoint health evidence 보존 상한.
HEALTH_EVIDENCE_LIMIT = 256


class OutboxRecordState(str, Enum):
    RESERVED = "RESERVED"
    READY = "READY"


def _error(code: str, category: ErrorCategory, message: str, reason, reason_code=None, **extra):
    details = dict(extra)
    details["mac_reason"] = reason.value
    if reason_code is not None:
        details["reason_code"] = reason_code
    return internal_error(
        Layer.RUNTIME, code, category, message, details=Details.of(**details)
    )


@dataclass(slots=True)
class OutboxRecord:
    """outbox의 한 command. `RESERVED`는 dispatcher에 보이지 않는다."""

    command: MacCommandEnvelope
    state: OutboxRecordState
    reservation_id: str


class EndpointHealthLog:
    """bounded endpoint health evidence. 공개 Service 오류를 만들지 않는다."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list = []

    def record(self, code: str, reason_code: str, details: Details) -> None:
        self._entries.append((code, reason_code, details))
        if len(self._entries) > HEALTH_EVIDENCE_LIMIT:
            del self._entries[0 : len(self._entries) - HEALTH_EVIDENCE_LIMIT]

    def entries(self) -> tuple:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()


class MacCommandOutbox:
    """`MacCommandSink` 구현. Stage 4 내부 가산 계약이며 공개 Port가 아니다."""

    __slots__ = (
        "_id_source",
        "_lock",
        "_records",
        "_reservations",
        "_draining",
        "_health",
        "_capacity",
    )

    def __init__(self, id_source, lock: RLock, health: EndpointHealthLog, *, capacity: int | None = None) -> None:
        self._id_source = id_source
        self._lock = lock
        self._health = health
        self._capacity = capacity if capacity is not None else Stage4Limits.COMMAND_OUTBOX
        self._records: list[OutboxRecord] = []
        self._reservations: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._draining = False

    # --- 관찰 ---

    @property
    def draining(self) -> bool:
        return self._draining

    def set_draining(self, value: bool) -> None:
        with self._lock:
            self._draining = bool(value)

    def occupancy(self) -> int:
        """`RESERVED+READY` 합계."""

        with self._lock:
            return len(self._records)

    def reserved_count(self) -> int:
        with self._lock:
            return sum(
                1 for item in self._records if item.state is OutboxRecordState.RESERVED
            )

    def ready_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._records if item.state is OutboxRecordState.READY)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._reservations.clear()
            self._draining = False

    # --- MacCommandSink ---

    def submit(self, batch: MacCommandBatch):
        """용량·ID를 원자적으로 검사하고 `RESERVED` record를 만든다.

        command 실행, event publish, callback 또는 MAC 재호출을 하지 않는다.
        """

        with self._lock:
            if not isinstance(batch, MacCommandBatch):
                return _error(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "MacCommandBatch가 필요합니다",
                    MacFailureReason.EVENT_INVALID,
                )
            if self._draining:
                blocked = tuple(
                    item.command_id for item in batch.commands if not is_cleanup_command(item)
                )
                if blocked:
                    return _error(
                        ErrorCode.MAC_TRANSFER_FAILED,
                        ErrorCategory.RESOURCE,
                        "DRAINING에서는 cleanup allowlist만 수락합니다",
                        MacFailureReason.DEPENDENCY_FAILED,
                        MacReasonCode.ENDPOINT_STOPPING,
                        blocked_command_count=len(blocked),
                    )
            if len(self._records) + len(batch.commands) > self._capacity:
                return _error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "command outbox 상한을 초과했습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    None,
                    outbox_occupancy=len(self._records),
                    batch_size=len(batch.commands),
                    outbox_capacity=self._capacity,
                )
            existing = {item.command.command_id for item in self._records}
            duplicates = tuple(
                item.command_id for item in batch.commands if item.command_id in existing
            )
            if duplicates:
                return _error(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "outbox에 이미 있는 command_id입니다",
                    MacFailureReason.EVENT_INVALID,
                    None,
                    duplicate_command_count=len(duplicates),
                )
            reservation_id = self._id_source.new_event_id()
            for command in batch.commands:
                self._records.append(
                    OutboxRecord(
                        command=command,
                        state=OutboxRecordState.RESERVED,
                        reservation_id=reservation_id,
                    )
                )
            command_ids = batch.command_ids()
            self._reservations[reservation_id] = (batch.batch_id, command_ids)
            return Ok(
                MacOutboxReservation(
                    reservation_id=reservation_id,
                    batch_id=batch.batch_id,
                    command_ids=command_ids,
                )
            )

    def publish(self, reservation: MacOutboxReservation) -> None:
        """이미 검증·예약된 record의 flag만 `READY`로 바꾼다. 예외를 내지 않는다."""

        with self._lock:
            if not self._consume_reservation(reservation, "publish"):
                return
            for record in self._records:
                if record.reservation_id == reservation.reservation_id:
                    record.state = OutboxRecordState.READY

    def abort(self, reservation: MacOutboxReservation) -> None:
        """state commit 실패 시 `RESERVED` record를 제거한다. 예외를 내지 않는다."""

        with self._lock:
            if not self._consume_reservation(reservation, "abort"):
                return
            self._records = [
                record
                for record in self._records
                if record.reservation_id != reservation.reservation_id
            ]

    def _consume_reservation(self, reservation, operation: str) -> bool:
        if not isinstance(reservation, MacOutboxReservation):
            self._health.record(
                ErrorCode.INVALID_REQUEST,
                MacReasonCode.OUTBOX_RESERVATION_INVALID,
                Details.of(operation=operation, reason="not_a_reservation"),
            )
            return False
        stored = self._reservations.get(reservation.reservation_id)
        if stored is None:
            self._health.record(
                ErrorCode.INVALID_REQUEST,
                MacReasonCode.OUTBOX_RESERVATION_INVALID,
                Details.of(operation=operation, reason="unknown_or_consumed"),
            )
            return False
        if stored != (reservation.batch_id, reservation.command_ids):
            self._health.record(
                ErrorCode.INVALID_REQUEST,
                MacReasonCode.OUTBOX_RESERVATION_INVALID,
                Details.of(operation=operation, reason="token_mismatch"),
            )
            return False
        matching = [
            record
            for record in self._records
            if record.reservation_id == reservation.reservation_id
        ]
        if not matching or any(
            record.state is not OutboxRecordState.RESERVED for record in matching
        ):
            self._health.record(
                ErrorCode.INVALID_REQUEST,
                MacReasonCode.OUTBOX_RESERVATION_INVALID,
                Details.of(operation=operation, reason="record_not_reserved"),
            )
            return False
        del self._reservations[reservation.reservation_id]
        return True

    # --- dispatcher 전용 ---

    def take_ready(self) -> tuple[MacCommandEnvelope, ...]:
        """`READY` record만 순서대로 꺼낸다. `RESERVED`는 절대 보이지 않는다.

        dispatcher도 같은 endpoint lock을 획득해야 하므로 state commit과 lock 해제 전에
        batch를 관측·실행할 수 없다.
        """

        with self._lock:
            ready = tuple(
                record.command
                for record in self._records
                if record.state is OutboxRecordState.READY
            )
            self._records = [
                record
                for record in self._records
                if record.state is not OutboxRecordState.READY
            ]
            return ready

    def peek_ready(self) -> tuple[MacCommandEnvelope, ...]:
        with self._lock:
            return tuple(
                record.command
                for record in self._records
                if record.state is OutboxRecordState.READY
            )


class MacIngressQueue:
    """단일 endpoint ingress lock에서 `ingress_sequence`를 부여하는 bounded event queue."""

    __slots__ = (
        "_lock",
        "_events",
        "_sequence",
        "_capacity",
        "_held",
        "_held_capacity",
        "_time_source",
        "_id_source",
    )

    def __init__(
        self,
        lock: RLock,
        time_source,
        id_source,
        *,
        capacity: int | None = None,
        held_capacity: int | None = None,
    ) -> None:
        self._lock = lock
        self._time_source = time_source
        self._id_source = id_source
        self._capacity = capacity if capacity is not None else Stage4Limits.INGRESS_EVENTS
        self._held_capacity = (
            held_capacity
            if held_capacity is not None
            else (
                Stage4Limits.PENDING_DEPENDENCY_TOTAL * 2
                + Stage4Limits.ACTIVE_TIMERS
                + Stage4Limits.RX_DELIVERY_LEDGER
            )
        )
        self._events: list[MacEventEnvelope] = []
        self._sequence = 0
        self._held: list = []

    @property
    def sequence(self) -> int:
        return self._sequence

    def depth(self) -> int:
        with self._lock:
            return len(self._events)

    def is_full(self) -> bool:
        with self._lock:
            return len(self._events) >= self._capacity

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._held.clear()
            self._sequence = 0

    def next_event_id(self) -> str:
        return self._id_source.new_event_id()

    def enqueue(
        self,
        payload,
        *,
        correlation: Correlation = Correlation(),
        causation_command_id: str | None = None,
        event_id: str | None = None,
        priority: bool = False,
    ):
        """event를 ingress에 넣고 `ingress_sequence`를 부여한다.

        ingress가 가득 차면 신규 local/clock work를 거부한다. 이미 수락된 command의 결과와
        active timer의 wakeup은 `priority=True`로 보류 slot을 통해 먼저 enqueue한다.
        """

        with self._lock:
            if len(self._events) >= self._capacity:
                if not priority:
                    return _error(
                        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                        ErrorCategory.RESOURCE,
                        "MAC ingress event queue가 가득 찼습니다",
                        MacFailureReason.RESOURCE_UNAVAILABLE,
                        None,
                        ingress_depth=len(self._events),
                    )
                if len(self._held) >= self._held_capacity:
                    return _error(
                        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                        ErrorCategory.RESOURCE,
                        "MAC ingress held-result slots가 가득 찼습니다",
                        MacFailureReason.RESOURCE_UNAVAILABLE,
                        None,
                        held_result_count=len(self._held),
                        held_result_limit=self._held_capacity,
                    )
                self._held.append((payload, correlation, causation_command_id, event_id))
                return _error(
                    ErrorCode.MAC_RESOURCE_UNAVAILABLE,
                    ErrorCategory.RESOURCE,
                    "ingress 포화로 dependency 결과를 보류했습니다",
                    MacFailureReason.RESOURCE_UNAVAILABLE,
                    None,
                    held_result_count=len(self._held),
                )
            envelope = MacEventEnvelope(
                event_id=event_id or self._id_source.new_event_id(),
                occurred_at_utc=self._time_source.now_utc(),
                observed_at_monotonic=self._time_source.now_monotonic(),
                ingress_sequence=self._sequence,
                correlation=correlation,
                payload=payload,
                causation_command_id=causation_command_id,
            )
            self._sequence += 1
            self._events.append(envelope)
            return Ok(envelope)

    def release_held(self) -> int:
        """자리가 생기면 보류한 결과를 신규 local work보다 먼저 enqueue한다."""

        released = 0
        with self._lock:
            while self._held and len(self._events) < self._capacity:
                payload, correlation, causation_command_id, event_id = self._held.pop(0)
                result = self.enqueue(
                    payload,
                    correlation=correlation,
                    causation_command_id=causation_command_id,
                    event_id=event_id,
                )
                if result.is_err:
                    break
                released += 1
        return released

    def held_count(self) -> int:
        with self._lock:
            return len(self._held)

    def has_held_causation(self, command_id: str) -> bool:
        with self._lock:
            return any(item[2] == command_id for item in self._held)

    def drain(self) -> tuple[MacEventEnvelope, ...]:
        """`(observed_at_monotonic, ingress_sequence, event_id)` 총순서로 꺼낸다."""

        with self._lock:
            ordered = tuple(sorted(self._events, key=lambda item: item.order_key()))
            self._events.clear()
            return ordered

    def pop_next(self) -> MacEventEnvelope | None:
        with self._lock:
            if not self._events:
                return None
            self._events.sort(key=lambda item: item.order_key())
            return self._events.pop(0)


__all__ = (
    "EndpointHealthLog",
    "HEALTH_EVIDENCE_LIMIT",
    "MAX_CONSUMER_ATTEMPTS",
    "MacCommandOutbox",
    "MacIngressQueue",
    "OutboxRecord",
    "OutboxRecordState",
    "command_class_of",
)
