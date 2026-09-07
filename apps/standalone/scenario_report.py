"""시나리오 완료 후 전송 단위 상세 보고 — 순수 parser/renderer (presentation, read-only).

`vdeter run`(workload 완료 직후)과 `vdeter results <run-id>`(저장 evidence 재열람)가
**같은** 모델·문자열 렌더러를 쓴다. 판정을 다시 계산하지 않고 기록된 값만 보여 주며,
기록에 없는 값은 `not recorded`로 표시한다.

입력(모두 이미 완성된 evidence):
    - workload trace 행 목록(`workloads/<wid>/workload-trace.jsonl`; run은 on_trace로
      메모리에 수집한 행, results는 파일을 한 번 읽은 행)
    - result dict(`session/results/<wid>.json` 또는 wait_result 반환값). 그 안의
      `scenario`(= `mass-roc-events/final-result.json`과 동일 내용)에서 step 상세·
      received_checks·transfer 검사를 가져오므로 run 경로는 final-result.json을
      따로 읽지 않는다(없을 때만 fallback).
    - 선택: endpoint service_events 행(`evidence/service_events/<EP>.jsonl`, 파일당 한 번
      bounded 읽기 → transfer_id·시간창으로 필터)

범위(scope): SC-VDE-006처럼 전제 시나리오(SC-VDE-005)가 같은 workload 안에서 실행되면
final-result의 `precondition`/`result` 항목을 **따로** 보여 준다 — [PRECONDITION] 블록
(전제 전송·전제 step·전제 counter)과 [SCENARIO] 블록(본 전송·본 step·본 counter).
trace submit 행의 scope는 final-result의 `transfers` 목록(transfer_id, source=recorded)
으로 정하고, 거기 없으면 step 접두("005-")로 정하며 그 사실을 표시한다. counter는
범위별 기록값을 그대로 쓰고 합산하지 않는다; 표시한 전송 총수는 trace-count로 따로 적는다.
`precondition_reused=true`면 그대로 표시하고 trace에 없는 전송을 만들어내지 않는다.

ACK 상관: final-result에 transfer UUID를 직접 잇는 기록이 있으면 `source=recorded`,
없으면 step 이름·역방향 path·순서로 **파생**한 후보를 `source=derived(...)`,
`matched=derived`로 표시한다(기록 사실처럼 보이지 않게). `-ack-correlated` step의
`linkage_id`·`ack_status`는 기록값으로 함께 보여 준다(step 이름으로 연결했음을 명시).
ack latency는 두 trace 수신 시각의 차이 → `ack_latency_source=derived(receive timestamps)`.

사건 시각은 evidence의 `t_monotonic_s`(workload 시작 기준)·`t_utc`·`t_mono`만 쓴다.
정확한 timeline은 workload trace(submit·data_received·transfer_terminal)만으로
만든다. service_events의 `t_mono`는 endpoint 이벤트 drain이 **기록한** 시각이라
(발생 시각보다 수 초 늦을 수 있음) 상태 순서와 `recorded` 범위로만 보여 주고,
trace의 (`t_mono`, `t_monotonic_s`) 쌍(anchor)으로 workload 상대시간에 맞춘다.
anchor가 없으면(수신 없음 등) sender terminal state 쌍으로 근사 anchor를 잡고 그
사실을 표시하며, 그것도 없으면 시각을 표시하지 않는다. 렌더링 시각·callback
시각은 절대 쓰지 않는다.

I/O 계약: 이 모듈은 파일을 쓰지 않는다. 읽기는 :func:`load_workload_evidence`·
:func:`read_service_events_once`뿐이며 호출자가 결과 확정 후 한 번만 부른다.
polling·flush·thread 없음.

비밀: :func:`redact_obj`가 입력(trace·result·final-result·service_events)을 깊이
훑어 payload 원문·DER·개인키·passphrase·password·secret·token 계열 키를(중첩·대소문자·
부분 일치 포함) 제거하고, 문자열 안의 `key=value`·`key: value`·`'key': 'value'`·
`"key": "value"` 형태도 :func:`redact_text`로 가린다. 렌더링 줄은 다시
:func:`redact_text`를 거친다. payload_format·payload_len·payload_sha256·
certificate_case·verification_reason·fingerprint·reference SHA·transfer UUID는 유지.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from apps.standalone.progress import scrub_text

MAJOR = "=" * 78
MINOR = "-" * 78
NOT_RECORDED = "not recorded"
REDACTED = "<redacted>"
DEFAULT_MAX_JSONL_BYTES = 8 * 1024 * 1024

#: 키 이름 어디에든 들어 있으면(대소문자 무시) 값을 통째로 버리는 조각.
SECRET_KEY_FRAGMENTS = ("payload_base64", "raw_payload", "certificate_der",
                        "private_key", "passphrase", "password", "secret",
                        "api_key", "access_token", "auth_token", "token")
#: 정확히 일치할 때만 버리는 키 (원문 payload/DER의 짧은 이름).
SECRET_KEY_EXACT = ("payload", "der", "pem")
#: 키 비교는 소문자·영숫자만 남긴 정규형으로 한다 (payloadBase64, x-access-token 등).
_SECRET_KEY_NORMALIZED = tuple(re.sub(r"[^a-z0-9]", "", f) for f in SECRET_KEY_FRAGMENTS)
_SECRET_TEXT_KEY = "|".join(re.sub(r"_", "[_-]?", f) for f in SECRET_KEY_FRAGMENTS)
#: 문자열 안의 key=value / key: value / 'key': 'value' / "key": "value".
_SECRET_TEXT_RE = re.compile(
    r"""(?P<lead>(?P<kq>['"]?)(?P<key>[A-Za-z0-9_.\-]*(?:%s)[A-Za-z0-9_.\-]*)(?P=kq)\s*[=:]\s*)"""
    r"""(?:(?P<vq>['"])(?P<qval>.*?)(?P=vq)|(?P<val>[^\s,;}\]]+))"""
    % _SECRET_TEXT_KEY, re.I | re.S)

_LAYER_RE = re.compile(r"layer=<Layer\.([A-Z_]+)")
_REASON_RE = re.compile(r"'reason', '([A-Za-z_]+)'")
_TERMINAL_OK = "COMPLETED"
_SECURITY_NORMAL_STEPS = ("001-verified-stored", "001-idempotent-store", "001-resend-ok")
_TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")


# ------------------------------------------------------------ 비밀 제거

def is_secret_key(key) -> bool:
    name = str(key)
    if name.lower() in SECRET_KEY_EXACT:
        return True
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(fragment in normalized for fragment in _SECRET_KEY_NORMALIZED)


def redact_text(text) -> str:
    """문자열 안의 비밀(PEM 블록, key=value 계열)을 가린다. 문자열이 아니면 str()."""
    if text is None:
        return ""
    text = scrub_text(str(text))

    def _sub(m):
        if m.group("vq") is not None:
            return m.group("lead") + m.group("vq") + REDACTED + m.group("vq")
        return m.group("lead") + REDACTED

    return _SECRET_TEXT_RE.sub(_sub, text)


def redact_obj(obj):
    """dict/list를 깊이 훑어 비밀 키를 제거하고 문자열은 redact_text를 거친 사본."""
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items() if not is_secret_key(k)}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# ------------------------------------------------------------ 읽기 (한 번)

def read_json_once(path) -> tuple:
    """(obj|None, warning|None). 없거나 손상되면 None + 경고 문자열."""
    path = Path(path)
    try:
        return json.loads(path.read_bytes().decode("utf-8")), None
    except FileNotFoundError:
        return None, "%s: missing" % path.name
    except (OSError, ValueError) as exc:
        return None, "%s: unavailable (%s)" % (path.name, exc.__class__.__name__)


def read_jsonl_bounded(path, *, max_bytes: int = DEFAULT_MAX_JSONL_BYTES) -> tuple:
    """append-only JSONL을 **한 번** 읽는다 (마지막 max_bytes만; 손상 행은 건너뜀).
    반환 (rows, warning|None)."""
    path = Path(path)
    newline = b"\n"
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                data = handle.read()
                data = data.split(newline, 1)[1] if newline in data else b""
                warning = "%s: truncated to last %d bytes" % (path.name, max_bytes)
            else:
                data = handle.read()
                warning = None
    except FileNotFoundError:
        return [], "%s: missing" % path.name
    except OSError as exc:
        return [], "%s: unavailable (%s)" % (path.name, exc.__class__.__name__)
    rows = []
    bad = 0
    for line in data.split(newline):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
    if bad:
        warning = ((warning + "; ") if warning else "") + (
            "%s: %d corrupt line(s) skipped" % (path.name, bad))
    return rows, warning


def read_service_events_once(run_root, endpoint_id: str, *,
                             max_bytes: int = DEFAULT_MAX_JSONL_BYTES) -> tuple:
    """`evidence/service_events/<EP>.jsonl` 한 번 읽기 (비밀 키·문자열 제거)."""
    rows, warning = read_jsonl_bounded(
        Path(run_root) / "evidence" / "service_events" / (endpoint_id + ".jsonl"),
        max_bytes=max_bytes)
    return redact_obj(rows), warning


def final_result_from(result):
    """result JSON 안의 시나리오 판정 dict(`scenario`, final-result.json과 동일)."""
    scenario = result.get("scenario") if isinstance(result, dict) else None
    if isinstance(scenario, dict) and isinstance(scenario.get("results"), dict):
        return scenario
    return None


def endpoints_in_trace(rows) -> list:
    seen = []
    for row in rows:
        for key in ("sender", "receiver"):
            value = row.get(key)
            if isinstance(value, str) and value and value not in seen:
                seen.append(value)
    return seen


def load_workload_evidence(run_root, workload_id: str, *, result=None,
                           trace_rows=None, service_events=None,
                           max_bytes: int = DEFAULT_MAX_JSONL_BYTES) -> dict:
    """완료된 workload의 evidence를 파일당 정확히 한 번 읽는다.

    `result`/`trace_rows`/`service_events`를 주면 그 파일은 읽지 않는다
    (run 경로: result·trace는 이미 메모리에 있다). final-result.json은 result에
    `scenario.results`가 없을 때만 읽는다. service_events는 trace에 나타난
    endpoint 파일만, 주어지지 않은 endpoint만 읽는다.
    반환: {"trace_rows", "result", "final_result", "service_events", "warnings",
    "files_read"}."""
    run_root = Path(run_root)
    warnings = []
    files_read = []
    wdir = run_root / "workloads" / workload_id
    if trace_rows is None:
        trace_rows, warn = read_jsonl_bounded(wdir / "workload-trace.jsonl",
                                              max_bytes=max_bytes)
        files_read.append(str(wdir / "workload-trace.jsonl"))
        if warn:
            warnings.append(warn)
    trace_rows = redact_obj(list(trace_rows))
    if result is None:
        result_path = run_root / "session" / "results" / (workload_id + ".json")
        result, warn = read_json_once(result_path)
        files_read.append(str(result_path))
        if warn:
            warnings.append(warn)
    result = redact_obj(result or {})
    final_result = final_result_from(result)
    if final_result is None:
        final_result, warn = read_json_once(wdir / "mass-roc-events" / "final-result.json")
        files_read.append(str(wdir / "mass-roc-events" / "final-result.json"))
        final_result = redact_obj(final_result)
        if warn:
            warnings.append(warn)
    events = dict(service_events or {})
    for endpoint in endpoints_in_trace(trace_rows):
        if endpoint in events:
            continue
        rows, warn = read_service_events_once(run_root, endpoint, max_bytes=max_bytes)
        files_read.append(str(run_root / "evidence" / "service_events" / (endpoint + ".jsonl")))
        events[endpoint] = rows
        if warn:
            warnings.append(warn)
    return {"trace_rows": trace_rows, "result": result,
            "final_result": final_result, "service_events": events,
            "warnings": warnings, "files_read": files_read}


# ------------------------------------------------------------ 공용 보조

def as_float(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return NOT_RECORDED if value is None else str(value)


def fmt_rel(seconds) -> str:
    """workload 시작 기준 상대시간 `+SS.mmms`."""
    if seconds is None:
        return NOT_RECORDED
    return "%s%06.3fs" % ("+" if seconds >= 0 else "-", abs(seconds))


def _first(rows, **match):
    for row in rows:
        if all(row.get(k) == v for k, v in match.items()):
            return row
    return None


def _step_prefix(step: str) -> str:
    for suffix in ("-ack", "-command", "-resend"):
        if step.endswith(suffix):
            return step[: -len(suffix)]
    return step


def _scenario_number(scenario_id) -> str:
    """'SC-VDE-005' → '005' (step 접두 매칭용)."""
    m = re.search(r"(\d{3})", str(scenario_id or ""))
    return m.group(1) if m else ""


def _scopes_from(final_result) -> list:
    """final-result `results` → scope 목록 [{kind, scenario_id, entry, reused}].
    006처럼 precondition/result가 중첩되면 precondition → result 순."""
    scopes = []
    if not isinstance(final_result, dict):
        return scopes
    for sid, entry in (final_result.get("results") or {}).items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("result"), dict) and "steps" in entry["result"]:
            pre = entry.get("precondition")
            if isinstance(pre, dict):
                scopes.append({"kind": "precondition",
                               "scenario_id": pre.get("scenario_id") or "precondition",
                               "entry": pre, "reused": entry.get("precondition_reused")})
            scopes.append({"kind": "scenario",
                           "scenario_id": entry["result"].get("scenario_id") or sid,
                           "entry": entry["result"], "reused": None})
        else:
            scopes.append({"kind": "scenario", "scenario_id": sid, "entry": entry,
                           "reused": None})
    return scopes


def _anchor(rows, transfers):
    """service_events `t_mono` → workload 상대시간 변환 상수와 종류.

    exact: trace data_received 행의 (t_mono, t_monotonic_s) 쌍.
    approx: sender terminal state 기록 t_mono ↔ trace transfer_terminal t_monotonic_s
    (기록 지연만큼 늦을 수 있음). 없으면 (None, None)."""
    for row in rows:
        if row.get("event") != "data_received":
            continue
        t_mono, t_rel = as_float(row.get("t_mono")), as_float(row.get("t_monotonic_s"))
        if t_mono is not None and t_rel is not None:
            return t_mono - t_rel, "exact"
    for tr in transfers:
        if tr["t_terminal"] is None:
            continue
        for t_mono, state in tr["_states"]:
            if state in _TERMINAL_STATES and t_mono is not None:
                return t_mono - tr["t_terminal"], "approx"
    return None, None


# ------------------------------------------------------------ 모델

def build_workload_model(trace_rows, result, *, final_result=None,
                         service_events=None, index=None, total=None,
                         evidence_path=None, warnings=()) -> dict:
    """완료된 workload evidence → 표시 모델 (순수 함수, 판정 재계산 없음)."""
    rows = redact_obj(list(trace_rows or []))
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: as_float(r.get("seq")) or 0)
    result = redact_obj(dict(result or {}))
    service_events = {ep: [r for r in redact_obj(list(ev or [])) if isinstance(r, dict)]
                      for ep, ev in (service_events or {}).items()}
    warnings = [redact_text(w) for w in warnings]
    final_result = redact_obj(final_result) if final_result is not None else final_result_from(result)
    begin = _first(rows, event="workload_begin") or {}
    end = _first(rows, event="workload_end") or {}
    verdict = _first(rows, event="scenario_result") or {}
    scenario_id = (result.get("scenario_id") or begin.get("scenario_id")
                   or (verdict.get("scenario") or "").split("[")[0] or "?")
    case = result.get("certificate_case") or begin.get("certificate_case")
    status = result.get("status") or verdict.get("status") or NOT_RECORDED
    if verdict.get("status") and result.get("status") and verdict["status"] != result["status"]:
        warnings.append("scenario_result status %s differs from result status %s"
                        % (verdict["status"], result["status"]))
    elapsed = as_float(end.get("t_monotonic_s"))
    if elapsed is None:
        elapsed = as_float(result.get("duration_s"))
    if elapsed is None:
        elapsed = as_float(result.get("wall_clock_s"))
    quiescence = result.get("quiescence") or {}
    health = result.get("session_health_before") or {}

    # --- scope (precondition / scenario) — final-result 기록 그대로
    scopes = _scopes_from(final_result)
    if not scopes:
        steps = []
        for item in verdict.get("steps") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                steps.append({"name": item[0], "status": item[1]})
            elif isinstance(item, dict):
                steps.append(dict(item))
        scopes = [{"kind": "scenario", "scenario_id": scenario_id, "reused": None,
                   "entry": {"status": verdict.get("status"), "steps": steps,
                             "counters": verdict.get("counters") or {},
                             "_source": "trace scenario_result"}}]
    for scope in scopes:
        entry = scope["entry"]
        scope["status"] = entry.get("status") or NOT_RECORDED
        scope["steps"] = [dict(st) for st in entry.get("steps") or [] if isinstance(st, dict)]
        scope["counters"] = dict(entry.get("counters") or {}) if isinstance(entry.get("counters"), dict) else {}
        scope["counters_source"] = entry.get("_source", "recorded") if scope["counters"] else None
        scope["recorded_transfers"] = {tr["transfer_id"]: tr for tr in entry.get("transfers") or []
                                       if isinstance(tr, dict) and tr.get("transfer_id")}
        scope["transfers"] = []
    main_scope = scopes[-1]
    all_steps = [st for scope in scopes for st in scope["steps"]]
    step_by_transfer = {st.get("transfer_id"): st for st in all_steps if st.get("transfer_id")}

    # --- 전송 (trace submit 순) + scope 배정
    transfers = []
    for row in rows:
        if row.get("event") != "submit":
            continue
        tid = row.get("transfer_id")
        step = row.get("step") or ""
        terminal = ((_first(rows, event="transfer_terminal", transfer_id=tid) if tid else None)
                    or _first(rows, event="transfer_terminal", step=step) or {})
        received = _first(rows, event="data_received", step=step) or {}
        sender_rows = service_events.get(row.get("sender")) or []
        states = [(as_float(r.get("t_mono")), str(r.get("transfer_state")))
                  for r in sender_rows
                  if r.get("event_type") == "TRANSFER_STATE_CHANGED"
                  and tid and r.get("transfer_id") == tid]
        scope = None
        scope_source = None
        for candidate in scopes:
            if tid in candidate["recorded_transfers"]:
                scope, scope_source = candidate, "recorded"
                break
        if scope is None:
            for candidate in scopes:
                number = _scenario_number(candidate["scenario_id"])
                if number and step.startswith(number + "-"):
                    scope, scope_source = candidate, "derived(step prefix)"
                    break
        if scope is None:
            scope, scope_source = main_scope, "not recorded"
            warnings.append("%s: scope not recorded in final-result; shown under %s"
                            % (step or tid, main_scope["scenario_id"]))
        transfer = {
            "step": step, "transfer_id": tid or NOT_RECORDED,
            "sender": row.get("sender"), "receiver": row.get("receiver"),
            "source_station": row.get("source_station"),
            "destination_station": row.get("destination_station"),
            "payload_format": row.get("payload_format"),
            "application_bytes": row.get("payload_len"),
            "payload_sha256": row.get("payload_sha256"),
            "ack_required": row.get("ack_required"),
            "t_submit": as_float(row.get("t_monotonic_s")),
            "t_received": (as_float(received.get("t_monotonic_s"))
                           if received.get("present") else None),
            "t_terminal": as_float(terminal.get("t_monotonic_s")),
            "utc_submit": row.get("t_utc"), "utc_terminal": terminal.get("t_utc"),
            "terminal_state": terminal.get("terminal_state"),
            "scope_kind": scope["kind"], "scope_scenario": scope["scenario_id"],
            "scope_source": scope_source,
            "step_status": (step_by_transfer.get(tid) or {}).get("status"),
            "is_ack": bool((row.get("payload_format") or "").endswith(".ack")
                           or step.endswith("-ack")),
            "correlation": None,
            "_received": received, "_states": states,
            "_check": scope["recorded_transfers"].get(tid) or {},
            "_scope": scope,
        }
        transfers.append(transfer)
        scope["transfers"].append(transfer)
    anchor, anchor_kind = _anchor(rows, transfers)

    for tr in transfers:
        counters = tr["_scope"]["counters"]
        events = []
        if tr["t_submit"] is not None:
            events.append((tr["t_submit"], "SUBMITTED"))
        received = tr["_received"]
        if tr["t_received"] is not None:
            events.append((tr["t_received"], "DATA_RECEIVED by %s"
                           % (received.get("receiver") or tr["receiver"] or "?")))
        elif received and not received.get("present", True):
            t_none = as_float(received.get("t_monotonic_s"))
            if t_none is not None:
                events.append((t_none, "no DATA_RECEIVED observed at %s"
                               % (received.get("receiver") or tr["receiver"] or "?")))
        if tr["t_terminal"] is not None:
            events.append((tr["t_terminal"], str(tr["terminal_state"] or NOT_RECORDED)))
        events.sort(key=lambda e: e[0])
        tr["events"] = events
        seq = []
        for _t, state in tr["_states"]:
            if not seq or seq[-1] != state:
                seq.append(state)
        recorded = ([t - anchor for t, _s in tr["_states"] if t is not None]
                    if anchor is not None else [])
        tr["endpoint_states"] = {
            "sequence": seq,
            "recorded_from": min(recorded) if recorded else None,
            "recorded_to": max(recorded) if recorded else None,
        }
        # 수신 검사: final-result transfers.received.checks → step received_checks →
        # (없으면) 수신단 service_events DATA_RECEIVED sha 대조 (원문은 모델에 없음).
        check = tr["_check"]
        rchk = check.get("received") if isinstance(check.get("received"), dict) else {}
        checks = rchk.get("checks") if isinstance(rchk.get("checks"), dict) else None
        if checks is None:
            st = step_by_transfer.get(tr["transfer_id"]) or {}
            checks = (st.get("received_checks")
                      if isinstance(st.get("received_checks"), dict) else None)
        sha_match = NOT_RECORDED
        if checks is not None and "payload_sha256" in checks:
            sha_match = bool(checks["payload_sha256"])
        elif tr["payload_sha256"] and anchor is not None and tr["t_submit"] is not None:
            limit = (tr["t_terminal"] if tr["t_terminal"] is not None
                     else (tr["t_received"] or tr["t_submit"])) + 10.0
            for rrow in service_events.get(tr["receiver"]) or []:
                if rrow.get("event_type") != "DATA_RECEIVED":
                    continue
                t_mono = as_float(rrow.get("t_mono"))
                if t_mono is None or not (tr["t_submit"] - 1.0 <= t_mono - anchor <= limit):
                    continue
                sha_match = rrow.get("payload_sha256") == tr["payload_sha256"]
                if sha_match:
                    break
        if received:
            present = received.get("present")
        elif rchk:
            present = True
        else:
            present = NOT_RECORDED
        if "valid" in rchk:
            valid = rchk["valid"]
        elif checks:
            valid = all(bool(v) for v in checks.values())
        else:
            valid = NOT_RECORDED
        if "wrong_destination" in check:
            wrong = bool(check["wrong_destination"])
        elif (received.get("original_destination") is not None
              and tr["destination_station"] is not None):
            wrong = received.get("original_destination") != tr["destination_station"]
        else:
            wrong = NOT_RECORDED
        tr["integrity"] = {
            "payload_present": present,
            "payload_valid": valid,
            "payload_sha_match": sha_match,
            "wrong_destination": wrong,
            "duplicate": ("false (%s counter duplicates=0)" % tr["scope_kind"]
                          if counters.get("duplicates") == 0
                          else "not recorded per transfer (%s counter duplicates=%s)"
                          % (tr["scope_kind"], counters.get("duplicates", "?"))),
        }
        tr["final_transfer_state"] = check.get("final_transfer_state")
        if (tr["final_transfer_state"] and tr["terminal_state"]
                and tr["final_transfer_state"] != tr["terminal_state"]):
            warnings.append("%s: trace terminal %s differs from recorded final state %s"
                            % (tr["step"], tr["terminal_state"], tr["final_transfer_state"]))

    # --- ACK 상관 (기록 vs 파생)
    for i, tr in enumerate(transfers):
        if not tr["is_ack"]:
            continue
        tr["correlation"] = _correlate_ack(tr, transfers[:i], all_steps)
    for tr in transfers:
        for key in ("_received", "_states", "_check", "_scope", "_acked"):
            tr.pop(key, None)

    errors = _collect_errors(rows, result, final_result, all_steps, transfers,
                             service_events, anchor, anchor_kind, elapsed)
    security = _security(case, all_steps, transfers) if scenario_id == "SC-VDE-001" else None
    if anchor is None and any(service_events.get(ep) for ep in endpoints_in_trace(rows)):
        warnings.append("endpoint service_events could not be time-aligned to this workload "
                        "(no t_mono anchor); endpoint ERROR events shown only when tagged "
                        "with a transfer_id of this workload")
    scope_models = []
    for scope in scopes:
        scope_models.append({
            "kind": scope["kind"], "scenario_id": scope["scenario_id"],
            "status": scope["status"], "reused": scope["reused"],
            "transfers": scope["transfers"], "steps": scope["steps"],
            "counters": scope["counters"], "counters_source": scope["counters_source"],
            "recorded_transfers_count": len(scope["recorded_transfers"]),
        })
    precondition = next((s for s in scope_models if s["kind"] == "precondition"), None)
    main = scope_models[-1]
    summary = {
        "precondition": precondition["scenario_id"] if precondition else None,
        "precondition_reused": precondition["reused"] if precondition else None,
        "precondition_transfers_completed": (
            precondition["counters"].get("transfers_completed", NOT_RECORDED)
            if precondition else None),
        "scenario_transfers_completed": main["counters"].get("transfers_completed", NOT_RECORDED),
        "rendered_transfers_total": len(transfers),
        "transfers_failed": main["counters"].get("transfers_failed", NOT_RECORDED),
        "wrong_destination": main["counters"].get("wrong_destination", NOT_RECORDED),
        "mismatched_ack": main["counters"].get("mismatched_ack", NOT_RECORDED),
        "duplicates": main["counters"].get("duplicates", NOT_RECORDED),
        "counters_source": main["counters_source"] or NOT_RECORDED,
        "non_terminal_transfers": quiescence.get("non_terminal_transfers", NOT_RECORDED),
        "quiescence": ("PASS" if quiescence.get("pass") else
                       ("FAIL" if quiescence else NOT_RECORDED)),
        "workers_running": _ratio(quiescence.get("workers_running"),
                                  health.get("expected_launchers")),
        "launchers_alive": _ratio(quiescence.get("launchers_alive"),
                                  health.get("expected_launchers")),
        "coordinator_faults": quiescence.get("faults", NOT_RECORDED),
        "inhibited": quiescence.get("inhibited", NOT_RECORDED),
    }
    return {
        "index": index, "total": total, "scenario_id": scenario_id,
        "certificate_case": case, "status": status,
        "workload_id": result.get("workload_id") or begin.get("workload_id") or NOT_RECORDED,
        "started_utc": begin.get("t_utc") or NOT_RECORDED,
        "elapsed_s": elapsed, "anchor_kind": anchor_kind,
        "scopes": scope_models, "transfers": transfers,
        "security": security, "errors": errors, "summary": summary,
        "evidence_path": evidence_path,
        "warnings": [redact_text(w) for w in warnings],
    }


