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

    setup = sub.add_parser("setup", help="one-time machine preparation "
                           "(apt online, venv, identity, hardware draft)")
    setup.add_argument("--new-identity", action="store_true",
                       help="explicitly regenerate identity (archives public "
                            "records, destroys the old private key; the "
                            "machine becomes UNQUALIFIED)")
    setup.add_argument("--no-apt", action="store_true",
                       help="do not install apt packages (fails if missing)")

    qualify = sub.add_parser("qualify", help="run the RR qualification "
                             "ladder and issue the active reference")
    qualify.add_argument("--yes", action="store_true",
                         help="skip the interactive confirmation")

    run = sub.add_parser("run", help="run scenarios on a hardware session")
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

    results = sub.add_parser("results", help="list runs or show one summary")
    results.add_argument("run_id", nargs="?", default=None)
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
    from apps.standalone import setup_cmd
    try:
        report = setup_cmd.run_setup(
            paths, apt_install=not args.no_apt,
            regenerate_identity=args.new_identity)
    except setup_cmd.SetupReexec as reexec:
        # 첫 실행 부트스트랩: 시스템 python3에 cryptography가 없으면 방금
        # 만든 var/venv로 같은 명령을 재실행한다 (앞 단계는 멱등).
        out("re-executing setup under %s (cryptography from the lock)"
            % reexec.venv_python)
        argv = [reexec.venv_python, os.path.abspath(__file__), "setup"]
        if args.new_identity:
            argv.append("--new-identity")
        if args.no_apt:
            argv.append("--no-apt")
        os.execv(reexec.venv_python, argv)
        return EXIT_INTERNAL  # execv가 돌아오지 않는다 — 방어적
    except setup_cmd.SetupError as exc:
        out("setup failed: %s" % exc)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - 사용자에게 원인 그대로
        out("setup failed: %s: %s" % (type(exc).__name__, exc))
        return EXIT_INTERNAL
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
    draft = steps["hardware_draft"]
    out("  hardware:   %s %s" % (
        "draft written to" if draft["created"] else "existing", draft["path"]))
    if draft["created"]:
        out("              edit serial/role mapping and set wiring.approved="
            "true before `vdeter qualify`")
    manifest = steps["deploy_manifest"]
    out("  manifest:   sha256 %s (%d files, %s)" % (
        manifest["sha256"][:12], manifest["file_count"],
        "reused" if manifest.get("reused") else "issued"))
    if manifest.get("qualification_invalidated"):
        out("              deployment tree changed — existing qualification "
            "is invalidated; run `vdeter qualify`")
    return EXIT_PASS


def _cmd_qualify(args, paths: StatePaths, out, input_fn) -> int:
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
            answer = input_fn("proceed with RF qualification? [yes/NO] > ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            out("aborted (no RF action taken)")
            return EXIT_CONFIG
    try:
        report = qualify_cmd.run_qualify(paths)
    except qualify_cmd.QualifyError as exc:
        out("qualify refused: %s" % exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        out("qualify interrupted — phase process group cleaned up; active "
            "reference unchanged")
        return EXIT_INTERRUPTED
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
            out=out, input_fn=input_fn)
    if args.command == "results":
        from apps.standalone import results_cmd
        return results_cmd.main_results(paths, args.run_id, out=out)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
