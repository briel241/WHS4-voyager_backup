"""standalone setup·identity·installation manifest·launcher 계약 시험 (v2).

증명하는 것 (apps/standalone/README.md §1·§2·§5, 결정 10·11, clone-bootstrap)
  identity        : 부재 시에만 생성(멱등), 부분 존재 거부, --new-identity는
                    공개 기록만 archive·개인키 폐기·fingerprint 교체
  setup           : apt 계획/설치·venv·wrapper·identity·B210 발견·초안·
                    installation manifest·환경 기록·사용자 launcher의 멱등
                    orchestration (실행기는 전부 주입 — apt/venv 미실행)
  installation    : 재실행에도 digest 불변, 파일 변경 시 영역별 무효화 보고,
                    손상 manifest는 재발급 + 무효화
  binding 도구    : (legacy, 개발 PC 배포 도구 전용) 계약 유지
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
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (  # noqa: E402
    identity,
    installation_manifest,
    local_config,
    setup_cmd,
    state,
)


class Done:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout


def make_mini_root(tmp) -> Path:
    """installation scan이 가능한 최소 설치 tree (RF-critical 심볼 실물 포함)."""
    root = Path(tmp) / "root"
    (root / "apps" / "standalone").mkdir(parents=True)
    shutil.copy2(Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py",
                 root / "apps" / "standalone" / "qualify_cmd.py")
    (root / "apps" / "standalone" / "cli.py").write_text("CLI = 1\n")
    (root / "VDETER" / "src" / "vdeter").mkdir(parents=True)
    (root / "VDETER" / "src" / "vdeter" / "a.py").write_text("A = 1\n")
    (root / "apps" / "x.py").write_text("X = 1\n")
    (root / "apps" / "__pycache__").mkdir()
    (root / "apps" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    (root / "common").mkdir()
    (root / "common" / "y.py").write_text("Y = 1\n")
    (root / "bin").mkdir()
    shutil.copy2(Path(_REPO) / "bin" / "vdeter", root / "bin" / "vdeter")
    (root / "requirements-standalone.lock").write_text("cryptography==50.0.0\n")
    return root


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

        # 부분 존재(개인키만 삭제) → 거부.
        paths_files["key"].unlink()
        try:
            identity.ensure_identity(paths)
            raise AssertionError("부분 존재가 거부되지 않았다")
        except identity.IdentityError:
            pass
        paths_files["key"].write_bytes(b"placeholder")  # 복구 (다음 단계용)

        renewed = identity.new_identity(paths)
        assert renewed["created"] is True
        assert renewed["fingerprint"] != fingerprint
        assert renewed["destroyed_private_key"] is True
        archive = Path(renewed["archived_to"])
        assert (archive / "identity-manifest.json").is_file()
        assert not any(p.name.endswith("key.pem") for p in archive.iterdir())
        manifest = identity.load_identity_manifest(paths)
        assert manifest["previous_fingerprint"] == fingerprint


# ---------------------------------------------------------------- setup

def test_run_setup_orchestration_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        home = Path(tmp) / "home"
        commands = []

        def runner(argv, **kwargs):
            commands.append(list(argv))
            if argv[:2] == ["dpkg", "-s"]:
                return Done(0 if argv[2] == "gnuradio" else 1)
            return Done(0)

        observed = {"34D03DD": {"usb_path": "3-1", "speed_mbps": 5000}}
        report = setup_cmd.run_setup(paths, runner=runner,
                                     usb_probe=lambda: observed, home=home)
        assert report["pass"], report

        # apt: 설치된 gnuradio 제외 나머지만 설치 명령 (python3-venv 포함).
        assert report["steps"]["apt_plan"]["missing"] == [
            "uhd-host", "python3-numpy", "python3-venv"]
        assert "python3-uhd" not in setup_cmd.REQUIRED_APT_PACKAGES
        install = next(c for c in commands if c[:3] == ["sudo", "apt-get",
                                                        "install"])
        assert install[-3:] == ["uhd-host", "python3-numpy", "python3-venv"]
        venv_cmd = next(c for c in commands if c[1:3] == ["-m", "venv"])
        assert "--system-site-packages" in venv_cmd
        pip_cmd = next(c for c in commands if "pip" in c and "install" in c)
        assert pip_cmd[-1].endswith("requirements-standalone.lock")
        # git 명령은 한 번도 호출되지 않았다.
        assert not any(os.path.basename(str(c[0])).startswith("git")
                       for c in commands)

        # wrapper·installation manifest·환경 기록·launcher.
        wrapper = Path(report["steps"]["wrapper"]["path"])
        assert "cli.py" in wrapper.read_text(encoding="utf-8")
        inst_dir = installation_manifest.installation_dir(paths)
        for name in ("installation-manifest.json", "rf-manifest.json",
                     "operation-manifest.json", "tooling-manifest.json"):
            assert (inst_dir / name).is_file(), name
        inst = report["steps"]["installation"]
        assert inst["reused"] is False
        assert inst["qualification_invalidated"] is False
        assert inst["domain_file_count"]["rf"] == 1        # VDETER/src/vdeter/a.py
        assert inst["domain_file_count"]["operation"] >= 3  # cli.py·x.py·y.py·bin/vdeter
        env_record = json.loads((paths.var_dir
                                 / "setup-environment.json").read_text())
        assert set(env_record["versions"]) == {
            "numpy_version", "cryptography_version",
            "uhd_version", "gnuradio_version"}
        discovery = report["steps"]["b210_discovery"]
        assert discovery["count"] == 1 and "34D03DD" in discovery["observed"]
        launcher = report["steps"]["user_launcher"]
        assert launcher["installed"] and Path(launcher["path"]).is_file()
        assert Path(launcher["path"]) == home / ".local" / "bin" / "vdeter"

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

        # 멱등: 재실행이 identity·초안을 덮어쓰지 않고 manifest·launcher 재사용.
        report2 = setup_cmd.run_setup(paths, runner=runner,
                                      usb_probe=lambda: observed, home=home)
        assert report2["steps"]["identity"]["created"] is False
        assert report2["steps"]["hardware_draft"]["created"] is False
        assert report2["steps"]["installation"]["reused"] is True
        assert report2["steps"]["user_launcher"]["status"] == "unchanged"

        # apt 설치 비활성 + 누락 → 명시적 실패.
        try:
            setup_cmd.run_setup(paths, runner=runner,
                                usb_probe=lambda: observed,
                                apt_install=False, home=home)
            raise AssertionError("apt 누락이 거부되지 않았다")
        except setup_cmd.SetupError as exc:
            assert "missing" in str(exc)


def test_setup_identity_passes_default_preflight():
    """setup이 만든 identity가 **기본 경로 그대로** preflight를 통과한다."""
    from apps.standalone import identity as identity_module
    from apps.standalone import local_preflight

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)  # vdeter import용
        paths.ensure_var()
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
        )
        assert report["pass"], report["failures"]
        assert seen_paths == [expected_key]
        assert report["checks"]["key_cert_match"] is True


def test_endpoint_launcher_loads_standalone_identity():
    """launcher가 identity root 전달 시 같은 fingerprint를 실제 적재하고,
    미전달 시 기존 configs/stage7/trust 경로를 그대로 쓴다."""
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
            assert deps.tbb_signer is not None
            assert fingerprint.hex() == created["fingerprint"]
        finally:
            del os.environ["VDETER_STANDALONE_IDENTITY_ROOT"]

        store_path, key_path = launcher._trust_material_paths("/deploy/root")
        assert store_path.replace("\\", "/").endswith(
            "configs/stage7/trust/tbb_trust_store.pem")
        assert key_path.replace("\\", "/").endswith(
            "configs/stage7/trust/roc_tbb_signer_key.pem")


def test_setup_rerun_keeps_installation_digest_and_reports_changed_domain():
    """코드 불변이면 재실행에도 installation digest가 그대로다; 파일이
    바뀌면 어느 영역이 바뀌었는지와 함께 자격 무효화를 보고한다."""
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)

        def runner(argv, **kwargs):
            return Done(0)

        first = setup_cmd.run_setup(paths, runner=runner, usb_probe=lambda: {},
                                    make_venv=False, install_launcher=False)
        _time.sleep(1.1)
        second = setup_cmd.run_setup(paths, runner=runner, usb_probe=lambda: {},
                                     make_venv=False, install_launcher=False)
        step1 = first["steps"]["installation"]
        step2 = second["steps"]["installation"]
        assert step2["installation_sha256"] == step1["installation_sha256"]
        assert step2["reused"] is True
        assert step2["qualification_invalidated"] is False
        # installation-manifest.json 파일 바이트도 그대로다 (재기록 안 함).
        path = installation_manifest.installation_dir(paths) / "installation-manifest.json"
        sha_before = installation_manifest.sha256_file(path)

        # operation 파일 변경 → 재발급 + operation 영역 무효화 보고.
        (root / "apps" / "x.py").write_text("X = 99\n")
        third = setup_cmd.run_setup(paths, runner=runner, usb_probe=lambda: {},
                                    make_venv=False, install_launcher=False)
        step3 = third["steps"]["installation"]
        assert step3["installation_sha256"] != step1["installation_sha256"]
        assert step3["reused"] is False
        assert step3["qualification_invalidated"] is True
        assert step3["changed_domains"] == ["operation"]
        assert step3["rf_manifest_sha256"] == step1["rf_manifest_sha256"]
        assert installation_manifest.sha256_file(path) != sha_before

        # rf 파일 변경 → rf 영역 무효화.
        (root / "VDETER" / "src" / "vdeter" / "a.py").write_text("A = 2\n")
        fourth = setup_cmd.run_setup(paths, runner=runner, usb_probe=lambda: {},
                                     make_venv=False, install_launcher=False)
        assert fourth["steps"]["installation"]["changed_domains"] == ["rf"]


def test_corrupted_manifest_invalidates_qualification():
    """손상된(파싱 불가) manifest도 '이전 manifest 존재'로 판정한다 —
    qualification_invalidated=true, 재발급본은 정상 적재된다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        paths.ensure_var()
        inst_dir = installation_manifest.installation_dir(paths)
        inst_dir.mkdir(parents=True)
        (inst_dir / "installation-manifest.json").write_text(
            "{ corrupted", encoding="utf-8")

        report = setup_cmd.run_setup(
            paths, runner=lambda argv, **kw: Done(0),
            usb_probe=lambda: {}, make_venv=False, install_launcher=False)
        step = report["steps"]["installation"]
        assert step["reused"] is False
        assert step["qualification_invalidated"] is True
        loaded = installation_manifest.load_installation(paths)
        assert loaded["installation_sha256"] == step["installation_sha256"]


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
                                make_venv=False, install_launcher=False,
                                crypto_available=lambda p: False)
            raise AssertionError("재실행 신호가 나지 않았다")
        except setup_cmd.SetupReexec as reexec:
            assert reexec.venv_python == str(venv_py)
        venv_py.unlink()
        try:
            setup_cmd.run_setup(paths, runner=lambda argv, **kw: Done(0),
                                usb_probe=lambda: {}, apt_install=False,
                                make_venv=False, install_launcher=False,
                                crypto_available=lambda p: False)
            raise AssertionError("실패하지 않았다")
        except setup_cmd.SetupError as exc:
            assert "cryptography" in str(exc)

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        assert setup_cmd._cryptography_available(paths) is True