def _correlate_ack(ack, earlier, all_steps) -> dict:
    """ACK 전송 ↔ 명령 전송 연결. 기록(transfer UUID 직접 기록)이 있으면 recorded,
    없으면 step 접두·역방향 path·순서로 파생(derived), 후보가 없으면 not recorded."""
    counters = ack["_scope"]["counters"]
    corr = {
        "source": NOT_RECORDED, "matched": NOT_RECORDED,
        "command_transfer_id": NOT_RECORDED, "ack_for": NOT_RECORDED,
        "ack_latency_s": None, "ack_latency_source": NOT_RECORDED,
        "recorded_step": None, "recorded_ack_status": None, "recorded_linkage_id": None,
        "mismatched_ack": ("false (%s counter mismatched_ack=0)" % ack["scope_kind"]
                           if counters.get("mismatched_ack") == 0
                           else "%s counter mismatched_ack=%s"
                           % (ack["scope_kind"], counters.get("mismatched_ack", "?"))),
    }
    # 1) 기록: step이 이 ack의 transfer UUID를 직접 잇는 경우
    for st in all_steps:
        ids = {st.get("ack_transfer_id"), st.get("transfer_id")}
        if ack["transfer_id"] in ids and ("linkage_id" in st or "ack_status" in st
                                           or st.get("command_transfer_id")):
            corr.update({"source": "recorded", "matched": "recorded",
                         "recorded_step": st.get("name"),
                         "recorded_ack_status": st.get("ack_status"),
                         "recorded_linkage_id": st.get("linkage_id")})
            if st.get("command_transfer_id"):
                corr["command_transfer_id"] = st["command_transfer_id"]
                cmd = next((c for c in earlier if c["transfer_id"] == st["command_transfer_id"]), None)
                if cmd is not None:
                    corr["ack_for"] = cmd["step"]
                    cmd["_acked"] = True
                    _derived_latency(corr, ack, cmd)
            if as_float(st.get("ack_latency_s")) is not None:
                corr["ack_latency_s"] = as_float(st["ack_latency_s"])
                corr["ack_latency_source"] = "recorded"
            return corr
    # 2) 파생: 같은 step 접두의 역방향 앞선 명령, 없으면 역방향 ack_required 명령
    prefix = _step_prefix(ack["step"])
    cmd = None
    for cand in reversed(earlier):
        if cand["is_ack"] or cand.get("_acked"):
            continue
        if (cand["sender"] == ack["receiver"] and cand["receiver"] == ack["sender"]
                and _step_prefix(cand["step"]) == prefix):
            cmd = cand
            break
    if cmd is None:
        for cand in reversed(earlier):
            if (not cand["is_ack"] and not cand.get("_acked") and cand["ack_required"]
                    and cand["sender"] == ack["receiver"] and cand["receiver"] == ack["sender"]):
                cmd = cand
                break
    if cmd is not None:
        cmd["_acked"] = True
        corr.update({"source": "derived(step+reverse-path+order)", "matched": "derived",
                     "command_transfer_id": cmd["transfer_id"], "ack_for": cmd["step"]})
        _derived_latency(corr, ack, cmd)
    # 기록된 `<prefix>-ack-correlated` step의 linkage_id/ack_status (step 이름으로 연결)
    name = prefix + "-ack-correlated"
    st = next((s for s in ack["_scope"]["steps"] if s.get("name") == name), None) or \
        next((s for s in all_steps if s.get("name") == name), None)
    if st is not None:
        corr["recorded_step"] = st.get("name")
        corr["recorded_ack_status"] = st.get("ack_status")
        corr["recorded_linkage_id"] = st.get("linkage_id")
    return corr


