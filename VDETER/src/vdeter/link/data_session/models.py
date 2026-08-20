"""Data Session 내부 immutable 결과 model."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.contracts.link import DataFragmentRef
from ...common.contracts.message import (
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
)
from ...common.exceptions import InvalidValueError
from ..packet.models import PacketBuildOutput

DataFragmentBody = DataStartFragmentBody | DataContinuationFragmentBody | DataEndFragmentBody


@dataclass(frozen=True, slots=True)
class DataFragmentBuildOutput:
    body: DataFragmentBody
    packet: PacketBuildOutput
    fragment: DataFragmentRef
    segment_offset: int
    segment_length: int
    segment_complete: bool

    def __post_init__(self) -> None:
        if not isinstance(
            self.body,
            (DataStartFragmentBody, DataContinuationFragmentBody, DataEndFragmentBody),
        ):
            raise InvalidValueError("body: Type 74/75/76 body여야 합니다")
        if not isinstance(self.packet, PacketBuildOutput):
            raise InvalidValueError("packet: PacketBuildOutput이어야 합니다")
        if not isinstance(self.fragment, DataFragmentRef):
            raise InvalidValueError("fragment: DataFragmentRef여야 합니다")
        if (
            isinstance(self.segment_offset, bool)
            or not isinstance(self.segment_offset, int)
            or not 0 <= self.segment_offset <= 65538
        ):
            raise InvalidValueError("segment_offset: 0..65538 정수여야 합니다")
        if (
            isinstance(self.segment_length, bool)
            or not isinstance(self.segment_length, int)
            or not 1 <= self.segment_length <= 65539
        ):
            raise InvalidValueError("segment_length: 1..65539 정수여야 합니다")
        if not isinstance(self.segment_complete, bool):
            raise InvalidValueError("segment_complete: bool이어야 합니다")
        if self.segment_length != len(self.body.fragment_payload):
            raise InvalidValueError("segment_length와 fragment_payload 길이가 다릅니다")
