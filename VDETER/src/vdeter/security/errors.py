"""Sanitized Presentation-layer security errors."""

from ..common.errors import (
    EMPTY_CORRELATION, EMPTY_DETAILS, Correlation, Details, ErrorCategory,
    ErrorCode, Layer,
)
from ..common.results import internal_error


class SecurityFault(Exception):
    def __init__(self, code: str, message: str = "Security operation failed") -> None:
        self.code = code
        super().__init__(message)


_RESOURCE_CODES = frozenset((
    ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE,
    ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED,
    ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE,
))
_VALID_CODES = frozenset(
    value for name, value in vars(ErrorCode).items() if name.startswith("PRESENTATION_SECURITY_")
)


def presentation_security_error(
    code: str, category: ErrorCategory, message: str, *,
    correlation: Correlation = EMPTY_CORRELATION, details: Details = EMPTY_DETAILS,
):
    if code not in _VALID_CODES:
        raise ValueError("Not a Presentation security error code")
    if not isinstance(details, Details):
        raise TypeError("Security details must be validated Details")
    return internal_error(
        Layer.PRESENTATION, code, category, message,
        correlation=correlation, details=details,
    )


def fault_result(fault: SecurityFault):
    category = ErrorCategory.RESOURCE if fault.code in _RESOURCE_CODES else ErrorCategory.PROTOCOL
    if fault.code in (ErrorCode.PRESENTATION_TRUNCATED, ErrorCode.PRESENTATION_STRUCTURE_INVALID):
        return internal_error(Layer.PRESENTATION, fault.code, category, str(fault))
    return presentation_security_error(fault.code, category, str(fault))
