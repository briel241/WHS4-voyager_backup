"""Approved 41-tap RRC, continuous ramp/guard waveform and test serialization."""

from __future__ import annotations

import math
import struct

RRC_TAP_BINARY64_BITS = (
    0x3F4DCDE66A98578B, 0xBF71779D20890A13, 0xBF766B3AE585E3F6,
    0xBF477C1DE743A561, 0x3F785F045282B2AF, 0x3F7D3453C379CF3C,
    0xBF4D9CEB7B71EE04, 0xBF8A4CE210E2EF2B, 0xBF90F5049BAD21E6,
    0xBF69BDEEE4E0F06C, 0x3F970CF32419485B, 0x3FA4B7AD1A9AF1B1,
    0x3F9CFB1111472F44, 0xBF93832F686AAFC1, 0xBFB3A9D5F1BA7197,
    0xBFB8F3245A6EF009, 0xBFA333D4E60A2A84, 0x3FBC81C997CBFC2B,
    0x3FD3B1732D8240C9, 0x3FDE6A59EA8B2D6F, 0x3FE1503C2AB12187,
    0x3FDE6A59EA8B2D6F, 0x3FD3B1732D8240C9, 0x3FBC81C997CBFC2B,
    0xBFA333D4E60A2A84, 0xBFB8F3245A6EF009, 0xBFB3A9D5F1BA7197,
    0xBF93832F686AAFC1, 0x3F9CFB1111472F44, 0x3FA4B7AD1A9AF1B1,
    0x3F970CF32419485B, 0xBF69BDEEE4E0F06C, 0xBF90F5049BAD21E6,
    0xBF8A4CE210E2EF2B, 0xBF4D9CEB7B71EE04, 0x3F7D3453C379CF3C,
    0x3F785F045282B2AF, 0xBF477C1DE743A561, 0xBF766B3AE585E3F6,
    0xBF71779D20890A13, 0x3F4DCDE66A98578B,
)
RRC_TAPS = tuple(
    struct.unpack("<d", struct.pack("<Q", value))[0]
    for value in RRC_TAP_BINARY64_BITS
)
SAMPLES_PER_SYMBOL = 4
RRC_HALF_SPAN_SAMPLES = 20

# numpy는 PHY·Radio 구현 단계에서 쓰도록 pyproject가 허용한다
# (dsp = ["numpy>=1.24"]). 없으면 참조 구현이 그대로 동작한다.
try:  # pragma: no cover - 환경 의존
    import numpy as _numpy
except ImportError:  # pragma: no cover
    _numpy = None
    _RRC_TAPS_ARRAY = None
else:
    _RRC_TAPS_ARRAY = _numpy.asarray(RRC_TAPS, dtype=_numpy.float64)
RAMP_SAMPLES = 32
BODY_SAMPLES = 1920
GUARD_SAMPLES = 64
SLOT_SAMPLES = 2048
SAMPLE_RATE_HZ = 76800


def _held_symbol(body_symbols: tuple[complex, ...], index: int) -> complex:
    if index < 0:
        return body_symbols[0]
    if index >= len(body_symbols):
        return body_symbols[-1]
    return body_symbols[index]


def pulse_shaped_carrier(body_symbols, first_sample: int = -32, last_sample: int = 1951) -> tuple[complex, ...]:
    symbols = tuple(complex(value) for value in body_symbols)
    if len(symbols) != 480:
        raise ValueError("Link ID 11 body requires exactly 480 symbols")
    output = []
    for n in range(first_sample, last_sample + 1):
        minimum_symbol = math.ceil((n - RRC_HALF_SPAN_SAMPLES) / SAMPLES_PER_SYMBOL)
        maximum_symbol = math.floor((n + RRC_HALF_SPAN_SAMPLES) / SAMPLES_PER_SYMBOL)
        value = 0.0 + 0.0j
        for symbol_index in range(minimum_symbol, maximum_symbol + 1):
            tap_index = n - SAMPLES_PER_SYMBOL * symbol_index
            value += _held_symbol(symbols, symbol_index) * RRC_TAPS[
                tap_index + RRC_HALF_SPAN_SAMPLES
            ]
        output.append(value)
    return tuple(output)


def make_slot(body_symbols) -> tuple[complex, ...]:
    carrier = pulse_shaped_carrier(body_symbols)
    ramp_up_carrier = carrier[:RAMP_SAMPLES]
    body = carrier[RAMP_SAMPLES:RAMP_SAMPLES + BODY_SAMPLES]
    ramp_down_carrier = carrier[RAMP_SAMPLES + BODY_SAMPLES:]
    ramp_up = tuple(
        value * (10.0 ** ((-50.0 + 50.0 * index / 32.0) / 20.0))
        for index, value in enumerate(ramp_up_carrier)
    )
    ramp_down = tuple(
        value * (10.0 ** ((-50.0 * index / 32.0) / 20.0))
        for index, value in enumerate(ramp_down_carrier)
    )
    slot = ramp_up + body + ramp_down + (0.0 + 0.0j,) * GUARD_SAMPLES
    if len(slot) != SLOT_SAMPLES or any(
        not math.isfinite(value.real) or not math.isfinite(value.imag) for value in slot
    ):
        raise AssertionError("Link ID 11 slot waveform invariant failed")
    return slot


def polyphase_energies() -> tuple[float, float, float, float]:
    return tuple(
        sum(RRC_TAPS[index] ** 2 for index in range(phase, len(RRC_TAPS), 4))
        for phase in range(4)
    )


