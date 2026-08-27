"""Stage 3 deterministic Software Radio adapter."""

from __future__ import annotations

import math
import struct
import time as _wall_time
from dataclasses import dataclass
from threading import Condition, RLock

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
from .faults import SoftwareRadioFault
from .medium import SoftwareRadioMedium

ADAPTER_KIND = RadioAdapterKind.SOFTWARE.value
_UTC_FLAGS = (
    RadioUtcFlag.HOST_ESTIMATE,
    RadioUtcFlag.NOT_UTC_DISCIPLINED,
    RadioUtcFlag.UTC_ACCURACY_UNVERIFIED,
)
_UTC_JUMP_NS = 10_000_000


@dataclass(slots=True)
class _QueueCounters:
    tx_rejected_count: int = 0
    rx_window_rejected_count: int = 0
    rx_dropped_block_count: int = 0
    rx_dropped_sample_count: int = 0


@dataclass(frozen=True, slots=True)
class _PendingTx:
    key: tuple
    request: RadioTxRequest
    hardware_start_ns: int
    hardware_end_ns: int


@dataclass(slots=True)
class _PendingRx:
    key: tuple
    request: RadioRxRequest
    expected_sample_count: int
    hardware_start_ns: int
    hardware_end_ns: int
    mapping: ClockMapping
    received_sample_count: int = 0


