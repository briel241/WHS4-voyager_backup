"""
RocAgent - 원격운항센터 쪽 행위자

하는 일
    1. 수신한 위치보고로 선박별 항적(track)을 갱신한다.
    2. 항로 지시를 만들어 내보내고 응답을 기다린다.
    3. 위치보고가 끊기면 통신 두절로 판정한다.

통신 두절 판정
    별도 heartbeat 메시지를 두지 않는다. AIS 위치보고는 원래 주기적으로
    반복 송신되는 메시지이므로 그 도착 자체가 생존 신호다.
    근거는 docs/mass_roc_message_set.md 7.3.

ACK 대기
    Type 92의 링크 계층 ACK(전파가 도달했다)와 여기서 다루는 응용 ACK
    (명령을 수락했다)는 서로 다른 층위다. 이 행위자는 응용 ACK만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common import config
from common.codec import decode, encode
from common.messages import ais_auth
from common.messages import application_ack as ack
from common.messages import navigational_warning as warn
from common.messages import position_report as pos
from common.messages import route_instruction as route
from common.security import default_backend, fingerprint_for_public_key
from common.outbound import DATA_TYPE_AIS_AUTH_INTEGRITY, Outbound

# Message Linkage ID는 1..1023이다 (SN.1/Circ.289 Table 13.2). 0은 not available.
_LINKAGE_ID_MIN = 1
_LINKAGE_ID_MAX = 1023

# 메시지 종류별 canonical 검사. 각 메시지 모듈이 자기 규칙을 갖는다.
_CANONICAL_CHECKS = {
    pos.PositionReport: pos.canonical_violation,
    ack.ApplicationAck: ack.canonical_violation,
    route.RouteInstruction: route.canonical_violation,
}


# 한 선박에 대해 마지막으로 관측한 상태
@dataclass
class Track:
    source_id: int
    report: pos.PositionReport
    updated_at: float
    report_count: int = 1


# 내보낸 명령 하나의 진행 상태
@dataclass
class PendingCommand:
    linkage_id: int
    destination_id: int
    issued_at: float
    status: int | None = None
    reason: int = ack.REASON_NONE
    answered_at: float | None = None

    @property
    def answered(self) -> bool:
        return self.status is not None


@dataclass
class RocAgent:
    mmsi: int = config.ROC_MMSI
    report_timeout: float = config.POSITION_REPORT_TIMEOUT_SEC

    def __post_init__(self) -> None:
        self._backend = default_backend()
        self._private_key, self._public_key = self._backend.generate_keypair()
        self._fingerprint = fingerprint_for_public_key(
            self._backend.public_bytes(self._public_key)
        )
        self.tracks: dict[int, Track] = {}
        self.commands: dict[int, PendingCommand] = {}
        self._next_linkage_id = _LINKAGE_ID_MIN
        self.ignored_count = 0
        # 해석은 됐지만 전선에 실을 수 없는 값이라 버린 수.
        self.rejected_count = 0
        # 명령 수신자가 아닌 국에서 온 ACK 수. 오귀속·위조 관찰용.
        self.mismatched_ack_count = 0
        self._lost: set[int] = set()

    # ------------------------------------------------------------ 수신 처리

    # payload 하나를 처리한다. ROC는 수신에 대해 응용 응답을 만들지 않으므로
    # 반환값은 항상 비어 있지만, 다른 행위자와 인터페이스를 맞춘다.
    def receive(self, payload: bytes, source_id: int, now: float = 0.0) -> list[Outbound]:
        try:
            message = decode(payload)
        except ValueError:
            self.ignored_count += 1
            return []

        # 전선에 실을 수 없는 값이면 관측에도 상관에도 쓰지 않는다.
        #
        # 관측만 하는 쪽에도 필요하다. 위도 raw 60,000,000은 100도가 되어 해도에
        # 그려지고, 그 해도가 육상 운영자의 상황 인식 수단이다. 예약 Status를 받으면
        # 무효한 응답 한 건이 대기 중인 명령을 종결시킨다.
        #
        # 검사 규칙은 승인된 SC-VDE codec과 같다. 그 codec은 무선 경로만 막으므로
        # 직결 전송·시험 주입은 여기서 걸러야 한다 (M.2092-2 §A4-4.15).
        check = _CANONICAL_CHECKS.get(type(message))
        if check is not None and check(message) is not None:
            self.rejected_count += 1
            return []

        if isinstance(message, pos.PositionReport):
            self._update_track(message, source_id, now)
        elif isinstance(message, ack.ApplicationAck):
            self._resolve_command(message, source_id, now)

        return []

    def _update_track(self, report: pos.PositionReport, source_id: int, now: float) -> None:
        existing = self.tracks.get(source_id)
        count = existing.report_count + 1 if existing else 1
        self.tracks[source_id] = Track(
            source_id=source_id, report=report, updated_at=now, report_count=count
        )
        self._lost.discard(source_id)

    # 응용 ACK을 원 명령에 결속한다.
    #
    # Linkage ID만으로 찾으면 안 된다. 명령을 보낸 상대가 아닌 국이 같은 ID로
    # 답해도 결속되어, 배가 수락하지 않은 항로를 수락한 것으로 기록된다.
    # 선박이 둘 이상이면 공격이 없어도 오귀속이 일어난다. 그래서 ACK을 보낸
    # 국이 그 명령의 수신자였는지 함께 본다.
    def _resolve_command(
        self, message: ack.ApplicationAck, source_id: int, now: float
    ) -> None:
        command = self.commands.get(message.linkage_id)
        if command is None:
            # 우리가 내보내지 않은 Linkage ID에 대한 응답이다.
            self.ignored_count += 1
            return
        if command.destination_id != source_id:
            # 명령을 받은 국이 아닌 곳에서 온 ACK다.
            self.mismatched_ack_count += 1
            return
        command.status = message.status
        command.reason = message.reason
        command.answered_at = now

    # ------------------------------------------------------------ 명령 발행

    # 항로 지시를 만들어 송신 요청으로 돌려준다.
    def issue_route(
        self,
        destination_id: int,
        waypoints_degrees: list[tuple[float, float] | route.Waypoint],
        now: float = 0.0,
        route_type: int = route.ROUTE_TYPE_MANDATORY,
        duration: int = route.DURATION_NOT_AVAILABLE,
        heading_setpoint: float | None = None,
        fallback_state: int = route.FALLBACK_NONE,
    ) -> Outbound:
        # heading setpoint·fallback은 S-421 조작 데이터의 제어 필드다(선택).
        # waypoint별 turn radius·계획 SOG가 필요하면 waypoint_from_degrees로 만든
        # Waypoint를 (위도, 경도) 짝 대신 그대로 넣는다.
        instruction = route.RouteInstruction(
            waypoints=[
                wp if isinstance(wp, route.Waypoint)
                else route.waypoint_from_degrees(wp[1], wp[0])
                for wp in waypoints_degrees
            ],
            linkage_id=self._take_linkage_id(),
            sender_classification=route.SENDER_AUTHORITY,
            route_type=route_type,
            duration=duration,
            heading_setpoint=route.heading_setpoint_from_degrees(heading_setpoint),
            fallback_state=fallback_state,
        )
        return self._send(instruction, destination_id, now, "Route Instruction")

    # 앞서 내보낸 항로를 취소한다.
    def cancel_route(
        self, destination_id: int, linkage_id: int, now: float = 0.0
    ) -> Outbound:
        instruction = route.RouteInstruction(
            linkage_id=linkage_id,
            sender_classification=route.SENDER_AUTHORITY,
            route_type=route.ROUTE_TYPE_CANCELLATION,
        )
        return self._send(instruction, destination_id, now, "Route Cancellation")

    def _send(
        self,
        instruction: route.RouteInstruction,
        destination_id: int,
        now: float,
        label: str,
    ) -> Outbound:
        self.commands[instruction.linkage_id] = PendingCommand(
            linkage_id=instruction.linkage_id,
            destination_id=destination_id,
            issued_at=now,
        )
        # 항로 지시는 ACK가 필요하므로 Type 92가 되어야 하고, 따라서 broadcast일 수 없다.
        return Outbound(
            payload=encode(instruction),
            destination_id=destination_id,
            ack_required=True,
            label=label,
        )

    # ------------------------------------------------ SC-VDE-003 AIS 인증값

    # 검증에 쓸 공개키와 인증서 지문. 실제 배포는 SC-VDE-001이 담당한다.
    @property
    def public_key(self):
        return self._public_key

    @property
    def fingerprint(self) -> bytes:
        return self._fingerprint

    # AIS 채널로 관측한 AIS 메시지에 대한 인증·무결성 값을 만든다.
    #
    # 브로드캐스트로 내보낸다. AIS 메시지 자체가 브로드캐스트이므로
    # 그 인증값도 같은 범위에 닿아야 한다.
    def issue_ais_auth(
        self,
        target_mmsi: int,
        ais_message_type: int,
        ais_message: bytes,
        now: float = 0.0,
        valid_until: int = ais_auth.VALID_UNTIL_NONE,
    ) -> Outbound:
        value = ais_auth.build(
            private_key=self._private_key,
            fingerprint=self._fingerprint,
            signer_mmsi=self.mmsi,
            target_mmsi=target_mmsi,
            ais_message_type=ais_message_type,
            ais_message=ais_message,
            valid_until=valid_until,
            backend=self._backend,
        )
        return Outbound(
            payload=encode(value),
            destination_id=0,
            ack_required=False,
            data_type=DATA_TYPE_AIS_AUTH_INTEGRITY,
            content_type="application/vnd.vdes.ais-auth",
            label="AIS Auth Value",
        )

    # ------------------------------------------------ 항행 경보 방송 (S-124)

    # 항행 경보를 브로드캐스트로 내보낸다.
    #
    # 실제 표준에서는 연안국(coastal authority)이 발신하지만, 본 시뮬레이션에서
    # ROC가 육상국 역할을 겸한다. 경보는 모든 선박에 닿아야 하므로 브로드캐스트이며,
    # 방송이므로 응용 ACK를 요구하지 않는다(Type 93).
    def issue_warning(
        self, warning: warn.NavigationalWarning, now: float = 0.0
    ) -> Outbound:
        return Outbound(
            payload=encode(warning),
            destination_id=0,
            ack_required=False,
            label=f"Nav Warning {warning.year}-{warning.warning_number}",
        )

    def _take_linkage_id(self) -> int:
        linkage_id = self._next_linkage_id
        self._next_linkage_id += 1
        if self._next_linkage_id > _LINKAGE_ID_MAX:
            self._next_linkage_id = _LINKAGE_ID_MIN
        return linkage_id

    # ------------------------------------------------------------ 시간 진행

    # 시간을 진행시키고 통신 두절 여부를 갱신한다.
    def tick(self, now: float) -> list[Outbound]:
        for source_id, track in self.tracks.items():
            if now - track.updated_at > self.report_timeout:
                self._lost.add(source_id)
        return []

    # 현재 통신이 두절된 것으로 판정된 선박 목록
    @property
    def link_lost(self) -> list[int]:
        return sorted(self._lost)

    # 대기 중인(아직 응답이 오지 않은) 명령 목록
    @property
    def unanswered(self) -> list[PendingCommand]:
        return [command for command in self.commands.values() if not command.answered]
