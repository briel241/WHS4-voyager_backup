"""SC-VDE Position Report TEST codec (제안 — 링크 팀 검토·이관용).

⚠ 이 파일은 **응용 팀이 만든 참고 구현**이다. VDETER 저장소에 아직 들어가 있지 않다.
   링크 팀이 검토한 뒤 `VDETER/src/vdeter/presentation/codecs/`로 옮기고 registry에
   등록하면 된다. 그 전까지는 동작하지 않는다(우리 시험만 이 파일을 직접 부른다).

구조는 승인된 `sc_vde_route_control.py`를 그대로 따랐다. 같은 carrier(`FF 7F`)의
비어 있는 **Message Format 1** 자리를 쓴다.

```text
position report wire = FF 7F | 01 | body[111] | zero pad[1]   = 17 bytes 고정
application bytes    = body[111] + zero pad[1]                = 14 bytes 고정
```

노선(MF 2)·ACK(MF 3)과 달리 길이가 **고정**이라 검증이 더 단순하다.

근거는 AIS Position Report Class A(ITU-R M.1371 Message 1/2/3)의 Common Navigation
Block이다. 전송 계층 필드(Message Type·Repeat Indicator·MMSI·Radio Status)는 VDE 링크
계층 Source ID와 중복이므로 빼고, Autonomy Mode 3 bit만 본 프로젝트가 새로 정의했다.

codec은 구조 정규성(길이·padding·필드 raw 범위)만 본다. 위치의 사실 여부, 항적
연속성, 두절 판정, 자율운항 모드의 업무 해석은 application 책임이다.

**broadcast**: 위치보고는 AIS와 같이 브로드캐스트로 나간다. 그래서 노선·조종과 달리
`addressed_only = False`다. 현재 VDETER는 MASS-originated broadcast를 지원하지 않으므로,
이 codec이 등록되어도 그 제약이 먼저 풀려야 실제로 쓸 수 있다.

이 unsigned Segment Type 0 profile은 송신자 인증·무결성·replay protection을 제공하지
않는다. 격리된 시험·데모 전용이다.
"""

from __future__ import annotations

# ── 이관 시 아래 import를 VDETER 상대경로로 바꾼다 ────────────────────────
#   from ...common.errors import Correlation
#   from ...common.results import Ok
#   from .base import (WireDiscriminator, decode_structure_error,
#                      encode_structure_error, truncated_error)
# 이 파일은 VDETER 밖에 있으므로 절대 import로 둔다.
from vdeter.common.errors import Correlation
from vdeter.common.results import Ok
from vdeter.presentation.codecs.base import (
    WireDiscriminator,
    decode_structure_error,
    encode_structure_error,
    truncated_error,
)

SC_VDE_POSITION_REPORT_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf1.v1"

#: 노선·조종과 같은 carrier. `sc_vde_route_control.py`의 값과 동일해야 한다.
SC_VDE_LOGICAL_VPFI = 16383
SC_VDE_WIRE_VPFI = bytes((0xFF, 0x7F))
SC_VDE_POSITION_REPORT_MESSAGE_FORMAT = 1
SC_VDE_POSITION_REPORT_WIRE_PREFIX = SC_VDE_WIRE_VPFI + bytes(
    (SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,)
)
SC_VDE_HEADER_BYTES = 3

POSITION_REPORT_BODY_BITS = 111
POSITION_REPORT_PADDING_BITS = 1
POSITION_REPORT_APPLICATION_BYTES = 14
POSITION_REPORT_WIRE_BYTES = SC_VDE_HEADER_BYTES + POSITION_REPORT_APPLICATION_BYTES

#: 좌표 N/A raw 값 (AIS와 동일: 경도 181°, 위도 91°)
LONGITUDE_NOT_AVAILABLE = 108_600_000
LATITUDE_NOT_AVAILABLE = 54_600_000
LONGITUDE_ABS_MAX = 108_000_000   # 180°
LATITUDE_ABS_MAX = 54_000_000     # 90°

COG_NOT_AVAILABLE = 3600
TRUE_HEADING_NOT_AVAILABLE = 511
AUTONOMY_MODE_MAX = 3             # 0=unknown 1=RC 2=MON 3=AUT, 4~7 예약


# ── bit 읽기 (첫 byte MSB부터 연속) ────────────────────────────────────────


def _read_uint(data: bytes, offset: int, width: int) -> int:
    value = 0
    for index in range(offset, offset + width):
        byte = data[index >> 3]
        bit = (byte >> (7 - (index & 7))) & 1
        value = (value << 1) | bit
    return value


def _read_int(data: bytes, offset: int, width: int) -> int:
    raw = _read_uint(data, offset, width)
    sign = 1 << (width - 1)
    return raw - (1 << width) if raw & sign else raw


# ── 필드 검증 (구조 정규성만) ──────────────────────────────────────────────

#: (이름, 시작 bit, 폭, signed 여부) — 명세 표와 같은 순서다.
_FIELDS = (
    ("navigation_status", 0, 4, False),
    ("rate_of_turn", 4, 8, True),
    ("sog", 12, 10, False),
    ("position_accuracy", 22, 1, False),
    ("longitude", 23, 28, True),
    ("latitude", 51, 27, True),
    ("cog", 78, 12, False),
    ("true_heading", 90, 9, False),
    ("timestamp", 99, 6, False),
    ("maneuver_indicator", 105, 2, False),
    ("raim", 107, 1, False),
    ("autonomy_mode", 108, 3, False),
)


