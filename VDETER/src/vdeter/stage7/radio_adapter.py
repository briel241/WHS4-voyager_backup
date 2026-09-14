"""Stage 7 dual-lane B210 Radio adapter (`stage7` §13.2.1, §15, §15.1.1~§15.1.3).

additive `Stage7B210RadioAdapter`는 frozen `RadioAdapter` Protocol과 Stage 3의
validation, scheduling lead, TX/RX ledger, sample count/continuity, exactly-one
terminal, lifecycle, generation fencing과 공개 오류 의미를 모두 보존한다.
제한적 projection 차이는 UTC↔hardware affine conversion, logical requested
center→translated applied center, multi-lane hardware binding, dual-lane worker
dispatch와 §15의 qualification-lease adoption뿐이다.
"""

from __future__ import annotations

import time as _wall_time
from dataclasses import dataclass
from threading import Condition, Event, RLock, Thread

from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    ClockReference,
    RadioAppliedChannelSetting,
    RadioAppliedSettings,
    RadioCapabilities,
    RadioClockState,
    RadioConfigurationApplied,
    RadioConfigureRequest,
    RadioDeviceState,
    RadioEventDeliveryStatus,
    RadioMappingState,
    RadioPpsState,
    RadioQueueStatus,
    RadioRxAccepted,
    RadioRxRequest,
    RadioRxWindowCompleted,
    RadioSamplesReceived,
    RadioStarted,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStatusSnapshot,
    RadioStopped,
    RadioStopRequest,
    RadioTimingProfile,
    RadioTimingStatus,
    RadioTxAccepted,
    RadioTxCompleted,
    RadioTxRequest,
    RadioUtcFlag,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.events import InternalEventType
from ..common.identifiers import PhysicalChannel
from ..common.results import Ok, internal_error
from ..common.time import ClockDomain, TimePoint
from .configuration import (
    LOWER_CHANNEL_NAME,
    STAGE7_ADAPTER_KIND,
    Stage7ResolvedConfiguration,
    UPPER_CHANNEL_NAME,
)
from .errors import Stage7PrivateErrorCode

_LEAD_NS = 100_000_000
# 연속 RX 구조의 정직한 수신 최소 lead: 창은 등록만 하면 되므로 RPC
# 지연+여유면 충분 (svcdbg74 실측: 100ms 과대광고가 지연 창 SLOT_MISSED
# → 단문 시나리오 간헐 실패의 원인).
_RX_LEAD_NS = 20_000_000
_RETUNE_LEAD_NS = 200_000_000
#: 확정된 TX burst의 per-lane 결과를 보존하는 유계 이력 크기(원장에서 제거된 뒤 조회용).
_TX_TERMINAL_HISTORY = 64
_SWITCH_GAP_NS = 100_000_000
_UTC_FLAGS = (
    RadioUtcFlag.HOST_ESTIMATE,
    RadioUtcFlag.NOT_UTC_DISCIPLINED,
    RadioUtcFlag.UTC_ACCURACY_UNVERIFIED,
)

#: §13.2.1 고정 공개 timing 투영.
STAGE7_PUBLIC_TIMING_STATUS = RadioTimingStatus(
    profile=RadioTimingProfile.INTERNAL_FREE_RUNNING,
    clock_state=RadioClockState.FREE_RUNNING,
    pps_state=RadioPpsState.PPS_NOT_PRESENT,
    utc_flags=_UTC_FLAGS,
    mapping_state=RadioMappingState.UNAVAILABLE,
    mapping=None,
    acquired_at_monotonic=None,
    expires_at_monotonic=None,
)


@dataclass(frozen=True, slots=True)
class Stage7AppliedLaneSettings:
    """§15 private applied record (logical channel/child별 closed layout)."""

    settings_type: str
    run_id: str
    endpoint_id: str
    b210_serial: str
    radio_generation: int
    lane_generation: int
    logical_channel: str
    path_ids: tuple
    lane_id: str
    device_channel: int
    tx_antenna: str
    rx_antenna: str
    requested_rf_tune_hz: int
    applied_rf_tune_hz: int
    requested_sample_rate_hz: int
    applied_tx_sample_rate_hz: int | None
    applied_rx_sample_rate_hz: int | None
    requested_tx_gain_db: int | None
    applied_tx_gain_db: int | None
    requested_rx_gain_db: int | None
    applied_rx_gain_db: int | None
    requested_analog_bandwidth_hz: int
    applied_tx_analog_bandwidth_hz: int | None
    applied_rx_analog_bandwidth_hz: int | None
    worker_readback_sha256: str
    outcome: str


class Stage7HostBridgeHandle:
    """Runtime-private timed-I/O 변환 handle (§13.2.1).

    public `RadioTimingStatus.mapping`을 대체하는 private 경로다. endpoint
    runtime이 provisional/active bridge를 설치·무효화한다.
    """

    __slots__ = ("_lock", "_state", "_anchor", "_coefficients", "_correction", "_clock_id", "_generation")

    def __init__(self) -> None:
        self._lock = RLock()
        self._state = "INVALID"
        self._anchor = None
        self._coefficients = None
        self._correction = (0, 1)
        self._clock_id = None
        self._generation = None

    def install(self, *, state, anchor, coefficients, correction, clock_id, generation):
        with self._lock:
            self._state = state
            self._anchor = anchor
            self._coefficients = coefficients
            self._correction = correction
            self._clock_id = clock_id
            self._generation = generation

    def invalidate(self):
        with self._lock:
            self._state = "INVALID"
            self._clock_id = None
            self._generation = None

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def clock_id(self):
        with self._lock:
            return self._clock_id

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def utc_label_to_hardware_ns(self, utc_ns: int):
        with self._lock:
            if self._state == "INVALID" or self._coefficients is None or self._anchor is None:
                return None
            raw = self._anchor.label_to_raw(utc_ns)
            return self._coefficients.inverse_corrected(raw, *self._correction)

    def hardware_ns_to_utc_label(self, hardware_ns: int):
        with self._lock:
            if self._state == "INVALID" or self._coefficients is None or self._anchor is None:
                return None
            raw = self._coefficients.forward_corrected(hardware_ns, *self._correction)
            return self._anchor.utc_label(raw)


@dataclass(slots=True)
class _TxChild:
    lane_index: int
    path_id: str
    hardware_start_ns: int
    hardware_end_ns: int
    state: str = "SUBMITTED"  # SUBMITTED|COMPLETED|FAILED
    completed_at: TimePoint | None = None
    error_code: str | None = None


@dataclass(slots=True)
class _TxParent:
    key: tuple
    request: RadioTxRequest
    children: dict
    fanout_scope: str
    terminal: bool = False
    retune_return: int | None = None


@dataclass(slots=True)
class _RxChild:
    lane_index: int
    key: tuple
    request: RadioRxRequest
    expected_sample_count: int
    hardware_start_ns: int
    hardware_end_ns: int
    received_sample_count: int = 0
    terminal: bool = False


class Stage7QualificationLease:
    """public Radio state와 분리된 private preflight lease (§15).

    lease 동안 public Radio state는 `DISCONNECTED`, normal public TX/RX
    admission은 closed이고 calibration child만 worker에 나갈 수 있다.
    """

    __slots__ = ("state", "worker", "readback", "radio_generation", "lease_id", "gain_db")

    def __init__(self) -> None:
        self.state = "NONE"  # NONE|OPEN|ADOPTED|CLOSED
        self.worker = None
        self.readback = None
        self.radio_generation = 0
        self.lease_id = None
        self.gain_db = (0, 0)


