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

from dataclasses import dataclass, field
from typing import Callable

from ..poc_api.errors import PocError
from ..poc_api.types import ObservationRecord
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
        self._processed: set[str] = set()
        self._transitions: list[str] = []
        self._armed = False
        self._closed_report: dict = {}
        self._lease_acquired = False
        self._finalized = False
        self._result: WorkloadResult | None = None

    # --- lifecycle ----------------------------------------------------------
    def arm(self) -> bool:
        """lease 독점 → 연결 시작 → ARMED. 실패 시 workload를 시작하지 않는다(§5.1)."""

        self._lease.acquire(self._workload_id)
        self._lease_acquired = True
        self._transitions.append("LEASE_ACQUIRED")
        self._conn.start()
        self._armed = self._conn.armed
        if self._armed:
            self._transitions.append("ARMED")
        return self._armed

    def deliver(self, step: WorkloadStep) -> None:
        """자극 하나를 전달하고 그로 인한 action을 terminal까지 배관·기록한다(§8~§10)."""

        if not self._conn.running:
            return
        trigger = step.event_type or "PACKET_OBSERVED"
        if step.kind == "observation":
            self._conn.emit_observation(step.observation)
        else:
            self._conn.emit_event(step.event_type, dict(step.body))
        self._conn.pump()
        self._drain_actions(trigger)

    def _drain_actions(self, trigger: str) -> None:
        for _ in range(_DRAIN_ITER_CAP):
            if self._conn.infrastructure_error is not None:
                return
            results = self._conn.poll_new_results()
            fresh = [r for r in results if r.get("request_id") not in self._processed]
            if not fresh and self._conn.client_idle():
                return
            for result in fresh:
                request_id = result.get("request_id")
                self._processed.add(request_id)
                action = self._conn.submitted_action(request_id) or {}
                if result.get("admission") == "accepted":
                    resolution = self._executor(action, result)
                    self._conn.provide_terminal(
                        request_id,
                        action_execution=resolution.action_execution.value,
                        target_effect=(resolution.target_effect.value
                                       if resolution.target_effect is not None else None),
                    )
                    self._attacks.append(self._make_attack(
                        request_id, trigger, action, result,
                        resolution.action_execution, resolution.target_effect,
                        resolution.reason,
                    ))
                else:
                    # RF 전 거부(scheduler·admission). scenario 실패로 재계산하지 않는다(§11.1).
                    self._attacks.append(self._make_attack(
                        request_id, trigger, action, result,
                        ActionExecution.REJECTED_BEFORE_TX, TargetEffect.NOT_APPLICABLE,
                        result.get("error_code"),
                    ))
            self._conn.pump()

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
        """workload terminal → on_finish → quiescence(§5.1). 예외는 상위에서 잡아 finalize한다."""

        if self._armed and self._infra_error() is None:
            self._transitions.append("WORKLOAD_TERMINAL")
            self._conn.finish()
            self._transitions.append("QUIESCENCE")

    def finish(self, scenario_verdict: ScenarioVerdict) -> WorkloadResult:
        """workload terminal → quiescence → on_finish → cleanup → 세 독립 결과 확정(§5.1, §11)."""

        raised: BaseException | None = None
        try:
            self._run_terminal()
        except BaseException as exc:  # noqa: BLE001 - 어떤 예외에서도 자원·lease를 반환
            raised = exc
        return self._finalize(scenario_verdict, raised=raised)

    def run(self, spec: WorkloadSpec) -> WorkloadResult:
        """arm → 모든 step 전달 → finish. 편의 진입점(§15 RF-off 대본 실행).

        어느 단계(arm·start·deliver·finish·close)에서 예외가 나도 cleanup을 보장하고
        MASS-B lease를 반환한다(§13, `S8-POC-REQ-20/27`, fail-closed).
        """

        raised: BaseException | None = None
        try:
            armed = self.arm()
            if armed:
                for step in spec.steps:
                    if self._infra_error() is not None:
                        break
                    self.deliver(step)
            self._run_terminal()
        except BaseException as exc:  # noqa: BLE001 - infra 예외 fail-closed
            raised = exc
        return self._finalize(spec.scenario_verdict, raised=raised)

    def _finalize(self, scenario_verdict: ScenarioVerdict,
                  *, raised: BaseException | None) -> WorkloadResult:
        """cleanup·lease 반환·결과 조립을 정확히 한 번(idempotent) 수행한다(§13).

        connection.close()나 lease.release()가 실패해도 MASS-B lease를 **반드시** 정상
        소유권으로 되돌린다(잔존 0). 이미 finalize됐으면 저장된 결과를 돌려준다.
        """

        if self._finalized:
            return self._result
        self._finalized = True

        close_error: BaseException | None = None
        try:
            self._closed_report = self._conn.close()
        except BaseException as exc:  # noqa: BLE001 - close 실패도 fail-closed로 흡수
            close_error = exc
            self._closed_report = {"error": repr(exc), "forced_kill": True}
        if "CLOSED" not in self._transitions:
            self._transitions.append("CLOSED")

        # 모든 경로(정상·오류·start 실패·강제 종료·close 실패)에서 lease를 반환한다.
        if self._lease_acquired:
            try:
                self._lease.release(self._workload_id)
                self._transitions.append("LEASE_RELEASED")
            except BaseException:  # noqa: BLE001 - 최후 방어: 강제 정상 복귀(잔존 0 보장)
                self._lease.role = MassBRole.NORMAL
                self._lease.holder = None
                self._transitions.append("LEASE_FORCE_RELEASED")

        self._result = self._build_result(scenario_verdict, raised=raised or close_error)
        return self._result

    # --- 결과 조립 ----------------------------------------------------------
    def _infra_error(self):
        """connection.infrastructure_error를 안전하게 읽는다(broken conn도 흡수)."""
        try:
            return self._conn.infrastructure_error
        except BaseException:  # noqa: BLE001
            return None

    def _armed_reached_safe(self) -> bool:
        try:
            return self._conn.armed_reached()
        except BaseException:  # noqa: BLE001
            return self._armed

    def _build_result(self, scenario_verdict: ScenarioVerdict,
                      *, raised: BaseException | None = None) -> WorkloadResult:
        conn_error = self._infra_error()
        if conn_error is not None:
            error_code = conn_error.code.value if isinstance(conn_error, PocError) else repr(conn_error)
            # ARMED 전 오류는 workload 미시작(ERROR), 이후 오류는 ABORTED(§11.2).
            infrastructure = (PocInfrastructure.ABORTED if self._armed_reached_safe()
                              else PocInfrastructure.ERROR)
        elif raised is not None:
            # connection이 code로 남기지 못한 Python 예외(start/deliver/close 등)도
            # 성공으로 추정하지 않고 fail-closed 처리한다(§11.2, `S8-POC-REQ-19`).
            error_code = (raised.code.value if isinstance(raised, PocError)
                          else f"EXCEPTION:{type(raised).__name__}")
            infrastructure = (PocInfrastructure.ABORTED if self._armed_reached_safe()
                              else PocInfrastructure.ERROR)
        else:
            infrastructure = PocInfrastructure.COMPLETED
            error_code = None

        desc = self._snapshot.descriptor
        evidence = {
            "connection": self._conn.evidence(),
            "cleanup": dict(self._closed_report),
            "lease": self._lease.as_evidence(),
            "snapshot": self._snapshot.as_evidence(),
        }
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
            evidence=evidence,
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
