"""Stage 7 Pi-side 실측 연산 (`stage7` §9.2, §9.3, §13.2, §13.3).

runtime client의 `operations` 실제 구현에 쓰이는 프리미티브다. Linux
`CLOCK_MONOTONIC_RAW`와 dual-lane worker RPC를 사용하며 Windows에서는 import만
가능하다 (실행은 Pi 전용).
"""

from __future__ import annotations

import hashlib
import math
import struct
import time as _time
from dataclasses import dataclass

from .cfo import estimate_burst, fit_path, generate_probe_vector
from .time_bridge import (
    BracketObservation,
    MAX_BRACKET_NS,
    TimeBridgeError,
    fit_affine,
    select_utc_anchor,
    verify_round_trip,
)

DEVICE_RATE = 307_200
SLOT_SAMPLES = 8_192
PROBE_SAMPLES = 307_200
PROBE_DURATION_NS = 1_000_000_000
MIN_PROBE_SPAN_NS = 60_000_000_000


def read_monotonic_raw_ns() -> int:
    return _time.clock_gettime_ns(_time.CLOCK_MONOTONIC_RAW)  # type: ignore[attr-defined]


def read_realtime_ns() -> int:
    return _time.clock_gettime_ns(_time.CLOCK_REALTIME)  # type: ignore[attr-defined]


def collect_utc_anchor():
    """§13.1: bracket 8회 중 최소 폭 하나 (`≤2,000,000 ns`)."""

    brackets = []
    for _ in range(8):
        raw_before = read_monotonic_raw_ns()
        utc_sample = read_realtime_ns()
        raw_after = read_monotonic_raw_ns()
        brackets.append((raw_before, utc_sample, raw_after))
    return select_utc_anchor(brackets)


def collect_bracket(worker) -> BracketObservation:
    """§13.2 bracket 하나: raw / hardware / raw.

    process 격리 worker는 RPC 왕복이 bracket 폭에 그대로 더해져 2 ms gate를
    상시 초과한다. worker가 `bracket_hardware_time()`을 제공하면 UHD 호출을
    worker process 안에서 같은 `CLOCK_MONOTONIC_RAW`로 직접 감싼 bracket을
    사용한다 (동일 host이므로 clock 좌표는 같다)."""

    bracket_reader = getattr(worker, "bracket_hardware_time", None)
    if bracket_reader is not None:
        m_before, hardware_ns, m_after = bracket_reader()
        return BracketObservation(
            m_before_ns=int(m_before), hardware_ns=int(hardware_ns), m_after_ns=int(m_after)
        )
    m_before = read_monotonic_raw_ns()
    hardware_ns = int(worker.hardware_time_ns())
    m_after = read_monotonic_raw_ns()
    return BracketObservation(
        m_before_ns=m_before, hardware_ns=hardware_ns, m_after_ns=m_after
    )


#: host-time bracket 선택 규칙 (operational_readiness_and_bringup_spec §13.3, OPT-01).
#: RESTART_SERIES_V1  — 현행(active): wide bracket(>2 ms) 또는 hardware 역행이면 시리즈 전체 폐기·재시작.
#: REJECT_BRACKET_KEEP_SERIES_V1 — 후보(PROPOSED/DISABLED): wide bracket만 객관 기준으로 reject하고 accepted
#:   series는 유지; hardware 역행은 clock epoch 손실로 보아 series 전체 reset. 최소 8개·2 s span·
#:   u_beta ≤ 1 ppm·|d| ≤ 25 ppm·round-trip ≤ 1 ns·600 s hard cap·fit_affine의 residual gate는 동일.
#: 활성화는 RF-off·저장 replay·synthetic adversarial·실물 side-by-side(shadow) 동등성 뒤 별도 승인
#: 대상이다 — production `acquire_host_time`은 baseline을 호출하며 후보는 같은 bracket 스트림 위에서
#: `HostTimeSelectionShadow`로만 병행 평가된다(active 경로 판정에 영향 없음).
HOST_TIME_SELECTION_BASELINE = "RESTART_SERIES_V1"
HOST_TIME_SELECTION_CANDIDATE = "REJECT_BRACKET_KEEP_SERIES_V1"


