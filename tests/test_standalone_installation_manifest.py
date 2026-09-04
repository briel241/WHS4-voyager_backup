"""installation manifest(v2) 계약 시험 — Git·binding 없는 설치 결속.

증명하는 것
  scan       : var/·identity/·.git·__pycache__·hardware.toml·binding 제외,
               순환 해시 없음(var/ 제외)
  분류       : 교차영향 파일 다중 결속, qualification 시험=tooling, Markdown=
               비결속, 미분류 실행 파일=operation(fail-safe)
  digest     : canonical JSON·파일별 SHA·영역별 SHA·installation SHA,
               비결속 변경은 어느 digest에도 영향 없음
  write/load : atomic 기록(.tmp 잔존 없음), 4 파일 상호 정합, 변조·누락 거부
  verify     : 재측정으로 missing/mismatch/untracked 검출, 영역별 변경 위치 보고
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import installation_manifest as im  # noqa: E402
from apps.standalone import qualification_domains as qd  # noqa: E402
from apps.standalone import state  # noqa: E402


def make_root(tmp) -> Path:
    """installation scan이 가능한 최소 설치 트리 (RF-critical 심볼 실물 포함)."""
    root = Path(tmp) / "root"
    (root / "apps" / "standalone").mkdir(parents=True)
    shutil.copy2(Path(_REPO) / "apps" / "standalone" / "qualify_cmd.py",
                 root / "apps" / "standalone" / "qualify_cmd.py")
    (root / "apps" / "standalone" / "cli.py").write_text("CLI = 1\n")
    (root / "apps" / "standalone" / "local_config.py").write_text("LC = 1\n")
    (root / "VDETER" / "src" / "vdeter" / "stage7").mkdir(parents=True)
    (root / "VDETER" / "src" / "vdeter" / "stage7" / "pi_runtime.py"
     ).write_text("RF = 1\n")
    (root / "VDETER" / "configs" / "stage7").mkdir(parents=True)
    (root / "VDETER" / "configs" / "stage7" / "system.toml").write_text("a=1\n")
    (root / "VDETER" / "configs" / "stage7"
     / "standalone-binding.json").write_text("{}")   # legacy: 제외
    (root / "VDETER" / "tests" / "unit").mkdir(parents=True)
    (root / "VDETER" / "tests" / "unit" / "test_x.py").write_text("T = 1\n")
    (root / "common").mkdir()
    (root / "common" / "codec.py").write_text("C = 1\n")
    (root / "mystery").mkdir()
    (root / "mystery" / "tool.py").write_text("M = 1\n")   # 미분류 → operation
    (root / "README.md").write_text("# doc\n")
    (root / "hardware.toml").write_text("x = 1\n")          # 제외
    (root / ".git").write_text("gitdir: /elsewhere\n")      # 제외 (worktree 파일)
    (root / "var" / "installation").mkdir(parents=True)
    (root / "var" / "installation" / "junk.json").write_text("{}")  # 제외
    (root / "identity" / "private").mkdir(parents=True)
    (root / "identity" / "private" / "key.pem").write_text("SECRET")  # 제외
    (root / "apps" / "__pycache__").mkdir()
    (root / "apps" / "__pycache__" / "x.pyc").write_bytes(b"junk")   # 제외
    return root


def test_scan_excludes_state_and_git_and_generated_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(tmp)
        files = im.scan_files(root)
        assert "apps/standalone/qualify_cmd.py" in files
        assert "README.md" in files
        for excluded in ("hardware.toml", ".git",
                         "VDETER/configs/stage7/standalone-binding.json",
                         "var/installation/junk.json",
                         "identity/private/key.pem", "apps/__pycache__/x.pyc"):
            assert excluded not in files, excluded
        assert not any(p.startswith(("var/", "identity/")) for p in files)


def test_classification_multi_domain_and_fail_safe():
    with tempfile.TemporaryDirectory() as tmp:
        record = im.measure(make_root(tmp))
        files = record["files"]
        assert files["VDETER/src/vdeter/stage7/pi_runtime.py"]["domains"] == ["rf"]
        assert files["apps/standalone/local_config.py"]["domains"] == [
            "rf", "operation"]                       # 교차영향: RF config 생성
        assert files["apps/standalone/qualify_cmd.py"]["domains"] == ["tooling"]
        assert files["VDETER/tests/unit/test_x.py"]["domains"] == ["tooling"]
        assert files["apps/standalone/cli.py"]["domains"] == ["operation"]
        assert files["common/codec.py"]["domains"] == ["operation"]
        assert files["mystery/tool.py"]["domains"] == ["operation"]  # fail-safe
        assert files["README.md"]["domains"] == ["non_binding"]
        # RF-critical 심볼은 rf digest에 함께 결속된다.
        assert set(record["rf_critical_symbols"]["apps/standalone/qualify_cmd.py"]) == set(
            qd.RF_CRITICAL_QUALIFY_SYMBOLS["apps/standalone/qualify_cmd.py"])
        for domain in qd.BOUND_DOMAINS:
            assert len(record["domains"][domain]["sha256"]) == 64
        assert len(record["installation_sha256"]) == 64


def test_digests_are_canonical_and_nonbinding_changes_do_not_affect_them():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(tmp)
        first = im.measure(root, now="T0")
        second = im.measure(root, now="T1")
        # generated_utc만 다르고 digest는 결정적이다.
        assert first["installation_sha256"] == second["installation_sha256"]
        assert first["domains"] == second["domains"]
        # canonical bytes: 키 정렬·구분자 최소.
        assert im.canonical_bytes({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'
        # Markdown 변경 → 어느 digest에도 영향 없음.
        (root / "README.md").write_text("# doc changed\n")
        third = im.measure(root)
        assert third["installation_sha256"] == first["installation_sha256"]
        assert third["files"]["README.md"]["sha256"] != first["files"]["README.md"]["sha256"]
        # rf 파일 변경 → rf digest·installation digest만 변경, 나머지 불변.
        (root / "VDETER" / "src" / "vdeter" / "stage7" / "pi_runtime.py"
         ).write_text("RF = 2\n")
        fourth = im.measure(root)
        assert fourth["domains"]["rf"]["sha256"] != first["domains"]["rf"]["sha256"]
        assert fourth["domains"]["operation"]["sha256"] == first["domains"]["operation"]["sha256"]
        assert fourth["domains"]["tooling"]["sha256"] == first["domains"]["tooling"]["sha256"]
        assert fourth["installation_sha256"] != first["installation_sha256"]
        # RF-critical 심볼(qualify_cmd run mode) 변경 → rf digest 변경 (파일은 tooling).
        src = (root / "apps" / "standalone" / "qualify_cmd.py").read_text("utf-8")
        (root / "apps" / "standalone" / "qualify_cmd.py").write_text(
            src.replace('"rr-p1": "GAIN_CALIBRATION"', '"rr-p1": "OPERATIONAL"'),
            encoding="utf-8")
        fifth = im.measure(root)
        assert fifth["domains"]["rf"]["sha256"] != fourth["domains"]["rf"]["sha256"]
        assert fifth["domains"]["tooling"]["sha256"] != fourth["domains"]["tooling"]["sha256"]


def test_write_load_verify_roundtrip_atomic_and_tamper_detection():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(tmp)
        paths = state.StatePaths(root)
        record = im.measure(root)
        written = im.write_installation(paths, record)
        directory = im.installation_dir(paths)
        for name in ("installation-manifest.json", "rf-manifest.json",
                     "operation-manifest.json", "tooling-manifest.json"):
            assert (directory / name).is_file(), name
            assert not (directory / (name + ".tmp")).exists()   # atomic
        assert written["installation_sha256"] == record["installation_sha256"]
        loaded = im.load_installation(paths)
        assert loaded["installation_sha256"] == record["installation_sha256"]
        report = im.verify_installation(root, loaded)
        assert report["pass"], report
        assert all(v["match"] for v in report["domains"].values())

        # 변조 1: 영역 digest를 손으로 고침 → 적재 거부.
        path = directory / "installation-manifest.json"
        payload = json.loads(path.read_text("utf-8"))
        payload["domains"]["rf"]["sha256"] = "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            im.load_installation(paths)
            raise AssertionError("변조가 거부되지 않았다")
        except im.InstallationError as exc:
            assert "변조" in str(exc)
        im.write_installation(paths, record)
        # 변조 2: 영역 manifest 파일만 바꿈 → 상호 정합 실패.
        sub = directory / "operation-manifest.json"
        sub_payload = json.loads(sub.read_text("utf-8"))
        sub_payload["files"]["apps/standalone/cli.py"] = "1" * 64
        sub.write_text(json.dumps(sub_payload), encoding="utf-8")
        try:
            im.load_installation(paths)
            raise AssertionError("영역 manifest 변조가 거부되지 않았다")
        except im.InstallationError as exc:
            assert "operation-manifest" in str(exc)
        # 누락 → 거부(setup 안내).
        sub.unlink()
        try:
            im.load_installation(paths)
            raise AssertionError("누락이 거부되지 않았다")
        except im.InstallationError as exc:
            assert "setup" in str(exc)


def test_verify_reports_missing_mismatch_untracked_by_domain():
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(tmp)
        paths = state.StatePaths(root)
        record = im.measure(root)
        im.write_installation(paths, record)
        (root / "common" / "codec.py").write_text("C = 2\n")          # operation
        (root / "VDETER" / "tests" / "unit" / "test_x.py").unlink()     # tooling
        (root / "apps" / "new_tool.py").write_text("N = 1\n")          # untracked
        (root / "README.md").write_text("# other\n")                    # 비결속
        report = im.verify_installation(root, im.load_installation(paths))
        assert report["pass"] is False
        assert report["mismatch"] == ["common/codec.py"]
        assert report["missing"] == ["VDETER/tests/unit/test_x.py"]
        assert report["untracked"] == ["apps/new_tool.py"]
        assert report["non_binding_changed"] == ["README.md"]
        assert report["domains"]["rf"]["match"] is True
        assert report["domains"]["operation"]["match"] is False
        assert report["domains"]["tooling"]["match"] is False
        text = im.describe_mismatch(report)
        assert "operation" in text and "vdeter qualify" in text


def test_real_repo_measure_is_self_consistent():
    """실제 저장소 실측: rf/operation/tooling 비어 있지 않고, 결속 파일은
    전부 하나 이상의 결속 영역, .git·var·identity 없음, 재측정 pass."""
    record = im.measure(Path(_REPO))
    assert record["domains"]["rf"]["file_count"] > 100
    assert record["domains"]["operation"]["file_count"] > 10
    assert record["domains"]["tooling"]["file_count"] > 100
    for path in record["files"]:
        assert not path.startswith(("var/", "identity/", ".git/"))
        assert path != ".git"
    assert "VDETER/apps/stage7_coordinator_main.py" in qd.domain_files_v2(
        record["files"], qd.RF)
    assert "apps/standalone/run_cmd.py" in qd.domain_files_v2(
        record["files"], qd.OPERATION)
    assert "apps/standalone/setup_cmd.py" in qd.domain_files_v2(
        record["files"], qd.TOOLING)
    assert "bin/vdeter" in record["files"]
    assert set(record["files"]["bin/vdeter"]["domains"]) == {"tooling", "operation"}
    report = im.verify_installation(Path(_REPO), record)
    assert report["pass"], (report["missing"][:3], report["mismatch"][:3],
                            report["untracked"][:3])
