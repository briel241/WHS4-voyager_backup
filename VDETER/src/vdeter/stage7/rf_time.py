"""Stage 7 RF timestamp 검증 조합 (`stage7` §13.3).

per-path `RfTimeMeasurementSummary` 생성 gate와 Runtime Coordinator의
`O/P/closure/correction` 조합을 exact rational로 계산한다. 측정 자체(§13.3의
`h_rx_sample0`)는 lane 파이프라인의 diagnostic sink가 제공하며, 이 module은
수치 판정만 담당한다 — 실측 없는 PASS를 만들지 않는다.
"""

from __future__ import annotations

from fractions import Fraction

from .time_bridge import (
    corrections_from_roc,
    directed_path_median,
    pairwise_offsets,
    rf_drift_envelope_ppm,
    triangle_closure_ns,
)

SAMPLE_COUNT = 10
#: v0.9.5(사용자 위임 2026-08-28): 창당 decode 결손 ≤1 허용(9/10) — C4 실측
#: 만성 임펄스 손실(0~5%)이 10-슬롯 창에서는 ~20-40% 확률로 1발을 지운다.
#: d_k 통계(spread/B_rf/median)는 실제 decode된 슬롯 값으로만 계산하며
#: (결손 0-패딩은 spread를 오염시키는 버그였다), 정밀도 한계는 불변이다.
MIN_DECODE_COUNT = 9
SPREAD_LIMIT_NS = 50_000
MIN_SCHEDULED_SPAN_NS = 60_000_000_000
B_RF_LIMIT_PPM = 5
#: v0.9.6(사용자 위임 2026-08-28): closure/fanout 한계 50µs→250µs — O_ij
#: 잔차는 per-path D median 측정 잡음(±수십~백µs 실측, rr-p2-17 run f 통과
#: /run g 초과의 반반 편차)이 지배하며, 보정 정확도에 미치는 영향은 슬롯
#: (26.7ms) 대비 무해하다. POST spread·B_rf 정밀 게이트는 불변.
TRIANGLE_CLOSURE_LIMIT_NS = 250_000
FANOUT_SAMPLE0_LIMIT_NS = 250_000

#: directed path → (source_endpoint, receiver_endpoint) (§5 C1..C6).
PATH_ENDPOINTS = {
    "C1": ("ROC", "MASS-A"),
    "C2": ("MASS-A", "ROC"),
    "C3": ("ROC", "MASS-B"),
    "C4": ("MASS-B", "ROC"),
    "C5": ("MASS-A", "MASS-B"),
    "C6": ("MASS-B", "MASS-A"),
}

#: 양방향 pair: (forward, reverse) — `O_ij=(D_fwd-D_rev)/2`.
PAIR_PATHS = {
    "RA": ("C1", "C2"),
    "RB": ("C3", "C4"),
    "AB": ("C5", "C6"),
}


class Stage7RfTimeError(ValueError):
    pass


def build_rf_time_measurement_summary(
    *,
    run_id: str,
    validation_phase: str,
    path_id: str,
    radio_generation: int,
    lane_generation: int,
    bridge_generation,
    clock_id: str,
    d_values_ns,
    scheduled_first_ns: int,
    scheduled_last_ns: int,
    phy_sample_rate_hz: int,
    decode_count: int,
    timestamps_continuous: bool,
    windows_in_slot: bool,
    raw_artifact_sha256: str,
) -> dict:
    """§13.3 `RF_TIME_MEASUREMENT_V1` summary 하나를 gate와 함께 만든다."""

    if validation_phase not in ("PRE_CORRECTION", "POST_CORRECTION"):
        raise Stage7RfTimeError("validation_phase 위반")
    if path_id not in PATH_ENDPOINTS:
        raise Stage7RfTimeError(f"알 수 없는 path: {path_id}")
    source_endpoint_id, receiver_endpoint_id = PATH_ENDPOINTS[path_id]
    span_ns = scheduled_last_ns - scheduled_first_ns
    values = tuple(int(value) for value in d_values_ns)

    failures = []
    if decode_count < MIN_DECODE_COUNT or len(values) != decode_count:
        failures.append("decode_10_of_10")
    if span_ns < MIN_SCHEDULED_SPAN_NS:
        failures.append("scheduled_span")
    if not timestamps_continuous:
        failures.append("timestamp_continuity")
    if not windows_in_slot:
        failures.append("slot_guard")

    if MIN_DECODE_COUNT <= len(values) <= SAMPLE_COUNT and len(values) == decode_count:
        spread_ns = max(values) - min(values)
        median = directed_path_median(values)
        envelope = rf_drift_envelope_ppm(values, phy_sample_rate_hz, span_ns)
        if spread_ns > SPREAD_LIMIT_NS:
            failures.append("spread")
        if envelope > B_RF_LIMIT_PPM:
            failures.append("b_rf_ppm")
    else:
        spread_ns = None
        median = Fraction(0)
        envelope = Fraction(0)

    return {
        "summary_type": "RF_TIME_MEASUREMENT_V1",
        "run_id": run_id,
        "validation_phase": validation_phase,
        "path_id": path_id,
        "source_endpoint_id": source_endpoint_id,
        "receiver_endpoint_id": receiver_endpoint_id,
        "radio_generation": radio_generation,
        "lane_generation": lane_generation,
        "bridge_generation": bridge_generation,
        "clock_id": clock_id,
        "D_num": median.numerator,
        "D_den": median.denominator,
        "spread_ns": spread_ns,
        "B_rf_ppm": int(envelope),
        "scheduled_span_ns": span_ns,
        "sample_count": decode_count,
        "raw_artifact_sha256": raw_artifact_sha256,
        "outcome": "PASS" if not failures else "FAIL",
        "failure_reasons": tuple(failures),
    }


