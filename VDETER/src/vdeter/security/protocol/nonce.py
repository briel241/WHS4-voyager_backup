"""Directional 96-bit AES-GCM nonce."""

from ...common.errors import ErrorCode
from ..constants import MAX_COUNTER, NONCE_BYTES, NONCE_PREFIX_BYTES
from ..errors import SecurityFault


def build_nonce(nonce_prefix: bytes, message_counter: int) -> bytes:
    if type(nonce_prefix) is not bytes or len(nonce_prefix) != NONCE_PREFIX_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE)
    if type(message_counter) is not int or not 1 <= message_counter <= MAX_COUNTER:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID)
    nonce = nonce_prefix + message_counter.to_bytes(4, "big")
    assert len(nonce) == NONCE_BYTES
    return nonce
