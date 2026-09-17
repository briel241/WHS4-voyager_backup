"""다중 launcher 종료 대기 — 공통 마감시각 기반 동시 관찰 계약 시험 (3-3).

증명하는 것
  동시 관찰 : 같은 phase 의 launcher 들이 **하나의 monotonic 마감시각**을 공유하고,
              전체 대기가 endpoint 수에 비례해 누적되지 않는다.
  보존      : endpoint 별 자연 종료·exit code·미확정이 서로를 덮지 않는다.
              먼저 끝난 endpoint 의 결과가 늦게 끝난 endpoint 때문에 사라지지 않는다.
  무개입    : 관찰기는 신호를 보내지 않는다 — 이미 끝난 프로세스에 TERM/KILL 이
              가지 않고, 관찰 중 새 제출도 없다(관찰기는 제출 경로를 모른다).
  escalation: 마감시각 뒤에 **남은 프로세스에만** 기존 escalation 이 적용된다
              (production 호출부 4곳을 AST 로 확인).
  합성 금지 : 종료되지 않은 endpoint 를 성공으로 합치지 않는다.

시간은 전부 주입 clock 이라 실제로 기다리지 않는다 (deterministic).
"""

import ast
import inspect
import os
import unittest

import apps.coordinator_main as coordinator_main

_AWAIT = coordinator_main._await_launcher_exits
ENDPOINTS = ("MASS-A", "MASS-B", "ROC")


class FakeClock:
    """주입 monotonic — sleep 이 시간을 앞으로 민다."""

    def __init__(self, start=1000.0):
        self.t = float(start)
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeProcess:
    """poll() 기반 가짜 자식. 신호 수신을 기록만 한다 (실제 프로세스 없음)."""

    def __init__(self, clock, *, exits_after=None, exit_code=0):
        self._clock = clock
        self._start = clock.now()
        self._exits_after = exits_after      # None 이면 끝나지 않는다
        self._exit_code = exit_code
        self.signals = []
        self.polls = 0

    def poll(self):
        self.polls += 1
        if (self._exits_after is not None
                and self._clock.now() >= self._start + self._exits_after):
            return self._exit_code
        return None

    def terminate(self):
        self.signals.append("TERM")
        self._exits_after = 0.0

    def kill(self):
        self.signals.append("KILL")
        self._exits_after = 0.0


def _make(clock, spec):
    """spec = {endpoint: (exits_after, exit_code)} — None 이면 끝나지 않는다."""
    return {endpoint: FakeProcess(clock, exits_after=after, exit_code=code)
            for endpoint, (after, code) in spec.items()}


