"""SC-VDE Route/Remote-control TEST codec family (`payload_extension_baseline.md` §6.4).

프로젝트 TEST/PROJECT 전용 profile이다. carrier는 R1027 Ed.1.1 Table 3의 Level 2 시험
전용 `FF 7F`이며 SC-VDE의 공식 OPERATIONAL 할당이 아니다. 노선(SC-VDE-005)과
원격조종(SC-VDE-006)은 동일한 MF 2 schema를 공유하므로 codec을 복제하지 않고, MF 2
command와 MF 3 ACK가 공통 bit helper를 공유하는 별도 codec instance/ref를 사용한다.

```text
command wire = FF 7F | 02 | fixed body[76] | waypoint[77]*n | zero pad to byte
ACK wire     = FF 7F | 03 | ACK body[17] | zero pad[7]
```

application bytes는 VPFI/Message ID를 제외한 body+zero padding이다. 모든 bit는 첫
byte MSB부터 연속 pack하고 signed 좌표만 지정 폭의 two's complement다.

codec은 §6.4.8의 canonical raw 허용 집합(구조 정규성)만 검증한다. 달력 일관성,
Cancellation 조합, Heading/Fallback 업무 profile, Linkage 상관, Sender와 실제
endpoint 역할 일치, 노선 적합성·권한·실제 수행 판정은 application 책임이다
(`DR-PAYLOAD-007`). MF 3 application ACK를 자동 생성하거나 Link `ack_required`와
결합하지 않는다 (`DR-PAYLOAD-008`).

이 unsigned Segment Type 0 profile은 송신자 인증·payload 무결성·replay protection을
제공하지 않는다. 격리된 유선 시험·프로젝트 데모 전용이며 실제 선박 제어에 사용해서는
안 된다 (`payload_extension_baseline.md` §4.4).
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

SC_VDE_COMMAND_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf2.v1"
SC_VDE_ACK_CODEC_REF = "project.sc_vde.vpfi_ff7f.mf3.v1"

#: 프로젝트 registry 정규화 logical VPFI. R1027 Table 3 Level 2 `FF 7F`의 continuation
#: bit를 제외한 두 7-bit content 연결값이며, wire에는 이 정수의 big-endian `3f ff`가
#: 아니라 아래 exact wire bytes만 사용한다 (`payload_extension_baseline.md` §6.4.2).
SC_VDE_LOGICAL_VPFI = 16383
SC_VDE_WIRE_VPFI = bytes((0xFF, 0x7F))
SC_VDE_COMMAND_MESSAGE_FORMAT = 2
SC_VDE_ACK_MESSAGE_FORMAT = 3
SC_VDE_COMMAND_WIRE_PREFIX = SC_VDE_WIRE_VPFI + bytes((SC_VDE_COMMAND_MESSAGE_FORMAT,))
SC_VDE_ACK_WIRE_PREFIX = SC_VDE_WIRE_VPFI + bytes((SC_VDE_ACK_MESSAGE_FORMAT,))
SC_VDE_HEADER_BYTES = 3

COMMAND_FIXED_BODY_BITS = 76
COMMAND_WAYPOINT_BITS = 77
COMMAND_MAX_WAYPOINTS = 16


def command_application_bytes(waypoint_count: int) -> int:
    """`ceil((76 + 77*n) / 8)` (`payload_extension_baseline.md` §6.4.9)."""

    return (COMMAND_FIXED_BODY_BITS + COMMAND_WAYPOINT_BITS * waypoint_count + 7) // 8


#: n=0..16의 허용 application 길이 {10,20,29,...,164}. 이 집합 밖 길이는 전부 거부한다.
COMMAND_ALLOWED_APPLICATION_BYTES = tuple(
    command_application_bytes(count) for count in range(COMMAND_MAX_WAYPOINTS + 1)
)
_COMMAND_ALLOWED_SET = frozenset(COMMAND_ALLOWED_APPLICATION_BYTES)
COMMAND_MIN_APPLICATION_BYTES = COMMAND_ALLOWED_APPLICATION_BYTES[0]
COMMAND_MAX_APPLICATION_BYTES = COMMAND_ALLOWED_APPLICATION_BYTES[-1]
COMMAND_MIN_WIRE_BYTES = COMMAND_MIN_APPLICATION_BYTES + SC_VDE_HEADER_BYTES
COMMAND_MAX_WIRE_BYTES = COMMAND_MAX_APPLICATION_BYTES + SC_VDE_HEADER_BYTES

ACK_APPLICATION_BYTES = 3
ACK_BODY_BITS = 17
ACK_PADDING_BITS = 7
ACK_WIRE_BYTES = ACK_APPLICATION_BYTES + SC_VDE_HEADER_BYTES

#: §6.4.8 canonical raw 허용 집합. bit 폭이 이미 상한인 field(Linkage ID, Duration,
#: Turn Radius, Planned SOG)는 모든 raw 값이 허용이므로 표에 없다.
_ALLOWED_SENDER_CLASSIFICATIONS = frozenset((0, 1))
_ALLOWED_ROUTE_TYPES = frozenset((0, 1, 2, 5, 31))
_ALLOWED_LEG_GEOMETRIES = frozenset((0, 1, 2))
_ALLOWED_ACK_STATUSES = frozenset((0, 4, 7, 8, 9))
_ALLOWED_ACK_REASONS = frozenset((0, 1, 2, 3))

_START_NA_TUPLE = (0, 0, 24, 60)
_HEADING_NA = 3600
_MAX_FALLBACK = 3

_LONGITUDE_BITS = 28
_LATITUDE_BITS = 27
_LONGITUDE_ABS_MAX = 108_000_000
_LONGITUDE_NA = 108_600_000
_LATITUDE_ABS_MAX = 54_000_000
_LATITUDE_NA = 54_600_000


def _field(bits: int, total_bits: int, offset: int, width: int) -> int:
    """MSB-first 연속 bit stream에서 `[offset, offset+width)` 구간을 읽는다."""

    return (bits >> (total_bits - offset - width)) & ((1 << width) - 1)


def _signed(raw: int, width: int) -> int:
    """지정 폭 two's complement 해석 (`payload_extension_baseline.md` §6.4.7)."""

    return raw - (1 << width) if raw >= (1 << (width - 1)) else raw


