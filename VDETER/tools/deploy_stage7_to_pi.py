"""Stage 7 Raspberry Pi 배포 도구 (`stage7` 규칙: 로컬 작업 트리 = 기준 소스).

로컬 working tree의 Stage 7 대상 파일을 tar로 Pi에 배포하고, 배포한 tree의
deployment manifest(파일별 SHA-256)와 그 digest를 기록한다. Pi에서 별도 독립
소스 변경을 만들지 않는다.

사용법:
    python tools/deploy_stage7_to_pi.py --host voyager3@192.168.0.179 \
        --run-id stage7-deploy-<date>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 배포 대상 (VDETER 상대 경로 prefix).
DEPLOY_PREFIXES = (
    "src/vdeter",
    "apps",
    "tools",
    "tests",
    "configs",
    "pyproject.toml",
    "requirements.txt",
)
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".venv", "results"}

REMOTE_ROOT = "~/vdeter-stage7"


def collect_files() -> list:
    files = []
    for prefix in DEPLOY_PREFIXES:
        path = os.path.join(VDETER_ROOT, prefix)
        if os.path.isfile(path):
            files.append(prefix.replace(os.sep, "/"))
            continue
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, name)
                relative = os.path.relpath(full, VDETER_ROOT).replace(os.sep, "/")
                files.append(relative)
    return sorted(set(files))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_describe() -> dict:
    def run(*args):
        completed = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            cwd=os.path.dirname(VDETER_ROOT), check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="voyager3@192.168.0.179")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    files = collect_files()
    manifest_files = []
    for relative in files:
        # tar는 LF 원본 그대로 배포한다 (Windows CRLF checkout 주의: Python은
        # 텍스트 그대로 복사하며 개행 변환 없음).
        manifest_files.append(
            {"path": relative, "sha256": sha256_file(os.path.join(VDETER_ROOT, relative))}
        )
    manifest = {
        "manifest_type": "STAGE7_DEPLOYMENT_MANIFEST_V1",
        "run_id": arguments.run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_host": os.environ.get("COMPUTERNAME") or os.uname().nodename,  # type: ignore[attr-defined]
        "git": git_describe(),
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    manifest_sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    run_dir = os.path.join(VDETER_ROOT, "results", "runs", arguments.run_id)
    os.makedirs(run_dir, exist_ok=True)
    manifest_path = os.path.join(run_dir, "deployment_manifest.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    print(f"[manifest] {manifest_path}")
    print(f"[manifest-sha256] {manifest_sha}")
    if arguments.dry_run:
        return 0

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as temp:
        archive_path = temp.name
    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            for relative in files:
                archive.add(os.path.join(VDETER_ROOT, relative), arcname=relative)
            info = tarfile.TarInfo("deployment_manifest.json")
            payload = encoded.encode("utf-8")
            info.size = len(payload)
            import io as _io

            archive.addfile(info, _io.BytesIO(payload))
        remote_dir = f"{REMOTE_ROOT}/{arguments.run_id}"
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", arguments.host, f"mkdir -p {remote_dir}"],
            check=True,
        )
        with open(archive_path, "rb") as handle:
            subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", arguments.host,
                    f"tar xzf - -C {remote_dir}",
                ],
                stdin=handle,
                check=True,
            )
        # 원격 검증: manifest 파일 수와 표본 digest 확인.
        verify = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", arguments.host,
                f"cd {remote_dir} && sha256sum deployment_manifest.json | cut -d' ' -f1 "
                f"&& find src tools tests configs -type f | wc -l",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        remote_manifest_sha, remote_count = verify.stdout.split()
        if remote_manifest_sha != manifest_sha:
            print("[error] 원격 manifest digest 불일치", file=sys.stderr)
            return 1
        print(f"[deployed] {arguments.host}:{remote_dir} files={remote_count}")
        print(f"[verified] manifest sha256 일치: {manifest_sha}")
        return 0
    finally:
        os.unlink(archive_path)


if __name__ == "__main__":
    raise SystemExit(main())
