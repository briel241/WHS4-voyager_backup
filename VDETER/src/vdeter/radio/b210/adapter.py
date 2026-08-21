"""Stage 3 USRP B210 adapter using the programmatic GNU Radio gr-uhd backend."""

from __future__ import annotations

import importlib.util
import math
import struct
import time as _wall_time
from collections import deque
from dataclasses import dataclass, replace
from threading import Condition, Event, RLock, Thread, current_thread

from ...common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    ClockReference,
    RadioAppliedChannelSetting,
    RadioAppliedSettings,
    RadioCapabilities,
    RadioConfiguration,
    RadioConfigurationApplied,
    RadioConfigureRequest,
    RadioDeviceState,
    RadioEventDeliveryStatus,
    RadioMappingState,
    RadioClockState,
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
from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.events import EventSeverity, InternalEvent, InternalEventType, RecordingInternalEventSink
from ...common.identifiers import IdSource, PhysicalChannel, Uuid4IdSource
from ...common.results import Ok, internal_error
from ...common.time import ClockDomain, ClockMapping, SystemTimeSource, TimePoint, TimeSource
from ..models import RadioAdapterKind
from .backend import GrUhdFlowgraphBackend

ADAPTER_KIND = RadioAdapterKind.B210.value
REQUIRED_MODULES = ("gnuradio", "gnuradio.uhd")
_LEAD_NS = 100_000_000
_UTC_JUMP_NS = 10_000_000
_UTC_FLAGS = (
    RadioUtcFlag.HOST_ESTIMATE,
    RadioUtcFlag.NOT_UTC_DISCIPLINED,
    RadioUtcFlag.UTC_ACCURACY_UNVERIFIED,
)


def missing_dependencies() -> tuple[str, ...]:
    """Return missing gr-uhd modules without importing backend dependencies."""
    missing = []
    for name in REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            found = False
        if not found:
            missing.append(name)
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class _TxLedgerEntry:
    key: tuple
    request: RadioTxRequest
    hardware_start_ns: int
    hardware_end_ns: int


@dataclass(slots=True)
class _RxLedgerEntry:
    key: tuple
    request: RadioRxRequest
    expected_sample_count: int
    hardware_start_ns: int
    hardware_end_ns: int
    mapping: ClockMapping
    received_sample_count: int = 0