def _try_fit(observations, min_observations, min_span_ns):
    """공통 fit 게이트 — 두 규칙이 정확히 같은 판정 함수(fit_affine + round-trip)를 쓴다."""
    if not (len(observations) >= min_observations
            and observations[-1].m_sample_ns - observations[0].m_sample_ns >= min_span_ns):
        return None, None
    try:
        fit = fit_affine(observations)
        start = fit.coefficients.forward(observations[0].hardware_ns)
        verify_round_trip(fit.coefficients, start, start + 120_000_000_000)
        return fit, None
    except TimeBridgeError as error:
        return None, error


class HostTimeSelectionShadow:
    """OPT-01 후보 규칙을 **같은 bracket 스트림**에서 병행 평가한다 (판정에 영향 없음).

    baseline이 폐기하는 bracket을 포함해 모든 관측을 받는다. 후보 규칙의 accepted/rejected/reset
    이력과 첫 PASS 시점(관측 index·경과 ns)·fit 요약을 기록해 실물 side-by-side 근거를 남긴다."""

    def __init__(self, *, min_observations: int = 8, min_span_ns: int = 2_000_000_000) -> None:
        self.policy_id = HOST_TIME_SELECTION_CANDIDATE
        self.min_observations = min_observations
        self.min_span_ns = min_span_ns
        self.accepted = []
        self.rejected = []          # {"index", "u_sample_ns", "reason"}
        self.resets = []            # {"index", "reason", "discarded"}
        self.first_pass = None      # {"index", "elapsed_ns", "observations", "span_ns", ...}
        self.fit_errors = 0
        self.observed = 0

    def observe(self, observation, *, index: int, elapsed_ns: int) -> None:
        self.observed += 1
        if observation.u_sample_ns > MAX_BRACKET_NS:
            self.rejected.append({"index": index, "u_sample_ns": observation.u_sample_ns,
                                  "reason": "bracket_wider_than_2ms"})
            return
        if self.accepted and observation.hardware_ns <= self.accepted[-1].hardware_ns:
            self.resets.append({"index": index, "reason": "hardware_time_regression",
                                "discarded": len(self.accepted)})
            self.accepted = []
            return
        self.accepted.append(observation)
        if self.first_pass is not None:
            return
        fit, error = _try_fit(self.accepted, self.min_observations, self.min_span_ns)
        if error is not None:
            self.fit_errors += 1
        if fit is not None:
            self.first_pass = {
                "index": index, "elapsed_ns": int(elapsed_ns),
                "observations": len(self.accepted), "span_ns": fit.span_ns,
                "u_beta_ppm": fit.u_beta_ppm, "d_ppm_num": fit.d_ppm_num,
                "d_ppm_den": fit.d_ppm_den, "max_u_sample_ns": fit.max_u_sample_ns,
                "alpha_num": fit.coefficients.alpha_num, "alpha_den": fit.coefficients.alpha_den,
                "beta_num": fit.coefficients.beta_num, "beta_den": fit.coefficients.beta_den,
            }

    def summary(self, *, baseline_first_pass_index=None, baseline_elapsed_ns=None,
                baseline_restarts=None) -> dict:
        return {
            "kind": "host_time_selection_shadow",
            "policy_id": self.policy_id,
            "active_policy_id": HOST_TIME_SELECTION_BASELINE,
            "activation": "PROPOSED/DISABLED — evaluated in shadow only",
            "observed": self.observed,
            "rejected_brackets": list(self.rejected),
            "resets": list(self.resets),
            "fit_errors_before_pass": self.fit_errors,
            "candidate_first_pass": self.first_pass,
            "baseline_first_pass_index": baseline_first_pass_index,
            "baseline_elapsed_ns": baseline_elapsed_ns,
            "baseline_restarts": list(baseline_restarts or []),
        }


