"""Stage 7 lane별 진단 PHY facade와 RX ingress context (`stage7` §9.3, §15.1.2).

frozen `RecoveredPacket`/`RecoveredPacketSet`에는 CFO field를 추가하지 않고 frozen
PHY 파일도 수정하지 않는다. additive `Stage7DiagnosticPhyFacade`를 **endpoint의
RX lane마다 하나씩** 사용한다. 각 facade는 frozen `MultiRatePhy`를 상속해 같은
receiver core와 lane-local state를 사용하고, `_build_recovered_set` 경계에서 이미
생성된 `ReceiverCoreResult`의 `cfo_hz`와 timestamp 입력을 Runtime-private
`Stage7PhyDiagnosticSink`에 먼저 기록한 뒤 frozen `super()._build_recovered_set()`
으로 lane-local public-shaped result를 만든다. samples, receiver result, buffer
consumption, Packet 순서나 error를 변경하거나 decode를 두 번 수행하지 않는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..common.time import ClockDomain
from ..phy import resampling, wideband
from ..phy.multilink import MultiRatePhy
from .errors import Stage7PrivateErrorCode

LINK11_RAMP_SAMPLES = 32.0
WIDEBAND_RAMP_SAMPLES = float(wideband.RAMP_SAMPLES)


def _private_error(private_code: Stage7PrivateErrorCode, message: str, **details):
    return internal_error(
        Layer.RUNTIME,
        ErrorCode.INTERNAL_ERROR,
        ErrorCategory.INTERNAL,
        message,
        details=Details.of(stage7_private_code=private_code.value, **details),
    )


def half_away_from_zero_ns(numerator: int, denominator: int) -> int:
    """`delta_ns = half-away-from-zero(delta_ns_exact)` (§13.3)."""

    exact = Fraction(numerator, denominator)
    floor = exact.numerator // exact.denominator
    remainder = exact - floor
    if exact >= 0:
        return int(floor) + (1 if remainder * 2 >= 1 else 0)
    # 음수: away-from-zero 반올림.
    if remainder == 0:
        return int(floor)
    if remainder * 2 > 1:
        return int(floor) + 1
    if remainder * 2 == 1:
        return int(floor)
    return int(floor)


@dataclass(frozen=True, slots=True)
class Stage7RxSegment:
    """completed-window append-only segment (§15.1.2)."""

    rx_window_key: tuple
    device_stream_offset: int
    device_sample_count: int
    device_sample_rate_hz: int
    phy_stream_offset: int
    phy_sample_count: int
    phy_sample_rate_hz: int
    first_sample_hardware_ns: int


@dataclass(frozen=True, slots=True)
class Stage7PhyAttemptRecord:
    """diagnostic key `(lane_generation,rx_window_key,lane_index,phy_attempt_index)`의 값."""

    lane_generation: int
    rx_window_key: tuple
    lane_index: int
    phy_attempt_index: int
    link_id: int
    packet_digest: str | None
    cfo_hz_hex: str
    cfo_hz: float
    phy_stream_buffer_offset: int
    training_epoch_samples_hex: str
    rho_hat_hex: str
    stream_epoch_id: int
    stream_t0_hardware_ns: int
    rx_sample0_hardware_ns: int
    estimator_status: str


class Stage7PhyDiagnosticSink:
    """lane-scoped diagnostic sink (§9.3). 다른 lane과 상태를 공유하지 않는다."""

    __slots__ = (
        "lane_index",
        "lane_generation",
        "_records",
        "_current_window_key",
        "_current_epoch_id",
        "_current_t0_ns",
        "_attempt_counters",
        "configured",
    )

    def __init__(self, *, lane_index: int, lane_generation: int) -> None:
        self.lane_index = lane_index
        self.lane_generation = lane_generation
        self._records = {}
        self._current_window_key = None
        self._current_epoch_id = None
        self._current_t0_ns = None
        self._attempt_counters = {}
        self.configured = True

    def begin_window(self, rx_window_key: tuple, stream_epoch_id: int, stream_t0_hardware_ns: int) -> None:
        self._current_window_key = rx_window_key
        self._current_epoch_id = stream_epoch_id
        self._current_t0_ns = stream_t0_hardware_ns

    def end_window(self) -> None:
        self._current_window_key = None
        self._current_epoch_id = None
        self._current_t0_ns = None

    @property
    def has_window(self) -> bool:
        return self._current_window_key is not None

    def next_attempt_index(self) -> int:
        key = (self.lane_generation, self._current_epoch_id)
        index = self._attempt_counters.get(key, 0)
        self._attempt_counters[key] = index + 1
        return index

    def record(self, record: Stage7PhyAttemptRecord) -> None:
        key = (
            record.lane_generation,
            record.rx_window_key,
            record.lane_index,
            record.phy_attempt_index,
        )
        self._records[key] = record

    def records(self) -> tuple:
        return tuple(self._records.values())

    def records_for_window(self, rx_window_key: tuple) -> tuple:
        return tuple(
            record
            for record in self._records.values()
            if record.rx_window_key == rx_window_key
        )

    @property
    def current_window_key(self):
        return self._current_window_key

    @property
    def current_epoch_id(self):
        return self._current_epoch_id

    @property
    def current_t0_ns(self):
        return self._current_t0_ns


class Stage7DiagnosticPhyFacade(MultiRatePhy):
    """frozen `MultiRatePhy`를 상속한 lane-local 진단 facade (§9.3).

    한 인스턴스에는 RF-A 또는 RF-B 중 정확히 한 lane의 sample만 들어간다.
    `encode_packet()`은 request `packet_id`의 immutable route entry를 먼저 exact
    lookup하고 frozen PHY delegate를 정확히 한 번 호출한 뒤, 성공한 같은
    `BasebandBurst`의 `(packet_id,burst_id)`를 route sidecar에 atomic bind한다.
    """

    __slots__ = ("_diagnostic_sink", "_route_sidecar", "_tx_only")

    def __init__(
        self,
        profiles,
        time_source,
        id_source,
        *,
        diagnostic_sink: Stage7PhyDiagnosticSink,
        route_sidecar=None,
        tx_only: bool = False,
    ) -> None:
        super().__init__(profiles, time_source, id_source)
        self._diagnostic_sink = diagnostic_sink
        self._route_sidecar = route_sidecar
        self._tx_only = tx_only

    @property
    def diagnostic_sink(self) -> Stage7PhyDiagnosticSink:
        return self._diagnostic_sink

    # --- TX: Packet→Burst route 결속 (§15) ------------------------------------

    def encode_packet(self, request):
        if self._route_sidecar is not None:
            looked_up = self._route_sidecar.lookup_packet(request.packet_id.value)
            if looked_up.is_err:
                return looked_up
        result = super().encode_packet(request)
        if result.is_err:
            return result
        if self._route_sidecar is not None:
            burst = result.value
            if burst.packet_id != request.packet_id:
                return _private_error(
                    Stage7PrivateErrorCode.LANE_ROUTE_INVALID,
                    "encode 결과의 Packet ID가 요청과 다릅니다",
                )
            bound = self._route_sidecar.bind_burst(
                request.packet_id.value, burst.burst_id.value
            )
            if bound.is_err:
                return bound
        return result

    # --- RX: 진단 기록 (§9.3) --------------------------------------------------

    def _build_recovered_set(self, request, state, rate, recovered):
        sink = self._diagnostic_sink
        if sink is None or not sink.configured:
            return _private_error(
                Stage7PrivateErrorCode.PHY_DIAGNOSTIC_UNAVAILABLE,
                "diagnostic sink가 configure되지 않았습니다",
            )
        if not sink.has_window:
            # 정상 recovered PHY attempt와 diagnostic의 1:1 결속이 깨졌다 (§9.3).
            return _private_error(
                Stage7PrivateErrorCode.PHY_DIAGNOSTIC_UNAVAILABLE,
                "recovered attempt가 lane window context에 결속되지 않았습니다",
            )
        if True:
            t0_ns = sink.current_t0_ns
            for start_offset_samples, result in recovered:
                attempt_index = sink.next_attempt_index()
                burst_start = start_offset_samples  # PHY stream 절대 offset (float)
                delta_scaled = burst_start * 1_000_000_000.0 / rate
                # §13.3: half-away-from-zero nanosecond 반올림. float 입력을
                # 분수화하지 않고 동일 규칙으로 반올림한다.
                if delta_scaled >= 0.0:
                    delta_ns = int(delta_scaled + 0.5)
                else:
                    delta_ns = -int(-delta_scaled + 0.5)
                sample0_ns = (t0_ns if t0_ns is not None else 0) + delta_ns
                packet_digest = hashlib.sha256(result.packet).hexdigest()
                sink.record(
                    Stage7PhyAttemptRecord(
                        lane_generation=sink.lane_generation,
                        rx_window_key=sink.current_window_key,
                        lane_index=sink.lane_index,
                        phy_attempt_index=attempt_index,
                        link_id=result.link_id,
                        packet_digest=packet_digest,
                        cfo_hz_hex=float(result.cfo_hz).hex(),
                        cfo_hz=float(result.cfo_hz),
                        phy_stream_buffer_offset=int(start_offset_samples // 1) if start_offset_samples >= 0 else -int((-start_offset_samples) // 1),
                        training_epoch_samples_hex=float(result.training_epoch_samples).hex(),
                        rho_hat_hex=float(result.rho_hat).hex(),
                        stream_epoch_id=sink.current_epoch_id,
                        stream_t0_hardware_ns=t0_ns if t0_ns is not None else 0,
                        rx_sample0_hardware_ns=sample0_ns,
                        estimator_status="RECOVERED",
                    )
                )
        return super()._build_recovered_set(request, state, rate, recovered)


class Stage7LaneRxIngress:
    """child window의 immutable private context와 append-only segment map (§15.1.2)."""

    __slots__ = (
        "run_id",
        "endpoint_id",
        "b210_serial",
        "lane_index",
        "lane_generation",
        "radio_generation",
        "_contexts",
        "_segments",
        "_epoch_counter",
        "_current_epoch",
        "_epoch_t0",
        "_expected_hardware_clock_id",
    )

    def __init__(
        self,
        *,
        run_id: str,
        endpoint_id: str,
        b210_serial: str,
        lane_index: int,
        lane_generation: int,
        radio_generation: int,
        expected_hardware_clock_id: str,
    ) -> None:
        self.run_id = run_id
        self.endpoint_id = endpoint_id
        self.b210_serial = b210_serial
        self.lane_index = lane_index
        self.lane_generation = lane_generation
        self.radio_generation = radio_generation
        self._contexts = {}
        self._segments = []
        self._epoch_counter = 0
        self._current_epoch = None
        self._epoch_t0 = {}
        self._expected_hardware_clock_id = expected_hardware_clock_id

    # --- window admission ----------------------------------------------------

    def admit_window(
        self,
        rx_window_key: tuple,
        *,
        timing_bridge_kind: str,
        timing_clock_id: str | None,
        acquisition_attempt: int | None,
        bridge_generation: int | None,
    ):
        if timing_bridge_kind == "NONE":
            if timing_clock_id is not None or acquisition_attempt is not None or bridge_generation is not None:
                return self._context_invalid("NONE bridge에는 timing 식별자가 없어야 합니다")
        elif timing_bridge_kind == "PROVISIONAL":
            if timing_clock_id is None or acquisition_attempt is None or bridge_generation is not None:
                return self._context_invalid("PROVISIONAL bridge 식별자 조합이 잘못되었습니다")
        elif timing_bridge_kind == "ACTIVE":
            if timing_clock_id is None or acquisition_attempt is not None or bridge_generation is None:
                return self._context_invalid("ACTIVE bridge 식별자 조합이 잘못되었습니다")
        else:
            return self._context_invalid("timing_bridge_kind가 잘못되었습니다")
        if rx_window_key in self._contexts:
            return self._context_invalid("이미 admitted된 rx_window_key입니다")
        self._contexts[rx_window_key] = {
            "context_type": "STAGE7_RX_INGRESS_CONTEXT_V1",
            "run_id": self.run_id,
            "endpoint_id": self.endpoint_id,
            "b210_serial": self.b210_serial,
            "lane_generation": self.lane_generation,
            "radio_generation": self.radio_generation,
            "lane_index": self.lane_index,
            "rx_window_key": rx_window_key,
            "timing_bridge_kind": timing_bridge_kind,
            "timing_clock_id": timing_clock_id,
            "acquisition_attempt": acquisition_attempt,
            "bridge_generation": bridge_generation,
            "stream_epoch_id": None,
            "blocks": [],
            "state": "ADMITTED",
        }
        return Ok(None)

    def _context_invalid(self, message: str, **details):
        return _private_error(
            Stage7PrivateErrorCode.RX_LANE_CONTEXT_INVALID, message, **details
        )

    # --- raw block ingest ----------------------------------------------------

    def observe_block(self, rx_window_key: tuple, received):
        """frozen bridge에 전달하기 **전에** raw hardware timestamp를 보존한다."""

        context = self._contexts.get(rx_window_key)
        if context is None:
            return self._context_invalid("admitted context가 없는 sample block입니다")
        hardware = received.hardware_received_at
        if hardware.clock_domain is not ClockDomain.RADIO_HARDWARE:
            self._invalidate_epoch(context)
            return self._context_invalid(
                "hardware_received_at이 RADIO_HARDWARE domain이 아닙니다"
            )
        expected_clock_id = self._expected_hardware_clock_id
        if callable(expected_clock_id):
            expected_clock_id = expected_clock_id()
        if hardware.clock_id != expected_clock_id:
            self._invalidate_epoch(context)
            return self._context_invalid(
                "hardware clock ID가 이 B210 raw clock과 다릅니다",
                clock_id=hardware.clock_id,
            )
        block = received.samples
        hardware_ns = hardware.total_nanoseconds()
        if self._current_epoch is None:
            self._epoch_counter += 1
            self._current_epoch = {
                "epoch_id": self._epoch_counter,
                "device_offset": 0,
                "t0_hardware_ns": hardware_ns,
            }
            self._epoch_t0[self._epoch_counter] = hardware_ns
        else:
            expected = self._expected_next_ns()
            if abs(hardware_ns - expected) > 1:
                if not context["blocks"]:
                    # 새 window의 첫 block: finite stream이 새로 시작한 것이므로
                    # 새 streaming epoch을 연다 (§15.1.2 — 연속 window만 같은
                    # epoch을 공유한다).
                    self._epoch_counter += 1
                    self._current_epoch = {
                        "epoch_id": self._epoch_counter,
                        "device_offset": 0,
                        "t0_hardware_ns": hardware_ns,
                    }
                    self._epoch_t0[self._epoch_counter] = hardware_ns
                else:
                    # window 중간의 overlap/gap: streaming epoch 전체 invalid.
                    self._invalidate_epoch(context)
                    return Ok(None)
        epoch = self._current_epoch
        context["stream_epoch_id"] = epoch["epoch_id"]
        context["blocks"].append(
            {
                "device_stream_offset": epoch["device_offset"],
                "device_sample_count": block.sample_count,
                "first_sample_hardware_ns": hardware_ns,
            }
        )
        context["state"] = "SAMPLES_BOUND"
        epoch["device_offset"] += block.sample_count
        return Ok(None)

    def _expected_next_ns(self) -> int:
        epoch = self._current_epoch
        exact = Fraction(epoch["t0_hardware_ns"]) + Fraction(
            epoch["device_offset"] * 1_000_000_000, resampling.DEVICE_SAMPLE_RATE_HZ
        )
        floor = exact.numerator // exact.denominator
        remainder = exact - floor
        return int(floor) + (1 if remainder * 2 >= 1 else 0)

    def _invalidate_epoch(self, context) -> None:
        context["state"] = "INVALID"
        context["blocks"] = []
        self._current_epoch = None

    # --- clean terminal → segment promote ------------------------------------

    def promote_window(self, rx_window_key: tuple, link_id_family: int):
        """frozen bridge가 정확히 8,192 device samples를 한 PHY request로 만들 때
        raw block들을 하나의 completed-window `segment`로 atomic promote한다."""

        context = self._contexts.get(rx_window_key)
        if context is None or context["state"] in ("INVALID", "TERMINAL"):
            return self._context_invalid("promote할 수 있는 clean context가 없습니다")
        blocks = context["blocks"]
        total = sum(block["device_sample_count"] for block in blocks)
        if not blocks or total != resampling.DEVICE_SLOT_SAMPLES:
            self._invalidate_epoch(context)
            return self._context_invalid(
                "completed window가 정확히 8,192 device samples가 아닙니다",
                buffered=total,
            )
        device_offset = blocks[0]["device_stream_offset"]
        first_ns = blocks[0]["first_sample_hardware_ns"]
        if link_id_family == 11:
            if device_offset % 4 != 0 or resampling.DEVICE_SLOT_SAMPLES % 4 != 0:
                self._invalidate_epoch(context)
                return self._context_invalid(
                    "Link 11 device offset/count가 4의 배수가 아닙니다",
                    device_stream_offset=device_offset,
                )
            phy_offset = device_offset // 4
            phy_count = resampling.DEVICE_SLOT_SAMPLES // 4
            phy_rate = resampling.NATIVE_SAMPLE_RATE_HZ
        else:
            phy_offset = device_offset
            phy_count = resampling.DEVICE_SLOT_SAMPLES
            phy_rate = resampling.DEVICE_SAMPLE_RATE_HZ
        segment = Stage7RxSegment(
            rx_window_key=rx_window_key,
            device_stream_offset=device_offset,
            device_sample_count=resampling.DEVICE_SLOT_SAMPLES,
            device_sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            phy_stream_offset=phy_offset,
            phy_sample_count=phy_count,
            phy_sample_rate_hz=phy_rate,
            first_sample_hardware_ns=first_ns,
        )
        self._segments.append(segment)
        context["state"] = "PHY_ATTEMPT_BOUND"
        return Ok((segment, context["stream_epoch_id"]))

    def epoch_t0_hardware_ns(self, stream_epoch_id: int) -> int | None:
        return self._epoch_t0.get(stream_epoch_id)

    def discard_window(self, rx_window_key: tuple, reason: str) -> None:
        context = self._contexts.pop(rx_window_key, None)
        if context is not None and context["state"] not in ("PHY_ATTEMPT_BOUND", "TERMINAL"):
            # 미완성 segment/attempt context는 즉시 폐기하고 epoch도 닫는다.
            self._current_epoch = None

    def finalize_window(self, rx_window_key: tuple) -> None:
        context = self._contexts.get(rx_window_key)
        if context is not None:
            context["state"] = "TERMINAL"
            self._contexts.pop(rx_window_key, None)

    def segments(self) -> tuple:
        return tuple(self._segments)

    def context_count(self) -> int:
        return len(self._contexts)


__all__ = (
    "Stage7DiagnosticPhyFacade",
    "Stage7LaneRxIngress",
    "Stage7PhyAttemptRecord",
    "Stage7PhyDiagnosticSink",
    "Stage7RxSegment",
    "half_away_from_zero_ns",
)
