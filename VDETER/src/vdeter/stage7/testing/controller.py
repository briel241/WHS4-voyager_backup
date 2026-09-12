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


class PocDeviceCallError(RuntimeError):
    """launcher PoC device op이 거부·실패했다. 성공으로 위장하지 않는다."""


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

    def invoke_service_inline(
        self,
        endpoint_id: str,
        *,
        scenario_id: str,
        step_id: str,
        service_operation: str,
        request: dict,
        trigger_host_raw_ns: int | None = None,
    ) -> dict:
        """§16.2 최소 확장 — canonical inline 값으로 SUBMIT/STATUS를 시킨다.

        adapter가 inline 값으로 public request DTO를 만들어 실제
        `service.submit()`/`service.status()`를 호출하고, 응답 body에 public
        transfer_id/transfer_state만 담아 돌려준다.
        """
        message = self._command(
            endpoint_id,
            "invoke_service_inline",
            {
                "scenario_id": scenario_id,
                "step_id": step_id,
                "service_operation": service_operation,
                "request": dict(request),
                "trigger_host_raw_ns": (
                    trigger_host_raw_ns
                    if trigger_host_raw_ns is not None
                    else int(self._host_raw_clock())
                ),
            },
        )
        response = self._channels[endpoint_id].request(message)
        body = response.get("body") or {}
        self.command_log.append(
            {
                "endpoint_id": endpoint_id,
                "command": "invoke_service_inline",
                "step_id": step_id,
                "service_operation": service_operation,
                "status": response.get("status"),
                "call_outcome": body.get("call_outcome"),
                "transfer_id": body.get("transfer_id"),
                "transfer_state": body.get("transfer_state"),
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

    # --- §16.2 PoC device 경계 (Stage 8 supervisor → launcher 소유 radio) ---------

    def poc_command(self, endpoint_id: str, command_name: str, body: dict) -> dict:
        """PoC device op 하나를 보내고 응답을 그대로 돌려준다.

        판정하지 않는다 — 호출자가 `status`/`call_outcome`/`reason`을 보고 결정한다. 거부를
        성공으로 바꾸거나 결과를 합성하지 않는다(장치 경계의 사실만 전달).
        """

        message = self._command(endpoint_id, command_name, dict(body))
        response = self._channels[endpoint_id].request(message)
        payload = response.get("body") or {}
        self.command_log.append(
            {
                "endpoint_id": endpoint_id,
                "command": command_name,
                "status": response.get("status"),
                "call_outcome": payload.get("call_outcome"),
                "reason": payload.get("reason"),
            }
        )
        return response

    @staticmethod
    def poc_result(response: dict):
        """PoC op 응답에서 result를 꺼낸다. 거부·오류면 `PocDeviceCallError`."""

        body = response.get("body") or {}
        if response.get("status") != "COMPLETED" or body.get("call_outcome") != "OK":
            raise PocDeviceCallError(
                f"{body.get('poc_operation')}: status={response.get('status')} "
                f"outcome={body.get('call_outcome')} reason={body.get('reason')}")
        return body.get("result")

    @staticmethod
    def fixture_bytes(value: dict) -> tuple:
        """(exact bytes, sha256) — source endpoint의 local fixture preload용."""

        data = encode_canonical_json(value)
        return data, hashlib.sha256(data).hexdigest()


__all__ = ("Stage7ScenarioController", "PocDeviceCallError")
