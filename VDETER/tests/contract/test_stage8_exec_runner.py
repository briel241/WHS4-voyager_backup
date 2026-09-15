"""workload 실행 lifecycle 오케스트레이션 시험(실행 명세 §5.1, §9~§11, §13).

`S8-POC-REQ-02/06/07/13/14/15/16/17/19` 관련. LoopbackConnection(fake-clock, socket 없음)으로
결정적으로 검증한다. platform 무관.
"""

from __future__ import annotations

import unittest

from vdeter.poc.execution.lease import MassBLease, MassBRole
from vdeter.poc.execution.runner import (
    WorkloadRunner,
    WorkloadSpec,
    observation_step,
    scenario_started,
    step_started,
)
from vdeter.poc.execution.snapshot import Snapshot
from vdeter.poc.execution.verdict import (
    ActionExecution,
    PocInfrastructure,
    ScenarioVerdict,
    TargetEffect,
)
from vdeter.poc.api.errors import PocErrorCode
from vdeter.poc.api.limits import ACK_MAX_DELAY_S, CALLBACK_ON_EVENT_DEADLINE_S
from vdeter.poc.api.negotiation import ServerCapabilityConfig
from vdeter.poc.api.types import PhysicalChannel, TuningState
from vdeter.poc.testing.client_runner import RecordingCallbacks
from vdeter.poc.testing.fakes import FakeClock, build_manifest, make_observation
from vdeter.poc.testing.execution_fakes import (
    FakeTxExecutor,
    LoopbackConnection,
    ScenarioPriorityProvider,
)

GRACE = CALLBACK_ON_EVENT_DEADLINE_S + ACK_MAX_DELAY_S


def _snap(desc):
    return Snapshot(
        snapshot_dir="/snap", script_path="/snap/p.py", manifest_path="/snap/p.poc.toml",
        script_sha256="ab" * 32, manifest_sha256="cd" * 32, binding_digest="ef" * 32,
        descriptor=desc, interpreter="/usr/bin/python3", interpreter_version="3.12",
        source_script_ino=1, source_manifest_ino=2,
    )


def _run(callbacks, *, manifest=None, provider=None, executor=None, steps=(),
         scenario_verdict=ScenarioVerdict.PASS, server_config=None, auto_ack=True):
    manifest = manifest or build_manifest()
    clock = FakeClock()
    conn = LoopbackConnection(callbacks=callbacks, manifest=manifest, clock=clock,
                              provider=provider or ScenarioPriorityProvider(),
                              server_config=server_config, auto_ack=auto_ack)
    lease = MassBLease()
    runner = WorkloadRunner(conn, lease, run_id="run1", workload_id="w-1",
                            scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                            tx_executor=executor or FakeTxExecutor())
    result = runner.run(WorkloadSpec(steps=tuple(steps), scenario_verdict=scenario_verdict))
    return result, conn, lease


class LifecycleOrderingTest(unittest.TestCase):
    def test_normal_lifecycle_completes(self):
        cb = RecordingCallbacks()
        result, conn, lease = _run(cb, steps=(scenario_started(),))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertTrue(cb.started)
        self.assertTrue(cb.finished)
        self.assertIn("ARMED", result.lifecycle_transitions)
        self.assertIn("LEASE_RELEASED", result.lifecycle_transitions)
        lease.assert_returned()

    def test_transitions_in_order(self):
        result, _, _ = _run(RecordingCallbacks(), steps=(scenario_started(),))
        t = list(result.lifecycle_transitions)
        self.assertLess(t.index("LEASE_ACQUIRED"), t.index("ARMED"))
        self.assertLess(t.index("ARMED"), t.index("WORKLOAD_TERMINAL"))
        self.assertLess(t.index("WORKLOAD_TERMINAL"), t.index("CLOSED"))
        self.assertLess(t.index("CLOSED"), t.index("LEASE_RELEASED"))

    def test_on_start_returning_actions_aborts_before_workload(self):
        # on_start는 action을 제출하지 않는다(§7.3). 반환하면 POC_PROTOCOL_ERROR로 abort.
        cb = RecordingCallbacks(start_actions=["not-allowed"])
        result, conn, lease = _run(cb, steps=(scenario_started(),))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ERROR)
        self.assertEqual(result.infrastructure_error_code, PocErrorCode.POC_PROTOCOL_ERROR.value)
        self.assertEqual(result.attacks_requested, 0)
        lease.assert_returned()


