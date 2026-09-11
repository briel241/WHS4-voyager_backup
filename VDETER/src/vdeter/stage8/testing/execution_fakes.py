"""Stage 4 실행 layer 시험용 test double(명세 §15, §18: real RF 대신 fake).

이 module은 Stage 4 소유의 **신규** test-support 파일이다. 공유 `testing/__init__.py`를
수정하지 않으므로 Stage 5 branch와 export 충돌이 없다. 시험은 이 module을 직접 import한다.

담는 것:

- :class:`LoopbackConnection` — Stage 3 `SupervisorEngine`(server) + `SdkEngine`(client)을
  socket 없이 in-process·fake-clock으로 결합한 :class:`WorkloadConnection` 구현. runner의
  실행 로직을 결정적으로 검증한다.
- :class:`ScenarioPriorityProvider` — scheduler spy. 정상 scenario TX가 점유한 target은
  `POC_RADIO_RESOURCE_CONFLICT`(REQUEST_REJECTED)로 거부해 "정상 우선"을 모델링(§10.3).
- :class:`FakeTxExecutor` — accepted action을 terminal로 해석하는 fake RF/target 모델(§11.1).
- catalog/snapshot fixture writer.

private `_supervisor.engine` import는 server 측 test-support의 관례다(server_harness와 동일).
production surface(`poc_api`)가 아니다.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field

from .._supervisor.engine import EngineState, SupervisorEngine
from ..poc_api import protocol
from ..poc_api.codec import decode_wire_packet, encode_wire_packet
from ..poc_api.errors import PocError, PocErrorCode
from ..poc_api.limits import ACK_MAX_DELAY_S
from ..poc_api.messages import parse_envelope
from ..poc_api.negotiation import ServerCapabilityConfig
from ..poc_api.sdk import ClientState, PocContext, SdkEngine
from ..poc_api.types import (
    PhysicalChannel,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
    PocManifestDescriptor,
)
from .fakes import FakeClock, build_manifest, hello_payload
from .server_harness import DEFAULT_TOKEN, DEFAULT_TOKEN_HEX

from ..execution.runner import TxExecutor, TxResolution
from ..execution.verdict import ActionExecution, TargetEffect

_PUMP_ITER_CAP = 4096


class ScenarioPriorityProvider:
    """scheduler spy(§10.3, `S8-POC-REQ-14`). 정상 scenario TX가 우선한다.

    `blocked_targets`에 든 target으로의 PoC action은 `POC_RADIO_RESOURCE_CONFLICT`
    (severity REQUEST_REJECTED)로 거부한다 → engine이 해당 action만 거부하고 PoC·scenario는
    계속한다. 그 외에는 canonical channel·parent·transfer를 유도해 accepted로 만든다.
    """

    def __init__(self, *, blocked_targets=frozenset()) -> None:
        self.blocked_targets = set(blocked_targets)
        self.submit_calls = 0
        self.submitted_request_ids: list = []
        self._seq = 0

    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action, *, request_id: str) -> ProviderSubmission:
        self.submit_calls += 1
        self.submitted_request_ids.append(request_id)
        if action.target in self.blocked_targets:
            raise PocError(PocErrorCode.POC_RADIO_RESOURCE_CONFLICT,
                           "정상 scenario TX와 자원 충돌")
        self._seq += 1
        channel = self.derive_physical_channel(action.target)
        return ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=f"parent-{self._seq:04d}",
            transfer_id=f"tx-{self._seq:04d}",
        )


@dataclass
class FakeTxExecutor:
    """accepted action을 terminal로 해석하는 fake RF/target 모델(§11.1).

    기본은 TX_COMPLETED + NO_EFFECT. `effect_by_kind`로 action_kind별 target effect를
    지정한다(예: FORGE_PACKET → REJECTED_BY_TARGET 'bad-signature'). action이 실제
    전송됐다는 이유로 EFFECT_OBSERVED로 추정하지 않는다(§11.1).
    """

    default_execution: ActionExecution = ActionExecution.TX_COMPLETED
    default_effect: TargetEffect = TargetEffect.NO_EFFECT
    default_reason: str | None = None
    effect_by_kind: dict = field(default_factory=dict)
    execution_by_kind: dict = field(default_factory=dict)
    calls: int = 0

    def __call__(self, action: dict, result: dict) -> TxResolution:
        self.calls += 1
        kind = action.get("action_kind")
        execution = self.execution_by_kind.get(kind, self.default_execution)
        effect, reason = self.effect_by_kind.get(
            kind, (self.default_effect, self.default_reason)
        )
        return TxResolution(action_execution=execution, target_effect=effect, reason=reason)


class LoopbackConnection:
    """SupervisorEngine + SdkEngine을 socket 없이 fake-clock으로 결합(§18).

    envelope는 실제 wire codec(encode/decode)을 한 번 통과시켜 직렬화 경계를 흉내 낸다.
    ACK는 각 pump 끝에서 flush해 replay ring 잔존을 0으로 만든다(cleanup 검증용).
    """

    def __init__(
        self,
        *,
        callbacks,
        manifest: PocManifestDescriptor,
        clock: FakeClock | None = None,
        provider=None,
        server_config: ServerCapabilityConfig | None = None,
        session_id: str = "s-exec",
        workload_id: str = "w-1",
        run_id: str = "r-1",
        scenario_id: str = "SC-VDE-006",
        token: bytes = DEFAULT_TOKEN,
        auto_ack: bool = True,
    ) -> None:
        self.clock = clock or FakeClock()
        self.auto_ack = auto_ack
        self._manifest = manifest
        self._provider = provider or ScenarioPriorityProvider()
        self._server_config = server_config or ServerCapabilityConfig()
        self._token = token
        self.session_id = session_id
        self.workload_id = workload_id
        self.run_id = run_id
        self.scenario_id = scenario_id

        self.server = SupervisorEngine(
            session_id=session_id,
            workload_id=workload_id,
            run_id=run_id,
            scenario_id=scenario_id,
            expected_token=token,
            manifest=manifest,
            server_config=self._server_config,
            provider=self._provider,
        )
        self.client = SdkEngine(callbacks, PocContext(session_id, workload_id, scenario_id))

        self._submitted: dict = {}      # request_id -> action body
        self._results: dict = {}        # request_id -> ACTION_RESULT body
        self._terminals: dict = {}      # request_id -> ACTION_TERMINAL body
        self._new_results: list = []    # since last poll
        self._armed_reached = False
        self._closed = False

    # --- routing ------------------------------------------------------------
    def _wire(self, msg: dict):
        # 모든 message는 **실제 wire codec**(canonical, float 금지)을 통과한다. R13 전에는 timeout
        # ERROR의 float detail 때문에 CodecError를 deep-copy로 우회했으나, 그 우회는 실 wire가 동작한다는
        # 증거가 아니었다. 이제 engine이 ms 정수 detail만 내므로 우회 없이 인코딩 실패는 그대로 드러낸다.
        return parse_envelope(decode_wire_packet(encode_wire_packet(msg)))

    def _route(self, origin: str, messages: list) -> None:
        """origin('client'|'server')이 낸 message를 상대에 전달하고 cascade한다."""

        queue = deque((origin, m) for m in messages)
        steps = 0
        while queue:
            steps += 1
            if steps > _PUMP_ITER_CAP:
                raise RuntimeError("loopback route iteration cap 초과")
            src, msg = queue.popleft()
            self._record(msg)
            parsed = self._wire(msg)
            now = self.clock.monotonic()
            if src == "client":
                out = self.server.receive(parsed, now)
                queue.extend(("server", m) for m in out)
            else:
                out = self.client.receive(parsed, now)
                queue.extend(("client", m) for m in out)

    def _record(self, msg: dict) -> None:
        mtype = msg.get("type")
        body = msg.get("body") or {}
        if mtype == protocol.ACTION_SUBMIT:
            self._submitted[body.get("request_id")] = dict(body.get("action") or {})
        elif mtype == protocol.ACTION_RESULT:
            rid = body.get("request_id")
            self._results[rid] = dict(body)
            self._new_results.append(dict(body))
        elif mtype == protocol.ACTION_TERMINAL:
            self._terminals[body.get("request_id")] = dict(body)
        elif mtype == protocol.ARMED:
            self._armed_reached = True

    def _flush_ack(self) -> None:
        # ACK를 flush해 replay ring 잔존을 0으로(cleanup 검증). 실제 session clock은 안 바꾼다.
        if not self.auto_ack:
            return
        ack_now = self.clock.monotonic() + ACK_MAX_DELAY_S + 0.001
        self._route("client", self.client.tick(ack_now))

    def advance_clock(self, dt: float) -> None:
        """fake clock을 진행시키고 server deadline tick을 돌린다(callback hang·grace 시험용)."""

        self.clock.advance(dt)
        now = self.clock.monotonic()
        self._route("server", self.server.tick(now))

    # --- WorkloadConnection 구현 --------------------------------------------
    def start(self) -> None:
        self.server.start_handshake(self.clock.monotonic())
        payload = hello_payload(DEFAULT_TOKEN_HEX if self._token == DEFAULT_TOKEN
                                else self._token.hex(), self.run_id, self._manifest)
        self._route("client", [self.client.build_hello(payload, connect_kind="initial")])
        if self.server.state == EngineState.RUNNING:
            self._armed_reached = True

    @property
    def armed(self) -> bool:
        return self._armed_reached and self.infrastructure_error is None and not self._closed

    @property
    def running(self) -> bool:
        return (self.server.state == EngineState.RUNNING
                and self.infrastructure_error is None and not self._closed)

    def emit_event(self, event_type: str, body: dict) -> None:
        self.server.enqueue_event(event_type, dict(body), self.clock.monotonic())
        self.pump()

    def emit_observation(self, record) -> None:
        self.server.enqueue_observation(record, self.clock.monotonic())
        self.pump()

    def pump(self) -> None:
        for _ in range(_PUMP_ITER_CAP):
            now = self.clock.monotonic()
            before = (len(self._new_results), len(self._terminals),
                      self.server._next_sequence, len(self.client._pending_descriptors))
            if self.server.state == EngineState.RUNNING:
                self._route("server", self.server.drain(now))
            self._route("server", self.server.tick(now))
            self._route("client", self.client.tick(now))
            self._flush_ack()
            after = (len(self._new_results), len(self._terminals),
                     self.server._next_sequence, len(self.client._pending_descriptors))
            if before == after:
                break

    def poll_new_results(self) -> list:
        out = list(self._new_results)
        self._new_results.clear()
        return out

    def submitted_action(self, request_id: str):
        return self._submitted.get(request_id)

    def provide_terminal(self, request_id: str, *, action_execution: str,
                         target_effect) -> None:
        now = self.clock.monotonic()
        out = self.server.provide_action_terminal(
            request_id, action_execution=action_execution,
            target_effect=target_effect, now=now)
        self._route("server", out)
        self.pump()

    def client_idle(self) -> bool:
        return (not self.client._pending_descriptors
                and self.client._inflight_request_id is None
                and not self.client.unresolved)

    def finish(self) -> None:
        if self.server.state != EngineState.RUNNING:
            return
        self._flush_ack()
        self._route("server", self.server.begin_finish(self.clock.monotonic()))
        self.pump()

    def close(self) -> dict:
        before = self.server.residual_snapshot()
        # Dispose memory-only delivery state on close, as a real supervisor does.
        # Outstanding RF ownership is not inferred away: keep inflight actions.
        self.server._queue.clear()
        self.server._queue_bytes = 0
        self.server._ring.clear()
        self.server._ring_bytes = 0
        self.client._pending_descriptors.clear()
        self.server.mark_closed()
        self._closed = True
        residual = self.server.residual_snapshot()
        return {
            "engine": residual,
            "before_close": before,
            "child_alive": False,
            "socket_file_exists": False,
            "workload_dir_exists": False,
            "forced_kill": False,
            "steps": ["FINISH", "CLOSE"] if not residual["inflight_actions"] else ["FORCE_CLOSE"],
        }

    @property
    def infrastructure_error(self):
        if self.server.fatal_error is not None:
            return self.server.fatal_error
        return self.client.aborted_error

    def armed_reached(self) -> bool:
        return self._armed_reached

    def evidence(self) -> dict:
        return {
            "server_state": self.server.state.value,
            "client_state": self.client.state.value,
            "connection_epoch": self.server.connection_epoch,
            "engine_evidence": dict(self.server.evidence),
            "residual": self.server.residual_snapshot(),
        }


# --- catalog / snapshot fixture writer --------------------------------------

_DEFAULT_SCRIPT = b"""class Poc:
    def on_start(self, context):
        return []

    def on_event(self, event, context):
        return []

    def on_finish(self, context):
        return None
