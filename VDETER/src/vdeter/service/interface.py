"""Service 공개 Port (`stage0_baseline.md` §5.3, 상위 명세 §7.1).

Service API는 MASS/ROC application과 VDE-TER 스택 사이의 공개 경계다. 즉시 결과는
`ServiceResult`로 반환하고 수신 데이터와 상태 변경은 `ServiceEvent`로 통지한다.
event 전달 매체(callback, queue, stream, RPC)는 단계 0에서 정하지 않는다.

Service API는 Message Type, Link ID, CRC, slot 또는 재전송 횟수를 결정하지 않으며 내부
식별자를 Client에 노출하지 않는다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.service import (
    CancelAccepted,
    CancelTransferRequest,
    ServiceStartRequest,
    ServiceStarted,
    ServiceStopRequest,
    ServiceStopped,
    SubmitTransferRequest,
    TransferAccepted,
    TransferStatusSnapshot,
    TransferStatusRequest,
)
from ..common.results import ServiceResult


@runtime_checkable
class VdeService(Protocol):
    """Client ↔ Service 공개 계약."""

    def start(self, request: ServiceStartRequest) -> ServiceResult[ServiceStarted]:
        """`ServiceResult[ServiceStarted]`를 반환한다."""

    def stop(self, request: ServiceStopRequest) -> ServiceResult[ServiceStopped]:
        """`ServiceResult[ServiceStopped]`를 반환한다."""

    def submit(self, request: SubmitTransferRequest) -> ServiceResult[TransferAccepted]:
        """`ServiceResult[TransferAccepted]`를 반환한다."""

    def cancel(self, request: CancelTransferRequest) -> ServiceResult[CancelAccepted]:
        """`ServiceResult[CancelAccepted]`를 반환한다."""

    def status(self, request: TransferStatusRequest) -> ServiceResult[TransferStatusSnapshot]:
        """`ServiceResult[TransferStatusSnapshot]`를 반환한다."""
