"""Stage 8 오류 severity mapping과 고정 상한의 단일 정의 시험(명세 §14, §10, §11; REQ-23).

플랫폼 무관.
"""

from __future__ import annotations

import unittest

from vdeter.stage8.poc_api import limits
from vdeter.stage8.poc_api.errors import (
    ERROR_SEVERITY,
    PocError,
    PocErrorCode,
    Severity,
    escalate,
    severity_of,
)

#: 명세 §14.2 고정 표.
EXPECTED_SEVERITY = {
    "POC_NOT_RUNNING": "REQUEST_REJECTED",
    "POC_CAPABILITY_DENIED": "REQUEST_REJECTED",
    "POC_TARGET_DENIED": "REQUEST_REJECTED",
    "POC_ACTION_SCHEMA_INVALID": "REQUEST_REJECTED",
    "POC_ACTION_TOO_LARGE": "REQUEST_REJECTED",
    "POC_ACTION_LIMIT_EXCEEDED": "REQUEST_REJECTED",
    "POC_ATTACK_INFLIGHT": "REQUEST_REJECTED",
    "POC_DEADLINE_MISSED": "REQUEST_REJECTED",
    "POC_RADIO_RESOURCE_CONFLICT": "REQUEST_REJECTED",
    "POC_TRANSPORT_UNAVAILABLE": "POC_ABORT",
    "POC_AUTH_FAILED": "POC_ABORT",
    "POC_VERSION_INCOMPATIBLE": "POC_ABORT",
    "POC_CAPABILITY_UNAVAILABLE": "POC_ABORT",
    "POC_PROTOCOL_ERROR": "POC_ABORT",
    "POC_REQUEST_ID_CONFLICT": "POC_ABORT",
    "POC_EVENT_QUEUE_OVERFLOW": "POC_ABORT",
    "POC_EVENT_REPLAY_GAP": "POC_ABORT",
    "POC_CALLBACK_EXCEPTION": "POC_ABORT",
    "POC_CALLBACK_TIMEOUT": "POC_ABORT",
    "POC_CONNECTION_LOST": "POC_ABORT",
    "POC_OUTPUT_LIMIT_EXCEEDED": "POC_ABORT",
    "POC_PROCESS_EXITED": "POC_ABORT",
    "POC_PROCESS_FORCED_KILL": "POC_ABORT",
    "POC_TX_OUTCOME_UNKNOWN": "SESSION_INHIBIT",
    "POC_RADIO_OWNERSHIP_UNCERTAIN": "SESSION_INHIBIT",
    "POC_CLOCK_STATE_UNCERTAIN": "SESSION_INHIBIT",
    "POC_DEVICE_HANDLE_UNCERTAIN": "SESSION_INHIBIT",
}


class ErrorSeverityTest(unittest.TestCase):
    def test_S8_POC_API_REQ_23_every_code_has_fixed_severity(self):
        codes = {c.value for c in PocErrorCode}
        self.assertEqual(codes, set(EXPECTED_SEVERITY))
        self.assertEqual(set(ERROR_SEVERITY), set(PocErrorCode))
        for code_str, sev in EXPECTED_SEVERITY.items():
            self.assertEqual(severity_of(PocErrorCode(code_str)).value, sev, code_str)

    def test_escalate_picks_highest(self):
        self.assertEqual(
            escalate(Severity.REQUEST_REJECTED, Severity.SESSION_INHIBIT, Severity.POC_ABORT),
            Severity.SESSION_INHIBIT,
        )
        self.assertEqual(
            escalate(Severity.REQUEST_REJECTED, Severity.POC_ABORT), Severity.POC_ABORT
        )

    def test_pocerror_carries_code_severity_no_secret(self):
        err = PocError(PocErrorCode.POC_AUTH_FAILED, "token 불일치", detail={"actual_uid": 1000})
        value = err.as_json_value()
        self.assertEqual(value["code"], "POC_AUTH_FAILED")
        self.assertEqual(value["severity"], "POC_ABORT")
        self.assertNotIn("token", value["detail"])


class LimitsSingleSourceTest(unittest.TestCase):
    def test_resource_limits_match_spec(self):
        self.assertEqual(limits.MAX_WIRE_PACKET_BYTES, 256 * 1024)
        self.assertEqual(limits.MAX_EVENT_QUEUE_MESSAGES, 256)
        self.assertEqual(limits.MAX_EVENT_QUEUE_BYTES, 2 * 1024 * 1024)
        self.assertEqual(limits.MAX_REPLAY_RING_MESSAGES, 256)
        self.assertEqual(limits.MAX_REPLAY_RING_BYTES, 2 * 1024 * 1024)
        self.assertEqual(limits.MAX_ACTION_DESCRIPTOR_BYTES, 64 * 1024)
        self.assertEqual(limits.MAX_WORKLOAD_ACTION_REQUESTS, 32)
        self.assertEqual(limits.MAX_INFLIGHT_ACTIONS, 1)
        self.assertEqual(limits.MAX_JSON_NESTING, 16)

    def test_callback_deadlines_match_spec(self):
        self.assertEqual(limits.CALLBACK_ON_START_DEADLINE_S, 3.0)
        self.assertEqual(limits.CALLBACK_ON_EVENT_DEADLINE_S, 0.250)
        self.assertEqual(limits.CALLBACK_ON_FINISH_DEADLINE_S, 2.0)
        self.assertEqual(limits.HELLO_DEADLINE_S, 3.0)
        self.assertEqual(limits.ACK_BATCH_EVENTS, 16)
        self.assertEqual(limits.ACK_MAX_DELAY_S, 0.050)

    def test_reconnect_and_cleanup_deadlines_match_spec(self):
        self.assertEqual(limits.RECONNECT_TOTAL_DEADLINE_S, 2.0)
        self.assertEqual(limits.RECONNECT_MAX_ATTEMPTS, 3)
        self.assertEqual(limits.RECONNECT_BACKOFFS_S, (0.100, 0.300, 0.600))
        self.assertEqual(limits.FINISH_WAIT_S, 2.0)
        self.assertEqual(limits.CLOSE_VOLUNTARY_WAIT_S, 2.0)
        self.assertEqual(limits.SIGTERM_WAIT_S, 1.0)

    def test_stdio_and_socket_limits_match_spec(self):
        self.assertEqual(limits.STDIO_RETAIN_BYTES_PER_STREAM, 64 * 1024)
        self.assertEqual(limits.STDIO_RETAIN_HEAD_BYTES, 32 * 1024)
        self.assertEqual(limits.STDIO_RETAIN_TAIL_BYTES, 32 * 1024)
        self.assertEqual(limits.STDIO_TOTAL_DRAIN_LIMIT_BYTES, 256 * 1024)
        self.assertEqual(limits.TOKEN_BYTES, 32)
        self.assertEqual(limits.WORKLOAD_NONCE_BYTES, 16)
        self.assertEqual(limits.MAX_UNIX_PATH_BYTES, 108)
        self.assertEqual((limits.SOCKET_ROOT_MODE, limits.WORKLOAD_DIR_MODE, limits.SOCKET_MODE),
                         (0o700, 0o700, 0o600))


if __name__ == "__main__":
    unittest.main()
