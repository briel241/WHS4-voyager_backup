"""MAC 계층 내부 model (`stage0_baseline.md` §4.1.1, §8).

공개 경계 DTO는 `common.contracts.mac`에 있다 (decision record `DR-STAGE0-001`).

TBB, `session_handle`, ACK와 buffer 상태는 runtime 상태이므로 memory에만 두고 profile이나
config에 쓰지 않으며 endpoint 재시작 시 복원하지 않는다. 실제 상태 전이는 단계 4에서
구현한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId, PhysicalChannel, SessionHandle, TransferId
from ..common.time import TimePoint


@dataclass(frozen=True, slots=True)
class MacPlanningDefaults:
    """설정으로 주입되는 계획 기본값. 규범 profile 값이 아니다."""

    link_id: LinkId
    tx_channel: PhysicalChannel
    rx_channel: PhysicalChannel
    transmit_lead_nanoseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("MacPlanningDefaults.link_id: LinkId여야 합니다")
        for name in ("tx_channel", "rx_channel"):
            if not isinstance(getattr(self, name), PhysicalChannel):
                raise InvalidValueError(f"MacPlanningDefaults.{name}: PhysicalChannel이어야 합니다")
        value = self.transmit_lead_nanoseconds
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidValueError(
                "MacPlanningDefaults.transmit_lead_nanoseconds: 0 이상의 정수여야 합니다"
            )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """활성 Data Session의 runtime 상태 snapshot. wire field가 아니다."""

    session_handle: SessionHandle
    transfer_id: TransferId | None
    opened_at: TimePoint
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("SessionRecord.session_handle: SessionHandle이어야 합니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("SessionRecord.transfer_id: TransferId여야 합니다")
        if not isinstance(self.opened_at, TimePoint):
            raise InvalidValueError("SessionRecord.opened_at: TimePoint여야 합니다")
        if not isinstance(self.active, bool):
            raise InvalidValueError("SessionRecord.active: bool이어야 합니다")

    def closed(self) -> "SessionRecord":
        return SessionRecord(
            session_handle=self.session_handle,
            transfer_id=self.transfer_id,
            opened_at=self.opened_at,
            active=False,
        )