def _derived_latency(corr, ack, cmd):
    if ack["t_received"] is not None and cmd["t_received"] is not None:
        corr["ack_latency_s"] = ack["t_received"] - cmd["t_received"]
        corr["ack_latency_source"] = "derived(receive timestamps)"


def _ratio(value, expected):
    if value is None:
        return NOT_RECORDED
    return "%s/%s" % (value, expected) if expected is not None else str(value)


def _reason_fields(text) -> dict:
    entry = {}
    text = str(text)
    m = _LAYER_RE.search(text)
    if m:
        entry["layer"] = m.group(1)
    m = _REASON_RE.search(text)
    if m:
        entry["reason"] = m.group(1)
    return entry


def _collect_errors(rows, result, final_result, steps, transfers,
                    service_events, anchor, anchor_kind, elapsed):
    errors = []
    scen = result.get("scenario") if isinstance(result.get("scenario"), dict) else {}
    seen_text = set()
    for text in (scen.get("error"), result.get("error"),
                 (_first(rows, event="scenario_result") or {}).get("reason"),
                 (_first(rows, event="workload_end") or {}).get("error"),
                 final_result.get("error") if isinstance(final_result, dict) else None):
        if text and str(text) not in seen_text:
            seen_text.add(str(text))
            entry = {"recorded_reason": redact_text(text)[:200]}
            entry.update(_reason_fields(text))
            errors.append(entry)
    cleanup = (final_result.get("cleanup_errors")
               if isinstance(final_result, dict) else None)
    for item in cleanup or []:
        errors.append({"cleanup_error": redact_text(item)[:200]})
    for st in steps:
        if st.get("status") not in (None, "PASS"):
            entry = {"step": st.get("name"), "status": st.get("status")}
            if st.get("reason"):
                entry["recorded_reason"] = redact_text(st["reason"])[:200]
            errors.append(entry)
    tids = set()
    for tr in transfers:
        tids.add(tr["transfer_id"])
        if tr["terminal_state"] and tr["terminal_state"] != _TERMINAL_OK:
            errors.append({"transfer_id": tr["transfer_id"], "step": tr["step"],
                           "terminal_state": tr["terminal_state"]})
    # endpoint ERROR 이벤트 — anchor가 있으면 workload 시간창, 없으면 transfer_id 태그만.
    hi = (elapsed if elapsed is not None else float("inf")) + 1.0
    for endpoint, ev_rows in service_events.items():
        found = {}
        for r in ev_rows:
            if r.get("event_type") != "ERROR":
                continue
            t_rel = None
            if anchor is not None:
                t_mono = as_float(r.get("t_mono"))
                if t_mono is None:
                    continue
                t_rel = t_mono - anchor
                if not (-1.0 <= t_rel <= hi):
                    continue
            elif r.get("transfer_id") not in tids:
                continue
            code = str(r.get("error_code") or "ERROR")
            detail = _reason_fields(r.get("error_details") or "")
            key = (code, detail.get("reason"), detail.get("layer"))
            item = found.setdefault(key, {"endpoint": endpoint, "error_code": code,
                                          "occurrences": 0, "first_seen_s": t_rel,
                                          "transfer_id": r.get("transfer_id")})
            for k, v in detail.items():
                item.setdefault(k, v)
            item["occurrences"] += 1
            if t_rel is not None and (item["first_seen_s"] is None
                                      or t_rel < item["first_seen_s"]):
                item["first_seen_s"] = t_rel
        for item in found.values():
            if anchor_kind == "approx":
                item["window"] = "approx"
            errors.append(item)
    return errors