class PreemptiveReactiveTest(unittest.TestCase):
    def test_preemptive_action_on_scenario_started(self):
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC")]
            return []
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act), steps=(scenario_started(),))
        self.assertEqual(result.attacks_requested, 1)
        self.assertEqual(result.attacks[0].trigger, "SCENARIO_STARTED")

    def test_reactive_replay_on_packet_observed(self):
        def act(event, ctx):
            if event.type == "PACKET_OBSERVED":
                return [ctx.replay(target="ROC",
                                   source_observation_id=event.body.get("observation_id"))]
            return []
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act),
                            steps=(scenario_started(), observation_step(obs)))
        self.assertEqual(result.attacks_requested, 1)
        self.assertEqual(result.attacks[0].trigger, "PACKET_OBSERVED")
        self.assertEqual(result.attacks[0].source_observation_id, obs.observation_id)


class TargetChannelMappingTest(unittest.TestCase):
    def test_roc_lower_massa_upper(self):
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC"), ctx.replay(target="MASS-A")]
            return []
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act), steps=(scenario_started(),))
        by_target = {a.target: a.physical_channel for a in result.attacks}
        self.assertEqual(by_target["ROC"], PhysicalChannel.LOWER.value)
        self.assertEqual(by_target["MASS-A"], PhysicalChannel.UPPER.value)


class ActionSerializationTest(unittest.TestCase):
    def test_two_actions_serialized(self):
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC"), ctx.replay(target="MASS-A")]
            return []
        provider = ScenarioPriorityProvider()
        result, conn, _ = _run(RecordingCallbacks(event_action_fn=act),
                               provider=provider, steps=(scenario_started(),))
        self.assertEqual(result.attacks_requested, 2)
        self.assertTrue(all(a.action_execution == ActionExecution.TX_COMPLETED
                            for a in result.attacks))
        # provider는 두 번 호출(각 request_id 정확히 1회, in-flight 1 직렬).
        self.assertEqual(provider.submit_calls, 2)


class ScenarioPriorityTest(unittest.TestCase):
    def test_conflict_rejects_poc_not_scenario(self):
        # 정상 TX가 ROC 채널을 점유 → PoC action은 RADIO_RESOURCE_CONFLICT로 거부.
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC")]
            return []
        provider = ScenarioPriorityProvider(blocked_targets={"ROC"})
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act),
                            provider=provider, steps=(scenario_started(),),
                            scenario_verdict=ScenarioVerdict.PASS)
        # scenario는 그대로 PASS. PoC action만 거부.
        self.assertEqual(result.scenario_verdict, ScenarioVerdict.PASS)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.REJECTED_BEFORE_TX)
        self.assertEqual(result.attacks[0].reason, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT.value)
        self.assertEqual(result.attacks[0].target_effect, TargetEffect.NOT_APPLICABLE)

    def test_rejection_does_not_change_scenario_fail(self):
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC")]
            return []
        provider = ScenarioPriorityProvider(blocked_targets={"ROC"})
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act),
                            provider=provider, steps=(scenario_started(),),
                            scenario_verdict=ScenarioVerdict.FAIL)
        # scenario verdict는 공격 거부와 독립(§11.1).
        self.assertEqual(result.scenario_verdict, ScenarioVerdict.FAIL)


class ForbiddenFieldTest(unittest.TestCase):
    def test_forbidden_rf_field_rejected(self):
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC", payload={"frequency": 2450000000})]
            return []
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act), steps=(scenario_started(),))
        self.assertEqual(result.attacks[0].action_execution, ActionExecution.REJECTED_BEFORE_TX)
        self.assertEqual(result.attacks[0].reason, PocErrorCode.POC_ACTION_SCHEMA_INVALID.value)


