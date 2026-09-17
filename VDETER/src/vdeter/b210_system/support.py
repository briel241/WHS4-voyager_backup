"""Stage 7 lane RX dispatch·merge와 Stage5Support 확장 (`stage7` §15, §15.1.2).

`LaneStage5Support(Stage5Support)`는 frozen support의 MAC command dispatch,
Service hook, pump, stop-drain과 repository 의미를 그대로 상속하되 RX 관련
`_execute_rx`, `on_samples_received`, `on_rx_window_completed`, `on_layer_error`만
`LaneRxDispatcher`로 위임한다. 나머지 method, 특히 TX
encode/accept/completion과 Service dispatch는 inherited path를 사용한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from threading import RLock

from ..common.contracts.phy import PhyRecoveryStatus, RecoveredPacketSet
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..runtime.phy_bridge import RuntimePhyRadioBridge
from ..runtime.stage5 import Stage5Support
from .errors import PrivateErrorCode
from .phy_facade import DiagnosticPhyFacade, LaneRxIngress


def _private_error(private_code: PrivateErrorCode, message: str, **details):
    return internal_error(
        Layer.RUNTIME,
        ErrorCode.INTERNAL_ERROR,
        ErrorCategory.INTERNAL,
        message,
        details=Details.of(stage7_private_code=private_code.value, **details),
    )


#: §15.1.2 오류 precedence rank (낮을수록 우선).
_ERROR_PRECEDENCE = {
    ErrorCode.RADIO_DEVICE_DISCONNECTED: 1,
    ErrorCode.RADIO_REFERENCE_LOST: 1,
    ErrorCode.RADIO_STREAM_FAILED: 1,
    ErrorCode.CONFIG_SEMANTIC_INVALID: 2,
    ErrorCode.RADIO_CONFIGURATION_FAILED: 2,
    ErrorCode.RADIO_LATE_COMMAND: 3,
    ErrorCode.RADIO_LATE_REQUEST: 3,
    ErrorCode.RADIO_RX_TIMEOUT: 3,
    ErrorCode.RADIO_OPERATION_CANCELLED: 3,
    ErrorCode.RADIO_RX_OVERFLOW: 4,
    ErrorCode.RADIO_RX_WINDOW_INCOMPLETE: 4,
    ErrorCode.PHY_SYNCHRONIZATION_FAILED: 5,
    ErrorCode.PHY_FEC_DECODE_FAILED: 5,
}


def error_precedence_rank(code) -> int:
    return _ERROR_PRECEDENCE.get(code, 5)


class LaneRxStack:
    """RF lane 하나의 frozen bridge + 진단 facade + ingress 묶음 (§15)."""

    __slots__ = ("lane_index", "bridge", "facade", "ingress", "lane_generation")

    def __init__(
        self,
        *,
        lane_index: int,
        facade: DiagnosticPhyFacade,
        ingress: LaneRxIngress,
        lane_generation: int,
    ) -> None:
        self.lane_index = lane_index
        self.facade = facade
        self.bridge = RuntimePhyRadioBridge(facade)
        self.ingress = ingress
        self.lane_generation = lane_generation

    def identity(self) -> tuple:
        return (self.lane_index, id(self.bridge), id(self.facade), id(self.ingress))


class _LogicalWindow:
    __slots__ = (
        "key",
        "command",
        "allowed",
        "results",
        "errors",
        "first_sample_utc_ns",
        "terminal_lanes",
        "expected_lanes",
        "delivered",
    )

    def __init__(self, key, command, allowed) -> None:
        self.key = key
        self.command = command
        self.allowed = allowed
        self.results = {}
        self.errors = {}
        self.first_sample_utc_ns = {}
        self.terminal_lanes = set()
        self.expected_lanes = {0, 1}
        self.delivered = False


class LaneRxDispatcher:
    """adapter private child event를 lane stack에 결속하고 merge를 소유한다."""

    __slots__ = (
        "_support", "_stacks", "_lock", "_windows", "_stale_events",
        "_lane_generation", "_gate_state", "_decode_executor",
    )

    def __init__(self, support, stacks, *, lane_generation: int) -> None:
        self._support = support
        self._stacks = tuple(stacks)
        self._lock = RLock()
        self._windows = {}
        self._stale_events = []
        self._lane_generation = lane_generation
        self._gate_state = {}
        self._decode_executor = None

    def set_decode_executor(self, executor) -> None:
        """리샘플·energy gate·디코드를 외부 실행기에 위임한다.

        `executor(lane_index, lane_generation, context, device_request,
        epoch_id, t0_hardware_ns)`로 호출되고, 결과는 호출자가 **펌프
        스레드에서** `complete_decoded(...)`에 같은 `context`로 돌려준다.
        미설정(기본)이면 현행 동기 경로 그대로다.
        """

        self._decode_executor = executor

    def _energy_gate(self, lane_index, native_request):
        """잡음 바닥 창의 샘플을 0으로 치환해 frozen 조기 탈출로 보낸다.

        Pi 실측: 무음(zeros) 창 디코드 4.9ms, 잡음 창은 레벨 무관
        ~106ms(상관 풀 탐색) — 창 완료 수요가 처리 용량을 넘어 pending
        장부 상시 포화(svc-run-f), 청취 창 기아의 뿌리. 문턱은 lane별
        잡음 전력 10분위수 ×8: 워밍업 60창 동안 통계 수집(10창 이후
        임시 문턱으로 잡음 창 선별 0-치환), 이상·실패 시 원 요청 그대로(fail-open). 치환은
        디코드 입력 사본에만 적용되고 관측 계약(진단 싱크·ingress·
        결과 타입)은 frozen 경로 그대로다. STAGE7_RX_ENERGY_GATE=0
        으로 비활성.

        무장 전 **전제 검사**와 **1회 한정 재무장**이 있다(diag-20260913-195703
        부검). 게이트는 성능 장치이지 안전 장치가 아니므로, 학습 전제가 깨졌을
        때의 올바른 실패 방향은 통과(느려짐)이지 전면 차단(기능 정지)이 아니다.
        """
        import os as _os

        if _os.environ.get("STAGE7_RX_ENERGY_GATE", "1") != "1":
            return native_request
        try:
            import dataclasses as _dc
            import sys as _sys

            import numpy as _np

            samples = native_request.samples
            if not samples.data:
                return native_request
            arr = _np.frombuffer(samples.data, dtype=_np.complex64)
            if arr.size == 0:
                return native_request
            power = float(_np.vdot(arr, arr).real) / arr.size
            state = self._gate_state.setdefault(
                lane_index,
                {"warm": [], "med": None, "gated": 0, "passed": 0,
                 "floor_trusted": False, "low_candidate": None,
                 "corrections": 0, "probes": 0,
                 # 연속 0-치환 창 수. warm-up 임시 문턱과 무장 후 유계 probe가
                 # **같은** 카운터를 쓴다(diag-20260914-113630 부검, 아래 두 분기).
                 "blackout": 0, "warm_probes": 0},
            )
            # 유계 probe 주기. 무장 후 분기가 이미 쓰던 값(16창당 1창)을 그대로
            # 인용한다 — 새 문턱을 만들거나 기존 배수(8.0)를 넓히지 않는다.
            probe_windows = 16
            if state["med"] is None:
                if power > 0.0:
                    state["warm"].append(power)
                # 워밍업 다이어트 (svc-run-p 부검: 워밍업 300창×2레인
                # 전량 풀디코드가 첫 30초에 큐 백로그 ~465항목을 만들고,
                # 이후 수요≈용량이라 영구 잔존 → 장부 한도 고착·청취
                # 확률화의 발원지). 워밍업 60창으로 단축하고, 10창
                # 이후부터는 관측 최솟값(잡음 바닥의 상계)×8 임시 문턱
                # 으로 잡음 창을 선별 0-치환 — 버스트(잡음 대비 +20dB)
                # 는 임시 문턱도 항상 통과하므로 TBB 획득 무영향.
                #
                # 위 주석의 "버스트는 임시 문턱도 항상 통과"는 **표본에 잡음 창이
                # 섞여 있을 때만** 참이다. 임시 문턱은 `min(warm) × 8`이므로 표본이
                # 단일 모드면(TBB 획득 전 bootstrap = BBSC 1 slot뿐이라 창에 ROC의
                # TBB fragment 버스트만 오는 정상 상태) 최솟값 자체가 버스트 전력이고
                # 문턱이 버스트 자신을 막는다. diag-20260914-113630 MASS-A lane 0이
                # 그 상태였다(min 2.821e-03 / max 2.891e-03 = 1.025배).
                #
                # 창 번호를 정확히 구분한다(모든 번호는 **이 lane에 도착한 창** 기준이며,
                # `commit_at`이나 admission 시각 기준이 아니다):
                #   창 0‥8   전력 표본만 모으고 **원본 통과**(warm < 10이라 문턱 없음)
                #   창 9‥58  임시 문턱 아래면 0-치환 — 단일 모드에서는 전량 차단
                #   창 59    무장 창. 이 호출은 0-치환하지 않고 **원본 통과**한다
                #   창 60‥   무장 문턱 적용, 미신뢰면 유계 probe
                # 즉 "첫 복호 기회"가 뒤로 밀리는 것이 아니라, 앞의 통과분으로 조립이
                # 끝나지 못한 경우에 **중간 50창이 연속으로 비는 것**이 문제다. 4-fragment
                # TBB를 무손실로 받으면 창 4개로 끝나므로(RF-off 실측) 이 구간은 무해하고,
                # fragment 유실·재전송으로 창 9 이후까지 필요해지면 그때 구조가 드러난다.
                # 게이트는 성능 장치이므로 전제가 깨졌을 때의 올바른 실패 방향은
                # 통과(느려짐)다. 무장 **뒤**에만 돌던 유계 probe를 여기에도 둔다.
                if len(state["warm"]) >= 10:
                    provisional = min(state["warm"]) * 8.0
                    if 0.0 < power < provisional and len(state["warm"]) < 60:
                        state["blackout"] += 1
                        if state["blackout"] < probe_windows:
                            zero_block = _dc.replace(
                                samples, data=b"\x00" * len(samples.data)
                            )
                            return _dc.replace(
                                native_request, samples=zero_block
                            )
                        state["blackout"] = 0
                        state["warm_probes"] += 1
                        if state["warm_probes"] <= 5:
                            print(
                                f"[launcher] rx-gate warm-probe lane={lane_index} "
                                f"n={state['warm_probes']} power={power:.3e} "
                                f"thr={provisional:.3e} (워밍업 임시 문턱)",
                                file=_sys.stderr, flush=True,
                            )
                        return native_request
                if len(state["warm"]) >= 60:
                    warm = sorted(state["warm"])
                    # 잡음 추정 = 10분위수 (svc-run-k 실측: 버스트 빈번
                    # 레인(ROC 하부 — 함정 TBB·DSCH ACK)에서 중앙값이
                    # 신호 전력으로 오염 → 문턱이 버스트×8로 치솟아
                    # ACK까지 전면 차단, passed 3/3000. 하위 10%는
                    # 버스트가 절반 섞여도 잡음 바닥을 유지한다.)
                    percentile10 = warm[len(warm) // 10]
                    # 전제 검사 (diag-20260913-195703 부검). 위 주석의 "절반"은
                    # 상한이 아니다. TBB 획득 전 MASS는 bootstrap slot map 때문에
                    # 프레임(2.4 s)당 RX 창을 사실상 1개만 계획하는데 ROC는 BBSC
                    # 기회마다 TBB fragment를 보내므로, 워밍업 표본이 버스트 위주가
                    # 되는 것이 정상 상태다. 실측: p10 2.848e-03(= C1 버스트 전력),
                    # 정상 회차 2.9e-06 — 1000배.
                    #
                    # 최솟값은 워밍업이 이미 임시 문턱으로 쓰는 "잡음 바닥의 상계"다.
                    # p10이 그 임시 문턱(min×8) 위로 나왔다면 p10 자체가 버스트
                    # 표본이라는 뜻이므로 잡음 추정치로 쓸 수 없다. 그때는 상계로
                    # 무장한다 — 문턱 배수(8.0)를 넓히는 것이 아니라, 워밍업 구간에서
                    # 이미 적용하던 것과 같은 문턱을 유지하는 것이다.
                    upper_bound = warm[0]
                    if percentile10 >= upper_bound * 8.0:
                        state["med"] = upper_bound
                        basis = "min_upper_bound"
                    else:
                        state["med"] = percentile10
                        basis = "p10"
                    # 잡음 **근거**의 유무를 값과 분리해 기록한다. 표본이 단일
                    # 모드(최댓값이 최솟값의 8배 미만)면 저전력 집단이 관측되지
                    # 않았다는 뜻이고, 그때 학습한 값은 잡음이라는 근거가 없다
                    # (전부 잡음인 조용한 레인과 전부 버스트인 레인을 표본만으로
                    # 구분할 수 없다). 값은 쓰되 **신뢰하지 않는다** — 아래
                    # 하향 보정과 유계 probe가 그 상태에서만 동작한다.
                    state["floor_trusted"] = warm[-1] >= upper_bound * 8.0
                    state["warm"] = []
                    print(
                        f"[launcher] rx-gate armed lane={lane_index} "
                        f"noise_p10={percentile10:.3e} "
                        f"p50={warm[len(warm) // 2]:.3e} "
                        f"min={upper_bound:.3e} max={warm[-1]:.3e} "
                        f"basis={basis} thr={state['med'] * 8.0:.3e} "
                        f"trusted={int(state['floor_trusted'])}",
                        file=_sys.stderr, flush=True,
                    )
                # warm-up에서 통과한 창(임시 문턱 위이거나 무장이 일어난 창)은
                # 연속 차단을 끊는다 — 무장 전후 경계에서 차단이 이어붙지 않는다.
                state["blackout"] = 0
                return native_request
            # 하향 보정. 학습한 바닥보다 확실히 낮은 창이 **실제로 도착하면**,
            # 그 바닥은 잡음이 아니었다는 양성 증거다(같은 표본을 다시 모으는
            # 것이 아니라 새 관측을 근거로 삼는다 — 재무장은 오염 표본을 재수집할
            # 뿐이라 해결이 아니었다). 단발 dropout 한 창이 바닥을 무너뜨려 잡음
            # 창이 전량 통과하는 일이 없도록 **2표본 확인** 후 둘 중 높은 쪽을
            # 택한다. 하향만 가능하므로 실제 최솟값으로 수렴하고 발산하지 않는다.
            if 0.0 < power < state["med"] / 8.0:
                candidate = state["low_candidate"]
                if candidate is None:
                    state["low_candidate"] = power
                elif max(candidate, power) > min(candidate, power) * 1.5:
                    # 두 저전력 관측이 같은 집단이 아니다. 보존 evidence 18건
                    # 실측에서 무장 표본 내부 p50/p10은 1.01~1.06 — 잡음 창끼리는
                    # 6% 안쪽으로 촘촘하다. 1.5배를 넘으면 한쪽은 dropout 같은
                    # 단발 이상치이므로 정정하지 않고, 더 그럴듯한(높은) 쪽만
                    # 후보로 남긴다. 이상치 하나가 바닥을 무너뜨리면 잡음 창이
                    # 전량 통과해 게이트 도입 이유 자체가 사라진다.
                    state["low_candidate"] = max(candidate, power)
                else:
                    previous = state["med"]
                    state["med"] = max(candidate, power)
                    state["low_candidate"] = None
                    state["floor_trusted"] = True
                    state["corrections"] += 1
                    print(
                        f"[launcher] rx-gate floor-correct lane={lane_index} "
                        f"{previous:.3e} -> {state['med']:.3e} "
                        f"thr={state['med'] * 8.0:.3e} "
                        f"n={state['corrections']}",
                        file=_sys.stderr, flush=True,
                    )
            if state["med"] <= 0.0 or power >= state["med"] * 8.0:
                state["passed"] += 1
                state["blackout"] = 0
                # 통과 이벤트 관측 (svc-run-l2 부검: ROC lane0의 유일
                # 버스트(ACK)가 무음 소실 — 창 미무장/게이트/디코드 3분기
                # 판별용. 통과가 드문 레인에서 특히 유효.)
                if state["passed"] <= 30 or state["passed"] % 200 == 1:
                    print(
                        f"[launcher] rx-gate pass lane={lane_index} "
                        f"n={state['passed']} power={power:.3e} "
                        f"thr={state['med'] * 8.0:.3e}",
                        file=_sys.stderr, flush=True,
                    )
                return native_request
            state["gated"] += 1
            if state["gated"] % 1000 == 1:
                print(
                    f"[launcher] rx-gate lane={lane_index} "
                    f"gated={state['gated']} passed={state['passed']}",
                    file=_sys.stderr, flush=True,
                )
            # 잡음 근거를 끝내 확보하지 못한 동안에만 유계 probe(16창당 1창)를
            # 게이트 밖으로 흘린다. 문턱을 넓히거나 무조건 통과시키는 것이 아니다
            # — 학습값을 신뢰할 수 없을 때 전면 차단으로 TBB 획득 경로가 죽는 것을
            # 막는 최소 장치이며, 복호 부하는 1/16로 묶인다. 하향 보정으로 잡음
            # 근거가 확보되면(`floor_trusted`) 즉시 꺼진다.
            #
            # 기준은 누적 `gated`가 아니라 **연속 차단** 수다(diag-20260914-113630):
            # 누적 기준이면 무장 직후 15창까지 probe가 한 번도 오지 않는데, 창이
            # frame당 1개뿐인 bootstrap에서 그 15창이 곧 36 s다. 실측 로그에 probe가
            # 한 줄도 없던 이유가 그것이고(무장 후 창이 16개도 못 왔다), 그 사이
            # publish deadline이 만료됐다. warm-up 분기와 같은 카운터를 공유해 구간
            # 경계에서도 차단이 이어붙지 않게 한다. 통과 비율(1/16)은 그대로다.
            state["blackout"] += 1
            if not state["floor_trusted"] and state["blackout"] >= probe_windows:
                state["blackout"] = 0
                state["probes"] += 1
                if state["probes"] <= 5 or state["probes"] % 50 == 1:
                    print(
                        f"[launcher] rx-gate probe lane={lane_index} "
                        f"n={state['probes']} power={power:.3e} "
                        f"thr={state['med'] * 8.0:.3e} (잡음 근거 미확보)",
                        file=_sys.stderr, flush=True,
                    )
                return native_request
            zero_block = _dc.replace(
                samples, data=b"\x00" * len(samples.data)
            )
            return _dc.replace(native_request, samples=zero_block)
        except Exception as exc:  # noqa: BLE001 - 게이트 실패 시 원 요청으로 디코드
            # 무음 전면 비활성 금지: 여기서 조용히 낙하하면 106ms/창
            # 전량 상관 부하가 로그 한 줄 없이 재귀한다 — 1회성 경고 (0.9.22).
            if not self._gate_state.get("_fail_warned"):
                self._gate_state["_fail_warned"] = True
                print(
                    f"[launcher] rx-gate 예외 → 게이트 무효(전량 디코드): "
                    f"{type(exc).__name__}: {exc}",
                    file=_sys.stderr, flush=True,
                )
            return native_request

    @property
    def stacks(self) -> tuple:
        return self._stacks

    def stale_events(self) -> tuple:
        with self._lock:
            return tuple(self._stale_events)

    def _stale(self, kind: str, lane_generation: int, lane_index: int) -> None:
        with self._lock:
            self._stale_events.append(
                {
                    "kind": kind,
                    "code": PrivateErrorCode.STALE_LANE_EVENT.value,
                    "lane_generation": lane_generation,
                    "lane_index": lane_index,
                }
            )

    # --- 등록 -----------------------------------------------------------------

    def register_window(self, command, request, canonical_allowed, *, timing_binding):
        """logical RX window 하나를 두 lane stack에 fan-out 등록한다."""

        key = (request.channel.name, request.start_at.total_nanoseconds())
        with self._lock:
            if key in self._windows:
                return _private_error(
                    PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                    "이미 등록된 logical RX window key입니다",
                )
            window = _LogicalWindow(key, command, canonical_allowed)
            window.expected_lanes = set(self._support._endpoint._radio.rx_lanes_for(request.channel.name))
            self._windows[key] = window
        rx_window_key = (
            request.channel.name,
            request.start_at.clock_id,
            request.start_at.total_nanoseconds(),
            request.duration_nanoseconds,
        )
        for stack in self._stacks:
            if stack.lane_index not in window.expected_lanes:
                continue
            admitted = stack.bridge.admit_window(
                request.channel, request.start_at, request.duration_nanoseconds,
                canonical_allowed,
            )
            if admitted.is_err:
                return admitted
            ingress_admitted = stack.ingress.admit_window(
                rx_window_key, **timing_binding
            )
            if ingress_admitted.is_err:
                return ingress_admitted
        return Ok(rx_window_key)

    # --- adapter callbacks ----------------------------------------------------

    def on_lane_samples(self, lane_generation, lane_index, child_key, received):
        if lane_generation != self._lane_generation:
            self._stale("samples", lane_generation, lane_index)
            return
        stack = self._stacks[lane_index]
        rx_window_key = (
            received.channel.name,
            received.samples.first_sample_at.clock_id,
            child_key[1][2],
            child_key[1][3],
        )
        observed = stack.ingress.observe_block(rx_window_key, received)
        if observed.is_err:
            self._support.report_error(observed.error)
        bridged = stack.bridge.observe_samples(received)
        if bridged.is_err:
            self._support.report_error(bridged.error)

    def on_lane_window_completed(self, lane_generation, lane_index, child_key, completion):
        if lane_generation != self._lane_generation:
            self._stale("completion", lane_generation, lane_index)
            return
        stack = self._stacks[lane_index]
        key = (completion.channel.name, completion.requested_start_at.total_nanoseconds())
        with self._lock:
            window = self._windows.get(key)
        if window is None:
            self._stale("completion_unmatched", lane_generation, lane_index)
            return
        rx_window_key = (
            completion.channel.name,
            completion.requested_start_at.clock_id,
            completion.requested_start_at.total_nanoseconds(),
            completion.requested_duration_nanoseconds,
        )
        executor = self._decode_executor
        # executor가 있으면 리샘플 이전(device rate)에서 끊어 넘긴다. 없으면
        # 합성 API를 그대로 부른다 — 두 경로의 반환 의미는 동일하다.
        prepared = (
            stack.bridge.complete_device_window(completion)
            if executor is not None
            else stack.bridge.complete_window(completion)
        )
        if prepared.is_err:
            self._support.report_error(prepared.error)
            self._lane_terminal(window, lane_index, error=prepared.error, received_at_ns=None)
            return
        native_request = prepared.value
        if native_request is None:
            # incomplete/gapped window: frozen 계약상 empty 정상 종료가 아니라
            # 해당 lane의 결과 없음으로 처리한다.
            self._lane_terminal(
                window, lane_index,
                error=_private_error(
                    PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                    "lane window가 완전한 연속 8,192 sample이 아닙니다",
                ).error,
                received_at_ns=None,
            )
            return
        link_family = 11 if window.allowed == (11,) else window.allowed[0]
        promoted = stack.ingress.promote_window(rx_window_key, link_family)
        if promoted.is_err:
            self._support.report_error(promoted.error)
            self._lane_terminal(window, lane_index, error=promoted.error, received_at_ns=None)
            return
        segment, epoch_id = promoted.value
        t0 = stack.ingress.epoch_t0_hardware_ns(epoch_id)
        if executor is not None:
            # 여기서부터(리샘플·게이트·디코드)가 별도 프로세스로 나간다.
            # 결과는 펌프 스레드가 complete_decoded로 되돌린다.
            executor(lane_index, lane_generation, (window, rx_window_key),
                     native_request, epoch_id, t0)
            return
        native_request = self._energy_gate(lane_index, native_request)
        stack.facade.diagnostic_sink.begin_window(rx_window_key, epoch_id, t0)
        try:
            decoded = stack.facade.decode_samples(native_request)
        finally:
            stack.facade.diagnostic_sink.end_window()
        stack.ingress.finalize_window(rx_window_key)
        if decoded.is_err:
            self._support.report_error(decoded.error)
            self._lane_terminal(window, lane_index, error=decoded.error, received_at_ns=None)
            return
        self._lane_terminal(
            window, lane_index, result=decoded.value,
            received_at_ns=native_request.samples.first_sample_at.total_nanoseconds(),
        )

    def complete_decoded(self, lane_generation, lane_index, context, *,
                         result=None, error=None, gapped=False,
                         received_at_ns=None, attempt_records=()):
        """외부 실행기의 decode 결과를 **펌프 스레드에서** 반영한다.

        동기 경로의 decode 이후 구간(진단 기록·finalize_window·lane terminal)을
        순서 그대로 수행하고, 늦게 온 결과는 기존 stale 경로로 폐기한다.
        """

        if lane_generation != self._lane_generation:
            self._stale("decoded", lane_generation, lane_index)
            return
        window, rx_window_key = context
        stack = self._stacks[lane_index]

        if gapped:
            # 동기 경로에서 native 변환이 Ok(None)을 낸 경우와 같은 처리다
            # (promote/finalize 없이 결과 없음). complete_device_window가
            # sample_count·continuity를 상수로 세우므로 실제로는 도달하지
            # 않지만, frozen 분기를 그대로 보존한다.
            self._lane_terminal(
                window, lane_index,
                error=_private_error(
                    PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                    "lane window가 완전한 연속 8,192 sample이 아닙니다",
                ).error,
                received_at_ns=None,
            )
            return

        # 진단 기록은 decode 안에서 생성되므로(§9.3) 실행기에서 회수해 여기서
        # 재생해야 evidence가 보존된다. 재생 실패가 수신 결과를 무효로 만들지는
        # 않는다 — fail-open.
        sink = stack.facade.diagnostic_sink
        for record in attempt_records or ():
            try:
                sink.record(record)
            except Exception:  # noqa: BLE001
                pass

        stack.ingress.finalize_window(rx_window_key)
        if error is not None:
            self._support.report_error(error)
            self._lane_terminal(window, lane_index, error=error,
                                received_at_ns=None)
            return
        self._lane_terminal(window, lane_index, result=result,
                            received_at_ns=received_at_ns)

    def on_lane_error(self, lane_generation, lane_index, child_key, code, details):
        if lane_generation != self._lane_generation:
            self._stale("error", lane_generation, lane_index)
            return
        stack = self._stacks[lane_index]
        recorded = stack.bridge.record_rx_fault(code, dict(details))
        if recorded.is_err:
            self._support.report_error(recorded.error)
        key = (details.get("channel"), child_key[1][2])
        with self._lock:
            window = self._windows.get(key)
        if window is None:
            self._stale("error_unmatched", lane_generation, lane_index)
            return
        error = internal_error(
            Layer.RADIO, code, ErrorCategory.DEVICE, "lane RX terminal 오류",
            details=Details.of(lane_index=lane_index),
        ).error
        self._lane_terminal(window, lane_index, error=error, received_at_ns=None)

    # --- merge (§15.1.2) --------------------------------------------------------

    def _lane_terminal(self, window, lane_index, *, result=None, error=None, received_at_ns=None):
        deliver = False
        with self._lock:
            if lane_index in window.terminal_lanes or window.delivered:
                self._stale("duplicate_terminal", self._lane_generation, lane_index)
                return
            window.terminal_lanes.add(lane_index)
            if result is not None:
                window.results[lane_index] = result
                window.first_sample_utc_ns[lane_index] = received_at_ns
            if error is not None:
                window.errors[lane_index] = error
            if window.terminal_lanes == window.expected_lanes:
                window.delivered = True
                deliver = True
                self._windows.pop(window.key, None)
        if deliver:
            self._deliver(window)

    def _deliver(self, window):
        if window.errors:
            # required lane 하나라도 terminal error이면 logical window 전체 실패;
            # partial delivery는 없다. 대표 오류는 §15.1.2 precedence 하나만.
            ranked = sorted(
                window.errors.items(),
                key=lambda item: (error_precedence_rank(item[1].code), item[0]),
            )
            representative = ranked[0][1]
            self._support.finish_logical_window(window.command, window.key, error=representative)
            return
        merged = self._merge_results(window)
        gate = getattr(self._support, "collision_gate", None)
        if gate is None or not gate.accepting:
            self._support.finish_logical_window(window.command, window.key, merged=merged)
            return
        request = window.command.payload.request
        rx_key = (request.channel.name, request.start_at.clock_id,
                  request.start_at.total_nanoseconds(), request.duration_nanoseconds)
        metadata, packets = {}, []
        for lane, result in sorted(window.results.items()):
            records = self._stacks[lane].facade.diagnostic_sink.recent_records_for_window(rx_key)
            for packet in result.packets:
                digest = hashlib.sha256(packet.packet).hexdigest()
                matches = [r for r in records if r.packet_digest == digest and r.estimator_status == "RECOVERED"]
                # Ambiguous/missing actual PHY time is not replaced by requested window time.
                meta = None
                if len(matches) == 1:
                    record = matches[0]
                    meta = {
                        "physical_channel": request.channel.name, "link_id": record.link_id,
                        "device_hw_timestamp_ns": record.rx_sample0_hardware_ns,
                        "rx_utc_ns": self._support._endpoint._radio.bridge.hardware_ns_to_utc_label(record.rx_sample0_hardware_ns),
                        "duration_ns": (8192 * 1_000_000_000 + 307199) // 307200,
                        "host_mono_timestamp_ns": self._support._endpoint._time().now_monotonic().total_nanoseconds(),
                        "lane": lane, "lane_generation": record.lane_generation,
                        "stream_epoch_id": record.stream_epoch_id,
                        "device_time_source": "PHY_RECOVERED_SAMPLE0_HARDWARE_NS",
                        "physical_channel_source": "ACTUAL_RX_WINDOW",
                    }
                packets.append(packet)
                metadata[packet.packet_id.value] = meta
        # Keep independently decoded equal packets until AFTER overlap classification.
        self._support.finish_logical_window(window.command, window.key,
            merged=replace(merged, packets=tuple(packets)), metadata=metadata)

    def _merge_results(self, window) -> RecoveredPacketSet:
        winners = {}
        comparisons = []
        for lane_index, result in sorted(window.results.items()):
            for packet in result.packets:
                digest = (len(packet.packet), hashlib.sha256(packet.packet).hexdigest())
                existing = winners.get(digest)
                if existing is None:
                    winners[digest] = (packet, lane_index)
                else:
                    current, current_lane = existing
                    comparisons.append(
                        {
                            "digest": digest[1],
                            "byte_equal": current.packet == packet.packet,
                            "lanes": (current_lane, lane_index),
                        }
                    )
                    if (
                        packet.received_at.total_nanoseconds(),
                        lane_index,
                    ) < (
                        current.received_at.total_nanoseconds(),
                        current_lane,
                    ):
                        winners[digest] = (packet, lane_index)
        ordered = sorted(
            winners.values(),
            key=lambda item: (
                item[0].received_at.total_nanoseconds(),
                item[1],
                hashlib.sha256(item[0].packet).hexdigest(),
                item[0].packet,
            ),
        )
        packets = tuple(item[0] for item in ordered)
        sample_result = next(iter(window.results.values()))
        if packets:
            received_at = min(
                (packet.received_at for packet in packets),
                key=lambda point: point.total_nanoseconds(),
            )
            status = PhyRecoveryStatus.RECOVERED
        else:
            candidates = [value for value in window.first_sample_utc_ns.values() if value is not None]
            base = sample_result.received_at
            if candidates:
                target = min(candidates)
                base = base.shifted(target - base.total_nanoseconds())
            received_at = base
            status = PhyRecoveryStatus.NO_BURST_DETECTED
        return RecoveredPacketSet(
            channel=sample_result.channel,
            received_at=received_at,
            status=status,
            packets=packets,
        )


class LaneStage5Support(Stage5Support):
    """frozen `Stage5Support`의 four-method RX override (§15)."""

    __slots__ = ("_lane_dispatcher", "collision_gate", "_collision_pump_lock")

    def __init__(self, endpoint, mac, outbox, ingress, health, lock) -> None:
        super().__init__(endpoint, mac, outbox, ingress, health, lock)
        self._lane_dispatcher = None
        self.collision_gate = None
        self._collision_pump_lock = RLock()

    def pump(self):
        # The RF/decoder producer only enqueues. Deliver on the existing pump,
        # never on the TEST socket thread, never under the RX dispatcher lock.
        if self.collision_gate is None:
            super().pump()
            return
        if self._collision_pump_lock.acquire(blocking=False):
            try:
                gate = self.collision_gate
                if gate is not None:
                    for item, packets, failure in gate.drain():
                        command, key, merged, _, _ = item
                        if failure:
                            self._finish_collision_window(command, key, error=_private_error(
                                PrivateErrorCode.RX_LANE_CONTEXT_INVALID, failure).error)
                        else:
                            self._finish_collision_window(command, key,
                                merged=replace(merged, packets=packets,
                                    status=PhyRecoveryStatus.RECOVERED if packets else PhyRecoveryStatus.NO_BURST_DETECTED))
            finally:
                self._collision_pump_lock.release()
        super().pump()

    def bind_lane_runtime(self, dispatcher: LaneRxDispatcher) -> None:
        """restart factory가 완전한 새 lane set을 한 번 atomic rebind한다."""

        self._lane_dispatcher = dispatcher

    def note_application_delivery(self, packet_id):
        gate = self.collision_gate
        if gate is not None:
            gate.application_delivered(packet_id.value)

    @property
    def lane_dispatcher(self):
        return self._lane_dispatcher

    def reset_for_restart(self) -> None:
        if self.collision_gate is not None:
            self.collision_gate.fail("ENDPOINT_RESTART")
        super().reset_for_restart()
        self._lane_dispatcher = None

    # --- overridden RX seam ----------------------------------------------------

    def _execute_rx(self, command, request) -> None:
        from ..common.contracts.radio import RadioRxRequest
        from ..common.contracts.mac_execution import PhyRxWindowAcceptedEvent

        dispatcher = self._lane_dispatcher
        if dispatcher is None:
            error = _private_error(
                PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                "lane dispatcher가 결속되지 않았습니다",
            ).error
            self.report_error(error)
            self._fail(command, error)
            return
        sample_rate = self._endpoint._dependencies.configuration.endpoint.phy.sample_rate_hz
        canonical_allowed = None
        for stack in dispatcher.stacks:
            intent = stack.bridge.validate_window_intent(
                getattr(request, "allowed_link_ids", ())
            )
            if intent.is_err:
                self.report_error(intent.error)
                self._fail(command, intent.error)
                return
            canonical_allowed = intent.value
            timing = stack.bridge.validate_window_timing(
                request.duration_nanoseconds, sample_rate
            )
            if timing.is_err:
                self.report_error(timing.error)
                self._fail(command, timing.error)
                return
        accepted = self._endpoint._radio.request_rx(
            RadioRxRequest(
                channel=request.channel,
                start_at=request.start_at,
                duration_nanoseconds=request.duration_nanoseconds,
                sample_rate_hz=sample_rate,
            )
        )
        if accepted.is_err:
            self._fail(command, accepted.error)
            return
        timing_binding = self._endpoint_timing_binding()
        registered = dispatcher.register_window(
            command, request, canonical_allowed, timing_binding=timing_binding
        )
        if registered.is_err:
            self.report_error(registered.error)
        key = (request.channel.name, request.start_at.total_nanoseconds())
        self._rx_commands[key] = command
        self.enqueue(
            PhyRxWindowAcceptedEvent(
                command_id=command.command_id,
                channel=request.channel,
                accepted_at=self._endpoint._time().now_monotonic(),
            ),
            correlation=command.correlation,
            causation_command_id=command.command_id,
            priority=True,
        )

    def _endpoint_timing_binding(self) -> dict:
        bridge = getattr(self._endpoint._radio, "bridge", None)
        if bridge is not None and bridge.state == "ACTIVE":
            return {
                "timing_bridge_kind": "ACTIVE",
                "timing_clock_id": bridge.clock_id,
                "acquisition_attempt": None,
                "bridge_generation": bridge.generation,
            }
        if bridge is not None and bridge.state == "PROVISIONAL":
            return {
                "timing_bridge_kind": "PROVISIONAL",
                "timing_clock_id": bridge.clock_id,
                "acquisition_attempt": bridge.generation or 0,
                "bridge_generation": None,
            }
        return {
            "timing_bridge_kind": "NONE",
            "timing_clock_id": None,
            "acquisition_attempt": None,
            "bridge_generation": None,
        }

    def on_samples_received(self, event) -> bool:
        # Stage 7 child sample event는 private dispatcher로만 흐른다. 공개 bus에
        # singular RX callback이 도착하면 stale evidence만 남긴다 (§15).
        if self._lane_dispatcher is not None:
            self._lane_dispatcher._stale("bus_samples", -1, -1)
            return True
        return super().on_samples_received(event)

    def on_rx_window_completed(self, event) -> bool:
        if self._lane_dispatcher is not None:
            self._lane_dispatcher._stale("bus_completion", -1, -1)
            return True
        return super().on_rx_window_completed(event)

    def on_layer_error(self, event) -> bool:
        if self._lane_dispatcher is not None:
            # parent-level Radio LAYER_ERROR는 frozen health 경로에 남기되 lane
            # ledger 반영은 dispatcher child 경로가 전담한다.
            return False
        return super().on_layer_error(event)

    # --- merge terminal --------------------------------------------------------

    def finish_logical_window(self, command, key, *, merged=None, error=None, metadata=None) -> None:
        gate = self.collision_gate
        if (gate is not None and merged is not None and merged.packets
                and command.payload.request.start_at.total_nanoseconds() >= gate.active_from_ns):
            request = command.payload.request
            start = request.start_at.total_nanoseconds()
            item = (command, key, merged, start, start + request.duration_nanoseconds)
            packets = tuple((p, (metadata or {}).get(p.packet_id.value))
                            for p in merged.packets)
            held = gate.hold(key, item, packets)
            if held is None:
                self._finish_collision_window(command, key, merged=merged)
                return
            if held:
                return
            error = _private_error(PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                                   "MODEL_COLLISION_RX_CAPACITY").error
            merged = None
        self._finish_collision_window(command, key, merged=merged, error=error)

    def _finish_collision_window(self, command, key, *, merged=None, error=None):
        from ..common.events import InternalEventType
        from ..common.contracts.mac_execution import PhyRxWindowClosedEvent

        if merged is not None and (merged.packets or self.collision_gate is None):
            self._endpoint._record(
                InternalEventType.PACKETS_RECOVERED, Layer.PHY, payload=merged
            )
            self._endpoint._deliver_recovered(merged, stage4=self)
        elif error is not None:
            self.report_error(error)
        stored = self._rx_commands.pop(key, None)
        if stored is not None:
            self.enqueue(
                PhyRxWindowClosedEvent(
                    command_id=stored.command_id,
                    channel=stored.payload.request.channel,
                    closed_at=self._endpoint._time().now_monotonic(),
                ),
                correlation=stored.correlation,
                causation_command_id=stored.command_id,
                priority=True,
            )
        # 직접 pump() 금지 (ABBA 교착: 이 경로는 monitor 스레드가 dispatch
        # 락을 쥔 채 실행되는데, pump는 MAC 락을 요구한다. 반대편 pump
        # 스레드는 MAC 락을 쥔 채 창 등록에서 dispatch 락을 기다린다 —
        # 상호 대기로 계획 마비. enqueue된 이벤트는 40ms 주기 펌프
        # 스레드가 처리하므로 기능 동일, 지연 +40ms).


__all__ = (
    "LaneRxDispatcher",
    "LaneRxStack",
    "LaneStage5Support",
    "error_precedence_rank",
)
