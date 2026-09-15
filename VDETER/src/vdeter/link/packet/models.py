"""Packet builder/parser 내부 immutable 결과."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.contracts.message import DecodedMessage
from ...common.exceptions import InvalidValueError


@dataclass(frozen=True, slots=True)
class PacketBuildOutput:
    packet: bytes
    message_types: tuple[int, ...]
    padding_size: int
    crc: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("packet: bytes여야 합니다")
        if not isinstance(self.message_types, tuple) or not self.message_types or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255
            for item in self.message_types
        ):
            raise InvalidValueError("message_types: 비어 있지 않은 uint8 tuple이어야 합니다")
        if isinstance(self.padding_size, bool) or not isinstance(self.padding_size, int) or self.padding_size < 0:
            raise InvalidValueError("padding_size: 0 이상의 정수여야 합니다")
        if not isinstance(self.crc, bytes) or len(self.crc) != 4 or not self.packet.endswith(self.crc):
            raise InvalidValueError("crc: packet 마지막의 4 bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class PacketParseOutput:
    messages: tuple[DecodedMessage, ...]
    padding_size: int
    crc: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.messages, tuple) or not self.messages or any(
            not isinstance(item, DecodedMessage) for item in self.messages
        ):
            raise InvalidValueError("messages: 비어 있지 않은 DecodedMessage tuple이어야 합니다")
        if isinstance(self.padding_size, bool) or not isinstance(self.padding_size, int) or self.padding_size < 0:
            raise InvalidValueError("padding_size: 0 이상의 정수여야 합니다")
        if not isinstance(self.crc, bytes) or len(self.crc) != 4:
            raise InvalidValueError("crc: 4 bytes여야 합니다")