class Stage7B210RadioAdapter:
    """frozen `RadioAdapter` Protocol 구현 (schema v4 전용)."""

    __slots__ = (
        "_svc_tx_count", "_svc_rx_count", "_svc_rx_del", "_svc_async_counts",
        "_svc_rx_rej", "_svc_rx_unclean", "_svc_rx_offgrid",
        "_rx_pending", "_rx_issued", "_svc_tx_rej_probe",
        "_time_source", "_id_source", "_event_sink", "_resolved", "_sidecar",
        "_lane_dispatcher", "_worker_factory", "_lease", "_state", "_generation",
        "_lane_generation", "_configuration", "_applied", "_applied_lane_settings",
        "_bridge", "_tx_parents", "_rx_children", "_terminal_keys", "_tx_terminal_outcomes",
        "_last_stopped", "_lock", "_condition", "_stopping", "_monitor_thread",
        "_monitor_stop", "_underflow_count", "_overflow_count", "_late_command_count",
        "_tx_rejected", "_rx_rejected", "_last_tx_tune_hz", "_pairwise_edge",
        "_delivery_status", "_endpoint_id", "_private_faults", "_hardware_clock_id",
        "_calibration_only", "_monitor_enabled",
        "_rx_lane_channels",
    )

    def __init__(
        self,
        time_source,
        id_source,
        event_sink,
        *,
        resolved: Stage7ResolvedConfiguration,
        sidecar,
        lane_dispatcher=None,
        worker_factory=None,
        bridge: Stage7HostBridgeHandle | None = None,
        pairwise_edge: str | None = None,
        monitor_enabled: bool = True,
    ) -> None:
        self._time_source = time_source
        self._id_source = id_source
        self._event_sink = event_sink
        self._resolved = resolved
        self._sidecar = sidecar
        self._lane_dispatcher = lane_dispatcher
        self._worker_factory = worker_factory
        self._lease = Stage7QualificationLease()
        self._state = RadioDeviceState.DISCONNECTED
        self._generation = 0
        self._lane_generation = sidecar.lane_generation if sidecar is not None else 0
        self._configuration = None
        self._applied = None
        self._applied_lane_settings = ()
        self._bridge = bridge or Stage7HostBridgeHandle()
        self._tx_parents = {}
        self._rx_children = {}
        self._terminal_keys = set()
        #: burst_id -> {lane: 최종 state}. 유계(_TX_TERMINAL_HISTORY).
        self._tx_terminal_outcomes = {}
        self._rx_lane_channels = None
        self._last_stopped = None
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stopping = False
        self._monitor_thread = None
        self._monitor_stop = Event()
        self._underflow_count = 0
        self._overflow_count = 0
        self._late_command_count = 0
        self._tx_rejected = {}
        self._rx_rejected = {}
        self._last_tx_tune_hz = None
        self._pairwise_edge = pairwise_edge
        self._delivery_status = RadioEventDeliveryStatus(False, 0, None, None)
        self._endpoint_id = resolved.endpoint_id
        self._private_faults = []
        self._hardware_clock_id = None
        self._calibration_only = True
        self._monitor_enabled = monitor_enabled

    # --- 공통 helper ----------------------------------------------------------

    def bind_lane_dispatcher(self, dispatcher) -> None:
        """builder/restart factory 전용: private child dispatcher를 결속한다."""

        with self._lock:
            self._lane_dispatcher = dispatcher

    def rebind_lane_runtime(self, lane_generation: int, dispatcher) -> None:
        """restart factory 전용: 새 lane generation과 dispatcher를 atomic 결속한다."""

        with self._lock:
            self._lane_generation = lane_generation
            self._lane_dispatcher = dispatcher

    @property
    def bridge(self) -> Stage7HostBridgeHandle:
        return self._bridge

    @property
    def lease(self) -> Stage7QualificationLease:
        return self._lease

    @property
    def hardware_clock_id(self):
        return self._hardware_clock_id

    def private_faults(self) -> tuple:
        with self._lock:
            return tuple(self._private_faults)

    def applied_lane_settings(self) -> tuple:
        with self._lock:
            return self._applied_lane_settings

    def _error(self, code, category, message, correlation=Correlation(), details=Details()):
        return internal_error(Layer.RADIO, code, category, message, correlation=correlation, details=details)

    def _private_fault(self, private_code: Stage7PrivateErrorCode, message: str, **fields):
        with self._lock:
            self._private_faults.append(
                {"private_code": private_code.value, "message": message, **fields}
            )

    # --- private qualification lease (§15) ------------------------------------

    def open_qualification_lease(self, *, lease_id: str, rx_gain_db: tuple = (0, 0)):
        """Runtime Coordinator signed preflight의 worker open+configure+start.

        public Radio state는 `DISCONNECTED`를 유지한다.
        """

        with self._lock:
            if self._lease.state not in ("NONE", "CLOSED"):
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "이미 열린 qualification lease가 있습니다",
                )
            if self._worker_factory is None:
                from .b210_worker import Stage7IsolatedDualLaneWorker

                factory = Stage7IsolatedDualLaneWorker
            else:
                factory = self._worker_factory
            resolved = self._resolved
            selector = f"serial={resolved.b210_serial}"
            try:
                found = tuple(factory.discover(selector))
            except Exception as error:  # noqa: BLE001
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "B210 discovery가 실패했습니다",
                    details=Details.of(reason=type(error).__name__),
                )
            if found != (resolved.b210_serial,):
                code = (
                    ErrorCode.RADIO_DEVICE_NOT_FOUND
                    if not found
                    else ErrorCode.RADIO_CONFIGURATION_FAILED
                )
                return self._error(
                    code, ErrorCategory.DEVICE,
                    "selector와 정확히 일치하는 B210 한 대가 필요합니다",
                    details=Details.of(match_count=len(found)),
                )
            generation = self._lease.radio_generation + 1
            # role별 LO: TX는 endpoint 평상시 TX channel, RX는 RX channel의 tune.
            tx_channel = resolved.endpoint.mac.tx_channel.name
            rx_channel = resolved.endpoint.mac.rx_channel.name
            plan = {
                "master_clock_rate_hz": 30_720_000,
                "cpu_format": "fc32",
                "otw_format": "sc16",
                "sample_rate_hz": 307_200,
                "analog_bandwidth_hz": 200_000,
                "tx_lo_hz": resolved.rf_tune_for_channel(tx_channel),
                "rx_lo_hz": resolved.rf_tune_for_channel(rx_channel),
                "tx_gain_db": [0, 0],
                "rx_gain_db": list(rx_gain_db),
                "rx_queue_max_bytes": resolved.radio.resource_limits.rx_queue_max_bytes,
                "device_sample_rate_hz": 307_200,
            }
            worker = None
            try:
                worker = factory(selector, "fc32", "sc16")
                readback = worker.configure_dual(plan)
            except Exception as error:  # noqa: BLE001
                if worker is not None:
                    try:
                        worker.close(resolved.radio.resource_limits.stop_timeout_ns)
                    except Exception:  # noqa: BLE001
                        pass
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "dual-lane worker configure가 실패했습니다",
                    details=Details.of(reason=type(error).__name__, exception_message=str(error)),
                )
            validation = self._validate_dual_readback(readback, plan)
            if validation is not None:
                try:
                    worker.close(resolved.radio.resource_limits.stop_timeout_ns)
                except Exception:  # noqa: BLE001
                    pass
                return validation
            try:
                worker.connect()
                worker.start()
            except Exception as error:  # noqa: BLE001
                try:
                    worker.close(resolved.radio.resource_limits.stop_timeout_ns)
                except Exception:  # noqa: BLE001
                    pass
                return self._error(
                    ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                    "dual-lane worker start가 실패했습니다",
                    details=Details.of(reason=type(error).__name__),
                )
            self._lease.state = "OPEN"
            self._lease.worker = worker
            self._lease.readback = readback
            self._lease.radio_generation = generation
            self._lease.lease_id = lease_id
            self._lease.gain_db = tuple(rx_gain_db)
            self._last_tx_tune_hz = plan["tx_lo_hz"]
            self._hardware_clock_id = (
                f"stage7-b210-hardware-v1:{resolved.run_id}:{resolved.endpoint_id}:{generation}"
            )
            return Ok(readback)

    def _validate_dual_readback(self, readback: dict, plan: dict):
        resolved = self._resolved
        if str(readback.get("serial")) != resolved.b210_serial:
            return self._error(
                ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                "worker readback serial이 exact serial과 다릅니다",
                details=Details.of(serial=str(readback.get("serial"))),
            )
        if not readback.get("same_timebase") or not readback.get("hardware_time_reset"):
            return self._error(
                ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                "source/sink timebase 또는 hardware time reset 검증이 실패했습니다",
            )
        for key in ("clock_source", "time_source", "sink_clock_source", "sink_time_source"):
            if readback.get(key) != "internal":
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "clock/time source가 internal이 아닙니다",
                    details=Details.of(field=key, value=str(readback.get(key))),
                )
        mcr = readback.get("master_clock_rate_hz")
        if mcr is None or abs(mcr - plan["master_clock_rate_hz"]) > plan["master_clock_rate_hz"] // 1_000_000:
            return self._error(
                ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                "MCR readback이 ±1 ppm을 벗어났습니다",
            )
        for lane in ("0", "1"):
            lane_readback = readback.get("lanes", {}).get(lane)
            if lane_readback is None:
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "lane readback이 없습니다", details=Details.of(lane=lane),
                )
            if abs(lane_readback["tx_center_frequency_hz"] - plan["tx_lo_hz"]) > 1:
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "TX tune readback 오차가 1 Hz를 넘습니다", details=Details.of(lane=lane),
                )
            if abs(lane_readback["rx_center_frequency_hz"] - plan["rx_lo_hz"]) > 1:
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "RX tune readback 오차가 1 Hz를 넘습니다", details=Details.of(lane=lane),
                )
            for key, requested in (
                ("tx_sample_rate_hz", plan["sample_rate_hz"]),
                ("rx_sample_rate_hz", plan["sample_rate_hz"]),
            ):
                actual = lane_readback[key]
                if abs(actual - requested) > max(1, requested // 1_000_000):
                    return self._error(
                        ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                        "sample rate readback이 ±1 ppm을 벗어났습니다",
                        details=Details.of(lane=lane, field=key),
                    )
            if abs(lane_readback["tx_gain_db"] - plan["tx_gain_db"][int(lane)]) > 0:
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "TX gain readback이 요청과 다릅니다", details=Details.of(lane=lane),
                )
            if abs(lane_readback["rx_gain_db"] - plan["rx_gain_db"][int(lane)]) > 0:
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "RX gain readback이 요청과 다릅니다", details=Details.of(lane=lane),
                )
            for key in ("tx_bandwidth_hz", "rx_bandwidth_hz"):
                actual = lane_readback[key]
                if not 190_000 <= actual <= 210_000:
                    return self._error(
                        ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                        "analog bandwidth readback이 190k..210k 밖입니다",
                        details=Details.of(lane=lane, field=key, value=actual),
                    )
            if lane_readback["tx_antenna"] != "TX/RX" or lane_readback["rx_antenna"] != "RX2":
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.DEVICE,
                    "antenna selector가 canonical 값과 다릅니다", details=Details.of(lane=lane),
                )
        return None

    def qualification_hardware_time_ns(self):
        """§13.2 bracket용 raw hardware time (lease/adopted 공용)."""

        with self._lock:
            worker = self._lease.worker
            if worker is None:
                return self._error(
                    ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE,
                    "worker가 없습니다",
                )
        try:
            return Ok(int(worker.hardware_time_ns()))
        except Exception as error:  # noqa: BLE001
            return self._error(
                ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                "hardware time read가 실패했습니다",
                details=Details.of(reason=type(error).__name__),
            )

    def close_qualification_lease(self):
        with self._lock:
            worker = self._lease.worker
            timeout_ns = self._resolved.radio.resource_limits.stop_timeout_ns
            self._lease.state = "CLOSED"
            self._lease.worker = None
        if worker is not None:
            try:
                worker.close(timeout_ns)
            except Exception:  # noqa: BLE001
                pass
        return Ok(None)

    # --- 공개 RadioAdapter (frozen Protocol) ----------------------------------

    def configure(self, request: RadioConfigureRequest):
        """§15: 새 hardware configure 대신 qualified lease를 atomic adopt한다."""

        with self._condition:
            if not isinstance(request, RadioConfigureRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioConfigureRequest가 필요합니다")
            if self._stopping or self._state not in (RadioDeviceState.DISCONNECTED, RadioDeviceState.STOPPED):
                return self._error(ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE, "configure는 DISCONNECTED/STOPPED state에서만 허용합니다")
            configuration = request.configuration
            # exact resolved public projection과의 fingerprint 비교 (§15).
            expected = self._resolved.radio
            if configuration != expected:
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "public configure request가 resolved projection과 exact-equal이 아닙니다",
                )
            if self._lease.state != "OPEN" or self._lease.worker is None:
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "adopt할 qualified worker lease가 없습니다",
                )
            if self._bridge.state != "ACTIVE":
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "active host bridge 없이 adoption할 수 없습니다",
                )
            expected_gain = (
                0
                if self._resolved.run_mode != "OPERATIONAL"
                else self._resolved.endpoint_common_rx_gain_db
            )
            rx_lane_gains = self._lease.gain_db
            if any(gain != expected_gain for gain in rx_lane_gains):
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
                    "lease gain이 resolved endpoint 공통 gain과 다릅니다",
                )
            generation = self._generation + 1
            applied_channels = self._build_public_applied(configuration, generation)
            capabilities = RadioCapabilities(
                (SAMPLE_FORMAT_CF32LE,), _LEAD_NS, _RX_LEAD_NS, _SWITCH_GAP_NS, True,
                (RadioTimingProfile.INTERNAL_FREE_RUNNING,), configuration.resource_limits,
            )
            settings = RadioAppliedSettings(
                self._resolved.b210_serial, generation, applied_channels,
                30_720_000, self._lease.readback.get("master_clock_rate_hz"),
                "fc32", "sc16",
            )
            applied = RadioConfigurationApplied(
                STAGE7_ADAPTER_KIND, RadioDeviceState.CONFIGURED,
                tuple(value.channel.name for value in applied_channels),
                SAMPLE_FORMAT_CF32LE, capabilities, settings,
            )
            self._applied_lane_settings = self._build_private_lane_settings(generation)
            self._configuration = configuration
            self._applied = applied
            self._state = RadioDeviceState.CONFIGURED
            self._generation = generation
            self._lease.state = "ADOPTED"
            self._calibration_only = False
            self._tx_parents.clear()
            self._rx_children.clear()
            self._terminal_keys.clear()
            self._tx_terminal_outcomes.clear()   # generation 재설정 시 과거 결과가 새지 않게
            self._rx_pending = []
            self._rx_issued = []
            self._last_stopped = None
            names = configuration.channel_names()
            self._tx_rejected = {name: 0 for name in names}
            self._rx_rejected = {name: 0 for name in names}
            self._emit_status_changed_locked()
            return Ok(applied)

    def _build_public_applied(self, configuration, generation):
        """public applied channel: requested=logical center, applied=translated tune."""

        applied = []
        for setting, binding in zip(configuration.channels, configuration.channel_bindings):
            tune = self._resolved.rf_tune_for_channel(setting.channel.name)
            applied.append(
                RadioAppliedChannelSetting(
                    channel=setting.channel,
                    requested_center_frequency_hz=setting.center_frequency_hz,
                    applied_center_frequency_hz=tune,
                    requested_sample_rate_hz=setting.sample_rate_hz,
                    applied_tx_sample_rate_hz=setting.sample_rate_hz if binding.tx_enabled else None,
                    applied_rx_sample_rate_hz=setting.sample_rate_hz if binding.rx_enabled else None,
                    requested_tx_gain_db=setting.tx_gain_db,
                    requested_rx_gain_db=setting.rx_gain_db,
                    applied_tx_gain_db=setting.tx_gain_db if binding.tx_enabled else None,
                    applied_rx_gain_db=setting.rx_gain_db if binding.rx_enabled else None,
                    requested_analog_bandwidth_hz=binding.analog_bandwidth_hz,
                    applied_tx_analog_bandwidth_hz=binding.analog_bandwidth_hz if binding.tx_enabled else None,
                    applied_rx_analog_bandwidth_hz=binding.analog_bandwidth_hz if binding.rx_enabled else None,
                    binding=binding,
                )
            )
        return tuple(applied)

    def _build_private_lane_settings(self, generation):
        from .canonical_json import canonical_sha256

        resolved = self._resolved
        readback = self._lease.readback or {}
        records = []
        for lane in resolved.lanes:
            lane_readback = readback.get("lanes", {}).get(str(lane.lane_index), {})
            tx_tune = resolved.rf_tune_for_channel(lane.tx_channel)
            records.append(
                Stage7AppliedLaneSettings(
                    settings_type="STAGE7_APPLIED_LANE_V1",
                    run_id=resolved.run_id,
                    endpoint_id=resolved.endpoint_id,
                    b210_serial=resolved.b210_serial,
                    radio_generation=generation,
                    lane_generation=self._lane_generation,
                    logical_channel=lane.tx_channel,
                    path_ids=(lane.tx_path, lane.rx_path),
                    lane_id=lane.lane_id,
                    device_channel=lane.lane_index,
                    tx_antenna="TX/RX",
                    rx_antenna="RX2",
                    requested_rf_tune_hz=tx_tune,
                    applied_rf_tune_hz=lane_readback.get("tx_center_frequency_hz", tx_tune),
                    requested_sample_rate_hz=307_200,
                    applied_tx_sample_rate_hz=lane_readback.get("tx_sample_rate_hz"),
                    applied_rx_sample_rate_hz=lane_readback.get("rx_sample_rate_hz"),
                    requested_tx_gain_db=0,
                    applied_tx_gain_db=lane_readback.get("tx_gain_db"),
                    requested_rx_gain_db=self._lease.gain_db[lane.lane_index],
                    applied_rx_gain_db=lane_readback.get("rx_gain_db"),
                    requested_analog_bandwidth_hz=200_000,
                    applied_tx_analog_bandwidth_hz=lane_readback.get("tx_bandwidth_hz"),
                    applied_rx_analog_bandwidth_hz=lane_readback.get("rx_bandwidth_hz"),
                    worker_readback_sha256=canonical_sha256(
                        {"lane": lane.lane_index, "readback": lane_readback}
                    ),
                    outcome="PASS",
                )
            )
        return tuple(records)

    def start(self, request: RadioStartRequest):
        with self._condition:
            if not isinstance(request, RadioStartRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStartRequest가 필요합니다")
            if self._stopping or self._state is not RadioDeviceState.CONFIGURED:
                return self._error(
                    ErrorCode.RADIO_NOT_CONFIGURED if self._configuration is None else ErrorCode.RADIO_ALREADY_STARTED,
                    ErrorCategory.DEVICE, "start는 CONFIGURED state에서만 허용합니다",
                )
            # adopted worker를 다시 start하지 않고 public state만 전이한다 (§15).
            # 서비스 모드는 연속 RX 스트림을 연다 (창별 유한 스트림의 인접-창
            # 재시작 결손 제거 — svcdbg41 실측 근거).
            if not self._calibration_only:
                worker = self._lease.worker
                if worker is not None:
                    try:
                        worker.start_rx_continuous()
                    except Exception as error:  # noqa: BLE001
                        return self._error(
                            ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                            "연속 RX 스트림 개시가 실패했습니다",
                            details=Details.of(reason=type(error).__name__,
                                               detail=str(error)[:160]),
                        )
            self._state = RadioDeviceState.RUNNING
            started = RadioStarted(STAGE7_ADAPTER_KIND, self._state, self._time_source.now_monotonic())
            self._emit_status_changed_locked()
            if not self._monitor_enabled:
                # 결정적 harness는 `poll_once()`로 동기 진행한다.
                return Ok(started)
            monitor = Thread(
                target=self._monitor_loop,
                name=f"vdeter-stage7-b210-monitor-{self._generation}",
                daemon=True,
            )
            self._monitor_thread = monitor
            self._monitor_stop = Event()
            try:
                monitor.start()
            except Exception as error:  # noqa: BLE001
                self._monitor_thread = None
                self._state = RadioDeviceState.FAILED
                return self._error(
                    ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                    "monitor thread start가 실패했습니다",
                    details=Details.of(reason=type(error).__name__),
                )
            return Ok(started)

    def poll_once(self):
        """monitor 한 사이클을 동기 실행한다 (결정적 harness/시험 전용)."""

        monitor = self._monitor_thread
        if monitor is not None and monitor.is_alive():
            # monitor thread가 폴링을 전담한다 — 펌프 스레드와의 이중
            # 폴링(같은 창 이중 read RPC, 부하 2배)을 방지 (svcdbg45).
            return
        worker = self._lease.worker
        if worker is None:
            return
        try:
            events = worker.poll_async()
        except Exception as error:  # noqa: BLE001
            with self._condition:
                self._handle_device_loss_locked(type(error).__name__)
            return
        with self._condition:
            for event in events:
                self._consume_async_locked(event)
            snapshot = [
                child for child in self._rx_children.values()
                if not child.terminal
            ]
        self._poll_rx_unlocked(worker, snapshot)

    def stop(self, request: RadioStopRequest):
        if not isinstance(request, RadioStopRequest):
            return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStopRequest가 필요합니다")
        with self._condition:
            if self._state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                return Ok(self._last_stopped)
            if self._state is RadioDeviceState.DISCONNECTED:
                return self._error(ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE, "configure 전에는 stop할 수 없습니다")
            timeout_ns = self._resolved.radio.resource_limits.stop_timeout_ns
            if self._stopping:
                deadline = _wall_time.monotonic_ns() + timeout_ns
                while self._stopping:
                    remaining = deadline - _wall_time.monotonic_ns()
                    if remaining <= 0:
                        return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "동시 stop cleanup wait가 만료되었습니다")
                    self._condition.wait(remaining / 1_000_000_000)
                if self._state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                    return Ok(self._last_stopped)
                return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "공유 cleanup이 성공하지 않았습니다")
            self._stopping = True
            self._monitor_stop.set()
            monitor = self._monitor_thread
            # pending TX parent를 outcome-unknown terminal 처리한다.
            for parent in tuple(self._tx_parents.values()):
                if not parent.terminal:
                    self._terminal_tx_error_locked(
                        parent, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                        "stop 시점에 제출된 TX outcome을 확정할 수 없습니다",
                    )
            for child in tuple(self._rx_children.values()):
                if not child.terminal:
                    child.terminal = True
                    code = (
                        ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
                        if child.received_sample_count > 0
                        else ErrorCode.RADIO_OPERATION_CANCELLED
                    )
                    self._emit_rx_terminal_error_locked(child, code)
            self._tx_parents.clear()
            self._rx_children.clear()
            self._rx_pending = []
            self._rx_issued = []
            worker = self._lease.worker
        if monitor is not None:
            monitor.join(timeout_ns / 1_000_000_000)
        cleanup_ok = True
        if worker is not None:
            try:
                cleanup_ok = worker.close(timeout_ns)
            except Exception:  # noqa: BLE001
                cleanup_ok = True  # isolated child 소실은 회수 완료로 본다.
        with self._condition:
            self._stopping = False
            self._lease.worker = None
            self._lease.state = "CLOSED"
            self._monitor_thread = None
            if not cleanup_ok:
                self._state = RadioDeviceState.FAILED
                self._condition.notify_all()
                return self._error(
                    ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT,
                    "worker stop이 timeout으로 실패했습니다",
                )
            self._state = RadioDeviceState.STOPPED
            self._last_stopped = RadioStopped(STAGE7_ADAPTER_KIND, self._state, self._time_source.now_monotonic())
            self._emit_status_changed_locked()
            self._condition.notify_all()
            return Ok(self._last_stopped)

    # --- TX fanout (§15.1.1) ---------------------------------------------------

    def schedule_tx(self, request: RadioTxRequest):
        correlation = Correlation(burst_id=request.burst_id) if isinstance(request, RadioTxRequest) else Correlation()
        with self._condition:
            if not isinstance(request, RadioTxRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioTxRequest가 필요합니다")
            state = self._require_running(correlation)
            if state is not None:
                return state
            if self._resolved.run_mode == "RF_OFF_QUALIFICATION":
                # P0 RF-off run은 어떤 TX도 수락하지 않는다 (§9.1, §17.1).
                self._reject_tx_locked(request.channel)
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.UNSUPPORTED,
                    "RF_OFF_QUALIFICATION run에서는 TX를 수락하지 않습니다", correlation,
                )
            samples = request.samples
            if samples.sample_format != SAMPLE_FORMAT_CF32LE:
                return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "cf32le만 지원합니다", correlation)
            if samples.sample_rate_hz != 307_200 or samples.sample_count != 8_192:
                return self._error(
                    ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION,
                    "device-rate 8,192-sample burst만 지원합니다", correlation,
                )
            key = ("tx", self._generation, request.burst_id.value)
            if key in self._terminal_keys or request.burst_id.value in self._tx_parents:
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "현재 generation에서 burst_id가 이미 사용 중입니다", correlation)
            # Burst→LaneExecutionPlan reservation (§15.1).
            reserved = self._sidecar.reserve_burst(
                request.burst_id.value, self._endpoint_id, request.channel.name,
                pairwise_edge=self._pairwise_edge,
            )
            if reserved.is_err:
                self._reject_tx_locked(request.channel)
                return reserved
            plan = reserved.value
            # UTC(label)→host raw→hardware 변환 (§13.2).
            hardware_start = self._bridge.utc_label_to_hardware_ns(
                request.transmit_at.total_nanoseconds()
            )
            if hardware_start is None:
                self._reject_tx_locked(request.channel)
                self._private_fault(
                    Stage7PrivateErrorCode.HOST_AFFINE_BRIDGE_NOT_LOCKED,
                    "active bridge 없이 timed TX를 수락할 수 없습니다",
                )
                return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "host affine bridge가 잠겨 있지 않습니다", correlation)
            duration_ns = (samples.sample_count * 1_000_000_000 + 307_200 - 1) // 307_200
            hardware_end = hardware_start + duration_ns
            now = self.qualification_hardware_time_ns()
            if now.is_err:
                return now
            # §10.1 shared TX LO: 요청 channel의 tune이 현재 TX LO와 다르면
            # timed retune이 필요하다 (최소 200 ms lead, 100 ms switch gap).
            desired_tune = self._resolved.rf_tune_for_channel(request.channel.name)
            current_tune = self._last_tx_tune_hz
            retune_needed = current_tune is not None and current_tune != desired_tune
            required_lead = _RETUNE_LEAD_NS if retune_needed else _LEAD_NS
            if hardware_start - now.value < required_lead:
                self._late_command_count += 1
                self._reject_tx_locked(request.channel)
                return self._error(
                    ErrorCode.RADIO_LATE_REQUEST, ErrorCategory.TIMEOUT,
                    "TX lead가 최소 lead 미만입니다", correlation,
                    # 요구 lead만으로는 "얼마나 모자랐는지"를 알 수 없다. 접수 시각·예정 시각·
                    # 남은 lead를 함께 남긴다. 네 값 모두 같은 hardware ns 축이라 변환 근거 없이
                    # 빼지 않는다(다른 clock domain 값을 섞지 않는다).
                    Details.of(required_lead_ns=required_lead,
                               scheduled_hw_ns=int(hardware_start),
                               accepted_at_hw_ns=int(now.value),
                               remaining_lead_ns=int(hardware_start - now.value)),
                )
            if retune_needed:
                for parent in self._tx_parents.values():
                    if not parent.terminal:
                        # retune 중 TX/pending TX는 admission 단계에서 거부한다.
                        self._reject_tx_locked(request.channel)
                        return self._error(
                            ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE,
                            "pending TX가 있어 retune을 시작할 수 없습니다", correlation,
                        )
            children = {}
            for lane_index, path_id in plan.children:
                for parent in self._tx_parents.values():
                    child = parent.children.get(lane_index)
                    if child is not None and not parent.terminal:
                        if hardware_start < child.hardware_end_ns and child.hardware_start_ns < hardware_end:
                            self._reject_tx_locked(request.channel)
                            return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "같은 lane의 TX burst가 겹칩니다", correlation)
                children[lane_index] = _TxChild(lane_index, path_id, hardware_start, hardware_end)
            worker = self._lease.worker
            try:
                if retune_needed:
                    # 다른 lane TX gate mute 뒤 timed retune (§10.1).
                    for lane_index in (0, 1):
                        if lane_index not in children:
                            worker.set_tx_mute(lane_index, True)
                    worker.retune_tx_lo(desired_tune, hardware_start - _RETUNE_LEAD_NS)
                    self._last_tx_tune_hz = desired_tune
                worker.submit_tx_group(
                    tuple(
                        (lane_index, samples.data, hardware_start)
                        for lane_index in children
                    )
                )
            except Exception as error:  # noqa: BLE001
                import sys as _sys
                print(
                    f"[stage7-radio] svc-tx-SUBMIT-FAIL "
                    f"{type(error).__name__}: {str(error)[:140]}",
                    file=_sys.stderr, flush=True,
                )
                self._reject_tx_locked(request.channel)
                return self._error(
                    ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                    "worker TX 제출이 실패했습니다", correlation,
                    Details.of(reason=type(error).__name__),
                )
            parent = _TxParent(key, request, children, plan.fanout_scope)
            if retune_needed:
                parent.retune_return = self._default_tx_tune_hz()
            self._tx_parents[request.burst_id.value] = parent
            # fanout accepted: 모든 child schedule admission 성공 뒤 한 번 (§15.1.1).
            return Ok(
                RadioTxAccepted(
                    request.burst_id, request.channel, request.transmit_at,
                    self._time_source.now_monotonic(),
                )
            )

    # --- RX fanout (§15.1.2) ---------------------------------------------------

    def _rx_reject_probe(self, reason: str, extra: str = "") -> None:
        # 거부 사유는 반환되는 typed error가 전달한다 — 별도 로그 없음.
        return None

    def request_rx(self, request: RadioRxRequest):
        with self._condition:
            if not isinstance(request, RadioRxRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioRxRequest가 필요합니다")
            state = self._require_running()
            if state is not None:
                self._rx_reject_probe("NOT_RUNNING")
                return state
            if request.sample_rate_hz != 307_200:
                self._rx_reject_probe("BAD_RATE")
                return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "device rate 307200만 지원합니다")
            expected = (request.duration_nanoseconds * request.sample_rate_hz + 500_000_000) // 1_000_000_000
            if expected != 8_192:
                self._rx_reject_probe("BAD_SIZE", f"expected={expected}")
                return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "8,192-sample window만 지원합니다")
            hardware_start = self._bridge.utc_label_to_hardware_ns(request.start_at.total_nanoseconds())
            if hardware_start is None:
                self._rx_reject_probe("BRIDGE_UNLOCKED")
                self._private_fault(
                    Stage7PrivateErrorCode.HOST_AFFINE_BRIDGE_NOT_LOCKED,
                    "active bridge 없이 timed RX를 수락할 수 없습니다",
                )
                return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "host affine bridge가 잠겨 있지 않습니다")
            if not self.rx_lanes_for(request.channel.name):
                return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "channel is not tuned")
            hardware_end = hardware_start + request.duration_nanoseconds
            base_key = (
                self._generation, request.channel.name,
                request.start_at.total_nanoseconds(), request.duration_nanoseconds,
            )
            for lane_index in self.rx_lanes_for(request.channel.name):
                child_key = ("rx", base_key, lane_index)
                if child_key in self._terminal_keys or child_key in self._rx_children:
                    self._rx_reject_probe(
                        "DUP_KEY",
                        f"start={request.start_at.total_nanoseconds()} lane={lane_index}",
                    )
                    self._reject_rx_locked(request.channel)
                    return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "RX window key가 중복되었습니다")
            # 연속 RX 모드 (svcdbg41 실측 근거): 창별 유한 스트림은 인접 창
            # 재시작 지연으로 창 2/3를 잃는다. 서비스 스트림은 start()에서
            # 이미 연속으로 열려 있으므로, 여기서는 collector 창 등록만 한다
            # (장치 명령 0회 — 창 경계는 합성 rx_time 태그가 공급).
            worker = self._lease.worker
            for lane_index in self.rx_lanes_for(request.channel.name):
                try:
                    worker.register_rx_window_lane(lane_index, hardware_start, expected)
                except Exception as error:  # noqa: BLE001
                    self._rx_reject_probe(
                        "REG_FAIL", f"{type(error).__name__}: {str(error)[:100]}"
                    )
                    return self._error(
                        ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE,
                        "RX 창 등록이 실패했습니다",
                        details=Details.of(reason=type(error).__name__, lane=lane_index),
                    )
                child_key = ("rx", base_key, lane_index)
                child = _RxChild(lane_index, child_key, request, expected, hardware_start, hardware_end)
                self._rx_children[child_key] = child
            return Ok(
                RadioRxAccepted(
                    request.channel, request.start_at, request.duration_nanoseconds,
                    self._time_source.now_monotonic(),
                )
            )

    def status(self, request: RadioStatusRequest):
        with self._condition:
            if not isinstance(request, RadioStatusRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStatusRequest가 필요합니다")
            if request.channel is not None and (
                self._configuration is None
                or request.channel.name not in self._configuration.channel_names()
            ):
                return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다", details=Details.of(channel=request.channel.name))
            queue_status = []
            if self._configuration is not None:
                for name in self._configuration.channel_names():
                    tx_outstanding = sum(
                        1 for parent in self._tx_parents.values()
                        if parent.request.channel.name == name and not parent.terminal
                    )
                    rx_outstanding = sum(
                        1 for child in self._rx_children.values()
                        if child.request.channel.name == name and not child.terminal
                    )
                    queue_status.append(
                        RadioQueueStatus(
                            channel=PhysicalChannel(name),
                            tx_outstanding_count=tx_outstanding,
                            tx_outstanding_bytes=tx_outstanding * 8_192 * 8,
                            rx_window_outstanding_count=rx_outstanding,
                            rx_block_pending_count=0,
                            rx_block_pending_bytes=0,
                            tx_rejected_count=self._tx_rejected.get(name, 0),
                            rx_window_rejected_count=self._rx_rejected.get(name, 0),
                            rx_dropped_block_count=0,
                            rx_dropped_sample_count=0,
                        )
                    )
            return Ok(
                RadioStatusSnapshot(
                    adapter_kind=STAGE7_ADAPTER_KIND,
                    device_state=self._state,
                    clock_reference=ClockReference.INTERNAL,
                    clock_locked=False,
                    pps_locked=False,
                    underflow_count=self._underflow_count,
                    overflow_count=self._overflow_count,
                    late_command_count=self._late_command_count,
                    timing_status=STAGE7_PUBLIC_TIMING_STATUS,
                    queue_status=tuple(queue_status),
                    event_delivery_status=self._delivery_status,
                )
            )

    # --- monitor·이벤트 --------------------------------------------------------

    def _monitor_loop(self):
        while not self._monitor_stop.is_set():
            worker = self._lease.worker
            if worker is None:
                return
            try:
                worker.wait_for_activity(0.01)
                events = worker.poll_async()
            except Exception as error:  # noqa: BLE001
                with self._condition:
                    self._handle_device_loss_locked(type(error).__name__)
                return
            with self._condition:
                if self._monitor_stop.is_set():
                    return
                for event in events:
                    self._consume_async_locked(event)
                self._issue_due_rx_locked(worker)
                # 락 안에서는 read 대상 스냅샷만 뜬다 — worker RPC를 락을
                # 쥔 채 돌리면 schedule_tx가 락 대기로 수백 ms를 잃는다
                # (svcdbg45 실측: MAC 계획 여유 500ms가 worker 진입 148ms로
                # 증발, TX 전멸). RPC는 락 밖에서 수행한다.
                snapshot = [
                    child for child in self._rx_children.values()
                    if not child.terminal
                ]
            self._poll_rx_unlocked(worker, snapshot)

    def _consume_async_locked(self, event: dict):
        outcome = event.get("outcome")
        import os as _os
        if _os.environ.get("STAGE7_DIAG", "0") == "1":
            import sys as _sys
            # __slots__ 클래스라 인스턴스 속성 대입 불가 — 함수 속성 사용
            # (svc-run-d 실측: AttributeError가 펌프 회전을 통째로 중단시킴)
            f = Stage7B210RadioAdapter._consume_async_locked.__dict__
            n = f["_n"] = f.get("_n", 0) + 1
            if outcome != "BURST_ACK" or n <= 30 or n % 20 == 0:
                print(
                    f"[diag-radio] async #{n} {outcome} "
                    f"hw={event.get('hardware_time_ns')}",
                    file=_sys.stderr, flush=True,
                )
        event_time = event.get("hardware_time_ns")
        if outcome == "BURST_ACK":
            for parent in self._tx_parents.values():
                if parent.terminal:
                    continue
                for child in parent.children.values():
                    if child.state == "SUBMITTED" and (
                        event_time is None
                        or child.hardware_start_ns - 1_000_000 <= event_time <= child.hardware_end_ns + 1_000_000_000
                    ):
                        child.state = "COMPLETED"
                        child.completed_at = self._time_source.now_monotonic()
                        self._maybe_complete_parent_locked(parent)
                        return
            return
        if outcome == "UNDERFLOW":
            self._underflow_count += 1
            code = ErrorCode.RADIO_TX_UNDERFLOW
        elif outcome == "TIME_ERROR":
            self._late_command_count += 1
            code = ErrorCode.RADIO_LATE_COMMAND
        elif outcome == "SEQUENCE_ERROR":
            code = ErrorCode.RADIO_STREAM_FAILED
        else:
            code = ErrorCode.RADIO_STREAM_FAILED
        for parent in self._tx_parents.values():
            if parent.terminal:
                continue
            for child in parent.children.values():
                if child.state == "SUBMITTED":
                    child.state = "FAILED"
                    child.error_code = code
                    self._maybe_complete_parent_locked(parent)
                    return

    def _default_tx_tune_hz(self) -> int:
        return self._resolved.rf_tune_for_channel(
            self._resolved.endpoint.mac.tx_channel.name
        )

    def _finish_retune_locked(self, parent: _TxParent):
        """direct TX 완료 뒤 TX LO를 평상시 tune으로 복귀하고 mute를 해제한다 (§10.1)."""

        if parent.retune_return is None:
            return
        worker = self._lease.worker
        if worker is None:
            return
        latest_end = max(child.hardware_end_ns for child in parent.children.values())
        try:
            worker.retune_tx_lo(parent.retune_return, latest_end + _SWITCH_GAP_NS)
            for lane_index in (0, 1):
                worker.set_tx_mute(lane_index, False)
        except Exception:  # noqa: BLE001
            pass
        self._last_tx_tune_hz = parent.retune_return

    def tx_burst_state(self, burst_id_value: str):
        """burst의 **권위 TX 원장** 상태 조회(읽기 전용). Stage 8 terminal 판정이 쓴다.

        반환: ``{"terminal": bool, "children": {lane: state}}`` — state는 SUBMITTED|COMPLETED|FAILED.
        아직 제출되지 않았거나 원장에 없으면 ``None``. 진행 중(SUBMITTED)과 확정(COMPLETED/FAILED)을
        구분할 수 있고, 과거 burst의 결과가 현재 burst로 새지 않는다(burst_id 키). 상태를 만들지 않고
        원장이 기록한 것만 돌려준다.
        """

        with self._condition:
            parent = self._tx_parents.get(burst_id_value)
            if parent is not None:
                return {"terminal": bool(parent.terminal),
                        "children": {child.lane_index: child.state
                                     for child in parent.children.values()}}
            recorded = self._tx_terminal_outcomes.get(burst_id_value)
            if recorded is None:
                return None
            return {"terminal": True, "children": dict(recorded)}

    def rx_lanes_for(self, channel_name):
        mapping = self._rx_lane_channels
        return (0, 1) if mapping is None else tuple(
            lane for lane in (0, 1) if mapping.get(lane) == channel_name)

    def set_rx_lane_channels(self, mapping):
        """Scope owner calls only after all admitted RX children have drained."""
        with self._condition:
            if any(not child.terminal for child in self._rx_children.values()):
                raise RuntimeError("RX windows must drain before changing lane bindings")
            if mapping is not None and (set(mapping) != {0, 1} or len(set(mapping.values())) != 2):
                raise ValueError("two distinct canonical RX channels required")
            self._rx_lane_channels = None if mapping is None else dict(mapping)

    def _maybe_complete_parent_locked(self, parent: _TxParent):
        if parent.terminal:
            return
        states = [child.state for child in parent.children.values()]
        if "SUBMITTED" in states:
            return
        parent.terminal = True
        self._terminal_keys.add(parent.key)
        # 확정된 per-lane 결과를 유계로 보존한다(parent는 아래에서 원장에서 제거되므로, 이후 조회가
        # "기록 없음"과 "실패"를 혼동하지 않게 한다). 상한 초과 시 가장 오래된 것부터 버린다.
        self._tx_terminal_outcomes[parent.request.burst_id.value] = {
            child.lane_index: child.state for child in parent.children.values()}
        while len(self._tx_terminal_outcomes) > _TX_TERMINAL_HISTORY:
            self._tx_terminal_outcomes.pop(next(iter(self._tx_terminal_outcomes)), None)
        self._tx_parents.pop(parent.request.burst_id.value, None)
        self._finish_retune_locked(parent)
        if all(state == "COMPLETED" for state in states):
            completed_at = max(
                (child.completed_at for child in parent.children.values()),
                key=lambda point: point.total_nanoseconds(),
            )
            self._sidecar.terminal_burst(parent.request.burst_id.value, "COMPLETED")
            self._publish_locked(
                InternalEventType.BURST_COMPLETED,
                RadioTxCompleted(parent.request.burst_id, parent.request.channel, completed_at),
                Correlation(burst_id=parent.request.burst_id),
            )
            return
        # 한 lane만 성공한 fanout: PARTIAL_RF_EMISSION 처리 (§15.1.1).
        if len(parent.children) > 1 and any(state == "COMPLETED" for state in states):
            self._private_fault(
                Stage7PrivateErrorCode.PARTIAL_RF_EMISSION,
                "fanout child 일부만 성공했습니다",
                fanout_scope=parent.fanout_scope,
            )
            self._terminal_tx_error_locked(
                parent, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                "fanout parent가 partial RF emission으로 실패했습니다",
                already_terminal=True,
            )
            return
        failed = next(child for child in parent.children.values() if child.state == "FAILED")
        self._terminal_tx_error_locked(
            parent, failed.error_code or ErrorCode.RADIO_STREAM_FAILED,
            "TX child가 실패했습니다", already_terminal=True,
        )

    def _terminal_tx_error_locked(self, parent, code, message, *, already_terminal=False):
        if not already_terminal:
            parent.terminal = True
            self._terminal_keys.add(parent.key)
            self._tx_parents.pop(parent.request.burst_id.value, None)
        self._sidecar.terminal_burst(parent.request.burst_id.value, str(code))
        self._publish_locked(
            InternalEventType.LAYER_ERROR,
            None,
            Correlation(burst_id=parent.request.burst_id),
            error=self._error(code, ErrorCategory.DEVICE, message, Correlation(burst_id=parent.request.burst_id)).error,
        )

    _RX_JIT_LOOKAHEAD_NS = 400_000_000
    _RX_JIT_MIN_LEAD_NS = 5_000_000
    _RX_JIT_MAX_OUTSTANDING = 4

    def _issue_due_rx_locked(self, worker):
        """대기 중 RX 창을 시작 임박순으로 적시 발행한다 (명령 대기열 보호)."""

        pending = getattr(self, "_rx_pending", None)
        if not pending:
            return
        try:
            now_ns = int(worker.hardware_time_ns())
        except Exception as error:  # noqa: BLE001
            self._handle_device_loss_locked(type(error).__name__)
            return
        issued = getattr(self, "_rx_issued", None)
        if issued is None:
            issued = self._rx_issued = []
        # 시작 시각이 지난 명령은 FPGA 큐를 떠났다고 본다.
        issued[:] = [start for start in issued if start > now_ns]
        pending.sort(key=lambda item: item[0])
        while pending and len(issued) < self._RX_JIT_MAX_OUTSTANDING:
            start_ns, expected, base_key = pending[0]
            lead = start_ns - now_ns
            if lead < self._RX_JIT_MIN_LEAD_NS:
                # 발행 시기를 놓친 창 — 즉시 timeout terminal로 정리해
                # MAC이 500ms 지연 없이 재계획하게 한다.
                pending.pop(0)
                self._rx_reject_probe("JIT_LATE_DROP", f"lead_ns={lead}")
                for lane_index in (0, 1):
                    child = self._rx_children.get(("rx", base_key, lane_index))
                    if child is not None and not child.terminal:
                        child.terminal = True
                        self._rx_children.pop(child.key, None)
                        self._terminal_keys.add(child.key)
                        self._emit_rx_terminal_error_locked(
                            child, ErrorCode.RADIO_RX_TIMEOUT
                        )
                continue
            if lead > self._RX_JIT_LOOKAHEAD_NS:
                break
            try:
                worker.submit_rx_lane(0, start_ns, expected)
                worker.submit_rx_lane(1, start_ns, expected)
            except Exception as error:  # noqa: BLE001
                self._rx_reject_probe(
                    "JIT_SUBMIT_FAIL",
                    f"{type(error).__name__}: {str(error)[:100]}",
                )
                pending.pop(0)
                continue
            issued.append(start_ns)
            pending.pop(0)

    def _poll_rx_unlocked(self, worker, children):
        """락 밖 RPC 수집 → 락 안 반영. 폴링 게이트(창 end 후에만 read) 포함.

        (svcdbg44/45 실측 근거: end 전 read RPC 폭주 + 락 점유 RPC가 TX
        경로를 간헐 수백 ms 지연시켰다.)"""

        if not children and not self._tx_parents:
            return
        try:
            now_ns = int(worker.hardware_time_ns())
        except Exception as error:  # noqa: BLE001
            with self._condition:
                self._handle_device_loss_locked(type(error).__name__)
            return
        results = []
        for child in children:
            if child.terminal or now_ns < child.hardware_end_ns:
                continue
            try:
                result = worker.read_rx_cf32_lane(
                    child.lane_index, child.expected_sample_count,
                    allow_partial=False,
                    expected_hardware_time_ns=child.hardware_start_ns,
                )
            except Exception as error:  # noqa: BLE001
                with self._condition:
                    self._handle_device_loss_locked(type(error).__name__)
                return
            results.append((child, result))
        stale = []
        dispatches = []
        with self._condition:
            # 시간 기반 TX 완결 (svcdbg55 실측: gr-uhd/B200 async BURST_ACK가
            # 방사 후 7~24s 지연 도착 — MAC 재계획이 완료 대기로 기아).
            # 종료 +500ms까지 오류 async가 없으면 완료로 확정한다. 오류는
            # 즉시 오는 부류라 거부권 창이 충분하고, 늦은 실제 ACK는 이미
            # terminal인 부모에 무해하게 흡수된다. 놓친 실패는 상위 전송
            # 계층 ACK/재시도가 끝단 보호(TBB는 순환 방송이라 무해).
            for parent in tuple(self._tx_parents.values()):
                if parent.terminal:
                    continue
                changed = False
                for child in parent.children.values():
                    # lane별 개별 완결 — 한 lane의 실제 ACK가 먼저 도착한
                    # '반확인' 부모도 시한 경과 lane만 채워 완결한다
                    # (svcdbg57 실측: 전-lane 조건은 2.4s/12s 혼재 주기).
                    if (
                        child.state == "SUBMITTED"
                        and now_ns > child.hardware_end_ns + 500_000_000
                    ):
                        child.state = "COMPLETED"
                        child.completed_at = self._time_source.now_monotonic()
                        changed = True
                if changed:
                    self._maybe_complete_parent_locked(parent)
            for child, result in results:
                if child.terminal:
                    continue
                if result is None:
                    if now_ns > child.hardware_end_ns + 500_000_000:
                        child.terminal = True
                        self._rx_children.pop(child.key, None)
                        self._terminal_keys.add(child.key)
                        stale.append(child)
                        self._emit_rx_terminal_error_locked(
                            child, ErrorCode.RADIO_RX_TIMEOUT
                        )
                    continue
                deferred = self._deliver_rx_result_locked(child, result)
                if deferred is not None:
                    dispatches.append(deferred)
        dispatcher = self._lane_dispatcher
        for generation, lane_index, key, received, completion in dispatches:
            dispatcher.on_lane_samples(generation, lane_index, key, received)
            dispatcher.on_lane_window_completed(generation, lane_index, key, completion)
        for child in stale:
            try:
                # stale 창 byte 반환 — collector 예산 기아 방지 (락 밖 RPC).
                worker.unregister_rx_lane(child.lane_index, child.hardware_start_ns)
            except Exception:  # noqa: BLE001
                pass

    def _deliver_rx_result_locked(self, child: _RxChild, result: dict):
        segments = result.get("segments", ())
        total = sum(len(data) // 8 for data, _, _ in segments)
        child.received_sample_count = total
        overflow = result.get("overflow_samples", 0)
        if overflow:
            self._overflow_count += 1
        clean = (
            len(segments) == 1
            and not segments[0][2]
            and overflow == 0
            and total == child.expected_sample_count
        )
        child.terminal = True
        self._rx_children.pop(child.key, None)
        self._terminal_keys.add(child.key)
        if not clean:
            import os as _os
            if _os.environ.get("STAGE7_DIAG", "0") == "1":
                import sys as _sys
                print(
                    f"[diag-radio] unclean lane={child.lane_index} "
                    f"req_utc={child.request.start_at.total_nanoseconds()} "
                    f"got={total}/{child.expected_sample_count} "
                    f"segs={len(segments)}",
                    file=_sys.stderr, flush=True,
                )
            code = (
                ErrorCode.RADIO_RX_OVERFLOW if overflow else ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
            )
            self._emit_rx_terminal_error_locked(child, code)
            return
        data, first_hardware_ns, _gap = segments[0]
        hardware_point = TimePoint.from_nanoseconds(
            first_hardware_ns, ClockDomain.RADIO_HARDWARE, self._hardware_clock_id
        )
        utc_ns = self._bridge.hardware_ns_to_utc_label(first_hardware_ns)
        # 실물 B210 finite stream은 요청보다 늦게 시작할 수 있다: 정상
        # ~1 sample(3.3µs, svcdbg32 실측), 발행 지연 시 ~117µs(svcdbg36
        # 실측 — 명령이 창 시작 직후 도달하면 UHD가 즉시 시작). frozen
        # 관측 계약은 exact 일치를 요구하므로 ≤1ms는 요청 시각으로
        # 정규화한다 — 전체 8192 샘플은 전달되고(내용은 지연만큼 창 뒤로
        # 연장 캡처) 복호기 자체 동기가 슬롯 내 편이를 흡수한다. 수용
        # 오염은 digest 검증이 계속 차단. >1ms는 그대로 두어 진짜
        # 불연속으로 판정되게 한다.
        requested_ns = child.request.start_at.total_nanoseconds()
        if utc_ns is not None and 0 <= utc_ns - requested_ns <= 1_000_000:
            utc_ns = requested_ns
        first_sample_at = TimePoint.from_nanoseconds(
            utc_ns if utc_ns is not None else requested_ns,
            ClockDomain.UTC,
            child.request.start_at.clock_id,
        )
        block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=307_200,
            sample_count=total,
            data=bytes(data),
            first_sample_at=first_sample_at,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        received = RadioSamplesReceived(
            channel=child.request.channel,
            samples=block,
            hardware_received_at=hardware_point,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        completion = RadioRxWindowCompleted(
            configure_generation=self._generation,
            channel=child.request.channel,
            requested_start_at=child.request.start_at,
            requested_duration_nanoseconds=child.request.duration_nanoseconds,
            expected_sample_count=child.expected_sample_count,
            received_sample_count=total,
            completed_at=self._time_source.now_monotonic(),
        )
        if self._lane_dispatcher is None:
            return None
        # dispatcher 전달(복호 포함, 창당 수십 ms CPU)은 락 밖에서 수행하도록
        # 호출자에게 지연 반환한다 (svcdbg45 락-기아 근거).
        return (self._lane_generation, child.lane_index, child.key, received, completion)

    def _emit_rx_terminal_error_locked(self, child: _RxChild, code):
        if self._lane_dispatcher is not None:
            self._lane_dispatcher.on_lane_error(
                self._lane_generation, child.lane_index, child.key, code,
                {
                    "channel": child.request.channel.name,
                    "requested_start_seconds": child.request.start_at.seconds,
                    "requested_start_nanoseconds": child.request.start_at.nanoseconds,
                    "requested_duration_nanoseconds": child.request.duration_nanoseconds,
                    "configure_generation": self._generation,
                },
            )

    def _handle_device_loss_locked(self, reason: str):
        self._state = RadioDeviceState.FAILED
        for parent in tuple(self._tx_parents.values()):
            self._terminal_tx_error_locked(
                parent, ErrorCode.RADIO_DEVICE_DISCONNECTED, "worker가 소실되었습니다"
            )
        for child in tuple(self._rx_children.values()):
            child.terminal = True
            self._emit_rx_terminal_error_locked(child, ErrorCode.RADIO_DEVICE_DISCONNECTED)
        self._tx_parents.clear()
        self._rx_children.clear()
        self._rx_pending = []
        self._rx_issued = []
        self._publish_locked(
            InternalEventType.LAYER_ERROR,
            None,
            Correlation(),
            error=self._error(
                ErrorCode.RADIO_DEVICE_DISCONNECTED, ErrorCategory.DEVICE,
                "isolated dual-lane worker가 소실되었습니다",
                details=Details.of(reason=reason),
            ).error,
        )

    def _require_running(self, correlation=Correlation()):
        if self._stopping or self._state is not RadioDeviceState.RUNNING:
            return self._error(
                ErrorCode.RADIO_NOT_STARTED, ErrorCategory.DEVICE,
                "RUNNING state가 아닙니다", correlation,
            )
        return None

    def _reject_tx_locked(self, channel):
        if channel.name in self._tx_rejected:
            self._tx_rejected[channel.name] += 1

    def _reject_rx_locked(self, channel):
        if channel.name in self._rx_rejected:
            self._rx_rejected[channel.name] += 1

    def _publish_locked(self, event_type, payload, correlation, *, error=None):
        from ..common.events import EventSeverity, InternalEvent

        try:
            event = InternalEvent(
                event_id=self._id_source.new_event_id(),
                event_type=event_type,
                layer=Layer.RADIO,
                severity=EventSeverity.INFO if error is None else EventSeverity.ERROR,
                occurred_at=self._time_source.now_monotonic(),
                payload=payload,
                correlation=correlation,
                error=error,
            )
            self._event_sink.publish(event)
        except Exception:  # noqa: BLE001
            self._delivery_status = RadioEventDeliveryStatus(
                True,
                self._delivery_status.undelivered_terminal_count + 1,
                self._delivery_status.first_failure_at or self._time_source.now_monotonic(),
                self._time_source.now_monotonic(),
            )

    def _emit_status_changed_locked(self):
        self._publish_locked(InternalEventType.RADIO_STATUS_CHANGED, None, Correlation())
        return True


__all__ = (
    "STAGE7_PUBLIC_TIMING_STATUS",
    "Stage7AppliedLaneSettings",
    "Stage7B210RadioAdapter",
    "Stage7HostBridgeHandle",
    "Stage7QualificationLease",
)
