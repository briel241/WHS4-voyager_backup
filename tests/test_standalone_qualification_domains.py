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
        "VDETER/apps/stage7_scenario_controller.py": qd.RF,   # 주영역(rf+operation)
        "VDETER/src/vdeter/stage7/pi_runtime.py": qd.RF,
        "VDETER/src/vdeter/phy/resampling.py": qd.RF,
        "VDETER/configs/stage7/system.toml": qd.RF,
        "VDETER/configs/stage7/endpoints/mass-a.toml": qd.RF,
        "VDETER/configs/stage7/profiles/data-profile.toml": qd.RF,
        "apps/standalone/qualify_cmd.py": qd.TOOLING,
        "apps/standalone/qualification_domains.py": qd.TOOLING,
        "apps/standalone/setup_cmd.py": qd.TOOLING,
        "VDETER/tools/stage7_rr_final_builder.py": qd.TOOLING,
        "apps/standalone/templates/hardware.toml.example": qd.TOOLING,
        "tools/check_docs.py": qd.TOOLING,
        "apps/standalone/cli.py": qd.OPERATION,
        "apps/standalone/run_cmd.py": qd.OPERATION,
        "apps/standalone/local_session.py": qd.OPERATION,
        "apps/standalone/local_preflight.py": qd.OPERATION,
        "apps/standalone/identity.py": qd.OPERATION,
        "apps/standalone/local_config.py": qd.RF,   # 주영역 rf (rf+operation)
        "apps/stage7_mass_roc_controller.py": qd.OPERATION,
        "apps/stage7_service_layer.py": qd.OPERATION,
        "common/codec.py": qd.OPERATION,
        "MASS/mass_agent.py": qd.OPERATION,
        "VDETER/apps/stage0_endpoint.py": qd.OPERATION,
        "unknown/new_module.py": qd.OPERATION,      # 미분류 → operation
        "VDETER/tests/unit/test_multirate_phy.py": qd.TOOLING,
        "VDETER/tests/contract/test_arch_traceability.py": qd.TOOLING,
        "VDETER/tests/integration/test_stage7_bringup.py": qd.TOOLING,
        "VDETER/tests/support/phy11.py": qd.TOOLING,
        "tests/test_standalone_qualify.py": qd.TOOLING,
        "VDETER/tests/README.md": qd.TEST_DOC,
        "docs/core/traceability.toml": qd.TEST_DOC,
        "README.md": qd.TEST_DOC,
        ".gitignore": qd.TEST_DOC,
        "docs/구상도.jpg": qd.TEST_DOC,
    }
    for path, expected in cases.items():
        assert qd.classify(path) == expected, path


def test_classify_domains_multi_membership_v2():
    assert qd.classify_domains("VDETER/apps/stage7_scenario_controller.py") == (
        qd.RF, qd.OPERATION)
    assert qd.classify_domains("apps/standalone/local_config.py") == (
        qd.RF, qd.OPERATION)
    assert qd.classify_domains("apps/standalone/installation_manifest.py") == (
        qd.TOOLING, qd.OPERATION)
    assert qd.classify_domains("apps/stage7_profile.py") == (qd.TOOLING, qd.OPERATION)
    assert qd.classify_domains("requirements-standalone.lock") == (
        qd.TOOLING, qd.OPERATION)
    assert qd.classify_domains("bin/vdeter") == (qd.TOOLING, qd.OPERATION)
    assert qd.classify_domains("README.md") == (qd.NON_BINDING,)
    assert qd.classify_domains("VDETER/tests/unit/test_x.py") == (qd.TOOLING,)
    assert qd.classify_domains("some/unknown.py") == (qd.OPERATION,)
    assert qd.classify_domains("VDETER/src/vdeter/mac/interface.py") == (qd.RF,)


def _files_v2(mapping):
    """{path: sha} → v2 files map (domains는 분류기로)."""
    return {p: {"sha256": s, "domains": list(qd.classify_domains(p))}
            for p, s in mapping.items()}


_BASE_V2 = {
    "VDETER/apps/stage7_coordinator_main.py": "rf1",
    "VDETER/src/vdeter/stage7/pi_runtime.py": "rf2",
    "VDETER/apps/stage7_scenario_controller.py": "x1",   # rf+operation
    "apps/standalone/local_config.py": "x2",              # rf+operation
    "apps/standalone/cli.py": "op1",
    "apps/standalone/run_cmd.py": "op2",
    "common/codec.py": "op3",
    "apps/standalone/qualify_cmd.py": "tool1",
    "VDETER/tools/stage7_rr_final_builder.py": "tool2",
    "VDETER/tests/unit/test_multirate_phy.py": "tool3",
    "tests/test_standalone_qualify.py": "tool4",
    "README.md": "doc1",
    "docs/사용법.md": "doc2",
}


