"""RX energy gate(`Stage7LaneRxDispatcher._energy_gate`)의 계약.

이 파일이 생기기 전까지 게이트를 덮는 시험은 **하나도 없었다**. 실장비 stage B
(diag-20260913-195703)에서 MASS-A lane 0이 잡음 바닥을 신호 전력으로 학습해
(`noise_p10=2.848e-03`, 정상 회차 `2.9e-06`) 이후 모든 창을 차단했고, TBB를
획득하지 못해 publish deadline으로 세션이 실패했다. 그 실패를 RF 없이 재현한다.

고정하려는 계약:
  - 조용한 warm-up에서는 잡음 바닥을 학습하고 버스트는 통과시킨다
  - warm-up에 버스트가 섞여도 잡음 바닥을 학습한다
  - **warm-up이 버스트 위주여도 버스트 전력을 잡음 바닥으로 학습하지 않는다**
  - 무장 뒤에 모든 창을 영구 차단하는 상태로 고착되지 않는다
  - 비정상 입력에서 죽지 않고, 게이트를 거치지 않은 원본으로 통과시킨다(fail-open)
  - lane별 상태가 서로 섞이지 않는다
  - warm-up 표본 수집이 유계다

게이트는 성능 장치다(잡음 창을 0-치환해 상관 풀 탐색 106 ms를 피한다). 안전
장치가 아니므로 전제가 깨졌을 때의 올바른 실패 방향은 **통과**(느려짐)이지
**전면 차단**(기능 정지)이 아니다.

주의: 소프트웨어 하니스는 `tests/support/stage7.py`에서 `STAGE7_RX_ENERGY_GATE=0`
으로 게이트를 끈다. 이 시험은 명시적으로 "1"로 세우고 끝나면 되돌린다 — 그러지
않으면 게이트를 거치지도 않고 통과한 것을 "정상"으로 오독한다.
"""

from __future__ import annotations

import math
import os
import random
import unittest

import numpy as np

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT
from vdeter.common.contracts.phy import PhyDecodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import LinkId
from vdeter.b210_system import rx_decode_worker as W
from vdeter.b210_system.support import Stage7LaneRxDispatcher

#: Link 11 native 창 = 2,048 복소 표본(= 8,192 device 표본을 4배 데시메이션).
NATIVE_SAMPLES = 2048

#: 실측값(diag-20260913-192523 MASS-A lane 0). 잡음 바닥과 C1 버스트 전력.
NOISE_POWER = 2.9e-06
BURST_POWER = 2.85e-03          # 잡음 대비 약 +29.9 dB


def _request(power: float, *, count: int = NATIVE_SAMPLES) -> PhyDecodeRequest:
    """평균 전력이 정확히 `power`인 native 복호 요청."""

    amplitude = math.sqrt(power)
    arr = np.full(count, complex(amplitude, 0.0), dtype=np.complex64)
    data = arr.tobytes()
    block = SampleBlock(
        sample_format=SAMPLE_FORMAT_CF32LE,
        sample_rate_hz=76_800,
        sample_count=count,
        data=data,
        first_sample_at=FIRST_SAMPLE_AT,
        continuity=SampleContinuity.CONTIGUOUS,
    )
    return PhyDecodeRequest(CHANNEL, block, (LinkId(11),))


def _gate(host, request, lane: int = 0):
    return Stage7LaneRxDispatcher._energy_gate(host, lane, request)


def _is_zeroed(result) -> bool:
    data = result.samples.data
    return len(data) > 0 and not any(data)


def _armed_floor(host, lane: int = 0):
    state = host._gate_state.get(lane)
    return None if state is None else state["med"]