class TargetEffectCombosTest(unittest.TestCase):
    def test_forge_rejected_by_target_is_not_infra_failure(self):
        def act(event, ctx):
            if event.type == "PACKET_OBSERVED":
                return [ctx.forge(target="ROC")]
            return []
        executor = FakeTxExecutor(effect_by_kind={
            "FORGE_PACKET": (TargetEffect.REJECTED_BY_TARGET, "bad-signature"),
        })
        manifest = build_manifest(
            requested_capabilities=("observe_packet", "forge_packet"),
            requested_triggers=("SCENARIO_STARTED", "PACKET_OBSERVED"))
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act), manifest=manifest,
                            executor=executor,
                            steps=(scenario_started(), observation_step(obs)))
        a = result.attacks[0]
        self.assertEqual(a.action_execution, ActionExecution.TX_COMPLETED)
        self.assertEqual(a.target_effect, TargetEffect.REJECTED_BY_TARGET)
        self.assertEqual(a.reason, "bad-signature")
        # target 보안 거부는 infra 실패가 아니다(§11.1, §11.2).
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.scenario_verdict, ScenarioVerdict.PASS)

    def test_effect_observed_not_assumed_from_tx(self):
        # 기본 executor는 TX_COMPLETED여도 NO_EFFECT. 전송=성공으로 추정하지 않음(§11.1).
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC")]
            return []
        result, _, _ = _run(RecordingCallbacks(event_action_fn=act), steps=(scenario_started(),))
        self.assertEqual(result.effects_observed, 0)


class NotTunedTest(unittest.TestCase):
    def test_not_tuned_no_event_no_attack_counter_only(self):
        def act(event, ctx):
            if event.type == "PACKET_OBSERVED":  # NOT_TUNED는 이 event를 만들지 않는다
                return [ctx.replay(target="ROC")]
            return []
        obs = make_observation(tuning_state=TuningState.NOT_TUNED,
                               physical_channel=PhysicalChannel.UPPER,
                               source_station="ROC", destination_station="MASS-A")
        result, conn, _ = _run(RecordingCallbacks(event_action_fn=act),
                               steps=(scenario_started(), observation_step(obs)))
        self.assertEqual(result.attacks_requested, 0)
        ev = conn.evidence()["engine_evidence"]
        self.assertEqual(ev["not_tuned_count"], 1)
        self.assertEqual(ev["tuned_observer_count"], 0)


class CallbackErrorTest(unittest.TestCase):
    def test_callback_exception_aborts_no_success(self):
        cb = RecordingCallbacks(raise_on={"PACKET_OBSERVED"})
        obs = make_observation(source_station="ROC", destination_station="MASS-A")
        result, _, lease = _run(cb, steps=(scenario_started(), observation_step(obs)))
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)
        self.assertEqual(result.infrastructure_error_code,
                         PocErrorCode.POC_CALLBACK_EXCEPTION.value)
        # 오류를 성공으로 추정하지 않음: effect 0.
        self.assertEqual(result.effects_observed, 0)
        lease.assert_returned()

    def test_callback_timeout_classified(self):
        # ACK 유예를 넘긴 callback hang은 POC_CALLBACK_TIMEOUT(§11.2). auto_ack off로 모델링.
        cb = RecordingCallbacks()
        manifest = build_manifest()
        clock = FakeClock()
        conn = LoopbackConnection(callbacks=cb, manifest=manifest, clock=clock, auto_ack=False)
        conn.start()
        conn.emit_event("PACKET_OBSERVED", {"observation_id": "obs-x"})
        conn.advance_clock(GRACE + 1.0)
        self.assertIsNotNone(conn.infrastructure_error)
        self.assertEqual(conn.infrastructure_error.code, PocErrorCode.POC_CALLBACK_TIMEOUT)


class _FaultyConnection:
    """지정 지점에서 예외를 던지는 최소 WorkloadConnection(예외 경로 lease 반환 검증)."""

    def __init__(self, *, on_start=False, on_emit=False, on_close=False, on_finish=False,
                 emit_exc=None, close_exc=None, evidence_error=False):
        self._on_start = on_start
        self._on_emit = on_emit
        self._on_close = on_close
        self._on_finish = on_finish
        self._emit_exc = emit_exc          # 특정 예외 인스턴스(KeyboardInterrupt 등)
        self._close_exc = close_exc        # close() 중 인터럽트 주입
        self._evidence_error = evidence_error
        self._armed = False
        self._reached = False
        self.closed = False
        self.close_calls = 0

    def start(self):
        if self._on_start:
            raise RuntimeError("boom start")
        self._armed = True
        self._reached = True

    @property
    def armed(self):
        return self._armed

    @property
    def running(self):
        return self._armed

    def _maybe_emit_raise(self):
        if self._emit_exc is not None:
            raise self._emit_exc
        if self._on_emit:
            raise RuntimeError("boom emit")

    def emit_event(self, event_type, body):
        self._maybe_emit_raise()

    def emit_observation(self, record):
        self._maybe_emit_raise()

    def pump(self):
        pass

    def poll_new_results(self):
        return []

    def submitted_action(self, request_id):
        return None

    def provide_terminal(self, request_id, *, action_execution, target_effect):
        pass

    def client_idle(self):
        return True

    def finish(self):
        if self._on_finish:
            raise RuntimeError("boom finish")

    def close(self):
        self.close_calls += 1
        self.closed = True
        if self._close_exc is not None:
            raise self._close_exc
        if self._on_close:
            raise RuntimeError("boom close")
        return {"engine": {"event_queue": 0, "replay_ring": 0, "inflight_actions": 0}}

    @property
    def infrastructure_error(self):
        return None

    def armed_reached(self):
        return self._reached

    def evidence(self):
        if self._evidence_error:
            raise RuntimeError("boom evidence")
        return {}


