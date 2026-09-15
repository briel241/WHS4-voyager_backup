"""Git 없는 환경 bootstrap 시험 — 추적 파일만 복사한 임시 설치본.

조건: `.git` 없음, standalone-binding.json 없음, var/·identity/·hardware.toml
없음. `run_setup(--no-apt 상당)` → 성공, installation/domain manifest 생성,
재실행 시 identity·hardware.toml·digest 불변, run/qualify의 preflight 이전
준비 경로가 Git 없이 동작하며 git 명령이 한 번도 호출되지 않는다.

venv 생성·pip 설치는 실행기 주입으로 생략한다(개발 PC에는 POSIX venv 배치가
없다) — 실제 venv 경로는 Pi clean-clone 시험이 검증한다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

# `vdeter` 패키지를 **영속 저장소 트리**에서 먼저 적재한다. 아래 시험은 임시
# 복사본(setup 시 sys.path에 삽입됨)에서 identity를 만드는데, 그 복사본이
# 삭제된 뒤 다른 시험이 `vdeter.stage7`를 처음 import하면 캐시된 패키지
# 경로가 사라져 ModuleNotFoundError가 난다 (Pi clean-clone 시험에서 실측).
import vdeter.link.tbb.crypto  # noqa: E402,F401
import vdeter.stage7.canonical_json  # noqa: E402,F401
import vdeter.stage7.configuration  # noqa: E402,F401

from standalone_v2_fixtures import (  # noqa: E402
    ENV_MANIFEST,
    Done,
    fake_preflight,
    issue_v2_reference,
    write_hardware_toml,
)
from apps.standalone import (  # noqa: E402
    identity,
    installation_manifest as im,
    provenance,
    qualify_cmd,
    run_cmd,
    setup_cmd,
    state,
)


def _tracked_files() -> list:
    done = subprocess.run(["git", "-C", _REPO, "ls-files", "-z"],
                          capture_output=True, timeout=60)
    if done.returncode == 0 and done.stdout:
        return [p for p in done.stdout.decode("utf-8").split("\0") if p]
    # git 없는 개발 환경 — installation scan 결과를 추적 파일 대용으로 쓴다.
    return im.scan_files(_REPO)


def copy_tracked_tree(tmp) -> Path:
    root = Path(tmp) / "nogit"
    for rel in _tracked_files():
        src = Path(_REPO) / rel
        if not src.is_file():
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    assert not (root / ".git").exists()
    assert not (root / "VDETER" / "configs" / "stage7"
                / "standalone-binding.json").exists()
    assert not (root / "var").exists() and not (root / "identity").exists()
    assert not (root / "hardware.toml").exists()
    return root


class GitForbidden(Exception):
    pass


def _forbid_git(monkeypatch_target=subprocess):
    """subprocess.run/Popen가 git을 호출하면 즉시 실패."""
    original_run, original_popen = subprocess.run, subprocess.Popen

    def guard(argv, *a, **k):
        if argv and os.path.basename(str(argv[0])).startswith("git"):
            raise GitForbidden(argv)
        return original_run(argv, *a, **k)

    class GuardPopen(original_popen):
        def __init__(self, argv, *a, **k):
            if argv and os.path.basename(str(argv[0])).startswith("git"):
                raise GitForbidden(argv)
            super().__init__(argv, *a, **k)

    subprocess.run, subprocess.Popen = guard, GuardPopen
    return lambda: setattr(subprocess, "run", original_run) or setattr(
        subprocess, "Popen", original_popen)


def _setup_runner(argv, **kwargs):
    if argv[:2] == ["dpkg", "-s"]:
        return Done(0)
    return Done(0)


def test_setup_without_git_creates_manifests_identity_and_draft_idempotently():
    with tempfile.TemporaryDirectory() as tmp:
        root = copy_tracked_tree(tmp)
        paths = state.StatePaths(root)
        home = Path(tmp) / "home"
        restore = _forbid_git()
        try:
            report = setup_cmd.run_setup(
                paths, runner=_setup_runner, usb_probe=lambda: {},
                apt_install=False, make_venv=False, home=home)
        finally:
            restore()
        assert report["pass"], report
        inst = report["steps"]["installation"]
        directory = im.installation_dir(paths)
        for name in ("installation-manifest.json", "rf-manifest.json",
                     "operation-manifest.json", "tooling-manifest.json"):
            assert (directory / name).is_file()
        assert inst["qualification_invalidated"] is False
        assert (root / "hardware.toml").is_file()
        assert (root / "identity" / "private").is_dir()
        fingerprint = report["steps"]["identity"]["fingerprint"]
        assert report["steps"]["user_launcher"]["installed"] is True
        # Git 정보는 없고(참고 필드 null), 실행은 막히지 않았다.
        assert provenance.build_provenance(root)["git"] is None
        assert not hasattr(provenance, "read_git_provenance")  # .git 관측 없음
        draft_sha = im.sha256_file(root / "hardware.toml")

        # 재실행: identity·hardware.toml·digest 불변, manifest 재사용.
        restore = _forbid_git()
        try:
            again = setup_cmd.run_setup(
                paths, runner=_setup_runner, usb_probe=lambda: {},
                apt_install=False, make_venv=False, home=home)
        finally:
            restore()
        assert again["steps"]["identity"]["created"] is False
        assert again["steps"]["identity"]["fingerprint"] == fingerprint
        assert again["steps"]["hardware_draft"]["created"] is False
        assert im.sha256_file(root / "hardware.toml") == draft_sha
        assert again["steps"]["installation"]["installation_sha256"] == inst["installation_sha256"]
        assert again["steps"]["installation"]["reused"] is True
        assert again["steps"]["installation"]["qualification_invalidated"] is False
        assert again["steps"]["user_launcher"]["status"] == "unchanged"
        # 설치본 record는 실제 파일 재측정으로 검증된다.
        loaded = im.load_installation(paths)
        assert im.verify_installation(root, loaded)["pass"]


def test_prepare_paths_of_run_and_qualify_work_without_git():
    with tempfile.TemporaryDirectory() as tmp:
        root = copy_tracked_tree(tmp)
        paths = state.StatePaths(root)
        setup_cmd.run_setup(paths, runner=_setup_runner, usb_probe=lambda: {},
                            apt_install=False, make_venv=False,
                            install_launcher=False)
        write_hardware_toml(root / "hardware.toml")
        record = im.load_installation(paths)
        fingerprint = identity.check_identity_consistency(paths)["fingerprint"]

        restore = _forbid_git()
        try:
            # qualify 준비 경로(RF 전) — Git·Markdown·binding 없이 통과.
            context = qualify_cmd.prepare_qualification(
                paths, repo_root=root, preflight_fn=fake_preflight,
                environment_manifest=dict(ENV_MANIFEST))
            assert context["installation"]["installation_sha256"] == record["installation_sha256"]
            assert context["install_check"]["pass"]
            assert context["provenance"]["git"] is None
            assert context["provenance"]["stage7_baseline"]["version"] == "0.9.23"

            # UNQUALIFIED 상태의 run은 RF 전 거부(exit 4) — Git 없음이 원인이 아니다.
            try:
                run_cmd.prepare(paths, repo_root=root,
                                preflight_fn=fake_preflight,
                                environment_manifest=dict(ENV_MANIFEST))
                raise AssertionError("UNQUALIFIED가 거부되지 않았다")
            except run_cmd.RunRefusal as exc:
                assert exc.code == run_cmd.EXIT_UNQUALIFIED
                assert "UNQUALIFIED" in str(exc)

            # v2 reference 발급(RF 없이 시험용) 후 run 준비 경로 통과.
            reference = issue_v2_reference(record, fingerprint,
                                           provenance=context["provenance"])
            qualify_cmd.write_reference(paths.qualification_candidate, reference)
            qualify_cmd.promote_candidate(paths, release_id="qual-nogit")
            ctx = run_cmd.prepare(paths, repo_root=root,
                                  preflight_fn=fake_preflight,
                                  environment_manifest=dict(ENV_MANIFEST))
        finally:
            restore()
        assert ctx["reference"]["schema_version"] == 2
        assert ctx["installation_sha256"] == record["installation_sha256"]
        assert "qualified_source_commit" not in ctx["reference"]
        assert ctx["reference"]["provenance"]["git"] is None
        # config 생성(coordinator 계약 파일)도 Git 없이 — git 필드는 null.
        from apps.standalone import local_config
        generated = local_config.generate_run_configs(
            repo_root=root, run_root=Path(tmp) / "r", run_id="r",
            rr_phase="rr-p2-17", hardware=ctx["hardware"],
            installation=ctx["installation"], installation_verified=True,
            provenance=ctx["provenance"], run_mode="OPERATIONAL",
            gain_input=run_cmd.gain_input_from_reference(ctx["reference"]),
            environment_manifest=dict(ENV_MANIFEST))
        git_state = json.loads((Path(generated["configs_dir"])
                                / "git_state.json").read_text("utf-8"))
        assert git_state["git_commit"] is None and git_state["git_dirty"] is None
        assert git_state["installation_sha256"] == record["installation_sha256"]
        assert git_state["baseline_sha256"] == provenance.STAGE7_BASELINE_PIN["sha256"]
        assert (Path(generated["configs_dir"]) / "installation-state.json").is_file()


def test_manifest_missing_or_tampered_refuses_run():
    with tempfile.TemporaryDirectory() as tmp:
        root = copy_tracked_tree(tmp)
        paths = state.StatePaths(root)
        setup_cmd.run_setup(paths, runner=_setup_runner, usb_probe=lambda: {},
                            apt_install=False, make_venv=False,
                            install_launcher=False)
        write_hardware_toml(root / "hardware.toml")
        record = im.load_installation(paths)
        fingerprint = identity.check_identity_consistency(paths)["fingerprint"]
        reference = issue_v2_reference(record, fingerprint)
        qualify_cmd.write_reference(paths.qualification_candidate, reference)
        qualify_cmd.promote_candidate(paths, release_id="qual-nogit")
        assert run_cmd.prepare(paths, repo_root=root, preflight_fn=fake_preflight,
                               environment_manifest=dict(ENV_MANIFEST))
        # 변조 → 거부.
        path = im.installation_dir(paths) / "installation-manifest.json"
        payload = json.loads(path.read_text("utf-8"))
        payload["files"]["apps/standalone/run_cmd.py"]["sha256"] = "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            run_cmd.prepare(paths, repo_root=root, preflight_fn=fake_preflight,
                            environment_manifest=dict(ENV_MANIFEST))
            raise AssertionError("변조 manifest로 실행됐다")
        except run_cmd.RunRefusal as exc:
            assert exc.code == run_cmd.EXIT_UNQUALIFIED and "tampered" in str(exc)
        # 누락 → 거부.
        path.unlink()
        try:
            run_cmd.prepare(paths, repo_root=root, preflight_fn=fake_preflight,
                            environment_manifest=dict(ENV_MANIFEST))
            raise AssertionError("manifest 누락으로 실행됐다")
        except run_cmd.RunRefusal as exc:
            assert exc.code == run_cmd.EXIT_UNQUALIFIED and "setup" in str(exc)
