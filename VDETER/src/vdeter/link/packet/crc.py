"""VDE-TER Link Packet CRC-32 (M.2092-2 Annex 2/4, V.42 polynomial).

refin/refout=false, init=0xFFFFFFFF, xorout=0이며 각 byte를 MSB-first로 처리한다.
일반적인 reflected/complemented ``zlib.crc32``와 의도적으로 다르다.
"""

from __future__ import annotations

CRC32_WIDTH = 32
CRC32_POLYNOMIAL = 0x04C11DB7
CRC32_INITIAL = 0xFFFFFFFF
CRC32_XOROUT = 0x00000000
CRC32_SIZE_BYTES = 4
CRC32_MASK = 0xFFFFFFFF
CRC32_TOP_BIT = 0x80000000


def crc32(data: bytes, *, initial: int = CRC32_INITIAL) -> int:
    """승인된 direct/MSB-first parameter로 CRC register 값을 계산한다."""

    if not isinstance(data, bytes):
        raise TypeError("CRC input은 bytes여야 합니다")
    if isinstance(initial, bool) or not isinstance(initial, int) or not 0 <= initial <= CRC32_MASK:
        raise ValueError("CRC initial은 uint32여야 합니다")
    register = initial
    for octet in data:
        register ^= octet << 24
        for _ in range(8):
            register = (
                ((register << 1) ^ CRC32_POLYNOMIAL) & CRC32_MASK
                if register & CRC32_TOP_BIT
                else (register << 1) & CRC32_MASK
            )
    return register ^ CRC32_XOROUT


def crc_bytes(data: bytes) -> bytes:
    """Message 영역에 붙일 4-byte big-endian CRC를 반환한다."""

    return crc32(data).to_bytes(CRC32_SIZE_BYTES, "big")


def has_zero_residue(message_region_and_crc: bytes) -> bool:
    """Message 영역과 전송 CRC 전체의 direct residue가 0인지 검사한다."""

    return crc32(message_region_and_crc) == 0
