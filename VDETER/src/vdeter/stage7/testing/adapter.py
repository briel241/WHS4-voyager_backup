"""Stage 7 TEST-only Service API adapter (`stage7` §4.3, §16.2).

`Stage7ServiceApiTestAdapter`는 Scenario Controller의 immutable 기능 input을
process-local public `VdeService` 호출로 바꾸고 결과를 public
`ServiceResult`/`ServiceEvent`만으로 판정한다. Presentation/Link/MAC/PHY/Radio
port, repository, queue 또는 private Runtime method를 직접 호출·변조하지 않는다.
"""

from __future__ import annotations

import hashlib

from ...common.addressing import Addressed, Broadcast
from ...common.contracts.service import (
    CancelTransferRequest,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferStatusRequest,
)
from ...common.identifiers import PayloadFormat, StationId, TransferId
from ...common.serialization import to_reference_json
from ..canonical_json import encode_canonical_json, sha256_hex
from ..ipc import TEST_PROTOCOL, body_sha256_of

_SERVICE_OPERATIONS = ("START", "SUBMIT", "STATUS", "CANCEL", "STOP")

#: §16.2 최소 확장 — inline command는 동적 payload가 필요한 두 연산만 연다.
#: START/STOP/CANCEL은 기존 access-controlled fixture 경로를 그대로 쓴다.
_INLINE_OPERATIONS = ("SUBMIT", "STATUS")

#: inline request의 허용 키 (fail-closed: 그 외 키는 CONTROL_MESSAGE_INVALID).
#: 임의 파일 경로·임의 객체 직렬화는 받지 않는다 — canonical 값만.
_INLINE_SUBMIT_KEYS = frozenset(
    {"destination", "payload_format", "payload_base64", "ack_required",
     "payload_sha256"}
)
_INLINE_STATUS_KEYS = frozenset({"transfer_id"})


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _artifact_digest(dto) -> str:
    return sha256_hex(encode_canonical_json(_jsonable(to_reference_json(dto))))


