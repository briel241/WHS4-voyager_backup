"""
MassAgent - 자율운항선박 쪽 행위자

하는 일
    1. 항로를 따라 실제로 움직인다(추측항법).
    2. 주기적으로 위치보고를 내보낸다.
    3. 항로 지시를 받으면 검증하고 응용 ACK로 답한다.

시간
    내부에서 시계를 읽지 않고 tick(now)로 주입받는다. 시험을 결정적으로
    만들기 위해서다. now는 초 단위 단조 증가 값이면 무엇이든 된다.

검증 순서
    docs/mass_roc_background.md 6.3의 원칙을 지금부터 구조로 반영한다.
    "수신 -> 검증 -> (통과한 경우에만) 상태 변경" 순서이며, 검증에 실패한
    메시지는 항로에 아무 영향을 주지 않는다. 인증은 후속 단계에서 이 검증
    자리에 추가된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common import config, navigation
from common.codec import decode, encode
from common.messages import ais_auth
from common.messages import application_ack as ack
from common.messages import navigational_warning as warn
from common.messages import position_report as pos
from common.messages import route_instruction as route
from common.outbound import BROADCAST_ID, Outbound

# 한 waypoint에 이 거리 안으로 들어오면 통과한 것으로 본다.
DEFAULT_ARRIVAL_RADIUS_NM = 0.05

# 선회율 상한(도/분). 실제 선박의 통상 범위를 따른다.
DEFAULT_MAX_TURN_RATE = 20.0


@dataclass
class ShipState:
    latitude: float
    longitude: float
    heading: float = 0.0
    sog: float = 0.0
    navigation_status: int = 0  # 0 = under way using engine
    autonomy_mode: int = pos.AUTONOMY_MON


# 수신한 항로 지시를 어떻게 처리할지 정한 결과.
# 상태를 바꾸기 전에 먼저 만들고, 통과한 경우에만 적용한다.
@dataclass
class RouteDecision:
    status: int
    reason: int = ack.REASON_NONE
    accept: bool = False
    cancel: bool = False


# AIS 인증값 검증 결과 한 건 (SC-VDE-003)
@dataclass
class AisAuthResult:
    time: float
    target_mmsi: int
    ais_message_type: int
    verified: bool
    reason: str


# 반영 중인 항행 경보 한 건 (S-124)
@dataclass
class ActiveWarning:
    warning: warn.NavigationalWarning
    received_at: float
    expires_at: float | None  # None이면 무기한


@dataclass
class MassAgent:
    mmsi: int = config.MASS_MMSI
    state: ShipState = field(
        default_factory=lambda: ShipState(latitude=35.0, longitude=129.0)
    )
    report_interval: float = config.POSITION_REPORT_INTERVAL_SEC
    max_turn_rate: float = DEFAULT_MAX_TURN_RATE
    arrival_radius: float = DEFAULT_ARRIVAL_RADIUS_NM

    def __post_init__(self) -> None:
        self._waypoints: list[route.Waypoint] = []
        self._waypoint_index = 0
        self._route_linkage_id = route.LINKAGE_ID_NOT_AVAILABLE
        self._route_expires_at: float | None = None
        self._leg_origin: tuple[float, float] | None = None
        self._rate_of_turn: float | None = None
        self._last_report_at: float | None = None
        self._last_tick_at: float | None = None
        self.rejected_count = 0
        self.ignored_count = 0
        # SC-VDE-003 — AIS 채널로 들은 메시지와 신뢰하는 공개키
        self._heard_ais: dict[tuple[int, int], bytes] = {}
        self._trusted: dict[bytes, tuple[object, int]] = {}
        self.ais_results: list[AisAuthResult] = []
        # S-124 — 반영 중인 항행 경보. (연도, 경보번호)로 구분한다.
        self._warnings: dict[tuple[int, int], ActiveWarning] = {}

    # ------------------------------------------------------------ 상태 조회

    @property
    def has_route(self) -> bool:
        return self._waypoint_index < len(self._waypoints)

    @property
    def route_linkage_id(self) -> int:
        return self._route_linkage_id

    @property
    def active_waypoint(self) -> route.Waypoint | None:
        if not self.has_route:
            return None
        return self._waypoints[self._waypoint_index]

    @property
    def remaining_waypoints(self) -> int:
        return max(len(self._waypoints) - self._waypoint_index, 0)

    # 현재 상태로 위치보고를 만든다.
    def position_report(self, now: float = 0.0) -> pos.PositionReport:
        return pos.from_measurements(
            longitude_degrees=self.state.longitude,
            latitude_degrees=self.state.latitude,
            sog_knots=self.state.sog,
            cog_degrees=self.state.heading,
            heading_degrees=round(self.state.heading) % 360,
            navigation_status=self.state.navigation_status,
            rate_of_turn=navigation.rot_to_ais(self._rate_of_turn),
            timestamp=int(now) % 60,
            autonomy_mode=self.state.autonomy_mode,
        )

    # ------------------------------------------------------------ 시간 진행

    # 시간을 진행시키고 이번에 내보낼 송신 요청을 반환한다.
    def tick(self, now: float) -> list[Outbound]:
        if self._last_tick_at is not None:
            self._advance(now - self._last_tick_at)
        self._last_tick_at = now

        self._expire_route(now)
        self._expire_warnings(now)

        if self._last_report_at is None or now - self._last_report_at >= self.report_interval:
            self._last_report_at = now
            return [
                Outbound(
                    payload=encode(self.position_report(now)),
                    destination_id=BROADCAST_ID,
                    ack_required=False,
                    label="Position Report",
                )
            ]
        return []

    # 항로를 따라 침로와 위치를 갱신한다.
    def _advance(self, elapsed: float) -> None:
        if elapsed <= 0:
            return

        if self.has_route:
            waypoint = self._waypoints[self._waypoint_index]
            target = navigation.bearing_degrees(
                self.state.latitude,
                self.state.longitude,
                waypoint.latitude_degrees,
                waypoint.longitude_degrees,
            )
            max_change = self.max_turn_rate * (elapsed / 60.0)
            self.state.heading, change = navigation.turn_toward(
                self.state.heading, target, max_change
            )
            self._rate_of_turn = change / (elapsed / 60.0) if elapsed else 0.0
        else:
            self._rate_of_turn = 0.0

        distance = navigation.travelled_nm(self.state.sog, elapsed)
        if distance > 0:
            self.state.latitude, self.state.longitude = navigation.advance(
                self.state.latitude, self.state.longitude, self.state.heading, distance
            )

        self._check_arrival()

    # waypoint를 통과했으면 다음 waypoint로 넘어간다.
    def _check_arrival(self) -> None:
        while self.has_route:
            waypoint = self._waypoints[self._waypoint_index]
            if not self._passed(waypoint):
                return
            self._leg_origin = (waypoint.latitude_degrees, waypoint.longitude_degrees)
            self._waypoint_index += 1

    # waypoint를 통과했는지 판정한다.
    #
    # 도착 반경만으로 판정하면 선회 반경보다 가까운 waypoint를 지나쳤을 때
    # 영원히 쫓게 된다. 그래서 자동조타가 쓰는 수직 통과 판정을 함께 둔다.
    # 구간 침로(직전 지점 -> 목표)와 현재 목표 방위의 차이가 90도를 넘으면
    # 배가 waypoint를 지나 뒤로 보낸 것이므로 통과로 본다.
    def _passed(self, waypoint: route.Waypoint) -> bool:
        distance = navigation.distance_nm(
            self.state.latitude,
            self.state.longitude,
            waypoint.latitude_degrees,
            waypoint.longitude_degrees,
        )
        if distance <= self.arrival_radius:
            return True

        if self._leg_origin is None:
            return False

        leg_course = navigation.bearing_degrees(
            self._leg_origin[0],
            self._leg_origin[1],
            waypoint.latitude_degrees,
            waypoint.longitude_degrees,
        )
        to_waypoint = navigation.bearing_degrees(
            self.state.latitude,
            self.state.longitude,
            waypoint.latitude_degrees,
            waypoint.longitude_degrees,
        )
        return abs(navigation.heading_difference(leg_course, to_waypoint)) > 90.0

    # 유효기간이 지난 항로를 버린다.
    def _expire_route(self, now: float) -> None:
        if self._route_expires_at is not None and now >= self._route_expires_at:
            self._clear_route()

    # 유효기간이 지난 항행 경보를 버린다.
    def _expire_warnings(self, now: float) -> None:
        expired = [
            key
            for key, active in self._warnings.items()
            if active.expires_at is not None and now >= active.expires_at
        ]
        for key in expired:
            del self._warnings[key]

    # ------------------------------------------------------------ 수신 처리

    # payload 하나를 처리하고 이번에 내보낼 송신 요청을 반환한다.
    # 우리 응용의 payload가 아니면 조용히 무시한다.
    def receive(self, payload: bytes, source_id: int, now: float = 0.0) -> list[Outbound]:
        try:
            message = decode(payload)
        except ValueError:
            # 다른 응용의 payload이거나 잡음에서 나온 검출이다.
            self.ignored_count += 1
            return []

        if isinstance(message, route.RouteInstruction):
            return self._handle_route(message, source_id, now)

        if isinstance(message, ais_auth.AisAuthValue):
            return self._handle_ais_auth(message, now)

        if isinstance(message, warn.NavigationalWarning):
            return self._handle_warning(message, now)

        # 위치보고는 다른 MASS의 브로드캐스트다. 지금은 관측만 하고 응답하지 않는다.
        return []

    # ------------------------------------------------ SC-VDE-003 AIS 인증

    # AIS 채널로 들은 메시지를 기록해 둔다. 인증값은 이것을 대상으로 검증한다.
    def observe_ais(
        self, target_mmsi: int, ais_message_type: int, ais_message: bytes
    ) -> None:
        self._heard_ais[(target_mmsi, ais_message_type)] = bytes(ais_message)

    # 신뢰하는 공개키를 등록한다.
    #
    # 지금은 사전 탑재(out-of-band provisioning)를 전제한다. 무선으로 인증서를
    # 받아 이 저장소를 채우는 것이 SC-VDE-001의 일이며, 그때까지 신뢰 앵커는
    # 여기에 직접 넣는다. 표준도 CA 주체와 초기 provisioning을 정의하지 않는다.
    def trust(self, fingerprint: bytes, public_key, signer_mmsi: int) -> None:
        self._trusted[bytes(fingerprint)] = (public_key, signer_mmsi)

    @property
    def ais_verified_count(self) -> int:
        return sum(1 for r in self.ais_results if r.verified)

    @property
    def ais_failed_count(self) -> int:
        return sum(1 for r in self.ais_results if not r.verified)

    # AIS 인증값을 검증하고 결과를 남긴다.
    #
    # 검증 실패는 조용히 폐기하되 기록은 남긴다. A4-4.15가 "수신하지 않은 것처럼
    # 행동하라"고 요구하므로 상태를 바꾸지 않지만, 관찰 도구가 공격을 보여주려면
    # 기록이 필요하다.
    def _handle_ais_auth(
        self, value: ais_auth.AisAuthValue, now: float
    ) -> list[Outbound]:
        def record(verified: bool, reason: str) -> list[Outbound]:
            self.ais_results.append(
                AisAuthResult(
                    time=now,
                    target_mmsi=value.target_mmsi,
                    ais_message_type=value.ais_message_type,
                    verified=verified,
                    reason=reason,
                )
            )
            return []

        trusted = self._trusted.get(value.fingerprint)
        if trusted is None:
            # 모르는 인증서로 서명된 값이다. 신뢰 앵커가 없으면 검증할 수 없다.
            return record(False, "unknown-certificate")

        public_key, signer_mmsi = trusted
        original = self._heard_ais.get((value.target_mmsi, value.ais_message_type))
        if original is None:
            # 대상 AIS 메시지를 듣지 못했다. 검증할 원본이 없다.
            return record(False, "no-such-ais-message")

        ok, reason = ais_auth.verify(
            value, public_key, signer_mmsi, original, now=now
        )
        return record(ok, reason)

    # ------------------------------------------------ S-124 항행 경보 반영

    # 방송으로 받은 항행 경보를 반영(저장)한다. 방송이므로 응답하지 않는다.
    #
    # 같은 (연도, 경보번호)가 다시 오면 최신 내용으로 갱신한다. Duration은
    # 유효 종료까지의 분(分)이며, 항로와 마찬가지로 수신 시각을 기준으로 센다
    # (절대 UTC 시각을 다루지 않는 시뮬레이션이므로 단순화한다).
    def _handle_warning(
        self, message: warn.NavigationalWarning, now: float
    ) -> list[Outbound]:
        if message.duration == warn.DURATION_INDEFINITE:
            expires_at = None
        else:
            expires_at = now + message.duration * 60.0

        self._warnings[(message.year, message.warning_number)] = ActiveWarning(
            warning=message, received_at=now, expires_at=expires_at
        )
        return []

    # 현재 반영 중인 항행 경보 목록
    @property
    def active_warnings(self) -> list[warn.NavigationalWarning]:
        return [active.warning for active in self._warnings.values()]

    @property
    def warning_count(self) -> int:
        return len(self._warnings)

    # 자선이 원형 경보 구역 안에 들어와 있는 경보들을 돌려준다.
    #
    # geometry가 원(circle)이고 반경(extent)이 지정된 경보만 판정한다. 점·사각·
    # 폴리곤은 여기서 다루지 않는다. 항법을 바꾸지는 않고(회피는 별도 행위),
    # "배가 위험 구역 안에 있음을 안다"는 상황 인식만 노출한다.
    def affecting_warnings(self) -> list[warn.NavigationalWarning]:
        inside: list[warn.NavigationalWarning] = []
        for active in self._warnings.values():
            w = active.warning
            if w.geometry_type != warn.GEOMETRY_CIRCLE or w.extent_nm is None:
                continue
            if w.latitude_degrees is None or w.longitude_degrees is None:
                continue
            distance = navigation.distance_nm(
                self.state.latitude,
                self.state.longitude,
                w.latitude_degrees,
                w.longitude_degrees,
            )
            if distance <= w.extent_nm:
                inside.append(w)
        return inside

    # 항로 지시를 검증하고, 통과한 경우에만 적용한 뒤 ACK를 만든다.
    def _handle_route(
        self, message: route.RouteInstruction, source_id: int, now: float
    ) -> list[Outbound]:
        decision = self._evaluate(message)

        # 검증이 끝난 뒤에만 상태를 바꾼다.
        if decision.cancel:
            self._clear_route()
        elif decision.accept:
            self._apply_route(message, now)
        else:
            self.rejected_count += 1

        return [
            Outbound(
                payload=encode(
                    ack.ApplicationAck(
                        linkage_id=message.linkage_id,
                        status=decision.status,
                        reason=decision.reason,
                    )
                ),
                destination_id=source_id,
                ack_required=False,
                label=f"ACK {ack.STATUS_NAMES.get(decision.status, decision.status)}",
            )
        ]

    # 항로 지시를 받아들일지 판정한다. 여기서는 상태를 바꾸지 않는다.
    def _evaluate(self, message: route.RouteInstruction) -> RouteDecision:
        if message.is_cancellation:
            return RouteDecision(status=ack.STATUS_ACKNOWLEDGED, cancel=True)

        if not message.waypoints:
            return RouteDecision(
                status=ack.STATUS_INCOMPLETE, reason=ack.REASON_ROUTE_UNSUITABLE
            )

        if message.route_type == route.ROUTE_TYPE_NOT_AVAILABLE:
            return RouteDecision(
                status=ack.STATUS_INCOMPLETE, reason=ack.REASON_ROUTE_UNSUITABLE
            )

        # 좌표가 not available인 waypoint는 따라갈 수 없다.
        for waypoint in message.waypoints:
            if waypoint.latitude_degrees is None or waypoint.longitude_degrees is None:
                return RouteDecision(
                    status=ack.STATUS_INCOMPLETE, reason=ack.REASON_ROUTE_UNSUITABLE
                )

        return RouteDecision(status=ack.STATUS_ACKNOWLEDGED, accept=True)

    # 검증을 통과한 항로를 적용한다.
    def _apply_route(self, message: route.RouteInstruction, now: float) -> None:
        self._waypoints = list(message.waypoints)
        self._waypoint_index = 0
        self._route_linkage_id = message.linkage_id
        # 수직 통과 판정의 기준점은 항로를 받아들인 지점이다.
        self._leg_origin = (self.state.latitude, self.state.longitude)

        # Duration은 유효 종료까지의 분(分)이다. 시험에서는 수락 시각을 기준으로 센다.
        # 표준은 Start Date/Time을 기준으로 하지만, 여기서는 UTC 절대시각을
        # 다루지 않으므로 단순화한다.
        if message.duration in (route.DURATION_NOT_AVAILABLE, route.DURATION_CANCEL):
            self._route_expires_at = None
        else:
            self._route_expires_at = now + message.duration * 60.0

        self._check_arrival()

    def _clear_route(self) -> None:
        self._waypoints = []
        self._waypoint_index = 0
        self._route_linkage_id = route.LINKAGE_ID_NOT_AVAILABLE
        self._route_expires_at = None
        self._leg_origin = None
