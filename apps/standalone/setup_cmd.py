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
    5. hardware.toml 초안: 부재 시에만 생성 — 기대 프로필(기본: 실험실
       프로필 `hardware_draft.LAB_DEFAULT`)과 관측을 대조해 관측된 기대
       serial만 역할별 기대 매핑으로 채우고, 누락·불일치는 사유와 함께
       REQUIRED_FILL로 둔다. 다른 장비를 대신 배정하지 않으며 전원 증언·
       배선 승인은 쓰지 않는다 (결정 1, R6). 승인은 사용자 몫.
    6. 설치 manifest 실측 (v2) → `var/installation/{installation,rf,operation,
       tooling}-manifest.json` (run/qualify 결속의 기준 — installation_manifest
       모듈). 결속 digest가 이전과 다르면 기존 자격 무효화를 보고한다.
    7. 환경 기록: 실측 버전을 `var/setup-environment.json`에 기록.
    8. 사용자 launcher `~/.local/bin/vdeter` 설치 (root 불필요, 타 프로그램의
       기존 `vdeter`는 덮어쓰지 않음, 이 프로젝트 launcher는 멱등 갱신,
       저장소 이동 시 stale 검증).

일반 실행은 이 모듈을 호출하지 않는다 — setup은 인터넷을 쓸 수 있는 유일한
명령이다. Git은 실행하지 않고 읽지도 않는다 (clone·tar·USB 복사본 동일).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from apps.standalone import hardware_draft, hardware_profiles
from apps.standalone import identity, installation_manifest, local_config
from apps.standalone.state import StatePaths, write_json_atomic

#: apt 의존성 (결정 10 — 대상 Ubuntu 패키지명). python3-venv는 깨끗한
#: Ubuntu에서 `python3 -m venv`가 실패하지 않기 위한 필수 항목 (W4 보완 3).
#: python3-uhd는 넣지 않는다 — runtime은 gr-uhd(gnuradio)로 B210을 다루고
#: python `uhd` 모듈을 import하지 않는다(2026-09-04 grep·Pi 실측). 자격
#: 환경에 없던 패키지를 setup이 추가하면 "동일 환경 복원"이 깨진다.
REQUIRED_APT_PACKAGES = (
    "gnuradio", "uhd-host", "python3-numpy", "python3-venv",
)

LOCK_RELPATH = "requirements-standalone.lock"


class SetupError(RuntimeError):
    """setup 단계 실패 — 메시지는 조치 가능해야 한다."""


class SetupReexec(Exception):
    """identity 생성에 필요한 cryptography가 현재 인터프리터에 없다 —
    방금 만든 var/venv(lock으로 cryptography 설치됨)로 setup을 재실행하라.
    CLI가 `os.execv`로 처리한다. 앞 단계는 멱등이라 재실행이 안전하다."""

    def __init__(self, venv_python) -> None:
        super().__init__(f"re-exec under {venv_python}")
        self.venv_python = str(venv_python)


def _cryptography_available(paths: StatePaths) -> bool:
    """runtime이 실제로 쓸 수 있는 cryptography인가 — TBB 서명 계층의 판정
    (`vdeter.link.tbb.crypto._crypto_modules`: major 50 요구)을 단일 출처로
    재사용한다. Pi 실측(2026-09-04): 시스템 python3의 41.0.7은 import는 되나
    runtime 요구를 못 맞춰 identity 생성이 실패했다 — import 가능 여부만
    보던 검사는 fail-open이었다."""
    from apps.standalone.identity import _ensure_vdeter_importable
    _ensure_vdeter_importable(paths.root)
    try:
        from vdeter.link.tbb.crypto import _crypto_modules
    except ImportError:
        return False
    return not _crypto_modules().is_err


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
            f"apt install failed (rc={done.returncode}): {' '.join(missing)} — "
            "check network access and sudo permission, then rerun setup")
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
            raise SetupError(f"venv creation failed (rc={done.returncode})")
        created = True
    lock_path = paths.root / LOCK_RELPATH
    if not lock_path.is_file():
        raise SetupError(f"pip lock file missing: {lock_path}")
    done = runner([str(venv_python(paths)), "-m", "pip", "install",
                   "--require-virtualenv", "-r", str(lock_path)],
                  timeout=1800)
    if done.returncode != 0:
        raise SetupError(
            f"pip lock install failed (rc={done.returncode}) — check network access, "
            "then rerun setup")
    return {"created": created, "lock": LOCK_RELPATH}


