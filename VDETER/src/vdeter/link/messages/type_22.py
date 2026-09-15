"""Type 22 TBB end fragment codec (Table 46)."""

from ...common.contracts.message import TbbEndFragmentBody
from .codec_base import MessageCodecBase, Reader, source_from_uint, u16, u32, u8
from .tbb_common import fragment_payload


def _encode(body: TbbEndFragmentBody) -> bytes:
    return b"".join((u32(body.source_id.value), u8(body.control_station_id), u16(body.bulletin_board_version), u8(body.fragment_number), body.fragment_payload))


def _decode(reader: Reader) -> TbbEndFragmentBody:
    source = source_from_uint(reader.uint(4))
    control = reader.uint(1)
    version = reader.uint(2)
    number = reader.uint(1)
    return TbbEndFragmentBody(source, control, version, number, fragment_payload(reader, 39))


class Type22Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(22, TbbEndFragmentBody, _encode, _decode, minimum_length=12, maximum_length=50)
