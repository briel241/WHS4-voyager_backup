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
from vdeter.stage8.integration.entry import (
    prepare_poc_workload,
    run_poc_workload,
    select_and_snapshot,
)
from vdeter.stage8.poc_api.errors import PocError, PocErrorCode
from vdeter.stage8.poc_api.negotiation import ServerCapabilityConfig


class PocRunError(Exception):
    """PoC 선택·실행 계약 위반(fail-closed). CLI가 종료 코드로 변환한다."""


#: 이 adapter가 설치 capability를 판정하는 **유일한** capability 이름(§7.2.4, POC-D22).
DUAL_CHANNEL_CAPABILITY = "observe_dual_physical_channel"


def installed_server_config(paths=None, installation=None) -> ServerCapabilityConfig:
    """설치 capability 증언에서 server capability 설정을 만든다(유일한 권위 producer).

    A(설치 capability)만 결정한다. runtime readiness(B: `dual_ready`)나 RF 실행 권한/자격(C)은
    여기에 섞지 않는다. 증언이 없거나 stale·불완전·미검증이면 미광고(fail-closed)다.

    `paths`/`installation`을 주지 않으면 이 프로세스가 속한 설치에서 읽는다 — coordinator·RF-off
    세션·CLI가 모두 같은 근거를 쓰게 하기 위함이다. 설치 기록을 읽을 수 없으면 미지원으로 답한다.
    """
    from apps.standalone import capability_attestation, installation_manifest
    from apps.standalone.state import StatePaths

    if paths is None:
        paths = StatePaths()
    if installation is None:
        try:
            installation = installation_manifest.load_installation(paths)
        except installation_manifest.InstallationError:
            return ServerCapabilityConfig()          # 설치 기록 없음/변조 → 미광고
    supported, _reasons = capability_attestation.support(
        paths, installation, capability=DUAL_CHANNEL_CAPABILITY)
    return ServerCapabilityConfig(wsm_g1_production_installed=bool(supported))


def installed_capability_evidence(paths=None, installation=None) -> dict:
    """evidence·보고용 설치 capability 요약(판정은 `installed_server_config`와 동일 근거)."""
    from apps.standalone import capability_attestation, installation_manifest
    from apps.standalone.state import StatePaths

    if paths is None:
        paths = StatePaths()
    if installation is None:
        try:
            installation = installation_manifest.load_installation(paths)
        except installation_manifest.InstallationError as exc:
            return {"capability": DUAL_CHANNEL_CAPABILITY, "supported": False,
                    "reasons": [f"installation record unavailable: {exc}"]}
    return capability_attestation.describe(
        paths, installation, capability=DUAL_CHANNEL_CAPABILITY)


def collision_state_error(message, *, ownership=False):
    """Map internal TEST/RX safety failure to public PoC severity at the sanctioned boundary."""
    return PocError(PocErrorCode.POC_RADIO_OWNERSHIP_UNCERTAIN if ownership
                    else PocErrorCode.POC_COLLISION_STATE_UNAVAILABLE, message)


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


#: catalog(--poc-dir) menu 제어 입력(§6.1). q=session 종료, r=rescan(READY 전용), 나머지는 poc_id.
MENU_QUIT = frozenset({"q", "quit", "exit"})
MENU_RESCAN = frozenset({"r", "rescan"})
MENU_BASELINE = frozenset({"none", "0"})


def resolve_catalog_selection(option: PocOption, scenario_id: str, choice: str, *,
                              server_config: ServerCapabilityConfig | None = None) -> tuple:
    """표시된 catalog menu 입력을 (kind, poc_id)로 해석한다(§6.1, R7).

    kind ∈ {"quit","rescan","baseline","poc","invalid"}. q→quit(scenario submit 0), r→rescan(READY
    전용, submit 0), none/0→baseline, 숫자·poc_id는 menu.resolve로 해당 package로 해석, 그 외 무효는
    invalid(submit 0). 표시한 제어(q/r)를 poc_id로 넘기지 않는다.
    """
    lowered = (choice or "").strip().lower()
    if lowered in MENU_QUIT:
        return "quit", None
    if lowered in MENU_RESCAN:
        return "rescan", None
    if lowered in MENU_BASELINE:
        return "baseline", None
    from vdeter.stage8.execution.catalog import scan_catalog
    from vdeter.stage8.execution.selection import build_menu
    catalog = scan_catalog(option.poc_dir)
    menu = build_menu(catalog, scenario_id,
                      server_config=server_config or ServerCapabilityConfig())
    item = menu.resolve((choice or "").strip())
    if item is None:
        return "invalid", None
    if item.kind == "none":
        return "baseline", None
    return "poc", item.poc_id


