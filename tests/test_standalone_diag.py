"""`vdeter diag` — 개발용 통제 RF 진단의 RF-off negative/positive 계약 시험.

증명하는 것 (실행 명세 §5A, apps/standalone/README.md §7A·§8·§11)
  분리      : 진단 authorization이 있어도 정상 `run`은 UNQUALIFIED로 거부(장치 접근 0)
  발급 거부 : 배선 미승인·교정 출처 부적합(결속 불일치)·잘못된 plan → 아무것도 쓰지 않음
  실행 거부 : authorization 없음·sidecar 변조·만료·stale(라이브러리/장비 불일치)·범위 초과·
              한도 도달 → 장치/세션 접근 전 거부
  실행      : 정상 run과 같은 세션 argv/phase/OPERATIONAL config, gain은 교정 출처에서,
              run.json/summary.json/RESULT는 NOT_QUALIFIED 표기, reference 발급·승격 없음
  중단      : bring-up 인터럽트 → 130 + cleanup 요약 + ledger 기록; SESSION_INHIBIT → 추가 제출 0
  RX-only   : workload 0으로 bring-up→hold→close PASS

가짜인 것은 coordinator 세션뿐이다(`test_standalone_run_cli.make_session_factory`).
installation manifest·identity·reference 발급기·검증기·config 생성은 실물 경로다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in (_REPO, _HERE, os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import standalone_v2_fixtures as fx  # noqa: E402
import test_standalone_run_cli as rc  # noqa: E402  (가짜 세션·run helper 재사용)
from apps.standalone import (  # noqa: E402
    cli,
    diag_cmd,
    qualify_cmd,
    run_cmd,
)

ENV_MANIFEST = fx.ENV_MANIFEST
USB_ALL = fx.USB_ALL
GAIN = fx.GAIN
fake_preflight = fx.fake_preflight

PLAN = {
    "label": "unit-stage-B",
    "allowed_scenarios": ["SC-VDE-005"],
    "poc_allowed": False,
    "max_runs": 2,
    "max_workloads_per_run": 2,
    "expires_utc": "2030-01-01T00:00:00Z",
    "max_hold_after_ready_s": 60,
}


# ---------------------------------------------------------------- 보조

def write_calibration_source(tmp, *, fingerprint="deadbeef0000", record=None,
                             mutate=None) -> Path:
    """교정 출처 reference — 다른 identity·다른 installation digest(=이전 후보)지만
    endpoints·wiring·라이브러리·rf_critical_symbols는 같은 v2 reference를 실제 발급기로
    만들고 sidecar와 함께 쓴다."""
    record = json.loads(json.dumps(record or fx.repo_installation_record()))
    record["installation_sha256"] = "11" * 32
    record["domains"]["tooling"]["sha256"] = "22" * 32
    record["domains"]["rf"]["sha256"] = "33" * 32
    reference = fx.issue_v2_reference(record, fingerprint)
    if mutate is not None:
        mutate(reference)
    directory = Path(tmp) / "calibration-source"
    directory.mkdir(exist_ok=True)
    qualify_cmd.write_reference(directory, reference)
    return directory / qualify_cmd.REFERENCE_BASENAME


def write_plan(tmp, **override) -> Path:
    plan = dict(PLAN)
    plan.update(override)
    path = Path(tmp) / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def authorize(paths, tmp, *, plan_path=None, source=None, **override) -> list:
    lines = []
    kwargs = dict(
        calibration_reference=source or write_calibration_source(tmp),
        plan_path=plan_path or write_plan(tmp),
        repo_root=_REPO, hardware_path=Path(tmp) / "hardware.toml",
        preflight_fn=fake_preflight, environment_manifest=dict(ENV_MANIFEST),
        out=lines.append)
    kwargs.update(override)
    diag_cmd.authorize(paths, **kwargs)
    return lines


def diag_run(paths, factory, scenarios, **override):
    lines = []
    kwargs = dict(
        scenarios=scenarios, session_factory=factory,
        preflight_fn=fake_preflight, usb_probe=lambda: dict(USB_ALL),
        environment_manifest=dict(ENV_MANIFEST), repo_root=_REPO,
        hardware_path=Path(paths.var_dir).parent / "hardware.toml",
        out=lines.append, input_fn=lambda prompt: "q", progress_ticker=False,
        progress=False)
    kwargs.update(override)
    code = diag_cmd.run(paths, **kwargs)
    return code, lines


def ledger(paths) -> dict:
    return json.loads(paths.diagnostic_ledger.read_bytes())


def qualification_state(paths) -> tuple:
    releases = paths.qualification_releases
    return (paths.active_pointer.exists(),
            sorted(p.name for p in releases.iterdir()) if releases.is_dir() else [])


def never_preflight():
    raise AssertionError("preflight must not run before the authorization gate")


# ---------------------------------------------------------------- plan 검증

def test_validate_plan_rejects_bad_plans():
    for bad in (
        {**PLAN, "allowed_scenarios": ["SC-VDE-999"]},
        {**PLAN, "max_runs": 0},
        {**PLAN, "max_workloads_per_run": -1},
        {**PLAN, "poc_allowed": "yes"},
        {**PLAN, "expires_utc": "tomorrow"},
        {**PLAN, "max_hold_after_ready_s": 10_000},
        {k: v for k, v in PLAN.items() if k != "expires_utc"},
        [],
    ):
        try:
            diag_cmd.validate_plan(bad)
            raise AssertionError("accepted bad plan: %r" % (bad,))
        except diag_cmd.DiagError as exc:
            assert exc.code == run_cmd.EXIT_CONFIG
    assert diag_cmd.validate_plan(dict(PLAN)) == PLAN


# ---------------------------------------------------------------- 분리: 정상 run

def test_normal_run_still_unqualified_with_diagnostic_authorization_present():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        assert paths.diagnostic_authorization.is_file()
        factory = rc.make_session_factory()
        code, lines = rc.run(paths, factory)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("UNQUALIFIED" in line and "vdeter qualify" in line for line in lines)
        assert factory.instances == []
        # 진단 authorization은 정상 경로의 reference 검증기를 통과할 수 없다.
        assert qualify_cmd.load_active_reference(paths) is None
        assert qualification_state(paths) == (False, [])


# ---------------------------------------------------------------- 발급 거부

def test_authorize_refuses_unapproved_wiring_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        fx.write_hardware_toml(Path(tmp) / "hardware.toml", approved=False)
        try:
            authorize(paths, tmp)
            raise AssertionError("issued with wiring.approved=false")
        except diag_cmd.DiagError as exc:
            assert exc.code == run_cmd.EXIT_CONFIG
            assert "wiring.approved" in str(exc)
        assert not paths.diagnostic_dir.exists()


def test_authorize_refuses_inapplicable_calibration_source():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)

        def swap_serials(reference):
            endpoints = reference["standalone_critical"]["endpoints"]
            endpoints["MASS-A"], endpoints["ROC"] = endpoints["ROC"], endpoints["MASS-A"]

        source = write_calibration_source(tmp, mutate=swap_serials)
        try:
            authorize(paths, tmp, source=source)
            raise AssertionError("issued with mismatching endpoints")
        except diag_cmd.DiagError as exc:
            assert exc.code == run_cmd.EXIT_UNQUALIFIED
            assert "not applicable" in str(exc) and "endpoints" in str(exc)
            assert "GAIN_CALIBRATION" in str(exc)
        assert not paths.diagnostic_dir.exists()

        # 라이브러리 버전이 다른 출처도 거부한다 (정상 run과 같은 항목).
        def other_uhd(reference):
            reference["standalone_critical"]["library_versions"]["uhd"] = "4.7"

        source = write_calibration_source(tmp, mutate=other_uhd)
        try:
            authorize(paths, tmp, source=source)
            raise AssertionError("issued with mismatching library versions")
        except diag_cmd.DiagError as exc:
            assert "library_versions" in str(exc)
        assert not paths.diagnostic_dir.exists()


def test_authorize_binds_candidate_digest_and_source_gain():
    with tempfile.TemporaryDirectory() as tmp:
        paths, record = fx.prepare_environment(tmp, qualified=False)
        lines = authorize(paths, tmp)
        assert any("NOT_QUALIFIED" in line for line in lines)
        authorization = diag_cmd.load_authorization(paths)
        critical = authorization["standalone_critical"]
        assert authorization["artifact_type"] == diag_cmd.AUTHORIZATION_TYPE
        assert authorization["not_a_qualification"] is True
        # 후보 **자체** digest에 결속 (출처의 11..·22..·33..이 아니다).
        assert critical["installation_sha256"] == record["installation_sha256"]
        assert authorization["candidate"]["installation_sha256"] == record["installation_sha256"]
        assert authorization["calibration_source"]["source_installation_sha256"] == "11" * 32
        # gain은 출처에서, identity는 후보 것.
        assert critical["gain"]["artifact_sha256"] == GAIN["artifact_sha256"]
        assert critical["identity_fingerprint"] != "deadbeef0000"
        assert authorization["calibration_source"]["applicability"]["applicable"] is True
        # var/qualification에는 아무것도 쓰지 않았다.
        assert qualification_state(paths) == (False, [])
        assert not (paths.qualification_candidate / qualify_cmd.REFERENCE_BASENAME).exists()
        # 정상 검증기는 이 파일을 거부한다 (artifact_type).
        from apps import stage7_profile
        try:
            stage7_profile.load_reference(str(paths.diagnostic_authorization))
            raise AssertionError("diagnostic authorization accepted as a reference")
        except stage7_profile.ProfileError:
            pass


# ---------------------------------------------------------------- 실행 거부 (장치 접근 전)

def test_diag_run_refused_without_valid_authorization():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        factory = rc.make_session_factory()
        code, lines = diag_run(paths, factory, ["SC-VDE-005"], preflight_fn=never_preflight)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("no diagnostic authorization" in line for line in lines)
        assert factory.instances == []

        authorize(paths, tmp)
        # sidecar를 갱신하지 않은 plan 한도 변조 → SHA 불일치로 거부 (권한 확대 불가).
        raw = json.loads(paths.diagnostic_authorization.read_bytes())
        raw["plan"]["max_runs"] = 99
        paths.diagnostic_authorization.write_text(json.dumps(raw), encoding="utf-8")
        code, lines = diag_run(paths, factory, ["SC-VDE-005"], preflight_fn=never_preflight)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("SHA mismatch" in line for line in lines)
        assert factory.instances == []

        # 만료.
        authorize(paths, tmp, plan_path=write_plan(tmp, expires_utc="2020-01-01T00:00:00Z"))
        code, lines = diag_run(paths, factory, ["SC-VDE-005"], preflight_fn=never_preflight)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("expired" in line for line in lines)
        assert factory.instances == []


def test_diag_run_refused_by_plan_scope_before_device():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        factory = rc.make_session_factory()
        for scenarios, extra, needle in (
            (["SC-VDE-006"], {}, "not allowed by plan"),
            (["SC-VDE-005"] * 3, {}, "exceeds plan max_workloads_per_run"),
            (["SC-VDE-005"], {"poc": os.path.join(tmp, "x.py")}, "PoC is not allowed"),
            ([], {"hold_after_ready_s": 61.0}, "exceeds plan limit"),
        ):
            code, lines = diag_run(paths, factory, scenarios,
                                   preflight_fn=never_preflight, **extra)
            assert code == run_cmd.EXIT_CONFIG, lines
            assert any(needle in line for line in lines), (needle, lines)
        assert factory.instances == []
        assert ledger(paths)["runs"] == []   # 거부된 요청은 run으로 계상하지 않는다


def test_diag_run_refused_when_stale_or_wrong_device():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        factory = rc.make_session_factory()
        # 라이브러리 drift → 블록 대조 불일치 → 진단 전용 문구로 거부.
        env = dict(ENV_MANIFEST)
        env["uhd_version"] = "4.7"
        code, lines = diag_run(paths, factory, ["SC-VDE-005"], environment_manifest=env)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("DIAGNOSTIC AUTHORIZATION STALE" in line for line in lines)
        assert not any("run `vdeter qualify`" in line for line in lines)
        assert factory.instances == []

        # 다른 장비(serial 불일치) → 거부.
        def wrong_device():
            report = fake_preflight()
            report["checks"]["b210"]["observed_by_role"]["MASS-A"]["serial"] = "0000000"
            return report

        code, lines = diag_run(paths, factory, ["SC-VDE-005"], preflight_fn=wrong_device)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("serial" in line for line in lines)
        assert factory.instances == []
        # 거부된 시도도 ledger에는 STARTED→FINISHED로 남는다 (한도 계상).
        assert all(e["state"] == "FINISHED" for e in ledger(paths)["runs"])


# ---------------------------------------------------------------- 실행

def test_diag_run_uses_normal_session_path_and_marks_not_qualified():
    with tempfile.TemporaryDirectory() as tmp:
        paths, record = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        factory = rc.make_session_factory()
        code, lines = diag_run(paths, factory, ["SC-VDE-005"])
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        # 실 세션 경로 그대로: phase·identity root·baseline pin, RF-off double 아님.
        assert session.kwargs["phase"] == "mass-roc-session"
        assert session.kwargs["rf_off_software"] is False
        assert session.kwargs["identity_root"] == paths.identity_dir
        from apps.standalone import provenance
        assert session.kwargs["baseline_sha256"] == provenance.STAGE7_BASELINE_PIN["sha256"]
        # gain은 교정 출처의 값 그대로 coordinator 입력이 된다.
        gain = json.loads(Path(session.kwargs["gain_artifact_path"]).read_text("utf-8"))
        assert gain["endpoint_common_rx_gain_db"] == GAIN["endpoint_common_rx_gain_db"]
        assert gain["source_gain_artifact_sha256"] == GAIN["artifact_sha256"]
        # resolved config는 정상 run과 같은 OPERATIONAL·후보 installation 결속.
        mass_b = (session.run_root / "resolved-configs" / "endpoints"
                  / "mass-b.toml").read_text("utf-8")
        assert 'run_mode = "OPERATIONAL"' in mass_b
        assert f'deployment_manifest_sha256 = "{record["installation_sha256"]}"' in mass_b
        assert session.submitted == [("SC-VDE-005", "normal")]
        # 표기: QUALIFIED 배너 없음, NOT_QUALIFIED 명시, RESULT 접미.
        assert not any(line.startswith("VDETER standalone — QUALIFIED") for line in lines)
        assert any("DIAGNOSTIC, NOT_QUALIFIED" in line for line in lines)
        assert any(line.startswith("RESULT overall=PASS") and
                   line.endswith("qualification=NOT_QUALIFIED") for line in lines)
        run_json = json.loads((session.run_root / "run.json").read_text("utf-8"))
        summary = json.loads((session.run_root / "summary.json").read_text("utf-8"))
        authorization = diag_cmd.load_authorization(paths)
        for doc in (run_json, summary):
            assert doc["reference_sha256"] is None
            assert doc["qualification_status"] == "NOT_QUALIFIED"
            assert doc["diagnostic_authorization_sha256"] == authorization["_reference_sha256"]
        assert session.run_root.name.startswith(diag_cmd.RUN_ID_PREFIX)
        # 진단 뒤에도 자격 상태·정상 run 거부는 그대로다.
        assert qualification_state(paths) == (False, [])
        assert qualify_cmd.load_active_reference(paths) is None
        code, lines2 = rc.run(paths, rc.make_session_factory())
        assert code == run_cmd.EXIT_UNQUALIFIED
        entry = ledger(paths)["runs"][-1]
        assert entry["state"] == "FINISHED" and entry["exit_code"] == 0
        assert entry["invariants_unchanged"] is True
        assert entry["authorization_still_valid"] is True
        assert entry["summary"]["qualification_status"] == "NOT_QUALIFIED"


def test_diag_rx_only_session_submits_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        factory = rc.make_session_factory()
        code, lines = diag_run(paths, factory, [], hold_after_ready_s=0.0)
        assert code == run_cmd.EXIT_PASS, lines
        session = factory.instances[0]
        assert session.submitted == []
        assert session.closed is True
        assert any("RX-only session" in line for line in lines)
        assert any(line.startswith("RESULT overall=PASS scenarios=0/0") for line in lines)
        assert ledger(paths)["runs"][-1]["rx_only"] is True


def test_rx_only_close_assessment_accepts_only_the_empty_measurement_reason():
    """coordinator는 빈 측정을 설계대로 fail-closed 판정한다
    (`_service_session_terminal_status`: `if not workloads: …`). RX-only는 workload를
    제출하지 않는 것이 목적이므로 그 사유 **하나만**인 FAIL은 기대된 결과다. 다른 사유가
    섞이거나 STOP·cleanup·fault·inhibit·topology에 문제가 있으면 실패 그대로여야 한다."""

    def summary(*, error=diag_cmd.EMPTY_MEASUREMENT_FAILURE, workloads=None,
                stop_rc=0, cleanup_pass=True, workers=0, inhibited=False,
                interrupted=False, topology=None):
        return {"interrupted": interrupted, "usb_topology_after_close": topology,
                "close": {"coordinator_rc": 1, "closed": {
                    "status": "FAIL", "error": error, "workloads": workloads or [],
                    "service_stop_rc": stop_rc, "inhibited": inhibited,
                    "cleanup": {"pass": cleanup_pass, "workers_after_stop": workers}}}}

    ok = diag_cmd.rx_only_close_assessment(summary())
    assert ok["expected_empty_measurement"] is True
    # coordinator의 원래 판정은 숨기지 않고 그대로 실린다.
    assert ok["coordinator_rc"] == 1 and ok["coordinator_status"] == "FAIL"
    assert ok["coordinator_error"] == diag_cmd.EMPTY_MEASUREMENT_FAILURE
    assert ok["failure_reasons"] == [diag_cmd.EMPTY_MEASUREMENT_FAILURE]

    for bad in (
        summary(error=diag_cmd.EMPTY_MEASUREMENT_FAILURE + "; service STOP 대행 rc=2",
                stop_rc=2),
        summary(error=diag_cmd.EMPTY_MEASUREMENT_FAILURE + "; endpoint STOP 미완료"),
        summary(error="하나 이상의 workload가 FAIL",
                workloads=[{"workload_id": "w", "status": "FAIL"}]),
        summary(cleanup_pass=False),
        summary(workers=2),
        summary(inhibited=True),
        summary(interrupted=True),
        summary(topology={"pass": False}),
        summary(error=None),
    ):
        assert diag_cmd.rx_only_close_assessment(bad)["expected_empty_measurement"] is False, bad


def test_diag_rx_only_maps_empty_measurement_to_pass_without_hiding_coordinator():
    """RX-only 실행에서 coordinator FAIL(rc=1, 빈 측정)만 있을 때 진단 판정은 성공이고,
    coordinator의 rc·status·error는 출력과 ledger에 그대로 남는다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        factory = rc.make_session_factory(
            {"close": {"pass": False,
                       "problems": ["coordinator exited abnormally (rc=1)"],
                       "coordinator_rc": 1, "coordinator_exited": True,
                       "closed": {"status": "FAIL",
                                  "error": diag_cmd.EMPTY_MEASUREMENT_FAILURE,
                                  "workloads": [], "service_stop_rc": 0,
                                  "inhibited": False,
                                  "cleanup": {"pass": True, "workers_after_stop": 0}}}})
        code, lines = diag_run(paths, factory, [], hold_after_ready_s=0.0)
        assert code == run_cmd.EXIT_PASS, lines
        assert any("designed fail-closed verdict for an empty measurement" in line
                   for line in lines)
        assert any("rc=1" in line and "status=FAIL" in line for line in lines)
        entry = ledger(paths)["runs"][-1]
        assessment = entry["rx_only_assessment"]
        assert assessment["expected_empty_measurement"] is True
        assert assessment["coordinator_rc"] == 1
        assert assessment["coordinator_error"] == diag_cmd.EMPTY_MEASUREMENT_FAILURE
        assert entry["exit_code"] == run_cmd.EXIT_PASS
        assert entry["invariants_unchanged"] is True
        # 다른 사유가 섞이면 성공으로 바꾸지 않는다.
        factory2 = rc.make_session_factory(
            {"close": {"pass": False, "problems": ["coordinator exited abnormally (rc=1)"],
                       "coordinator_rc": 1, "coordinator_exited": True,
                       "closed": {"status": "FAIL",
                                  "error": diag_cmd.EMPTY_MEASUREMENT_FAILURE
                                           + "; endpoint STOP 미완료",
                                  "workloads": [], "service_stop_rc": 0,
                                  "inhibited": False,
                                  "cleanup": {"pass": True, "workers_after_stop": 0}}}})
        code2, _ = diag_run(paths, factory2, [], hold_after_ready_s=0.0)
        assert code2 != run_cmd.EXIT_PASS
        assert ledger(paths)["runs"][-1]["rx_only_assessment"][
            "expected_empty_measurement"] is False


