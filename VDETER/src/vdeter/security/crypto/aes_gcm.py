"""V2 detached AES-256-GCM wrapper adapted from the crypto feature branch."""

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ...common.errors import ErrorCode
from ..constants import AAD_BYTES, KEY_BYTES, NONCE_BYTES, TAG_BYTES
from ..errors import SecurityFault


def _validate(key: bytes, nonce: bytes, aad: bytes):
    if type(key) is not bytes or len(key) != KEY_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE)
    if type(nonce) is not bytes or len(nonce) != NONCE_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID)
    if type(aad) is not bytes or len(aad) != AAD_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)


def encrypt_detached(*, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    _validate(key, nonce, aad)
    if type(plaintext) is not bytes:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
    combined = AESGCM(key).encrypt(nonce, plaintext, aad)
    return combined[:-TAG_BYTES], combined[-TAG_BYTES:]


def decrypt_detached(*, key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes:
    _validate(key, nonce, aad)
    if type(ciphertext) is not bytes or type(tag) is not bytes or len(tag) != TAG_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext + tag, aad)
    except InvalidTag as exc:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED) from exc
