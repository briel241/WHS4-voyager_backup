"""Unsigned VDE Segment를 Type 92/93 Packet으로 결합하고 복원한다."""

from __future__ import annotations

from ...common.addressing import OriginalDestination, OriginalSource, RouteAddresses
from ...common.contracts.message import (
    DecodedMessage,
    ShortDataAckBody,
    ShortDataNoAckBody,
)
from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import LinkId, SequentialId
from ...common.results import InternalResult, Ok, internal_error
from ..packet.builder import build_packet
from ..packet.models import PacketBuildOutput
from ..segment.codec import decode_segment, encode_unsigned_segment


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


def build_short_data_packet(
    link_id: LinkId,
    route: RouteAddresses,
    ack_required: bool,
    retransmission_number: int | None,
    sequential_id: SequentialId,
    presentation_data: bytes,
    message_registry,
    link_profile_registry,
) -> InternalResult[PacketBuildOutput]:
    """Short Data 소유 경로에서 Type 92 또는 93 Packet 하나를 만든다."""

    if not isinstance(link_id, LinkId):
        return _error(
            ErrorCode.LINK_UNSUPPORTED_LINK_ID,
            ErrorCategory.VALIDATION,
            "LinkId가 필요합니다",
        )
    if not getattr(link_profile_registry, "is_frozen", False):
        return _error(
            ErrorCode.LINK_PROFILE_MISMATCH,
            ErrorCategory.VALIDATION,
            "Link Profile registry가 freeze되지 않았습니다",
            link_id=link_id.value,
        )
    profile = link_profile_registry.lookup(link_id.value)
    if profile.is_err:
        return _error(
            ErrorCode.LINK_PROFILE_NOT_FOUND,
            ErrorCategory.UNSUPPORTED,
            "등록된 Link Profile이 없습니다",
            link_id=link_id.value,
        )
    if not isinstance(route, RouteAddresses):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Short Data route가 필요합니다",
        )
    if not isinstance(ack_required, bool):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "ack_required는 bool이어야 합니다",
        )
    if not isinstance(sequential_id, SequentialId):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "SequentialId가 필요합니다",
        )

    segment = encode_unsigned_segment(sequential_id, presentation_data)
    if segment.is_err:
        return segment

    source_id = route.original_source.station_id
    destination = route.original_destination.address
    if source_id.value == 0:
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Short Data Source ID 0은 허용하지 않습니다",
        )
    if ack_required:
        if destination.is_broadcast:
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "broadcast Short Data는 ACK를 요청할 수 없습니다",
            )
        if (
            isinstance(retransmission_number, bool)
            or not isinstance(retransmission_number, int)
            or not 0 <= retransmission_number <= 254
        ):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "ACK Short Data에는 MAC의 retransmission number 0..254가 필요합니다",
            )
        body = ShortDataAckBody(
            source_id=source_id,
            session_id=0,
            destination=destination,
            retransmission_number=retransmission_number,
            segment=segment.value,
        )
    else:
        if retransmission_number is not None:
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "ACK 없는 Short Data에는 retransmission number를 지정하지 않습니다",
            )
        body = ShortDataNoAckBody(
            source_id=source_id,
            session_id=0,
            destination=destination,
            segment=segment.value,
        )

    return build_packet(link_id, (body,), message_registry, link_profile_registry)


def decode_short_data_message(message: DecodedMessage) -> InternalResult[DecodedMessage]:
    """Raw Type 92/93 Message의 Segment와 public decode envelope를 완성한다."""

    if not isinstance(message, DecodedMessage) or message.message_type not in (92, 93):
        return _error(
            ErrorCode.LINK_UNSUPPORTED_MESSAGE_TYPE,
            ErrorCategory.UNSUPPORTED,
            "Type 92/93 DecodedMessage가 필요합니다",
        )
    body = message.body
    if not isinstance(body, (ShortDataAckBody, ShortDataNoAckBody)):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Short Data message type과 body가 일치하지 않습니다",
            message_type=message.message_type,
        )

    segment = decode_segment(body.segment)
    if segment.is_err:
        return segment
    return Ok(
        DecodedMessage(
            message_type=message.message_type,
            length=message.length,
            route=RouteAddresses(
                original_source=OriginalSource(body.source_id),
                original_destination=OriginalDestination(body.destination),
            ),
            session_handle_candidate=None,
            sequential_id=segment.value.sequential_id,
            fragment=None,
            body=body,
            presentation_data=segment.value.presentation_data,
        )
    )
