"""한 PoC workload의 실행 lifecycle 오케스트레이션(실행 명세 §5.1, §9~§11, §13).

순서: `on_start` → `ARMED` → event → action → terminal → `on_finish` → cleanup.

이 runner는 Stage 3 connection seam(:class:`WorkloadConnection`)만 구동하고 다음을 강제한다.

- `on_start()`에서 RF action을 제출하지 않는다(선제 공격도 시작 event 기반; engine이 강제).
- action은 한 개만 in-flight이고 후속 action은 terminal 뒤 직렬화한다(§9, engine 강제).
- 정상 scenario·infrastructure가 PoC action보다 우선한다. 충돌 시 PoC action만
  `RADIO_RESOURCE_CONFLICT`로 거부되며(provider가 강제), 이를 scenario 실패로 재계산하지
  않는다(§10.3, §11.1, `S8-POC-REQ-14/16`).
- scenario verdict·PoC infrastructure·attack outcome을 독립적으로 기록한다(§11, `S8-POC-REQ-15`).
- 오류를 성공으로 추정하지 않고 fail-closed 처리한다(§11.2, `S8-POC-REQ-19`).
- MASS-B `POC_ACTOR` lease를 workload 동안 독점하고 terminal·cleanup에서 반환한다(§4.2).
- RF 활성으로 모델링된 구간에는 per-event 출력·write·flush를 하지 않는다. 상세 보고는
  terminal·quiescence 뒤 renderer가 만든다(§12, `S8-POC-REQ-17`). runner는 결과 model만
  bounded memory에 쌓는다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from ..poc_api.errors import PocError, PocErrorCode, SEVERITY_RANK
from ..poc_api.limits import MAX_WORKLOAD_ACTION_REQUESTS
from ..poc_api.types import ActionKind, ObservationRecord, TARGET_TO_PHYSICAL_CHANNEL
from .connection import WorkloadConnection
from .lease import MassBLease, MassBRole
from .snapshot import Snapshot
from .verdict import (
    ActionExecution,
    AttackRecord,
    PocInfrastructure,
    ScenarioVerdict,
    TargetEffect,
    WorkloadResult,
)

# action 배관 방어적 상한(무한 loop 방지). 실제 상한은 engine의 MAX_WORKLOAD_ACTION_REQUESTS.
_DRAIN_ITER_CAP = 512


@dataclass(frozen=True, slots=True)
class TxResolution:
    """accepted action의 terminal 판정(§11.1). action_execution과 target_effect를 분리."""

    action_execution: ActionExecution
    target_effect: TargetEffect
    reason: str | None = None


#: accepted action을 terminal로 해석하는 fake RF/target 모델(§15: scheduler spy·fake endpoint).
#: (action_body, result_body) -> TxResolution. Stage 5·6에서 production provider가 대체한다.
TxExecutor = Callable[[dict, dict], TxResolution]


@dataclass(frozen=True, slots=True)
class WorkloadStep:
    """workload 중 전달할 한 자극(§8). scenario/step event 또는 관측."""

    kind: str  # "event" | "observation"
    event_type: str | None = None
    body: dict = field(default_factory=dict)
    observation: ObservationRecord | None = None


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    """workload 실행 대본(§15 RF-off). 실물에서는 실제 RF event가 이 자극을 대신한다."""

    steps: tuple = ()
    scenario_verdict: ScenarioVerdict = ScenarioVerdict.PASS


def scenario_started(body: dict | None = None) -> WorkloadStep:
    return WorkloadStep(kind="event", event_type="SCENARIO_STARTED", body=dict(body or {}))


def step_started(name: str) -> WorkloadStep:
    return WorkloadStep(kind="event", event_type="STEP_STARTED", body={"step": name})


def observation_step(record: ObservationRecord) -> WorkloadStep:
    return WorkloadStep(kind="observation", observation=record)


class WorkloadRunner:
    """한 workload를 실행하고 세 독립 결과를 담은 :class:`WorkloadResult`를 만든다."""

    def __init__(
        self,
        connection: WorkloadConnection,
        lease: MassBLease,
        *,
        run_id: str,
        workload_id: str,
        scenario_id: str,
        snapshot: Snapshot,
        tx_executor: TxExecutor,
    ) -> None:
        self._conn = connection
        self._lease = lease
        self._run_id = run_id
        self._workload_id = workload_id
        self._scenario_id = scenario_id
        self._snapshot = snapshot
        self._executor = tx_executor

        self._attacks: list[AttackRecord] = []
        self._processed: dict[str, dict] = {}
        self._uncertain_submissions: dict[str, dict] = {}
        self._transitions: list[str] = []
        self._armed = False
        self._closed_report: dict = {}
        self._lease_acquired = False
        self._result: WorkloadResult | None = None  # 한 번 세워지면 idempotent 반환값
        self._terminal_done = False                  # workload terminal 1회 보장(§5.5)
        self._failure: BaseException | None = None
        self._interrupt: BaseException | None = None
        self._failure_codes: list[str] = []
        self._arm_started = False

    # --- lifecycle ----------------------------------------------------------
    def arm(self) -> bool:
        """lease 독점 → 연결 시작 → ARMED. 실패 시 workload를 시작하지 않는다(§5.1)."""

        try:
            return self._arm_once()
        except BaseException as exc:
            self._record_failure(exc)
            raise

    def _arm_once(self) -> bool:

        if self._arm_started or self._result is not None:
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "runner cannot be armed twice")
        self._arm_started = True
        self._lease.acquire(self._workload_id)
        self._lease_acquired = True
        self._transitions.append("LEASE_ACQUIRED")
        self._conn.start()
        self._armed = self._boolean(self._conn.armed)
        if self._armed:
            self._transitions.append("ARMED")
        if not self._armed:
            error = self._infra_error()
            raise error or PocError(PocErrorCode.POC_PROTOCOL_ERROR, "start returned before ARMED")
        return True

    def deliver(self, step: WorkloadStep) -> None:
        """자극 하나를 전달하고 그로 인한 action을 terminal까지 배관·기록한다(§8~§10)."""

        try:
            self._deliver_step(step)
        except BaseException as exc:
            self._record_failure(exc)
            raise

    def _deliver_step(self, step: WorkloadStep) -> None:

        if self._failure is not None:
            raise self._failure
        if self._result is not None or self._terminal_done or not self._armed:
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "event outside running workload")
        if not self._boolean(self._conn.running):
            raise self._infra_error() or PocError(
                PocErrorCode.POC_CONNECTION_LOST, "connection stopped before event delivery")
        trigger = step.event_type or "PACKET_OBSERVED"
        if step.kind == "observation":
            self._conn.emit_observation(step.observation)
        else:
            self._conn.emit_event(step.event_type, dict(step.body))
        self._conn.pump()
        self._drain_actions(trigger)

    def _drain_actions(self, trigger: str) -> None:
        for _ in range(_DRAIN_ITER_CAP):
            # P11: action 처리 루프는 최대 _DRAIN_ITER_CAP 회 돌며 매 회 pump한다. 느린 child·
            # 포화 큐에서는 이 안에 오래 머무를 수 있으므로, step 경계뿐 아니라 **여기서도**
            # 권위 deadline·중단을 확인한다(상한을 새로 만들지 않고 조회만 한다).
            guard = getattr(self._conn, "_guard", None)
            if guard is not None:
                guard()
            if self._infra_error() is not None:
                return
            results = self._conn.poll_new_results()
            if not isinstance(results, list) or len(results) > MAX_WORKLOAD_ACTION_REQUESTS:
                raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid action result batch")
            if not results and self._boolean(self._conn.client_idle()):
                return
            for result in results:
                if (not isinstance(result, dict)
                        or not isinstance(result.get("request_id"), str)
                        or not result["request_id"]
                        or result.get("admission") not in ("accepted", "rejected")):
                    raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid action result")
                request_id = result.get("request_id")
                if request_id in self._processed:
                    if result != self._processed[request_id]:
                        raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "conflicting action replay")
                    continue
                if len(self._processed) >= MAX_WORKLOAD_ACTION_REQUESTS:
                    raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "action result limit exceeded")
                self._processed[request_id] = dict(result)
                action = self._conn.submitted_action(request_id)
                if not isinstance(action, dict):
                    raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "action binding missing")
                if result.get("admission") == "accepted":
                    # Preserve accepted parent correlation even if the executor or
                    # terminal delivery is interrupted after transmission.
                    index = len(self._attacks)
                    self._attacks.append(self._make_attack(
                        request_id, trigger, action, result,
                        ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                        "terminal not confirmed"))
                    expected_channel = TARGET_TO_PHYSICAL_CHANNEL.get(action.get("target"))
                    if (not isinstance(result.get("broadcast_parent_id"), str)
                            or not result["broadcast_parent_id"]
                            or not isinstance(result.get("transfer_id"), str)
                            or not result["transfer_id"]
                            or expected_channel is None
                            or result.get("physical_channel") != expected_channel.value):
                        raise PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN,
                                       "accepted action correlation is invalid")
                    try:
                        resolution = self._executor(action, result)
                    except BaseException as exc:
                        self._record_failure(exc)
                        self._record_failure(PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN))
                        raise
                    if (not isinstance(resolution, TxResolution)
                            or not isinstance(resolution.action_execution, ActionExecution)
                            or not isinstance(resolution.target_effect, TargetEffect)):
                        raise PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN, "invalid TX terminal")
                    self._attacks[index] = self._make_attack(
                        request_id, trigger, action, result,
                        resolution.action_execution, resolution.target_effect, resolution.reason)
                    if resolution.action_execution == ActionExecution.TX_UNKNOWN:
                        raise PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
                    self._conn.provide_terminal(
                        request_id,
                        action_execution=resolution.action_execution.value,
                        target_effect=(resolution.target_effect.value
                                       if resolution.target_effect is not None else None),
                    )
                else:
                    # RF 전 거부(scheduler·admission). scenario 실패로 재계산하지 않는다(§11.1).
                    self._attacks.append(self._make_attack(
                        request_id, trigger, action, result,
                        ActionExecution.REJECTED_BEFORE_TX, TargetEffect.NOT_APPLICABLE,
                        result.get("error_code"),
                    ))
            self._conn.pump()
        raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "action drain did not become idle")

    @staticmethod
    def _boolean(value) -> bool:
        if type(value) is not bool:
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid lifecycle state")
        return value

    def _make_attack(self, request_id, trigger, action, result, execution, effect, reason):
        return AttackRecord(
            request_id=request_id,
            action_kind=action.get("action_kind", "UNKNOWN"),
            target=action.get("target", "UNKNOWN"),
            trigger=trigger,
            source_observation_id=action.get("source_observation_id"),
            schedule=dict(action.get("schedule") or {}),
            action_execution=execution,
            target_effect=effect,
            physical_channel=result.get("physical_channel"),
            broadcast_parent_id=result.get("broadcast_parent_id"),
            transfer_id=result.get("transfer_id"),
            reason=reason,
        )

    def _run_terminal(self) -> None:
        """workload terminal → on_finish → quiescence(§5.1). 예외는 상위에서 잡아 finalize한다.

        정확히 한 번만 수행한다(§5.5: 상태 전이는 단조롭고 evidence에 정확히 한 번). 중복
        `finish()` 호출이 seam의 finish를 두 번 부르거나 전이를 중복 기록하지 않게 한다.
        """

        if self._terminal_done:
            return
        self._terminal_done = True
        if self._failure is not None:
            return
        if not self._armed:
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "finish before ARMED")
        if self._infra_error() is None:
            if not self._boolean(self._conn.client_idle()):
                raise PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN, "outstanding action at finish")
            self._transitions.append("WORKLOAD_TERMINAL")
            self._conn.finish()
            self._transitions.append("QUIESCENCE")

    def finish(self, scenario_verdict: ScenarioVerdict) -> WorkloadResult:
        """workload terminal → quiescence → on_finish → cleanup → 세 독립 결과 확정(§5.1, §11)."""

        if self._result is not None:
            return self._result  # 재진입: 이미 확정된 결과(중복 terminal/close 금지)
        try:
            self._run_terminal()
        except Exception as exc:  # noqa: BLE001 - 일반 실행 결함만 fail-closed 결과화
            return self._finalize(scenario_verdict, raised=exc)
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit/GeneratorExit: 인터럽트를 **결과에 기록**하고
            # (성공으로 추정 금지, §11.2) cleanup 뒤 원형 그대로 상위로 재전달한다.
            self._finalize(scenario_verdict, raised=exc)
            raise
        return self._finalize(scenario_verdict, raised=None)

    def run(self, spec: WorkloadSpec) -> WorkloadResult:
        """arm → 모든 step 전달 → finish. 편의 진입점(§15 RF-off 대본 실행).

        일반 실행 결함(Exception)은 어느 단계(arm·start·deliver·finish·close)에서 나도 cleanup을
        보장하고 MASS-B lease를 반환하며 fail-closed 결과로 변환한다(§13, `S8-POC-REQ-19/20/27`).
        `KeyboardInterrupt`·`SystemExit`·`GeneratorExit`은 삼키지 않고 cleanup 뒤 상위 coordinator로
        다시 전달한다(사용자 Ctrl+C가 무시되지 않도록).
        """

        if self._result is not None:
            return self._result  # 재진입: 이미 확정된 결과
        try:
            armed = self.arm()
            if armed:
                for step in spec.steps:
                    if self._infra_error() is not None:
                        break
                    self.deliver(step)
            self._run_terminal()
        except Exception as exc:  # noqa: BLE001 - 일반 실행 결함만 fail-closed 결과화
            return self._finalize(spec.scenario_verdict, raised=exc)
        except BaseException as exc:
            # 인터럽트를 결과에 기록한 뒤(ARMED 전 ERROR / 이후 ABORTED) 원형 그대로 재전달.
            self._finalize(spec.scenario_verdict, raised=exc)
            raise
        return self._finalize(spec.scenario_verdict, raised=None)

    def abort(self, scenario_verdict: ScenarioVerdict, *, cause: BaseException | None = None) -> WorkloadResult:
        """cleanup-only 종료(N1/N2): step 전달·신규 action 제출·FINISH 없이 cleanup만 수행한다.

        `finish()`/`run()`과 달리 `_run_terminal`을 부르지 않으므로 FINISH·scenario event·
        provider.submit이 발생하지 않는다(취소·인터럽트·arm 실패의 안전한 정리 경로). `cause`를
        실패로 기록해 결과를 ERROR/ABORTED로 확정하고(성공 합성 금지), 이미 제출된 action은 신규
        제출 없이 terminal·소유권만 추적한다(`_harvest_pending`). 인터럽트는 여기서 재전달하지
        않고 결과를 돌려준다 — 원형 재전달은 caller가 관장한다(cleanup 오류로 원인을 덮지 않도록)."""
        if self._result is not None:
            return self._result
        self._terminal_done = True  # FINISH·terminal 억제(신규 event/action 없음)
        if cause is not None:
            self._record_failure(cause)
        return self._finalize(scenario_verdict, raised=None, reraise_interrupt=False)

    def _finalize(self, scenario_verdict: ScenarioVerdict,
                  *, raised: BaseException | None,
                  reraise_interrupt: bool = True) -> WorkloadResult:
        """cleanup·lease 반환·결과 조립을 정확히 한 번(idempotent) 수행한다(§13).

        재진입 안전: `_result`가 세워지면 그 값을 돌려준다. cleanup 시작 전에 evidence 없는
        **최소 결과**를 먼저 확정해, close/lease-release가 인터럽트(KeyboardInterrupt 등)로
        중단돼도 재진입 시 `_result`가 None으로 남지 않게 한다(그 뒤 전체 결과로 덮어씀).
        lease는 close가 어떻게 끝나든 반드시 정상 소유권으로 반환하고, 인터럽트는 cleanup·
        결과 저장 뒤 상위로 재전달한다. `reraise_interrupt=False`(abort 경로)면 재전달을 caller에
        맡기고 결과만 돌려준다.
        """

        if self._result is not None:
            return self._result

        # Result construction reads cached state only. All external queries are guarded
        # below, so neither a broken query nor malformed evidence can bypass cleanup.
        if raised is not None:
            self._record_failure(raised)
        self._result = self._build_result(scenario_verdict, provisional=True)
        self._collect(self._infra_error, None)
        reached = self._collect(lambda: self._boolean(self._conn.armed_reached()), self._armed)
        self._armed = self._armed or reached
        if self._failure is not None and self._armed:
            self._collect(self._harvest_pending, None)
        try:
            self._closed_report = self._collect(
                lambda: self._checked_close(), {"error": "cleanup unconfirmed"})
            if "error" not in self._closed_report:
                self._transitions.append("CLOSED")
            else:
                self._transitions.append("CLEANUP_UNCONFIRMED")
        finally:
            # acquire may take ownership and then be interrupted before the local
            # success flag is assigned. Recover only this workload's actual holder.
            if self._lease_acquired or self._lease.holder == self._workload_id:
                try:
                    self._lease.release(self._workload_id)
                    self._lease.assert_returned()
                    self._transitions.append("LEASE_RELEASED")
                except BaseException as exc:
                    self._record_failure(exc)
                    # This lease is a local state model, not proof of radio cleanup.
                    # Never overwrite another workload's ownership.
                    if self._lease.holder == self._workload_id:
                        self._lease.role = MassBRole.NORMAL
                        self._lease.holder = None
                        self._transitions.append("LEASE_FORCE_RELEASED")
                    self._record_failure(PocError(PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN))

        evidence_dict = {
            "connection": self._collect(self._conn.evidence,
                                        {"error": "evidence unavailable"}, diagnostic=True),
            "cleanup": dict(self._closed_report),
            "lease": self._collect(self._lease.as_evidence, {}, diagnostic=True),
            "snapshot": self._collect(self._snapshot.as_evidence, {}, diagnostic=True),
        }
        self._collect(self._infra_error, None)  # close may have reported a new failure
        evidence_dict["failure_codes"] = list(self._failure_codes)
        evidence_dict["uncertain_submissions"] = copy.deepcopy(list(self._uncertain_submissions.values()))
        self._result = self._build_result(scenario_verdict, evidence=evidence_dict)
        if self._interrupt is not None and reraise_interrupt:
            raise self._interrupt
        return self._result

    # --- 결과 조립 ----------------------------------------------------------
    def _harvest_pending(self) -> None:
        """Preserve accepted results after an abort without submitting any new TX."""
        pending = self._conn.poll_new_results()
        if not isinstance(pending, list) or len(pending) > MAX_WORKLOAD_ACTION_REQUESTS:
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid pending result batch")
        recorded = {attack.request_id for attack in self._attacks}
        for result in list(self._processed.values()) + pending:
            if (not isinstance(result, dict) or not isinstance(result.get("request_id"), str)
                    or not result["request_id"]
                    or result.get("admission") not in ("accepted", "rejected")):
                self._record_failure(PocError(PocErrorCode.POC_PROTOCOL_ERROR))
                continue
            request_id = result["request_id"]
            if request_id in recorded:
                continue
            if len(recorded) >= MAX_WORKLOAD_ACTION_REQUESTS:
                self._record_failure(PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN))
                break
            accepted = result["admission"] == "accepted"
            if accepted:
                self._record_failure(PocError(PocErrorCode.POC_TX_OUTCOME_UNKNOWN))
            self._attacks.append(self._make_attack(
                request_id, "not recorded", {}, result,
                ActionExecution.TX_UNKNOWN if accepted else ActionExecution.REJECTED_BEFORE_TX,
                TargetEffect.INDETERMINATE if accepted else TargetEffect.NOT_APPLICABLE,
                "terminal not confirmed" if accepted else result.get("error_code")))
            recorded.add(request_id)

    def _infra_error(self):
        """Live query: failure is never converted to 'no error'."""
        error = self._conn.infrastructure_error
        if error is not None:
            if not isinstance(error, PocError):
                raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid infrastructure error")
            self._record_failure(error)
            self._capture_uncertain_submission(error)
        return error

    def _capture_uncertain_submission(self, error: PocError) -> None:
        """Retain a server-bound failed attempt without inventing accepted admission.

        This is separate from ACTION_RESULT replay: a provider can have an unknown
        physical outcome before that message exists. Repeated error queries must
        neither duplicate the attack nor cause an executor/terminal callback.
        """
        if error.code != PocErrorCode.POC_TX_OUTCOME_UNKNOWN:
            return
        attempt = error.detail.get("action_attempt")
        if not isinstance(attempt, dict) or attempt.get("admission") != "unknown":
            return
        request_id = attempt.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in self._uncertain_submissions:
            return
        if len(self._uncertain_submissions) >= MAX_WORKLOAD_ACTION_REQUESTS:
            return  # already SESSION_INHIBIT; retain the bounded records we have
        target = attempt.get("target")
        channel = TARGET_TO_PHYSICAL_CHANNEL.get(target) if isinstance(target, str) else None
        if (channel is None or attempt.get("physical_channel") != channel.value
                or attempt.get("action_kind") not in tuple(kind.value for kind in ActionKind)
                or not isinstance(attempt.get("schedule"), dict)):
            return
        # Copy only the server-defined metadata; never arbitrary provider detail/payload.
        captured = {key: copy.deepcopy(attempt.get(key)) for key in (
            "request_id", "action_kind", "target", "physical_channel",
            "source_observation_id", "schedule", "action_digest", "admission")}
        for key in ("broadcast_parent_id", "transfer_id"):
            value = attempt.get(key)
            captured[key] = value if isinstance(value, str) and value else None
        self._uncertain_submissions[request_id] = captured
        if not any(a.request_id == request_id for a in self._attacks):
            if len(self._attacks) >= MAX_WORKLOAD_ACTION_REQUESTS:
                return
            self._attacks.append(self._make_attack(
                request_id, "not recorded", captured, captured,
                ActionExecution.TX_UNKNOWN, TargetEffect.INDETERMINATE,
                "provider outcome unconfirmed before accepted ACTION_RESULT"))

    def _collect(self, fn, fallback, *, diagnostic=False):
        """Continue cleanup after a failure, retaining interrupts for rethrow."""
        try:
            value = fn()
            if diagnostic and not isinstance(value, dict):
                raise ValueError("invalid evidence mapping")
            return value
        except BaseException as exc:
            if not diagnostic or not isinstance(exc, Exception):
                self._record_failure(exc)
            return fallback

    @staticmethod
    def _error_code(error):
        return error.code.value if isinstance(error, PocError) else f"EXCEPTION:{type(error).__name__}"

    def _record_failure(self, error: BaseException) -> None:
        code = self._error_code(error)
        if code not in self._failure_codes and len(self._failure_codes) < 32:
            self._failure_codes.append(code)
        if not isinstance(error, Exception) and self._interrupt is None:
            self._interrupt = error
        def rank(exc):
            return SEVERITY_RANK[exc.severity] if isinstance(exc, PocError) else 2
        if self._failure is None or rank(error) > rank(self._failure):
            self._failure = error

    def _checked_close(self) -> dict:
        report = self._conn.close()
        if not isinstance(report, dict):
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid cleanup report")
        if report.get("error"):
            raise PocError(PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN, "cleanup reported failure")
        for key in ("child_alive", "socket_file_exists", "workload_dir_exists"):
            if key in report and (type(report[key]) is not bool or report[key]):
                raise PocError(PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN, "cleanup residue")
        engine = report.get("engine")
        if not isinstance(engine, dict):
            raise PocError(PocErrorCode.POC_PROTOCOL_ERROR, "invalid cleanup counters")
        for key in ("event_queue", "replay_ring", "inflight_actions"):
            if type(engine.get(key)) is not int or engine[key] != 0:
                raise PocError(PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN, "cleanup residue")
        return report

    def _build_result(self, scenario_verdict: ScenarioVerdict,
                       *, evidence: dict | None = None,
                       provisional: bool = False) -> WorkloadResult:
        failure = self._failure
        infrastructure = (PocInfrastructure.ABORTED if self._armed else PocInfrastructure.ERROR)
        if failure is None and not provisional:
            infrastructure = PocInfrastructure.COMPLETED
        error_code = self._error_code(failure) if failure is not None else None
        desc = self._snapshot.descriptor
        return WorkloadResult(
            run_id=self._run_id,
            workload_id=self._workload_id,
            scenario_id=self._scenario_id,
            poc_id=desc.poc_id,
            poc_version=desc.version,
            attacker_endpoint=desc.attacker_endpoint,
            script_sha256=self._snapshot.script_sha256,
            manifest_sha256=self._snapshot.manifest_sha256,
            binding_digest=self._snapshot.binding_digest,
            interpreter=self._snapshot.interpreter,
            scenario_verdict=scenario_verdict,
            poc_infrastructure=infrastructure,
            attacks=tuple(self._attacks),
            infrastructure_error_code=error_code,
            lifecycle_transitions=tuple(self._transitions),
            evidence=evidence or {},
        )


__all__ = (
    "TxResolution",
    "TxExecutor",
    "WorkloadStep",
    "WorkloadSpec",
    "scenario_started",
    "step_started",
    "observation_step",
    "WorkloadRunner",
)
