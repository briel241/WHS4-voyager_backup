"""`vdeter qualify` — 단독 RR 재자격 orchestrator (Phase I W5, README §7).

확정 사다리 (결정 20 → 2026-09-04 정책 변경으로 RR-P2-19 제거):

    1. RR-P0                      → TC-STAGE7-RR-001
    2. RR-P1 (+gain 측정)         → TC-RR-002·003·004 + gain artifact
    3. candidate reference 생성    (이후 단계·향후 run이 쓸 결속값 확정)
    4. RR-P2-17                   → TC-RR-005
    5. RR-P3-fault                → TC-RR-007 (판정 필수)
    6. verification bundle → final builder 검증
    7. 전체 PASS 시에만 candidate → releases/ 이동 + active 포인터 원자 교체

soak(RR-008)·**Link 19(RR-006)** 는 범위 제외. Link 19는 현재 프로젝트의
운용 시나리오 3종(Link 11/17)이 쓰지 않아 standalone 자격·운용 범위에서
제외한다 — 구현·profile·registry·단위시험은 그대로 존재하며, final builder의
전역 정책(RR-006 = DEFERRED_TCS 기록 전용, REQUIRED_TCS = RR-001~005 + 007)은
바뀌지 않는다. 2차 ladder qual-20260904-055636이 rr-p2-19의 ROC RX
`LinkIdDetectionError`(frozen PHY 계층)로 정지한 뒤 결정됐다. 어느 단계든
실패하면 candidate 미승격 — 기존 active reference 유지.

발급 원칙 (승인 조건 2·3)
    - `stage7_issue_qualification_reference.py` CLI는 호출하지 않는다 (SSH·
      Git 결합). reference **스키마**와 검증기(`apps.stage7_profile.
      load_reference`)를 재사용해 로컬 발급하고, 발급 직후 그 검증기로
      재검증한다.
    - final builder는 `build(..., head_provider=...)`로 호출하고 head는
      standalone-binding `source_head`를 공급한다.
    - RR 판정 코드·threshold·deadline은 무수정 — coordinator를 phase 인자로
      spawn만 한다 (프로세스 구성은 결정 6과 동일 패턴).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from apps.standalone import deploy_manifest, identity, local_config
from apps.standalone import qualification_domains as domains
from apps.standalone.local_session import build_child_env
from apps.standalone.state import (
    SingleInstanceLock,
    StatePaths,
    allocate_run_dir,
    write_json_atomic,
)

#: 사다리의 coordinator phase 순서 (candidate 생성은 p1 직후 별도 단계).
#: rr-p2-19(Link 19)는 2026-09-04 정책 변경으로 제거 — 아래 LINK19_LIMITATION.
LADDER_PHASES = ("rr-p0", "rr-p1", "rr-p2-17", "rr-p3-fault")

#: phase별 그 run이 산출·PASS해야 하는 TC — 단계 실패 기준. final builder의
#: REQUIRED_TCS(RR-001~005 + 007)와 정확히 대응한다.
PHASE_EXPECTED_TCS = {
    "rr-p0": ("TC-STAGE7-RR-001",),
    "rr-p1": ("TC-STAGE7-RR-002", "TC-STAGE7-RR-003", "TC-STAGE7-RR-004"),
    "rr-p2-17": ("TC-STAGE7-RR-005",),
    "rr-p3-fault": ("TC-STAGE7-RR-007",),
}

#: 최종 reference·보고서에 명시하는 Link 19 제한 (사용자 지정 문구).
LINK19_LIMITATION = {
    "link_19_implementation": "present",
    "link_19_standalone_qualification": "not exercised",
    "link_19_hardware_performance": "not guaranteed",
    "operational_scenario_links": "Link 11/17 only",
    "link_19_note": (
        "Link 19 implementation exists in the tree (PHY/profile/registry/"
        "unit tests untouched) but is not exercised by the standalone "
        "qualification ladder and its hardware performance is not "
        "guaranteed. The three operational scenarios use Link 11/17 only. "
        "TC-STAGE7-RR-006 remains DEFERRED (record-only) in the final "
        "builder; REQUIRED_TCS are unchanged."),
}
#: RR-P3-fault 물리 USB detach 제외 (2026-09-04 정책 변경, 사용자 지정 문구).
USB_DETACH_LIMITATION = {
    "physical_usb_detach_recovery": "not exercised",
    "b210_hot_unplug_reenumeration_recovery": "not guaranteed",
    "session_close_and_next_run_b210_rediscovery": "verified as before",
    "vdeter_normal_communication_link17_scenarios": "not affected",
    "usb_detach_note": (
        "TC-STAGE7-RR-007 is judged on the six software fault subcases "
        "(stale-generation-command, worker-kill, coordinator-ipc-loss, "
        "restart-generation-fencing, scenario-controller-ipc-loss, "
        "stale-callback). The physical USB detach / re-attach / USB3 speed "
        "restore / UHD re-enumeration subcase is excluded from the standalone "
        "qualification scope: it is a hardware fault-recovery test the "
        "project does not need, not a relaxation of VDE-TER protocol "
        "judgement. Known issue (not fixed here): write_rr_evidence asserts "
        "status in PASS/FAIL/ABORTED and would reject HARDWARE_BLOCKED; that "
        "path is no longer reachable."),
}
#: reference `limitations`에 담기는 전체 제한 (키 충돌 없음).
QUALIFICATION_LIMITATIONS = {**LINK19_LIMITATION, **USB_DETACH_LIMITATION}

#: phase별 resolved config run_mode — **명시 고정** (2026-09-04 사용자 승인).
#:
#: 1차 RF qualification(ladder qual-20260904-053720)이 RR-P1에서
#: `calibrate_rf PREPARE REJECTED`로 실패한 원인: rr-p1을 template 기본값
#: (RF_OFF_QUALIFICATION)으로 생성했는데 endpoint runtime의
#: `pi_runtime.check_calibrate`는 탐색 모드 gain 측정을 run_mode ==
#: GAIN_CALIBRATION에서만 수락한다 (Phase F rr-p1 evidence도 전부
#: GAIN_CALIBRATION). RF·장비·배선 문제가 아닌 orchestrator 결함이었다.
#:
#:   rr-p0        RF_OFF_QUALIFICATION  — RF-off 생애주기, TX 0
#:   rr-p1        GAIN_CALIBRATION      — gain 입력 없음; 새 gain artifact 산출
#:   rr-p2-17     OPERATIONAL           — rr-p1 산출 gain을 결속(loader 계약)
#:   rr-p3-fault  OPERATIONAL           — 동일
PHASE_RUN_MODE = {
    "rr-p0": "RF_OFF_QUALIFICATION",
    "rr-p1": "GAIN_CALIBRATION",
    "rr-p2-17": "OPERATIONAL",
    "rr-p3-fault": "OPERATIONAL",
}
#: rr-p1이 산출한 gain artifact를 결속하는 phase (= OPERATIONAL phase).
GAIN_CONSUMING_PHASES = ("rr-p2-17", "rr-p3-fault")

#: coordinator deadline + 회수·정리 여유.
PHASE_TIMEOUT_MARGIN_S = 900

#: verification bundle의 시험 명령 (로그 파일명은 final builder 계약).
UNIT_CONTRACT_TARGETS = ("VDETER/tests/unit", "VDETER/tests/contract")
INTEGRATION_TARGETS = ("VDETER/tests/integration",)

#: standalone verification bundle에서 **명시적으로 분리**하는 governance/architecture
#: 시험. 이들은 배포 노드의 RF/PHY/MAC/service 동작이 아니라 **저장소·문서·CI
#: 거버넌스**(docs/core traceability, source-tree 구조, import-graph 아키텍처)를
#: 감사하므로 배포 트리에서 구조적으로 실패한다 — 배포에 docs/core가 없고
#: (traceability.toml 부재), dev 전용 legacy/ archive가 없으며, gnuradio가 설치된
#: 노드의 단일-프로세스 suite에서는 선행 시험이 sys.modules를 오염시킨다.
#: 이 목록은 **정확한 node-id 단위 --deselect**다 (광범위 --ignore나 실패 무시가
#: 아니다). 여기(standalone bundle)에서만 제외하며 **host V1/CI에서는 계속 실행**된다
#: (host는 qualify_cmd를 거치지 않고 suite를 직접 돈다). 각 항목 (node-id, 사유).
#: import-graph 2건은 clean 별도 프로세스 재실행에서 PASS 확인(=runtime 결함 아님),
#: legacy 1건은 배포 미포함 archive 존재를 요구하는 저장소 구조 감사(배포 src의
#: legacy 부재 자체는 통과). 2026-09-04 사용자 승인(option 1).
GOVERNANCE_DESELECT = (
    ("tests/contract/test_stage5_regression_audit.py::"
     "Stage5TraceabilityAuditTest",
     "docs/core/traceability.toml 로드 — 저장소 문서 추적성 감사(배포 미포함)"),
    ("tests/contract/test_arch_traceability.py::TraceabilitySchemaTest",
     "docs/core/traceability.toml 스키마·ID 유일성·evidence 게이트 감사"),
    ("tests/contract/test_link_stage0_regression.py::"
     "LinkStage0RegressionTest",
     "docs/core/traceability.toml 기반 stage0 케이스 구성 감사"),
    ("tests/contract/test_arch_structure.py::TargetPackageImportTest::"
     "test_TC_ARCH_001_core_import_without_gnuradio_uhd",
     "import-graph 아키텍처 감사 — clean 인터프리터 요구. gnuradio 설치 노드의 "
     "단일-프로세스 suite에서 선행 시험이 sys.modules를 오염(격리 재실행 PASS)"),
    ("tests/contract/test_arch_structure.py::TargetPackageImportTest::"
     "test_TC_ARCH_001_radio_skeletons_import_without_dependencies",
     "위와 동일 — radio skeleton import 무의존 감사(격리 재실행 PASS)"),
    ("tests/contract/test_arch_structure.py::ForbiddenImportTest::"
     "test_TC_ARCH_002_legacy_is_outside_the_source_tree",
     "source-tree 구조 감사 — dev 전용 legacy/temporary_bpsk archive 존재 요구"
     "(배포 미포함). 배포 src에 legacy 부재는 확인됨"),
)
#: suite 실행 상한 (W5 보완 1 — Phase H 실측: unit+contract ~4분,
#: integration stage7 부분집합 여유 포함).
UNIT_CONTRACT_TIMEOUT_S = 1800
#: 2026-09-04 사용자 승인(V2 착수 직전): 5400→14400초. integration 전체가
#: Windows 데스크톱에서 1시간 07분~19분이고 Pi 5 소요는 미측정이라, RF
#: 사다리 완주 뒤 소프트웨어 검증이 상한 초과로 승격을 막는 낭비를 막기
#: 위한 **실행 안전 상한 확장**이다. 시험 PASS 기준·필수 TC·RR phase
#: deadline·RF threshold는 바뀌지 않는다.
INTEGRATION_TIMEOUT_S = 14400

#: 강제 정리 대기 (TERM 후 / KILL 후).
ESCALATE_TERM_WAIT_S = 30.0
ESCALATE_KILL_WAIT_S = 10.0

REFERENCE_BASENAME = "qualification-reference.json"


class QualifyError(RuntimeError):
    """자격 절차 거부/실패 — candidate는 승격되지 않는다."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _process_group_alive(pgid: int) -> bool:
    """POSIX: process group에 살아 있는 구성원이 남았는가 (signal 0 probe)."""
    import os as _os
    try:
        _os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # 존재하지만 신호 권한 없음 — 살아 있다
        return True


