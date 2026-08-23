"""TC-STAGE5-020: Stage 0~4 회귀 연동과 traceability·evidence schema 검사.

요구사항: `STAGE5-REQ-20` (`stage5_service_integration_baseline.md` §11, §12, §14)

Stage 0~4 전체 회귀 자체는 `tools/run_stage5_tests.py`가 별도 process로 실행해
evidence에 기록한다. 이 module은 그 evidence와 추적성 record의 schema 정합성을
검사한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest

from tests import DOCS_CORE, VDETER_ROOT

PROJECT_ROOT = os.path.dirname(os.path.dirname(VDETER_ROOT))
VECTOR_ROOT = os.path.join(VDETER_ROOT, "tests", "vectors", "presentation")

STAGE5_BASELINE_ID = "VDETER-STAGE5-SERVICE-INTEGRATION-BL-001"
STAGE5_BASELINE_VERSION = "0.1.4"
STAGE5_REQUIREMENTS = tuple(f"STAGE5-REQ-{index:02d}" for index in range(1, 21))
STAGE5_TEST_CASES = tuple(f"TC-STAGE5-{index:03d}" for index in range(1, 21))

#: 승인 문서 §6.6의 독립 golden vector SHA-256 (production codec으로 생성 금지).
APPROVED_VECTOR_SHA256 = {
    "GV-STAGE5-PRES-OPAQUE-001": {
        "application": "f742b965f156c10374bc23aea96e3a8aff8facd6fc079defeaa30219ad86f211",
        "wire": "890f598700c95a02a21c6dab71240a92682a8cb5abcce45a40852eb28f656248",
    },
    "GV-STAGE5-PRES-X509-001": {
        "wire": "374b10e619ca33c6584c3eaf8510aa7bdcf8b54e54bdea5dae3796b8aaa65501",
    },
    "GV-STAGE5-PRES-AIS1-001": {
        "application": "fbe7fca9aefeec9de1f97e9287fb7af4c1393529c46d3d582e6c4ebbed32f3bf",
        "wire": "095d988bff18222e77415f0cfe516d66025cdca20d296096d67076c031779780",
    },
}


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class Stage5TraceabilityAuditTest(unittest.TestCase):
    def setUp(self):
        self.data = _load_toml(os.path.join(DOCS_CORE, "traceability.toml"))
        self.baselines = {record["id"]: record for record in self.data["baselines"]}
        self.requirements = {
            record["id"]: record
            for record in self.data["requirements"]
            if record["baseline_id"] == STAGE5_BASELINE_ID
        }
        self.test_cases = {record["id"]: record for record in self.data["test_cases"]}

    def test_TC_STAGE5_020_baseline_record_is_verified_and_frozen(self):
        baseline = self.baselines[STAGE5_BASELINE_ID]
        self.assertEqual(STAGE5_BASELINE_VERSION, baseline["version"])
        self.assertEqual("VERIFIED/FROZEN", baseline["status"])

    def test_TC_STAGE5_020_all_requirements_are_registered_and_verified(self):
        self.assertEqual(set(STAGE5_REQUIREMENTS), set(self.requirements))
        for requirement_id, record in self.requirements.items():
            with self.subTest(requirement=requirement_id):
                self.assertEqual(STAGE5_BASELINE_VERSION, record["baseline_version"])
                self.assertEqual("VERIFIED", record["status"])
                self.assertTrue(record["normative_refs"])
                self.assertTrue(record["implementation"])
                self.assertTrue(record["test_cases"])
                self.assertTrue(record["evidence"])
                for reference in record["implementation"] + record["evidence"]:
                    path = reference.split("::")[0]
                    run_id = os.path.basename(os.path.dirname(path))
                    if (
                        not os.path.exists(os.path.join(PROJECT_ROOT, path))
                        and run_id == os.environ.get("VDETER_STAGE5_ACTIVE_RUN")
                        and path.endswith("result.json")
                    ):
                        # 현재 runner의 결과는 suite와 회귀가 끝난 뒤 완성된다.
                        continue
                    self.assertTrue(
                        os.path.exists(os.path.join(PROJECT_ROOT, path)), path
                    )

    def test_TC_STAGE5_020_all_test_cases_have_existing_modules(self):
        for test_case in STAGE5_TEST_CASES:
            with self.subTest(test_case=test_case):
                record = self.test_cases[test_case]
                self.assertTrue(record["title"])
                self.assertTrue(record["module"])
                self.assertTrue(
                    os.path.exists(os.path.join(PROJECT_ROOT, record["module"]))
                )

    def test_TC_STAGE5_020_every_test_case_is_traced_to_a_requirement(self):
        referenced = set()
        for record in self.requirements.values():
            referenced.update(record["test_cases"])
        self.assertEqual(set(STAGE5_TEST_CASES), referenced)

    def test_TC_STAGE5_020_stage5_evidence_schema_is_consistent(self):
        result_paths = set()
        for record in self.requirements.values():
            for reference in record["evidence"]:
                path = reference.split("::")[0]
                if path.endswith("result.json") and "stage5" in path:
                    result_paths.add(os.path.join(PROJECT_ROOT, path))
        self.assertTrue(
            result_paths, "Stage 5 요구사항 record는 실행 result.json evidence를 가져야 합니다"
        )
        active_run = os.environ.get("VDETER_STAGE5_ACTIVE_RUN")
        for result_path in sorted(result_paths):
            run_id = os.path.basename(os.path.dirname(result_path))
            with self.subTest(result=run_id):
                if active_run is not None and run_id == active_run and not os.path.exists(result_path):
                    # 현재 runner가 최종 payload를 쓰기 전인 실행 창만 허용한다.
                    continue
                with open(result_path, "r", encoding="utf-8") as handle:
                    evidence = json.load(handle)
                self.assertEqual(STAGE5_BASELINE_ID, evidence["baseline_id"])
                self.assertEqual(STAGE5_BASELINE_VERSION, evidence["baseline_version"])
                if active_run is not None and run_id == active_run:
                    # 이 audit 자체가 그 run 안에서 실행 중이다. 완료 payload는 runner가
                    # suite 종료 뒤 기록하므로 여기서는 RUNNING sentinel만 허용한다.
                    self.assertEqual(
                        "RUNNING", evidence["stage5_completion"]["gate"]
                    )
                    continue
                if "totals" not in evidence:
                    # 중단된 run의 RUNNING sentinel은 완료 evidence가 아니다. KeyError가
                    # 아니라 명시적 실패로 보고한다.
                    self.fail(
                        f"완료되지 않은 Stage 5 evidence(RUNNING sentinel)입니다: {run_id}"
                    )
                self.assertEqual("VERIFIED/FROZEN", evidence["baseline_status"])
                self.assertEqual(
                    "APPROVED_2026-08-23",
                    evidence["stage5_completion"]["freeze_decision"],
                )
                self.assertIn("started_at", evidence)
                self.assertIn("finished_at", evidence)
                self.assertIn("command", evidence)
                self.assertIn("git", evidence)
                totals = evidence["totals"]
                self.assertTrue(totals["unittest_success"])
                self.assertEqual(0, totals["unittest_failures"])
                self.assertEqual(0, totals["unittest_errors"])
                self.assertEqual(0, totals["failed"])
                # 새 Stage 5 software test는 skip을 허용하지 않는다.
                self.assertEqual(0, totals["skipped"])
                outcomes = {
                    case["test_case"]: case["outcome"]
                    for case in evidence["test_cases"]
                }
                self.assertEqual(set(STAGE5_TEST_CASES), set(outcomes))
                self.assertTrue(
                    all(outcome == "PASS" for outcome in outcomes.values()), outcomes
                )
                regression = evidence["stage04_regression"]
                self.assertEqual("PASS", regression["outcome"])
                self.assertGreater(regression["tests_run"], 0)
                self.assertEqual(0, regression["failures"])
                self.assertEqual(0, regression["errors"])
                run_directory = os.path.dirname(result_path)
                for name in ("stdout", "junit"):
                    artifact = os.path.join(run_directory, regression["artifacts"][name])
                    self.assertTrue(os.path.isfile(artifact), artifact)
                    self.assertGreater(os.path.getsize(artifact), 0)
                vectors = evidence["golden_vectors"]
                self.assertEqual(
                    APPROVED_VECTOR_SHA256["GV-STAGE5-PRES-AIS1-001"]["wire"],
                    vectors["GV-STAGE5-PRES-AIS1-001"]["wire_sha256"],
                )
                # §7.2: registry fingerprint와 content version이 evidence에 기록된다.
                registry = evidence["payload_format_registry"]
                self.assertEqual("stage5-r1027-ed1.1-v1", registry["content_version"])
                self.assertEqual(
                    ["stage5-ship-a", "stage5-ship-b", "stage5-roc"],
                    registry["endpoints"],
                )
                self.assertEqual(2, registry["scopes"]["OPERATIONAL"]["entry_count"])
                self.assertEqual(3, registry["scopes"]["TEST"]["entry_count"])
                for scope in ("OPERATIONAL", "TEST"):
                    self.assertEqual(64, len(registry["scopes"][scope]["fingerprint"]))
                # scope가 fingerprint에 포함되므로 두 scope의 지문은 다르다.
                self.assertNotEqual(
                    registry["scopes"]["OPERATIONAL"]["fingerprint"],
                    registry["scopes"]["TEST"]["fingerprint"],
                )
                # §8.5: 세 format 시나리오의 checkpoint correlation artifact가 보존된다.
                checkpoints = evidence["checkpoint_artifacts"]
                for name in (
                    "opaque-short-data.json",
                    "x509-certificate-report.json",
                    "ais-authentication-scheme1.json",
                ):
                    self.assertIn(name, checkpoints, name)
                    artifact_path = os.path.join(
                        run_directory, "artifacts", "stage5-checkpoints", name
                    )
                    self.assertTrue(os.path.isfile(artifact_path), artifact_path)
                    payload = open(artifact_path, "rb").read()
                    self.assertEqual(
                        checkpoints[name]["sha256"],
                        hashlib.sha256(payload).hexdigest(),
                        name,
                    )
                    checkpoint = json.loads(payload.decode("utf-8"))
                    self.assertTrue(checkpoint["byte_for_byte_equal"], name)
                    self.assertEqual(
                        checkpoint["checkpoint_1_tx_application"]["sha256"],
                        checkpoint["checkpoint_7_rx_application"]["sha256"],
                        name,
                    )
                # 기준선 문서 hash가 evidence에 기록되고 현재 문서와 일치한다.
                document_path = os.path.join(
                    DOCS_CORE, "stage5_service_integration_baseline.md"
                )
                current_document = hashlib.sha256(
                    open(document_path, "rb").read()
                ).hexdigest()
                self.assertEqual(
                    current_document, evidence["baseline_document_sha256"],
                    "기준선 문서가 evidence 생성 이후 변경되었습니다",
                )
                # source manifest가 현재 tree와 일치해야 코드↔결과 1:1 연결이 성립한다.
                from tools.run_stage5_tests import _source_manifest

                current_sources = _source_manifest()
                self.assertEqual(
                    current_sources["sha256"],
                    evidence["source_manifest"]["sha256"],
                    "구현 tree가 evidence 생성 이후 변경되었습니다",
                )


class Stage5GoldenVectorManifestAuditTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(VECTOR_ROOT, "manifest.json"), "r", encoding="utf-8") as handle:
            self.manifest = json.load(handle)

    def test_TC_STAGE5_020_manifest_matches_approved_constants_and_files(self):
        self.assertEqual(STAGE5_BASELINE_ID, self.manifest["baseline_id"])
        self.assertEqual(STAGE5_BASELINE_VERSION, self.manifest["baseline_version"])
        for vector_id, expected in APPROVED_VECTOR_SHA256.items():
            entry = self.manifest["vectors"][vector_id]
            for kind, digest in expected.items():
                with self.subTest(vector=vector_id, kind=kind):
                    self.assertEqual(digest, entry[kind]["sha256"])
                    artifact = os.path.join(VECTOR_ROOT, entry[kind]["file"])
                    payload = open(artifact, "rb").read()
                    self.assertEqual(entry[kind]["bytes"], len(payload))
                    self.assertEqual(digest, hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
