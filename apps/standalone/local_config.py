"""로컬 resolved config 생성기 — run·qualify 공통 (Phase I W3, README §4~§6).

host `VDETER/tools/prepare_stage7_run_configs.py`의 로컬 대응물. 같은
template·같은 loader·같은 fixture 규칙을 쓰되, 출처를 바꾼다:

    host git commit / Markdown 재해시  →  standalone-binding 승계 (결정 8)
    SSH 환경 probe                     →  로컬 실측
    scp 배포                           →  로컬 디렉터리 기록

git_state.json은 **파일명·스키마를 유지**한다 (coordinator
`_load_run_metadata`/`validate_rr_binding` 무수정 호환 — 승인 조건 1).
필드별 출처는 README §6 표가 정본이다.

이 모듈은 정책을 정하지 않는다 — rr_phase·run_mode·gain 입력은 호출자
(run_cmd/qualify_cmd)가 결정해 넘긴다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

from apps.standalone.state import write_json_atomic

ENDPOINTS = ("mass-a", "mass-b", "roc")

#: coordinator `RR_PHASE_DEADLINE_S` 정본과 일치해야 하는 결속값 (판정 phase)
#: + 비판정 기록용 (rr-p1-gain·soak). host prepare 도구와 동일 값.
PHASE_DEADLINES_S = {
    "rr-p0": 900, "rr-p1": 3600, "rr-p1-gain": 1200,
    "rr-p2-17": 4200, "rr-p2-19": 4200, "rr-p3-fault": 2400,
    "rr-p3-soak-17": 600, "rr-p3-soak-19": 600,
}

#: fixture가 필요한 결속 phase (host prepare와 동일 목록).
_PHASES_NEEDING_FIXTURES = (
    "rr-p2-17", "rr-p2-19", "rr-p3-soak-17", "rr-p3-soak-19", "rr-p3-fault",
)

RESEARCH_READY_PROFILE = "RESEARCH_READY_MINIMAL"

#: qualification-critical 대조 대상 라이브러리 4종 (README §8, W1 수정 1).
CRITICAL_LIBRARIES = ("gnuradio", "uhd", "numpy", "cryptography")


class ConfigGenerationError(RuntimeError):
    """config 생성 거부 — 항상 RF 접근 전. 메시지는 조치 가능해야 한다."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


# ------------------------------------------------------------ binding·선언

def load_standalone_binding(path: Path | str) -> dict:
    """standalone-binding.json을 읽고 스키마를 검증한다 (README §5)."""
    path = Path(path)
    if not path.is_file():
        raise ConfigGenerationError(
            f"standalone-binding이 없습니다: {path} — 배포물 패키징 시 "
            "make_standalone_binding.py가 생성해야 합니다")
    binding = json.loads(path.read_bytes().decode("utf-8"))
    if binding.get("artifact_type") != "STAGE7_STANDALONE_BINDING_V1":
        raise ConfigGenerationError("standalone-binding artifact_type 불일치")
    head = binding.get("source_head")
    if not (isinstance(head, str) and len(head) == 40
            and all(c in "0123456789abcdef" for c in head)):
        raise ConfigGenerationError(
            "standalone-binding source_head가 40-hex commit이 아닙니다")
    if binding.get("source_worktree_dirty") is not False:
        raise ConfigGenerationError(
            "standalone-binding source_worktree_dirty != false — dirty "
            "트리에서 패키징된 배포물은 결속 근거가 없습니다")
    if not _is_sha256(binding.get("baseline_sha256")):
        raise ConfigGenerationError("standalone-binding baseline_sha256 비정상")
    frozen = binding.get("frozen_files")
    if not isinstance(frozen, dict) or not frozen:
        raise ConfigGenerationError("standalone-binding frozen_files 부재")
    return binding


