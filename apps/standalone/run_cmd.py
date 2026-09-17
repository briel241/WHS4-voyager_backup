"""`vdeter run` — 일상 실행 (Phase I W6 → operational readiness, README §1·§8·§10·§11).

여기서 W3~W5의 재료가 **실제로 연결**된다 (이관 조건):
    installation record 무결성 → readiness 실행 폐포 대조 → readiness state 적재 →
    qualification reference(있으면) 대조 → calibration source 선택(warm/cold) →
    stable binding → 준비 RF plan → 한 live session 안의 preparation → Link 17 probe →
    process-local READY_FOR_WORKLOAD → workload → close → readiness 승격/inhibit.

흐름 (결정 5·13·14·16·17·21, clone-bootstrap v2, operational_readiness_and_bringup_spec §5–§9)
    flock → hardware.toml → installation manifest 적재(무결성) → 3층 installation 검증(record
    integrity / readiness-bound closure / inventory-only) → readiness state(state.json) →
    active reference(v2, 선택) → identity → 필수 preflight → critical 대조(qualification 적용성) →
    calibration source(warm: 적용 가능한 artifact / cold: RR-P1 ADAPTIVE_SEARCH_AND_SEAL 자동 수행) →
    stable binding·applicability → run 디렉터리 → resolved config → coordinator 세션(bring-up 1회,
    warm 2700 s / cold 3900 s) → workload admission reserve(1600 s) → 내부 Link 17 양방향 probe →
    READY_FOR_WORKLOAD → 메뉴 또는 인자 workload N개 → close → 분류기 판정 → readiness 승격/inhibit
    → summary·RESULT 줄. Git 상태는 검사하지 않는다.

readiness 경로의 bring-up 자동 재시도는 **0회**다(warm/cold 모두). 이전 결정 16의 host-time 단일
재시도(`classify_bringup_failure`)는 이 경로에서 호출하지 않으며 실패 설명 줄에만 그 분류를 쓴다.
실패·timeout·취소·signal은 bounded cleanup·잔존 0·B210 3대 재발견 뒤 사용자가 새 `vdeter run`을
시작해야 한다.

`workload_plan`(개발용 진단 `diag_cmd`)이나 `critical_check`(진단 authorization)이 주어진 실행은
readiness를 발급·표시하지 않는다(`NOT_QUALIFIED` 진단 권한은 readiness/qualification으로 승격 금지).
`--rf-off-software`(장치 없는 software double)도 readiness를 발급하지 않는다.

출력은 전부 영어. 시나리오 구간은 RF 활성 중에는 `running …`만 내고, workload 결과가 확정된 뒤
evidence를 한 번 읽어 전송 단위 세로 상세 블록(scenario_report)과 result 요약을 낸다. 종료 코드는
README §11.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from apps.standalone import (
    hardware_profiles,
    identity,
    installation_manifest,
    local_config,
    progress as progress_module,
    provenance as provenance_module,
    qualify_cmd,
    readiness,
    readiness_classifier,
    readiness_closure,
    readiness_probe,
    readiness_run,
    scenario_report,
)
from apps.standalone.local_session import (
    LocalCoordinatorSession,
    LocalSessionError,
    build_child_env,
)
from apps.standalone.state import (
    SingleInstanceLock,
    StateError,
    StatePaths,
    allocate_run_dir,
    check_disk_space,
    write_json_atomic,
)

# 배치 계획은 host 계층과 단일 출처 (읽기 전용 재사용).
from apps.service_layer import DEFAULT_BATCH_PLAN  # noqa: E402

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_HARDWARE = 3
EXIT_UNQUALIFIED = 4
EXIT_INTERNAL = 5
EXIT_INTERRUPTED = 130

SCENARIOS = {
    "SC-VDE-001": "X.509 certificate delivery",
    "SC-VDE-005": "S-421 route plan exchange",
    "SC-VDE-006": "MASS-ROC remote control",
}

#: (legacy 결정 16) bring-up 시도 상한 — readiness 경로는 항상 1회다. 실패 설명 줄의 표기에만 쓴다.
MAX_BRINGUP_ATTEMPTS = 2
#: readiness 경로의 bring-up 시도 수 (warm/cold 공통, 명세 §5-7).
READINESS_BRINGUP_ATTEMPTS = 1

#: 일반 실행이 결속하는 서비스 phase (README §6 — coordinator 계약).
RUN_RR_PHASE = "rr-p2-17"
WARM_SESSION_PHASE = "mass-roc-session"
COLD_SESSION_PHASE = "mass-roc-session-cold"

PROBE_SCENARIO_ID = readiness_probe.PROBE_SCENARIO_ID


class RunRefusal(Exception):
    """RF 접근 전 거부 — exit code와 조치 가능한 메시지를 담는다."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class BringupFailed(Exception):
    def __init__(self, summary: dict, error: str, *, run_root=None,
                 phase_label: str | None = None) -> None:
        super().__init__(error)
        self.summary = summary
        self.error = error
        self.run_root = run_root
        self.phase_label = phase_label


class BringupInterrupted(Exception):
    """bring-up 중 Ctrl+C/SIGTERM — 정상 close를 마친 뒤 올라온다 (재시도 없음).

    summary는 finalize의 정직 판정(cleanup·잔존·B210 반환), run_root는
    종료 evidence를 기록할 위치다."""

    def __init__(self, summary: dict, run_id: str, run_root: Path) -> None:
        super().__init__("bring-up interrupted")
        self.summary = summary
        self.run_id = run_id
        self.run_root = run_root


# ------------------------------------------------------------ 출력 보조

def stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "[%2d:%02d]" % (seconds // 60, seconds % 60)


#: coordinator 콘솔의 한국어 단계 문구 → 영어 (표시 전용; coordinator 무수정).
_STAGE_KO_EN = (("연결됨", "connected"),)


def english_stage_line(line: str) -> str:
    """bring-up 단계 줄을 영어로 — coordinator가 내는 `MASS-A 연결됨` 같은
    한국어 조각만 바꾸고 나머지는 그대로 둔다."""
    for ko, en in _STAGE_KO_EN:
        line = line.replace(ko, en)
    return line


def render_trace_event(row: dict, elapsed_s: float):
    """workload trace 이벤트 한 줄 (영어, workload 기준 경과). 없으면 None."""
    event = row.get("event")
    prefix = "  " + stamp(elapsed_s) + " "
    if event == "submit":
        sender = row.get("sender") or "?"
        receiver = row.get("receiver") or "?"
        step = row.get("step") or ""
        return (prefix + "%-16s submit    %s  %s B%s"
                % (sender + " -> " + receiver,
                   row.get("payload_format") or "?",
                   row.get("payload_len"),
                   ("  [" + step + "]") if step else ""))
    if event == "transfer_terminal":
        return prefix + "%-16s transfer  %s" % (
            "", row.get("terminal_state") or "?")
    if event == "data_received":
        receiver = row.get("receiver") or "?"
        if not row.get("present", True):
            return prefix + "%-16s received  (none)" % receiver
        return prefix + "%-16s received  %s -> %s" % (
            receiver, row.get("original_source"),
            row.get("original_destination"))
    if event == "scenario_result":
        steps = row.get("steps") or []
        failed = [name for name, status in steps if status != "PASS"]
        tail = ("  failed: " + ", ".join(failed)) if failed else ""
        return prefix + "%-16s verdict   %s (%d steps)%s" % (
            row.get("scenario") or "", row.get("status"), len(steps), tail)
    return None


#: `result: PASS · 19.2 s · quiescence OK` — results_cmd와 공유 (scenario_report).
result_line = scenario_report.result_line


# ------------------------------------------------------------ 게이트

_DOMAIN_ACTIONS = {
    "rf": "RF files changed after qualify -> run `vdeter qualify` (RF ladder)",
    "operation": "operation files changed after qualify -> run `vdeter "
                 "qualify` (current policy: no automatic reuse)",
    "tooling": "qualification tooling/test files changed after qualify -> "
               "re-run the software verification bundle (evidence reuse) "
               "before running",
}


def compare_critical(reference: dict, *, installation: dict,
                     hardware: dict, preflight: dict,
                     identity_fingerprint: str,
                     environment_manifest: dict) -> dict:
    """현재 qualification-critical 값 ↔ active reference(v2) 대조 (README §8).

    `installation`은 현재 설치 재측정 record(또는 digest 부분집합). Git
    commit·branch·dirty·baseline Markdown SHA는 대조하지 않는다.
    불일치 항목을 전부 모아 돌려준다 — 하나라도 있으면 qualification STALE
    (readiness 경로) / UNQUALIFIED(진단·legacy 경로)."""
    mismatches = []
    if reference.get("schema_version") != 2:
        mismatches.append("active reference is schema v%s (legacy git-bound "
                          "binding); a v2 installation-bound reference is "
                          "required — run `vdeter qualify`"
                          % reference.get("schema_version"))
    mismatches.extend(critical_block_mismatches(
        reference.get("standalone_critical") or {},
        installation=installation, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_fingerprint,
        environment_manifest=environment_manifest))
    return {"pass": not mismatches, "mismatches": mismatches}


def critical_block_mismatches(critical: dict, *, installation: dict,
                              hardware: dict, preflight: dict,
                              identity_fingerprint: str,
                              environment_manifest: dict) -> list:
    """`standalone_critical` 블록 하나를 현재 설치·장비·identity·환경과 대조한다.

    `compare_critical`(정상 reference)과 개발용 진단 authorization(`diag_cmd`)이
    **같은 검사**를 공유한다. 진단은 자격 상태(active 포인터·schema v2 reference)
    만 분리하고, installation/rf/operation/tooling digest·배선 승인·platform/host
    profile·hub topology·serial/속도 등급·identity·라이브러리 4종·gain artifact
    결속 검사는 그대로 받는다. 반환은 불일치 문자열 목록(비면 통과)."""
    mismatches = []
    current_digests = installation_manifest.digests_of(installation) \
        if installation.get("domains") else {}
    if critical.get("installation_sha256") != current_digests.get(
            "installation_sha256"):
        changed = [d for d in ("rf", "operation", "tooling")
                   if critical.get(f"{d}_manifest_sha256")
                   != current_digests.get(f"{d}_manifest_sha256")]
        if not changed:
            mismatches.append("installation digest differs from the "
                              "qualified installation")
        for domain in changed:
            mismatches.append("%s manifest SHA differs: %s"
                              % (domain, _DOMAIN_ACTIONS[domain]))
    wiring = (critical.get("wiring") or {})
    if wiring.get("profile") != (hardware.get("wiring") or {}).get("profile"):
        mismatches.append("wiring profile %r qualified, %r declared"
                          % (wiring.get("profile"),
                             (hardware.get("wiring") or {}).get("profile")))
    if (hardware.get("wiring") or {}).get("approved") is not True:
        mismatches.append("hardware.toml wiring.approved is not true")
    if critical.get("platform_profile") != hardware.get("platform_profile"):
        mismatches.append("platform_profile differs (%r qualified, %r "
                          "declared)" % (critical.get("platform_profile"),
                                         hardware.get("platform_profile")))
    # host profile + USB hub topology (2026-09-10 additive). A reference
    # without `host_profile` is a legacy Pi reference. A powered-hub
    # declaration is UNQUALIFIED against any reference that does not carry
    # the same hub asset/VID:PID, role→serial/speed_class/transport and power
    # provenance, and it requires the preflight shared-hub check to pass.
    declared_profile = hardware_profiles.profile_of(hardware)
    qualified_profile = (critical.get("host_profile")
                         or hardware_profiles.LEGACY_PI_PROFILE)
    if qualified_profile != declared_profile:
        mismatches.append("host_profile %r qualified, %r declared — re-qualify "
                          "on this host profile"
                          % (qualified_profile, declared_profile))
    elif declared_profile == hardware_profiles.PI_POWERED_HUB_PROFILE:
        expected_topology = critical.get("usb_topology")
        if not isinstance(expected_topology, dict) or not expected_topology:
            mismatches.append("reference carries no USB hub topology binding "
                              "(usb_topology) — run `vdeter qualify` on this "
                              "powered-hub installation")
        else:
            topology_check = (preflight.get("checks") or {}).get(
                "usb_topology") or {}
            if topology_check.get("pass") is not True:
                mismatches.append("USB hub topology preflight did not pass: "
                                  + "; ".join((topology_check.get("errors")
                                               or ["not checked"])[:3]))
            mismatches.extend(hardware_profiles.topology_mismatches(
                expected_topology, hardware_profiles.critical_topology(hardware)))
    observed = (preflight.get("checks") or {}).get("b210", {}).get(
        "observed_by_role") or {}
    for role, expected in sorted((critical.get("endpoints") or {}).items()):
        seen = observed.get(role)
        if seen is None:
            mismatches.append(f"{role}: device not observed")
            continue
        if seen.get("serial") != expected.get("serial"):
            mismatches.append("%s: serial %s qualified, %s observed"
                              % (role, expected.get("serial"),
                                 seen.get("serial")))
        if seen.get("speed_class") != expected.get("speed_class"):
            mismatches.append("%s: USB speed class %s qualified, %s observed"
                              % (role, expected.get("speed_class"),
                                 seen.get("speed_class")))
    if critical.get("identity_fingerprint") != identity_fingerprint:
        mismatches.append("identity fingerprint differs from the qualified "
                          "identity (new identity requires re-qualification)")
    qualified_versions = critical.get("library_versions") or {}
    for name in local_config.CRITICAL_LIBRARIES:
        current = environment_manifest.get(f"{name}_version")
        if qualified_versions.get(name) != current:
            mismatches.append("%s version %r qualified, %r installed"
                              % (name, qualified_versions.get(name), current))
    gain = critical.get("gain") or {}
    if not gain.get("endpoint_common_rx_gain_db") or not gain.get(
            "artifact_sha256"):
        mismatches.append("reference carries no gain artifact")
    return mismatches


def gain_input_from_reference(reference: dict) -> dict:
    """coordinator `--gain-artifact` 입력 (gain_artifact_input.json 스키마)."""
    gain = reference["standalone_critical"]["gain"]
    return {
        "endpoint_common_rx_gain_db": gain["endpoint_common_rx_gain_db"],
        "source_gain_artifact_sha256": gain["artifact_sha256"],
    }


def is_diagnostic_authority(reference: dict) -> bool:
    """개발용 진단 authorization인가 (`diag_cmd`, `not_a_qualification`)."""
    return bool(reference and reference.get("not_a_qualification"))


def authority_fields(reference: dict | None, *, readiness_block: dict | None = None) -> dict:
    """run.json/summary.json의 권한 표기.

    정상 run은 종전과 같이 `reference_sha256`(있으면)을 기록한다. 진단 authorization은
    qualification reference로 기록하지 않는다 — `reference_sha256`은 null이고,
    `diagnostic_authorization_sha256`과 `qualification_status: NOT_QUALIFIED`를
    별도 필드로 남긴다(재결속 위장 금지). readiness 경로는 `current_authority`·
    `qualification_status`를 함께 적는다."""
    if is_diagnostic_authority(reference):
        fields = {"reference_sha256": None,
                  "diagnostic_authorization_sha256":
                      reference.get("_reference_sha256"),
                  "qualification_status": "NOT_QUALIFIED"}
    else:
        fields = {"reference_sha256": (reference or {}).get("_reference_sha256")}
    if readiness_block is not None:
        fields["current_authority"] = readiness_block.get("current_authority")
        fields["qualification_status"] = (
            "QUALIFIED" if readiness_block.get("qualification") == "APPLICABLE"
            else "NOT_QUALIFIED")
        fields["operational_readiness_before"] = readiness_block.get("operational_readiness")
    return fields


def result_suffix(reference: dict | None, readiness_block: dict | None = None) -> str:
    """비대화형 `RESULT` 줄 접미 — 진단이면 자격 상태를, readiness 경로면 권위·발급 상태를 명시한다."""
    if is_diagnostic_authority(reference):
        return " qualification=NOT_QUALIFIED"
    if readiness_block is None:
        return ""
    return " authority=%s readiness=%s qualification=%s" % (
        readiness_block.get("current_authority"),
        readiness_block.get("issue_status"),
        "QUALIFIED" if readiness_block.get("qualification") == "APPLICABLE" else "NOT_QUALIFIED")


#: (legacy 결정 16) bring-up 실패 설명에 쓰는 신호 — readiness 경로는 이 분류로 **재시도하지 않는다**.
_NON_TRANSIENT_MARKERS = (
    "BINDING_REJECTED", "config 적재 실패", "system plan 적재 실패",
    "spawn 실패", "spawn failed", "trust", "identity", "fingerprint",
    "REQUIRED_UNSET",
    "DEVICE_ERROR", "No devices found", "USB",
)
_HOST_TIME_MARKERS = ("host affine fit FAIL", "acquire_host_time")


def classify_bringup_failure(console_text: str, finalize_summary: dict,
                             usb_observed: dict, declared_serials) -> dict:
    """(legacy 결정 16) 재시도 허용 분류 — 보수적이다. readiness 경로에서는 **설명 전용**이며
    어떤 값이어도 재시도하지 않는다.

    transient=True 조건 전부: host-time 획득 실패 신호가 있고, 비일시적
    신호가 없고, closed.json의 cleanup PASS, 잔존 프로세스 0, 선언된 B210
    serial이 전부 USB에서 재발견됨."""
    reasons = []
    text = console_text or ""
    if not any(marker in text for marker in _HOST_TIME_MARKERS):
        reasons.append("no host-time acquisition failure signature")
    hits = [m for m in _NON_TRANSIENT_MARKERS if m in text]
    if hits:
        reasons.append("non-transient signature present: %s" % hits[:3])
    closed = (finalize_summary or {}).get("closed") or {}
    if (closed.get("cleanup") or {}).get("pass") is not True:
        reasons.append("cleanup not confirmed PASS")
    residual = (finalize_summary or {}).get("residual_processes")
    if residual is None or residual:
        reasons.append("residual processes not confirmed 0")
    missing = [s for s in declared_serials if s not in (usb_observed or {})]
    if missing:
        reasons.append("B210 not rediscovered: %s" % missing)
    return {"transient_host_time": not reasons, "reasons": reasons}


# ------------------------------------------------------------ 준비 단계

def _refuse_readiness_state(snapshot: dict) -> RunRefusal:
    status = snapshot["status"]
    problems = "; ".join(snapshot.get("problems") or []) or status
    if status == "MIGRATION_REQUIRED":
        return RunRefusal(
            EXIT_UNQUALIFIED,
            "MIGRATION_REQUIRED: this installation has no readiness state marker. "
            "Impact: workload RF is blocked until the RF-free migration is done. "
            "Action: run `./bin/vdeter setup` (RF-free; creates var/readiness/state.json), "
            "then run again.")
    if status == "STATE_UNREADABLE":
        return RunRefusal(
            EXIT_UNQUALIFIED,
            "INHIBITED / STATE_UNREADABLE: var/readiness/state.json is required by the "
            "installation manifest but cannot be read (%s). Impact: RF is refused "
            "(fail-closed; a deleted state is not a fresh install). Action: restore "
            "var/readiness/state.json from the installation, or investigate before "
            "any RF." % problems)
    if status == "STATE_CORRUPT":
        return RunRefusal(
            EXIT_UNQUALIFIED,
            "INHIBITED / STATE_CORRUPT: readiness state or its inhibit record is invalid "
            "(%s). Impact: RF is refused. Action: investigate var/readiness; the state is "
            "never regenerated silently." % problems)
    return RunRefusal(
        EXIT_UNQUALIFIED,
        "READINESS ORPHAN_INVALID: an unreferenced readiness release is incomplete or "
        "tampered (%s). Impact: RF is refused before any device access. Action: inspect "
        "var/readiness/releases and remove or restore the invalid directory manually."
        % problems)


def prepare(paths: StatePaths, *, repo_root=None,
            hardware_path=None, preflight_fn=None, reference_loader=None,
            environment_manifest=None, progress=None,
            critical_check=None, readiness_enabled: bool = True,
            machine_id=None, readiness_sleeper=None) -> dict:
    """RF 접근 전 게이트 전부. 실패는 RunRefusal(code, message).

    Git·baseline Markdown·standalone-binding은 읽지 않는다.

    readiness 경로(`readiness_enabled=True`, 정상 `vdeter run`): installation record 무결성 →
    3층 installation 검증(bound 불일치 = 거부, inventory-only = 기록) → readiness state
    (fail-closed 상태 거부) → active qualification reference(선택) → identity → preflight →
    qualification 적용성(compare_critical; 불일치 = STALE, 거부 아님) → calibration source →
    stable binding·applicability·current authority.

    진단/legacy 경로(`readiness_enabled=False`, `diag_cmd`·`--rf-off-software`): 종전 그대로
    active reference 필수 + `critical_check` 통과 + 전체 installation 일치.

    `reference_loader`/`critical_check`는 주입 seam이다."""
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    paths.ensure_var()

    def stage(name, **fields):
        if progress is not None:
            progress.gate(name, **fields)

    try:
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))
    except local_config.ConfigGenerationError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc

    stage("installation manifest")
    try:
        installation = installation_manifest.load_installation(paths)
    except installation_manifest.InstallationError as exc:
        raise RunRefusal(EXIT_UNQUALIFIED,
                         "Installation manifest unavailable or tampered: %s. "
                         "Action: run `./bin/vdeter setup`, then `vdeter "
                         "qualify`." % exc) from exc
    closure_report = None
    if readiness_enabled:
        closure_report = readiness_closure.verify_readiness_installation(
            paths, repo_root, record=installation)
        bound = closure_report["readiness_bound"]
        if not bound["pass"]:
            raise RunRefusal(
                EXIT_UNQUALIFIED,
                "READINESS-BOUND FILES differ from the setup manifest (%s). Impact: the "
                "recorded installation binding no longer describes the rf/operation tree "
                "and no preparation RF is started. Action: restore the files, or run "
                "`./bin/vdeter setup` (RF-free) to re-issue the installation manifest and "
                "run again (a new bounded preparation follows)."
                % readiness_closure.describe_bound_mismatch(closure_report))
        current_installation = closure_report["current_record"]
    else:
        install_check = installation_manifest.verify_installation(repo_root, installation)
        if not install_check["pass"]:
            raise RunRefusal(
                EXIT_UNQUALIFIED,
                "Installation files differ from the setup manifest (%s). Impact: "
                "the recorded installation binding no longer describes this "
                "tree. Action: restore the files, or run `./bin/vdeter setup` "
                "and `vdeter qualify` again."
                % installation_manifest.describe_mismatch(install_check))
        current_installation = install_check["current_record"]

    readiness_snapshot = None
    if readiness_enabled:
        stage("readiness state")
        readiness_snapshot = readiness.load_readiness_state(
            paths, installation_record=installation,
            **({"sleeper": readiness_sleeper} if readiness_sleeper is not None else {}))
        if readiness_snapshot["fail_closed"]:
            raise _refuse_readiness_state(readiness_snapshot)

    stage("active reference")
    loader = reference_loader or qualify_cmd.load_active_reference
    reference = loader(paths)
    if reference is None and not readiness_enabled:
        raise RunRefusal(
            EXIT_UNQUALIFIED,
            "UNQUALIFIED: no valid active qualification reference on this "
            "machine. Impact: RF execution is refused. Action: run "
            "`vdeter qualify` (RR ladder, several hours).")

    stage("identity")
    identity_check = identity.check_identity_consistency(paths)
    if not identity_check["pass"]:
        raise RunRefusal(EXIT_CONFIG,
                         "Identity material is inconsistent: "
                         + "; ".join(identity_check["errors"])
                         + ". Action: `vdeter setup --new-identity` then "
                         "`vdeter qualify`.")

    stage("preflight / B210 discovery")
    if preflight_fn is None:
        from apps.standalone.local_preflight import run_required_preflight

        def preflight_fn():
            return run_required_preflight(hardware=hardware, paths=paths)
    preflight = preflight_fn()
    if not preflight["pass"]:
        checks = preflight.get("checks") or {}
        b210_failed = not (checks.get("b210") or {}).get("pass", True)
        key_failed = (not (checks.get("private_key") or {}).get("pass", True)
                      or checks.get("key_cert_match") in (False, None))
        code = (EXIT_HARDWARE if b210_failed
                else EXIT_CONFIG if key_failed else EXIT_INTERNAL)
        raise RunRefusal(code, "Preflight failed: "
                         + "; ".join(preflight["failures"]))

    stage("critical comparison")
    environment_manifest = (environment_manifest
                            or local_config.build_environment_manifest())
    comparison = {"pass": False, "mismatches": ["no active qualification reference"]}
    if reference is not None:
        comparison = (critical_check or compare_critical)(
            reference, installation=current_installation, hardware=hardware,
            preflight=preflight,
            identity_fingerprint=identity_check["fingerprint"],
            environment_manifest=environment_manifest)
    if not readiness_enabled and not comparison["pass"]:
        if is_diagnostic_authority(reference):
            raise RunRefusal(
                EXIT_UNQUALIFIED,
                "DIAGNOSTIC AUTHORIZATION STALE: current values differ from "
                "the diagnostic authorization:\n  - "
                + "\n  - ".join(comparison["mismatches"])
                + "\nImpact: RF execution is refused. Action: re-issue with "
                "`vdeter diag authorize` after verifying the candidate.")
        raise RunRefusal(
            EXIT_UNQUALIFIED,
            "UNQUALIFIED: current qualification-critical values differ from "
            "the active reference:\n  - "
            + "\n  - ".join(comparison["mismatches"])
            + "\nImpact: RF execution is refused. Action: restore the "
            "qualified state or run `vdeter qualify` again.")

    disk = check_disk_space(paths.var_dir)
    if disk["status"] == "refuse":
        raise RunRefusal(EXIT_INTERNAL,
                         "Insufficient free disk space for a session "
                         f"({disk['free_bytes']} bytes). Action: move or "
                         "remove old evidence manually.")

    context = {
        "repo_root": repo_root, "hardware": hardware,
        "installation": current_installation,
        "installation_record": installation,
        "installation_sha256": current_installation["installation_sha256"],
        "installation_check": ({k: v for k, v in closure_report.items() if k != "current_record"}
                               if closure_report is not None else None),
        "provenance": provenance_module.build_provenance(repo_root),
        "reference": reference,
        "qualification": {"present": reference is not None,
                          "applicable": bool(reference is not None and comparison["pass"]),
                          "mismatches": list(comparison.get("mismatches") or [])},
        "preflight": preflight, "identity": identity_check,
        "environment_manifest": environment_manifest, "disk": disk,
        "declared_serials": [
            hardware["endpoints"][role]["serial"]
            for role in ("MASS-A", "MASS-B", "ROC")],
        "host_profile": hardware_profiles.profile_of(hardware),
        # observed sysfs topology path per role — provisions the powered-hub
        # templates' usb_physical_path (recorded, not a binding value).
        "usb_paths": {
            role: (info or {}).get("usb_path")
            for role, info in sorted((((preflight.get("checks") or {}).get(
                "b210") or {}).get("observed_by_role") or {}).items())},
        "readiness_enabled": readiness_enabled,
        "readiness_snapshot": readiness_snapshot,
    }
    if not readiness_enabled:
        return context

    # --- readiness: calibration source → stable binding → applicability → authority ----
    stage("readiness binding")
    readiness_id = readiness.new_readiness_id()
    if machine_id is None:
        machine_id = readiness_run.read_machine_id()
    if not machine_id:
        raise RunRefusal(EXIT_CONFIG,
                         "cannot read the stable machine identity (/etc/machine-id) required "
                         "by the readiness binding; no preparation RF is started.")
    placeholder = readiness_run.cold_calibration_placeholder(readiness_id)
    partial = readiness_run.build_binding(
        installation_record=installation, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_check["fingerprint"], machine_id=machine_id,
        repo_root=repo_root, calibration=placeholder)
    selection = readiness_run.select_calibration_source(
        reference=reference if not is_diagnostic_authority(reference) else None,
        qualification_applicable=context["qualification"]["applicable"],
        readiness_snapshot=readiness_snapshot, hardware=hardware, preflight=preflight,
        environment_manifest=environment_manifest, installation_record=installation,
        partial_binding=readiness_run.binding_without_calibration(partial))
    calibration = selection["calibration"] or placeholder
    binding = dict(partial)
    binding["calibration"] = calibration
    applicability = readiness.evaluate_applicability(readiness_snapshot, binding)
    authority = readiness.project_authority(
        qualification_applicable=context["qualification"]["applicable"],
        readiness_applicable=applicability["operational_readiness"] == "APPLICABLE")
    active = (readiness_snapshot.get("active") or {})
    active_digest = ((active.get("record") or {}).get("readiness_digest_sha256")
                     if active.get("valid") else None)
    context.update({
        "readiness_id": readiness_id,
        "machine_id": machine_id,
        "binding": binding,
        "calibration": selection,
        "applicability": applicability,
        "current_authority": authority,
        "active_same_binding": bool(active_digest and active_digest == readiness.readiness_digest(binding)),
        "expected_generation": readiness_snapshot["generation"],
    })
    return context


