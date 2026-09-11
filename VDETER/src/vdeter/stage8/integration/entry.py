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
from ..execution.selection import SelectionError, SourceMode, build_menu, resolve_source_mode
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
    if mode == SourceMode.OFF:
        return None
    if mode == SourceMode.DIRECT:
        base = _base_of_path(poc)
        return create_snapshot(os.path.dirname(os.path.abspath(poc)), base,
                               dest_dir=dest_dir, scenario_id=scenario_id, uid=uid)
    # CATALOG: scenario-first 선택.
    catalog = scan_catalog(poc_dir, uid=uid)
    menu = build_menu(catalog, scenario_id, server_config=server_config or ServerCapabilityConfig())
    if poc_id is None or poc_id == "none":
        return None  # baseline: PoC 자원 0(구조적 0).
    if poc_id not in menu.selectable_ids():
        raise SelectionError(
            f"PoC {poc_id!r}는 scenario {scenario_id}에서 선택할 수 없습니다(호환/유효성/capability)")
    entry = catalog.valid_by_id()[poc_id]
    base = os.path.basename(entry.script.path)[:-3]
    return create_snapshot(poc_dir, base, dest_dir=dest_dir, scenario_id=scenario_id, uid=uid)


def build_connection(*, snapshot: Snapshot, provider, session_id: str, workload_id: str,
                     run_id: str, scenario_id: str, socket_root: str,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None, python_executable: str | None = None,
                     clock=None, extra_env=None) -> SupervisorConnection:
    """immutable snapshot을 실 child로 띄우는 `SupervisorConnection`을 만든다(실 socket·SDK)."""

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
        socket_root=os.path.join(socket_root, workload_id),
    )


def run_poc_workload(*, snapshot: Snapshot, steps, scenario_id: str, run_id: str, workload_id: str,
                     session_id: str, socket_root: str, device_seams,
                     scenario_verdict: ScenarioVerdict = ScenarioVerdict.PASS,
                     dual_channel_installed: bool = False, target_response=None,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None, python_executable: str | None = None) -> tuple:
    """한 PoC workload를 production 경로로 실행한다(§5.1 lifecycle 전체).

    `device_seams`는 **필수**다(fail-closed). RF-off 통합·시험에서는 `rf_off.rf_off_seams()`를 호출해
    명시적으로 넘기고, 실장비(Stage 6)에서는 실 Stage 7 seam으로 만든 `DeviceSeams`를 넘긴다. entry가
    시험용 seam을 조용히 기본값으로 쓰지 않는다. 반환 ``(WorkloadResult, seams)``. seams.group_tx로
    실제 방사(software submit) 기록을 검사할 수 있다.
    """

    seams = device_seams
    provider = assemble_provider(seams, dual_channel_installed=dual_channel_installed)
    conn = build_connection(
        snapshot=snapshot, provider=provider, session_id=session_id, workload_id=workload_id,
        run_id=run_id, scenario_id=scenario_id, socket_root=socket_root,
        server_config=server_config, src_path=src_path, python_executable=python_executable)
    runner = WorkloadRunner(conn, MassBLease(), run_id=run_id, workload_id=workload_id,
                            scenario_id=scenario_id, snapshot=snapshot,
                            tx_executor=ProductionTxExecutor(provider, target_response=target_response))
    result = runner.run(WorkloadSpec(steps=tuple(steps), scenario_verdict=scenario_verdict))
    return result, seams


__all__ = (
    "select_and_snapshot",
    "build_connection",
    "run_poc_workload",
)
