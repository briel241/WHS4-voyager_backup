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

import ast
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
#: 자격 tooling 명시 파일(상위 apps 중). RF/run 거동을 직접 만들지 않는
#: 오케스트레이션·검증 진입점. qualify_cmd.py는 **파일 단위로는 tooling**이되,
#: 그 안의 RF 지배 심볼(:data:`RF_CRITICAL_QUALIFY_SYMBOLS`)은 함수 단위로 rf에
#: 별도 결속한다 — run_mode/phase 순서/phase 인자 변경은 재사용을 거부한다.
_TOOLING_FILES = frozenset({
    "apps/standalone/qualify_cmd.py",
    "apps/standalone/qualification_domains.py",
})

#: 파일 단위로는 tooling이지만 RF qualification-critical로 **함수 단위** 결속하는
#: 심볼(사용자 지정: RF phase 순서·run mode·phase→TC·gain 소비 phase·phase 인자/
#: env 전달). 이들이 하나라도 바뀌면 RF 재사용을 거부한다. config/gain 해석
#: 본체(`generate_run_configs`)와 env 빌더(`build_child_env`)는 operation 파일에
#: 있어 operation byte-identity로 결속된다.
RF_CRITICAL_QUALIFY_SYMBOLS = {
    "apps/standalone/qualify_cmd.py": (
        "LADDER_PHASES",          # RF phase 순서
        "PHASE_RUN_MODE",         # phase별 run mode
        "PHASE_EXPECTED_TCS",     # phase→TC 결속
        "GAIN_CONSUMING_PHASES",  # gain 소비 phase
        "default_phase_runner",   # coordinator argv·gain-artifact·env 전달
    ),
}


def classify(relpath: str) -> str:
    """배포 상대경로를 정확히 한 영역으로 분류(첫 일치)."""
    p = relpath.replace("\\", "/")

    # 1) 문서만 비결속(markdown·docs). tests 하위의 .md도 여기(첫 일치).
    if p.endswith(".md") or "/docs/" in p or p.startswith("docs/"):
        return TEST_DOC

    # 2) qualification 시험 suite = qualification-tool(변경 시 bundle 재실행).
    #    bundle이 도는 unit/contract/integration + support/fixtures 전체.
    if p.startswith("VDETER/tests/") or p.startswith("tests/"):
        return TOOLING

    # 3) RF 거동(byte-identical 요구).
    if p in _RF_APPS:
        return RF
    if p.startswith("VDETER/src/vdeter/"):
        return RF
    if p.startswith("VDETER/configs/"):
        return RF

    # 4) 자격 tooling(변경 시 bundle 재실행).
    if p in _TOOLING_FILES:
        return TOOLING
    if p.startswith("VDETER/tools/"):
        return TOOLING
    if p.startswith("apps/standalone/templates/"):
        return TOOLING

    # 5) 그 외 배포 코드 = 일반 운용(보수적 기본값).
    return OPERATION


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rf_critical_symbol_hashes(source: str, symbols) -> dict:
    """소스에서 지정 최상위 심볼(함수 def 또는 상수 Assign)의 (AST, source)
    해시를 뽑는다. 누락 심볼은 제외되며 호출부가 완전성을 검사한다."""
    tree = ast.parse(source)
    want = set(symbols)
    out = {}
    for node in tree.body:
        names = []
        if isinstance(node, ast.FunctionDef):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in names:
            if name in want:
                out[name] = [_sha(ast.dump(node)), _sha(ast.unparse(node))]
    return out


def rf_critical_symbol_baseline(file_source_map: dict) -> dict:
    """{배포경로: 소스문자열} → {배포경로: {심볼: [ast_sha, src_sha]}}.

    RF_CRITICAL_QUALIFY_SYMBOLS의 모든 심볼이 없으면 예외(결속 대상 누락)."""
    baseline = {}
    for relpath, wanted in RF_CRITICAL_QUALIFY_SYMBOLS.items():
        source = file_source_map.get(relpath)
        if source is None:
            raise ValueError(f"RF-critical 심볼 소스 없음: {relpath}")
        hashes = rf_critical_symbol_hashes(source, wanted)
        missing = sorted(set(wanted) - set(hashes))
        if missing:
            raise ValueError(f"{relpath} RF-critical 심볼 누락: {missing}")
        baseline[relpath] = hashes
    return baseline


