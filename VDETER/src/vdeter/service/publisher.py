"""Stage 5 bounded Service event publisher (`stage5_service_integration_baseline.md` §9.4).

endpoint별 미처리 공개 event queue는 총 256개이며 240 일반 영역과 16 cleanup reserve로
나뉜다. reservation(아직 payload가 채워지지 않은 token)도 occupancy에 포함하므로
reservation으로 상한을 우회할 수 없다.

- 각 accepted outgoing transfer: 일반 영역 nonterminal state-chain token 2개 +
  cleanup reserve terminal token 1개
- endpoint 시작 transaction: cleanup reserve에서 health 2, endpoint `ERROR` 1,
  이전/현재 run `SERVICE_STOPPED` slot 2 (고정 slot pair)
- incoming Data Session: 일반 영역 `DATA_RECEIVED` slot 1 + payload byte budget
- `DATA_RECEIVED.payload` 합계는 16 MiB budget을 넘지 않는다.
- FIFO를 보존하고 head 실패 중 뒤 event를 앞질러 전달하지 않는다.
- 아직 sink에 제출하지 않은 같은 transfer의 nonterminal 상태와 연속 health event만
  coalesce한다. `DATA_RECEIVED`, terminal state, `ERROR`는 폐기·coalesce하지 않는다.
- 한 전달 시도는 `Ok(EventPublished(event_id=<제출값>, delivered=true))`일 때만 성공이다.
  실패하면 같은 `event_id`로 최대 3회 자동 재시도하고, 그 뒤에는 delivery-failed pending으로
  유지하며 endpoint를 `DEGRADED`로 만든다. 복구 신호(`notify_sink_recovered`)에서 같은
  `event_id`로 재개한다.

publisher 자체는 binding을 강제하지 않으며 주입된 `ServiceEventSink.publish`만 호출한다.
"""

from __future__ import annotations

from functools import wraps
from threading import RLock

from ..common.contracts.service import (
    TERMINAL_TRANSFER_STATES,
    TransferState,
)
from ..common.errors import (
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
)
from ..common.events import (
    EventSeverity,
    ServiceEvent,
    ServiceEventType,
)
from ..common.results import Ok, internal_error
from .models import Stage5ServicePolicy

_GENERAL = "GENERAL"
_CLEANUP = "CLEANUP"

_KIND_STATE = "TRANSFER_STATE_NONTERMINAL"
_KIND_TERMINAL = "TRANSFER_TERMINAL"
_KIND_DATA = "DATA_RECEIVED"
_KIND_ERROR = "ERROR"
_KIND_HEALTH = "HEALTH"
_KIND_STARTED = "SERVICE_STARTED"
_KIND_STOPPED = "SERVICE_STOPPED"
_KIND_OTHER = "OTHER"

#: `stage5` §5.7의 health별 severity.
_HEALTH_SEVERITY = {
    "STARTING": EventSeverity.INFO,
    "RUNNING": EventSeverity.INFO,
    "STOPPED": EventSeverity.INFO,
    "DEGRADED": EventSeverity.WARNING,
    "UNHEALTHY": EventSeverity.CRITICAL,
}


def _synchronized(method):
    """publisher 내부 상태 접근을 단일 lock으로 직렬화한다 (`stage5` §9.6).

    sink callback은 이 lock 안에서 호출하지 않는다. `pump()`는 별도의 single-flight
    구조를 사용한다.
    """

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return guarded


def _resource_error(message: str, **details):
    return internal_error(
        Layer.SERVICE,
        ErrorCode.SERVICE_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        message,
        retryable=True,
        details=Details.of(**details) if details else Details(),
    )


class _QueuedEvent:
    __slots__ = ("event", "region", "kind", "attempted", "payload_bytes", "transfer_key", "run_id")

    def __init__(self, event, region, kind, *, payload_bytes=0, transfer_key=None, run_id=0):
        self.event = event
        self.region = region
        self.kind = kind
        self.attempted = False
        self.payload_bytes = payload_bytes
        self.transfer_key = transfer_key
        self.run_id = run_id