class SoftwareRadioAdapter:
    """Deterministic implementation of the public ``RadioAdapter`` contract."""

    __slots__ = (
        "_time_source", "_id_source", "_event_sink", "_endpoint_id", "_medium",
        "_configuration", "_applied", "_device_state", "_configure_generation",
        "_timing_status", "_mapping_utc_reference_ns", "_mapping_monotonic_reference_ns",
        "_hardware_origin_monotonic_ns", "_utc_jump_blocked", "_tx_pending", "_rx_pending",
        "_terminal_keys", "_terminal_codes", "_terminal_details", "_counters", "_rx_block_pending_count", "_rx_block_pending_bytes",
        "_next_rx_gap", "_underflow_count", "_overflow_count", "_late_command_count",
        "_delivery_status", "_last_stopped", "_faults", "_consumed_faults", "_fault_seed",
        "_minimum_schedule_lead_ns", "_minimum_rx_schedule_lead_ns",
        "_minimum_tx_channel_switch_gap_ns", "_lock", "_condition", "_stopping",
    )

    def __init__(
        self,
        time_source: TimeSource | None = None,
        id_source: IdSource | None = None,
        event_sink=None,
        *,
        endpoint_id: str = "software",
        medium: SoftwareRadioMedium | None = None,
        faults: tuple[SoftwareRadioFault, ...] = (),
        fault_seed: int | None = None,
        minimum_schedule_lead_ns: int = 0,
        minimum_rx_schedule_lead_ns: int = 0,
        minimum_tx_channel_switch_gap_ns: int = 0,
    ) -> None:
        self._time_source = time_source or SystemTimeSource()
        self._id_source = id_source or Uuid4IdSource()
        self._event_sink = event_sink or RecordingInternalEventSink()
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError("endpoint_id must be nonempty")
        for name, value in (
            ("minimum_schedule_lead_ns", minimum_schedule_lead_ns),
            ("minimum_rx_schedule_lead_ns", minimum_rx_schedule_lead_ns),
            ("minimum_tx_channel_switch_gap_ns", minimum_tx_channel_switch_gap_ns),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 60_000_000_000:
                raise ValueError(f"{name} must be an integer in 0..60000000000")
        self._endpoint_id = endpoint_id
        self._medium = medium or SoftwareRadioMedium()
        self._configuration: RadioConfiguration | None = None
        self._applied: RadioConfigurationApplied | None = None
        self._device_state = RadioDeviceState.DISCONNECTED
        self._configure_generation = 0
        self._timing_status: RadioTimingStatus | None = None
        self._mapping_utc_reference_ns = 0
        self._mapping_monotonic_reference_ns = 0
        self._hardware_origin_monotonic_ns = 0
        self._utc_jump_blocked = False
        self._tx_pending: dict[str, _PendingTx] = {}
        self._rx_pending: dict[tuple, _PendingRx] = {}
        self._terminal_keys: set[tuple] = set()
        self._terminal_codes: dict[tuple, ErrorCode | None] = {}
        self._terminal_details: dict[tuple, Details] = {}
        self._counters: dict[str, _QueueCounters] = {}
        self._rx_block_pending_count: dict[str, int] = {}
        self._rx_block_pending_bytes: dict[str, int] = {}
        self._next_rx_gap: dict[str, bool] = {}
        self._underflow_count = self._overflow_count = self._late_command_count = 0
        self._delivery_status = RadioEventDeliveryStatus(False, 0, None, None)
        self._last_stopped: RadioStopped | None = None
        if not isinstance(faults, tuple) or any(not isinstance(value, SoftwareRadioFault) for value in faults):
            raise ValueError("faults must be a tuple of SoftwareRadioFault")
        if fault_seed is not None and (isinstance(fault_seed, bool) or not isinstance(fault_seed, int)):
            raise ValueError("fault_seed must be an int or None")
        self._faults = faults
        self._consumed_faults: set[int] = set()
        self._fault_seed = fault_seed
        self._minimum_schedule_lead_ns = minimum_schedule_lead_ns
        self._minimum_rx_schedule_lead_ns = minimum_rx_schedule_lead_ns
        self._minimum_tx_channel_switch_gap_ns = minimum_tx_channel_switch_gap_ns
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stopping = False

    def configure(self, request: RadioConfigureRequest):
        with self._condition:
            if not isinstance(request, RadioConfigureRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioConfigureRequest가 필요합니다")
            if self._stopping or self._device_state not in (RadioDeviceState.DISCONNECTED, RadioDeviceState.STOPPED):
                return self._error(ErrorCode.RADIO_CONFIGURATION_FAILED, ErrorCategory.DEVICE, "현재 lifecycle state에서는 configure할 수 없습니다")
            validation = _validate_software_configuration(request.configuration)
            if validation is not None:
                return validation
            configure_fault = self._find_fault_locked("configure")
            if configure_fault is not None:
                return self._error(configure_fault.error_code, ErrorCategory.DEVICE, "Software configure fault가 주입되었습니다")

            configuration = request.configuration
            generation = self._configure_generation + 1
            capabilities = RadioCapabilities(
                (SAMPLE_FORMAT_CF32LE,), self._minimum_schedule_lead_ns,
                self._minimum_rx_schedule_lead_ns, self._minimum_tx_channel_switch_gap_ns,
                True, (RadioTimingProfile.INTERNAL_FREE_RUNNING,), configuration.resource_limits,
            )
            bindings = {value.channel.name: value for value in configuration.channel_bindings}
            applied_channels = tuple(
                RadioAppliedChannelSetting(
                    value.channel, value.center_frequency_hz, value.center_frequency_hz,
                    value.sample_rate_hz,
                    value.sample_rate_hz if bindings[value.channel.name].tx_enabled else None,
                    value.sample_rate_hz if bindings[value.channel.name].rx_enabled else None,
                    value.tx_gain_db, value.rx_gain_db,
                    value.tx_gain_db if bindings[value.channel.name].tx_enabled else None,
                    value.rx_gain_db if bindings[value.channel.name].rx_enabled else None,
                    bindings[value.channel.name].analog_bandwidth_hz,
                    bindings[value.channel.name].analog_bandwidth_hz if bindings[value.channel.name].tx_enabled else None,
                    bindings[value.channel.name].analog_bandwidth_hz if bindings[value.channel.name].rx_enabled else None,
                    bindings[value.channel.name],
                ) for value in configuration.channels
            )
            settings = RadioAppliedSettings(
                f"software:{self._endpoint_id}", generation, applied_channels,
                None, None, None, None,
            )
            applied = RadioConfigurationApplied(
                ADAPTER_KIND, RadioDeviceState.CONFIGURED,
                tuple(value.channel.name for value in applied_channels), SAMPLE_FORMAT_CF32LE,
                capabilities, settings,
            )
            self._configuration = configuration
            self._applied = applied
            self._configure_generation = generation
            self._device_state = RadioDeviceState.CONFIGURED
            self._hardware_origin_monotonic_ns = self._time_source.now_monotonic().total_nanoseconds()
            self._tx_pending.clear()
            self._rx_pending.clear()
            self._terminal_keys.clear()
            self._terminal_codes.clear()
            self._terminal_details.clear()
            self._counters = {value.channel.name: _QueueCounters() for value in configuration.channels}
            self._rx_block_pending_count = {value.channel.name: 0 for value in configuration.channels}
            self._rx_block_pending_bytes = {value.channel.name: 0 for value in configuration.channels}
            self._next_rx_gap = {value.channel.name: False for value in configuration.channels}
            self._underflow_count = self._overflow_count = self._late_command_count = 0
            self._delivery_status = RadioEventDeliveryStatus(False, 0, None, None)
            self._last_stopped = None
            self._utc_jump_blocked = False
            self._acquire_mapping_locked()
            if not self._emit_status_changed_locked():
                return self._delivery_error()
            return Ok(applied)

    def start(self, request: RadioStartRequest):
        with self._condition:
            if not isinstance(request, RadioStartRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStartRequest가 필요합니다")
            if self._stopping or self._device_state is not RadioDeviceState.CONFIGURED:
                code = ErrorCode.RADIO_NOT_CONFIGURED if self._configuration is None else ErrorCode.RADIO_ALREADY_STARTED
                return self._error(code, ErrorCategory.DEVICE, "start는 CONFIGURED state에서만 허용합니다")
            self._device_state = RadioDeviceState.RUNNING
            started = RadioStarted(ADAPTER_KIND, self._device_state, self._time_source.now_monotonic())
            if not self._emit_status_changed_locked():
                return self._delivery_error()
            return Ok(started)

    def stop(self, request: RadioStopRequest):
        if not isinstance(request, RadioStopRequest):
            return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioStopRequest가 필요합니다")
        with self._condition:
            if self._device_state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                return Ok(self._last_stopped)
            if self._device_state is RadioDeviceState.DISCONNECTED:
                return self._error(ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE, "configure 전에는 stop할 수 없습니다")
            timeout_ns = self._configuration.resource_limits.stop_timeout_ns
            if self._stopping:
                deadline = _wall_time.monotonic_ns() + timeout_ns
                while self._stopping:
                    remaining = deadline - _wall_time.monotonic_ns()
                    if remaining <= 0:
                        return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "동시 stop cleanup의 bounded wait가 만료되었습니다")
                    self._condition.wait(remaining / 1_000_000_000)
                if self._device_state is RadioDeviceState.STOPPED and self._last_stopped is not None:
                    return Ok(self._last_stopped)
                return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "공유 cleanup이 성공하지 않았습니다")
            self._stopping = True
            try:
                self._process_due_locked()
                for burst_value, pending in tuple(self._tx_pending.items()):
                    self._commit_error_terminal_locked(
                        pending.key, ErrorCode.RADIO_OPERATION_CANCELLED,
                        "stop이 접수된 TX를 취소했습니다", Correlation(burst_id=pending.request.burst_id),
                        Details.of(channel=pending.request.channel.name),
                    )
                    self._medium.cancel(burst_value, source_endpoint_id=self._endpoint_id, configure_generation=self._configure_generation)
                for pending in tuple(self._rx_pending.values()):
                    if self._delivery_status.failed:
                        break
                    self._interrupt_rx_locked(
                        pending,
                        "stop이 sample 미수신 RX window를 취소했습니다",
                        "stop이 부분 수신된 RX window를 중단했습니다",
                    )
                    self._medium.release_rx(self._endpoint_id, pending.key)
                self._tx_pending.clear()
                self._rx_pending.clear()
                if self._find_fault_locked("stop") is not None:
                    self._device_state = RadioDeviceState.FAILED
                    return self._error(ErrorCode.RADIO_STOP_TIMEOUT, ErrorCategory.TIMEOUT, "Software cleanup timeout fault가 주입되었습니다")
                if self._delivery_status.failed:
                    self._device_state = RadioDeviceState.STOPPED
                    self._last_stopped = RadioStopped(
                        ADAPTER_KIND, self._device_state,
                        self._time_source.now_monotonic(),
                    )
                    return Ok(self._last_stopped)
                self._device_state = RadioDeviceState.STOPPED
                self._last_stopped = RadioStopped(ADAPTER_KIND, self._device_state, self._time_source.now_monotonic())
                if not self._emit_status_changed_locked():
                    self._device_state = RadioDeviceState.FAILED
                    return self._delivery_error()
                return Ok(self._last_stopped)
            finally:
                self._stopping = False
                self._condition.notify_all()

    def _interrupt_rx_locked(
        self,
        pending: _PendingRx,
        cancelled_message: str,
        incomplete_message: str,
    ) -> None:
        hardware_now = self._hardware_now_ns_locked()
        elapsed_ns = max(
            0,
            min(
                pending.request.duration_nanoseconds,
                hardware_now - pending.hardware_start_ns,
            ),
        )
        actual = min(
            pending.expected_sample_count,
            _round_half_up_samples(
                elapsed_ns, pending.request.sample_rate_hz
            ),
        )
        if actual <= 0:
            self._commit_error_terminal_locked(
                ("rx", pending.key),
                ErrorCode.RADIO_OPERATION_CANCELLED,
                cancelled_message,
                Correlation(),
                _rx_details(pending, 0),
            )
            return
        data = self._medium.render(
            self._endpoint_id,
            pending.request.channel,
            pending.request.start_at,
            pending.request.sample_rate_hz,
            actual,
        )
        pending.received_sample_count = actual
        block = SampleBlock(
            SAMPLE_FORMAT_CF32LE,
            pending.request.sample_rate_hz,
            actual,
            data,
            pending.request.start_at,
            SampleContinuity.CONTIGUOUS,
        )
        self._deliver_rx_block_locked(
            pending, block, pending.hardware_start_ns
        )
        if self._delivery_status.failed:
            return
        self._commit_error_terminal_locked(
            ("rx", pending.key),
            ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
            incomplete_message,
            Correlation(),
            _rx_details(pending, actual),
        )

    def schedule_tx(self, request: RadioTxRequest):
        correlation = Correlation(burst_id=request.burst_id) if isinstance(request, RadioTxRequest) else Correlation()
        with self._condition:
            if not isinstance(request, RadioTxRequest):
                return self._error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, "RadioTxRequest가 필요합니다", correlation)
            validation = self._validate_tx(request, correlation)
            if validation is not None:
                return validation
            state = self._require_running(correlation)
            if state is not None:
                return state
            self._process_due_locked()
            state = self._require_running(correlation)
            if state is not None:
                return state
            duration_ns = _duration_ceil_ns(request.samples.sample_count, request.samples.sample_rate_hz)
            mapping = self._ensure_mapping_locked(request.transmit_at, request.transmit_at.shifted(duration_ns), correlation)
            if mapping.is_err:
                self._reject_tx_locked(request.channel)
                return mapping
            hardware_start = mapping.value
            hardware_end = hardware_start + duration_ns
            remaining = hardware_start - self._hardware_now_ns_locked()
            if remaining < self._minimum_schedule_lead_ns:
                self._late_command_count += 1
                self._reject_tx_locked(request.channel)
                return self._error(
                    ErrorCode.RADIO_LATE_REQUEST, ErrorCategory.TIMEOUT, "TX scheduling lead가 부족합니다", correlation,
                    Details.of(required_lead_ns=self._minimum_schedule_lead_ns, remaining_lead_ns=remaining),
                )
            terminal_key = ("tx", self._configure_generation, request.burst_id.value)
            if request.burst_id.value in self._tx_pending or terminal_key in self._terminal_keys:
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "현재 generation에서 burst_id가 이미 사용 중입니다", correlation)
            if any(_overlap(hardware_start, hardware_end, value.hardware_start_ns, value.hardware_end_ns) for value in self._tx_pending.values()):
                self._reject_tx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "같은 logical device TX resource의 시간 구간이 겹칩니다", correlation)
            capacity = self._tx_capacity_error_locked(request, correlation)
            if capacity is not None:
                return capacity
            pending = _PendingTx(terminal_key, request, hardware_start, hardware_end)
            try:
                self._medium.submit(self._endpoint_id, request, self._configure_generation)
            except Exception as error:
                return self._error(ErrorCode.RADIO_STREAM_FAILED, ErrorCategory.DEVICE, "Software medium 제출이 실패했습니다", correlation, Details.of(reason=type(error).__name__))
            self._tx_pending[request.burst_id.value] = pending
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
            self._process_due_locked()
            state = self._require_running()
            if state is not None:
                return state
            expected = _round_half_up_samples(request.duration_nanoseconds, request.sample_rate_hz)
            if expected <= 0:
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.INVALID_VALUE, ErrorCategory.VALIDATION, "RX expected sample count가 0입니다")
            mapping = self._ensure_mapping_locked(request.start_at, request.start_at.shifted(request.duration_nanoseconds), Correlation())
            if mapping.is_err:
                self._reject_rx_locked(request.channel)
                return mapping
            hardware_start = mapping.value
            hardware_end = hardware_start + request.duration_nanoseconds
            remaining = hardware_start - self._hardware_now_ns_locked()
            if remaining < self._minimum_rx_schedule_lead_ns:
                self._late_command_count += 1
                self._reject_rx_locked(request.channel)
                return self._error(
                    ErrorCode.RADIO_LATE_REQUEST, ErrorCategory.TIMEOUT, "RX scheduling lead가 부족합니다",
                    details=Details.of(required_lead_ns=self._minimum_rx_schedule_lead_ns, remaining_lead_ns=remaining),
                )
            key = (
                self._configure_generation, request.channel.name, request.start_at.seconds,
                request.start_at.nanoseconds, request.start_at.clock_id, request.duration_nanoseconds,
            )
            if key in self._rx_pending or ("rx", key) in self._terminal_keys:
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "RX window key가 중복되었습니다")
            if any(_overlap(hardware_start, hardware_end, value.hardware_start_ns, value.hardware_end_ns) for value in self._rx_pending.values()):
                self._reject_rx_locked(request.channel)
                return self._error(ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCategory.RESOURCE, "같은 logical RX device resource의 window가 겹칩니다")
            capacity = self._rx_capacity_error_locked(request.channel, expected)
            if capacity is not None:
                return capacity
            pending = _PendingRx(
                key,
                request,
                expected,
                hardware_start,
                hardware_end,
                self._timing_status.mapping,
            )
            try:
                self._medium.register_rx(
                    self._endpoint_id,
                    key,
                    request.channel,
                    request.start_at,
                    request.sample_rate_hz,
                    expected,
                )
            except Exception as error:
                return self._error(
                    ErrorCode.RADIO_STREAM_FAILED,
                    ErrorCategory.DEVICE,
                    "Software medium RX reservation failed",
                    details=Details.of(reason=type(error).__name__),
                )
            self._rx_pending[key] = pending
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
            self._process_due_locked()
            if request.channel is not None and (self._configuration is None or request.channel.name not in self._configuration.channel_names()):
                return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다", details=Details.of(channel=request.channel.name))
            return Ok(self._snapshot_locked(request.channel))

    def process_due(self) -> None:
        """Test-scope scheduling pump; public adapter methods call it automatically."""
        with self._condition:
            self._process_due_locked()

    def _process_due_locked(self) -> None:
        if self._configuration is None:
            return
        self._refresh_mapping_locked()
        if self._device_state is not RadioDeviceState.RUNNING:
            return
        runtime_fault = self._find_fault_locked("runtime", hardware_time_ns=self._hardware_now_ns_locked())
        if runtime_fault is not None and runtime_fault.error_code == ErrorCode.RADIO_DEVICE_DISCONNECTED:
            self._state_failed_with_pending_locked(ErrorCode.RADIO_DEVICE_DISCONNECTED, "Software device disconnect fault가 주입되었습니다")
            return
        hardware_now = self._hardware_now_ns_locked()
        for burst_value, pending in tuple(self._tx_pending.items()):
            if burst_value not in self._tx_pending:
                continue
            if pending.hardware_end_ns > hardware_now:
                continue
            fault = self._find_fault_locked(
                "tx", pending.request.channel, pending.request.burst_id,
                hardware_time_ns=pending.hardware_end_ns,
            )
            if fault is not None:
                counter_changed = False
                if fault.error_code == ErrorCode.RADIO_TX_UNDERFLOW:
                    self._underflow_count += 1
                    counter_changed = True
                elif fault.error_code == ErrorCode.RADIO_LATE_COMMAND:
                    self._late_command_count += 1
                    counter_changed = True
                self._commit_error_terminal_locked(
                    pending.key, fault.error_code, "Software TX fault가 주입되었습니다",
                    Correlation(burst_id=pending.request.burst_id),
                    Details.of(channel=pending.request.channel.name, sample_index=fault.sample_index, hardware_time_ns=pending.hardware_end_ns),
                )
                if counter_changed and not self._delivery_status.failed:
                    self._emit_status_changed_locked()
            else:
                completion = RadioTxCompleted(pending.request.burst_id, pending.request.channel, self._time_source.now_monotonic())
                self._commit_success_terminal_locked(
                    pending.key, InternalEventType.BURST_COMPLETED, completion,
                    Correlation(burst_id=pending.request.burst_id),
                    Details.of(channel=pending.request.channel.name, hardware_time_ns=pending.hardware_end_ns),
                )
            self._tx_pending.pop(burst_value, None)
        for key, pending in tuple(self._rx_pending.items()):
            if key not in self._rx_pending:
                continue
            if pending.hardware_end_ns <= hardware_now:
                try:
                    self._complete_rx_locked(pending)
                finally:
                    self._medium.release_rx(self._endpoint_id, pending.key)
                    self._rx_pending.pop(key, None)

    def _complete_rx_locked(self, pending: _PendingRx) -> None:
        request = pending.request
        fault = self._find_fault_locked("rx", request.channel, rx_key=pending.key, hardware_time_ns=pending.hardware_end_ns)
        data = self._medium.render(self._endpoint_id, request.channel, request.start_at, request.sample_rate_hz, pending.expected_sample_count)
        received_count = 0
        delivery_failed = False
        if fault is None:
            block = SampleBlock(SAMPLE_FORMAT_CF32LE, request.sample_rate_hz, pending.expected_sample_count, data, request.start_at, SampleContinuity.CONTIGUOUS)
            pending.received_sample_count = pending.expected_sample_count
            delivered = self._deliver_rx_block_locked(pending, block, pending.hardware_start_ns)
            received_count = pending.expected_sample_count if delivered else 0
            delivery_failed = self._delivery_status.failed
        elif fault.error_code in (ErrorCode.RADIO_RX_OVERFLOW, ErrorCode.RADIO_RX_WINDOW_INCOMPLETE, ErrorCode.RADIO_RESOURCE_EXHAUSTED):
            drop_index = min(pending.expected_sample_count - 1, fault.sample_index if fault.sample_index is not None else pending.expected_sample_count // 2)
            counters = self._counters[request.channel.name]
            counters.rx_dropped_block_count += 1
            counters.rx_dropped_sample_count += 1
            self._overflow_count += 1
            pending.received_sample_count = pending.expected_sample_count - 1
            details = _rx_details(pending, pending.expected_sample_count - 1)
            overflow_error = internal_error(Layer.RADIO, ErrorCode.RADIO_RX_OVERFLOW, ErrorCategory.RESOURCE, "Software RX sample drop/overflow fault", details=details).error
            self._publish_locked(InternalEventType.LAYER_ERROR, None, Correlation(), details, error=overflow_error)
            if not self._delivery_status.failed:
                self._emit_status_changed_locked()
            if not self._delivery_status.failed:
                if drop_index:
                    prefix = SampleBlock(SAMPLE_FORMAT_CF32LE, request.sample_rate_hz, drop_index, data[: drop_index * 8], request.start_at, SampleContinuity.CONTIGUOUS)
                    if self._deliver_rx_block_locked(pending, prefix, pending.hardware_start_ns):
                        received_count += drop_index
                self._next_rx_gap[request.channel.name] = True
                suffix_count = pending.expected_sample_count - drop_index - 1
                if suffix_count and not self._delivery_status.failed:
                    suffix = SampleBlock(
                        SAMPLE_FORMAT_CF32LE, request.sample_rate_hz, suffix_count,
                        data[(drop_index + 1) * 8 :],
                        request.start_at.shifted(((drop_index + 1) * 1_000_000_000) // request.sample_rate_hz),
                        SampleContinuity.GAP_BEFORE,
                    )
                    suffix_hardware = pending.hardware_start_ns + ((drop_index + 1) * 1_000_000_000) // request.sample_rate_hz
                    if self._deliver_rx_block_locked(pending, suffix, suffix_hardware):
                        received_count += suffix_count
            delivery_failed = self._delivery_status.failed
        elif fault.error_code == ErrorCode.RADIO_RX_TIMEOUT:
            received_count = 0
        elif fault.error_code in (ErrorCode.RADIO_STREAM_FAILED, ErrorCode.RADIO_DEVICE_DISCONNECTED):
            self._commit_error_terminal_locked(("rx", pending.key), fault.error_code, "Software RX stream fault가 주입되었습니다", Correlation(), _rx_details(pending, 0))
            if fault.error_code == ErrorCode.RADIO_DEVICE_DISCONNECTED:
                self._device_state = RadioDeviceState.FAILED
            return
        else:
            prefix_count = min(pending.expected_sample_count - 1, fault.sample_index if fault.sample_index is not None else pending.expected_sample_count // 2)
            if prefix_count:
                pending.received_sample_count = prefix_count
                prefix = SampleBlock(SAMPLE_FORMAT_CF32LE, request.sample_rate_hz, prefix_count, data[: prefix_count * 8], request.start_at, SampleContinuity.CONTIGUOUS)
                if self._deliver_rx_block_locked(pending, prefix, pending.hardware_start_ns):
                    received_count = prefix_count
            delivery_failed = self._delivery_status.failed
        terminal_key = ("rx", pending.key)
        if delivery_failed:
            self._terminal_keys.add(terminal_key)
            return
        if received_count == pending.expected_sample_count:
            completion = RadioRxWindowCompleted(
                self._configure_generation, request.channel, request.start_at,
                request.duration_nanoseconds, pending.expected_sample_count,
                received_count, self._time_source.now_monotonic(),
            )
            self._commit_success_terminal_locked(terminal_key, InternalEventType.RADIO_RX_WINDOW_COMPLETED, completion, Correlation(), _rx_details(pending, received_count))
        else:
            code = ErrorCode.RADIO_RX_TIMEOUT if received_count == 0 else ErrorCode.RADIO_RX_WINDOW_INCOMPLETE
            self._commit_error_terminal_locked(terminal_key, code, "Software RX window가 불완전하게 종료되었습니다", Correlation(), _rx_details(pending, received_count))

    def _deliver_rx_block_locked(self, pending: _PendingRx, block: SampleBlock, hardware_time_ns: int) -> bool:
        name = pending.request.channel.name
        limits = self._configuration.resource_limits
        size = len(block.data)
        if self._rx_block_pending_count[name] + 1 > limits.rx_queue_max_count or self._rx_block_pending_bytes[name] + size > limits.rx_queue_max_bytes:
            counters = self._counters[name]
            counters.rx_dropped_block_count += 1
            counters.rx_dropped_sample_count += block.sample_count
            self._overflow_count += 1
            self._next_rx_gap[name] = True
            details = _rx_details(pending, 0)
            error = internal_error(Layer.RADIO, ErrorCode.RADIO_RX_OVERFLOW, ErrorCategory.RESOURCE, "RX block queue가 가득 차 새 block을 폐기했습니다", details=details).error
            self._publish_locked(InternalEventType.LAYER_ERROR, None, Correlation(), details, error=error)
            self._emit_status_changed_locked()
            return False
        if self._next_rx_gap[name] and block.continuity is SampleContinuity.CONTIGUOUS:
            block = SampleBlock(block.sample_format, block.sample_rate_hz, block.sample_count, block.data, block.first_sample_at, SampleContinuity.GAP_BEFORE)
        self._rx_block_pending_count[name] += 1
        self._rx_block_pending_bytes[name] += size
        try:
            received = RadioSamplesReceived(
                pending.request.channel, block,
                TimePoint.from_nanoseconds(
                    hardware_time_ns,
                    ClockDomain.RADIO_HARDWARE,
                    pending.mapping.target_clock_id,
                ),
                block.continuity,
            )
            delivered = self._publish_locked(
                InternalEventType.SAMPLES_RECEIVED, received, Correlation(),
                Details.of(channel=name, sample_count=block.sample_count),
                delivery_failure_terminal=True,
            )
            if delivered:
                self._next_rx_gap[name] = False
            return delivered
        finally:
            self._rx_block_pending_count[name] -= 1
            self._rx_block_pending_bytes[name] -= size

    def _validate_tx(self, request: RadioTxRequest, correlation: Correlation):
        if self._configuration is None:
            return None
        setting, binding = self._channel_parts(request.channel)
        if setting is None:
            return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다", correlation)
        if not binding.tx_enabled:
            return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "TX 비활성 channel입니다", correlation)
        samples = request.samples
        if samples.sample_format != SAMPLE_FORMAT_CF32LE or samples.sample_rate_hz != setting.sample_rate_hz:
            return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "TX sample format/rate가 설정과 다릅니다", correlation)
        if not samples.first_sample_at.same_clock(request.transmit_at) or samples.first_sample_at != request.transmit_at:
            return self._error(ErrorCode.INVALID_VALUE, ErrorCategory.VALIDATION, "first_sample_at이 transmit_at과 다릅니다", correlation)
        for i_value, q_value in struct.iter_unpack("<ff", samples.data):
            if not math.isfinite(i_value) or not math.isfinite(q_value) or float(i_value) ** 2 + float(q_value) ** 2 > 1.0:
                return self._error(ErrorCode.RADIO_TX_SAMPLE_OUT_OF_RANGE, ErrorCategory.VALIDATION, "TX cf32le sample이 finite unit circle 범위를 벗어났습니다", correlation)
        return None

    def _validate_rx(self, request: RadioRxRequest):
        if self._configuration is None:
            return None
        setting, binding = self._channel_parts(request.channel)
        if setting is None:
            return self._error(ErrorCode.RADIO_UNKNOWN_CHANNEL, ErrorCategory.VALIDATION, "설정되지 않은 channel입니다")
        if not binding.rx_enabled or request.sample_rate_hz != setting.sample_rate_hz:
            return self._error(ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "RX 방향/rate가 설정과 다릅니다")
        return None

    def _require_running(self, correlation=Correlation()):
        if self._configuration is None:
            return self._error(ErrorCode.RADIO_NOT_CONFIGURED, ErrorCategory.DEVICE, "Radio가 설정되지 않았습니다", correlation)
        if self._stopping or self._device_state is not RadioDeviceState.RUNNING:
            return self._error(ErrorCode.RADIO_NOT_STARTED, ErrorCategory.DEVICE, "Radio가 RUNNING 상태가 아닙니다", correlation)
        return None

    def _ensure_mapping_locked(self, start: TimePoint, end: TimePoint, correlation: Correlation):
        mapping_fault = self._find_fault_locked("mapping", hardware_time_ns=self._hardware_now_ns_locked())
        if mapping_fault is not None:
            self._set_mapping_unavailable_locked()
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "Software mapping unavailable fault가 주입되었습니다", correlation)
        self._refresh_mapping_locked()
        if self._utc_jump_blocked:
            self._utc_jump_blocked = False
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.DEVICE, "host UTC jump 뒤 첫 timed 요청을 거부했습니다", correlation)
        if self._timing_status is None or self._timing_status.mapping_state is not RadioMappingState.VALID:
            self._acquire_mapping_locked()
        mapping = self._timing_status.mapping
        if start.clock_domain is not ClockDomain.UTC or start.clock_id != mapping.source_clock_id:
            return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.VALIDATION, "요청 UTC clock ID가 mapping source와 다릅니다", correlation)
        horizon = self._configuration.timing.mapping_valid_for_nanoseconds
        if end.total_nanoseconds() - mapping.source_reference.total_nanoseconds() > horizon:
            self._acquire_mapping_locked()
            mapping = self._timing_status.mapping
            if end.total_nanoseconds() - mapping.source_reference.total_nanoseconds() > horizon:
                return self._error(ErrorCode.CLOCK_MAPPING_UNAVAILABLE, ErrorCategory.TIMEOUT, "요청 종료시각이 mapping horizon 밖입니다", correlation)
        converted = mapping.convert(start)
        return converted if converted.is_err else Ok(converted.value.total_nanoseconds())

    def _acquire_mapping_locked(self) -> None:
        utc = self._time_source.now_utc()
        monotonic = self._time_source.now_monotonic()
        hardware_clock_id = f"software:{self._endpoint_id}:cfg:{self._configure_generation}"
        hardware_now = monotonic.total_nanoseconds() - self._hardware_origin_monotonic_ns
        mapping = ClockMapping(
            ClockDomain.UTC, utc.clock_id, ClockDomain.RADIO_HARDWARE, hardware_clock_id,
            utc, TimePoint.from_nanoseconds(hardware_now, ClockDomain.RADIO_HARDWARE, hardware_clock_id), 0,
        )
        self._mapping_utc_reference_ns = utc.total_nanoseconds()
        self._mapping_monotonic_reference_ns = monotonic.total_nanoseconds()
        self._timing_status = RadioTimingStatus(
            RadioTimingProfile.INTERNAL_FREE_RUNNING, RadioClockState.FREE_RUNNING,
            RadioPpsState.PPS_NOT_PRESENT, _UTC_FLAGS, RadioMappingState.VALID,
            mapping, monotonic, monotonic.shifted(self._configuration.timing.mapping_valid_for_nanoseconds),
        )

    def _refresh_mapping_locked(self) -> None:
        status = self._timing_status
        if status is None or status.mapping_state is not RadioMappingState.VALID:
            return
        mono = self._time_source.now_monotonic()
        utc = self._time_source.now_utc()
        elapsed_mono = mono.total_nanoseconds() - self._mapping_monotonic_reference_ns
        elapsed_utc = utc.total_nanoseconds() - self._mapping_utc_reference_ns
        if elapsed_utc < 0 or abs(elapsed_utc - elapsed_mono) > _UTC_JUMP_NS:
            self._set_mapping_unavailable_locked()
            self._utc_jump_blocked = True
            for burst_value, pending in tuple(self._tx_pending.items()):
                self._commit_error_terminal_locked(pending.key, ErrorCode.RADIO_OPERATION_CANCELLED, "host UTC jump로 미실행 Software TX를 취소했습니다", Correlation(burst_id=pending.request.burst_id), Details.of(channel=pending.request.channel.name))
                self._medium.cancel(burst_value, source_endpoint_id=self._endpoint_id, configure_generation=self._configure_generation)
            for pending in tuple(self._rx_pending.values()):
                if self._delivery_status.failed:
                    break
                self._interrupt_rx_locked(
                    pending,
                    "host UTC jump로 미실행 Software RX window를 취소했습니다",
                    "host UTC jump로 부분 수신된 Software RX window를 불완전 종료했습니다",
                )
                self._medium.release_rx(self._endpoint_id, pending.key)
            self._tx_pending.clear()
            self._rx_pending.clear()
            self._emit_status_changed_locked()
        elif mono >= status.expires_at_monotonic:
            self._timing_status = RadioTimingStatus(
                status.profile, status.clock_state, status.pps_state, status.utc_flags,
                RadioMappingState.STALE, status.mapping, status.acquired_at_monotonic,
                status.expires_at_monotonic,
            )
            self._emit_status_changed_locked()

    def _set_mapping_unavailable_locked(self) -> None:
        self._timing_status = RadioTimingStatus(
            RadioTimingProfile.INTERNAL_FREE_RUNNING, RadioClockState.FREE_RUNNING,
            RadioPpsState.PPS_NOT_PRESENT, _UTC_FLAGS, RadioMappingState.UNAVAILABLE,
            None, None, None,
        )

    def _hardware_now_ns_locked(self) -> int:
        return max(0, self._time_source.now_monotonic().total_nanoseconds() - self._hardware_origin_monotonic_ns)

    def _channel_parts(self, channel: PhysicalChannel):
        for index, setting in enumerate(self._configuration.channels):
            if setting.channel == channel:
                return setting, self._configuration.channel_bindings[index]
        return None, None

    def _tx_capacity_error_locked(self, request: RadioTxRequest, correlation: Correlation):
        limits = self._configuration.resource_limits
        relevant = [value for value in self._tx_pending.values() if value.request.channel == request.channel]
        total_bytes = sum(len(value.request.samples.data) for value in relevant)
        shared_bytes = sum(len(value.request.samples.data) for value in self._tx_pending.values())
        size = len(request.samples.data)
        if size > limits.tx_queue_max_bytes or len(relevant) + 1 > limits.tx_queue_max_count or total_bytes + size > limits.tx_queue_max_bytes or len(self._tx_pending) + 1 > limits.tx_queue_max_count or shared_bytes + size > limits.tx_queue_max_bytes:
            self._reject_tx_locked(request.channel)
            return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "TX queue capacity가 부족합니다", correlation)
        return None

    def _rx_capacity_error_locked(self, channel: PhysicalChannel, expected_samples: int):
        limits = self._configuration.resource_limits
        relevant = [value for value in self._rx_pending.values() if value.request.channel == channel]
        if expected_samples * 8 > limits.rx_queue_max_bytes or len(relevant) + 1 > limits.rx_window_max_count:
            self._reject_rx_locked(channel)
            return self._error(ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCategory.RESOURCE, "RX window capacity가 부족합니다")
        return None

    def _reject_tx_locked(self, channel: PhysicalChannel) -> None:
        if channel.name in self._counters:
            self._counters[channel.name].tx_rejected_count += 1
            self._emit_status_changed_locked()

    def _reject_rx_locked(self, channel: PhysicalChannel) -> None:
        if channel.name in self._counters:
            self._counters[channel.name].rx_window_rejected_count += 1
            self._emit_status_changed_locked()

    def _queue_status_locked(self, name: str) -> RadioQueueStatus:
        channel = next(value.channel for value in self._configuration.channels if value.channel.name == name)
        tx = [value for value in self._tx_pending.values() if value.request.channel.name == name]
        rx = [value for value in self._rx_pending.values() if value.request.channel.name == name]
        counters = self._counters[name]
        return RadioQueueStatus(
            channel, len(tx), sum(len(value.request.samples.data) for value in tx), len(rx),
            self._rx_block_pending_count[name], self._rx_block_pending_bytes[name],
            counters.tx_rejected_count, counters.rx_window_rejected_count,
            counters.rx_dropped_block_count, counters.rx_dropped_sample_count,
        )

    def _snapshot_locked(self, channel: PhysicalChannel | None = None) -> RadioStatusSnapshot:
        names = () if self._configuration is None else self._configuration.channel_names()
        if channel is not None:
            names = (channel.name,)
        queues = tuple(self._queue_status_locked(name) for name in names)
        return RadioStatusSnapshot(
            ADAPTER_KIND, self._device_state,
            self._configuration.clock_reference if self._configuration else ClockReference.NONE,
            self._device_state in (RadioDeviceState.CONFIGURED, RadioDeviceState.RUNNING)
                and self._timing_status is not None and self._timing_status.mapping_state is RadioMappingState.VALID,
            False, self._underflow_count, self._overflow_count, self._late_command_count,
            Details.of(configure_generation=self._configure_generation), self._timing_status,
            queues, self._delivery_status,
        )

    def _commit_success_terminal_locked(self, key, event_type, payload, correlation, details) -> bool:
        if key in self._terminal_keys:
            return True
        self._terminal_keys.add(key)
        self._terminal_codes[key] = None
        self._terminal_details[key] = details
        return self._publish_locked(event_type, payload, correlation, details, terminal=True)

    def _commit_error_terminal_locked(self, key, code, message, correlation, details) -> bool:
        if key in self._terminal_keys:
            return True
        self._terminal_keys.add(key)
        self._terminal_codes[key] = code
        self._terminal_details[key] = details
        error = internal_error(Layer.RADIO, code, _category_for(code), message, correlation=correlation, details=details).error
        return self._publish_locked(InternalEventType.LAYER_ERROR, None, correlation, details, error=error, terminal=True)

    def _emit_status_changed_locked(self) -> bool:
        if self._delivery_status.failed:
            return False
        return self._publish_locked(
            InternalEventType.RADIO_STATUS_CHANGED, self._snapshot_locked(),
            Correlation(), Details.of(configure_generation=self._configure_generation),
        )

    def _publish_locked(self, event_type, payload, correlation, details, *, error=None, terminal=False, delivery_failure_terminal=False) -> bool:
        if self._delivery_status.failed:
            if terminal:
                self._record_undelivered_terminals_locked(1)
            return False
        event = InternalEvent(
            self._id_source.new_event_id(), event_type, self._time_source.now_monotonic(),
            EventSeverity.ERROR if error is not None else EventSeverity.INFO,
            Layer.RADIO, correlation, payload, error, details,
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
        self._device_state = RadioDeviceState.FAILED
        self._terminalize_delivery_failure_pending_locked()
        return False

    def _record_undelivered_terminals_locked(self, count: int) -> None:
        if count <= 0:
            return
        now = self._time_source.now_monotonic()
        self._delivery_status = RadioEventDeliveryStatus(
            True,
            self._delivery_status.undelivered_terminal_count + count,
            self._delivery_status.first_failure_at or now,
            now,
        )

    def _terminalize_delivery_failure_pending_locked(self) -> None:
        """Commit every remaining operation without attempting a dead sink again."""

        hardware_now = self._hardware_now_ns_locked()
        for burst_value, pending in tuple(self._tx_pending.items()):
            self._commit_error_terminal_locked(
                pending.key,
                ErrorCode.RADIO_OPERATION_CANCELLED,
                "event delivery failure cancelled an unexecuted Software TX",
                Correlation(burst_id=pending.request.burst_id),
                Details.of(
                    channel=pending.request.channel.name,
                    hardware_time_ns=hardware_now,
                ),
            )
            self._medium.cancel(
                burst_value,
                source_endpoint_id=self._endpoint_id,
                configure_generation=self._configure_generation,
            )
        for pending in tuple(self._rx_pending.values()):
            elapsed_ns = max(
                0,
                min(
                    pending.request.duration_nanoseconds,
                    hardware_now - pending.hardware_start_ns,
                ),
            )
            observed = _round_half_up_samples(
                elapsed_ns, pending.request.sample_rate_hz
            )
            pending.received_sample_count = max(
                pending.received_sample_count,
                min(pending.expected_sample_count, observed),
            )
            self._commit_error_terminal_locked(
                ("rx", pending.key),
                ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
                "event delivery failure made the Software RX window incomplete",
                Correlation(),
                _rx_details(pending, pending.received_sample_count),
            )
            self._medium.release_rx(self._endpoint_id, pending.key)
        self._tx_pending.clear()
        self._rx_pending.clear()

    def _find_fault_locked(self, operation, channel=None, burst_id=None, rx_key=None, hardware_time_ns=None):
        for index, fault in enumerate(self._faults):
            if index in self._consumed_faults or fault.operation != operation:
                continue
            if fault.adapter_id is not None and fault.adapter_id != self._endpoint_id:
                continue
            if fault.channel is not None and fault.channel != channel:
                continue
            if fault.burst_id is not None and fault.burst_id != burst_id:
                continue
            if fault.rx_window_key is not None and fault.rx_window_key != rx_key:
                continue
            if fault.hardware_time_ns is not None and (hardware_time_ns is None or hardware_time_ns < fault.hardware_time_ns):
                continue
            if not fault.persistent:
                self._consumed_faults.add(index)
            return fault
        return None

    def _state_failed_with_pending_locked(self, code, message):
        for burst_value, pending in tuple(self._tx_pending.items()):
            self._commit_error_terminal_locked(pending.key, code, message, Correlation(burst_id=pending.request.burst_id), Details.of(channel=pending.request.channel.name))
            self._medium.cancel(burst_value, source_endpoint_id=self._endpoint_id, configure_generation=self._configure_generation)
        for pending in tuple(self._rx_pending.values()):
            self._commit_error_terminal_locked(("rx", pending.key), code, message, Correlation(), _rx_details(pending, 0))
            self._medium.release_rx(self._endpoint_id, pending.key)
        self._tx_pending.clear()
        self._rx_pending.clear()
        self._device_state = RadioDeviceState.FAILED
        self._emit_status_changed_locked()

    def _delivery_error(self):
        return self._error(ErrorCode.RADIO_EVENT_DELIVERY_FAILED, ErrorCategory.INTERNAL, "Runtime event sink가 Radio event를 전달하지 못했습니다")

    def _error(self, code, category, message, correlation=Correlation(), details=Details()):
        return internal_error(Layer.RADIO, code, category, message, correlation=correlation, details=details)


def _rx_details(pending: _PendingRx, received_sample_count: int | None = None) -> Details:
    request = pending.request
    values = dict(
        configure_generation=pending.key[0], channel=request.channel.name,
        requested_start_seconds=request.start_at.seconds,
        requested_start_nanoseconds=request.start_at.nanoseconds,
        requested_clock_id=request.start_at.clock_id,
        requested_duration_nanoseconds=request.duration_nanoseconds,
        expected_sample_count=pending.expected_sample_count,
    )
    if received_sample_count is not None:
        values["received_sample_count"] = received_sample_count
    return Details.of(**values)


def _duration_ceil_ns(sample_count: int, sample_rate_hz: int) -> int:
    return (sample_count * 1_000_000_000 + sample_rate_hz - 1) // sample_rate_hz


def _round_half_up_samples(duration_ns: int, sample_rate_hz: int) -> int:
    return (duration_ns * sample_rate_hz + 500_000_000) // 1_000_000_000


def _overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def _category_for(code: str) -> ErrorCategory:
    if code in (ErrorCode.RADIO_OPERATION_CANCELLED, ErrorCode.RADIO_RESOURCE_EXHAUSTED, ErrorCode.RADIO_RESOURCE_CONFLICT, ErrorCode.RADIO_RX_OVERFLOW):
        return ErrorCategory.RESOURCE
    if code in (ErrorCode.RADIO_RX_TIMEOUT, ErrorCode.RADIO_STOP_TIMEOUT, ErrorCode.RADIO_LATE_REQUEST):
        return ErrorCategory.TIMEOUT
    if code == ErrorCode.RADIO_EVENT_DELIVERY_FAILED:
        return ErrorCategory.INTERNAL
    return ErrorCategory.DEVICE


def _validate_software_configuration(configuration: RadioConfiguration):
    if configuration.adapter_kind != ADAPTER_KIND:
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "SoftwareRadioAdapter 설정이 아닙니다")
    if configuration.sample_format != SAMPLE_FORMAT_CF32LE or configuration.device_selector or configuration.b210_stream is not None:
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "Software는 cf32le과 빈 device selector를 사용해야 합니다")
    if configuration.timing is None or configuration.resource_limits is None or not configuration.channels or len(configuration.channels) != len(configuration.channel_bindings):
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "Software Stage 3 timing/resource/channel binding이 필요합니다")
    timing = configuration.timing
    if (
        timing.profile is not RadioTimingProfile.INTERNAL_FREE_RUNNING
        or configuration.clock_reference is not ClockReference.INTERNAL
        or timing.clock_source is not ClockReference.INTERNAL
        or timing.time_source is not ClockReference.INTERNAL
        or timing.pps_source is not ClockReference.NONE
        or timing.mapping_attempt_count != 8
        or timing.mapping_valid_for_nanoseconds != 1_000_000_000
        or timing.max_mapping_uncertainty_ns != 10_000_000
    ):
        return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "승인된 INTERNAL_FREE_RUNNING 설정이 아닙니다")
    names = tuple(setting.channel.name for setting in configuration.channels)
    frequencies = tuple(
        setting.center_frequency_hz for setting in configuration.channels
    )
    directions = tuple(
        (binding.tx_enabled, binding.rx_enabled)
        for binding in configuration.channel_bindings
    )
    # `DR-STAGE6-MR-001`: 승인 profile은 legacy 25 kHz 2-PC 또는 wideband 100 kHz 2-PC다.
    # 두 profile 사이 silent fallback은 없고 alias 이름이 profile을 명시적으로 선택한다.
    wideband = names == ("VDETER_LOWER_100_TEST", "VDETER_UPPER_100_TEST")
    if wideband:
        if frequencies != (157_237_500, 161_837_500) or directions not in (
            ((True, False), (False, True)),
            ((False, True), (True, False)),
        ):
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "Software wideband channel 순서·주파수·역할 방향이 승인 profile과 다릅니다",
            )
    elif (
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
            "Software channel 순서·주파수·역할 방향이 승인 profile과 다릅니다",
        )
    expected_rate = 307_200 if wideband else 76_800
    expected_bandwidth = 100_000 if wideband else 25_000
    for setting, binding in zip(configuration.channels, configuration.channel_bindings, strict=True):
        if (
            setting.channel != binding.channel
            or setting.sample_rate_hz != expected_rate
            or not 0 <= setting.tx_gain_db <= 89
            or not 0 <= setting.rx_gain_db <= 76
            or binding.waveform_bandwidth_hz != expected_bandwidth
            or binding.analog_bandwidth_hz != expected_bandwidth
        ):
            return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "Software channel setting이 승인값과 다릅니다")
        if any(value is not None for value in (binding.tx_device_channel, binding.rx_device_channel, binding.tx_antenna, binding.rx_antenna)):
            return internal_error(Layer.RADIO, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION, "Software binding에는 hardware field를 둘 수 없습니다")
    return None
