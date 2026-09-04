"""자격 결속 3영역 분리 + evidence 재사용 판정 (Phase I, 2026-09-05 사용자 승인).

배포 manifest의 모든 파일을 **정확히 한 영역**으로 분류한다:

  - ``rf``        : RF evidence 거동을 만드는 파일(coordinator·launcher·scenario
                    controller·frozen ``src/vdeter``·RF config). 재사용은
                    **byte-identical**을 요구한다.
  - ``operation`` : 일반 ``vdeter run`` 거동 파일(standalone run 경로·상위 apps·
                    common·MASS·ROC·환경 선언). 재사용은 **동일하거나 별도
                    회귀 통과**를 요구한다.
  - ``tooling``   : 자격 오케스트레이션·검증·패키징 도구(``qualify_cmd``·
                    ``VDETER/tools``·템플릿). 변경 시 **수정된 verification
                    bundle 재실행**으로 검증한다.
  - ``test_doc``  : 시험·문서. **자격 결속이 아니다** — 변경만으로 RF 자격을
                    무효화하지 않는다.

분류는 경로 패턴(첫 일치)이며, ``classify``가 모든 배포 파일을 빠짐없이 한
영역에 넣는다(‘unknown’ 없음 — completeness 시험이 고정). tooling·test_doc는
명시 집합이고, 나머지는 rf(명시) 또는 operation(보수적 기본값)이다. 즉 미분류
파일은 operation(핵심)으로 떨어져 **fail-safe**하다.

evidence 재사용 판정(:func:`evaluate_reuse`)은 순수 함수다 — 결과를 바꾸지
않고 이전/현재 manifest만 비교한다. rf 또는 operation 핵심 파일이 하나라도
다르면(operation은 회귀 미통과 시) 재사용을 거부한다.
"""

from __future__ import annotations

import hashlib
import json

RF = "rf"
OPERATION = "operation"
TOOLING = "tooling"
TEST_DOC = "test_doc"

DOMAINS = (RF, OPERATION, TOOLING, TEST_DOC)

#: 재사용 자격에서 **핵심**(변경 시 원칙적으로 거부) 영역.
CORE_DOMAINS = (RF, OPERATION)
#: 변경이 허용되되 재검증을 요구하는 영역(verification bundle 재실행).
TOOLING_DOMAINS = (TOOLING,)
#: 결속이 아닌 영역(변경 무시).
NONBINDING_DOMAINS = (TEST_DOC,)

#: RF 거동을 만드는 명시 실행 파일(상위 apps 중).
_RF_APPS = frozenset({
    "VDETER/apps/stage7_coordinator_main.py",
    "VDETER/apps/stage7_endpoint_launcher.py",
    "VDETER/apps/stage7_scenario_controller.py",
})
#: 자격 tooling 명시 파일(상위 apps 중). run/RF 경로 거동에 관여하지 않는
#: 오케스트레이션·검증 진입점. (run_cmd가 qualify_cmd.load_active_reference를
#: import하지만, 이는 read-only reference 로더이고 operation 거동 판정은
#: operation 회귀가 담당한다.)
_TOOLING_FILES = frozenset({
    "apps/standalone/qualify_cmd.py",
})


def classify(relpath: str) -> str:
    """배포 상대경로를 정확히 한 영역으로 분류(첫 일치)."""
    p = relpath.replace("\\", "/")

    # 1) 시험·문서 — 결속 아님.
    if p.startswith("VDETER/tests/") or p.startswith("tests/"):
        return TEST_DOC
    if p.endswith(".md") or "/docs/" in p or p.startswith("docs/"):
        return TEST_DOC

    # 2) RF 거동(byte-identical 요구).
    if p in _RF_APPS:
        return RF
    if p.startswith("VDETER/src/vdeter/"):
        return RF
    if p.startswith("VDETER/configs/"):
        return RF

    # 3) 자격 tooling(변경 시 bundle 재실행).
    if p in _TOOLING_FILES:
        return TOOLING
    if p.startswith("VDETER/tools/"):
        return TOOLING
    if p.startswith("apps/standalone/templates/"):
        return TOOLING

    # 4) 그 외 배포 코드 = 일반 운용(보수적 기본값).
    return OPERATION


