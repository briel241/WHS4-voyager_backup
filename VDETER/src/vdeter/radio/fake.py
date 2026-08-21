"""`FakeRadioAdapter` (`stage0_baseline.md` §5.5, §9.2).

실제 장치를 열지 않고 configure → start → TX/RX request → event → stop lifecycle과
`burst_id` correlation을 재현하는 **test scope** adapter다. 실제 sample I/O와 장치 제어는
단계 3 Radio 기준선의 `SoftwareRadioAdapter`·`B210RadioAdapter`가 구현한다.

이 adapter는 Packet, Message, Link ID 정책, CRC, FEC 또는 payload를 해석하지 않는다.
"""

from __future__ import annotations

from ..common.contracts.radio import (
    ClockReference,
    RadioConfigurationApplied,
    RadioConfigureRequest,
    RadioDeviceState,
    RadioRxAccepted,
    RadioRxRequest,
    RadioSamplesReceived,
    RadioStarted,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStatusSnapshot,
    RadioStopped,
    RadioStopRequest,
    RadioTxAccepted,
    RadioTxCompleted,
    RadioTxRequest,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.events import EventSeverity, InternalEvent, InternalEventType
from ..common.identifiers import IdSource, PhysicalChannel
from ..common.results import Ok, internal_error
from ..common.time import TimeSource
from .models import RadioAdapterKind

ADAPTER_KIND = RadioAdapterKind.FAKE.value


class FakeRadioAdapter:
    """장치 없이 Radio 계약을 실행하는 test scope adapter."""

    __slots__ = (
        "_time_source",
        "_id_source",
        "_event_sink",
        "_configuration",
        "_device_state",
        "_scheduled",
        "_rx_windows",
        "_completed",
        "_last_stopped",
    )

    def __init__(self, time_source: TimeSource, id_source: IdSource, event_sink) -> None:
        self._time_source = time_source
        self._id_source = id_source
        self._event_sink = event_sink
        self._configuration = None
        self._device_state = RadioDeviceState.DISCONNECTED
        self._scheduled = []
        self._rx_windows = []
        self._completed = []
        self._last_stopped = None

    # --- lifecycle ---

    def configure(self, request: RadioConfigureRequest):
        if not isinstance(request, RadioConfigureRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioConfigureRequest가 필요합니다",
            )
        if self._device_state not in (RadioDeviceState.DISCONNECTED, RadioDeviceState.STOPPED):
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_CONFIGURATION_FAILED,
                ErrorCategory.DEVICE,
                "configure는 DISCONNECTED 또는 STOPPED state에서만 허용됩니다",
            )
        configuration = request.configuration
        if configuration.adapter_kind != ADAPTER_KIND:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "FakeRadioAdapter 설정이 아닙니다",
                details=Details.of(adapter_kind=configuration.adapter_kind),
            )
        if not configuration.channels:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "Physical Channel 설정이 최소 하나 필요합니다",
            )
        self._configuration = configuration
        self._device_state = RadioDeviceState.CONFIGURED
        # configure는 새 실행의 시작점이다. 이전 실행의 예약·수신·완료 runtime 상태를
        # 재사용하지 않는다.
        self._scheduled.clear()
        self._rx_windows.clear()
        self._completed.clear()
        self._last_stopped = None
        return Ok(
            RadioConfigurationApplied(
                adapter_kind=ADAPTER_KIND,
                device_state=self._device_state,
                channels=configuration.channel_names(),
                sample_format=configuration.sample_format,
            )
        )

    def start(self, request: RadioStartRequest):
        if not isinstance(request, RadioStartRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioStartRequest가 필요합니다",
            )
        if self._configuration is None:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_NOT_CONFIGURED,
                ErrorCategory.VALIDATION,
                "configure 전에는 시작할 수 없습니다",
            )
        if self._device_state is not RadioDeviceState.CONFIGURED:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_ALREADY_STARTED,
                ErrorCategory.VALIDATION,
                "start는 CONFIGURED state에서만 허용됩니다",
            )
        self._device_state = RadioDeviceState.RUNNING
        return Ok(
            RadioStarted(
                adapter_kind=ADAPTER_KIND,
                device_state=self._device_state,
                started_at=self._time_source.now_utc(),
            )
        )

    def stop(self, request: RadioStopRequest):
        if not isinstance(request, RadioStopRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioStopRequest가 필요합니다",
            )
        if self._device_state is RadioDeviceState.STOPPED and self._last_stopped is not None:
            return Ok(self._last_stopped)
        if self._device_state is RadioDeviceState.DISCONNECTED:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_NOT_STARTED,
                ErrorCategory.VALIDATION,
                "configure 전에는 정지할 수 없습니다",
            )
        self._device_state = RadioDeviceState.STOPPED
        self._scheduled.clear()
        self._rx_windows.clear()
        self._completed.clear()
        self._last_stopped = RadioStopped(
                adapter_kind=ADAPTER_KIND,
                device_state=self._device_state,
                stopped_at=self._time_source.now_utc(),
            )
        return Ok(self._last_stopped)

    # --- 송수신 ---

    def schedule_tx(self, request: RadioTxRequest):
        if not isinstance(request, RadioTxRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioTxRequest가 필요합니다",
            )
        guard = self._require_running(Correlation(burst_id=request.burst_id))
        if guard is not None:
            return guard
        channel_check = self._require_channel(request.channel, Correlation(burst_id=request.burst_id))
        if channel_check is not None:
            return channel_check
        if request.samples.sample_format != self._configuration.sample_format:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "설정된 sample format과 다릅니다",
                correlation=Correlation(burst_id=request.burst_id),
                details=Details.of(
                    configured=self._configuration.sample_format,
                    requested=request.samples.sample_format,
                ),
            )
        now = self._time_source.now_utc()
        self._scheduled.append(request)
        # 예약 접수와 실제 송신 완료는 구분한다. 완료는 별도 event로 통지한다.
        return Ok(
            RadioTxAccepted(
                burst_id=request.burst_id,
                channel=request.channel,
                scheduled_at=request.transmit_at,
                accepted_at=now,
            )
        )

    def complete_scheduled_tx(self):
        """예약된 burst의 송신 완료를 통지한다.

        Fake adapter는 hardware clock을 실행하지 않으므로 시험 fixture가 완료 시점을
        명시적으로 진행시킨다. 실제 완료 통지는 단계 3 adapter가 장치 결과로 만든다.
        """

        guard = self._require_running()
        if guard is not None:
            return guard
        completed = []
        pending, self._scheduled = self._scheduled, []
        for request in pending:
            completion = RadioTxCompleted(
                burst_id=request.burst_id,
                channel=request.channel,
                completed_at=self._time_source.now_utc(),
            )
            self._completed.append(completion)
            published = self._publish(
                InternalEventType.BURST_COMPLETED,
                payload=completion,
                correlation=Correlation(burst_id=request.burst_id),
                details=Details.of(channel=request.channel.name),
            )
            if published.is_err:
                return published
            completed.append(completion)
        return Ok(tuple(completed))

    def request_rx(self, request: RadioRxRequest):
        if not isinstance(request, RadioRxRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioRxRequest가 필요합니다",
            )
        guard = self._require_running()
        if guard is not None:
            return guard
        channel_check = self._require_channel(request.channel)
        if channel_check is not None:
            return channel_check
        self._rx_windows.append(request)
        return Ok(
            RadioRxAccepted(
                channel=request.channel,
                start_at=request.start_at,
                duration_nanoseconds=request.duration_nanoseconds,
                accepted_at=self._time_source.now_utc(),
            )
        )

    def status(self, request: RadioStatusRequest):
        if not isinstance(request, RadioStatusRequest):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "RadioStatusRequest가 필요합니다",
            )
        clock_reference = (
            self._configuration.clock_reference if self._configuration else ClockReference.NONE
        )
        running = self._device_state is RadioDeviceState.RUNNING
        return Ok(
            RadioStatusSnapshot(
                adapter_kind=ADAPTER_KIND,
                device_state=self._device_state,
                clock_reference=clock_reference,
                clock_locked=running,
                pps_locked=running,
                details=Details.of(
                    scheduled_tx=len(self._scheduled),
                    rx_windows=len(self._rx_windows),
                ),
            )
        )

    # --- 시험 전용 주입 helper ---

    def inject_rx_samples(
        self, channel: PhysicalChannel, samples: SampleBlock, continuity=SampleContinuity.CONTIGUOUS
    ):
        """시험 fixture가 수신 sample을 주입한다. 운영 경로에서는 사용하지 않는다."""

        if not isinstance(samples, SampleBlock):
            return internal_error(
                Layer.RADIO,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "SampleBlock이 필요합니다",
            )
        guard = self._require_running()
        if guard is not None:
            return guard
        channel_check = self._require_channel(channel)
        if channel_check is not None:
            return channel_check
        normalized_samples = SampleBlock(
            sample_format=samples.sample_format,
            sample_rate_hz=samples.sample_rate_hz,
            sample_count=samples.sample_count,
            data=samples.data,
            first_sample_at=samples.first_sample_at,
            continuity=continuity,
        )
        received = RadioSamplesReceived(
            channel=channel,
            samples=normalized_samples,
            hardware_received_at=normalized_samples.first_sample_at,
            continuity=continuity,
        )
        published = self._publish(
            InternalEventType.SAMPLES_RECEIVED,
            payload=received,
            details=Details.of(channel=channel.name, sample_count=samples.sample_count),
        )
        if published.is_err:
            return published
        return Ok(received)

    def scheduled_bursts(self) -> tuple:
        """아직 완료 통지를 내지 않은 예약 burst."""

        return tuple(self._scheduled)

    def completed_bursts(self) -> tuple:
        return tuple(self._completed)

    def rx_windows(self) -> tuple:
        return tuple(self._rx_windows)

    # --- 내부 helper ---

    def _require_running(self, correlation: Correlation = Correlation()):
        if self._configuration is None:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_NOT_CONFIGURED,
                ErrorCategory.VALIDATION,
                "Radio가 설정되지 않았습니다",
                correlation=correlation,
            )
        if self._device_state is not RadioDeviceState.RUNNING:
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_NOT_STARTED,
                ErrorCategory.VALIDATION,
                "Radio가 시작되지 않았습니다",
                correlation=correlation,
            )
        return None

    def _require_channel(self, channel: PhysicalChannel, correlation: Correlation = Correlation()):
        if channel.name not in self._configuration.channel_names():
            return internal_error(
                Layer.RADIO,
                ErrorCode.RADIO_UNKNOWN_CHANNEL,
                ErrorCategory.VALIDATION,
                "설정되지 않은 Physical Channel입니다",
                correlation=correlation,
                details=Details.of(channel=channel.name),
            )
        return None

    def _publish(self, event_type, payload, correlation: Correlation = Correlation(), details=None):
        from ..common.errors import EMPTY_DETAILS

        return self._event_sink.publish(
            InternalEvent(
                event_id=self._id_source.new_event_id(),
                event_type=event_type,
                occurred_at=self._time_source.now_utc(),
                severity=EventSeverity.INFO,
                layer=Layer.RADIO,
                correlation=correlation,
                payload=payload,
                details=details if details is not None else EMPTY_DETAILS,
            )
        )
