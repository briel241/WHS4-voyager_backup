"""standalone-binding 생성 — **개발 환경 전용** 패키징 도구 (Phase I W4).

배포물이 잘리는 시점의 결속 상수를 `VDETER/configs/b210_system/
standalone-binding.json`에 기록한다 (apps/standalone/README.md §5):

    source_head / source_branch     git 실측 (dirty 트리는 거부)
    baseline_version / _sha256      baseline Markdown 실측 (개발 환경에만 문서 존재)
    frozen_files                    frozen 14파일의 정규화(LF) 내용 SHA-256
                                    (목록은 stage7_frozen_hash_audit와 단일 출처)

대상 기계는 이 파일을 **읽기만** 한다 — setup·run·qualify는 Git과 Markdown을
읽지 않는다 (결정 8, W1 수정 4). qualify의 로컬 frozen 대조와 final builder
`head_provider`가 이 파일을 기준으로 삼는다.

사용: python tools/make_standalone_binding.py \
          [--baseline-doc <경로>] [--out <경로>] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(VDETER_ROOT)
DEFAULT_OUT = os.path.join(VDETER_ROOT, "configs", "b210_system",
                           "standalone-binding.json")
#: docs는 git 밖·저장소 옆에 있다 (개발 환경 배치).
DEFAULT_BASELINE_DOC = os.path.join(
    REPO_ROOT, "..", "docs", "core", "stage7_b210_system_baseline.md")


class BindingError(RuntimeError):
    """생성 거부 — 부분 산출물을 남기지 않는다."""


def _load_frozen_file_list():
    """frozen 목록을 감사 도구에서 단일 출처로 적재 (도구 무수정)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "stage7_frozen_hash_audit.py")
    spec = importlib.util.spec_from_file_location("frozen_audit_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.FROZEN_FILES)


def _git(root, *args, runner=subprocess.run) -> str:
    done = runner(["git", "-C", root, *args],
                  capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise BindingError(
            f"git {' '.join(args)} 실패 (rc={done.returncode}) — 이 도구는 "
            "git이 있는 개발 환경 전용입니다")
    return (done.stdout or "").strip()


def _normalized_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        data = handle.read()
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _baseline_info(doc_path: str) -> tuple:
    if not os.path.isfile(doc_path):
        raise BindingError(
            f"baseline 문서가 없습니다: {doc_path} — --baseline-doc으로 "
            "실제 경로를 지정하십시오")
    with open(doc_path, "rb") as handle:
        data = handle.read()
    sha = hashlib.sha256(data).hexdigest()
    version = None
    for line in data.decode("utf-8", "replace").splitlines()[:8]:
        if line.startswith("문서 버전"):
            version = line.split("`")[1] if "`" in line else None
            break
    if not version:
        raise BindingError("baseline 문서에서 '문서 버전' 헤더를 찾지 못함")
    return version, sha


def build_binding(*, repo_root: str = REPO_ROOT,
                  vdeter_root: str = VDETER_ROOT,
                  baseline_doc: str = DEFAULT_BASELINE_DOC,
                  frozen_files=None,
                  git_runner=subprocess.run) -> dict:
    dirty = _git(repo_root, "status", "--porcelain", runner=git_runner)
    if dirty:
        raise BindingError(
            "worktree가 dirty합니다 — 결속 상수는 clean commit에서만 "
            "생성합니다 (변경을 커밋하거나 되돌린 뒤 재실행)")
    head = _git(repo_root, "rev-parse", "HEAD", runner=git_runner)
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD",
                  runner=git_runner)
    if len(head) != 40:
        raise BindingError(f"git HEAD 형식 비정상: {head!r}")

    version, baseline_sha = _baseline_info(baseline_doc)

    frozen = {}
    for relative in (frozen_files or _load_frozen_file_list()):
        path = os.path.join(vdeter_root, relative.replace("/", os.sep))
        if not os.path.isfile(path):
            raise BindingError(f"frozen 파일이 없습니다: {relative}")
        frozen[relative] = _normalized_sha256(path)

    return {
        "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
        "generated_utc": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_head": head,
        "source_branch": branch,
        "source_worktree_dirty": False,
        "baseline_version": version,
        "baseline_sha256": baseline_sha,
        "frozen_files": frozen,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-doc", default=DEFAULT_BASELINE_DOC)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        binding = build_binding(baseline_doc=arguments.baseline_doc)
    except BindingError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(binding, ensure_ascii=True, sort_keys=True,
                         indent=1) + "\n"
    print(f"[binding] head={binding['source_head'][:12]} "
          f"baseline={binding['baseline_version']} "
          f"frozen={len(binding['frozen_files'])}개")
    if arguments.dry_run:
        return 0
    with open(arguments.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    print(f"[written] {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
