"""`vdeter status` — 장치를 열지 않는 현재 권위·readiness·qualification·live session 조회 (spec §9.1).

RF를 송신하거나 장치를 open하지 않는다. 읽는 것: hardware.toml, installation manifest(무결성 + 3층
분류), `var/readiness/state.json`과 참조 record, qualification `active.json`/reference, identity 공개
자료, sysfs/pgrep preflight(장치 open 없음), 환경 manifest. lock을 잡지 않는다(writer가 될 수 없다).

`vdeter run`과 **같은** `readiness.load_readiness_state()`·`readiness.evaluate_applicability()`·
`readiness_run.build_binding()`을 쓴다 — 두 명령이 다른 판정을 낼 수 없다.

출력 축 (§9.1):
    current_authority      QUALIFIED | OPERATIONAL_READY | UNVERIFIED
    operational_readiness  APPLICABLE | STALE | INHIBITED | ABSENT | MIGRATION_REQUIRED (+ STATE_* fail-closed)
    qualification          APPLICABLE | STALE | ABSENT
    live_session           NONE (명령이 끝난 대기 상태; 잔존 process가 보이면 RESIDUAL_PROCESSES)
    workload_rf            REQUIRES_LIVE_PREPARATION | INHIBITED
    reason                 machine-readable code + human-readable action
"""

from __future__ import annotations

import json
from pathlib import Path

from apps.standalone import (
    hardware_profiles,
    identity,
    installation_manifest,
    local_config,
    qualify_cmd,
    readiness,
    readiness_closure,
    readiness_run,
    run_cmd,
)
from apps.standalone.state import StatePaths

EXIT_PASS = 0
EXIT_CONFIG = 2
EXIT_INTERNAL = 5


