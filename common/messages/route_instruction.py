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

필드 구성 (고정부 61 bit + waypoint 55 bit x n)
    [Message Linkage ID 10 | Sender Classification 3 | Route Type 5 |
     Start Month 4 | Start Day 5 | Start Hour 5 | Start Minute 6 |
     Duration 18 | Number of Waypoints 5 | Waypoints n x 55]
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
WAYPOINT_COUNT_BITS = 5

FIXED_BITS = (
    LINKAGE_ID_BITS
    + SENDER_CLASSIFICATION_BITS
    + ROUTE_TYPE_BITS
    + START_MONTH_BITS
    + START_DAY_BITS
    + START_HOUR_BITS
    + START_MINUTE_BITS
    + DURATION_BITS
    + WAYPOINT_COUNT_BITS
)
WAYPOINT_BITS = coordinates.LONGITUDE_BITS + coordinates.LATITUDE_BITS

# 표준이 정한 waypoint 개수 상한
MAX_WAYPOINTS = 16

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

# not available 값 (Table 13.2)
LINKAGE_ID_NOT_AVAILABLE = 0
MONTH_NOT_AVAILABLE = 0
DAY_NOT_AVAILABLE = 0
HOUR_NOT_AVAILABLE = 24
MINUTE_NOT_AVAILABLE = 60
DURATION_NOT_AVAILABLE = 262_143
DURATION_CANCEL = 0


@dataclass
class Waypoint:
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)


# 도 단위 좌표로 Waypoint를 만든다.
def waypoint_from_degrees(longitude_degrees: float, latitude_degrees: float) -> Waypoint:
    return Waypoint(
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
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
    writer.write_uint(len(message.waypoints), WAYPOINT_COUNT_BITS, "Number of Waypoints")

    for index, waypoint in enumerate(message.waypoints, start=1):
        writer.write_int(waypoint.longitude, coordinates.LONGITUDE_BITS, f"WP{index} Longitude")
        writer.write_int(waypoint.latitude, coordinates.LATITUDE_BITS, f"WP{index} Latitude")


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
    waypoint_count = reader.read_uint(WAYPOINT_COUNT_BITS, "Number of Waypoints")

    if waypoint_count > MAX_WAYPOINTS:
        raise ValueError(
            f"Waypoint 개수 {waypoint_count}는 표준 상한 {MAX_WAYPOINTS}를 넘습니다"
        )

    waypoints = [
        Waypoint(
            longitude=reader.read_int(coordinates.LONGITUDE_BITS, f"WP{index} Longitude"),
            latitude=reader.read_int(coordinates.LATITUDE_BITS, f"WP{index} Latitude"),
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
        f"Waypoints: {len(message.waypoints)}",
    ]
    for index, waypoint in enumerate(message.waypoints, start=1):
        lines.append(
            f"  WP{index}: {waypoint.latitude_degrees}, {waypoint.longitude_degrees}"
        )
    return "\n".join(lines)
