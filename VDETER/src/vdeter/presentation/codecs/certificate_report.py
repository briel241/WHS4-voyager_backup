"""X.509 Certificate Report codec (`stage5_service_integration_baseline.md` §6.2).

IALA R1027 Ed.1.1 Table 4·6의 `VPFI=0`, `Message format=1`을 사용한다.

wire = `[0x00 VPFI][0x01 Message format][application body]`

application body 구조:

```text
number_of_entries: 1 byte (n >= 1)
repeat n times:
  issuer_certificate_fingerprint: 6 bytes
  certificate_type: 1 byte       # 0, 1 or 2; 3..255 reserved -> 거부
  encoded_length: 2 bytes        # big-endian byte count
  encoded_object: encoded_length bytes
```

codec은 형식 구조만 검증한다 (`DR-STAGE5-026`).

- type 0·1: X.509 v3 DER certificate 구조 (SEQUENCE{tbs SEQUENCE{[0]{INTEGER 2}...},
  AlgorithmIdentifier SEQUENCE, BIT STRING})
- type 2: DER CRL 구조 (SEQUENCE{tbsCertList SEQUENCE, AlgorithmIdentifier SEQUENCE,
  BIT STRING})
- trust anchor, chain, signature, revocation, root/self-signed 여부는 판정하지 않는다.
- DER bytes와 entry 순서를 재인코딩하거나 변경하지 않는다. 같은 fingerprint의 entry
  중복도 허용한다.
"""

from __future__ import annotations

from ...common.errors import Correlation
from ...common.results import Ok
from .base import (
    MAX_APPLICATION_BYTES,
    WireDiscriminator,
    decode_structure_error,
    encode_structure_error,
    truncated_error,
)

CERTIFICATE_REPORT_CODEC_REF = "r1027.vpfi0.certificate_report.v1"
CERTIFICATE_REPORT_VPFI = 0
CERTIFICATE_REPORT_MESSAGE_FORMAT = 1
CERTIFICATE_REPORT_WIRE_PREFIX = bytes((CERTIFICATE_REPORT_VPFI, CERTIFICATE_REPORT_MESSAGE_FORMAT))
CERTIFICATE_REPORT_HEADER_BYTES = 2
#: 최소 wire: 2-byte header + n(1) + fingerprint(6) + type(1) + length(2) + DER 최소 1 byte.
CERTIFICATE_REPORT_MIN_WIRE_BYTES = 13
#: 2-byte header 포함 encoded 최대 (§7.1).
CERTIFICATE_REPORT_MAX_ENCODED_BYTES = 65_533

_FINGERPRINT_BYTES = 6
_ENTRY_FIXED_BYTES = _FINGERPRINT_BYTES + 1 + 2
_ALLOWED_CERTIFICATE_TYPES = (0, 1, 2)

_TAG_SEQUENCE = 0x30
_TAG_BIT_STRING = 0x03
_TAG_INTEGER = 0x02
_TAG_CONTEXT_0_CONSTRUCTED = 0xA0


def _parse_der_tlv(data: bytes, offset: int):
    """정의 길이 DER TLV 하나를 판독한다. `(tag, content_offset, content_length)` 또는 None."""

    if offset + 2 > len(data):
        return None
    tag = data[offset]
    if tag & 0x1F == 0x1F:  # multi-byte tag은 이 형식에서 사용하지 않는다.
        return None
    first = data[offset + 1]
    if first < 0x80:
        return tag, offset + 2, first
    if first == 0x80:  # indefinite length는 DER가 아니다.
        return None
    count = first & 0x7F
    if count > 4 or offset + 2 + count > len(data):
        return None
    raw = data[offset + 2 : offset + 2 + count]
    if raw[0] == 0x00:  # 비최소 길이 encode는 DER가 아니다.
        return None
    length = int.from_bytes(raw, "big")
    if length < 0x80:
        return None
    return tag, offset + 2 + count, length


def _parse_exact_sequence(data: bytes):
    """전체 bytes가 정확히 하나의 SEQUENCE TLV인지 확인하고 내용 범위를 반환한다."""

    parsed = _parse_der_tlv(data, 0)
    if parsed is None:
        return None
    tag, content_offset, content_length = parsed
    if tag != _TAG_SEQUENCE:
        return None
    if content_offset + content_length != len(data):
        return None
    return content_offset, content_length