def _security(case, steps, transfers):
    by_name = {st.get("name"): st for st in steps}
    first = transfers[0] if transfers else {}
    model = {"certificate_case": case or NOT_RECORDED,
             "transport_result": first.get("terminal_state") or NOT_RECORDED,
             "certificate_received": (first.get("integrity") or {}).get(
                 "payload_present", NOT_RECORDED)}
    if case == "forged":
        rejected = by_name.get("001-forged-rejected") or {}
        stored = by_name.get("001-not-stored") or {}
        reason = rejected.get("reason")
        model["signature_valid"] = (False if reason == "bad-signature"
                                    else (NOT_RECORDED if reason is None
                                          else "reason=%s" % reason))
        model["verification_reason"] = reason if reason is not None else NOT_RECORDED
        size = rejected.get("trust_store_size", stored.get("trust_store_size"))
        model["stored_in_trust_store"] = (False if stored.get("status") == "PASS"
                                          else (NOT_RECORDED if not stored
                                                else "step %s" % stored.get("status")))
        model["trust_store_size"] = size if size is not None else NOT_RECORDED
        if rejected.get("status") == "PASS" and stored.get("status") == "PASS":
            model["security_result"] = "PASS (forged certificate correctly rejected)"
        elif rejected or stored:
            model["security_result"] = "FAIL (001-forged-rejected=%s, 001-not-stored=%s)" % (
                rejected.get("status", NOT_RECORDED), stored.get("status", NOT_RECORDED))
        else:
            model["security_result"] = _not_reached(model["transport_result"])
    else:
        verified = by_name.get("001-verified-stored") or {}
        reason = verified.get("reason")
        model["signature_valid"] = (True if reason == "ok"
                                    else (NOT_RECORDED if reason is None
                                          else "reason=%s" % reason))
        model["verification_reason"] = reason if reason is not None else NOT_RECORDED
        model["stored_in_trust_store"] = (True if verified.get("status") == "PASS"
                                          else (NOT_RECORDED if not verified
                                                else "step %s" % verified.get("status")))
        size = verified.get("trust_store_size")
        model["trust_store_size"] = size if size is not None else NOT_RECORDED
        related = [by_name[n] for n in _SECURITY_NORMAL_STEPS if by_name.get(n)]
        if related and all(st.get("status") == "PASS" for st in related):
            model["security_result"] = "PASS"
        elif related:
            model["security_result"] = "FAIL (%s)" % ", ".join(
                "%s=%s" % (st.get("name"), st.get("status"))
                for st in related if st.get("status") != "PASS")
        else:
            model["security_result"] = _not_reached(model["transport_result"])
    return model


