#!/usr/bin/env python3
"""문서가 주장하는 사실이 구현과 맞는지 대조한다.

    python3 tools/check_docs.py

프로젝트가 커지면서 코드를 고칠 때마다 문서 여러 곳이 함께 낡는다. 눈으로
훑는 대신 여기서 기계적으로 잡는다. 실패하면 **문서와 코드 중 어느 쪽이
틀렸는지** 판단해서 고친다.

검사하는 것
    1. 상수 — 문서에 적힌 수치가 config·모듈 상수와 같은가
    2. payload_format — 이름·VPFI·Message Format이 registry와 같은가
    3. 크기 — 문서에 적힌 byte 수를 실제로 인코딩해서 맞는가
    4. 참조 — 문서가 가리키는 파일·명령이 실재하는가
    5. 낡은 표현 — 이미 해결된 상태를 여전히 미해결로 적고 있지 않은가
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from common.textout import ensure_utf8_output

ensure_utf8_output()

failures: list[str] = []
checked = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if ok:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 1. 상수

print("[상수]")
from common import config  # noqa: E402
from MASS.mass_agent import DEFAULT_MAX_TURN_RATE, MassAgent  # noqa: E402

CONSTANTS = [
    ("제어 링크 두절 30초", config.CONTROL_LINK_TIMEOUT_SEC, 30.0),
    ("ROC 두절 판정 10초", config.POSITION_REPORT_TIMEOUT_SEC, 10.0),
    ("선회율 상한 20도/분", DEFAULT_MAX_TURN_RATE, 20.0),
    ("12노트 보고주기 10초", config.POSITION_REPORT_INTERVAL_SLOW, 10.0),
    ("정박 보고주기 180초", config.POSITION_REPORT_INTERVAL_ANCHORED, 180.0),
    ("기본 fallback = DP(2)", config.DEFAULT_LINK_LOSS_FALLBACK, 2),
    ("MASS MMSI", config.MASS_MMSI, 440_000_001),
    ("ROC MMSI", config.ROC_MMSI, 440_000_002),
]
for label, actual, expected in CONSTANTS:
    check(label, actual == expected, f"실제 {actual} / 문서 {expected}")


# ------------------------------------------------------- 2. payload_format

print("\n[payload_format registry]")
try:
    sys.path.insert(0, os.path.join(ROOT, "VDETER", "src"))
    from vdeter.profiles.models import RegistryScope
    from vdeter.profiles.payload_formats import entries_for

    reg = {
        e.payload_format.value: (e.vpfi.value, e.message_format)
        for e in entries_for(RegistryScope.TEST)
    }
    EXPECTED_FORMATS = [
        ("x509.certificate_report", 0, 1),
        ("sc_vde.position_report", 16383, 1),
        ("sc_vde.route_control.command", 16383, 2),
        ("sc_vde.route_control.ack", 16383, 3),
    ]
    for key, vpfi, mf in EXPECTED_FORMATS:
        got = reg.get(key)
        check(f"{key} VPFI {vpfi} MF {mf}", got == (vpfi, mf), f"실제 {got}")

    operational = {e.payload_format.value for e in entries_for(RegistryScope.OPERATIONAL)}
    check("인증서는 OPERATIONAL scope", "x509.certificate_report" in operational)
    check(
        "sc_vde는 OPERATIONAL에 없음(TEST 전용)",
        not any(k.startswith("sc_vde") for k in operational),
    )
except ImportError as exc:
    check("VDETER registry 조회", False, f"불러올 수 없음: {exc}")


# ------------------------------------------------------------------ 3. 크기

print("\n[메시지 크기]")
from common import codec  # noqa: E402
from common.messages import application_ack as ack  # noqa: E402
from common.messages import position_report as pos  # noqa: E402
from common.messages import route_instruction as route  # noqa: E402


def route_msg(n: int):
    return route.RouteInstruction(
        waypoints=[
            route.waypoint_from_degrees(129.0 + i * 0.05, 35.0 + i * 0.05)
            for i in range(n)
        ],
        linkage_id=1,
        sender_classification=route.SENDER_AUTHORITY,
        route_type=route.ROUTE_TYPE_MANDATORY,
    )


SIZES = [
    ("위치보고 application 14 B", len(codec.encode_body(pos.from_measurements())), 14),
    ("조종 단독(WP 0) 10 B", len(codec.encode_body(route_msg(0))), 10),
    ("노선 WP 2개 29 B", len(codec.encode_body(route_msg(2))), 29),
    ("노선 WP 3개 39 B", len(codec.encode_body(route_msg(3))), 39),
    ("노선 WP 16개 164 B", len(codec.encode_body(route_msg(16))), 164),
    (
        "응용 ACK 3 B",
        len(codec.encode_body(ack.ApplicationAck(linkage_id=1, status=4, reason=0))),
        3,
    ),
]
for label, actual, expected in SIZES:
    check(label, actual == expected, f"실제 {actual} B")


# ------------------------------------------------------------ 4. 상태 정의

print("\n[상태 정의]")
check(
    "Autonomy Mode 1=RC 2=MON 3=AUT",
    (pos.AUTONOMY_RC, pos.AUTONOMY_MON, pos.AUTONOMY_AUT) == (1, 2, 3),
)
check(
    "Fallback 1=Anchoring 2=DP 3=Drifting",
    (
        route.FALLBACK_ANCHORING,
        route.FALLBACK_DYNAMIC_POSITIONING,
        route.FALLBACK_DRIFTING,
    )
    == (1, 2, 3),
)
check(
    "Fallback→NavStatus 대응 {1:1, 2:0, 3:2}",
    MassAgent._FALLBACK_NAV_STATUS == {1: 1, 2: 0, 3: 2},
    f"실제 {MassAgent._FALLBACK_NAV_STATUS}",
)


# -------------------------------------------------------------- 5. 참조·명령

print("\n[문서가 가리키는 파일]")
REFERENCED = [
    "apps/scenario_runner.py",
    "apps/console_server.py",
    "apps/console_transport.py",
    "apps/console_static/roc.html",
    "apps/console_static/mass.html",
    "apps/cert_loopback_demo.py",
    "apps/route_loopback_demo.py",
    "apps/control_loopback_demo.py",
    "apps/rf_normal_cycle.py",
    "tests/test_sc_vde_contract.py",
    "tools/build_scenario_catalog.py",
    "docs/사용법.md",
    "docs/인수인계.md",
    "docs/시나리오_카탈로그_확정3종.xlsx",
    "docs/scenarios/SC-VDE-001-x509-certificate.md",
    "docs/scenarios/SC-VDE-005-route-guidance.md",
    "docs/scenarios/SC-VDE-006-remote-control.md",
    "docs/archive/README.md",
    "docs/vdeter_interim_report_technical_summary.md",
]
for path in REFERENCED:
    check(path, os.path.exists(os.path.join(ROOT, path)))

# 마크다운 상대 링크가 실재하는가
print("\n[마크다운 링크]")
broken = []
for base, _dirs, files in os.walk(os.path.join(ROOT, "docs")):
    for name in files:
        if not name.endswith(".md"):
            continue
        path = os.path.join(base, name)
        with open(path, encoding="utf-8") as f:
            body = f.read()
        for target in re.findall(r"\]\((?!https?:)([^)#]+)", body):
            resolved = os.path.normpath(os.path.join(base, target))
            if not os.path.exists(resolved):
                broken.append(f"{os.path.relpath(path, ROOT)} → {target}")
check(f"상대 링크 {len(broken)}건 깨짐", not broken, "; ".join(broken[:5]))


# ---------------------------------------------------------- 6. 낡은 표현

print("\n[낡은 표현]")
STALE = [
    ("시험용 opaque로만", "노선·조종은 이제 등록 포맷을 쓴다"),
    ("data_type 미배정", "배정 완료"),
    ("payload_format이 VDETER registry에 없", "등록 완료"),
    ("2초마다 위치를 브로드캐스트", "AIS 표준 주기 + addressed"),
]
docs_text = {}
for base, _dirs, files in os.walk(os.path.join(ROOT, "docs")):
    if "archive" in base:
        continue  # 보관 문서는 당시 기록이므로 검사하지 않는다
    for name in files:
        if name.endswith(".md"):
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as f:
                docs_text[os.path.relpath(path, ROOT)] = f.read()

for phrase, why in STALE:
    hits = [p for p, body in docs_text.items() if phrase in body]
    check(f"'{phrase}' 없음 ({why})", not hits, ", ".join(hits))


# --------------------------------------------------------------------- 결과

print()
if failures:
    print(f"실패 {len(failures)} / 검사 {checked}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"PASS: 문서와 구현이 일치한다 (검사 {checked}건)")
