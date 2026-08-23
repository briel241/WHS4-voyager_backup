"""Link 11·17·19 multi-rate PHY facade와 §13.5 변환 경계 소유.

`stage6_phy_common_baseline.md` §13 (`DR-STAGE6-MR-001`): Radio가 관찰하는 모든 TX/RX는
device `307,200/8,192`이고, Link 11 native `76,800/2,048`은 이 facade가 소유한 결정적
변환(`phy/resampling.py`)으로 상호 변환한다. PHY encode/decode 규범(§7.8)은 native rate만
관찰한다. Link 17·19는 무변환 pass-through다.
"""

from __future__ import annotations

import math

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
from . import resampling, wideband
from .burst.rrc import from_cf32le, to_cf32le
from .receiver import (
    FecDecodeFailed,
    LinkIdDetectionError,
    LinkIdNotAllowed,
    NoBurstDetected,
    SynchronizationFailed,
    UnsupportedDetectedLinkId,
)
from .transmitter import Link11Phy

LINK11_ID = 11
WIDEBAND_LINK_IDS = frozenset(wideband.PROFILES)
DEVICE_SAMPLE_RATE_HZ = resampling.DEVICE_SAMPLE_RATE_HZ
DEVICE_SLOT_SAMPLES = resampling.DEVICE_SLOT_SAMPLES