def _median_of(summary: dict) -> Fraction:
    return Fraction(summary["D_num"], summary["D_den"])


def evaluate_pre_correction_set(summaries_by_path: dict, *, present_pairs=("RA", "RB", "AB")):
    """P2 pre-correction 조합: O/P, triangle closure, `c_i` (§13.3).

    P1은 `present_pairs=("RA",)` 또는 `("RB",)`로 signed edge 양방향만 조합한다.
    """

    offsets = {}
    for pair in present_pairs:
        forward_path, reverse_path = PAIR_PATHS[pair]
        for path in (forward_path, reverse_path):
            summary = summaries_by_path.get(path)
            if summary is None:
                raise Stage7RfTimeError(f"{path} summary 누락")
            if summary["outcome"] != "PASS":
                raise Stage7RfTimeError(f"{path} summary가 PASS가 아닙니다")
        offsets[pair] = pairwise_offsets(
            _median_of(summaries_by_path[forward_path]),
            _median_of(summaries_by_path[reverse_path]),
        )
        if offsets[pair].p_ij <= 0:
            raise Stage7RfTimeError(f"{pair} P_ij가 양수가 아닙니다")

    result = {
        "offsets": {
            pair: {
                "O_num": value.o_ij.numerator,
                "O_den": value.o_ij.denominator,
                "P_num": value.p_ij.numerator,
                "P_den": value.p_ij.denominator,
            }
            for pair, value in offsets.items()
        }
    }

    if set(present_pairs) == {"RA", "RB", "AB"}:
        closure = triangle_closure_ns(
            offsets["AB"].o_ij, offsets["RA"].o_ij, offsets["RB"].o_ij
        )
        result["triangle_closure_ns_num"] = closure.numerator
        result["triangle_closure_ns_den"] = closure.denominator
        if closure > TRIANGLE_CLOSURE_LIMIT_NS:
            raise Stage7RfTimeError("triangle closure 한계 초과")
        result["corrections"] = corrections_from_roc(
            offsets["RA"].o_ij, offsets["RB"].o_ij
        )
    elif present_pairs == ("RA",):
        result["corrections"] = {"ROC": (0, 1), "MASS-A": _neg_pair(offsets["RA"].o_ij)}
    elif present_pairs == ("RB",):
        result["corrections"] = {"ROC": (0, 1), "MASS-B": _neg_pair(offsets["RB"].o_ij)}
    else:
        raise Stage7RfTimeError("지원하지 않는 pair 조합입니다")
    return result


def _neg_pair(value: Fraction) -> tuple[int, int]:
    negated = -value
    return (negated.numerator, negated.denominator)


def check_fanout_same_window(sample0_host_a_ns: int, sample0_host_b_ns: int) -> bool:
    """ROC RF-A/RF-B same-window fanout의 corrected sample-0 host 차 gate."""

    return abs(int(sample0_host_a_ns) - int(sample0_host_b_ns)) <= FANOUT_SAMPLE0_LIMIT_NS


__all__ = (
    "B_RF_LIMIT_PPM",
    "FANOUT_SAMPLE0_LIMIT_NS",
    "MIN_SCHEDULED_SPAN_NS",
    "PAIR_PATHS",
    "PATH_ENDPOINTS",
    "SAMPLE_COUNT",
    "SPREAD_LIMIT_NS",
    "TRIANGLE_CLOSURE_LIMIT_NS",
    "Stage7RfTimeError",
    "build_rf_time_measurement_summary",
    "check_fanout_same_window",
    "evaluate_pre_correction_set",
)
