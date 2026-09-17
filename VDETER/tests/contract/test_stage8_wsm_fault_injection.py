"""Stage 5 shared-medium: 병합 전 동결용 fault-injection 전수 검증.

모든 외부 seam(scheduler·arbiter.reserve·arbiter.release·rx_readiness·waveform factory·
radio sink.prepare_group·commit_group·terminal)이 실패한다고 가정하고 상태 전이와 자원 소유권을
적대적으로 검사한다. 검증 불변:
- raw Python 예외(ValueError/TypeError/KeyError/RuntimeError 등)는 public 경계(submit/resolve_outcome/
  cleanup) 밖으로 나가지 않는다. BaseException(KeyboardInterrupt/SystemExit)만 예외로 그대로 전파하되
  자원 누수는 없다.
- atomic fan-out에서 partial RF commit을 성공으로 만들지 않는다(commit 불명확 → OUTCOME_UNKNOWN).
- 오류·불명확 결과를 성공으로 추정하지 않는다.
- pre-fire 거부(scheduler·reserve·readiness·staging·build)는 예약을 반환하고 방사 0.
- cleanup 뒤 provider tracking(reservation/parent/cache/outcome) 잔존 0. release 실패 자원의 식별자는
  unresolved_ownership에 보존하고 SESSION_INHIBIT로 승격한다(거짓 잔존 0 주장 금지).
"""

from __future__ import annotations

import unittest

from vdeter.poc.api.errors import PocError, PocErrorCode, Severity
from vdeter.poc.api.types import ActionDescriptor, ActionKind
from vdeter.poc.shared_medium import testing as TD
from vdeter.poc.shared_medium.radiation import ChildTerminal, ParentOutcome, ReadinessDecision
from vdeter.poc.shared_medium.provider import NextSafeSlotScheduler, SharedMediumActionProvider


def _action(target="ROC", **kw):
    return ActionDescriptor(
        kind=ActionKind.REPLAY_PACKET, target=target,
        payload=kw.pop("payload", {"packet_hex": "48454c4c4f"}),
        schedule=kw.pop("schedule", {"kind": "NEXT_SAFE_SLOT"}), **kw
    )


# --- 구성 가능한 fault double ------------------------------------------------
class FaultScheduler:
    def __init__(self, *, exc=None, bad_result=False, current_slot=0, lead_slots=1):
        self._inner = NextSafeSlotScheduler(current_slot=current_slot, lead_slots=lead_slots)
        self._exc = exc
        self._bad = bad_result

    def resolve(self, action, *, request_id):
        if self._exc is not None:
            raise self._exc
        if self._bad:
            return "not-a-scheduled-slot"
        return self._inner.resolve(action, request_id=request_id)

    def register_trigger(self, *a, **k):
        self._inner.register_trigger(*a, **k)

    def clear_triggers(self):
        self._inner.clear_triggers()

    @property
    def trigger_count(self):
        return self._inner.trigger_count


class FaultArbiter(TD.FakeArbiter):
    def __init__(self, *, reserve_exc=None, reserve_false=False, release_exc=None, normal_reserved=()):
        super().__init__(normal_reserved=normal_reserved)
        self._reserve_exc = reserve_exc
        self._reserve_false = reserve_false
        self._release_exc = release_exc

    def reserve(self, *, resource_key, owner):
        if self._reserve_exc is not None:
            raise self._reserve_exc
        if self._reserve_false:
            return False
        return super().reserve(resource_key=resource_key, owner=owner)

    def release(self, *, resource_key, owner):
        if self._release_exc is not None:
            raise self._release_exc
        return super().release(resource_key=resource_key, owner=owner)


class FaultReadiness:
    def __init__(self, *, exc=None, ready=True):
        self._exc = exc
        self._ready = ready

    def window_ready(self, request):
        if self._exc is not None:
            raise self._exc
        return ReadinessDecision(ready=self._ready, reason="fault-double")


class FaultFactory:
    def __init__(self, *, exc=None):
        self._exc = exc
        self._inner = TD.CountingWaveformFactory()

    def encode(self, *, source_station, channel, payload):
        if self._exc is not None:
            raise self._exc
        return self._inner.encode(source_station=source_station, channel=channel, payload=payload)


