"""Phase I W5 — 단독 RR 자격 orchestrator 계약 시험 (RF 없음).

증명하는 것 (apps/standalone/README.md §7, 결정 20)
  사다리     : P0→P1(+gain)→candidate→P2-17→P2-19→P3-fault→final builder→
               전체 PASS 시에만 원자적 승격. 단계 실패 시 기존 active 유지.
  발급       : reference가 **실제 검증기**(apps.stage7_profile.load_reference)
               를 통과하고, final builder는 **실물 도구**를 head_provider
               주입으로 실행한다 (가짜 builder 아님 — 승인 조건 2·3).
  frozen     : git 없는 로컬 frozen 감사가 binding 대조로 all_unchanged를
               정직 판정.
  identity   : manifest·인증서·trust store·fingerprint 4자 일치 검사
               (W5 필수 — 불일치 시 자격 착수 거부).

가짜인 것은 **RF phase 실행뿐**이다 — 주입 phase_runner가 coordinator의
evidence 계약(write_rr_evidence 스키마·index·원본 결속·gain artifact)을
그대로 재현해 실물 final builder의 전 검사를 통과시킨다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import (
    deploy_manifest,
    identity,
    qualify_cmd,
    state,
)


class Done:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout


def normalized_sha(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def make_real_binding() -> dict:
    """실제 repo의 frozen 파일 2개를 실측 결속한 binding (0.9.23 사슬 내)."""
    frozen = {}
    for relative in ("src/vdeter/config/models.py",
                     "src/vdeter/config/loader.py"):
        frozen[relative] = normalized_sha(
            Path(_REPO) / "VDETER" / relative)
    return {
        "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
        "source_head": "e" * 40,
        "source_branch": "feature/phase-i-pi-standalone",
        "source_worktree_dirty": False,
        "baseline_version": "0.9.23",
        "baseline_sha256": "2a" * 32,
        "frozen_files": frozen,
    }


HARDWARE = {
    "schema_version": 1,
    "platform_profile": "pi",
    "endpoints": {
        "MASS-A": {"serial": "34D03DD"},
        "MASS-B": {"serial": "3515425", "external_power_asset": "AT2024T"},
        "ROC": {"serial": "34D03E8"},
    },
    "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
}


def write_hardware_toml(path: Path):
    lines = ["schema_version = 1", 'platform_profile = "pi"']
    for role, body in HARDWARE["endpoints"].items():
        lines.append(f'[endpoints."{role}"]')
        for key, value in body.items():
            lines.append(f'{key} = "{value}"')
    lines += ["[wiring]", 'profile = "C1-C6-STANDARD"', "approved = true"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fake_preflight():
    return {
        "pass": True, "failures": [],
        "checks": {"b210": {"observed_by_role": {
            "MASS-A": {"serial": "34D03DD", "usb_path": "3-1",
                       "speed_mbps": 5000, "speed_class": "super"},
            "MASS-B": {"serial": "3515425", "usb_path": "4-2",
                       "speed_mbps": 480, "speed_class": "high"},
            "ROC": {"serial": "34D03E8", "usb_path": "5-1",
                    "speed_mbps": 5000, "speed_class": "super"},
        }}},
    }


ENV_MANIFEST = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": "unit-test",
    "usb_devices": "X",
    "numpy_version": "2.5.2",
    "cryptography_version": "50.0.0",
    "uhd_version": "4.6",
    "gnuradio_version": "3.10",
}

BINDING = make_real_binding()


def fabricating_phase_runner(context: dict) -> int:
    """coordinator의 evidence 계약을 재현하는 가짜 RR phase 실행기."""
    run_root = Path(context["run_root"])
    phase = context["phase"]
    run_id = context["run_id"]
    evidence = run_root / "evidence"
    research = evidence / "research_ready"
    research.mkdir(parents=True, exist_ok=True)
    (evidence / f"coordinator-{phase}.json").write_text("{}")
    for member in ("MASS-A", "MASS-B", "ROC"):
        (evidence / f"{member}-runtime.json").write_text("{}")

    index = {"profile": "RESEARCH_READY_MINIMAL", "entries": {}}
    for tc in qualify_cmd.PHASE_EXPECTED_TCS[phase]:
        payload = {
            "artifact_type": "STAGE7_RESEARCH_READY_EVIDENCE_V1",
            "profile": "RESEARCH_READY_MINIMAL",
            "tc_id": tc,
            "baseline_version": BINDING["baseline_version"],
            "baseline_sha256": BINDING["baseline_sha256"],
            "git_commit": BINDING["source_head"],
            "git_dirty": False,
            "hardware": {"usb_devices": "X"},
            "run_id": run_id,
            "stage7_diag_env": "0",
            "lifecycle_generation": 1,
            "counters": {"retries": 0, "duplicates": 0},
            "status": "PASS",
            "detail": {},
        }
        data = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                          indent=1).encode()
        (research / f"{tc}.json").write_bytes(data)
        index["entries"][tc] = {
            "path": f"evidence/research_ready/{tc}.json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "status": "PASS",
        }
    (research / "index.json").write_bytes(
        json.dumps(index, ensure_ascii=True, sort_keys=True,
                   indent=1).encode())

    if phase == "rr-p1":
        artifact = {
            "artifact_type": "STAGE7_GAIN_ARTIFACT_V1",
            "run_id": run_id,
            "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69,
                                           "ROC": 75},
        }
        artifact_bytes = json.dumps(artifact, ensure_ascii=True,
                                    sort_keys=True, indent=1).encode()
        (evidence / "gain_artifact.json").write_bytes(artifact_bytes)
        gain_input = {
            "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69,
                                           "ROC": 75},
            "source_gain_artifact_sha256": hashlib.sha256(
                artifact_bytes).hexdigest(),
        }
        (evidence / "gain_artifact_input.json").write_bytes(
            json.dumps(gain_input, ensure_ascii=True, sort_keys=True,
                       indent=1).encode())
    return 0


def prepare_environment(tmp) -> state.StatePaths:
    paths = state.StatePaths(tmp)
    paths.root = Path(_REPO)  # vdeter import·template·frozen 실물 사용
    paths.ensure_var()
    identity.ensure_identity(paths)
    write_hardware_toml(Path(tmp) / "hardware.toml")
    # setup의 manifest 단계에 해당: 실제 repo tree 실측 → tmp var에 기록.
    deploy_manifest.write_manifest(_REPO,
                                   paths.var_dir / "deploy-manifest.json")
    return paths


def run(paths, tmp, **overrides):
    kwargs = dict(
        hardware_path=Path(tmp) / "hardware.toml",
        repo_root=_REPO,
        binding_path=None,
        phase_runner=fabricating_phase_runner,
        suite_runner=lambda argv, **kw: Done(0),
        preflight_fn=fake_preflight,
        residual_probe=lambda: [],
        environment_manifest=dict(ENV_MANIFEST),
        stamp="20260904-000000",
    )
    kwargs.update(overrides)
    if kwargs["binding_path"] is None:
        binding_path = Path(tmp) / "standalone-binding.json"
        binding_path.write_text(json.dumps(BINDING), encoding="utf-8")
        kwargs["binding_path"] = binding_path
    return qualify_cmd.run_qualify(paths, **kwargs)


# ---------------------------------------------------------------- 단위

def test_local_frozen_audit_honest_verdicts():
    audit = qualify_cmd.local_frozen_hash_audit(
        Path(_REPO), BINDING, deploy_verify_pass=True)
    assert audit["all_unchanged"] is True
    assert audit["worktree_dirty"] is False
    assert audit["head_commit"] == BINDING["source_head"]

    tampered = dict(BINDING)
    tampered["frozen_files"] = dict(BINDING["frozen_files"])
    first = next(iter(tampered["frozen_files"]))
    tampered["frozen_files"][first] = "0" * 64
    audit2 = qualify_cmd.local_frozen_hash_audit(
        Path(_REPO), tampered, deploy_verify_pass=True)
    assert audit2["all_unchanged"] is False
    audit3 = qualify_cmd.local_frozen_hash_audit(
        Path(_REPO), BINDING, deploy_verify_pass=False)
    assert audit3["worktree_dirty"] is True


def test_identity_consistency_four_way():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        created = identity.ensure_identity(paths)
        check = identity.check_identity_consistency(paths)
        assert check["pass"], check["errors"]
        assert check["fingerprint"] == created["fingerprint"]

        # fingerprint 파일 변조 → 4자 불일치 검출.
        files = identity.identity_paths(paths)
        files["fingerprint"].write_text("deadbeef0000\n", encoding="utf-8")
        tampered = identity.check_identity_consistency(paths)
        assert not tampered["pass"]
        assert any("불일치" in e for e in tampered["errors"])


def test_promote_candidate_pointer_atomic():
    """release 불변 보관 + 포인터 원자 교체 — 'active 없음' 창이 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        # 기존 active: release-A를 가리키는 포인터.
        (paths.qualification_releases / "rel-a").mkdir()
        (paths.qualification_releases / "rel-a" / "old.json").write_text(
            "old", encoding="utf-8")
        state.write_json_atomic(paths.active_pointer, {"release": "rel-a"})

        (paths.qualification_candidate
         / qualify_cmd.REFERENCE_BASENAME).write_text("new", encoding="utf-8")
        (paths.qualification_candidate
         / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            "ab" * 32 + "\n", encoding="utf-8")
        result = qualify_cmd.promote_candidate(paths, release_id="rel-b")
        pointer = json.loads(paths.active_pointer.read_bytes())
        assert pointer["release"] == "rel-b"
        assert pointer["previous_release"] == "rel-a"
        assert result["reference_sha256"] == "ab" * 32
        # 이전 release는 불변 이력으로 남는다.
        assert (paths.qualification_releases / "rel-a"
                / "old.json").read_text() == "old"
        assert (paths.qualification_releases / "rel-b"
                / qualify_cmd.REFERENCE_BASENAME).read_text() == "new"
        assert not any(paths.qualification_candidate.iterdir())

        # 같은 release id 재사용은 거부 (불변 보관 보장).
        (paths.qualification_candidate
         / qualify_cmd.REFERENCE_BASENAME).write_text("x", encoding="utf-8")
        (paths.qualification_candidate
         / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            "cd" * 32 + "\n", encoding="utf-8")
        try:
            qualify_cmd.promote_candidate(paths, release_id="rel-b")
            raise AssertionError("release 중복이 거부되지 않았다")
        except qualify_cmd.QualifyError:
            pass
        # 실패해도 포인터는 그대로다.
        assert json.loads(
            paths.active_pointer.read_bytes())["release"] == "rel-b"


