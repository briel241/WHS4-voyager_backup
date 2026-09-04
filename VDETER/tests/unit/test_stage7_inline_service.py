"""Stage 7 TEST adapter inline SUBMIT/STATUS 계약 시험 (`stage7` §16.2 최소 확장).

Phase B: MASS/ROC 업무 시나리오가 동적으로 만든 payload를 public
`SubmitTransferRequest`로 제출하고 public transfer ID/state를 controller에
돌려받는 경로를 잠근다. 기존 fixture 경로·replay·peer 검증은 함께 회귀한다.
"""

import base64
import hashlib
import unittest
import uuid

from vdeter.common.contracts.service import (
    TransferAccepted,
    TransferState,
    TransferStatusSnapshot,
)
from vdeter.common.identifiers import TransferId
from vdeter.common.results import Ok
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
from vdeter.stage7.testing.controller import Stage7ScenarioController

_PEER = (4242, 1000)
_TP = TimePoint(1, 0, ClockDomain.UTC, "test-utc")


class _RecordingService:
    """submit/status 요청을 기록하고 실제 public DTO로 응답하는 가짜 Service."""

    def __init__(self):
        self.submits = []
        self.status_requests = []
        self.transfer_id = TransferId(str(uuid.uuid4()))

    def submit(self, request):
        self.submits.append(request)
        return Ok(TransferAccepted(
            transfer_id=self.transfer_id, state=TransferState.QUEUED,
            accepted_at=_TP,
        ))

    def status(self, request):
        self.status_requests.append(request)
        return Ok(TransferStatusSnapshot(
            transfer_id=request.transfer_id, state=TransferState.COMPLETED,
            updated_at=_TP,
        ))

    def start(self, request):  # pragma: no cover - inline은 열지 않는 연산
        raise AssertionError("inline 경로에서 start가 불리면 안 된다")

    def stop(self, request):
        return self.status  # noqa: unused - fixture 시험은 별도

    def cancel(self, request):  # pragma: no cover
        raise AssertionError("inline 경로에서 cancel이 불리면 안 된다")


def _build(service=None):
    service = service or _RecordingService()
    adapter = Stage7ServiceApiTestAdapter(
        endpoint_id="MASS-A",
        service=service,
        resolved_test_plan_sha256="e" * 64,
        authorized_controller=_PEER,
    )

    class _Channel:
        def request(self, message):
            return adapter.handle_command(message, _PEER)

    controller = Stage7ScenarioController(
        run_id="stage7-inline-test",
        resolved_test_plan_sha256="e" * 64,
        adapter_channels={"MASS-A": _Channel()},
        host_raw_clock=lambda: 1_000_000_000,
    )
    return service, adapter, controller


