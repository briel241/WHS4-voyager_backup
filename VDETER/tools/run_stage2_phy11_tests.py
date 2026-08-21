"""Run Stage 2 Link ID 11 verification and preserve formal evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone


VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if VDETER_ROOT not in sys.path:
    sys.path.insert(0, VDETER_ROOT)
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from vdeter.profiles.link_phy import (  # noqa: E402
    PHY11_REGISTRY_FINGERPRINT,
    build_phy_profile_registry,
)
from vdeter.profiles.models import RegistryScope  # noqa: E402


PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
TRACEABILITY_PATH = os.path.join(PROJECT_ROOT, "docs", "core", "traceability.toml")
BASELINE_PATH = os.path.join(PROJECT_ROOT, "docs", "core", "stage2_phy_link11_baseline.md")
BASELINE_ID = "VDETER-STAGE2-PHY11-BL-001"
PHY_PATTERN = re.compile(r"TC_PHY11_(\d{3}(?:_\d{3})*)")
ARCH_PATTERN = re.compile(r"TC_ARCH_(\d{3}(?:_\d{3})*)")
LINK_PATTERN = re.compile(r"TC_LINK_(\d{3}(?:_\d{3})*)")
PLANNED_PHY_CASES = tuple(f"TC-PHY11-{index:03d}" for index in range(1, 17))
PLANNED_ARCH_CASES = tuple(f"TC-ARCH-{index:03d}" for index in range(1, 28))
PLANNED_LINK_CASES = tuple(f"TC-LINK-{index:03d}" for index in range(1, 35))


def load_toml(path: str):
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def baseline_scope() -> dict:
    for record in load_toml(TRACEABILITY_PATH)["baselines"]:
        if record["id"] == BASELINE_ID:
            return record
    raise RuntimeError(f"missing baseline {BASELINE_ID}")


def case_ids(test_id: str, pattern: re.Pattern[str], prefix: str) -> tuple[str, ...]:
    match = pattern.search(test_id.rsplit(".", 1)[-1])
    if match is None:
        return ()
    return tuple(f"{prefix}-{part}" for part in match.group(1).split("_"))


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict] = {}

    def record(self, test, outcome: str, reason: str = "") -> None:
        previous = self.records.get(test.id())
        if previous is not None and previous["outcome"] == "FAIL" and outcome != "FAIL":
            return
        self.records[test.id()] = {
            "test_id": test.id(),
            "outcome": outcome,
            "reason": reason,
            "phy_test_cases": case_ids(test.id(), PHY_PATTERN, "TC-PHY11"),
            "arch_test_cases": case_ids(test.id(), ARCH_PATTERN, "TC-ARCH"),
            "link_test_cases": case_ids(test.id(), LINK_PATTERN, "TC-LINK"),
            "docstring": test.shortDescription() or "",
        }

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "FAIL", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "SKIP", reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(test, "FAIL", self._exc_info_to_string(err, subtest))


def aggregate(records: dict[str, dict], planned: tuple[str, ...], key: str) -> list[dict]:
    output = {
        case: {"test_case": case, "outcome": "SKIP", "tests": [], "failures": []}
        for case in planned
    }
    for record in records.values():
        for case in record[key]:
            if case not in output:
                continue
            item = output[case]
            item["tests"].append(record["test_id"])
            if record["outcome"] == "FAIL":
                item["outcome"] = "FAIL"
                item["failures"].append({"test_id": record["test_id"], "reason": record["reason"]})
            elif record["outcome"] == "SKIP" and item["outcome"] != "FAIL":
                item["outcome"] = "SKIP"
            elif item["outcome"] not in ("FAIL", "SKIP") or not item["tests"][:-1]:
                item["outcome"] = "PASS"
            elif record["outcome"] == "PASS" and item["outcome"] == "SKIP":
                item["outcome"] = "PASS"
    for item in output.values():
        item["tests"] = sorted(set(item["tests"]))
        if not item["tests"]:
            item["outcome"] = "SKIP"
        item["expected"] = "all assertions pass"
        item["actual"] = {
            "PASS": "all assertions passed",
            "FAIL": "one or more assertions failed",
            "SKIP": "missing or skipped test",
        }[item["outcome"]]
    return list(output.values())


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: str, suffixes: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    paths = []
    for directory, _, names in os.walk(root):
        for name in names:
            if name.endswith(suffixes):
                paths.append(os.path.join(directory, name))
    for path in sorted(paths):
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        digest.update(relative.encode("utf-8") + b"\0")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def dependency_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def write_json(path: str, value: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def counts(cases: list[dict]) -> dict[str, int]:
    return {
        "passed": sum(case["outcome"] == "PASS" for case in cases),
        "failed": sum(case["outcome"] == "FAIL" for case in cases),
        "skipped": sum(case["outcome"] == "SKIP" for case in cases),
    }


def run(run_id: str) -> int:
    if dependency_version("cryptography") == "not_installed":
        print(
            "[preflight] Stage 2 formal verification includes the complete Stage 1 "
            "regression and requires `uv sync --frozen --extra crypto`.",
            file=sys.stderr,
        )
        return 2
    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts_directory = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts_directory, exist_ok=True)
    generator = subprocess.run(
        [sys.executable, os.path.join(VDETER_ROOT, "tools", "generate_phy11_vectors.py")],
        cwd=VDETER_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    with open(os.path.join(artifacts_directory, "vector_generator_output.txt"), "w", encoding="utf-8") as handle:
        handle.write(generator.stdout)
        handle.write(generator.stderr)
    if generator.returncode != 0:
        print(generator.stdout)
        print(generator.stderr, file=sys.stderr)
        return generator.returncode

    baseline = baseline_scope()
    registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
    fingerprint = registry.freeze().value
    if fingerprint.fingerprint != PHY11_REGISTRY_FINGERPRINT:
        raise RuntimeError("production registry fingerprint constant mismatch")

    performance_path = os.path.join(run_directory, "performance.json")
    impairment_path = os.path.join(run_directory, "impairment_results.json")
    previous_output = os.environ.get("VDETER_STAGE2_PERFORMANCE_OUTPUT")
    previous_impairment_output = os.environ.get("VDETER_STAGE2_IMPAIRMENT_OUTPUT")
    os.environ["VDETER_STAGE2_PERFORMANCE_OUTPUT"] = performance_path
    os.environ["VDETER_STAGE2_IMPAIRMENT_OUTPUT"] = impairment_path
    started_at = datetime.now(timezone.utc)
    stream = io.StringIO()
    suite = unittest.TestLoader().discover(
        start_dir=os.path.join(VDETER_ROOT, "tests"), top_level_dir=VDETER_ROOT
    )
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    ).run(suite)
    finished_at = datetime.now(timezone.utc)
    if previous_output is None:
        os.environ.pop("VDETER_STAGE2_PERFORMANCE_OUTPUT", None)
    else:
        os.environ["VDETER_STAGE2_PERFORMANCE_OUTPUT"] = previous_output
    if previous_impairment_output is None:
        os.environ.pop("VDETER_STAGE2_IMPAIRMENT_OUTPUT", None)
    else:
        os.environ["VDETER_STAGE2_IMPAIRMENT_OUTPUT"] = previous_impairment_output

    phy_cases = aggregate(result.records, PLANNED_PHY_CASES, "phy_test_cases")
    arch_cases = aggregate(result.records, PLANNED_ARCH_CASES, "arch_test_cases")
    link_cases = aggregate(result.records, PLANNED_LINK_CASES, "link_test_cases")
    phy_counts = counts(phy_cases)
    arch_counts = counts(arch_cases)
    link_counts = counts(link_cases)
    complete = (
        result.wasSuccessful()
        and not result.skipped
        and phy_counts == {"passed": 16, "failed": 0, "skipped": 0}
        and arch_counts == {"passed": 27, "failed": 0, "skipped": 0}
        and link_counts == {"passed": 34, "failed": 0, "skipped": 0}
        and os.path.isfile(performance_path)
        and os.path.isfile(impairment_path)
    )
    snapshot = {
        "baseline_document_sha256": file_digest(BASELINE_PATH),
        "traceability_sha256": file_digest(TRACEABILITY_PATH),
        "source_tree_sha256": tree_digest(SRC_ROOT, (".py",)),
        "test_tree_sha256": tree_digest(os.path.join(VDETER_ROOT, "tests"), (".py", ".json", ".cf32le")),
        "vector_manifest_sha256": file_digest(os.path.join(VDETER_ROOT, "tests", "vectors", "phy", "link11", "manifest_v1.json")),
        "generator_sha256": file_digest(os.path.join(VDETER_ROOT, "tools", "generate_phy11_vectors.py")),
    }
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": BASELINE_ID,
        "baseline_version": baseline["version"],
        "baseline_status": baseline["status"],
        "stage": "stage2-phy11",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "dependencies": {
                "cryptography": dependency_version("cryptography"),
                "numpy": dependency_version("numpy"),
                "gnuradio": "not_required",
                "uhd": "not_required",
            },
        },
        "configuration": {
            "registry": {
                "name": fingerprint.name,
                "scope": fingerprint.scope.value,
                "schema_version": fingerprint.schema_version,
                "content_version": fingerprint.content_version,
                "entry_count": fingerprint.entry_count,
                "fingerprint": fingerprint.fingerprint,
            }
        },
        "snapshot": snapshot,
        "totals": {
            "tests_run": result.testsRun,
            "unittest_success": complete,
            "unittest_framework_success": result.wasSuccessful(),
            "unittest_failures": len(result.failures),
            "unittest_errors": len(result.errors),
            "unittest_skips": len(result.skipped),
            "test_cases_planned": 16,
            **phy_counts,
        },
        "stage0_regression": {**arch_counts, "test_cases": arch_cases},
        "stage1_regression": {**link_counts, "test_cases": link_cases},
        "test_cases": phy_cases,
        "tests": sorted(result.records.values(), key=lambda item: item["test_id"]),
        "performance_artifact": "performance.json",
        "impairment_artifact": "impairment_results.json",
    }
    candidate_result = os.path.join(run_directory, "candidate-result.json")
    candidate_logs = os.path.join(run_directory, "candidate-logs.jsonl")
    candidate_output = os.path.join(artifacts_directory, "candidate-unittest-output.txt")
    candidate_manifest = os.path.join(artifacts_directory, "candidate-verification-manifest.json")
    write_json(candidate_result, payload)
    write_json(candidate_manifest, snapshot)
    with open(candidate_output, "w", encoding="utf-8") as handle:
        handle.write(stream.getvalue())
    with open(candidate_logs, "w", encoding="utf-8") as handle:
        for case in phy_cases:
            handle.write(json.dumps({
                "timestamp": finished_at.isoformat(),
                "layer": "PHY",
                "event_code": f"TEST_{case['outcome']}",
                "test_case": case["test_case"],
                "outcome": case["outcome"],
            }, sort_keys=True) + "\n")

    if complete:
        os.replace(candidate_result, os.path.join(run_directory, "result.json"))
        os.replace(candidate_logs, os.path.join(run_directory, "logs.jsonl"))
        os.replace(candidate_output, os.path.join(artifacts_directory, "unittest_output.txt"))
        os.replace(candidate_manifest, os.path.join(artifacts_directory, "verification_manifest.json"))
    else:
        os.replace(candidate_result, os.path.join(run_directory, "failed-result.json"))
        os.replace(candidate_logs, os.path.join(run_directory, "failed-logs.jsonl"))
        os.replace(candidate_output, os.path.join(artifacts_directory, "failed-unittest-output.txt"))
        os.replace(candidate_manifest, os.path.join(artifacts_directory, "failed-verification-manifest.json"))
    print(f"[run] {run_id}: PHY {phy_counts}; ARCH {arch_counts}; LINK {link_counts}")
    print(f"[evidence] {os.path.relpath(run_directory, VDETER_ROOT).replace(os.sep, '/')}")
    return 0 if complete else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 Link ID 11 verification runner")
    parser.add_argument(
        "--run-id",
        default=datetime.now(timezone.utc).strftime("stage2-phy11-%Y%m%dT%H%M%SZ"),
    )
    arguments = parser.parse_args(argv)
    return run(arguments.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