def read_fields(application: bytes) -> dict:
    """application bytes를 필드 raw 값 dict로 편다. 검증은 하지 않는다."""

    out = {}
    for name, offset, width, signed in _FIELDS:
        out[name] = (
            _read_int(application, offset, width)
            if signed
            else _read_uint(application, offset, width)
        )
    return out


def _validate_fields(application: bytes, error_factory, correlation: Correlation):
    """§6.4.8과 같은 방식의 canonical raw 허용 집합 검증."""

    # 마지막 byte의 padding 1 bit는 0이어야 한다.
    padding = _read_uint(application, POSITION_REPORT_BODY_BITS, POSITION_REPORT_PADDING_BITS)
    if padding != 0:
        return error_factory(
            "Position Report padding bit는 0이어야 합니다",
            correlation=correlation,
            padding=padding,
        )

    f = read_fields(application)

    longitude = f["longitude"]
    if longitude != LONGITUDE_NOT_AVAILABLE and not (
        -LONGITUDE_ABS_MAX <= longitude <= LONGITUDE_ABS_MAX
    ):
        return error_factory(
            "Longitude raw가 허용 범위 밖입니다",
            correlation=correlation,
            longitude=longitude,
        )

    latitude = f["latitude"]
    if latitude != LATITUDE_NOT_AVAILABLE and not (
        -LATITUDE_ABS_MAX <= latitude <= LATITUDE_ABS_MAX
    ):
        return error_factory(
            "Latitude raw가 허용 범위 밖입니다",
            correlation=correlation,
            latitude=latitude,
        )

    if f["cog"] > COG_NOT_AVAILABLE:
        return error_factory(
            "COG raw는 0..3600이어야 합니다 (3600=N/A)",
            correlation=correlation,
            cog=f["cog"],
        )

    heading = f["true_heading"]
    if heading != TRUE_HEADING_NOT_AVAILABLE and heading > 359:
        return error_factory(
            "True Heading raw는 0..359 또는 511(N/A)이어야 합니다",
            correlation=correlation,
            true_heading=heading,
        )

    if f["autonomy_mode"] > AUTONOMY_MODE_MAX:
        return error_factory(
            "Autonomy Mode raw는 0..3이어야 합니다 (4~7 예약)",
            correlation=correlation,
            autonomy_mode=f["autonomy_mode"],
        )

    # Navigation Status(4)·Rate of Turn(8 signed)·SOG(10)·Position Accuracy(1)·
    # Timestamp(6)·Maneuver Indicator(2)·RAIM(1)은 폭 안의 모든 값이 AIS에서
    # 의미를 갖는다. 구조 검증 대상이 아니다.
    return Ok(None)


class ScVdePositionReportCodec:
    """MF 1 위치보고 구조 codec. 길이가 14 bytes로 고정이다."""

    __slots__ = ()

    codec_ref = SC_VDE_POSITION_REPORT_CODEC_REF
    vpfi = SC_VDE_LOGICAL_VPFI
    message_format = SC_VDE_POSITION_REPORT_MESSAGE_FORMAT
    discriminator = WireDiscriminator(prefix=SC_VDE_POSITION_REPORT_WIRE_PREFIX)
    min_wire_length = POSITION_REPORT_WIRE_BYTES
    max_application_bytes = POSITION_REPORT_APPLICATION_BYTES
    max_wire_bytes = POSITION_REPORT_WIRE_BYTES
    #: 위치보고는 AIS와 같이 broadcast로 나간다. 노선·조종(addressed 전용)과 다르다.
    #: 현재 VDETER는 MASS-originated broadcast 미지원이므로 그 제약이 먼저 풀려야 한다.
    addressed_only = False

    def encoded_size_bound(self, application_length: int):
        if application_length != POSITION_REPORT_APPLICATION_BYTES:
            return encode_structure_error(
                "Position Report application은 정확히 14 bytes여야 합니다",
                payload_size=application_length,
            )
        return Ok(POSITION_REPORT_WIRE_BYTES)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        validated = _validate_fields(
            application_payload, encode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        return Ok(SC_VDE_POSITION_REPORT_WIRE_PREFIX + bytes(application_payload))

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < SC_VDE_HEADER_BYTES:
            return truncated_error(
                "Position Report header 3 bytes가 잘렸습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:SC_VDE_HEADER_BYTES] != SC_VDE_POSITION_REPORT_WIRE_PREFIX:
            return decode_structure_error(
                "Position Report wire prefix ff 7f 01이 아닙니다",
                correlation=correlation,
            )
        if len(wire) < POSITION_REPORT_WIRE_BYTES:
            return truncated_error(
                "Position Report wire는 정확히 17 bytes여야 합니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if len(wire) > POSITION_REPORT_WIRE_BYTES:
            return decode_structure_error(
                "Position Report body 뒤에 trailing bytes가 있습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        application = bytes(wire[SC_VDE_HEADER_BYTES:])
        validated = _validate_fields(application, decode_structure_error, correlation)
        if validated.is_err:
            return validated
        return Ok(application)