class EnergyGateContract(unittest.TestCase):

    def setUp(self) -> None:
        self._prior = os.environ.get("STAGE7_RX_ENERGY_GATE")
        os.environ["STAGE7_RX_ENERGY_GATE"] = "1"
        self.host = W._GateHost()

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop("STAGE7_RX_ENERGY_GATE", None)
        else:
            os.environ["STAGE7_RX_ENERGY_GATE"] = self._prior

    # --- warm-up ---------------------------------------------------------

    def _feed(self, powers, lane: int = 0):
        """전력 목록을 순서대로 게이트에 넣고 (0-치환 여부) 목록을 돌려준다."""

        return [_is_zeroed(_gate(self.host, _request(p), lane)) for p in powers]

    def test_quiet_warmup_learns_noise_floor_and_passes_bursts(self):
        """조용한 warm-up: 잡음 바닥을 학습하고 버스트는 통과한다."""

        self._feed([NOISE_POWER] * 60)
        floor = _armed_floor(self.host)
        self.assertIsNotNone(floor, "warm-up 60창 뒤에는 무장해야 한다")
        self.assertAlmostEqual(floor, NOISE_POWER, delta=NOISE_POWER * 0.5)
        self.assertFalse(_is_zeroed(_gate(self.host, _request(BURST_POWER))),
                         "버스트는 통과해야 한다")
        self.assertTrue(_is_zeroed(_gate(self.host, _request(NOISE_POWER))),
                        "잡음 창은 0-치환되어야 한다(게이트의 목적)")

    def test_warmup_with_some_bursts_still_learns_noise_floor(self):
        """버스트가 20% 섞여도 잡음 바닥을 학습한다(설계상 허용 오염)."""

        powers = [BURST_POWER if i % 5 == 0 else NOISE_POWER for i in range(60)]
        self._feed(powers)
        floor = _armed_floor(self.host)
        self.assertIsNotNone(floor)
        self.assertLess(floor, BURST_POWER / 10,
                        "20%% 오염에서 버스트 전력을 바닥으로 학습하면 안 된다 (학습값 %r)" % floor)

    def test_burst_dominated_warmup_does_not_learn_burst_power_as_floor(self):
        """**실장비 stage B 재현** — warm-up이 버스트 위주일 때.

        TBB 획득 전 MASS endpoint는 프레임(2.4 s)당 RX 창을 사실상 1개만 계획하고
        (bootstrap slot map = BBSC 1슬롯), 그동안 ROC는 BBSC 기회마다 TBB fragment를
        보낸다. 따라서 warm-up 표본이 버스트 위주가 되는 것은 예외가 아니라
        정상 상태다. 그때 바닥을 버스트 전력으로 학습하면 이후 모든 창이 차단된다.
        """

        # 6창만 잡음, 54창은 버스트 — 실측 p10=2.848e-03과 같은 구성.
        powers = [NOISE_POWER if i % 10 == 0 else BURST_POWER for i in range(60)]
        self._feed(powers)
        floor = _armed_floor(self.host)
        self.assertIsNotNone(floor)
        self.assertLess(
            floor, BURST_POWER / 10,
            "버스트 위주 warm-up에서 바닥을 버스트 전력으로 학습했다 (학습값 %r) — "
            "이후 모든 창이 차단되어 TBB를 획득할 수 없다" % floor)

    def test_armed_gate_does_not_lock_out_every_window(self):
        """무장 뒤에 버스트조차 전부 차단되는 고착 상태가 되면 안 된다."""

        powers = [NOISE_POWER if i % 10 == 0 else BURST_POWER for i in range(60)]
        self._feed(powers)
        after = self._feed([BURST_POWER] * 50)
        self.assertFalse(all(after),
                         "무장 뒤 버스트 50창이 전부 차단됐다 — 세션 내 회복 경로가 없다")

    def test_warmup_sample_collection_is_bounded(self):
        """warm-up 표본은 유계다 — 무한히 모으지 않는다."""

        self._feed([NOISE_POWER] * 200)
        state = self.host._gate_state[0]
        self.assertIsNotNone(state["med"], "200창을 넣으면 이미 무장했어야 한다")
        self.assertLessEqual(len(state["warm"]), 60,
                             "무장 뒤에도 warm 표본이 계속 쌓이면 메모리가 증가한다")

    def test_zero_power_windows_do_not_feed_the_estimate(self):
        """전력 0 창은 표본에 들어가지 않는다(이미 무음이라 학습 정보가 없다)."""

        self._feed([0.0] * 30)
        self.assertIsNone(_armed_floor(self.host),
                          "전력 0 창만으로 무장하면 안 된다")

    # --- 비정상 입력 ------------------------------------------------------

    def test_empty_samples_pass_through_unchanged(self):
        request = _request(NOISE_POWER, count=0)
        result = _gate(self.host, request)
        self.assertIs(result, request, "빈 표본은 원본 그대로 통과해야 한다")

    def test_non_finite_samples_do_not_crash_and_fail_open(self):
        """NaN/Inf가 섞여도 죽지 않고, 잘못된 바닥을 학습해 차단하지 않는다."""

        arr = np.full(NATIVE_SAMPLES, complex(float("nan"), 0.0), dtype=np.complex64)
        block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE, sample_rate_hz=76_800,
            sample_count=NATIVE_SAMPLES, data=arr.tobytes(),
            first_sample_at=FIRST_SAMPLE_AT, continuity=SampleContinuity.CONTIGUOUS)
        result = _gate(self.host, PhyDecodeRequest(CHANNEL, block, (LinkId(11),)))
        self.assertFalse(_is_zeroed(result),
                         "판정할 수 없는 입력을 0-치환하면 복호 기회를 잃는다")

    def test_disabled_by_env_returns_original(self):
        os.environ["STAGE7_RX_ENERGY_GATE"] = "0"
        request = _request(NOISE_POWER)
        self.assertIs(_gate(self.host, request), request)

    # --- lane 격리·재진입 -------------------------------------------------

    def test_lane_states_are_independent(self):
        self._feed([NOISE_POWER] * 60, lane=0)
        self.assertIsNotNone(_armed_floor(self.host, 0))
        self.assertIsNone(_armed_floor(self.host, 1),
                          "lane 0의 학습이 lane 1로 새면 안 된다")
        self._feed([BURST_POWER] * 5, lane=1)
        self.assertIsNone(_armed_floor(self.host, 1))

    def test_repeated_calls_after_arming_are_stable(self):
        """같은 전력을 반복해도 판정이 흔들리지 않는다(결정성)."""

        self._feed([NOISE_POWER] * 60)
        first = [_is_zeroed(_gate(self.host, _request(BURST_POWER))) for _ in range(10)]
        second = [_is_zeroed(_gate(self.host, _request(NOISE_POWER))) for _ in range(10)]
        self.assertEqual(first, [False] * 10)
        self.assertEqual(second, [True] * 10)

    # --- 전제 검사·유계 재무장 --------------------------------------------

    def test_clean_warmup_still_arms_on_percentile_not_minimum(self):
        """전제가 성립하면 기존 추정(10분위수)을 그대로 쓴다 — 수정이 정상 경로를
        바꾸지 않는다는 negative 확인."""

        # 최솟값과 10분위수가 서로 다르도록 퍼진 잡음 분포를 쓴다.
        powers = [NOISE_POWER * (1.0 + i / 100.0) for i in range(60)]
        self._feed(powers)
        floor = _armed_floor(self.host)
        expected_p10 = sorted(powers)[60 // 10]
        # 게이트는 complex64 표본을 `np.vdot`으로 합산한다. float32 자체 정밀도는
        # ~1e-7이지만 2,048항 누적 오차가 더해져 실측 상대오차는 ~1e-5 수준이다
        # (1e-5 허용에서 실패: 3.0739474823349155e-06 vs 3.0740000000000006e-06).
        # 이보다 촘촘한 허용오차는 계약이 아니라 부동소수 누적을 시험하는 것이다.
        # 이 시험이 실제로 가르려는 두 값(최솟값 2.900e-06 vs 10분위수 3.074e-06)은
        # 6% 떨어져 있으므로 1e-3 허용으로도 구분에 전혀 지장이 없다.
        self.assertAlmostEqual(floor / expected_p10, 1.0, delta=1e-3)
        # 그리고 최솟값이 아니라 10분위수를 골랐음을 분리해서 확인한다
        # (두 값은 이 분포에서 6% 떨어져 있으므로 위 허용오차와 혼동되지 않는다).
        self.assertGreater(floor, min(powers) * 1.01,
                           "전제가 성립하는데도 상계(최솟값)로 무장했다")

    def test_premise_failure_arms_on_observed_minimum(self):
        """버스트 위주 표본이면 상계(최솟값)로 무장한다 — 문턱을 넓히는 것이
        아니라 워밍업이 이미 쓰던 임시 문턱과 같은 값을 유지하는 것이다."""

        powers = [NOISE_POWER if i % 10 == 0 else BURST_POWER for i in range(60)]
        self._feed(powers)
        self.assertAlmostEqual(_armed_floor(self.host), NOISE_POWER,
                               delta=NOISE_POWER * 0.5)

    def test_all_burst_warmup_is_not_trusted_and_keeps_a_bounded_probe(self):
        """조건 3: warm-up 60창이 **전부 버스트**인 경우.

        최솟값마저 신호 전력이므로 전제 검사로는 걸러지지 않는다. 그때는 학습값을
        **신뢰하지 않는다**고 기록하고(`floor_trusted=False`), 그 동안만 유계
        probe를 열어 전면 차단을 막는다. 무장 자체를 영구 보류하면 워밍업 문턱이
        같은 이유로 버스트를 막으므로 그것도 해법이 아니다.
        """

        self._feed([BURST_POWER] * 60)
        state = self.host._gate_state[0]
        self.assertIsNotNone(state["med"], "60창 뒤에는 무장해야 한다")
        self.assertFalse(state["floor_trusted"],
                         "단일 모드 표본을 잡음 근거로 신뢰하면 안 된다")

        out = self._feed([BURST_POWER] * 160)
        passed = out.count(False)
        self.assertGreater(passed, 0,
                           "전면 차단으로 고착되면 TBB를 획득할 수 없다")
        self.assertLess(passed, len(out) // 4,
                        "probe는 유계여야 한다 — 무조건 통과가 아니다")
        self.assertEqual(state["probes"], passed,
                         "통과분은 전부 probe로 계수되어야 한다")

    def test_contamination_only_after_arming_never_locks_out(self):
        """조건 5: 무장 뒤에도 오염된 표본만 계속 들어오는 경우."""

        self._feed([BURST_POWER] * 60)
        state = self.host._gate_state[0]
        for _ in range(5):
            out = self._feed([BURST_POWER] * 64)
            self.assertIn(False, out,
                          "오염이 계속돼도 전면 차단 상태가 되면 안 된다")
        self.assertFalse(state["floor_trusted"],
                         "잡음 근거가 없는데 신뢰로 바뀌면 안 된다")
        self.assertEqual(state["corrections"], 0,
                         "낮은 창이 온 적 없으므로 보정도 없어야 한다")

    def test_quiet_window_after_arming_corrects_the_floor_downward(self):
        """조건 2의 핵심: 학습한 바닥보다 확실히 낮은 창이 **실제로 오면** 정정한다.

        같은 오염 표본을 다시 모으는 것이 아니라 새 관측을 근거로 삼는다.
        """

        self._feed([BURST_POWER] * 60)
        state = self.host._gate_state[0]
        self.assertAlmostEqual(state["med"] / BURST_POWER, 1.0, delta=1e-3)

        self._feed([NOISE_POWER, NOISE_POWER])        # 2표본 확인
        self.assertLess(state["med"], BURST_POWER / 100.0,
                        "낮은 창이 왔는데 바닥이 그대로면 정정이 없는 것이다")
        self.assertTrue(state["floor_trusted"],
                        "실제 저전력 관측은 잡음 근거다")
        self.assertEqual(state["corrections"], 1)

        before = state["probes"]
        out = self._feed([BURST_POWER] * 32)
        self.assertNotIn(True, out, "정정 뒤 버스트는 전부 통과해야 한다")
        self.assertEqual(state["probes"], before,
                         "근거를 확보했으면 probe는 꺼져야 한다")

    def test_single_low_outlier_does_not_crater_the_floor(self):
        """단발 dropout 한 창이 바닥을 무너뜨리면 잡음 창이 전량 통과해 부하가
        폭증한다(게이트 도입 이유 자체가 무너진다).

        보존 evidence 18건 실측에서 잡음 창끼리는 p50/p10 1.01~1.06으로 6% 안쪽이다.
        따라서 두 저전력 관측이 **서로 1.5배 안**일 때만 같은 집단으로 인정한다.
        """

        self._feed([NOISE_POWER] * 60)
        state = self.host._gate_state[0]
        floor = state["med"]

        self._feed([NOISE_POWER * 1e-6])              # 단발 이상치(dropout 모사)
        self.assertAlmostEqual(state["med"], floor, delta=floor * 1e-3,
                               msg="1표본만으로 정정하면 안 된다")

        self._feed([NOISE_POWER / 100.0])             # 이상치와 10,000배 차 — 다른 집단
        self.assertAlmostEqual(
            state["med"], floor, delta=floor * 1e-3,
            msg="집단이 다른 두 표본으로 정정하면 이상치가 바닥을 끌어내린다")

        self._feed([NOISE_POWER / 100.0])             # 같은 집단 2회 -> 정정
        self.assertAlmostEqual(state["med"] / (NOISE_POWER / 100.0), 1.0,
                               delta=1e-3,
                               msg="같은 집단 2표본이면 정정해야 한다")
        self.assertEqual(state["corrections"], 1)

    def test_quiet_lane_probe_cost_is_bounded_and_documented(self):
        """조용한 lane은 표본만으로 '전부 버스트'와 구분되지 않아 미신뢰로 남는다.

        구분 불가능성에서 오는 불가피한 비용이며, probe 비율이 유계임을 고정한다.
        게이트 전 106 ms/창, 게이트 후 4.9 ms/창, 1/16 probe면 평균 11.2 ms —
        게이트 없을 때의 약 1/9.5다.
        """

        self._feed([NOISE_POWER] * 60)
        state = self.host._gate_state[0]
        self.assertFalse(state["floor_trusted"],
                         "조밀도만으로는 잡음/버스트를 구분할 수 없다")
        out = self._feed([NOISE_POWER] * 160)
        passed = out.count(False)
        self.assertEqual(passed, 10, "160창에 16창당 1창 = 10창이어야 한다")
        self.assertEqual(state["probes"], passed)

    def test_counters_are_recorded_for_evidence(self):
        """gated/passed 계수가 남아야 사후에 차단을 식별할 수 있다."""

        self._feed([NOISE_POWER] * 60)
        _gate(self.host, _request(BURST_POWER))
        _gate(self.host, _request(NOISE_POWER))
        state = self.host._gate_state[0]
        self.assertEqual(state["passed"], 1)
        self.assertEqual(state["gated"], 1)

    # --- bootstrap 창 기아 (diag-20260914-113630 부검) ----------------------
    #
    # 위 `test_all_burst_warmup_...`은 **무장 뒤**의 probe 비율만 본다. 실장비 4차는
    # 무장 뒤가 아니라 **warm-up 구간**에서 죽었고, 그래서 그 시험은 통과한 채로
    # 세션이 실패했다. 여기서는 창 기아 자체를 계약으로 고정한다.

    #: TBB publish 예산. `commit_at` +30 s(intent) +90 s(TBB publish) +30 s(endpoint 여유)
    #: — `stage7/ipc.py`의 `SERVICE_START_*_TIMEOUT_NS`와 `stage7/endpoint.py:423`.
    _TBB_BUDGET_NS = 150_000_000_000
    #: TBB 획득 **전** MASS가 감시하는 RX 창은 frame당 1개다: bootstrap slot map이
    #: BBSC 한 slot만 열고(`mac/tbb.py build_bootstrap_slot_map`) `required_rx_slots`가
    #: 그 하나만 고르며, frame은 15 hexslot × 160 ms다(`mac/tdma.py`).
    _FRAME_NS = 2_400_000_000
    #: 창이 frame마다 하나씩 도착한다고 볼 때 예산 구간에 들어오는 창 수의 **상계**다.
    #: (실제 첫 창의 도착 시각은 `commit_at`과 다르므로 두 기준을 더하지 않는다.)
    _BUDGET_WINDOWS = _TBB_BUDGET_NS // _FRAME_NS
    #: 게이트가 스스로 정한 유계 probe 주기(16창당 1창). 새 문턱을 만들지 않고 이 값을 쓴다.
    _PROBE_PERIOD = 16

    def _burst_only(self, count: int):
        """실패 회차 lane 0의 실측 분포 — warm-up 60창이 전부 버스트였다.

        봉인 로그 `pi/runs/diag-20260914-113630/logs/MASS-A.launcher.log:25`:
            noise_p10=2.828e-03 p50=2.858e-03 min=2.821e-03 max=2.891e-03
            basis=p10 thr=2.262e-02 trusted=0
        max/min = 1.025 — 단일 모드다. 같은 구간에서 결정적으로 뽑는다.
        """

        rnd = random.Random(20260914)
        return [rnd.uniform(2.821e-03, 2.891e-03) for _ in range(count)]

    @staticmethod
    def _longest_blackout(flags) -> int:
        longest = current = 0
        for zeroed in flags:
            current = current + 1 if zeroed else 0
            longest = max(longest, current)
        return longest

    def _assert_blackout_bounded(self, flags, where: str) -> None:
        """연속 0-치환 구간이 게이트 자신의 probe 주기 안에 있는지만 본다.

        이 시험이 고정하는 것은 **차단 공백의 상계**이지 "TBB가 획득된다"가 아니다.
        조립까지의 확인은 별도 집중시험(`FINDINGS-A` §4·§5)이 실제 조립기로 수행한다.
        환산 시간은 "bootstrap에서 창이 frame마다 하나씩 도착할 때"의 상계이며
        `commit_at` 기준 예산과 시작점이 다르므로 두 값을 더하지 않는다.
        """
        blackout = self._longest_blackout(flags)
        self.assertLessEqual(
            blackout, self._PROBE_PERIOD,
            "%s에서 %d창 연속 복호 차단(창당 frame 1개면 약 %.0f s). 참고: TBB publish "
            "예산은 commit_at 기준 %.0f s이고 ROC는 BBSC 기회마다 fragment를 보낸다."
            % (where, blackout, blackout * self._FRAME_NS / 1e9,
               self._TBB_BUDGET_NS / 1e9))

    def test_burst_only_warmup_does_not_black_out_the_tbb_acquisition_window(self):
        """**실장비 4차 재현**: 버스트만 오는 bootstrap에서 창이 굶으면 안 된다.

        TBB 획득 전 MASS는 frame당 창 1개만 계획하고 그 창에 오는 것은 ROC의 TBB fragment
        버스트뿐이다. 그러면 warm-up 임시 문턱(`min(warm) × 8`)이 **버스트 자신**을 0-치환한다
        — 표본이 단일 모드라 문턱이 버스트 전력에 붙기 때문이다. 실측 결과가 그 상태였다
        (`gated=1 passed=0`, probe 줄 0개 = 무장 뒤 창이 16개도 오지 못했다는 뜻).

        게이트는 성능 장치다. 전제가 깨졌을 때의 올바른 실패 방향은 **통과**(느려짐)이지
        전면 차단이 아니며, 따라서 복호 기회는 게이트 자신이 정한 probe 주기 안에 돌아와야
        한다. 문턱 배수나 예산을 늘려서가 아니라 **탈출구를 두어서** 만족해야 하는 계약이다.
        """

        flags = self._feed(self._burst_only(self._BUDGET_WINDOWS + self._PROBE_PERIOD))
        self._assert_blackout_bounded(flags, "버스트-only lane")

    def test_provisional_threshold_does_not_black_out_a_single_mode_warmup(self):
        """같은 계약을 warm-up 구간에 한정해 고정한다 — 유계 probe는 무장 **뒤**에만 돌고,
        warm-up 임시 문턱에는 탈출구가 아예 없다(`support.py`의 probe 분기 위치).

        임시 문턱의 목적은 주석대로 '잡음 창 **선별** 0-치환'이다. 표본이 단일 모드면
        잡음과 버스트를 가를 근거가 없으므로 선별이 성립하지 않으며, 그때 전량 0-치환하는
        것은 선별이 아니라 전면 차단이다.
        """

        flags = self._feed(self._burst_only(60))
        self._assert_blackout_bounded(flags, "warm-up 구간")

    def test_mixed_warmup_never_opens_the_warmup_probe(self):
        """음성 확인: 잡음 창이 섞인 warm-up(설계가 전제한 정상 상태)에서는 새 탈출구가
        한 번도 열리지 않는다 — 수정이 정상 경로의 부하·판정을 바꾸지 않는다.

        임시 문턱이 잡음 창만 선별하면 연속 차단은 1창을 넘지 못하므로 warm-probe는 0이다.
        전부 잡음인 조용한 lane은 여기에 해당하지 않는다(그 비용은
        `test_quiet_lane_probe_cost_is_bounded_and_documented`가 따로 고정한다).
        """

        for share in (2, 5, 10):                    # 잡음 50% / 20% / 10%
            host = W._GateHost()
            for i in range(60):
                _gate(host, _request(NOISE_POWER if i % share == 0 else BURST_POWER))
            state = host._gate_state[0]
            self.assertEqual(
                state["warm_probes"], 0,
                "잡음 1/%d 혼입 warm-up에서 warm-probe %d회 — 정상 경로가 바뀌었다"
                % (share, state["warm_probes"]))
            self.assertTrue(state["floor_trusted"], "잡음이 섞였으면 바닥은 신뢰돼야 한다")
            self.assertEqual(state["blackout"], 0)


if __name__ == "__main__":
    unittest.main()
