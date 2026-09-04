"""scenario_runner CLI 회귀 — direct 보존·run all·transport 규칙·stage7 검증.

실행: VDETER\\.venv\\Scripts\\python.exe -m pytest tests/test_scenario_runner_cli.py -q

Phase B 테스트 요구 1~6·17을 잠근다:
기존 direct 단일 회귀, `run all` 비대화형, `--transport` parser, stage7의
run-id/configs-dir 필수, 중복 run-id 거부(endpoint 시작 전), stage7 실패의
direct fallback 금지, 실패 중 cleanup·evidence 보존.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

import apps.scenario_runner as runner
import apps.stage7_run as stage7_run

NOW = 1_760_000_000.0


# ------------------------------------------------------------ 시험 보조

def _make_valid_configs(root: str, run_id: str, *, rr_phase="rr-p2-17") -> str:
    """검증을 통과하는 최소 resolved-configs 디렉터리를 만든다."""
    configs = os.path.join(root, "resolved-configs")
    os.makedirs(os.path.join(configs, "endpoints"))
    os.makedirs(os.path.join(configs, "profiles"))
    os.makedirs(os.path.join(configs, "fixtures"))
    for name in ("mass-a.toml", "mass-b.toml", "roc.toml"):
        open(os.path.join(configs, "endpoints", name), "w").close()
    open(os.path.join(configs, "profiles", "data-profile.toml"), "w").close()
    open(os.path.join(configs, "system.toml"), "w").close()
    for name in ("start-mass-a.json", "start-mass-b.json", "start-roc.json",
                 "stop.json"):
        open(os.path.join(configs, "fixtures", name), "w").close()
    with open(os.path.join(configs, "git_state.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"run_id": run_id, "rr_phase": rr_phase,
                   "baseline_sha256": "a" * 64}, handle)
    return configs


class _SpawnRecorder:
    """spawn 호출 여부를 기록한다 (endpoint 시작 전 거부 검증용)."""

    def __init__(self, error: Exception | None = None):
        self.calls = []
        self._error = error

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self._error is not None:
            raise self._error
        raise AssertionError("이 시험에서는 실제 spawn까지 가면 안 된다")


# ------------------------------------------------------------ 1·2: direct 회귀

def test_direct_single_scenario_regression():
    # 기존 호출 형태 그대로 — transport 인자 없이 direct로 동작해야 한다.
    assert runner.main(["run", "SC-VDE-005", "--no-save",
                        "--now", str(NOW)]) == 0
    assert runner.main(["run", "SC-VDE-001", "--no-save",
                        "--now", str(NOW)]) == 0


def test_run_all_direct_noninteractive():
    # run all은 대화형 입력 없이 세 시나리오를 전부 실행해야 한다.
    original_input = builtins.input

    def forbidden_input(*_a, **_k):
        raise AssertionError("run all이 대화형 입력을 요구하면 안 된다")

    builtins.input = forbidden_input
    try:
        assert runner.main(["run", "all", "--no-save", "--now", str(NOW)]) == 0
    finally:
        builtins.input = original_input


def test_unknown_scenario_still_rejected():
    assert runner.main(["run", "SC-VDE-999", "--no-save"]) == 2


# ------------------------------------------------------------ 3: parser 규칙

def test_transport_parser_default_and_choices():
    parser = runner.build_parser()
    args = parser.parse_args(["run", "SC-VDE-005"])
    assert args.transport == "direct"
    assert args.run_id is None and args.configs_dir is None
    assert args.certificate_case is None

    args = parser.parse_args([
        "run", "SC-VDE-001", "--transport", "stage7",
        "--run-id", "sc001-rf-001", "--configs-dir", "X",
        "--certificate-case", "both",
    ])
    assert args.transport == "stage7"
    assert args.run_id == "sc001-rf-001"
    assert args.certificate_case == "both"


def test_certificate_case_rejected_for_direct():
    assert runner.main(["run", "SC-VDE-001", "--no-save",
                        "--certificate-case", "forged"]) == 2


def test_configs_dir_rejected_for_non_stage7():
    assert runner.main(["run", "SC-VDE-005", "--no-save",
                        "--configs-dir", "X"]) == 2


# ------------------------------------------------------------ 4: stage7 필수 인자

def test_stage7_requires_run_id_and_configs_dir():
    assert runner.main(["run", "SC-VDE-005", "--transport", "stage7"]) == 2
    assert runner.main(["run", "SC-VDE-005", "--transport", "stage7",
                        "--run-id", "x"]) == 2
    with tempfile.TemporaryDirectory() as tmp:
        configs = _make_valid_configs(tmp, "x")
        assert runner.main(["run", "SC-VDE-005", "--transport", "stage7",
                            "--configs-dir", configs]) == 2


# ------------------------------------------------------------ 5·6: 사전 검증

def test_duplicate_run_id_rejected_before_any_spawn():
    spawn = _SpawnRecorder()
    with tempfile.TemporaryDirectory() as tmp:
        configs = _make_valid_configs(tmp, "dup-run")
        os.makedirs(os.path.join(tmp, "results", "runs", "dup-run"))
        try:
            stage7_run.run_stage7(
                scenario_id="SC-VDE-005", run_id="dup-run",
                configs_dir=configs,
                results_dir=os.path.join(tmp, "results"), spawn=spawn)
            raise AssertionError("중복 run-id가 거부되지 않았다")
        except stage7_run.Stage7RequestError as exc:
            assert "이미" in str(exc)
    assert spawn.calls == []  # 어떤 endpoint도 시작하기 전에 거부됐다


def test_missing_configs_dir_rejected():
    spawn = _SpawnRecorder()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            stage7_run.run_stage7(
                scenario_id="SC-VDE-005", run_id="r1",
                configs_dir=os.path.join(tmp, "없는-디렉터리"),
                results_dir=os.path.join(tmp, "results"), spawn=spawn)
            raise AssertionError("없는 configs-dir이 거부되지 않았다")
        except stage7_run.Stage7RequestError:
            pass
    assert spawn.calls == []


def test_metadata_run_id_mismatch_rejected():
    spawn = _SpawnRecorder()
    with tempfile.TemporaryDirectory() as tmp:
        configs = _make_valid_configs(tmp, "other-run")   # metadata는 other-run
        try:
            stage7_run.run_stage7(
                scenario_id="SC-VDE-005", run_id="requested-run",
                configs_dir=configs,
                results_dir=os.path.join(tmp, "results"), spawn=spawn)
            raise AssertionError("metadata run-id 불일치가 거부되지 않았다")
        except stage7_run.Stage7RequestError as exc:
            assert "run-id" in str(exc)
    assert spawn.calls == []


# ------------------------------------------------------------ 6: fallback 금지

def test_stage7_spawn_failure_is_explicit_fail_not_fallback():
    spawn = _SpawnRecorder(error=OSError("coordinator 실행 파일 없음"))
    with tempfile.TemporaryDirectory() as tmp:
        configs = _make_valid_configs(tmp, "fail-run")
        rc = stage7_run.run_stage7(
            scenario_id="SC-VDE-005", run_id="fail-run", configs_dir=configs,
            results_dir=os.path.join(tmp, "results"), spawn=spawn)
        assert rc == 1
        final_path = os.path.join(tmp, "results", "runs", "fail-run",
                                  "final-result.json")
        with open(final_path, "r", encoding="utf-8") as handle:
            final = json.load(handle)
        assert final["status"] == "FAIL"
        assert final["transport"] == "stage7"
        assert "spawn 실패" in final["error"]
        # run 구조(§8)가 준비됐다 — run.json/environment/config-hashes.
        run_dir = os.path.dirname(final_path)
        for name in ("run.json", "environment.json", "config-hashes.json"):
            assert os.path.isfile(os.path.join(run_dir, name)), name
    assert len(spawn.calls) == 1


def test_stage7_cli_failure_does_not_run_direct():
    """CLI 층에서도 stage7 실패가 direct 실행으로 새지 않는다."""
    original_run_stage7 = stage7_run.run_stage7
    original_report = runner._run_and_report
    calls = {"stage7": 0}

    def fake_run_stage7(**kwargs):
        calls["stage7"] += 1
        return 1  # 명시적 FAIL

    def forbidden_direct(*_a, **_k):
        raise AssertionError("stage7 실패가 direct 시나리오 실행으로 샜다")

    stage7_run.run_stage7 = fake_run_stage7
    runner._run_and_report = forbidden_direct
    try:
        rc = runner.main(["run", "SC-VDE-005", "--transport", "stage7",
                          "--run-id", "r", "--configs-dir", "c"])
    finally:
        stage7_run.run_stage7 = original_run_stage7
        runner._run_and_report = original_report
    assert rc == 1
    assert calls["stage7"] == 1


# ------------------------------------------------------------ 17: 실패 중 cleanup

class _HangingProcess:
    """종료 요청에 응하지 않는 coordinator를 흉내낸다."""

    def __init__(self):
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="coordinator", timeout=timeout or 0)

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_cleanup_on_coordinator_hang_preserves_evidence():
    process = _HangingProcess()
    with tempfile.TemporaryDirectory() as tmp:
        configs = _make_valid_configs(tmp, "hang-run")
        rc = stage7_run.run_stage7(
            scenario_id="SC-VDE-005", run_id="hang-run", configs_dir=configs,
            results_dir=os.path.join(tmp, "results"),
            spawn=lambda _cmd, **_k: process,
            timeout_s=0.01)
        assert rc == 1
        assert process.terminated, "coordinator terminate가 요청되지 않았다"
        assert process.killed, "bounded wait 초과 후 kill이 수행되지 않았다"
        final_path = os.path.join(tmp, "results", "runs", "hang-run",
                                  "final-result.json")
        with open(final_path, "r", encoding="utf-8") as handle:
            final = json.load(handle)
        # 원래 실패 사유와 cleanup 기록이 모두 보존된다.
        assert final["status"] == "FAIL"
        assert "초과" in final["error"]
        assert any("terminate" in item for item in final["cleanup"])
        assert any("kill" in item for item in final["cleanup"])


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: scenario_runner CLI 회귀")
