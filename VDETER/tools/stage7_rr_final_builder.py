"""RESEARCH_READY_MINIMAL final builder (`stage7` §17A.6, v0.9.1 J항).

여러 RR run-root의 evidence/research_ready/*.json을 모아 필수 TC
(RR-001..005 + RR-007; RR-006/008은 v0.9.10/0.9.14 이관 — 기록 전용)가
모두 존재·PASS하고 profile/baseline/lineage/hardware 일관성과 무결 조건을 만족할
때만 `final-result.json`을 `RESEARCH_READY`로 생성한다. 하나라도 누락/FAIL이면
`RESEARCH_NOT_READY`/`HARDWARE_BLOCKED`/`ABORTED`를 생성한다.

VERIFIED/FROZEN이나 기존 TC-STAGE7-001..020 PASS는 표시하지 않는다.
실제 hardware evidence 없이 PASS를 생성하지 않는다.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys

REQUIRED_TCS = (
    "TC-STAGE7-RR-001", "TC-STAGE7-RR-002", "TC-STAGE7-RR-003",
    "TC-STAGE7-RR-004", "TC-STAGE7-RR-005",
    "TC-STAGE7-RR-007",
)
# v0.9.14: soak 전체가 후속 이관 — 판정 필수 soak 없음.
REQUIRED_SOAK = ()
# v0.9.10: Link 19 자격(RR-006, RR-008-link19)은 판정 제외·후속 이관.
# v0.9.14: RR-008-link17(soak)도 이관 — 단발 재현 시연에 지속 반복 요구
# 부재(실측 조사 기록은 전량 보존). artifact가 있으면 기록하되 판정에
# 넣지 않는다.
DEFERRED_TCS = (
    "TC-STAGE7-RR-006", "TC-STAGE7-RR-008-link19", "TC-STAGE7-RR-008-link17",
)
# 각 phase는 실행 시점의 승인 개정으로 결속된다. 아래 사슬 밖 버전이 하나라도
# 있으면 불일치로 본다(사슬 내 진행은 개정 이력이 문서 헤더에 전량 보존됨).
APPROVED_REVISION_CHAIN = (
    "0.9.1", "0.9.2", "0.9.3", "0.9.4", "0.9.5", "0.9.6", "0.9.7",
    "0.9.8", "0.9.9", "0.9.10", "0.9.11", "0.9.12", "0.9.13", "0.9.14",
    "0.9.15", "0.9.16", "0.9.17", "0.9.18", "0.9.19", "0.9.20", "0.9.21",
    "0.9.22",
    # 0.9.23 (2026-08-30, 사용자 승인 — RESEARCH_READY 동결 기록):
    # 승인 당시 builder 재실행이 없어 사슬 연장이 누락됐던 것을
    # 2026-09-02 재자격에서 현행화한다 (문서 헤더 개정 이력 근거).
    "0.9.23",
)
# v0.9.14: 시나리오 계열 TC는 v0.9.8의 기록된 한정 재시도가 정당하다.
SCENARIO_TCS_WITH_SANCTIONED_RETRY = (
    "TC-STAGE7-RR-005", "TC-STAGE7-RR-006",
    "TC-STAGE7-RR-008-link17", "TC-STAGE7-RR-008-link19",
)


def _load(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def _int_or_none(value):
    """counter 값의 방어적 정수화 — 비정상 타입은 crash 대신 None.

    (0.9.22 R2 감사: `retries="abc"` 주입이 traceback으로 죽음 — READY는
    안 나오므로 fail-closed이나, 설계된 "문제 계상 후 NOT_READY" 패턴로
    통일한다.)"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _git_head():
    """builder 자신이 속한 저장소의 현행 HEAD (실패 시 None).

    (0.9.22 사용자 지적: audit head_commit을 40자 형식만 검사하면 어느
    시점의 감사든 통과 — 현행 HEAD와의 일치를 요구해 '코드가 바뀐 뒤의
    낡은 검증 묶음' 결속을 기계 차단한다.)"""
    import subprocess
    try:
        done = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
        return done.stdout.strip() if done.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _root_run_dir(root):
    """evidence root 경로에서 실제 run 디렉터리명을 얻는다.

    규약 경로 `.../<run_id>/evidence/research_ready`의 고정 꼬리를 걷어낸
    마지막 성분. (0.9.22 R2 감사: 구판 `run_id in parts`는 경로에 항상
    있는 "evidence"/"research_ready" 성분과도 일치해 run_id를 그 문자열로
    위조하면 어느 root든 통과 — 보증 0의 검사였다.)"""
    parts = os.path.normpath(root).split(os.sep)
    while parts and parts[-1] in ("evidence", "research_ready"):
        parts.pop()
    return parts[-1] if parts else ""


