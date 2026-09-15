"""identity 저장소 — 생성·검증·재생성 (Phase I W4, README §2, 결정 7-⑤·11).

기존 `VDETER/tools/provision_stage7_trust.py`와 같은 재료(P-256 self-signed
TEST TBB signer, PKCS8 PEM, trust store = signer cert, 6-byte fingerprint)를
만들되 **배포 tree 밖의 identity/ 저장소**에 둔다.

정책 (결정 11)
    - `ensure_identity`: identity가 **없을 때만** 생성. 재실행은 불변경.
    - `new_identity`: 명시적 재생성 전용 — 기존 **공개 기록(인증서·trust
      store·fingerprint·manifest)만** `identity/archive/<시각>/`에 보관하고
      **기존 개인키는 보관 없이 폐기**한다. 새 identity는 UNQUALIFIED다 —
      run의 reference 대조(fingerprint 불일치)가 기계적으로 차단한다.
    - 개인키 원문·해시는 어떤 산출물에도 기록하지 않는다. manifest에는
      인증서 fingerprint 등 공개 정보만 담는다.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import time
from pathlib import Path

IDENTITY_MANIFEST_TYPE = "STAGE7_STANDALONE_IDENTITY_V1"
SUBJECT_COMMON_NAME = "VDETER STAGE7 TEST TBB SIGNER"

KEY_NAME = "roc_tbb_signer_key.pem"
CERT_NAME = "roc_tbb_signer_cert.pem"
STORE_NAME = "tbb_trust_store.pem"
FINGERPRINT_NAME = "tbb_certificate_fingerprint.txt"


class IdentityError(RuntimeError):
    """identity 저장소 계약 위반 — RF 접근 전 거부."""


def _ensure_vdeter_importable(repo_root: Path) -> None:
    for entry in (repo_root / "VDETER", repo_root / "VDETER" / "src"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def identity_paths(paths) -> dict:
    return {
        "key": paths.identity_private / KEY_NAME,
        "certificate": paths.identity_certificates / CERT_NAME,
        "trust_store": paths.identity_trust / STORE_NAME,
        "fingerprint": paths.identity_trust / FINGERPRINT_NAME,
        "manifest": paths.identity_manifest,
    }


def load_identity_manifest(paths):
    try:
        manifest = json.loads(paths.identity_manifest.read_bytes())
    except (OSError, ValueError):
        return None
    if manifest.get("artifact_type") != IDENTITY_MANIFEST_TYPE:
        return None
    return manifest


def ensure_identity(paths, *, now=None,
                    previous_fingerprint: str | None = None) -> dict:
    """identity가 없으면 생성, 있으면 그대로 (멱등 — 결정 11).

    반환: {"created": bool, "fingerprint": hex}. 부분 존재(인증서만 있고
    키가 없는 등)는 모호하므로 거부한다 — 명시적 `--new-identity`로만
    정리하게 한다.
    """
    files = identity_paths(paths)
    present = {name: path.is_file() for name, path in files.items()}
    if all(present.values()):
        manifest = load_identity_manifest(paths)
        if manifest is None:
            raise IdentityError(
                "identity-manifest.json is missing or corrupt — "
                "regenerate it with `vdeter setup --new-identity`")
        return {"created": False,
                "fingerprint": manifest.get("certificate_fingerprint")}
    if any(present.values()):
        broken = [name for name, exists in present.items() if not exists]
        raise IdentityError(
            f"identity is only partially present (missing: {broken}) — not "
            "repaired automatically; regenerate it explicitly with "
            "`vdeter setup --new-identity`")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    _ensure_vdeter_importable(paths.root)
    from vdeter.link.tbb.crypto import certificate_fingerprint

    for directory in (paths.identity_private, paths.identity_certificates,
                      paths.identity_trust, paths.identity_archive):
        directory.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, SUBJECT_COMMON_NAME)])
    start = now or datetime.datetime(2026, 1, 1,
                                     tzinfo=datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=20 * 365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_der = certificate.public_bytes(serialization.Encoding.DER)

    fingerprint = certificate_fingerprint(cert_der)
    if fingerprint.is_err:
        raise IdentityError(f"fingerprint computation failed: {fingerprint.error}")
    fingerprint_hex = fingerprint.value.hex()

    files["key"].write_bytes(key_pem)
    try:
        os.chmod(files["key"], 0o600)
    except OSError:
        pass  # Windows 개발 환경 — 대상(Ubuntu)에서는 적용, preflight가 검증
    files["certificate"].write_bytes(cert_pem)
    files["trust_store"].write_bytes(cert_pem)
    files["fingerprint"].write_text(fingerprint_hex + "\n",
                                    encoding="utf-8", newline="\n")
    manifest = {
        "artifact_type": IDENTITY_MANIFEST_TYPE,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "subject_common_name": SUBJECT_COMMON_NAME,
        "certificate_fingerprint": fingerprint_hex,
        "previous_fingerprint": previous_fingerprint,
        "note": ("Identity for the TEST profile only. Neither the private key nor its hash is "
                 "recorded in any artifact. A new identity is UNQUALIFIED until "
                 "re-qualification."),
    }
    files["manifest"].write_text(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=1),
        encoding="utf-8", newline="\n")
    return {"created": True, "fingerprint": fingerprint_hex}


def check_identity_consistency(paths) -> dict:
    """manifest·인증서·trust store·fingerprint 파일이 전부 같은 identity를
    가리키는지 검사한다 (W5 자격 발급 전 필수 — 사용자 지시).

    반환: {"pass", "fingerprint", "errors"}. 개인키↔인증서 일치는
    local_preflight.check_key_cert_match가 별도로 본다 — 여기는 공개 자료
    간 정합만 본다.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    _ensure_vdeter_importable(paths.root)
    from vdeter.link.tbb.crypto import certificate_fingerprint

    files = identity_paths(paths)
    errors = []

    manifest = load_identity_manifest(paths)
    manifest_fp = (manifest or {}).get("certificate_fingerprint")
    if manifest is None:
        errors.append("identity-manifest.json missing or corrupt")

    def fingerprint_of(pem_path):
        certificate = x509.load_pem_x509_certificate(pem_path.read_bytes())
        der = certificate.public_bytes(serialization.Encoding.DER)
        result = certificate_fingerprint(der)
        if result.is_err:
            raise IdentityError(f"fingerprint computation failed: {result.error}")
        return result.value.hex()

    cert_fp = store_fp = file_fp = None
    try:
        cert_fp = fingerprint_of(files["certificate"])
    except (OSError, ValueError) as exc:
        errors.append(f"certificate load failed: {exc}")
    try:
        store_fp = fingerprint_of(files["trust_store"])
    except (OSError, ValueError) as exc:
        errors.append(f"trust store load failed: {exc}")
    try:
        file_fp = files["fingerprint"].read_text(encoding="utf-8").strip()
    except OSError as exc:
        errors.append(f"fingerprint file load failed: {exc}")

    observed = {"manifest": manifest_fp, "certificate": cert_fp,
                "trust_store": store_fp, "fingerprint_file": file_fp}
    distinct = {value for value in observed.values() if value is not None}
    if len(distinct) != 1:
        errors.append(f"fingerprint mismatch between identity materials: {observed}")
    return {"pass": not errors,
            "fingerprint": cert_fp,
            "observed": observed,
            "errors": errors}


def new_identity(paths) -> dict:
    """명시적 재생성 (결정 11): 공개 기록만 archive, 개인키는 폐기.

    반환: ensure_identity 결과 + {"archived_to", "destroyed_private_key"}."""
    files = identity_paths(paths)
    previous = load_identity_manifest(paths)
    previous_fingerprint = (previous or {}).get("certificate_fingerprint")

    archived_to = None
    public_records = [files["certificate"], files["trust_store"],
                      files["fingerprint"], files["manifest"]]
    if any(path.is_file() for path in public_records):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        archive_dir = paths.identity_archive / stamp
        suffix = 1
        while archive_dir.exists():
            suffix += 1
            archive_dir = paths.identity_archive / f"{stamp}-{suffix}"
        archive_dir.mkdir(parents=True)
        for path in public_records:
            if path.is_file():
                shutil.move(str(path), str(archive_dir / path.name))
        archived_to = archive_dir

    destroyed = False
    if files["key"].is_file():
        files["key"].unlink()  # 개인키는 보관하지 않는다 (결정 11)
        destroyed = True

    result = ensure_identity(paths, previous_fingerprint=previous_fingerprint)
    result["archived_to"] = str(archived_to) if archived_to else None
    result["destroyed_private_key"] = destroyed
    return result
