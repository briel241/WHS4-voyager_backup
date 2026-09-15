"""
서명 백엔드

M.2092-2 A4-4.15가 암호 파라미터를 지정한다. 우리가 고를 여지가 없다.

    PKI/인증서   ITU-T X.509 (10/2016)
    알고리즘     ECDSA
    곡선         secp256r1
    공개키       256 bit
    다이제스트   SHA-256
    인코딩       IEEE P1363 Fixed-Field Concatenation
    서명 필드    512 bit (64 byte)
    인증서 지문  signatureValue 최하위 48 bit (6 byte)

백엔드가 둘인 이유
    표준 백엔드(EcdsaP256)는 `cryptography` 패키지를 요구한다. 이 저장소의
    Python은 GNU Radio와 공유되고 의존성 규약이 없어 설치를 강제하지 않는다.
    패키지가 없으면 시험용 백엔드로 자동 대체하되, `is_standard=False`로
    분명히 구분한다. 시험용 백엔드는 대칭키 HMAC이므로 공개키 암호가 아니며
    실제 인증 강도를 갖지 않는다. 보안 실증에는 반드시 표준 백엔드를 쓴다.

    표준 백엔드 활성화:
        /opt/homebrew/bin/python3 -m pip install cryptography
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Protocol

# A4-4.15가 정한 크기
SIGNATURE_SIZE = 64  # 512 bit
FINGERPRINT_SIZE = 6  # 48 bit
PUBLIC_KEY_BITS = 256
CURVE_NAME = "secp256r1"
DIGEST_NAME = "SHA-256"


class SignatureBackend(Protocol):
    name: str
    is_standard: bool

    def generate_keypair(self) -> tuple[Any, Any]: ...

    def sign(self, private_key: Any, data: bytes) -> bytes: ...

    def verify(self, public_key: Any, data: bytes, signature: bytes) -> bool: ...

    def public_bytes(self, public_key: Any) -> bytes: ...


# ------------------------------------------------------------- 표준 백엔드


# ECDSA secp256r1 + SHA-256, IEEE P1363 고정 길이 연접 (A4-4.15)
class EcdsaP256Backend:
    name = "ECDSA-secp256r1-SHA256"
    is_standard = True

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        self._ec = ec
        self._hashes = hashes
        self._utils = utils
        self._serialization = serialization

    def generate_keypair(self) -> tuple[Any, Any]:
        private_key = self._ec.generate_private_key(self._ec.SECP256R1())
        return private_key, private_key.public_key()

    # DER 서명을 P1363 고정 길이 연접(r||s, 각 32 byte)으로 바꾼다.
    def sign(self, private_key: Any, data: bytes) -> bytes:
        der = private_key.sign(data, self._ec.ECDSA(self._hashes.SHA256()))
        r, s = self._utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def verify(self, public_key: Any, data: bytes, signature: bytes) -> bool:
        if len(signature) != SIGNATURE_SIZE:
            return False
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        der = self._utils.encode_dss_signature(r, s)
        try:
            public_key.verify(der, data, self._ec.ECDSA(self._hashes.SHA256()))
        except Exception:
            return False
        return True

    def public_bytes(self, public_key: Any) -> bytes:
        return public_key.public_bytes(
            encoding=self._serialization.Encoding.X962,
            format=self._serialization.PublicFormat.UncompressedPoint,
        )


# ------------------------------------------------------------- 시험용 백엔드


# HMAC-SHA256 기반 대체 백엔드.
#
# 주의: 대칭키 방식이므로 공개키 암호가 아니다. 검증자가 서명자와 같은 키를
# 가져야 하며, 따라서 실제 신원 인증을 제공하지 않는다. 메시지 구조와 배관을
# 시험하기 위한 것이며 보안 실증에 쓰면 안 된다.
class TestHmacBackend:
    name = "TEST-HMAC-SHA256 (비표준·대칭키)"
    is_standard = False

    def generate_keypair(self) -> tuple[bytes, bytes]:
        key = os.urandom(32)
        # 대칭키이므로 개인키와 공개키가 같다. 이것이 비표준인 이유다.
        return key, key

    def sign(self, private_key: bytes, data: bytes) -> bytes:
        # 표준 서명과 같은 64 byte를 채워 메시지 크기를 동일하게 유지한다.
        first = hmac.new(private_key, data, hashlib.sha256).digest()
        second = hmac.new(private_key, first + data, hashlib.sha256).digest()
        return first + second

    def verify(self, public_key: bytes, data: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(public_key, data), signature)

    def public_bytes(self, public_key: bytes) -> bytes:
        return public_key


# ------------------------------------------------------------- 선택


_CACHED: SignatureBackend | None = None


# 사용 가능한 백엔드를 고른다. 표준 백엔드를 우선한다.
def default_backend() -> SignatureBackend:
    global _CACHED
    if _CACHED is None:
        try:
            _CACHED = EcdsaP256Backend()
        except ImportError:
            _CACHED = TestHmacBackend()
    return _CACHED


# 표준 백엔드를 강제한다. 없으면 이유를 담아 예외를 낸다.
def require_standard_backend() -> SignatureBackend:
    backend = default_backend()
    if not backend.is_standard:
        raise RuntimeError(
            "표준 서명 백엔드(ECDSA secp256r1)를 쓸 수 없습니다. "
            "'python3 -m pip install cryptography'로 설치하십시오. "
            "보안 실증에는 시험용 백엔드를 쓸 수 없습니다."
        )
    return backend


# 인증서 지문 - signatureValue의 최하위 48 bit (A4-4.15, RFC 5480 4.1.1.3)
def fingerprint_from_certificate_signature(signature_value: bytes) -> bytes:
    if len(signature_value) < FINGERPRINT_SIZE:
        raise ValueError(
            f"signatureValue가 {FINGERPRINT_SIZE} byte보다 짧습니다 ({len(signature_value)})"
        )
    return signature_value[-FINGERPRINT_SIZE:]


# 인증서가 없는 단계의 임시 지문.
#
# SC-VDE-001에서 실제 인증서를 다루게 되면
# fingerprint_from_certificate_signature()로 교체한다.
def fingerprint_for_public_key(public_key_bytes: bytes) -> bytes:
    return hashlib.sha256(public_key_bytes).digest()[-FINGERPRINT_SIZE:]