def _not_reached(transport_result) -> str:
    """검증 step 기록이 없을 때: 전송이 끝나지 않았으면 그 사실을, 아니면 미기록."""
    if transport_result not in (NOT_RECORDED, _TERMINAL_OK):
        return "not reached (transport %s)" % transport_result
    return NOT_RECORDED


# ------------------------------------------------------------ 렌더링

def result_line(result: dict) -> str:
    """`result: PASS · 19.2 s · quiescence OK` 요약 (run·results 공용)."""
    result = redact_obj(result or {})
    quiescence = result.get("quiescence") or {}
    line = "  result: %s · %s s · quiescence %s" % (
        result.get("status"), result.get("wall_clock_s", result.get("duration_s", "?")),
        "OK" if quiescence.get("pass") else "FAIL")
    if result.get("status") != "PASS":
        scenario = result.get("scenario") or {}
        reason = scenario.get("error") if isinstance(scenario, dict) else None
        reason = reason or result.get("error")
        if reason:
            line += "\n  reason: " + str(reason)[:200]
    return redact_text(line)


_ERROR_KEYS = ("layer", "reason", "recorded_reason", "endpoint", "error_code",
               "occurrences", "first_seen_s", "window", "transfer_id", "step",
               "status", "terminal_state", "cleanup_error")