class B210RadioAdapter:
    """Public RadioAdapter implementation backed only by programmatic gr-uhd."""

    __slots__ = (
        "_time_source", "_id_source", "_event_sink", "_backend_factory", "_backend",
        "_configuration", "_applied", "_state", "_generation", "_timing_status",
        "_mapping_utc_reference_ns", "_mapping_monotonic_reference_ns", "_utc_jump_blocked",
        "_tx_ledger", "_rx_ledger", "_terminal_keys", "_terminal_codes", "_terminal_details", "_delivery_status", "_last_stopped",
        "_backend_started", "_underflow_count", "_overflow_count", "_late_command_count", "_duplicate_ack_count",
        "_tx_rejected", "_rx_rejected", "_rx_dropped_blocks", "_rx_dropped_samples",
        "_rx_block_pending_count", "_rx_block_pending_bytes", "_next_rx_gap",
        "_last_tx_channel", "_recent_ack_intervals", "_monitor_thread",
        "_monitor_stop", "_lock", "_condition", "_stopping",
    )

    def __init__(
        self,
        time_source: TimeSource | None = None,
        id_source: IdSource | None = None,
        event_sink=None,
        *,
        backend_factory=None,
    ) -> None:
        self._time_source = time_source or SystemTimeSource()
        self._id_source = id_source or Uuid4IdSource()
        self._event_sink = event_sink or RecordingInternalEventSink()
        self._backend_factory = backend_factory
        self._backend = None
        self._configuration = None
        self._applied = None
        self._state = RadioDeviceState.DISCONNECTED
        self._generation = 0
        self._timing_status = None
        self._mapping_utc_reference_ns = 0
        self._mapping_monotonic_reference_ns = 0
        self._utc_jump_blocked = False
        self._tx_ledger: list[_TxLedgerEntry] = []
        self._rx_ledger: list[_RxLedgerEntry] = []
        self._terminal_keys = set()
        self._terminal_codes = {}
        self._terminal_details = {}
        self._delivery_status = RadioEventDeliveryStatus(False, 0, None, None)
        self._last_stopped = None
        self._backend_started = False
        self._underflow_count = self._overflow_count = self._late_command_count = 0
        self._duplicate_ack_count = 0
        self._tx_rejected = {}
        self._rx_rejected = {}
        self._rx_dropped_blocks = {}
        self._rx_dropped_samples = {}
        self._rx_block_pending_count = {}
        self._rx_block_pending_bytes = {}
        self._next_rx_gap = {}
        self._last_tx_channel = None
        self._recent_ack_intervals = deque(maxlen=4_096)
        self._monitor_thread = None
        self._monitor_stop = Event()
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stopping = False

    def configure(self, request: RadioConfigureRequest):
        with self._condition:
            if not isinstance(request, RadioConfigureRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioConfigureRequest가 필요합니다")
            if self._stopping or self._state not in (RadioDeviceState.DISCONNECTED, RadioDeviceState.STOPPED):
                return self._error(ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE, "configure는 DISCONNECTED/STOPPED state에서만 허용합니다")
            validation = _validate_b210_configuration(request.configuration)
            if validation is not None:
                return validation
            if self._backend_factory is None:
                missing = missing_dependencies()
                if missing:
                    return self._error(
                        ErrorCode.RADIO_DEPENDENCY_MISSING, ErrorCategory.DEVICE,
                        "승인된 GNU Radio gr-uhd dependency가 없습니다",
                        details=Details.of(missing_modules=missing),
                    )
                factory = GrUhdFlowgraphBackend
            else:
                factory = self._backend_factory
            configuration = request.configuration
            expected_serial = configuration.device_selector.split("=", 1)[1]
            try:
                found = tuple(factory.discover(configuration.device_selector))
            except Exception as error:
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED,
                    ErrorCategory.DEVICE,
                    "B210 discovery가 실패했습니다",
                    details=Details.of(
                        reason=type(error).__name__,
                        exception_message=str(error),
                    ),
                )
            if found != (expected_serial,):
                code = ErrorCode.RADIO_DEVICE_NOT_FOUND if not found else ErrorCode.RADIO_CONFIGURATION_FAILED
                return self._error(code, ErrorCategory.DEVICE, "selector와 정확히 일치하는 B210 한 대가 필요합니다", details=Details.of(match_count=len(found)))

            previous_state = self._state
            backend = None
            try:
                backend = factory(configuration.device_selector, configuration.b210_stream.cpu_format, configuration.b210_stream.otw_format)
                readback = backend.configure(configuration)
            except Exception as error:
                if backend is not None and not self._cleanup_backend(backend, configuration.resource_limits.stop_timeout_ns):
                    self._retain_failed_backend_locked(backend, configuration)
                else:
                    self._state = previous_state
                return self._error(ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE, "B210 block 생성 또는 설정 readback이 실패했습니다", details=Details.of(reason=type(error).__name__))
            readback_error = _validate_readback(configuration, expected_serial, readback)
            if readback_error is not None:
                if not self._cleanup_backend(backend, configuration.resource_limits.stop_timeout_ns):
                    self._retain_failed_backend_locked(backend, configuration)
                else:
                    self._state = previous_state
                return readback_error

            generation = self._generation + 1
            timing = self._acquire_mapping(backend, expected_serial, generation, configuration)
            if timing is None:
                if not self._cleanup_backend(backend, configuration.resource_limits.stop_timeout_ns):
                    self._retain_failed_backend_locked(backend, configuration)
                else:
                    self._state = previous_state
                return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "허용 uncertainty 안에서 UTC-hardware mapping을 획득하지 못했습니다")
            try:
                connect = getattr(backend, "connect", None)
                if connect is not None:
                    connect()
            except Exception as error:
                if not self._cleanup_backend(
                    backend, configuration.resource_limits.stop_timeout_ns
                ):
                    self._retain_failed_backend_locked(backend, configuration)
                else:
                    self._state = previous_state
                return self._error(
                    ErrorCode.RADIO_CONFIGURATION_FAILED,
                    ErrorCategory.DEVICE,
                    "검증된 gr-uhd flowgraph 연결이 실패했습니다",
                    details=Details.of(reason=type(error).__name__),
                )
            bindings = {value.channel.name: value for value in configuration.channel_bindings}
            try:
                applied_channels = tuple(
                    _applied_channel(value, bindings[value.channel.name], readback["channels"][value.channel.name])
                    for value in configuration.channels
                )
            except Exception as error:
                if not self._cleanup_backend(
                    backend, configuration.resource_limits.stop_timeout_ns
                ):
                    self._retain_failed_backend_locked(backend, configuration)
                else:
                    self._state = previous_state
                return self._error(ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE, "B210 applied settings 생성이 실패했습니다", details=Details.of(reason=type(error).__name__))
            capabilities = RadioCapabilities(
                (SAMPLE_FORMAT_CF32LE,), _LEAD_NS, _LEAD_NS, _LEAD_NS, True,
                (RadioTimingProfile.INTERNAL_FREE_RUNNING,), configuration.resource_limits,
            )
            settings = RadioAppliedSettings(
                expected_serial, generation, applied_channels, 30_720_000,
                readback["master_clock_rate_hz"], readback["cpu_format"], readback["otw_format"],
            )
            applied = RadioConfigurationApplied(
                ADAPTER_KIND, RadioDeviceState.CONFIGURED,
                tuple(value.channel.name for value in applied_channels), SAMPLE_FORMAT_CF32LE,
                capabilities, settings,
            )
            self._backend = backend
            self._configuration = configuration
            self._applied = applied
            self._state = RadioDeviceState.CONFIGURED
            self._generation = generation
            self._timing_status = timing
            self._tx_ledger.clear()
            self._rx_ledger.clear()
            self._terminal_keys.clear()
            self._terminal_codes.clear()
            self._terminal_details.clear()
            self._delivery_status = RadioEventDeliveryStatus(False, 0, None, None)
            self._last_stopped = None
            self._backend_started = False
            self._underflow_count = self._overflow_count = self._late_command_count = 0
            self._duplicate_ack_count = 0
            self._recent_ack_intervals.clear()
            names = configuration.channel_names()
            self._tx_rejected = {name: 0 for name in names}
            self._rx_rejected = {name: 0 for name in names}
            self._rx_dropped_blocks = {name: 0 for name in names}
            self._rx_dropped_samples = {name: 0 for name in names}
            self._rx_block_pending_count = {name: 0 for name in names}
            self._rx_block_pending_bytes = {name: 0 for name in names}
            self._next_rx_gap = {name: False for name in names}
            self._last_tx_channel = readback.get("initial_tx_channel")
            self._utc_jump_blocked = False
            self._monitor_thread = None
            self._monitor_stop = Event()
            if not self._emit_status_changed_locked():
                return self._delivery_error()
            return Ok(applied)

    def start(self, request: RadioStartRequest):
        with self._condition:
            if not isinstance(request, RadioStartRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStartRequest가 필요합니다")
            if self._stopping or self._state is not RadioDeviceState.CONFIGURED:
                return self._error(
                    ErrorCode.RADIO_NOT_CONFIGURED if self._configuration is None else ErrorCode.RADIO_ALREADY_STARTED,
                    ErrorCategory.DEVICE, "start는 CONFIGURED state에서만 허용합니다",
                )
            try:
                self._backend.start()
            except Exception as error:
                self._state = RadioDeviceState.FAILED
                return self._error(ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE, "GNU Radio flowgraph start가 실패했습니다", details=Details.of(reason=type(error).__name__))
            self._backend_started = True
            self._state = RadioDeviceState.RUNNING
            started = RadioStarted(ADAPTER_KIND, self._state, self._time_source.now_monotonic())
            if not self._emit_status_changed_locked():
                return self._delivery_error()
            monitor = Thread(
                target=self._monitor_loop,
                name=f"vdeter-b210-monitor-{self._generation}",
                daemon=True,
            )
            self._monitor_thread = monitor
            try:
                monitor.start()
            except Exception as error:
                self._monitor_thread = None
                self._monitor_stop.set()
                self._state = RadioDeviceState.FAILED
                return self._error(
                    ErrorCode.RADIO_STREAM_FAILED,
                    ErrorCategory.DEVICE,
                    "B210 async monitoring worker start가 실패했습니다",
                    details=Details.of(reason=type(error).__name__),
                )
            return Ok(started)

    def stop(self, request: RadioStopRequest):
        if not isinstance(request, RadioStopRequest):
            return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStopRequest가 필요합니다")
        with self._condition:
            if self._state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                return Ok(self._last_stopped)
            if self._state is RadioDeviceState.DISCONNECTED:
                return self._error(ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE, "configure 전에는 stop할 수 없습니다")
            timeout_ns = self._configuration.resource_limits.stop_timeout_ns
            cleanup_deadline_ns = _wall_time.monotonic_ns() + timeout_ns
            if self._stopping:
                deadline = _wall_time.monotonic_ns() + timeout_ns
                while self._stopping:
                    remaining = deadline - _wall_time.monotonic_ns()
                    if remaining <= 0:
                        return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "동시 stop cleanup의 bounded wait가 만료되었습니다")
                    self._condition.wait(remaining / 1_000_000_000)
                if self._state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                    return Ok(self._last_stopped)
                return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "공유 cleanup이 성공하지 않았습니다")
            self._stopping = True
            self._monitor_stop.set()
            monitor = self._monitor_thread
            for entry in tuple(self._tx_ledger):
                self._terminal_error_locked(
                    entry.key, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                    "B210 stop 시점에 제출된 TX outcome을 확정할 수 없습니다",
                    Correlation(burst_id=entry.request.burst_id), Details.of(channel=entry.request.channel.name),
                )
            for entry in tuple(self._rx_ledger):
                if self._delivery_status.failed:
                    break
                try:
                    result = self._read_rx(entry, allow_partial=True)
                except Exception:
                    result = None
                actual = entry.received_sample_count
                if result is not None:
                    actual, _missing_timestamp, _overflow_samples = (
                        self._consume_rx_result_locked(entry, result)
                    )
                if self._delivery_status.failed:
                    break
                if actual > 0:
                    self._terminal_error_locked(
                        ("rx", entry.key),
                        ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
                        "B210 stop이 부분 수신된 RX window를 중단했습니다",
                        Correlation(),
                        _rx_details(entry, actual),
                    )
                else:
                    self._terminal_error_locked(
                        ("rx", entry.key),
                        ErrorCode.RADIO_OPERATION_CANCELLED,
                        "B210 stop이 sample 미수신 RX window를 취소했습니다",
                        Correlation(),
                        _rx_details(entry, 0),
                    )
            self._tx_ledger.clear()
            self._rx_ledger.clear()
            backend = self._backend
            started = self._backend_started
        monitor_ok = self._join_monitor(monitor, cleanup_deadline_ns)
        cleanup_ok = True
        cleanup_error = None
        try:
            remaining_ns = max(0, cleanup_deadline_ns - _wall_time.monotonic_ns())
            if started:
                cleanup_ok = remaining_ns > 0 and self._stop_backend(backend, remaining_ns)
            elif backend is not None:
                cleanup_ok = remaining_ns > 0 and self._cleanup_backend(backend, remaining_ns)
        except Exception as error:
            cleanup_ok = False
            cleanup_error = type(error).__name__
        cleanup_ok = monitor_ok and cleanup_ok
        with self._condition:
            self._stopping = False
            if cleanup_ok:
                self._backend_started = False
                self._monitor_thread = None
            if not cleanup_ok:
                self._state = RadioDeviceState.FAILED
                self._condition.notify_all()
                return self._error(
                    ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT,
                    "GNU Radio flowgraph stop/wait가 timeout 또는 오류로 실패했습니다",
                    details=Details.of(reason=cleanup_error or "timeout"),
                )
            if self._delivery_status.failed:
                self._state = RadioDeviceState.STOPPED
                self._last_stopped = RadioStopped(
                    ADAPTER_KIND, self._state, self._time_source.now_monotonic()
                )
                self._condition.notify_all()
                return Ok(self._last_stopped)
            self._state = RadioDeviceState.STOPPED
            self._last_stopped = RadioStopped(ADAPTER_KIND, self._state, self._time_source.now_monotonic())
            delivered = self._emit_status_changed_locked()
            self._condition.notify_all()
            if not delivered:
                self._state = RadioDeviceState.FAILED
                return self._delivery_error()
            return Ok(self._last_stopped)

    def schedule_tx(self, request: RadioTxRequest):
        correlation = Correlation(burst_id=request.burst_id) if isinstance(request, RadioTxRequest) else Correlation()
        with self._condition:
            if not isinstance(request, RadioTxRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioTxRequest가 필요합니다")
            validation = self._validate_tx(request, correlation)
            if validation is not None:
                return validation
            state = self._require_running(correlation)
            if state is not None:
                return state
            self._poll_locked()
            state = self._require_running(correlation)
            if state is not None:
                return state
            duration_ns = _duration_ceil_ns(request.samples.sample_count, request.samples.sample_rate_hz)
            changed = self._last_tx_channel not in (None, request.channel.name)
            lead = _LEAD_NS * (2 if changed else 1)
            converted = self._convert_with_lead_locked(request.transmit_at, request.transmit_at.shifted(duration_ns), correlation, lead)
            if converted.is_err:
                self._reject_tx_locked(request.channel)
                return converted
            hardware_start = converted.value
            hardware_end = hardware_start + duration_ns
            key = ("tx", self._generation, request.burst_id.value)
            if key in self._terminal_keys or any(entry.request.burst_id == request.burst_id for entry in self._tx_ledger):
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "현재 generation에서 burst_id가 이미 사용 중입니다", correlation)
            if self._tx_ledger and hardware_start < self._tx_ledger[-1].hardware_start_ns:
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "이미 제출한 뒤쪽 command 앞에 새 TX를 삽입할 수 없습니다", correlation)
            if any(_overlap(hardware_start, hardware_end, entry.hardware_start_ns, entry.hardware_end_ns) for entry in self._tx_ledger):
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "같은 B210 TX device channel의 burst가 겹칩니다", correlation)
            retune_time = None
            if changed:
                retune_time = hardware_start - _LEAD_NS
                if self._tx_ledger and self._tx_ledger[-1].hardware_end_ns > retune_time:
                    self._reject_tx_locked(request.channel)
                    return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "이전 burst와 100 ms switch gap을 만족하지 않습니다", correlation)
            capacity = self._tx_capacity_error_locked(request, correlation)
            if capacity is not None:
                return capacity
            setting = next(value for value in self._configuration.channels if value.channel == request.channel)
            try:
                self._backend.submit_tx(request.samples.data, hardware_start, setting.center_frequency_hz, retune_time)
            except Exception as error:
                return self._error(ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE, "gr-uhd TX 제출이 실패했습니다", correlation, Details.of(reason=type(error).__name__))
            self._tx_ledger.append(_TxLedgerEntry(key, request, hardware_start, hardware_end))
            self._last_tx_channel = request.channel.name
            return Ok(RadioTxAccepted(request.burst_id, request.channel, request.transmit_at, self._time_source.now_monotonic()))

    def request_rx(self, request: RadioRxRequest):
        with self._condition:
            if not isinstance(request, RadioRxRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioRxRequest가 필요합니다")
            validation = self._validate_rx(request)
            if validation is not None:
                return validation
            state = self._require_running()
            if state is not None:
                return state
            self._poll_locked()
            state = self._require_running()
            if state is not None:
                return state
            expected = _round_half_up_samples(request.duration_nanoseconds, request.sample_rate_hz)
            if expected <= 0 or expected * 8 > self._configuration.resource_limits.rx_queue_max_bytes:
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "B210 RX window sample budget이 잘못되었습니다")
            converted = self._convert_with_lead_locked(request.start_at, request.start_at.shifted(request.duration_nanoseconds), Correlation(), _LEAD_NS)
            if converted.is_err:
                self._reject_rx_locked(request.channel)
                return converted
            hardware_start = converted.value
            hardware_end = hardware_start + request.duration_nanoseconds
            key = (self._generation, request.channel.name, request.start_at.seconds, request.start_at.nanoseconds, request.start_at.clock_id, request.duration_nanoseconds)
            if ("rx", key) in self._terminal_keys or any(entry.key == key for entry in self._rx_ledger):
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "RX window key가 중복되었습니다")
            if any(_overlap(hardware_start, hardware_end, entry.hardware_start_ns, entry.hardware_end_ns) for entry in self._rx_ledger):
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "같은 B210 RX device channel의 window가 겹칩니다")
            relevant = [entry for entry in self._rx_ledger if entry.request.channel == request.channel]
            if len(relevant) + 1 > self._configuration.resource_limits.rx_window_max_count:
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "B210 RX window ledger가 가득 찼습니다")
            setting = next(value for value in self._configuration.channels if value.channel == request.channel)
            try:
                self._backend.submit_rx(hardware_start, expected, setting.center_frequency_hz)
            except Exception as error:
                return self._error(ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE, "gr-uhd finite RX command 제출이 실패했습니다", details=Details.of(reason=type(error).__name__))
            self._rx_ledger.append(_RxLedgerEntry(key, request, expected, hardware_start, hardware_end, self._timing_status.mapping))
            self._rx_ledger.sort(key=lambda entry: entry.hardware_start_ns)
            return Ok(RadioRxAccepted(request.channel, request.start_at, request.duration_nanoseconds, self._time_source.now_monotonic()))

    def status(self, request: RadioStatusRequest):
        with self._condition:
            if not isinstance(request, RadioStatusRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStatusRequest가 필요합니다")
            if self._stopping and self._configuration is not None:
                deadline = _wall_time.monotonic_ns() + self._configuration.resource_limits.stop_timeout_ns
                while self._stopping:
                    remaining = deadline - _wall_time.monotonic_ns()
                    if remaining <= 0:
                        return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "status가 stop cleanup bounded wait를 초과했습니다")
                    self._condition.wait(remaining / 1_000_000_000)
            if request.channel is not None and (self._configuration is None or request.channel.name not in self._configuration.channel_names()):
                return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다", details=Details.of(channel=request.channel.name))
            return Ok(self._snapshot_locked(request.channel))

    def _acquire_mapping(self, backend, serial, generation, configuration):
        candidates = []
        for _ in range(configuration.timing.mapping_attempt_count):
            try:
                before = self._time_source.now_utc()
                hardware_ns = int(backend.hardware_time_ns())
                after = self._time_source.now_utc()
                acquired = self._time_source.now_monotonic()
            except Exception:
                # Admission and configure must never leak a backend exception.
                # A missing candidate is reported as CLOCK_MAPPING_UNAVAILABLE by
                # the caller (or RADIO_CONFIGURATION_FAILED during configure).
                continue
            if not before.same_clock(after):
                continue
            interval = after.difference_nanoseconds(before)
            if interval < 0 or interval > configuration.timing.max_mapping_uncertainty_ns:
                continue
            candidates.append((interval, before, after, hardware_ns, acquired))
        if not candidates:
            return None
        interval, before, _, hardware_ns, acquired = min(candidates, key=lambda value: value[0])
        midpoint = before.shifted(interval // 2)
        hardware_id = f"b210:{serial}:cfg:{generation}"
        mapping = ClockMapping(
            ClockDomain.UTC, midpoint.clock_id, ClockDomain.RADIO_HARDWARE, hardware_id,
            midpoint, TimePoint.from_nanoseconds(hardware_ns, ClockDomain.RADIO_HARDWARE, hardware_id), interval,
        )
        self._mapping_utc_reference_ns = midpoint.total_nanoseconds()
        self._mapping_monotonic_reference_ns = acquired.total_nanoseconds()
        return RadioTimingStatus(
            RadioTimingProfile.INTERNAL_FREE_RUNNING, RadioClockState.FREE_RUNNING,
            RadioPpsState.PPS_NOT_PRESENT, _UTC_FLAGS, RadioMappingState.VALID,
            mapping, acquired, acquired.shifted(configuration.timing.mapping_valid_for_nanoseconds),
        )

    def _convert_with_lead_locked(self, start, end, correlation, lead_ns):
        self._refresh_mapping_locked()
        if self._utc_jump_blocked:
            self._utc_jump_blocked = False
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "host UTC jump 뒤 첫 timed 요청을 거부했습니다", correlation)
        if self._timing_status is None or self._timing_status.mapping_state is not RadioMappingState.VALID:
            renewed = self._acquire_mapping(self._backend, self._applied.applied_settings.device_id, self._generation, self._configuration)
            if renewed is None:
                return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "B210 clock mapping을 갱신하지 못했습니다", correlation)
            self._timing_status = renewed
            self._emit_status_changed_locked()
        mapping = self._timing_status.mapping
        if start.clock_domain is not ClockDomain.UTC or start.clock_id != mapping.source_clock_id:
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.VALIDATION, "요청 UTC clock ID가 mapping source와 다릅니다", correlation)
        horizon = self._configuration.timing.mapping_valid_for_nanoseconds
        if end.total_nanoseconds() - mapping.source_reference.total_nanoseconds() > horizon:
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.TIMEOUT, "요청 종료시각이 mapping horizon 밖입니다", correlation)
        converted = mapping.convert(start)
        if converted.is_err:
            return converted
        try:
            remaining = converted.value.total_nanoseconds() - int(self._backend.hardware_time_ns())
        except Exception as error:
            return self._error(ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE, "B210 hardware time read가 실패했습니다", correlation, Details.of(reason=type(error).__name__))
        if remaining < lead_ns:
            return self._error(
                ErrorCode.RADIO_LATE_REQUEST, ErrorCategory.TIMEOUT,
                "B210 timed command lead가 부족합니다", correlation,
                Details.of(required_lead_ns=lead_ns, remaining_lead_ns=remaining),
            )
        return Ok(converted.value.total_nanoseconds())

    def _refresh_mapping_locked(self):
        status = self._timing_status
        if status is None or status.mapping_state is not RadioMappingState.VALID:
            return
        mono = self._time_source.now_monotonic()
        utc = self._time_source.now_utc()
        elapsed_mono = mono.total_nanoseconds() - self._mapping_monotonic_reference_ns
        elapsed_utc = utc.total_nanoseconds() - self._mapping_utc_reference_ns
        if elapsed_utc < 0 or abs(elapsed_utc - elapsed_mono) > _UTC_JUMP_NS:
            self._timing_status = RadioTimingStatus(
                RadioTimingProfile.INTERNAL_FREE_RUNNING, RadioClockState.FREE_RUNNING,
                RadioPpsState.PPS_NOT_PRESENT, _UTC_FLAGS, RadioMappingState.UNAVAILABLE,
                None, None, None,
            )
            self._utc_jump_blocked = True
            for entry in tuple(self._tx_ledger):
                self._terminal_error_locked(entry.key, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN, "host UTC jump 뒤 제출된 B210 TX outcome을 확정할 수 없습니다", Correlation(burst_id=entry.request.burst_id), Details.of(channel=entry.request.channel.name))
            for entry in tuple(self._rx_ledger):
                if self._delivery_status.failed:
                    break
                try:
                    partial = self._read_rx(entry, allow_partial=True)
                except Exception:
                    partial = None
                actual = entry.received_sample_count
                if partial is not None:
                    actual, _missing_timestamp, _overflow_samples = (
                        self._consume_rx_result_locked(entry, partial)
                    )
                if self._delivery_status.failed:
                    break
                self._terminal_error_locked(
                    ("rx", entry.key),
                    ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
                    "host UTC jump로 B210 RX window가 불완전 종료되었습니다",
                    Correlation(),
                    _rx_details(entry, actual),
                )
            self._tx_ledger.clear()
            self._rx_ledger.clear()
            self._emit_status_changed_locked()
        elif mono >= status.expires_at_monotonic:
            self._timing_status = RadioTimingStatus(
                status.profile, status.clock_state, status.pps_state, status.utc_flags,
                RadioMappingState.STALE, status.mapping, status.acquired_at_monotonic,
                status.expires_at_monotonic,
            )
            self._emit_status_changed_locked()

    def _poll_locked(self) -> None:
        if self._backend is None or not self._backend_started:
            return
        self._refresh_mapping_locked()
        try:
            hardware_now = int(self._backend.hardware_time_ns())
        except Exception as error:
            self._handle_device_disconnect_locked(type(error).__name__)
            return
        try:
            async_events = tuple(self._backend.poll_async())
        except Exception as error:
            self._fail_monitor_locked(
                "B210 async event monitoring failed", type(error).__name__
            )
            return
        for native in async_events:
            event = _normalize_async(native)
            outcome = event["outcome"]
            event_time = event["hardware_time_ns"]
            if outcome == "STREAM_ERROR":
                self._fail_monitor_locked(
                    "B210 native async event is unrecognized or ambiguous",
                    "STREAM_ERROR",
                )
                return
            if (
                outcome == "BURST_ACK"
                and event_time is not None
                and any(start <= event_time <= end for start, end in self._recent_ack_intervals)
            ):
                self._duplicate_ack_count += 1
                self._emit_status_changed_locked()
                continue
            if not self._tx_ledger:
                if outcome == "BURST_ACK":
                    self._duplicate_ack_count += 1
                    self._emit_status_changed_locked()
                else:
                    self._fail_monitor_locked(
                        "B210 async event has no correlatable submitted TX", outcome
                    )
                    return
                continue
            if outcome == "BURST_ACK" or event_time is None:
                entry = self._tx_ledger[0]
                matches = (
                    event_time is None
                    or entry.hardware_start_ns
                    <= event_time
                    <= entry.hardware_end_ns
                )
            else:
                candidates = tuple(
                    value for value in self._tx_ledger
                    if value.hardware_start_ns
                    <= event_time
                    <= value.hardware_end_ns
                )
                entry = candidates[0] if len(candidates) == 1 else None
                matches = len(candidates) == 1
            if not matches:
                for affected in tuple(self._tx_ledger):
                    self._terminal_error_locked(affected.key, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN, "B210 async event correlation이 모호합니다", Correlation(burst_id=affected.request.burst_id), Details.of(channel=affected.request.channel.name))
                self._tx_ledger.clear()
                self._state = RadioDeviceState.FAILED
                self._emit_status_changed_locked()
                break
            self._tx_ledger.remove(entry)
            if outcome == "BURST_ACK":
                self._recent_ack_intervals.append(
                    (entry.hardware_start_ns, entry.hardware_end_ns)
                )
                completion = RadioTxCompleted(entry.request.burst_id, entry.request.channel, self._time_source.now_monotonic())
                self._terminal_success_locked(entry.key, InternalEventType.BURST_COMPLETED, completion, Correlation(burst_id=entry.request.burst_id), Details.of(channel=entry.request.channel.name, hardware_time_ns=event_time))
            else:
                counter_changed = False
                code = {
                    "UNDERFLOW": ErrorCode.RADIO_TX_UNDERFLOW,
                    "TIME_ERROR": ErrorCode.RADIO_LATE_COMMAND,
                    "SEQUENCE_ERROR": ErrorCode.RADIO_STREAM_FAILED,
                }.get(outcome, ErrorCode.RADIO_STREAM_FAILED)
                if outcome == "UNDERFLOW":
                    self._underflow_count += 1
                    counter_changed = True
                if outcome == "TIME_ERROR":
                    self._late_command_count += 1
                    counter_changed = True
                self._terminal_error_locked(entry.key, code, "B210 async TX 실패", Correlation(burst_id=entry.request.burst_id), Details.of(channel=entry.request.channel.name, backend_outcome=outcome, hardware_time_ns=event_time))
                if counter_changed and not self._delivery_status.failed:
                    self._emit_status_changed_locked()
        for entry in tuple(self._rx_ledger):
            if entry not in self._rx_ledger:
                continue
            try:
                result = self._read_rx(entry, allow_partial=False)
            except Exception as error:
                self._fail_monitor_locked(
                    "B210 RX collector failed", type(error).__name__
                )
                return
            deadline_reached = hardware_now is not None and hardware_now >= entry.hardware_end_ns
            if result is None and deadline_reached:
                try:
                    result = self._read_rx(entry, allow_partial=True)
                except Exception as error:
                    self._fail_monitor_locked(
                        "B210 RX collector failed while reading its partial window",
                        type(error).__name__,
                    )
                    return
            if result is None and not deadline_reached:
                continue
            if result is None:
                self._terminal_error_locked(("rx", entry.key), ErrorCode.RADIO_RX_TIMEOUT, "B210 RX window deadline까지 sample을 받지 못했습니다", Correlation(), _rx_details(entry, 0))
                if entry in self._rx_ledger:
                    self._rx_ledger.remove(entry)
                continue
            actual, missing_timestamp, overflow_samples = (
                self._consume_rx_result_locked(entry, result)
            )
            if self._delivery_status.failed:
                self._terminal_keys.add(("rx", entry.key))
            elif missing_timestamp:
                self._terminal_error_locked(("rx", entry.key), ErrorCode.RADIO_RX_WINDOW_INCOMPLETE, "B210 RX block에 rx_time tag가 없습니다", Correlation(), _rx_details(entry, actual))
            elif actual == entry.expected_sample_count and not overflow_samples:
                completed = RadioRxWindowCompleted(self._generation, entry.request.channel, entry.request.start_at, entry.request.duration_nanoseconds, entry.expected_sample_count, actual, self._time_source.now_monotonic())
                self._terminal_success_locked(("rx", entry.key), InternalEventType.RADIO_RX_WINDOW_COMPLETED, completed, Correlation(), _rx_details(entry, actual))
            else:
                code = ErrorCode.RADIO_RX_TIMEOUT if actual == 0 else ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
                self._terminal_error_locked(("rx", entry.key), code, "B210 RX window가 불완전하게 종료되었습니다", Correlation(), _rx_details(entry, actual))
            if entry in self._rx_ledger:
                self._rx_ledger.remove(entry)

    def _monitor_loop(self):
        """Advance hardware outcomes independently of public API calls."""

        while not self._monitor_stop.is_set():
            with self._condition:
                if (
                    self._monitor_stop.is_set()
                    or not self._backend_started
                    or self._state is not RadioDeviceState.RUNNING
                ):
                    return
                try:
                    self._poll_locked()
                except Exception as error:
                    self._fail_monitor_locked(
                        "B210 monitoring worker failed", type(error).__name__
                    )
                if self._state is not RadioDeviceState.RUNNING:
                    return
                backend = self._backend
            waiter = getattr(backend, "wait_for_activity", None)
            if waiter is None:
                self._monitor_stop.wait(0.01)
                continue
            try:
                waiter(0.01)
            except Exception as error:
                with self._condition:
                    if not self._monitor_stop.is_set():
                        self._fail_monitor_locked(
                            "B210 activity wait failed", type(error).__name__
                        )
                return

    def _fail_monitor_locked(self, message, reason):
        """Fail every affected operation after an unrecoverable monitor error."""

        for entry in tuple(self._tx_ledger):
            self._terminal_error_locked(
                entry.key,
                ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                message,
                Correlation(burst_id=entry.request.burst_id),
                Details.of(channel=entry.request.channel.name, reason=reason),
            )
            if self._delivery_status.failed:
                break
        if not self._delivery_status.failed:
            for entry in tuple(self._rx_ledger):
                try:
                    partial = self._read_rx(entry, allow_partial=True)
                except Exception:
                    partial = None
                actual = entry.received_sample_count
                if partial is not None:
                    actual, _missing_timestamp, _overflow_samples = (
                        self._consume_rx_result_locked(entry, partial)
                    )
                if self._delivery_status.failed:
                    break
                self._terminal_error_locked(
                    ("rx", entry.key),
                    ErrorCode.RADIO_STREAM_FAILED,
                    message,
                    Correlation(),
                    Details.of(
                        **dict(_rx_details(entry, actual).entries), reason=reason
                    ),
                )
        self._tx_ledger.clear()
        self._rx_ledger.clear()
        self._state = RadioDeviceState.FAILED
        self._emit_status_changed_locked()

    @staticmethod
    def _join_monitor(monitor, deadline_ns):
        if monitor is None or not monitor.is_alive():
            return True
        if monitor is current_thread():
            return False
        remaining_ns = max(0, deadline_ns - _wall_time.monotonic_ns())
        monitor.join(remaining_ns / 1_000_000_000)
        return not monitor.is_alive()

    def _read_rx(self, entry, *, allow_partial):
        try:
            result = self._backend.read_rx_cf32(
                entry.expected_sample_count,
                allow_partial=allow_partial,
                expected_hardware_time_ns=entry.hardware_start_ns,
            )
        except TypeError:
            try:
                result = self._backend.read_rx_cf32(
                    entry.expected_sample_count, allow_partial=allow_partial
                )
            except TypeError:
                result = self._backend.read_rx_cf32(entry.expected_sample_count)
        if result is None:
            return None
        if isinstance(result, dict):
            return result
        if len(result) == 2:
            data, hardware_ns = result
            overflow_samples = 0
        else:
            data, hardware_ns, overflow_samples = result
        segments = () if not data else ((bytes(data), hardware_ns, False),)
        return {
            "segments": segments,
            "overflow_samples": overflow_samples,
            "trailing_gap": bool(overflow_samples),
        }

    def _consume_rx_result_locked(self, entry, result):
        segments = tuple(result.get("segments", ()))
        overflow_samples = int(result.get("overflow_samples", 0))
        trailing_gap = bool(result.get("trailing_gap", overflow_samples))
        actual = sum(len(data) // 8 for data, _, _ in segments)
        entry.received_sample_count = actual
        name = entry.request.channel.name
        if overflow_samples:
            self._overflow_count += 1
            self._rx_dropped_blocks[name] += 1
            self._rx_dropped_samples[name] += overflow_samples
            details = _rx_details(entry, actual)
            error = internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_RX_OVERFLOW,
                ErrorCategory.RESOURCE,
                "B210 RX collector overflow",
                details=details,
            ).error
            self._publish_locked(
                InternalEventType.LAYER_ERROR,
                None,
                Correlation(),
                details,
                error=error,
            )
            if not self._delivery_status.failed:
                self._emit_status_changed_locked()
        missing_timestamp = False
        pending_gap = self._next_rx_gap[name]
        for data, hardware_ns, gap_before in segments:
            if not data:
                continue
            if hardware_ns is None:
                missing_timestamp = True
                continue
            continuity = (
                SampleContinuity.GAP_BEFORE
                if pending_gap or gap_before
                else SampleContinuity.CONTIGUOUS
            )
            first_utc = _hardware_to_utc(entry.mapping, hardware_ns)
            block = SampleBlock(
                SAMPLE_FORMAT_CF32LE,
                entry.request.sample_rate_hz,
                len(data) // 8,
                bytes(data),
                first_utc,
                continuity,
            )
            if not self._delivery_status.failed and self._deliver_rx_block_locked(
                entry, block, hardware_ns
            ):
                pending_gap = False
        self._next_rx_gap[name] = pending_gap or trailing_gap
        return actual, missing_timestamp, overflow_samples

    def _handle_device_disconnect_locked(self, reason):
        for affected in tuple(self._tx_ledger):
            self._terminal_error_locked(
                affected.key, ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                "B210 hardware time became unavailable while TX was submitted",
                Correlation(burst_id=affected.request.burst_id),
                Details.of(channel=affected.request.channel.name, reason=reason),
            )
        for affected in tuple(self._rx_ledger):
            try:
                partial = self._read_rx(affected, allow_partial=True)
            except Exception:
                partial = None
            actual = affected.received_sample_count
            if partial is not None:
                actual, _missing_timestamp, _overflow_samples = (
                    self._consume_rx_result_locked(affected, partial)
                )
            if self._delivery_status.failed:
                break
            self._terminal_error_locked(
                ("rx", affected.key), ErrorCode.RADIO_DEVICE_DISCONNECTED,
                "B210 hardware time is unavailable while RX is active",
                Correlation(),
                Details.of(**dict(_rx_details(affected, actual).entries), reason=reason),
            )
        self._tx_ledger.clear()
        self._rx_ledger.clear()
        self._terminal_error_locked(
            ("device-disconnect", self._generation),
            ErrorCode.RADIO_DEVICE_DISCONNECTED,
            "B210 device connection was lost",
            Correlation(), Details.of(reason=reason),
        )
        self._state = RadioDeviceState.FAILED
        self._emit_status_changed_locked()

    def _deliver_rx_block_locked(self, entry, block, hardware_ns):
        name = entry.request.channel.name
        limits = self._configuration.resource_limits
        size = len(block.data)
        if self._rx_block_pending_count[name] + 1 > limits.rx_queue_max_count or self._rx_block_pending_bytes[name] + size > limits.rx_queue_max_bytes:
            self._overflow_count += 1
            self._rx_dropped_blocks[name] += 1
            self._rx_dropped_samples[name] += block.sample_count
            self._next_rx_gap[name] = True
            details = _rx_details(entry, entry.received_sample_count)
            error = internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_RX_OVERFLOW,
                ErrorCategory.RESOURCE,
                "B210 RX block queue가 가득 차 새 block을 폐기했습니다",
                details=details,
            ).error
            self._publish_locked(
                InternalEventType.LAYER_ERROR,
                None,
                Correlation(),
                details,
                error=error,
            )
            if not self._delivery_status.failed:
                self._emit_status_changed_locked()
            return False
        self._rx_block_pending_count[name] += 1
        self._rx_block_pending_bytes[name] += size
        try:
            received = RadioSamplesReceived(
                entry.request.channel, block,
                TimePoint.from_nanoseconds(hardware_ns, ClockDomain.RADIO_HARDWARE, entry.mapping.target_clock_id),
                block.continuity,
            )
            return self._publish_locked(InternalEventType.SAMPLES_RECEIVED, received, Correlation(), Details.of(channel=name, sample_count=block.sample_count), delivery_failure_terminal=True)
        finally:
            self._rx_block_pending_count[name] -= 1
            self._rx_block_pending_bytes[name] -= size

    def _validate_tx(self, request, correlation):
        if self._configuration is None:
            return None
        pair = self._channel(request.channel)
        if pair is None:
            return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다", correlation)
        setting, binding = pair
        if not binding.tx_enabled or request.samples.sample_format != SAMPLE_FORMAT_CF32LE or request.samples.sample_rate_hz != setting.sample_rate_hz:
            return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 TX 방향/format/rate가 잘못되었습니다", correlation)
        if request.samples.first_sample_at != request.transmit_at:
            return self._error(ErrorCode.INVALID_VALUE, ErrorCategory.VALIDATION, "TX first_sample_at이 transmit_at과 다릅니다", correlation)
        if any(not math.isfinite(i) or not math.isfinite(q) or i * i + q * q > 1.0 for i, q in struct.iter_unpack("<ff", request.samples.data)):
            return self._error(ErrorCode.RADIO_TX_SAMPLE_OUT_OF_RANGE, ErrorCategory.VALIDATION, "TX cf32le sample이 finite unit circle 범위를 벗어났습니다", correlation)
        return None

    def _validate_rx(self, request):
        if self._configuration is None:
            return None
        pair = self._channel(request.channel)
        if pair is None:
            return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다")
        setting, binding = pair
        if not binding.rx_enabled or request.sample_rate_hz != setting.sample_rate_hz:
            return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 RX 방향/rate가 잘못되었습니다")
        return None

    def _channel(self, channel):
        for index, setting in enumerate(self._configuration.channels):
            if setting.channel == channel:
                return setting, self._configuration.channel_bindings[index]
        return None

    def _require_running(self, correlation=Correlation()):
        if self._configuration is None:
            return self._error(ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE, "Radio가 configure되지 않았습니다", correlation)
        if self._stopping or self._state is not RadioDeviceState.RUNNING:
            return self._error(ErrorCode.RADIO_NOT_STARTED, ErrorCategory.DEVICE, "Radio가 RUNNING 상태가 아닙니다", correlation)
        return None

    def _tx_capacity_error_locked(self, request, correlation):
        limits = self._configuration.resource_limits
        relevant = [entry for entry in self._tx_ledger if entry.request.channel == request.channel]
        channel_bytes = sum(len(entry.request.samples.data) for entry in relevant)
        shared_bytes = sum(len(entry.request.samples.data) for entry in self._tx_ledger)
        size = len(request.samples.data)
        if size > limits.tx_queue_max_bytes or len(relevant) + 1 > limits.tx_queue_max_count or channel_bytes + size > limits.tx_queue_max_bytes or len(self._tx_ledger) + 1 > limits.tx_queue_max_count or shared_bytes + size > limits.tx_queue_max_bytes:
            self._reject_tx_locked(request.channel)
            return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "B210 TX ledger capacity가 부족합니다", correlation)
        return None

    def _reject_tx_locked(self, channel):
        self._tx_rejected[channel.name] += 1
        self._emit_status_changed_locked()

    def _reject_rx_locked(self, channel):
        self._rx_rejected[channel.name] += 1
        self._emit_status_changed_locked()

    def _queue_locked(self, name):
        channel = next(value.channel for value in self._configuration.channels if value.channel.name == name)
        tx = tuple(value for value in self._tx_ledger if value.request.channel.name == name)
        rx = tuple(value for value in self._rx_ledger if value.request.channel.name == name)
        return RadioQueueStatus(
            channel, len(tx), sum(len(value.request.samples.data) for value in tx), len(rx),
            self._rx_block_pending_count[name], self._rx_block_pending_bytes[name],
            self._tx_rejected[name], self._rx_rejected[name],
            self._rx_dropped_blocks[name], self._rx_dropped_samples[name],
        )

    def _snapshot_locked(self, channel: PhysicalChannel | None = None):
        names = () if self._configuration is None else self._configuration.channel_names()
        if channel is not None:
            names = (channel.name,)
        return RadioStatusSnapshot(
            ADAPTER_KIND, self._state,
            self._configuration.clock_reference if self._configuration else ClockReference.NONE,
            self._state in (RadioDeviceState.CONFIGURED, RadioDeviceState.RUNNING)
                and self._timing_status is not None and self._timing_status.mapping_state is RadioMappingState.VALID,
            False, self._underflow_count, self._overflow_count, self._late_command_count,
            Details.of(
                configure_generation=self._generation,
                device_id=(
                    self._applied.applied_settings.device_id
                    if self._applied is not None else None
                ),
                duplicate_ack_count=self._duplicate_ack_count,
            ), self._timing_status,
            tuple(self._queue_locked(name) for name in names), self._delivery_status,
        )

    def _terminal_success_locked(self, key, event_type, payload, correlation, details):
        if key in self._terminal_keys:
            return True
        details = self._common_details_locked(details)
        self._terminal_keys.add(key)
        self._terminal_codes[key] = None
        self._terminal_details[key] = details
        return self._publish_locked(event_type, payload, correlation, details, terminal=True)

    def _terminal_error_locked(self, key, code, message, correlation, details):
        if key in self._terminal_keys:
            return True
        details = self._common_details_locked(details)
        self._terminal_keys.add(key)
        self._terminal_codes[key] = code
        self._terminal_details[key] = details
        error = internal_error(Layer.RADIO, code, _category_for(code), message, correlation=correlation, details=details).error
        return self._publish_locked(InternalEventType.LAYER_ERROR, None, correlation, details, error=error, terminal=True)

    def _emit_status_changed_locked(self):
        if self._delivery_status.failed:
            return False
        return self._publish_locked(InternalEventType.RADIO_STATUS_CHANGED, self._snapshot_locked(), Correlation(), Details.of(configure_generation=self._generation))

    def _publish_locked(self, event_type, payload, correlation, details, *, error=None, terminal=False, delivery_failure_terminal=False):
        if self._delivery_status.failed:
            if terminal:
                self._record_undelivered_terminals_locked(1)
            return False
        details = self._common_details_locked(details)
        if error is not None and error.details != details:
            error = replace(error, details=details)
        event = InternalEvent(
            self._id_source.new_event_id(), event_type, self._time_source.now_monotonic(),
            EventSeverity.ERROR if error else EventSeverity.INFO, Layer.RADIO,
            correlation, payload, error, details,
        )
        try:
            result = self._event_sink.publish(event)
            delivered = not result.is_err and result.value.delivered
        except Exception:
            delivered = False
        if delivered:
            return True
        now = self._time_source.now_monotonic()
        self._delivery_status = RadioEventDeliveryStatus(
            True, self._delivery_status.undelivered_terminal_count + (1 if terminal else 0),
            self._delivery_status.first_failure_at or now, now,
        )
        self._state = RadioDeviceState.FAILED
        self._terminalize_delivery_failure_pending_locked()
        return False

    def _record_undelivered_terminals_locked(self, count):
        if count <= 0:
            return
        now = self._time_source.now_monotonic()
        self._delivery_status = RadioEventDeliveryStatus(
            True,
            self._delivery_status.undelivered_terminal_count + count,
            self._delivery_status.first_failure_at or now,
            now,
        )

    def _terminalize_delivery_failure_pending_locked(self):
        """Commit submitted hardware work after the runtime event channel dies."""

        for entry in tuple(self._tx_ledger):
            self._terminal_error_locked(
                entry.key,
                ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
                "event delivery failure made the submitted B210 TX outcome unknown",
                Correlation(burst_id=entry.request.burst_id),
                Details.of(channel=entry.request.channel.name),
            )
        for entry in tuple(self._rx_ledger):
            if entry.received_sample_count == 0 and self._backend_started:
                try:
                    partial = self._read_rx(entry, allow_partial=True)
                except Exception:
                    partial = None
                if partial is not None:
                    entry.received_sample_count = sum(
                        len(data) // 8
                        for data, _hardware_ns, _gap_before
                        in partial.get("segments", ())
                    )
            self._terminal_error_locked(
                ("rx", entry.key),
                ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
                "event delivery failure made the active B210 RX window incomplete",
                Correlation(),
                _rx_details(entry, entry.received_sample_count),
            )
        self._tx_ledger.clear()
        self._rx_ledger.clear()

    def _cleanup_backend(self, backend, timeout_ns):
        try:
            if hasattr(backend, "close"):
                return self._bounded_backend_call(backend.close, timeout_ns)
            else:
                return self._stop_backend(backend, timeout_ns)
        except Exception:
            return False

    def _retain_failed_backend_locked(self, backend, configuration):
        """Preserve a partially mutated backend so FAILED.stop() can retry cleanup."""

        self._backend = backend
        self._configuration = configuration
        self._applied = None
        self._timing_status = None
        self._backend_started = False
        names = configuration.channel_names()
        self._tx_rejected = {name: 0 for name in names}
        self._rx_rejected = {name: 0 for name in names}
        self._rx_dropped_blocks = {name: 0 for name in names}
        self._rx_dropped_samples = {name: 0 for name in names}
        self._rx_block_pending_count = {name: 0 for name in names}
        self._rx_block_pending_bytes = {name: 0 for name in names}
        self._next_rx_gap = {name: False for name in names}
        self._state = RadioDeviceState.FAILED

    @staticmethod
    def _stop_backend(backend, timeout_ns):
        return B210RadioAdapter._bounded_backend_call(backend.stop, timeout_ns)

    @staticmethod
    def _bounded_backend_call(method, timeout_ns):
        """Bound even a backend method that blocks before starting its own wait."""

        finished = Event()
        results = []
        failures = []

        def invoke():
            try:
                try:
                    results.append(method(timeout_ns))
                except TypeError:
                    results.append(method())
            except Exception as error:
                failures.append(error)
            finally:
                finished.set()

        worker = Thread(target=invoke, name="vdeter-b210-backend-cleanup", daemon=True)
        worker.start()
        if not finished.wait(timeout_ns / 1_000_000_000):
            return False
        if failures:
            raise failures[0]
        return bool(results) and results[0] is not False

    def _delivery_error(self):
        return self._error(ErrorCode.RADIO_EVENT_DELIVERY_FAILED, ErrorCategory.INTERNAL, "Runtime event sink가 Radio event를 전달하지 못했습니다")

    def _common_details_locked(self, details):
        values = dict(details.entries)
        if self._generation:
            values.setdefault("configure_generation", self._generation)
        if self._applied is not None:
            values.setdefault(
                "device_id", self._applied.applied_settings.device_id
            )
        return Details.of(**values)

    def _error(self, code, category, message, correlation=Correlation(), details=Details()):
        return internal_error(
            Layer.RADIO,
            code,
            category,
            message,
            correlation=correlation,
            details=self._common_details_locked(details),
        )


