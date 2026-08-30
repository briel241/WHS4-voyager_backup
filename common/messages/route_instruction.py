"""
Route Instruction - 항로 지시

근거
    IMO SN.1/Circ.289 Annex Table 13.2 "Route information - (addressed)",
    DAC = 001 / FI = 28의 응용 필드를 그대로 차용한다.
    Waypoint 구조는 같은 문서 Table 13.3이다.

    전송 계층 필드(Message ID, Source ID, Destination ID, IAI 등)는
    가져오지 않는다. VDE 링크 계층 Type 92 헤더가 이미 담고 있다.

    Sender Classification에 ship(0)과 authority(1)가 모두 정의되어 있으므로
    ROC 발신과 MASS 발신을 같은 메시지로 표현할 수 있다.
    즉 MASS<->MASS 시나리오에도 그대로 쓴다.

S-421 제어 필드
    ROC 조작 데이터(docs/mass_roc_control_and_ack_message_format.md)의 S-421 기반
    제어 항목을 좁은 링크에 맞게 비트로 추가한다.
    - Heading Setpoint / Fallback State: 논문 기반 제어 개념(§1.2)
    - waypoint별 Turn Radius / Planned SOG / Leg Geometry: S-421 필드
    좌표는 여전히 1/10,000분 정수로 싣는다(전송용). DMM/WGS84는 논리 표현이다.

필드 구성 (고정부 76 bit + waypoint 77 bit x n)
    [Message Linkage ID 10 | Sender Classification 3 | Route Type 5 |
     Start Month 4 | Start Day 5 | Start Hour 5 | Start Minute 6 |
     Duration 18 | Heading Setpoint 12 | Fallback State 3 |
     Number of Waypoints 5 |
     Waypoints n x (경도 28 | 위도 27 | Turn Radius 10 | Planned SOG 10 | Leg Geometry 2)]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import coordinates
from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 2
NAME = "Route Instruction"

LINKAGE_ID_BITS = 10
SENDER_CLASSIFICATION_BITS = 3
ROUTE_TYPE_BITS = 5
START_MONTH_BITS = 4
START_DAY_BITS = 5
START_HOUR_BITS = 5
START_MINUTE_BITS = 6
DURATION_BITS = 18
HEADING_SETPOINT_BITS = 12
FALLBACK_BITS = 3
WAYPOINT_COUNT_BITS = 5

TURN_RADIUS_BITS = 10
PLANNED_SOG_BITS = 10
LEG_GEOMETRY_BITS = 2

FIXED_BITS = (
    LINKAGE_ID_BITS
    + SENDER_CLASSIFICATION_BITS
    + ROUTE_TYPE_BITS
    + START_MONTH_BITS
    + START_DAY_BITS
    + START_HOUR_BITS
    + START_MINUTE_BITS
    + DURATION_BITS
    + HEADING_SETPOINT_BITS
    + FALLBACK_BITS
    + WAYPOINT_COUNT_BITS
)
WAYPOINT_BITS = (
    coordinates.LONGITUDE_BITS
    + coordinates.LATITUDE_BITS
    + TURN_RADIUS_BITS
    + PLANNED_SOG_BITS
    + LEG_GEOMETRY_BITS
)

# 표준이 정한 waypoint 개수 상한
MAX_WAYPOINTS = 16

# Heading Setpoint - 진북 기준 목표 침로, 0.1도 단위 (논문 기반 제어)
HEADING_SETPOINT_NOT_AVAILABLE = 3600
HEADING_SETPOINT_SCALE = 10

# Fallback State - 논문에 제시된 세 상태 (S-421 열거값 아님)
FALLBACK_NONE = 0
FALLBACK_ANCHORING = 1
FALLBACK_DYNAMIC_POSITIONING = 2
FALLBACK_DRIFTING = 3
FALLBACK_NAMES = {
    FALLBACK_NONE: "none",
    FALLBACK_ANCHORING: "Anchoring",
    FALLBACK_DYNAMIC_POSITIONING: "DynamicPositioning",
    FALLBACK_DRIFTING: "Drifting",
}

# Turn Radius - S-421 routeWaypointTurnRadius, 0.01 해리(NM) 단위
TURN_RADIUS_NOT_AVAILABLE = 1023
TURN_RADIUS_SCALE = 100

# Planned SOG - S-421 routeScheduleElementPlanSOG, 0.1 knot 단위
PLANNED_SOG_NOT_AVAILABLE = 1023
PLANNED_SOG_SCALE = 10

# 유효 raw 상한·순환값. not available 센티널과의 충돌을 막는다.
#
# 반올림 결과가 센티널과 같아지면 유효한 지시가 "지시 없음"으로 둔갑한다.
# 예: 침로 359.95도는 round(3599.5)=3600이 되어 HEADING_SETPOINT_NOT_AVAILABLE과
# 구별되지 않는다. 침로는 순환값이므로 3600을 0으로 되돌리고(359.95 ≈ 360 = 0),
# 선회반경·계획속력은 순환하지 않으므로 상한에 붙인다.
HEADING_SETPOINT_MODULO = 3600   # 0..3599 (0.1도 단위)
TURN_RADIUS_MAX = 1022           # 10.22 NM 이상
PLANNED_SOG_MAX = 1022           # 102.2 knots 이상

# Leg Geometry - S-421 routeWaypointLegGeometryType
LEG_GEOMETRY_NONE = 0
LEG_GEOMETRY_LEXODROME = 1   # 등각항로 (rhumb line)
LEG_GEOMETRY_ORTHODROME = 2  # 대권항로 (great circle)
LEG_GEOMETRY_NAMES = {
    LEG_GEOMETRY_NONE: "none",
    LEG_GEOMETRY_LEXODROME: "Lexodrome",
    LEG_GEOMETRY_ORTHODROME: "Orthodrome",
}

# Sender Classification (Table 13.2)
SENDER_SHIP = 0
SENDER_AUTHORITY = 1

SENDER_NAMES = {SENDER_SHIP: "ship", SENDER_AUTHORITY: "authority"}

# Route Type (Table 13.2)
ROUTE_TYPE_NOT_AVAILABLE = 0
ROUTE_TYPE_MANDATORY = 1
ROUTE_TYPE_RECOMMENDED = 2
ROUTE_TYPE_ALTERNATIVE = 3
ROUTE_TYPE_RECOMMENDED_THROUGH_ICE = 4
ROUTE_TYPE_SHIP_ROUTE_PLAN = 5
ROUTE_TYPE_CANCELLATION = 31

ROUTE_TYPE_NAMES = {
    ROUTE_TYPE_NOT_AVAILABLE: "not available",
    ROUTE_TYPE_MANDATORY: "mandatory route",
    ROUTE_TYPE_RECOMMENDED: "recommended route",
    ROUTE_TYPE_ALTERNATIVE: "alternative route",
    ROUTE_TYPE_RECOMMENDED_THROUGH_ICE: "recommended route through ice",
    ROUTE_TYPE_SHIP_ROUTE_PLAN: "ship route plan",
    ROUTE_TYPE_CANCELLATION: "cancellation",
}

# SC-VDE 전선 형식이 실을 수 있는 Route Type.
#
# 위 ROUTE_TYPE_NAMES는 IMO Table 13.2를 그대로 옮긴 것이고, 이쪽은 **우리가
# 링크 팀과 합의한 전선 형식**이 허용하는 부분집합이다. 3(alternative)과
# 4(through ice)는 표준에는 있으나 SC-VDE TEST 프로파일이 뺐으므로 실어 보내면
# 승인된 codec이 INVALID_REQUEST로 거부한다
# (VDETER `sc_vde_route_control.py` `_ALLOWED_ROUTE_TYPES`,
#  docs/전선형식_노선_SC-VDE-005.md).
SC_VDE_ROUTE_TYPES = frozenset(
    {
        ROUTE_TYPE_NOT_AVAILABLE,
        ROUTE_TYPE_MANDATORY,
        ROUTE_TYPE_RECOMMENDED,
        ROUTE_TYPE_SHIP_ROUTE_PLAN,
        ROUTE_TYPE_CANCELLATION,
    }
)

# not available 값 (Table 13.2)
LINKAGE_ID_NOT_AVAILABLE = 0
MONTH_NOT_AVAILABLE = 0
DAY_NOT_AVAILABLE = 0
HOUR_NOT_AVAILABLE = 24
MINUTE_NOT_AVAILABLE = 60
DURATION_NOT_AVAILABLE = 262_143
DURATION_CANCEL = 0

# Start Date/Time은 넷이 함께 not available이거나, 넷이 함께 정상 범위여야 한다.
# 섞이면(예: 6월인데 시각은 N/A) 언제부터 유효한지 정할 수 없다.
START_NOT_AVAILABLE = (
    MONTH_NOT_AVAILABLE,
    DAY_NOT_AVAILABLE,
    HOUR_NOT_AVAILABLE,
    MINUTE_NOT_AVAILABLE,
)


@dataclass
class Waypoint:
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE
    turn_radius: int = TURN_RADIUS_NOT_AVAILABLE      # 0.01 NM
    planned_sog: int = PLANNED_SOG_NOT_AVAILABLE      # 0.1 knot
    leg_geometry: int = LEG_GEOMETRY_NONE

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)

    @property
    def turn_radius_nm(self) -> float | None:
        return (
            None if self.turn_radius == TURN_RADIUS_NOT_AVAILABLE
            else self.turn_radius / TURN_RADIUS_SCALE
        )

    @property
    def planned_sog_knots(self) -> float | None:
        return (
            None if self.planned_sog == PLANNED_SOG_NOT_AVAILABLE
            else self.planned_sog / PLANNED_SOG_SCALE
        )

    @property
    def leg_geometry_name(self) -> str:
        return LEG_GEOMETRY_NAMES.get(self.leg_geometry, f"reserved({self.leg_geometry})")


# 목표 침로(도)를 raw로 바꾼다. None이면 not available이다.
#
# 침로는 순환값이므로 360도는 0도와 같다. 그대로 반올림하면 359.95도가
# 3600(= not available)이 되어 "침로 지시 없음"으로 둔갑하므로 modulo를 쓴다.
def heading_setpoint_from_degrees(degrees: float | None) -> int:
    if degrees is None:
        return HEADING_SETPOINT_NOT_AVAILABLE
    return round(degrees * HEADING_SETPOINT_SCALE) % HEADING_SETPOINT_MODULO


# 도 단위 좌표로 Waypoint를 만든다. 제어 필드는 실측값(NM·knot)으로 받는다.
def waypoint_from_degrees(
    longitude_degrees: float,
    latitude_degrees: float,
    turn_radius_nm: float | None = None,
    planned_sog_knots: float | None = None,
    leg_geometry: int = LEG_GEOMETRY_NONE,
) -> Waypoint:
    return Waypoint(
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
        turn_radius=(
            TURN_RADIUS_NOT_AVAILABLE if turn_radius_nm is None
            else min(max(round(turn_radius_nm * TURN_RADIUS_SCALE), 0), TURN_RADIUS_MAX)
        ),
        planned_sog=(
            PLANNED_SOG_NOT_AVAILABLE if planned_sog_knots is None
            else min(max(round(planned_sog_knots * PLANNED_SOG_SCALE), 0), PLANNED_SOG_MAX)
        ),
        leg_geometry=leg_geometry,
    )


@dataclass
class RouteInstruction:
    waypoints: list[Waypoint] = field(default_factory=list)
    linkage_id: int = LINKAGE_ID_NOT_AVAILABLE
    sender_classification: int = SENDER_AUTHORITY
    route_type: int = ROUTE_TYPE_MANDATORY
    start_month: int = MONTH_NOT_AVAILABLE
    start_day: int = DAY_NOT_AVAILABLE
    start_hour: int = HOUR_NOT_AVAILABLE
    start_minute: int = MINUTE_NOT_AVAILABLE
    duration: int = DURATION_NOT_AVAILABLE
    heading_setpoint: int = HEADING_SETPOINT_NOT_AVAILABLE  # 0.1도
    fallback_state: int = FALLBACK_NONE

    def __post_init__(self) -> None:
        if len(self.waypoints) > MAX_WAYPOINTS:
            raise ValueError(
                f"Waypoint는 최대 {MAX_WAYPOINTS}개입니다 ({len(self.waypoints)}개)"
            )

    @property
    def route_type_name(self) -> str:
        return ROUTE_TYPE_NAMES.get(self.route_type, f"reserved({self.route_type})")

    @property
    def sender_name(self) -> str:
        return SENDER_NAMES.get(self.sender_classification, f"reserved({self.sender_classification})")

    @property
    def heading_setpoint_degrees(self) -> float | None:
        return (
            None if self.heading_setpoint == HEADING_SETPOINT_NOT_AVAILABLE
            else self.heading_setpoint / HEADING_SETPOINT_SCALE
        )

    @property
    def fallback_name(self) -> str:
        return FALLBACK_NAMES.get(self.fallback_state, f"reserved({self.fallback_state})")

    # 항로 취소 지시인지 판정한다. Table 13.2가 정한 두 가지 표현을 모두 본다.
    @property
    def is_cancellation(self) -> bool:
        return self.route_type == ROUTE_TYPE_CANCELLATION or self.duration == DURATION_CANCEL

    # 봉투를 제외한 본문 bit 수
    @property
    def body_bits(self) -> int:
        return FIXED_BITS + len(self.waypoints) * WAYPOINT_BITS


# Waypoint 개수에 따른 본문 bit 수를 미리 계산한다.
def body_bits_for(waypoint_count: int) -> int:
    return FIXED_BITS + waypoint_count * WAYPOINT_BITS


# 필드가 전선에 실을 수 있는 값인지 본다. 위반이면 사유 문자열, 아니면 None.
#
# 승인된 SC-VDE codec이 보는 canonical raw 허용 집합과 같은 규칙이다
# (VDETER `sc_vde_route_control.py` `_validate_command_fields`).
#
# **왜 응용이 또 보는가** — 그 codec은 무선 경로만 막는다. 콘솔 기본값(direct
# 전송)·loopback 데모·공격 실험의 주입 지점은 모두 codec을 지나지 않는다.
# M.2092-2 §A4-4.15가 요구하는 "검증에 실패하면 수신하지 않은 것처럼 행동하라"는
# 응용이 스스로 지켜야 하는 것이고, 그래야 무선을 타든 안 타든 배가 똑같이
# 행동한다.
#
# 부호화 폭이 담을 수 있는 값이 유효 범위보다 넓은 필드가 문제다. 예를 들어
# Heading Setpoint는 12 bit라 4095까지 담기지만 유효한 값은 3600까지다. 걸러내지
# 않으면 4000이 400.0도가 되고, 조타는 그것을 40도로 정규화해 **지시한 적 없는
# 침로로 배가 돈다.**
def canonical_violation(message: "RouteInstruction") -> str | None:
    if message.sender_classification not in SENDER_NAMES:
        return f"Sender Classification {message.sender_classification} (0·1만 허용)"

    if message.route_type not in SC_VDE_ROUTE_TYPES:
        return f"Route Type {message.route_type} ({sorted(SC_VDE_ROUTE_TYPES)}만 허용)"

    start = (
        message.start_month,
        message.start_day,
        message.start_hour,
        message.start_minute,
    )
    if start != START_NOT_AVAILABLE and not (
        1 <= message.start_month <= 12
        and 1 <= message.start_day <= 31
        and message.start_hour <= 23
        and message.start_minute <= 59
    ):
        return f"Start Date/Time {start} (전체 N/A {START_NOT_AVAILABLE} 또는 정상 범위)"

    if message.heading_setpoint > HEADING_SETPOINT_NOT_AVAILABLE:
        return (
            f"Heading Setpoint {message.heading_setpoint} "
            f"(0..{HEADING_SETPOINT_MODULO - 1} 또는 "
            f"{HEADING_SETPOINT_NOT_AVAILABLE}(N/A))"
        )

    if message.fallback_state not in FALLBACK_NAMES:
        return f"Fallback State {message.fallback_state} (0..3만 허용)"

    for index, waypoint in enumerate(message.waypoints, start=1):
        if not coordinates.longitude_raw_ok(waypoint.longitude):
            return f"WP{index} Longitude raw {waypoint.longitude} (범위 밖)"
        if not coordinates.latitude_raw_ok(waypoint.latitude):
            return f"WP{index} Latitude raw {waypoint.latitude} (범위 밖)"
        if waypoint.leg_geometry not in LEG_GEOMETRY_NAMES:
            return f"WP{index} Leg Geometry {waypoint.leg_geometry} (0·1·2만 허용)"

    return None


# 본문 bit를 writer에 이어 붙인다.
def write_body(writer: BitWriter, message: RouteInstruction) -> None:
    if len(message.waypoints) > MAX_WAYPOINTS:
        raise ValueError(
            f"Waypoint는 최대 {MAX_WAYPOINTS}개입니다 ({len(message.waypoints)}개)"
        )

    writer.write_uint(message.linkage_id, LINKAGE_ID_BITS, "Message Linkage ID")
    writer.write_uint(
        message.sender_classification, SENDER_CLASSIFICATION_BITS, "Sender Classification"
    )
    writer.write_uint(message.route_type, ROUTE_TYPE_BITS, "Route Type")
    writer.write_uint(message.start_month, START_MONTH_BITS, "Start Month")
    writer.write_uint(message.start_day, START_DAY_BITS, "Start Day")
    writer.write_uint(message.start_hour, START_HOUR_BITS, "Start Hour")
    writer.write_uint(message.start_minute, START_MINUTE_BITS, "Start Minute")
    writer.write_uint(message.duration, DURATION_BITS, "Duration")
    writer.write_uint(message.heading_setpoint, HEADING_SETPOINT_BITS, "Heading Setpoint")
    writer.write_uint(message.fallback_state, FALLBACK_BITS, "Fallback State")
    writer.write_uint(len(message.waypoints), WAYPOINT_COUNT_BITS, "Number of Waypoints")

    for index, waypoint in enumerate(message.waypoints, start=1):
        writer.write_int(waypoint.longitude, coordinates.LONGITUDE_BITS, f"WP{index} Longitude")
        writer.write_int(waypoint.latitude, coordinates.LATITUDE_BITS, f"WP{index} Latitude")
        writer.write_uint(waypoint.turn_radius, TURN_RADIUS_BITS, f"WP{index} Turn Radius")
        writer.write_uint(waypoint.planned_sog, PLANNED_SOG_BITS, f"WP{index} Planned SOG")
        writer.write_uint(waypoint.leg_geometry, LEG_GEOMETRY_BITS, f"WP{index} Leg Geometry")


# reader에서 본문을 읽어 RouteInstruction을 만든다.
def read_body(reader: BitReader) -> RouteInstruction:
    linkage_id = reader.read_uint(LINKAGE_ID_BITS, "Message Linkage ID")
    sender_classification = reader.read_uint(
        SENDER_CLASSIFICATION_BITS, "Sender Classification"
    )
    route_type = reader.read_uint(ROUTE_TYPE_BITS, "Route Type")
    start_month = reader.read_uint(START_MONTH_BITS, "Start Month")
    start_day = reader.read_uint(START_DAY_BITS, "Start Day")
    start_hour = reader.read_uint(START_HOUR_BITS, "Start Hour")
    start_minute = reader.read_uint(START_MINUTE_BITS, "Start Minute")
    duration = reader.read_uint(DURATION_BITS, "Duration")
    heading_setpoint = reader.read_uint(HEADING_SETPOINT_BITS, "Heading Setpoint")
    fallback_state = reader.read_uint(FALLBACK_BITS, "Fallback State")
    waypoint_count = reader.read_uint(WAYPOINT_COUNT_BITS, "Number of Waypoints")

    if waypoint_count > MAX_WAYPOINTS:
        raise ValueError(
            f"Waypoint 개수 {waypoint_count}는 표준 상한 {MAX_WAYPOINTS}를 넘습니다"
        )

    waypoints = [
        Waypoint(
            longitude=reader.read_int(coordinates.LONGITUDE_BITS, f"WP{index} Longitude"),
            latitude=reader.read_int(coordinates.LATITUDE_BITS, f"WP{index} Latitude"),
            turn_radius=reader.read_uint(TURN_RADIUS_BITS, f"WP{index} Turn Radius"),
            planned_sog=reader.read_uint(PLANNED_SOG_BITS, f"WP{index} Planned SOG"),
            leg_geometry=reader.read_uint(LEG_GEOMETRY_BITS, f"WP{index} Leg Geometry"),
        )
        for index in range(1, waypoint_count + 1)
    ]

    return RouteInstruction(
        waypoints=waypoints,
        linkage_id=linkage_id,
        sender_classification=sender_classification,
        route_type=route_type,
        start_month=start_month,
        start_day=start_day,
        start_hour=start_hour,
        start_minute=start_minute,
        duration=duration,
        heading_setpoint=heading_setpoint,
        fallback_state=fallback_state,
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: RouteInstruction) -> str:
    lines = [
        f"Linkage ID: {message.linkage_id}",
        f"Sender: {message.sender_name}",
        f"Route Type: {message.route_type_name}",
        f"Duration: "
        + (
            "not available"
            if message.duration == DURATION_NOT_AVAILABLE
            else f"{message.duration} min"
        ),
        f"Heading Setpoint: "
        + (
            "not available"
            if message.heading_setpoint == HEADING_SETPOINT_NOT_AVAILABLE
            else f"{message.heading_setpoint_degrees} deg"
        ),
        f"Fallback: {message.fallback_name}",
        f"Waypoints: {len(message.waypoints)}",
    ]
    for index, waypoint in enumerate(message.waypoints, start=1):
        extra = []
        if waypoint.turn_radius_nm is not None:
            extra.append(f"turn {waypoint.turn_radius_nm} NM")
        if waypoint.planned_sog_knots is not None:
            extra.append(f"SOG {waypoint.planned_sog_knots} kn")
        if waypoint.leg_geometry != LEG_GEOMETRY_NONE:
            extra.append(waypoint.leg_geometry_name)
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(
            f"  WP{index}: {waypoint.latitude_degrees}, {waypoint.longitude_degrees}{suffix}"
        )
    return "\n".join(lines)