# ------------------------------------------------------------ 세션 실행

def _topology_after_close(hardware: dict, topology_probe=None) -> dict:
    """powered-hub profile: close 뒤 sysfs 재관측 → 세 B210이 승인 hub 아래
    5000M로 돌아왔는지 (기록·판정, 재시도 없음)."""
    host = hardware_profiles.host_profiles
    probe = topology_probe or host.probe_usb_topology
    declared = {role: ((hardware.get("endpoints") or {}).get(role) or {}).get("serial")
                for role in hardware_profiles.ROLES}
    try:
        check = host.check_shared_powered_hub(
            declared, probe(), hub_vid_pid=(hardware.get("usb_hub") or {}).get("vid_pid"))
    except Exception as exc:  # noqa: BLE001 - 관측 실패도 정직 기록 (fail-closed)
        return {"pass": False, "errors": [
            "topology probe failed: %s: %s" % (type(exc).__name__, exc)],
            "hub": None, "observed_by_role": {}}
    return {"pass": check["pass"], "errors": check["errors"],
            "hub": check.get("hub"), "observed_by_role": check["observed_by_role"]}


def topology_after_line(topology_after: dict) -> str:
    if topology_after["pass"]:
        parts = ["%s %s@%s %sM" % (role, info.get("serial"), info.get("usb_path"),
                                    info.get("speed_mbps"))
                 for role, info in sorted(
                     (topology_after.get("observed_by_role") or {}).items()) if info]
        return ("[topology]  after close: B210s rediscovered below the approved hub "
                "%s (%s)" % ((topology_after.get("hub") or {}).get("usb_path"),
                             ", ".join(parts)))
    return ("[topology]  after close: PROBLEMS — "
            + "; ".join((topology_after.get("errors") or ["unknown"])[:3]))


def close_notes(summary: dict) -> list:
    """close 요약의 보충 설명 — 판정은 바꾸지 않는다. coordinator는 workload가
    하나라도 FAIL이면 스스로 FAIL 판정·rc=1로 끝나므로(설계), 그 경우 정리
    (cleanup)가 통과했다는 사실을 따로 밝힌다 (실측 run-20260905-044555)."""
    notes = []
    closed = summary.get("closed") or {}
    cleanup = closed.get("cleanup") or {}
    rc = summary.get("coordinator_rc")
    error = str(closed.get("error") or "")
    if rc not in (0, None) and "workload" in error.lower():
        notes.append("coordinator verdict FAIL because a workload failed (rc=%s "
                     "is the designed exit for that; not a crash)" % rc)
    if cleanup:
        rediscovered = cleanup.get("rediscovery") or {}
        notes.append("cleanup %s — launcher clean exit %s, B210 rediscovered %d/%d, "
                     "workers after stop %s" % (
                         "PASS" if cleanup.get("pass") else "FAIL",
                         _plain_bool(cleanup.get("launcher_clean_exit")),
                         sum(1 for v in rediscovered.values() if v),
                         len(rediscovered), cleanup.get("workers_after_stop")))
    return notes


def _plain_bool(value) -> str:
    return "true" if value is True else "false" if value is False else "-"


def _close_with_interrupt_guard(session, out) -> dict:
    """첫 Ctrl+C는 정상 close, close 중 두 번째 Ctrl+C는 즉시 에스컬레이션
    (결정 17). 어느 경로든 finalize의 정직 판정을 돌려준다."""
    try:
        return session.close()
    except KeyboardInterrupt:
        out("Second interrupt — escalating immediately (TERM/KILL group)")
        session.escalate()
        return session.finalize()


def _readiness_mode(context: dict) -> str:
    if not context.get("readiness_enabled"):
        return "legacy"
    return context["calibration"]["mode"]


