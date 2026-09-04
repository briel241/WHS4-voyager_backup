"""배포 tree 실측 manifest — 생성(setup)·검증(run/qualify) (Phase I W4).

host `VDETER/tools/deploy_stage7_to_pi.py`의 tar/scp 없이, **대상 기계의
배포 tree 자체**를 실측해 manifest를 만들고(설정 ③ `var/deploy-manifest.json`),
매 실행 전 재실측 대조로 무결을 검증한다. 이것이 git_dirty=false 결속의
근거다 (README §6 — 미통과면 config 생성 거부).

대상 prefix·제외 규칙은 host 배포 도구에서 **단일 출처로 적재**한다
(importlib — 도구 파일 무수정). git check-ignore 단계는 대상 기계에 git이
없으므로 없다 — 대신 개인키가 애초에 prefix 밖(identity/)에 있도록 배치가
보장한다 (결정 7-⑤).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path

MANIFEST_TYPE = "STAGE7_STANDALONE_DEPLOY_MANIFEST_V1"
MANIFEST_RELPATH = "var/deploy-manifest.json"

#: prefix 밖에 있지만 배포물의 일부인 최상위 파일.
EXTRA_FILES = ("requirements-standalone.lock",)


def _load_host_deploy_rules(repo_root: Path):
    """host 배포 도구의 DEPLOY_PREFIXES/EXCLUDE_DIR_NAMES를 적재 (단일 출처)."""
    path = repo_root / "VDETER" / "tools" / "deploy_stage7_to_pi.py"
    spec = importlib.util.spec_from_file_location(
        "deploy_stage7_to_pi_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.DEPLOY_PREFIXES), set(module.EXCLUDE_DIR_NAMES)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_files(repo_root: Path | str) -> list:
    """배포 대상 파일의 repo 상대 경로 목록 (정렬·중복 제거)."""
    repo_root = Path(repo_root)
    prefixes, exclude_dirs = _load_host_deploy_rules(repo_root)
    files = set()
    for prefix in prefixes:
        base = repo_root / prefix
        if base.is_file():
            files.add(prefix.replace("\\", "/"))
            continue
        if not base.is_dir():
            continue
        stack = [base]
        while stack:
            directory = stack.pop()
            for entry in directory.iterdir():
                if entry.is_dir():
                    if entry.name in exclude_dirs:
                        continue
                    stack.append(entry)
                    continue
                if entry.name.endswith((".pyc", ".pyo")):
                    continue
                files.add(entry.relative_to(repo_root).as_posix())
    for extra in EXTRA_FILES:
        if (repo_root / extra).is_file():
            files.add(extra)
    return sorted(files)


def measure(repo_root: Path | str) -> dict:
    """배포 tree 실측 manifest (파일별 SHA-256)."""
    repo_root = Path(repo_root)
    entries = [{"path": relative,
                "sha256": _sha256_file(repo_root / relative)}
               for relative in collect_files(repo_root)]
    return {
        "manifest_type": MANIFEST_TYPE,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_count": len(entries),
        "files": entries,
    }


def manifest_sha256(manifest: dict) -> str:
    encoded = json.dumps(manifest, ensure_ascii=True, sort_keys=True,
                         indent=1).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_manifest(repo_root: Path | str, out_path: Path | str) -> dict:
    """측정→기록. 반환: {"manifest", "sha256", "relpath"}."""
    manifest = measure(repo_root)
    encoded = json.dumps(manifest, ensure_ascii=True, sort_keys=True,
                         indent=1).encode("utf-8")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_bytes(encoded)
    tmp.replace(out_path)
    return {"manifest": manifest,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "relpath": MANIFEST_RELPATH}


def load_manifest(path: Path | str):
    """기록된 manifest 적재. 반환: (manifest, sha256) 또는 (None, None)."""
    path = Path(path)
    try:
        data = path.read_bytes()
        manifest = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None, None
    if manifest.get("manifest_type") != MANIFEST_TYPE:
        return None, None
    return manifest, hashlib.sha256(data).hexdigest()


def verify(repo_root: Path | str, manifest: dict) -> dict:
    """재실측 대조 — 양방향(누락·불일치·미등재 추가분) 전부 계상.

    pass=True만 git_dirty=false 결속의 근거가 된다 (README §6)."""
    repo_root = Path(repo_root)
    recorded = {entry["path"]: entry["sha256"]
                for entry in manifest.get("files", ())}
    missing, mismatch = [], []
    for relative, expected in sorted(recorded.items()):
        path = repo_root / relative
        if not path.is_file():
            missing.append(relative)
        elif _sha256_file(path) != expected:
            mismatch.append(relative)
    untracked = sorted(set(collect_files(repo_root)) - set(recorded))
    return {
        "pass": not (missing or mismatch or untracked),
        "checked": len(recorded),
        "missing": missing,
        "mismatch": mismatch,
        "untracked": untracked,
    }
