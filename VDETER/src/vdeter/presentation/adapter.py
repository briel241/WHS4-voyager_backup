"""Stage 5 registry-backed Presentation Adapter (`stage5_service_integration_baseline.md` §6).

송신은 frozen registry의 `payload_format` lookup과 codec encode, 수신은 §6.1.1의
prefix-first discriminator로 정확히 하나의 wire key를 먼저 결정한 뒤 그 codec 하나만
호출한다. codec을 차례로 호출해 "먼저 decode되는 형식"을 고르는 trial decode는 하지
않는다 (`DR-STAGE5-020`).

Adapter는 VDE Segment, Sequential ID, fragment, Packet, CRC, ACK/NACK, Session, slot
또는 재전송을 생성·관리하지 않는다.
"""

from __future__ import annotations

from ..common.contracts.presentation import (
    ApplicationData,
    PresentationData,
    PresentationDecodeRequest,
    PresentationEncodeRequest,
)
from ..common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    Layer,
)
from ..common.identifiers import PayloadFormat, Vpfi
from ..common.results import Ok, internal_error
from ..profiles.models import RegistryScope
from ..profiles.payload_formats import (
    AIS_SCHEME1_CODEC_REF,
    CERTIFICATE_REPORT_CODEC_REF,
    OPAQUE_CODEC_REF,
    TEST_PAYLOAD_FORMAT,
)
from .codecs.ais_scheme1 import (
    AIS_SCHEME1_MESSAGE_FORMAT,
    AIS_SCHEME1_WIRE_PREFIX,
    AisAuthenticationScheme1Codec,
)
from .codecs.base import MAX_ENCODED_BYTES, truncated_error
from .codecs.certificate_report import (
    CERTIFICATE_REPORT_WIRE_PREFIX,
    CertificateReportCodec,
)
from .codecs.opaque import OPAQUE_WIRE_PREFIX, OpaqueTestCodec
from .registry import PayloadFormatRegistry

#: 승인된 `codec_ref` -> codec 구현. registry entry는 이 table로만 codec을 얻는다.
_CODEC_BY_REF = {
    OPAQUE_CODEC_REF: OpaqueTestCodec(),
    CERTIFICATE_REPORT_CODEC_REF: CertificateReportCodec(),
    AIS_SCHEME1_CODEC_REF: AisAuthenticationScheme1Codec(),
}


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    correlation: Correlation = Correlation(),
    **details,
):
    return internal_error(
        Layer.PRESENTATION,
        code,
        category,
        message,
        correlation=correlation,
        details=Details.of(**details) if details else Details(),
    )


