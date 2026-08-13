"""
VDE Packet Parser

Packet 앞에서부터 Type과 Length를 읽어 Message 경계를 나누고, registry에서 찾은
Type별 decode()로 필드를 복원한다. 
"""

from __future__ import annotations

from ..messages.registry import MESSAGE_CODECS
from ..messages.type_92 import Type92Message
from ..messages.type_93 import Type93Message
from ..utils import binary_rw

VDEMessage = Type92Message | Type93Message # Type union. 새 Type이 생기면 여기만 고치면 된다.

# 모든 Type이 공유하는 앞부분 - Message 경계를 판단하는 데 필요한 최소 크기
TYPE_OFFSET = 0
TYPE_SIZE = 1
LENGTH_OFFSET = TYPE_OFFSET + TYPE_SIZE
LENGTH_SIZE = 2
MESSAGE_PREFIX_SIZE = TYPE_SIZE + LENGTH_SIZE


# VDE Packet bytes에서 Type별 dataclass 목록을 얻는다.
def parse(packet: bytes) -> list[VDEMessage]:
    if not packet:
        raise ValueError("VDE Packet이 비어 있습니다")

    messages: list[VDEMessage] = []
    offset = 0
    total = len(packet)

    while offset < total:
        remaining = total - offset
        if remaining < MESSAGE_PREFIX_SIZE:
            raise ValueError(
                f"Message {len(messages) + 1}: Type/Length를 읽을 byte가 부족합니다 "
                f"(남은 byte {remaining})"
            )

        type_id = binary_rw.read_uint_bytes(packet, offset + TYPE_OFFSET, TYPE_SIZE, "Type")
        codec = MESSAGE_CODECS.get(type_id)
        if codec is None:
            raise ValueError(f"Message {len(messages) + 1}: 등록되지 않은 Type {type_id}")

        length = binary_rw.read_uint_bytes(
            packet, offset + LENGTH_OFFSET, LENGTH_SIZE, "Length"
        )
        if length == 0:
            raise ValueError(f"Message {len(messages) + 1}: Length가 0입니다")
        if length > remaining:
            raise ValueError(
                f"Message {len(messages) + 1}: Length {length}가 Packet 경계를 넘습니다 "
                f"(남은 byte {remaining})"
            )

        message, consumed = codec.decode(packet[offset:])
        if consumed != length:
            raise ValueError(
                f"Message {len(messages) + 1}: 소비 byte {consumed}가 Length {length}와 다릅니다"
            )

        messages.append(message)
        offset += consumed

    # 후속 단계 확장 지점: Padding Message 식별, CRC-32 검증.
    if offset != total:
        raise ValueError(f"Packet의 모든 byte를 소비하지 못했습니다 ({offset}/{total})")

    return messages
