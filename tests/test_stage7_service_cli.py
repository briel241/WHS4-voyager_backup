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

import hashlib
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
    # 발급 도구의 무덮어쓰기 금지 때문에 profile은 하위 profile/로 발행된다
    # (phase-g-formal-20260902b부터). 구 배치도 계속 지원한다.
    nested = os.path.join(_latest_phase_g(), "profile", "stage7-profile.json")
    if os.path.isfile(nested):
        return nested
    return os.path.join(_latest_phase_g(), "stage7-profile.json")


def _load_profile_copy(tmp: str, *, mutate=None) -> str:
    """profile+reference+sidecar를 임시 디렉터리로 복사하고 선택적으로 변조한다."""
    # reference 본문·sidecar는 profile과 같은 디렉터리의 사본을 쓴다
    # (로더가 qualification_reference.path를 profile 기준으로 풀기 때문).
    source_dir = os.path.dirname(_profile_path())
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
    bound_specs = (profile["_reference"]["qualification_critical_manifest"]
                   .get("function_bound_files") or {})
    # 이 시험은 USB/process/trust 게이트를 격리한다. 보존된 과거 profile의
    # Phase-G 전체-file 결속만 현재 fake 배포 source에 맞춰 새 게이트를 통과시킨다.
    for candidate, spec in bound_specs.items():
        with open(os.path.join(_REPO, candidate), encoding="utf-8") as handle:
            source = handle.read()
        spec["phase_g_file_sha256"] = hashlib.sha256(
            source.encode("utf-8")).hexdigest()
    bound = list(bound_specs)

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


class _FakeSession:
    """menu가 쓰는 persistent session 대역 (실제 SSH·RF 없음)."""

    def __init__(self, *, workload_status="PASS", quiescence_pass=True):
        self.session_id = "phase-g-service-testsession"
        self.session_dir = os.path.join(_REPO, "VDETER", "results", "phase-g",
                                        "unit-test-session")
        self.status = "READY"
        self.ready = {"phase_elapsed_s": {"rf_time_post": 1240.0},
                      "rf_time_paths": ["C1", "C2", "C3", "C4", "C5", "C6"]}
        self.marks = {"session_ready": 1300.0}
        self.workloads = []
        self.calls = []
        self.closed = None
        self.error = None
        self.close_count = 0
        self._workload_status = workload_status
        self._quiescence_pass = quiescence_pass
        self._counter = 0

    def run_workload(self, scenario_id, certificate_case="normal"):
        if self.status != "READY":
            from apps.stage7_service_layer import ServiceSessionError
            raise ServiceSessionError(f"session이 READY가 아닙니다: {self.status}")
        self._counter += 1
        self.calls.append((scenario_id, certificate_case))
        workload_id = f"{self.session_id}-w{self._counter:03d}"
        result = {
            "workload_id": workload_id, "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "status": self._workload_status,
            "wall_clock_s": 12.3,
            "quiescence": {"pass": self._quiescence_pass},
            "scenario": {"status": self._workload_status},
        }
        self.workloads.append(result)
        if not self._quiescence_pass:
            self.status = "QUIESCENCE_FAIL"
        return result

    def close(self):
        # 실제 Stage7ServiceSession.close()와 같은 계약: closed 결과로 상태를
        # 갱신한다(quiescence 실패는 유지).
        self.close_count += 1
        self.closed = {"status": "PASS", "cleanup": {"pass": True}}
        if self.status != "QUIESCENCE_FAIL":
            self.status = self.closed["status"]
        return self.closed

    def summary(self):
        return {"session_id": self.session_id, "status": self.status,
                "workload_ids": [w["workload_id"] for w in self.workloads]}


