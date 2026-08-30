"""신뢰 저장소 — 검증을 통과한 인증서만 담는다 (SC-VDE-001).

키는 인증서 지문(signatureValue 최하위 48 bit, 6 byte)이다.
같은 지문의 인증서를 다시 받아도 저장소는 변하지 않는다(멱등).

ITU-R M.2092-2 §A4-4.15: 검증에 실패한 인증서는 "수신하지 않은 것처럼"
처리한다. 그래서 이 클래스에는 검증 로직이 없다 — 넣기 전에 검증하는 책임은
호출자(``cert_flow.mass_receive_certificate``)에 있고, 이 클래스는 저장만 한다.
"""

from __future__ import annotations

from . import certificate as cert


class TrustStore:
    """지문 → 인증서 DER 매핑. 검증 통과한 인증서만 넣는다. 멱등."""

    def __init__(self) -> None:
        self._by_fingerprint: dict[bytes, bytes] = {}

    # -- 저장 ---------------------------------------------------------------

    def add(self, der: bytes) -> None:
        """지문을 키로 저장한다. 같은 지문 재수신은 무시한다."""
        fingerprint = cert.fingerprint_of(der)
        if fingerprint in self._by_fingerprint:
            return                                   # 멱등: 첫 인증서를 유지
        self._by_fingerprint[fingerprint] = bytes(der)

    # -- 조회 ---------------------------------------------------------------

    def get(self, fingerprint: bytes) -> bytes | None:
        return self._by_fingerprint.get(bytes(fingerprint))

    def __contains__(self, fingerprint: object) -> bool:
        if not isinstance(fingerprint, (bytes, bytearray)):
            return False
        return bytes(fingerprint) in self._by_fingerprint

    def public_key_of(self, fingerprint: bytes):
        """저장된 인증서의 공개키 객체. 없으면 None."""
        der = self.get(fingerprint)
        if der is None:
            return None
        return cert.public_key_from_der(cert.parse_certificate(der).public_key_der)

    # -- 편의 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_fingerprint)

    def fingerprints(self) -> list[bytes]:
        """저장된 지문 목록(삽입 순서)."""
        return list(self._by_fingerprint)

    def certificates(self) -> list[bytes]:
        """저장된 인증서 DER 목록(삽입 순서)."""
        return list(self._by_fingerprint.values())

    def __repr__(self) -> str:  # pragma: no cover - 표시용
        keys = ", ".join(f.hex() for f in self._by_fingerprint)
        return f"TrustStore({len(self)}건: {keys})"
