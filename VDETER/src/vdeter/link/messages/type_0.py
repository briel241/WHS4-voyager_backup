"""Type 0 Media access control codec (M.2092-2 Table 36)."""

from ...common.contracts.message import MediaAccessControlBody
from .codec_base import MessageCodecBase, Reader, reserved, source_from_uint, u16, u32, u8


def _encode(body: MediaAccessControlBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u8(body.media_access_priority_level),
        u16(body.random_access_selection_interval),
        u8(body.short_data_message_limit),
        u8(body.system_status),
    ))


def _decode(reader: Reader) -> MediaAccessControlBody:
    source = source_from_uint(reader.uint(4))
    priority = reader.uint(1)
    if priority != 0:
        raise reserved("Media Access priority level은 0이어야 합니다")
    return MediaAccessControlBody(
        source_id=source,
        media_access_priority_level=priority,
        random_access_selection_interval=reader.uint(2),
        short_data_message_limit=reader.uint(1),
        system_status=reader.uint(1),
    )


class Type0Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(0, MediaAccessControlBody, _encode, _decode, fixed_length=12)