# ---------------------------------------------------------------- e2e

def test_qualify_end_to_end_promotes_real_validated_reference():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        report = run(paths, tmp)
        assert report["status"] == "QUALIFIED", (
            report["errors"], report.get("consistency_problems"))
        # 실물 final builder가 READY를 냈다 (가짜 builder 아님).
        assert report["final_status"] == "RESEARCH_READY"
        assert report["consistency_problems"] == []
        # 사다리 5 phase 전부 PASS·TC 매핑 확인.
        assert list(report["phases"]) == list(qualify_cmd.LADDER_PHASES)

        # active reference가 실제 검증기(load_reference)로 재적재된다.
        active = qualify_cmd.load_active_reference(paths)
        assert active is not None
        assert active["qualified_source_commit"] == BINDING["source_head"]
        tc = active["qualification"]["tc_results"]
        assert tc["TC-STAGE7-RR-007"]["status"] == "PASS"
        critical = active["standalone_critical"]
        assert critical["gain"]["endpoint_common_rx_gain_db"]["ROC"] == 75
        assert critical["identity_fingerprint"] == identity.\
            load_identity_manifest(paths)["certificate_fingerprint"]
        assert critical["library_versions"]["cryptography"] == "50.0.0"
        assert len(critical["deploy_manifest_sha256"]) == 64
        # candidate는 승격 후 비워지고, 포인터가 release를 가리킨다.
        assert not any(paths.qualification_candidate.iterdir())
        pointer = json.loads(paths.active_pointer.read_bytes())
        assert pointer["release"] == "qual-20260904-000000"
        final = json.loads(
            (paths.qualification_releases / pointer["release"]
             / "final-result.json").read_bytes())
        assert final["final_status"] == "RESEARCH_READY"


