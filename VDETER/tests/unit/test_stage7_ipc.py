"""Stage 7 Runtime IPC canonical 계약 시험 (`stage7` §16, §16.1)."""

import unittest
import uuid

from vdeter.b210_system.ipc import (
    RUNTIME_PROTOCOL,
    IpcError,
    body_set_sha256,
    body_sha256_of,
    build_barrier_manifest,
    decode_message,
    encode_message,
    validate_runtime_command,
    validate_runtime_event,
    validate_runtime_response,
)

RUN_ID = "stage7-op-20260826"
PLAN_SHA = "a" * 64


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=n, version=4))


def make_command(command_name: str, body_fields: dict, *, phase="PREPARE", **overrides):
    body = dict(body_fields)
    if command_name not in ("status", "emergency_inhibit"):
        body.setdefault(
            "logical_action",
            "active_bridge_install"
            if command_name.endswith("_active_bridge")
            else command_name,
        )
        body.setdefault("phase_plan_sha256", "b" * 64)
        body.setdefault("commit_at_host_raw_ns", 2_000_000_000)
    message = {
        "protocol": RUNTIME_PROTOCOL,
        "kind": "command",
        "command_name": command_name,
        "run_id": RUN_ID,
        "resolved_system_plan_sha256": PLAN_SHA,
        "command_id": _uuid(1),
        "endpoint_id": "MASS-A",
        "expected_lifecycle_generation": 1,
        "expected_bridge_generation": None,
        "barrier_id": None if command_name in ("status", "emergency_inhibit") else _uuid(2),
        "barrier_phase": None if command_name in ("status", "emergency_inhibit") else phase,
        "barrier_manifest_sha256": None
        if command_name in ("status", "emergency_inhibit")
        else "c" * 64,
        "issued_host_raw_ns": 1_000_000_000,
        "deadline_host_raw_ns": 31_000_000_000,
        "request_body_sha256": None,
        "body_sha256": None,
        "body": body,
    }
    message.update(overrides)
    digest = body_sha256_of(message["body"])
    if message["request_body_sha256"] is None:
        message["request_body_sha256"] = digest
    if message["body_sha256"] is None:
        message["body_sha256"] = digest
    return message


def make_prepare():
    return make_command(
        "prepare",
        {
            "deployment_sha256": "d" * 64,
            "endpoint_plan_relpath": "configs/stage7/endpoints/mass-a.toml",
            "endpoint_plan_sha256": "e" * 64,
            "environment_manifest_sha256": "f" * 64,
            "member_set_sha256": "0" * 64,
            "p1_member_plan_relpath": None,
            "p1_member_plan_sha256": None,
        },
    )


