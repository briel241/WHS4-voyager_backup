"""필수 preflight — 일반 실행의 로컬 검사 (Phase I W3, README §8·§9).

host `verify_remote_environment`(SSH)의 로컬 대응물 중 **필수 preflight**
부분이다 (결정 13). 환경 manifest 정확 일치·baseline 실물 대조는 여기 없다
— qualify/audit 전용이며 W5에서 구현한다.

검사 항목 (하나라도 실패 = RF 접근 전 거부)
    - B210 serial↔역할 매핑 정확 일치 + USB **속도 등급** (포트 경로는
      기록만 — 결정 12)
    - 잔존 프로세스 (발견 시 자동 kill 없이 거부 — 결정 17)
    - identity: 개인키 존재·0600·key-cert 공개키 일치
    - platform 검사: pi = throttled 필수 / generic-pc = 기록만 (W1 수정 3)
    - 디스크 여유 (state.check_disk_space)

모든 probe는 주입 가능하다 — 대상은 Ubuntu지만 unit 시험은 Windows에서
돈다. 기본 구현은 sysfs·pgrep·os.stat을 쓴다.
"""

from __future__ import annotations

import os
import stat as stat_module
import subprocess
from pathlib import Path

from apps.standalone import hardware_profiles
from apps.standalone import state as state_module

#: 역할별 요구 USB 속도 등급 (README §8). super=USB3(5000+), high=480.
#: legacy Pi profile 값 — MASS-A super / MASS-B high / ROC super. 2026-09-10부터
#: 단일 정의(vdeter.stage7.host_profiles.LEGACY_PI)에서 유도하며 값은 종전과
#: 동일하다; 다른 profile(Pi powered-USB3-hub = super/super/super)은
#: run_required_preflight가 hardware.toml의 profile 정의에서 가져온다.
ROLE_SPEED_CLASS = dict(hardware_profiles.host_profiles.LEGACY_PI.speed_classes)

#: host 계층과 같은 잔존 프로세스 패턴 (읽기 전용 재사용).
from apps.stage7_service_layer import _COMPETING_PROCESS_PATTERN  # noqa: E402


class PreflightError(RuntimeError):
    """필수 preflight 거부 — RF 접근 전."""


def speed_class(speed_mbps) -> str:
    try:
        value = int(speed_mbps)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 5000:
        return "super"
    if value == 480:
        return "high"
    return "other"


# ------------------------------------------------------------ USB topology

