"""Link 11·17·19 native-rate PHY implementation.

This object implements the public ``PhyPort`` semantics.  It only observes
native PHY samples: Link 11 uses 76,800 samples/s and Links 17/19 use 307,200
samples/s.  Device-rate conversion is deliberately owned by the Runtime
private bridge in ``runtime/phy_bridge.py``.
"""

from __future__ import annotations

import math
from fractions import Fraction

from ..common.contracts.phy import (
    BasebandBurst,
    PhyDecodeRequest,
    PhyEncodeRequest,
    PhyRecoveryStatus,
    RecoveredPacket,
    RecoveredPacketSet,
)
from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import LinkId
from ..common.results import Ok, internal_error
from ..common.time import ClockDomain
from . import wideband
from .burst.rrc import SAMPLE_RATE_HZ as LINK11_SAMPLE_RATE_HZ, from_cf32le, to_cf32le
from .receiver import (
    FecDecodeFailed,
    LinkIdDetectionError,
    LinkIdNotAllowed,
    NoBurstDetected,
    Phy11Receiver,
    SynchronizationFailed,
    UnsupportedDetectedLinkId,
)
from .transmitter import Link11Phy

LINK11_ID = 11
WIDEBAND_LINK_IDS = frozenset(wideband.PROFILES)
WIDEBAND_SAMPLE_RATE_HZ = wideband.SAMPLE_RATE_HZ

#: §7.9: per-channel raw rolling buffer 상한 = slot 8,192 + RRC history 40.
STREAM_HISTORY_SAMPLES = 40


class _StreamState:
    """§7.9 bounded streaming 상태 (channel별, Runtime 공개 DTO 아님)."""

    __slots__ = (
        "identity",
        "t0_point",
        "t0_ns",
        "buffer",
        "buffer_offset",
        "total_received",
    )

    def __init__(self, identity, t0_point) -> None:
        self.identity = identity
        self.t0_point = t0_point
        self.t0_ns = t0_point.total_nanoseconds()
        self.buffer: list[complex] = []
        #: stream 시작 이후 buffer[0]의 sample offset.
        self.buffer_offset = 0
        #: stream 시작 이후 소비(수신)한 누적 sample 수.
        self.total_received = 0

    def expected_epoch_ns(self, sample_rate_hz: int) -> int:
        """다음 block 기대 epoch: exact rational `t0 + N_total/Fs`를 한 번만 반올림."""

        return _round_half_away_ns(
            self.t0_ns, self.total_received, sample_rate_hz
        )

    def epoch_ns_at(self, sample_offset: int, sample_rate_hz: int) -> int:
        return _round_half_away_ns(self.t0_ns, sample_offset, sample_rate_hz)


def _round_half_away_ns(t0_ns: int, samples: int, sample_rate_hz: int) -> int:
    exact = Fraction(t0_ns) + Fraction(samples * 1_000_000_000, sample_rate_hz)
    floor = exact.numerator // exact.denominator
    remainder = exact - floor
    if remainder * 2 >= 1:
        return int(floor) + 1
    return int(floor)


