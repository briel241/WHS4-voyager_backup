"""Stage 4 `MacExecutionPort`와 내부 `MacCommandSink` (`DR-STAGE4-MAC-001`).

두 Port는 동일한 내부 coordinator와 state repository에 위임하며 상태 복사본을 갖지 않는다.
`MacExecutionPort`는 Stage 4 가산 계약이며 동결 `MacPort`를 대체하지 않는다.

`MacCommandSink`는 Stage 4 내부 가산 계약이고 외부 Service API나 Stage 0 공개 Port가 아니다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.link import (
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    ReassemblyControlKind,
    TbbAssemblyControlRequest,
    TransferControlKind,
)
from ..common.contracts.mac_execution import (
    CLEANUP_COMMAND_PAYLOAD_TYPES,
    CancelWakeupCommand,
    InvokeLinkCommand,
    InvokePhyCommand,
    MacBatchReceipt,
    MacCommandBatch,
    MacCommandEnvelope,
    MacEventEnvelope,
    MacOutboxReservation,
    PublishTransferStateCommand,
)
from ..common.results import InternalResult
from .models import PendingCommandClass


@runtime_checkable
class MacExecutionPort(Protocol):
    """clock/timer, dependency 결과, cancel과 stop 같은 비동기 사건 진입점."""

    def handle_event(self, event: MacEventEnvelope) -> InternalResult[MacBatchReceipt]:
        """`InternalResult[MacBatchReceipt]`를 반환한다.

        성공값은 실행 command가 아니라 commit이 끝난 결과를 알리는 식별 receipt다. Runtime은
        receipt를 sink나 outbox에 넣지 않는다.
        """


@runtime_checkable
class MacCommandSink(Protocol):
    """endpoint bounded outbox. `submit` → state commit → `publish`/`abort` 순서다."""

    def submit(self, batch: MacCommandBatch) -> InternalResult[MacOutboxReservation]:
        """용량·ID를 검사하고 `RESERVED` record를 원자적으로 만든다.

        command 실행, event publish, callback 또는 MAC 재호출을 동기 수행해서는 안 되며
        반환한 reservation은 dispatcher에 보이지 않는다.
        """

    def publish(self, reservation: MacOutboxReservation) -> None:
        """이미 검증·예약된 record의 flag만 `READY`로 바꾸는 예외 없는 total operation."""

    def abort(self, reservation: MacOutboxReservation) -> None:
        """state commit 실패 시 `RESERVED` record를 제거하는 예외 없는 operation."""


def is_cleanup_command(command: MacCommandEnvelope) -> bool:
    """`DRAINING` cleanup allowlist 판정 (`DR-STAGE4-MAC-012`)."""

    payload = command.payload
    if not isinstance(payload, CLEANUP_COMMAND_PAYLOAD_TYPES):
        return False
    if isinstance(payload, CancelWakeupCommand):
        return True
    if isinstance(payload, PublishTransferStateCommand):
        from ..common.contracts.service import TERMINAL_TRANSFER_STATES

        return payload.state in TERMINAL_TRANSFER_STATES
    if isinstance(payload, InvokeLinkCommand):
        request = payload.request
        if isinstance(request, LinkTransferControlRequest):
            return request.kind in (
                TransferControlKind.FAIL,
                TransferControlKind.CANCEL,
                TransferControlKind.ABORT_SESSION,
            )
        if isinstance(request, LinkReassemblyControlRequest):
            return request.kind in (
                ReassemblyControlKind.TIMEOUT,
                ReassemblyControlKind.ABORT,
                ReassemblyControlKind.CLOSE,
            )
        if isinstance(request, TbbAssemblyControlRequest):
            return True
    return False


def command_class_of(command: MacCommandEnvelope) -> PendingCommandClass:
    """96 pending dependency quota의 class를 결정한다."""

    payload = command.payload
    if isinstance(payload, InvokePhyCommand) and not payload.is_transmit:
        return PendingCommandClass.RX
    if is_cleanup_command(command):
        return PendingCommandClass.CLEANUP
    return PendingCommandClass.NON_CLEANUP_NON_RX


__all__ = (
    "MacCommandSink",
    "MacExecutionPort",
    "command_class_of",
    "is_cleanup_command",
)
