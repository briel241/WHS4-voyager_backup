#!/usr/bin/env python3
"""SC-VDE-001 인증서 — VDETER 소프트웨어 무선 loopback 종단 시연.

우리 인증서 payload가 **실제 VDETER 스택 전체**(Presentation → Link → PHY →
소프트웨어 무선 매체 → 수신 복원)를 지나 반대편 endpoint에 byte 그대로 도착하고,
우리 응용이 그것을 파싱·검증·신뢰저장소 반영까지 하는 것을 보인다. 하드웨어도,
팀원 launcher도 필요 없다 — VDETER가 제공하는 소프트웨어 무선 그룹
(`build_stage5_software_group`, tests/support)을 그대로 쓴다.

무엇을 증명하나
    - 정상: ROC→MASS 인증서 전송이 COMPLETED, 수신 byte 일치, 서명 검증·저장 성공.
    - 위조: 서명 변조 인증서는 링크로는 전달되지만(구조는 유효) 응용 검증에서
      bad-signature로 거부되어 신뢰저장소가 불변.

전제
    - `cryptography` 필요(VDETER cert codec이 진짜 X.509 DER 구조를 검증).
    - in-repo `VDETER/`(src + tests/support 하네스) 필요.
    없으면 안내를 출력하고 건너뛴다.

실행
    (cryptography 설치된 인터프리터로)
    python3 apps/cert_loopback_demo.py
"""

from __future__ import annotations

import os
import sys

# sys.path 순서: vdeter(src) → VDETER(tests.support 하네스) → repo(common).
# `tests`가 VDETER/tests로 해석되도록 VDETER를 repo 앞에 둔다.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.security import certificate as cert

if not cert._HAVE_CRYPTOGRAPHY:
    print("SKIP: cryptography가 없어 실제 X.509 DER를 만들 수 없습니다.")
    print("      pip install cryptography 후 다시 실행하세요.")
    sys.exit(0)

try:
    from tests.support.stage5 import (  # VDETER 소프트웨어 무선 하네스
        MASS_A_STATION_ID,
        ROC_STATION_ID,
        build_stage5_software_group,
        drive_stage5_group,
        received_payloads,
        start_stage5_group,
    )
    from vdeter.common.contracts.service import (
        TransferState,
        TransferStatusRequest,
    )
except ImportError as exc:
    print(f"SKIP: VDETER 소프트웨어 무선 하네스를 불러올 수 없습니다 ({exc}).")
    sys.exit(0)

cert.set_backend(cert.BACKEND_STANDARD)  # 실 X.509 경로 강제

from common import vdeter_bridge as bridge
from common.security.cert_flow import (
    mass_receive_certificate_report,
    roc_issue_certificate_report,
)
from common.security.trust_store import TrustStore

NOW = 1_760_000_000.0
DAY = 86_400.0
MAX_DRIVE_STEPS = 800


def _make_ca():
    key, pub = cert.generate_key_pair()
    der = cert.build_certificate(
        subject="ROC-CA", issuer="ROC-CA",
        subject_public_key=pub, issuer_private_key=key,
        serial_number=1, not_before=NOW - DAY, not_after=NOW + 365 * DAY,
        roc_id="ROC-01", is_ca=True,
    )
    return key, der


def _make_vessel_cert(ca_key, *, serial=2):
    _, pub = cert.generate_key_pair()
    return cert.build_certificate(
        subject="MASS-A", issuer="ROC-CA",
        subject_public_key=pub, issuer_private_key=ca_key,
        serial_number=serial, not_before=NOW - DAY, not_after=NOW + 30 * DAY,
        mass_id="MASS-A", mmsi=str(MASS_A_STATION_ID.value),
    )


def _drive_to_terminal(clock, group, endpoint, transfer_id):
    state = None
    for _ in range(MAX_DRIVE_STEPS):
        drive_stage5_group(clock, group, 1)
        state = endpoint.status(TransferStatusRequest(transfer_id)).value.state
        if state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED):
            break
    return state


