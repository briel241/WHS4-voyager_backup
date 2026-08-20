"""Type 92 Short data with ACK codec (Table 40)."""

from ...common.contracts.message import ShortDataAckBody
from .codec_base import (
    MessageCodecBase,
    Reader,
    address_from_uint,
    address_to_uint,
    invalid_field,
    reserved,
    source_from_uint,
    u32,
    u8,
)


def _encode(body: ShortDataAckBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u8(body.session_id),
        u32(address_to_uint(body.destination)),
        u8(body.retransmission_number),
        body.segment,
    ))


def _decode(reader: Reader) -> ShortDataAckBody:
    source = source_from_uint(reader.uint(4))
    session = reader.uint(1)
    if session != 0:
        raise reserved("wire Session ID는 0이어야 합니다")
    destination = address_from_uint(reader.uint(4))
    if destination.is_broadcast:
        raise invalid_field("Type 92 Destination ID 0은 허용하지 않습니다")
    retransmission = reader.uint(1)
    segment = reader.remaining()
    if not segment:
        raise invalid_field("Short Data segment가 비어 있습니다")
    return ShortDataAckBody(source, session, destination, retransmission, segment)


class Type92Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(92, ShortDataAckBody, _encode, _decode, minimum_length=14)