def probe_usb_serials(sysfs_root: str = "/sys/bus/usb/devices") -> dict:
    """sysfs 실측: {serial: {"usb_path": ..., "speed_mbps": int|None}}."""
    observed = {}
    root = Path(sysfs_root)
    if not root.is_dir():
        return observed
    for device_dir in sorted(root.iterdir()):
        # 루트 허브(usb1, usb3 …)도 serial 파일을 갖는다(xhci-hcd.0 등) —
        # 장치가 아니므로 제외한다 (Pi 실측 2026-09-04: 초안 주석 오염).
        if device_dir.name.startswith("usb"):
            continue
        serial_file = device_dir / "serial"
        if not serial_file.is_file():
            continue
        try:
            serial = serial_file.read_text(
                encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        speed = None
        try:
            speed = int((device_dir / "speed").read_text().strip())
        except (OSError, ValueError):
            pass
        observed[serial] = {"usb_path": device_dir.name, "speed_mbps": speed}
    return observed


def check_b210_roles(declared_serials: dict, observed: dict,
                     speed_classes: dict | None = None) -> dict:
    """serial↔역할 정확 일치 + 속도 등급 강제. 포트 경로는 기록만.

    declared_serials: {"MASS-A": serial, "MASS-B": ..., "ROC": ...}
    observed: probe_usb_serials() 결과.
    speed_classes: 역할→요구 등급. 기본 ROLE_SPEED_CLASS(legacy Pi); 호출자가
    hardware.toml의 host profile 정의에서 가져온 표를 넘긴다.
    """
    speed_classes = speed_classes or ROLE_SPEED_CLASS
    errors = []
    by_role = {}
    for role, serial in sorted(declared_serials.items()):
        seen = observed.get(serial)
        if seen is None:
            errors.append(
                f"{role}: declared serial {serial} is not visible on USB — "
                "check the cable and power")
            by_role[role] = None
            continue
        required = speed_classes[role]
        actual = speed_class(seen.get("speed_mbps"))
        if actual != required:
            errors.append(
                f"{role}: USB speed class mismatch — required {required}, observed "
                f"{actual}({seen.get('speed_mbps')}M). "
                + ("connect it to a USB 3 port" if required == "super"
                   else "it must be a USB 2 (480M) connection"))
        # usb_path는 기록만 한다 (결정 12 — 차단·재자격 사유 아님).
        by_role[role] = {"serial": serial,
                         "usb_path": seen.get("usb_path"),
                         "speed_mbps": seen.get("speed_mbps"),
                         "speed_class": actual}
    return {"pass": not errors, "errors": errors, "observed_by_role": by_role}


# ------------------------------------------------------------ 잔존 프로세스

def residual_processes(runner=subprocess.run, *, which=None):
    """경쟁/잔존 프로세스 목록. probe 불가 환경은 None (미확인 — 호출자가
    fail-closed로 계상한다). `which`는 시험 주입용."""
    if which is None:
        import shutil
        which = shutil.which
    if which("pgrep") is None:
        return None
    done = runner(["pgrep", "-af", _COMPETING_PROCESS_PATTERN],
                  capture_output=True, text=True, timeout=30)
    lines = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line or "pgrep" in line:
            continue
        if line.split()[0] == str(os.getpid()):
            continue
        lines.append(line)
    return lines


# ------------------------------------------------------------ identity

def check_private_key_mode(path: Path | str, *, mode_reader=None) -> dict:
    """개인키 존재 + 권한 0600 (POSIX). mode_reader는 시험 주입용."""
    path = Path(path)
    if mode_reader is None:
        def mode_reader(p):  # noqa: ANN001
            return stat_module.S_IMODE(os.stat(p).st_mode)
    if not path.is_file():
        return {"pass": False, "present": False, "mode": None,
                "error": f"private key missing: {path}"}
    try:
        mode = mode_reader(path)
    except OSError as exc:
        return {"pass": False, "present": True, "mode": None,
                "error": f"private key permission check failed: {exc}"}
    ok = mode == 0o600
    return {"pass": ok, "present": True, "mode": oct(mode),
            "error": None if ok else
            f"private key permissions are not 0600 ({oct(mode)}) — "
            f"`chmod 600 {path}` and retry"}


def check_key_cert_match(key_path: Path | str, cert_path: Path | str):
    """개인키↔인증서 공개키 일치 (cryptography 사용, 로컬).

    반환: True/False, 또는 None(확인 불가 — 호출자가 fail-closed 계상).
    비밀정보는 어떤 산출물에도 기록하지 않는다 — 반환은 진위뿐이다.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    try:
        key = serialization.load_pem_private_key(
            Path(key_path).read_bytes(), password=None)
        certificate = x509.load_pem_x509_certificate(
            Path(cert_path).read_bytes())
    except (OSError, ValueError):
        return None
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    encoding = serialization.Encoding.DER
    return (key.public_key().public_bytes(encoding, fmt)
            == certificate.public_key().public_bytes(encoding, fmt))


# ------------------------------------------------------------ platform

def platform_checks(profile: str, *, runner=subprocess.run) -> dict:
    """pi: throttled=0x0 필수. generic-pc: governor·thermal **기록만**
    (Phase I에서는 정의되지 않은 최소 기준을 강제하지 않는다 — W1 수정 3)."""
    if profile == "pi":
        try:
            done = runner(["vcgencmd", "get_throttled"],
                          capture_output=True, text=True, timeout=15)
            value = (done.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return {"pass": False, "profile": profile, "throttled": None,
                    "error": f"vcgencmd failed: {exc}"}
        ok = value == "throttled=0x0"
        return {"pass": ok, "profile": profile, "throttled": value,
                "error": None if ok else f"Pi throttled state: {value}"}

    # generic-pc — 기록 전용.
    records = {}
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        records["governor"] = governor.read_text().strip()
    except OSError:
        records["governor"] = None
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        records["thermal_zone0_millic"] = int(thermal.read_text().strip())
    except (OSError, ValueError):
        records["thermal_zone0_millic"] = None
    return {"pass": True, "profile": profile, "recorded": records,
            "error": None}


# ------------------------------------------------------------ 집계

def run_required_preflight(*, hardware: dict, paths, key_path=None,
                           cert_path=None,
                           usb_probe=probe_usb_serials,
                           residual_probe=residual_processes,
                           platform_probe=platform_checks,
                           key_mode_check=check_private_key_mode,
                           key_cert_check=check_key_cert_match,
                           disk_check=state_module.check_disk_space,
                           topology_probe=None) -> dict:
    """필수 preflight 일습 (결정 13). 반환 report의 `pass`가 게이트다.

    reference critical subset 대조(UNQUALIFIED 판정)는 이 함수 밖이다 —
    reference 적재기가 있는 계층(run_cmd, W5/W6)에서 이 report와 결합한다.

    host profile(2026-09-10 additive): 역할별 요구 속도 등급은 hardware.toml이
    가리키는 profile 정의(vdeter.stage7.host_profiles)에서 가져온다 — legacy
    Pi = super/high/super(종전과 동일), Pi powered-USB3-hub = super/super/super
    + 같은 self-powered USB3 hub 아래 topology 검사(`checks.usb_topology`,
    fail-closed; `topology_probe`는 시험 주입용 sysfs 스냅샷).
    """
    definition = hardware_profiles.definition_of(hardware)
    declared = {role: (hardware.get("endpoints") or {}).get(role, {}).get(
        "serial") for role in ("MASS-A", "MASS-B", "ROC")}
    report = {"checks": {}, "host_profile": definition.name}
    failures = []

    usb = check_b210_roles(declared, usb_probe(),
                           speed_classes=definition.speed_classes)
    report["checks"]["b210"] = usb
    failures.extend(usb["errors"])

    if definition.shared_powered_hub:
        probe = topology_probe or hardware_profiles.host_profiles.probe_usb_topology
        hub_vid_pid = (hardware.get("usb_hub") or {}).get("vid_pid")
        try:
            topology = hardware_profiles.host_profiles.check_shared_powered_hub(
                declared, probe(), hub_vid_pid=hub_vid_pid)
        except Exception as exc:  # noqa: BLE001 - 관측 실패도 fail-closed
            topology = {"pass": False, "host_profile": definition.name,
                        "errors": [f"USB topology probe failed: {type(exc).__name__}: {exc}"],
                        "hub": None, "observed_by_role": {}}
        report["checks"]["usb_topology"] = topology
        failures.extend(topology["errors"])

    residual = residual_probe()
    report["checks"]["residual_processes"] = residual
    if residual is None:
        failures.append("cannot check residual processes (pgrep missing)")
    elif residual:
        failures.append(
            f"residual processes found: {residual[:3]} — not terminated automatically; "
            "inspect and clean them up, then retry")

    # identity 파일명은 identity 모듈이 단일 출처다 (W4 보완 1) —
    # setup이 만든 실제 파일과 preflight가 검사하는 파일이 항상 같다.
    from apps.standalone.identity import identity_paths
    identity_files = identity_paths(paths)
    key_path = key_path or identity_files["key"]
    cert_path = cert_path or identity_files["certificate"]
    key_report = key_mode_check(key_path)
    report["checks"]["private_key"] = key_report
    if not key_report["pass"]:
        failures.append(key_report["error"])
    else:
        match = key_cert_check(key_path, cert_path)
        report["checks"]["key_cert_match"] = match
        if match is None:
            failures.append("cannot verify key-certificate match")
        elif match is False:
            failures.append("the private key does not match the certificate's public key")

    plat = platform_probe(hardware.get("platform_profile"))
    report["checks"]["platform"] = plat
    if not plat["pass"]:
        failures.append(plat["error"])

    disk = disk_check(paths.var_dir)
    report["checks"]["disk"] = disk
    if disk["status"] == "refuse":
        failures.append(
            f"insufficient free disk space ({disk['free_bytes']} bytes) — move or clean up evidence "
            "manually, then retry")

    report["failures"] = failures
    report["pass"] = not failures
    return report