def _bring_up(context: dict, paths: StatePaths, *, run_id, session_factory,
              diagnostics, out, clock, reporter=None, mirror=None,
              rf_off_software: bool = False) -> tuple:
    """run 디렉터리 할당 → config 생성 → 세션 spawn → READY 대기.

    warm: OPERATIONAL config + 적용 가능한 gain artifact, phase `mass-roc-session`, 상한 2700 s.
    cold: GAIN_CALIBRATION config(gain 없음), phase `mass-roc-session-cold`, cold lifecycle 3900 s
          (`wait_ready(timeout_s=3900)` 명시 호출 — 기본 인자 2700은 바꾸지 않는다).
    실패는 BringupFailed(summary) — summary는 finalize 결과(정직).
    `reporter`가 있으면 bring-up 진행바(BringupTracker)를 구동한다 — 표시 전용."""
    try:
        run_id, run_root = allocate_run_dir(paths.runs_dir, run_id)
    except StateError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc
    presentation = run_root / "presentation"
    if reporter is not None:
        reporter.set_evidence_log(presentation / "progress.log")
    if mirror is not None:
        mirror.set_path(presentation / "console.log")
    reference = context["reference"]
    mode = _readiness_mode(context)
    if mode == "cold":
        run_mode, gain_input, phase = "GAIN_CALIBRATION", None, COLD_SESSION_PHASE
        timeout_s = readiness_run.READINESS_COLD_PREPARATION_S
    else:
        run_mode, phase = "OPERATIONAL", WARM_SESSION_PHASE
        timeout_s = None
        if mode == "warm":
            gain_input = readiness_run.gain_input_for(context["calibration"]["calibration"])
        else:
            gain_input = gain_input_from_reference(reference)
    try:
        generated = local_config.generate_run_configs(
            repo_root=context["repo_root"], run_root=run_root, run_id=run_id,
            rr_phase=RUN_RR_PHASE, hardware=context["hardware"],
            installation=context["installation"],
            installation_verified=True,
            provenance=context.get("provenance"),
            run_mode=run_mode,
            gain_input=gain_input,
            environment_manifest=context["environment_manifest"],
            usb_paths=context.get("usb_paths"))
    except local_config.ConfigGenerationError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc
    gain_path = None
    if gain_input is not None:
        gain_path = Path(generated["configs_dir"]) / "gain_artifact_input.json"
        write_json_atomic(gain_path, gain_input)
    digests = installation_manifest.digests_of(context["installation"])
    readiness_block = None
    if context.get("readiness_enabled"):
        readiness_block = {
            "current_authority": context["current_authority"],
            "operational_readiness": context["applicability"]["operational_readiness"],
            "qualification": "APPLICABLE" if context["qualification"]["applicable"]
            else ("STALE" if context["qualification"]["present"] else "ABSENT"),
        }
        plan = readiness_run.readiness_plan(
            mode, readiness_id=context["readiness_id"], run_id=run_id,
            calibration=context["calibration"]["calibration"],
            preparation_cap_s=timeout_s or readiness_run.WARM_BRINGUP_TIMEOUT_S)
        (run_root / "plans").mkdir(parents=True, exist_ok=True)
        (run_root / "plans" / "readiness-plan.json").write_bytes(readiness_run.plan_bytes(plan))
        (run_root / "readiness").mkdir(parents=True, exist_ok=True)
        write_json_atomic(run_root / "readiness" / "closure-report.json", {
            "artifact_type": "VDETER_READINESS_INSTALLATION_CHECK_V1",
            **(context.get("installation_check") or {})})
        write_json_atomic(run_root / "readiness" / "preparation.json", {
            "artifact_type": "VDETER_READINESS_PREPARATION_CONTEXT_V1",
            "readiness_id": context["readiness_id"],
            "mode": mode,
            "authority_scope": "RUN_READINESS_PREPARATION",
            "qualification_status": "NOT_QUALIFIED",
            "binding": context["binding"],
            "readiness_digest_sha256": (
                readiness.readiness_digest(context["binding"])
                if mode != "cold" else None),
            "calibration_selection": {k: v for k, v in context["calibration"].items()},
            "applicability": context["applicability"],
            "current_authority": context["current_authority"],
            "qualification": context["qualification"],
            "readiness_state": {
                "status": context["readiness_snapshot"]["status"],
                "generation": context["readiness_snapshot"]["generation"],
                "active_release": ((context["readiness_snapshot"].get("active") or {}).get("release")),
                "orphans": context["readiness_snapshot"]["orphans"],
            },
            "session_reuse_scope": "THIS_RUN_ONLY",
            "cross_command_handoff_allowed": False,
        })
    write_json_atomic(run_root / "run.json", {
        "artifact_type": "STAGE7_STANDALONE_RUN_V1",
        "run_id": run_id,
        **authority_fields(reference, readiness_block=readiness_block),
        "installation_sha256": digests["installation_sha256"],
        "rf_manifest_sha256": digests["rf_manifest_sha256"],
        "operation_manifest_sha256": digests["operation_manifest_sha256"],
        "tooling_manifest_sha256": digests["tooling_manifest_sha256"],
        "identity_fingerprint": context["identity"]["fingerprint"],
        "requested_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_hashes": generated["resolved_sha256"],
        "readiness_mode": mode,
        "readiness_id": context.get("readiness_id"),
        "session_phase": phase,
        # 이 실행에 **실제 적용된** 진단 설정. 요청값이 아니라 자식 env 를 만드는
        # 단일 출처(build_child_env)가 STAGE7_DIAG 를 넣었는지 읽는다.
        "diagnostics": build_child_env(
            diagnostics=diagnostics).get("STAGE7_DIAG") == "1",
    })

    session = session_factory(
        repo_root=context["repo_root"], run_id=run_id, run_root=run_root,
        configs_dir=generated["configs_dir"], gain_artifact_path=gain_path,
        baseline_sha256=provenance_module.STAGE7_BASELINE_PIN["sha256"],
        phase=phase, diagnostics=diagnostics,
        identity_root=paths.identity_dir, rf_off_software=rf_off_software)
    out(f"[config]    {run_id} generated and validated")
    if mode == "cold":
        out("Bringing up hardware session — COLD calibration path (CFO -> host fit -> "
            "ADAPTIVE_SEARCH_AND_SEAL -> fresh host fit -> RF-time PRE/POST); hard cap "
            "%d s, no automatic retry" % int(timeout_s))
    else:
        out("Bringing up hardware session (~20-23 min, once per run; no automatic retry)")
    started = clock()
    tracker = None

    def stage_event(line):
        return "  %s %s" % (stamp(clock() - started), english_stage_line(line))

    if reporter is not None:
        tracker = progress_module.BringupTracker(
            reporter, console_log=getattr(session, "console_log_path",
                                          run_root / "coordinator-console.log"),
            trace_log=run_root / "session-coordinator-trace.jsonl",
            clock=clock, event_formatter=stage_event)
        reporter.tick_hook = tracker.poll

    def on_stage(line):
        # 표시 순서(진행바 100% → 완료 이벤트 → 다음 단계)는 tracker가 지킨다;
        # 같은 줄이 콘솔 tail로 먼저 처리됐으면 다시 내지 않는다.
        if tracker is not None:
            tracker.on_stage_line(line)
            tracker.poll()
        else:
            out(stage_event(line))

    try:
        session.start()
        if timeout_s is not None:
            ready = session.wait_ready(timeout_s=timeout_s, on_stage=on_stage)
        else:
            ready = session.wait_ready(on_stage=on_stage)
    except LocalSessionError as exc:
        phase_label = tracker.failed_at("FAILED") if tracker else None
        if reporter is not None:
            reporter.tick_hook = None
        summary = _close_with_interrupt_guard(session, out)
        summary["console_log_path"] = str(
            getattr(session, "console_log_path", ""))
        raise BringupFailed(summary, str(exc), run_root=run_root,
                            phase_label=phase_label) from exc
    except KeyboardInterrupt:
        # bring-up(최대 ~20분, cold 65분) 중 Ctrl+C/SIGTERM — 전파시키지 않고 정상
        # close(shutdown 마커→cleanup→B210 반환)를 먼저 마친다. close 중
        # 두 번째 인터럽트는 guard가 TERM/KILL로 에스컬레이션한다 (결정 17).
        if tracker is not None:
            tracker.failed_at("INTERRUPTED")
            reporter.tick_hook = None
        out("Interrupt during bring-up — closing session (shutdown -> "
            "cleanup -> release B210s). Press Ctrl+C again to force.")
        summary = _close_with_interrupt_guard(session, out)
        raise BringupInterrupted(summary, run_id, run_root) from None
    if tracker is not None:
        tracker.poll()
        tracker.ready()
        reporter.tick_hook = None
        reporter.close()  # READY 이후에는 진행바를 완전히 종료한다
    out("  %s SESSION READY" % stamp(clock() - started))
    return session, run_id, run_root, ready, started


def _submit(session, scenario_id, certificate_case, *, workload_kind, submit_kwargs):
    """세션이 `workload_kind`를 알면 request에 싣는다 (readiness probe / user 구분)."""
    if getattr(session, "supports_workload_kind", False):
        submit_kwargs = dict(submit_kwargs, workload_kind=workload_kind)
    return session.submit_workload(scenario_id, certificate_case, **submit_kwargs)


def _run_workload(session, scenario_id, certificate_case, *, out, clock,
                  index=None, total=None, poc_ctx=None, poc_id=None,
                  workload_kind=readiness_probe.USER_WORKLOAD_KIND):
    """READY 이후 workload 하나 — 88fa6df와 같은 저 I/O 경로.

    RF 활성 구간(submit→result)에는 `running …`과 settle 안내만 표시하고
    trace 행은 on_trace로 **메모리에 수집만** 한다. 결과 JSON이 확정된 뒤
    (terminal state·verdict·quiescence·non_terminal 포함) 완료 evidence를
    파일당 한 번 읽어(final-result·service_events, scenario_report) 전송
    단위 세로 상세 블록을 출력하고, 마지막에 `result_line` 요약을 낸다.
    service_events 폴링·강제 flush·presentation 파일 갱신·실시간 블록은
    실행 경로에 없다(R1 선택 롤백). 상세 블록 렌더링 실패는 판정·close·
    evidence에 영향을 주지 않는다(경고 한 줄). 판정·timeout·evidence 계약은
    세션 객체가 그대로 소유한다.

    PoC opt-in(`poc_ctx`)이면 PoC 요청(source·확정된 poc_id·src_path)을 workload
    request에 실어 **coordinator에 위임**한다. coordinator(실물 controller child 또는
    RF-off software 세션)가 workload-local PoC(실 child·socket·SDK·provider)를
    arm → scenario 중 관측 전달 → finish/abort → cleanup까지 소유하고, CLI는 회신된
    `result["poc"]` evidence와 그 severity(R2: 다음 workload 허용 판정)만 반영한다.
    CLI 프로세스는 PoC를 실행하지 않는다(장치 경계는 launcher/coordinator 소유).
    PoC를 coordinator에서 실행한다고 선언하지 않은 세션에 PoC를 요청하면 조용한
    in-process 대체 없이 fail-closed로 거부한다.
    """
    poc_request = None
    if poc_ctx is not None:
        mod = poc_ctx["module"]
        if getattr(session, "poc_workloads_in_coordinator", False) is not True:
            raise mod.PocRunError(
                "session does not run PoC workloads in the coordinator — refusing to run "
                "the PoC in the CLI process (device boundary is owned by the coordinator)")
        poc_request = mod.workload_request(poc_ctx["option"], poc_id=poc_id,
                                           src_path=poc_ctx.get("src_path"))
    label = scenario_id + (" (%s)" % certificate_case
                           if scenario_id == "SC-VDE-001" else "")
    out("  running " + label
        + (" (poc via coordinator)" if poc_request is not None else ""))
    submit_kwargs = {
        "on_settle": lambda remaining, prev: out(
            "  settling %.0f s (RX pipeline drain after %s)" % (remaining, prev)),
    }
    if poc_request is not None:
        submit_kwargs["poc"] = poc_request
    workload_id = _submit(session, scenario_id, certificate_case,
                          workload_kind=workload_kind, submit_kwargs=submit_kwargs)
    started = clock()
    trace_rows = []
    result = session.wait_result(workload_id, on_trace=trace_rows.append)
    result.setdefault("wall_clock_s", round(clock() - started, 1))
    result.setdefault("workload_id", workload_id)
    result.setdefault("scenario_id", scenario_id)
    result.setdefault("certificate_case", certificate_case)
    result.setdefault("workload_kind", workload_kind)
    # 결과 확정 후에만 evidence를 한 번 읽는다 (RF 활성 구간 출력 없음).
    try:
        for line in scenario_report.workload_report_lines(
                trace_rows, result, run_root=getattr(session, "run_root", None),
                index=index, total=total):
            out(line)
    except Exception as exc:  # noqa: BLE001 - 상세 렌더링 실패가 판정·close·evidence를 바꾸지 않는다
        out("  (workload report rendering failed: %s: %s)" % (type(exc).__name__, exc))
    out(result_line(result))
    # coordinator가 result["poc"]에 evidence를 실어 준다(§11.1: scenario 판정과 독립, 재계산하지 않음).
    if poc_ctx is not None and result.get("poc") is not None:
        result["poc_severity"] = poc_ctx["module"].poc_severity(result["poc"])
    return result


def _run_probe(session, *, out, clock) -> dict:
    """내부 Link 17 양방향 probe — readiness 전용 고정 vector·횟수·상한 (§6.2).

    일반 scenario 성공으로 보고하지 않는다. 반환 {"result", "evaluation", "classification",
    "pass", "workload_id"}. probe 뒤에는 STOP·장치 close·worker 회수·재발견을 하지 않는다."""
    out("[readiness] internal Link 17 bidirectional probe (fixed vector %s, %d transfers, "
        "per-transfer timeout %d s)" % (readiness_probe.probe_plan_sha256()[:12],
                                        len(readiness_probe.PROBE_VECTOR),
                                        readiness_probe.PROBE_LIMITS["status_timeout_s"]))
    workload_id = _submit(session, PROBE_SCENARIO_ID, "normal",
                          workload_kind=readiness_probe.PROBE_WORKLOAD_KIND,
                          submit_kwargs={})
    started = clock()
    result = session.wait_result(workload_id, on_trace=lambda _row: None)
    result.setdefault("workload_id", workload_id)
    result.setdefault("scenario_id", PROBE_SCENARIO_ID)
    result.setdefault("workload_kind", readiness_probe.PROBE_WORKLOAD_KIND)
    result.setdefault("wall_clock_s", round(clock() - started, 1))
    body = ((result.get("scenario") or {}).get("results") or {}).get(PROBE_SCENARIO_ID)
    evaluation = readiness_probe.evaluate_probe_result(body)
    classification = readiness_classifier.classify_workload(result)
    passed = bool(evaluation["pass"] and classification["verdict"] == "PASS"
                  and result.get("status") == "PASS")
    problems = list(evaluation["problems"])
    if classification["verdict"] != "PASS":
        problems.append("classifier verdict %s (%s)" % (classification["verdict"],
                                                        classification["codes"]))
    out("[readiness] Link 17 probe %s (%.1f s)%s" % (
        "PASS" if passed else "FAIL", result.get("wall_clock_s") or 0.0,
        "" if passed else " — " + "; ".join(problems[:3])))
    return {"result": result, "evaluation": evaluation, "classification": classification,
            "pass": passed, "workload_id": workload_id, "problems": problems}


_ESCAPE_SEQ_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b[A-Za-z]|[\x00-\x1f\x7f]")


def _clean_choice(raw: str) -> str:
    """메뉴 입력 정리 — 방향키·기능키 ESC 시퀀스와 제어 문자를 제거한다
    (`input()`은 방향키를 '\\x1b[A' 같은 문자열로 넘긴다)."""
    return _ESCAPE_SEQ_RE.sub("", str(raw)).strip()


