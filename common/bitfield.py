"""
비트 단위 읽기·쓰기

MASS/ROC 응용 메시지는 byte 경계에 맞지 않는 필드를 쓴다.
예를 들어 위도는 27 bit, 경도는 28 bit다(AIS와 동일한 부호화).
따라서 byte 단위인 `vdeter_link/utils/binary_rw.py`와 달리
bit 단위 누적 writer/reader가 필요하다.

표현 형식
    MSB-first: 한 byte 안의 bit는 상위 비트부터 채운다.
    uint: 0 이상의 정수.
    int:  2의 보수(two's complement) 부호付 정수.
          AIS의 위도·경도·선회율이 이 방식을 쓴다.
"""

from __future__ import annotations

from typing import Sequence

BITS_PER_BYTE = 8


# 필드를 MSB-first bit로 이어 붙인다.
class BitWriter:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def __len__(self) -> int:
        return len(self._bits)

    # 부호 없는 정수를 지정한 bit 수로 기록한다.
    def write_uint(self, value: int, bit_size: int, field: str = "value") -> None:
        if bit_size <= 0:
            raise ValueError(f"{field}: bit 크기는 1 이상이어야 합니다 ({bit_size})")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field}: 정수가 아닙니다 ({value!r})")
        limit = 1 << bit_size
        if not 0 <= value < limit:
            raise ValueError(
                f"{field}: {bit_size} bit 표현 범위(0..{limit - 1})를 벗어났습니다 ({value})"
            )
        self._bits.extend(
            (value >> (bit_size - 1 - index)) & 1 for index in range(bit_size)
        )

    # 2의 보수 부호付 정수를 지정한 bit 수로 기록한다.
    def write_int(self, value: int, bit_size: int, field: str = "value") -> None:
        if bit_size <= 1:
            raise ValueError(f"{field}: 부호付 bit 크기는 2 이상이어야 합니다 ({bit_size})")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field}: 정수가 아닙니다 ({value!r})")
        limit = 1 << (bit_size - 1)
        if not -limit <= value < limit:
            raise ValueError(
                f"{field}: {bit_size} bit 부호付 범위({-limit}..{limit - 1})를 "
                f"벗어났습니다 ({value})"
            )
        self.write_uint(value + (1 << bit_size) if value < 0 else value, bit_size, field)

    # byte 경계까지 0으로 채운다. 남는 bit가 없으면 아무것도 하지 않는다.
    def pad_to_byte(self) -> None:
        remainder = len(self._bits) % BITS_PER_BYTE
        if remainder:
            self._bits.extend([0] * (BITS_PER_BYTE - remainder))

    # 기록한 bit를 bytes로 묶는다. byte 경계에 맞지 않으면 오류다.
    def to_bytes(self) -> bytes:
        if len(self._bits) % BITS_PER_BYTE:
            raise ValueError(
                f"bit 수가 8의 배수가 아닙니다 ({len(self._bits)}). "
                "pad_to_byte()를 먼저 호출하십시오"
            )
        packed = bytearray()
        for offset in range(0, len(self._bits), BITS_PER_BYTE):
            byte = 0
            for bit in self._bits[offset : offset + BITS_PER_BYTE]:
                byte = (byte << 1) | bit
            packed.append(byte)
        return bytes(packed)


# bytes를 MSB-first bit 순서로 읽어 나간다.
class BitReader:
    def __init__(self, data: bytes | Sequence[int]) -> None:
        self._bits: list[int] = []
        for byte in data:
            self._bits.extend((byte >> (BITS_PER_BYTE - 1 - i)) & 1 for i in range(BITS_PER_BYTE))
        self._offset = 0

    @property
    def remaining(self) -> int:
        return len(self._bits) - self._offset

    # 부호 없는 정수를 지정한 bit 수만큼 읽는다.
    def read_uint(self, bit_size: int, field: str = "value") -> int:
        if bit_size <= 0:
            raise ValueError(f"{field}: bit 크기는 1 이상이어야 합니다 ({bit_size})")
        if self.remaining < bit_size:
            raise ValueError(
                f"{field}: {bit_size} bit를 읽을 수 없습니다 (남은 bit {self.remaining})"
            )
        value = 0
        for bit in self._bits[self._offset : self._offset + bit_size]:
            value = (value << 1) | bit
        self._offset += bit_size
        return value

    # 2의 보수 부호付 정수를 지정한 bit 수만큼 읽는다.
    def read_int(self, bit_size: int, field: str = "value") -> int:
        if bit_size <= 1:
            raise ValueError(f"{field}: 부호付 bit 크기는 2 이상이어야 합니다 ({bit_size})")
        raw = self.read_uint(bit_size, field)
        limit = 1 << (bit_size - 1)
        return raw - (1 << bit_size) if raw >= limit else raw

    # 남은 padding bit가 모두 0인지 확인한다.
    def check_padding(self, field: str = "padding") -> None:
        if any(self._bits[self._offset :]):
            raise ValueError(f"{field}: 남은 bit가 0이 아닙니다")
