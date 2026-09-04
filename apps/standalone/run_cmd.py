"""`vdeter run` — 일상 실행 (Phase I W6, README §1·§8·§10·§11).

여기서 W3~W5의 재료가 **실제로 연결**된다 (이관 조건):
    deploy manifest 실검증 → active 포인터 적재(SHA 결속) →
    qualification-critical 대조(라이브러리 4종 포함) → 불일치 = UNQUALIFIED
    → RF 접근 전 거부.

흐름 (결정 5·13·14·16·17·21)
    flock → binding·hardware.toml → deploy manifest 검증 → active reference
    → identity 정합 → 필수 preflight → critical 대조 → run 디렉터리 할당 →
    resolved config(rr-p2-17·OPERATIONAL·reference gain) → coordinator
    세션(bring-up 1회) → 메뉴 또는 인자 workload N개 → close → summary·
    RESULT 줄.

bring-up 자동 재시도(결정 16): host-time 획득의 일시적 실패로 **명확히
분류된** 경우에만, cleanup PASS·잔존 0·B210 3대 재발견을 확인한 뒤 새
run-id로 최대 1회. 설정·장비·키·UNQUALIFIED·cleanup 실패는 재시도 없음.

출력은 전부 영어, 시나리오 진행은 trace 발생 즉시 workload 기준 경과
타임스탬프로 흘린다 (결정 21). 종료 코드는 README §11.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from apps.standalone import deploy_manifest, identity, local_config, qualify_cmd
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
    def __init__(self, summary: dict, error: str) -> None:
        super().__init__(error)
        self.summary = summary
        self.error = error


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


def result_line(result: dict) -> str:
    quiescence = result.get("quiescence") or {}
    line = "  result: %s · %s s · quiescence %s" % (
        result.get("status"), result.get("wall_clock_s", "?"),
        "OK" if quiescence.get("pass") else "FAIL")
    if result.get("status") != "PASS":
        scenario = result.get("scenario") or {}
        reason = scenario.get("error") or result.get("error")
        if reason:
            line += "\n  reason: " + str(reason)[:200]
    return line


# ------------------------------------------------------------ 게이트

def compare_critical(reference: dict, *, deploy_sha256: str,
                     hardware: dict, preflight: dict,
                     identity_fingerprint: str,
                     environment_manifest: dict) -> dict:
    """현재 qualification-critical 값 ↔ active reference 대조 (README §8).

    불일치 항목을 전부 모아 돌려준다 — 하나라도 있으면 UNQUALIFIED."""
    critical = reference.get("standalone_critical") or {}
    mismatches = []
    if critical.get("deploy_manifest_sha256") != deploy_sha256:
        mismatches.append("deploy manifest SHA differs from the qualified "
                          "deployment (code tree changed after qualify)")
    if critical.get("platform_profile") != hardware.get("platform_profile"):
        mismatches.append("platform_profile differs (%r qualified, %r "
                          "declared)" % (critical.get("platform_profile"),
                                         hardware.get("platform_profile")))
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
    "spawn 실패", "trust", "identity", "fingerprint", "REQUIRED_UNSET",
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

def prepare(paths: StatePaths, *, repo_root=None, binding_path=None,
            hardware_path=None, preflight_fn=None, reference_loader=None,
            environment_manifest=None) -> dict:
    """RF 접근 전 게이트 전부. 실패는 RunRefusal(code, message)."""
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    paths.ensure_var()
    try:
        binding = local_config.load_standalone_binding(
            binding_path if binding_path is not None else
            (repo_root / "VDETER" / "configs" / "stage7"
             / "standalone-binding.json"))
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))
    except local_config.ConfigGenerationError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc

    manifest, manifest_sha = deploy_manifest.load_manifest(
        paths.var_dir / "deploy-manifest.json")
    if manifest is None:
        raise RunRefusal(EXIT_UNQUALIFIED,
                         "No deploy manifest — run `vdeter setup` first, "
                         "then `vdeter qualify`.")
    deploy_check = deploy_manifest.verify(repo_root, manifest)
    if not deploy_check["pass"]:
        raise RunRefusal(
            EXIT_UNQUALIFIED,
            "Deployment tree differs from the qualified manifest "
            "(missing=%s, mismatch=%s, untracked=%s). Impact: the active "
            "qualification no longer binds this code. Action: restore the "
            "tree, or run `vdeter setup` and `vdeter qualify` again."
            % (deploy_check["missing"][:3], deploy_check["mismatch"][:3],
               deploy_check["untracked"][:3]))

    loader = reference_loader or qualify_cmd.load_active_reference
    reference = loader(paths)
    if reference is None:
        raise RunRefusal(
            EXIT_UNQUALIFIED,
            "UNQUALIFIED: no valid active qualification reference on this "
            "machine. Impact: RF execution is refused. Action: run "
            "`vdeter qualify` (RR ladder, several hours).")

    identity_check = identity.check_identity_consistency(paths)
    if not identity_check["pass"]:
        raise RunRefusal(EXIT_CONFIG,
                         "Identity material is inconsistent: "
                         + "; ".join(identity_check["errors"])
                         + ". Action: `vdeter setup --new-identity` then "
                         "`vdeter qualify`.")

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

    environment_manifest = (environment_manifest
                            or local_config.build_environment_manifest())
    comparison = compare_critical(
        reference, deploy_sha256=manifest_sha, hardware=hardware,
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
        "repo_root": repo_root, "binding": binding, "hardware": hardware,
        "manifest_sha256": manifest_sha, "reference": reference,
        "preflight": preflight, "identity": identity_check,
        "environment_manifest": environment_manifest, "disk": disk,
        "declared_serials": [
            hardware["endpoints"][role]["serial"]
            for role in ("MASS-A", "MASS-B", "ROC")],
    }


# ------------------------------------------------------------ 세션 실행

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
              diagnostics, out, clock) -> tuple:
    """run 디렉터리 할당 → config 생성 → 세션 spawn → READY 대기.

    실패는 BringupFailed(summary) — summary는 finalize 결과(정직)."""
    try:
        run_id, run_root = allocate_run_dir(paths.runs_dir, run_id)
    except StateError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc
    reference = context["reference"]
    try:
        generated = local_config.generate_run_configs(
            repo_root=context["repo_root"], run_root=run_root, run_id=run_id,
            rr_phase=RUN_RR_PHASE, hardware=context["hardware"],
            binding=context["binding"],
            deployment_manifest_relpath=deploy_manifest.MANIFEST_RELPATH,
            deployment_manifest_sha256=context["manifest_sha256"],
            deploy_verified=True, run_mode="OPERATIONAL",
            gain_input=gain_input_from_reference(reference),
            environment_manifest=context["environment_manifest"])
    except local_config.ConfigGenerationError as exc:
        raise RunRefusal(EXIT_CONFIG, str(exc)) from exc
    gain_path = Path(generated["configs_dir"]) / "gain_artifact_input.json"
    write_json_atomic(gain_path, gain_input_from_reference(reference))
    write_json_atomic(run_root / "run.json", {
        "artifact_type": "STAGE7_STANDALONE_RUN_V1",
        "run_id": run_id,
        "reference_sha256": reference.get("_reference_sha256"),
        "deploy_manifest_sha256": context["manifest_sha256"],
        "identity_fingerprint": context["identity"]["fingerprint"],
        "requested_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_hashes": generated["resolved_sha256"],
    })

    session = session_factory(
        repo_root=context["repo_root"], run_id=run_id, run_root=run_root,
        configs_dir=generated["configs_dir"], gain_artifact_path=gain_path,
        baseline_sha256=context["binding"]["baseline_sha256"],
        phase="mass-roc-session", diagnostics=diagnostics,
        identity_root=paths.identity_dir)
    out(f"[config]    {run_id} generated and validated")
    out("Bringing up hardware session (~20-23 min, once per run)")
    started = clock()
    try:
        session.start()
        ready = session.wait_ready(
            on_stage=lambda line: out("  %s %s" % (stamp(clock() - started),
                                                  line)))
    except LocalSessionError as exc:
        summary = _close_with_interrupt_guard(session, out)
        summary["console_log_path"] = str(
            getattr(session, "console_log_path", ""))
        raise BringupFailed(summary, str(exc)) from exc
    except KeyboardInterrupt:
        # bring-up(최대 ~20분) 중 Ctrl+C/SIGTERM — 전파시키지 않고 정상
        # close(shutdown 마커→cleanup→B210 반환)를 먼저 마친다. close 중
        # 두 번째 인터럽트는 guard가 TERM/KILL로 에스컬레이션한다 (결정 17).
        out("Interrupt during bring-up — closing session (shutdown -> "
            "cleanup -> release B210s). Press Ctrl+C again to force.")
        summary = _close_with_interrupt_guard(session, out)
        raise BringupInterrupted(summary, run_id, run_root) from None
    out("  %s SESSION READY" % stamp(clock() - started))
    return session, run_id, run_root, ready


def _emit_trace(row, elapsed_s, out):
    line = render_trace_event(row, elapsed_s)
    if line:
        out(line)


def _run_workload(session, scenario_id, certificate_case, *, out, clock):
    label = scenario_id + (" (%s)" % certificate_case
                           if scenario_id == "SC-VDE-001" else "")
    out("  running " + label)
    workload_id = session.submit_workload(
        scenario_id, certificate_case,
        on_settle=lambda remaining, prev: out(
            "  settling %.0f s (RX pipeline drain after %s)"
            % (remaining, prev)))
    started = clock()
    result = session.wait_result(
        workload_id,
        on_trace=lambda row: _emit_trace(row, clock() - started, out))
    result.setdefault("wall_clock_s", round(clock() - started, 1))
    result.setdefault("workload_id", workload_id)
    result.setdefault("scenario_id", scenario_id)
    result.setdefault("certificate_case", certificate_case)
    out(result_line(result))
    return result


def _menu(session, *, out, input_fn, clock) -> list:
    ordered = sorted(SCENARIOS)
    results = []
    while True:
        out("")
        for index, scenario_id in enumerate(ordered, 1):
            out("  %d) %s  %s" % (index, scenario_id, SCENARIOS[scenario_id]))
        out("  a) run all (005 -> 006 -> 001 normal -> 001 forged)")
        out("  q) quit")
        try:
            choice = input_fn("select > ").strip()
        except EOFError:
            break
        if not choice:
            continue
        lowered = choice.lower()
        if lowered in ("q", "quit", "exit"):
            break
        if lowered in ("a", "all"):
            for scenario_id, case in DEFAULT_BATCH_PLAN:
                result = _run_workload(session, scenario_id, case,
                                       out=out, clock=clock)
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
        result = _run_workload(session, scenario_id, case, out=out,
                               clock=clock)
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
            environment_manifest=None, repo_root=None, binding_path=None,
            hardware_path=None, out=print, input_fn=input,
            clock=time.monotonic) -> int:
    """`vdeter run` 본체. 반환: 종료 코드 (README §11)."""
    try:
        plan = plan_for(scenario, certificate_case)
    except RunRefusal as exc:
        out("Refused: " + str(exc))
        return exc.code
    interactive = scenario is None

    lock = SingleInstanceLock(paths.lock_path)
    try:
        lock.acquire(diagnostics={"command": "run"})
    except StateError as exc:
        out("Refused: " + str(exc))
        return EXIT_INTERNAL
    try:
        try:
            context = prepare(
                paths, repo_root=repo_root, binding_path=binding_path,
                hardware_path=hardware_path, preflight_fn=preflight_fn,
                reference_loader=reference_loader,
                environment_manifest=environment_manifest)
        except RunRefusal as exc:
            out("Refused (exit %d): %s" % (exc.code, exc))
            return exc.code
        reference = context["reference"]
        out("VDETER standalone — QUALIFIED (reference %s, issued %s)"
            % ((reference.get("_reference_sha256") or "")[:12],
               reference.get("issued_utc", "?")))
        observed = context["preflight"]["checks"]["b210"]["observed_by_role"]
        out("[preflight] devices 3/3 (%s)" % " · ".join(
            "%s %s %s" % (role, info["serial"],
                          "USB3" if info["speed_class"] == "super" else "USB2")
            for role, info in sorted(observed.items()) if info))
        out("[preflight] identity, config schema, residual processes, deploy "
            "manifest — OK")
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
            try:
                session, active_run_id, run_root, _ready = _bring_up(
                    context, paths, run_id=run_id if attempt == 1 else None,
                    session_factory=session_factory, diagnostics=diagnostics,
                    out=out, clock=clock)
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
                classification = classify_bringup_failure(
                    console_text, failed.summary, usb_probe(),
                    context["declared_serials"])
                attempts.append({"attempt": attempt,
                                 "bringup": "FAILED", "error": failed.error,
                                 "finalize": failed.summary,
                                 "classification": classification})
                out("Bring-up failed: %s" % failed.error)
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
        try:
            if interactive:
                results = _menu(session, out=out, input_fn=input_fn,
                                clock=clock)
            else:
                for scenario_id, case in plan:
                    result = _run_workload(session, scenario_id, case,
                                           out=out, clock=clock)
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
                exit_code = EXIT_FAIL
            passed = sum(1 for r in results if r.get("status") == "PASS")
            planned = len(plan) if not interactive else len(results)
            if interactive:
                overall = ("PASS" if summary["pass"] and passed == len(results)
                           else "FAIL")
            else:
                overall = ("PASS" if summary["pass"] and results
                           and passed == len(plan) else "FAIL")
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
        lock.release()
