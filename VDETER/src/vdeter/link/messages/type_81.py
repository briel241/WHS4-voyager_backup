"""Type 81 one-byte padding codec (§A4-4.9.13).

Packet builder/parser만 사용하며 public Link encode 경로에는 노출하지 않는다.
"""

from ...common.contracts.message import MessageBody, PaddingBody
from ...common.errors import ErrorCategory, ErrorCode, Layer
from ...common.results import InternalResult, Ok, internal_error


class Type81Codec:
    __slots__ = ()

    def message_type(self) -> int:
        return 81

    def dto_type(self) -> type:
        return PaddingBody

    def validate(self, message: MessageBody) -> InternalResult[None]:
        if type(message) is PaddingBody:
            return Ok(None)
        return internal_error(
            Layer.LINK,
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Type 81에는 PaddingBody가 필요합니다",
        )

    def encode(self, message: MessageBody) -> InternalResult[bytes]:
        checked = self.validate(message)
        return Ok(b"\x51") if checked.is_ok else checked

    def decode(self, data: bytes) -> InternalResult[MessageBody]:
        if data != b"\x51":
            return internal_error(
                Layer.LINK,
                ErrorCode.LINK_PADDING_INVALID,
                ErrorCategory.PROTOCOL,
                "Type 81은 정확히 한 byte 0x51이어야 합니다",
            )
        return Ok(PaddingBody())
