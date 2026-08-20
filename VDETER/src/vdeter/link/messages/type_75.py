"""Type 75 Data continuation fragment codec (Table 50)."""

from ...common.contracts.message import DataContinuationFragmentBody
from .codec_base import MessageCodecBase, Reader, u8
from .data_common import decode_common, encode_common, payload


def _encode(body: DataContinuationFragmentBody) -> bytes:
    return encode_common(body) + u8(body.fragment_position_in_frame) + u8(body.global_fragment_number) + body.fragment_payload


def _decode(reader: Reader) -> DataContinuationFragmentBody:
    source, session, destination, count = decode_common(reader)
    position = reader.uint(1)
    number = reader.uint(1)
    return DataContinuationFragmentBody(source, session, destination, count, position, number, payload(reader))


class Type75Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(75, DataContinuationFragmentBody, _encode, _decode, minimum_length=15)
