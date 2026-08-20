"""Type 20/21/22 TBB fragment 공통 wire validation."""

from ...common.errors import ErrorCode
from .codec_base import MessageWireError, Reader


def fragment_payload(reader: Reader, maximum: int) -> bytes:
    value = reader.remaining()
    if not value:
        raise MessageWireError(
            ErrorCode.LINK_TBB_FRAGMENT_PAYLOAD_EMPTY,
            "TBB fragment payload가 비어 있습니다",
        )
    if len(value) > maximum:
        raise MessageWireError(
            ErrorCode.LINK_TBB_FRAGMENT_SIZE_INVALID,
            f"TBB fragment payload가 {maximum} bytes를 넘습니다",
        )
    return value
