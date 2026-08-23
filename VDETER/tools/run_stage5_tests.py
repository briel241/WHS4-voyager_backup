"""Run TC-STAGE5-001..020 and emit deterministic Stage 5 software evidence.

`stage5_service_integration_baseline.md` §11·§12·§14의 evidence 요구를 따른다.

- Stage 5 module들을 per-test-case 기록과 함께 실행한다.
- Stage 0~4 전체 회귀를 별도 process의 pytest로 완주시키고 junit/stdout artifact를
  보존한다. 짧은 timeout으로 중단하지 않는다.
- §6.6 golden vector artifact의 SHA-256을 결과에 기록한다.
- B210 hardware test는 Stage 5 범위 밖이므로 실행하지 않는다 (`tests/hardware` 제외).
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
BASELINE_ID = "VDETER-STAGE5-SERVICE-INTEGRATION-BL-001"
BASELINE_VERSION = "0.1.4"
BASELINE_STATUS = "VERIFIED/FROZEN"

#: 승인된 필수 시험 순서 (`stage5` 지시): unit → contract → codec/vector →
#: lifecycle/admission/event → 3-endpoint 통합 → TC 전체.
MODULES = (
    "tests.unit.test_stage5_projection",
    "tests.unit.test_stage5_events",
    "tests.contract.test_stage5_contract",
    "tests.unit.test_stage5_registry_dispatch",
    "tests.unit.test_stage5_codec_x509",
    "tests.unit.test_stage5_codec_ais",
    "tests.unit.test_stage5_codec_opaque_chart",
    "tests.integration.test_stage5_lifecycle",
    "tests.unit.test_stage5_submit",
    "tests.unit.test_stage5_status_retention",
    "tests.unit.test_stage5_cancel",
    "tests.integration.test_stage5_tbb_dispatch",
    "tests.integration.test_stage5_short_data",
    "tests.integration.test_stage5_data_session",
    "tests.integration.test_stage5_broadcast",
    "tests.integration.test_stage5_ack_retry",
    "tests.integration.test_stage5_fragmentation",
    "tests.integration.test_stage5_end_to_end_bytes",
    "tests.integration.test_stage5_rx_failures",
    "tests.contract.test_stage5_regression_audit",
)
CASE_PATTERN = re.compile(r"TC_STAGE5_(\d{3})")
CASES = tuple(f"TC-STAGE5-{number:03d}" for number in range(1, 21))

VECTOR_ROOT = os.path.join(VDETER_ROOT, "tests", "vectors", "presentation")
VECTOR_FILES = {
    "GV-STAGE5-PRES-OPAQUE-001": (
        "gv_stage5_pres_opaque_001.application.bin",
        "gv_stage5_pres_opaque_001.wire.bin",
    ),
    "GV-STAGE5-PRES-X509-001": (
        "gv_stage5_pres_x509_001.application.bin",
        "gv_stage5_pres_x509_001.wire.bin",
    ),
    "GV-STAGE5-PRES-AIS1-001": (
        "gv_stage5_pres_ais1_001.application.bin",
        "gv_stage5_pres_ais1_001.wire.bin",
    ),
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
        self.records.append(
            {
                "test_id": test.id(),
                "test_case": f"TC-STAGE5-{match.group(1)}" if match else "UNMAPPED",
                "outcome": outcome,
                "reason": reason[:2000],
                "started_at": self.started_at.pop(test.id(), None),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )

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

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._append(test, "FAIL", self._exc_info_to_string(err, test))


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _module_path(module: str) -> str:
    return os.path.join(VDETER_ROOT, *module.split(".")) + ".py"


def _git_identity() -> dict:
    def run(*args):
        completed = subprocess.run(
            ("git",) + args,
            cwd=VDETER_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "tree_dirty": bool(run("status", "--porcelain")),
    }


def _stage04_regression(output_dir: str) -> dict:
    """현재 tree의 Stage 0~4 전체 회귀를 별도 process로 완주시킨다."""

    artifact_dir = os.path.join(output_dir, "artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    junit_path = os.path.join(artifact_dir, "stage04-regression-junit.xml")
    log_path = os.path.join(artifact_dir, "stage04-regression.txt")
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
    started_at = datetime.now(timezone.utc).isoformat()
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
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if os.path.exists(junit_path):
        root = ET.parse(junit_path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.attrib.get(key, "0"))
    return {
        "outcome": "PASS" if completed.returncode == 0 else "FAIL",
        "return_code": completed.returncode,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": command,
        "tests_run": totals["tests"],
        "failures": totals["failures"],
        "errors": totals["errors"],
        # 기존 hardware skip은 실패가 아니다. B210 test는 실행하지 않는다.
        "skipped": totals["skipped"],
        "artifacts": {
            "stdout": os.path.relpath(log_path, output_dir).replace(os.sep, "/"),
            "stdout_sha256": _sha256(log_path),
            "junit": os.path.relpath(junit_path, output_dir).replace(os.sep, "/"),
            "junit_sha256": _sha256(junit_path) if os.path.exists(junit_path) else None,
        },
    }


def _checkpoint_artifacts(output_dir: str) -> dict:
    """TC-STAGE5-018이 보존한 §8.5 correlation checkpoint artifact 목록."""

    directory = os.path.join(output_dir, "artifacts", "stage5-checkpoints")
    items = {}
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            items[name] = {"bytes": os.path.getsize(path), "sha256": _sha256(path)}
    return items


def _baseline_document_sha256() -> str:
    return _sha256(
        os.path.join(
            PROJECT_ROOT, "docs", "core", "stage5_service_integration_baseline.md"
        )
    )


def _source_manifest() -> dict:
    """evidence를 정확한 tree 내용과 1:1로 결속하는 source manifest hash.

    commit 전 dirty tree에서도 코드↔결과 연결을 검증할 수 있게 한다. audit은 같은
    방식으로 재계산해 비교한다.
    """

    paths = []
    for relative_root in ("src", "tests", "configs", "tools"):
        for root, _dirs, files in os.walk(os.path.join(VDETER_ROOT, relative_root)):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith((".py", ".toml", ".json", ".bin")):
                    paths.append(os.path.join(root, name))
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = os.path.relpath(path, VDETER_ROOT).replace(os.sep, "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\x00")
    return {"files": len(paths), "sha256": digest.hexdigest()}


def _payload_registry_identity() -> dict:
    """§7.2: registry fingerprint와 content version을 endpoint evidence에 기록한다."""

    from vdeter.presentation.registry import build_payload_format_registry
    from vdeter.profiles.models import RegistryScope
    from vdeter.profiles.payload_formats import STAGE5_PAYLOAD_CONTENT_VERSION

    identity = {
        "content_version": STAGE5_PAYLOAD_CONTENT_VERSION,
        # 세 endpoint는 시험 fixture에서 같은 frozen registry snapshot을 공유한다.
        "endpoints": ["stage5-ship-a", "stage5-ship-b", "stage5-roc"],
        "scopes": {},
    }
    for scope in (RegistryScope.OPERATIONAL, RegistryScope.TEST):
        registry = build_payload_format_registry(
            scope, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.freeze()
        fingerprint = registry.fingerprint()
        identity["scopes"][scope.value] = {
            "fingerprint": fingerprint.fingerprint,
            "entry_count": fingerprint.entry_count,
        }
    return identity


def _golden_vector_digests() -> dict:
    digests = {}
    for vector_id, (application_name, wire_name) in VECTOR_FILES.items():
        digests[vector_id] = {
            "application_sha256": _sha256(os.path.join(VECTOR_ROOT, application_name)),
            "wire_sha256": _sha256(os.path.join(VECTOR_ROOT, wire_name)),
        }
    return digests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id",
        default=f"stage5-service-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
    )
    parser.add_argument(
        "--skip-regression",
        action="store_true",
        help="개발 중간 점검용. 최종 evidence에는 사용하지 않는다.",
    )
    args = parser.parse_args()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    output_dir = os.path.join(RESULTS_ROOT, args.run_id)
    os.makedirs(output_dir, exist_ok=False)
    output = os.path.join(output_dir, "result.json")
    # 장기 실행이 중단돼도 PASS로 오인되지 않는 RUNNING sentinel을 먼저 남긴다.
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {
                "baseline_id": BASELINE_ID,
                "baseline_version": BASELINE_VERSION,
                "run_id": args.run_id,
                "stage5_completion": {"gate": "RUNNING"},
            },
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    # 진행 중인 run의 RUNNING sentinel을 audit이 완료 evidence로 오인하지 않도록
    # 현재 run 식별자를 알린다. audit은 이 run에 대해서만 sentinel 모양을 허용한다.
    os.environ["VDETER_STAGE5_ACTIVE_RUN"] = args.run_id
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)

    if args.skip_regression:
        stage04 = {"outcome": "SKIPPED_BY_FLAG"}
    else:
        stage04 = _stage04_regression(output_dir)
    os.environ.pop("VDETER_STAGE5_ACTIVE_RUN", None)

    by_case = []
    case_outcomes = {}
    for case in CASES:
        records = [item for item in result.records if item["test_case"] == case]
        outcome = (
            "PASS"
            if records and all(item["outcome"] == "PASS" for item in records)
            else "FAIL"
        )
        case_outcomes[case] = outcome
        by_case.append(
            {
                "test_case": case,
                "outcome": outcome,
                "tests": [item["test_id"] for item in records],
                "failures": [item for item in records if item["outcome"] != "PASS"],
            }
        )

    totals = {
        "unittest_success": result.wasSuccessful(),
        "unittest_failures": len(result.failures),
        "unittest_errors": len(result.errors),
        "tests_run": result.testsRun,
        "failed": sum(1 for item in result.records if item["outcome"] == "FAIL"),
        "errors": sum(1 for item in result.records if item["outcome"] == "ERROR"),
        # 새 Stage 5 software test는 skip을 허용하지 않는다.
        "skipped": sum(1 for item in result.records if item["outcome"] == "SKIP"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    gate_pass = (
        totals["unittest_success"]
        and totals["skipped"] == 0
        and all(outcome == "PASS" for outcome in case_outcomes.values())
        and stage04.get("outcome") == "PASS"
    )
    payload = {
        "baseline_id": BASELINE_ID,
        "baseline_version": BASELINE_VERSION,
        "baseline_status": BASELINE_STATUS,
        "implementation_status": "VERIFIED/FROZEN",
        "run_id": args.run_id,
        "command": [sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "git": _git_identity(),
        "conformance_scope": {
            "software_radio_three_endpoint": "IMPLEMENTED",
            "b210_full_stack": "STAGE_7",
            "link_id_17_19_phy": "STAGE_6",
            "official_bbsc_rf_compliance": "NOT_CLAIMED",
            "electronic_chart": "UNSUPPORTED_BY_DR-STAGE5-022",
        },
        "stage5_completion": {
            "gate": "PASS" if gate_pass else "INCOMPLETE",
            "freeze_decision": "APPROVED_2026-08-23",
        },
        "golden_vectors": _golden_vector_digests(),
        "payload_format_registry": _payload_registry_identity(),
        "baseline_document_sha256": _baseline_document_sha256(),
        "source_manifest": _source_manifest(),
        "checkpoint_artifacts": _checkpoint_artifacts(output_dir),
        "stage04_regression": stage04,
        "totals": totals,
        "case_summaries": by_case,
        "test_cases": [
            {"test_case": case, "outcome": case_outcomes[case]} for case in CASES
        ],
        "records": result.records,
    }
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"stage5 evidence: {output}")
    print(
        "gate:",
        payload["stage5_completion"]["gate"],
        "| stage04:",
        stage04.get("outcome"),
        "| totals:",
        totals,
    )
    return 0 if gate_pass or args.skip_regression else 1


if __name__ == "__main__":
    raise SystemExit(main())
