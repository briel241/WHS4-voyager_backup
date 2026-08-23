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


@dataclass(frozen=True, slots=True)
class Stage5ServicePolicy:
    """Stage 5 wiring이 주입하는 immutable 내부 정책 값 (`stage5` §10, `DR-STAGE5-014`).

    공개 config schema를 확장하지 않는다. Stage 5 적합 profile에서는 runtime override할 수
    없으며 아래 기본값이 승인된 규범값이다.
    """

    max_application_bytes: int = 65_531
    max_encoded_bytes: int = 65_535
    max_nonterminal_transfers: int = 8
    max_queued_transfers: int = 8
    terminal_retention_nanoseconds: int = 15 * 60 * 1_000_000_000
    terminal_retention_max_records: int = 1_024
    event_queue_capacity: int = 256
    event_general_capacity: int = 240
    event_cleanup_reserve: int = 16
    state_chain_tokens_per_transfer: int = 2
    event_payload_budget_bytes: int = 16 * 1024 * 1024
    event_publish_max_retries: int = 3

    def __post_init__(self) -> None:
        for name in (
            "max_application_bytes",
            "max_encoded_bytes",
            "max_nonterminal_transfers",
            "max_queued_transfers",
            "terminal_retention_nanoseconds",
            "terminal_retention_max_records",
            "event_queue_capacity",
            "event_general_capacity",
            "event_cleanup_reserve",
            "state_chain_tokens_per_transfer",
            "event_payload_budget_bytes",
            "event_publish_max_retries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"Stage5ServicePolicy.{name}: 양의 정수여야 합니다")
        if self.event_general_capacity + self.event_cleanup_reserve != self.event_queue_capacity:
            raise InvalidValueError(
                "Stage5ServicePolicy: 일반 영역과 cleanup reserve의 합은 총 event 상한이어야 합니다"
            )
        if self.max_queued_transfers > self.max_nonterminal_transfers:
            raise InvalidValueError(
                "Stage5ServicePolicy: QUEUED 상한은 nonterminal 상한 안이어야 합니다"
            )
        if self.max_application_bytes + 4 > self.max_encoded_bytes:
            raise InvalidValueError(
                "Stage5ServicePolicy: application 상한이 encoded 상한과 모순됩니다"
            )