def _validate_start_tuple(month: int, day: int, hour: int, minute: int) -> bool:
    """전체 N/A `0/0/24/60` 또는 정상 범위 조합만 허용한다. 혼합 N/A는 거부한다."""

    if (month, day, hour, minute) == _START_NA_TUPLE:
        return True
    return 1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59


def _validate_coordinate(raw: int, width: int, abs_max: int, na_value: int) -> bool:
    """signed 정상 범위, N/A 값만 허용하고 그 사이 gap과 나머지 signed 값을 거부한다."""

    if raw == na_value:
        return True
    return -abs_max <= _signed(raw, width) <= abs_max


def _validate_command_fields(
    application: bytes, waypoint_count: int, error_factory, correlation: Correlation
):
    """길이가 count와 일치하는 application의 field·reserved·padding 검증 (§6.4.8)."""

    total_bits = len(application) * 8
    bits = int.from_bytes(application, "big")
    sender = _field(bits, total_bits, 10, 3)
    if sender not in _ALLOWED_SENDER_CLASSIFICATIONS:
        return error_factory(
            "Sender Classification은 0(ship)·1(authority)만 허용합니다",
            correlation=correlation,
            sender_classification=sender,
        )
    route_type = _field(bits, total_bits, 13, 5)
    if route_type not in _ALLOWED_ROUTE_TYPES:
        return error_factory(
            "Route Type은 0, 1, 2, 5, 31만 허용합니다",
            correlation=correlation,
            route_type=route_type,
        )
    month = _field(bits, total_bits, 18, 4)
    day = _field(bits, total_bits, 22, 5)
    hour = _field(bits, total_bits, 27, 5)
    minute = _field(bits, total_bits, 32, 6)
    if not _validate_start_tuple(month, day, hour, minute):
        return error_factory(
            "Start tuple은 전체 N/A 0/0/24/60 또는 정상 범위 조합이어야 합니다",
            correlation=correlation,
            start_month=month,
            start_day=day,
            start_hour=hour,
            start_minute=minute,
        )
    heading = _field(bits, total_bits, 56, 12)
    if heading > _HEADING_NA:
        return error_factory(
            "Heading Setpoint는 0..3599 또는 3600(N/A)만 허용합니다",
            correlation=correlation,
            heading_setpoint=heading,
        )
    fallback = _field(bits, total_bits, 68, 3)
    if fallback > _MAX_FALLBACK:
        return error_factory(
            "Fallback State는 0..3만 허용합니다",
            correlation=correlation,
            fallback_state=fallback,
        )
    for index in range(waypoint_count):
        base = COMMAND_FIXED_BODY_BITS + COMMAND_WAYPOINT_BITS * index
        longitude = _field(bits, total_bits, base, _LONGITUDE_BITS)
        if not _validate_coordinate(
            longitude, _LONGITUDE_BITS, _LONGITUDE_ABS_MAX, _LONGITUDE_NA
        ):
            return error_factory(
                "Longitude는 ±108000000 또는 108600000(N/A)만 허용합니다",
                correlation=correlation,
                waypoint_index=index,
                longitude_raw=longitude,
            )
        latitude = _field(bits, total_bits, base + 28, _LATITUDE_BITS)
        if not _validate_coordinate(
            latitude, _LATITUDE_BITS, _LATITUDE_ABS_MAX, _LATITUDE_NA
        ):
            return error_factory(
                "Latitude는 ±54000000 또는 54600000(N/A)만 허용합니다",
                correlation=correlation,
                waypoint_index=index,
                latitude_raw=latitude,
            )
        geometry = _field(bits, total_bits, base + 75, 2)
        if geometry not in _ALLOWED_LEG_GEOMETRIES:
            return error_factory(
                "Leg Geometry 3은 거부합니다",
                correlation=correlation,
                waypoint_index=index,
                leg_geometry=geometry,
            )
    meaningful_bits = (
        COMMAND_FIXED_BODY_BITS + COMMAND_WAYPOINT_BITS * waypoint_count
    )
    padding_bits = total_bits - meaningful_bits
    if padding_bits and bits & ((1 << padding_bits) - 1):
        return error_factory(
            "command 마지막 byte의 zero padding bits가 0이 아닙니다",
            correlation=correlation,
            padding_bits=padding_bits,
        )
    return Ok(waypoint_count)


