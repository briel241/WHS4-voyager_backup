"""application-only 실패 분류기 `VDETER_READINESS_FAILURE_CLASSIFIER_V1` (spec §6.3.1).

producer가 낸 **구조화 enum/필드만** 읽는다. 콘솔 문구·exception 문자열·번역된 message는
읽지 않는다(그 필드들은 입력에서 아예 참조하지 않는다).

allowlist(신규 규범 code 2개)만 application-only가 될 수 있고, 그것도 transfer terminal 전부 확정
·timing/tracking/device/ownership/cleanup health PASS가 별도 구조화 필드로 확인될 때뿐이다.
denylist prefix/exact가 하나라도 있으면 operational이 우선한다. 알 수 없는 code, code 부재, 복합
실패의 구조화 필드 누락은 UNKNOWN(신규 승격 fail-closed; 기존 active는 자동 inhibit하지 않음).
"""

from __future__ import annotations

CLASSIFIER_POLICY_ID = "VDETER_READINESS_FAILURE_CLASSIFIER_V1"

APPLICATION_ONLY_CODES = frozenset({
    "SCENARIO_ASSERTION_FAILED",
    "EXPECTED_APPLICATION_OUTCOME_NOT_OBSERVED",
})
OPERATIONAL_PREFIXES = (
    "TRANSFER_", "ACK_", "DATA_RECEIVED_", "DECODE_", "TIMEOUT_", "TIMING_", "TRACKING_",
    "TERMINAL_", "OWNERSHIP_", "DEVICE_", "CLEANUP_", "TOPOLOGY_", "PROCESS_", "USB_",
)
OPERATIONAL_EXACT = frozenset({"UNEXPECTED_TX", "SESSION_INHIBIT"})

#: coordinator workload abort `error.reason` enum → 구조화 code (문자열 detail은 읽지 않는다).
COORDINATOR_ABORT_CODES = {
    "workload_timeout": "TIMEOUT_WORKLOAD",
    "session_fault": "DEVICE_SESSION_FAULT",
    "session_shutdown": "TERMINAL_SHUTDOWN_DURING_WORKLOAD",
    "session_guard_or_event_failure": "TRACKING_SESSION_GUARD",
}

#: inhibit를 유발하는 operational code prefix — §9.1: timing/closure/decode 필수 경로 실패, tracking
#: loss, 예상 밖 송신, terminal/소유권 불확실, device error, cleanup·잔존·재발견 실패.
#: TIMEOUT_(외부/호스트 timeout 그 자체)는 승격을 막지만 그 자체로는 inhibit 사유가 아니다.
INHIBIT_PREFIXES = ("TIMING_", "DECODE_", "TRACKING_", "TERMINAL_", "OWNERSHIP_", "DEVICE_",
                    "CLEANUP_", "PROCESS_", "TOPOLOGY_", "USB_")
INHIBIT_EXACT = frozenset({"UNEXPECTED_TX", "SESSION_INHIBIT"})


def is_operational_code(code) -> bool:
    return isinstance(code, str) and (code in OPERATIONAL_EXACT
                                      or any(code.startswith(p) for p in OPERATIONAL_PREFIXES))


def is_application_code(code) -> bool:
    return isinstance(code, str) and code in APPLICATION_ONLY_CODES


def is_inhibit_code(code) -> bool:
    return isinstance(code, str) and (code in INHIBIT_EXACT
                                      or any(code.startswith(p) for p in INHIBIT_PREFIXES))


def _quiescence_codes(quiescence) -> list:
    """coordinator quiescence 구조화 필드 → operational code."""
    codes = []
    if not isinstance(quiescence, dict):
        return ["TERMINAL_QUIESCENCE_UNRECORDED"]
    if quiescence.get("pass") is True:
        return []
    if quiescence.get("child_terminated") is False:
        codes.append("PROCESS_WORKLOAD_CHILD_ALIVE")
    if (quiescence.get("non_terminal_transfers") or 0) > 0:
        codes.append("TERMINAL_TRANSFERS_NON_TERMINAL")
    if quiescence.get("inhibited") is True:
        codes.append("SESSION_INHIBIT")
    workers = quiescence.get("workers_running")
    if workers is not None and workers != 3:
        codes.append("PROCESS_WORKER_COUNT")
    launchers = quiescence.get("launchers_alive")
    expected = quiescence.get("expected_launchers")
    if launchers is not None and expected is not None and launchers != expected:
        codes.append("PROCESS_LAUNCHER_COUNT")
    if quiescence.get("abort_reason") in COORDINATOR_ABORT_CODES:
        codes.append(COORDINATOR_ABORT_CODES[quiescence["abort_reason"]])
    if not codes:
        # quiescence.pass False인데 개별 원인 필드가 없다 — 구조화 근거 없이 PASS로 볼 수 없다.
        codes.append("TERMINAL_QUIESCENCE_FAILED")
    return codes


