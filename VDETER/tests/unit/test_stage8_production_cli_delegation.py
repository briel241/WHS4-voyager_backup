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
