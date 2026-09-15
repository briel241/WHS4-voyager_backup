"""SC-VDE Position Report MF 1 TEST codec (`payload_extension_baseline.md` §6.5).

프로젝트 TEST/PROJECT 전용 profile이다. carrier는 기존 SC-VDE MF 2/3과 같은 R1027
Ed.1.1 Table 3 Level 2 시험 전용 `FF 7F`이며 공식 OPERATIONAL 할당이 아니다.
Navigation Status부터 RAIM까지는 ITU-R M.1371-6 Table 46을 선택 차용하고 Autonomy
Mode 3 bits는 프로젝트 전용 확장이므로, 전체 형식은 AIS 표준 메시지나 공식 SC-VDE
format이 아니라 AIS-derived TEST/PROJECT profile이다 (§6.5.2, §6.5.14).

```text
application = body[111] | zero pad[1]                            = 14 bytes 고정
wire        = VPFI FF7F[16] | Message Format 01[8] | application = 17 bytes 고정
```

모든 bit는 첫 byte MSB부터 연속 pack하고 signed ROT/coordinate만 지정 폭 two's
complement다. codec은 §6.5.8의 canonical raw 집합(구조 정규성)만 검증한다. 2초 송신
주기, sensor sampling, degree/knot→raw 변환, freshness, 이동의 물리적 가능성,
COG/heading/ROT 상관과 통신 두절 판정은 application 책임이다 (`DR-PAYLOAD-013`).

`DR-PAYLOAD-016` 승인에 따라 addressed(MASS→ROC) `ack_required=false` 전용이며 이
정책은 아래 immutable class metadata로 선언하고 adapter가 강제한다. Presentation은
address kind만 판정하며 addressed station이 실제 ROC인지, endpoint 역할이 MASS인지는
판정하지 않는다 (§6.5.4).

이 unsigned Segment Type 0 profile은 송신자 인증·payload 무결성·replay/freshness
protection을 제공하지 않는다. 실제 선박 위치 판단·감시·충돌회피·제어 입력으로
사용해서는 안 된다 (`payload_extension_baseline.md` §4.4, §6.5.14).
"""

from __future__ import annotations

from ...common.errors import Correlation
from ...common.results import Ok
from .base import (
    WireDiscriminator,
    decode_structure_error,
    encode_structure_error,
    truncated_error,
)
from .sc_vde_route_control import (
    SC_VDE_HEADER_BYTES,
    SC_VDE_LOGICAL_VPFI,
    SC_VDE_WIRE_VPFI,
    _field,
    _LATITUDE_ABS_MAX,
    _LATITUDE_BITS,
    _LATITUDE_NA,
    _LONGITUDE_ABS_MAX,
    _LONGITUDE_BITS,
    _LONGITUDE_NA,
    _validate_coordinate,
)

SC_VDE_POSITION_REPORT_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf1.v1"
SC_VDE_POSITION_REPORT_MESSAGE_FORMAT = 1
SC_VDE_POSITION_REPORT_WIRE_PREFIX = SC_VDE_WIRE_VPFI + bytes(
    (SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,)
)

POSITION_REPORT_BODY_BITS = 111
POSITION_REPORT_PADDING_BITS = 1
POSITION_REPORT_APPLICATION_BYTES = 14
POSITION_REPORT_WIRE_BYTES = POSITION_REPORT_APPLICATION_BYTES + SC_VDE_HEADER_BYTES
_TOTAL_BITS = POSITION_REPORT_APPLICATION_BYTES * 8

#: §6.5.8 canonical raw 허용 집합. bit 폭이 이미 상한인 field(ROT, SOG, Position
#: Accuracy, Timestamp(`DR-PAYLOAD-015`의 0..63 전체 허용), RAIM)는 모든 raw 값이
#: 허용이므로 여기에 없다.
_ALLOWED_NAVIGATION_STATUSES = frozenset((0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15))
_ALLOWED_MANEUVER_INDICATORS = frozenset((0, 1, 2))
_ALLOWED_AUTONOMY_MODES = frozenset((0, 1, 2, 3))
_COG_NA = 3600
_HEADING_MAX = 359
_HEADING_NA = 511