def acquire_host_time_fit(
    worker,
    *,
    min_observations: int = 8,
    min_span_ns: int = 2_000_000_000,
    observation_period_s: float = 1.0,
    max_acquisition_s: float = 600.0,
    restart_log: list | None = None,
    on_observation=None,
    selection_policy: str = HOST_TIME_SELECTION_BASELINE,
    shadow=None,
):
    """§13.2 adaptive acquisition. 실패는 `TimeBridgeError`다.

    `selection_policy=RESTART_SERIES_V1`(기본, active): hardware time 역행·bracket 실패는 표본 선별
    없이 해당 시리즈 전체를 폐기하고, 600초 예산 안이면 빈 시리즈에서 재시작한다 (`0.8.19`).
    `selection_policy=REJECT_BRACKET_KEEP_SERIES_V1`(OPT-01 후보, RF-off/replay 전용): wide bracket만
    reject하고 accepted series는 유지, 역행은 reset. fit 게이트·hard cap·주기는 동일하다.
    `shadow`(HostTimeSelectionShadow)가 주어지면 후보 규칙을 같은 스트림에서 병행 평가한다.
    """

    if selection_policy not in (HOST_TIME_SELECTION_BASELINE, HOST_TIME_SELECTION_CANDIDATE):
        raise TimeBridgeError(f"unknown host-time selection policy: {selection_policy}")
    keep_series = selection_policy == HOST_TIME_SELECTION_CANDIDATE
    observations = []
    started = read_monotonic_raw_ns()
    deadline = started + int(max_acquisition_s * 1e9)
    last_error = None
    index = -1

    def _restart(reason: str):
        nonlocal observations, last_error
        last_error = TimeBridgeError(reason)
        if restart_log is not None:
            restart_log.append({"reason": reason, "discarded": len(observations)})
        observations = []

    while True:
        now = read_monotonic_raw_ns()
        if now > deadline:
            raise TimeBridgeError(
                f"600초 안에 host affine fit이 합격하지 못했습니다: {last_error}"
            )
        observation = collect_bracket(worker)
        index += 1
        if shadow is not None:
            shadow.observe(observation, index=index, elapsed_ns=now - started)
        if observation.u_sample_ns > MAX_BRACKET_NS:
            if keep_series:
                if restart_log is not None:
                    restart_log.append({"reason": "bracket rejected (>2,000,000 ns), series kept",
                                        "discarded": 0})
                last_error = TimeBridgeError("bracket 폭이 2,000,000 ns를 넘습니다")
            else:
                _restart("bracket 폭이 2,000,000 ns를 넘습니다")
            _time.sleep(observation_period_s)
            continue
        if observations and observation.hardware_ns <= observations[-1].hardware_ns:
            _restart("hardware time이 역행했습니다")
            _time.sleep(observation_period_s)
            continue
        observations.append(observation)
        if on_observation is not None:
            on_observation()
        fit, error = _try_fit(observations, min_observations, min_span_ns)
        if fit is not None:
            return fit, tuple(observations)
        if error is not None:
            last_error = error
        _time.sleep(observation_period_s)