def build(evidence_roots, out_path, *, software_ok, frozen_hash_ok,
          verification_root=None, head_provider=None):
    """`head_provider`는 현행 HEAD를 돌려주는 호출가능 객체다 (기본 `_git_head`).

    검사 자체는 바뀌지 않는다 — audit의 `head_commit`이 현행 HEAD와 같아야
    하고, 못 얻으면 fail-closed다. 주입 지점을 둔 이유는 시험이 ambient git
    상태에 의존하지 않게 하기 위해서다 (배포본처럼 `.git`이 없는 환경에서도
    "일치하면 통과·불일치하면 거부" 논리를 그대로 검증할 수 있다).
    """

    _head_of = head_provider if head_provider is not None else _git_head
    collected = {}
    problems = []
    for root in evidence_roots:
        for path in glob.glob(os.path.join(root, "*.json")):
            name = os.path.basename(path)[:-5]
            if name in ("index", "final-result"):
                continue
            payload, sha = _load(path)
            # 0.9.22: 같은 TC가 복수 root에 있으면 last-wins로 조용히
            # 덮여 index 대조가 오도된다 — 모호성 자체를 문제로 계상.
            if name in collected:
                problems.append(f"동일 TC가 복수 root에 존재: {name}")
            collected[name] = {"payload": payload, "sha256": sha, "path": path}

    def status_of(tc):
        item = collected.get(tc)
        return item["payload"].get("status") if item else "MISSING"

    # 0.9.20: artifact-SHA/index 상호 대조 — 각 evidence root의 index.json이
    # 가리키는 sha256과 실측 artifact sha256의 불일치를 일관성 문제로 계상.
    for root in evidence_roots:
        index_path = os.path.join(root, "index.json")
        root_artifacts = {
            os.path.basename(path)[:-5]
            for path in glob.glob(os.path.join(root, "*.json"))
        } - {"index", "final-result"}
        if not os.path.exists(index_path):
            problems.append(f"index.json 부재: {root}")
            continue
        index_payload, _ = _load(index_path)
        entries = index_payload.get("entries")
        if not isinstance(entries, dict):
            problems.append(f"index.json entries 구조 아님: {root}")
            continue
        for name, meta in entries.items():
            expected = (
                meta.get("sha256") if isinstance(meta, dict) else meta
            )
            item = collected.get(name)
            if item is None:
                problems.append(f"index가 가리키는 artifact 부재: {name} ({root})")
                continue
            if expected != item["sha256"]:
                problems.append(
                    f"index/artifact sha 불일치: {name} ({root})"
                )
        # 역방향: root의 artifact가 index에 미등재면 문제 (변조·누락 방지)
        for name in sorted(root_artifacts - set(entries.keys())):
            problems.append(f"artifact가 index에 미등재: {name} ({root})")

    tc_status = {tc: status_of(tc) for tc in REQUIRED_TCS + REQUIRED_SOAK}
    deferred_status = {tc: status_of(tc) for tc in DEFERRED_TCS}

    # 일관성 검사. 판정 대상 artifact만 검사한다(이관 항목은 기록 전용).
    judged = {
        name: c for name, c in collected.items() if name not in DEFERRED_TCS
    }
    profiles = {c["payload"].get("profile") for c in judged.values()}
    versions = {c["payload"].get("baseline_version") for c in judged.values()}
    baseline_shas = {c["payload"].get("baseline_sha256") for c in judged.values()}
    retries = []
    for name, c in judged.items():
        if name in SCENARIO_TCS_WITH_SANCTIONED_RETRY:
            continue
        value = _int_or_none(
            (c["payload"].get("counters") or {}).get("retries", 0)
        )
        if value is None:
            problems.append(f"retries 값 비정상(정수 아님): {name}")
            value = -1  # 아래 != 0 검사에서 위반으로 계상
        retries.append(value)
    hardware_maps = {
        json.dumps(c["payload"].get("hardware"), sort_keys=True)
        for c in judged.values()
    }
    # 0.9.22 추가 봉합: §17A.6 "run lineage/generation 일관성"의 실검사 —
    # 구판은 run_id 존재만 보고 lifecycle_generation은 아예 읽지 않아
    # 부재·혼재(1/99) 변조가 통과했다. 존재(정수 ≥1) + 같은 root(=같은
    # run) 안에서 단일값을 강제한다(정당한 재시작은 새 run ID/root).
    generations_by_root = {}

    if profiles - {"RESEARCH_READY_MINIMAL"}:
        problems.append(f"profile 불일치: {profiles}")
    # v0.9.10: phase 사이에 사용자 승인 개정이 있었다 — 균일성 대신
    # "승인 개정 사슬 내"를 강제하고 최신치를 보고한다(사슬 말단 일치는
    # 0.9.20 명문화대로 강제하지 않음 — 문서 전용 개정이 evidence 재실행을
    # 강요하지 않게). 진행 내역은 revision_progression으로 전량 보고(은폐 없음).
    off_chain = versions - set(APPROVED_REVISION_CHAIN)
    if off_chain:
        problems.append(f"승인 개정 사슬 밖 baseline_version: {off_chain}")
    revision_progression = {
        name: {
            "baseline_version": c["payload"].get("baseline_version"),
            "baseline_sha256": c["payload"].get("baseline_sha256"),
        }
        for name, c in sorted(collected.items())
    }
    if any(r != 0 for r in retries):
        problems.append("implicit retry != 0 인 artifact 존재")
    # 0.9.20 보강: §17A.6 필수 확인의 실검사 — artifact type/tc_id 정합,
    # 금지 counter(wrong bytes/destination), duplicates 규율, 금지 UHD
    # outcome을 PASS 표기와 무관하게 counters에서 직접 검증한다.
    for name, c in judged.items():
        payload = c["payload"]
        if payload.get("artifact_type") != "STAGE7_RESEARCH_READY_EVIDENCE_V1":
            problems.append(f"artifact_type 불일치: {name}")
        if payload.get("tc_id") != name:
            problems.append(
                f"tc_id 부재/불일치: {name} (tc_id={payload.get('tc_id')!r})"
            )
        counters = payload.get("counters") or {}
        # 금지 counter는 실측 명칭·별칭 전수로 검사한다 (이름 추측으로
        # 놓치는 fail-open 방지 — 변조 픽스처 실검 근거).
        banned_groups = {
            "wrong destination": (
                "wrong_destination", "wrong_destination_total",
            ),
            "wrong bytes": ("wrong_bytes", "wrong_bytes_total"),
            "UHD underflow": (
                "uhd_underflow", "underflow_count", "underflows",
            ),
            "UHD overflow": (
                "uhd_overflow", "overflow_count", "overflows",
            ),
            "UHD late command": (
                "uhd_late_command", "late_command_count", "late_commands",
            ),
        }
        for label, keys in banned_groups.items():
            for key in keys:
                if counters.get(key, 0) != 0:
                    problems.append(
                        f"금지 counter {label}({key}) != 0: {name}"
                    )
        # 0.9.22: retries 상한 강제 — controller 실측 상한(max_attempts=3
        # → 재시도 ≤2)과 대조. 상한이 없으면 retries를 부풀려 duplicates
        # 허용치를 임의로 키우는 우회가 성립한다 (R1-A 공격 4).
        raw_retries = _int_or_none(counters.get("retries", 0))
        if raw_retries is None:
            problems.append(f"retries 값 비정상(정수 아님): {name}")
            raw_retries = 0  # duplicates 허용치 0으로 보수 계상
        if name in SCENARIO_TCS_WITH_SANCTIONED_RETRY and raw_retries > 2:
            problems.append(
                f"retries > 상한 2 (controller max_attempts=3): "
                f"{name} ({raw_retries})"
            )
        dup = _int_or_none(counters.get("duplicates", 0))
        if dup is None:
            problems.append(f"duplicates 값 비정상(정수 아님): {name}")
            dup = 0
        limit = (
            raw_retries
            if name in SCENARIO_TCS_WITH_SANCTIONED_RETRY else 0
        )
        if dup > limit:
            problems.append(f"duplicates > 허용치: {name} ({dup} > {limit})")
        generation = payload.get("lifecycle_generation")
        if not isinstance(generation, int) or generation < 1:
            problems.append(
                f"lifecycle_generation 부재/비정상: {name} ({generation!r})"
            )
        generations_by_root.setdefault(
            os.path.dirname(os.path.normpath(c["path"])), set()
        ).add(generation)
        run_id = payload.get("run_id")
        if not run_id:
            problems.append(f"run_id 부재: {name}")
        else:
            # 0.9.22: run_id ↔ 소속 root 경로 대응 강제 — evidence 규약은
            # results/runs/<run_id>/evidence/research_ready/. 다른 run의
            # artifact를 복사해 root를 위장하는 바꿔치기를 기계 검사
            # (R1-A 공격 3). R2 재봉합: 구판 "임의 성분 중 일치"는 항상
            # 존재하는 "evidence"/"research_ready" 성분으로 우회 가능 —
            # 고정 꼬리를 걷어낸 실제 run 디렉터리명과의 정확 일치로 교체.
            run_dir = _root_run_dir(os.path.dirname(c["path"]))
            if run_id != run_dir:
                problems.append(
                    f"run_id가 evidence 경로의 run 디렉터리와 불일치: "
                    f"{name} (run_id={run_id!r}, 경로={run_dir!r})"
                )
    if len(hardware_maps) > 1:
        problems.append(f"hardware mapping 불일치: {len(hardware_maps)}종")
    # 0.9.22 (R2 감사 S-1): 재현 조건 기계 기록 3필드(stage7_diag_env 등)는
    # 기록기 신설(0.9.20) 이전에 산출된 채택 evidence에 없다. fail-closed로
    # 하면 기준선 산문(0.9.19 ①)에 이미 공시된 조건 때문에 hardware 재실행을
    # 강요하게 되므로, 부재를 문제 대신 **기계 노출**한다 — 이후 run부터는
    # 필드가 실재하므로 이 목록은 자연 소멸한다.
    reproduction_fields_missing = sorted(
        name for name, c in judged.items()
        if "stage7_diag_env" not in c["payload"]
    )
    for root_key, generations in sorted(generations_by_root.items()):
        if len(generations) > 1:
            problems.append(
                f"lifecycle_generation 혼재(단일 run 내): {root_key} "
                f"{sorted(generations, key=repr)}"
            )

    missing = [tc for tc, s in tc_status.items() if s == "MISSING"]
    failed = [tc for tc, s in tc_status.items() if s == "FAIL"]
    hw_blocked = [tc for tc, s in tc_status.items() if s == "HARDWARE_BLOCKED"]
    aborted = [tc for tc, s in tc_status.items() if s == "ABORTED"]
    # 0.9.22: 허용목록 판정 — READY의 근거는 명시적 "PASS"뿐이다.
    # 거부목록만으로는 임의 문자열("FAILED", "fail", 오타·위장)이 전
    # 버킷을 빠져나가 READY로 탈출한다 (R1-A 공격 6, fail-open 역전).
    unrecognized = [
        tc for tc, s in tc_status.items()
        if s not in ("PASS", "MISSING", "FAIL", "HARDWARE_BLOCKED", "ABORTED")
    ]
    if unrecognized:
        problems.append(
            "status 허용목록 밖(PASS 아님): "
            + ", ".join(f"{tc}={tc_status[tc]!r}" for tc in sorted(unrecognized))
        )

    if not software_ok:
        problems.append("software 검증(회귀/suite/import) 미통과")
    if not frozen_hash_ok:
        problems.append("frozen source hash audit 미통과")
    # 0.9.22: 증언 플래그와 근거 로그의 기계적 연결 — 검증 묶음 root를
    # 필수 결속하고, frozen-hash-audit.json의 all_unchanged를 실검사하며,
    # 로그 sha를 최종 체인에 포함한다 (R1-A 공격 5: 순수 증언 fail-open).
    verification_evidence = {}
    if not verification_root:
        problems.append("검증 로그 미결속 (--verification-root 부재)")
    else:
        audit_path = os.path.join(verification_root, "frozen-hash-audit.json")
        if not os.path.exists(audit_path):
            problems.append(
                f"frozen-hash-audit.json 부재: {verification_root}"
            )
        else:
            audit_payload, _audit_sha = _load(audit_path)
            # 0.9.22 R2 보강: 플래그 하나만 보면 손으로 쓴 한 줄
            # `{"all_unchanged": true}`도 통과한다 — 감사 도구 산출물의
            # 구조(파일 목록 실재)와 자기결속(head_commit)을 함께 요구.
            # (배열 등 비객체 JSON의 AttributeError crash도 함께 봉합.)
            if not isinstance(audit_payload, dict):
                problems.append("frozen-hash-audit 구조 아님(JSON 객체 아님)")
            else:
                if audit_payload.get("all_unchanged") is not True:
                    problems.append(
                        "frozen-hash-audit all_unchanged != true "
                        "(증언 플래그와 근거 로그 모순)"
                    )
                audit_files = audit_payload.get("files")
                if not isinstance(audit_files, list) or not audit_files:
                    problems.append(
                        "frozen-hash-audit files 목록 부재/공백 "
                        "(감사 도구 산출물 아님)"
                    )
                installation_digest = audit_payload.get(
                    "installation_sha256")
                if installation_digest is not None:
                    # v2 (standalone clone-bootstrap, additive): 감사 묶음이
                    # git HEAD 대신 **installation digest**로 자기결속한다.
                    # head_provider는 현행 installation digest를 돌려준다
                    # (Git digest를 installation digest로 위장하지 않는다 —
                    # 두 경로는 필드명으로 구분된다). 검사 의미는 v1과 같다:
                    # 64-hex 형식 + 현행값 일치 + 확인 불능 fail-closed.
                    if not (isinstance(installation_digest, str)
                            and len(installation_digest) == 64):
                        problems.append(
                            "installation-audit installation_sha256 "
                            "자기결속 부재"
                        )
                    else:
                        current_digest = _head_of()
                        if current_digest is None:
                            problems.append(
                                "현행 installation digest 확인 불가 — "
                                "installation_sha256 결속 검증 불능"
                            )
                        elif installation_digest != current_digest:
                            problems.append(
                                "installation-audit installation_sha256이 "
                                "현행 설치와 불일치: "
                                f"{installation_digest[:12]} != "
                                f"{current_digest[:12]} (검증 묶음 재생성 필요)"
                            )
                    if audit_payload.get("installation_verified") is not True:
                        problems.append(
                            "installation-audit installation_verified != true "
                            "(설치 재측정 미통과 또는 필드 부재)"
                        )
                else:
                    head_commit = audit_payload.get("head_commit")
                    if not (isinstance(head_commit, str)
                            and len(head_commit) == 40):
                        problems.append(
                            "frozen-hash-audit head_commit 자기결속 부재"
                        )
                    else:
                        # 0.9.22 사용자 지적: 형식 검사만으론 낡은 묶음이
                        # 통과 — 현행 HEAD 일치를 요구(코드 변경 후엔 묶음
                        # 재생성 강제). HEAD 확인 불능도 fail-closed.
                        current_head = _head_of()
                        if current_head is None:
                            problems.append(
                                "현행 git HEAD 확인 불가 — head_commit 결속 "
                                "검증 불능"
                            )
                        elif head_commit != current_head:
                            problems.append(
                                "frozen-hash-audit head_commit이 현행 HEAD와 "
                                f"불일치: {head_commit[:12]} != "
                                f"{current_head[:12]} (검증 묶음 재생성 필요)"
                            )
                    if audit_payload.get("worktree_dirty") is not False:
                        problems.append(
                            "frozen-hash-audit worktree_dirty != false "
                            "(오염 트리 감사 또는 필드 부재)"
                        )
        for fname in (
            "frozen-hash-audit.json", "unit-contract.log",
            "integration-stage7.log", "frozen-audit-stdout.log",
        ):
            fpath = os.path.join(verification_root, fname)
            if not os.path.exists(fpath):
                if fname == "frozen-audit-stdout.log":
                    continue  # 부수 stdout 사본 — 있으면 결속, 없어도 무방
                problems.append(f"검증 로그 부재: {fname}")
                continue
            with open(fpath, "rb") as handle:
                verification_evidence[fname] = hashlib.sha256(
                    handle.read()
                ).hexdigest()

    # 원본 evidence 결속: 각 root의 상위 evidence 디렉터리에 있는
    # coordinator/runtime 원본의 sha256을 최종 체인에 포함한다 —
    # artifact 요약이 결손인 경우(예: RR-004 TX/RX 사전화 충돌)에도
    # 원본이 SHA로 결속·검증되게 한다 (artifact 자체는 불변 유지).
    # 주의: 이 블록은 반드시 final 판정 **이전**이어야 한다 — 여기서
    # 계상한 problems가 판정에 반영되게 (0.9.22 순서 재배치).
    source_evidence = {}
    for root in evidence_roots:
        parent = os.path.dirname(os.path.normpath(root))
        bound = {}
        for pattern in ("coordinator-*.json", "*-runtime.json"):
            for path in sorted(glob.glob(os.path.join(parent, pattern))):
                with open(path, "rb") as handle:
                    bound[os.path.basename(path)] = hashlib.sha256(
                        handle.read()
                    ).hexdigest()
        # 0.9.22 추가 봉합: 구판 `if bound:`는 원본이 전무한 root를 조용히
        # 누락(부재 비가시 — 삭제 변조와 구분 불가). 실측 규약(root당
        # coordinator 1 + endpoint runtime 3)을 하한으로 강제하고, 빈
        # 결속도 결과에 노출한다.
        coordinator_count = sum(
            1 for k in bound if k.startswith("coordinator-")
        )
        runtime_count = sum(1 for k in bound if k.endswith("-runtime.json"))
        if coordinator_count < 1 or runtime_count < 3:
            problems.append(
                f"원본 evidence 부재: {root} (coordinator={coordinator_count}"
                f", runtime={runtime_count} — 요구 ≥1/≥3)"
            )
        source_evidence[os.path.normpath(root)] = bound

    if missing or aborted:
        final = "ABORTED" if aborted else "RESEARCH_NOT_READY"
    elif failed or problems:
        final = "RESEARCH_NOT_READY"
    elif hw_blocked:
        final = "HARDWARE_BLOCKED"
    else:
        final = "RESEARCH_READY"

    latest_version = max(
        (v for v in versions if v in APPROVED_REVISION_CHAIN),
        key=APPROVED_REVISION_CHAIN.index, default=None,
    )

    result = {
        "artifact_type": "STAGE7_RESEARCH_READY_FINAL_V1",
        "source_evidence_sha256": source_evidence,
        "profile": "RESEARCH_READY_MINIMAL",
        "baseline_version": latest_version,
        "baseline_versions_used": sorted(versions),
        "baseline_sha256_used": sorted(baseline_shas),
        "revision_progression": revision_progression,
        "deferred_follow_up": {
            "items": dict(deferred_status),
            "reason": "v0.9.10 사용자 지시 — Link 19 자격은 후속 단계 이관 "
                      "(확정 응용 시나리오 3종의 지정 링크는 17)",
        },
        "final_status": final,
        "tc_status": tc_status,
        "missing": missing,
        "failed": failed,
        "hardware_blocked": hw_blocked,
        "aborted": aborted,
        "consistency_problems": problems,
        "software_verification_ok": bool(software_ok),
        "frozen_hash_ok": bool(frozen_hash_ok),
        "verification_evidence_sha256": verification_evidence,
        "reproduction_fields_missing": reproduction_fields_missing,
        "artifact_sha256": {name: c["sha256"] for name, c in sorted(collected.items())},
        "scope": "RESEARCH_READY는 통제된 conducted 환경에서 OTA 연구 실험 진입 "
                 "가능성만 뜻한다. full qualification(TC-STAGE7-001..020) 및 "
                 "VERIFIED/FROZEN을 대체하지 않는다.",
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # 0.9.22 (R2 감사 M-9): 제자리 덮어쓰기는 이전 판정 이력을 소실시킨다
    # (results/는 git 밖). 내용이 다르면 이전 본을 superseded-*로 보존 —
    # §17A.5 "전량 보존"의 판정 산출물 확장.
    if os.path.exists(out_path):
        with open(out_path, "rb") as handle:
            previous = handle.read()
        new_bytes = json.dumps(
            result, ensure_ascii=True, sort_keys=True, indent=1
        ).encode("utf-8")
        if previous != new_bytes:
            import datetime as _dt

            stamp = _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            backup = out_path[:-5] + f".superseded-{stamp}.json" \
                if out_path.endswith(".json") \
                else out_path + f".superseded-{stamp}"
            with open(backup, "wb") as handle:
                handle.write(previous)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=True, sort_keys=True, indent=1)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", action="append", required=True,
                        help="evidence/research_ready 디렉터리 (여러 run 가능)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--software-ok", action="store_true")
    parser.add_argument("--frozen-hash-ok", action="store_true")
    parser.add_argument(
        "--verification-root",
        help="frozen-hash-audit.json/unit-contract.log/integration-stage7.log"
             "가 있는 검증 묶음 디렉터리 — 미지정 시 READY 불가 (0.9.22)",
    )
    arguments = parser.parse_args(argv)
    result = build(
        arguments.evidence_root, arguments.out,
        software_ok=arguments.software_ok, frozen_hash_ok=arguments.frozen_hash_ok,
        verification_root=arguments.verification_root,
    )
    try:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    except UnicodeEncodeError:  # Windows cp949 콘솔 — 파일 기록엔 무영향
        print(json.dumps(result, ensure_ascii=True, indent=1))
    return 0 if result["final_status"] == "RESEARCH_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
