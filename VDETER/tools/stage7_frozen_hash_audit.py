"""Stage 7 frozen 계층 해시 감사 (0.9.22 — 구 애드혹 스크립트의 도구화).

동결 대상 14개 frozen 파일의 현재 작업 트리 내용을 기준 ref의 blob과
대조한다. 비교는 CRLF→LF 정규화 후 내용 일치(raw blob 해시는 참고
기록). 산출 JSON은 final builder가 `--verification-root`로 결속·실검사
한다(`all_unchanged` 필수 true).

0.9.22 자기결속: 감사 산출물에 head_commit/base_ref/generated_utc를
포함해 "어느 코드 상태의 감사인지"를 산출물 자신이 증언하게 한다
(구판은 무결속이라 임의 시점 감사를 끼워 넣어도 구분 불가 — R1-A 공격
5 지적의 시정).

사용: python tools/stage7_frozen_hash_audit.py --base <ref> --out <json>
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

FROZEN_FILES = (
    "src/vdeter/config/models.py",
    "src/vdeter/config/loader.py",
    "src/vdeter/radio/models.py",
    "src/vdeter/runtime/wiring.py",
    "src/vdeter/runtime/stage5.py",
    "src/vdeter/runtime/endpoint.py",
    "src/vdeter/runtime/phy_bridge.py",
    "src/vdeter/phy/multilink.py",
    "src/vdeter/phy/resampling.py",
    "src/vdeter/link/data_plane.py",
    "src/vdeter/mac/interface.py",
    "src/vdeter/radio/b210/adapter.py",
    "src/vdeter/radio/b210/backend.py",
    "src/vdeter/radio/b210/isolated_backend.py",
)


def _git(root, *args) -> bytes:
    return subprocess.run(
        ("git", "-C", root) + args, check=True, capture_output=True,
    ).stdout


def _normalize(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def audit(root: str, base_ref: str) -> dict:
    # root가 저장소 최상위가 아닐 수 있다(예: 본 worktree는 VDETER/ 접두)
    # — 트리 경로에는 저장소 기준 접두를 붙인다.
    prefix = _git(root, "rev-parse", "--show-prefix").decode().strip()
    files = []
    for rel in FROZEN_FILES:
        tree_path = prefix + rel
        base_blob = _git(
            root, "rev-parse", f"{base_ref}:{tree_path}"
        ).decode().strip()
        base_data = _git(root, "cat-file", "blob", base_blob)
        with open(os.path.join(root, rel), "rb") as handle:
            current_data = handle.read()
        current_blob = (
            _git(root, "hash-object", os.path.join(root, rel)).decode().strip()
        )
        unchanged = _normalize(base_data) == _normalize(current_data)
        files.append({
            "file": rel,
            "base_blob": base_blob,
            "current_blob": current_blob,
            "raw_blob_equal": base_blob == current_blob,
            "unchanged": unchanged,
            "sha256": hashlib.sha256(current_data).hexdigest(),
        })
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    # 0.9.22 (R2 감사): head_commit만으론 dirty 트리에서의 감사를 식별할
    # 수 없다 — 작업 트리 오염 여부도 자기결속.
    dirty = bool(_git(root, "status", "--porcelain").decode().strip())
    return {
        "all_unchanged": all(f["unchanged"] for f in files),
        "worktree_dirty": dirty,
        "comparison": "content-exact after CRLF→LF normalization "
                      "(raw blob hash recorded)",
        "base_ref": base_ref,
        "head_commit": head,
        "generated_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--base", required=True,
                        help="frozen 승인 상태의 git ref (예: 분기점 커밋)")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args(argv)
    result = audit(os.path.abspath(arguments.root), arguments.base)
    os.makedirs(os.path.dirname(os.path.abspath(arguments.out)), exist_ok=True)
    with open(arguments.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    print(f"frozen all_unchanged: {result['all_unchanged']}")
    return 0 if result["all_unchanged"] else 1


if __name__ == "__main__":
    sys.exit(main())
