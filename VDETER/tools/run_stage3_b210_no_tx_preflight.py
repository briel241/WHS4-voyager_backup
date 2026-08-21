"""Validate the approved apt stack and B210 configure/readback without starting RF."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import sys
from datetime import datetime, timezone
from enum import Enum


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for path in (ROOT, SRC):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.common.contracts.radio import RadioConfigureRequest, RadioStopRequest  # noqa: E402
from vdeter.config.loader import load_configuration_file  # noqa: E402
from vdeter.radio.b210 import (  # noqa: E402
    APPROVED_GNURADIO_VERSION,
    APPROVED_UHD_VERSION,
    B210RadioAdapter,
    apt_package_manifest_issues,
    approved_host_environment_issues,
    host_environment_snapshot,
    live_apt_holds,
    live_apt_package_snapshot,
    live_raspberry_pi_usb_snapshot,
    load_apt_package_manifest,
)


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return {"byte_count": len(value)}
    return value


def _write_json_output(output_path, payload):
    absolute_output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(absolute_output_path), exist_ok=True)
    with open(absolute_output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return absolute_output_path


def _failed_device_summaries(records):
    return [
        {
            "role": record["role"],
            "serial": record["serial"],
            "configure_result": record["configure_result"],
            "cleanup_result": record["cleanup_result"],
        }
        for record in records
        if not record["configured"] or not record["cleanup_ok"]
    ]


def _environment(package_manifest_path):
    try:
        from gnuradio import gr, uhd
    except (ImportError, ModuleNotFoundError) as error:
        return None, (f"GNU Radio/gr-uhd import failed: {type(error).__name__}",)
    host = host_environment_snapshot()
    packages, package_issues = live_apt_package_snapshot()
    holds, hold_issues = live_apt_holds()
    runtime = {
        "python": platform.python_version(),
        "gnuradio": str(gr.version()),
        "uhd": str(getattr(uhd, "get_version_string", lambda: "")()),
    }
    issues = list(approved_host_environment_issues(host))
    issues.extend(package_issues)
    issues.extend(hold_issues)
    raspberry_pi_usb, raspberry_pi_usb_issues = live_raspberry_pi_usb_snapshot(host)
    issues.extend(raspberry_pi_usb_issues)
    try:
        manifest = load_apt_package_manifest(package_manifest_path)
    except ValueError as error:
        issues.append(str(error))
        manifest = {}
    issues.extend(apt_package_manifest_issues(
        manifest, host, runtime, packages, holds
    ))
    if runtime["gnuradio"] != APPROVED_GNURADIO_VERSION:
        issues.append("GNU Radio runtime version does not match")
    if runtime["uhd"] != APPROVED_UHD_VERSION:
        issues.append("UHD runtime version does not match")
    return {
        "host_environment": host,
        "runtime_versions": runtime,
        "apt_packages": packages,
        "apt_held_packages": holds,
        "raspberry_pi_usb": raspberry_pi_usb,
        "package_manifest_path": os.path.abspath(package_manifest_path),
    }, tuple(issues)


def _configuration(role, serial):
    path = os.path.join(ROOT, "configs", f"stage3_b210_{role}.toml")
    loaded = load_configuration_file(path)
    if loaded.is_err:
        raise RuntimeError(f"{role} configuration load failed: {loaded.error}")
    return dataclasses.replace(
        loaded.value.radio, device_selector=f"serial={serial}"
    )


def run(args):
    if args.ship_serial == args.control_serial:
        print(json.dumps({"outcome": "FAIL", "issues": ["serials must differ"]}))
        return 2
    environment, issues = _environment(args.package_manifest)
    if issues:
        print(json.dumps({
            "outcome": "BLOCKED", "reason": "environment gate failed",
            "issues": issues, "environment": environment,
        }, ensure_ascii=False, indent=2))
        return 2

    records = []
    for role, serial in (
        ("ship", args.ship_serial), ("control", args.control_serial)
    ):
        adapter = B210RadioAdapter()
        configured = None
        stopped = None
        try:
            configured = adapter.configure(RadioConfigureRequest(
                _configuration(role, serial)
            ))
        finally:
            stopped = adapter.stop(RadioStopRequest("no-TX preflight cleanup"))
        record = {
            "role": role,
            "serial": serial,
            "configured": configured is not None and configured.is_ok,
            "configure_result": _jsonable(
                configured.value if configured is not None and configured.is_ok
                else configured.error if configured is not None else None
            ),
            "cleanup_ok": stopped.is_ok,
            "cleanup_result": _jsonable(
                stopped.value if stopped.is_ok else stopped.error
            ),
        }
        records.append(record)
        if not record["configured"] or not record["cleanup_ok"]:
            break

    outcome = "PASS" if len(records) == 2 and all(
        value["configured"] and value["cleanup_ok"] for value in records
    ) else "FAIL"
    payload = {
        "schema_version": 1,
        "preflight_id": "VDETER-STAGE3-B210-NO-TX-PREFLIGHT-001",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "claims": {
            "flowgraph_started": False,
            "rf_samples_transmitted": False,
            "tc_radio_012_pass": False,
            "tc_radio_013_pass": False,
        },
        "environment": environment,
        "devices": records,
    }
    absolute_output_path = _write_json_output(args.output, payload)
    failures = _failed_device_summaries(records)
    print(json.dumps({
        "output": absolute_output_path, "outcome": outcome,
        "devices_checked": len(records), "rf_started": False,
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    return 0 if outcome == "PASS" else 2


def main():
    parser = argparse.ArgumentParser(
        description="RF disconnected only: configure/readback/cleanup without start()"
    )
    parser.add_argument("--ship-serial", required=True)
    parser.add_argument("--control-serial", required=True)
    parser.add_argument("--package-manifest", required=True)
    parser.add_argument("--output", required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
