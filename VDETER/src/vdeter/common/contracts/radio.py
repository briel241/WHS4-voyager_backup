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
from ..time import ClockDomain, TimePoint

#: 단계 0 Fake adapter 전용 sample format. I, Q 순서의 little-endian signed 16-bit pair다.
SAMPLE_FORMAT_TEST_IQ_INT16_LE = "test-iq-int16-le"

#: `sample_format` → complex sample 하나가 차지하는 byte 수.
#: 단계 0에는 test scope 값만 등록한다. 운용 format은 단계 3에서 등록한다.
#: runtime에 변경할 수 없는 읽기 전용 표다.
SAMPLE_FORMAT_BYTES_PER_SAMPLE = MappingProxyType(
    {
        SAMPLE_FORMAT_TEST_IQ_INT16_LE: 4,
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
        if not isinstance(self.samples, SampleBlock):
            raise InvalidValueError("RadioSamplesReceived.samples: SampleBlock이어야 합니다")
        if not isinstance(self.continuity, SampleContinuity):
            raise InvalidValueError("RadioSamplesReceived.continuity가 잘못되었습니다")


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
