"""Link 11·17·19 native-rate PHY implementation.

This object implements the public ``PhyPort`` semantics.  It only observes
native PHY samples: Link 11 uses 76,800 samples/s and Links 17/19 use 307,200
samples/s.  Device-rate conversion is deliberately owned by the Runtime
private bridge in ``runtime/phy_bridge.py``.
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
from . import wideband
from .burst.rrc import SAMPLE_RATE_HZ as LINK11_SAMPLE_RATE_HZ, from_cf32le, to_cf32le
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
WIDEBAND_SAMPLE_RATE_HZ = wideband.SAMPLE_RATE_HZ


class MultiRatePhy:
    """2-PC 통합 profile의 PHY 경계. 공개 `PhyPort` 의미를 유지한다."""

    __slots__ = (
        "_profiles",
        "_time_source",
        "_id_source",
        "_link11",
        "_wideband",
        "_rx_stream_identities",
    )

    def __init__(self, profiles, time_source, id_source) -> None:
        self._profiles = profiles
        self._time_source = time_source
        self._id_source = id_source
        self._link11 = Link11Phy(
            profiles, time_source, id_source, sample_format=SAMPLE_FORMAT_CF32LE
        )
        self._wideband = wideband.WidebandReceiver()
        self._rx_stream_identities = {}

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
        self._update_stream_identity(request, allowed)
        if samples.sample_rate_hz not in (
            LINK11_SAMPLE_RATE_HZ,
            WIDEBAND_SAMPLE_RATE_HZ,
        ):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SAMPLE_RATE_MISMATCH, ErrorCategory.VALIDATION,
                "지원되는 native PHY sample rate가 아닙니다",
                details=Details.of(actual_sample_rate_hz=samples.sample_rate_hz),
            )
        try:
            complex_samples = from_cf32le(samples.data)
        except ValueError as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "수신 sample serialization이 잘못되었습니다",
                details=Details.of(reason=str(error)),
            )
        if any(
            not math.isfinite(value.real) or not math.isfinite(value.imag)
            for value in complex_samples
        ):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SYNCHRONIZATION_FAILED, ErrorCategory.PROTOCOL,
                "수신 sample에 비유한 값이 있습니다",
            )
        if not complex_samples or all(value == 0.0 + 0.0j for value in complex_samples):
            return Ok(
                RecoveredPacketSet(
                    channel=request.channel,
                    received_at=samples.first_sample_at,
                    status=PhyRecoveryStatus.NO_BURST_DETECTED,
                    packets=(),
                )
            )
        if samples.sample_rate_hz == LINK11_SAMPLE_RATE_HZ:
            return self._link11.decode_samples(request)
        return self._decode_wideband(request, allowed, complex_samples)

    def _decode_wideband(
        self,
        request: PhyDecodeRequest,
        allowed: frozenset[int],
        complex_samples,
    ):
        samples = request.samples
        try:
            results = self._wideband.decode_all(complex_samples, allowed or None)
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
        if any(
            not math.isfinite(value)
            for result in results
            for value in (result.training_epoch_samples, result.rho_hat)
        ):
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "복구 timestamp 내부 불변식이 깨졌습니다",
            )
        packets = []
        for result in results:
            burst_start_samples = (
                result.training_epoch_samples
                - float(wideband.RAMP_SAMPLES) * result.rho_hat
            )
            offset = burst_start_samples * 1_000_000_000.0 / WIDEBAND_SAMPLE_RATE_HZ
            offset_ns = (
                math.floor(offset + 0.5)
                if offset >= 0.0
                else math.ceil(offset - 0.5)
            )
            received_at = samples.first_sample_at.shifted(offset_ns)
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

    # --- 공통 ---------------------------------------------------------------

    def _ready_registry(self):
        return self._link11._ready_registry()

    def _update_stream_identity(
        self,
        request: PhyDecodeRequest,
        allowed: frozenset[int],
    ) -> None:
        """Reset candidate state whenever the normalized window intent changes."""

        samples = request.samples
        key = request.channel.name
        identity = (
            samples.sample_rate_hz,
            samples.sample_format,
            samples.first_sample_at.clock_id,
            tuple(sorted(allowed)),
        )
        previous = self._rx_stream_identities.get(key)
        if samples.continuity is not SampleContinuity.CONTIGUOUS or (
            previous is not None and previous != identity
        ):
            self.reset_stream(request.channel)
        self._rx_stream_identities[key] = identity

    def reset_stream(self, channel=None) -> None:
        """Discard endpoint-local partial receive identity without producing an error."""

        if channel is None:
            self._rx_stream_identities.clear()
            return
        name = channel.name if hasattr(channel, "name") else str(channel)
        self._rx_stream_identities.pop(name, None)


__all__ = ("MultiRatePhy",)
