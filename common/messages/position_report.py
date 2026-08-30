"""
Position Report - MASS가 주기적으로 내보내는 위치·상태 보고

근거
    AIS Position Report Class A(ITU-R M.1371 Message 1/2/3)의
    Common Navigation Block을 차용한다.

    전송 계층 필드(Message Type, Repeat Indicator, MMSI, Radio Status)는
    가져오지 않는다. 송신원 식별은 VDE 링크 계층 Source ID가 담당하므로
    중복이다. 자세한 근거는 docs/mass_roc_message_set.md 2.3을 보라.

    Autonomy Mode만 표준에 대응 필드가 없어 본 프로젝트가 새로 정의했다.
    AIS Navigation Status에는 자율운항을 나타내는 값이 없고, 두 개념은
    직교하므로(자율운항 중에도 정박 상태일 수 있다) 별도 필드로 둔다.

필드 구성 (본문 111 bit, 봉투 제외)
    [Navigation Status 4 | Rate of Turn 8 | SOG 10 | Position Accuracy 1 |
     Longitude 28 | Latitude 27 | COG 12 | True Heading 9 | Timestamp 6 |
     Maneuver Indicator 2 | RAIM 1 | Autonomy Mode 3]
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import coordinates
from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 1
NAME = "Position Report"

NAVIGATION_STATUS_BITS = 4
RATE_OF_TURN_BITS = 8
SOG_BITS = 10
POSITION_ACCURACY_BITS = 1
COG_BITS = 12
TRUE_HEADING_BITS = 9
TIMESTAMP_BITS = 6
MANEUVER_INDICATOR_BITS = 2
RAIM_BITS = 1
AUTONOMY_MODE_BITS = 3

BODY_BITS = (
    NAVIGATION_STATUS_BITS
    + RATE_OF_TURN_BITS
    + SOG_BITS
    + POSITION_ACCURACY_BITS
    + coordinates.LONGITUDE_BITS
    + coordinates.LATITUDE_BITS
    + COG_BITS
    + TRUE_HEADING_BITS
    + TIMESTAMP_BITS
    + MANEUVER_INDICATOR_BITS
    + RAIM_BITS
    + AUTONOMY_MODE_BITS
)

# AIS가 정한 not available 값
NAVIGATION_STATUS_UNDEFINED = 15
RATE_OF_TURN_NOT_AVAILABLE = -128
SOG_NOT_AVAILABLE = 1023
COG_NOT_AVAILABLE = 3600
TRUE_HEADING_NOT_AVAILABLE = 511
TIMESTAMP_NOT_AVAILABLE = 60

# SOG는 0.1 knot, COG는 0.1도 단위 정수로 기록한다.
SOG_SCALE = 10
COG_SCALE = 10

# 유효 raw 상한. not available 값 바로 아래다.
#
# 이 상한이 필요한 이유: 반올림 결과가 not available 센티널과 같아지면 유효한
# 측정값이 "측정 불가"로 둔갑한다. 예를 들어 침로 359.95도는 round(3599.5)=3600이
# 되어 COG_NOT_AVAILABLE과 구별되지 않는다.
#
#   - COG는 순환값이므로 3600을 0으로 되돌린다(359.95도 ≈ 360도 = 0도).
#   - SOG는 순환하지 않으므로 상한에 붙인다. AIS도 1022를
#     "102.2 knots or higher"로 정의하므로 의미가 맞는다.
COG_MODULO = 3600          # 0..3599
SOG_MAX = 1022             # 102.2 knots or higher
TRUE_HEADING_MODULO = 360  # 0..359

# Autonomy Mode - 본 프로젝트 정의. 표준 근거 없음.
# 값은 docs/mass_roc_background.md 2.5의 상태 모델과 맞춘다.
AUTONOMY_UNKNOWN = 0
AUTONOMY_RC = 1  # 원격제어 - 제어권이 ROC에 있다
AUTONOMY_MON = 2  # 감독 자율 - 세션은 있고 제어권은 본선에 있다
AUTONOMY_AUT = 3  # 독립 자율 - 세션 없음

AUTONOMY_MODE_NAMES = {
    AUTONOMY_UNKNOWN: "unknown",
    AUTONOMY_RC: "RC",
    AUTONOMY_MON: "MON",
    AUTONOMY_AUT: "AUT",
}


# 각 필드는 AIS 원 부호화 그대로의 정수다.
# 도 단위 좌표가 필요하면 longitude_degrees/latitude_degrees를 쓴다.
@dataclass
class PositionReport:
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE
    navigation_status: int = NAVIGATION_STATUS_UNDEFINED
    rate_of_turn: int = RATE_OF_TURN_NOT_AVAILABLE
    sog: int = SOG_NOT_AVAILABLE
    cog: int = COG_NOT_AVAILABLE
    true_heading: int = TRUE_HEADING_NOT_AVAILABLE
    position_accuracy: int = 0
    timestamp: int = TIMESTAMP_NOT_AVAILABLE
    maneuver_indicator: int = 0
    raim: int = 0
    autonomy_mode: int = AUTONOMY_UNKNOWN

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)

    @property
    def sog_knots(self) -> float | None:
        return None if self.sog == SOG_NOT_AVAILABLE else self.sog / SOG_SCALE

    @property
    def cog_degrees(self) -> float | None:
        return None if self.cog == COG_NOT_AVAILABLE else self.cog / COG_SCALE

    @property
    def heading_degrees(self) -> int | None:
        return None if self.true_heading == TRUE_HEADING_NOT_AVAILABLE else self.true_heading

    @property
    def autonomy_mode_name(self) -> str:
        return AUTONOMY_MODE_NAMES.get(self.autonomy_mode, f"reserved({self.autonomy_mode})")


# 도·knot 단위 실측값으로 PositionReport를 만든다. None은 not available로 기록한다.
def from_measurements(
    longitude_degrees: float | None = None,
    latitude_degrees: float | None = None,
    sog_knots: float | None = None,
    cog_degrees: float | None = None,
    heading_degrees: int | None = None,
    navigation_status: int = NAVIGATION_STATUS_UNDEFINED,
    rate_of_turn: int = RATE_OF_TURN_NOT_AVAILABLE,
    position_accuracy: int = 0,
    timestamp: int = TIMESTAMP_NOT_AVAILABLE,
    maneuver_indicator: int = 0,
    raim: int = 0,
    autonomy_mode: int = AUTONOMY_UNKNOWN,
) -> PositionReport:
    return PositionReport(
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
        sog=(
            SOG_NOT_AVAILABLE if sog_knots is None
            else min(max(round(sog_knots * SOG_SCALE), 0), SOG_MAX)
        ),
        cog=(
            COG_NOT_AVAILABLE if cog_degrees is None
            else round(cog_degrees * COG_SCALE) % COG_MODULO
        ),
        true_heading=(
            TRUE_HEADING_NOT_AVAILABLE if heading_degrees is None
            else int(heading_degrees) % TRUE_HEADING_MODULO
        ),
        navigation_status=navigation_status,
        rate_of_turn=rate_of_turn,
        position_accuracy=position_accuracy,
        timestamp=timestamp,
        maneuver_indicator=maneuver_indicator,
        raim=raim,
        autonomy_mode=autonomy_mode,
    )


# 본문 bit를 writer에 이어 붙인다. 봉투(VPFI/Message ID)는 codec이 담당한다.
def write_body(writer: BitWriter, message: PositionReport) -> None:
    writer.write_uint(message.navigation_status, NAVIGATION_STATUS_BITS, "Navigation Status")
    writer.write_int(message.rate_of_turn, RATE_OF_TURN_BITS, "Rate of Turn")
    writer.write_uint(message.sog, SOG_BITS, "SOG")
    writer.write_uint(message.position_accuracy, POSITION_ACCURACY_BITS, "Position Accuracy")
    writer.write_int(message.longitude, coordinates.LONGITUDE_BITS, "Longitude")
    writer.write_int(message.latitude, coordinates.LATITUDE_BITS, "Latitude")
    writer.write_uint(message.cog, COG_BITS, "COG")
    writer.write_uint(message.true_heading, TRUE_HEADING_BITS, "True Heading")
    writer.write_uint(message.timestamp, TIMESTAMP_BITS, "Timestamp")
    writer.write_uint(message.maneuver_indicator, MANEUVER_INDICATOR_BITS, "Maneuver Indicator")
    writer.write_uint(message.raim, RAIM_BITS, "RAIM")
    writer.write_uint(message.autonomy_mode, AUTONOMY_MODE_BITS, "Autonomy Mode")


# reader에서 본문을 읽어 PositionReport를 만든다.
def read_body(reader: BitReader) -> PositionReport:
    return PositionReport(
        navigation_status=reader.read_uint(NAVIGATION_STATUS_BITS, "Navigation Status"),
        rate_of_turn=reader.read_int(RATE_OF_TURN_BITS, "Rate of Turn"),
        sog=reader.read_uint(SOG_BITS, "SOG"),
        position_accuracy=reader.read_uint(POSITION_ACCURACY_BITS, "Position Accuracy"),
        longitude=reader.read_int(coordinates.LONGITUDE_BITS, "Longitude"),
        latitude=reader.read_int(coordinates.LATITUDE_BITS, "Latitude"),
        cog=reader.read_uint(COG_BITS, "COG"),
        true_heading=reader.read_uint(TRUE_HEADING_BITS, "True Heading"),
        timestamp=reader.read_uint(TIMESTAMP_BITS, "Timestamp"),
        maneuver_indicator=reader.read_uint(MANEUVER_INDICATOR_BITS, "Maneuver Indicator"),
        raim=reader.read_uint(RAIM_BITS, "RAIM"),
        autonomy_mode=reader.read_uint(AUTONOMY_MODE_BITS, "Autonomy Mode"),
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: PositionReport) -> str:
    def show(value, unit: str = "") -> str:
        return "not available" if value is None else f"{value}{unit}"

    return "\n".join(
        [
            f"Navigation Status: {message.navigation_status}",
            f"Autonomy Mode: {message.autonomy_mode_name}",
            f"Longitude: {show(message.longitude_degrees, ' deg')}",
            f"Latitude: {show(message.latitude_degrees, ' deg')}",
            f"SOG: {show(message.sog_knots, ' kn')}",
            f"COG: {show(message.cog_degrees, ' deg')}",
            f"Heading: {show(message.heading_degrees, ' deg')}",
            f"Rate of Turn: {show(None if message.rate_of_turn == RATE_OF_TURN_NOT_AVAILABLE else message.rate_of_turn)}",
        ]
    )