def _send_certificate(cert_der, ca_der):
    """ROC→MASS로 인증서를 보내고, 수신·검증 결과를 돌려준다.

    전송 하나마다 소프트웨어 무선 그룹을 새로 세운다. ACK 전송을 한 그룹에서
    연달아 돌리면 TDMA·재전송 상태가 얽혀 뒤 전송이 실패할 수 있어, 시연에서는
    전송을 격리한다(VDETER 통합 시험도 전송당 그룹을 격리한다).
    """
    clock, group = build_stage5_software_group()
    start_stage5_group(clock, group)
    try:
        by_station = group.by_station()
        roc = by_station[ROC_STATION_ID.value]
        mass = by_station[MASS_A_STATION_ID.value]

        outbound = roc_issue_certificate_report(cert_der, mass_id=MASS_A_STATION_ID.value)
        request = bridge.to_submit_request(outbound)

        before = len(received_payloads(mass))
        accepted = roc.submit(request)
        if not accepted.is_ok:
            raise RuntimeError(f"submit 거부: {getattr(accepted, 'error', accepted)}")

        state = _drive_to_terminal(clock, group, roc, accepted.value.transfer_id)
        received = received_payloads(mass)[before:]
    finally:
        group.stop()

    trust = TrustStore()
    results = []
    if received:
        results = mass_receive_certificate_report(received[0], trust, ca_der, NOW)

    return {
        "submitted_bytes": len(request.payload),
        "state": state,
        "received": received,
        "byte_equal": bool(received) and received[0] == request.payload,
        "verify_results": results,
        "trust_size": len(trust),
    }


def main() -> int:
    ca_key, ca_der = _make_ca()
    print("=== VDETER 소프트웨어 무선 loopback — SC-VDE-001 ===\n")

    # 1) 정상 인증서
    vessel_der = _make_vessel_cert(ca_key)
    good = _send_certificate(vessel_der, ca_der)
    print(f"[정상] ROC→MASS 인증서 {good['submitted_bytes']} byte 제출")
    print(f"  전송 상태     : {good['state'].value}")
    print(f"  수신 byte 일치: {good['byte_equal']}")
    print(f"  검증 결과     : {good['verify_results']}")
    print(f"  신뢰저장소     : {good['trust_size']}건\n")
    assert good["state"] is TransferState.COMPLETED, "정상 전송이 COMPLETED가 아님"
    assert good["byte_equal"], "수신 byte 불일치"
    assert good["verify_results"] == [(1, True, "ok")], "검증 실패"
    assert good["trust_size"] == 1

    # 2) 위조 인증서(서명 1 byte 변조) — 링크는 전달, 응용이 거부
    forged = bytearray(_make_vessel_cert(ca_key, serial=3))
    forged[-1] ^= 0x01
    bad = _send_certificate(bytes(forged), ca_der)
    print(f"[위조] 서명 변조 인증서 {bad['submitted_bytes']} byte 제출")
    print(f"  전송 상태     : {bad['state'].value}")
    print(f"  수신 byte 일치: {bad['byte_equal']}")
    print(f"  검증 결과     : {bad['verify_results']}")
    print(f"  신뢰저장소     : {bad['trust_size']}건\n")
    assert bad["state"] is TransferState.COMPLETED, "위조도 링크 전달 자체는 성공해야 함"
    assert bad["byte_equal"], "수신 byte 불일치"
    assert bad["verify_results"] and bad["verify_results"][0][1] is False
    assert bad["verify_results"][0][2] == "bad-signature"
    assert bad["trust_size"] == 0, "위조 인증서가 신뢰저장소에 남으면 안 됨"

    print("PASS: 인증서가 실 VDETER 스택을 종단 왕복하고, 응용 검증이 정상/위조를 가름")
    return 0


if __name__ == "__main__":
    sys.exit(main())
