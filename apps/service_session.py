"""Phase G 최종 모델 — bounded persistent Stage 7 service session (CLI 아님).

CLI 시작 시 하드웨어 session을 **한 번** 세우고(B210 acquire·endpoint START·
host fit·C1~C6 RF-time PRE/POST·active bridge·HOST_TIME_LOCKED·admission·
TBB·publish), 메뉴 선택마다 **application workload child만** 새로 만든다.
`q`나 fault에서 한 번만 전체 cleanup을 수행한다.

원격 coordinator와는 session 제어 디렉터리(파일)로 주고받는다 — 새로운 사용자용
CLI를 만들지 않기 위한 최소 IPC이며 직접 진입점으로 문서화하지 않는다.
정식 진입점은 `apps/scenario_runner.py menu --transport stage7 --profile ...`이다.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SESSION_DIR_NAME = "session"
SESSION_READY = "ready.json"
SESSION_SHUTDOWN = "shutdown"
SESSION_CLOSED = "closed.json"
SESSION_REQUESTS = "requests"
SESSION_RESULTS = "results"

#: session bring-up 상한 (C1~C6 time-lock 실측 약 21~22분 + 여유).
SESSION_BRINGUP_TIMEOUT_S = 2700.0
#: workload 하나의 상한.
WORKLOAD_TIMEOUT_S = 900.0
#: cleanup 상한.
SESSION_CLOSE_TIMEOUT_S = 600.0

_REQUEST_HEREDOC = "STAGE7_WORKLOAD_REQUEST_END"
#: workload 사이 RX 파이프라인 정착 시간(초).
#:
#: 근거 — run phase-g-service-20260901-145556 실측(16 workload):
#:   SC-VDE-001 normal 이 끝나며 멱등성 재전송을 하면 그 구간에서만
#:   디코드 큐가 약 10초간 배압 상태가 된다(배압 104~157회, 미뤄진 job
#:   5,740~10,063). 다음 workload 는 그 20초 뒤에 시작하므로 type-4 가
#:   배압 꼬리에 정확히 착지해 proc_lag 2,860~3,940ms → frames_late=1
#:   → LC 활성화 프레임 경과 → DC 수신 창 미계획 → 전송 timeout.
#:   SC-VDE-001 forged 가 4/4 이 경로로 실패했다.
#: 배압은 001 구간에서만 발생하고 005/006 은 같은 유입 스파이크(31/s)
#: 에서도 발생하지 않는다 — 유입량이 아니라 실버스트 창의 복호 비용
#: 차이다. 큐가 빈 뒤 다음 workload 를 시작하도록 유계 대기한다.
#:
#: orchestration 계층 정책이며 PHY/MAC/TDMA/RF 는 불변이다.
WORKLOAD_SETTLE_SECONDS = 30.0

#: 정착 대기를 요구하는 **직전** 시나리오.
#:
#: 배압 덩어리 4개가 전부 SC-VDE-001 구간에서만 났다. SC-VDE-005/006 은
#: 같은 유입 스파이크(31창/초)에서도 배압이 0회다 — 유입량이 아니라
#: 실버스트 창의 복호 비용 차이다. 001 만 페이로드가 가장 크고(411B)
#: 멱등성 재전송까지 해 실버스트 창이 가장 많다.
#: 필요 없는 곳까지 기다리면 16 workload 기준 7.5분을 버린다.
SETTLE_AFTER_SCENARIOS = frozenset({"SC-VDE-001"})


def _render_trace_event(row: dict) -> str | None:
    """workload-trace 이벤트 한 줄을 사람이 읽는 형태로 (없으면 None).

    evidence 는 원본 JSON 이 그대로 남는다 — 이 출력은 진행 가시화 전용이다.
    """

    event = row.get("event")
    step = row.get("step") or ""
    if event == "submit":
        sender = row.get("sender") or "?"
        receiver = row.get("receiver") or "?"
        return ("    %-16s 제출   %s  %s B%s"
                % (sender + " -> " + receiver,
                   row.get("payload_format") or "?",
                   row.get("payload_len"),
                   ("  [" + step + "]") if step else ""))
    if event == "transfer_terminal":
        return "    %-16s 전송   %s" % ("", row.get("terminal_state") or "?")
    if event == "data_received":
        receiver = row.get("receiver") or "?"
        if not row.get("present", True):
            return "    %-16s 수신   없음" % receiver
        return ("    %-16s 수신   %s -> %s"
                % (receiver, row.get("original_source"),
                   row.get("original_destination")))
    if event == "scenario_result":
        steps = row.get("steps") or []
        failed = [n for n, st in steps if st != "PASS"]
        tail = ("  실패: " + ", ".join(failed)) if failed else ""
        return ("    판정 %s  %s  %d단계%s"
                % (row.get("status"), row.get("scenario") or "", len(steps), tail))
    return None


class ServiceSession:
    """살아 있는 하드웨어 service session 하나. workload는 이 위에서만 돈다."""

    #: 계측용 session 상태 전이.
    STATES = ("NEW", "VALIDATING", "STARTING", "TIME_LOCKING",
              "SERVICE_ADMISSION", "READY", "WORKLOAD_RUNNING",
              "QUIESCING", "STOPPING", "CLOSED", "FAILED")

    def __init__(self, profile: dict, *, session_id: str, session_dir: str,
                 runner=subprocess.run, clock=time.time, sleeper=time.sleep,
                 trace=None, diagnostics: bool = False,
                 live: bool = True) -> None:
        self.profile = profile
        self.session_id = session_id
        self.session_dir = session_dir
        self.runner = runner
        self.clock = clock
        #: 정착 대기용 — 가짜 시계 하네스가 실제로 자지 않게 주입 가능.
        self.sleeper = sleeper
        #: 진행 상황을 터미널에 흘린다 (시연·긴 대기 가시화).
        #: evidence 는 별도 파일이라 이 출력은 기록이 아니다.
        self.live = bool(live)
        self.ready = None
        self.marks = {}
        self.workloads = []
        self.closed = None
        self.status = "PENDING"
        self.error = None
        self.local_validation = None
        self.remote_preflight = None
        self.config = None
        self.logs = []
        self.evidence_collection = None
        self._coordinator_launch_attempted = False
        self._coordinator_spawned = False
        self._counter = 0
        #: 직전 workload 종료 시각 (정착 대기 계산용).
        self._last_workload_end = None
        #: 직전 workload 의 시나리오 (정착 필요 여부 판정용).
        self._last_scenario_id = None
        self._started = clock()
        self.diagnostics = bool(diagnostics)
        if trace is None:
            from apps.service_trace import TraceWriter
            trace = TraceWriter(
                os.path.join(session_dir, "session-trace.jsonl"),
                session_id=session_id, diagnostics=self.diagnostics)
        self.trace = trace
        self.trace.state("NEW", profile_id=profile.get("profile_id"))

    # ---- 원격 경로 -------------------------------------------------------

    @property
    def remote_root(self) -> str:
        from apps.service_layer import remote_deploy_root
        return remote_deploy_root(self.profile)

    @property
    def remote_run_root(self) -> str:
        return f"{self.remote_root}/runs/{self.session_id}"

    @property
    def remote_session_dir(self) -> str:
        return f"{self.remote_run_root}/{SESSION_DIR_NAME}"

    def _ssh(self, script, *, timeout=300.0, check=True):
        from apps.service_layer import _ssh
        return _ssh(self.profile, script, timeout=timeout, check=check,
                    runner=self.runner)

    def _mark(self, name):
        self.marks[name] = round(self.clock() - self._started, 1)

    def _live(self, text: str) -> None:
        """진행 상황 한 줄 (fail-open — 출력 실패가 세션을 깨지 않는다)."""

        if not self.live:
            return
        try:
            print(text, flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _live_elapsed(self) -> str:
        seconds = int(self.clock() - self._started)
        return "%2d:%02d" % (seconds // 60, seconds % 60)

    def _remote_bringup_stage(self):
        """coordinator 콘솔의 마지막 단계 줄 (없으면 None)."""

        if not self.live:
            return None
        try:
            done = self._ssh(
                "grep -F '[coordinator]' "
                + self.remote_run_root + "/coordinator-console.log"
                + " 2>/dev/null | tail -1 || true",
                timeout=60.0, check=False)
            line = (done.stdout or "").strip()
            return line[len("[coordinator] "):] if line else None
        except Exception:  # noqa: BLE001
            return None

    def _remote_trace_tail(self, workload_id: str, seen: int):
        """workload-trace.jsonl 의 새 줄만 가져온다 (증분 기록이다)."""

        if not self.live:
            return []
        path = (self.remote_run_root + "/workloads/" + workload_id
                + "/workload-trace.jsonl")
        try:
            done = self._ssh(
                "tail -n +" + str(seen + 1) + " " + path + " 2>/dev/null || true",
                timeout=60.0, check=False)
        except Exception:  # noqa: BLE001
            return []
        rows = []
        for line in (done.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _remote_json(self, relpath):
        done = self._ssh(
            f"cat {self.remote_session_dir}/{relpath} 2>/dev/null || true",
            timeout=120.0, check=False)
        text = (done.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def _coordinator_exited(self) -> bool:
        done = self._ssh(
            f"test -f {self.remote_run_root}/done.rc && echo yes || echo no",
            timeout=60.0, check=False)
        return (done.stdout or "").strip().endswith("yes")

    def _coordinator_rc(self):
        done = self._ssh(f"cat {self.remote_run_root}/done.rc 2>/dev/null",
                         timeout=60.0, check=False)
        text = (done.stdout or "").strip()
        return int(text) if text.isdigit() else None

    # ---- session 시작 (§2) -----------------------------------------------

    def start(self) -> dict:
        """profile/reference 검증 → 원격 preflight → fresh config → bring-up."""
        from apps import run_profile
        from apps.service_layer import (
            ServiceSessionError, prepare_service_config,
            verify_remote_environment,
        )

        self.trace.state("VALIDATING")
        self.local_validation = run_profile.validate_local_bindings(self.profile)
        self._mark("local_validation")
        reference = self.profile["_reference"]
        self.trace.emit("profile_reference_validated",
                        checks=len(self.local_validation),
                        reference_sha256=reference["_reference_sha256"],
                        qualification_run_id=reference["qualification"]["run_id"])

        self.remote_preflight = verify_remote_environment(
            self.profile, runner=self.runner)
        self._mark("remote_preflight")
        preflight = self.remote_preflight
        self.trace.emit(
            "remote_preflight",
            critical=(preflight.get("critical_manifest") or {}).get("pass"),
            function_bound=list((preflight.get("function_bound") or {})),
            usb_topology=(preflight.get("usb_topology") or {}).get("pass"),
            trust=(preflight.get("trust") or {}).get("pass"),
            throttled=(preflight.get("throttled") or {}).get("value"))

        self.logs = []
        self.trace.state("STARTING")
        self.config = prepare_service_config(
            self.profile, self.session_id, self.logs, runner=self.runner)
        self._mark("config_prepared")
        self.trace.emit("config_prepared",
                        resolved_run_id=self.config["metadata"].get("run_id"),
                        git_dirty=self.config["metadata"].get("git_dirty"),
                        git_commit=self.config["metadata"].get("git_commit"))

        gain_rel = self.profile["gain"]["remote_artifact_input_relpath"]
        inner = (
            "PYTHONPYCACHEPREFIX=/tmp/stage7-session-pyc "
            # `--diagnostics`는 endpoint launcher의 진단 계측도 켠다. 이게 없으면
            # RX 창 계획·MAC 박동·unclean lane 사유가 통째로 기록되지 않아,
            # DEVICE_ERROR로 접힌 내부 code를 사후에 되살릴 수 없다
            # (phase-g-service-20260831-114945: [diag] 0줄, DEVICE_ERROR 458건 —
            #  성공 대조군 rr-p2-17-20260830k는 40,242줄).
            + ("STAGE7_DIAG=1 " if self.diagnostics else "")
            + self.profile["pi"]["venv_python"]
            + " apps/coordinator_main.py"
            + " --run-id " + self.session_id
            + " --phase mass-roc-session"
            + " --run-root " + self.remote_run_root
            + " --configs-dir " + self.remote_root + "/run-configs/" + self.session_id
            + " --gain-artifact " + self.remote_root + "/" + gain_rel
            + " --baseline-sha256 " + self.profile["baseline_sha256"]
            + "; echo $? > " + self.remote_run_root + "/done.rc"
        )
        script = (
            "set -e\n"
            "cd " + self.remote_root + "/VDETER\n"
            "mkdir -p " + self.remote_run_root + "\n"
            "nohup bash -c " + json.dumps(inner) + " > "
            + self.remote_run_root + "/coordinator-console.log 2>&1 < /dev/null &\n"
            "echo spawned\n"
        )
        # SSH가 원격 nohup spawn 직후 끊기면 호출자는 성공 응답을 못 받았어도
        # coordinator가 살아 있을 수 있다. 시도 사실을 먼저 기록해 close()가
        # 그 경우에도 bounded cleanup 확인을 생략하지 않게 한다.
        self._coordinator_launch_attempted = True
        self._ssh(script, timeout=180.0)
        self._coordinator_spawned = True
        self._mark("coordinator_spawned")
        self.trace.emit("coordinator_spawned", phase="mass-roc-session",
                        remote_run_root=self.remote_run_root)
        self.trace.state("TIME_LOCKING")
        self.trace.flush()

        deadline = self.clock() + SESSION_BRINGUP_TIMEOUT_S
        #: 단계가 바뀔 때만 찍는다. 두 번에 한 번만 조회해 ssh 부담을 줄인다.
        last_stage = None
        poll = 0
        while self.clock() < deadline:
            ready = self._remote_json(SESSION_READY)
            if ready is not None:
                self.ready = ready
                self._mark("session_ready")
                self.status = "READY"
                self.trace.state("SERVICE_ADMISSION",
                                 rf_time_paths=ready.get("rf_time_paths"))
                self.trace.emit("session_ready",
                                bringup_elapsed_s=ready.get("phase_elapsed_s"),
                                health_baseline=ready.get("health_baseline"))
                self.trace.state("READY")
                self.trace.flush()
                return ready
            poll += 1
            if poll % 2 == 0:
                stage = self._remote_bringup_stage()
                if stage and stage != last_stage:
                    self._live("  [%s]  %s" % (self._live_elapsed(), stage))
                    last_stage = stage
            if self._coordinator_exited():
                self.trace.state("FAILED",
                                 reason="coordinator_exit_during_bringup",
                                 coordinator_rc=self._coordinator_rc())
                self.trace.flush()
                raise ServiceSessionError(
                    "session bring-up 중 coordinator가 종료했습니다 "
                    f"(rc={self._coordinator_rc()})")
            time.sleep(5.0)
        self.trace.state("FAILED", reason="bringup_timeout")
        self.trace.flush()
        raise ServiceSessionError("session bring-up timeout — RF 실행 중단")

    # ---- workload (§4) ---------------------------------------------------

    def next_workload_id(self, scenario_id: str, certificate_case: str) -> str:
        self._counter += 1
        tag = scenario_id.lower().replace("sc-vde-", "sc")
        if scenario_id == "SC-VDE-001":
            tag += "-" + certificate_case
        return f"{self.session_id}-{tag}-{self._counter:03d}"

    def run_workload(self, scenario_id: str,
                     certificate_case: str = "normal") -> dict:
        """workload child 하나 — application state만 새로 만든다."""
        from apps.service_layer import ServiceSessionError

        if self.status != "READY":
            raise ServiceSessionError(f"session이 READY가 아닙니다: {self.status}")
        if (self._last_workload_end is not None
                and self._last_scenario_id in SETTLE_AFTER_SCENARIOS):
            remaining = WORKLOAD_SETTLE_SECONDS - (
                self.clock() - self._last_workload_end)
            if remaining > 0:
                self.trace.emit("workload_settle",
                                seconds=round(remaining, 1),
                                after_scenario=self._last_scenario_id,
                                reason="rx_pipeline_drain")
                self._live("  정착 대기 %.0f초 (직전 %s — RX 파이프라인 배수)"
                           % (remaining, self._last_scenario_id))
                self.sleeper(remaining)
        workload_id = self.next_workload_id(scenario_id, certificate_case)
        started = self.clock()
        request = {
            "workload_id": workload_id,
            "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "requested_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(started)),
        }
        payload = json.dumps(request, ensure_ascii=True, sort_keys=True)
        name = workload_id + ".json"
        script = (
            "mkdir -p " + self.remote_session_dir + "/" + SESSION_REQUESTS + "\n"
            "cat > " + self.remote_session_dir + "/" + SESSION_REQUESTS + "/"
            + name + " <<'" + _REQUEST_HEREDOC + "'\n"
            + payload + "\n" + _REQUEST_HEREDOC + "\n"
        )
        self.trace.state("WORKLOAD_RUNNING", workload=workload_id,
                         scenario_id=scenario_id,
                         certificate_case=certificate_case)
        self.trace.flush()
        self._ssh(script, timeout=120.0)

        deadline = self.clock() + WORKLOAD_TIMEOUT_S
        result = None
        seen_trace = 0
        while self.clock() < deadline:
            result = self._remote_json(SESSION_RESULTS + "/" + name)
            if result is not None:
                break
            if self._coordinator_exited():
                raise ServiceSessionError(
                    f"workload {workload_id} 중 session이 종료했습니다 "
                    f"(rc={self._coordinator_rc()})")
            rows = self._remote_trace_tail(workload_id, seen_trace)
            seen_trace += len(rows)
            for row in rows:
                line = _render_trace_event(row)
                if line:
                    self._live(line)
            time.sleep(3.0)
        # 결과가 먼저 도착하면 남은 trace 를 마저 흘린다.
        for row in self._remote_trace_tail(workload_id, seen_trace):
            line = _render_trace_event(row)
            if line:
                self._live(line)
        if result is None:
            raise ServiceSessionError(f"workload {workload_id} timeout")
        result["wall_clock_s"] = round(self.clock() - started, 1)
        self.workloads.append(result)
        self._mark("workload:" + workload_id)
        quiescence = result.get("quiescence") or {}
        self.trace.state("QUIESCING", workload=workload_id)
        self.trace.emit("workload_finished", workload=workload_id,
                        scenario_id=scenario_id,
                        certificate_case=certificate_case,
                        status=result.get("status"),
                        child_exit_code=result.get("child_exit_code"),
                        wall_clock_s=result["wall_clock_s"],
                        quiescence=quiescence,
                        session_health_before=result.get("session_health_before"))
        self.trace.flush()
        if not quiescence.get("pass"):
            # quiescence 실패면 다음 workload를 시작하지 않는다 (§7).
            self.status = "QUIESCENCE_FAIL"
            self.trace.state("FAILED", reason="quiescence_fail",
                             workload=workload_id)
            self.trace.flush()
        else:
            self.status = "READY"
            self.trace.state("READY")
        self._last_workload_end = self.clock()
        self._last_scenario_id = scenario_id
        return result

    # ---- 종료 (§10) ------------------------------------------------------

    def close(self) -> dict:
        """`q`/fault — 한 번만 전체 cleanup. 실패도 FAIL로 남긴다."""
        if self.closed is not None:
            return self.closed
        self.trace.state("STOPPING")
        self.trace.flush()
        close_errors = []
        coordinator_exited = False
        shutdown_rc = None
        try:
            shutdown = self._ssh(
                "mkdir -p " + self.remote_session_dir + " && touch "
                + self.remote_session_dir + "/" + SESSION_SHUTDOWN,
                timeout=120.0, check=False)
            shutdown_rc = shutdown.returncode
            if shutdown_rc != 0:
                close_errors.append(
                    f"shutdown 요청 실패(rc={shutdown_rc})")
            if self._coordinator_launch_attempted:
                deadline = self.clock() + SESSION_CLOSE_TIMEOUT_S
                while self.clock() < deadline:
                    if self._coordinator_exited():
                        coordinator_exited = True
                        break
                    time.sleep(3.0)
                if not coordinator_exited:
                    coordinator_exited = self._coordinator_exited()
            if self._coordinator_launch_attempted and not coordinator_exited:
                close_errors.append(
                    "bounded close timeout — coordinator가 남아 있을 수 있음")
            closed = self._remote_json(SESSION_CLOSED)
            if closed is None:
                closed = {"status": "FAIL", "error": "closed.json 없음"}
            coordinator_rc = self._coordinator_rc() if coordinator_exited else None
            reported_rc = closed.get("coordinator_rc")
            if reported_rc is not None and reported_rc != coordinator_rc:
                close_errors.append(
                    "closed.json과 done.rc의 coordinator rc가 다름")
            closed["coordinator_rc"] = coordinator_rc
            if coordinator_exited and coordinator_rc != 0:
                close_errors.append(
                    f"coordinator 비정상 종료(rc={coordinator_rc})")
            cleanup = closed.get("cleanup") or {}
            if closed.get("status") != "PASS":
                close_errors.append(
                    f"원격 close status={closed.get('status')!r}")
            if cleanup.get("pass") is not True:
                close_errors.append("원격 cleanup pass가 true가 아님")
            closed["bounded_close"] = {
                "timeout_s": SESSION_CLOSE_TIMEOUT_S,
                "shutdown_request_returncode": shutdown_rc,
                "coordinator_exited": coordinator_exited,
                "coordinator_may_be_running": (
                    self._coordinator_launch_attempted and not coordinator_exited),
                "launch_attempted": self._coordinator_launch_attempted,
                "spawn_confirmed": self._coordinator_spawned,
                "pass": not close_errors,
            }
            if close_errors:
                previous = closed.get("error")
                closed["status"] = "FAIL"
                closed["error"] = "; ".join(
                    ([str(previous)] if previous else []) + close_errors)
            self.closed = closed
        except Exception as exc:  # noqa: BLE001 - cleanup 실패도 evidence로
            self.closed = {
                "status": "FAIL",
                "error": f"cleanup 실패: {type(exc).__name__}: {exc}",
                "bounded_close": {
                    "timeout_s": SESSION_CLOSE_TIMEOUT_S,
                    "shutdown_request_returncode": shutdown_rc,
                    "coordinator_exited": coordinator_exited,
                    "coordinator_may_be_running": (
                        self._coordinator_launch_attempted and not coordinator_exited),
                    "launch_attempted": self._coordinator_launch_attempted,
                    "spawn_confirmed": self._coordinator_spawned,
                    "pass": False,
                },
            }

        try:
            self.evidence_collection = self.collect_evidence()
        except Exception as exc:  # noqa: BLE001 - 수집 실패도 session FAIL
            self.evidence_collection = {
                "pass": False,
                "error": f"evidence 수집 실패: {type(exc).__name__}: {exc}",
            }
            previous = self.closed.get("error")
            self.closed["status"] = "FAIL"
            self.closed["error"] = "; ".join(
                ([str(previous)] if previous else [])
                + [self.evidence_collection["error"]])
        self._mark("session_closed")
        if self.closed.get("status") != "PASS":
            self.status = "FAIL"
            self.error = self.closed.get("error")
        elif self.status != "QUIESCENCE_FAIL":
            self.status = self.closed.get("status", "FAIL")
        self.trace.emit("session_closed", status=self.status,
                        cleanup=(self.closed or {}).get("cleanup"),
                        bounded_close=(self.closed or {}).get("bounded_close"),
                        evidence_collection=self.evidence_collection,
                        coordinator_rc=(self.closed or {}).get("coordinator_rc"),
                        error=(self.closed or {}).get("error"))
        self.trace.state("CLOSED" if self.status == "PASS" else "FAILED")
        self.trace.close()
        return self.closed

    def _validate_existing_evidence_target(self, target: str) -> dict:
        from apps.service_layer import ServiceSessionError

        if not os.path.isdir(target):
            raise ServiceSessionError(
                f"기존 evidence target이 디렉터리가 아닙니다: {target}")
        files = []
        for base, _dirs, names in os.walk(target):
            files.extend(os.path.join(base, name) for name in names)
        if not files:
            raise ServiceSessionError(
                f"기존 evidence target이 비어 있습니다: {target}")

        if self.ready is not None:
            ready_path = os.path.join(target, SESSION_DIR_NAME, SESSION_READY)
            if not os.path.isfile(ready_path):
                raise ServiceSessionError(
                    "기존 evidence target에 session/ready.json이 없습니다")
            try:
                with open(ready_path, "r", encoding="utf-8") as handle:
                    collected_ready = json.load(handle)
            except (OSError, ValueError) as exc:
                raise ServiceSessionError(
                    f"기존 session/ready.json 검증 실패: {exc}") from exc
            if collected_ready.get("run_id") != self.session_id:
                raise ServiceSessionError(
                    "기존 evidence target의 ready run-id가 현재 session과 다릅니다")
        if self.closed and self.closed.get("artifact_type"):
            closed_path = os.path.join(target, SESSION_DIR_NAME, SESSION_CLOSED)
            if not os.path.isfile(closed_path):
                raise ServiceSessionError(
                    "기존 evidence target에 session/closed.json이 없습니다")
            try:
                with open(closed_path, "r", encoding="utf-8") as handle:
                    collected_closed = json.load(handle)
            except (OSError, ValueError) as exc:
                raise ServiceSessionError(
                    f"기존 session/closed.json 검증 실패: {exc}") from exc
            if collected_closed.get("status") != self.closed.get("status"):
                raise ServiceSessionError(
                    "기존 evidence target의 closed status가 현재 결과와 다릅니다")
            if collected_closed.get("run_id") != self.session_id:
                raise ServiceSessionError(
                    "기존 evidence target의 closed run-id가 현재 session과 다릅니다")
        expected_rc = (self.closed or {}).get("coordinator_rc")
        if expected_rc is not None:
            rc_path = os.path.join(target, "done.rc")
            try:
                with open(rc_path, "r", encoding="utf-8") as handle:
                    collected_rc = int(handle.read().strip())
            except (OSError, ValueError) as exc:
                raise ServiceSessionError(
                    f"기존 evidence target의 done.rc 검증 실패: {exc}") from exc
            if collected_rc != expected_rc:
                raise ServiceSessionError(
                    "기존 evidence target의 coordinator rc가 현재 결과와 다릅니다")
        return {"pass": True, "reused": True, "file_count": len(files),
                "target": target}

    def collect_evidence(self) -> dict:
        from apps.service_layer import ServiceSessionError

        target = os.path.join(self.session_dir, "remote-run")
        if os.path.exists(target):
            return self._validate_existing_evidence_target(target)
        done = self.runner(
            ["scp", "-q", "-r",
             self.profile["pi"]["host"] + ":"
             + self.profile["pi"]["deploy_relpath"]
             + "/runs/" + self.session_id, target],
            capture_output=True, text=True, timeout=900,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            detail = ((done.stdout or "") + (done.stderr or ""))[-300:]
            raise ServiceSessionError(
                f"remote evidence scp 실패(rc={done.returncode}): {detail}")
        validated = self._validate_existing_evidence_target(target)
        validated.update({"reused": False,
                          "scp_returncode": done.returncode})
        return validated

    def summary(self) -> dict:
        return {
            "artifact_type": "STAGE7_SERVICE_SESSION_SUMMARY_V1",
            "judgement_scope": "OPERATIONAL_SERVICE",
            "not_a_qualification": (
                "RF qualification이 아니다 — CFO/adaptive gain/raw-PHY 미실행. "
                "자격은 Phase F qualification reference가 담보한다."),
            "session_id": self.session_id,
            "profile_id": self.profile["profile_id"],
            "qualification_run_id":
                self.profile["_reference"]["qualification"]["run_id"],
            "reference_sha256": self.profile["_reference"]["_reference_sha256"],
            "status": self.status,
            "error": self.error,
            "phase_elapsed_s": self.marks,
            "local_validation": self.local_validation,
            "remote_preflight": self.remote_preflight,
            "config": self.config and {
                "resolved_run_id": self.config["metadata"].get("run_id"),
                "git_dirty": self.config["metadata"].get("git_dirty"),
                "git_commit": self.config["metadata"].get("git_commit"),
            },
            "session_ready": self.ready,
            "workloads": self.workloads,
            "workload_ids": [w.get("workload_id") for w in self.workloads],
            "closed": self.closed,
            "evidence_collection": self.evidence_collection,
            "hardware_lifecycle_note": (
                "B210 acquire·endpoint START·host fit·RF-time PRE/POST·"
                "active bridge·admission·STOP은 session당 각 1회. "
                "workload는 application state만 새로 만든다."),
            "trace_path": getattr(self.trace, "path", None),
            "diagnostics": self.diagnostics,
        }


def open_service_session(profile: dict, *, session_id: str | None = None,
                         evidence_root: str | None = None,
                         runner=subprocess.run, clock=time.time,
                         sleeper=time.sleep, live: bool = True,
                         diagnostics: bool = False) -> ServiceSession:
    """CLI 시작 시 하드웨어 session을 세운다 (§2)."""
    from apps.service_layer import ServiceSessionError

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    session_id = session_id or ("phase-g-service-" + stamp)
    root = evidence_root or profile["evidence_root"]
    session_dir = os.path.join(_ROOT, root, session_id)
    if os.path.exists(session_dir):
        raise ServiceSessionError(f"session 디렉터리 중복: {session_dir}")
    os.makedirs(session_dir)
    session = ServiceSession(profile, session_id=session_id,
                                   session_dir=session_dir, runner=runner,
                                   clock=clock, sleeper=sleeper, live=live,
                                   diagnostics=diagnostics)
    try:
        session.start()
    except Exception:
        # bring-up 실패에도 마지막 계측을 보존하고 하드웨어를 정리한다.
        try:
            session.close()
        finally:
            session.trace.close(reason="bringup_failed")
        raise
    return session