def write_wrapper(paths: StatePaths) -> dict:
    """`var/venv/bin/vdeter` — 한 단어 진입점 (POSIX sh)."""
    wrapper = paths.var_dir / "venv" / "bin" / "vdeter"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# VDETER standalone wrapper - generated by `vdeter setup`.\n"
        f"exec \"{venv_python(paths)}\" "
        f"\"{paths.root / 'apps' / 'standalone' / 'cli.py'}\" \"$@\"\n"
    )
    wrapper.write_text(script, encoding="utf-8", newline="\n")
    try:
        wrapper.chmod(0o755)
    except OSError:
        pass
    return {"path": str(wrapper)}


# ------------------------------------------------------------ user launcher

USER_LAUNCHER_MARKER = "VDETER_STANDALONE_USER_LAUNCHER_V1"
REPO_LAUNCHER_RELPATH = "bin/vdeter"


def _sh_single_quote(value: str) -> str:
    """POSIX sh 단일 인용 (내부 '는 '\\'' 로)."""
    return "'" + value.replace("'", "'\\''") + "'"


def render_user_launcher(root: Path, launcher_id: str) -> str:
    """`~/.local/bin/vdeter` 본문 — 저장소 root·설치 id를 상수로 박고 exec.

    stale 검증: root의 launcher·launcher-id 파일이 없거나 id가 다르면 다른
    경로를 조용히 실행하지 않고 오류로 끝난다 (저장소 이동/교체 감지)."""
    root_q = _sh_single_quote(str(root))
    id_q = _sh_single_quote(launcher_id)
    return "\n".join([
        "#!/bin/sh",
        f"# {USER_LAUNCHER_MARKER}",
        "# VDETER standalone user launcher - generated by `vdeter setup`.",
        "# Idempotently refreshed by setup; never overwrites a foreign vdeter.",
        f"VDETER_ROOT={root_q}",
        f"VDETER_LAUNCHER_ID={id_q}",
        "launcher=\"$VDETER_ROOT/" + REPO_LAUNCHER_RELPATH + "\"",
        "id_file=\"$VDETER_ROOT/var/installation/launcher-id\"",
        "if [ ! -f \"$launcher\" ] || [ ! -f \"$id_file\" ]; then",
        "  echo \"vdeter: installation not found at $VDETER_ROOT (moved or "
        "removed) - run ./bin/vdeter setup from the repository to refresh "
        "this launcher\" >&2",
        "  exit 2",
        "fi",
        "if [ \"$(cat \"$id_file\" 2>/dev/null)\" != \"$VDETER_LAUNCHER_ID\" ]; "
        "then",
        "  echo \"vdeter: stale launcher for $VDETER_ROOT (a different "
        "installation lives there) - run ./bin/vdeter setup there to "
        "refresh\" >&2",
        "  exit 2",
        "fi",
        "exec \"$launcher\" \"$@\"",
        "",
    ])


def install_user_launcher(paths: StatePaths, *, home: Path | str | None = None,
                          path_env: str | None = None) -> dict:
    """`~/.local/bin/vdeter` 설치/갱신. 반환: 경로·상태·PATH 안내."""
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    bin_dir = home / ".local" / "bin"
    target = bin_dir / "vdeter"
    launcher_id = installation_manifest.ensure_launcher_id(paths)
    body = render_user_launcher(paths.root.resolve(), launcher_id)
    status = "installed"
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if USER_LAUNCHER_MARKER not in existing:
            return {"path": str(target), "status": "conflict",
                    "installed": False,
                    "message": (f"{target} exists and is not a VDETER "
                                "launcher — left untouched. Use "
                                f"{paths.root / REPO_LAUNCHER_RELPATH} "
                                "directly or move the foreign file."),
                    "path_hint": None}
        status = "updated" if existing != body else "unchanged"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if status != "unchanged":
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(body, encoding="utf-8", newline="\n")
        try:
            tmp.chmod(0o755)
        except OSError:
            pass
        os.replace(tmp, target)
    path_value = path_env if path_env is not None else os.environ.get("PATH", "")
    entries = [os.path.normpath(p) for p in path_value.split(os.pathsep) if p]
    on_path = os.path.normpath(str(bin_dir)) in entries
    hint = None
    if not on_path:
        hint = ("~/.local/bin is not on PATH. Add this line to ~/.profile "
                "(or ~/.bashrc) and re-login:\n"
                "    export PATH=\"$HOME/.local/bin:$PATH\"\n"
                f"Until then use: {paths.root / REPO_LAUNCHER_RELPATH}")
    return {"path": str(target), "status": status, "installed": True,
            "on_path": on_path, "path_hint": hint, "launcher_id": launcher_id}


