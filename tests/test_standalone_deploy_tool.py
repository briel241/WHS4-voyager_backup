"""Phase I V2 — standalone 배포 패키지 도구 계약 시험 (SSH 없음).

증명하는 것 (b안 — 사용자 지정 순서 3·4)
  패키지 내용 : 지정 commit의 추적 파일(git archive 원본) + 미커밋 binding +
                자기 기술 manifest. results/·VDETER/results/·var/·identity/·
                hardware.toml·.git 제외.
  전제 검사   : HEAD≠지정 commit 거부, binding head≠HEAD 거부.
  manifest    : 멤버 바이트 SHA와 일치.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()


def _load_tool():
    path = Path(_REPO) / "VDETER" / "tools" / "deploy_standalone_to_pi.py"
    spec = importlib.util.spec_from_file_location("deploy_standalone_test",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _head() -> str:
    import subprocess
    return subprocess.run(["git", "-C", _REPO, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _write_binding(tmp, head) -> Path:
    path = Path(tmp) / "standalone-binding.json"
    path.write_text(json.dumps({
        "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
        "source_head": head, "source_worktree_dirty": False,
        "baseline_version": "0.9.23", "baseline_sha256": "2a" * 32,
        "frozen_files": {"src/vdeter/config/models.py": "ab" * 32},
    }), encoding="utf-8")
    return path


def test_build_package_contents_and_exclusions():
    tool = _load_tool()
    head = _head()
    with tempfile.TemporaryDirectory() as tmp:
        binding_path = _write_binding(tmp, head)
        archive, manifest = tool.build_package(head, str(binding_path))
        names = []
        digests = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                names.append(member.name)
                digests[member.name] = hashlib.sha256(
                    tar.extractfile(member).read()).hexdigest()
        assert "apps/standalone/cli.py" in names
        assert "requirements-standalone.lock" in names
        assert tool.BINDING_RELPATH in names
        assert tool.PACKAGE_MANIFEST_NAME in names
        assert "VDETER/apps/stage7_coordinator_main.py" in names
        assert not any(n.startswith(("results/", "VDETER/results/", "var/",
                                     "identity/", ".git/")) for n in names)
        assert "hardware.toml" not in names
        # manifest는 멤버 바이트와 일치하고 head를 자기 기술한다.
        assert manifest["source_head"] == head
        assert manifest["file_count"] == len(names) - 1  # manifest 자신 제외
        for entry in manifest["files"]:
            assert digests[entry["path"]] == entry["sha256"], entry["path"]
        # binding은 미커밋 산출물이지만 패키지에 들어간다.
        assert digests[tool.BINDING_RELPATH] == hashlib.sha256(
            binding_path.read_bytes()).hexdigest()


def test_transfer_update_flag_semantics():
    """--update 없이 기존 root면 거부, --update인데 root 부재면 거부."""
    tool = _load_tool()

    class Done:
        def __init__(self, stdout=""):
            self.returncode, self.stdout, self.stderr = 0, stdout, b""

    def runner_exists(argv, **kwargs):
        return Done("exists\n")

    def runner_absent(argv, **kwargs):
        return Done("absent\n")

    for runner, update, needle in ((runner_exists, False, "--update"),
                                   (runner_absent, True, "없습니다")):
        try:
            tool.transfer_and_verify(b"", host="h", remote_root="~/x",
                                     runner=runner, update=update)
            raise AssertionError("거부되지 않았다")
        except tool.DeployError as exc:
            assert needle in str(exc)


def test_preconditions_reject_wrong_commit_and_binding_head():
    tool = _load_tool()
    head = _head()
    try:
        tool.check_preconditions("0000000")
        raise AssertionError("HEAD 불일치가 거부되지 않았다")
    except tool.DeployError as exc:
        assert "지정 commit" in str(exc)

    # binding head 불일치 — 실제 binding 경로를 임시로 바꿔치기하지 않고
    # 검사 함수의 판정만 확인한다 (repo 파일 무수정).
    with tempfile.TemporaryDirectory() as tmp:
        wrong = _write_binding(tmp, "e" * 40)
        original = tool.BINDING_RELPATH
        tool.BINDING_RELPATH = os.path.relpath(str(wrong), _REPO).replace(
            os.sep, "/")
        try:
            tool.check_preconditions(head[:7])
            raise AssertionError("binding head 불일치가 거부되지 않았다")
        except tool.DeployError as exc:
            assert "source_head" in str(exc) or "추적 파일" in str(exc)
        finally:
            tool.BINDING_RELPATH = original


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase I V2 standalone deploy tool 계약")
