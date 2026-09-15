"""Approved Ubuntu apt host-environment gate for physical B210 tests."""

from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path


APPROVED_OS_ID = "ubuntu"
APPROVED_OS_VERSION_ID = "24.04"
APPROVED_MACHINES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
# Retained for additive import compatibility with the v0.2.1 environment API.
APPROVED_MACHINE = "x86_64"
APPROVED_RASPBERRY_PI_MODEL_PREFIX = "Raspberry Pi 5"
APPROVED_PYTHON = (3, 12)
APPROVED_GNURADIO_VERSION = "3.10.9.2"
APPROVED_UHD_VERSION = "4.6.0.0+ds1-5.1ubuntu0.24.04.1"
APT_PROFILE_ID = "VDETER-STAGE3-B210-UBUNTU-APT-002"
APT_PACKAGE_GROUPS = {
    "python": ("python3",),
    "gnuradio": ("gnuradio",),
    "gnuradio_uhd": (
        "libgnuradio-uhd3.10.9",
        "libgnuradio-uhd3.10.9t64",
    ),
    "uhd_host": ("uhd-host",),
    "uhd_runtime": ("libuhd4.6.0", "libuhd4.6.0t64"),
}


def host_environment_snapshot():
    """Return portable host facts used by the hardware gate and evidence."""

    try:
        release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        release = {}
    try:
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        model = ""
    try:
        detected = subprocess.run(
            ["systemd-detect-virt"], capture_output=True, text=True, check=False
        )
        virtualization = (
            detected.stdout.strip().lower() if detected.returncode == 0 else "none"
        )
    except OSError:
        virtualization = "unknown"
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "kernel_release": platform.release(),
        "virtualization": virtualization,
        "hardware_model": model,
        "os_id": release.get("ID", ""),
        "os_version_id": release.get("VERSION_ID", ""),
        "os_pretty_name": release.get("PRETTY_NAME", ""),
        "python_version": platform.python_version(),
    }


def approved_host_environment_issues(snapshot=None):
    """Return all mismatches instead of accepting a different host profile."""

    value = host_environment_snapshot() if snapshot is None else dict(snapshot)
    issues = []
    if value.get("system") != "Linux":
        issues.append("system must be Linux")
    if str(value.get("os_id", "")).lower() != APPROVED_OS_ID:
        issues.append("distribution must be Ubuntu")
    if value.get("os_version_id") != APPROVED_OS_VERSION_ID:
        issues.append("Ubuntu VERSION_ID must be 24.04")
    machine = str(value.get("machine", "")).lower()
    if machine not in APPROVED_MACHINES:
        issues.append("machine must be x86_64/amd64 or Raspberry Pi 5 aarch64/arm64")
    kernel_release = str(value.get("kernel_release", "")).lower()
    virtualization = str(value.get("virtualization", "")).lower()
    if "microsoft" in kernel_release or virtualization in {"wsl", "wsl2"}:
        issues.append("WSL is not an approved physical B210 host")
    if machine in {"aarch64", "arm64"} and not str(
        value.get("hardware_model", "")
    ).startswith(APPROVED_RASPBERRY_PI_MODEL_PREFIX):
        issues.append("arm64 physical B210 host must be Raspberry Pi 5")
    python_parts = str(value.get("python_version", "")).split(".")
    try:
        python_pair = tuple(map(int, python_parts[:2]))
    except ValueError:
        python_pair = ()
    if python_pair != APPROVED_PYTHON:
        issues.append("Python must be 3.12.x")
    return tuple(issues)