class MultiRatePhy:
    """2-PC 통합 profile의 PHY 경계. 공개 `PhyPort` 의미를 유지한다."""

    __slots__ = ("_profiles", "_time_source", "_id_source", "_link11", "_wideband")

    def __init__(self, profiles, time_source, id_source) -> None:
        self._profiles = profiles
        self._time_source = time_source
        self._id_source = id_source
        self._link11 = Link11Phy(
            profiles, time_source, id_source, sample_format=SAMPLE_FORMAT_CF32LE
        )
        self._wideband = wideband.WidebandReceiver()

    # --- TX -----------------------------------------------------------------

    def encode_packet(self, request: PhyEncodeRequest):
        if not isinstance(request, PhyEncodeRequest):
            return internal_error(
                Layer.PHY, ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                "PhyEncodeRequest가 필요합니다",
            )
        if request.link_id.value == LINK11_ID:
            native = self._link11.encode_packet(request)
            if native.is_err:
                return native
            return self._interpolated_burst(native.value)
        if request.link_id.value in WIDEBAND_LINK_IDS:
            return self._encode_wideband(request)
        return internal_error(
            Layer.PHY, ErrorCode.PHY_UNSUPPORTED_LINK_ID, ErrorCategory.UNSUPPORTED,
            "multi-rate PHY는 Link ID 11·17·19만 지원합니다",
            correlation=Correlation(packet_id=request.packet_id),
            details=Details.of(link_id=request.link_id.value),
        )

    def _interpolated_burst(self, burst: BasebandBurst):
        correlation = Correlation(packet_id=burst.packet_id)
        try:
            native_samples = from_cf32le(burst.samples.data)
            device_samples = resampling.interpolate_slot(native_samples)
            if any(
                abs(value.real) > 1.0 or abs(value.imag) > 1.0
                for value in device_samples
            ):
                raise ValueError("converted samples exceed the Radio TX full-scale contract")
            data = to_cf32le(device_samples)
        except (ValueError, ArithmeticError) as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "Link 11 native→device 변환 불변식이 깨졌습니다",
                correlation=correlation, details=Details.of(reason=str(error)),
            )
        samples = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=DEVICE_SAMPLE_RATE_HZ,
            sample_count=DEVICE_SLOT_SAMPLES,
            data=data,
            first_sample_at=burst.samples.first_sample_at,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        return Ok(
            BasebandBurst(
                burst_id=burst.burst_id, packet_id=burst.packet_id,
                link_id=burst.link_id, channel=burst.channel,
                transmit_at=burst.transmit_at, samples=samples,
            )
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
                sample_rate_hz=DEVICE_SAMPLE_RATE_HZ,
                sample_count=DEVICE_SLOT_SAMPLES,
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
        samples = request.samples
        if samples.sample_rate_hz != DEVICE_SAMPLE_RATE_HZ:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SAMPLE_RATE_MISMATCH, ErrorCategory.VALIDATION,
                "multi-rate PHY 경계의 device rate는 307,200 samples/s여야 합니다",
                details=Details.of(
                    actual_sample_rate_hz=samples.sample_rate_hz,
                    expected_sample_rate_hz=DEVICE_SAMPLE_RATE_HZ,
                ),
            )
        allowed = frozenset(link_id.value for link_id in request.allowed_link_ids)
        wants_link11 = LINK11_ID in allowed
        wants_wideband = bool(allowed & WIDEBAND_LINK_IDS)
        if wants_link11 and wants_wideband:
            return internal_error(
                Layer.PHY, ErrorCode.CONFIG_SEMANTIC_INVALID, ErrorCategory.VALIDATION,
                "한 RX window의 허용 집합에 두 rate family를 섞을 수 없습니다",
                details=Details.of(allowed_link_ids=tuple(sorted(allowed))),
            )
        if wants_link11:
            return self._decode_link11(request)
        return self._decode_wideband(request, allowed)

    def _decode_link11(self, request: PhyDecodeRequest):
        samples = request.samples
        if samples.sample_format != SAMPLE_FORMAT_CF32LE:
            return internal_error(
                Layer.PHY, ErrorCode.RADIO_UNSUPPORTED_SETTING, ErrorCategory.VALIDATION,
                "multi-rate 변환 경계는 cf32le samples만 지원합니다",
                details=Details.of(sample_format=samples.sample_format),
            )
        try:
            device_samples = from_cf32le(samples.data)
            if len(device_samples) != resampling.DEVICE_SLOT_SAMPLES:
                raise ValueError(
                    f"device window must contain exactly {resampling.DEVICE_SLOT_SAMPLES} samples"
                )
            native_samples = resampling.decimate_slot(device_samples)
        except (ValueError, ArithmeticError) as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "device→native 변환 불변식이 깨졌습니다",
                details=Details.of(reason=str(error)),
            )
        native_block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.NATIVE_SAMPLE_RATE_HZ,
            sample_count=resampling.NATIVE_SLOT_SAMPLES,
            data=to_cf32le(native_samples),
            first_sample_at=samples.first_sample_at,
            continuity=samples.continuity,
        )
        native_request = PhyDecodeRequest(
            channel=request.channel,
            samples=native_block,
            allowed_link_ids=request.allowed_link_ids,
        )
        return self._link11.decode_samples(native_request)

    def _decode_wideband(self, request: PhyDecodeRequest, allowed: frozenset[int]):
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
                "wideband PHY는 cf32le samples만 지원합니다",
                details=Details.of(sample_format=samples.sample_format),
            )
        try:
            complex_samples = from_cf32le(samples.data)
            result = self._wideband.decode(complex_samples, allowed or None)
        except NoBurstDetected:
            return Ok(
                RecoveredPacketSet(
                    channel=request.channel, received_at=samples.first_sample_at,
                    status=PhyRecoveryStatus.NO_BURST_DETECTED, packets=(),
                )
            )
        except SynchronizationFailed as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "burst candidate 뒤 동기 복구가 실패했습니다",
                details=Details.of(reason=type(error).__name__),
            )
        except LinkIdDetectionError as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_LINK_ID_DETECTION_FAILED, ErrorCategory.PROTOCOL,
                "유효한 Link ID codeword를 검출하지 못했습니다",
                details=Details.of(reason=type(error).__name__),
            )
        except UnsupportedDetectedLinkId as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_UNSUPPORTED_LINK_ID, ErrorCategory.UNSUPPORTED,
                "wideband PHY가 지원하지 않는 유효 Link ID를 검출했습니다",
                details=Details.of(link_id=error.link_id),
            )
        except LinkIdNotAllowed:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_LINK_ID_NOT_ALLOWED, ErrorCategory.UNSUPPORTED,
                "검출된 wideband Link ID가 request 허용 집합에 없습니다",
                details=Details.of(allowed_link_ids=tuple(sorted(allowed))),
            )
        except FecDecodeFailed as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_FEC_DECODE_FAILED, ErrorCategory.PROTOCOL,
                "finite wideband FEC candidate를 만들지 못했습니다",
                details=Details.of(reason=type(error).__name__),
            )
        if not all(
            math.isfinite(value)
            for value in (result.training_epoch_samples, result.rho_hat)
        ):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "복구 timestamp 내부 불변식이 깨졌습니다",
            )
        burst_start_samples = (
            result.training_epoch_samples - float(wideband.RAMP_SAMPLES) * result.rho_hat
        )
        offset = burst_start_samples * 1_000_000_000.0 / DEVICE_SAMPLE_RATE_HZ
        offset_ns = math.floor(offset + 0.5) if offset >= 0.0 else math.ceil(offset - 0.5)
        received_at = samples.first_sample_at.shifted(offset_ns)
        packet = RecoveredPacket(
            packet_id=self._id_source.new_packet_id(), link_id=LinkId(result.link_id),
            channel=request.channel, received_at=received_at, packet=result.packet,
        )
        return Ok(
            RecoveredPacketSet(
                channel=request.channel, received_at=received_at,
                status=PhyRecoveryStatus.RECOVERED, packets=(packet,),
            )
        )

    # --- 공통 ---------------------------------------------------------------

    def _ready_registry(self):
        return self._link11._ready_registry()


__all__ = ("MultiRatePhy",)
