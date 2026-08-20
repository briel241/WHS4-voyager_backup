"""VDE Segment 내부 immutable model."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.exceptions import InvalidValueError
from ...common.identifiers import SequentialId


@dataclass(frozen=True, slots=True)
class UnsignedVdeSegment:
    """검증을 마친 Segment Type 0의 payload."""

    sequential_id: SequentialId
    presentation_data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.sequential_id, SequentialId):
            raise InvalidValueError("sequential_id: SequentialId여야 합니다")
        if not isinstance(self.presentation_data, bytes) or len(self.presentation_data) > 65535:
            raise InvalidValueError("presentation_data: 0..65535 bytes여야 합니다")
