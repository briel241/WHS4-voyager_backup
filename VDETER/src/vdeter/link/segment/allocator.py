"""Endpoint-local 14-bit Sequential ID 순환 할당기."""

from __future__ import annotations

from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import SEQUENTIAL_ID_MAX, SequentialId, TransferId
from ...common.results import InternalResult, Ok, internal_error

SEQUENTIAL_ID_CAPACITY = SEQUENTIAL_ID_MAX + 1


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    transfer_id: TransferId | None = None,
    retryable: bool = False,
    **details,
):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        retryable=retryable,
        correlation=Correlation(transfer_id=transfer_id),
        details=Details.of(**details) if details else Details(),
    )


class SequentialIdAllocator:
    """Transfer가 활성인 동안 같은 Sequential ID를 보존한다.

    새 endpoint instance는 항상 후보 0에서 시작한다. ``release_after_terminal``은 상위
    송신-context 관리자가 COMPLETE/FAIL/CANCEL/ABORT_SESSION을 확정한 뒤에만 호출한다.
    """

    __slots__ = ("_next_candidate", "_by_transfer", "_active_values")

    def __init__(self) -> None:
        self._next_candidate = 0
        self._by_transfer: dict[TransferId, SequentialId] = {}
        self._active_values: set[int] = set()

    @property
    def active_count(self) -> int:
        return len(self._by_transfer)

    def reserve_for_transfer(
        self,
        transfer_id: TransferId,
        caller_sequential_id: SequentialId | None = None,
    ) -> InternalResult[SequentialId]:
        """최초 build용 ID를 할당하고 후속 fragment/retry에는 같은 ID를 반환한다."""

        if not isinstance(transfer_id, TransferId):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "TransferId가 필요합니다",
            )
        if caller_sequential_id is not None:
            return _error(
                ErrorCode.LINK_SEQUENTIAL_ID_CALLER_FORBIDDEN,
                ErrorCategory.VALIDATION,
                "Sequential ID는 Link만 할당할 수 있습니다",
                transfer_id=transfer_id,
            )

        existing = self._by_transfer.get(transfer_id)
        if existing is not None:
            return Ok(existing)
        if len(self._by_transfer) == SEQUENTIAL_ID_CAPACITY:
            return _error(
                ErrorCode.LINK_SEQUENTIAL_ID_EXHAUSTED,
                ErrorCategory.RESOURCE,
                "활성 Sequential ID 공간이 모두 사용 중입니다",
                transfer_id=transfer_id,
                retryable=True,
                active_count=len(self._by_transfer),
                capacity=SEQUENTIAL_ID_CAPACITY,
            )

        for _ in range(SEQUENTIAL_ID_CAPACITY):
            candidate = self._next_candidate
            self._next_candidate = (candidate + 1) % SEQUENTIAL_ID_CAPACITY
            if candidate in self._active_values:
                continue
            allocated = SequentialId(candidate)
            self._by_transfer[transfer_id] = allocated
            self._active_values.add(candidate)
            return Ok(allocated)

        # active_count 검사와 같은 lock domain에서 호출되는 endpoint-local component이므로
        # 정상 실행에서는 도달할 수 없다.
        return _error(
            ErrorCode.LINK_SEQUENTIAL_ID_EXHAUSTED,
            ErrorCategory.RESOURCE,
            "Sequential ID 후보를 찾지 못했습니다",
            transfer_id=transfer_id,
            retryable=True,
            active_count=len(self._by_transfer),
            capacity=SEQUENTIAL_ID_CAPACITY,
        )

    def release_after_terminal(self, transfer_id: TransferId) -> InternalResult[SequentialId]:
        """확정된 terminal 송신 context와 결합된 ID를 함께 해제한다."""

        return self._release(transfer_id)

    def discard_uncommitted(self, transfer_id: TransferId) -> InternalResult[SequentialId]:
        """Roll back an ID when the first Packet build did not succeed.

        This is intentionally separate from ``release_after_terminal``: no active
        transfer context exists yet, so the operation is not a terminal release.
        """

        return self._release(transfer_id)

    def reset(self) -> None:
        """Drop endpoint-local allocator state during an endpoint restart."""

        self._next_candidate = 0
        self._by_transfer.clear()
        self._active_values.clear()

    def _release(self, transfer_id: TransferId) -> InternalResult[SequentialId]:

        if not isinstance(transfer_id, TransferId):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "TransferId가 필요합니다",
            )
        allocated = self._by_transfer.pop(transfer_id, None)
        if allocated is None:
            return _error(
                ErrorCode.LINK_UNKNOWN_TRANSFER,
                ErrorCategory.PROTOCOL,
                "활성 Sequential ID가 없는 transfer입니다",
                transfer_id=transfer_id,
            )
        self._active_values.remove(allocated.value)
        return Ok(allocated)
