"""Compose and independently audit the final Stage 3 Radio evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone


VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
TRACEABILITY_PATH = os.path.join(WORKSPACE_ROOT, "docs", "core", "traceability.toml")
BASELINE_ID = "VDETER-STAGE3-RADIO-BL-001"
BASELINE_VERSION = "0.2.3"
REQUIRED_CASES = tuple(f"TC-RADIO-{value:03d}" for value in range(1, 16))
REQUIRED_REQUIREMENTS = tuple(f"RADIO-REQ-{value:02d}" for value in range(1, 12))
REQUIRED_DECISIONS = tuple(f"DR-STAGE3-RADIO-{value:03d}" for value in range(1, 34))


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path):
    return os.path.relpath(path, VDETER_ROOT).replace(os.sep, "/")


def _artifact(path):
    return {
        "path": _relative(path),
        "sha256": _sha256(path),
        "size_bytes": os.path.getsize(path),
    }


def _unique(items, label):
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate {label}")


def _traceability_audit():
    with open(TRACEABILITY_PATH, "rb") as handle:
        traceability = tomllib.load(handle)
    requirement_ids = [value["id"] for value in traceability["requirements"]]
    test_case_ids = [value["id"] for value in traceability["test_cases"]]
    _unique(requirement_ids, "requirement ID")
    _unique(test_case_ids, "test case ID")
    radio_requirements = [
        value for value in traceability["requirements"]
        if value["id"].startswith("RADIO-REQ-")
    ]
    if tuple(value["id"] for value in radio_requirements) != REQUIRED_REQUIREMENTS:
        raise ValueError("Stage 3 requirement IDs are incomplete or out of order")
    decisions = []
    requirement_rows = []
    for value in radio_requirements:
        if value["status"] not in {"IMPLEMENTED", "VERIFIED"}:
            raise ValueError(f"{value['id']} is not implemented")
        for field in (
            "normative_refs", "decision_records", "implementation",
            "test_cases", "evidence",
        ):
            if not value.get(field):
                raise ValueError(f"{value['id']} has no {field}")
        if any(test_id not in test_case_ids for test_id in value["test_cases"]):
            raise ValueError(f"{value['id']} references an unknown test")
        decisions.extend(value["decision_records"])
        requirement_rows.append({
            "requirement_id": value["id"],
            "status": value["status"],
            "normative_refs": value["normative_refs"],
            "decision_records": value["decision_records"],
            "implementation": value["implementation"],
            "test_cases": value["test_cases"],
            "traceability_evidence": value["evidence"],
        })
    if tuple(sorted(set(decisions))) != tuple(sorted(REQUIRED_DECISIONS)):
        raise ValueError("DR-STAGE3-RADIO-001..033 coverage is incomplete")
    if any(value not in test_case_ids for value in REQUIRED_CASES):
        raise ValueError("TC-RADIO-001..015 registry is incomplete")
    return {
        "traceability": _artifact(TRACEABILITY_PATH),
        "requirement_ids": list(REQUIRED_REQUIREMENTS),
        "decision_ids": list(REQUIRED_DECISIONS),
        "test_case_ids": list(REQUIRED_CASES),
        "requirements": requirement_rows,
    }


def _case_map(payload):
    values = payload.get("test_cases", [])
    ids = [value["test_case"] for value in values]
    _unique(ids, "result test case ID")
    return {value["test_case"]: value for value in values}


def _validate_inputs(software, hardware):
    for label, value in (("software", software), ("hardware", hardware)):
        if value.get("baseline_id") != BASELINE_ID:
            raise ValueError(f"{label} baseline ID mismatch")
        if value.get("baseline_version") != BASELINE_VERSION:
            raise ValueError(f"{label} baseline version mismatch")
    software_cases = _case_map(software)
    hardware_cases = _case_map(hardware)
    for case_id in (*REQUIRED_CASES[:11], "TC-RADIO-014"):
        if software_cases.get(case_id, {}).get("outcome") != "PASS":
            raise ValueError(f"{case_id} software evidence is not PASS")
    for case_id in ("TC-RADIO-012", "TC-RADIO-013"):
        if hardware_cases.get(case_id, {}).get("outcome") != "PASS":
            raise ValueError(f"{case_id} hardware evidence is not PASS")
    gate = hardware.get("hardware_gate", {})
    if not gate.get("tc_radio_012_pass") or not gate.get("tc_radio_013_pass"):
        raise ValueError("hardware gate is not PASS")
    if gate.get("actual_b210_count_verified") != 2:
        raise ValueError("exactly two physical B210 devices were not verified")
    if software.get("regression", {}).get("outcome") != "PASS":
        raise ValueError("Stage 0..2 regression evidence is not PASS")
    for stage in ("stage0", "stage1", "stage2"):
        regression = software["regression"].get(stage, {})
        if regression.get("failed") != 0 or regression.get("skipped") != 0:
            raise ValueError(f"{stage} regression contains failure or skip")
    return software_cases, hardware_cases


def run(run_id, software_path, hardware_path):
    started_ns = time.time_ns()
    software_path = os.path.abspath(software_path)
    hardware_path = os.path.abspath(hardware_path)
    software = _read_json(software_path)
    hardware = _read_json(hardware_path)
    software_cases, hardware_cases = _validate_inputs(software, hardware)
    traceability = _traceability_audit()

    merged = []
    for case_id in REQUIRED_CASES[:11]:
        merged.append(software_cases[case_id])
    merged.extend((hardware_cases["TC-RADIO-012"], hardware_cases["TC-RADIO-013"]))
    merged.append(software_cases["TC-RADIO-014"])
    merged.append({
        "test_case": "TC-RADIO-015",
        "outcome": "PASS",
        "tests": [
            "tests.contract.test_stage3_radio_audit."
            "Stage3RadioAuditTest."
            "test_TC_RADIO_015_all_cases_have_nonblocked_pass_evidence"
        ],
        "audit": "requirement-decision-test-result-evidence",
    })

    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": BASELINE_ID,
        "baseline_version": BASELINE_VERSION,
        "stage": "stage3-radio",
        "overall_outcome": "PASS",
        "started_at": datetime.fromtimestamp(
            started_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "test_cases": merged,
        "totals": {
            "test_cases": 15,
            "passed": 15,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        },
        "hardware_gate": hardware["hardware_gate"],
        "environment": {
            "software": software.get("environment"),
            "hardware": hardware.get("environment"),
        },
        "configuration_evidence": {
            "schema_version": 2,
            "source_radio_fingerprint": hardware["fixture"]["source_radio_fingerprint"],
            "receiver_radio_fingerprint": hardware["fixture"]["receiver_radio_fingerprint"],
            "fixture_id": hardware["fixture"]["fixture_id"],
        },
        "fixture": hardware["fixture"],
        "regression": software["regression"],
        "conformance_scope": software["conformance_scope"],
        "traceability_audit": traceability,
        "artifacts": {
            "software_result": _artifact(software_path),
            "hardware_result": _artifact(hardware_path),
            "hardware": hardware.get("artifacts", {}),
        },
    }

    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    result_path = os.path.join(run_directory, "result.json")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    environment = dict(os.environ)
    environment["VDETER_STAGE3_FINAL_AUDIT"] = "1"
    environment["VDETER_STAGE3_FINAL_EVIDENCE"] = result_path
    completed = subprocess.run(
        [
            sys.executable, "-m", "unittest", "-v",
            "tests.contract.test_stage3_radio_audit",
        ],
        cwd=VDETER_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    audit_log = completed.stdout + completed.stderr
    audit_log_path = os.path.join(artifacts, "final_audit_unittest.txt")
    with open(audit_log_path, "w", encoding="utf-8") as handle:
        handle.write(audit_log)
    if completed.returncode != 0:
        payload["overall_outcome"] = "FAIL"
        payload["totals"]["passed"] = 14
        payload["totals"]["failed"] = 1
        payload["test_cases"][-1]["outcome"] = "FAIL"
        payload["test_cases"][-1]["reason"] = audit_log[-4000:]
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return 1
    payload["artifacts"]["final_audit"] = _artifact(audit_log_path)
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        "run_id": run_id,
        "overall_outcome": "PASS",
        "result": _relative(result_path),
    }, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--software-result", required=True)
    parser.add_argument("--hardware-result", required=True)
    arguments = parser.parse_args()
    return run(arguments.run_id, arguments.software_result, arguments.hardware_result)


if __name__ == "__main__":
    raise SystemExit(main())
