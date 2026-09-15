"""설치 capability 증언 — "이 설치가 명세상 지원 조건을 충족하는가"의 **유일한 권위 producer**.

이 module이 답하는 것은 하나뿐이다(A):

    A. 설치 capability — 이 설치가 dual-channel 구현의 명세상 지원 조건을 충족했는가?

다음 둘은 여기서 답하지 않으며 이 값으로 대체되지 않는다.

    B. runtime readiness — 지금 workload에서 tuning·창 등록·clock/generation·수신 준비가 유효한가?
       (`WorkloadRxScope.dual_ready` 등 별도 생산·검증·철회 경로)
    C. RF 실행 권한/최종 qualification — 지금 RF를 실행할 수 있는가? 설치가 qualified인가?
       (`vdeter qualify` reference·RF gate. capability 광고는 이 gate를 우회하지 않는다)

설계 제약(실측 근거)
--------------------
- 증언은 `var/installation/`에 둔다. `var`는 installation manifest scan에서 제외되므로
  (`installation_manifest.EXCLUDED_DIR_NAMES`) 이 파일은 어떤 domain digest도 바꾸지 않는다.
  기존 qualified reference(`vdeter-pi-hub-r7`·`vdeter-r7`·`vdeter-r4`)를 무효화하지 않는다.
- `RF_CRITICAL_QUALIFY_SYMBOLS` 확장은 쓰지 않는다. 그것은 `_domain_sha256`을 통해 rf digest →
  `installation_sha256`을 바꿔 **전체 RF ladder 재자격**을 강제한다(`compare_critical`).
  또한 심볼 해시기는 top-level `FunctionDef`/`Assign`만 결속하므로 `WorkloadRxScope.activate`·
  `apply_manual_tuning`(클래스 메서드)은 애초에 결속 대상이 될 수 없다.
- 새 registry·자격 원장·수동 승인 flag를 만들지 않는다. 기존 installation digest 체계에 결속만 한다.

거부 규칙(fail-closed)
----------------------
증언이 **없거나**, 형식·contract version이 다르거나, 결속 digest가 현재 설치와 다르거나(stale),
요구 검증 항목이 빠졌거나, 어떤 항목이라도 PASS가 아니면 **미지원**으로 답한다. 이유 목록을 함께
돌려주어 호출부가 evidence에 남길 수 있게 한다. 클라이언트·PoC manifest의 자기 선언이나 단순 파일
존재는 근거가 아니다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from apps.standalone import installation_manifest
from apps.standalone.state import write_json_atomic

ATTESTATION_TYPE = "STAGE7_STANDALONE_CAPABILITY_ATTESTATION_V1"
#: 증언 계약 version. 요구 검증 항목·판정 의미가 바뀌면 올린다(이전 증언은 stale로 거부된다).
CONTRACT_VERSION = 1

ATTESTATION_BASENAME = "capability-attestation.json"

#: 이 module이 판정할 수 있는 capability와, 지원으로 인정하기 위해 **반드시** PASS여야 하는 검증 항목.
#: 항목 id는 명세 gate에 대응한다(WSM-G2 = production 구현 설치 일치, WSM-G3 = RF-off 기능·계약 회귀).
REQUIRED_VERIFICATION = {
    "observe_dual_physical_channel": (
        "WSM-G2_production_common_lo_implementation_installed",
        "WSM-G3_rf_off_software_contract_regression",
    ),
}

#: 실신호로만 확인 가능한 항목 — 소프트웨어 증언으로 대체하지 않는다. 기록에만 남긴다.
HARDWARE_ONLY_ITEMS = {
    "observe_dual_physical_channel": (
        "WSM-G4_live_signal_dual_channel_decode",
        "WSM-G4_analog_bandwidth_4700000_calibration_noise_threshold",
        "WSM-G5_changed_installation_full_qualification",
    ),
}


class AttestationError(RuntimeError):
    """증언 파일 부재·손상·변조 — 호출부는 미지원으로 처리한다."""


def attestation_path(paths) -> Path:
    return installation_manifest.installation_dir(paths) / ATTESTATION_BASENAME


def build_attestation(installation: dict, *, capability: str, items: dict,
                      producer: str, method: str, evidence: dict | None = None,
                      now: str | None = None) -> dict:
    """검증 결과를 설치 digest에 결속한 증언 record로 만든다.

    `items`: {검증항목 id: {"result": "PASS"|..., "evidence_sha256": str|None, "note": str}}
    요구 항목이 빠지면 여기서 거부한다 — 불완전한 증언을 만들지 않는다.
    """
    if capability not in REQUIRED_VERIFICATION:
        raise AttestationError(f"unknown capability: {capability!r}")
    required = REQUIRED_VERIFICATION[capability]
    missing = [name for name in required if name not in (items or {})]
    if missing:
        raise AttestationError(
            f"{capability}: required verification items missing: {sorted(missing)}")
    digests = installation_manifest.digests_of(installation)
    return {
        "artifact_type": ATTESTATION_TYPE,
        "contract_version": CONTRACT_VERSION,
        "generated_utc": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "capability": capability,
        "installation_sha256": digests["installation_sha256"],
        "rf_manifest_sha256": digests["rf_manifest_sha256"],
        "operation_manifest_sha256": digests["operation_manifest_sha256"],
        "producer": producer,
        "verification_method": method,
        "required_items": list(required),
        "items": {name: dict(entry) for name, entry in sorted((items or {}).items())},
        "hardware_only_items": list(HARDWARE_ONLY_ITEMS.get(capability, ())),
        "note": ("Installation capability only. This record does not grant RF execution "
                 "authority, does not assert runtime readiness, and does not qualify the "
                 "installation."),
    }


def write_attestation(paths, record: dict) -> dict:
    """증언 + sha256 sidecar를 원자적으로 기록한다(reference 발급과 같은 패턴)."""
    path = attestation_path(paths)
    write_json_atomic(path, record)
    encoded = path.read_bytes()
    sha = installation_manifest.sha256_bytes(encoded)
    sidecar = path.with_name(path.name + ".sha256")
    with open(sidecar, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(sha + "\n")
    return {"path": str(path), "sha256": sha}


def load_attestation(paths):
    """증언을 적재한다. 없으면 None, 손상·형식 위반이면 :class:`AttestationError`."""
    path = attestation_path(paths)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AttestationError(f"capability attestation corrupt (not JSON): {path}") from exc
    if not isinstance(record, dict):
        raise AttestationError("capability attestation is not an object")
    if record.get("artifact_type") != ATTESTATION_TYPE:
        raise AttestationError("capability attestation artifact_type mismatch")
    return record


def support(paths, installation: dict, *, capability: str) -> tuple:
    """`(supported, reasons)` — 이 설치가 `capability`를 지원한다고 증언되어 있는가.

    `installation`은 현재 설치 record(`load_installation`/`verify_installation`의 결과). 증언이 그
    설치 digest에 결속되지 않았으면 stale로 거부한다. 어떤 이유로든 확신할 수 없으면 미지원이다.
    """
    reasons = []
    if capability not in REQUIRED_VERIFICATION:
        return False, [f"unknown capability: {capability}"]
    try:
        record = load_attestation(paths)
    except AttestationError as exc:
        return False, [str(exc)]
    if record is None:
        return False, ["no capability attestation on this installation "
                       "(absent = unsupported, fail-closed)"]
    if record.get("contract_version") != CONTRACT_VERSION:
        reasons.append("attestation contract_version %r != %r (stale contract)"
                       % (record.get("contract_version"), CONTRACT_VERSION))
    if record.get("capability") != capability:
        reasons.append("attestation is for capability %r, not %r"
                       % (record.get("capability"), capability))
    try:
        digests = installation_manifest.digests_of(installation)
    except (KeyError, TypeError):
        return False, ["current installation record has no domain digests"]
    for field in ("installation_sha256", "rf_manifest_sha256", "operation_manifest_sha256"):
        if record.get(field) != digests.get(field):
            reasons.append(
                "%s differs from the attested installation (stale attestation: "
                "attested %s, current %s) — re-verify this installation"
                % (field, str(record.get(field))[:12], str(digests.get(field))[:12]))
    required = REQUIRED_VERIFICATION[capability]
    items = record.get("items")
    if not isinstance(items, dict):
        reasons.append("attestation has no verification items")
        items = {}
    for name in required:
        entry = items.get(name)
        if not isinstance(entry, dict):
            reasons.append(f"required verification item missing: {name}")
            continue
        if entry.get("result") != "PASS":
            reasons.append("required verification item %s is %r (not PASS)"
                           % (name, entry.get("result")))
    return (not reasons), reasons


def describe(paths, installation: dict, *, capability: str) -> dict:
    """evidence·보고용 요약. 판정은 :func:`support`와 동일하다."""
    supported, reasons = support(paths, installation, capability=capability)
    try:
        record = load_attestation(paths)
    except AttestationError:
        record = None
    return {
        "capability": capability,
        "supported": supported,
        "reasons": reasons,
        "contract_version": CONTRACT_VERSION,
        "attested_installation_sha256": (record or {}).get("installation_sha256"),
        "producer": (record or {}).get("producer"),
        "verification_method": (record or {}).get("verification_method"),
        "hardware_only_items": list(HARDWARE_ONLY_ITEMS.get(capability, ())),
        "note": ("installation capability only — not runtime readiness, "
                 "not RF authority, not qualification"),
    }


__all__ = (
    "ATTESTATION_TYPE",
    "CONTRACT_VERSION",
    "ATTESTATION_BASENAME",
    "REQUIRED_VERIFICATION",
    "HARDWARE_ONLY_ITEMS",
    "AttestationError",
    "attestation_path",
    "build_attestation",
    "write_attestation",
    "load_attestation",
    "support",
    "describe",
)