class TC_COMMON_DEADLINE(unittest.TestCase):
    """공통 마감시각 관찰기의 계약."""

    def test_all_three_exit_immediately(self):
        clock = FakeClock()
        processes = _make(clock, {e: (0.0, 0) for e in ENDPOINTS})
        result = _AWAIT(processes, timeout_s=60.0,
                        now=clock.now, sleep=clock.sleep)
        self.assertEqual(sorted(result), sorted(ENDPOINTS))
        self.assertTrue(all(r["exited"] for r in result.values()))
        self.assertEqual([r["exit_code"] for r in result.values()], [0, 0, 0])
        # 즉시 끝났으면 한 번도 자지 않는다.
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(clock.t, 1000.0)

    def test_exit_order_does_not_change_the_recorded_results(self):
        """서로 다른 순서로 끝나도 endpoint 별 결과가 정확히 보존된다."""
        for order in (("MASS-A", "MASS-B", "ROC"),
                      ("ROC", "MASS-A", "MASS-B"),
                      ("MASS-B", "ROC", "MASS-A")):
            with self.subTest(order=order):
                clock = FakeClock()
                spec = {endpoint: ((index + 1) * 5.0, index + 1)
                        for index, endpoint in enumerate(order)}
                processes = _make(clock, spec)
                result = _AWAIT(processes, timeout_s=60.0,
                                now=clock.now, sleep=clock.sleep)
                self.assertTrue(all(r["exited"] for r in result.values()))
                for endpoint, (after, code) in spec.items():
                    self.assertEqual(result[endpoint]["exit_code"], code)
                    # 먼저 끝난 것이 나중 것 때문에 늦게 기록되지 않는다.
                    self.assertLess(result[endpoint]["waited_s"], after + 1.0)
                # evidence 순서는 processes 순서로 안정적이다.
                self.assertEqual(list(result), list(processes))

    def test_one_survivor_is_reported_and_the_others_are_not_lost(self):
        clock = FakeClock()
        processes = _make(clock, {"MASS-A": (3.0, 0), "MASS-B": (7.0, 0),
                                  "ROC": (None, None)})
        result = _AWAIT(processes, timeout_s=60.0,
                        now=clock.now, sleep=clock.sleep)
        self.assertTrue(result["MASS-A"]["exited"])
        self.assertTrue(result["MASS-B"]["exited"])
        self.assertFalse(result["ROC"]["exited"])
        self.assertIsNone(result["ROC"]["exit_code"])
        # 하나가 남아도 전체 대기는 공통 상한 안이다.
        self.assertLessEqual(clock.t - 1000.0, 60.0)
        # 합성 금지: 하나라도 안 끝났으면 clean 이 아니다 (호출부와 같은 식).
        self.assertFalse(all(r["exited"] for r in result.values()))

    def test_all_survive_to_the_deadline(self):
        clock = FakeClock()
        processes = _make(clock, {e: (None, None) for e in ENDPOINTS})
        result = _AWAIT(processes, timeout_s=60.0,
                        now=clock.now, sleep=clock.sleep)
        self.assertEqual([r["exited"] for r in result.values()],
                         [False, False, False])
        self.assertAlmostEqual(clock.t - 1000.0, 60.0, places=6)

    def test_total_wait_does_not_accumulate_per_endpoint(self):
        """상한 60 s 인 endpoint 3개가 있어도 전체 대기는 60 s 를 넘지 않는다."""
        for count in (1, 3, 6):
            with self.subTest(count=count):
                clock = FakeClock()
                processes = {f"E{i}": FakeProcess(clock, exits_after=None)
                             for i in range(count)}
                _AWAIT(processes, timeout_s=60.0,
                       now=clock.now, sleep=clock.sleep)
                self.assertAlmostEqual(clock.t - 1000.0, 60.0, places=6)
        # 직렬이었다면 6개 × 60 s = 360 s 였다.

    def test_the_observer_never_signals_any_process(self):
        """관찰기는 TERM/KILL 을 보내지 않는다 — 이미 끝난 것도, 남은 것도."""
        clock = FakeClock()
        processes = _make(clock, {"MASS-A": (0.0, 0), "MASS-B": (None, None),
                                  "ROC": (10.0, 0)})
        _AWAIT(processes, timeout_s=60.0, now=clock.now, sleep=clock.sleep)
        for endpoint, process in processes.items():
            self.assertEqual(process.signals, [], endpoint)

    def test_deadline_boundary_is_inclusive_like_the_previous_path(self):
        """마감시각에 딱 끝난 프로세스는 종료로 본다 (기존 재확인과 같은 의미)."""
        clock = FakeClock()
        processes = {"ROC": FakeProcess(clock, exits_after=60.0)}
        result = _AWAIT(processes, timeout_s=60.0,
                        now=clock.now, sleep=clock.sleep)
        self.assertTrue(result["ROC"]["exited"])

        # 마감시각을 지나서야 끝나는 것은 미확정이다.
        clock2 = FakeClock()
        late = {"ROC": FakeProcess(clock2, exits_after=60.5)}
        result2 = _AWAIT(late, timeout_s=60.0,
                         now=clock2.now, sleep=clock2.sleep)
        self.assertFalse(result2["ROC"]["exited"])

    def test_sleep_never_overshoots_the_common_deadline(self):
        clock = FakeClock()
        processes = _make(clock, {e: (None, None) for e in ENDPOINTS})
        _AWAIT(processes, timeout_s=7.3, now=clock.now, sleep=clock.sleep)
        self.assertAlmostEqual(sum(clock.sleeps), 7.3, places=6)
        self.assertTrue(all(value <= 0.5 + 1e-9 for value in clock.sleeps),
                        clock.sleeps)

    def test_zero_processes_returns_immediately(self):
        clock = FakeClock()
        self.assertEqual(_AWAIT({}, timeout_s=60.0,
                                now=clock.now, sleep=clock.sleep), {})
        self.assertEqual(clock.sleeps, [])

    def test_interrupt_during_the_wait_is_not_turned_into_success(self):
        """인터럽트는 전파된다 — 남은 프로세스를 성공으로 합성하지 않는다."""
        clock = FakeClock()

        def interrupting_sleep(seconds):
            raise KeyboardInterrupt

        processes = _make(clock, {e: (None, None) for e in ENDPOINTS})
        with self.assertRaises(KeyboardInterrupt):
            _AWAIT(processes, timeout_s=60.0,
                   now=clock.now, sleep=interrupting_sleep)
        for process in processes.values():
            self.assertEqual(process.signals, [])