def classify_workload(result: dict) -> dict:
    """coordinator workload result record 하나를 분류한다.

    입력 필드(전부 구조화): result.status, result.quiescence, result.error.reason(enum),
    result.scenario.results[*].readiness_classification({codes, transfers{all_terminal, failed,
    wrong_destination, missing_data_received}}), result.poc_severity / result.poc.severity.

    반환 {"policy_id", "verdict": PASS|APPLICATION_ONLY|OPERATIONAL|UNKNOWN, "codes",
          "operational_codes", "application_codes", "unknown_codes", "reasons", "inhibit_codes"}."""
    codes = []
    reasons = []
    health_ok = True
    result = result or {}
    quiescence = result.get("quiescence")
    q_codes = _quiescence_codes(quiescence)
    if q_codes:
        health_ok = False
        codes.extend(q_codes)
    error = result.get("error")
    if isinstance(error, dict) and error.get("reason") in COORDINATOR_ABORT_CODES:
        code = COORDINATOR_ABORT_CODES[error["reason"]]
        if code not in codes:
            codes.append(code)
        health_ok = False
    severity = result.get("poc_severity") or ((result.get("poc") or {}).get("severity"))
    if severity == "SESSION_INHIBIT":
        codes.append("SESSION_INHIBIT")
        health_ok = False
    scenario = result.get("scenario") or {}
    scenario_results = scenario.get("results") or {}
    producer_seen = False
    transfers_ok = True
    for _name, payload in sorted(scenario_results.items()):
        for body in _bodies(payload):
            classification = body.get("readiness_classification")
            if not isinstance(classification, dict):
                if body.get("status") != "PASS":
                    reasons.append("producer emitted no readiness_classification for a "
                                   "non-PASS scenario body")
                    codes.append("UNKNOWN_NO_CLASSIFICATION")
                continue
            producer_seen = True
            if classification.get("policy_id") != CLASSIFIER_POLICY_ID:
                codes.append("UNKNOWN_CLASSIFIER_POLICY")
                reasons.append("producer classification policy %r"
                               % classification.get("policy_id"))
            for code in classification.get("codes") or ():
                codes.append(code if isinstance(code, str) else "UNKNOWN_NON_STRING_CODE")
            transfers = classification.get("transfers")
            if not isinstance(transfers, dict):
                transfers_ok = False
                reasons.append("producer classification lacks the transfers health block")
                codes.append("UNKNOWN_TRANSFER_HEALTH_MISSING")
            else:
                if transfers.get("all_terminal") is not True:
                    transfers_ok = False
                    codes.append("TERMINAL_TRANSFERS_NON_TERMINAL")
                if (transfers.get("failed") or 0) > 0:
                    transfers_ok = False
                    codes.append("TRANSFER_FAILED")
                if (transfers.get("wrong_destination") or 0) > 0:
                    transfers_ok = False
                    codes.append("DATA_RECEIVED_WRONG_DESTINATION")
                if (transfers.get("missing_data_received") or 0) > 0:
                    transfers_ok = False
                    codes.append("DATA_RECEIVED_MISSING")
    status = result.get("status")
    operational = sorted({c for c in codes if is_operational_code(c)})
    application = sorted({c for c in codes if is_application_code(c)})
    unknown = sorted({c for c in codes
                      if not is_operational_code(c) and not is_application_code(c)})
    inhibit_codes = sorted({c for c in operational if is_inhibit_code(c)})
    if status == "PASS" and health_ok and not codes:
        verdict = "PASS"
    elif operational:
        verdict = "OPERATIONAL"
    elif unknown or not producer_seen:
        verdict = "UNKNOWN"
        if not producer_seen and not unknown:
            reasons.append("no producer classification and status is not PASS")
    elif application and health_ok and transfers_ok:
        verdict = "APPLICATION_ONLY"
    else:
        verdict = "UNKNOWN"
        reasons.append("application-only codes without a confirmed health/terminal block")
    return {"policy_id": CLASSIFIER_POLICY_ID, "verdict": verdict,
            "codes": sorted(set(codes)), "operational_codes": operational,
            "application_codes": application, "unknown_codes": unknown,
            "inhibit_codes": inhibit_codes, "reasons": reasons,
            "health_pass": health_ok and transfers_ok}


