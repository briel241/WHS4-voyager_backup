"""TC-ARCH-026: 요구사항·시험·증거 추적 schema 검사.

요구사항: `ARCH-REQ-03`, `ARCH-REQ-04`
"""

from __future__ import annotations

import io
import json
import os
import re
import unittest

from tests import DOCS_CORE, VDETER_ROOT

ALLOWED_STATUS = ("NOT_STARTED", "BASELINE_PENDING", "IMPLEMENTED", "VERIFIED", "BLOCKED")
ALLOWED_BASELINE_STATUS = ("DRAFT", "APPROVED", "VERIFIED/FROZEN")
REQUIREMENT_PATTERN = re.compile(r"^(?:ARCH|LINK|PHY11|RADIO)-REQ-\d{2}$")
TEST_CASE_PATTERN = re.compile(r"^TC-(?:ARCH|LINK|PHY11|RADIO)-\d{3}$")
EXPECTED_REQUIREMENTS = (
    tuple(f"ARCH-REQ-{index:02d}" for index in range(1, 8))
    + tuple(f"LINK-REQ-{index:02d}" for index in range(1, 14))
    + tuple(f"PHY11-REQ-{index:02d}" for index in range(1, 11))
    + tuple(f"RADIO-REQ-{index:02d}" for index in range(1, 12))
)
EXPECTED_TEST_CASES = (
    tuple(f"TC-ARCH-{index:03d}" for index in range(1, 28))
    + tuple(f"TC-LINK-{index:03d}" for index in range(1, 35))
    + tuple(f"TC-PHY11-{index:03d}" for index in range(1, 17))
    + tuple(f"TC-RADIO-{index:03d}" for index in range(1, 16))
)
STAGE0_BASELINE_ID = "VDETER-STAGE0-BL-001"
STAGE2_PHY11_BASELINE_ID = "VDETER-STAGE2-PHY11-BL-001"
STAGE3_RADIO_BASELINE_ID = "VDETER-STAGE3-RADIO-BL-001"


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class TraceabilitySchemaTest(unittest.TestCase):
    """traceability.toml의 schema, ID 유일성과 evidence gate."""

    def setUp(self):
        self.path = os.path.join(DOCS_CORE, "traceability.toml")
        self.data = _load_toml(self.path)
        self.baselines = {record["id"]: record for record in self.data["baselines"]}

    def test_TC_ARCH_026_schema_fields_present(self):
        self.assertEqual(2, self.data["schema_version"])
        self.assertIn("baselines", self.data)
        self.assertIn("requirements", self.data)
        self.assertIn("test_cases", self.data)

    def test_TC_ARCH_026_baseline_scopes_are_unique_and_valid(self):
        records = self.data["baselines"]
        ids = [record["id"] for record in records]
        self.assertEqual(len(ids), len(set(ids)))
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        for record in records:
            with self.subTest(baseline=record["id"]):
                self.assertTrue(record["version"])
                self.assertIn(record["status"], ALLOWED_BASELINE_STATUS)
                self.assertTrue(os.path.isfile(os.path.join(project_root, record["document"])))

    def test_TC_ARCH_026_requirement_ids_are_unique_and_defined(self):
        ids = [record["id"] for record in self.data["requirements"]]
        self.assertEqual(len(ids), len(set(ids)), "요구사항 ID가 중복되었습니다")
        for requirement_id in ids:
            self.assertRegex(requirement_id, REQUIREMENT_PATTERN)
        self.assertEqual(set(EXPECTED_REQUIREMENTS), set(ids))

    def test_TC_ARCH_026_status_values_are_allowed(self):
        for record in self.data["requirements"]:
            with self.subTest(requirement=record["id"]):
                self.assertIn(record["status"], ALLOWED_STATUS)
                self.assertIn(record["baseline_id"], self.baselines)
                self.assertEqual(
                    self.baselines[record["baseline_id"]]["version"],
                    record["baseline_version"],
                )

    def test_TC_ARCH_026_test_case_ids_are_defined(self):
        referenced = set()
        for record in self.data["requirements"]:
            for test_case in record["test_cases"]:
                self.assertRegex(test_case, TEST_CASE_PATTERN)
                referenced.add(test_case)
        undefined = sorted(referenced - set(EXPECTED_TEST_CASES))
        self.assertEqual([], undefined, f"정의되지 않은 시험 ID: {undefined}")

    def test_TC_ARCH_026_every_planned_test_case_is_traced(self):
        referenced = set()
        for record in self.data["requirements"]:
            referenced.update(record["test_cases"])
        missing = sorted(set(EXPECTED_TEST_CASES) - referenced)
        self.assertEqual([], missing, f"추적되지 않은 시험 ID: {missing}")

    def test_TC_ARCH_026_verified_requires_evidence_and_tests(self):
        for record in self.data["requirements"]:
            if record["status"] != "VERIFIED":
                continue
            with self.subTest(requirement=record["id"]):
                self.assertTrue(record["test_cases"], "VERIFIED에는 시험이 필요합니다")
                self.assertTrue(record["evidence"], "VERIFIED에는 증거가 필요합니다")
                self.assertTrue(
                    record["implementation"], "VERIFIED에는 구현 참조가 필요합니다"
                )

    def test_TC_ARCH_026_implementation_paths_exist(self):
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        for record in self.data["requirements"]:
            for reference in record.get("implementation", ()):
                path = reference.split("::")[0]
                with self.subTest(requirement=record["id"], path=path):
                    self.assertTrue(
                        os.path.exists(os.path.join(project_root, path)),
                        f"구현 경로가 없습니다: {path}",
                    )

    def test_TC_ARCH_026_evidence_paths_exist(self):
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        for record in self.data["requirements"]:
            for reference in record.get("evidence", ()):
                path = reference.split("::")[0]
                with self.subTest(requirement=record["id"], path=path):
                    self.assertTrue(
                        os.path.exists(os.path.join(project_root, path)),
                        f"증거 경로가 없습니다: {path}",
                    )

    def test_TC_ARCH_026_verified_evidence_proves_current_successful_run(self):
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        loaded_results = {}
        for record in self.data["requirements"]:
            if record["status"] != "VERIFIED":
                continue
            result_references = [
                reference.split("::")[0]
                for reference in record.get("evidence", ())
                if reference.split("::")[0].endswith("result.json")
            ]
            with self.subTest(requirement=record["id"]):
                self.assertTrue(
                    result_references,
                    "VERIFIED 요구사항은 실행 result.json을 증거로 가져야 합니다",
                )
            for reference in result_references:
                result_path = os.path.join(project_root, reference)
                if result_path not in loaded_results:
                    with open(result_path, "r", encoding="utf-8") as handle:
                        loaded_results[result_path] = json.load(handle)
                evidence = loaded_results[result_path]
                self.assertIn("totals", evidence, f"완료되지 않은 증거입니다: {reference}")
                self.assertIn("test_cases", evidence, f"시험 결과가 없는 증거입니다: {reference}")
                totals = evidence["totals"]
                outcomes = {
                    case["test_case"]: case["outcome"]
                    for case in evidence["test_cases"]
                }
                baseline_test_cases = {
                    test_case
                    for requirement in self.data["requirements"]
                    if requirement["baseline_id"] == record["baseline_id"]
                    for test_case in requirement["test_cases"]
                }
                with self.subTest(requirement=record["id"], result=reference):
                    self.assertEqual(record["baseline_id"], evidence["baseline_id"])
                    self.assertEqual(
                        record["baseline_version"], evidence["baseline_version"]
                    )
                    self.assertTrue(totals["unittest_success"])
                    self.assertEqual(0, totals["unittest_failures"])
                    self.assertEqual(0, totals["unittest_errors"])
                    self.assertEqual(0, totals["failed"])
                    self.assertEqual(0, totals["skipped"])
                    self.assertEqual(baseline_test_cases, set(outcomes))
                    self.assertTrue(all(outcome == "PASS" for outcome in outcomes.values()))
                    for test_case in record["test_cases"]:
                        self.assertEqual("PASS", outcomes.get(test_case), test_case)

                    run_directory = os.path.dirname(result_path)
                    for artifact in (
                        "logs.jsonl",
                        os.path.join("artifacts", "unittest_output.txt"),
                    ):
                        artifact_path = os.path.join(run_directory, artifact)
                        self.assertTrue(os.path.isfile(artifact_path), artifact_path)
                        self.assertGreater(os.path.getsize(artifact_path), 0, artifact_path)

    def test_TC_ARCH_026_test_case_table_is_complete_and_unique(self):
        declared = [record["id"] for record in self.data["test_cases"]]
        self.assertEqual(len(declared), len(set(declared)), "시험 ID가 중복되었습니다")
        self.assertEqual(set(EXPECTED_TEST_CASES), set(declared))
        for record in self.data["test_cases"]:
            with self.subTest(test_case=record["id"]):
                self.assertRegex(record["id"], TEST_CASE_PATTERN)
                self.assertTrue(record["title"])

    def test_TC_ARCH_026_test_case_modules_exist(self):
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        for record in self.data["test_cases"]:
            with self.subTest(test_case=record["id"]):
                if not record["module"]:
                    requirements = [
                        requirement
                        for requirement in self.data["requirements"]
                        if record["id"] in requirement["test_cases"]
                    ]
                    self.assertTrue(requirements)
                    self.assertTrue(
                        all(
                            requirement["status"] in ("NOT_STARTED", "BASELINE_PENDING")
                            for requirement in requirements
                        ),
                        "planned module omission is allowed only for pending requirements",
                    )
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(project_root, record["module"])),
                    f"시험 module이 없습니다: {record['module']}",
                )

    def test_TC_ARCH_026_normative_refs_present(self):
        for record in self.data["requirements"]:
            with self.subTest(requirement=record["id"]):
                self.assertTrue(record["normative_refs"])

    def test_TC_ARCH_026_runner_records_subtest_failures_and_current_baseline(self):
        from tools.run_stage0_tests import RecordingResult, _baseline_version

        class FailingSubtest(unittest.TestCase):
            def runTest(self):
                with self.subTest(case="failure"):
                    self.fail("synthetic subtest failure")

        case = FailingSubtest()
        result = RecordingResult(io.StringIO(), True, 1)
        case.run(result)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual("FAIL", result.records[case.id()]["outcome"])
        self.assertEqual(self.baselines[STAGE0_BASELINE_ID]["version"], _baseline_version())

    def test_TC_LINK_032_stage1_scope_and_verified_gate(self):
        stage1 = self.baselines["VDETER-STAGE1-LINK-BL-001"]
        self.assertEqual("0.5.3", stage1["version"])
        self.assertEqual("VERIFIED/FROZEN", stage1["status"])
        cases = {record["id"]: record for record in self.data["test_cases"]}
        for requirement in self.data["requirements"]:
            if requirement["baseline_id"] != stage1["id"]:
                continue
            with self.subTest(requirement=requirement["id"]):
                self.assertEqual(stage1["version"], requirement["baseline_version"])
                self.assertEqual("VERIFIED", requirement["status"])
                self.assertTrue(requirement["implementation"])
                self.assertTrue(requirement["evidence"])
                self.assertTrue(
                    all(cases[test_case]["module"] for test_case in requirement["test_cases"])
                )

    def test_TC_LINK_032_stage1_runner_records_compound_cases(self):
        from tools.run_stage1_link_tests import RecordingResult, _baseline_scope

        class CompoundCase(unittest.TestCase):
            def test_TC_LINK_013_014_029_synthetic(self):
                pass

        case = CompoundCase("test_TC_LINK_013_014_029_synthetic")
        result = RecordingResult(io.StringIO(), True, 1)
        case.run(result)
        record = result.records[case.id()]
        self.assertEqual(
            ("TC-LINK-013", "TC-LINK-014", "TC-LINK-029"),
            record["link_test_cases"],
        )
        self.assertEqual("0.5.3", _baseline_scope()["version"])
        self.assertEqual("VERIFIED/FROZEN", _baseline_scope()["status"])

    def test_TC_ARCH_026_stage2_phy11_scope_is_verified_and_frozen(self):
        stage2 = self.baselines[STAGE2_PHY11_BASELINE_ID]
        self.assertEqual("0.4.6", stage2["version"])
        self.assertEqual("VERIFIED/FROZEN", stage2["status"])
        requirements = [
            record
            for record in self.data["requirements"]
            if record["baseline_id"] == STAGE2_PHY11_BASELINE_ID
        ]
        self.assertEqual(10, len(requirements))
        cases = {record["id"]: record for record in self.data["test_cases"]}
        for requirement in requirements:
            with self.subTest(requirement=requirement["id"]):
                self.assertEqual("VERIFIED", requirement["status"])
                self.assertTrue(requirement["parent_requirements"])
                self.assertTrue(requirement["implementation"])
                self.assertTrue(requirement["evidence"])
                self.assertTrue(requirement["test_cases"])
                self.assertTrue(
                    all(cases[test_case]["module"] for test_case in requirement["test_cases"])
                )

    def test_TC_RADIO_015_stage3_scope_is_approved_and_implemented_not_verified(self):
        stage3 = self.baselines[STAGE3_RADIO_BASELINE_ID]
        self.assertEqual("0.2.2", stage3["version"])
        self.assertEqual("APPROVED", stage3["status"])
        requirements = [
            record
            for record in self.data["requirements"]
            if record["baseline_id"] == STAGE3_RADIO_BASELINE_ID
        ]
        self.assertEqual(11, len(requirements))
        cases = {record["id"]: record for record in self.data["test_cases"]}
        for requirement in requirements:
            with self.subTest(requirement=requirement["id"]):
                self.assertEqual("IMPLEMENTED", requirement["status"])
                self.assertTrue(requirement["normative_refs"])
                self.assertTrue(requirement["decision_records"])
                self.assertTrue(requirement["implementation"])
                self.assertTrue(requirement["test_cases"])
                self.assertTrue(requirement["evidence"])
                self.assertTrue(
                    all(
                        cases[test_case]["module"]
                        for test_case in requirement["test_cases"]
                        if test_case not in {"TC-RADIO-012", "TC-RADIO-013", "TC-RADIO-015"}
                    )
                )


if __name__ == "__main__":
    unittest.main()
