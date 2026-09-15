"""Stage 7 endpoint-side Runtime IPC client (`stage7` §14.2, §16, §16.1).

Runtime Coordinator command를 수신해 endpoint/adapter 동작으로 변환하고
response/event를 canonical 규칙으로 반환한다. 검증 우선순위는
peer/socket→JSON/framing→run/plan/current generation→deadline→replay다.

hardware 동작(CFO probe, gain calibration, RF timestamp 측정)은 주입된
`operations` 객체가 수행한다. software 시험은 fake operations를, Pi runtime은
실제 구현을 주입한다.
"""

from __future__ import annotations

import uuid as _uuid

from .canonical_json import encode_canonical_json, sha256_hex
from .errors import Stage7PrivateErrorCode
from .ipc import (
    ACTIVE_BRIDGE_COMMANDS,
    RUNTIME_PROTOCOL,
    Stage7IpcError,
    body_sha256_of,
    validate_runtime_command,
)

#: §14.2 정상 command 순서가 유도하는 state 전이 (COMMIT 성공 시).
_COMMIT_TRANSITIONS = {
    "prepare": "PREFLIGHT",
    "configure": "RX_READY",
    "arm_rx": "RX_READY",
    "start_cfo": "CFO_VALIDATED",
    "acquire_host_time": "PROVISIONAL_HOST_BRIDGE",
    "prepare_active_bridge": "ACTIVE_BRIDGE_PREPARED",
    "commit_active_bridge": "ACTIVE_BRIDGE_COMMITTED",
    "open_service_admission": "SERVICE_ADMISSION_READY",
    "commit_service_start": "SERVICE_START_STAGED",
    "publish_service_start": "RUNNING",
    "resume_runtime": "RUNNING",
    "stop": "STOPPED",
}


