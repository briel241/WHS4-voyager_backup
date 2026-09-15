"""standalone 배포 패키지 생성·전송 — 개발 환경 전용 (Phase I V2, b안).

패키지 = 지정 commit의 **추적 파일**(`git archive`, blob 원본 그대로)
       + 생성된 `VDETER/configs/b210_system/standalone-binding.json`(미커밋 산출물)
       + `deploy-package-manifest.json`(파일별 SHA-256, 자기 기술).
제외 = .git(archive가 애초에 미포함), results/, VDETER/results/, var/,
       identity/, hardware.toml.

SSH/SCP는 **전송 1회**에만 쓴다 — 설치 뒤 setup·qualify·run은 host 없이
동작한다(standalone 기준). 원격 경로가 이미 있으면 거부한다(fail-closed).
전송 후 원격에서 manifest 전수 SHA 대조와 binding source_head 확인을 수행한다.

사용:
    python VDETER/tools/deploy_standalone_to_pi.py --commit 59bcbda \
        --host voyager3@192.168.0.179 --remote-root '~/vdeter-standalone'
        [--dry-run] [--out-dir <로컬 evidence 디렉터리>]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(VDETER_ROOT)
for _entry in (REPO_ROOT,):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

BINDING_RELPATH = "VDETER/configs/b210_system/standalone-binding.json"
PACKAGE_MANIFEST_NAME = "deploy-package-manifest.json"
EXCLUDED_PREFIXES = ("results/", "VDETER/results/", "var/", "identity/",
                     ".git/")
EXCLUDED_FILES = ("hardware.toml",)

REMOTE_VERIFY_SCRIPT = r"""
import hashlib, json, os, sys
root = sys.argv[1]
manifest = json.load(open(os.path.join(root, "deploy-package-manifest.json"),
                          encoding="utf-8"))
missing, mismatch = [], []
for entry in manifest["files"]:
    path = os.path.join(root, entry["path"])
    if not os.path.isfile(path):
        missing.append(entry["path"]); continue
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != entry["sha256"]:
        mismatch.append(entry["path"])
binding = json.load(open(os.path.join(
    root, "VDETER/configs/b210_system/standalone-binding.json"), encoding="utf-8"))
