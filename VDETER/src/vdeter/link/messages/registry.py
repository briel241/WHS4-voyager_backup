"""Message Registry (`stage0_baseline.md` §5.4, §7).

Link가 소유하는 Message Type → codec registry다. 명시적 등록, 검증, freeze, 조회의
생명주기를 따르며 mutable module dictionary와 import side effect 등록을 사용하지 않는다.

단계 0에서는 어떤 운영 codec도 등록하지 않는다. Type 0, 4, 13, 20, 21, 22, 74, 75, 76,
81, 90, 92, 93의 실제 codec은 단계 1 Link 기준선에서 규범 표와 대조해 등록한다.

이전 구현의 mutable `MESSAGE_CODECS` dictionary는 decision record `DR-STAGE0-002`에 따라
비운영 보관 경로(`legacy/temporary_bpsk/message_codecs.py`)로 옮겼다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.exceptions import InvalidValueError
from ...common.results import Ok, internal_error
from ...profiles.models import (
    ExplicitRegistry,
    RegistryScope,
    SourceReference,
    SupportStatus,
)
from .interface import MessageCodec

MESSAGE_REGISTRY_NAME = "message_codec"


@dataclass(frozen=True, slots=True)
class MessageCodecEntry:
    """Message Type 하나의 codec 등록 항목.

    `codec`은 portable DTO가 아니므로 registry 지문에는 `codec_ref` 문자열만 사용한다.
    """

    message_type: int
    codec: object
    codec_ref: str
    dto_name: str
    scope: RegistryScope
    support_status: SupportStatus
    source: SourceReference

    def __post_init__(self) -> None:
        if isinstance(self.message_type, bool) or not isinstance(self.message_type, int):
            raise InvalidValueError("MessageCodecEntry.message_type: 정수여야 합니다")
        if not 0 <= self.message_type <= 255:
            raise InvalidValueError("MessageCodecEntry.message_type: 0..255여야 합니다")
        if not isinstance(self.codec, MessageCodec):
            raise InvalidValueError("MessageCodecEntry.codec: MessageCodec 계약을 만족해야 합니다")
        for name in ("codec_ref", "dto_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InvalidValueError(f"MessageCodecEntry.{name}: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("MessageCodecEntry.scope: RegistryScope여야 합니다")
        if not isinstance(self.support_status, SupportStatus):
            raise InvalidValueError("MessageCodecEntry.support_status: SupportStatus여야 합니다")
        if not isinstance(self.source, SourceReference):
            raise InvalidValueError("MessageCodecEntry.source: SourceReference여야 합니다")
        if self.codec.message_type() != self.message_type:
            raise InvalidValueError("MessageCodecEntry.message_type과 codec이 일치하지 않습니다")
        if self.codec.dto_type().__name__ != self.dto_name:
            raise InvalidValueError("MessageCodecEntry.dto_name과 codec DTO가 일치하지 않습니다")

    def registry_key(self) -> int:
        return self.message_type

    def fingerprint_content(self) -> dict:
        """지문 대상 자료. codec 객체는 포함하지 않는다."""

        return {
            "message_type": self.message_type,
            "codec_ref": self.codec_ref,
            "dto_name": self.dto_name,
            "scope": self.scope.value,
            "support_status": self.support_status.value,
            "source": {
                "document": self.source.document,
                "edition": self.source.edition,
                "location": self.source.location,
            },
        }


def create_message_registry(
    scope: RegistryScope, content_version: str = "stage0"
) -> ExplicitRegistry:
    """빈 Message Registry를 만든다. 등록은 `builtins.register_builtin_codecs`가 한다."""

    return ExplicitRegistry(MESSAGE_REGISTRY_NAME, scope, content_version, MessageCodecEntry)


def validate_message_registry_alignment(profile_registry, codec_registry):
    """Message metadata와 codec key/DTO/source 표가 정확히 교차 대응하는지 검사한다."""

    if profile_registry.scope is not codec_registry.scope:
        return internal_error(
            Layer.LINK,
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Message profile/codec registry scope가 다릅니다",
        )
    profile_keys = profile_registry.keys()
    codec_keys = codec_registry.keys()
    if profile_keys != codec_keys:
        return internal_error(
            Layer.LINK,
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Message profile과 codec key가 정확히 일치하지 않습니다",
            details=Details.of(
                profile_types=tuple(str(item) for item in profile_keys),
                codec_types=tuple(str(item) for item in codec_keys),
            ),
        )
    for message_type in profile_keys:
        profile = next(item for item in profile_registry.entries() if item.message_type == message_type)
        codec = next(item for item in codec_registry.entries() if item.message_type == message_type)
        if profile.description != codec.dto_name or profile.source.location != codec.source.location:
            return internal_error(
                Layer.LINK,
                ErrorCode.LINK_PROFILE_MISMATCH,
                ErrorCategory.VALIDATION,
                "Message profile과 codec metadata가 일치하지 않습니다",
                details=Details.of(message_type=message_type),
            )
    return Ok(len(profile_keys))
