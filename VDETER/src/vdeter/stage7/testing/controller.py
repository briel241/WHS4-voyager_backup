"""Stage 7 TEST-only Scenario Controller (`stage7` §16.2).

test runner와 evidence collector를 합친 별도 process 로직이다. SUT나 운영
dependency가 아니며 Runtime Coordinator, endpoint와 worker의 기능을 대신하지
않는다. 시험 API fixture·fault injection·반복·사후 비교만 소유한다.
"""

from __future__ import annotations

import hashlib
import uuid as _uuid

from ..canonical_json import encode_canonical_json
from ..ipc import TEST_PROTOCOL, body_sha256_of


class Stage7ScenarioController:
    """§16.2 test command 발행자. transport는 주입형이다.

    `adapter_channels`: {endpoint_id: channel} — channel.request(message)가
    endpoint test adapter의 response를 반환한다.
    """

    __slots__ = (
        "run_id",
        "resolved_test_plan_sha256",
        "_channels",
        "_host_raw_clock",
        "command_log",
    )

    def __init__(
        self,
        *,
        run_id: str,
        resolved_test_plan_sha256: str,
        adapter_channels: dict,
        host_raw_clock,
    ) -> None:
        self.run_id = run_id
        self.resolved_test_plan_sha256 = resolved_test_plan_sha256
        self._channels = dict(adapter_channels)
        self._host_raw_clock = host_raw_clock
        self.command_log = []

    def _command(self, endpoint_id: str, command_name: str, body: dict) -> dict:
        issued = int(self._host_raw_clock())
        message = {
            "protocol": TEST_PROTOCOL,
            "kind": "command",
            "command_name": command_name,
            "run_id": self.run_id,
            "resolved_test_plan_sha256": self.resolved_test_plan_sha256,
            "command_id": str(_uuid.uuid4()),
            "endpoint_id": endpoint_id,
            "expected_lifecycle_generation": 1,
            "expected_bridge_generation": 0,
            "issued_host_raw_ns": issued,
            "deadline_host_raw_ns": issued + 30_000_000_000,
            "request_body_sha256": None,
            "body_sha256": None,
            "body": body,
        }
        digest = body_sha256_of(body)
        message["request_body_sha256"] = digest
        message["body_sha256"] = digest
        return message

    def invoke_service_fixture(
        self,
        endpoint_id: str,
        *,
        scenario_id: str,
        step_id: str,
        service_operation: str,
        local_fixture_relpath: str,
        local_fixture_sha256: str,
        trigger_host_raw_ns: int | None = None,
    ) -> dict:
        message = self._command(
            endpoint_id,
            "invoke_service_fixture",
            {
                "scenario_id": scenario_id,
                "step_id": step_id,
                "service_operation": service_operation,
                "local_fixture_relpath": local_fixture_relpath,
                "local_fixture_sha256": local_fixture_sha256,
                "trigger_host_raw_ns": (
                    trigger_host_raw_ns
                    if trigger_host_raw_ns is not None
                    else int(self._host_raw_clock())
                ),
            },
        )
        response = self._channels[endpoint_id].request(message)
        self.command_log.append(
            {
                "endpoint_id": endpoint_id,
                "command": "invoke_service_fixture",
                "step_id": step_id,
                "status": response.get("status"),
                "call_outcome": (response.get("body") or {}).get("call_outcome"),
            }
        )
        return response

    def inject_test_fault(
        self,
        endpoint_id: str,
        *,
        scenario_id: str,
        step_id: str,
        fault_fixture_id: str,
        target_correlation_sha256: str,
        trigger_host_raw_ns: int | None = None,
    ) -> dict:
        message = self._command(
            endpoint_id,
            "inject_test_fault",
            {
                "scenario_id": scenario_id,
                "step_id": step_id,
                "fault_fixture_id": fault_fixture_id,
                "target_correlation_sha256": target_correlation_sha256,
                "trigger_host_raw_ns": (
                    trigger_host_raw_ns
                    if trigger_host_raw_ns is not None
                    else int(self._host_raw_clock())
                ),
            },
        )
        response = self._channels[endpoint_id].request(message)
        self.command_log.append(
            {
                "endpoint_id": endpoint_id,
                "command": "inject_test_fault",
                "step_id": step_id,
                "status": response.get("status"),
            }
        )
        return response

    def test_status(self, endpoint_id: str, query_command_id: str | None) -> dict:
        message = self._command(
            endpoint_id, "test_status", {"query_command_id": query_command_id}
        )
        return self._channels[endpoint_id].request(message)

    @staticmethod
    def fixture_bytes(value: dict) -> tuple:
        """(exact bytes, sha256) — source endpoint의 local fixture preload용."""

        data = encode_canonical_json(value)
        return data, hashlib.sha256(data).hexdigest()


__all__ = ("Stage7ScenarioController",)
