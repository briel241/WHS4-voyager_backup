"""PHY ↔ Radio 경계 DTO (`stage0_baseline.md` §5.3, §5.3.1, §9.2).

Radio에는 Link Packet이나 Message가 아니라 baseband samples와 장비 실행 metadata만
전달한다. 이 module의 어떤 DTO도 Packet, Message, Link ID 정책, CRC, FEC 또는 payload를
포함하지 않으며 GNU Radio/UHD/PMT/NumPy 객체를 담지 않는다.

운용 sample encoding·dtype, buffer 크기·소유권, backpressure와 최소 scheduling lead time은
단계 3 Radio 기준선에서 확정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from ..errors import Details, EMPTY_DETAILS
from ..exceptions import InvalidValueError
from ..identifiers import BurstId, PhysicalChannel
from ..time import ClockDomain, ClockMapping, TimePoint

#: 단계 0 Fake adapter 전용 sample format. I, Q 순서의 little-endian signed 16-bit pair다.
SAMPLE_FORMAT_TEST_IQ_INT16_LE = "test-iq-int16-le"
SAMPLE_FORMAT_CF32LE = "cf32le"

#: `sample_format` → complex sample 하나가 차지하는 byte 수.
#: 단계 0에는 test scope 값만 등록한다. 운용 format은 단계 3에서 등록한다.
#: runtime에 변경할 수 없는 읽기 전용 표다.
SAMPLE_FORMAT_BYTES_PER_SAMPLE = MappingProxyType(
    {
        SAMPLE_FORMAT_TEST_IQ_INT16_LE: 4,
        SAMPLE_FORMAT_CF32LE: 8,
    }
)

#: 운영 설정에서 거부해야 하는 test scope sample format.
TEST_SCOPE_SAMPLE_FORMATS = frozenset({SAMPLE_FORMAT_TEST_IQ_INT16_LE})


class SampleContinuity(str, Enum):
    CONTIGUOUS = "CONTIGUOUS"
    GAP_BEFORE = "GAP_BEFORE"
    UNKNOWN = "UNKNOWN"


class RadioDeviceState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONFIGURED = "CONFIGURED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class ClockReference(str, Enum):
    """clock/PPS 기준원. 실제 허용 오차와 획득 절차는 단계 3에서 확정한다."""

    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"
    GPSDO = "GPSDO"
    NONE = "NONE"


class RadioTimingProfile(str, Enum):
    INTERNAL_FREE_RUNNING = "INTERNAL_FREE_RUNNING"


class RadioMappingState(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class RadioClockState(str, Enum):
    FREE_RUNNING = "FREE_RUNNING"
    NOT_RUNNING = "NOT_RUNNING"


class RadioPpsState(str, Enum):
    PPS_NOT_PRESENT = "PPS_NOT_PRESENT"


class RadioUtcFlag(str, Enum):
    HOST_ESTIMATE = "HOST_ESTIMATE"
    NOT_UTC_DISCIPLINED = "NOT_UTC_DISCIPLINED"
    UTC_ACCURACY_UNVERIFIED = "UTC_ACCURACY_UNVERIFIED"


def _require_int(name: str, value: object, minimum: int, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValueError(f"{name}: 정수여야 합니다")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"{minimum}..{maximum}" if maximum is not None else f"{minimum} 이상"
        raise InvalidValueError(f"{name}: {suffix}이어야 합니다")


def _require_optional_positive_int(name: str, value: object) -> None:
    if value is not None:
        _require_int(name, value, 1)


def _require_optional_nonblank_ascii(name: str, value: object) -> None:
    if value is not None and (
        not isinstance(value, str) or not value or not value.isascii() or not value.isprintable()
    ):
        raise InvalidValueError(f"{name}: 비어 있지 않은 출력 가능 ASCII 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioTimingConfiguration:
    profile: RadioTimingProfile
    clock_source: ClockReference
    time_source: ClockReference
    pps_source: ClockReference
    mapping_attempt_count: int
    mapping_valid_for_nanoseconds: int
    max_mapping_uncertainty_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile, RadioTimingProfile):
            raise InvalidValueError("RadioTimingConfiguration.profile이 잘못되었습니다")
        for name in ("clock_source", "time_source", "pps_source"):
            if not isinstance(getattr(self, name), ClockReference):
                raise InvalidValueError(f"RadioTimingConfiguration.{name}이 잘못되었습니다")
        _require_int("RadioTimingConfiguration.mapping_attempt_count", self.mapping_attempt_count, 1, 64)
        _require_int(
            "RadioTimingConfiguration.mapping_valid_for_nanoseconds",
            self.mapping_valid_for_nanoseconds,
            1_000_000,
            60_000_000_000,
        )
        _require_int(
            "RadioTimingConfiguration.max_mapping_uncertainty_ns",
            self.max_mapping_uncertainty_ns,
            0,
            self.mapping_valid_for_nanoseconds,
        )


@dataclass(frozen=True, slots=True)
class RadioResourceLimits:
    tx_queue_max_count: int
    tx_queue_max_bytes: int
    rx_window_max_count: int
    rx_queue_max_count: int
    rx_queue_max_bytes: int
    stop_timeout_ns: int

    def __post_init__(self) -> None:
        for name in ("tx_queue_max_count", "rx_window_max_count", "rx_queue_max_count"):
            _require_int(f"RadioResourceLimits.{name}", getattr(self, name), 1, 4096)
        for name in ("tx_queue_max_bytes", "rx_queue_max_bytes"):
            _require_int(f"RadioResourceLimits.{name}", getattr(self, name), 1, 1_073_741_824)
        _require_int(
            "RadioResourceLimits.stop_timeout_ns", self.stop_timeout_ns, 1_000_000, 60_000_000_000
        )


@dataclass(frozen=True, slots=True)
class RadioChannelBinding:
    channel: PhysicalChannel
    waveform_bandwidth_hz: int
    analog_bandwidth_hz: int
    tx_enabled: bool
    rx_enabled: bool
    tx_device_channel: int | None = None
    rx_device_channel: int | None = None
    tx_antenna: str | None = None
    rx_antenna: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioChannelBinding.channel이 잘못되었습니다")
        _require_int("RadioChannelBinding.waveform_bandwidth_hz", self.waveform_bandwidth_hz, 1)
        _require_int("RadioChannelBinding.analog_bandwidth_hz", self.analog_bandwidth_hz, 1)
        for name in ("tx_enabled", "rx_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise InvalidValueError(f"RadioChannelBinding.{name}: bool이어야 합니다")
        if not self.tx_enabled and not self.rx_enabled:
            raise InvalidValueError("RadioChannelBinding: TX 또는 RX 방향 하나 이상이 활성화되어야 합니다")
        for name in ("tx_device_channel", "rx_device_channel"):
            value = getattr(self, name)
            if value is not None:
                _require_int(f"RadioChannelBinding.{name}", value, 0, 255)
        for name in ("tx_antenna", "rx_antenna"):
            _require_optional_nonblank_ascii(f"RadioChannelBinding.{name}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class RadioB210StreamConfiguration:
    master_clock_rate_hz: int
    cpu_format: str
    otw_format: str

    def __post_init__(self) -> None:
        if self.master_clock_rate_hz != 30_720_000:
            raise InvalidValueError("RadioB210StreamConfiguration.master_clock_rate_hz는 30720000이어야 합니다")
        if self.cpu_format != "fc32":
            raise InvalidValueError("RadioB210StreamConfiguration.cpu_format은 fc32여야 합니다")
        if self.otw_format != "sc16":
            raise InvalidValueError("RadioB210StreamConfiguration.otw_format은 sc16이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioCapabilities:
    sample_formats: tuple[str, ...]
    minimum_schedule_lead_ns: int
    minimum_rx_schedule_lead_ns: int
    minimum_tx_channel_switch_gap_ns: int
    full_duplex: bool
    timing_profiles: tuple[RadioTimingProfile, ...]
    resource_limits: RadioResourceLimits

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sample_formats, tuple)
            or not self.sample_formats
            or any(not isinstance(value, str) or not value for value in self.sample_formats)
            or len(set(self.sample_formats)) != len(self.sample_formats)
        ):
            raise InvalidValueError("RadioCapabilities.sample_formats는 nonempty unique 문자열 tuple이어야 합니다")
        for name in (
            "minimum_schedule_lead_ns",
            "minimum_rx_schedule_lead_ns",
            "minimum_tx_channel_switch_gap_ns",
        ):
            _require_int(f"RadioCapabilities.{name}", getattr(self, name), 0, 60_000_000_000)
        if not isinstance(self.full_duplex, bool):
            raise InvalidValueError("RadioCapabilities.full_duplex: bool이어야 합니다")
        if (
            not isinstance(self.timing_profiles, tuple)
            or not self.timing_profiles
            or any(not isinstance(value, RadioTimingProfile) for value in self.timing_profiles)
            or len(set(self.timing_profiles)) != len(self.timing_profiles)
        ):
            raise InvalidValueError("RadioCapabilities.timing_profiles가 잘못되었습니다")
        if not isinstance(self.resource_limits, RadioResourceLimits):
            raise InvalidValueError("RadioCapabilities.resource_limits가 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class RadioAppliedChannelSetting:
    channel: PhysicalChannel
    requested_center_frequency_hz: int
    applied_center_frequency_hz: int
    requested_sample_rate_hz: int
    applied_tx_sample_rate_hz: int | None
    applied_rx_sample_rate_hz: int | None
    requested_tx_gain_db: int
    requested_rx_gain_db: int
    applied_tx_gain_db: int | None
    applied_rx_gain_db: int | None
    requested_analog_bandwidth_hz: int
    applied_tx_analog_bandwidth_hz: int | None
    applied_rx_analog_bandwidth_hz: int | None
    binding: RadioChannelBinding

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel) or not isinstance(self.binding, RadioChannelBinding):
            raise InvalidValueError("RadioAppliedChannelSetting channel/binding이 잘못되었습니다")
        if self.binding.channel != self.channel:
            raise InvalidValueError("RadioAppliedChannelSetting.binding.channel이 channel과 다릅니다")
        for name in ("requested_center_frequency_hz", "applied_center_frequency_hz", "requested_sample_rate_hz", "requested_analog_bandwidth_hz"):
            _require_int(f"RadioAppliedChannelSetting.{name}", getattr(self, name), 1)
        _require_int("RadioAppliedChannelSetting.requested_tx_gain_db", self.requested_tx_gain_db, 0, 89)
        _require_int("RadioAppliedChannelSetting.requested_rx_gain_db", self.requested_rx_gain_db, 0, 76)
        for name in ("applied_tx_sample_rate_hz", "applied_rx_sample_rate_hz", "applied_tx_analog_bandwidth_hz", "applied_rx_analog_bandwidth_hz"):
            _require_optional_positive_int(f"RadioAppliedChannelSetting.{name}", getattr(self, name))
        if self.applied_tx_gain_db is not None:
            _require_int("RadioAppliedChannelSetting.applied_tx_gain_db", self.applied_tx_gain_db, 0, 89)
        if self.applied_rx_gain_db is not None:
            _require_int("RadioAppliedChannelSetting.applied_rx_gain_db", self.applied_rx_gain_db, 0, 76)
        for enabled, prefix in ((self.binding.tx_enabled, "tx"), (self.binding.rx_enabled, "rx")):
            values = (
                getattr(self, f"applied_{prefix}_sample_rate_hz"),
                getattr(self, f"applied_{prefix}_gain_db"),
                getattr(self, f"applied_{prefix}_analog_bandwidth_hz"),
            )
            if (enabled and not all(value is not None for value in values)) or (
                not enabled and any(value is not None for value in values)
            ):
                raise InvalidValueError(f"RadioAppliedChannelSetting: {prefix} 활성 상태와 applied 값이 일치하지 않습니다")


@dataclass(frozen=True, slots=True)
class RadioAppliedSettings:
    device_id: str
    configure_generation: int
    channels: tuple[RadioAppliedChannelSetting, ...]
    requested_master_clock_rate_hz: int | None
    applied_master_clock_rate_hz: int | None
    cpu_format: str | None
    otw_format: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id:
            raise InvalidValueError("RadioAppliedSettings.device_id가 비어 있습니다")
        _require_int("RadioAppliedSettings.configure_generation", self.configure_generation, 1)
        if not isinstance(self.channels, tuple) or not self.channels or any(not isinstance(value, RadioAppliedChannelSetting) for value in self.channels):
            raise InvalidValueError("RadioAppliedSettings.channels가 잘못되었습니다")
        if len({value.channel.name for value in self.channels}) != len(self.channels):
            raise InvalidValueError("RadioAppliedSettings.channels에 중복 channel이 있습니다")
        for name in ("requested_master_clock_rate_hz", "applied_master_clock_rate_hz"):
            _require_optional_positive_int(f"RadioAppliedSettings.{name}", getattr(self, name))
        for name in ("cpu_format", "otw_format"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidValueError(f"RadioAppliedSettings.{name}가 비어 있습니다")


@dataclass(frozen=True, slots=True)
class RadioTimingStatus:
    profile: RadioTimingProfile
    clock_state: RadioClockState
    pps_state: RadioPpsState
    utc_flags: tuple[RadioUtcFlag, ...]
    mapping_state: RadioMappingState
    mapping: ClockMapping | None
    acquired_at_monotonic: TimePoint | None
    expires_at_monotonic: TimePoint | None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, RadioTimingProfile) or not isinstance(self.clock_state, RadioClockState) or not isinstance(self.pps_state, RadioPpsState) or not isinstance(self.mapping_state, RadioMappingState):
            raise InvalidValueError("RadioTimingStatus enum 값이 잘못되었습니다")
        if not isinstance(self.utc_flags, tuple) or any(not isinstance(value, RadioUtcFlag) for value in self.utc_flags) or len(set(self.utc_flags)) != len(self.utc_flags):
            raise InvalidValueError("RadioTimingStatus.utc_flags가 잘못되었습니다")
        values = (self.mapping, self.acquired_at_monotonic, self.expires_at_monotonic)
        if self.mapping_state is RadioMappingState.UNAVAILABLE:
            if any(value is not None for value in values):
                raise InvalidValueError("UNAVAILABLE timing status는 mapping 시각을 가질 수 없습니다")
            return
        if not isinstance(self.mapping, ClockMapping) or not isinstance(self.acquired_at_monotonic, TimePoint) or not isinstance(self.expires_at_monotonic, TimePoint):
            raise InvalidValueError("VALID/STALE timing status에는 mapping과 획득/만료 시각이 필요합니다")
        for name, point in (("acquired", self.acquired_at_monotonic), ("expires", self.expires_at_monotonic)):
            if point.clock_domain is not ClockDomain.MONOTONIC:
                raise InvalidValueError(f"RadioTimingStatus.{name} 시각은 MONOTONIC이어야 합니다")
        if not self.acquired_at_monotonic.same_clock(self.expires_at_monotonic) or self.acquired_at_monotonic >= self.expires_at_monotonic:
            raise InvalidValueError("RadioTimingStatus 획득 시각은 같은 clock의 만료 시각보다 앞서야 합니다")


@dataclass(frozen=True, slots=True)
class RadioQueueStatus:
    channel: PhysicalChannel
    tx_outstanding_count: int
    tx_outstanding_bytes: int
    rx_window_outstanding_count: int
    rx_block_pending_count: int
    rx_block_pending_bytes: int
    tx_rejected_count: int
    rx_window_rejected_count: int
    rx_dropped_block_count: int
    rx_dropped_sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioQueueStatus.channel이 잘못되었습니다")
        for name in self.__slots__[1:]:
            _require_int(f"RadioQueueStatus.{name}", getattr(self, name), 0)


@dataclass(frozen=True, slots=True)
class RadioEventDeliveryStatus:
    failed: bool
    undelivered_terminal_count: int
    first_failure_at: TimePoint | None
    last_failure_at: TimePoint | None

    def __post_init__(self) -> None:
        if not isinstance(self.failed, bool):
            raise InvalidValueError("RadioEventDeliveryStatus.failed: bool이어야 합니다")
        _require_int("RadioEventDeliveryStatus.undelivered_terminal_count", self.undelivered_terminal_count, 0)
        if not self.failed:
            if self.undelivered_terminal_count != 0 or self.first_failure_at is not None or self.last_failure_at is not None:
                raise InvalidValueError("정상 event delivery status에는 실패 count/시각이 없어야 합니다")
            return
        if not isinstance(self.first_failure_at, TimePoint) or not isinstance(self.last_failure_at, TimePoint):
            raise InvalidValueError("실패 event delivery status에는 두 시각이 필요합니다")
        if self.first_failure_at.clock_domain is not ClockDomain.MONOTONIC or not self.first_failure_at.same_clock(self.last_failure_at) or self.first_failure_at > self.last_failure_at:
            raise InvalidValueError("event delivery 실패 시각이 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class RadioRxWindowCompleted:
    configure_generation: int
    channel: PhysicalChannel
    requested_start_at: TimePoint
    requested_duration_nanoseconds: int
    expected_sample_count: int
    received_sample_count: int
    completed_at: TimePoint

    def __post_init__(self) -> None:
        _require_int("RadioRxWindowCompleted.configure_generation", self.configure_generation, 1)
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioRxWindowCompleted.channel이 잘못되었습니다")
        if not isinstance(self.requested_start_at, TimePoint) or self.requested_start_at.clock_domain is not ClockDomain.UTC:
            raise InvalidValueError("RadioRxWindowCompleted.requested_start_at은 UTC여야 합니다")
        _require_int("RadioRxWindowCompleted.requested_duration_nanoseconds", self.requested_duration_nanoseconds, 1)
        _require_int("RadioRxWindowCompleted.expected_sample_count", self.expected_sample_count, 1)
        _require_int("RadioRxWindowCompleted.received_sample_count", self.received_sample_count, 1)
        if self.expected_sample_count != self.received_sample_count:
            raise InvalidValueError("RadioRxWindowCompleted sample count가 일치하지 않습니다")
        if not isinstance(self.completed_at, TimePoint) or self.completed_at.clock_domain is not ClockDomain.MONOTONIC:
            raise InvalidValueError("RadioRxWindowCompleted.completed_at은 MONOTONIC이어야 합니다")


@dataclass(frozen=True, slots=True)
class SampleBlock:
    """PHY ↔ Radio 경계의 complex baseband sample block.

    `data`는 reference serialization과 Fake adapter가 사용하는 portable `bytes`다.
    native buffer, NumPy view, GNU Radio/UHD/PMT 객체는 담지 않는다.
    """

    sample_format: str
    sample_rate_hz: int
    sample_count: int
    data: bytes
    first_sample_at: TimePoint
    continuity: SampleContinuity = SampleContinuity.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.sample_format, str) or not self.sample_format:
            raise InvalidValueError("SampleBlock.sample_format: 비어 있지 않은 문자열이어야 합니다")
        bytes_per_sample = SAMPLE_FORMAT_BYTES_PER_SAMPLE.get(self.sample_format)
        if bytes_per_sample is None:
            raise InvalidValueError(
                f"SampleBlock.sample_format: 등록되지 않은 format입니다 ({self.sample_format!r})"
            )
        if isinstance(self.sample_rate_hz, bool) or not isinstance(self.sample_rate_hz, int):
            raise InvalidValueError("SampleBlock.sample_rate_hz: 정수여야 합니다")
        if self.sample_rate_hz <= 0:
            raise InvalidValueError("SampleBlock.sample_rate_hz: 양의 정수여야 합니다")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
            raise InvalidValueError("SampleBlock.sample_count: 정수여야 합니다")
        if self.sample_count < 0:
            raise InvalidValueError("SampleBlock.sample_count: 0 이상이어야 합니다")
        if not isinstance(self.data, bytes):
            raise InvalidValueError("SampleBlock.data: bytes여야 합니다")
        expected = self.sample_count * bytes_per_sample
        if len(self.data) != expected:
            raise InvalidValueError(
                f"SampleBlock.data: {self.sample_format} format은 sample당 {bytes_per_sample} "
                f"bytes이므로 {expected} bytes여야 합니다 ({len(self.data)})"
            )
        if not isinstance(self.first_sample_at, TimePoint):
            raise InvalidValueError("SampleBlock.first_sample_at: TimePoint여야 합니다")
        if not isinstance(self.continuity, SampleContinuity):
            raise InvalidValueError("SampleBlock.continuity: SampleContinuity가 아닙니다")

    @property
    def is_test_scope(self) -> bool:
        return self.sample_format in TEST_SCOPE_SAMPLE_FORMATS


@dataclass(frozen=True, slots=True)
class RadioChannelSetting:
    """Physical Channel 하나의 장비 설정. 실제 값 범위는 단계 3에서 확정한다."""

    channel: PhysicalChannel
    center_frequency_hz: int
    sample_rate_hz: int
    tx_gain_db: int
    rx_gain_db: int

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioChannelSetting.channel: PhysicalChannel이 아닙니다")
        for name in ("center_frequency_hz", "sample_rate_hz", "tx_gain_db", "rx_gain_db"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidValueError(f"RadioChannelSetting.{name}: 정수여야 합니다")
        if self.center_frequency_hz <= 0 or self.sample_rate_hz <= 0:
            raise InvalidValueError(
                "RadioChannelSetting: center_frequency_hz와 sample_rate_hz는 양수여야 합니다"
            )


@dataclass(frozen=True, slots=True)
class RadioConfiguration:
    """frozen Radio 설정 snapshot. Endpoint Config와 분리해 관리한다."""

    adapter_kind: str
    device_selector: str
    clock_reference: ClockReference
    channels: tuple[RadioChannelSetting, ...] = ()
    sample_format: str = SAMPLE_FORMAT_TEST_IQ_INT16_LE
    timing: RadioTimingConfiguration | None = None
    resource_limits: RadioResourceLimits | None = None
    channel_bindings: tuple[RadioChannelBinding, ...] = ()
    b210_stream: RadioB210StreamConfiguration | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, str) or not self.adapter_kind:
            raise InvalidValueError("RadioConfiguration.adapter_kind: 비어 있으면 안 됩니다")
        if not isinstance(self.device_selector, str):
            raise InvalidValueError("RadioConfiguration.device_selector: 문자열이어야 합니다")
        if not isinstance(self.clock_reference, ClockReference):
            raise InvalidValueError("RadioConfiguration.clock_reference: ClockReference가 아닙니다")
        if not isinstance(self.channels, tuple):
            raise InvalidValueError("RadioConfiguration.channels: tuple이어야 합니다")
        names = []
        for setting in self.channels:
            if not isinstance(setting, RadioChannelSetting):
                raise InvalidValueError(
                    "RadioConfiguration.channels: RadioChannelSetting만 담을 수 있습니다"
                )
            names.append(setting.channel.name)
        if len(set(names)) != len(names):
            raise InvalidValueError("RadioConfiguration.channels: Physical Channel이 중복되었습니다")
        if self.sample_format not in SAMPLE_FORMAT_BYTES_PER_SAMPLE:
            raise InvalidValueError(
                f"RadioConfiguration.sample_format: 등록되지 않은 format입니다 ({self.sample_format!r})"
            )
        if self.timing is not None and not isinstance(self.timing, RadioTimingConfiguration):
            raise InvalidValueError("RadioConfiguration.timing이 잘못되었습니다")
        if self.resource_limits is not None and not isinstance(self.resource_limits, RadioResourceLimits):
            raise InvalidValueError("RadioConfiguration.resource_limits가 잘못되었습니다")
        if not isinstance(self.channel_bindings, tuple) or any(
            not isinstance(binding, RadioChannelBinding) for binding in self.channel_bindings
        ):
            raise InvalidValueError("RadioConfiguration.channel_bindings가 잘못되었습니다")
        binding_names = tuple(binding.channel.name for binding in self.channel_bindings)
        if len(set(binding_names)) != len(binding_names):
            raise InvalidValueError("RadioConfiguration.channel_bindings에 중복 channel이 있습니다")
        if self.b210_stream is not None and not isinstance(self.b210_stream, RadioB210StreamConfiguration):
            raise InvalidValueError("RadioConfiguration.b210_stream이 잘못되었습니다")

    def channel_names(self) -> tuple[str, ...]:
        return tuple(setting.channel.name for setting in self.channels)


@dataclass(frozen=True, slots=True)
class RadioConfigureRequest:
    configuration: RadioConfiguration

    def __post_init__(self) -> None:
        if not isinstance(self.configuration, RadioConfiguration):
            raise InvalidValueError("RadioConfigureRequest.configuration 형식이 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class RadioConfigurationApplied:
    adapter_kind: str
    device_state: RadioDeviceState
    channels: tuple[str, ...] = ()
    sample_format: str = SAMPLE_FORMAT_TEST_IQ_INT16_LE
    capabilities: RadioCapabilities | None = None
    applied_settings: RadioAppliedSettings | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, str) or not self.adapter_kind:
            raise InvalidValueError("RadioConfigurationApplied.adapter_kind가 비어 있습니다")
        if not isinstance(self.device_state, RadioDeviceState):
            raise InvalidValueError("RadioConfigurationApplied.device_state가 잘못되었습니다")
        if not isinstance(self.channels, tuple) or any(
            not isinstance(name, str) or not name for name in self.channels
        ):
            raise InvalidValueError("RadioConfigurationApplied.channels: 문자열 tuple이어야 합니다")
        if not isinstance(self.sample_format, str) or not self.sample_format:
            raise InvalidValueError("RadioConfigurationApplied.sample_format이 비어 있습니다")
        if self.capabilities is not None and not isinstance(self.capabilities, RadioCapabilities):
            raise InvalidValueError("RadioConfigurationApplied.capabilities가 잘못되었습니다")
        if self.applied_settings is not None:
            if not isinstance(self.applied_settings, RadioAppliedSettings):
                raise InvalidValueError("RadioConfigurationApplied.applied_settings가 잘못되었습니다")
            projected = tuple(value.channel.name for value in self.applied_settings.channels)
            if self.channels != projected:
                raise InvalidValueError("RadioConfigurationApplied.channels projection이 일치하지 않습니다")


@dataclass(frozen=True, slots=True)
class RadioStartRequest:
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise InvalidValueError("RadioStartRequest.reason: 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioStarted:
    adapter_kind: str
    device_state: RadioDeviceState
    started_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, str) or not self.adapter_kind:
            raise InvalidValueError("RadioStarted.adapter_kind가 비어 있습니다")
        if not isinstance(self.device_state, RadioDeviceState):
            raise InvalidValueError("RadioStarted.device_state가 잘못되었습니다")
        if not isinstance(self.started_at, TimePoint):
            raise InvalidValueError("RadioStarted.started_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioStopRequest:
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise InvalidValueError("RadioStopRequest.reason: 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioStopped:
    adapter_kind: str
    device_state: RadioDeviceState
    stopped_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, str) or not self.adapter_kind:
            raise InvalidValueError("RadioStopped.adapter_kind가 비어 있습니다")
        if not isinstance(self.device_state, RadioDeviceState):
            raise InvalidValueError("RadioStopped.device_state가 잘못되었습니다")
        if not isinstance(self.stopped_at, TimePoint):
            raise InvalidValueError("RadioStopped.stopped_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioTxRequest:
    """timed TX 예약 요청. 송신시각은 MAC이 정한 UTC 계획이다."""

    burst_id: BurstId
    channel: PhysicalChannel
    transmit_at: TimePoint
    samples: SampleBlock

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("RadioTxRequest.burst_id: BurstId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioTxRequest.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.transmit_at, TimePoint):
            raise InvalidValueError("RadioTxRequest.transmit_at: TimePoint여야 합니다")
        if self.transmit_at.clock_domain is not ClockDomain.UTC:
            raise InvalidValueError("RadioTxRequest.transmit_at: UTC 계획이어야 합니다")
        if not isinstance(self.samples, SampleBlock):
            raise InvalidValueError("RadioTxRequest.samples: SampleBlock이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioTxAccepted:
    """예약 접수. 실제 송신 완료가 아니다."""

    burst_id: BurstId
    channel: PhysicalChannel
    scheduled_at: TimePoint
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("RadioTxAccepted.burst_id: BurstId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioTxAccepted.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.scheduled_at, TimePoint):
            raise InvalidValueError("RadioTxAccepted.scheduled_at: TimePoint여야 합니다")
        if not isinstance(self.accepted_at, TimePoint):
            raise InvalidValueError("RadioTxAccepted.accepted_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioTxCompleted:
    burst_id: BurstId
    channel: PhysicalChannel
    completed_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("RadioTxCompleted.burst_id: BurstId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioTxCompleted.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.completed_at, TimePoint):
            raise InvalidValueError("RadioTxCompleted.completed_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioRxRequest:
    """RX window 요청. 시각은 MAC의 UTC 계획이다."""

    channel: PhysicalChannel
    start_at: TimePoint
    duration_nanoseconds: int
    sample_rate_hz: int

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioRxRequest.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.start_at, TimePoint):
            raise InvalidValueError("RadioRxRequest.start_at: TimePoint여야 합니다")
        if self.start_at.clock_domain is not ClockDomain.UTC:
            raise InvalidValueError("RadioRxRequest.start_at: UTC 계획이어야 합니다")
        for name in ("duration_nanoseconds", "sample_rate_hz"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"RadioRxRequest.{name}: 양의 정수여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioRxAccepted:
    channel: PhysicalChannel
    start_at: TimePoint
    duration_nanoseconds: int
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioRxAccepted.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.start_at, TimePoint):
            raise InvalidValueError("RadioRxAccepted.start_at: TimePoint여야 합니다")
        if (
            isinstance(self.duration_nanoseconds, bool)
            or not isinstance(self.duration_nanoseconds, int)
            or self.duration_nanoseconds <= 0
        ):
            raise InvalidValueError("RadioRxAccepted.duration_nanoseconds: 양의 정수여야 합니다")
        if not isinstance(self.accepted_at, TimePoint):
            raise InvalidValueError("RadioRxAccepted.accepted_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class RadioSamplesReceived:
    """수신 sample 통지. 연속성과 hardware 수신시각을 함께 전달한다."""

    channel: PhysicalChannel
    samples: SampleBlock
    hardware_received_at: TimePoint
    continuity: SampleContinuity

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioSamplesReceived.channel이 잘못되었습니다")
        if not isinstance(self.samples, SampleBlock):
            raise InvalidValueError("RadioSamplesReceived.samples: SampleBlock이어야 합니다")
        if not isinstance(self.hardware_received_at, TimePoint):
            raise InvalidValueError("RadioSamplesReceived.hardware_received_at이 잘못되었습니다")
        if not isinstance(self.continuity, SampleContinuity):
            raise InvalidValueError("RadioSamplesReceived.continuity가 잘못되었습니다")
        if self.continuity is not self.samples.continuity:
            raise InvalidValueError("RadioSamplesReceived.continuity가 SampleBlock과 다릅니다")


@dataclass(frozen=True, slots=True)
class RadioStatusRequest:
    channel: PhysicalChannel | None = None

    def __post_init__(self) -> None:
        if self.channel is not None and not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RadioStatusRequest.channel: PhysicalChannel이어야 합니다")


@dataclass(frozen=True, slots=True)
class RadioStatusSnapshot:
    """장치 상태 snapshot. UTC 동기 판정과 frame/slot 계산은 하지 않는다."""

    adapter_kind: str
    device_state: RadioDeviceState
    clock_reference: ClockReference
    clock_locked: bool
    pps_locked: bool
    underflow_count: int = 0
    overflow_count: int = 0
    late_command_count: int = 0
    details: Details = field(default=EMPTY_DETAILS)
    timing_status: RadioTimingStatus | None = None
    queue_status: tuple[RadioQueueStatus, ...] = ()
    event_delivery_status: RadioEventDeliveryStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_kind, str) or not self.adapter_kind:
            raise InvalidValueError("RadioStatusSnapshot.adapter_kind가 비어 있습니다")
        if not isinstance(self.device_state, RadioDeviceState):
            raise InvalidValueError("RadioStatusSnapshot.device_state가 잘못되었습니다")
        if not isinstance(self.clock_reference, ClockReference):
            raise InvalidValueError("RadioStatusSnapshot.clock_reference가 잘못되었습니다")
        for name in ("clock_locked", "pps_locked"):
            if not isinstance(getattr(self, name), bool):
                raise InvalidValueError(f"RadioStatusSnapshot.{name}: bool이어야 합니다")
        for name in ("underflow_count", "overflow_count", "late_command_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidValueError(f"RadioStatusSnapshot.{name}: 0 이상의 정수여야 합니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("RadioStatusSnapshot.details: Details여야 합니다")
        if self.timing_status is not None and not isinstance(self.timing_status, RadioTimingStatus):
            raise InvalidValueError("RadioStatusSnapshot.timing_status가 잘못되었습니다")
        if not isinstance(self.queue_status, tuple) or any(
            not isinstance(value, RadioQueueStatus) for value in self.queue_status
        ):
            raise InvalidValueError("RadioStatusSnapshot.queue_status가 잘못되었습니다")
        if self.event_delivery_status is not None and not isinstance(
            self.event_delivery_status, RadioEventDeliveryStatus
        ):
            raise InvalidValueError("RadioStatusSnapshot.event_delivery_status가 잘못되었습니다")
