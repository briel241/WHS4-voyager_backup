"""TC-LINK-017: Stage 0 frozen contract remains in the combined regression suite."""

from __future__ import annotations

import os
import unittest

from tests import DOCS_CORE, VDETER_ROOT


def _load_traceability():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    with open(os.path.join(DOCS_CORE, "traceability.toml"), "rb") as handle:
        return tomllib.load(handle)


class LinkStage0RegressionTest(unittest.TestCase):
    def test_TC_LINK_017_frozen_stage0_cases_remain_in_combined_suite(self):
        trace = _load_traceability()
        baselines = {item["id"]: item for item in trace["baselines"]}
        stage0 = baselines["VDETER-STAGE0-BL-001"]
        self.assertEqual("VERIFIED/FROZEN", stage0["status"])
        project_root = os.path.dirname(os.path.dirname(VDETER_ROOT))
        declared_cases = {item["id"]: item for item in trace["test_cases"]}
        stage0_requirements = [
            item
            for item in trace["requirements"]
            if item["baseline_id"] == stage0["id"]
        ]
        self.assertTrue(stage0_requirements)
        self.assertTrue(all(item["status"] == "VERIFIED" for item in stage0_requirements))
        stage0_cases = {
            case
            for requirement in stage0_requirements
            for case in requirement["test_cases"]
        }
        self.assertEqual(set(f"TC-ARCH-{index:03d}" for index in range(1, 28)), stage0_cases)
        for case in stage0_cases:
            module = declared_cases[case]["module"]
            self.assertTrue(module)
            self.assertTrue(os.path.exists(os.path.join(project_root, module)))


if __name__ == "__main__":
    unittest.main()
