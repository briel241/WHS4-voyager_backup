"""Service projection (`stage0_baseline.md` §6.3, §10).

Runtime은 내부 오류 code를 공개 code로 명시적으로 매핑하고 금지 field를 제거한다.
매핑되지 않은 내부 오류는 안전한 공개 `INTERNAL_ERROR`로 일반화한다. 내부 오류 원본은
보호된 내부 event·log에 유지해 진단 가능성을 잃지 않는다.

공개 결과에는 `session_handle`, `SequentialId`, Packet/fragment ID, `burst_id`, 내부 layer
stack과 장치·buffer 정보를 포함하지 않는다.
"""

from __future__ import annotations

from ..common.errors import (
    Details,
    ErrorCode,
    InternalProtocolError,
    ServiceError,
    ServiceErrorCategory,
)


class PublicErrorCode:
    """Client에 노출하는 안정된 공개 오류 code."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    SERVICE_NOT_STARTED = "SERVICE_NOT_STARTED"
    SERVICE_ALREADY_STARTED = "SERVICE_ALREADY_STARTED"
    UNKNOWN_TRANSFER = "UNKNOWN_TRANSFER"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    TRANSFER_TIMEOUT = "TRANSFER_TIMEOUT"
    DEVICE_ERROR = "DEVICE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Stage 5가 승인한 가산 공개 code (`DR-STAGE5-005`). DTO 모양은 바꾸지 않는다.
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"


#: 내부 code → (공개 code, 공개 category) 명시 매핑.
_CODE_MAP = {
    ErrorCode.INVALID_REQUEST: (PublicErrorCode.INVALID_REQUEST, ServiceErrorCategory.VALIDATION),
    ErrorCode.INVALID_VALUE: (PublicErrorCode.INVALID_REQUEST, ServiceErrorCategory.VALIDATION),
    ErrorCode.UNSUPPORTED_REQUEST: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.NOT_IMPLEMENTED: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.REGISTRY_UNKNOWN_KEY: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.SERVICE_NOT_STARTED: (
        PublicErrorCode.SERVICE_NOT_STARTED,
        ServiceErrorCategory.VALIDATION,
    ),
    ErrorCode.ENDPOINT_NOT_RUNNING: (
        PublicErrorCode.SERVICE_NOT_STARTED,
        ServiceErrorCategory.VALIDATION,
    ),
    ErrorCode.SERVICE_ALREADY_STARTED: (
        PublicErrorCode.SERVICE_ALREADY_STARTED,
        ServiceErrorCategory.VALIDATION,
    ),
    ErrorCode.UNKNOWN_TRANSFER: (
        PublicErrorCode.UNKNOWN_TRANSFER,
        ServiceErrorCategory.VALIDATION,
    ),
    # Stage 4 MAC 가산 migration (DR-STAGE4-MAC-015). 기존 공개 code 문자열은 바꾸지 않는다.
    ErrorCode.MAC_TRANSFER_FAILED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.MAC_RESOURCE_UNAVAILABLE: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.RESOURCE,
    ),
    ErrorCode.MAC_TRANSFER_TIMEOUT: (
        PublicErrorCode.TRANSFER_TIMEOUT,
        ServiceErrorCategory.TIMEOUT,
    ),
    ErrorCode.RADIO_NOT_CONFIGURED: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_NOT_STARTED: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_ALREADY_STARTED: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_DEPENDENCY_MISSING: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_UNSUPPORTED_SETTING: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_UNKNOWN_CHANNEL: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    # --- Stage 5 §9.5 명시적 계층별 mapping (가산; DR-STAGE5-005, DR-STAGE5-015) ---
    # Service/event/전송 queue 포화
    ErrorCode.SERVICE_RESOURCE_UNAVAILABLE: (
        PublicErrorCode.RESOURCE_UNAVAILABLE,
        ServiceErrorCategory.RESOURCE,
    ),
    # Presentation RX
    ErrorCode.PRESENTATION_TRUNCATED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PRESENTATION_STRUCTURE_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.PRESENTATION_DISCRIMINATOR_CONFLICT: (
        PublicErrorCode.INVALID_REQUEST,
        ServiceErrorCategory.VALIDATION,
    ),
    # Link protocol (invariant성 진행 오류 LINK_TRANSFER_PROGRESS_*는 Stage 4 동결
    # projection대로 INTERNAL_ERROR로 남긴다)
    ErrorCode.LINK_CRC_MISMATCH: (PublicErrorCode.TRANSFER_FAILED, ServiceErrorCategory.PROTOCOL),
    ErrorCode.LINK_PACKET_LENGTH_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_PACKET_CAPACITY_EXCEEDED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_MESSAGE_LENGTH_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_PADDING_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_PACKET_CONTENT_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_INVALID_MESSAGE_FIELD: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_RESERVED_FIELD_NONZERO: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_FRAGMENT_COUNT_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_FRAGMENT_NUMBER_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_FRAGMENT_CONFLICT: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_REASSEMBLY_INCOMPLETE: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_SEGMENT_LENGTH_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_SESSION_CONFLICT: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_DATA_ROUTE_INCOMPLETE: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_DATA_ROUTE_MISMATCH: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.LINK_UNSUPPORTED_LINK_ID: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.LINK_UNSUPPORTED_MESSAGE_TYPE: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.LINK_UNSUPPORTED_MESSAGE_PROFILE: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.LINK_UNSUPPORTED_SEGMENT_TYPE: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    # PHY
    ErrorCode.PHY_TRANSMIT_DEADLINE_EXPIRED: (
        PublicErrorCode.TRANSFER_TIMEOUT,
        ServiceErrorCategory.TIMEOUT,
    ),
    ErrorCode.PHY_PACKET_LENGTH_INVALID: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PHY_SYNCHRONIZATION_FAILED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PHY_LINK_ID_DETECTION_FAILED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PHY_FEC_DECODE_FAILED: (
        PublicErrorCode.TRANSFER_FAILED,
        ServiceErrorCategory.PROTOCOL,
    ),
    ErrorCode.PHY_UNSUPPORTED_LINK_ID: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    ErrorCode.PHY_LINK_ID_NOT_ALLOWED: (
        PublicErrorCode.UNSUPPORTED_REQUEST,
        ServiceErrorCategory.UNSUPPORTED,
    ),
    # Radio
    ErrorCode.RADIO_RESOURCE_EXHAUSTED: (
        PublicErrorCode.RESOURCE_UNAVAILABLE,
        ServiceErrorCategory.RESOURCE,
    ),
    ErrorCode.RADIO_RESOURCE_CONFLICT: (
        PublicErrorCode.RESOURCE_UNAVAILABLE,
        ServiceErrorCategory.RESOURCE,
    ),
    ErrorCode.RADIO_LATE_REQUEST: (
        PublicErrorCode.TRANSFER_TIMEOUT,
        ServiceErrorCategory.TIMEOUT,
    ),
    ErrorCode.RADIO_LATE_COMMAND: (
        PublicErrorCode.TRANSFER_TIMEOUT,
        ServiceErrorCategory.TIMEOUT,
    ),
    ErrorCode.RADIO_DEVICE_NOT_FOUND: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_CONFIGURATION_FAILED: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_TX_UNDERFLOW: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_RX_OVERFLOW: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_RX_TIMEOUT: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_RX_WINDOW_INCOMPLETE: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_STREAM_FAILED: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_DEVICE_DISCONNECTED: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_REFERENCE_LOST: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_OPERATION_CANCELLED: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_TX_OUTCOME_UNKNOWN: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_STOP_TIMEOUT: (PublicErrorCode.DEVICE_ERROR, ServiceErrorCategory.DEVICE),
    ErrorCode.RADIO_EVENT_DELIVERY_FAILED: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
    ErrorCode.RADIO_TX_SAMPLE_OUT_OF_RANGE: (
        PublicErrorCode.DEVICE_ERROR,
        ServiceErrorCategory.DEVICE,
    ),
}

_PUBLIC_MESSAGES = {
    PublicErrorCode.INVALID_REQUEST: "요청이 올바르지 않습니다",
    PublicErrorCode.UNSUPPORTED_REQUEST: "지원하지 않는 요청입니다",
    PublicErrorCode.SERVICE_NOT_STARTED: "Service가 시작되지 않았습니다",
    PublicErrorCode.SERVICE_ALREADY_STARTED: "Service가 이미 시작되었습니다",
    PublicErrorCode.UNKNOWN_TRANSFER: "알 수 없는 transfer입니다",
    PublicErrorCode.TRANSFER_FAILED: "전송 처리에 실패했습니다",
    PublicErrorCode.TRANSFER_TIMEOUT: "전송 처리 시간이 초과되었습니다",
    PublicErrorCode.DEVICE_ERROR: "무선 장치 처리에 실패했습니다",
    PublicErrorCode.INTERNAL_ERROR: "내부 처리 중 오류가 발생했습니다",
    PublicErrorCode.RESOURCE_UNAVAILABLE: "요청을 수락할 자원이 없습니다",
}

#: 공개 details에 남길 수 있는 key. 그 밖의 key는 제거한다.
PUBLIC_DETAIL_KEYS = frozenset(
    {
        "payload_format",
        "payload_size",
        "max_payload_bytes",
        "reason",
        "operation",
        "stage",
        # Stage 5 승인 allowlist (`stage5_service_integration_baseline.md` §9.5)
        "limit_name",
        "limit_value",
        "current_value",
        "endpoint_id",
        "previous_health",
        "health",
    }
)


def public_details(details: Details) -> Details:
    """공개 allowlist에 있는 key만 남긴다."""

    kept = tuple(
        (key, value) for key, value in details.entries if key in PUBLIC_DETAIL_KEYS
    )
    return Details(kept)


def to_service_error(error: InternalProtocolError) -> ServiceError:
    """내부 오류를 공개 오류로 projection한다.

    `transfer_id`만 상관관계로 남기고 `session_handle`, `SequentialId`, Packet/fragment ID,
    `burst_id`, layer와 장치 정보는 제거한다. 매핑되지 않은 code는 `INTERNAL_ERROR`로
    일반화한다.
    """

    mapped = _CODE_MAP.get(error.code)
    if mapped is None:
        code, category = PublicErrorCode.INTERNAL_ERROR, ServiceErrorCategory.INTERNAL
    else:
        code, category = mapped
    # 내부 message는 자유 문자열이므로 correlation ID, 장치 문자열 또는 secret이 섞일 수
    # 있다. 공개 경계에서는 안정된 code별 문구로 대체하고 진단 원문은 내부 event/log에만 둔다.
    message = _PUBLIC_MESSAGES[code]
    # Stage 5 §9.5 retryable 원칙: TIMEOUT과 RESOURCE 포화는 원인 해소 뒤 새 submit을
    # 고려할 수 있다는 hint이므로 공개 결과에서 retryable=true다.
    retryable = error.retryable or code in (
        PublicErrorCode.TRANSFER_TIMEOUT,
        PublicErrorCode.RESOURCE_UNAVAILABLE,
    )
    return ServiceError(
        code=code,
        category=category,
        message=message,
        retryable=retryable,
        transfer_id=error.correlation.transfer_id,
        details=public_details(error.details),
    )