"""


def poc_toml(
    *,
    poc_id: str = "replay-attack",
    version: str = "1.0.0",
    entrypoint: str | None = None,
    script_basename: str = "replay_attack.py",
    compatible_scenarios=("SC-VDE-006",),
    attacker_endpoint: str = "MASS-B",
    allowed_targets=("ROC", "MASS-A"),
    capabilities=("observe_packet", "replay_packet"),
    triggers=("SCENARIO_STARTED", "PACKET_OBSERVED"),
    required_capabilities=(),
    max_actions_requested: int = 10,
    extra: str = "",
) -> str:
    """유효 .poc.toml 텍스트를 만든다. extra로 잘못된 field를 주입해 negative 시험."""

    ep = entrypoint or f"{script_basename}:Poc"

    def arr(items):
        return "[" + ", ".join(f'"{i}"' for i in items) + "]"

    lines = [
        'schema = "vdeter.stage8.poc-manifest.v1"',
        f'poc_id = "{poc_id}"',
        f'version = "{version}"',
        f'entrypoint = "{ep}"',
        f'compatible_scenarios = {arr(compatible_scenarios)}',
        f'attacker_endpoint = "{attacker_endpoint}"',
        f'allowed_targets = {arr(allowed_targets)}',
        f'capabilities = {arr(capabilities)}',
        f'triggers = {arr(triggers)}',
        f'required_capabilities = {arr(required_capabilities)}',
        f'max_actions_requested = {max_actions_requested}',
    ]
    text = "\n".join(lines) + "\n"
    if extra:
        text += extra + "\n"
    return text


def write_poc_package(
    poc_dir: str,
    base: str,
    *,
    script: bytes | None = None,
    manifest_text: str | None = None,
    poc_id: str | None = None,
    **manifest_kwargs,
) -> tuple:
    """`<base>.py`와 `<base>.poc.toml`을 쓴다. 경로 tuple 반환(§6.2)."""

    script_name = base + ".py"
    manifest_name = base + ".poc.toml"
    script_path = os.path.join(poc_dir, script_name)
    manifest_path = os.path.join(poc_dir, manifest_name)
    with open(script_path, "wb") as fh:
        fh.write(script if script is not None else _DEFAULT_SCRIPT)
    if manifest_text is None:
        manifest_text = poc_toml(
            poc_id=poc_id or base.replace("_", "-"),
            script_basename=script_name,
            **manifest_kwargs,
        )
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(manifest_text)
    return script_path, manifest_path


def make_manifest_descriptor(**kwargs) -> PocManifestDescriptor:
    """engine/loopback에 넣을 검증된 manifest descriptor(build_manifest wrapper)."""

    return build_manifest(**kwargs)


class ResultWriteError(Exception):
    """evidence fixture 기록 실패(fail-closed)."""


def write_result_fixture(result, path: str) -> str:
    """**시험 전용** evidence 파일 fixture writer(production writer 아님).

    Stage 4는 production evidence writer를 두지 않는다. evidence를 쓰는 주체는 기존 runtime의
    evidence pipeline이며 Stage 6 통합에서 연결된다. 이 helper는 read-only 경계
    (`execution.render.render_results_file`, §12.3)를 검증할 fixture를 만들기 위한 것이다.

    fixture여도 "확정 evidence는 불변"이라는 전제를 시험이 실제로 확인할 수 있도록 동일한
    안전 속성을 갖는다: 기존 대상·symlink 거부(확정 1회), short write 검사, 임시 파일에 쓰고
    `os.link`로 원자적 확정(중간 실패 시 대상 경로에 부분 JSON 없음), 임시 파일 정리.
    """

    payload = (json.dumps(result.as_json_value(), ensure_ascii=False,
                          sort_keys=True, indent=2) + "\n").encode("utf-8")
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    if os.path.lexists(target):
        raise ResultWriteError(
            f"이미 확정된 evidence를 덮어쓸 수 없습니다: {os.path.basename(target)}")
    fd, tmp = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=directory)
    try:
        try:
            view = memoryview(payload)
            total = 0
            while total < len(view):
                written = os.write(fd, view[total:])
                if written <= 0:
                    raise ResultWriteError("evidence write가 진행되지 않음(short write)")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, target)  # 원자적 확정: 대상 존재 시 FileExistsError
    except FileExistsError:
        raise ResultWriteError(
            f"이미 확정된 evidence를 덮어쓸 수 없습니다: {os.path.basename(target)}")
    except OSError as exc:
        raise ResultWriteError(f"evidence 확정 실패({exc.__class__.__name__})")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return target


__all__ = (
    "ResultWriteError",
    "write_result_fixture",
    "ScenarioPriorityProvider",
    "FakeTxExecutor",
    "LoopbackConnection",
    "poc_toml",
    "write_poc_package",
    "make_manifest_descriptor",
    "DEFAULT_TOKEN",
    "DEFAULT_TOKEN_HEX",
)