def test_diag_run_limit_reached_blocks_further_runs():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp, plan_path=write_plan(tmp, max_runs=1))
        factory = rc.make_session_factory()
        code, _ = diag_run(paths, factory, ["SC-VDE-005"])
        assert code == run_cmd.EXIT_PASS
        code, lines = diag_run(paths, factory, ["SC-VDE-005"], preflight_fn=never_preflight)
        assert code == run_cmd.EXIT_CONFIG
        assert any("run limit reached" in line for line in lines)
        assert len(factory.instances) == 1
        # 재발급은 ledger를 새로 시작하되 이전 ledger를 보존한다.
        authorize(paths, tmp, plan_path=write_plan(tmp, max_runs=1))
        assert ledger(paths)["runs"] == []
        assert any(p.name.startswith("ledger-") for p in paths.diagnostic_dir.iterdir())


def test_diag_bringup_interrupt_and_session_inhibit_are_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        authorize(paths, tmp)
        # bring-up 중 인터럽트 → 정상 close 경로 → 130, ledger 기록, 자격 상태 불변.
        factory = rc.make_session_factory({"bringup_interrupt": [True]})
        code, lines = diag_run(paths, factory, ["SC-VDE-005"])
        assert code == run_cmd.EXIT_INTERRUPTED
        assert factory.instances[0].closed is True
        assert any("interrupted during bring-up" in line and
                   "qualification=NOT_QUALIFIED" in line for line in lines)
        entry = ledger(paths)["runs"][-1]
        assert entry["exit_code"] == run_cmd.EXIT_INTERRUPTED
        assert entry["invariants_unchanged"] is True
        summary = json.loads((factory.instances[0].run_root / "summary.json").read_text("utf-8"))
        assert summary["interrupted"] is True
        assert summary["qualification_status"] == "NOT_QUALIFIED"

        # TX 결과·소유권 불확실(SESSION_INHIBIT) → 두 번째 workload 제출 0.
        factory = rc.make_session_factory(
            {"results": {"SC-VDE-005": {"poc_severity": "SESSION_INHIBIT"}}})
        code, lines = diag_run(paths, factory, ["SC-VDE-005", "SC-VDE-005"])
        assert code == run_cmd.EXIT_FAIL
        assert factory.instances[0].submitted == [("SC-VDE-005", "normal")]
        assert any("session inhibit" in line for line in lines)
        assert qualification_state(paths) == (False, [])


