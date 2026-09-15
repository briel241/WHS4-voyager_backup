import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from apps.standalone import run_cmd
from apps.standalone.local_session import LocalCoordinatorSession


class ProductionCliDelegationTests(unittest.TestCase):
    def test_real_session_declares_coordinator_ownership(self):
        self.assertIs(True, LocalCoordinatorSession.poc_workloads_in_coordinator)

    def test_production_poc_uses_same_coordinator_path_without_rf_off_flag(self):
        session = NS(poc_workloads_in_coordinator=True)
        context = {"rf_off_software": False}
        with patch.object(run_cmd, "_run_workload_rf_off_poc", return_value={"from": "coordinator"}) as delegate, \
                patch.object(run_cmd, "_prepare_poc") as local:
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0,
                                           poc_ctx=context)
        self.assertEqual({"from": "coordinator"}, result)
        delegate.assert_called_once()
        local.assert_not_called()

    def test_poc_off_never_delegates_to_poc(self):
        session = Mock(poc_workloads_in_coordinator=True)
        session.wait_result.return_value = {"status": "PASS", "quiescence": {"pass": True}}
        with patch.object(run_cmd, "_run_workload_rf_off_poc") as delegate:
            run_cmd._run_workload(session, "SC-VDE-006", "normal", out=Mock(), clock=lambda: 0, poc_ctx=None)
        delegate.assert_not_called()


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
    (`CollisionExchange`)는 `prepare_poc_scope`가 `arm()`보다 **먼저** 열고 controller가
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


class ControllerPreselectedCatalogTests(unittest.TestCase):
    """coordinator에 위임된 PoC 요청은 선택이 **이미 끝난** 것이다.

    CLI가 menu(대화형) 또는 진단 계획(`allowed_workloads`)으로 poc_id를 정해 실어 보내므로,
    controller는 catalog(`poc_dir`) 요청을 prompt 없는 확정 선택으로 해석해야 한다. 그렇지
    않으면 실물의 `--poc-dir`(계획 기반 조합 포함)이 controller의 비대화형 catalog 거부(§6.1)에
    걸려 **arm 실패**로 끝난다 — 지금까지 실물이 전부 `--poc` direct라 드러나지 않던 공백이다.

    행위 확인은 RF-off 재현(실 controller subprocess에 catalog 요청)이 맡고, 여기서는 결선을
    잠근다.
    """

    @staticmethod
    def _resolve_option_calls(function):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(function))
        return [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "resolve_option"]

    def _assert_preselected_non_interactive(self, function, who):
        import ast

        calls = self._resolve_option_calls(function)
        self.assertEqual(1, len(calls), f"{who}의 resolve_option 호출은 하나여야 한다")
        keywords = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("preselected", keywords,
                      f"{who}가 위임된 catalog 요청을 확정 선택으로 표시하지 않는다 — arm 실패로 끝난다")
        self.assertIsInstance(keywords["preselected"], ast.Constant)
        self.assertIs(keywords["preselected"].value, True)
        self.assertIs(keywords["interactive"].value, False,
                      f"{who}는 어떤 경우에도 prompt를 열지 않는다")

    def test_controller_resolves_delegated_catalog_as_preselected(self):
        from apps import stage7_mass_roc_controller as controller

        self._assert_preselected_non_interactive(controller.main, "실물 controller")

    def test_rf_off_session_resolves_delegated_catalog_as_preselected(self):
        """위임 요청의 소비자는 **둘**이다 — 실물 controller와 RF-off software 세션.

        둘 중 하나만 고치면 RF-off e2e는 통과하는데 실물이 arm 실패하거나 그 반대가 된다.
        실측: controller만 고친 뒤 실 coordinator e2e가 같은 거부 문구로 실패했다
        (`--poc-dir는 대화형 mode 전용`, 0.3 s, controller 미기동).
        """
        from apps import stage8_rf_off_session as rf_off

        self._assert_preselected_non_interactive(rf_off._handle_poc_request, "RF-off 세션")


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
