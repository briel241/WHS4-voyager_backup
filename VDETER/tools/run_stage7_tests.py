"""Stage 7 software 검증 실행기 (`stage7_b210_system_baseline.md` §7, §18, §20).

software-only 판정 범위의 Stage 7 시험을 실행하고 §18.4 trace 구조의 evidence
bundle을 생성한다. hardware-required 판정(P0~P3)은 이 runner가 PASS로 표시하지
않으며 해당 TC의 final status는 hardware run 전까지 `NOT_RUN`으로 남긴다.

사용법:
    python tools/run_stage7_tests.py --run-id stage7-software-<date>
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.b210_system.testing.evidence import (  # noqa: E402
    ALL_TEST_CASES,
    Stage7EvidenceBundle,
    sha256_file,
)

#: §15/§18: 실행 전후 SHA-256 불변이어야 하는 frozen source 파일.
FROZEN_SOURCE_FILES = (
    "src/vdeter/config/models.py",
    "src/vdeter/config/loader.py",
    "src/vdeter/radio/models.py",
    "src/vdeter/runtime/wiring.py",
    "src/vdeter/runtime/stage5.py",
    "src/vdeter/runtime/endpoint.py",
    "src/vdeter/runtime/phy_bridge.py",
    "src/vdeter/phy/multilink.py",
    "src/vdeter/phy/resampling.py",
    "src/vdeter/link/data_plane.py",
    "src/vdeter/mac/interface.py",
    "src/vdeter/radio/b210/adapter.py",
    "src/vdeter/radio/b210/backend.py",
    "src/vdeter/radio/b210/isolated_backend.py",
)

BASE_COMMIT = "8aa0d33"

#: software 시험 module과 결속 TC (software 부분 판정).
SOFTWARE_SUITES = {
    "tests.unit.test_stage7_canonical_json": ("TC-STAGE7-001",),
    "tests.unit.test_stage7_configuration": ("TC-STAGE7-001", "TC-STAGE7-002", "TC-STAGE7-016"),
    "tests.unit.test_stage7_routing": ("TC-STAGE7-003", "TC-STAGE7-015", "TC-STAGE7-016"),
    "tests.unit.test_stage7_phy_facade": ("TC-STAGE7-003", "TC-STAGE7-015", "TC-STAGE7-019"),
    "tests.unit.test_stage7_time_bridge": ("TC-STAGE7-007", "TC-STAGE7-016"),
    "tests.unit.test_stage7_cfo": ("TC-STAGE7-003",),
    "tests.unit.test_stage7_rf_time": ("TC-STAGE7-007", "TC-STAGE7-008"),
    "tests.unit.test_stage7_ipc": ("TC-STAGE7-016", "TC-STAGE7-020"),
    "tests.unit.test_stage7_coordinator_flow": ("TC-STAGE7-016", "TC-STAGE7-020"),
    "tests.unit.test_stage7_wiring_smoke": ("TC-STAGE7-002", "TC-STAGE7-017", "TC-STAGE7-020"),
    "tests.integration.test_stage7_software_group": ("TC-STAGE7-007", "TC-STAGE7-020"),
    "tests.unit.test_stage7_payload_vectors": ("TC-STAGE7-013",),
    "tests.unit.test_stage7_testing_harness": ("TC-STAGE7-018",),
    "tests.integration.test_stage7_software_scenarios": (
        "TC-STAGE7-009",
        "TC-STAGE7-010",
        "TC-STAGE7-011",
    ),
    "tests.integration.test_stage7_software_fault_injection": ("TC-STAGE7-012",),
    # v0.9.1 K항: RESEARCH_READY_MINIMAL software suite. RR-004..008은
    # hardware-required이므로 software-only PASS로 hardware TC를 대신하지 않는다
    # (이 module은 adaptive gain/deadline/evidence schema/final builder/negative만
    # software로 검증한다).
    "tests.unit.test_stage7_rr_profile": (
        "TC-STAGE7-RR-002", "TC-STAGE7-RR-003", "TC-STAGE7-RR-007",
    ),
}


def git_blob_sha_current(path: str) -> str | None:
    """현재 working tree 파일의 git blob hash (LF 정규화 반영)."""

    completed = subprocess.run(
        ["git", "hash-object", path],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(VDETER_ROOT),
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_blob_sha_at_base(relative: str) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", f"{BASE_COMMIT}:VDETER/{relative}"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(VDETER_ROOT),
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_blob_bytes_at_base(relative: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "cat-file", "blob", f"{BASE_COMMIT}:VDETER/{relative}"],
        capture_output=True,
        cwd=os.path.dirname(VDETER_ROOT),
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def frozen_hash_audit() -> dict:
    """frozen 파일들이 base commit 8aa0d33과 내용 불변인지 검증한다.

    판정은 CRLF→LF 정규화 후 byte-exact 비교다. 일부 파일이 최초 checkout 당시
    system `core.autocrlf=true`로 CRLF로 구체화되어 있어(내용 동일, git status
    clean) raw blob hash는 진짜 변경 판정에 쓸 수 없다. EOL만 다른 "진짜 수정"은
    존재하지 않으므로 정규화 비교가 변경 검출을 약화시키지 않는다. raw blob
    hash도 evidence로 함께 기록한다.
    """

    entries = []
    all_ok = True
    for relative in FROZEN_SOURCE_FILES:
        path = os.path.join(VDETER_ROOT, relative)
        current = git_blob_sha_current(path)
        base = git_blob_sha_at_base(relative)
        base_bytes = git_blob_bytes_at_base(relative)
        try:
            with open(path, "rb") as handle:
                current_bytes = handle.read()
        except OSError:
            current_bytes = None
        normalized_ok = (
            base_bytes is not None
            and current_bytes is not None
            and base_bytes.replace(b"\r\n", b"\n")
            == current_bytes.replace(b"\r\n", b"\n")
        )
        all_ok = all_ok and normalized_ok
        entries.append(
            {
                "file": relative,
                "base_blob": base,
                "current_blob": current,
                "raw_blob_equal": current is not None and current == base,
                "unchanged": normalized_ok,
                "sha256": sha256_file(path) if os.path.exists(path) else None,
            }
        )
    return {
        "all_unchanged": all_ok,
        "comparison": "content-exact after CRLF→LF normalization (raw blob hash recorded)",
        "files": entries,
    }


def import_graph_audit() -> dict:
    """core->TEST import 0, frozen core->B210 시스템 역참조 0 (§18.3, §22.14).

    규칙은 그대로다. 2-3 구조 정리에서 package 이름이 `stage7` -> `b210_system`,
    런타임 제어면이 `testing` -> `control_plane`으로 바뀌었으므로 **같은 규칙을 새
    이름으로** 검사한다. 이름만 옛것으로 남기면 아무것도 찾지 못한 채 항상 통과한다.

    시험 전용 자료(`b210_system.testing`: 시험계획·evidence)는 core 가 import하면 안 된다.
    런타임 제어면(`b210_system.control_plane`)은 진입점이 실행 중에 쓰는 production 경로라
    이 금지 대상이 아니다.
    """

    package = "b210_system"
    test_only = package + ".testing"
    violations = []
    core_root = os.path.join(VDETER_ROOT, "src", "vdeter")
    test_pattern = re.compile(
        r"from\s+\S*" + re.escape(test_only) + r"|import\s+\S*" + re.escape(test_only))
    test_only_dir = os.path.join(package, "testing")
    for root, _dirs, files in os.walk(core_root):
        if test_only_dir in root:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            if test_pattern.search(text):
                violations.append(os.path.relpath(path, VDETER_ROOT))
    # frozen 코드가 B210 시스템 package 를 역참조하는지도 검사한다.
    frozen_refs = []
    package_pattern = re.compile(
        r"from\s+\S*(?<![A-Za-z0-9_])" + re.escape(package) + r"(?![A-Za-z0-9_])"
        r"|import\s+\S*(?<![A-Za-z0-9_])" + re.escape(package) + r"(?![A-Za-z0-9_])")
    for root, _dirs, files in os.walk(core_root):
        if os.sep + package in root:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            if package_pattern.search(text):
                frozen_refs.append(os.path.relpath(path, VDETER_ROOT))
    return {
        "core_imports_test_only": violations,
        "frozen_imports_b210_system": frozen_refs,
        # 옛 키 이름도 유지한다 — 소비자(보고·evidence)가 참조한다.
        "core_imports_testing": violations,
        "frozen_imports_stage7": frozen_refs,
        "pass": not violations and not frozen_refs,
    }


def run_suite(module_name: str) -> dict:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(module_name)
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    return {
        "module": module_name,
        "run": result.testsRun,
        "failures": [
            {"test": str(test), "reason": reason[-2000:]}
            for test, reason in result.failures
        ],
        "errors": [
            {"test": str(test), "reason": reason[-2000:]}
            for test, reason in result.errors
        ],
        "skipped": [str(test) for test, _reason in result.skipped],
        "passed": result.wasSuccessful(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None)
    arguments = parser.parse_args(argv)
    run_id = arguments.run_id or datetime.now(timezone.utc).strftime(
        "stage7-software-%Y%m%dT%H%M%SZ"
    )
    bundle_root = os.path.join(VDETER_ROOT, "results", "runs", run_id)
    bundle = Stage7EvidenceBundle(bundle_root, run_id=run_id, judgement_scope="SOFTWARE_ONLY")

    frozen = frozen_hash_audit()
    frozen_artifact = bundle.write_artifact("artifacts/frozen_hash_audit.json", frozen)
    imports = import_graph_audit()
    import_artifact = bundle.write_artifact("artifacts/import_graph_audit.json", imports)

    suite_results = {}
    tc_subcases = {tc: [] for tc in ALL_TEST_CASES}
    # v0.9.1 K항: RR trace는 full qualification trace와 분리한다. RR TC ID는
    # ALL_TEST_CASES(full-qual)에 없으므로 별도 dict로 라우팅한다.
    rr_subcases = {}
    overall_ok = True
    for module_name, test_cases in SOFTWARE_SUITES.items():
        print(f"[suite] {module_name}", flush=True)
        outcome = run_suite(module_name)
        suite_results[module_name] = outcome
        overall_ok = overall_ok and outcome["passed"]
        artifact = bundle.write_artifact(
            f"artifacts/suite_{module_name.replace('.', '_')}.json", outcome
        )
        for test_case in test_cases:
            entry = {
                "subcase": module_name,
                "judgement": "software-only",
                "status": "PASS" if outcome["passed"] else "FAIL",
                "tests_run": outcome["run"],
                "skipped": len(outcome["skipped"]),
                "artifact": artifact,
            }
            if test_case.startswith("TC-STAGE7-RR-"):
                rr_subcases.setdefault(test_case, []).append(entry)
            else:
                tc_subcases[test_case].append(entry)

    static_pass = frozen["all_unchanged"] and imports["pass"]
    tc_subcases["TC-STAGE7-001"].append(
        {
            "subcase": "frozen_hash_and_import_graph_static_audit",
            "judgement": "software-only",
            "status": "PASS" if static_pass else "FAIL",
            "artifact": frozen_artifact,
        }
    )
    tc_subcases["TC-STAGE7-002"].append(
        {
            "subcase": "frozen_source_hash_invariance",
            "judgement": "software-only",
            "status": "PASS" if frozen["all_unchanged"] else "FAIL",
            "artifact": frozen_artifact,
        }
    )
    tc_subcases["TC-STAGE7-020"].append(
        {
            "subcase": "module_import_call_spy",
            "judgement": "software-only",
            "status": "PASS" if imports["pass"] else "FAIL",
            "artifact": import_artifact,
        }
    )

    for test_case in ALL_TEST_CASES:
        subcases = tc_subcases[test_case]
        any_fail = any(item["status"] == "FAIL" for item in subcases)
        # hardware-required 판정이 남은 TC는 software 부분만으로 PASS를 만들지
        # 않는다 (§18.3, §22). software 부분 실패는 즉시 FAIL이다.
        status = "FAIL" if any_fail else "NOT_RUN"
        bundle.write_trace(
            test_case,
            status=status,
            judgement="mixed",
            expected="§18.3 표의 해당 TC 합격 기준",
            actual=(
                "software-only 부분 실행 결과는 subcases에 있다; "
                "hardware-required 판정은 미실행"
            ),
            child_artifacts=[item["artifact"] for item in subcases],
            subcases=subcases,
            root_cause=None if not any_fail else "software subcase failure",
        )

    # v0.9.1 K항: RR software trace를 full-qual trace와 별도 artifact로 기록한다.
    # software-only 결과는 hardware-required RR TC(RR-004..008)의 PASS를
    # 대신하지 않는다 — software 부분 상태만 남기고 hardware는 미실행 표기.
    bundle.write_artifact(
        "artifacts/rr_software_trace.json",
        {
            "profile": "RESEARCH_READY_MINIMAL",
            "note": "software-only 부분 검증. hardware-required RR TC의 PASS를 "
                    "대신하지 않는다. 최종 판정은 evidence/research_ready/의 "
                    "hardware evidence와 final-result.json이 한다.",
            "rr_subcases": rr_subcases,
        },
    )
    bundle.write_artifact(
        "artifacts/software_suite_summary.json",
        {
            "run_id": run_id,
            "overall_software_pass": overall_ok and static_pass,
            "suites": {
                name: {
                    "passed": data["passed"],
                    "run": data["run"],
                    "failures": len(data["failures"]),
                    "errors": len(data["errors"]),
                    "skipped": len(data["skipped"]),
                }
                for name, data in suite_results.items()
            },
            "frozen_hash_pass": frozen["all_unchanged"],
            "import_graph_pass": imports["pass"],
            "rr_software_traced": sorted(rr_subcases),
        },
    )
    bundle.write_index()
    bundle.write_sha256sums()
    print(
        f"[stage7-software] overall={'PASS' if overall_ok and static_pass else 'FAIL'} "
        f"bundle={bundle_root}",
        flush=True,
    )
    return 0 if overall_ok and static_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
