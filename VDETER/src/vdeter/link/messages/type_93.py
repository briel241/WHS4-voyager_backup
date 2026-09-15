"""Type 93 Short data without ACK codec (Table 41)."""

from ...common.contracts.message import ShortDataNoAckBody
from .codec_base import MessageCodecBase, Reader, address_from_uint, address_to_uint, invalid_field, reserved, source_from_uint, u32, u8


def _encode(body: ShortDataNoAckBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u8(body.session_id),
        u32(address_to_uint(body.destination)),
        body.segment,
    ))


def _decode(reader: Reader) -> ShortDataNoAckBody:
    source = source_from_uint(reader.uint(4))
    session = reader.uint(1)
    if session != 0:
        raise reserved("wire Session ID는 0이어야 합니다")
    destination = address_from_uint(reader.uint(4))
    segment = reader.remaining()
    if not segment:
        raise invalid_field("Short Data segment가 비어 있습니다")
    return ShortDataNoAckBody(source, session, destination, segment)


class Type93Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(93, ShortDataNoAckBody, _encode, _decode, minimum_length=13)
