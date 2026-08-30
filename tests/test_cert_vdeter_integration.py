"""SC-VDE-001 X.509 인증서 — VDETER 실 연동 검증.

우리가 만든 Certificate Report 본문이 **실제 VDETER Presentation codec**
(x509.certificate_report)을 통과하고, 우리 브리지가 만든 `SubmitTransferRequest`가
VDETER DTO 검증을 통과하며, 수신 통지(`ReceivedApplicationData`)를 우리 응용이
파싱·검증·저장하는 전 경로를 확인한다.

전제
    - `cryptography` 필요: VDETER cert codec은 진짜 X.509 v3 DER 구조를 검증하므로
      대체(HMAC) 백엔드 인증서는 통과하지 못한다.
    - `vdeter` 패키지 필요(in-repo VDETER/src 또는 설치본).
    둘 중 하나라도 없으면 이 시험은 건너뛴다(SKIP).

실행
    python3 tests/test_cert_vdeter_integration.py
    (cryptography가 있는 인터프리터에서 돌려야 실제 검증까지 간다.)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.security import certificate as cert
from common import vdeter_bridge as bridge

# --- 전제 확인: 없으면 SKIP -------------------------------------------------

if not cert._HAVE_CRYPTOGRAPHY:
    print("SKIP: cryptography가 없어 실제 X.509 DER를 만들 수 없다")
    sys.exit(0)
if not bridge.VDETER_AVAILABLE:
    print("SKIP: vdeter 패키지를 import할 수 없다 (VDETER/src 또는 설치본 필요)")
    sys.exit(0)

cert.set_backend(cert.BACKEND_STANDARD)  # 실 X.509 경로 강제

from common.messages import certificate_report as report
from common.outbound import DATA_TYPE_X509_CERTIFICATE
from common.security.cert_flow import (
    mass_receive_certificate_report,
    roc_issue_certificate_report,
)
from common.security.trust_store import TrustStore

# 실 VDETER 구성요소
from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    ReceivedApplicationData,
    SubmitTransferRequest,
)
from vdeter.common.identifiers import PayloadFormat, StationId
from vdeter.presentation.codecs.certificate_report import CertificateReportCodec

NOW = 1_760_000_000.0
DAY = 86_400.0
ROC_MMSI = 4_400_0001
MASS_MMSI = 440_123_456


def make_ca():
    private_key, public_key = cert.generate_key_pair()
    der = cert.build_certificate(
        subject="ROC-CA", issuer="ROC-CA",
        subject_public_key=public_key, issuer_private_key=private_key,
        serial_number=1, not_before=NOW - DAY, not_after=NOW + 365 * DAY,
        roc_id="ROC-01", is_ca=True,
    )
    return private_key, der


def make_vessel_cert(ca_key, *, not_after=NOW + 30 * DAY, serial=2):
    _, vessel_pub = cert.generate_key_pair()
    return cert.build_certificate(
        subject="MASS-440123456", issuer="ROC-CA",
        subject_public_key=vessel_pub, issuer_private_key=ca_key,
        serial_number=serial, not_before=NOW - DAY, not_after=not_after,
        mass_id="MASS-01", mmsi=str(MASS_MMSI), imo="IMO9999999",
    )


# --------------------------------------------------------------------------
# 1. ROC: 인증서 → Report Outbound → SubmitTransferRequest (실 DTO)
# --------------------------------------------------------------------------

def test_roc_outbound_to_vdeter_submit_request():
    ca_key, _ = make_ca()
    vessel_der = make_vessel_cert(ca_key)

    outbound = roc_issue_certificate_report(vessel_der, mass_id=MASS_MMSI)
    assert outbound.data_type == DATA_TYPE_X509_CERTIFICATE
    assert outbound.ack_required is True

    request = bridge.to_submit_request(outbound)
    assert isinstance(request, SubmitTransferRequest)
    assert request.payload_format == PayloadFormat("x509.certificate_report")
    assert request.destination == Addressed(StationId(MASS_MMSI))
    assert request.ack_required is True
    # payload는 우리 Report 본문 그대로 (VDE header 없는 완성 bytes)
    assert request.payload == outbound.payload
    print("  [1] ROC Outbound → SubmitTransferRequest OK")


# --------------------------------------------------------------------------
# 2. 실 VDETER codec 왕복 — 가장 중요한 통합 증명
# --------------------------------------------------------------------------

def test_report_body_passes_real_vdeter_codec():
    ca_key, _ = make_ca()
    vessel_der = make_vessel_cert(ca_key)
    outbound = roc_issue_certificate_report(vessel_der, mass_id=MASS_MMSI)

    codec = CertificateReportCodec()
    encoded = codec.encode(outbound.payload)
    assert encoded.is_ok, f"VDETER codec encode 실패: {encoded}"
    # wire = 2-byte prefix + 우리 본문
    assert encoded.value[:2] == bytes((0, 1))
    assert encoded.value[2:] == outbound.payload

    decoded = codec.decode(encoded.value)
    assert decoded.is_ok, f"VDETER codec decode 실패: {decoded}"
    assert decoded.value == outbound.payload
    print("  [2] Report 본문이 실 VDETER CertificateReportCodec 왕복 통과 OK")


# --------------------------------------------------------------------------
# 3. MASS: 수신 통지 → 파싱·검증·저장
# --------------------------------------------------------------------------

def _received(body):
    return ReceivedApplicationData(
        original_source=StationId(ROC_MMSI),
        original_destination=Addressed(StationId(MASS_MMSI)),
        payload_format=PayloadFormat("x509.certificate_report"),
        payload=body,
    )


def test_mass_receive_verifies_and_stores():
    ca_key, ca_der = make_ca()
    vessel_der = make_vessel_cert(ca_key)
    outbound = roc_issue_certificate_report(vessel_der, mass_id=MASS_MMSI)

    inbound = bridge.inbound_from_received(_received(outbound.payload))
    assert inbound.is_certificate_report
    assert inbound.source_id == ROC_MMSI

    trust = TrustStore()
    results = mass_receive_certificate_report(inbound.payload, trust, ca_der, NOW)
    assert results == [(report.CERT_TYPE_X509, True, "ok")]
    assert len(trust) == 1

    # 멱등: 같은 Report 재수신해도 1건
    mass_receive_certificate_report(inbound.payload, trust, ca_der, NOW)
    assert len(trust) == 1
    print("  [3] MASS 수신→검증→저장 + 멱등 OK")


def test_mass_rejects_forged_and_expired():
    ca_key, ca_der = make_ca()

    # 위조: 서명 뒤 1 byte 변조
    vessel_der = bytearray(make_vessel_cert(ca_key))
    vessel_der[-1] ^= 0x01
    outbound = roc_issue_certificate_report(bytes(vessel_der), mass_id=MASS_MMSI)
    trust = TrustStore()
    results = mass_receive_certificate_report(outbound.payload, trust, ca_der, NOW)
    assert results[0][1] is False and results[0][2] == "bad-signature"
    assert len(trust) == 0, "위조 인증서는 저장소에 남으면 안 된다"

    # 만료
    expired = make_vessel_cert(ca_key, not_after=NOW - DAY, serial=3)
    outbound2 = roc_issue_certificate_report(expired, mass_id=MASS_MMSI)
    results2 = mass_receive_certificate_report(outbound2.payload, trust, ca_der, NOW)
    assert results2[0][1] is False and results2[0][2] == "expired"
    assert len(trust) == 0
    print("  [4] 위조·만료 거부 + 저장소 불변 OK")


if __name__ == "__main__":
    test_roc_outbound_to_vdeter_submit_request()
    test_report_body_passes_real_vdeter_codec()
    test_mass_receive_verifies_and_stores()
    test_mass_rejects_forged_and_expired()
    print("PASS: SC-VDE-001 VDETER 실 연동 (4 시나리오)")
