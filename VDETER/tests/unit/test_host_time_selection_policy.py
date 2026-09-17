"""OPT-01 host-time bracket 선택 규칙 — RF-off replay / synthetic adversarial 시험 (spec §13.3).

증명하는 것
  동등성   : wide bracket·역행이 없는 스트림에서 baseline과 후보는 같은 관측 집합·같은 fit 계수를 낸다
  가용성   : wide bracket 하나가 섞이면 baseline은 시리즈를 폐기·재시작하지만 후보는 그 bracket만 reject하고
             accepted 유지 → 더 이른 PASS; rejected/accepted 이력·사유가 모두 기록된다
  false-accept: (a) wide bracket 뒤에 시계 관계가 바뀐(offset step) 표본이 이어지면 후보도 residual/u_beta
             gate에서 거부한다(baseline과 동일하게 실패, 더 관대하지 않다); (b) hardware 역행은 두 규칙 모두
             series reset; (c) 게이트 상수(8개·2 s·1 ppm·25 ppm·1 ns·600 s)는 후보에서도 그대로다
  shadow   : production 경로는 baseline을 호출하며 shadow는 같은 스트림에서 후보를 병행 평가만 한다
             (반환 fit은 baseline의 것); summary에 첫 PASS 시점과 baseline restart 이력이 남는다
"""

from __future__ import annotations

import inspect
from fractions import Fraction

import pytest

from vdeter.b210_system import hw_operations as hw
from vdeter.b210_system.time_bridge import (
    ABSOLUTE_RATE_LIMIT_PPM,
    MAX_ACQUISITION_NS,
    MAX_BRACKET_NS,
    MIN_OBSERVATIONS,
    MIN_SPAN_NS,
    ROUND_TRIP_LIMIT_NS,
    U_BETA_LIMIT_PPM,
    TimeBridgeError,
)


class StreamWorker:
    """미리 정의한 bracket 스트림을 순서대로 돌려주는 worker double (RPC 없음)."""

    def __init__(self, brackets):
        self._brackets = list(brackets)
        self.calls = 0

    def bracket_hardware_time(self):
        if not self._brackets:
            raise AssertionError("stream exhausted")
        self.calls += 1
        return self._brackets.pop(0)


