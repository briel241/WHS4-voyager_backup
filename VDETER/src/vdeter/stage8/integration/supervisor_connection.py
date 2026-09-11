"""Stage 4 runner의 `WorkloadConnection`을 **실제 `PocSupervisor`**로 구현한다(production 연결).

RF-off 시험은 `LoopbackConnection`(in-process SupervisorEngine+SdkEngine)으로 runner를 검증했지만,
production 통합에서는 이 `SupervisorConnection`이 같은 seam을 **실 subprocess child·실 Unix
`SOCK_SEQPACKET` socket·SDK·engine**으로 구현한다. 장비 경계(RF)만 software로 대체하며 child·
socket·SDK·engine·provider는 모두 실제다.

담는 것:

- `PocSupervisor`를 상속해 server→client outbound(`_send_all`)와 client→server inbound
  (`_handle_inbound`)를 tee해 `poll_new_results`/`submitted_action`을 구현한다.
- 관측 event의 `observation_id`를 provider의 `register_observation_trigger`에 **정수 slot**으로
  등록해 반응형 인과성을 결속한다(H1c 연결, L4 방어: raw ValueError 차단).
- workload close에서 **provider 예약·trigger cleanup**을 함께 수행하고 잔존/소유권 불확실을
  runner에 노출한다(§13, §10).

pump는 실 subprocess 타이밍을 다루므로 quiescence 기반이다: child가 event/observation에 반응해
ACTION_SUBMIT을 보내고 조용해질 때까지 bounded 시간 동안 I/O를 돌린다.
"""

from __future__ import annotations

import time

from .._supervisor.engine import EngineState
from .._supervisor.supervisor import PocSupervisor
from ..poc_api import protocol
from ..poc_api.codec import decode_wire_packet
from ..poc_api.errors import PocError

#: 반응형 trigger slot을 유도할 slot grid(shared_medium NextSafeSlotScheduler와 동일 규약).
_SLOT_SAMPLES = 8192