class TC_ESCALATION_ONLY_FOR_SURVIVORS(unittest.TestCase):
    """production 호출부 4곳: 마감 뒤 **남은 것에만** 기존 escalation."""

    CALLERS = ("run_rr_p0", "run_mass_roc_service", "run_mass_roc_session",
               "main")
    ESCALATIONS = ("_terminate_process_bounded", "terminate", "kill")

    @classmethod
    def setUpClass(cls):
        source = inspect.getsource(coordinator_main)
        cls.tree = ast.parse(source)
        cls.functions = {node.name: node for node in cls.tree.body
                         if isinstance(node, ast.FunctionDef)}

    def _escalation_calls(self, node):
        found = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = None
            if isinstance(child.func, ast.Name):
                name = child.func.id
            elif isinstance(child.func, ast.Attribute):
                name = child.func.attr
            if name in self.ESCALATIONS:
                found.append(child)
        return found

    def test_every_caller_shares_one_common_deadline(self):
        for name in self.CALLERS:
            with self.subTest(caller=name):
                body = ast.unparse(self.functions[name])
                self.assertIn("_await_launcher_exits(", body)

    def test_no_caller_waits_per_endpoint_in_series(self):
        """endpoint 마다 자기 deadline 을 만드는 옛 구조가 남아 있지 않다."""
        source = inspect.getsource(coordinator_main)
        self.assertNotIn(
            "while process.poll() is None and time.monotonic() < deadline",
            source)

    def test_escalation_is_guarded_by_the_exited_flag(self):
        """launcher 를 강제 종료하는 곳은 전부 `exited` 가 거짓인 분기 안이다."""
        parents = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def guarded_by_exited(call):
            """호출을 감싼 for 문이 `exited` 인 endpoint 를 **먼저 건너뛰는지**."""
            cursor = call
            while cursor in parents:
                parent = parents[cursor]
                if isinstance(parent, ast.For):
                    for statement in parent.body:
                        if statement.lineno >= call.lineno:
                            break
                        if (isinstance(statement, ast.If)
                                and "exited" in ast.unparse(statement.test)
                                and any(isinstance(inner, ast.Continue)
                                        for inner in statement.body)):
                            return True
                cursor = parent
            return False

        for name in ("run_mass_roc_session", "main"):
            with self.subTest(caller=name):
                calls = self._escalation_calls(self.functions[name])
                launcher_calls = [
                    call for call in calls
                    if "processes[" in ast.unparse(call)]
                self.assertTrue(launcher_calls, name)
                for call in launcher_calls:
                    self.assertTrue(guarded_by_exited(call),
                                    (name, ast.unparse(call)))

    def test_paths_without_prior_escalation_did_not_gain_one(self):
        """예전부터 강제 종료를 하지 않던 두 경로에 escalation 을 새로 넣지 않았다."""
        for name in ("run_rr_p0", "run_mass_roc_service"):
            with self.subTest(caller=name):
                body = ast.unparse(self.functions[name])
                self.assertNotIn("_terminate_process_bounded", body)
                self.assertNotIn("processes[", body.replace(
                    "processes[endpoint_id]", ""))

    def test_clean_exit_still_gates_the_cleanup_verdict(self):
        """`launcher_clean_exit` 가 cleanup 판정에 계속 쓰인다 (합성 금지)."""
        session = ast.unparse(self.functions["run_mass_roc_session"])
        self.assertIn("'launcher_clean_exit': clean_exit", session)
        self.assertIn("clean_exit and cleanup['workers_after_stop'] == 0",
                      session)

    def test_every_caller_records_per_endpoint_shutdown_evidence(self):
        for name in ("run_rr_p0", "run_mass_roc_service",
                     "run_mass_roc_session"):
            with self.subTest(caller=name):
                self.assertIn("'launcher_exits': launcher_exits",
                              ast.unparse(self.functions[name]))


class TC_TERMINATE_BOUNDED_CONTRACT(unittest.TestCase):
    """기존 escalation helper 의 계약은 그대로다 (3-3 무접촉 확인)."""

    def test_already_exited_process_receives_no_signal(self):
        clock = FakeClock()
        process = FakeProcess(clock, exits_after=0.0, exit_code=0)
        outcome = coordinator_main._terminate_process_bounded(process)
        self.assertEqual(process.signals, [])
        self.assertFalse(outcome["terminate_sent"])
        self.assertFalse(outcome["kill_sent"])
        self.assertEqual(outcome["exit_code"], 0)

    def test_survivor_gets_term_then_kill_and_the_outcome_is_recorded(self):
        class Stubborn(FakeProcess):
            def __init__(self, clock, *, die_on):
                super().__init__(clock, exits_after=None)
                self.die_on = die_on

            def terminate(self):
                self.signals.append("TERM")
                if self.die_on == "TERM":
                    self._exits_after = 0.0

            def kill(self):
                self.signals.append("KILL")
                if self.die_on == "KILL":
                    self._exits_after = 0.0

            def wait(self, timeout=None):
                if self.poll() is None:
                    import subprocess
                    raise subprocess.TimeoutExpired("launcher", timeout)
                return self.poll()

        clock = FakeClock()
        soft = Stubborn(clock, die_on="TERM")
        outcome = coordinator_main._terminate_process_bounded(soft)
        self.assertEqual(soft.signals, ["TERM"])
        self.assertTrue(outcome["terminate_sent"])
        self.assertFalse(outcome["kill_sent"])

        hard = Stubborn(clock, die_on="KILL")
        outcome = coordinator_main._terminate_process_bounded(hard)
        self.assertEqual(hard.signals, ["TERM", "KILL"])
        self.assertTrue(outcome["kill_sent"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