class FaultSink(TD.CountingRadioSink):
    def __init__(self, *, prepare_exc=None, commit_exc=None, commit_partial_then=None):
        super().__init__()
        self._prepare_exc = prepare_exc
        self._commit_exc = commit_exc
        self._partial = commit_partial_then

    def prepare_group(self, *, group_id, children):
        if self._prepare_exc is not None:
            raise self._prepare_exc
        return super().prepare_group(group_id=group_id, children=children)

    def commit_group(self, admission):
        if self._partial is not None:
            self.commit_calls += 1
            self.committed_child_ids.append(admission.children[0].child_id)  # 한쪽만 방사 후 실패
            raise self._partial
        if self._commit_exc is not None:
            self.commit_calls += 1
            raise self._commit_exc
        return super().commit_group(admission)


def _provider(*, scheduler=None, arbiter=None, readiness=None, factory=None, sink=None):
    return SharedMediumActionProvider(
        radio_sink=sink or TD.CountingRadioSink(),
        rx_readiness=readiness or TD.ReadyReadiness(),
        waveform_factory=factory or TD.CountingWaveformFactory(),
        arbiter=arbiter if arbiter is not None else TD.FakeArbiter(),
        scheduler=scheduler or NextSafeSlotScheduler(current_slot=0, lead_slots=1),
    )


def _poc_holds(arbiter):
    return [k for k, v in arbiter.owner_by_key.items() if v == "POC"]


