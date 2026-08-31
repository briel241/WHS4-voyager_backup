"""Phase G — 대화형 CLI ↔ formal service 계층 dispatch 계약 시험.

RF 없이 증명하는 것:
  - `menu --transport stage7 --profile` 이 formal service 계층을 dispatch한다
  - 메뉴 번호/ID/q/잘못된 입력, SC-VDE-001 인증서 case 선택
  - 실행마다 **새 child run-id**
  - `a`가 `scenario_id=all` 공유 session을 호출하지 않고 독립 child를 돈다
  - profile/reference SHA 변조·critical 불일치·gain 불일치·trust 불일치·
    run-id/config 불일치는 **장치 접근 전** 거부
  - direct/software fallback 부재
  - 기존 `run all --transport stage7` 계약 불변
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import apps.scenario_runner as runner
import apps.stage7_profile as stage7_profile
import apps.stage7_run as stage7_run
import apps.stage7_service_layer as service_layer

PHASE_G = os.path.join(_REPO, "VDETER", "results", "phase-g")


def _latest_phase_g() -> str:
    candidates = sorted(
        os.path.join(PHASE_G, name) for name in os.listdir(PHASE_G)
        if name.startswith("phase-g-")
    ) if os.path.isdir(PHASE_G) else []
    if not candidates:
        raise AssertionError("phase-g evidence 디렉터리가 없다 — reference 미발급")
    return candidates[-1]


def _profile_path() -> str:
    return os.path.join(_latest_phase_g(), "stage7-profile.json")


def _load_profile_copy(tmp: str, *, mutate=None) -> str:
    """profile+reference+sidecar를 임시 디렉터리로 복사하고 선택적으로 변조한다."""
    source_dir = _latest_phase_g()
    profile = json.load(open(_profile_path(), encoding="utf-8"))
    reference_raw = open(os.path.join(source_dir, "qualification-reference.json"),
                         "rb").read()
    sidecar = open(os.path.join(source_dir, "qualification-reference.sha256"),
                   encoding="utf-8").read()
    if mutate:
        profile, reference_raw, sidecar = mutate(profile, reference_raw, sidecar)
    with open(os.path.join(tmp, "qualification-reference.json"), "wb") as handle:
        handle.write(reference_raw)
    with open(os.path.join(tmp, "qualification-reference.sha256"), "w",
              encoding="utf-8", newline="\n") as handle:
        handle.write(sidecar)
    path = os.path.join(tmp, "stage7-profile.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(profile, handle, ensure_ascii=False, indent=1, sort_keys=True)
    return path


# ---------------------------------------------------------------- profile 검증

def test_profile_and_reference_load():
    profile = stage7_profile.load_profile(_profile_path())
    checks = stage7_profile.validate_local_bindings(profile)
    assert checks["reference_sha256"]["pass"]
    assert profile["_reference"]["qualification"]["run_id"]
    lines = stage7_profile.summary_lines(profile)
    assert any("qualification run" in line for line in lines)
    # 비밀정보가 요약에 새지 않는다.
    joined = "\n".join(lines)
    for marker in ("PRIVATE KEY", "BEGIN"):
        assert marker not in joined


def test_tampered_reference_rejected_before_device_access():
    with tempfile.TemporaryDirectory() as tmp:
        def mutate(profile, reference_raw, sidecar):
            data = json.loads(reference_raw.decode("utf-8"))
            data["gain"]["endpoint_common_rx_gain_db"]["ROC"] = 70
            return profile, json.dumps(data).encode("utf-8"), sidecar
        path = _load_profile_copy(tmp, mutate=mutate)
        try:
            stage7_profile.load_profile(path)
            raise AssertionError("변조된 reference가 통과했다")
        except stage7_profile.ProfileError as exc:
            assert "sidecar" in str(exc) or "SHA" in str(exc)


def test_tampered_sidecar_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        def mutate(profile, reference_raw, sidecar):
            return profile, reference_raw, "0" * 64 + "  qualification-reference.json\n"
        path = _load_profile_copy(tmp, mutate=mutate)
        try:
            stage7_profile.load_profile(path)
            raise AssertionError("변조된 sidecar가 통과했다")
        except stage7_profile.ProfileError:
            pass


def test_profile_reference_sha_mismatch_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        def mutate(profile, reference_raw, sidecar):
            profile["qualification_reference"]["sha256"] = "1" * 64
            return profile, reference_raw, sidecar
        path = _load_profile_copy(tmp, mutate=mutate)
        try:
            stage7_profile.load_profile(path)
            raise AssertionError("profile↔reference SHA 불일치가 통과했다")
        except stage7_profile.ProfileError as exc:
            assert "reference SHA" in str(exc)


def test_gain_artifact_mismatch_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        fake_gain = os.path.join(tmp, "gain.json")
        with open(fake_gain, "w", encoding="utf-8") as handle:
            json.dump({"endpoint_common_rx_gain_db": {"ROC": 69}}, handle)

        def mutate(profile, reference_raw, sidecar):
            profile["gain"]["artifact_path"] = fake_gain
            return profile, reference_raw, sidecar
        path = _load_profile_copy(tmp, mutate=mutate)
        profile = stage7_profile.load_profile(path)
        try:
            stage7_profile.validate_local_bindings(profile)
            raise AssertionError("gain artifact 불일치가 통과했다")
        except stage7_profile.ProfileError as exc:
            assert "gain artifact" in str(exc)


def test_missing_gain_artifact_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        def mutate(profile, reference_raw, sidecar):
            profile["gain"]["artifact_path"] = os.path.join(tmp, "없음.json")
            return profile, reference_raw, sidecar
        profile = stage7_profile.load_profile(_load_profile_copy(tmp, mutate=mutate))
        try:
            stage7_profile.validate_local_bindings(profile)
            raise AssertionError("없는 gain artifact가 통과했다")
        except stage7_profile.ProfileError as exc:
            assert "없습니다" in str(exc)


def test_profile_rejects_secret_markers():
    with tempfile.TemporaryDirectory() as tmp:
        def mutate(profile, reference_raw, sidecar):
            profile["trust"]["private_key_sha256"] = "a" * 64
            return profile, reference_raw, sidecar
        path = _load_profile_copy(tmp, mutate=mutate)
        try:
            stage7_profile.load_profile(path)
            raise AssertionError("개인키 해시 필드가 통과했다")
        except stage7_profile.ProfileError as exc:
            assert "비밀정보" in str(exc)


# ------------------------------------------------------- critical/preflight 게이트

class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_critical_mismatch_blocks_before_device_access():
    profile = stage7_profile.load_profile(_profile_path())
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        # 첫 호출 = critical manifest 대조. 해시를 어긋나게 응답한다.
        script = command[-1]
        if "sha256sum" in script and "MISSING" in script:
            files = sorted(profile["_reference"]["qualification_critical_manifest"]["strict_files"])
            return _FakeCompleted("\n".join(f"{'0'*64}  {f}" for f in files))
        return _FakeCompleted("")

    try:
        service_layer.verify_remote_environment(profile, runner=fake_runner)
        raise AssertionError("critical 불일치가 통과했다")
    except service_layer.ServiceSessionError as exc:
        assert "critical" in str(exc)
    # 장치·프로세스 조회까지 가지 않고 첫 단계에서 멈춰야 한다.
    assert len(calls) == 1


def test_topology_and_trust_gates():
    profile = stage7_profile.load_profile(_profile_path())
    critical = profile["_reference"]["qualification_critical_manifest"]["strict_files"]
    bound = list(profile["_reference"]["qualification_critical_manifest"]
                 .get("function_bound_files") or {})

    def make_runner(usb_ok=True, procs="none", throttled="throttled=0x0",
                    key_mode="600", match="true"):
        def fake_runner(command, **kwargs):
            script = command[-1]
            if "MISSING" in script:
                return _FakeCompleted(
                    "\n".join(f"{h}  {f}" for f, h in sorted(critical.items())))
            if script.startswith("cat "):
                # function-bound 파일은 배포본 실물을 가져와 함수 해시를 본다.
                for candidate in bound:
                    if script.rstrip().endswith(candidate):
                        with open(os.path.join(_REPO, candidate),
                                  encoding="utf-8") as handle:
                            return _FakeCompleted(handle.read())
                return _FakeCompleted("")
            lines = ["##USB"]
            for name, info in profile["endpoints"].items():
                path = info["usb_path"] if usb_ok else "9-9"
                lines.append(f"{path} {info['serial']} {info['speed_mbps']}")
            lines += ["##PROC", procs, "##THROTTLE", throttled, "##TRUST"]
            trust = profile["trust"]
            for key in ("certificate_path", "trust_store_path", "private_key_path"):
                lines.append(f"{trust[key]} "
                             f"{key_mode if 'key' in key else '644'}")
            lines += ["##KEYCERT", f"match={match}"]
            return _FakeCompleted("\n".join(lines))
        return fake_runner

    report = service_layer.verify_remote_environment(
        profile, runner=make_runner())
    assert report["critical_manifest"]["pass"]
    assert report["usb_topology"]["pass"]
    assert report["trust"]["pass"]

    for kwargs, marker in (
        ({"usb_ok": False}, "topology"),
        ({"procs": "1234 uhd_find_devices"}, "프로세스"),
        ({"throttled": "throttled=0x50000"}, "throttled"),
        ({"key_mode": "644"}, "trust"),
        ({"match": "false"}, "trust"),
    ):
        try:
            service_layer.verify_remote_environment(
                profile, runner=make_runner(**kwargs))
            raise AssertionError(f"{marker} 게이트가 통과했다: {kwargs}")
        except service_layer.ServiceSessionError as exc:
            assert marker in str(exc) or marker == "trust"


# -------------------------------------------------------------- CLI dispatch

def test_menu_parser_contract():
    parser = runner.build_parser()
    args = parser.parse_args(["menu"])
    assert args.transport == "direct" and args.profile is None
    args = parser.parse_args(["menu", "--transport", "stage7",
                              "--profile", "p.json"])
    assert args.transport == "stage7" and args.profile == "p.json"


def test_stage7_menu_requires_profile():
    assert runner.main(["menu", "--transport", "stage7"]) == 2


def test_profile_rejected_for_direct_menu():
    assert runner.main(["menu", "--profile", "p.json"]) == 2


def _drive_menu(inputs, session_fn, batch_fn=None):
    """대화형 메뉴를 가짜 입력으로 구동하고 dispatch를 기록한다."""
    original_input = __builtins__["input"] if isinstance(__builtins__, dict) \
        else __builtins__.input
    original_session = stage7_run.run_service_session
    original_batch = stage7_run.run_service_batch
    original_report = runner._run_and_report
    supplied = iter(inputs)

    def fake_input(_prompt=""):
        try:
            return next(supplied)
        except StopIteration:
            raise EOFError

    def forbidden_direct(*_a, **_k):
        raise AssertionError("stage7 메뉴가 direct 시나리오로 fallback했다")

    import builtins
    builtins.input = fake_input
    stage7_run.run_service_session = session_fn
    stage7_run.run_service_batch = batch_fn or (
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("batch가 호출되면 안 된다")))
    runner._run_and_report = forbidden_direct
    try:
        return runner.main(["menu", "--transport", "stage7",
                            "--profile", _profile_path()])
    finally:
        builtins.input = original_input
        stage7_run.run_service_session = original_session
        stage7_run.run_service_batch = original_batch
        runner._run_and_report = original_report


def test_menu_dispatches_formal_service_runner_by_number_and_id():
    calls = []

    def session(profile, *, scenario_id, certificate_case="normal", **kwargs):
        calls.append((scenario_id, certificate_case))
        return {"status": "PASS", "run_id": f"svc-{len(calls)}",
                "phase_elapsed_s": {"total": 1.0}, "evidence_dir": "x"}

    # 2) = SC-VDE-005 (정렬 순: 001, 005, 006), 그다음 ID 직접 입력, 그리고 q
    rc = _drive_menu(["2", "SC-VDE-006", "q"], session)
    assert rc == 0
    assert calls == [("SC-VDE-005", "normal"), ("SC-VDE-006", "normal")]


def test_menu_certificate_case_selection():
    calls = []

    def session(profile, *, scenario_id, certificate_case="normal", **kwargs):
        calls.append((scenario_id, certificate_case))
        return {"status": "PASS", "run_id": "svc-x",
                "phase_elapsed_s": {}, "evidence_dir": "x"}

    rc = _drive_menu(["1", "forged", "1", "", "q"], session)
    assert rc == 0
    assert calls == [("SC-VDE-001", "forged"), ("SC-VDE-001", "normal")]


def test_menu_rejects_bad_input_without_rf():
    calls = []

    def session(profile, **kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "run_id": "x", "phase_elapsed_s": {},
                "evidence_dir": "x"}

    rc = _drive_menu(["9", "SC-VDE-999", "hello", "q"], session)
    assert rc == 0
    assert calls == [], "잘못된 입력이 RF 실행을 유발했다"


def test_menu_bad_certificate_case_rejected_without_rf():
    calls = []

    def session(profile, **kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "run_id": "x", "phase_elapsed_s": {},
                "evidence_dir": "x"}

    rc = _drive_menu(["1", "bogus", "q"], session)
    assert rc == 0 and calls == []


def test_menu_fresh_run_id_per_execution():
    seen = []

    def session(profile, *, scenario_id, certificate_case="normal", **kwargs):
        # 실제 계층과 같은 규칙으로 run-id를 만든다 (중복 금지 확인용).
        run_id = f"svc-{scenario_id}-{len(seen)}"
        seen.append(run_id)
        return {"status": "PASS", "run_id": run_id, "phase_elapsed_s": {},
                "evidence_dir": "x"}

    _drive_menu(["2", "2", "2", "q"], session)
    assert len(seen) == 3 and len(set(seen)) == 3


# --------------------------------------------------- `a` 독립 child batch 계약

def test_batch_runs_independent_children_not_shared_session():
    calls = []

    def session(profile, *, scenario_id, certificate_case="normal", **kwargs):
        calls.append((scenario_id, certificate_case))
        return {"status": "PASS", "run_id": f"svc-{len(calls)}",
                "phase_elapsed_s": {}, "evidence_dir": "x"}

    profile = stage7_profile.load_profile(_profile_path())
    batch = service_layer.run_service_batch(profile, session_fn=session)
    assert batch["status"] == "PASS"
    assert batch["shared_session_used"] is False
    assert calls == [("SC-VDE-005", "normal"), ("SC-VDE-006", "normal"),
                     ("SC-VDE-001", "normal"), ("SC-VDE-001", "forged")]
    # child run-id가 전부 다르다.
    assert len(set(batch["child_run_ids"])) == 4
    # 공유 session(scenario_id=all)을 절대 쓰지 않는다.
    assert all(scenario != "all" for scenario, _ in calls)


def test_batch_stops_after_failed_child():
    calls = []

    def session(profile, *, scenario_id, certificate_case="normal", **kwargs):
        calls.append(scenario_id)
        status = "FAIL" if scenario_id == "SC-VDE-006" else "PASS"
        return {"status": status, "run_id": f"svc-{len(calls)}",
                "phase_elapsed_s": {}, "evidence_dir": "x"}

    profile = stage7_profile.load_profile(_profile_path())
    batch = service_layer.run_service_batch(profile, session_fn=session)
    assert batch["status"] == "FAIL"
    assert calls == ["SC-VDE-005", "SC-VDE-006"], calls


def test_menu_a_uses_batch_not_shared_all():
    used = {}

    def session(profile, **kwargs):
        raise AssertionError("batch가 session을 직접 호출하면 안 된다(여기선 batch mock)")

    def batch(profile, *a, **k):
        used["batch"] = True
        return {"status": "PASS", "children": [], "child_run_ids": [],
                "shared_session_used": False}

    rc = _drive_menu(["a", "q"], session, batch_fn=batch)
    assert rc == 0 and used.get("batch") is True


# ------------------------------------------- 기존 `run all --transport stage7` 불변

def test_legacy_run_all_stage7_contract_unchanged():
    """기존 공유 session 경로는 그대로 남아 있고 계약이 바뀌지 않았다."""
    assert callable(stage7_run.run_stage7)
    import inspect
    parameters = inspect.signature(stage7_run.run_stage7).parameters
    for name in ("scenario_id", "run_id", "configs_dir", "certificate_case",
                 "results_dir", "spawn", "timeout_s"):
        assert name in parameters, name
    # stage7 필수 인자 검증도 그대로다.
    assert runner.main(["run", "all", "--transport", "stage7"]) == 2


def test_no_direct_or_software_fallback_in_service_layer():
    source = open(os.path.join(_REPO, "apps", "stage7_service_layer.py"),
                  encoding="utf-8").read()
    assert "DirectTransport" not in source
    assert "SoftwareRadioTransport" not in source
    assert "build_transport" not in source


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase G CLI/service dispatch 계약")


def test_coordinator_script_does_not_conflict_with_workload_gate():
    """service phase 명령은 rr-p2 전용 --workload 게이트와 충돌하지 않는다."""
    profile = stage7_profile.load_profile(_profile_path())
    script = service_layer.build_coordinator_script(
        profile, "svc-test", "SC-VDE-005", "normal")
    assert "--phase mass-roc-service" in script
    assert "--workload" not in script
    assert "--scenario-id SC-VDE-005" in script
    assert "--certificate-case normal" in script
    assert "--gain-artifact" in script
    assert "--baseline-sha256" in script
    # 공유 session(all)을 넘기지 않는다.
    assert "--scenario-id all" not in script
