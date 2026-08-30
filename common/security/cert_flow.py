"""인증서 송수신 행위자 흐름 (SC-VDE-001).

통합 시 이 두 함수는 각각 ``RocAgent`` / ``MassAgent`` 의 메서드로 옮기면 된다.
외부에서는 저장소를 볼 수 없어 독립 함수로 제출한다. 본문은 그대로 쓰고
``self`` 만 붙이면 된다.

인증서는 **계열 B(원시 payload)** 다. 응용이 하는 일은 논리적으로 두 줄뿐이며,
분할·재조립·자원할당은 전부 링크(VDE-TER Service) 내부 일이다.
"""

from __future__ import annotations

from ..messages import certificate_report as report
from .certificate import MAX_CERTIFICATE_BYTES, fingerprint_of, verify_certificate
from .trust_store import TrustStore

# 송신 요청은 저장소의 outbound 계약을 그대로 쓴다 (지시서 3.3).
from ..outbound import BROADCAST_ID, DATA_TYPE_X509_CERTIFICATE, Outbound

CONTENT_TYPE_X509 = "application/pkix-cert"


def roc_issue_certificate(cert_der: bytes, mass_id: int) -> Outbound:
    """ROC 가 인증서 전송 요청을 만든다. data_type=X509_CERTIFICATE, ack_required=True.

    링크 계층 값(Link ID·Type·VPFI 등)은 넣지 않는다 — 지시서 3.3.
    """
    if not isinstance(cert_der, (bytes, bytearray)) or not cert_der:
        raise ValueError("인증서 DER 이 비어 있다")
    if len(cert_der) > MAX_CERTIFICATE_BYTES:
        raise ValueError(
            f"인증서가 {len(cert_der)} byte 로 한도 {MAX_CERTIFICATE_BYTES} byte 를 넘는다")
    if mass_id == BROADCAST_ID:
        # 3.3 규칙: broadcast 에는 ack_required=True 금지. 인증서는 ACK 가 필수라
        # 브로드캐스트로 보낼 수 없다.
        raise ValueError("인증서는 ACK 가 필요하므로 broadcast 로 보낼 수 없다")

    return Outbound(
        payload=bytes(cert_der),
        destination_id=mass_id,
        ack_required=True,
        data_type=DATA_TYPE_X509_CERTIFICATE,
        content_type=CONTENT_TYPE_X509,
        label="X.509 certificate",
    )


def mass_receive_certificate(cert_der: bytes, trust: TrustStore,
                             trust_anchor_der: bytes,
                             now: float) -> tuple[bool, str]:
    """MASS 가 인증서를 검증하고 통과 시에만 trust 에 반영한다.

    ITU-R M.2092-2 §A4-4.15: 검증 실패 시 '수신하지 않은 것처럼' 처리 —
    저장소 불변.

    **검증이 저장보다 먼저다.** 실패 경로에서는 ``trust`` 를 건드리지 않는다.
    """
    ok, reason = verify_certificate(cert_der, trust_anchor_der, now)
    if not ok:
        return False, reason                     # 저장소 불변
    trust.add(cert_der)                          # 멱등: 재수신해도 1건
    return True, reason


# --------------------------------------------------------------------------
# VDETER 연동 경로 — Certificate Report 본문 (payload_format=x509.certificate_report)
#
# VDETER 링크로 실제로 오가는 인증서 payload는 bare DER이 아니라 R1027
# Certificate Report 본문이다(common/messages/certificate_report.py). 아래 두
# 함수가 그 본문을 만들고(ROC) 받아 검증한다(MASS). 위의 bare-DER 함수는
# 링크 없이 응용 단독으로 다룰 때 그대로 쓴다.
# --------------------------------------------------------------------------


def roc_issue_certificate_report(
    cert_der: bytes, mass_id: int, *,
    certificate_type: int = report.CERT_TYPE_X509,
    fingerprint: bytes | None = None,
) -> Outbound:
    """ROC가 인증서를 Certificate Report 본문으로 감싼 전송 요청을 만든다.

    payload는 R1027 Certificate Report 본문이며, VDETER
    payload_format=x509.certificate_report로 전달된다(가이드 §8).
    """
    if not isinstance(cert_der, (bytes, bytearray)) or not cert_der:
        raise ValueError("인증서 DER이 비어 있다")
    if len(cert_der) > MAX_CERTIFICATE_BYTES:
        raise ValueError(
            f"인증서가 {len(cert_der)} byte로 한도 {MAX_CERTIFICATE_BYTES} byte를 넘는다")
    if mass_id == BROADCAST_ID:
        raise ValueError("인증서는 ACK가 필요하므로 broadcast로 보낼 수 없다")

    body = report.build_single(
        cert_der,
        fingerprint=fingerprint if fingerprint is not None else fingerprint_of(cert_der),
        certificate_type=certificate_type,
    )
    return Outbound(
        payload=body,
        destination_id=mass_id,
        ack_required=True,
        data_type=DATA_TYPE_X509_CERTIFICATE,
        content_type=CONTENT_TYPE_X509,
        label="X.509 certificate report",
    )


def mass_receive_certificate_report(
    report_body: bytes, trust: TrustStore, trust_anchor_der: bytes, now: float,
) -> list[tuple[int, bool, str]]:
    """MASS가 Certificate Report 본문을 받아 항목마다 검증·반영한다.

    각 항목을 (certificate_type, 성공여부, 사유)로 돌려준다. 검증을 통과한
    인증서만 trust에 넣는다(§A4-4.15, 실패 항목은 저장소 불변). CRL(type 2)은
    본 흐름에서 폐기 정책을 적용하지 않고 사유 "crl-not-handled"로 남긴다.
    """
    results: list[tuple[int, bool, str]] = []
    for entry in report.parse_report(report_body):
        if entry.certificate_type == report.CERT_TYPE_CRL:
            results.append((entry.certificate_type, False, "crl-not-handled"))
            continue
        ok, reason = verify_certificate(entry.der, trust_anchor_der, now)
        if ok:
            trust.add(entry.der)
        results.append((entry.certificate_type, ok, reason))
    return results
