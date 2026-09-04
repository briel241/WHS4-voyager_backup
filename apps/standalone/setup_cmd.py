"""`vdeter setup` — 기계당 1회 환경 준비 (Phase I W4, README §1·§2, 결정 10·11).

책임 (전부 멱등 — 재실행이 기존 상태를 훼손하지 않는다)
    1. apt 의존성: GNU Radio·UHD·numpy — 온라인 설치 허용 (결정 10).
       이미 설치된 패키지는 건너뛴다.
    2. venv: `var/venv` (`--system-site-packages` — apt의 gnuradio/uhd
       바인딩을 보기 위함) + pip 계열은 `requirements-standalone.lock`으로
       정확 pin 설치.
    3. wrapper: `var/venv/bin/vdeter` 한 단어 명령.
    4. identity: 부재 시에만 자동 생성 (`--new-identity`는 명시 재생성 —
       identity 모듈 정책, 결정 11).
    5. hardware.toml 초안: 부재 시에만 생성 — 관측된 B210 목록을 주석으로
       제공하되 **역할 배정은 하지 않는다** (결정 1). 승인은 사용자 몫.
    6. 배포 manifest 실측 → `var/deploy-manifest.json` (run/qualify 검증의
       기준 — deploy_manifest 모듈).
    7. 환경 기록: 실측 버전을 `var/setup-environment.json`에 기록.

일반 실행은 이 모듈을 호출하지 않는다 — setup은 인터넷을 쓸 수 있는 유일한
명령이다. Git은 읽지 않는다 (W1 수정 4).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from apps.standalone import deploy_manifest, identity, local_config
from apps.standalone.state import StatePaths, write_json_atomic

#: apt 의존성 (결정 10 — 대상 Ubuntu 패키지명). python3-venv는 깨끗한
#: Ubuntu에서 `python3 -m venv`가 실패하지 않기 위한 필수 항목 (W4 보완 3).
REQUIRED_APT_PACKAGES = (
    "gnuradio", "uhd-host", "python3-uhd", "python3-numpy", "python3-venv",
)

LOCK_RELPATH = "requirements-standalone.lock"


class SetupError(RuntimeError):
    """setup 단계 실패 — 메시지는 조치 가능해야 한다."""


# ------------------------------------------------------------ apt

def plan_apt(*, runner=subprocess.run) -> dict:
    """설치 필요 패키지 목록 (dpkg 조회 — 설치는 하지 않는다)."""
    missing = []
    for package in REQUIRED_APT_PACKAGES:
        done = runner(["dpkg", "-s", package],
                      capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            missing.append(package)
    return {"required": list(REQUIRED_APT_PACKAGES), "missing": missing}


def install_apt(missing, *, runner=subprocess.run) -> dict:
    """누락 패키지 설치 (온라인 — setup 전용). sudo가 필요하다."""
    if not missing:
        return {"installed": [], "skipped": True}
    command = ["sudo", "apt-get", "install", "-y", *missing]
    done = runner(command, timeout=1800)
    if done.returncode != 0:
        raise SetupError(
            f"apt 설치 실패 (rc={done.returncode}): {' '.join(missing)} — "
            "네트워크·sudo 권한을 확인한 뒤 setup을 재실행하십시오")
    return {"installed": list(missing), "skipped": False}


# ------------------------------------------------------------ venv·wrapper

def venv_python(paths: StatePaths) -> Path:
    return paths.var_dir / "venv" / "bin" / "python3"


def ensure_venv(paths: StatePaths, *, runner=subprocess.run,
                system_python: str = "python3") -> dict:
    """venv 생성(부재 시) + lock 기반 pip 설치 (멱등 — pin 재설치는 no-op)."""
    venv_dir = paths.var_dir / "venv"
    created = False
    if not venv_python(paths).exists():
        done = runner([system_python, "-m", "venv", "--system-site-packages",
                       str(venv_dir)], timeout=300)
        if done.returncode != 0:
            raise SetupError(f"venv 생성 실패 (rc={done.returncode})")
        created = True
    lock_path = paths.root / LOCK_RELPATH
    if not lock_path.is_file():
        raise SetupError(f"pip lock 파일이 없습니다: {lock_path}")
    done = runner([str(venv_python(paths)), "-m", "pip", "install",
                   "--require-virtualenv", "-r", str(lock_path)],
                  timeout=1800)
    if done.returncode != 0:
        raise SetupError(
            f"pip lock 설치 실패 (rc={done.returncode}) — 네트워크를 확인한 "
            "뒤 setup을 재실행하십시오")
    return {"created": created, "lock": LOCK_RELPATH}


def write_wrapper(paths: StatePaths) -> dict:
    """`var/venv/bin/vdeter` — 한 단어 진입점 (POSIX sh)."""
    wrapper = paths.var_dir / "venv" / "bin" / "vdeter"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# VDETER standalone wrapper - `vdeter setup`이 생성한다.\n"
        f"exec \"{venv_python(paths)}\" "
        f"\"{paths.root / 'apps' / 'standalone' / 'cli.py'}\" \"$@\"\n"
    )
    wrapper.write_text(script, encoding="utf-8", newline="\n")
    try:
        wrapper.chmod(0o755)
    except OSError:
        pass
    return {"path": str(wrapper)}


# ------------------------------------------------------------ hardware 초안

def draft_hardware_declaration(paths: StatePaths, observed: dict) -> dict:
    """hardware.toml 초안 — 부재 시에만. 역할 자동 배정은 하지 않는다.

    observed: local_preflight.probe_usb_serials() 결과 (serial→경로·속도).
    """
    target = paths.root / "hardware.toml"
    if target.is_file():
        return {"created": False, "path": str(target)}
    observed_lines = [
        f"#   serial {serial}: usb_path={info.get('usb_path')} "
        f"speed={info.get('speed_mbps')}M"
        for serial, info in sorted(observed.items())
    ] or ["#   (탐지된 USB serial 없음 — 장비 연결 후 다시 확인)"]
    body = "\n".join([
        "# VDETER standalone 하드웨어 선언 — 사용자가 편집·승인한다.",
        "# 역할(MASS-A/MASS-B/ROC)과 serial 대응은 자동으로 정할 수 없다:",
        "# 물리 배선·전원 구성을 아는 사용자가 직접 기입해야 한다.",
        "# 탐지된 장치 (참고용):",
        *observed_lines,
        "",
        "schema_version = 1",
        'platform_profile = "pi"   # "pi" | "generic-pc"',
        "",
        '[endpoints.MASS-A]',
        'serial = "REQUIRED_FILL"',
        "",
        '[endpoints.MASS-B]',
        'serial = "REQUIRED_FILL"',
        'external_power_asset = "REQUIRED_FILL"',
        "",
        '[endpoints.ROC]',
        'serial = "REQUIRED_FILL"',
        "",
        "[wiring]",
        'profile = "C1-C6-STANDARD"',
        "approved = false   # 배선 확인 후 사용자가 true로 바꿔야 실행 가능",
        "",
    ])
    target.write_text(body, encoding="utf-8", newline="\n")
    return {"created": True, "path": str(target)}


# ------------------------------------------------------------ 본체

def run_setup(paths: StatePaths, *, runner=subprocess.run,
              usb_probe=None, apt_install: bool = True,
              make_venv: bool = True, regenerate_identity: bool = False,
              now=None) -> dict:
    """setup 전체 — 단계별 결과를 모아 report로 돌려준다.

    실패한 단계에서 중단한다 (부분 성공 상태도 멱등 재실행으로 이어진다).
    """
    if usb_probe is None:
        from apps.standalone.local_preflight import probe_usb_serials
        usb_probe = probe_usb_serials

    report = {"steps": {}, "pass": False}
    paths.ensure_var()
    report["steps"]["state_dirs"] = {"pass": True}

    apt_plan = plan_apt(runner=runner)
    report["steps"]["apt_plan"] = apt_plan
    if apt_install:
        report["steps"]["apt_install"] = install_apt(
            apt_plan["missing"], runner=runner)
    elif apt_plan["missing"]:
        raise SetupError(
            f"apt 패키지 누락: {apt_plan['missing']} — 설치가 비활성화되어 "
            "진행할 수 없습니다")

    if make_venv:
        report["steps"]["venv"] = ensure_venv(paths, runner=runner)
        report["steps"]["wrapper"] = write_wrapper(paths)

    if regenerate_identity:
        report["steps"]["identity"] = identity.new_identity(paths)
    else:
        report["steps"]["identity"] = identity.ensure_identity(paths, now=now)

    report["steps"]["hardware_draft"] = draft_hardware_declaration(
        paths, usb_probe())

    # 배포 manifest: 기존 manifest가 현재 tree와 일치하면 **파일·SHA를 그대로
    # 유지**한다 (W4 보완 2 — generated_utc 때문에 재기록만으로 SHA가 바뀌어
    # setup 재실행이 자격을 무효화하는 결함 봉합). tree가 실제로 달라졌을
    # 때만 재발급하고 qualification_invalidated=true로 보고한다.
    manifest_path = paths.var_dir / "deploy-manifest.json"
    # "이전 manifest가 있었는가"는 파싱 성공 여부가 아니라 **파일 존재**로
    # 판정한다 — 손상된 manifest도 기존 자격의 결속이 깨졌다는 뜻이다
    # (W4 추가 보완: existing is None이 invalidated=false로 새던 경로 봉합).
    had_previous_manifest = manifest_path.exists()
    existing, existing_sha = deploy_manifest.load_manifest(manifest_path)
    if existing is not None and deploy_manifest.verify(
            paths.root, existing)["pass"]:
        report["steps"]["deploy_manifest"] = {
            "sha256": existing_sha,
            "file_count": existing["file_count"],
            "relpath": deploy_manifest.MANIFEST_RELPATH,
            "reused": True,
            "qualification_invalidated": False,
        }
    else:
        written = deploy_manifest.write_manifest(paths.root, manifest_path)
        report["steps"]["deploy_manifest"] = {
            "sha256": written["sha256"],
            "file_count": written["manifest"]["file_count"],
            "relpath": written["relpath"],
            "reused": False,
            # 이전 manifest 파일이 있었는데 (tree 변경이든 파일 손상이든)
            # 재발급했다 = 기존 자격의 배포 결속이 더 이상 성립하지 않는다.
            "qualification_invalidated": had_previous_manifest,
        }

    manifest_now = local_config.build_environment_manifest()
    write_json_atomic(paths.var_dir / "setup-environment.json", {
        "artifact_type": "STAGE7_STANDALONE_SETUP_ENVIRONMENT_V1",
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version,
        "versions": {key: manifest_now.get(key)
                     for key in ("numpy_version", "cryptography_version",
                                 "uhd_version", "gnuradio_version")},
    })
    report["steps"]["environment_record"] = {"pass": True}
    report["pass"] = True
    return report