def _bodies(payload):
    """scenario result payload → 판정 body 목록 (006은 precondition+result 두 body)."""
    if not isinstance(payload, dict):
        return []
    if "result" in payload and isinstance(payload.get("result"), dict):
        bodies = [payload["result"]]
        pre = payload.get("precondition")
        if isinstance(pre, dict):
            bodies.append(pre)
        return bodies
    return [payload]


def classify_close(summary: dict, topology_after=None) -> dict:
    """session close/finalize 구조화 결과 → operational code (§6.3 cleanup·소유권 판정)."""
    codes = []
    summary = summary or {}
    closed = summary.get("closed") or {}
    cleanup = closed.get("cleanup") or {}
    if summary.get("coordinator_exited") is False:
        codes.append("OWNERSHIP_COORDINATOR_EXIT_UNCONFIRMED")
    if summary.get("escalation"):
        codes.append("OWNERSHIP_ESCALATION_USED")
    if not closed:
        codes.append("CLEANUP_UNCONFIRMED")
    else:
        if closed.get("status") != "PASS" and closed.get("status") is not None:
            codes.append("CLEANUP_SESSION_STATUS_%s" % closed.get("status"))
        if cleanup and cleanup.get("pass") is not True:
            if cleanup.get("launcher_clean_exit") is False:
                codes.append("PROCESS_LAUNCHER_FORCED")
            if cleanup.get("workers_after_stop") not in (0, None):
                codes.append("PROCESS_WORKERS_AFTER_STOP")
            rediscovery = cleanup.get("rediscovery") or {}
            if rediscovery and not all(rediscovery.values()):
                codes.append("TOPOLOGY_REDISCOVERY_FAIL")
            if cleanup.get("throttled") not in (None, "throttled=0x0"):
                codes.append("DEVICE_THROTTLED")
            if not any(c.startswith(("PROCESS_", "TOPOLOGY_", "DEVICE_")) for c in codes):
                codes.append("CLEANUP_FAIL")
        if closed.get("inhibited") is True:
            codes.append("SESSION_INHIBIT")
    residual = summary.get("residual_processes")
    if residual is None:
        codes.append("PROCESS_RESIDUAL_UNVERIFIED")
    elif residual:
        codes.append("PROCESS_RESIDUAL")
    if topology_after is not None and topology_after.get("pass") is not True:
        codes.append("TOPOLOGY_AFTER_CLOSE_FAIL")
    codes = sorted(set(codes))
    return {"policy_id": CLASSIFIER_POLICY_ID, "pass": summary.get("pass") is True and not codes,
            "codes": codes, "inhibit_codes": [c for c in codes if is_inhibit_code(c)]}


def classify_bringup(phase_marks: dict, failed: bool, *, ready: bool) -> dict:
    """bring-up 실패 위치(구조화 phase mark) → code. host-time 실패는 TIMEOUT_(승격 불가, inhibit 아님),
    RF-time/bridge 실패는 TIMING_(inhibit 사유)."""
    if ready and not failed:
        return {"codes": [], "inhibit_codes": []}
    marks = set((phase_marks or {}).keys())
    if "rf_time_post" in marks:
        code = "TIMING_SERVICE_ADMISSION_FAIL"
    elif "active_bridge" in marks:
        code = "TIMING_RF_TIME_POST_FAIL"
    elif "rf_time_pre" in marks:
        code = "TIMING_ACTIVE_BRIDGE_FAIL"
    elif "acquire_host_time" in marks or "readiness_host_time" in marks:
        code = "TIMING_RF_TIME_PRE_FAIL"
    elif "calibration_sealed" in marks or "calibration_quiescent" in marks:
        code = "TIMEOUT_READINESS_HOST_TIME"
    elif "calibrate_rf" in marks:
        code = "DEVICE_CALIBRATION_QUIESCENCE_FAIL"
    elif "calibration_host_time" in marks:
        code = "DECODE_GAIN_CALIBRATION_FAIL"
    elif "start_cfo" in marks:
        code = "TIMEOUT_CALIBRATION_HOST_TIME"
    elif "prepare_configure_arm" in marks:
        code = "TIMEOUT_HOST_TIME"
    else:
        code = "DEVICE_PREPARE_CONFIGURE_ARM_FAIL"
    return {"codes": [code], "inhibit_codes": [code] if is_inhibit_code(code) else []}
