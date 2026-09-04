"""Phase I W4 — setup·identity·deploy manifest·binding 도구 계약 시험.

증명하는 것 (apps/standalone/README.md §1·§2·§5, 결정 10·11)
  deploy manifest : 실측→검증 왕복, 변조·누락·미등재 추가분 전부 계상
                    (prefix·제외 규칙은 host 배포 도구와 단일 출처)
  identity        : 부재 시에만 생성(멱등), 부분 존재 거부, --new-identity는
                    공개 기록만 archive·개인키 폐기·fingerprint 교체
  setup           : apt 계획/설치·venv·wrapper·초안·manifest·환경 기록의
                    멱등 orchestration (실행기는 전부 주입 — apt/venv 미실행)
  binding 도구    : dirty 거부, 40-hex head, baseline 버전 파싱, frozen
                    정규화(LF) SHA — 산출물이 load_standalone_binding을 통과
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
# identity 생성이 vdeter.link.tbb.crypto를 쓴다 — 시험 순서와 무관하게 보장.
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (
    deploy_manifest,
    identity,
    local_config,
    setup_cmd,
    state,
)


class Done:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout


def make_mini_root(tmp) -> Path:
    """host 배포 규칙이 적재 가능한 최소 배포 tree."""
    root = Path(tmp) / "root"
    (root / "VDETER" / "tools").mkdir(parents=True)
    shutil.copy2(Path(_REPO) / "VDETER" / "tools" / "deploy_stage7_to_pi.py",
                 root / "VDETER" / "tools" / "deploy_stage7_to_pi.py")
    (root / "VDETER" / "src" / "vdeter").mkdir(parents=True)
    (root / "VDETER" / "src" / "vdeter" / "a.py").write_text("A = 1\n")
    (root / "apps").mkdir()
    (root / "apps" / "x.py").write_text("X = 1\n")
    (root / "apps" / "__pycache__").mkdir()
    (root / "apps" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    (root / "common").mkdir()
    (root / "common" / "y.py").write_text("Y = 1\n")
    (root / "requirements-standalone.lock").write_text("cryptography==50.0.0\n")
    return root


# ---------------------------------------------------------------- manifest

def test_deploy_manifest_measure_verify_tamper_untracked():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        written = deploy_manifest.write_manifest(
            root, root / "var" / "deploy-manifest.json")
        manifest = written["manifest"]
        paths = {entry["path"] for entry in manifest["files"]}
        assert "apps/x.py" in paths and "common/y.py" in paths
        assert "requirements-standalone.lock" in paths
        assert not any("__pycache__" in p for p in paths)  # 제외 규칙 공유
        assert len(written["sha256"]) == 64

        assert deploy_manifest.verify(root, manifest)["pass"]

        # 변조 → mismatch, 삭제 → missing, 추가 → untracked. 전부 fail.
        (root / "apps" / "x.py").write_text("X = 2\n")
        report = deploy_manifest.verify(root, manifest)
        assert not report["pass"] and report["mismatch"] == ["apps/x.py"]
        (root / "apps" / "x.py").unlink()
        report = deploy_manifest.verify(root, manifest)
        assert report["missing"] == ["apps/x.py"]
        (root / "apps" / "x.py").write_text("X = 1\n")
        (root / "apps" / "new.py").write_text("N = 1\n")
        report = deploy_manifest.verify(root, manifest)
        assert report["untracked"] == ["apps/new.py"]

        loaded, sha = deploy_manifest.load_manifest(
            root / "var" / "deploy-manifest.json")
        assert sha == written["sha256"]
        assert loaded["file_count"] == manifest["file_count"]


# ---------------------------------------------------------------- identity

def test_identity_lifecycle_idempotent_and_new_identity():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)  # vdeter import용 — 파일 경로는 아래 그대로
        paths_files = identity.identity_paths(paths)

        first = identity.ensure_identity(paths)
        assert first["created"] is True
        fingerprint = first["fingerprint"]
        assert len(fingerprint) == 12  # 6-byte fingerprint (기존 도구와 동일)
        for path in paths_files.values():
            assert path.is_file(), path

        second = identity.ensure_identity(paths)
        assert second["created"] is False
        assert second["fingerprint"] == fingerprint

        # 부분 존재는 자동 복구하지 않는다 — 명시적 재생성 안내.
        paths_files["certificate"].unlink()
        try:
            identity.ensure_identity(paths)
            raise AssertionError("부분 존재가 거부되지 않았다")
        except identity.IdentityError as exc:
            assert "--new-identity" in str(exc)

        # new_identity: 공개 기록 archive + 개인키 폐기 + fingerprint 교체.
        old_key_bytes = paths_files["key"].read_bytes()
        renewed = identity.new_identity(paths)
        assert renewed["created"] is True
        assert renewed["fingerprint"] != fingerprint
        assert renewed["destroyed_private_key"] is True
        archive_dir = Path(renewed["archived_to"])
        archived = sorted(p.name for p in archive_dir.iterdir())
        assert identity.KEY_NAME not in archived  # 개인키는 보관하지 않는다
        assert identity.STORE_NAME in archived
        assert identity.FINGERPRINT_NAME in archived
        assert paths_files["key"].read_bytes() != old_key_bytes
        manifest = identity.load_identity_manifest(paths)
        assert manifest["certificate_fingerprint"] == renewed["fingerprint"]
        # 계보 기록: 이전 fingerprint가 새 manifest에 남는다 (공개 정보).
        assert manifest["previous_fingerprint"] == fingerprint


# ---------------------------------------------------------------- setup

def test_run_setup_orchestration_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        commands = []

        def runner(argv, **kwargs):
            commands.append(list(argv))
            if argv[:2] == ["dpkg", "-s"]:
                return Done(0 if argv[2] == "gnuradio" else 1)
            return Done(0)

        observed = {"34D03DD": {"usb_path": "3-1", "speed_mbps": 5000}}
        report = setup_cmd.run_setup(paths, runner=runner,
                                     usb_probe=lambda: observed)
        assert report["pass"], report

        # apt: 설치된 gnuradio 제외 나머지만 설치 명령 (python3-venv 포함 —
        # W4 보완 3; python3-uhd는 runtime 불필요라 목록에 없다).
        assert report["steps"]["apt_plan"]["missing"] == [
            "uhd-host", "python3-numpy", "python3-venv"]
        assert "python3-uhd" not in setup_cmd.REQUIRED_APT_PACKAGES
        install = next(c for c in commands if c[:3] == ["sudo", "apt-get",
                                                        "install"])
        assert install[-3:] == ["uhd-host", "python3-numpy", "python3-venv"]
        # venv 생성 + lock 설치 명령이 정확히 구성됐다.
        venv_cmd = next(c for c in commands if c[1:3] == ["-m", "venv"])
        assert "--system-site-packages" in venv_cmd
        pip_cmd = next(c for c in commands if "pip" in c and "install" in c)
        assert pip_cmd[-1].endswith("requirements-standalone.lock")

        # wrapper·manifest·환경 기록.
        wrapper = Path(report["steps"]["wrapper"]["path"])
        assert "cli.py" in wrapper.read_text(encoding="utf-8")
        assert (paths.var_dir / "deploy-manifest.json").is_file()
        env_record = json.loads((paths.var_dir
                                 / "setup-environment.json").read_text())
        assert set(env_record["versions"]) == {
            "numpy_version", "cryptography_version",
            "uhd_version", "gnuradio_version"}

        # 초안: 역할 자동 배정 없음 + 관측 serial은 주석 참고용.
        draft = (root / "hardware.toml").read_text(encoding="utf-8")
        assert 'serial = "REQUIRED_FILL"' in draft
        assert "34D03DD" in draft and "approved = false" in draft
        parse_refused = False
        try:
            local_config.parse_hardware_declaration(root / "hardware.toml")
        except local_config.ConfigGenerationError:
            parse_refused = True
        assert parse_refused  # 초안은 승인 전 — 그대로는 실행 불가

        # 멱등: 재실행이 identity·초안을 덮어쓰지 않는다.
        report2 = setup_cmd.run_setup(paths, runner=runner,
                                      usb_probe=lambda: observed)
        assert report2["steps"]["identity"]["created"] is False
        assert report2["steps"]["hardware_draft"]["created"] is False

        # apt 설치 비활성 + 누락 → 명시적 실패.
        try:
            setup_cmd.run_setup(paths, runner=runner,
                                usb_probe=lambda: observed,
                                apt_install=False)
            raise AssertionError("apt 누락이 거부되지 않았다")
        except setup_cmd.SetupError as exc:
            assert "누락" in str(exc)


# ------------------------------------------------- W4 보완 검증 (승인 조건)

def test_setup_identity_passes_default_preflight():
    """setup이 만든 identity가 **기본 경로 그대로** preflight를 통과한다.

    파일명 단일 출처(identity 모듈) 검증 + key-cert 일치는 실물 crypto로
    확인한다 (W4 보완 1의 preflight 측)."""
    from apps.standalone import identity as identity_module
    from apps.standalone import local_preflight

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)  # vdeter import용
        paths.ensure_var()  # disk 검사 대상 디렉터리
        created = identity_module.ensure_identity(paths)
        assert created["created"]
        expected_key = identity_module.identity_paths(paths)["key"]
        seen_paths = []

        def key_mode_check(path):
            seen_paths.append(Path(path))
            return {"pass": True, "present": True, "mode": "0o600",
                    "error": None}

        report = local_preflight.run_required_preflight(
            hardware={"platform_profile": "generic-pc",
                      "endpoints": {"MASS-A": {"serial": "S1"},
                                    "MASS-B": {"serial": "S2"},
                                    "ROC": {"serial": "S3"}}},
            paths=paths,
            usb_probe=lambda: {
                "S1": {"usb_path": "1-1", "speed_mbps": 5000},
                "S2": {"usb_path": "1-2", "speed_mbps": 480},
                "S3": {"usb_path": "1-3", "speed_mbps": 5000}},
            residual_probe=lambda: [],
            platform_probe=lambda p: {"pass": True, "profile": p,
                                      "error": None},
            key_mode_check=key_mode_check,
            # key_cert_check는 기본(실물 crypto) — setup 산출물로 일치해야 함
        )
        assert report["pass"], report["failures"]
        assert seen_paths == [expected_key]  # 기본 경로 = identity 모듈 상수
        assert report["checks"]["key_cert_match"] is True


def test_endpoint_launcher_loads_standalone_identity():
    """launcher가 identity root 전달 시 **같은 fingerprint를 실제 적재**하고,
    미전달 시 기존 configs/stage7/trust 경로를 그대로 쓴다 (W4 보완 1)."""
    from apps.standalone import identity as identity_module

    launcher_path = (Path(_REPO) / "VDETER" / "apps"
                     / "stage7_endpoint_launcher.py")
    spec = importlib.util.spec_from_file_location(
        "stage7_endpoint_launcher_for_test", launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        created = identity_module.ensure_identity(paths)

        os.environ["VDETER_STANDALONE_IDENTITY_ROOT"] = str(
            paths.identity_dir)
        try:
            store_path, key_path = launcher._trust_material_paths("/ignored")
            assert Path(store_path) == identity_module.identity_paths(
                paths)["trust_store"]
            assert Path(key_path) == identity_module.identity_paths(
                paths)["key"]
            deps, fingerprint = launcher._load_trust(
                "/ignored", is_control=True)
            # CONTROL(ROC)이 setup identity의 키로 signer를 실제 조립했다.
            assert deps.tbb_signer is not None
            assert fingerprint.hex() == created["fingerprint"]
        finally:
            del os.environ["VDETER_STANDALONE_IDENTITY_ROOT"]

        # env 미지정 → 기존 host 경로 그대로 (동작 보존).
        store_path, key_path = launcher._trust_material_paths("/deploy/root")
        assert store_path.replace("\\", "/").endswith(
            "configs/stage7/trust/tbb_trust_store.pem")
        assert key_path.replace("\\", "/").endswith(
            "configs/stage7/trust/roc_tbb_signer_key.pem")


def test_setup_rerun_keeps_manifest_sha():
    """코드 불변이면 1초 뒤 setup 재실행에도 manifest SHA가 그대로다
    (W4 보완 2 — 재실행이 자격을 무효화하지 않는다)."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)

        def runner(argv, **kwargs):
            return Done(0)

        observed = {}
        first = setup_cmd.run_setup(paths, runner=runner,
                                    usb_probe=lambda: observed,
                                    make_venv=False)
        _time.sleep(1.1)
        second = setup_cmd.run_setup(paths, runner=runner,
                                     usb_probe=lambda: observed,
                                     make_venv=False)
        step1 = first["steps"]["deploy_manifest"]
        step2 = second["steps"]["deploy_manifest"]
        assert step2["sha256"] == step1["sha256"]
        assert step2["reused"] is True
        assert step2["qualification_invalidated"] is False

        # tree가 실제로 바뀌면 재발급 + 자격 무효화 보고.
        (root / "apps" / "x.py").write_text("X = 99\n")
        third = setup_cmd.run_setup(paths, runner=runner,
                                    usb_probe=lambda: observed,
                                    make_venv=False)
        step3 = third["steps"]["deploy_manifest"]
        assert step3["sha256"] != step1["sha256"]
        assert step3["reused"] is False
        assert step3["qualification_invalidated"] is True


