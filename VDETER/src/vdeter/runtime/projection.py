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
    return ServiceError(
        code=code,
        category=category,
        message=message,
        retryable=error.retryable,
        transfer_id=error.correlation.transfer_id,
        details=public_details(error.details),
    )
