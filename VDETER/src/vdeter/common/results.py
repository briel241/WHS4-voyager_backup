"""동기 결과 type (`stage0_baseline.md` §5.2).

요청의 즉시 validation, 접수 또는 거부는 typed `Result`로 반환한다. 내부 계층 계약은
`InternalResult`, Service 공개 계약은 `ServiceResult`를 사용하며 두 계약은 서로 다른
오류 type을 갖는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar, Union

from .errors import (
    Correlation,
    Details,
    EMPTY_CORRELATION,
    EMPTY_DETAILS,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
    ServiceError,
    ServiceErrorCategory,
)
from .exceptions import VdeterValidationError

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """성공 결과."""

    value: T

    @property
    def is_ok(self) -> bool:
        return True

    @property
    def is_err(self) -> bool:
        return False

    def unwrap(self) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    """구조화된 실패 결과."""

    error: E

    @property
    def is_ok(self) -> bool:
        return False

    @property
    def is_err(self) -> bool:
        return True

    def unwrap(self):
        raise AssertionError(f"Err 결과에서 unwrap을 호출했습니다: {self.error!r}")


Result = Union[Ok[T], Err[E]]
InternalResult = Union[Ok[T], Err[InternalProtocolError]]
ServiceResult = Union[Ok[T], Err[ServiceError]]


def ok(value: T) -> Ok[T]:
    return Ok(value)


def internal_error(
    layer: Layer,
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    correlation: Correlation = EMPTY_CORRELATION,
    details: Details = EMPTY_DETAILS,
) -> Err[InternalProtocolError]:
    """구조화된 내부 오류 결과를 만든다."""

    return Err(
        InternalProtocolError(
            layer=layer,
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            correlation=correlation,
            details=details,
        )
    )


def service_error(
    code: str,
    category: ServiceErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    transfer_id=None,
    details: Details = EMPTY_DETAILS,
) -> Err[ServiceError]:
    """구조화된 Service 공개 오류 결과를 만든다."""

    return Err(
        ServiceError(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            transfer_id=transfer_id,
            details=details,
        )
    )


def try_call(
    layer: Layer,
    code: str,
    factory: Callable[[], T],
    *,
    category: ErrorCategory = ErrorCategory.VALIDATION,
    correlation: Correlation = EMPTY_CORRELATION,
    details: Details = EMPTY_DETAILS,
) -> Result:
    """계층 내부 validation 예외를 경계용 구조화 오류로 변환한다.

    programming error(`VdeterInternalError`, 그 밖의 예외)는 잡지 않는다. 이런 오류는
    성공이나 입력 오류로 위장하지 않고 호출자가 endpoint를 `UNHEALTHY`로 전환하도록
    그대로 전파한다.
    """

    try:
        return Ok(factory())
    except VdeterValidationError as error:
        return internal_error(
            layer,
            code,
            category,
            str(error) or ErrorCode.INVALID_VALUE,
            correlation=correlation,
            details=details,
        )
