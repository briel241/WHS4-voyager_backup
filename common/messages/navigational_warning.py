"""
Navigational Warning - 항행 경보 (S-124 압축)

근거
    IHO S-124 Navigational Warnings(Ed 2.0.0)의 GML 구조에서 무선 방송에
    필요한 핵심 필드만 비트 단위로 압축한다. 텍스트 원문은 보존하고 나머지는
    열거값·좌표로 옮긴다. 설계 근거는 docs/compact_message_schemas.md 1을 보라.

    전송 계층 필드(Source ID 등)는 담지 않는다. VDE 링크 계층 Type 93 헤더의
    Source ID가 발신 육상국을 식별한다.

필드 구성 (고정부 154 bit + 텍스트 8 x N)
    [Warning Number 14 | Year 7 | Category 2 | Hazard Type 6 |
     Geometry Type 2 | Longitude 28 | Latitude 27 | Extent 12 |
     Valid-from 32 | Duration 16 | Text Length 8 | Text 8xN]
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import coordinates
from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 5
NAME = "Navigational Warning"

WARNING_NUMBER_BITS = 14
YEAR_BITS = 7
CATEGORY_BITS = 2
HAZARD_TYPE_BITS = 6
GEOMETRY_TYPE_BITS = 2
EXTENT_BITS = 12
VALID_FROM_BITS = 32
DURATION_BITS = 16
TEXT_LENGTH_BITS = 8

FIXED_BITS = (
    WARNING_NUMBER_BITS
    + YEAR_BITS
    + CATEGORY_BITS
    + HAZARD_TYPE_BITS
    + GEOMETRY_TYPE_BITS
    + coordinates.LONGITUDE_BITS
    + coordinates.LATITUDE_BITS
    + EXTENT_BITS
    + VALID_FROM_BITS
    + DURATION_BITS
    + TEXT_LENGTH_BITS
)

# 시각 기준: UTC 초, 2000-01-01 00:00:00 (M.2092-2 TBB Table 43 필드 1 규약)
YEAR_BASE = 2000
MAX_TEXT_BYTES = (1 << TEXT_LENGTH_BITS) - 1  # 255

# Category (IMO WWNWS 3계층)
CATEGORY_NAVAREA = 0
CATEGORY_COASTAL = 1
CATEGORY_LOCAL = 2
CATEGORY_NAMES = {
    CATEGORY_NAVAREA: "NAVAREA",
    CATEGORY_COASTAL: "coastal",
    CATEGORY_LOCAL: "local",
}

# Geometry Type
GEOMETRY_POINT = 0
GEOMETRY_CIRCLE = 1
GEOMETRY_RECTANGLE = 2
GEOMETRY_POLYGON_REF = 3
GEOMETRY_NAMES = {
    GEOMETRY_POINT: "point",
    GEOMETRY_CIRCLE: "circle",
    GEOMETRY_RECTANGLE: "rectangle",
    GEOMETRY_POLYGON_REF: "polygon(ref)",
}

# Hazard Type - 6 bit(64종). IMO/IHO/WMO MSI Manual의 NAVAREA 경보 주제와
# S-124 generalCategory를 그룹별 코드값으로 매핑한 것이다(본 프로젝트 정의 매핑).
# 빈 코드는 각 그룹의 확장용 예약이며 default로 "reserved(n)"이 된다.
HAZARD_UNSPECIFIED = 0

# 항로표지 (1~9)
HAZARD_AID_MALFUNCTION = 1
HAZARD_AID_OFF_STATION = 2
HAZARD_AID_CHANGED = 3
HAZARD_LIGHT_UNRELIABLE = 4

# 위험물 (10~19)
HAZARD_DANGEROUS_WRECK = 10
HAZARD_NEW_OBSTRUCTION = 11
HAZARD_UNCHARTED_DANGER = 12
HAZARD_DRIFTING_HAZARD = 13
HAZARD_DRIFTING_MINE = 14
HAZARD_DERELICT = 15
HAZARD_ICE = 16
HAZARD_LARGE_TOW = 17

# 작업 (20~29)
HAZARD_CABLE_PIPELINE = 20
HAZARD_SURVEY = 21
HAZARD_SAR = 22
HAZARD_ANTIPOLLUTION = 23
HAZARD_OFFSHORE = 24

# 군사·보안 (30~39)
HAZARD_MILITARY_EXERCISE = 30
HAZARD_FIRING = 31
HAZARD_MILITARY_OPS = 32
HAZARD_PIRACY = 33
HAZARD_SECURITY = 34

# 항행 서비스·경로 (40~49)
HAZARD_RADIONAV_FAULT = 40
HAZARD_ROUTEING = 41
HAZARD_TSS = 42
HAZARD_VTS = 43

# 자연 현상 (50~59)
HAZARD_TSUNAMI = 50
HAZARD_ABNORMAL_WATER = 51
HAZARD_SEVERE_WEATHER = 52
HAZARD_VOLCANIC = 53
HAZARD_NATURAL_OTHER = 54

HAZARD_NAMES = {
    HAZARD_UNSPECIFIED: "unspecified",
    HAZARD_AID_MALFUNCTION: "aid to navigation malfunction",
    HAZARD_AID_OFF_STATION: "aid off station or missing",
    HAZARD_AID_CHANGED: "aid established or changed",
    HAZARD_LIGHT_UNRELIABLE: "light unreliable",
    HAZARD_DANGEROUS_WRECK: "dangerous wreck",
    HAZARD_NEW_OBSTRUCTION: "new rock, shoal or obstruction",
    HAZARD_UNCHARTED_DANGER: "uncharted danger",
    HAZARD_DRIFTING_HAZARD: "drifting hazard",
    HAZARD_DRIFTING_MINE: "drifting mine",
    HAZARD_DERELICT: "adrift container or derelict",
    HAZARD_ICE: "ice hazard",
    HAZARD_LARGE_TOW: "large or unwieldy tow",
    HAZARD_CABLE_PIPELINE: "cable or pipeline operations",
    HAZARD_SURVEY: "survey operations",
    HAZARD_SAR: "search and rescue operations",
    HAZARD_ANTIPOLLUTION: "anti-pollution operations",
    HAZARD_OFFSHORE: "offshore installation activity",
    HAZARD_MILITARY_EXERCISE: "naval exercise",
    HAZARD_FIRING: "gunnery or missile firing",
    HAZARD_MILITARY_OPS: "military operations area",
    HAZARD_PIRACY: "piracy or armed robbery",
    HAZARD_SECURITY: "security special warning",
    HAZARD_RADIONAV_FAULT: "radionavigation service fault",
    HAZARD_ROUTEING: "ship routeing change",
    HAZARD_TSS: "traffic separation scheme change",
    HAZARD_VTS: "VTS related",
    HAZARD_TSUNAMI: "tsunami",
    HAZARD_ABNORMAL_WATER: "abnormal water level or surge",
    HAZARD_SEVERE_WEATHER: "severe weather or tropical cyclone",
    HAZARD_VOLCANIC: "volcanic activity",
    HAZARD_NATURAL_OTHER: "other natural phenomenon",
}

# Extent: 원 반경/사각 반크기 0.01 NM. 0 = 지정 없음(점 경보 등).
EXTENT_NONE = 0
EXTENT_SCALE = 100  # 1 NM = 100 단위

# Duration: 분. 0 = 무기한.
DURATION_INDEFINITE = 0


@dataclass
class NavigationalWarning:
    warning_number: int = 0
    year: int = YEAR_BASE  # 실제 연도(2000~2127)
    category: int = CATEGORY_NAVAREA
    hazard_type: int = HAZARD_UNSPECIFIED
    geometry_type: int = GEOMETRY_POINT
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE
    extent: int = EXTENT_NONE  # 0.01 NM 단위
    valid_from: int = 0  # UTC 초 (2000 기준)
    duration: int = DURATION_INDEFINITE  # 분
    text: str = ""

    def __post_init__(self) -> None:
        if len(self.text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(
                f"텍스트는 UTF-8 {MAX_TEXT_BYTES} byte 이하여야 합니다 "
                f"({len(self.text.encode('utf-8'))} byte)"
            )

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES.get(self.category, f"reserved({self.category})")

    @property
    def geometry_name(self) -> str:
        return GEOMETRY_NAMES.get(self.geometry_type, f"reserved({self.geometry_type})")

    @property
    def hazard_name(self) -> str:
        return HAZARD_NAMES.get(self.hazard_type, f"code({self.hazard_type})")

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)

    @property
    def extent_nm(self) -> float | None:
        return None if self.extent == EXTENT_NONE else self.extent / EXTENT_SCALE


# 도·해리·연도 단위 실측값으로 NavigationalWarning을 만든다.
def from_values(
    warning_number: int,
    year: int,
    hazard_type: int,
    longitude_degrees: float | None = None,
    latitude_degrees: float | None = None,
    category: int = CATEGORY_NAVAREA,
    geometry_type: int = GEOMETRY_POINT,
    extent_nm: float | None = None,
    valid_from: int = 0,
    duration: int = DURATION_INDEFINITE,
    text: str = "",
) -> NavigationalWarning:
    return NavigationalWarning(
        warning_number=warning_number,
        year=year,
        category=category,
        hazard_type=hazard_type,
        geometry_type=geometry_type,
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
        extent=EXTENT_NONE if extent_nm is None else round(extent_nm * EXTENT_SCALE),
        valid_from=valid_from,
        duration=duration,
        text=text,
    )


# 본문 bit를 writer에 이어 붙인다.
def write_body(writer: BitWriter, message: NavigationalWarning) -> None:
    text_bytes = message.text.encode("utf-8")
    if len(text_bytes) > MAX_TEXT_BYTES:
        raise ValueError(f"텍스트가 {MAX_TEXT_BYTES} byte를 넘습니다 ({len(text_bytes)})")

    writer.write_uint(message.warning_number, WARNING_NUMBER_BITS, "Warning Number")
    writer.write_uint(message.year - YEAR_BASE, YEAR_BITS, "Year")
    writer.write_uint(message.category, CATEGORY_BITS, "Category")
    writer.write_uint(message.hazard_type, HAZARD_TYPE_BITS, "Hazard Type")
    writer.write_uint(message.geometry_type, GEOMETRY_TYPE_BITS, "Geometry Type")
    writer.write_int(message.longitude, coordinates.LONGITUDE_BITS, "Longitude")
    writer.write_int(message.latitude, coordinates.LATITUDE_BITS, "Latitude")
    writer.write_uint(message.extent, EXTENT_BITS, "Extent")
    writer.write_uint(message.valid_from, VALID_FROM_BITS, "Valid-from")
    writer.write_uint(message.duration, DURATION_BITS, "Duration")
    writer.write_uint(len(text_bytes), TEXT_LENGTH_BITS, "Text Length")
    for byte in text_bytes:
        writer.write_uint(byte, 8, "Text")


# reader에서 본문을 읽어 NavigationalWarning을 만든다.
def read_body(reader: BitReader) -> NavigationalWarning:
    warning_number = reader.read_uint(WARNING_NUMBER_BITS, "Warning Number")
    year = reader.read_uint(YEAR_BITS, "Year") + YEAR_BASE
    category = reader.read_uint(CATEGORY_BITS, "Category")
    hazard_type = reader.read_uint(HAZARD_TYPE_BITS, "Hazard Type")
    geometry_type = reader.read_uint(GEOMETRY_TYPE_BITS, "Geometry Type")
    longitude = reader.read_int(coordinates.LONGITUDE_BITS, "Longitude")
    latitude = reader.read_int(coordinates.LATITUDE_BITS, "Latitude")
    extent = reader.read_uint(EXTENT_BITS, "Extent")
    valid_from = reader.read_uint(VALID_FROM_BITS, "Valid-from")
    duration = reader.read_uint(DURATION_BITS, "Duration")
    text_length = reader.read_uint(TEXT_LENGTH_BITS, "Text Length")
    text_bytes = bytes(reader.read_uint(8, "Text") for _ in range(text_length))
    try:
        text = text_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"텍스트 UTF-8 디코딩 실패: {error}")

    return NavigationalWarning(
        warning_number=warning_number,
        year=year,
        category=category,
        hazard_type=hazard_type,
        geometry_type=geometry_type,
        longitude=longitude,
        latitude=latitude,
        extent=extent,
        valid_from=valid_from,
        duration=duration,
        text=text,
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: NavigationalWarning) -> str:
    def show(value, unit: str = "") -> str:
        return "not available" if value is None else f"{value}{unit}"

    return "\n".join(
        [
            f"Warning: {message.year}-{message.warning_number}",
            f"Category: {message.category_name}",
            f"Hazard: {message.hazard_name}",
            f"Geometry: {message.geometry_name}",
            f"Position: {show(message.latitude_degrees)}, {show(message.longitude_degrees)}",
            f"Extent: {show(message.extent_nm, ' NM')}",
            f"Valid-from: {message.valid_from} (UTC s since 2000)",
            f"Duration: "
            + ("indefinite" if message.duration == DURATION_INDEFINITE else f"{message.duration} min"),
            f"Text: {message.text!r}",
        ]
    )
