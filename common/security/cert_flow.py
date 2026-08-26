"""인증서 송수신 행위자 흐름 (SC-VDE-001).

통합 시 이 두 함수는 각각 ``RocAgent`` / ``MassAgent`` 의 메서드로 옮기면 된다.
외부에서는 저장소를 볼 수 없어 독립 함수로 제출한다. 본문은 그대로 쓰고
``self`` 만 붙이면 된다.

인증서는 **계열 B(원시 payload)** 다. 응용이 하는 일은 논리적으로 두 줄뿐이며,
분할·재조립·자원할당은 전부 링크(VDE-TER Service) 내부 일이다.
"""

from __future__ import annotations

from .certificate import MAX_CERTIFICATE_BYTES, verify_certificate
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
