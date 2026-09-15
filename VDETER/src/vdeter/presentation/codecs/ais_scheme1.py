"""AIS Authentication Scheme 1 codec (`stage5_service_integration_baseline.md` §6.3).

normative R1027 Ed.1.1 Table 21의 fingerprint 포함 672-bit/84-byte wire 구조를 사용한다.
G1192 Ed.1.0 Table 15의 624-bit 이전 구조로 fallback decode하지 않는다.

wire = `16-bit VPFI=7 || 6-bit Message format=1 || 650-bit body` (연속 bit-pack, 84 bytes)

application bytes = field 3..10의 650 meaningful bits를 MSB-first로 담은 고정 82 bytes.
마지막 6 padding bits는 0이어야 한다.

| field | bits | 허용값 |
| --- | ---: | --- |
| Authentication Scheme ID | 8 | 1 |
| AIS Message ID | 6 | 1..63 |
| MMSI | 30 | 0..2^30-1 |
| Channel ID | 2 | 0=AIS1, 1=AIS2; 2..3 거부 |
| Slot number | 12 | 0..2249 |
| Timestamp | 32 | unsigned seconds |
| Certificate fingerprint | 48 | 임의 |
| Signature | 512 | 임의 |

codec은 field 범위와 padding만 검증한다. AIS 원문 대조, signature 검증, freshness/replay
판정 또는 인증 결과 산출은 하지 않는다.
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

AIS_SCHEME1_CODEC_REF = "r1027.vpfi7.authentication_scheme1.v1"
AIS_SCHEME1_VPFI = 7
AIS_SCHEME1_MESSAGE_FORMAT = 1
#: 첫 두 octet `00 07` 뒤 세 번째 byte의 상위 6 bits가 Message format이다.
AIS_SCHEME1_WIRE_PREFIX = bytes((0x00, 0x07))
AIS_SCHEME1_WIRE_BYTES = 84
AIS_SCHEME1_APPLICATION_BYTES = 82
AIS_SCHEME1_BODY_BITS = 650
AIS_SCHEME1_PADDING_BITS = 6

_BODY_MASK = (1 << AIS_SCHEME1_BODY_BITS) - 1

#: body 안 field 배치: (이름, offset bits, width bits).
_FIELDS = (
    ("scheme_id", 0, 8),
    ("ais_message_id", 8, 6),
    ("mmsi", 14, 30),
    ("channel_id", 44, 2),
    ("slot_number", 46, 12),
    ("timestamp", 58, 32),
    ("certificate_fingerprint", 90, 48),
    ("signature", 138, 512),
)


def _body_field(body: int, offset: int, width: int) -> int:
    return (body >> (AIS_SCHEME1_BODY_BITS - offset - width)) & ((1 << width) - 1)


def _validate_body_fields(body: int, error_factory, correlation: Correlation):
    scheme_id = _body_field(body, 0, 8)
    if scheme_id != 1:
        return error_factory(
            "Authentication Scheme ID는 1이어야 합니다",
            correlation=correlation,
            scheme_id=scheme_id,
        )
    ais_message_id = _body_field(body, 8, 6)
    if not 1 <= ais_message_id <= 63:
        return error_factory(
            "AIS Message ID는 1..63이어야 합니다",
            correlation=correlation,
            ais_message_id=ais_message_id,
        )
    channel_id = _body_field(body, 44, 2)
    if channel_id not in (0, 1):
        return error_factory(
            "Channel ID 2..3은 거부합니다", correlation=correlation, channel_id=channel_id
        )
    slot_number = _body_field(body, 46, 12)
    if slot_number > 2249:
        return error_factory(
            "Slot number는 0..2249여야 합니다",
            correlation=correlation,
            slot_number=slot_number,
        )
    return Ok(body)


class AisAuthenticationScheme1Codec:
    """R1027 Table 21 bit codec. 인증 판정을 하지 않는다."""

    __slots__ = ()

    codec_ref = AIS_SCHEME1_CODEC_REF
    vpfi = AIS_SCHEME1_VPFI
    message_format = AIS_SCHEME1_MESSAGE_FORMAT
    discriminator = WireDiscriminator(
        prefix=AIS_SCHEME1_WIRE_PREFIX,
        third_byte_mask=0xFC,
        third_byte_value=AIS_SCHEME1_MESSAGE_FORMAT << 2,
    )
    min_wire_length = 3
    max_application_bytes = AIS_SCHEME1_APPLICATION_BYTES

    def encoded_size_bound(self, application_length: int):
        if application_length != AIS_SCHEME1_APPLICATION_BYTES:
            return encode_structure_error(
                "AIS Scheme 1 application bytes는 정확히 82 bytes여야 합니다",
                payload_size=application_length,
            )
        return Ok(AIS_SCHEME1_WIRE_BYTES)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        application_bits = int.from_bytes(application_payload, "big")
        if application_bits & ((1 << AIS_SCHEME1_PADDING_BITS) - 1):
            return encode_structure_error(
                "application bytes의 마지막 6 padding bits는 0이어야 합니다",
                correlation=correlation,
            )
        body = (application_bits >> AIS_SCHEME1_PADDING_BITS) & _BODY_MASK
        validated = _validate_body_fields(body, encode_structure_error, correlation)
        if validated.is_err:
            return validated
        wire_bits = (
            (AIS_SCHEME1_VPFI << (6 + AIS_SCHEME1_BODY_BITS))
            | (AIS_SCHEME1_MESSAGE_FORMAT << AIS_SCHEME1_BODY_BITS)
            | body
        )
        return Ok(wire_bits.to_bytes(AIS_SCHEME1_WIRE_BYTES, "big"))

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < 3:
            return truncated_error(
                "AIS Scheme 1 wire는 최소 3 bytes가 필요합니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:2] != AIS_SCHEME1_WIRE_PREFIX:
            return decode_structure_error(
                "16-bit VPFI 7 prefix가 아닙니다", correlation=correlation
            )
        message_format = (wire[2] >> 2) & 0x3F
        if message_format != AIS_SCHEME1_MESSAGE_FORMAT:
            return decode_structure_error(
                "VPFI 7 Message format 1이 아닙니다",
                correlation=correlation,
                message_format=message_format,
            )
        if len(wire) != AIS_SCHEME1_WIRE_BYTES:
            # 3-byte 정렬 header로 85 bytes를 만드는 오정렬 구현도 여기서 거부된다.
            return decode_structure_error(
                "AIS Scheme 1 wire는 정확히 84 bytes여야 합니다",
                correlation=correlation,
                data_size=len(wire),
            )
        wire_bits = int.from_bytes(wire, "big")
        body = wire_bits & _BODY_MASK
        validated = _validate_body_fields(body, decode_structure_error, correlation)
        if validated.is_err:
            return validated
        application_bits = body << AIS_SCHEME1_PADDING_BITS
        return Ok(application_bits.to_bytes(AIS_SCHEME1_APPLICATION_BYTES, "big"))
