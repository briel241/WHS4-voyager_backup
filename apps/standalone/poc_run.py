"""Stage 8 PoC 실행을 실제 `vdeter run` CLI에 연결하는 **sanctioned adapter**.

이 파일은 `apps/`에서 `vdeter.stage8`을 import하도록 **명시 허용된 유일한 CLI adapter**다
(import 경계 시험이 이 파일 하나만 allowlist에 둔다). `run_cmd`는 `--poc`/`--poc-dir`가 주어졌을
때만 이 module을 **lazy import**하므로, PoC를 쓰지 않는 일반 `vdeter run`/`--help` 경로는 stage8을
로드하지 않는다(기능 OFF 구조적 0). public SDK가 private supervisor를 역참조하지 않는 계약도 그대로다.

역할:

- `--poc`/`--poc-dir` 소스 해석과 비대화형 오류 처리(§5.3, §6.1).
- scenario-호환 PoC를 선택해 workload evidence 아래 **immutable snapshot**을 만든다(§5.3).
- 실제 child·Unix socket·SDK·engine·Stage 5 provider로 PoC workload를 실행하고 세 독립 판정을
  기존 evidence 형식으로 돌려준다(§11, §12.4).

**device 경계(RF)는 session이 제공한다**(`session.poc_device_seams()`). RF-off에서는 software
device seam, production(LocalCoordinatorSession→coordinator)에서는 실 radio 연결이 필요하다. session이
seam을 제공하지 않으면 조용히 software로 대체하지 않고 **fail-closed**한다(production 미연결을 성공으로
위장하지 않는다). scenario event(선제/반응형 관측)도 session이 제공하며(`poc_workload_steps`), 없으면
선제(`SCENARIO_STARTED`)만 전달한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# stage8 import는 이 sanctioned adapter 안에서만 한다(run_cmd가 PoC 선택 시 lazy import).
from vdeter.stage8.execution.runner import scenario_started
from vdeter.stage8.execution.selection import SelectionError, SourceMode, resolve_source_mode
from vdeter.stage8.execution.verdict import ScenarioVerdict
from vdeter.stage8.integration.entry import run_poc_workload, select_and_snapshot
from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig


class PocRunError(Exception):
    """PoC 선택·실행 계약 위반(fail-closed). CLI가 종료 코드로 변환한다."""


@dataclass(frozen=True)
class PocOption:
    """CLI `--poc`/`--poc-dir` 선택 상태."""

    mode: SourceMode
    poc: str | None = None
    poc_dir: str | None = None

    @property
    def active(self) -> bool:
        return self.mode != SourceMode.OFF


def resolve_option(*, poc: str | None, poc_dir: str | None, interactive: bool) -> PocOption:
    """`--poc`/`--poc-dir`를 해석한다(§5.3, §6.1). 위반은 :class:`PocRunError`.

    - 두 option 동시 사용 금지.
    - `--poc-dir`는 대화형 전용(비대화형 prompt 금지). 비대화형은 `--poc`만.
    """

    try:
        mode = resolve_source_mode(poc=poc, poc_dir=poc_dir, interactive=interactive)
    except SelectionError as error:
        raise PocRunError(str(error)) from error
    return PocOption(mode=mode, poc=poc, poc_dir=poc_dir)


def select_snapshot(option: PocOption, *, scenario_id: str, dest_dir: str,
                    poc_id: str | None = None,
                    server_config: ServerCapabilityConfig | None = None,
                    interactive: bool = True):
    """scenario-호환 PoC를 선택해 immutable snapshot을 만든다. baseline(none)/OFF면 None.

    catalog mode(`--poc-dir`)에서 `poc_id`가 None/`"none"`이면 baseline(PoC 미사용)이다.
    """

    if not option.active:
        return None
    try:
        return select_and_snapshot(
            scenario_id=scenario_id, dest_dir=dest_dir,
            poc=option.poc, poc_dir=option.poc_dir, poc_id=poc_id,
            server_config=server_config or ServerCapabilityConfig(),
            interactive=interactive)
    except SelectionError as error:
        raise PocRunError(str(error)) from error


def catalog_menu_lines(option: PocOption, scenario_id: str,
                       server_config: ServerCapabilityConfig | None = None) -> list:
    """catalog(`--poc-dir`) mode에서 scenario-호환 PoC 선택 menu 줄을 만든다(§6.1).

    compatible PoC와 `none`만 선택 가능(단회 표시, countdown 없음). direct(`--poc`)면 빈 목록.
    """
    if not option.poc_dir:
        return []
    from vdeter.stage8.execution.catalog import scan_catalog
    from vdeter.stage8.execution.selection import build_menu
    catalog = scan_catalog(option.poc_dir)
    menu = build_menu(catalog, scenario_id,
                      server_config=server_config or ServerCapabilityConfig())
    return menu.render().splitlines()


def _default_steps():
    # session이 scenario event를 주지 않으면 선제(SCENARIO_STARTED)만 전달한다.
    return (scenario_started(),)


def run_for_workload(session, snapshot, *, scenario_id: str, run_id: str, workload_id: str,
                     socket_root: str, scenario_verdict: ScenarioVerdict = ScenarioVerdict.PASS,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None) -> dict:
    """한 workload의 PoC를 실제 실행 경로로 돌리고 JSON evidence를 돌려준다(§5.1, §11, §12.4).

    device 경계는 `session.poc_device_seams()`에서 온다(없으면 fail-closed). scenario event는
    `session.poc_workload_steps(scenario_id)`가 있으면 그것을, 없으면 선제 event만 쓴다.
    """

    seams_fn = getattr(session, "poc_device_seams", None)
    if not callable(seams_fn):
        raise PocRunError(
            "session이 PoC device 경계(poc_device_seams)를 제공하지 않습니다 — production radio "
            "연결이 필요합니다(RF-off 기본값으로 조용히 대체하지 않음, fail-closed)")
    device_seams = seams_fn()
    if device_seams is None:
        raise PocRunError("session.poc_device_seams()가 None을 반환했습니다(device 경계 미연결)")

    steps_fn = getattr(session, "poc_workload_steps", None)
    steps = steps_fn(scenario_id) if callable(steps_fn) else None
    if not steps:
        steps = _default_steps()

    os.makedirs(socket_root, exist_ok=True)
    result, seams = run_poc_workload(
        snapshot=snapshot, steps=steps, scenario_id=scenario_id, run_id=run_id,
        workload_id=workload_id, session_id=run_id, socket_root=socket_root,
        device_seams=device_seams, scenario_verdict=scenario_verdict,
        server_config=server_config or ServerCapabilityConfig(), src_path=src_path)
    evidence = result.as_json_value()
    # 방사(software submit) 요약 — 실 RF atomicity 증명이 아니라 RF-off 호출 기록이다.
    group_tx = getattr(seams, "group_tx", None)
    evidence["radiation"] = {
        "committed_groups": int(getattr(group_tx, "committed_groups", 0) or 0),
        "radiated_children": int(getattr(group_tx, "radiated_children", 0) or 0),
    }
    return evidence


__all__ = (
    "PocRunError",
    "PocOption",
    "resolve_option",
    "select_snapshot",
    "run_for_workload",
)
