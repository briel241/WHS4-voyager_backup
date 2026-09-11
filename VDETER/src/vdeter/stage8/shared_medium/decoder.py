"""RF-off 참조 frozen decoder (`WIRED_SHARED_MEDIUM_EMULATION`, 명세 §7.3, §11.1).

Stage 5는 실장비 PHY/GNU Radio(numpy 의존)를 실행하지 않는 순수 software track(WSM-G3)이다.
이 module은 그 track에서 composite IQ의 decode 성공·실패를 결정하는 **결정적 참조 decoder**다.
실신호용 frozen PHY는 `vdeter.phy.transmitter.Link11Phy`이며, 그 실신호 dual-channel decode
검증(WSM-G4)과 qualification(WSM-G5)은 Stage 6 이후로 이관된다(로드맵 §6.1).

decoder 판정 규칙은 표준 QPSK hard-decision + CRC이며 fixture에 맞춰 조정하지 않는다.
collision 결과는 이 decoder가 composite IQ(선형 합산 + virtual clipping)에 대해 자연스럽게
결정한다. 명세에 없는 정규화·보정 상수를 추가하지 않는다.

waveform provenance (byte-pinned fixture 생성 근거):
- modulation: QPSK, symbol당 1 complex sample(SAMPLES_PER_SYMBOL=1).
- symbol amplitude A = 0.6(축당). 단일 burst 축 peak 0.6 < full-scale 1.0(clip 없음);
  동일 두 burst 정렬 합산 시 축 1.2 > 1.0 → virtual frontend clipping이 자연 발생.
- bit→symbol: byte는 MSB-first 2bit씩; 첫 bit=I 선택, 둘째 bit=Q 선택.
  I = +A(bit 0) / -A(bit 1), Q = +A(bit 0) / -A(bit 1).
- hard-decision: I real>=0 → bit 0, else 1; Q imag>=0 → bit 0, else 1. 판정 경계 0(표준).
- frame: PREAMBLE(4 byte 동기어) + LENGTH(2 byte BE) + PAYLOAD + CRC32(4 byte BE, payload 기준).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from enum import Enum

from ..poc_api.types import PhysicalChannel
from .constants import MAX_BURST_SAMPLES
from .iq import IqBurst

#: 참조 decoder 버전. evidence·golden vector에 기록한다.
REFERENCE_DECODER_VERSION = "WSM-REF-DECODER-1"

#: QPSK symbol 축 amplitude(waveform provenance 고정값).
SYMBOL_AMPLITUDE = 0.6
#: symbol당 sample 수.
SAMPLES_PER_SYMBOL = 1
#: byte당 QPSK symbol 수(8 bit / 2 bit).
SYMBOLS_PER_BYTE = 4

#: 고정 동기어(preamble). burst 존재 검출용.
PREAMBLE = b"\xA5\x5A\xA5\x5A"
#: LENGTH field byte 수(payload 길이, big-endian).
LENGTH_FIELD_BYTES = 2
#: CRC field byte 수(CRC32, big-endian).
CRC_FIELD_BYTES = 4
#: payload 없는 최소 frame byte 수.
MIN_FRAME_BYTES = len(PREAMBLE) + LENGTH_FIELD_BYTES + CRC_FIELD_BYTES
#: payload 최대 길이(LENGTH field 표현 범위와 burst 상한 안).
MAX_PAYLOAD_BYTES = min((1 << (8 * LENGTH_FIELD_BYTES)) - 1, MAX_BURST_SAMPLES // SYMBOLS_PER_BYTE)


class DecodeStatus(str, Enum):
    """참조 decoder 결과. RECOVERED만 성공이다."""

    RECOVERED = "RECOVERED"
    NO_BURST_DETECTED = "NO_BURST_DETECTED"
    PREAMBLE_MISMATCH = "PREAMBLE_MISMATCH"
    CRC_MISMATCH = "CRC_MISMATCH"
    TRUNCATED = "TRUNCATED"


@dataclass(frozen=True, slots=True)
class DecodeResult:
    status: DecodeStatus
    payload: bytes | None = None

    @property
    def recovered(self) -> bool:
        return self.status is DecodeStatus.RECOVERED


def _bits_of(data: bytes):
    for byte in data:
        for shift in range(7, -1, -1):
            yield (byte >> shift) & 1


def _symbol_for_bits(bit_i: int, bit_q: int) -> complex:
    real = SYMBOL_AMPLITUDE if bit_i == 0 else -SYMBOL_AMPLITUDE
    imag = SYMBOL_AMPLITUDE if bit_q == 0 else -SYMBOL_AMPLITUDE
    return complex(real, imag)


def modulate(frame: bytes) -> tuple:
    """frame bytes → QPSK complex sample tuple(byte-pinned waveform)."""

    bits = list(_bits_of(frame))
    samples = []
    for i in range(0, len(bits), 2):
        samples.append(_symbol_for_bits(bits[i], bits[i + 1]))
    return tuple(samples)


def build_frame(payload: bytes) -> bytes:
    """PREAMBLE + LENGTH + PAYLOAD + CRC32 frame."""

    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("payload는 bytes여야 합니다")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload 길이 {len(payload)}가 상한 {MAX_PAYLOAD_BYTES}를 넘습니다")
    crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
    return (
        PREAMBLE
        + len(payload).to_bytes(LENGTH_FIELD_BYTES, "big")
        + bytes(payload)
        + crc.to_bytes(CRC_FIELD_BYTES, "big")
    )


def encode_payload(payload: bytes) -> tuple:
    """payload → QPSK complex sample tuple."""

    return modulate(build_frame(payload))


def build_burst(channel: PhysicalChannel, start_sample: int, payload: bytes) -> IqBurst:
    """payload를 QPSK로 변조해 :class:`IqBurst`로 만든다(fixture·시험용)."""

    return IqBurst(channel=channel, start_sample=start_sample, samples=encode_payload(payload))


def _hard_decision_bits(samples) -> list:
    bits = []
    for sample in samples:
        bits.append(0 if sample.real >= 0.0 else 1)
        bits.append(0 if sample.imag >= 0.0 else 1)
    return bits


def _bits_to_bytes(bits) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        value = 0
        for j in range(8):
            value = (value << 1) | bits[i + j]
        out.append(value)
    return bytes(out)


def decode(samples) -> DecodeResult:
    """composite/단일 IQ를 참조 frozen decoder로 복호한다.

    RECOVERED만 성공이며, preamble 불일치·CRC 실패·truncation은 성공을 합성하지 않는다.
    """

    seq = tuple(samples)
    if len(seq) < MIN_FRAME_BYTES * SYMBOLS_PER_BYTE:
        return DecodeResult(DecodeStatus.NO_BURST_DETECTED)
    if len(seq) % SYMBOLS_PER_BYTE != 0:
        return DecodeResult(DecodeStatus.TRUNCATED)
    frame = _bits_to_bytes(_hard_decision_bits(seq))
    if frame[: len(PREAMBLE)] != PREAMBLE:
        return DecodeResult(DecodeStatus.PREAMBLE_MISMATCH)
    off = len(PREAMBLE)
    length = int.from_bytes(frame[off : off + LENGTH_FIELD_BYTES], "big")
    off += LENGTH_FIELD_BYTES
    payload = frame[off : off + length]
    off += length
    crc_bytes = frame[off : off + CRC_FIELD_BYTES]
    if len(payload) != length or len(crc_bytes) != CRC_FIELD_BYTES:
        return DecodeResult(DecodeStatus.TRUNCATED)
    expected_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if int.from_bytes(crc_bytes, "big") != expected_crc:
        return DecodeResult(DecodeStatus.CRC_MISMATCH)
    return DecodeResult(DecodeStatus.RECOVERED, payload=bytes(payload))


__all__ = (
    "REFERENCE_DECODER_VERSION",
    "SYMBOL_AMPLITUDE",
    "SAMPLES_PER_SYMBOL",
    "SYMBOLS_PER_BYTE",
    "PREAMBLE",
    "LENGTH_FIELD_BYTES",
    "CRC_FIELD_BYTES",
    "MIN_FRAME_BYTES",
    "MAX_PAYLOAD_BYTES",
    "DecodeStatus",
    "DecodeResult",
    "modulate",
    "build_frame",
    "encode_payload",
    "build_burst",
    "decode",
)
