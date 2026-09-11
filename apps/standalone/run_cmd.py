"""`vdeter run` — 일상 실행 (Phase I W6, README §1·§8·§10·§11).

여기서 W3~W5의 재료가 **실제로 연결**된다 (이관 조건):
    deploy manifest 실검증 → active 포인터 적재(SHA 결속) →
    qualification-critical 대조(라이브러리 4종 포함) → 불일치 = UNQUALIFIED
    → RF 접근 전 거부.

흐름 (결정 5·13·14·16·17·21, clone-bootstrap v2)
    flock → hardware.toml → installation manifest 적재·재측정 검증 → active
    reference(v2) → identity 정합 → 필수 preflight → critical 대조(installation/
    rf/operation/tooling digest·serial·speed_class·gain·identity·라이브러리) →
    run 디렉터리 할당 → resolved config(rr-p2-17·OPERATIONAL·reference gain) →
    coordinator 세션(bring-up 1회) → 메뉴 또는 인자 workload N개 → close →
    summary·RESULT 줄. Git 상태는 검사하지 않는다.

bring-up 자동 재시도(결정 16): host-time 획득의 일시적 실패로 **명확히
분류된** 경우에만, cleanup PASS·잔존 0·B210 3대 재발견을 확인한 뒤 새
run-id로 최대 1회. 설정·장비·키·UNQUALIFIED·cleanup 실패는 재시도 없음.

출력은 전부 영어. 시나리오 구간은 RF 활성 중에는 `running …`만 내고,
workload 결과가 확정된 뒤 evidence를 한 번 읽어 전송 단위 세로 상세 블록
(scenario_report — `vdeter results`와 같은 렌더러)과 result 요약을 낸다.
종료 코드는 README §11.
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
    scenario_report,
)
from apps.standalone.local_session import (
    LocalCoordinatorSession,
    LocalSessionError,
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
from apps.stage7_service_layer import DEFAULT_BATCH_PLAN  # noqa: E402

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

#: bring-up 자동 재시도 상한 (총 시도 2회 — 결정 16).
MAX_BRINGUP_ATTEMPTS = 2

#: 일반 실행이 결속하는 서비스 phase (README §6 — coordinator 계약).
RUN_RR_PHASE = "rr-p2-17"


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
    불일치 항목을 전부 모아 돌려준다 — 하나라도 있으면 UNQUALIFIED."""
    critical = reference.get("standalone_critical") or {}
    mismatches = []
    if reference.get("schema_version") != 2:
        mismatches.append("active reference is schema v%s (legacy git-bound "
                          "binding); a v2 installation-bound reference is "
                          "required — run `vdeter qualify`"
                          % reference.get("schema_version"))
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
    return {"pass": not mismatches, "mismatches": mismatches}


def gain_input_from_reference(reference: dict) -> dict:
    """coordinator `--gain-artifact` 입력 (gain_artifact_input.json 스키마)."""
    gain = reference["standalone_critical"]["gain"]
    return {
        "endpoint_common_rx_gain_db": gain["endpoint_common_rx_gain_db"],
        "source_gain_artifact_sha256": gain["artifact_sha256"],
    }


#: bring-up 실패 중 재시도를 **금지**하는 신호 (설정·장비·키·결속 오류).
_NON_TRANSIENT_MARKERS = (
    "BINDING_REJECTED", "config 적재 실패", "system plan 적재 실패",
    "spawn 실패", "spawn failed", "trust", "identity", "fingerprint",
    "REQUIRED_UNSET",
    "DEVICE_ERROR", "No devices found", "USB",
)
_HOST_TIME_MARKERS = ("host affine fit FAIL", "acquire_host_time")