def _submit_request(payload: bytes, **overrides) -> dict:
    request = {
        "destination": 111_111_111,
        "payload_format": "sc_vde.route_control.command",
        "payload_base64": base64.b64encode(payload).decode("ascii"),
        "ack_required": True,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    request.update(overrides)
    return request


class TC_STAGE7_INLINE_SUBMIT(unittest.TestCase):
    def test_inline_submit_returns_public_transfer_id(self):
        service, _adapter, controller = _build()
        payload = bytes(range(39))
        response = controller.invoke_service_inline(
            "MASS-A", scenario_id="s", step_id="submit-1",
            service_operation="SUBMIT", request=_submit_request(payload),
        )
        self.assertEqual(response["status"], "COMPLETED")
        body = response["body"]
        self.assertEqual(body["call_outcome"], "OK")
        # submit 성공 응답이 public transfer ID를 controller에 전달한다.
        self.assertEqual(body["transfer_id"], service.transfer_id.value)
        self.assertIsNone(body["transfer_state"])
        # adapter가 실제 service.submit()을 public DTO로 호출했다.
        self.assertEqual(len(service.submits), 1)
        request = service.submits[0]
        self.assertEqual(request.payload, payload)
        self.assertEqual(request.payload_format.value,
                         "sc_vde.route_control.command")
        self.assertEqual(request.destination.station_id.value, 111_111_111)
        self.assertTrue(request.ack_required)

    def test_inline_status_by_transfer_id(self):
        service, _adapter, controller = _build()
        transfer_id = str(uuid.uuid4())
        response = controller.invoke_service_inline(
            "MASS-A", scenario_id="s", step_id="status-1",
            service_operation="STATUS", request={"transfer_id": transfer_id},
        )
        body = response["body"]
        self.assertEqual(body["call_outcome"], "OK")
        self.assertEqual(body["transfer_id"], transfer_id)
        self.assertEqual(body["transfer_state"], "COMPLETED")
        self.assertEqual(len(service.status_requests), 1)
        self.assertEqual(service.status_requests[0].transfer_id.value, transfer_id)

    def test_payload_sha_mismatch_rejected_without_call(self):
        service, _adapter, controller = _build()
        payload = b"payload-bytes"
        response = controller.invoke_service_inline(
            "MASS-A", scenario_id="s", step_id="bad-sha",
            service_operation="SUBMIT",
            request=_submit_request(payload, payload_sha256="0" * 64),
        )
        self.assertEqual(response["status"], "REJECTED")
        self.assertEqual(response["body"]["call_outcome"], "NOT_CALLED")
        self.assertEqual(service.submits, [])

    def test_unknown_operation_and_extra_keys_rejected(self):
        service, _adapter, controller = _build()
        for request, operation in (
            (_submit_request(b"x", local_path="C:/x"), "SUBMIT"),   # 임의 경로 금지
            ({"transfer_id": str(uuid.uuid4()), "x": 1}, "STATUS"),  # 미지 키 금지
            (_submit_request(b"x"), "START"),                        # inline 미개방 연산
        ):
            response = controller.invoke_service_inline(
                "MASS-A", scenario_id="s", step_id="reject",
                service_operation=operation, request=request,
            )
            self.assertEqual(response["status"], "REJECTED", (operation, request))
        self.assertEqual(service.submits, [])
        self.assertEqual(service.status_requests, [])

    def test_broadcast_and_nonint_destination_rejected(self):
        # float 목적지는 canonical JSON 계층이 전송 자체를 거부하므로(§16.2
        # 전선 형식) adapter에 닿는 비정수 입력은 문자열/불리언/None뿐이다.
        service, _adapter, controller = _build()
        for destination in ("BROADCAST", True, None):
            response = controller.invoke_service_inline(
                "MASS-A", scenario_id="s", step_id="dest",
                service_operation="SUBMIT",
                request=_submit_request(b"x", destination=destination),
            )
            self.assertEqual(response["status"], "REJECTED", destination)
        self.assertEqual(service.submits, [])

    def test_wrong_peer_rejected_without_call(self):
        service, adapter, controller = _build()
        message = controller._command(
            "MASS-A", "invoke_service_inline",
            {"scenario_id": "s", "step_id": "peer", "service_operation": "SUBMIT",
             "request": _submit_request(b"x"), "trigger_host_raw_ns": 1},
        )
        response = adapter.handle_command(message, (9999, 1000))
        self.assertEqual(response["status"], "REJECTED")
        self.assertEqual(service.submits, [])

    def test_replay_returns_cached_response_without_second_call(self):
        service, adapter, controller = _build()
        message = controller._command(
            "MASS-A", "invoke_service_inline",
            {"scenario_id": "s", "step_id": "replay",
             "service_operation": "SUBMIT",
             "request": _submit_request(b"replay-bytes"),
             "trigger_host_raw_ns": 1},
        )
        first = adapter.handle_command(message, _PEER)
        second = adapter.handle_command(message, _PEER)
        self.assertEqual(first, second)
        self.assertEqual(len(service.submits), 1)

    def test_fixture_command_still_works_alongside_inline(self):
        """기존 fixture 경로 회귀 — inline 추가가 fixture 계약을 바꾸지 않는다."""
        service, adapter, controller = _build()
        fixture, digest = Stage7ScenarioController.fixture_bytes(
            {"transfer_id": str(uuid.uuid4())})
        adapter.register_fixture("fixtures/status.json", fixture)
        response = controller.invoke_service_fixture(
            "MASS-A", scenario_id="s", step_id="fix-status",
            service_operation="STATUS",
            local_fixture_relpath="fixtures/status.json",
            local_fixture_sha256=digest,
        )
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual(response["body"]["call_outcome"], "OK")
        # fixture 응답 형식은 기존 그대로다 (inline 전용 키를 강요하지 않는다).
        self.assertIn("public_result_artifact_sha256", response["body"])
        self.assertEqual(len(service.status_requests), 1)


if __name__ == "__main__":
    unittest.main()
