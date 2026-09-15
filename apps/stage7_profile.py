"""Stage 7 실행 profile과 qualification reference 검증 (Phase G).

이 모듈은 **직접 실행하는 CLI가 아니다.** 정식 사용자 진입점은
`apps/scenario_runner.py menu --transport stage7 --profile <profile.json>`이며,
그 아래 `apps/stage7_run.py`(formal service 계층)가 이 모듈을 호출한다.

profile은 비밀정보를 담지 않는다. 개인키 본문·해시는 어디에도 기록하지 않고,
Pi 안에 존재하는지와 권한(0600)·key/cert 일치 여부만 확인한다.

검증 단계(전부 RF 접근 전, fail-closed)
    1. profile schema
    2. qualification reference sidecar SHA-256과 본문 SHA-256 일치
    3. reference 내부 정합(TC PASS·gain·baseline·hardware·wiring)
    4. gain artifact 파일 SHA-256이 reference와 일치
    5. qualification-critical manifest: reference에 결속된 파일 해시가
       현재 배포본과 일치 (원격 대조는 stage7_run이 수행)
"""

from __future__ import annotations

import hashlib
import json
import os

PROFILE_ARTIFACT_TYPE = "STAGE7_EXECUTION_PROFILE"
PROFILE_SCHEMA_VERSION = 1
REFERENCE_ARTIFACT_TYPE = "STAGE7_QUALIFICATION_REFERENCE"
REFERENCE_SCHEMA_VERSION = 1
#: v2 (clone-bootstrap): Git·baseline Markdown 결속 대신 installation/domain
#: digest 결속. v1은 과거 evidence 조회용으로 계속 읽는다.
REFERENCE_SCHEMA_VERSION_V2 = 2
ACCEPTED_REFERENCE_SCHEMA_VERSIONS = (REFERENCE_SCHEMA_VERSION,
                                      REFERENCE_SCHEMA_VERSION_V2)

#: profile 필수 최상위 키.
_REQUIRED_PROFILE_KEYS = (
    "artifact_type", "schema_version", "profile_id",
    "qualification_reference", "orchestration", "pi", "config_preparation",
    "baseline_sha256", "gain", "endpoints", "wiring_profile", "rf_settings",
    "trust", "evidence_root",
)

#: profile/reference/evidence에 절대 들어가면 안 되는 표지.
_SECRET_MARKERS = ("PRIVATE KEY", "BEGIN EC PRIVATE", "BEGIN RSA",
                   "private_key_sha256", "private_key_hex", "private_key_base64")


class ProfileError(ValueError):
    """profile 또는 qualification reference가 계약을 지키지 않는다."""


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_secrets(name: str, payload: str) -> None:
    for marker in _SECRET_MARKERS:
        if marker in payload:
            raise ProfileError(f"{name} contains a secret marker: {marker!r}")


