"""
Environmental Info - 환경 정보 nowcast (S-104 / S-411 / S-412 압축)

근거
    IHO S-104(수위)·S-111(표층류)·S-412(기상)·S-411(해빙)의 격자 제품에서
    한 지점/셀의 nowcast 값을 방송용으로 압축한다. 실제 전체 격자(HDF5, 대용량)는
    data session 경로로 따로 전송하며, 본 스키마는 방송용 보완이다.
    설계 근거는 docs/compact_message_schemas.md 2를 보라.

    SOG/COG와 같은 부호화(0.1 단위, sentinel)를 재사용한다.

필드 구성 (185 bit)
    [Longitude 28 | Latitude 27 | Valid-time 32 | Presence Flags 4 |
     Water Level 16 | WL Trend 2 | Current Speed 10 | Current Dir 12 |
     Wind Speed 10 | Wind Dir 9 | Wave Height 8 | Wave Dir 9 |
     Wave Period 6 | Ice Conc 4 | Ice Thickness 8]
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import coordinates
from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 6
NAME = "Environmental Info"

VALID_TIME_BITS = 32
PRESENCE_FLAGS_BITS = 4
WATER_LEVEL_BITS = 16
WL_TREND_BITS = 2
CURRENT_SPEED_BITS = 10
CURRENT_DIR_BITS = 12
WIND_SPEED_BITS = 10
WIND_DIR_BITS = 9
WAVE_HEIGHT_BITS = 8
WAVE_DIR_BITS = 9
WAVE_PERIOD_BITS = 6
ICE_CONC_BITS = 4
ICE_THICKNESS_BITS = 8

BODY_BITS = (
    coordinates.LONGITUDE_BITS
    + coordinates.LATITUDE_BITS
    + VALID_TIME_BITS
    + PRESENCE_FLAGS_BITS
    + WATER_LEVEL_BITS
    + WL_TREND_BITS
    + CURRENT_SPEED_BITS
    + CURRENT_DIR_BITS
    + WIND_SPEED_BITS
    + WIND_DIR_BITS
    + WAVE_HEIGHT_BITS
    + WAVE_DIR_BITS
    + WAVE_PERIOD_BITS
    + ICE_CONC_BITS
    + ICE_THICKNESS_BITS
)

# Presence Flags (어떤 제품 값이 실렸는가)
FLAG_WATER_LEVEL = 0x1
FLAG_CURRENT = 0x2
FLAG_WEATHER = 0x4  # 바람·파랑
FLAG_ICE = 0x8

# 값 없음 sentinel
WATER_LEVEL_NOT_AVAILABLE = -(1 << (WATER_LEVEL_BITS - 1))  # -32768
CURRENT_SPEED_NOT_AVAILABLE = 1023
CURRENT_DIR_NOT_AVAILABLE = 3600
WIND_SPEED_NOT_AVAILABLE = 1023
WIND_DIR_NOT_AVAILABLE = 511
WAVE_HEIGHT_NOT_AVAILABLE = 255
WAVE_DIR_NOT_AVAILABLE = 511
WAVE_PERIOD_NOT_AVAILABLE = 63
ICE_CONC_NOT_AVAILABLE = 15
ICE_THICKNESS_NOT_AVAILABLE = 255

# 스케일
CURRENT_SPEED_SCALE = 10  # 0.1 knot
DIR_SCALE = 10            # 0.1 도 (10분의 1도)
WIND_SPEED_SCALE = 10     # 0.1 m/s
WAVE_HEIGHT_SCALE = 10    # 0.1 m
WAVE_PERIOD_SCALE = 2     # 0.5 s
ICE_CONC_SCALE = 1        # 1/10 (0~10)

# WL Trend
WL_TREND_RISING = 0
WL_TREND_FALLING = 1
WL_TREND_STEADY = 2
WL_TREND_UNKNOWN = 3
WL_TREND_NAMES = {
    WL_TREND_RISING: "rising",
    WL_TREND_FALLING: "falling",
    WL_TREND_STEADY: "steady",
    WL_TREND_UNKNOWN: "unknown",
}


@dataclass
class EnvironmentalInfo:
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE
    valid_time: int = 0  # UTC 초 (2000 기준)
    presence_flags: int = 0
    water_level: int = WATER_LEVEL_NOT_AVAILABLE  # cm, datum 기준
    wl_trend: int = WL_TREND_UNKNOWN
    current_speed: int = CURRENT_SPEED_NOT_AVAILABLE
    current_dir: int = CURRENT_DIR_NOT_AVAILABLE
    wind_speed: int = WIND_SPEED_NOT_AVAILABLE
    wind_dir: int = WIND_DIR_NOT_AVAILABLE
    wave_height: int = WAVE_HEIGHT_NOT_AVAILABLE
    wave_dir: int = WAVE_DIR_NOT_AVAILABLE
    wave_period: int = WAVE_PERIOD_NOT_AVAILABLE
    ice_conc: int = ICE_CONC_NOT_AVAILABLE
    ice_thickness: int = ICE_THICKNESS_NOT_AVAILABLE

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)

    @property
    def water_level_m(self) -> float | None:
        return None if self.water_level == WATER_LEVEL_NOT_AVAILABLE else self.water_level / 100

    @property
    def current_speed_kn(self) -> float | None:
        return (
            None if self.current_speed == CURRENT_SPEED_NOT_AVAILABLE
            else self.current_speed / CURRENT_SPEED_SCALE
        )

    @property
    def current_dir_deg(self) -> float | None:
        return (
            None if self.current_dir == CURRENT_DIR_NOT_AVAILABLE
            else self.current_dir / DIR_SCALE
        )

    @property
    def wind_speed_ms(self) -> float | None:
        return (
            None if self.wind_speed == WIND_SPEED_NOT_AVAILABLE
            else self.wind_speed / WIND_SPEED_SCALE
        )

    @property
    def wind_dir_deg(self) -> int | None:
        return None if self.wind_dir == WIND_DIR_NOT_AVAILABLE else self.wind_dir

    @property
    def wave_height_m(self) -> float | None:
        return (
            None if self.wave_height == WAVE_HEIGHT_NOT_AVAILABLE
            else self.wave_height / WAVE_HEIGHT_SCALE
        )

    @property
    def wave_period_s(self) -> float | None:
        return (
            None if self.wave_period == WAVE_PERIOD_NOT_AVAILABLE
            else self.wave_period / WAVE_PERIOD_SCALE
        )

    @property
    def wl_trend_name(self) -> str:
        return WL_TREND_NAMES.get(self.wl_trend, f"reserved({self.wl_trend})")


# 실측값으로 EnvironmentalInfo를 만든다. None은 not available로 기록하고
# 실린 항목에 맞춰 presence_flags를 자동 설정한다.
def from_measurements(
    longitude_degrees: float | None = None,
    latitude_degrees: float | None = None,
    valid_time: int = 0,
    water_level_m: float | None = None,
    wl_trend: int = WL_TREND_UNKNOWN,
    current_speed_kn: float | None = None,
    current_dir_deg: float | None = None,
    wind_speed_ms: float | None = None,
    wind_dir_deg: int | None = None,
    wave_height_m: float | None = None,
    wave_dir_deg: int | None = None,
    wave_period_s: float | None = None,
    ice_conc_tenths: int | None = None,
    ice_thickness_cm: int | None = None,
) -> EnvironmentalInfo:
    flags = 0
    if water_level_m is not None:
        flags |= FLAG_WATER_LEVEL
    if current_speed_kn is not None or current_dir_deg is not None:
        flags |= FLAG_CURRENT
    if any(v is not None for v in (wind_speed_ms, wind_dir_deg, wave_height_m, wave_period_s)):
        flags |= FLAG_WEATHER
    if ice_conc_tenths is not None or ice_thickness_cm is not None:
        flags |= FLAG_ICE

    return EnvironmentalInfo(
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
        valid_time=valid_time,
        presence_flags=flags,
        water_level=(
            WATER_LEVEL_NOT_AVAILABLE if water_level_m is None else round(water_level_m * 100)
        ),
        wl_trend=wl_trend,
        current_speed=(
            CURRENT_SPEED_NOT_AVAILABLE if current_speed_kn is None
            else round(current_speed_kn * CURRENT_SPEED_SCALE)
        ),
        current_dir=(
            CURRENT_DIR_NOT_AVAILABLE if current_dir_deg is None
            else round(current_dir_deg * DIR_SCALE)
        ),
        wind_speed=(
            WIND_SPEED_NOT_AVAILABLE if wind_speed_ms is None
            else round(wind_speed_ms * WIND_SPEED_SCALE)
        ),
        wind_dir=WIND_DIR_NOT_AVAILABLE if wind_dir_deg is None else int(wind_dir_deg),
        wave_height=(
            WAVE_HEIGHT_NOT_AVAILABLE if wave_height_m is None
            else round(wave_height_m * WAVE_HEIGHT_SCALE)
        ),
        wave_dir=WAVE_DIR_NOT_AVAILABLE if wave_dir_deg is None else int(wave_dir_deg),
        wave_period=(
            WAVE_PERIOD_NOT_AVAILABLE if wave_period_s is None
            else round(wave_period_s * WAVE_PERIOD_SCALE)
        ),
        ice_conc=ICE_CONC_NOT_AVAILABLE if ice_conc_tenths is None else int(ice_conc_tenths),
        ice_thickness=(
            ICE_THICKNESS_NOT_AVAILABLE if ice_thickness_cm is None else int(ice_thickness_cm)
        ),
    )


# 본문 bit를 writer에 이어 붙인다.
def write_body(writer: BitWriter, message: EnvironmentalInfo) -> None:
    writer.write_int(message.longitude, coordinates.LONGITUDE_BITS, "Longitude")
    writer.write_int(message.latitude, coordinates.LATITUDE_BITS, "Latitude")
    writer.write_uint(message.valid_time, VALID_TIME_BITS, "Valid-time")
    writer.write_uint(message.presence_flags, PRESENCE_FLAGS_BITS, "Presence Flags")
    writer.write_int(message.water_level, WATER_LEVEL_BITS, "Water Level")
    writer.write_uint(message.wl_trend, WL_TREND_BITS, "WL Trend")
    writer.write_uint(message.current_speed, CURRENT_SPEED_BITS, "Current Speed")
    writer.write_uint(message.current_dir, CURRENT_DIR_BITS, "Current Dir")
    writer.write_uint(message.wind_speed, WIND_SPEED_BITS, "Wind Speed")
    writer.write_uint(message.wind_dir, WIND_DIR_BITS, "Wind Dir")
    writer.write_uint(message.wave_height, WAVE_HEIGHT_BITS, "Wave Height")
    writer.write_uint(message.wave_dir, WAVE_DIR_BITS, "Wave Dir")
    writer.write_uint(message.wave_period, WAVE_PERIOD_BITS, "Wave Period")
    writer.write_uint(message.ice_conc, ICE_CONC_BITS, "Ice Conc")
    writer.write_uint(message.ice_thickness, ICE_THICKNESS_BITS, "Ice Thickness")


# reader에서 본문을 읽어 EnvironmentalInfo를 만든다.
def read_body(reader: BitReader) -> EnvironmentalInfo:
    return EnvironmentalInfo(
        longitude=reader.read_int(coordinates.LONGITUDE_BITS, "Longitude"),
        latitude=reader.read_int(coordinates.LATITUDE_BITS, "Latitude"),
        valid_time=reader.read_uint(VALID_TIME_BITS, "Valid-time"),
        presence_flags=reader.read_uint(PRESENCE_FLAGS_BITS, "Presence Flags"),
        water_level=reader.read_int(WATER_LEVEL_BITS, "Water Level"),
        wl_trend=reader.read_uint(WL_TREND_BITS, "WL Trend"),
        current_speed=reader.read_uint(CURRENT_SPEED_BITS, "Current Speed"),
        current_dir=reader.read_uint(CURRENT_DIR_BITS, "Current Dir"),
        wind_speed=reader.read_uint(WIND_SPEED_BITS, "Wind Speed"),
        wind_dir=reader.read_uint(WIND_DIR_BITS, "Wind Dir"),
        wave_height=reader.read_uint(WAVE_HEIGHT_BITS, "Wave Height"),
        wave_dir=reader.read_uint(WAVE_DIR_BITS, "Wave Dir"),
        wave_period=reader.read_uint(WAVE_PERIOD_BITS, "Wave Period"),
        ice_conc=reader.read_uint(ICE_CONC_BITS, "Ice Conc"),
        ice_thickness=reader.read_uint(ICE_THICKNESS_BITS, "Ice Thickness"),
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: EnvironmentalInfo) -> str:
    def show(value, unit: str = "") -> str:
        return "not available" if value is None else f"{value}{unit}"

    return "\n".join(
        [
            f"Position: {show(message.latitude_degrees)}, {show(message.longitude_degrees)}",
            f"Valid-time: {message.valid_time} (UTC s since 2000)",
            f"Water Level: {show(message.water_level_m, ' m')} ({message.wl_trend_name})",
            f"Current: {show(message.current_speed_kn, ' kn')} @ {show(message.current_dir_deg, ' deg')}",
            f"Wind: {show(message.wind_speed_ms, ' m/s')} @ {show(message.wind_dir_deg, ' deg')}",
            f"Wave: {show(message.wave_height_m, ' m')} @ {show(message.wave_dir if message.wave_dir != WAVE_DIR_NOT_AVAILABLE else None, ' deg')}, "
            f"{show(message.wave_period_s, ' s')}",
            f"Ice: {show(None if message.ice_conc == ICE_CONC_NOT_AVAILABLE else message.ice_conc, '/10')}, "
            f"{show(None if message.ice_thickness == ICE_THICKNESS_NOT_AVAILABLE else message.ice_thickness, ' cm')}",
        ]
    )
