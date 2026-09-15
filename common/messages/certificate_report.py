"""R1027 Certificate Report 본문 구성·해석 (SC-VDE-001 · VDETER 연동).

VDETER Presentation codec ``x509.certificate_report`` (IALA R1027 Ed.1.1
Table 4·6, VPFI 0 · Message format 1)가 받는 **application body**를 만든다.
링크가 앞에 2-byte wire prefix ``[0x00][0x01]``를 붙이므로 응용은 그 prefix를
붙이지 않는다. 우리 codec.py의 16-bit VPFI 봉투도 붙이지 않는다(계열 B).

본문 구조 (VDETER certificate_report codec와 동일)::

    number_of_entries : 1 byte (n >= 1)
    n번 반복:
        issuer_certificate_fingerprint : 6 byte
        certificate_type               : 1 byte  (0·1 = X.509 인증서, 2 = CRL)
        encoded_length                 : 2 byte big-endian
        encoded_object                 : encoded_length byte (DER 그대로)

이 모듈은 **구조만** 다룬다. DER의 X.509 유효성·서명·신뢰 판정은
certificate.py와 trust_store.py가 맡는다(VDETER도 구조만 검증, DR-STAGE5-026).
"""

from __future__ import annotations

from dataclasses import dataclass

NAME = "X.509 Certificate Report"

# VDETER certificate_report codec와 맞춘 상수
FINGERPRINT_BYTES = 6
CERT_TYPE_X509 = 1          # X.509 v3 인증서 (0도 인증서로 허용됨)
CERT_TYPE_X509_ALT = 0
CERT_TYPE_CRL = 2           # DER CRL
ALLOWED_CERT_TYPES = (0, 1, 2)

MAX_ENTRIES = 255           # number_of_entries가 1 byte
_ENTRY_HEADER_BYTES = FINGERPRINT_BYTES + 1 + 2   # 지문 + 타입 + 길이


class CertificateReportError(ValueError):
    """Certificate Report 본문 구성·해석 실패."""


@dataclass(frozen=True)
class ReportEntry:
    """Certificate Report의 항목 하나."""

    fingerprint: bytes          # 6 byte issuer certificate fingerprint
    certificate_type: int       # 0·1 = 인증서, 2 = CRL
    der: bytes                  # 인코딩된 DER object

    def __post_init__(self) -> None:
        if len(self.fingerprint) != FINGERPRINT_BYTES:
            raise CertificateReportError(
                f"지문은 {FINGERPRINT_BYTES} byte여야 한다 ({len(self.fingerprint)})")
        if self.certificate_type not in ALLOWED_CERT_TYPES:
            raise CertificateReportError(
                f"certificate_type은 {ALLOWED_CERT_TYPES} 중 하나여야 한다 "
                f"({self.certificate_type})")
        if not (1 <= len(self.der) <= 0xFFFF):
            raise CertificateReportError(
                f"DER 길이는 1..65535 byte여야 한다 ({len(self.der)})")


def build_report(entries: list[ReportEntry]) -> bytes:
    """항목 목록을 Certificate Report 본문 bytes로 만든다."""
    if not entries:
        raise CertificateReportError("항목이 최소 1개여야 한다")
    if len(entries) > MAX_ENTRIES:
        raise CertificateReportError(
            f"항목은 최대 {MAX_ENTRIES}개다 ({len(entries)})")

    parts = [bytes([len(entries)])]
    for entry in entries:
        parts.append(bytes(entry.fingerprint))
        parts.append(bytes([entry.certificate_type]))
        parts.append(len(entry.der).to_bytes(2, "big"))
        parts.append(bytes(entry.der))
    return b"".join(parts)


def build_single(der: bytes, fingerprint: bytes,
                 certificate_type: int = CERT_TYPE_X509) -> bytes:
    """인증서 하나짜리 Certificate Report 본문을 만든다."""
    return build_report([ReportEntry(fingerprint=fingerprint,
                                     certificate_type=certificate_type,
                                     der=bytes(der))])


def parse_report(body: bytes) -> list[ReportEntry]:
    """Certificate Report 본문을 항목 목록으로 해석한다.

    VDETER codec이 이미 구조를 검증한 payload를 받지만, 응용이 단독으로
    (링크 없이) 재현·시험할 때를 위해 같은 구조 검사를 여기서도 한다.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise CertificateReportError("본문은 bytes여야 한다")
    body = bytes(body)
    if len(body) < 1:
        raise CertificateReportError("entry count가 없다")

    count = body[0]
    if count < 1:
        raise CertificateReportError("entry는 최소 1개여야 한다")

    entries: list[ReportEntry] = []
    offset = 1
    for index in range(count):
        if offset + _ENTRY_HEADER_BYTES > len(body):
            raise CertificateReportError(f"entry {index} 헤더가 잘렸다")
        fingerprint = body[offset:offset + FINGERPRINT_BYTES]
        certificate_type = body[offset + FINGERPRINT_BYTES]
        encoded_length = int.from_bytes(
            body[offset + FINGERPRINT_BYTES + 1:offset + _ENTRY_HEADER_BYTES], "big")
        if encoded_length < 1:
            raise CertificateReportError(f"entry {index}: encoded_length는 1 이상이어야 한다")
        start = offset + _ENTRY_HEADER_BYTES
        end = start + encoded_length
        if end > len(body):
            raise CertificateReportError(f"entry {index}: DER이 선언 길이보다 짧다")
        entries.append(ReportEntry(fingerprint=fingerprint,
                                   certificate_type=certificate_type,
                                   der=body[start:end]))
        offset = end

    if offset != len(body):
        raise CertificateReportError(f"entry 뒤에 {len(body) - offset} byte가 남았다")
    return entries


def describe(body: bytes) -> str:
    """콘솔 표시용."""
    entries = parse_report(body)
    lines = [f"Certificate Report: {len(entries)}개 항목"]
    for i, entry in enumerate(entries, 1):
        kind = "CRL" if entry.certificate_type == CERT_TYPE_CRL else "인증서"
        lines.append(
            f"  [{i}] {kind} type={entry.certificate_type} "
            f"지문={entry.fingerprint.hex().upper()} DER {len(entry.der)} byte")
    return "\n".join(lines)
