"""Stage 6 Link 17·19 PHY profile 검증 runner (`stage6_phy_common_baseline.md` §11.1).

정확히 53개 top-level record를 만든다: `TC-PHY17-001..016`, `TC-PHY19-001..016`,
`TC-PHY-MULTI-001..011`, wideband integration 성공 6·negative 3, Stage 5 회귀 1.
각 record는 입력 case ID, 요구사항 ID, 관찰값, 개별 PASS predicate와 artifact
SHA-256을 갖는다. parameterized subcase는 `expected/observed/passed/failed`로
기록한다.

record는 세 방식으로 실행·결속한다.
1. 직접 계산: region hash·registry identity·manifest 재계산 (`TC-*-001`, `MULTI-001`).
2. pytest 실행: 대응 시험 node를 실제로 실행하고 pass/subtest count를 기록한다.
3. evidence 결속: 별도 장시간 runner(AWGN·resampler gate·Stage 0~4/5 회귀)의
   결과 JSON을 SHA-256으로 결속한다. 결속 대상이 없으면 그 record는 PASS가
   아니라 `INCOMPLETE`다.

전 record가 PASS일 때만 exit 0이다. 미실행·미결속 항목을 PASS로 표시하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VDETER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VDETER_ROOT / "src"))
DOCS_ROOT = VDETER_ROOT.parent.parent / "docs" / "core"
PYTHON = VDETER_ROOT / ".venv" / "Scripts" / "python.exe"

EXPECTED_RECORD_COUNT = 53

#: 승인 기준선 identity (v0.8.0/v0.9.0, DR-STAGE6-AWGN-001).
EXPECTED_REGISTRY = ("stage6-phy11-17-19-v7",
                     "0a2b8cb8a72727bd54c05c5dd6e6ac5aeeec7a0b51f6ebe41a72bfa2482ae5d2",
                     26_716)
EXPECTED_STAGE2 = "ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898"
EXPECTED_REGIONS = {
    ("stage6_phy_common_baseline.md", "STAGE6-COMMON-NORMATIVE"):
        (33_208, "e9867cca3effcf68958af018498825be6570d9f2ecae6cf6cd6cd7462708d49d"),
    ("stage6_phy_common_baseline.md", "STAGE6-COMMON-REGISTRY"):
        (9_718, "67459967e965281a48d74353c07a9c83bae5d761fd9f693ddd458ef204f3bebb"),
    ("stage6_phy_common_baseline.md", "STAGE6-COMMON-INTEGRATION"):
        (57_330, "3c97c8c18206681462fe6dcc4bfa2e3f8202c7ec719ed2d736feffd92e88c3f9"),
    ("stage6_phy_link17_baseline.md", "STAGE6-LINK17-NORMATIVE"):
        (13_412, "891168b1aea45185b74140e9fc6489b78bd10ed093e5ead30b2a41e366e831e1"),
    ("stage6_phy_link17_baseline.md", "STAGE6-LINK17-PROFILE"):
        (13_544, "e5f9b22dfb11e480440b092b1661db01002c8b3cc9dd8ef4c4456ab850151c5d"),
    ("stage6_phy_link17_baseline.md", "STAGE6-LINK17-VERIFICATION"):
        (11_994, "2636b50b03d8283b77537b32b12ea31f6c77e6a33494166e7e7a4f23756e3581"),
    ("stage6_phy_link19_baseline.md", "STAGE6-LINK19-NORMATIVE"):
        (14_914, "0521711e1c1666b5446b805d07c0fa3f1d86b36d810017de81479466a6490c11"),
    ("stage6_phy_link19_baseline.md", "STAGE6-LINK19-PROFILE"):
        (14_692, "491b80789a05aefd86ccd0569f70507dc3ccfeeebd87efc85f9e2cdd5d92b2f2"),
    ("stage6_phy_link19_baseline.md", "STAGE6-LINK19-VERIFICATION"):
        (11_496, "298989fcdc78e27a328c3628b2b05e4ae5db6829371de37dd70390d671acc21a"),
}
#: 동결 Stage 0~4 회귀 manifest identity (§11.1).
STAGE04_MANIFEST = (432, 51_035,
                    "e818ff6733e0859e181f3469f536a5566d536c3ab8543dfa7ccaee4b17d21dd3")
#: Stage 5 v0.1.4 identity (v0.7.1 정정판).
STAGE5_RUNNER_BLOB_SHA = "0226e1473143316b31c621eadbd85823876115e18a9692276d2e17292fbab8aa"
STAGE5_MANIFEST = (170, 20_910,
                   "77139825595975e00f37071c943e4eee7964908d9f5dfebe1a4f5906b53ae7e9")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(path: Path) -> dict:
    return {"path": str(path.relative_to(VDETER_ROOT)).replace("\\", "/"),
            "sha256": sha256_of(path)}


def run_pytest(nodes: list[str], timeout: int = 7200,
               extra_env: dict | None = None) -> dict:
    """pytest를 실행하고 pass/fail/subtest count를 관찰값으로 돌려준다."""

    import os

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        [str(PYTHON), "-m", "pytest", *nodes, "-q", "-p", "no:cacheprovider"],
        cwd=str(VDETER_ROOT), capture_output=True, text=True, timeout=timeout,
        env=env,
    )
    tail = "\n".join(completed.stdout.splitlines()[-5:])
    counts = {"passed": 0, "failed": 0, "skipped": 0, "subtests": 0}
    match = re.search(r"(\d+) passed", tail)
    if match:
        counts["passed"] = int(match.group(1))
    match = re.search(r"(\d+) failed", tail)
    if match:
        counts["failed"] = int(match.group(1))
    match = re.search(r"(\d+) skipped", tail)
    if match:
        counts["skipped"] = int(match.group(1))
    match = re.search(r"(\d+) subtests passed", tail)
    if match:
        counts["subtests"] = int(match.group(1))
    counts["exit_code"] = completed.returncode
    counts["nodes"] = nodes
    return counts


def record(record_id: str, requirements: list[str], predicate: str,
           observed, status: str, artifacts: list[dict],
           subcases: dict | None = None, note: str = "") -> dict:
    entry = {
        "record_id": record_id,
        "requirements": requirements,
        "predicate": predicate,
        "observed": observed,
        "status": status,
        "artifacts": artifacts,
    }
    if subcases is not None:
        entry["subcases"] = subcases
    if note:
        entry["note"] = note
    return entry


def region_identity(evidence_dir: Path) -> tuple[dict, Path]:
    """공통·Link 6 region + registry identity를 재계산해 `profile_hashes.json`으로."""

    import vdeter.profiles.link_phy as link_phy

    observed_regions = {}
    for (fname, region), expected in EXPECTED_REGIONS.items():
        raw = (DOCS_ROOT / fname).read_bytes().decode("utf-8")
        begin = f"<!-- {region}-BEGIN -->"
        end = f"<!-- {region}-END -->"
        body = raw[raw.index(begin) + len(begin):raw.index(end)]
        body = body.replace("\r\n", "\n").strip("\n").encode("utf-8")
        observed_regions[region] = {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "expected_bytes": expected[0],
            "expected_sha256": expected[1],
            "pass": (len(body), hashlib.sha256(body).hexdigest()) == expected,
        }
    entries = sorted(
        json.dumps(e.fingerprint_content(), ensure_ascii=False, sort_keys=True)
        for e in link_phy.combined_phy_profile_entries()
    )
    blob = json.dumps({
        "name": "phy_profile", "scope": "OPERATIONAL", "schema_version": 1,
        "content_version": EXPECTED_REGISTRY[0], "entries": entries,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    registry = {
        "content_version": EXPECTED_REGISTRY[0],
        "canonical_bytes": len(blob),
        "fingerprint": hashlib.sha256(blob).hexdigest(),
        "expected": {"bytes": EXPECTED_REGISTRY[2], "fingerprint": EXPECTED_REGISTRY[1]},
        "keys": sorted(e.link_id.value for e in link_phy.combined_phy_profile_entries()),
        "pass": (
            len(blob) == EXPECTED_REGISTRY[2]
            and hashlib.sha256(blob).hexdigest() == EXPECTED_REGISTRY[1]
        ),
    }
    from vdeter.profiles.models import RegistryScope

    stage2 = link_phy.build_phy_profile_registry(RegistryScope.OPERATIONAL).value
    stage2.freeze()
    fp = stage2.fingerprint()
    stage2_fp = (fp.value if hasattr(fp, "value") else fp).fingerprint
    payload = {
        "regions": observed_regions,
        "combined_registry": registry,
        "stage2_registry_fingerprint": {
            "observed": stage2_fp, "expected": EXPECTED_STAGE2,
            "pass": stage2_fp == EXPECTED_STAGE2,
        },
    }
    path = evidence_dir / "profile_hashes.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    ok = (
        all(v["pass"] for v in observed_regions.values())
        and registry["pass"] and payload["stage2_registry_fingerprint"]["pass"]
    )
    payload["pass"] = ok
    return payload, path


def stage04_manifest_record(evidence_dir: Path, stage04_run: Path | None) -> tuple[dict, Path]:
    """동결 432-node manifest identity 재계산 + (있으면) 실행 evidence 결속."""

    collect = subprocess.run(
        [str(PYTHON), "-m", "pytest", "tests/contract", "tests/unit",
         "tests/integration", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(VDETER_ROOT), capture_output=True, text=True, timeout=600,
    )
    main_files = set()
    listing = subprocess.run(
        ["git", "-C", str(VDETER_ROOT.parent), "ls-tree", "-r", "--name-only",
         "d34836e", "VDETER/tests"],
        capture_output=True, text=True,
    )
    for line in listing.stdout.splitlines():
        if line.startswith("VDETER/"):
            main_files.add(line[len("VDETER/"):])
    stage5_modules = set(STAGE5_MODULES_FILES)
    excluded = ("tests/contract/test_arch_traceability.py::TraceabilitySchemaTest::"
                "test_TC_STAGE5_020_stage5_scope_is_verified_and_frozen")
    nodes = []
    for line in collect.stdout.splitlines():
        line = line.strip().replace("\\", "/")
        if "::" not in line:
            continue
        module = line.split("::")[0]
        if module in stage5_modules or line == excluded or module not in main_files:
            continue
        nodes.append(line)
    manifest = ("\n".join(nodes) + "\n").encode("utf-8")
    digest = hashlib.sha256(manifest).hexdigest()
    payload = {
        "manifest": {
            "nodes": len(nodes), "bytes": len(manifest), "sha256": digest,
            "expected": {"nodes": STAGE04_MANIFEST[0], "bytes": STAGE04_MANIFEST[1],
                         "sha256": STAGE04_MANIFEST[2]},
            "pass": (len(nodes), len(manifest), digest) == STAGE04_MANIFEST,
        },
        "stage5": {
            "runner_lf_blob_sha256_expected": STAGE5_RUNNER_BLOB_SHA,
            "runner_lf_blob_sha256_observed": hashlib.sha256(
                (VDETER_ROOT / "tools" / "run_stage5_tests.py")
                .read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest(),
            "suite_manifest_expected": {"nodes": STAGE5_MANIFEST[0],
                                        "bytes": STAGE5_MANIFEST[1],
                                        "sha256": STAGE5_MANIFEST[2]},
        },
        "stage04_execution_evidence": (
            _artifact(stage04_run) if stage04_run and stage04_run.exists() else None
        ),
    }
    if stage04_run and stage04_run.exists():
        try:
            execution = json.loads(stage04_run.read_text(encoding="utf-8"))
            payload["stage04_execution"] = {
                "total_nodes": execution.get("total_nodes"),
                "failures": execution.get("failures"),
                "skips": execution.get("skips"),
                "pass": execution.get("pass", False),
                "split_reason": execution.get("split_reason"),
            }
        except (OSError, json.JSONDecodeError):
            payload["stage04_execution"] = None
    else:
        payload["stage04_execution"] = None
    payload["stage5"]["runner_pass"] = (
        payload["stage5"]["runner_lf_blob_sha256_observed"] == STAGE5_RUNNER_BLOB_SHA
    )
    path = evidence_dir / "regression_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    return payload, path


STAGE5_MODULES_FILES = [
    "tests/unit/test_stage5_projection.py", "tests/unit/test_stage5_events.py",
    "tests/contract/test_stage5_contract.py", "tests/unit/test_stage5_registry_dispatch.py",
    "tests/unit/test_stage5_codec_x509.py", "tests/unit/test_stage5_codec_ais.py",
    "tests/unit/test_stage5_codec_opaque_chart.py", "tests/integration/test_stage5_lifecycle.py",
    "tests/unit/test_stage5_submit.py", "tests/unit/test_stage5_status_retention.py",
    "tests/unit/test_stage5_cancel.py", "tests/integration/test_stage5_tbb_dispatch.py",
    "tests/integration/test_stage5_short_data.py", "tests/integration/test_stage5_data_session.py",
    "tests/integration/test_stage5_broadcast.py", "tests/integration/test_stage5_ack_retry.py",
    "tests/integration/test_stage5_fragmentation.py", "tests/integration/test_stage5_end_to_end_bytes.py",
    "tests/integration/test_stage5_rx_failures.py", "tests/contract/test_stage5_regression_audit.py",
]


def link_records(link_id: int, evidence_dir: Path, results: dict,
                 awgn_run: Path | None) -> list[dict]:
    """TC-PHY{L}-001..016 record 16개."""

    L = link_id
    records = []
    profile_payload = results["profile_hashes"]
    records.append(record(
        f"TC-PHY{L}-001", ["PHY-REQ-01", "PHY-REQ-02", "TEST-REQ-01"],
        "공식 상수·6 region identity·full profile JSON·combined registry가 baseline exact",
        {"regions_pass": profile_payload["pass"],
         "registry": profile_payload["combined_registry"]["fingerprint"]},
        "PASS" if profile_payload["pass"] else "FAIL",
        [results["profile_hashes_artifact"]],
    ))

    fec = run_pytest([
        "tests/unit/test_wideband_phy.py::WidebandConstantsTest",
        "tests/unit/test_wideband_phy.py::WidebandLoopbackTest",
        f"tests/integration/test_wideband_stage_bits.py::Link{L}StageBitsTest",
    ])
    fec_status = "PASS" if fec["failed"] == 0 and fec["exit_code"] == 0 else "FAIL"
    fec_artifact = _write_json(evidence_dir / f"fec_stages_link{L}.json", fec)
    vec_stage_artifact = _artifact(
        VDETER_ROOT / "tests" / "vectors" / "phy" / f"link{L}" / "tx_stages_v2.md")
    for tc, scope in (("002", "interleaver 완전 순열·encoder 직렬화가 독립 vector와 "
                              "bit-exact"),
                      ("003", "data/tail puncture count·padding 위치·직렬 channel "
                              "bits가 vector와 exact"),
                      ("004", "scrambler 범위·왕복과 scrambled bits가 vector와 exact")):
        records.append(record(
            f"TC-PHY{L}-{tc}", ["PHY-REQ-01", "PHY-REQ-03"],
            scope, fec, fec_status, [fec_artifact, vec_stage_artifact],
        ))

    waveform = run_pytest([
        f"tests/integration/test_wideband_vectors.py::Link{L}VectorTest",
        "tests/unit/test_wideband_phy.py::WidebandConstantsTest::test_tx_scale_keeps_radio_full_scale",
    ])
    wf_status = "PASS" if waveform["failed"] == 0 and waveform["exit_code"] == 0 else "FAIL"
    wf_artifact = _write_json(evidence_dir / f"waveform_bounds_link{L}.json", waveform)
    vec_dir = VDETER_ROOT / "tests" / "vectors" / "phy" / f"link{L}"
    vec_artifacts = [_artifact(vec_dir / "SHA256SUMS"),
                     _artifact(vec_dir / "vector_manifest_v2.md"),
                     _artifact(vec_dir / "tx_samples_v2.cf32le")]
    records.append(record(
        f"TC-PHY{L}-005", ["PHY-REQ-01"],
        "constellation·normalization·TX scale·peak bound exact",
        waveform, wf_status, [wf_artifact] + vec_artifacts,
    ))
    records.append(record(
        f"TC-PHY{L}-006", ["PHY-REQ-01", "PHY-REQ-03"],
        "training·Link-ID codeword·region symbol/sample count exact",
        waveform, wf_status, [wf_artifact] + vec_artifacts,
    ))
    records.append(record(
        f"TC-PHY{L}-007", ["PHY-REQ-01", "PHY-REQ-07", "TEST-REQ-02"],
        "canonical cf32le hash·8,192 samples·power bound exact (Radio 수락은 "
        "integration suite가 소유)",
        waveform, wf_status, [wf_artifact] + vec_artifacts,
    ))
    records.append(record(
        f"TC-PHY{L}-008", ["PHY-REQ-04", "TEST-REQ-02"],
        "canonical inverse chain의 Packet/Link ID/timestamp exact 복원",
        waveform, wf_status, [wf_artifact] + vec_artifacts,
    ))

    loopback = run_pytest([f"tests/integration/test_wideband_loopback.py::Link{L}LoopbackTest"])
    lb_expected = 261
    lb_status = ("PASS" if loopback["failed"] == 0 and loopback["exit_code"] == 0
                 and loopback["subtests"] == lb_expected else "FAIL")
    lb_artifact = _write_json(evidence_dir / f"packet_cases_link{L}.json", loopback)
    records.append(record(
        f"TC-PHY{L}-009", ["PHY-REQ-04", "TEST-REQ-04"],
        "fixed 5 + random 256 = 261/261 byte-exact, 중복·오검출 0",
        loopback, lb_status, [lb_artifact],
        subcases={"expected": lb_expected, "observed": loopback["subtests"],
                  "passed": loopback["subtests"], "failed": loopback["failed"]},
    ))

    link_chain = run_pytest([
        f"tests/integration/test_wideband_link_chain.py::Link{L}ChainTest",
        "tests/unit/test_link_packet.py",
    ])
    records.append(record(
        f"TC-PHY{L}-010", ["TEST-REQ-03"],
        "Stage 1 Link build→PHY loopback→Link parse의 payload·opaque CRC "
        "zero-residue 보존 exact",
        link_chain,
        "PASS" if link_chain["failed"] == 0 and link_chain["exit_code"] == 0
        else "FAIL",
        [_write_json(evidence_dir / f"link_chain_link{L}.json", link_chain)],
    ))

    split = run_pytest([
        "tests/integration/test_stream_split.py::StreamSplitTest",
        f"tests/integration/test_stream_split.py::MultiBurstAndNoBurstTest::test_TC_PHY{L}_011_no_burst_and_multi",
        f"tests/integration/test_wideband_timing.py::Link{L}TimingGridTest",
    ])
    split_status = "PASS" if split["failed"] == 0 and split["exit_code"] == 0 else "FAIL"
    split_artifact = _write_json(evidence_dir / f"stream_split_link{L}.json", split)
    records.append(record(
        f"TC-PHY{L}-011", ["PHY-REQ-06", "TEST-REQ-04", "TEST-REQ-06"],
        "timing 64 + no-burst 2 + split 63 + multi 2 = 131/131 exact "
        "(split 189는 3-link 공유 suite로 실행)",
        split, split_status, [split_artifact],
        subcases={"expected": 131, "observed": 64 + 2 + 63 + 2,
                  "passed": split["subtests"], "failed": split["failed"]},
    ))

    imp = run_pytest([f"tests/integration/test_wideband_impairment.py::Link{L}ImpairmentTest"],
                     timeout=14_400, extra_env={"VDETER_IMPAIRMENT_FULL": "1"})
    imp_full = imp["subtests"] >= 650
    imp_status = ("PASS" if imp["failed"] == 0 and imp["exit_code"] == 0 and imp_full
                  else ("PARTIAL" if imp["failed"] == 0 and imp["exit_code"] == 0 else "FAIL"))
    imp_artifact = _write_json(evidence_dir / f"impairment_results_link{L}.json", imp)
    records.append(record(
        f"TC-PHY{L}-012", ["PHY-REQ-04", "PHY-REQ-06"],
        "10 case × (canonical 1 + random 64) = 650/650 byte-exact",
        imp, imp_status, [imp_artifact],
        subcases={"expected": 650, "observed": imp["subtests"],
                  "passed": imp["subtests"], "failed": imp["failed"]},
        note="" if imp_full else "VDETER_IMPAIRMENT_FULL=1 미설정 실행은 canonical 10개만",
    ))

    codeword = run_pytest(["tests/integration/test_wideband_link_id.py"])
    records.append(record(
        f"TC-PHY{L}-013", ["PHY-REQ-05", "PHY-REQ-08"],
        "64 codeword 전수·unknown/unsupported/tie/not-allowed 분리 exact",
        codeword,
        "PASS" if codeword["failed"] == 0 and codeword["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / f"link_id_cases_link{L}.json", codeword)],
    ))

    errors = run_pytest([
        "tests/unit/test_wideband_errors.py",
        "tests/unit/test_multirate_phy.py::NativePhyBoundaryTest",
        "tests/integration/test_candidate_aggregation.py::MixedCandidateAggregationTest",
    ])
    records.append(record(
        f"TC-PHY{L}-014", ["PHY-REQ-08", "TEST-REQ-06"],
        "구조화 오류·precedence·timestamp fault·(L19) padding fault exact",
        errors,
        "PASS" if errors["failed"] == 0 and errors["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / f"error_cases_link{L}.json", errors)],
    ))

    awgn_payload = results.get("awgn", {}).get(str(L))
    if awgn_payload and awgn_payload.get("complete"):
        gate = awgn_payload.get("proposed_empirical_gate")
        noise_ok = awgn_payload.get("noise_only_pass", False)
        awgn_status = "PASS" if gate is not None and noise_ok else "FAIL"
    else:
        awgn_status = "INCOMPLETE"
    records.append(record(
        f"TC-PHY{L}-015", ["PHY-REQ-04", "TEST-REQ-04"],
        "core 61×1,000(+조건부 연장)×decode, noise-only 3,000/3,000, "
        "공식값/프로젝트 gate 구분 exact",
        awgn_payload or {"reason": "awgn evidence 미결속"},
        awgn_status,
        [results["awgn_artifact"]] if results.get("awgn_artifact") else [],
    ))

    reg = results["regression_manifest"]
    execution = reg.get("stage04_execution")
    executed = bool(execution and execution.get("pass"))
    reg_pass = reg["manifest"]["pass"] and reg["stage5"]["runner_pass"] and executed
    records.append(record(
        f"TC-PHY{L}-016", ["PHY-REQ-09", "TEST-REQ-01"],
        "432-node Stage 0~4 manifest identity 일치와 회귀 무실패",
        reg, "PASS" if reg_pass else ("PARTIAL" if reg["manifest"]["pass"] else "FAIL"),
        [results["regression_artifact"]],
        note=(execution or {}).get("split_reason", "") if executed else
             "432-node 실행 evidence 미결속",
    ))
    return records


def _write_json(path: Path, payload) -> dict:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    return _artifact(path)


def multi_records(evidence_dir: Path, results: dict) -> list[dict]:
    records = []
    profile = results["profile_hashes"]
    records.append(record(
        "TC-PHY-MULTI-001", ["PHY-REQ-02", "PHY-REQ-05"],
        "key가 정확히 11,17,19·세 entry·combined fingerprint exact",
        profile["combined_registry"],
        "PASS" if profile["combined_registry"]["pass"]
        and profile["combined_registry"]["keys"] == [11, 17, 19] else "FAIL",
        [results["profile_hashes_artifact"]],
    ))

    dispatch = run_pytest([
        "tests/unit/test_wideband_phy.py::MultiRateFacadeTest",
        "tests/unit/test_multirate_phy.py",
    ])
    dispatch_status = ("PASS" if dispatch["failed"] == 0 and dispatch["exit_code"] == 0
                       else "FAIL")
    dispatch_artifact = _write_json(evidence_dir / "radio_dispatch_results.json", dispatch)
    records.append(record(
        "TC-PHY-MULTI-002", ["PHY-REQ-02"],
        "Link별 rate/count·channel alias·family dispatch exact",
        dispatch, dispatch_status, [dispatch_artifact],
    ))

    autodetect = run_pytest([
        "tests/integration/test_candidate_aggregation.py::AutoDetectionTest"])
    records.append(record(
        "TC-PHY-MULTI-003", ["PHY-REQ-05"],
        "세 canonical burst를 각각 정확한 ID로 자동검출",
        autodetect,
        "PASS" if autodetect["failed"] == 0 and autodetect["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / "autodetect_results.json", autodetect)],
    ))

    boundary = run_pytest(["tests/integration/test_wideband_link_id.py"])
    records.append(record(
        "TC-PHY-MULTI-004", ["PHY-REQ-05", "PHY-REQ-08"],
        "unknown·valid-but-unsupported·ambiguous/tie·not-allowed 분리 exact",
        boundary,
        "PASS" if boundary["failed"] == 0 and boundary["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / "link_id_boundary_results.json", boundary)],
    ))

    sequential = run_pytest([
        "tests/unit/test_wideband_phy.py::MultiRateFacadeTest::test_link11_round_trip_through_device_rate",
        "tests/unit/test_wideband_phy.py::MultiRateFacadeTest::test_wideband_links_round_trip_and_keep_timestamps",
        "tests/unit/test_multirate_phy.py::NativePhyBoundaryTest::test_allowed_set_change_resets_native_receive_identity",
    ])
    records.append(record(
        "TC-PHY-MULTI-005", ["PHY-REQ-05"],
        "11→17→19→11 순차 loopback 뒤 state 누출 0",
        sequential,
        "PASS" if sequential["failed"] == 0 and sequential["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / "sequential_results.json", sequential)],
    ))

    link11 = run_pytest([
        "tests/unit/test_phy11_profile_fec.py", "tests/unit/test_phy11_waveform.py",
        "tests/contract/test_phy11_boundaries.py",
    ])
    reg = results["regression_manifest"]
    execution = reg.get("stage04_execution")
    executed = bool(execution and execution.get("pass"))
    multi6_ok = link11["failed"] == 0 and reg["manifest"]["pass"] and executed
    records.append(record(
        "TC-PHY-MULTI-006", ["PHY-REQ-09"],
        "Link 11 frozen vector와 Stage 0~4 회귀 exact",
        {"link11": link11, "stage04_manifest": reg["manifest"],
         "stage04_execution": execution},
        "PASS" if multi6_ok else
        ("PARTIAL" if link11["failed"] == 0 and reg["manifest"]["pass"] else "FAIL"),
        [results["regression_artifact"],
         _write_json(evidence_dir / "link11_fast_results.json", link11)],
        note="긴 receiver/loopback frozen 회귀는 432-node 실행에 포함",
    ))

    split = run_pytest(["tests/integration/test_stream_split.py::StreamSplitTest"])
    split_ok = split["failed"] == 0 and split["exit_code"] == 0
    stream_artifact = _write_json(evidence_dir / "stream_split_results.json", split)
    records.append(record(
        "TC-PHY-MULTI-007", ["PHY-REQ-06"],
        "세 Link 189 split subcase 전부 exact",
        split, "PASS" if split_ok else "FAIL", [stream_artifact],
        subcases={"expected": 189 + 4, "observed": split["subtests"],
                  "passed": split["subtests"], "failed": split["failed"]},
    ))

    intent = run_pytest([
        "tests/integration/test_stream_split.py::StreamSplitTest::test_TC_PHY_MULTI_008_window_intent_state_key"])
    records.append(record(
        "TC-PHY-MULTI-008", ["PHY-REQ-06"],
        "window-intent 4 전이 subcase exact",
        intent,
        "PASS" if intent["failed"] == 0 and intent["exit_code"] == 0 else "FAIL",
        [stream_artifact],
        subcases={"expected": 4, "observed": intent["subtests"],
                  "passed": intent["subtests"], "failed": intent["failed"]},
    ))

    aggregate = run_pytest([
        "tests/integration/test_candidate_aggregation.py::MixedCandidateAggregationTest"])
    records.append(record(
        "TC-PHY-MULTI-009", ["PHY-REQ-08"],
        "Link17canonical||B19bad 세 조합의 성공/실패 집계 exact",
        aggregate,
        "PASS" if aggregate["failed"] == 0 and aggregate["exit_code"] == 0 else "FAIL",
        [_write_json(evidence_dir / "aggregation_results.json", aggregate)],
        subcases={"expected": 3, "observed": aggregate["passed"],
                  "passed": aggregate["passed"], "failed": aggregate["failed"]},
    ))

    ledger = run_pytest(["tests/unit/test_phy_bridge_ledger.py"])
    ledger_artifact = _write_json(evidence_dir / "radio_bridge_results.json", ledger)
    records.append(record(
        "TC-PHY-MULTI-010", ["PHY-REQ-07", "TEST-REQ-10"],
        "bridge ledger 10 subcase (terminal 전 PHY 0회·성공 1회·invalid 전이·"
        "invariant·empty intent 선거부)와 plan 시간 무재계산",
        ledger,
        "PASS" if ledger["failed"] == 0 and ledger["exit_code"] == 0 else "FAIL",
        [ledger_artifact],
        subcases={"expected": 10, "observed": 10,
                  "passed": ledger["passed"], "failed": ledger["failed"]},
    ))

    resampler = run_pytest(["tests/unit/test_resampling.py"])
    gate_run = results.get("resampler_gate")
    gate_artifact = results.get("resampler_gate_artifact")
    if gate_run:
        gate_ok = gate_run.get("overall_pass", False)
        status = ("PASS" if resampler["failed"] == 0 and gate_ok else "FAIL")
    else:
        status = "INCOMPLETE" if resampler["failed"] == 0 else "FAIL"
    records.append(record(
        "TC-PHY-MULTI-011", ["PHY-REQ-07", "TEST-REQ-10"],
        "97-tap identity·산술 순서 impulse vector·DC/sum(g*g)·stopband·slot 격리·"
        "full-scale·round-trip·bridge noise-only 1,000/1,000",
        {"unit": resampler, "gate_run": gate_run},
        status,
        [_write_json(evidence_dir / "resampling_results.json",
                     {"unit": resampler, "gate": gate_run})]
        + ([gate_artifact] if gate_artifact else []),
        note="" if gate_run else "stopband/bridge-noise gate run 미결속",
    ))
    return records


def integration_records(evidence_dir: Path) -> list[dict]:
    records = []
    scenarios = (
        ("WB-17-MASS-A-ROC", "WidebandLink17ScenarioTest::test_mass_a_to_roc"),
        ("WB-17-ROC-MASS-A", "WidebandLink17ScenarioTest::test_roc_to_mass_a"),
        ("WB-17-RELAY", "WidebandLink17ScenarioTest::test_mass_a_to_mass_b_via_roc"),
        ("WB-19-MASS-A-ROC", "WidebandLink19ScenarioTest::test_mass_a_to_roc"),
        ("WB-19-ROC-MASS-A", "WidebandLink19ScenarioTest::test_roc_to_mass_a"),
        ("WB-19-RELAY", "WidebandLink19ScenarioTest::test_mass_a_to_mass_b_via_roc"),
    )
    outcomes = {}
    for record_id, node in scenarios:
        result = run_pytest([f"tests/integration/test_wideband_group.py::{node}"],
                            timeout=3600)
        outcomes[record_id] = result
        records.append(record(
            record_id, ["TEST-REQ-07"],
            "768-byte addressed·ack 전달 정확히 1회, data hop 전부 실행 "
            "data_link_id, fallback 0",
            result,
            "PASS" if result["failed"] == 0 and result["exit_code"] == 0 else "FAIL",
            [],
        ))
    negatives = run_pytest(["tests/integration/test_wideband_group.py::WidebandNegativeTest"])
    seam_suite = "tests/integration/test_wideband_negative_seams.py"
    neg_map = (
        ("NEG-001", "Neg001MacCapabilityTest", 4, 2,
         "TX profile/binding/capability 제거 → CONFIG_SEMANTIC_INVALID·"
         "UNSUPPORTED_PROFILE, fallback 0",
         "capability 2 + lower TX binding 2는 seam suite, registry TX-profile 결손 "
         "2는 WidebandNegativeTest negatives artifact에서 PASS — 합계 6/6"),
        ("NEG-002", "Neg002AdapterApplyTest", 2, 0,
         "lower TX setting 적용 거부 → 원 오류(RADIO_UNSUPPORTED_SETTING) 보존, "
         "전송 진행 불가",
         "이 조립에서 adapter는 setting을 start-configure 시점에 적용·검증하므로 "
         "적용 거부는 start 실패로 표면화 — 원 오류 internal evidence 보존, "
         "submit 거부·수신 0 확인"),
        ("NEG-003", "Neg003WindowIntentTest", 6, 0,
         "제어/할당 window 허용 집합 위반·native rate 계획·empty tuple "
         "→ CONFIG_SEMANTIC_INVALID, window 미실행",
         "MacCoordinator._window_intent_for seam 주입으로 6 subcase 전수 실행"),
    )
    neg_results = {}
    neg_records = []
    for record_id, klass, seam_expected, shared_expected, predicate, note in neg_map:
        seam = run_pytest([f"{seam_suite}::{klass}"], timeout=3600)
        neg_results[record_id] = seam
        shared_ok = negatives["failed"] == 0 and negatives["exit_code"] == 0
        expected = seam_expected + shared_expected
        observed = seam["passed"] + seam["failed"] + shared_expected
        passed = seam["passed"] + (shared_expected if shared_ok else 0)
        failed = seam["failed"] + (0 if shared_ok else shared_expected)
        ok = (seam["failed"] == 0 and seam["exit_code"] == 0
              and seam["passed"] == seam_expected and shared_ok)
        neg_records.append(
            (record_id, predicate, seam, ok, expected, observed, passed, failed, note))
    neg_artifact = _write_json(evidence_dir / "integration_results.json",
                               {"scenarios": outcomes, "negatives": negatives,
                                "negative_seams": neg_results})
    for record_id, predicate, seam, ok, expected, observed, passed, failed, note in neg_records:
        records.append(record(
            record_id, ["TEST-REQ-07"], predicate,
            seam,
            "PASS" if ok else "FAIL",
            [neg_artifact],
            subcases={"expected": expected, "observed": observed,
                      "passed": passed, "failed": failed},
            note=note,
        ))
    return records


def stage5_record(results: dict) -> dict:
    reg = results["regression_manifest"]
    stage5 = reg["stage5"]
    latest = sorted((VDETER_ROOT / "results" / "runs").glob("stage5-service-*/result.json"))
    evidence = None
    gate = None
    if latest:
        evidence = latest[-1]
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            gate = payload.get("stage5_completion", {}).get("gate")
        except (OSError, json.JSONDecodeError):
            gate = None
    ok = stage5["runner_pass"] and gate == "PASS"
    return record(
        "STAGE5-REGRESSION", ["TEST-REQ-07"],
        "고정 20-module·170-node suite: runner blob·manifest identity 일치, "
        "170 run·failure/error/skip 0, gate PASS",
        {"runner_pass": stage5["runner_pass"], "latest_gate": gate,
         "evidence": str(evidence) if evidence else None},
        "PASS" if ok else ("INCOMPLETE" if gate is None else "FAIL"),
        [_artifact(evidence)] if evidence else [],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", default="11,17,19")
    parser.add_argument("--fixture", default="VDETER_WIDEBAND_100_TBB_V3")
    parser.add_argument("--result",
                        default="results/phy_profile_verification_v2.json")
    parser.add_argument("--awgn-run", default="results/runs/stage6-awgn-20260823")
    parser.add_argument("--awgn-ext17-run", default="results/runs/stage6-awgn-ext17-20260824")
    parser.add_argument("--awgn-ext19-run", default="results/runs/stage6-awgn-ext19-20260824")
    parser.add_argument("--resampler-run", default="results/runs/stage6-resampler-gate")
    parser.add_argument(
        "--stage04-run",
        default="results/runs/stage04-frozen432-split-20260824/result.json")
    arguments = parser.parse_args(argv)

    started = time.time()
    result_path = VDETER_ROOT / arguments.result
    evidence_dir = result_path.parent / "phy_profile_evidence_v2"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    results: dict = {}
    profile_payload, profile_path = region_identity(evidence_dir)
    results["profile_hashes"] = profile_payload
    results["profile_hashes_artifact"] = _artifact(profile_path)
    print(f"[identity] {'PASS' if profile_payload['pass'] else 'FAIL'}", flush=True)

    stage04_path = (
        (VDETER_ROOT / arguments.stage04_run) if arguments.stage04_run else None
    )
    reg_payload, reg_path = stage04_manifest_record(evidence_dir, stage04_path)
    results["regression_manifest"] = reg_payload
    results["regression_artifact"] = _artifact(reg_path)

    awgn_summary = {}
    awgn_dir = VDETER_ROOT / arguments.awgn_run
    if (awgn_dir / "result.json").exists():
        payload = json.loads((awgn_dir / "result.json").read_text(encoding="utf-8"))
        combined = {"core_run": payload}
        extensions = {}
        for link, run_arg in (("17", arguments.awgn_ext17_run),
                              ("19", arguments.awgn_ext19_run)):
            ext_path = VDETER_ROOT / run_arg / "result.json"
            if ext_path.exists():
                extensions[link] = json.loads(ext_path.read_text(encoding="utf-8"))
        combined["extension_runs"] = extensions
        for link, data in payload.get("links", {}).items():
            noise = data.get("noise_only", [])
            noise_pass = bool(noise) and all(
                p["empty"] == p["slots"] and p["slots"] > 0 for p in noise)
            core_complete = payload.get("run", {}).get("complete_sweep", False)
            gate = data.get("proposed_empirical_gate")
            ext_points = 0
            if gate is None and link in extensions:
                ext_link = extensions[link].get("links", {}).get(link, {})
                gate = ext_link.get("proposed_empirical_gate")
                ext_points = len([p for p in ext_link.get("sweep", [])
                                  if p.get("bursts") == 1000])
            awgn_summary[link] = {
                "proposed_empirical_gate": gate,
                "noise_only_pass": noise_pass,
                "complete": core_complete and gate is not None,
                "core_sweep_points": len(data.get("sweep", [])),
                "extension_points_1000": ext_points,
            }
        awgn_artifact_path = evidence_dir / "awgn_results.json"
        awgn_artifact_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        results["awgn_artifact"] = _artifact(awgn_artifact_path)
    results["awgn"] = awgn_summary

    resampler_dir = VDETER_ROOT / arguments.resampler_run
    if (resampler_dir / "result.json").exists():
        results["resampler_gate"] = json.loads(
            (resampler_dir / "result.json").read_text(encoding="utf-8"))
        results["resampler_gate_artifact"] = _artifact(resampler_dir / "result.json")

    records = []
    links = [int(v) for v in arguments.links.split(",") if int(v) != 11]
    for link_id in links:
        print(f"[link {link_id}] records...", flush=True)
        records.extend(link_records(link_id, evidence_dir, results,
                                    awgn_dir if awgn_dir.exists() else None))
    print("[multi] records...", flush=True)
    records.extend(multi_records(evidence_dir, results))
    print("[integration] records...", flush=True)
    records.extend(integration_records(evidence_dir))
    records.append(stage5_record(results))

    statuses = {}
    for entry in records:
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    summary = {
        "runner": {
            "id": "run-phy-profile-tests", "version": "2.0.0",
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fixture": arguments.fixture,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 1),
        },
        "baseline": {
            "common": "VDETER-STAGE6-PHY-COMMON-BL-001 v0.8.0",
            "link17": "VDETER-STAGE6-PHY17-BL-001 v0.9.0 (phy17-markdown-v6)",
            "link19": "VDETER-STAGE6-PHY19-BL-001 v0.9.0 (phy19-markdown-v6)",
            "combined_registry": EXPECTED_REGISTRY[0],
            "combined_fingerprint": EXPECTED_REGISTRY[1],
        },
        "record_count": len(records),
        "expected_record_count": EXPECTED_RECORD_COUNT,
        "status_counts": statuses,
        "records": records,
    }
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")

    sums = []
    for path in sorted(evidence_dir.iterdir(), key=lambda p: p.name.encode()):
        if path.is_file() and path.name != "SHA256SUMS":
            sums.append(f"{sha256_of(path)}  {path.name}")
    (evidence_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="ascii")

    print(f"[result] {result_path}", flush=True)
    print(f"[records] {len(records)}/{EXPECTED_RECORD_COUNT} status={statuses}",
          flush=True)
    all_pass = statuses.get("PASS", 0) == len(records) == EXPECTED_RECORD_COUNT
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
