"""Stage 7 Runtime/TEST IPC canonical 계약 (`stage7` §16, §16.1, §16.2).

socket transport와 분리된 순수 codec·validation 계층이다. 한 `SOCK_SEQPACKET`
record당 canonical UTF-8 JSON 하나이고 record 전체가 최대 `1 MiB`다.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from .canonical_json import (
    CanonicalJsonError,
    decode_canonical_json,
    encode_canonical_json,
    sha256_hex,
)
from .errors import Stage7PrivateErrorCode

RUNTIME_PROTOCOL = "vdeter.stage7.runtime-coordinator.v1"
TEST_PROTOCOL = "vdeter.stage7.scenario-controller.v1"
RUNTIME_BODY_SCHEMA = "STAGE7_RUNTIME_BODY_V1"
TEST_BODY_SCHEMA = "STAGE7_SCENARIO_BODY_V1"
MAX_RECORD_BYTES = 1_048_576
MAX_U63 = (1 << 63) - 1
MAX_U31 = (1 << 31) - 1
MAX_RATIONAL_DIGITS = 256

ENDPOINT_IDS = ("MASS-A", "MASS-B", "ROC")
PATH_IDS = ("C1", "C2", "C3", "C4", "C5", "C6")

RUNTIME_COMMANDS = (
    "prepare",
    "configure",
    "arm_rx",
    "start_cfo",
    "acquire_host_time",
    "calibrate_rf",
    "validate_rf_time",
    "prepare_active_bridge",
    "commit_active_bridge",
    "abort_active_bridge",
    "open_service_admission",
    "commit_service_start",
    "publish_service_start",
    "resume_runtime",
    "status",
    "stop",
    "emergency_inhibit",
)

#: §16 Runtime Coordinator-side hard timeout (seconds).
COMMAND_TIMEOUTS_S = {
    "prepare": 30,
    "configure": 30,
    "arm_rx": 30,
    "start_cfo": 420,
    "acquire_host_time": 610,
    "calibrate_rf": 1800,
    "validate_rf_time": 900,
    "prepare_active_bridge": 5,
    "commit_active_bridge": 5,
    "abort_active_bridge": 5,
    "open_service_admission": 5,
    "commit_service_start": 5,
    "publish_service_start": 5,
    "resume_runtime": 5,
    "status": 2,
    "stop": 30,
    "emergency_inhibit": 2,
}

#: barrier 없는 command.
NON_BARRIER_COMMANDS = frozenset({"status", "emergency_inhibit"})

ACTIVE_BRIDGE_COMMANDS = frozenset(
    {"prepare_active_bridge", "commit_active_bridge", "abort_active_bridge"}
)
ACTIVE_BRIDGE_PHASE = {
    "prepare_active_bridge": "PREPARE",
    "commit_active_bridge": "COMMIT",
    "abort_active_bridge": "ABORT",
}

ENDPOINT_STATES = (
    "CONFIGURED", "PREFLIGHT", "RX_READY", "CFO_PROBE_ONLY", "CFO_VALIDATED",
    "HOST_TIME_ACQUIRING", "PROVISIONAL_HOST_BRIDGE", "RF_GAIN_SEARCHING",
    "RF_GAIN_SEALED", "RF_GAIN_REVALIDATING", "RF_GAIN_REVALIDATED",
    "RF_TIME_PRE_VALIDATING", "ACTIVE_BRIDGE_PREPARED", "ACTIVE_BRIDGE_COMMITTED",
    "RF_TIME_POST_VALIDATING", "HOST_TIME_LOCKED", "ARMED",
    "SERVICE_ADMISSION_READY", "SERVICE_START_PENDING", "SERVICE_START_STAGED",
    "TBB_ACQUIRING", "TBB_ACTIVE", "RUNNING", "DRAINING", "TX_INHIBITED",
    "HOST_TIME_REACQUIRING", "STOPPED",
)

EVENT_TYPES = ("COMMAND_TERMINAL", "STATE_CHANGED", "DEVICE_FAULT", "COORDINATOR_HEALTH")

RESPONSE_STATUS_BY_PHASE = {
    "PREPARE": ("PREPARED", "REJECTED", "TIMED_OUT"),
    "COMMIT": ("ACCEPTED", "COMPLETED", "REJECTED", "TIMED_OUT"),
    "ABORT": ("COMPLETED", "REJECTED", "TIMED_OUT"),
}
STATUS_RESPONSE_STATUSES = ("COMPLETED", "REJECTED", "TIMED_OUT")
EMERGENCY_RESPONSE_STATUSES = ("ACCEPTED", "COMPLETED", "REJECTED", "TIMED_OUT")
#: 측정 result를 terminal response에 결속하는 두 command의 COMMIT은 ACCEPTED 금지.
MEASUREMENT_COMMANDS = frozenset({"acquire_host_time", "validate_rf_time"})

SERVICE_START_INTENT_TIMEOUT_NS = 30_000_000_000
SERVICE_START_TBB_PUBLISH_TIMEOUT_NS = 90_000_000_000

_ENVELOPE_KEYS = frozenset(
    {
        "protocol", "kind", "command_name", "run_id", "resolved_system_plan_sha256",
        "command_id", "endpoint_id", "expected_lifecycle_generation",
        "expected_bridge_generation", "barrier_id", "barrier_phase",
        "barrier_manifest_sha256", "issued_host_raw_ns", "deadline_host_raw_ns",
        "request_body_sha256", "body_sha256", "body",
    }
)
_RESPONSE_EXTRA_KEYS = frozenset({"status"})
_EVENT_EXTRA_KEYS = frozenset(
    {
        "event_type", "endpoint_state", "lifecycle_generation", "bridge_generation",
        "terminal_command_id", "terminal_status", "artifact_sha256", "private_code",
    }
)

_STATE_CHANGING_BODY_COMMON = ("logical_action", "phase_plan_sha256", "commit_at_host_raw_ns")

#: command-specific body의 정확한 필수 field (§16.1 표). 값은 (name, type-tag).
COMMAND_BODY_FIELDS = {
    "prepare": (
        ("deployment_sha256", "sha256"),
        ("endpoint_plan_relpath", "relpath"),
        ("endpoint_plan_sha256", "sha256"),
        ("environment_manifest_sha256", "sha256"),
        ("member_set_sha256", "sha256"),
        ("p1_member_plan_relpath", "relpath|null"),
        ("p1_member_plan_sha256", "sha256|null"),
    ),
    "configure": (
        ("endpoint_config_sha256", "sha256"),
        ("expected_serial", "serial"),
        ("usb_physical_path", "string160"),
        ("radio_generation", "u31"),
    ),
    "arm_rx": (
        ("rx_plan_sha256", "sha256"),
        ("ready_deadline_host_raw_ns", "u63ns"),
    ),
    "start_cfo": (
        ("cfo_plan_sha256", "sha256"),
        ("path_ids", "path_ids"),
        ("operation_deadline_host_raw_ns", "u63ns"),
    ),
    "acquire_host_time": (
        ("profile_id", "literal:STAGE7_PI_HOST_AFFINE_TIME_V1"),
        ("acquisition_attempt", "u31"),
        ("max_acquisition_ns", "literal_int:600000000000"),
    ),
    "calibrate_rf": (
        # ADAPTIVE_SEARCH_AND_SEAL: §9.2.1 RESEARCH_READY_MINIMAL adaptive 절차 (0.9.0 additive).
        ("calibration_mode", "enum:SEARCH_AND_SEAL,APPLY_AND_REVERIFY,ADAPTIVE_SEARCH_AND_SEAL"),
        ("gain_plan_sha256", "sha256"),
        ("source_gain_artifact_sha256", "sha256|null"),
        ("operational_config_sha256", "sha256|null"),
        ("path_ids", "path_ids"),
    ),
    "validate_rf_time": (
        # RAW_PHY: v0.9.1 D항 — path별 100 burst 실측 raw PHY 자격 (additive).
        ("validation_phase", "enum:PRE_CORRECTION,POST_CORRECTION,RAW_PHY"),
        ("rf_time_plan_sha256", "sha256"),
        ("clock_id", "clock_id"),
        ("path_ids", "path_ids"),
    ),
    "prepare_active_bridge": (
        ("candidate_set", "member_digest_set"),
        ("candidate_set_sha256", "sha256"),
        ("candidate_sha256", "sha256"),
        ("active_clock_id", "clock_id"),
        ("bridge_generation", "u31"),
        ("alpha_num", "srat"),
        ("alpha_den", "urat"),
        ("beta_num", "urat"),
        ("beta_den", "urat"),
        ("correction_num", "srat"),
        ("correction_den", "urat"),
    ),
    "open_service_admission": (
        ("intent_window_id", "uuid"),
        ("intent_timeout_ns", "literal_int:30000000000"),
        ("tbb_publish_timeout_ns", "literal_int:90000000000"),
        ("service_start_deadline_host_raw_ns", "u63ns"),
        ("tbb_publish_deadline_host_raw_ns", "u63ns"),
    ),
    "commit_service_start": (
        ("intent_window_id", "uuid"),
        ("intent_set", "member_digest_set"),
        ("intent_set_sha256", "sha256"),
        ("service_start_deadline_host_raw_ns", "u63ns"),
        ("tbb_publish_deadline_host_raw_ns", "u63ns"),
    ),
    "publish_service_start": (
        ("start_result_set", "member_digest_set"),
        ("start_result_set_sha256", "sha256"),
        ("tbb_active_set", "member_digest_set"),
        ("tbb_active_set_sha256", "sha256"),
        ("publish_at_host_raw_ns", "u63ns"),
    ),
    "resume_runtime": (
        ("tbb_active_set", "member_digest_set"),
        ("tbb_active_set_sha256", "sha256"),
        ("bridge_generation", "u31"),
        ("resume_at_host_raw_ns", "u63ns"),
    ),
    "status": (("query_command_id", "uuid|null"),),
    "stop": (
        ("stop_at_frame_ordinal", "u63ns"),
        ("stop_deadline_host_raw_ns", "u63ns"),
    ),
    "emergency_inhibit": (
        ("private_code", "ascii64"),
        ("root_cause_id", "uuid"),
    ),
}
COMMAND_BODY_FIELDS["commit_active_bridge"] = COMMAND_BODY_FIELDS["prepare_active_bridge"]
COMMAND_BODY_FIELDS["abort_active_bridge"] = COMMAND_BODY_FIELDS["prepare_active_bridge"]


class Stage7IpcError(ValueError):
    """`CONTROL_MESSAGE_INVALID` — framing/schema/type 위반."""

    private_code = Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID


def is_u31(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= MAX_U31


def is_u63(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= MAX_U63


def is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def is_uuid(value) -> bool:
    if not isinstance(value, str) or len(value) != 36 or value != value.lower():
        return False
    try:
        return str(_uuid.UUID(value)) == value
    except ValueError:
        return False


def is_run_id(value) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._-"
    return value[0] in "abcdefghijklmnopqrstuvwxyz0123456789" and all(
        c in allowed for c in value
    )


def is_clock_id(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("ascii", "ignore")) <= 160
        and value.isascii()
        and value.isprintable()
    )


def is_serial(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and value.isascii()
        and value.isprintable()
    )


def is_relpath(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8")) > 256 or value.startswith("/") or "\\" in value:
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


def is_rational_digits(value, *, positive: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    digits = len(str(abs(value)))
    if digits > MAX_RATIONAL_DIGITS:
        return False
    if positive and value <= 0:
        return False
    return True


def _check_typed(name: str, value, type_tag: str) -> None:
    def fail(reason: str):
        raise Stage7IpcError(f"body.{name}: {reason}")

    optional = type_tag.endswith("|null")
    base = type_tag[: -len("|null")] if optional else type_tag
    if value is None:
        if optional:
            return
        fail("null이 허용되지 않습니다")
    if base == "sha256":
        if not is_sha256(value):
            fail("sha256이 아닙니다")
    elif base == "uuid":
        if not is_uuid(value):
            fail("lowercase canonical UUID가 아닙니다")
    elif base == "u31":
        if not is_u31(value):
            fail("u31이 아닙니다")
    elif base == "u63ns":
        if not is_u63(value):
            fail("u63ns가 아닙니다")
    elif base == "relpath":
        if not is_relpath(value):
            fail("relpath 규칙 위반입니다")
    elif base == "serial":
        if not is_serial(value):
            fail("serial 형식이 아닙니다")
    elif base == "clock_id":
        if not is_clock_id(value):
            fail("clock_id 형식이 아닙니다")
    elif base == "string160":
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 160:
            fail("1..160 byte 문자열이 아닙니다")
    elif base == "ascii64":
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 64
            or not value.isascii()
        ):
            fail("1..64 ASCII 문자열이 아닙니다")
    elif base == "srat":
        if not is_rational_digits(value, positive=False):
            fail("srat 범위 위반입니다")
    elif base == "urat":
        if not is_rational_digits(value, positive=True):
            fail("urat 범위 위반입니다")
    elif base == "path_ids":
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 6
            or any(item not in PATH_IDS for item in value)
            or len(set(value)) != len(value)
            or value != sorted(value, key=PATH_IDS.index)
        ):
            fail("canonical path_id 배열이 아닙니다")
    elif base == "member_digest_set":
        if not isinstance(value, list) or not 2 <= len(value) <= 3:
            fail("(endpoint_id,sha256) 배열 2..3이 아닙니다")
        members = []
        for entry in value:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or entry[0] not in ENDPOINT_IDS
                or not is_sha256(entry[1])
            ):
                fail("member digest entry 형식 위반입니다")
            members.append(entry[0])
        if len(set(members)) != len(members):
            fail("duplicate member가 있습니다")
        order = [ENDPOINT_IDS.index(member) for member in members]
        if order != sorted(order):
            fail("canonical MASS-A,MASS-B,ROC 순서가 아닙니다")
    elif base.startswith("literal:"):
        if value != base.split(":", 1)[1]:
            fail("canonical literal이 아닙니다")
    elif base.startswith("literal_int:"):
        expected = int(base.split(":", 1)[1])
        if isinstance(value, bool) or value != expected:
            fail("고정 정수 값이 아닙니다")
    elif base.startswith("enum:"):
        if value not in base.split(":", 1)[1].split(","):
            fail("허용 enum 값이 아닙니다")
    else:  # pragma: no cover - 정의 오류
        fail(f"알 수 없는 type tag {base}")


def body_sha256_of(body: dict) -> str:
    return sha256_hex(encode_canonical_json(body))


def encode_message(message: dict) -> bytes:
    encoded = encode_canonical_json(message)
    if len(encoded) > MAX_RECORD_BYTES:
        raise Stage7IpcError("record가 1 MiB를 넘습니다")
    return encoded


def decode_message(data: bytes) -> dict:
    if len(data) > MAX_RECORD_BYTES:
        raise Stage7IpcError("record가 1 MiB를 넘습니다")
    try:
        value = decode_canonical_json(data)
    except CanonicalJsonError as error:
        raise Stage7IpcError(f"canonical JSON 위반: {error}") from error
    if not isinstance(value, dict):
        raise Stage7IpcError("record는 object여야 합니다")
    return value


def validate_runtime_command(message: dict) -> None:
    """§16/§16.1 command envelope+body 검증. 위반은 `Stage7IpcError`다."""

    keys = set(message)
    if keys != _ENVELOPE_KEYS:
        raise Stage7IpcError(
            f"command envelope key 집합 위반: {sorted(keys ^ _ENVELOPE_KEYS)}"
        )
    if message["protocol"] != RUNTIME_PROTOCOL:
        raise Stage7IpcError("protocol literal 위반")
    if message["kind"] != "command":
        raise Stage7IpcError("kind는 command여야 합니다")
    command_name = message["command_name"]
    if command_name not in RUNTIME_COMMANDS:
        raise Stage7IpcError(f"허용되지 않은 command_name: {command_name!r}")
    if not is_run_id(message["run_id"]):
        raise Stage7IpcError("run_id 형식 위반")
    if not is_sha256(message["resolved_system_plan_sha256"]):
        raise Stage7IpcError("resolved_system_plan_sha256 위반")
    if not is_uuid(message["command_id"]):
        raise Stage7IpcError("command_id UUID 위반")
    if message["endpoint_id"] not in ENDPOINT_IDS:
        raise Stage7IpcError("endpoint_id 위반")
    if not is_u31(message["expected_lifecycle_generation"]):
        raise Stage7IpcError("expected_lifecycle_generation 위반")
    expected_bridge = message["expected_bridge_generation"]
    if expected_bridge is not None and not is_u31(expected_bridge):
        raise Stage7IpcError("expected_bridge_generation 위반")
    issued = message["issued_host_raw_ns"]
    deadline = message["deadline_host_raw_ns"]
    if not is_u63(issued) or not is_u63(deadline) or deadline <= issued:
        raise Stage7IpcError("issued/deadline 위반")
    for key in ("request_body_sha256", "body_sha256"):
        if not is_sha256(message[key]):
            raise Stage7IpcError(f"{key} 위반")
    body = message["body"]
    if not isinstance(body, dict):
        raise Stage7IpcError("body는 object여야 합니다")
    if body_sha256_of(body) != message["body_sha256"]:
        raise Stage7IpcError("body_sha256이 body와 다릅니다")
    if message["request_body_sha256"] != message["body_sha256"]:
        raise Stage7IpcError("command의 request_body_sha256은 body_sha256과 같아야 합니다")

    # barrier field 규칙.
    if command_name in NON_BARRIER_COMMANDS:
        for key in ("barrier_id", "barrier_phase", "barrier_manifest_sha256"):
            if message[key] is not None:
                raise Stage7IpcError(f"{command_name}의 {key}는 null이어야 합니다")
    else:
        if not is_uuid(message["barrier_id"]):
            raise Stage7IpcError("barrier_id UUID 위반")
        if message["barrier_phase"] not in ("PREPARE", "COMMIT", "ABORT"):
            raise Stage7IpcError("barrier_phase 위반")
        if not is_sha256(message["barrier_manifest_sha256"]):
            raise Stage7IpcError("barrier_manifest_sha256 위반")
        if command_name in ACTIVE_BRIDGE_COMMANDS:
            if message["barrier_phase"] != ACTIVE_BRIDGE_PHASE[command_name]:
                raise Stage7IpcError("active-bridge verb와 barrier_phase 불일치")

    # body schema.
    expected_fields = COMMAND_BODY_FIELDS[command_name]
    field_names = tuple(name for name, _ in expected_fields)
    if command_name in NON_BARRIER_COMMANDS:
        allowed = set(field_names)
    else:
        allowed = set(field_names) | set(_STATE_CHANGING_BODY_COMMON)
    if set(body) != allowed:
        raise Stage7IpcError(
            f"{command_name} body key 집합 위반: {sorted(set(body) ^ allowed)}"
        )
    for name, type_tag in expected_fields:
        _check_typed(name, body[name], type_tag)
    if command_name not in NON_BARRIER_COMMANDS:
        expected_action = (
            "active_bridge_install"
            if command_name in ACTIVE_BRIDGE_COMMANDS
            else command_name
        )
        if body["logical_action"] != expected_action:
            raise Stage7IpcError("logical_action 위반")
        if not is_sha256(body["phase_plan_sha256"]):
            raise Stage7IpcError("phase_plan_sha256 위반")
        commit_at = body["commit_at_host_raw_ns"]
        if not is_u63(commit_at):
            raise Stage7IpcError("commit_at_host_raw_ns 위반")
        if commit_at > deadline:
            raise Stage7IpcError("commit_at이 deadline 이후입니다")

    # command별 추가 규칙.
    if command_name == "prepare":
        p1_relpath = body["p1_member_plan_relpath"]
        p1_sha = body["p1_member_plan_sha256"]
        if (p1_relpath is None) != (p1_sha is None):
            raise Stage7IpcError("P1 member plan field는 둘 다 null이거나 둘 다 non-null이어야 합니다")
    if command_name == "calibrate_rf":
        mode = body["calibration_mode"]
        has_artifacts = (
            body["source_gain_artifact_sha256"] is not None
            and body["operational_config_sha256"] is not None
        )
        if mode == "SEARCH_AND_SEAL" and (
            body["source_gain_artifact_sha256"] is not None
            or body["operational_config_sha256"] is not None
        ):
            raise Stage7IpcError("SEARCH_AND_SEAL은 두 SHA field가 null이어야 합니다")
        if mode == "APPLY_AND_REVERIFY" and not has_artifacts:
            raise Stage7IpcError("APPLY_AND_REVERIFY는 두 SHA field가 non-null이어야 합니다")
    if command_name == "open_service_admission":
        commit_at = body["commit_at_host_raw_ns"]
        start_deadline = body["service_start_deadline_host_raw_ns"]
        tbb_deadline = body["tbb_publish_deadline_host_raw_ns"]
        if commit_at + SERVICE_START_INTENT_TIMEOUT_NS > MAX_U63:
            raise Stage7IpcError("intent deadline checked-add overflow")
        if start_deadline != commit_at + SERVICE_START_INTENT_TIMEOUT_NS:
            raise Stage7IpcError("service_start_deadline 식 위반")
        if start_deadline + SERVICE_START_TBB_PUBLISH_TIMEOUT_NS > MAX_U63:
            raise Stage7IpcError("TBB publish deadline checked-add overflow")
        if tbb_deadline != start_deadline + SERVICE_START_TBB_PUBLISH_TIMEOUT_NS:
            raise Stage7IpcError("tbb_publish_deadline 식 위반")
    if command_name == "commit_service_start":
        if message["issued_host_raw_ns"] >= body["service_start_deadline_host_raw_ns"]:
            raise Stage7IpcError("commit_service_start는 intent cutoff 전에만 허용됩니다")
    if command_name == "publish_service_start":
        if body["publish_at_host_raw_ns"] != body.get("commit_at_host_raw_ns"):
            raise Stage7IpcError("publish_at은 commit_at과 exact-equal이어야 합니다")
    if command_name == "resume_runtime":
        if body["bridge_generation"] != message["expected_bridge_generation"]:
            raise Stage7IpcError("resume bridge_generation과 envelope 불일치")
        if body["resume_at_host_raw_ns"] != body.get("commit_at_host_raw_ns"):
            raise Stage7IpcError("resume_at은 commit_at과 exact-equal이어야 합니다")
    if command_name in ACTIVE_BRIDGE_COMMANDS:
        for name in ("alpha_den", "beta_num", "beta_den", "correction_den"):
            if body[name] <= 0:
                raise Stage7IpcError(f"{name}은 양수여야 합니다")
        from math import gcd

        if gcd(abs(body["alpha_num"]), body["alpha_den"]) != 1 and body["alpha_num"] != 0:
            raise Stage7IpcError("alpha rational이 gcd-reduced가 아닙니다")
        if gcd(body["beta_num"], body["beta_den"]) != 1:
            raise Stage7IpcError("beta rational이 gcd-reduced가 아닙니다")
        if (
            gcd(abs(body["correction_num"]), body["correction_den"]) != 1
            and body["correction_num"] != 0
        ):
            raise Stage7IpcError("correction rational이 gcd-reduced가 아닙니다")
        entries = {entry[0]: entry[1] for entry in body["candidate_set"]}
        if message["endpoint_id"] not in entries:
            raise Stage7IpcError("candidate_set에 자기 entry가 없습니다")
        if entries[message["endpoint_id"]] != body["candidate_sha256"]:
            raise Stage7IpcError("자기 candidate digest가 set entry와 다릅니다")
        if body_set_sha256(body["candidate_set"]) != body["candidate_set_sha256"]:
            raise Stage7IpcError("candidate_set_sha256 위반")
    if command_name == "commit_service_start":
        if body_set_sha256(body["intent_set"]) != body["intent_set_sha256"]:
            raise Stage7IpcError("intent_set_sha256 위반")
    if command_name == "publish_service_start":
        if body_set_sha256(body["start_result_set"]) != body["start_result_set_sha256"]:
            raise Stage7IpcError("start_result_set_sha256 위반")
        if body_set_sha256(body["tbb_active_set"]) != body["tbb_active_set_sha256"]:
            raise Stage7IpcError("tbb_active_set_sha256 위반")
    if command_name == "resume_runtime":
        if body_set_sha256(body["tbb_active_set"]) != body["tbb_active_set_sha256"]:
            raise Stage7IpcError("tbb_active_set_sha256 위반")


def body_set_sha256(member_set) -> str:
    """§16 canonical JSON으로 encode한 member digest 배열의 SHA-256."""

    return sha256_hex(encode_canonical_json(member_set))


_RESPONSE_BODY_KEYS = frozenset(
    {
        "status", "endpoint_state", "lifecycle_generation", "bridge_generation",
        "private_code", "result_sha256", "result",
    }
)


def validate_runtime_response(message: dict, command: dict) -> None:
    keys = set(message)
    if keys != _ENVELOPE_KEYS:
        raise Stage7IpcError("response envelope key 집합 위반")
    if message["kind"] != "response":
        raise Stage7IpcError("kind는 response여야 합니다")
    for key in (
        "protocol", "command_name", "run_id", "resolved_system_plan_sha256",
        "command_id", "endpoint_id", "expected_lifecycle_generation",
        "expected_bridge_generation", "barrier_id", "barrier_phase",
        "barrier_manifest_sha256", "issued_host_raw_ns", "deadline_host_raw_ns",
    ):
        if message[key] != command[key]:
            raise Stage7IpcError(f"response가 request {key}를 보존하지 않습니다")
    if message["request_body_sha256"] != command["body_sha256"]:
        raise Stage7IpcError("response request_body_sha256 위반")
    body = message["body"]
    if not isinstance(body, dict) or set(body) != _RESPONSE_BODY_KEYS:
        raise Stage7IpcError("response body key 집합 위반")
    if body_sha256_of(body) != message["body_sha256"]:
        raise Stage7IpcError("response body_sha256 위반")
    if body["endpoint_state"] not in ENDPOINT_STATES:
        raise Stage7IpcError("endpoint_state enum 위반")
    if not is_u31(body["lifecycle_generation"]):
        raise Stage7IpcError("lifecycle_generation 위반")
    if body["bridge_generation"] is not None and not is_u31(body["bridge_generation"]):
        raise Stage7IpcError("bridge_generation 위반")
    status = body["status"]
    command_name = command["command_name"]
    phase = command["barrier_phase"]
    if command_name == "status":
        allowed = STATUS_RESPONSE_STATUSES
    elif command_name == "emergency_inhibit":
        allowed = EMERGENCY_RESPONSE_STATUSES
    else:
        allowed = RESPONSE_STATUS_BY_PHASE[phase]
        if command_name in MEASUREMENT_COMMANDS and phase == "COMMIT":
            allowed = ("COMPLETED", "REJECTED", "TIMED_OUT")
    if status not in allowed:
        raise Stage7IpcError(f"{command_name}/{phase} response status 위반: {status}")
    result = body["result"]
    result_sha = body["result_sha256"]
    if result is None:
        if result_sha is not None:
            raise Stage7IpcError("result 없이 result_sha256을 가질 수 없습니다")
    else:
        if not is_sha256(result_sha) or body_sha256_of(result) != result_sha:
            raise Stage7IpcError("result_sha256 위반")


def validate_runtime_event(message: dict) -> None:
    keys = set(message)
    expected = (_ENVELOPE_KEYS - {"body"}) | {"body"}
    if keys != expected:
        raise Stage7IpcError("event envelope key 집합 위반")
    if message["kind"] != "event":
        raise Stage7IpcError("kind는 event여야 합니다")
    if message["protocol"] != RUNTIME_PROTOCOL:
        raise Stage7IpcError("protocol literal 위반")
    body = message["body"]
    if not isinstance(body, dict) or set(body) != {
        "event_type", "endpoint_state", "lifecycle_generation", "bridge_generation",
        "terminal_command_id", "terminal_status", "artifact_sha256", "private_code",
    }:
        raise Stage7IpcError("event body key 집합 위반")
    if body_sha256_of(body) != message["body_sha256"]:
        raise Stage7IpcError("event body_sha256 위반")
    event_type = body["event_type"]
    if event_type not in EVENT_TYPES:
        raise Stage7IpcError("event_type 위반")
    source_less = message["command_id"] is None
    if source_less:
        for key in (
            "command_name", "request_body_sha256", "expected_lifecycle_generation",
            "expected_bridge_generation", "deadline_host_raw_ns", "barrier_id",
            "barrier_phase", "barrier_manifest_sha256",
        ):
            if message[key] is not None:
                raise Stage7IpcError(f"source-less event의 {key}는 null이어야 합니다")
        if event_type == "COMMAND_TERMINAL":
            raise Stage7IpcError("COMMAND_TERMINAL은 command-bound여야 합니다")
    else:
        if not is_uuid(message["command_id"]):
            raise Stage7IpcError("command_id 위반")
        if message["command_name"] not in RUNTIME_COMMANDS:
            raise Stage7IpcError("command-bound event command_name 위반")
    if not is_u63(message["issued_host_raw_ns"]):
        raise Stage7IpcError("issued_host_raw_ns 위반")
    if event_type == "COMMAND_TERMINAL":
        if body["terminal_command_id"] != message["command_id"]:
            raise Stage7IpcError("terminal_command_id와 envelope command_id 불일치")
        if body["terminal_status"] not in ("COMPLETED", "REJECTED", "TIMED_OUT"):
            raise Stage7IpcError("terminal_status 위반")
    else:
        if body["terminal_status"] is not None and event_type == "STATE_CHANGED":
            raise Stage7IpcError("STATE_CHANGED의 terminal_status는 null이어야 합니다")
    if not is_u31(body["lifecycle_generation"]):
        raise Stage7IpcError("event lifecycle_generation 위반")
    if body["bridge_generation"] is not None and not is_u31(body["bridge_generation"]):
        raise Stage7IpcError("event bridge_generation 위반")
    state = body["endpoint_state"]
    if state is not None and state not in ENDPOINT_STATES:
        raise Stage7IpcError("event endpoint_state 위반")
    artifact = body["artifact_sha256"]
    if artifact is not None and not is_sha256(artifact):
        raise Stage7IpcError("artifact_sha256 위반")
    # §16.1 start-intent/TBB-active source-less 조합.
    if source_less and event_type == "STATE_CHANGED":
        if state == "SERVICE_START_PENDING":
            if (
                body["terminal_command_id"] is not None
                or body["terminal_status"] is not None
                or body["private_code"] is not None
                or artifact is None
                or body["bridge_generation"] is None
            ):
                raise Stage7IpcError("start-intent event 조합 위반")
        if state == "TBB_ACTIVE":
            if (
                body["terminal_command_id"] is not None
                or body["terminal_status"] is not None
                or body["private_code"] is not None
                or artifact is None
                or body["bridge_generation"] is None
            ):
                raise Stage7IpcError("TBB-active event 조합 위반")


def build_barrier_manifest(entries) -> str:
    """canonical member 순서의 `(endpoint_id, endpoint_body_sha256)` manifest SHA."""

    ordered = sorted(entries, key=lambda item: ENDPOINT_IDS.index(item[0]))
    if [item[0] for item in ordered] != [item[0] for item in entries]:
        raise Stage7IpcError("barrier manifest member 순서 위반")
    return sha256_hex(
        encode_canonical_json([[endpoint, digest] for endpoint, digest in entries])
    )


__all__ = (
    "ACTIVE_BRIDGE_COMMANDS",
    "COMMAND_BODY_FIELDS",
    "COMMAND_TIMEOUTS_S",
    "ENDPOINT_IDS",
    "ENDPOINT_STATES",
    "EVENT_TYPES",
    "MAX_RECORD_BYTES",
    "MEASUREMENT_COMMANDS",
    "NON_BARRIER_COMMANDS",
    "RESPONSE_STATUS_BY_PHASE",
    "RUNTIME_BODY_SCHEMA",
    "RUNTIME_COMMANDS",
    "RUNTIME_PROTOCOL",
    "SERVICE_START_INTENT_TIMEOUT_NS",
    "SERVICE_START_TBB_PUBLISH_TIMEOUT_NS",
    "Stage7IpcError",
    "TEST_BODY_SCHEMA",
    "TEST_PROTOCOL",
    "body_set_sha256",
    "body_sha256_of",
    "build_barrier_manifest",
    "decode_message",
    "encode_message",
    "is_relpath",
    "is_run_id",
    "is_sha256",
    "is_u31",
    "is_u63",
    "is_uuid",
    "validate_runtime_command",
    "validate_runtime_event",
    "validate_runtime_response",
)
