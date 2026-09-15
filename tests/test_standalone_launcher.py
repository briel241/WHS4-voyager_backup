"""`bin/vdeter` 저장소 launcher + `~/.local/bin/vdeter` 사용자 launcher 시험.

sh(POSIX)로 실제 실행한다 — Windows 개발 환경은 Git Bash의 sh를 쓴다.
python3는 스텁 스크립트로 대체해 argv·종료 코드 전달을 검증한다.
signal 전달은 POSIX 전용(Windows에서는 skip; Pi clean-clone 시험에서 실행).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import setup_cmd, state  # noqa: E402
from apps.standalone import installation_manifest as im  # noqa: E402

LAUNCHER = Path(_REPO) / "bin" / "vdeter"
SH = shutil.which("sh")


def _require_sh():
    if SH is None:
        raise unittest.SkipTest("POSIX sh 없음 — Pi clean-clone 시험에서 실행")


def _posix(path) -> str:
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _write_stub(path: Path, *, exit_code: int = 0) -> None:
    """argv를 기록 파일에 JSON으로 남기고 지정 코드로 종료하는 python3 스텁."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0\" \"$@\" > \"$VDETER_STUB_RECORD\"\n"
        f"exit {exit_code}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _make_root(tmp, *, with_venv: bool) -> Path:
    root = Path(tmp) / "checkout"
    (root / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, root / "bin" / "vdeter")
    (root / "bin" / "vdeter").chmod(0o755)
    (root / "apps" / "standalone").mkdir(parents=True)
    (root / "apps" / "standalone" / "cli.py").write_text("# stub cli\n")
    if with_venv:
        _write_stub(root / "var" / "venv" / "bin" / "python3")
    return root


def _run(root: Path, args, *, cwd=None, path_dirs=(), exit_code=0,
         record_name="record.txt"):
    record = root / record_name
    env = dict(os.environ)
    env["VDETER_STUB_RECORD"] = _posix(record)
    env["VDETER_STUB_EXIT"] = str(exit_code)
    if path_dirs:
        env["PATH"] = os.pathsep.join([str(p) for p in path_dirs]) + os.pathsep + env.get("PATH", "")
    done = subprocess.run([SH, _posix(root / "bin" / "vdeter"), *args],
                          cwd=str(cwd or root), env=env, capture_output=True,
                          text=True, timeout=60)
    recorded = record.read_text("utf-8").splitlines() if record.is_file() else None
    return done, recorded


def test_repo_launcher_is_executable_in_git_index():
    """실행 비트: git index mode 100755 (NTFS에는 비트가 없다) + 내용 검사."""
    text = LAUNCHER.read_text("utf-8")
    assert text.startswith("#!/bin/sh\n")
    code_lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    assert not any("eval" in l for l in code_lines)   # eval·문자열 조립 금지
    assert 'exec "$venv_python" "$cli" "$@"' in text
    assert 'exec python3 "$cli" "$@"' in text
    done = subprocess.run(["git", "-C", _REPO, "ls-files", "-s", "bin/vdeter"],
                          capture_output=True, text=True, timeout=30)
    if done.returncode == 0 and done.stdout.strip():
        assert done.stdout.split()[0] == "100755", done.stdout
    elif os.name == "posix":
        assert os.access(LAUNCHER, os.X_OK)


def test_launcher_uses_venv_python_and_passes_argv_and_exit_code():
    _require_sh()
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_root(tmp, with_venv=True)
        done, recorded = _run(root, ["run", "all", "--case", "both", "--no-retry"])
        assert done.returncode == 0, done.stderr
        assert recorded is not None
        assert recorded[1].endswith("apps/standalone/cli.py")
        assert recorded[2:] == ["run", "all", "--case", "both", "--no-retry"]
        # 종료 코드 전달 (exec).
        _write_stub(root / "var" / "venv" / "bin" / "python3", exit_code=4)
        done, _ = _run(root, ["run"])
        assert done.returncode == 4
        # 다른 작업 디렉터리에서 실행해도 root를 스스로 계산한다.
        done, recorded = _run(root, ["results"], cwd=tmp)
        assert done.returncode == 4  # 스텁 exit 4 그대로
        assert recorded[1].replace("\\", "/").endswith(
            "checkout/apps/standalone/cli.py")


