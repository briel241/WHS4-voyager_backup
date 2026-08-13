"""
Temporary Burst Builder (송신 전용)

VDE Packet 앞에 F9A8과 Temporary Packet Length를 붙인다.
Length는 개별 Message가 아니라 뒤따르는 VDE Packet 전체의 byte 수이며,
F9A8과 Length 자신은 계산에서 뺀다.

bytes를 MSB-first bit로 푸는 일은 GNU Radio Constellation Modulator가 한다.
"""

from __future__ import annotations

from ...common import binary_rw

# 2단계 임시 동기 패턴. RX의 Correlate Access Code '1111100110101000'과 같은 값이다.
PREAMBLE = b"\xf9\xa8"

PACKET_LENGTH_SIZE = 2
PACKET_LENGTH_BITS = PACKET_LENGTH_SIZE * binary_rw.BITS_PER_BYTE
MAX_PACKET_SIZE = (1 << PACKET_LENGTH_BITS) - 1


# VDE Packet에 임시 Burst header를 붙인 bytes를 만든다.
def build(packet: bytes) -> bytes:
    if not packet:
        raise ValueError("Temporary Burst에 담을 VDE Packet이 비어 있습니다")
    if len(packet) > MAX_PACKET_SIZE:
        raise ValueError(
            f"VDE Packet {len(packet)} byte가 Temporary Packet Length 표현 범위"
            f"(최대 {MAX_PACKET_SIZE} byte)를 넘습니다"
        )

    length = binary_rw.write_uint_bytes(
        len(packet), PACKET_LENGTH_SIZE, "Temporary Packet Length"
    )
    return PREAMBLE + length + bytes(packet)
