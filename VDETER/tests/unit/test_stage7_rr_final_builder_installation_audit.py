"""stage7_rr_final_builder — v1(git HEAD) / v2(installation digest) 감사 병행 계약.

v2(standalone clone-bootstrap): verification root의 frozen-hash-audit.json이
`installation_sha256`을 담으면 head_provider가 돌려주는 현행 installation
digest와 대조한다. v1(`head_commit`) 경로는 그대로다. 두 경로는 필드명으로
구분되며 서로 위장할 수 없다(64-hex ≠ 40-hex, 필드 상이).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_VDETER = _HERE.parents[2]
_BUILDER = _VDETER / "tools" / "stage7_rr_final_builder.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "stage7_rr_final_builder_audit_test", _BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_evidence(root: Path, run_id: str, tcs, *, baseline_sha="2a" * 32):
    research = root / run_id / "evidence" / "research_ready"
    research.mkdir(parents=True)
    evidence = research.parent
    (evidence / f"coordinator-{run_id}.json").write_text("{}")
    for member in ("MASS-A", "MASS-B", "ROC"):
        (evidence / f"{member}-runtime.json").write_text("{}")
    index = {"profile": "RESEARCH_READY_MINIMAL", "entries": {}}
    for tc in tcs:
        payload = {
            "artifact_type": "STAGE7_RESEARCH_READY_EVIDENCE_V1",
            "profile": "RESEARCH_READY_MINIMAL", "tc_id": tc,
            "baseline_version": "0.9.23", "baseline_sha256": baseline_sha,
            "git_commit": None, "git_dirty": None,
            "hardware": {"usb_devices": "X"}, "run_id": run_id,
            "stage7_diag_env": "0", "lifecycle_generation": 1,
            "counters": {"retries": 0, "duplicates": 0}, "status": "PASS",
            "detail": {},
        }
        data = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                          indent=1).encode()
        (research / f"{tc}.json").write_bytes(data)
        index["entries"][tc] = {"path": f"evidence/research_ready/{tc}.json",
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "status": "PASS"}
    (research / "index.json").write_bytes(json.dumps(
        index, ensure_ascii=True, sort_keys=True, indent=1).encode())
    return str(research)


def _verification_root(tmp: Path, audit: dict) -> str:
    root = tmp / "verification"
    root.mkdir()
    (root / "frozen-hash-audit.json").write_text(json.dumps(audit))
    (root / "unit-contract.log").write_text("ok\n")
    (root / "integration-stage7.log").write_text("ok\n")
    return str(root)


class InstallationAuditBuilderTest(unittest.TestCase):
    REQUIRED = ("TC-STAGE7-RR-001", "TC-STAGE7-RR-002", "TC-STAGE7-RR-003",
                "TC-STAGE7-RR-004", "TC-STAGE7-RR-005", "TC-STAGE7-RR-007")

    def _build(self, audit, head_provider):
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            roots = [_write_evidence(tmp / "runs", "qual-x-rr-all", self.REQUIRED)]
            return builder.build(
                roots, str(tmp / "final.json"), software_ok=True,
                frozen_hash_ok=True,
                verification_root=_verification_root(tmp, audit),
                head_provider=head_provider)

    def test_v2_installation_audit_matching_digest_is_ready(self):
        digest = "c" * 64
        audit = {"audit_type": "STAGE7_STANDALONE_INSTALLATION_AUDIT_V2",
                 "installation_sha256": digest, "installation_verified": True,
                 "all_unchanged": True, "files": [{"path": "x", "unchanged": True}]}
        result = self._build(audit, lambda: digest)
        self.assertEqual(result["final_status"], "RESEARCH_READY",
                         result["consistency_problems"])

    def test_v2_digest_mismatch_or_unknown_is_not_ready(self):
        digest = "c" * 64
        audit = {"installation_sha256": digest, "installation_verified": True,
                 "all_unchanged": True, "files": [{"path": "x"}]}
        result = self._build(audit, lambda: "d" * 64)
        self.assertEqual(result["final_status"], "RESEARCH_NOT_READY")
        self.assertTrue(any("installation_sha256" in p and "불일치" in p
                            for p in result["consistency_problems"]))
        result = self._build(audit, lambda: None)  # 확인 불능 → fail-closed
        self.assertEqual(result["final_status"], "RESEARCH_NOT_READY")
        self.assertTrue(any("확인 불가" in p for p in result["consistency_problems"]))

    def test_v2_requires_installation_verified_true(self):
        digest = "c" * 64
        audit = {"installation_sha256": digest, "installation_verified": False,
                 "all_unchanged": True, "files": [{"path": "x"}]}
        result = self._build(audit, lambda: digest)
        self.assertEqual(result["final_status"], "RESEARCH_NOT_READY")
        self.assertTrue(any("installation_verified" in p
                            for p in result["consistency_problems"]))

    def test_v2_rejects_git_hash_shaped_digest(self):
        """40-hex(git commit)을 installation digest 자리에 넣어도 통과 불가."""
        audit = {"installation_sha256": "a" * 40, "installation_verified": True,
                 "all_unchanged": True, "files": [{"path": "x"}]}
        result = self._build(audit, lambda: "a" * 40)
        self.assertEqual(result["final_status"], "RESEARCH_NOT_READY")

    def test_v1_head_commit_path_unchanged(self):
        head = "e" * 40
        audit = {"head_commit": head, "worktree_dirty": False,
                 "all_unchanged": True, "files": [{"path": "x"}]}
        self.assertEqual(self._build(audit, lambda: head)["final_status"],
                         "RESEARCH_READY")
        self.assertEqual(self._build(audit, lambda: "f" * 40)["final_status"],
                         "RESEARCH_NOT_READY")
        dirty = dict(audit, worktree_dirty=True)
        self.assertEqual(self._build(dirty, lambda: head)["final_status"],
                         "RESEARCH_NOT_READY")


if __name__ == "__main__":
    unittest.main()