def _render_transfer(out, tr, n, count, anchor_kind):
    out.append("")
    out.append(MINOR)
    out.append("[TRANSFER %d/%d] %s" % (n, count, tr["step"] or "(no step)"))
    out.append(MINOR)
    out.append("  transfer_id=%s" % tr["transfer_id"])
    if tr["correlation"]:
        out.append("  command_transfer_id=%s" % tr["correlation"]["command_transfer_id"])
    out.append("  path=%s(%s) -> %s(%s)" % (tr["sender"] or "?", fmt_value(tr["source_station"]),
                                           tr["receiver"] or "?",
                                           fmt_value(tr["destination_station"])))
    out.append("  payload_format=%s" % fmt_value(tr["payload_format"]))
    out.append("  application_bytes=%s" % fmt_value(tr["application_bytes"]))
    out.append("  payload_sha256=%s" % fmt_value(tr["payload_sha256"]))
    if tr["correlation"]:
        out.append("  ack_for=%s" % tr["correlation"]["ack_for"])
    else:
        out.append("  ack_required=%s" % fmt_value(tr["ack_required"]))
    if tr["scope_source"] != "recorded":
        out.append("  scope=%s (source=%s)" % (tr["scope_scenario"], tr["scope_source"]))
    out.append("")
    out.append("  events (workload trace, t_monotonic_s):")
    if tr["events"]:
        for t_rel, name in tr["events"]:
            out.append("    %s  %s" % (fmt_rel(t_rel), name))
    else:
        out.append("    (no timed events recorded)")
    ep = tr["endpoint_states"]
    if ep["sequence"]:
        span = ""
        if ep["recorded_from"] is not None:
            span = "   (recorded %s .. %s%s)" % (
                fmt_rel(ep["recorded_from"]), fmt_rel(ep["recorded_to"]),
                ", approx anchor" if anchor_kind == "approx" else "")
        out.append("  endpoint states (%s service_events, drain order):" % (tr["sender"] or "?"))
        out.append("    %s%s" % (" -> ".join(ep["sequence"]), span))
    out.append("")
    if tr["correlation"]:
        c = tr["correlation"]
        out.append("  correlation:")
        out.append("    source=%s" % c["source"])
        out.append("    command_transfer_id=%s" % c["command_transfer_id"])
        out.append("    matched=%s" % c["matched"])
        out.append("    ack_latency=%s" % ("%.3fs" % c["ack_latency_s"]
                                           if c["ack_latency_s"] is not None else NOT_RECORDED))
        out.append("    ack_latency_source=%s" % c["ack_latency_source"])
        if c["recorded_step"]:
            out.append("    recorded_step=%s (linked by step name)" % c["recorded_step"])
            out.append("    recorded_ack_status=%s" % fmt_value(c["recorded_ack_status"]))
            out.append("    recorded_linkage_id=%s" % fmt_value(c["recorded_linkage_id"]))
        else:
            out.append("    recorded_ack_status=%s" % NOT_RECORDED)
            out.append("    recorded_linkage_id=%s" % NOT_RECORDED)
        out.append("    mismatched_ack=%s" % c["mismatched_ack"])
    else:
        i = tr["integrity"]
        out.append("  integrity:")
        out.append("    payload_present=%s" % fmt_value(i["payload_present"]))
        out.append("    payload_valid=%s" % fmt_value(i["payload_valid"]))
        out.append("    payload_sha_match=%s" % fmt_value(i["payload_sha_match"]))
        out.append("    wrong_destination=%s" % fmt_value(i["wrong_destination"]))
        out.append("    duplicate=%s" % i["duplicate"])
    out.append("")
    if tr["step_status"]:
        out.append("  result=%s (step judgement as recorded)" % tr["step_status"])
    else:
        out.append("  result=terminal:%s (no per-transfer step judgement recorded)"
                   % fmt_value(tr["terminal_state"]))