def run_child_bounded(argv, *, cwd, env, stdout_path: Path,
                      timeout_s: float,
                      term_wait_s: float = ESCALATE_TERM_WAIT_S,
                      kill_wait_s: float = ESCALATE_KILL_WAIT_S,
                      natural_group_wait_s: float = 5.0) -> dict:
    """자식(과 그 process group 전체)을 어떤 경로에서도 반드시 정리한다.

    W5 보완 1(3차): leader가 **정상 rc=0으로 종료해도** 손자가 group에
    남을 수 있다. 그 경우 ① 자연 소멸을 짧게 bounded 대기하고 ② 남으면
    `cleanup_anomaly=True`를 기록한 뒤 ③ group에 TERM→KILL을 수행하고
    ④ 소멸을 재확인한다 — group_cleared=True가 되어도 anomaly는 자격
    단계 실패 사유다 (호출자 게이트). timeout·중단 경로도 동일하게 group
    소멸을 실측 확인한다. 잔존 probe는 사후 판정용이지 이 정리의 대용이
    아니다.

    반환: {"rc", "timed_out", "interrupted", "escalation", "group_cleared",
    "cleanup_anomaly"} — group_cleared는 POSIX에서 True/False,
    비-POSIX(개발 환경)는 None.
    """
    import os as _os
    import signal as _signal

    use_group = _os.name == "posix"
    popen_kwargs = {}
    if use_group:
        popen_kwargs["start_new_session"] = True
    escalation = []
    timed_out = False
    interrupted = False
    group_cleared = None
    cleanup_anomaly = False
    with open(stdout_path, "ab") as handle:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            **popen_kwargs)

        def signal_all(name: str) -> None:
            if use_group:
                try:
                    _os.killpg(process.pid, getattr(_signal, "SIG" + name))
                    escalation.append(name.lower() + "-group")
                    return
                except OSError as exc:
                    escalation.append(f"{name.lower()}-group-failed:{exc}")
            try:
                if name == "TERM":
                    process.terminate()
                    escalation.append("terminate")
                else:
                    process.kill()
                    escalation.append("kill")
            except OSError:
                pass

        def await_group_gone(wait_s: float) -> bool:
            """leader 회수 + group 소멸을 bounded 확인 (POSIX).

            leader는 wait()로 회수해 zombie가 group 생존으로 오판되지 않게
            한다. 비-POSIX는 leader 회수만 확인한다."""
            deadline = time.monotonic() + wait_s
            leader_reaped = False
            while time.monotonic() < deadline:
                if not leader_reaped:
                    try:
                        process.wait(timeout=0.2)
                        leader_reaped = True
                    except subprocess.TimeoutExpired:
                        pass
                if leader_reaped:
                    if not use_group:
                        return True
                    if not _process_group_alive(process.pid):
                        return True
                time.sleep(0.2)
            if not leader_reaped:
                return False
            return (not use_group) or (not _process_group_alive(process.pid))

        def escalate_and_verify() -> None:
            nonlocal group_cleared
            signal_all("TERM")
            if await_group_gone(term_wait_s):
                group_cleared = True if use_group else None
                return
            escalation.append("group-survived-term")
            signal_all("KILL")
            if await_group_gone(kill_wait_s):
                group_cleared = True if use_group else None
                return
            escalation.append("group-survived-kill")
            group_cleared = False if use_group else None

        try:
            process.wait(timeout=timeout_s)
            # 정상 완주 — coordinator가 launcher를 회수하므로 group은 곧
            # 사라져야 한다. 자연 소멸을 짧게 기다리고, 남으면 anomaly로
            # 기록한 뒤 강제 정리·재확인한다 (W5 보완 1·3차).
            if use_group:
                deadline = time.monotonic() + natural_group_wait_s
                while (time.monotonic() < deadline
                       and _process_group_alive(process.pid)):
                    time.sleep(0.2)
                if _process_group_alive(process.pid):
                    cleanup_anomaly = True
                    escalation.append("post-exit-group-residue")
                    escalate_and_verify()
                else:
                    group_cleared = True
        except subprocess.TimeoutExpired:
            timed_out = True
            escalate_and_verify()
        except KeyboardInterrupt:
            interrupted = True
            escalate_and_verify()
            raise
    return {"rc": process.poll(), "timed_out": timed_out,
            "interrupted": interrupted, "escalation": escalation,
            "group_cleared": group_cleared,
            "cleanup_anomaly": cleanup_anomaly}


