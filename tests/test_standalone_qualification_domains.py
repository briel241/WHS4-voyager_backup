"""자격 결속 3영역 분리 + evidence 재사용 판정 시험 (Phase I, 2026-09-05).

- 분류 completeness/partition: 실제 배포 manifest의 모든 파일이 정확히 한
  영역으로 분류되는지(‘unknown’ 없음, 합=전체).
- 알려진 파일의 영역 고정(coordinator/pi_runtime→rf, qualify_cmd→tooling,
  cli/run_cmd→operation, tests→test_doc, config→rf, tools→tooling).
- evaluate_reuse 가드: identical→eligible, tooling-only 변경→eligible,
  rf 변경→거부, operation 변경→회귀 없으면 거부/있으면 허용, test_doc→무영향.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from pathlib import Path

from apps.standalone import deploy_manifest
from apps.standalone import qualification_domains as qd


def _manifest_from(paths):
    return {"files": [{"path": p, "sha256": s} for p, s in paths.items()]}


# --------------------------------------------------------- classification

def test_known_files_classify_to_expected_domain():
    cases = {
        "VDETER/apps/stage7_coordinator_main.py": qd.RF,
        "VDETER/apps/stage7_endpoint_launcher.py": qd.RF,
        "VDETER/apps/stage7_scenario_controller.py": qd.RF,
        "VDETER/src/vdeter/stage7/pi_runtime.py": qd.RF,
        "VDETER/src/vdeter/phy/resampling.py": qd.RF,
        "VDETER/configs/stage7/standalone-binding.json": qd.RF,
        "apps/standalone/qualify_cmd.py": qd.TOOLING,
        "VDETER/tools/stage7_rr_final_builder.py": qd.TOOLING,
        "apps/standalone/templates/hardware.toml.example": qd.TOOLING,
        "apps/standalone/cli.py": qd.OPERATION,
        "apps/standalone/run_cmd.py": qd.OPERATION,
        "apps/standalone/local_config.py": qd.OPERATION,
        "common/codec.py": qd.OPERATION,
        "MASS/mass_agent.py": qd.OPERATION,
        "VDETER/apps/stage0_endpoint.py": qd.OPERATION,
        "VDETER/tests/unit/test_multirate_phy.py": qd.TEST_DOC,
        "docs/core/traceability.toml": qd.TEST_DOC,
        "README.md": qd.TEST_DOC,
    }
    for path, expected in cases.items():
        assert qd.classify(path) == expected, path


def test_every_deployed_file_classifies_into_exactly_one_domain():
    """실제 배포 tree measure로 completeness/partition을 고정한다."""
    manifest = deploy_manifest.measure(Path(_REPO))
    assert manifest["file_count"] > 400  # 배포 규모 sanity
    split = qd.split_manifest(manifest)
    # 모든 영역 키 존재.
    assert set(split) == set(qd.DOMAINS)
    # 합 = 전체(중복·누락 없음).
    total = sum(split[d]["file_count"] for d in qd.DOMAINS)
    assert total == manifest["file_count"]
    # 각 파일은 정확히 한 영역(디스조인트).
    seen = {}
    for d in qd.DOMAINS:
        for path in split[d]["files"]:
            assert path not in seen, (path, d, seen.get(path))
            seen[path] = d
    assert len(seen) == manifest["file_count"]
    # rf·operation 핵심 영역은 비어 있지 않아야 한다(잘못된 전량 tooling 방지).
    assert split[qd.RF]["file_count"] > 100      # src/vdeter+configs+3 apps
    assert split[qd.OPERATION]["file_count"] > 5


def test_rf_domain_contains_core_and_excludes_tooling():
    manifest = deploy_manifest.measure(Path(_REPO))
    split = qd.split_manifest(manifest)
    rf = split[qd.RF]["files"]
    assert "VDETER/apps/stage7_coordinator_main.py" in rf
    assert "VDETER/src/vdeter/stage7/pi_runtime.py" in rf
    assert "apps/standalone/qualify_cmd.py" not in rf
    tooling = split[qd.TOOLING]["files"]
    assert "apps/standalone/qualify_cmd.py" in tooling
    # 시험은 어느 결속 영역에도 없다.
    for d in (qd.RF, qd.OPERATION, qd.TOOLING):
        assert not any(p.startswith("VDETER/tests/")
                       for p in split[d]["files"])


# ------------------------------------------------------------- reuse guard

_BASE = {
    "VDETER/apps/stage7_coordinator_main.py": "rf1",
    "VDETER/src/vdeter/stage7/pi_runtime.py": "rf2",
    "VDETER/configs/stage7/standalone-binding.json": "rf3",
    "apps/standalone/cli.py": "op1",
    "apps/standalone/run_cmd.py": "op2",
    "common/codec.py": "op3",
    "apps/standalone/qualify_cmd.py": "tool1",
    "VDETER/tools/stage7_rr_final_builder.py": "tool2",
    "VDETER/tests/unit/test_multirate_phy.py": "td1",
}


def test_identical_manifests_are_eligible_no_tooling_change():
    m = _manifest_from(_BASE)
    r = qd.evaluate_reuse(m, m)
    assert r["eligible"] is True
    assert r["rf_identical"] and r["operation_identical"]
    assert r["tooling_changed"] is False
    assert r["test_doc_changed"] is False
    assert r["blocking_files"] == []


def test_tooling_only_change_is_eligible():
    cur = dict(_BASE); cur["apps/standalone/qualify_cmd.py"] = "tool1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is True
    assert r["tooling_changed"] is True
    assert r["rf_identical"] and r["operation_identical"]
    assert r["blocking_files"] == []


def test_rf_change_refuses_reuse():
    cur = dict(_BASE); cur["VDETER/src/vdeter/stage7/pi_runtime.py"] = "rf2-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is False
    assert r["rf_identical"] is False
    assert any("rf:changed:VDETER/src/vdeter/stage7/pi_runtime.py" == b
               for b in r["blocking_files"])


def test_rf_file_removed_or_added_refuses_reuse():
    cur = dict(_BASE); del cur["VDETER/apps/stage7_coordinator_main.py"]
    cur["VDETER/src/vdeter/stage7/new_rf.py"] = "rfX"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is False
    assert any(b.startswith("rf:removed:") for b in r["blocking_files"])
    assert any(b.startswith("rf:added:") for b in r["blocking_files"])


def test_operation_change_refused_without_regression_allowed_with():
    cur = dict(_BASE); cur["apps/standalone/cli.py"] = "op1-NEW"
    r_no = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r_no["eligible"] is False
    assert r_no["operation_identical"] is False
    assert any("operation:changed:apps/standalone/cli.py" == b
               for b in r_no["blocking_files"])
    r_yes = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur),
                              operation_regression_passed=True)
    assert r_yes["eligible"] is True
    assert r_yes["operation_ok"] is True
    assert r_yes["blocking_files"] == []


def test_test_doc_only_change_is_eligible_and_nonbinding():
    cur = dict(_BASE); cur["VDETER/tests/unit/test_multirate_phy.py"] = "td1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is True
    assert r["test_doc_changed"] is True
    assert r["rf_identical"] and r["operation_identical"]
    assert r["blocking_files"] == []


def test_combined_tooling_and_testdoc_change_eligible_rf_untouched():
    cur = dict(_BASE)
    cur["apps/standalone/qualify_cmd.py"] = "tool1-NEW"
    cur["VDETER/tests/unit/test_multirate_phy.py"] = "td1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is True
    assert r["tooling_changed"] and r["test_doc_changed"]
    assert r["rf_identical"] and r["operation_identical"]


def test_domain_aggregate_sha_changes_only_for_changed_domain():
    cur = dict(_BASE); cur["apps/standalone/qualify_cmd.py"] = "tool1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert (r["prior_domain_sha256"][qd.RF]
            == r["current_domain_sha256"][qd.RF])
    assert (r["prior_domain_sha256"][qd.OPERATION]
            == r["current_domain_sha256"][qd.OPERATION])
    assert (r["prior_domain_sha256"][qd.TOOLING]
            != r["current_domain_sha256"][qd.TOOLING])