def _render_scope(out, scope, anchor_kind):
    label = "PRECONDITION" if scope["kind"] == "precondition" else "SCENARIO"
    out.append("")
    out.append("[%s] %s — %s" % (label, scope["scenario_id"], scope["status"]))
    if scope["kind"] == "precondition":
        out.append("  precondition_reused=%s" % fmt_value(scope["reused"]))
    out.append("  transfers=%d (rendered from trace; recorded %s)  steps=%d" % (
        len(scope["transfers"]),
        scope["recorded_transfers_count"] if scope["counters_source"] == "recorded"
        else NOT_RECORDED, len(scope["steps"])))
    if scope["reused"] is True and not scope["transfers"]:
        out.append("  (precondition reused from an earlier workload — no transfers in this trace)")
    if not scope["transfers"]:
        out.append("  transfers: none recorded in this trace")
    for n, tr in enumerate(scope["transfers"], 1):
        _render_transfer(out, tr, n, len(scope["transfers"]), anchor_kind)
    out.append("")
    out.append("[%s STEPS]" % label)
    steps = scope["steps"]
    width = max((len(st.get("name") or "") for st in steps), default=10)
    for n, st in enumerate(steps, 1):
        out.append("  [%2d/%d] %-*s %s" % (n, len(steps), width, st.get("name") or "?",
                                          st.get("status") or NOT_RECORDED))
    if not steps:
        out.append("  %s" % NOT_RECORDED)


