"""작업 B 수용조건 검증 (지시서 5.3-3). 표준 라이브러리 assert 만 사용.

    python3 tests/test_route_json_bridge.py

핵심: JSON → fields → JSON 라운드트립에서
  * 좌표 오차가 1/10,000 분 이내
  * status · fallback · leg 기하 열거값이 보존됨
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.messages import route_json_bridge as bridge

#: 1/10,000 분을 십진도로 환산한 허용 오차
TOLERANCE_MINUTES = 1.0 / bridge.COORD_UNITS_PER_MINUTE


OPERATION_JSON = {
    "sourceMMSI": "004401234",              # 선행 0 보존 확인용
    "destinationMMSI": "440123456",
    "routeFormatVersion": "1.0",
    "routeID": "urn:route:roc01:2026-0042",
    "routeEditionNo": 3,
    "routeInfo": {
        "routeInfoName": "부산 진입 항로",
        "routeInfoAuthor": "ROC-01",
        "routeInfoEditionTime": "2026-08-26T00:00:00Z",
        "routeInfoStatus": 3,               # Recommended
        "routeInfoValidityStart": "2026-08-26T00:00:00Z",
        "routeInfoValidityEnd": "2026-08-27T00:00:00Z",
        "routeInfoVesselMMSI": "440123456",
    },
    "headingSetPoint": 127.4,
    "fallbackState": "DynamicPositioning",
    "routeWaypoints": [
        {"id": "WP1",
         "position": {"lat": {"degrees": 35, "minutes": 6.1234, "hemisphere": "N"},
                      "lon": {"degrees": 129, "minutes": 4.5678, "hemisphere": "E"}},
         "turn_radius": 0.5, "leg_geometry": "Lexodrome"},
        {"id": "WP2",
         "position": {"lat": {"degrees": 35, "minutes": 12.9999, "hemisphere": "N"},
                      "lon": {"degrees": 129, "minutes": 20.0001, "hemisphere": "E"}},
         "turn_radius": 1.2, "leg_geometry": "Orthodrome"},
        {"id": "WP3",
         "position": {"lat": {"degrees": 33, "minutes": 59.9999, "hemisphere": "S"},
                      "lon": {"degrees": 18, "minutes": 25.5000, "hemisphere": "W"}},
         "turn_radius": 0.0},
    ],
    "routeSchedule": [
        {"id": "WP1", "planned_SOG": 12.5},
        {"id": "WP2", "planned_SOG": 8.0},
        {"id": "WP3", "planned_SOG": 0.0},
    ],
}


def dd_of(node):
    return bridge.dmm_to_dd(node["degrees"], node["minutes"], node["hemisphere"])


# --------------------------------------------------------------------------
# 1. 좌표 오차가 1/10,000 분 이내
# --------------------------------------------------------------------------

def check_coordinate_precision():
    fields = bridge.route_json_to_fields(OPERATION_JSON)
    restored = bridge.fields_to_route_json(fields)

    originals = OPERATION_JSON["routeWaypoints"]
    assert len(restored["routeWaypoints"]) == len(originals)

    for original, back in zip(originals, restored["routeWaypoints"]):
        for axis in ("lat", "lon"):
            before = dd_of(original["position"][axis])
            after = dd_of(back["position"][axis])
            error_minutes = abs(before - after) * 60
            assert error_minutes <= TOLERANCE_MINUTES, (
                f"{original['id']} {axis} 오차 {error_minutes:.8f}분 "
                f"(허용 {TOLERANCE_MINUTES}분)")
            # 반구도 보존
            assert original["position"][axis]["hemisphere"] \
                == back["position"][axis]["hemisphere"]

    # 극단값도 비트 범위 안에 들어간다
    for degrees, minutes, hemisphere, axis in (
            (90, 0.0, "N", "lat"), (90, 0.0, "S", "lat"),
            (180, 0.0, "E", "lon"), (180, 0.0, "W", "lon"),
            (0, 0.0, "N", "lat"), (0, 0.0, "E", "lon")):
        node = {"degrees": degrees, "minutes": minutes, "hemisphere": hemisphere}
        units = bridge._coord_to_units(node, axis)
        back = bridge._units_to_coord(units, axis)
        assert back["degrees"] == degrees, (node, back)
        assert abs(back["minutes"] - minutes) <= TOLERANCE_MINUTES

    # DD 변환 공식 (지시서 5.2)
    assert abs(bridge.dmm_to_dd(35, 30.0, "N") - 35.5) < 1e-12
    assert abs(bridge.dmm_to_dd(35, 30.0, "S") + 35.5) < 1e-12
    assert bridge.dd_to_dmm(-35.5, "lat") == {
        "degrees": 35, "minutes": 30.0, "hemisphere": "S"}


# --------------------------------------------------------------------------
# 2. 열거값 보존 (status · fallback · geometry)
# --------------------------------------------------------------------------

def check_enums_preserved():
    fields = bridge.route_json_to_fields(OPERATION_JSON)
    restored = bridge.fields_to_route_json(fields)

    assert fields["message_id"] == bridge.MESSAGE_ID_ROUTE
    assert restored["fallbackState"] == "DynamicPositioning"
    assert fields["fallback_state"] == 2

    geometries = [w.get("leg_geometry") for w in restored["routeWaypoints"]]
    assert geometries == ["Lexodrome", "Orthodrome", None], geometries
    assert [w["geometry"] for w in fields["waypoints"]] == [1, 2, 0]

    # fallback 전 코드 왕복
    for code, name in bridge.FALLBACK_STATES.items():
        payload = dict(OPERATION_JSON)
        if name is None:
            payload.pop("fallbackState", None)
        else:
            payload["fallbackState"] = name
        back = bridge.fields_to_route_json(bridge.route_json_to_fields(payload))
        assert back.get("fallbackState") == name, (code, name, back.get("fallbackState"))

    # ACK 상태 코드 왕복
    for status in bridge.ACK_STATUS_CODES:
        ack = bridge.ack_json_for(OPERATION_JSON, status)
        fields = bridge.route_json_to_fields(ack)
        assert fields["message_id"] == bridge.MESSAGE_ID_ACK
        assert fields["status"] == status
        assert bridge.fields_to_route_json(fields)["routeInfoStatus"] == status

    assert bridge.fields_to_route_json(
        {"message_id": 3, "status": 4})["routeInfoStatusName"] == "Acknowledged"


# --------------------------------------------------------------------------
# 3. 스칼라 값 왕복 (heading · turn_radius · planned SOG)
# --------------------------------------------------------------------------

def check_scalars():
    fields = bridge.route_json_to_fields(OPERATION_JSON)
    restored = bridge.fields_to_route_json(fields)

    assert abs(restored["headingSetPoint"] - 127.4) <= bridge.HEADING_SCALE_DEG / 2
    radii = [w["turn_radius"] for w in restored["routeWaypoints"]]
    for before, after in zip((0.5, 1.2, 0.0), radii):
        assert abs(before - after) <= bridge.TURN_RADIUS_SCALE_NM / 2

    sogs = [s["planned_SOG"] for s in restored["routeSchedule"]]
    for before, after in zip((12.5, 8.0, 0.0), sogs):
        assert abs(before - after) <= bridge.SOG_SCALE_KNOT / 2

    # 웨이포인트 id 로 스케줄이 짝지어진다 (배열 순서가 달라도)
    shuffled = dict(OPERATION_JSON)
    shuffled["routeSchedule"] = list(reversed(OPERATION_JSON["routeSchedule"]))
    fields = bridge.route_json_to_fields(shuffled)
    assert [w["plan_sog"] for w in fields["waypoints"]] == [125, 80, 0]

    # 비트 폭을 넘는 값은 거부
    for key, value in (("headingSetPoint", 360.0),
                       ("headingSetPoint", -1.0)):
        payload = dict(OPERATION_JSON, **{key: value})
        try:
            bridge.route_json_to_fields(payload)
        except bridge.RouteBridgeError:
            pass
        else:
            raise AssertionError(f"{key}={value} 가 허용됐다")

    payload = json.loads(json.dumps(OPERATION_JSON))
    payload["routeWaypoints"][0]["turn_radius"] = 200.0     # 10 bit @0.1 초과
    try:
        bridge.route_json_to_fields(payload)
    except bridge.RouteBridgeError:
        pass
    else:
        raise AssertionError("turn_radius 200 NM 이 허용됐다")


# --------------------------------------------------------------------------
# 4. 압축 미포함 metadata 분리 보고
# --------------------------------------------------------------------------

def check_metadata_split():
    fields = bridge.route_json_to_fields(OPERATION_JSON)
    report = bridge.metadata_not_in_bits(OPERATION_JSON)

    # 압축 필드에는 metadata 가 섞여 있지 않다
    for key in bridge.METADATA_NOT_IN_BITS:
        assert key not in fields, f"압축 필드에 metadata {key} 가 들어갔다"

    dropped = report["dropped"]
    assert dropped["routeID"] == "urn:route:roc01:2026-0042"
    assert dropped["routeEditionNo"] == 3
    assert dropped["routeInfoName"] == "부산 진입 항로"
    assert dropped["routeInfoAuthor"] == "ROC-01"
    assert "routeInfoValidityEnd" in dropped

    # MMSI 는 링크 Source/Destination ID 로 나른다 — 선행 0 보존
    assert report["carried_by_link"]["sourceMMSI"] == "004401234"
    assert report["carried_by_link"]["destinationMMSI"] == "440123456"
    assert "sourceMMSI" not in dropped

    # ACK 는 같은 routeID·editionNo 를 되돌리고 MMSI 를 뒤집는다
    ack = bridge.ack_json_for(OPERATION_JSON, 7, description="WP2 좌표 범위 오류")
    assert ack["routeID"] == OPERATION_JSON["routeID"]
    assert ack["routeEditionNo"] == OPERATION_JSON["routeEditionNo"]
    assert ack["sourceMMSI"] == OPERATION_JSON["destinationMMSI"]
    assert ack["destinationMMSI"] == OPERATION_JSON["sourceMMSI"]
    assert ack["routeInfoDescription"] == "WP2 좌표 범위 오류"

    # 그러나 압축 ACK 에는 routeID 가 남지 않는다 — 상관 갭 (docs 갭분석 참조)
    ack_fields = bridge.route_json_to_fields(ack)
    assert set(ack_fields) == {"message_id", "status"}


# --------------------------------------------------------------------------
# 5. 안정성: 두 번 왕복해도 값이 변하지 않는다
# --------------------------------------------------------------------------

def check_idempotent_roundtrip():
    fields = bridge.route_json_to_fields(OPERATION_JSON)
    once = bridge.fields_to_route_json(fields)
    twice = bridge.fields_to_route_json(bridge.route_json_to_fields(once))
    assert once == twice, "2회차 왕복에서 값이 흔들린다"
    # JSON 직렬화 가능
    assert json.loads(json.dumps(once, ensure_ascii=False)) == once


def check_input_validation():
    bad_cases = [
        {},                                                     # 빈 dict
        {"headingSetPoint": 10, "routeWaypoints": []},           # 웨이포인트 없음
        {"routeInfoStatus": 5},                                  # ACK 코드 아님
    ]
    for payload in bad_cases:
        try:
            bridge.route_json_to_fields(payload)
        except bridge.RouteBridgeError:
            pass
        else:
            raise AssertionError(f"잘못된 입력이 통과했다: {payload}")

    payload = json.loads(json.dumps(OPERATION_JSON))
    payload["fallbackState"] = "Hovering"                        # 없는 열거값
    try:
        bridge.route_json_to_fields(payload)
    except bridge.RouteBridgeError:
        pass
    else:
        raise AssertionError("없는 fallback 열거값이 통과했다")

    for node, axis in (({"degrees": 35, "minutes": 60.0, "hemisphere": "N"}, "lat"),
                       ({"degrees": 91, "minutes": 0.0, "hemisphere": "N"}, "lat"),
                       ({"degrees": 35, "minutes": 0.0, "hemisphere": "E"}, "lat")):
        try:
            bridge._coord_to_units(node, axis)
        except bridge.RouteBridgeError:
            pass
        else:
            raise AssertionError(f"잘못된 좌표가 통과했다: {node}")


CHECKS = (
    ("1. 좌표 정밀도", check_coordinate_precision),
    ("2. 열거값 보존", check_enums_preserved),
    ("3. 스칼라 왕복", check_scalars),
    ("4. metadata 분리 보고", check_metadata_split),
    ("5. 왕복 안정성", check_idempotent_roundtrip),
    ("6. 입력 검증", check_input_validation),
)


if __name__ == "__main__":
    for title, check in CHECKS:
        try:
            check()
        except AssertionError as exc:
            raise AssertionError(f"{title} 실패: {exc}") from exc
