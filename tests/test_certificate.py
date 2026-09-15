"""작업 A 수용조건 검증 (지시서 4.6). 표준 라이브러리 assert 만 사용.

    python3 tests/test_certificate.py

통과하면 조용히 끝나고, 실패하면 AssertionError 가 난다.
수용조건 7 에 따라 1~6 을 **백엔드 두 경로 모두**에서 돌린다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.security import certificate as cert
from common.security.cert_flow import mass_receive_certificate, roc_issue_certificate
from common.security.trust_store import TrustStore

NOW = 1_760_000_000.0          # 기준 시각 (2025-10 경)
DAY = 86_400.0


# --------------------------------------------------------------------------
# 준비 도우미
# --------------------------------------------------------------------------

def make_ca(now=NOW):
    """자기서명 CA 인증서와 그 개인키를 만든다."""
    private_key, public_key = cert.generate_key_pair()
    der = cert.build_certificate(
        subject="ROC-CA", issuer="ROC-CA",
        subject_public_key=public_key, issuer_private_key=private_key,
        serial_number=1, not_before=now - DAY, not_after=now + 365 * DAY,
        roc_id="ROC-01", is_ca=True,
    )
    return private_key, der


def make_vessel_cert(ca_key, *, now=NOW, not_before=None, not_after=None,
                     serial_number=2, **extensions):
    """MASS 단말 인증서를 만든다."""
    _, public_key = cert.generate_key_pair()
    fields = dict(mass_id="MASS-01", roc_id="ROC-01",
                  mmsi="440123456", imo="9074729")
    fields.update(extensions)
    return cert.build_certificate(
        subject="MASS-440123456", issuer="ROC-CA",
        subject_public_key=public_key, issuer_private_key=ca_key,
        serial_number=serial_number,
        not_before=now - DAY if not_before is None else not_before,
        not_after=now + 30 * DAY if not_after is None else not_after,
        **fields,
    )


def flip_a_byte(der: bytes, index: int) -> bytes:
    """바이트 하나의 비트를 뒤집는다 (위조 모사)."""
    mutated = bytearray(der)
    mutated[index] ^= 0x01
    return bytes(mutated)


# --------------------------------------------------------------------------
# 1. 생성 → 파싱 라운드트립
# --------------------------------------------------------------------------

def check_roundtrip():
    ca_key, _ = make_ca()
    der = make_vessel_cert(ca_key)

    info = cert.parse_certificate(der)
    assert info.subject == "MASS-440123456", info.subject
    assert info.issuer == "ROC-CA", info.issuer
    assert info.serial_number == 2, info.serial_number
    assert info.not_before == int(NOW - DAY), info.not_before
    assert info.not_after == int(NOW + 30 * DAY), info.not_after
    assert info.mass_id == "MASS-01", info.mass_id
    assert info.roc_id == "ROC-01", info.roc_id
    assert info.mmsi == "440123456", info.mmsi
    assert info.imo == "9074729", info.imo

    # 지문은 signatureValue 최하위 48 bit = 6 byte
    assert len(info.fingerprint) == cert.FINGERPRINT_BYTES == 6
    assert info.fingerprint == cert.fingerprint_of(der)

    # 공개키가 SubjectPublicKeyInfo 로 복원 가능해야 한다
    assert cert.public_key_from_der(info.public_key_der) is not None

    # 확장 필드를 생략하면 None 으로 남는다
    bare = cert.build_certificate(
        subject="MASS-2", issuer="ROC-CA",
        subject_public_key=cert.generate_key_pair()[1], issuer_private_key=ca_key,
        serial_number=3, not_before=NOW - DAY, not_after=NOW + DAY,
    )
    bare_info = cert.parse_certificate(bare)
    assert bare_info.mass_id is None and bare_info.mmsi is None


# --------------------------------------------------------------------------
# 2. 정상 검증
# --------------------------------------------------------------------------

def check_valid_signature():
    ca_key, ca_der = make_ca()
    der = make_vessel_cert(ca_key)
    assert cert.verify_certificate(der, ca_der, NOW) == (True, "ok")

    # 다른 CA 로는 통과하면 안 된다
    other_key, other_der = make_ca()
    ok, reason = cert.verify_certificate(der, other_der, NOW)
    assert ok is False, "다른 CA 의 앵커로 검증이 통과했다"
    assert reason in ("bad-signature", "issuer-mismatch"), reason


# --------------------------------------------------------------------------
# 3. 위조 탐지
# --------------------------------------------------------------------------

def check_tamper_detected():
    ca_key, ca_der = make_ca()
    der = make_vessel_cert(ca_key)

    # signatureValue 안쪽 한 바이트를 뒤집는다 → 서명 불일치
    assert cert.verify_certificate(flip_a_byte(der, len(der) - 1), ca_der, NOW) \
        == (False, "bad-signature")

    # tbsCertificate 안쪽(확장 값 문자열 영역)을 건드려도 잡아야 한다
    marker = der.find(b"440123456")
    assert marker > 0, "MMSI 확장 값을 DER 에서 찾지 못했다"
    assert cert.verify_certificate(flip_a_byte(der, marker), ca_der, NOW) \
        == (False, "bad-signature")

    # 완전히 깨진 바이트열은 parse-error
    assert cert.verify_certificate(b"\x30\x03not-a-cert", ca_der, NOW)[0] is False
    assert cert.verify_certificate(b"", ca_der, NOW) == (False, "parse-error")


# --------------------------------------------------------------------------
# 4. 유효기간
# --------------------------------------------------------------------------

def check_validity_window():
    ca_key, ca_der = make_ca()

    expired = make_vessel_cert(ca_key, not_before=NOW - 10 * DAY,
                               not_after=NOW - DAY)
    assert cert.verify_certificate(expired, ca_der, NOW) == (False, "expired")

    future = make_vessel_cert(ca_key, not_before=NOW + DAY,
                              not_after=NOW + 10 * DAY)
    assert cert.verify_certificate(future, ca_der, NOW) == (False, "not-yet-valid")

    # 경계값: not_before 와 not_after 시각 자체는 유효
    window = make_vessel_cert(ca_key, not_before=NOW, not_after=NOW + DAY)
    assert cert.verify_certificate(window, ca_der, NOW) == (True, "ok")
    assert cert.verify_certificate(window, ca_der, NOW + DAY) == (True, "ok")
    assert cert.verify_certificate(window, ca_der, NOW + DAY + 1) \
        == (False, "expired")
    assert cert.verify_certificate(window, ca_der, NOW - 1) \
        == (False, "not-yet-valid")


# --------------------------------------------------------------------------
# 5. 신뢰저장소 멱등 + 실패 시 저장소 불변
# --------------------------------------------------------------------------

def check_trust_store():
    ca_key, ca_der = make_ca()
    der = make_vessel_cert(ca_key)
    trust = TrustStore()

    ok, reason = mass_receive_certificate(der, trust, ca_der, NOW)
    assert (ok, reason) == (True, "ok")
    assert len(trust) == 1

    # 같은 인증서 재수신 → 여전히 1건 (멱등)
    assert mass_receive_certificate(der, trust, ca_der, NOW) == (True, "ok")
    trust.add(der)
    assert len(trust) == 1, "같은 지문이 두 번 저장됐다"

    fingerprint = cert.fingerprint_of(der)
    assert fingerprint in trust
    assert trust.get(fingerprint) == der
    assert trust.public_key_of(fingerprint) is not None
    assert trust.get(b"\x00" * 6) is None
    assert (b"\x00" * 6) not in trust

    # 검증 실패 인증서는 저장소에 반영되지 않는다 (ITU §A4-4.15)
    forged = flip_a_byte(der, len(der) - 1)
    before = list(trust.fingerprints())
    ok, reason = mass_receive_certificate(forged, trust, ca_der, NOW)
    assert ok is False and reason == "bad-signature"
    assert list(trust.fingerprints()) == before, "실패했는데 저장소가 바뀌었다"
    assert cert.fingerprint_of(forged) not in trust

    expired = make_vessel_cert(ca_key, not_before=NOW - 10 * DAY,
                               not_after=NOW - DAY, serial_number=9)
    ok, reason = mass_receive_certificate(expired, trust, ca_der, NOW)
    assert (ok, reason) == (False, "expired")
    assert len(trust) == 1, "만료 인증서가 저장됐다"


# --------------------------------------------------------------------------
# 6. 크기 한계 1,024 byte
# --------------------------------------------------------------------------

def check_size_limit():
    ca_key, ca_der = make_ca()

    normal = make_vessel_cert(ca_key)
    assert len(normal) <= cert.MAX_CERTIFICATE_BYTES, len(normal)

    # 확장 값을 키워 한도를 넘기면 생성 단계에서 ValueError
    try:
        make_vessel_cert(ca_key, mass_id="M" * 1200)
    except ValueError as exc:
        assert "1024" in str(exc) or "한도" in str(exc), str(exc)
    else:
        raise AssertionError("1,024 byte 초과 인증서가 생성됐다")

    # 수신 측도 한도 초과 payload 를 거부한다
    oversized = b"\x30" + b"\x00" * cert.MAX_CERTIFICATE_BYTES
    assert cert.verify_certificate(oversized, ca_der, NOW) == (False, "too-large")

    trust = TrustStore()
    assert mass_receive_certificate(oversized, trust, ca_der, NOW) \
        == (False, "too-large")
    assert len(trust) == 0


# --------------------------------------------------------------------------
# 행위자 흐름 (지시서 4.5)
# --------------------------------------------------------------------------

def check_outbound_contract():
    ca_key, _ = make_ca()
    der = make_vessel_cert(ca_key)

    outbound = roc_issue_certificate(der, mass_id=42)
    assert outbound.payload == der, "payload 는 DER 바이트 그대로여야 한다"
    assert outbound.destination_id == 42
    assert outbound.ack_required is True
    assert outbound.data_type == "X509_CERTIFICATE"
    assert outbound.content_type == "application/pkix-cert"

    # broadcast 에는 ack_required=True 금지 → 인증서는 broadcast 불가
    try:
        roc_issue_certificate(der, mass_id=0)
    except ValueError:
        pass
    else:
        raise AssertionError("broadcast 인증서 전송이 허용됐다")

    try:
        roc_issue_certificate(b"", mass_id=42)
    except ValueError:
        pass
    else:
        raise AssertionError("빈 payload 가 허용됐다")


def check_input_validation():
    ca_key, _ = make_ca()
    _, public_key = cert.generate_key_pair()

    try:                                   # serialNumber 는 양수 (RFC 5280)
        cert.build_certificate("A", "ROC-CA", public_key, ca_key, 0,
                               NOW, NOW + DAY)
    except ValueError:
        pass
    else:
        raise AssertionError("serial_number=0 이 허용됐다")

    try:                                   # not_after < not_before
        cert.build_certificate("A", "ROC-CA", public_key, ca_key, 5,
                               NOW + DAY, NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("역전된 유효기간이 허용됐다")


# --------------------------------------------------------------------------
# 실행 (수용조건 7: 두 경로 모두)
# --------------------------------------------------------------------------

CHECKS = (
    ("1. 생성→파싱 라운드트립", check_roundtrip),
    ("2. 정상 검증", check_valid_signature),
    ("3. 위조 탐지", check_tamper_detected),
    ("4. 유효기간", check_validity_window),
    ("5. 신뢰저장소 멱등·불변", check_trust_store),
    ("6. 크기 한계", check_size_limit),
    ("7. Outbound 계약", check_outbound_contract),
    ("8. 입력 검증", check_input_validation),
)


def run_all(backend_name: str) -> None:
    cert.set_backend(backend_name)
    try:
        label = "표준(ECDSA P-256)" if cert.is_standard_backend() \
            else "대체(HMAC, 표준 아님)"
        for title, check in CHECKS:
            try:
                check()
            except AssertionError as exc:
                raise AssertionError(f"[{label}] {title} 실패: {exc}") from exc
    finally:
        cert.set_backend(None)


if __name__ == "__main__":
    backends = [cert.BACKEND_TEST]
    if cert._HAVE_CRYPTOGRAPHY:
        backends.insert(0, cert.BACKEND_STANDARD)
    else:
        print("주의: cryptography 가 없어 표준 경로는 건너뛴다", file=sys.stderr)

    for backend in backends:
        run_all(backend)
