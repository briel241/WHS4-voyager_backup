#!/usr/bin/env python3
"""SC-VDE-001 인증서 — VDETER 소프트웨어 무선 loopback 시연 (운영 미지원 참고).

⚠ 이것은 **소프트웨어 무선 loopback**이다. SC-VDE-001의 실제 운영 경로인
  인증서 **Data Session은 링크 계층에서 미완성**(data_session_enabled=false)이라
  현재 미지원이며, 이 데모의 통과를 Data Session 종단 완료로 읽으면 안 된다.
  응용 코드·코덱 검증용 참고 시연으로 보존한다.

우리 인증서 payload가 실제 VDETER 소프트웨어 스택(Presentation → Link → PHY →
소프트웨어 무선 매체 → 수신 복원)을 지나 반대편 endpoint에 byte 그대로 도착하고,
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

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.security import certificate as cert

if not cert._HAVE_CRYPTOGRAPHY:
    print("SKIP: cryptography가 없어 실제 X.509 DER를 만들 수 없습니다.")
    print("      pip install cryptography 후 다시 실행하세요.")
    sys.exit(0)

try:
    from tests.support.stage5 import (  # VDETER 소프트웨어 무선 하네스(launcher 역할)
        MASS_A_STATION_ID,
        ROC_STATION_ID,
        STATION_ENDPOINT_IDS,
        build_stage5_software_group,
        drive_stage5_group,
        start_stage5_group,
    )
    from vdeter.common.contracts.service import (
        TransferState,
        TransferStatusRequest,
    )
    from vdeter.common.events import ServiceEventType
    from vdeter.service import HandlerServiceEventSink  # 공식 이벤트 binding
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


class _ReceiverApp:
    """공식 binding — DATA_RECEIVED로 온 application data를 모은다.

    실 launcher(또는 Stage 7)가 우리 앱에 넘길 방식과 동일하다:
    `HandlerServiceEventSink(handle)`로 endpoint에 연결되고, handle은 event를
    받아 두기만 한다(무거운 검증은 callback 밖에서).
    """

    def __init__(self) -> None:
        self.received = []

    def handle(self, event) -> None:
        if event.event_type is ServiceEventType.DATA_RECEIVED:
            self.received.append(event.received_data)


def _send_certificate(cert_der, ca_der):
    """ROC→MASS로 인증서를 보내고, 수신·검증 결과를 돌려준다.

    수신은 공식 `HandlerServiceEventSink` + `DATA_RECEIVED`로 받는다(가이드 §13·
    VDETER `stage5_service_api_demo`와 동일). 전송 하나마다 그룹을 격리한다.
    """
    receiver = _ReceiverApp()
    mass_endpoint_id = STATION_ENDPOINT_IDS[MASS_A_STATION_ID.value]

    def service_sink_factory(endpoint_id):
        handler = receiver.handle if endpoint_id == mass_endpoint_id else (lambda _e: None)
        return HandlerServiceEventSink(handler)

    clock, group = build_stage5_software_group(service_sink_factory=service_sink_factory)
    start_stage5_group(clock, group)
    try:
        roc = group.by_station()[ROC_STATION_ID.value]

        outbound = roc_issue_certificate_report(cert_der, mass_id=MASS_A_STATION_ID.value)
        request = bridge.to_submit_request(outbound)

        accepted = roc.submit(request)
        if not accepted.is_ok:
            raise RuntimeError(f"submit 거부: {getattr(accepted, 'error', accepted)}")

        state = _drive_to_terminal(clock, group, roc, accepted.value.transfer_id)
    finally:
        group.stop()

    # 공식 수신 경로: ServiceEvent → vdeter_bridge.Inbound → 우리 앱 검증
    inbounds = [bridge.inbound_from_received(rd) for rd in receiver.received]
    trust = TrustStore()
    results = []
    if inbounds:
        results = mass_receive_certificate_report(inbounds[0].payload, trust, ca_der, NOW)

    return {
        "submitted_bytes": len(request.payload),
        "state": state,
        "received": inbounds,
        "byte_equal": bool(inbounds) and inbounds[0].payload == request.payload,
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

    print("PASS: 인증서가 소프트웨어 무선 스택을 왕복하고, 응용 검증이 정상/위조를 가름")
    print("주의: 소프트웨어 loopback 결과다. 인증서 Data Session(운영 경로)은 미완성·미지원이다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
