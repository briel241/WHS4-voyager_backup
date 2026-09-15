"""Stage 1 Link verification runner and evidence generator.

The runner executes the combined Stage 0/Stage 1 suite, records every
``TC-LINK-001..034`` outcome, and preserves the Stage 0 regression outcome in
the same evidence bundle.  A successful run produces::

    results/runs/<run-id>/
    |-- result.json
    |-- logs.jsonl
    `-- artifacts/
        |-- unittest_output.txt
        `-- verification_manifest.json

Final evidence files are replaced only after a completely successful run.
Failed attempts are retained under ``failed-*`` names and cannot invalidate a
previous successful evidence bundle with the same run ID.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import sys
import unittest
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if VDETER_ROOT not in sys.path:
    sys.path.insert(0, VDETER_ROOT)
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from vdeter.common.events import EventSeverity  # noqa: E402
from vdeter.common.logging import JsonlLogger, LogContext  # noqa: E402
from vdeter.common.errors import Details, Layer  # noqa: E402
from vdeter.link.messages.builtins import build_message_registry  # noqa: E402
from vdeter.profiles.link_phy import (  # noqa: E402
    build_link_profile_registry,
    build_message_profile_registry,
)
from vdeter.profiles.models import RegistryScope  # noqa: E402
from tools.run_stage0_tests import _iso, _now  # noqa: E402

RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
TRACEABILITY_PATH = os.path.join(PROJECT_ROOT, "docs", "core", "traceability.toml")
BASELINE_PATH = os.path.join(PROJECT_ROOT, "docs", "core", "stage1_link_baseline.md")
STAGE1_BASELINE_ID = "VDETER-STAGE1-LINK-BL-001"
LINK_CASE_PATTERN = re.compile(r"TC_LINK_(\d{3}(?:_\d{3})*)")
ARCH_CASE_PATTERN = re.compile(r"TC_ARCH_(\d{3})")
PLANNED_LINK_CASES = tuple(f"TC-LINK-{index:03d}" for index in range(1, 35))
PLANNED_ARCH_CASES = tuple(f"TC-ARCH-{index:03d}" for index in range(1, 28))
DEFAULT_SEED = 20260819


def _load_toml(path: str):
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _baseline_scope() -> dict:
    for baseline in _load_toml(TRACEABILITY_PATH)["baselines"]:
        if baseline["id"] == STAGE1_BASELINE_ID:
            return baseline
    raise RuntimeError(f"missing baseline scope: {STAGE1_BASELINE_ID}")


def _case_ids(test_id: str, pattern: re.Pattern[str], prefix: str) -> tuple[str, ...]:
    method_name = test_id.rsplit(".", 1)[-1]
    match = pattern.search(method_name)
    if match is None:
        return ()
    return tuple(f"{prefix}-{part}" for part in match.group(1).split("_"))


class RecordingResult(unittest.TextTestResult):
    """Record exact tests and all Link case IDs named by compound test methods."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict] = {}

    def _record(self, test, outcome: str, reason: str = "") -> None:
        previous = self.records.get(test.id())
        if previous is not None and previous["outcome"] == "FAIL" and outcome != "FAIL":
            return
        if previous is not None and previous["outcome"] == "FAIL" and reason:
            reason = previous["reason"] + "\n" + reason
        self.records[test.id()] = {
            "test_id": test.id(),
            "outcome": outcome,
            "reason": reason,
            "link_test_cases": _case_ids(test.id(), LINK_CASE_PATTERN, "TC-LINK"),
            "arch_test_cases": _case_ids(test.id(), ARCH_CASE_PATTERN, "TC-ARCH"),
            "docstring": test.shortDescription() or "",
        }

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "FAIL", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "SKIP", reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._record(test, "FAIL", self._exc_info_to_string(err, subtest))


