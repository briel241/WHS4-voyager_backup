"""
항법 계산 - 방위, 거리, 추측항법(dead reckoning)

MASS가 항로를 따라 실제로 움직여야 위치보고에 변화가 나타나고, 그 변화를
관찰 도구가 보여줄 수 있다. 여기서는 그 최소한의 운동 모형만 둔다.

평면 근사
    VHF 도달 범위(수십 해리)에서는 지구 곡률을 무시한 평면 근사로 충분하다.
    위도 1분 = 1해리라는 관계를 쓰고, 경도는 위도에 따라 cos(lat)만큼 줄어든다.
    장거리 항해를 다루게 되면 대권항법으로 바꿔야 한다.

선회율(ROT) 부호화
    AIS는 실제 선회율(도/분)을 그대로 싣지 않고 ITU-R M.1371이 정한
    비선형 식을 쓴다.

        ROT_AIS = 4.733 x sqrt(ROT_sensor)

    -128은 not available이고, +-127은 규정 이상으로 빠른 선회를 뜻한다.
"""

from __future__ import annotations

import math

# 위도 1분 = 1해리
MINUTES_PER_DEGREE = 60.0
SECONDS_PER_HOUR = 3600.0

# ROT_AIS가 표현할 수 있는 최대 크기. +-127은 "규정 이상"이라 126까지만 쓴다.
_ROT_AIS_MAX = 126
_ROT_AIS_NOT_AVAILABLE = -128

# 극점 근처에서 cos(lat)이 0이 되는 것을 막는다.
_MIN_COS_LAT = 1e-6


# 각도를 0 이상 360 미만으로 맞춘다.
def normalize_degrees(degrees: float) -> float:
    return degrees % 360.0


# 두 침로의 차이를 -180..180으로 만든다. 어느 쪽으로 도는 것이 가까운지 나타낸다.
def heading_difference(from_degrees: float, to_degrees: float) -> float:
    return (to_degrees - from_degrees + 180.0) % 360.0 - 180.0


# 현재 위치에서 목표 지점을 향하는 진방위(도)를 구한다.
def bearing_degrees(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> float:
    mean_latitude = math.radians((from_latitude + to_latitude) / 2.0)
    north = (to_latitude - from_latitude) * MINUTES_PER_DEGREE
    east = (to_longitude - from_longitude) * MINUTES_PER_DEGREE * math.cos(mean_latitude)
    if north == 0.0 and east == 0.0:
        return 0.0
    return normalize_degrees(math.degrees(math.atan2(east, north)))


# 두 지점 사이의 거리를 해리(nautical mile)로 구한다.
def distance_nm(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> float:
    mean_latitude = math.radians((from_latitude + to_latitude) / 2.0)
    north = (to_latitude - from_latitude) * MINUTES_PER_DEGREE
    east = (to_longitude - from_longitude) * MINUTES_PER_DEGREE * math.cos(mean_latitude)
    return math.hypot(north, east)


# 주어진 침로로 주어진 거리만큼 나아간 위치를 구한다.
def advance(
    latitude: float, longitude: float, course_degrees: float, distance: float
) -> tuple[float, float]:
    course = math.radians(course_degrees)
    next_latitude = latitude + (distance * math.cos(course)) / MINUTES_PER_DEGREE

    cos_latitude = max(abs(math.cos(math.radians(latitude))), _MIN_COS_LAT)
    next_longitude = longitude + (distance * math.sin(course)) / (
        MINUTES_PER_DEGREE * cos_latitude
    )
    return next_latitude, next_longitude


# 속력(knot)과 경과 시간(초)으로 이동 거리(해리)를 구한다.
def travelled_nm(speed_knots: float, elapsed_seconds: float) -> float:
    return speed_knots * (elapsed_seconds / SECONDS_PER_HOUR)


# 목표 침로 쪽으로 최대 선회량만큼만 돌린다.
# 반환값은 (새 침로, 실제 선회량)이며 선회량의 부호는 우선회가 양수다.
def turn_toward(
    current_degrees: float, target_degrees: float, max_change: float
) -> tuple[float, float]:
    difference = heading_difference(current_degrees, target_degrees)
    change = max(-max_change, min(max_change, difference))
    return normalize_degrees(current_degrees + change), change


# 실제 선회율(도/분)을 AIS ROT 값으로 바꾼다. None은 not available이다.
def rot_to_ais(degrees_per_minute: float | None) -> int:
    if degrees_per_minute is None:
        return _ROT_AIS_NOT_AVAILABLE
    magnitude = min(round(4.733 * math.sqrt(abs(degrees_per_minute))), _ROT_AIS_MAX)
    return magnitude if degrees_per_minute >= 0 else -magnitude


# AIS ROT 값을 실제 선회율(도/분)로 되돌린다. not available이면 None이다.
def ais_to_rot(value: int) -> float | None:
    if value == _ROT_AIS_NOT_AVAILABLE:
        return None
    magnitude = (abs(value) / 4.733) ** 2
    return magnitude if value >= 0 else -magnitude