def test_qualify_phase_failure_preserves_active_pointer():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        # 기존 active: release-prev를 가리키는 포인터 (내용은 불변 이력).
        (paths.qualification_releases / "rel-prev").mkdir()
        state.write_json_atomic(paths.active_pointer,
                                {"release": "rel-prev"})

        def failing_runner(context):
            if context["phase"] == "rr-p2-17":
                return 1  # RF phase 실패 모사
            return fabricating_phase_runner(context)

        report = run(paths, tmp, phase_runner=failing_runner)
        assert report["status"] == "FAIL"
        assert any("기존 active reference 유지" in e
                   for e in report["errors"])
        # p2-19·p3-fault는 시작하지 않았다 (단계 실패 즉시 중단).
        assert "rr-p2-19" not in report["phases"]
        # 포인터는 이전 release 그대로 — 승격되지 않았다.
        assert json.loads(
            paths.active_pointer.read_bytes())["release"] == "rel-prev"


def test_qualify_residual_and_timeout_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        # phase 후 잔존 프로세스 발견 → 자격 실패 (정리 미확인).
        report = run(paths, tmp, residual_probe=lambda: ["888 launcher"])
        assert report["status"] == "FAIL"
        assert any("정리 미확인" in e for e in report["errors"])
        assert not paths.active_pointer.exists()

    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)

        def timing_out_runner(context):
            if context["phase"] == "rr-p0":
                return {"rc": None, "timed_out": True,
                        "escalation": ["term-group", "kill-group"]}
            raise AssertionError("timeout 후 다음 phase가 시작됐다")

        report = run(paths, tmp, phase_runner=timing_out_runner)
        assert report["status"] == "FAIL"
        assert any("timeout" in e for e in report["errors"])
        assert report["phases"]["rr-p0"]["escalation"] == [
            "term-group", "kill-group"]