def parse_hardware_declaration(path: Path | str) -> dict:
    """hardware.toml(사용자 선언)을 읽고 스키마·승인 상태를 검증한다."""
    path = Path(path)
    if not path.is_file():
        raise ConfigGenerationError(
            f"hardware.toml이 없습니다: {path} — `vdeter setup`이 만든 초안을 "
            "편집·승인하십시오 (templates/hardware.toml.example 참조)")
    with open(path, "rb") as handle:
        declaration = tomllib.load(handle)
    errors = []
    if declaration.get("schema_version") != 1:
        errors.append("schema_version은 1이어야 합니다")
    if declaration.get("platform_profile") not in ("pi", "generic-pc"):
        errors.append("platform_profile은 'pi' 또는 'generic-pc'여야 합니다")
    endpoints = declaration.get("endpoints") or {}
    for role in ("MASS-A", "MASS-B", "ROC"):
        serial = (endpoints.get(role) or {}).get("serial")
        if not serial or not isinstance(serial, str):
            errors.append(f"endpoints.{role}.serial이 없습니다")
    if not (endpoints.get("MASS-B") or {}).get("external_power_asset"):
        errors.append("endpoints.MASS-B.external_power_asset이 없습니다 "
                      "(사용자만 제공할 수 있는 물리 자산 ID)")
    serials = [
        (endpoints.get(role) or {}).get("serial")
        for role in ("MASS-A", "MASS-B", "ROC")
    ]
    if len(set(filter(None, serials))) != len(list(filter(None, serials))):
        errors.append("역할별 serial이 중복됩니다")
    wiring = declaration.get("wiring") or {}
    if not wiring.get("profile"):
        errors.append("wiring.profile이 없습니다")
    if wiring.get("approved") is not True:
        errors.append("wiring.approved = true가 아닙니다 — 배선을 확인한 뒤 "
                      "사용자가 직접 승인해야 실행할 수 있습니다")
    if errors:
        raise ConfigGenerationError(
            "hardware.toml 검증 실패:\n  - " + "\n  - ".join(errors))
    return declaration


# ------------------------------------------------------------ 환경 manifest

def _probe_versions() -> dict:
    """critical 라이브러리 4종 + 파이썬 버전 실측 (import 기반, 로컬)."""
    versions = {}
    for name, module, attr in (
        ("numpy", "numpy", "__version__"),
        ("cryptography", "cryptography", "__version__"),
    ):
        try:
            imported = __import__(module)
            versions[name] = str(getattr(imported, attr))
        except Exception:  # noqa: BLE001 - 부재는 null로 정직 기록
            versions[name] = None
    try:
        import uhd  # type: ignore
        versions["uhd"] = str(uhd.get_version_string())
    except Exception:  # noqa: BLE001
        versions["uhd"] = None
    try:
        from gnuradio import gr  # type: ignore
        versions["gnuradio"] = str(gr.version())
    except Exception:  # noqa: BLE001
        versions["gnuradio"] = None
    return versions


def _probe_usb_devices(sysfs_root: str = "/sys/bus/usb/devices") -> str:
    """host probe와 같은 한 줄 포맷: '<경로>/serial <값>' 나열."""
    lines = []
    root = Path(sysfs_root)
    if root.is_dir():
        for serial_file in sorted(root.glob("*/serial")):
            try:
                value = serial_file.read_text(encoding="ascii",
                                              errors="replace").strip()
            except OSError:
                continue
            lines.append(f"{serial_file} {value}")
    return "\n".join(lines)


def build_environment_manifest(*, prober=None) -> dict:
    """로컬 환경 manifest (host ENVIRONMENT_PROBE의 로컬판).

    `prober`가 주어지면 그 dict 값으로 전부 대체한다 (시험용)."""
    if prober is not None:
        manifest = dict(prober)
        manifest.setdefault("manifest_type", "STAGE7_ENVIRONMENT_MANIFEST_V1")
        return manifest
    versions = _probe_versions()
    manifest = {
        "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
        "uname": " ".join(platform.uname()),
        "machine": platform.machine(),
        "python": sys.version,
        "runtime_python": sys.executable,
        "numpy_version": versions["numpy"],
        "cryptography_version": versions["cryptography"],
        "uhd_version": versions["uhd"],
        "gnuradio_version": versions["gnuradio"],
        "usb_devices": _probe_usb_devices(),
    }
    return manifest


# ------------------------------------------------------------ fixtures