def _read_waypoint_count(application: bytes) -> int:
    """fixed body 뒤쪽 5 bits의 Number of Waypoints. 최소 10 bytes를 전제한다."""

    total_bits = len(application) * 8
    return _field(int.from_bytes(application, "big"), total_bits, 71, 5)


def _validate_ack_fields(application: bytes, error_factory, correlation: Correlation):
    """3-byte ACK body의 Status/Reason과 7-bit zero padding 검증 (§6.4.8)."""

    bits = int.from_bytes(application, "big")
    if bits & ((1 << ACK_PADDING_BITS) - 1):
        return error_factory(
            "ACK 마지막 7 padding bits는 0이어야 합니다", correlation=correlation
        )
    status = (bits >> 10) & 0x0F
    if status not in _ALLOWED_ACK_STATUSES:
        return error_factory(
            "ACK Status는 0, 4, 7, 8, 9만 허용합니다",
            correlation=correlation,
            ack_status=status,
        )
    reason = (bits >> 7) & 0x07
    if reason not in _ALLOWED_ACK_REASONS:
        return error_factory(
            "ACK Reason은 0..3만 허용합니다",
            correlation=correlation,
            ack_reason=reason,
        )
    return Ok(status)


class ScVdeRouteControlCommandCodec:
    """MF 2 route/remote-control command 구조 codec. 업무 의미는 판정하지 않는다."""

    __slots__ = ()

    codec_ref = SC_VDE_COMMAND_CODEC_REF
    vpfi = SC_VDE_LOGICAL_VPFI
    message_format = SC_VDE_COMMAND_MESSAGE_FORMAT
    discriminator = WireDiscriminator(prefix=SC_VDE_COMMAND_WIRE_PREFIX)
    min_wire_length = COMMAND_MIN_WIRE_BYTES
    max_application_bytes = COMMAND_MAX_APPLICATION_BYTES
    max_wire_bytes = COMMAND_MAX_WIRE_BYTES
    #: §6.4.4: addressed 전용. adapter가 broadcast route를 송수신 양방향에서 거부한다.
    addressed_only = True

    def encoded_size_bound(self, application_length: int):
        if application_length not in _COMMAND_ALLOWED_SET:
            return encode_structure_error(
                "SC-VDE command application 길이는 n=0..16의 ceil((76+77*n)/8) 집합만"
                " 허용합니다",
                payload_size=application_length,
                allowed_lengths=str(COMMAND_ALLOWED_APPLICATION_BYTES),
            )
        return Ok(application_length + SC_VDE_HEADER_BYTES)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        waypoint_count = _read_waypoint_count(application_payload)
        if waypoint_count > COMMAND_MAX_WAYPOINTS:
            return encode_structure_error(
                "Number of Waypoints는 0..16만 허용합니다",
                correlation=correlation,
                waypoint_count=waypoint_count,
            )
        expected_length = command_application_bytes(waypoint_count)
        if expected_length != len(application_payload):
            return encode_structure_error(
                "waypoint count가 application 길이와 일치하지 않습니다",
                correlation=correlation,
                waypoint_count=waypoint_count,
                payload_size=len(application_payload),
                expected_size=expected_length,
            )
        validated = _validate_command_fields(
            application_payload, waypoint_count, encode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        # 검증된 body bits는 canonicalize 없이 byte-exact로 보존한다 (§6.4.10).
        return Ok(SC_VDE_COMMAND_WIRE_PREFIX + application_payload)

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < SC_VDE_HEADER_BYTES:
            return truncated_error(
                "SC-VDE command header 3 bytes가 잘렸습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:SC_VDE_HEADER_BYTES] != SC_VDE_COMMAND_WIRE_PREFIX:
            return decode_structure_error(
                "SC-VDE command wire prefix ff 7f 02가 아닙니다", correlation=correlation
            )
        application = wire[SC_VDE_HEADER_BYTES:]
        if len(application) < COMMAND_MIN_APPLICATION_BYTES:
            return truncated_error(
                "SC-VDE command fixed body 76 bits가 잘렸습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        waypoint_count = _read_waypoint_count(application)
        if waypoint_count > COMMAND_MAX_WAYPOINTS:
            return decode_structure_error(
                "Number of Waypoints는 0..16만 허용합니다",
                correlation=correlation,
                waypoint_count=waypoint_count,
            )
        expected_length = command_application_bytes(waypoint_count)
        if len(application) < expected_length:
            return truncated_error(
                "waypoint field가 선언된 count보다 짧게 잘렸습니다",
                correlation=correlation,
                waypoint_count=waypoint_count,
                payload_size=len(application),
                expected_size=expected_length,
            )
        if len(application) > expected_length:
            return decode_structure_error(
                "waypoint count 뒤에 trailing bytes가 있습니다",
                correlation=correlation,
                waypoint_count=waypoint_count,
                payload_size=len(application),
                expected_size=expected_length,
            )
        validated = _validate_command_fields(
            application, waypoint_count, decode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        return Ok(application)


class ScVdeRouteControlAckCodec:
    """MF 3 application ACK 구조 codec. ACK를 자동 생성·상관하지 않는다."""

    __slots__ = ()

    codec_ref = SC_VDE_ACK_CODEC_REF
    vpfi = SC_VDE_LOGICAL_VPFI
    message_format = SC_VDE_ACK_MESSAGE_FORMAT
    discriminator = WireDiscriminator(prefix=SC_VDE_ACK_WIRE_PREFIX)
    min_wire_length = ACK_WIRE_BYTES
    max_application_bytes = ACK_APPLICATION_BYTES
    max_wire_bytes = ACK_WIRE_BYTES
    #: §6.4.4: addressed 전용. adapter가 broadcast route를 송수신 양방향에서 거부한다.
    addressed_only = True

    def encoded_size_bound(self, application_length: int):
        if application_length != ACK_APPLICATION_BYTES:
            return encode_structure_error(
                "SC-VDE ACK application은 정확히 3 bytes여야 합니다",
                payload_size=application_length,
            )
        return Ok(ACK_WIRE_BYTES)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        validated = _validate_ack_fields(
            application_payload, encode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        return Ok(SC_VDE_ACK_WIRE_PREFIX + application_payload)

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < SC_VDE_HEADER_BYTES:
            return truncated_error(
                "SC-VDE ACK header 3 bytes가 잘렸습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:SC_VDE_HEADER_BYTES] != SC_VDE_ACK_WIRE_PREFIX:
            return decode_structure_error(
                "SC-VDE ACK wire prefix ff 7f 03이 아닙니다", correlation=correlation
            )
        if len(wire) < ACK_WIRE_BYTES:
            return truncated_error(
                "SC-VDE ACK wire는 정확히 6 bytes여야 합니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if len(wire) > ACK_WIRE_BYTES:
            return decode_structure_error(
                "SC-VDE ACK body 뒤에 trailing bytes가 있습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        application = wire[SC_VDE_HEADER_BYTES:]
        validated = _validate_ack_fields(
            application, decode_structure_error, correlation
        )
        if validated.is_err:
            return validated
        return Ok(application)
