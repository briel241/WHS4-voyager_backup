"""`vdeter results` — run 목록·요약 재열람 (Phase I W6, README §2·§11 결정 15).

evidence는 `var/runs/<run-id>/`에 그대로 있고, 이 명령은 그 중
`summary.json`(run_cmd가 종료 시 기록)과 `run.json`을 읽어 영어 요약을
보여준다. 파일을 바꾸지 않는다. 복사는 사용자가 임의 수단으로 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from apps.standalone.state import StatePaths

EXIT_OK = 0
EXIT_NOT_FOUND = 2


def _read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def list_runs(paths: StatePaths, *, limit: int = 20) -> list:
    """최신순 run 요약 목록 (summary 없는 run은 status '-')."""
    if not paths.runs_dir.is_dir():
        return []
    entries = []
    for directory in sorted(paths.runs_dir.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        summary = _read_json(directory / "summary.json") or {}
        run_info = _read_json(directory / "run.json") or {}
        if directory.name.startswith("qual-"):
            # qualification ladder phase evidence (`vdeter qualify`) — not a
            # scenario run; scenarios column does not apply.
            status, scenarios = "qualify", "-"
        elif summary:
            status = summary.get("status", "-")
            scenarios = "%s/%s" % (summary.get("scenarios_passed", "-"),
                                   summary.get("scenarios_planned", "-"))
        else:
            # started through `vdeter run` but no summary.json yet: still in
            # progress, or ended without a normal close.
            status, scenarios = "unfinished", "-"
        entries.append({
            "run_id": directory.name,
            "status": status,
            "scenarios": scenarios,
            "finished_utc": summary.get("finished_utc")
            or run_info.get("requested_utc") or "-",
            "path": str(directory),
        })
        if len(entries) >= limit:
            break
    return entries


def show_run(paths: StatePaths, run_id: str, out=print) -> int:
    directory = paths.runs_dir / run_id
    if not directory.is_dir():
        out("No such run: %s (see `vdeter results` for the list)" % run_id)
        return EXIT_NOT_FOUND
    summary = _read_json(directory / "summary.json")
    run_info = _read_json(directory / "run.json") or {}
    out("run-id:      %s" % run_id)
    out("evidence:    %s" % directory)
    if run_info:
        out("requested:   %s" % run_info.get("requested_utc", "-"))
        out("reference:   %s" % (run_info.get("reference_sha256") or "-")[:12])
    if summary is None:
        out("status:      (no summary.json — run did not finish through "
            "`vdeter run`, inspect the evidence directory)")
        return EXIT_OK
    out("status:      %s%s" % (summary.get("status"),
                              "  (interrupted)" if summary.get("interrupted")
                              else ""))
    out("scenarios:   %s passed / %s run / %s planned" % (
        summary.get("scenarios_passed"), summary.get("scenarios_run"),
        summary.get("scenarios_planned")))
    for workload in summary.get("workloads") or []:
        quiescence = (workload.get("quiescence") or {}).get("pass")
        out("  %-12s %-7s %-5s %6s s  quiescence %s" % (
            workload.get("scenario_id", "?"),
            workload.get("certificate_case", ""),
            workload.get("status", "?"),
            workload.get("wall_clock_s", "?"),
            "OK" if quiescence else "FAIL"))
    close = summary.get("close") or {}
    out("close:       %s%s" % (
        "PASS" if close.get("pass") else "FAIL",
        "" if close.get("pass") else "  (" + "; ".join(
            close.get("problems") or []) + ")"))
    attempts = summary.get("attempts") or []
    if len(attempts) > 1:
        out("bring-up:    %d attempts (%s)" % (
            len(attempts), ", ".join(a.get("bringup", "?") for a in attempts)))
    return EXIT_OK


def main_results(paths: StatePaths, run_id: str | None = None,
                 out=print) -> int:
    if run_id:
        return show_run(paths, run_id, out=out)
    entries = list_runs(paths)
    if not entries:
        out("No runs recorded yet (evidence root: %s)" % paths.runs_dir)
        return EXIT_OK
    width = max([len("run-id")] + [len(e["run_id"]) for e in entries])
    out("%-*s %-10s %-9s %s" % (width, "run-id", "status", "scenarios",
                                "finished / requested (UTC)"))
    for entry in entries:
        out("%-*s %-10s %-9s %s" % (width, entry["run_id"], entry["status"],
                                    entry["scenarios"], entry["finished_utc"]))
    out("(show one: vdeter results <run-id>; qualify = qualification ladder "
        "phase evidence, unfinished = no summary.json yet)")
    return EXIT_OK
