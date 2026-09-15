"""
Weather Observation - 선박 기상관측 보고 (BUFR 압축)

근거
    WMO FM-94 BUFR 선박 관측 시퀀스(BUFR 3-08-009)의 관측 요소를 고정 서브셋으로
    비트 인코딩한다. BUFR 자체가 이진 압축이지만 테이블 구동이라 디코더가 무겁다 -
    좁은 VDE-TER 슬롯용으로 고정 필드 배치를 쓴다.
    설계 근거는 docs/compact_message_schemas.md 3을 보라.

    방향은 위치보고와 같은 ship->shore다. Type 93(ACK 불요) 또는 Type 92로 보낸다.

필드 구성 (180 bit)
    [Longitude 28 | Latitude 27 | Obs Time 32 | Air Temp 11 | Dew Point 11 |
     Sea Temp 11 | Pressure 14 | Pressure Tendency 4 | Wind Dir 9 |
     Wind Speed 10 | Wave Height 8 | Wave Period 6 | Present Weather 5 |
     Visibility 4]
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import coordinates
from ..bitfield import BitReader, BitWriter

MESSAGE_ID = 7
NAME = "Weather Observation"

OBS_TIME_BITS = 32
AIR_TEMP_BITS = 11
DEW_POINT_BITS = 11
SEA_TEMP_BITS = 11
PRESSURE_BITS = 14
PRESSURE_TENDENCY_BITS = 4
WIND_DIR_BITS = 9
WIND_SPEED_BITS = 10
WAVE_HEIGHT_BITS = 8
WAVE_PERIOD_BITS = 6
PRESENT_WEATHER_BITS = 5
VISIBILITY_BITS = 4

BODY_BITS = (
    coordinates.LONGITUDE_BITS
    + coordinates.LATITUDE_BITS
    + OBS_TIME_BITS
    + AIR_TEMP_BITS
    + DEW_POINT_BITS
    + SEA_TEMP_BITS
    + PRESSURE_BITS
    + PRESSURE_TENDENCY_BITS
    + WIND_DIR_BITS
    + WIND_SPEED_BITS
    + WAVE_HEIGHT_BITS
    + WAVE_PERIOD_BITS
    + PRESENT_WEATHER_BITS
    + VISIBILITY_BITS
)

# 온도: 0.1도 C, offset -60.0 (raw = (C + 60.0) x 10)
TEMP_OFFSET_C = 60.0
TEMP_SCALE = 10
TEMP_NOT_AVAILABLE = (1 << AIR_TEMP_BITS) - 1  # 2047

# 기압: 0.1 hPa, offset 500.0 (raw = (hPa - 500.0) x 10)
PRESSURE_OFFSET_HPA = 500.0
PRESSURE_SCALE = 10
PRESSURE_NOT_AVAILABLE = (1 << PRESSURE_BITS) - 1  # 16383

PRESSURE_TENDENCY_NOT_AVAILABLE = 15  # WMO 특성 코드 0~8, 15=없음
WIND_DIR_NOT_AVAILABLE = 511
WIND_SPEED_NOT_AVAILABLE = 1023
WIND_SPEED_SCALE = 10  # 0.1 m/s
WAVE_HEIGHT_NOT_AVAILABLE = 255
WAVE_HEIGHT_SCALE = 10  # 0.1 m
WAVE_PERIOD_NOT_AVAILABLE = 63  # 초
PRESENT_WEATHER_NOT_AVAILABLE = 31  # WMO 4677 축약 0~30
VISIBILITY_NOT_AVAILABLE = 15

# Present Weather - WMO 코드표 4677(ww)을 5 bit(0~30)로 축약한 매핑(본 프로젝트 정의).
PRESENT_WEATHER_NAMES = {
    0: "no significant weather",
    1: "clouds developing or increasing",
    2: "haze",
    3: "smoke",
    4: "dust or sand in air",
    5: "duststorm or sandstorm",
    6: "mist",
    7: "fog",
    8: "freezing fog",
    9: "light drizzle",
    10: "drizzle",
    11: "freezing drizzle",
    12: "light rain",
    13: "rain",
    14: "heavy rain",
    15: "freezing rain",
    16: "rain showers",
    17: "light snow",
    18: "snow",
    19: "heavy snow",
    20: "rain and snow mixed",
    21: "snow showers",
    22: "snow grains",
    23: "ice pellets or hail",
    24: "thunderstorm without precipitation",
    25: "thunderstorm with rain",
    26: "thunderstorm with hail or snow",
    27: "squall",
    28: "tornado or waterspout",
    29: "blowing snow",
    30: "widespread dust",
    PRESENT_WEATHER_NOT_AVAILABLE: "not available",
}

# Visibility - WMO 코드표 4377의 축약(90~99)을 4 bit(0~9)로 옮긴 매핑.
VISIBILITY_NAMES = {
    0: "< 0.05 km",
    1: "0.05 km",
    2: "0.2 km",
    3: "0.5 km",
    4: "1 km",
    5: "2 km",
    6: "4 km",
    7: "10 km",
    8: "20 km",
    9: ">= 50 km",
    VISIBILITY_NOT_AVAILABLE: "not available",
}


# 섭씨 <-> raw 변환
def _temp_to_raw(celsius: float | None) -> int:
    if celsius is None:
        return TEMP_NOT_AVAILABLE
    raw = round((celsius + TEMP_OFFSET_C) * TEMP_SCALE)
    if not 0 <= raw < TEMP_NOT_AVAILABLE:
        raise ValueError(f"온도 표현 범위를 벗어났습니다 ({celsius} C)")
    return raw


def _raw_to_temp(raw: int) -> float | None:
    return None if raw == TEMP_NOT_AVAILABLE else raw / TEMP_SCALE - TEMP_OFFSET_C


@dataclass
class WeatherObservation:
    longitude: int = coordinates.LONGITUDE_NOT_AVAILABLE
    latitude: int = coordinates.LATITUDE_NOT_AVAILABLE
    obs_time: int = 0  # UTC 초 (2000 기준)
    air_temp: int = TEMP_NOT_AVAILABLE
    dew_point: int = TEMP_NOT_AVAILABLE
    sea_temp: int = TEMP_NOT_AVAILABLE
    pressure: int = PRESSURE_NOT_AVAILABLE
    pressure_tendency: int = PRESSURE_TENDENCY_NOT_AVAILABLE
    wind_dir: int = WIND_DIR_NOT_AVAILABLE
    wind_speed: int = WIND_SPEED_NOT_AVAILABLE
    wave_height: int = WAVE_HEIGHT_NOT_AVAILABLE
    wave_period: int = WAVE_PERIOD_NOT_AVAILABLE
    present_weather: int = PRESENT_WEATHER_NOT_AVAILABLE
    visibility: int = VISIBILITY_NOT_AVAILABLE

    @property
    def longitude_degrees(self) -> float | None:
        return coordinates.raw_to_longitude(self.longitude)

    @property
    def latitude_degrees(self) -> float | None:
        return coordinates.raw_to_latitude(self.latitude)

    @property
    def air_temp_c(self) -> float | None:
        return _raw_to_temp(self.air_temp)

    @property
    def dew_point_c(self) -> float | None:
        return _raw_to_temp(self.dew_point)

    @property
    def sea_temp_c(self) -> float | None:
        return _raw_to_temp(self.sea_temp)

    @property
    def pressure_hpa(self) -> float | None:
        return (
            None if self.pressure == PRESSURE_NOT_AVAILABLE
            else self.pressure / PRESSURE_SCALE + PRESSURE_OFFSET_HPA
        )

    @property
    def wind_dir_deg(self) -> int | None:
        return None if self.wind_dir == WIND_DIR_NOT_AVAILABLE else self.wind_dir

    @property
    def wind_speed_ms(self) -> float | None:
        return (
            None if self.wind_speed == WIND_SPEED_NOT_AVAILABLE
            else self.wind_speed / WIND_SPEED_SCALE
        )

    @property
    def wave_height_m(self) -> float | None:
        return (
            None if self.wave_height == WAVE_HEIGHT_NOT_AVAILABLE
            else self.wave_height / WAVE_HEIGHT_SCALE
        )

    @property
    def wave_period_s(self) -> int | None:
        return None if self.wave_period == WAVE_PERIOD_NOT_AVAILABLE else self.wave_period

    @property
    def present_weather_name(self) -> str:
        return PRESENT_WEATHER_NAMES.get(self.present_weather, f"reserved({self.present_weather})")

    @property
    def visibility_name(self) -> str:
        return VISIBILITY_NAMES.get(self.visibility, f"reserved({self.visibility})")


# 실측값으로 WeatherObservation을 만든다. None은 not available로 기록한다.
def from_measurements(
    longitude_degrees: float | None = None,
    latitude_degrees: float | None = None,
    obs_time: int = 0,
    air_temp_c: float | None = None,
    dew_point_c: float | None = None,
    sea_temp_c: float | None = None,
    pressure_hpa: float | None = None,
    pressure_tendency: int = PRESSURE_TENDENCY_NOT_AVAILABLE,
    wind_dir_deg: int | None = None,
    wind_speed_ms: float | None = None,
    wave_height_m: float | None = None,
    wave_period_s: int | None = None,
    present_weather: int = PRESENT_WEATHER_NOT_AVAILABLE,
    visibility: int = VISIBILITY_NOT_AVAILABLE,
) -> WeatherObservation:
    if pressure_hpa is None:
        pressure = PRESSURE_NOT_AVAILABLE
    else:
        pressure = round((pressure_hpa - PRESSURE_OFFSET_HPA) * PRESSURE_SCALE)
        if not 0 <= pressure < PRESSURE_NOT_AVAILABLE:
            raise ValueError(f"기압 표현 범위를 벗어났습니다 ({pressure_hpa} hPa)")

    return WeatherObservation(
        longitude=coordinates.longitude_to_raw(longitude_degrees),
        latitude=coordinates.latitude_to_raw(latitude_degrees),
        obs_time=obs_time,
        air_temp=_temp_to_raw(air_temp_c),
        dew_point=_temp_to_raw(dew_point_c),
        sea_temp=_temp_to_raw(sea_temp_c),
        pressure=pressure,
        pressure_tendency=pressure_tendency,
        wind_dir=WIND_DIR_NOT_AVAILABLE if wind_dir_deg is None else int(wind_dir_deg),
        wind_speed=(
            WIND_SPEED_NOT_AVAILABLE if wind_speed_ms is None
            else round(wind_speed_ms * WIND_SPEED_SCALE)
        ),
        wave_height=(
            WAVE_HEIGHT_NOT_AVAILABLE if wave_height_m is None
            else round(wave_height_m * WAVE_HEIGHT_SCALE)
        ),
        wave_period=WAVE_PERIOD_NOT_AVAILABLE if wave_period_s is None else int(wave_period_s),
        present_weather=present_weather,
        visibility=visibility,
    )


# 본문 bit를 writer에 이어 붙인다.
def write_body(writer: BitWriter, message: WeatherObservation) -> None:
    writer.write_int(message.longitude, coordinates.LONGITUDE_BITS, "Longitude")
    writer.write_int(message.latitude, coordinates.LATITUDE_BITS, "Latitude")
    writer.write_uint(message.obs_time, OBS_TIME_BITS, "Obs Time")
    writer.write_uint(message.air_temp, AIR_TEMP_BITS, "Air Temp")
    writer.write_uint(message.dew_point, DEW_POINT_BITS, "Dew Point")
    writer.write_uint(message.sea_temp, SEA_TEMP_BITS, "Sea Temp")
    writer.write_uint(message.pressure, PRESSURE_BITS, "Pressure")
    writer.write_uint(message.pressure_tendency, PRESSURE_TENDENCY_BITS, "Pressure Tendency")
    writer.write_uint(message.wind_dir, WIND_DIR_BITS, "Wind Dir")
    writer.write_uint(message.wind_speed, WIND_SPEED_BITS, "Wind Speed")
    writer.write_uint(message.wave_height, WAVE_HEIGHT_BITS, "Wave Height")
    writer.write_uint(message.wave_period, WAVE_PERIOD_BITS, "Wave Period")
    writer.write_uint(message.present_weather, PRESENT_WEATHER_BITS, "Present Weather")
    writer.write_uint(message.visibility, VISIBILITY_BITS, "Visibility")


# reader에서 본문을 읽어 WeatherObservation을 만든다.
def read_body(reader: BitReader) -> WeatherObservation:
    return WeatherObservation(
        longitude=reader.read_int(coordinates.LONGITUDE_BITS, "Longitude"),
        latitude=reader.read_int(coordinates.LATITUDE_BITS, "Latitude"),
        obs_time=reader.read_uint(OBS_TIME_BITS, "Obs Time"),
        air_temp=reader.read_uint(AIR_TEMP_BITS, "Air Temp"),
        dew_point=reader.read_uint(DEW_POINT_BITS, "Dew Point"),
        sea_temp=reader.read_uint(SEA_TEMP_BITS, "Sea Temp"),
        pressure=reader.read_uint(PRESSURE_BITS, "Pressure"),
        pressure_tendency=reader.read_uint(PRESSURE_TENDENCY_BITS, "Pressure Tendency"),
        wind_dir=reader.read_uint(WIND_DIR_BITS, "Wind Dir"),
        wind_speed=reader.read_uint(WIND_SPEED_BITS, "Wind Speed"),
        wave_height=reader.read_uint(WAVE_HEIGHT_BITS, "Wave Height"),
        wave_period=reader.read_uint(WAVE_PERIOD_BITS, "Wave Period"),
        present_weather=reader.read_uint(PRESENT_WEATHER_BITS, "Present Weather"),
        visibility=reader.read_uint(VISIBILITY_BITS, "Visibility"),
    )


# 콘솔 표시용 여러 줄 문자열
def describe(message: WeatherObservation) -> str:
    def show(value, unit: str = "") -> str:
        return "not available" if value is None else f"{value}{unit}"

    return "\n".join(
        [
            f"Position: {show(message.latitude_degrees)}, {show(message.longitude_degrees)}",
            f"Obs-time: {message.obs_time} (UTC s since 2000)",
            f"Air Temp: {show(message.air_temp_c, ' C')}",
            f"Dew Point: {show(message.dew_point_c, ' C')}",
            f"Sea Temp: {show(message.sea_temp_c, ' C')}",
            f"Pressure: {show(message.pressure_hpa, ' hPa')}",
            f"Wind: {show(message.wind_speed_ms, ' m/s')} @ {show(message.wind_dir_deg, ' deg')}",
            f"Wave: {show(message.wave_height_m, ' m')}, {show(message.wave_period_s, ' s')}",
            f"Present Weather: {message.present_weather_name}",
            f"Visibility: {message.visibility_name}",
        ]
    )