def test_evaluate_reuse_v2_rules():
    base = _files_v2(_BASE_V2)
    same = qd.evaluate_reuse_v2(base, base)
    assert same["eligible"] and not same["tooling_changed"]
    # RF 파일 변경 → RF 재자격 필요(재사용 거부).
    cur = dict(_BASE_V2); cur["VDETER/src/vdeter/stage7/pi_runtime.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert not r["eligible"] and "rf:changed:VDETER/src/vdeter/stage7/pi_runtime.py" in r["blocking_files"]
    # 교차영향 파일(scenario controller) 변경 → rf·operation 둘 다 차단.
    cur = dict(_BASE_V2); cur["VDETER/apps/stage7_scenario_controller.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert not r["rf_identical"] and not r["operation_identical"]
    # operation 변경 → 자동 재사용 거부(현 정책).
    cur = dict(_BASE_V2); cur["apps/standalone/cli.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert not r["eligible"] and "operation:changed:apps/standalone/cli.py" in r["blocking_files"]
    # tooling 변경 → software bundle 재실행 요구(eligible, tooling_changed).
    cur = dict(_BASE_V2); cur["apps/standalone/qualify_cmd.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert r["eligible"] and r["tooling_changed"] and r["rf_identical"]
    # qualification 시험 변경 → tooling 변경(비결속 아님).
    cur = dict(_BASE_V2); cur["tests/test_standalone_qualify.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert r["eligible"] and r["tooling_changed"] and not r["non_binding_changed"]
    # Markdown 변경 → 자격 영향 없음.
    cur = dict(_BASE_V2); cur["README.md"] = "NEW"; cur["docs/사용법.md"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert r["eligible"] and not r["tooling_changed"] and r["non_binding_changed"]
    assert r["blocking_files"] == []
    # 미분류 실행 파일 추가 → operation 추가 → 거부(fail-safe).
    cur = dict(_BASE_V2); cur["plugins/extra.py"] = "NEW"
    r = qd.evaluate_reuse_v2(base, _files_v2(cur))
    assert not r["eligible"] and "operation:added:plugins/extra.py" in r["blocking_files"]
    # RF-critical 심볼 변경 → 파일 동일해도 거부.
    sym = {"apps/standalone/qualify_cmd.py": {
        "LADDER_PHASES": ["a", "b"], "PHASE_RUN_MODE": ["c", "d"],
        "PHASE_EXPECTED_TCS": ["e", "f"], "GAIN_CONSUMING_PHASES": ["g", "h"],
        "default_phase_runner": ["i", "j"]}}
    mutated = copy.deepcopy(sym)
    mutated["apps/standalone/qualify_cmd.py"]["PHASE_RUN_MODE"] = ["c", "NEW"]
    r = qd.evaluate_reuse_v2(base, base, prior_rf_symbols=sym,
                             current_rf_symbols=mutated)
    assert not r["eligible"] and "apps/standalone/qualify_cmd.py::PHASE_RUN_MODE" in r["rf_symbols_changed"]


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
    assert "apps/standalone/qualify_cmd.py" in tooling
    assert any(p.startswith("VDETER/tests/") for p in tooling)
    for d in (qd.RF, qd.OPERATION):
        assert not any(p.startswith("VDETER/tests/")
                       for p in split[d]["files"])
    # v2 실측: 결속 파일은 비결속과 겹치지 않고 교차영향 파일은 두 영역에.
    from apps.standalone import installation_manifest as im
    record = im.measure(Path(_REPO))
    files = record["files"]
    assert set(files["VDETER/apps/stage7_scenario_controller.py"]["domains"]) == {
        qd.RF, qd.OPERATION}
    assert set(files["apps/standalone/local_config.py"]["domains"]) == {
        qd.RF, qd.OPERATION}
    for path, entry in files.items():
        if qd.NON_BINDING in entry["domains"]:
            assert entry["domains"] == [qd.NON_BINDING], path
        else:
            assert set(entry["domains"]) <= set(qd.BOUND_DOMAINS), path


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