# --- Exception at each seam -> PocError, pre-fire no leak --------------------
class SeamExceptionTest(unittest.TestCase):
    def _run(self, *, scheduler=None, arbiter=None, readiness=None, factory=None, sink=None):
        arb = arbiter if arbiter is not None else FaultArbiter()
        prov = _provider(scheduler=scheduler, arbiter=arb, readiness=readiness, factory=factory, sink=sink)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        return prov, arb, ctx.exception

    def test_scheduler_exception(self):
        prov, arb, err = self._run(scheduler=FaultScheduler(exc=RuntimeError("sched boom")))
        self.assertEqual(err.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)
        self.assertEqual(_poc_holds(arb), [])

    def test_reserve_exception_is_ownership_uncertain(self):
        # reserve 예외는 자원 확보 여부가 불확실 → conflict가 아니라 OWNERSHIP_UNCERTAIN + 식별자 보존.
        prov, arb, err = self._run(arbiter=FaultArbiter(reserve_exc=RuntimeError("reserve boom")))
        self.assertEqual(err.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        self.assertEqual(prov._reservations, {})
        self.assertIn("lower:slot-1", prov.unresolved_ownership())
        self.assertIn("lower:slot-1", err.detail["unresolved_ownership"])

    def test_reserve_holds_then_raises_tracks_unresolved(self):
        # 실제로 잡은 뒤 예외를 내는 경우: 소유권이 실제로 남으므로 반드시 unresolved로 추적.
        class ReserveHoldThenRaise(TD.FakeArbiter):
            def reserve(self, *, resource_key, owner):
                self.owner_by_key[resource_key] = owner
                raise RuntimeError("boom after holding")

        prov, arb, err = self._run(arbiter=ReserveHoldThenRaise())
        self.assertEqual(err.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        self.assertIn("lower:slot-1", prov.unresolved_ownership())

    def test_readiness_exception(self):
        prov, arb, err = self._run(readiness=FaultReadiness(exc=RuntimeError("rdy boom")))
        self.assertEqual(err.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertEqual(_poc_holds(arb), [])          # 방사 전 거부 → 예약 반환
        self.assertIsNone(prov.parent_for("r"))

    def test_waveform_factory_exception(self):
        prov, arb, err = self._run(factory=FaultFactory(exc=RuntimeError("factory boom")))
        self.assertEqual(err.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertEqual(_poc_holds(arb), [])
        self.assertIsNone(prov.parent_for("r"))

    def test_prepare_group_exception(self):
        sink = FaultSink(prepare_exc=RuntimeError("prepare boom"))
        prov, arb, err = self._run(sink=sink)
        self.assertEqual(err.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        self.assertEqual(_poc_holds(arb), [])
        self.assertEqual(sink.submit_calls, 0)          # 방사 0

    def test_commit_group_exception_is_outcome_unknown_not_success(self):
        sink = FaultSink(commit_exc=RuntimeError("commit boom"))
        arb = FaultArbiter()
        prov = _provider(sink=sink, arbiter=arb)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
        self.assertEqual(ctx.exception.severity, Severity.SESSION_INHIBIT)
        # commit 불명확: 성공 아님, correlation·예약 보존(cleanup 회수).
        self.assertIsNotNone(prov.parent_for("r"))
        self.assertNotEqual(prov._broadcast.outcome_of(prov.parent_for("r").parent_id), ParentOutcome.COMPLETED)
        prov.cleanup()
        self.assertEqual(prov.active_group_count, 0)
        self.assertEqual(prov._reservations, {})


# --- BaseException at each seam -> propagate, no leak -----------------------
class SeamBaseExceptionTest(unittest.TestCase):
    def _expect_baseexc(self, **kw):
        arb = kw.pop("arbiter", None) or FaultArbiter()
        prov = _provider(arbiter=arb, **kw)
        with self.assertRaises(KeyboardInterrupt):
            prov.submit(_action(), request_id="r")
        # 자원 누수 0: reserve 성공 후 seam이면 release, reserve 전이면 미보유.
        self.assertEqual(_poc_holds(arb), [])
        self.assertEqual(prov._reservations, {})

    def test_scheduler_baseexception(self):
        self._expect_baseexc(scheduler=FaultScheduler(exc=KeyboardInterrupt()))

    def test_reserve_baseexception(self):
        self._expect_baseexc(arbiter=FaultArbiter(reserve_exc=KeyboardInterrupt()))

    def test_readiness_baseexception(self):
        self._expect_baseexc(readiness=FaultReadiness(exc=KeyboardInterrupt()))

    def test_waveform_factory_baseexception(self):
        self._expect_baseexc(factory=FaultFactory(exc=KeyboardInterrupt()))


class CommitBaseExceptionTest(unittest.TestCase):
    """commit 진입 후 BaseException은 pre-fire 취소가 아니라 TX outcome unknown이다.

    인터럽트를 재전달하되 correlation·reservation·OUTCOME_UNKNOWN 상태를 보존하고, 부분 방사를
    잃지 않는다. 이후 cleanup으로만 회수한다.
    """

    def test_commit_baseexception_preserves_outcome_unknown(self):
        class CommitFireThenKB(TD.CountingRadioSink):
            def commit_group(self, admission):
                self.commit_calls += 1
                self.committed_child_ids.append(admission.children[0].child_id)  # 한쪽 방사
                raise KeyboardInterrupt("ctrl-c mid-commit")

        sink = CommitFireThenKB()
        arb = FaultArbiter()
        prov = _provider(sink=sink, arbiter=arb)
        with self.assertRaises(KeyboardInterrupt):
            prov.submit(_action(), request_id="r")
        parent = prov.parent_for("r")
        self.assertIsNotNone(parent)                              # correlation 보존
        self.assertIn("r", prov._reservations)                   # reservation 보존
        self.assertEqual(prov._broadcast.outcome_of(parent.parent_id), ParentOutcome.OUTCOME_UNKNOWN)
        self.assertEqual(len(sink.committed_child_ids), 1)       # 부분 방사 사실 보존
        # cleanup으로만 회수(정상 arbiter이므로 잔존 0).
        prov.cleanup()
        self.assertEqual(prov.active_group_count, 0)
        self.assertEqual(prov._reservations, {})


# --- reject / timeout / malformed-result ------------------------------------
class SeamRejectAndMalformedTest(unittest.TestCase):
    def test_reserve_false_is_conflict(self):
        arb = FaultArbiter(reserve_false=True)
        prov = _provider(arbiter=arb)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)
        self.assertEqual(_poc_holds(arb), [])

    def test_readiness_not_ready_is_conflict(self):
        prov = _provider(readiness=FaultReadiness(ready=False))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_RESOURCE_CONFLICT)

    def test_scheduler_timeout_exception_is_pocerror(self):
        prov = _provider(scheduler=FaultScheduler(exc=TimeoutError("slow")))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)

    def test_scheduler_bad_result_is_pocerror(self):
        prov = _provider(scheduler=FaultScheduler(bad_result=True))
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_ACTION_SCHEMA_INVALID)

    def test_waveform_wrong_channel_is_pocerror(self):
        from vdeter.poc.api.types import PhysicalChannel
        from vdeter.poc.shared_medium.iq import IqBurst

        class WrongChannelFactory:
            def encode(self, *, source_station, channel, payload):
                # 요청 channel과 다른 channel의 waveform 반환(malformed result).
                other = PhysicalChannel.UPPER if channel == PhysicalChannel.LOWER else PhysicalChannel.LOWER
                return IqBurst(channel=other, start_sample=0, samples=(0.1 + 0.1j,))

        arb = FaultArbiter()
        prov = _provider(arbiter=arb, factory=WrongChannelFactory())
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action("ROC"), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertEqual(_poc_holds(arb), [])  # build 실패 → 예약 반환


# --- partial RF commit -------------------------------------------------------
class PartialCommitTest(unittest.TestCase):
    def test_partial_commit_is_not_success(self):
        sink = FaultSink(commit_partial_then=RuntimeError("died after first child"))
        arb = FaultArbiter()
        prov = _provider(sink=sink, arbiter=arb)
        with self.assertRaises(PocError) as ctx:
            prov.submit(_action(), request_id="r")
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_TX_OUTCOME_UNKNOWN)
        parent = prov.parent_for("r")
        self.assertNotEqual(prov._broadcast.outcome_of(parent.parent_id), ParentOutcome.COMPLETED)
        # 한쪽만 방사됐어도 성공으로 만들지 않는다.
        self.assertEqual(prov._broadcast.outcome_of(parent.parent_id), ParentOutcome.OUTCOME_UNKNOWN)


