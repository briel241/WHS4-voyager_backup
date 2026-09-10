"""외부 PoC가 import하는 public SDK(client 측 state machine).

이 module은 production private 구현(`_supervisor`)을 import하지 않는다. 연결·handshake·
event 처리·누적 ACK·action 멱등성·reconnect 복구의 client 측 계약을 담는다. 연구자
callback의 **의미**(어떤 공격을 할지)는 Stage 4 PoC가 정하며, 여기서는 callback을
직렬 동기 호출하고 반환된 논리 action descriptor만 전송한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from . import protocol
from .codec import canonical_sha256
from .errors import PocError, PocErrorCode
from .limits import (
    ACK_BATCH_EVENTS,
    ACK_MAX_DELAY_S,
    CALLBACK_ON_EVENT_DEADLINE_S,
    CALLBACK_ON_FINISH_DEADLINE_S,
    CALLBACK_ON_START_DEADLINE_S,
    MAX_ACTION_DESCRIPTOR_BYTES,
    MAX_REPLAY_RING_MESSAGES,
)
from .messages import build_message, parse_envelope
from .types import ActionDescriptor, ActionKind, Event


class ClientState(str, Enum):
    INIT = "INIT"
    HELLO_SENT = "HELLO_SENT"
    WELCOMED = "WELCOMED"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    FINISHING = "FINISHING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


@dataclass
class PocContext:
    """callback에 넘기는 immutable metadata와 action builder(§7.2 실행 명세).

    builder는 논리 target만 받는 :class:`ActionDescriptor`를 만든다. frequency·lane·
    gain·waveform·raw IQ 같은 RF field는 만들 수 없다(§9.4).
    """

    session_id: str
    workload_id: str
    scenario_id: str
    negotiated_capabilities: frozenset = field(default_factory=frozenset)
    negotiated_triggers: frozenset = field(default_factory=frozenset)

    def _build(self, kind: ActionKind, target: str, *, source_observation_id=None,
               schedule=None, payload=None) -> ActionDescriptor:
        return ActionDescriptor(
            kind=kind,
            target=target,
            source_observation_id=source_observation_id,
            schedule=dict(schedule or {}),
            payload=dict(payload or {}),
        )

    def replay(self, *, target, source_observation_id=None, schedule=None, payload=None):
        return self._build(ActionKind.REPLAY_PACKET, target,
                           source_observation_id=source_observation_id,
                           schedule=schedule, payload=payload)

    def mutate(self, *, target, source_observation_id=None, schedule=None, payload=None):
        return self._build(ActionKind.MUTATE_PACKET, target,
                           source_observation_id=source_observation_id,
                           schedule=schedule, payload=payload)

    def forge(self, *, target, schedule=None, payload=None):
        return self._build(ActionKind.FORGE_PACKET, target, schedule=schedule, payload=payload)

    def duplicate(self, *, target, source_observation_id=None, schedule=None, payload=None):
        return self._build(ActionKind.DUPLICATE_PACKET, target,
                           source_observation_id=source_observation_id,
                           schedule=schedule, payload=payload)


@runtime_checkable
class PocCallbacks(Protocol):
    """연구자 callback 계약(§7.1 실행 명세, 연결 API §7.3/§8.2/§15).

    동기식이며 action descriptor의 유한 목록만 반환한다. production object를 받지
    않는다.
    """

    def on_start(self, context: PocContext) -> list:
        ...

    def on_event(self, event: Event, context: PocContext) -> list:
        ...

    def on_finish(self, context: PocContext) -> None:
        ...


@dataclass
class _Submitted:
    request_id: str
    body: dict
    digest: str
    status: str  # UNRESOLVED / ACCEPTED / REJECTED / TERMINAL


class SdkEngine:
    """client 측 순수 state machine. I/O·clock을 갖지 않고 message와 now만 받는다.

    `receive(parsed, now)`/`tick(now)`는 보낼 message envelope 목록을 돌려준다. 이
    설계로 fake clock·fake transport로 deadline·ACK·reconnect·멱등성을 sleep 없이
    검증한다(§18).
    """

    def __init__(self, callbacks: PocCallbacks, context: PocContext) -> None:
        self._callbacks = callbacks
        self._context = context
        self.state = ClientState.INIT
        self.negotiation = None
        self.connection_epoch = 0
        self._reconnecting = False
        # event 처리
        self.last_completed_sequence = 0
        self._unacked_since = None  # 첫 미ACK event 처리 시각(fake clock 기준)
        self._events_since_ack = 0
        # 완료 event의 (event_id, payload_digest) — replay 무결성 대조용(bounded).
        self._completed_events: dict[int, tuple[str, str]] = {}
        # action 멱등성
        self._submitted: dict[str, _Submitted] = {}
        self._pending_descriptors: list[ActionDescriptor] = []
        self._inflight_request_id: str | None = None
        self.unresolved: set[str] = set()
        self.aborted_error: PocError | None = None

    # --- 내부 유틸 ----------------------------------------------------------
    def _msg(self, message_type: str, body: dict) -> dict:
        return build_message(
            message_type,
            session_id=self._context.session_id,
            workload_id=self._context.workload_id,
            connection_epoch=self.connection_epoch,
            protocol_minor=(self.negotiation.protocol_minor if self.negotiation else protocol.CLIENT_MIN_MINOR),
            body=body,
        )

    def _abort(self, code: PocErrorCode, message: str, **detail) -> list:
        self.aborted_error = PocError(code, message, detail=detail)
        self.state = ClientState.ABORTED
        return [self._msg(protocol.ERROR, self.aborted_error.as_json_value())]

    # --- HELLO --------------------------------------------------------------
    def build_hello(self, manifest_hello: dict, *, connect_kind: str = "initial") -> dict:
        body = {
            "token": manifest_hello["token"],
            "run_id": manifest_hello["run_id"],
            "client_nonce": uuid.uuid4().hex,
            "protocol_minor_min": protocol.CLIENT_MIN_MINOR,
            "protocol_minor_max": protocol.CLIENT_MAX_MINOR,
            "connect_kind": connect_kind,
            "manifest": manifest_hello["manifest"],
            "requested_capabilities": manifest_hello["requested_capabilities"],
            "requested_triggers": manifest_hello["requested_triggers"],
            "last_completed_sequence": self.last_completed_sequence,
            "unresolved_request_ids": sorted(self.unresolved),
        }
        self._reconnecting = connect_kind == "reconnect"
        self.state = ClientState.HELLO_SENT
        return self._msg(protocol.HELLO, body)

    # --- 수신 처리 ----------------------------------------------------------
    def receive(self, parsed, now: float) -> list:
        if self.state in (ClientState.CLOSED, ClientState.ABORTED):
            return []
        handler = {
            protocol.WELCOME: self._on_welcome,
            protocol.START: self._on_start,
            protocol.EVENT: self._on_event,
            protocol.ACTION_RESULT: self._on_action_result,
            protocol.ACTION_TERMINAL: self._on_action_terminal,
            protocol.FINISH: self._on_finish,
            protocol.CLOSE: self._on_close,
            protocol.ERROR: self._on_error,
        }.get(parsed.type)
        if handler is None:
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "unexpected message type", type=parsed.type)
        return handler(parsed, now)

    def _on_welcome(self, parsed, now):
        from .types import Negotiation

        body = parsed.body
        try:
            self.connection_epoch = int(body["connection_epoch"])
            self.negotiation = Negotiation(
                protocol_minor=int(body["protocol_minor"]),
                capabilities=frozenset(body["capabilities"]),
                triggers=frozenset(body["triggers"]),
                connection_epoch=self.connection_epoch,
            )
        except (KeyError, TypeError, ValueError):
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "malformed WELCOME")
        self._context.negotiated_capabilities = self.negotiation.capabilities
        self._context.negotiated_triggers = self.negotiation.triggers
        if self._reconnecting:
            # reconnect는 새 START/ARMED 없이 RUNNING으로 복원한다(§12.3). 재전송할 unresolved는
            # resend_unresolved로 즉시 다시 보낸다.
            self._reconnecting = False
            self.state = ClientState.RUNNING
            return self.resend_unresolved()
        self.state = ClientState.WELCOMED
        return []

    def _on_start(self, parsed, now):
        if self.state not in (ClientState.WELCOMED,):
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "START before WELCOME")
        try:
            descriptors = self._callbacks.on_start(self._context)
        except Exception as error:  # noqa: BLE001 - 예외는 삼키지 않고 유계 변환
            return self._abort(PocErrorCode.POC_CALLBACK_EXCEPTION, f"on_start raised: {error!r}")
        if descriptors:
            # on_start는 action을 제출하지 않는다(§7.3). 선제 action은 이후 event에서.
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "on_start returned actions")
        self.state = ClientState.ARMED
        return [self._msg(protocol.ARMED, {"client_nonce": uuid.uuid4().hex})]

    def _on_event(self, parsed, now):
        if self.state not in (ClientState.ARMED, ClientState.RUNNING):
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "EVENT before ARMED")
        self.state = ClientState.RUNNING
        body = parsed.body
        try:
            sequence = int(body["sequence"])
            event_id = str(body["event_id"])
            event_type = str(body["event_type"])
            event_body = dict(body.get("body") or {})
            payload_digest = str(body.get("payload_digest") or "")
        except (KeyError, TypeError, ValueError):
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "malformed EVENT")
        # payload_digest는 event body의 canonical SHA-256과 일치해야 한다(§8.1). 모순은
        # fail-closed(REQ-13): 잘못된 digest의 event는 성공으로 추정하지 않는다.
        actual_digest = canonical_sha256(event_body)
        if payload_digest != actual_digest:
            return self._abort(PocErrorCode.POC_EVENT_REPLAY_GAP, "payload digest 모순",
                               sequence=sequence, declared=payload_digest, actual=actual_digest)
        if sequence <= self.last_completed_sequence:
            # 이미 callback-completed인 sequence(replay)면 callback을 다시 호출하지 않는다(§8.2).
            # 단, 같은 sequence는 같은 event_id·digest여야 한다(§8.1). 다르면 fail-closed(§8.3).
            recorded = self._completed_events.get(sequence)
            if recorded is not None and recorded != (event_id, payload_digest):
                return self._abort(PocErrorCode.POC_EVENT_REPLAY_GAP,
                                   "replay event_id/digest 모순", sequence=sequence)
            return self._maybe_ack(now, force=True)
        if sequence != self.last_completed_sequence + 1:
            # 연속 증가가 아니면 gap. server는 순서대로 replay하므로 client 측 gap은 오류.
            return self._abort(PocErrorCode.POC_EVENT_REPLAY_GAP, "event sequence gap",
                               expected=self.last_completed_sequence + 1, got=sequence)
        event = Event(
            sequence=sequence,
            event_id=event_id,
            type=event_type,
            body=event_body,
            payload_digest=payload_digest,
        )
        try:
            descriptors = self._callbacks.on_event(event, self._context) or []
        except Exception as error:  # noqa: BLE001
            return self._abort(PocErrorCode.POC_CALLBACK_EXCEPTION, f"on_event raised: {error!r}")
        # callback-completed sequence 전진 + replay 대조용 (event_id, digest) 기록(bounded).
        self.last_completed_sequence = sequence
        self._completed_events[sequence] = (event_id, payload_digest)
        if len(self._completed_events) > MAX_REPLAY_RING_MESSAGES:
            oldest = min(self._completed_events)
            self._completed_events.pop(oldest, None)
        if self._unacked_since is None:
            self._unacked_since = now
        self._events_since_ack += 1
        outbound: list = []
        # 새 action 제출(unresolved가 없을 때만; §9.3)
        for descriptor in descriptors:
            self._pending_descriptors.append(descriptor)
        outbound.extend(self._pump_actions())
        outbound.extend(self._maybe_ack(now))
        return outbound

    def _pump_actions(self) -> list:
        outbound: list = []
        # unresolved 또는 in-flight가 있으면 새 action을 제출하지 않는다(§9.3, in-flight 1).
        while (
            self._pending_descriptors
            and not self.unresolved
            and self._inflight_request_id is None
        ):
            descriptor = self._pending_descriptors.pop(0)
            request_id = uuid.uuid4().hex
            body = {"request_id": request_id, "action": descriptor.as_body()}
            digest = canonical_sha256(body["action"])
            record = _Submitted(request_id=request_id, body=body, digest=digest, status="UNRESOLVED")
            self._submitted[request_id] = record
            self.unresolved.add(request_id)
            self._inflight_request_id = request_id
            outbound.append(self._msg(protocol.ACTION_SUBMIT, body))
        return outbound

    def _maybe_ack(self, now: float, *, force: bool = False) -> list:
        if self._unacked_since is None and not force:
            return []
        should = force or self._events_since_ack >= ACK_BATCH_EVENTS or (
            self._unacked_since is not None and (now - self._unacked_since) >= ACK_MAX_DELAY_S
        )
        if not should:
            return []
        ack = self._msg(protocol.EVENT_ACK, {"cumulative_sequence": self.last_completed_sequence})
        self._unacked_since = None
        self._events_since_ack = 0
        return [ack]

    def _on_action_result(self, parsed, now):
        body = parsed.body
        request_id = body.get("request_id")
        record = self._submitted.get(request_id)
        if record is None:
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "unknown request_id in ACTION_RESULT")
        admission = body.get("admission")
        self.unresolved.discard(request_id)
        if admission == "rejected":
            record.status = "REJECTED"
            self._inflight_request_id = None
            return self._pump_actions()
        record.status = "ACCEPTED"
        # accepted면 ACTION_TERMINAL까지 in-flight 유지(§10.3 실행 명세: terminal 뒤 다음).
        return []

    def _on_action_terminal(self, parsed, now):
        body = parsed.body
        request_id = body.get("request_id")
        record = self._submitted.get(request_id)
        if record is None:
            return self._abort(PocErrorCode.POC_PROTOCOL_ERROR, "unknown request_id in ACTION_TERMINAL")
        record.status = "TERMINAL"
        self.unresolved.discard(request_id)
        if self._inflight_request_id == request_id:
            self._inflight_request_id = None
        return self._pump_actions()

    def _on_finish(self, parsed, now):
        self.state = ClientState.FINISHING
        try:
            self._callbacks.on_finish(self._context)
        except Exception as error:  # noqa: BLE001
            return self._abort(PocErrorCode.POC_CALLBACK_EXCEPTION, f"on_finish raised: {error!r}")
        ack_body = {"cumulative_sequence": self.last_completed_sequence}
        return [self._msg(protocol.FINISH_RESULT, ack_body)]

    def _on_close(self, parsed, now):
        self.state = ClientState.CLOSED
        return []

    def _on_error(self, parsed, now):
        self.state = ClientState.ABORTED
        try:
            self.aborted_error = PocError(
                PocErrorCode(parsed.body.get("code")), parsed.body.get("detail", {}).get("message", "")
            )
        except (ValueError, AttributeError):
            self.aborted_error = PocError(PocErrorCode.POC_PROTOCOL_ERROR, "malformed ERROR")
        return []

    def tick(self, now: float) -> list:
        """time 기반 one-shot ACK(§8.2). periodic polling이 아니라 미ACK가 있을 때만."""

        if self.state in (ClientState.CLOSED, ClientState.ABORTED):
            return []
        return self._maybe_ack(now)

    def resend_unresolved(self) -> list:
        """연결이 살아 있으면 같은 request ID를 정확히 한 번 즉시 재전송(§9.3)."""

        outbound = []
        for request_id in sorted(self.unresolved):
            record = self._submitted[request_id]
            outbound.append(self._msg(protocol.ACTION_SUBMIT, record.body))
        return outbound


__all__ = (
    "ClientState",
    "PocContext",
    "PocCallbacks",
    "SdkEngine",
)
