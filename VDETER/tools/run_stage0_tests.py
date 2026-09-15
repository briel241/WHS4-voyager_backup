"""단계 0 시험 실행기와 증거 생성기 (`stage0_baseline.md` §11).

`TC-ARCH-001..027`을 실행하고 다음 구조로 증거를 보존한다.

```text
results/runs/<run-id>/
├─ result.json
├─ logs.jsonl
└─ artifacts/
```

`result.json`에는 run ID, start/end time과 clock domain, code/config/profile
version·fingerprint, 환경과 dependency version, seed, 실행한 `TC-*`, expected/actual,
PASS/FAIL/SKIP 및 실패 사유를 기록한다.

이 실행기는 외부 시험 framework에 의존하지 않는 표준 `unittest` 기반이다. pytest가 설치된
환경에서도 같은 시험 module을 그대로 수집할 수 있다.

사용법
```text
python tools/run_stage0_tests.py
python tools/run_stage0_tests.py --run-id stage0-manual-1
```
"""

from __future__ import annotations

import argparse
import io
import importlib.metadata
import json
import os
import platform
import re
import sys
import time
import unittest
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if VDETER_ROOT not in sys.path:
    sys.path.insert(0, VDETER_ROOT)
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from vdeter.common.errors import Details, Layer  # noqa: E402
from vdeter.common.events import EventSeverity  # noqa: E402
from vdeter.common.logging import JsonlLogger, LogContext  # noqa: E402
from vdeter.common.time import ClockDomain, TimePoint  # noqa: E402

RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
TRACEABILITY_PATH = os.path.join(PROJECT_ROOT, "docs", "core", "traceability.toml")
TEST_CASE_PATTERN = re.compile(r"TC_ARCH_(\d{3})")
PLANNED_TEST_CASES = tuple(f"TC-ARCH-{index:03d}" for index in range(1, 28))
DEFAULT_SEED = 20260819
STAGE0_BASELINE_ID = "VDETER-STAGE0-BL-001"


def _now() -> TimePoint:
    return TimePoint.from_nanoseconds(time.time_ns(), ClockDomain.UTC, "system-utc")


def _iso(point: TimePoint) -> str:
    return datetime.fromtimestamp(point.seconds, tz=timezone.utc).replace(
        microsecond=point.nanoseconds // 1_000
    ).isoformat()


def _baseline_version() -> str:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with open(TRACEABILITY_PATH, "rb") as handle:
        data = tomllib.load(handle)
    for baseline in data["baselines"]:
        if baseline["id"] == STAGE0_BASELINE_ID:
            return baseline["version"]
    raise RuntimeError(f"missing baseline scope: {STAGE0_BASELINE_ID}")


def _dependency_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def _test_case_of(test_id: str) -> str | None:
    match = TEST_CASE_PATTERN.search(test_id)
    if match is None:
        return None
    return f"TC-ARCH-{match.group(1)}"


