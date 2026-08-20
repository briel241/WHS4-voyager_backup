"""Type 76 Data end fragment codec (Table 51)."""

from ...common.contracts.message import DataEndFragmentBody
from .codec_base import MessageCodecBase, Reader, u8
from .data_common import decode_common, encode_common, payload


def _encode(body: DataEndFragmentBody) -> bytes:
    return encode_common(body) + u8(body.global_fragment_number) + u8(body.continue_resource_usage) + body.fragment_payload


def _decode(reader: Reader) -> DataEndFragmentBody:
    source, session, destination, count = decode_common(reader)
    number = reader.uint(1)
    continuation = reader.uint(1)
    return DataEndFragmentBody(source, session, destination, count, number, continuation, payload(reader))


class Type76Codec(MessageCodecBase):
    def __init__(self) -> None:
        super().__init__(76, DataEndFragmentBody, _encode, _decode, minimum_length=15)