class ExceptionCleanupTest(unittest.TestCase):
    """어느 단계에서 예외가 나도 MASS-B lease를 반환하고 fail-closed 결과를 낸다(§13, REQ-19/20/27)."""

    def _run_faulty(self, **fault):
        manifest = build_manifest()
        conn = _FaultyConnection(**fault)
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                                tx_executor=FakeTxExecutor())
        result = runner.run(WorkloadSpec(steps=(scenario_started(),),
                                         scenario_verdict=ScenarioVerdict.PASS))
        return result, lease

    def test_start_exception_returns_lease(self):
        result, lease = self._run_faulty(on_start=True)
        lease.assert_returned()  # POC_ACTOR로 남으면 예외
        self.assertEqual(lease.role, MassBRole.NORMAL)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ERROR)  # ARMED 전
        self.assertIn("EXCEPTION:RuntimeError", result.infrastructure_error_code)

    def test_emit_exception_returns_lease(self):
        result, lease = self._run_faulty(on_emit=True)
        lease.assert_returned()
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)  # ARMED 후

    def test_finish_exception_returns_lease(self):
        result, lease = self._run_faulty(on_finish=True)
        lease.assert_returned()
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)

    def test_close_exception_still_returns_lease(self):
        result, lease = self._run_faulty(on_close=True)
        lease.assert_returned()  # close 실패에도 lease는 강제 정상 복귀
        self.assertEqual(lease.role, MassBRole.NORMAL)

    def _runner_with(self, conn):
        manifest = build_manifest()
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                                tx_executor=FakeTxExecutor())
        return runner, lease

    def test_keyboard_interrupt_reraised_after_cleanup(self):
        # 사용자 Ctrl+C: cleanup(lease 반환·close)은 하되 KeyboardInterrupt는 상위로 재전달.
        conn = _FaultyConnection(emit_exc=KeyboardInterrupt())
        runner, lease = self._runner_with(conn)
        with self.assertRaises(KeyboardInterrupt):
            runner.run(WorkloadSpec(steps=(scenario_started(),),
                                    scenario_verdict=ScenarioVerdict.PASS))
        lease.assert_returned()
        self.assertTrue(conn.closed)  # cleanup은 수행됨

    def test_system_exit_reraised_after_cleanup(self):
        conn = _FaultyConnection(emit_exc=SystemExit(2))
        runner, lease = self._runner_with(conn)
        with self.assertRaises(SystemExit):
            runner.run(WorkloadSpec(steps=(scenario_started(),),
                                    scenario_verdict=ScenarioVerdict.PASS))
        lease.assert_returned()
        self.assertTrue(conn.closed)

    def test_generator_exit_reraised_after_cleanup(self):
        conn = _FaultyConnection(emit_exc=GeneratorExit())
        runner, lease = self._runner_with(conn)
        with self.assertRaises(GeneratorExit):
            runner.run(WorkloadSpec(steps=(scenario_started(),),
                                    scenario_verdict=ScenarioVerdict.PASS))
        lease.assert_returned()
        self.assertTrue(conn.closed)

    def test_close_interrupt_reentry_returns_result(self):
        # close() 중 KeyboardInterrupt → 첫 호출은 인터럽트 재전달·lease 반환, 재진입은 유효 결과.
        conn = _FaultyConnection(close_exc=KeyboardInterrupt())
        runner, lease = self._runner_with(conn)
        with self.assertRaises(KeyboardInterrupt):
            runner.run(WorkloadSpec(steps=(scenario_started(),),
                                    scenario_verdict=ScenarioVerdict.PASS))
        lease.assert_returned()  # close 인터럽트에도 lease는 정상 복귀
        # 재진입: _result가 None이 아니라 유효 결과를 돌려준다(finding 2).
        second = runner._finalize(ScenarioVerdict.PASS, raised=None)
        self.assertIsNotNone(second)
        self.assertEqual(second.poc_infrastructure, PocInfrastructure.ABORTED)
        # close는 한 번만 호출됐다(재진입이 다시 close하지 않음).
        self.assertEqual(conn.close_calls, 1)

    def test_evidence_error_does_not_lose_result(self):
        # _build_result의 evidence 수집이 실패해도 _result가 None으로 남지 않는다.
        conn = _FaultyConnection(evidence_error=True)
        runner, lease = self._runner_with(conn)
        result = runner.run(WorkloadSpec(steps=(scenario_started(),),
                                         scenario_verdict=ScenarioVerdict.PASS))
        self.assertIsNotNone(result)
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
        self.assertEqual(result.evidence["connection"], {"error": "evidence unavailable"})
        lease.assert_returned()

    def test_tx_executor_exception_returns_lease(self):
        # accepted action의 terminal 해석 중 예외 → lease 반환·fail-closed.
        def act(event, ctx):
            if event.type == "SCENARIO_STARTED":
                return [ctx.replay(target="ROC")]
            return []
        def boom_executor(action, result):
            raise RuntimeError("boom executor")
        manifest = build_manifest()
        clock = FakeClock()
        conn = LoopbackConnection(callbacks=RecordingCallbacks(event_action_fn=act),
                                  manifest=manifest, clock=clock,
                                  provider=ScenarioPriorityProvider())
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                                tx_executor=boom_executor)
        result = runner.run(WorkloadSpec(steps=(scenario_started(),),
                                         scenario_verdict=ScenarioVerdict.PASS))
        lease.assert_returned()
        self.assertEqual(result.poc_infrastructure, PocInfrastructure.ABORTED)


