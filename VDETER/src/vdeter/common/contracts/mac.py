"""MAC 경계 DTO (`stage0_baseline.md` §5.3).

MAC은 통신 방향, Link ID, 채널, 송신시각과 로컬 `session_handle` 수명을 결정한다.
단계 0은 이 결정의 *표현*만 고정하고 실제 TDMA, TBB, RAC, 자원 할당, ACK와 재전송 상태
전이는 단계 4 MAC 기준선이 구현한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..addressing import RouteAddresses
from ..errors import Details, EMPTY_DETAILS
from ..exceptions import InvalidValueError
from ..identifiers import LinkId, PacketId, PhysicalChannel, SessionHandle, TransferId
from ..time import ClockDomain, TimePoint
from .message import DecodedMessages
from .phy import RecoveredPacket


class TransmissionProcedure(str, Enum):
    """단계 0에서 구분하는 최소 송신 절차 종류."""

    SHORT_DATA = "SHORT_DATA"
    DATA_SESSION = "DATA_SESSION"


class SessionControlKind(str, Enum):
    OPEN = "OPEN"
    KEEP = "KEEP"
    CLOSE = "CLOSE"


class MacActionKind(str, Enum):
    """MAC이 Runtime에 지시하는 다음 처리 종류."""

    DECODE_PACKET = "DECODE_PACKET"
    DELIVER_PRESENTATION_DATA = "DELIVER_PRESENTATION_DATA"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True, slots=True)
class RequirementAccepted:
    """전송 요구 접수. 실제 전송 계획은 비동기 `TransmissionPlan`으로 전달한다."""

    transfer_id: TransferId
    accepted_at: TimePoint

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("RequirementAccepted.transfer_id: TransferId여야 합니다")
        if not isinstance(self.accepted_at, TimePoint):
            raise InvalidValueError("RequirementAccepted.accepted_at: TimePoint여야 합니다")


@dataclass(frozen=True, slots=True)
class TransmissionPlan:
    """MAC이 결정한 전송 계획. Link가 이 계획으로 Packet을 구체화한다."""

    transfer_id: TransferId
    link_id: LinkId
    channel: PhysicalChannel
    transmit_at: TimePoint
    procedure: TransmissionProcedure
    route: RouteAddresses
    session_handle: SessionHandle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("TransmissionPlan.transfer_id: TransferId여야 합니다")
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("TransmissionPlan.link_id: LinkId여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("TransmissionPlan.channel: PhysicalChannel이어야 합니다")
        if not isinstance(self.transmit_at, TimePoint):
            raise InvalidValueError("TransmissionPlan.transmit_at: TimePoint여야 합니다")
        if self.transmit_at.clock_domain is not ClockDomain.UTC:
            raise InvalidValueError(
                "TransmissionPlan.transmit_at: TDMA 계획은 UTC domain을 사용해야 합니다"
            )
        if not isinstance(self.procedure, TransmissionProcedure):
            raise InvalidValueError("TransmissionPlan.procedure: TransmissionProcedure여야 합니다")
        if not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("TransmissionPlan.route: RouteAddresses여야 합니다")
        if self.session_handle is not None and not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("TransmissionPlan.session_handle: SessionHandle이어야 합니다")


@dataclass(frozen=True, slots=True)
class PacketDecodeAction:
    """MAC이 수신 Packet의 decode를 지시하는 결과."""

    packet: RecoveredPacket
    accepted: bool
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.packet, RecoveredPacket):
            raise InvalidValueError("PacketDecodeAction.packet: RecoveredPacket이어야 합니다")
        if not isinstance(self.accepted, bool):
            raise InvalidValueError("PacketDecodeAction.accepted: bool이어야 합니다")
        if self.reason_code is not None and not isinstance(self.reason_code, str):
            raise InvalidValueError("PacketDecodeAction.reason_code: 문자열이어야 합니다")


@dataclass(frozen=True, slots=True)
class MacMessageInput:
    """decode된 Message를 MAC 상태 처리에 전달하는 입력."""

    decoded: DecodedMessages
    channel: PhysicalChannel

    def __post_init__(self) -> None:
        if not isinstance(self.decoded, DecodedMessages):
            raise InvalidValueError("MacMessageInput.decoded: DecodedMessages여야 합니다")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("MacMessageInput.channel: PhysicalChannel이어야 합니다")


@dataclass(frozen=True, slots=True)
class MacAction:
    """MAC 처리 결과 하나. Runtime은 이 지시대로 다음 Port만 호출한다."""

    kind: MacActionKind
    packet_id: PacketId | None = None
    session_handle: SessionHandle | None = None
    presentation_data: bytes | None = None
    route: RouteAddresses | None = None
    details: Details = field(default=EMPTY_DETAILS)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MacActionKind):
            raise InvalidValueError("MacAction.kind: MacActionKind여야 합니다")
        if self.packet_id is not None and not isinstance(self.packet_id, PacketId):
            raise InvalidValueError("MacAction.packet_id: PacketId여야 합니다")
        if self.session_handle is not None and not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("MacAction.session_handle: SessionHandle이어야 합니다")
        if self.presentation_data is not None and not isinstance(self.presentation_data, bytes):
            raise InvalidValueError("MacAction.presentation_data: bytes여야 합니다")
        if self.route is not None and not isinstance(self.route, RouteAddresses):
            raise InvalidValueError("MacAction.route: RouteAddresses여야 합니다")
        if not isinstance(self.details, Details):
            raise InvalidValueError("MacAction.details: Details여야 합니다")
        if self.kind is MacActionKind.DELIVER_PRESENTATION_DATA:
            if self.presentation_data is None or self.route is None:
                raise InvalidValueError(
                    "MacAction: DELIVER_PRESENTATION_DATA에는 route와 presentation_data가 필요합니다"
                )


@dataclass(frozen=True, slots=True)
class MacActionSet:
    actions: tuple[MacAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.actions, tuple) or any(
            not isinstance(action, MacAction) for action in self.actions
        ):
            raise InvalidValueError("MacActionSet.actions: MacAction tuple이어야 합니다")


@dataclass(frozen=True, slots=True)
class SessionControlRequest:
    """로컬 `session_handle` 수명 지시. wire Session ID와 무관하다."""

    kind: SessionControlKind
    session_handle: SessionHandle | None = None
    transfer_id: TransferId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SessionControlKind):
            raise InvalidValueError("SessionControlRequest.kind: SessionControlKind여야 합니다")
        if self.session_handle is not None and not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("SessionControlRequest.session_handle 형식이 잘못되었습니다")
        if self.transfer_id is not None and not isinstance(self.transfer_id, TransferId):
            raise InvalidValueError("SessionControlRequest.transfer_id 형식이 잘못되었습니다")
        if self.kind is not SessionControlKind.OPEN and self.session_handle is None:
            raise InvalidValueError("SessionControlRequest: KEEP/CLOSE에는 session_handle이 필요합니다")


@dataclass(frozen=True, slots=True)
class SessionControlResult:
    kind: SessionControlKind
    session_handle: SessionHandle
    active: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SessionControlKind):
            raise InvalidValueError("SessionControlResult.kind: SessionControlKind여야 합니다")
        if not isinstance(self.session_handle, SessionHandle):
            raise InvalidValueError("SessionControlResult.session_handle 형식이 잘못되었습니다")
        if not isinstance(self.active, bool):
            raise InvalidValueError("SessionControlResult.active: bool이어야 합니다")
