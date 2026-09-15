"""TC-RADIO-015 final requirement-to-evidence audit gate."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path


REQUIRED_CASES = {f"TC-RADIO-{index:03d}" for index in range(1, 16)}
REQUIRED_REQUIREMENTS = {f"RADIO-REQ-{index:02d}" for index in range(1, 12)}
REQUIRED_DECISIONS = {f"DR-STAGE3-RADIO-{index:03d}" for index in range(1, 35)}


def load_tests(loader, tests, pattern):
    """The final audit is absent, rather than skipped, until explicitly armed."""
    if os.environ.get("VDETER_STAGE3_FINAL_AUDIT") == "1":
        return tests
    return unittest.TestSuite()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _values_for_key(value, target):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == target:
                yield item
            yield from _values_for_key(item, target)
    elif isinstance(value, list):
        for item in value:
            yield from _values_for_key(item, target)


@unittest.skipUnless(
    os.environ.get("VDETER_STAGE3_FINAL_AUDIT") == "1",
    "run only after TC-RADIO-012/013 physical hardware PASS",
)
class Stage3RadioAuditTest(unittest.TestCase):
    def test_TC_RADIO_015_all_cases_have_nonblocked_pass_evidence(self):
        root = Path(__file__).resolve().parents[2]
        evidence = Path(os.environ["VDETER_STAGE3_FINAL_EVIDENCE"])
        if not evidence.is_absolute():
            evidence = root / evidence
        payload = json.loads(evidence.read_text(encoding="utf-8"))

        self.assertEqual("VDETER-STAGE3-RADIO-BL-001", payload["baseline_id"])
        self.assertEqual("0.2.4", payload["baseline_version"])
        self.assertEqual("stage3-radio", payload["stage"])
        self.assertEqual("PASS", payload["overall_outcome"])
        cases_list = payload["test_cases"]
        case_ids = [value["test_case"] for value in cases_list]
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(REQUIRED_CASES, set(case_ids))
        self.assertTrue(all(value["outcome"] == "PASS" for value in cases_list))
        self.assertEqual({
            "test_cases": 15, "passed": 15, "failed": 0,
            "errors": 0, "skipped": 0,
        }, payload["totals"])

        gate = payload["hardware_gate"]
        self.assertTrue(gate["tc_radio_012_pass"])
        self.assertTrue(gate["tc_radio_013_pass"])
        self.assertEqual(2, gate["actual_b210_count_verified"])

        trace = payload["traceability_audit"]
        self.assertEqual(REQUIRED_REQUIREMENTS, set(trace["requirement_ids"]))
        self.assertEqual(REQUIRED_DECISIONS, set(trace["decision_ids"]))
        self.assertEqual(REQUIRED_CASES, set(trace["test_case_ids"]))
        self.assertEqual(11, len(trace["requirements"]))
        covered_tests = set()
        covered_decisions = set()
        for row in trace["requirements"]:
            self.assertIn(row["requirement_id"], REQUIRED_REQUIREMENTS)
            self.assertIn(row["status"], {"IMPLEMENTED", "VERIFIED"})
            for field in (
                "normative_refs", "decision_records", "implementation",
                "test_cases", "traceability_evidence",
            ):
                self.assertTrue(row[field], f"{row['requirement_id']}:{field}")
            covered_tests.update(row["test_cases"])
            covered_decisions.update(row["decision_records"])
        self.assertTrue(REQUIRED_CASES.issubset(covered_tests))
        self.assertEqual(REQUIRED_DECISIONS, covered_decisions)

        for name in ("software_result", "hardware_result"):
            artifact = payload["artifacts"][name]
            path = Path(artifact["path"])
            if not path.is_absolute():
                path = root / path
            self.assertTrue(path.is_file(), path)
            self.assertEqual(artifact["sha256"], _sha256(path))
            self.assertEqual(artifact["size_bytes"], path.stat().st_size)
        trace_artifact = trace["traceability"]
        trace_path = Path(trace_artifact["path"])
        if not trace_path.is_absolute():
            trace_path = root / trace_path
        self.assertTrue(trace_path.is_file())
        self.assertEqual(trace_artifact["sha256"], _sha256(trace_path))

        hardware_result_artifact = payload["artifacts"]["hardware_result"]
        hardware_result_path = Path(hardware_result_artifact["path"])
        if not hardware_result_path.is_absolute():
            hardware_result_path = root / hardware_result_path
        hardware_artifact = payload["artifacts"]["hardware"]["hardware_evidence"]
        hardware_evidence_path = hardware_result_path.parent / hardware_artifact["path"]
        self.assertTrue(hardware_evidence_path.is_file())
        self.assertEqual(hardware_artifact["sha256"], _sha256(hardware_evidence_path))
        package_manifest_artifact = payload["artifacts"]["hardware"][
            "package_manifest"
        ]
        package_manifest_path = (
            hardware_result_path.parent / package_manifest_artifact["path"]
        )
        self.assertTrue(package_manifest_path.is_file())
        self.assertEqual(
            package_manifest_artifact["sha256"], _sha256(package_manifest_path)
        )
        hardware_records = [
            json.loads(line) for line in hardware_evidence_path.read_text(
                encoding="utf-8"
            ).splitlines() if line
        ]
        self.assertEqual(hardware_artifact["records"], len(hardware_records))

        modes = {}
        for record in hardware_records:
            key = (record["test_case"], record["mode"])
            modes[key] = modes.get(key, 0) + 1
        self.assertEqual(100, modes[("TC-RADIO-013", "lower")])
        self.assertEqual(100, modes[("TC-RADIO-013", "upper")])
        self.assertEqual(100, modes[("TC-RADIO-013", "full_duplex")])
        self.assertEqual(1, modes[("TC-RADIO-013", "timed_retune_upper_rx")])
        self.assertEqual(1, modes[("TC-RADIO-012", "async_prequeue")])
        self.assertEqual(1, modes[("TC-RADIO-012", "async_error_distinction")])
        self.assertEqual(20, modes[("TC-RADIO-012", "lifecycle")])
        self.assertEqual(1, modes[("TC-RADIO-012", "controlled_usb_disconnect")])
        async_probe = next(
            value for value in hardware_records
            if value["mode"] == "async_error_distinction"
        )
        self.assertTrue(async_probe["time_error_distinct"])
        self.assertTrue(async_probe["underflow_distinct"])
        self.assertIn("TIME_ERROR", {
            value["outcome"] for value in async_probe["time_error_native_events"]
        })
        self.assertIn("UNDERFLOW", {
            value["outcome"] for value in async_probe["underflow_native_events"]
        })

        normal_modes = {"lower", "upper", "full_duplex", "timed_retune_upper_rx"}
        for record in hardware_records:
            if record["mode"] not in normal_modes:
                continue
            encoded = json.dumps(record, sort_keys=True)
            self.assertIn("burst_id", encoded)
            self.assertIn("rx_window", encoded)
            self.assertIn("underflow_count", encoded)
            self.assertIn("overflow_count", encoded)
            self.assertIn("late_command_count", encoded)
            for counter in (
                "tx_underflow_count", "rx_overflow_count", "late_command_count",
                "tx_outcome_unknown_count", "rx_gap_count",
            ):
                values = tuple(_values_for_key(record, counter))
                self.assertTrue(values, counter)
                self.assertTrue(all(value == 0 for value in values), (counter, values))
            self.assertNotIn('"GAP_BEFORE"', encoded)
            self.assertNotIn("OUTCOME_UNKNOWN", encoded)

        environment = payload["environment"]["hardware"]
        host = environment["host_environment"]
        self.assertEqual("Linux", host["system"])
        self.assertEqual("ubuntu", host["os_id"].lower())
        self.assertEqual("24.04", host["os_version_id"])
        self.assertIn(
            host["machine"].lower(), {"x86_64", "amd64", "aarch64", "arm64"}
        )
        self.assertNotIn("microsoft", host["kernel_release"].lower())
        self.assertNotIn(host.get("virtualization", "none").lower(), {"wsl", "wsl2"})
        if host["machine"].lower() in {"aarch64", "arm64"}:
            self.assertTrue(host["hardware_model"].startswith("Raspberry Pi 5"))
            pi_usb = environment["raspberry_pi_usb"]
            self.assertTrue(pi_usb["applicable"])
            self.assertIn("usb_max_current_enable=1", pi_usb["usb_max_current"])
            self.assertIn("throttled=0x0", pi_usb["throttled"].lower())
            self.assertGreaterEqual(pi_usb["usb_topology"].count("5000M"), 2)
        self.assertTrue(environment["python_version"].startswith("3.12."))
        self.assertEqual("3.10.9.2", environment["gnuradio_version"])
        self.assertEqual("3.10.9.2", environment["gnuradio_uhd_version"])
        self.assertEqual(
            "4.6.0.0+ds1-5.1ubuntu0.24.04.1", environment["uhd_version"]
        )
        self.assertEqual(2, environment["actual_b210_count_verified"])
        packages = environment["apt_packages"]
        expected_package_arch = (
            "arm64" if host["machine"].lower() in {"aarch64", "arm64"}
            else "amd64"
        )
        self.assertTrue(all(
            value["architecture"] in {expected_package_arch, "all"}
            for value in packages.values()
        ))
        self.assertEqual(
            "3.10.9.2-1.1ubuntu2", packages["gnuradio"]["version"]
        )
        self.assertEqual(
            "3.10.9.2-1.1ubuntu2", packages["gnuradio_uhd"]["version"]
        )
        for role in ("uhd_host", "uhd_runtime"):
            self.assertEqual(
                "4.6.0.0+ds1-5.1ubuntu0.24.04.1",
                packages[role]["version"],
            )
        self.assertTrue(packages["python"]["version"].startswith("3.12"))
        held = set(environment["apt_held_packages"])
        self.assertTrue({
            value["name"].split(":", 1)[0] for value in packages.values()
        }.issubset(held))
        for role in ("ship", "control"):
            probe = environment["device_probes"][role]
            self.assertTrue(probe["firmware_fpga_lines"])
            self.assertTrue(any(
                value in line.lower()
                for line in probe["usb_lines"]
                for value in ("usb 3", "usb3", "superspeed")
            ))

        fixture = payload["fixture"]
        self.assertEqual("PASS", fixture["outcome"])
        self.assertEqual(
            payload["configuration_evidence"]["source_radio_fingerprint"],
            fixture["source_radio_fingerprint"],
        )
        self.assertEqual(
            payload["configuration_evidence"]["receiver_radio_fingerprint"],
            fixture["receiver_radio_fingerprint"],
        )
        self.assertEqual(2, len(fixture["safety"]))
        nominal_user_profile = (
            fixture["validation_profile"]
            == "USER_ACCEPTED_NOMINAL_30DB_CONDUCTED_V1"
        )
        if nominal_user_profile:
            self.assertEqual(
                "VDETER-STAGE3-USER-ACCEPTED-NOMINAL-30DB-CONDUCTED-001",
                fixture["risk_acceptance_id"],
            )
            self.assertEqual("FUNCTIONAL_CONDUCTED_ONLY", fixture["scope"])
            expected_directions = {
                "34D03DD": (
                    "34D6357", "ATT-A-30DB-2W-6GHZ-UNSERIALIZED"
                ),
                "34D6357": (
                    "34D03DD", "ATT-B-30DB-2W-6GHZ-UNSERIALIZED"
                ),
            }
        else:
            self.assertEqual("CALIBRATED_PROTECTION", fixture["validation_profile"])
            self.assertIsNone(fixture["risk_acceptance_id"])
        for direction in fixture["safety"]:
            self.assertTrue(direction["components"])
            if nominal_user_profile:
                self.assertEqual(0, direction["approved_tx_gain_db"])
                self.assertEqual(20_000, direction["p_tx_bound_millidbm"])
                self.assertEqual(30_000, direction["nominal_path_loss_millidb"])
                self.assertEqual(0, direction["path_min_loss_millidb"])
                self.assertEqual(20_000, direction["p_rx_worst_millidbm"])
                self.assertEqual(
                    "USER_ACCEPTED_UNCALIBRATED",
                    direction["tx_power_evidence"],
                )
                self.assertIsNone(direction["tx_power_calibration"])
                self.assertEqual(
                    "NOT_CLAIMED_USER_ACCEPTED",
                    direction["protection_claim"],
                )
                self.assertEqual(1, len(direction["components"]))
                component = direction["components"][0]
                expected_rx, expected_component = expected_directions[
                    direction["tx_serial"]
                ]
                self.assertEqual(expected_rx, direction["rx_serial"])
                self.assertEqual("TX/RX", direction["tx_connector"])
                self.assertEqual("RX2", direction["rx_connector"])
                self.assertEqual(expected_component, component["component_id"])
                self.assertEqual(
                    "SMA_FIXED_ATTENUATOR_AND_UNCHARACTERIZED_CABLE_PATH",
                    component["kind"],
                )
                self.assertEqual(30_000, component["nominal_loss_millidb"])
                self.assertEqual(30_000, component["tolerance_millidb"])
                self.assertEqual(0, component["guaranteed_min_loss_millidb"])
                self.assertEqual(
                    "USER_ACCEPTED_UNVERIFIED_LABEL",
                    component["loss_evidence"],
                )
                self.assertEqual(0, component["minimum_frequency_hz"])
                self.assertEqual(6_000_000_000, component["maximum_frequency_hz"])
                self.assertEqual(33_010, component["maximum_input_millidbm"])
                continue
            self.assertLessEqual(direction["p_rx_worst_millidbm"], -20_000)
            self.assertEqual(
                "VERIFIED_CONSERVATIVE_BOUND", direction["protection_claim"]
            )
            if direction["path_min_loss_millidb"] >= 40_000:
                continue
            self.assertEqual(
                "CALIBRATED_MEASUREMENT", direction["tx_power_evidence"]
            )
            calibration = direction["tx_power_calibration"]
            self.assertIsNotNone(calibration)
            calibration_path = Path(calibration["path"])
            if not calibration_path.is_absolute():
                calibration_path = hardware_result_path.parent / calibration_path
            self.assertTrue(calibration_path.is_file())
            self.assertEqual(calibration["sha256"], "sha256:" + _sha256(
                calibration_path
            ))

        regression = payload["regression"]
        self.assertEqual("PASS", regression["outcome"])
        for stage in ("stage0", "stage1", "stage2"):
            self.assertEqual(0, regression[stage]["failed"])
            self.assertEqual(0, regression[stage]["skipped"])
        scope = payload["conformance_scope"]
        self.assertEqual("NOT_SUPPORTED", scope["lower_leg_tbb_receive"])
        self.assertEqual("NOT_VERIFIED", scope["dual_leg_tbb_interoperability"])
        self.assertEqual("NOT_VERIFIED", scope["radiated_rf_compliance"])
        self.assertEqual("NOT_CLAIMED", scope["uncalibrated_fixture_protection"])


if __name__ == "__main__":
    unittest.main()