def _applied_channel(setting, binding, readback):
    return RadioAppliedChannelSetting(
        setting.channel, setting.center_frequency_hz, readback["center_frequency_hz"],
        setting.sample_rate_hz, readback["tx_sample_rate_hz"], readback["rx_sample_rate_hz"],
        setting.tx_gain_db, setting.rx_gain_db, readback["tx_gain_db"], readback["rx_gain_db"],
        binding.analog_bandwidth_hz, readback["tx_bandwidth_hz"], readback["rx_bandwidth_hz"], binding,
    )


def _validate_readback(configuration, expected_serial, readback):
    try:
        if readback["serial"] != expected_serial or readback["sink_serial"] != expected_serial or not readback["same_timebase"]:
            raise ValueError("serial/timebase")
        if readback["master_clock_rate_hz"] != 30_720_000 or readback["sink_master_clock_rate_hz"] != 30_720_000:
            raise ValueError("master_clock")
        if (
            readback["clock_source"] != "internal"
            or readback["time_source"] != "internal"
            or readback["sink_clock_source"] != "internal"
            or readback["sink_time_source"] != "internal"
            or not readback["hardware_time_reset"]
        ):
            raise ValueError("clock/time")
        if readback["cpu_format"] != "fc32" or readback["otw_format"] != "sc16":
            raise ValueError("stream_format")
        bindings = {value.channel.name: value for value in configuration.channel_bindings}
        for setting in configuration.channels:
            binding = bindings[setting.channel.name]
            actual = readback["channels"][setting.channel.name]
            if actual["center_frequency_hz"] != setting.center_frequency_hz:
                raise ValueError("center_frequency")
            if binding.tx_enabled and (
                actual["tx_center_frequency_hz"] != setting.center_frequency_hz
                or actual["tx_sample_rate_hz"] != 76_800 or actual["tx_gain_db"] != setting.tx_gain_db
                or actual["tx_bandwidth_hz"] < binding.analog_bandwidth_hz
                or actual["tx_antenna"] != "TX/RX" or actual["tx_device_channel"] != 0
            ):
                raise ValueError("tx_readback")
            if binding.rx_enabled and (
                actual["rx_center_frequency_hz"] != setting.center_frequency_hz
                or actual["rx_sample_rate_hz"] != 76_800 or actual["rx_gain_db"] != setting.rx_gain_db
                or actual["rx_bandwidth_hz"] < binding.analog_bandwidth_hz
                or actual["rx_antenna"] != "RX2" or actual["rx_device_channel"] != 0
            ):
                raise ValueError("rx_readback")
    except (KeyError, TypeError, ValueError) as error:
        return internal_error(
            Layer.RADIO, ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE,
            "B210 readback이 승인된 exact 설정과 다릅니다",
            details=Details.of(reason=str(error)),
        )
    return None