class TC_STAGE7_IPC_COMMANDS(unittest.TestCase):
    def test_prepare_valid(self):
        validate_runtime_command(make_prepare())

    def test_roundtrip_encoding(self):
        message = make_prepare()
        encoded = encode_message(message)
        decoded = decode_message(encoded)
        self.assertEqual(decoded, message)
        validate_runtime_command(decoded)

    def test_unknown_envelope_key_rejected(self):
        message = make_prepare()
        message["extra"] = 1
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_missing_envelope_key_rejected(self):
        message = make_prepare()
        del message["barrier_id"]
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_wrong_body_hash_rejected(self):
        message = make_prepare()
        message["body_sha256"] = "9" * 64
        message["request_body_sha256"] = "9" * 64
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_deadline_not_after_issued_rejected(self):
        message = make_prepare()
        message["deadline_host_raw_ns"] = message["issued_host_raw_ns"]
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_bool_as_integer_rejected(self):
        message = make_command(
            "configure",
            {
                "endpoint_config_sha256": "d" * 64,
                "expected_serial": "34D03DD",
                "usb_physical_path": "3-1",
                "radio_generation": True,
            },
        )
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_status_requires_null_barrier(self):
        message = make_command("status", {"query_command_id": None})
        validate_runtime_command(message)
        message = make_command(
            "status", {"query_command_id": None}, barrier_id=_uuid(9)
        )
        with self.assertRaises(IpcError):
            validate_runtime_command(message)

    def test_active_bridge_verb_phase_binding(self):
        body = {
            "candidate_set": [["MASS-A", "1" * 64], ["MASS-B", "2" * 64], ["ROC", "3" * 64]],
            "candidate_set_sha256": body_set_sha256(
                [["MASS-A", "1" * 64], ["MASS-B", "2" * 64], ["ROC", "3" * 64]]
            ),
            "candidate_sha256": "1" * 64,
            "active_clock_id": "stage7-pi-host-affine-v1:run:0",
            "bridge_generation": 0,
            "alpha_num": 3,
            "alpha_den": 2,
            "beta_num": 1_000_001,
            "beta_den": 1_000_000,
            "correction_num": -1,
            "correction_den": 4,
        }
        validate_runtime_command(make_command("prepare_active_bridge", body, phase="PREPARE"))
        with self.assertRaises(IpcError):
            validate_runtime_command(
                make_command("prepare_active_bridge", body, phase="COMMIT")
            )

    def test_active_bridge_requires_reduced_rationals(self):
        candidate_set = [["MASS-A", "1" * 64], ["MASS-B", "2" * 64], ["ROC", "3" * 64]]
        body = {
            "candidate_set": candidate_set,
            "candidate_set_sha256": body_set_sha256(candidate_set),
            "candidate_sha256": "1" * 64,
            "active_clock_id": "clock",
            "bridge_generation": 0,
            "alpha_num": 2,
            "alpha_den": 4,
            "beta_num": 1,
            "beta_den": 1,
            "correction_num": 0,
            "correction_den": 1,
        }
        with self.assertRaises(IpcError):
            validate_runtime_command(make_command("prepare_active_bridge", body))

    def test_member_order_enforced(self):
        wrong = [["MASS-B", "2" * 64], ["MASS-A", "1" * 64], ["ROC", "3" * 64]]
        body = {
            "candidate_set": wrong,
            "candidate_set_sha256": body_set_sha256(wrong),
            "candidate_sha256": "1" * 64,
            "active_clock_id": "clock",
            "bridge_generation": 0,
            "alpha_num": 0,
            "alpha_den": 1,
            "beta_num": 1,
            "beta_den": 1,
            "correction_num": 0,
            "correction_den": 1,
        }
        with self.assertRaises(IpcError):
            validate_runtime_command(make_command("prepare_active_bridge", body))

    def test_open_service_admission_deadline_formula(self):
        commit_at = 100_000_000_000
        good = {
            "intent_window_id": _uuid(7),
            "intent_timeout_ns": 30_000_000_000,
            "tbb_publish_timeout_ns": 90_000_000_000,
            "service_start_deadline_host_raw_ns": commit_at + 30_000_000_000,
            "tbb_publish_deadline_host_raw_ns": commit_at + 120_000_000_000,
        }
        message = make_command("open_service_admission", dict(good))
        message["body"]["commit_at_host_raw_ns"] = commit_at
        message["deadline_host_raw_ns"] = commit_at + 1
        digest = body_sha256_of(message["body"])
        message["body_sha256"] = digest
        message["request_body_sha256"] = digest
        validate_runtime_command(message)
        for delta in (-1, 1):
            broken = make_command("open_service_admission", dict(good))
            broken["body"]["commit_at_host_raw_ns"] = commit_at
            broken["body"]["service_start_deadline_host_raw_ns"] = (
                commit_at + 30_000_000_000 + delta
            )
            broken["deadline_host_raw_ns"] = commit_at + 1
            digest = body_sha256_of(broken["body"])
            broken["body_sha256"] = digest
            broken["request_body_sha256"] = digest
            with self.assertRaises(IpcError):
                validate_runtime_command(broken)

    def test_calibrate_rf_mode_null_hash_combinations(self):
        base = {
            "gain_plan_sha256": "d" * 64,
            "path_ids": ["C1", "C2"],
        }
        search = make_command(
            "calibrate_rf",
            {
                **base,
                "calibration_mode": "SEARCH_AND_SEAL",
                "source_gain_artifact_sha256": None,
                "operational_config_sha256": None,
            },
        )
        validate_runtime_command(search)
        broken = make_command(
            "calibrate_rf",
            {
                **base,
                "calibration_mode": "SEARCH_AND_SEAL",
                "source_gain_artifact_sha256": "e" * 64,
                "operational_config_sha256": None,
            },
        )
        with self.assertRaises(IpcError):
            validate_runtime_command(broken)
        apply_missing = make_command(
            "calibrate_rf",
            {
                **base,
                "calibration_mode": "APPLY_AND_REVERIFY",
                "source_gain_artifact_sha256": "e" * 64,
                "operational_config_sha256": None,
            },
        )
        with self.assertRaises(IpcError):
            validate_runtime_command(apply_missing)

    def test_path_ids_canonical_order(self):
        good = make_command(
            "start_cfo",
            {
                "cfo_plan_sha256": "d" * 64,
                "path_ids": ["C1", "C2", "C3", "C4", "C5", "C6"],
                "operation_deadline_host_raw_ns": 5_000_000_000,
            },
        )
        validate_runtime_command(good)
        bad = make_command(
            "start_cfo",
            {
                "cfo_plan_sha256": "d" * 64,
                "path_ids": ["C2", "C1"],
                "operation_deadline_host_raw_ns": 5_000_000_000,
            },
        )
        with self.assertRaises(IpcError):
            validate_runtime_command(bad)


