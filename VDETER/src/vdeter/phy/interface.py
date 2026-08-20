"""PHY Port (`stage0_baseline.md` §5.3, 상위 명세 §7.5, §7.6).

PHY는 Link Packet과 무선 전송 burst 사이의 변환을 소유한다. Link ID, Physical Channel,
slot과 재전송 여부는 결정하지 않으며 Packet CRC, Message Type, Source/Destination과
application payload를 해석하지 않는다.

PHY는 GNU Radio, UHD, PMT 또는 장치 객체를 받지 않는다. 실제 FEC, interleaving,
scrambling, modulation, burst와 수신 동기는 단계 2·6 PHY 기준선이 구현한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.phy import (
    BasebandBurst,
    PhyDecodeRequest,
    PhyEncodeRequest,
    RecoveredPacketSet,
)
from ..common.results import InternalResult


@runtime_checkable
class PhyPort(Protocol):
    """MAC ↔ PHY ↔ Radio 경계 계약."""

    def encode_packet(self, request: PhyEncodeRequest) -> InternalResult[BasebandBurst]:
        """`InternalResult[BasebandBurst]`를 반환한다."""

    def decode_samples(self, request: PhyDecodeRequest) -> InternalResult[RecoveredPacketSet]:
        """`InternalResult[RecoveredPacketSet]`을 반환한다."""
