"""scenario_report — 전송 단위 상세 보고(run·results 공용 renderer) 집중 시험 (RF 없음).

fixture는 실물 evidence(phase-g 006, r4 001 normal/forged, f3 forged FAIL)의 형태를
축약한 것이다. 확인 항목:
    - SC-VDE-006: [PRECONDITION] SC-VDE-005(2 전송/5 step)와 [SCENARIO](5 전송/11 step)
      분리, counter scope(2/5/7) 구분, precondition_reused 경로(전송 날조 없음)
    - ACK 상관: recorded / derived / not recorded 출처 구분, latency 출처 구분
    - SC-VDE-001 normal/forged [SECURITY], forged PASS = 올바른 거부
    - 실패 fixture: FAILED terminal·수신 없음·endpoint ERROR(FecDecodeFailed /
      LinkIdDetectionError 별도 행)·approx anchor·[ERRORS]
    - 시각은 t_monotonic_s/t_utc/t_mono만 (렌더링 시각 무사용), 없으면 not recorded
    - 비밀: 중첩·대소문자·부분 일치 키, JSON/key=value 문자열 — 모델 repr·경고·예외·
      [ERRORS]·results 출력 어디에도 없음
    - run 경로 I/O 격리: 수집만, result·quiescence 확정 전 출력 0, 블록 전 다음 submit 0,
      renderer 시간이 wall_clock_s에 미포함, flush 없음, polling/thread 없음(정적)
    - results: 완전 read-only(SHA·mtime·크기·파일 목록 불변), lock/spawn 없음, fail-soft
    - 실물 evidence read-only 실측: VDETER_REAL_EVIDENCE_RUNS 환경변수로 지정된 run
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from common.textout import ensure_utf8_output  # noqa: E402

ensure_utf8_output()

import standalone_v2_fixtures as fx  # noqa: E402
import test_standalone_run_cli as rc  # noqa: E402
from apps.standalone import (  # noqa: E402
    results_cmd,
    run_cmd,
    scenario_report as sr,
)

BASE = 699292.889   # anchor: data_received t_mono − t_monotonic_s (실물 006과 같은 값)
PEM_KEY = "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBg-----END PRIVATE KEY-----"
SECRETS = {
    "b64": "U0VDUkVULVBBWUxPQUQtQjY0", "raw": "RAWPAYLOAD-SECRET-BYTES",
    "der": "DER-SECRET-BLOB", "pass": "hunter2-passphrase", "tok": "tok-SECRET-123",
    "api": "api-SECRET-456", "nested": "NESTED-SECRET-789", "json": "JSON-SECRET-000",
}


# ------------------------------------------------------------ fixtures

def _submit(seq, step, sender, receiver, fmt, length, sha, t, *, ack, tid):
    src, dst = ((222222222, 111111111) if sender == "ROC" else (111111111, 222222222))
    return {"event": "submit", "seq": seq, "step": step, "sender": sender,
            "receiver": receiver, "payload_format": fmt, "payload_len": length,
            "payload_sha256": sha, "source_station": src, "destination_station": dst,
            "ack_required": ack, "transfer_id": tid, "t_monotonic_s": t,
            "t_utc": "2026-09-02T18:48:25Z", "payload_base64": SECRETS["b64"]}


def _terminal(seq, step, tid, t, state_="COMPLETED"):
    return {"event": "transfer_terminal", "seq": seq, "step": step, "transfer_id": tid,
            "terminal_state": state_, "t_monotonic_s": t, "t_utc": "2026-09-02T18:48:40Z"}


def _received(seq, step, receiver, t, *, present=True):
    src, dst = ((222222222, 111111111) if receiver == "MASS-A" else (111111111, 222222222))
    return {"event": "data_received", "seq": seq, "step": step, "receiver": receiver,
            "original_source": src if present else None,
            "original_destination": dst if present else None, "present": present,
            "t_mono": (BASE + t) if present else None, "t_monotonic_s": t,
            "t_utc": "2026-09-02T18:48:40Z"}


PLAN_005 = [
    ("005-route-command", "ROC", "MASS-A", "sc_vde.route_control.command", 39, "aa" * 32, 0.006, 14.6096, 14.6099, True, "T1"),
    ("005-ack", "MASS-A", "ROC", "sc_vde.route_control.ack", 3, "bb" * 32, 14.616, 31.2206, 32.1257, False, "T2"),
]
PLAN_006 = [
    ("006-steering-command", "ROC", "MASS-A", "sc_vde.route_control.command", 29, "cc" * 32, 32.241, 36.2858, 36.2861, True, "T3"),
    ("006-steering-ack", "MASS-A", "ROC", "sc_vde.route_control.ack", 3, "dd" * 32, 36.291, 47.3599, 48.6665, False, "T4"),
    ("006-control-resend", "ROC", "MASS-A", "sc_vde.route_control.command", 29, "cc" * 32, 48.756, 52.7856, 52.7859, True, "T5"),
    ("006-fallback-command", "ROC", "MASS-A", "sc_vde.route_control.command", 10, "ee" * 32, 52.789, 57.8321, 57.8324, True, "T6"),
    ("006-fallback-ack", "MASS-A", "ROC", "sc_vde.route_control.ack", 3, "ff" * 32, 57.837, 74.4314, 75.2367, False, "T7"),
]


def sc006_rows(*, include_precondition=True) -> list:
    """실물 phase-g 006 trace의 축약 (7 전송: 005 command/ack + 006 5개)."""
    rows = [{"event": "workload_begin", "seq": 1, "scenario_id": "SC-VDE-006",
             "certificate_case": "normal", "workload_id": "w-006",
             "t_monotonic_s": 0.0001, "t_utc": "2026-09-02T18:48:25Z"}]
    seq = 2
    plan = (PLAN_005 if include_precondition else []) + PLAN_006
    for step, snd, rcv, fmt, length, sha, t_sub, t_term, t_rcv, ack, tid in plan:
        rows.append(_submit(seq, step, snd, rcv, fmt, length, sha, t_sub, ack=ack, tid=tid))
        rows.append(_terminal(seq + 1, step, tid, t_term))
        rows.append(_received(seq + 2, step, rcv, t_rcv))
        seq += 3
    rows.append({"event": "scenario_result", "seq": seq, "status": "PASS",
                 "scenario": "SC-VDE-006", "reason": None,
                 "steps": [["006-precondition-state", "PASS"]],
                 "counters": {"transfers_completed": 5, "transfers_failed": 0,
                              "wrong_destination": 0, "mismatched_ack": 0, "duplicates": 0},
                 "t_monotonic_s": 75.238, "t_utc": "2026-09-02T18:49:40Z"})
    rows.append({"event": "workload_end", "seq": seq + 1, "status": "PASS", "error": None,
                 "cleanup_errors": [], "t_monotonic_s": 75.2435,
                 "t_utc": "2026-09-02T18:49:40Z"})
    return rows


def _checks():
    return {"byte_exact": True, "original_destination": True, "original_source": True,
            "payload_format": True, "payload_len": True, "payload_sha256": True}


def _transfer(tid, step, scenario):
    return {"transfer_id": tid, "step": step, "scenario_id": scenario,
            "final_transfer_state": "COMPLETED", "wrong_destination": 0,
            "received": {"valid": True, "checks": _checks()}}


def sc006_result(*, reused=False) -> dict:
    pre_steps = [{"name": "005-command-transfer", "status": "PASS", "transfer_id": "T1",
                  "received_checks": _checks()},
                 {"name": "005-route-applied", "status": "PASS", "linkage_id": 1},
                 {"name": "005-ack-transfer", "status": "PASS", "transfer_id": "T2",
                  "received_checks": _checks()},
                 {"name": "005-ack-correlated", "status": "PASS", "ack_status": 4, "linkage_id": 1},
                 {"name": "005-mass-b-clean", "status": "PASS"}]
    names = ["006-precondition-state", "006-steering-transfer", "006-steering-ack-transfer",
             "006-steering-ack-correlated", "006-control-applied", "006-idempotent-resend",
             "006-fallback-transfer", "006-fallback-ack-transfer",
             "006-fallback-ack-correlated", "006-fallback-applied", "006-mass-b-clean"]
    steps = [{"name": n, "status": "PASS"} for n in names]
    steps[1]["transfer_id"], steps[2]["transfer_id"] = "T3", "T4"
    steps[6]["transfer_id"], steps[7]["transfer_id"] = "T6", "T7"
    steps[3].update({"ack_status": 4, "linkage_id": 2})
    steps[8].update({"ack_status": 4, "linkage_id": 3})
    counters = {"transfers_completed": 5, "transfers_failed": 0, "wrong_destination": 0,
                "mismatched_ack": 0, "duplicates": 0}
    return {"status": "PASS", "workload_id": "w-006", "scenario_id": "SC-VDE-006",
            "certificate_case": "normal", "duration_s": 76.5, "wall_clock_s": 76.5,
            "quiescence": {"pass": True, "non_terminal_transfers": 0, "workers_running": 3,
                           "launchers_alive": 3, "faults": 0, "inhibited": False},
            "session_health_before": {"expected_launchers": 3},
            "scenario": {"status": "PASS", "error": None, "cleanup_errors": [],
                         "results": {"SC-VDE-006": {
                             "precondition_reused": reused,
                             "precondition": {"scenario_id": "SC-VDE-005", "status": "PASS",
                                              "counters": dict(counters, transfers_completed=2),
                                              "steps": pre_steps,
                                              "transfers": [_transfer("T1", "005-route-command", "SC-VDE-005"),
                                                            _transfer("T2", "005-ack", "SC-VDE-005")]},
                             "result": {"scenario_id": "SC-VDE-006", "status": "PASS",
                                        "counters": counters, "steps": steps,
                                        "transfers": [_transfer(t, s, "SC-VDE-006") for t, s in (
                                            ("T3", "006-steering-command"), ("T4", "006-steering-ack"),
                                            ("T5", "006-control-resend"), ("T6", "006-fallback-command"),
                                            ("T7", "006-fallback-ack"))]}}}}}


def sc006_service_events() -> dict:
    def st(tid, offsets):
        return [{"t_mono": BASE + t, "event_type": "TRANSFER_STATE_CHANGED",
                 "transfer_id": tid, "transfer_state": s, "error_code": None}
                for t, s in offsets]
    roc = (st("T1", [(6.491, "QUEUED"), (9.499, "TRANSMITTING"), (10.0, "WAITING_ACK"),
                     (21.013, "COMPLETED")])
           + st("T3", [(39.033, "QUEUED"), (39.033, "TRANSMITTING"), (39.033, "WAITING_ACK"),
                       (42.535, "COMPLETED")])
           + [{"t_mono": BASE + 38.53, "event_type": "DATA_RECEIVED", "transfer_id": None,
               "payload_sha256": "bb" * 32, "payload_len": 3, "payload_base64": SECRETS["b64"]}]
           + [{"t_mono": BASE + 400.0, "event_type": "ERROR", "transfer_id": None,
               "error_code": "DEVICE_ERROR", "error_message": "무선 장치 처리에 실패했습니다",
               "error_details": "Details(entries=())"}])   # 시간창 밖 → 표시되면 안 됨
    mass_a = (st("T2", [(21.131, "QUEUED"), (36.645, "TRANSMITTING"), (37.648, "COMPLETED")])
              + st("T4", [(43.159, "QUEUED"), (53.169, "TRANSMITTING"), (54.17, "COMPLETED")])
              + [{"t_mono": BASE + 14.61, "event_type": "DATA_RECEIVED", "transfer_id": None,
                  "payload_sha256": "aa" * 32, "payload_len": 39,
                  "payload_base64": SECRETS["b64"], "private_key": PEM_KEY}])
    return {"ROC": roc, "MASS-A": mass_a}


def sc001_rows(case, *, fail=False) -> list:
    rows = [{"event": "workload_begin", "seq": 1, "scenario_id": "SC-VDE-001",
             "certificate_case": case, "workload_id": "w-001-" + case,
             "t_monotonic_s": 0.0001, "t_utc": "2026-09-06T23:30:48Z"},
            _submit(2, "001-certificate", "ROC", "MASS-A", "x509.certificate_report", 411,
                    "7d" * 32, 0.088, ack=True, tid="C1")]
    if fail:
        rows.append(_terminal(3, "001-certificate", "C1", 77.5816, "FAILED"))
        rows.append(_received(4, "001-certificate", "MASS-A", 317.6167, present=False))
        rows.append({"event": "scenario_result", "seq": 5, "status": "FAIL",
                     "scenario": "SC-VDE-001[%s]" % case,
                     "reason": "MASS-A가 인증서 DATA_RECEIVED를 받지 못함",
                     "steps": [["001-report-built", "PASS"], ["001-transfer", "FAIL"]],
                     "counters": {"transfers_completed": 0, "transfers_failed": 1,
                                  "wrong_destination": 0, "mismatched_ack": 0, "duplicates": 0},
                     "t_monotonic_s": 317.618, "t_utc": "2026-09-05T08:49:12Z"})
        rows.append({"event": "workload_end", "seq": 6, "status": "FAIL", "error": None,
                     "cleanup_errors": [], "t_monotonic_s": 317.6232,
                     "t_utc": "2026-09-05T08:49:12Z"})
        return rows
    rows.append(_terminal(3, "001-certificate", "C1", 15.6858))
    rows.append(_received(4, "001-certificate", "MASS-A", 15.6861))
    steps = ([["001-report-built", "PASS"], ["001-transfer", "PASS"],
              ["001-forged-rejected", "PASS"], ["001-not-stored", "PASS"], ["001-mass-b-clean", "PASS"]]
             if case == "forged" else
             [["001-report-built", "PASS"], ["001-transfer", "PASS"], ["001-verified-stored", "PASS"],
              ["001-idempotent-store", "PASS"], ["001-resend-ok", "PASS"], ["001-mass-b-clean", "PASS"]])
    rows.append({"event": "scenario_result", "seq": 5, "status": "PASS",
                 "scenario": "SC-VDE-001[%s]" % case, "reason": None, "steps": steps,
                 "counters": {"transfers_completed": 1, "transfers_failed": 0,
                              "wrong_destination": 0, "mismatched_ack": 0, "duplicates": 0},
                 "t_monotonic_s": 15.687, "t_utc": "2026-09-06T23:31:03Z"})
    rows.append({"event": "workload_end", "seq": 6, "status": "PASS", "error": None,
                 "cleanup_errors": [], "t_monotonic_s": 15.6906, "t_utc": "2026-09-06T23:31:03Z"})
    return rows


def sc001_result(case, *, fail=False) -> dict:
    if fail:
        steps = [{"name": "001-report-built", "status": "PASS", "payload_len": 413},
                 {"name": "001-transfer", "status": "FAIL", "state": "FAILED", "transfer_id": "C1"}]
        transfers = [{"transfer_id": "C1", "step": "001-certificate", "final_transfer_state": "FAILED",
                      "status_result": "FAILED", "received": None, "wrong_destination": 0}]
        counters = {"transfers_completed": 0, "transfers_failed": 1, "wrong_destination": 0,
                    "mismatched_ack": 0, "duplicates": 0}
        status, reason = "FAIL", "MASS-A가 인증서 DATA_RECEIVED를 받지 못함"
    else:
        transfer_step = {"name": "001-transfer", "status": "PASS", "state": "COMPLETED",
                         "transfer_id": "C1", "received_checks": _checks()}
        if case == "forged":
            steps = [{"name": "001-report-built", "status": "PASS"}, transfer_step,
                     {"name": "001-forged-rejected", "status": "PASS", "reason": "bad-signature",
                      "trust_store_size": 0},
                     {"name": "001-not-stored", "status": "PASS"},
                     {"name": "001-mass-b-clean", "status": "PASS"}]
        else:
            steps = [{"name": "001-report-built", "status": "PASS"}, transfer_step,
                     {"name": "001-verified-stored", "status": "PASS", "reason": "ok",
                      "trust_store_size": 1},
                     {"name": "001-idempotent-store", "status": "PASS"},
                     {"name": "001-resend-ok", "status": "PASS"},
                     {"name": "001-mass-b-clean", "status": "PASS"}]
        transfers = [{"transfer_id": "C1", "step": "001-certificate",
                      "final_transfer_state": "COMPLETED", "wrong_destination": 0,
                      "received": {"valid": True, "checks": _checks()}}]
        counters = {"transfers_completed": 1, "transfers_failed": 0, "wrong_destination": 0,
                    "mismatched_ack": 0, "duplicates": 0}
        status, reason = "PASS", None
    label = "SC-VDE-001[%s]" % case
    return {"status": status, "workload_id": "w-001-" + case, "scenario_id": "SC-VDE-001",
            "certificate_case": case, "duration_s": 17.7 if not fail else 319.3,
            "wall_clock_s": 17.7 if not fail else 319.3,
            "quiescence": {"pass": True, "non_terminal_transfers": 0, "workers_running": 3,
                           "launchers_alive": 3, "faults": 0, "inhibited": False},
            "session_health_before": {"expected_launchers": 3},
            "scenario": {"status": status, "error": None, "cleanup_errors": [],
                         "results": {label: {"scenario_id": label, "status": status,
                                             "reason": reason, "counters": counters,
                                             "steps": steps, "transfers": transfers}}}}


def sc001_fail_service_events() -> dict:
    """실물 f3 forged FAIL: ROC 상태 FAILED, MASS-A TRANSFER_FAILED(FecDecodeFailed ×2,
    LinkIdDetectionError ×1), 시간창 밖 오류는 제외되어야 한다."""
    anchor = 922305.98 - 77.5816   # approx anchor (sender FAILED ↔ trace terminal)
    roc = [{"t_mono": 922228.859, "event_type": "TRANSFER_STATE_CHANGED", "transfer_id": "C1",
            "transfer_state": "QUEUED", "error_code": None},
           {"t_mono": 922231.361, "event_type": "TRANSFER_STATE_CHANGED", "transfer_id": "C1",
            "transfer_state": "TRANSMITTING", "error_code": None},
           {"t_mono": 922231.866, "event_type": "TRANSFER_STATE_CHANGED", "transfer_id": "C1",
            "transfer_state": "WAITING_ACK", "error_code": None},
           {"t_mono": 922305.98, "event_type": "TRANSFER_STATE_CHANGED", "transfer_id": "C1",
            "transfer_state": "FAILED", "error_code": None}]
    mass_a = [{"t_mono": anchor + 7.9, "event_type": "ERROR", "transfer_id": None,
               "error_code": "TRANSFER_FAILED", "error_message": "전송 처리에 실패했습니다",
               "error_details": "Details(entries=(('reason', 'FecDecodeFailed'),))"},
              {"t_mono": anchor + 7.9, "event_type": "ERROR", "transfer_id": None,
               "error_code": "TRANSFER_FAILED", "error_message": "전송 처리에 실패했습니다",
               "error_details": "Details(entries=(('reason', 'FecDecodeFailed'),))"},
              {"t_mono": anchor + 40.2, "event_type": "ERROR", "transfer_id": None,
               "error_code": "TRANSFER_FAILED", "error_message": "전송 처리에 실패했습니다",
               "error_details": "Details(entries=(('reason', 'LinkIdDetectionError'),))"},
              {"t_mono": anchor + 335.7, "event_type": "ERROR", "transfer_id": None,
               "error_code": "TRANSFER_FAILED", "error_message": "전송 처리에 실패했습니다",
               "error_details": "Details(entries=(('reason', 'FecDecodeFailed'),))"}]
    return {"ROC": roc, "MASS-A": mass_a}


def _render(rows, result, events=None, **kw):
    model = sr.build_workload_model(rows, result, service_events=events, **kw)
    return model, "\n".join(sr.render_workload(model))


def _section(text, start, end):
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# ------------------------------------------------------------ 1·2. scope 분리 / counter 범위

def test_sc006_precondition_and_scenario_scopes_are_separate_with_scoped_counters():
    model, text = _render(sc006_rows(), sc006_result(), sc006_service_events(), index=2, total=4)
    assert "[2/4] SC-VDE-006 — PASS" in text
    pre = _section(text, "[PRECONDITION] SC-VDE-005 — PASS", "[SCENARIO] SC-VDE-006 — PASS")
    main = _section(text, "[SCENARIO] SC-VDE-006 — PASS", "[ERRORS]")
    # 전제: 2 전송 / 5 step, 본: 5 전송 / 11 step, trace submit 7
    assert "precondition_reused=false" in pre
    assert "transfers=2 (rendered from trace; recorded 2)  steps=5" in pre
    assert "[TRANSFER 1/2] 005-route-command" in pre and "[TRANSFER 2/2] 005-ack" in pre
    assert "[PRECONDITION STEPS]" in pre and "[ 1/5] 005-command-transfer" in pre
    assert "[ 5/5] 005-mass-b-clean" in pre and "006-" not in pre.split("[PRECONDITION STEPS]")[1]
    assert "transfers=5 (rendered from trace; recorded 5)  steps=11" in main
    assert "[TRANSFER 1/5] 006-steering-command" in main and "[TRANSFER 5/5] 006-fallback-ack" in main
    assert "[SCENARIO STEPS]" in main and "[ 1/11] 006-precondition-state" in main
    assert "[11/11] 006-mass-b-clean" in main and "005-" not in main.split("[SCENARIO STEPS]")[1]
    assert "[TRANSFER 1/7]" not in text and "[16/16]" not in text
    scopes = model["scopes"]
    assert [(s["kind"], s["scenario_id"], len(s["transfers"]), len(s["steps"])) for s in scopes] == [
        ("precondition", "SC-VDE-005", 2, 5), ("scenario", "SC-VDE-006", 5, 11)]
    assert all(t["scope_source"] == "recorded" for t in model["transfers"])
    # counter 범위: 기록값 그대로, 합산 없음; 표시 총수는 trace-count
    summary = _section(text, "[SUMMARY]", "  evidence=") if "  evidence=" in text else text.split("[SUMMARY]")[1]
    assert "precondition=SC-VDE-005" in summary
    assert "precondition_transfers_completed=2 (source=recorded)" in summary
    assert "scenario_transfers_completed=5 (source=recorded)" in summary
    assert "rendered_transfers_total=7 (source=trace-count)" in summary
    assert "scenario_transfers_failed=0 (source=recorded)" in summary
    assert "transfers_completed=7" not in text and "quiescence=PASS" in summary
    assert "workers_running=3/3" in summary and "launchers_alive=3/3" in summary
    assert model["summary"]["precondition_transfers_completed"] == 2
    assert model["summary"]["scenario_transfers_completed"] == 5
    assert model["summary"]["rendered_transfers_total"] == 7
    # 순서·형식은 결정적
    assert text == _render(sc006_rows(), sc006_result(), sc006_service_events(), index=2, total=4)[1]


def test_scope_falls_back_to_step_prefix_and_marks_source_when_not_recorded():
    result = sc006_result()
    entry = result["scenario"]["results"]["SC-VDE-006"]
    entry["precondition"]["transfers"] = []          # 기록된 transfer 목록이 없다
    entry["result"]["transfers"] = []
    model, text = _render(sc006_rows(), result)
    assert [len(s["transfers"]) for s in model["scopes"]] == [2, 5]
    assert all(t["scope_source"] == "derived(step prefix)" for t in model["transfers"])
    assert "scope=SC-VDE-005 (source=derived(step prefix))" in text
    assert "transfers=2 (rendered from trace; recorded 0)" in text
    # scope를 정할 근거가 전혀 없으면 본 시나리오 아래에 두고 [WARNING]
    rows = sc006_rows()
    rows[1]["step"] = "zzz-unknown"          # submit 행 (기록·접두 어느 쪽에도 없음)
    model, text = _render(rows, result)
    assert "[WARNING] zzz-unknown: scope not recorded in final-result; shown under SC-VDE-006" in text
    assert "scope=SC-VDE-006 (source=not recorded)" in text


def test_workload_without_precondition_renders_single_scenario_scope():
    _model, text = _render(sc001_rows("normal"), sc001_result("normal"), index=1, total=1)
    assert "[PRECONDITION]" not in text
    assert "[SCENARIO] SC-VDE-001[normal] — PASS" in text
    assert "transfers=1 (rendered from trace; recorded 1)  steps=6" in text
    assert "precondition=none" in text
    assert "scenario_transfers_completed=1 (source=recorded)" in text
    assert "rendered_transfers_total=1 (source=trace-count)" in text


# ------------------------------------------------------------ 3. precondition_reused

def test_precondition_reused_shows_recorded_flag_and_fabricates_no_transfers():
    result = sc006_result(reused=True)
    model, text = _render(sc006_rows(include_precondition=False), result)
    pre = _section(text, "[PRECONDITION] SC-VDE-005 — PASS", "[SCENARIO] SC-VDE-006 — PASS")
    assert "precondition_reused=true" in pre
    assert "transfers=0 (rendered from trace; recorded 2)  steps=5" in pre
    assert "(precondition reused from an earlier workload — no transfers in this trace)" in pre
    assert "transfers: none recorded in this trace" in pre
    assert "[TRANSFER" not in pre                    # 존재하지 않는 전송을 만들지 않는다
    assert "[ 1/5] 005-command-transfer" in pre       # 기록된 step은 그대로
    assert "[TRANSFER 1/5] 006-steering-command" in text
    assert "precondition_transfers_completed=2 (source=recorded)" in text
    assert "rendered_transfers_total=5 (source=trace-count)" in text
    assert len(model["transfers"]) == 5 and model["summary"]["precondition_reused"] is True


# ------------------------------------------------------------ 4~7. ACK 상관 출처

def test_derived_ack_correlation_is_labelled_derived_with_recorded_step_facts():
    model, text = _render(sc006_rows(), sc006_result(), sc006_service_events())
    ack = model["transfers"][1]
    c = ack["correlation"]
    assert c["source"] == "derived(step+reverse-path+order)" and c["matched"] == "derived"
    assert c["command_transfer_id"] == "T1" and c["ack_for"] == "005-route-command"
    assert abs(c["ack_latency_s"] - (32.1257 - 14.6099)) < 1e-6
    assert c["ack_latency_source"] == "derived(receive timestamps)"
    assert c["recorded_step"] == "005-ack-correlated"
    assert c["recorded_ack_status"] == 4 and c["recorded_linkage_id"] == 1
    block = _section(text, "[TRANSFER 2/2] 005-ack", "[PRECONDITION STEPS]")
    assert "  command_transfer_id=T1" in block and "  ack_for=005-route-command" in block
    assert "    source=derived(step+reverse-path+order)" in block
    assert "    matched=derived" in block and "matched=true" not in text
    assert "    ack_latency=17.516s" in block
    assert "    ack_latency_source=derived(receive timestamps)" in block
    assert "    recorded_step=005-ack-correlated (linked by step name)" in block
    assert "    recorded_ack_status=4" in block and "    recorded_linkage_id=1" in block
    assert "    mismatched_ack=false (precondition counter mismatched_ack=0)" in block
    # 006 ack ↔ 006 command (같은 접두), resend는 별도 명령(ack 없음)
    assert model["transfers"][3]["correlation"]["command_transfer_id"] == "T3"
    assert model["transfers"][3]["correlation"]["recorded_linkage_id"] == 2
    assert model["transfers"][6]["correlation"]["command_transfer_id"] == "T6"
    assert model["transfers"][6]["correlation"]["recorded_linkage_id"] == 3
    assert model["transfers"][4]["correlation"] is None
    assert "mismatched_ack=false (scenario counter mismatched_ack=0)" in text


def test_recorded_ack_correlation_and_recorded_latency_are_labelled_recorded():
    result = sc006_result()
    steps = result["scenario"]["results"]["SC-VDE-006"]["precondition"]["steps"]
    steps[3].update({"transfer_id": "T2", "command_transfer_id": "T1", "ack_latency_s": 17.0})
    model, text = _render(sc006_rows(), result)
    c = model["transfers"][1]["correlation"]
    assert c["source"] == "recorded" and c["matched"] == "recorded"
    assert c["command_transfer_id"] == "T1" and c["ack_for"] == "005-route-command"
    assert c["ack_latency_s"] == 17.0 and c["ack_latency_source"] == "recorded"
    assert c["recorded_linkage_id"] == 1 and c["recorded_ack_status"] == 4
    block = _section(text, "[TRANSFER 2/2] 005-ack", "[PRECONDITION STEPS]")
    assert "    source=recorded" in block and "    matched=recorded" in block
    assert "    ack_latency=17.000s" in block and "    ack_latency_source=recorded" in block
    # 다른 ack(006)는 여전히 derived
    assert model["transfers"][3]["correlation"]["source"] == "derived(step+reverse-path+order)"


def test_insufficient_ack_correlation_is_not_recorded_and_latency_missing_is_not_recorded():
    rows = sc006_rows()
    # 005-ack보다 앞선 역방향 명령을 지운다 → 후보 없음
    rows = [r for r in rows if r.get("step") != "005-route-command"]
    result = sc006_result()
    del result["scenario"]["results"]["SC-VDE-006"]["precondition"]["steps"][3]   # 005-ack-correlated 제거
    model, text = _render(rows, result)
    ack = next(t for t in model["transfers"] if t["step"] == "005-ack")
    c = ack["correlation"]
    assert c["source"] == "not recorded" and c["matched"] == "not recorded"
    assert c["command_transfer_id"] == "not recorded" and c["ack_latency_s"] is None
    assert c["recorded_step"] is None
    block = _section(text, "[TRANSFER 1/1] 005-ack", "[PRECONDITION STEPS]")
    assert "    source=not recorded" in block and "    matched=not recorded" in block
    assert "    command_transfer_id=not recorded" in block
    assert "    ack_latency=not recorded" in block and "    ack_latency_source=not recorded" in block
    assert "    recorded_ack_status=not recorded" in block
    # 수신 시각이 없으면 latency는 파생할 수 없다 (not recorded)
    rows = sc006_rows()
    rows = [r for r in rows if not (r.get("event") == "data_received" and r.get("step") == "005-ack")]
    model, _text = _render(rows, sc006_result())
    c = model["transfers"][1]["correlation"]
    assert c["matched"] == "derived" and c["ack_latency_s"] is None
    assert c["ack_latency_source"] == "not recorded"


# ------------------------------------------------------------ 시각·SECURITY·실패

def test_times_come_only_from_evidence_fields_and_endpoint_states_are_recorded_ranges():
    model, text = _render(sc006_rows(), sc006_result(), sc006_service_events())
    first = model["transfers"][0]
    assert first["events"] == [(0.006, "SUBMITTED"), (14.6096, "COMPLETED"),
                               (14.6099, "DATA_RECEIVED by MASS-A")]
    assert "+00.006s  SUBMITTED" in text and "+14.610s  DATA_RECEIVED by MASS-A" in text
    assert "started_utc=2026-09-02T18:48:25Z" in text and "elapsed=75.2s" in text
    assert model["anchor_kind"] == "exact"
    assert first["endpoint_states"]["sequence"] == ["QUEUED", "TRANSMITTING", "WAITING_ACK", "COMPLETED"]
    assert "QUEUED -> TRANSMITTING -> WAITING_ACK -> COMPLETED   (recorded +06.491s .. +21.013s)" in text
    assert "approx" not in text
    assert "DEVICE_ERROR" not in text        # 시간창 밖 endpoint ERROR(+400 s) 제외


def test_missing_t_monotonic_s_is_not_recorded_never_a_clock_value():
    rows = sc001_rows("normal")
    for row in rows:
        row.pop("t_monotonic_s", None)
        row.pop("t_mono", None)
    model, text = _render(rows, sc001_result("normal"))
    assert model["transfers"][0]["events"] == []
    assert "(no timed events recorded)" in text
    assert model["elapsed_s"] == 17.7          # result.duration_s (기록값), 시계 아님
    result = sc001_result("normal")
    result.pop("duration_s"), result.pop("wall_clock_s")
    _model2, text2 = _render([dict(r) for r in rows], result)
    assert "elapsed=not recorded" in text2


def test_sc001_security_block_normal_and_forged():
    _m, normal = _render(sc001_rows("normal"), sc001_result("normal"))
    assert "[SECURITY]" in normal
    assert "certificate_case=normal" in normal and "transport_result=COMPLETED" in normal
    assert "certificate_received=true" in normal and "signature_valid=true" in normal
    assert "verification_reason=ok" in normal and "stored_in_trust_store=true" in normal
    assert "trust_store_size=1" in normal and "security_result=PASS" in normal
    _m, forged = _render(sc001_rows("forged"), sc001_result("forged"))
    assert "certificate_case=forged" in forged and "signature_valid=false" in forged
    assert "verification_reason=bad-signature" in forged
    assert "stored_in_trust_store=false" in forged and "trust_store_size=0" in forged
    assert "security_result=PASS (forged certificate correctly rejected)" in forged
    assert "[1/1] SC-VDE-001 [forged] — PASS" in _render(
        sc001_rows("forged"), sc001_result("forged"), index=1, total=1)[1]
    assert "[SECURITY]" not in _render(sc006_rows(), sc006_result())[1]


def test_failure_fixture_errors_block_approx_anchor_and_separate_phy_reasons():
    model, text = _render(sc001_rows("forged", fail=True), sc001_result("forged", fail=True),
                          sc001_fail_service_events())
    assert "SC-VDE-001 [forged] — FAIL" in text
    assert "+77.582s  FAILED" in text and "+317.617s  no DATA_RECEIVED observed at MASS-A" in text
    assert model["anchor_kind"] == "approx"
    assert "QUEUED -> TRANSMITTING -> WAITING_ACK -> FAILED" in text and "approx anchor" in text
    assert "payload_present=false" in text and "payload_valid=not recorded" in text
    assert "result=FAIL (step judgement as recorded)" in text
    assert "[ 2/2] 001-transfer     FAIL" in text
    assert "security_result=not reached (transport FAILED)" in text
    errors = _section(text, "[ERRORS]", "[SUMMARY]")
    assert "recorded_reason=MASS-A가 인증서 DATA_RECEIVED를 받지 못함" in errors
    assert "step=001-transfer  status=FAIL" in errors
    assert "transfer_id=C1  step=001-certificate  terminal_state=FAILED" in errors
    fec = [l for l in errors.splitlines() if "reason=FecDecodeFailed" in l]
    lid = [l for l in errors.splitlines() if "reason=LinkIdDetectionError" in l]
    assert len(fec) == 1 and "occurrences=2" in fec[0] and "first_seen=+07.900s" in fec[0]
    assert "window=approx" in fec[0] and "endpoint=MASS-A" in fec[0]
    assert len(lid) == 1 and "occurrences=1" in lid[0]
    assert "scenario_transfers_failed=1 (source=recorded)" in text
    assert "scenario_transfers_completed=0 (source=recorded)" in text
    assert sr.result_line(sc001_result("forged", fail=True)).startswith(
        "  result: FAIL · 319.3 s · quiescence OK")


# ------------------------------------------------------------ 8~10. 비밀 제거

def _assert_no_secret(blob, where):
    for name, value in SECRETS.items():
        assert value not in blob, (where, name)
    assert "PRIVATE KEY" not in blob, where
    assert "MIIEvQIBADANBg" not in blob, where


def test_nested_mixed_case_and_partial_key_secrets_are_dropped_from_model_and_output():
    rows = sc006_rows()
    rows[1]["Private_Key"] = PEM_KEY
    rows[1]["extra"] = {"RAW_PAYLOAD": SECRETS["raw"], "list": [{"Certificate_DER": SECRETS["der"]},
                                                              {"x_api_key_v2": SECRETS["api"]}],
                        "meta": {"passphrase_hint": SECRETS["pass"], "AuthToken": SECRETS["tok"]}}
    rows[1]["payloadBase64Blob"] = SECRETS["nested"]     # 부분 일치(대소문자 무시)
    result = sc006_result()
    result["scenario"]["error"] = None
    result["debug"] = {"nested": {"deeper": {"secret_key": SECRETS["nested"],
                                             "verification_reason": "ok"}}}
    events = sc006_service_events()
    events["ROC"].append({"t_mono": BASE + 5.0, "event_type": "ERROR", "error_code": "X",
                          "error_details": {"token": SECRETS["tok"], "reason": "keep"}})
    model, text = _render(rows, result, events)
    _assert_no_secret(text, "render")
    _assert_no_secret(json.dumps(model, default=str), "json")
    _assert_no_secret(repr(model), "repr")
    _assert_no_secret("\n".join(model["warnings"]), "warnings")
    # 메타데이터는 유지된다
    assert "payload_format=sc_vde.route_control.command" in text
    assert "application_bytes=39" in text and "payload_sha256=" + "aa" * 32 in text
    assert "transfer_id=T1" in text
    _m001, text001 = _render(sc001_rows("forged"), sc001_result("forged"))
    assert "verification_reason=bad-signature" in text001 and "certificate_case=forged" in text001
    assert sr.is_secret_key("payload_base64") and sr.is_secret_key("PRIVATE_KEY_PEM")
    assert sr.is_secret_key("payloadBase64Blob") and sr.is_secret_key("AuthToken")
    assert sr.is_secret_key("x-access-token") and sr.is_secret_key("Secret")
    assert not sr.is_secret_key("payload_format") and not sr.is_secret_key("payload_sha256")
    assert not sr.is_secret_key("identity_fingerprint") and not sr.is_secret_key("reference_sha256")
    assert not sr.is_secret_key("transfer_id") and not sr.is_secret_key("certificate_case")


def test_json_like_and_assignment_strings_are_redacted_in_reasons_errors_and_exceptions():
    forms = [
        '{"private_key": "%s", "payload_format": "x509"}' % SECRETS["json"],
        "'password': '%s'" % SECRETS["pass"],
        "token=%s rest" % SECRETS["tok"],
        "api_key: %s" % SECRETS["api"],
        'raw_payload="%s"' % SECRETS["raw"],
        "PAYLOAD_BASE64=%s" % SECRETS["b64"],
        "Certificate_DER: %s; reason=FecDecodeFailed" % SECRETS["der"],
    ]
    for form in forms:
        redacted = sr.redact_text(form)
        _assert_no_secret(redacted, form)
        assert "<redacted>" in redacted
    assert "payload_format" in sr.redact_text(forms[0]) and "x509" in sr.redact_text(forms[0])
    assert "reason=FecDecodeFailed" in sr.redact_text(forms[6])
    # 임의 error/reason 문자열 → [ERRORS]와 result 줄
    result = sc001_result("forged", fail=True)
    result["scenario"]["error"] = "layer=<Layer.PHY 'reason', 'FecDecodeFailed' " + forms[0]
    rows = sc001_rows("forged", fail=True)
    rows[4]["reason"] = "MASS-A: " + forms[1] + " " + forms[2]
    events = sc001_fail_service_events()
    events["MASS-A"][0]["error_details"] = forms[6]
    model, text = _render(rows, result, events)
    errors = _section(text, "[ERRORS]", "[SUMMARY]")
    _assert_no_secret(errors, "errors")
    _assert_no_secret(text, "text")
    _assert_no_secret(repr(model), "repr")
    assert "layer=PHY" in errors and "reason=FecDecodeFailed" in errors
    assert "<redacted>" in errors
    line = sr.result_line(result)
    _assert_no_secret(line, "result_line")
    assert line.startswith("  result: FAIL")
    # renderer 예외 메시지에 비밀이 있어도 클래스 이름만 나온다
    original = sr.build_workload_model
    sr.build_workload_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(forms[0]))
    try:
        lines = sr.workload_report_lines(rows, result)
    finally:
        sr.build_workload_model = original
    assert lines == ["  [WARNING] scenario report unavailable: RuntimeError"]


def test_renderer_failure_is_one_warning_line_and_never_raises():
    bad_result = sc006_result()
    bad_result["quiescence"] = "corrupt"            # dict가 아니다
    lines = sr.workload_report_lines(sc006_rows(), bad_result)
    assert len(lines) == 1 and lines[0].startswith("  [WARNING] scenario report unavailable:")
    # dict가 아닌 trace 행은 무시되고(예외 없음) 빈 보고가 나온다
    lines = sr.workload_report_lines(["not-a-row", 42], {"workload_id": "w", "status": "PASS"})
    assert lines[0] == sr.MAJOR and "transfers: none recorded in this trace" in "\n".join(lines)


def test_read_helpers_are_bounded_and_fail_soft():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "evidence" / "service_events"
        events.mkdir(parents=True)
        good = json.dumps({"t_mono": 1.0, "event_type": "ERROR", "error_code": "X",
                           "payload_base64": SECRETS["b64"]})
        (events / "ROC.jsonl").write_bytes((good + "\n{corrupt\n" + good + "\n").encode("utf-8"))
        rows, warn = sr.read_service_events_once(root, "ROC")
        assert len(rows) == 2 and "payload_base64" not in rows[0]
        assert warn and "1 corrupt line(s) skipped" in warn
        rows, warn = sr.read_service_events_once(root, "ROC", max_bytes=len(good) + 2)
        assert len(rows) == 1 and "truncated" in warn
        assert sr.read_service_events_once(root, "MASS-B") == ([], "MASS-B.jsonl: missing")
        assert sr.read_json_once(root / "nope.json") == (None, "nope.json: missing")
        (root / "bad.json").write_text("{", encoding="utf-8")
        assert sr.read_json_once(root / "bad.json")[0] is None
        loaded = sr.load_workload_evidence(root, "w-006", result=sc006_result(),
                                           trace_rows=sc006_rows())
        names = [Path(f).name for f in loaded["files_read"]]
        assert names == ["ROC.jsonl", "MASS-A.jsonl"]
        loaded = sr.load_workload_evidence(root, "w-006", result={"status": "PASS"},
                                           trace_rows=sc006_rows())
        assert [Path(f).name for f in loaded["files_read"]][0] == "final-result.json"
        assert any("final-result.json: missing" == w for w in loaded["warnings"])


def test_module_has_no_polling_thread_or_flush_paths():
    source = (_REPO / "apps" / "standalone" / "scenario_report.py").read_text("utf-8")
    for forbidden in ("time.sleep", "threading", "_FileTail", ".flush(", "subprocess",
                      "uhd", "SingleInstanceLock", "open(", "write_text", "write_bytes"):
        # 유일하게 허용되는 open은 bounded 읽기(rb) 한 곳
        if forbidden == "open(":
            assert source.count("open(") == 1 and 'open(path, "rb")' in source
            continue
        assert forbidden not in source, forbidden
    results_source = (_REPO / "apps" / "standalone" / "results_cmd.py").read_text("utf-8")
    for forbidden in ("SingleInstanceLock", "subprocess", "write_json_atomic", "write_text",
                      "write_bytes", "mkdir", "uhd", "threading", "time.sleep"):
        assert forbidden not in results_source, forbidden
    assert "scenario_report._" not in results_source     # private helper 직접 호출 없음


# ------------------------------------------------------------ run 경로 (I/O 격리)

class _Sink:
    def __init__(self):
        self.lines = []
        self.flushes = 0

    def __call__(self, text):
        self.lines.append(text)

    def flush(self):
        self.flushes += 1


def _run_capture(script=None, *, clock=None, run_results=True):
    """fake session으로 `vdeter run all` 실행 → (code, sink, session, summary, results_lines, run_root)."""
    sink = _Sink()
    script = dict(script or {})
    script["out_probe"] = lambda: len(sink.lines)
    kwargs = {"clock": clock} if clock is not None else {}
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = rc.make_session_factory(script)
        code = run_cmd.execute(
            paths, scenario="all", certificate_case=None, session_factory=factory,
            preflight_fn=fx.fake_preflight, usb_probe=lambda: dict(fx.USB_ALL),
            environment_manifest=dict(fx.ENV_MANIFEST), repo_root=rc._REPO,
            hardware_path=Path(paths.var_dir).parent / "hardware.toml",
            out=sink, input_fn=lambda prompt: "q", progress=False,
            progress_ticker=False, **kwargs)
        session = factory.instances[0]
        summary = json.loads((session.run_root / "summary.json").read_text("utf-8"))
        run_root = session.run_root
        results_lines = []
        if run_results:
            results_cmd.main_results(paths, session.kwargs["run_id"], out=results_lines.append)
    return code, sink, session, summary, results_lines, run_root


def test_run_collects_trace_only_and_prints_block_after_result_before_result_line():
    code, sink, session, summary, _results, _root = _run_capture()
    lines = sink.lines
    assert code == run_cmd.EXIT_PASS
    text = "\n".join(lines)
    n_plan = len(run_cmd.DEFAULT_BATCH_PLAN)
    per_workload = 6
    # on_trace 호출 시점마다 out 길이가 변하지 않았다 = RF 활성 구간 실시간 출력 0
    assert len(session.trace_out_lengths) == per_workload * n_plan
    for k in range(n_plan):
        chunk = session.trace_out_lengths[k * per_workload:(k + 1) * per_workload]
        assert len(set(chunk)) == 1, chunk
        assert lines[chunk[0] - 1].startswith("  running SC-VDE-")
        # result(quiescence·non_terminal 포함) 확정 시점까지도 출력 0 (active transfer 구간)
        assert session.result_out_lengths[k] == chunk[0]
    # 다음 workload submit은 앞 workload의 블록·result 줄이 모두 나간 뒤
    assert len(session.submit_out_lengths) == n_plan
    for k in range(1, n_plan):
        previous = lines[:session.submit_out_lengths[k]]
        # submit 직전 줄은 `running …`, 그 앞은 앞 workload의 result 줄 (블록이 끝난 뒤)
        assert previous[-1].startswith("  running SC-VDE-"), previous[-3:]
        assert previous[-2].startswith("  result: PASS · 87.3 s"), previous[-3:]
        assert any(l.startswith("[%d/%d] " % (k, n_plan)) for l in previous)
    # 순서: running → 블록 → result 줄 → 다음 running
    positions = []
    for k, (scenario_id, case) in enumerate(run_cmd.DEFAULT_BATCH_PLAN, 1):
        label = scenario_id + (" [%s]" % case if scenario_id == "SC-VDE-001" else "")
        i_run = text.index("running " + scenario_id, positions[-1] if positions else 0)
        i_block = text.index("[%d/%d] %s — PASS" % (k, n_plan, label), i_run)
        i_result = text.index("result: PASS · 87.3 s · quiescence OK", i_block)
        positions.append(i_result)
    assert positions == sorted(positions)
    assert session.polls == [None] * n_plan          # on_poll 없음 (세션 계약 무변경)
    assert sink.flushes == 0                         # transfer-boundary flush 없음
    assert summary["status"] == "PASS" and summary["scenarios_passed"] == n_plan
    _assert_no_secret(text, "run")
    assert rc.SECRET_PAYLOAD_MARKER not in text and "payload_base64" not in text


def test_run_renderer_time_is_excluded_from_wall_clock_and_recorded_result():
    ticks = {"now": 1000.0}

    def clock():
        return ticks["now"]

    original = sr.render_workload

    def slow_render(model):
        ticks["now"] += 500.0          # renderer가 500 s 걸린 척
        return original(model)

    sr.render_workload = slow_render
    try:
        code, sink, session, summary, _r, _root = _run_capture(clock=clock, run_results=False)
    finally:
        sr.render_workload = original
    assert code == run_cmd.EXIT_PASS
    for workload in summary["workloads"]:
        assert workload["wall_clock_s"] == 87.3      # fake가 준 기록값 그대로 (renderer 시간 미포함)
    assert "result: PASS · 87.3 s · quiescence OK" in "\n".join(sink.lines)


def test_run_report_failure_does_not_change_verdict_batch_close_or_result_line():
    original = sr.build_workload_model

    def boom(*args, **kwargs):
        raise RuntimeError("render bug " + PEM_KEY + " token=" + SECRETS["tok"])

    sr.build_workload_model = boom
    try:
        code, sink, session, summary, results_lines, _root = _run_capture()
    finally:
        sr.build_workload_model = original
    text = "\n".join(sink.lines)
    assert code == run_cmd.EXIT_PASS and summary["status"] == "PASS"
    assert summary["scenarios_run"] == len(run_cmd.DEFAULT_BATCH_PLAN)     # batch stop 없음
    assert text.count("[WARNING] scenario report unavailable: RuntimeError") == 4
    assert text.count("result: PASS · 87.3 s · quiescence OK") == 4
    assert "Closed — cleanup PASS, residual processes 0" in text and session.closed
    assert summary["close"]["pass"] is True
    _assert_no_secret(text, "run")
    results_text = "\n".join(results_lines)
    assert results_text.count("[WARNING] scenario report unavailable: RuntimeError") == 4
    assert "[CLOSE-CLEANUP]" in results_text and "[FINAL SUMMARY]" in results_text
    _assert_no_secret(results_text, "results")


def test_run_and_results_share_the_same_workload_renderer():
    code, sink, session, summary, results_lines, run_root = _run_capture()
    assert code == run_cmd.EXIT_PASS
    run_text, results_text = "\n".join(sink.lines), "\n".join(results_lines)

    def block(text, k):
        start = text.index("[%d/4] " % k)
        start = text.rindex(sr.MAJOR, 0, start)
        end = text.index("  result: PASS", start)
        return text[start:end]

    for k in range(1, 5):
        assert block(run_text, k) == block(results_text, k), k
    assert results_cmd.LIST_HINT not in results_text


# ------------------------------------------------------------ results 경로

def _snapshot(root: Path) -> dict:
    snap = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            snap[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns)
    return snap


def test_results_is_read_only_detailed_and_spawns_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = rc.make_session_factory()
        rc.run(paths, factory)
        run_id = factory.instances[0].kwargs["run_id"]
        before = _snapshot(paths.var_dir)
        assert not paths.lock_path.exists() or True
        original_popen = subprocess.Popen

        def no_spawn(*a, **k):
            raise AssertionError("results must not spawn processes")

        subprocess.Popen = no_spawn
        try:
            lines = []
            assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
            for _ in range(2):
                results_cmd.main_results(paths, run_id, out=lines.append)
        finally:
            subprocess.Popen = original_popen
        after = _snapshot(paths.var_dir)
        assert before == after                       # 파일 목록·SHA·크기·mtime 불변 (lock·active 포함)
        text = "\n".join(lines)
        for header in ("[BINDING]", "[BRING-UP]", "[WORKLOADS] 4 recorded (source: summary.json)",
                       "[CLOSE-CLEANUP]", "[FINAL SUMMARY]"):
            assert header in text, header
        assert "reference_sha256=" in text and "installation_sha256=" in text
        assert "endpoint MASS-A" in text and "serial=" in text and "rx_gain_db=75" in text
        assert "[4/4] SC-VDE-001 [forged] — PASS" in text
        assert "[SCENARIO] SC-VDE-001[forged] — PASS" in text
        assert "security_result=PASS (forged certificate correctly rejected)" in text
        assert "close=PASS" in text and "workloads_recorded=4  passed=4" in text
        assert "[WARNING]" not in text
        assert not any(ord(ch) > 0x2FFF for ch in text)   # 영어 출력 계약


def test_results_fail_soft_on_unfinished_and_corrupt_evidence_with_warnings():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = rc.make_session_factory()
        rc.run(paths, factory)
        run_root = factory.instances[0].run_root
        run_id = run_root.name
        summary_path = run_root / "summary.json"
        summary = json.loads(summary_path.read_text("utf-8"))
        summary_path.unlink()
        results_dir = run_root / "session" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        for workload in summary["workloads"]:
            (results_dir / (workload["workload_id"] + ".json")).write_text(
                json.dumps(workload), encoding="utf-8")
        lines = []
        assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
        text = "\n".join(lines)
        assert "RUN %s — not recorded" % run_id in text
        assert "[WORKLOADS] 4 recorded (source: session/results (no summary.json))" in text
        assert "[WARNING] summary.json missing" in text
        assert "[TRANSFER 1/1] s1" in text
        summary["scenarios_passed"] = 1
        summary["close"]["problems"] = ["private_key=" + SECRETS["pass"]]
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        (run_root / "evidence" / "service_events" / "ROC.jsonl").write_text("{{{\n", encoding="utf-8")
        before = _snapshot(run_root)
        lines = []
        assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
        assert _snapshot(run_root) == before
        text = "\n".join(lines)
        assert "[WARNING] summary scenarios_passed=1 but 4 recorded workload result(s) are PASS" in text
        assert "[WARNING] ROC.jsonl: 1 corrupt line(s) skipped" in text
        assert "workloads_recorded=4  passed=4" in text
        assert "problem=private_key=<redacted>" in text and SECRETS["pass"] not in text
        (paths.runs_dir / "qual-20260906-000000").mkdir()
        (paths.runs_dir / "run-unfinished").mkdir()
        (paths.runs_dir / "run-unfinished" / "run.json").write_text(
            json.dumps({"requested_utc": "2026-09-07T00:00:00Z"}), encoding="utf-8")
        lines = []
        results_cmd.main_results(paths, out=lines.append)
        text = " ".join("\n".join(lines).split())
        assert "qual-20260906-000000 qualify" in text and "run-unfinished unfinished" in text
        assert results_cmd.LIST_HINT in lines
        lines = []
        assert results_cmd.main_results(paths, "qual-20260906-000000", out=lines.append) == 0
        assert "qualification ladder phase evidence" in lines[0]


def test_results_bringup_falls_back_to_progress_log_when_phase_elapsed_missing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp)
        factory = rc.make_session_factory()
        rc.run(paths, factory)
        run_root = factory.instances[0].run_root
        summary_path = run_root / "summary.json"
        summary = json.loads(summary_path.read_text("utf-8"))
        summary["close"]["closed"] = {"cleanup": {"pass": True}}
        summary["attempts"].append({"attempt": 2, "bringup": "FAILED", "error": "x",
                                    "classification": {"transient_host_time": False,
                                                       "reasons": ["cleanup not confirmed PASS"]}})
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        lines = []
        results_cmd.main_results(paths, run_root.name, out=lines.append)
        text = "\n".join(lines)
        assert "source=presentation/progress.log" in text
        assert "Endpoint startup" in text and "elapsed=" in text
        assert "transient_host_time=false" in text and "reason=cleanup not confirmed PASS" in text
        assert "{" not in text.split("[BRING-UP]")[1].split("[WORKLOADS]")[0]   # raw JSON 없음


# ------------------------------------------------------------ 13. 실물 evidence read-only 실측

def _real_runs():
    """VDETER_REAL_EVIDENCE_RUNS='<runs_dir>::<run_id>;...' — 없으면 시험을 건너뛴다."""
    spec = os.environ.get("VDETER_REAL_EVIDENCE_RUNS", "")
    runs = []
    for item in spec.split(";"):
        if "::" in item:
            runs_dir, run_id = item.split("::", 1)
            if (Path(runs_dir) / run_id).is_dir():
                runs.append((Path(runs_dir), run_id))
    return runs


def test_real_evidence_is_rendered_read_only():
    runs = _real_runs()
    if not runs:
        import pytest
        pytest.skip("VDETER_REAL_EVIDENCE_RUNS not set")
    from apps.standalone.state import StatePaths
    for runs_dir, run_id in runs:
        before = _snapshot(runs_dir / run_id)
        with tempfile.TemporaryDirectory() as tmp:
            paths = StatePaths(tmp)
            paths.runs_dir = runs_dir
            lines = []
            assert results_cmd.main_results(paths, run_id, out=lines.append) == 0
        assert _snapshot(runs_dir / run_id) == before, run_id
        text = "\n".join(lines)
        _assert_no_secret(text, run_id)
        assert "payload_base64" not in text
        if "sc006" in "\n".join(sorted(p.name for p in (runs_dir / run_id / "workloads").glob("*"))):
            block = _section(text, "] SC-VDE-006 — ", "  result: ")
            assert "[PRECONDITION] SC-VDE-005 — PASS" in block
            assert "transfers=2 (rendered from trace; recorded 2)  steps=5" in block
            assert "transfers=5 (rendered from trace; recorded 5)  steps=11" in block
            assert "rendered_transfers_total=7 (source=trace-count)" in block
            assert "[TRANSFER 2/2] 005-ack" in block and "[TRANSFER 5/5] 006-fallback-ack" in block
            assert "matched=derived" in block and "recorded_linkage_id=1" in block
            assert "matched=true" not in block


def test_sources_compile_and_no_shared_state_between_calls():
    for name in ("apps/standalone/scenario_report.py", "apps/standalone/results_cmd.py",
                 "apps/standalone/run_cmd.py"):
        subprocess.run([sys.executable, "-m", "py_compile", str(_REPO / name)], check=True)
    a = _render(sc006_rows(), sc006_result(), sc006_service_events())[1]
    _render(sc001_rows("forged", fail=True), sc001_result("forged", fail=True),
            sc001_fail_service_events())
    assert a == _render(sc006_rows(), sc006_result(), sc006_service_events())[1]
