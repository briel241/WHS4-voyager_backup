#!/usr/bin/env python3
"""SC-VDE-005 노선 계획 교환 — VDETER 소프트웨어 무선 loopback 시연.

노선지시(Message ID 2) payload가 **실제 VDETER 스택 전체**를 지나 반대편에 byte
그대로 도착하고, 우리 압축 코덱이 풀어 waypoint·계획속도·leg 기하를 복원하는 것을 본다.

노선용 SC-VDE format(`sc_vde.route_control.command`, TEST scope)이 VDETER registry에
등록되어 있어 그 포맷으로 나른다. format이 없는 옛 빌드에서만 시험용
`test.stage0.opaque`로 자동 전환한다. SC-VDE는 서명·replay 보호가 없는 TEST 전용이다.

전제: in-repo `VDETER/` + `cryptography`(소프트웨어 그룹 조립에 필요). 없으면 SKIP.
실행: python3 apps/route_loopback_demo.py
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps._loopback_opaque import preflight, run_scenario
from common.messages import route_instruction as route


def _make_route():
    """SC-VDE-005 순수 노선 계획: waypoint + 계획 SOG + leg 기하 (조종 필드 없음)."""
    waypoints = [
        route.waypoint_from_degrees(129.076, 35.102, turn_radius_nm=0.5,
                                    planned_sog_knots=12.5,
                                    leg_geometry=route.LEG_GEOMETRY_LEXODROME),
        route.waypoint_from_degrees(129.333, 35.208, turn_radius_nm=1.2,
                                    planned_sog_knots=8.0,
                                    leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
    ]
    return route.RouteInstruction(waypoints=waypoints)


def main() -> int:
    if not preflight():
        return 0

    print("=== VDETER 소프트웨어 무선 loopback — SC-VDE-005 노선 ===\n")
    run_scenario(
        "SC-VDE-005 노선", _make_route(),
        lambda d: [
            ("waypoint 수", len(d.waypoints), 2),
            ("WP1 계획SOG", d.waypoints[0].planned_sog_knots, 12.5),
            ("WP2 leg기하", d.waypoints[1].leg_geometry_name, "Orthodrome"),
        ],
    )
    print("PASS: 노선 payload가 실 VDETER 스택을 종단 왕복하고 필드까지 복원")
    print("주의: SC-VDE는 서명·replay 보호가 없는 TEST 전용이다. 실제 조타에 쓰지 않는다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