print(json.dumps({
    "checked": len(manifest["files"]), "missing": missing,
    "mismatch": mismatch, "binding_source_head": binding["source_head"],
    "manifest_source_head": manifest["source_head"],
    "pass": (not missing and not mismatch
             and binding["source_head"] == manifest["source_head"]),
}))
"""


class DeployError(RuntimeError):
    """배포 중단 — 부분 산출물을 원격에 남기지 않는다."""


def _git(*args, runner=subprocess.run, binary=False):
    done = runner(["git", "-C", REPO_ROOT, *args], capture_output=True,
                  text=not binary, timeout=120)
    if done.returncode != 0:
        raise DeployError(f"git {' '.join(args)} 실패 (rc={done.returncode})")
    return done.stdout if binary else done.stdout.strip()


def check_preconditions(expected_commit: str, *, runner=subprocess.run) -> dict:
    """HEAD == 지정 commit, 추적 파일 무변경, binding 존재·head 일치·검증."""
    head = _git("rev-parse", "HEAD", runner=runner)
    if not head.startswith(expected_commit):
        raise DeployError(f"HEAD {head[:12]}가 지정 commit {expected_commit}"
                          "이 아닙니다 — 코드 기준을 먼저 고정하십시오")
    modified = _git("status", "--porcelain", "--untracked-files=no",
                    runner=runner)
    if modified:
        raise DeployError("추적 파일에 미커밋 변경이 있습니다 — 패키지는 "
                          f"commit 원본만 담습니다:\n{modified}")
    binding_path = os.path.join(REPO_ROOT, BINDING_RELPATH.replace("/", os.sep))
    from apps.standalone.local_config import (
        ConfigGenerationError,
        load_standalone_binding,
    )
    try:
        binding = load_standalone_binding(binding_path)
    except ConfigGenerationError as exc:
        raise DeployError(str(exc)) from exc
    if binding["source_head"] != head:
        raise DeployError(
            f"binding source_head {binding['source_head'][:12]} != HEAD "
            f"{head[:12]} — make_standalone_binding.py를 재실행하십시오")
    return {"head": head, "binding": binding, "binding_path": binding_path}


def build_package(head: str, binding_path: str, *,
                  runner=subprocess.run) -> tuple:
    """(tar.gz bytes, manifest dict). 내용은 git archive의 blob 원본이다."""
    archive_bytes = _git("archive", "--format=tar", head, runner=runner,
                         binary=True)
    entries = []
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as src, \
            tarfile.open(fileobj=output, mode="w:gz") as dst:
        for member in src.getmembers():
            if not member.isfile():
                continue
            path = member.name
            if path.startswith(EXCLUDED_PREFIXES) or path in EXCLUDED_FILES:
                continue
            data = src.extractfile(member).read()
            entries.append({"path": path,
                            "sha256": hashlib.sha256(data).hexdigest()})
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = member.mode
            info.mtime = member.mtime
            dst.addfile(info, io.BytesIO(data))
        with open(binding_path, "rb") as handle:
            binding_bytes = handle.read()
        entries.append({"path": BINDING_RELPATH,
                        "sha256": hashlib.sha256(binding_bytes).hexdigest()})
        info = tarfile.TarInfo(BINDING_RELPATH)
        info.size = len(binding_bytes)
        info.mtime = int(time.time())
        dst.addfile(info, io.BytesIO(binding_bytes))

        manifest = {
            "manifest_type": "STAGE7_STANDALONE_DEPLOY_PACKAGE_V1",
            "source_head": head,
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()),
            "excluded_prefixes": list(EXCLUDED_PREFIXES),
            "excluded_files": list(EXCLUDED_FILES),
            "file_count": len(entries),
            "files": sorted(entries, key=lambda e: e["path"]),
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=True,
                                     sort_keys=True, indent=1) + "\n"
                          ).encode("utf-8")
        info = tarfile.TarInfo(PACKAGE_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        dst.addfile(info, io.BytesIO(manifest_bytes))
    return output.getvalue(), manifest


def transfer_and_verify(archive: bytes, *, host: str, remote_root: str,
                        runner=subprocess.run, update: bool = False) -> dict:
    """원격 경로 부재 확인 → 생성 → 추출 → 전수 SHA·binding head 검증.

    `update=True`는 기존 설치 root 위에 코드 파일만 덮어쓴다 — 패키지에는
    var/·identity/·hardware.toml이 없으므로 영속 state는 건드리지 않는다
    (결정 15). 코드가 바뀌면 다음 `vdeter setup`이 manifest를 재발급하고
    qualification_invalidated를 보고한다."""
    probe = runner(["ssh", "-o", "BatchMode=yes", host,
                    f"test -e {remote_root} && echo exists || echo absent"],
                   capture_output=True, text=True, timeout=60)
    exists = "exists" in (probe.stdout or "")
    if exists and not update:
        raise DeployError(f"원격 경로가 이미 있습니다: {host}:{remote_root} — "
                          "코드 갱신이면 --update, 아니면 다른 경로를 쓰십시오")
    if update and not exists:
        raise DeployError(f"--update인데 원격 경로가 없습니다: {remote_root}")
    runner(["ssh", "-o", "BatchMode=yes", host, f"mkdir -p {remote_root}"],
           check=True, timeout=60)
    done = runner(["ssh", "-o", "BatchMode=yes", host,
                   f"tar xzf - -C {remote_root}"],
                  input=archive, capture_output=True, timeout=600)
    if done.returncode != 0:
        raise DeployError(f"원격 추출 실패 (rc={done.returncode}): "
                          f"{(done.stderr or b'')[-300:]!r}")
    verify = runner(["ssh", "-o", "BatchMode=yes", host,
                     f"python3 - {remote_root} <<'PYEOF'\n"
                     + REMOTE_VERIFY_SCRIPT + "\nPYEOF\n"],
                    capture_output=True, text=True, timeout=600)
    if verify.returncode != 0:
        raise DeployError(f"원격 검증 실행 실패: {(verify.stderr or '')[-300:]}")
    report = json.loads((verify.stdout or "").strip().splitlines()[-1])
    if not report["pass"]:
        raise DeployError(f"원격 검증 불일치: {report}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True,
                        help="코드 기준 commit (HEAD와 일치해야 함)")
    parser.add_argument("--host", default="voyager3@192.168.0.179")
    parser.add_argument("--remote-root", default="~/vdeter-standalone")
    parser.add_argument("--out-dir", default=None,
                        help="로컬 evidence 디렉터리 (기본 VDETER/results/"
                             "phase-i/deploy-<시각>)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update", action="store_true",
                        help="기존 설치 root 위에 코드 파일만 갱신 "
                             "(var/·identity/·hardware.toml 불변)")
    arguments = parser.parse_args(argv)

    try:
        pre = check_preconditions(arguments.commit)
        archive, manifest = build_package(pre["head"], pre["binding_path"])
    except DeployError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out_dir = arguments.out_dir or os.path.join(
        VDETER_ROOT, "results", "phase-i", f"deploy-{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "package.tar.gz"), "wb") as handle:
        handle.write(archive)
    with open(os.path.join(out_dir, "package_manifest.json"), "w",
              encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=True, sort_keys=True,
                  indent=1)
    archive_sha = hashlib.sha256(archive).hexdigest()
    print(f"[package] head={pre['head'][:12]} files={manifest['file_count']} "
          f"bytes={len(archive)} sha256={archive_sha[:12]}")
    print(f"[evidence] {out_dir}")
    if arguments.dry_run:
        return 0

    try:
        report = transfer_and_verify(archive, host=arguments.host,
                                     remote_root=arguments.remote_root,
                                     update=arguments.update)
    except (DeployError, subprocess.SubprocessError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    with open(os.path.join(out_dir, "remote_verification.json"), "w",
              encoding="utf-8", newline="\n") as handle:
        json.dump({"host": arguments.host, "remote_root": arguments.remote_root,
                   "archive_sha256": archive_sha, **report}, handle,
                  ensure_ascii=True, sort_keys=True, indent=1)
    print(f"[deployed] {arguments.host}:{arguments.remote_root} "
          f"checked={report['checked']} binding_head="
          f"{report['binding_source_head'][:12]} pass={report['pass']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
