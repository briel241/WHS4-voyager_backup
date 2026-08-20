"""Payload Format 표준 catalogue (`stage0_baseline.md` §7.2).

운영 profile에는 IALA R1027에 할당된 값만 등록한다. 단계 0은 실제 VPFI 할당과 codec을
확정하지 않으므로 운영 catalogue는 비어 있고, 시험 catalogue만 test scope VPFI를 갖는다.
지원 Payload Format catalogue와 각 codec은 단계 5 Presentation 기준선이 확정한다.

R1027은 `VPFMI`가 아니라 `Message format`이라는 field 명칭을 사용하므로 공통 모델도
`message_format`을 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import PayloadFormat, Vpfi
from .models import RegistryScope, SourceReference, SupportStatus

#: 단계 0 시험 전용 payload format key. 운영 registry에 등록하지 않는다.
TEST_PAYLOAD_FORMAT = "test.stage0.opaque"

#: R1027 Table 3의 시험 전용 범위를 사용하는 단계 0 시험 VPFI.
#: 실제 운영 할당값이 아니며 운영 profile에 등록하지 않는다.
TEST_VPFI = 268435455

_R1027 = SourceReference(
    document="IALA R1027",
    edition="Ed.1.1",
    location="Annex §2, §3.1, Table 3 (시험 전용 값)",
)


@dataclass(frozen=True, slots=True)
class PayloadFormatEntry:
    """local `payload_format`과 공식 VPFI·`message_format`의 매핑."""

    payload_format: PayloadFormat
    vpfi: Vpfi
    scope: RegistryScope
    support_status: SupportStatus
    source: SourceReference
    message_format: int | None = None
    codec_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.payload_format, PayloadFormat):
            raise InvalidValueError("PayloadFormatEntry.payload_format: PayloadFormat이어야 합니다")
        if not isinstance(self.vpfi, Vpfi):
            raise InvalidValueError("PayloadFormatEntry.vpfi: Vpfi여야 합니다")
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("PayloadFormatEntry.scope: RegistryScope여야 합니다")
        if not isinstance(self.support_status, SupportStatus):
            raise InvalidValueError("PayloadFormatEntry.support_status: SupportStatus여야 합니다")
        if not isinstance(self.source, SourceReference):
            raise InvalidValueError("PayloadFormatEntry.source: SourceReference여야 합니다")
        if self.message_format is not None:
            if isinstance(self.message_format, bool) or not isinstance(self.message_format, int):
                raise InvalidValueError("PayloadFormatEntry.message_format: 정수여야 합니다")
            if self.message_format < 0:
                raise InvalidValueError("PayloadFormatEntry.message_format: 0 이상이어야 합니다")
        if not isinstance(self.codec_ref, str):
            raise InvalidValueError("PayloadFormatEntry.codec_ref: 문자열이어야 합니다")

    def registry_key(self) -> str:
        return self.payload_format.value

    def wire_key(self) -> tuple:
        """수신 lookup용 `(vpfi, message_format)` key."""

        return (self.vpfi.value, self.message_format)


def operational_entries() -> tuple:
    """운영 catalogue.

    단계 0에서는 공식 VPFI 할당과 codec을 확정하지 않으므로 비어 있다. 임의 VPFI를
    운영 profile에 등록하지 않는다 (`PRES-REQ-01`).
    """

    return ()


def test_entries() -> tuple:
    """단계 0 interface 시험 전용 catalogue. 운영에 사용하지 않는다."""

    return (
        PayloadFormatEntry(
            payload_format=PayloadFormat(TEST_PAYLOAD_FORMAT),
            vpfi=Vpfi(TEST_VPFI),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.PLANNED,
            source=_R1027,
            message_format=None,
            codec_ref="test.stage0.opaque_passthrough",
        ),
    )


def entries_for(scope: RegistryScope) -> tuple:
    return operational_entries() if scope is RegistryScope.OPERATIONAL else test_entries()
