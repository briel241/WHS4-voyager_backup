import ast
import inspect
import textwrap
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from apps.standalone import run_cmd
from apps.standalone.local_session import LocalCoordinatorSession


class ProductionCliDelegationTests(unittest.TestCase):
    """CLI(run_cmd)는 PoC를 프로세스 안에서 실행하지 않는다 — 요청을 coordinator 세션에 위임하고
    회신된 evidence·severity만 반영한다(2-1: in-process 분기 제거)."""

    def test_real_session_declares_coordinator_ownership(self):
        self.assertIs(True, LocalCoordinatorSession.poc_workloads_in_coordinator)

    def test_poc_request_is_delegated_to_the_coordinator_session(self):
        from apps.standalone import poc_run

        session = NS(poc_workloads_in_coordinator=True,
                     submit_workload=Mock(return_value="w-1"),
                     wait_result=Mock(return_value={
                         "status": "PASS", "quiescence": {"pass": True},
                         "poc": {"status": "BASELINE", "poc_infrastructure": "NOT_RUN"}}))
        context = {"module": poc_run, "option": NS(poc="/x.py", poc_dir=None), "src_path": "/src"}
        result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0,
                                       poc_ctx=context, poc_id=None)
        kwargs = session.submit_workload.call_args.kwargs
        self.assertEqual({"poc": "/x.py", "poc_dir": None, "poc_id": None, "src_path": "/src"},
                         kwargs["poc"])
        self.assertEqual("BASELINE", result["poc"]["status"])       # coordinator evidence 그대로
        self.assertEqual("OK", result["poc_severity"])               # severity만 CLI가 매긴다
        # CLI in-process PoC 경로는 존재하지 않는다(재도입 방지).
        for name in ("_prepare_poc", "_finish_poc", "_run_workload_rf_off_poc"):
            self.assertFalse(hasattr(run_cmd, name), name)

    def test_poc_off_never_sends_a_poc_request(self):
        session = Mock(poc_workloads_in_coordinator=True)
        session.submit_workload.return_value = "w-1"
        session.wait_result.return_value = {"status": "PASS", "quiescence": {"pass": True}}
        result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0,
                                       poc_ctx=None)
        self.assertNotIn("poc", session.submit_workload.call_args.kwargs)
        self.assertNotIn("poc", result)
        self.assertNotIn("poc_severity", result)

    def test_session_without_coordinator_poc_hosting_is_refused_fail_closed(self):
        """부정 대조: PoC를 coordinator에서 실행한다고 선언하지 않은 세션에 PoC 요청이 오면
        조용한 in-process 대체 없이 거부하고 workload를 제출하지 않는다."""
        from apps.standalone import poc_run

        session = Mock(spec=["submit_workload", "wait_result"])     # 선언 없음
        context = {"module": poc_run, "option": NS(poc="/x.py", poc_dir=None), "src_path": None}
        with self.assertRaises(poc_run.PocRunError):
            run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0,
                                  poc_ctx=context)
        session.submit_workload.assert_not_called()

