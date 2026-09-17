"""자격 결속 영역 분류 + evidence 재사용 판정 (Phase I → clone-bootstrap v2).

설치 파일(installation scan)의 모든 파일을 **하나 이상의** 영역으로 분류한다
(v2: 교차영향 파일은 여러 영역에 중복 결속 — 사용자 지정):

  - ``rf``          : RF evidence 거동을 만드는 파일. PHY/MAC/Link/Radio·
                      coordinator·launcher·pi_runtime·시간 변환·gain/channel/
                      sample-rate config·RF phase 인자/run mode·RF config 생성
                      코드. 재사용은 **byte-identical**을 요구한다.
  - ``operation``   : 일반 ``vdeter run`` 거동 파일(run_cmd·local_session·
                      local_config·local_preflight·identity 적재·MASS/ROC
                      application·scenario controller·Service API 결속).
                      현 정책: 재사용은 **byte-identical**을 요구한다.
  - ``tooling``     : setup·qualify의 비RF 부분·verification bundle·final
                      builder·reference 발급/검증·영역 분류기·installation
                      manifest·qualification에서 실행하는 시험 코드. 변경 시
                      **software bundle 재실행**으로 검증한다.
  - ``non_binding`` : 일반 Markdown·보고서·개발 메모·이미지 등 실행 판정과
                      무관한 열람 자료. 자격 결속이 아니다.

규칙은 경로 패턴이며, 미분류 실행 파일은 **operation**으로 떨어져 fail-safe
하다(사용자 지정). qualification 시험 파일은 비결속이 아니라 tooling이다.
``apps/standalone/qualify_cmd.py``는 파일 단위로는 tooling이되 RF 지배 심볼
(:data:`RF_CRITICAL_QUALIFY_SYMBOLS`)은 함수 단위 AST/소스 해시로 rf 영역에
별도 결속한다.

v1 호환: :func:`classify`(단일 주영역, test_doc)와 :func:`split_manifest`·
:func:`evaluate_reuse`는 v1 배포 manifest(`files: [{path, sha256}]`)용으로
유지한다. v2 installation record(`files: {path: {sha256, domains}}`)는
:func:`evaluate_reuse_v2`가 판정한다.
"""

from __future__ import annotations

import ast
import hashlib
import json

RF = "rf"
OPERATION = "operation"
TOOLING = "tooling"
TEST_DOC = "test_doc"          # v1 명칭 (v2 non_binding과 동의)
NON_BINDING = "non_binding"

DOMAINS = (RF, OPERATION, TOOLING, TEST_DOC)
#: v2 결속 영역(비결속 제외).
BOUND_DOMAINS = (RF, OPERATION, TOOLING)

#: 재사용 자격에서 **핵심**(변경 시 원칙적으로 거부) 영역.
CORE_DOMAINS = (RF, OPERATION)
TOOLING_DOMAINS = (TOOLING,)
NONBINDING_DOMAINS = (TEST_DOC,)

#: RF 거동을 만드는 명시 실행 파일(상위 apps 중). launcher·coordinator는 rf
#: 전용, scenario controller는 rf+operation(교차영향).
_RF_ONLY_FILES = frozenset({
    "VDETER/apps/coordinator_main.py",
    "VDETER/apps/endpoint_launcher.py",
})
_RF_AND_OPERATION_FILES = frozenset({
    "VDETER/apps/scenario_controller.py",   # scenario controller
    "apps/standalone/local_config.py",              # RF config 생성에 관여
    "apps/standalone/hardware_profiles.py",         # host profile→template·provisioning 입력
})
#: 자격 tooling 명시 파일(standalone 중). setup·qualify·분류기·초안 생성.
_TOOLING_ONLY_FILES = frozenset({
    "VDETER/tools/e2e_security_provision.py",
    "apps/standalone/qualify_cmd.py",
    "apps/standalone/qualification_domains.py",
    "apps/standalone/setup_cmd.py",
    "apps/standalone/hardware_draft.py",         # setup 전용 hardware.toml 초안 생성
})
#: tooling이면서 일반 실행도 쓰는 파일(교차영향 → 두 영역).
_TOOLING_AND_OPERATION_FILES = frozenset({
    "apps/standalone/installation_manifest.py",   # setup 생성 + run 검증
    "apps/standalone/provenance.py",               # baseline pin·git 참고
    "apps/run_profile.py",                      # reference 검증기(run·qualify)
    "requirements-standalone.lock",                # 환경 정의
    "bin/vdeter",                                  # 저장소 launcher
})
#: 비결속 파일명/접미사.
_NON_BINDING_SUFFIXES = (".md", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                         ".pdf", ".xlsx", ".docx", ".pptx")
_NON_BINDING_FILES = frozenset({".gitignore", ".gitattributes",
                                "VDETER/.gitignore"})
