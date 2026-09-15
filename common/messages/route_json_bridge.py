"""S-421 논리 JSON ↔ 우리 압축 비트 필드 변환 (작업 B).

새 링크 프로토콜을 만들지 않는다. 이미 구현된 **항로지시(Message ID 2)** ·
**ACK(Message ID 3)** 의 필드 값 dict 와, S-421 논리 JSON 모델 사이를 오가는
순수 함수만 제공한다. 표준 라이브러리 ``json`` 외 의존성 없음.

압축 형식은 metadata(routeID 문자열, name, validity 시각, author)를 비트로
담지 않는다. 이 변환기는 **압축이 담을 수 있는 필드만** 왕복 보존하고,
나머지는 ``metadata_not_in_bits()`` 로 분리 보고한다. 억지로 비트에 넣지 않는다.

==========================================================================
통합 전에 반드시 대조할 것 — 스케일 상수
==========================================================================
지시서 5.1 은 각 필드의 **비트 폭**만 알려주고 LSB 스케일은 알려주지 않는다.
아래 값은 AIS 관례(COG 12 bit @0.1°, SOG 10 bit @0.1 kn)에서 유추한 것이므로
저장소의 실제 항로지시 구현과 **대조해 맞춰야 한다**. 틀렸다면 이 상수 세 개만
고치면 되고 나머지 로직은 그대로다.

    HEADING_SCALE_DEG     0.1°   (12 bit → 0 ~ 409.5)   route_instruction.py 와 일치
    TURN_RADIUS_SCALE_NM  0.01 NM (10 bit → 0 ~ 10.23)  route_instruction.py 와 일치
    SOG_SCALE_KNOT        0.1 kn (10 bit → 0 ~ 102.3)   route_instruction.py 와 일치

좌표 단위(1/10,000분)와 열거값·상태 코드는 지시서에 명시돼 있어 확정이다.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------
# 압축 형식 상수 (지시서 5.1)
# --------------------------------------------------------------------------

MESSAGE_ID_ROUTE = 2                 # 항로지시
MESSAGE_ID_ACK = 3                   # 응용 ACK

HEADING_BITS = 12
FALLBACK_BITS = 3
LON_BITS = 28                        # 부호 있음
LAT_BITS = 27                        # 부호 있음
TURN_RADIUS_BITS = 10
PLAN_SOG_BITS = 10
GEOMETRY_BITS = 2
ACK_STATUS_BITS = 4

#: 좌표 LSB = 1/10,000 분 (확정)
COORD_UNITS_PER_MINUTE = 10_000

# route_instruction.py 의 SCALE 상수와 맞춘 값이다.
#   HEADING_SETPOINT_SCALE=10 → 0.1°, TURN_RADIUS_SCALE=100 → 0.01 NM,
#   PLANNED_SOG_SCALE=10 → 0.1 kn
HEADING_SCALE_DEG = 0.1
TURN_RADIUS_SCALE_NM = 0.01
SOG_SCALE_KNOT = 0.1

#: fallback 상태 3 bit (지시서 5.1). 없으면 JSON 에서 필드를 생략한다.
FALLBACK_STATES = {0: None, 1: "Anchoring", 2: "DynamicPositioning", 3: "Drifting"}
FALLBACK_CODES = {name: code for code, name in FALLBACK_STATES.items()
                  if name is not None}

#: leg 기하 2 bit. "Lexodrome" 철자는 지시서 5.1 표기를 그대로 따른다
#: (일반 표기는 Loxodrome — S-421 열거값 확인 필요).
LEG_GEOMETRIES = {0: None, 1: "Lexodrome", 2: "Orthodrome"}
GEOMETRY_CODES = {name: code for code, name in LEG_GEOMETRIES.items()
                  if name is not None}

#: routeInfoStatus (S-421 Edition 1.0.0)
ROUTE_STATUS_NAMES = {
    0: "received",
    3: "Recommended",                # 조작 데이터(항로지시)가 쓰는 값
    4: "Acknowledged",
    7: "Errors",
    8: "Incomplete",
    9: "Route issues",
}
ACK_STATUS_CODES = (0, 4, 7, 8, 9)   # ACK 로 되돌아올 수 있는 값

#: 압축 비트가 담지 못하는 논리 JSON 키
METADATA_NOT_IN_BITS = (
    "routeFormatVersion", "routeID", "routeEditionNo",
    "routeInfoName", "routeInfoAuthor", "routeInfoEditionTime",
    "routeInfoValidityStart", "routeInfoValidityEnd", "routeInfoVesselMMSI",
    "routeInfoDescription", "sourceMMSI", "destinationMMSI",
)


class RouteBridgeError(ValueError):
    """논리 JSON ↔ 압축 필드 변환 실패."""


# --------------------------------------------------------------------------
# 좌표 변환 (WGS84 / DMM ↔ 1/10,000 분)
# --------------------------------------------------------------------------

def _round_half_up(value: float) -> int:
    """0 에서 먼 쪽으로 반올림. 파이썬 기본 round 는 은행가 반올림이라 쓰지 않는다."""
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def dmm_to_dd(degrees: int, minutes: float, hemisphere: str) -> float:
    """DMM → 십진도. ``DD = deg + min/60``, S·W 면 음수 (지시서 5.2)."""
    hemisphere = str(hemisphere).upper()
    if hemisphere not in ("N", "S", "E", "W"):
        raise RouteBridgeError(f"알 수 없는 반구: {hemisphere!r}")
    if degrees < 0:
        raise RouteBridgeError("DMM 의 도(degrees)는 음수일 수 없다 — 부호는 반구가 정한다")
    if not (0 <= minutes < 60):
        raise RouteBridgeError(f"분(minutes)은 0 이상 60 미만이어야 한다: {minutes}")
    value = degrees + minutes / 60.0
    return -value if hemisphere in ("S", "W") else value


def dd_to_dmm(value: float, axis: str) -> dict:
    """십진도 → DMM dict. ``axis`` 는 "lat" 또는 "lon"."""
    if axis == "lat":
        positive, negative, limit = "N", "S", 90
    elif axis == "lon":
        positive, negative, limit = "E", "W", 180
    else:
        raise RouteBridgeError(f"axis 는 'lat' 또는 'lon': {axis!r}")
    if abs(value) > limit:
        raise RouteBridgeError(f"{axis} 범위 초과: {value}")

    hemisphere = negative if value < 0 else positive
    total_minutes = abs(value) * 60.0
    degrees = int(total_minutes // 60)
    minutes = total_minutes - degrees * 60
    return {"degrees": degrees, "minutes": minutes, "hemisphere": hemisphere}


def _coord_to_units(node: dict, axis: str) -> int:
    """DMM dict → 1/10,000 분 부호 정수."""
    if not isinstance(node, dict):
        raise RouteBridgeError(f"{axis} 좌표는 dict 여야 한다")
    try:
        degrees = int(node["degrees"])
        minutes = float(node["minutes"])
        hemisphere = node["hemisphere"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteBridgeError(
            f"{axis} 좌표에 degrees/minutes/hemisphere 가 필요하다: {exc}") from exc

    axis_hemispheres = ("N", "S") if axis == "lat" else ("E", "W")
    if str(hemisphere).upper() not in axis_hemispheres:
        raise RouteBridgeError(f"{axis} 반구는 {axis_hemispheres} 중 하나여야 한다")
    if degrees < 0:
        raise RouteBridgeError("DMM 의 도(degrees)는 음수일 수 없다 — 부호는 반구가 정한다")
    if not (0 <= minutes < 60):
        raise RouteBridgeError(f"{axis} 분(minutes)은 0 이상 60 미만이어야 한다: {minutes}")
    limit = 90 if axis == "lat" else 180
    if degrees > limit or (degrees == limit and minutes > 0):
        raise RouteBridgeError(f"{axis} 범위 초과: {degrees}°{minutes}′")

    units = _round_half_up((degrees * 60 + minutes) * COORD_UNITS_PER_MINUTE)
    if str(hemisphere).upper() in ("S", "W"):
        units = -units

    bits = LAT_BITS if axis == "lat" else LON_BITS
    bound = 1 << (bits - 1)
    if not (-bound <= units < bound):
        raise RouteBridgeError(f"{axis} 값이 {bits} bit 부호 정수 범위를 벗어난다: {units}")
    return units


def _units_to_coord(units: int, axis: str) -> dict:
    """1/10,000 분 부호 정수 → DMM dict."""
    total_minutes = abs(int(units)) / COORD_UNITS_PER_MINUTE
    degrees = int(total_minutes // 60)
    minutes = total_minutes - degrees * 60
    if axis == "lat":
        hemisphere = "S" if units < 0 else "N"
    else:
        hemisphere = "W" if units < 0 else "E"
    return {"degrees": degrees, "minutes": minutes, "hemisphere": hemisphere}


# --------------------------------------------------------------------------
# 스칼라 양자화
# --------------------------------------------------------------------------

def _quantize(value: float, scale: float, bits: int, label: str) -> int:
    raw = _round_half_up(float(value) / scale)
    if not (0 <= raw < (1 << bits)):
        raise RouteBridgeError(
            f"{label} 값 {value} 는 {bits} bit 로 표현할 수 없다 "
            f"(허용 0 ~ {((1 << bits) - 1) * scale:g})")
    return raw


def _dequantize(raw: int, scale: float) -> float:
    return round(int(raw) * scale, 6)


# --------------------------------------------------------------------------
# 논리 JSON → 압축 필드
# --------------------------------------------------------------------------

def _looks_like_ack(json_obj: dict) -> bool:
    """ACK 인지 조작 데이터인지 가른다.

    ACK 는 웨이포인트를 담지 않고 ``routeInfoStatus`` 가 4/7/8/9(또는 0)다
    (조작 데이터는 3=Recommended).
    """
    info = json_obj.get("routeInfo") or {}
    status = json_obj.get("routeInfoStatus", info.get("routeInfoStatus"))
    if not json_obj.get("routeWaypoints"):
        return True
    return status in ACK_STATUS_CODES and status != 3


def _schedule_by_waypoint(json_obj: dict) -> dict:
    """routeSchedule[] 을 웨이포인트 id → planned SOG 로 정리한다."""
    mapping = {}
    for item in json_obj.get("routeSchedule") or []:
        key = item.get("id", item.get("waypointId"))
        sog = item.get("planned_SOG", item.get("plannedSOG", item.get("plan_SOG")))
        if key is not None and sog is not None:
            mapping[key] = sog
    return mapping


def route_json_to_fields(json_obj: dict) -> dict:
    """S-421 논리 JSON → 항로지시/ACK 압축 필드 값 dict.

    조작 데이터면 ``message_id=2`` 필드를, ACK 면 ``message_id=3`` 필드를 낸다.
    압축이 못 담는 metadata 는 여기 넣지 않는다 —
    ``metadata_not_in_bits(json_obj)`` 로 따로 받는다.
    """
    if not isinstance(json_obj, dict):
        raise RouteBridgeError("논리 JSON 은 dict 여야 한다")

    info = json_obj.get("routeInfo") or {}

    if _looks_like_ack(json_obj):
        status = json_obj.get("routeInfoStatus", info.get("routeInfoStatus"))
        if status is None:
            raise RouteBridgeError("ACK 에 routeInfoStatus 가 없다")
        if status not in ACK_STATUS_CODES:
            raise RouteBridgeError(
                f"ACK 상태 코드가 아니다: {status} (허용 {ACK_STATUS_CODES})")
        if not (0 <= status < (1 << ACK_STATUS_BITS)):
            raise RouteBridgeError(f"상태 코드가 {ACK_STATUS_BITS} bit 를 넘는다: {status}")
        return {"message_id": MESSAGE_ID_ACK, "status": int(status)}

    # --- 조작 데이터(항로지시) ---------------------------------------------
    heading = json_obj.get("headingSetPoint")
    if heading is None:
        raise RouteBridgeError("headingSetPoint 가 없다")
    if not (0 <= float(heading) < 360):
        raise RouteBridgeError(f"headingSetPoint 는 0 이상 360 미만: {heading}")

    fallback = json_obj.get("fallbackState")
    if fallback is None:
        fallback_code = 0
    elif fallback in FALLBACK_CODES:
        fallback_code = FALLBACK_CODES[fallback]
    else:
        raise RouteBridgeError(
            f"알 수 없는 fallbackState: {fallback!r} (허용 {sorted(FALLBACK_CODES)})")

    schedule = _schedule_by_waypoint(json_obj)
    waypoints = []
    for index, waypoint in enumerate(json_obj["routeWaypoints"]):
        identifier = waypoint.get("id", index)
        position = waypoint.get("position") or waypoint
        geometry = waypoint.get("leg_geometry", waypoint.get("legGeometry"))
        if geometry is None:
            geometry_code = 0
        elif geometry in GEOMETRY_CODES:
            geometry_code = GEOMETRY_CODES[geometry]
        else:
            raise RouteBridgeError(
                f"알 수 없는 leg 기하: {geometry!r} (허용 {sorted(GEOMETRY_CODES)})")

        turn_radius = waypoint.get("turn_radius", waypoint.get("turnRadius", 0.0))
        plan_sog = schedule.get(identifier, waypoint.get("planned_SOG", 0.0))

        waypoints.append({
            "id": identifier,
            "lon": _coord_to_units(position["lon"], "lon"),
            "lat": _coord_to_units(position["lat"], "lat"),
            "turn_radius": _quantize(turn_radius, TURN_RADIUS_SCALE_NM,
                                     TURN_RADIUS_BITS, "turn_radius"),
            "plan_sog": _quantize(plan_sog, SOG_SCALE_KNOT,
                                  PLAN_SOG_BITS, "planned SOG"),
            "geometry": geometry_code,
        })

    if not waypoints:
        raise RouteBridgeError("웨이포인트가 하나도 없다")

    return {
        "message_id": MESSAGE_ID_ROUTE,
        "heading_setpoint": _quantize(heading, HEADING_SCALE_DEG,
                                      HEADING_BITS, "headingSetPoint"),
        "fallback_state": fallback_code,
        "waypoints": waypoints,
    }


# --------------------------------------------------------------------------
# 압축 필드 → 논리 JSON
# --------------------------------------------------------------------------

def fields_to_route_json(fields: dict) -> dict:
    """압축 필드 값 dict → S-421 논리 JSON (압축이 담은 부분만).

    metadata(routeID·name·validity·author·MMSI)는 비트에 없으므로 복원하지
    않는다. 필요하면 호출자가 ``metadata_not_in_bits()`` 결과를 병합한다.
    """
    if not isinstance(fields, dict):
        raise RouteBridgeError("압축 필드는 dict 여야 한다")

    message_id = fields.get("message_id")
    if message_id == MESSAGE_ID_ACK:
        status = int(fields["status"])
        if status not in ACK_STATUS_CODES:
            raise RouteBridgeError(f"ACK 상태 코드가 아니다: {status}")
        return {
            "routeInfoStatus": status,
            "routeInfoStatusName": ROUTE_STATUS_NAMES.get(status, "unknown"),
        }
    if message_id != MESSAGE_ID_ROUTE:
        raise RouteBridgeError(f"알 수 없는 message_id: {message_id!r}")

    fallback_code = int(fields.get("fallback_state", 0))
    if fallback_code not in FALLBACK_STATES:
        raise RouteBridgeError(f"fallback 코드 범위 밖: {fallback_code}")

    waypoints = []
    schedule = []
    for index, waypoint in enumerate(fields.get("waypoints") or []):
        identifier = waypoint.get("id", index)
        geometry_code = int(waypoint.get("geometry", 0))
        if geometry_code not in LEG_GEOMETRIES:
            raise RouteBridgeError(f"leg 기하 코드 범위 밖: {geometry_code}")

        entry = {
            "id": identifier,
            "position": {
                "lat": _units_to_coord(waypoint["lat"], "lat"),
                "lon": _units_to_coord(waypoint["lon"], "lon"),
            },
            "turn_radius": _dequantize(waypoint.get("turn_radius", 0),
                                       TURN_RADIUS_SCALE_NM),
        }
        if LEG_GEOMETRIES[geometry_code] is not None:
            entry["leg_geometry"] = LEG_GEOMETRIES[geometry_code]
        waypoints.append(entry)
        schedule.append({
            "id": identifier,
            "planned_SOG": _dequantize(waypoint.get("plan_sog", 0), SOG_SCALE_KNOT),
        })

    if not waypoints:
        raise RouteBridgeError("웨이포인트가 하나도 없다")

    result = {
        "headingSetPoint": _dequantize(fields["heading_setpoint"],
                                       HEADING_SCALE_DEG),
        "routeWaypoints": waypoints,
        "routeSchedule": schedule,
    }
    if FALLBACK_STATES[fallback_code] is not None:
        result["fallbackState"] = FALLBACK_STATES[fallback_code]
    return result


# --------------------------------------------------------------------------
# 압축이 담지 못하는 부분 — 분리 보고
# --------------------------------------------------------------------------

def metadata_not_in_bits(json_obj: dict) -> dict:
    """압축 비트가 담지 못해 버려지는 논리 JSON 항목을 모아 보고한다.

    ``sourceMMSI`` / ``destinationMMSI`` 는 본문 비트가 아니라 링크의
    Source/Destination ID 로 나른다(지시서 3.3). 여기서는 "본문 비트에 없음"
    으로 함께 보고하되 ``carried_by`` 로 구분해 준다.
    """
    if not isinstance(json_obj, dict):
        raise RouteBridgeError("논리 JSON 은 dict 여야 한다")

    flat = dict(json_obj)
    flat.update(json_obj.get("routeInfo") or {})

    dropped = {}
    for key in METADATA_NOT_IN_BITS:
        if key in flat and flat[key] is not None:
            dropped[key] = flat[key]

    link_carried = {k: dropped.pop(k) for k in ("sourceMMSI", "destinationMMSI")
                    if k in dropped}
    return {
        "dropped": dropped,                 # 어디에도 실리지 않음 — 별도 채널 필요
        "carried_by_link": link_carried,    # 링크 Source/Destination ID 로 대체
    }


def ack_json_for(operation_json: dict, status: int,
                 description: str | None = None) -> dict:
    """조작 데이터에 대한 논리 ACK JSON 을 만든다 (지시서 5.2 마지막 문단).

    source/destination MMSI 를 서로 바꾸고 같은 routeID·routeEditionNo 를
    되돌린다. 오류면 ``routeInfoDescription`` 에 사유를 적는다.
    이건 **논리 형식**이고, 압축 ACK 는 status 4 bit 만 나른다 —
    그래서 routeID 상관 문제가 생긴다(docs/작업B_ACK상관_갭분석.md 참조).
    """
    if status not in ACK_STATUS_CODES:
        raise RouteBridgeError(f"ACK 상태 코드가 아니다: {status}")
    info = operation_json.get("routeInfo") or {}
    ack = {
        "sourceMMSI": operation_json.get("destinationMMSI"),
        "destinationMMSI": operation_json.get("sourceMMSI"),
        "routeFormatVersion": operation_json.get("routeFormatVersion", "1.0"),
        "routeID": operation_json.get("routeID", info.get("routeID")),
        "routeEditionNo": operation_json.get("routeEditionNo",
                                            info.get("routeEditionNo")),
        "routeInfoStatus": status,
    }
    if description:
        ack["routeInfoDescription"] = description
    return ack