def test_qualify_refuses_missing_library_versions_before_p0():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        calls = []

        def recording_runner(context):
            calls.append(context["phase"])
            return fabricating_phase_runner(context)

        broken_env = dict(ENV_MANIFEST)
        broken_env["uhd_version"] = None
        try:
            run(paths, tmp, phase_runner=recording_runner,
                environment_manifest=broken_env)
            raise AssertionError("라이브러리 부재가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "uhd" in str(exc)
        assert calls == []  # RF phase는 시작조차 하지 않았다


def test_verification_suite_receives_timeout():
    seen = []

    def suite_runner(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return Done(0)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "verification"
        qualify_cmd.write_verification_bundle(
            Path(_REPO), out, BINDING, deploy_verify_pass=True,
            runner=suite_runner)
        assert seen == [qualify_cmd.UNIT_CONTRACT_TIMEOUT_S,
                        qualify_cmd.INTEGRATION_TIMEOUT_S]


def test_run_child_bounded_reaps_on_timeout():
    """실제 자식(60초 sleep)을 1초 timeout으로 — 종료·회수를 실증한다."""
    # Windows: kill 직후 자식의 로그 핸들 해제가 반환보다 늦을 수 있어
    # tmp 정리 오류만 허용한다 (검증 대상은 회수·escalation이다).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        result = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "child.log",
            timeout_s=1.0, term_wait_s=5.0, kill_wait_s=5.0)
        assert result["timed_out"] is True
        assert result["rc"] is not None  # 회수 완료 (좀비 아님)
        assert result["escalation"]  # TERM 계열이 실제로 나갔다
        if os.name == "posix":
            assert result["group_cleared"] is True
        else:
            assert result["group_cleared"] is None  # 비-POSIX 개발 환경
        assert result["cleanup_anomaly"] is False  # timeout 경로 — 정상 아님


def test_run_child_bounded_reaps_grandchildren_posix():
    """부모가 손자를 만들고 먼저 죽는 실제 POSIX 시나리오 (원 결함 재현).

    Windows 개발 환경에서는 SKIP — V2(Pi) 검증 캠페인에서 실행된다."""
    import unittest
    if os.name != "posix":
        raise unittest.SkipTest("POSIX 전용 — Pi V2에서 실행")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # ① 부모 즉시 종료 + TERM 무시 손자 → 자연 소멸 대기 후
        #    anomaly 기록 + 강제 정리 + 소멸 재확인 (W5 보완 1·3차).
        #    부모는 손자 PID를 stdout(log)으로 남긴다 — 실제 소멸 검증용.
        orphan_parent = (
            "import subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,"
            "signal.SIG_IGN);time.sleep(60)']);"
            "print(p.pid,flush=True)"
        )
        result = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", orphan_parent],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "a.log",
            timeout_s=30.0, term_wait_s=2.0, kill_wait_s=5.0,
            natural_group_wait_s=1.0)
        assert result["rc"] == 0
        assert result["cleanup_anomaly"] is True   # 잔류가 실측·기록됐다
        assert result["group_cleared"] is True     # 강제 정리·소멸 재확인
        assert "post-exit-group-residue" in result["escalation"]
        # 손자 PID가 시험 종료 전에 실제로 소멸했는지 확인.
        grandchild_pid = int(
            (Path(tmp) / "a.log").read_text().strip().splitlines()[-1])
        try:
            os.kill(grandchild_pid, 0)
            raise AssertionError(f"손자 {grandchild_pid}가 아직 살아 있다")
        except ProcessLookupError:
            pass

        # ② timeout 경로: TERM 무시 손자는 KILL 단계에서 group째 소멸한다.
        stubborn = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',"
            "'import signal,time;signal.signal(signal.SIGTERM,"
            "signal.SIG_IGN);time.sleep(60)']);time.sleep(60)"
        )
        result2 = qualify_cmd.run_child_bounded(
            [sys.executable, "-c", stubborn],
            cwd=Path(tmp), env=dict(os.environ),
            stdout_path=Path(tmp) / "b.log",
            timeout_s=1.0, term_wait_s=3.0, kill_wait_s=5.0)
        assert result2["timed_out"] is True
        assert result2["group_cleared"] is True  # KILL로 group 소멸 확인
        assert "group-survived-term" in result2["escalation"]