def load_reference(reference_path: str, *, expected_sha256: str | None = None) -> dict:
    """qualification reference를 읽고 sidecar·본문 SHA-256을 검증한다."""
    if not os.path.isfile(reference_path):
        raise ProfileError(f"qualification reference missing: {reference_path}")
    with open(reference_path, "rb") as handle:
        raw = handle.read()
    actual = sha256_bytes(raw)
    _no_secrets("qualification reference", raw.decode("utf-8", "replace"))

    sidecar_path = reference_path + ".sha256"
    if not os.path.isfile(sidecar_path):
        # `<name>.json` → `<name>.sha256` 형태도 허용한다.
        sidecar_path = os.path.splitext(reference_path)[0] + ".sha256"
    if not os.path.isfile(sidecar_path):
        raise ProfileError("qualification reference sidecar (.sha256) missing")
    with open(sidecar_path, "r", encoding="utf-8") as handle:
        sidecar = handle.read().split()[0].strip()
    if sidecar != actual:
        raise ProfileError(
            f"reference sidecar SHA mismatch: sidecar={sidecar[:16]}… "
            f"actual={actual[:16]}…")
    if expected_sha256 is not None and expected_sha256 != actual:
        raise ProfileError(
            f"differs from the reference SHA bound by the profile: "
            f"profile={expected_sha256[:16]}… actual={actual[:16]}…")

    reference = json.loads(raw.decode("utf-8"))
    if reference.get("artifact_type") != REFERENCE_ARTIFACT_TYPE:
        raise ProfileError(f"reference artifact_type differs: "
                           f"{reference.get('artifact_type')!r}")
    schema = reference.get("schema_version")
    if schema not in ACCEPTED_REFERENCE_SCHEMA_VERSIONS:
        raise ProfileError("reference schema_version differs")

    qualification = reference.get("qualification") or {}
    tc = qualification.get("tc_results") or {}
    for name in ("TC-STAGE7-RR-002", "TC-STAGE7-RR-003", "TC-STAGE7-RR-004"):
        if (tc.get(name) or {}).get("status") != "PASS":
            raise ProfileError(f"reference {name} is not PASS")
    if schema == REFERENCE_SCHEMA_VERSION_V2:
        # v2: installation/domain digest 결속이 필수. Git commit·branch·
        # dirty·baseline Markdown SHA는 판정값이 아니다 (optional provenance).
        installation = reference.get("installation") or {}
        digest = installation.get("installation_sha256")
        if not (isinstance(digest, str) and len(digest) == 64):
            raise ProfileError("reference (v2) has no installation_sha256")
        for key in ("rf_manifest_sha256", "operation_manifest_sha256",
                    "tooling_manifest_sha256"):
            if not (isinstance(installation.get(key), str)
                    and len(installation[key]) == 64):
                raise ProfileError(f"reference (v2) has no installation.{key}")
        if not installation.get("files"):
            raise ProfileError("reference (v2) has no installation.files")
    else:
        if not reference.get("qualified_source_commit"):
            raise ProfileError("reference has no qualified_source_commit")
        critical = reference.get("qualification_critical_manifest") or {}
        # v1: strict_files(전체 해시 결속) + function_bound_files(함수 단위
        # 결속). 구판 호환: files 하나만 있는 형식도 받는다.
        if not (critical.get("strict_files") or critical.get("files")):
            raise ProfileError(
                "reference has no qualification-critical manifest")
    reference["_reference_sha256"] = actual
    reference["_reference_path"] = os.path.abspath(reference_path)
    return reference


def load_profile(profile_path: str) -> dict:
    """profile을 읽고 schema를 검증한 뒤 reference까지 함께 적재한다."""
    if not os.path.isfile(profile_path):
        raise ProfileError(f"profile missing: {profile_path}")
    with open(profile_path, "rb") as handle:
        raw = handle.read()
    _no_secrets("profile", raw.decode("utf-8", "replace"))
    profile = json.loads(raw.decode("utf-8"))

    if profile.get("artifact_type") != PROFILE_ARTIFACT_TYPE:
        raise ProfileError(f"profile artifact_type differs: "
                           f"{profile.get('artifact_type')!r}")
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ProfileError("profile schema_version differs")
    missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in profile]
    if missing:
        raise ProfileError(f"profile required keys missing: {missing}")

    reference_spec = profile["qualification_reference"]
    reference_path = reference_spec.get("path")
    if not reference_path:
        raise ProfileError("profile.qualification_reference.path missing")
    if not os.path.isabs(reference_path):
        reference_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(profile_path)),
                         reference_path))
    reference = load_reference(
        reference_path, expected_sha256=reference_spec.get("sha256"))

    profile["_profile_path"] = os.path.abspath(profile_path)
    profile["_profile_sha256"] = sha256_bytes(raw)
    profile["_reference"] = reference
    return profile


