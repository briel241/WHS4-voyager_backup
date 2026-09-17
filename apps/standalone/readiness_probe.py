"""Readiness Link 17 양방향 probe — 고정 vector·횟수·timeout (spec §5.2, §6.2).

canonical payload bytes·최대 payload 크기·per-transfer timeout은 **기존 SC-VDE-005 PASS
evidence에서 추출**했다(임의 새 payload 없음). 출처와 SHA는 `PROBE_VECTOR_SOURCE`에 적는다.
CLI(`run_cmd`)와 workload producer(`apps/mass_roc_workload_controller.py`)가 같은 모듈을 읽는다.

probe는 readiness 전용 내부 workload다. 일반 scenario 성공으로 보고하지 않으며 Link 19·fault
ladder·adaptive gain·raw-PHY·software bundle을 수행했다고 주장하지 않는다.
"""

from __future__ import annotations

import hashlib
import json

PROBE_POLICY_ID = "VDETER_READINESS_LINK17_PROBE_V1"
PROBE_SCENARIO_ID = "READINESS-LINK17-PROBE"
PROBE_WORKLOAD_KIND = "readiness_probe"
USER_WORKLOAD_KIND = "user"

#: 출처 evidence (독립 run 8건 이상에서 동일 바이트·동일 SHA 확인, 2026-09-17 M0 조사).
PROBE_VECTOR_SOURCE = {
    "scenario": "SC-VDE-005",
    "evidence": ("evidence/service_events/{MASS-A,ROC}.jsonl DATA_RECEIVED payload_base64 and "
                 "workloads/*sc005*/mass-roc-events/final-result.json transfers"),
    "representative_run": "r7-final-20260907-050408 (backups/r7-final-20260907)",
    "service_events_sha256": {
        "MASS-A.jsonl": "25cdcb86aa05c36f",   # prefix; full value in M0 SOURCES.sha256
        "ROC.jsonl": "86d2f58dcf36a0a9",
    },
    "note": "prefixes above are cross-reference labels only; the binding values are the payload "
            "bytes and SHA-256 recorded in PROBE_VECTOR",
}

#: 양방향 각 1 transfer. `ack_required=True`(protocol ACK 확인)는 두 방향 모두 요구한다.
PROBE_VECTOR = (
    {
        "step": "probe-roc-to-mass-a",
        "sender": "ROC", "receiver": "MASS-A",
        "payload_format": "sc_vde.route_control.command",
        "payload_hex": ("00484018f3ffffe100349db9e0282bca0190faa500a1c14256c01e05092866e8"
                        "0a197307ffff80"),
        "payload_len": 39,
        "payload_sha256": "811e1496eae1339bc5bb72dabd59b41b6bbd6640d88b3d554d0a977b750579ac",
        "ack_required": True,
    },
    {
        "step": "probe-mass-a-to-roc",
        "sender": "MASS-A", "receiver": "ROC",
        "payload_format": "sc_vde.route_control.ack",
        "payload_hex": "005000",
        "payload_len": 3,
        "payload_sha256": "2b657b4908dcb4ac9d65162cb17b02301da011245a0400441ddf51296ce7fffd",
        "ack_required": True,
    },
)

#: 고정 상한 — 기존 controller 값과 endpoint template 값 (구현 전 추출).
PROBE_LIMITS = {
    "transfers_per_direction": 1,
    "directions": 2,
    "status_timeout_s": 240,      # apps/mass_roc_workload_controller.py::main status_timeout_s
    "receive_timeout_s": 240,     # apps/mass_roc_workload_controller.py::main receive_timeout_s
    "status_poll_interval_s_x1000": 500,
    "max_payload_bytes": 65531,   # [service] max_payload_bytes (endpoint templates)
    "retry": 0,
}


def payload_bytes(entry: dict) -> bytes:
    return bytes.fromhex(entry["payload_hex"])


def verify_vector() -> list:
    """vector 자기 검증 — hex 길이·SHA가 기록값과 일치하는가. 반환: 문제 목록."""
    problems = []
    for entry in PROBE_VECTOR:
        data = payload_bytes(entry)
        if len(data) != entry["payload_len"]:
            problems.append("%s: length %d != %d" % (entry["step"], len(data), entry["payload_len"]))
        if hashlib.sha256(data).hexdigest() != entry["payload_sha256"]:
            problems.append("%s: sha256 mismatch" % entry["step"])
        if len(data) > PROBE_LIMITS["max_payload_bytes"]:
            problems.append("%s: exceeds max_payload_bytes" % entry["step"])
    return problems


def probe_plan() -> dict:
    """machine-readable probe plan (binding의 `probe_plan_sha256` 입력)."""
    return {
        "policy_id": PROBE_POLICY_ID,
        "scenario_id": PROBE_SCENARIO_ID,
        "vector": [dict(entry) for entry in PROBE_VECTOR],
        "limits": dict(PROBE_LIMITS),
        "source": {"scenario": PROBE_VECTOR_SOURCE["scenario"],
                   "representative_run": PROBE_VECTOR_SOURCE["representative_run"]},
    }


def probe_plan_sha256() -> str:
    from apps.standalone.readiness import canonical_bytes
    return hashlib.sha256(canonical_bytes(probe_plan())).hexdigest()


def probe_plan_json() -> bytes:
    return (json.dumps(probe_plan(), ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("utf-8")


def evaluate_probe_result(scenario_result: dict) -> dict:
    """producer의 probe 결과(final-result.json의 results[PROBE_SCENARIO_ID])를 판정한다.

    PASS 조건(전부): status PASS, transfers 정확히 2건, 각 transfer COMPLETED·received_valid·
    payload_sha256이 vector와 일치, wrong_destination 0. 반환 {"pass", "problems", "transfers"}."""
    problems = []
    body = (scenario_result or {}).get("result", scenario_result) or {}
    if body.get("status") != "PASS":
        problems.append("probe status %r" % body.get("status"))
    transfers = body.get("transfers") or []
    if len(transfers) != len(PROBE_VECTOR):
        problems.append("probe transfers %d != %d" % (len(transfers), len(PROBE_VECTOR)))
    by_step = {t.get("step"): t for t in transfers}
    for entry in PROBE_VECTOR:
        transfer = by_step.get(entry["step"])
        if transfer is None:
            problems.append("%s: transfer missing" % entry["step"])
            continue
        if transfer.get("final_transfer_state") != "COMPLETED":
            problems.append("%s: state %r" % (entry["step"], transfer.get("final_transfer_state")))
        if transfer.get("payload_sha256") != entry["payload_sha256"]:
            problems.append("%s: submitted payload sha differs from the vector" % entry["step"])
        received = transfer.get("received") or {}
        if not received.get("valid"):
            problems.append("%s: receive digest/format/addressing check failed" % entry["step"])
        if transfer.get("wrong_destination"):
            problems.append("%s: wrong destination observed" % entry["step"])
    if (body.get("counters") or {}).get("wrong_destination"):
        problems.append("wrong_destination counter > 0")
    return {"pass": not problems, "problems": problems, "transfers": transfers}
