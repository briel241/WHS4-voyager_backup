"""실제 `vdeter run --poc/--poc-dir` CLI → Stage 8 PoC 실행 연결 시험 (RF-off).

- CLI 계약(어디서나): `--poc`/`--poc-dir` 파싱, PoC OFF 구조적 0(stage8 미로드), `--poc`+`--poc-dir`
  상호배제·비대화형 `--poc-dir` 거부(게이트 이전에 fail-closed).
- 위임 경계(어디서나): `run_cmd._run_workload`는 PoC를 CLI 프로세스에서 실행하지 않고 PoC 요청
  (`poc_run.workload_request`)을 coordinator 세션에 실어 위임한다. 요청 해석·baseline·arm 실패 evidence는
  `poc_run` 공통 조립기가 소유한다(2-1).
- 실행 종단(Linux 전용): coordinator 세션 fake가 production RF-off 세션의 per-workload handler
  (`rf_off_software_session.poc_workload_record` → `_handle_poc_request` → `poc_run`)를 **같은 프로세스에서**
  실행해 실 child·Unix socket·SDK·engine·Stage 5 provider·독립 판정·cleanup을 CLI 경로(`_run_workload`/
  `_menu`/`execute`)로 검증한다. device 경계 seam은 session이 제공한다(RF-off software). 실 coordinator
  프로세스(`--rf-off-software`) 종단은 `RealCoordinatorPocEndToEnd`가 맡는다. scenario verdict → PoC
  `scenario_verdict` 매핑은 실물 controller `_finalize_workload`로 검증한다(RF-off 세션은 scenario를
  돌리지 않아 PASS 고정).
- cleanup 계약(Linux 전용): arm 뒤 scenario 경로 실패·인터럽트의 cleanup-only 정리(`poc_run.abort_prepared`,
  실물 controller의 finally/finalize가 부른다)와 arm 중 인터럽트(`prepare_workload`)를 실 child로 검증한다.

환경 주: 최상위 `execute`의 Stage 7 gate(identity fingerprint)는 cryptography 50을 요구하는데 WSL은
41이고, 실 PoC child는 Linux SOCK_SEQPACKET을 요구한다(Windows 미지원). 두 요건이 한 환경에 없어
`execute`+실 child를 한 번에 돌리지 못한다. 그래서 gate-무관 PoC 실행 코드를 Linux에서 실 child로,
상호배제/opt-in 계약을 gate 이전 경로로 검증한다.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
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
    from vdeter.poc.testing.execution_fakes import write_poc_package
    write_poc_package(poc_dir, base, poc_id=base.replace("_", "-"),
                      script=_REACTIVE_REPLAY)
    return os.path.join(poc_dir, base + ".py")


def _ctx(poc_path, *, interactive=False):
    """CLI PoC opt-in context(`run_cmd.execute`가 만드는 것과 같은 필드)."""
    option = poc_run.resolve_option(poc=poc_path, poc_dir=None, interactive=interactive)
    return {"module": poc_run, "option": option, "src_path": _SRC}


class _Trace:
    """controller `_finalize_workload`용 trace 기록기(파일 없음)."""

    def __init__(self):
        self.rows = []

    def emit(self, kind, **fields):
        self.rows.append((kind, fields))

    def close(self):
        pass


class _PocDeviceSession:
    """coordinator 세션 최소 fake: workload 계약 + PoC device 경계(RF-off software) + scenario event.

    PoC 요청이 실린 workload는 production RF-off 세션 handler(`poc_workload_record`)를 **이 프로세스에서**
    실행해 처리한다 — 실 coordinator 프로세스와 같은 코드, 프로세스 경계만 없다. 실 RF-off 세션처럼
    결과 record가 그대로 workload 결과가 된다(status PASS/ERROR + `poc` evidence; scenario는 돌리지 않는다).
    """

    poc_workloads_in_coordinator = True

    def __init__(self, run_root, run_id):
        from apps import rf_off_software_session as rf_off
        self.run_root = Path(run_root)
        self.run_id = run_id
        self.submitted = []
        self._requests = {}
        self.runtime = rf_off.build_rf_off_runtime()
        self.seams = None            # device 경계가 실제로 조립됐는지(=arm 진입) 관측용

    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None, poc=None):
        self.submitted.append((scenario_id, certificate_case))
        workload_id = "%s-%s-%03d" % (self.run_id, scenario_id.lower(), len(self.submitted))
        self._requests[workload_id] = {"workload_id": workload_id, "scenario_id": scenario_id,
                                       "certificate_case": certificate_case, "poc": poc}
        return workload_id

    def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
        request = self._requests[workload_id]
        if request.get("poc") is None:
            return self._scenario_result()
        from apps import rf_off_software_session as rf_off
        return rf_off.poc_workload_record(self, self.runtime, request,
                                          run_root=self.run_root, run_id=self.run_id)

    def _scenario_result(self):
        return {"status": "PASS", "quiescence": {"pass": True}, "scenario": {"results": {}}}

    # PoC device 경계와 scenario event는 session이 제공한다(RF-off software).
    def poc_device_seams(self):
        from vdeter.poc.integration.rf_off import rf_off_seams
        self.seams = rf_off_seams()
        return self.seams

    def poc_workload_steps(self, scenario_id):
        from vdeter.poc.execution.runner import observation_step
        from vdeter.poc.testing.fakes import make_observation
        return [observation_step(make_observation(
            source_station="ROC", destination_station="MASS-A"))]

    def poc_terminal_source(self):
        # R3: RF-off 시험은 장비 double의 child terminal source를 **명시적으로** 제공한다.
        # 실장비 session은 이 자리에 실 RF terminal 콜백을 연결한다(미제공 시 executor는 TX_UNKNOWN).
        from vdeter.poc.integration.rf_off import rf_off_terminal_source
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
                "bad=[m for m in sys.modules if m.startswith('vdeter.poc')]\n"
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


class PocRequestContractTest(unittest.TestCase):
    """위임 요청 스키마·확정 선택 해석·evidence 짝(`poc_run` 공통 조립기) — 어디서나 실행."""

    def test_workload_request_carries_selection_and_source(self):
        option = poc_run.resolve_option(poc="./x.py", poc_dir=None, interactive=False)
        self.assertEqual(poc_run.workload_request(option, poc_id=None, src_path="/src"),
                         {"poc": "./x.py", "poc_dir": None, "poc_id": None, "src_path": "/src"})

    def test_snapshot_for_request_is_preselected_and_non_interactive(self):
        seen = {}

        def fake_resolve(**kwargs):
            seen["resolve"] = kwargs
            return poc_run.PocOption(mode=poc_run.SourceMode.CATALOG, poc=None, poc_dir="/pocs")

        def fake_select(option, **kwargs):
            seen["select"] = (option, kwargs)
            return None

        with patch.object(poc_run, "resolve_option", fake_resolve), \
                patch.object(poc_run, "select_snapshot", fake_select):
            out = poc_run.snapshot_for_request(
                {"poc": None, "poc_dir": "/pocs", "poc_id": "abc", "src_path": None},
                scenario_id="SC-VDE-006", dest_dir="/tmp/x", server_config=None)
        self.assertIsNone(out)
        self.assertEqual(seen["resolve"], {"poc": None, "poc_dir": "/pocs",
                                           "interactive": False, "preselected": True})
        option, kwargs = seen["select"]
        self.assertEqual(option.poc_dir, "/pocs")
        self.assertEqual(kwargs["poc_id"], "abc")
        self.assertIs(kwargs["interactive"], False)
        self.assertEqual(kwargs["scenario_id"], "SC-VDE-006")
        self.assertEqual(kwargs["dest_dir"], "/tmp/x")

    def test_baseline_and_arm_failure_evidence_shapes(self):
        self.assertEqual(poc_run.baseline_evidence(workload_id="w", scenario_id="S"),
                         {"status": "BASELINE", "poc_infrastructure": "NOT_RUN",
                          "workload_id": "w", "scenario_id": "S"})
        plain = poc_run.arm_failure_evidence(RuntimeError("boom"), workload_id="w", scenario_id="S")
        self.assertEqual(plain, {"status": "ERROR", "poc_infrastructure": "ERROR",
                                 "error": "RuntimeError: boom", "workload_id": "w", "scenario_id": "S"})
        self.assertEqual(poc_run.poc_severity(plain), "POC_ABORT")
        error = poc_run.PocRunError("arm")
        error.evidence = {"status": "ERROR", "poc_infrastructure": "ABORTED",
                          "infrastructure_error_code": "POC_TX_OUTCOME_UNKNOWN", "workload_id": "already"}
        kept = poc_run.arm_failure_evidence(error, workload_id="w", scenario_id="S")
        self.assertIs(kept, error.evidence)                 # 정리 결과를 합성으로 덮지 않는다
        self.assertEqual(kept["workload_id"], "already")    # 있는 값은 유지
        self.assertEqual(kept["scenario_id"], "S")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class RunWorkloadPocEndToEnd(unittest.TestCase):
    """`run_cmd._run_workload`(실 per-workload CLI 함수) → coordinator 세션 위임 → production RF-off handler
    → 실 child·socket·provider PoC 실행."""

    def test_run_workload_executes_poc_with_real_child(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-poc")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic, poc_ctx=_ctx(poc_path), poc_id=None)
            # scenario 판정과 독립으로 PoC evidence가 붙는다(§11.1).
            self.assertEqual(result["status"], "PASS")
            poc = result.get("poc")
            self.assertIsNotNone(poc, "\n".join(lines))
            self.assertEqual(poc["poc_infrastructure"], "COMPLETED")
            self.assertEqual(len(poc["attacks"]), 1)
            self.assertEqual(poc["attacks"][0]["action_execution"], "TX_COMPLETED")
            self.assertEqual(poc["attacks"][0]["target"], "ROC")
            self.assertEqual(poc["radiation"]["radiated_children"], 2)
            self.assertEqual(result["poc_severity"], "OK")
            self.assertTrue(any("poc via coordinator" in ln for ln in lines))

    def test_poc_off_workload_has_no_poc_resources(self):
        # poc_ctx 없음(=PoC OFF) → result에 poc 없음, 자원 0(구조적 0), 요청에 poc 없음.
        with tempfile.TemporaryDirectory() as run_root:
            session = _PocDeviceSession(run_root, "run-off")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic)
            self.assertNotIn("poc", result)
            self.assertIsNone(session._requests[result["workload_id"]]["poc"])
            self.assertIsNone(session.seams)

    def test_missing_device_seams_fails_closed(self):
        # session이 device 경계를 제공하지 않으면 조용히 software로 대체하지 않고 fail-closed.
        class _NoSeamSession(_PocDeviceSession):
            poc_device_seams = None  # 미제공
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoSeamSession(run_root, "run-noseam")
            lines = []
            result = run_cmd._run_workload(
                session, "SC-VDE-006", "normal", out=lines.append,
                clock=time.monotonic, poc_ctx=_ctx(poc_path), poc_id=None)
            self.assertEqual(result["poc"]["status"], "ERROR")
            self.assertEqual(result["poc_severity"], "POC_ABORT")


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


class _RejectSession(_PocDeviceSession):
    """action 하나가 RF 전 거부(REJECTED_BEFORE_TX)되는 session — session inhibit 아님(R2)."""
    def poc_device_seams(self):
        from vdeter.poc.integration.rf_off import rf_off_seams
        self.seams = rf_off_seams(fail_on_retune=True)
        return self.seams


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
    검증하고, SESSION_INHIBIT 생성은 실 child의 실제 결과(POC_TX_OUTCOME_UNKNOWN)에서 나온다. PoC 실행은
    coordinator 세션 fake가 production RF-off handler로 처리한다(위 module docstring).
    """

    # --- R1: coordinator가 scenario 전에 arm; 준비/ARMED 실패면 fail-closed(PoC action 0) ---
    def test_r1_arm_failure_is_fail_closed_with_zero_poc_action(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.poc.testing.execution_fakes import write_poc_package
            write_poc_package(poc_dir, "armfail", poc_id="armfail", script=_ARM_FAIL_POC)
            poc_path = os.path.join(poc_dir, "armfail.py")
            session = _PocDeviceSession(run_root, "run-armfail")
            lines = []
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal", out=lines.append,
                                           clock=time.monotonic, poc_ctx=_ctx(poc_path), poc_id=None)
            # coordinator(RF-off handler)가 arm 실패를 fail-closed evidence로 회신한다: TX 0, severity POC_ABORT,
            # workload 상태는 READY로 복귀(다음 workload 가능). CLI는 evidence를 그대로 반영한다.
            self.assertIn(result["poc"]["poc_infrastructure"], ("ERROR", "ABORTED"), "\n".join(lines))
            self.assertEqual(result["poc_severity"], "POC_ABORT")
            self.assertEqual(session.seams.group_tx.committed_groups, 0)
            from apps import rf_off_software_session as rf_off
            self.assertEqual(session.runtime.state, rf_off.build_rf_off_runtime().state)

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
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=_ctx(poc_path), poc_id=None)
            poc = result["poc"]
            self.assertEqual(poc["attacks"][0]["action_execution"], "TX_UNKNOWN")
            self.assertEqual(poc["poc_infrastructure"], "ABORTED")

    # --- R4: scenario FAIL → PoC scenario_verdict FAIL, 최종 JSON 일관 ---
    def test_r4_scenario_fail_propagates_to_poc_verdict(self):
        """매핑의 production 소유자는 실물 controller의 `_finalize_workload`다(RF-off 세션은 scenario를
        돌리지 않아 PASS 고정). 실 child로 arm한 PoC를 scenario FAIL로 종결해 poc-result를 확인한다."""
        from apps import mass_roc_workload_controller as controller
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-scfail")          # device 경계 공급자
            request = poc_run.workload_request(_ctx(poc_path)["option"], poc_id=None, src_path=_SRC)
            snapshot = poc_run.snapshot_for_request(
                request, scenario_id="SC-VDE-006", dest_dir=os.path.join(run_root, "poc-snapshot"))
            prepared = poc_run.prepare_workload(session, snapshot, scenario_id="SC-VDE-006",
                                                run_id="run-scfail", workload_id="w-scfail",
                                                src_path=_SRC)
            trace = _Trace()
            rc = controller._finalize_workload(
                arguments=NS(run_id="run-scfail", run_root=run_root, lifecycle="session",
                             scenario_id="SC-VDE-006", certificate_case="normal"),
                workload_id="w-scfail", results={"SC-VDE-006": {"status": "FAIL"}},
                run_error=None, cleanup_errors=[], poc_prepared=prepared, poc_evidence=None,
                poc_failure=None, poc_host=None, scenario_success=False,
                events=NS(baseline=None, records={}, poll=lambda: None), trace=trace,
                inline_controller=NS(command_log=[]), controller=None)
            self.assertEqual(rc, 1)                                       # scenario FAIL
            events_dir = Path(run_root, "mass-roc-events")
            poc = json.loads((events_dir / "poc-result.json").read_text(encoding="utf-8"))
            self.assertEqual(poc["scenario_verdict"], "FAIL")
            self.assertEqual(poc["poc_infrastructure"], "COMPLETED")      # PoC 판정은 독립(§11.1)
            final = json.loads((events_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "FAIL")
            self.assertTrue(any(kind == "poc_result" for kind, _ in trace.rows))

    def test_r4_cli_keeps_scenario_and_poc_verdicts_independent(self):
        # coordinator가 scenario FAIL + PoC evidence를 함께 회신하면 CLI는 둘을 재계산하지 않는다.
        class _ScenarioFailSession(_PocDeviceSession):
            def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
                return {"status": "FAIL", "quiescence": {"pass": True},
                        "poc": {"status": "COMPLETED", "poc_infrastructure": "COMPLETED",
                                "scenario_verdict": "FAIL", "attacks": []}}
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            result = run_cmd._run_workload(_ScenarioFailSession(run_root, "run-scfail2"), "SC-VDE-006",
                                           "normal", out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=_ctx(poc_path), poc_id=None)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["poc"]["scenario_verdict"], "FAIL")
            self.assertEqual(result["poc"]["poc_infrastructure"], "COMPLETED")
            self.assertEqual(result["poc_severity"], "OK")

    # --- R5: 최종 JSON에 Stage 5 provider evidence(cleanup 전 snapshot) ---
    def test_r5_provider_evidence_in_final_json(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-evd")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=_ctx(poc_path), poc_id=None)
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
            self._plan_run(session, _ctx(poc_path), [("SC-VDE-006", "normal"), ("SC-VDE-006", "normal")])
            self.assertEqual(len(session.submitted), 1, "second workload must NOT be submitted")

    def test_r2_request_reject_does_not_inhibit_session(self):
        # RF 전 단일 거부(REJECTED_BEFORE_TX)는 session inhibit이 아니다 — 다음 workload 계속.
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _RejectSession(run_root, "run-reject")
            self._plan_run(session, _ctx(poc_path), [("SC-VDE-006", "normal"), ("SC-VDE-006", "normal")])
            self.assertEqual(len(session.submitted), 2, "REQUEST_REJECTED must not stop session")

    def test_r2_session_inhibit_stops_interactive_menu(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _NoTerminalSession(run_root, "run-menu")
            fed = _feeder(["SC-VDE-006", "SC-VDE-006", "q"])
            run_cmd._menu(session, out=lambda l: None, input_fn=fed,
                          clock=time.monotonic, poc_ctx=_ctx(poc_path, interactive=True))
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
            ctx = {"module": poc_run, "option": option, "src_path": _SRC}
            run_cmd._menu(session, out=lambda l: None, input_fn=fed,
                          clock=time.monotonic, poc_ctx=ctx)
            self.assertEqual(session.submitted, [])

    # --- R9: direct --poc 미지원 required capability는 import·spawn 전에 거부 ---
    def test_r9_direct_unsupported_capability_rejected_before_spawn(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            marker = os.path.join(poc_dir, "imported.marker")
            from vdeter.poc.testing.execution_fakes import write_poc_package
            script = ("from pathlib import Path\nPath(%r).write_text('x')\n" % marker
                      + "class Poc:\n    def on_start(self, c): return []\n"
                        "    def on_event(self, e, c): return []\n"
                        "    def on_finish(self, c): return None\n").encode("utf-8")
            write_poc_package(poc_dir, "dual", poc_id="dual", script=script,
                              required_capabilities=("observe_dual_physical_channel",),
                              capabilities=("observe_packet", "observe_dual_physical_channel"))
            poc_path = os.path.join(poc_dir, "dual.py")
            session = _PocDeviceSession(run_root, "run-cap")
            result = run_cmd._run_workload(session, "SC-VDE-006", "normal",
                                           out=lambda l: None, clock=time.monotonic,
                                           poc_ctx=_ctx(poc_path), poc_id=None)
            # coordinator가 선택 단계에서 거부(record ERROR) → CLI 결과는 PASS가 아니라 다음 workload 제출 0.
            # import·spawn 전 거부: import marker 미생성, device 경계(seam) 조립도 없다.
            self.assertNotEqual(result["status"], "PASS")
            self.assertIn("error", result)
            self.assertFalse(os.path.exists(marker), "researcher import must not run")
            self.assertIsNone(session.seams, "device boundary must not be assembled for a refused PoC")


def _write_poc_compat(poc_dir, base, compat):
    from vdeter.poc.testing.execution_fakes import write_poc_package
    write_poc_package(poc_dir, base, poc_id=base.replace("_", "-"),
                      script=_REACTIVE_REPLAY, compatible_scenarios=compat)
    return os.path.join(poc_dir, base + ".py")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class ExecuteLevelPocEndToEnd(unittest.TestCase):
    """실 사용자 CLI 본체 `run_cmd.execute(... --poc)` 종단 — gate → bring-up → workload request(poc) →
    coordinator 세션의 PoC(production RF-off handler, 실 child spawn·ARMED·finish/evidence/cleanup) →
    wait → **저장된 summary.json**.

    coordinator session만 가짜(기존 test_standalone_run_cli 하네스; 실물 controller 경로처럼 scenario 결과에
    PoC evidence를 함께 싣는다)이고 PoC child·socket·SDK·engine·provider·snapshot·CLI 호출 순서·최종 JSON은
    실제다. 수용 기준: 실제 CLI 호출 순서, 다음 workload 허용, 최종 저장 JSON.
    """

    def _factory(self, *, terminal=True):
        import test_standalone_run_cli as harness
        from apps import rf_off_software_session as rf_off
        base_cls = harness.make_session_factory()

        class PocFakeSession(base_cls):
            poc_workloads_in_coordinator = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._poc_requests = {}
                self.runtime = rf_off.build_rf_off_runtime()

            def submit_workload(self, scenario_id, certificate_case="normal", *,
                                on_settle=None, poc=None):
                workload_id = super().submit_workload(scenario_id, certificate_case,
                                                      on_settle=on_settle)
                if poc is not None:
                    self._poc_requests[workload_id] = {"workload_id": workload_id,
                                                       "scenario_id": scenario_id, "poc": poc}
                return workload_id

            def wait_result(self, workload_id, **kwargs):
                result = super().wait_result(workload_id, **kwargs)
                request = self._poc_requests.get(workload_id)
                if request is not None:
                    # 실물 controller 경로처럼 scenario 결과에 PoC evidence를 함께 싣는다.
                    record = rf_off.poc_workload_record(self, self.runtime, request,
                                                        run_root=self.run_root,
                                                        run_id=self.kwargs["run_id"])
                    if record["status"] != "PASS":
                        result["status"] = "FAIL"
                        result["error"] = record.get("error")
                    else:
                        result["poc"] = record["poc"]
                return result

            def poc_device_seams(self):
                from vdeter.poc.integration.rf_off import rf_off_seams
                return rf_off_seams()

            def poc_workload_steps(self, scenario_id):
                from vdeter.poc.execution.runner import observation_step
                from vdeter.poc.testing.fakes import make_observation
                return [observation_step(make_observation(
                    source_station="ROC", destination_station="MASS-A"))]

            def poc_terminal_source(self):
                if not terminal:
                    return None
                from vdeter.poc.integration.rf_off import rf_off_terminal_source
                return rf_off_terminal_source()

        PocFakeSession.instances = base_cls.instances
        return PocFakeSession, harness

    def _summary(self, factory):
        sess = factory.instances[-1]
        return sess, json.loads((sess.run_root / "summary.json").read_text(encoding="utf-8"))

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
    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None, poc=None):
        self.submitted.append((scenario_id, certificate_case))
        raise RuntimeError("injected submit failure")


class _WaitInterruptSession(_PocDeviceSession):
    def submit_workload(self, scenario_id, certificate_case="normal", *, on_settle=None, poc=None):
        self.submitted.append((scenario_id, certificate_case))
        return "actual-session-workload"

    def wait_result(self, workload_id, *, timeout_s=None, on_trace=None, on_poll=None):
        raise KeyboardInterrupt("injected wait interrupt")


@unittest.skipUnless(_LINUX, "실 subprocess·SOCK_SEQPACKET 전용")
class PocCleanupN1N2Test(unittest.TestCase):
    """N1/N2 — armed PoC의 cleanup-only 정리, 신규 action 0, 원형 예외/인터럽트 보존, COMPLETED 금지.
    실 child·socket·RF-off provider.

    N1의 production 소유자는 coordinator다: 실물 controller가 scenario 경로 실패·인터럽트에서
    `poc_run.abort_prepared`(finally/`_finalize_workload`)를 부른다. 여기서는 실 child로 arm한 workload에
    그 함수를 그대로 적용하고, CLI 쪽은 PoC를 arm하지 않으며 세션 오류를 원형 그대로 재전달함을 확인한다.
    """

    def _armed(self, session, poc_path, run_id):
        request = poc_run.workload_request(_ctx(poc_path)["option"], poc_id=None, src_path=_SRC)
        snapshot = poc_run.snapshot_for_request(
            request, scenario_id="SC-VDE-006",
            dest_dir=os.path.join(str(session.run_root), "poc-snapshot"))
        return poc_run.prepare_workload(session, snapshot, scenario_id="SC-VDE-006",
                                        run_id=run_id, workload_id=run_id + "-w1", src_path=_SRC)

    def _assert_cleanup_only(self, workload, prep, evidence):
        res = workload.connection.residuals()
        self.assertFalse(res["child_alive"], res)            # armed PoC child 정리됨
        self.assertFalse(res["socket_file_exists"], res)
        self.assertFalse(res["workload_dir_exists"], res)
        self.assertEqual(workload.seams.group_tx.committed_groups, 0)   # 신규 action 0
        self.assertIs(prep["evidence"], evidence)
        self.assertIn(evidence["poc_infrastructure"], ("ABORTED", "ERROR"))
        self.assertNotEqual(evidence["poc_infrastructure"], "COMPLETED")

    def test_n1_scenario_path_failure_aborts_armed_poc_cleanup_only(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-n1submit")
            workload = self._armed(session, poc_path, "run-n1submit")
            prep, lines = {"workload": workload}, []
            evidence = poc_run.abort_prepared(prep, cause=RuntimeError("injected submit failure"),
                                              out=lines.append)
            self._assert_cleanup_only(workload, prep, evidence)
            self.assertTrue(any("poc aborted" in ln and "RuntimeError" in ln for ln in lines), lines)

    def test_n1_scenario_path_interrupt_aborts_armed_poc_cleanup_only(self):
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            poc_path = _write_poc(poc_dir)
            session = _PocDeviceSession(run_root, "run-n1wait")
            workload = self._armed(session, poc_path, "run-n1wait")
            prep = {"workload": workload}
            evidence = poc_run.abort_prepared(prep, cause=KeyboardInterrupt("injected wait interrupt"),
                                              out=lambda _: None)
            self._assert_cleanup_only(workload, prep, evidence)

    def test_n1_cli_arms_nothing_and_reraises_session_errors(self):
        # CLI는 PoC를 arm하지 않는다(spawn 0) — 세션 submit/wait의 예외·인터럽트는 원형 그대로 재전달된다.
        for session_cls, expected in ((_SubmitFailSession, RuntimeError),
                                      (_WaitInterruptSession, KeyboardInterrupt)):
            with self.subTest(session=session_cls.__name__), \
                    tempfile.TemporaryDirectory() as poc_dir, \
                    tempfile.TemporaryDirectory() as run_root:
                os.chmod(poc_dir, 0o700)
                poc_path = _write_poc(poc_dir)
                session = session_cls(run_root, "run-n1cli")
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
                                              clock=time.monotonic, poc_ctx=_ctx(poc_path), poc_id=None)
                    except BaseException as exc:  # noqa: BLE001
                        caught = exc
                self.assertIsInstance(caught, expected)          # 원형 재전달(덮어쓰기 없음)
                self.assertEqual(captured, [])                   # CLI 프로세스는 PoC를 spawn하지 않는다
                self.assertIsNone(session.seams)

    def test_n2_arm_interrupt_reraises_original_no_action_no_completed(self):
        # ARMED 직후 KeyboardInterrupt: PocRunError로 변환하지 않고 원형 재전달, cleanup 중 신규 event/
        # action 0(TX group 0), 결과 COMPLETED 아님. 실 child.
        from vdeter.poc.integration.entry import select_and_snapshot
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.poc.testing.execution_fakes import write_poc_package
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
        from vdeter.poc.integration.entry import select_and_snapshot
        with tempfile.TemporaryDirectory() as poc_dir, \
                tempfile.TemporaryDirectory() as run_root:
            os.chmod(poc_dir, 0o700)
            from vdeter.poc.testing.execution_fakes import write_poc_package
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
                summary = json.loads(runs[-1].read_text(encoding="utf-8"))
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

    def test_real_coordinator_plan_catalog_preselected_baseline_and_poc_in_one_session(self):
        """진단 계획이 정한 `(scenario, case, poc_id)` 조합이 실 coordinator·controller까지 통한다.

        catalog(`--poc-dir`)를 **비대화형**으로 열되 prompt 없이 계획의 poc_id로 고른다(확정 선택).
        같은 세션에서 PoC workload 다음에 baseline workload가 실행되고(동일 세션 전환), forged
        case가 계획대로 전달된다. controller가 위임 요청을 확정 선택으로 해석하지 못하면 첫
        workload가 비대화형 catalog 거부로 **arm 실패**한다 — 이 시험이 그 회귀를 잡는다.

        baseline은 `select_and_snapshot`이 호환성 판정 전에 None을 돌려주므로 006 전용 catalog로도
        001 forged baseline이 성립한다(PoC 자원 0).
        """
        import test_standalone_run_cli as harness
        from apps.standalone.local_session import LocalCoordinatorSession
        with tempfile.TemporaryDirectory() as poc_dir, tempfile.TemporaryDirectory() as tmp:
            os.chmod(poc_dir, 0o700)
            _write_poc(poc_dir)                          # poc_id "cli-replay", SC-VDE-006 호환
            paths, _ = harness.fx.prepare_environment(tmp)
            plan = [("SC-VDE-006", "normal", "cli-replay"),   # catalog에서 계획의 id로 선택
                    ("SC-VDE-006", "normal", None),           # 같은 세션의 baseline
                    ("SC-VDE-001", "forged", "none")]         # forged case + 명시 baseline
            code, lines = harness.run(paths, LocalCoordinatorSession, scenario=None,
                                      workload_plan=plan, poc_dir=poc_dir,
                                      rf_off_software=True)
            joined = chr(10).join(lines)
            runs = sorted(Path(paths.runs_dir).glob("*/summary.json"))
            self.assertTrue(runs, joined)
            summary = json.loads(runs[-1].read_text(encoding="utf-8"))
            workloads = summary["workloads"]
            self.assertEqual(len(workloads), 3, joined)
            first, second, third = workloads
            # 1) PoC workload — 계획의 id가 catalog에서 골라져 실 child가 실행됐다.
            self.assertEqual(first["status"], "PASS", joined)
            self.assertEqual(first["poc"]["poc_infrastructure"], "COMPLETED", joined)
            self.assertEqual(first["poc"]["poc_id"], "cli-replay")
            # 2) baseline — 같은 세션, PoC 자원 0. 실행됐다고 꾸미지 않는다.
            self.assertEqual(second["status"], "PASS", joined)
            self.assertEqual(second["poc"]["poc_infrastructure"], "NOT_RUN", joined)
            self.assertEqual(second["poc"]["status"], "BASELINE")
            # 3) forged case가 계획대로 전달됐고 명시 "none"도 baseline이다.
            self.assertEqual(third["certificate_case"], "forged")
            self.assertEqual(third["poc"]["poc_infrastructure"], "NOT_RUN", joined)
            self.assertEqual(summary["status"], "PASS", joined)
            self.assertEqual(code, cli.EXIT_PASS, joined)


if __name__ == "__main__":
    unittest.main()
