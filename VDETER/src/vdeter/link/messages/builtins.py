"""Stage 1 승인 Message codec의 단일 명시 등록 지점."""

from __future__ import annotations

from ...common.results import Ok
from ...profiles.models import RegistryScope, SourceReference, SupportStatus
from .registry import MessageCodecEntry, create_message_registry
from .type_0 import Type0Codec
from .type_4 import Type4Codec
from .type_13 import Type13Codec
from .type_20 import Type20Codec
from .type_21 import Type21Codec
from .type_22 import Type22Codec
from .type_74 import Type74Codec
from .type_75 import Type75Codec
from .type_76 import Type76Codec
from .type_81 import Type81Codec
from .type_90 import Type90Codec
from .type_92 import Type92Codec
from .type_93 import Type93Codec


_BUILTINS = (
    (Type0Codec, "Table 36"),
    (Type4Codec, "Table 37"),
    (Type13Codec, "Table 38"),
    (Type20Codec, "Table 42"),
    (Type21Codec, "Table 45"),
    (Type22Codec, "Table 46"),
    (Type74Codec, "Table 49"),
    (Type75Codec, "Table 50"),
    (Type76Codec, "Table 51"),
    (Type81Codec, "§A4-4.9.13"),
    (Type90Codec, "Table 39"),
    (Type92Codec, "Table 40"),
    (Type93Codec, "Table 41"),
)


def register_builtin_codecs(registry) -> object:
    """운영 scope에만 승인된 13개 codec을 명시적으로 등록한다."""

    if registry.scope is RegistryScope.TEST:
        return Ok(registry.entry_count())
    for codec_type, location in _BUILTINS:
        codec = codec_type()
        result = registry.register(
            MessageCodecEntry(
                message_type=codec.message_type(),
                codec=codec,
                codec_ref=f"vdeter.link.messages.type_{codec.message_type()}.{codec_type.__name__}",
                dto_name=codec.dto_type().__name__,
                scope=RegistryScope.OPERATIONAL,
                support_status=SupportStatus.SUPPORTED,
                source=SourceReference(
                    document="ITU-R M.2092-2",
                    edition="02/2026",
                    location=location,
                ),
            )
        )
        if result.is_err:
            return result
    return Ok(registry.entry_count())


def build_message_registry(scope: RegistryScope, content_version: str = "stage1-link-0.5.3"):
    registry = create_message_registry(scope, content_version)
    result = register_builtin_codecs(registry)
    if result.is_err:
        return result
    return Ok(registry)