def host_affine_fit_summary(
    fit,
    observations,
    *,
    run_id: str,
    endpoint_id: str,
    b210_serial: str,
    radio_generation: int,
    lane_generation: int,
    acquisition_attempt: int,
    provisional_clock_id: str,
) -> dict:
    """§13.2.1 canonical `HostAffineFitSummary` (terminal response body 전용)."""

    artifact = {
        "observations": [
            {
                "m_before_ns": item.m_before_ns,
                "hardware_ns": item.hardware_ns,
                "m_after_ns": item.m_after_ns,
                "m_sample_ns": item.m_sample_ns,
                "u_sample_ns": item.u_sample_ns,
                "u_mid_ns": item.u_mid_ns,
            }
            for item in observations
        ],
        "residuals_ns": list(fit.residuals_ns),
    }
    artifact_sha = hashlib.sha256(
        repr(sorted(artifact["observations"][0].items())).encode("utf-8")
        + repr(artifact["residuals_ns"]).encode("utf-8")
    ).hexdigest()
    d_ppm = fit.d_ppm_exact
    return {
        "summary_type": "HOST_AFFINE_FIT_V1",
        "run_id": run_id,
        "endpoint_id": endpoint_id,
        "b210_serial": b210_serial,
        "radio_generation": radio_generation,
        "lane_generation": lane_generation,
        "acquisition_attempt": acquisition_attempt,
        "provisional_clock_id": provisional_clock_id,
        "h0_ns": fit.coefficients.h0_ns,
        "m0_ns": fit.coefficients.m0_ns,
        "alpha_num": fit.coefficients.alpha_num,
        "alpha_den": fit.coefficients.alpha_den,
        "beta_num": fit.coefficients.beta_num,
        "beta_den": fit.coefficients.beta_den,
        "d_ppm_num": d_ppm.numerator,
        "d_ppm_den": d_ppm.denominator,
        "u_beta_ppm": fit.u_beta_ppm,
        "max_u_sample_ns": fit.max_u_sample_ns,
        "observation_span_ns": fit.span_ns,
        "fit_artifact_sha256": artifact_sha,
        "outcome": "PASS",
    }, artifact


@dataclass(frozen=True, slots=True)
class CfoProbePlanEntry:
    """immutable CFO plan의 directed path 하나 (TX 10회 scheduled hardware time)."""

    path_id: str
    tx_endpoint_id: str
    rx_endpoint_id: str
    tx_lane_index: int
    rx_lane_index: int
    tx_hardware_times_ns: tuple  # 10개, 최소 60초 span


_PROBE_CACHE = {}


def probe_vector_bytes() -> bytes:
    cached = _PROBE_CACHE.get("vector")
    if cached is None:
        cached = generate_probe_vector()
        _PROBE_CACHE["vector"] = cached
    return cached


def probe_vector_sha256() -> str:
    return hashlib.sha256(probe_vector_bytes()).hexdigest()


def transmit_cfo_probe(worker, lane_index: int, tx_hardware_time_ns: int) -> None:
    """canonical probe 1초를 한 lane에 timed 제출한다 (다른 lane inhibit는
    caller/worker gate 소관)."""

    worker.submit_tx_group(((lane_index, probe_vector_bytes(), tx_hardware_time_ns),))


def _wait_for_window_end(worker, window_end_hardware_ns: int) -> None:
    """계획 시각이 미래인 timed 창은 종료 시각 전에는 완료될 수 없다.

    hardware 시계 기준으로 창 종료까지 대기한 뒤에야 timeout 폴링을 시작한다
    (계획이 수십 초 미래일 수 있다 — 벽시계 timeout만 쓰면 창 시작 전에
    포기한다)."""

    while int(worker.hardware_time_ns()) < window_end_hardware_ns:
        _time.sleep(0.05)


def receive_cfo_burst(worker, lane_index: int, rx_hardware_time_ns: int, *, timeout_s=8.0):
    """probe 구간 raw `cf32le` 1초를 수신한다 (finite RX)."""

    worker.submit_rx_lane(lane_index, rx_hardware_time_ns, PROBE_SAMPLES)
    window_end_ns = rx_hardware_time_ns + PROBE_DURATION_NS
    _wait_for_window_end(worker, window_end_ns)
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        result = worker.read_rx_cf32_lane(
            lane_index,
            PROBE_SAMPLES,
            allow_partial=False,
            expected_hardware_time_ns=rx_hardware_time_ns,
        )
        if result is not None:
            segments = result.get("segments", ())
            if len(segments) == 1 and not result.get("overflow_samples"):
                data = segments[0][0]
                if len(data) == PROBE_SAMPLES * 8:
                    return bytes(data), segments[0][1]
            return None, None
        _time.sleep(0.02)
    return None, None