class RegistryPresentationAdapter:
    """frozen registry snapshot 하나로 송수신을 함께 처리하는 실제 Presentation Adapter."""

    __slots__ = ("_registry", "_codecs", "_fingerprint")

    def __init__(self, registry: PayloadFormatRegistry, codecs: dict, fingerprint: str) -> None:
        self._registry = registry
        self._codecs = codecs
        self._fingerprint = fingerprint

    # --- 관찰 ---

    @property
    def registry(self) -> PayloadFormatRegistry:
        return self._registry

    @property
    def scope(self) -> RegistryScope:
        return self._registry.scope

    def registry_fingerprint(self) -> str:
        """freeze 시점에 고정한 registry 지문. Phase C 재검증에 사용한다."""

        return self._fingerprint

    # --- Service pre-admission 지원 ---

    def encoded_size_bound(self, payload_format: PayloadFormat, application_length: int):
        """format descriptor의 결정적 encoded 크기 상한 (`stage5` §5.4 Phase A)."""

        found = self._registry.lookup(payload_format)
        if found.is_err:
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 payload_format입니다",
                payload_format=payload_format.value,
            )
        codec = self._codecs[found.value.codec_ref]
        return codec.encoded_size_bound(application_length)

    # --- PresentationPort ---

    def encode_payload(self, request: PresentationEncodeRequest):
        if not isinstance(request, PresentationEncodeRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PresentationEncodeRequest가 필요합니다",
            )
        correlation = Correlation(transfer_id=request.transfer_id)
        found = self._registry.lookup(request.payload_format)
        if found.is_err:
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 payload_format입니다",
                correlation=correlation,
                payload_format=request.payload_format.value,
            )
        entry = found.value
        codec = self._codecs[entry.codec_ref]
        encoded = codec.encode(request.application_payload, correlation)
        if encoded.is_err:
            return encoded
        wire = encoded.value
        if not 1 <= len(wire) <= MAX_ENCODED_BYTES:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "encoded Presentation data가 1..65,535 bytes 상한을 벗어났습니다",
                correlation=correlation,
                payload_format=request.payload_format.value,
                payload_size=len(wire),
            )
        return Ok(
            PresentationData(
                transfer_id=request.transfer_id,
                route=request.route,
                vpfi=entry.vpfi,
                data=wire,
                message_format=entry.message_format,
                ack_required=request.ack_required,
            )
        )

    def decode_payload(self, request: PresentationDecodeRequest):
        if not isinstance(request, PresentationDecodeRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PresentationDecodeRequest가 필요합니다",
            )
        data = request.data
        # §6.1.1 step 1: 최소 2 bytes.
        if not isinstance(data, bytes) or len(data) < 2:
            return truncated_error(
                "Presentation data는 최소 2 bytes가 필요합니다", data_size=len(data)
            )
        dispatched = self._dispatch_wire_key(data)
        if dispatched.is_err:
            return dispatched
        entry = dispatched.value
        codec = self._codecs[entry.codec_ref]
        application = codec.decode(data)
        if application.is_err:
            return application
        return Ok(
            ApplicationData(
                route=request.route,
                payload_format=entry.payload_format,
                application_payload=application.value,
                received_at=request.received_at,
            )
        )

    # --- §6.1.1 prefix-first discriminator ---

    def _dispatch_wire_key(self, data: bytes):
        """정확히 하나의 wire key를 결정한다. codec body 검증은 선택된 codec만 수행한다."""

        # step 2: TEST scope의 opaque prefix.
        if self.scope is RegistryScope.TEST and TEST_PAYLOAD_FORMAT in {
            entry.payload_format.value for entry in self._registry.registry.entries()
        }:
            if data[:4] == OPAQUE_WIRE_PREFIX:
                return self._entry_for_wire(Vpfi(OpaqueTestCodec.vpfi), None)
            if len(data) < 4 and data == OPAQUE_WIRE_PREFIX[: len(data)]:
                return truncated_error(
                    "opaque test envelope prefix가 잘렸습니다", data_size=len(data)
                )
        # step 3: byte-aligned VPFI 0 Certificate Report.
        if data[:2] == CERTIFICATE_REPORT_WIRE_PREFIX:
            return self._entry_for_wire(Vpfi(0), 1)
        # step 4·5: 16-bit VPFI 7 후보.
        if data[:2] == AIS_SCHEME1_WIRE_PREFIX:
            if len(data) < 3:
                return truncated_error(
                    "VPFI 7 message가 Message format bits 앞에서 잘렸습니다",
                    data_size=len(data),
                )
            message_format = (data[2] >> 2) & 0x3F
            if message_format == AIS_SCHEME1_MESSAGE_FORMAT:
                return self._entry_for_wire(Vpfi(7), AIS_SCHEME1_MESSAGE_FORMAT)
            return _error(
                ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 VPFI 7 Message format입니다",
                message_format=message_format,
            )
        return _error(
            ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
            ErrorCategory.UNSUPPORTED,
            "활성 scope의 어떤 wire pattern과도 일치하지 않습니다",
        )

    def _entry_for_wire(self, vpfi: Vpfi, message_format: int | None):
        found = self._registry.lookup_wire(vpfi, message_format)
        if found.is_err:
            return _error(
                ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
                ErrorCategory.UNSUPPORTED,
                "활성 scope registry에 등록되지 않은 wire key입니다",
                vpfi=vpfi.value,
                message_format=message_format,
            )
        return found


def build_presentation_adapter(registry: PayloadFormatRegistry):
    """frozen registry에서 실제 Presentation Adapter를 만든다.

    - registry는 freeze 상태여야 한다.
    - 모든 entry는 승인된 codec_ref로 해석돼야 하며 codec wire metadata와 일치해야 한다.
    - 모든 prefix pattern의 pairwise disjointness를 검사한다 (`stage5` §6.1.1-6).
    """

    if not isinstance(registry, PayloadFormatRegistry):
        return _error(
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "PayloadFormatRegistry가 필요합니다",
        )
    if not registry.is_frozen:
        return _error(
            ErrorCode.REGISTRY_NOT_FROZEN,
            ErrorCategory.VALIDATION,
            "Presentation Adapter는 frozen registry에서만 만들 수 있습니다",
        )
    codecs = {}
    resolved = []
    for entry in registry.registry.entries():
        codec = _CODEC_BY_REF.get(entry.codec_ref)
        if codec is None:
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "승인되지 않은 codec_ref입니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        if codec.vpfi != entry.vpfi.value or codec.message_format != entry.message_format:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "registry entry의 wire metadata가 codec과 일치하지 않습니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        codecs[entry.codec_ref] = codec
        resolved.append((entry, codec))
    for index, (first_entry, first_codec) in enumerate(resolved):
        for second_entry, second_codec in resolved[index + 1 :]:
            if first_codec.discriminator.conflicts_with(second_codec.discriminator):
                return _error(
                    ErrorCode.PRESENTATION_DISCRIMINATOR_CONFLICT,
                    ErrorCategory.VALIDATION,
                    "같은 bytes가 둘 이상의 codec 후보가 되는 registry 구성입니다",
                    first_format=first_entry.payload_format.value,
                    second_format=second_entry.payload_format.value,
                )
    return Ok(
        RegistryPresentationAdapter(
            registry, codecs, registry.fingerprint().fingerprint
        )
    )
