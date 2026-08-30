"""X.509 v3 ECDSA 공개키 인증서 생성·파싱·검증 (SC-VDE-001).

암호 파라미터는 ITU-R M.2092-2 §A4-4.15 에 따라 고정이며 선택 불가다.

  * 공개키 알고리즘 : id-ecPublicKey
  * 곡선           : secp256r1 (P-256), 공개키 256 bit
  * 발급자 서명    : ecdsa-with-SHA256
  * 서명 인코딩    : IEEE P1363 고정 길이 연접 (r‖s, 각 32 byte = 64 byte)
  * 인증서 지문    : signatureValue 최하위 48 bit (6 byte)
  * 인증서 최대    : 1,024 byte

인증서는 지시서 1장의 **계열 B(원시 payload)** 다. 즉 우리 비트 봉투로 감싸지
않고 DER 바이트 자체가 payload 이며, 분할·재조립·자원할당은 전부 링크
(VDE-TER Service) 내부 일이다. 이 모듈은 DER 바이트만 다룬다.

================================================================
백엔드 두 갈래
================================================================
표준 경로 (``cryptography`` 설치됨)
    진짜 X.509 v3 DER 를 만들고 ECDSA(P-256/SHA-256) 로 서명·검증한다.

대체 경로 (``cryptography`` 없음)  ─  **실제 공개키 암호가 아니다**
    같은 함수 시그니처를 유지하되, 자체 정의한 DER 유사 TLV 구조를 쓰고
    "서명"은 HMAC-SHA256 으로 만든다. HMAC 키는 발급자의 **공개** 키 바이트라서
    공개키를 아는 쪽은 누구나 위조할 수 있다. 즉 이 경로는 위·변조 **탐지**
    (무결성)와 흐름 시험만 가능하고, 부인방지·인증 강도는 전혀 없다.
    **테스트·데모 전용이며 운용 링크에 절대 쓰지 말 것.**
    두 경로는 DER 이 서로 호환되지 않는다(대체 경로 인증서는 표준 파서가 못 읽음).

================================================================
통합 시 확인할 것 (외부 작성분이므로 저장소 값과 대조 필요)
================================================================
1. 확장 필드 OID (`OID_MASS_ID` 등)는 **임시 사설 arc** 다. 정식 배정 OID 로
   교체할 것. 교체해도 파싱 로직은 그대로 동작한다.
2. 저장소의 ``common/security/signature.py`` 가 있으면 그 모듈의
   ``default_backend().is_standard`` 를 존중해 경로를 고른다. 없으면
   ``cryptography`` 설치 여부로 판단한다.
3. ``build_certificate`` 에 문서에 없던 키워드 인자 ``is_ca`` 를 하나 추가했다
   (기본 False). CA 자기서명 인증서에 BasicConstraints(ca=TRUE) 를 넣기 위한
   것으로, 문서에 적힌 호출 형태는 전부 그대로 동작한다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# 규격 상수
# --------------------------------------------------------------------------

CURVE_NAME = "secp256r1"                 # ITU-R M.2092-2 §A4-4.15
HASH_NAME = "sha256"
MAX_CERTIFICATE_BYTES = 1024             # 인증서 최대 크기
FINGERPRINT_BITS = 48                    # 지문 = signatureValue 최하위 48 bit
FINGERPRINT_BYTES = FINGERPRINT_BITS // 8
P1363_SIGNATURE_BYTES = 64               # r‖s, 각 32 byte

#: 확장 필드 OID. **임시 사설 arc** — 정식 배정 OID 로 교체 필요.
_PRIVATE_ARC = "1.3.6.1.4.1.99999.1"
OID_MASS_ID = _PRIVATE_ARC + ".1"
OID_ROC_ID = _PRIVATE_ARC + ".2"
OID_MMSI = _PRIVATE_ARC + ".3"
OID_IMO = _PRIVATE_ARC + ".4"

#: 대체 경로에서 쓰는 확장 필드 이름(같은 의미, DER 유사 구조용)
_EXT_KEYS = (("mass_id", OID_MASS_ID), ("roc_id", OID_ROC_ID),
             ("mmsi", OID_MMSI), ("imo", OID_IMO))

BACKEND_STANDARD = "ecdsa-p256"          # 표준 경로 식별자
BACKEND_TEST = "hmac-test"               # 대체 경로 식별자

_ALT_SIG_ALGORITHM = "hmac-sha256-testing"   # 대체 경로 signatureAlgorithm 표기


# --------------------------------------------------------------------------
# 백엔드 선택
# --------------------------------------------------------------------------

try:  # 표준 경로 의존성
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives.asymmetric import utils as _asym_utils
    from cryptography.x509.oid import NameOID as _NameOID

    _HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - 환경 의존
    _HAVE_CRYPTOGRAPHY = False


def _repo_prefers_standard() -> bool | None:
    """저장소의 ``common/security/signature.py`` 가 고른 경로를 물어본다.

    그 모듈은 ``default_backend()`` 가 ``is_standard`` 속성을 가진 객체를
    돌려준다고 지시서 4.4 에 적혀 있다. 모듈이 없으면 ``None``.
    """
    try:
        from . import signature as _sig  # type: ignore[attr-defined]
    except Exception:
        return None
    try:
        return bool(_sig.default_backend().is_standard)
    except Exception:
        return None


_forced_backend: str | None = None


def set_backend(name: str | None) -> None:
    """백엔드를 강제한다. ``None`` 이면 자동 선택으로 되돌린다.

    테스트에서 두 경로를 모두 돌려보기 위한 것이다
    (지시서 4.6 수용조건 7). 표준 경로는 ``cryptography`` 가 있어야 한다.
    """
    global _forced_backend
    if name not in (None, BACKEND_STANDARD, BACKEND_TEST):
        raise ValueError(f"알 수 없는 백엔드: {name!r}")
    if name == BACKEND_STANDARD and not _HAVE_CRYPTOGRAPHY:
        raise RuntimeError("cryptography 가 없어 표준 경로를 쓸 수 없다")
    _forced_backend = name


def default_backend() -> str:
    """현재 유효한 백엔드 식별자를 돌려준다."""
    if _forced_backend is not None:
        return _forced_backend
    if os.environ.get("MASSROC_FORCE_TEST_BACKEND") == "1":
        return BACKEND_TEST
    if not _HAVE_CRYPTOGRAPHY:
        return BACKEND_TEST
    if _repo_prefers_standard() is False:
        # 저장소가 이미 대체 백엔드를 쓰기로 했으면 그 결정을 따른다.
        return BACKEND_TEST
    return BACKEND_STANDARD


def is_standard_backend() -> bool:
    """실제 공개키 암호(ECDSA P-256)를 쓰는 경로면 True."""
    return default_backend() == BACKEND_STANDARD


# --------------------------------------------------------------------------
# CertificateInfo
# --------------------------------------------------------------------------

@dataclass
class CertificateInfo:
    """파싱된 인증서 내용."""

    subject: str            # 인증 대상 (예: "MASS-440123456")
    issuer: str             # 발급자 (예: "ROC-CA")
    serial_number: int
    not_before: float       # epoch seconds (UTC)
    not_after: float
    public_key_der: bytes   # 대상의 공개키 (SubjectPublicKeyInfo, DER)
    fingerprint: bytes      # 48-bit(6 byte), signatureValue 최하위 48 bit
    # 확장 필드
    mass_id: str | None = None
    roc_id: str | None = None
    mmsi: str | None = None
    imo: str | None = None


class CertificateError(ValueError):
    """인증서 생성·파싱 실패."""


# --------------------------------------------------------------------------
# 최소 DER TLV 도우미 (대체 경로 전용, 표준 라이브러리만 사용)
# --------------------------------------------------------------------------

_TAG_INTEGER = 0x02
_TAG_OCTET_STRING = 0x04
_TAG_UTF8_STRING = 0x0C
_TAG_SEQUENCE = 0x30


def _der_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(value)) + value


def _der_int(value: int) -> bytes:
    if value < 0:
        raise CertificateError("음수 INTEGER 는 지원하지 않는다")
    body = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if body[0] & 0x80:                      # DER: 부호 비트 충돌 시 0x00 선행
        body = b"\x00" + body
    return _tlv(_TAG_INTEGER, body)


def _der_utf8(text) -> bytes:
    # MMSI·IMO는 저장소 전체에서 정수로 다룬다(config.MASS_MMSI 등). 인증서
    # 확장은 UTF8String이므로 여기서 문자열로 맞춘다. 정수를 넘겼을 때
    # AttributeError로 죽지 않게 하려는 것이다.
    if not isinstance(text, str):
        text = str(text)
    return _tlv(_TAG_UTF8_STRING, text.encode("utf-8"))


def _der_octets(data: bytes) -> bytes:
    return _tlv(_TAG_OCTET_STRING, data)


def _der_seq(*parts: bytes) -> bytes:
    return _tlv(_TAG_SEQUENCE, b"".join(parts))


def _read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """(tag, value, 다음 offset) 을 돌려준다."""
    if offset + 2 > len(data):
        raise CertificateError("TLV 헤더가 잘렸다")
    tag = data[offset]
    first = data[offset + 1]
    pos = offset + 2
    if first == 0x80:
        raise CertificateError("DER 은 부정 길이(indefinite length)를 허용하지 않는다")
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if pos + count > len(data):
            raise CertificateError("길이 필드가 잘렸다")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise CertificateError("TLV 내용이 잘렸다")
    return tag, data[pos:pos + length], pos + length


def _expect(data: bytes, offset: int, tag: int) -> tuple[bytes, int]:
    got_tag, value, nxt = _read_tlv(data, offset)
    if got_tag != tag:
        raise CertificateError(f"tag 0x{tag:02X} 를 기대했으나 0x{got_tag:02X}")
    return value, nxt


def _der_int_value(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


# --------------------------------------------------------------------------
# 대체 경로 키 객체
# --------------------------------------------------------------------------

_ALT_SPKI_LABEL = "test-p256-surrogate"


@dataclass(frozen=True)
class TestPublicKey:
    """대체 경로 공개키. **실제 타원곡선 공개키가 아니다.**"""

    raw: bytes   # 32 byte

    def public_bytes_der(self) -> bytes:
        """간이 SubjectPublicKeyInfo."""
        return _der_seq(_der_utf8(_ALT_SPKI_LABEL), _der_octets(self.raw))


@dataclass(frozen=True)
class TestPrivateKey:
    """대체 경로 개인키. **실제 타원곡선 개인키가 아니다.**"""

    seed: bytes  # 32 byte

    def public_key(self) -> TestPublicKey:
        return TestPublicKey(hashlib.sha256(b"massroc-test-pub" + self.seed).digest())


def generate_key_pair():
    """현재 백엔드에 맞는 (개인키, 공개키) 쌍을 만든다.

    지시서에 시그니처가 없는 보조 함수다. 테스트·데모가 백엔드에 상관없이
    같은 코드로 키를 만들 수 있게 하려고 둔다. 표준 경로에서는
    ``cryptography`` 의 EC 키 객체를, 대체 경로에서는 위 두 dataclass 를 준다.
    """
    if is_standard_backend():
        private_key = _ec.generate_private_key(_ec.SECP256R1())
        return private_key, private_key.public_key()
    private_key = TestPrivateKey(os.urandom(32))
    return private_key, private_key.public_key()


def public_key_from_der(spki_der: bytes):
    """SubjectPublicKeyInfo DER 에서 공개키 객체를 복원한다."""
    if is_standard_backend():
        return _ser.load_der_public_key(spki_der)
    body, _ = _expect(spki_der, 0, _TAG_SEQUENCE)
    label_raw, off = _expect(body, 0, _TAG_UTF8_STRING)
    if label_raw.decode("utf-8") != _ALT_SPKI_LABEL:
        raise CertificateError("대체 경로 SPKI 라벨이 아니다")
    raw, _ = _expect(body, off, _TAG_OCTET_STRING)
    return TestPublicKey(raw)


def _public_key_der(public_key) -> bytes:
    if isinstance(public_key, TestPublicKey):
        return public_key.public_bytes_der()
    return public_key.public_bytes(
        _ser.Encoding.DER, _ser.PublicFormat.SubjectPublicKeyInfo)


# --------------------------------------------------------------------------
# 서명 인코딩 (IEEE P1363)
# --------------------------------------------------------------------------

def _to_p1363(signature_value: bytes) -> bytes:
    """signatureValue 를 P1363 고정 길이 연접(r‖s, 64 byte)으로 정규화한다.

    표준 경로의 X.509 signatureValue 는 DER 로 감싼 ECDSA-Sig-Value 라서
    길이가 70~72 byte 사이에서 흔들린다. 지문(최하위 48 bit)이 인코딩 변동에
    흔들리지 않도록 항상 P1363 으로 바꾼 뒤 잘라 쓴다
    (ITU-R M.2092-2 §A4-4.15 의 전송용 서명 인코딩과도 일치).
    """
    if len(signature_value) == P1363_SIGNATURE_BYTES:
        return signature_value                       # 대체 경로는 이미 64 byte
    r, s = _asym_utils.decode_dss_signature(signature_value)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --------------------------------------------------------------------------
# 표준 경로 구현
# --------------------------------------------------------------------------

def _std_naive_or_aware(epoch: float) -> datetime:
    return datetime.fromtimestamp(int(epoch), timezone.utc)


def _std_build(subject, issuer, subject_public_key, issuer_private_key,
               serial_number, not_before, not_after, *,
               mass_id, roc_id, mmsi, imo, is_ca) -> bytes:
    builder = (
        _x509.CertificateBuilder()
        .subject_name(_x509.Name([_x509.NameAttribute(_NameOID.COMMON_NAME, subject)]))
        .issuer_name(_x509.Name([_x509.NameAttribute(_NameOID.COMMON_NAME, issuer)]))
        .public_key(subject_public_key)
        .serial_number(serial_number)
        .not_valid_before(_std_naive_or_aware(not_before))
        .not_valid_after(_std_naive_or_aware(not_after))
        .add_extension(_x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    )
    for attr, oid in _EXT_KEYS:
        value = {"mass_id": mass_id, "roc_id": roc_id, "mmsi": mmsi, "imo": imo}[attr]
        if value is None:
            continue
        builder = builder.add_extension(
            _x509.UnrecognizedExtension(_x509.ObjectIdentifier(oid), _der_utf8(value)),
            critical=False,
        )
    certificate = builder.sign(issuer_private_key, _hashes.SHA256())
    return certificate.public_bytes(_ser.Encoding.DER)


def _std_common_name(name) -> str:
    attrs = name.get_attributes_for_oid(_NameOID.COMMON_NAME)
    if not attrs:
        raise CertificateError("commonName 이 없다")
    return attrs[0].value


def _std_epoch(certificate, field: str) -> float:
    value = getattr(certificate, field + "_utc", None)
    if value is None:                                   # 구버전 cryptography
        value = getattr(certificate, field).replace(tzinfo=timezone.utc)
    return float(value.timestamp())


def _std_extension(certificate, oid: str) -> str | None:
    try:
        extension = certificate.extensions.get_extension_for_oid(
            _x509.ObjectIdentifier(oid))
    except _x509.ExtensionNotFound:
        return None
    raw = extension.value.value                          # UnrecognizedExtension
    text, _ = _expect(raw, 0, _TAG_UTF8_STRING)
    return text.decode("utf-8")


def _std_parse(der: bytes) -> CertificateInfo:
    certificate = _x509.load_der_x509_certificate(der)
    return CertificateInfo(
        subject=_std_common_name(certificate.subject),
        issuer=_std_common_name(certificate.issuer),
        serial_number=certificate.serial_number,
        not_before=_std_epoch(certificate, "not_valid_before"),
        not_after=_std_epoch(certificate, "not_valid_after"),
        public_key_der=_public_key_der(certificate.public_key()),
        fingerprint=_to_p1363(certificate.signature)[-FINGERPRINT_BYTES:],
        mass_id=_std_extension(certificate, OID_MASS_ID),
        roc_id=_std_extension(certificate, OID_ROC_ID),
        mmsi=_std_extension(certificate, OID_MMSI),
        imo=_std_extension(certificate, OID_IMO),
    )


def _std_check_signature(der: bytes, anchor_der: bytes) -> str:
    certificate = _x509.load_der_x509_certificate(der)
    anchor = _x509.load_der_x509_certificate(anchor_der)

    if _std_common_name(certificate.issuer) != _std_common_name(anchor.subject):
        return "issuer-mismatch"
    try:
        basic = anchor.extensions.get_extension_for_class(_x509.BasicConstraints)
        if not basic.value.ca:
            return "anchor-not-ca"
    except _x509.ExtensionNotFound:
        pass                                             # 확장이 없으면 판단 보류
    try:
        anchor.public_key().verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            _ec.ECDSA(_hashes.SHA256()),
        )
    except Exception:
        return "bad-signature"
    return "ok"


# --------------------------------------------------------------------------
# 대체 경로 구현 (실제 공개키 암호 아님)
# --------------------------------------------------------------------------

def _alt_sign(tbs: bytes, issuer_public_raw: bytes) -> bytes:
    """HMAC-SHA256 두 번으로 64 byte "서명"을 만든다 (r‖s 자리 맞춤용)."""
    left = hmac.new(issuer_public_raw, b"r" + tbs, hashlib.sha256).digest()
    right = hmac.new(issuer_public_raw, b"s" + tbs, hashlib.sha256).digest()
    return left + right


def _alt_tbs(subject, issuer, serial_number, not_before, not_after,
             spki: bytes, extensions: list[tuple[str, str]]) -> bytes:
    return _der_seq(
        _der_int(3),                                     # version = v3
        _der_int(serial_number),
        _der_utf8(issuer),
        _der_utf8(subject),
        _der_int(int(not_before)),
        _der_int(int(not_after)),
        _der_octets(spki),
        _der_seq(*[_der_seq(_der_utf8(k), _der_utf8(v)) for k, v in extensions]),
    )


def _alt_build(subject, issuer, subject_public_key, issuer_private_key,
               serial_number, not_before, not_after, *,
               mass_id, roc_id, mmsi, imo, is_ca) -> bytes:
    if not isinstance(issuer_private_key, TestPrivateKey):
        raise CertificateError("대체 경로에는 TestPrivateKey 가 필요하다")
    if int(not_before) < 0 or int(not_after) < 0:
        raise CertificateError("대체 경로는 1970 이전 시각을 지원하지 않는다")

    values = {"mass_id": mass_id, "roc_id": roc_id, "mmsi": mmsi, "imo": imo}
    extensions = [(attr, values[attr]) for attr, _ in _EXT_KEYS
                  if values[attr] is not None]
    if is_ca:
        extensions.append(("basic_constraints_ca", "TRUE"))

    tbs = _alt_tbs(subject, issuer, serial_number, not_before, not_after,
                   _public_key_der(subject_public_key), extensions)
    signature = _alt_sign(tbs, issuer_private_key.public_key().raw)
    return _der_seq(tbs, _der_utf8(_ALT_SIG_ALGORITHM), _der_octets(signature))


def _alt_split(der: bytes) -> tuple[bytes, str, bytes]:
    """(tbs 원문, 서명알고리즘, signatureValue) 로 쪼갠다."""
    body, end = _expect(der, 0, _TAG_SEQUENCE)
    if end != len(der):
        raise CertificateError("인증서 뒤에 잉여 바이트가 있다")
    _, tbs_end = _expect(body, 0, _TAG_SEQUENCE)
    tbs = body[0:tbs_end]                                # tag 포함 원문 그대로
    algorithm_raw, off = _expect(body, tbs_end, _TAG_UTF8_STRING)
    signature, off = _expect(body, off, _TAG_OCTET_STRING)
    if off != len(body):
        raise CertificateError("인증서 구조에 잉여 필드가 있다")
    if len(signature) != P1363_SIGNATURE_BYTES:
        raise CertificateError("signatureValue 길이가 64 byte 가 아니다")
    return tbs, algorithm_raw.decode("utf-8"), signature


def _alt_parse_tbs(tbs: bytes) -> dict:
    body, _ = _expect(tbs, 0, _TAG_SEQUENCE)
    version_raw, off = _expect(body, 0, _TAG_INTEGER)
    if _der_int_value(version_raw) != 3:
        raise CertificateError("v3 인증서가 아니다")
    serial_raw, off = _expect(body, off, _TAG_INTEGER)
    issuer_raw, off = _expect(body, off, _TAG_UTF8_STRING)
    subject_raw, off = _expect(body, off, _TAG_UTF8_STRING)
    not_before_raw, off = _expect(body, off, _TAG_INTEGER)
    not_after_raw, off = _expect(body, off, _TAG_INTEGER)
    spki, off = _expect(body, off, _TAG_OCTET_STRING)
    extensions_raw, off = _expect(body, off, _TAG_SEQUENCE)
    if off != len(body):
        raise CertificateError("tbsCertificate 에 잉여 필드가 있다")

    extensions: dict[str, str] = {}
    pos = 0
    while pos < len(extensions_raw):
        item, pos = _expect(extensions_raw, pos, _TAG_SEQUENCE)
        key_raw, inner = _expect(item, 0, _TAG_UTF8_STRING)
        value_raw, inner = _expect(item, inner, _TAG_UTF8_STRING)
        extensions[key_raw.decode("utf-8")] = value_raw.decode("utf-8")

    return {
        "serial_number": _der_int_value(serial_raw),
        "issuer": issuer_raw.decode("utf-8"),
        "subject": subject_raw.decode("utf-8"),
        "not_before": float(_der_int_value(not_before_raw)),
        "not_after": float(_der_int_value(not_after_raw)),
        "public_key_der": spki,
        "extensions": extensions,
    }


def _alt_parse(der: bytes) -> CertificateInfo:
    tbs, algorithm, signature = _alt_split(der)
    if algorithm != _ALT_SIG_ALGORITHM:
        raise CertificateError(f"알 수 없는 서명 알고리즘: {algorithm!r}")
    fields = _alt_parse_tbs(tbs)
    extensions = fields["extensions"]
    return CertificateInfo(
        subject=fields["subject"],
        issuer=fields["issuer"],
        serial_number=fields["serial_number"],
        not_before=fields["not_before"],
        not_after=fields["not_after"],
        public_key_der=fields["public_key_der"],
        fingerprint=signature[-FINGERPRINT_BYTES:],
        mass_id=extensions.get("mass_id"),
        roc_id=extensions.get("roc_id"),
        mmsi=extensions.get("mmsi"),
        imo=extensions.get("imo"),
    )


def _alt_check_signature(der: bytes, anchor_der: bytes) -> str:
    tbs, algorithm, signature = _alt_split(der)
    if algorithm != _ALT_SIG_ALGORITHM:
        return "unsupported-algorithm"

    certificate_fields = _alt_parse_tbs(tbs)
    anchor_tbs, _, _ = _alt_split(anchor_der)
    anchor_fields = _alt_parse_tbs(anchor_tbs)

    if certificate_fields["issuer"] != anchor_fields["subject"]:
        return "issuer-mismatch"
    if anchor_fields["extensions"].get("basic_constraints_ca") == "FALSE":
        return "anchor-not-ca"

    anchor_public = public_key_from_der(anchor_fields["public_key_der"])
    expected = _alt_sign(tbs, anchor_public.raw)
    if not hmac.compare_digest(expected, signature):
        return "bad-signature"
    return "ok"


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------

def build_certificate(
    subject: str, issuer: str,
    subject_public_key,          # 대상 공개키 객체
    issuer_private_key,          # 발급자(CA) 개인키 — 이 키로 서명
    serial_number: int,
    not_before: float, not_after: float,
    *, mass_id=None, roc_id=None, mmsi=None, imo=None,
    is_ca: bool = False,
) -> bytes:
    """X.509 v3 ECDSA(secp256r1/SHA256) 인증서를 DER 바이트로 만든다.

    확장 필드에 mass_id/roc_id/mmsi/imo 를 넣는다. 크기 1,024 byte 초과 시
    ValueError. ``is_ca=True`` 면 BasicConstraints(ca=TRUE) 를 넣는다
    (신뢰 앵커용 자기서명 인증서에 쓴다).
    """
    if serial_number <= 0:
        raise CertificateError("serialNumber 는 양의 정수여야 한다 (RFC 5280 §4.1.2.2)")
    if not_after < not_before:
        raise CertificateError("not_after 가 not_before 보다 이르다")

    builder = _std_build if is_standard_backend() else _alt_build
    try:
        der = builder(subject, issuer, subject_public_key, issuer_private_key,
                      serial_number, not_before, not_after,
                      mass_id=mass_id, roc_id=roc_id, mmsi=mmsi, imo=imo,
                      is_ca=is_ca)
    except CertificateError:
        raise
    except ValueError as exc:
        raise CertificateError(f"인증서 생성 실패: {exc}") from exc

    if len(der) > MAX_CERTIFICATE_BYTES:
        raise ValueError(
            f"인증서가 {len(der)} byte 로 한도 {MAX_CERTIFICATE_BYTES} byte 를 넘는다 "
            "(ITU-R M.2092-2 §A4-4.15)")
    return der


def parse_certificate(der: bytes) -> CertificateInfo:
    """DER 를 파싱해 CertificateInfo 로 돌려준다."""
    if not isinstance(der, (bytes, bytearray)):
        raise CertificateError("DER 은 bytes 여야 한다")
    der = bytes(der)
    parser = _std_parse if is_standard_backend() else _alt_parse
    try:
        return parser(der)
    except CertificateError:
        raise
    except Exception as exc:
        raise CertificateError(f"인증서 파싱 실패: {exc}") from exc


def verify_certificate(der: bytes, trust_anchor_der: bytes,
                       now: float) -> tuple[bool, str]:
    """발급자 서명을 trust_anchor 의 공개키로 검증하고 유효기간을 확인한다.

    반환: (성공여부, 사유). 사유: "ok" / "bad-signature" / "expired" /
    "not-yet-valid" / "too-large" / "parse-error" /
    "issuer-mismatch" / "anchor-not-ca" / "unsupported-algorithm".

    검사 순서는 서명 → 유효기간이다. 서명이 깨진 인증서의 유효기간 값은
    신뢰할 수 없으므로 먼저 서명을 본다.
    """
    if not isinstance(der, (bytes, bytearray)) or not der:
        return False, "parse-error"
    if len(der) > MAX_CERTIFICATE_BYTES:
        return False, "too-large"

    checker = _std_check_signature if is_standard_backend() else _alt_check_signature
    try:
        reason = checker(bytes(der), bytes(trust_anchor_der))
    except Exception:
        return False, "parse-error"
    if reason != "ok":
        return False, reason

    try:
        info = parse_certificate(der)
    except CertificateError:
        return False, "parse-error"

    if now < info.not_before:
        return False, "not-yet-valid"
    if now > info.not_after:
        return False, "expired"
    return True, "ok"


def fingerprint_of(der: bytes) -> bytes:
    """인증서 signatureValue 최하위 48 bit(6 byte)를 돌려준다."""
    if is_standard_backend():
        certificate = _x509.load_der_x509_certificate(bytes(der))
        return _to_p1363(certificate.signature)[-FINGERPRINT_BYTES:]
    _, _, signature = _alt_split(bytes(der))
    return signature[-FINGERPRINT_BYTES:]
