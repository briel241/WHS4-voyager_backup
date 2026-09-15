"""Run TC-MAC-001..018 and emit deterministic Stage 4 software evidence.

B210 full-stack and radiated-RF conformance remain Stage 7 / NOT_VERIFIED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
for path in (VDETER_ROOT, SRC_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
MODULES = (
    "tests.contract.test_mac_contracts",
    "tests.unit.test_mac_tdma",
    "tests.unit.test_mac_tbb",
    "tests.unit.test_mac_access",
    "tests.unit.test_mac_resources",
    "tests.unit.test_mac_session",
    "tests.unit.test_mac_short_data",
    "tests.unit.test_mac_routing",
    "tests.integration.test_mac_three_endpoint",
    "tests.unit.test_mac_runtime",
    "tests.unit.test_mac_time_profile",
    "tests.integration.test_mac_vertical_path",
    "tests.contract.test_mac_stage03_regression",
)
CASE_PATTERN = re.compile(r"TC_MAC_(\d{3})")
CASES = tuple(f"TC-MAC-{number:03d}" for number in range(1, 19))
CONFORMANCE_SCOPE = {
    "software_radio_three_endpoint": "VERIFIED",
    "link_id_11_vertical_path": "VERIFIED",
    "b210_full_stack": "STAGE_7",
    "radiated_rf_compliance": "NOT_VERIFIED",
    "link_id_17_19_phy": "STAGE_6",
}
KNOWN_COMPLETION_GAPS = ()
REQUIRED_CASE_METHOD_COUNTS = {
    "TC-MAC-001": 4, "TC-MAC-002": 2, "TC-MAC-003": 6,
    "TC-MAC-004": 4, "TC-MAC-005": 6, "TC-MAC-006": 1,
    "TC-MAC-007": 1, "TC-MAC-008": 5, "TC-MAC-009": 2,
    "TC-MAC-010": 3, "TC-MAC-011": 6, "TC-MAC-012": 5,
    "TC-MAC-013": 4, "TC-MAC-014": 2, "TC-MAC-015": 13,
    "TC-MAC-016": 2, "TC-MAC-017": 1, "TC-MAC-018": 1,
}


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started_at = {}

    def startTest(self, test):
        self.started_at[test.id()] = datetime.now(timezone.utc).isoformat()
        super().startTest(test)

    def _append(self, test, outcome, reason=""):
        match = CASE_PATTERN.search(test.id())
        self.records.append({
            "test_id": test.id(),
            "test_case": f"TC-MAC-{match.group(1)}" if match else "UNMAPPED",
            "outcome": outcome,
            "reason": reason,
            "started_at": self.started_at.pop(test.id(), None),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._append(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._append(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._append(test, "ERROR", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._append(test, "SKIP", reason)


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _module_path(module: str) -> str:
    return os.path.join(VDETER_ROOT, *module.split(".")) + ".py"


def _stage03_regression(output_dir: str) -> dict:
    """Run the current-tree Stage 0-3/non-MAC regression in a fresh process."""

    artifact_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    junit_path = os.path.join(artifact_dir, "stage03-regression-junit.xml")
    log_path = os.path.join(artifact_dir, "stage03-regression.txt")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/contract",
        "tests/unit",
        "tests/integration",
        "-q",
        f"--junitxml={junit_path}",
    ]
    for module in MODULES:
        command.append(f"--ignore={_module_path(module)}")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=VDETER_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    with open(log_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(completed.stdout)
    categories = {"stage0": 0, "stage1": 0, "stage2": 0, "stage3": 0, "other": 0}
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if os.path.exists(junit_path):
        root = ET.parse(junit_path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.attrib.get(key, "0"))
        for case in root.iter("testcase"):
            identity = f"{case.attrib.get('classname', '')}.{case.attrib.get('name', '')}"
            if "TC_ARCH_" in identity:
                categories["stage0"] += 1
            elif "TC_LINK_" in identity:
                categories["stage1"] += 1
            elif "TC_PHY11_" in identity:
                categories["stage2"] += 1
            elif "TC_RADIO_" in identity:
                categories["stage3"] += 1
            else:
                categories["other"] += 1
    return {
        "outcome": "PASS" if completed.returncode == 0 else "FAIL",
        "return_code": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": command,
        "method_categories": categories,
        "junit_totals": totals,
        "artifacts": {
            "stdout": os.path.relpath(log_path, output_dir).replace(os.sep, "/"),
            "stdout_sha256": _sha256(log_path),
            "junit": os.path.relpath(junit_path, output_dir).replace(os.sep, "/"),
            "junit_sha256": _sha256(junit_path) if os.path.exists(junit_path) else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"stage4-mac-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    args = parser.parse_args()
    started = time.monotonic()
    output_dir = os.path.join(RESULTS_ROOT, args.run_id)
    os.makedirs(output_dir, exist_ok=False)
    # traceability의 evidence 경로 존재 검사는 회귀 suite 자체에 포함된다. 장기
    # 실행이 중단돼도 PASS로 오인되지 않는 명시적 RUNNING sentinel을 먼저 쓰고,
    # 모든 audit이 끝난 뒤 아래에서 최종 payload로 원자적 의미를 덮어쓴다.
    output = os.path.join(output_dir, "result.json")
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "baseline_id": "VDETER-STAGE4-MAC-BL-001",
                "baseline_version": "0.3.13",
                "run_id": args.run_id,
                "stage4_completion": {"freeze_gate": "RUNNING"},
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    stage03 = _stage03_regression(output_dir)
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    by_case = {}
    for case in CASES:
        records = [item for item in result.records if item["test_case"] == case]
        by_case[case] = {
            "outcome": "PASS" if records and all(item["outcome"] == "PASS" for item in records) else "FAIL",
            "tests": [item["test_id"] for item in records],
            "failures": [item for item in records if item["outcome"] != "PASS"],
        }
    coverage_complete = all(
        len(by_case[case]["tests"]) >= minimum
        and by_case[case]["outcome"] == "PASS"
        for case, minimum in REQUIRED_CASE_METHOD_COUNTS.items()
    )
    by_case["TC-MAC-018"]["tests"].append("stage03-current-tree-pytest-regression")
    if stage03["outcome"] != "PASS":
        by_case["TC-MAC-018"]["outcome"] = "FAIL"
        by_case["TC-MAC-018"]["failures"].append(stage03)

    source_paths = []
    for relative_root in ("src", "tests", "configs"):
        absolute_root = os.path.join(VDETER_ROOT, relative_root)
        for root, _dirs, files in os.walk(absolute_root):
            for name in files:
                if name.endswith((".py", ".toml", ".json")):
                    source_paths.append(os.path.join(root, name))
    source_paths.extend((
        os.path.join(VDETER_ROOT, "pyproject.toml"),
        os.path.join(VDETER_ROOT, "tools", "run_stage4_mac_tests.py"),
        os.path.join(PROJECT_ROOT, "docs", "core", "stage4_mac_baseline.md"),
        os.path.join(PROJECT_ROOT, "docs", "core", "traceability.toml"),
        os.path.join(PROJECT_ROOT, "docs", "vdeter_protocol_implementation_spec.md"),
    ))
    source_paths = sorted(set(source_paths))
    freeze_pass = (
        not KNOWN_COMPLETION_GAPS
        and result.wasSuccessful()
        and coverage_complete
        and stage03["outcome"] == "PASS"
        and all(item["outcome"] == "PASS" for item in by_case.values())
    )
    payload = {
        "baseline_id": "VDETER-STAGE4-MAC-BL-001",
        "baseline_version": "0.3.13",
        "baseline_status": "BASELINE_PENDING",
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "conformance_scope": CONFORMANCE_SCOPE,
        "stage4_completion": {
            "freeze_gate": (
                "PASS" if freeze_pass else "INCOMPLETE"
            ),
            "baseline_case_coverage_manifest_complete": coverage_complete,
            "required_case_method_counts": REQUIRED_CASE_METHOD_COUNTS,
            "known_gaps": KNOWN_COMPLETION_GAPS,
        },
        "totals": {
            "tests_run": result.testsRun,
            "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
            "failed": len(result.failures) + len(result.errors),
            "skipped": len(result.skipped),
            "unittest_success": result.wasSuccessful(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
        "test_cases": by_case,
        "stage03_regression": stage03,
        "records": result.records,
        "input_sha256": {
            os.path.relpath(path, PROJECT_ROOT).replace(os.sep, "/"): _sha256(path)
            for path in source_paths
        },
    }
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(output)
    return 0 if freeze_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
