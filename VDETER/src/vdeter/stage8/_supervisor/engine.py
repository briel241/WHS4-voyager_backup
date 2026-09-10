"""supervisor(server) 측 순수 state machine(명세 §7~§15).

I/O·clock·socket·process를 갖지 않는다. `receive(parsed, now)`·`enqueue_event(...)`·
`drain(now)`·`tick(now)`가 보낼 message envelope 목록을 돌려준다. `PocSupervisor`가
이 engine을 실제 transport·clock·spawner로 구동한다. 이 분리로 deadline·replay·멱등성·
overflow를 fake clock·fake provider로 sleep 없이 검증한다(§18).

모든 오류는 삼키지 않고 §14 고정 code·severity의 :class:`PocError`로 유계 변환한다.
"""

from __future__ import annotations

import uuid
from enum import Enum

from ..poc_api import protocol
from ..poc_api.codec import canonical_sha256, encode_canonical
from ..poc_api.errors import PocError, PocErrorCode
from ..poc_api.limits import (
    ARMED_DEADLINE_S,
    CALLBACK_ON_EVENT_DEADLINE_S,
    ACK_MAX_DELAY_S,
    HELLO_DEADLINE_S,
    MAX_ACTION_DESCRIPTOR_BYTES,
    MAX_EVENT_QUEUE_BYTES,
    MAX_EVENT_QUEUE_MESSAGES,
    MAX_INFLIGHT_ACTIONS,
    MAX_REPLAY_RING_BYTES,
    MAX_REPLAY_RING_MESSAGES,
    MAX_WORKLOAD_ACTION_REQUESTS,
)
from ..poc_api.messages import build_message, require_body_field
from ..poc_api.negotiation import HelloRequest, negotiate, restore_matches
from ..poc_api.types import (
    FORBIDDEN_ACTION_FIELDS,
    ActionKind,
    TuningState,
)
from .token import constant_time_equals

#: server가 delivered event의 callback 완료(ACK)를 기다리는 유예(§8.2, §11).
#: on_event deadline + ACK one-shot delay. 초과하면 callback hang으로 간주한다.
SERVER_EVENT_ACK_GRACE_S = CALLBACK_ON_EVENT_DEADLINE_S + ACK_MAX_DELAY_S

#: delivery_matrix evidence 항목 상한(bounded trace, §3.3/§16). NOT_TUNED counter는 무제한
#: int로 유지하되 상세 항목 list는 이 값에서 멈추고 drop 수만 센다.
MAX_DELIVERY_MATRIX_ENTRIES = MAX_REPLAY_RING_MESSAGES


class EngineState(str, Enum):
    SETUP = "SETUP"
    AWAIT_HELLO = "AWAIT_HELLO"
    AWAIT_ARMED = "AWAIT_ARMED"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    FINISHING = "FINISHING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


class _QueuedEvent:
    __slots__ = ("event_type", "body", "size", "sequence", "event_id", "payload_digest")

    def __init__(self, event_type: str, body: dict, size: int) -> None:
        self.event_type = event_type
        self.body = body
        self.size = size
        self.sequence: int | None = None
        self.event_id: str | None = None
        self.payload_digest: str | None = None


class _CachedAction:
    __slots__ = ("digest", "result_body", "terminal_body", "accepted")

    def __init__(self, digest: str, result_body: dict, accepted: bool) -> None:
        self.digest = digest
        self.result_body = result_body
        self.terminal_body: dict | None = None
        self.accepted = accepted