def _rx_details(entry, received_sample_count=None):
    request = entry.request
    values = dict(
        configure_generation=entry.key[0], channel=request.channel.name,
        requested_start_seconds=request.start_at.seconds,
        requested_start_nanoseconds=request.start_at.nanoseconds,
        requested_clock_id=request.start_at.clock_id,
        requested_duration_nanoseconds=request.duration_nanoseconds,
        expected_sample_count=entry.expected_sample_count,
    )
    if received_sample_count is not None:
        values["received_sample_count"] = received_sample_count
    return Details.of(**values)


def _normalize_async(value):
    if isinstance(value, str):
        return {"outcome": value, "hardware_time_ns": None}
    return {
        "outcome": str(value.get("outcome", "STREAM_ERROR")),
        "hardware_time_ns": value.get("hardware_time_ns"),
    }


def _hardware_to_utc(mapping, hardware_ns):
    offset = hardware_ns - mapping.target_reference.total_nanoseconds()
    return mapping.source_reference.shifted(offset)


def _duration_ceil_ns(sample_count, sample_rate_hz):
    return (sample_count * 1_000_000_000 + sample_rate_hz - 1) // sample_rate_hz


def _round_half_up_samples(duration_ns, sample_rate_hz):
    return (duration_ns * sample_rate_hz + 500_000_000) // 1_000_000_000