def _session_device_boundary(session, scenario_id: str) -> tuple:
    """session이 제공하는 PoC device 경계 요소(provider|seams·steps·terminal_source·target_response).

    session이 `poc_provider()`를 제공하면 **이미 조립된 권위 provider**를 그대로 쓴다(권위
    `schedule_tx` 경로 결속). 없으면 기존처럼 `poc_device_seams()`로 provider를 조립한다.
    둘 다 없으면 fail-closed다(조용한 RF-off 대체 금지).
    """
    provider_fn = getattr(session, "poc_provider", None)
    provider = provider_fn() if callable(provider_fn) else None
    seams_fn = getattr(session, "poc_device_seams", None)
    if provider is None and not callable(seams_fn):
        raise PocRunError(
            "session이 PoC device 경계(poc_device_seams)를 제공하지 않습니다 — production radio "
            "연결이 필요합니다(RF-off 기본값으로 조용히 대체하지 않음, fail-closed)")
    device_seams = seams_fn() if callable(seams_fn) else None
    if provider is None and device_seams is None:
        raise PocRunError("session.poc_device_seams()가 None을 반환했습니다(device 경계 미연결)")
    steps_fn = getattr(session, "poc_workload_steps", None)
    steps = steps_fn(scenario_id) if callable(steps_fn) else None
    if not steps:
        steps = _default_steps()
    ts_fn = getattr(session, "poc_terminal_source", None)
    terminal_source = ts_fn() if callable(ts_fn) else None  # 없으면 executor가 TX_UNKNOWN
    tr_fn = getattr(session, "poc_target_response", None)
    target_response = tr_fn() if callable(tr_fn) else None
    return device_seams, steps, terminal_source, target_response, provider


def prepare_workload(session, snapshot, *, scenario_id: str, run_id: str, workload_id: str,
                     socket_root: str | None = None,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None):
    """PoC child를 spawn·연결·ARMED까지 준비한다(scenario submit 전, §5.1). 실패는 :class:`PocRunError`.

    반환 :class:`~vdeter.stage8.integration.entry.PocWorkload`(ARMED). arm 실패 시 자원을 정리하고
    fail-closed evidence를 담아 `PocRunError`(`.evidence`)로 올린다 — caller는 scenario를 제출하지 않는다.
    `socket_root=None`이면 명세 §4.2 socket root(짧은 경로·nonce dir)를 supervisor가 고른다(production).
    """
    prepare_scope = getattr(session, "prepare_poc_scope", None)
    if callable(prepare_scope):
        prepare_scope(snapshot.descriptor)
        session_config = getattr(session, "poc_server_config", None)
        if not callable(session_config):
            raise PocRunError("prepared RX scope has no verified server capabilities")
        resolved = session_config()
        # 근거가 둘이면 조용히 덮어쓰지 않는다. caller와 session은 같은 설치 증언을 읽어야 하므로
        # 광고 capability가 다르면 계약 위반으로 거부한다(fail-closed).
        if server_config is not None and (
                server_config.advertised_capabilities() != resolved.advertised_capabilities()):
            raise PocRunError(
                "server capability conflict: caller advertises %s but the session's installation "
                "attestation advertises %s — refusing rather than silently overriding"
                % (sorted(server_config.advertised_capabilities()),
                   sorted(resolved.advertised_capabilities())))
        server_config = resolved
    if server_config is None:
        # session이 제공하지 않는 경로(RF-off 등)도 같은 권위 producer를 쓴다 — 조용한 기본 False 금지.
        server_config = installed_server_config()
    device_seams, steps, terminal_source, target_response, provider = _session_device_boundary(
        session, scenario_id)
    if socket_root:
        os.makedirs(socket_root, exist_ok=True)  # 명시 root(시험·격리)만 미리 만든다
    workload = prepare_poc_workload(
        snapshot=snapshot, steps=steps, scenario_id=scenario_id, run_id=run_id,
        workload_id=workload_id, session_id=run_id, socket_root=socket_root,
        device_seams=device_seams, terminal_source=terminal_source, target_response=target_response,
        provider=provider,
        server_config=server_config or ServerCapabilityConfig(), src_path=src_path)
    try:
        armed = workload.arm()
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        # N2: 취소·인터럽트는 cleanup-only(신규 event/action·FINISH 없음) 뒤 **원형 그대로** 재전달한다.
        # PocRunError로 변환하지 않고, cleanup 과정에서 scenario step을 전달하지 않는다(COMPLETED 금지).
        workload.abort(ScenarioVerdict.NOT_RUN, cause=exc)
        raise
    except Exception as exc:  # noqa: BLE001 - arm 실패(일반 예외): 자원 정리 후 fail-closed 보고
        evidence = _finalize_evidence(workload, ScenarioVerdict.NOT_RUN, cause=exc)
        error = PocRunError(f"PoC arm 실패: {type(exc).__name__}: {exc}")
        error.evidence = evidence
        raise error from exc
    if not armed:
        evidence = _finalize_evidence(workload, ScenarioVerdict.NOT_RUN,
                                      cause=PocError(PocErrorCode.POC_PROTOCOL_ERROR,
                                                     "ARMED 미도달"))
        error = PocRunError("PoC가 ARMED에 도달하지 못했습니다(scenario submit 금지)")
        error.evidence = evidence
        raise error
    return workload