class _InterruptConn:
    """지정 lifecycle 지점에서 임의 예외를 던지는 WorkloadConnection(인터럽트 matrix용)."""

    def __init__(self, stage, exc):
        self._stage = stage
        self._exc = exc
        self._armed = False
        self.close_calls = 0
        self.finish_calls = 0

    def _maybe(self, point):
        if self._stage == point:
            raise self._exc

    def start(self):
        self._maybe("start")
        self._armed = True

    @property
    def armed(self):
        return self._armed

    @property
    def running(self):
        return self._armed

    def emit_event(self, event_type, body):
        self._maybe("emit")

    def emit_observation(self, record):
        self._maybe("emit")

    def pump(self):
        pass

    def poll_new_results(self):
        # action 단계 주입용: accepted 결과 한 건을 한 번만 돌려준다.
        if self._stage == "action" and not getattr(self, "_served", False):
            self._served = True
            return [{"request_id": "rid-1", "admission": "accepted",
                     "physical_channel": "lower", "broadcast_parent_id": "p1",
                     "transfer_id": "t1", "error_code": None}]
        return []

    def submitted_action(self, request_id):
        return {"action_kind": "REPLAY_PACKET", "target": "ROC", "schedule": {}, "payload": {}}

    def provide_terminal(self, request_id, *, action_execution, target_effect):
        pass

    def client_idle(self):
        return True

    def finish(self):
        self.finish_calls += 1
        self._maybe("finish")

    def close(self):
        self.close_calls += 1
        # close 단계 주입은 teardown **뒤**에 던져, cleanup이 끝난 뒤의 인터럽트를 모델링한다.
        report = {"engine": {"event_queue": 0, "replay_ring": 0, "inflight_actions": 0}}
        self._maybe("close")
        return report

    @property
    def infrastructure_error(self):
        return None

    def armed_reached(self):
        return self._armed

    def evidence(self):
        return {}


