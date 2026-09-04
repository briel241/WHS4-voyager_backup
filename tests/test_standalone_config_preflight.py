"""Phase I W3 — 로컬 config 생성기·필수 preflight 계약 시험 (RF·SSH 없음).

증명하는 것 (apps/standalone/README.md §4~§6·§8·§9)
  binding    : standalone-binding 스키마 검증 (head 40-hex·dirty=false 강제)
  선언       : hardware.toml 스키마·승인 게이트 (미승인 = 거부)
  생성기     : 실제 template + 실제 Stage7ConfigurationLoader로 end-to-end
               생성·검증, git_state.json이 README §6 표·coordinator 결속
               계약(RR_PHASE_DEADLINE_S)과 일치, fixture는 host prepare와
               canonical JSON byte 동일
  preflight  : serial↔역할 정확 일치 + 속도 등급 강제(포트 경로는 기록만),
               잔존 프로세스 자동 kill 없는 거부, 키 0600·key-cert 일치,
               platform 검사(pi 필수 / generic-pc 기록만), 집계 게이트
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import local_config, local_preflight, state

GOOD_BINDING = {
    "artifact_type": "STAGE7_STANDALONE_BINDING_V1",
    "source_head": "f" * 40,
    "source_worktree_dirty": False,
    "baseline_version": "0.9.23",
    "baseline_sha256": "2a" * 32,
    "frozen_files": {"src/vdeter/config/models.py": "ab" * 32},
}

GOOD_HARDWARE = {
    "schema_version": 1,
    "platform_profile": "pi",
    "endpoints": {
        "MASS-A": {"serial": "34D03DD"},
        "MASS-B": {"serial": "3515425", "external_power_asset": "AT2024T"},
        "ROC": {"serial": "34D03E8"},
    },
    "wiring": {"profile": "C1-C6-STANDARD", "approved": True},
}


def write_binding(tmp, override=None) -> Path:
    binding = dict(GOOD_BINDING)
    binding.update(override or {})
    path = Path(tmp) / "standalone-binding.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    return path


def write_hardware(tmp, mutate=None) -> Path:
    import copy
    declaration = copy.deepcopy(GOOD_HARDWARE)
    if mutate:
        mutate(declaration)
    lines = ["schema_version = 1",
             f'platform_profile = "{declaration["platform_profile"]}"']
    for role, body in declaration["endpoints"].items():
        lines.append(f'[endpoints."{role}"]')
        for key, value in body.items():
            lines.append(f'{key} = "{value}"')
    lines.append("[wiring]")
    lines.append(f'profile = "{declaration["wiring"]["profile"]}"')
    lines.append(
        "approved = " + ("true" if declaration["wiring"]["approved"]
                         else "false"))
    path = Path(tmp) / "hardware.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- binding

def test_binding_validation_legacy_deploy_tool_only():
    """(legacy) 개발 PC 배포 도구 전용 — 대상 기계 실행 경로는 v2부터 이
    파일을 읽지 않는다 (README §5)."""
    with tempfile.TemporaryDirectory() as tmp:
        good = local_config.load_standalone_binding(write_binding(tmp))
        assert good["source_head"] == "f" * 40

        for override, needle in (
            ({"artifact_type": "X"}, "artifact_type"),
            ({"source_head": "abc"}, "40-hex"),
            ({"source_worktree_dirty": True}, "dirty"),
            ({"baseline_sha256": "zz"}, "baseline_sha256"),
            ({"frozen_files": {}}, "frozen_files"),
        ):
            try:
                local_config.load_standalone_binding(
                    write_binding(tmp, override))
                raise AssertionError(f"통과하면 안 됨: {override}")
            except local_config.ConfigGenerationError as exc:
                assert needle in str(exc)


# ---------------------------------------------------------------- 선언

def test_hardware_declaration_validation():
    with tempfile.TemporaryDirectory() as tmp:
        parsed = local_config.parse_hardware_declaration(write_hardware(tmp))
        assert parsed["endpoints"]["MASS-B"]["external_power_asset"] == "AT2024T"

        cases = [
            (lambda d: d["endpoints"]["ROC"].pop("serial"), "ROC.serial"),
            (lambda d: d["endpoints"]["MASS-B"].pop("external_power_asset"),
             "external_power_asset"),
            (lambda d: d["wiring"].update(approved=False), "승인"),
            (lambda d: d["endpoints"]["MASS-A"].update(serial="3515425"),
             "중복"),
        ]
        for mutate, needle in cases:
            try:
                local_config.parse_hardware_declaration(
                    write_hardware(tmp, mutate))
                raise AssertionError(f"통과하면 안 됨: {needle}")
            except local_config.ConfigGenerationError as exc:
                assert needle in str(exc), (needle, str(exc))


# ---------------------------------------------------------------- 생성기

_ENV_MANIFEST = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": "unit-test",
    "usb_devices": "/sys/.../serial 34D03DD",
    "numpy_version": "1.26.0",
    "cryptography_version": "50.0",
    "uhd_version": None,
    "gnuradio_version": None,
}


#: v2 결속 입력 — installation manifest digest 부분집합 (measure 결과 호환).
INSTALLATION = {
    "installation_sha256": "cd" * 32,
    "domains": {"rf": {"sha256": "aa" * 32, "file_count": 1},
                "operation": {"sha256": "bb" * 32, "file_count": 1},
                "tooling": {"sha256": "cc" * 32, "file_count": 1}},
}
PROVENANCE = {"note": "test", "git": {"commit": "f" * 40, "branch": "feature/x",
                                       "dirty": None},
              "legacy_binding": None, "stage7_baseline": {"version": "0.9.23"}}


def generate(tmp, rr_phase, **overrides):
    run_root = Path(tmp) / "runs" / "r1"
    run_root.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        repo_root=_REPO, run_root=run_root, run_id="r1", rr_phase=rr_phase,
        hardware=GOOD_HARDWARE, installation=INSTALLATION,
        installation_verified=True, provenance=PROVENANCE,
        environment_manifest=dict(_ENV_MANIFEST),
    )
    kwargs.update(overrides)
    return local_config.generate_run_configs(**kwargs)


def test_generate_run_configs_end_to_end_with_real_loader():
    with tempfile.TemporaryDirectory() as tmp:
        result = generate(tmp, "rr-p0")
        out = Path(result["configs_dir"])
        # 3 endpoint 전부 실제 loader 검증 통과 + resolved sha 기록.
        assert sorted(result["resolved_sha256"]) == ["mass-a", "mass-b", "roc"]
        assert all(len(sha) == 64 for sha in result["resolved_sha256"].values())
        for endpoint in ("mass-a", "mass-b", "roc"):
            text = (out / "endpoints" / f"{endpoint}.toml").read_text("utf-8")
            for field in ("environment_manifest_path",
                          "environment_manifest_sha256",
                          "deployment_manifest_relpath",
                          "deployment_manifest_sha256"):
                assert f'{field} = "REQUIRED_UNSET"' not in text, (
                    endpoint, field)
        mass_b = (out / "endpoints" / "mass-b.toml").read_text("utf-8")
        assert 'external_power_asset = "AT2024T"' in mass_b
        assert (out / "system.toml").is_file()
        assert (out / "profiles" / "data-profile.toml").is_file()
        # rr-p0에는 fixture가 없다 (host prepare와 동일 목록).
        assert result["fixtures"] == []
        assert not (out / "fixtures").exists()

        # deployment_manifest_* config 필드 = installation digest 결속.
        mass_a = (out / "endpoints" / "mass-a.toml").read_text("utf-8")
        assert f'deployment_manifest_sha256 = "{INSTALLATION["installation_sha256"]}"' in mass_a
        assert 'deployment_manifest_relpath = "var/installation/installation-manifest.json"' in mass_a

        git_state = json.loads((out / "git_state.json").read_text("utf-8"))
        # README §6 표 — 스키마는 호환. git 필드는 참고 provenance(있으면
        # 기록, 없으면 null)이며 판정에 쓰이지 않는다. baseline은 커밋 상수 pin.
        from apps.standalone.provenance import STAGE7_BASELINE_PIN
        assert git_state["git_commit"] == "f" * 40
        assert git_state["git_dirty"] is None
        assert git_state["baseline_sha256"] == STAGE7_BASELINE_PIN["sha256"]
        assert git_state["baseline_version"] == STAGE7_BASELINE_PIN["version"]
        assert git_state["provenance_schema"] == 2
        assert git_state["installation_sha256"] == INSTALLATION["installation_sha256"]
        assert git_state["rf_manifest_sha256"] == "aa" * 32
        assert git_state["installation_verified"] is True
        installation_state = json.loads(
            (out / "installation-state.json").read_text("utf-8"))
        assert installation_state["artifact_type"] == "STAGE7_STANDALONE_INSTALLATION_STATE_V2"
        assert installation_state["installation_sha256"] == INSTALLATION["installation_sha256"]
        assert installation_state["domains"]["tooling"] == "cc" * 32
        # git 없는 환경: provenance None → git 필드 null, 생성은 그대로 성공.
        nogit = generate(tmp, "rr-p0", provenance=None,
                         run_root=Path(tmp) / "runs" / "r2", run_id="r2")
        assert nogit["git_state"]["git_commit"] is None
        assert nogit["git_state"]["git_branch"] is None
        assert git_state["qualification_profile"] == "RESEARCH_READY_MINIMAL"
        assert git_state["rr_phase"] == "rr-p0"
        assert git_state["run_id"] == "r1"
        assert git_state["implicit_retry"] == 0
        assert git_state["data_link_id"] is None
        assert (git_state["environment_manifest_sha256"]
                == result["environment_manifest_sha256"])
        assert git_state["hardware"]["usb_devices"] == (
            _ENV_MANIFEST["usb_devices"])


def test_generate_refuses_unverified_installation_and_bad_phase():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            generate(tmp, "rr-p0", installation_verified=False)
            raise AssertionError("무결 미검증 설치가 거부되지 않았다")
        except local_config.ConfigGenerationError as exc:
            assert "무결 검증" in str(exc)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            generate(tmp, "rr-p0", installation={"installation_sha256": "zz"})
            raise AssertionError("digest 형식 오류가 거부되지 않았다")
        except local_config.ConfigGenerationError as exc:
            assert "sha256" in str(exc)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            generate(tmp, "not-a-phase")
            raise AssertionError("알 수 없는 phase가 거부되지 않았다")
        except local_config.ConfigGenerationError as exc:
            assert "rr_phase" in str(exc)


def test_fixtures_match_host_canonical_bytes_and_link_binding():
    with tempfile.TemporaryDirectory() as tmp:
        result = generate(tmp, "rr-p2-17")
        out = Path(result["configs_dir"])
        git_state = json.loads((out / "git_state.json").read_text("utf-8"))
        assert git_state["data_link_id"] == 17
        names = set(result["fixtures"])
        assert {"start-mass-a.json", "start-mass-b.json", "start-roc.json",
                "stop.json"} <= names
        # host prepare와 byte 동일한 canonical JSON인지 직접 대조.
        from vdeter.stage7.canonical_json import encode_canonical_json
        start = (out / "fixtures" / "start-mass-a.json").read_bytes()
        assert start == encode_canonical_json({
            "local_station_id": 111111111, "role": "SHIP", "config_ref": ""})
        import base64
        payload = bytes((i * 7 + 7) % 256 for i in range(353))
        data_fixture = (out / "fixtures" / "submit-data-roc-to-a.json"
                        ).read_bytes()
        assert data_fixture == encode_canonical_json({
            "destination": 111111111,
            "payload_format": "test.stage0.opaque",
            "payload_base64": base64.b64encode(payload).decode("ascii"),
            "ack_required": True,
        })


def test_version_probe_cli_fallback_for_uhd():
    """`import uhd` 불가 환경(Pi 실측)에서 uhd_config_info로 버전을 얻는다."""
    calls = []

    class Done:
        def __init__(self, rc, stdout):
            self.returncode, self.stdout = rc, stdout

    def runner(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "uhd_config_info":
            return Done(0, "\nUHD 4.6.0.0-0-g1234abcd\n")
        return Done(1, "")

    versions = local_config._probe_versions(runner=runner)
    # python uhd 모듈이 없는 개발 환경에서는 fallback 결과가 그대로 온다.
    try:
        import uhd  # type: ignore # noqa: F401
        has_uhd = True
    except Exception:  # noqa: BLE001
        has_uhd = False
    if not has_uhd:
        assert versions["uhd"] == "UHD 4.6.0.0-0-g1234abcd"
        assert "uhd_config_info" in calls
    # CLI 실패는 None (부재 정직 기록) — 별도 runner로 확인.
    assert local_config._cli_version(
        lambda argv, **kw: Done(1, ""), ["x"]) is None
    assert local_config._cli_version(
        lambda argv, **kw: (_ for _ in ()).throw(OSError("no tool")),
        ["x"]) is None


def test_phase_deadlines_match_coordinator_contract():
    """git_state에 결속하는 deadline이 coordinator 정본과 일치해야
    RR_BINDING_REJECTED가 나지 않는다 (README §6)."""
    coordinator_path = (Path(_REPO) / "VDETER" / "apps"
                        / "stage7_coordinator_main.py")
    spec = importlib.util.spec_from_file_location(
        "stage7_coordinator_main_for_test", coordinator_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for phase, deadline in module.RR_PHASE_DEADLINE_S.items():
        assert local_config.PHASE_DEADLINES_S[phase] == deadline, phase


# ---------------------------------------------------------------- preflight

def make_sysfs(tmp, devices):
    """devices: [(dirname, serial, speed)] — 가짜 sysfs 트리."""
    root = Path(tmp) / "sysfs"
    for dirname, serial, speed in devices:
        device = root / dirname
        device.mkdir(parents=True, exist_ok=True)
        (device / "serial").write_text(serial, encoding="ascii")
        if speed is not None:
            (device / "speed").write_text(str(speed), encoding="ascii")
    return str(root)


DECLARED = {"MASS-A": "34D03DD", "MASS-B": "3515425", "ROC": "34D03E8"}


def test_b210_roles_speed_class_enforced_port_path_recorded_only():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs = make_sysfs(tmp, [("3-1", "34D03DD", 5000),
                                 ("4-2", "3515425", 480),
                                 ("5-1", "34D03E8", 10000)])
        observed = local_preflight.probe_usb_serials(sysfs)
        report = local_preflight.check_b210_roles(DECLARED, observed)
        assert report["pass"], report["errors"]
        # 포트 경로는 기록될 뿐 판정에 쓰이지 않는다 — 다른 포트여도 통과.
        sysfs2 = make_sysfs(tmp + "/alt", [("1-9", "34D03DD", 5000),
                                           ("2-7", "3515425", 480),
                                           ("6-3", "34D03E8", 5000)])
        report2 = local_preflight.check_b210_roles(
            DECLARED, local_preflight.probe_usb_serials(sysfs2))
        assert report2["pass"]
        assert report2["observed_by_role"]["MASS-A"]["usb_path"] == "1-9"

        # 속도 등급 위반 (MASS-B가 USB3) / serial 부재는 거부.
        sysfs3 = make_sysfs(tmp + "/bad", [("3-1", "34D03DD", 5000),
                                           ("4-2", "3515425", 5000)])
        report3 = local_preflight.check_b210_roles(
            DECLARED, local_preflight.probe_usb_serials(sysfs3))
        assert not report3["pass"]
        text = " / ".join(report3["errors"])
        assert "속도 등급 불일치" in text and "보이지 않습니다" in text


def test_residual_processes_filters_and_unavailable():
    def runner(argv, **kwargs):
        class Done:
            stdout = (f"{os.getpid()} self-should-be-filtered\n"
                      "777 pgrep -af pattern\n"
                      "888 python stage7_endpoint_launcher.py\n")
        return Done()

    lines = local_preflight.residual_processes(
        runner, which=lambda name: "/usr/bin/pgrep")
    assert lines == ["888 python stage7_endpoint_launcher.py"]
    assert local_preflight.residual_processes(
        runner, which=lambda name: None) is None


def test_private_key_mode_and_key_cert_match():
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "k.pem"
        missing = local_preflight.check_private_key_mode(key_path)
        assert not missing["pass"] and not missing["present"]

        key_path.write_text("dummy", encoding="ascii")
        good = local_preflight.check_private_key_mode(
            key_path, mode_reader=lambda p: 0o600)
        assert good["pass"]
        bad = local_preflight.check_private_key_mode(
            key_path, mode_reader=lambda p: 0o644)
        assert not bad["pass"] and "0600" in bad["error"]

        # key-cert 일치 — cryptography로 실물 생성 (재료는 일회성).
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            print("  (cryptography 부재 — key-cert 일치 소검사 SKIP)")
            return
        import datetime
        key = ec.generate_private_key(ec.SECP256R1())
        other = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(
            x509.oid.NameOID.COMMON_NAME, "unit")])
        now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        certificate = (x509.CertificateBuilder()
                       .subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(1)
                       .not_valid_before(now)
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .sign(key, hashes.SHA256()))
        key_pem = Path(tmp) / "real-key.pem"
        cert_pem = Path(tmp) / "cert.pem"
        key_pem.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        cert_pem.write_bytes(certificate.public_bytes(
            serialization.Encoding.PEM))
        assert local_preflight.check_key_cert_match(key_pem, cert_pem) is True
        other_pem = Path(tmp) / "other-key.pem"
        other_pem.write_bytes(other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        assert local_preflight.check_key_cert_match(other_pem, cert_pem) is False


def test_platform_checks_pi_enforced_generic_recorded_only():
    def runner_ok(argv, **kwargs):
        class Done:
            stdout = "throttled=0x0\n"
        return Done()

    def runner_bad(argv, **kwargs):
        class Done:
            stdout = "throttled=0x50000\n"
        return Done()

    ok = local_preflight.platform_checks("pi", runner=runner_ok)
    assert ok["pass"] and ok["throttled"] == "throttled=0x0"
    bad = local_preflight.platform_checks("pi", runner=runner_bad)
    assert not bad["pass"] and "0x50000" in bad["error"]
    generic = local_preflight.platform_checks("generic-pc")
    assert generic["pass"] is True  # 기록만 — 기준 강제 없음 (W1 수정 3)
    assert "recorded" in generic


def test_run_required_preflight_aggregation():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        sysfs = make_sysfs(tmp, [("3-1", "34D03DD", 5000),
                                 ("4-2", "3515425", 480),
                                 ("5-1", "34D03E8", 5000)])
        kwargs = dict(
            hardware=GOOD_HARDWARE, paths=paths,
            key_path=Path(tmp) / "k.pem", cert_path=Path(tmp) / "c.pem",
            usb_probe=lambda: local_preflight.probe_usb_serials(sysfs),
            residual_probe=lambda: [],
            platform_probe=lambda p: {"pass": True, "profile": p,
                                      "error": None},
            key_mode_check=lambda p: {"pass": True, "present": True,
                                      "mode": "0o600", "error": None},
            key_cert_check=lambda k, c: True,
        )
        report = local_preflight.run_required_preflight(**kwargs)
        assert report["pass"], report["failures"]

        # 잔존 프로세스 발견 → 자동 kill 없이 거부 (조치 안내 포함).
        kwargs["residual_probe"] = lambda: ["888 launcher"]
        report2 = local_preflight.run_required_preflight(**kwargs)
        assert not report2["pass"]
        assert any("자동 종료하지 않습니다" in f for f in report2["failures"])

        # key-cert 확인 불가(None)도 fail-closed.
        kwargs["residual_probe"] = lambda: []
        kwargs["key_cert_check"] = lambda k, c: None
        report3 = local_preflight.run_required_preflight(**kwargs)
        assert not report3["pass"]
        assert any("확인 불가" in f for f in report3["failures"])


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase I W3 standalone config/preflight 계약")