class TC_STAGE7_IPC_RESPONSES(unittest.TestCase):
    def _response(self, command, status="PREPARED", **body_overrides):
        body = {
            "status": status,
            "endpoint_state": "PREFLIGHT",
            "lifecycle_generation": 1,
            "bridge_generation": None,
            "private_code": None,
            "result_sha256": None,
            "result": None,
        }
        body.update(body_overrides)
        message = dict(command)
        message["kind"] = "response"
        message["request_body_sha256"] = command["body_sha256"]
        message["body"] = body
        message["body_sha256"] = body_sha256_of(body)
        return message

    def test_prepare_response_statuses(self):
        command = make_prepare()
        validate_runtime_response(self._response(command, "PREPARED"), command)
        validate_runtime_response(self._response(command, "REJECTED"), command)
        with self.assertRaises(IpcError):
            validate_runtime_response(self._response(command, "ACCEPTED"), command)

    def test_measurement_commit_rejects_accepted(self):
        command = make_command(
            "acquire_host_time",
            {
                "profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                "acquisition_attempt": 0,
                "max_acquisition_ns": 600_000_000_000,
            },
            phase="COMMIT",
        )
        with self.assertRaises(IpcError):
            validate_runtime_response(self._response(command, "ACCEPTED"), command)
        validate_runtime_response(self._response(command, "COMPLETED"), command)

    def test_result_hash_binding(self):
        command = make_prepare()
        result = {"summary_type": "X", "value": 1}
        good = self._response(
            command, "PREPARED", result=result, result_sha256=body_sha256_of(result)
        )
        validate_runtime_response(good, command)
        bad = self._response(command, "PREPARED", result=result, result_sha256="9" * 64)
        with self.assertRaises(IpcError):
            validate_runtime_response(bad, command)


class TC_STAGE7_IPC_EVENTS(unittest.TestCase):
    def _intent_event(self, **overrides):
        body = {
            "event_type": "STATE_CHANGED",
            "endpoint_state": "SERVICE_START_PENDING",
            "lifecycle_generation": 1,
            "bridge_generation": 0,
            "terminal_command_id": None,
            "terminal_status": None,
            "artifact_sha256": "a" * 64,
            "private_code": None,
        }
        body.update(overrides.pop("body_overrides", {}))
        message = {
            "protocol": RUNTIME_PROTOCOL,
            "kind": "event",
            "command_name": None,
            "run_id": RUN_ID,
            "resolved_system_plan_sha256": PLAN_SHA,
            "command_id": None,
            "endpoint_id": "MASS-A",
            "expected_lifecycle_generation": None,
            "expected_bridge_generation": None,
            "barrier_id": None,
            "barrier_phase": None,
            "barrier_manifest_sha256": None,
            "issued_host_raw_ns": 5_000_000_000,
            "deadline_host_raw_ns": None,
            "request_body_sha256": None,
            "body_sha256": None,
            "body": body,
        }
        message.update(overrides)
        message["body_sha256"] = body_sha256_of(message["body"])
        return message

    def test_start_intent_event_valid(self):
        validate_runtime_event(self._intent_event())

    def test_start_intent_null_artifact_rejected(self):
        with self.assertRaises(IpcError):
            validate_runtime_event(
                self._intent_event(body_overrides={"artifact_sha256": None})
            )

    def test_start_intent_null_bridge_generation_rejected(self):
        with self.assertRaises(IpcError):
            validate_runtime_event(
                self._intent_event(body_overrides={"bridge_generation": None})
            )

    def test_command_terminal_requires_command_binding(self):
        with self.assertRaises(IpcError):
            validate_runtime_event(
                self._intent_event(
                    body_overrides={
                        "event_type": "COMMAND_TERMINAL",
                        "terminal_status": "COMPLETED",
                    }
                )
            )

    def test_tbb_active_event_valid(self):
        validate_runtime_event(
            self._intent_event(body_overrides={"endpoint_state": "TBB_ACTIVE"})
        )


class TC_STAGE7_IPC_MANIFEST(unittest.TestCase):
    def test_manifest_order(self):
        entries = (("MASS-A", "1" * 64), ("MASS-B", "2" * 64), ("ROC", "3" * 64))
        digest = build_barrier_manifest(entries)
        self.assertEqual(len(digest), 64)
        with self.assertRaises(IpcError):
            build_barrier_manifest((("ROC", "3" * 64), ("MASS-A", "1" * 64)))


if __name__ == "__main__":
    unittest.main()