def _aggregate_sha256(files: dict) -> str:
    """영역 파일 map(path→sha)의 정규 집계 해시(순서 무관, 내용 결속)."""
    canonical = json.dumps(files, ensure_ascii=True, sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def split_manifest(manifest: dict) -> dict:
    """배포 manifest를 영역별 sub-manifest로 분리.

    반환 {domain: {"files": {path: sha}, "file_count": n,
    "aggregate_sha256": sha}} — 모든 영역 키가 존재(빈 영역은 file_count 0)."""
    buckets = {d: {} for d in DOMAINS}
    for entry in manifest.get("files", ()):
        buckets[classify(entry["path"])][entry["path"]] = entry["sha256"]
    return {d: {"files": files, "file_count": len(files),
                "aggregate_sha256": _aggregate_sha256(files)}
            for d, files in buckets.items()}


def _domain_diff(prior_files: dict, current_files: dict) -> dict:
    """두 영역 파일 map 차이(추가·삭제·불일치). 빈 dict면 byte-identical."""
    added = sorted(set(current_files) - set(prior_files))
    removed = sorted(set(prior_files) - set(current_files))
    changed = sorted(k for k in set(prior_files) & set(current_files)
                     if prior_files[k] != current_files[k])
    return {"added": added, "removed": removed, "changed": changed}


def _diff_empty(diff: dict) -> bool:
    return not (diff["added"] or diff["removed"] or diff["changed"])


def evaluate_reuse(prior_manifest: dict, current_manifest: dict, *,
                   operation_regression_passed: bool = False) -> dict:
    """이전 RF evidence manifest 대비 현재 배포 트리의 재사용 자격 판정.

    규칙(사용자 지정):
      - rf 핵심이 하나라도 다르면 거부(byte-identical 요구).
      - operation 핵심이 다르면, ``operation_regression_passed``가 아니면 거부
        (동일하거나 별도 회귀 통과 요구).
      - tooling 변경은 허용하되 호출부가 수정된 verification bundle을 재실행해야
        한다(이 함수는 bundle 실행 여부를 판정하지 않고 tooling 변경만 보고).
      - test_doc 변경은 자격에 영향 없음.

    반환: eligibility·영역별 diff·차단 파일. 결과를 바꾸지 않는 순수 판정."""
    prior = split_manifest(prior_manifest)
    current = split_manifest(current_manifest)
    diffs = {d: _domain_diff(prior[d]["files"], current[d]["files"])
             for d in DOMAINS}

    rf_identical = _diff_empty(diffs[RF])
    operation_identical = _diff_empty(diffs[OPERATION])
    tooling_changed = not _diff_empty(diffs[TOOLING])
    test_doc_changed = not _diff_empty(diffs[TEST_DOC])

    blocking = []
    if not rf_identical:
        for k in ("added", "removed", "changed"):
            blocking += [f"rf:{k}:{f}" for f in diffs[RF][k]]
    operation_ok = operation_identical or operation_regression_passed
    if not operation_ok:
        for k in ("added", "removed", "changed"):
            blocking += [f"operation:{k}:{f}" for f in diffs[OPERATION][k]]

    eligible = rf_identical and operation_ok
    return {
        "eligible": eligible,
        "rf_identical": rf_identical,
        "operation_identical": operation_identical,
        "operation_regression_passed": operation_regression_passed,
        "operation_ok": operation_ok,
        "tooling_changed": tooling_changed,
        "test_doc_changed": test_doc_changed,
        "blocking_files": blocking,
        "diffs": diffs,
        "prior_domain_sha256": {d: prior[d]["aggregate_sha256"] for d in DOMAINS},
        "current_domain_sha256": {d: current[d]["aggregate_sha256"]
                                  for d in DOMAINS},
    }