def render_workload(model: dict) -> list:
    """모델 → 줄 목록 (순수, deterministic). 모든 줄은 redact_text를 거친다."""
    out = []
    label = model["scenario_id"]
    if model.get("certificate_case") and model["scenario_id"] == "SC-VDE-001":
        label += " [%s]" % model["certificate_case"]
    pos = ("[%s/%s] " % (model["index"], model["total"])
           if model.get("index") is not None and model.get("total") is not None else "")
    out.append(MAJOR)
    out.append("%s%s — %s" % (pos, label, model["status"]))
    out.append(MAJOR)
    out.append("  workload_id=%s" % model["workload_id"])
    out.append("  started_utc=%s" % model["started_utc"])
    out.append("  elapsed=%s" % ("%.1fs" % model["elapsed_s"]
                                 if model["elapsed_s"] is not None else NOT_RECORDED))
    for scope in model["scopes"]:
        _render_scope(out, scope, model.get("anchor_kind"))
    if model.get("security"):
        out.append("")
        out.append("[SECURITY]")
        for key in ("certificate_case", "transport_result", "certificate_received",
                    "signature_valid", "verification_reason", "stored_in_trust_store",
                    "trust_store_size", "security_result"):
            out.append("  %s=%s" % (key, fmt_value(model["security"].get(key))))
    out.append("")
    out.append("[ERRORS]")
    if not model["errors"]:
        out.append("  none")
    for err in model["errors"]:
        parts = []
        for key in _ERROR_KEYS:
            if key in err and err[key] is not None:
                value = fmt_rel(err[key]) if key == "first_seen_s" else err[key]
                parts.append("%s=%s" % ("first_seen" if key == "first_seen_s" else key, value))
        out.append("  " + "  ".join(parts))
    out.append("")
    out.append("[SUMMARY]")
    s = model["summary"]
    if s["precondition"]:
        out.append("  precondition=%s" % s["precondition"])
        out.append("  precondition_reused=%s" % fmt_value(s["precondition_reused"]))
        out.append("  precondition_transfers_completed=%s (source=recorded)"
                   % fmt_value(s["precondition_transfers_completed"]))
    else:
        out.append("  precondition=none")
    out.append("  scenario_transfers_completed=%s (source=%s)"
               % (fmt_value(s["scenario_transfers_completed"]), s["counters_source"]))
    out.append("  rendered_transfers_total=%d (source=trace-count)" % s["rendered_transfers_total"])
    for key in ("transfers_failed", "wrong_destination", "mismatched_ack", "duplicates"):
        out.append("  scenario_%s=%s (source=%s)" % (key, fmt_value(s[key]), s["counters_source"]))
    for key in ("non_terminal_transfers", "quiescence", "workers_running", "launchers_alive",
                "coordinator_faults", "inhibited"):
        out.append("  %s=%s" % (key, fmt_value(s.get(key))))
    for warn in model.get("warnings") or []:
        out.append("  [WARNING] %s" % warn)
    if model.get("evidence_path"):
        out.append("  evidence=%s" % model["evidence_path"])
    out.append("")
    return [redact_text(line) for line in out]


def workload_report_lines(trace_rows, result, *, run_root=None,
                          service_events=None, index=None, total=None) -> list:
    """run 경로 편의 함수: 결과 확정 후 필요한 evidence를 한 번 읽고(run_root가
    있을 때) 모델을 만들어 렌더링한다. 실패해도 예외를 밖으로 내지 않는다 —
    판정·close에 영향을 주지 않도록 예외 **클래스 이름만** 담은 경고 한 줄을 돌려준다."""
    try:
        warnings = []
        evidence_path = None
        final_result = None
        if run_root is not None and result.get("workload_id"):
            loaded = load_workload_evidence(
                run_root, result["workload_id"], result=result,
                trace_rows=trace_rows, service_events=service_events)
            final_result = loaded["final_result"]
            service_events = loaded["service_events"]
            warnings = loaded["warnings"]
            evidence_path = str(Path(run_root) / "workloads" / result["workload_id"])
        model = build_workload_model(trace_rows, result, final_result=final_result,
                                     service_events=service_events, index=index,
                                     total=total, evidence_path=evidence_path,
                                     warnings=warnings)
        return render_workload(model)
    except Exception as exc:  # noqa: BLE001 — 표시 실패는 판정에 영향을 주지 않는다
        return ["  [WARNING] scenario report unavailable: %s" % exc.__class__.__name__]
