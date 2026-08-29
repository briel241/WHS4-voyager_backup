"""Stage 7 TEST TBB signer/trust 자료 provisioning (`stage7` §7, §13.4).

프로젝트 TEST 전용 P-256 signer key, self-signed certificate와 trust store를
`configs/stage7/trust/`에 생성한다. 이 identity는 재현 가능한 TEST profile
전용이며 실제 운용 신원을 주장하지 않는다. private key는 log/evidence에
복사하지 않는다.

한 번 생성한 뒤 fingerprint가 배포 identity에 결속되므로 재생성하지 않는다.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

TRUST_DIR = os.path.join(VDETER_ROOT, "configs", "stage7", "trust")
KEY_PATH = os.path.join(TRUST_DIR, "roc_tbb_signer_key.pem")
CERT_PATH = os.path.join(TRUST_DIR, "roc_tbb_signer_cert.pem")
STORE_PATH = os.path.join(TRUST_DIR, "tbb_trust_store.pem")
FINGERPRINT_PATH = os.path.join(TRUST_DIR, "tbb_certificate_fingerprint.txt")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args(argv)
    if os.path.exists(CERT_PATH) and not arguments.force:
        print(f"[exists] {CERT_PATH} — 기존 identity를 유지합니다 (--force로 재생성)")
        with open(FINGERPRINT_PATH, "r", encoding="utf-8") as handle:
            print(f"[fingerprint] {handle.read().strip()}")
        return 0

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from vdeter.link.tbb.crypto import certificate_fingerprint

    os.makedirs(TRUST_DIR, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "VDETER STAGE7 TEST TBB SIGNER")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2046, 1, 1, tzinfo=datetime.timezone.utc))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_der = certificate.public_bytes(serialization.Encoding.DER)
    with open(KEY_PATH, "wb") as handle:
        handle.write(key_pem)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    with open(CERT_PATH, "wb") as handle:
        handle.write(cert_pem)
    with open(STORE_PATH, "wb") as handle:
        handle.write(cert_pem)
    fingerprint = certificate_fingerprint(cert_der)
    if fingerprint.is_err:
        print(f"[error] fingerprint 계산 실패: {fingerprint.error}", file=sys.stderr)
        return 1
    with open(FINGERPRINT_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(fingerprint.value.hex() + "\n")
    print(f"[generated] {CERT_PATH}")
    print(f"[fingerprint] {fingerprint.value.hex()}")
    print("[note] private key는 evidence/log에 복사하지 않는다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
