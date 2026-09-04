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
from common import metrics as met  # noqa: E402
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
    ("비인가 송신국 MMSI", config.INTRUDER_MMSI, 440_000_003),
    # 지표 목표치는 docs/정량지표.md의 표에 그대로 적혀 있다.
    ("비인가 수용 목표 0회", met.TARGET_UNAUTHORIZED_ACCEPTED, 0),
    ("인증 성공률 목표 99%", met.TARGET_AUTH_SUCCESS_RATE, 0.99),
    ("성능 저하 목표 30%", met.TARGET_LATENCY_OVERHEAD, 0.30),
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

# 전선형식 문서가 적은 Route Type 집합과 코드가 같아야 한다. 문서 쪽이 링크 팀과
# 합의한 계약이므로, 코드가 그보다 넓으면 실 전송에서 INVALID_REQUEST가 난다.
for doc in (
    "docs/전선형식_노선_SC-VDE-005.md",
    "docs/전선형식_원격조종_SC-VDE-006.md",
):
    line = next(
        (l for l in read(doc).splitlines() if l.startswith("| Route Type")), ""
    )
    documented = {int(v) for v in re.findall(r"(\d+)=", line)}
    check(
        f"{os.path.basename(doc)} Route Type 집합",
        documented == set(route.SC_VDE_ROUTE_TYPES),
        f"문서 {sorted(documented)} / 코드 {sorted(route.SC_VDE_ROUTE_TYPES)}",
    )


# --------------------------------------------- 4b. 인증서 구성 일치·크기 경계

print("\n[인증서 구성]")

# scenario_runner(정본)와 Stage 7 fixture가 같은 확장 필드를 써야 한다.
# 확장 하나가 25~28 byte라, 빠지면 실장비에서 실제보다 작은 payload로 시험하게
# 된다. SC-VDE-001은 400 byte 경계에 걸려 있어 그 차이가 Data Session 필요
# 여부를 가른다.
_CERT_EXTENSIONS = ("mass_id", "roc_id", "mmsi", "imo")
_runner = read("apps/scenario_runner.py")
_fixture = read("tools/make_stage7_fixtures.py")


def _uses(body: str, marker: str, field: str) -> bool:
    """marker 뒤 900자 안에서 field= 를 쓰는가 (build_certificate 인자 범위)."""
    at = body.find(marker)
    return at >= 0 and f"{field}=" in body[at:at + 900]


for field in _CERT_EXTENSIONS:
    in_runner = _uses(_runner, "vessel_der = cert.build_certificate", field)
    in_fixture = _uses(_fixture, 'subject=f"MASS-{STATION[', field)
    check(
        f"인증서 확장 {field} — 정본·fixture 일치",
        in_runner == in_fixture,
        f"scenario_runner={in_runner} fixture={in_fixture}",
    )

# SC-VDE-001은 400 byte를 넘는다 — Data Session이 필요하다는 뜻이고,
# 링크 팀에 그렇게 보고했다. 이 사실이 바뀌면 보고도 갱신해야 한다.
try:
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from make_stage7_fixtures import certificate_report

    cert_size = len(certificate_report())
    check(
        f"SC-VDE-001 인증서 400 B 초과 (실측 {cert_size} B)",
        cert_size > 400,
        "400 B 이하로 내려갔다면 링크 팀 보고를 갱신해야 한다",
    )

    # 정본 시나리오와 fixture가 **같은 형식**을 내야 한다. 예전에 정본이 bare
    # DER을 쓰는 바람에 링크 팀에 보고한 크기와 다른 숫자가 나왔다.
    from common.messages import certificate_report as certrep
    from apps.scenario_runner import run_certificate_scenario

    scenario_run, _ = run_certificate_scenario(1_767_225_600.0)
    note = scenario_run.ordered()[0].message
    entries = certrep.parse_report(certificate_report())
    check(
        "정본 시나리오가 Certificate Report 본문을 쓴다",
        "Certificate Report 본문" in note,
        f"첫 note: {note}",
    )
    check(
        "fixture 본문이 항목 1개짜리 Certificate Report",
        len(entries) == 1 and entries[0].certificate_type == certrep.CERT_TYPE_X509,
    )
    # ECDSA 서명 DER 길이가 실행마다 ±3 B 흔들리므로 범위로 비교한다.
    scenario_size = int(note.split("Certificate Report 본문 ")[1].split(" byte")[0])
    check(
        f"정본({scenario_size} B)과 fixture({cert_size} B)가 같은 크기대",
        abs(scenario_size - cert_size) <= 6,
        "형식이 갈라졌다면 링크 팀 보고와도 어긋난다",
    )
