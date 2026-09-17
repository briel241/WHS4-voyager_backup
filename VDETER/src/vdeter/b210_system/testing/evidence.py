"""Stage 7 evidence/trace 생성기 (`stage7` §13, §18.4, §20).

각 TC는 top-level bundle 안의 exact relative path
`evidence/trace/TC-STAGE7-0NN.json`을 생성한다. 해당 JSON은 requirement ID,
TC ID, 실제 child artifact path와 SHA-256, expected/actual,
`NOT_RUN|PASS|FAIL` 중 하나의 final status를 가진다.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

#: §18.4 requirement matrix (TC → requirement IDs).
TRACEABILITY_MATRIX = {
    "TC-STAGE7-001": ("STAGE7-REQ-01", "STAGE7-REQ-02", "STAGE7-REQ-06", "STAGE7-REQ-09", "STAGE7-REQ-11"),
    "TC-STAGE7-002": ("STAGE7-REQ-01", "STAGE7-REQ-02", "STAGE7-REQ-06"),
    "TC-STAGE7-003": ("STAGE7-REQ-02", "STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-10", "STAGE7-REQ-11"),
    "TC-STAGE7-004": ("STAGE7-REQ-01", "STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-07", "STAGE7-REQ-08", "STAGE7-REQ-11"),
    "TC-STAGE7-005": ("STAGE7-REQ-01", "STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-07", "STAGE7-REQ-08", "STAGE7-REQ-11"),
    "TC-STAGE7-006": ("STAGE7-REQ-01", "STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-07", "STAGE7-REQ-08", "STAGE7-REQ-11"),
    "TC-STAGE7-007": ("STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-09", "STAGE7-REQ-10"),
    "TC-STAGE7-008": ("STAGE7-REQ-04", "STAGE7-REQ-08"),
    "TC-STAGE7-009": ("STAGE7-REQ-04", "STAGE7-REQ-07", "STAGE7-REQ-08"),
    "TC-STAGE7-010": ("STAGE7-REQ-04", "STAGE7-REQ-07", "STAGE7-REQ-08"),
    "TC-STAGE7-011": ("STAGE7-REQ-04", "STAGE7-REQ-07", "STAGE7-REQ-08"),
    "TC-STAGE7-012": ("STAGE7-REQ-07", "STAGE7-REQ-08", "STAGE7-REQ-10"),
    "TC-STAGE7-013": ("STAGE7-REQ-08", "STAGE7-REQ-11"),
    "TC-STAGE7-014": ("STAGE7-REQ-08", "STAGE7-REQ-10"),
    "TC-STAGE7-015": ("STAGE7-REQ-04", "STAGE7-REQ-07", "STAGE7-REQ-10", "STAGE7-REQ-11"),
    "TC-STAGE7-016": ("STAGE7-REQ-02", "STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-09", "STAGE7-REQ-10"),
    "TC-STAGE7-017": ("STAGE7-REQ-02", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-09", "STAGE7-REQ-10", "STAGE7-REQ-11"),
    "TC-STAGE7-018": ("STAGE7-REQ-01", "STAGE7-REQ-02", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-07", "STAGE7-REQ-08", "STAGE7-REQ-09", "STAGE7-REQ-11", "STAGE7-REQ-12"),
    "TC-STAGE7-019": ("STAGE7-REQ-03", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-10", "STAGE7-REQ-12"),
    "TC-STAGE7-020": ("STAGE7-REQ-02", "STAGE7-REQ-04", "STAGE7-REQ-05", "STAGE7-REQ-06", "STAGE7-REQ-08", "STAGE7-REQ-09", "STAGE7-REQ-10", "STAGE7-REQ-11", "STAGE7-REQ-12"),
}

ALL_TEST_CASES = tuple(sorted(TRACEABILITY_MATRIX))
VALID_STATUSES = ("NOT_RUN", "PASS", "FAIL")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EvidenceBundle:
    """한 run bundle의 evidence/trace 구조를 소유한다."""

    def __init__(self, bundle_root: str, *, run_id: str, judgement_scope: str) -> None:
        self.bundle_root = bundle_root
        self.run_id = run_id
        #: "SOFTWARE_ONLY" | "HARDWARE" | "MIXED" — 실제 실행 범위 표시.
        self.judgement_scope = judgement_scope
        self.trace_dir = os.path.join(bundle_root, "evidence", "trace")
        os.makedirs(self.trace_dir, exist_ok=True)
        self.artifact_dir = os.path.join(bundle_root, "artifacts")
        os.makedirs(self.artifact_dir, exist_ok=True)

    def write_artifact(self, relpath: str, payload) -> dict:
        """child artifact 하나를 기록하고 (path, sha256)를 반환한다."""

        path = os.path.join(self.bundle_root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(payload, (bytes, bytearray)):
            with open(path, "wb") as handle:
                handle.write(payload)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)
                handle.write("\n")
        return {"path": relpath.replace(os.sep, "/"), "sha256": sha256_file(path)}

    def write_trace(
        self,
        test_case_id: str,
        *,
        status: str,
        judgement: str,
        expected,
        actual,
        child_artifacts=(),
        subcases=(),
        root_cause=None,
        cleanup_state="CLEAN",
    ) -> dict:
        if test_case_id not in TRACEABILITY_MATRIX:
            raise ValueError(f"알 수 없는 TC: {test_case_id}")
        if status not in VALID_STATUSES:
            raise ValueError(f"status는 {VALID_STATUSES} 중 하나여야 합니다")
        payload = {
            "trace_type": "STAGE7_TC_TRACE_V1",
            "test_case_id": test_case_id,
            "requirement_ids": list(TRACEABILITY_MATRIX[test_case_id]),
            "run_id": self.run_id,
            "judgement": judgement,  # software-only | hardware-required | mixed
            "judgement_scope": self.judgement_scope,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "expected": expected,
            "actual": actual,
            "subcases": list(subcases),
            "child_artifacts": [dict(item) for item in child_artifacts],
            "final_status": status,
            "root_cause": root_cause,
            "cleanup_state": cleanup_state,
        }
        path = os.path.join(self.trace_dir, f"{test_case_id}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)
            handle.write("\n")
        return payload

    def write_index(self) -> dict:
        """§18.4: 20개 trace JSON과 child digest를 exact ID 순서로 결속한다."""

        entries = []
        for test_case_id in ALL_TEST_CASES:
            path = os.path.join(self.trace_dir, f"{test_case_id}.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    trace = json.load(handle)
                entries.append(
                    {
                        "test_case_id": test_case_id,
                        "path": f"evidence/trace/{test_case_id}.json",
                        "sha256": sha256_file(path),
                        "final_status": trace.get("final_status"),
                        "child_artifacts": trace.get("child_artifacts", []),
                    }
                )
            else:
                entries.append(
                    {
                        "test_case_id": test_case_id,
                        "path": f"evidence/trace/{test_case_id}.json",
                        "sha256": None,
                        "final_status": "NOT_RUN",
                        "child_artifacts": [],
                    }
                )
        index = {
            "index_type": "STAGE7_TRACE_INDEX_V1",
            "run_id": self.run_id,
            "judgement_scope": self.judgement_scope,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        }
        path = os.path.join(self.trace_dir, "index.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        return index

    def write_sha256sums(self) -> str:
        """bundle 전체 파일의 SHA256SUMS를 만든다 (자기 자신 제외)."""

        lines = []
        for root, _dirs, files in os.walk(self.bundle_root):
            for name in sorted(files):
                if name == "SHA256SUMS":
                    continue
                path = os.path.join(root, name)
                relative = os.path.relpath(path, self.bundle_root).replace(os.sep, "/")
                lines.append(f"{sha256_file(path)}  {relative}")
        lines.sort(key=lambda line: line.split("  ", 1)[1])
        output = os.path.join(self.bundle_root, "SHA256SUMS")
        with open(output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        return output


__all__ = (
    "ALL_TEST_CASES",
    "EvidenceBundle",
    "TRACEABILITY_MATRIX",
    "VALID_STATUSES",
    "sha256_file",
)