def _menu_poc_id(poc_ctx, scenario_id, *, out, input_fn):
    """catalog(`--poc-dir`) mode에서 scenario-호환 PoC를 대화형 선택한다(§6.1, R7). 반환 (kind, poc_id).

    kind ∈ {"poc","baseline","rescan","quit","invalid","eof"}. direct(`--poc`)면 ("poc", None): 고정 package.
    표시한 제어(q/r/none/숫자)를 그대로 poc_id로 넘기지 않고 menu.resolve로 해석한다.
    """
    if poc_ctx is None or not poc_ctx["option"].poc_dir:
        return ("poc", None)
    selection_kwargs = ({"server_config": poc_ctx["selection_config"]}
                        if poc_ctx.get("selection_config") is not None else {})
    for line in poc_ctx["module"].catalog_menu_lines(poc_ctx["option"], scenario_id, **selection_kwargs):
        out("  " + line)
    try:
        choice = _clean_choice(
            input_fn("  select poc [number / id / none / r=rescan / q=quit] > "))
    except EOFError:
        return ("eof", None)
    return poc_ctx["module"].resolve_catalog_selection(
        poc_ctx["option"], scenario_id, choice, **selection_kwargs)


def _stop_after_workload(result, out) -> bool:
    """workload 뒤 다음 workload 제출을 멈춰야 하는지(§14.1, R2).

    quiescence 실패 또는 PoC SESSION_INHIBIT(TX 결과·소유권 불확실)면 멈추고 session을 닫는다.
    """
    if not (result.get("quiescence") or {}).get("pass"):
        out("  session can no longer accept workloads (quiescence failed) — closing")
        return True
    if result.get("poc_severity") == "SESSION_INHIBIT":
        out("  PoC session inhibit (TX outcome/ownership uncertain) — no further "
            "workloads, closing session")
        return True
    return False


def _admit_or_stop(admission_fn, out) -> bool:
    """workload 제출 직전 absolute reserve 검사 (§5.1.1). False면 제출하지 않고 정상 close만."""
    if admission_fn is None:
        return True
    admission = admission_fn()
    if admission["admitted"]:
        return True
    out("  workload admission refused: remaining absolute session budget %.0f s < reserve "
        "%d s — no new workload is submitted; closing normally"
        % (admission["remaining_s"] or 0.0, admission["reserve_s"]))
    return False


def _menu(session, *, out, input_fn, clock, poc_ctx=None, admission_fn=None) -> tuple:
    """대화형 메뉴. 반환 (results, termination) — termination은 readiness_run.TERMINATION_*."""
    ordered = sorted(SCENARIOS)
    results = []
    while True:
        out("")
        for index, scenario_id in enumerate(ordered, 1):
            out("  %d) %s  %s" % (index, scenario_id, SCENARIOS[scenario_id]))
        out("  a) run all (005 -> 006 -> 001 normal -> 001 forged)")
        out("  q) quit")
        try:
            choice = _clean_choice(input_fn("select > "))
        except EOFError:
            out("  input channel closed (EOF) — closing session; no readiness is issued")
            return results, readiness_run.TERMINATION_INPUT_LOST
        if not choice:
            continue          # 빈 입력·방향키 등 제어 시퀀스만이면 다시 표시
        lowered = choice.lower()
        if lowered in ("q", "quit", "exit"):
            return results, readiness_run.TERMINATION_EXPLICIT_QUIT
        if lowered in ("a", "all"):
            for number, (scenario_id, case) in enumerate(DEFAULT_BATCH_PLAN, 1):
                kind, poc_id = _menu_poc_id(poc_ctx, scenario_id, out=out,
                                            input_fn=input_fn)
                if kind == "eof":
                    out("  input channel closed (EOF) — closing session; no readiness is issued")
                    return results, readiness_run.TERMINATION_INPUT_LOST
                if kind == "quit":
                    return results, readiness_run.TERMINATION_EXPLICIT_QUIT
                if kind in ("rescan", "invalid"):
                    out("  poc %s — no RF action for this scenario" % kind)
                    continue
                if not _admit_or_stop(admission_fn, out):
                    return results, readiness_run.TERMINATION_ADMISSION_RESERVE
                result = _run_workload(session, scenario_id, case,
                                       out=out, clock=clock, index=number,
                                       total=len(DEFAULT_BATCH_PLAN),
                                       poc_ctx=poc_ctx,
                                       poc_id=None if kind == "baseline" else poc_id)
                results.append(result)
                if result.get("status") != "PASS" or _stop_after_workload(result, out):
                    out("  stopping batch")
                    return results, readiness_run.TERMINATION_WORKLOAD_STOP
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(ordered):
            scenario_id = ordered[int(choice) - 1]
        elif choice.upper() in SCENARIOS:
            scenario_id = choice.upper()
        else:
            out("  unknown selection %r (no RF action taken)" % choice)
            continue
        case = "normal"
        if scenario_id == "SC-VDE-001":
            try:
                answer = input_fn(
                    "  certificate case [normal/forged] (default normal) > "
                ).strip().lower() or "normal"
            except EOFError:
                out("  input channel closed (EOF) — closing session; no readiness is issued")
                return results, readiness_run.TERMINATION_INPUT_LOST
            if answer not in ("normal", "forged"):
                out("  unknown certificate case %r (no RF action taken)"
                    % answer)
                continue
            case = answer
        # R7: PoC 선택. q=session 종료, r=rescan(READY 유지), 무효=거부 — 모두 scenario submit 0.
        kind, poc_id = _menu_poc_id(poc_ctx, scenario_id, out=out, input_fn=input_fn)
        if kind == "eof":
            out("  input channel closed (EOF) — closing session; no readiness is issued")
            return results, readiness_run.TERMINATION_INPUT_LOST
        if kind == "quit":
            return results, readiness_run.TERMINATION_EXPLICIT_QUIT
        if kind == "rescan":
            out("  rescan — returning to menu (no RF action, READY preserved)")
            continue
        if kind == "invalid":
            out("  invalid poc selection (no RF action taken)")
            continue
        if not _admit_or_stop(admission_fn, out):
            return results, readiness_run.TERMINATION_ADMISSION_RESERVE
        result = _run_workload(session, scenario_id, case, out=out,
                               clock=clock, poc_ctx=poc_ctx,
                               poc_id=None if kind == "baseline" else poc_id)
        results.append(result)
        if _stop_after_workload(result, out):
            return results, readiness_run.TERMINATION_WORKLOAD_STOP


def plan_for(scenario: str | None, certificate_case: str | None) -> list:
    """비대화형 인자 → workload 계획. None이면 대화형."""
    if scenario is None:
        return []
    if scenario == "all":
        return list(DEFAULT_BATCH_PLAN)
    if scenario not in SCENARIOS:
        raise RunRefusal(EXIT_CONFIG, "Unknown scenario %r. Choose one of: %s"
                         % (scenario, ", ".join(sorted(SCENARIOS)) + ", all"))
    if scenario == "SC-VDE-001":
        cases = (["normal", "forged"] if certificate_case == "both"
                 else [certificate_case or "normal"])
        return [(scenario, case) for case in cases]
    return [(scenario, "normal")]


# ------------------------------------------------------------ readiness 마감

def _authority_lines(context: dict) -> list:
    """§4 거부/상태 문구 — what/why, impact, action."""
    lines = []
    applicability = context["applicability"]
    status = applicability["operational_readiness"]
    qual = context["qualification"]
    q_axis = "APPLICABLE" if qual["applicable"] else ("STALE" if qual["present"] else "ABSENT")
    lines.append("VDETER standalone — current_authority=%s operational_readiness=%s "
                 "qualification=%s (workload RF: REQUIRES_LIVE_PREPARATION)"
                 % (context["current_authority"], status, q_axis))
    if qual["present"] and not qual["applicable"]:
        lines.append("[qualification] STALE: %s" % "; ".join(qual["mismatches"][:3]))
    if context["current_authority"] != "QUALIFIED":
        lines.append("[qualification] OPERATIONAL_READY is not QUALIFIED. A full `vdeter qualify` "
                     "is required for official release, publication evidence, or baseline "
                     "freeze.")
    if status == "ABSENT":
        lines.append("[readiness] UNVERIFIED: no applicable active operational-readiness record.")
        lines.append("            Impact: workload RF is blocked until this run completes bounded "
                     "readiness preparation.")
        lines.append("            Action: allow the run preparation to complete, or restore the "
                     "bound environment.")
    elif status == "STALE":
        lines.append("[readiness] READINESS STALE: %s differ from the active record."
                     % "; ".join(applicability["reasons"][:3]))
        lines.append("            Impact: the prior readiness record does not authorize workload "
                     "RF in this environment.")
        lines.append("            Action: restore the recorded values or let this run perform a "
                     "new bounded preparation.")
    elif status == "INHIBITED":
        lines.append("[readiness] INHIBITED (%s): a prior run observed a safety failure on this "
                     "stable binding. Impact: workload RF is blocked until this run's "
                     "preparation PASSes and a new release is promoted. Action: verify the "
                     "hardware, then let this run complete."
                     % ((context["readiness_snapshot"].get("inhibit") or {}).get("reason_code")
                        or "orphan inhibit"))
    elif status == "APPLICABLE":
        lines.append("[readiness] OPERATIONAL_READY: active record %s applies to this stable "
                     "binding; live preparation is still performed in this new session."
                     % (context["readiness_snapshot"]["active"]["release"]))
    selection = context["calibration"]
    if selection["mode"] == "warm":
        lines.append("[readiness] calibration: WARM — %s (%s)" % (
            selection["calibration"]["source"], selection["reason"]))
    else:
        lines.append("[readiness] calibration: COLD — %s; this run performs the bounded "
                     "CALIBRATION_PREPARATION (RR-P1 ADAPTIVE_SEARCH_AND_SEAL contract) first "
                     "(hard cap %d s, no retry)" % (selection["reason"],
                                                     int(readiness_run.READINESS_COLD_PREPARATION_S)))
    return lines


def _absolute_deadline(ready: dict, spawn_monotonic: float) -> float:
    """coordinator가 ready.json에 실은 절대 deadline(monotonic)과 spawn+7200 중 이른 값(보수)."""
    conservative = spawn_monotonic + readiness_run.SESSION_ABSOLUTE_DEADLINE_S
    reported = (ready or {}).get("session_absolute_deadline_monotonic_s")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return min(float(reported), conservative)
    return conservative


