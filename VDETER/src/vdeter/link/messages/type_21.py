"""Type 21 TBB continuation fragment codec (Table 45)."""

from ...common.contracts.message import TbbContinuationFragmentBody
from .codec_base import MessageCodecBase, Reader, source_from_uint, u16, u32, u8
from .tbb_common import fragment_payload


def _encode(body: TbbContinuationFragmentBody) -> bytes:
    return b"".join((u32(body.source_id.value), u8(body.control_station_id), u16(body.bulletin_board_version), u8(body.fragment_number), body.fragment_payload))


def _decode(reader: Reader) -> TbbContinuationFragmentBody:
    source = source_from_uint(reader.uint(4))
    control = reader.uint(1)
    version = reader.uint(2)
    number = reader.uint(1)
    return TbbContinuationFragmentBody(source, control, version, number, fragment_payload(reader, 39))


class Type21Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(21, TbbContinuationFragmentBody, _encode, _decode, minimum_length=12, maximum_length=50)