def to_cf32le(samples) -> bytes:
    output = bytearray()
    for value in samples:
        output.extend(struct.pack("<ff", float(value.real), float(value.imag)))
    return bytes(output)


def from_cf32le(data: bytes) -> tuple[complex, ...]:
    if not isinstance(data, bytes) or len(data) % 8:
        raise ValueError("cf32le data length must be a multiple of eight")
    return tuple(complex(i, q) for i, q in struct.iter_unpack("<ff", data))


def validate_cf32le_full_scale(data: bytes) -> None:
    """Validate the frozen Radio unit-circle contract on serialized samples.

    The Radio contract is defined on the binary32 values that cross the
    ``cf32le`` boundary, not on their pre-serialization Python values.  Values
    yielded by ``struct.iter_unpack`` are exact binary32-to-binary64
    promotions, so the arithmetic below is the required ``I*I + Q*Q`` test.
    """

    if not isinstance(data, bytes) or len(data) % 8:
        raise ValueError("cf32le data length must be a multiple of eight")
    for i_value, q_value in struct.iter_unpack("<ff", data):
        i_binary64 = float(i_value)
        q_binary64 = float(q_value)
        magnitude_squared = (
            i_binary64 * i_binary64 + q_binary64 * q_binary64
        )
        if not math.isfinite(magnitude_squared) or magnitude_squared > 1.0:
            raise ValueError("cf32le sample exceeds the Radio TX unit-circle contract")


def _round_half_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0.0 else math.ceil(value - 0.5)


def to_test_iq_int16_le(samples) -> bytes:
    source = tuple(complex(value) for value in samples)
    amplitude = max(
        (1.0,) + tuple(abs(value.real) for value in source) + tuple(abs(value.imag) for value in source)
    )
    output = bytearray()
    for value in source:
        i = max(-32768, min(32767, _round_half_away_from_zero(32767.0 * value.real / amplitude)))
        q = max(-32768, min(32767, _round_half_away_from_zero(32767.0 * value.imag / amplitude)))
        output.extend(struct.pack("<hh", i, q))
    return bytes(output)


def from_test_iq_int16_le(data: bytes) -> tuple[complex, ...]:
    if not isinstance(data, bytes) or len(data) % 4:
        raise ValueError("test-iq-int16-le length must be a multiple of four")
    return tuple(complex(i / 32767.0, q / 32767.0) for i, q in struct.iter_unpack("<hh", data))


def _matched_filter_reference(source) -> tuple[complex, ...]:
    """참조 구현. numpy가 없을 때 그대로 쓰인다."""

    output = []
    for n in range(len(source)):
        value = 0.0 + 0.0j
        first = max(0, n - RRC_HALF_SPAN_SAMPLES)
        last = min(len(source) - 1, n + RRC_HALF_SPAN_SAMPLES)
        for source_index in range(first, last + 1):
            value += source[source_index] * RRC_TAPS[n - source_index + RRC_HALF_SPAN_SAMPLES]
        output.append(value)
    return tuple(output)


def matched_filter(samples) -> tuple[complex, ...]:
    """RRC matched filter. 판정·탐색·출력 의미는 참조 구현과 같다.

    `output[n] = sum_j TAPS[j] * source[n + H - j]` (H=RRC_HALF_SPAN_SAMPLES)
    이고 범위 밖은 0으로 확장되므로 `numpy.convolve(source, TAPS)`의
    `[H : H+len(source)]` 구간과 정확히 대응한다 — 참조 루프의
    `max(0, ...)`·`min(len-1, ...)` 가드까지 같은 결과다.

    Pi 5 프로파일에서 이 함수가 잡음 창 디코드 시간의 38%를 차지했다
    (창당 ~17ms). 합산 순서가 달라 부동소수 반올림 차이가 생기지만
    PHY 경계 출력은 float32라 전송선상 결과는 보존된다.
    """

    source = tuple(complex(value) for value in samples)
    if _numpy is not None and source:
        try:
            array = _numpy.asarray(source, dtype=_numpy.complex128)
            convolved = _numpy.convolve(array, _RRC_TAPS_ARRAY)
            picked = convolved[
                RRC_HALF_SPAN_SAMPLES:RRC_HALF_SPAN_SAMPLES + len(source)
            ]
            # tolist()로 파이썬 complex로 되돌린다. numpy 스칼라를 그대로
            # 흘리면 하류의 conjugate/곱셈이 파이썬 complex보다 느려
            # (Pi 프로파일: numpy.generic.conjugate 14,560회) 필터에서 아낀
            # 시간을 도로 까먹는다.
            return tuple(picked.tolist())
        except Exception:  # noqa: BLE001 — 참조 경로로 안전 복귀
            pass
    return _matched_filter_reference(source)


def cubic_interpolate(samples, position: float) -> complex:
    source = samples
    k = math.floor(position)
    mu = position - k
    coefficients = (
        -mu * (mu - 1.0) * (mu - 2.0) / 6.0,
        (mu + 1.0) * (mu - 1.0) * (mu - 2.0) / 2.0,
        -(mu + 1.0) * mu * (mu - 2.0) / 2.0,
        (mu + 1.0) * mu * (mu - 1.0) / 6.0,
    )
    value = 0.0 + 0.0j
    for offset, coefficient in zip((-1, 0, 1, 2), coefficients):
        index = k + offset
        if 0 <= index < len(source):
            value += coefficient * source[index]
    return value