_NON_BINDING_NAMES = frozenset({"LICENSE", "LICENSE.txt", "NOTICE"})

#: 파일 단위로는 tooling이지만 RF qualification-critical로 **함수 단위** 결속하는
#: 심볼(RF phase 순서·run mode·phase→TC·gain 소비 phase·phase 인자/env 전달).
RF_CRITICAL_QUALIFY_SYMBOLS = {
    "apps/standalone/qualify_cmd.py": (
        "LADDER_PHASES",          # RF phase 순서
        "PHASE_RUN_MODE",         # phase별 run mode
        "PHASE_EXPECTED_TCS",     # phase→TC 결속
        "GAIN_CONSUMING_PHASES",  # gain 소비 phase
        "default_phase_runner",   # coordinator argv·gain-artifact·env 전달
    ),
}


def _is_non_binding(p: str) -> bool:
    name = p.rsplit("/", 1)[-1]
    if p in _NON_BINDING_FILES or name in _NON_BINDING_NAMES:
        return True
    if p.startswith("docs/") or "/docs/" in p:
        return True
    return p.lower().endswith(_NON_BINDING_SUFFIXES)


def classify_domains(relpath: str) -> tuple:
    """설치 상대경로 → 영역 tuple (하나 이상). 비결속은 ``(NON_BINDING,)``.

    순서는 "주영역 먼저"다 — v1 :func:`classify`는 첫 원소를 쓴다."""
    p = relpath.replace("\\", "/")

    # 1) 비결속: Markdown·문서·이미지·git 메타.
    if _is_non_binding(p):
        return (NON_BINDING,)

    # 2) qualification 시험 suite·시험 도구 = tooling (비결속 아님).
    if p.startswith(("VDETER/tests/", "tests/", "VDETER/tools/", "tools/",
                     "apps/standalone/templates/")):
        return (TOOLING,)
    if p in ("VDETER/pyproject.toml", "VDETER/requirements.txt",
             "VDETER/uv.lock"):
        return (TOOLING,)

    # 3) 명시 교차영향 파일.
    if p in _RF_AND_OPERATION_FILES:
        return (RF, OPERATION)
    if p in _TOOLING_AND_OPERATION_FILES:
        return (TOOLING, OPERATION)

    # 4) RF 거동(byte-identical 요구).
    if p in _RF_ONLY_FILES:
        return (RF,)
    if p.startswith("VDETER/src/vdeter/") or p.startswith("VDETER/configs/"):
        return (RF,)

    # 5) 자격 tooling.
    if p in _TOOLING_ONLY_FILES:
        return (TOOLING,)

    # 6) 그 외 실행 파일 = 일반 운용(보수적 기본값, fail-safe).
    return (OPERATION,)


def classify(relpath: str) -> str:
    """v1 호환: 주영역 하나(비결속은 ``test_doc``)."""
    primary = classify_domains(relpath)[0]
    return TEST_DOC if primary == NON_BINDING else primary


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rf_critical_symbol_hashes(source: str, symbols) -> dict:
    """소스에서 지정 최상위 심볼(함수 def 또는 상수 Assign)의 해시 두 개를
    뽑는다: [원문 구간 SHA-256, CRLF→LF 정규화 구간 SHA-256]. 누락 심볼은
    제외되며 호출부가 완전성을 검사한다.

    원문 구간(`ast.get_source_segment`)은 **바이트 그대로**라 Python 판 사이에
    안정적이다. `ast.unparse`/`ast.dump` 표현은 CPython patch 판에 따라 달라질
    수 있어(3.12.3 vs 3.12.13 실측: unparse 상이) 결속에 쓰지 않는다 — Python
    패키지 갱신만으로 rf digest가 바뀌어 UNQUALIFIED가 나면 안 된다."""
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
                segment = ast.get_source_segment(source, node) or ""
                out[name] = [_sha(segment),
                             _sha(segment.replace("\r\n", "\n"))]
    return out


def rf_critical_symbol_baseline(file_source_map: dict) -> dict:
    """{설치경로: 소스문자열} → {설치경로: {심볼: [ast_sha, src_sha]}}.

    RF_CRITICAL_QUALIFY_SYMBOLS의 모든 심볼이 없으면 예외(결속 대상 누락)."""
    baseline = {}
    for relpath, wanted in RF_CRITICAL_QUALIFY_SYMBOLS.items():
        source = file_source_map.get(relpath)
        if source is None:
            raise ValueError(f"RF-critical symbol source missing: {relpath}")
        hashes = rf_critical_symbol_hashes(source, wanted)
        missing = sorted(set(wanted) - set(hashes))
        if missing:
            raise ValueError(f"{relpath} RF-critical symbols missing: {missing}")
        baseline[relpath] = hashes
    return baseline