# ------------------------------------------------------------ frozen audit

def local_frozen_hash_audit(repo_root: Path, binding: dict,
                            *, deploy_verify_pass: bool) -> dict:
    """git 없는 frozen 감사 (README §7) — binding 기록과 정규화(LF) 대조.

    산출 스키마는 final builder `--verification-root` 실검사 계약을 따른다:
    all_unchanged·files·head_commit(40-hex)·worktree_dirty. head는 binding
    source_head, worktree_dirty는 배포 tree manifest 전수 검증 결과의 부정.
    """
    files = []
    all_unchanged = True
    for relative, expected in sorted(binding["frozen_files"].items()):
        path = repo_root / "VDETER" / relative
        try:
            data = path.read_bytes()
            actual = hashlib.sha256(
                data.replace(b"\r\n", b"\n")).hexdigest()
        except OSError:
            actual = None
        unchanged = actual == expected
        all_unchanged = all_unchanged and unchanged
        files.append({"path": relative, "expected_sha256": expected,
                      "actual_sha256": actual, "unchanged": unchanged})
    return {
        "audit_type": "STAGE7_STANDALONE_FROZEN_AUDIT_V1",
        "base_ref": "standalone-binding",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "head_commit": binding["source_head"],
        "worktree_dirty": not deploy_verify_pass,
        "all_unchanged": all_unchanged,
        "files": files,
    }