def collect_status(paths: StatePaths, *, repo_root=None, hardware_path=None,
                   preflight_fn=None, reference_loader=None, environment_manifest=None,
                   machine_id=None, residual_probe=None) -> dict:
    """status 판정 재료 전부 (RF 없음, lock 없음). 실패 항목은 축 값으로 표현하고 예외를 올리지 않는다."""
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    report = {"artifact_type": "VDETER_OPERATIONAL_STATUS_V1", "schema_version": 1,
              "current_authority": "UNVERIFIED", "operational_readiness": None,
              "qualification": "ABSENT", "live_session": "NONE",
              "workload_rf": "REQUIRES_LIVE_PREPARATION",
              "reason": {"code": None, "action": None}, "details": {}}
    details = report["details"]

    try:
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))
    except local_config.ConfigGenerationError as exc:
        details["hardware_error"] = str(exc)
        hardware = None

    try:
        installation = installation_manifest.load_installation(paths)
        details["installation"] = installation_manifest.digests_of(installation)
        details["installation"]["file_count"] = installation.get("file_count")
    except installation_manifest.InstallationError as exc:
        report.update(operational_readiness="STATE_UNREADABLE", workload_rf="INHIBITED")
        report["reason"] = {"code": "INSTALLATION_MANIFEST_INVALID",
                            "action": "run `./bin/vdeter setup`; %s" % exc}
        return report

    closure = readiness_closure.verify_readiness_installation(paths, repo_root, record=installation)
    details["installation_check"] = {k: v for k, v in closure.items() if k != "current_record"}
    current_installation = closure["current_record"]

    snapshot = readiness.load_readiness_state(paths, installation_record=installation)
    details["readiness_state"] = {
        "status": snapshot["status"], "generation": snapshot["generation"],
        "active": ((snapshot.get("active") or {}).get("release")),
        "active_valid": ((snapshot.get("active") or {}).get("valid")),
        "inhibit": ((snapshot.get("inhibit") or {}).get("release")),
        "inhibit_reason": ((snapshot.get("inhibit") or {}).get("reason_code")),
        "orphans": snapshot["orphans"], "history": snapshot["history"],
        "problems": snapshot["problems"],
    }

    loader = reference_loader or qualify_cmd.load_active_reference
    reference = loader(paths)
    identity_check = identity.check_identity_consistency(paths)
    details["identity"] = {"pass": identity_check["pass"], "fingerprint": identity_check["fingerprint"],
                           "errors": identity_check["errors"]}
    if hardware is None or not identity_check["pass"]:
        report.update(operational_readiness=snapshot["status"] if snapshot["fail_closed"] else "STALE")
        report["reason"] = {"code": "STATIC_CONFIG_ERROR",
                            "action": details.get("hardware_error") or "; ".join(identity_check["errors"])}
        report["qualification"] = "STALE" if reference is not None else "ABSENT"
        return report

    if preflight_fn is None:
        from apps.standalone.local_preflight import run_required_preflight

        def preflight_fn():
            return run_required_preflight(hardware=hardware, paths=paths)
    preflight = preflight_fn()
    details["preflight"] = {"pass": preflight["pass"], "failures": preflight.get("failures")}
    residual = (preflight.get("checks") or {}).get("residual_processes")
    if residual:
        report["live_session"] = "RESIDUAL_PROCESSES"
    environment_manifest = environment_manifest or local_config.build_environment_manifest()

    qualification = {"present": reference is not None, "applicable": False, "mismatches": []}
    if reference is not None:
        comparison = run_cmd.compare_critical(
            reference, installation=current_installation, hardware=hardware,
            preflight=preflight, identity_fingerprint=identity_check["fingerprint"],
            environment_manifest=environment_manifest)
        qualification.update(applicable=comparison["pass"], mismatches=comparison["mismatches"],
                             reference_sha256=reference.get("_reference_sha256"),
                             release=(reference.get("qualification") or {}).get("run_id"))
    details["qualification"] = qualification
    report["qualification"] = ("APPLICABLE" if qualification["applicable"]
                               else ("STALE" if qualification["present"] else "ABSENT"))
    # historical qualified releases (삭제·재기록 없음, 표시만)
    details["historical_qualified"] = sorted(
        p.name for p in paths.qualification_releases.glob("*") if p.is_dir())

    if snapshot["fail_closed"] or snapshot["status"] in ("STATE_UNREADABLE", "STATE_CORRUPT"):
        report["operational_readiness"] = ("INHIBITED" if snapshot["status"]
                                           in ("STATE_UNREADABLE", "STATE_CORRUPT")
                                           else snapshot["status"])
        report["workload_rf"] = "INHIBITED"
        report["reason"] = {"code": snapshot["reason_code"],
                            "action": "; ".join(snapshot["problems"]) or "investigate var/readiness"}
        report["current_authority"] = readiness.project_authority(
            qualification_applicable=qualification["applicable"], readiness_applicable=False)
        return report

    if machine_id is None:
        machine_id = readiness_run.read_machine_id()
    placeholder = readiness_run.cold_calibration_placeholder("ready-00000000T000000Z-00000000")
    partial = readiness_run.build_binding(
        installation_record=installation, hardware=hardware, preflight=preflight,
        identity_fingerprint=identity_check["fingerprint"], machine_id=machine_id or "unknown",
        repo_root=repo_root, calibration=placeholder)
    selection = readiness_run.select_calibration_source(
        reference=reference if not run_cmd.is_diagnostic_authority(reference) else None,
        qualification_applicable=qualification["applicable"], readiness_snapshot=snapshot,
        hardware=hardware, preflight=preflight, environment_manifest=environment_manifest,
        installation_record=installation,
        partial_binding=readiness_run.binding_without_calibration(partial))
    binding = dict(partial)
    binding["calibration"] = selection["calibration"] or placeholder
    applicability = readiness.evaluate_applicability(snapshot, binding)
    details["calibration"] = {"mode": selection["mode"], "authority": selection["authority"],
                              "reason": selection["reason"],
                              "source": (selection["calibration"] or {}).get("source")}
    details["applicability"] = applicability
    details["binding_digest"] = (readiness.readiness_digest(binding)
                                 if selection["calibration"] is not None else None)
    report["operational_readiness"] = applicability["operational_readiness"]
    report["current_authority"] = readiness.project_authority(
        qualification_applicable=qualification["applicable"],
        readiness_applicable=applicability["operational_readiness"] == "APPLICABLE")
    if applicability["operational_readiness"] == "INHIBITED":
        report["workload_rf"] = "INHIBITED"
        report["reason"] = {"code": snapshot["reason_code"] or "INHIBITED",
                            "action": "workload RF stays blocked until a `vdeter run` preparation "
                                      "PASSes on this binding and promotes a new release"}
    elif applicability["operational_readiness"] == "STALE":
        report["reason"] = {"code": "READINESS_STALE",
                            "action": "restore the recorded values or let `vdeter run` perform a "
                                      "new bounded preparation: " + "; ".join(applicability["reasons"][:3])}
    elif applicability["operational_readiness"] == "ABSENT":
        report["reason"] = {"code": "UNVERIFIED",
                            "action": "no applicable active operational-readiness record; "
                                      "`vdeter run` performs a bounded preparation (%s path)"
                                      % selection["mode"]}
    else:
        report["reason"] = {"code": "OPERATIONAL_READY",
                            "action": "active readiness applies; every `vdeter run` still performs "
                                      "live preparation in a new session"}
    if not closure["readiness_bound"]["pass"]:
        report["reason"]["code"] = "READINESS_BOUND_FILES_CHANGED"
        report["reason"]["action"] = ("readiness-bound files differ from the setup manifest — "
                                      "run `./bin/vdeter setup` (RF-free) to re-issue it: "
                                      + readiness_closure.describe_bound_mismatch(closure))
        if report["operational_readiness"] == "APPLICABLE":
            report["operational_readiness"] = "STALE"
            report["current_authority"] = readiness.project_authority(
                qualification_applicable=qualification["applicable"], readiness_applicable=False)
    return report


