"""`VDETER_READINESS_FAILURE_CLASSIFIER_V1` 시험 (spec §6.3.1).

증명하는 것: allowlist 2 code만 application-only, denylist prefix/exact 우선, 복합 실패 = operational,
unknown/code 부재/구조화 필드 누락 = UNKNOWN(승격 fail-closed), 콘솔·exception 문자열 비의존,
coordinator abort enum·quiescence 구조화 필드·PoC severity 매핑, close/bring-up 분류와 inhibit 부분집합.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import readiness_classifier as rc  # noqa: E402

GOOD_Q = {"pass": True, "non_terminal_transfers": 0, "workers_running": 3, "inhibited": False,
          "child_terminated": True, "launchers_alive": 3, "expected_launchers": 3}


def scenario(codes, *, status="FAIL", transfers=None, policy=rc.CLASSIFIER_POLICY_ID,
             with_classification=True, name="SC-VDE-006"):
    body = {"status": status, "reason": "free text — TRANSFER_FAILED DEVICE_ERROR (must be ignored)"}
    if with_classification:
        body["readiness_classification"] = {
            "policy_id": policy, "codes": list(codes),
            "transfers": {"all_terminal": True, "failed": 0, "wrong_destination": 0,
                          "missing_data_received": 0} if transfers is None else transfers}
    return {"results": {name: body}}


def test_pass_when_status_pass_and_health_pass():
    verdict = rc.classify_workload({"status": "PASS", "quiescence": GOOD_Q,
                                    "scenario": scenario([], status="PASS")})
    assert verdict["verdict"] == "PASS" and verdict["codes"] == []


def test_application_only_requires_allowlist_and_health():
    for code in sorted(rc.APPLICATION_ONLY_CODES):
        verdict = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                        "scenario": scenario([code])})
        assert verdict["verdict"] == "APPLICATION_ONLY", code
        assert verdict["inhibit_codes"] == []


def test_denylist_prefixes_and_exact_are_operational_and_win_over_allowlist():
    for code in ("TRANSFER_FAILED", "ACK_MISMATCH", "DATA_RECEIVED_MISSING", "DECODE_FAIL",
                 "TIMEOUT_STATUS", "TIMING_LOSS", "TRACKING_LOSS", "TERMINAL_UNCONFIRMED",
                 "OWNERSHIP_UNCERTAIN", "DEVICE_ERROR", "CLEANUP_FAIL", "TOPOLOGY_CHANGED",
                 "PROCESS_RESIDUAL", "USB_SPEED", "UNEXPECTED_TX", "SESSION_INHIBIT"):
        verdict = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                        "scenario": scenario(["SCENARIO_ASSERTION_FAILED", code])})
        assert verdict["verdict"] == "OPERATIONAL", code
        assert code in verdict["operational_codes"]
        assert "SCENARIO_ASSERTION_FAILED" in verdict["application_codes"]


def test_unknown_code_or_missing_classification_is_unknown():
    unknown = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                    "scenario": scenario(["SOMETHING_ELSE"])})
    assert unknown["verdict"] == "UNKNOWN" and unknown["unknown_codes"] == ["SOMETHING_ELSE"]
    absent = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                   "scenario": scenario([], with_classification=False)})
    assert absent["verdict"] == "UNKNOWN"
    no_scenario = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q})
    assert no_scenario["verdict"] == "UNKNOWN"
    wrong_policy = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                         "scenario": scenario(["SCENARIO_ASSERTION_FAILED"],
                                                              policy="OTHER_V0")})
    assert wrong_policy["verdict"] == "UNKNOWN"
    missing_health = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                           "scenario": {"results": {"SC-VDE-006": {
                                               "status": "FAIL", "readiness_classification": {
                                                   "policy_id": rc.CLASSIFIER_POLICY_ID,
                                                   "codes": ["SCENARIO_ASSERTION_FAILED"]}}}}})
    assert missing_health["verdict"] == "UNKNOWN"


def test_transfer_health_block_gates_application_only():
    for field, code in (("all_terminal", "TERMINAL_TRANSFERS_NON_TERMINAL"),
                        ("failed", "TRANSFER_FAILED"),
                        ("wrong_destination", "DATA_RECEIVED_WRONG_DESTINATION"),
                        ("missing_data_received", "DATA_RECEIVED_MISSING")):
        transfers = {"all_terminal": True, "failed": 0, "wrong_destination": 0,
                     "missing_data_received": 0}
        transfers[field] = False if field == "all_terminal" else 1
        verdict = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                        "scenario": scenario(["SCENARIO_ASSERTION_FAILED"],
                                                             transfers=transfers)})
        assert verdict["verdict"] == "OPERATIONAL" and code in verdict["codes"], field


def test_quiescence_and_coordinator_abort_are_structured_only():
    q = dict(GOOD_Q, pass_=False)
    q = {**GOOD_Q, "pass": False, "non_terminal_transfers": 2}
    verdict = rc.classify_workload({"status": "FAIL", "quiescence": q,
                                    "scenario": scenario([], status="PASS")})
    assert verdict["verdict"] == "OPERATIONAL"
    assert "TERMINAL_TRANSFERS_NON_TERMINAL" in verdict["inhibit_codes"]
    abort = rc.classify_workload({"status": "FAIL",
                                  "quiescence": {"pass": False, "abort_reason": "workload_timeout",
                                                 "child_terminated": True},
                                  "error": {"reason": "workload_timeout", "detail": "상한 900s"}})
    assert abort["verdict"] == "OPERATIONAL" and "TIMEOUT_WORKLOAD" in abort["codes"]
    assert "TIMEOUT_WORKLOAD" not in abort["inhibit_codes"]    # timeout 자체는 inhibit 아님
    fault = rc.classify_workload({"status": "FAIL", "quiescence": {"pass": False, "child_terminated": True,
                                                                   "abort_reason": "session_fault"},
                                  "error": {"reason": "session_fault"}})
    assert "DEVICE_SESSION_FAULT" in fault["inhibit_codes"]
    inhibited = rc.classify_workload({"status": "PASS", "quiescence": GOOD_Q,
                                      "scenario": scenario([], status="PASS"),
                                      "poc_severity": "SESSION_INHIBIT"})
    assert inhibited["verdict"] == "OPERATIONAL" and "SESSION_INHIBIT" in inhibited["inhibit_codes"]


def test_console_and_exception_strings_are_never_parsed():
    result = {"status": "FAIL", "quiescence": GOOD_Q,
              "error": "TRANSFER_FAILED DEVICE_ERROR CLEANUP_FAIL",   # 문자열 error는 무시
              "console": "[coordinator] host affine fit FAIL",
              "scenario": scenario(["SCENARIO_ASSERTION_FAILED"])}
    verdict = rc.classify_workload(result)
    assert verdict["verdict"] == "APPLICATION_ONLY"
    assert verdict["operational_codes"] == []


def test_precondition_body_is_classified_too():
    payload = {"result": {"status": "PASS"},
               "precondition": {"status": "FAIL", "readiness_classification": {
                   "policy_id": rc.CLASSIFIER_POLICY_ID, "codes": ["TRANSFER_SUBMIT_REJECTED"],
                   "transfers": {"all_terminal": True, "failed": 1, "wrong_destination": 0,
                                 "missing_data_received": 0}}}}
    verdict = rc.classify_workload({"status": "FAIL", "quiescence": GOOD_Q,
                                    "scenario": {"results": {"SC-VDE-006": payload}}})
    assert verdict["verdict"] == "OPERATIONAL" and "TRANSFER_SUBMIT_REJECTED" in verdict["codes"]


def test_close_classification_maps_structured_cleanup_fields():
    ok = rc.classify_close({"pass": True, "coordinator_exited": True, "escalation": [],
                            "closed": {"status": "PASS", "cleanup": {"pass": True}},
                            "residual_processes": []}, {"pass": True})
    assert ok["pass"] and ok["codes"] == []
    bad = rc.classify_close({"pass": False, "coordinator_exited": False, "escalation": ["term-group"],
                             "closed": {"status": "FAIL", "cleanup": {
                                 "pass": False, "launcher_clean_exit": False,
                                 "workers_after_stop": 2,
                                 "rediscovery": {"34D03DD": True, "3515425": False},
                                 "throttled": "throttled=0x50000"}, "inhibited": True},
                             "residual_processes": ["1234 endpoint_launcher"]}, {"pass": False})
    assert bad["pass"] is False
    for code in ("OWNERSHIP_COORDINATOR_EXIT_UNCONFIRMED", "OWNERSHIP_ESCALATION_USED",
                 "PROCESS_LAUNCHER_FORCED", "PROCESS_WORKERS_AFTER_STOP",
                 "TOPOLOGY_REDISCOVERY_FAIL", "DEVICE_THROTTLED", "SESSION_INHIBIT",
                 "PROCESS_RESIDUAL", "TOPOLOGY_AFTER_CLOSE_FAIL"):
        assert code in bad["codes"], code
        assert code in bad["inhibit_codes"], code
    unverified = rc.classify_close({"pass": False, "coordinator_exited": True, "escalation": [],
                                    "closed": None, "residual_processes": None})
    assert {"CLEANUP_UNCONFIRMED", "PROCESS_RESIDUAL_UNVERIFIED"} <= set(unverified["codes"])


def test_bringup_classification_by_phase_mark():
    assert rc.classify_bringup({}, True, ready=False)["codes"] == ["DEVICE_PREPARE_CONFIGURE_ARM_FAIL"]
    host = rc.classify_bringup({"prepare_configure_arm": 14.0}, True, ready=False)
    assert host["codes"] == ["TIMEOUT_HOST_TIME"] and host["inhibit_codes"] == []
    pre = rc.classify_bringup({"prepare_configure_arm": 14.0, "acquire_host_time": 400.0}, True,
                              ready=False)
    assert pre["codes"] == ["TIMING_RF_TIME_PRE_FAIL"] and pre["inhibit_codes"] == pre["codes"]
    post = rc.classify_bringup({"acquire_host_time": 1, "rf_time_pre": 2, "active_bridge": 3}, True,
                               ready=False)
    assert post["codes"] == ["TIMING_RF_TIME_POST_FAIL"]
    assert rc.classify_bringup({"rf_time_post": 9}, False, ready=True)["codes"] == []
