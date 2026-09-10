"""고정 오류 code와 severity 분류(명세 §14).

severity 3단계(§14.1):

- `REQUEST_REJECTED` : action 하나만 거부하고 PoC·scenario는 계속.
- `POC_ABORT`        : 현재 PoC experiment 중단, outstanding 정리, scenario orderly close.
- `SESSION_INHIBIT`  : 전체 session의 신규 TX 차단, RF fail-closed, session 종료·cleanup.

구현은 같은 code를 run마다 다른 severity로 바꾸지 않는다(§14.2). 더 높은 severity의
조건이 동시에 성립하면 높은 쪽을 적용하고 두 원인을 모두 evidence에 남긴다.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    REQUEST_REJECTED = "REQUEST_REJECTED"
    POC_ABORT = "POC_ABORT"
    SESSION_INHIBIT = "SESSION_INHIBIT"


#: severity 순위(높을수록 강함). 동시 성립 시 큰 값을 적용한다(§14.2).
SEVERITY_RANK = {
    Severity.REQUEST_REJECTED: 1,
    Severity.POC_ABORT: 2,
    Severity.SESSION_INHIBIT: 3,
}


class PocErrorCode(str, Enum):
    # REQUEST_REJECTED
    POC_NOT_RUNNING = "POC_NOT_RUNNING"
    POC_CAPABILITY_DENIED = "POC_CAPABILITY_DENIED"
    POC_TARGET_DENIED = "POC_TARGET_DENIED"
    POC_ACTION_SCHEMA_INVALID = "POC_ACTION_SCHEMA_INVALID"
    POC_ACTION_TOO_LARGE = "POC_ACTION_TOO_LARGE"
    POC_ACTION_LIMIT_EXCEEDED = "POC_ACTION_LIMIT_EXCEEDED"
    POC_ATTACK_INFLIGHT = "POC_ATTACK_INFLIGHT"
    POC_DEADLINE_MISSED = "POC_DEADLINE_MISSED"
    POC_RADIO_RESOURCE_CONFLICT = "POC_RADIO_RESOURCE_CONFLICT"
    # POC_ABORT
    POC_TRANSPORT_UNAVAILABLE = "POC_TRANSPORT_UNAVAILABLE"
    POC_AUTH_FAILED = "POC_AUTH_FAILED"
    POC_VERSION_INCOMPATIBLE = "POC_VERSION_INCOMPATIBLE"
    POC_CAPABILITY_UNAVAILABLE = "POC_CAPABILITY_UNAVAILABLE"
    POC_PROTOCOL_ERROR = "POC_PROTOCOL_ERROR"
    POC_REQUEST_ID_CONFLICT = "POC_REQUEST_ID_CONFLICT"
    POC_EVENT_QUEUE_OVERFLOW = "POC_EVENT_QUEUE_OVERFLOW"
    POC_EVENT_REPLAY_GAP = "POC_EVENT_REPLAY_GAP"
    POC_CALLBACK_EXCEPTION = "POC_CALLBACK_EXCEPTION"
    POC_CALLBACK_TIMEOUT = "POC_CALLBACK_TIMEOUT"
    POC_CONNECTION_LOST = "POC_CONNECTION_LOST"
    POC_OUTPUT_LIMIT_EXCEEDED = "POC_OUTPUT_LIMIT_EXCEEDED"
    POC_PROCESS_EXITED = "POC_PROCESS_EXITED"
    POC_PROCESS_FORCED_KILL = "POC_PROCESS_FORCED_KILL"
    # SESSION_INHIBIT
    POC_TX_OUTCOME_UNKNOWN = "POC_TX_OUTCOME_UNKNOWN"
    POC_RADIO_OWNERSHIP_UNCERTAIN = "POC_RADIO_OWNERSHIP_UNCERTAIN"
    POC_CLOCK_STATE_UNCERTAIN = "POC_CLOCK_STATE_UNCERTAIN"
    POC_DEVICE_HANDLE_UNCERTAIN = "POC_DEVICE_HANDLE_UNCERTAIN"


#: §14.2 고정 mapping. run마다 바뀌지 않는다.
ERROR_SEVERITY = {
    PocErrorCode.POC_NOT_RUNNING: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_CAPABILITY_DENIED: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_TARGET_DENIED: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_ACTION_SCHEMA_INVALID: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_ACTION_TOO_LARGE: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_ACTION_LIMIT_EXCEEDED: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_ATTACK_INFLIGHT: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_DEADLINE_MISSED: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_RADIO_RESOURCE_CONFLICT: Severity.REQUEST_REJECTED,
    PocErrorCode.POC_TRANSPORT_UNAVAILABLE: Severity.POC_ABORT,
    PocErrorCode.POC_AUTH_FAILED: Severity.POC_ABORT,
    PocErrorCode.POC_VERSION_INCOMPATIBLE: Severity.POC_ABORT,
    PocErrorCode.POC_CAPABILITY_UNAVAILABLE: Severity.POC_ABORT,
    PocErrorCode.POC_PROTOCOL_ERROR: Severity.POC_ABORT,
    PocErrorCode.POC_REQUEST_ID_CONFLICT: Severity.POC_ABORT,
    PocErrorCode.POC_EVENT_QUEUE_OVERFLOW: Severity.POC_ABORT,
    PocErrorCode.POC_EVENT_REPLAY_GAP: Severity.POC_ABORT,
    PocErrorCode.POC_CALLBACK_EXCEPTION: Severity.POC_ABORT,
    PocErrorCode.POC_CALLBACK_TIMEOUT: Severity.POC_ABORT,
    PocErrorCode.POC_CONNECTION_LOST: Severity.POC_ABORT,
    PocErrorCode.POC_OUTPUT_LIMIT_EXCEEDED: Severity.POC_ABORT,
    PocErrorCode.POC_PROCESS_EXITED: Severity.POC_ABORT,
    PocErrorCode.POC_PROCESS_FORCED_KILL: Severity.POC_ABORT,
    PocErrorCode.POC_TX_OUTCOME_UNKNOWN: Severity.SESSION_INHIBIT,
    PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN: Severity.SESSION_INHIBIT,
    PocErrorCode.POC_CLOCK_STATE_UNCERTAIN: Severity.SESSION_INHIBIT,
    PocErrorCode.POC_DEVICE_HANDLE_UNCERTAIN: Severity.SESSION_INHIBIT,
}


def severity_of(code: PocErrorCode) -> Severity:
    """code의 고정 severity. mapping에 없는 code는 프로그래밍 오류다."""

    return ERROR_SEVERITY[PocErrorCode(code)]


def escalate(*severities: Severity) -> Severity:
    """동시 성립 severity 중 가장 높은 것을 고른다(§14.2)."""

    if not severities:
        raise ValueError("severity가 하나 이상 필요합니다")
    return max(severities, key=lambda s: SEVERITY_RANK[Severity(s)])


class PocError(Exception):
    """고정 code·severity로 유계 변환된 PoC 연결 오류(명세 §14).

    예외를 삼키지 않고 이 형태로 변환해 error 단계로 분류한다(구조 원칙). 원인
    문맥은 `detail`(비밀 없는 요약)에 담고 raw token/secret은 담지 않는다.
    """

    def __init__(self, code: PocErrorCode, message: str = "", *, detail: dict | None = None) -> None:
        self.code = PocErrorCode(code)
        self.severity = severity_of(self.code)
        self.detail = dict(detail or {})
        super().__init__(f"{self.code.value}: {message}" if message else self.code.value)

    def as_json_value(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "detail": dict(self.detail),
        }


__all__ = (
    "Severity",
    "SEVERITY_RANK",
    "PocErrorCode",
    "ERROR_SEVERITY",
    "severity_of",
    "escalate",
    "PocError",
)
