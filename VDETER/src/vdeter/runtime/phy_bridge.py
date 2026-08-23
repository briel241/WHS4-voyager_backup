"""Runtime-private conversion between native PHY and Radio device samples.

The public PHY implementation never sees Radio short reads and never changes
Link 11's native rate.  This bridge is the single owner of the deterministic
76.8/307.2 ksps conversion used by the wideband two-channel deployment.
"""

from __future__ import annotations

from ..common.contracts.phy import BasebandBurst, PhyDecodeRequest
from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..phy import resampling
from ..phy.burst.rrc import (
    from_cf32le,
    to_cf32le,
    validate_cf32le_full_scale,
)

LINK11_ID = 11
WIDEBAND_LINK_IDS = frozenset((17, 19))
SUPPORTED_LINK_IDS = frozenset((LINK11_ID,)) | WIDEBAND_LINK_IDS


class RuntimePhyRadioBridge:
    """Convert complete device windows and discard incomplete Radio windows."""

    __slots__ = ("_native_phy",)

    def __init__(self, native_phy) -> None:
        self._native_phy = native_phy

    def to_device_burst(self, burst: BasebandBurst):
        if not isinstance(burst, BasebandBurst):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "BasebandBurst가 필요합니다",
            )
        correlation = Correlation(packet_id=burst.packet_id, burst_id=burst.burst_id)
        try:
            samples = burst.samples
            if samples.sample_format != SAMPLE_FORMAT_CF32LE:
                raise ValueError("native PHY burst must use cf32le")
            if burst.link_id.value == LINK11_ID:
                if (
                    samples.sample_rate_hz != resampling.NATIVE_SAMPLE_RATE_HZ
                    or samples.sample_count != resampling.NATIVE_SLOT_SAMPLES
                ):
                    raise ValueError("Link 11 native burst rate/count mismatch")
                device_values = resampling.interpolate_slot(from_cf32le(samples.data))
                device_data = to_cf32le(device_values)
            elif burst.link_id.value in WIDEBAND_LINK_IDS:
                if (
                    samples.sample_rate_hz != resampling.DEVICE_SAMPLE_RATE_HZ
                    or samples.sample_count != resampling.DEVICE_SLOT_SAMPLES
                ):
                    raise ValueError("wideband native burst rate/count mismatch")
                device_data = samples.data
            else:
                raise ValueError("unsupported Link ID at the Runtime bridge")
            validate_cf32le_full_scale(device_data)
        except (ArithmeticError, ValueError) as error:
            return internal_error(
                Layer.PHY,
                ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
                ErrorCategory.INTERNAL,
                "native PHY→Radio device 변환 불변식이 깨졌습니다",
                correlation=correlation,
                details=Details.of(reason=str(error)),
            )
        device_block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            sample_count=resampling.DEVICE_SLOT_SAMPLES,
            data=device_data,
            first_sample_at=samples.first_sample_at,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        return Ok(
            BasebandBurst(
                burst_id=burst.burst_id,
                packet_id=burst.packet_id,
                link_id=burst.link_id,
                channel=burst.channel,
                transmit_at=burst.transmit_at,
                samples=device_block,
            )
        )

    def to_native_decode_request(self, request: PhyDecodeRequest):
        """Return ``Ok(None)`` when Radio delivered an incomplete/gapped window."""

        if not isinstance(request, PhyDecodeRequest):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PhyDecodeRequest가 필요합니다",
            )
        samples = request.samples
        allowed = frozenset(link_id.value for link_id in request.allowed_link_ids)
        if not allowed:
            return self._configuration_error(
                "device-rate RX window에는 비어 있지 않은 allowed_link_ids가 필요합니다",
                allowed,
            )
        if not allowed <= SUPPORTED_LINK_IDS:
            return self._configuration_error(
                "device-rate RX window에 지원되지 않는 Link ID가 있습니다",
                allowed,
            )
        link11_family = LINK11_ID in allowed
        wideband_family = bool(allowed & WIDEBAND_LINK_IDS)
        if link11_family and wideband_family:
            return self._configuration_error(
                "한 device-rate RX window에 두 native rate family를 섞을 수 없습니다",
                allowed,
            )
        if samples.sample_format != SAMPLE_FORMAT_CF32LE:
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "Runtime PHY bridge는 cf32le Radio samples만 지원합니다",
                details=Details.of(sample_format=samples.sample_format),
            )
        if samples.sample_rate_hz != resampling.DEVICE_SAMPLE_RATE_HZ:
            return self._configuration_error(
                "Radio RX window가 device sample rate와 일치하지 않습니다",
                allowed,
                actual_sample_rate_hz=samples.sample_rate_hz,
            )
        if (
            samples.sample_count != resampling.DEVICE_SLOT_SAMPLES
            or samples.continuity is not SampleContinuity.CONTIGUOUS
        ):
            self.reset_stream(request.channel)
            return Ok(None)
        if wideband_family:
            return Ok(request)
        try:
            native_values = resampling.decimate_slot(from_cf32le(samples.data))
            native_data = to_cf32le(native_values)
        except (ArithmeticError, ValueError) as error:
            self.reset_stream(request.channel)
            return internal_error(
                Layer.PHY,
                ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
                ErrorCategory.INTERNAL,
                "Radio device→Link 11 native 변환 불변식이 깨졌습니다",
                details=Details.of(reason=str(error)),
            )
        return Ok(
            PhyDecodeRequest(
                channel=request.channel,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=resampling.NATIVE_SAMPLE_RATE_HZ,
                    sample_count=resampling.NATIVE_SLOT_SAMPLES,
                    data=native_data,
                    first_sample_at=samples.first_sample_at,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=request.allowed_link_ids,
            )
        )

    def reset_stream(self, channel=None) -> None:
        reset = getattr(self._native_phy, "reset_stream", None)
        if reset is not None:
            reset(channel)

    @staticmethod
    def _configuration_error(message: str, allowed, **details):
        return internal_error(
            Layer.RUNTIME,
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            ErrorCategory.VALIDATION,
            message,
            details=Details.of(
                allowed_link_ids=tuple(sorted(allowed)),
                **details,
            ),
        )


__all__ = ("RuntimePhyRadioBridge",)
