#!/usr/bin/env python3
"""SC-VDE-006 MASS-ROC 원격 조종 — VDETER 소프트웨어 무선 loopback 시연.

노선(S-421)이 교환된 상황을 전제로, ROC의 조종 데이터(heading setpoint·fallback·
속도 조정)를 노선지시(Message ID 2) 확장으로 실어 보낸다. payload가 **실제 VDETER
스택 전체**를 지나 반대편에 byte 그대로 도착하고, 압축 코덱을 왕복해도 heading·
fallback·SOG가 보존되는지 본다.

조종용 SC-VDE format(`sc_vde.route_control.command`, TEST scope)이 VDETER registry에
등록되어 있어 그 포맷으로 나른다. format이 없는 옛 빌드에서만 시험용
`test.stage0.opaque`로 자동 전환한다. SC-VDE는 서명·replay 보호가 없는 TEST 전용이다.

전제: in-repo `VDETER/` + `cryptography`(소프트웨어 그룹 조립에 필요). 없으면 SKIP.
실행: python3 apps/control_loopback_demo.py
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


def _make_control():
    """SC-VDE-006 조종 데이터: heading setpoint + fallback + 속도 조정."""
    waypoints = [
        route.waypoint_from_degrees(129.05, 35.10, turn_radius_nm=0.3,
                                    planned_sog_knots=6.0,
                                    leg_geometry=route.LEG_GEOMETRY_LEXODROME),
        route.waypoint_from_degrees(129.10, 35.133, turn_radius_nm=0.3,
                                    planned_sog_knots=4.0,
                                    leg_geometry=route.LEG_GEOMETRY_LEXODROME),
    ]
    return route.RouteInstruction(
        waypoints=waypoints,
        heading_setpoint=route.heading_setpoint_from_degrees(92.5),
        fallback_state=route.FALLBACK_ANCHORING,
    )


def main() -> int:
    if not preflight():
        return 0

    print("=== VDETER 소프트웨어 무선 loopback — SC-VDE-006 원격 조종 ===\n")
    run_scenario(
        "SC-VDE-006 원격조종", _make_control(),
        lambda d: [
            ("heading setpoint", d.heading_setpoint_degrees, 92.5),
            ("fallback", d.fallback_name, "Anchoring"),
            ("WP1 계획SOG", d.waypoints[0].planned_sog_knots, 6.0),
        ],
    )
    print("PASS: 원격조종 payload가 실 VDETER 스택을 종단 왕복하고 heading·fallback·SOG 복원")
    print("주의: SC-VDE는 서명·replay 보호가 없는 TEST 전용이다. 실제 조타에 쓰지 않는다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