def _validate_position_fields(
    application: bytes, error_factory, correlation: Correlation
):
    """14-byte application의 field·reserved·padding 검증 (§6.5.7~§6.5.8)."""

    bits = int.from_bytes(application, "big")
    navigation_status = _field(bits, _TOTAL_BITS, 0, 4)
    if navigation_status not in _ALLOWED_NAVIGATION_STATUSES:
        return error_factory(
            "Navigation Status reserved 9, 10, 13은 거부합니다",
            correlation=correlation,
            navigation_status=navigation_status,
        )
    # §6.5.13: 오류 세부정보에 실제 위치를 불필요하게 기록하지 않는다. 좌표 거부
    # 오류에는 거부된 raw 값을 details에 담지 않는다 (field 식별은 message로 충분).
    longitude = _field(bits, _TOTAL_BITS, 23, _LONGITUDE_BITS)
    if not _validate_coordinate(
        longitude, _LONGITUDE_BITS, _LONGITUDE_ABS_MAX, _LONGITUDE_NA
    ):
        return error_factory(
            "Longitude는 ±108000000 또는 108600000(N/A)만 허용합니다",
            correlation=correlation,
        )
    latitude = _field(bits, _TOTAL_BITS, 51, _LATITUDE_BITS)
    if not _validate_coordinate(
        latitude, _LATITUDE_BITS, _LATITUDE_ABS_MAX, _LATITUDE_NA
    ):
        return error_factory(
            "Latitude는 ±54000000 또는 54600000(N/A)만 허용합니다",
            correlation=correlation,
        )
    cog = _field(bits, _TOTAL_BITS, 78, 12)
    if cog > _COG_NA:
        return error_factory(
            "COG는 0..3599 또는 3600(N/A)만 허용합니다",
            correlation=correlation,
            cog=cog,
        )
    heading = _field(bits, _TOTAL_BITS, 90, 9)
    if heading > _HEADING_MAX and heading != _HEADING_NA:
        return error_factory(
            "True Heading은 0..359 또는 511(N/A)만 허용합니다",
            correlation=correlation,
            true_heading=heading,
        )
    maneuver = _field(bits, _TOTAL_BITS, 105, 2)
    if maneuver not in _ALLOWED_MANEUVER_INDICATORS:
        return error_factory(
            "Maneuver Indicator reserved 3은 거부합니다",
            correlation=correlation,
            maneuver_indicator=maneuver,
        )
    autonomy = _field(bits, _TOTAL_BITS, 108, 3)
    if autonomy not in _ALLOWED_AUTONOMY_MODES:
        return error_factory(
            "Autonomy Mode reserved 4..7은 거부합니다",
            correlation=correlation,
            autonomy_mode=autonomy,
        )
    if bits & ((1 << POSITION_REPORT_PADDING_BITS) - 1):
        return error_factory(
            "position report 마지막 padding bit는 0이어야 합니다",
            correlation=correlation,
        )
    return Ok(POSITION_REPORT_BODY_BITS)


class ScVdePositionReportCodec:
    """MF 1 위치보고 고정길이 구조 codec. 업무 의미·freshness는 판정하지 않는다."""

    __slots__ = ()

    codec_ref = SC_VDE_POSITION_REPORT_CODEC_REF
    vpfi = SC_VDE_LOGICAL_VPFI
    message_format = SC_VDE_POSITION_REPORT_MESSAGE_FORMAT
    discriminator = WireDiscriminator(prefix=SC_VDE_POSITION_REPORT_WIRE_PREFIX)
    min_wire_length = POSITION_REPORT_WIRE_BYTES
    max_application_bytes = POSITION_REPORT_APPLICATION_BYTES
    max_wire_bytes = POSITION_REPORT_WIRE_BYTES
    #: §6.5.4/§8.1: addressed 전용이며 Link `ack_required=false`만 허용한다. adapter가
    #: 이 immutable metadata로 route/ACK 정책을 codec 호출 전에 강제한다.
    addressed_only = True
    allowed_ack_required = frozenset({False})

    def encoded_size_bound(self, application_length: int):
        if application_length != POSITION_REPORT_APPLICATION_BYTES:
            return encode_structure_error(
                "SC-VDE position report application은 정확히 14 bytes여야 합니다",
                payload_size=application_length,
            )
        return Ok(POSITION_REPORT_WIRE_BYTES)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        validated = _validate_position_fields(
            application_payload, encode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        # 검증된 body bits는 canonicalize 없이 byte-exact로 보존한다 (§6.5.10).
        return Ok(SC_VDE_POSITION_REPORT_WIRE_PREFIX + application_payload)

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < SC_VDE_HEADER_BYTES:
            return truncated_error(
                "SC-VDE position report header 3 bytes가 잘렸습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:SC_VDE_HEADER_BYTES] != SC_VDE_POSITION_REPORT_WIRE_PREFIX:
            return decode_structure_error(
                "SC-VDE position report wire prefix ff 7f 01이 아닙니다",
                correlation=correlation,
            )
        if len(wire) < POSITION_REPORT_WIRE_BYTES:
            return truncated_error(
                "SC-VDE position report wire는 정확히 17 bytes여야 합니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if len(wire) > POSITION_REPORT_WIRE_BYTES:
            return decode_structure_error(
                "position report body 뒤에 trailing bytes가 있습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        application = wire[SC_VDE_HEADER_BYTES:]
        validated = _validate_position_fields(
            application, decode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        return Ok(application)
