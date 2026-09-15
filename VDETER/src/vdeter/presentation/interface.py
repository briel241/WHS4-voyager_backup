"""Presentation Port (`stage0_baseline.md` §5.3, 상위 명세 §7.2, §7.3).

Presentation Adapter는 application payload와 R1027 VDE payload 표현 사이의 변환만
소유한다. VDE Segment header, Sequential ID, Type 74/75/76 fragment, Packet, CRC, ACK와
Session 수명은 처리하지 않는다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.presentation import (
    ApplicationData,
    PresentationData,
    PresentationDecodeRequest,
    PresentationEncodeRequest,
)
from ..common.results import InternalResult


@runtime_checkable
class PresentationPort(Protocol):
    """Service ↔ Presentation ↔ Link 경계 계약."""

    def encode_payload(self, request: PresentationEncodeRequest) -> InternalResult[PresentationData]:
        """`InternalResult[PresentationData]`를 반환한다."""

    def decode_payload(self, request: PresentationDecodeRequest) -> InternalResult[ApplicationData]:
        """`InternalResult[ApplicationData]`를 반환한다."""