def measure_cfo_path_rx(
    worker,
    entry: CfoProbePlanEntry,
    *,
    tx_to_rx_hardware_offset_ns: int = 0,
    on_burst=None,
):
    """RX 측: 10개 burst를 수신·추정해 `(burst_estimates, t_k)`를 만든다.

    caller가 fit_path로 `B_CFO`를 판정한다. 수신 실패 burst는 측정 실패다.
    """

    estimates = []
    receive_times = []
    raw_hashes = []
    for tx_time in entry.tx_hardware_times_ns:
        rx_time = tx_time + tx_to_rx_hardware_offset_ns
        data, actual_time = receive_cfo_burst(worker, entry.rx_lane_index, rx_time)
        if on_burst is not None:
            on_burst()
        if data is None:
            raise RuntimeError(f"{entry.path_id}: probe burst 수신 실패 (rx={rx_time})")
        raw_hashes.append(hashlib.sha256(data).hexdigest())
        estimates.append(estimate_burst(data))
        receive_times.append(actual_time)
    last = receive_times[-1]
    t_values = [(value - last) / 1e9 for value in receive_times]
    fit = fit_path(estimates, t_values, 120.0)
    return {
        "path_id": entry.path_id,
        "estimates": [
            {"f_k_hex": e.f_k_hz.hex(), "f_k": repr(e.f_k_hz), "u_k_hex": e.u_k_hz.hex(),
             "valid_blocks": e.valid_block_count}
            for e in estimates
        ],
        "raw_iq_sha256": raw_hashes,
        "fit": fit.evidence(),
        "b_cfo_hz": fit.b_cfo_hz,
        "passed": fit.passed,
    }


def link11_calibration_burst_device_bytes() -> bytes:
    """§9.2 Link 11 calibration burst의 device-rate `cf32le` bytes.

    frozen Stage 6 resampler로 native 2,048 sample burst를 8,192 device sample로
    interpolate한다.
    """

    cached = _PROBE_CACHE.get("link11_calibration")
    if cached is not None:
        return cached
    from vdeter.common.identifiers import DeterministicIdSource, LinkId, PhysicalChannel
    from vdeter.common.time import ClockDomain, TimePoint
    from vdeter.common.contracts.phy import PhyEncodeRequest
    from vdeter.phy import resampling
    from vdeter.phy.burst.rrc import from_cf32le, to_cf32le
    from vdeter.phy.transmitter import Link11Phy
    from vdeter.profiles.link_phy import build_combined_phy_profile_registry

    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok

    class _StaticTime:
        def now_utc(self):
            return TimePoint(2_000_000_000, 0, ClockDomain.UTC, "stage7-cal-utc")

        def now_monotonic(self):
            return TimePoint(10, 0, ClockDomain.MONOTONIC, "stage7-cal-mono")

    ids = DeterministicIdSource(0xCA1)
    phy = Link11Phy(registry, _StaticTime(), ids, sample_format="cf32le")
    # frozen Link packet builder로 CRC 정합 canonical 54B packet을 만든다.
    packet = _link11_canonical_packet()
    encoded = phy.encode_packet(
        PhyEncodeRequest(
            packet_id=ids.new_packet_id(),
            link_id=LinkId(11),
            channel=PhysicalChannel("VDETER_LOWER_100_TEST"),
            transmit_at=TimePoint(2_000_000_100, 0, ClockDomain.UTC, "stage7-cal-utc"),
            packet=packet,
        )
    )
    assert encoded.is_ok, encoded.error
    native = encoded.value.samples
    device_values = resampling.interpolate_slot(from_cf32le(native.data))
    cached = to_cf32le(device_values)
    _PROBE_CACHE["link11_calibration"] = cached
    _PROBE_CACHE["link11_calibration_packet"] = packet
    return cached


