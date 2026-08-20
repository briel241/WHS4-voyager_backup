"""Presentation 계층 내부 model (`stage0_baseline.md` §4.1.1).

공개 경계 DTO는 `common.contracts.presentation`에 있다 (decision record `DR-STAGE0-001`).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError


@dataclass(frozen=True, slots=True)
class PresentationLimits:
    """설정으로 주입되는 Presentation 한도. 표준값이 아니다."""

    max_application_payload_bytes: int

    def __post_init__(self) -> None:
        value = self.max_application_payload_bytes
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidValueError(
                "PresentationLimits.max_application_payload_bytes: 양의 정수여야 합니다"
            )
