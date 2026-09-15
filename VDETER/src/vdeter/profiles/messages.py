"""Message Type 표준 metadata catalogue (`stage0_baseline.md` §7.2).

이 module은 Message Type의 *표준 자료*(Type ID, 출처 표, 지원 상태)만 제공한다. 실제
codec과 codec registry는 Link가 소유한다 (`vdeter.link.messages`). 단계 0은 Message field
codec을 구현하지 않으므로 운영 catalogue를 비워 두고, 단계 1 Link 기준선에서 Type 0, 4,
13, 20, 21, 22, 74, 75, 76, 81, 90, 92, 93의 정확한 표 위치와 함께 등록한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from .models import RegistryScope, SourceReference, SupportStatus

#: 상위 명세 §4.1.2의 구현 대상 Message Type. 단계 1에서 codec과 함께 등록한다.
PLANNED_MESSAGE_TYPES = (0, 4, 13, 20, 21, 22, 74, 75, 76, 81, 90, 92, 93)


@dataclass(frozen=True, slots=True)
class MessageProfileEntry:
    """Message Type 하나의 표준 metadata."""

    message_type: int
    scope: RegistryScope
    support_status: SupportStatus
    source: SourceReference
    name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.message_type, bool) or not isinstance(self.message_type, int):
            raise InvalidValueError("MessageProfileEntry.message_type: 정수여야 합니다")
        if not 0 <= self.message_type <= 255:
            raise InvalidValueError("MessageProfileEntry.message_type: 0..255여야 합니다")
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("MessageProfileEntry.scope: RegistryScope여야 합니다")
        if not isinstance(self.support_status, SupportStatus):
            raise InvalidValueError("MessageProfileEntry.support_status: SupportStatus여야 합니다")
        if not isinstance(self.source, SourceReference):
            raise InvalidValueError("MessageProfileEntry.source: SourceReference여야 합니다")
        for name in ("name", "description"):
            if not isinstance(getattr(self, name), str):
                raise InvalidValueError(f"MessageProfileEntry.{name}: 문자열이어야 합니다")

    def registry_key(self) -> int:
        return self.message_type


def operational_entries() -> tuple:
    """Stage 1에서 승인된 13개 운영 Message metadata."""

    rows = (
        (0, "Media access control", "Table 36", "MediaAccessControlBody"),
        (4, "Resource allocation", "Table 37", "ResourceAllocationBody"),
        (13, "ACK/NACK", "Table 38", "AcknowledgeNackBody"),
        (20, "TBB start fragment", "Table 42", "TbbStartFragmentBody"),
        (21, "TBB continuation fragment", "Table 45", "TbbContinuationFragmentBody"),
        (22, "TBB end fragment", "Table 46", "TbbEndFragmentBody"),
        (74, "Data start fragment", "Table 49", "DataStartFragmentBody"),
        (75, "Data continuation fragment", "Table 50", "DataContinuationFragmentBody"),
        (76, "Data end fragment", "Table 51", "DataEndFragmentBody"),
        (81, "Padding byte", "§A4-4.9.13", "PaddingBody"),
        (90, "Resource request/transmission announcement", "Table 39", "ResourceRequestBody"),
        (92, "Short data with ACK", "Table 40", "ShortDataAckBody"),
        (93, "Short data without ACK", "Table 41", "ShortDataNoAckBody"),
    )
    return tuple(
        MessageProfileEntry(
            message_type=message_type,
            scope=RegistryScope.OPERATIONAL,
            support_status=SupportStatus.SUPPORTED,
            source=SourceReference(
                document="ITU-R M.2092-2",
                edition="02/2026",
                location=location,
            ),
            name=name,
            description=dto_name,
        )
        for message_type, name, location, dto_name in rows
    )


def test_entries() -> tuple:
    """단계 0 registry 생명주기 시험용 catalogue. 규범 Message가 아니다."""

    return (
        MessageProfileEntry(
            message_type=255,
            scope=RegistryScope.TEST,
            support_status=SupportStatus.PLANNED,
            source=SourceReference(
                document="stage0_baseline.md",
                edition="0.3.0",
                location="§5.5 단계 0 Fake/Stub 동작",
            ),
            name="Stage 0 test placeholder",
            description="단계 0 registry 생명주기 시험 전용 항목",
        ),
    )


def entries_for(scope: RegistryScope) -> tuple:
    return operational_entries() if scope is RegistryScope.OPERATIONAL else test_entries()
