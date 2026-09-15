"""Type 4 Resource allocation codec (M.2092-2 Table 37)."""

from ...common.contracts.message import ResourceAllocationBody
from ...common.errors import ErrorCategory, ErrorCode
from ...common.identifiers import LinkId
from .codec_base import (
    MessageCodecBase,
    MessageWireError,
    Reader,
    address_from_uint,
    address_to_uint,
    reserved,
    source_from_uint,
    u32,
    u8,
)


def _encode(body: ResourceAllocationBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u32(address_to_uint(body.destination)),
        u8(body.logical_channel_tx),
        u8(body.logical_channel_rx),
        u8(body.link_id.value),
        u8(body.tdma_frame_delay),
        u8(body.session_id),
        u8(body.cqi),
    ))


def _decode(reader: Reader) -> ResourceAllocationBody:
    source = source_from_uint(reader.uint(4))
    destination = address_from_uint(reader.uint(4))
    tx = reader.uint(1)
    rx = reader.uint(1)
    link_id = reader.uint(1)
    if link_id not in (11, 17, 19):
        raise MessageWireError(
            ErrorCode.LINK_UNSUPPORTED_LINK_ID,
            "Type 4 Link ID는 운영 지원값 11, 17, 19 중 하나여야 합니다",
            ErrorCategory.UNSUPPORTED,
        )
    delay = reader.uint(1)
    session = reader.uint(1)
    if session != 0:
        raise reserved("wire Session ID는 0이어야 합니다")
    return ResourceAllocationBody(source, destination, tx, rx, LinkId(link_id), delay, session, reader.uint(1))


class Type4Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(4, ResourceAllocationBody, _encode, _decode, fixed_length=17)
