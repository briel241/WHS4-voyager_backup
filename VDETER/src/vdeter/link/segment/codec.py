"""M.2092-2 unsigned VDE Segment Type 0 wire codec."""

from __future__ import annotations

from ...common.errors import Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import SequentialId
from ...common.results import InternalResult, Ok, internal_error
from .models import UnsignedVdeSegment

SEGMENT_HEADER_SIZE = 4
SEGMENT_TYPE_SHIFT = 14
SEGMENT_TYPE_MASK = 0x03
SEQUENTIAL_ID_MASK = 0x3FFF
MAX_PRESENTATION_SIZE = 0xFFFF


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


def encode_unsigned_segment(
    sequential_id: SequentialId,
    presentation_data: bytes,
) -> InternalResult[bytes]:
    """비어 있지 않은 application payload를 Segment Type 0으로 encode한다."""

    if not isinstance(sequential_id, SequentialId):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "SequentialId가 필요합니다",
        )
    if not isinstance(presentation_data, bytes):
        return _error(
            ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
            ErrorCategory.VALIDATION,
            "Presentation data는 bytes여야 합니다",
        )
    payload_size = len(presentation_data)
    if not 1 <= payload_size <= MAX_PRESENTATION_SIZE:
        return _error(
            ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
            ErrorCategory.VALIDATION,
            "송신 Presentation data는 1..65535 bytes여야 합니다",
            actual_bytes=payload_size,
            minimum_bytes=1,
            maximum_bytes=MAX_PRESENTATION_SIZE,
        )

    type_and_id = sequential_id.value
    header = type_and_id.to_bytes(2, "big") + payload_size.to_bytes(2, "big")
    return Ok(header + presentation_data)


def decode_segment(segment: bytes) -> InternalResult[UnsignedVdeSegment]:
    """완전한 VDE Segment를 검증하고 지원되는 Type 0만 반환한다."""

    if not isinstance(segment, bytes) or len(segment) < SEGMENT_HEADER_SIZE:
        return _error(
            ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
            ErrorCategory.PROTOCOL,
            "VDE Segment header가 잘렸습니다",
            actual_bytes=len(segment) if isinstance(segment, bytes) else -1,
            minimum_bytes=SEGMENT_HEADER_SIZE,
        )

    type_and_id = int.from_bytes(segment[:2], "big")
    segment_type = (type_and_id >> SEGMENT_TYPE_SHIFT) & SEGMENT_TYPE_MASK
    sequential_value = type_and_id & SEQUENTIAL_ID_MASK
    if segment_type != 0:
        segment_class = "SIGNED_UNSUPPORTED" if segment_type == 1 else "FUTURE_USE"
        return _error(
            ErrorCode.LINK_UNSUPPORTED_SEGMENT_TYPE,
            ErrorCategory.UNSUPPORTED,
            "지원하지 않는 VDE Segment Type입니다",
            segment_type=segment_type,
            segment_class=segment_class,
        )

    declared_size = int.from_bytes(segment[2:4], "big")
    actual_size = len(segment) - SEGMENT_HEADER_SIZE
    if actual_size != declared_size:
        return _error(
            ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
            ErrorCategory.PROTOCOL,
            "VDE Segment Payload Size와 실제 길이가 다릅니다",
            declared_bytes=declared_size,
            actual_bytes=actual_size,
        )

    return Ok(
        UnsignedVdeSegment(
            sequential_id=SequentialId(sequential_value),
            presentation_data=segment[SEGMENT_HEADER_SIZE:],
        )
    )
