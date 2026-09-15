"""Stage 7 host affine time bridge 수치 코어 (`stage7` §13.1~§13.3).

binary floating point를 timed-command 변환에 사용하지 않는다. coefficient는 정수
nanosecond ordinary least squares의 reduced rational로 보존하고, `RNE(p/q)`는
signed rational을 nearest-even으로 반올림한다.

이 module은 순수 수치 코어다. 실제 bracket 수집(CLOCK_MONOTONIC_RAW,
worker hardware time)은 Pi runtime layer가 수행해 observation을 이 코어에
공급한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd

MAX_BRACKET_NS = 2_000_000
MIN_OBSERVATIONS = 8
MIN_SPAN_NS = 2_000_000_000
MAX_ACQUISITION_NS = 600_000_000_000
# §13.2 v0.8.15 개정(사용자 승인 2026-08-26): timed 변환은 측정된 beta를 exact
# rational로 그대로 사용하므로 절대 비율 편차 |d|의 크기는 변환 정확도에 영향이
# 없고, host 수정발진기(±20ppm급)와 B210 TCXO의 물리 범위를 반영해 |d|≤25ppm,
# 측정 불확도는 u_beta≤1ppm으로 분리·강화한다 (실측: Pi host 대비 두 B210 모두
# d≈−6.2..−6.8ppm, 600초 적응 획득에서 u_beta ~0.15ppm 수렴).
ABSOLUTE_RATE_LIMIT_PPM = Fraction(25)
U_BETA_LIMIT_PPM = Fraction(1)
ROUND_TRIP_LIMIT_NS = 1
UTC_ANCHOR_MAX_BRACKET_NS = 2_000_000
UTC_RECHECK_LIMIT_NS = 10_000_000
RF_SPREAD_LIMIT_NS = 50_000
RF_WARNING_NS = 40_000
TRIANGLE_CLOSURE_LIMIT_NS = 50_000
ACTIVE_HORIZON_NS = 120_000_000_000
MAX_BRIDGE_GENERATIONS = 3


class Stage7TimeBridgeError(ValueError):
    """수치 계약 위반 (§13)."""


def rne(numerator: int, denominator: int) -> int:
    """signed rational `p/q`의 nearest-even 반올림 (§13.2)."""

    if denominator == 0:
        raise Stage7TimeBridgeError("분모가 0입니다")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    quotient, remainder = divmod(numerator, denominator)
    # Python divmod: remainder는 항상 0..denominator-1 (denominator>0).
    twice = 2 * remainder
    if twice < denominator:
        return quotient
    if twice > denominator:
        return quotient + 1
    # ties-to-even
    return quotient if quotient % 2 == 0 else quotient + 1


def rne_fraction(value: Fraction) -> int:
    return rne(value.numerator, value.denominator)


def ceil_pos(value: Fraction) -> int:
    """양의 rational 올림 (§13.2)."""

    if value < 0:
        raise Stage7TimeBridgeError("ceil_pos 입력은 음수가 아니어야 합니다")
    return -((-value.numerator) // value.denominator)


def _reduced(numerator: int, denominator: int) -> tuple[int, int]:
    if denominator == 0:
        raise Stage7TimeBridgeError("분모가 0입니다")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    common = gcd(abs(numerator), denominator)
    if common:
        numerator //= common
        denominator //= common
    return numerator, denominator


@dataclass(frozen=True, slots=True)
class BracketObservation:
    """§13.2 bracket 하나. `m_sample=RNE((before+after)/2)`, `u_mid=ceil(u/2)`."""

    m_before_ns: int
    hardware_ns: int
    m_after_ns: int

    def __post_init__(self) -> None:
        if self.m_after_ns < self.m_before_ns:
            raise Stage7TimeBridgeError("bracket 순서가 역행합니다")

    @property
    def u_sample_ns(self) -> int:
        return self.m_after_ns - self.m_before_ns

    @property
    def m_sample_ns(self) -> int:
        return rne(self.m_before_ns + self.m_after_ns, 2)

    @property
    def u_mid_ns(self) -> int:
        return -((-self.u_sample_ns) // 2)  # ceil(u/2)


@dataclass(frozen=True, slots=True)
class AffineCoefficients:
    """reduced rational alpha/beta와 anchor (§13.2)."""

    h0_ns: int
    m0_ns: int
    alpha_num: int
    alpha_den: int
    beta_num: int
    beta_den: int

    def forward_exact(self, hardware_ns: int) -> Fraction:
        """`M_p_exact(h)` — 마지막 RNE를 제거한 exact 값."""

        return (
            Fraction(self.m0_ns)
            + Fraction(self.alpha_num, self.alpha_den)
            + Fraction(self.beta_num * (hardware_ns - self.h0_ns), self.beta_den)
        )

    def forward(self, hardware_ns: int) -> int:
        """`M(h) = m0 + RNE(alpha + beta*(h-h0))`."""

        value = (
            Fraction(self.alpha_num, self.alpha_den)
            + Fraction(self.beta_num * (hardware_ns - self.h0_ns), self.beta_den)
        )
        return self.m0_ns + rne_fraction(value)

    def inverse(self, m_ns: int) -> int:
        """`H(m) = h0 + RNE((((m-m0)*alpha_den-alpha_num)*beta_den)/(alpha_den*beta_num))`."""

        numerator = ((m_ns - self.m0_ns) * self.alpha_den - self.alpha_num) * self.beta_den
        denominator = self.alpha_den * self.beta_num
        return self.h0_ns + rne(numerator, denominator)

    def forward_corrected(self, hardware_ns: int, c_num: int, c_den: int) -> int:
        """`M_corrected(h) = RNE(M_p_exact(h) + c)`."""

        return rne_fraction(self.forward_exact(hardware_ns) + Fraction(c_num, c_den))

    def inverse_corrected(self, m_ns: int, c_num: int, c_den: int) -> int:
        value = (
            Fraction(m_ns)
            - Fraction(self.m0_ns)
            - Fraction(self.alpha_num, self.alpha_den)
            - Fraction(c_num, c_den)
        ) * Fraction(self.beta_den, self.beta_num)
        return self.h0_ns + rne_fraction(value)


@dataclass(frozen=True, slots=True)
class AffineFitResult:
    coefficients: AffineCoefficients
    observation_count: int
    span_ns: int
    max_u_sample_ns: int
    d_ppm_num: int
    d_ppm_den: int
    u_beta_ppm: int
    residuals_ns: tuple

    @property
    def d_ppm_exact(self) -> Fraction:
        return Fraction(self.d_ppm_num, self.d_ppm_den)

    @property
    def drift_envelope_ppm(self) -> Fraction:
        return abs(self.d_ppm_exact) + self.u_beta_ppm


def fit_affine(observations) -> AffineFitResult:
    """§13.2 정수 nanosecond OLS. 실패는 `Stage7TimeBridgeError`다."""

    observations = tuple(observations)
    if len(observations) < MIN_OBSERVATIONS:
        raise Stage7TimeBridgeError("최소 8 observation이 필요합니다")
    for observation in observations:
        if observation.u_sample_ns > MAX_BRACKET_NS:
            raise Stage7TimeBridgeError("bracket 폭이 2,000,000 ns를 넘습니다")
    hardware = [observation.hardware_ns for observation in observations]
    if any(b <= a for a, b in zip(hardware, hardware[1:])):
        raise Stage7TimeBridgeError("hardware time이 단조 증가하지 않습니다")
    span = observations[-1].m_sample_ns - observations[0].m_sample_ns
    if span < MIN_SPAN_NS:
        raise Stage7TimeBridgeError("측정 span이 2초 미만입니다")

    h0 = observations[0].hardware_ns
    m0 = observations[0].m_sample_ns
    xs = [observation.hardware_ns - h0 for observation in observations]
    ys = [observation.m_sample_ns - m0 for observation in observations]
    count = len(observations)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    beta_num = count * sum_xy - sum_x * sum_y
    beta_den = count * sum_xx - sum_x * sum_x
    if beta_den <= 0 or beta_num <= 0:
        raise Stage7TimeBridgeError("OLS 기울기가 양수가 아닙니다")
    alpha_num = sum_y * beta_den - beta_num * sum_x
    alpha_den = count * beta_den
    beta_num, beta_den = _reduced(beta_num, beta_den)
    alpha_num, alpha_den = _reduced(alpha_num, alpha_den)
    coefficients = AffineCoefficients(
        h0_ns=h0,
        m0_ns=m0,
        alpha_num=alpha_num,
        alpha_den=alpha_den,
        beta_num=beta_num,
        beta_den=beta_den,
    )

    # drift와 conservative slope uncertainty (§13.2).
    d_ppm = Fraction((beta_num - beta_den) * 1_000_000, beta_den)
    d_ppm_num, d_ppm_den = _reduced(d_ppm.numerator, d_ppm.denominator)
    envelope_sum = sum(
        abs(count * x - sum_x) * observation.u_mid_ns
        for x, observation in zip(xs, observations)
    )
    # 주의: uncertainty 분모는 reduce 전의 OLS 분모다.
    raw_beta_den = count * sum_xx - sum_x * sum_x
    u_beta_ppm = ceil_pos(Fraction(envelope_sum * 1_000_000, raw_beta_den))

    residuals = []
    for observation in observations:
        predicted = coefficients.forward(observation.hardware_ns)
        residual = observation.m_sample_ns - predicted
        residuals.append(residual)
        if abs(residual) > observation.u_mid_ns:
            raise Stage7TimeBridgeError(
                "observation residual이 u_mid envelope를 벗어났습니다"
            )
    if u_beta_ppm > U_BETA_LIMIT_PPM:
        raise Stage7TimeBridgeError("slope 불확도 u_beta가 1 ppm을 넘습니다")
    if abs(Fraction(d_ppm_num, d_ppm_den)) > ABSOLUTE_RATE_LIMIT_PPM:
        raise Stage7TimeBridgeError("절대 비율 편차 |d|가 25 ppm을 넘습니다")

    return AffineFitResult(
        coefficients=coefficients,
        observation_count=count,
        span_ns=span,
        max_u_sample_ns=max(observation.u_sample_ns for observation in observations),
        d_ppm_num=d_ppm_num,
        d_ppm_den=d_ppm_den,
        u_beta_ppm=u_beta_ppm,
        residuals_ns=tuple(residuals),
    )


def verify_round_trip(
    coefficients: AffineCoefficients,
    horizon_start_ns: int,
    horizon_end_ns: int,
    *,
    c_num: int = 0,
    c_den: int = 1,
) -> None:
    """active horizon 시작·중간·끝 forward/inverse round-trip `≤1 ns` (§13.2/§13.3)."""

    midpoint = (horizon_start_ns + horizon_end_ns) // 2
    for m_ns in (horizon_start_ns, midpoint, horizon_end_ns):
        hardware = coefficients.inverse_corrected(m_ns, c_num, c_den)
        back = coefficients.forward_corrected(hardware, c_num, c_den)
        if abs(back - m_ns) > ROUND_TRIP_LIMIT_NS:
            raise Stage7TimeBridgeError(
                f"round-trip 오차가 1 ns를 넘습니다 ({back - m_ns} ns)"
            )


# --- host UTC label anchor (§13.1) --------------------------------------------


@dataclass(frozen=True, slots=True)
class HostUtcAnchor:
    host_raw_anchor_ns: int
    host_utc_anchor_ns: int
    bracket_width_ns: int

    def utc_label(self, m_ns: int) -> int:
        return self.host_utc_anchor_ns + (m_ns - self.host_raw_anchor_ns)

    def label_to_raw(self, utc_ns: int) -> int:
        return self.host_raw_anchor_ns + (utc_ns - self.host_utc_anchor_ns)


def select_utc_anchor(brackets) -> HostUtcAnchor:
    """8회 bracket 중 가장 작은 폭 하나 (`≤2,000,000 ns`)를 선택한다 (§13.1)."""

    brackets = tuple(brackets)
    if len(brackets) != 8:
        raise Stage7TimeBridgeError("UTC anchor bracket은 정확히 8회 측정해야 합니다")
    best = None
    for raw_before, utc_sample, raw_after in brackets:
        if raw_after < raw_before:
            raise Stage7TimeBridgeError("UTC anchor bracket이 역행합니다")
        width = raw_after - raw_before
        if best is None or width < best[0]:
            best = (width, raw_before, utc_sample, raw_after)
    width, raw_before, utc_sample, raw_after = best
    if width > UTC_ANCHOR_MAX_BRACKET_NS:
        raise Stage7TimeBridgeError("UTC anchor bracket 폭이 2 ms를 넘습니다")
    return HostUtcAnchor(
        host_raw_anchor_ns=rne(raw_before + raw_after, 2),
        host_utc_anchor_ns=utc_sample,
        bracket_width_ns=width,
    )


# --- §13.3 RF timestamp rational 계산 -----------------------------------------


def median_even_fraction(sorted_values) -> Fraction:
    """`median_even`: 홀수면 중앙값, 짝수면 중앙 두 값의 `(v[n/2-1]+v[n/2])/2`."""

    values = tuple(sorted_values)
    if not values:
        raise Stage7TimeBridgeError("빈 입력의 median은 정의되지 않습니다")
    count = len(values)
    if count % 2 == 1:
        return Fraction(values[count // 2])
    left = Fraction(values[count // 2 - 1])
    right = Fraction(values[count // 2])
    return (left + right) / 2


def directed_path_median(d_values_ns) -> Fraction:
    """path별 `d_k`의 §13.3 median `D_ij` (v0.9.5: 창당 결손 ≤1 허용 —
    9 또는 10개 값의 표준 median; rr-p2-17 run l 실측: 정확히-10 강제가
    v0.9.5 관용과 어긋나 9-값 경로를 예외로 죽였다)."""

    values = sorted(d_values_ns)
    if len(values) not in (9, 10):
        raise Stage7TimeBridgeError("D_ij에는 9 또는 10개 d_k가 필요합니다")
    count = len(values)
    if count % 2 == 1:
        return Fraction(values[count // 2])
    return Fraction(values[count // 2 - 1] + values[count // 2], 2)


def rf_drift_envelope_ppm(
    d_values_ns, phy_sample_rate_hz: int, scheduled_span_ns: int
) -> Fraction:
    """`B_rf_ppm = ceil_pos((max-min+2*q_rx)*1e6/span)` (rational 반환 전 ceil)."""

    if scheduled_span_ns <= 0:
        raise Stage7TimeBridgeError("scheduled span이 양수가 아닙니다")
    q_rx = -((-500_000_000) // phy_sample_rate_hz)  # ceil
    spread = max(d_values_ns) - min(d_values_ns)
    return Fraction(
        ceil_pos(Fraction((spread + 2 * q_rx) * 1_000_000, scheduled_span_ns))
    )


@dataclass(frozen=True, slots=True)
class PairwiseOffsets:
    """`O_ij=(D_ij-D_ji)/2`, `P_ij=(D_ij+D_ji)/2` (reduced signed rational)."""

    o_ij: Fraction
    p_ij: Fraction


def pairwise_offsets(d_forward: Fraction, d_reverse: Fraction) -> PairwiseOffsets:
    return PairwiseOffsets(
        o_ij=(d_forward - d_reverse) / 2, p_ij=(d_forward + d_reverse) / 2
    )


def triangle_closure_ns(o_ab: Fraction, o_ra: Fraction, o_rb: Fraction) -> Fraction:
    """`abs(O_AB-(O_RB-O_RA))` — `≤50000 ns`여야 한다 (§13.3)."""

    return abs(o_ab - (o_rb - o_ra))


def corrections_from_roc(o_ra: Fraction, o_rb: Fraction):
    """ROC 기준 `c_R=0`, `c_A=-O_RA`, `c_B=-O_RB` (reduced rational tuple)."""

    def as_pair(value: Fraction) -> tuple[int, int]:
        return _reduced(value.numerator, value.denominator)

    return {
        "ROC": (0, 1),
        "MASS-A": as_pair(-o_ra),
        "MASS-B": as_pair(-o_rb),
    }


__all__ = (
    "ACTIVE_HORIZON_NS",
    "AffineCoefficients",
    "AffineFitResult",
    "BracketObservation",
    "ABSOLUTE_RATE_LIMIT_PPM",
    "U_BETA_LIMIT_PPM",
    "HostUtcAnchor",
    "MAX_BRIDGE_GENERATIONS",
    "PairwiseOffsets",
    "RF_SPREAD_LIMIT_NS",
    "RF_WARNING_NS",
    "Stage7TimeBridgeError",
    "TRIANGLE_CLOSURE_LIMIT_NS",
    "ceil_pos",
    "corrections_from_roc",
    "directed_path_median",
    "fit_affine",
    "median_even_fraction",
    "pairwise_offsets",
    "rf_drift_envelope_ppm",
    "rne",
    "rne_fraction",
    "select_utc_anchor",
    "triangle_closure_ns",
    "verify_round_trip",
)