def _sequence_children(data: bytes, start: int, end: int):
    """SEQUENCE 내용 구간의 최상위 TLV 목록. 구조가 깨지면 None."""

    children = []
    offset = start
    while offset < end:
        parsed = _parse_der_tlv(data, offset)
        if parsed is None:
            return None
        tag, content_offset, content_length = parsed
        next_offset = content_offset + content_length
        if next_offset > end:
            return None
        children.append((tag, offset, content_offset, content_length))
        offset = next_offset
    return children


_TAG_UTC_TIME = 0x17
_TAG_GENERALIZED_TIME = 0x18
_TAG_OID = 0x06
#: TBSCertificate optional tail: issuerUniqueID [1], subjectUniqueID [2], extensions [3].
_CERTIFICATE_TAIL_TAGS = (0x81, 0xA1, 0x82, 0xA2, 0xA3)


def _validate_signed_der(encoded: bytes):
    """`SEQUENCE{tbs SEQUENCE, AlgorithmIdentifier, BIT STRING}` 공통 signed 구조 검사."""

    outer = _parse_exact_sequence(encoded)
    if outer is None:
        return None
    children = _sequence_children(encoded, outer[0], outer[0] + outer[1])
    if children is None or len(children) != 3:
        return None
    tags = tuple(child[0] for child in children)
    if tags != (_TAG_SEQUENCE, _TAG_SEQUENCE, _TAG_BIT_STRING):
        return None
    if not _is_algorithm_identifier(encoded, children[1]):
        return None
    if not _is_valid_bit_string(encoded, children[2]):
        return None
    return children


def _is_algorithm_identifier(encoded: bytes, child) -> bool:
    """AlgorithmIdentifier ::= SEQUENCE{algorithm OID, parameters ANY OPTIONAL}."""

    tag, _offset, content_offset, content_length = child
    if tag != _TAG_SEQUENCE:
        return False
    inner = _sequence_children(encoded, content_offset, content_offset + content_length)
    if not inner or len(inner) > 2:
        return False
    oid_tag, _o, oid_content_offset, oid_content_length = inner[0]
    return oid_tag == _TAG_OID and oid_content_length >= 1


def _is_valid_bit_string(encoded: bytes, child) -> bool:
    """BIT STRING: unused-bits octet 0..7 뒤에 1 byte 이상의 값이 있어야 한다."""

    tag, _offset, content_offset, content_length = child
    if tag != _TAG_BIT_STRING or content_length < 2:
        return False
    return encoded[content_offset] <= 7


def _is_name(encoded: bytes, child) -> bool:
    """Name ::= RDNSequence (SEQUENCE). RDN 내부 의미는 판정하지 않는다."""

    tag, _offset, content_offset, content_length = child
    if tag != _TAG_SEQUENCE:
        return False
    return _sequence_children(
        encoded, content_offset, content_offset + content_length
    ) is not None


def _is_time(child) -> bool:
    return child[0] in (_TAG_UTC_TIME, _TAG_GENERALIZED_TIME) and child[3] >= 1


def _is_validity(encoded: bytes, child) -> bool:
    """Validity ::= SEQUENCE{notBefore Time, notAfter Time}."""

    tag, _offset, content_offset, content_length = child
    if tag != _TAG_SEQUENCE:
        return False
    inner = _sequence_children(encoded, content_offset, content_offset + content_length)
    if inner is None or len(inner) != 2:
        return False
    return _is_time(inner[0]) and _is_time(inner[1])


def _is_subject_public_key_info(encoded: bytes, child) -> bool:
    """SubjectPublicKeyInfo ::= SEQUENCE{AlgorithmIdentifier, BIT STRING}."""

    tag, _offset, content_offset, content_length = child
    if tag != _TAG_SEQUENCE:
        return False
    inner = _sequence_children(encoded, content_offset, content_offset + content_length)
    if inner is None or len(inner) != 2:
        return False
    return _is_algorithm_identifier(encoded, inner[0]) and _is_valid_bit_string(
        encoded, inner[1]
    )