def render(report: dict) -> list:
    lines = [
        "current_authority: %s" % report["current_authority"],
        "operational_readiness: %s" % report["operational_readiness"],
        "qualification: %s" % report["qualification"],
        "live_session: %s" % report["live_session"],
        "workload_rf: %s" % report["workload_rf"],
        "reason: %s — %s" % (report["reason"].get("code"), report["reason"].get("action")),
    ]
    if report["current_authority"] != "QUALIFIED":
        lines.append("note: OPERATIONAL_READY is not QUALIFIED. A full `vdeter qualify` is required for "
                     "official release, publication evidence, or baseline freeze.")
    details = report.get("details") or {}
    state = details.get("readiness_state") or {}
    if state:
        lines.append("readiness_state: status=%s generation=%s active=%s inhibit=%s"
                     % (state.get("status"), state.get("generation"), state.get("active"),
                        state.get("inhibit")))
        for row in state.get("history") or []:
            lines.append("  release %s%s%s status=%s created=%s"
                         % (row["release"], " [active]" if row["active"] else "",
                            "" if row["valid"] else " [INVALID]", row.get("status"),
                            row.get("created_utc")))
        for orphan in (state.get("orphans") or {}).get("readiness") or []:
            lines.append("  orphan %s %s" % (orphan["release"], orphan["status"]))
        for orphan in (state.get("orphans") or {}).get("inhibits") or []:
            lines.append("  orphan inhibit %s %s (%s)" % (orphan["release"], orphan["status"],
                                                          orphan.get("reason_code")))
    qual = details.get("qualification") or {}
    if qual.get("present"):
        lines.append("qualification_reference: %s release=%s applicable=%s"
                     % ((qual.get("reference_sha256") or "")[:12], qual.get("release"),
                        qual.get("applicable")))
        for mismatch in (qual.get("mismatches") or [])[:5]:
            lines.append("  stale: " + mismatch)
    for release in details.get("historical_qualified") or []:
        lines.append("historical_qualified: %s" % release)
    calibration = details.get("calibration") or {}
    if calibration:
        lines.append("calibration: %s (%s)" % (calibration.get("mode"), calibration.get("source")
                                               or calibration.get("reason")))
    check = details.get("installation_check") or {}
    inventory = check.get("inventory_only") or {}
    changed = sum(len(inventory.get(k) or []) for k in ("added", "removed", "changed"))
    if changed:
        lines.append("inventory_only_differences: %d file(s) outside the readiness execution closure "
                     "(recorded only)" % changed)
    return lines


def main_status(paths: StatePaths, *, out=print, json_output: bool = False, **injections) -> int:
    report = collect_status(paths, **injections)
    if json_output:
        out(json.dumps(report, ensure_ascii=True, indent=1, default=str))
    else:
        for line in render(report):
            out(line)
    return EXIT_PASS
