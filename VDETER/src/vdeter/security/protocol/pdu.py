"""Security Header || Ciphertext || 128-bit GCM tag."""

from ...common.errors import ErrorCode
from ..constants import HEADER_BYTES, PDU_OVERHEAD, TAG_BYTES
from ..errors import SecurityFault
from .header import SecurityHeader


def build_security_pdu(header: SecurityHeader, ciphertext: bytes, tag: bytes) -> bytes:
    if type(ciphertext) is not bytes or type(tag) is not bytes or len(tag) != TAG_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED)
    return header.to_bytes() + ciphertext + tag


def parse_security_pdu(data: bytes) -> tuple[SecurityHeader, bytes, bytes]:
    if type(data) is not bytes or len(data) < PDU_OVERHEAD:
        raise SecurityFault(ErrorCode.PRESENTATION_TRUNCATED, "Security PDU truncated")
    return SecurityHeader.parse(data[:HEADER_BYTES]), data[HEADER_BYTES:-TAG_BYTES], data[-TAG_BYTES:]
