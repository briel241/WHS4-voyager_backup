"""설치 기반 manifest — 대상 기계의 실제 설치 파일 실측 (clone-bootstrap v2).

Git 기반 ``standalone-binding.json``을 대체하는 **설치 실측 결속**이다.
``./vdeter setup``이 설치 root의 파일을 읽어 ``var/installation/`` 아래에
네 파일을 만든다:

    installation-manifest.json   전체 파일 map(sha256·size·영역) + 영역 digest
    rf-manifest.json             rf 영역 파일 map + RF-critical 심볼 해시
    operation-manifest.json      operation 영역 파일 map
    tooling-manifest.json        tooling 영역 파일 map

요구사항(사용자 지정): canonical JSON, 파일별 SHA-256, 영역별 최종 SHA-256,
atomic write(파일 fsync → replace → 디렉터리 fsync), 현재 파일 재측정으로
검증 가능, ``var/``는 스캔 제외(순환 해시 없음), 미분류 실행 파일은 최소
operation, 교차영향 파일은 여러 영역에 중복 결속.

결속값 정의
    domain sha256        = sha256(canonical({"files": {path: sha}, ["critical_symbols": …]}))
    installation_sha256  = sha256(canonical({"rf": …, "operation": …, "tooling": …}))
비결속(non_binding) 파일은 inventory에만 있고 어느 digest에도 들어가지 않는다
— Markdown 변경은 자격에 영향이 없다.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from apps.standalone import qualification_domains as domains
from apps.standalone.state import fsync_dir

INSTALLATION_DIR = "var/installation"
INSTALLATION_MANIFEST = "installation-manifest.json"
DOMAIN_MANIFESTS = {
    domains.RF: "rf-manifest.json",
    domains.OPERATION: "operation-manifest.json",
    domains.TOOLING: "tooling-manifest.json",
}
LAUNCHER_ID_FILE = "launcher-id"

INSTALLATION_MANIFEST_TYPE = "STAGE7_STANDALONE_INSTALLATION_MANIFEST_V2"
DOMAIN_MANIFEST_TYPE = "STAGE7_STANDALONE_DOMAIN_MANIFEST_V2"
SCHEMA_VERSION = 2

#: 스캔 제외 디렉터리(이름 단위, 어느 깊이든) — var/·identity/·evidence·
#: 캐시·가상환경·저장소 메타. ``*.egg-info``도 제외한다.
EXCLUDED_DIR_NAMES = frozenset({
    ".git", "var", "identity", "results", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".uv-cache", ".idea", ".vscode", "node_modules",
    "references", "legacy", ".mypy_cache", ".ruff_cache",
})
#: 스캔 제외 파일(설치 상대경로 또는 파일명).
EXCLUDED_FILES = frozenset({
    "hardware.toml",
    "VDETER/configs/stage7/standalone-binding.json",
    ".DS_Store",
    ".git",   # git worktree checkout에서는 `.git`이 gitdir 포인터 **파일**이다
})
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", ".orig", ".rej")


class InstallationError(RuntimeError):
    """installation manifest 부재·손상·변조 — 실행 거부 사유."""


# ------------------------------------------------------------ canonical/sha

def canonical_bytes(obj) -> bytes:
    """canonical JSON: 키 정렬·ASCII·구분자 최소·개행 없음."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_canonical_json_atomic(path: Path, payload: dict) -> bytes:
    """canonical(정렬·ASCII) + 가독 들여쓰기·LF. 임시 파일 fsync → replace →
    디렉터리 fsync. 반환: 기록한 바이트."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1)
            + "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
    return data


# ------------------------------------------------------------ scan

def _excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or name.endswith(".egg-info")


def scan_files(root: Path | str) -> list:
    """설치 파일의 root 상대 경로(posix, 정렬). 제외 규칙 적용."""
    root = Path(root)
    files = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if entry.is_symlink():
                continue  # 설치 결속은 실체 파일만
            if entry.is_dir():
                if _excluded_dir(name):
                    continue
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            rel = entry.relative_to(root).as_posix()
            if rel in EXCLUDED_FILES or name in EXCLUDED_FILES:
                continue
            if name.endswith(EXCLUDED_SUFFIXES):
                continue
            files.append(rel)
    return sorted(files)


def _domain_sha256(files_map: dict, critical_symbols: dict | None) -> str:
    payload = {"files": files_map}
    if critical_symbols is not None:
        payload["critical_symbols"] = critical_symbols
    return sha256_bytes(canonical_bytes(payload))


def _installation_sha256(domain_sha: dict) -> str:
    return sha256_bytes(canonical_bytes(
        {d: domain_sha[d] for d in domains.BOUND_DOMAINS}))


def measure(root: Path | str, *, now=None) -> dict:
    """설치 root 실측 → installation record (기록 전 메모리 형태).

    record["files"] = {path: {"sha256", "size", "domains": [...]}} — 비결속
    파일도 inventory에 포함하되 domains=["non_binding"]."""
    root = Path(root)
    files = {}
    for rel in scan_files(root):
        path = root / rel
        files[rel] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "domains": list(domains.classify_domains(rel)),
        }
    # RF-critical 심볼(qualify_cmd)의 함수 단위 해시 — rf 영역 digest에 포함.
    sources = {}
    for rel in domains.RF_CRITICAL_QUALIFY_SYMBOLS:
        source_path = root / rel
        if source_path.is_file():
            sources[rel] = source_path.read_text(encoding="utf-8")
    try:
        critical_symbols = domains.rf_critical_symbol_baseline(sources)
    except ValueError as exc:
        raise InstallationError(
            f"RF-critical 심볼 결속 불가: {exc} — 설치가 불완전합니다") from exc

    domain_sha = {}
    domain_count = {}
    for domain in domains.BOUND_DOMAINS:
        files_map = domains.domain_files_v2(files, domain)
        domain_count[domain] = len(files_map)
        domain_sha[domain] = _domain_sha256(
            files_map, critical_symbols if domain == domains.RF else None)
    non_binding = sum(1 for e in files.values()
                      if domains.NON_BINDING in e["domains"])
    stamp = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "artifact_type": INSTALLATION_MANIFEST_TYPE,
        "schema_version": SCHEMA_VERSION,
        "generated_utc": stamp,
        "root_marker": "apps/standalone/cli.py",
        "scan": {
            "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
            "excluded_files": sorted(EXCLUDED_FILES),
            "excluded_suffixes": list(EXCLUDED_SUFFIXES),
        },
        "file_count": len(files),
        "files": files,
        "rf_critical_symbols": critical_symbols,
        "domains": {d: {"file_count": domain_count[d], "sha256": domain_sha[d]}
                    for d in domains.BOUND_DOMAINS},
        "non_binding": {"file_count": non_binding},
        "installation_sha256": _installation_sha256(domain_sha),
    }


def domain_manifest(record: dict, domain: str) -> dict:
    files_map = domains.domain_files_v2(record["files"], domain)
    payload = {
        "artifact_type": DOMAIN_MANIFEST_TYPE,
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "generated_utc": record["generated_utc"],
        "file_count": len(files_map),
        "files": files_map,
        "sha256": record["domains"][domain]["sha256"],
        "installation_sha256": record["installation_sha256"],
    }
    if domain == domains.RF:
        payload["critical_symbols"] = record["rf_critical_symbols"]
    return payload


def digests_of(record: dict) -> dict:
    """reference·git_state에 결속하는 digest 묶음."""
    return {
        "installation_sha256": record["installation_sha256"],
        "rf_manifest_sha256": record["domains"][domains.RF]["sha256"],
        "operation_manifest_sha256":
            record["domains"][domains.OPERATION]["sha256"],
        "tooling_manifest_sha256": record["domains"][domains.TOOLING]["sha256"],
    }


def bound_files(record: dict) -> dict:
    """결속 파일(비결속 제외)의 {path: {sha256, domains}} — reference에 기록."""
    return {path: {"sha256": e["sha256"], "domains": list(e["domains"])}
            for path, e in sorted(record["files"].items())
            if domains.NON_BINDING not in e["domains"]}


# ------------------------------------------------------------ write/load

def installation_dir(paths) -> Path:
    return paths.var_dir / "installation"


def write_installation(paths, record: dict) -> dict:
    """4개 manifest를 atomic 기록. 반환: 경로·digest 요약."""
    directory = installation_dir(paths)
    written = {}
    for domain, name in DOMAIN_MANIFESTS.items():
        data = write_canonical_json_atomic(directory / name,
                                           domain_manifest(record, domain))
        written[domain] = {"path": str(directory / name),
                           "file_sha256": sha256_bytes(data)}
    data = write_canonical_json_atomic(directory / INSTALLATION_MANIFEST, record)
    written["installation"] = {"path": str(directory / INSTALLATION_MANIFEST),
                               "file_sha256": sha256_bytes(data)}
    return {"written": written, **digests_of(record),
            "file_count": record["file_count"],
            "domain_file_count": {d: record["domains"][d]["file_count"]
                                  for d in domains.BOUND_DOMAINS}}


def _load_json(path: Path) -> dict:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InstallationError(
            f"installation manifest가 없습니다: {path} — `./vdeter setup`을 "
            "먼저 실행하십시오") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstallationError(
            f"installation manifest 손상(JSON 아님): {path}") from exc
    if not isinstance(payload, dict):
        raise InstallationError(f"installation manifest 구조 아님: {path}")
    return payload


def load_installation(paths) -> dict:
    """var/installation/* 적재 + 자기 정합 검증(변조 시 거부).

    검증: artifact_type/schema, files→영역 digest 재계산 == 기록 digest,
    installation_sha256 == 영역 digest 합성, 영역 manifest 파일의 files·sha가
    installation manifest와 일치."""
    directory = installation_dir(paths)
    record = _load_json(directory / INSTALLATION_MANIFEST)
    if record.get("artifact_type") != INSTALLATION_MANIFEST_TYPE:
        raise InstallationError("installation manifest artifact_type 불일치")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise InstallationError("installation manifest schema_version 불일치")
    files = record.get("files")
    if not isinstance(files, dict) or not files:
        raise InstallationError("installation manifest files 부재")
    symbols = record.get("rf_critical_symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise InstallationError("installation manifest rf_critical_symbols 부재")
    recomputed = {}
    for domain in domains.BOUND_DOMAINS:
        files_map = domains.domain_files_v2(files, domain)
        recomputed[domain] = _domain_sha256(
            files_map, symbols if domain == domains.RF else None)
        recorded = (record.get("domains") or {}).get(domain, {}).get("sha256")
        if recorded != recomputed[domain]:
            raise InstallationError(
                f"installation manifest 변조 의심: {domain} digest 불일치 "
                f"(기록 {str(recorded)[:12]} != 재계산 {recomputed[domain][:12]})")
    if record.get("installation_sha256") != _installation_sha256(recomputed):
        raise InstallationError(
            "installation manifest 변조 의심: installation_sha256 불일치")
    for domain, name in DOMAIN_MANIFESTS.items():
        sub = _load_json(directory / name)
        if sub.get("artifact_type") != DOMAIN_MANIFEST_TYPE:
            raise InstallationError(f"{name} artifact_type 불일치")
        if sub.get("domain") != domain:
            raise InstallationError(f"{name} domain 불일치")
        if sub.get("sha256") != recomputed[domain]:
            raise InstallationError(
                f"{name} digest가 installation manifest와 다릅니다 (변조 의심)")
        if sub.get("files") != domains.domain_files_v2(files, domain):
            raise InstallationError(
                f"{name} files가 installation manifest와 다릅니다 (변조 의심)")
        if domain == domains.RF and sub.get("critical_symbols") != symbols:
            raise InstallationError("rf-manifest critical_symbols 불일치")
    return record


def verify_installation(root: Path | str, record: dict) -> dict:
    """현재 파일 재측정 ↔ 기록 대조 (결속 파일 기준 fail-closed).

    반환: pass, missing, mismatch, untracked(결속 영역의 새 파일), 비결속
    변경 목록(정보), 영역별 digest {recorded, current, match}, 현재
    installation_sha256, 현재 record(재측정)."""
    current = measure(root)
    recorded_files = record["files"]
    missing, mismatch, untracked, nonbinding = [], [], [], []
    for rel, entry in sorted(recorded_files.items()):
        bound = domains.NON_BINDING not in entry["domains"]
        cur = current["files"].get(rel)
        if cur is None:
            (missing if bound else nonbinding).append(rel)
        elif cur["sha256"] != entry["sha256"]:
            (mismatch if bound else nonbinding).append(rel)
    for rel, entry in sorted(current["files"].items()):
        if rel not in recorded_files:
            if domains.NON_BINDING in entry["domains"]:
                nonbinding.append(rel)
            else:
                untracked.append(rel)
    domain_report = {}
    for domain in domains.BOUND_DOMAINS:
        recorded_sha = record["domains"][domain]["sha256"]
        current_sha = current["domains"][domain]["sha256"]
        domain_report[domain] = {"recorded": recorded_sha,
                                 "current": current_sha,
                                 "match": recorded_sha == current_sha}
    digests_match = (record["installation_sha256"]
                     == current["installation_sha256"])
    return {
        "pass": digests_match and not (missing or mismatch or untracked),
        "checked": len(recorded_files),
        "missing": missing,
        "mismatch": mismatch,
        "untracked": untracked,
        "non_binding_changed": nonbinding,
        "domains": domain_report,
        "installation_sha256": current["installation_sha256"],
        "current_record": current,
    }


def describe_mismatch(report: dict) -> str:
    """거부 메시지용 요약(영역별 변경 → 조치 안내)."""
    changed = [d for d in domains.BOUND_DOMAINS
               if not report["domains"][d]["match"]]
    hints = []
    if domains.RF in changed:
        hints.append("rf files changed -> full `vdeter qualify` (RF ladder) "
                     "is required")
    if domains.OPERATION in changed:
        hints.append("operation files changed -> full `vdeter qualify` "
                     "is required under the current policy")
    if domains.TOOLING in changed and not (
            domains.RF in changed or domains.OPERATION in changed):
        hints.append("tooling files changed -> re-run the software "
                     "verification bundle (evidence reuse) before running")
    return ("missing=%s mismatch=%s untracked=%s; %s" % (
        report["missing"][:3], report["mismatch"][:3],
        report["untracked"][:3], "; ".join(hints) or "restore the files"))


# ------------------------------------------------------------ launcher id

def ensure_launcher_id(paths) -> str:
    """설치 식별자(사용자 launcher의 stale 검증용). 없을 때만 생성."""
    import uuid
    path = installation_dir(paths) / LAUNCHER_ID_FILE
    try:
        value = path.read_text(encoding="ascii").strip()
        if value:
            return value
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = uuid.uuid4().hex
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="ascii", newline="\n") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
    return value
