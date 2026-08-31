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


class Stage7ServiceSession:
    """살아 있는 하드웨어 service session 하나. workload는 이 위에서만 돈다."""

    #: 계측용 session 상태 전이.
    STATES = ("NEW", "VALIDATING", "STARTING", "TIME_LOCKING",
              "SERVICE_ADMISSION", "READY", "WORKLOAD_RUNNING",
              "QUIESCING", "STOPPING", "CLOSED", "FAILED")

    def __init__(self, profile: dict, *, session_id: str, session_dir: str,
                 runner=subprocess.run, clock=time.time,
                 trace=None, diagnostics: bool = False) -> None:
        self.profile = profile
        self.session_id = session_id
        self.session_dir = session_dir
        self.runner = runner
        self.clock = clock
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
        self._counter = 0
        self._started = clock()
        self.diagnostics = bool(diagnostics)
        if trace is None:
            from apps.stage7_trace import TraceWriter
            trace = TraceWriter(
                os.path.join(session_dir, "session-trace.jsonl"),
                session_id=session_id, diagnostics=self.diagnostics)
        self.trace = trace
        self.trace.state("NEW", profile_id=profile.get("profile_id"))

    # ---- 원격 경로 -------------------------------------------------------

    @property
    def remote_root(self) -> str:
        from apps.stage7_service_layer import remote_deploy_root
        return remote_deploy_root(self.profile)

    @property
    def remote_run_root(self) -> str:
        return f"{self.remote_root}/runs/{self.session_id}"

    @property
    def remote_session_dir(self) -> str:
        return f"{self.remote_run_root}/{SESSION_DIR_NAME}"

    def _ssh(self, script, *, timeout=300.0, check=True):
        from apps.stage7_service_layer import _ssh
        return _ssh(self.profile, script, timeout=timeout, check=check,
                    runner=self.runner)

    def _mark(self, name):
        self.marks[name] = round(self.clock() - self._started, 1)

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
        from apps import stage7_profile
        from apps.stage7_service_layer import (
            ServiceSessionError, prepare_service_config,
            verify_remote_environment,
        )

        self.trace.state("VALIDATING")
        self.local_validation = stage7_profile.validate_local_bindings(self.profile)
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
            + self.profile["pi"]["venv_python"]
            + " apps/stage7_coordinator_main.py"
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
        self._ssh(script, timeout=180.0)
        self._mark("coordinator_spawned")
        self.trace.emit("coordinator_spawned", phase="mass-roc-session",
                        remote_run_root=self.remote_run_root)
        self.trace.state("TIME_LOCKING")
        self.trace.flush()

        deadline = self.clock() + SESSION_BRINGUP_TIMEOUT_S
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
        from apps.stage7_service_layer import ServiceSessionError

        if self.status != "READY":
            raise ServiceSessionError(f"session이 READY가 아닙니다: {self.status}")
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
        while self.clock() < deadline:
            result = self._remote_json(SESSION_RESULTS + "/" + name)
            if result is not None:
                break
            if self._coordinator_exited():
                raise ServiceSessionError(
                    f"workload {workload_id} 중 session이 종료했습니다 "
                    f"(rc={self._coordinator_rc()})")
            time.sleep(3.0)
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
        return result

    # ---- 종료 (§10) ------------------------------------------------------

    def close(self) -> dict:
        """`q`/fault — 한 번만 전체 cleanup. 실패도 FAIL로 남긴다."""
        if self.closed is not None:
            return self.closed
        self.trace.state("STOPPING")
        self.trace.flush()
        try:
            self._ssh("mkdir -p " + self.remote_session_dir + " && touch "
                      + self.remote_session_dir + "/" + SESSION_SHUTDOWN,
                      timeout=120.0, check=False)
            deadline = self.clock() + SESSION_CLOSE_TIMEOUT_S
            while self.clock() < deadline:
                if self._coordinator_exited():
                    break
                time.sleep(3.0)
            closed = self._remote_json(SESSION_CLOSED)
            if closed is None:
                closed = {"status": "FAIL", "error": "closed.json 없음"}
            closed.setdefault("coordinator_rc", self._coordinator_rc())
            self.closed = closed
        except Exception as exc:  # noqa: BLE001 - cleanup 실패도 evidence로
            self.closed = {
                "status": "FAIL",
                "error": f"cleanup 실패: {type(exc).__name__}: {exc}",
            }
        self._mark("session_closed")
        if self.status != "QUIESCENCE_FAIL":
            self.status = self.closed.get("status", "FAIL")
        self.trace.emit("session_closed", status=self.status,
                        cleanup=(self.closed or {}).get("cleanup"),
                        coordinator_rc=(self.closed or {}).get("coordinator_rc"),
                        error=(self.closed or {}).get("error"))
        self.trace.state("CLOSED" if self.status == "PASS" else "FAILED")
        self.trace.close()
        self.collect_evidence()
        return self.closed

    def collect_evidence(self) -> None:
        target = os.path.join(self.session_dir, "remote-run")
        if os.path.exists(target):
            return
        self.runner(["scp", "-q", "-r",
                     self.profile["pi"]["host"] + ":"
                     + self.profile["pi"]["deploy_relpath"]
                     + "/runs/" + self.session_id, target],
                    capture_output=True, text=True, timeout=900,
                    encoding="utf-8", errors="replace")

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
                         diagnostics: bool = False) -> Stage7ServiceSession:
    """CLI 시작 시 하드웨어 session을 세운다 (§2)."""
    from apps.stage7_service_layer import ServiceSessionError

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    session_id = session_id or ("phase-g-service-" + stamp)
    root = evidence_root or profile["evidence_root"]
    session_dir = os.path.join(_ROOT, root, session_id)
    if os.path.exists(session_dir):
        raise ServiceSessionError(f"session 디렉터리 중복: {session_dir}")
    os.makedirs(session_dir)
    session = Stage7ServiceSession(profile, session_id=session_id,
                                   session_dir=session_dir, runner=runner,
                                   clock=clock, diagnostics=diagnostics)
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
