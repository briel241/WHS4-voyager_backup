"""
Temporary Burst Extractor (수신 전용)

Correlator가 F9A8을 찾아 붙인 tag 위치부터 Temporary Packet Length 16 bit를 모으고 
그 값의 8배만큼 bit를 더 모아 MSB-first로 byte packing하여 VDE Packet을 돌려준다.

수신 bit는 GNU Radio work chunk 단위로 잘려 들어오므로 상태와 buffer를 들고
여러 chunk에 걸쳐 이어 모은다. 수집 도중 새 tag가 오면 그 자리에서 다시 시작한다
(새 preamble을 봤다는 뜻이므로 앞의 미완성 수집은 버린다).

Burst 생성은 temporary_builder.py가 맡는다. 여기서는 하지 않는다.
"""

from __future__ import annotations

from typing import Sequence

from ..utils import binary_rw
from .temporary_builder import PACKET_LENGTH_BITS

_STATE_IDLE = "idle"
_STATE_LENGTH = "length"
_STATE_PACKET = "packet"


# tag 이후의 bit stream에서 VDE Packet을 꺼낸다.
class Extractor:
    def __init__(self) -> None:
        self.reset()

    # 수집 상태를 비우고 다음 F9A8 tag를 기다린다.
    def reset(self) -> None:
        self._state = _STATE_IDLE
        self._bits: list[int] = []
        self._needed = 0

    # bit chunk 하나를 처리하고 이번에 완성된 VDE Packet들을 반환한다.
    # bits는 GNU Radio uint8 stream의 unpacked 0/1 값이다.
    # tags는 chunk 시작을 0으로 본 tag 위치이며, 해당 위치가 Length의 첫 bit다.
    def consume(self, bits: Sequence[int], tags: Sequence[int]) -> list[bytes]:
        packets: list[bytes] = []
        total = len(bits)
        pending = sorted({int(tag) for tag in tags if 0 <= int(tag) < total})
        cursor = 0

        while cursor < total:
            if pending and pending[0] == cursor:
                pending.pop(0)
                self._start_length()

            # 다음 tag 전까지만 모은다. tag 자리에서는 위에서 다시 시작한다.
            next_tag = pending[0] if pending else total

            if self._state == _STATE_IDLE:
                cursor = next_tag
                continue

            take = min(self._needed - len(self._bits), next_tag - cursor)
            self._bits.extend(int(bit) & 1 for bit in bits[cursor:cursor + take])
            cursor += take

            if len(self._bits) < self._needed:
                continue

            packet = self._complete()
            if packet is not None:
                packets.append(packet)

        return packets

    def _start_length(self) -> None:
        self._state = _STATE_LENGTH
        self._bits = []
        self._needed = PACKET_LENGTH_BITS

    # 필요한 bit를 모두 모았을 때 처리하고, Packet이 완성되면 반환한다.
    def _complete(self) -> bytes | None:
        if self._state == _STATE_LENGTH:
            packet_size = binary_rw.read_uint(self._bits, 0, PACKET_LENGTH_BITS)
            if packet_size == 0:
                # 잡음에서 나온 검출로 본다. 다음 F9A8을 기다린다.
                self.reset()
                return None
            self._state = _STATE_PACKET
            self._bits = []
            self._needed = packet_size * binary_rw.BITS_PER_BYTE
            return None

        packet = binary_rw.bits_to_bytes(self._bits)
        self.reset()
        return packet
