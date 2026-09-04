"""참고용 provenance — Git·기준선 문서 pin (clone-bootstrap v2, informational only).

대상 기계의 setup·qualify·run·results는 **Git을 실행하지 않고**, 기준선
Markdown을 읽지 않는다. 이 모듈이 제공하는 값은 전부 *참고 기록*이며
qualification 유효성 판정이나 실행 차단에 쓰이지 않는다:

- :func:`read_git_provenance` — ``.git/HEAD``와 ref 파일을 **파일로만** 읽어
  commit/branch를 추정한다. git 명령 호출 없음. ``.git`` 부재·형식 불명·
  worktree 간접 참조 실패 등 어떤 경우에도 예외 없이 ``None``이다.
- :data:`STAGE7_BASELINE_PIN` — Stage 7 RR evidence 계약(coordinator
  ``--baseline-sha256`` 대조, final builder 승인 개정 사슬)이 요구하는 기준선
  버전·SHA의 **커밋된 상수**. 문서 실물은 host 감사 환경에서만 대조한다.
  installation digest를 이 자리에 넣지 않는다 (Git digest ↔ installation
  digest 위장 금지).
- :func:`legacy_binding_provenance` — 구판 패키지에 ``standalone-binding.json``
  이 남아 있으면 그 내용을 참고로 기록한다. 없어도 무방하다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Stage 7 기준선 문서 pin (0.9.23 — Phase I 자격에서 실측 일치 확인됨).
#: 개정 시 개발자가 final builder APPROVED_REVISION_CHAIN과 함께 갱신한다.
STAGE7_BASELINE_PIN = {
    "version": "0.9.23",
    "sha256": ("2a87c1d0f7bee78c13def8b153745ecbe42ac7dcf701d69abffc9141bc"
               "9cc026"),
    "document": "docs/core/stage7_b210_system_baseline.md",
    "note": ("committed provenance constant for the Stage 7 RR evidence "
             "contract; the document is not read on the target machine and "
             "this value is not a qualification-critical comparison input"),
}

PROVENANCE_NOTE = ("informational only — not used for qualification validity "
                   "or execution gating; git absence never blocks execution")

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _git_dir(root: Path):
    dotgit = root / ".git"
    if dotgit.is_dir():
        return dotgit
    if dotgit.is_file():  # worktree: "gitdir: <path>"
        try:
            text = dotgit.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            if not target.is_absolute():
                target = (root / target).resolve()
            return target if target.is_dir() else None
    return None


def _resolve_ref(git_dir: Path, ref: str):
    """refs/heads/x → commit (loose ref 또는 packed-refs). 실패는 None."""
    candidates = [git_dir / ref]
    # worktree의 gitdir는 <common>/worktrees/<name> — refs는 commondir에 있다.
    common = git_dir / "commondir"
    if common.is_file():
        try:
            common_dir = Path(common.read_text(encoding="utf-8").strip())
            if not common_dir.is_absolute():
                common_dir = (git_dir / common_dir).resolve()
            candidates.append(common_dir / ref)
        except OSError:
            pass
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _HEX40.match(value):
            return value
    for base in [git_dir] + [c.parent for c in candidates[1:]]:
        packed = base / "packed-refs"
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _HEX40.match(parts[0]):
                    return parts[0]
        except OSError:
            continue
    return None


def read_git_provenance(root: Path | str):
    """`.git` 파일 읽기만으로 {commit, branch, source} 추정. 없으면 None."""
    try:
        root = Path(root)
        git_dir = _git_dir(root)
        if git_dir is None:
            return None
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            commit = _resolve_ref(git_dir, ref)
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif _HEX40.match(head):
            commit, branch = head, None
        else:
            return None
        return {"commit": commit, "branch": branch,
                "source": ".git files (no git command)",
                "dirty": None}
    except Exception:  # noqa: BLE001 — 참고 기록은 어떤 실패도 실행을 막지 않는다
        return None


def legacy_binding_provenance(root: Path | str):
    """구판 standalone-binding.json이 있으면 참고 기록으로 돌려준다."""
    try:
        path = (Path(root) / "VDETER" / "configs" / "stage7"
                / "standalone-binding.json")
        if not path.is_file():
            return None
        binding = json.loads(path.read_bytes().decode("utf-8"))
        if binding.get("artifact_type") != "STAGE7_STANDALONE_BINDING_V1":
            return None
        return {"source_head": binding.get("source_head"),
                "source_branch": binding.get("source_branch"),
                "generated_utc": binding.get("generated_utc"),
                "baseline_version": binding.get("baseline_version"),
                "note": "legacy git-based binding; informational only"}
    except Exception:  # noqa: BLE001
        return None


def build_provenance(root: Path | str) -> dict:
    """reference·git_state에 담는 참고 provenance 묶음."""
    return {
        "note": PROVENANCE_NOTE,
        "git": read_git_provenance(root),
        "legacy_binding": legacy_binding_provenance(root),
        "stage7_baseline": dict(STAGE7_BASELINE_PIN),
    }