def write_verification_bundle(repo_root: Path, out_dir: Path, binding: dict,
                              *, deploy_verify_pass: bool,
                              runner=subprocess.run,
                              python_executable: str | None = None) -> dict:
    """frozen audit + unit/contract + integration(stage7) 로그 생성.

    반환: {"frozen_ok", "software_ok", "root"} — 둘 다 true여야 final
    builder가 READY를 낼 수 있다 (builder가 재검사하므로 여기 값은 요약).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    python_executable = python_executable or sys.executable

    audit = local_frozen_hash_audit(
        repo_root, binding, deploy_verify_pass=deploy_verify_pass)
    write_json_atomic(out_dir / "frozen-hash-audit.json", audit)

    def run_suite(log_name: str, targets, timeout_s: float, extra=()):
        log_path = out_dir / log_name
        argv = [python_executable, "-m", "pytest", "-q",
                *[str(repo_root / t) for t in targets], *extra]
        if runner is not subprocess.run:
            # 시험 주입 경로 — timeout 계약이 전달되는지 검증 가능하게.
            with open(log_path, "wb") as handle:
                done = runner(argv, cwd=str(repo_root), stdout=handle,
                              stderr=subprocess.STDOUT,
                              env=build_child_env(),
                              stdin=subprocess.DEVNULL,
                              timeout=timeout_s)
            return done.returncode == 0
        # 기본 경로 — coordinator와 같은 bounded group 정리 (W5 보완 1).
        result = run_child_bounded(
            argv, cwd=repo_root, env=build_child_env(),
            stdout_path=log_path, timeout_s=timeout_s)
        return result["rc"] == 0 and not result["timed_out"]

    # governance/architecture 감사는 명시 node-id로만 분리한다(광범위 무시 금지).
    # 제외 목록·사유를 evidence로 남긴다 — host V1/CI에서는 계속 실행됨.
    deselect_args = ()
    for nodeid, _reason in GOVERNANCE_DESELECT:
        deselect_args += ("--deselect", nodeid)
    write_json_atomic(out_dir / "governance-deselected.json", {
        "artifact_type": "STAGE7_STANDALONE_GOVERNANCE_DESELECT_V1",
        "note": ("배포 노드 자격 범위 밖의 저장소·문서·CI 거버넌스 감사. "
                 "명시적 node-id --deselect이며 광범위 --ignore·실패 무시가 "
                 "아니다. host V1/CI에서는 계속 실행된다."),
        "target_suite": "unit-contract",
        "deselected": [{"nodeid": n, "reason": r}
                       for n, r in GOVERNANCE_DESELECT],
    })
    unit_ok = run_suite("unit-contract.log", UNIT_CONTRACT_TARGETS,
                        UNIT_CONTRACT_TIMEOUT_S, extra=deselect_args)
    integration_ok = run_suite("integration-stage7.log", INTEGRATION_TARGETS,
                               INTEGRATION_TIMEOUT_S, extra=("-k", "stage7"))
    return {"frozen_ok": audit["all_unchanged"] and not audit["worktree_dirty"],
            "software_ok": unit_ok and integration_ok,
            "unit_contract_ok": unit_ok,
            "integration_ok": integration_ok,
            "root": out_dir}


# ------------------------------------------------------------ reference

def issue_reference(*, ladder_id: str, binding: dict, deploy_sha256: str,
                    deploy_files: dict, tc_results: dict,
                    endpoints_observed: dict, platform_profile: str,
                    identity_fingerprint: str,
                    gain: dict | None,
                    environment_manifest: dict,
                    environment_manifest_sha256: str,
                    phase_run_ids: dict,
                    final_status: str | None = None,
                    final_result_sha256: str | None = None,
                    qualification_method: str = "full_ladder",
                    binding_domains: dict | None = None,
                    evidence_reuse: dict | None = None) -> dict:
    """로컬 reference 조립 — 기존 스키마(STAGE7_QUALIFICATION_REFERENCE v1)
    + standalone critical subset (README §8).

    `qualified_source_commit`은 binding source_head (승인 조건 3과 동일 head
    identity), critical manifest strict_files는 배포 manifest 파일 map 전체.

    `qualification_method`는 "full_ladder"(RF 사다리 완주) 또는 "evidence_reuse"
    (RF 재실행 없이 기존 RF evidence 재사용, 2026-09-05 사용자 승인). 후자는
    `binding_domains`(rf_evidence/operation/qualification_tool 3영역 manifest)와
    `evidence_reuse`(이전 ladder·재사용 사유·가드 결과)를 기록한다. RF/operation
    핵심 파일이 다르면 발급 자체가 이뤄지지 않는다(호출부가 거부)."""
    versions = {name: environment_manifest.get(f"{name}_version")
                for name in local_config.CRITICAL_LIBRARIES}
    reference = {
        "artifact_type": "STAGE7_QUALIFICATION_REFERENCE",
        "schema_version": 1,
        "issued_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "issuer": "vdeter qualify (standalone, Phase I)",
        "qualification_method": qualification_method,
        "qualified_source_commit": binding["source_head"],
        "qualification": {
            "run_id": ladder_id,
            "profile": "RESEARCH_READY_MINIMAL",
            "tc_results": {tc: {"status": status}
                           for tc, status in sorted(tc_results.items())},
            "phase_run_ids": phase_run_ids,
            "final_status": final_status,
            "final_result_sha256": final_result_sha256,
        },
        "qualification_critical_manifest": {
            "strict_files": dict(sorted(deploy_files.items())),
        },
        "standalone_critical": {
            "deploy_manifest_sha256": deploy_sha256,
            "platform_profile": platform_profile,
            "endpoints": endpoints_observed,
            "identity_fingerprint": identity_fingerprint,
            "library_versions": versions,
            "environment_manifest_sha256": environment_manifest_sha256,
            "baseline_version": binding.get("baseline_version"),
            "baseline_sha256": binding["baseline_sha256"],
            "gain": gain,
        },
        "limitations": dict(QUALIFICATION_LIMITATIONS),
        "scope": ("이 reference는 이 기계·이 배포본·이 identity·이 환경의 "
                  "표준 실행 자격만 담보한다. baseline 문서 실물 대조는 "
                  "audit 환경에서만 수행한다 (결정 8)."),
    }
    if binding_domains is not None:
        # 요구사항 4: 이전 RF evidence manifest / 현재 operation manifest /
        # 현재 qualification-tool manifest를 각각 기록.
        reference["binding_domains"] = binding_domains
    if evidence_reuse is not None:
        # 요구사항 5: qualification_method=evidence_reuse + 재사용 사유·가드.
        reference["evidence_reuse"] = evidence_reuse
    return reference


def write_reference(directory: Path, reference: dict) -> dict:
    """reference + sidecar 기록 후 기존 검증기로 재검증 (발급 직후 강제)."""
    directory.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(reference, ensure_ascii=True, sort_keys=True,
                         indent=1).encode("utf-8")
    sha = hashlib.sha256(encoded).hexdigest()
    path = directory / REFERENCE_BASENAME
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        import os as _os
        _os.fsync(handle.fileno())
    tmp.replace(path)
    sidecar = directory / (REFERENCE_BASENAME + ".sha256")
    with open(sidecar, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(sha + "\n")
        handle.flush()
        import os as _os
        _os.fsync(handle.fileno())
    from apps.standalone.state import fsync_dir
    fsync_dir(directory)

    from apps import stage7_profile
    validated = stage7_profile.load_reference(str(path), expected_sha256=sha)
    return {"path": path, "sha256": sha,
            "validated_run_id": validated["qualification"]["run_id"]}


def load_active_pointer(paths: StatePaths):
    """active 포인터 적재·검증 (스키마·SHA 형식). 실패는 None."""
    try:
        pointer = json.loads(paths.active_pointer.read_bytes())
    except (OSError, ValueError):
        return None
    if pointer.get("artifact_type") != "STAGE7_STANDALONE_ACTIVE_POINTER_V1":
        return None
    release = pointer.get("release")
    if (not isinstance(release, str) or not release
            or "/" in release or "\\" in release or ".." in release):
        return None
    sha = pointer.get("reference_sha256")
    if not (isinstance(sha, str) and len(sha) == 64
            and all(c in "0123456789abcdef" for c in sha)):
        return None
    return pointer


def active_release_dir(paths: StatePaths):
    """active 포인터가 가리키는 release 디렉터리 (없으면 None)."""
    pointer = load_active_pointer(paths)
    if pointer is None:
        return None
    directory = paths.qualification_releases / pointer["release"]
    return directory if directory.is_dir() else None


def load_active_reference(paths: StatePaths):
    """active reference 적재 (없거나 검증 실패면 None — UNQUALIFIED).

    W5 보완(2차): 포인터에 결속된 `reference_sha256`을 `expected_sha256`으로
    강제한다 — release 안에서 reference와 sidecar를 **함께** 바꿔치기해도
    포인터 SHA와 달라 UNQUALIFIED가 된다. 포인터는 원자 교체 대상이라
    변조하려면 승격 절차를 다시 통과해야 한다.
    """
    pointer = load_active_pointer(paths)
    if pointer is None:
        return None
    directory = paths.qualification_releases / pointer["release"]
    path = directory / REFERENCE_BASENAME
    if not path.is_file():
        return None
    from apps import stage7_profile
    try:
        return stage7_profile.load_reference(
            str(path), expected_sha256=pointer["reference_sha256"])
    except stage7_profile.ProfileError:
        return None


def promote_candidate(paths: StatePaths, *, release_id: str) -> dict:
    """candidate → releases/<id> 이동 후 **포인터만 원자 교체** (W5 보완 2).

    순서가 안전을 만든다: ① candidate를 변경 불가 release 디렉터리로 rename
    (이 사이 장애 → 포인터는 여전히 이전 release를 가리켜 active 유지)
    ② `active.json` 포인터를 tmp→os.replace 원자 교체 (이 지점 이후 장애 →
    새 release가 active). 어느 순간에도 "active 없음" 창이 없다.
    이전 release 디렉터리는 그대로 남아 불변 이력(archive) 역할을 한다.
    """
    reference_path = paths.qualification_candidate / REFERENCE_BASENAME
    if not reference_path.is_file():
        raise QualifyError("승격할 candidate reference가 없습니다")
    reference_sha = (paths.qualification_candidate
                     / (REFERENCE_BASENAME + ".sha256")).read_text(
                         encoding="utf-8").split()[0]
    target = paths.qualification_releases / release_id
    if target.exists():
        raise QualifyError(f"release가 이미 존재합니다: {target}")
    previous = None
    try:
        previous = json.loads(paths.active_pointer.read_bytes()).get("release")
    except (OSError, ValueError):
        pass
    paths.qualification_candidate.rename(target)
    # rename을 내구화한 뒤에야 포인터를 바꾼다 — 장애 시 "포인터는 새
    # release를 가리키는데 rename은 소실"이라는 역전이 없게 (W5 보완 2차).
    from apps.standalone.state import fsync_dir
    fsync_dir(paths.qualification_releases)
    fsync_dir(paths.qualification_dir)
    paths.qualification_candidate.mkdir()
    write_json_atomic(paths.active_pointer, {
        "artifact_type": "STAGE7_STANDALONE_ACTIVE_POINTER_V1",
        "release": release_id,
        "reference_sha256": reference_sha,
        "promoted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "previous_release": previous,
    })
    return {"release": release_id, "previous_release": previous,
            "reference_sha256": reference_sha}


# ------------------------------------------------------------ phase 실행

def default_phase_runner(context: dict) -> dict:
    """coordinator phase 하나를 로컬 자식으로 완주시킨다 (기본 구현).

    독립 process group으로 실행하고, timeout·중단 시 group 전체를
    TERM→KILL bounded 정리한다 (W5 보완 1). 반환은 run_child_bounded 계약.
    """
    repo_root = Path(context["repo_root"])
    argv = [
        sys.executable,
        str(repo_root / "VDETER" / "apps" / "stage7_coordinator_main.py"),
        "--run-id", context["run_id"],
        "--phase", context["phase"],
        "--run-root", str(context["run_root"]),
        "--configs-dir", str(context["configs_dir"]),
        "--baseline-sha256", context["baseline_sha256"],
    ]
    if context.get("gain_artifact"):
        argv += ["--gain-artifact", str(context["gain_artifact"])]
    return run_child_bounded(
        argv, cwd=repo_root / "VDETER",
        env=build_child_env(identity_root=context.get("identity_root")),
        stdout_path=Path(context["run_root"]) / "coordinator-console.log",
        timeout_s=context["timeout_s"])


def verify_phase_evidence(run_root: Path, phase: str) -> dict:
    """phase run이 자기 TC를 산출·PASS했는지 확인 (사다리 단계 판정)."""
    directory = run_root / "evidence" / "research_ready"
    statuses = {}
    errors = []
    for tc in PHASE_EXPECTED_TCS[phase]:
        path = directory / f"{tc}.json"
        try:
            payload = json.loads(path.read_bytes())
            statuses[tc] = payload.get("status")
        except (OSError, ValueError):
            statuses[tc] = "MISSING"
        if statuses[tc] != "PASS":
            errors.append(f"{tc}: {statuses[tc]}")
    return {"pass": not errors, "tc_status": statuses, "errors": errors}


# ------------------------------------------------------------ 본체

def run_qualify(paths: StatePaths, *, hardware_path: Path | str | None = None,
                repo_root: Path | str | None = None,
                binding_path: Path | str | None = None,
                phase_runner=default_phase_runner,
                suite_runner=subprocess.run,
                preflight_fn=None,
                residual_probe=None,
                environment_manifest: dict | None = None,
                final_builder_module=None,
                stamp: str | None = None) -> dict:
    """자격 사다리 전체. 반환 report — `status`가 QUALIFIED여야 승격됨.

    실패는 QualifyError 또는 report["status"]="FAIL"로 정직 보고하며, 그
    경우 active reference는 이전 상태 그대로다. `repo_root`/`binding_path`는
    기본적으로 설치 root에서 파생된다 (명시 인자는 시험 격리용).
    """
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    report = {"status": "FAIL", "phases": {}, "errors": []}

    lock = SingleInstanceLock(paths.lock_path)
    lock.acquire(diagnostics={"command": "qualify"})
    try:
        paths.ensure_var()
        binding = local_config.load_standalone_binding(
            binding_path if binding_path is not None else
            (repo_root / "VDETER" / "configs" / "stage7"
             / "standalone-binding.json"))
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))

        # --- 배포 무결 (setup manifest 기준) -----------------------------
        manifest, manifest_sha = deploy_manifest.load_manifest(
            paths.var_dir / "deploy-manifest.json")
        if manifest is None:
            raise QualifyError(
                "deploy manifest가 없습니다 — 먼저 `vdeter setup`을 "
                "실행하십시오")
        deploy_check = deploy_manifest.verify(repo_root, manifest)
        report["deploy_verify"] = {k: v for k, v in deploy_check.items()
                                   if k != "checked"}
        if not deploy_check["pass"]:
            raise QualifyError(
                "배포 tree가 setup 시점 manifest와 다릅니다 "
                f"(missing={deploy_check['missing'][:3]}, "
                f"mismatch={deploy_check['mismatch'][:3]}, "
                f"untracked={deploy_check['untracked'][:3]}) — 변경을 "
                "되돌리거나 `vdeter setup`으로 manifest를 재발급하십시오")

        # --- identity 정합 (W5 필수 — manifest·cert·store·파일 일치) ------
        identity_check = identity.check_identity_consistency(paths)
        report["identity"] = identity_check
        if not identity_check["pass"]:
            raise QualifyError(
                "identity 자료가 서로 일치하지 않습니다: "
                + "; ".join(identity_check["errors"]))

        # --- 필수 preflight ---------------------------------------------
        if preflight_fn is None:
            from apps.standalone.local_preflight import run_required_preflight

            def preflight_fn():
                return run_required_preflight(hardware=hardware, paths=paths)
        preflight = preflight_fn()
        report["preflight"] = preflight
        if not preflight["pass"]:
            raise QualifyError(
                "필수 preflight 실패: " + "; ".join(preflight["failures"]))
        endpoints_observed = {
            role: {"serial": info["serial"],
                   "speed_class": info["speed_class"]}
            for role, info in
            preflight["checks"]["b210"]["observed_by_role"].items()
            if info is not None
        }

        # candidate는 새 사다리마다 비운 뒤 시작한다 (승격 전 scratch).
        for entry in paths.qualification_candidate.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

        stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        ladder_id = f"qual-{stamp}"
        environment_manifest = (environment_manifest
                                or local_config.build_environment_manifest())

        # --- 필수 라이브러리 4종 실재 게이트 (W5 보완 3) ------------------
        # 부재(None/공백)를 자격값으로 기록하는 경로를 P0 시작 전에 차단한다.
        missing_libraries = [
            name for name in local_config.CRITICAL_LIBRARIES
            if not (isinstance(environment_manifest.get(f"{name}_version"),
                               str)
                    and environment_manifest[f"{name}_version"].strip())
        ]
        if missing_libraries:
            raise QualifyError(
                f"필수 라이브러리 버전 확인 불가: {missing_libraries} — "
                "`vdeter setup`으로 설치를 완료한 뒤 재시도하십시오 "
                "(부재 상태로는 자격을 발급하지 않습니다)")

        if residual_probe is None:
            from apps.standalone.local_preflight import residual_processes
            residual_probe = residual_processes

        gain_artifact_path = None
        gain_record = None
        candidate_info = None
        tc_results = {}
        phase_run_ids = {}
        environment_manifest_sha256 = None

        for phase in LADDER_PHASES:
            run_id = f"{ladder_id}-{phase}"
            run_id, run_root = allocate_run_dir(paths.runs_dir, run_id)
            phase_run_ids[phase] = run_id
            gain_input = None
            # run_mode는 PHASE_RUN_MODE로 명시 고정한다 — template 기본값에
            # 기대지 않는다 (1차 실패 원인). rr-p1은 gain 입력 없이
            # GAIN_CALIBRATION으로 새 gain artifact를 산출한다.
            run_mode = PHASE_RUN_MODE[phase]
            if phase in GAIN_CONSUMING_PHASES:
                if gain_artifact_path is None:
                    raise QualifyError(
                        f"{phase}: gain artifact가 없습니다 — rr-p1이 "
                        "산출하지 못했습니다")
                gain_input = json.loads(
                    Path(gain_artifact_path).read_bytes())
                # loader 계약: gain artifact 결속은 OPERATIONAL run에서만
                # 허용된다 (RF-off/gain-calibration run 결속은 거부).
                assert run_mode == "OPERATIONAL", phase
            generated = local_config.generate_run_configs(
                repo_root=repo_root, run_root=run_root, run_id=run_id,
                rr_phase=phase, hardware=hardware, binding=binding,
                deployment_manifest_relpath=deploy_manifest.MANIFEST_RELPATH,
                deployment_manifest_sha256=manifest_sha,
                deploy_verified=True, run_mode=run_mode,
                gain_input=gain_input,
                environment_manifest=environment_manifest)
            environment_manifest_sha256 = generated[
                "environment_manifest_sha256"]

            timeout_s = (local_config.PHASE_DEADLINES_S[phase]
                         + PHASE_TIMEOUT_MARGIN_S)
            outcome = phase_runner({
                "repo_root": repo_root, "run_id": run_id,
                "run_root": run_root,
                "configs_dir": generated["configs_dir"],
                "phase": phase,
                "baseline_sha256": binding["baseline_sha256"],
                "gain_artifact": gain_artifact_path,
                "identity_root": paths.identity_dir,
                "timeout_s": timeout_s,
            })
            if not isinstance(outcome, dict):  # 주입 실행기의 rc-int 호환
                outcome = {"rc": outcome, "timed_out": False,
                           "escalation": [], "group_cleared": None,
                           "cleanup_anomaly": False}
            # phase 후 잔존 검사 — 정리가 확인되지 않으면 자격 실패
            # (W5 보완 1: timeout/중단 정리 포함 어떤 경로든 실측 확인).
            residual = residual_probe()
            evidence = verify_phase_evidence(run_root, phase)
            report["phases"][phase] = {
                "run_id": run_id, "rc": outcome["rc"],
                "timed_out": outcome.get("timed_out", False),
                "escalation": outcome.get("escalation", []),
                "residual_processes": residual,
                **evidence,
            }
            if outcome.get("timed_out"):
                report["errors"].append(
                    f"{phase} timeout — group 정리 수행"
                    f"({outcome.get('escalation')}) — candidate 미승격, "
                    "기존 active reference 유지")
                return report
            if outcome.get("group_cleared") is False:
                report["errors"].append(
                    f"{phase} process group 소멸 미확인 "
                    f"({outcome.get('escalation')}) — 자격 실패, "
                    "기존 active reference 유지")
                return report
            if outcome.get("cleanup_anomaly"):
                # 정상 종료 뒤 잔류 group을 강제 정리했다 — 정리는 됐어도
                # coordinator의 자체 회수가 실패한 run이므로 자격 불가.
                report["errors"].append(
                    f"{phase} 정상 종료 후 잔류 group 강제 정리"
                    f"(cleanup_anomaly, {outcome.get('escalation')}) — "
                    "자격 실패, 기존 active reference 유지")
                return report
            if residual is None or residual:
                report["errors"].append(
                    f"{phase} 후 잔존 프로세스 정리 미확인 "
                    f"({residual!r}) — 자격 실패, 기존 active reference 유지")
                return report
            if outcome["rc"] != 0 or not evidence["pass"]:
                report["errors"].append(
                    f"{phase} 실패 (rc={outcome['rc']}, "
                    f"{evidence['errors']}) — "
                    "candidate 미승격, 기존 active reference 유지")
                return report
            tc_results.update(evidence["tc_status"])

            if phase == "rr-p1":
                gain_artifact_path = (run_root / "evidence"
                                      / "gain_artifact_input.json")
                artifact_file = run_root / "evidence" / "gain_artifact.json"
                if not (gain_artifact_path.is_file()
                        and artifact_file.is_file()):
                    report["errors"].append(
                        "rr-p1 PASS인데 gain artifact 산출물이 없습니다")
                    return report
                gain_record = {
                    "artifact_sha256": _sha256_file(artifact_file),
                    "artifact_input_sha256": _sha256_file(gain_artifact_path),
                    "endpoint_common_rx_gain_db": json.loads(
                        gain_artifact_path.read_bytes())
                    ["endpoint_common_rx_gain_db"],
                    "source_run_id": run_id,
                }
                # --- 3단계: candidate reference 생성 (사다리 진행용) ------
                candidate = issue_reference(
                    ladder_id=ladder_id, binding=binding,
                    deploy_sha256=manifest_sha,
                    deploy_files={e["path"]: e["sha256"]
                                  for e in manifest["files"]},
                    tc_results=dict(tc_results),
                    endpoints_observed=endpoints_observed,
                    platform_profile=hardware["platform_profile"],
                    identity_fingerprint=identity_check["fingerprint"],
                    gain=gain_record,
                    environment_manifest=environment_manifest,
                    environment_manifest_sha256=environment_manifest_sha256,
                    phase_run_ids=dict(phase_run_ids))
                candidate_info = write_reference(
                    paths.qualification_candidate, candidate)
                report["candidate"] = {
                    "sha256": candidate_info["sha256"],
                    "path": str(candidate_info["path"]),
                }

        # --- 7단계: verification bundle + final builder -------------------
        verification_root = paths.qualification_candidate / "verification"
        bundle = write_verification_bundle(
            repo_root, verification_root, binding,
            deploy_verify_pass=True, runner=suite_runner)
        report["verification"] = {k: v for k, v in bundle.items()
                                  if k != "root"}
        if final_builder_module is None:
            final_builder_module = _load_module(
                repo_root / "VDETER" / "tools" / "stage7_rr_final_builder.py",
                "stage7_rr_final_builder_standalone")
        evidence_roots = [
            str(paths.runs_dir / phase_run_ids[phase]
                / "evidence" / "research_ready")
            for phase in LADDER_PHASES
        ]
        final_out = paths.qualification_candidate / "final-result.json"
        final_result = final_builder_module.build(
            evidence_roots, str(final_out),
            software_ok=bundle["software_ok"],
            frozen_hash_ok=bundle["frozen_ok"],
            verification_root=str(verification_root),
            head_provider=lambda: binding["source_head"])
        report["final_status"] = final_result["final_status"]
        report["consistency_problems"] = final_result["consistency_problems"]
        if final_result["final_status"] != "RESEARCH_READY":
            report["errors"].append(
                f"final builder: {final_result['final_status']} — "
                "candidate 미승격, 기존 active reference 유지")
            return report

        # --- 8단계: 최종 reference 갱신 + 원자적 승격 ---------------------
        final_reference = issue_reference(
            ladder_id=ladder_id, binding=binding,
            deploy_sha256=manifest_sha,
            deploy_files={e["path"]: e["sha256"]
                          for e in manifest["files"]},
            tc_results=dict(tc_results),
            endpoints_observed=endpoints_observed,
            platform_profile=hardware["platform_profile"],
            identity_fingerprint=identity_check["fingerprint"],
            gain=gain_record,
            environment_manifest=environment_manifest,
            environment_manifest_sha256=environment_manifest_sha256,
            phase_run_ids=dict(phase_run_ids),
            final_status=final_result["final_status"],
            final_result_sha256=_sha256_file(final_out))
        final_info = write_reference(
            paths.qualification_candidate, final_reference)
        promotion = promote_candidate(paths, release_id=ladder_id)
        report["reference"] = {
            "sha256": final_info["sha256"],
            "release": promotion["release"],
            "path": str(paths.qualification_releases / ladder_id
                        / REFERENCE_BASENAME),
            "previous_release": promotion["previous_release"],
        }
        report["status"] = "QUALIFIED"
        return report
    finally:
        lock.release()


def _domain_manifest_record(manifest: dict, domain: str, *, source: str) -> dict:
    """reference에 담을 한 영역의 manifest 요약(집계 해시·파일 수·출처)."""
    split = domains.split_manifest(manifest)[domain]
    return {"aggregate_sha256": split["aggregate_sha256"],
            "file_count": split["file_count"], "source": source}


def _rf_resolved_config_bound_to(prior_ladder_id: str, paths: StatePaths,
                                 expected_manifest_sha256: str) -> dict:
    """RF phase resolved config가 이전 배포 manifest에 결속됐는지 확인.

    각 phase endpoints/*.toml의 `deployment_manifest_sha256`이 전부
    `expected_manifest_sha256`과 같아야 한다(그 manifest에서 RF가 돌았다는
    증거). 하나라도 다르거나 없으면 mismatch로 보고한다. 파일 내용은 바꾸지
    않는다(읽기만)."""
    import re
    checked, mismatch = [], []
    for phase in LADDER_PHASES:
        cfg_dir = (paths.runs_dir / f"{prior_ladder_id}-{phase}"
                   / "resolved-configs" / "endpoints")
        if not cfg_dir.is_dir():
            mismatch.append(f"{phase}: resolved-configs 없음")
            continue
        for toml_path in sorted(cfg_dir.glob("*.toml")):
            text = toml_path.read_text(encoding="utf-8")
            m = re.search(r'deployment_manifest_sha256\s*=\s*"([0-9a-f]{64})"',
                          text)
            checked.append(str(toml_path.relative_to(paths.runs_dir)))
            if not m:
                mismatch.append(f"{toml_path.name}: 결속 필드 없음")
            elif m.group(1) != expected_manifest_sha256:
                mismatch.append(f"{toml_path.name}: {m.group(1)[:12]} != "
                                f"{expected_manifest_sha256[:12]}")
    return {"pass": not mismatch and bool(checked),
            "checked": checked, "mismatch": mismatch}


REUSE_BASELINE_TYPE = "STAGE7_STANDALONE_REUSE_BASELINE_V1"


def _read_rf_critical_symbol_baseline(repo_root: Path | str) -> dict:
    """현재 배포 트리에서 RF-critical 심볼 소스를 읽어 함수 단위 해시 baseline."""
    sources = {}
    for relpath in domains.RF_CRITICAL_QUALIFY_SYMBOLS:
        sources[relpath] = (Path(repo_root) / relpath).read_text(
            encoding="utf-8")
    return domains.rf_critical_symbol_baseline(sources)


def capture_reuse_baseline(repo_root: Path | str,
                           manifest_path: Path | str) -> dict:
    """재사용 baseline: 이 시점 배포 manifest + RF-critical 심볼 해시.

    **수정 배포 전에** 호출해 이전(RF 실행) 트리 상태를 고정한다. RF evidence를
    재사용하려면 배포 후 이 baseline과 대조한다."""
    manifest, manifest_sha = deploy_manifest.load_manifest(manifest_path)
    if manifest is None:
        raise QualifyError(f"deploy manifest 없음: {manifest_path}")
    return {
        "artifact_type": REUSE_BASELINE_TYPE,
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "deploy_manifest": manifest,
        "deploy_manifest_sha256": manifest_sha,
        "rf_critical_symbols": _read_rf_critical_symbol_baseline(repo_root),
    }


def write_reuse_baseline(repo_root: Path | str, manifest_path: Path | str,
                         out_path: Path | str) -> dict:
    baseline = capture_reuse_baseline(repo_root, manifest_path)
    Path(out_path).write_text(
        json.dumps(baseline, ensure_ascii=True, sort_keys=True, indent=1),
        encoding="utf-8")
    return baseline


def load_reuse_baseline(path: Path | str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("artifact_type") != REUSE_BASELINE_TYPE:
        raise QualifyError(f"reuse baseline 형식이 아닙니다: {path}")
    return data


def run_qualify_reuse(paths: StatePaths, *, prior_ladder_id: str,
                      prior_baseline_path: Path | str, reason: str,
                      hardware_path: Path | str | None = None,
                      repo_root: Path | str | None = None,
                      binding_path: Path | str | None = None,
                      suite_runner=subprocess.run,
                      preflight_fn=None, residual_probe=None,
                      environment_manifest: dict | None = None,
                      final_builder_module=None) -> dict:
    """RF 사다리 재실행 없이 기존 RF evidence를 재사용해 승격 (2026-09-05 승인,
    보완 반영).

    가드: ① rf 영역 파일 byte-identical + RF-critical 심볼(run mode·phase 순서·
    phase 인자 등) 함수 단위 byte-identical, ② operation 영역 byte-identical(현
    정책: 회귀 기반 재사용 불허), ③ 변경된 tooling(qualify_cmd governance/bundle,
    VDETER/tools, qualification 시험)에 수정된 verification bundle 재실행, ④
    reference에 rf_evidence/operation/qualification_tool manifest + RF 심볼 각각
    기록, ⑤ qualification_method=evidence_reuse + 사유, ⑥ rf/operation 핵심
    파일·RF 심볼 하나라도 다르면 거부, ⑦ 문서 변경만으로는 무효화 안 됨, ⑧ 해시
    임의 패치·기존 결과 변경 금지(읽기만).

    RF 사다리 phase는 실행하지 않는다 — 이전 phase evidence(PASS)만 재사용한다.
    """
    repo_root = Path(repo_root) if repo_root is not None else paths.root
    report = {"status": "FAIL", "qualification_method": "evidence_reuse",
              "prior_ladder_id": prior_ladder_id, "errors": []}

    lock = SingleInstanceLock(paths.lock_path)
    lock.acquire(diagnostics={"command": "qualify-reuse"})
    try:
        paths.ensure_var()
        binding = local_config.load_standalone_binding(
            binding_path if binding_path is not None else
            (repo_root / "VDETER" / "configs" / "stage7"
             / "standalone-binding.json"))
        hardware = local_config.parse_hardware_declaration(
            hardware_path or (repo_root / "hardware.toml"))

        current_manifest, current_manifest_sha = deploy_manifest.load_manifest(
            paths.var_dir / "deploy-manifest.json")
        if current_manifest is None:
            raise QualifyError("현재 deploy manifest가 없습니다 — `vdeter setup` 필요")
        deploy_check = deploy_manifest.verify(repo_root, current_manifest)
        report["deploy_verify"] = {k: v for k, v in deploy_check.items()
                                   if k != "checked"}
        if not deploy_check["pass"]:
            raise QualifyError(
                "현재 배포 tree가 manifest와 다릅니다 "
                f"(missing={deploy_check['missing'][:3]}, "
                f"mismatch={deploy_check['mismatch'][:3]}, "
                f"untracked={deploy_check['untracked'][:3]})")

        baseline = load_reuse_baseline(prior_baseline_path)
        prior_manifest = baseline["deploy_manifest"]
        prior_manifest_sha = baseline["deploy_manifest_sha256"]
        prior_rf_symbols = baseline["rf_critical_symbols"]
        current_rf_symbols = _read_rf_critical_symbol_baseline(repo_root)
        report["rf_evidence_manifest_sha256"] = prior_manifest_sha
        report["current_deploy_manifest_sha256"] = current_manifest_sha

        identity_check = identity.check_identity_consistency(paths)
        report["identity"] = identity_check
        if not identity_check["pass"]:
            raise QualifyError("identity 불일치: "
                               + "; ".join(identity_check["errors"]))

        if preflight_fn is None:
            from apps.standalone.local_preflight import run_required_preflight

            def preflight_fn():
                return run_required_preflight(hardware=hardware, paths=paths)
        preflight = preflight_fn()
        report["preflight"] = preflight
        if not preflight["pass"]:
            raise QualifyError("필수 preflight 실패: "
                               + "; ".join(preflight["failures"]))
        endpoints_observed = {
            role: {"serial": obs["serial"], "usb_path": obs["usb_path"],
                   "speed_mbps": obs["speed_mbps"]}
            for role, obs in
            preflight["checks"]["b210"]["observed_by_role"].items()}

        environment_manifest = (environment_manifest
                                or local_config.build_environment_manifest())

        # --- 이전 RF phase evidence 재구성·검증 (재실행 없음) --------------
        phase_run_ids = {phase: f"{prior_ladder_id}-{phase}"
                         for phase in LADDER_PHASES}
        tc_results, gain_record = {}, None
        for phase in LADDER_PHASES:
            run_root = paths.runs_dir / phase_run_ids[phase]
            evidence = verify_phase_evidence(run_root, phase)
            report.setdefault("phases", {})[phase] = evidence
            if not evidence["pass"]:
                raise QualifyError(
                    f"{phase} 이전 evidence 미검증({evidence['errors']}) — 재사용 불가")
            tc_results.update(evidence["tc_status"])
            if phase == "rr-p1":
                gi = run_root / "evidence" / "gain_artifact_input.json"
                gf = run_root / "evidence" / "gain_artifact.json"
                if not (gi.is_file() and gf.is_file()):
                    raise QualifyError("rr-p1 gain artifact 산출물 없음 — 재사용 불가")
                gain_record = {
                    "artifact_sha256": _sha256_file(gf),
                    "artifact_input_sha256": _sha256_file(gi),
                    "endpoint_common_rx_gain_db": json.loads(gi.read_bytes())
                    ["endpoint_common_rx_gain_db"],
                    "source_run_id": phase_run_ids[phase]}

        # 요구사항 1(configs): RF resolved config가 이전 manifest에 결속됐는가.
        cfg_bound = _rf_resolved_config_bound_to(
            prior_ladder_id, paths, prior_manifest_sha)
        report["rf_resolved_config_binding"] = cfg_bound
        if not cfg_bound["pass"]:
            raise QualifyError(
                "RF resolved config가 이전 manifest에 결속되지 않음: "
                + "; ".join(cfg_bound["mismatch"][:3]))

        # --- 요구사항 3: 수정된 verification bundle 재실행 (operation 회귀 겸용) --
        verification_root = paths.qualification_candidate / "verification"
        bundle = write_verification_bundle(
            repo_root, verification_root, binding,
            deploy_verify_pass=True, runner=suite_runner)
        report["verification"] = {k: v for k, v in bundle.items()
                                  if k != "root"}
        if not bundle["frozen_ok"]:
            raise QualifyError("frozen-hash audit 실패 — 재사용 승격 불가")
        if not bundle["software_ok"]:
            raise QualifyError("verification bundle software 검증 실패 — 승격 불가")

        # --- 재사용 판정: rf 파일+RF-critical 심볼 byte-identical,
        #     operation byte-identical (현 정책). 아니면 거부. ---
        reuse = domains.evaluate_reuse(
            prior_manifest, current_manifest,
            prior_rf_symbols=prior_rf_symbols,
            current_rf_symbols=current_rf_symbols)
        report["reuse_evaluation"] = {
            k: reuse[k] for k in
            ("eligible", "rf_identical", "rf_files_identical",
             "rf_symbols_identical", "rf_symbols_changed",
             "operation_identical", "tooling_changed", "test_doc_changed",
             "blocking_files")}
        if not reuse["eligible"]:
            raise QualifyError(
                "evidence 재사용 거부 — rf/operation 핵심 또는 RF 심볼 변경: "
                + "; ".join(reuse["blocking_files"][:5]))

        # --- final builder (이전 RF evidence roots) -----------------------
        # env manifest sha는 generate_run_configs와 동일 직렬화로 계산한다.
        environment_manifest_sha256 = hashlib.sha256(json.dumps(
            environment_manifest, ensure_ascii=True, sort_keys=True,
            indent=1).encode("utf-8")).hexdigest()
        if final_builder_module is None:
            final_builder_module = _load_module(
                repo_root / "VDETER" / "tools" / "stage7_rr_final_builder.py",
                "stage7_rr_final_builder_standalone")
        evidence_roots = [
            str(paths.runs_dir / phase_run_ids[phase]
                / "evidence" / "research_ready") for phase in LADDER_PHASES]
        final_out = paths.qualification_candidate / "final-result.json"
        final_result = final_builder_module.build(
            evidence_roots, str(final_out),
            software_ok=bundle["software_ok"], frozen_hash_ok=bundle["frozen_ok"],
            verification_root=str(verification_root),
            head_provider=lambda: binding["source_head"])
        report["final_status"] = final_result["final_status"]
        if final_result["final_status"] != "RESEARCH_READY":
            raise QualifyError(
                f"final builder: {final_result['final_status']} — 승격 불가")

        # --- 3영역 manifest + RF-critical 심볼 + evidence_reuse 기록 -------
        binding_domains = {
            "rf_evidence": {
                **_domain_manifest_record(prior_manifest, domains.RF,
                                          source=f"prior_ladder:{prior_ladder_id}"),
                "manifest_sha256": prior_manifest_sha,
                "rf_critical_symbols": prior_rf_symbols},
            "operation": _domain_manifest_record(
                current_manifest, domains.OPERATION, source="current_deploy"),
            "qualification_tool": _domain_manifest_record(
                current_manifest, domains.TOOLING, source="current_deploy"),
        }
        evidence_reuse = {
            "reason": reason,
            "prior_ladder_id": prior_ladder_id,
            "rf_evidence_manifest_sha256": prior_manifest_sha,
            "current_deploy_manifest_sha256": current_manifest_sha,
            "rf_identical": reuse["rf_identical"],
            "rf_files_identical": reuse["rf_files_identical"],
            "rf_symbols_identical": reuse["rf_symbols_identical"],
            "operation_identical": reuse["operation_identical"],
            "tooling_changed": reuse["tooling_changed"],
            "test_doc_changed": reuse["test_doc_changed"],
            "rf_resolved_config_binding_pass": cfg_bound["pass"],
            "verification": {"software_ok": bundle["software_ok"],
                             "frozen_ok": bundle["frozen_ok"]},
        }

        reference = issue_reference(
            ladder_id=prior_ladder_id, binding=binding,
            deploy_sha256=current_manifest_sha,
            deploy_files={e["path"]: e["sha256"]
                          for e in current_manifest["files"]},
            tc_results=dict(tc_results),
            endpoints_observed=endpoints_observed,
            platform_profile=hardware["platform_profile"],
            identity_fingerprint=identity_check["fingerprint"],
            gain=gain_record,
            environment_manifest=environment_manifest,
            environment_manifest_sha256=environment_manifest_sha256,
            phase_run_ids=dict(phase_run_ids),
            final_status=final_result["final_status"],
            final_result_sha256=_sha256_file(final_out),
            qualification_method="evidence_reuse",
            binding_domains=binding_domains,
            evidence_reuse=evidence_reuse)
        final_info = write_reference(paths.qualification_candidate, reference)
        promotion = promote_candidate(paths, release_id=prior_ladder_id)
        report["reference"] = {
            "sha256": final_info["sha256"],
            "release": promotion["release"],
            "path": str(paths.qualification_releases / prior_ladder_id
                        / REFERENCE_BASENAME),
            "previous_release": promotion["previous_release"]}
        report["status"] = "QUALIFIED"
        return report
    finally:
        lock.release()
