"""Approved Link ID 11 transmitter and frozen Stage 0 PhyPort facade."""

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
    SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import IdSource, LinkId
from ..common.results import Ok, internal_error
from ..common.time import ClockDomain, TimeSource
from ..profiles.models import RegistryScope
from .burst.framing import TxStages, build
from .burst.link_id import LinkIdDetectionError
from .burst.rrc import from_cf32le, from_test_iq_int16_le, to_cf32le, to_test_iq_int16_le
from .receiver import (
    FecDecodeFailed,
    LinkIdNotAllowed,
    NoBurstDetected,
    Phy11Receiver,
    SynchronizationFailed,
    UnsupportedDetectedLinkId,
)

LINK_ID = 11
PACKET_BYTES = 54
SAMPLE_RATE_HZ = 76800
SLOT_SAMPLES = 2048


class Phy11Transmitter:
    """Private complex-sample core; it does not parse CRC or Message content."""

    def encode(self, packet: bytes) -> TxStages:
        return build(packet)


class Link11Phy:
    """Stage 2 concrete implementation of the frozen ``PhyPort`` contract."""

    __slots__ = (
        "_profiles", "_time_source", "_id_source", "_transmitter", "_receiver", "_sample_format"
    )

    def __init__(
        self,
        profiles,
        time_source: TimeSource,
        id_source: IdSource,
        sample_format: str = SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    ) -> None:
        if sample_format not in (SAMPLE_FORMAT_TEST_IQ_INT16_LE, SAMPLE_FORMAT_CF32LE):
            raise ValueError("Link11Phy sample_format must be test-iq-int16-le or cf32le")
        self._profiles = profiles
        self._time_source = time_source
        self._id_source = id_source
        self._transmitter = Phy11Transmitter()
        self._receiver = Phy11Receiver()
        self._sample_format = sample_format

    def _ready_registry(self):
        registry = self._profiles
        if registry is None or not hasattr(registry, "lookup"):
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_NOT_FROZEN,
                ErrorCategory.VALIDATION,
                "phy_profile registry가 제공되지 않았습니다",
                details=Details.of(registry="phy_profile"),
            )
        if not registry.is_frozen:
            return registry.lookup(LINK_ID)
        if registry.scope is not RegistryScope.OPERATIONAL:
            return internal_error(
                Layer.COMMON,
                ErrorCode.REGISTRY_SCOPE_MISMATCH,
                ErrorCategory.VALIDATION,
                "운영 PHY에는 operational registry가 필요합니다",
                details=Details.of(
                    registry="phy_profile",
                    registry_scope=registry.scope.value,
                    entry_scope=RegistryScope.OPERATIONAL.value,
                ),
            )
        return Ok(registry)

    def encode_packet(self, request: PhyEncodeRequest):
        if not isinstance(request, PhyEncodeRequest):
            return internal_error(
                Layer.PHY, ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                "PhyEncodeRequest가 필요합니다",
            )
        correlation = Correlation(packet_id=request.packet_id)
        registry = self._ready_registry()
        if registry.is_err:
            return registry
        if request.link_id.value != LINK_ID:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_UNSUPPORTED_LINK_ID, ErrorCategory.UNSUPPORTED,
                "Stage 2 PHY는 Link ID 11만 지원합니다",
                correlation=correlation, details=Details.of(link_id=request.link_id.value),
            )
        profile = registry.value.lookup(LINK_ID)
        if profile.is_err:
            return profile
        if len(request.packet) != PACKET_BYTES:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_PACKET_LENGTH_INVALID, ErrorCategory.VALIDATION,
                "Link ID 11 Packet은 정확히 54 bytes여야 합니다",
                correlation=correlation,
                details=Details.of(actual_bytes=len(request.packet), expected_bytes=PACKET_BYTES),
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
            stages = self._transmitter.encode(request.packet)
            if len(stages.samples) != SLOT_SAMPLES:
                raise AssertionError("slot sample count")
            if self._sample_format == SAMPLE_FORMAT_CF32LE:
                sample_data = to_cf32le(stages.samples)
            else:
                sample_data = to_test_iq_int16_le(stages.samples)
            samples = SampleBlock(
                sample_format=self._sample_format,
                sample_rate_hz=SAMPLE_RATE_HZ,
                sample_count=SLOT_SAMPLES,
                data=sample_data,
                first_sample_at=request.transmit_at,
                continuity=SampleContinuity.CONTIGUOUS,
            )
        except (ArithmeticError, ValueError, AssertionError) as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, ErrorCategory.INTERNAL,
                "승인된 Link ID 11 송신 count 또는 finite sample 불변식이 깨졌습니다",
                correlation=correlation, details=Details.of(reason=type(error).__name__),
            )
        return Ok(
            BasebandBurst(
                burst_id=self._id_source.new_burst_id(), packet_id=request.packet_id,
                link_id=request.link_id, channel=request.channel,
                transmit_at=request.transmit_at, samples=samples,
            )
        )

    def decode_samples(self, request: PhyDecodeRequest):
        if not isinstance(request, PhyDecodeRequest):
            return internal_error(
                Layer.PHY, ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                "PhyDecodeRequest가 필요합니다",
            )
        registry = self._ready_registry()
        if registry.is_err:
            return registry
        profile = registry.value.lookup(LINK_ID)
        if profile.is_err:
            return profile
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
        if samples.sample_rate_hz != SAMPLE_RATE_HZ:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_SAMPLE_RATE_MISMATCH, ErrorCategory.VALIDATION,
                "Link ID 11 sample rate는 76,800 samples/s여야 합니다",
                details=Details.of(
                    actual_sample_rate_hz=samples.sample_rate_hz,
                    expected_sample_rate_hz=SAMPLE_RATE_HZ,
                ),
            )
        allowed = frozenset(link_id.value for link_id in request.allowed_link_ids)
        try:
            if samples.sample_format == SAMPLE_FORMAT_CF32LE:
                complex_samples = from_cf32le(samples.data)
            elif samples.sample_format == SAMPLE_FORMAT_TEST_IQ_INT16_LE:
                complex_samples = from_test_iq_int16_le(samples.data)
            else:
                return internal_error(
                    Layer.PHY,
                    ErrorCode.RADIO_UNSUPPORTED_SETTING,
                    ErrorCategory.VALIDATION,
                    "Link ID 11 PHY가 지원하지 않는 sample format입니다",
                    details=Details.of(sample_format=samples.sample_format),
                )
            result = self._receiver.decode(complex_samples, allowed)
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
                "Stage 2 PHY가 지원하지 않는 유효 Link ID를 검출했습니다",
                details=Details.of(link_id=error.link_id),
            )
        except LinkIdNotAllowed:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_LINK_ID_NOT_ALLOWED, ErrorCategory.UNSUPPORTED,
                "검출된 Link ID 11이 request 허용 집합에 없습니다",
                details=Details.of(link_id=LINK_ID),
            )
        except FecDecodeFailed as error:
            return internal_error(
                Layer.PHY, ErrorCode.PHY_FEC_DECODE_FAILED, ErrorCategory.PROTOCOL,
                "finite 432-bit FEC candidate를 만들지 못했습니다",
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
        burst_start_samples = result.training_epoch_samples - 32.0 * result.rho_hat
        offset = burst_start_samples * 1_000_000_000.0 / SAMPLE_RATE_HZ
        offset_ns = math.floor(offset + 0.5) if offset >= 0.0 else math.ceil(offset - 0.5)
        received_at = samples.first_sample_at.shifted(offset_ns)
        packet = RecoveredPacket(
            packet_id=self._id_source.new_packet_id(), link_id=LinkId(LINK_ID),
            channel=request.channel, received_at=received_at, packet=result.packet,
        )
        return Ok(
            RecoveredPacketSet(
                channel=request.channel, received_at=received_at,
                status=PhyRecoveryStatus.RECOVERED, packets=(packet,),
            )
        )


__all__ = ("Link11Phy", "Phy11Transmitter")
