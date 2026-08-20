"""MAC Port (`stage0_baseline.md` §5.3, 상위 명세 §7.4, §7.5).

MAC은 VDE-TER의 시간, 채널, 자원과 통신 절차 상태를 소유한다. Message bit layout, CRC,
FEC, modulation과 장비 제어는 구현하지 않는다.

TDMA, TBB, RAC, Logical Channel, Data Session 상태 전이, ACK와 재전송은 단계 4 MAC
기준선이 구현한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.link import TransferRequirement
from ..common.contracts.mac import (
    MacActionSet,
    MacMessageInput,
    PacketDecodeAction,
    RequirementAccepted,
    SessionControlRequest,
    SessionControlResult,
)
from ..common.contracts.phy import RecoveredPacketSet
from ..common.results import InternalResult


@runtime_checkable
class MacPort(Protocol):
    """Link ↔ MAC ↔ PHY 경계 계약."""

    def submit_requirement(
        self, requirement: TransferRequirement
    ) -> InternalResult[RequirementAccepted]:
        """`InternalResult[RequirementAccepted]`를 반환한다.

        실제 전송 계획은 접수 뒤 `TransmissionPlan` event로 전달한다.
        """

    def handle_recovered_packet(
        self, recovered: RecoveredPacketSet
    ) -> InternalResult[PacketDecodeAction]:
        """`InternalResult[PacketDecodeAction]`을 반환한다."""

    def handle_message(self, message_input: MacMessageInput) -> InternalResult[MacActionSet]:
        """`InternalResult[MacActionSet]`을 반환한다."""

    def control_session(
        self, request: SessionControlRequest
    ) -> InternalResult[SessionControlResult]:
        """`InternalResult[SessionControlResult]`를 반환한다."""