def test_apt_plan_includes_python3_venv():
    assert "python3-venv" in setup_cmd.REQUIRED_APT_PACKAGES

    def runner(argv, **kwargs):
        return Done(1)  # 전부 미설치

    plan = setup_cmd.plan_apt(runner=runner)
    assert "python3-venv" in plan["missing"]


def test_setup_progress_steps_are_exact_and_optional():
    """진행 표시는 선택이며 setup 결과에 영향이 없다 (표시 on/off 동일)."""
    from apps.standalone import progress as pg

    with tempfile.TemporaryDirectory() as tmp:
        root = make_mini_root(tmp)
        paths = state.StatePaths(root)
        lines = []
        reporter = pg.ProgressReporter(lines.append, tty=False, ticker=False,
                                       wallclock=lambda: 1_800_000_000)
        report = setup_cmd.run_setup(
            paths, runner=lambda argv, **kw: Done(0), usb_probe=lambda: {},
            make_venv=False, install_launcher=False,
            progress=pg.SetupProgress(reporter))
        reporter.close()
        assert report["pass"]
        assert any("Setup · installation manifest" in l and "step=" in l
                   for l in lines)
        assert not any("estimated" in l for l in lines)  # 단계 n/N은 exact
        silent = setup_cmd.run_setup(
            paths, runner=lambda argv, **kw: Done(0), usb_probe=lambda: {},
            make_venv=False, install_launcher=False)
        assert (silent["steps"]["installation"]["installation_sha256"]
                == report["steps"]["installation"]["installation_sha256"])


# ---------------------------------------------------------------- binding (legacy)

def _load_binding_tool():
    path = (Path(_REPO) / "VDETER" / "tools"
            / "make_standalone_binding.py")
    spec = importlib.util.spec_from_file_location("make_binding_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_standalone_binding_contract_legacy_tool():
    """개발 PC 배포 도구 전용 legacy 계약 — 대상 기계 실행 경로는 더 이상
    이 파일을 요구하지 않는다(installation manifest로 대체)."""
    tool = _load_binding_tool()
    with tempfile.TemporaryDirectory() as tmp:
        vdeter_root = Path(tmp) / "VDETER"
        frozen_rel = "src/vdeter/config/models.py"
        frozen_path = vdeter_root / frozen_rel
        frozen_path.parent.mkdir(parents=True)
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
        assert binding["frozen_files"][frozen_rel] == expected_sha
        out = Path(tmp) / "standalone-binding.json"
        out.write_text(json.dumps(binding), encoding="utf-8")
        assert local_config.load_standalone_binding(out)["source_branch"] == "feature/phase-i"


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: standalone setup/identity/installation/launcher 계약")