def minimal_valid_reference() -> dict:
    """load_reference 검증을 통과하는 최소 reference (포인터 결속 시험용)."""
    return {
        "artifact_type": "STAGE7_QUALIFICATION_REFERENCE",
        "schema_version": 1,
        "qualified_source_commit": "e" * 40,
        "qualification": {
            "run_id": "rel-test",
            "tc_results": {
                "TC-STAGE7-RR-002": {"status": "PASS"},
                "TC-STAGE7-RR-003": {"status": "PASS"},
                "TC-STAGE7-RR-004": {"status": "PASS"},
            },
        },
        "qualification_critical_manifest": {
            "strict_files": {"apps/x.py": "ab" * 32},
        },
    }


def test_active_pointer_binds_reference_sha():
    """release 안에서 reference+sidecar를 함께 바꿔도 포인터 SHA가 막는다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        qualify_cmd.write_reference(paths.qualification_candidate,
                                    minimal_valid_reference())
        qualify_cmd.promote_candidate(paths, release_id="rel-1")
        assert qualify_cmd.load_active_reference(paths) is not None

        # reference와 sidecar를 **함께** 변조 (쌍은 서로 일치하게).
        release = paths.qualification_releases / "rel-1"
        forged = minimal_valid_reference()
        forged["qualification"]["run_id"] = "forged"
        encoded = json.dumps(forged, ensure_ascii=True, sort_keys=True,
                             indent=1).encode()
        (release / qualify_cmd.REFERENCE_BASENAME).write_bytes(encoded)
        (release / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
            hashlib.sha256(encoded).hexdigest() + "\n", encoding="utf-8")
        # sidecar는 일치하지만 포인터 SHA와 달라 UNQUALIFIED.
        assert qualify_cmd.load_active_reference(paths) is None

        # 포인터 자체의 스키마·SHA 형식도 검증된다.
        pointer = json.loads(paths.active_pointer.read_bytes())
        pointer["artifact_type"] = "WRONG"
        paths.active_pointer.write_text(json.dumps(pointer),
                                        encoding="utf-8")
        assert qualify_cmd.load_active_pointer(paths) is None
        pointer["artifact_type"] = "STAGE7_STANDALONE_ACTIVE_POINTER_V1"
        pointer["reference_sha256"] = "not-a-sha"
        paths.active_pointer.write_text(json.dumps(pointer),
                                        encoding="utf-8")
        assert qualify_cmd.load_active_pointer(paths) is None


def test_qualify_group_uncleared_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)

        def gc_false_runner(context):
            return {"rc": 0, "timed_out": False,
                    "escalation": ["term-group", "group-survived-kill"],
                    "group_cleared": False, "cleanup_anomaly": False}

        report = run(paths, tmp, phase_runner=gc_false_runner)
        assert report["status"] == "FAIL"
        assert any("group 소멸 미확인" in e for e in report["errors"])
        assert not paths.active_pointer.exists()


def test_qualify_cleanup_anomaly_fails_even_when_cleared():
    """정리는 됐어도(group_cleared=True) 잔류가 있었으면 자격 실패다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)

        def anomaly_runner(context):
            fabricating_phase_runner(context)  # evidence는 PASS로 만들어도
            return {"rc": 0, "timed_out": False,
                    "escalation": ["post-exit-group-residue", "term-group",
                                   "kill-group"],
                    "group_cleared": True, "cleanup_anomaly": True}

        report = run(paths, tmp, phase_runner=anomaly_runner)
        assert report["status"] == "FAIL"
        assert any("cleanup_anomaly" in e for e in report["errors"])
        assert not paths.active_pointer.exists()


def test_qualify_refuses_identity_mismatch_and_missing_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        paths = prepare_environment(tmp)
        files = identity.identity_paths(paths)
        files["fingerprint"].write_text("deadbeef0000\n", encoding="utf-8")
        try:
            run(paths, tmp)
            raise AssertionError("identity 불일치가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "identity" in str(exc)

    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.root = Path(_REPO)
        paths.ensure_var()
        identity.ensure_identity(paths)
        write_hardware_toml(Path(tmp) / "hardware.toml")
        try:
            run(paths, tmp)  # deploy manifest 없음
            raise AssertionError("manifest 부재가 거부되지 않았다")
        except qualify_cmd.QualifyError as exc:
            assert "vdeter setup" in str(exc)


if __name__ == "__main__":
    import unittest as _unittest
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            try:
                value()
            except _unittest.SkipTest as reason:
                print(f"  {name} SKIP ({reason})")
                continue
            print(f"  {name} OK")
    print("PASS: Phase I W5 standalone qualify 계약")