def classify_bringup_failure(console_text: str, finalize_summary: dict,
                             usb_observed: dict, declared_serials) -> dict:
    """결정 16의 재시도 허용 분류 — 보수적이다.

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

def prepare(paths: StatePaths, *, repo_root=None,
            hardware_path=None, preflight_fn=None, reference_loader=None,
            environment_manifest=None, progress=None) -> dict:
    """RF 접근 전 게이트 전부. 실패는 RunRefusal(code, message).

    Git·baseline Markdown·standalone-binding은 읽지 않는다. 결속은
    var/installation manifest(적재 + 현재 파일 재측정)와 active reference
    v2의 digest 대조로만 성립한다."""
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
    install_check = installation_manifest.verify_installation(
        repo_root, installation)
    if not install_check["pass"]:
        raise RunRefusal(
            EXIT_UNQUALIFIED,
            "Installation files differ from the setup manifest (%s). Impact: "
            "the recorded installation binding no longer describes this "
            "tree. Action: restore the files, or run `./bin/vdeter setup` "
            "and `vdeter qualify` again."
            % installation_manifest.describe_mismatch(install_check))
    current_installation = install_check["current_record"]

    stage("active reference")
    loader = reference_loader or qualify_cmd.load_active_reference
    reference = loader(paths)
    if reference is None:
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
    comparison = compare_critical(
        reference, installation=current_installation, hardware=hardware,
        preflight=preflight,
        identity_fingerprint=identity_check["fingerprint"],
        environment_manifest=environment_manifest)
    if not comparison["pass"]:
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

    return {
        "repo_root": repo_root, "hardware": hardware,
        "installation": current_installation,
        "installation_sha256": current_installation["installation_sha256"],
        "installation_check": {k: v for k, v in install_check.items()
                               if k != "current_record"},
        "provenance": provenance_module.build_provenance(repo_root),
        "reference": reference,
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
    }


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


def _bring_up(context: dict, paths: StatePaths, *, run_id, session_factory,
              diagnostics, out, clock, reporter=None, mirror=None) -> tuple:
    """run 디렉터리 할당 → config 생성 → 세션 spawn → READY 대기.

    실패는 BringupFailed(summary) — summary는 finalize 결과(정직).
    `reporter`가 있으면 bring-up 진행바(BringupTracker)를 구동한다 — 표시
    전용이며 timeout·판정에는 관여하지 않는다."""
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
    try:
        generated = local_config.generate_run_configs(
            repo_root=context["repo_root"], run_root=run_root, run_id=run_id,
            rr_phase=RUN_RR_PHASE, hardware=context["hardware"],
            installation=context["installation"],
            installation_verified=True,
            provenance=context.get("provenance"),
            run_mode="OPERATIONAL",
            gain_input=gain_input_from_reference(reference),
            environment_manifest=context["environment_manifest"],
            usb_paths=context.get("usb_paths"))
    except local_config.ConfigGenerationError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc
    gain_path = Path(generated["configs_dir"]) / "gain_artifact_input.json"
    write_json_atomic(gain_path, gain_input_from_reference(reference))
    digests = installation_manifest.digests_of(context["installation"])
    write_json_atomic(run_root / "run.json", {
        "artifact_type": "STAGE7_STANDALONE_RUN_V1",
        "run_id": run_id,
        "reference_sha256": reference.get("_reference_sha256"),
        "installation_sha256": digests["installation_sha256"],
        "rf_manifest_sha256": digests["rf_manifest_sha256"],
        "operation_manifest_sha256": digests["operation_manifest_sha256"],
        "tooling_manifest_sha256": digests["tooling_manifest_sha256"],
        "identity_fingerprint": context["identity"]["fingerprint"],
        "requested_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_hashes": generated["resolved_sha256"],
    })

    session = session_factory(
        repo_root=context["repo_root"], run_id=run_id, run_root=run_root,
        configs_dir=generated["configs_dir"], gain_artifact_path=gain_path,
        baseline_sha256=provenance_module.STAGE7_BASELINE_PIN["sha256"],
        phase="mass-roc-session", diagnostics=diagnostics,
        identity_root=paths.identity_dir)
    out(f"[config]    {run_id} generated and validated")
    out("Bringing up hardware session (~20-23 min, once per run)")
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
        # bring-up(최대 ~20분) 중 Ctrl+C/SIGTERM — 전파시키지 않고 정상
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
    return session, run_id, run_root, ready


def _run_poc(poc_ctx, session, *, scenario_id, workload_id, poc_id, out) -> dict | None:
    """한 workload의 Stage 8 PoC를 실제 실행 경로로 돌린다(§5, §11). baseline(none)/미선택이면 None.

    scenario-호환 PoC를 선택해 immutable snapshot을 만들고, 실 child·socket·SDK·engine·Stage 5
    provider로 실행한 뒤 세 독립 판정을 workload result에 붙일 evidence로 돌려준다. device 경계는
    session이 제공한다(RF-off는 software, production은 실 radio — 미제공 시 poc_run이 fail-closed).
    """
    mod = poc_ctx["module"]
    dest = Path(poc_ctx["run_root"]) / "workloads" / workload_id / "poc-snapshot"
    try:
        snapshot = mod.select_snapshot(
            poc_ctx["option"], scenario_id=scenario_id, dest_dir=str(dest),
            poc_id=poc_id, interactive=poc_ctx.get("interactive", True))
    except mod.PocRunError as exc:
        out("  poc refused: %s" % exc)
        return {"status": "REFUSED", "error": str(exc)}
    if snapshot is None:
        return None  # baseline: PoC none (자원 0)
    sock_root = str(Path(poc_ctx["run_root"]) / "poc-sockets")
    try:
        evidence = mod.run_for_workload(
            session, snapshot, scenario_id=scenario_id, run_id=poc_ctx["run_id"],
            workload_id=workload_id, socket_root=sock_root,
            src_path=poc_ctx.get("src_path"))
    except mod.PocRunError as exc:
        out("  poc error: %s" % exc)
        return {"status": "ERROR", "error": str(exc)}
    out("  poc infrastructure=%s attacks=%s radiated_children=%s"
        % (evidence.get("poc_infrastructure"),
           len(evidence.get("attacks") or []),
           (evidence.get("radiation") or {}).get("radiated_children")))
    return evidence


def _run_workload(session, scenario_id, certificate_case, *, out, clock,
                  index=None, total=None, poc_ctx=None, poc_id=None):
    """READY 이후 workload 하나 — 88fa6df와 같은 저 I/O 경로.

    RF 활성 구간(submit→result)에는 `running …`과 settle 안내만 표시하고
    trace 행은 on_trace로 **메모리에 수집만** 한다. 결과 JSON이 확정된 뒤
    (terminal state·verdict·quiescence·non_terminal 포함) 완료 evidence를
    파일당 한 번 읽어(final-result·service_events, scenario_report) 전송
    단위 세로 상세 블록을 출력하고, 마지막에 `result_line` 요약을 낸다.
    service_events 폴링·강제 flush·presentation 파일 갱신·실시간 블록은
    실행 경로에 없다(R1 선택 롤백). 상세 블록 렌더링 실패는 판정·close·
    evidence에 영향을 주지 않는다(경고 한 줄). 판정·timeout·evidence 계약은
    세션 객체가 그대로 소유한다."""
    label = scenario_id + (" (%s)" % certificate_case
                           if scenario_id == "SC-VDE-001" else "")
    out("  running " + label)
    workload_id = session.submit_workload(
        scenario_id, certificate_case,
        on_settle=lambda remaining, prev: out(
            "  settling %.0f s (RX pipeline drain after %s)"
            % (remaining, prev)))
    started = clock()
    trace_rows = []
    result = session.wait_result(workload_id, on_trace=trace_rows.append)
    result.setdefault("wall_clock_s", round(clock() - started, 1))
    result.setdefault("workload_id", workload_id)
    result.setdefault("scenario_id", scenario_id)
    result.setdefault("certificate_case", certificate_case)
    # 결과 확정 후에만 evidence를 한 번 읽는다 (RF 활성 구간 출력 없음).
    for line in scenario_report.workload_report_lines(
            trace_rows, result, run_root=getattr(session, "run_root", None),
            index=index, total=total):
        out(line)
    out(result_line(result))
    # Stage 8 PoC: scenario 판정과 **독립**으로 기록한다(§11.1, scenario verdict 재계산 없음).
    if poc_ctx is not None:
        poc_evidence = _run_poc(poc_ctx, session, scenario_id=scenario_id,
                                workload_id=workload_id, poc_id=poc_id, out=out)
        if poc_evidence is not None:
            result["poc"] = poc_evidence
    return result


_ESCAPE_SEQ_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b[A-Za-z]|[\x00-\x1f\x7f]")


def _clean_choice(raw: str) -> str:
    """메뉴 입력 정리 — 방향키·기능키 ESC 시퀀스와 제어 문자를 제거한다
    (`input()`은 방향키를 '\\x1b[A' 같은 문자열로 넘긴다)."""
    return _ESCAPE_SEQ_RE.sub("", str(raw)).strip()


def _menu_poc_id(poc_ctx, scenario_id, *, out, input_fn):
    """catalog(`--poc-dir`) mode에서 scenario-호환 PoC를 대화형 선택한다(§6.1). 반환 poc_id 또는 None.

    direct(`--poc`)면 고정 package라 menu 없이 None(select_snapshot이 --poc를 쓴다).
    """
    if poc_ctx is None or not poc_ctx["option"].poc_dir:
        return None
    for line in poc_ctx["module"].catalog_menu_lines(poc_ctx["option"], scenario_id):
        out("  " + line)
    try:
        choice = _clean_choice(input_fn("  select poc [id / none] > "))
    except EOFError:
        return "none"
    return choice or "none"


def _menu(session, *, out, input_fn, clock, poc_ctx=None) -> list:
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
            break
        if not choice:
            continue          # 빈 입력·방향키 등 제어 시퀀스만이면 다시 표시
        lowered = choice.lower()
        if lowered in ("q", "quit", "exit"):
            break
        if lowered in ("a", "all"):
            for number, (scenario_id, case) in enumerate(DEFAULT_BATCH_PLAN, 1):
                poc_id = _menu_poc_id(poc_ctx, scenario_id, out=out,
                                      input_fn=input_fn)
                result = _run_workload(session, scenario_id, case,
                                       out=out, clock=clock, index=number,
                                       total=len(DEFAULT_BATCH_PLAN),
                                       poc_ctx=poc_ctx, poc_id=poc_id)
                results.append(result)
                if result.get("status") != "PASS" or not (
                        result.get("quiescence") or {}).get("pass"):
                    out("  stopping batch — session can no longer accept "
                        "workloads")
                    return results
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
                break
            if answer not in ("normal", "forged"):
                out("  unknown certificate case %r (no RF action taken)"
                    % answer)
                continue
            case = answer
        poc_id = _menu_poc_id(poc_ctx, scenario_id, out=out, input_fn=input_fn)
        result = _run_workload(session, scenario_id, case, out=out,
                               clock=clock, poc_ctx=poc_ctx, poc_id=poc_id)
        results.append(result)
        if not (result.get("quiescence") or {}).get("pass"):
            out("  session can no longer accept workloads (quiescence "
                "failed) — closing")
            break
    return results


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
            topology_probe=None) -> int:
    """`vdeter run` 본체. 반환: 종료 코드 (README §11).

    `progress=False`(`--no-progress`)면 진행바 없이 줄 단위 상태만 출력한다.
    표시 계층은 판정·evidence 결과에 영향을 주지 않는다.

    `poc`/`poc_dir`(Stage 8 opt-in)가 주어지면 workload마다 scenario-호환 PoC를 선택·snapshot해
    실제 child·socket·provider로 실행한다. 둘 다 없으면 `poc_run`(stage8) module을 로드하지 않는다
    (기능 OFF 구조적 0). device 경계는 session이 제공한다(`poc_device_seams`)."""
    try:
        plan = plan_for(scenario, certificate_case)
    except RunRefusal as exc:
        out("Refused: " + str(exc))
        return exc.code
    interactive = scenario is None

    # Stage 8 opt-in: --poc/--poc-dir가 있을 때만 sanctioned adapter를 lazy import한다.
    poc_option = None
    poc_run = None
    if poc is not None or poc_dir is not None:
        from apps.standalone import poc_run as poc_run
        try:
            poc_option = poc_run.resolve_option(poc=poc, poc_dir=poc_dir,
                                                interactive=interactive)
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
        gates = progress_module.GateProgress(reporter, total=5)
        try:
            context = prepare(
                paths, repo_root=repo_root,
                hardware_path=hardware_path, preflight_fn=preflight_fn,
                reference_loader=reference_loader,
                environment_manifest=environment_manifest, progress=gates)
        except RunRefusal as exc:
            reporter.finish("FAILED")
            out("Refused (exit %d): %s" % (exc.code, exc))
            return exc.code
        gates.done()
        reference = context["reference"]
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

        attempts = []
        session = None
        active_run_id = None
        run_root = None
        for attempt in range(1, MAX_BRINGUP_ATTEMPTS + 1):
            if attempt > 1:
                out("attempt %d/%d" % (attempt, MAX_BRINGUP_ATTEMPTS))
            try:
                session, active_run_id, run_root, _ready = _bring_up(
                    context, paths, run_id=run_id if attempt == 1 else None,
                    session_factory=session_factory, diagnostics=diagnostics,
                    out=out, clock=clock, reporter=reporter, mirror=mirror)
                attempts.append({"attempt": attempt, "run_id": active_run_id,
                                 "bringup": "READY"})
                break
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
                attempts.append({"attempt": attempt, "run_id": stopped.run_id,
                                 "bringup": "INTERRUPTED",
                                 "finalize": summary})
                write_json_atomic(stopped.run_root / "summary.json", {
                    "artifact_type": "STAGE7_STANDALONE_RUN_SUMMARY_V1",
                    "run_id": stopped.run_id, "status": "FAIL",
                    "interactive": interactive, "interrupted": True,
                    "interrupted_during": "bring-up",
                    "scenarios_passed": 0, "scenarios_run": 0,
                    "scenarios_planned": len(plan),
                    "workloads": [], "close": summary,
                    "attempts": attempts,
                    "reference_sha256": reference.get("_reference_sha256"),
                    "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime()),
                })
                out("Evidence: %s   (summary: vdeter results %s)"
                    % (stopped.run_root, stopped.run_id))
                if not interactive:
                    out("RESULT overall=FAIL scenarios=0/%d run-id=%s "
                        "evidence=%s (interrupted during bring-up)"
                        % (len(plan), stopped.run_id, stopped.run_root))
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
                attempts.append({"attempt": attempt,
                                 "bringup": "FAILED", "error": failed.error,
                                 "finalize": failed.summary,
                                 "classification": classification})
                out("Bring-up failed: %s" % failed.error)
                for line in progress_module.bringup_failure_lines(
                        phase_label=failed.phase_label or "bring-up",
                        finalize_summary=failed.summary,
                        classification=classification,
                        console_text=console_text,
                        run_root=failed.run_root, attempt=attempt,
                        max_attempts=MAX_BRINGUP_ATTEMPTS,
                        declared_serials=context["declared_serials"],
                        usb_observed=usb_now,
                        host_time_detail=(
                            progress_module.host_time_detail_from_evidence(
                                failed.run_root)
                            if failed.run_root is not None else None)):
                    out(line)
                if (retry and attempt < MAX_BRINGUP_ATTEMPTS
                        and classification["transient_host_time"]):
                    out("Classified as transient host-time acquisition "
                        "failure; cleanup confirmed. Retrying once with a "
                        "new run-id (evidence of both attempts is kept).")
                    continue
                if classification["reasons"]:
                    out("No retry: " + "; ".join(classification["reasons"]))
                out("RESULT overall=FAIL scenarios=0/%d run-id=- evidence=%s"
                    % (len(plan), paths.runs_dir))
                return EXIT_FAIL
        if session is None:
            return EXIT_FAIL

        results = []
        exit_code = EXIT_PASS
        interrupted = False
        # SESSION READY 이후: 88fa6df와 같은 저 I/O 경로. 진행바는 이미 닫혔고
        # (bring-up 끝), presentation mirror도 끊는다 — 시나리오 구간에는
        # service_events 폴링·실시간 추적·presentation 파일 갱신이 없다(R1).
        # workload trace·result·evidence 생성은 세션/coordinator 계약 그대로다.
        mirror.set_path(None)
        out = terminal_out
        poc_ctx = None
        if poc_option is not None and poc_option.active:
            poc_ctx = {
                "module": poc_run, "option": poc_option, "run_root": run_root,
                "run_id": active_run_id, "interactive": interactive,
                "input_fn": input_fn,
                "src_path": str(Path(context["repo_root"]) / "VDETER" / "src"),
            }
            out("[poc]       Stage 8 PoC opt-in active (%s)"
                % ("catalog" if poc_option.poc_dir else "direct"))
        try:
            if interactive:
                results = _menu(session, out=out, input_fn=input_fn,
                                clock=clock, poc_ctx=poc_ctx)
            else:
                for number, (scenario_id, case) in enumerate(plan, 1):
                    result = _run_workload(session, scenario_id, case,
                                           out=out, clock=clock,
                                           index=number, total=len(plan),
                                           poc_ctx=poc_ctx)
                    results.append(result)
                    if result.get("status") != "PASS" or not (
                            result.get("quiescence") or {}).get("pass"):
                        break
        except KeyboardInterrupt:
            interrupted = True
            out("Interrupt — closing session (shutdown -> cleanup -> "
                "release B210s). Press Ctrl+C again to force.")
        except LocalSessionError as exc:
            out("Session error: %s" % exc)
            exit_code = EXIT_FAIL
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
                overall = ("PASS" if summary["pass"] and results
                           and passed == len(plan) else "FAIL")
            if topology_after is not None and not topology_after["pass"]:
                overall = "FAIL"
            if overall != "PASS":
                exit_code = exit_code or EXIT_FAIL
            if interrupted:
                exit_code = EXIT_INTERRUPTED
            run_summary = {
                "artifact_type": "STAGE7_STANDALONE_RUN_SUMMARY_V1",
                "run_id": active_run_id, "status": overall,
                "interactive": interactive, "interrupted": interrupted,
                "scenarios_passed": passed, "scenarios_run": len(results),
                "scenarios_planned": planned,
                "workloads": results, "close": summary,
                "usb_topology_after_close": topology_after,
                "attempts": attempts,
                "reference_sha256": reference.get("_reference_sha256"),
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
            }
            write_json_atomic(run_root / "summary.json", run_summary)
            out("Evidence: %s   (summary: vdeter results %s)"
                % (run_root, active_run_id))
            if not interactive:
                out("RESULT overall=%s scenarios=%d/%d run-id=%s evidence=%s"
                    % (overall, passed, len(plan), active_run_id, run_root))
            if json_summary:
                out(json.dumps(run_summary, ensure_ascii=True, indent=1,
                               default=str))
        return exit_code
    finally:
        reporter.close()
        lock.release()
