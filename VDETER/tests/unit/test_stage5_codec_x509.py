"""TC-STAGE5-009: X.509 Certificate Report codec.

요구사항: `STAGE5-REQ-09`, `STAGE5-REQ-11`, `STAGE5-REQ-18`
(`stage5_service_integration_baseline.md` §6.2, §6.6)
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from vdeter.presentation.codecs.certificate_report import (
    CERTIFICATE_REPORT_WIRE_PREFIX,
    CertificateReportCodec,
)

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

#: 승인 문서 §6.6에 고정된 독립 기대값. production codec으로 생성하지 않았다.
X509_WIRE_SHA256 = "374b10e619ca33c6584c3eaf8510aa7bdcf8b54e54bdea5dae3796b8aaa65501"


def _load_vector(stem: str) -> tuple[bytes, bytes]:
    application = (VECTOR_ROOT / f"{stem}.application.bin").read_bytes()
    wire = (VECTOR_ROOT / f"{stem}.wire.bin").read_bytes()
    return application, wire


def _der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes((length,))
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(encoded),)) + encoded


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes((tag,)) + _der_length(len(content)) + content


def build_test_certificate_v3(*, self_issued: bool = False, version: int = 2) -> bytes:
    """production codec과 독립적으로 손으로 조립한 X.509 v3 구조 DER fixture.

    서명은 의미 없는 고정 bytes이며 구조 검증 전용이다. `self_issued=True`는
    subject==issuer인 self-issued intermediate 모양을 만든다.
    """

    name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"T"))))
    issuer = name
    subject = name if self_issued else _tlv(
        0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"U")))
    )
    validity = _tlv(
        0x30,
        _tlv(0x17, b"250101000000Z") + _tlv(0x17, b"350101000000Z"),
    )
    spki = _tlv(0x30, _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x02\x01")) + _tlv(0x03, b"\x00\x04\x01\x02"))
    tbs = _tlv(
        0x30,
        _tlv(0xA0, _tlv(0x02, bytes((version,))))  # [0] EXPLICIT version
        + _tlv(0x02, b"\x07")  # serialNumber
        + _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x04\x03\x02"))  # signature alg
        + issuer
        + validity
        + subject
        + spki,
    )
    algorithm = _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x04\x03\x02"))
    signature = _tlv(0x03, b"\x00" + bytes(range(16)))
    return _tlv(0x30, tbs + algorithm + signature)


def build_test_crl() -> bytes:
    """손으로 조립한 DER CRL 구조 fixture."""

    name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"C"))))
    tbs = _tlv(
        0x30,
        _tlv(0x02, b"\x01")  # version v2
        + _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x04\x03\x02"))
        + name
        + _tlv(0x17, b"250101000000Z"),
    )
    algorithm = _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x04\x03\x02"))
    signature = _tlv(0x03, b"\x00" + bytes(range(8)))
    return _tlv(0x30, tbs + algorithm + signature)


def build_report_body(entries) -> bytes:
    """(fingerprint, type, der) 목록에서 Certificate Report body를 손으로 조립한다."""

    body = bytes((len(entries),))
    for fingerprint, certificate_type, der in entries:
        body += fingerprint + bytes((certificate_type,)) + len(der).to_bytes(2, "big") + der
    return body


class Stage5CertificateReportGoldenVectorTest(unittest.TestCase):
    def setUp(self):
        self.codec = CertificateReportCodec()
        self.application, self.wire = _load_vector("gv_stage5_pres_x509_001")

    def test_TC_STAGE5_009_artifact_matches_approved_sha256(self):
        self.assertEqual(249, len(self.wire))
        self.assertEqual(X509_WIRE_SHA256, hashlib.sha256(self.wire).hexdigest())
        self.assertEqual(self.wire[2:], self.application)
        manifest = json.loads((VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["vectors"]["GV-STAGE5-PRES-X509-001"]
        self.assertEqual(X509_WIRE_SHA256, entry["wire"]["sha256"])
        self.assertEqual(
            hashlib.sha256(self.application).hexdigest(), entry["application"]["sha256"]
        )

    def test_TC_STAGE5_009_decode_returns_exact_application_bytes(self):
        decoded = self.codec.decode(self.wire)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(self.application, decoded.value)
        self.assertEqual(len(self.application), len(decoded.value))

    def test_TC_STAGE5_009_encode_matches_fixed_wire_bytes(self):
        encoded = self.codec.encode(self.application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(self.wire, encoded.value)
        self.assertEqual(X509_WIRE_SHA256, hashlib.sha256(encoded.value).hexdigest())

    def test_TC_STAGE5_009_vector_fields_are_the_approved_entry(self):
        body = self.application
        self.assertEqual(1, body[0])
        self.assertEqual(bytes((1, 2, 3, 4, 5, 6)), body[1:7])
        self.assertEqual(2, body[7])  # type 2 CRL
        self.assertEqual(0x00ED, int.from_bytes(body[8:10], "big"))


class Stage5CertificateReportStructureTest(unittest.TestCase):
    def setUp(self):
        self.codec = CertificateReportCodec()
        self.fingerprint = bytes((0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF))

    def test_TC_STAGE5_009_multi_entry_report_preserves_order_and_bytes(self):
        certificate = build_test_certificate_v3()
        other = build_test_certificate_v3(self_issued=True)
        crl = build_test_crl()
        body = build_report_body(
            (
                (self.fingerprint, 0, certificate),
                (self.fingerprint, 1, other),  # 같은 fingerprint 중복 entry 허용
                (bytes(6), 2, crl),
            )
        )
        encoded = self.codec.encode(body)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        # self round trip이 아니라 손으로 조립한 기대 wire와 직접 비교한다.
        self.assertEqual(CERTIFICATE_REPORT_WIRE_PREFIX + body, encoded.value)
        decoded = self.codec.decode(encoded.value)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(body, decoded.value)
        # entry 순서와 DER bytes가 재인코딩 없이 그대로 보존된다.
        offset = 1 + 6 + 1 + 2
        self.assertEqual(certificate, decoded.value[offset : offset + len(certificate)])

    def test_TC_STAGE5_009_self_issued_certificate_is_not_judged_as_root(self):
        # subject==issuer self-issued intermediate도 구조만 검증하고 수락한다
        # (`DR-STAGE5-026`: root/trust-anchor 판정 비수행).
        body = build_report_body(((self.fingerprint, 1, build_test_certificate_v3(self_issued=True)),))
        self.assertTrue(self.codec.encode(body).is_ok)

    def test_TC_STAGE5_009_reserved_type_is_rejected(self):
        for reserved in (3, 4, 255):
            body = build_report_body(((self.fingerprint, 0, build_test_certificate_v3()),))
            broken = bytearray(body)
            broken[7] = reserved
            result = self.codec.encode(bytes(broken))
            self.assertTrue(result.is_err, reserved)

    def test_TC_STAGE5_009_zero_entries_is_rejected(self):
        self.assertTrue(self.codec.encode(b"\x00").is_err)

    def test_TC_STAGE5_009_length_mismatch_is_rejected(self):
        certificate = build_test_certificate_v3()
        body = build_report_body(((self.fingerprint, 0, certificate),))
        self.assertTrue(self.codec.encode(body[:-1]).is_err)  # truncated object
        self.assertTrue(self.codec.encode(body + b"\x00").is_err)  # trailing bytes

    def test_TC_STAGE5_009_entry_count_mismatch_is_rejected(self):
        certificate = build_test_certificate_v3()
        body = build_report_body(((self.fingerprint, 0, certificate),))
        overstated = bytes((2,)) + body[1:]
        self.assertTrue(self.codec.encode(overstated).is_err)

    def test_TC_STAGE5_009_type_cross_validation(self):
        certificate = build_test_certificate_v3()
        crl = build_test_crl()
        # certificate DER를 type 2로 선언하면 거부한다.
        self.assertTrue(
            self.codec.encode(build_report_body(((self.fingerprint, 2, certificate),))).is_err
        )
        # CRL DER를 type 0/1로 선언하면 거부한다.
        for certificate_type in (0, 1):
            self.assertTrue(
                self.codec.encode(
                    build_report_body(((self.fingerprint, certificate_type, crl),))
                ).is_err,
                certificate_type,
            )

    def test_TC_STAGE5_009_non_v3_certificate_is_rejected(self):
        # version 필드가 없는 v1 모양(첫 child가 INTEGER serial)은 v3가 아니다.
        v1_like = build_test_crl()  # 첫 child INTEGER — certificate 요구 [0]이 없다.
        body = build_report_body(((self.fingerprint, 0, v1_like),))
        self.assertTrue(self.codec.encode(body).is_err)
        # [0]{INTEGER 1} (v2)도 거부한다.
        v2 = build_test_certificate_v3(version=1)
        self.assertTrue(
            self.codec.encode(build_report_body(((self.fingerprint, 0, v2),))).is_err
        )

    def test_TC_STAGE5_009_garbage_der_is_rejected(self):
        body = build_report_body(((self.fingerprint, 0, b"\x30\x03\x01\x01\xff"),))
        self.assertTrue(self.codec.encode(body).is_err)

    def test_TC_STAGE5_009_minimal_shell_certificate_is_rejected(self):
        # v3 version 표식만 흉내 낸 최소 외형(SEQ{SEQ{[0]{INT 2}}, SEQ{}, BITSTRING})은
        # TBSCertificate 필수 field가 없으므로 거부한다.
        fake = bytes.fromhex("300c" "3005" "a003" "020102" "3000" "030100")
        body = build_report_body(((self.fingerprint, 0, fake),))
        self.assertTrue(self.codec.encode(body).is_err)
        fake_crl = bytes.fromhex("3008" "3000" "3000" "030100")
        crl_body = build_report_body(((self.fingerprint, 2, fake_crl),))
        self.assertTrue(self.codec.encode(crl_body).is_err)

    def test_TC_STAGE5_009_missing_required_tbs_fields_are_rejected(self):
        # 정상 fixture에서 필수 field를 하나씩 제거·훼손한 변형을 모두 거부한다.
        name = _tlv(0x30, _tlv(0x31, _tlv(0x30, _tlv(0x06, b"\x55\x04\x03") + _tlv(0x0C, b"T"))))
        validity = _tlv(0x30, _tlv(0x17, b"250101000000Z") + _tlv(0x17, b"350101000000Z"))
        algid = _tlv(0x30, _tlv(0x06, b"\x2a\x86\x48\xce\x3d\x04\x03\x02"))
        spki = _tlv(0x30, algid + _tlv(0x03, b"\x00\x04"))
        version = _tlv(0xA0, _tlv(0x02, b"\x02"))
        serial = _tlv(0x02, b"\x07")
        signature = _tlv(0x03, b"\x00" + bytes(8))

        def cert(tbs_fields, outer_alg=algid, outer_sig=signature):
            return _tlv(0x30, _tlv(0x30, b"".join(tbs_fields)) + outer_alg + outer_sig)

        complete = [version, serial, algid, name, validity, name, spki]
        variants = {
            "no-serial": cert([version, algid, name, validity, name, spki]),
            "no-validity": cert([version, serial, algid, name, name, spki]),
            "validity-wrong-tags": cert(
                [version, serial, algid, name,
                 _tlv(0x30, _tlv(0x02, b"\x01") + _tlv(0x02, b"\x01")), name, spki]
            ),
            "algid-without-oid": cert(
                [version, serial, _tlv(0x30, _tlv(0x02, b"\x01")), name, validity, name, spki]
            ),
            "no-spki": cert([version, serial, algid, name, validity, name]),
            "spki-missing-bitstring": cert(
                [version, serial, algid, name, validity, name, _tlv(0x30, algid)]
            ),
            "signature-bad-unused-bits": cert(
                complete, outer_sig=_tlv(0x03, b"\x08" + bytes(8))
            ),
            "signature-empty": cert(complete, outer_sig=_tlv(0x03, b"\x00")),
            "outer-alg-not-oid": cert(
                complete, outer_alg=_tlv(0x30, _tlv(0x02, b"\x01"))
            ),
        }
        # 완전한 형태 자체는 수락된다 (구조 검증의 기준점).
        baseline = build_report_body(((self.fingerprint, 0, cert(complete)),))
        self.assertTrue(self.codec.encode(baseline).is_ok)
        for case, der in variants.items():
            with self.subTest(case=case):
                body = build_report_body(((self.fingerprint, 0, der),))
                self.assertTrue(self.codec.encode(body).is_err, case)

    def test_TC_STAGE5_009_decode_rejects_wrong_header_and_truncation(self):
        certificate = build_test_certificate_v3()
        body = build_report_body(((self.fingerprint, 0, certificate),))
        wire = CERTIFICATE_REPORT_WIRE_PREFIX + body
        self.assertTrue(self.codec.decode(wire[:12]).is_err)
        self.assertTrue(self.codec.decode(b"\x00\x02" + body).is_err)
        broken = bytearray(wire)
        broken[-1] ^= 0xFF  # BIT STRING 마지막 byte 훼손 - 길이는 그대로이므로 구조는 통과한다
        # 구조 검증만 수행하므로 서명 bytes 훼손은 codec이 판정하지 않는다.
        self.assertTrue(self.codec.decode(bytes(broken)).is_ok)


if __name__ == "__main__":
    unittest.main()
