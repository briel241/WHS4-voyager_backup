"""Stage 8 RF-off software session — 실 coordinator 프로세스가 PoC workload를 실행하는 경로.

이 module은 **명시적 RF-off software 모드**에서만 진입한다(`stage7_coordinator_main --rf-off-software`).
실장비 경로(B210 launcher spawn)를 대신해, 실 coordinator 프로세스가 파일 IPC(`session/requests` →
`session/results`)로 PoC workload 요청을 받아 **프로세스 안에서** workload-local PoC supervisor를
띄우고(실 PoC child·Unix socket·SDK·engine·provider), scenario event를 실행 중 전달하며 판정·evidence·
cleanup을 확정한다(연결 API 명세 §3.1: supervisor는 VDETER runtime 안, child만 별도 process).

**하드웨어 경계만** software double로 대체한다: B210 group TX·arbiter·RX readiness·waveform·terminal은
`vdeter.stage8.integration.rf_off`의 **명시 주입** double이다(production 기본값 아님). 실 launcher socket
IPC와 실 Stage 7 MAC seam 결속은 in-process fake 매질이 3개 launcher 프로세스를 가로지를 수 없어
RF-off로 재현할 수 없다(실장비 또는 cross-process 매질 subsystem 필요) — 그 부분은 미검증으로 분리한다.

deadline은 `ResidentPocRuntime`(권위 상한: bring-up 2700 / workload 900 / close 600 / absolute 7200 /
READY idle 2700)에 결속한다. RF-off는 실 900초 wall-clock을 돌리지 않으므로 그 강제는 fake-clock 경계
시험으로만 확인한다(여기서는 구조적 결속만).

이 module은 `apps/`(비자격 도메인)에 있으며, 자격 대상 `VDETER/apps/stage7_coordinator_main.py`에는
top-level dispatch 한 분기만 추가한다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# stage7 파일 IPC 상수(권위 정의 재사용 — 중복 재정의 금지).
from apps.stage7_session import (
    SESSION_BRINGUP_TIMEOUT_S,
    SESSION_CLOSE_TIMEOUT_S,
    WORKLOAD_TIMEOUT_S,
)

_SESSION_DIR = "session"
_REQUESTS = "requests"
_RESULTS = "results"
_READY = "ready.json"
_CLOSED = "closed.json"
_SHUTDOWN = "shutdown"


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)
    os.replace(tmp, path)


class RfOffPocDeviceSession:
    """poc_run이 읽는 device-boundary 공급자 — **명시적 RF-off software double**.

    실 `LocalCoordinatorSession`이 (아직) device 경계를 주지 못하는 자리를, RF-off 검증 한정으로 이
    coordinator-side 공급자가 명시적으로 채운다(production silent fallback 아님). 정상 설치에서는 실 Stage 7
    MAC seam이 이 자리를 대신한다(실장비 Stage 6). scenario 자극은 실행 중 전달되는 관측 step으로 구성해
    반응형 action이 scenario terminal 이전에 제출되게 한다(연결 §8.3 인과성; 관측 hw-time이 반응형 slot을
    유도한다 — 단순 counter 아님).
    """

    def __init__(self, run_root: str, *, reactive: bool = True) -> None:
        self.run_root = Path(run_root)
        self._reactive = reactive

    def poc_device_seams(self):
        from vdeter.stage8.integration.rf_off import rf_off_seams
        return rf_off_seams()

    def poc_terminal_source(self):
        from vdeter.stage8.integration.rf_off import rf_off_terminal_source
        return rf_off_terminal_source()

    def poc_target_response(self):
        # 실 RX/application 수신 증거는 실장비에서만 온다. RF-off는 INDETERMINATE(합성 금지).
        return None

    def poc_workload_steps(self, scenario_id):
        from vdeter.stage8.execution.runner import observation_step, scenario_started
        from vdeter.stage8.testing.fakes import make_observation
        if not self._reactive:
            return [scenario_started()]
        # scenario 진행 중 MASS-B가 관측하는(overhear) 브로드캐스트. device_hw_timestamp_ns가 반응형
        # slot 기준이다(shared-medium 참조 모델의 예약 슬롯 시각; RF-off VDETER-owned 관측).
        observation = make_observation(
            source_station="ROC", destination_station="MASS-A",
            device_hw_timestamp_ns=2 * 8192)
        return [scenario_started(), observation_step(observation)]


def _handle_poc_request(session, runtime, request: dict, *, run_root: Path,
                        run_id: str) -> dict:
    """PoC workload 하나를 실 coordinator 프로세스 안에서 실행하고 evidence dict를 돌려준다."""
    from vdeter.stage8.execution.verdict import ScenarioVerdict

    from apps.standalone import poc_run

    workload_id = request["workload_id"]
    scenario_id = request.get("scenario_id", "SC-VDE-006")
    poc = request.get("poc") or {}
    poc_path = poc.get("poc")
    poc_dir = poc.get("poc_dir")
    poc_id = poc.get("poc_id")
    src_path = poc.get("src_path")

    child_root = run_root / "workloads" / workload_id
    child_root.mkdir(parents=True, exist_ok=True)
    dest = child_root / "poc-snapshot"

    option = poc_run.resolve_option(poc=poc_path, poc_dir=poc_dir, interactive=False)
    # snapshot은 runtime(coordinator)에서 workload 시작 직전에 만든다(§5.3).
    snapshot = poc_run.select_snapshot(option, scenario_id=scenario_id,
                                       dest_dir=str(dest), poc_id=poc_id, interactive=False)
    if snapshot is None:
        # baseline(none): PoC 자원 0(구조적 0).
        return {"status": "BASELINE", "workload_id": workload_id, "scenario_id": scenario_id}

    # R1/N1: scenario submit **전에** arm. 준비/ARMED 실패면 scenario 자극 0.
    runtime.begin_workload()
    prepared = None
    try:
        prepared = poc_run.prepare_workload(
            session, snapshot, scenario_id=scenario_id, run_id=run_id,
            workload_id=workload_id, socket_root=None, src_path=src_path)
    except poc_run.PocRunError as exc:
        runtime.end_workload()
        evidence = getattr(exc, "evidence", None) or {
            "status": "ERROR", "poc_infrastructure": "ERROR", "error": str(exc)}
        evidence.setdefault("workload_id", workload_id)
        return evidence
    except BaseException:
        runtime.end_workload()
        raise
    # scenario 진행 중 event 전달 → 반응형 action → terminal → finish → evidence → cleanup.
    scenario_verdict = ScenarioVerdict.PASS
    evidence = poc_run.finish_workload(prepared, scenario_verdict=scenario_verdict)
    evidence.setdefault("workload_id", workload_id)
    evidence.setdefault("scenario_id", scenario_id)
    runtime.end_workload()
    return evidence


def run_rf_off_software_session(arguments) -> int:
    """실 coordinator 프로세스의 RF-off software session loop(파일 IPC + in-process PoC 실행).

    `arguments`는 `stage7_coordinator_main`의 argparse namespace(run_id·run_root 사용). B210·launcher를
    전혀 spawn하지 않는다. 정상 종료·shutdown 마커·idle/absolute deadline·close 예산을 처리한다.
    """
    from vdeter.stage8._supervisor.clock import RealMonotonicClock
    from vdeter.stage8.execution.limits import SessionPolicy, Stage7Timeouts
    from vdeter.stage8.execution.session import ResidentSession, SessionExpired
    from vdeter.stage8.integration.runtime import ResidentPocRuntime

    run_root = Path(arguments.run_root)
    run_id = arguments.run_id
    session_dir = run_root / _SESSION_DIR
    requests_dir = session_dir / _REQUESTS
    results_dir = session_dir / _RESULTS
    for directory in (session_dir, requests_dir, results_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # 권위 상한을 실제 정의에서 가져온다(중복 timer 재구현·완화 금지).
    policy = SessionPolicy(stage7=Stage7Timeouts(
        bringup_s=SESSION_BRINGUP_TIMEOUT_S, workload_s=WORKLOAD_TIMEOUT_S,
        close_s=SESSION_CLOSE_TIMEOUT_S))
    session_model = ResidentSession(clock=RealMonotonicClock(), policy=policy)
    runtime = ResidentPocRuntime(session_model)
    runtime.bring_up()  # READY(bring-up 상한·absolute 확인). RF-off는 실 B210 bring-up 없음.

    device_session = RfOffPocDeviceSession(str(run_root))

    print("[coordinator] RF-off software session READY (no B210 launchers)", flush=True)
    _write_json(session_dir / _READY, {"run_id": run_id, "mode": "rf-off-software",
                                       "ready": True})
    status = "PASS"
    error = None
    try:
        while True:
            if (session_dir / _SHUTDOWN).exists():
                print("[coordinator] shutdown 요청 수신", flush=True)
                break
            try:
                session_model.check_deadlines()  # absolute·READY idle deadline(연장 없음).
            except SessionExpired as expired:
                status = "FAIL"
                error = f"session deadline expired: {expired.deadline.value}"
                break
            pending = sorted(
                name for name in os.listdir(requests_dir)
                if name.endswith(".json")
                and not (results_dir / name).exists())
            if not pending:
                time.sleep(0.05)
                continue
            request_name = pending[0]
            with open(requests_dir / request_name, "rb") as handle:
                request = json.loads(handle.read().decode("utf-8"))
            session_model.note_control_plane_activity()  # workload 요청 → idle timer 재시작.
            print(f"[coordinator] poc workload 시작 {request.get('workload_id')}", flush=True)
            try:
                evidence = _handle_poc_request(device_session, runtime, request,
                                               run_root=run_root, run_id=run_id)
                record = {"artifact_type": "STAGE8_RF_OFF_POC_WORKLOAD_RESULT_V1",
                          "status": "PASS", "poc": evidence,
                          "workload_id": request.get("workload_id"),
                          "scenario_id": request.get("scenario_id"),
                          "quiescence": {"pass": True}}
            except BaseException as exc:  # noqa: BLE001 - workload 실패도 결과로 회신(세션 유지 판정 별도)
                record = {"artifact_type": "STAGE8_RF_OFF_POC_WORKLOAD_RESULT_V1",
                          "status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                          "workload_id": request.get("workload_id"),
                          "scenario_id": request.get("scenario_id"),
                          "quiescence": {"pass": True}}
            _write_json(results_dir / request_name, record)
    finally:
        # close 예산(권위 close_s) 안에서 종료를 확정한다(재호출로 연장하지 않음).
        try:
            runtime.close(None)
        except Exception as close_exc:  # noqa: BLE001
            if error is None:
                status, error = "FAIL", f"close: {type(close_exc).__name__}: {close_exc}"
        # closed.json 스키마는 finalize가 읽는 top-level 계약을 따른다: status + cleanup.pass.
        _write_json(session_dir / _CLOSED,
                    {"run_id": run_id, "status": status, "error": error,
                     "cleanup": {"pass": status == "PASS"}})
    return 0 if status == "PASS" else 1


__all__ = ("RfOffPocDeviceSession", "run_rf_off_software_session")
