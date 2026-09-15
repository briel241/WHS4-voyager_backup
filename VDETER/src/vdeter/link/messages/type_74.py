"""Type 74 Data start fragment codec (Table 49)."""

from ...common.contracts.message import DataStartFragmentBody
from .codec_base import MessageCodecBase, Reader, u8
from .data_common import decode_common, encode_common, payload


def _encode(body: DataStartFragmentBody) -> bytes:
    return encode_common(body) + u8(body.global_fragment_number) + u8(body.continue_resource_usage) + body.fragment_payload


def _decode(reader: Reader) -> DataStartFragmentBody:
    source, session, destination, count = decode_common(reader)
    number = reader.uint(1)
    continuation = reader.uint(1)
    return DataStartFragmentBody(source, session, destination, count, number, continuation, payload(reader))


class Type74Codec(MessageCodecBase):
    def __init__(self) -> None:
        # 15-byte fixed header도 decode 함수까지 전달해 전용 empty-payload 오류로 구분한다.
        super().__init__(74, DataStartFragmentBody, _encode, _decode, minimum_length=15)
