"""production PoC 실행 진입 driver — `vdeter run [SCENARIO] --poc/--poc-dir` 형상(실행 명세 §5).

명세가 정한 사용자 진입점 형상을 하나의 driver로 결속한다:

    scenario 선택 -> catalog(--poc-dir) 또는 direct(--poc) 소스 -> scenario-호환 PoC 선택(또는
    none) -> **immutable snapshot** -> production 연결(실 child·socket·Stage 5 provider) ->
    workload runner(§5.1 lifecycle·§11 세 독립 판정) -> renderer.

RF-off 통합에서는 workload 자극(scenario/step event·관측)을 `steps`로 주입하고 장비 경계만
software harness로 대체한다. 실장비(Stage 6)에서는 이 자극을 실 scenario RF event가 대신하고,
`device_seams`에 실 Stage 7 seam을 넣는다. PoC 미사용(`none`) 경로는 child·socket·provider·lease를
전혀 만들지 않는다(기능 OFF 구조적 0).

이 driver는 argv parsing이나 기존 Stage 7 CLI(`apps/standalone/cli.py`) 연결을 하지 않는다. 그
최종 결선(`--poc`/`--poc-dir` flag 추가 + import 경계 계약 시험 갱신)과 실 coordinator·B210
구동은 Stage 6 실장비 단계의 별도 승인 항목이다.
"""

from __future__ import annotations

import json
import os
import sys

from ..execution.catalog import scan_catalog
from ..execution.lease import MassBLease
from ..execution.runner import WorkloadRunner, WorkloadSpec
from ..execution.selection import (
    SelectionError,
    SourceMode,
    build_menu,
    resolve_source_mode,
    unavailable_required_capabilities,
)
from ..execution.snapshot import Snapshot, create_snapshot
from ..execution.verdict import ScenarioVerdict, WorkloadResult
from ..poc_api.negotiation import ServerCapabilityConfig
from .._supervisor.child import SubprocessChildSpawner
from .._supervisor.clock import RealMonotonicClock
from .._supervisor.engine import EngineState  # noqa: F401 - state 참조 편의
from .assembly import assemble_provider
from .supervisor_connection import SupervisorConnection
from .tx_executor import ProductionTxExecutor

#: child launcher argv(실 PoC snapshot 실행). child_main이 snapshot·entrypoint·hello를 env로 받는다.
_CHILD_MODULE = "vdeter.stage8.execution.child_main"


def _base_of_path(poc_path: str) -> str:
    name = os.path.basename(poc_path)
    if not name.endswith(".py"):
        raise SelectionError("--poc는 <base>.py 경로여야 합니다(§6.2)")
    return name[:-3]


def select_and_snapshot(*, scenario_id: str, dest_dir: str, poc: str | None = None,
                        poc_dir: str | None = None, poc_id: str | None = None,
                        server_config: ServerCapabilityConfig | None = None,
                        interactive: bool = True, uid=None) -> Snapshot | None:
    """scenario-호환 PoC를 선택해 **immutable snapshot**을 만든다(§5.3, §6.1).

    - `--poc`(direct): 단일 package path. 그 자리에서 snapshot한다.
    - `--poc-dir`(catalog): 비재귀 scan → scenario-호환 PoC와 `none`만 선택 가능. `poc_id`가 None이거나
      `"none"`이면 baseline(PoC 미사용)으로 `None`을 돌려준다.
    - 둘 다 없으면 PoC OFF로 `None`.
    """

    mode = resolve_source_mode(poc=poc, poc_dir=poc_dir, interactive=interactive)
    server = server_config or ServerCapabilityConfig()
    if mode == SourceMode.OFF:
        return None
    if mode == SourceMode.DIRECT:
        base = _base_of_path(poc)
        snapshot = create_snapshot(os.path.dirname(os.path.abspath(poc)), base,
                                   dest_dir=dest_dir, scenario_id=scenario_id, uid=uid)
        # R9: direct(--poc)도 catalog와 **같은** 설치 capability 사전 검증을 거친다. 미지원 required
        # capability는 연구자 코드 import·constructor·child spawn 이전에 거부한다(§6.3, POC-D22).
        # snapshot(파일 복사·manifest 검증)은 연구자 코드를 import하지 않으므로 이 시점이 import 전이다.
        unavailable = unavailable_required_capabilities(snapshot.descriptor, server)
        if unavailable:
            raise SelectionError(
                f"required capability unavailable: {','.join(unavailable)} "
                f"(direct --poc는 import·spawn 전에 거부; handshake 재검증과 별개)")
        return snapshot
    # CATALOG: scenario-first 선택.
    catalog = scan_catalog(poc_dir, uid=uid)
    menu = build_menu(catalog, scenario_id, server_config=server)
    if poc_id is None or poc_id == "none":
        return None  # baseline: PoC 자원 0(구조적 0).
    if poc_id not in menu.selectable_ids():
        raise SelectionError(
            f"PoC {poc_id!r}는 scenario {scenario_id}에서 선택할 수 없습니다(호환/유효성/capability)")
    entry = catalog.valid_by_id()[poc_id]
    base = os.path.basename(entry.script.path)[:-3]
    return create_snapshot(poc_dir, base, dest_dir=dest_dir, scenario_id=scenario_id, uid=uid)


def build_connection(*, snapshot: Snapshot, provider, session_id: str, workload_id: str,
                     run_id: str, scenario_id: str, socket_root: str | None = None,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None, python_executable: str | None = None,
                     clock=None, extra_env=None) -> SupervisorConnection:
    """immutable snapshot을 실 child로 띄우는 `SupervisorConnection`을 만든다(실 socket·SDK).

    `socket_root`가 None이면 supervisor가 명세 §4.2의 socket root(`$XDG_RUNTIME_DIR/vdeter-poc` 또는
    `/tmp/vdeter-poc-<uid>`, workload마다 128-bit nonce dir)를 쓴다. 실 CLI run root(`var/runs/<run-id>/…`)
    아래에 socket을 두면 sun_path 상한(§4.2 check_path_length)을 넘어 child spawn이 실패하므로 production
    CLI는 None을 전달한다. 명시 root는 시험·격리용이며 그 아래에 workload_id를 덧붙인다.
    """

    src_path = src_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    )
    env = {
        "VDETER_POC_SNAPSHOT_SCRIPT": snapshot.script_path,
        "VDETER_POC_ENTRYPOINT": snapshot.descriptor.entrypoint,
        "VDETER_POC_HELLO": json.dumps(snapshot.descriptor.as_hello_value()),
        "VDETER_POC_SESSION": session_id,
        "VDETER_POC_WORKLOAD": workload_id,
        "VDETER_POC_RUN_ID": run_id,
        "VDETER_POC_SCENARIO": scenario_id,
        "PYTHONPATH": src_path + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    if extra_env:
        env.update(extra_env)
    spawner = SubprocessChildSpawner([python_executable or sys.executable, "-m", _CHILD_MODULE],
                                     extra_env=env)
    return SupervisorConnection(
        provider=provider, spawner=spawner,
        session_id=session_id, workload_id=workload_id, run_id=run_id, scenario_id=scenario_id,
        manifest=snapshot.descriptor, server_config=server_config or ServerCapabilityConfig(),
        clock=clock or RealMonotonicClock(),
        # None → 명세 §4.2 root(짧은 경로, nonce dir 격리). 명시 root(시험)는 workload_id로 격리.
        socket_root=(os.path.join(socket_root, workload_id) if socket_root else None),
    )


class PocWorkload:
    """arm(scenario submit 전) / run(scenario 실행 중·후)로 분리한 PoC workload 핸들(§5.1 순서).

    R1: PoC는 scenario submit **전에** arm(child spawn·on_start·ARMED)되어야 하고, 실제 workload event
    처리 후 terminal·finish·evidence·cleanup으로 끝난다. arm 실패는 예외로 올려 caller가 scenario를
    제출하지 않게 한다.
    """

    def __init__(self, runner, connection, provider, seams, steps) -> None:
        self._runner = runner
        self._connection = connection
        self._provider = provider
        self._seams = seams
        self._steps = tuple(steps)

    def arm(self) -> bool:
        """child spawn + on_start → ARMED. 실패 시 예외(scenario submit 금지)."""
        return self._runner.arm()

    def abort(self, scenario_verdict: ScenarioVerdict = ScenarioVerdict.NOT_RUN, *,
              cause: BaseException | None = None):
        """cleanup-only 종료(N1/N2): step 전달·신규 action·FINISH 없이 정리만 한다.

        arm 실패·취소·submit/wait 예외·인터럽트에서 쓴다. `cause`를 실패로 기록해 ERROR/ABORTED로
        확정하고(COMPLETED 금지), 인터럽트는 재전달하지 않고 결과를 돌려준다 — 원형 재전달은 caller가
        관장한다. `PocWorkload.run`과 달리 저장된 scenario step을 전달하지 않는다."""
        return self._runner.abort(scenario_verdict, cause=cause)

    def run(self, scenario_verdict: ScenarioVerdict):
        """ARMED 후: workload event 전달 → terminal → finish → evidence → cleanup. fail-closed 결과."""
        try:
            for step in self._steps:
                if self._connection.infrastructure_error is not None:
                    break
                self._runner.deliver(step)
        except Exception:  # noqa: BLE001 - runner가 기록; finish가 fail-closed 결과화
            pass
        except BaseException:
            self._runner.finish(scenario_verdict)
            raise
        return self._runner.finish(scenario_verdict)

    def run_full(self, scenario_verdict: ScenarioVerdict):
        """arm + run(원자적). arm 실패도 fail-closed 결과로 확정(기존 run_poc_workload 호환)."""
        try:
            armed = self.arm()
            if armed:
                for step in self._steps:
                    if self._connection.infrastructure_error is not None:
                        break
                    self._runner.deliver(step)
        except Exception:  # noqa: BLE001
            return self._runner.finish(scenario_verdict)
        except BaseException:
            self._runner.finish(scenario_verdict)
            raise
        return self._runner.finish(scenario_verdict)

    @property
    def seams(self):
        return self._seams

    @property
    def connection(self):
        return self._connection

    @property
    def provider(self):
        return self._provider


def prepare_poc_workload(*, snapshot: Snapshot, steps, scenario_id: str, run_id: str, workload_id: str,
                         session_id: str, socket_root: str, device_seams, terminal_source=None,
                         dual_channel_installed: bool = False, target_response=None,
                         server_config: ServerCapabilityConfig | None = None,
                         src_path: str | None = None,
                         python_executable: str | None = None) -> PocWorkload:
    """PoC workload를 조립한다(아직 arm하지 않음). caller가 scenario submit 전에 `.arm()`을 호출한다.

    `device_seams` 필수(fail-closed). `terminal_source`는 실제 child terminal 공급자다 — 미주입이면
    executor가 TX_UNKNOWN으로 남긴다(성공 합성 금지). RF-off 시험은 `rf_off.rf_off_terminal_source()`를
    명시 주입한다.
    """

    seams = device_seams
    provider = assemble_provider(seams, dual_channel_installed=dual_channel_installed)
    conn = build_connection(
        snapshot=snapshot, provider=provider, session_id=session_id, workload_id=workload_id,
        run_id=run_id, scenario_id=scenario_id, socket_root=socket_root,
        server_config=server_config, src_path=src_path, python_executable=python_executable)
    runner = WorkloadRunner(
        conn, MassBLease(), run_id=run_id, workload_id=workload_id,
        scenario_id=scenario_id, snapshot=snapshot,
        tx_executor=ProductionTxExecutor(provider, terminal_source=terminal_source,
                                         target_response=target_response))
    return PocWorkload(runner, conn, provider, seams, steps)


def run_poc_workload(*, snapshot: Snapshot, steps, scenario_id: str, run_id: str, workload_id: str,
                     session_id: str, socket_root: str, device_seams,
                     scenario_verdict: ScenarioVerdict = ScenarioVerdict.PASS,
                     dual_channel_installed: bool = False, terminal_source=None, target_response=None,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None, python_executable: str | None = None) -> tuple:
    """한 PoC workload를 arm+run 원자적으로 실행한다(§5.1). 반환 ``(WorkloadResult, seams)``.

    scenario 실행 순서(arm→scenario→run)가 필요한 CLI는 `prepare_poc_workload` + `PocWorkload.arm/run`을
    쓴다. 이 함수는 그 순서가 무관한 focused 시험·단독 실행용이다.
    """

    workload = prepare_poc_workload(
        snapshot=snapshot, steps=steps, scenario_id=scenario_id, run_id=run_id,
        workload_id=workload_id, session_id=session_id, socket_root=socket_root,
        device_seams=device_seams, terminal_source=terminal_source, target_response=target_response,
        dual_channel_installed=dual_channel_installed, server_config=server_config,
        src_path=src_path, python_executable=python_executable)
    result = workload.run_full(scenario_verdict)
    return result, workload.seams


__all__ = (
    "select_and_snapshot",
    "build_connection",
    "PocWorkload",
    "prepare_poc_workload",
    "run_poc_workload",
)
