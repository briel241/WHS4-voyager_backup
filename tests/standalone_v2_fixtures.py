"""standalone v2(clone-bootstrap) 시험 공용 재료 — Git·binding 없는 환경 재현.

시험은 실제 저장소 트리를 설치본으로 실측(installation_manifest.measure)하고
그 record를 tmp `var/installation/`에 기록해 setup 직후 상태를 만든다.
reference는 v2 스키마로 실제 발급기(issue_reference)·검증기(load_reference)를
그대로 탄다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
for _entry in (os.path.join(_REPO, "VDETER"),
               os.path.join(_REPO, "VDETER", "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from apps.standalone import (  # noqa: E402
    identity,
    installation_manifest,
    provenance,
    qualify_cmd,
    readiness,
    state,
)

#: readiness binding의 stable machine identity (시험 주입값 — /etc/machine-id 대신).
MACHINE_ID = "test-machine-id-0001"

REPO = Path(_REPO)

ENV_MANIFEST = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": "unit-test",
    "usb_devices": "X",
    "numpy_version": "2.5.2",
    "cryptography_version": "50.0.0",
    "uhd_version": "4.6",
    "gnuradio_version": "3.10",
}

OBSERVED = {
    "MASS-A": {"serial": "34D03DD", "usb_path": "3-1",
               "speed_mbps": 5000, "speed_class": "super"},
    "MASS-B": {"serial": "3515425", "usb_path": "4-2",
               "speed_mbps": 480, "speed_class": "high"},
    "ROC": {"serial": "34D03E8", "usb_path": "5-1",
            "speed_mbps": 5000, "speed_class": "super"},
}
USB_ALL = {info["serial"]: {"usb_path": info["usb_path"],
                            "speed_mbps": info["speed_mbps"]}
           for info in OBSERVED.values()}

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

GAIN = {
    "artifact_sha256": "ab" * 32,
    "artifact_input_sha256": "cd" * 32,
    "endpoint_common_rx_gain_db": {"MASS-A": 69, "MASS-B": 69, "ROC": 75},
    "source_run_id": "qual-x-rr-p1",
}

_RECORD_CACHE = {}


def repo_installation_record() -> dict:
    """실제 저장소 실측 record (시험 모듈 내 캐시 — 트리는 시험 중 불변)."""
    if "record" not in _RECORD_CACHE:
        _RECORD_CACHE["record"] = installation_manifest.measure(REPO)
    return json.loads(json.dumps(_RECORD_CACHE["record"]))


def fake_preflight() -> dict:
    return {"pass": True, "failures": [],
            "checks": {"b210": {"pass": True,
                                "observed_by_role": json.loads(
                                    json.dumps(OBSERVED))}}}


def write_hardware_toml(path: Path, *, approved: bool = True) -> None:
    lines = ["schema_version = 1", 'platform_profile = "pi"']
    for role, body in HARDWARE["endpoints"].items():
        lines.append(f'[endpoints."{role}"]')
        for key, value in body.items():
            lines.append(f'{key} = "{value}"')
    lines += ["[wiring]", 'profile = "C1-C6-STANDARD"',
              "approved = " + ("true" if approved else "false")]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_into(paths: state.StatePaths, *, readiness_marker: bool = True) -> dict:
    """setup의 installation 단계 재현: 실제 저장소 실측 → tmp var/installation.

    `readiness_marker=True`면 setup과 같이 migration marker를 manifest에 적고 최초
    `var/readiness/state.json`(generation=1, active/inhibit null)을 만든다."""
    record = repo_installation_record()
    if readiness_marker:
        record.update(readiness.MANIFEST_MARKER)
    installation_manifest.write_installation(paths, record)
    if readiness_marker:
        readiness.ensure_initial_state(paths)
    return record


def endpoints_critical() -> dict:
    return {role: {"serial": i["serial"], "speed_class": i["speed_class"]}
            for role, i in OBSERVED.items()}


def issue_v2_reference(record: dict, fingerprint: str, *, ladder_id="qual-test",
                       tc_results=None, **over) -> dict:
    kwargs = dict(
        ladder_id=ladder_id, installation=record,
        tc_results=tc_results or {"TC-STAGE7-RR-002": "PASS",
                                  "TC-STAGE7-RR-003": "PASS",
                                  "TC-STAGE7-RR-004": "PASS"},
        endpoints_observed=endpoints_critical(),
        platform_profile="pi", wiring=HARDWARE["wiring"],
        identity_fingerprint=fingerprint, gain=GAIN,
        environment_manifest=dict(ENV_MANIFEST),
        environment_manifest_sha256="ef" * 32, phase_run_ids={},
        final_status="RESEARCH_READY",
        provenance=provenance.build_provenance(REPO))
    kwargs.update(over)
    return qualify_cmd.issue_reference(**kwargs)


def prepare_environment(tmp, *, qualified: bool = True,
                        readiness_marker: bool = True) -> tuple:
    """setup(+qualify)이 끝난 기계 상태 (RF 없이). 반환 (paths, record).

    `readiness_marker=False`는 marker 없는 기존 설치(MIGRATION_REQUIRED)를 재현한다."""
    paths = state.StatePaths(tmp)
    paths.root = REPO  # template·vdeter 실물 (설치본 = 저장소 트리)
    paths.ensure_var()
    ident = identity.ensure_identity(paths)
    write_hardware_toml(Path(tmp) / "hardware.toml")
    record = install_into(paths, readiness_marker=readiness_marker)
    if qualified:
        reference = issue_v2_reference(record, ident["fingerprint"])
        qualify_cmd.write_reference(paths.qualification_candidate, reference)
        qualify_cmd.promote_candidate(paths, release_id="qual-test")
    return paths, record


def rewrite_release_reference(paths: state.StatePaths, release_id: str,
                              mutate) -> dict:
    """release의 reference를 변형해 다시 쓰고 sidecar를 갱신한다 (시험 전용 —
    '이전 RF evidence가 다른 설치에서 나왔다'를 모델링)."""
    directory = paths.qualification_releases / release_id
    path = directory / qualify_cmd.REFERENCE_BASENAME
    reference = json.loads(path.read_bytes().decode("utf-8"))
    mutate(reference)
    encoded = json.dumps(reference, ensure_ascii=True, sort_keys=True,
                         indent=1).encode("utf-8")
    path.write_bytes(encoded)
    (directory / (qualify_cmd.REFERENCE_BASENAME + ".sha256")).write_text(
        hashlib.sha256(encoded).hexdigest() + "\n", encoding="utf-8")
    return reference


class Done:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout
