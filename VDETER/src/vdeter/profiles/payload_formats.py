"""Payload Format 표준 catalogue (`stage5_service_integration_baseline.md` §7).

운영 registry에는 IALA R1027의 공식 mapping만 등록한다 (`PRES-REQ-01`).

| `payload_format` | VPFI | 형식 식별자 | 운영 | 시험 |
| --- | ---: | --- | --- | --- |
| `x509.certificate_report` | 0 | Message format 1 | 지원 | 지원 |
| `ais.authentication_integrity.scheme1` | 7 | Message format 1, Scheme ID 1 | 지원 | 지원 |
| `test.stage0.opaque` | 268435455 | 없음 | 미등록 | 지원 |
| `electronic_chart.data` | 미확정 | 미확정 | 미등록 (`DR-STAGE5-022`) | 미등록 |

전자해도(`electronic_chart.data`)는 canonical key만 예약하고 registry entry와 codec을
만들지 않는다. submit은 registry lookup 실패로 `UNSUPPORTED_REQUEST`가 된다.

R1027은 `VPFMI`가 아니라 `Message format`이라는 field 명칭을 사용하므로 공통 모델도
`message_format`을 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import PayloadFormat, Vpfi
from .models import RegistryScope, SourceReference, SupportStatus

#: 시험 전용 opaque payload format key. 운영 registry에 등록하지 않는다 (`DR-STAGE5-008`).
TEST_PAYLOAD_FORMAT = "test.stage0.opaque"

#: R1027 Table 3의 시험 전용 범위를 사용하는 opaque 시험 VPFI.
#: 실제 운영 할당값이 아니며 운영 profile에 등록하지 않는다.
TEST_VPFI = 268435455

#: Stage 5 운영 catalogue의 canonical key (`stage5` §7.1).
CERTIFICATE_REPORT_PAYLOAD_FORMAT = "x509.certificate_report"
AIS_SCHEME1_PAYLOAD_FORMAT = "ais.authentication_integrity.scheme1"

#: 향후 호환성을 위해 예약만 하는 key. Stage 5에서는 registry entry가 없다
#: (`DR-STAGE5-022`). 운영·시험 어느 scope에도 등록해서는 안 된다.
ELECTRONIC_CHART_RESERVED_KEY = "electronic_chart.data"

#: Stage 5 Payload Format Registry content version (`stage5` §7.1).
STAGE5_PAYLOAD_CONTENT_VERSION = "stage5-r1027-ed1.1-v1"

#: registry portable codec 참조 (`stage5` §7.1).
OPAQUE_CODEC_REF = "test.stage0.opaque_passthrough"
CERTIFICATE_REPORT_CODEC_REF = "r1027.vpfi0.certificate_report.v1"
AIS_SCHEME1_CODEC_REF = "r1027.vpfi7.authentication_scheme1.v1"

_R1027_TEST = SourceReference(
    document="IALA R1027",
    edition="Ed.1.1",
    location="Annex §2, §3.1, Table 3 (시험 전용 값)",
)
_R1027_CERTIFICATE_REPORT = SourceReference(
    document="IALA R1027",
    edition="Ed.1.1",
    location="Tables 4·6 (VPFI 0, Certificate Report Message format 1)",
)
_R1027_AIS_SCHEME1 = SourceReference(
    document="IALA R1027",
    edition="Ed.1.1",
    location="Table 21 (VPFI 7, Authentication Scheme 1, 672-bit)",
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


def _official_entries(scope: RegistryScope) -> tuple:
    """R1027 공식 mapping 두 entry. 운영·시험 scope에 같은 codec·wire key로 등록한다."""

    return (
        PayloadFormatEntry(
            payload_format=PayloadFormat(CERTIFICATE_REPORT_PAYLOAD_FORMAT),
            vpfi=Vpfi(0),
            scope=scope,
            support_status=SupportStatus.SUPPORTED,
            source=_R1027_CERTIFICATE_REPORT,
            message_format=1,
            codec_ref=CERTIFICATE_REPORT_CODEC_REF,
        ),
        PayloadFormatEntry(
            payload_format=PayloadFormat(AIS_SCHEME1_PAYLOAD_FORMAT),
            vpfi=Vpfi(7),
            scope=scope,
            support_status=SupportStatus.SUPPORTED,
            source=_R1027_AIS_SCHEME1,
            message_format=1,
            codec_ref=AIS_SCHEME1_CODEC_REF,
        ),
    )


def operational_entries() -> tuple:
    """운영 catalogue. R1027 공식 mapping 두 entry만 등록한다 (`stage5` §7.2)."""

    return _official_entries(RegistryScope.OPERATIONAL)


def test_entries() -> tuple:
    """시험 catalogue. 공식 두 entry의 test-scope copy와 opaque test entry."""

    return _official_entries(RegistryScope.TEST) + (
        PayloadFormatEntry(
            payload_format=PayloadFormat(TEST_PAYLOAD_FORMAT),
            vpfi=Vpfi(TEST_VPFI),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=_R1027_TEST,
            message_format=None,
            codec_ref=OPAQUE_CODEC_REF,
        ),
    )


def entries_for(scope: RegistryScope) -> tuple:
    return operational_entries() if scope is RegistryScope.OPERATIONAL else test_entries()