def test_launcher_without_venv_uses_system_python3_only_for_setup():
    _require_sh()
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_root(tmp, with_venv=False)
        stub_dir = Path(tmp) / "stubbin"
        _write_stub(stub_dir / "python3")
        done, recorded = _run(root, ["setup", "--no-apt"], path_dirs=[stub_dir])
        assert done.returncode == 0, done.stderr
        assert recorded[2:] == ["setup", "--no-apt"]
        # setup 이외 명령은 명확한 오류 + exit 2 (python3 호출 없음).
        done, recorded = _run(root, ["run", "all"], path_dirs=[stub_dir],
                              record_name="never.txt")
        assert done.returncode == 2
        assert "./bin/vdeter setup" in done.stderr
        assert recorded is None
        done, _ = _run(root, ["qualify", "--help"], path_dirs=[stub_dir],
                       record_name="never2.txt")
        assert done.returncode == 2 and "setup" in done.stderr


def test_launcher_signal_passthrough_posix():
    if os.name != "posix":
        raise unittest.SkipTest("POSIX 전용 — Pi clean-clone 시험에서 실행")
    _require_sh()
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_root(tmp, with_venv=True)
        stub = root / "var" / "venv" / "bin" / "python3"
        stub.write_text("#!/bin/sh\ntrap 'exit 143' TERM\nsleep 30 & wait\n",
                        encoding="utf-8", newline="\n")
        stub.chmod(0o755)
        proc = subprocess.Popen([SH, str(root / "bin" / "vdeter"), "run"],
                                cwd=str(root))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)  # exec이라 python 스텁이 직접 받는다
        assert proc.wait(timeout=10) == 143


# ---------------------------------------------------------------- user launcher

def _paths_with_installation(tmp) -> state.StatePaths:
    root = Path(tmp) / "checkout"
    (root / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, root / "bin" / "vdeter")
    (root / "apps" / "standalone").mkdir(parents=True)
    (root / "apps" / "standalone" / "cli.py").write_text("# stub\n")
    paths = state.StatePaths(root)
    paths.ensure_var()
    return paths


def test_user_launcher_install_idempotent_conflict_and_path_hint():
    with tempfile.TemporaryDirectory() as tmp:
        paths = _paths_with_installation(tmp)
        home = Path(tmp) / "home"
        first = setup_cmd.install_user_launcher(paths, home=home, path_env="/usr/bin")
        target = home / ".local" / "bin" / "vdeter"
        assert first["status"] == "installed" and target.is_file()
        body = target.read_text("utf-8")
        assert setup_cmd.USER_LAUNCHER_MARKER in body
        assert "exec \"$launcher\" \"$@\"" in body
        assert first["on_path"] is False
        assert 'export PATH="$HOME/.local/bin:$PATH"' in first["path_hint"]
        launcher_id = (im.installation_dir(paths) / "launcher-id").read_text().strip()
        assert launcher_id and launcher_id in body
        # 멱등: 재설치는 unchanged, id 불변.
        second = setup_cmd.install_user_launcher(
            paths, home=home, path_env=str(home / ".local" / "bin"))
        assert second["status"] == "unchanged" and second["on_path"] is True
        assert second["path_hint"] is None
        assert (im.installation_dir(paths) / "launcher-id").read_text().strip() == launcher_id
        # 타 프로그램의 vdeter는 덮어쓰지 않는다.
        target.write_text("#!/bin/sh\necho other program\n", encoding="utf-8")
        third = setup_cmd.install_user_launcher(paths, home=home)
        assert third["status"] == "conflict" and third["installed"] is False
        assert "other program" in target.read_text("utf-8")


def test_user_launcher_refuses_stale_root_or_foreign_installation():
    _require_sh()
    with tempfile.TemporaryDirectory() as tmp:
        paths = _paths_with_installation(tmp)
        home = Path(tmp) / "home"
        _write_stub(paths.root / "var" / "venv" / "bin" / "python3")
        setup_cmd.install_user_launcher(paths, home=home)
        target = home / ".local" / "bin" / "vdeter"
        env = dict(os.environ)
        env["VDETER_STUB_RECORD"] = _posix(paths.root / "rec.txt")
        done = subprocess.run([SH, _posix(target), "results", "x"], env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert (paths.root / "rec.txt").read_text().splitlines()[2:] == ["results", "x"]
        # 설치 id가 바뀐 경우(다른 설치본이 같은 경로에 있음) → stale 오류.
        (im.installation_dir(paths) / "launcher-id").write_text("otherid\n")
        done = subprocess.run([SH, _posix(target), "results"], env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and "stale" in done.stderr
        # 저장소가 이동/삭제된 경우 → 다른 경로를 조용히 실행하지 않는다.
        shutil.rmtree(paths.root)
        done = subprocess.run([SH, _posix(target), "results"], env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and "not found" in done.stderr
