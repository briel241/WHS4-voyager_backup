"""MAC Port (`stage0_baseline.md` §5.3, 상위 명세 §7.4, §7.5).

MAC은 VDE-TER의 시간, 채널, 자원과 통신 절차 상태를 소유한다. Message bit layout, CRC,
FEC, modulation과 장비 제어는 구현하지 않는다.

Stage 4는 동결 `MacPort` 네 메서드를 그대로 유지하면서 가산 `MacExecutionPort`를 함께
구현하는 `Stage4Mac` facade를 제공한다. 두 Port는 동일한 coordinator와 state repository에
위임하며 상태 복사본을 갖지 않는다.
"""

from __future__ import annotations

from threading import RLock
from typing import Protocol, runtime_checkable

from ..common.contracts.link import TransferRequirement
from ..common.contracts.mac import (
    MacActionSet,
    MacMessageInput,
    PacketDecodeAction,
    RequirementAccepted,
    SessionControlRequest,
    SessionControlResult,
)
from ..common.contracts.mac_execution import (
    EMPTY_MAC_BATCH_RECEIPT,
    MacBatchReceipt,
    MacEventEnvelope,
    MacFailureReason,
    MacReasonCode,
)
from ..common.contracts.phy import RecoveredPacketSet
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.results import InternalResult, Ok, internal_error


@runtime_checkable
class MacPort(Protocol):
    """Link ↔ MAC ↔ PHY 경계 계약."""

    def submit_requirement(
        self, requirement: TransferRequirement
    ) -> InternalResult[RequirementAccepted]:
        """`InternalResult[RequirementAccepted]`를 반환한다.

        실제 전송 계획은 접수 뒤 `TransmissionPlan` event로 전달한다.
        """

    def handle_recovered_packet(
        self, recovered: RecoveredPacketSet
    ) -> InternalResult[PacketDecodeAction]:
        """`InternalResult[PacketDecodeAction]`을 반환한다."""

    def handle_message(self, message_input: MacMessageInput) -> InternalResult[MacActionSet]:
        """`InternalResult[MacActionSet]`을 반환한다."""

    def control_session(
        self, request: SessionControlRequest
    ) -> InternalResult[SessionControlResult]:
        """`InternalResult[SessionControlResult]`를 반환한다."""


def _state_commit_failed(exception: BaseException):
    return internal_error(
        Layer.MAC,
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.INTERNAL,
        "MAC state commit이 실패했습니다",
        details=Details.of(
            mac_reason=MacFailureReason.DEPENDENCY_FAILED.value,
            reason_code=MacReasonCode.STATE_COMMIT_FAILED,
            exception_type=type(exception).__name__,
        ),
    )


class Stage4Mac:
    """동결 `MacPort`와 가산 `MacExecutionPort`를 함께 구현하는 Stage 4 facade.

    모든 진입점은 endpoint 직렬화 lock 안에서 coordinator로
    `(result, next_state, batch_or_none)`을 계산한 뒤 다음 순서를 지킨다.

    1. `batch_or_none`이 있으면 sink `submit()`으로 용량·ID를 검사하고 `RESERVED`를 만든다.
    2. 거부되면 상태를 commit하지 않고 원래 Port 호출을 구조화 오류로 끝낸다.
    3. 수락되면 같은 lock 안에서 `commit(next_state)` 뒤 `publish(reservation)`을 호출한다.
    4. commit이 예외로 끝나면 `abort(reservation)`으로 예약을 제거하고 command를 실행하지 않는다.
    """

    __slots__ = (
        "_coordinator",
        "_repository",
        "_sink",
        "_lock",
        "_id_source",
        "_applying",
    )

    def __init__(self, coordinator, repository, sink, lock: RLock, id_source) -> None:
        self._coordinator = coordinator
        self._repository = repository
        self._sink = sink
        self._lock = lock
        self._id_source = id_source
        self._applying = False

    # --- 관찰 (시험·Runtime 진단용) ---

    @property
    def coordinator(self):
        return self._coordinator

    @property
    def repository(self):
        return self._repository

    @property
    def state(self):
        return self._repository.state

    def _synthetic_event_id(self) -> str:
        """`MacPort` 호출에는 공개 event가 없으므로 private synthetic event ID를 만든다.

        이는 Runtime에 `TransferReadyEvent`를 공개한다는 뜻이 아니다.
        """

        return self._id_source.new_event_id()

    def _apply(self, plan, *, receipt: bool = False):
        """`submit -> commit -> publish` 원자 순서를 강제한다."""

        if self._applying:
            return internal_error(
                Layer.MAC,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "MacCommandSink.submit 중 동기 MAC 재진입은 허용되지 않습니다",
                details=Details.of(
                    mac_reason=MacFailureReason.EVENT_INVALID.value,
                    reason_code="SYNCHRONOUS_SINK_REENTRY",
                ),
            )
        self._applying = True
        try:
            return self._apply_once(plan, receipt=receipt)
        finally:
            self._applying = False

    def _apply_once(self, plan, *, receipt: bool = False):
        """재진입 guard 안에서 reservation과 snapshot commit을 수행한다."""

        if plan.batch is None:
            try:
                self._repository.commit(plan.next_state)
            except Exception as error:  # noqa: BLE001 - 구조화 오류로 승격한다.
                return _state_commit_failed(error)
            # 자원 만료처럼 오류 자체가 protocol 상태 전이를 종결하는 경우에도
            # 계산된 immutable snapshot은 먼저 commit해야 같은 오류가 반복되지 않는다.
            if plan.result.is_err:
                return plan.result
            if receipt:
                return Ok(EMPTY_MAC_BATCH_RECEIPT)
            return plan.result
        reservation = self._sink.submit(plan.batch)
        if reservation.is_err:
            # 상한 초과·stop 등으로 거부되면 상태를 commit하지 않는다.
            return reservation
        try:
            self._repository.commit(plan.next_state)
        except Exception as error:  # noqa: BLE001
            self._sink.abort(reservation.value)
            return _state_commit_failed(error)
        self._sink.publish(reservation.value)
        if plan.result.is_err:
            return plan.result
        if receipt:
            return Ok(
                MacBatchReceipt(
                    batch_id=plan.batch.batch_id, command_ids=plan.batch.command_ids()
                )
            )
        return plan.result

    # --- 동결 MacPort ---

    def submit_requirement(self, requirement: TransferRequirement):
        with self._lock:
            plan = self._coordinator.submit_requirement(
                requirement, self._synthetic_event_id()
            )
            return self._apply(plan)

    def handle_recovered_packet(self, recovered: RecoveredPacketSet):
        with self._lock:
            plan = self._coordinator.handle_recovered_packet(recovered)
            return self._apply(plan)

    def handle_message(self, message_input: MacMessageInput):
        with self._lock:
            plan = self._coordinator.handle_message(
                message_input, self._synthetic_event_id()
            )
            return self._apply(plan)

    def control_session(self, request: SessionControlRequest):
        with self._lock:
            plan = self._coordinator.control_session(request)
            return self._apply(plan)

    # --- 가산 MacExecutionPort ---

    def handle_event(self, event: MacEventEnvelope) -> InternalResult[MacBatchReceipt]:
        with self._lock:
            plan = self._coordinator.handle_event(event)
            return self._apply(plan, receipt=True)

    # --- Runtime lifecycle ---

    def begin_running(self):
        """`RUNNING` readiness. 필수 기본 RX window를 만들 수 없으면 거부한다."""

        with self._lock:
            plan = self._coordinator.begin_running(self._synthetic_event_id())
            return self._apply(plan, receipt=True)


__all__ = ("MacPort", "Stage4Mac")