def _payload_bytes(length: int, seed: int) -> bytes:
    return bytes((i * 7 + seed) % 256 for i in range(length))


#: host prepare 도구와 동일한 station/역할/제출 표 (v0.9.11 확정 시나리오).
_STATION = {"mass-a": 111111111, "mass-b": 333333333, "roc": 222222222}
_ROLE = {"mass-a": "SHIP", "mass-b": "SHIP", "roc": "CONTROL"}
_SUBMITS = {
    "short-roc-to-a": (_STATION["mass-a"], 29, 1, True),
    "data-roc-to-a": (_STATION["mass-a"], 353, 7, True),
    "short-b-to-roc-noack": (_STATION["roc"], 30, 5, False),
    "timeout-unknown-dest": (999999999, 29, 6, True),
}


def build_fixtures() -> dict:
    """START/submit/STOP fixture — host prepare와 byte 동일 (canonical JSON)."""
    fixtures = {}
    for endpoint in ENDPOINTS:
        fixtures[f"start-{endpoint}.json"] = {
            "local_station_id": _STATION[endpoint],
            "role": _ROLE[endpoint],
            "config_ref": "",
        }
    for name, (destination, length, seed, ack) in _SUBMITS.items():
        fixtures[f"submit-{name}.json"] = {
            "destination": destination,
            "payload_format": "test.stage0.opaque",
            "payload_base64": base64.b64encode(
                _payload_bytes(length, seed)).decode("ascii"),
            "ack_required": ack,
        }
    fixtures["stop.json"] = {"reason": "rr scenario complete"}
    return fixtures


# ------------------------------------------------------------ 생성기 본체

