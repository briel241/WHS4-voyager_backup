"""실제 `vdeter run --poc/--poc-dir` CLI → Stage 8 PoC 실행 연결 시험 (RF-off).

- CLI 계약(어디서나): `--poc`/`--poc-dir` 파싱, PoC OFF 구조적 0(stage8 미로드), `--poc`+`--poc-dir`
  상호배제·비대화형 `--poc-dir` 거부(게이트 이전에 fail-closed).
- 실행 종단(Linux 전용): `run_cmd`의 실제 per-workload 함수 `_run_workload`가 scenario 선택 →
  immutable snapshot → 실 child·Unix socket·SDK·engine·Stage 5 provider → 세 독립 판정 → cleanup을
  CLI 경로로 실행함을 검증한다. device 경계 seam은 session이 제공한다(RF-off software).

환경 주: 최상위 `execute`의 Stage 7 gate(identity fingerprint)는 cryptography 50을 요구하는데 WSL은
41이고, 실 PoC child는 Linux SOCK_SEQPACKET을 요구한다(Windows 미지원). 두 요건이 한 환경에 없어
`execute`+실 child를 한 번에 돌리지 못한다. 그래서 gate-무관 PoC 실행 코드(`_run_workload`→`_run_poc`
→`poc_run`→integration.entry)를 Linux에서 실 child로, 상호배제/opt-in 계약을 gate 이전 경로로 검증한다.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from apps.standalone import cli, poc_run, run_cmd, state  # noqa: E402

_LINUX = (hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET")
          and hasattr(os, "getuid") and sys.platform != "win32")
_SRC = os.path.join(_REPO, "VDETER", "src")

_REACTIVE_REPLAY = (
    "class Poc:\n"
    "    def on_start(self, context):\n        return []\n"
    "    def on_event(self, event, context):\n"
    "        if event.type == 'PACKET_OBSERVED':\n"
    "            return [context.replay(target='ROC',\n"
    "                                   source_observation_id=event.body.get('observation_id'),\n"
    "                                   payload={'packet_hex': 'deadbeefcafe'})]\n"
    "        return []\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")


def _write_poc(poc_dir, base="cli_replay"):
    from vdeter.stage8.testing.execution_fakes import write_poc_package
    write_poc_package(poc_dir, base, poc_id=base.replace("_", "-"),
                      script=_REACTIVE_REPLAY)
    return os.path.join(poc_dir, base + ".py")


class _PocDeviceSession:
    """coordinator session 최소 fake: workload 계약 + PoC device 경계(RF-off software) + scenario event."""

    def __init__(self, run_root, run_id):
        self.run_root = Path(run_root)
        self.run_id = run_id
        self.submitted = []

    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None):
        self.submitted.append((scenario_id, certificate_case))
        return "%s-%s-%03d" % (self.run_id, scenario_id.lower(), len(self.submitted))

    def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
        return {"status": "PASS", "quiescence": {"pass": True}, "scenario": {"results": {}}}

    # PoC device 경계와 scenario event는 session이 제공한다(RF-off software).
    def poc_device_seams(self):
        from vdeter.stage8.integration.rf_off import rf_off_seams
        return rf_off_seams()

    def poc_workload_steps(self, scenario_id):
        from vdeter.stage8.execution.runner import observation_step
        from vdeter.stage8.testing.fakes import make_observation
        return [observation_step(make_observation(
            source_station="ROC", destination_station="MASS-A"))]

    def poc_terminal_source(self):
        # R3: RF-off 시험은 장비 double의 child terminal source를 **명시적으로** 제공한다.
        # 실장비 session은 이 자리에 실 RF terminal 콜백을 연결한다(미제공 시 executor는 TX_UNKNOWN).
        from vdeter.stage8.integration.rf_off import rf_off_terminal_source
        return rf_off_terminal_source()


class CliContractTest(unittest.TestCase):
    """gate 이전 CLI opt-in 계약 — crypto/실장비 불필요, 어디서나 실행."""

    def test_cli_parser_has_poc_flags(self):
        args = cli.build_parser().parse_args(["run", "SC-VDE-006", "--poc", "./x.py"])
        self.assertEqual(args.poc, "./x.py")
        self.assertIsNone(args.poc_dir)
        args = cli.build_parser().parse_args(["run", "--poc-dir", "/pocs"])
        self.assertEqual(args.poc_dir, "/pocs")
        self.assertIsNone(args.poc)

    def test_poc_off_is_structural_zero_no_stage8_import(self):
        code = ("import sys\n"
                "sys.path[:0]=[%r,%r,%r]\n"
                "from apps.standalone import cli\n"
                "cli.build_parser().parse_args(['run','SC-VDE-006'])\n"
                "bad=[m for m in sys.modules if m.startswith('vdeter.stage8')]\n"
                "sys.exit(0 if not bad else 3)\n"
                % (_REPO, _SRC, os.path.join(_REPO, "VDETER")))
        env = dict(os.environ)
        env["PYTHONPATH"] = (os.pathsep.join([_REPO, _SRC])
                             + os.pathsep + env.get("PYTHONPATH", ""))
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))

    def test_mutually_exclusive_poc_refused_before_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = state.StatePaths(tmp)
            lines = []
            code = run_cmd.execute(paths, scenario="SC-VDE-006", poc="./x.py",
                                   poc_dir="/pocs", out=lines.append,
                                   input_fn=lambda p: "q", progress=False)
            self.assertEqual(code, cli.EXIT_CONFIG, lines)
            self.assertTrue(any("Refused" in ln for ln in lines))

    def test_noninteractive_poc_dir_refused_before_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = state.StatePaths(tmp)
            lines = []
            code = run_cmd.execute(paths, scenario="SC-VDE-006", poc_dir="/pocs",
                                   out=lines.append, input_fn=lambda p: "q",
                                   progress=False)
            self.assertEqual(code, cli.EXIT_CONFIG, lines)


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class RunWorkloadPocEndToEnd(unittest.TestCase):
    """`run_cmd._run_workload`(실 per-workload CLI 함수) → 실 child·socket·provider PoC 실행."""

    def _ctx(self, poc_path, run_root, run_id):
        option = poc_run.resolve_option(poc=poc_path, poc_dir=None, interactive=False)
        return {"module": poc_run, "option": option, "run_root": run_root,
                "run_id": run_id, "interactive": False,
                "input_fn": lambda p: "", "src_path": _SRC}

    def test_run_workload_executes_poc_with_real_child(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-poc")
            ctx = self._ctx(poc_path, run_root, "run-poc")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic, poc_ctx=ctx, poc_id=None)
            # scenario 판정과 독립으로 PoC evidence가 붙는다(§11.1).
            self.assertEqual(result["status"], "PASS")   # scenario 판정 불변
            poc = result.get("poc")
            self.assertIsNotNone(poc, "\n".join(lines))
            self.assertEqual(poc["poc_infrastructure"], "COMPLETED")
            self.assertEqual(len(poc["attacks"]), 1)
            self.assertEqual(poc["attacks"][0]["action_execution"], "TX_COMPLETED")
            self.assertEqual(poc["attacks"][0]["target"], "ROC")
            self.assertEqual(poc["radiation"]["radiated_children"], 2)

    def test_poc_off_workload_has_no_poc_resources(self):
        # poc_ctx 없음(=PoC OFF) → result에 poc 없음, 자원 0(구조적 0).
        with tempfile.TemporaryDirectory() as run_root:
            session = _PocDeviceSession(run_root, "run-off")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic)
            self.assertNotIn("poc", result)

    def test_missing_device_seams_fails_closed(self):
        # session이 device 경계를 제공하지 않으면 조용히 software로 대체하지 않고 fail-closed.
        class _NoSeamSession(_PocDeviceSession):
            poc_device_seams = None  # 미제공
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoSeamSession(run_root, "run-noseam")
            ctx = self._ctx(poc_path, run_root, "run-noseam")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic, poc_ctx=ctx, poc_id=None)
            self.assertEqual(result["poc"]["status"], "ERROR")


_ARM_FAIL_POC = (
    "class Poc:\n"
    "    def on_start(self, context):\n        raise RuntimeError('injected on_start failure')\n"
    "    def on_event(self, event, context):\n        return []\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")


class _NoTerminalSession(_PocDeviceSession):
    """실 RF terminal source를 연결하지 않은 session — production 기본(성공 합성 금지)."""
    def poc_terminal_source(self):
        return None


class _ScenarioFailSession(_PocDeviceSession):
    """scenario 판정이 FAIL로 오는 session(§11.1: PoC 판정과 독립)."""
    def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
        return {"status": "FAIL", "quiescence": {"pass": True}, "scenario": {"results": {}}}


class _RejectSession(_PocDeviceSession):
    """action 하나가 RF 전 거부(REJECTED_BEFORE_TX)되는 session — session inhibit 아님(R2)."""
    def poc_device_seams(self):
        from vdeter.stage8.integration.rf_off import rf_off_seams
        return rf_off_seams(fail_on_retune=True)


def _feeder(answers):
    it = iter(answers)

    def _fn(prompt):
        try:
            return next(it)
        except StopIteration:
            return "q"  # 소진되면 종료 입력
    return _fn


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class PocNegativeCliTest(unittest.TestCase):
    """R1~R9 CLI 종단 negative test — 실 child·socket·provider, 실제 CLI 호출 순서/다음 workload 허용/최종 JSON.

    수용 기준: 순수 모델·loopback 대체 금지. 각 항목은 실 `_run_workload`/`_menu`(또는 실 selection)로
    검증하고, SESSION_INHIBIT 생성은 실 child의 실제 결과(POC_TX_OUTCOME_UNKNOWN)에서 나온다.
    """

    def _ctx(self, poc_path, run_root, run_id, *, interactive=False, input_fn=None):
        option = poc_run.resolve_option(poc=poc_path, poc_dir=None, interactive=interactive)
        return {"module": poc_run, "option": option, "run_root": run_root,
                "run_id": run_id, "interactive": interactive,
                "input_fn": input_fn or (lambda p: ""), "src_path": _SRC}

    # --- R1: PoC는 scenario submit 전에 arm; 준비/ARMED 실패면 scenario 제출 0 ---
    def test_r1_arm_failure_does_not_submit_scenario(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.stage8.testing.execution_fakes import write_poc_package
            write_poc_package(poc_dir, "armfail", poc_id="armfail", script=_ARM_FAIL_POC)
            poc_path = os.path.join(poc_dir, "armfail.py")
            session = _PocDeviceSession(run_root, "run-armfail")
            ctx = self._ctx(poc_path, run_root, "run-armfail")
            lines = []
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=lines.append,
                                           clock=time.monotonic, poc_ctx=ctx, poc_id=None)
            # scenario는 제출되지 않는다(NOT_RUN), submit 0. PoC 실패는 evidence로 남는다.
            self.assertEqual(result["status"], "NOT_RUN", "\n".join(lines))
            self.assertTrue(result.get("poc_prep_failed"))
            self.assertEqual(session.submitted, [])
            self.assertIn(result["poc"]["poc_infrastructure"], ("ERROR", "ABORTED"))

    def test_r1_poc_off_is_structural_zero(self):
        # PoC OFF(poc_ctx 없음) → scenario는 그대로 실행되고 PoC 자원 0.
        with tempfile.TemporaryDirectory() as run_root:
            session = _PocDeviceSession(run_root, "run-off")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic)
            self.assertEqual(session.submitted, [("SC-VDE-006", "normal")])
            self.assertNotIn("poc", result)

    # --- R3: 실 terminal source 없으면 TX_UNKNOWN(성공 합성 금지); 있으면 TX_COMPLETED ---
    def test_r3_no_terminal_source_is_tx_unknown(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoTerminalSession(run_root, "run-noterm")
            ctx = self._ctx(poc_path, run_root, "run-noterm")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=ctx, poc_id=None)
            poc = result["poc"]
            self.assertEqual(poc["attacks"][0]["action_execution"], "TX_UNKNOWN")
            self.assertEqual(poc["poc_infrastructure"], "ABORTED")

    # --- R4: scenario FAIL → PoC scenario_verdict FAIL, 최종 JSON 일관 ---
    def test_r4_scenario_fail_propagates_to_poc_verdict(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _ScenarioFailSession(run_root, "run-scfail")
            ctx = self._ctx(poc_path, run_root, "run-scfail")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=ctx, poc_id=None)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["poc"]["scenario_verdict"], "FAIL")
            # PoC 인프라 판정은 scenario와 독립(§11.1).
            self.assertEqual(result["poc"]["poc_infrastructure"], "COMPLETED")

    # --- R5: 최종 JSON에 Stage 5 provider evidence(cleanup 전 snapshot) ---
    def test_r5_provider_evidence_in_final_json(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-evd")
            ctx = self._ctx(poc_path, run_root, "run-evd")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=ctx, poc_id=None)
            pe = result["poc"]["provider_evidence"]
            self.assertIsNotNone(pe)
            self.assertEqual(pe["model_boundary"]["model"], "WIRED_SHARED_MEDIUM_EMULATION")
            self.assertIn("readiness_by_parent", pe["broadcast_infrastructure_result"])
            self.assertIn("parent_outcomes", pe["broadcast_infrastructure_result"])

    # --- R2: SESSION_INHIBIT면 다음 workload 제출 0 (menu·run-all·non-interactive) ---
    def _plan_run(self, session, ctx, plan):
        """execute()의 비대화형 loop gate를 그대로 적용해 다음 workload 허용 여부를 검증."""
        out = lambda l: None
        results = []
        for sid, case in plan:
            r = run_cmd._run_workload(session, sid, case, out=out, clock=time.monotonic,
                                      poc_ctx=ctx)
            results.append(r)
            if r.get("status") != "PASS" or run_cmd._stop_after_workload(r, out):
                break
        return results

    def test_r2_session_inhibit_stops_noninteractive_plan(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoTerminalSession(run_root, "run-inhibit")  # 실 SESSION_INHIBIT
            ctx = self._ctx(poc_path, run_root, "run-inhibit")
            self._plan_run(session, ctx, [("SC-VDE-006", "normal"), ("SC-VDE-006", "normal")])
            self.assertEqual(len(session.submitted), 1, "second workload must NOT be submitted")

    def test_r2_request_reject_does_not_inhibit_session(self):
        # RF 전 단일 거부(REJECTED_BEFORE_TX)는 session inhibit이 아니다 — 다음 workload 계속.
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _RejectSession(run_root, "run-reject")
            ctx = self._ctx(poc_path, run_root, "run-reject")
            self._plan_run(session, ctx, [("SC-VDE-006", "normal"), ("SC-VDE-006", "normal")])
            self.assertEqual(len(session.submitted), 2, "REQUEST_REJECTED must not stop session")

    def test_r2_session_inhibit_stops_interactive_menu(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoTerminalSession(run_root, "run-menu")
            fed = _feeder(["SC-VDE-006", "SC-VDE-006", "q"])
            ctx = self._ctx(poc_path, run_root, "run-menu", interactive=True, input_fn=fed)
            run_cmd._menu(session, out=lambda l: None, input_fn=fed,
                          clock=time.monotonic, poc_ctx=ctx)
            self.assertEqual(len(session.submitted), 1, "menu must stop after SESSION_INHIBIT")

    def test_r2_stop_after_workload_decision(self):
        out = lambda l: None
        self.assertTrue(run_cmd._stop_after_workload(
            {"status": "PASS", "quiescence": {"pass": True}, "poc_severity": "SESSION_INHIBIT"}, out))
        self.assertFalse(run_cmd._stop_after_workload(
            {"status": "PASS", "quiescence": {"pass": True}, "poc_severity": "REQUEST_REJECTED"}, out))
        self.assertFalse(run_cmd._stop_after_workload(
            {"status": "PASS", "quiescence": {"pass": True}, "poc_severity": "POC_ABORT"}, out))
        self.assertTrue(run_cmd._stop_after_workload(
            {"status": "PASS", "quiescence": {"pass": False}}, out))

    # --- R7: catalog 메뉴 q/r/number/none/invalid 해석 ---
    def test_r7_resolve_catalog_selection(self):
        with tempfile.TemporaryDirectory() as poc_dir:
            os.chmod(poc_dir, 0o700)
            _write_poc(poc_dir, base="cat_poc")
            option = poc_run.resolve_option(poc=None, poc_dir=poc_dir, interactive=True)
            R = lambda c: poc_run.resolve_catalog_selection(option, "SC-VDE-006", c)
            self.assertEqual(R("q"), ("quit", None))
            self.assertEqual(R("quit"), ("quit", None))
            self.assertEqual(R("r"), ("rescan", None))
            self.assertEqual(R("none"), ("baseline", None))
            self.assertEqual(R("0"), ("baseline", None))
            self.assertEqual(R("zzz-not-a-poc"), ("invalid", None))
            kind, poc_id = R("1")
            self.assertEqual(kind, "poc")
            self.assertEqual(poc_id, "cat-poc")

    def test_r7_menu_quit_submits_zero(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            _write_poc(poc_dir, base="cat_poc")
            session = _PocDeviceSession(run_root, "run-q")
            # scenario 선택 후 PoC 메뉴에서 q → session 종료, scenario submit 0.
            fed = _feeder(["SC-VDE-006", "q"])
            option = poc_run.resolve_option(poc=None, poc_dir=poc_dir, interactive=True)
            ctx = {"module": poc_run, "option": option, "run_root": run_root,
                   "run_id": "run-q", "interactive": True, "input_fn": fed, "src_path": _SRC}
            run_cmd._menu(session, out=lambda l: None, input_fn=fed,
                          clock=time.monotonic, poc_ctx=ctx)
            self.assertEqual(session.submitted, [])

    # --- R9: direct --poc 미지원 required capability는 import·spawn 전에 거부 ---
    def test_r9_direct_unsupported_capability_rejected_before_spawn(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            marker = os.path.join(poc_dir, "imported.marker")
            from vdeter.stage8.testing.execution_fakes import write_poc_package
            script = ("from pathlib import Path\nPath(%r).write_text('x')\n" % marker
                      + "class Poc:\n    def on_start(self, c): return []\n"
                        "    def on_event(self, e, c): return []\n"
                        "    def on_finish(self, c): return None\n").encode("utf-8")
            write_poc_package(poc_dir, "dual", poc_id="dual", script=script,
                              required_capabilities=("observe_dual_physical_channel",),
                              capabilities=("observe_packet", "observe_dual_physical_channel"))
            poc_path = os.path.join(poc_dir, "dual.py")
            session = _PocDeviceSession(run_root, "run-cap")
            ctx = self._ctx(poc_path, run_root, "run-cap")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=ctx, poc_id=None)
            # import·spawn 전에 거부: scenario 미제출, import marker 미생성.
            self.assertEqual(result["status"], "NOT_RUN")
            self.assertEqual(session.submitted, [])
            self.assertFalse(os.path.exists(marker), "researcher import must not run")


def _write_poc_compat(poc_dir, base, compat):
    from vdeter.stage8.testing.execution_fakes import write_poc_package
    write_poc_package(poc_dir, base, poc_id=base.replace("_", "-"),
                      script=_REACTIVE_REPLAY, compatible_scenarios=compat)
    return os.path.join(poc_dir, base + ".py")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class ExecuteLevelPocEndToEnd(unittest.TestCase):
    """실 사용자 CLI 본체 `run_cmd.execute(... --poc)` 종단 — gate → bring-up → PoC prep(실 child spawn·
    ARMED, scenario submit **전**) → submit → wait → finish/evidence/cleanup → **저장된 summary.json**.

    coordinator session만 가짜(기존 test_standalone_run_cli 하네스)이고 PoC child·socket·SDK·engine·
    provider·snapshot·CLI 호출 순서·최종 JSON은 실제다. 수용 기준: 실제 CLI 호출 순서, 다음 workload
    허용, 최종 저장 JSON.
    """

    def _factory(self, *, terminal=True):
        import test_standalone_run_cli as harness
        base_cls = harness.make_session_factory()

        class PocFakeSession(base_cls):
            def poc_device_seams(self):
                from vdeter.stage8.integration.rf_off import rf_off_seams
                return rf_off_seams()

            def poc_workload_steps(self, scenario_id):
                from vdeter.stage8.execution.runner import observation_step
                from vdeter.stage8.testing.fakes import make_observation
                return [observation_step(make_observation(
                    source_station="ROC", destination_station="MASS-A"))]

            def poc_terminal_source(self):
                if not terminal:
                    return None
                from vdeter.stage8.integration.rf_off import rf_off_terminal_source
                return rf_off_terminal_source()

        PocFakeSession.instances = base_cls.instances
        return PocFakeSession, harness

    def _summary(self, factory):
        import json as _json
        sess = factory.instances[-1]
        return sess, _json.loads((sess.run_root / "summary.json").read_text(encoding="utf-8"))

    def test_execute_poc_completes_and_persists_summary(self):
        factory, harness = self._factory()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as poc_dir:
            paths, _ = harness.fx.prepare_environment(tmp)
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            code, lines = harness.run(paths, factory, scenario="SC-VDE-006", poc=poc_path)
            self.assertEqual(code, run_cmd.EXIT_PASS, "\n".join(lines))
            sess, summary = self._summary(factory)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(sess.submitted, [("SC-VDE-006", "normal")])
            w = summary["workloads"][0]
            self.assertEqual(w["status"], "PASS")
            self.assertEqual(w["poc"]["poc_infrastructure"], "COMPLETED")
            self.assertEqual(w["poc"]["scenario_verdict"], "PASS")
            self.assertEqual(w["poc"]["attacks"][0]["action_execution"], "TX_COMPLETED")
            self.assertEqual(w["poc_severity"], "OK")
            # R5: 저장된 최종 JSON에 Stage 5 provider evidence(cleanup 전 snapshot)가 있다.
            self.assertEqual(w["poc"]["provider_evidence"]["model_boundary"]["model"],
                             "WIRED_SHARED_MEDIUM_EMULATION")
            self.assertTrue(any("Stage 8 PoC opt-in active" in ln for ln in lines))

    def test_execute_poc_session_inhibit_fails_run_and_exit_code(self):
        # 실 terminal source 미연결 → 실 child의 POC_TX_OUTCOME_UNKNOWN → SESSION_INHIBIT →
        # scenario 판정은 PASS로 보존하되 run 전체 FAIL + EXIT_FAIL(R2: 인프라 실패를 숨기지 않음).
        factory, harness = self._factory(terminal=False)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as poc_dir:
            paths, _ = harness.fx.prepare_environment(tmp)
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            code, lines = harness.run(paths, factory, scenario="SC-VDE-006", poc=poc_path)
            self.assertEqual(code, run_cmd.EXIT_FAIL, "\n".join(lines))
            sess, summary = self._summary(factory)
            self.assertEqual(summary["status"], "FAIL")
            w = summary["workloads"][0]
            self.assertEqual(w["status"], "PASS")                        # scenario 판정 보존
            self.assertEqual(w["poc"]["poc_infrastructure"], "ABORTED")
            self.assertEqual(w["poc_severity"], "SESSION_INHIBIT")
            self.assertTrue(sess.closed)                                 # session 종료·cleanup

    def test_execute_batch_stops_after_session_inhibit(self):
        # run all(005→006→001n→001f): 첫 workload SESSION_INHIBIT → 다음 workload 제출 0(R2).
        factory, harness = self._factory(terminal=False)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as poc_dir:
            paths, _ = harness.fx.prepare_environment(tmp)
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc_compat(poc_dir, "batch_poc",
                                         ("SC-VDE-005", "SC-VDE-006", "SC-VDE-001"))
            code, lines = harness.run(paths, factory, scenario="all", poc=poc_path)
            self.assertEqual(code, run_cmd.EXIT_FAIL, "\n".join(lines))
            sess, summary = self._summary(factory)
            self.assertEqual(sess.submitted, [("SC-VDE-005", "normal")])
            self.assertEqual(summary["scenarios_run"], 1)
            self.assertEqual(summary["scenarios_planned"], 4)
            self.assertEqual(summary["workloads"][0]["poc_severity"], "SESSION_INHIBIT")

    def test_execute_poc_off_unchanged(self):
        # PoC OFF: 기존 실행 그대로(poc 블록 없음, 구조적 0).
        factory, harness = self._factory()
        with tempfile.TemporaryDirectory() as tmp:
            paths, _ = harness.fx.prepare_environment(tmp)
            code, lines = harness.run(paths, factory, scenario="SC-VDE-006")
            self.assertEqual(code, run_cmd.EXIT_PASS, "\n".join(lines))
            sess, summary = self._summary(factory)
            self.assertNotIn("poc", summary["workloads"][0])
            self.assertFalse(any("Stage 8 PoC" in ln for ln in lines))


_REACTIVE_ANY = (
    "class Poc:\n"
    "    def on_start(self, context):\n        return []\n"
    "    def on_event(self, event, context):\n"
    "        return [context.replay(target='ROC', payload={'packet_hex': '01020304'})]\n"
    "    def on_finish(self, context):\n        return None\n"
).encode("utf-8")


class _SubmitFailSession(_PocDeviceSession):
    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None):
        self.submitted.append((scenario_id, certificate_case))
        raise RuntimeError("injected submit failure")


class _WaitInterruptSession(_PocDeviceSession):
    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None):
        self.submitted.append((scenario_id, certificate_case))
        return "actual-session-workload"

    def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
        raise KeyboardInterrupt("injected wait interrupt")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class PocCleanupN1N2Test(unittest.TestCase):
    """N1/N2 CLI 종단 — scenario submit/wait 실패·arm 인터럽트에서 armed PoC의 cleanup-only 정리,
    신규 action 0, 원형 예외/인터럽트 재전달, COMPLETED 금지. 실 child·socket·RF-off provider."""

    def _ctx(self, poc_path, run_root, run_id):
        option = poc_run.resolve_option(poc=poc_path, poc_dir=None, interactive=False)
        return {"module": poc_run, "option": option, "run_root": run_root,
                "run_id": run_id, "interactive": False,
                "input_fn": lambda p: "", "src_path": _SRC}

    def _run_capturing(self, session, ctx):
        """`_run_workload`를 돌리되 준비된 PocWorkload를 붙잡아 종료 뒤 잔존을 검사한다."""
        captured = []
        original = poc_run.prepare_poc_workload

        def capture(**kwargs):
            w = original(**kwargs)
            captured.append(w)
            return w
        caught = None
        with patch.object(poc_run, "prepare_poc_workload", capture):
            try:
                run_cmd._run_workload(session, "SC-VDE-006", "normal", out=lambda _: None,
                                      clock=time.monotonic, poc_ctx=ctx, poc_id=None)
            except BaseException as exc:  # noqa: BLE001
                caught = exc
        return caught, captured

    def test_n1_submit_exception_cleans_armed_poc_and_reraises(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _SubmitFailSession(run_root, "run-n1submit")
            caught, captured = self._run_capturing(session, self._ctx(poc_path, run_root, "run-n1submit"))
            self.assertIsInstance(caught, RuntimeError)      # 원래 예외 재전달(덮어쓰기 없음)
            self.assertEqual(len(captured), 1)
            conn = captured[0].connection
            res = conn.residuals()
            self.assertFalse(res["child_alive"], res)        # armed PoC child 정리됨
            self.assertFalse(res["socket_file_exists"], res)
            self.assertFalse(res["workload_dir_exists"], res)
            self.assertEqual(captured[0].seams.group_tx.committed_groups, 0)  # 신규 action 0

    def test_n1_wait_interrupt_cleans_armed_poc_and_reraises(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _WaitInterruptSession(run_root, "run-n1wait")
            caught, captured = self._run_capturing(session, self._ctx(poc_path, run_root, "run-n1wait"))
            self.assertIsInstance(caught, KeyboardInterrupt)  # 원형 인터럽트 재전달
            self.assertEqual(len(captured), 1)
            res = captured[0].connection.residuals()
            self.assertFalse(res["child_alive"], res)
            self.assertFalse(res["socket_file_exists"], res)
            self.assertFalse(res["workload_dir_exists"], res)
            self.assertEqual(captured[0].seams.group_tx.committed_groups, 0)

    def test_n2_arm_interrupt_reraises_original_no_action_no_completed(self):
        # ARMED 직후 KeyboardInterrupt: PocRunError로 변환하지 않고 원형 재전달, cleanup 중 신규 event/
        # action 0(TX group 0), 결과 COMPLETED 아님. 실 child.
        from vdeter.stage8.integration.entry import select_and_snapshot
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.stage8.testing.execution_fakes import write_poc_package
            write_poc_package(poc_dir, "reactive", poc_id="reactive", script=_REACTIVE_ANY)
            poc_path = os.path.join(poc_dir, "reactive.py")
            session = _PocDeviceSession(run_root, "run-n2")
            snap = select_and_snapshot(scenario_id="SC-VDE-006",
                                       dest_dir=os.path.join(run_root, "snapshot"),
                                       poc=poc_path, interactive=False)
            captured = []
            original = poc_run.prepare_poc_workload

            def capture(**kwargs):
                w = original(**kwargs)
                captured.append(w)
                real_arm = w.arm

                def interrupted():
                    assert real_arm()               # 실제 ARMED 도달
                    raise KeyboardInterrupt("injected immediately after ARMED")
                w.arm = interrupted
                return w
            caught = None
            with patch.object(poc_run, "prepare_poc_workload", capture):
                try:
                    poc_run.prepare_workload(session, snap, scenario_id="SC-VDE-006",
                                             run_id="r-n2", workload_id="w-n2", src_path=_SRC)
                except BaseException as exc:  # noqa: BLE001
                    caught = exc
            self.assertIsInstance(caught, KeyboardInterrupt)          # 원형 재전달(PocRunError 아님)
            self.assertEqual(session.submitted, [])                   # scenario submit 0
            self.assertEqual(captured[0].seams.group_tx.committed_groups, 0)  # cleanup TX 0
            res = captured[0].connection.residuals()
            self.assertFalse(res["child_alive"], res)
            self.assertFalse(res["socket_file_exists"], res)
            # 재진입 안전: 다시 abort해도 중복 실행·잔존 없음.
            captured[0].abort()
            self.assertFalse(captured[0].connection.residuals()["child_alive"])

    def test_n2_arm_ordinary_failure_is_pocrunerror_aborted_no_action(self):
        # arm이 일반 예외로 실패하면 PocRunError(.evidence ABORTED/ERROR), 신규 action 0.
        from vdeter.stage8.integration.entry import select_and_snapshot
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.stage8.testing.execution_fakes import write_poc_package
            write_poc_package(poc_dir, "reactive", poc_id="reactive", script=_REACTIVE_ANY)
            poc_path = os.path.join(poc_dir, "reactive.py")
            session = _PocDeviceSession(run_root, "run-n2e")
            snap = select_and_snapshot(scenario_id="SC-VDE-006",
                                       dest_dir=os.path.join(run_root, "snapshot"),
                                       poc=poc_path, interactive=False)
            captured = []
            original = poc_run.prepare_poc_workload

            def capture(**kwargs):
                w = original(**kwargs)
                captured.append(w)
                real_arm = w.arm

                def failing():
                    assert real_arm()
                    raise RuntimeError("injected ordinary arm failure")
                w.arm = failing
                return w
            caught = None
            with patch.object(poc_run, "prepare_poc_workload", capture):
                try:
                    poc_run.prepare_workload(session, snap, scenario_id="SC-VDE-006",
                                             run_id="r-n2e", workload_id="w-n2e", src_path=_SRC)
                except BaseException as exc:  # noqa: BLE001
                    caught = exc
            self.assertEqual(type(caught).__name__, "PocRunError")
            self.assertIn(caught.evidence["poc_infrastructure"], ("ABORTED", "ERROR"))
            self.assertNotEqual(caught.evidence["poc_infrastructure"], "COMPLETED")
            self.assertEqual(captured[0].seams.group_tx.committed_groups, 0)
            self.assertFalse(captured[0].connection.residuals()["child_alive"])


@unittest.skipUnless(_LINUX, "실 coordinator 프로세스·PoC child·SOCK_SEQPACKET 전용")
class RealCoordinatorPocEndToEnd(unittest.TestCase):
    """실 `LocalCoordinatorSession` + 실 coordinator 프로세스(--rf-off-software) 종단 — fake session이
    아니다. 실 CLI execute → 실 coordinator(파일 IPC) → coordinator 안에서 workload-local PoC(실 child·
    socket·SDK·engine·provider) → summary.json. 하드웨어 경계만 명시적 RF-off double."""

    def _run(self, poc_path, *, scenario="SC-VDE-006"):
        import test_standalone_run_cli as harness
        from apps.standalone.local_session import LocalCoordinatorSession
        with tempfile.TemporaryDirectory() as tmp:
            paths, _ = harness.fx.prepare_environment(tmp)
            code, lines = harness.run(paths, LocalCoordinatorSession,
                                      scenario=scenario, poc=poc_path,
                                      rf_off_software=True)
            # 실 coordinator가 쓴 summary.json을 실 run root에서 읽는다.
            runs = sorted((Path(paths.runs_dir)).glob("*/summary.json"))
            summary = None
            if runs:
                import json as _json
                summary = _json.loads(runs[-1].read_text(encoding="utf-8"))
            return code, lines, summary

    def test_real_coordinator_runs_poc_through_authoritative_tx_path(self):
        """실 CLI→실 coordinator→**권위 Stage 7 송신 경로**→실 PoC child 종단(P01~P10 결선).

        PoC action이 sidecar 공유매체 예약 → `radio_adapter.schedule_tx`(최소 lead·timed retune·
        lane 겹침·정상 TX 우선) → 한 번의 group TX(두 lane) → **권위 TX 원장**으로 나간다. terminal은
        그 원장에서 읽으므로(SUBMITTED→COMPLETED) 매질 로그 휴리스틱이 아니다.
        """
        with tempfile.TemporaryDirectory() as poc_dir:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            code, lines, summary = self._run(poc_path)
            joined = chr(10).join(lines)
            self.assertIsNotNone(summary, joined)
            w = summary["workloads"][0]
            poc = w.get("poc")
            self.assertIsNotNone(poc, joined)
            # scenario 판정과 PoC 판정의 독립성(§11.1).
            self.assertEqual(w["status"], "PASS")
            self.assertEqual(poc["scenario_verdict"], "PASS")
            # 권위 원장이 두 child terminal을 COMPLETED로 확정 → parent COMPLETED.
            self.assertEqual(poc["poc_infrastructure"], "COMPLETED")
            self.assertEqual(poc["attacks"][0]["action_execution"], "TX_COMPLETED")
            # 실 방사 1 group(두 물리 경로 fan-out).
            self.assertEqual((poc.get("radiation") or {}).get("committed_groups"), 1)
            self.assertEqual(poc["provider_evidence"]["model_boundary"]["model"],
                             "WIRED_SHARED_MEDIUM_EMULATION")
            # 미확정 TX가 없으므로 세션은 계속 진행 가능하다(SESSION_INHIBIT 아님).
            self.assertNotEqual(poc.get("infrastructure_error_code"), "POC_TX_OUTCOME_UNKNOWN")
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(code, cli.EXIT_PASS, joined)

    def test_real_coordinator_multi_workload_isolation_on_authoritative_path(self):
        """run all: 각 workload가 새 snapshot·새 child를 쓰고 이전 event가 섞이지 않는다.

        권위 TX 원장이 terminal을 확정하므로 미확정 TX로 세션이 차단되지 않고 4개 workload가 모두
        실행된다. burst_id별 원장 조회이므로 앞 workload의 ACK이 뒤 workload를 완료 처리하지 못한다.
        """
        with tempfile.TemporaryDirectory() as poc_dir:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc_compat(poc_dir, "batch_poc",
                                         ("SC-VDE-005", "SC-VDE-006", "SC-VDE-001"))
            code, lines, summary = self._run(poc_path, scenario="all")
            joined = chr(10).join(lines)
            self.assertIsNotNone(summary, joined)
            workloads = summary["workloads"]
            self.assertEqual(len(workloads), 4)          # 005, 006, 001 normal, 001 forged
            wids = [w.get("workload_id") for w in workloads]
            self.assertEqual(len(set(wids)), 4, wids)    # 각 workload 고유 id(새 snapshot/child)
            for w in workloads:
                self.assertEqual(w["status"], "PASS")
                self.assertEqual(w["poc"]["poc_infrastructure"], "COMPLETED")
                self.assertEqual(w["poc"]["attacks"][0]["action_execution"], "TX_COMPLETED")
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(code, cli.EXIT_PASS, joined)


if __name__ == "__main__":
    unittest.main()
