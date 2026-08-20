"""Service 계층 내부 model (`stage0_baseline.md` §4.1.1, §8).

공개 경계 DTO는 `common.contracts.service`에 있다 (decision record `DR-STAGE0-001`).
이 module은 Service 계층 안에서만 쓰는 model을 정의한다. 구성 가능한 최대 payload 크기,
terminal 상태 보존 기간과 동시 송신 한도의 실제 값은 단계 5 Service API 기준선에서
확정하며, 여기서는 설정으로 주입되는 표현만 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.contracts.service import TransferState
from ..common.errors import ServiceError
from ..common.exceptions import InvalidValueError
from ..common.identifiers import TransferId
from ..common.time import TimePoint


@dataclass(frozen=True, slots=True)
class ServiceLimits:
    """설정으로 주입되는 Service 한도. 표준값이 아니라 운용 설정이다."""

    max_payload_bytes: int
    max_concurrent_transfers: int

    def __post_init__(self) -> None:
        for name in ("max_payload_bytes", "max_concurrent_transfers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"ServiceLimits.{name}: 양의 정수여야 합니다")


@dataclass(frozen=True, slots=True)
class TransferRecord:
    """송신 요청 하나의 현재 상태 snapshot. runtime 상태이며 설정에 쓰지 않는다."""

    transfer_id: TransferId
    state: TransferState
    updated_at: TimePoint
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("TransferRecord.transfer_id: TransferId여야 합니다")
        if not isinstance(self.state, TransferState):
            raise InvalidValueError("TransferRecord.state: TransferState여야 합니다")
        if not isinstance(self.updated_at, TimePoint):
            raise InvalidValueError("TransferRecord.updated_at: TimePoint여야 합니다")
        if self.error is not None and not isinstance(self.error, ServiceError):
            raise InvalidValueError("TransferRecord.error: ServiceError여야 합니다")

    def with_state(
        self, state: TransferState, updated_at: TimePoint, error: ServiceError | None = None
    ) -> "TransferRecord":
        return TransferRecord(
            transfer_id=self.transfer_id, state=state, updated_at=updated_at, error=error
        )
