"""CRC 선검증과 Message 경계 Type 81 규칙을 적용하는 Packet parser."""

from __future__ import annotations

from ...common.contracts.message import DecodedMessage, FragmentRef
from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import LinkId
from ...common.results import InternalResult, Ok, internal_error
from ...profiles.link_phy import LinkProfileEntry
from .crc import CRC32_SIZE_BYTES, crc_bytes
from .models import PacketParseOutput

PADDING_BYTE = 0x51
MESSAGE_PREFIX_SIZE = 3


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


def parse_packet(
    link_id: LinkId,
    packet: bytes,
    message_registry,
    link_profile_registry,
) -> InternalResult[PacketParseOutput]:
    """Packet 전체를 원자적으로 검증하고 non-padding Message만 반환한다."""

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

    if not isinstance(packet, bytes) or len(packet) != profile.packet_capacity_bytes:
        return _error(
            ErrorCode.LINK_PACKET_LENGTH_INVALID,
            ErrorCategory.PROTOCOL,
            "Packet 길이가 Link Profile의 고정 크기와 다릅니다",
            link_id=link_id.value,
            actual_bytes=len(packet) if isinstance(packet, bytes) else -1,
            expected_bytes=profile.packet_capacity_bytes,
        )
    message_region = packet[:-CRC32_SIZE_BYTES]
    received_crc = packet[-CRC32_SIZE_BYTES:]
    expected_crc = crc_bytes(message_region)
    if received_crc != expected_crc:
        return _error(
            ErrorCode.LINK_CRC_MISMATCH,
            ErrorCategory.PROTOCOL,
            "Packet CRC가 일치하지 않습니다",
            link_id=link_id.value,
            expected_crc=expected_crc.hex(),
            actual_crc=received_crc.hex(),
        )
    if not getattr(message_registry, "is_frozen", False):
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Message Codec registry가 freeze되지 않았습니다",
        )

    frames: list[tuple[int, int, int]] = []
    offset = 0
    padding_size = 0
    while offset < len(message_region):
        if message_region[offset] == PADDING_BYTE:
            padding = message_region[offset:]
            if any(value != PADDING_BYTE for value in padding):
                return _error(
                    ErrorCode.LINK_PADDING_INVALID,
                    ErrorCategory.PROTOCOL,
                    "첫 Type 81 뒤 Message 영역은 모두 0x51이어야 합니다",
                    byte_offset=offset,
                )
            padding_size = len(padding)
            break

        remaining = len(message_region) - offset
        if remaining < MESSAGE_PREFIX_SIZE:
            return _error(
                ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
                ErrorCategory.PROTOCOL,
                "Message Type/Length prefix가 잘렸습니다",
                byte_offset=offset,
            )
        message_type = message_region[offset]
        length = int.from_bytes(message_region[offset + 1 : offset + 3], "big")
        if length < MESSAGE_PREFIX_SIZE or length > remaining:
            return _error(
                ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
                ErrorCategory.PROTOCOL,
                "Message Length가 현재 Message 영역 경계를 벗어났습니다",
                message_type=message_type,
                message_index=len(frames),
                byte_offset=offset,
            )
        frames.append((message_type, length, offset))
        if len(frames) > profile.max_messages_per_packet:
            return _error(
                ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED,
                ErrorCategory.PROTOCOL,
                "Packet의 최대 non-padding Message 수를 넘었습니다",
                link_id=link_id.value,
                message_count=len(frames),
                maximum_messages=profile.max_messages_per_packet,
            )
        offset += length

    if not frames:
        return _error(
            ErrorCode.LINK_PACKET_CONTENT_INVALID,
            ErrorCategory.PROTOCOL,
            "all-padding 또는 빈 Message Packet은 허용하지 않습니다",
            link_id=link_id.value,
        )

    # framing과 전체 padding이 정상임을 확인한 뒤에만 Message field를 decode한다.
    messages: list[DecodedMessage] = []
    for message_index, (message_type, length, byte_offset) in enumerate(frames):
        codec_result = message_registry.lookup(message_type)
        if codec_result.is_err:
            return _error(
                ErrorCode.LINK_CODEC_NOT_REGISTERED,
                ErrorCategory.UNSUPPORTED,
                "Message Type codec이 등록되지 않았습니다",
                message_type=message_type,
                message_index=message_index,
                byte_offset=byte_offset,
            )
        decoded = codec_result.value.codec.decode(
            message_region[byte_offset : byte_offset + length]
        )
        if decoded.is_err:
            return decoded
        messages.append(
            DecodedMessage(
                message_type=message_type,
                length=length,
                fragment=(
                    FragmentRef(index=decoded.value.global_fragment_number, count=None)
                    if message_type in (74, 75, 76)
                    else None
                ),
                body=decoded.value,
            )
        )
    return Ok(PacketParseOutput(tuple(messages), padding_size, received_crc))