def _finish_readiness(context: dict, paths: StatePaths, lock, *, run_root, run_id,
                      outcome: dict, summary: dict, topology_after, probe: dict | None,
                      ready: dict | None, admission: dict | None, boot_id, out,
                      cold_calibration: dict | None, phase_marks: dict) -> dict:
    """close 뒤 readiness 마감 — candidate 기록 → 승격, 또는 inhibit record → state 참조 추가.

    반환 summary용 readiness block. 어떤 경로도 qualification namespace를 건드리지 않는다."""
    snapshot = context["readiness_snapshot"]
    binding = context["binding"]
    mode = context["calibration"]["mode"]
    block = {
        "enabled": True, "not_a_qualification": True,
        "readiness_id": context["readiness_id"], "mode": mode,
        "status": outcome["readiness_status"], "issue_status": "NOT_PROMOTED",
        "release": None, "generation": None, "inhibit": None,
        "reasons": list(outcome["reasons"]), "termination": outcome["termination"],
        "inhibit_codes": outcome["inhibit_codes"],
        "probe": None if probe is None else {
            "pass": probe["pass"], "workload_id": probe["workload_id"],
            "problems": probe["problems"], "plan_sha256": readiness_probe.probe_plan_sha256()},
        "admission": admission,
        "current_authority": context["current_authority"],
        "operational_readiness": context["applicability"]["operational_readiness"],
        "qualification": ("APPLICABLE" if context["qualification"]["applicable"]
                          else ("STALE" if context["qualification"]["present"] else "ABSENT")),
        "calibration_source": (context["calibration"]["calibration"] or {}).get("source"),
        "workload_verdicts": [
            {"workload_id": r.get("workload_id"), "verdict": v["verdict"], "codes": v["codes"]}
            for r, v in zip(outcome.get("_user_results", []), outcome["workload_verdicts"])],
    }
    close_class = readiness_classifier.classify_close(summary, topology_after)
    checks = {
        "preflight": {"pass": True, "checks": sorted((context["preflight"].get("checks") or {}).keys())},
        "host_time": {"pass": "acquire_host_time" in phase_marks or "readiness_host_time" in phase_marks,
                      "elapsed_s": phase_marks.get("acquire_host_time")},
        "rf_time_pre": {"pass": "rf_time_pre" in phase_marks, "elapsed_s": phase_marks.get("rf_time_pre")},
        "active_bridge": {"pass": "active_bridge" in phase_marks,
                          "elapsed_s": phase_marks.get("active_bridge")},
        "rf_time_post": {"pass": "rf_time_post" in phase_marks,
                         "elapsed_s": phase_marks.get("rf_time_post")},
        "service_admission": {"pass": "service_running" in phase_marks,
                              "elapsed_s": phase_marks.get("service_running")},
        "link17_bidirectional": None if probe is None else {
            "pass": probe["pass"], "evaluation": probe["evaluation"],
            "classification": probe["classification"]},
        "pre_workload_quiescence": {"pass": bool(probe and probe["pass"]
                                                and (probe["result"].get("quiescence") or {}).get("pass"))},
        "calibration": cold_calibration,
        "workload_admission_reserve_s": int(readiness_run.WORKLOAD_ADMISSION_RESERVE_S),
    }
    execution_close = {
        "cleanup": {"pass": bool(summary.get("pass")), "problems": summary.get("problems"),
                    "escalation": summary.get("escalation"),
                    "coordinator_rc": summary.get("coordinator_rc"),
                    "closed_status": (summary.get("closed") or {}).get("status"),
                    "cleanup_detail": ((summary.get("closed") or {}).get("cleanup") or {}),
                    "residual_processes": summary.get("residual_processes"),
                    "classification": close_class},
        "post_close_discovery": {"pass": topology_after["pass"] if topology_after else None,
                                 "topology_after": topology_after},
    }
    session_block = {
        "boot_id": boot_id, "session_id": run_id, "run_root": str(run_root),
        "session_phase": COLD_SESSION_PHASE if mode == "cold" else WARM_SESSION_PHASE,
        "ready_at_utc": (ready or {}).get("ready_at_utc"),
        "bridge_generation": ((ready or {}).get("health_baseline") or {}).get("bridge_generation"),
        "active_clock_id": ((ready or {}).get("health_baseline") or {}).get("active_clock_id"),
        "cross_command_handoff_allowed": False, "session_reuse_scope": "THIS_RUN_ONLY",
    }
    extra = {"binding.json": readiness_run.plan_bytes(binding)}
    evidence = readiness_run.collect_run_evidence(
        run_root, probe_workload_id=(probe or {}).get("workload_id"), extra=extra)

    if outcome["promote"]:
        readiness_id = context["readiness_id"]
        record = readiness.build_readiness_record(
            readiness_id=readiness_id, binding=binding, status="PASS",
            record_metadata={"source_run_id": run_id, "mode": mode},
            preparation_checks=checks, execution_close=execution_close, session=session_block,
            qualification_provenance=(
                {"reference_sha256": (context["reference"] or {}).get("_reference_sha256"),
                 "authority": "APPLICABLE"} if context["qualification"]["applicable"] else None),
            observations={"phase_elapsed_s": phase_marks, "safety_policy": readiness_run.SAFETY_POLICY,
                          "inventory_only": (context.get("installation_check") or {}).get("inventory_only"),
                          "ready": {k: v for k, v in (ready or {}).items() if k != "note"}},
            calibration_candidate=cold_calibration,
            workloads=[{"workload_id": r.get("workload_id"), "scenario_id": r.get("scenario_id"),
                        "certificate_case": r.get("certificate_case"), "status": r.get("status"),
                        "workload_kind": r.get("workload_kind"), "verdict": v["verdict"]}
                       for r, v in zip(outcome.get("_user_results", []), outcome["workload_verdicts"])])
        try:
            written = readiness.write_immutable_record(
                paths.readiness_candidate / readiness_id, basename=readiness.RECORD_BASENAME,
                record=record, evidence=evidence, producer="vdeter run")
            writer = readiness.ReadinessWriter(paths, lock)
            new_state = writer.promote(readiness_id, expected_generation=context["expected_generation"])
        except readiness.StaleWriterError as exc:
            block["issue_status"] = "NOT_PROMOTED"
            block["reasons"].append("stale writer: %s" % exc)
            out("[readiness] NOT PROMOTED — %s (candidate kept as inactive evidence)" % exc)
            return block
        except readiness.ReadinessError as exc:
            block["issue_status"] = "NOT_PROMOTED"
            block["reasons"].append("record/promotion error: %s" % exc)
            out("[readiness] NOT PROMOTED — %s" % exc)
            return block
        block.update(issue_status="PROMOTED", release=readiness_id,
                     generation=new_state["generation"], record_sha256=written["sha256"],
                     readiness_digest_sha256=record["readiness_digest_sha256"])
        out("[readiness] PROMOTED — new immutable release %s (generation %d, digest %s…); "
            "OPERATIONAL_READY is not QUALIFIED"
            % (readiness_id, new_state["generation"], record["readiness_digest_sha256"][:12]))
        return block

    if outcome["inhibit"]:
        inhibit_id = readiness.new_readiness_id("inhibit")
        reason_code = outcome["inhibit_codes"][0]
        evidence_sha = readiness_run.evidence_sha256_of(evidence)
        record = readiness.build_inhibit_record(
            inhibit_id=inhibit_id, binding=binding, reason_code=reason_code,
            source_run_id=run_id, evidence_sha256=evidence_sha,
            detail={"codes": outcome["inhibit_codes"], "termination": outcome["termination"],
                    "close": execution_close, "phase_elapsed_s": phase_marks},
            active_release=((snapshot.get("active") or {}).get("release")),
            observations={"session": session_block})
        try:
            readiness.write_immutable_record(
                paths.readiness_inhibits / inhibit_id, basename=readiness.INHIBIT_BASENAME,
                record=record, evidence=evidence, producer="vdeter run")
            writer = readiness.ReadinessWriter(paths, lock)
            new_state = writer.inhibit(inhibit_id, expected_generation=context["expected_generation"])
        except readiness.ReadinessError as exc:
            block["reasons"].append("inhibit record error: %s" % exc)
            out("[readiness] INHIBIT NOT RECORDED — %s (state unchanged; investigate before RF)" % exc)
            block["issue_status"] = "INHIBIT_RECORD_FAILED"
            return block
        block.update(issue_status="INHIBITED", generation=new_state["generation"],
                     inhibit={"release": inhibit_id, "reason_code": reason_code,
                              "codes": outcome["inhibit_codes"]})
        out("[readiness] INHIBITED — active readiness %s is inhibited (%s); workload RF stays "
            "blocked until a new preparation PASSes and promotes a new release"
            % ((snapshot.get("active") or {}).get("release"), reason_code))
        return block

    out("[readiness] NOT PROMOTED — %s" % ("; ".join(block["reasons"]) or "no readiness issued"))
    if outcome["inhibit_codes"] and not context.get("active_same_binding"):
        out("[readiness] safety codes %s observed but no active release of this stable binding "
            "exists; recorded in the run evidence only" % outcome["inhibit_codes"])
    return block