# --- duplicate / reentry -----------------------------------------------------
class DuplicateSubmitTest(unittest.TestCase):
    def test_duplicate_request_id_no_new_parent_or_rf(self):
        sink = TD.CountingRadioSink()
        factory = TD.CountingWaveformFactory()
        prov = _provider(sink=sink, factory=factory)
        s1 = prov.submit(_action(), request_id="dup")
        s2 = prov.submit(_action(), request_id="dup")
        self.assertEqual(s1.broadcast_parent_id, s2.broadcast_parent_id)
        self.assertEqual(sink.submit_calls, 2)   # child 2개(재방사 없음)
        self.assertEqual(factory.encode_calls, 1)
        self.assertEqual(sink.duplicate_child_submits, 0)


# --- release 실패 matrix (first/middle/last) --------------------------------
class ReleaseFailureMatrixTest(unittest.TestCase):
    def _three_reservation_provider(self, fail_key):
        class SelectiveReleaseArbiter(TD.FakeArbiter):
            def __init__(self):
                super().__init__()
                self.released = []
                self.recovered = False  # True가 되면 fail_key release도 성공(외부 복구 모사)

            def release(self, *, resource_key, owner):
                if resource_key == fail_key and not self.recovered:
                    raise RuntimeError("release boom " + resource_key)
                self.released.append(resource_key)
                super().release(resource_key=resource_key, owner=owner)

        arb = SelectiveReleaseArbiter()
        sched = NextSafeSlotScheduler(current_slot=0, lead_slots=1)
        prov = SharedMediumActionProvider(
            radio_sink=TD.CountingRadioSink(), rx_readiness=TD.ReadyReadiness(),
            waveform_factory=TD.CountingWaveformFactory(), arbiter=arb, scheduler=sched,
        )
        for i in range(3):
            sched.register_trigger(f"o{i}", slot=0)
            prov.submit(_action("ROC", source_observation_id=f"o{i}",
                                schedule={"kind": "after_slots", "slots": i + 1}), request_id=f"r{i}")
        return prov, arb

    def _check(self, fail_key, kept):
        prov, arb = self._three_reservation_provider(fail_key)
        with self.assertRaises(PocError) as ctx:
            prov.cleanup()
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        self.assertEqual(sorted(arb.released), sorted(kept))          # 나머지는 정상 release
        self.assertIn(fail_key, ctx.exception.detail["unresolved_ownership"])
        self.assertEqual(prov._reservations, {})                     # tracking 잔존 0
        self.assertEqual(prov.active_group_count, 0)
        # 미반환 자원이 남아 있는 한 재-cleanup도 조용히 성공하지 않는다(거짓 잔존 0 금지).
        with self.assertRaises(PocError) as ctx2:
            prov.cleanup()
        self.assertEqual(ctx2.exception.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        self.assertIn(fail_key, prov.unresolved_ownership())
        # 외부 복구 후에는 cleanup이 회수(release 재시도 성공)하고 조용히 통과한다.
        arb.recovered = True
        prov.cleanup()
        self.assertEqual(prov.unresolved_ownership(), ())

    def test_first_release_fails(self):
        self._check("lower:slot-1", ["lower:slot-2", "lower:slot-3"])

    def test_middle_release_fails(self):
        self._check("lower:slot-2", ["lower:slot-1", "lower:slot-3"])

    def test_last_release_fails(self):
        self._check("lower:slot-3", ["lower:slot-1", "lower:slot-2"])


# --- terminal fault ----------------------------------------------------------
class TerminalFaultTest(unittest.TestCase):
    def _committed_provider(self):
        prov = _provider(scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1))
        prov.submit(_action(), request_id="r")
        return prov, prov.parent_for("r")

    def test_bogus_terminal_pocerror_no_success_no_release(self):
        prov, parent = self._committed_provider()
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("r", {parent.children[0].child_id: "BOGUS",
                                       parent.children[1].child_id: "COMPLETED"})
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertFalse(prov._broadcast.is_resolved(parent.parent_id))
        self.assertIn("r", prov._reservations)   # 해제 안 함

    def test_missing_terminal_outcome_unknown(self):
        prov, parent = self._committed_provider()
        outcome = prov.resolve_outcome("r", {parent.children[0].child_id: ChildTerminal.COMPLETED})
        self.assertEqual(outcome, ParentOutcome.OUTCOME_UNKNOWN)

    def test_non_dict_terminals_is_pocerror(self):
        prov, parent = self._committed_provider()
        for bad in (["a", "b"], "COMPLETED", 5):
            with self.assertRaises(PocError) as ctx:
                prov.resolve_outcome("r", bad)
            self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertFalse(prov._broadcast.is_resolved(parent.parent_id))  # 미확정 유지

    def test_unknown_child_id_in_terminals_is_pocerror(self):
        prov, parent = self._committed_provider()
        terminals = {c.child_id: ChildTerminal.COMPLETED for c in parent.children}
        terminals["ghost-child-id"] = ChildTerminal.COMPLETED  # parent에 없는 child id
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("r", terminals)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_PROTOCOL_ERROR)
        self.assertFalse(prov._broadcast.is_resolved(parent.parent_id))

    def test_resolve_release_failure_is_ownership_uncertain(self):
        arb = FaultArbiter(release_exc=RuntimeError("release boom"))
        prov = _provider(arbiter=arb, scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1))
        prov.submit(_action(), request_id="r")
        parent = prov.parent_for("r")
        with self.assertRaises(PocError) as ctx:
            prov.resolve_outcome("r", {c.child_id: ChildTerminal.COMPLETED for c in parent.children})
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN)
        self.assertIn("lower:slot-1", ctx.exception.detail["unresolved_ownership"])


# --- evidence surfaces unresolved ownership ---------------------------------
class UnresolvedOwnershipEvidenceTest(unittest.TestCase):
    def test_unresolved_ownership_in_evidence(self):
        from vdeter.poc.shared_medium import observation as O
        from vdeter.poc.shared_medium.radiation import BroadcastResult

        arb = FaultArbiter(release_exc=RuntimeError("release boom"))
        prov = _provider(arbiter=arb, scheduler=NextSafeSlotScheduler(current_slot=0, lead_slots=1))
        prov.submit(_action(), request_id="r")
        parent = prov.parent_for("r")
        with self.assertRaises(PocError):
            prov.cleanup()
        scene = O.observe_scene([parent])
        pending = BroadcastResult(
            parent=parent, committed=True,
            submitted_child_ids=tuple(c.child_id for c in parent.children),
            outcome=ParentOutcome.PENDING,
        )
        j = prov.build_evidence([parent], scene, broadcast_results=[pending]).as_json_value()
        self.assertIn("lower:slot-1", j["broadcast_infrastructure_result"]["unresolved_ownership"])


if __name__ == "__main__":
    unittest.main()
