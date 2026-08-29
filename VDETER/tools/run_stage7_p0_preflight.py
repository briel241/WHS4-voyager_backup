"""Stage 7 P0 RF-off preflight 실행기 (Pi 전용; `stage7` §9.1.1, §17.1 P0).

단일 Raspberry Pi 5에서 §9.1.1 mixed-USB 실행환경 자격 gate와 세 B210 동시
discovery→configure→start→stop 10회를 수행한다. **RF 송신은 절대 하지 않는다**
(TX burst 제출 0; timed stream command 0).

사용법 (Pi의 배포 tree에서):
    python3 tools/run_stage7_p0_preflight.py --run-id stage7-p0-<date> \
        --cycles 10 --output results/runs/<run-id>/p0_preflight.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

EXPECTED_DEVICES = {
    "MASS-A": {"serial": "34D03DD", "usb_path": "3-1", "speed": "5000"},
    "ROC": {"serial": "34D03E8", "usb_path": "5-1", "speed": "5000"},
    "MASS-B": {"serial": "3515425", "usb_path": "4-2", "speed": "480"},
}

LOWER_TUNE = 2_447_237_500
UPPER_TUNE = 2_451_837_500


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip().replace("\x00", "")
    except OSError:
        return None


def _read_bytes_hex(path: str) -> str | None:
    """binary device-tree 값은 텍스트 디코드로 byte가 소실되므로 hex로 보존한다."""

    try:
        with open(path, "rb") as handle:
            return handle.read().hex()
    except OSError:
        return None


def _cmd(*args) -> str | None:
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=30, check=False
        )
    except Exception:  # noqa: BLE001
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def environment_snapshot() -> dict:
    usb = {}
    base = "/sys/bus/usb/devices"
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else ():
        vendor = _read(f"{base}/{name}/idVendor")
        if vendor != "2500":
            continue
        usb[name] = {
            "serial": _read(f"{base}/{name}/serial"),
            "speed": _read(f"{base}/{name}/speed"),
            "busnum": _read(f"{base}/{name}/busnum"),
            "devpath": _read(f"{base}/{name}/devpath"),
        }
    numpy_version = None
    try:
        import numpy

        numpy_version = numpy.__version__
    except Exception:  # noqa: BLE001
        pass
    return {
        "snapshot_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "os_release": _read("/etc/os-release"),
        "kernel": _cmd("uname", "-r"),
        "machine": _cmd("uname", "-m"),
        "pi_model": _read("/proc/device-tree/model"),
        "machine_id": _read("/etc/machine-id"),
        "throttled": _cmd("vcgencmd", "get_throttled"),
        "temperature": _cmd("vcgencmd", "measure_temp"),
        "ext5v": _cmd("vcgencmd", "pmic_read_adc", "EXT5V_V"),
        "usb_max_current_enable": _cmd("vcgencmd", "get_config", "usb_max_current_enable"),
        "psu_max_current_raw": _read_bytes_hex("/proc/device-tree/chosen/power/max_current"),
        "python": sys.version.split()[0],
        "numpy": numpy_version,
        "uhd": _cmd("bash", "-c", "uhd_config_info --version 2>/dev/null | head -1"),
        "usb_b210": usb,
        "virtualization": _cmd("systemd-detect-virt") or "none",
    }


def check_environment(snapshot: dict) -> list:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": str(detail)[:400]})

    os_release = snapshot.get("os_release") or ""
    check("ubuntu_2404_native", 'VERSION_ID="24.04"' in os_release, "os-release")
    check("arm64", snapshot.get("machine") == "aarch64", snapshot.get("machine"))
    check(
        "pi5_model",
        "Raspberry Pi 5" in (snapshot.get("pi_model") or ""),
        snapshot.get("pi_model"),
    )
    check(
        "virtualization_none",
        (snapshot.get("virtualization") or "none") == "none",
        snapshot.get("virtualization"),
    )
    check(
        "throttled_zero",
        snapshot.get("throttled") == "throttled=0x0",
        snapshot.get("throttled"),
    )
    check(
        "usb_high_current",
        snapshot.get("usb_max_current_enable") == "usb_max_current_enable=1",
        snapshot.get("usb_max_current_enable"),
    )
    raw = snapshot.get("psu_max_current_raw")
    psu_ok = False
    if raw is not None:
        try:
            data = bytes.fromhex(raw)
        except ValueError:
            data = b""
        if len(data) >= 4:
            value = int.from_bytes(data[:4], "big", signed=False)
            psu_ok = value == 5000
    check("psu_5a_negotiated", psu_ok, raw)
    check(
        "uhd_apt_stack",
        "4.6.0" in (snapshot.get("uhd") or ""),
        snapshot.get("uhd"),
    )
    check("numpy_1264", snapshot.get("numpy") == "1.26.4", snapshot.get("numpy"))

    usb = snapshot.get("usb_b210", {})
    for endpoint_id, expected in EXPECTED_DEVICES.items():
        entry = usb.get(expected["usb_path"])
        ok = (
            entry is not None
            and entry.get("serial") == expected["serial"]
            and entry.get("speed") == expected["speed"]
        )
        check(
            f"usb_{endpoint_id}_{expected['serial']}",
            ok,
            json.dumps(entry, ensure_ascii=False),
        )
    return checks


def worker_plan(endpoint_id: str) -> dict:
    tx_lo = UPPER_TUNE if endpoint_id == "ROC" else LOWER_TUNE
    rx_lo = LOWER_TUNE if endpoint_id == "ROC" else UPPER_TUNE
    return {
        "master_clock_rate_hz": 30_720_000,
        "cpu_format": "fc32",
        "otw_format": "sc16",
        "sample_rate_hz": 307_200,
        "analog_bandwidth_hz": 200_000,
        "tx_lo_hz": tx_lo,
        "rx_lo_hz": rx_lo,
        "tx_gain_db": [0, 0],
        "rx_gain_db": [0, 0],
        "rx_queue_max_bytes": 67_108_864,
        "device_sample_rate_hz": 307_200,
    }


def run_cycle(cycle_index: int) -> dict:
    from vdeter.stage7.b210_worker import Stage7IsolatedDualLaneWorker

    outcome = {"cycle": cycle_index, "devices": {}, "pass": True}
    workers = {}
    try:
        for endpoint_id, expected in EXPECTED_DEVICES.items():
            selector = f"serial={expected['serial']}"
            found = Stage7IsolatedDualLaneWorker.discover(selector)
            device = {"discovered": list(found)}
            if tuple(found) != (expected["serial"],):
                device["error"] = "discovery mismatch"
                outcome["pass"] = False
                outcome["devices"][endpoint_id] = device
                continue
            worker = Stage7IsolatedDualLaneWorker(selector, "fc32", "sc16")
            workers[endpoint_id] = worker
            readback = worker.configure_dual(worker_plan(endpoint_id))
            device["readback_serial"] = readback.get("serial")
            device["clock_source"] = readback.get("clock_source")
            device["time_source"] = readback.get("time_source")
            device["same_timebase"] = readback.get("same_timebase")
            device["hardware_time_reset"] = readback.get("hardware_time_reset")
            device["tx_lo_hz"] = readback.get("tx_lo_hz")
            device["rx_lo_hz"] = readback.get("rx_lo_hz")
            device["mcr"] = readback.get("master_clock_rate_hz")
            device["lanes"] = readback.get("lanes")
            device["readback_sha256"] = hashlib.sha256(
                json.dumps(readback, sort_keys=True).encode("utf-8")
            ).hexdigest()
            checks_ok = (
                readback.get("serial") == expected["serial"]
                and readback.get("clock_source") == "internal"
                and readback.get("time_source") == "internal"
                and readback.get("same_timebase") is True
            )
            if not checks_ok:
                device["error"] = "readback mismatch"
                outcome["pass"] = False
            worker.connect()
            worker.start()
            t1 = worker.hardware_time_ns()
            time.sleep(0.2)
            t2 = worker.hardware_time_ns()
            device["hardware_monotonic"] = t2 > t1
            if t2 <= t1:
                device["error"] = "hardware time not monotonic"
                outcome["pass"] = False
            outcome["devices"][endpoint_id] = device
    except Exception as error:  # noqa: BLE001
        outcome["pass"] = False
        outcome["exception"] = f"{type(error).__name__}: {error}"
    finally:
        for endpoint_id, worker in workers.items():
            try:
                closed = worker.close(10_000_000_000)
                outcome["devices"].setdefault(endpoint_id, {})["closed"] = bool(closed)
            except Exception as error:  # noqa: BLE001
                outcome["devices"].setdefault(endpoint_id, {})["closed"] = False
                outcome["pass"] = False
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--output", default=None)
    arguments = parser.parse_args(argv)

    output = arguments.output or os.path.join(
        VDETER_ROOT, "results", "runs", arguments.run_id, "p0_preflight.json"
    )
    os.makedirs(os.path.dirname(output), exist_ok=True)

    before = environment_snapshot()
    environment_checks = check_environment(before)
    cycles = []
    all_pass = all(check["pass"] for check in environment_checks)
    for index in range(arguments.cycles):
        print(f"[cycle] {index + 1}/{arguments.cycles}", flush=True)
        outcome = run_cycle(index)
        cycles.append(outcome)
        all_pass = all_pass and outcome["pass"]
        throttled = _cmd("vcgencmd", "get_throttled")
        cycles[-1]["throttled_after"] = throttled
        if throttled != "throttled=0x0":
            all_pass = False
    after = environment_snapshot()
    # USB path/speed/serial 이동 0 검증.
    usb_stable = before.get("usb_b210") == after.get("usb_b210")
    result = {
        "result_type": "STAGE7_P0_PREFLIGHT_V1",
        "run_id": arguments.run_id,
        "started_environment": before,
        "environment_checks": environment_checks,
        "cycles": cycles,
        "finished_environment": after,
        "usb_topology_stable": usb_stable,
        "rf_tx_performed": False,
        "pass": all_pass and usb_stable,
    }
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    print(f"[p0-preflight] {'PASS' if result['pass'] else 'FAIL'} -> {output}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