def test_corrupted_manifest_invalidates_qualification():
    """손상된(파싱 불가) manifest도 '이전 manifest 존재'로 판정한다 —
    qualification_invalidated=true (W4 추가 보완)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        paths.ensure_var()
        (paths.var_dir / "deploy-manifest.json").write_text(
            "{ corrupted", encoding="utf-8")

        report = setup_cmd.run_setup(
            paths, runner=lambda argv, **kw: Done(0),
            usb_probe=lambda: {}, make_venv=False)
        step = report["steps"]["deploy_manifest"]
        assert step["reused"] is False
        assert step["qualification_invalidated"] is True
        # 재발급본은 정상 적재된다.
        loaded, _sha = deploy_manifest.load_manifest(
            paths.var_dir / "deploy-manifest.json")
        assert loaded is not None


def test_setup_reexec_when_cryptography_missing_in_interpreter():
    """시스템 python3에 cryptography가 없고 venv가 준비됐으면 재실행 신호."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        venv_py = setup_cmd.venv_python(paths)
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("", encoding="ascii")  # venv 존재 모사
        try:
            setup_cmd.run_setup(paths, runner=lambda argv, **kw: Done(0),
                                usb_probe=lambda: {}, apt_install=False,
                                make_venv=False,
                                crypto_available=lambda: False)
            raise AssertionError("재실행 신호가 나지 않았다")
        except setup_cmd.SetupReexec as reexec:
            assert reexec.venv_python == str(venv_py)
        # venv도 없으면 조치 안내와 함께 실패.
        venv_py.unlink()
        try:
            setup_cmd.run_setup(paths, runner=lambda argv, **kw: Done(0),
                                usb_probe=lambda: {}, apt_install=False,
                                make_venv=False,
                                crypto_available=lambda: False)
            raise AssertionError("실패하지 않았다")
        except setup_cmd.SetupError as exc:
            assert "cryptography" in str(exc)