class Stage7EndpointRuntimeClient:
    """한 endpoint process의 Runtime IPC handler."""

    __slots__ = (
        "endpoint_id",
        "run_id",
        "plan_sha256",
        "lifecycle_generation",
        "bridge_generation",
        "state",
        "_endpoint",
        "_operations",
        "_event_sender",
        "_replay",
        "_outcomes",
        "_inhibited",
        "_host_raw_clock",
        "_stale_rejections",
    )

    def __init__(
        self,
        *,
        endpoint_id: str,
        run_id: str,
        plan_sha256: str,
        lifecycle_generation: int,
        endpoint,
        operations,
        event_sender,
        host_raw_clock,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.run_id = run_id
        self.plan_sha256 = plan_sha256
        self.lifecycle_generation = lifecycle_generation
        self.bridge_generation = None
        self.state = "CONFIGURED"
        self._endpoint = endpoint
        self._operations = operations
        self._event_sender = event_sender
        self._replay = {}
        self._outcomes = {}
        self._inhibited = False
        self._host_raw_clock = host_raw_clock
        self._stale_rejections = []
        if endpoint is not None:
            endpoint.set_lifecycle_generation(lifecycle_generation)

    # --- 응답 조립 ----------------------------------------------------------------

    def _response(self, command: dict, status: str, *, private_code=None, result=None):
        body = {
            "status": status,
            "endpoint_state": self.state,
            "lifecycle_generation": self.lifecycle_generation,
            "bridge_generation": self.bridge_generation,
            "private_code": None if private_code is None else private_code.value,
            "result_sha256": None if result is None else body_sha256_of(result),
            "result": result,
        }
        message = dict(command)
        message["kind"] = "response"
        message["request_body_sha256"] = command["body_sha256"]
        message["body"] = body
        message["body_sha256"] = body_sha256_of(body)
        return message

    def _reject(self, command, *, private_code=Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID):
        return self._response(command, "REJECTED", private_code=private_code)

    def stale_rejections(self) -> tuple:
        return tuple(self._stale_rejections)

    # --- source-less event 전송 (§16.1) --------------------------------------------

    def _send_event(self, *, endpoint_state, artifact_sha256, event_type="STATE_CHANGED"):
        body = {
            "event_type": event_type,
            "endpoint_state": endpoint_state,
            "lifecycle_generation": self.lifecycle_generation,
            "bridge_generation": self.bridge_generation,
            "terminal_command_id": None,
            "terminal_status": None,
            "artifact_sha256": artifact_sha256,
            "private_code": None,
        }
        message = {
            "protocol": RUNTIME_PROTOCOL,
            "kind": "event",
            "command_name": None,
            "run_id": self.run_id,
            "resolved_system_plan_sha256": self.plan_sha256,
            "command_id": None,
            "endpoint_id": self.endpoint_id,
            "expected_lifecycle_generation": None,
            "expected_bridge_generation": None,
            "barrier_id": None,
            "barrier_phase": None,
            "barrier_manifest_sha256": None,
            "issued_host_raw_ns": int(self._host_raw_clock()),
            "deadline_host_raw_ns": None,
            "request_body_sha256": None,
            "body_sha256": body_sha256_of(body),
            "body": body,
        }
        return bool(self._event_sender(message))

    def make_intent_notifier(self):
        """endpoint `start()` gate에 주입하는 source-less intent notifier."""

        def notifier(event_fields: dict) -> bool:
            return self._send_event(
                endpoint_state="SERVICE_START_PENDING",
                artifact_sha256=event_fields["artifact_sha256"],
            )

        return notifier

    def emit_tbb_active(self, record) -> bool:
        """staged frozen MAC readiness의 source-less TBB_ACTIVE event (§16.1)."""

        self.state = "TBB_ACTIVE"
        return self._send_event(
            endpoint_state="TBB_ACTIVE", artifact_sha256=record.digest()
        )

    # --- command 처리 --------------------------------------------------------------

    def handle_command(self, message: dict) -> dict:
        # 1. JSON/schema.
        try:
            validate_runtime_command(message)
        except Stage7IpcError:
            return self._reject_shell(message)
        # 2. run/plan/current generation.
        if (
            message["run_id"] != self.run_id
            or message["resolved_system_plan_sha256"] != self.plan_sha256
        ):
            return self._reject(message)
        if message["expected_lifecycle_generation"] != self.lifecycle_generation:
            self._stale_rejections.append(message["command_id"])
            return self._reject(message)
        if message["expected_bridge_generation"] != self.bridge_generation:
            # 최초 active bridge commit 전 null, 이후 current u31 (§16.1).
            command_name = message["command_name"]
            if not (
                command_name == "prepare_active_bridge"
                and self.bridge_generation is None
                and message["expected_bridge_generation"] is None
            ):
                self._stale_rejections.append(message["command_id"])
                return self._reject(message)
        # 3. replay (deadline보다 먼저 exact replay lookup — 단, 처음 보는 expired
        #    command는 거부한다).
        command_id = message["command_id"]
        exact = encode_canonical_json(message)
        cached = self._replay.get(command_id)
        if cached is not None:
            cached_bytes, cached_response = cached
            if cached_bytes == exact:
                return cached_response
            return self._reject(message)
        # 4. deadline.
        now = int(self._host_raw_clock())
        if now >= message["deadline_host_raw_ns"]:
            response = self._response(message, "TIMED_OUT")
            self._replay[command_id] = (exact, response)
            return response
        if self._inhibited and message["command_name"] not in ("status", "emergency_inhibit", "stop"):
            response = self._reject(
                message, private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED
            )
            self._replay[command_id] = (exact, response)
            return response
        handler = getattr(self, f"_handle_{message['command_name']}", None)
        if handler is None:  # pragma: no cover - enum이 이미 막는다
            response = self._reject(message)
        else:
            try:
                response = handler(message)
            except Exception:  # noqa: BLE001 - handler 예외는 REJECTED로 구조화
                # traceback을 evidence에 보존한다 — 이 catch는 지금까지 여러
                # run의 근본 원인을 status만 남기고 은폐해 왔다 (rr-p1/p2 실측
                # 반복 함정). 판정 semantics는 불변(REJECTED 그대로).
                try:
                    import traceback as _tb

                    self._operations.evidence.append(
                        {"kind": "handler_exception",
                         "command_name": message.get("command_name"),
                         "barrier_phase": message.get("barrier_phase"),
                         "traceback": _tb.format_exc()[-2000:]}
                    )
                except Exception:  # noqa: BLE001
                    pass
                response = self._reject(
                    message, private_code=Stage7PrivateErrorCode.CONTROL_BARRIER_FAILED
                )
        self._replay[command_id] = (exact, response)
        self._record_outcome(message, response)
        return response

    def _reject_shell(self, message: dict) -> dict:
        """schema 위반 message에도 §16 응답 형태를 유지한다 (peer 판단용)."""

        shell = {
            "protocol": RUNTIME_PROTOCOL,
            "kind": "response",
            "command_name": message.get("command_name"),
            "run_id": message.get("run_id", self.run_id),
            "resolved_system_plan_sha256": message.get(
                "resolved_system_plan_sha256", self.plan_sha256
            ),
            "command_id": message.get("command_id", str(_uuid.uuid4())),
            "endpoint_id": self.endpoint_id,
            "expected_lifecycle_generation": message.get("expected_lifecycle_generation"),
            "expected_bridge_generation": message.get("expected_bridge_generation"),
            "barrier_id": message.get("barrier_id"),
            "barrier_phase": message.get("barrier_phase"),
            "barrier_manifest_sha256": message.get("barrier_manifest_sha256"),
            "issued_host_raw_ns": message.get("issued_host_raw_ns", 0),
            "deadline_host_raw_ns": message.get("deadline_host_raw_ns", 1),
            "request_body_sha256": message.get("body_sha256", "0" * 64),
            "body_sha256": None,
            "body": None,
        }
        body = {
            "status": "REJECTED",
            "endpoint_state": self.state,
            "lifecycle_generation": self.lifecycle_generation,
            "bridge_generation": self.bridge_generation,
            "private_code": Stage7PrivateErrorCode.CONTROL_MESSAGE_INVALID.value,
            "result_sha256": None,
            "result": None,
        }
        shell["body"] = body
        shell["body_sha256"] = body_sha256_of(body)
        return shell

    def _record_outcome(self, command: dict, response: dict) -> None:
        status = response["body"]["status"]
        entry = {
            "result_type": "STAGE7_COMMAND_STATUS_V1",
            "query_command_id": command["command_id"],
            "lookup_state": "ACCEPTED_PENDING" if status == "ACCEPTED" else "TERMINAL",
            "command_lifecycle_generation": self.lifecycle_generation,
            "command_bridge_generation": self.bridge_generation,
            "terminal_status": None if status == "ACCEPTED" else status,
            "terminal_artifact_sha256": None,
            "private_code": response["body"]["private_code"],
        }
        self._outcomes[command["command_id"]] = entry

    def promote_outcome_terminal(self, command_id: str, terminal_status: str, artifact_sha256=None):
        entry = self._outcomes.get(command_id)
        if entry is not None and entry["lookup_state"] == "ACCEPTED_PENDING":
            entry["lookup_state"] = "TERMINAL"
            entry["terminal_status"] = terminal_status
            entry["terminal_artifact_sha256"] = artifact_sha256

    # --- handlers ------------------------------------------------------------------

    def _handle_prepare(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        body = message["body"]
        operations = self._operations
        if phase == "PREPARE":
            checked = operations.check_prepare(body)
            if not checked:
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self.state = _COMMIT_TRANSITIONS["prepare"]
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")  # ABORT

    def _handle_configure(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            if not self._operations.check_configure(message["body"]):
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            done = self._operations.do_configure(message["body"])
            if not done:
                return self._reject(message)
            self.state = "RX_READY"
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_arm_rx(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self.state = "RX_READY"
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_start_cfo(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self.state = "CFO_PROBE_ONLY"
            outcome = self._operations.run_cfo(message["body"])
            if outcome is None:
                return self._reject(message)
            self.state = "CFO_VALIDATED" if outcome else "TX_INHIBITED"
            if not outcome:
                self._inhibited = True
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_acquire_host_time(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self.state = "HOST_TIME_ACQUIRING"
            summary = self._operations.acquire_host_time(message["body"])
            if summary is None:
                return self._response(message, "TIMED_OUT")
            if summary.get("outcome") == "PASS":
                self.state = "PROVISIONAL_HOST_BRIDGE"
            return self._response(message, "COMPLETED", result=summary)
        return self._response(message, "COMPLETED")

    def _handle_calibrate_rf(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            if not self._operations.check_calibrate(message["body"]):
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            mode = message["body"]["calibration_mode"]
            searching = mode in ("SEARCH_AND_SEAL", "ADAPTIVE_SEARCH_AND_SEAL")
            self.state = (
                "RF_GAIN_SEARCHING" if searching else "RF_GAIN_REVALIDATING"
            )
            outcome = self._operations.run_calibrate(message["body"])
            if isinstance(outcome, dict):
                passed, result = bool(outcome.get("passed")), outcome
            else:
                # 시험 fake 하위호환: bool 반환.
                passed, result = bool(outcome), None
            if not passed:
                self._inhibited = True
                self.state = "TX_INHIBITED"
                return self._reject(
                    message,
                    private_code=Stage7PrivateErrorCode.RF_GAIN_CALIBRATION_FAILED,
                )
            self.state = (
                "RF_GAIN_SEALED" if searching else "RF_GAIN_REVALIDATED"
            )
            return self._response(message, "COMPLETED", result=result)
        return self._response(message, "COMPLETED")

    def _handle_validate_rf_time(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            body = message["body"]
            self.state = (
                "RF_TIME_PRE_VALIDATING"
                if body["validation_phase"] == "PRE_CORRECTION"
                else "RF_TIME_POST_VALIDATING"
            )
            summaries = self._operations.validate_rf_time(body)
            if summaries is None:
                return self._response(message, "TIMED_OUT")
            if body["validation_phase"] == "POST_CORRECTION" and all(
                item.get("outcome") == "PASS" for item in summaries
            ):
                self.state = "HOST_TIME_LOCKED"
            return self._response(message, "COMPLETED", result=summaries)
        return self._response(message, "COMPLETED")

    def _handle_prepare_active_bridge(self, message: dict) -> dict:
        body = message["body"]
        if not self._operations.check_active_candidate(body):
            return self._reject(message)
        self.state = "ACTIVE_BRIDGE_PREPARED"
        return self._response(message, "PREPARED")

    def _handle_commit_active_bridge(self, message: dict) -> dict:
        body = message["body"]
        installed = self._operations.install_active_bridge(body)
        if not installed:
            return self._reject(message)
        self.bridge_generation = body["bridge_generation"]
        if self._endpoint is not None:
            self._endpoint.set_bridge_generation(self.bridge_generation)
        self.state = "ACTIVE_BRIDGE_COMMITTED"
        return self._response(message, "COMPLETED")

    def _handle_abort_active_bridge(self, message: dict) -> dict:
        self._operations.abort_active_bridge(message["body"])
        self.state = "PROVISIONAL_HOST_BRIDGE"
        return self._response(message, "COMPLETED")

    def _handle_open_service_admission(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        body = message["body"]
        if phase == "PREPARE":
            allowed = ("ARMED", "HOST_TIME_LOCKED")
            # 디버그 전용(기본 OFF): svcdbg 하네스가 RF ladder 없이 서비스
            # 구간만 반복 계측할 때 RX_READY 입장을 허용한다. 정식 RR run은
            # 이 env를 설정하지 않으므로 §16.1 semantics 불변.
            import os as _os
            if _os.environ.get("STAGE7_SVCDBG_ALLOW_RX_READY") == "1":
                allowed = allowed + ("RX_READY", "ACTIVE_BRIDGE_COMMITTED")
            if self.state not in allowed:
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            if self._endpoint is not None:
                self._endpoint.install_service_admission(
                    intent_window_id=body["intent_window_id"],
                    service_start_deadline_host_raw_ns=body[
                        "service_start_deadline_host_raw_ns"
                    ],
                    tbb_publish_deadline_host_raw_ns=body[
                        "tbb_publish_deadline_host_raw_ns"
                    ],
                )
            self.state = "SERVICE_ADMISSION_READY"
            return self._response(message, "COMPLETED")
        if self._endpoint is not None:
            self._endpoint.discard_service_admission()
        return self._response(message, "COMPLETED")

    def _handle_commit_service_start(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        body = message["body"]
        entries = {entry[0]: entry[1] for entry in body["intent_set"]}
        my_digest = entries.get(self.endpoint_id)
        if phase == "PREPARE":
            def _why(reason, **extra):
                # 거부 사유를 evidence로 남긴다 — PREPARE 거부는 status만
                # 전달되어 원인이 은폐된다 (rr-p2-17 run f 실측 함정).
                try:
                    self._operations.evidence.append(
                        {"kind": "service_start_prepare_rejected",
                         "reason": reason, **extra}
                    )
                except Exception:  # noqa: BLE001
                    pass
                return self._reject(message)

            if my_digest is None or self._endpoint is None:
                return _why("intent_set에 자기 항목 없음",
                            has_endpoint=self._endpoint is not None)
            local = self._endpoint.pending_intent_digest()
            if local != my_digest:
                # authoritative 검증: set의 자기 digest ↔ current local record (§16.1).
                return _why("digest 불일치", local=str(local),
                            expected=str(my_digest))
            reserved = self._endpoint.prepare_service_start(my_digest)
            if reserved.is_err:
                return _why("prepare_service_start 실패",
                            error=str(reserved.error)[:400])
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            staged = self._endpoint.commit_service_start_staged()
            if staged.is_err:
                try:
                    self._operations.evidence.append(
                        {"kind": "service_start_commit_rejected",
                         "error": str(staged.error)[:800]}
                    )
                except Exception:  # noqa: BLE001
                    pass
                return self._reject(message)
            value = staged.value
            summary = {
                "summary_type": "STAGE7_STAGED_SERVICE_START_V1",
                "endpoint_id": self.endpoint_id,
                "lifecycle_generation": self.lifecycle_generation,
                "radio_generation": 1,
                "staged_service_started_sha256": value["staged_service_started_sha256"],
                "staged_public_event_set_sha256": value["staged_public_event_set_sha256"],
                "outcome": "PASS",
            }
            self.state = "SERVICE_START_STAGED"
            return self._response(message, "COMPLETED", result=summary)
        self._endpoint.abort_service_start()
        return self._response(message, "COMPLETED")

    def _handle_publish_service_start(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            if self.state not in ("SERVICE_START_STAGED", "TBB_ACTIVE"):
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            published = self._endpoint.publish_service_start_commit()
            if published.is_err:
                return self._reject(message)
            self.state = "RUNNING"
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_resume_runtime(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            if self.state != "TBB_ACTIVE":
                return self._reject(message)
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self.state = "RUNNING"
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_status(self, message: dict) -> dict:
        query = message["body"]["query_command_id"]
        if query is None:
            return self._response(message, "COMPLETED")
        entry = self._outcomes.get(query)
        if entry is None:
            result = {
                "result_type": "STAGE7_COMMAND_STATUS_V1",
                "query_command_id": query,
                "lookup_state": "UNKNOWN",
                "command_lifecycle_generation": None,
                "command_bridge_generation": None,
                "terminal_status": None,
                "terminal_artifact_sha256": None,
                "private_code": None,
            }
        else:
            result = dict(entry)
        return self._response(message, "COMPLETED", result=result)

    def _handle_stop(self, message: dict) -> dict:
        phase = message["barrier_phase"]
        if phase == "PREPARE":
            return self._response(message, "PREPARED")
        if phase == "COMMIT":
            self._operations.do_stop(message["body"])
            self.state = "STOPPED"
            return self._response(message, "COMPLETED")
        return self._response(message, "COMPLETED")

    def _handle_emergency_inhibit(self, message: dict) -> dict:
        self._inhibited = True
        self.state = "TX_INHIBITED"
        self._operations.do_inhibit(message["body"])
        if self._endpoint is not None:
            from ..common.errors import ServiceError, ServiceErrorCategory
            from ..runtime.projection import PublicErrorCode

            self._endpoint.fail_pending_start(
                ServiceError(
                    code=PublicErrorCode.DEVICE_ERROR,
                    category=ServiceErrorCategory.DEVICE,
                    message="group emergency inhibit",
                    retryable=True,
                ),
                inhibit=True,
            )
        return self._response(message, "COMPLETED")


__all__ = ("Stage7EndpointRuntimeClient",)
