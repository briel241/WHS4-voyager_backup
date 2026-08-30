#!/usr/bin/env python3
"""확정 3개 시나리오를 Stage 7 fixture로 만든다.

    python3 tools/make_stage7_fixtures.py --out results/stage7-fixtures

Stage 7 Scenario Controller는 `--fixtures-dir`의 JSON을 읽어 endpoint에게
`VdeService.submit()`을 시킨다. 그 JSON 형식은 다음 네 필드다
(`VDETER/tools/prepare_stage7_run_configs.py`).

    {"destination": <station id>, "payload_format": "<key>",
     "payload_base64": "<...>", "ack_required": <bool>}

현재 링크 팀 생성기는 **크기와 전송 유형만 재현**한다 — payload_format이
전부 `test.stage0.opaque`이고 내용은 합성 바이트다. 전송 특성(단편화·ACK·
방향) 검증에는 충분하지만, 우리 **실제 메시지가 실장비를 통과하는지**는
확인되지 않는다.

이 스크립트는 같은 형식에 **우리 실제 payload와 등록된 payload_format**을
넣어 낸다. 실장비 실행 때 이 fixture를 쓰면 SC-VDE-001/005/006이 그대로
B210을 통과한다.

주의
    Stage 7 station id는 우리 MMSI와 다르다 (MASS-A 111111111,
    MASS-B 333333333, ROC 222222222). 아래 STATION이 그 값이다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from common import codec  # noqa: E402
from common import vdeter_bridge as bridge  # noqa: E402
from common.messages import application_ack as ack  # noqa: E402
from common.messages import certificate_report as certrep  # noqa: E402
from common.messages import position_report as pos  # noqa: E402
from common.messages import route_instruction as route  # noqa: E402

#: Stage 7 endpoint의 station id (`stage7_scenario_controller.py`)
STATION = {"MASS-A": 111_111_111, "MASS-B": 333_333_333, "ROC": 222_222_222}


def route_command() -> bytes:
    """SC-VDE-005 노선 — waypoint 2개."""
    return codec.encode_body(
        route.RouteInstruction(
            waypoints=[
                route.waypoint_from_degrees(
                    129.076, 35.102, turn_radius_nm=0.5, planned_sog_knots=12.5,
                    leg_geometry=route.LEG_GEOMETRY_LEXODROME),
                route.waypoint_from_degrees(
                    129.333, 35.208, turn_radius_nm=1.2, planned_sog_knots=8.0,
                    leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
            ],
            linkage_id=7,
            sender_classification=route.SENDER_AUTHORITY,
            route_type=route.ROUTE_TYPE_MANDATORY,
        )
    )


def control_command() -> bytes:
    """SC-VDE-006 원격 조종 — 침로 92.5°, 투묘. waypoint 없이 성립한다."""
    return codec.encode_body(
        route.RouteInstruction(
            linkage_id=8,
            sender_classification=route.SENDER_AUTHORITY,
            route_type=route.ROUTE_TYPE_RECOMMENDED,
            heading_setpoint=route.heading_setpoint_from_degrees(92.5),
            fallback_state=route.FALLBACK_ANCHORING,
        )
    )


def application_ack() -> bytes:
    """MASS→ROC 응용 ACK — acknowledged."""
    return codec.encode_body(
        ack.ApplicationAck(
            linkage_id=7, status=ack.STATUS_ACKNOWLEDGED, reason=ack.REASON_NONE
        )
    )


def position_report() -> bytes:
    """위치보고 — 부산 앞바다, 12.5노트."""
    return codec.encode_body(
        pos.from_measurements(
            longitude_degrees=129.076, latitude_degrees=35.102,
            sog_knots=12.5, cog_degrees=47.2, heading_degrees=47,
            navigation_status=0, rate_of_turn=0, timestamp=30,
            autonomy_mode=pos.AUTONOMY_MON,
        )
    )


def certificate_report() -> bytes:
    """SC-VDE-001 인증서 — X.509 v3 DER 한 건을 담은 Certificate Report 본문.

    **`apps/scenario_runner.py`의 SC-VDE-001과 같은 구성**이어야 한다. 확장 필드
    하나가 25~28 byte라, 빼먹으면 실장비에서 실제보다 작은 payload로 시험하게 된다.
    이 시나리오는 400 byte 경계에 걸려 있어(≈413 B) 그 차이가 Data Session 필요
    여부를 가른다.

        ROC-CA가 발급한 **선박 인증서** · 확장 mass_id · roc_id · mmsi · imo
    """
    from common.security import certificate as cert
    from common.security import default_backend, fingerprint_for_public_key

    backend = default_backend()
    day = 86_400.0
    now = 1_767_225_600.0  # 2026-01-01 UTC 고정 — fixture를 재현 가능하게

    # ROC의 CA
    ca_key, ca_public = cert.generate_key_pair()
    cert.build_certificate(
        subject="ROC-CA", issuer="ROC-CA",
        subject_public_key=ca_public, issuer_private_key=ca_key,
        serial_number=1, not_before=now - day, not_after=now + 365 * day,
        roc_id="ROC-01", is_ca=True)

    # CA가 발급하는 선박 인증서 (scenario_runner와 같은 확장 구성)
    _, vessel_public = cert.generate_key_pair()
    der = cert.build_certificate(
        subject=f"MASS-{STATION['MASS-A']}", issuer="ROC-CA",
        subject_public_key=vessel_public, issuer_private_key=ca_key,
        serial_number=2, not_before=now - day, not_after=now + 30 * day,
        mass_id="MASS-01", roc_id="ROC-01",
        mmsi=str(STATION["MASS-A"]), imo="9074729")

    fingerprint = fingerprint_for_public_key(backend.public_bytes(vessel_public))
    return certrep.build_single(der, fingerprint)


#: (이름, 송신, 수신, payload 만드는 함수, payload_format, ack_required, 시나리오)
CASES = [
    ("sc-vde-005-route", "ROC", "MASS-A", route_command,
     bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND, True, "SC-VDE-005 노선"),
    ("sc-vde-006-control", "ROC", "MASS-A", control_command,
     bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_COMMAND, True, "SC-VDE-006 원격 조종"),
    ("sc-vde-006-ack", "MASS-A", "ROC", application_ack,
     bridge.PAYLOAD_FORMAT_ROUTE_CONTROL_ACK, False, "응용 ACK"),
    ("sc-vde-position", "MASS-A", "ROC", position_report,
     bridge.PAYLOAD_FORMAT_POSITION_REPORT, False, "위치보고"),
    ("sc-vde-001-certificate", "ROC", "MASS-A", certificate_report,
     bridge.PAYLOAD_FORMAT_CERTIFICATE_REPORT, True, "SC-VDE-001 인증서"),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="results/stage7-fixtures")
    arguments = parser.parse_args(argv)

    out_dir = os.path.join(ROOT, arguments.out)
    os.makedirs(out_dir, exist_ok=True)

    print(f"{'fixture':26} {'크기':>6}  {'payload_format':32} ACK  시나리오")
    print("-" * 92)
    for name, sender, receiver, build, fmt, ack_required, label in CASES:
        payload = build()

        # SC-VDE는 길이·addressed 규칙이 있다. 내보내기 전에 우리 쪽에서 검사한다.
        bridge.check_sc_vde_payload(fmt, payload)

        fixture = {
            "destination": STATION[receiver],
            "payload_format": fmt,
            "payload_base64": base64.b64encode(payload).decode("ascii"),
            "ack_required": ack_required,
        }
        path = os.path.join(out_dir, f"submit-{name}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(fixture, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")

        print(f"submit-{name+'.json':<19} {len(payload):>4} B  {fmt:32} "
              f"{'요구' if ack_required else '불요':4} {sender}→{receiver} {label}")

    print()
    print(f"생성 위치: {out_dir}")
    print()
    print("실장비 실행 시:")
    print("  python apps/stage7_scenario_controller.py --fixtures-dir <이 폴더> ...")
    print()
    print("주의 — 이 fixture는 payload만 정의한다. start-*.json과 stop.json은")
    print("      링크 팀 prepare_stage7_run_configs.py가 만드는 것을 함께 쓴다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
