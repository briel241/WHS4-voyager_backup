"""
좌표 부호화 - AIS와 동일한 1/10,000 분 정수

근거
    AIS Position Report(ITU-R M.1371)와 IMO SN.1/Circ.289 Table 13.3의
    Route information waypoint가 같은 부호화를 쓴다.
    경도 28 bit, 위도 27 bit, 2의 보수, 단위는 1/10,000 분.

    1도 = 60분이므로 1도 = 600,000 단위다.

"값 없음"
    경도 181도, 위도 91도가 표준이 정한 not available 값이다.
    실제 좌표로는 나올 수 없는 값을 쓴다.
"""

from __future__ import annotations

LONGITUDE_BITS = 28
LATITUDE_BITS = 27

# 1도당 단위 수 (60분 x 10,000)
UNITS_PER_DEGREE = 600_000

# 표준이 정한 not available 값
LONGITUDE_NOT_AVAILABLE = 181 * UNITS_PER_DEGREE  # 108,600,000
LATITUDE_NOT_AVAILABLE = 91 * UNITS_PER_DEGREE  # 54,600,000

LONGITUDE_MIN_DEGREES = -180.0
LONGITUDE_MAX_DEGREES = 180.0
LATITUDE_MIN_DEGREES = -90.0
LATITUDE_MAX_DEGREES = 90.0

# raw 유효 범위. not available 값은 이 범위 **밖**에 있다(181도·91도).
#
# 부호화 폭(28·27 bit)이 담을 수 있는 값은 이 범위보다 넓다. 그 사이 구간은
# 지구상에 없는 좌표이므로 수신 시 걸러야 한다 — 그러지 않으면 위도 100도
# 같은 값이 항로로 실린다.
LONGITUDE_ABS_MAX = 180 * UNITS_PER_DEGREE  # 108,000,000
LATITUDE_ABS_MAX = 90 * UNITS_PER_DEGREE  # 54,000,000


# 도 단위 경도를 1/10,000 분 정수로 바꾼다. None은 not available로 기록한다.
def longitude_to_raw(degrees: float | None) -> int:
    if degrees is None:
        return LONGITUDE_NOT_AVAILABLE
    if not LONGITUDE_MIN_DEGREES <= degrees <= LONGITUDE_MAX_DEGREES:
        raise ValueError(f"경도 범위(-180..180)를 벗어났습니다 ({degrees})")
    return round(degrees * UNITS_PER_DEGREE)


# 도 단위 위도를 1/10,000 분 정수로 바꾼다. None은 not available로 기록한다.
def latitude_to_raw(degrees: float | None) -> int:
    if degrees is None:
        return LATITUDE_NOT_AVAILABLE
    if not LATITUDE_MIN_DEGREES <= degrees <= LATITUDE_MAX_DEGREES:
        raise ValueError(f"위도 범위(-90..90)를 벗어났습니다 ({degrees})")
    return round(degrees * UNITS_PER_DEGREE)


# 1/10,000 분 정수를 도 단위로 되돌린다. not available이면 None이다.
def raw_to_longitude(raw: int) -> float | None:
    if raw == LONGITUDE_NOT_AVAILABLE:
        return None
    return raw / UNITS_PER_DEGREE


# 1/10,000 분 정수를 도 단위로 되돌린다. not available이면 None이다.
def raw_to_latitude(raw: int) -> float | None:
    if raw == LATITUDE_NOT_AVAILABLE:
        return None
    return raw / UNITS_PER_DEGREE


# 수신한 raw 경도가 쓸 수 있는 값인가. not available도 쓸 수 있는 값으로 본다.
def longitude_raw_ok(raw: int) -> bool:
    return raw == LONGITUDE_NOT_AVAILABLE or -LONGITUDE_ABS_MAX <= raw <= LONGITUDE_ABS_MAX


# 수신한 raw 위도가 쓸 수 있는 값인가. not available도 쓸 수 있는 값으로 본다.
def latitude_raw_ok(raw: int) -> bool:
    return raw == LATITUDE_NOT_AVAILABLE or -LATITUDE_ABS_MAX <= raw <= LATITUDE_ABS_MAX