class InterruptMatrixTest(unittest.TestCase):
    """5개 lifecycle 지점 × 3개 BaseException: 재전달·cleanup·결과 기록(§11.2, REQ-19/20/27).

    인터럽트를 성공으로 추정하지 않고(COMPLETED 금지), ARMED 전은 ERROR·이후는 ABORTED로
    기록하며, lease 반환·close 1회·재진입 결과 non-None을 보장한다.
    """

    STAGES = ("start", "emit", "action", "finish", "close")
    INTERRUPTS = (KeyboardInterrupt, SystemExit, GeneratorExit)

    def test_interrupt_matrix(self):
        manifest = build_manifest()
        for stage in self.STAGES:
            for exc_type in self.INTERRUPTS:
                with self.subTest(stage=stage, interrupt=exc_type.__name__):
                    conn = _InterruptConn(stage, exc_type())
                    lease = MassBLease()
                    executor = FakeTxExecutor()
                    if stage == "action":
                        def _boom(action, result, _e=exc_type):
                            raise _e()
                        executor = _boom
                    runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                            scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                                            tx_executor=executor)
                    spec = WorkloadSpec(steps=(scenario_started(),),
                                        scenario_verdict=ScenarioVerdict.PASS)
                    with self.assertRaises(exc_type):
                        runner.run(spec)          # 1) 인터럽트 원형 재전달
                    lease.assert_returned()       # 2) lease 반환
                    self.assertEqual(conn.close_calls, 1)  # 3) close 정확히 1회
                    result = runner._finalize(ScenarioVerdict.PASS, raised=None)
                    self.assertIsNotNone(result)  # 4) 재진입 결과 non-None
                    # 5) 성공으로 추정하지 않는다.
                    self.assertNotEqual(result.poc_infrastructure, PocInfrastructure.COMPLETED)
                    expected = (PocInfrastructure.ERROR if stage == "start"
                                else PocInfrastructure.ABORTED)
                    self.assertEqual(result.poc_infrastructure, expected)
                    self.assertIn(f"EXCEPTION:{exc_type.__name__}",
                                  result.evidence["failure_codes"])
                    self.assertEqual(result.infrastructure_error_code,
                                     PocErrorCode.POC_TX_OUTCOME_UNKNOWN.value if stage == "action"
                                     else f"EXCEPTION:{exc_type.__name__}")
                    # scenario verdict는 인터럽트와 독립(§11.1).
                    self.assertEqual(result.scenario_verdict, ScenarioVerdict.PASS)

    def test_terminal_and_close_run_once_on_reentry(self):
        # 중복 finish/run 재진입이 seam finish/close를 두 번 부르지 않는다(§5.5).
        manifest = build_manifest()
        conn = _InterruptConn("none", RuntimeError("unused"))
        lease = MassBLease()
        runner = WorkloadRunner(conn, lease, run_id="r", workload_id="w",
                                scenario_id="SC-VDE-006", snapshot=_snap(manifest),
                                tx_executor=FakeTxExecutor())
        spec = WorkloadSpec(steps=(scenario_started(),), scenario_verdict=ScenarioVerdict.PASS)
        first = runner.run(spec)
        again = runner.run(spec)
        third = runner.finish(ScenarioVerdict.PASS)
        self.assertIs(first, again)
        self.assertIs(first, third)
        self.assertEqual(conn.finish_calls, 1)
        self.assertEqual(conn.close_calls, 1)
        self.assertEqual(list(first.lifecycle_transitions).count("WORKLOAD_TERMINAL"), 1)
        self.assertEqual([h[0] for h in lease.history], ["ACQUIRE", "RELEASE"])


class DualCapabilityFailClosedTest(unittest.TestCase):
    def test_required_dual_channel_unavailable_fails_closed(self):
        # observe_dual_physical_channel required인데 server 미광고 → 연결 전 fail-closed.
        manifest = build_manifest(
            requested_capabilities=("observe_packet", "observe_dual_physical_channel"),
            required_capabilities=("observe_dual_physical_channel",))
        cb = RecordingCallbacks()
        result, conn, lease = _run(cb, manifest=manifest,
                                   server_config=ServerCapabilityConfig(),
                                   steps=(scenario_started(),))
        self.assertIn(result.poc_infrastructure, (PocInfrastructure.ERROR, PocInfrastructure.ABORTED))
        self.assertEqual(result.infrastructure_error_code,
                         PocErrorCode.POC_CAPABILITY_UNAVAILABLE.value)
        lease.assert_returned()


if __name__ == "__main__":
    unittest.main()