def scenario_verdict_from_status(status) -> ScenarioVerdict:
    """coordinator workload 결과 status(문자열)를 권위 scenario verdict로 매핑한다(R4, §11.1).

    run_cmd는 stage8을 직접 import하지 않고(import 경계 §S8-POC-API-REQ-01) 이 adapter 함수를 쓴다.
    PASS만 PASS, 그 외(FAIL/NOT_RUN/None 등)는 FAIL(성공 추정 금지).
    """
    return ScenarioVerdict.PASS if status == "PASS" else ScenarioVerdict.FAIL


def finish_workload(workload, *, scenario_verdict: ScenarioVerdict, guard=None) -> dict:
    """ARMED된 PoC를 실 scenario 실행 뒤 종료·evidence 수집한다(§5.1, §11, §12.4). 반환 JSON evidence.

    `guard`(선택)는 각 scenario step 전달 **전에** 호출되는 검사 훅이다(P11: 실행 중 권위 deadline
    만료·운영자 중단 감지). 만료 시 남은 step 없이 cleanup-only로 종료하고 원인을 올린다.
    """
    result = workload.run(scenario_verdict, guard=guard)
    return _result_evidence(result, workload)


def _finalize_evidence(workload, scenario_verdict: ScenarioVerdict, *,
                       cause: BaseException | None = None) -> dict:
    """arm 실패·취소 등에서 **cleanup-only**로 evidence만 확정한다(N2).

    이전에는 `workload.run`을 불러 저장된 scenario step을 전달했고, 그 결과 취소 cleanup 중에도
    SCENARIO_STARTED가 전달돼 RF-off TX group이 생성되고 evidence가 COMPLETED로 남는 결함이 있었다.
    이제 `workload.abort`(step 전달·신규 provider.submit·FINISH 없음)를 써서 정리만 하고 `cause`를
    실패로 기록해 ERROR/ABORTED로 확정한다. 이미 제출된 action은 신규 제출 없이 소유권만 추적한다."""
    result = workload.abort(scenario_verdict, cause=cause)
    return _result_evidence(result, workload)


def abort_prepared(prep: dict, *, cause: BaseException, out,
                   scenario_verdict=ScenarioVerdict.NOT_RUN) -> dict | None:
    """N1: 이미 ARMED된 PoC를 scenario submit/wait/render 실패·인터럽트에서 cleanup-only로 정리한다.

    신규 event/action 없이 child·socket·workload dir·provider 예약을 정리하고, `cause`를 실패로
    기록해 ERROR/ABORTED evidence를 만든다. **원래 예외/인터럽트를 cleanup 오류로 덮지 않도록** 자체
    예외(재전달된 인터럽트 포함)는 삼키고 evidence를 `prep["evidence"]`에 남긴다 — 원형 재전달은
    caller가 한다. coordinator session.close만으로 PoC가 정리됐다고 추정하지 않는다."""
    workload = (prep or {}).get("workload")
    if workload is None:
        return None
    evidence = None
    try:
        result = workload.abort(scenario_verdict=scenario_verdict, cause=cause)
        evidence = _result_evidence(result, workload)
    except BaseException as cleanup_exc:  # noqa: BLE001 - 원래 예외 보존; cleanup 오류는 evidence로만
        evidence = {"status": "ERROR", "poc_infrastructure": "ABORTED",
                    "infrastructure_error_code": "POC_RADIO_OWNERSHIP_UNCERTAIN",
                    "cleanup_error": type(cleanup_exc).__name__}
    prep["evidence"] = evidence
    try:
        out("  poc aborted (scenario path %s) infrastructure=%s severity=%s"
            % (type(cause).__name__, evidence.get("poc_infrastructure"), poc_severity(evidence)))
    except Exception:  # noqa: BLE001 - 로그 실패가 정리·재전달을 막지 않는다
        pass
    return evidence