def rf_symbol_diff(prior: dict, current: dict) -> list:
    """이전/현재 RF-critical 심볼 baseline 차이(변경·누락 심볼 목록). 빈
    리스트면 함수 단위로 byte-identical."""
    changed = []
    for relpath, wanted in RF_CRITICAL_QUALIFY_SYMBOLS.items():
        p = prior.get(relpath, {})
        c = current.get(relpath, {})
        for symbol in wanted:
            if p.get(symbol) != c.get(symbol):
                changed.append(f"{relpath}::{symbol}")
    return changed


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
                   prior_rf_symbols: dict | None = None,
                   current_rf_symbols: dict | None = None) -> dict:
    """이전 RF evidence manifest 대비 현재 배포 트리의 재사용 자격 판정.

    규칙(사용자 지정, 2026-09-05 보완):
      - rf 영역 파일이 하나라도 다르면 거부(byte-identical 요구).
      - rf-critical 심볼(qualify_cmd의 phase 순서·run mode·phase→TC·gain 소비
        phase·phase 인자 전달)이 함수 단위로 다르면 거부. (파일 단위로는
        qualify_cmd가 tooling이라 governance/bundle 변경은 허용되지만, RF 지배
        심볼 변경은 여기서 잡는다.)
      - operation 영역은 **byte-identical**이어야 한다(현 정책: 회귀 기반
        재사용은 별도 영향분석 정책 전까지 불허).
      - tooling 변경은 허용하되 호출부가 수정된 verification bundle을 재실행한다.
      - test_doc(문서) 변경은 자격에 영향 없음.

    반환: eligibility·영역별 diff·rf 심볼 diff·차단 목록. 순수 판정(무수정)."""
    prior = split_manifest(prior_manifest)
    current = split_manifest(current_manifest)
    diffs = {d: _domain_diff(prior[d]["files"], current[d]["files"])
             for d in DOMAINS}

    rf_files_identical = _diff_empty(diffs[RF])
    operation_identical = _diff_empty(diffs[OPERATION])
    tooling_changed = not _diff_empty(diffs[TOOLING])
    test_doc_changed = not _diff_empty(diffs[TEST_DOC])

    rf_symbols_changed = rf_symbol_diff(prior_rf_symbols or {},
                                        current_rf_symbols or {})
    rf_symbols_identical = not rf_symbols_changed
    rf_identical = rf_files_identical and rf_symbols_identical

    blocking = []
    if not rf_files_identical:
        for k in ("added", "removed", "changed"):
            blocking += [f"rf:{k}:{f}" for f in diffs[RF][k]]
    for symbol in rf_symbols_changed:
        blocking.append(f"rf_symbol:changed:{symbol}")
    if not operation_identical:
        for k in ("added", "removed", "changed"):
            blocking += [f"operation:{k}:{f}" for f in diffs[OPERATION][k]]

    eligible = rf_identical and operation_identical
    return {
        "eligible": eligible,
        "rf_identical": rf_identical,
        "rf_files_identical": rf_files_identical,
        "rf_symbols_identical": rf_symbols_identical,
        "rf_symbols_changed": rf_symbols_changed,
        "operation_identical": operation_identical,
        "tooling_changed": tooling_changed,
        "test_doc_changed": test_doc_changed,
        "blocking_files": blocking,
        "diffs": diffs,
        "prior_domain_sha256": {d: prior[d]["aggregate_sha256"] for d in DOMAINS},
        "current_domain_sha256": {d: current[d]["aggregate_sha256"]
                                  for d in DOMAINS},
    }