class RecordingResult(unittest.TextTestResult):
    """시험별 결과와 실패 사유를 수집한다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = {}

    def _record(self, test, outcome: str, reason: str = ""):
        previous = self.records.get(test.id())
        if previous is not None and previous["outcome"] == "FAIL" and outcome != "FAIL":
            return
        if previous is not None and previous["outcome"] == "FAIL" and reason:
            reason = previous["reason"] + "\n" + reason
        self.records[test.id()] = {
            "test_id": test.id(),
            "outcome": outcome,
            "reason": reason,
            "test_case": _test_case_of(test.id()),
            "docstring": (test.shortDescription() or ""),
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


def _environment() -> dict:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "dependencies": {
            "gnuradio": "not_required",
            "uhd": "not_required",
            "numpy": _dependency_version("numpy"),
            "tomli": _dependency_version("tomli"),
        },
    }


def _configuration_summary():
    """시험이 사용하는 설정과 registry 지문을 요약한다."""

    from vdeter.config.loader import load_configuration_file
    from vdeter.runtime.wiring import build_dependencies

    path = os.path.join(VDETER_ROOT, "configs", "stage0_fake_endpoint.toml")
    loaded = load_configuration_file(path)
    if loaded.is_err:
        return {"config_error": loaded.error.code}, ()
    configuration = loaded.value
    dependencies = build_dependencies(configuration)
    fingerprints = ()
    if dependencies.is_ok:
        fingerprints = dependencies.value.registries.fingerprints()
    summary = {
        "config_path": os.path.relpath(path, VDETER_ROOT).replace(os.sep, "/"),
        "config_scope": configuration.endpoint.scope.value,
        "code_version": configuration.endpoint.code_version,
        "endpoint_fingerprint": configuration.endpoint_fingerprint,
        "radio_fingerprint": configuration.radio_fingerprint,
        "registry_fingerprints": {name: value for name, value in fingerprints},
    }
    return summary, fingerprints


def run(run_id: str, seed: int = DEFAULT_SEED) -> int:
    started_at = _now()
    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts, exist_ok=True)

    # VERIFIED evidence를 시험 시작 전에 RUNNING marker로 덮어쓰지 않는다. 추적성 시험은
    # 직전 최종 증거를 검증하고, 이번 실행 결과는 모든 시험이 끝난 뒤에만 교체한다.

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.join(VDETER_ROOT, "tests"), top_level_dir=VDETER_ROOT)

    stream = io.StringIO()
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    )
    result = runner.run(suite)
    finished_at = _now()

    summary, fingerprints = _configuration_summary()

    per_case = {}
    for record in result.records.values():
        case = record["test_case"]
        if case is None:
            continue
        entry = per_case.setdefault(
            case, {"test_case": case, "outcome": "PASS", "tests": [], "failures": []}
        )
        entry["tests"].append(record["test_id"])
        if record["outcome"] == "FAIL":
            entry["outcome"] = "FAIL"
            entry["failures"].append({"test_id": record["test_id"], "reason": record["reason"]})
        elif record["outcome"] == "SKIP" and entry["outcome"] == "PASS":
            entry["outcome"] = "SKIP"

    cases = []
    for case in PLANNED_TEST_CASES:
        entry = per_case.get(
            case,
            {
                "test_case": case,
                "outcome": "SKIP",
                "tests": [],
                "failures": [],
            },
        )
        entry["expected"] = "모든 assertion 통과"
        entry["actual"] = (
            "모든 assertion 통과"
            if entry["outcome"] == "PASS"
            else ("시험 없음" if not entry["tests"] else "assertion 실패")
        )
        cases.append(entry)

    passed = sum(1 for case in cases if case["outcome"] == "PASS")
    failed = sum(1 for case in cases if case["outcome"] == "FAIL")
    skipped = sum(1 for case in cases if case["outcome"] == "SKIP")

    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": "VDETER-STAGE0-BL-001",
        "baseline_version": _baseline_version(),
        "stage": "stage0",
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "clock_domain": started_at.clock_domain.value,
        "clock_id": started_at.clock_id,
        "seed": seed,
        "environment": _environment(),
        "configuration": summary,
        "totals": {
            "tests_run": result.testsRun,
            "unittest_success": result.wasSuccessful(),
            "unittest_failures": len(result.failures),
            "unittest_errors": len(result.errors),
            "test_cases_planned": len(PLANNED_TEST_CASES),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "test_cases": cases,
        "tests": sorted(result.records.values(), key=lambda item: item["test_id"]),
    }

    with open(os.path.join(run_directory, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    context = LogContext(
        endpoint_id="stage0-test-runner",
        role="TEST",
        code_version=summary.get("code_version", "0.1.0"),
        config_fingerprint=summary.get("endpoint_fingerprint", ""),
        registry_fingerprints=tuple(fingerprints),
        run_id=run_id,
        seed=seed,
        test_clock_id=started_at.clock_id,
    )
    with open(os.path.join(run_directory, "logs.jsonl"), "w", encoding="utf-8") as handle:
        logger = JsonlLogger(handle, context, logger_name="stage0-runner")
        for case in cases:
            logger.log(
                timestamp=finished_at,
                severity=(
                    EventSeverity.INFO if case["outcome"] == "PASS" else EventSeverity.ERROR
                ),
                layer=Layer.RUNTIME,
                event_code=f"TEST_{case['outcome']}",
                message=f"{case['test_case']} {case['outcome']}",
                details=Details.of(
                    test_case=case["test_case"],
                    outcome=case["outcome"],
                    test_count=len(case["tests"]),
                ),
            )

    with open(os.path.join(artifacts, "unittest_output.txt"), "w", encoding="utf-8") as handle:
        handle.write(stream.getvalue())

    relative = os.path.relpath(run_directory, VDETER_ROOT).replace(os.sep, "/")
    print(f"[run] {run_id}: {passed} PASS / {failed} FAIL / {skipped} SKIP")
    print(f"[evidence] {relative}/result.json")
    return 0 if result.wasSuccessful() and failed == 0 and skipped == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="단계 0 TC-ARCH 시험 실행과 증거 생성")
    parser.add_argument(
        "--run-id",
        default=datetime.now(timezone.utc).strftime("stage0-%Y%m%dT%H%M%SZ"),
        help="증거 directory 이름",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="기록할 시험 seed")
    arguments = parser.parse_args(argv)
    return run(arguments.run_id, arguments.seed)


if __name__ == "__main__":
    raise SystemExit(main())