def _dependency_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def _project_version() -> str:
    return _load_toml(os.path.join(VDETER_ROOT, "pyproject.toml"))["project"]["version"]


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_digest(root: str, suffixes: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    paths = []
    for directory, _, names in os.walk(root):
        for name in names:
            if name.endswith(suffixes):
                paths.append(os.path.join(directory, name))
    for path in sorted(paths):
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot() -> dict:
    vector_root = os.path.join(VDETER_ROOT, "tests", "vectors")
    vectors = []
    for directory, _, names in os.walk(vector_root):
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            vectors.append(
                {
                    "path": os.path.relpath(path, VDETER_ROOT).replace(os.sep, "/"),
                    "source": payload.get("source", "UNSPECIFIED"),
                    "sha256": _file_digest(path),
                }
            )
    return {
        "project_version": _project_version(),
        "source_tree_sha256": _tree_digest(SRC_ROOT, (".py",)),
        "test_tree_sha256": _tree_digest(os.path.join(VDETER_ROOT, "tests"), (".py", ".json")),
        "baseline_document_sha256": _file_digest(BASELINE_PATH),
        "traceability_sha256": _file_digest(TRACEABILITY_PATH),
        "vectors": sorted(vectors, key=lambda item: item["path"]),
    }


def _fingerprint_payload(fingerprint) -> dict:
    return {
        "name": fingerprint.name,
        "scope": fingerprint.scope.value,
        "schema_version": fingerprint.schema_version,
        "content_version": fingerprint.content_version,
        "entry_count": fingerprint.entry_count,
        "fingerprint": fingerprint.fingerprint,
    }


def _registry_summary() -> tuple[dict, tuple]:
    builders = (
        ("message_codec", build_message_registry),
        ("message_profile", build_message_profile_registry),
        ("link_profile", build_link_profile_registry),
    )
    fingerprints = []
    registries = {}
    for name, builder in builders:
        built = builder(RegistryScope.OPERATIONAL, "stage1-link-0.5.3")
        if built.is_err:
            raise RuntimeError(f"{name} build failed: {built.error.code}")
        frozen = built.value.freeze()
        if frozen.is_err:
            raise RuntimeError(f"{name} freeze failed: {frozen.error.code}")
        fingerprints.append(frozen.value)
        registries[name] = _fingerprint_payload(frozen.value)
    log_fingerprints = tuple(
        (fingerprint.name, fingerprint.fingerprint) for fingerprint in fingerprints
    )
    return {"registries": registries}, log_fingerprints


def _environment() -> dict:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "dependencies": {
            "cryptography": _dependency_version("cryptography"),
            "cffi": _dependency_version("cffi"),
            "gnuradio": "not_required",
            "uhd": "not_required",
            "numpy": _dependency_version("numpy"),
            "tomli": _dependency_version("tomli"),
        },
    }