class Stage7ServiceApiTestAdapter:
    """endpoint composition root에 TEST run에서만 주입되는 adapter."""

    __slots__ = (
        "endpoint_id",
        "_service",
        "_resolved_test_plan_sha256",
        "_authorized_controller",
        "_fixtures",
        "_fault_hooks",
        "_replay",
        "call_log",
        "artifacts",
    )

    def __init__(
        self,
        *,
        endpoint_id: str,
        service,
        resolved_test_plan_sha256: str,
        authorized_controller: tuple,
        fixtures: dict | None = None,
        fault_hooks: dict | None = None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self._service = service
        self._resolved_test_plan_sha256 = resolved_test_plan_sha256
        self._authorized_controller = tuple(authorized_controller)
        #: local access-controlled fixture: relpath → exact bytes (§16.2).
        self._fixtures = dict(fixtures or {})
        #: TC-STAGE7-012/014/016/017 signed allowlist의 fault hook.
        self._fault_hooks = dict(fault_hooks or {})
        self._replay = {}
        self.call_log = []
        self.artifacts = {}

    def register_fixture(self, relpath: str, data: bytes) -> None:
        self._fixtures[relpath] = data

    # --- §16.2 command 처리 ----------------------------------------------------

    def handle_command(self, message: dict, peer: tuple) -> dict:
        if tuple(peer) != self._authorized_controller:
            return self._reject(message, "CONTROL_PEER_INVALID")
        if (
            message.get("protocol") != TEST_PROTOCOL
            or message.get("kind") != "command"
            or message.get("resolved_test_plan_sha256") != self._resolved_test_plan_sha256
            or message.get("endpoint_id") != self.endpoint_id
        ):
            return self._reject(message, "CONTROL_MESSAGE_INVALID")
        command_id = message.get("command_id")
        exact = encode_canonical_json(_jsonable(message))
        cached = self._replay.get(command_id)
        if cached is not None:
            cached_bytes, cached_response = cached
            if cached_bytes == exact:
                return cached_response
            return self._reject(message, "CONTROL_MESSAGE_INVALID")
        name = message.get("command_name")
        if name == "invoke_service_fixture":
            response = self._invoke_service_fixture(message)
        elif name == "invoke_service_inline":
            response = self._invoke_service_inline(message)
        elif name == "inject_test_fault":
            response = self._inject_test_fault(message)
        elif name == "test_status":
            response = self._test_status(message)
        else:
            response = self._reject(message, "CONTROL_MESSAGE_INVALID")
        self._replay[command_id] = (exact, response)
        return response

    def _response(self, message, status, body):
        response = dict(message)
        response["kind"] = "response"
        response["status"] = status
        response["request_body_sha256"] = message.get("body_sha256")
        response["body"] = body
        response["body_sha256"] = body_sha256_of(body)
        return response

    def _reject(self, message, code):
        return self._response(
            message,
            "REJECTED",
            {
                "service_operation": (message.get("body") or {}).get("service_operation"),
                "call_outcome": "NOT_CALLED",
                "public_result_artifact_sha256": None,
                "public_error_code": None,
            },
        )

    def _invoke_service_fixture(self, message) -> dict:
        body = message.get("body") or {}
        operation = body.get("service_operation")
        if operation not in _SERVICE_OPERATIONS:
            return self._reject(message, "CONTROL_MESSAGE_INVALID")
        relpath = body.get("local_fixture_relpath")
        fixture = self._fixtures.get(relpath)
        if fixture is None or hashlib.sha256(fixture).hexdigest() != body.get(
            "local_fixture_sha256"
        ):
            return self._reject(message, "CONTROL_MESSAGE_INVALID")
        request = self._build_request(operation, fixture)
        if request is None:
            return self._reject(message, "CONTROL_MESSAGE_INVALID")
        method = {
            "START": self._service.start,
            "SUBMIT": self._service.submit,
            "STATUS": self._service.status,
            "CANCEL": self._service.cancel,
            "STOP": self._service.stop,
        }[operation]
        self.call_log.append((operation, relpath))
        result = method(request)
        if result.is_ok:
            digest = _artifact_digest(result.value)
            self.artifacts[digest] = result.value
            response_body = {
                "service_operation": operation,
                "call_outcome": "OK",
                "public_result_artifact_sha256": digest,
                "public_error_code": None,
            }
        else:
            import sys as _sys
            print(f"[test-adapter] {operation} ERR: {str(result.error)[:300]}",
                  file=_sys.stderr, flush=True)
            digest = _artifact_digest(result.error)
            self.artifacts[digest] = result.error
            response_body = {
                "service_operation": operation,
                "call_outcome": "ERR",
                "public_result_artifact_sha256": digest,
                "public_error_code": result.error.code,
            }
        return self._response(message, "COMPLETED", response_body)

    def _build_request(self, operation: str, fixture: bytes):
        """local fixture bytes(canonical JSON)를 공개 request DTO로 만든다."""

        try:
            from ..canonical_json import decode_canonical_json

            value = decode_canonical_json(fixture)
        except Exception:  # noqa: BLE001
            return None
        try:
            if operation == "START":
                return ServiceStartRequest(
                    local_station_id=StationId(value["local_station_id"]),
                    role=__import__(
                        "vdeter.common.contracts.service", fromlist=["ServiceRole"]
                    ).ServiceRole(value["role"]),
                    config_ref=value.get("config_ref", ""),
                )
            if operation == "SUBMIT":
                destination = (
                    Broadcast()
                    if value["destination"] == "BROADCAST"
                    else Addressed(StationId(value["destination"]))
                )
                import base64

                return SubmitTransferRequest(
                    destination=destination,
                    payload_format=PayloadFormat(value["payload_format"]),
                    payload=base64.b64decode(value["payload_base64"]),
                    ack_required=bool(value["ack_required"]),
                )
            if operation == "STATUS":
                return TransferStatusRequest(transfer_id=TransferId(value["transfer_id"]))
            if operation == "CANCEL":
                return CancelTransferRequest(transfer_id=TransferId(value["transfer_id"]))
            if operation == "STOP":
                return ServiceStopRequest(reason=value.get("reason", ""))
        except Exception:  # noqa: BLE001
            return None
        return None

    # --- §16.2 최소 확장: inline Service request (동적 MASS/ROC payload) -------
    #
    # fixture 경로는 시작 시 적재된 bytes만 호출할 수 있어, MASS가 런타임에
    # 만든 application ACK를 제출할 수 없고 submit이 발급한 public transfer
    # ID도 controller에 돌아가지 않았다. inline 경로는 canonical 값만 담은
    # request로 public `SubmitTransferRequest`/`TransferStatusRequest`를
    # 만들어 실제 `service.submit()`/`service.status()`를 호출하고, public
    # ID/state만 응답에 담는다. private repository/queue/MAC/PHY는 만지지
    # 않으며 private/internal identifier는 노출하지 않는다.

    def _reject_inline(self, message):
        return self._response(
            message,
            "REJECTED",
            {
                "service_operation": (message.get("body") or {}).get("service_operation"),
                "call_outcome": "NOT_CALLED",
                "public_result_artifact_sha256": None,
                "public_error_code": None,
                "transfer_id": None,
                "transfer_state": None,
            },
        )

    def _invoke_service_inline(self, message) -> dict:
        body = message.get("body") or {}
        operation = body.get("service_operation")
        if operation not in _INLINE_OPERATIONS:
            return self._reject_inline(message)
        value = body.get("request")
        if not isinstance(value, dict):
            return self._reject_inline(message)
        request = self._build_inline_request(operation, value)
        if request is None:
            return self._reject_inline(message)
        method = {"SUBMIT": self._service.submit, "STATUS": self._service.status}[
            operation
        ]
        self.call_log.append((f"{operation}_INLINE", body.get("step_id")))
        result = method(request)
        response_body = {
            "service_operation": operation,
            "transfer_id": None,
            "transfer_state": None,
        }
        if result.is_ok:
            digest = _artifact_digest(result.value)
            self.artifacts[digest] = result.value
            response_body.update(
                {
                    "call_outcome": "OK",
                    "public_result_artifact_sha256": digest,
                    "public_error_code": None,
                }
            )
            if operation == "SUBMIT":
                response_body["transfer_id"] = result.value.transfer_id.value
            else:  # STATUS
                response_body["transfer_id"] = result.value.transfer_id.value
                response_body["transfer_state"] = result.value.state.value
        else:
            digest = _artifact_digest(result.error)
            self.artifacts[digest] = result.error
            response_body.update(
                {
                    "call_outcome": "ERR",
                    "public_result_artifact_sha256": digest,
                    "public_error_code": result.error.code,
                }
            )
        return self._response(message, "COMPLETED", response_body)

    def _build_inline_request(self, operation: str, value: dict):
        """canonical inline 값으로 공개 request DTO를 만든다. 위반이면 None."""

        import base64

        try:
            if operation == "SUBMIT":
                if set(value) != _INLINE_SUBMIT_KEYS:
                    return None
                destination = value["destination"]
                # SC-VDE 동적 제출은 addressed 전용이다 — broadcast/문자열 금지.
                if isinstance(destination, bool) or not isinstance(destination, int):
                    return None
                encoded = value["payload_base64"]
                if not isinstance(encoded, str):
                    return None
                payload = base64.b64decode(encoded.encode("ascii"), validate=True)
                digest = value["payload_sha256"]
                if (
                    not isinstance(digest, str)
                    or hashlib.sha256(payload).hexdigest() != digest
                ):
                    return None
                if not isinstance(value["ack_required"], bool):
                    return None
                return SubmitTransferRequest(
                    destination=Addressed(StationId(destination)),
                    payload_format=PayloadFormat(value["payload_format"]),
                    payload=payload,
                    ack_required=value["ack_required"],
                )
            if operation == "STATUS":
                if set(value) != _INLINE_STATUS_KEYS:
                    return None
                return TransferStatusRequest(
                    transfer_id=TransferId(value["transfer_id"])
                )
        except Exception:  # noqa: BLE001
            return None
        return None

    def _inject_test_fault(self, message) -> dict:
        body = message.get("body") or {}
        fault_id = body.get("fault_fixture_id")
        hook = self._fault_hooks.get(fault_id)
        if hook is None:
            return self._response(
                message,
                "REJECTED",
                {
                    "fault_fixture_id": fault_id,
                    "call_outcome": "NOT_CALLED",
                    "fault_artifact_sha256": None,
                    "private_code": "FAULT_NOT_ALLOWLISTED",
                },
            )
        try:
            artifact = hook(body)
        except Exception as error:  # noqa: BLE001
            return self._response(
                message,
                "COMPLETED",
                {
                    "fault_fixture_id": fault_id,
                    "call_outcome": "ERR",
                    "fault_artifact_sha256": None,
                    "private_code": type(error).__name__[:64],
                },
            )
        digest = None
        if artifact is not None:
            digest = sha256_hex(encode_canonical_json(_jsonable(artifact)))
        return self._response(
            message,
            "COMPLETED",
            {
                "fault_fixture_id": fault_id,
                "call_outcome": "OK",
                "fault_artifact_sha256": digest,
                "private_code": None,
            },
        )

    def _test_status(self, message) -> dict:
        query = (message.get("body") or {}).get("query_command_id")
        if query is None:
            body = {
                "query_command_id": None,
                "lookup_state": "UNKNOWN",
                "terminal_status": None,
                "terminal_response_sha256": None,
                "private_code": None,
            }
            return self._response(message, "COMPLETED", body)
        cached = self._replay.get(query)
        if cached is None:
            body = {
                "query_command_id": query,
                "lookup_state": "UNKNOWN",
                "terminal_status": None,
                "terminal_response_sha256": None,
                "private_code": None,
            }
        else:
            _bytes, response = cached
            body = {
                "query_command_id": query,
                "lookup_state": "TERMINAL",
                "terminal_status": response.get("status"),
                "terminal_response_sha256": response.get("body_sha256"),
                "private_code": None,
            }
        return self._response(message, "COMPLETED", body)


__all__ = ("Stage7ServiceApiTestAdapter",)
