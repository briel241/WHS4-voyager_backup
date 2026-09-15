"""Deterministic 4x rational sample-rate conversion between 76,800 and 307,200 S/s.

`stage6_phy_common_baseline.md` §13.5 (`DR-STAGE6-MR-001`)가 고정한 Runtime 소유 변환
경계의 primitive다. Link 11 native slot(2,048 samples @76,800)과 device slot(8,192 samples
@307,200) 사이를 sample 반복/드롭 없이 linear-phase polyphase FIR로 변환한다. slot마다
상태가 없고(zero-extension) 군지연은 정확히 보상되므로 slot 시작 timestamp와 slot 길이는
변하지 않는다.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

NATIVE_SAMPLE_RATE_HZ = 76_800
DEVICE_SAMPLE_RATE_HZ = 307_200
RATE_FACTOR = 4
NATIVE_SLOT_SAMPLES = 2_048
DEVICE_SLOT_SAMPLES = 8_192

# 승인 prototype lowpass FIR: N=97, 군지연 M=48 device samples(=12 native samples),
# cutoff pi/4, Kaiser beta=8.6 (I0 급수는 term < acc*1e-21에서 종료). 생성식은
# 기준선 §13.5에 있으며 아래 binary64 literal이 유일한 동결 원본이다.
PROTOTYPE_TAP_COUNT = 97
PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES = 48
PROTOTYPE_TAP_SHA256 = "8c578693f74566139d5f8c886e2aae1c64e3449a09cff2df5f2500994d81b9c9"
PROTOTYPE_TAP_BINARY64_BITS = (
    0xBBCEA981FEDED3F2, 0xBEE9B3E6DA113526, 0xBEFEC5DA39B65A24,
    0xBF00F448C9EFAA0E, 0x3C1863EB51FEA39D, 0x3F11BA5DB45021E2,
    0x3F213C9132823051, 0x3F205A67C4043485, 0xBC14F6A282D37DB7,
    0xBF2BCA261DC23F16, 0xBF39063EA1C860B6, 0xBF363EA914BA4A92,
    0x3C284254CC12CF16, 0x3F410140BBBAEB1E, 0x3F4D500DFB167BFF,
    0x3F490DE60228B482, 0xBC37714AB8B38F8B, 0xBF51E6ED6F6238F9,
    0xBF5DF7CB1F9AE503, 0xBF58EFA2FB318DB4, 0x3C43EB240F52B4A5,
    0x3F60FE88E6623D7A, 0x3F6BDC5D7D56C3B6, 0x3F66BDE3FF92F91F,
    0xBC4E98B5FA278115, 0xBF6DF7916841D3A6, 0xBF78347F97D48AAD,
    0xBF737F027EC0FC30, 0x3C559961655D7C62, 0x3F791EE3C531842C,
    0x3F841BBF29583913, 0x3F80137EF9DFD904, 0xBC5C551FC210A55C,
    0xBF8483B2AFE46296, 0xBF906423EB1294F1, 0xBF8A3924D5450B51,
    0x3C616398FF098B2D, 0x3F90E689ED3A28F9, 0x3F9B4D0BCA6C1DD5,
    0x3F9630C9685F4E58, 0xBC6411FB58F0FFF5, 0xBF9E336D49C672C9,
    0xBFA97E859865DF12, 0xBFA60E841EEAC057, 0x3C65D936C3914444,
    0x3FB2E7D6FC18F120, 0x3FC43AB564FCF285, 0x3FCCC2769FFEA83B,
    0x3FD0000000000000, 0x3FCCC2769FFEA83B, 0x3FC43AB564FCF285,
    0x3FB2E7D6FC18F120, 0x3C65D936C3914444, 0xBFA60E841EEAC057,
    0xBFA97E859865DF12, 0xBF9E336D49C672C9, 0xBC6411FB58F0FFF5,
    0x3F9630C9685F4E58, 0x3F9B4D0BCA6C1DD5, 0x3F90E689ED3A28F9,
    0x3C616398FF098B2D, 0xBF8A3924D5450B51, 0xBF906423EB1294F1,
    0xBF8483B2AFE46296, 0xBC5C551FC210A55C, 0x3F80137EF9DFD904,
    0x3F841BBF29583913, 0x3F791EE3C531842C, 0x3C559961655D7C62,
    0xBF737F027EC0FC30, 0xBF78347F97D48AAD, 0xBF6DF7916841D3A6,
    0xBC4E98B5FA278115, 0x3F66BDE3FF92F91F, 0x3F6BDC5D7D56C3B6,
    0x3F60FE88E6623D7A, 0x3C43EB240F52B4A5, 0xBF58EFA2FB318DB4,
    0xBF5DF7CB1F9AE503, 0xBF51E6ED6F6238F9, 0xBC37714AB8B38F8B,
    0x3F490DE60228B482, 0x3F4D500DFB167BFF, 0x3F410140BBBAEB1E,
    0x3C284254CC12CF16, 0xBF363EA914BA4A92, 0xBF39063EA1C860B6,
    0xBF2BCA261DC23F16, 0xBC14F6A282D37DB7, 0x3F205A67C4043485,
    0x3F213C9132823051, 0x3F11BA5DB45021E2, 0x3C1863EB51FEA39D,
    0xBF00F448C9EFAA0E, 0xBEFEC5DA39B65A24, 0xBEE9B3E6DA113526,
    0xBBCEA981FEDED3F2,
)
PROTOTYPE_TAPS = tuple(
    struct.unpack("<d", struct.pack("<Q", value))[0]
    for value in PROTOTYPE_TAP_BINARY64_BITS
)

_NATIVE_DELAY = PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES // RATE_FACTOR


def _index_order_sum(values) -> float:
    # §13.5: 정규화 합은 tap index 오름차순의 순차 binary64 덧셈이다. `fsum` 같은
    # extended-precision 누적은 승인 nominal DC 이득과 1 ULP 다른 계수를 만든다.
    total = 0.0
    for value in values:
        total += value
    return total


def _interpolation_branches() -> tuple[tuple[float, ...], ...]:
    branches = []
    for phase in range(RATE_FACTOR):
        raw = [
            RATE_FACTOR * PROTOTYPE_TAPS[index]
            for index in range(phase, PROTOTYPE_TAP_COUNT, RATE_FACTOR)
        ]
        scale = _index_order_sum(raw)
        branches.append(tuple(value / scale for value in raw))
    return tuple(branches)


def _decimation_taps() -> tuple[float, ...]:
    scale = _index_order_sum(PROTOTYPE_TAPS)
    return tuple(value / scale for value in PROTOTYPE_TAPS)


# 파생 계수: 보간 branch는 DC 이득이 정확히 1이 되도록 branch별 정규화하고(scalar 곱은
# branch의 위상 응답을 바꾸지 않는다), 데시메이션 tap은 전체 합이 정확히 1이 되게 정규화한다.
_INTERP_BRANCHES = _interpolation_branches()
_DECIM_TAPS = _decimation_taps()

# numpy는 PHY·Radio 구현 단계에서 쓰도록 pyproject가 이미 허용한다
# (`dsp = ["numpy>=1.24"]`). 없으면 참조 구현이 그대로 동작한다.
try:  # pragma: no cover - 환경 의존
    import numpy as _numpy
except ImportError:  # pragma: no cover
    _numpy = None
    _DECIM_TAPS_ARRAY = None
    _DECIM_PICK = None
else:
    _DECIM_TAPS_ARRAY = _numpy.asarray(_DECIM_TAPS, dtype=_numpy.float64)
    #: convolve 결과에서 뽑을 위치 = RATE_FACTOR*i + 군지연.
    _DECIM_PICK = (
        RATE_FACTOR * _numpy.arange(NATIVE_SLOT_SAMPLES)
        + PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES
    )


def _validated(samples: Sequence[complex], expected: int, direction: str) -> tuple[complex, ...]:
    values = tuple(complex(value) for value in samples)
    if len(values) != expected:
        raise ValueError(
            f"{direction} conversion requires exactly {expected} samples, got {len(values)}"
        )
    for value in values:
        if not (math.isfinite(value.real) and math.isfinite(value.imag)):
            raise ValueError(f"{direction} conversion input contains non-finite sample")
    return values


def interpolate_slot(samples: Sequence[complex]) -> tuple[complex, ...]:
    """native 2,048 @76,800 -> device 8,192 @307,200, 군지연 보상 포함."""

    values = _validated(samples, NATIVE_SLOT_SAMPLES, "native-to-device")
    if not any(value != 0j for value in values):
        return (0j,) * DEVICE_SLOT_SAMPLES
    length = NATIVE_SLOT_SAMPLES
    output: list[complex] = [0j] * DEVICE_SLOT_SAMPLES
    for phase, taps in enumerate(_INTERP_BRANCHES):
        tap_count = len(taps)
        for native_index in range(NATIVE_SLOT_SAMPLES):
            base = native_index + _NATIVE_DELAY
            accumulator = 0j
            for tap_index in range(tap_count):
                source = base - tap_index
                if 0 <= source < length:
                    accumulator += taps[tap_index] * values[source]
            output[RATE_FACTOR * native_index + phase] = accumulator
    return tuple(output)


def _decimate_slot_reference(
    values: tuple[complex, ...],
) -> tuple[complex, ...]:
    """§13.5 참조 구현. numpy가 없을 때 그대로 쓰인다."""

    length = DEVICE_SLOT_SAMPLES
    taps = _DECIM_TAPS
    delay = PROTOTYPE_GROUP_DELAY_DEVICE_SAMPLES
    output: list[complex] = [0j] * NATIVE_SLOT_SAMPLES
    for native_index in range(NATIVE_SLOT_SAMPLES):
        base = RATE_FACTOR * native_index + delay
        accumulator = 0j
        for tap_index in range(PROTOTYPE_TAP_COUNT):
            source = base - tap_index
            if 0 <= source < length:
                accumulator += taps[tap_index] * values[source]
        output[native_index] = accumulator
    return tuple(output)


def _decimate_slot_vectorized(samples: Sequence[complex]) -> tuple[complex, ...]:
    """참조 구현과 같은 연산·같은 검증을 numpy로 수행한다.

    `convolve(x, taps)[RATE_FACTOR*i + delay]`가 참조 루프의 zero-extension
    가드까지 그대로 대응한다.
    """

    array = _numpy.asarray(samples, dtype=_numpy.complex128)
    if array.ndim != 1 or array.size != DEVICE_SLOT_SAMPLES:
        raise ValueError(
            f"device-to-native conversion requires exactly {DEVICE_SLOT_SAMPLES} "
            f"samples, got {array.size}"
        )
    if not bool(_numpy.isfinite(array).all()):
        raise ValueError(
            "device-to-native conversion input contains non-finite sample"
        )
    if not array.any():
        return (0j,) * NATIVE_SLOT_SAMPLES
    convolved = _numpy.convolve(array, _DECIM_TAPS_ARRAY)
    # tolist()로 파이썬 complex 복귀 — numpy 스칼라를 하류로 흘리면
    # 이어지는 스칼라 연산이 오히려 느려진다.
    return tuple(convolved[_DECIM_PICK].tolist())


def decimate_slot(samples: Sequence[complex]) -> tuple[complex, ...]:
    """device 8,192 @307,200 -> native 2,048 @76,800, 군지연 보상 포함.

    numpy가 있으면 벡터 경로를, 없으면 참조 구현을 쓴다. 합산 순서가 달라
    반올림이 1e-15 상대 수준으로 갈리지만 PHY 경계 출력(cf32le float32)은
    같다.
    """

    if _numpy is not None:
        try:
            return _decimate_slot_vectorized(samples)
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 — 참조 경로로 안전 복귀
            pass
    values = _validated(samples, DEVICE_SLOT_SAMPLES, "device-to-native")
    if not any(value != 0j for value in values):
        return (0j,) * NATIVE_SLOT_SAMPLES
    return _decimate_slot_reference(values)