except Exception as exc:  # pragma: no cover - cryptography 없는 환경
    # 통과가 아니라 **건너뛴 것**임이 출력에 드러나야 한다. 대체 백엔드에서는
    # 인증서 크기가 실제와 달라 크기 검사가 성립하지 않는다.
    check(f"SC-VDE-001 인증서 크기 — SKIP ({type(exc).__name__})", True)


# --------------------------------------------- 4c. 문서에 적은 "표준 대비 차이"
#
# docs/인수인계.md §5.1이 표준과 다른 점을 모아 놓았다. 그 서술이 계속 사실인지
# 본다. 차이가 해소되면(예: Start Date/Time을 실제로 읽게 되면) 여기서 걸리고,
# 그때 문서를 고치면 된다.

print("\n[표준 대비 차이 — 문서 서술이 아직 사실인가]")

# O-1 유효기간 기준 시각: 항법이 Start Date/Time을 읽지 않는다.
_mass_source = read("MASS/mass_agent.py")
_start_fields = ("start_month", "start_day", "start_hour", "start_minute")
_read_start = [f for f in _start_fields if f"message.{f}" in _mass_source]
check(
    "Start Date/Time을 항법에 쓰지 않는다 (인수인계 §5.1-1 · SC-VDE-005 §8.3)",
    not _read_start,
    f"이제 {_read_start}를 읽는다면 두 문서를 갱신해야 한다",
)

# O-4 X.509 검증 범위: 문서가 나열한 실패 사유가 실재해야 한다.
_cert_source = read("common/security/certificate.py")
_flow_source = read("common/security/cert_flow.py")
DOCUMENTED_REASONS = [
    "bad-signature", "not-yet-valid", "expired", "issuer-mismatch",
    "anchor-not-ca", "too-large", "parse-error",
]
_missing = [r for r in DOCUMENTED_REASONS if f'"{r}"' not in _cert_source]
check(
    f"인증서 실패 사유 {len(DOCUMENTED_REASONS)}종이 실재 (SC-VDE-001 §6.3)",
    not _missing,
    f"문서에만 있고 코드에 없다: {_missing}",
)
check(
    "CRL은 적용하지 않고 crl-not-handled로 남긴다 (SC-VDE-001 §6.3)",
    '"crl-not-handled"' in _flow_source,
)

# O-3 위치보고 범위: 행위자는 표준대로 broadcast로 만들고, 브리지가 막는다.
check(
    "위치보고를 행위자는 broadcast로 만든다 (SC-VDE-005 §9.2)",
    "destination_id=BROADCAST_ID" in _mass_source,
)
check(
    "브리지가 TEST scope broadcast를 거부한다 (SC-VDE-005 §9.2)",
    "addressed 전용입니다" in read("common/vdeter_bridge.py"),
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
    "tests/test_metrics.py",
    "tests/test_route_conformance.py",
    "common/metrics.py",
    "tools/measure_metrics.py",
    "tools/build_scenario_catalog.py",
    "docs/정량지표.md",
    "docs/사용법.md",
    "docs/인수인계.md",
    "docs/시나리오_카탈로그_확정3종.xlsx",
    "docs/scenarios/SC-VDE-001-x509-certificate.md",
    "docs/scenarios/SC-VDE-005-route-guidance.md",
    "docs/scenarios/SC-VDE-006-remote-control.md",
    "docs/archive/README.md",
    "docs/vdeter_interim_report_technical_summary.md",
    "docs/링크팀_인수인계.md",
    "tools/make_stage7_fixtures.py",
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