# ------------------------------------------------------------ hardware 초안

def draft_hardware_declaration(paths: StatePaths, observed: dict,
                               topology: dict | None = None, *,
                               candidate: dict | None = None,
                               expected=hardware_draft.LAB_DEFAULT) -> dict:
    """hardware.toml 초안 — 부재 시에만. 생성 로직은 `hardware_draft` 한곳이다.

    observed: local_preflight.probe_usb_serials() 결과 (serial→경로·속도).
    topology: host_profiles.probe_usb_topology() 스냅샷 — `candidate`(hub
    후보 판정)를 직접 주면 다시 판정하지 않는다.
    expected: 기대 프로필(기본 실험실 프로필). None이면 기대값 없이 종전
    형태의 초안을 쓴다(다른 환경). 기대값은 관측이 뒷받침하는 항목에만 채우고
    나머지는 REQUIRED_FILL + 사유로 남긴다. 기존 파일은 읽지도 쓰지도 않는다.
    어느 초안도 승인·증언 없이는 검증을 통과하지 않는다 (fail-closed).
    """
    target = paths.root / "hardware.toml"
    if target.is_file():
        return {"created": False, "path": str(target), "profile": None,
                "expected_label": None, "expected_applied": {}, "mismatches": []}
    if candidate is None:
        candidate = (hardware_draft.hub_draft_candidate(topology)
                     if topology else {"candidate": False, "reasons": [], "devices": []})
    draft = hardware_draft.build_hardware_draft(
        observed=observed, candidate=candidate, expected=expected)
    target.write_text(draft.text, encoding="utf-8", newline="\n")
    return {"created": True, "path": str(target), "profile": draft.profile,
            "expected_label": draft.expected_label,
            "expected_applied": draft.expected_applied,
            "mismatches": list(draft.mismatches)}