def _overlap(left_start, left_end, right_start, right_end):
    return left_start < right_end and right_start < left_end


def _category_for(code):
    if code in (ErrorCode.RADIO_OPERATION_CANCELLED, ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCode.RADIO_RX_OVERFLOW):
        return ErrorCategory.RESOURCE
    if code in (ErrorCode.RADIO_RX_TIMEOUT, ErrorCode.RADIO_STOP_TIMEOUT, ErrorCode.RADIO_LATE_REQUEST):
        return ErrorCategory.TIMEOUT
    if code == ErrorCode.RADIO_EVENT_DELIVERY_FAILED:
        return ErrorCategory.INTERNAL
    return ErrorCategory.DEVICE


def _valid_selector(value):
    if not isinstance(value, str) or not value.startswith("serial="):
        return False
    serial = value[7:]
    return bool(serial and serial.isascii() and serial.isprintable() and not any(character.isspace() for character in serial) and "," not in serial and "=" not in serial)


def _validate_b210_configuration(configuration: RadioConfiguration):
    if configuration.adapter_kind != ADAPTER_KIND or configuration.sample_format != SAMPLE_FORMAT_CF32LE:
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 adapter/cf32le 설정이 필요합니다")
    if not _valid_selector(configuration.device_selector) or configuration.clock_reference is not ClockReference.INTERNAL or configuration.timing is None or configuration.resource_limits is None or configuration.b210_stream is None or not configuration.channels or len(configuration.channels) != len(configuration.channel_bindings):
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 selector/timing/resource/stream/binding 설정이 잘못되었습니다")
    if configuration.timing.profile is not RadioTimingProfile.INTERNAL_FREE_RUNNING or configuration.timing.clock_source is not ClockReference.INTERNAL or configuration.timing.time_source is not ClockReference.INTERNAL or configuration.timing.pps_source is not ClockReference.NONE:
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 timing profile이 승인값과 다릅니다")
    timing = configuration.timing
    if (
        timing.mapping_attempt_count != 8
        or timing.mapping_valid_for_nanoseconds != 1_000_000_000
        or timing.max_mapping_uncertainty_ns != 10_000_000
    ):
        return internal_error(
            Layer.RADIO,
            ErrorCode.RADIO_UNSUPPORTED_SETTING,
            ErrorCategory.VALIDATION,
            "B210 timing 수치가 승인 profile과 다릅니다",
        )
    names = tuple(setting.channel.name for setting in configuration.channels)
    frequencies = tuple(
        setting.center_frequency_hz for setting in configuration.channels
    )
    directions = tuple(
        (binding.tx_enabled, binding.rx_enabled)
        for binding in configuration.channel_bindings
    )
    if (
        names != ("VDETER_LOWER_25_TEST", "VDETER_UPPER_25_TEST")
        or frequencies != (157_237_500, 161_837_500)
        or directions
        not in (
            ((True, False), (True, True)),
            ((False, True), (True, False)),
        )
    ):
        return internal_error(
            Layer.RADIO,
            ErrorCode.RADIO_UNSUPPORTED_SETTING,
            ErrorCategory.VALIDATION,
            "B210 channel 순서·주파수·역할 방향이 승인 profile과 다릅니다",
        )
    for setting, binding in zip(configuration.channels, configuration.channel_bindings, strict=True):
        if (
            setting.channel != binding.channel
            or setting.sample_rate_hz != 76_800
            or not 0 <= setting.tx_gain_db <= 89
            or not 0 <= setting.rx_gain_db <= 76
            or binding.waveform_bandwidth_hz != 25_000
            or binding.analog_bandwidth_hz != 200_000
        ):
            return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 channel setting이 승인값과 다릅니다")
        for enabled, device_channel, antenna, expected in (
            (binding.tx_enabled, binding.tx_device_channel, binding.tx_antenna, "TX/RX"),
            (binding.rx_enabled, binding.rx_device_channel, binding.rx_antenna, "RX2"),
        ):
            if enabled and (device_channel != 0 or antenna != expected):
                return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 활성 방향 binding이 잘못되었습니다")
            if not enabled and (device_channel is not None or antenna is not None):
                return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "B210 비활성 방향 binding이 잘못되었습니다")
    return None
