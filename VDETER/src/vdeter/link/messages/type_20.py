"""Type 20 TBB start fragment codec (Table 42)."""

from ...common.contracts.message import TbbStartFragmentBody
from .codec_base import MessageCodecBase, Reader, source_from_uint, u16, u32, u8
from .tbb_common import fragment_payload


def _encode(body: TbbStartFragmentBody) -> bytes:
    return b"".join((
        u32(body.source_id.value),
        u8(body.control_station_id),
        u8(body.service_capability),
        u16(body.bulletin_board_version),
        u8(body.number_of_fragments),
        body.fragment_payload,
    ))


def _decode(reader: Reader) -> TbbStartFragmentBody:
    source = source_from_uint(reader.uint(4))
    control = reader.uint(1)
    capability = reader.uint(1)
    version = reader.uint(2)
    count = reader.uint(1)
    return TbbStartFragmentBody(source, control, capability, version, count, fragment_payload(reader, 38))


class Type20Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(20, TbbStartFragmentBody, _encode, _decode, minimum_length=13, maximum_length=50)
