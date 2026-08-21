"""Run Stage 3 software/non-hardware tests and emit partial evidence.

This runner cannot mark TC-RADIO-012 or TC-RADIO-013 PASS. Those cases need
two physical B210 devices and the separately armed hardware runner.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import re
import sys
import time
import unittest
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(VDETER_ROOT, "src")
for path in (VDETER_ROOT, SRC_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from vdeter.radio.b210 import missing_dependencies  # noqa: E402

RESULTS_ROOT = os.path.join(VDETER_ROOT, "results", "runs")
MODULES = (
    "tests.contract.test_stage3_radio_contract",
    "tests.unit.test_stage3_radio_software",
    "tests.integration.test_stage3_radio_medium",
    "tests.integration.test_stage3_phy_radio_codec",
    "tests.unit.test_stage3_b210_nonhardware",
    "tests.unit.test_stage3_b210_process_isolation",
)
CASE_PATTERN = re.compile(r"TC_RADIO_(\d{3})")
SOFTWARE_CASES = tuple(f"TC-RADIO-{value:03d}" for value in range(1, 12)) + (
    "TC-RADIO-014",
)
HARDWARE_CASES = ("TC-RADIO-012", "TC-RADIO-013")
CONFORMANCE_SCOPE = {
    "roc_tbb_profile": "PROJECT_UPPER_ONLY",
    "roc_tbb_transmit_channel": "VDETER_UPPER_25_TEST",
    "mass_tbb_receive_channels": ["VDETER_UPPER_25_TEST"],
    "lower_leg_tbb_receive": "NOT_SUPPORTED",
    "dual_leg_tbb_interoperability": "NOT_VERIFIED",
    "utc_100_microsecond_compliance": "NOT_VERIFIED",
    "radiated_rf_compliance": "NOT_VERIFIED",
    "uncalibrated_fixture_protection": "NOT_CLAIMED",
}

# A TC is not complete merely because one method containing its ID passed.
REQUIRED_COVERAGE = {
    "TC-RADIO-001": (
        "adapters_implement_the_same_public_port",
        "programmatic_backend_has_required_gr_uhd_calls_and_no_multiusrp_or_grc",
        "process_proxy_preserves_backend_contract_and_results",
    ),
    "TC-RADIO-002": (
        "schema_v2_is_explicit_and_canonical",
        "role_direction_and_hardware_keys_are_closed",
        "operational_b210_ship_and_control_configs_are_loadable",
        "inactive_applied_direction_requires_all_none",
        "injected_backend_applies_full_readback",
        "direct_adapter_configuration_enforces_exact_profile",
    ),
    "TC-RADIO-003": (
        "bytes_are_immutable_and_no_amplitude_rewrite_occurs",
        "out_of_range_and_nonfinite_tx_are_rejected_whole",
    ),
    "TC-RADIO-004": (
        "tx_backpressure_is_bounded_and_observable",
        "shared_tx_bytes_and_rx_window_conflicts_are_enforced",
        "shared_byte_budget_and_per_channel_window_count_are_enforced",
        "overflow_delivers_prefix_and_gap_suffix_then_incomplete",
    ),
    "TC-RADIO-005": (
        "lifecycle_stop_cancels_once_and_is_idempotent",
        "failed_stop_can_be_retried_and_generation_only_counts_success",
        "failed_readback_rolls_back_without_consuming_generation",
        "failed_late_config_cleanup_is_retained_for_stop_retry",
        "stop_timeout_is_retryable",
        "blocking_backend_stop_is_bounded_and_retryable",
        "blocking_monitor_wait_shares_cleanup_deadline",
        "flowgraph_stop_and_wait_share_one_bounded_worker",
        "concurrent_stop_shares_one_cleanup",
        "stop_preserves_active_software_rx_prefix",
        "stop_delivers_received_rx_prefix_before_incomplete",
        "blocking_child_stop_is_bounded_and_retryable",
    ),
    "TC-RADIO-006": (
        "mapping_domains_expiry_and_clock_jump",
        "mapping_uses_eight_brackets_and_generation_clock_id",
        "utc_jump_preserves_active_software_rx_prefix",
        "utc_jump_preserves_active_b210_rx_prefix",
    ),
    "TC-RADIO-007": (
        "timed_tx_completion_and_underflow_terminal",
        "injected_lead_accepts_boundary_and_rejects_below",
        "burst_ack_is_the_only_success_terminal",
        "ack_terminal_arrives_without_status_polling",
        "frequency_change_uses_200ms_lead_and_100ms_retune_gap",
    ),
    "TC-RADIO-008": (
        "finite_rx_rounding_and_generation_terminal",
        "link11_80ms_window_has_exact_6144_samples",
        "finite_rx_uses_exact_count_and_rx_time",
        "rx_deadline_distinguishes_timeout_and_short_read",
        "multiple_finite_rx_windows_keep_private_data_boundaries",
        "rx_terminal_arrives_without_status_polling",
        "rx_assembler_correlates_by_time_not_registration_order",
        "rx_assembler_keeps_resumed_timestamp_in_same_window",
        "overflow_keeps_contiguous_segments_separate",
    ),
    "TC-RADIO-009": (
        "tx_rx_are_independently_outstanding",
        "tx_and_rx_can_overlap_but_same_direction_resources_cannot",
    ),
    "TC-RADIO-010": (
        "event_delivery_failure_is_failed_without_retry",
        "nonterminal_delivery_failure_does_not_increment_terminal_count",
        "sample_delivery_failure_terminalizes_active_rx_by_actual_count",
        "terminal_delivery_failure_drains_multiple_pending_operations",
        "status_changes_are_emitted_but_polling_is_silent",
        "rx_timeout_uses_rx_key_not_burst_correlation",
        "async_error_preserves_oldest_burst_correlation",
        "native_numeric_async_codes_and_time_spec_are_decoded",
        "backend_rx_exception_is_typed_and_fails_adapter",
        "hardware_time_loss_terminates_all_pending_work",
        "disconnect_is_detected_without_status_polling",
        "native_abort_is_contained_as_backend_unavailable",
        "native_process_abort_is_typed_without_status_polling",
        "native_abort_terminalizes_pending_tx_and_rx",
        "native_task_failure_contains_live_child_before_stop",
        "delivery_failure_marks_submitted_tx_unknown_and_rx_incomplete",
        "sample_delivery_failure_preserves_actual_rx_count",
        "timestamped_async_error_matches_its_actual_burst",
        "nonterminal_b210_delivery_failure_keeps_terminal_count_zero",
        "monitor_failure_terminalizes_all_work_without_status",
        "late_duplicate_ack_does_not_damage_next_tx",
        "rx_assembler_rejects_uncorrelated_native_timestamp",
        "status_is_snapshot_only_not_a_progress_trigger",
    ),
    "TC-RADIO-011": (
        "directed_delay_zero_fill_and_overlap_sum",
        "same_fault_plan_produces_same_terminal",
        "fault_target_hardware_time_and_one_shot_are_exact",
        "retention_is_bounded_and_active_rx_is_protected",
        "concurrent_submit_and_render_are_safe",
    ),
    "TC-RADIO-014": (
        "rx_terminal_is_frozen_serializable_and_registered",
        "v1_legacy_hash_is_preserved_and_v2_is_additive",
        "radio_has_no_concrete_upper_layer_or_packet_logic_dependency",
        "evidence_scope_declares_upper_only_tbb_nonconformance",
        "cf32le_round_trip_preserves_stage2_detector",
    ),
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

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._append(test, "FAIL", self._exc_info_to_string(err, subtest))


def _version(distribution):
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def _regression_summary(paths):
    if not paths:
        return None
    summary = {"outcome": "PASS"}
    for stage, path in paths.items():
        absolute = os.path.abspath(path)
        with open(absolute, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        with open(absolute, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        totals = payload["totals"]
        failed = int(totals.get("failed", 0))
        skipped = int(totals.get("skipped", totals.get("unittest_skips", 0)))
        unittest_success = bool(totals.get("unittest_success", False))
        if failed or skipped or not unittest_success:
            summary["outcome"] = "FAIL"
        summary[stage] = {
            "baseline_id": payload["baseline_id"],
            "baseline_version": payload["baseline_version"],
            "run_id": payload["run_id"],
            "result": os.path.relpath(absolute, VDETER_ROOT).replace(os.sep, "/"),
            "passed": int(totals["passed"]),
            "failed": failed,
            "skipped": skipped,
            "tests_run": int(totals["tests_run"]),
            "sha256": digest,
        }
    return summary


def _case_result(case_id, records):
    case_records = [record for record in records if record["test_case"] == case_id]
    expected = REQUIRED_COVERAGE[case_id]
    observed = {
        item: [record for record in case_records if item in record["test_id"]]
        for item in expected
    }
    missing = [item for item, matched in observed.items() if not matched]
    failures = [record for record in case_records if record["outcome"] != "PASS"]
    return {
        "test_case": case_id,
        "outcome": "PASS" if not missing and not failures else "FAIL",
        "required_coverage": list(expected),
        "missing_coverage": missing,
        "tests": [record["test_id"] for record in case_records],
        "failures": failures,
    }


def run(run_id: str, regression_paths=None) -> int:
    started_ns = time.time_ns()
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for module_name in MODULES:
        suite.addTests(loader.loadTestsFromModule(importlib.import_module(module_name)))
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    ).run(suite)
    finished_ns = time.time_ns()

    cases = [_case_result(case_id, result.records) for case_id in SOFTWARE_CASES]
    software_coverage_complete = all(value["outcome"] == "PASS" for value in cases)
    hardware_reason = (
        "Two physical B210 devices, an approved attenuated fixture, and hardware "
        "evidence are not present in this run."
    )
    for case_id in HARDWARE_CASES:
        cases.append({"test_case": case_id, "outcome": "BLOCKED", "reason": hardware_reason})
    cases.append({
        "test_case": "TC-RADIO-015",
        "outcome": "BLOCKED",
        "reason": "TC-RADIO-012/013 hardware PASS and the final trace/evidence audit are pending.",
    })

    nonhardware = [
        record for record in result.records
        if ".test_stage3_b210_nonhardware." in record["test_id"]
    ]
    regression_path = os.path.join(
        RESULTS_ROOT, run_id, "artifacts", "regression_summary.json"
    )
    regression = _regression_summary(regression_paths)
    if regression is None and os.path.exists(regression_path):
        with open(regression_path, "r", encoding="utf-8") as handle:
            regression = json.load(handle)

    regression_complete = regression is None or regression.get("outcome") == "PASS"
    successful = (
        result.wasSuccessful() and software_coverage_complete and regression_complete
    )
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "baseline_id": "VDETER-STAGE3-RADIO-BL-001",
        "baseline_version": "0.2.4",
        "stage": "stage3-radio",
        "overall_outcome": "PARTIAL_PASS" if successful else "FAIL",
        "started_at": datetime.fromtimestamp(
            started_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "finished_at": datetime.fromtimestamp(
            finished_ns / 1_000_000_000, timezone.utc
        ).isoformat(),
        "clock_domain": "UTC",
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "approved_python_3_12": platform.python_version_tuple()[:2] == ("3", "12"),
            "gnuradio": _version("gnuradio"),
            "gnuradio_uhd": _version("gnuradio-uhd"),
            "uhd": _version("uhd"),
            "missing_modules": missing_dependencies(),
        },
        "totals": {
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "software_cases_passed": sum(
                value["outcome"] == "PASS"
                for value in cases
                if value["test_case"] in SOFTWARE_CASES
            ),
            "software_coverage_complete": software_coverage_complete,
            "regression_complete": regression_complete,
            "hardware_cases_blocked": 2,
            "final_audit_blocked": True,
        },
        "test_cases": cases,
        "b210_nonhardware_checks": nonhardware,
        "regression": regression,
        "conformance_scope": CONFORMANCE_SCOPE,
        "hardware_gate": {
            "tc_radio_012_pass": False,
            "tc_radio_013_pass": False,
            "actual_b210_count_verified": 0,
            "reason": hardware_reason,
        },
    }

    run_directory = os.path.join(RESULTS_ROOT, run_id)
    artifacts = os.path.join(run_directory, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    if regression is not None:
        with open(regression_path, "w", encoding="utf-8") as handle:
            json.dump(regression, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    with open(os.path.join(run_directory, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(os.path.join(run_directory, "logs.jsonl"), "w", encoding="utf-8") as handle:
        for record in result.records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with open(os.path.join(artifacts, "unittest.txt"), "w", encoding="utf-8") as handle:
        handle.write(stream.getvalue())

    print(json.dumps({
        "run_id": run_id,
        "overall_outcome": payload["overall_outcome"],
        "tests_run": result.testsRun,
        "software_cases_passed": payload["totals"]["software_cases_passed"],
        "blocked": ["TC-RADIO-012", "TC-RADIO-013", "TC-RADIO-015"],
    }, ensure_ascii=False))
    return 0 if successful else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="stage3-radio-partial")
    parser.add_argument("--stage0-result")
    parser.add_argument("--stage1-result")
    parser.add_argument("--stage2-result")
    arguments = parser.parse_args()
    regression_paths = None
    values = {
        "stage0": arguments.stage0_result,
        "stage1": arguments.stage1_result,
        "stage2": arguments.stage2_result,
    }
    if any(values.values()):
        if not all(values.values()):
            parser.error("all three Stage 0..2 result paths are required together")
        regression_paths = values
    return run(arguments.run_id, regression_paths)


if __name__ == "__main__":
    raise SystemExit(main())
