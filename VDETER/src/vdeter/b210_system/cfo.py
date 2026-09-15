"""Stage 7 상대 CFO 측정 profile `STAGE7_RAW_TONE_CFO_V1` (`stage7` §9.3).

numeric profile은 `STAGE7_NUMPY_FFT3072_BINARY64_V1`이다. spectral DFT는 resolved
manifest에 exact version/hash를 결속한 NumPy의 single-thread
`numpy.fft.fft(block,n=3072,norm=None)`를 사용하고, `f_b`의 complex product
real/imaginary 합은 sample index 오름차순의 Python 3.12 `math.fsum` 두 개로
계산한다. `arg`는 같은 runtime의 binary64 `math.atan2`, `pi`는 `math.pi`다.

NumPy는 Pi 실행환경(1.26.4)에서 결속되며 이 module은 lazy import한다.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

F_TONE_HZ = 25_000.0
SAMPLE_RATE_HZ = 307_200
PROBE_SAMPLES = 307_200
PROBE_AMPLITUDE = 0.125
BLOCK_SAMPLES = 3_072
BLOCKS_PER_BURST = 100
MIN_VALID_BLOCKS = 90
PEAK_BIN_LOW = 50
PEAK_BIN_HIGH = 450
FLOOR_BIN_LOW = 1
FLOOR_BIN_HIGH = 1_535
PEAK_EXCLUDE_HALF_WIDTH = 2
TONE_RATIO_MIN_DB = 20.0
PEAK_LIMIT_DBFS = -3.0
RMS_LOW_DBFS = -36.0
RMS_HIGH_DBFS = -6.0
CFO_ACCEPT_BOUND_HZ = 1_800.0
MIN_UNCERTAINTY_HZ = 10.0
MAD_SCALE = 1.4826
MIN_PROBE_SPAN_S = 60.0


class CfoError(ValueError):
    """측정 계약 위반 또는 측정 실패."""


def generate_probe_vector() -> bytes:
    """canonical active vector (§9.3 항목 1)의 interleaved `cf32le` bytes.

    `s[n]=A*exp(j*2*pi*25000*n/307200)` (`0≤n<N`), real/imaginary를 IEEE-754
    binary32 round-to-nearest-ties-to-even으로 양자화한다. initial phase `0 rad`,
    window/ramp/padding/digital gain 없음.
    """

    out = bytearray()
    pack = struct.Struct("<2f").pack
    for n in range(PROBE_SAMPLES):
        phase = 2.0 * math.pi * 25_000.0 * n / 307_200.0
        out += pack(
            PROBE_AMPLITUDE * math.cos(phase), PROBE_AMPLITUDE * math.sin(phase)
        )
    return bytes(out)


def median_even_binary64(sorted_values) -> float:
    """`median_even(v)`: 홀수면 중앙값, 짝수면 binary64 `(v[n/2-1]+v[n/2])/2`."""

    values = list(sorted_values)
    if not values:
        raise CfoError("빈 입력의 median은 정의되지 않습니다")
    count = len(values)
    if count % 2 == 1:
        return float(values[count // 2])
    return (float(values[count // 2 - 1]) + float(values[count // 2])) / 2.0


@dataclass(frozen=True, slots=True)
class BlockEstimate:
    valid: bool
    f_b_hz: float | None
    reason: str


def _decode_cf32le(data: bytes):
    count = len(data) // 8
    values = struct.unpack(f"<{count * 2}f", data)
    return values


def estimate_block(block_cf32le: bytes) -> BlockEstimate:
    """10 ms(3,072 sample) block 하나의 CFO 추정 (§9.3 항목 2)."""

    import numpy

    if len(block_cf32le) != BLOCK_SAMPLES * 8:
        raise CfoError("block은 정확히 3,072 cf32le sample이어야 합니다")
    interleaved = _decode_cf32le(block_cf32le)
    # binary32 → binary64 exact 승격.
    real = [float(interleaved[2 * i]) for i in range(BLOCK_SAMPLES)]
    imag = [float(interleaved[2 * i + 1]) for i in range(BLOCK_SAMPLES)]
    if any(not math.isfinite(v) for v in real) or any(
        not math.isfinite(v) for v in imag
    ):
        return BlockEstimate(False, None, "NON_FINITE")

    # energy gate: clipping 0, peak ≤ -3 dBFS, RMS -36..-6 dBFS.
    peak_amp = 0.0
    sum_sq = math.fsum(
        real[i] * real[i] + imag[i] * imag[i] for i in range(BLOCK_SAMPLES)
    )
    for i in range(BLOCK_SAMPLES):
        magnitude = math.hypot(real[i], imag[i])
        if magnitude > peak_amp:
            peak_amp = magnitude
        if abs(real[i]) >= 1.0 or abs(imag[i]) >= 1.0:
            return BlockEstimate(False, None, "CLIPPING")
    if peak_amp <= 0.0 or sum_sq <= 0.0:
        return BlockEstimate(False, None, "SILENT")
    peak_dbfs = 20.0 * math.log10(peak_amp)
    rms_dbfs = 10.0 * math.log10(sum_sq / BLOCK_SAMPLES)
    if peak_dbfs > PEAK_LIMIT_DBFS:
        return BlockEstimate(False, None, "PEAK_OVER")
    if not (RMS_LOW_DBFS <= rms_dbfs <= RMS_HIGH_DBFS):
        return BlockEstimate(False, None, "RMS_OUT_OF_RANGE")

    # spectral gate: rectangular 3,072-point DFT, P[k]=|DFT[k]|^2/N^2.
    array = numpy.asarray(
        [complex(real[i], imag[i]) for i in range(BLOCK_SAMPLES)], dtype=numpy.complex128
    )
    spectrum = numpy.fft.fft(array, n=BLOCK_SAMPLES, norm=None)
    power = (numpy.abs(spectrum) ** 2) / float(BLOCK_SAMPLES * BLOCK_SAMPLES)
    peak_bin = PEAK_BIN_LOW
    peak_value = float(power[PEAK_BIN_LOW])
    for k in range(PEAK_BIN_LOW, PEAK_BIN_HIGH + 1):
        value = float(power[k])
        if value > peak_value:
            peak_value = value
            peak_bin = k
    floor_values = sorted(
        float(power[k])
        for k in range(FLOOR_BIN_LOW, FLOOR_BIN_HIGH + 1)
        if not (peak_bin - PEAK_EXCLUDE_HALF_WIDTH <= k <= peak_bin + PEAK_EXCLUDE_HALF_WIDTH)
    )
    floor = median_even_binary64(floor_values)
    if floor <= 0.0 or peak_value <= 0.0:
        return BlockEstimate(False, None, "SPECTRUM_DEGENERATE")
    ratio_db = 10.0 * math.log10(peak_value / floor)
    if ratio_db < TONE_RATIO_MIN_DB:
        return BlockEstimate(False, None, "TONE_RATIO_LOW")

    # f_b = Fs/(2*pi) * arg(sum(r[n+1]*conj(r[n]))) - f_tone.
    real_sum = math.fsum(
        real[n + 1] * real[n] + imag[n + 1] * imag[n]
        for n in range(BLOCK_SAMPLES - 1)
    )
    imag_sum = math.fsum(
        imag[n + 1] * real[n] - real[n + 1] * imag[n]
        for n in range(BLOCK_SAMPLES - 1)
    )
    angle = math.atan2(imag_sum, real_sum)
    f_b = SAMPLE_RATE_HZ / (2.0 * math.pi) * angle - F_TONE_HZ
    if not math.isfinite(f_b):
        return BlockEstimate(False, None, "NON_FINITE")
    return BlockEstimate(True, f_b, "VALID")


@dataclass(frozen=True, slots=True)
class BurstEstimate:
    f_k_hz: float
    u_k_hz: float
    valid_block_count: int
    block_estimates: tuple


def estimate_burst(burst_cf32le: bytes) -> BurstEstimate:
    """energy gate를 통과한 1초를 3,072-sample block 100개로 나눠 추정한다."""

    if len(burst_cf32le) != BLOCK_SAMPLES * BLOCKS_PER_BURST * 8:
        raise CfoError("burst는 정확히 1초(307,200 sample)여야 합니다")
    estimates = []
    for index in range(BLOCKS_PER_BURST):
        start = index * BLOCK_SAMPLES * 8
        estimates.append(estimate_block(burst_cf32le[start : start + BLOCK_SAMPLES * 8]))
    valid = [item.f_b_hz for item in estimates if item.valid]
    if len(valid) < MIN_VALID_BLOCKS:
        raise CfoError(
            f"유효 block {len(valid)}/{BLOCKS_PER_BURST}: 90개 미만은 측정 실패입니다"
        )
    ordered = sorted(valid)
    f_k = median_even_binary64(ordered)
    deviations = sorted(abs(value - f_k) for value in valid)
    u_k = max(MIN_UNCERTAINTY_HZ, 3.0 * MAD_SCALE * median_even_binary64(deviations))
    return BurstEstimate(
        f_k_hz=f_k,
        u_k_hz=u_k,
        valid_block_count=len(valid),
        block_estimates=tuple(estimates),
    )


@dataclass(frozen=True, slots=True)
class PathCfoFit:
    a_hz: float
    b_hz_per_s: float
    r_max_hz: float
    horizon_s: float
    b_cfo_hz: float
    passed: bool

    def evidence(self) -> dict:
        return {
            "a_hex": self.a_hz.hex(),
            "a": repr(self.a_hz),
            "b_hex": self.b_hz_per_s.hex(),
            "b": repr(self.b_hz_per_s),
            "r_max_hex": self.r_max_hz.hex(),
            "horizon_s_hex": self.horizon_s.hex(),
            "b_cfo_hex": self.b_cfo_hz.hex(),
            "b_cfo": repr(self.b_cfo_hz),
            "passed": self.passed,
        }


def fit_path(burst_estimates, t_k_seconds, horizon_seconds: float) -> PathCfoFit:
    """10개 `(t_k,f_k)`의 OLS `f(t)=a+b*t`와 run-horizon bound (§9.3 항목 3~4).

    `t_k`는 마지막 burst first-sample hardware time 기준 음의 경과 초이고 입력
    순서는 `t_k` 오름차순으로 고정한다.
    """

    if len(burst_estimates) != 10 or len(t_k_seconds) != 10:
        raise CfoError("10개 burst 표본이 필요합니다")
    pairs = list(zip(t_k_seconds, burst_estimates))
    if any(b[0] <= a[0] for a, b in zip(pairs, pairs[1:])):
        raise CfoError("t_k는 오름차순이어야 합니다")
    if pairs[-1][0] != 0.0:
        raise CfoError("마지막 burst의 t_k는 0이어야 합니다")
    if -pairs[0][0] < MIN_PROBE_SPAN_S:
        raise CfoError("첫/마지막 probe 간격이 60초 미만입니다")
    count = 10.0
    sum_t = math.fsum(t for t, _ in pairs)
    sum_f = math.fsum(estimate.f_k_hz for _, estimate in pairs)
    sum_tt = math.fsum(t * t for t, _ in pairs)
    sum_tf = math.fsum(t * estimate.f_k_hz for t, estimate in pairs)
    denominator = count * sum_tt - sum_t * sum_t
    if denominator == 0.0:
        raise CfoError("OLS 분모가 0입니다")
    b = (count * sum_tf - sum_t * sum_f) / denominator
    a = (sum_f - b * sum_t) / count
    r_max = max(
        abs(estimate.f_k_hz - (a + b * t)) + estimate.u_k_hz for t, estimate in pairs
    )
    horizon = float(horizon_seconds)
    b_cfo = max(abs(a), abs(a + b * horizon)) + r_max
    return PathCfoFit(
        a_hz=a,
        b_hz_per_s=b,
        r_max_hz=r_max,
        horizon_s=horizon,
        b_cfo_hz=b_cfo,
        passed=b_cfo <= CFO_ACCEPT_BOUND_HZ,
    )


__all__ = (
    "BLOCKS_PER_BURST",
    "BLOCK_SAMPLES",
    "BlockEstimate",
    "BurstEstimate",
    "CFO_ACCEPT_BOUND_HZ",
    "F_TONE_HZ",
    "MIN_VALID_BLOCKS",
    "PROBE_AMPLITUDE",
    "PROBE_SAMPLES",
    "PathCfoFit",
    "SAMPLE_RATE_HZ",
    "CfoError",
    "estimate_block",
    "estimate_burst",
    "fit_path",
    "generate_probe_vector",
    "median_even_binary64",
)