def rf_symbol_diff(prior: dict, current: dict) -> list:
    """이전/현재 RF-critical 심볼 baseline 차이(변경·누락 심볼 목록)."""
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
    """v1 배포 manifest를 영역별 sub-manifest로 분리(주영역 하나씩)."""
    buckets = {d: {} for d in DOMAINS}
    for entry in manifest.get("files", ()):
        buckets[classify(entry["path"])][entry["path"]] = entry["sha256"]
    return {d: {"files": files, "file_count": len(files),
                "aggregate_sha256": _aggregate_sha256(files)}
            for d, files in buckets.items()}


def domain_files_v2(files: dict, domain: str) -> dict:
    """v2 installation files map(path→{sha256, domains}) 중 한 영역의
    path→sha256 map."""
    return {path: entry["sha256"] for path, entry in files.items()
            if domain in (entry.get("domains") or ())}


def _domain_diff(prior_files: dict, current_files: dict) -> dict:
    added = sorted(set(current_files) - set(prior_files))
    removed = sorted(set(prior_files) - set(current_files))
    changed = sorted(k for k in set(prior_files) & set(current_files)
                     if prior_files[k] != current_files[k])
    return {"added": added, "removed": removed, "changed": changed}


def _diff_empty(diff: dict) -> bool:
    return not (diff["added"] or diff["removed"] or diff["changed"])


def _evaluate(prior_by_domain: dict, current_by_domain: dict, *,
              prior_rf_symbols, current_rf_symbols,
              nonbinding_key: str) -> dict:
    diffs = {d: _domain_diff(prior_by_domain[d], current_by_domain[d])
             for d in prior_by_domain}
    rf_files_identical = _diff_empty(diffs[RF])
    operation_identical = _diff_empty(diffs[OPERATION])
    tooling_changed = not _diff_empty(diffs[TOOLING])
    nonbinding_changed = not _diff_empty(diffs[nonbinding_key])

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

    return {
        "eligible": rf_identical and operation_identical,
        "rf_identical": rf_identical,
        "rf_files_identical": rf_files_identical,
        "rf_symbols_identical": rf_symbols_identical,
        "rf_symbols_changed": rf_symbols_changed,
        "operation_identical": operation_identical,
        "tooling_changed": tooling_changed,
        "test_doc_changed": nonbinding_changed,
        "non_binding_changed": nonbinding_changed,
        "blocking_files": blocking,
        "diffs": diffs,
        "prior_domain_sha256": {d: _aggregate_sha256(prior_by_domain[d])
                                for d in prior_by_domain},
        "current_domain_sha256": {d: _aggregate_sha256(current_by_domain[d])
                                  for d in current_by_domain},
    }


def evaluate_reuse(prior_manifest: dict, current_manifest: dict, *,
                   prior_rf_symbols: dict | None = None,
                   current_rf_symbols: dict | None = None) -> dict:
    """v1 배포 manifest 기반 재사용 판정 (rf+심볼 byte-identical AND operation
    byte-identical → eligible; tooling 변경은 bundle 재실행; 문서 무영향)."""
    prior = split_manifest(prior_manifest)
    current = split_manifest(current_manifest)
    return _evaluate({d: prior[d]["files"] for d in DOMAINS},
                     {d: current[d]["files"] for d in DOMAINS},
                     prior_rf_symbols=prior_rf_symbols,
                     current_rf_symbols=current_rf_symbols,
                     nonbinding_key=TEST_DOC)


def evaluate_reuse_v2(prior_files: dict, current_files: dict, *,
                      prior_rf_symbols: dict | None = None,
                      current_rf_symbols: dict | None = None) -> dict:
    """v2 installation files map 기반 재사용 판정 (교차영향 파일은 각 영역에서
    독립 대조). 규칙은 v1과 같다: rf(파일+심볼)·operation byte-identical일 때만
    eligible, tooling 변경은 software bundle 재실행, 비결속은 무영향."""
    keys = (RF, OPERATION, TOOLING, NON_BINDING)
    prior = {d: domain_files_v2(prior_files, d) for d in keys}
    current = {d: domain_files_v2(current_files, d) for d in keys}
    return _evaluate(prior, current, prior_rf_symbols=prior_rf_symbols,
                     current_rf_symbols=current_rf_symbols,
                     nonbinding_key=NON_BINDING)
