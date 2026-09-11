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


if __name__ == "__main__":
    unittest.main()