def test_apt_plan_includes_python3_venv():
    assert "python3-venv" in setup_cmd.REQUIRED_APT_PACKAGES

    def runner(argv, **kwargs):
        return Done(1)  # 전부 미설치

    plan = setup_cmd.plan_apt(runner=runner)
    assert "python3-venv" in plan["missing"]


# ---------------------------------------------------------------- binding

def _load_binding_tool():
    path = (Path(_REPO) / "VDETER" / "tools"
            / "make_standalone_binding.py")
    spec = importlib.util.spec_from_file_location("make_binding_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_standalone_binding_contract():
    tool = _load_binding_tool()
    with tempfile.TemporaryDirectory() as tmp:
        vdeter_root = Path(tmp) / "VDETER"
        frozen_rel = "src/vdeter/config/models.py"
        frozen_path = vdeter_root / frozen_rel
        frozen_path.parent.mkdir(parents=True)
        # CRLF로 기록해도 정규화(LF) 해시가 나온다.
        frozen_path.write_bytes(b"FROZEN = 1\r\nX = 2\r\n")
        import hashlib
        expected_sha = hashlib.sha256(b"FROZEN = 1\nX = 2\n").hexdigest()

        doc = Path(tmp) / "baseline.md"
        doc.write_text("# 제목\n\n문서 ID: `X`  \n문서 버전: `0.9.23`\n",
                       encoding="utf-8")

        def git_runner(argv, **kwargs):
            if "status" in argv:
                return Done(0, "")
            if "--abbrev-ref" in argv:
                return Done(0, "feature/phase-i\n")
            return Done(0, "a" * 40 + "\n")

        binding = tool.build_binding(
            repo_root=tmp, vdeter_root=str(vdeter_root),
            baseline_doc=str(doc), frozen_files=[frozen_rel],
            git_runner=git_runner)
        assert binding["source_head"] == "a" * 40
        assert binding["baseline_version"] == "0.9.23"
        assert binding["frozen_files"][frozen_rel] == expected_sha
        # 산출물이 소비자(load_standalone_binding) 검증을 통과한다.
        out = Path(tmp) / "standalone-binding.json"
        out.write_text(json.dumps(binding), encoding="utf-8")
        loaded = local_config.load_standalone_binding(out)
        assert loaded["source_branch"] == "feature/phase-i"

        # dirty 트리는 거부.
        def dirty_runner(argv, **kwargs):
            if "status" in argv:
                return Done(0, " M apps/x.py\n")
            return Done(0, "a" * 40 + "\n")

        try:
            tool.build_binding(repo_root=tmp, vdeter_root=str(vdeter_root),
                               baseline_doc=str(doc),
                               frozen_files=[frozen_rel],
                               git_runner=dirty_runner)
            raise AssertionError("dirty 트리가 거부되지 않았다")
        except tool.BindingError as exc:
            assert "dirty" in str(exc)


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase I W4 standalone setup/identity/manifest/binding 계약")