def link11_calibration_packet_sha256() -> str:
    """calibration packet(54B)의 SHA-256 — §13.3 decode 결속 검증용."""

    link11_calibration_burst_device_bytes()
    return hashlib.sha256(_PROBE_CACHE["link11_calibration_packet"]).hexdigest()


def capture_slot_window(worker, lane_index: int, rx_hardware_time_ns: int, *, timeout_s=8.0):
    """§13.3 calibration용 정확히 한 device slot(8,192 sample) 창을 수신한다.

    반환: (raw_cf32le_bytes|None, actual_first_sample_hardware_ns|None).
    """

    from vdeter.phy import resampling

    count = resampling.DEVICE_SLOT_SAMPLES
    window_end_ns = rx_hardware_time_ns + (
        count * 1_000_000_000 + resampling.DEVICE_SAMPLE_RATE_HZ - 1
    ) // resampling.DEVICE_SAMPLE_RATE_HZ
    if int(worker.hardware_time_ns()) >= rx_hardware_time_ns:
        # 창 시작이 이미 지났다 — timed stream command가 소급 적용될 수 없으므로
        # 제출하지 않고 즉시 실패를 반환한다 (긴 폴링으로 뒤처짐을 증폭하지
        # 않는다).
        return None, None
    worker.submit_rx_lane(lane_index, rx_hardware_time_ns, count)
    _wait_for_window_end(worker, window_end_ns)
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        result = worker.read_rx_cf32_lane(
            lane_index,
            count,
            allow_partial=False,
            expected_hardware_time_ns=rx_hardware_time_ns,
        )
        if result is not None:
            segments = result.get("segments", ())
            if len(segments) == 1 and not result.get("overflow_samples"):
                data = segments[0][0]
                if len(data) == count * 8:
                    return bytes(data), segments[0][1]
            return None, None
        _time.sleep(0.02)
    return None, None


def capture_step_window(
    worker, lane_index: int, start_hardware_ns: int, sample_count: int, *, timeout_s=20.0
):
    """§9.2 step 전체를 하나의 연속 finite 창으로 수신한다.

    반환: (raw_cf32le|None, actual_first_sample_hardware_ns|None). per-burst RPC
    왕복 없이 step당 2회 RPC로 끝나 스케줄 뒤처짐이 없다.
    """

    if int(worker.hardware_time_ns()) >= start_hardware_ns:
        return None, None
    worker.submit_rx_lane(lane_index, start_hardware_ns, sample_count)
    window_end_ns = start_hardware_ns + (
        sample_count * 1_000_000_000 + DEVICE_RATE - 1
    ) // DEVICE_RATE
    _wait_for_window_end(worker, window_end_ns)
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        result = worker.read_rx_cf32_lane(
            lane_index,
            sample_count,
            allow_partial=False,
            expected_hardware_time_ns=start_hardware_ns,
        )
        if result is not None:
            segments = result.get("segments", ())
            if len(segments) == 1 and not result.get("overflow_samples"):
                data = segments[0][0]
                if len(data) == sample_count * 8:
                    return bytes(data), segments[0][1]
            return None, None
        _time.sleep(0.05)
    return None, None