def _drive_menu(inputs, session, extra_args=()):
    """대화형 메뉴를 가짜 입력으로 구동한다. 실제 session 생성은 mock한다."""
    import builtins

    import apps.stage7_run as run_module

    original_input = builtins.input
    original_open = run_module.open_service_session
    original_report = runner._run_and_report
    supplied = iter(inputs)
    opened = []

    def fake_input(_prompt=""):
        try:
            return next(supplied)
        except StopIteration:
            raise EOFError

    def fake_open(profile, **kwargs):
        opened.append(kwargs)
        os.makedirs(session.session_dir, exist_ok=True)
        return session

    def forbidden_direct(*_a, **_k):
        raise AssertionError("stage7 메뉴가 direct 시나리오로 fallback했다")

    builtins.input = fake_input
    run_module.open_service_session = fake_open
    runner._run_and_report = forbidden_direct
    try:
        rc = runner.main(["menu", "--transport", "stage7",
                          "--profile", _profile_path(), *extra_args])
    finally:
        builtins.input = original_input
        run_module.open_service_session = original_open
        runner._run_and_report = original_report
    return rc, opened


def test_menu_opens_one_session_and_dispatches_workloads():
    session = _FakeSession()
    rc, opened = _drive_menu(["2", "SC-VDE-006", "q"], session)
    assert rc == 0
    # 하드웨어 session은 한 번만 열린다.
    assert len(opened) == 1
    assert session.calls == [("SC-VDE-005", "normal"), ("SC-VDE-006", "normal")]
    # 종료 시 cleanup은 정확히 한 번.
    assert session.close_count == 1


def test_menu_certificate_case_selection():
    session = _FakeSession()
    rc, _ = _drive_menu(["1", "forged", "1", "", "q"], session)
    assert rc == 0
    assert session.calls == [("SC-VDE-001", "forged"), ("SC-VDE-001", "normal")]


def test_menu_rejects_bad_input_without_rf():
    session = _FakeSession()
    rc, _ = _drive_menu(["9", "SC-VDE-999", "hello", "q"], session)
    assert rc == 0
    assert session.calls == [], "잘못된 입력이 RF workload를 유발했다"
    assert session.close_count == 1


def test_menu_bad_certificate_case_rejected_without_rf():
    session = _FakeSession()
    rc, _ = _drive_menu(["1", "bogus", "q"], session)
    assert rc == 0 and session.calls == []


def test_menu_fresh_workload_id_per_execution():
    session = _FakeSession()
    _drive_menu(["2", "2", "2", "q"], session)
    ids = [w["workload_id"] for w in session.workloads]
    assert len(ids) == 3 and len(set(ids)) == 3


def test_menu_a_runs_independent_children_in_one_session():
    session = _FakeSession()
    rc, opened = _drive_menu(["a", "q"], session)
    assert rc == 0
    assert len(opened) == 1, "batch가 hardware session을 다시 열었다"
    assert session.calls == [("SC-VDE-005", "normal"), ("SC-VDE-006", "normal"),
                             ("SC-VDE-001", "normal"), ("SC-VDE-001", "forged")]
    # 공유 session controller(scenario_id=all)를 절대 호출하지 않는다.
    assert all(scenario != "all" for scenario, _ in session.calls)
    assert session.close_count == 1


def test_menu_quiescence_failure_stops_and_cleans_up():
    session = _FakeSession(quiescence_pass=False)
    rc, _ = _drive_menu(["a", "q"], session)
    assert rc == 1
    # 첫 child에서 quiescence 실패 → 다음 child를 시작하지 않는다.
    assert session.calls == [("SC-VDE-005", "normal")]
    assert session.close_count == 1


def test_menu_eof_still_cleans_up():
    session = _FakeSession()
    rc, _ = _drive_menu([], session)   # 입력 없음 → EOF
    assert session.close_count == 1


def test_menu_exception_still_cleans_up():
    session = _FakeSession()

    def explode(*_a, **_k):
        raise RuntimeError("workload 폭발")

    session.run_workload = explode
    rc, _ = _drive_menu(["2", "q"], session)
    assert rc == 1
    assert session.close_count == 1


def test_menu_verbose_and_diagnostics_flags_reach_session():
    session = _FakeSession()
    rc, opened = _drive_menu(["q"], session, extra_args=("--diagnostics",))
    assert rc == 0
    assert opened and opened[0].get("diagnostics") is True


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
