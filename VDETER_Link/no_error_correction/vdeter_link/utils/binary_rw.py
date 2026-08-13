"""
공통 이진 데이터 변환 기능

표현 형식
    uint: 0 이상의 정수.

    bytes: 여러 byte로 구성된 값은 big-endian 순서로 저장한다.

    bits:
        각 원소가 0 또는 1인 unpacked bit 목록이다.
        한 byte 내부의 bit는 MSB-first 순서로 배치한다.

주요 변환
    int <-> bytes
    int <-> bits
    bytes <-> bits
"""

from __future__ import annotations

from typing import Sequence

BITS_PER_BYTE = 8


# 정수를 지정한 크기의 MSB-first bytes로 기록한다.
def write_uint_bytes(value: int, byte_size: int, field: str = "value") -> bytes:
    if byte_size <= 0:
        raise ValueError(f"{field}: byte 크기는 1 이상이어야 합니다 ({byte_size})")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field}: 정수가 아닙니다 ({value!r})")
    limit = 1 << (byte_size * BITS_PER_BYTE)
    if not 0 <= value < limit:
        raise ValueError(
            f"{field}: {byte_size} byte 표현 범위(0..{limit - 1})를 벗어났습니다 ({value})"
        )
    return value.to_bytes(byte_size, "big")


# bytes의 지정 위치에서 MSB-first 정수를 읽는다.
def read_uint_bytes(data: bytes, offset: int, byte_size: int, field: str = "value") -> int:
    if byte_size <= 0:
        raise ValueError(f"{field}: byte 크기는 1 이상이어야 합니다 ({byte_size})")
    end = offset + byte_size
    if offset < 0 or end > len(data):
        raise ValueError(
            f"{field}: offset {offset}에서 {byte_size} byte를 읽을 수 없습니다 "
            f"(남은 byte {max(len(data) - offset, 0)})"
        )
    return int.from_bytes(data[offset:end], "big")


# 정수를 MSB-first 0/1 bit 목록으로 기록한다.
def write_uint(value: int, bit_size: int) -> list[int]:
    if bit_size <= 0:
        raise ValueError(f"bit 크기는 1 이상이어야 합니다 ({bit_size})")
    limit = 1 << bit_size
    if not 0 <= value < limit:
        raise ValueError(f"{bit_size} bit 표현 범위(0..{limit - 1})를 벗어났습니다 ({value})")
    return [(value >> (bit_size - 1 - index)) & 1 for index in range(bit_size)]


# bit 목록의 지정 위치에서 MSB-first 정수를 읽는다.
def read_uint(bits: Sequence[int], bit_offset: int, bit_size: int) -> int:
    if bit_size <= 0:
        raise ValueError(f"bit 크기는 1 이상이어야 합니다 ({bit_size})")
    end = bit_offset + bit_size
    if bit_offset < 0 or end > len(bits):
        raise ValueError(
            f"bit offset {bit_offset}에서 {bit_size} bit를 읽을 수 없습니다 "
            f"(남은 bit {max(len(bits) - bit_offset, 0)})"
        )
    value = 0
    for bit in bits[bit_offset:end]:
        value = (value << 1) | (int(bit) & 1)
    return value


# bytes를 MSB-first unpacked bit 목록으로 바꾼다.
def bytes_to_bits(data: bytes) -> list[int]:
    bits: list[int] = []
    for byte in data:
        bits.extend(write_uint(byte, BITS_PER_BYTE))
    return bits


# MSB-first unpacked bit 목록을 bytes로 묶는다.
def bits_to_bytes(bits: Sequence[int]) -> bytes:
    if len(bits) % BITS_PER_BYTE:
        raise ValueError(f"bit 수가 8의 배수가 아닙니다 ({len(bits)})")
    packed = bytearray()
    for offset in range(0, len(bits), BITS_PER_BYTE):
        packed.append(read_uint(bits, offset, BITS_PER_BYTE))
    return bytes(packed)
