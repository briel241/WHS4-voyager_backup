"""Short Data와 단일 Data Session flow (`DR-STAGE4-MAC-009`, `010`, `014`).

이 module은 procedure-local 순수 계산만 소유한다. event 전달, command 생성과 state commit은
`MacCoordinator`가 수행한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.contracts.mac_execution import (
    MacFailureReason,
    MacReasonCode,
)
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import InvalidValueError
from ..common.results import Ok, internal_error
from .access import ActiveSelectionInterval
from .models import Stage4Limits
from .tdma import FRAME_NANOSECONDS

#: Data Session 한 TDMA frame이 담을 수 있는 fragment 수.
MIN_FRAME_FRAGMENTS = 1
MAX_FRAME_FRAGMENTS = 14

#: Stage 4 완료 profile이 지원하는 Type 92 retransmission number.
MIN_RETRANSMISSION_NUMBER = 0
MAX_RETRANSMISSION_NUMBER = Stage4Limits.MAX_SHORT_DATA_RETRANSMISSION

#: Type 92 wire `255`는 no-ACK 변형이며 Stage 4는 구조화 미지원으로 거부한다.
WIRE_NO_ACK_RETRANSMISSION_NUMBER = 255


def _error(code: str, category: ErrorCategory, message: str, reason, reason_code=None, **extra):
    details = dict(extra)
    details["mac_reason"] = reason.value
    if reason_code is not None:
        details["reason_code"] = reason_code
    return internal_error(Layer.MAC, code, category, message, details=Details.of(**details))


def validate_received_retransmission_number(value: int):
    """수신 Type 92의 `4..254`와 `255`는 Type 93으로 바꾸지 않고 거부한다."""

    if isinstance(value, bool) or not isinstance(value, int):
        return _error(
            ErrorCode.INVALID_REQUEST,
            ErrorCategory.VALIDATION,
            "retransmission number가 정수가 아닙니다",
            MacFailureReason.EVENT_INVALID,
        )
    if MIN_RETRANSMISSION_NUMBER <= value <= MAX_RETRANSMISSION_NUMBER:
        return Ok(value)
    return _error(
        ErrorCode.UNSUPPORTED_REQUEST,
        ErrorCategory.UNSUPPORTED,
        "Stage 4 완료 profile은 Type 92 retransmission number 0..3만 지원합니다",
        MacFailureReason.FEATURE_NOT_SUPPORTED,
        MacReasonCode.UNSUPPORTED_RETRANSMISSION_NUMBER,
        retransmission_number=value,
    )


@dataclass(frozen=True, slots=True)
class FramePlan:
    """한 Data Session TDMA frame의 fragment 배치."""

    session_frame_index: int
    frame_fragment_count: int
    continue_resource_usage: int

    def __post_init__(self) -> None:
        if not MIN_FRAME_FRAGMENTS <= self.frame_fragment_count <= MAX_FRAME_FRAGMENTS:
            raise InvalidValueError("FramePlan.frame_fragment_count는 1..14여야 합니다")
        if self.continue_resource_usage not in (0, 1):
            raise InvalidValueError("FramePlan.continue_resource_usage는 0 또는 1이어야 합니다")

    def message_types(self) -> tuple[int, ...]:
        """`1`이면 Type 74만, `2`이면 74→76, `3..14`이면 74→필요한 75→76 순서다."""

        if self.frame_fragment_count == 1:
            return (74,)
        if self.frame_fragment_count == 2:
            return (74, 76)
        return (74,) + (75,) * (self.frame_fragment_count - 2) + (76,)


def plan_session_frames(
    *, total_fragments: int, fragments_per_frame: int
) -> tuple[FramePlan, ...]:
    """payload fragment 수를 frame 단위 계획으로 나눈다.

    payload가 더 남으면 그 frame의 Type 76 `continue_resource_usage=1`로 기본 renewal을
    요청하고 다음 frame에서도 같은 규칙을 반복한다.
    """

    if isinstance(total_fragments, bool) or not isinstance(total_fragments, int) or total_fragments < 1:
        raise InvalidValueError("plan_session_frames: total_fragments는 1 이상이어야 합니다")
    if not MIN_FRAME_FRAGMENTS <= fragments_per_frame <= MAX_FRAME_FRAGMENTS:
        raise InvalidValueError("plan_session_frames: fragments_per_frame은 1..14여야 합니다")
    plans = []
    remaining = total_fragments
    index = 0
    while remaining > 0:
        count = min(remaining, fragments_per_frame)
        remaining -= count
        plans.append(
            FramePlan(
                session_frame_index=index,
                frame_fragment_count=count,
                continue_resource_usage=1 if remaining > 0 else 0,
            )
        )
        index += 1
    return tuple(plans)


def response_deadline_nanoseconds(
    *, tx_completed_monotonic_nanoseconds: int, interval: ActiveSelectionInterval
) -> int:
    """`실제 PHY 송신 완료 + 요청 Physical Channel SI + 2.4초` (`DR-STAGE4-MAC-010`)."""

    if not isinstance(interval, ActiveSelectionInterval):
        raise InvalidValueError("response_deadline_nanoseconds: ActiveSelectionInterval이 필요합니다")
    return (
        tx_completed_monotonic_nanoseconds + interval.duration_nanoseconds + FRAME_NANOSECONDS
    )


def may_retry_resource_request(attempt_count: int) -> bool:
    """최초 송신 뒤 최대 3회 재송신한다. 즉 최대 물리 송신 시도는 4회다."""

    return attempt_count < Stage4Limits.MAX_RESOURCE_REQUEST_ATTEMPTS


def may_retry_short_data(retransmission_number: int) -> bool:
    return retransmission_number < MAX_RETRANSMISSION_NUMBER


def may_retry_fragment(retransmission_count: int) -> bool:
    """NACK fragment만 fragment별 최대 3회 재전송한다."""

    return retransmission_count < Stage4Limits.MAX_FRAGMENT_RETRANSMISSION


def retry_exhausted_error(*, resource_request: bool):
    """Type 90 자원 획득 종료는 명시적으로 `MAC_RESOURCE_UNAVAILABLE`이다."""

    if resource_request:
        return _error(
            ErrorCode.MAC_RESOURCE_UNAVAILABLE,
            ErrorCategory.RESOURCE,
            "Type 90 최대 재송신 뒤에도 Type 4 응답을 받지 못했습니다",
            MacFailureReason.RETRY_EXHAUSTED,
        )
    return _error(
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.PROTOCOL,
        "최대 재전송 횟수를 초과했습니다",
        MacFailureReason.RETRY_EXHAUSTED,
    )


def ack_timeout_error():
    return _error(
        ErrorCode.MAC_TRANSFER_TIMEOUT,
        ErrorCategory.TIMEOUT,
        "연속 세 TDMA frame 동안 유효한 Type 13을 받지 못했습니다",
        MacFailureReason.ACK_TIMEOUT,
    )


def resource_busy_error():
    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "endpoint당 활성 Data Session은 하나입니다",
        MacFailureReason.RESOURCE_UNAVAILABLE,
        MacReasonCode.RESOURCE_BUSY,
    )


def control_station_out_of_service_error():
    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "Control station이 out of service 상태입니다",
        MacFailureReason.RESOURCE_UNAVAILABLE,
        MacReasonCode.CONTROL_STATION_OUT_OF_SERVICE,
    )


def slot_missed_error(procedure: str):
    """lead time 위반은 protocol 응답 timeout이 아니므로 retry로 숨기지 않는다."""

    return _error(
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.RESOURCE,
        "선택한 slot이 Radio 최소 lead time을 만족하지 못했습니다",
        MacFailureReason.SLOT_MISSED,
        None,
        procedure=procedure,
    )


def unsupported_profile_error(link_id_value: int):
    """미등록 또는 `SUPPORTED`가 아닌 PHY profile은 다른 Link ID로 대체하지 않는다."""

    return _error(
        ErrorCode.REGISTRY_UNKNOWN_KEY,
        ErrorCategory.UNSUPPORTED,
        "운영 PHY registry에 지원되는 Link ID profile이 없습니다",
        MacFailureReason.FEATURE_NOT_SUPPORTED,
        MacReasonCode.UNSUPPORTED_PROFILE,
        link_id=link_id_value,
    )


def tbb_expired_error(*, during_session: bool):
    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE if not during_session else ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.RESOURCE if not during_session else ErrorCategory.PROTOCOL,
        "활성 TBB 유효구간이 끝났습니다",
        MacFailureReason.RESOURCE_UNAVAILABLE
        if not during_session
        else MacFailureReason.SESSION_ABORTED,
        MacReasonCode.TBB_EXPIRED_DURING_SESSION
        if during_session
        else MacReasonCode.TBB_EXPIRED,
    )


def endpoint_stopping_error():
    return _error(
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.RESOURCE,
        "endpoint가 정지 중이므로 신규 protocol 작업을 수락하지 않습니다",
        MacFailureReason.DEPENDENCY_FAILED,
        MacReasonCode.ENDPOINT_STOPPING,
    )


def local_fifo_full_error():
    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "대기 중인 local/relay transfer가 16개 상한에 도달했습니다",
        MacFailureReason.RESOURCE_UNAVAILABLE,
    )


def lc_conflict_error():
    return _error(
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.PROTOCOL,
        "다른 station 할당과 Logical Channel이 충돌했습니다",
        MacFailureReason.LC_CONFLICT,
    )


__all__ = (
    "FramePlan",
    "MAX_FRAME_FRAGMENTS",
    "MAX_RETRANSMISSION_NUMBER",
    "MIN_FRAME_FRAGMENTS",
    "MIN_RETRANSMISSION_NUMBER",
    "WIRE_NO_ACK_RETRANSMISSION_NUMBER",
    "ack_timeout_error",
    "control_station_out_of_service_error",
    "endpoint_stopping_error",
    "lc_conflict_error",
    "local_fifo_full_error",
    "may_retry_fragment",
    "may_retry_resource_request",
    "may_retry_short_data",
    "plan_session_frames",
    "resource_busy_error",
    "response_deadline_nanoseconds",
    "retry_exhausted_error",
    "slot_missed_error",
    "tbb_expired_error",
    "unsupported_profile_error",
    "validate_received_retransmission_number",
)
