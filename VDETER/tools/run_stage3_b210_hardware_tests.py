"""Run the explicitly armed physical B210 gate and write hardware evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone


VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
for path in (VDETER_ROOT, SRC_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.radio.b210 import (  # noqa: E402
    APPROVED_GNURADIO_VERSION,
    APPROVED_UHD_VERSION,
    apt_package_manifest_issues,
    approved_host_environment_issues,
    host_environment_snapshot,
    live_apt_holds,
    live_apt_package_snapshot,
    live_raspberry_pi_usb_snapshot,
    load_apt_package_manifest,
)

RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
MODULE_NAME = "tests.hardware.test_stage3_b210_hardware"
CASE_PATTERN = re.compile(r"TC_RADIO_(012|013)")
REQUIRED_ENVIRONMENT = (
    "VDETER_B210_HARDWARE_TEST",
    "VDETER_B210_USB_DISCONNECT_TEST",
    "VDETER_B210_SHIP_SERIAL",
    "VDETER_B210_CONTROL_SERIAL",
    "VDETER_B210_FIXTURE_MANIFEST",
    "VDETER_B210_PACKAGE_MANIFEST",
)
EXPECTED_MODES = {
    "TC-RADIO-012": {
        "lifecycle": 20,
        "negative_admission": 1,
        "no_tx_window": 1,
        "controlled_usb_disconnect": 1,
        "async_prequeue": 1,
        "async_error_distinction": 1,
    },
    "TC-RADIO-013": {
        "lower": 100,
        "upper": 100,
        "full_duplex": 100,
        "timed_retune_upper_rx": 1,
    },
}


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started_at = {}

    def startTest(self, test):
        self.started_at[test.id()] = datetime.now(timezone.utc).isoformat()
        super().startTest(test)

    def _append(self, test, outcome, reason=""):
        match = CASE_PATTERN.search(test.id())
        self.records.append({
            "test_id": test.id(),
            "test_case": f"TC-RADIO-{match.group(1)}" if match else "UNMAPPED",
            "outcome": outcome,
            "reason": reason,
            "started_at": self.started_at.pop(test.id(), None),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._append(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._append(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._append(test, "ERROR", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._append(test, "SKIP", reason)


def _refusal_reason():
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    invalid_flags = [
        name for name in REQUIRED_ENVIRONMENT[:2]
        if os.environ.get(name) not in (None, "1")
    ]
    manifest = os.environ.get("VDETER_B210_FIXTURE_MANIFEST")
    if manifest and not os.path.isfile(manifest):
        missing.append("VDETER_B210_FIXTURE_MANIFEST(existing file)")
    package_manifest = os.environ.get("VDETER_B210_PACKAGE_MANIFEST")
    if package_manifest and not os.path.isfile(package_manifest):
        missing.append("VDETER_B210_PACKAGE_MANIFEST(existing file)")
    if missing or invalid_flags:
        return {"missing": missing, "invalid_flags": invalid_flags}
    if os.environ["VDETER_B210_SHIP_SERIAL"] == os.environ["VDETER_B210_CONTROL_SERIAL"]:
        return {"serial_error": "ship and control serials must differ"}
    return None


def _probe_devices():
    executable = shutil.which("uhd_usrp_probe") or shutil.which("uhd_usrp_probe.exe")
    if executable is None:
        return None, {"probe_error": "uhd_usrp_probe is not available on PATH"}
    probes = {}
    for role, serial in (
        ("ship", os.environ["VDETER_B210_SHIP_SERIAL"]),
        ("control", os.environ["VDETER_B210_CONTROL_SERIAL"]),
    ):
        try:
            completed = subprocess.run(
                [executable, "--args", f"serial={serial}"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, {"probe_error": type(error).__name__, "role": role}
        output = completed.stdout + completed.stderr
        if completed.returncode != 0:
            return None, {
                "probe_error": "nonzero exit",
                "role": role,
                "returncode": completed.returncode,
                "output_tail": output[-2000:],
            }
        version_lines = [
            line.strip() for line in output.splitlines()
            if (
                "firmware" in line.lower()
                or "fw_version" in line.lower()
                or "fw version" in line.lower()
                or "fpga" in line.lower()
            )
        ]
        if not version_lines:
            return None, {
                "probe_error": "firmware/FPGA version lines were not found",
                "role": role,
            }
        usb_lines = [
            line.strip() for line in output.splitlines()
            if "usb" in line.lower()
        ]
        if not any(
            value in line.lower()
            for line in usb_lines
            for value in ("usb 3", "usb3", "superspeed")
        ):
            return None, {
                "probe_error": "USB 3/SuperSpeed evidence was not found",
                "role": role,
                "usb_lines": usb_lines,
            }
        probes[role] = {
            "serial": serial,
            "output": output,
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "firmware_fpga_lines": version_lines,
            "usb_lines": usb_lines,
        }
    return probes, None


def _case(case_id, records, evidence):
    tests = [record for record in records if record["test_case"] == case_id]
    failures = [record for record in tests if record["outcome"] != "PASS"]
    observed_modes = {}
    for record in evidence:
        if record.get("test_case") == case_id:
            mode = record.get("mode")
            observed_modes[mode] = observed_modes.get(mode, 0) + 1
    expected_modes = EXPECTED_MODES[case_id]
    evidence_complete = observed_modes == expected_modes
    return {
        "test_case": case_id,
        "outcome": "PASS" if tests and not failures and evidence_complete else "FAIL",
        "tests": [record["test_id"] for record in tests],
        "failures": failures,
        "expected_evidence_modes": expected_modes,
        "observed_evidence_modes": observed_modes,
        "evidence_complete": evidence_complete,
    }


def run(run_id):
    refusal = _refusal_reason()
    if refusal is not None:
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "physical B210 gate is not fully armed",
            "details": refusal,
        }, ensure_ascii=False))
        return 2

    host_environment = host_environment_snapshot()
    host_issues = approved_host_environment_issues(host_environment)
    if host_issues:
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "host does not match the approved Ubuntu B210 profile",
            "details": {
                "issues": host_issues,
                "host_environment": host_environment,
            },
        }, ensure_ascii=False))
        return 2

    raspberry_pi_usb, raspberry_pi_usb_issues = live_raspberry_pi_usb_snapshot(
        host_environment
    )
    if raspberry_pi_usb_issues:
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "Raspberry Pi 5 USB power/topology preflight failed",
            "details": {
                "issues": raspberry_pi_usb_issues,
                "snapshot": raspberry_pi_usb,
            },
        }, ensure_ascii=False))
        return 2

    probes, probe_error = _probe_devices()
    if probe_error is not None:
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "UHD device probe did not produce required firmware/FPGA evidence",
            "details": probe_error,
        }, ensure_ascii=False))
        return 2

    started_ns = time.time_ns()
    try:
        module = importlib.import_module(MODULE_NAME)
        gr_module = importlib.import_module("gnuradio.gr")
        uhd_module = importlib.import_module("gnuradio.uhd")
    except (ImportError, ModuleNotFoundError) as error:
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "approved GNU Radio/gr-uhd modules are unavailable",
            "details": {"exception": type(error).__name__},
        }, ensure_ascii=False))
        return 2
    gnuradio_version = str(gr_module.version())
    uhd_version = str(getattr(uhd_module, "get_version_string", lambda: "")())
    runtime_versions = {
        "python": platform.python_version(),
        "gnuradio": gnuradio_version,
        "uhd": uhd_version,
    }
    apt_packages, package_collection_issues = live_apt_package_snapshot()
    apt_holds, hold_collection_issues = live_apt_holds()
    package_manifest_path = os.path.abspath(
        os.environ["VDETER_B210_PACKAGE_MANIFEST"]
    )
    try:
        package_manifest = load_apt_package_manifest(package_manifest_path)
    except ValueError as error:
        package_manifest = {}
        package_manifest_load_issues = (str(error),)
    else:
        package_manifest_load_issues = ()
    package_manifest_issues = (
        tuple(package_collection_issues)
        + tuple(hold_collection_issues)
        + tuple(package_manifest_load_issues)
        + tuple(apt_package_manifest_issues(
            package_manifest, host_environment, runtime_versions,
            apt_packages, apt_holds,
        ))
    )
    if (
        platform.python_version_tuple()[:2] != ("3", "12")
        or gnuradio_version != APPROVED_GNURADIO_VERSION
        or uhd_version != APPROVED_UHD_VERSION
        or package_manifest_issues
    ):
        print(json.dumps({
            "overall_outcome": "BLOCKED",
            "reason": "installed versions do not match the approved B210 profile",
            "details": {
                "python": platform.python_version(),
                "gnuradio": gnuradio_version,
                "uhd": uhd_version,
                "apt_packages": apt_packages,
                "package_manifest_issues": package_manifest_issues,
            },
        }, ensure_ascii=False))
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    ).run(suite)
    finished_ns = time.time_ns()
    evidence = list(module.B210HardwareTest.evidence_records)
    cases = [_case(case_id, result.records, evidence) for case_id in (
        "TC-RADIO-012", "TC-RADIO-013"
    )]
    successful = result.wasSuccessful() and not result.skipped and all(
        case["outcome"] == "PASS" for case in cases
    )

    manifest_path = os.path.abspath(os.environ["VDETER_B210_FIXTURE_MANIFEST"])
    with open(manifest_path, "rb") as handle:
        manifest_bytes = handle.read()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    fixture = json.loads(manifest_bytes.decode("utf-8"))
    validation_profile = fixture.get(
        "validation_profile", "CALIBRATED_PROTECTION"
    )
    user_accepted_nominal = (
        validation_profile == "USER_ACCEPTED_NOMINAL_30DB_CONDUCTED_V1"
    )
    fixture_safety = []
    for direction in fixture["directions"]:
        path_min_loss = sum(
            value["guaranteed_min_loss_millidb"]
            for value in direction["components"]
        )
        nominal_path_loss = sum(
            value["nominal_loss_millidb"]
            for value in direction["components"]
        )
        calibration = None
        if direction.get("tx_power_evidence") == "CALIBRATED_MEASUREMENT":
            calibration_path = direction["tx_power_calibration_path"]
            if not os.path.isabs(calibration_path):
                calibration_path = os.path.join(
                    os.path.dirname(manifest_path), calibration_path
                )
            calibration_path = os.path.abspath(calibration_path)
            calibration = {
                "path": calibration_path,
                "sha256": direction["tx_power_calibration_sha256"],
            }
        fixture_safety.append({
            "tx_serial": direction["tx_serial"],
            "tx_connector": direction["tx_connector"],
            "rx_serial": direction["rx_serial"],
            "rx_connector": direction["rx_connector"],
            "approved_tx_gain_db": direction["approved_tx_gain_db"],
            "p_tx_bound_millidbm": direction["p_tx_bound_millidbm"],
            "nominal_path_loss_millidb": nominal_path_loss,
            "path_min_loss_millidb": path_min_loss,
            "p_rx_worst_millidbm": (
                direction["p_tx_bound_millidbm"] - path_min_loss
            ),
            "tx_power_evidence": direction.get(
                "tx_power_evidence", "MANUFACTURER_MAXIMUM"
            ),
            "tx_power_calibration": calibration,
            "protection_claim": (
                "NOT_CLAIMED_USER_ACCEPTED"
                if user_accepted_nominal else "VERIFIED_CONSERVATIVE_BOUND"
            ),
            "components": direction["components"],
        })
    evidence_text = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in evidence
    )
    with open(package_manifest_path, "rb") as handle:
        package_manifest_bytes = handle.read()
    package_manifest_sha256 = hashlib.sha256(package_manifest_bytes).hexdigest()
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": "VDETER-STAGE3-RADIO-BL-001",
        "baseline_version": "0.2.3",
        "stage": "stage3-radio-hardware",
        "overall_outcome": "PASS" if successful else "FAIL",
        "started_at": datetime.fromtimestamp(
            started_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "finished_at": datetime.fromtimestamp(
            finished_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "host_environment": host_environment,
            "raspberry_pi_usb": raspberry_pi_usb,
            "gnuradio_version": gnuradio_version,
            "gnuradio_uhd_version": gnuradio_version,
            "uhd_version": uhd_version,
            "apt_packages": apt_packages,
            "apt_held_packages": apt_holds,
            "ship_serial": os.environ["VDETER_B210_SHIP_SERIAL"],
            "control_serial": os.environ["VDETER_B210_CONTROL_SERIAL"],
            "actual_b210_count_verified": 2 if successful else 0,
            "fixture_manifest_path": manifest_path,
            "fixture_manifest_sha256": manifest_sha256,
            "package_manifest_path": package_manifest_path,
            "package_manifest_sha256": package_manifest_sha256,
            "device_probes": {
                role: {
                    "serial": value["serial"],
                    "output_sha256": value["output_sha256"],
                    "firmware_fpga_lines": value["firmware_fpga_lines"],
                    "usb_lines": value["usb_lines"],
                }
                for role, value in probes.items()
            },
        },
        "totals": {
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "evidence_records": len(evidence),
        },
        "test_cases": cases,
        "fixture": {
            "fixture_id": fixture["fixture_id"],
            "outcome": fixture["outcome"],
            "created_at_utc": fixture["created_at_utc"],
            "preflight_version": fixture["preflight_version"],
            "validation_profile": validation_profile,
            "risk_acceptance_id": fixture.get("risk_acceptance_id"),
            "scope": (
                "FUNCTIONAL_CONDUCTED_ONLY"
                if user_accepted_nominal else "CALIBRATED_CONDUCTED_PROTECTION"
            ),
            "center_frequencies_hz": fixture["center_frequencies_hz"],
            "source_radio_fingerprint": fixture["source_radio_fingerprint"],
            "receiver_radio_fingerprint": fixture["receiver_radio_fingerprint"],
            "safety": fixture_safety,
        },
        "hardware_gate": {
            "tc_radio_012_pass": cases[0]["outcome"] == "PASS",
            "tc_radio_013_pass": cases[1]["outcome"] == "PASS",
            "actual_b210_count_verified": 2 if successful else 0,
        },
        "artifacts": {
            "hardware_evidence": {
                "path": "artifacts/hardware_evidence.jsonl",
                "sha256": hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
                "records": len(evidence),
            },
            "fixture_manifest": {
                "path": manifest_path,
                "sha256": manifest_sha256,
            },
            "package_manifest": {
                "path": "artifacts/ubuntu-apt-package-manifest.json",
                "sha256": package_manifest_sha256,
            },
        },
    }

    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    with open(os.path.join(run_directory, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(os.path.join(run_directory, "logs.jsonl"), "w", encoding="utf-8") as handle:
        for record in result.records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with open(
        os.path.join(artifacts, "hardware_evidence.jsonl"), "w", encoding="utf-8"
    ) as handle:
        handle.write(evidence_text)
    for role, value in probes.items():
        with open(
            os.path.join(artifacts, f"uhd_usrp_probe_{role}.txt"),
            "w", encoding="utf-8",
        ) as handle:
            handle.write(value["output"])
    with open(
        os.path.join(artifacts, "ubuntu-apt-package-manifest.json"), "wb"
    ) as handle:
        handle.write(package_manifest_bytes)
    with open(os.path.join(artifacts, "unittest.txt"), "w", encoding="utf-8") as handle:
        handle.write(stream.getvalue())
    print(json.dumps({
        "run_id": run_id,
        "overall_outcome": payload["overall_outcome"],
        "tests_run": result.testsRun,
        "evidence_records": len(evidence),
    }, ensure_ascii=False))
    return 0 if successful else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    return run(parser.parse_args().run_id)


if __name__ == "__main__":
    raise SystemExit(main())
