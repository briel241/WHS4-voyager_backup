#!/usr/bin/env python3
"""SC-VDE-005 노선 · SC-VDE-006 원격 조종 — VDETER 소프트웨어 무선 loopback 시연.

노선지시(Message ID 2)와 조종 데이터 payload가 **실제 VDETER 스택 전체**를 지나
반대편에 byte 그대로 도착하고, 우리 압축 코덱이 그것을 풀어 필드를 복원하는 것을
보인다.

⚠ 운영 포맷이 아니라 **시험 전용 `test.stage0.opaque`(통과 포맷)** 로 나른다.
   노선/조종용 운영 payload_format은 아직 VDETER registry에 없어(링크 팀 등록 대기),
   지금 종단 시연이 가능한 경로는 opaque뿐이다. 인증서(SC-VDE-001)와 달리 이 두
   시나리오의 "운영 종단"은 registry 포맷이 등록돼야 완성된다.

전제
    - in-repo `VDETER/`(src + tests/support 하네스) 필요.
    - 소프트웨어 그룹 조립이 `cryptography`를 요구한다(TBB 서명자). 없으면 건너뛴다.

실행
    python3 apps/route_loopback_demo.py
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import unittest

    from tests.support.stage5 import (
        MASS_A_STATION_ID,
        ROC_STATION_ID,
        build_stage5_software_group,
        drive_stage5_group,
        received_payloads,
        start_stage5_group,
    )
    from vdeter.common.addressing import Addressed
    from vdeter.common.contracts.service import (
        SubmitTransferRequest,
        TransferState,
        TransferStatusRequest,
    )
    from vdeter.common.identifiers import PayloadFormat
except ImportError as exc:
    print(f"SKIP: VDETER 소프트웨어 무선 하네스를 불러올 수 없습니다 ({exc}).")
    sys.exit(0)

from common import codec
from common.messages import route_instruction as route

OPAQUE = "test.stage0.opaque"   # ⚠ 시험 전용 통과 포맷
MAX_DRIVE_STEPS = 800


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
        heading_setpoint=round(92.5 * route.HEADING_SETPOINT_SCALE),
        fallback_state=route.FALLBACK_ANCHORING,
    )


def _send_opaque(payload: bytes):
    """ROC→MASS로 payload를 opaque 포맷으로 보내고 (전송상태, 수신 payload) 반환.

    전송마다 그룹을 격리한다(ACK 전송 연속 시 상태 얽힘 회피).
    """
    clock, group = build_stage5_software_group()
    start_stage5_group(clock, group)
    try:
        by_station = group.by_station()
        roc = by_station[ROC_STATION_ID.value]
        mass = by_station[MASS_A_STATION_ID.value]

        request = SubmitTransferRequest(
            destination=Addressed(MASS_A_STATION_ID),
            payload_format=PayloadFormat(OPAQUE),
            payload=payload,
            ack_required=True,
        )
        before = len(received_payloads(mass))
        accepted = roc.submit(request)
        if not accepted.is_ok:
            raise RuntimeError(f"submit 거부: {getattr(accepted, 'error', accepted)}")

        state = None
        for _ in range(MAX_DRIVE_STEPS):
            drive_stage5_group(clock, group, 1)
            state = roc.status(TransferStatusRequest(accepted.value.transfer_id)).value.state
            if state in (TransferState.COMPLETED, TransferState.FAILED,
                         TransferState.CANCELLED):
                break
        received = received_payloads(mass)[before:]
        return state, received
    finally:
        group.stop()


def _run_case(title, message, checks):
    payload = codec.encode(message)
    state, received = _send_opaque(payload)
    print(f"[{title}] 압축 payload {len(payload)} byte 제출")
    print(f"  전송 상태     : {state.value}")
    byte_equal = bool(received) and received[0] == payload
    print(f"  수신 byte 일치: {byte_equal}")
    assert state is TransferState.COMPLETED, f"{title}: COMPLETED 아님 ({state})"
    assert byte_equal, f"{title}: 수신 byte 불일치"

    decoded = codec.decode(received[0])
    for label, actual, expected in checks(decoded):
        mark = "OK" if actual == expected else "FAIL"
        print(f"  {label}: {actual} (기대 {expected}) [{mark}]")
        assert actual == expected, f"{title}: {label} 불일치"
    print()


def main() -> int:
    try:
        # 조립 가능 여부(=cryptography)만 미리 확인
        _clock, _group = build_stage5_software_group()
        _group.stop()
    except unittest.SkipTest as exc:
        print(f"SKIP: 소프트웨어 그룹 조립에 cryptography가 필요합니다 ({exc}).")
        return 0

    print("=== VDETER 소프트웨어 무선 loopback — 노선/원격조종 (⚠ 시험용 opaque 포맷) ===\n")

    _run_case(
        "SC-VDE-005 노선", _make_route(),
        lambda d: [
            ("waypoint 수", len(d.waypoints), 2),
            ("WP1 계획SOG", d.waypoints[0].planned_sog_knots, 12.5),
            ("WP2 leg기하", d.waypoints[1].leg_geometry_name, "Orthodrome"),
        ],
    )
    _run_case(
        "SC-VDE-006 원격조종", _make_control(),
        lambda d: [
            ("heading setpoint", d.heading_setpoint_degrees, 92.5),
            ("fallback", d.fallback_name, "Anchoring"),
            ("WP1 계획SOG", d.waypoints[0].planned_sog_knots, 6.0),
        ],
    )

    print("PASS: 노선·원격조종 payload가 실 VDETER 스택을 종단 왕복하고 필드까지 복원")
    print("주의: 운영 종단은 노선용 payload_format이 VDETER registry에 등록돼야 완성됨(링크 팀).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
