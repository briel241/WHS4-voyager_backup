"""자격 결속 3영역 분리 + evidence 재사용 판정 시험 (Phase I, 2026-09-05).

- 분류 completeness/partition: 실제 배포 manifest의 모든 파일이 정확히 한
  영역으로 분류되는지(‘unknown’ 없음, 합=전체).
- 알려진 파일의 영역 고정(coordinator/pi_runtime→rf, qualify_cmd→tooling,
  cli/run_cmd→operation, tests→test_doc, config→rf, tools→tooling).
- evaluate_reuse 가드: identical→eligible, tooling-only 변경→eligible,
  rf 변경→거부, operation 변경→회귀 없으면 거부/있으면 허용, test_doc→무영향.
"""

from __future__ import annotations

import copy
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
        "apps/standalone/qualification_domains.py": qd.TOOLING,
        "VDETER/tools/stage7_rr_final_builder.py": qd.TOOLING,
        "apps/standalone/templates/hardware.toml.example": qd.TOOLING,
        "apps/standalone/cli.py": qd.OPERATION,
        "apps/standalone/run_cmd.py": qd.OPERATION,
        "apps/standalone/local_config.py": qd.OPERATION,
        "common/codec.py": qd.OPERATION,
        "MASS/mass_agent.py": qd.OPERATION,
        "VDETER/apps/stage0_endpoint.py": qd.OPERATION,
        # qualification 시험 suite는 tooling(변경 시 bundle 재실행), 문서만 비결속.
        "VDETER/tests/unit/test_multirate_phy.py": qd.TOOLING,
        "VDETER/tests/contract/test_arch_traceability.py": qd.TOOLING,
        "VDETER/tests/integration/test_stage7_bringup.py": qd.TOOLING,
        "VDETER/tests/support/phy11.py": qd.TOOLING,
        "VDETER/tests/README.md": qd.TEST_DOC,
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


def test_rf_domain_contains_core_and_tests_are_tooling():
    manifest = deploy_manifest.measure(Path(_REPO))
    split = qd.split_manifest(manifest)
    rf = split[qd.RF]["files"]
    assert "VDETER/apps/stage7_coordinator_main.py" in rf
    assert "VDETER/src/vdeter/stage7/pi_runtime.py" in rf
    assert "apps/standalone/qualify_cmd.py" not in rf
    tooling = split[qd.TOOLING]["files"]
    # qualify_cmd(파일 단위 tooling) + qualification 시험 suite = tooling.
    assert "apps/standalone/qualify_cmd.py" in tooling
    assert any(p.startswith("VDETER/tests/") for p in tooling)
    # 시험 파일은 rf/operation 결속에는 없다(=RF 자격을 파일로 무효화 안 함).
    for d in (qd.RF, qd.OPERATION):
        assert not any(p.startswith("VDETER/tests/")
                       for p in split[d]["files"])


# ---------------------------------------------- RF-critical 심볼(함수 단위 결속)

def _qualify_source():
    return (Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py").read_text(
        encoding="utf-8")


def test_rf_critical_symbols_present_and_hashable():
    base = qd.rf_critical_symbol_baseline(
        {"apps/standalone/qualify_cmd.py": _qualify_source()})
    got = base["apps/standalone/qualify_cmd.py"]
    for symbol in qd.RF_CRITICAL_QUALIFY_SYMBOLS[
            "apps/standalone/qualify_cmd.py"]:
        assert symbol in got and len(got[symbol]) == 2


def test_rf_symbol_diff_detects_run_mode_change():
    src = _qualify_source()
    prior = qd.rf_critical_symbol_baseline(
        {"apps/standalone/qualify_cmd.py": src})
    # PHASE_RUN_MODE(=RR-P1 run mode 포함)의 rr-p1 값을 바꾼 소스.
    mutated = src.replace('"rr-p1": "GAIN_CALIBRATION"',
                          '"rr-p1": "OPERATIONAL"')
    assert mutated != src
    current = qd.rf_critical_symbol_baseline(
        {"apps/standalone/qualify_cmd.py": mutated})
    changed = qd.rf_symbol_diff(prior, current)
    assert "apps/standalone/qualify_cmd.py::PHASE_RUN_MODE" in changed


def test_rf_symbol_diff_empty_when_identical():
    base = qd.rf_critical_symbol_baseline(
        {"apps/standalone/qualify_cmd.py": _qualify_source()})
    assert qd.rf_symbol_diff(base, base) == []


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
    "VDETER/tests/unit/test_multirate_phy.py": "tool3",  # 시험 suite = tooling
    "README.md": "doc1",                                 # 문서 = test_doc
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


def test_operation_change_refuses_reuse_current_policy():
    # 현 정책: operation 변경은 회귀와 무관하게 재사용 거부(byte-identical만 허용).
    cur = dict(_BASE); cur["apps/standalone/cli.py"] = "op1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is False
    assert r["operation_identical"] is False
    assert any("operation:changed:apps/standalone/cli.py" == b
               for b in r["blocking_files"])


_SYM = {"apps/standalone/qualify_cmd.py": {
    "LADDER_PHASES": ["a", "b"], "PHASE_RUN_MODE": ["c", "d"],
    "PHASE_EXPECTED_TCS": ["e", "f"], "GAIN_CONSUMING_PHASES": ["g", "h"],
    "default_phase_runner": ["i", "j"]}}


def test_rf_symbol_change_refuses_even_when_files_identical():
    # 파일(manifest)은 동일하지만 RF-critical 심볼(run mode 등)이 바뀌면 거부.
    m = _manifest_from(_BASE)
    cur_sym = copy.deepcopy(_SYM)
    cur_sym["apps/standalone/qualify_cmd.py"]["PHASE_RUN_MODE"] = ["c", "d-NEW"]
    r = qd.evaluate_reuse(m, m, prior_rf_symbols=_SYM,
                          current_rf_symbols=cur_sym)
    assert r["eligible"] is False
    assert r["rf_symbols_identical"] is False
    assert ("apps/standalone/qualify_cmd.py::PHASE_RUN_MODE"
            in r["rf_symbols_changed"])
    assert any(b.startswith("rf_symbol:changed:") for b in r["blocking_files"])


def test_tooling_change_with_identical_rf_symbols_is_eligible():
    cur = dict(_BASE); cur["apps/standalone/qualify_cmd.py"] = "tool1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur),
                          prior_rf_symbols=_SYM, current_rf_symbols=_SYM)
    assert r["eligible"] is True
    assert r["tooling_changed"] and r["rf_symbols_identical"]


def test_doc_only_change_is_eligible_and_nonbinding():
    # 문서(.md)만 변경 → 자격 무영향(test_doc), tooling 변경 아님.
    cur = dict(_BASE); cur["README.md"] = "doc1-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is True
    assert r["test_doc_changed"] is True
    assert r["tooling_changed"] is False
    assert r["rf_identical"] and r["operation_identical"]
    assert r["blocking_files"] == []


def test_qualification_test_file_change_is_tooling_not_testdoc():
    # 시험 파일 변경 → tooling(=software bundle 재실행 필요), RF 재실행 불요.
    cur = dict(_BASE)
    cur["VDETER/tests/unit/test_multirate_phy.py"] = "tool3-NEW"
    r = qd.evaluate_reuse(_manifest_from(_BASE), _manifest_from(cur))
    assert r["eligible"] is True
    assert r["tooling_changed"] is True
    assert r["test_doc_changed"] is False
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