def _is_x509_v3_certificate(encoded: bytes) -> bool:
    """RFC 5280 TBSCertificate 필수 field의 X.509 v3 DER 구조 검사.

    trust anchor, chain, signature, revocation 판정은 하지 않는다 (`DR-STAGE5-026`).
    """

    children = _validate_signed_der(encoded)
    if children is None:
        return False
    _tag, _tbs_offset, tbs_content_offset, tbs_content_length = children[0]
    tbs_children = _sequence_children(
        encoded, tbs_content_offset, tbs_content_offset + tbs_content_length
    )
    if tbs_children is None or len(tbs_children) < 7:
        return False
    # [0] EXPLICIT version, INTEGER 2 (v3)
    version_tag, _v_off, version_content_offset, version_content_length = tbs_children[0]
    if version_tag != _TAG_CONTEXT_0_CONSTRUCTED:
        return False  # version 필드가 없으면 v1이므로 v3 요구를 만족하지 않는다.
    version_value = _parse_der_tlv(encoded, version_content_offset)
    if version_value is None:
        return False
    inner_tag, inner_offset, inner_length = version_value
    if inner_tag != _TAG_INTEGER or inner_length != 1:
        return False
    if inner_offset + inner_length != version_content_offset + version_content_length:
        return False
    if encoded[inner_offset] != 2:
        return False
    # serialNumber INTEGER
    serial_tag = tbs_children[1]
    if serial_tag[0] != _TAG_INTEGER or serial_tag[3] < 1:
        return False
    # signature AlgorithmIdentifier
    if not _is_algorithm_identifier(encoded, tbs_children[2]):
        return False
    # issuer Name, validity, subject Name, subjectPublicKeyInfo
    if not _is_name(encoded, tbs_children[3]):
        return False
    if not _is_validity(encoded, tbs_children[4]):
        return False
    if not _is_name(encoded, tbs_children[5]):
        return False
    if not _is_subject_public_key_info(encoded, tbs_children[6]):
        return False
    # optional tail: issuerUniqueID [1], subjectUniqueID [2], extensions [3] 순서.
    tail_tags = [child[0] for child in tbs_children[7:]]
    allowed = list(_CERTIFICATE_TAIL_TAGS)
    for tag in tail_tags:
        if tag not in allowed:
            return False
        # 같은 tag의 중복과 역순을 거부한다.
        allowed = allowed[allowed.index(tag) + 1 :]
    return True


def _is_der_crl(encoded: bytes) -> bool:
    """RFC 5280 TBSCertList 필수 field의 DER CRL 구조 검사.

    revocation 의미 판정은 하지 않는다.
    """

    children = _validate_signed_der(encoded)
    if children is None:
        return False
    _tag, _off, tbs_content_offset, tbs_content_length = children[0]
    tbs_children = _sequence_children(
        encoded, tbs_content_offset, tbs_content_offset + tbs_content_length
    )
    if tbs_children is None or len(tbs_children) < 3:
        return False
    index = 0
    # optional version INTEGER: CRL v2만 정의돼 있으므로 존재하면 값 1이어야 한다.
    if tbs_children[0][0] == _TAG_INTEGER:
        if tbs_children[0][3] != 1 or encoded[tbs_children[0][2]] != 1:
            return False
        index = 1
    remaining = tbs_children[index:]
    if len(remaining) < 3:
        return False
    if not _is_algorithm_identifier(encoded, remaining[0]):
        return False
    if not _is_name(encoded, remaining[1]):
        return False
    if not _is_time(remaining[2]):
        return False
    # optional: nextUpdate Time, revokedCertificates SEQUENCE, crlExtensions [0]
    tail = remaining[3:]
    position = 0
    if position < len(tail) and _is_time(tail[position]):
        position += 1
    if position < len(tail) and tail[position][0] == _TAG_SEQUENCE:
        position += 1
    if position < len(tail) and tail[position][0] == _TAG_CONTEXT_0_CONSTRUCTED:
        position += 1
    return position == len(tail)


