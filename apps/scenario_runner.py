#!/usr/bin/env python3
"""시나리오 선택·재현 인터페이스 (작업 D, 선택 항목).

    python3 apps/scenario_runner.py list
    python3 apps/scenario_runner.py run SC-VDE-001
    python3 apps/scenario_runner.py run SC-VDE-001 --json

각 시나리오는 작업 A~C 에서 만든 함수를 호출하는 **얇은 어댑터**다. 실행은
현재 모의(in-process)다 — 링크(VDE-TER)를 타지 않는다. 통합 시
``SCENARIOS`` dict 의 실행 함수만 실제 ``RocAgent``/``MassAgent`` 호출로
갈아끼우면 되고, 로그·판정 계층은 그대로 재사용된다.

표준 라이브러리 ``argparse`` 만 사용한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.messages import route_json_bridge as bridge
from common.observe.scenario_analysis import analyze, to_json, to_text
from common.observe.scenario_log import ScenarioRun
from common.security import certificate as cert
from common.security.cert_flow import mass_receive_certificate, roc_issue_certificate
from common.security.trust_store import TrustStore

RESULTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

DAY = 86_400.0
MASS_ID = 42
ROC_ID = 1


# --------------------------------------------------------------------------
# SC-VDE-001 — X.509 인증서 송수신
# --------------------------------------------------------------------------

def run_certificate_scenario(now: float) -> tuple[ScenarioRun, dict]:
    """ROC 가 인증서를 발급·전송하고 MASS 가 검증·저장하는 흐름을 모의한다."""
    run = ScenarioRun(scenario_id="SC-VDE-001", started=now)

    # --- ROC: CA 와 단말 인증서 준비 -------------------------------------
    ca_key, ca_public = cert.generate_key_pair()
    ca_der = cert.build_certificate(
        subject="ROC-CA", issuer="ROC-CA",
        subject_public_key=ca_public, issuer_private_key=ca_key,
        serial_number=1, not_before=now - DAY, not_after=now + 365 * DAY,
        roc_id="ROC-01", is_ca=True)

    _, vessel_public = cert.generate_key_pair()
    vessel_der = cert.build_certificate(
        subject="MASS-440123456", issuer="ROC-CA",
        subject_public_key=vessel_public, issuer_private_key=ca_key,
        serial_number=2, not_before=now - DAY, not_after=now + 30 * DAY,
        mass_id="MASS-01", roc_id="ROC-01", mmsi="440123456", imo="9074729")

    backend_note = "표준 ECDSA P-256" if cert.is_standard_backend() \
        else "대체 HMAC 백엔드 — 실제 공개키 암호 아님"
    run.record(now, "ROC->MASS", "note",
               f"인증서 준비 완료 ({len(vessel_der)} byte, {backend_note})")

    outbound = roc_issue_certificate(vessel_der, mass_id=MASS_ID)

    # --- 링크 분할 모의 -----------------------------------------------------
    # 실제 분할·재조립은 VDE-TER Service 내부 일이다(지시서 4.2). 여기서는
    # 로그에 fragment 번호를 남기기 위해 크기만으로 나눠 흉내낸다.
    fragment_size = 128
    payload = outbound.payload
    fragments = [payload[i:i + fragment_size]
                 for i in range(0, len(payload), fragment_size)]
    total = len(fragments)

    clock = now
    for index, chunk in enumerate(fragments, start=1):
        clock += 0.4
        run.record(clock, "ROC->MASS", "send",
                   f"X509 cert frag {index}/{total}",
                   corr=f"frag-{index}", source_id=ROC_ID, destination_id=MASS_ID,
                   fragment=index, fragment_total=total, bytes=len(chunk),
                   data_type=outbound.data_type)
        clock += 0.25
        run.record(clock, "MASS->ROC", "ack",
                   f"frag {index} ACK",
                   corr=f"frag-{index}", source_id=MASS_ID, destination_id=ROC_ID,
                   status=4)

    reassembled = b"".join(fragments)
    clock += 0.1
    run.record(clock, "MASS->ROC", "recv", "인증서 재조립 완료",
               source_id=MASS_ID, fragment_total=total,
               payload_sha256=hashlib.sha256(reassembled).hexdigest())

    # --- MASS: 검증 후에만 저장 --------------------------------------------
    trust = TrustStore()
    clock += 0.2
    ok, reason = mass_receive_certificate(reassembled, trust, ca_der, now)
    fingerprint = cert.fingerprint_of(reassembled)
    run.record(clock, "MASS->ROC", "verify", f"인증서 검증: {reason}",
               ok=ok, reason=reason, stored=ok,
               fingerprint=fingerprint.hex(),
               payload_sha256=hashlib.sha256(reassembled).hexdigest(),
               trust_store_size=len(trust))

    # 같은 인증서 재수신 → 멱등이어야 한다 (저장소 크기 불변)
    clock += 0.2
    ok_again, reason_again = mass_receive_certificate(reassembled, trust, ca_der, now)
    run.record(clock, "MASS->ROC", "verify", "동일 인증서 재수신 (멱등 확인)",
               ok=ok_again, reason=reason_again,
               stored=False, trust_store_size=len(trust))

    clock += 0.2
    run.record(clock, "MASS->ROC", "note",
               f"신뢰 저장소 {len(trust)}건 (재수신 후에도 불변)")
    run.finish(clock)

    expected = {
        "scenario_id": "SC-VDE-001",
        "signature_verified": True,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "fragments": total,
        "all_fragments_acked": True,
        "max_retransmissions": 0,
        "ack_status": 4,
        "max_latency_seconds": 2.0,
        "no_duplicate_store": True,
    }
    return run, expected


# --------------------------------------------------------------------------
# SC-VDE-005 — S-421 노선 계획 교환
# --------------------------------------------------------------------------

SAMPLE_ROUTE = {
    "sourceMMSI": "004401234",
    "destinationMMSI": "440123456",
    "routeFormatVersion": "1.0",
    "routeID": "urn:route:roc01:demo-0001",
    "routeEditionNo": 1,
    "routeInfo": {
        "routeInfoName": "데모 항로",
        "routeInfoAuthor": "ROC-01",
        "routeInfoStatus": 3,
    },
    "headingSetPoint": 127.4,
    "fallbackState": "DynamicPositioning",
    "routeWaypoints": [
        {"id": "WP1",
         "position": {"lat": {"degrees": 35, "minutes": 6.1234, "hemisphere": "N"},
                      "lon": {"degrees": 129, "minutes": 4.5678, "hemisphere": "E"}},
         "turn_radius": 0.5, "leg_geometry": "Lexodrome"},
        {"id": "WP2",
         "position": {"lat": {"degrees": 35, "minutes": 12.5, "hemisphere": "N"},
                      "lon": {"degrees": 129, "minutes": 20.0, "hemisphere": "E"}},
         "turn_radius": 1.2, "leg_geometry": "Orthodrome"},
    ],
    "routeSchedule": [
        {"id": "WP1", "planned_SOG": 12.5},
        {"id": "WP2", "planned_SOG": 8.0},
    ],
}


def run_route_scenario(now: float) -> tuple[ScenarioRun, dict]:
    """논리 JSON → 압축 필드 → 전송 → ACK 흐름을 모의한다."""
    run = ScenarioRun(scenario_id="SC-VDE-005", started=now)

    fields = bridge.route_json_to_fields(SAMPLE_ROUTE)
    dropped = bridge.metadata_not_in_bits(SAMPLE_ROUTE)
    waypoints = len(fields["waypoints"])

    run.record(now, "ROC->MASS", "note",
               f"압축 미포함 metadata {len(dropped['dropped'])}항목: "
               f"{', '.join(sorted(dropped['dropped']))}")
    run.record(now + 0.3, "ROC->MASS", "send", "S-421 항로지시 (Message ID 2)",
               corr="route-1", source_id=ROC_ID, destination_id=MASS_ID,
               waypoints=waypoints,
               heading_setpoint=fields["heading_setpoint"],
               fallback_state=fields["fallback_state"])

    # MASS 측: 압축 필드를 논리 JSON 으로 되돌려 검토한 뒤 ACK
    restored = bridge.fields_to_route_json(fields)
    ack_json = bridge.ack_json_for(SAMPLE_ROUTE, 4)
    ack_fields = bridge.route_json_to_fields(ack_json)

    run.record(now + 0.9, "MASS->ROC", "ack", "S-421 ACK (Message ID 3)",
               corr="route-1", source_id=MASS_ID, destination_id=ROC_ID,
               status=ack_fields["status"],
               waypoints=len(restored["routeWaypoints"]))
    run.record(now + 1.0, "MASS->ROC", "note",
               "압축 ACK 에는 routeID 참조가 없다 — "
               "docs/작업B_ACK상관_갭분석.md 의 (a)안 권고")
    run.finish(now + 1.2)

    expected = {
        "scenario_id": "SC-VDE-005",
        "ack_status": 4,
        "waypoints": waypoints,
        "max_latency_seconds": 2.0,
    }
    return run, expected


# --------------------------------------------------------------------------
# 시나리오 등록 — 통합 시 여기만 실제 행위자 호출로 바꾸면 된다
# --------------------------------------------------------------------------

SCENARIOS = {
    "SC-VDE-001": ("X.509 인증서 송수신", run_certificate_scenario),
    "SC-VDE-005": ("S-421 노선 계획 교환", run_route_scenario),
}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def command_list(_args) -> int:
    for scenario_id, (name, _) in SCENARIOS.items():
        print(f"  {scenario_id}  {name}")
    return 0


def command_run(args) -> int:
    scenario_id = args.scenario_id
    if scenario_id not in SCENARIOS:
        print(f"알 수 없는 시나리오: {scenario_id}", file=sys.stderr)
        print("사용 가능한 목록은 'list' 명령으로 확인하라.", file=sys.stderr)
        return 2

    name, runner = SCENARIOS[scenario_id]
    now = float(args.now) if args.now is not None else time.time()

    run, expected = runner(now)
    summary = analyze(run, expected)

    if args.json:
        print(json.dumps({"run": run.to_dict(), "summary": summary},
                         ensure_ascii=False, indent=2))
    else:
        print(f"시나리오 {scenario_id} — {name}")
        print()
        for entry in run.ordered():
            offset = entry.time - run.started
            print(f"  +{offset:6.2f}s  {entry.direction:<9} {entry.kind:<7}"
                  f" {entry.message}")
        print()
        print(to_text(summary))

    if not args.no_save:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        path = os.path.join(RESULTS_DIR, f"{scenario_id}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"run": run.to_dict(), "summary": summary},
                                    ensure_ascii=False, indent=2))
        if not args.json:
            print(f"\n로그 저장: {path}")

    return 0 if summary["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario_runner",
        description="MASS/ROC 시나리오를 골라 실행하고 로그·판정을 낸다")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="시나리오 목록").set_defaults(
        handler=command_list)

    run_parser = subparsers.add_parser("run", help="시나리오 실행")
    run_parser.add_argument("scenario_id", help="예: SC-VDE-001")
    run_parser.add_argument("--json", action="store_true",
                            help="로그·판정을 JSON 으로 출력")
    run_parser.add_argument("--now", type=float, default=None,
                            help="기준 시각(epoch). 재현용 고정값")
    run_parser.add_argument("--no-save", action="store_true",
                            help="results/ 에 저장하지 않는다")
    run_parser.set_defaults(handler=command_run)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