def _link11_canonical_packet() -> bytes:
    """Link 11 54-byte canonical calibration packet (CRC 정합)."""

    from vdeter.link.packet.builder import build_packet
    from vdeter.link.messages.builtins import build_message_registry
    from vdeter.profiles.link_phy import build_link_profile_registry
    from vdeter.profiles.models import RegistryScope
    from vdeter.common.contracts.message import MediaAccessControlBody
    from vdeter.common.identifiers import StationId

    message_registry = build_message_registry(RegistryScope.OPERATIONAL, "stage7-cal").value
    link_registry = build_link_profile_registry(RegistryScope.OPERATIONAL, "stage7-cal").value
    for registry in (message_registry, link_registry):
        frozen = registry.freeze()
        assert frozen.is_ok
    body = MediaAccessControlBody(
        source_id=StationId(222222222),
        media_access_priority_level=0,
        random_access_selection_interval=235,
        short_data_message_limit=4,
        system_status=0,
    )
    built = build_packet(
        __import__("vdeter.common.identifiers", fromlist=["LinkId"]).LinkId(11),
        (body,),
        message_registry,
        link_registry,
    )
    assert built.is_ok, built.error
    return built.value.packet


def measure_gain_step_metrics(raw_cf32le: bytes) -> dict:
    """§9.2 accepted samples의 peak/RMS/clipping 측정 (binary64)."""

    count = len(raw_cf32le) // 8
    values = struct.unpack(f"<{count * 2}f", raw_cf32le)
    peak = 0.0
    clipping = 0
    total = 0.0
    for index in range(count):
        real = values[2 * index]
        imag = values[2 * index + 1]
        if abs(real) >= 1.0 or abs(imag) >= 1.0:
            clipping += 1
        magnitude = math.hypot(real, imag)
        if magnitude > peak:
            peak = magnitude
        total += magnitude * magnitude
    rms = math.sqrt(total / count) if count else 0.0
    return {
        "peak_dbfs": (20.0 * math.log10(peak)) if peak > 0 else float("-inf"),
        "rms_dbfs": (20.0 * math.log10(rms)) if rms > 0 else float("-inf"),
        "clipping_samples": clipping,
        "sample_count": count,
    }


def measure_gain_step_metrics_fast(raw_cf32le: bytes) -> dict:
    """`measure_gain_step_metrics`와 동일 정의의 numpy binary64 구현.

    §9.2 step당 100 슬롯 판정이 캡처 일정과 경쟁하지 않도록 빠른 경로를
    제공한다 (수치 정의는 동일: peak/RMS dBFS, clipping |re|/|im|≥1.0)."""

    import numpy

    values = numpy.frombuffer(raw_cf32le, dtype=numpy.dtype("<c8")).astype(
        numpy.complex128
    )
    clipping = int(
        ((numpy.abs(values.real) >= 1.0) | (numpy.abs(values.imag) >= 1.0)).sum()
    )
    magnitude = numpy.abs(values)
    peak = float(magnitude.max()) if values.size else 0.0
    rms = float(numpy.sqrt(numpy.mean(magnitude ** 2))) if values.size else 0.0
    return {
        "peak_dbfs": (20.0 * math.log10(peak)) if peak > 0 else float("-inf"),
        "rms_dbfs": (20.0 * math.log10(rms)) if rms > 0 else float("-inf"),
        "clipping_samples": clipping,
        "sample_count": int(values.size),
    }


def gain_step_acceptable(metrics: dict) -> bool:
    """§9.2 v0.8.20: peak ≤ -3 dBFS, RMS -32..-12 dBFS, clipping 0."""

    return (
        metrics["clipping_samples"] == 0
        and metrics["peak_dbfs"] <= -3.0
        and -32.0 <= metrics["rms_dbfs"] <= -12.0
    )


__all__ = (
    "CfoProbePlanEntry",
    "acquire_host_time_fit",
    "collect_bracket",
    "collect_utc_anchor",
    "gain_step_acceptable",
    "host_affine_fit_summary",
    "link11_calibration_burst_device_bytes",
    "measure_cfo_path_rx",
    "measure_gain_step_metrics",
    "probe_vector_bytes",
    "probe_vector_sha256",
    "read_monotonic_raw_ns",
    "read_realtime_ns",
    "receive_cfo_burst",
    "transmit_cfo_probe",
)
