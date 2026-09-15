"""Type 74/75/76 공통 Data fragment wire field."""

from ...common.errors import ErrorCode
from .codec_base import MessageWireError, Reader, address_from_uint, address_to_uint, reserved, source_from_uint, u32, u8


def encode_common(body) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u8(body.session_id),
        u32(address_to_uint(body.destination)),
        u8(body.frame_fragment_count),
    ))


def decode_common(reader: Reader):
    source = source_from_uint(reader.uint(4))
    session = reader.uint(1)
    if session != 0:
        raise reserved("wire Session ID는 0이어야 합니다")
    destination = address_from_uint(reader.uint(4))
    count = reader.uint(1)
    return source, session, destination, count


def payload(reader: Reader) -> bytes:
    value = reader.remaining()
    if not value:
        raise MessageWireError(
            ErrorCode.LINK_DATA_FRAGMENT_PAYLOAD_EMPTY,
            "Data fragment payload가 비어 있습니다",
        )
    return value
