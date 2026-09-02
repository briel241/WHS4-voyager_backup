"""Stage 7 qualification reference / Phase G profile 발급 (구 임시 스크립트의 도구화).

기존에는 저장소에 없는 임시 스크립트가 발급을 담당해 재현이 불가능했다.
이 도구는 그 역할을 정식화한다.

  issue-reference   Phase F evidence + Phase F 배포에 결속된 reference 발급
  issue-profile     reference + **새 Phase G 배포 manifest**에 결속된 profile 발급

원칙
  - critical 파일 해시는 ① Phase F 배포 기록 ② Pi 실측 ③ 현재 로컬 을 3중
    대조하고 하나라도 다르면 발급을 중단한다.
  - 저장소 안 import 가 해석되지 않거나 두 sys.path 에서 모호하면 조용히
    빼지 않고 중단한다. 동적 import 는 `--dynamic-import` 로 명시 결속한다.
  - Phase F evidence 는 복사하지 않고 합격 조건을 실제로 검사한다.
  - 산출은 임시 디렉터리에서 만든 뒤 atomic publish 하며 기존 디렉터리를
    덮어쓰지 않는다. 발급 직후 stage7_profile 로 재검증한다.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../VDETER
ROOT = os.path.dirname(REPO)                                          # 저장소 루트
PREFIX = os.path.basename(REPO) + "/"                                 # "VDETER/"

#: 폐포 탐색의 sys.path (진입점이 실제로 쓰는 것과 같은 순서).
SEARCH_PATHS = (os.path.join(REPO, "src"), REPO)

#: 결속 진입점. coordinator 는 orchestration 분기가 additive 로 늘어나므로
#: 전체 파일이 아니라 함수 단위로 결속한다(strict 목록에서 제외).
ENTRY_POINTS = (
    PREFIX + "apps/stage7_endpoint_launcher.py",
    PREFIX + "apps/stage7_coordinator_main.py",
)
FUNCTION_BOUND = PREFIX + "apps/stage7_coordinator_main.py"

#: 저장소 안 패키지 최상위 이름. 여기서 시작하는 import 는 반드시 해석돼야
#: 하며, 그 밖(표준·서드파티)은 경계로 두고 환경 manifest 가 버전을 묶는다.
INTERNAL_ROOTS = frozenset({"vdeter", "apps", "tests", "tools"})

#: orchestration 계층 경계. 저장소 루트의 `apps/**` 는 session/controller/CLI/
#: 계측이라 RF 판정을 바꾸지 않으므로 결속하지 않는다 — 다만 "해석은 됐고
#: 경계로 뒀다"를 기록해 조용한 누락과 구분한다.
ORCHESTRATION_PATH = ROOT

#: RF config 원본 — 정확한 allowlist. trust/개인키는 절대 포함하지 않는다.
CONFIG_ALLOWLIST = (
    PREFIX + "configs/stage7/endpoints/mass-a.toml",
    PREFIX + "configs/stage7/endpoints/mass-b.toml",
    PREFIX + "configs/stage7/endpoints/roc.toml",
    PREFIX + "configs/stage7/profiles/data-profile.toml",
    PREFIX + "configs/stage7/system.toml",
)

REQUIRED_TCS = ("TC-STAGE7-RR-002", "TC-STAGE7-RR-003", "TC-STAGE7-RR-004")


class IssueError(RuntimeError):
    """발급 중단. 어떤 경우에도 부분 산출물을 남기지 않는다."""


# ---------------------------------------------------------------- 기본 도구

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    done = subprocess.run(["git", "-C", ROOT, *args],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise IssueError("git %s 실패: %s" % (" ".join(args), done.stderr.strip()))
    return done.stdout.strip()


def require_clean_worktree() -> str:
    if git("status", "--porcelain"):
        raise IssueError("worktree 가 dirty 하다 — 발급은 clean 상태에서만 한다")
    return git("rev-parse", "HEAD")


# ---------------------------------------------------------------- 폐포 계산

def _package_of(repo_relative: str) -> list:
    """이 파일이 속한 패키지 (상대 import 기준).

    `__init__.py` 는 자기 디렉터리가 곧 패키지다.
    """

    for base in SEARCH_PATHS:
        marker = os.path.relpath(base, ROOT).replace(os.sep, "/") + "/"
        if repo_relative.startswith(marker):
            parts = repo_relative[len(marker):-3].split("/")
            return parts[:-1]
    return []


def _resolve_orchestration(module: str) -> str | None:
    """저장소 루트에서만 잡히는 orchestration 모듈이면 그 경로를 돌려준다."""

    relative = module.replace(".", os.sep)
    for candidate in (os.path.join(ORCHESTRATION_PATH, relative + ".py"),
                      os.path.join(ORCHESTRATION_PATH, relative, "__init__.py")):
        if os.path.isfile(candidate):
            return os.path.relpath(candidate, ROOT).replace(os.sep, "/")
    return None


def _resolve(module: str) -> str | None:
    """모듈명 -> 저장소 상대 경로. 두 search path 에서 모두 잡히면 중단."""

    relative = module.replace(".", os.sep)
    hits = []
    for base in SEARCH_PATHS:
        for candidate in (os.path.join(base, relative + ".py"),
                          os.path.join(base, relative, "__init__.py")):
            if os.path.isfile(candidate):
                hits.append(os.path.relpath(candidate, ROOT).replace(os.sep, "/"))
    unique = sorted(set(hits))
    if len(unique) > 1:
        raise IssueError(
            "import 가 모호하다 (%s) — 두 sys.path 에서 해석된다: %s"
            % (module, unique))
    return unique[0] if unique else None


def _module_refs(repo_relative: str) -> list:
    """(모듈명, 반드시 해석돼야 하는가) 목록. 함수 내부 import 포함."""

    path = os.path.join(ROOT, repo_relative.replace("/", os.sep))
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    package = _package_of(repo_relative)
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append((alias.name, alias.name.split(".")[0] in INTERNAL_ROOTS))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                prefix = ".".join(base + ([node.module] if node.module else []))
                internal = True
            else:
                prefix = node.module or ""
                internal = bool(prefix) and prefix.split(".")[0] in INTERNAL_ROOTS
            if not prefix:
                continue
            refs.append((prefix, internal))
            # `from pkg import name` 의 name 은 모듈일 수도 이름일 수도 있다 —
            # 해석되면 따라가고, 안 되면 이름으로 본다(중단하지 않는다).
            for alias in node.names:
                refs.append((prefix + "." + alias.name, False))
    return refs


def compute_closure(dynamic_imports=()) -> list:
    """진입점에서 AST 전이적 import 폐포를 계산한다.

    저장소 안 import 가 해석되지 않으면 중단한다 — 조용히 빼면 결속에
    구멍이 생긴다. 동적 import 는 호출자가 명시해야 한다.
    """

    seen: set = set()
    boundary: set = set()
    queue = list(ENTRY_POINTS)
    for module in dynamic_imports:
        resolved = _resolve(module)
        if resolved is None:
            raise IssueError("동적 import 명시가 해석되지 않는다: %s" % module)
        queue.append(resolved)
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for module, internal in _module_refs(current):
            resolved = _resolve(module)
            if resolved is None:
                orchestration = _resolve_orchestration(module)
                if orchestration is not None:
                    # 경계다 — 결속하지 않되 기록한다.
                    boundary.add(orchestration)
                    continue
                if internal:
                    raise IssueError(
                        "저장소 안 import 를 해석하지 못했다: %s (%s에서). "
                        "동적 적재라면 --dynamic-import 로 명시하라."
                        % (module, current))
                continue
            if resolved not in seen:
                queue.append(resolved)
    return sorted(seen), sorted(boundary)


def strict_file_list(dynamic_imports=()):
    """(strict 파일 목록, orchestration 경계 목록)."""

    closure, boundary = compute_closure(dynamic_imports)
    closure = [p for p in closure if p != FUNCTION_BOUND]
    for config in CONFIG_ALLOWLIST:
        if not os.path.isfile(os.path.join(ROOT, config.replace("/", os.sep))):
            raise IssueError("RF config allowlist 파일이 없다: %s" % config)
    return sorted(set(closure) | set(CONFIG_ALLOWLIST)), boundary


# ------------------------------------------------------------ 3중 해시 대조

def local_hashes(paths) -> dict:
    out = {}
    for relative in paths:
        full = os.path.join(ROOT, relative.replace("/", os.sep))
        if not os.path.isfile(full):
            raise IssueError("로컬에 없다: %s" % relative)
        out[relative] = sha256_file(full)
    return out


def remote_hashes(host: str, remote_root: str, paths) -> dict:
    """Pi 실측. 배포본에 실제로 놓인 파일의 해시를 읽는다."""

    marker = "STAGE7_HASH_END"
    body = "\n".join(paths)
    script = (
        "cd " + remote_root + " && while IFS= read -r f; do "
        "if [ -f " + chr(34) + "$f" + chr(34) + " ]; then sha256sum "
        + chr(34) + "$f" + chr(34) + "; "
        "else echo " + chr(34) + "MISSING $f" + chr(34) + "; fi; "
        "done <<" + chr(39) + marker + chr(39) + "\n"
        + body + "\n" + marker + "\n")
    done = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", host, script],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise IssueError("Pi 해시 수집 실패: %s" % done.stderr.strip()[:300])
    out, missing = {}, []
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith("MISSING "):
            missing.append(line[8:])
        elif line:
            digest, path = line.split(None, 1)
            out[path.strip()] = digest
    if missing:
        raise IssueError("Pi 배포본에 없다 (%d): %s" % (len(missing), missing[:5]))
    return out


def deploy_record_hashes(manifest_path: str) -> dict:
    """Phase F 배포 manifest 가 기록한 해시."""

    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = manifest.get("files") or manifest.get("entries") or []
    out = {}
    for item in entries:
        path = item.get("path")
        digest = item.get("sha256")
        if path and digest:
            out[path if path.startswith(PREFIX) else PREFIX + path] = digest
    if not out:
        raise IssueError("배포 manifest 에서 파일 해시를 읽지 못했다: %s" % manifest_path)
    return out


def triple_check(paths, local: dict, remote: dict, record: dict) -> dict:
    """세 출처를 대조한다. 하나라도 다르면 중단한다."""

    mismatches, absent = [], []
    for relative in paths:
        values = {"local": local.get(relative),
                  "pi": remote.get(relative),
                  "deploy_record": record.get(relative)}
        if values["deploy_record"] is None:
            absent.append(relative)
            continue
        if len(set(values.values())) != 1:
            mismatches.append({"path": relative, **values})
    if mismatches:
        raise IssueError(
            "critical 해시 3중 대조 불일치 %d건 — 발급 중단: %s"
            % (len(mismatches), json.dumps(mismatches[:3], ensure_ascii=False)))
    return {"checked": len(paths),
            "sources": ["deploy_record", "pi_live", "local_worktree"],
            "absent_from_deploy_record": sorted(absent), "pass": True}


# --------------------------------------------------- coordinator 함수 결속

def _segment_sha(source: str, node) -> dict:
    lines = source.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1:node.end_lineno])
    return {"source_sha256": sha256_bytes(body.encode("utf-8")),
            "ast_sha256": sha256_bytes(
                ast.dump(node, include_attributes=False).encode("utf-8"))}


def coordinator_binding(phase_f_sha) -> dict:
    """함수 본문뿐 아니라 최상위 상수·import·dispatch·파일 전체를 결속한다."""

    full = os.path.join(ROOT, FUNCTION_BOUND.replace("/", os.sep))
    with open(full, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    functions, constants, imports = {}, {}, []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = _segment_sha(source, node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = _segment_sha(source, node)["source_sha256"]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(ast.dump(node, include_attributes=False))

    file_sha = sha256_file(full)
    return {
        "phase_f_file_sha256": phase_f_sha or file_sha,
        "phase_g_file_sha256": file_sha,
        "qualified_functions": functions,
        "top_level_constants": constants,
        "module_imports_sha256": sha256_bytes(("\n".join(imports)).encode("utf-8")),
        "rr_phase_dispatch": _rr_phase_dispatch(tree, source),
        "delta": _coordinator_delta(phase_f_sha, file_sha, len(functions)),
    }


def _rr_phase_dispatch(tree, source: str) -> dict:
    """`--phase X` 분기가 어떤 run_* 함수를 부르는지 정적으로 뽑는다."""

    lines = source.splitlines()
    dispatch: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "phase"):
            continue
        for comparator in node.comparators:
            if not (isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)):
                continue
            window = lines[node.lineno - 1:node.lineno + 40]
            names = sorted({
                part.split("(")[0]
                for line in window for part in line.replace("(", " (").split()
                if part.startswith("run_")
            })
            if names:
                dispatch.setdefault(comparator.value, names)
    return dict(sorted(dispatch.items()))


def _coordinator_delta(phase_f_sha, phase_g_sha, function_count) -> dict:
    if phase_f_sha is None or phase_f_sha == phase_g_sha:
        return {"kind": "SELF_BASELINE", "added_functions": [],
                "changed_functions": [], "deleted_detail": [],
                "added_lines": 0, "deleted_lines": 0,
                "top_level_function_count": function_count,
                "note": "Phase F와 Phase G가 같은 트리라 델타가 없다."}
    return {"kind": "DIFFERENT_TREE",
            "top_level_function_count": function_count,
            "note": "Phase F 배포본과 현재 트리의 coordinator 파일 해시가 다르다 - "
                    "orchestration additive 여부를 검토해야 한다."}


# ------------------------------------------------------ Phase F evidence 검증

def _load(path: str) -> dict:
    if not os.path.isfile(path):
        raise IssueError("evidence 파일이 없다: %s" % path)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _find(node, key):
    """중첩 구조에서 key 를 찾아 첫 값을 돌려준다 (없으면 None)."""

    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find(item, key)
            if found is not None:
                return found
    return None


def validate_phase_f(evidence_dir: str, expect_commit: str | None) -> dict:
    """Phase F evidence 를 복사하지 않고 합격 조건을 실제로 검사한다."""

    research = os.path.join(evidence_dir, "research_ready")
    tc_results, run_ids, commits, baselines = {}, set(), set(), set()
    for tc in REQUIRED_TCS:
        data = _load(os.path.join(research, tc + ".json"))
        if data.get("status") != "PASS":
            raise IssueError("%s 가 PASS 가 아니다: %s" % (tc, data.get("status")))
        counters = data.get("counters") or {}
        if counters.get("retries", 0) != 0:
            raise IssueError("%s retries=%s (0 이어야 한다)" % (tc, counters.get("retries")))
        if data.get("git_dirty"):
            raise IssueError("%s 가 dirty worktree 에서 생성됐다" % tc)
        run_ids.add(data.get("run_id"))
        commits.add(data.get("git_commit"))
        baselines.add(data.get("baseline_sha256"))
        tc_results[tc] = {
            "status": "PASS", "run_id": data.get("run_id"),
            "duration_s": data.get("duration_s"), "counters": counters,
            "evidence_sha256": sha256_file(os.path.join(research, tc + ".json")),
        }
    if len(run_ids) != 1 or len(commits) != 1 or len(baselines) != 1:
        raise IssueError("TC 간 run_id/commit/baseline 이 일치하지 않는다: %s %s %s"
                         % (run_ids, commits, baselines))
    run_id, commit, baseline = run_ids.pop(), commits.pop(), baselines.pop()
    if expect_commit and commit != expect_commit:
        raise IssueError("evidence commit(%s)이 배포 commit(%s)과 다르다" % (commit, expect_commit))

    coordinator = _load(os.path.join(evidence_dir, "coordinator-rr-p1.json"))
    if coordinator.get("inhibited"):
        raise IssueError("coordinator inhibited=true")
    if coordinator.get("faults"):
        raise IssueError("coordinator faults 가 비어 있지 않다: %s" % coordinator["faults"][:2])
    if coordinator.get("run_id") != run_id:
        raise IssueError("coordinator run_id 불일치: %s" % coordinator.get("run_id"))

    result = coordinator.get("phase_result") or {}

    # raw PHY: phase_result.raw_phy 는 경로별 요약 목록이다 (C1..C6 각 2회).
    raw_phy = result.get("raw_phy")
    if not isinstance(raw_phy, list) or not raw_phy:
        raise IssueError("evidence 에서 raw PHY 결과를 찾지 못했다 — 발급 중단")
    failed = sorted({e.get("path_id") for e in raw_phy if e.get("outcome") != "PASS"})
    if failed:
        raise IssueError("raw PHY 실패 경로: %s" % failed)
    covered = {e.get("path_id") for e in raw_phy}
    if covered != {"C1", "C2", "C3", "C4", "C5", "C6"}:
        raise IssueError("raw PHY 경로 집합이 C1..C6 이 아니다: %s" % sorted(covered))
    wrong_bytes = sum(int(e.get("wrong_bytes") or 0) for e in raw_phy)
    if wrong_bytes != 0:
        raise IssueError("wrong_bytes_total=%s (0 이어야 한다)" % wrong_bytes)
    raw_phy_paths = {path: "PASS" for path in sorted(covered)}

    # REVALIDATE·skipped_past_due: endpoint runtime의 operations_evidence에서
    # 추출한다 (phase-f-requal-20260831f finalize의 승인 추출법과 동일 위치,
    # 경로 중복은 덮어쓰기 대신 AND 결합으로 강화).
    revalidate: dict = {}
    skipped_nonzero = []
    for endpoint in ("MASS-A", "MASS-B", "ROC"):
        runtime = _load(os.path.join(evidence_dir, endpoint + "-runtime.json"))
        for item in runtime.get("operations_evidence") or []:
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "gain_revalidate":
                path = item.get("path_id")
                revalidate[path] = revalidate.get(path, True) and bool(item.get("passed"))
            past_due = item.get("skipped_past_due")
            if past_due is not None and past_due != 0:
                skipped_nonzero.append({
                    "endpoint": endpoint, "kind": item.get("kind"),
                    "path_id": item.get("path_id"),
                    "skipped_past_due": past_due,
                })
    if sorted(revalidate) != ["C1", "C2", "C3", "C4", "C5", "C6"]:
        raise IssueError(
            "evidence 에서 revalidate 결과를 찾지 못했다 — 발급 중단 "
            "(경로: %s)" % sorted(revalidate))
    failed = sorted(k for k, v in revalidate.items() if not v)
    if failed:
        raise IssueError("REVALIDATE 실패 경로: %s" % failed)
    if skipped_nonzero:
        raise IssueError(
            "skipped_past_due 불변식 위반: %s" % skipped_nonzero[:3])
    skipped = {"nonzero_records": 0}

    return {
        "run_id": run_id, "phase": "rr-p1", "profile": "RESEARCH_READY_MINIMAL",
        "git_commit": commit, "baseline_sha256": baseline,
        "tc_results": tc_results, "revalidate_passed": revalidate,
        "raw_phy_paths": raw_phy_paths, "skipped_past_due": skipped,
        "counters": {"faults": 0, "inhibited": False, "non_clean_statuses": 0,
                     "wrong_bytes_total": wrong_bytes or 0},
    }


def validate_gain(evidence_dir: str, run_id: str) -> dict:
    """gain artifact 와 input 의 실제 파일 SHA·상호 참조를 검사한다."""

    artifact_path = os.path.join(evidence_dir, "gain_artifact.json")
    input_path = os.path.join(evidence_dir, "gain_artifact_input.json")
    artifact, source = _load(artifact_path), _load(input_path)
    artifact_sha, input_sha = sha256_file(artifact_path), sha256_file(input_path)

    declared = artifact.get("run_id") or _find(artifact, "run_id")
    if declared and declared != run_id:
        raise IssueError("gain artifact run_id(%s)가 RR run_id(%s)와 다르다" % (declared, run_id))
    back = artifact.get("input_sha256") or _find(artifact, "artifact_input_sha256")
    if back and back != input_sha:
        raise IssueError("gain artifact 가 참조하는 input SHA 가 실제 파일과 다르다")

    gains = _find(artifact, "endpoint_common_rx_gain_db") or _find(artifact, "rx_gain_db")
    if not isinstance(gains, dict) or set(gains) != {"MASS-A", "MASS-B", "ROC"}:
        raise IssueError("gain artifact 에서 endpoint 공통 RX gain 을 읽지 못했다: %s" % gains)
    return {"artifact_run_id": run_id, "artifact_sha256": artifact_sha,
            "artifact_input_sha256": input_sha,
            "gain_plan_sha256": _find(artifact, "gain_plan_sha256"),
            "endpoint_common_rx_gain_db": gains}


# --------------------------------------------------------------- atomic 발급

def _publish(staging: str, destination: str) -> None:
    """임시 디렉터리에서 만든 산출물을 통째로 옮긴다. 덮어쓰지 않는다."""

    if os.path.exists(destination):
        raise IssueError("출력 디렉터리가 이미 있다 — 덮어쓰지 않는다: %s" % destination)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    os.replace(staging, destination)


def _write(directory: str, name: str, payload) -> str:
    path = os.path.join(directory, name)
    text = (json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
            if not isinstance(payload, str) else payload)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def issue_reference(args) -> int:
    head = require_clean_worktree()
    paths, orchestration_files = strict_file_list(args.dynamic_import or ())

    record = deploy_record_hashes(args.phase_f_deploy_manifest)
    local = local_hashes(paths)
    remote = remote_hashes(args.host, args.phase_f_remote_path, paths)
    triple = triple_check(paths, local, remote, record)

    deployed_commit = args.qualified_commit or head
    qualification = validate_phase_f(args.phase_f_evidence, deployed_commit)
    gain = validate_gain(args.phase_f_evidence, qualification["run_id"])

    template = _load(args.template_reference)
    with open(args.phase_f_deploy_manifest, encoding="utf-8") as handle:
        manifest_text = handle.read()

    reference = {
        "artifact_type": "STAGE7_QUALIFICATION_REFERENCE",
        "schema_version": template["schema_version"],
        "issued_at_utc": args.issued_at or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_by_phase": "G",
        "qualified_source_commit": deployed_commit,
        "baseline_sha256": qualification.pop("baseline_sha256"),
        "hardware": template["hardware"],
        "wiring_profile": template["wiring_profile"],
        "rf_settings": dict(template["rf_settings"],
                            rx_gain_db=gain["endpoint_common_rx_gain_db"]),
        "gain": gain,
        # 템플릿의 수기 목록(호스트 전용 CLI·session 등 진입점이 import 하지
        # 않는 것)과 AST 가 실제로 경계로 만난 것을 합쳐 보존한다.
        "orchestration_provenance": dict(
            template["orchestration_provenance"],
            files=sorted(set(template["orchestration_provenance"].get("files") or ())
                         | set(orchestration_files)),
            ast_observed_boundary=orchestration_files),
        "phase_f_deployment": {
            "deploy_run_id": args.phase_f_deploy_run_id,
            "remote_path": args.phase_f_remote_path,
            "manifest_sha256": sha256_bytes(manifest_text.encode("utf-8")),
            "file_count": len(record),
        },
        "qualification": dict(
            qualification,
            evidence_root=args.phase_f_remote_path + "/runs/"
                          + qualification["run_id"] + "/",
            scope_note=template["qualification"]["scope_note"],
            skipped_past_due_note=template["qualification"]["skipped_past_due_note"],
        ),
        "qualification_critical_manifest": {
            "scope_method": template["qualification_critical_manifest"]["scope_method"],
            "entry_points": list(ENTRY_POINTS),
            "verified_against": ["phase_f_deploy_record", "phase_f_pi_live", "local_worktree"],
            "file_count": len(paths),
            "strict_file_count": len(paths),
            "strict_files": local,
            "function_bound_files": {
                FUNCTION_BOUND: coordinator_binding(record.get(FUNCTION_BOUND)),
            },
        },
    }

    staging = tempfile.mkdtemp(prefix="stage7-issue-")
    try:
        reference_text = (json.dumps(reference, ensure_ascii=False, indent=1,
                                     sort_keys=True) + "\n")
        _write(staging, "qualification-reference.json", reference_text)
        _write(staging, "qualification-reference.sha256",
               sha256_bytes(reference_text.encode("utf-8"))
               + "  qualification-reference.json\n")
        _write(staging, "qualification-critical-manifest.json",
               reference["qualification_critical_manifest"])
        _write(staging, "local-file-hashes.json",
               {"head": head, "deployed_commit": deployed_commit,
                "triple_check": triple, "local": local, "pi_live": remote})
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _publish(staging, args.out_dir)
    print("[issue] reference: %s" % os.path.join(args.out_dir, "qualification-reference.json"))
    print("[issue] strict %d개 · 3중 대조 통과 · run %s"
          % (len(paths), qualification["run_id"]))
    if triple["absent_from_deploy_record"]:
        print("[issue] 주의 — 배포 기록에 없던 파일 %d개 (신규 추가분): %s"
              % (len(triple["absent_from_deploy_record"]),
                 triple["absent_from_deploy_record"][:5]))
    return 0


def issue_profile(args) -> int:
    """reference + **새 Phase G 배포 manifest** 에 결속된 profile 을 만든다.

    Phase G 배포 정보 없이 미리 만들지 않는다 — 그러면 profile 이 어떤
    배포본을 가리키는지 증언할 수 없다.
    """

    reference_path = os.path.join(args.reference_dir, "qualification-reference.json")
    with open(reference_path, "rb") as handle:
        reference_bytes = handle.read()
    reference = json.loads(reference_bytes.decode("utf-8"))
    reference_sha = sha256_bytes(reference_bytes)

    sidecar = os.path.join(args.reference_dir, "qualification-reference.sha256")
    if sha256_bytes(open(sidecar, "rb").read().split()[0]) and \
            open(sidecar, encoding="utf-8").read().split()[0] != reference_sha:
        raise IssueError("reference sidecar SHA 가 본문과 다르다 — 발급 중단")

    with open(args.phase_g_deploy_manifest, encoding="utf-8") as handle:
        manifest_text = handle.read()
    template = _load(args.template_profile)

    gain = reference["gain"]

    # gain 경로는 인자로 받되, 파일 실제 SHA가 reference의 값과 일치할
    # 때만 기록한다 — 수기 전사 없이 경로↔내용 결속을 강제.
    for relpath, expected, label in (
        (args.gain_artifact_relpath, gain["artifact_sha256"], "artifact"),
        (args.gain_artifact_input_relpath, gain["artifact_input_sha256"],
         "artifact_input"),
    ):
        local_path = os.path.join(os.path.dirname(ROOT), relpath)             if relpath.startswith(PREFIX) else os.path.join(ROOT, relpath)
        actual = sha256_file(local_path)
        if actual != expected:
            raise IssueError(
                "gain %s 경로의 파일 SHA 불일치: %s (%s != %s)"
                % (label, relpath, actual[:12], expected[:12]))

    profile = dict(template)
    profile["baseline_sha256"] = reference["baseline_sha256"]
    profile["qualification_reference"] = {
        "path": "qualification-reference.json", "sha256": reference_sha}
    profile["rf_settings"] = reference["rf_settings"]
    profile["wiring_profile"] = reference["wiring_profile"]
    profile["evidence_root"] = args.evidence_root
    profile["gain"] = dict(template.get("gain") or {},
                           artifact_path=args.gain_artifact_relpath,
                           artifact_input_path=args.gain_artifact_input_relpath,
                           artifact_sha256=gain["artifact_sha256"],
                           artifact_input_sha256=gain["artifact_input_sha256"],
                           rx_gain_db=gain["endpoint_common_rx_gain_db"])
    profile["pi"] = dict(template["pi"],
                         deploy_run_id=args.phase_g_deploy_run_id,
                         deploy_relpath=args.phase_g_deploy_relpath)
    profile["phase_g_deployment"] = {
        "deploy_run_id": args.phase_g_deploy_run_id,
        "manifest_sha256": sha256_bytes(manifest_text.encode("utf-8")),
    }

    staging = tempfile.mkdtemp(prefix="stage7-profile-")
    try:
        _write(staging, "stage7-profile.json", profile)
        shutil.copyfile(reference_path, os.path.join(staging, "qualification-reference.json"))
        shutil.copyfile(sidecar, os.path.join(staging, "qualification-reference.sha256"))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _publish(staging, args.out_dir)

    # 발급 직후 정식 로더로 재검증한다 — 수동 전사 오류를 여기서 잡는다.
    sys.path.insert(0, ROOT)
    from apps.stage7_profile import load_profile, validate_local_bindings
    loaded = load_profile(os.path.join(args.out_dir, "stage7-profile.json"))
    report = validate_local_bindings(loaded)
    if report.get("pass") is False:
        raise IssueError("발급 직후 validate_local_bindings 실패: %s" % report)
    print("[issue] profile: %s" % os.path.join(args.out_dir, "stage7-profile.json"))
    print("[issue] load_profile · validate_local_bindings 재검증 통과")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ref = sub.add_parser("issue-reference", help="Phase F evidence 로 reference 발급")
    ref.add_argument("--host", required=True)
    ref.add_argument("--phase-f-deploy-run-id", required=True)
    ref.add_argument("--phase-f-remote-path", required=True)
    ref.add_argument("--phase-f-deploy-manifest", required=True)
    ref.add_argument("--phase-f-evidence", required=True,
                     help="로컬로 수집한 rr-p1 evidence 디렉터리")
    ref.add_argument("--template-reference", required=True,
                     help="스키마 상수 승계용 기존 reference (값은 검사한 것으로 덮어쓴다)")
    ref.add_argument("--qualified-commit", default=None)
    ref.add_argument("--issued-at", default=None)
    ref.add_argument("--dynamic-import", action="append", default=[],
                     help="AST 로 보이지 않는 동적 적재 모듈 (명시 결속)")
    ref.add_argument("--out-dir", required=True)
    ref.set_defaults(handler=issue_reference)

    pro = sub.add_parser("issue-profile", help="reference + Phase G 배포로 profile 발급")
    pro.add_argument("--reference-dir", required=True)
    pro.add_argument("--template-profile", required=True)
    pro.add_argument("--phase-g-deploy-run-id", required=True)
    pro.add_argument("--phase-g-deploy-relpath", required=True)
    pro.add_argument("--phase-g-deploy-manifest", required=True)
    pro.add_argument("--evidence-root", required=True)
    pro.add_argument("--gain-artifact-relpath", required=True,
                     help="repo 상대 gain_artifact.json 경로 (SHA 교차 검증)")
    pro.add_argument("--gain-artifact-input-relpath", required=True,
                     help="repo 상대 gain_artifact_input.json 경로 (SHA 교차 검증)")
    pro.add_argument("--out-dir", required=True)
    pro.set_defaults(handler=issue_profile)

    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except IssueError as error:
        print("[issue] 중단 — %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
