"""13개 Message codec이 공유하는 framing과 구조화 오류 경계."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...common.addressing import Address, Addressed, Broadcast
from ...common.contracts.message import MessageBody
from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.exceptions import VdeterValidationError
from ...common.identifiers import StationId
from ...common.results import InternalResult, Ok, internal_error


@dataclass(frozen=True, slots=True)
class MessageWireError(Exception):
    code: str
    message: str
    category: ErrorCategory = ErrorCategory.PROTOCOL


def unsupported(message: str) -> MessageWireError:
    return MessageWireError(ErrorCode.LINK_UNSUPPORTED_MESSAGE_PROFILE, message, ErrorCategory.UNSUPPORTED)


def invalid_field(message: str) -> MessageWireError:
    return MessageWireError(ErrorCode.LINK_INVALID_MESSAGE_FIELD, message)


def reserved(message: str) -> MessageWireError:
    return MessageWireError(ErrorCode.LINK_RESERVED_FIELD_NONZERO, message)


def address_to_uint(address: Address) -> int:
    return 0 if address.is_broadcast else address.station_id.value  # type: ignore[union-attr]


def address_from_uint(value: int) -> Address:
    return Broadcast() if value == 0 else Addressed(StationId(value))


def source_from_uint(value: int) -> StationId:
    if value == 0:
        raise invalid_field("Source ID 0은 허용하지 않습니다")
    return StationId(value)


def u8(value: int) -> bytes:
    return value.to_bytes(1, "big")


def u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


class Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise MessageWireError(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "Message field가 잘렸습니다")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def remaining(self) -> bytes:
        return self.take(len(self.data) - self.offset)


class MessageCodecBase:
    """Type/Length framing을 공유하되 body field 처리는 Type module이 주입한다."""

    __slots__ = (
        "_message_type",
        "_dto_type",
        "_fixed_length",
        "_minimum_length",
        "_maximum_length",
        "_encode_payload",
        "_decode_payload",
    )

    def __init__(
        self,
        message_type: int,
        dto_type: type,
        encode_payload: Callable[[object], bytes],
        decode_payload: Callable[[Reader], object],
        *,
        fixed_length: int | None = None,
        minimum_length: int = 3,
        maximum_length: int = 65535,
    ) -> None:
        self._message_type = message_type
        self._dto_type = dto_type
        self._fixed_length = fixed_length
        self._minimum_length = minimum_length
        self._maximum_length = maximum_length
        self._encode_payload = encode_payload
        self._decode_payload = decode_payload

    def message_type(self) -> int:
        return self._message_type

    def dto_type(self) -> type:
        return self._dto_type

    def validate(self, message: MessageBody) -> InternalResult[None]:
        if type(message) is not self._dto_type:
            return internal_error(
                Layer.LINK,
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                f"Type {self._message_type}에는 {self._dto_type.__name__}가 필요합니다",
                details=Details.of(message_type=self._message_type),
            )
        return Ok(None)

    def encode(self, message: MessageBody) -> InternalResult[bytes]:
        checked = self.validate(message)
        if checked.is_err:
            return checked
        try:
            payload = self._encode_payload(message)
            length = 3 + len(payload)
            if self._fixed_length is not None and length != self._fixed_length:
                raise MessageWireError(
                    ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
                    f"Type {self._message_type} 길이가 {self._fixed_length}가 아닙니다",
                )
            if not self._minimum_length <= length <= self._maximum_length:
                raise MessageWireError(
                    ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
                    f"Type {self._message_type} 길이 범위를 벗어났습니다",
                )
            return Ok(u8(self._message_type) + u16(length) + payload)
        except MessageWireError as error:
            return self._error(error)
        except VdeterValidationError as error:
            return self._error(invalid_field(str(error)))

    def decode(self, data: bytes) -> InternalResult[MessageBody]:
        if not isinstance(data, bytes):
            return self._error(invalid_field("Message slice는 bytes여야 합니다"))
        if len(data) < 3:
            return self._error(
                MessageWireError(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "Message prefix가 잘렸습니다")
            )
        if data[0] != self._message_type:
            return self._error(invalid_field(f"codec Type {self._message_type}과 wire Type이 다릅니다"))
        declared = int.from_bytes(data[1:3], "big")
        if declared != len(data):
            return self._error(
                MessageWireError(
                    ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
                    "Length가 전달된 Message slice 전체 길이와 다릅니다",
                )
            )
        if self._fixed_length is not None and declared != self._fixed_length:
            code = (
                ErrorCode.LINK_UNSUPPORTED_MESSAGE_PROFILE
                if self._message_type == 4
                else ErrorCode.LINK_MESSAGE_LENGTH_INVALID
            )
            category = ErrorCategory.UNSUPPORTED if self._message_type == 4 else ErrorCategory.PROTOCOL
            return self._error(MessageWireError(code, "지원하지 않는 Message 길이입니다", category))
        if not self._minimum_length <= declared <= self._maximum_length:
            return self._error(
                MessageWireError(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "Message 길이 범위를 벗어났습니다")
            )
        try:
            reader = Reader(data[3:])
            body = self._decode_payload(reader)
            if reader.offset != len(reader.data):
                raise MessageWireError(
                    ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "Message trailing bytes를 소비하지 않았습니다"
                )
            if type(body) is not self._dto_type:
                raise invalid_field("codec이 다른 DTO type을 반환했습니다")
            return Ok(body)
        except MessageWireError as error:
            return self._error(error)
        except VdeterValidationError as error:
            return self._error(invalid_field(str(error)))

    def _error(self, error: MessageWireError):
        return internal_error(
            Layer.LINK,
            error.code,
            error.category,
            error.message,
            details=Details.of(message_type=self._message_type),
        )
