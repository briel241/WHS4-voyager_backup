"""`verify_readiness_installation()` 세 층 분류 시험 (spec §6.1.1, §10).

증명하는 것
  record integrity   : 저장 manifest 손상 = 항상 RF 전 거부
  readiness-bound    : rf/operation 파일·closure allowlist(tooling) 변경/삭제/추가 = stale
  inventory-only     : 폐포 밖 시험·도구·문서의 변경/추가/삭제는 기록만, bound PASS 유지
  fail-closed        : 실행 트리 아래 새 파일은 분류와 무관하게 bound; 분류 불명 = bound
  closure digest     : bound 파일이 바뀔 때만 바뀐다 (inventory 변경에는 불변)
  qualification 불변 : `verify_installation()`은 종전처럼 전체 일치를 요구한다 (README 변경도 실패)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _entry in (_REPO, os.path.join(_REPO, "VDETER"), os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import installation_manifest as im  # noqa: E402
from apps.standalone import readiness_closure as rc  # noqa: E402
from apps.standalone import state  # noqa: E402


def make_root(tmp) -> Path:
    root = Path(tmp) / "root"
    (root / "apps" / "standalone").mkdir(parents=True)
    shutil.copy2(Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py",
                 root / "apps" / "standalone" / "qualify_cmd.py")
    (root / "apps" / "standalone" / "cli.py").write_text("CLI = 1\n")
    (root / "apps" / "standalone" / "run_cmd.py").write_text("RUN = 1\n")
    (root / "apps" / "standalone" / "hardware_draft.py").write_text("DRAFT = 1\n")  # tooling, 폐포 밖
    (root / "VDETER" / "src" / "vdeter" / "b210_system").mkdir(parents=True)
    (root / "VDETER" / "src" / "vdeter" / "b210_system" / "pi_runtime.py").write_text("RF = 1\n")
    (root / "VDETER" / "configs" / "b210_system").mkdir(parents=True)
    (root / "VDETER" / "configs" / "b210_system" / "system.toml").write_text("a=1\n")
    (root / "VDETER" / "tests" / "unit").mkdir(parents=True)
    (root / "VDETER" / "tests" / "unit" / "test_x.py").write_text("T = 1\n")
    (root / "VDETER" / "tools").mkdir(parents=True)
    (root / "VDETER" / "tools" / "tool.py").write_text("TOOL = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_y.py").write_text("Y = 1\n")
    (root / "common").mkdir()
    (root / "common" / "codec.py").write_text("C = 1\n")
    (root / "bin").mkdir()
    shutil.copy2(Path(_REPO) / "bin" / "vdeter", root / "bin" / "vdeter")
    (root / "requirements-standalone.lock").write_text("cryptography==50.0.0\n")
    (root / "README.md").write_text("# doc\n")
    (root / "VDETER" / "pyproject.toml").write_text("[project]\nname='x'\n")
    return root


def setup_installation(tmp):
    root = make_root(tmp)
    paths = state.StatePaths(root)
    paths.ensure_var()
    record = im.measure(root)
    im.write_installation(paths, record)
    return root, paths


def verify(paths, root):
    return rc.verify_readiness_installation(paths, root)


def test_unchanged_tree_passes_all_layers():
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        report = verify(paths, root)
        assert report["record_integrity"]["pass"]
        bound = report["readiness_bound"]
        assert bound["pass"] and bound["closure_sha256_recorded"] == bound["closure_sha256_current"]
        assert report["inventory_only"] == {"added": [], "removed": [], "changed": []}
        closure = rc.closure_files(im.load_installation(paths))
        # allowlist tooling(qualify_cmd·bin/vdeter·lock)과 rf/operation은 폐포 안, 시험/도구/문서는 밖.
        for inside in ("apps/standalone/qualify_cmd.py", "bin/vdeter", "requirements-standalone.lock",
                       "apps/standalone/run_cmd.py", "VDETER/src/vdeter/b210_system/pi_runtime.py",
                       "common/codec.py", "VDETER/configs/b210_system/system.toml"):
            assert inside in closure, inside
        for outside in ("VDETER/tests/unit/test_x.py", "tests/test_y.py", "VDETER/tools/tool.py",
                        "README.md", "VDETER/pyproject.toml", "apps/standalone/hardware_draft.py"):
            assert outside not in closure, outside


def test_inventory_only_changes_do_not_stale_but_are_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        before = verify(paths, root)["readiness_bound"]["closure_sha256_current"]
        (root / "README.md").write_text("# doc changed\n")
        (root / "tests" / "test_y.py").write_text("Y = 2\n")
        (root / "VDETER" / "tools" / "tool.py").unlink()
        (root / "tests" / "test_new.py").write_text("N = 1\n")
        (root / "docs").mkdir()
        (root / "docs" / "note.md").write_text("x\n")
        report = verify(paths, root)
        assert report["readiness_bound"]["pass"]
        assert report["readiness_bound"]["closure_sha256_current"] == before
        inv = report["inventory_only"]
        assert sorted(e["path"] for e in inv["changed"]) == ["README.md", "tests/test_y.py"]
        assert [e["path"] for e in inv["removed"]] == ["VDETER/tools/tool.py"]
        assert sorted(e["path"] for e in inv["added"]) == ["docs/note.md", "tests/test_new.py"]
        assert all("old" in e and "new" in e for e in inv["changed"])
        # qualification 정책은 그대로: 전체 installation 일치 검사는 이 변경을 거부한다.
        assert im.verify_installation(root, im.load_installation(paths))["pass"] is False


def test_bound_changes_are_stale_by_layer():
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        before = verify(paths, root)["readiness_bound"]["closure_sha256_current"]
        (root / "VDETER" / "src" / "vdeter" / "b210_system" / "pi_runtime.py").write_text("RF = 2\n")
        report = verify(paths, root)
        assert report["readiness_bound"]["mismatch"] == ["VDETER/src/vdeter/b210_system/pi_runtime.py"]
        assert not report["readiness_bound"]["pass"]
        assert report["readiness_bound"]["closure_sha256_current"] != before
        (root / "VDETER" / "src" / "vdeter" / "b210_system" / "pi_runtime.py").write_text("RF = 1\n")
        # allowlist tooling(qualify_cmd.py)은 tooling이지만 폐포 안 → stale.
        (root / "apps" / "standalone" / "qualify_cmd.py").write_text(
            (root / "apps" / "standalone" / "qualify_cmd.py").read_text(encoding="utf-8") + "\n# x\n",
            encoding="utf-8")
        report = verify(paths, root)
        assert "apps/standalone/qualify_cmd.py" in report["readiness_bound"]["mismatch"]
        # 폐포 밖 tooling(hardware_draft.py) 변경은 inventory-only.
        shutil.copy2(Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py",
                     root / "apps" / "standalone" / "qualify_cmd.py")
        (root / "apps" / "standalone" / "hardware_draft.py").write_text("DRAFT = 2\n")
        report = verify(paths, root)
        assert report["readiness_bound"]["pass"]
        assert [e["path"] for e in report["inventory_only"]["changed"]] == [
            "apps/standalone/hardware_draft.py"]
        # 삭제된 operation 파일은 missing.
        (root / "common" / "codec.py").unlink()
        report = verify(paths, root)
        assert report["readiness_bound"]["missing"] == ["common/codec.py"]


def test_new_files_under_execution_tree_are_bound_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        (root / "apps" / "plugin.py").write_text("P = 1\n")               # operation 기본값
        (root / "VDETER" / "src" / "vdeter" / "new.py").write_text("N = 1\n")   # rf
        (root / "apps" / "standalone" / "templates").mkdir()
        (root / "apps" / "standalone" / "templates" / "x.example").write_text("t\n")  # tooling but under apps/
        report = verify(paths, root)
        assert sorted(report["readiness_bound"]["added"]) == [
            "VDETER/src/vdeter/new.py", "apps/plugin.py", "apps/standalone/templates/x.example"]
        assert not report["readiness_bound"]["pass"]
        assert report["inventory_only"]["added"] == []


def test_unknown_classification_is_bound(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        (root / "mystery.bin").write_bytes(b"\x00")
        original = rc.domains.classify_domains

        def classify(relpath):
            if relpath == "mystery.bin":
                return ()
            return original(relpath)

        monkeypatch.setattr(rc.domains, "classify_domains", classify)
        report = verify(paths, root)
        assert report["readiness_bound"]["unknown"] == ["mystery.bin"]
        assert not report["readiness_bound"]["pass"]


def test_record_integrity_failure_is_reported_first():
    with tempfile.TemporaryDirectory() as tmp:
        root, paths = setup_installation(tmp)
        manifest = paths.var_dir / "installation" / im.INSTALLATION_MANIFEST
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            '"installation_sha256": "', '"installation_sha256": "0'), encoding="utf-8")
        report = verify(paths, root)
        assert report["record_integrity"]["pass"] is False
        assert "tampering" in report["record_integrity"]["error"]
        assert report["readiness_bound"]["pass"] is False
        assert report["current_record"] is None