class MultiRatePhy:
    """2-PC 통합 profile의 PHY 경계. 공개 `PhyPort` 의미를 유지한다."""

    __slots__ = (
        "_profiles",
        "_time_source",
        "_id_source",
        "_link11",
        "_wideband",
        "_rx_streams",
    )

    def __init__(self, profiles, time_source, id_source) -> None:
        self._profiles = profiles
        self._time_source = time_source
        self._id_source = id_source
        self._link11 = Link11Phy(
            profiles, time_source, id_source, sample_format=SAMPLE_FORMAT_CF32LE
        )
        self._wideband = wideband.WidebandReceiver()
        self._rx_streams = {}

    # --- TX -----------------------------------------------------------------

    def encode_packet(self, request: PhyEncodeRequest):
        if not isinstance(request, PhyEncodeRequest):
            return internal_error(
                Layer.PHY, ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                "PhyEncodeRequest가 필요합니다",
            )
        if request.link_id.value == LINK11_ID:
            return self._link11.encode_packet(request)
        if request.link_id.value in WIDEBAND_LINK_IDS:
            return self._encode_wideband(request)
        return internal_error(
            Layer.PHY, ErrorCode.PHY_UNSUPPORTED_LINK_ID, ErrorCategory.UNSUPPORTED,
            "multi-rate PHY는 Link ID 11·17·19만 지원합니다",
            correlation=Correlation(packet_id=request.packet_id),
            details=Details.of(link_id=request.link_id.value),
        )

    def _encode_wideband(self, request: PhyEncodeRequest):
        correlation = Correlation(packet_id=request.packet_id)
        registry = self._ready_registry()
        if registry.is_err:
            return registry
        profile = registry.value.lookup(request.link_id.value)
        if profile.is_err:
            return profile
        link_profile = wideband.PROFILES[request.link_id.value]
        if len(request.packet) != link_profile.packet_bytes:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_PACKET_LENGTH_INVALID, ErrorCategory.VALIDATION,
                f"Link ID {request.link_id.value} Packet은 정확히 "
                f"{link_profile.packet_bytes} bytes여야 합니다",
                correlation=correlation,
                details=Details.of(
                    actual_bytes=len(request.packet),
                    expected_bytes=link_profile.packet_bytes,
                ),
            )
        now = self._time_source.now_utc()
        if request.transmit_at.clock_id != now.clock_id:
            return internal_error(
                Layer.PHY, ErrorCode.CLOCK_DOMAIN_MISMATCH, ErrorCategory.VALIDATION,
                "송신 UTC clock ID가 주입된 TimeSource와 다릅니다",
                correlation=correlation,
                details=Details.of(
                    left_clock_domain=request.transmit_at.clock_domain.value,
                    left_clock_id=request.transmit_at.clock_id,
                    right_clock_domain=now.clock_domain.value,
                    right_clock_id=now.clock_id,
                ),
            )
        if request.transmit_at.total_nanoseconds() < now.total_nanoseconds():
            return internal_error(
                Layer.PHY, ErrorCode.PHY_TRANSMIT_DEADLINE_EXPIRED, ErrorCategory.TIMEOUT,
                "송신 계획 시각이 이미 지났습니다",
                correlation=correlation,
            )
        try:
            stages = wideband.encode_slot(request.link_id.value, request.packet)
            samples = SampleBlock(
                sample_format=SAMPLE_FORMAT_CF32LE,
                sample_rate_hz=WIDEBAND_SAMPLE_RATE_HZ,
                sample_count=wideband.SLOT_SAMPLES,
                data=to_cf32le(stages.samples),
                first_sample_at=request.transmit_at,
                continuity=SampleContinuity.CONTIGUOUS,
            )
        except (ArithmeticError, ValueError, AssertionError) as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "wideband 송신 count 또는 finite sample 불변식이 깨졌습니다",
                correlation=correlation, details=Details.of(reason=type(error).__name__),
            )
        return Ok(
            BasebandBurst(
                burst_id=self._id_source.new_burst_id(), packet_id=request.packet_id,
                link_id=request.link_id, channel=request.channel,
                transmit_at=request.transmit_at, samples=samples,
            )
        )

    # --- RX -----------------------------------------------------------------

    def decode_samples(self, request: PhyDecodeRequest):
        if not isinstance(request, PhyDecodeRequest):
            return internal_error(
                Layer.PHY, ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                "PhyDecodeRequest가 필요합니다",
            )
        registry = self._ready_registry()
        if registry.is_err:
            return registry
        samples = request.samples
        if samples.first_sample_at.clock_domain is not ClockDomain.UTC:
            return internal_error(
                Layer.PHY, ErrorCode.CLOCK_DOMAIN_MISMATCH, ErrorCategory.VALIDATION,
                "수신 첫 sample 시각은 UTC여야 합니다",
                details=Details.of(
                    left_clock_domain=samples.first_sample_at.clock_domain.value,
                    left_clock_id=samples.first_sample_at.clock_id,
                    right_clock_domain=ClockDomain.UTC.value,
                    right_clock_id=samples.first_sample_at.clock_id,
                ),
            )
        if samples.sample_format != SAMPLE_FORMAT_CF32LE:
            return internal_error(
                Layer.PHY, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION,
                "multi-rate PHY는 cf32le samples만 지원합니다",
                details=Details.of(sample_format=samples.sample_format),
            )
        allowed = frozenset(link_id.value for link_id in request.allowed_link_ids)
        identity = (
            samples.sample_rate_hz,
            samples.sample_format,
            samples.first_sample_at.clock_id,
            tuple(sorted(allowed)),
        )
        state = self._transition_stream(request, identity)
        if samples.sample_rate_hz not in (
            LINK11_SAMPLE_RATE_HZ,
            WIDEBAND_SAMPLE_RATE_HZ,
        ):
            self._rx_streams.pop(request.channel.name, None)
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SAMPLE_RATE_MISMATCH, ErrorCategory.VALIDATION,
                "지원되는 native PHY sample rate가 아닙니다",
                details=Details.of(actual_sample_rate_hz=samples.sample_rate_hz),
            )
        try:
            complex_samples = from_cf32le(samples.data)
        except ValueError as error:
            self._rx_streams.pop(request.channel.name, None)
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "수신 sample serialization이 잘못되었습니다",
                details=Details.of(reason=str(error)),
            )
        if any(
            not math.isfinite(value.real) or not math.isfinite(value.imag)
            for value in complex_samples
        ):
            self._rx_streams.pop(request.channel.name, None)
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "수신 sample에 비유한 값이 있습니다",
            )
        if not complex_samples:
            return Ok(
                RecoveredPacketSet(
                    channel=request.channel,
                    received_at=samples.first_sample_at,
                    status=PhyRecoveryStatus.NO_BURST_DETECTED,
                    packets=(),
                )
            )
        state.buffer.extend(complex_samples)
        state.total_received += len(complex_samples)
        return self._scan_stream(request, state, allowed)

    def _scan_stream(self, request: PhyDecodeRequest, state, allowed: frozenset[int]):
        """§7.9: earliest rolling window마다 detect/decode/consume을 반복한다."""

        samples = request.samples
        rate = samples.sample_rate_hz
        if rate == LINK11_SAMPLE_RATE_HZ:
            slot_samples = 2_048
            ramp_samples = 32.0

            def run(window):
                return (Phy11Receiver().decode(window, allowed or None),)

        else:
            slot_samples = wideband.SLOT_SAMPLES
            ramp_samples = float(wideband.RAMP_SAMPLES)

            def run(window):
                return self._wideband.decode_all(window, allowed or None)

        cap = slot_samples + STREAM_HISTORY_SAMPLES
        recovered: list[tuple[float, object]] = []
        first_failure = None
        while state.buffer:
            window = state.buffer[:cap]
            try:
                results = run(window)
            except NoBurstDetected:
                if len(state.buffer) > cap:
                    advance = cap - STREAM_HISTORY_SAMPLES
                    del state.buffer[:advance]
                    state.buffer_offset += advance
                    continue
                break
            except (
                LinkIdDetectionError,
                UnsupportedDetectedLinkId,
                LinkIdNotAllowed,
                SynchronizationFailed,
                FecDecodeFailed,
            ) as error:
                # §7.6: 처리 순서에서 처음 발생한 terminal failure만 보존한다.
                if first_failure is None:
                    first_failure = error
                if len(state.buffer) > cap:
                    advance = cap - STREAM_HISTORY_SAMPLES
                    del state.buffer[:advance]
                    state.buffer_offset += advance
                    continue
                # 같은 실패 candidate를 다음 호출에서 반복하지 않도록 소비한다.
                state.buffer_offset += len(state.buffer)
                state.buffer.clear()
                break
            window_offset = state.buffer_offset
            last_end = 0.0
            for result in results:
                if not math.isfinite(result.training_epoch_samples) or not math.isfinite(
                    result.rho_hat
                ):
                    return internal_error(
                        Layer.PHY,
                        ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
                        ErrorCategory.INTERNAL,
                        "복구 timestamp 내부 불변식이 깨졌습니다",
                    )
                start_samples = (
                    result.training_epoch_samples - ramp_samples * result.rho_hat
                )
                end_samples = start_samples + slot_samples * result.rho_hat
                last_end = max(last_end, end_samples)
                recovered.append((window_offset + start_samples, result))
            consume = min(
                len(state.buffer), max(1, math.floor(last_end + 0.5))
            )
            del state.buffer[:consume]
            state.buffer_offset += consume
        if recovered:
            return self._build_recovered_set(request, state, rate, recovered)
        if first_failure is not None:
            return self._map_stream_failure(first_failure, allowed, rate)
        return Ok(
            RecoveredPacketSet(
                channel=request.channel,
                received_at=samples.first_sample_at,
                status=PhyRecoveryStatus.NO_BURST_DETECTED,
                packets=(),
            )
        )

    def _build_recovered_set(self, request, state, rate: int, recovered):
        packets = []
        for start_offset_samples, result in recovered:
            offset = start_offset_samples * 1_000_000_000.0 / rate
            offset_ns = (
                math.floor(offset + 0.5)
                if offset >= 0.0
                else math.ceil(offset - 0.5)
            )
            received_at = state.t0_point.shifted(offset_ns)
            packets.append(
                RecoveredPacket(
                    packet_id=self._id_source.new_packet_id(),
                    link_id=LinkId(result.link_id),
                    channel=request.channel,
                    received_at=received_at,
                    packet=result.packet,
                )
            )
        packets.sort(key=lambda item: item.received_at.total_nanoseconds())
        return Ok(
            RecoveredPacketSet(
                channel=request.channel,
                received_at=packets[0].received_at,
                status=PhyRecoveryStatus.RECOVERED,
                packets=tuple(packets),
            )
        )

    def _map_stream_failure(self, error, allowed: frozenset[int], input_rate_hz: int):
        if isinstance(error, SynchronizationFailed):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "burst candidate 뒤 동기 복구가 실패했습니다",
                details=Details.of(reason=type(error).__name__),
            )
        if isinstance(error, LinkIdDetectionError):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_LINK_ID_DETECTION_FAILED, ErrorCategory.PROTOCOL,
                "유효한 Link ID codeword를 검출하지 못했습니다",
                details=Details.of(reason=type(error).__name__),
            )
        if isinstance(error, UnsupportedDetectedLinkId):
            remapped = self._detected_profile_rate_error(error.link_id, input_rate_hz)
            if remapped is not None:
                return remapped
            return internal_error(
                Layer.PHY, ErrorCode.PHY_UNSUPPORTED_LINK_ID, ErrorCategory.UNSUPPORTED,
                "PHY가 지원하지 않는 유효 Link ID를 검출했습니다",
                details=Details.of(link_id=error.link_id),
            )
        if isinstance(error, LinkIdNotAllowed):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_LINK_ID_NOT_ALLOWED, ErrorCategory.UNSUPPORTED,
                "검출된 Link ID가 request 허용 집합에 없습니다",
                details=Details.of(allowed_link_ids=tuple(sorted(allowed))),
            )
        return internal_error(
            Layer.PHY, ErrorCode.PHY_FEC_DECODE_FAILED, ErrorCategory.PROTOCOL,
            "finite FEC candidate를 만들지 못했습니다",
            details=Details.of(reason=type(error).__name__),
        )

    # --- 공통 ---------------------------------------------------------------

    def _detected_profile_rate_error(self, detected_link_id, input_rate_hz):
        """§7.8 4~5단계: 검출 ID가 registry에 등록된 다른 rate profile이면
        `PHY_SAMPLE_RATE_MISMATCH`로 구조화한다. 미등록이면 None을 돌려 기존
        `PHY_UNSUPPORTED_LINK_ID` 경로를 유지한다."""

        if detected_link_id is None:
            return None
        looked_up = self._profiles.lookup(detected_link_id)
        if looked_up.is_err:
            return None
        profile_rate = getattr(looked_up.value, "sample_rate_hz", None)
        if profile_rate is None or profile_rate == input_rate_hz:
            return None
        return internal_error(
            Layer.PHY, ErrorCode.PHY_SAMPLE_RATE_MISMATCH, ErrorCategory.VALIDATION,
            "검출된 Link ID profile의 sample rate가 입력 rate와 다릅니다",
            details=Details.of(
                link_id=detected_link_id,
                profile_sample_rate_hz=profile_rate,
                actual_sample_rate_hz=input_rate_hz,
            ),
        )

    def _ready_registry(self):
        return self._link11._ready_registry()

    def _transition_stream(self, request: PhyDecodeRequest, identity):
        """§7.9: identity·continuity·기대 epoch로 stream 보존/폐기를 결정한다."""

        samples = request.samples
        key = request.channel.name
        previous = self._rx_streams.get(key)
        if samples.continuity is not SampleContinuity.CONTIGUOUS:
            # GAP_BEFORE/UNKNOWN: 이전 filter/acquisition state를 폐기하고 새 stream.
            self.reset_stream(request.channel)
        elif previous is None:
            pass
        elif previous.identity != identity:
            self.reset_stream(request.channel)
        elif (
            samples.first_sample_at.total_nanoseconds()
            != previous.expected_epoch_ns(samples.sample_rate_hz)
        ):
            # enum과 무관한 implicit discontinuity.
            self.reset_stream(request.channel)
        else:
            return previous
        state = _StreamState(identity, samples.first_sample_at)
        self._rx_streams[key] = state
        return state

    def reset_stream(self, channel=None) -> None:
        """Discard endpoint-local partial receive state without producing an error."""

        if channel is None:
            self._rx_streams.clear()
            return
        name = channel.name if hasattr(channel, "name") else str(channel)
        self._rx_streams.pop(name, None)


__all__ = ("MultiRatePhy",)
