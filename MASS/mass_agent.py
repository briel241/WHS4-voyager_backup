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
from common.outbound import BROADCAST_ID, DATA_TYPE_ROUTE_CONTROL_ACK, Outbound

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
    # None이면 AIS Class A 표준 주기(속력·선회에 따라 가변)를 따른다.
    # 고정 주기가 필요하면 초 단위 값을 준다.
    report_interval: float | None = None
    max_turn_rate: float = DEFAULT_MAX_TURN_RATE
    arrival_radius: float = DEFAULT_ARRIVAL_RADIUS_NM

    # 항로 지시를 받아들일 송신국 판정. 기본값 None은 "누구든 받는다"이며,
    # 이는 VDE-TER 표준에 송신자 인증이 없다는 현재 상태를 그대로 재현한 것이다
    # (그 부재가 이 프로젝트의 연구 대상이다).
    #
    # 방어 기법을 실험할 때 여기에 판정 함수를 넣으면 거부 경로가 살아난다.
    #
    #     mass.authority_policy = lambda source_id, message: source_id == ROC_MMSI
    #
    # 반환이 False면 STATUS_ERRORS / REASON_NOT_AUTHORIZED로 거절한다.
    authority_policy: object = None

    # 제어권을 가진 국에서 이 시간(초) 동안 아무것도 오지 않으면 원격 제어를
    # 유지할 수 없다고 보고 fallback으로 전환한다(MASS Code). None이면 끈다.
    link_timeout: float | None = config.CONTROL_LINK_TIMEOUT_SEC

    # 연결이 끊겼을 때 들어갈 사전 정의 fallback. 실제로는 선박의 운항 한계와
    # 비상 계획이 정한다.
    link_loss_fallback: int = config.DEFAULT_LINK_LOSS_FALLBACK

    def __post_init__(self) -> None:
        self._waypoints: list[route.Waypoint] = []
        self._waypoint_index = 0
        self._route_linkage_id = route.LINKAGE_ID_NOT_AVAILABLE
        self._route_expires_at: float | None = None
        # 현재 항로를 발행한 국. 만료 통지를 보낼 곳이다.
        self._route_issuer: int | None = None
        # 원격 조종 — 지시받은 목표 침로(raw)와 반영 중인 fallback.
        self._heading_setpoint = route.HEADING_SETPOINT_NOT_AVAILABLE
        self._fallback_state = route.FALLBACK_NONE
        # 제어권을 가진 국과 마지막으로 그 국에서 무언가를 받은 시각.
        self._controlling_station: int | None = None
        self._last_control_contact: float | None = None
        self._leg_origin: tuple[float, float] | None = None
        self._rate_of_turn: float | None = None
        self._last_report_at: float | None = None
        self._last_tick_at: float | None = None
        # 마지막으로 적용한 지시 원문. 재전송으로 같은 지시가 다시 오면
        # 재적용하지 않고 ACK만 다시 보내기 위한 기준이다.
        self._applied_instruction: route.RouteInstruction | None = None
        self.rejected_count = 0
        self.ignored_count = 0
        # 같은 지시의 재전송을 재적용하지 않고 ACK만 돌려준 횟수.
        self.duplicate_count = 0
        # 원격 제어 중 링크가 끊겨 fallback으로 전환한 횟수.
        self.link_loss_count = 0
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

    # 지금 상태에서 다음 위치보고까지의 간격(초).
    #
    # report_interval을 명시로 주면 그 값을 쓰고(시험·시연용 고정 주기),
    # None이면 AIS Class A 표준 주기를 따른다.
    def _report_interval(self) -> float:
        if self.report_interval is not None:
            return self.report_interval

        sog = abs(self.state.sog)
        turning = (
            self._rate_of_turn is not None
            and abs(self._rate_of_turn) > config.TURNING_RATE_THRESHOLD
        )

        if sog <= config.SPEED_ANCHORED_MAX and not turning:
            return config.POSITION_REPORT_INTERVAL_ANCHORED
        if sog <= config.SPEED_SLOW_MAX:
            return (
                config.POSITION_REPORT_INTERVAL_SLOW_TURNING if turning
                else config.POSITION_REPORT_INTERVAL_SLOW
            )
        if sog <= config.SPEED_MEDIUM_MAX:
            return (
                config.POSITION_REPORT_INTERVAL_MEDIUM_TURNING if turning
                else config.POSITION_REPORT_INTERVAL_MEDIUM
            )
        return config.POSITION_REPORT_INTERVAL_FAST

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
            # AIS Timestamp는 보고를 만든 UTC 초(0-59)다. 이 행위자는 UTC를
            # 읽지 않고 tick(now)로 주입받은 시뮬레이션 경과 초를 쓰므로,
            # 값의 자릿수와 갱신 양상만 표준과 같고 절대 시각은 아니다.
            # 실장비 연동에서 UTC를 다루게 되면 여기를 바꾼다.
            timestamp=int(now) % 60,
            autonomy_mode=self.state.autonomy_mode,
        )

    # ------------------------------------------------------------ 시간 진행

    # 시간을 진행시키고 이번에 내보낼 송신 요청을 반환한다.
    def tick(self, now: float) -> list[Outbound]:
        if self._last_tick_at is not None:
            self._advance(now - self._last_tick_at)
        self._last_tick_at = now

        outbound = self._expire_route(now)
        self._check_control_link(now)
        self._expire_warnings(now)

        if self._last_report_at is None or now - self._last_report_at >= self._report_interval():
            self._last_report_at = now
            outbound.append(
                Outbound(
                    payload=encode(self.position_report(now)),
                    destination_id=BROADCAST_ID,
                    ack_required=False,
                    label="Position Report",
                )
            )
        return outbound

    # 원격 제어 링크가 살아 있는지 보고, 끊겼으면 사전 정의 fallback으로 간다.
    #
    # MASS Code는 정상 운용 조건을 유지할 수 없을 때 사전 정의된 fallback으로
    # 전환하도록 요구한다. 실제 운용에서도 연결이 기준 아래로 떨어지면 선박이
    # fallback에 들어가고 제어권이 본선으로 돌아온다.
    #
    # 이것이 없으면 원격 제어 중 링크가 끊겨도 배는 마지막 명령대로 계속
    # 움직이고, ROC는 두절을 알지만 배는 모르는 상태가 된다.
    def _check_control_link(self, now: float) -> bool:
        if self.link_timeout is None:
            return False
        if self.state.autonomy_mode != pos.AUTONOMY_RC:
            return False
        if self._last_control_contact is None:
            return False
        if now - self._last_control_contact < self.link_timeout:
            return False

        self.link_loss_count += 1
        self._apply_fallback(self.link_loss_fallback)
        # 제어권이 본선으로 돌아온다.
        self.state.autonomy_mode = pos.AUTONOMY_MON
        self._controlling_station = None
        self._last_control_contact = None
        return True

    # fallback을 실제 선박 상태에 반영한다 (MASS Code의 사전 정의 fallback 상태).
    #
    # 셋 다 "정해진 항로를 따라가기를 그만두고 그 자리를 유지한다"는 점이 같고,
    # 유지 방법이 다르다. 그래서 항행을 멈추고(속력 0) 항행상태를 바꾼다.
    #
    # Navigation Status는 ITU-R M.1371 Table 45를 쓴다. AIS에는 DP 전용 값이
    # 없어 DP는 "엔진으로 항행 중"(0)으로 둔다. 표류는 능동 유지를 하지 않으므로
    # "조종 불능"(2)으로 본다. 이 대응은 본 프로젝트가 정한 것이다.
    _FALLBACK_NAV_STATUS = {
        route.FALLBACK_ANCHORING: 1,             # at anchor
        route.FALLBACK_DYNAMIC_POSITIONING: 0,   # under way using engine (위치 유지)
        route.FALLBACK_DRIFTING: 2,              # not under command
    }

    def _apply_fallback(self, state: int) -> None:
        self._fallback_state = state
        if state == route.FALLBACK_NONE:
            self.state.navigation_status = 0
            return

        # 항행을 그만둔다. 남은 항로와 목표 침로는 더 이상 유효하지 않다.
        self.state.sog = 0.0
        self._heading_setpoint = route.HEADING_SETPOINT_NOT_AVAILABLE
        self._waypoints = []
        self._waypoint_index = 0
        self._rate_of_turn = 0.0
        self.state.navigation_status = self._FALLBACK_NAV_STATUS.get(state, 0)

    @property
    def fallback_state(self) -> int:
        return self._fallback_state

    @property
    def fallback_name(self) -> str:
        return route.FALLBACK_NAMES.get(self._fallback_state, "?")

    @property
    def heading_setpoint_degrees(self) -> float | None:
        if self._heading_setpoint == route.HEADING_SETPOINT_NOT_AVAILABLE:
            return None
        return self._heading_setpoint / route.HEADING_SETPOINT_SCALE

    # 지금 무엇을 향해 조타해야 하는지 정한다. None이면 조타할 목표가 없다.
    #
    # 우선순위
    #   1) fallback이 걸려 있으면 항행하지 않는다 (투묘·DP·표류).
    #   2) 목표 침로(Heading Setpoint)를 받았으면 그 침로를 잡는다.
    #      원격 조종은 waypoint가 아니라 침로로 조타하는 것이다.
    #   3) 그 밖에는 다음 waypoint 방위를 따른다.
    def _steering_target(self) -> float | None:
        if self._fallback_state != route.FALLBACK_NONE:
            return None
        if self._heading_setpoint != route.HEADING_SETPOINT_NOT_AVAILABLE:
            return self._heading_setpoint / route.HEADING_SETPOINT_SCALE
        if self.has_route:
            waypoint = self._waypoints[self._waypoint_index]
            return navigation.bearing_degrees(
                self.state.latitude,
                self.state.longitude,
                waypoint.latitude_degrees,
                waypoint.longitude_degrees,
            )
        return None

    # 항로를 따라 침로와 위치를 갱신한다.
    def _advance(self, elapsed: float) -> None:
        if elapsed <= 0:
            return

        target = self._steering_target()
        if target is not None:
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
    # 유효기간이 다한 항로를 버리고, 발행자에게 만료를 알린다.
    #
    # 통지가 없으면 ROC는 배가 아직 항로를 따라간다고 믿는다. 이는 배경지식
    # 2.4.5가 지적한 상태 불일치와 같은 형태이므로 알려 준다. S-421 ACK을
    # 재사용하되 원 지시의 linkage_id를 실어 상관할 수 있게 한다.
    def _expire_route(self, now: float) -> list[Outbound]:
        if self._route_expires_at is None or now < self._route_expires_at:
            return []

        linkage_id = self._route_linkage_id
        issuer = self._route_issuer
        self._clear_route()
        if issuer is None:
            return []
        return [
            Outbound(
                payload=encode(
                    ack.ApplicationAck(
                        linkage_id=linkage_id,
                        status=ack.STATUS_ROUTE_ISSUES,
                        reason=ack.REASON_EXPIRED,
                    )
                ),
                destination_id=issuer,
                ack_required=False,
                data_type=DATA_TYPE_ROUTE_CONTROL_ACK,
                label="Route Expired",
            )
        ]

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
        # 제어권을 가진 국에서 온 것이면 링크가 살아 있다는 신호다.
        # 해석에 실패해도 그 국에서 무언가 도착했다는 사실은 유효하다.
        if self._controlling_station is not None and source_id == self._controlling_station:
            self._last_control_contact = now

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
        decision = self._evaluate(message, source_id)

        # 같은 발행자가 같은 지시를 다시 보내면 ACK 유실에 대비한 재전송이다.
        # 재적용하면 항로 진행(waypoint index)이 처음으로 되돌아가므로,
        # 상태는 그대로 두고 ACK만 다시 보낸다.
        duplicate = (
            decision.accept
            and self._applied_instruction is not None
            and message == self._applied_instruction
            and source_id == self._route_issuer
        )

        # 검증이 끝난 뒤에만 상태를 바꾼다.
        if decision.cancel:
            self._clear_route()
        elif duplicate:
            self.duplicate_count += 1
        elif decision.accept:
            self._apply_route(message, now, source_id)
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
    def _evaluate(
        self, message: route.RouteInstruction, source_id: int = 0
    ) -> RouteDecision:
        # ① 권한 — 정책이 설정된 경우에만 판정한다(기본값은 누구든 허용).
        if self.authority_policy is not None and not self.authority_policy(
            source_id, message
        ):
            return RouteDecision(
                status=ack.STATUS_ERRORS, reason=ack.REASON_NOT_AUTHORIZED
            )

        if message.is_cancellation:
            return RouteDecision(status=ack.STATUS_ACKNOWLEDGED, cancel=True)

        # ② 스키마 — 예약값이 들어오면 해석할 수 없다. 누락이 아니라 위반이므로
        #    INCOMPLETE가 아니라 ERRORS다.
        if message.sender_classification not in route.SENDER_NAMES:
            return RouteDecision(
                status=ack.STATUS_ERRORS, reason=ack.REASON_ROUTE_UNSUITABLE
            )
        if message.fallback_state not in route.FALLBACK_NAMES:
            return RouteDecision(
                status=ack.STATUS_ERRORS, reason=ack.REASON_ROUTE_UNSUITABLE
            )
        for waypoint in message.waypoints:
            if waypoint.leg_geometry not in route.LEG_GEOMETRY_NAMES:
                return RouteDecision(
                    status=ack.STATUS_ERRORS, reason=ack.REASON_ROUTE_UNSUITABLE
                )

        # ③ 누락 — 따라가는 데 필요한 것이 없다.
        #
        # 다만 원격 조종은 waypoint 없이도 성립한다. 목표 침로나 fallback을
        # 지시하는 것 자체가 조종이며, 그때는 따라갈 항로가 없어도 된다.
        controls = (
            message.heading_setpoint != route.HEADING_SETPOINT_NOT_AVAILABLE
            or message.fallback_state != route.FALLBACK_NONE
        )
        if not message.waypoints and not controls:
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

        # ④ 경로 문제 — 해석은 되지만 따라갈 수 없는 형상.
        #    연속한 두 waypoint가 같은 지점이면 그 구간의 침로가 정의되지 않는다.
        for previous, current in zip(message.waypoints, message.waypoints[1:]):
            if (
                previous.latitude == current.latitude
                and previous.longitude == current.longitude
            ):
                return RouteDecision(
                    status=ack.STATUS_ROUTE_ISSUES, reason=ack.REASON_ROUTE_UNSUITABLE
                )

        return RouteDecision(status=ack.STATUS_ACKNOWLEDGED, accept=True)

    # 검증을 통과한 항로를 적용한다.
    def _apply_route(
        self, message: route.RouteInstruction, now: float, issuer: int | None = None
    ) -> None:
        self._waypoints = list(message.waypoints)
        self._waypoint_index = 0
        self._route_linkage_id = message.linkage_id
        self._route_issuer = issuer
        self._applied_instruction = message
        # 수직 통과 판정의 기준점은 항로를 받아들인 지점이다.
        self._leg_origin = (self.state.latitude, self.state.longitude)

        # Duration은 유효 종료까지의 분(分)이다. 시험에서는 수락 시각을 기준으로 센다.
        # 표준은 Start Date/Time을 기준으로 하지만, 여기서는 UTC 절대시각을
        # 다루지 않으므로 단순화한다.
        # duration 0(DURATION_CANCEL)은 Table 13.2가 정한 취소 표현이라
        # _evaluate가 이미 취소로 처리했다. not available이면 무기한이다.
        if message.duration == route.DURATION_NOT_AVAILABLE:
            self._route_expires_at = None
        else:
            self._route_expires_at = now + message.duration * 60.0

        # ── 조종 데이터를 실제로 반영한다 (SC-VDE-006) ──────────────────
        #
        # 목표 침로와 fallback은 지시받은 값을 그대로 쓴다. waypoint의 계획
        # 속력은 첫 구간 값을 명령 속력으로 본다. 이 값들이 실제로 배를
        # 움직이지 않으면 조종 명령을 실증할 수 없다.
        self._heading_setpoint = message.heading_setpoint

        first = message.waypoints[0] if message.waypoints else None
        if first is not None and first.planned_sog_knots is not None:
            self.state.sog = first.planned_sog_knots

        # fallback은 항행을 멈추므로 마지막에 적용한다(위 설정을 덮어쓴다).
        self._apply_fallback(message.fallback_state)

        # 제어권 — 조종 필드를 받았으면 원격 제어, 순수 노선이면 감독 자율.
        remote = (
            self._heading_setpoint != route.HEADING_SETPOINT_NOT_AVAILABLE
            or message.fallback_state != route.FALLBACK_NONE
        )
        if remote:
            self.state.autonomy_mode = pos.AUTONOMY_RC
            self._controlling_station = issuer
            self._last_control_contact = now
        else:
            self.state.autonomy_mode = pos.AUTONOMY_MON
            self._controlling_station = None
            self._last_control_contact = None

        self._check_arrival()

    def _clear_route(self) -> None:
        self._waypoints = []
        self._waypoint_index = 0
        self._route_linkage_id = route.LINKAGE_ID_NOT_AVAILABLE
        self._route_expires_at = None
        self._route_issuer = None
        self._applied_instruction = None
        self._leg_origin = None
        self._heading_setpoint = route.HEADING_SETPOINT_NOT_AVAILABLE