# ---------------------------------------------------------------- CLI

def test_cli_diag_subcommands_parse_and_dispatch():
    parser = cli.build_parser()
    args = parser.parse_args(["diag", "run", "SC-VDE-005", "--hold-after-ready", "30",
                              "--no-progress"])
    assert args.command == "diag" and args.diag_command == "run"
    assert args.scenario == ["SC-VDE-005"] and args.hold_after_ready == 30.0
    args = parser.parse_args(["diag", "run"])
    assert args.scenario == []
    args = parser.parse_args(["diag", "authorize", "--calibration-reference", "r.json",
                              "--plan", "p.json"])
    assert args.diag_command == "authorize"
    with tempfile.TemporaryDirectory() as tmp:
        paths, _ = fx.prepare_environment(tmp, qualified=False)
        lines = []
        code = cli.main(["diag", "status"], paths=paths, out=lines.append)
        assert code == run_cmd.EXIT_UNQUALIFIED
        assert any("none or invalid" in line for line in lines)
        code = cli.main(["diag", "authorize", "--calibration-reference",
                         os.path.join(tmp, "missing.json"), "--plan",
                         os.path.join(tmp, "missing-plan.json")],
                        paths=paths, out=lines.append)
        assert code == run_cmd.EXIT_CONFIG
        assert not paths.diagnostic_dir.exists()