def execute(paths: StatePaths, *, scenario: str | None = None,
            certificate_case: str | None = None, run_id: str | None = None,
            diagnostics: bool = False, json_summary: bool = False,
            retry: bool = True,
            session_factory=LocalCoordinatorSession,
            preflight_fn=None, reference_loader=None, usb_probe=None,
            environment_manifest=None, repo_root=None,
            hardware_path=None, poc=None, poc_dir=None, out=print, input_fn=input,
            clock=time.monotonic, progress: bool = True,
            progress_stream=None, progress_ticker: bool = True,
            topology_probe=None, rf_off_software: bool = False,
            critical_check=None, workload_plan=None,
            hold_after_ready_s: float = 0.0, readiness_enabled=None,
            machine_id=None, boot_id=None, readiness_sleeper=None) -> int:
    """`vdeter run` 본체. 반환: 종료 코드 (README §11).

    `progress=False`(`--no-progress`)면 진행바 없이 줄 단위 상태만 출력한다.
    표시 계층은 판정·evidence 결과에 영향을 주지 않는다.

    `poc`/`poc_dir`(Stage 8 opt-in)가 주어지면 workload마다 scenario-호환 PoC를 선택·snapshot해
    실제 child·socket·provider로 실행한다. 둘 다 없으면 `poc_run`(stage8) module을 로드하지 않는다
    (기능 OFF 구조적 0). device 경계는 session이 제공한다(`poc_device_seams`).

    `workload_plan`(개발용 진단 전용, `diag_cmd`)은 `(scenario_id, case)` 목록으로 비대화형
    계획을 직접 준다 — 빈 목록이면 bring-up→(선택적 `hold_after_ready_s` 동안 READY 유지)→
    close만 수행하는 RX-only 세션이다. 정상 `vdeter run`은 이 인자를 쓰지 않는다.

    `readiness_enabled`(기본 자동): 정상 run(workload_plan·critical_check 없음, `--rf-off-software`
    아님)에서만 readiness preparation·probe·승격을 수행한다. `retry`는 readiness 경로에서 무시된다
    (bring-up 시도는 항상 1회)."""
    if readiness_enabled is None:
        readiness_enabled = (workload_plan is None and critical_check is None
                             and not rf_off_software)
    try:
        if workload_plan is not None:
            # 항목은 `(scenario, case)` 또는 `(scenario, case, poc_id)`. poc_id는 catalog
            # 선택을 비대화형으로 고정하기 위한 값이며 None이면 종전과 같다.
            plan = []
            for item in workload_plan:
                sid, case = item[0], item[1]
                poc_id_of = item[2] if len(item) > 2 else None
                plan.append((str(sid), str(case),
                             None if poc_id_of is None else str(poc_id_of)))
            for scenario_id, case, _poc_id in plan:
                if scenario_id not in SCENARIOS or case not in ("normal", "forged"):
                    raise RunRefusal(EXIT_CONFIG,
                                     "Unknown workload %r/%r in plan" % (scenario_id, case))
        else:
            plan = plan_for(scenario, certificate_case)
            if scenario is not None and not plan:
                raise RunRefusal(EXIT_CONFIG,
                                 "non-interactive workload list is empty — no RF action")
    except RunRefusal as exc:
        out("Refused: " + str(exc))
        return exc.code
    interactive = scenario is None and workload_plan is None
    rx_only = workload_plan is not None and not plan
    rx_only_ok = rx_only  # READY 유지 중 coordinator가 죽으면 False

    # Stage 8 opt-in: --poc/--poc-dir가 있을 때만 sanctioned adapter를 lazy import한다.
    poc_option = None
    poc_run = None
    if poc is not None or poc_dir is not None:
        from apps.standalone import poc_run as poc_run
        # 계획이 workload마다 poc_id를 확정해 두었으면 menu를 띄우지 않는다. 그 경우에만
        # 비대화형 catalog가 허용된다(§6.1의 "prompt 금지" 취지는 그대로 — prompt를 열지
        # 않는 경로다). 계획에 id가 하나도 없으면 종전대로 비대화형 catalog는 거부된다.
        preselected = bool(workload_plan) and any(
            len(item) > 2 and item[2] is not None for item in workload_plan)
        try:
            poc_option = poc_run.resolve_option(poc=poc, poc_dir=poc_dir,
                                                interactive=interactive,
                                                preselected=preselected)
        except poc_run.PocRunError as exc:
            out("Refused: " + str(exc))
            return EXIT_CONFIG

    lock = SingleInstanceLock(paths.lock_path)
    try:
        lock.acquire(diagnostics={"command": "run"})
    except StateError as exc:
        out("Refused: " + str(exc))
        return EXIT_INTERNAL
    # READY 이후(시나리오 구간)에는 진행바·mirror 없이 이 원 sink로만 출력한다.
    terminal_out = out
    mirror = progress_module.MirroredOut(out, None)
    reporter = progress_module.make_reporter(
        plain=not progress, sink=mirror, stream=progress_stream,
        scope="PREFLIGHT",
        evidence_log=paths.var_dir / "progress" / "run.log",
        ticker=progress_ticker)
    out = reporter.log
    try:
        gates = progress_module.GateProgress(reporter, total=7 if readiness_enabled else 5)
        try:
            context = prepare(
                paths, repo_root=repo_root,
                hardware_path=hardware_path, preflight_fn=preflight_fn,
                reference_loader=reference_loader,
                environment_manifest=environment_manifest, progress=gates,
                critical_check=critical_check, readiness_enabled=readiness_enabled,
                machine_id=machine_id, readiness_sleeper=readiness_sleeper)
        except RunRefusal as exc:
            reporter.finish("FAILED")
            out("Refused (exit %d): %s" % (exc.code, exc))
            return exc.code
        gates.done()
        reference = context["reference"]
        readiness_block = None
        if readiness_enabled:
            for line in _authority_lines(context):
                out(line)
            if retry is False:
                out("[readiness] note: --no-retry has no effect — the readiness path never retries")
            readiness_block = {
                "current_authority": context["current_authority"],
                "operational_readiness": context["applicability"]["operational_readiness"],
                "qualification": ("APPLICABLE" if context["qualification"]["applicable"]
                                  else ("STALE" if context["qualification"]["present"] else "ABSENT")),
                "issue_status": "NOT_PROMOTED",
            }
        elif is_diagnostic_authority(reference):
            # 개발용 진단 — QUALIFIED/RESEARCH_READY로 표시하지 않는다.
            out("VDETER standalone — DIAGNOSTIC, NOT_QUALIFIED (authorization "
                "%s, issued %s; not a qualification, no reference is issued)"
                % ((reference.get("_reference_sha256") or "")[:12],
                   reference.get("issued_utc", "?")))
        else:
            out("VDETER standalone — QUALIFIED (reference %s, issued %s)"
                % ((reference.get("_reference_sha256") or "")[:12],
                   reference.get("issued_utc", "?")))
        observed = context["preflight"]["checks"]["b210"]["observed_by_role"]
        out("[preflight] devices 3/3 (%s)" % " · ".join(
            "%s %s %s" % (role, info["serial"],
                          "USB3" if info["speed_class"] == "super" else "USB2")
            for role, info in sorted(observed.items()) if info))
        out("[preflight] identity, config schema, residual processes, "
            "installation manifest — OK")
        if readiness_enabled:
            inventory = (context.get("installation_check") or {}).get("inventory_only") or {}
            changed = sum(len(inventory.get(k) or []) for k in ("added", "removed", "changed"))
            if changed:
                out("[preflight] inventory-only differences outside the readiness execution "
                    "closure: %d file(s) (recorded in evidence; readiness RF not refused)"
                    % changed)
        if hardware_profiles.is_pi_powered_hub(context["hardware"]):
            hub_decl = context["hardware"].get("usb_hub") or {}
            topo = context["preflight"]["checks"].get("usb_topology") or {}
            hub_obs = topo.get("hub") or {}
            out("[preflight] host profile %s — hub %s (%s) at %s %sM, descriptor "
                "self-powered=%s; operator-attested: hub supply connected=%s, "
                "per-B210 DC=%s" % (
                    hardware_profiles.PI_POWERED_HUB_PROFILE,
                    hub_decl.get("asset_id"), hub_decl.get("vid_pid"),
                    hub_obs.get("usb_path"), hub_obs.get("speed_mbps"),
                    hub_obs.get("descriptor_self_powered"),
                    hub_decl.get("self_powered"),
                    hub_decl.get("individual_b210_dc")))
        if context["disk"]["status"] == "warn":
            out("[warning]   free disk space is low (%d MiB)"
                % (context["disk"]["free_bytes"] >> 20))

        if usb_probe is None:
            from apps.standalone.local_preflight import probe_usb_serials
            usb_probe = probe_usb_serials
        if boot_id is None:
            boot_id = readiness_run.read_boot_id()

        attempts = []
        session = None
        active_run_id = None
        run_root = None
        ready = None
        spawn_monotonic = None
        # readiness 경로: bring-up 시도는 정확히 1회 (warm/cold 모두 implicit retry 0).
        try:
            session, active_run_id, run_root, ready, spawn_monotonic = _bring_up(
                context, paths, run_id=run_id,
                session_factory=session_factory, diagnostics=diagnostics,
                out=out, clock=clock, reporter=reporter, mirror=mirror,
                rf_off_software=rf_off_software)
            attempts.append({"attempt": 1, "run_id": active_run_id, "bringup": "READY",
                             "mode": _readiness_mode(context)})
        except RunRefusal as exc:
            out("Refused (exit %d): %s" % (exc.code, exc))
            return exc.code
        except BringupInterrupted as stopped:
            # 재시도 없음. cleanup·잔존·B210 반환 결과를 evidence로 남긴다.
            summary = stopped.summary
            if summary["pass"]:
                out("Closed — cleanup PASS, residual processes 0")
            else:
                out("Closed with problems: "
                    + "; ".join(summary["problems"]))
                for note in close_notes(summary):
                    out("  note: " + note)
            attempts.append({"attempt": 1, "run_id": stopped.run_id,
                             "bringup": "INTERRUPTED",
                             "finalize": summary})
            if readiness_enabled:
                readiness_block.update(
                    enabled=True, not_a_qualification=True, readiness_id=context["readiness_id"],
                    mode=_readiness_mode(context), status="CANCELLED",
                    termination=readiness_run.TERMINATION_USER_CANCELLED,
                    reasons=["interrupted during bring-up — no readiness is issued"])
                out("[readiness] NOT PROMOTED — interrupted during bring-up")
            write_json_atomic(stopped.run_root / "summary.json", {
                "artifact_type": "STAGE7_STANDALONE_RUN_SUMMARY_V1",
                "run_id": stopped.run_id, "status": "FAIL",
                "interactive": interactive, "interrupted": True,
                "cancelled": True,
                "interrupted_during": "bring-up",
                "scenarios_passed": 0, "scenarios_run": 0,
                "scenarios_planned": len(plan),
                "workloads": [], "close": summary,
                "attempts": attempts,
                "readiness": readiness_block,
                "diagnostics": build_child_env(
                    diagnostics=diagnostics).get("STAGE7_DIAG") == "1",
                **authority_fields(reference, readiness_block=readiness_block),
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
            })
            out("Evidence: %s   (summary: vdeter results %s)"
                % (stopped.run_root, stopped.run_id))
            if not interactive:
                out("RESULT overall=FAIL scenarios=0/%d run-id=%s "
                    "evidence=%s (interrupted during bring-up)%s"
                    % (len(plan), stopped.run_id, stopped.run_root,
                       result_suffix(reference, readiness_block)))
            return EXIT_INTERRUPTED
        except BringupFailed as failed:
            console_text = ""
            try:
                console_text = Path(
                    failed.summary.get("console_log_path")
                    or "").read_text(encoding="utf-8", errors="replace")
            except (OSError, TypeError):
                pass
            usb_now = usb_probe()
            classification = classify_bringup_failure(
                console_text, failed.summary, usb_now,
                context["declared_serials"])
            phase_marks = ((failed.summary.get("closed") or {}).get("phase_elapsed_s") or {})
            structured = readiness_classifier.classify_bringup(phase_marks, True, ready=False)
            attempts.append({"attempt": 1,
                             "bringup": "FAILED", "error": failed.error,
                             "finalize": failed.summary,
                             "classification": classification,
                             "readiness_classification": structured})
            out("Bring-up failed: %s" % failed.error)
            for line in progress_module.bringup_failure_lines(
                    phase_label=failed.phase_label or "bring-up",
                    finalize_summary=failed.summary,
                    classification=classification,
                    console_text=console_text,
                    run_root=failed.run_root, attempt=1,
                    max_attempts=READINESS_BRINGUP_ATTEMPTS if readiness_enabled
                    else MAX_BRINGUP_ATTEMPTS,
                    declared_serials=context["declared_serials"],
                    usb_observed=usb_now,
                    host_time_detail=(
                        progress_module.host_time_detail_from_evidence(
                            failed.run_root)
                        if failed.run_root is not None else None)):
                out(line)
            out("No retry: the readiness path never retries a bring-up; start a new "
                "`vdeter run` after checking the hardware"
                if readiness_enabled else
                "No retry: " + "; ".join(classification["reasons"] or ["diagnostic run"]))
            if readiness_enabled and failed.run_root is not None:
                close_class = readiness_classifier.classify_close(failed.summary, None)
                outcome = readiness_run.decide_outcome(
                    readiness_enabled=True, prepared=False, probe_pass=False,
                    ready_issued=False, termination=readiness_run.TERMINATION_BRINGUP_FAILED,
                    user_results=[], close_class=close_class, topology_after_pass=None,
                    bringup_class=structured, active_same_binding=context["active_same_binding"],
                    interactive=interactive)
                outcome["_user_results"] = []
                readiness_block = _finish_readiness(
                    context, paths, lock, run_root=failed.run_root, run_id=run_id or "-",
                    outcome=outcome, summary=failed.summary, topology_after=None, probe=None,
                    ready=None, admission=None, boot_id=boot_id, out=out,
                    cold_calibration=None, phase_marks=phase_marks)
                write_json_atomic(failed.run_root / "summary.json", {
                    "artifact_type": "STAGE7_STANDALONE_RUN_SUMMARY_V1",
                    "run_id": failed.run_root.name, "status": "FAIL",
                    "interactive": interactive, "interrupted": False, "cancelled": False,
                    "bringup_failed": True,
                    "scenarios_passed": 0, "scenarios_run": 0, "scenarios_planned": len(plan),
                    "workloads": [], "close": failed.summary, "attempts": attempts,
                    "readiness": readiness_block,
                    "diagnostics": build_child_env(diagnostics=diagnostics).get("STAGE7_DIAG") == "1",
                    **authority_fields(reference, readiness_block=readiness_block),
                    "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            out("RESULT overall=FAIL scenarios=0/%d run-id=- evidence=%s%s"
                % (len(plan), paths.runs_dir, result_suffix(reference, readiness_block)))
            return EXIT_FAIL
        if session is None:
            return EXIT_FAIL

        results = []
        exit_code = EXIT_PASS
        interrupted = False
        termination = readiness_run.TERMINATION_PLAN_COMPLETE
        probe = None
        ready_issued = False
        admission = None
        cold_calibration = None
        deadline_monotonic = _absolute_deadline(ready, spawn_monotonic)

        def admission_fn():
            return readiness_run.workload_admission(deadline_monotonic - clock())

        # SESSION READY 이후: 88fa6df와 같은 저 I/O 경로. 진행바는 이미 닫혔고
        # (bring-up 끝), presentation mirror도 끊는다 — 시나리오 구간에는
        # service_events 폴링·실시간 추적·presentation 파일 갱신이 없다(R1).
        # workload trace·result·evidence 생성은 세션/coordinator 계약 그대로다.
        mirror.set_path(None)
        out = terminal_out
        # PoC는 CLI 프로세스에서 실행하지 않는다 — `_run_workload`가 요청을 workload request에 실어
        # coordinator(실물 controller child 또는 RF-off software 세션)에 위임하고 evidence만 반영한다.
        poc_ctx = None
        if poc_option is not None and poc_option.active:
            poc_ctx = {
                "module": poc_run, "option": poc_option,
                "src_path": str(Path(context["repo_root"]) / "VDETER" / "src"),
                # Catalogue availability only. The owning launcher validates
                # actual tuning/windows BEFORE runtime WELCOME advertises dual.
                "selection_config": poc_run.ServerCapabilityConfig(
                    wsm_g1_production_installed=(
                        getattr(session, "poc_workloads_in_coordinator", False) is True)),
            }
            out("[poc]       Stage 8 PoC opt-in active (%s%s)"
                % ("catalog" if poc_option.poc_dir else "direct",
                   ", rf-off-software" if rf_off_software else ""))
        try:
            if readiness_enabled:
                mode = _readiness_mode(context)
                if mode == "cold":
                    cold_calibration = _seal_cold_calibration(context, run_root, ready, out)
                    if cold_calibration is None:
                        termination = readiness_run.TERMINATION_BRINGUP_FAILED
                        raise _ReadinessStop("cold calibration candidate not sealed in ready.json")
                admission = admission_fn()
                if not admission["admitted"]:
                    termination = readiness_run.TERMINATION_NOT_ADMITTED
                    out("[readiness] READY_FOR_WORKLOAD not issued: remaining absolute budget "
                        "%.0f s < reserve %d s — closing normally without workload"
                        % (admission["remaining_s"] or 0.0, admission["reserve_s"]))
                    raise _ReadinessStop("admission reserve")
                probe = _run_probe(session, out=out, clock=clock)
                if not probe["pass"]:
                    termination = readiness_run.TERMINATION_PROBE_FAILED
                    exit_code = EXIT_FAIL
                    raise _ReadinessStop("probe failed")
                admission = admission_fn()
                if not admission["admitted"]:
                    termination = readiness_run.TERMINATION_NOT_ADMITTED
                    out("[readiness] READY_FOR_WORKLOAD not issued after the probe: remaining "
                        "%.0f s < reserve %d s — closing normally"
                        % (admission["remaining_s"] or 0.0, admission["reserve_s"]))
                    raise _ReadinessStop("admission reserve")
                ready_issued = True
                out("[readiness] READY_FOR_WORKLOAD (process-local lease, this run only; "
                    "remaining absolute budget %.0f s, reserve %d s)"
                    % (admission["remaining_s"], admission["reserve_s"]))
            if interactive:
                results, termination = _menu(session, out=out, input_fn=input_fn,
                                             clock=clock, poc_ctx=poc_ctx,
                                             admission_fn=admission_fn if readiness_enabled else None)
            elif rx_only:
                # 개발용 진단 RX-only: workload 제출 0. READY를 유계 시간 동안 유지한 뒤
                # 기존 close 경로로 닫는다(Ctrl+C는 아래 인터럽트 처리와 동일).
                hold = max(0.0, float(hold_after_ready_s or 0.0))
                out("  RX-only session — no workload will be submitted; holding "
                    "READY for %.0f s then closing" % hold)
                held = clock()
                while clock() - held < hold:
                    time.sleep(min(1.0, hold - (clock() - held)))
                    if session.coordinator_exited():
                        out("  coordinator exited during RX-only hold")
                        exit_code = EXIT_FAIL
                        rx_only_ok = False
                        break
            else:
                for number, entry in enumerate(plan, 1):
                    # 계획 항목은 `(scenario, case)` 또는 `(scenario, case, poc_id)`다.
                    scenario_id, case = entry[0], entry[1]
                    planned_poc_id = entry[2] if len(entry) > 2 else None
                    if readiness_enabled and not _admit_or_stop(admission_fn, out):
                        termination = readiness_run.TERMINATION_ADMISSION_RESERVE
                        break
                    result = _run_workload(session, scenario_id, case,
                                           out=out, clock=clock,
                                           index=number, total=len(plan),
                                           poc_ctx=poc_ctx,
                                           poc_id=planned_poc_id)
                    results.append(result)
                    # R2: scenario 실패·quiescence 실패·PoC SESSION_INHIBIT면 다음 workload 제출 0.
                    if result.get("status") != "PASS" or _stop_after_workload(result, out):
                        termination = readiness_run.TERMINATION_WORKLOAD_STOP
                        break
        except _ReadinessStop:
            pass
        except KeyboardInterrupt:
            interrupted = True
            termination = readiness_run.TERMINATION_USER_CANCELLED
            out("Interrupt — closing session (shutdown -> cleanup -> "
                "release B210s). Press Ctrl+C again to force.")
        except LocalSessionError as exc:
            out("Session error: %s" % exc)
            exit_code = EXIT_FAIL
            termination = readiness_run.TERMINATION_SESSION_ERROR
        finally:
            out("Closing session (endpoint STOP, releasing B210s)...")
            summary = _close_with_interrupt_guard(session, out)
            if summary["pass"]:
                out("Closed — cleanup PASS, residual processes 0")
            else:
                out("Closed with problems: " + "; ".join(summary["problems"]))
                for note in close_notes(summary):
                    out("  note: " + note)
                exit_code = EXIT_FAIL
            # powered-hub profile (2026-09-10): after the B210s are released,
            # re-observe sysfs — all three must be back below the approved hub
            # at 5000M. Recorded in the summary; a failure is FAIL (no retry).
            topology_after = None
            if hardware_profiles.is_pi_powered_hub(context["hardware"]):
                topology_after = _topology_after_close(context["hardware"],
                                                       topology_probe)
                out(topology_after_line(topology_after))
                if not topology_after["pass"]:
                    exit_code = EXIT_FAIL
            passed = sum(1 for r in results if r.get("status") == "PASS")
            planned = len(plan) if not interactive else len(results)
            if interactive:
                overall = ("PASS" if summary["pass"] and passed == len(results)
                           else "FAIL")
            else:
                # RX-only(진단, workload 0)는 bring-up·READY 유지·close·cleanup PASS만으로
                # PASS다. 정상 run은 workload_plan을 쓰지 않으므로 종전 판정식 그대로다.
                overall = ("PASS" if summary["pass"] and (results or rx_only_ok)
                           and passed == len(plan) else "FAIL")
            if topology_after is not None and not topology_after["pass"]:
                overall = "FAIL"
            # R2: PoC experiment 인프라 실패(ABORTED/ERROR)를 최종 판정·exit에 정직하게 반영한다.
            def _poc_infra_ok(result):
                poc_block = result.get("poc") or {}
                infra = poc_block.get("poc_infrastructure")
                if infra in (None, "COMPLETED"):
                    return True
                # baseline(none) workload는 PoC를 **쓰지 않기로 한** 것이지 인프라 실패가 아니다.
                return infra == "NOT_RUN" and poc_block.get("status") == "BASELINE"

            poc_infra_ok = all(_poc_infra_ok(r) for r in results)
            if not poc_infra_ok:
                overall = "FAIL"
            cancelled = False
            phase_marks = ((summary.get("closed") or {}).get("phase_elapsed_s")
                           or (ready or {}).get("phase_elapsed_s") or {})
            if readiness_enabled:
                close_class = readiness_classifier.classify_close(summary, topology_after)
                bringup_class = readiness_classifier.classify_bringup(phase_marks, False, ready=True)
                outcome = readiness_run.decide_outcome(
                    readiness_enabled=True, prepared=True,
                    probe_pass=bool(probe and probe["pass"]), ready_issued=ready_issued,
                    termination=termination, user_results=results,
                    close_class=close_class,
                    topology_after_pass=None if topology_after is None else topology_after["pass"],
                    bringup_class=bringup_class,
                    active_same_binding=context["active_same_binding"],
                    interactive=interactive)
                outcome["_user_results"] = results
                if not poc_infra_ok:
                    outcome["promote"] = False
                    outcome["reasons"].append("PoC infrastructure failure")
                overall = outcome["summary_status"]
                cancelled = outcome["cancelled"]
                readiness_block = _finish_readiness(
                    context, paths, lock, run_root=run_root, run_id=active_run_id,
                    outcome=outcome, summary=summary, topology_after=topology_after,
                    probe=probe, ready=ready, admission=admission, boot_id=boot_id, out=out,
                    cold_calibration=cold_calibration, phase_marks=phase_marks)
                if overall == "NO_USER_WORKLOAD":
                    exit_code = EXIT_PASS
                elif overall == "PASS":
                    exit_code = exit_code or EXIT_PASS
                elif overall == "USER_CANCELLED":
                    exit_code = EXIT_INTERRUPTED
                else:
                    exit_code = exit_code or EXIT_FAIL
            else:
                if overall != "PASS":
                    exit_code = exit_code or EXIT_FAIL
                if interrupted:
                    exit_code = EXIT_INTERRUPTED
            run_summary = {
                "artifact_type": "STAGE7_STANDALONE_RUN_SUMMARY_V1",
                "run_id": active_run_id, "status": overall,
                "interactive": interactive, "interrupted": interrupted,
                "cancelled": cancelled, "termination": termination,
                "scenarios_passed": passed, "scenarios_run": len(results),
                "scenarios_planned": planned,
                "workloads": results, "close": summary,
                "usb_topology_after_close": topology_after,
                "attempts": attempts,
                "readiness": readiness_block,
                "readiness_probe": None if probe is None else probe["result"],
                "diagnostics": build_child_env(
                    diagnostics=diagnostics).get("STAGE7_DIAG") == "1",
                **authority_fields(reference, readiness_block=readiness_block),
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
            }
            write_json_atomic(run_root / "summary.json", run_summary)
            out("Evidence: %s   (summary: vdeter results %s)"
                % (run_root, active_run_id))
            if not interactive:
                out("RESULT overall=%s scenarios=%d/%d run-id=%s evidence=%s%s"
                    % (overall, passed, len(plan), active_run_id, run_root,
                       result_suffix(reference, readiness_block)))
            if json_summary:
                out(json.dumps(run_summary, ensure_ascii=True, indent=1,
                               default=str))
        return exit_code
    finally:
        reporter.close()
        lock.release()


class _ReadinessStop(Exception):
    """READY 이후 workload 없이 정상 close로 가는 내부 신호 (probe 실패·admission 부족)."""


def _seal_cold_calibration(context: dict, run_root: Path, ready: dict, out) -> dict | None:
    """cold path: coordinator가 ready.json에 실은 calibration candidate를 검증하고 binding을 채운다.

    ready.json의 `calibration`(artifact/input SHA·공통 gain)과 run evidence 파일의 실측 SHA가 같아야
    한다. 하나라도 다르면 None — workload 없이 close한다(§5.1.1 6단계 CALIBRATION_CANDIDATE_DURABLE)."""
    calibration = (ready or {}).get("calibration") or {}
    artifact_path = run_root / "evidence" / "gain_artifact.json"
    input_path = run_root / "evidence" / "gain_artifact_input.json"
    problems = []
    try:
        artifact_bytes = artifact_path.read_bytes()
        input_bytes = input_path.read_bytes()
    except OSError as exc:
        out("[readiness] cold calibration candidate missing in run evidence: %s" % exc)
        return None
    artifact_sha = readiness.sha256_hex(artifact_bytes)
    input_sha = readiness.sha256_hex(input_bytes)
    if calibration.get("artifact_sha256") != artifact_sha:
        problems.append("gain_artifact.json sha differs from ready.json")
    if calibration.get("artifact_input_sha256") != input_sha:
        problems.append("gain_artifact_input.json sha differs from ready.json")
    try:
        gain_input = json.loads(input_bytes.decode("utf-8"))
        common = {k: int(v) for k, v in gain_input["endpoint_common_rx_gain_db"].items()}
        if gain_input.get("source_gain_artifact_sha256") != artifact_sha:
            problems.append("gain_artifact_input.json does not reference gain_artifact.json")
    except (ValueError, KeyError, TypeError) as exc:
        problems.append("gain_artifact_input.json unreadable: %s" % exc)
        common = {}
    if set(common) != {"MASS-A", "MASS-B", "ROC"}:
        problems.append("endpoint common gain incomplete: %s" % sorted(common))
    if calibration.get("quiescent") is not True:
        problems.append("coordinator did not confirm CALIBRATION_TO_SERVICE_QUIESCENT")
    if problems:
        out("[readiness] cold calibration candidate rejected: " + "; ".join(problems))
        return None
    sealed = {
        "source": "readiness_calibration:%s" % context["readiness_id"],
        "artifact_sha256": artifact_sha, "artifact_input_sha256": input_sha,
        "endpoint_common_rx_gain_db": common,
        "calibration_policy_id": readiness_run.CALIBRATION_POLICY_ID,
    }
    context["binding"]["calibration"] = sealed
    context["calibration"]["calibration"] = sealed
    # cold binding이 확정됐으니 applicability를 다시 계산한다 (active와 같은 binding일 수 있다).
    context["applicability"] = readiness.evaluate_applicability(
        context["readiness_snapshot"], context["binding"])
    active = context["readiness_snapshot"].get("active") or {}
    active_digest = ((active.get("record") or {}).get("readiness_digest_sha256")
                     if active.get("valid") else None)
    context["active_same_binding"] = bool(
        active_digest and active_digest == readiness.readiness_digest(context["binding"]))
    out("[readiness] cold calibration candidate sealed: artifact %s… common gain %s "
        "(inactive until this run's readiness release is promoted)"
        % (artifact_sha[:12], common))
    return {
        **sealed,
        "gain_plan_sha256": calibration.get("gain_plan_sha256"),
        "cfo_plan_sha256": calibration.get("cfo_plan_sha256"),
        "host_time_attempts": calibration.get("host_time_attempts"),
        "phase_elapsed_s": calibration.get("phase_elapsed_s"),
        "not_yet_applicable": True,
    }
