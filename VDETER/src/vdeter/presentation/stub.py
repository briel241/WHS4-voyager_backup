"""단계 0 Presentation Stub (`stage0_baseline.md` §5.5).

이 구현은 interface 연결을 시험하기 위한 **test scope** stub이며 VDE-TER Presentation
codec이 아니다. 실제 VPFI payload format, R1027 Certificate Report, AIS 인증 payload와
전자해도 format은 단계 5 Presentation 기준선이 구현한다.

단계 0 test envelope
- `[VPFI 4 bytes big-endian][application payload]`
- 규범 형식이 아니며 운영 registry·profile에 등록하지 않는다.
- 기준 벡터나 protocol 적합성 증거로 사용하지 않는다.
- 다음 계층이 받을 최소 envelope일 뿐 VPFI payload format을 적용했다고 주장하지 않는다.
"""

from __future__ import annotations

from ..common.contracts.presentation import (
    ApplicationData,
    PresentationData,
    PresentationDecodeRequest,
    PresentationEncodeRequest,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import VdeterValidationError
from ..common.identifiers import Vpfi
from ..common.results import Ok, internal_error
from .registry import PayloadFormatRegistry

#: 단계 0 test envelope의 VPFI prefix 길이. 규범 형식이 아니다.
STAGE0_TEST_ENVELOPE_PREFIX_BYTES = 4


class StubPresentationAdapter:
    """test scope Presentation stub. payload bytes를 무손실로 보존한다."""

    __slots__ = ("_registry",)

    def __init__(self, registry: PayloadFormatRegistry) -> None:
        self._registry = registry

    def encode_payload(self, request: PresentationEncodeRequest):
        if not isinstance(request, PresentationEncodeRequest):
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PresentationEncodeRequest가 필요합니다",
            )
        correlation = Correlation(transfer_id=request.transfer_id)
        found = self._registry.lookup(request.payload_format)
        if found.is_err:
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 payload_format입니다",
                correlation=correlation,
                details=Details.of(payload_format=request.payload_format.value),
            )
        entry = found.value
        envelope = entry.vpfi.value.to_bytes(
            STAGE0_TEST_ENVELOPE_PREFIX_BYTES, "big"
        ) + request.application_payload
        return Ok(
            PresentationData(
                transfer_id=request.transfer_id,
                route=request.route,
                vpfi=entry.vpfi,
                data=envelope,
                message_format=entry.message_format,
                ack_required=request.ack_required,
            )
        )

    def decode_payload(self, request: PresentationDecodeRequest):
        if not isinstance(request, PresentationDecodeRequest):
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PresentationDecodeRequest가 필요합니다",
            )
        if len(request.data) < STAGE0_TEST_ENVELOPE_PREFIX_BYTES:
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "단계 0 test envelope 길이에 못 미칩니다",
                details=Details.of(data_size=len(request.data)),
            )
        raw_vpfi = int.from_bytes(request.data[:STAGE0_TEST_ENVELOPE_PREFIX_BYTES], "big")
        try:
            vpfi = Vpfi(raw_vpfi)
        except VdeterValidationError:
            return internal_error(
                Layer.PRESENTATION,
                ErrorCode.INVALID_VALUE,
                ErrorCategory.VALIDATION,
                "VPFI 범위를 벗어난 값입니다",
                details=Details.of(raw_vpfi=raw_vpfi),
            )
        found = self._registry.lookup_wire(vpfi, None)
        if found.is_err:
            return found
        entry = found.value
        return Ok(
            ApplicationData(
                route=request.route,
                payload_format=entry.payload_format,
                application_payload=request.data[STAGE0_TEST_ENVELOPE_PREFIX_BYTES:],
                received_at=request.received_at,
            )
        )
