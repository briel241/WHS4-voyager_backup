"""MASS/ROC 행위자 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_agents.py

메시지 왕복은 test_mass_roc_messages.py가, 링크 통과는
test_mass_roc_link_integration.py가 본다. 여기서는 행위자의 행동을 본다.

    - 항법 계산이 맞는가 (방위, 거리, ROT 부호화)
    - 항로를 실제로 따라가는가
    - 잘못된 지시를 거부하는가, 그때 상태가 바뀌지 않는가
    - 통신 두절을 판정하는가
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common import codec, navigation
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.outbound import Outbound
from common.messages import application_ack as ack
from common.messages import position_report as pos
from common.messages import route_instruction as route

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# 두 행위자를 직접 연결해 지정한 시간만큼 돌린다(링크 계층 없이 payload만 전달).
# 시계는 start부터 이어지며, 끝난 시각을 돌려준다. 행위자가 경과 시간으로
# 움직이므로 시계를 되돌리면 정지한 것처럼 보인다.
def run(
    mass: MassAgent, roc: RocAgent, seconds: float, start: float = 0.0, step: float = 1.0
) -> float:
    now = start
    end = start + seconds
    while now <= end:
        for outbound in mass.tick(now):
            roc.receive(outbound.payload, mass.mmsi, now)
        roc.tick(now)
        now += step
    return now


# ------------------------------------------------------------------ 항법

section("navigation — 방위와 거리")

# 정북/정동은 손으로 검산할 수 있다.
check(
    abs(navigation.bearing_degrees(35.0, 129.0, 36.0, 129.0) - 0.0) < 1e-6,
    "정북 방위 = 0도",
)
check(
    abs(navigation.bearing_degrees(35.0, 129.0, 35.0, 130.0) - 90.0) < 1e-6,
    "정동 방위 = 90도",
)
check(
    abs(navigation.bearing_degrees(35.0, 129.0, 34.0, 129.0) - 180.0) < 1e-6,
    "정남 방위 = 180도",
)

# 위도 1분 = 1해리
check(
    abs(navigation.distance_nm(35.0, 129.0, 35.0 + 1 / 60, 129.0) - 1.0) < 1e-6,
    "위도 1분 = 1해리",
)

# 12 knot로 30분이면 6해리
check(abs(navigation.travelled_nm(12.0, 1800.0) - 6.0) < 1e-9, "12kn x 30분 = 6해리")

# 나아간 뒤 거리가 맞아야 한다
lat, lon = navigation.advance(35.0, 129.0, 90.0, 1.0)
check(abs(navigation.distance_nm(35.0, 129.0, lat, lon) - 1.0) < 1e-6,
      "정동으로 1해리 이동")

# 선회 방향 (짧은 쪽으로 돈다)
check(navigation.heading_difference(350.0, 10.0) == 20.0, "350->10도는 우선회 20도")
check(navigation.heading_difference(10.0, 350.0) == -20.0, "10->350도는 좌선회 20도")
heading, change = navigation.turn_toward(90.0, 0.0, 30.0)
check(heading == 60.0 and change == -30.0, "선회량 상한이 걸린다", "90->0도, 상한 30도")

section("navigation — AIS ROT 부호화 (ITU-R M.1371)")

check(navigation.rot_to_ais(None) == -128, "ROT None = -128 (not available)")
check(navigation.rot_to_ais(0.0) == 0, "선회 없음 = 0")
# ROT_AIS = 4.733 * sqrt(ROT), 예: 10도/분 -> 4.733*sqrt(10) = 14.97 -> 15
check(navigation.rot_to_ais(10.0) == 15, "10도/분 -> 15", "4.733 x sqrt(10)")
check(navigation.rot_to_ais(-10.0) == -15, "좌선회는 음수")
check(abs(navigation.ais_to_rot(15) - 10.04) < 0.05, "역변환이 근사 복원된다")
check(navigation.ais_to_rot(-128) is None, "-128은 None으로 돌아온다")

# ------------------------------------------------------- MASS 기본 동작

section("MassAgent — 위치보고 주기")

mass = MassAgent(
    state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0),
    report_interval=2.0,
)
check(len(mass.tick(0.0)) == 1, "첫 tick에 위치보고를 낸다")
check(len(mass.tick(1.0)) == 0, "주기 전에는 내지 않는다")
check(len(mass.tick(2.0)) == 1, "주기가 되면 낸다")

outbound = mass.tick(4.0)[0]
check(isinstance(outbound, Outbound), "Outbound를 낸다")
check(outbound.is_broadcast, "위치보고는 broadcast")
check(not outbound.ack_required, "위치보고는 ACK 불요 (Type 93)")

report = codec.decode(outbound.payload)
check(isinstance(report, pos.PositionReport), "payload가 위치보고다")
check(abs(report.sog_knots - 12.0) < 1e-9, "SOG가 실린다")
check(report.longitude_degrees > 129.0, "정동 항행으로 경도가 늘었다",
      f"{report.longitude_degrees:.5f}")

section("MassAgent — Outbound 안전장치")

try:
    Outbound(payload=b"x", destination_id=0, ack_required=True)
    check(False, "ACK 요구 broadcast를 거부한다")
except ValueError:
    check(True, "ACK 요구 broadcast를 거부한다", "M.2092-2 Table 40")

# --------------------------------------------------------- 항로 추종

section("MassAgent — 항로 추종")

mass = MassAgent(
    state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0),
    max_turn_rate=60.0,
)
roc = RocAgent()
check(not mass.has_route, "처음에는 항로가 없다")

command = roc.issue_route(mass.mmsi, [(35.02, 129.02), (35.04, 129.02)], now=0.0)
check(command.ack_required, "항로 지시는 ACK 요구 (Type 92)")
check(not command.is_broadcast, "항로 지시는 broadcast가 아니다")

replies = mass.receive(command.payload, roc.mmsi, 0.0)
check(mass.has_route, "항로를 받아들였다")
check(mass.remaining_waypoints == 2, "waypoint 2개")
check(len(replies) == 1, "ACK를 하나 낸다")

reply = codec.decode(replies[0].payload)
command_linkage = codec.decode(command.payload).linkage_id
check(reply.status == ack.STATUS_ACKNOWLEDGED, "ACK acknowledged")
check(reply.linkage_id == command_linkage,
      "ACK가 같은 Linkage ID를 참조한다", f"Linkage {command_linkage}")

# ROC가 응답을 받으면 대기 목록에서 빠진다
roc.receive(replies[0].payload, mass.mmsi, 0.0)
check(len(roc.unanswered) == 0, "ROC의 대기 명령이 해소된다")

# 실제로 따라가는지
start_heading = mass.state.heading
clock = run(mass, roc, seconds=60.0)
check(mass.state.heading != start_heading, "항로 쪽으로 침로를 바꾼다",
      f"{start_heading:.0f} -> {mass.state.heading:.1f}도")

# 총 항정 약 2.75해리, 12kn이면 약 825초가 필요하다.
run(mass, roc, seconds=1200.0, start=clock)
check(mass.remaining_waypoints == 0, "모든 waypoint를 통과한다")
check(mass.state.latitude > 35.03, "북쪽으로 항행했다", f"{mass.state.latitude:.4f}")

section("MassAgent — 선회 반경보다 가까운 waypoint도 막히지 않는다")

# 도착 반경만으로 판정하면 지나친 waypoint를 영원히 쫓게 된다.
tight = MassAgent(
    state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0),
    max_turn_rate=20.0,  # 선회 반경 약 0.57해리
)
tight_roc = RocAgent()
# 0.1해리 간격 — 선회 반경보다 훨씬 가깝다
close_route = [(35.0 + 0.1 / 60, 129.0), (35.0 + 0.2 / 60, 129.0)]
tight.receive(tight_roc.issue_route(tight.mmsi, close_route, now=0.0).payload,
              tight_roc.mmsi, 0.0)
run(tight, tight_roc, seconds=600.0)
check(tight.remaining_waypoints == 0, "수직 통과 판정으로 항로가 끝난다",
      "지나친 waypoint에 갇히지 않는다")

# ------------------------------------------------------- 검증과 거부

section("MassAgent — 잘못된 지시는 상태를 바꾸지 않는다")

mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0))
roc = RocAgent()
mass.receive(roc.issue_route(mass.mmsi, [(35.02, 129.02)], now=0.0).payload, roc.mmsi, 0.0)
good_linkage = mass.route_linkage_id
check(mass.has_route, "정상 항로를 받았다")

# waypoint가 없는 지시
empty = route.RouteInstruction(linkage_id=99, route_type=route.ROUTE_TYPE_MANDATORY)
replies = mass.receive(codec.encode(empty), roc.mmsi, 1.0)
answer = codec.decode(replies[0].payload)
check(answer.status == ack.STATUS_INCOMPLETE, "waypoint 없는 지시를 거부한다")
check(answer.reason == ack.REASON_ROUTE_UNSUITABLE, "사유가 route unsuitable")
check(mass.route_linkage_id == good_linkage,
      "거부된 지시는 기존 항로를 바꾸지 않는다", "검증 -> 상태 변경 순서")

# Route Type이 not available인 지시
bad_type = route.RouteInstruction(
    waypoints=[route.waypoint_from_degrees(129.1, 35.1)],
    linkage_id=98,
    route_type=route.ROUTE_TYPE_NOT_AVAILABLE,
)
replies = mass.receive(codec.encode(bad_type), roc.mmsi, 2.0)
check(codec.decode(replies[0].payload).status == ack.STATUS_INCOMPLETE,
      "Route Type 미지정을 거부한다")
check(mass.route_linkage_id == good_linkage, "역시 상태가 유지된다")
check(mass.rejected_count == 2, "거부 횟수를 센다", f"{mass.rejected_count}건")

# 우리 응용이 아닌 payload
before_ignored = mass.ignored_count
check(mass.receive(b"\xde\xad\xbe\xef", roc.mmsi, 3.0) == [],
      "남의 payload는 조용히 무시한다")
check(mass.ignored_count == before_ignored + 1, "무시 횟수를 센다")
check(mass.route_linkage_id == good_linkage, "무시된 payload도 상태를 바꾸지 않는다")

section("MassAgent — 항로 취소")

cancel = roc.cancel_route(mass.mmsi, good_linkage, now=4.0)
replies = mass.receive(cancel.payload, roc.mmsi, 4.0)
check(codec.decode(replies[0].payload).status == ack.STATUS_ACKNOWLEDGED,
      "취소에 completed로 답한다")
check(not mass.has_route, "항로가 해제된다")

section("MassAgent — 유효기간 경과")

expiring = MassAgent(state=ShipState(latitude=35.0, longitude=129.0, sog=6.0))
expiring_roc = RocAgent()
# duration은 분 단위. 1분짜리 항로를 준다.
mass_command = expiring_roc.issue_route(
    expiring.mmsi, [(35.5, 129.0)], now=0.0, duration=1
)
expiring.receive(mass_command.payload, expiring_roc.mmsi, 0.0)
check(expiring.has_route, "유효기간이 있는 항로를 받았다")
expiring.tick(0.0)
expiring.tick(30.0)
check(expiring.has_route, "30초 뒤에는 아직 유효하다")
expiring.tick(61.0)
check(not expiring.has_route, "60초가 지나면 항로가 만료된다")

# --------------------------------------------------------- ROC 동작

section("RocAgent — 항적 추적")

mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0, heading=90.0, sog=12.0),
                 report_interval=2.0)
roc = RocAgent(report_timeout=10.0)
run(mass, roc, seconds=20.0)

track = roc.tracks[mass.mmsi]
check(track.report_count > 1, "위치보고를 누적한다", f"{track.report_count}건")
check(track.source_id == mass.mmsi, "송신원으로 구분한다")
check(roc.link_lost == [], "보고가 오는 동안은 두절이 아니다")

section("RocAgent — 통신 두절 판정 (별도 heartbeat 없음)")

# MASS를 멈추고 ROC 시간만 흘린다
for t in range(21, 40):
    roc.tick(float(t))
check(roc.link_lost == [mass.mmsi], "보고가 끊기면 두절로 판정한다",
      f"timeout {roc.report_timeout:g}s")

# 보고가 재개되면 복구된다
mass.tick(40.0)
outbound = mass.tick(42.0)[0]
roc.receive(outbound.payload, mass.mmsi, 42.0)
check(roc.link_lost == [], "보고가 재개되면 두절이 풀린다")

section("RocAgent — Linkage ID 관리")

roc = RocAgent()
first = codec.decode(roc.issue_route(1, [(35.1, 129.1)], now=0.0).payload).linkage_id
second = codec.decode(roc.issue_route(1, [(35.2, 129.2)], now=0.0).payload).linkage_id
check(first != second, "명령마다 다른 Linkage ID를 쓴다", f"{first}, {second}")
check(1 <= first <= 1023 and 1 <= second <= 1023,
      "Linkage ID가 표준 범위(1..1023)에 있다")
check(len(roc.unanswered) == 2, "응답 없는 명령을 센다")

# 우리가 보내지 않은 Linkage ID에 대한 응답은 무시
before = roc.ignored_count
roc.receive(codec.encode(ack.ApplicationAck(linkage_id=777)), 1, 0.0)
check(roc.ignored_count == before + 1, "모르는 Linkage ID의 ACK를 무시한다")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