def _result_evidence(result, workload) -> dict:
    evidence = result.as_json_value()
    seams = workload.seams
    group_tx = getattr(seams, "group_tx", None)
    # R5: cleanup 전 snapshot한 Stage 5 provider evidence(readiness/composite/model_boundary)를 올린다.
    conn = workload.connection
    provider_evidence = getattr(conn, "provider_evidence", None)
    if provider_evidence is not None:
        evidence["provider_evidence"] = provider_evidence
    # 방사 요약 — 실 RF atomicity 증명이 아니라 RF-off 호출 기록이다. `SoftwareGroupTx` double은 자체
    # 카운터(committed_groups)를 노출하고, 실 Stage 7 worker seam은 그렇지 않으므로 공유 provider
    # evidence(broadcast_infrastructure_result)에서 committed parent·child 수를 유도한다(동일 수치).
    committed = getattr(group_tx, "committed_groups", None)
    radiated = getattr(group_tx, "radiated_children", None)
    if committed is None or radiated is None:
        bir = (provider_evidence or {}).get("broadcast_infrastructure_result") or {}
        committed_ids = set(bir.get("committed_parent_ids") or [])
        committed = len(committed_ids)
        radiated = sum(len(p.get("children") or []) for p in (bir.get("parents") or [])
                       if p.get("parent_id") in committed_ids)
    evidence["radiation"] = {
        "committed_groups": int(committed or 0),
        "radiated_children": int(radiated or 0),
    }
    return evidence


def poc_severity(evidence: dict) -> str:
    """PoC infrastructure 오류의 severity(§14.1): OK/REQUEST_REJECTED/POC_ABORT/SESSION_INHIBIT.

    R2: outer loop이 다음 workload 허용 여부·session 안전 상태를 이 severity로 결정한다.
    """
    if evidence is None:
        return "OK"
    if evidence.get("status") in ("REFUSED", "ERROR"):
        return "POC_ABORT"
    infra = evidence.get("poc_infrastructure")
    if infra not in ("ABORTED", "ERROR"):
        return "OK"
    code = evidence.get("infrastructure_error_code")
    if not code:
        return "POC_ABORT"
    try:
        return PocError(PocErrorCode(code)).severity.value
    except (ValueError, KeyError):
        return "POC_ABORT"  # 알 수 없는 code(예외 등)는 보수적으로 abort


def run_for_workload(session, snapshot, *, scenario_id: str, run_id: str, workload_id: str,
                     socket_root: str, scenario_verdict: ScenarioVerdict = ScenarioVerdict.PASS,
                     server_config: ServerCapabilityConfig | None = None,
                     src_path: str | None = None) -> dict:
    """한 workload의 PoC를 arm+run 원자적으로 실행한다(순서 무관 focused 경로). 반환 JSON evidence.

    scenario 실행 순서(arm→scenario→run)가 필요한 CLI는 `prepare_workload`+`finish_workload`를 쓴다.
    """
    device_seams, steps, terminal_source, target_response, provider = _session_device_boundary(
        session, scenario_id)
    os.makedirs(socket_root, exist_ok=True)
    result, seams = run_poc_workload(
        snapshot=snapshot, steps=steps, scenario_id=scenario_id, run_id=run_id,
        workload_id=workload_id, session_id=run_id, socket_root=socket_root,
        device_seams=device_seams, terminal_source=terminal_source, target_response=target_response,
        scenario_verdict=scenario_verdict,
        server_config=server_config or ServerCapabilityConfig(), src_path=src_path)
    evidence = result.as_json_value()
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
    "resolve_catalog_selection",
    "prepare_workload",
    "abort_prepared",
    "scenario_verdict_from_status",
    "finish_workload",
    "poc_severity",
    "run_for_workload",
)
