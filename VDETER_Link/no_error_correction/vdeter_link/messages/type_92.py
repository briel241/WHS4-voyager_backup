"""
Type 92 Message - Retransmission ID가 있는 Short Data Message

[Type | Length | Source ID | Session ID | Destination ID | Retransmission ID | Payload]
   1        2          4           1            4                  1             가변
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils import binary_rw

TYPE_ID = 92

TYPE_SIZE = 1
LENGTH_SIZE = 2
SOURCE_ID_SIZE = 4
SESSION_ID_SIZE = 1
DESTINATION_ID_SIZE = 4
RETRANSMISSION_ID_SIZE = 1

# Payload를 제외한 고정 header 크기
HEADER_SIZE = (
    TYPE_SIZE
    + LENGTH_SIZE
    + SOURCE_ID_SIZE
    + SESSION_ID_SIZE
    + DESTINATION_ID_SIZE
    + RETRANSMISSION_ID_SIZE
)

_TYPE_OFFSET = 0
_LENGTH_OFFSET = _TYPE_OFFSET + TYPE_SIZE
_SOURCE_ID_OFFSET = _LENGTH_OFFSET + LENGTH_SIZE
_SESSION_ID_OFFSET = _SOURCE_ID_OFFSET + SOURCE_ID_SIZE
_DESTINATION_ID_OFFSET = _SESSION_ID_OFFSET + SESSION_ID_SIZE
_RETRANSMISSION_ID_OFFSET = _DESTINATION_ID_OFFSET + DESTINATION_ID_SIZE


# Type과 Length는 입력받지 않으며 Length는 payload 길이로 계산한다.
@dataclass
class Type92Message:
    source_id: int
    session_id: int
    destination_id: int
    retransmission_id: int
    payload: bytes
    type_id: int = field(init=False, default=TYPE_ID)

    @property
    def length(self) -> int:
        return HEADER_SIZE + len(self.payload)


# Type 92 Message bytes를 만든다.
def encode(message: Type92Message) -> bytes:
    payload = bytes(message.payload)
    length = HEADER_SIZE + len(payload)

    encoded = bytearray()
    encoded += binary_rw.write_uint_bytes(TYPE_ID, TYPE_SIZE, "Type")
    encoded += binary_rw.write_uint_bytes(length, LENGTH_SIZE, "Length")
    encoded += binary_rw.write_uint_bytes(message.source_id, SOURCE_ID_SIZE, "Source ID")
    encoded += binary_rw.write_uint_bytes(message.session_id, SESSION_ID_SIZE, "Session ID")
    encoded += binary_rw.write_uint_bytes(
        message.destination_id, DESTINATION_ID_SIZE, "Destination ID"
    )
    encoded += binary_rw.write_uint_bytes(
        message.retransmission_id, RETRANSMISSION_ID_SIZE, "Retransmission ID"
    )
    encoded += payload
    return bytes(encoded)


# Message bytes에서 Type92Message와 소비한 byte 수를 얻는다.
def decode(data: bytes) -> tuple[Type92Message, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError(
            f"Type 92: header {HEADER_SIZE} byte에 못 미칩니다 (남은 byte {len(data)})"
        )

    type_id = binary_rw.read_uint_bytes(data, _TYPE_OFFSET, TYPE_SIZE, "Type")
    if type_id != TYPE_ID:
        raise ValueError(f"Type 92 codec에 Type {type_id} Message가 들어왔습니다")

    length = binary_rw.read_uint_bytes(data, _LENGTH_OFFSET, LENGTH_SIZE, "Length")
    if length < HEADER_SIZE:
        raise ValueError(f"Type 92: Length {length}가 header 크기 {HEADER_SIZE}보다 작습니다")
    if length > len(data):
        raise ValueError(f"Type 92: Length {length} byte가 없습니다 (남은 byte {len(data)})")

    message = Type92Message(
        source_id=binary_rw.read_uint_bytes(
            data, _SOURCE_ID_OFFSET, SOURCE_ID_SIZE, "Source ID"
        ),
        session_id=binary_rw.read_uint_bytes(
            data, _SESSION_ID_OFFSET, SESSION_ID_SIZE, "Session ID"
        ),
        destination_id=binary_rw.read_uint_bytes(
            data, _DESTINATION_ID_OFFSET, DESTINATION_ID_SIZE, "Destination ID"
        ),
        retransmission_id=binary_rw.read_uint_bytes(
            data, _RETRANSMISSION_ID_OFFSET, RETRANSMISSION_ID_SIZE, "Retransmission ID"
        ),
        payload=bytes(data[HEADER_SIZE:length]),
    )
    return message, length
