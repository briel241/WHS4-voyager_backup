"""Stage 7 host-side fail-closed regression tests (SSH/RF 없음)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from apps import service_layer, service_session
from apps.service_trace import NullTrace
from stage7_binding_fixtures import (approved_rename_pins,
                                     diagnostics_gating_pins,
                                     launcher_shutdown_pins,
                                     operational_readiness_pins,
                                     legacy_phase_i_pins)


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


_BOUND_SOURCE = """\
def run_rr_p1():
    pass

def main():
    if args.phase == "rr-p1":
        run_rr_p1()
"""


def _function_hash(source, name):
    tree = ast.parse(source)
    node = next(item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == name)
    return {
        "ast_sha256": hashlib.sha256(
            ast.dump(node).encode("utf-8")).hexdigest(),
        "source_sha256": hashlib.sha256(
            ast.unparse(node).encode("utf-8")).hexdigest(),
    }


def _bound_profile(**overrides):
    spec = {
        "phase_f_file_sha256": "f" * 64,
        "phase_g_file_sha256": hashlib.sha256(
            _BOUND_SOURCE.encode("utf-8")).hexdigest(),
        "qualified_functions": {
            "run_rr_p1": _function_hash(_BOUND_SOURCE, "run_rr_p1"),
        },
        "rr_phase_dispatch": {"rr-p1": ["run_rr_p1"]},
    }
    spec.update(overrides)
    return {
        "pi": {"host": "fake"},
        "_reference": {
            "qualification_critical_manifest": {
                "function_bound_files": {"VDETER/apps/coordinator.py": spec},
            },
        },
    }


def _source_runner(command, **_kwargs):
    assert command[0] == "ssh" and command[-1].startswith("cat ")
    return _Completed(_BOUND_SOURCE)


def test_function_bound_rejects_remote_phase_g_file_mismatch():
    profile = _bound_profile(phase_g_file_sha256="0" * 64)
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _source_runner)
        raise AssertionError("원격 coordinator 전체 파일 불일치가 통과했다")
    except service_layer.ServiceSessionError as exc:
        assert "전체 파일" in str(exc)


def test_function_bound_rejects_dispatch_mismatch():
    profile = _bound_profile(
        rr_phase_dispatch={"rr-p1": ["run_unqualified_path"]})
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _source_runner)
        raise AssertionError("RR dispatch 불일치가 통과했다")
    except service_layer.ServiceSessionError as exc:
        assert "dispatch_mismatch" in str(exc)


def test_function_bound_requires_all_binding_fields():
    profile = _bound_profile()
    spec = (profile["_reference"]["qualification_critical_manifest"]
            ["function_bound_files"]["VDETER/apps/coordinator.py"])
    del spec["phase_g_file_sha256"]
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _source_runner)
        raise AssertionError("누락된 전체 파일 결속이 통과했다")
    except service_layer.ServiceSessionError as exc:
        assert "phase_g_file_sha256" in str(exc)


def test_function_bound_accepts_complete_matching_reference():
    report = service_layer._verify_function_bound(
        _bound_profile(), "$HOME/deploy", _source_runner)
    item = report["VDETER/apps/coordinator.py"]
    assert item["pass"] is True
    assert item["dispatch_changed"] is False


# ---------------------------------------------------------------------------
# Phase I 조건부 승인(2026-09-04) — 실제 Phase G 결속 vs 현재 coordinator.
# 계약 A: 패치하지 않은 진짜 Phase G reference는 변경된 coordinator를 RF 접근
#         전에 거부해야 한다 (production `_verify_function_bound` 무수정).
# 계약 B: synthetic Phase I 재결속(고정 해시) fixture에서만 통과하고, 다른
#         qualification 함수 변조·run_rr_p3_fault 추가 변경은 다시 거부된다.
# ---------------------------------------------------------------------------

#: 현재 저장소의 coordinator 경로(파일을 읽을 때 쓴다).
_COORDINATOR_REL = "VDETER/apps/coordinator_main.py"
#: 자격 당시 경로 — 과거 reference 기록의 결속 **키**다(소급 수정하지 않는다).
_COORDINATOR_REL_AT_QUALIFIED_COMMIT = "VDETER/apps/stage7_coordinator_main.py"


def _phase_g_profile_copy():
    """Phase G profile(tracked fixture — reference byte-identical 사본)을 적재한
    in-memory 사본 (파일 무수정). 로컬 evidence에 의존하지 않는다."""
    import copy

    from apps import run_profile
    path = os.path.join(_REPO, "tests", "fixtures", "phase-g-reference",
                        "stage7-profile.json")
    if not os.path.isfile(path):
        raise AssertionError("phase-g reference fixture가 없다: " + path)
    return copy.deepcopy(run_profile.load_profile(path))


def _current_coordinator_source() -> str:
    with open(os.path.join(_REPO, _COORDINATOR_REL), encoding="utf-8") as h:
        return h.read()


def _serving_runner(source: str):
    def runner(command, **_kwargs):
        assert command[0] == "ssh" and command[-1].startswith("cat ")
        return _Completed(source)
    return runner


def _mutate_function(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = source.splitlines(keepends=True)
    first = node.body[0].lineno - 1
    indent = len(lines[first]) - len(lines[first].lstrip())
    lines.insert(first, " " * indent + "_phase_i_mutation_probe = 1\n")
    return "".join(lines)


def _phase_i_pinned() -> dict:
    return legacy_phase_i_pins()


def _rebind_for_phase_i(profile: dict, source: str, *,
                        pin_run_rr_p3_fault: bool = True) -> dict:
    """synthetic 재결속: 전체 file SHA(항상) + run_rr_p3_fault 고정 해시(선택)."""
    spec = profile["_reference"]["qualification_critical_manifest"][
        "function_bound_files"][_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    spec["phase_g_file_sha256"] = hashlib.sha256(
        source.encode("utf-8")).hexdigest()
    # 2-3 승인 이름·경로 치환으로 바뀐 자격 함수의 고정 해시(기록된 값).
    for _name, _pinned in approved_rename_pins().items():
        if _name in spec["qualified_functions"]:
            spec["qualified_functions"][_name] = dict(_pinned)
    # 3-1 진단 게이트(제한적 동작 변경)로 바뀐 자격 함수의 고정 해시.
    # rename 핀 뒤에 적용한다 — 같은 함수가 두 기록에 걸치면 나중 승인이 이긴다.
    for _name, _pinned in diagnostics_gating_pins().items():
        if _name in spec["qualified_functions"]:
            spec["qualified_functions"][_name] = dict(_pinned)
    # 3-3 종료 대기 구조 변경(제한적 동작 변경)으로 바뀐 자격 함수의 고정 해시.
    for _name, _pinned in launcher_shutdown_pins().items():
        if _name in spec["qualified_functions"]:
            spec["qualified_functions"][_name] = dict(_pinned)
    # operational readiness(2026-09-17, cold path/ready deadline/binding gate 별칭)로 바뀐 main.
    for _name, _pinned in operational_readiness_pins().items():
        if _name in spec["qualified_functions"]:
            spec["qualified_functions"][_name] = dict(_pinned)
    if pin_run_rr_p3_fault:
        for name, pinned in _phase_i_pinned().items():
            spec["qualified_functions"][name] = dict(pinned)
    return profile


def test_real_phase_g_binding_rejects_current_coordinator():
    """계약 A: 패치 없는 실제 Phase G 결속은 현재 coordinator를 거부한다 —
    사유는 전체 file SHA 불일치(첫 게이트)."""
    profile = _phase_g_profile_copy()
    source = _current_coordinator_source()
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _serving_runner(source))
        raise AssertionError("실제 Phase G 결속이 변경된 coordinator를 통과시켰다")
    except service_layer.ServiceSessionError as exc:
        text = str(exc)
        assert "run_rr_p3_fault" in text or "전체 파일" in text, text


def test_real_phase_g_function_hashes_reject_run_rr_p3_fault_change():
    """계약 A(함수 단위): 전체 file SHA만 현재값으로 맞추고 함수 해시는 실제
    Phase G 값 그대로면 승인 대상 함수들만 changed로 거부된다 — 즉 변경된 함수가
    정확히 승인 목록과 일치함을 실제 결속으로 확인한다(Phase I run_rr_p3_fault,
    2026-09-10 Pi powered-USB3-hub run_rr_p0, 2026-09-12 Stage 8 PoC 수명주기
    run_mass_roc_session). 승인 목록은 fixture의
    `expected_changed_vs_qualified_commit`가 소유하며 여기서 따로 적지 않는다."""
    profile = _rebind_for_phase_i(_phase_g_profile_copy(),
                                  _current_coordinator_source(),
                                  pin_run_rr_p3_fault=False)
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy",
            _serving_runner(_current_coordinator_source()))
        raise AssertionError("함수 해시 불일치가 거부되지 않았다")
    except service_layer.ServiceSessionError as exc:
        text = str(exc)
        assert "run_rr_p3_fault" in text, text
        # 승인 목록과 **정확히** 일치해야 한다(승인 밖 함수가 하나라도 끼면 실패).
        with open(os.path.join(_REPO, "tests", "fixtures",
                               "stage7_phase_i_function_bindings.json"),
                  encoding="utf-8") as handle:
            approved = json.load(handle)["expected_changed_vs_qualified_commit"]
        assert f"changed={sorted(approved)!r}" in text, (text, sorted(approved))


def test_synthetic_phase_i_rebinding_passes_and_detects_further_changes():
    """계약 B: synthetic Phase I 재결속 fixture에서만 통과. 다른 qualification
    함수 변조(run_rr_p1)나 run_rr_p3_fault 추가 변경은 다시 거부된다."""
    source = _current_coordinator_source()
    profile = _rebind_for_phase_i(_phase_g_profile_copy(), source)
    report = service_layer._verify_function_bound(
        profile, "$HOME/deploy", _serving_runner(source))
    item = report[_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    assert item["pass"] is True and item["changed"] == []

    for target in ("run_rr_p1", "run_rr_p3_fault"):
        mutated = _mutate_function(source, target)
        # 전체 file SHA는 변조본에 맞추되 함수 고정 해시는 승인값 그대로 —
        # 함수 단위 가드가 변조를 잡아야 한다.
        profile2 = _rebind_for_phase_i(_phase_g_profile_copy(), mutated)
        try:
            service_layer._verify_function_bound(
                profile2, "$HOME/deploy", _serving_runner(mutated))
            raise AssertionError(f"{target} 변조가 거부되지 않았다")
        except service_layer.ServiceSessionError as exc:
            assert f"changed=['{target}']" in str(exc), str(exc)


def _preflight_profile():
    endpoints = {
        "MASS-A": {"serial": "A", "usb_path": "3-1", "speed_mbps": 5000},
        "MASS-B": {"serial": "B", "usb_path": "4-2", "speed_mbps": 480},
        "ROC": {"serial": "C", "usb_path": "5-1", "speed_mbps": 5000},
    }
    return {
        "pi": {"host": "fake", "deploy_relpath": "vdeter-stage7/fake"},
        "endpoints": endpoints,
        "trust": {
            "certificate_path": "cert.pem",
            "trust_store_path": "trust.pem",
            "private_key_path": "signer.pem",
            "private_key_required_mode": "600",
        },
        "_reference": {
            "qualification_critical_manifest": {
                "strict_files": {}, "function_bound_files": {},
            },
        },
    }


def _process_runner(process_line, scripts):
    def run(command, **_kwargs):
        script = command[-1]
        scripts.append(script)
        if "STAGE7_CRITICAL_LIST_END" in script:
            return _Completed("")
        return _Completed("\n".join([
            "##USB", "3-1 A 5000", "4-2 B 480", "5-1 C 5000",
            "##PROC", process_line,
            "##THROTTLE", "throttled=0x0",
            "##TRUST", "cert.pem 644", "trust.pem 644", "signer.pem 600",
            "##KEYCERT", "match=true",
        ]))
    return run


def _assert_process_is_rejected(process_line):
    scripts = []
    try:
        service_layer.verify_remote_environment(
            _preflight_profile(),
            runner=_process_runner(process_line, scripts))
        raise AssertionError("경쟁 process가 통과했다")
    except service_layer.ServiceSessionError as exc:
        assert "프로세스" in str(exc)
    process_script = next(script for script in scripts if "##PROC" in script)
    assert "mass_roc_workload_controller" in process_script
    assert "multiprocessing\\.spawn" in process_script
    assert "spawn_main" in process_script


def test_preflight_rejects_mass_roc_controller_process():
    _assert_process_is_rejected(
        "101 python3 apps/mass_roc_workload_controller.py --run-id stale")


def test_preflight_rejects_spawned_b210_worker_process():
    _assert_process_is_rejected(
        "102 python3 -c from multiprocessing.spawn import spawn_main; "
        "spawn_main(pipe_handle=1) --multiprocessing-fork")


def _session_profile():
    return {
        "profile_id": "test-profile",
        "pi": {"host": "fake", "deploy_relpath": "vdeter-stage7/fake"},
        "_reference": {"qualification": {"run_id": "qualified"},
                       "_reference_sha256": "r" * 64},
    }


class _CloseRunner:
    def __init__(self, *, exited=True, shutdown_rc=0, scp_rc=0):
        self.exited = exited
        self.shutdown_rc = shutdown_rc
        self.scp_rc = scp_rc

    def __call__(self, command, **_kwargs):
        if command[0] == "scp":
            return _Completed(stderr="copy failed", returncode=self.scp_rc)
        script = command[-1]
        if "touch" in script and "shutdown" in script:
            return _Completed(returncode=self.shutdown_rc)
        if script.startswith("test -f") and "done.rc" in script:
            return _Completed("yes" if self.exited else "no")
        if script.startswith("cat ") and "done.rc" in script:
            return _Completed("0" if self.exited else "")
        if script.startswith("cat ") and "closed.json" in script:
            return _Completed(json.dumps({
                "artifact_type": "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1",
                "status": "PASS", "cleanup": {"pass": True},
            }))
        return _Completed()


def _new_session(tmp, runner):
    session = service_session.ServiceSession(
        _session_profile(), session_id="session-test", session_dir=tmp,
        runner=runner, trace=NullTrace())
    session.status = "READY"
    session._coordinator_launch_attempted = True
    session._coordinator_spawned = True
    return session


def test_collect_evidence_rejects_scp_failure():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, _CloseRunner(scp_rc=7))
        try:
            session.collect_evidence()
            raise AssertionError("scp 실패가 성공으로 처리됐다")
        except service_layer.ServiceSessionError as exc:
            assert "scp 실패" in str(exc) and "rc=7" in str(exc)


def test_collect_evidence_revalidates_existing_target():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, _CloseRunner())
        target = os.path.join(tmp, "remote-run")
        os.makedirs(target)
        try:
            session.collect_evidence()
            raise AssertionError("빈 기존 evidence target이 재사용됐다")
        except service_layer.ServiceSessionError as exc:
            assert "비어" in str(exc)

        session.ready = {"run_id": session.session_id}
        session.closed = {
            "artifact_type": "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1",
            "run_id": session.session_id,
            "status": "PASS", "coordinator_rc": 0,
        }
        session_dir = os.path.join(target, "session")
        os.makedirs(session_dir)
        for name, payload in (("ready.json", session.ready),
                              ("closed.json", session.closed)):
            with open(os.path.join(session_dir, name), "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        with open(os.path.join(target, "done.rc"), "w", encoding="utf-8") as handle:
            handle.write("0\n")
        result = session.collect_evidence()
        assert result["pass"] is True and result["reused"] is True


def test_collect_evidence_validates_fresh_scp_target():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, None)
        session.ready = {"run_id": session.session_id}
        session.closed = {
            "artifact_type": "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1",
            "run_id": session.session_id,
            "status": "PASS", "coordinator_rc": 0,
        }

        def runner(command, **_kwargs):
            assert command[0] == "scp"
            target = command[-1]
            session_dir = os.path.join(target, "session")
            os.makedirs(session_dir)
            for name, payload in (("ready.json", session.ready),
                                  ("closed.json", session.closed)):
                with open(os.path.join(session_dir, name), "w",
                          encoding="utf-8") as handle:
                    json.dump(payload, handle)
            with open(os.path.join(target, "done.rc"), "w",
                      encoding="utf-8") as handle:
                handle.write("0\n")
            return _Completed(returncode=0)

        session.runner = runner
        result = session.collect_evidence()
        assert result["pass"] is True
        assert result["reused"] is False
        assert result["scp_returncode"] == 0
        assert result["file_count"] == 3


def test_close_before_coordinator_spawn_skips_exit_wait():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, _CloseRunner(scp_rc=8))
        session._coordinator_launch_attempted = False
        session._coordinator_spawned = False
        exit_checks = []

        def coordinator_exited():
            exit_checks.append(True)
            return False

        session._coordinator_exited = coordinator_exited
        result = session.close()
        assert exit_checks == []
        assert result["status"] == "FAIL"
        assert result["bounded_close"]["coordinator_may_be_running"] is False


def test_close_unconfirmed_launch_attempt_is_treated_as_maybe_running():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, _CloseRunner(exited=False, scp_rc=8))
        session._coordinator_spawned = False
        previous = service_session.SESSION_CLOSE_TIMEOUT_S
        service_session.SESSION_CLOSE_TIMEOUT_S = 0.0
        try:
            result = session.close()
        finally:
            service_session.SESSION_CLOSE_TIMEOUT_S = previous
        assert result["status"] == "FAIL"
        assert result["bounded_close"]["launch_attempted"] is True
        assert result["bounded_close"]["spawn_confirmed"] is False
        assert result["bounded_close"]["coordinator_may_be_running"] is True


def test_close_timeout_reports_coordinator_may_still_run():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(tmp, _CloseRunner(exited=False))
        previous = service_session.SESSION_CLOSE_TIMEOUT_S
        service_session.SESSION_CLOSE_TIMEOUT_S = 0.0
        try:
            result = session.close()
        finally:
            service_session.SESSION_CLOSE_TIMEOUT_S = previous
        assert result["status"] == "FAIL"
        assert result["bounded_close"]["pass"] is False
        assert result["bounded_close"]["coordinator_exited"] is False
        assert result["bounded_close"]["coordinator_may_be_running"] is True
        assert session.status == "FAIL"


def test_close_shutdown_or_evidence_failure_is_explicit_fail():
    with tempfile.TemporaryDirectory() as tmp:
        session = _new_session(
            tmp, _CloseRunner(exited=True, shutdown_rc=9, scp_rc=8))
        result = session.close()
        assert result["status"] == "FAIL"
        assert result["bounded_close"]["shutdown_request_returncode"] == 9
        assert session.evidence_collection["pass"] is False
        assert "evidence 수집 실패" in result["error"]
        assert session.summary()["status"] == "FAIL"


def test_launcher_shutdown_rebinding_is_hash_pinned_not_a_name_allowlist():
    """3-3 으로 재결속한 함수라도 **그 뒤의 추가 변경**은 다시 거부된다."""
    pinned_names = sorted(launcher_shutdown_pins())
    assert pinned_names, "종료 대기 재결속 기록이 비어 있다"
    target = "run_mass_roc_service"
    assert target in pinned_names, pinned_names

    source = _current_coordinator_source()
    profile = _rebind_for_phase_i(_phase_g_profile_copy(), source)
    mutated = _mutate_function(source, target)
    spec = profile["_reference"]["qualification_critical_manifest"][
        "function_bound_files"][_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    spec["phase_g_file_sha256"] = hashlib.sha256(
        mutated.encode("utf-8")).hexdigest()
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _serving_runner(mutated))
        raise AssertionError(
            "재결속 대상 함수의 추가 변경이 통과했다 — 고정 해시가 아니라 이름 허용목록이 됐다")
    except service_layer.ServiceSessionError as exc:
        assert target in str(exc), str(exc)


def test_operational_readiness_rebinding_is_hash_pinned_not_a_name_allowlist():
    """operational readiness 로 재결속한 `main` 이라도 **그 뒤의 추가 변경**은 다시 거부된다."""
    pinned_names = sorted(operational_readiness_pins())
    assert pinned_names == ["main"], pinned_names
    target = "main"
    source = _current_coordinator_source()
    profile = _rebind_for_phase_i(_phase_g_profile_copy(), source)
    mutated = _mutate_function(source, target)
    spec = profile["_reference"]["qualification_critical_manifest"][
        "function_bound_files"][_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    spec["phase_g_file_sha256"] = hashlib.sha256(
        mutated.encode("utf-8")).hexdigest()
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _serving_runner(mutated))
        raise AssertionError(
            "재결속 대상 함수의 추가 변경이 통과했다 — 고정 해시가 아니라 이름 허용목록이 됐다")
    except service_layer.ServiceSessionError as exc:
        assert target in str(exc), str(exc)


def test_diagnostics_gating_rebinding_is_hash_pinned_not_a_name_allowlist():
    """3-1 진단 게이트로 재결속한 함수라도 **그 뒤의 추가 변경**은 다시 거부된다.

    제한적 동작 변경을 한 번 승인한 것이지, 그 함수를 앞으로 자유롭게 바꿔도 된다는
    뜻이 아니다. 재결속은 이름 목록이 아니라 기록된 고정 해시다.
    """
    pinned_names = sorted(diagnostics_gating_pins())
    assert pinned_names, "진단 게이트 재결속 기록이 비어 있다"
    target = "_spawn_mass_roc_controller"
    assert target in pinned_names, pinned_names

    source = _current_coordinator_source()
    profile = _rebind_for_phase_i(_phase_g_profile_copy(), source)
    mutated = _mutate_function(source, target)
    # 전체 file SHA 는 변조본에 맞춰 둔다 — 함수 단위 검출만 격리한다.
    spec = profile["_reference"]["qualification_critical_manifest"][
        "function_bound_files"][_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    spec["phase_g_file_sha256"] = hashlib.sha256(
        mutated.encode("utf-8")).hexdigest()
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _serving_runner(mutated))
        raise AssertionError(
            "재결속 대상 함수의 추가 변경이 통과했다 — 고정 해시가 아니라 이름 허용목록이 됐다")
    except service_layer.ServiceSessionError as exc:
        assert target in str(exc), str(exc)


def test_approved_rename_rebinding_is_hash_pinned_not_a_name_allowlist():
    """승인 rename 으로 재결속한 함수라도 **그 뒤의 추가 변경**은 다시 거부된다.

    재결속은 이름 목록이 아니라 기록된 고정 해시다. 재결속 대상 함수를 변조하면
    gate 가 거부해야 한다 — 그렇지 않으면 "한 번 재결속하면 이후 무엇이든 통과"가 된다.
    """
    pinned_names = sorted(approved_rename_pins())
    assert pinned_names, "승인 rename 재결속 기록이 비어 있다"
    target = "_spawn_controller"
    assert target in pinned_names, pinned_names

    source = _current_coordinator_source()
    profile = _rebind_for_phase_i(_phase_g_profile_copy(), source)
    mutated = _mutate_function(source, target)
    # 전체 file SHA 는 변조본에 맞춰 둔다 — 함수 단위 검출만 격리한다.
    spec = profile["_reference"]["qualification_critical_manifest"][
        "function_bound_files"][_COORDINATOR_REL_AT_QUALIFIED_COMMIT]
    spec["phase_g_file_sha256"] = hashlib.sha256(
        mutated.encode("utf-8")).hexdigest()
    try:
        service_layer._verify_function_bound(
            profile, "$HOME/deploy", _serving_runner(mutated))
        raise AssertionError(
            "재결속 대상 함수의 추가 변경이 통과했다 — 고정 해시가 아니라 이름 허용목록이 됐다")
    except service_layer.ServiceSessionError as exc:
        assert target in str(exc), str(exc)
