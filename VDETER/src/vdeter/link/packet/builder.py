"""Link Profile 고정 용량 Packet builder.

모든 Message를 먼저 encode한 뒤 용량을 검증하므로 실패 시 부분 Packet을 반환하지 않는다.
Type 81은 caller 입력이 아니라 남은 Message 영역을 채우는 내부 padding으로만 생성한다.
"""

from __future__ import annotations

from ...common.contracts.message import (
    BODY_TYPE_TO_MESSAGE_TYPE,
    MessageBody,
    PaddingBody,
)
from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import LinkId
from ...common.results import InternalResult, Ok, internal_error
from ...profiles.link_phy import LinkProfileEntry
from .crc import crc_bytes
from .models import PacketBuildOutput

PADDING_BYTE = 0x51


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


def _lookup_profile(link_id: LinkId, link_profile_registry):
    if not getattr(link_profile_registry, "is_frozen", False):
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Link Profile registry가 freeze되지 않았습니다",
            link_id=link_id.value,
        )
    found = link_profile_registry.lookup(link_id.value)
    if found.is_err:
        return _error(
            ErrorCode.LINK_PROFILE_NOT_FOUND,
            ErrorCategory.UNSUPPORTED,
            "등록된 Link Profile이 없습니다",
            link_id=link_id.value,
        )
    profile = found.value
    if not isinstance(profile, LinkProfileEntry) or None in (
        profile.packet_capacity_bytes,
        profile.message_region_bytes,
        profile.crc_size_bytes,
        profile.padding_message_type,
        profile.max_messages_per_packet,
    ):
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Link Profile의 Packet field가 완결되지 않았습니다",
            link_id=link_id.value,
        )
    return Ok(profile)


def build_packet(
    link_id: LinkId,
    messages: tuple[MessageBody, ...],
    message_registry,
    link_profile_registry,
) -> InternalResult[PacketBuildOutput]:
    """caller 순서의 non-padding Message를 고정 크기 Packet 하나로 만든다."""

    if not isinstance(link_id, LinkId):
        return _error(
            ErrorCode.LINK_UNSUPPORTED_LINK_ID,
            ErrorCategory.VALIDATION,
            "LinkId가 필요합니다",
        )
    profile_result = _lookup_profile(link_id, link_profile_registry)
    if profile_result.is_err:
        return profile_result
    profile = profile_result.value

    if not isinstance(messages, tuple) or not messages:
        return _error(
            ErrorCode.LINK_PACKET_CONTENT_INVALID,
            ErrorCategory.VALIDATION,
            "Packet에는 non-padding Message가 하나 이상 필요합니다",
            link_id=link_id.value,
        )
    if len(messages) > profile.max_messages_per_packet:
        return _error(
            ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED,
            ErrorCategory.VALIDATION,
            "Packet의 최대 non-padding Message 수를 넘었습니다",
            link_id=link_id.value,
            message_count=len(messages),
            maximum_messages=profile.max_messages_per_packet,
        )
    if not getattr(message_registry, "is_frozen", False):
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Message Codec registry가 freeze되지 않았습니다",
        )

    encoded_messages: list[bytes] = []
    message_types: list[int] = []
    total_message_bytes = 0
    for index, body in enumerate(messages):
        if type(body) is PaddingBody:
            return _error(
                ErrorCode.LINK_PACKET_CONTENT_INVALID,
                ErrorCategory.VALIDATION,
                "Type 81 padding은 Packet builder만 생성할 수 있습니다",
                message_index=index,
            )
        message_type = BODY_TYPE_TO_MESSAGE_TYPE.get(type(body))
        if message_type is None:
            return _error(
                ErrorCode.LINK_UNSUPPORTED_MESSAGE_TYPE,
                ErrorCategory.UNSUPPORTED,
                "지원하지 않는 Message body type입니다",
                message_index=index,
            )
        codec_result = message_registry.lookup(message_type)
        if codec_result.is_err:
            return _error(
                ErrorCode.LINK_CODEC_NOT_REGISTERED,
                ErrorCategory.UNSUPPORTED,
                "Message Type codec이 등록되지 않았습니다",
                message_type=message_type,
                message_index=index,
            )
        encoded = codec_result.value.codec.encode(body)
        if encoded.is_err:
            return encoded
        total_message_bytes += len(encoded.value)
        if total_message_bytes > profile.message_region_bytes:
            return _error(
                ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED,
                ErrorCategory.VALIDATION,
                "Message들이 Link Profile Packet 용량을 넘습니다",
                link_id=link_id.value,
                message_index=index,
                encoded_bytes=total_message_bytes,
                capacity_bytes=profile.message_region_bytes,
            )
        encoded_messages.append(encoded.value)
        message_types.append(message_type)

    padding_size = profile.message_region_bytes - total_message_bytes
    message_region = b"".join(encoded_messages) + bytes((PADDING_BYTE,)) * padding_size
    checksum = crc_bytes(message_region)
    packet = message_region + checksum
    if len(packet) != profile.packet_capacity_bytes:
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.INTERNAL,
            "구성된 Packet 크기가 Link Profile과 일치하지 않습니다",
            link_id=link_id.value,
        )
    return Ok(PacketBuildOutput(packet, tuple(message_types), padding_size, checksum))
