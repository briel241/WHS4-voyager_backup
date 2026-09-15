"""qualification reference 발급 도구 계약시험.

핵심은 부정시험이다 — 3중 해시 중 하나만 변조해도 발급이 반드시 중단돼야
한다. 발급이 통과하면 그 자체가 "세 출처가 같다"는 증언이 되기 때문이다.
"""

from __future__ import annotations

import os
import sys

import pytest

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import stage7_issue_qualification_reference as issue  # noqa: E402


def test_closure_covers_config_allowlist_and_excludes_secrets():
    paths, _ = issue.strict_file_list()
    for config in issue.CONFIG_ALLOWLIST:
        assert config in paths
    assert not [p for p in paths if "trust" in p or p.endswith(".pem")], \
        "trust/개인키가 결속 목록에 섞이면 안 된다"
    assert issue.FUNCTION_BOUND not in paths, "coordinator는 함수 단위 결속이다"


def test_closure_includes_runtime_package_init():
    """`vdeter.b210_system.support` import 는 `stage7/__init__.py` 를 실행한다."""

    paths, _ = issue.strict_file_list()
    assert "VDETER/src/vdeter/b210_system/__init__.py" in paths


def test_orchestration_layer_is_a_boundary_not_a_silent_drop():
    _, boundary = issue.strict_file_list()
    assert "apps/stage7_trace.py" in boundary
    paths, _ = issue.strict_file_list()
    assert not [p for p in paths if p.startswith("apps/")], \
        "orchestration 계층은 결속하지 않는다"


def test_unresolved_internal_import_aborts(monkeypatch):
    original = issue._resolve

    def _blind(module):
        if module.startswith("vdeter."):
            return None
        return original(module)

    monkeypatch.setattr(issue, "_resolve", _blind)
    with pytest.raises(issue.IssueError, match="해석하지 못했다"):
        issue.strict_file_list()


def test_ambiguous_import_aborts(monkeypatch):
    monkeypatch.setattr(os.path, "isfile", lambda _p: True)
    with pytest.raises(issue.IssueError, match="모호하다"):
        issue._resolve("vdeter.b210_system.support")


@pytest.mark.parametrize("tampered", ["local", "pi", "deploy_record"])
def test_triple_check_fails_when_any_source_is_tampered(tampered):
    paths = ["VDETER/src/vdeter/b210_system/support.py"]
    good = {paths[0]: "a" * 64}
    local, remote, record = dict(good), dict(good), dict(good)
    {"local": local, "pi": remote, "deploy_record": record}[tampered][paths[0]] = "b" * 64
    with pytest.raises(issue.IssueError, match="3중 대조 불일치"):
        issue.triple_check(paths, local, remote, record)


def test_triple_check_passes_when_all_three_agree():
    paths = ["VDETER/src/vdeter/b210_system/support.py"]
    same = {paths[0]: "c" * 64}
    report = issue.triple_check(paths, dict(same), dict(same), dict(same))
    assert report["pass"] is True
    assert report["sources"] == ["deploy_record", "pi_live", "local_worktree"]


def test_publish_refuses_to_overwrite(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(issue.IssueError, match="덮어쓰지 않는다"):
        issue._publish(str(staging), str(destination))


def test_coordinator_binding_covers_more_than_function_bodies():
    binding = issue.coordinator_binding(None)
    for key in ("qualified_functions", "top_level_constants",
                "module_imports_sha256", "rr_phase_dispatch",
                "phase_f_file_sha256", "phase_g_file_sha256", "delta"):
        assert key in binding, key
    assert binding["qualified_functions"], "함수 결속이 비어 있다"
    assert binding["rr_phase_dispatch"].get("rr-p1"), "rr-p1 dispatch 를 못 찾았다"
