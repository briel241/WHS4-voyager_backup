"""Frozen deterministic fault plan used only by SoftwareRadioAdapter tests."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.errors import ErrorCode
from ...common.exceptions import InvalidValueError
from ...common.identifiers import BurstId, PhysicalChannel

_ALLOWED_CODES = frozenset(
    {
        ErrorCode.RADIO_DEVICE_NOT_FOUND,
        ErrorCode.RADIO_CONFIGURATION_FAILED,
        ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
        ErrorCode.RADIO_LATE_COMMAND,
        ErrorCode.RADIO_TX_UNDERFLOW,
        ErrorCode.RADIO_RX_OVERFLOW,
        ErrorCode.RADIO_RX_TIMEOUT,
        ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
        ErrorCode.RADIO_STREAM_FAILED,
        ErrorCode.RADIO_DEVICE_DISCONNECTED,
        ErrorCode.RADIO_STOP_TIMEOUT,
        ErrorCode.RADIO_EVENT_DELIVERY_FAILED,
        ErrorCode.RADIO_RESOURCE_EXHAUSTED,
    }
)


@dataclass(frozen=True, slots=True)
class SoftwareRadioFault:
    operation: str
    error_code: str
    channel: PhysicalChannel | None = None
    burst_id: BurstId | None = None
    rx_window_key: tuple | None = None
    hardware_time_ns: int | None = None
    sample_index: int | None = None
    persistent: bool = False
    adapter_id: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"configure", "mapping", "runtime", "stop", "tx", "rx"}:
            raise InvalidValueError("SoftwareRadioFault.operation이 잘못되었습니다")
        if self.error_code not in _ALLOWED_CODES:
            raise InvalidValueError("SoftwareRadioFault.error_code가 승인된 fault code가 아닙니다")
        if self.channel is not None and not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("SoftwareRadioFault.channel이 잘못되었습니다")
        if self.burst_id is not None and not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("SoftwareRadioFault.burst_id가 잘못되었습니다")
        if self.rx_window_key is not None and not isinstance(self.rx_window_key, tuple):
            raise InvalidValueError("SoftwareRadioFault.rx_window_key가 잘못되었습니다")
        for name in ("hardware_time_ns", "sample_index"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise InvalidValueError(f"SoftwareRadioFault.{name}는 0 이상의 정수여야 합니다")
        if not isinstance(self.persistent, bool):
            raise InvalidValueError("SoftwareRadioFault.persistent는 bool이어야 합니다")
        if self.adapter_id is not None and (
            not isinstance(self.adapter_id, str) or not self.adapter_id
        ):
            raise InvalidValueError("SoftwareRadioFault.adapter_id는 nonempty 문자열이어야 합니다")
