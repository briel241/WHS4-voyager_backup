"""opaque binary test codec (`stage5_service_integration_baseline.md` §6.4).

Stage 0의 동결 4-byte big-endian test envelope 계약을 그대로 유지한다. TEST scope
전용이며 운영 registry에는 등록하지 않는다 (`DR-STAGE5-008`).

wire = `[0f ff ff ff][application bytes]`
"""

from __future__ import annotations

from ...common.errors import Correlation, ErrorCategory, ErrorCode
from ...common.results import Ok
from .base import (
    MAX_APPLICATION_BYTES,
    WireDiscriminator,
    decode_structure_error,
    encode_structure_error,
    presentation_error,
    truncated_error,
)

OPAQUE_CODEC_REF = "test.stage0.opaque_passthrough"
OPAQUE_TEST_VPFI = 268_435_455
OPAQUE_WIRE_PREFIX = OPAQUE_TEST_VPFI.to_bytes(4, "big")  # 0f ff ff ff
OPAQUE_PREFIX_BYTES = 4
OPAQUE_MIN_WIRE_BYTES = 5


class OpaqueTestCodec:
    """test scope opaque passthrough. application bytes를 해석하지 않는다."""

    __slots__ = ()

    codec_ref = OPAQUE_CODEC_REF
    vpfi = OPAQUE_TEST_VPFI
    message_format = None
    discriminator = WireDiscriminator(prefix=OPAQUE_WIRE_PREFIX)
    min_wire_length = OPAQUE_MIN_WIRE_BYTES
    max_application_bytes = MAX_APPLICATION_BYTES

    def encoded_size_bound(self, application_length: int):
        if not 1 <= application_length <= MAX_APPLICATION_BYTES:
            return encode_structure_error(
                "opaque application bytes는 1..65,531 bytes여야 합니다",
                payload_size=application_length,
                max_payload_bytes=MAX_APPLICATION_BYTES,
            )
        return Ok(OPAQUE_PREFIX_BYTES + application_length)

    def encode(self, application_payload: bytes, correlation: Correlation = Correlation()):
        bound = self.encoded_size_bound(len(application_payload))
        if bound.is_err:
            return bound
        return Ok(OPAQUE_WIRE_PREFIX + application_payload)

    def decode(self, wire: bytes, correlation: Correlation = Correlation()):
        if len(wire) < OPAQUE_MIN_WIRE_BYTES:
            return truncated_error(
                "opaque test envelope의 최소 전체 길이 5 bytes에 못 미칩니다",
                correlation=correlation,
                data_size=len(wire),
            )
        if wire[:OPAQUE_PREFIX_BYTES] != OPAQUE_WIRE_PREFIX:
            return presentation_error(
                ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
                ErrorCategory.UNSUPPORTED,
                "opaque test envelope prefix가 아닙니다",
                correlation=correlation,
            )
        application = wire[OPAQUE_PREFIX_BYTES:]
        if len(application) > MAX_APPLICATION_BYTES:
            return decode_structure_error(
                "opaque application bytes가 65,531 bytes를 넘습니다",
                correlation=correlation,
                payload_size=len(application),
            )
        return Ok(application)
