"""`vdeter diag` — 미자격 후보 전용 **개발용 통제 RF 진단** (정상 운용 권한과 분리).

정상 `vdeter run`은 active qualification reference(`active.json` → `releases/<id>`,
포인터 SHA 강제)만 권한 원천으로 삼고 그대로 유지된다. 이 module은 그 경로를 바꾸지
않는다. 진단은 별도 authorization(`var/diagnostic/authorization.json`,
`STAGE8_DIAGNOSTIC_AUTHORIZATION_V1`)에 결속된다.

분리 원칙 (실행 명세 §5A):
- `active.json`·`releases/`·`candidate/`에는 **쓰지 않는다**(읽기만). reference 해시 교체·
  재결속 위장을 하지 않는다.
- `artifact_type`이 다르므로 `run_profile.load_reference`(정상 경로의 유일한 reference
  검증기)를 통과할 수 없다 — 정상 run은 이 파일을 구조적으로 볼 수 없다.
- QUALIFIED·RESEARCH_READY로 표시하지 않는다. run.json/summary.json은
  `reference_sha256=null`, `qualification_status=NOT_QUALIFIED`,
  `diagnostic_authorization_sha256`을 기록한다(`run_cmd.authority_fields`).
- 자격 상태(active 포인터·schema v2 reference)만 분리하고, 설치 무결성·identity·필수
  preflight(장비 serial/역할·속도·hub topology·잔존 프로세스·개인키)·`standalone_critical`
  블록 대조(digest·배선·profile·라이브러리·gain)·세션 deadline·단일 인스턴스·cleanup·B210
  반환·topology 재관측은 `run_cmd`의 것을 **그대로** 쓴다(`critical_block_mismatches`).
- 결속: 정확한 후보 installation digest(4종), Pi/B210 serial·역할·hub asset/VID:PID·배선
  profile, 허용 시나리오·PoC 허용, run/workload 상한, 만료. 후보 digest가 바뀌면 블록
  대조에서 거부되고 재발급이 필요하다(재발급은 아래 검증을 전부 다시 한다).
- 교정값(gain)은 지정한 **출처 reference**에서 가져오되, 출처와 후보의 물리 결속(platform/
  host profile·wiring profile·endpoints serial+speed_class·usb_topology·라이브러리 4종)과
  `installation.rf_critical_symbols`가 같을 때만 발급한다 — 정상 run이 gain artifact를
  수락하는 기준(`compare_critical`)과 같은 항목이다. 진단 run이 gain을 RF로 재측정한다고
  주장하지 않는다. 적합성을 세울 수 없으면 발급을 거부한다.

발급자는 이 CLI뿐이다(PoC 코드·외부 client가 쓸 수 있는 발급·확대 API 없음). plan을
파일에서 고치면 sidecar SHA 불일치로 거부된다. 한계: PoC child는 같은 UID로 실행되므로
파일시스템 수준의 변조 저항은 주장하지 않는다. RF 안전에 결정적인 상한(bring-up 2700 s·
workload 900 s·close 600 s)은 authorization이 아니라 기존 세션 코드가 강제한다.
진단 run은 bring-up 자동 재시도를 쓰지 않는다(run 1회 = bring-up 시도 1회).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from apps.standalone import (
    hardware_profiles,
    identity,
    installation_manifest,
    local_config,
    run_cmd,
)
from apps.standalone.state import StatePaths, write_json_atomic

AUTHORIZATION_TYPE = "STAGE8_DIAGNOSTIC_AUTHORIZATION_V1"
LEDGER_TYPE = "STAGE8_DIAGNOSTIC_LEDGER_V1"
RUN_ID_PREFIX = "diag-"
#: RX-only 세션이 READY를 유지할 수 있는 상한(초). 그 이상은 계획 밖.
MAX_HOLD_AFTER_READY_S = 600.0
#: plan 필수 키. 값 검증은 `validate_plan`.
PLAN_REQUIRED_KEYS = ("label", "allowed_scenarios", "poc_allowed",
                      "max_runs", "max_workloads_per_run", "expires_utc")
#: 출처 reference와 후보 실측이 같아야 하는 결속 항목 — gain 적합성의 근거.
CALIBRATION_BINDING_KEYS = ("platform_profile", "host_profile",
                            "wiring_profile", "endpoints", "usb_topology",
                            "library_versions", "rf_critical_symbols")

EXIT_PASS = run_cmd.EXIT_PASS
EXIT_CONFIG = run_cmd.EXIT_CONFIG
EXIT_HARDWARE = run_cmd.EXIT_HARDWARE
EXIT_UNQUALIFIED = run_cmd.EXIT_UNQUALIFIED
EXIT_INTERNAL = run_cmd.EXIT_INTERNAL


class DiagError(Exception):
    """진단 발급·실행 거부 — exit code와 조치 가능한 메시지."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------ 보조

def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_utc(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def _canonical_sha256(payload) -> str:
    return _sha256_bytes(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                    indent=1).encode("utf-8"))


