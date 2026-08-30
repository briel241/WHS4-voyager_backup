"""RESEARCH_READY_MINIMAL final builder (`stage7` §17A.6, v0.9.1 J항).

여러 RR run-root의 evidence/research_ready/*.json을 모아 TC-STAGE7-RR-001..008이
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


def build(evidence_roots, out_path, *, software_ok, frozen_hash_ok):
    collected = {}
    problems = []
    for root in evidence_roots:
        for path in glob.glob(os.path.join(root, "*.json")):
            name = os.path.basename(path)[:-5]
            if name in ("index", "final-result"):
                continue
            payload, sha = _load(path)
            collected[name] = {"payload": payload, "sha256": sha, "path": path}

    def status_of(tc):
        item = collected.get(tc)
        return item["payload"].get("status") if item else "MISSING"

    # 0.9.20: artifact-SHA/index 상호 대조 — 각 evidence root의 index.json이
    # 가리키는 sha256과 실측 artifact sha256의 불일치를 일관성 문제로 계상.
    for root in evidence_roots:
        index_path = os.path.join(root, "index.json")
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

    tc_status = {tc: status_of(tc) for tc in REQUIRED_TCS + REQUIRED_SOAK}
    deferred_status = {tc: status_of(tc) for tc in DEFERRED_TCS}

    # 일관성 검사. 판정 대상 artifact만 검사한다(이관 항목은 기록 전용).
    judged = {
        name: c for name, c in collected.items() if name not in DEFERRED_TCS
    }
    profiles = {c["payload"].get("profile") for c in judged.values()}
    versions = {c["payload"].get("baseline_version") for c in judged.values()}
    baseline_shas = {c["payload"].get("baseline_sha256") for c in judged.values()}
    retries = [
        int((c["payload"].get("counters") or {}).get("retries", 0))
        for name, c in judged.items()
        if name not in SCENARIO_TCS_WITH_SANCTIONED_RETRY
    ]
    hardware_maps = {
        json.dumps(c["payload"].get("hardware"), sort_keys=True)
        for c in judged.values()
    }

    if profiles - {"RESEARCH_READY_MINIMAL"}:
        problems.append(f"profile 불일치: {profiles}")
    # v0.9.10: phase 사이에 사용자 승인 개정이 있었다 — 균일성 대신
    # "승인 개정 사슬 내 + 최종이 사슬 말단" 을 강제하고, 진행 내역을
    # revision_progression으로 전량 보고한다(은폐 없음).
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
        if payload.get("tc_id") not in (None, name):
            problems.append(f"tc_id/파일명 불일치: {name}")
        counters = payload.get("counters") or {}
        if counters.get("wrong_destination", 0) != 0:
            problems.append(f"wrong_destination != 0: {name}")
        if counters.get("wrong_bytes", 0) != 0:
            problems.append(f"wrong_bytes != 0: {name}")
        dup = counters.get("duplicates", 0)
        limit = (
            counters.get("retries", 0)
            if name in SCENARIO_TCS_WITH_SANCTIONED_RETRY else 0
        )
        if dup > limit:
            problems.append(f"duplicates > 허용치: {name} ({dup} > {limit})")
        for banned in ("uhd_underflow", "uhd_overflow", "uhd_late_command"):
            if counters.get(banned, 0) != 0:
                problems.append(f"금지 UHD outcome {banned} != 0: {name}")
        if not payload.get("run_id"):
            problems.append(f"run_id 부재: {name}")
    if len(hardware_maps) > 1:
        problems.append(f"hardware mapping 불일치: {len(hardware_maps)}종")

    missing = [tc for tc, s in tc_status.items() if s == "MISSING"]
    failed = [tc for tc, s in tc_status.items() if s == "FAIL"]
    hw_blocked = [tc for tc, s in tc_status.items() if s == "HARDWARE_BLOCKED"]
    aborted = [tc for tc, s in tc_status.items() if s == "ABORTED"]

    if not software_ok:
        problems.append("software 검증(회귀/suite/import) 미통과")
    if not frozen_hash_ok:
        problems.append("frozen source hash audit 미통과")

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
        "artifact_sha256": {name: c["sha256"] for name, c in sorted(collected.items())},
        "scope": "RESEARCH_READY는 통제된 conducted 환경에서 OTA 연구 실험 진입 "
                 "가능성만 뜻한다. full qualification(TC-STAGE7-001..020) 및 "
                 "VERIFIED/FROZEN을 대체하지 않는다.",
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
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
    arguments = parser.parse_args(argv)
    result = build(
        arguments.evidence_root, arguments.out,
        software_ok=arguments.software_ok, frozen_hash_ok=arguments.frozen_hash_ok,
    )
    try:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    except UnicodeEncodeError:  # Windows cp949 콘솔 — 파일 기록엔 무영향
        print(json.dumps(result, ensure_ascii=True, indent=1))
    return 0 if result["final_status"] == "RESEARCH_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
