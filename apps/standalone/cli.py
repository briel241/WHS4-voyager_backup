#!/usr/bin/env python3
"""`vdeter` — standalone 단일 명령 진입점 (Phase I W6, 결정 4·5·21).

    vdeter setup [--new-identity] [--no-apt]     기계당 1회 (인터넷 허용)
    vdeter qualify [--yes]                        단독 RR 재자격 (수 시간, RF)
    vdeter run [SCENARIO|all] [--case ...] [...]  일상 실행 (오프라인)
    vdeter results [RUN-ID]                       run 목록·요약

wrapper(`var/venv/bin/vdeter`)가 이 파일을 경로로 실행하므로 sys.path를
여기서 직접 세운다. 출력은 영어. 종료 코드는 README §11.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from apps.standalone.state import StatePaths  # noqa: E402

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_INTERNAL = 5
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vdeter",
        description="VDETER standalone: setup / qualify / run / results")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_progress_flag(sub_parser):
        sub_parser.add_argument(
            "--no-progress", action="store_true",
            help="disable the progress bar; print plain status lines only")

    setup = sub.add_parser("setup", help="one-time machine preparation "
                           "(apt online, venv, identity, hardware draft, "
                           "installation manifest, user launcher)")
    setup.add_argument("--new-identity", action="store_true",
                       help="explicitly regenerate identity (archives public "
                            "records, destroys the old private key; the "
                            "machine becomes UNQUALIFIED)")
    setup.add_argument("--no-apt", action="store_true",
                       help="do not install apt packages (fails if missing)")
    setup.add_argument("--no-launcher", action="store_true",
                       help="do not install/refresh ~/.local/bin/vdeter")
    add_progress_flag(setup)

    qualify = sub.add_parser("qualify", help="run the RR qualification "
                             "ladder and issue the active reference "
                             "(no git, no baseline document needed)")
    qualify.add_argument("--yes", action="store_true",
                         help="skip the interactive confirmation")
    add_progress_flag(qualify)

    run = sub.add_parser("run", help="run scenarios on a hardware session "
                         "(offline; requires an active v2 reference)")
    add_progress_flag(run)
    run.add_argument("scenario", nargs="?", default=None,
                     help="SC-VDE-001 | SC-VDE-005 | SC-VDE-006 | all "
                          "(omit for the interactive menu)")
    run.add_argument("--case", choices=("normal", "forged", "both"),
                     default=None, help="SC-VDE-001 certificate case")
    run.add_argument("--run-id", default=None,
                     help="explicit run-id (refused if it already exists)")
    run.add_argument("--diagnostics", action="store_true",
                     help="raise evidence instrumentation detail")
    run.add_argument("--json", action="store_true",
                     help="print the run summary as JSON at the end")
    run.add_argument("--no-retry", action="store_true",
                     help="disable the single automatic bring-up retry")
    run.add_argument("--poc", default=None, metavar="PATH",
                     help="Stage 8: external PoC package <base>.py to run against "
                          "the selected scenario (opt-in; off by default)")
    run.add_argument("--poc-dir", default=None, metavar="DIR",
                     help="Stage 8: external PoC catalog directory for interactive "
                          "scenario-first PoC selection (opt-in; interactive only)")
    run.add_argument("--rf-off-software", action="store_true",
                     help="Stage 8: run the PoC through a real coordinator process in RF-off "
                          "software mode (no B210; hardware boundary is an explicit software "
                          "double). Verification only, not for production hardware runs.")

    results = sub.add_parser("results", help="list runs or show one summary")
    results.add_argument("run_id", nargs="?", default=None)

    capability = sub.add_parser(
        "capability",
        help="show, or verify-and-issue, this installation's dual-channel capability "
             "attestation (RF-off verification only; starts no transmission and does "
             "not qualify the installation)")
    capability.add_argument(
        "action", nargs="?", default="show", choices=("show", "issue"),
        help="show the current attestation (default), or run this installation's "
             "RF-off verification and issue an attestation bound to it")

    # 개발용 통제 RF 진단 — 정상 `run`의 UNQUALIFIED 거부는 그대로다. 별도 authorization
    # (var/diagnostic)에만 결속되며 reference를 발급·승격하지 않는다 (실행 명세 §5A).
    diag = sub.add_parser(
        "diag",
        help="development-time controlled RF diagnostics on an UNQUALIFIED candidate "
             "(separate authorization; never a qualification, never promotes)")
    diag_sub = diag.add_subparsers(dest="diag_command", required=True)
    diag_authorize = diag_sub.add_parser(
        "authorize",
        help="issue a diagnostic authorization bound to this installation, hardware "
             "declaration, identity, a verified calibration source and a test plan "
             "(RF-off; opens no device)")
    diag_authorize.add_argument(
        "--calibration-reference", required=True, metavar="PATH",
        help="qualification reference (with its .sha256 sidecar) whose gain artifact is "
             "the calibration source; refused unless its hardware/wiring/profile/library "
             "bindings equal this candidate")
    diag_authorize.add_argument(
        "--plan", required=True, metavar="PATH",
        help="JSON test plan: label, allowed_scenarios, poc_allowed, max_runs, "
             "max_workloads_per_run, expires_utc [, max_hold_after_ready_s]")
    diag_run = diag_sub.add_parser(
        "run",
        help="run the authorized diagnostic session (one bring-up attempt, no automatic "
             "retry; the result is always NOT_QUALIFIED)")
    add_progress_flag(diag_run)
    diag_run.add_argument(
        "scenario", nargs="*", default=[],
        help="zero or more of SC-VDE-001 | SC-VDE-005 | SC-VDE-006 (none = RX-only "
             "session: bring-up, optional hold, close)")
    diag_run.add_argument(
        "--hold-after-ready", type=float, default=0.0, metavar="SECONDS",
        help="RX-only: keep the READY session up this long before closing "
             "(bounded by the plan)")
    diag_run.add_argument("--diagnostics", action="store_true",
                          help="raise evidence instrumentation detail")
    diag_run.add_argument("--json", action="store_true",
                          help="print the run summary as JSON at the end")
    diag_run.add_argument("--poc", default=None, metavar="PATH",
                          help="Stage 8 PoC package <base>.py (only if the plan allows PoC)")
    diag_run.add_argument("--poc-dir", default=None, metavar="DIR",
                          help="Stage 8 PoC catalog directory (only if the plan allows PoC)")
    diag_run.add_argument(
        "--rf-off-software", action="store_true",
        help="RF-off software session (no B210; verification only)")
    diag_sub.add_parser("status",
                        help="show the diagnostic authorization and its ledger")
    return parser


def _install_signal_handlers() -> None:
    """SIGTERM도 첫 Ctrl+C와 같은 정상 종료 절차를 타게 한다 (결정 17)."""
    def to_interrupt(_signum, _frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, to_interrupt)
    except (ValueError, OSError, AttributeError):
        pass


def _cmd_setup(args, paths: StatePaths, out) -> int:
    from apps.standalone import progress as progress_module
    from apps.standalone import setup_cmd
    reporter = progress_module.make_reporter(
        plain=getattr(args, "no_progress", False), sink=out,
        scope="SETUP", evidence_log=paths.var_dir / "progress" / "setup.log")
    setup_progress = progress_module.SetupProgress(reporter)
    out = reporter.log
    try:
        report = setup_cmd.run_setup(
            paths, apt_install=not args.no_apt,
            regenerate_identity=args.new_identity,
            install_launcher=not getattr(args, "no_launcher", False),
            progress=setup_progress)
    except setup_cmd.SetupReexec as reexec:
        # 첫 실행 부트스트랩: 시스템 python3에 cryptography가 없으면 방금
        # 만든 var/venv로 같은 명령을 재실행한다 (앞 단계는 멱등).
        reporter.finish("done", reexec="var/venv")
        reporter.close()
        out("re-executing setup under %s (cryptography from the lock)"
            % reexec.venv_python)
        argv = [reexec.venv_python, os.path.abspath(__file__), "setup"]
        for flag, name in (("--new-identity", "new_identity"),
                           ("--no-apt", "no_apt"),
                           ("--no-launcher", "no_launcher"),
                           ("--no-progress", "no_progress")):
            if getattr(args, name, False):
                argv.append(flag)
        os.execv(reexec.venv_python, argv)
        return EXIT_INTERNAL  # execv가 돌아오지 않는다 — 방어적
    except setup_cmd.SetupError as exc:
        reporter.finish("FAILED")
        reporter.close()
        out("setup failed: %s" % exc)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - 사용자에게 원인 그대로
        reporter.finish("FAILED")
        reporter.close()
        out("setup failed: %s: %s" % (type(exc).__name__, exc))
        return EXIT_INTERNAL
    setup_progress.done()
    reporter.close()
    steps = report["steps"]
    out("setup complete")
    out("  apt:        missing=%s" % (steps["apt_plan"]["missing"] or "none"))
    if "venv" in steps:
        out("  venv:       %s (lock %s)" % (
            "created" if steps["venv"]["created"] else "present",
            steps["venv"]["lock"]))
        out("  wrapper:    %s" % steps["wrapper"]["path"])
    ident = steps["identity"]
    out("  identity:   %s (fingerprint %s)" % (
        "created" if ident.get("created") else "unchanged",
        ident.get("fingerprint")))
    if ident.get("destroyed_private_key"):
        out("              previous private key destroyed; public records "
            "archived at %s" % ident.get("archived_to"))
        out("              this machine is now UNQUALIFIED — run "
            "`vdeter qualify`")
    discovery = steps.get("b210_discovery") or {}
    observed = discovery.get("observed") or {}
    out("  b210:       %d device(s) discovered%s" % (
        discovery.get("count", 0),
        (" — " + ", ".join(
            "%s usb=%s %sM" % (serial, info.get("usb_path"),
                               info.get("speed_mbps"))
            for serial, info in sorted(observed.items()))) if observed
        else " (connect the three B210s before `vdeter qualify`)"))
    draft = steps["hardware_draft"]
    out("  hardware:   %s %s" % (
        "draft written to" if draft["created"] else "existing", draft["path"]))
    if draft["created"]:
        label = draft.get("expected_label")
        if label:
            applied = draft.get("expected_applied") or {}
            roles = applied.get("endpoints") or {}
            out("              expected profile %s: %s; hub asset %s (expected values, "
                "not confirmed)" % (
                    label,
                    ", ".join("%s=%s" % (role, roles.get(role) or "REQUIRED_FILL")
                              for role in ("MASS-A", "MASS-B", "ROC")),
                    applied.get("usb_hub.asset_id") or "REQUIRED_FILL"))
            for note in draft.get("mismatches") or []:
                out("              mismatch: " + note)
        out("              review the values, fill REQUIRED_FILL, record the power "
            "attestations only after a physical check, then set wiring.approved="
            "true before `vdeter qualify`")
    inst = steps["installation"]
    out("  install:    installation %s (%d files; rf %s · operation %s · "
        "tooling %s; %s)" % (
            inst["installation_sha256"][:12], inst["file_count"],
            inst["rf_manifest_sha256"][:12],
            inst["operation_manifest_sha256"][:12],
            inst["tooling_manifest_sha256"][:12],
            "unchanged" if inst.get("reused") else "written"))
    if inst.get("qualification_invalidated"):
        out("              installation files changed (%s) — existing "
            "qualification is invalidated; run `vdeter qualify`"
            % ", ".join(inst.get("changed_domains") or ["digest"]))
    launcher = steps.get("user_launcher")
    if launcher:
        out("  launcher:   %s (%s)" % (launcher["path"], launcher["status"]))
        if launcher.get("message"):
            out("              " + launcher["message"])
        if launcher.get("path_hint"):
            for line in launcher["path_hint"].splitlines():
                out("              " + line)
    return EXIT_PASS


def _cmd_qualify(args, paths: StatePaths, out, input_fn) -> int:
    from apps.standalone import progress as progress_module
    from apps.standalone import qualify_cmd
    out("vdeter qualify — RR qualification ladder (RF transmission)")
    out("  steps: RR-P0 -> RR-P1 (+gain) -> candidate -> RR-P2-17 -> "
        "RR-P3-fault -> verification bundle -> final builder -> promote")
    out("  scope: Link 19 implementation present, not exercised by this "
        "qualification, hardware performance not guaranteed; operational "
        "scenarios use Link 11/17 only")
    out("  scope: RR-P3-fault judges the six software fault subcases only; "
        "physical USB detach recovery is not exercised (B210 hot-unplug/"
        "re-enumeration recovery not guaranteed; session close and next-run "
        "B210 rediscovery verified as before)")
    out("  duration: several hours; the machine's active reference will be "
        "replaced only if every step passes")
    existing = qualify_cmd.load_active_reference(paths)
    if existing is not None:
        out("  note: this machine is currently QUALIFIED (reference %s); a "
            "successful ladder replaces it" % (
                existing.get("_reference_sha256") or "")[:12])
    if not args.yes:
        try:
            answer = input_fn("proceed with RF qualification? [y/N] > ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out("aborted (no RF action taken)")
            return EXIT_CONFIG
    reporter = progress_module.make_reporter(
        plain=getattr(args, "no_progress", False), sink=out,
        scope="QUALIFY",
        evidence_log=paths.var_dir / "progress" / "qualify.log")
    qualify_progress = progress_module.QualifyProgress(reporter)
    out = reporter.log
    try:
        report = qualify_cmd.run_qualify(paths, progress=qualify_progress)
    except qualify_cmd.QualifyError as exc:
        reporter.finish("FAILED")
        reporter.close()
        out("qualify refused: %s" % exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        reporter.finish("INTERRUPTED")
        reporter.close()
        out("qualify interrupted — phase process group cleaned up; active "
            "reference unchanged")
        return EXIT_INTERRUPTED
    finally:
        reporter.close()
    for phase, info in (report.get("phases") or {}).items():
        out("  %-12s rc=%s tc=%s" % (phase, info.get("rc"),
                                     info.get("tc_status")))
    if report.get("status") == "QUALIFIED":
        out("QUALIFIED — active reference %s (release %s)" % (
            report["reference"]["sha256"][:12], report["reference"]["release"]))
        for key, value in qualify_cmd.QUALIFICATION_LIMITATIONS.items():
            if not key.endswith("note"):
                out("  limitation: %s = %s" % (key, value))
        return EXIT_PASS
    out("NOT QUALIFIED — " + "; ".join(report.get("errors") or ["unknown"]))
    out("active reference unchanged")
    return EXIT_FAIL


def main(argv=None, *, paths: StatePaths | None = None, out=print,
         input_fn=input) -> int:
    args = build_parser().parse_args(argv)
    paths = paths or StatePaths()
    _install_signal_handlers()
    if args.command == "setup":
        return _cmd_setup(args, paths, out)
    if args.command == "qualify":
        return _cmd_qualify(args, paths, out, input_fn)
    if args.command == "run":
        from apps.standalone import run_cmd
        return run_cmd.execute(
            paths, scenario=args.scenario, certificate_case=args.case,
            run_id=args.run_id, diagnostics=args.diagnostics,
            json_summary=args.json, retry=not args.no_retry,
            progress=not getattr(args, "no_progress", False),
            poc=getattr(args, "poc", None), poc_dir=getattr(args, "poc_dir", None),
            rf_off_software=getattr(args, "rf_off_software", False),
            out=out, input_fn=input_fn)
    if args.command == "diag":
        from apps.standalone import diag_cmd
        if args.diag_command == "authorize":
            try:
                diag_cmd.authorize(
                    paths, calibration_reference=args.calibration_reference,
                    plan_path=args.plan, out=out)
            except diag_cmd.DiagError as exc:
                out("Refused (exit %d): %s" % (exc.code, exc))
                return exc.code
            return EXIT_PASS
        if args.diag_command == "run":
            return diag_cmd.run(
                paths, scenarios=list(args.scenario), poc=args.poc,
                poc_dir=args.poc_dir, rf_off_software=args.rf_off_software,
                diagnostics=args.diagnostics,
                hold_after_ready_s=args.hold_after_ready, json_summary=args.json,
                progress=not getattr(args, "no_progress", False),
                out=out, input_fn=input_fn)
        if args.diag_command == "status":
            return diag_cmd.status(paths, out=out)
        return EXIT_CONFIG
    if args.command == "results":
        from apps.standalone import results_cmd
        return results_cmd.main_results(paths, args.run_id, out=out)
    if args.command == "capability":
        from apps.standalone import capability_issue_cmd
        return capability_issue_cmd.main(args, paths, out=out)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