def _validate_body(body: bytes, error_factory, correlation: Correlation):
    """format 구조를 검증한다. bytes를 변경하거나 재정렬하지 않는다."""

    if len(body) < 1:
        return error_factory(
            "Certificate Report body에 entry count가 없습니다",
            correlation=correlation,
            data_size=len(body),
        )
    entry_count = body[0]
    if entry_count < 1:
        return error_factory(
            "Certificate Report entry는 최소 1개여야 합니다", correlation=correlation
        )
    offset = 1
    for index in range(entry_count):
        if offset + _ENTRY_FIXED_BYTES > len(body):
            return error_factory(
                "Certificate Report entry header가 잘렸습니다",
                correlation=correlation,
                entry_index=index,
            )
        certificate_type = body[offset + _FINGERPRINT_BYTES]
        if certificate_type not in _ALLOWED_CERTIFICATE_TYPES:
            return error_factory(
                "reserved certificate type은 거부합니다",
                correlation=correlation,
                entry_index=index,
                certificate_type=certificate_type,
            )
        encoded_length = int.from_bytes(
            body[offset + _FINGERPRINT_BYTES + 1 : offset + _ENTRY_FIXED_BYTES], "big"
        )
        if encoded_length < 1:
            return error_factory(
                "encoded_length는 1 이상이어야 합니다",
                correlation=correlation,
                entry_index=index,
            )
        object_start = offset + _ENTRY_FIXED_BYTES
        object_end = object_start + encoded_length
        if object_end > len(body):
            return error_factory(
                "encoded_object가 선언된 길이보다 짧습니다",
                correlation=correlation,
                entry_index=index,
                encoded_length=encoded_length,
            )
        encoded_object = body[object_start:object_end]
        if certificate_type in (0, 1):
            if not _is_x509_v3_certificate(encoded_object):
                return error_factory(
                    "type 0/1 entry가 X.509 v3 DER certificate 구조가 아닙니다",
                    correlation=correlation,
                    entry_index=index,
                    certificate_type=certificate_type,
                )
        else:
            if not _is_der_crl(encoded_object):
                return error_factory(
                    "type 2 entry가 DER CRL 구조가 아닙니다",
                    correlation=correlation,
                    entry_index=index,
                )
        offset = object_end
    if offset != len(body):
        return error_factory(
            "entry count 뒤에 남는 bytes가 있습니다",
            correlation=correlation,
            trailing_bytes=len(body) - offset,
        )
    return Ok(entry_count)


class CertificateReportCodec:
    """R1027 Certificate Report structural codec."""

    __slots__ = ()

    codec_ref = CERTIFICATE_REPORT_CODEC_REF
    vpfi = CERTIFICATE_REPORT_VPFI
    message_format = CERTIFICATE_REPORT_MESSAGE_FORMAT
    discriminator = WireDiscriminator(prefix=CERTIFICATE_REPORT_WIRE_PREFIX)
    min_wire_length = CERTIFICATE_REPORT_MIN_WIRE_BYTES
    max_application_bytes = MAX_APPLICATION_BYTES

    def encoded_size_bound(self, application_length: int):
        if not 1 <= application_length <= MAX_APPLICATION_BYTES:
            return encode_structure_error(
                "Certificate Report body는 1..65,531 bytes여야 합니다",
                payload_size=application_length,
                max_payload_bytes=MAX_APPLICATION_BYTES,
            )
        return Ok(CERTIFICATE_REPORT_HEADER_BYTES + application_length)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        validated = _validate_body(application_payload, encode_structure_error, correlation)
        if validated.is_err:
            return validated
        return Ok(CERTIFICATE_REPORT_WIRE_PREFIX + application_payload)

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < CERTIFICATE_REPORT_MIN_WIRE_BYTES:
            return truncated_error(
                "Certificate Report의 최소 전체 길이 13 bytes에 못 미칩니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:CERTIFICATE_REPORT_HEADER_BYTES] != CERTIFICATE_REPORT_WIRE_PREFIX:
            return decode_structure_error(
                "Certificate Report wire header가 아닙니다", correlation=correlation
            )
        if len(wire) > CERTIFICATE_REPORT_MAX_ENCODED_BYTES:
            return decode_structure_error(
                "Certificate Report encoded 크기가 상한을 넘습니다",
                correlation=correlation,
                data_size=len(wire),
            )
        body = wire[CERTIFICATE_REPORT_HEADER_BYTES:]
        validated = _validate_body(body, decode_structure_error, correlation)
        if validated.is_err:
            return validated
        return Ok(body)
