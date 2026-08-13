"""
VDE Packet Builder

완성된 Message bytes 1..N을 순서대로 이어 붙인다.

    [Message 1 | Message 2 | ... | Message N | (향후 Padding) | (향후 CRC-32)]

Message가 하나면 단일 메시지 Packet, 둘 이상이면 다중 메시지 Packet이다.
둘의 형식이 같으므로 Builder가 개수를 따로 구분하지 않는다.
"""

from __future__ import annotations


# Message bytes 목록을 하나의 VDE Packet bytes로 만든다.
def build(messages: list[bytes]) -> bytes:
    if not messages:
        raise ValueError("VDE Packet에는 Message가 최소 하나 필요합니다")

    packet = bytearray()
    for index, message in enumerate(messages, start=1):
        if not message:
            raise ValueError(f"Message {index}가 비어 있습니다")
        packet += bytes(message)

    # 후속 단계 확장 지점: Packet 크기 제한, Padding Message, CRC-32 부착.
    # 2단계에서는 어느 것도 적용하지 않는다.
    return bytes(packet)