class SupervisorEngine:
    def __init__(
        self,
        *,
        session_id: str,
        workload_id: str,
        run_id: str,
        scenario_id: str,
        expected_token: bytes,
        manifest,
        server_config: ServerCapabilityConfig,
        provider,
    ) -> None:
        self.session_id = session_id
        self.workload_id = workload_id
        self.run_id = run_id
        self.scenario_id = scenario_id
        self._expected_token = bytes(expected_token)
        self._manifest = manifest
        self._server_config = server_config
        self._provider = provider

        self.state = EngineState.SETUP
        self.negotiation = None
        self.connection_epoch = 0
        self.fatal_error: PocError | None = None

        # deadline(초, monotonic) — None이면 비활성.
        self._hello_deadline: float | None = None
        self._armed_deadline: float | None = None
        self._oldest_unacked_sent_at: float | None = None

        # event pipeline
        self._queue: list[_QueuedEvent] = []
        self._queue_bytes = 0
        self._ring: list[_QueuedEvent] = []  # sent-but-unacked
        self._ring_bytes = 0
        self._next_sequence = 1
        self.last_acked_sequence = 0

        # action 멱등성·상한
        self._cache: dict[str, _CachedAction] = {}
        self._inflight: set[str] = set()

        # evidence counter
        self.evidence = {
            "not_tuned_count": 0,
            "tuned_observer_count": 0,
            "events_emitted": 0,
            "events_replayed": 0,
            "event_queue_high_water": 0,
            "replay_ring_high_water": 0,
            "provider_submit_calls": 0,
            "actions_accepted": 0,
            "actions_rejected": 0,
            "actions_cached_hits": 0,
            "reconnects": 0,
            "delivery_matrix": [],
            "delivery_matrix_dropped": 0,
            "delivery_matrix_truncated": False,
        }

    # --- 메시지 helper ------------------------------------------------------
    def _msg(self, message_type: str, body: dict) -> dict:
        minor = self.negotiation.protocol_minor if self.negotiation else protocol.SERVER_MIN_MINOR
        return build_message(
            message_type,
            session_id=self.session_id,
            workload_id=self.workload_id,
            connection_epoch=self.connection_epoch,
            protocol_minor=minor,
            body=body,
        )

    def _fail(self, error: PocError) -> list:
        self.fatal_error = error
        self.state = EngineState.ABORTED
        return [self._msg(protocol.ERROR, error.as_json_value())]

    # --- handshake ----------------------------------------------------------
    def start_handshake(self, now: float) -> None:
        self.state = EngineState.AWAIT_HELLO
        self._hello_deadline = now + HELLO_DEADLINE_S

    def receive(self, parsed, now: float) -> list:
        if self.state in (EngineState.CLOSED, EngineState.ABORTED):
            return []
        if parsed.type == protocol.HELLO:
            return self._on_hello(parsed, now)
        if parsed.type == protocol.ARMED:
            return self._on_armed(parsed, now)
        if parsed.type == protocol.EVENT_ACK:
            return self._on_event_ack(parsed, now)
        if parsed.type == protocol.ACTION_SUBMIT:
            return self._on_action_submit(parsed, now)
        if parsed.type == protocol.FINISH_RESULT:
            return self._on_finish_result(parsed, now)
        if parsed.type == protocol.ERROR:
            # client가 보낸 ERROR → PoC abort로 반영한다.
            code = parsed.body.get("code")
            try:
                self.fatal_error = PocError(PocErrorCode(code), "client ERROR")
            except ValueError:
                self.fatal_error = PocError(PocErrorCode.POC_PROTOCOL_ERROR, "malformed client ERROR")
            self.state = EngineState.ABORTED
            return []
        return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "unexpected message type",
                                   detail={"type": parsed.type}))

    def _verify_envelope_binding(self, parsed) -> PocError | None:
        if parsed.session_id != self.session_id or parsed.workload_id != self.workload_id:
            return PocError(PocErrorCode.POC_PROTOCOL_ERROR, "session/workload binding 불일치",
                            detail={"session": parsed.session_id, "workload": parsed.workload_id})
        return None

    def _verify_post_handshake(self, parsed) -> PocError | None:
        """handshake 뒤 모든 message의 session/workload/connection_epoch 대조(§6.2).

        HELLO/ARMED 외 message(EVENT_ACK·ACTION_SUBMIT·FINISH_RESULT)도 exact binding과
        현재 epoch에 결속돼야 한다. 불일치는 `POC_PROTOCOL_ERROR`(fail-closed).
        """

        binding_error = self._verify_envelope_binding(parsed)
        if binding_error is not None:
            return binding_error
        if parsed.connection_epoch != self.connection_epoch:
            return PocError(PocErrorCode.POC_PROTOCOL_ERROR, "connection_epoch 불일치",
                            detail={"expected_epoch": self.connection_epoch,
                                    "got_epoch": parsed.connection_epoch})
        return None

    def _on_hello(self, parsed, now):
        if self.state not in (EngineState.AWAIT_HELLO, EngineState.RECONNECTING):
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "예상치 못한 HELLO"))
        binding_error = self._verify_envelope_binding(parsed)
        if binding_error is not None:
            return self._fail(binding_error)
        body = parsed.body
        try:
            token_hex = require_body_field(body, "token", str)
            run_id = require_body_field(body, "run_id", str)
            minor_min = int(require_body_field(body, "protocol_minor_min", int))
            minor_max = int(require_body_field(body, "protocol_minor_max", int))
            connect_kind = require_body_field(body, "connect_kind", str)
            manifest_body = require_body_field(body, "manifest", dict)
            requested_caps = require_body_field(body, "requested_capabilities", list)
            requested_triggers = require_body_field(body, "requested_triggers", list)
            last_completed = int(require_body_field(body, "last_completed_sequence", int))
        except PocError as error:
            return self._fail(error)

        # connect_kind는 initial/reconnect만 허용하고 상태와 일치해야 한다.
        # (오타 등 unknown kind를 신규 연결로 처리해 WELCOME/START를 재발행하지 않는다.)
        if connect_kind not in ("initial", "reconnect"):
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "알 수 없는 connect_kind", detail={"connect_kind": connect_kind}))
        if self.state == EngineState.AWAIT_HELLO and connect_kind != "initial":
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "최초 연결은 connect_kind=initial이어야 합니다"))
        if self.state == EngineState.RECONNECTING and connect_kind != "reconnect":
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "reconnect 중에는 connect_kind=reconnect만 허용합니다"))

        # token 인증(§5.2, constant-time).
        try:
            token_bytes = bytes.fromhex(token_hex)
        except ValueError:
            return self._fail(PocError(PocErrorCode.POC_AUTH_FAILED, "token 형식 오류"))
        if not constant_time_equals(token_bytes, self._expected_token):
            return self._fail(PocError(PocErrorCode.POC_AUTH_FAILED, "token 불일치"))
        if run_id != self.run_id:
            return self._fail(PocError(PocErrorCode.POC_AUTH_FAILED, "run_id 불일치"))
        if parsed.protocol_major != protocol.PROTOCOL_MAJOR:
            return self._fail(PocError(PocErrorCode.POC_VERSION_INCOMPATIBLE, "major 불일치"))

        hello_request = HelloRequest(
            protocol_major=parsed.protocol_major,
            minor_min=minor_min,
            minor_max=minor_max,
            requested_capabilities=frozenset(requested_caps),
            requested_triggers=frozenset(requested_triggers),
        )

        if connect_kind == "reconnect":
            return self._resume_reconnect(hello_request, last_completed, body, now)
        # 최초 연결
        try:
            negotiation = negotiate(
                self._server_config, self._manifest, hello_request,
                connection_epoch=1,
            )
        except PocError as error:
            return self._fail(error)
        self.negotiation = negotiation
        self.connection_epoch = 1
        self._hello_deadline = None
        welcome = self._build_welcome()
        start = self._msg(protocol.START, {"scenario_id": self.scenario_id})
        self.state = EngineState.AWAIT_ARMED
        self._armed_deadline = now + ARMED_DEADLINE_S
        return [welcome, start]

    def _build_welcome(self) -> dict:
        from ..poc_api import limits as L

        body = {
            "connection_epoch": self.connection_epoch,
            "protocol_minor": self.negotiation.protocol_minor,
            "capabilities": sorted(self.negotiation.capabilities),
            "triggers": sorted(self.negotiation.triggers),
            "server_nonce": uuid.uuid4().hex,
            "limits": {
                "max_wire_packet_bytes": L.MAX_WIRE_PACKET_BYTES,
                "max_event_queue_messages": L.MAX_EVENT_QUEUE_MESSAGES,
                "max_action_descriptor_bytes": L.MAX_ACTION_DESCRIPTOR_BYTES,
                "max_workload_action_requests": L.MAX_WORKLOAD_ACTION_REQUESTS,
                "max_inflight_actions": L.MAX_INFLIGHT_ACTIONS,
            },
        }
        return self._msg(protocol.WELCOME, body)

    def _resume_reconnect(self, hello_request, last_completed, body, now):
        # 같은 workload binding·같은 협상 결과만 복원한다(§12.3). 새 capability 확대 금지.
        if self.negotiation is None:
            return self._fail(PocError(PocErrorCode.POC_CONNECTION_LOST, "복원할 협상이 없습니다"))
        try:
            candidate = negotiate(
                self._server_config, self._manifest, hello_request,
                connection_epoch=self.connection_epoch + 1,
            )
        except PocError as error:
            return self._fail(error)
        if not restore_matches(self.negotiation, candidate):
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "reconnect가 capability를 확대·변경했습니다"))
        self.connection_epoch += 1
        self.evidence["reconnects"] += 1
        self.negotiation = candidate  # epoch만 갱신, 나머지 동일
        welcome = self._build_welcome()
        outbound = [welcome]
        # unresolved request cache 조회 및 같은 ID 응답(§12.3.2)
        for request_id in body.get("unresolved_request_ids", []) or []:
            cached = self._cache.get(request_id)
            if cached is not None:
                outbound.append(self._msg(protocol.ACTION_RESULT, cached.result_body))
                if cached.terminal_body is not None:
                    outbound.append(self._msg(protocol.ACTION_TERMINAL, cached.terminal_body))
        # reconnect가 주장하는 last_completed는 실제 emit한 최고 sequence를 넘을 수 없다.
        highest_emitted = self._next_sequence - 1
        if last_completed < 0 or last_completed > highest_emitted:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "reconnect last_completed_sequence가 emit 범위를 벗어남",
                                       detail={"last_completed": last_completed,
                                               "highest_emitted": highest_emitted}))
        # 마지막 completed sequence 이후 event replay(§12.3.3, §8.3)
        replay = self._replay_from(last_completed)
        if isinstance(replay, PocError):
            return self._fail(replay)
        outbound.extend(replay)
        self.state = EngineState.RUNNING
        self._armed_deadline = None
        self._hello_deadline = None
        return outbound

    def begin_reconnect_window(self, now: float) -> None:
        """transport 두절 시 호출(§12.1). 신규 action admission을 닫고 대기."""

        if self.state in (EngineState.CLOSED, EngineState.ABORTED):
            return
        self.state = EngineState.RECONNECTING

    def _replay_from(self, last_completed: int):
        # ring은 아직 ACK되지 않은 event를 순서대로 보존한다.
        if not self._ring:
            return []
        first_available = self._ring[0].sequence
        if last_completed + 1 < first_available:
            # 필요한 sequence가 이미 evict됐다 → gap(§8.3).
            return PocError(PocErrorCode.POC_EVENT_REPLAY_GAP, "replay ring에서 필요한 event가 사라짐",
                            detail={"need_from": last_completed + 1, "have_from": first_available})
        outbound = []
        for item in self._ring:
            if item.sequence > last_completed:
                outbound.append(self._emit_event_message(item))
                self.evidence["events_replayed"] += 1
        return outbound

    def _on_armed(self, parsed, now):
        if self.state != EngineState.AWAIT_ARMED:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "예상치 못한 ARMED"))
        binding_error = self._verify_envelope_binding(parsed)
        if binding_error is not None:
            return self._fail(binding_error)
        if parsed.connection_epoch != self.connection_epoch:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "connection_epoch 불일치"))
        self.state = EngineState.RUNNING
        self._armed_deadline = None
        return []

    # --- event pipeline -----------------------------------------------------
    def enqueue_event(self, event_type: str, body: dict, now: float) -> list:
        """production event producer가 호출하는 bounded enqueue(§3.1, §10).

        RF worker를 block하지 않는다. 상한 초과는 `POC_EVENT_QUEUE_OVERFLOW`. 반환
        message는 drain 시점에 전송하며, 여기서는 enqueue만 하고 빈 list를 돌려준다.
        """

        if self.state in (EngineState.CLOSED, EngineState.ABORTED):
            return []
        size = len(encode_canonical(body))
        if (
            len(self._queue) + 1 > MAX_EVENT_QUEUE_MESSAGES
            or self._queue_bytes + size > MAX_EVENT_QUEUE_BYTES
        ):
            return self._fail(PocError(PocErrorCode.POC_EVENT_QUEUE_OVERFLOW,
                                       "event queue overflow",
                                       detail={"messages": len(self._queue), "bytes": self._queue_bytes}))
        item = _QueuedEvent(event_type, dict(body), size)
        self._queue.append(item)
        self._queue_bytes += size
        self.evidence["event_queue_high_water"] = max(
            self.evidence["event_queue_high_water"], len(self._queue)
        )
        return []

    def enqueue_observation(self, record, now: float) -> list:
        """관측 provider가 낸 observation 처리(§8.4).

        `NOT_TUNED`면 event·bytes를 만들지 않고 delivery matrix counter/state로만
        기록한다. `NOT_TUNED`는 transport/connection 오류가 아니다.
        """

        if record.tuning_state == TuningState.NOT_TUNED:
            self.evidence["not_tuned_count"] += 1
            self._record_delivery_matrix(record, TuningState.NOT_TUNED, emitted=False)
            return []
        self.evidence["tuned_observer_count"] += 1
        self._record_delivery_matrix(record, TuningState.TUNED_OBSERVER, emitted=True)
        return self.enqueue_event("PACKET_OBSERVED", record.as_event_body(), now)

    def _record_delivery_matrix(self, record, tuning_state, *, emitted: bool) -> None:
        """delivery_matrix 상세 항목을 bounded list에 기록(§3.3/§16). 상한 초과분은
        counter(not_tuned_count·tuned_observer_count)로만 남기고 drop 수를 센다.
        counter는 무제한 int로 유지하므로 관측 수가 커도 메모리는 유계다."""

        matrix = self.evidence["delivery_matrix"]
        if len(matrix) >= MAX_DELIVERY_MATRIX_ENTRIES:
            self.evidence["delivery_matrix_dropped"] += 1
            self.evidence["delivery_matrix_truncated"] = True
            return
        matrix.append({
            "physical_channel": record.physical_channel.value,
            "tuning_state": tuning_state.value,
            "observation_id": record.observation_id,
            "source_station": record.source_station,
            "emitted_event": emitted,
        })

    def _emit_event_message(self, item: _QueuedEvent) -> dict:
        body = {
            "sequence": item.sequence,
            "event_id": item.event_id,
            "event_type": item.event_type,
            "payload_digest": item.payload_digest,
            "body": item.body,
        }
        return self._msg(protocol.EVENT, body)

    def drain(self, now: float) -> list:
        """queue의 event에 sequence/event_id를 부여해 전송하고 replay ring으로 옮긴다."""

        if self.state not in (EngineState.RUNNING,):
            return []
        outbound = []
        while self._queue:
            item = self._queue[0]
            # ring 상한 확인(unacked 보존 불가 시 overflow·gap으로 abort; §10.2).
            if (
                len(self._ring) + 1 > MAX_REPLAY_RING_MESSAGES
                or self._ring_bytes + item.size > MAX_REPLAY_RING_BYTES
            ):
                return self._fail(PocError(PocErrorCode.POC_EVENT_QUEUE_OVERFLOW,
                                           "replay ring overflow(unacked 보존 불가)",
                                           detail={"ring_messages": len(self._ring)}))
            self._queue.pop(0)
            self._queue_bytes -= item.size
            item.sequence = self._next_sequence
            self._next_sequence += 1
            item.event_id = uuid.uuid4().hex
            item.payload_digest = canonical_sha256(item.body)
            self._ring.append(item)
            self._ring_bytes += item.size
            self.evidence["replay_ring_high_water"] = max(
                self.evidence["replay_ring_high_water"], len(self._ring)
            )
            self.evidence["events_emitted"] += 1
            if self._oldest_unacked_sent_at is None:
                self._oldest_unacked_sent_at = now
            outbound.append(self._emit_event_message(item))
        return outbound

    def _on_event_ack(self, parsed, now):
        binding_error = self._verify_post_handshake(parsed)
        if binding_error is not None:
            return self._fail(binding_error)
        body = parsed.body
        try:
            cumulative = int(require_body_field(body, "cumulative_sequence", int))
        except PocError as error:
            return self._fail(error)
        if cumulative < self.last_acked_sequence:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "ACK sequence 후퇴"))
        # 아직 emit하지 않은 미래 sequence를 ACK할 수 없다(전달 안 된 event를 evict 금지).
        highest_emitted = self._next_sequence - 1
        if cumulative > highest_emitted:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                       "ACK가 emit 범위를 초과",
                                       detail={"cumulative": cumulative,
                                               "highest_emitted": highest_emitted}))
        self.last_acked_sequence = cumulative
        # ring에서 ACK된 event를 evict.
        kept = []
        for item in self._ring:
            if item.sequence <= cumulative:
                self._ring_bytes -= item.size
            else:
                kept.append(item)
        self._ring = kept
        if not self._ring:
            self._oldest_unacked_sent_at = None
        return []

    # --- action -------------------------------------------------------------
    def _on_action_submit(self, parsed, now):
        binding_error = self._verify_post_handshake(parsed)
        if binding_error is not None:
            # 잘못된 session/workload/epoch action은 protocol 위반으로 fail-closed(provider 미호출).
            return self._fail(binding_error)
        if self.state != EngineState.RUNNING:
            # ARMED 전/종료 후 action은 허용하지 않는다.
            return [self._reject_action(parsed.body.get("request_id"),
                                        PocErrorCode.POC_NOT_RUNNING, cache=False)]
        body = parsed.body
        try:
            request_id = require_body_field(body, "request_id", str)
            action = require_body_field(body, "action", dict)
        except PocError as error:
            return self._fail(error)

        digest = canonical_sha256(action)

        cached = self._cache.get(request_id)
        if cached is not None:
            if cached.digest != digest:
                return self._fail(PocError(PocErrorCode.POC_REQUEST_ID_CONFLICT,
                                           "같은 request_id, 다른 body",
                                           detail={"request_id": request_id}))
            # 동일 ID·동일 digest 재요청 → scheduler/RF submit 재호출 없이 known result(§9.2).
            self.evidence["actions_cached_hits"] += 1
            out = [self._msg(protocol.ACTION_RESULT, cached.result_body)]
            if cached.terminal_body is not None:
                out.append(self._msg(protocol.ACTION_TERMINAL, cached.terminal_body))
            return out

        # 신규 request: 총수 상한(§10.2) — content와 무관하게 거부.
        if len(self._cache) >= MAX_WORKLOAD_ACTION_REQUESTS:
            return [self._reject_action(request_id, PocErrorCode.POC_ACTION_LIMIT_EXCEEDED, cache=False)]

        # content 검증.
        content_error = self._validate_action_content(action)
        if content_error is not None:
            return [self._reject_action(request_id, content_error, cache=True, digest=digest)]

        # in-flight 1개(§10.1).
        if len(self._inflight) >= MAX_INFLIGHT_ACTIONS:
            return [self._reject_action(request_id, PocErrorCode.POC_ATTACK_INFLIGHT, cache=False)]

        # provider.submit은 request_id별로 정확히 한 번(§9.2).
        from ..poc_api.types import ActionDescriptor

        descriptor = ActionDescriptor(
            kind=ActionKind(action["action_kind"]),
            target=action["target"],
            source_observation_id=action.get("source_observation_id"),
            schedule=dict(action.get("schedule") or {}),
            payload=dict(action.get("payload") or {}),
        )
        try:
            submission = self._provider.submit(descriptor, request_id=request_id)
        except PocError as error:
            return self._fail(error) if error.severity.value != "REQUEST_REJECTED" else [
                self._reject_action(request_id, error.code, cache=False)
            ]
        self.evidence["provider_submit_calls"] += 1
        self.evidence["actions_accepted"] += 1
        result_body = {
            "request_id": request_id,
            "admission": "accepted",
            "physical_channel": submission.physical_channel.value,
            "broadcast_parent_id": submission.broadcast_parent_id,
            "transfer_id": submission.transfer_id,
            "error_code": None,
        }
        cache_entry = _CachedAction(digest, result_body, accepted=True)
        self._cache[request_id] = cache_entry
        self._inflight.add(request_id)
        return [self._msg(protocol.ACTION_RESULT, result_body)]

    def _validate_action_content(self, action: dict) -> PocErrorCode | None:
        # canonical size(§10.1).
        if len(encode_canonical(action)) > MAX_ACTION_DESCRIPTOR_BYTES:
            return PocErrorCode.POC_ACTION_TOO_LARGE
        # action_kind.
        kind = action.get("action_kind")
        try:
            ActionKind(kind)
        except ValueError:
            return PocErrorCode.POC_ACTION_SCHEMA_INVALID
        # 금지 RF field(§9.4) — 어느 depth에서든.
        if _has_forbidden_field(action):
            return PocErrorCode.POC_ACTION_SCHEMA_INVALID
        # 논리 target(§9.4).
        target = action.get("target")
        if target not in protocol.LOGICAL_TARGETS:
            return PocErrorCode.POC_TARGET_DENIED
        if target not in set(self._manifest.allowed_targets):
            return PocErrorCode.POC_TARGET_DENIED
        return None

    def _reject_action(self, request_id, code: PocErrorCode, *, cache: bool, digest: str = "") -> dict:
        self.evidence["actions_rejected"] += 1
        result_body = {
            "request_id": request_id,
            "admission": "rejected",
            "physical_channel": None,
            "broadcast_parent_id": None,
            "transfer_id": None,
            "error_code": code.value,
        }
        if cache and request_id:
            self._cache[request_id] = _CachedAction(digest, result_body, accepted=False)
        return self._msg(protocol.ACTION_RESULT, result_body)

    def provide_action_terminal(self, request_id: str, *, action_execution: str,
                                target_effect: str | None = None, now: float = 0.0) -> list:
        """accepted action의 terminal을 client에 전달한다(§6.3 ACTION_TERMINAL).

        real provider는 나중에, fake provider는 즉시 호출할 수 있다. in-flight를 해제한다.
        """

        cached = self._cache.get(request_id)
        if cached is None or not cached.accepted:
            return []
        terminal_body = {
            "request_id": request_id,
            "transfer_id": cached.result_body.get("transfer_id"),
            "action_execution": action_execution,
            "target_effect": target_effect,
            "physical_channel": cached.result_body.get("physical_channel"),
            "broadcast_parent_id": cached.result_body.get("broadcast_parent_id"),
        }
        cached.terminal_body = terminal_body
        self._inflight.discard(request_id)
        return [self._msg(protocol.ACTION_TERMINAL, terminal_body)]

    # --- finish/close -------------------------------------------------------
    def begin_finish(self, now: float) -> list:
        if self.state not in (EngineState.RUNNING,):
            return []
        self.state = EngineState.FINISHING
        return [self._msg(protocol.FINISH, {"cumulative_sequence": self.last_acked_sequence})]

    def _on_finish_result(self, parsed, now):
        binding_error = self._verify_post_handshake(parsed)
        if binding_error is not None:
            return self._fail(binding_error)
        if self.state != EngineState.FINISHING:
            return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR, "예상치 못한 FINISH_RESULT"))
        self.state = EngineState.CLOSING
        return [self._msg(protocol.CLOSE, {"infrastructure": "COMPLETED"})]

    def force_close(self, now: float, *, infrastructure: str = "COMPLETED") -> list:
        self.state = EngineState.CLOSING
        return [self._msg(protocol.CLOSE, {"infrastructure": infrastructure})]

    def mark_closed(self) -> None:
        self.state = EngineState.CLOSED

    # --- deadline -----------------------------------------------------------
    def tick(self, now: float) -> list:
        if self.state == EngineState.AWAIT_HELLO and self._hello_deadline is not None:
            if now > self._hello_deadline:
                return self._fail(PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                           "HELLO deadline 초과", detail={"deadline_s": HELLO_DEADLINE_S}))
        if self.state == EngineState.AWAIT_ARMED and self._armed_deadline is not None:
            if now > self._armed_deadline:
                return self._fail(PocError(PocErrorCode.POC_CALLBACK_TIMEOUT,
                                           "on_start(ARMED) deadline 초과",
                                           detail={"deadline_s": ARMED_DEADLINE_S}))
        if self.state == EngineState.RUNNING and self._oldest_unacked_sent_at is not None:
            if now - self._oldest_unacked_sent_at > SERVER_EVENT_ACK_GRACE_S:
                return self._fail(PocError(PocErrorCode.POC_CALLBACK_TIMEOUT,
                                           "on_event ACK grace 초과(callback hang)",
                                           detail={"grace_s": SERVER_EVENT_ACK_GRACE_S}))
        return []

    def next_deadline(self, now: float) -> float | None:
        """가장 가까운 deadline까지 남은 시간(초). 없으면 None. serve loop의 select timeout."""

        candidates = []
        if self.state == EngineState.AWAIT_HELLO and self._hello_deadline is not None:
            candidates.append(self._hello_deadline)
        if self.state == EngineState.AWAIT_ARMED and self._armed_deadline is not None:
            candidates.append(self._armed_deadline)
        if self.state == EngineState.RUNNING and self._oldest_unacked_sent_at is not None:
            candidates.append(self._oldest_unacked_sent_at + SERVER_EVENT_ACK_GRACE_S)
        if not candidates:
            return None
        return max(0.0, min(candidates) - now)

    # --- 잔존 검사용 snapshot ------------------------------------------------
    def residual_snapshot(self) -> dict:
        """cleanup 검증용: queue·ring·inflight 잔존(§15, §26 요구)."""

        return {
            "event_queue": len(self._queue),
            "replay_ring": len(self._ring),
            "inflight_actions": len(self._inflight),
            "cached_requests": len(self._cache),
        }


def _has_forbidden_field(value: object, depth: int = 0) -> bool:
    """action body 어느 depth에서든 금지 RF field가 있으면 True(§9.4)."""

    if depth > 32:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_ACTION_FIELDS:
                return True
            if _has_forbidden_field(item, depth + 1):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_forbidden_field(item, depth + 1):
                return True
    return False


__all__ = (
    "EngineState",
    "SupervisorEngine",
    "SERVER_EVENT_ACK_GRACE_S",
)