class Stage5EventPublisher:
    """bounded FIFO Service event queue와 reservation·coalescing·retry 실행기."""

    __slots__ = (
        "_policy",
        "_sink",
        "_id_source",
        "_queue",
        "_transfer_tokens",
        "_rx_reservations",
        "_health_free",
        "_error_free",
        "_stopped_reserved",
        "_started_reserved",
        "_lifecycle_reserved",
        "_payload_reserved_bytes",
        "_payload_queued_bytes",
        "_delivery_failed",
        "_head_attempts",
        "_run_id",
        "_issued_event_ids",
        "_lock",
        "_pump_active",
        "_pump_requested",
        "on_delivery_failed",
        "on_queue_drained",
    )

    def __init__(self, policy: Stage5ServicePolicy, sink, id_source) -> None:
        self._policy = policy
        self._sink = sink
        self._id_source = id_source
        self._lock = RLock()
        self._pump_active = False
        self._pump_requested = False
        self._queue = []
        self._transfer_tokens = {}
        self._rx_reservations = {}
        self._health_free = 0
        self._error_free = 0
        self._stopped_reserved = False
        self._started_reserved = False
        self._lifecycle_reserved = False
        self._payload_reserved_bytes = 0
        self._payload_queued_bytes = 0
        self._delivery_failed = False
        self._head_attempts = 0
        self._run_id = 0
        self._issued_event_ids = set()
        self.on_delivery_failed = None
        self.on_queue_drained = None

    # --- 관찰 ---

    @property
    def delivery_failed(self) -> bool:
        return self._delivery_failed

    @_synchronized
    def pending_count(self) -> int:
        return len(self._queue)

    @_synchronized
    def general_occupancy(self) -> int:
        queued = sum(1 for item in self._queue if item.region == _GENERAL)
        free_state_tokens = sum(entry["free_state"] for entry in self._transfer_tokens.values())
        started = 1 if self._started_reserved else 0
        return queued + free_state_tokens + len(self._rx_reservations) + started

    @_synchronized
    def cleanup_occupancy(self) -> int:
        queued = sum(1 for item in self._queue if item.region == _CLEANUP)
        terminal_tokens = sum(
            1 for entry in self._transfer_tokens.values() if entry["terminal"]
        )
        stopped = 1 if self._stopped_reserved else 0
        return queued + terminal_tokens + self._health_free + self._error_free + stopped

    @_synchronized
    def total_occupancy(self) -> int:
        return self.general_occupancy() + self.cleanup_occupancy()

    @_synchronized
    def payload_occupancy_bytes(self) -> int:
        return self._payload_queued_bytes + self._payload_reserved_bytes

    # --- lifecycle reservation (`stage5` §9.4) ---

    @_synchronized
    def reserve_lifecycle(self):
        """start transaction의 cleanup reserve 선예약. 실패 시 아무것도 바꾸지 않는다."""

        if self._lifecycle_reserved:
            return Ok(True)
        pending_stopped = sum(1 for item in self._queue if item.kind == _KIND_STOPPED)
        if pending_stopped > 1:
            # 이전/현재 run SERVICE_STOPPED slot pair(2)가 이미 소진됐다.
            return _resource_error(
                "이전 실행의 SERVICE_STOPPED event가 전달되지 않아 새 실행을 예약할 수 없습니다",
                limit_name="service_stopped_slots",
                limit_value=2,
                current_value=pending_stopped,
            )
        needed_cleanup = 2 + 1 + 1  # health 2 + ERROR 1 + 현재 run SERVICE_STOPPED 1
        if self.cleanup_occupancy() + needed_cleanup > self._policy.event_cleanup_reserve:
            return _resource_error(
                "cleanup reserve에 lifecycle token을 예약할 수 없습니다",
                limit_name="event_cleanup_reserve",
                limit_value=self._policy.event_cleanup_reserve,
                current_value=self.cleanup_occupancy(),
            )
        if self.general_occupancy() + 1 > self._policy.event_general_capacity:
            return _resource_error(
                "SERVICE_STARTED admission slot을 예약할 수 없습니다",
                limit_name="event_general_capacity",
                limit_value=self._policy.event_general_capacity,
                current_value=self.general_occupancy(),
            )
        self._health_free = 2
        self._error_free = 1
        self._stopped_reserved = True
        self._started_reserved = True
        self._lifecycle_reserved = True
        self._run_id += 1
        return Ok(True)

    @_synchronized
    def release_lifecycle(self) -> None:
        """start 실패 rollback 또는 stop 완료 뒤 남은 lifecycle 예약을 해제한다."""

        self._health_free = 0
        self._error_free = 0
        self._stopped_reserved = False
        self._started_reserved = False
        self._lifecycle_reserved = False

    # --- transfer reservation ---

    @_synchronized
    def reserve_transfer(self, transfer_key: str):
        if transfer_key in self._transfer_tokens:
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "이미 예약된 transfer event token입니다",
            )
        tokens = self._policy.state_chain_tokens_per_transfer
        if self.general_occupancy() + tokens > self._policy.event_general_capacity:
            return _resource_error(
                "일반 event 영역에 상태 chain token을 예약할 수 없습니다",
                limit_name="event_general_capacity",
                limit_value=self._policy.event_general_capacity,
                current_value=self.general_occupancy(),
            )
        if self.cleanup_occupancy() + 1 > self._policy.event_cleanup_reserve:
            return _resource_error(
                "cleanup reserve에 terminal token을 예약할 수 없습니다",
                limit_name="event_cleanup_reserve",
                limit_value=self._policy.event_cleanup_reserve,
                current_value=self.cleanup_occupancy(),
            )
        self._transfer_tokens[transfer_key] = {"free_state": tokens, "terminal": True}
        return Ok(True)

    @_synchronized
    def release_transfer(self, transfer_key: str) -> None:
        """rollback용 idempotent 해제. queue에 이미 materialize된 event는 그대로 둔다."""

        self._transfer_tokens.pop(transfer_key, None)

    # --- incoming Data Session reservation ---

    @_synchronized
    def reserve_incoming(self, reservation_key: str, expected_payload_bytes: int):
        if reservation_key in self._rx_reservations:
            return Ok(True)
        if self.general_occupancy() + 1 > self._policy.event_general_capacity:
            return _resource_error(
                "수신 DATA_RECEIVED slot을 예약할 수 없습니다",
                limit_name="event_general_capacity",
                limit_value=self._policy.event_general_capacity,
                current_value=self.general_occupancy(),
            )
        if (
            self.payload_occupancy_bytes() + expected_payload_bytes
            > self._policy.event_payload_budget_bytes
        ):
            return _resource_error(
                "DATA_RECEIVED payload byte budget을 예약할 수 없습니다",
                limit_name="event_payload_budget_bytes",
                limit_value=self._policy.event_payload_budget_bytes,
                current_value=self.payload_occupancy_bytes(),
            )
        self._rx_reservations[reservation_key] = expected_payload_bytes
        self._payload_reserved_bytes += expected_payload_bytes
        return Ok(True)

    @_synchronized
    def release_incoming(self, reservation_key: str) -> None:
        """abort/timeout/stop cleanup의 idempotent 해제 (`stage5` §9.4)."""

        released = self._rx_reservations.pop(reservation_key, None)
        if released is not None:
            self._payload_reserved_bytes -= released

    # --- event admission ---

    def _new_event(self, event_type, severity, **fields) -> ServiceEvent:
        event_id = self._id_source.new_event_id()
        if event_id in self._issued_event_ids:
            raise RuntimeError("event ID가 endpoint 수명 안에서 재사용되었습니다")
        self._issued_event_ids.add(event_id)
        return ServiceEvent(
            event_id=event_id,
            event_type=event_type,
            severity=severity,
            **fields,
        )

    @_synchronized
    def admit_transfer_state(
        self,
        transfer_key: str,
        transfer_id,
        state: TransferState,
        occurred_at,
        error=None,
    ):
        terminal = state in TERMINAL_TRANSFER_STATES
        severity = (
            EventSeverity.ERROR if state is TransferState.FAILED else EventSeverity.INFO
        )
        event = self._new_event(
            ServiceEventType.TRANSFER_STATE_CHANGED,
            severity,
            occurred_at=occurred_at,
            transfer_id=transfer_id,
            transfer_state=state,
            error=error,
        )
        tokens = self._transfer_tokens.get(transfer_key)
        if terminal:
            if tokens is None or not tokens["terminal"]:
                return internal_error(
                    Layer.SERVICE,
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "terminal event token이 예약돼 있지 않습니다",
                )
            # 중요 event admission을 위해 아직 제출하지 않은 같은 transfer의 coalescible
            # nonterminal event를 먼저 제거한다. token은 아래에서 함께 해제된다.
            self._queue = [
                item
                for item in self._queue
                if not (
                    item.kind == _KIND_STATE
                    and item.transfer_key == transfer_key
                    and not item.attempted
                )
            ]
            self._transfer_tokens.pop(transfer_key, None)
            self._queue.append(
                _QueuedEvent(event, _CLEANUP, _KIND_TERMINAL, transfer_key=transfer_key)
            )
            return Ok(True)
        # nonterminal: 아직 제출하지 않은 최신 상태 event에 coalesce한다.
        for index in range(len(self._queue) - 1, -1, -1):
            item = self._queue[index]
            if (
                item.kind == _KIND_STATE
                and item.transfer_key == transfer_key
                and not item.attempted
            ):
                del self._queue[index]
                self._queue.append(
                    _QueuedEvent(event, _GENERAL, _KIND_STATE, transfer_key=transfer_key)
                )
                return Ok(True)
        if tokens is None or tokens["free_state"] <= 0:
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "nonterminal 상태 chain token이 남아 있지 않습니다",
            )
        tokens["free_state"] -= 1
        self._queue.append(
            _QueuedEvent(event, _GENERAL, _KIND_STATE, transfer_key=transfer_key)
        )
        return Ok(True)

    @_synchronized
    def admit_data_received(self, received_data, occurred_at, reservation_key=None):
        payload_bytes = len(received_data.payload)
        if reservation_key is not None and reservation_key in self._rx_reservations:
            reserved = self._rx_reservations.pop(reservation_key)
            self._payload_reserved_bytes -= reserved
        else:
            if self.general_occupancy() + 1 > self._policy.event_general_capacity:
                return _resource_error(
                    "일반 event 영역이 가득 차 수신 data를 수락할 수 없습니다",
                    limit_name="event_general_capacity",
                    limit_value=self._policy.event_general_capacity,
                    current_value=self.general_occupancy(),
                )
        if self.payload_occupancy_bytes() + payload_bytes > self._policy.event_payload_budget_bytes:
            return _resource_error(
                "DATA_RECEIVED payload byte budget을 초과합니다",
                limit_name="event_payload_budget_bytes",
                limit_value=self._policy.event_payload_budget_bytes,
                current_value=self.payload_occupancy_bytes(),
            )
        event = self._new_event(
            ServiceEventType.DATA_RECEIVED,
            EventSeverity.INFO,
            occurred_at=occurred_at,
            received_data=received_data,
        )
        self._payload_queued_bytes += payload_bytes
        self._queue.append(
            _QueuedEvent(event, _GENERAL, _KIND_DATA, payload_bytes=payload_bytes)
        )
        return Ok(True)

    @_synchronized
    def admit_error(self, occurred_at, error, transfer_id=None):
        if self._error_free <= 0:
            # 예약된 endpoint ERROR가 아직 전달되지 않았다. 두 번째 spill queue를 만들지
            # 않고 원인 계층의 bounded pending 상태가 authoritative owner로 남는다.
            return _resource_error(
                "예약된 endpoint ERROR event가 아직 전달되지 않았습니다",
                limit_name="endpoint_error_tokens",
                limit_value=1,
                current_value=0,
            )
        event = self._new_event(
            ServiceEventType.ERROR,
            EventSeverity.ERROR,
            occurred_at=occurred_at,
            transfer_id=transfer_id,
            error=error,
        )
        self._error_free -= 1
        self._queue.append(_QueuedEvent(event, _CLEANUP, _KIND_ERROR))
        return Ok(True)

    @_synchronized
    def admit_health_changed(self, occurred_at, previous_health, health, details=None):
        run_id = self._run_id
        severity = _HEALTH_SEVERITY[health.value]
        extra = dict(details.as_dict()) if details is not None else {}
        extra["previous_health"] = previous_health.value
        extra["health"] = health.value
        # 아직 제출하지 않은 같은 run의 연속 health event에는 net transition으로 coalesce한다.
        for index in range(len(self._queue) - 1, -1, -1):
            item = self._queue[index]
            if item.kind == _KIND_HEALTH and not item.attempted and item.run_id == run_id:
                original_previous = item.event.details.get("previous_health")
                extra["previous_health"] = original_previous
                event = self._new_event(
                    ServiceEventType.ENDPOINT_HEALTH_CHANGED,
                    _HEALTH_SEVERITY[health.value],
                    occurred_at=occurred_at,
                    details=Details.from_mapping(extra),
                )
                del self._queue[index]
                self._queue.append(
                    _QueuedEvent(event, _CLEANUP, _KIND_HEALTH, run_id=run_id)
                )
                return Ok(True)
        if self._health_free <= 0:
            return _resource_error(
                "health chain token이 남아 있지 않습니다",
                limit_name="health_tokens",
                limit_value=2,
                current_value=0,
            )
        event = self._new_event(
            ServiceEventType.ENDPOINT_HEALTH_CHANGED,
            severity,
            occurred_at=occurred_at,
            details=Details.from_mapping(extra),
        )
        self._health_free -= 1
        self._queue.append(_QueuedEvent(event, _CLEANUP, _KIND_HEALTH, run_id=run_id))
        return Ok(True)

    @_synchronized
    def admit_service_started(self, occurred_at, endpoint_id: str):
        if not self._started_reserved:
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "SERVICE_STARTED slot이 예약돼 있지 않습니다",
            )
        event = self._new_event(
            ServiceEventType.SERVICE_STARTED,
            EventSeverity.INFO,
            occurred_at=occurred_at,
            details=Details.of(endpoint_id=endpoint_id),
        )
        self._started_reserved = False
        self._queue.append(_QueuedEvent(event, _GENERAL, _KIND_STARTED))
        return Ok(True)

    @_synchronized
    def release_service_started(self) -> None:
        self._started_reserved = False

    @_synchronized
    def admit_service_stopped(self, occurred_at, endpoint_id: str):
        if not self._stopped_reserved:
            return internal_error(
                Layer.SERVICE,
                ErrorCode.INTERNAL_ERROR,
                ErrorCategory.INTERNAL,
                "SERVICE_STOPPED slot이 예약돼 있지 않습니다",
            )
        event = self._new_event(
            ServiceEventType.SERVICE_STOPPED,
            EventSeverity.INFO,
            occurred_at=occurred_at,
            details=Details.of(endpoint_id=endpoint_id),
        )
        self._stopped_reserved = False
        self._queue.append(_QueuedEvent(event, _CLEANUP, _KIND_STOPPED))
        return Ok(True)

    # --- 전달 ---

    def pump(self) -> None:
        """head부터 FIFO로 sink 전달을 시도한다. 실패한 head를 앞지르지 않는다.

        동시 호출은 single-flight로 직렬화한다: 이미 pump가 실행 중이면 재실행 요청만
        남기고 반환하며, 실행 중인 pump가 종료 직전에 그 요청을 이어받아 새로 admission된
        event를 놓치지 않는다. `ServiceEventSink.publish`와 상태 callback은 내부 lock
        밖에서 호출한다 (`stage5` §9.6).
        """

        with self._lock:
            if self._pump_active:
                self._pump_requested = True
                return
            self._pump_active = True
        try:
            self._pump_loop()
        finally:
            with self._lock:
                self._pump_active = False

    def _pump_loop(self) -> None:
        while True:
            failed_callback = None
            drained_callback = None
            with self._lock:
                self._pump_requested = False
                if self._delivery_failed:
                    return
                if not self._queue:
                    if self.on_queue_drained is not None:
                        drained_callback = self.on_queue_drained
                    head = None
                else:
                    head = self._queue[0]
                    head.attempted = True
            if head is None:
                if drained_callback is not None:
                    drained_callback()
                with self._lock:
                    # drained callback 또는 다른 thread가 그 사이 admission했으면 이어간다.
                    if self._pump_requested and self._queue and not self._delivery_failed:
                        continue
                return
            delivered = self._attempt(head)  # sink 호출은 lock 밖이다.
            with self._lock:
                if delivered:
                    # single-flight이므로 head는 여전히 queue[0]이다.
                    self._queue.pop(0)
                    self._on_delivered(head)
                    self._head_attempts = 0
                    continue
                self._head_attempts += 1
                if self._head_attempts > self._policy.event_publish_max_retries:
                    self._delivery_failed = True
                    self._head_attempts = 0
                    failed_callback = self.on_delivery_failed
            if failed_callback is not None:
                failed_callback()
            if failed_callback is not None or self._delivery_failed:
                return

    def _attempt(self, head: _QueuedEvent) -> bool:
        try:
            result = self._sink.publish(head.event)
        except Exception:  # noqa: BLE001 - sink 예외도 전달 실패로 센다.
            return False
        if result is None or getattr(result, "is_err", True):
            return False
        published = result.value
        return bool(
            getattr(published, "delivered", False)
            and getattr(published, "event_id", None) == head.event.event_id
        )

    def _on_delivered(self, head: _QueuedEvent) -> None:
        if head.kind == _KIND_STATE:
            tokens = self._transfer_tokens.get(head.transfer_key)
            if tokens is not None:
                tokens["free_state"] = min(
                    tokens["free_state"] + 1, self._policy.state_chain_tokens_per_transfer
                )
        elif head.kind == _KIND_HEALTH:
            if self._lifecycle_reserved and head.run_id == self._run_id:
                self._health_free = min(self._health_free + 1, 2)
        elif head.kind == _KIND_ERROR:
            if self._lifecycle_reserved:
                self._error_free = 1
        elif head.kind == _KIND_DATA:
            self._payload_queued_bytes -= head.payload_bytes

    def notify_sink_recovered(self) -> None:
        """binding별 복구 신호. 같은 `event_id`로 head부터 전달을 재개한다."""

        with self._lock:
            self._delivery_failed = False
            self._head_attempts = 0
        self.pump()