def _ensure_vdeter_importable(repo_root: Path) -> None:
    for entry in (repo_root / "VDETER", repo_root / "VDETER" / "src"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def generate_run_configs(*, repo_root: Path | str, run_root: Path | str,
                         run_id: str, rr_phase: str,
                         hardware: dict, binding: dict,
                         deployment_manifest_relpath: str,
                         deployment_manifest_sha256: str,
                         deploy_verified: bool,
                         run_mode: str | None = None,
                         gain_input: dict | None = None,
                         environment_manifest: dict | None = None) -> dict:
    """run별 resolved config 일습을 생성·검증한다 (README §6 표 구현).

    반환: {"configs_dir", "git_state", "resolved_sha256": {endpoint: sha},
           "environment_manifest_sha256", "fixtures": [이름...]}.
    실패는 전부 `ConfigGenerationError` — RF 접근 전이다.
    """
    repo_root = Path(repo_root)
    run_root = Path(run_root)

    if deploy_verified is not True:
        raise ConfigGenerationError(
            "배포 tree 무결 검증이 통과하지 않았습니다 — git_dirty=false를 "
            "결속할 근거가 없어 config를 생성하지 않습니다")
    if rr_phase not in PHASE_DEADLINES_S:
        raise ConfigGenerationError(f"알 수 없는 rr_phase: {rr_phase!r}")
    if not _is_sha256(deployment_manifest_sha256):
        raise ConfigGenerationError("deployment_manifest_sha256이 sha256이 아님")

    _ensure_vdeter_importable(repo_root)
    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.configuration import (
        Stage7ConfigurationLoader,
        Stage7OperationalGainInput,
    )

    template_dir = repo_root / "VDETER" / "configs" / "stage7"
    out_dir = run_root / "resolved-configs"
    (out_dir / "endpoints").mkdir(parents=True, exist_ok=True)
    (out_dir / "profiles").mkdir(parents=True, exist_ok=True)

    # --- 환경 manifest (로컬 실측 또는 주입) -----------------------------
    manifest = environment_manifest or build_environment_manifest()
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, indent=1).encode("utf-8")
    manifest_path = out_dir / "environment_manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha = _sha256_bytes(manifest_bytes)

    data_profile_text = (template_dir / "profiles"
                         / "data-profile.toml").read_text(encoding="utf-8")
    (out_dir / "profiles" / "data-profile.toml").write_text(
        data_profile_text, encoding="utf-8", newline="\n")

    endpoints_decl = hardware.get("endpoints") or {}
    loader = Stage7ConfigurationLoader()
    resolved_sha = {}
    for endpoint in ENDPOINTS:
        text = (template_dir / "endpoints"
                / f"{endpoint}.toml").read_text(encoding="utf-8")
        text = text.replace(
            'environment_manifest_path = "REQUIRED_UNSET"',
            f'environment_manifest_path = "{manifest_path.as_posix()}"',
        ).replace(
            'environment_manifest_sha256 = "REQUIRED_UNSET"',
            f'environment_manifest_sha256 = "{manifest_sha}"',
        ).replace(
            'deployment_manifest_relpath = "REQUIRED_UNSET"',
            f'deployment_manifest_relpath = "{deployment_manifest_relpath}"',
        ).replace(
            'deployment_manifest_sha256 = "REQUIRED_UNSET"',
            f'deployment_manifest_sha256 = "{deployment_manifest_sha256}"',
        )
        if run_mode is not None:
            text = re.sub(r'run_mode = "[A-Z_]+"',
                          f'run_mode = "{run_mode}"', text)
        if endpoint == "mass-b":
            asset = (endpoints_decl.get("MASS-B") or {}).get(
                "external_power_asset")
            text = text.replace(
                'external_power_asset = "REQUIRED_UNSET"',
                f'external_power_asset = "{asset}"',
            )
        operational_gain = None
        if gain_input is not None:
            common = gain_input["endpoint_common_rx_gain_db"]
            key = endpoint.upper()
            value = common[key] if isinstance(common, dict) else common
            operational_gain = Stage7OperationalGainInput(
                endpoint_common_rx_gain_db=int(value),
                source_gain_artifact_sha256=gain_input[
                    "source_gain_artifact_sha256"],
            )
        loaded = loader.load_endpoint_configuration(
            text, data_profile_text, run_id=run_id,
            operational_gain=operational_gain,
            source_path=f"resolved-configs/endpoints/{endpoint}.toml",
        )
        if loaded.is_err:
            raise ConfigGenerationError(
                f"{endpoint} config 검증 실패: {loaded.error}")
        out_path = out_dir / "endpoints" / f"{endpoint}.toml"
        out_path.write_text(text, encoding="utf-8", newline="\n")
        resolved_sha[endpoint] = loaded.value.resolved_sha256

    (out_dir / "system.toml").write_bytes(
        (template_dir / "system.toml").read_bytes())

    # --- git_state.json — 호환 스키마, 출처는 binding/로컬 (README §6 표) --
    usb_devices = manifest.get("usb_devices")
    data_link_id = (int(rr_phase[-2:])
                    if rr_phase.endswith(("17", "19")) else None)
    git_state = {
        "git_commit": binding["source_head"],
        "git_branch": binding.get("source_branch"),
        # 배포 tree 무결 검증 통과가 전제됐다 (위 deploy_verified 게이트).
        "git_dirty": False,
        "baseline_version": binding.get("baseline_version"),
        "baseline_sha256": binding["baseline_sha256"],
        "environment_manifest_sha256": manifest_sha,
        "hardware": {"usb_devices": usb_devices},
        "qualification_profile": RESEARCH_READY_PROFILE,
        "rr_phase": rr_phase,
        "run_id": run_id,
        "data_link_id": data_link_id,
        "implicit_retry": 0,
        "phase_deadlines_s": dict(PHASE_DEADLINES_S),
    }
    write_json_atomic(out_dir / "git_state.json", git_state)

    # --- fixtures (host와 동일 canonical JSON) ---------------------------
    fixture_names = []
    if rr_phase in _PHASES_NEEDING_FIXTURES:
        fixtures_dir = out_dir / "fixtures"
        fixtures_dir.mkdir(exist_ok=True)
        for name, value in build_fixtures().items():
            (fixtures_dir / name).write_bytes(encode_canonical_json(value))
            fixture_names.append(name)

    return {
        "configs_dir": out_dir,
        "git_state": git_state,
        "resolved_sha256": resolved_sha,
        "environment_manifest_sha256": manifest_sha,
        "fixtures": sorted(fixture_names),
    }
