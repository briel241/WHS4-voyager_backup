"""Stage 7 lane RX dispatch·merge와 Stage5Support 확장 (`stage7` §15, §15.1.2).

`Stage7Stage5Support(Stage5Support)`는 frozen support의 MAC command dispatch,
Service hook, pump, stop-drain과 repository 의미를 그대로 상속하되 RX 관련
`_execute_rx`, `on_samples_received`, `on_rx_window_completed`, `on_layer_error`만
`Stage7LaneRxDispatcher`로 위임한다. 나머지 method, 특히 TX
encode/accept/completion과 Service dispatch는 inherited path를 사용한다.
"""

from __future__ import annotations

import hashlib
from threading import RLock

from ..common.contracts.phy import PhyRecoveryStatus, RecoveredPacketSet
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..runtime.phy_bridge import RuntimePhyRadioBridge
from ..runtime.stage5 import Stage5Support
from .errors import Stage7PrivateErrorCode
from .phy_facade import Stage7DiagnosticPhyFacade, Stage7LaneRxIngress


def _private_error(private_code: Stage7PrivateErrorCode, message: str, **details):
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


class Stage7LaneRxStack:
    """RF lane 하나의 frozen bridge + 진단 facade + ingress 묶음 (§15)."""

    __slots__ = ("lane_index", "bridge", "facade", "ingress", "lane_generation")

    def __init__(
        self,
        *,
        lane_index: int,
        facade: Stage7DiagnosticPhyFacade,
        ingress: Stage7LaneRxIngress,
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
        self.delivered = False


class Stage7LaneRxDispatcher:
    """adapter private child event를 lane stack에 결속하고 merge를 소유한다."""

    __slots__ = (
        "_support", "_stacks", "_lock", "_windows", "_stale_events",
        "_lane_generation", "_gate_state",
    )

    def __init__(self, support, stacks, *, lane_generation: int) -> None:
        self._support = support
        self._stacks = tuple(stacks)
        self._lock = RLock()
        self._windows = {}
        self._stale_events = []
        self._lane_generation = lane_generation
        self._gate_state = {}

    def _energy_gate(self, lane_index, native_request):
        """잡음 바닥 창의 샘플을 0으로 치환해 frozen 조기 탈출로 보낸다.

        Pi 실측: 무음(zeros) 창 디코드 4.9ms, 잡음 창은 레벨 무관
        ~106ms(상관 풀 탐색) — 창 완료 수요가 처리 용량을 넘어 pending
        장부 상시 포화(svc-run-f), 청취 창 기아의 뿌리. 문턱은 lane별
        잡음 전력 중앙값 ×8(+9dB): 워밍업 300창은 무조건 디코드하며
        통계만 수집, 이상·실패 시 원 요청 그대로(fail-open). 치환은
        디코드 입력 사본에만 적용되고 관측 계약(진단 싱크·ingress·
        결과 타입)은 frozen 경로 그대로다. STAGE7_RX_ENERGY_GATE=0
        으로 비활성.
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
                {"warm": [], "med": None, "gated": 0, "passed": 0},
            )
            if state["med"] is None:
                if power > 0.0:
                    state["warm"].append(power)
                if len(state["warm"]) >= 300:
                    warm = sorted(state["warm"])
                    # 잡음 추정 = 10분위수 (svc-run-k 실측: 버스트 빈번
                    # 레인(ROC 하부 — 함정 TBB·DSCH ACK)에서 중앙값이
                    # 신호 전력으로 오염 → 문턱이 버스트×8로 치솟아
                    # ACK까지 전면 차단, passed 3/3000. 하위 10%는
                    # 버스트가 절반 섞여도 잡음 바닥을 유지한다.)
                    state["med"] = warm[len(warm) // 10]
                    state["warm"] = []
                    print(
                        f"[launcher] rx-gate armed lane={lane_index} "
                        f"noise_p10={state['med']:.3e} "
                        f"p50={warm[len(warm) // 2]:.3e}",
                        file=_sys.stderr, flush=True,
                    )
                return native_request
            if state["med"] <= 0.0 or power >= state["med"] * 8.0:
                state["passed"] += 1
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
            zero_block = _dc.replace(
                samples, data=b"\x00" * len(samples.data)
            )
            return _dc.replace(native_request, samples=zero_block)
        except Exception:  # noqa: BLE001 - 게이트 실패 시 원 요청으로 디코드
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
                    "code": Stage7PrivateErrorCode.STALE_LANE_EVENT.value,
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
                    Stage7PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
                    "이미 등록된 logical RX window key입니다",
                )
            window = _LogicalWindow(key, command, canonical_allowed)
            self._windows[key] = window
        rx_window_key = (
            request.channel.name,
            request.start_at.clock_id,
            request.start_at.total_nanoseconds(),
            request.duration_nanoseconds,
        )
        for stack in self._stacks:
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
        prepared = stack.bridge.complete_window(completion)
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
                    Stage7PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
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
            if len(window.terminal_lanes) == len(self._stacks):
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
        self._support.finish_logical_window(window.command, window.key, merged=merged)

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


class Stage7Stage5Support(Stage5Support):
    """frozen `Stage5Support`의 four-method RX override (§15)."""

    __slots__ = ("_lane_dispatcher",)

    def __init__(self, endpoint, mac, outbox, ingress, health, lock) -> None:
        super().__init__(endpoint, mac, outbox, ingress, health, lock)
        self._lane_dispatcher = None

    def bind_lane_runtime(self, dispatcher: Stage7LaneRxDispatcher) -> None:
        """restart factory가 완전한 새 lane set을 한 번 atomic rebind한다."""

        self._lane_dispatcher = dispatcher

    @property
    def lane_dispatcher(self):
        return self._lane_dispatcher

    def reset_for_restart(self) -> None:
        super().reset_for_restart()
        self._lane_dispatcher = None

    # --- overridden RX seam ----------------------------------------------------

    def _execute_rx(self, command, request) -> None:
        from ..common.contracts.radio import RadioRxRequest
        from ..common.contracts.mac_execution import PhyRxWindowAcceptedEvent

        dispatcher = self._lane_dispatcher
        if dispatcher is None:
            error = _private_error(
                Stage7PrivateErrorCode.RX_LANE_CONTEXT_INVALID,
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

    def finish_logical_window(self, command, key, *, merged=None, error=None) -> None:
        from ..common.events import InternalEventType
        from ..common.contracts.mac_execution import PhyRxWindowClosedEvent

        if merged is not None:
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
    "Stage7LaneRxDispatcher",
    "Stage7LaneRxStack",
    "Stage7Stage5Support",
    "error_precedence_rank",
)
