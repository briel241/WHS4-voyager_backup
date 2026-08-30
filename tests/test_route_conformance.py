"""노선·조종 메시지의 표준 적합성 — 장비 없이 돌아간다.

실행: python3 tests/test_route_conformance.py

여기서 보는 것은 **본선이 받은 값을 얼마나 따져보는가**다.

승인된 SC-VDE codec은 canonical raw 허용 집합을 검사해서 이상한 값을 무선에
싣지 못하게 막는다. 그런데 그 codec은 **무선 경로만** 막는다 — 콘솔 기본값
(direct 전송)·loopback 데모·공격 실험의 주입 지점은 전부 codec을 지나지 않는다.
그래서 같은 규칙을 응용이 스스로 지켜야 무선을 타든 안 타든 배가 똑같이
행동한다 (M.2092-2 §A4-4.15).

이 시험은 VDETER를 import할 수 있으면 **링크 codec과 우리 판정을 나란히 놓고
대조**한다. 한쪽만 막는 필드가 생기면 여기서 걸린다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from MASS import MassAgent, ShipState
from ROC import RocAgent
from common import codec, coordinates
from common.messages import application_ack as ack
from common.messages import route_instruction as route

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


ROC_ID = 440_000_002


def fresh() -> MassAgent:
    return MassAgent(state=ShipState(latitude=35.0, longitude=129.0, sog=10.0))


def deliver(agent: MassAgent, message, now: float = 1.0):
    """메시지 하나를 넣고 응용 ACK을 돌려준다."""
    replies = agent.receive(codec.encode(message), source_id=ROC_ID, now=now)
    return codec.decode(replies[0].payload) if replies else None


def wp(lon: float, lat: float) -> route.Waypoint:
    return route.waypoint_from_degrees(lon, lat)


def command(**overrides) -> route.RouteInstruction:
    fields = dict(
        waypoints=[wp(129.00, 35.00), wp(129.20, 35.20)],
        linkage_id=5,
        sender_classification=route.SENDER_AUTHORITY,
        route_type=route.ROUTE_TYPE_MANDATORY,
    )
    fields.update(overrides)
    return route.RouteInstruction(**fields)


def with_route(linkage_id: int = 1) -> MassAgent:
    """항로 하나를 실은 본선."""
    agent = fresh()
    deliver(agent, command(linkage_id=linkage_id))
    assert agent.has_route and agent.route_linkage_id == linkage_id
    return agent


# ------------------------------------------------ C-1 취소는 Linkage ID에 묶인다

section("C-1 취소 지시가 Message Linkage ID에 묶이는가")

# Table 13.2의 Linkage ID는 "이 메시지가 어느 메시지에 대한 것인가"를 밝힌다.
# 그것을 보지 않으면 오래된·재전송된 취소 한 건이 지금 따라가는 항로를 지운다.
agent = with_route(linkage_id=1)
answer = deliver(agent, command(waypoints=[], linkage_id=999,
                                route_type=route.ROUTE_TYPE_CANCELLATION), now=2.0)
check(agent.has_route, "다른 Linkage(999) 취소가 활성 항로(1)를 지우지 않는다")
check(answer.status == ack.STATUS_ROUTE_ISSUES,
      "가리키는 항로가 없으면 route issues로 답한다", answer.status_name)

agent = with_route(linkage_id=1)
answer = deliver(agent, command(waypoints=[], linkage_id=1,
                                route_type=route.ROUTE_TYPE_CANCELLATION), now=2.0)
check(not agent.has_route, "같은 Linkage(1) 취소는 항로를 해제한다")
check(answer.status == ack.STATUS_ACKNOWLEDGED, "취소 수락은 acknowledged",
      answer.status_name)

# Duration 0도 Table 13.2가 정한 취소 표현이다. 같은 규칙을 받아야 한다.
agent = with_route(linkage_id=1)
deliver(agent, command(waypoints=[], linkage_id=777, duration=route.DURATION_CANCEL),
        now=2.0)
check(agent.has_route, "Duration 0 취소도 Linkage가 다르면 항로를 지우지 않는다")

agent = with_route(linkage_id=1)
deliver(agent, command(waypoints=[], linkage_id=1, duration=route.DURATION_CANCEL),
        now=2.0)
check(not agent.has_route, "Duration 0 + 같은 Linkage는 해제한다")

# Linkage ID 0은 not available이라 무엇도 가리키지 못한다.
agent = fresh()
answer = deliver(agent, command(waypoints=[], linkage_id=0,
                                route_type=route.ROUTE_TYPE_CANCELLATION))
check(answer.status == ack.STATUS_ROUTE_ISSUES,
      "Linkage 0(not available) 취소는 아무것도 가리키지 못한다", answer.status_name)

# 조종(waypoint 없는 지시)도 Linkage로 취소된다.
agent = fresh()
deliver(agent, command(waypoints=[], linkage_id=12,
                       route_type=route.ROUTE_TYPE_RECOMMENDED,
                       heading_setpoint=route.heading_setpoint_from_degrees(90.0)))
check(agent.heading_setpoint_degrees == 90.0, "조종 지시가 실렸다")
deliver(agent, command(waypoints=[], linkage_id=12,
                       route_type=route.ROUTE_TYPE_CANCELLATION), now=2.0)
check(agent.heading_setpoint_degrees is None, "같은 Linkage 취소가 조종을 푼다")


# --------------------------------------- C-2 canonical 규칙을 응용이 지키는가

section("C-2 전선에 실을 수 없는 값을 본선이 거절하는가")

# (설명, 메시지) — 전부 승인된 codec이 거부하는 값이다.
HOSTILE = [
    ("Sender Classification 예약(5)", command(sender_classification=5)),
    ("Route Type 예약(7)", command(route_type=7)),
    ("Route Type 30", command(route_type=30)),
    ("Route Type 3 (프로파일 밖)", command(route_type=route.ROUTE_TYPE_ALTERNATIVE)),
    ("Route Type 4 (프로파일 밖)",
     command(route_type=route.ROUTE_TYPE_RECOMMENDED_THROUGH_ICE)),
    ("Fallback State 예약(5)", command(fallback_state=5)),
    ("Fallback State 7", command(fallback_state=7)),
    ("Heading Setpoint 3601", command(heading_setpoint=3601)),
    ("Heading Setpoint 4095", command(heading_setpoint=4095)),
    ("Leg Geometry 예약(3)",
     command(waypoints=[route.Waypoint(longitude=77_400_000, latitude=21_060_000,
                                       leg_geometry=3)])),
    ("Longitude 200도",
     command(waypoints=[route.Waypoint(longitude=120_000_000, latitude=21_060_000)])),
    ("Latitude 100도",
     command(waypoints=[route.Waypoint(longitude=77_400_000, latitude=60_000_000)])),
    ("Start Date/Time 혼합 N/A",
     command(start_month=6, start_day=0, start_hour=24, start_minute=60)),
]

for label, message in HOSTILE:
    agent = with_route(linkage_id=1)
    before = agent.active_waypoint
    answer = deliver(agent, message, now=2.0)
    rejected = answer is not None and answer.status == ack.STATUS_ERRORS
    check(rejected, f"{label} 거절", answer.status_name if answer else "응답 없음")
    check(agent.active_waypoint == before, f"{label} — 기존 항로 유지")

# 정상 메시지는 그대로 통과해야 한다. 방어가 정상 운항을 막으면 안 된다.
section("정상 메시지는 통과한다")
GOOD = [
    ("기본 노선", command()),
    ("Route Type 2 recommended", command(route_type=route.ROUTE_TYPE_RECOMMENDED)),
    ("Route Type 5 ship route plan", command(route_type=route.ROUTE_TYPE_SHIP_ROUTE_PLAN)),
    ("Heading Setpoint 359.9도",
     command(waypoints=[],
             heading_setpoint=route.heading_setpoint_from_degrees(359.9))),
    ("Fallback Drifting", command(fallback_state=route.FALLBACK_DRIFTING)),
    ("Start Date/Time 정상 범위",
     command(start_month=6, start_day=15, start_hour=8, start_minute=30)),
    ("Leg Geometry Orthodrome",
     command(waypoints=[wp(129.0, 35.0),
                        route.waypoint_from_degrees(
                            129.2, 35.2, leg_geometry=route.LEG_GEOMETRY_ORTHODROME)])),
]
for label, message in GOOD:
    answer = deliver(fresh(), message)
    check(answer.status == ack.STATUS_ACKNOWLEDGED, f"{label} 수락",
          answer.status_name)

# 침로 359.95도가 "지시 없음"으로 둔갑하지 않는다 (B-1 회귀).
agent = fresh()
deliver(agent, command(waypoints=[],
                       heading_setpoint=route.heading_setpoint_from_degrees(359.95)))
check(agent.heading_setpoint_degrees == 0.0,
      "침로 359.95도는 0도로 접힌다 (not available이 아니다)",
      str(agent.heading_setpoint_degrees))


# ----------------------------------------- 링크 codec과 판정이 갈리지 않는가

section("링크 codec과의 대조")

try:
    sys.path.insert(0, str(ROOT / "VDETER" / "src"))
    from vdeter.common.errors import Correlation
    from vdeter.presentation.codecs.sc_vde_route_control import (
        _validate_command_fields,
    )

    def link_rejects(message) -> bool:
        application = codec.encode_body(message)

        def error(text, **_kw):
            return ("REJECT", text)

        result = _validate_command_fields(
            application, len(message.waypoints), error, Correlation()
        )
        return isinstance(result, tuple)

    mismatched = []
    for label, message in HOSTILE:
        app_rejects = route.canonical_violation(message) is not None
        if app_rejects != link_rejects(message):
            mismatched.append(label)
    check(not mismatched, f"거부 대상 {len(HOSTILE)}건이 링크 codec과 일치",
          ", ".join(mismatched))

    mismatched = []
    for label, message in GOOD:
        app_rejects = route.canonical_violation(message) is not None
        if app_rejects != link_rejects(message):
            mismatched.append(label)
    check(not mismatched, f"허용 대상 {len(GOOD)}건이 링크 codec과 일치",
          ", ".join(mismatched))

    from vdeter.presentation.codecs.sc_vde_route_control import (
        _ALLOWED_ROUTE_TYPES,
    )

    check(set(route.SC_VDE_ROUTE_TYPES) == set(_ALLOWED_ROUTE_TYPES),
          "SC_VDE_ROUTE_TYPES가 링크 codec 허용 집합과 같다",
          f"우리 {sorted(route.SC_VDE_ROUTE_TYPES)} / 링크 {sorted(_ALLOWED_ROUTE_TYPES)}")
except ImportError as exc:
    check(True, f"SKIP — VDETER를 불러올 수 없다 ({exc})")


# ------------------------------------------------------- 좌표 범위 보조 함수

section("좌표 raw 범위 판정")

check(coordinates.longitude_raw_ok(coordinates.longitude_to_raw(180.0)),
      "경도 180도는 유효")
check(coordinates.longitude_raw_ok(coordinates.LONGITUDE_NOT_AVAILABLE),
      "경도 not available은 유효한 값으로 본다")
check(not coordinates.longitude_raw_ok(coordinates.LONGITUDE_ABS_MAX + 1),
      "경도 180도 초과는 무효")
check(coordinates.latitude_raw_ok(-coordinates.LATITUDE_ABS_MAX),
      "위도 -90도는 유효")
check(not coordinates.latitude_raw_ok(coordinates.LATITUDE_ABS_MAX + 1),
      "위도 90도 초과는 무효")


# ------------------------------------------------------ C-3·C-4 콘솔 발행 검증

section("C-3·C-4 콘솔이 실을 수 없는 명령을 막는가")

from apps.console_server import Simulation  # noqa: E402

simulation = Simulation(transport_kind="direct")
GOOD_WAYPOINTS = [[35.10, 129.10], [35.20, 129.20]]


def submit(**body):
    try:
        simulation.submit_command(dict(kind="route", waypoints=GOOD_WAYPOINTS, **body))
        return None
    except ValueError as exc:
        return str(exc)


check(submit(route_type=route.ROUTE_TYPE_ALTERNATIVE) is not None,
      "Route Type 3은 발행 단계에서 거절한다")
check(submit(route_type=route.ROUTE_TYPE_RECOMMENDED_THROUGH_ICE) is not None,
      "Route Type 4는 발행 단계에서 거절한다")
check(submit(route_type=route.ROUTE_TYPE_CANCELLATION) is not None,
      "취소는 노선 발행으로 낼 수 없다")
check(submit(duration=route.DURATION_CANCEL) is not None,
      "유효 시간 0(=취소)은 노선 발행으로 낼 수 없다")
check(submit(route_type=route.ROUTE_TYPE_MANDATORY) is None,
      "정상 노선은 발행된다")
check(submit(duration=60) is None, "유효 시간 60분은 발행된다")

# GUI 선택지가 전선 형식 밖으로 나가지 않는가
import re  # noqa: E402

html = (ROOT / "apps" / "console_static" / "roc.html").read_text(encoding="utf-8")
select = re.search(r'<select id="route-type">(.*?)</select>', html, re.S)
offered = {int(v) for v in re.findall(r'value="(\d+)"', select.group(1))}
check(offered <= set(route.SC_VDE_ROUTE_TYPES),
      "ROC 화면의 항로 종류가 모두 전선 형식 안에 있다",
      f"제공 {sorted(offered)} / 허용 {sorted(route.SC_VDE_ROUTE_TYPES)}")
check(route.ROUTE_TYPE_ALTERNATIVE not in offered,
      "Alternative(3)는 더 이상 제공하지 않는다")


# --------------------------------------------- 문서 §8 거부표가 여전히 맞는가

section("문서 SC-VDE-005 §8 거부표 대조")

TABLE = [
    ("waypoint 없음", command(waypoints=[], linkage_id=9),
     ack.STATUS_INCOMPLETE, True),
    ("Route Type 미지정(0)",
     command(waypoints=[wp(129.0, 35.0)], linkage_id=9,
             route_type=route.ROUTE_TYPE_NOT_AVAILABLE),
     ack.STATUS_INCOMPLETE, True),
    ("좌표가 not available",
     command(waypoints=[route.Waypoint()], linkage_id=9),
     ack.STATUS_INCOMPLETE, True),
    ("취소 지시 (같은 Linkage)",
     command(waypoints=[], linkage_id=1, route_type=route.ROUTE_TYPE_CANCELLATION),
     ack.STATUS_ACKNOWLEDGED, False),
]
for label, message, expected_status, keeps_route in TABLE:
    agent = with_route(linkage_id=1)
    answer = deliver(agent, message, now=2.0)
    check(answer.status == expected_status, f"{label} → {ack.STATUS_NAMES[expected_status]}",
          answer.status_name)
    check(agent.has_route == keeps_route,
          f"{label} → 기존 항로 {'유지' if keeps_route else '해제'}")

agent = with_route(linkage_id=1)
replies = agent.receive(b"\x00\x01\x02rubbish", source_id=ROC_ID, now=2.0)
check(not replies and agent.has_route,
      "다른 응용의 payload는 응답 없이 무시하고 항로를 유지한다")


# ------------------------------------------------- 거절해도 배는 움직이지 않는다

section("거절된 지시가 조타에 닿지 않는가")

agent = fresh()
agent.state.heading = 0.0
deliver(agent, command(waypoints=[], heading_setpoint=4000))   # 400.0도
for now in (2.0, 30.0, 60.0):
    agent.tick(now)
check(agent.heading_setpoint_degrees is None, "범위 밖 침로는 반영되지 않는다")
check(abs(agent.state.heading) < 1e-6,
      "배가 돌지 않았다 (예전에는 400도가 40도로 해석돼 선회했다)",
      f"{agent.state.heading:.1f}도")


# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
