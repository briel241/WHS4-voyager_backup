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

import json
import time

from .._supervisor.engine import EngineState
from .._supervisor.supervisor import PocSupervisor
from ..api import protocol
from ..api.codec import decode_wire_packet
from ..api.errors import PocError, PocErrorCode
from ..api.limits import (
    MAX_ACTION_DESCRIPTOR_BYTES,
    MAX_WORKLOAD_ACTION_REQUESTS,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_TOTAL_DEADLINE_S,
)

#: 반응형 trigger slot을 유도할 slot grid(shared_medium NextSafeSlotScheduler와 동일 규약).
_SLOT_SAMPLES = 8192

#: R11: 통합 tee 보존 상한. 엔진 action cache와 같은 기존 명세 상한(MAX_WORKLOAD_ACTION_REQUESTS)을
#: entry 상한으로, action descriptor 상한×그 개수를 byte 예산으로 쓴다(새 정책값 아님).
#: accepted·unknown correlation은 이 상한과 무관하게 절대 eviction하지 않는다.
_TEE_MAX_ENTRIES = MAX_WORKLOAD_ACTION_REQUESTS
_TEE_MAX_BYTES = MAX_ACTION_DESCRIPTOR_BYTES * MAX_WORKLOAD_ACTION_REQUESTS


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
        #: P11: 블로킹 대기 중에도 권위 deadline·운영자 중단을 확인하는 훅. 상한을 새로 만들지
        #: 않고 기존 모델을 조회만 한다. None이면 기존 동작(고정 pump timeout)과 같다.
        self._guard = None
        self._submitted: dict = {}      # request_id -> action body(§9.4), 유계(R11)
        self._results: dict = {}        # request_id -> ACTION_RESULT body, 유계(R11)
        self._terminals: dict = {}      # request_id -> ACTION_TERMINAL body
        self._new_results: list = []    # since last poll, 유계(R11)
        self._accepted: list = []       # accepted request_id (provider evidence 조립용)
        self._provider_evidence = None  # cleanup 전에 snapshot한 Stage 5 provider evidence
        self._armed_reached = False
        self._conn_closed = False
        self._msg_count = 0             # outbound+inbound activity counter(quiescence 판정)
        self._slot_counter = 0          # 반응형 trigger용 단조 정수 slot
        # R11: tee overflow 계수(임의 유실을 감추지 않고 요약으로 노출). byte 예산 누적.
        self._tee_bytes = 0
        self._pending_rids: set = set()  # N3: _new_results에 아직 poll되지 않은 request_id(중복 큐 방지)
        self._tee_overflow = {"submitted_overflow": 0, "submitted_duplicate": 0,
                              "results_overflow": 0, "new_results_overflow": 0,
                              "results_coalesced": 0, "contradictory_result": 0}

    # --- outbound/inbound tee ----------------------------------------------
    def _send_all(self, messages) -> None:
        messages = list(messages)
        for message in messages:
            self._observe_outbound(message)
        super()._send_all(messages)

    @staticmethod
    def _approx_bytes(body) -> int:
        try:
            return len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            return len(repr(body).encode("utf-8"))

    def _observe_outbound(self, message: dict) -> None:
        self._msg_count += 1
        mtype = message.get("type")
        body = message.get("body") or {}
        if mtype == protocol.ACTION_RESULT:
            rid = body.get("request_id")
            admission = body.get("admission")
            must_keep = admission in ("accepted", "unknown")  # correlation 보존 대상(R11)
            record = dict(body)
            prior = self._results.get(rid)
            # N3: 같은 rid에 **모순된** 결과(다른 body)가 오면 성공으로 덮지 않고 fail-closed로 센다.
            # 정상 replay는 engine cache가 동일 body를 돌려주므로 이 경로는 방어적이다.
            if prior is not None and prior != record:
                self._tee_overflow["contradictory_result"] += 1
                return
            # _results: rid별 1개(dict). accepted/unknown은 상한과 무관히 correlation을 보존하고,
            # rejected 신규 rid는 entry·byte 예산 안에서만 보존한다. 재emit(동일 body)은 멱등 갱신.
            if prior is not None:
                self._results[rid] = record
            elif must_keep or self._tee_admit(record):
                self._results[rid] = record
            else:
                self._tee_overflow["results_overflow"] += 1
            # N3: _new_results는 **rid별 최대 1개 pending**으로 coalesce한다. 같은 accepted/unknown을
            # 1,000회 replay해도 pending이 이미 있으면 중복 큐 보관하지 않는다(멱등성·correlation은 유지,
            # 상한 회피 아님). accepted/unknown 신규 rid는 correlation을 위해 상한과 무관히 1회 전달한다.
            if rid in self._pending_rids:
                self._tee_overflow["results_coalesced"] += 1
            elif must_keep or len(self._new_results) < _TEE_MAX_ENTRIES:
                self._new_results.append(record)
                self._pending_rids.add(rid)
            else:
                self._tee_overflow["new_results_overflow"] += 1
            if admission == "accepted" and isinstance(rid, str) and rid not in self._accepted:
                self._accepted.append(rid)
        elif mtype == protocol.ACTION_TERMINAL:
            self._terminals[body.get("request_id")] = dict(body)
        elif mtype == protocol.ARMED:
            self._armed_reached = True

    def _tee_admit(self, body) -> bool:
        """rejected 기록을 entry·byte 예산 안에서만 보존한다(초과는 호출자가 counter 처리)."""
        size = self._approx_bytes(body)
        if len(self._results) >= _TEE_MAX_ENTRIES or self._tee_bytes + size > _TEE_MAX_BYTES:
            return False
        self._tee_bytes += size
        return True

    def _handle_inbound(self, data: bytes) -> None:
        self._msg_count += 1
        try:
            # decode_wire_packet은 raw dict를 돌려준다(parse_envelope는 객체 -> 여기선 dict 접근).
            obj = decode_wire_packet(data)
            if isinstance(obj, dict) and obj.get("type") == protocol.ACTION_SUBMIT:
                body = obj.get("body") or {}
                rid = body.get("request_id")
                if isinstance(rid, str):
                    self._tee_submit(rid, dict(body.get("action") or {}))
        except Exception:  # noqa: BLE001 - tee 실패가 실제 처리를 막지 않는다(super가 fail-closed)
            pass
        super()._handle_inbound(data)

    def _tee_submit(self, rid: str, action: dict) -> None:
        """inbound action metadata를 유계 보존한다(R11).

        - 같은 rid의 재제출(변조 포함)은 기존 정상 metadata를 **덮어쓰지 않는다**(first-writer-wins).
          digest 불일치·변조는 engine이 별도 판정한다(§9.4). tee는 first를 신뢰 근거로 남긴다.
        - entry·byte 예산 초과 신규 rid는 저장하지 않고 overflow counter로 요약한다.
        """
        if rid in self._submitted:
            self._tee_overflow["submitted_duplicate"] += 1
            return
        size = self._approx_bytes(action)
        if len(self._submitted) >= _TEE_MAX_ENTRIES or self._tee_bytes + size > _TEE_MAX_BYTES:
            self._tee_overflow["submitted_overflow"] += 1
            return
        self._tee_bytes += size
        self._submitted[rid] = action

    def _prune_resolved_tee(self) -> None:
        """cleanup 시 resolved 기록만 폐기하고 unresolved correlation은 보존한다(R11, §13.8).

        폐기 가능: terminal을 받은 rejected 결과·그 submit metadata. 보존: accepted/unknown
        admission, terminal 미수신 accepted(= 실 ownership 불확실), _accepted 목록.
        """
        keep_rids = set(self._accepted)
        for rid, body in list(self._results.items()):
            admission = body.get("admission")
            if admission in ("accepted", "unknown"):
                keep_rids.add(rid)
                continue
            if rid in self._terminals:  # resolved rejected → 폐기 가능
                self._results.pop(rid, None)
                self._submitted.pop(rid, None)
        # submit metadata는 결과가 남아있거나 보존 대상인 rid만 유지.
        for rid in list(self._submitted):
            if rid not in self._results and rid not in keep_rids:
                self._submitted.pop(rid, None)

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
        # 반응형 schedule 기준(slot·hardware timestamp)은 **VDETER가 소유한 실제 관측의
        # device hardware timestamp**에서 유도한다(단순 event counter를 slot/hw-time으로 쓰지 않는다).
        # slot은 정수이고 client가 조작할 수 없다(L4 방어). record가 hw timestamp를 주지 않는
        # 경우에만(예: NOT_TUNED·합성 없는 최소 관측) 단조 counter로 fallback한다.
        obs_id = getattr(record, "observation_id", None)
        register = getattr(self._provider, "register_observation_trigger", None)
        if obs_id and callable(register):
            hw_ts = getattr(record, "device_hw_timestamp_ns", None)
            if isinstance(hw_ts, int) and hw_ts > 0:
                # slot 좌표 환산은 scheduler가 소유한다(P03: 연결 layer가 slot 단위를 가정하지 않는다).
                # 실 MAC 결속 scheduler는 실 device slot 길이(26.666… ms)를, RF-off stub은 8192을 쓴다.
                convert = getattr(self._provider, "slot_for_hw_ns", None)
                slot = convert(hw_ts) if callable(convert) else None
                if slot is None:
                    slot = int(hw_ts // _SLOT_SAMPLES)      # scheduler 미제공 시 stub 좌표 fallback
                register(obs_id, slot=int(slot), hw_timestamp_ns=int(hw_ts))
            else:
                self._slot_counter += 1                     # hw-time 없는 관측 fallback(합성 아님)
                slot = int(self._slot_counter)
                register(obs_id, slot=slot, hw_timestamp_ns=int(slot * _SLOT_SAMPLES))
        super().emit_observation(record)
        self.pump()

    def set_deadline_guard(self, guard) -> None:
        """블로킹 대기 루프에서 호출할 권위 deadline/중단 검사 훅을 건다(P11).

        `pump`는 child가 조용해질 때까지 최대 `pump_timeout`을 기다린다. child가 느리거나 응답하지
        않으면 그 고정 상한까지 붙잡혀 있고, 그 동안 권위 workload/absolute deadline이 만료돼도
        아무도 눈치채지 못했다 — guard 호출이 step 경계에만 있었기 때문이다. 이제 대기 루프
        **안에서도** 확인한다.
        """
        self._guard = guard

    def pump(self) -> None:
        if self._conn_closed or self.engine is None:
            return
        deadline = time.monotonic() + self._pump_timeout
        last_change = time.monotonic()
        last_count = self._msg_count
        while time.monotonic() < deadline:
            if self._guard is not None:
                # 만료면 여기서 예외가 올라가고, workload는 남은 step 없이 cleanup-only로 끝난다.
                self._guard()
            # R10: workload 실행 루프(emit_event/observation → pump)에서도 base run_until처럼
            # 같은 PID 재접속을 수락한다(§12.2). 이 배선이 없으면 실행 중 단절은 child가 살아
            # 재접속하려 해도 parent가 받아주지 않아 복구가 불가능했다.
            if self.engine.state == EngineState.RECONNECTING and not self._registered:
                self.reconnect_accept()  # 성공 시 HELLO는 다음 pump_once가 읽는다.
                state = self.engine.state.value
            else:
                state = self.pump_once(timeout=self._poll)
            if state in (EngineState.ABORTED.value, EngineState.CLOSED.value):
                break
            now = time.monotonic()
            if self._msg_count != last_count:
                last_count = self._msg_count
                last_change = now
            elif (self.engine.state != EngineState.RECONNECTING
                  and now - last_change >= self._quiescence):
                break  # child가 조용해짐(더 낼 message 없음). 재접속 대기 중이면 계속 편다.

    def poll_new_results(self) -> list:
        out = list(self._new_results)
        self._new_results.clear()
        self._pending_rids.clear()  # N3: drain 뒤 pending 초기화(다음 상태 갱신은 다시 전달 가능)
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
        # on_finish(child) 완료까지 pump. R12: on_finish 예산(2초)은 engine의 FINISHING deadline이
        # 직접 강제한다(초과 → POC_CALLBACK_TIMEOUT, ABORTED). 아래 timeout은 그 deadline을 연장하는
        # 값이 아니라 serve loop의 상한일 뿐이다(예산 초과는 pump timeout 이전에 확정된다).
        self.run_until(lambda: self.engine.state != EngineState.FINISHING, timeout=self._pump_timeout)

    def close(self) -> dict:
        """workload close: protocol finish/close 실패·인터럽트와 무관하게 OS·provider 정리를 완주한다.

        R8: `close_workload`(child 종료·socket teardown)가 protocol 송신 실패나 인터럽트로 끝나도
        provider evidence snapshot(R5)과 provider cleanup을 **반드시** 수행한다. protocol close
        실패는 memory 정리만으로 완료로 위장하지 않고 `POC_RADIO_OWNERSHIP_UNCERTAIN`(SESSION_INHIBIT)로
        surface한다(§14.2). 인터럽트는 정리를 마친 뒤 원형 재전달한다.
        """
        infra = "COMPLETED" if self.infrastructure_error is None else "ABORTED"
        interrupt: BaseException | None = None
        report: dict = {}
        try:
            report = self.close_workload(infrastructure=infra)
        except (KeyboardInterrupt, SystemExit) as exc:
            interrupt = exc  # close_workload가 가능한 정리를 마친 뒤 올린 인터럽트 — 재전달 예정
            report = {"protocol_error": self._error_code(exc)}
        except Exception as exc:  # noqa: BLE001 - close_workload는 이미 유계 정리; 방어적 보존
            report = {"protocol_error": self._error_code(exc)}
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
        # R11: cleanup 시 resolved(terminal 받은 rejected) tee 기록은 폐기하고, unresolved
        # (accepted·unknown correlation)는 보존한다.
        self._prune_resolved_tee()
        residual = self.residuals()
        out = {
            "child_alive": bool(residual.get("child_alive")),
            "socket_file_exists": bool(residual.get("socket_file_exists")),
            "workload_dir_exists": bool(residual.get("workload_dir_exists")),
            "engine": dict(residual.get("engine") or {}),
            "forced_kill": bool(report.get("forced_kill")),
            "steps": list(report.get("steps", [])),
        }
        if report.get("cleanup_errors"):
            out["cleanup_errors"] = list(report["cleanup_errors"])
        # R5: cleanup 전에 provider가 이미 보존한 bounded evidence(readiness·composite·correlation·
        # model_boundary)를 snapshot한다. 없는 실측값을 합성하지 않고 이미 존재하는 정보만 수집한다.
        self._provider_evidence = self._snapshot_provider_evidence()
        # provider 예약·trigger 정리(§10, §13). 소유권 불확실은 cleanup 실패로 노출한다.
        cleanup = getattr(self._provider, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except PocError as error:
                out["error"] = error.code.value
            except Exception as exc:  # noqa: BLE001 - provider 정리 실패도 삼키지 않고 surface
                out["error"] = self._error_code(exc)
        # R8: protocol finish/close 실패는 RF 소유권 불확실(SESSION_INHIBIT)로 노출한다.
        # OS 자원이 정리됐어도(잔존 0) memory 정리만으로 RF 반환 완료를 선언하지 않는다.
        proto_err = report.get("protocol_error")
        if proto_err and not out.get("error"):
            out["error"] = PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN.value
            out["close_protocol_error"] = proto_err
        if interrupt is not None:
            raise interrupt  # 정리를 마친 뒤 원래 인터럽트 재전달
        return out

    @staticmethod
    def _error_code(error: BaseException) -> str:
        return error.code.value if isinstance(error, PocError) else f"EXCEPTION:{type(error).__name__}"

    def _snapshot_provider_evidence(self):
        """accepted parent들의 Stage 5 provider evidence를 cleanup 전에 bounded snapshot한다.

        parent가 없으면(방사 0) None. build_evidence/parent_for가 없으면 None. 없는 RX event·
        terminal·실측값을 합성하지 않는다 — 이미 provider/관측자에 있는 정보만 조립한다.
        """
        build = getattr(self._provider, "build_evidence", None)
        parent_for = getattr(self._provider, "parent_for", None)
        if not callable(build) or not callable(parent_for):
            return None
        try:
            seen = []
            parents = []
            for rid in self._accepted:
                parent = parent_for(rid)
                if parent is not None and parent.parent_id not in seen:
                    seen.append(parent.parent_id)
                    parents.append(parent)
            if not parents:
                return None
            from ..shared_medium.observation import observe_scene
            scene = observe_scene(parents)
            evidence = build(parents, scene)
            as_json = getattr(evidence, "as_json_value", None)
            return as_json() if callable(as_json) else evidence
        except Exception:  # noqa: BLE001 - evidence 수집 실패가 cleanup/판정을 막지 않는다
            return None

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
            # R5: cleanup 전 snapshot한 Stage 5 provider evidence(readiness/composite/model_boundary).
            "provider_evidence": self._provider_evidence,
            # R11: 통합 tee 유계 상태·overflow 요약(임의 유실을 감추지 않는다).
            "tee": {
                "submitted": len(self._submitted),
                "results": len(self._results),
                "new_results": len(self._new_results),
                "bytes": self._tee_bytes,
                "max_entries": _TEE_MAX_ENTRIES,
                "max_bytes": _TEE_MAX_BYTES,
                "overflow": dict(self._tee_overflow),
            },
        }

    @property
    def tee_overflow(self) -> dict:
        return dict(self._tee_overflow)

    @property
    def provider_evidence(self):
        return self._provider_evidence


__all__ = ("SupervisorConnection",)
