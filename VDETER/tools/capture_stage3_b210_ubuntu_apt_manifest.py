"""Capture the approved Ubuntu apt SDR stack without opening a B210."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for path in (ROOT, SRC):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.radio.b210 import (  # noqa: E402
    APT_PROFILE_ID,
    APPROVED_GNURADIO_VERSION,
    APPROVED_UHD_VERSION,
    apt_package_snapshot_issues,
    approved_host_environment_issues,
    host_environment_snapshot,
    live_apt_holds,
    live_apt_package_snapshot,
)


def run(output):
    try:
        from gnuradio import gr, uhd
    except (ImportError, ModuleNotFoundError) as error:
        print(json.dumps({
            "outcome": "FAIL",
            "issues": [f"GNU Radio/gr-uhd import failed: {type(error).__name__}"],
        }, ensure_ascii=False))
        return 2
    host = host_environment_snapshot()
    packages, package_collection_issues = live_apt_package_snapshot()
    holds, hold_collection_issues = live_apt_holds()
    runtime_versions = {
        "python": platform.python_version(),
        "gnuradio": str(gr.version()),
        "uhd": str(getattr(uhd, "get_version_string", lambda: "")()),
    }
    required_holds = tuple(sorted(
        value["name"].split(":", 1)[0] for value in packages.values()
    ))
    issues = list(approved_host_environment_issues(host))
    issues.extend(package_collection_issues)
    issues.extend(hold_collection_issues)
    issues.extend(apt_package_snapshot_issues(packages, host.get("machine")))
    if runtime_versions["gnuradio"] != APPROVED_GNURADIO_VERSION:
        issues.append("GNU Radio runtime version does not match")
    if runtime_versions["uhd"] != APPROVED_UHD_VERSION:
        issues.append("UHD runtime version does not match")
    if any(value not in holds for value in required_holds):
        issues.append("approved packages must be held with apt-mark")
    payload = {
        "schema_version": 1,
        "profile_id": APT_PROFILE_ID,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "outcome": "PASS" if not issues else "FAIL",
        "host_environment": host,
        "runtime_versions": runtime_versions,
        "packages": packages,
        "held_packages": list(required_holds),
    }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        "output": os.path.abspath(output),
        "outcome": payload["outcome"],
        "issues": issues,
        "required_hold_command": (
            "sudo apt-mark hold " + " ".join(required_holds)
            if required_holds else None
        ),
    }, ensure_ascii=False, indent=2))
    return 0 if not issues else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    return run(parser.parse_args().output)


if __name__ == "__main__":
    raise SystemExit(main())