def run_setup(paths: StatePaths, *, runner=subprocess.run,
              usb_probe=None, apt_install: bool = True,
              make_venv: bool = True, regenerate_identity: bool = False,
              now=None, crypto_available=_cryptography_available,
              install_launcher: bool = True, home: Path | str | None = None,
              progress=None, topology_probe=None,
              expected_hardware=hardware_draft.LAB_DEFAULT) -> dict:
    """setup 전체 — 단계별 결과를 모아 report로 돌려준다.

    실패한 단계에서 중단한다 (부분 성공 상태도 멱등 재실행으로 이어진다).
    `progress`는 선택적 진행 표시 콜백 객체(progress.SetupProgress 호환) —
    None이면 표시 없음. 판정·산출물에는 영향이 없다.
    """
    if usb_probe is None:
        from apps.standalone.local_preflight import probe_usb_serials
        usb_probe = probe_usb_serials

    def step(name, **fields):
        if progress is not None:
            progress.step(name, **fields)

    report = {"steps": {}, "pass": False}
    paths.ensure_var()
    report["steps"]["state_dirs"] = {"pass": True}

    step("apt dependency check")
    apt_plan = plan_apt(runner=runner)
    report["steps"]["apt_plan"] = apt_plan
    if apt_install:
        step("apt install", packages=f"{len(apt_plan['missing'])}/"
             f"{len(apt_plan['required'])}")
        report["steps"]["apt_install"] = install_apt(
            apt_plan["missing"], runner=runner)
    elif apt_plan["missing"]:
        raise SetupError(
            f"apt packages missing: {apt_plan['missing']} — installation is disabled, "
            "cannot continue")

    if make_venv:
        step("venv + pip lock")
        report["steps"]["venv"] = ensure_venv(paths, runner=runner)
        report["steps"]["wrapper"] = write_wrapper(paths)

    # identity 생성은 cryptography가 필요하다. 깨끗한 Ubuntu의 시스템
    # python3에는 없을 수 있다 — venv(lock)가 준비됐으면 그 인터프리터로
    # 재실행해 나머지 단계를 마친다 (첫 실행 부트스트랩).
    if not crypto_available(paths):
        if venv_python(paths).exists():
            raise SetupReexec(venv_python(paths))
        raise SetupError(
            "the runtime requires cryptography (major 50), which the current interpreter "
            "cannot import, and var/venv does not exist — check whether the venv step failed, "
            "then rerun setup")
    step("identity")
    if regenerate_identity:
        report["steps"]["identity"] = identity.new_identity(paths)
    else:
        report["steps"]["identity"] = identity.ensure_identity(paths, now=now)

    step("B210 discovery")
    observed = usb_probe() or {}
    # USB physical topology snapshot (2026-09-10 additive): hub/port/speed per
    # device for the operator record and for choosing the draft profile only;
    # device numbers are recorded for diagnosis and never bound.
    probe_topology = (topology_probe
                      or hardware_profiles.host_profiles.probe_usb_topology)
    try:
        topology = probe_topology() or {}
    except Exception as exc:  # noqa: BLE001 - 기록 실패가 setup을 막지 않는다
        topology = {"devices": {}, "roots": {},
                    "error": f"{type(exc).__name__}: {exc}"}
    hub_candidate = hardware_draft.hub_draft_candidate(topology)
    report["steps"]["b210_discovery"] = {
        "observed": {serial: {"usb_path": info.get("usb_path"),
                              "speed_mbps": info.get("speed_mbps")}
                     for serial, info in sorted(observed.items())},
        "count": len(observed),
        "hub_candidate": hub_candidate,
    }
    step("hardware.toml draft", b210=f"{len(observed)}/3")
    draft = draft_hardware_declaration(
        paths, observed, candidate=hub_candidate, expected=expected_hardware)
    report["steps"]["hardware_draft"] = draft
    # 관측 스냅샷과 초안 결정(기대값 적용·불일치)을 함께 기록한다 — 값은
    # 기록용이며 결속하지 않는다.
    write_json_atomic(paths.var_dir / "setup-usb-topology.json", {
        "artifact_type": "STAGE7_STANDALONE_SETUP_USB_TOPOLOGY_V1",
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": ("sysfs physical topology at setup time; paths/ports/device "
                 "numbers are recorded for reference and never bound"),
        "topology": topology,
        "hub_candidate": hub_candidate,
        "hardware_draft": {
            "created": draft["created"], "profile": draft["profile"],
            "expected_label": draft["expected_label"],
            "expected_applied": draft["expected_applied"],
            "mismatches": draft["mismatches"],
            "note": ("expected values are expectations from the expected profile, "
                     "filled only where the observation supports them; approval and "
                     "attestations are never written by setup"),
        },
    })

    # 설치 manifest (v2): 실측 → var/installation/*. 결속 digest(rf/operation/
    # tooling)가 이전 기록과 같으면 파일을 다시 쓰지 않는다(멱등 — 재실행이
    # 자격을 무효화하지 않는다). 이전 manifest 파일이 있었는데(손상 포함)
    # digest가 달라졌으면 qualification_invalidated=true로 보고한다.
    step("installation manifest")
    inst_dir = installation_manifest.installation_dir(paths)
    had_previous = (inst_dir
                    / installation_manifest.INSTALLATION_MANIFEST).exists()
    previous = None
    if had_previous:
        try:
            previous = installation_manifest.load_installation(paths)
        except installation_manifest.InstallationError:
            previous = None
    record = installation_manifest.measure(paths.root)
    previous_digests = (installation_manifest.digests_of(previous)
                        if previous is not None else None)
    current_digests = installation_manifest.digests_of(record)
    digests_unchanged = previous_digests == current_digests
    files_unchanged = (previous is not None
                       and previous.get("files") == record["files"])
    if previous is not None and files_unchanged:
        written = {**current_digests, "file_count": previous["file_count"],
                   "domain_file_count": {
                       d: previous["domains"][d]["file_count"]
                       for d in installation_manifest.domains.BOUND_DOMAINS}}
        reused = True
    else:
        written = installation_manifest.write_installation(paths, record)
        reused = False
    changed_domains = []
    if previous_digests is not None:
        changed_domains = [d for d in ("rf", "operation", "tooling")
                           if previous_digests[f"{d}_manifest_sha256"]
                           != current_digests[f"{d}_manifest_sha256"]]
    report["steps"]["installation"] = {
        "relpath": installation_manifest.INSTALLATION_DIR,
        "installation_sha256": written["installation_sha256"],
        "rf_manifest_sha256": written["rf_manifest_sha256"],
        "operation_manifest_sha256": written["operation_manifest_sha256"],
        "tooling_manifest_sha256": written["tooling_manifest_sha256"],
        "file_count": written["file_count"],
        "domain_file_count": written["domain_file_count"],
        "reused": reused,
        "changed_domains": changed_domains,
        "qualification_invalidated": had_previous and not digests_unchanged,
    }

    step("environment record")
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

    if install_launcher:
        step("user launcher")
        report["steps"]["user_launcher"] = install_user_launcher(
            paths, home=home)
    report["pass"] = True
    return report
