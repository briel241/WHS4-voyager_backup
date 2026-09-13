"""Stage 5 registry-backed Presentation Adapter (`stage5_service_integration_baseline.md` §6,
`payload_extension_baseline.md` §8).

송신은 frozen registry의 `payload_format` lookup과 codec encode, 수신은 §6.1.1의
prefix-first discriminator로 정확히 하나의 wire key를 먼저 결정한 뒤 그 codec 하나만
호출한다. codec을 차례로 호출해 "먼저 decode되는 형식"을 고르는 trial decode는 하지
않는다 (`DR-STAGE5-020`).

수신 dispatch는 승인된 `(codec_ref, codec)` 선언 sequence와 active registry entry에서
build 시점에 만든 discriminator table로 결정한다. parser는 bounded header(최대 3 bytes)만
읽고 body 의미로 format을 추측하지 않으며, partial exact prefix는 truncated로, 어떤
pattern과도 일치하지 않는 header는 unknown으로 결정적으로 분류한다
(`payload_extension_baseline.md` §8.2).

Adapter는 VDE Segment, Sequential ID, fragment, Packet, CRC, ACK/NACK, Session, slot
또는 재전송을 생성·관리하지 않는다. SC-VDE MF 3 application ACK를 자동 생성하거나 Link
`ack_required`와 결합하지도 않는다 (`DR-PAYLOAD-008`).
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
from ..common.results import Err, Ok, internal_error
from ..profiles.models import RegistryScope
from ..profiles.payload_formats import (
    AIS_SCHEME1_CODEC_REF,
    CERTIFICATE_REPORT_CODEC_REF,
    OPAQUE_CODEC_REF,
    SC_VDE_ACK_CODEC_REF,
    SC_VDE_COMMAND_CODEC_REF,
    SC_VDE_POSITION_REPORT_CODEC_REF,
)
from ..security.dependencies import E2eSecurityDependencies
from .codecs.ais_scheme1 import AisAuthenticationScheme1Codec
from .codecs.base import MAX_ENCODED_BYTES, decode_structure_error, truncated_error
from .codecs.certificate_report import CertificateReportCodec
from .codecs.opaque import OpaqueTestCodec
from .codecs.sc_vde_position_report import ScVdePositionReportCodec
from .codecs.sc_vde_route_control import (
    ScVdeRouteControlAckCodec,
    ScVdeRouteControlCommandCodec,
)
from .registry import PayloadFormatRegistry

#: 승인된 `(codec_ref, codec)` 선언 sequence (`payload_extension_baseline.md` §8.1).
#: dict가 아니라 sequence로 선언해 build가 duplicate ref를 overwrite 전에 검출한다.
#: import side effect, decorator 자동 등록과 동적 이름 lookup은 사용하지 않는다.
_CODEC_DECLARATIONS = (
    (OPAQUE_CODEC_REF, OpaqueTestCodec()),
    (CERTIFICATE_REPORT_CODEC_REF, CertificateReportCodec()),
    (AIS_SCHEME1_CODEC_REF, AisAuthenticationScheme1Codec()),
    (SC_VDE_POSITION_REPORT_CODEC_REF, ScVdePositionReportCodec()),
    (SC_VDE_COMMAND_CODEC_REF, ScVdeRouteControlCommandCodec()),
    (SC_VDE_ACK_CODEC_REF, ScVdeRouteControlAckCodec()),
)


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


#: `allowed_ack_required` 미선언 codec의 기존 호환 기본값 (`payload_extension_baseline.md`
#: §8.1): Link `ack_required` 양 값을 모두 허용한다.
_DEFAULT_ALLOWED_ACK_REQUIRED = frozenset((False, True))


def _is_addressed_only(codec) -> bool:
    """payload-specific addressed-only 정책 선언 (`payload_extension_baseline.md` §6.4.4,
    §6.5.4). 미선언 codec은 기존 호환 기본값 `False`다."""

    return bool(getattr(codec, "addressed_only", False))


def _allowed_ack_required(codec) -> frozenset:
    """payload-specific Link ACK 정책 선언 (`payload_extension_baseline.md` §8.1)."""

    return getattr(codec, "allowed_ack_required", _DEFAULT_ALLOWED_ACK_REQUIRED)


def _validated_encoded_size_bound(
    codec,
    application_length: int,
    *,
    correlation: Correlation = Correlation(),
    payload_format_value: str = "",
):
    """format별 length/bound의 공용 방어 검증 (`payload_extension_baseline.md` §8.1).

    Service Phase A가 호출하는 공개 `encoded_size_bound()`와 direct encode preflight가
    같은 판정을 공유하도록 이 helper 하나만 사용한다. codec bound의 `Err`는 그대로
    반환하고, `Ok` 값은 `bool`이 아닌 exact `int`이며 1..65,535 범위여야 한다.
    `InternalResult`가 아니거나 exact int가 아닌 malformed 반환은 사용자 입력 오류가
    아니라 codec 계약 위반이므로 `INTERNAL_ERROR/INTERNAL`로 처리한다.
    """

    bound = codec.encoded_size_bound(application_length)
    if not isinstance(bound, (Ok, Err)):
        return _error(
            ErrorCode.INTERNAL_ERROR,
            ErrorCategory.INTERNAL,
            "codec encoded_size_bound가 InternalResult를 반환하지 않았습니다",
            correlation=correlation,
            payload_format=payload_format_value,
            codec_ref=codec.codec_ref,
        )
    if bound.is_err:
        return bound
    value = bound.value
    if type(value) is not int:
        return _error(
            ErrorCode.INTERNAL_ERROR,
            ErrorCategory.INTERNAL,
            "codec encoded_size_bound의 Ok 값이 exact int가 아닙니다",
            correlation=correlation,
            payload_format=payload_format_value,
            codec_ref=codec.codec_ref,
        )
    if not 1 <= value <= MAX_ENCODED_BYTES:
        return _error(
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "encoded 크기 상한이 1..65,535 bytes 범위를 벗어났습니다",
            correlation=correlation,
            payload_format=payload_format_value,
            payload_size=value,
        )
    return Ok(value)


class RegistryPresentationAdapter:
    """frozen registry snapshot 하나로 송수신을 함께 처리하는 실제 Presentation Adapter."""

    __slots__ = ("_registry", "_codecs", "_fingerprint", "_dispatch_table", "_e2e_security")

    def __init__(
        self,
        registry: PayloadFormatRegistry,
        codecs: dict,
        fingerprint: str,
        dispatch_table: tuple,
        e2e_security: E2eSecurityDependencies,
    ) -> None:
        self._registry = registry
        self._codecs = codecs
        self._fingerprint = fingerprint
        self._dispatch_table = dispatch_table
        self._e2e_security = e2e_security

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

    def encoded_size_bound(self, payload_format, application_length: int):
        """format descriptor의 결정적 encoded 크기 상한 (`stage5` §5.4 Phase A).

        direct encode preflight와 같은 `_validated_encoded_size_bound` helper를
        사용한다 (`payload_extension_baseline.md` §8.1).
        """

        found = self._registry.lookup(payload_format)
        if found.is_err:
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 payload_format입니다",
                payload_format=payload_format.value,
            )
        codec = self._codecs[found.value.codec_ref]
        bound = _validated_encoded_size_bound(
            codec,
            application_length,
            payload_format_value=payload_format.value,
        )
        if bound.is_err:
            return bound
        final = bound.value + self._e2e_security.presentation_security.encoded_overhead(found.value)
        if final > MAX_ENCODED_BYTES:
            return _error(ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION,
                          "보안 Presentation 크기가 65,535 bytes를 넘습니다")
        return Ok(final)

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
        # §6.5.13/§8.1 오류 우선순위: registry lookup 다음은 format별 length/bound다.
        # Service Phase A가 같은 bound를 이미 확인했더라도 direct Presentation 호출과
        # 구현 편차에서 우선순위를 보존하기 위한 방어적 재검증이므로 생략하지 않는다.
        bound = _validated_encoded_size_bound(
            codec,
            len(request.application_payload),
            correlation=correlation,
            payload_format_value=request.payload_format.value,
        )
        if bound.is_err:
            return bound
        # §6.4.4/§6.5.4: addressed 전용 format의 송신 broadcast는 codec encode 전에
        # 거부해 Phase C commit과 하위 작업을 남기지 않는다. route와 ACK가 동시에
        # 위반되면 route 오류가 우선한다 (§8.1).
        if (
            _is_addressed_only(codec)
            and request.route.original_destination.address.is_broadcast
        ):
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "addressed 전용 payload format은 broadcast 송신을 지원하지 않습니다",
                correlation=correlation,
                payload_format=request.payload_format.value,
            )
        # §8.1: format별 Link ACK 정책. payload key가 아니라 codec metadata로 판정한다.
        if request.ack_required not in _allowed_ack_required(codec):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "이 payload format이 허용하지 않는 ack_required 값입니다",
                correlation=correlation,
                payload_format=request.payload_format.value,
                ack_required=request.ack_required,
            )
        encoded = codec.encode(request.application_payload, correlation)
        if encoded.is_err:
            return encoded
        wire = encoded.value
        # §8.1: 실제 wire 길이가 사전 bound와 정확히 같고 같은 범위인지 재확인한다
        # (Stage 5 Phase C와 동일 판정).
        if len(wire) != bound.value or not 1 <= len(wire) <= MAX_ENCODED_BYTES:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "encode 결과 길이가 사전 bound 또는 1..65,535 bytes 범위와 다릅니다",
                correlation=correlation,
                payload_format=request.payload_format.value,
                payload_size=len(wire),
                expected_size=bound.value,
            )
        secured = self._e2e_security.presentation_security.protect(
            entry=entry, codec=codec, plain_wire=wire, route=request.route,
        )
        if secured.is_err:
            return secured
        wire = secured.value
        if len(wire) != bound.value + self._e2e_security.presentation_security.encoded_overhead(entry):
            return _error(ErrorCode.PRESENTATION_STRUCTURE_INVALID, ErrorCategory.PROTOCOL,
                          "보안 Presentation 결과 길이가 예상과 다릅니다")
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
        # §6.4.4: addressed 전용 format의 알려진 wire가 broadcast route로 들어오면
        # protocol 구조 오류로 거부하고 `DATA_RECEIVED`를 만들지 않는다.
        if (
            _is_addressed_only(codec)
            and request.route.original_destination.address.is_broadcast
        ):
            return decode_structure_error(
                "addressed 전용 payload wire가 broadcast route로 수신되었습니다",
                payload_format=entry.payload_format.value,
            )
        plain = self._e2e_security.presentation_security.unprotect(
            entry=entry, codec=codec, secure_wire=data, route=request.route,
        )
        if plain.is_err:
            return plain
        application = codec.decode(plain.value)
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

    # --- §6.1.1 prefix-first discriminator (build-time table 기반) ---

    def _dispatch_wire_key(self, data: bytes):
        """정확히 하나의 wire key를 결정한다. codec body 검증은 선택된 codec만 수행한다.

        table의 discriminator는 build 시점에 pairwise disjoint가 증명됐으므로 완전
        prefix match는 최대 하나다. bounded header만 비교하며 body 의미는 시험하지
        않는다.
        """

        partial_prefix = False
        mask_mismatch = None
        for entry, discriminator in self._dispatch_table:
            prefix = discriminator.prefix
            if len(data) >= len(prefix):
                if data[: len(prefix)] != prefix:
                    continue
                if discriminator.third_byte_mask:
                    if len(data) < 3:
                        return truncated_error(
                            "Message format bits 앞에서 wire가 잘렸습니다",
                            data_size=len(data),
                        )
                    if (
                        data[2] & discriminator.third_byte_mask
                    ) != discriminator.third_byte_value:
                        mask_mismatch = (prefix, data[2])
                        continue
                return Ok(entry)
            if data == prefix[: len(data)]:
                partial_prefix = True
        if partial_prefix:
            return truncated_error(
                "등록된 wire prefix가 완성되기 전에 잘렸습니다", data_size=len(data)
            )
        if mask_mismatch is not None:
            prefix, third_byte = mask_mismatch
            return _error(
                ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
                ErrorCategory.UNSUPPORTED,
                "알려진 VPFI의 등록되지 않은 Message format입니다",
                prefix=prefix.hex(),
                third_byte=third_byte,
            )
        return _error(
            ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY,
            ErrorCategory.UNSUPPORTED,
            "활성 scope의 어떤 wire pattern과도 일치하지 않습니다",
        )


def build_presentation_adapter(registry: PayloadFormatRegistry, *, e2e_security: E2eSecurityDependencies):
    """frozen registry에서 실제 Presentation Adapter를 만든다.

    build는 registry freeze 뒤 다음을 모두 검사한다 (`payload_extension_baseline.md`
    §8.1).

    1. codec 선언 sequence의 ref가 dict materialize 전에 고유하고, 모든 supported
       entry의 `codec_ref`가 알려져 있으며 active registry entry 사이에서도 고유함
    2. active registry의 `(vpfi, message_format)` wire key가 고유함
    3. entry의 VPFI/MF metadata와 codec metadata가 exact 일치
    4. 모든 discriminator가 pairwise disjoint (`stage5` §6.1.1-6)
    5. discriminator가 요구하는 최소 header 길이와 codec 최소 wire 길이가 모순되지 않음
    6. format별 최대 wire 길이가 65,535 이하
    7. `addressed_only`를 선언한 codec의 값 type이 정확히 `bool`
    8. `allowed_ack_required`를 선언한 codec의 값이 비어 있지 않은 `frozenset`이고
       원소가 정확히 `bool`의 부분집합
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
    if not isinstance(e2e_security, E2eSecurityDependencies):
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, ErrorCategory.VALIDATION,
                      "명시적 E2E security dependency가 필요합니다")
    # 검사 1 전반부: 선언 sequence의 duplicate ref를 dict 생성 전에 거부한다.
    declared = {}
    for codec_ref, codec in _CODEC_DECLARATIONS:
        if codec_ref in declared:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec 선언 sequence에 중복된 codec_ref가 있습니다",
                codec_ref=codec_ref,
            )
        if codec.codec_ref != codec_ref:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec 선언 ref와 codec metadata ref가 일치하지 않습니다",
                codec_ref=codec_ref,
                metadata_ref=codec.codec_ref,
            )
        # 검사 7: `addressed_only`를 선언하면 type이 정확히 bool이어야 한다.
        if hasattr(codec, "addressed_only") and type(codec.addressed_only) is not bool:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec addressed_only 정책은 bool이어야 합니다",
                codec_ref=codec_ref,
            )
        # 검사 8: `allowed_ack_required`를 선언하면 비어 있지 않은 frozenset이고 원소가
        # 정확히 bool의 부분집합이어야 한다 (mutable set 선언 거부 포함).
        if hasattr(codec, "allowed_ack_required"):
            allowed_ack = codec.allowed_ack_required
            if (
                not isinstance(allowed_ack, frozenset)
                or not allowed_ack
                or any(type(value) is not bool for value in allowed_ack)
            ):
                return _error(
                    ErrorCode.INVALID_REQUEST,
                    ErrorCategory.VALIDATION,
                    "codec allowed_ack_required 정책은 비어 있지 않은 bool frozenset이어야"
                    " 합니다",
                    codec_ref=codec_ref,
                )
        declared[codec_ref] = codec

    codecs = {}
    resolved = []
    seen_entry_refs = set()
    seen_wire_keys = set()
    for entry in registry.registry.entries():
        # 검사 1 후반부: active entry 사이 codec_ref 고유성.
        if entry.codec_ref in seen_entry_refs:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec_ref가 active registry entry 사이에서 재사용되었습니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        seen_entry_refs.add(entry.codec_ref)
        # 검사 2: wire key 고유성. discriminator가 우연히 disjoint해도 `lookup_wire()`가
        # 모호해지므로 별도 시작 실패다.
        wire_key = entry.wire_key()
        if wire_key in seen_wire_keys:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "(vpfi, message_format) wire key가 중복 등록되었습니다",
                payload_format=entry.payload_format.value,
                vpfi=entry.vpfi.value,
                message_format=entry.message_format,
            )
        seen_wire_keys.add(wire_key)
        codec = declared.get(entry.codec_ref)
        if codec is None:
            return _error(
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "승인되지 않은 codec_ref입니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        # 검사 3: entry와 codec의 wire metadata exact 일치.
        if codec.vpfi != entry.vpfi.value or codec.message_format != entry.message_format:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "registry entry의 wire metadata가 codec과 일치하지 않습니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        # 검사 5: discriminator가 요구하는 bounded header와 codec 최소 wire 길이.
        discriminator = codec.discriminator
        required_header = 3 if discriminator.third_byte_mask else len(discriminator.prefix)
        if codec.min_wire_length < required_header:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec 최소 wire 길이가 discriminator header보다 짧습니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        # 검사 6: format별 최대 wire 길이 상한.
        if getattr(codec, "max_wire_bytes", MAX_ENCODED_BYTES) > MAX_ENCODED_BYTES:
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "codec 최대 wire 길이가 65,535 bytes를 넘습니다",
                payload_format=entry.payload_format.value,
                codec_ref=entry.codec_ref,
            )
        codecs[entry.codec_ref] = codec
        resolved.append((entry, codec))
    # 검사 4: 모든 discriminator의 pairwise disjointness.
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
    # 등록 순서와 무관한 결정적 dispatch 순서를 고정한다.
    try:
        e2e_security.presentation_security.policy.validate_registry(registry, codecs)
    except ValueError:
        return _error(ErrorCode.CONFIG_SEMANTIC_INVALID, ErrorCategory.VALIDATION,
                      "Security policy와 active registry가 일치하지 않습니다")
    dispatch_table = tuple(
        sorted(
            ((entry, codec.discriminator) for entry, codec in resolved),
            key=lambda item: (
                item[1].prefix,
                item[1].third_byte_mask,
                item[1].third_byte_value,
            ),
        )
    )
    return Ok(
        RegistryPresentationAdapter(
            registry, codecs, registry.fingerprint().fingerprint, dispatch_table,
            e2e_security,
        )
    )
