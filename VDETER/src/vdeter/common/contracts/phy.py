"""MAC ↔ PHY 및 PHY 결과 경계 DTO (`stage0_baseline.md` §5.3).

PHY는 MAC이 지정한 Link ID profile과 송수신 계획을 수행한다. 이 module의 DTO는 Packet
bytes와 추적 식별자만 다루며 Packet CRC, Message Type, Source/Destination, TBB, Session,
ACK 또는 application payload 의미를 포함하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..exceptions import InvalidValueError
from ..identifiers import BurstId, LinkId, PacketId, PhysicalChannel
from ..time import ClockDomain, TimePoint
from .radio import SampleBlock


class PhyRecoveryStatus(str, Enum):
    """복원 결과 구분. 실패 data를 정상 Packet으로 전달하지 않는다."""

    RECOVERED = "RECOVERED"
    NO_BURST_DETECTED = "NO_BURST_DETECTED"


@dataclass(frozen=True, slots=True)
class PhyEncodeRequest:
    """Packet 송신 요청. MAC이 Link ID, 채널과 송신시각을 결정한다."""

    packet_id: PacketId
    link_id: LinkId
    channel: PhysicalChannel
    transmit_at: TimePoint
    packet: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyEncodeRequest.packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("PhyEncodeRequest.link_id: LinkId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyEncodeRequest.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.transmit_at, TimePoint):
            raise InvalidValueError("PhyEncodeRequest.transmit_at: TimePoint여야 합니다")
        if self.transmit_at.clock_domain is not ClockDomain.UTC:
            raise InvalidValueError("PhyEncodeRequest.transmit_at: UTC 계획이어야 합니다")
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("PhyEncodeRequest.packet: bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class BasebandBurst:
    """PHY가 생성한 burst. Radio에 전달할 sample과 실행 metadata만 담는다."""

    burst_id: BurstId
    packet_id: PacketId
    link_id: LinkId
    channel: PhysicalChannel
    transmit_at: TimePoint
    samples: SampleBlock

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("BasebandBurst.burst_id: BurstId여야 합니다")
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("BasebandBurst.packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("BasebandBurst.link_id: LinkId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("BasebandBurst.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.transmit_at, TimePoint):
            raise InvalidValueError("BasebandBurst.transmit_at: TimePoint여야 합니다")
        if not isinstance(self.samples, SampleBlock):
            raise InvalidValueError("BasebandBurst.samples: SampleBlock이어야 합니다")


@dataclass(frozen=True, slots=True)
class PhyDecodeRequest:
    """수신 sample 처리 요청. 허용 Link ID 집합은 MAC이 정한다."""

    channel: PhysicalChannel
    samples: SampleBlock
    allowed_link_ids: tuple[LinkId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("PhyDecodeRequest.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.samples, SampleBlock):
            raise InvalidValueError("PhyDecodeRequest.samples: SampleBlock이어야 합니다")
        if not isinstance(self.allowed_link_ids, tuple) or any(
            not isinstance(link_id, LinkId) for link_id in self.allowed_link_ids
        ):
            raise InvalidValueError("PhyDecodeRequest.allowed_link_ids: LinkId tuple이어야 합니다")


@dataclass(frozen=True, slots=True)
class RecoveredPacket:
    """복원된 Packet 하나. 검출된 Link ID와 수신시각을 함께 전달한다."""

    packet_id: PacketId
    link_id: LinkId
    channel: PhysicalChannel
    received_at: TimePoint
    packet: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("RecoveredPacket.packet_id: PacketId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("RecoveredPacket.link_id: LinkId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RecoveredPacket.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("RecoveredPacket.received_at: TimePoint여야 합니다")
        if not isinstance(self.packet, bytes):
            raise InvalidValueError("RecoveredPacket.packet: bytes여야 합니다")


@dataclass(frozen=True, slots=True)
class RecoveredPacketSet:
    """한 수신 구간에서 복원한 Packet 집합. 정상 복원된 Packet만 담는다."""

    channel: PhysicalChannel
    received_at: TimePoint
    status: PhyRecoveryStatus
    packets: tuple[RecoveredPacket, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("RecoveredPacketSet.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.received_at, TimePoint):
            raise InvalidValueError("RecoveredPacketSet.received_at: TimePoint여야 합니다")
        if not isinstance(self.status, PhyRecoveryStatus):
            raise InvalidValueError("RecoveredPacketSet.status: PhyRecoveryStatus여야 합니다")
        if not isinstance(self.packets, tuple) or any(
            not isinstance(packet, RecoveredPacket) for packet in self.packets
        ):
            raise InvalidValueError("RecoveredPacketSet.packets: RecoveredPacket tuple이어야 합니다")
        if self.status is PhyRecoveryStatus.NO_BURST_DETECTED and self.packets:
            raise InvalidValueError(
                "RecoveredPacketSet: NO_BURST_DETECTED에는 Packet을 담을 수 없습니다"
            )


@dataclass(frozen=True, slots=True)
class PhyTxCompleted:
    """PHY 송신 결과 통지."""

    burst_id: BurstId
    packet_id: PacketId
    completed_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, BurstId):
            raise InvalidValueError("PhyTxCompleted.burst_id: BurstId여야 합니다")
        if not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("PhyTxCompleted.packet_id: PacketId여야 합니다")
        if not isinstance(self.completed_at, TimePoint):
            raise InvalidValueError("PhyTxCompleted.completed_at: TimePoint여야 합니다")