def _dpkg_record(package):
    completed = subprocess.run(
        [
            "dpkg-query", "-W",
            "-f=${db:Status-Status}\t${binary:Package}\t${Version}\t${Architecture}",
            package,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    fields = completed.stdout.strip().split("\t")
    if len(fields) != 4 or fields[0] != "installed":
        return None
    return {
        "name": fields[1],
        "version": fields[2],
        "architecture": fields[3],
    }


def live_apt_package_snapshot():
    """Collect one installed package for every approved package role."""

    records = {}
    issues = []
    for role, candidates in APT_PACKAGE_GROUPS.items():
        matches = [value for value in map(_dpkg_record, candidates) if value]
        if len(matches) != 1:
            issues.append(
                f"apt package role {role} must have exactly one installed candidate: "
                + ",".join(candidates)
            )
            continue
        records[role] = matches[0]
    return records, tuple(issues)


def live_apt_holds():
    completed = subprocess.run(
        ["apt-mark", "showhold"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return (), ("apt-mark showhold failed",)
    values = tuple(sorted(
        value.strip() for value in completed.stdout.splitlines() if value.strip()
    ))
    return values, ()


def live_raspberry_pi_usb_snapshot(host=None):
    """Capture the Pi 5 power and USB 3 gate used by two physical B210s."""

    value = host_environment_snapshot() if host is None else dict(host)
    if str(value.get("machine", "")).lower() not in {"aarch64", "arm64"}:
        return {"applicable": False}, ()
    snapshot = {"applicable": True}
    issues = []
    commands = {
        "usb_max_current": ["vcgencmd", "get_config", "usb_max_current_enable"],
        "throttled": ["vcgencmd", "get_throttled"],
        "usb_topology": ["lsusb", "-t"],
    }
    for name, command in commands.items():
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
        except OSError:
            issues.append(f"Raspberry Pi preflight command is unavailable: {command[0]}")
            snapshot[name] = ""
            continue
        output = (completed.stdout + completed.stderr).strip()
        snapshot[name] = output
        if completed.returncode != 0:
            issues.append(f"Raspberry Pi preflight command failed: {' '.join(command)}")
    if "usb_max_current_enable=1" not in snapshot.get("usb_max_current", ""):
        issues.append(
            "Raspberry Pi 5 USB high-current mode is required for two bus-powered B210s"
        )
    if "throttled=0x0" not in snapshot.get("throttled", "").lower():
        issues.append("Raspberry Pi 5 reports current or historical power/throttle faults")
    if snapshot.get("usb_topology", "").count("5000M") < 2:
        issues.append("two USB 3/SuperSpeed device links are required on Raspberry Pi 5")
    return snapshot, tuple(issues)


def apt_package_snapshot_issues(packages, machine=None):
    """Validate package roles, architecture and approved Ubuntu versions."""

    issues = []
    if set(packages) != set(APT_PACKAGE_GROUPS):
        issues.append("apt package role set is incomplete")
        return tuple(issues)
    normalized_machine = str(machine or "").lower()
    expected_architecture = APPROVED_MACHINES.get(normalized_machine)
    for role, record in packages.items():
        if not isinstance(record, dict) or set(record) != {
            "name", "version", "architecture"
        }:
            issues.append(f"apt package record is malformed: {role}")
            continue
        if record["architecture"] not in {"amd64", "arm64", "all"}:
            issues.append(f"apt package architecture is not amd64/arm64/all: {role}")
        elif expected_architecture and record["architecture"] not in {
            expected_architecture, "all"
        }:
            issues.append(
                f"apt package architecture does not match host {expected_architecture}: {role}"
            )
        if record["name"].split(":", 1)[0] not in APT_PACKAGE_GROUPS[role]:
            issues.append(f"unexpected apt package name: {role}")
    if packages.get("gnuradio", {}).get("version") != "3.10.9.2-1.1ubuntu2":
        issues.append("gnuradio apt package must be 3.10.9.2-1.1ubuntu2")
    if packages.get("gnuradio_uhd", {}).get("version") != "3.10.9.2-1.1ubuntu2":
        issues.append("gnuradio-uhd apt package must be 3.10.9.2-1.1ubuntu2")
    for role in ("uhd_host", "uhd_runtime"):
        if packages.get(role, {}).get("version") != APPROVED_UHD_VERSION:
            issues.append(f"{role} apt package must be {APPROVED_UHD_VERSION}")
    if not str(packages.get("python", {}).get("version", "")).startswith("3.12"):
        issues.append("python3 apt package must be 3.12.x")
    return tuple(issues)


def load_apt_package_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Ubuntu apt package manifest is unreadable") from error
    required = {
        "schema_version", "profile_id", "created_at_utc", "outcome",
        "host_environment", "runtime_versions", "packages", "held_packages",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Ubuntu apt package manifest fields are invalid")
    return value


def apt_package_manifest_issues(
    manifest, host, runtime_versions, live_packages, live_holds
):
    issues = []
    if manifest.get("schema_version") != 1:
        issues.append("apt manifest schema_version must be 1")
    if manifest.get("profile_id") != APT_PROFILE_ID:
        issues.append("apt manifest profile_id is invalid")
    if manifest.get("outcome") != "PASS":
        issues.append("apt manifest outcome must be PASS")
    manifest_host = manifest.get("host_environment", {})
    for name in (
        "system", "machine", "kernel_release", "virtualization",
        "hardware_model", "os_id", "os_version_id",
    ):
        if manifest_host.get(name) != host.get(name):
            issues.append(f"apt manifest host mismatch: {name}")
    if manifest.get("runtime_versions") != runtime_versions:
        issues.append("apt manifest runtime versions do not match")
    if manifest.get("packages") != live_packages:
        issues.append("apt manifest packages do not match installed packages")
    issues.extend(apt_package_snapshot_issues(live_packages, host.get("machine")))
    held = tuple(sorted(manifest.get("held_packages", ())))
    required_holds = tuple(sorted(
        value["name"].split(":", 1)[0] for value in live_packages.values()
    ))
    if held != required_holds:
        issues.append("apt manifest held package set is incomplete")
    if any(value not in live_holds for value in required_holds):
        issues.append("approved apt packages are not currently held")
    return tuple(issues)