def _aggregate(records: dict[str, dict], planned: tuple[str, ...], key: str) -> list[dict]:
    per_case = {
        case: {"test_case": case, "outcome": "PASS", "tests": [], "failures": []}
        for case in planned
    }
    seen = set()
    for record in records.values():
        for case in record[key]:
            if case not in per_case:
                continue
            seen.add(case)
            entry = per_case[case]
            entry["tests"].append(record["test_id"])
            if record["outcome"] == "FAIL":
                entry["outcome"] = "FAIL"
                entry["failures"].append(
                    {"test_id": record["test_id"], "reason": record["reason"]}
                )
            elif record["outcome"] == "SKIP" and entry["outcome"] != "FAIL":
                entry["outcome"] = "SKIP"
    for case, entry in per_case.items():
        if case not in seen:
            entry["outcome"] = "SKIP"
        entry["tests"] = sorted(set(entry["tests"]))
        entry["expected"] = "all assertions pass"
        if entry["outcome"] == "PASS":
            entry["actual"] = "all assertions passed"
        elif not entry["tests"]:
            entry["actual"] = "no test recorded"
        elif entry["outcome"] == "SKIP":
            entry["actual"] = "one or more tests skipped"
        else:
            entry["actual"] = "one or more assertions failed"
    return list(per_case.values())


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def run(run_id: str, seed: int = DEFAULT_SEED) -> int:
    if _dependency_version("cryptography") == "not_installed":
        print(
            "[preflight] Stage 1 formal verification requires the locked crypto extra; "
            "run `uv sync --frozen --extra crypto`.",
            file=sys.stderr,
        )
        return 2
    started_at = _now()
    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts, exist_ok=True)

    suite = unittest.TestLoader().discover(
        start_dir=os.path.join(VDETER_ROOT, "tests"), top_level_dir=VDETER_ROOT
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    ).run(suite)
    finished_at = _now()

    link_cases = _aggregate(result.records, PLANNED_LINK_CASES, "link_test_cases")
    arch_cases = _aggregate(result.records, PLANNED_ARCH_CASES, "arch_test_cases")
    link_passed = sum(case["outcome"] == "PASS" for case in link_cases)
    link_failed = sum(case["outcome"] == "FAIL" for case in link_cases)
    link_skipped = sum(case["outcome"] == "SKIP" for case in link_cases)
    arch_passed = sum(case["outcome"] == "PASS" for case in arch_cases)
    arch_failed = sum(case["outcome"] == "FAIL" for case in arch_cases)
    arch_skipped = sum(case["outcome"] == "SKIP" for case in arch_cases)
    complete_success = (
        result.wasSuccessful()
        and not result.skipped
        and link_failed == 0
        and link_skipped == 0
        and arch_failed == 0
        and arch_skipped == 0
    )

    baseline = _baseline_scope()
    configuration, fingerprints = _registry_summary()
    snapshot = _snapshot()
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": STAGE1_BASELINE_ID,
        "baseline_version": baseline["version"],
        "stage": "stage1-link",
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "clock_domain": started_at.clock_domain.value,
        "clock_id": started_at.clock_id,
        "seed": seed,
        "environment": _environment(),
        "configuration": configuration,
        "snapshot": snapshot,
        "totals": {
            "tests_run": result.testsRun,
            "unittest_success": complete_success,
            "unittest_framework_success": result.wasSuccessful(),
            "unittest_failures": len(result.failures),
            "unittest_errors": len(result.errors),
            "unittest_skips": len(result.skipped),
            "test_cases_planned": len(PLANNED_LINK_CASES),
            "passed": link_passed,
            "failed": link_failed,
            "skipped": link_skipped,
        },
        "stage0_regression": {
            "test_cases_planned": len(PLANNED_ARCH_CASES),
            "passed": arch_passed,
            "failed": arch_failed,
            "skipped": arch_skipped,
            "test_cases": arch_cases,
        },
        "test_cases": link_cases,
        "tests": sorted(result.records.values(), key=lambda item: item["test_id"]),
    }

    candidate_result = os.path.join(run_directory, "candidate-result.json")
    candidate_logs = os.path.join(run_directory, "candidate-logs.jsonl")
    candidate_output = os.path.join(artifacts, "candidate-unittest-output.txt")
    candidate_manifest = os.path.join(artifacts, "candidate-verification-manifest.json")
    _write_json(candidate_result, payload)
    _write_json(candidate_manifest, snapshot)
    with open(candidate_output, "w", encoding="utf-8") as handle:
        handle.write(stream.getvalue())

    context = LogContext(
        endpoint_id="stage1-link-test-runner",
        role="TEST",
        code_version=snapshot["project_version"],
        config_fingerprint=snapshot["source_tree_sha256"],
        registry_fingerprints=fingerprints,
        run_id=run_id,
        seed=seed,
        test_clock_id=started_at.clock_id,
    )
    with open(candidate_logs, "w", encoding="utf-8") as handle:
        logger = JsonlLogger(handle, context, logger_name="stage1-link-runner")
        for case in link_cases:
            logger.log(
                timestamp=finished_at,
                severity=(
                    EventSeverity.INFO
                    if case["outcome"] == "PASS"
                    else EventSeverity.ERROR
                ),
                layer=Layer.LINK,
                event_code=f"TEST_{case['outcome']}",
                message=f"{case['test_case']} {case['outcome']}",
                details=Details.of(
                    test_case=case["test_case"],
                    outcome=case["outcome"],
                    test_count=len(case["tests"]),
                ),
            )

    if complete_success:
        os.replace(candidate_output, os.path.join(artifacts, "unittest_output.txt"))
        os.replace(
            candidate_manifest, os.path.join(artifacts, "verification_manifest.json")
        )
        os.replace(candidate_logs, os.path.join(run_directory, "logs.jsonl"))
        os.replace(candidate_result, os.path.join(run_directory, "result.json"))
    else:
        os.replace(candidate_output, os.path.join(artifacts, "failed-unittest-output.txt"))
        os.replace(
            candidate_manifest, os.path.join(artifacts, "failed-verification-manifest.json")
        )
        os.replace(candidate_logs, os.path.join(run_directory, "failed-logs.jsonl"))
        os.replace(candidate_result, os.path.join(run_directory, "failed-result.json"))

    relative = os.path.relpath(run_directory, VDETER_ROOT).replace(os.sep, "/")
    print(
        f"[run] {run_id}: {link_passed} LINK PASS / {link_failed} FAIL / "
        f"{link_skipped} SKIP; {arch_passed} ARCH PASS"
    )
    print(f"[evidence] {relative}/result.json")
    return 0 if complete_success else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 1 Link TC-LINK verification and evidence generation"
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now(timezone.utc).strftime("stage1-link-%Y%m%dT%H%M%SZ"),
        help="evidence directory name",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    arguments = parser.parse_args(argv)
    return run(arguments.run_id, arguments.seed)


if __name__ == "__main__":
    raise SystemExit(main())