def validate_local_bindings(profile: dict) -> dict:
    """RF 접근 전 로컬에서 확인 가능한 결속을 전부 대조한다.

    반환: 검증 항목별 결과(dict). 하나라도 실패하면 ProfileError를 던진다.
    """
    reference = profile["_reference"]
    checks = {}

    def require(name, condition, detail=""):
        checks[name] = {"pass": bool(condition), "detail": detail}
        if not condition:
            raise ProfileError(f"{name} failed: {detail}")

    # baseline
    require("baseline_sha256 binding",
            profile["baseline_sha256"] == reference["baseline_sha256"],
            f"profile={profile['baseline_sha256'][:16]}… "
            f"reference={reference['baseline_sha256'][:16]}…")

    # gain artifact — 파일 실측 SHA가 reference와 일치해야 한다.
    gain = profile["gain"]
    artifact_path = gain["artifact_path"]
    input_path = gain["artifact_input_path"]
    for path, key, label in ((artifact_path, "artifact_sha256", "gain artifact"),
                             (input_path, "artifact_input_sha256", "gain artifact input")):
        if not os.path.isfile(path):
            raise ProfileError(f"{label} file missing: {path}")
        actual = sha256_file(path)
        require(f"{label} SHA", actual == reference["gain"][key],
                f"actual={actual[:16]}… reference={reference['gain'][key][:16]}…")

    # gain input이 artifact를 가리키는지 (공식 계약)
    with open(input_path, "rb") as handle:
        gain_input = json.loads(handle.read().decode("utf-8"))
    require("gain input source_gain_artifact_sha256",
            gain_input.get("source_gain_artifact_sha256")
            == reference["gain"]["artifact_sha256"],
            str(gain_input.get("source_gain_artifact_sha256"))[:16])
    require("common RX gain binding",
            gain_input.get("endpoint_common_rx_gain_db")
            == reference["gain"]["endpoint_common_rx_gain_db"],
            str(gain_input.get("endpoint_common_rx_gain_db")))

    # hardware/wiring/RF 설정
    require("wiring profile",
            profile["wiring_profile"] == reference["wiring_profile"],
            profile["wiring_profile"])
    require("endpoint mapping",
            profile["endpoints"] == reference["hardware"]["endpoints"],
            "profile.endpoints != reference.hardware.endpoints")
    require("RF settings binding",
            profile["rf_settings"] == reference["rf_settings"],
            "profile.rf_settings != reference.rf_settings")
    forbidden = reference["hardware"].get("forbidden_legacy_serial")
    serials = {e["serial"] for e in profile["endpoints"].values()}
    require("forbidden legacy serial not used", forbidden not in serials, str(forbidden))

    # trust: 개인키는 경로와 요구 권한만 기술한다(내용·해시 금지).
    trust = profile["trust"]
    for key in ("certificate_path", "trust_store_path", "private_key_path",
                "private_key_required_mode"):
        require(f"trust.{key} present", key in trust, str(sorted(trust)))
    require("trust private key hash not recorded",
            not any("sha" in k.lower() and "key" in k.lower() for k in trust),
            str(sorted(trust)))

    checks["reference_sha256"] = {"pass": True,
                                  "detail": reference["_reference_sha256"]}
    checks["qualification_run_id"] = {
        "pass": True, "detail": reference["qualification"]["run_id"]}
    manifest = reference["qualification_critical_manifest"]
    strict = manifest.get("strict_files") or manifest.get("files") or {}
    checks["critical_file_count"] = {
        "pass": True,
        "detail": f"strict={len(strict)} "
                  f"function_bound={len(manifest.get('function_bound_files') or {})}"}
    return checks


def summary_lines(profile: dict) -> list:
    """대화형 메뉴 진입 시 보여줄 요약(비밀정보 없음)."""
    reference = profile["_reference"]
    endpoints = profile["endpoints"]
    serials = ", ".join(f"{name} {info['serial']}"
                        for name, info in sorted(endpoints.items()))
    return [
        f"profile ID        : {profile['profile_id']}",
        f"qualification run : {reference['qualification']['run_id']} "
        f"(Phase {reference['qualification']['phase']})",
        f"reference SHA-256 : {reference['_reference_sha256'][:32]}…",
        f"B210 serial       : {serials}",
        f"gain artifact     : {reference['gain']['artifact_sha256'][:32]}… "
        f"(RX {reference['gain']['endpoint_common_rx_gain_db']})",
        f"wiring            : {profile['wiring_profile']}",
        f"Pi                : {profile['pi']['host']}",
        f"deployment        : {profile['pi']['deploy_run_id']}",
    ]