def clean_stream(count=60, *, start_host=1_000_000_000_000, start_hw=5_000_000_000_000,
                 period_ns=1_000_000_000, d_ppm=Fraction(-65, 10), width_ns=10_000):
    """d_ppm 드리프트의 정직한 affine 시계 쌍 (bracket 폭 10 µs → u_beta ≤ 1 ppm에 약 15표본).

    실물 bracket(0.2~2 ms)은 약 300 s가 필요하지만 규칙 비교에는 폭 크기가 무관하다 — 게이트 상수는
    그대로이며 폭이 좁을수록 같은 규칙이 더 빨리 수렴할 뿐이다."""
    rows = []
    for k in range(count):
        m = start_host + k * period_ns
        hw_ns = start_hw + int(Fraction(k * period_ns) * (1 - d_ppm / 1_000_000))
        rows.append((m - width_ns // 2, hw_ns, m + width_ns // 2))
    return rows


class FakeClock:
    def __init__(self, brackets):
        self.brackets = brackets
        self.i = 0

    def now(self):
        # read_monotonic_raw_ns는 collect 직전에 한 번, 시작 시 한 번 호출된다 — bracket의 m_before를 따른다.
        idx = min(self.i, len(self.brackets) - 1)
        return self.brackets[idx][0]


@pytest.fixture
def clock(monkeypatch):
    holder = {}

    def install(brackets):
        fake = FakeClock(brackets)

        def raw():
            value = fake.now()
            return value

        monkeypatch.setattr(hw, "read_monotonic_raw_ns", raw)
        monkeypatch.setattr(hw._time, "sleep", lambda _s: fake.__setattr__("i", fake.i + 1))
        holder["clock"] = fake
        return fake

    return install


def run_policy(brackets, policy, clock, shadow=None):
    clock(brackets)
    worker = StreamWorker(brackets)
    log = []
    fit, observations = hw.acquire_host_time_fit(worker, restart_log=log, selection_policy=policy,
                                                 shadow=shadow)
    return fit, observations, log, worker


def test_constants_are_the_frozen_gate_values():
    assert MIN_OBSERVATIONS == 8 and MIN_SPAN_NS == 2_000_000_000
    assert MAX_BRACKET_NS == 2_000_000 and U_BETA_LIMIT_PPM == 1 and ABSOLUTE_RATE_LIMIT_PPM == 25
    assert ROUND_TRIP_LIMIT_NS == 1 and MAX_ACQUISITION_NS == 600_000_000_000
    source = inspect.getsource(hw.acquire_host_time_fit)
    assert "max_acquisition_s: float = 600.0" in source
    assert hw.HOST_TIME_SELECTION_BASELINE == "RESTART_SERIES_V1"
    assert hw.HOST_TIME_SELECTION_CANDIDATE == "REJECT_BRACKET_KEEP_SERIES_V1"


def test_clean_stream_equivalence(clock):
    stream = clean_stream(40)
    base_fit, base_obs, base_log, _ = run_policy(list(stream), hw.HOST_TIME_SELECTION_BASELINE, clock)
    cand_fit, cand_obs, cand_log, _ = run_policy(list(stream), hw.HOST_TIME_SELECTION_CANDIDATE, clock)
    assert base_log == [] and cand_log == []
    assert len(base_obs) == len(cand_obs)
    assert base_fit.coefficients == cand_fit.coefficients
    assert (base_fit.u_beta_ppm, base_fit.d_ppm_num, base_fit.d_ppm_den) == (
        cand_fit.u_beta_ppm, cand_fit.d_ppm_num, cand_fit.d_ppm_den)


def test_wide_bracket_baseline_restarts_candidate_keeps_series(clock):
    stream = clean_stream(60)
    wide = list(stream[10])
    wide = (wide[0], wide[1], wide[0] + 3_000_000)          # 3 ms bracket at index 10 (PASS 전)
    stream[10] = wide
    base_fit, base_obs, base_log, base_worker = run_policy(list(stream), hw.HOST_TIME_SELECTION_BASELINE, clock)
    cand_fit, cand_obs, cand_log, cand_worker = run_policy(list(stream), hw.HOST_TIME_SELECTION_CANDIDATE, clock)
    assert base_log == [{"reason": "bracket 폭이 2,000,000 ns를 넘습니다", "discarded": 10}]
    assert cand_log == [{"reason": "bracket rejected (>2,000,000 ns), series kept", "discarded": 0}]
    # 후보는 accepted 20개를 유지해 wide bracket 직후 표본으로 PASS 가능 → 더 적은 bracket 소비.
    assert cand_worker.calls < base_worker.calls
    assert all(o.u_sample_ns <= MAX_BRACKET_NS for o in cand_obs)
    assert all(o.u_sample_ns <= MAX_BRACKET_NS for o in base_obs)
    # 두 fit 모두 같은 드리프트를 복원한다 (동일 시계 관계).
    assert abs(Fraction(base_fit.d_ppm_num, base_fit.d_ppm_den)
               - Fraction(cand_fit.d_ppm_num, cand_fit.d_ppm_den)) < Fraction(1, 10)


def test_regime_change_hidden_behind_wide_bracket_never_yields_a_mixed_fit(clock):
    """wide bracket 뒤에 host 시계가 +5 ms 점프한 표본이 이어진다.

    후보는 accepted(점프 전)를 유지하므로 섞인 시리즈가 되고, fit_affine의 residual gate(≤ u_mid)가
    그것을 **끝까지 거부**한다 — 섞인 시계 관계를 수용하는 false accept는 없다(스트림 소진까지 PASS 없음).
    baseline은 시리즈를 폐기하고 점프 뒤의 자기 일관된 표본만으로 PASS한다. 즉 후보의 비용은
    false-accept 증가가 아니라 이런 경우의 **가용성(false-reject) 비용**이며, 이는 활성화 판정 record에
    남긴다."""
    stream = clean_stream(40)
    wide = list(stream[12])
    stream[12] = (wide[0], wide[1], wide[0] + 4_000_000)
    for k in range(13, 40):
        m_before, hw_ns, m_after = stream[k]
        stream[k] = (m_before + 5_000_000, hw_ns, m_after + 5_000_000)   # host offset step
    with pytest.raises(AssertionError, match="stream exhausted"):
        run_policy(list(stream), hw.HOST_TIME_SELECTION_CANDIDATE, clock)
    base_fit, base_obs, base_log, _ = run_policy(list(stream), hw.HOST_TIME_SELECTION_BASELINE, clock)
    assert base_log[0]["discarded"] == 12
    assert all(o.m_before_ns >= stream[13][0] for o in base_obs)   # 점프 뒤 표본만으로 fit
    # 후보가 수용한 fit이 baseline이 거부한 관계를 담는 경우는 없다(정의상 후보 PASS ⊆ fit gate PASS).


def test_hardware_regression_resets_series_in_both_policies(clock):
    stream = clean_stream(40)
    m_before, hw_ns, m_after = stream[10]
    stream[10] = (m_before, stream[9][1] - 1_000, m_after)   # hardware time goes backwards
    _, _, base_log, _ = run_policy(list(stream), hw.HOST_TIME_SELECTION_BASELINE, clock)
    _, _, cand_log, _ = run_policy(list(stream), hw.HOST_TIME_SELECTION_CANDIDATE, clock)
    assert base_log[0]["reason"].startswith("hardware time") and base_log[0]["discarded"] == 10
    assert cand_log[0]["reason"].startswith("hardware time") and cand_log[0]["discarded"] == 10


def test_unknown_policy_is_refused(clock):
    clock(clean_stream(10))
    with pytest.raises(TimeBridgeError):
        hw.acquire_host_time_fit(StreamWorker(clean_stream(10)), selection_policy="ANYTHING_GOES")


def test_shadow_runs_candidate_alongside_baseline_without_changing_the_result(clock):
    stream = clean_stream(60)
    wide = list(stream[10])
    stream[10] = (wide[0], wide[1], wide[0] + 3_000_000)
    shadow = hw.HostTimeSelectionShadow()
    fit, observations, log, worker = run_policy(list(stream), hw.HOST_TIME_SELECTION_BASELINE, clock,
                                                shadow=shadow)
    # active 결과는 baseline 그대로 (restart 1회, wide 뒤의 시리즈로 PASS).
    assert log and log[0]["discarded"] == 10
    summary = shadow.summary(baseline_first_pass_index=worker.calls - 1,
                             baseline_elapsed_ns=0, baseline_restarts=log)
    assert summary["policy_id"] == hw.HOST_TIME_SELECTION_CANDIDATE
    assert summary["active_policy_id"] == hw.HOST_TIME_SELECTION_BASELINE
    assert summary["rejected_brackets"] == [{"index": 10, "u_sample_ns": 3_000_000,
                                             "reason": "bracket_wider_than_2ms"}]
    assert summary["resets"] == []
    assert summary["candidate_first_pass"]["index"] < summary["baseline_first_pass_index"]
    assert summary["candidate_first_pass"]["observations"] >= MIN_OBSERVATIONS
    assert summary["baseline_restarts"] == log
    assert summary["activation"].startswith("PROPOSED/DISABLED")


def test_production_acquire_host_time_calls_baseline_and_records_shadow():
    from vdeter.b210_system import pi_runtime
    source = inspect.getsource(pi_runtime.PiOperations.acquire_host_time)
    assert "selection_policy" not in source            # active 경로 = baseline 기본값
    assert "HostTimeSelectionShadow()" in source and "shadow=shadow" in source
    assert "shadow.summary(" in source
