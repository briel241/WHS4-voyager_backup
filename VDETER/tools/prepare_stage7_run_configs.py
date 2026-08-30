"""Stage 7 run별 resolved config 준비 (`stage7` §9.1.1, §15).

endpoint TOML template의 `REQUIRED_UNSET` provisioning field를 실제 값으로
치환해 run 전용 config를 만들고 loader로 검증한 뒤 Pi에 배포한다.

- environment manifest: Pi에서 §9.1.1 환경 상태를 수집해 JSON으로 기록.
- deployment manifest: `tools/deploy_stage7_to_pi.py`가 만든 run manifest.
- `external_power_asset`(MASS-B): 사용자만 제공할 수 있는 물리 자산 ID다.
  값이 없으면 MASS-B config는 생성하지 않고 blocker로 보고한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

ENDPOINTS = ("mass-a", "mass-b", "roc")

ENVIRONMENT_PROBE = r"""
set -e
python3 - <<'PY'
import json, subprocess, hashlib, os

def run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as error:
        return f"<error {error}>"

manifest = {
    "manifest_type": "STAGE7_ENVIRONMENT_MANIFEST_V1",
    "uname": run("uname", "-a"),
    "os_release": run("sh", "-c", ". /etc/os-release && echo $ID $VERSION_ID"),
    "machine_id": run("cat", "/etc/machine-id"),
    "pi_model": run("cat", "/proc/device-tree/model").replace("\x00", ""),
    "pi_revision": run("sh", "-c", "grep Revision /proc/cpuinfo | awk '{print $3}'"),
    "throttled": run("vcgencmd", "get_throttled"),
    "governor": run("cat", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
    "psu_max_current": run("vcgencmd", "pmic_read_adc"),
    "uhd_version": run("sh", "-c", "python3 -c 'import uhd; print(uhd.get_version_string())' 2>/dev/null || uhd_config_info --version 2>/dev/null | head -1"),
    "gnuradio_version": run("sh", "-c", "python3 -c 'from gnuradio import gr; print(gr.version())' 2>/dev/null"),
    "cryptography_version": run("sh", "-c", "python3 -c 'import cryptography; print(cryptography.__version__)'"),
    "runtime_python": os.path.expanduser("~/vdeter-stage7/venv/bin/python3"),
    "runtime_cryptography_version": run(
        "sh", "-c",
        "~/vdeter-stage7/venv/bin/python3 -c 'import cryptography; print(cryptography.__version__)' 2>/dev/null || echo 'venv-missing'",
    ),
    "numpy_version": run("sh", "-c", "python3 -c 'import numpy; print(numpy.__version__)'"),
    "usb_devices": run("sh", "-c", "for d in /sys/bus/usb/devices/*/serial; do echo $d $(cat $d); done"),
}
print(json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=1))
PY
"""


def probe_environment(host: str, remote_dir: str):
    """Pi에서 환경 manifest를 수집·기록하고 (relpath, sha256)을 반환한다."""

    collected = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, ENVIRONMENT_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    data = collected.stdout.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    remote_path = f"{remote_dir}/environment_manifest.json"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         f"mkdir -p {remote_dir} && cat > {remote_path}"],
        input=data,
        timeout=60,
        check=True,
    )
    return remote_path, digest, data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--deploy-run-id", required=True)
    parser.add_argument("--host", default="voyager3@192.168.0.179")
    parser.add_argument("--external-power-asset-mass-b", default=None)
    parser.add_argument("--run-mode", default=None, help="치환할 run_mode (선택)")
    parser.add_argument(
        "--gain-artifact", default=None,
        help="OPERATIONAL 검증용 gain artifact input JSON (endpoint별 공통 gain)",
    )
    parser.add_argument(
        "--rr-phase", default=None,
        choices=("rr-p0", "rr-p1", "rr-p2-17", "rr-p2-19",
                 "rr-p3-fault", "rr-p3-soak-17", "rr-p3-soak-19"),
        help="RESEARCH_READY_MINIMAL phase 결속 (§17A/v0.9.1 I항)",
    )
    arguments = parser.parse_args(argv)

    from vdeter.stage7.configuration import (
        Stage7ConfigurationLoader,
        Stage7OperationalGainInput,
    )

    gain_input_data = None
    if arguments.gain_artifact is not None:
        import json as _json

        with open(arguments.gain_artifact, "rb") as handle:
            gain_input_data = _json.loads(handle.read().decode("utf-8"))

    manifest_local = os.path.join(
        VDETER_ROOT, "results", "runs", arguments.deploy_run_id, "deployment_manifest.json"
    )
    with open(manifest_local, "rb") as handle:
        deployment_sha = hashlib.sha256(handle.read()).hexdigest()
    deployment_relpath = f"results/runs/{arguments.deploy_run_id}/deployment_manifest.json"

    remote_env_dir = f"~/vdeter-stage7/{arguments.deploy_run_id}/env/{arguments.run_id}"
    environment_path, environment_sha, environment_bytes = probe_environment(
        arguments.host, remote_env_dir
    )
    out_dir = os.path.join(VDETER_ROOT, "results", "runs", arguments.run_id, "resolved-configs")
    os.makedirs(os.path.join(out_dir, "endpoints"), exist_ok=True)
    with open(os.path.join(out_dir, "environment_manifest.json"), "wb") as handle:
        handle.write(environment_bytes)
    print(f"[environment] {environment_path} sha256={environment_sha}")

    with open(
        os.path.join(VDETER_ROOT, "configs", "stage7", "profiles", "data-profile.toml"),
        "r", encoding="utf-8",
    ) as handle:
        data_profile_text = handle.read()

    loader = Stage7ConfigurationLoader()
    blockers = []
    written = []
    for endpoint in ENDPOINTS:
        template_path = os.path.join(
            VDETER_ROOT, "configs", "stage7", "endpoints", f"{endpoint}.toml"
        )
        with open(template_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        text = text.replace(
            'environment_manifest_path = "REQUIRED_UNSET"',
            f'environment_manifest_path = "{environment_path}"',
        ).replace(
            'environment_manifest_sha256 = "REQUIRED_UNSET"',
            f'environment_manifest_sha256 = "{environment_sha}"',
        ).replace(
            'deployment_manifest_relpath = "REQUIRED_UNSET"',
            f'deployment_manifest_relpath = "{deployment_relpath}"',
        ).replace(
            'deployment_manifest_sha256 = "REQUIRED_UNSET"',
            f'deployment_manifest_sha256 = "{deployment_sha}"',
        )
        if arguments.run_mode is not None:
            import re as _re

            text = _re.sub(
                r'run_mode = "[A-Z_]+"', f'run_mode = "{arguments.run_mode}"', text
            )
        if endpoint == "mass-b":
            if arguments.external_power_asset_mass_b is None:
                blockers.append(
                    "mass-b.toml external_power_asset: 사용자 제공 필요 "
                    "(--external-power-asset-mass-b)"
                )
                continue
            text = text.replace(
                'external_power_asset = "REQUIRED_UNSET"',
                f'external_power_asset = "{arguments.external_power_asset_mass_b}"',
            )
        operational_gain = None
        if gain_input_data is not None:
            common = gain_input_data["endpoint_common_rx_gain_db"]
            endpoint_key = endpoint.upper()
            value = common[endpoint_key] if isinstance(common, dict) else common
            operational_gain = Stage7OperationalGainInput(
                endpoint_common_rx_gain_db=int(value),
                source_gain_artifact_sha256=gain_input_data[
                    "source_gain_artifact_sha256"
                ],
            )
        loaded = loader.load_endpoint_configuration(
            text, data_profile_text, run_id=arguments.run_id,
            operational_gain=operational_gain,
            source_path=f"resolved-configs/endpoints/{endpoint}.toml",
        )
        if loaded.is_err:
            print(f"[error] {endpoint}: {loaded.error}", file=sys.stderr)
            return 2
        out_path = os.path.join(out_dir, "endpoints", f"{endpoint}.toml")
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        written.append((endpoint, loaded.value.resolved_sha256))
        print(f"[config] {endpoint} resolved_sha256={loaded.value.resolved_sha256}")

    for name in ("system.toml",):
        source = os.path.join(VDETER_ROOT, "configs", "stage7", name)
        with open(source, "rb") as handle:
            payload = handle.read()
        with open(os.path.join(out_dir, name), "wb") as handle:
            handle.write(payload)

    # §17A.6: RR evidence용 git/baseline/hardware metadata를 run configs에 결속.
    def _git(*args):
        try:
            done = subprocess.run(
                ["git", "-C", os.path.join(VDETER_ROOT, "..")] + list(args),
                capture_output=True, text=True, timeout=20,
            )
            return done.stdout.strip() if done.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    baseline_path = os.path.abspath(
        os.path.join(VDETER_ROOT, "..", "..", "docs", "core",
                     "stage7_b210_system_baseline.md")
    )
    baseline_sha = None
    baseline_version = None
    try:
        baseline_bytes = open(baseline_path, "rb").read()
        baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
        for line in baseline_bytes.decode("utf-8", "replace").splitlines()[:8]:
            if line.startswith("문서 버전"):
                baseline_version = line.split("`")[1] if "`" in line else None
                break
    except OSError:
        pass
    serials = {}
    try:
        env = json.loads(environment_bytes.decode("utf-8"))
        serials = {"usb_devices": env.get("usb_devices")}
    except Exception:  # noqa: BLE001
        pass
    rr_link = None
    if arguments.rr_phase and arguments.rr_phase.endswith(("17", "19")):
        rr_link = int(arguments.rr_phase[-2:])
    git_state = {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "baseline_version": baseline_version,
        "baseline_sha256": baseline_sha,
        "environment_manifest_sha256": environment_sha,
        "hardware": serials,
        # v0.9.1 I항: qualification profile을 구성에 명시 결속.
        "qualification_profile": (
            "RESEARCH_READY_MINIMAL" if arguments.rr_phase else None
        ),
        "rr_phase": arguments.rr_phase,
        "run_id": arguments.run_id,
        "data_link_id": rr_link,
        "implicit_retry": 0,
        "phase_deadlines_s": {
            "rr-p0": 900, "rr-p1": 3600, "rr-p1-gain": 1200,
            "rr-p2-17": 2400, "rr-p2-19": 2400, "rr-p3-fault": 2400,
            "rr-p3-soak-17": 600, "rr-p3-soak-19": 600,
        },
    }
    with open(os.path.join(out_dir, "git_state.json"), "wb") as handle:
        handle.write(
            json.dumps(git_state, ensure_ascii=True, sort_keys=True, indent=1).encode()
        )

    # v0.9.1 G/H항: RR-P2/soak scenario fixture (canonical JSON, adapter 스키마).
    needs_fixtures = arguments.rr_phase in (
        "rr-p2-17", "rr-p2-19", "rr-p3-soak-17", "rr-p3-soak-19", "rr-p3-fault",
    )
    if needs_fixtures:
        import base64

        from vdeter.stage7.canonical_json import encode_canonical_json

        def payload_bytes(length, seed):
            return bytes((i * 7 + seed) % 256 for i in range(length))

        station = {"mass-a": 111111111, "mass-b": 333333333, "roc": 222222222}
        role = {"mass-a": "SHIP", "mass-b": "SHIP", "roc": "CONTROL"}
        fixtures = {}
        for endpoint in ENDPOINTS:
            fixtures[f"start-{endpoint}.json"] = {
                "local_station_id": station[endpoint],
                "role": role[endpoint],
                "config_ref": "",
            }
        # v0.9.11 정리: 확정 응용 시나리오(SC-VDE-001/005/006)가 요구하는
        # 전송 유형만 유지. 함정↔함정 직송·릴레이·함정발 데이터는 요구 부재로
        # 제거(기준선 "후속 구현 단계" 절에 유예 기록). data-roc-to-a는
        # 인증서(≤1024B) 운송로인 관제→함정 데이터 세션 시험.
        submits = {
            "short-roc-to-a": (station["mass-a"], 29, 1, True),
            "data-roc-to-a": (station["mass-a"], 353, 7, True),
            "short-b-to-roc-noack": (station["roc"], 30, 5, False),
            "timeout-unknown-dest": (999999999, 29, 6, True),
        }
        for name, (destination, length, seed, ack) in submits.items():
            fixtures[f"submit-{name}.json"] = {
                "destination": destination,
                "payload_format": "test.stage0.opaque",
                "payload_base64": base64.b64encode(
                    payload_bytes(length, seed)
                ).decode("ascii"),
                "ack_required": ack,
            }
        fixtures["stop.json"] = {"reason": "rr scenario complete"}
        fixtures_dir = os.path.join(out_dir, "fixtures")
        os.makedirs(fixtures_dir, exist_ok=True)
        for name, value in fixtures.items():
            with open(os.path.join(fixtures_dir, name), "wb") as handle:
                handle.write(encode_canonical_json(value))
        print(f"[fixtures] {len(fixtures)}개 생성")

    if written:
        remote_config_dir = (
            f"~/vdeter-stage7/{arguments.deploy_run_id}/run-configs/{arguments.run_id}"
        )
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", arguments.host,
             f"mkdir -p {remote_config_dir}/endpoints {remote_config_dir}/profiles"],
            timeout=60, check=True,
        )
        for endpoint, _sha in written:
            subprocess.run(
                ["scp", "-q",
                 os.path.join(out_dir, "endpoints", f"{endpoint}.toml"),
                 f"{arguments.host}:{remote_config_dir}/endpoints/"],
                timeout=60, check=True,
            )
        subprocess.run(
            ["scp", "-q", os.path.join(out_dir, "system.toml"),
             f"{arguments.host}:{remote_config_dir}/"],
            timeout=60, check=True,
        )
        subprocess.run(
            ["scp", "-q", os.path.join(out_dir, "git_state.json"),
             f"{arguments.host}:{remote_config_dir}/"],
            timeout=60, check=True,
        )
        if needs_fixtures:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", arguments.host,
                 f"mkdir -p {remote_config_dir}/fixtures"],
                timeout=60, check=True,
            )
            for name in sorted(os.listdir(os.path.join(out_dir, "fixtures"))):
                subprocess.run(
                    ["scp", "-q", os.path.join(out_dir, "fixtures", name),
                     f"{arguments.host}:{remote_config_dir}/fixtures/"],
                    timeout=60, check=True,
                )
        subprocess.run(
            ["scp", "-q",
             os.path.join(VDETER_ROOT, "configs", "stage7", "profiles", "data-profile.toml"),
             f"{arguments.host}:{remote_config_dir}/profiles/"],
            timeout=60, check=True,
        )
        print(f"[deployed] {remote_config_dir}")

    if blockers:
        print("[blockers]")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
