"""`vdeter results` — run 목록·저장 evidence 상세 재열람 (Phase I W6, README §2·§11 결정 15).

evidence는 `var/runs/<run-id>/`에 그대로 있고, 이 명령은 그것을 **읽기만** 한다:
파일 생성·수정·mtime 변경·lock 없음, coordinator·B210·RF 접근 없음.

- `vdeter results`            : 최신순 목록(간결). qualify = 자격 사다리 phase evidence,
                                unfinished = summary.json 없음.
- `vdeter results <run-id>`   : 저장된 실행 전체를 상세 재출력 — RUN HEADER / BINDING /
                                BRING-UP / WORKLOADS(전송 단위 세로 블록, `vdeter run`과
                                같은 renderer = scenario_report) / CLOSE-CLEANUP /
                                FINAL SUMMARY. 판정을 다시 계산하지 않고 기록된 값만
                                보여 주며, 없는 값은 `not recorded`, 서로 모순되는
                                기록은 `[WARNING]`으로 표시한다. 미완료·손상 evidence는
                                있는 부분만 보여 준다(fail-soft).

읽는 파일(각 한 번): summary.json, run.json, resolved-configs/{installation-state.json,
gain_artifact_input.json, endpoints/*.toml}, presentation/progress.log(bring-up 단계
fallback), session/results/*.json(summary가 없을 때), workloads/<wid>/workload-trace.jsonl,
workloads/<wid>/mass-roc-events/final-result.json(result에 scenario가 없을 때),
evidence/service_events/<EP>.jsonl, var/qualification/active.json(release 이름 대조).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from apps.standalone import scenario_report
from apps.standalone.state import StatePaths

EXIT_OK = 0
EXIT_NOT_FOUND = 2

NOT_RECORDED = scenario_report.NOT_RECORDED
MAJOR = scenario_report.MAJOR
LIST_HINT = "`vdeter results <run-id>` shows the complete recorded report."

#: bring-up 단계 (coordinator phase_elapsed_s 키 → 표시 이름, 누적 초 순서).
_BRINGUP_PHASES = (
    ("prepare_configure_arm", "Endpoint startup / prepare-configure-arm"),
    ("acquire_host_time", "Host-time acquisition"),
    ("rf_time_pre", "RF-time PRE validation"),
    ("active_bridge", "Active bridge commit"),
    ("rf_time_post", "RF-time POST validation"),
    ("service_running", "Service session READY"),
)
_PROGRESS_DONE_RE = re.compile(
    r"^(?P<utc>\S+) \[BRINGUP\] (?:100%|done) (?P<label>.+?)"
    r"(?: [a-z_]+=\S+)* overall=(?P<overall>\d+)%(?: [a-z_]+=\S+)* elapsed=(?P<elapsed>\d+:\d\d)")
_SERIAL_RE = re.compile(r'device_selector\s*=\s*"serial=([A-Za-z0-9]+)"')
_ROLE_RE = re.compile(r'^role\s*=\s*"([A-Z_]+)"', re.M)


def _read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _read_text(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def _short(sha) -> str:
    return (sha or "")[:16] if sha else NOT_RECORDED


_b = scenario_report.fmt_value      # 공개 helper (bool → true/false, None → not recorded)
_f = scenario_report.as_float


def _mmss(seconds) -> str:
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return NOT_RECORDED
    return "%02d:%02d" % (seconds // 60, seconds % 60)


# ------------------------------------------------------------ 목록

def list_runs(paths: StatePaths, *, limit: int = 20) -> list:
    """최신순 run 요약 목록 (summary 없는 run은 status 'unfinished', qual-* 은 'qualify')."""
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
            if summary.get("interrupted"):
                status += "*"
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


# ------------------------------------------------------------ 상세

def _bringup_stages(summary: dict, directory: Path) -> tuple:
    """(단계 목록[(label, cumulative_s, delta_s)], source). coordinator의 phase_elapsed_s
    (summary.close.closed) 우선, 없으면 presentation/progress.log의 100%/done 줄."""
    closed = ((summary.get("close") or {}).get("closed") or {}) if summary else {}
    elapsed = closed.get("phase_elapsed_s") if isinstance(closed.get("phase_elapsed_s"), dict) else None
    stages = []
    if elapsed:
        previous = 0.0
        for key, label in _BRINGUP_PHASES:
            value = _f(elapsed.get(key))
            if value is None:
                stages.append((label, None, None))
                continue
            stages.append((label, value, value - previous))
            previous = value
        return stages, "coordinator phase_elapsed_s"
    text = _read_text(directory / "presentation" / "progress.log")
    for line in text.splitlines():
        m = _PROGRESS_DONE_RE.match(line.strip())
        if m:
            stages.append((m.group("label").strip(), m.group("elapsed"), None))
    return stages, "presentation/progress.log"


def _endpoint_bindings(directory: Path) -> list:
    """resolved-configs/endpoints/*.toml → [(endpoint, role, serial)] (읽기만)."""
    found = []
    endpoints_dir = directory / "resolved-configs" / "endpoints"
    if not endpoints_dir.is_dir():
        return found
    for toml_path in sorted(endpoints_dir.glob("*.toml")):
        text = _read_text(toml_path)
        serial = _SERIAL_RE.search(text)
        role = _ROLE_RE.search(text)
        found.append((toml_path.stem.upper(), role.group(1) if role else NOT_RECORDED,
                      serial.group(1) if serial else NOT_RECORDED))
    return found


def _usb_speed_map(summary: dict, results: list) -> dict:
    closed = ((summary.get("close") or {}).get("closed") or {}) if summary else {}
    cleanup = closed.get("cleanup") or {}
    speeds = cleanup.get("usb_speed") if isinstance(cleanup.get("usb_speed"), dict) else None
    if not speeds:
        for result in results:
            q = (result or {}).get("quiescence") or {}
            if isinstance(q.get("usb_speed"), dict):
                speeds = q["usb_speed"]
                break
    return speeds or {}


def _speed_class(values) -> str:
    if not values:
        return NOT_RECORDED
    first = str(values[0] if isinstance(values, (list, tuple)) else values)
    return {"5000": "USB3 (super)", "10000": "USB3 (super+)", "480": "USB2 (high)"}.get(
        first, first)


def _release_name(paths: StatePaths, reference_sha: str) -> str:
    """active.json / releases/*/qualification-reference.json.sha256 대조 (읽기만)."""
    if not reference_sha:
        return NOT_RECORDED
    active = _read_json(paths.active_pointer) or {}
    if active.get("reference_sha256") == reference_sha and active.get("release"):
        return "%s (active)" % active["release"]
    releases = paths.qualification_releases
    if releases.is_dir():
        for release_dir in sorted(releases.iterdir(), reverse=True):
            digest = _read_text(release_dir / "qualification-reference.json.sha256",
                                max_bytes=256).strip().split()
            if digest and digest[0] == reference_sha:
                return "%s (not active)" % release_dir.name
    return "no matching release found for %s" % _short(reference_sha)


def _workload_results(summary, directory: Path) -> tuple:
    """(results 목록, source). summary.workloads 우선; 없으면 session/results/*.json."""
    if summary and isinstance(summary.get("workloads"), list):
        return list(summary["workloads"]), "summary.json"
    results_dir = directory / "session" / "results"
    results = []
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):
            result = scenario_report.redact_obj(_read_json(path))
            if isinstance(result, dict):
                result.setdefault("workload_id", path.stem)
                results.append(result)
    return results, "session/results (no summary.json)"


def show_run(paths: StatePaths, run_id: str, out=print) -> int:
    directory = paths.runs_dir / run_id
    if not directory.is_dir():
        out("No such run: %s (see `vdeter results` for the list)" % run_id)
        return EXIT_NOT_FOUND
    sink = out

    def out(line):   # noqa: F811 — 모든 줄이 강화된 secret scrubber를 거친다
        sink(scenario_report.redact_text(line))

    warnings = []
    redact = scenario_report.redact_obj
    summary = redact(_read_json(directory / "summary.json"))
    run_info = redact(_read_json(directory / "run.json") or {})
    state = redact(_read_json(directory / "resolved-configs" / "installation-state.json") or {})
    gain = redact(_read_json(directory / "resolved-configs" / "gain_artifact_input.json") or {})
    if summary is None:
        warnings.append("summary.json missing — run did not finish through `vdeter run` "
                        "(unfinished, interrupted before close, or evidence incomplete); "
                        "showing what is recorded")
    summary = summary or {}
    results, results_source = _workload_results(summary, directory)
    if run_id.startswith("qual-") and not summary and not results:
        out("Run %s is qualification ladder phase evidence (`vdeter qualify`), "
            "not a scenario run — inspect %s directly." % (run_id, directory))
        return EXIT_OK

    # ---- RUN HEADER
    out(MAJOR)
    out("RUN %s — %s%s" % (run_id, summary.get("status") or NOT_RECORDED,
                          " (interrupted%s)" % (
                              " during " + summary["interrupted_during"]
                              if summary.get("interrupted_during") else "")
                          if summary.get("interrupted") else ""))
    out(MAJOR)
    out("  evidence=%s" % directory)
    out("  requested_utc=%s" % (run_info.get("requested_utc") or NOT_RECORDED))
    out("  finished_utc=%s" % (summary.get("finished_utc") or NOT_RECORDED))
    out("  mode=%s" % ("interactive" if summary.get("interactive") else
                       ("batch" if summary else NOT_RECORDED)))
    out("  scenarios=%s passed / %s run / %s planned" % (
        _b(summary.get("scenarios_passed")), _b(summary.get("scenarios_run")),
        _b(summary.get("scenarios_planned"))))
    attempts = summary.get("attempts") or []
    if attempts:
        out("  bring-up attempts=%d (%s)" % (len(attempts), ", ".join(
            "%s:%s" % (a.get("attempt", "?"), a.get("bringup", "?")) for a in attempts)))
    if summary.get("interrupted"):
        out("  interrupted=true")

    # ---- BINDING
    out("")
    out("[BINDING]")
    reference_sha = run_info.get("reference_sha256") or summary.get("reference_sha256")
    if (run_info.get("reference_sha256") and summary.get("reference_sha256")
            and run_info["reference_sha256"] != summary["reference_sha256"]):
        warnings.append("run.json reference %s differs from summary.json reference %s"
                        % (_short(run_info["reference_sha256"]),
                           _short(summary["reference_sha256"])))
    out("  reference_sha256=%s" % (reference_sha or NOT_RECORDED))
    out("  qualification_release=%s" % _release_name(paths, reference_sha))
    domains = state.get("domains") or {}
    for label, key, dom in (("installation", "installation_sha256", None),
                            ("rf", "rf_manifest_sha256", "rf"),
                            ("operation", "operation_manifest_sha256", "operation"),
                            ("tooling", "tooling_manifest_sha256", "tooling")):
        value = run_info.get(key)
        other = state.get("installation_sha256") if dom is None else domains.get(dom)
        if value and other and value != other:
            warnings.append("%s digest differs between run.json (%s) and "
                            "installation-state.json (%s)" % (label, _short(value), _short(other)))
        out("  %s_sha256=%s" % (label, value or other or NOT_RECORDED))
    out("  installation_verified=%s" % _b(state.get("installation_verified")))
    out("  identity_fingerprint=%s" % (run_info.get("identity_fingerprint") or NOT_RECORDED))
    speeds = _usb_speed_map(summary, results)
    bindings = _endpoint_bindings(directory)
    common = gain.get("endpoint_common_rx_gain_db") or {}
    if bindings:
        for endpoint, role, serial in bindings:
            out("  endpoint %-6s role=%-8s serial=%-8s usb=%-14s rx_gain_db=%s" % (
                endpoint, role, serial, _speed_class(speeds.get(serial)),
                _b(common.get(endpoint))))
    else:
        out("  endpoints=%s (resolved-configs/endpoints missing)" % NOT_RECORDED)
        for endpoint, value in sorted(common.items()):
            out("  endpoint %-6s rx_gain_db=%s" % (endpoint, value))
    out("  gain_artifact_sha256=%s" % (gain.get("source_gain_artifact_sha256") or NOT_RECORDED))
    hashes = run_info.get("config_hashes") or {}
    for name in sorted(hashes):
        out("  config %-6s sha256=%s" % (name, hashes[name]))

    # ---- BRING-UP
    out("")
    out("[BRING-UP]")
    stages, source = _bringup_stages(summary, directory)
    if stages:
        out("  source=%s" % source)
        for label, cumulative, delta in stages:
            if cumulative is None:
                out("  %-44s %s" % (label, NOT_RECORDED))
            elif isinstance(cumulative, str):
                out("  %-44s elapsed=%s" % (label, cumulative))
            else:
                out("  %-44s done at %s (stage %s)" % (label, _mmss(cumulative), _mmss(delta)))
    else:
        out("  %s" % NOT_RECORDED)
    for attempt in attempts:
        if attempt.get("bringup") != "READY":
            out("  attempt %s: %s — %s" % (attempt.get("attempt"), attempt.get("bringup"),
                                          str(attempt.get("error") or "")[:160]))
            classification = attempt.get("classification") or {}
            if classification:
                out("    transient_host_time=%s" % _b(classification.get("transient_host_time")))
                for reason in classification.get("reasons") or []:
                    out("    reason=%s" % str(reason)[:160])

    # ---- WORKLOADS
    out("")
    out("[WORKLOADS] %d recorded (source: %s)" % (len(results), results_source))
    service_events = {}
    for index, result in enumerate(results, 1):
        workload_id = result.get("workload_id")
        if not workload_id:
            out("  workload %d: workload_id %s — skipped" % (index, NOT_RECORDED))
            continue
        out("")
        try:
            loaded = scenario_report.load_workload_evidence(
                directory, workload_id, result=result, service_events=service_events)
            service_events = loaded["service_events"]   # endpoint 파일은 run당 한 번만 읽는다
            model = scenario_report.build_workload_model(
                loaded["trace_rows"], loaded["result"], final_result=loaded["final_result"],
                service_events=service_events, index=index, total=len(results),
                evidence_path=str(directory / "workloads" / workload_id),
                warnings=loaded["warnings"])
            lines = scenario_report.render_workload(model)
        except Exception as exc:  # noqa: BLE001 — 표시 실패는 나머지 보고를 막지 않는다
            lines = ["  [WARNING] scenario report unavailable: %s" % exc.__class__.__name__]
        for line in lines:
            out(line)
        out(scenario_report.result_line(result))

    # ---- CLOSE / CLEANUP
    out("")
    out("[CLOSE-CLEANUP]")
    close = summary.get("close") or {}
    if close:
        out("  close=%s" % ("PASS" if close.get("pass") else "FAIL"))
        for problem in close.get("problems") or []:
            out("  problem=%s" % problem)
        out("  coordinator_rc=%s" % _b(close.get("coordinator_rc")))
        out("  escalation=%s" % _b(close.get("escalation")))
        residual = close.get("residual_processes")
        out("  residual_processes=%s" % (len(residual) if isinstance(residual, list)
                                         else _b(residual)))
        closed = close.get("closed") or {}
        cleanup = closed.get("cleanup") or {}
        if cleanup:
            out("  cleanup=%s" % ("PASS" if cleanup.get("pass") else "FAIL"))
            out("  launcher_clean_exit=%s" % _b(cleanup.get("launcher_clean_exit")))
            out("  workers_after_stop=%s" % _b(cleanup.get("workers_after_stop")))
            rediscovery = cleanup.get("rediscovery") or {}
            if rediscovery:
                out("  b210_rediscovery=%s" % ", ".join(
                    "%s:%s" % (serial, "ok" if ok else "MISSING")
                    for serial, ok in sorted(rediscovery.items())))
            if cleanup.get("throttled") is not None:
                out("  throttled=%s" % cleanup["throttled"])
        if closed:
            out("  service_stop_rc=%s" % _b(closed.get("service_stop_rc")))
            out("  session_duration=%s" % (
                "%.1fs" % closed["duration_s"] if _f(closed.get("duration_s")) is not None
                else NOT_RECORDED))
            if closed.get("error"):
                out("  coordinator_error=%s" % str(closed["error"])[:200])
        from apps.standalone.run_cmd import close_notes   # 순수 함수 (RF·세션 없음)
        for note in close_notes(close):
            out("  note: " + note)
    else:
        out("  %s" % NOT_RECORDED)

    # ---- FINAL SUMMARY
    out("")
    out("[FINAL SUMMARY]")
    passed = sum(1 for r in results if (r or {}).get("status") == "PASS")
    out("  status=%s" % (summary.get("status") or NOT_RECORDED))
    out("  workloads_recorded=%d  passed=%d" % (len(results), passed))
    if summary and summary.get("scenarios_passed") is not None \
            and summary.get("scenarios_passed") != passed:
        warnings.append("summary scenarios_passed=%s but %d recorded workload result(s) are PASS"
                        % (summary.get("scenarios_passed"), passed))
    for result in results:
        q = (result or {}).get("quiescence") or {}
        out("  %-12s %-7s %-5s %8s s  quiescence %s" % (
            result.get("scenario_id", "?"), result.get("certificate_case") or "",
            result.get("status", "?"), result.get("wall_clock_s", result.get("duration_s", "?")),
            "OK" if q.get("pass") else "FAIL"))
    out("  close=%s" % ("PASS" if close.get("pass") else ("FAIL" if close else NOT_RECORDED)))
    for warning in warnings:
        out("  [WARNING] %s" % warning)
    out(MAJOR)
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
    out("(qualify = qualification ladder phase evidence, unfinished = no summary.json yet, "
        "* = interrupted)")
    out(LIST_HINT)
    return EXIT_OK