class SupervisorConnection(PocSupervisor):
    """`WorkloadConnection`을 실 `PocSupervisor`로 구현한다(실 child·socket·SDK).

    `provider`는 `PocSupervisor`의 provider이자 이 connection이 trigger 등록·예약 cleanup에
    쓰는 참조다. RF-off에서는 `assembly.assemble_rf_off_provider`로 만든 SharedMediumActionProvider,
    실장비에서는 실 Stage 7 seam으로 조립한 provider를 넣는다.
    """

    def __init__(self, *, provider, pump_timeout: float = 5.0, quiescence: float = 0.15,
                 poll: float = 0.02, start_timeout: float = 15.0, **kwargs) -> None:
        super().__init__(provider=provider, **kwargs)
        self._provider = provider
        self._pump_timeout = float(pump_timeout)
        self._quiescence = float(quiescence)
        self._poll = float(poll)
        self._start_timeout = float(start_timeout)
        self._submitted: dict = {}      # request_id -> action body(§9.4)
        self._results: dict = {}        # request_id -> ACTION_RESULT body
        self._terminals: dict = {}      # request_id -> ACTION_TERMINAL body
        self._new_results: list = []    # since last poll
        self._armed_reached = False
        self._conn_closed = False
        self._msg_count = 0             # outbound+inbound activity counter(quiescence 판정)
        self._slot_counter = 0          # 반응형 trigger용 단조 정수 slot

    # --- outbound/inbound tee ----------------------------------------------
    def _send_all(self, messages) -> None:
        messages = list(messages)
        for message in messages:
            self._observe_outbound(message)
        super()._send_all(messages)

    def _observe_outbound(self, message: dict) -> None:
        self._msg_count += 1
        mtype = message.get("type")
        body = message.get("body") or {}
        if mtype == protocol.ACTION_RESULT:
            self._results[body.get("request_id")] = dict(body)
            self._new_results.append(dict(body))
        elif mtype == protocol.ACTION_TERMINAL:
            self._terminals[body.get("request_id")] = dict(body)
        elif mtype == protocol.ARMED:
            self._armed_reached = True

    def _handle_inbound(self, data: bytes) -> None:
        self._msg_count += 1
        try:
            # decode_wire_packet은 raw dict를 돌려준다(parse_envelope는 객체 -> 여기선 dict 접근).
            obj = decode_wire_packet(data)
            if isinstance(obj, dict) and obj.get("type") == protocol.ACTION_SUBMIT:
                body = obj.get("body") or {}
                rid = body.get("request_id")
                if isinstance(rid, str):
                    self._submitted[rid] = dict(body.get("action") or {})
        except Exception:  # noqa: BLE001 - tee 실패가 실제 처리를 막지 않는다(super가 fail-closed)
            pass
        super()._handle_inbound(data)

    # --- WorkloadConnection 구현 -------------------------------------------
    def start(self) -> None:
        super().start()  # 실 socket 생성·child spawn·accept·cred 검증·handshake 시작
        self.run_until(lambda: self.engine.state == EngineState.RUNNING, timeout=self._start_timeout)
        if self.engine is not None and self.engine.state == EngineState.RUNNING:
            self._armed_reached = True

    @property
    def armed(self) -> bool:
        return self._armed_reached and self.infrastructure_error is None and not self._conn_closed

    @property
    def running(self) -> bool:
        return (self.engine is not None and self.engine.state == EngineState.RUNNING
                and self.infrastructure_error is None and not self._conn_closed)

    def emit_event(self, event_type: str, body: dict) -> None:
        super().emit_event(event_type, dict(body))
        self.pump()

    def emit_observation(self, record) -> None:
        # 관측 event의 observation_id를 scheduler trigger로 등록한다(반응형 인과성, H1c).
        # slot은 VDETER(=이 connection)가 정하는 **정수**다(L4 방어: client 조작·raw ValueError 차단).
        obs_id = getattr(record, "observation_id", None)
        register = getattr(self._provider, "register_observation_trigger", None)
        if obs_id and callable(register):
            self._slot_counter += 1
            slot = int(self._slot_counter)
            register(obs_id, slot=slot, hw_timestamp_ns=int(slot * _SLOT_SAMPLES))
        super().emit_observation(record)
        self.pump()

    def pump(self) -> None:
        if self._conn_closed or self.engine is None:
            return
        deadline = time.monotonic() + self._pump_timeout
        last_change = time.monotonic()
        last_count = self._msg_count
        while time.monotonic() < deadline:
            state = self.pump_once(timeout=self._poll)
            if state in (EngineState.ABORTED.value, EngineState.CLOSED.value):
                break
            now = time.monotonic()
            if self._msg_count != last_count:
                last_count = self._msg_count
                last_change = now
            elif now - last_change >= self._quiescence:
                break  # child가 조용해짐(더 낼 message 없음)

    def poll_new_results(self) -> list:
        out = list(self._new_results)
        self._new_results.clear()
        return out

    def submitted_action(self, request_id: str):
        return self._submitted.get(request_id)

    def provide_terminal(self, request_id: str, *, action_execution: str, target_effect) -> None:
        self.provide_action_terminal(request_id, action_execution=action_execution,
                                     target_effect=target_effect)
        self.pump()

    def client_idle(self) -> bool:
        if self.engine is None:
            return True
        residual = self.engine.residual_snapshot()
        return int(residual.get("inflight_actions", 0)) == 0 and not self._new_results

    def finish(self) -> None:
        if self.engine is None or self.engine.state != EngineState.RUNNING:
            return
        self._send_all(self.engine.begin_finish(self._clock.monotonic()))
        # on_finish(child) 완료까지 pump(bounded). 완료 못 해도 close가 force_close로 처리한다.
        self.run_until(lambda: self.engine.state != EngineState.FINISHING, timeout=self._pump_timeout)

    def close(self) -> dict:
        infra = "COMPLETED" if self.infrastructure_error is None else "ABORTED"
        report = self.close_workload(infrastructure=infra)
        self._conn_closed = True
        engine = self.engine
        if engine is not None:
            # §13.8: bounded 메모리 버퍼(event_queue·replay_ring) 폐기. inflight_actions는
            # 폐기하지 않는다(실제 ownership 불확실은 그대로 노출).
            try:
                engine._queue.clear()
                engine._queue_bytes = 0
                engine._ring.clear()
                engine._ring_bytes = 0
            except AttributeError:
                pass
        residual = self.residuals()
        out = {
            "child_alive": bool(residual.get("child_alive")),
            "socket_file_exists": bool(residual.get("socket_file_exists")),
            "workload_dir_exists": bool(residual.get("workload_dir_exists")),
            "engine": dict(residual.get("engine") or {}),
            "forced_kill": bool(report.get("forced_kill")),
            "steps": list(report.get("steps", [])),
        }
        # provider 예약·trigger 정리(§10, §13). 소유권 불확실은 cleanup 실패로 노출한다.
        cleanup = getattr(self._provider, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except PocError as error:
                out["error"] = error.code.value
        return out

    @property
    def infrastructure_error(self):
        engine = self.engine
        if engine is not None and engine.fatal_error is not None:
            return engine.fatal_error
        return None

    def armed_reached(self) -> bool:
        return self._armed_reached

    def evidence(self) -> dict:
        base = super().evidence()
        residual = self.residuals() if self.engine is not None else {}
        return {
            "server_state": self.engine.state.value if self.engine is not None else None,
            "connection_epoch": base.get("connection_epoch", 0),
            "engine_evidence": base.get("engine", {}),
            "residual": residual,
            "stdio": base.get("stdio", {}),
        }


__all__ = ("SupervisorConnection",)