class ControllerSignalDispositionTests(unittest.TestCase):
    """PoC를 호스팅하는 controller child가 SIGTERM에서 정리 경로를 탄다(§취소·복원).

    실측 diag-20260914-142043: dual scope 활성 중 운영자 취소로 coordinator가
    `process.terminate()`를 보냈고, controller에 처분이 없어 child가 `exit_code=-15`로
    즉사했다. 그 결과 `run_workload`의 `finally`가 실행되지 못해 PoC child·socket 정리
    (`abort_prepared`), RX scope 복원과 `collision_model_closed`, `poc-result` 기록이
    전부 누락되고 `poc.sock`이 잔존했다. 정상 종료 회차에는 넷 다 남는다.

    coordinator는 terminate 뒤 `CHILD_TERMINATE_TIMEOUT_S`(30 s)를 기다리므로 유예는
    이미 있다 — 필요한 것은 신호를 정리 경로로 바꾸는 처분 하나다.
    """

    def test_controller_main_installs_a_sigterm_handler(self):
        """`main()`이 인자 해석 전에 SIGTERM 처분을 설치한다 — 이후 전 구간이 정리 경로를 탄다."""
        import ast
        import inspect

        from apps import stage7_mass_roc_controller as controller

        self.assertTrue(callable(getattr(controller, "_install_signal_handlers", None)),
                        "controller에 신호 처분 설치 함수가 없다")
        tree = ast.parse(inspect.getsource(controller.main))
        called = [node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertIn("_install_signal_handlers", called,
                      "main()이 신호 처분을 설치하지 않는다 — SIGTERM에 즉사해 정리가 누락된다")
        body = tree.body[0].body
        first = next(i for i, node in enumerate(body)
                     if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                     and isinstance(node.value.func, ast.Name)
                     and node.value.func.id == "_install_signal_handlers")
        parsed = next((i for i, node in enumerate(body)
                       if "parse_args" in ast.dump(node)), len(body))
        self.assertLess(first, parsed, "신호 처분은 인자 해석보다 먼저 설치해야 한다")

    def test_sigterm_handler_raises_keyboard_interrupt_like_the_cli(self):
        """처분은 CLI(`cli.py::_install_signal_handlers`)와 같은 형태다 — SIGTERM → KeyboardInterrupt.

        `finally`가 도는지는 이 예외 종류에 달려 있다(SystemExit·기본 종료는 정리 없이 끝난다).
        """
        import signal as _signal

        from apps import stage7_mass_roc_controller as controller

        previous = _signal.getsignal(_signal.SIGTERM)
        try:
            controller._install_signal_handlers()
            handler = _signal.getsignal(_signal.SIGTERM)
            self.assertTrue(callable(handler), "SIGTERM 처분이 설치되지 않았다")
            with self.assertRaises(KeyboardInterrupt):
                handler(_signal.SIGTERM, None)
        finally:
            try:
                _signal.signal(_signal.SIGTERM, previous)
            except (ValueError, OSError, TypeError):
                pass


class ControllerArmInterruptTests(unittest.TestCase):
    """arm 구간의 중단도 자원을 회수하고 종결 evidence를 남긴다.

    처분을 설치하면 SIGTERM은 `KeyboardInterrupt`가 된다. 그런데 arm 구간의
    `except Exception`은 그것을 잡지 않아, 중단이 `main()` 밖으로 그대로 빠져나가며
    `poc-result.json`·`final-result.json`·`workload-trace.jsonl`을 **한 건도** 남기지
    않았다(`TraceWriter`는 버퍼링이라 `close()` 미도달이면 파일 자체가 생기지 않는다).

    자원 회수는 두 곳이 나눠 맡는다. PoC child·socket·provider 예약은
    `poc_run.prepare_workload`가 이미 cleanup-only로 정리한다. 반면 workload RX scope
    (`WorkloadRxScope`)는 `prepare_poc_scope`가 `arm()`보다 **먼저** 열고 controller가
    소유하므로, 중단 창이 `prepare_workload`의 처리 구간보다 넓고 controller가 닫아야 한다.

    여기서는 그 구조를 잠근다. 실제로 자원이 회수되고 파일 셋이 생기는지는 실 controller
    subprocess를 SIGTERM으로 취소하는 RF-off 재현이 확인한다 — 이 단위시험이 그것을
    대신한다고 주장하지 않는다.
    """

    def _arm_try_node(self):
        """`main()`에서 PoC arm을 감싼 `try` 노드를 찾는다(arm 실패 기록으로 식별)."""
        import ast
        import inspect

        from apps import stage7_mass_roc_controller as controller

        tree = ast.parse(inspect.getsource(controller.main))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if any("poc_arm_failed" in ast.dump(handler) for handler in node.handlers):
                return node
        self.fail("main()에서 PoC arm try 블록을 찾지 못했다")

    def _base_exception_handler(self):
        import ast

        node = self._arm_try_node()
        for handler in node.handlers:
            if isinstance(handler.type, ast.Name) and handler.type.id == "BaseException":
                return handler
        self.fail("arm 구간에 BaseException 처리가 없다 — 중단이 종결 기록을 건너뛴다")

    def test_arm_interrupt_records_both_result_artifacts(self):
        """중단에도 poc-result와 final-result를 남기고 trace를 flush한다."""
        import ast

        dumped = ast.dump(self._base_exception_handler())
        self.assertIn("poc-result", dumped,
                      "중단 경로가 poc-result를 남기지 않는다")
        self.assertIn("final-result", dumped,
                      "중단 경로가 final-result를 남기지 않는다")
        # TraceWriter는 버퍼링이다 — close 없이는 workload-trace.jsonl이 생기지 않는다.
        self.assertIn("'close'", dumped,
                      "중단 경로가 trace를 닫지 않는다 — trace 파일 자체가 생기지 않는다")

    def test_arm_interrupt_closes_the_workload_rx_scope(self):
        """RX scope는 controller가 소유한다 — `prepare_workload`가 닫아주지 않는다."""
        import ast

        dumped = ast.dump(self._base_exception_handler())
        self.assertIn("close_collision_exchange", dumped,
                      "중단 경로가 workload RX scope를 닫지 않는다")

    def test_arm_interrupt_is_reraised_not_swallowed(self):
        """중단 사유를 보존한다 — 정상 종료나 일반 실패로 재분류하지 않는다."""
        import ast

        handler = self._base_exception_handler()
        bare = [node for node in ast.walk(handler)
                if isinstance(node, ast.Raise) and node.exc is None]
        self.assertTrue(bare, "중단을 원형 그대로 재전파하지 않는다")
        returns = [node for node in ast.walk(handler) if isinstance(node, ast.Return)]
        self.assertFalse(returns,
                         "중단 경로가 return으로 끝난다 — 중단이 종료 코드로 삼켜진다")

    def test_general_exception_path_still_returns_fail(self):
        """일반 arm 실패의 기존 계약(fail-closed 기록 후 return 1)은 그대로다."""
        import ast

        node = self._arm_try_node()
        general = [handler for handler in node.handlers
                   if isinstance(handler.type, ast.Name) and handler.type.id == "Exception"]
        self.assertEqual(1, len(general), "일반 예외 처리가 사라졌거나 중복됐다")
        dumped = ast.dump(general[0])
        self.assertIn("poc_arm_failed", dumped)
        self.assertIn("final-result", dumped)

    def test_arm_interrupt_cleanup_evidence_is_preserved_not_discarded(self):
        """`prepare_workload`의 인터럽트 정리 결과를 버리지 않고 중단에 실어 보낸다.

        이전에는 `workload.abort(...)`의 반환을 폐기해, 정리가 실제로 수행됐는데도 caller가
        그것을 기록할 근거를 잃었다. 값을 합성하는 것이 아니라 **실행된 정리 경로가 돌려준**
        evidence를 보존하는 것이다.
        """
        import ast
        import inspect

        from apps.standalone import poc_run

        tree = ast.parse(inspect.getsource(poc_run.prepare_workload))
        handlers = [node for node in ast.walk(tree)
                    if isinstance(node, ast.ExceptHandler)
                    and "KeyboardInterrupt" in ast.dump(node)]
        self.assertTrue(handlers, "arm 인터럽트 처리가 없다")
        dumped = ast.dump(handlers[0])
        self.assertIn("_finalize_evidence", dumped,
                      "정리 evidence를 만들지 않는다")
        self.assertIn("evidence", dumped)
        bare = [node for node in ast.walk(handlers[0])
                if isinstance(node, ast.Raise) and node.exc is None]
        self.assertTrue(bare, "인터럽트를 원형 그대로 재전달하지 않는다")


class ControllerArmFailureTests(unittest.TestCase):
    """arm 실패(일반 예외)는 fail-closed다 — scenario에 진입하지 않고, RX scope를 닫고, 공통 evidence
    조립기(`poc_run.arm_failure_evidence`)로 기록한다. 실제 실행은 RF-off 실 child 시험이 맡는다."""

    def _arm_try(self):
        from apps import stage7_mass_roc_controller as controller

        tree = ast.parse(inspect.getsource(controller.main))
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                    "poc_arm_failed" in ast.dump(handler) for handler in node.handlers):
                return node
        self.fail("main()에서 PoC arm try 블록을 찾지 못했다")

    def test_arm_failure_returns_before_any_scenario(self):
        node = self._arm_try()
        handler = next(h for h in node.handlers
                       if isinstance(h.type, ast.Name) and h.type.id == "Exception")
        self.assertIsInstance(handler.body[-1], ast.Return,
                              "arm 실패 처리가 return으로 끝나지 않는다 — scenario로 흘러간다")
        called_names = {n.func.id for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn("run_scenarios", called_names)
        called_attrs = {n.func.attr for n in ast.walk(handler)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("arm_failure_evidence", called_attrs,
                      "arm 실패 evidence는 poc_run.arm_failure_evidence(공통 조립기)로 만든다")
        self.assertIn("close_collision_exchange", called_attrs, "arm 실패에도 workload RX scope를 닫는다")


class ControllerPreselectedCatalogTests(unittest.TestCase):
    """coordinator에 위임된 PoC 요청은 선택이 **이미 끝난** 것이다.

    CLI가 menu(대화형) 또는 진단 계획(`allowed_workloads`)으로 poc_id를 정해 실어 보내므로,
    coordinator 쪽은 catalog(`poc_dir`) 요청을 prompt 없는 확정 선택으로 해석해야 한다. 그렇지
    않으면 실물의 `--poc-dir`(계획 기반 조합 포함)이 비대화형 catalog 거부(§6.1)에 걸려
    **arm 실패**로 끝난다.

    위임 요청의 소비자는 **둘**이다 — 실물 controller와 RF-off software 세션. 예전엔 둘이 각자
    해석해 한쪽만 고쳐지는 회귀가 있었다(실측: controller만 고친 뒤 실 coordinator e2e가 같은 거부
    문구로 실패). 2-1부터 해석 규칙은 `poc_run.snapshot_for_request` **하나**가 소유하고 두 소비자는
    그것만 부른다. 행위 확인은 RF-off 재현(실 controller subprocess·실 coordinator e2e)이 맡고,
    여기서는 그 결선을 잠근다.
    """

    @staticmethod
    def _calls(function, name):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if ((isinstance(func, ast.Name) and func.id == name)
                    or (isinstance(func, ast.Attribute) and func.attr == name)):
                found.append(node)
        return found

    def _assert_preselected_non_interactive(self, function, who):
        calls = self._calls(function, "resolve_option")
        self.assertEqual(1, len(calls), f"{who}의 resolve_option 호출은 하나여야 한다")
        keywords = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("preselected", keywords,
                      f"{who}가 위임된 catalog 요청을 확정 선택으로 표시하지 않는다 — arm 실패로 끝난다")
        self.assertIsInstance(keywords["preselected"], ast.Constant)
        self.assertIs(keywords["preselected"].value, True)
        self.assertIs(keywords["interactive"].value, False,
                      f"{who}는 어떤 경우에도 prompt를 열지 않는다")

    def _assert_delegates_resolution(self, function, who):
        self.assertEqual([], self._calls(function, "resolve_option"),
                         f"{who}가 위임 요청을 직접 해석한다 — 단일 소유자(snapshot_for_request) 우회")
        self.assertTrue(self._calls(function, "snapshot_for_request"),
                        f"{who}가 snapshot_for_request를 부르지 않는다")

    def test_single_owner_resolves_delegated_requests_as_preselected(self):
        from apps.standalone import poc_run

        self._assert_preselected_non_interactive(poc_run.snapshot_for_request,
                                                 "poc_run.snapshot_for_request")

    def test_controller_delegates_resolution_to_the_single_owner(self):
        from apps import stage7_mass_roc_controller as controller

        self._assert_delegates_resolution(controller.main, "실물 controller")

    def test_rf_off_session_delegates_resolution_to_the_single_owner(self):
        from apps import rf_off_software_session as rf_off

        self._assert_delegates_resolution(rf_off._handle_poc_request, "RF-off 세션")

    def test_checkers_reject_direct_or_unconfirmed_resolution(self):
        """부정 대조: 소비자의 직접 해석과 소유자의 미확정(대화형) 해석은 검사기가 거부한다
        (production 파일 무변조 — 검사 대상은 여기 정의한 잘못된 결선 표본이다)."""

        def direct_consumer(request):                       # 소유자를 우회해 직접 해석
            return poc_run.resolve_option(poc=request.get("poc"), poc_dir=None,   # noqa: F821
                                          interactive=False, preselected=True)

        def unconfirmed_owner(request):                     # preselected 없음·대화형
            return resolve_option(poc=request.get("poc"), poc_dir=None, interactive=True)  # noqa: F821

        with self.assertRaises(AssertionError):
            self._assert_delegates_resolution(direct_consumer, "직접 해석 소비자")
        with self.assertRaises(AssertionError):
            self._assert_preselected_non_interactive(unconfirmed_owner, "미확정 해석 소유자")

class ControllerScenarioInterruptTests(unittest.TestCase):
    """scenario 실행 중 중단도 종결 evidence를 남긴다 — **`finally` 안에서** 기록해야 한다.

    중단은 `except Exception`에 걸리지 않으므로 `finally`가 끝나는 즉시 try 문 **전체 밖으로**
    전파된다. 따라서 try 문 **뒤**에 있는 종결 구간은 도달하지 못하고, 그것을 다시 try/finally로
    감싸는 것으로도 덮이지 않는다.

    실측으로 반증했다: 감싸기만 넣은 판과 넣지 않은 판이 **동일하게** 산출물 3종 전량 부재·
    rc -2였고 traceback이 같은 지점을 가리켰다. 그래서 종결 기록을 `finally` 안으로 옮겼다.
    """

    def _scenario_try_node(self):
        """`main()`에서 scenario 실행을 감싼 try 노드(일반 예외 기록으로 식별)."""
        import ast
        import inspect

        from apps import stage7_mass_roc_controller as controller

        tree = ast.parse(inspect.getsource(controller.main))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if any("workload_exception" in ast.dump(handler) for handler in node.handlers):
                return node
        self.fail("main()에서 scenario try 블록을 찾지 못했다")

    def test_finalization_runs_inside_the_finally_not_after_the_try(self):
        """종결 호출이 `finally` 안에 있다 — try 뒤에 두면 중단 경로가 도달하지 못한다."""
        import ast

        node = self._scenario_try_node()
        self.assertTrue(node.finalbody, "scenario try에 finally가 없다")
        dumped = ast.dump(ast.Module(body=node.finalbody, type_ignores=[]))
        self.assertIn("_finalize_workload", dumped,
                      "중단 경로의 종결 기록이 finally 밖에 있다 — 중단은 여기 닿지 않는다")

    def test_interrupt_forces_the_abort_path_not_finish(self):
        """취소에서는 `finish_workload`(FINISH 전달)가 아니라 abort 경로여야 한다."""
        import ast

        node = self._scenario_try_node()
        dumped = ast.dump(ast.Module(body=node.finalbody, type_ignores=[]))
        self.assertIn("poc_failure", dumped,
                      "중단을 PoC 실패로 표시하지 않으면 종결이 FINISH를 전달한다")

    def test_cleanup_precedes_the_finalization_record(self):
        """정리(RX scope 복원)가 먼저고 evidence 기록이 나중이다 — 기록이 정리 결과를 담는다."""
        import ast

        node = self._scenario_try_node()
        order = []
        for index, statement in enumerate(node.finalbody):
            dumped = ast.dump(statement)
            if "close_collision_exchange" in dumped:
                order.append(("cleanup", index))
            if "_finalize_workload" in dumped:
                order.append(("finalize", index))
        names = [name for name, _ in order]
        self.assertIn("cleanup", names, "RX scope 정리가 finally에 없다")
        self.assertIn("finalize", names, "종결 기록이 finally에 없다")
        self.assertLess(dict(order)["cleanup"], dict(order)["finalize"],
                        "정리보다 기록이 먼저다 — evidence가 정리 결과를 담지 못한다")

    def test_interrupt_is_not_swallowed_by_the_finally(self):
        """`finally`가 중단을 삼키지 않는다 — return도 새 raise도 두지 않는다."""
        import ast

        node = self._scenario_try_node()
        for statement in node.finalbody:
            for inner in ast.walk(statement):
                self.assertNotIsInstance(
                    inner, ast.Return,
                    "finally의 return은 전파 중인 중단을 삼킨다")
