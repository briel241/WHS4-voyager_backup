"""Type 13 ACK/NACK codec (M.2092-2 Table 38)."""

from ...common.contracts.message import AcknowledgeNackBody
from .codec_base import (
    MessageCodecBase,
    Reader,
    address_from_uint,
    address_to_uint,
    invalid_field,
    reserved,
    source_from_uint,
    u16,
    u32,
    u8,
)


def _encode(body: AcknowledgeNackBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u32(address_to_uint(body.destination)),
        u8(body.session_id),
        *(u16(mask) for mask in body.ack_nack_masks),
        u8(body.cqi),
        u8(body.acm_or_edn),
        u8(body.power_setting),
    ))


def _decode(reader: Reader) -> AcknowledgeNackBody:
    source = source_from_uint(reader.uint(4))
    destination = address_from_uint(reader.uint(4))
    if destination.is_broadcast:
        raise invalid_field("Type 13 Destination ID 0은 허용하지 않습니다")
    session = reader.uint(1)
    if session != 0:
        raise reserved("wire Session ID는 0이어야 합니다")
    masks = (reader.uint(2), reader.uint(2), reader.uint(2))
    cqi = reader.uint(1)
    acm = reader.uint(1)
    power = reader.uint(1)
    if power != 0:
        raise reserved("Power setting은 현재 0만 지원합니다")
    return AcknowledgeNackBody(source, destination, session, masks, cqi, acm, power)


class Type13Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(13, AcknowledgeNackBody, _encode, _decode, fixed_length=21)
