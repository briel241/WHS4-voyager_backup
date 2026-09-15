"""Payload Format 표준 catalogue (`stage5_service_integration_baseline.md` §7,
`payload_extension_baseline.md` §5~§7).

운영 registry에는 IALA R1027의 공식 mapping만 등록한다 (`PRES-REQ-01`).

| `payload_format` | VPFI | 형식 식별자 | 운영 | 시험 |
| --- | ---: | --- | --- | --- |
| `x509.certificate_report` | 0 | Message format 1 | 지원 | 지원 |
| `ais.authentication_integrity.scheme1` | 7 | Message format 1, Scheme ID 1 | 지원 | 지원 |
| `test.stage0.opaque` | 268435455 | 없음 | 미등록 | 지원 |
| `sc_vde.position_report` | 16383 (wire `ff 7f`) | Message format 1 | 미등록 | 지원 |
| `sc_vde.route_control.command` | 16383 (wire `ff 7f`) | Message format 2 | 미등록 | 지원 |
| `sc_vde.route_control.ack` | 16383 (wire `ff 7f`) | Message format 3 | 미등록 | 지원 |
| `electronic_chart.data` | 미확정 | 미확정 | 미등록 (`DR-STAGE5-022`) | 미등록 |

전자해도(`electronic_chart.data`)는 canonical key만 예약하고 registry entry와 codec을
만들지 않는다. submit은 registry lookup 실패로 `UNSUPPORTED_REQUEST`가 된다.

SC-VDE 세 entry는 프로젝트 TEST profile이다 (`payload_extension_baseline.md` §6.4,
§6.5). carrier는 R1027 Ed.1.1 Table 3의 Level 2 시험 전용 `FF 7F`이며, 공식 운영
할당이 아니므로 OPERATIONAL registry에 등록해서는 안 된다. logical registry 값
16383은 프로젝트 정규화 값이고 wire field는 exact `ff 7f` 16 bits다. source의 임시
`FF 01`과 logical 정수 직렬화 `3f ff`는 supported alias/fallback이 아니다.
MF 1 위치보고는 M.1371-6 Table 46을 선택 차용한 AIS-derived TEST/PROJECT profile로
AIS 표준 적합성을 주장하지 않는다.

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

#: 승인된 SC-VDE TEST profile의 canonical key
#: (`payload_extension_baseline.md` §6.4.1, §6.5.1, `DR-PAYLOAD-001`).
SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT = "sc_vde.position_report"
SC_VDE_COMMAND_PAYLOAD_FORMAT = "sc_vde.route_control.command"
SC_VDE_ACK_PAYLOAD_FORMAT = "sc_vde.route_control.ack"

#: SC-VDE TEST profile의 프로젝트 정규화 logical VPFI (`payload_extension_baseline.md`
#: §6.4.2, `DR-PAYLOAD-003`). wire field는 exact `ff 7f` 16 bits이며 이 정수를
#: big-endian `3f ff`로 직렬화해서는 안 된다.
SC_VDE_LOGICAL_VPFI = 16383
SC_VDE_POSITION_REPORT_MESSAGE_FORMAT = 1
SC_VDE_COMMAND_MESSAGE_FORMAT = 2
SC_VDE_ACK_MESSAGE_FORMAT = 3

#: Payload 확장 Payload Format Registry content version
#: (`payload_extension_baseline.md` §7.3, `DR-PAYLOAD-017` 선택 A). 두 scope가 같은
#: content version을 사용하므로 OPERATIONAL fingerprint도 함께 갱신된다.
STAGE5_PAYLOAD_CONTENT_VERSION = "payload-extension-project-sc-vde-family-v2"

#: registry portable codec 참조 (`stage5` §7.1, `payload_extension_baseline.md` §7.1).
OPAQUE_CODEC_REF = "test.stage0.opaque_passthrough"
CERTIFICATE_REPORT_CODEC_REF = "r1027.vpfi0.certificate_report.v1"
AIS_SCHEME1_CODEC_REF = "r1027.vpfi7.authentication_scheme1.v1"
SC_VDE_POSITION_REPORT_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf1.v1"
SC_VDE_COMMAND_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf2.v1"
SC_VDE_ACK_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf3.v1"

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
#: MF 1 위치보고 entry의 project SourceReference. field 값은
#: `payload_extension_baseline.md` §7.1의 exact 값이며 fingerprint 입력이므로 바꾸면
#: 승인된 기대 fingerprint와 달라진다. MF 2/3과 다른 SourceReference를 사용한다.
_PROJECT_SC_VDE_POSITION_REPORT = SourceReference(
    document="VDETER Project SC-VDE Position Report Profile",
    edition="v1",
    location=(
        "payload_extension_baseline §4.4, §6.5; carrier IALA R1027 Ed.1.1 Table 3; "
        "AIS-derived fields ITU-R M.1371-6 Table 46; schema position-report source "
        "sha256:7e826a688fafb694d4e7a4985cf68ebf09df1e38863fd1c46f1b5de832aa6f53; "
        "Autonomy Mode project-defined; unsigned TEST-only, no real-vessel control"
    ),
)
#: MF 2/3 두 entry가 공유하는 project SourceReference. field 값은
#: `payload_extension_baseline.md` §7.1의 exact 값이며 fingerprint 입력이므로 바꾸면
#: 승인된 기대 fingerprint와 달라진다.
_PROJECT_SC_VDE_ROUTE_CONTROL = SourceReference(
    document="VDETER Project SC-VDE Route Control Profile",
    edition="v1",
    location=(
        "payload_extension_baseline §4.4, §6.4; carrier IALA R1027 Ed.1.1 Table 3; "
        "schema SC-VDE-005 sha256:"
        "c55f908c869880c60cf7a84b8c00c1d778a7f0c0a30cd310e908374e5ad99fcd"
        " and SC-VDE-006 sha256:"
        "c157fcea6193209811b54621faa3aee1a9201a414b53ff9d4af4f0f9df84cf95"
        "; unsigned TEST-only, no real-vessel control"
    ),
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
    """시험 catalogue. 공식 두 entry의 test-scope copy, opaque test entry와 승인된
    SC-VDE TEST/PROJECT entry 세 개 (`payload_extension_baseline.md` §7.1)."""

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
        PayloadFormatEntry(
            payload_format=PayloadFormat(SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT),
            vpfi=Vpfi(SC_VDE_LOGICAL_VPFI),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=_PROJECT_SC_VDE_POSITION_REPORT,
            message_format=SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,
            codec_ref=SC_VDE_POSITION_REPORT_CODEC_REF,
        ),
        PayloadFormatEntry(
            payload_format=PayloadFormat(SC_VDE_COMMAND_PAYLOAD_FORMAT),
            vpfi=Vpfi(SC_VDE_LOGICAL_VPFI),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=_PROJECT_SC_VDE_ROUTE_CONTROL,
            message_format=SC_VDE_COMMAND_MESSAGE_FORMAT,
            codec_ref=SC_VDE_COMMAND_CODEC_REF,
        ),
        PayloadFormatEntry(
            payload_format=PayloadFormat(SC_VDE_ACK_PAYLOAD_FORMAT),
            vpfi=Vpfi(SC_VDE_LOGICAL_VPFI),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=_PROJECT_SC_VDE_ROUTE_CONTROL,
            message_format=SC_VDE_ACK_MESSAGE_FORMAT,
            codec_ref=SC_VDE_ACK_CODEC_REF,
        ),
    )


def entries_for(scope: RegistryScope) -> tuple:
    return operational_entries() if scope is RegistryScope.OPERATIONAL else test_entries()