def validate_plan(plan: dict) -> dict:
    """시험 계획 검증 — 허용 시나리오는 `run_cmd.SCENARIOS`의 부분집합, 상한은 양의 정수."""
    if not isinstance(plan, dict):
        raise DiagError(EXIT_CONFIG, "plan must be a JSON object")
    missing = [k for k in PLAN_REQUIRED_KEYS if k not in plan]
    if missing:
        raise DiagError(EXIT_CONFIG, "plan is missing required keys: %s" % missing)
    scenarios = plan["allowed_scenarios"]
    if (not isinstance(scenarios, list)
            or any(s not in run_cmd.SCENARIOS for s in scenarios)):
        raise DiagError(EXIT_CONFIG,
                        "plan.allowed_scenarios must be a subset of %s"
                        % sorted(run_cmd.SCENARIOS))
    for key in ("max_runs", "max_workloads_per_run"):
        value = plan[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DiagError(EXIT_CONFIG, "plan.%s must be a non-negative integer" % key)
    if plan["max_runs"] < 1:
        raise DiagError(EXIT_CONFIG, "plan.max_runs must be at least 1")
    if not isinstance(plan["poc_allowed"], bool):
        raise DiagError(EXIT_CONFIG, "plan.poc_allowed must be true/false")
    # 선택 키 `allowed_workloads`: `(scenario, case, poc_id)` 조합을 계획이 직접 표현한다.
    # 없으면 종전과 같이 `allowed_scenarios` × normal 로 해석한다(기존 계획 파일 호환).
    # 이것이 있어야 SC-VDE-001 forged와 workload별 PoC(동일 세션 baseline↔PoC 전환)를
    # 비대화형 진단에서 지정할 수 있다. 새 실행 엔진을 만들지 않고 기존 run 계획 통로를 쓴다.
    workloads = plan.get("allowed_workloads")
    if workloads is not None:
        if not isinstance(workloads, list) or not workloads:
            raise DiagError(EXIT_CONFIG,
                            "plan.allowed_workloads must be a non-empty list when present")
        for item in workloads:
            if not isinstance(item, dict):
                raise DiagError(EXIT_CONFIG,
                                "plan.allowed_workloads entries must be objects "
                                "{scenario, case[, poc_id]}")
            unknown = sorted(set(item) - {"scenario", "case", "poc_id"})
            if unknown:
                raise DiagError(EXIT_CONFIG,
                                "unknown allowed_workloads field(s): %s" % unknown)
            if item.get("scenario") not in plan["allowed_scenarios"]:
                raise DiagError(EXIT_CONFIG,
                                "allowed_workloads.scenario %r is not in allowed_scenarios"
                                % item.get("scenario"))
            if item.get("case", "normal") not in ("normal", "forged"):
                raise DiagError(EXIT_CONFIG,
                                "allowed_workloads.case must be normal or forged")
            poc_id = item.get("poc_id")
            if poc_id is not None and not (isinstance(poc_id, str) and poc_id.strip()):
                raise DiagError(EXIT_CONFIG,
                                "allowed_workloads.poc_id must be a non-empty string or absent")
            if poc_id is not None and not plan["poc_allowed"]:
                raise DiagError(EXIT_CONFIG,
                                "allowed_workloads.poc_id is set but plan.poc_allowed is false")
        if len(workloads) > plan["max_workloads_per_run"]:
            raise DiagError(EXIT_CONFIG,
                            "allowed_workloads has %d entries but max_workloads_per_run is %d"
                            % (len(workloads), plan["max_workloads_per_run"]))
    hold = plan.get("max_hold_after_ready_s", 0)
    if (not isinstance(hold, (int, float)) or isinstance(hold, bool)
            or hold < 0 or hold > MAX_HOLD_AFTER_READY_S):
        raise DiagError(EXIT_CONFIG, "plan.max_hold_after_ready_s must be within "
                                     "[0, %d]" % MAX_HOLD_AFTER_READY_S)
    try:
        _parse_utc(plan["expires_utc"])
    except (TypeError, ValueError) as exc:
        raise DiagError(EXIT_CONFIG, "plan.expires_utc must be UTC "
                                     "YYYY-MM-DDTHH:MM:SSZ") from exc
    return plan


# ------------------------------------------------------------ 결속 블록

def build_candidate_block(*, installation: dict, hardware: dict, preflight: dict,
                          identity_fingerprint: str, environment_manifest: dict,
                          gain: dict) -> dict:
    """후보의 `standalone_critical` 블록 — 정상 reference(`qualify_cmd.issue_reference`)와
    같은 모양·같은 helper(`digests_of`·`critical_topology`)로 조립한다.

    `compare_critical`이 읽는 키만 판정값이다(installation/rf/operation/tooling digest,
    platform_profile, endpoints, wiring, identity_fingerprint, library_versions, gain,
    host_profile, usb_topology). `environment_manifest_sha256`은 기록용이다."""
    digests = installation_manifest.digests_of(installation)
    observed = ((preflight.get("checks") or {}).get("b210") or {}).get(
        "observed_by_role") or {}
    endpoints = {role: {"serial": info.get("serial"),
                        "speed_class": info.get("speed_class")}
                 for role, info in sorted(observed.items()) if info}
    versions = {name: environment_manifest.get(f"{name}_version")
                for name in local_config.CRITICAL_LIBRARIES}
    block = {
        **digests,
        "platform_profile": hardware.get("platform_profile"),
        "endpoints": endpoints,
        "wiring": {"profile": (hardware.get("wiring") or {}).get("profile"),
                   "approved": True},
        "identity_fingerprint": identity_fingerprint,
        "library_versions": versions,
        "environment_manifest_sha256": _canonical_sha256(environment_manifest),
        "gain": dict(gain),
        "host_profile": hardware_profiles.profile_of(hardware),
    }
    if hardware_profiles.is_pi_powered_hub(hardware):
        block["usb_topology"] = hardware_profiles.critical_topology(hardware)
    return block


def calibration_applicability(source_reference: dict, candidate_block: dict, *,
                              candidate_installation: dict) -> dict:
    """출처 reference의 교정값이 이 후보에 적합한가 — 결속 동일성으로 판정(fail-closed).

    정상 run이 gain artifact를 수락할 때 대조하는 항목과 같은 항목이 출처와 후보에서
    같아야 한다. installation digest와 identity fingerprint는 **다를 수 있다**(그것이
    "미자격 후보"의 정의다) — 그 둘은 authorization에 후보 값으로 별도 결속된다."""
    src = source_reference.get("standalone_critical") or {}
    differences = []

    def same(name, a, b):
        if a != b:
            differences.append("%s: source=%r candidate=%r" % (name, a, b))

    same("platform_profile", src.get("platform_profile"),
         candidate_block.get("platform_profile"))
    same("host_profile",
         src.get("host_profile") or hardware_profiles.LEGACY_PI_PROFILE,
         candidate_block.get("host_profile"))
    same("wiring_profile", (src.get("wiring") or {}).get("profile"),
         (candidate_block.get("wiring") or {}).get("profile"))
    same("endpoints", src.get("endpoints"), candidate_block.get("endpoints"))
    same("usb_topology", src.get("usb_topology"), candidate_block.get("usb_topology"))
    same("library_versions", src.get("library_versions"),
         candidate_block.get("library_versions"))
    same("rf_critical_symbols",
         (source_reference.get("installation") or {}).get("rf_critical_symbols"),
         candidate_installation.get("rf_critical_symbols"))
    gain = src.get("gain") or {}
    for key in ("endpoint_common_rx_gain_db", "artifact_sha256",
                "artifact_input_sha256"):
        if not gain.get(key):
            differences.append("gain.%s missing in calibration source" % key)
    return {"applicable": not differences, "differences": differences,
            "checked": list(CALIBRATION_BINDING_KEYS) + ["gain fields present"],
            "not_compared_by_design": ["installation_sha256",
                                       "rf/operation/tooling manifest sha256",
                                       "identity_fingerprint"]}


def compare_authorization(reference: dict, *, installation: dict, hardware: dict,
                          preflight: dict, identity_fingerprint: str,
                          environment_manifest: dict) -> dict:
    """`run_cmd.prepare(critical_check=…)` 주입 — authorization 종류 확인 후 정상 run과
    **같은** 블록 검사(`critical_block_mismatches`)를 적용한다."""
    if (reference.get("artifact_type") != AUTHORIZATION_TYPE
            or reference.get("not_a_qualification") is not True):
        return {"pass": False,
                "mismatches": ["not a diagnostic authorization (artifact_type %r)"
                               % reference.get("artifact_type")]}
    mismatches = run_cmd.critical_block_mismatches(
        reference.get("standalone_critical") or {},
        installation=installation, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_fingerprint,
        environment_manifest=environment_manifest)
    return {"pass": not mismatches, "mismatches": mismatches}


# ------------------------------------------------------------ 발급

def authorize(paths: StatePaths, *, calibration_reference, plan_path,
              repo_root=None, hardware_path=None, preflight_fn=None,
              environment_manifest=None, now=None, out=print) -> dict:
    """진단 authorization 발급 (RF-off; 장치 open 없음 — preflight는 sysfs·파일 검사).

    순서: plan 검증 → hardware.toml(배선 승인) → installation 적재·재측정 → identity →
    필수 preflight → 환경 manifest → 출처 reference 적재(sidecar·TC PASS 검증) → 후보
    블록 조립 → 교정 적합성(결속 동일성) → 자기 검증(정상 run과 같은 블록 검사) →
    authorization + sidecar 원자 기록 → ledger 초기화(기존 ledger는 보존).
    어느 단계든 실패하면 아무것도 쓰지 않는다."""
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    try:
        with open(plan_path, encoding="utf-8") as handle:
            plan = validate_plan(json.load(handle))
    except (OSError, ValueError) as exc:
        raise DiagError(EXIT_CONFIG, "plan file unreadable or not JSON (%s): %s"
                        % (plan_path, exc)) from exc
    if not Path(calibration_reference).is_file():
        raise DiagError(EXIT_CONFIG, "calibration reference not found: %s"
                        % calibration_reference)
    try:
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))
    except local_config.ConfigGenerationError as exc:
        raise DiagError(EXIT_CONFIG, str(exc)) from exc
    if (hardware.get("wiring") or {}).get("approved") is not True:
        raise DiagError(EXIT_CONFIG, "hardware.toml wiring.approved is not true — "
                                     "the operator must approve the wiring first")
    try:
        installation = installation_manifest.load_installation(paths)
    except installation_manifest.InstallationError as exc:
        raise DiagError(EXIT_UNQUALIFIED,
                        "installation manifest unavailable or tampered: %s — "
                        "run `./bin/vdeter setup --no-launcher`" % exc) from exc
    check = installation_manifest.verify_installation(repo_root, installation)
    if not check["pass"]:
        raise DiagError(EXIT_UNQUALIFIED,
                        "installation files differ from the setup manifest (%s)"
                        % installation_manifest.describe_mismatch(check))
    current = check["current_record"]
    identity_check = identity.check_identity_consistency(paths)
    if not identity_check["pass"]:
        raise DiagError(EXIT_CONFIG, "identity material is inconsistent: "
                        + "; ".join(identity_check["errors"]))
    if preflight_fn is None:
        from apps.standalone.local_preflight import run_required_preflight

        def preflight_fn():
            return run_required_preflight(hardware=hardware, paths=paths)
    preflight = preflight_fn()
    if not preflight["pass"]:
        raise DiagError(EXIT_HARDWARE, "preflight failed: "
                        + "; ".join(preflight["failures"]))
    environment_manifest = (environment_manifest
                            or local_config.build_environment_manifest())

    from apps import run_profile
    try:
        source = run_profile.load_reference(str(calibration_reference))
    except run_profile.ProfileError as exc:
        raise DiagError(EXIT_CONFIG, "calibration source reference rejected: %s"
                        % exc) from exc
    source_sha = source.get("_reference_sha256") or _sha256_bytes(
        Path(calibration_reference).read_bytes())
    gain = dict((source.get("standalone_critical") or {}).get("gain") or {})
    block = build_candidate_block(
        installation=current, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_check["fingerprint"],
        environment_manifest=environment_manifest, gain=gain)
    applicability = calibration_applicability(
        source, block, candidate_installation=installation)
    if not applicability["applicable"]:
        raise DiagError(
            EXIT_UNQUALIFIED,
            "calibration source is not applicable to this candidate:\n  - "
            + "\n  - ".join(applicability["differences"])
            + "\nImpact: no diagnostic authorization is issued. Action: a new "
            "gain measurement (GAIN_CALIBRATION phase, RR-P1 equivalent, phase "
            "deadline 60 min, RF transmission) is required, or fix the "
            "candidate declaration so the bindings match.")
    self_check = run_cmd.critical_block_mismatches(
        block, installation=current, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_check["fingerprint"],
        environment_manifest=environment_manifest)
    if self_check:
        raise DiagError(EXIT_INTERNAL, "authorization self-check failed: "
                        + "; ".join(self_check))

    digests = installation_manifest.digests_of(current)
    authorization = {
        "artifact_type": AUTHORIZATION_TYPE,
        "schema_version": 1,
        "not_a_qualification": True,
        "qualification_status": "NOT_QUALIFIED",
        "issued_utc": now or _utc_now(),
        "issuer": "vdeter diag authorize (development-time controlled RF diagnostics)",
        "scope": ("Development-time controlled RF diagnostics on an UNQUALIFIED "
                  "candidate. This is not a qualification reference, grants no "
                  "normal-operation authority, and is never written to "
                  "var/qualification. Bound to exactly this installation digest, "
                  "this host/hardware declaration, this identity and this plan."),
        "candidate": {**digests, "file_count": current.get("file_count")},
        "standalone_critical": block,
        "calibration_source": {
            "reference_path": str(calibration_reference),
            "reference_sha256": source_sha,
            "qualification_run_id": ((source.get("qualification") or {})
                                     .get("run_id")),
            "source_installation_sha256": (source.get("standalone_critical") or {}
                                           ).get("installation_sha256"),
            "source_rf_manifest_sha256": (source.get("standalone_critical") or {}
                                          ).get("rf_manifest_sha256"),
            "gain": gain,
            "applicability": applicability,
            "note": ("gain is bound by binding equality — the same items the "
                     "normal run compares before accepting a gain artifact; the "
                     "diagnostic session does not re-measure gain over RF"),
        },
        "plan": plan,
        "limits_note": (
            "Enforced before any device access by `vdeter diag run`: run count, "
            "workloads per run, scenario set, PoC permission, expiry. Enforced by "
            "the existing session code (unchanged): bring-up 2700 s, workload "
            "900 s, close 600 s, single instance, cleanup and B210 return. Not "
            "used: bring-up auto-retry (one attempt per run). Not observable in "
            "evidence: per-burst RF counts (transfer-level counters only)."),
        "preflight_observed": {
            "endpoints": block["endpoints"],
            "hub": (((preflight.get("checks") or {}).get("usb_topology") or {})
                    .get("hub")),
        },
    }
    payload = (json.dumps(authorization, ensure_ascii=True, sort_keys=True,
                          indent=1) + "\n").encode("utf-8")
    sha = _sha256_bytes(payload)
    paths.diagnostic_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.diagnostic_authorization.with_name("authorization.json.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, paths.diagnostic_authorization)
    sidecar = paths.diagnostic_authorization.with_name("authorization.json.sha256")
    sidecar.write_text("%s  authorization.json\n" % sha, encoding="utf-8")
    previous = None
    if paths.diagnostic_ledger.exists():
        try:
            previous = json.loads(paths.diagnostic_ledger.read_bytes())
        except ValueError:
            previous = None
        archived = paths.diagnostic_dir / ("ledger-%s.json" % (
            (previous or {}).get("authorization_sha256", "unknown")[:12]))
        if not archived.exists():
            os.replace(paths.diagnostic_ledger, archived)
    write_json_atomic(paths.diagnostic_ledger, {
        "artifact_type": LEDGER_TYPE, "authorization_sha256": sha,
        "created_utc": _utc_now(), "runs": []})
    summary = {"path": str(paths.diagnostic_authorization), "sha256": sha,
               "plan": plan, "applicability": applicability,
               "calibration_reference_sha256": source_sha}
    out("diagnostic authorization issued: %s (sha256 %s)" % (
        paths.diagnostic_authorization, sha[:12]))
    out("  qualification_status : NOT_QUALIFIED (this is not a reference)")
    out("  bound installation   : %s" % digests["installation_sha256"][:12])
    out("  calibration source   : %s (%s)" % (
        source_sha[:12], authorization["calibration_source"]["qualification_run_id"]))
    out("  plan                 : %s scenarios=%s poc=%s max_runs=%d "
        "max_workloads_per_run=%d expires=%s" % (
            plan["label"], plan["allowed_scenarios"], plan["poc_allowed"],
            plan["max_runs"], plan["max_workloads_per_run"], plan["expires_utc"]))
    return summary


# ------------------------------------------------------------ 적재·ledger

def load_authorization(paths: StatePaths, *, now=None) -> dict:
    """authorization 적재·검증 — sidecar SHA, artifact_type, 진단 표지, plan, 만료.

    실패는 DiagError(EXIT_UNQUALIFIED). 반환 dict에는 `_reference_sha256`(= 파일 SHA)이
    추가된다 — `run_cmd`가 run.json/summary.json에 `diagnostic_authorization_sha256`으로
    기록한다."""
    path = paths.diagnostic_authorization
    if not path.is_file():
        raise DiagError(EXIT_UNQUALIFIED,
                        "no diagnostic authorization on this candidate (%s). Impact: "
                        "RF diagnostics are refused. Action: `vdeter diag authorize`."
                        % path)
    raw = path.read_bytes()
    sidecar = path.with_name("authorization.json.sha256")
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        raise DiagError(EXIT_UNQUALIFIED,
                        "diagnostic authorization sidecar (.sha256) missing or empty")
    actual = _sha256_bytes(raw)
    if expected != actual:
        raise DiagError(EXIT_UNQUALIFIED,
                        "diagnostic authorization SHA mismatch (sidecar %s… actual %s…)"
                        " — the file was modified after issuance; re-issue with "
                        "`vdeter diag authorize`" % (expected[:12], actual[:12]))
    try:
        authorization = json.loads(raw)
    except ValueError as exc:
        raise DiagError(EXIT_UNQUALIFIED,
                        "diagnostic authorization is not valid JSON: %s" % exc)
    if authorization.get("artifact_type") != AUTHORIZATION_TYPE:
        raise DiagError(EXIT_UNQUALIFIED, "diagnostic authorization artifact_type "
                                          "differs: %r" % authorization.get("artifact_type"))
    if authorization.get("not_a_qualification") is not True:
        raise DiagError(EXIT_UNQUALIFIED, "diagnostic authorization lacks the "
                                          "not_a_qualification marker")
    if not isinstance(authorization.get("standalone_critical"), dict):
        raise DiagError(EXIT_UNQUALIFIED, "diagnostic authorization has no binding block")
    plan = validate_plan(authorization.get("plan"))
    current_time = time.time() if now is None else now
    if _parse_utc(plan["expires_utc"]) <= current_time:
        raise DiagError(EXIT_UNQUALIFIED, "diagnostic authorization expired at %s"
                                          % plan["expires_utc"])
    authorization["_reference_sha256"] = actual
    return authorization


def load_ledger(paths: StatePaths, authorization_sha256: str) -> dict:
    if not paths.diagnostic_ledger.is_file():
        return {"artifact_type": LEDGER_TYPE,
                "authorization_sha256": authorization_sha256,
                "created_utc": _utc_now(), "runs": []}
    ledger = json.loads(paths.diagnostic_ledger.read_bytes())
    if ledger.get("authorization_sha256") != authorization_sha256:
        raise DiagError(EXIT_UNQUALIFIED,
                        "diagnostic ledger belongs to a different authorization "
                        "(%s… vs %s…) — re-issue" % (
                            str(ledger.get("authorization_sha256"))[:12],
                            authorization_sha256[:12]))
    return ledger


def plan_workloads(plan: dict, scenarios: list) -> list:
    """계획 + 요청 scenario → `run_cmd.execute`의 `workload_plan`.

    `allowed_workloads`가 있으면 그 조합(`scenario`, `case`, `poc_id`)을 **순서 그대로** 쓰되,
    요청된 scenario로 걸러 낸다(요청이 비면 RX-only이므로 빈 목록). 없으면 종전과 같이
    `scenario × normal`이다. 계획에 없는 case·PoC 조합은 여기서 만들어지지 않는다 —
    `ledger_refusals`가 장치 접근 전에 거부하고, 이 함수는 계획을 넘어 확장하지 않는다.
    """
    requested = list(scenarios)
    entries = plan.get("allowed_workloads")
    if not entries:
        return [(s, "normal", None) for s in requested]
    if not requested:
        return []
    remaining = list(requested)
    selected = []
    for item in entries:
        scenario = item.get("scenario")
        if scenario in remaining:
            remaining.remove(scenario)
            selected.append((scenario, item.get("case", "normal"), item.get("poc_id")))
    return selected


def ledger_refusals(ledger: dict, plan: dict, *, scenarios: list, poc_requested: bool,
                    hold_after_ready_s: float) -> list:
    """plan 한도 대조 — 장치 접근 전. 시작된 run은 결과와 무관하게 전부 계상한다."""
    reasons = []
    started = len(ledger.get("runs") or [])
    if started >= plan["max_runs"]:
        reasons.append("run limit reached (%d/%d runs already started)"
                       % (started, plan["max_runs"]))
    if len(scenarios) > plan["max_workloads_per_run"]:
        reasons.append("workload count %d exceeds plan max_workloads_per_run %d"
                       % (len(scenarios), plan["max_workloads_per_run"]))
    outside = [s for s in scenarios if s not in plan["allowed_scenarios"]]
    if outside:
        reasons.append("scenario(s) not allowed by plan: %s" % outside)
    # `allowed_workloads`가 있으면 **조합 단위**로도 대조한다. scenario만 맞고 계획에 없는
    # 조합이 실행되는 것을 막는다(계획 밖 workload 금지). 요청 scenario가 계획 항목에
    # 없으면 위 `outside`가 이미 잡지만, 계획이 scenario를 한 번만 허용했는데 같은
    # scenario를 여러 번 요청한 경우는 여기서 걸린다.
    entries = plan.get("allowed_workloads")
    if entries:
        available = {}
        for item in entries:
            available[item.get("scenario")] = available.get(item.get("scenario"), 0) + 1
        requested_counts = {}
        for scenario in scenarios:
            requested_counts[scenario] = requested_counts.get(scenario, 0) + 1
        for scenario, count in sorted(requested_counts.items()):
            allowed = available.get(scenario, 0)
            if count > allowed:
                reasons.append(
                    "scenario %s requested %d time(s) but the plan allows %d workload(s)"
                    % (scenario, count, allowed))
    if poc_requested and not plan["poc_allowed"]:
        reasons.append("PoC is not allowed by this plan")
    limit = float(plan.get("max_hold_after_ready_s", 0) or 0)
    if hold_after_ready_s > limit:
        reasons.append("hold_after_ready_s %.0f exceeds plan limit %.0f"
                       % (hold_after_ready_s, limit))
    return reasons


def unique_run_id(paths: StatePaths, *, clock=time.time) -> str:
    """`diag-YYYYmmdd-HHMMSS`; 같은 초에 겹치면 `-2`, `-3`… 접미(`allocate_run_dir`의
    자동 id와 같은 규칙). 사용자 지정 id는 존재 시 거부되므로 여기서 미리 유일하게 만든다
    — 빠른 재실행이 "run-id already exists"로 거부되지 않게."""
    base = RUN_ID_PREFIX + time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    candidate, n = base, 1
    while (paths.runs_dir / candidate).exists():
        n += 1
        candidate = "%s-%d" % (base, n)
    return candidate


#: coordinator가 "빈 측정"(workload 0)에 붙이는 정확한 실패 사유
#: (`VDETER/apps/coordinator_main.py::_service_session_terminal_status`).
#: 이 문자열 **하나만** 있을 때가 RX-only 세션의 기대 결과다.
EMPTY_MEASUREMENT_FAILURE = "workload가 하나도 완료되지 않음"


def rx_only_close_assessment(summary: dict) -> dict:
    """RX-only(workload 0) 세션의 close 결과 해석.

    coordinator는 빈 측정을 **설계상** fail-closed로 판정한다(같은 함수가 workload 실패·
    불완전 STOP도 함께 본다). RX-only는 workload를 제출하지 않는 것이 목적이므로 그 사유
    하나만 있는 FAIL은 기대된 결과다. 그러나 다른 사유가 섞이거나 STOP·cleanup·fault·
    inhibit에 문제가 있으면 **실패 그대로** 취급한다.

    coordinator의 rc·status·error는 바꾸지 않고 그대로 기록한다(숨기지 않는다). 반환:
    `{"expected_empty_measurement", "reasons", "coordinator_rc", "coordinator_status",
      "coordinator_error", "service_stop_rc", "cleanup_pass", "inhibited", "workloads"}`.
    """
    close = summary.get("close") or {}
    closed = close.get("closed") or {}
    cleanup = closed.get("cleanup") or (close.get("cleanup") or {})
    error = closed.get("error")
    workloads = closed.get("workloads")
    reasons = [part.strip() for part in str(error or "").split(";") if part.strip()]
    assessment = {
        "coordinator_rc": close.get("coordinator_rc"),
        "coordinator_status": closed.get("status"),
        "coordinator_error": error,
        "service_stop_rc": closed.get("service_stop_rc"),
        "cleanup_pass": cleanup.get("pass"),
        "workers_after_stop": cleanup.get("workers_after_stop"),
        "inhibited": closed.get("inhibited"),
        "workloads": workloads,
        "failure_reasons": reasons,
    }
    assessment["expected_empty_measurement"] = bool(
        reasons == [EMPTY_MEASUREMENT_FAILURE]
        and not workloads
        and closed.get("service_stop_rc") == 0
        and cleanup.get("pass") is True
        and cleanup.get("workers_after_stop") == 0
        and closed.get("inhibited") is False
        and summary.get("interrupted") is False
        and (summary.get("usb_topology_after_close") or {}).get("pass") is not False)
    return assessment


def _qualification_invariants(paths: StatePaths) -> dict:
    releases = paths.qualification_releases
    return {"active_json_present": paths.active_pointer.exists(),
            "release_count": (len([p for p in releases.iterdir()])
                              if releases.is_dir() else 0)}


# ------------------------------------------------------------ 실행

def run(paths: StatePaths, *, scenarios: list, poc=None, poc_dir=None,
        rf_off_software: bool = False, diagnostics: bool = False,
        hold_after_ready_s: float = 0.0, json_summary: bool = False,
        progress: bool = True, out=print, input_fn=input, now=None,
        **injections) -> int:
    """`vdeter diag run` — authorization·ledger 검사(장치 접근 전) → 정상 run과 같은
    `run_cmd.execute`(주입: 진단 loader·블록 검사·비대화형 계획·재시도 없음) → ledger 기록.

    `injections`는 시험용 seam(`preflight_fn`·`session_factory`·`usb_probe`·`repo_root`·
    `environment_manifest`·`topology_probe`·`clock`)을 `execute`에 그대로 넘긴다."""
    try:
        authorization = load_authorization(paths, now=now)
        plan = authorization["plan"]
        ledger = load_ledger(paths, authorization["_reference_sha256"])
        hold = float(hold_after_ready_s or 0.0)
        reasons = ledger_refusals(ledger, plan, scenarios=list(scenarios),
                                  poc_requested=bool(poc or poc_dir),
                                  hold_after_ready_s=hold)
        if reasons:
            raise DiagError(EXIT_CONFIG,
                            "diagnostic run refused by plan:\n  - "
                            + "\n  - ".join(reasons)
                            + "\nImpact: no device access. Action: adjust the "
                            "request or re-issue a new authorization.")
        # 실행할 조합을 **여기서 한 번** 확정해 ledger 기록과 execute 인자가 같은 값을 쓰게
        # 한다. 기록이 `scenarios`만 담으면 forged·PoC 조합을 실행하고도 남지 않는다.
        workloads = plan_workloads(plan, list(scenarios))
        # 계획이 이 run에 PoC(id)를 골랐는데 catalog가 없으면 조용히 baseline으로 돌면서
        # ledger에는 poc_id가 남아 기록과 실제가 어긋난다. `--poc`(direct)는 poc_id를 무시하므로
        # 같은 불일치다. 둘 다 **장치 접근 전** 거부한다.
        planned_pocs = sorted({p for _s, _c, p in workloads
                               if p is not None and p != "none"})
        if planned_pocs and not poc_dir:
            raise DiagError(EXIT_CONFIG,
                            "plan selects PoC %s for this run but no --poc-dir catalog was "
                            "given (plan poc_id is resolved from the catalog; --poc direct "
                            "mode ignores it). Impact: no device access. Action: pass "
                            "--poc-dir, or request only baseline workloads." % planned_pocs)
    except DiagError as exc:
        out("Refused (exit %d): %s" % (exc.code, exc))
        return exc.code

    run_id = unique_run_id(paths)
    before = _qualification_invariants(paths)
    entry = {"run_id": run_id, "started_utc": _utc_now(), "state": "STARTED",
             "scenarios": list(scenarios), "rx_only": not scenarios,
             "workloads": [{"scenario": s, "case": c, "poc_id": p}
                           for s, c, p in workloads],
             "hold_after_ready_s": hold, "poc": bool(poc or poc_dir),
             "rf_off_software": bool(rf_off_software),
             "invariants_before": before}
    ledger.setdefault("runs", []).append(entry)
    write_json_atomic(paths.diagnostic_ledger, ledger)

    out("[diag]      development-time controlled RF diagnostics — NOT_QUALIFIED "
        "(authorization %s, run %d/%d, workloads %d/%d%s)" % (
            authorization["_reference_sha256"][:12], len(ledger["runs"]),
            plan["max_runs"], len(scenarios), plan["max_workloads_per_run"],
            ", RX-only" if not scenarios else ""))
    exit_code = EXIT_INTERNAL
    try:
        exit_code = run_cmd.execute(
            paths, run_id=run_id,
            workload_plan=workloads,
            reference_loader=lambda _paths: authorization,
            critical_check=compare_authorization,
            retry=False, diagnostics=diagnostics, json_summary=json_summary,
            poc=poc, poc_dir=poc_dir, rf_off_software=rf_off_software,
            hold_after_ready_s=hold, progress=progress, out=out,
            input_fn=input_fn, **injections)
    finally:
        after = _qualification_invariants(paths)
        summary_path = paths.runs_dir / run_id / "summary.json"
        summary = None
        rx_only_assessment = None
        if summary_path.is_file():
            try:
                full = json.loads(summary_path.read_bytes())
                summary = {k: full.get(k) for k in (
                    "status", "scenarios_passed", "scenarios_run", "interrupted",
                    "qualification_status", "diagnostic_authorization_sha256")}
                summary["attempts"] = len(full.get("attempts") or [])
                summary["close_pass"] = (full.get("close") or {}).get("pass")
                if not scenarios:
                    rx_only_assessment = rx_only_close_assessment(full)
            except ValueError:
                summary = {"error": "summary.json unreadable"}
        # RX-only 보정은 **ledger 기록 전에** 확정한다 — 반환값·콘솔·ledger가 서로
        # 다른 종료코드를 말하면 그 자체가 증거 결함이다. coordinator의 rc·status·
        # error는 rx_only_assessment에 그대로 남는다(숨기지 않는다).
        if (rx_only_assessment is not None
                and rx_only_assessment["expected_empty_measurement"]
                and exit_code != EXIT_PASS):
            coordinator_exit_code = exit_code
            exit_code = EXIT_PASS
            rx_only_assessment["diagnostic_exit_code_override"] = {
                "from": coordinator_exit_code, "to": EXIT_PASS,
                "reason": ("empty measurement is the purpose of an RX-only stage; "
                           "the coordinator's fail-closed verdict is recorded as-is")}
        entry.update({"state": "FINISHED", "finished_utc": _utc_now(),
                      "exit_code": exit_code, "summary": summary,
                      "rx_only_assessment": rx_only_assessment,
                      "invariants_after": after,
                      "invariants_unchanged": before == after,
                      "authorization_still_valid": _authorization_unchanged(
                          paths, authorization["_reference_sha256"])})
        write_json_atomic(paths.diagnostic_ledger, ledger)
    if before != after:
        out("INVARIANT VIOLATION: qualification state changed during a diagnostic "
            "run (%s -> %s) — stop and investigate" % (before, after))
        return EXIT_INTERNAL
    if (rx_only_assessment or {}).get("diagnostic_exit_code_override"):
        # RX-only는 workload를 제출하지 않는 것이 목적이다. coordinator는 빈 측정을
        # 설계대로 fail-closed 판정하므로(_service_session_terminal_status), 그 사유
        # **하나만**이고 STOP·cleanup·fault·inhibit·topology가 모두 정상일 때에 한해
        # 진단 판정을 성공으로 본다. coordinator의 rc·status·error는 그대로 남긴다.
        out("  RX-only: coordinator reported rc=%s status=%s error=%r — this is the "
            "designed fail-closed verdict for an empty measurement (no workload was "
            "submitted, which is the purpose of this stage). STOP rc=0, cleanup PASS, "
            "residual workers 0, no faults, not inhibited."
            % (rx_only_assessment["coordinator_rc"],
               rx_only_assessment["coordinator_status"],
               rx_only_assessment["coordinator_error"]))
    out("DIAGNOSTIC RESULT exit=%d qualification=NOT_QUALIFIED run-id=%s ledger=%s"
        % (exit_code, run_id, paths.diagnostic_ledger))
    return exit_code


def _authorization_unchanged(paths: StatePaths, sha256: str) -> bool:
    try:
        return _sha256_bytes(paths.diagnostic_authorization.read_bytes()) == sha256
    except OSError:
        return False


def status(paths: StatePaths, *, out=print, now=None) -> int:
    """authorization·ledger 요약(읽기 전용)."""
    try:
        authorization = load_authorization(paths, now=now)
    except DiagError as exc:
        out("diagnostic authorization: none or invalid — %s" % exc)
        return EXIT_UNQUALIFIED
    plan = authorization["plan"]
    ledger = load_ledger(paths, authorization["_reference_sha256"])
    out("diagnostic authorization %s — NOT_QUALIFIED (issued %s, expires %s)" % (
        authorization["_reference_sha256"][:12], authorization.get("issued_utc"),
        plan["expires_utc"]))
    out("  bound installation : %s" % authorization["candidate"]["installation_sha256"][:12])
    out("  calibration source : %s (%s)" % (
        authorization["calibration_source"]["reference_sha256"][:12],
        authorization["calibration_source"]["qualification_run_id"]))
    out("  plan %s: scenarios=%s poc=%s max_runs=%d max_workloads_per_run=%d" % (
        plan["label"], plan["allowed_scenarios"], plan["poc_allowed"],
        plan["max_runs"], plan["max_workloads_per_run"]))
    runs = ledger.get("runs") or []
    out("  runs started %d/%d" % (len(runs), plan["max_runs"]))
    for entry in runs:
        summary = entry.get("summary") or {}
        out("    %s %s exit=%s status=%s scenarios=%s invariants_unchanged=%s" % (
            entry.get("run_id"), entry.get("state"), entry.get("exit_code"),
            summary.get("status"), entry.get("scenarios"),
            entry.get("invariants_unchanged")))
        rx = entry.get("rx_only_assessment")
        if rx:
            out("      RX-only: coordinator rc=%s status=%s reasons=%s → expected "
                "empty measurement=%s" % (
                    rx.get("coordinator_rc"), rx.get("coordinator_status"),
                    rx.get("failure_reasons"), rx.get("expected_empty_measurement")))
    return EXIT_PASS
