"""Type 90 Resource request/transmission announcement codec (Table 39)."""

from ...common.contracts.message import ResourceRequestBody
from .codec_base import MessageCodecBase, Reader, address_from_uint, address_to_uint, invalid_field, reserved, source_from_uint, u32, u8


def _encode(body: ResourceRequestBody) -> bytes:
    return b"".join((
        u32(body.original_source_id.value),
        u32(body.node_source_id.value),
        u32(address_to_uint(body.node_destination)),
        u32(address_to_uint(body.original_destination)),
        u8(body.priority),
        u32(body.terminal_capabilities),
    ))


def _decode(reader: Reader) -> ResourceRequestBody:
    original_source = source_from_uint(reader.uint(4))
    node_source = source_from_uint(reader.uint(4))
    node_destination = address_from_uint(reader.uint(4))
    original_destination = address_from_uint(reader.uint(4))
    priority = reader.uint(1)
    if priority != 0:
        raise reserved("Type 90 priority는 0이어야 합니다")
    capabilities = reader.uint(4)
    if capabilities & ~0x1F:
        raise reserved("Terminal capabilities bits 5..31은 0이어야 합니다")
    if capabilities & 0x0F not in (0, 1, 2):
        raise invalid_field("M.2092 version은 0, 1, 2 중 하나여야 합니다")
    return ResourceRequestBody(
        original_source,
        node_source,
        node_destination,
        original_destination,
        priority,
        capabilities,
    )


class Type90Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(90, ResourceRequestBody, _encode, _decode, fixed_length=24)
