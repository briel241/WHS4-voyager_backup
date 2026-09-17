"""Stage 7 private 오류 (`stage7_b210_system_baseline.md` §15.2).

아래 값은 frozen `common.errors.ErrorCode`/`PublicErrorCode`에 추가하는 enum이
아니다. Stage 7 Runtime Coordinator/TEST Controller 전용 private code이며
protected trace와 test evidence에서만 원문을 보존한다. 공개 Service 경계가
필요한 경우 §15.2 표의 기존 internal `ErrorCode`로 한 번 변환한 뒤 frozen
`runtime.projection`을 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..common.errors import ErrorCode


class PrivateErrorCode(str, Enum):
    ENVIRONMENT_UNAPPROVED = "ENVIRONMENT_UNAPPROVED"
    CFO_OUT_OF_FROZEN_PHY_RANGE = "CFO_OUT_OF_FROZEN_PHY_RANGE"
    RF_GAIN_CALIBRATION_FAILED = "RF_GAIN_CALIBRATION_FAILED"
    PHY_DIAGNOSTIC_UNAVAILABLE = "PHY_DIAGNOSTIC_UNAVAILABLE"
    RX_LANE_CONTEXT_INVALID = "RX_LANE_CONTEXT_INVALID"
    TIME_BRIDGE_GENERATIONS_EXHAUSTED = "TIME_BRIDGE_GENERATIONS_EXHAUSTED"
    LANE_ROUTE_CONTEXT_MISSING = "LANE_ROUTE_CONTEXT_MISSING"
    LANE_ROUTE_AMBIGUOUS = "LANE_ROUTE_AMBIGUOUS"
    LANE_ROUTE_INVALID = "LANE_ROUTE_INVALID"
    HOST_AFFINE_BRIDGE_NOT_LOCKED = "HOST_AFFINE_BRIDGE_NOT_LOCKED"
    HOST_AFFINE_BRIDGE_INVALID = "HOST_AFFINE_BRIDGE_INVALID"
    HOST_AFFINE_BRIDGE_HORIZON_EXCEEDED = "HOST_AFFINE_BRIDGE_HORIZON_EXCEEDED"
    STALE_TIME_BRIDGE_EVENT = "STALE_TIME_BRIDGE_EVENT"
    STALE_LANE_EVENT = "STALE_LANE_EVENT"
    PARTIAL_RF_EMISSION = "PARTIAL_RF_EMISSION"
    CONTROL_PEER_INVALID = "CONTROL_PEER_INVALID"
    CONTROL_MESSAGE_INVALID = "CONTROL_MESSAGE_INVALID"
    CONTROL_BARRIER_FAILED = "CONTROL_BARRIER_FAILED"


#: §15.2: 이미 수락된 Service work가 있을 때의 공개 투영에 쓰는 underlying
#: frozen `ErrorCode`. `None`은 공개 투영이 없다는 뜻이다.
UNDERLYING_ERROR_CODE = {
    PrivateErrorCode.ENVIRONMENT_UNAPPROVED: None,
    PrivateErrorCode.CFO_OUT_OF_FROZEN_PHY_RANGE: ErrorCode.RADIO_REFERENCE_LOST,
    PrivateErrorCode.RF_GAIN_CALIBRATION_FAILED: None,
    PrivateErrorCode.PHY_DIAGNOSTIC_UNAVAILABLE: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.RX_LANE_CONTEXT_INVALID: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.TIME_BRIDGE_GENERATIONS_EXHAUSTED: ErrorCode.RADIO_REFERENCE_LOST,
    PrivateErrorCode.LANE_ROUTE_CONTEXT_MISSING: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.LANE_ROUTE_AMBIGUOUS: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.LANE_ROUTE_INVALID: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.HOST_AFFINE_BRIDGE_NOT_LOCKED: ErrorCode.RADIO_REFERENCE_LOST,
    PrivateErrorCode.HOST_AFFINE_BRIDGE_INVALID: ErrorCode.RADIO_REFERENCE_LOST,
    PrivateErrorCode.HOST_AFFINE_BRIDGE_HORIZON_EXCEEDED: ErrorCode.RADIO_LATE_REQUEST,
    PrivateErrorCode.STALE_TIME_BRIDGE_EVENT: None,
    PrivateErrorCode.STALE_LANE_EVENT: None,
    PrivateErrorCode.PARTIAL_RF_EMISSION: ErrorCode.RADIO_TX_OUTCOME_UNKNOWN,
    PrivateErrorCode.CONTROL_PEER_INVALID: ErrorCode.RADIO_REFERENCE_LOST,
    PrivateErrorCode.CONTROL_MESSAGE_INVALID: ErrorCode.INTERNAL_ERROR,
    PrivateErrorCode.CONTROL_BARRIER_FAILED: ErrorCode.RADIO_REFERENCE_LOST,
}


class ControlPlane(str, Enum):
    RUNTIME = "RUNTIME"
    TEST = "TEST"


@dataclass(frozen=True, slots=True)
class PrivateFault:
    """§15.2 immutable private fault record. payload/IQ/certificate 원문은 담지 않는다."""

    private_code: PrivateErrorCode
    phase: str
    endpoint_id: str
    run_id: str
    radio_generation: int | None = None
    lane_generation: int | None = None
    bridge_generation: int | None = None
    parent_correlation: str | None = None
    child_correlation: str | None = None
    first_hardware_timestamp_ns: int | None = None
    underlying_error_code: str | None = None
    artifact_reference: str | None = None
    control_plane: ControlPlane | None = None
    command_id: str | None = None
    socket_path: str | None = None
    socket_mode: str | None = None
    expected_peer_pid: int | None = None
    actual_peer_pid: int | None = None
    expected_peer_uid: int | None = None
    actual_peer_uid: int | None = None
    details: dict = field(default_factory=dict)

    def as_json_value(self) -> dict:
        value = {
            "record_type": "STAGE7_PRIVATE_FAULT_V1",
            "private_code": self.private_code.value,
            "phase": self.phase,
            "endpoint_id": self.endpoint_id,
            "run_id": self.run_id,
            "radio_generation": self.radio_generation,
            "lane_generation": self.lane_generation,
            "bridge_generation": self.bridge_generation,
            "parent_correlation": self.parent_correlation,
            "child_correlation": self.child_correlation,
            "first_hardware_timestamp_ns": self.first_hardware_timestamp_ns,
            "underlying_error_code": self.underlying_error_code,
            "artifact_reference": self.artifact_reference,
            "details": dict(self.details),
        }
        if self.control_plane is not None:
            value.update(
                {
                    "control_plane": self.control_plane.value,
                    "command_id": self.command_id,
                    "socket_path": self.socket_path,
                    "socket_mode": self.socket_mode,
                    "expected_peer_pid": self.expected_peer_pid,
                    "actual_peer_pid": self.actual_peer_pid,
                    "expected_peer_uid": self.expected_peer_uid,
                    "actual_peer_uid": self.actual_peer_uid,
                }
            )
        return value


__all__ = (
    "ControlPlane",
    "PrivateErrorCode",
    "PrivateFault",
    "UNDERLYING_ERROR_CODE",
)
