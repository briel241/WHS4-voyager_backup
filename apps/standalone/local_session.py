"""로컬 coordinator 세션 계층 — SSH 없는 Stage 7 세션 구동 (Phase I W2).

host측 `apps/stage7_session.py`의 로컬 대응물. coordinator·launcher와
세션 디렉터리 파일 IPC 계약(`session/requests|results|shutdown`·`ready.json`·
`closed.json`·`done.rc`)은 **무수정**으로 쓴다 (README §3·§10, 결정 6·17).

host 판과 다른 점
    - spawn: SSH nohup 대신 명시적 argv `subprocess.Popen` (shell 문자열 금지).
    - IPC: 원격 cat/heredoc 대신 로컬 파일. request는 임시 파일 →
      `os.replace`로 원자 발행. result·ready는 JSON 파싱 성공을 완성 기준으로
      읽는다 (coordinator의 기록은 원자적이지 않을 수 있다).
    - done.rc: host 판에서는 ssh wrapper(`echo $? > done.rc`)가 만들었다.
      로컬에서는 coordinator가 직접 자식이므로 **이 계층이 회수한 exit code로
      done.rc를 기록**해 evidence 계약을 보존한다.
    - trace: 로컬 tail이라 폴링을 0.2~0.5초로 좁혀 발생 즉시 출력한다
      (결정 21). 표시 알갱이는 기존 trace 이벤트 단위 그대로.

신호 처리(SIGINT/SIGTERM)는 CLI 계층(run_cmd)이 담당하고, 이 클래스는
그 재료(`request_shutdown`·`wait_exit`·`escalate`·`finalize`)만 제공한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 파일 IPC·정착 계약 — host 세션 계층과 단일 출처 공유 (읽기 전용 재사용).
# SETTLE 계약: SC-VDE-001 직후에는 RX 파이프라인 배수 30초를 기다려야
# 다음 workload가 배압 꼬리에 착지하지 않는다 (host 실측 근거는
# apps/stage7_session.py의 상수 주석 참조 — standalone도 동일 적용).
from apps.stage7_session import (  # noqa: F401
    SESSION_CLOSED,
    SESSION_DIR_NAME,
    SESSION_READY,
    SESSION_REQUESTS,
    SESSION_RESULTS,
    SESSION_SHUTDOWN,
    SETTLE_AFTER_SCENARIOS,
    WORKLOAD_SETTLE_SECONDS,
)

#: bring-up 상한 — host 실측 21~23분 + 여유 (host 판과 동일 값).
SESSION_BRINGUP_TIMEOUT_S = 2700.0
#: workload 하나의 상한 (host 판과 동일 값).
WORKLOAD_TIMEOUT_S = 900.0
#: 정상 종료(cleanup) 상한 (host 판과 동일 값).
SESSION_CLOSE_TIMEOUT_S = 600.0
#: 에스컬레이션 대기: terminate 후 / kill 후.
ESCALATE_TERM_WAIT_S = 30.0
ESCALATE_KILL_WAIT_S = 10.0

#: 로컬 폴링 주기 — ready/result는 0.5s, trace는 0.3s (결정 21 실시간 출력).
POLL_INTERVAL_S = 0.5
TRACE_POLL_INTERVAL_S = 0.3


class LocalSessionError(RuntimeError):
    """세션 구동 실패 — RF 접근 전이거나, 이후라면 cleanup 시도를 남긴다."""


def build_child_env(*, diagnostics: bool = False,
                    identity_root: Path | str | None = None) -> dict:
    """coordinator 자식 환경을 명시적으로 구성한다 (README §3-3).

    run(mass-roc-session)과 qualify(RR phase)가 같은 정책을 공유한다:
    ambient `STAGE7_*`·`PYTHONPATH`·`PYTHONHOME`·낡은
    `VDETER_STANDALONE_IDENTITY_ROOT` 전부 차단, 명시 인자만 주입.
    """
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("STAGE7_")
           and key not in ("PYTHONPATH", "PYTHONHOME",
                           "VDETER_STANDALONE_IDENTITY_ROOT")}
    env["PYTHONPYCACHEPREFIX"] = os.path.join(
        tempfile.gettempdir(), "vdeter-standalone-pyc")
    if diagnostics:
        env["STAGE7_DIAG"] = "1"
    if identity_root is not None:
        env["VDETER_STANDALONE_IDENTITY_ROOT"] = str(identity_root)
    return env


def _read_json_if_complete(path: Path):
    """JSON 파싱이 성공할 때만 내용을 돌려준다 (미완성 기록은 None)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class _FileTail:
    """증분 기록 파일의 새 내용만 돌려주는 로컬 tail (offset 기억)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._buffer = b""

    def new_lines(self) -> list:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        if not chunk:
            return []
        self._offset += len(chunk)
        self._buffer += chunk
        lines = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if text:
                lines.append(text)
        return lines


class LocalCoordinatorSession:
    """coordinator 프로세스 하나를 로컬 자식으로 소유·감시·회수한다."""

    # Both production and RF-off requests are owned by the coordinator child.
    poc_workloads_in_coordinator = True

    def __init__(self, *, repo_root: Path | str, run_id: str,
                 run_root: Path | str, configs_dir: Path | str,
                 gain_artifact_path: Path | str,
                 baseline_sha256: str,
                 phase: str = "mass-roc-session",
                 python_executable: str | None = None,
                 spawn=subprocess.Popen,
                 clock=time.monotonic, sleeper=time.sleep,
                 diagnostics: bool = False,
                 residual_probe=None,
                 use_process_group: bool | None = None,
                 group_signal=None,
                 identity_root: Path | str | None = None,
                 rf_off_software: bool = False) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.run_root = Path(run_root)
        self.configs_dir = Path(configs_dir)
        self.gain_artifact_path = Path(gain_artifact_path)
        self.baseline_sha256 = baseline_sha256
        self.phase = phase
        self.python_executable = python_executable or sys.executable
        self.spawn = spawn
        self.clock = clock
        self.sleeper = sleeper
        self.diagnostics = bool(diagnostics)
        #: 잔존 프로세스 probe (finalize의 worker-0 검증 — README §10-5).
        #: None이면 기본(pgrep) 구현을 쓴다. 시험에서 주입한다.
        self.residual_probe = residual_probe
        #: Linux에서는 coordinator를 독립 process group(setsid)으로 시작하고,
        #: 강제 에스컬레이션은 그 group 전체를 대상으로 한다 — coordinator만
        #: 죽이면 launcher/worker가 고아로 남을 수 있다. 정상 shutdown 마커
        #: 경로는 이와 무관하게 그대로다.
        self._use_process_group = (use_process_group if use_process_group
                                   is not None else os.name == "posix")
        #: group signal 전달자 (pid, signal_name) — 시험에서 주입한다.
        self.group_signal = group_signal
        #: standalone identity 저장소 root — 지정 시 launcher가 배포 tree
        #: 밖 identity(키·trust store)를 쓴다 (W4 보완 1). None이면 기존
        #: configs/stage7/trust 동작 그대로.
        self.identity_root = (Path(identity_root)
                              if identity_root is not None else None)
        #: Stage 8 RF-off software 모드 — coordinator를 B210 없이 in-process PoC 실행 세션으로
        #: 띄운다(하드웨어 경계 명시 double 검증 전용). production 실장비 실행은 False.
        self.rf_off_software = bool(rf_off_software)
        #: 정착 대기 상태 (host 계약 승계).
        self._last_workload_end = None
        self._last_scenario_id = None
        self._pending_scenario = {}

        self.session_dir = self.run_root / SESSION_DIR_NAME
        self.requests_dir = self.session_dir / SESSION_REQUESTS
        self.results_dir = self.session_dir / SESSION_RESULTS
        self.done_rc_path = self.run_root / "done.rc"
        self.console_log_path = self.run_root / "coordinator-console.log"

        self.process = None
        self.launch_attempted = False
        self._console_tail = _FileTail(self.console_log_path)
        self._console_handle = None
        self._workload_counter = 0
        self._escalation = []  # 진단용 이력 ("terminate", "kill" 등)

    # ---- spawn ----------------------------------------------------------

    def build_argv(self) -> list:
        if self.rf_off_software:
            # Stage 8 RF-off software 세션은 **검증 전용**이므로 자격 결속된
            # `stage7_coordinator_main.main()`을 거치지 않고 자체 entry point로 띄운다.
            # (그 함수에 분기를 넣으면 Phase-I function binding이 바뀌어 RF 접근이 거부된다 —
            #  자격 경로는 byte-identical로 보존한다.) B210 launcher는 spawn하지 않는다.
            return [
                self.python_executable,
                str(self.repo_root / "apps" / "stage8_rf_off_session.py"),
                "--run-id", self.run_id,
                "--run-root", str(self.run_root),
            ]
        return [
            self.python_executable,
            str(self.repo_root / "VDETER" / "apps"
                / "stage7_coordinator_main.py"),
            "--run-id", self.run_id,
            "--phase", self.phase,
            "--run-root", str(self.run_root),
            "--configs-dir", str(self.configs_dir),
            "--gain-artifact", str(self.gain_artifact_path),
            "--baseline-sha256", self.baseline_sha256,
        ]

    def build_env(self) -> dict:
        """`build_child_env` 위임 — run/qualify 공통 env 정책 (README §3-3)."""
        return build_child_env(diagnostics=self.diagnostics,
                               identity_root=self.identity_root)

    def start(self) -> None:
        if self.process is not None:
            raise LocalSessionError("session already started")
        self.run_root.mkdir(parents=True, exist_ok=True)
        # spawn 시도 사실을 먼저 기록한다 — spawn 예외가 나도 finalize가
        # bounded cleanup 확인을 생략하지 않게 (host 판과 같은 원칙).
        self.launch_attempted = True
        self._console_handle = open(self.console_log_path, "ab")
        spawn_kwargs = {
            "cwd": str(self.repo_root / "VDETER"),
            "env": self.build_env(),
            "stdout": self._console_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if self._use_process_group:
            # POSIX 전용 — 자식이 setsid로 독립 session/group 리더가 된다.
            spawn_kwargs["start_new_session"] = True
        try:
            self.process = self.spawn(self.build_argv(), **spawn_kwargs)
        except Exception as exc:
            self._close_console_handle()
            raise LocalSessionError(
                f"coordinator spawn failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _close_console_handle(self) -> None:
        if self._console_handle is not None:
            try:
                self._console_handle.close()
            except OSError:
                pass
            self._console_handle = None

    # ---- 상태 감시 ------------------------------------------------------

    def coordinator_exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def coordinator_rc(self):
        if self.process is None:
            return None
        return self.process.poll()

    def console_stage_lines(self) -> list:
        """console log의 새 `[coordinator]` 단계 줄 (진행 표시용)."""
        prefix = "[coordinator] "
        return [line[len(prefix):]
                for line in self._console_tail.new_lines()
                if line.startswith(prefix)]

    def wait_ready(self, *, timeout_s: float = SESSION_BRINGUP_TIMEOUT_S,
                   on_stage=None) -> dict:
        """`session/ready.json`을 bounded 대기한다. 실패는 예외."""
        if self.process is None:
            raise LocalSessionError("session not started")
        deadline = self.clock() + timeout_s
        ready_path = self.session_dir / SESSION_READY
        while self.clock() < deadline:
            ready = _read_json_if_complete(ready_path)
            if ready is not None:
                return ready
            if on_stage is not None:
                for line in self.console_stage_lines():
                    on_stage(line)
            if self.coordinator_exited():
                raise LocalSessionError(
                    "coordinator exited during bring-up "
                    f"(rc={self.coordinator_rc()})")
            self.sleeper(POLL_INTERVAL_S)
        raise LocalSessionError(
            f"bring-up timeout ({timeout_s:.0f}s) — RF run aborted")

    # ---- workload -------------------------------------------------------

    def next_workload_id(self, scenario_id: str, certificate_case: str) -> str:
        self._workload_counter += 1
        tag = scenario_id.lower().replace("sc-vde-", "sc")
        if scenario_id == "SC-VDE-001":
            tag += "-" + certificate_case
        return f"{self.run_id}-{tag}-{self._workload_counter:03d}"

    def settle_before_next_workload(self, *, on_settle=None) -> float:
        """직전 시나리오가 정착 대상이면 남은 배수 시간을 기다린다.

        host 계약 승계: `SETTLE_AFTER_SCENARIOS`(SC-VDE-001) 직후에만
        `WORKLOAD_SETTLE_SECONDS`(30s)에서 경과분을 뺀 만큼 대기. 반환값은
        실제 대기한 초 (0.0이면 대기 없음)."""
        if (self._last_workload_end is None
                or self._last_scenario_id not in SETTLE_AFTER_SCENARIOS):
            return 0.0
        remaining = WORKLOAD_SETTLE_SECONDS - (
            self.clock() - self._last_workload_end)
        if remaining <= 0:
            return 0.0
        if on_settle is not None:
            on_settle(remaining, self._last_scenario_id)
        self.sleeper(remaining)
        return remaining

    def submit_workload(self, scenario_id: str,
                        certificate_case: str = "normal", *,
                        on_settle=None, poc=None) -> str:
        """request를 원자 발행한다 (임시명은 `.json`으로 끝나지 않는다 —
        coordinator의 `endswith('.json')` 목록에 미완성본이 잡히지 않게).
        발행 전에 host 계약대로 정착 대기를 적용한다.

        `poc`(dict)를 주면 request에 실어 coordinator가 workload-local PoC를 실행하게 한다.
        None이면 기존 scenario workload 그대로다.

        일반 dispatch도 이제 PoC를 처리한다: coordinator가 요청을 workload child에 넘기고, child가
        PoC supervisor를 호스팅한 뒤 evidence를 결과에 싣는다. PoC를 요청했는데 evidence가 없으면
        coordinator가 workload를 FAIL로 확정하므로, PoC 없이 시나리오만 돌고 PASS로 보고되는 거짓
        성공은 성립하지 않는다."""
        self.settle_before_next_workload(on_settle=on_settle)
        workload_id = self.next_workload_id(scenario_id, certificate_case)
        self._pending_scenario[workload_id] = scenario_id
        request = {
            "workload_id": workload_id,
            "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "requested_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if poc is not None:
            request["poc"] = dict(poc)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        final = self.requests_dir / (workload_id + ".json")
        tmp = self.requests_dir / (workload_id + ".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(request, handle, ensure_ascii=True, sort_keys=True)
        os.replace(tmp, final)
        return workload_id

    def workload_trace_path(self, workload_id: str) -> Path:
        return (self.run_root / "workloads" / workload_id
                / "workload-trace.jsonl")

    def wait_result(self, workload_id: str, *,
                    timeout_s: float = WORKLOAD_TIMEOUT_S,
                    on_trace=None) -> dict:
        """결과 JSON을 bounded 대기하며 trace를 발생 즉시 흘린다 (결정 21)."""
        deadline = self.clock() + timeout_s
        result_path = self.results_dir / (workload_id + ".json")
        trace_tail = _FileTail(self.workload_trace_path(workload_id))

        def drain_trace():
            if on_trace is None:
                return
            for line in trace_tail.new_lines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                on_trace(row)

        while self.clock() < deadline:
            drain_trace()
            result = _read_json_if_complete(result_path)
            if result is not None:
                drain_trace()  # 결과가 먼저 완성돼도 남은 trace를 마저 흘린다
                # 정착 대기 계산용 상태 (host 계약 승계).
                self._last_workload_end = self.clock()
                self._last_scenario_id = self._pending_scenario.get(workload_id)
                return result
            if self.coordinator_exited():
                raise LocalSessionError(
                    f"workload {workload_id}: session exited during the workload "
                    f"(rc={self.coordinator_rc()})")
            self.sleeper(TRACE_POLL_INTERVAL_S)
        raise LocalSessionError(f"workload {workload_id} timeout")

    # ---- 종료 (결정 17 / README §10) ------------------------------------

    def request_shutdown(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        marker = self.session_dir / SESSION_SHUTDOWN
        with open(marker, "wb"):
            pass

    def wait_exit(self, timeout_s: float) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            if self.coordinator_exited():
                return True
            self.sleeper(POLL_INTERVAL_S)
        return self.coordinator_exited()

    def _default_group_signal(self, pid: int, signal_name: str) -> None:
        import signal as _signal
        os.killpg(pid, getattr(_signal, "SIG" + signal_name))

    def _escalate_step(self, signal_name: str) -> None:
        """TERM/KILL 한 단계 — process group 우선, 실패 시 단일 프로세스.

        setsid로 시작했으므로 group id == coordinator pid다. group 전달이
        실패하면(권한·이미 소멸 등) 단일 프로세스 신호로 후퇴하고 이력에
        남긴다. 정상 shutdown 마커 경로는 이 함수와 무관하다.
        """
        if self._use_process_group:
            sender = (self.group_signal if self.group_signal is not None
                      else self._default_group_signal)
            try:
                sender(self.process.pid, signal_name)
                self._escalation.append(signal_name.lower() + "-group")
                return
            except OSError as exc:
                self._escalation.append(
                    f"{signal_name.lower()}-group-failed:{exc}")
        try:
            if signal_name == "TERM":
                self.process.terminate()
            else:
                self.process.kill()
            self._escalation.append(
                "terminate" if signal_name == "TERM" else "kill")
        except OSError:
            pass

    def escalate(self) -> None:
        """TERM → bounded 대기 → KILL → bounded 대기 (진단 이력 기록).

        Linux에서는 coordinator의 process group 전체가 대상이다 —
        coordinator만 죽이면 launcher/worker가 고아로 남을 수 있다.
        """
        if self.process is None or self.coordinator_exited():
            return
        self._escalate_step("TERM")
        if self.wait_exit(ESCALATE_TERM_WAIT_S):
            return
        self._escalate_step("KILL")
        self.wait_exit(ESCALATE_KILL_WAIT_S)

    def _default_residual_probe(self):
        """pgrep 기반 잔존 검사. 사용 불가 환경에서는 None (미확인)."""
        import shutil as _shutil
        if _shutil.which("pgrep") is None:
            return None
        from apps.stage7_service_layer import _COMPETING_PROCESS_PATTERN
        done = subprocess.run(
            ["pgrep", "-af", _COMPETING_PROCESS_PATTERN],
            capture_output=True, text=True, timeout=30)
        lines = [line.strip() for line in (done.stdout or "").splitlines()
                 if line.strip() and "pgrep" not in line
                 and str(os.getpid()) != line.split()[0]]
        return lines

    def finalize(self) -> dict:
        """종료 결과를 정직하게 판정한다 (성공 조건은 전부 실측).

        성공(pass=True) 조건 — 하나라도 미충족이면 실패로 보고한다:
        coordinator 종료 확인, rc==0, closed.json 존재·status PASS·
        cleanup.pass, 잔존 프로세스 0 (probe 불가 환경도 실패로 계상 —
        fail-closed). done.rc는 이 계층이 회수 rc로 기록해 계약을 보존한다.
        """
        problems = []
        exited = self.coordinator_exited()
        rc = self.coordinator_rc()
        if self.launch_attempted and not exited:
            problems.append("coordinator exit was not confirmed")

        # done.rc — 로컬 계층이 기록한다 (모듈 docstring 참조).
        if exited and rc is not None:
            try:
                existing = None
                if self.done_rc_path.exists():
                    existing = self.done_rc_path.read_text(
                        encoding="utf-8").strip()
                if existing is None:
                    tmp = self.done_rc_path.with_name("done.rc.tmp")
                    tmp.write_text(f"{rc}\n", encoding="utf-8")
                    os.replace(tmp, self.done_rc_path)
                elif existing != str(rc):
                    problems.append(
                        f"existing done.rc ({existing!r}) and reaped rc ({rc}) differ")
            except OSError as exc:
                problems.append(f"failed to record done.rc: {exc}")
        if exited and rc not in (0, None):
            problems.append(f"coordinator exited abnormally (rc={rc})")

        closed = _read_json_if_complete(self.session_dir / SESSION_CLOSED)
        if closed is None:
            problems.append("closed.json missing — cleanup unconfirmed")
        else:
            if closed.get("status") != "PASS":
                problems.append(f"close status={closed.get('status')!r}")
            if (closed.get("cleanup") or {}).get("pass") is not True:
                problems.append("cleanup pass is not true")

        probe = (self.residual_probe if self.residual_probe is not None
                 else self._default_residual_probe)
        try:
            residual = probe()
        except Exception as exc:  # noqa: BLE001 - probe 실패도 정직 계상
            residual = None
            problems.append(f"residual process probe failed: {exc}")
        if residual is None:
            problems.append("cannot check residual processes (probe unavailable)")
        elif residual:
            problems.append(f"residual processes found: {residual[:3]}")

        self._close_console_handle()
        return {
            "pass": not problems,
            "problems": problems,
            "coordinator_exited": exited,
            "coordinator_rc": rc,
            "launch_attempted": self.launch_attempted,
            "escalation": list(self._escalation),
            "closed": closed,
            "residual_processes": residual,
        }

    def close(self, *, close_timeout_s: float = SESSION_CLOSE_TIMEOUT_S) -> dict:
        """정상 종료 절차: shutdown 요청 → bounded 대기 → 필요 시에만
        에스컬레이션 → finalize. (첫 Ctrl+C/SIGTERM·정상 q·오류 종료 공통)"""
        if self.launch_attempted:
            try:
                self.request_shutdown()
            except OSError as exc:
                # 요청 실패도 finalize가 실패로 계상하도록 진행한다.
                self._escalation.append(f"shutdown-request-failed:{exc}")
            if not self.wait_exit(close_timeout_s):
                self.escalate()
        return self.finalize()
