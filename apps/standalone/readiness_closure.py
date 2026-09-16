"""`verify_readiness_installation()` — installation 검증의 세 층 (spec §6.1.1, §10).

qualification의 `installation_manifest.verify_installation()`(전체 installation SHA 일치)은 바꾸지
않는다. readiness는 다음 세 결과를 **별도로** 계산한다.

1. record integrity   저장 manifest의 schema·file entry·domain digest·installation digest·sidecar
                      (domain manifest 4종) 자기 일관성 — `load_installation`이 강제. 실패는 RF 전 거부.
2. readiness-bound    현재 tree의 rf/operation 전체 + readiness 실행 폐포(execution closure) 파일을
   equality           재측정해 stable binding과 exact 대조. 불일치 = stale/준비 재실행.
3. inventory-only     폐포 밖 tooling·시험·개발 도구·non-binding 문서의 추가/수정/삭제는 path/old/new
   difference         SHA와 분류를 evidence로 기록만 한다(readiness RF 거부 없음).

fail-closed 규칙: 분류 불명 파일, bound와 inventory-only에 동시에 걸리는 파일, 폐포 allowlist 자체의
변조(이 모듈은 operation 영역이므로 자기 자신도 bound다)는 readiness-bound에 포함한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from apps.standalone import installation_manifest
from apps.standalone import qualification_domains as domains

READINESS_CLOSURE_POLICY_ID = "VDETER_READINESS_EXECUTION_CLOSURE_V1"

#: 파일 단위로는 tooling이지만 setup/run/status 실행 폐포에 **실제로** 소비되는 파일
#: (production entrypoint·동적 import·child `-m`/script·config/template 소비자·preflight/manifest loader).
#: 정적 import graph만으로 축소하지 않았다 — `run`은 `qualify_cmd.load_active_reference`를 직접 호출하고
#: `installation_manifest`는 `qualification_domains`를 import하며, `setup`은 RF-free migration의
#: bootstrap이다. `bin/vdeter`와 lock 파일은 실행 환경 정의다.
CLOSURE_TOOLING_ALLOWLIST = frozenset({
    "apps/standalone/qualify_cmd.py",
    "apps/standalone/qualification_domains.py",
    "apps/standalone/setup_cmd.py",
    "apps/standalone/installation_manifest.py",
    "apps/standalone/provenance.py",
    "apps/run_profile.py",
    "requirements-standalone.lock",
    "bin/vdeter",
})

#: 새 파일이 여기 아래에 나타나면 분류와 무관하게 bound(fail-closed) — 동적 소비 가능성.
BOUND_PREFIXES = ("apps/", "VDETER/src/", "VDETER/configs/", "VDETER/apps/", "common/", "bin/")
#: 여기 아래의 새 파일은 inventory-only (시험·도구·문서).
INVENTORY_PREFIXES = ("tests/", "VDETER/tests/", "VDETER/tools/", "tools/", "docs/")


def _is_bound(relpath: str, entry_domains) -> bool:
    if relpath in CLOSURE_TOOLING_ALLOWLIST:
        return True
    return domains.RF in entry_domains or domains.OPERATION in entry_domains


def _classification_for_new_file(relpath: str) -> str:
    """새 파일의 처리: 'bound' | 'inventory' | 'unknown'(→ bound, fail-closed)."""
    try:
        entry_domains = domains.classify_domains(relpath)
    except Exception:  # noqa: BLE001 - 분류기 예외 = 분류 불명
        return "unknown"
    if not entry_domains:
        return "unknown"
    if relpath in CLOSURE_TOOLING_ALLOWLIST:
        return "bound"
    if domains.RF in entry_domains or domains.OPERATION in entry_domains:
        return "bound"
    if relpath.startswith(BOUND_PREFIXES):
        # tooling/non_binding으로 분류됐더라도 실행 트리 아래의 새 파일은 fail-closed.
        return "bound" if domains.NON_BINDING not in entry_domains else "inventory"
    if relpath.startswith(INVENTORY_PREFIXES) or domains.NON_BINDING in entry_domains \
            or domains.TOOLING in entry_domains:
        return "inventory"
    return "unknown"


def closure_files(record: dict) -> dict:
    """저장 manifest에서 readiness 실행 폐포 파일 map {relpath: sha256}."""
    return {relpath: entry["sha256"]
            for relpath, entry in sorted((record.get("files") or {}).items())
            if _is_bound(relpath, entry.get("domains") or ())}


def closure_sha256(files_map: dict) -> str:
    payload = json.dumps({"policy_id": READINESS_CLOSURE_POLICY_ID, "files": files_map},
                         ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def critical_symbols_sha256(record: dict) -> str:
    payload = json.dumps(record.get("rf_critical_symbols") or {}, ensure_ascii=True,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_readiness_installation(paths, root, *, record=None, current=None) -> dict:
    """세 층 결과를 별도로 돌려준다.

    반환:
      record_integrity   {"pass", "error"}
      readiness_bound    {"pass", "missing", "mismatch", "added", "unknown",
                          "closure_sha256_recorded", "closure_sha256_current", "count"}
      inventory_only     {"added": [...], "removed": [...], "changed": [...]}
      current_record     재측정 record (호출자 재사용)
    `record`/`current`는 시험·재사용 seam(없으면 적재·재측정)."""
    root = Path(root)
    integrity = {"pass": False, "error": None}
    if record is None:
        try:
            record = installation_manifest.load_installation(paths)
        except installation_manifest.InstallationError as exc:
            integrity["error"] = str(exc)
            return {"policy_id": READINESS_CLOSURE_POLICY_ID, "record_integrity": integrity,
                    "readiness_bound": {"pass": False, "missing": [], "mismatch": [], "added": [],
                                        "unknown": [], "closure_sha256_recorded": None,
                                        "closure_sha256_current": None, "count": 0},
                    "inventory_only": {"added": [], "removed": [], "changed": []},
                    "current_record": None}
    integrity["pass"] = True
    if current is None:
        current = installation_manifest.measure(root)
    recorded_files = record.get("files") or {}
    current_files = current.get("files") or {}
    recorded_closure = closure_files(record)
    missing, mismatch, added, unknown = [], [], [], []
    inv_added, inv_removed, inv_changed = [], [], []
    for relpath, entry in sorted(recorded_files.items()):
        bound = _is_bound(relpath, entry.get("domains") or ())
        cur = current_files.get(relpath)
        if cur is None:
            (missing if bound else inv_removed).append(
                relpath if bound else {"path": relpath, "old": entry["sha256"],
                                       "domains": list(entry.get("domains") or ())})
        elif cur["sha256"] != entry["sha256"]:
            if bound:
                mismatch.append(relpath)
            else:
                inv_changed.append({"path": relpath, "old": entry["sha256"],
                                    "new": cur["sha256"],
                                    "domains": list(entry.get("domains") or ())})
    for relpath, cur in sorted(current_files.items()):
        if relpath in recorded_files:
            continue
        kind = _classification_for_new_file(relpath)
        if kind == "bound":
            added.append(relpath)
        elif kind == "inventory":
            inv_added.append({"path": relpath, "new": cur["sha256"],
                              "domains": list(cur.get("domains") or ())})
        else:
            unknown.append(relpath)
    current_closure = {relpath: cur["sha256"] for relpath, cur in sorted(current_files.items())
                       if _is_bound(relpath, cur.get("domains") or ())
                       or relpath in added or relpath in unknown}
    bound_pass = not (missing or mismatch or added or unknown)
    return {
        "policy_id": READINESS_CLOSURE_POLICY_ID,
        "record_integrity": integrity,
        "readiness_bound": {
            "pass": bound_pass, "missing": missing, "mismatch": mismatch, "added": added,
            "unknown": unknown,
            "closure_sha256_recorded": closure_sha256(recorded_closure),
            "closure_sha256_current": closure_sha256(current_closure),
            "count": len(recorded_closure),
        },
        "inventory_only": {"added": inv_added, "removed": inv_removed, "changed": inv_changed},
        "current_record": current,
    }


def describe_bound_mismatch(report: dict) -> str:
    bound = report["readiness_bound"]
    parts = []
    for key in ("missing", "mismatch", "added", "unknown"):
        if bound.get(key):
            parts.append("%s=%s" % (key, bound[key][:3]))
    return "; ".join(parts) or "no bound difference"
