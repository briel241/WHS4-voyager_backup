"""scenario_runner의 `--transport stage7` 오케스트레이션 (Phase B).

역할: 사전 검증 → run 디렉터리 준비 → Stage 7 coordinator(mass-roc workload)
spawn → 대기 → evidence 수집 → cleanup. 시나리오 업무 자체는 coordinator가
spawn하는 `apps/mass_roc_workload_controller.py`가 수행한다.

원칙 (지시서 §3·§8)
    - stage7 조립·연결 실패는 **명시적 FAIL**이다. direct/software로 절대
      fallback하지 않는다.
    - run-id가 이미 결과 디렉터리에 존재하면 어떤 endpoint도 시작하기 전에
      거부한다.
    - configs-dir은 존재하는 resolved config만 허용하며, config metadata
      (`git_state.json`)의 run-id가 요청 run-id와 다르면 거부한다.
    - 중간 실패 시 coordinator 종료 요청 → bounded wait → terminate/kill,
      원래 예외와 cleanup 실패를 모두 evidence에 보존한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: coordinator가 소비하는 resolved-configs 필수 구성물.
_REQUIRED_CONFIG_FILES = (
    os.path.join("endpoints", "mass-a.toml"),
    os.path.join("endpoints", "mass-b.toml"),
    os.path.join("endpoints", "roc.toml"),
    os.path.join("profiles", "data-profile.toml"),
    "system.toml",
    "git_state.json",
)
_REQUIRED_FIXTURES = (
    "start-mass-a.json", "start-mass-b.json", "start-roc.json", "stop.json",
)

#: mass-roc workload가 올라탈 수 있는 coordinator phase.
_ALLOWED_PHASES = ("rr-p2-17", "rr-p2-19")

#: coordinator 전체 대기 상한 (RF 사다리 ~40분 + 시나리오 + 여유).
DEFAULT_COORDINATOR_TIMEOUT_S = 3 * 3600.0


class ServiceRequestError(ValueError):
    """stage7 실행 요청이 사전 검증에서 거부됐다 (endpoint 시작 전)."""


# --------------------------------------------------------------------------
# Phase G — formal service 실행 계층
#
# 정식 진입점은 `scenario_runner.py menu --transport stage7 --profile ...`이며
# 그 아래에서 이 모듈이 독립 service session을 조율한다. 복잡한 내부 로직은
# `apps/service_layer.py`에 두고(별도 CLI 아님) 여기서 재노출한다.
# 아래 기존 `run_service_session()` 계약(= `run all --transport stage7`)은 불변이다.
# --------------------------------------------------------------------------

from apps.service_layer import (  # noqa: E402
    DEFAULT_BATCH_PLAN,
    ServiceSessionError,
    build_coordinator_script,
    prepare_service_config,
    run_service_batch,
    run_service_session,
    verify_remote_environment,
)
from apps.service_session import (  # noqa: E402
    ServiceSession,
    open_service_session,
)

__all__ = [
    "ServiceRequestError", "ServiceSessionError", "run_service_session", "runs_root",
    "validate_service_request", "run_service_session", "run_service_batch",
    "verify_remote_environment", "prepare_service_config",
    "build_coordinator_script", "DEFAULT_BATCH_PLAN",
    # Phase G 최종 모델 — bounded persistent session
    "open_service_session", "ServiceSession",
]


def runs_root(results_dir: str | None = None) -> str:
    return os.path.join(results_dir or os.path.join(_ROOT, "results"), "runs")


def validate_service_request(*, run_id: str, configs_dir: str,
                            results_dir: str | None = None) -> dict:
    """endpoint를 시작하기 전에 요청 전체를 검증한다. 통과 시 metadata 반환."""

    if not run_id or any(sep in run_id for sep in ("/", "\\", "..")):
        raise ServiceRequestError(f"run-id 형식이 잘못됐습니다: {run_id!r}")

    run_dir = os.path.join(runs_root(results_dir), run_id)
    if os.path.exists(run_dir):
        raise ServiceRequestError(
            f"run-id {run_id!r}가 이미 결과 디렉터리에 존재합니다: {run_dir} — "
            "새 run-id를 쓰거나 기존 결과를 다른 곳으로 옮기십시오")

    if not configs_dir or not os.path.isdir(configs_dir):
        raise ServiceRequestError(
            f"configs-dir이 존재하지 않습니다: {configs_dir!r}")
    for relpath in _REQUIRED_CONFIG_FILES:
        if not os.path.isfile(os.path.join(configs_dir, relpath)):
            raise ServiceRequestError(
                f"resolved config 구성물이 없습니다: {relpath} ({configs_dir})")
    fixtures_dir = os.path.join(configs_dir, "fixtures")
    for name in _REQUIRED_FIXTURES:
        if not os.path.isfile(os.path.join(fixtures_dir, name)):
            raise ServiceRequestError(
                f"START/STOP fixture가 없습니다: fixtures/{name} ({configs_dir})")

    with open(os.path.join(configs_dir, "git_state.json"), "rb") as handle:
        try:
            metadata = json.loads(handle.read().decode("utf-8"))
        except ValueError as exc:
            raise ServiceRequestError(f"git_state.json 해석 실패: {exc}") from exc

    if metadata.get("run_id") != run_id:
        raise ServiceRequestError(
            f"config metadata run-id {metadata.get('run_id')!r} != "
            f"요청 run-id {run_id!r} — 다른 run용 config입니다")
    phase = metadata.get("rr_phase")
    if phase not in _ALLOWED_PHASES:
        raise ServiceRequestError(
            f"config가 mass-roc workload가 올라탈 phase로 결속되지 않았습니다 "
            f"(rr_phase={phase!r}, 허용: {_ALLOWED_PHASES})")
    if not metadata.get("baseline_sha256"):
        raise ServiceRequestError(
            "config metadata에 baseline_sha256이 없습니다 (prepare 도구로 "
            "결속된 resolved config가 필요합니다)")
    return metadata


def _config_hashes(configs_dir: str) -> dict:
    hashes = {}
    for base, _dirs, files in os.walk(configs_dir):
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, configs_dir).replace(os.sep, "/")
            with open(path, "rb") as handle:
                hashes[rel] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def _environment_snapshot() -> dict:
    def _git(*args):
        try:
            done = subprocess.run(
                ["git", "-C", _ROOT] + list(args),
                capture_output=True, text=True, timeout=20,
            )
            return done.stdout.strip() if done.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    return {
        "platform": platform.platform(),
        "python": sys.version,
        "hostname": platform.node(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)


def _collect_endpoint_logs(run_dir: str) -> list[str]:
    """launcher/controller log를 §8 구조의 endpoint-logs/로 모은다."""
    import shutil

    out_dir = os.path.join(run_dir, "endpoint-logs")
    os.makedirs(out_dir, exist_ok=True)
    copied = []
    logs_dir = os.path.join(run_dir, "logs")
    if os.path.isdir(logs_dir):
        for name in sorted(os.listdir(logs_dir)):
            try:
                shutil.copy2(os.path.join(logs_dir, name),
                             os.path.join(out_dir, name))
                copied.append(name)
            except OSError:
                pass
    return copied


def run_service_session(*, scenario_id: str, run_id: str, configs_dir: str,
               certificate_case: str = "normal", results_dir: str | None = None,
               spawn=subprocess.Popen,
               timeout_s: float = DEFAULT_COORDINATOR_TIMEOUT_S) -> int:
    """stage7 backend 한 번 실행. 반환: exit code (0=PASS).

    조립·연결·실행 실패는 전부 명시적 FAIL이다 — direct/software fallback 없음.
    """
    metadata = validate_service_request(
        run_id=run_id, configs_dir=configs_dir, results_dir=results_dir)

    run_dir = os.path.join(runs_root(results_dir), run_id)
    os.makedirs(run_dir, exist_ok=False)
    _write_json(os.path.join(run_dir, "run.json"), {
        "run_id": run_id,
        "transport": "stage7",
        "scenario_id": scenario_id,
        "certificate_case": certificate_case,
        "configs_dir": os.path.abspath(configs_dir),
        "coordinator_phase": metadata.get("rr_phase"),
        "baseline_sha256": metadata.get("baseline_sha256"),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _write_json(os.path.join(run_dir, "environment.json"), _environment_snapshot())
    _write_json(os.path.join(run_dir, "config-hashes.json"),
                _config_hashes(configs_dir))

    command = [
        sys.executable,
        os.path.join(_ROOT, "VDETER", "apps", "coordinator_main.py"),
        "--run-id", run_id,
        "--phase", metadata["rr_phase"],
        "--e2e-security-mode", "ENFORCED",
        "--run-root", run_dir,
        "--configs-dir", os.path.abspath(configs_dir),
        "--baseline-sha256", metadata["baseline_sha256"],
        "--workload", "mass-roc",
        "--scenario-id", scenario_id,
        "--certificate-case", certificate_case,
    ]

    final = {
        "run_id": run_id,
        "transport": "stage7",
        "scenario_id": scenario_id,
        "certificate_case": certificate_case,
        "status": "FAIL",
        "coordinator_rc": None,
        "error": None,
        "cleanup": [],
        "scenario_results": None,
    }
    process = None
    try:
        try:
            process = spawn(command)
        except Exception as exc:  # noqa: BLE001 - 조립 실패 = 명시적 FAIL
            final["error"] = f"coordinator spawn 실패: {type(exc).__name__}: {exc}"
            return 1
        try:
            final["coordinator_rc"] = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            final["error"] = f"coordinator {timeout_s:.0f}s 초과 — 강제 종료"
            return 1
        except KeyboardInterrupt:
            final["error"] = "사용자 중단 (KeyboardInterrupt)"
            raise
        scenario_result_path = os.path.join(
            run_dir, "mass-roc-events", "final-result.json")
        try:
            with open(scenario_result_path, "rb") as handle:
                final["scenario_results"] = json.loads(handle.read().decode("utf-8"))
        except OSError:
            final["error"] = final["error"] or (
                "mass-roc-events/final-result.json 없음 — controller 미완주")
        scenario_status = (final["scenario_results"] or {}).get("status")
        if final["coordinator_rc"] == 0 and scenario_status == "PASS":
            final["status"] = "PASS"
        elif final["error"] is None:
            final["error"] = (
                f"coordinator rc={final['coordinator_rc']}, "
                f"scenario status={scenario_status!r}")
        return 0 if final["status"] == "PASS" else 1
    finally:
        # cleanup — 원래 예외/실패 사유와 cleanup 결과를 모두 보존한다.
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                final["cleanup"].append("coordinator terminate 요청")
                try:
                    process.wait(timeout=30.0)
                    final["cleanup"].append("coordinator 종료 확인")
                except subprocess.TimeoutExpired:
                    kill = getattr(process, "kill", None)
                    if kill is not None:
                        kill()
                    final["cleanup"].append(
                        "coordinator terminate 30s 초과 — kill")
            except Exception as exc:  # noqa: BLE001
                final["cleanup"].append(f"cleanup 실패: {exc}")
        final["endpoint_logs"] = _collect_endpoint_logs(run_dir)
        _write_json(os.path.join(run_dir, "final-result.json"), final)
