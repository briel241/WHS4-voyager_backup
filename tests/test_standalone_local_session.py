"""Phase I W2 — standalone 상태·로컬 세션 계층 계약 시험 (RF·SSH 없음).

증명하는 것 (apps/standalone/README.md §2·§3·§10)
  state    : 영속 디렉터리 보장, 자동 run-id 접미사 증가, 사용자 run-id
             중복 거부, flock 단일 인스턴스, atomic JSON 발행
  spawn    : 명시적 argv(list)·명시적 env(STAGE7_* 차단)·stdin 단절·
             shell 문자열 미사용
  IPC      : request 원자 발행(미완성본이 .json으로 보이지 않음),
             ready/result는 JSON 파싱 성공 기준으로만 읽음,
             trace는 발생 즉시 증분 전달
  종료     : shutdown→bounded 대기→필요 시에만 terminate→kill,
             closed.json·done.rc·잔존 0 미확인이면 성공으로 간주하지 않음
  이식성   : ssh·shell=True 미사용 (소스 계약 검사)

가짜 coordinator 하네스: 주입한 spawn이 FakeProcess를 돌려주고, 주입한
sleeper 훅이 파일 IPC 산출물을 시점 제어로 만들어낸다. 실제 대기 없음.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from apps.standalone import local_session, state


# ---------------------------------------------------------------- 하네스

class FakeProcess:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeTime:
    """가짜 시계 — sleep이 시간을 전진시키고 훅으로 하네스 행동을 만든다."""

    def __init__(self):
        self.now = 0.0
        self.sleep_count = 0
        self.on_sleep = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleep_count += 1
        for hook in list(self.on_sleep):
            hook()


def make_session(tmp, fake_time, *, spawn=None, residual_probe=lambda: [],
                 diagnostics=False, use_process_group=None, group_signal=None):
    run_root = Path(tmp) / "runs" / "run-x"
    run_root.mkdir(parents=True, exist_ok=True)
    return local_session.LocalCoordinatorSession(
        repo_root=_REPO, run_id="run-x", run_root=run_root,
        configs_dir=Path(tmp) / "cfg",
        gain_artifact_path=Path(tmp) / "gain.json",
        baseline_sha256="ab" * 32,
        spawn=spawn or (lambda *a, **k: FakeProcess()),
        clock=fake_time.clock, sleeper=fake_time.sleep,
        diagnostics=diagnostics, residual_probe=residual_probe,
        use_process_group=use_process_group, group_signal=group_signal)


def append_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.write(data)


# ---------------------------------------------------------------- state

def test_state_paths_ensure_var():
    with tempfile.TemporaryDirectory() as tmp:
        paths = state.StatePaths(tmp)
        paths.ensure_var()
        assert paths.runs_dir.is_dir()
        # 자격은 불변 release 디렉터리 + active 포인터 파일 구조 (W5 보완 2).
        assert paths.qualification_releases.is_dir()
        assert paths.qualification_candidate.is_dir()
        assert paths.active_pointer.name == "active.json"
        assert not paths.active_pointer.exists()  # 포인터는 승격이 만든다
        # identity는 setup 소관 — ensure_var가 만들지 않는다.
        assert not paths.identity_dir.exists()


def test_allocate_run_dir_auto_suffix_and_user_refusal():
    with tempfile.TemporaryDirectory() as tmp:
        runs = Path(tmp) / "runs"
        fixed = lambda: 1760000000.0  # noqa: E731 - 고정 시각
        rid1, root1 = state.allocate_run_dir(runs, clock=fixed)
        rid2, root2 = state.allocate_run_dir(runs, clock=fixed)
        assert rid1.startswith("run-") and root1.is_dir()
        assert rid2 == rid1 + "-2" and root2.is_dir()

        # 사용자 지정 run-id: 존재 시 거부 (접미사 우회 없음), 형식 검증.
        state.allocate_run_dir(runs, "my-run")
        try:
            state.allocate_run_dir(runs, "my-run")
            raise AssertionError("중복 run-id가 거부되지 않았다")
        except state.StateError as exc:
            assert "이미 존재" in str(exc)
        for bad in ("", "a/b", "a\\b", ".."):
            try:
                state.allocate_run_dir(runs, bad)
                raise AssertionError(f"잘못된 run-id 통과: {bad!r}")
            except state.StateError:
                pass


def test_single_instance_lock_exclusive_and_diagnostic_only():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "var" / "lock"
        first = state.SingleInstanceLock(lock_path)
        second = state.SingleInstanceLock(lock_path)
        first.acquire(diagnostics={"run_id": "run-x"})
        try:
            second.acquire()
            raise AssertionError("동시 lock 획득이 거부되지 않았다")
        except state.StateError:
            pass
        first.release()
        # 내용은 진단 정보일 뿐 — 판정은 flock이 했다. (두 번째 획득이
        # 내용을 덮어쓰기 전에 확인한다.)
        recorded = json.loads(lock_path.read_text(encoding="utf-8"))
        assert recorded.get("run_id") == "run-x"
        second.acquire()  # 해제 후에는 획득 가능 (stale lock 없음)
        second.release()


def test_write_json_atomic_leaves_no_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "a" / "b.json"
        state.write_json_atomic(target, {"k": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"k": 1}
        assert list(target.parent.glob("*.tmp")) == []


def test_check_disk_space_statuses():
    with tempfile.TemporaryDirectory() as tmp:
        free = state.check_disk_space(tmp)["free_bytes"]
        assert state.check_disk_space(
            tmp, warn_bytes=0, refuse_bytes=0)["status"] == "ok"
        assert state.check_disk_space(
            tmp, warn_bytes=free + 1, refuse_bytes=0)["status"] == "warn"
        assert state.check_disk_space(
            tmp, warn_bytes=free + 2, refuse_bytes=free + 1)["status"] == "refuse"


# ---------------------------------------------------------------- spawn 계약

def test_build_argv_env_and_spawn_contract():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        seen = {}

        def spawn(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return FakeProcess()

        os.environ["STAGE7_SVCDBG_ALLOW_RX_READY"] = "1"
        os.environ["PYTHONPATH"] = "/should/not/leak"
        os.environ["PYTHONHOME"] = "/should/not/leak"
        os.environ["VDETER_STANDALONE_IDENTITY_ROOT"] = "/stale/identity"
        try:
            session = make_session(tmp, ft, spawn=spawn)
            session.start()
        finally:
            for key in ("STAGE7_SVCDBG_ALLOW_RX_READY",
                        "PYTHONPATH", "PYTHONHOME",
                        "VDETER_STANDALONE_IDENTITY_ROOT"):
                os.environ.pop(key, None)
            session._close_console_handle()  # Windows tmp 정리용

        argv = seen["argv"]
        assert isinstance(argv, list)  # shell 문자열 금지 — 명시적 argv
        assert argv[1].endswith("stage7_coordinator_main.py")
        assert argv[argv.index("--phase") + 1] == "mass-roc-session"
        assert argv[argv.index("--baseline-sha256") + 1] == "ab" * 32
        kwargs = seen["kwargs"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.STDOUT
        assert Path(kwargs["cwd"]).name == "VDETER"
        env = kwargs["env"]
        # ambient 디버그 스위치는 전달되지 않는다 (정식 run 거부 예방).
        assert not any(key.startswith("STAGE7_") for key in env)
        # 자식 인터프리터 경로 오염 차단 (지시 3).
        assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
        assert "PYTHONPYCACHEPREFIX" in env
        # process group 사용 여부와 spawn 인자가 일치한다 (Windows 기본 off).
        assert ("start_new_session" in kwargs) == session._use_process_group
        assert session.launch_attempted

        # 부모의 낡은 identity 환경변수는 identity_root=None이면 자식에 없다
        # (fail-open 상속 차단 — W4 추가 보완).
        assert "VDETER_STANDALONE_IDENTITY_ROOT" not in env

        # diagnostics=True일 때만 STAGE7_DIAG=1.
        diag = make_session(tmp, ft, diagnostics=True)
        assert diag.build_env().get("STAGE7_DIAG") == "1"


def test_identity_root_env_injection_exact_and_isolated():
    """identity_root 명시 시에만, 정확한 경로만 자식 환경에 전달된다."""
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        os.environ["VDETER_STANDALONE_IDENTITY_ROOT"] = "/stale/identity"
        try:
            # 미지정 → 부모 값이 있어도 자식 env에 없다.
            plain = make_session(tmp, ft)
            assert "VDETER_STANDALONE_IDENTITY_ROOT" not in plain.build_env()

            # 지정 → 명시 값만 정확히 전달 (부모의 낡은 값이 아니라).
            wanted = Path(tmp) / "identity"
            session = local_session.LocalCoordinatorSession(
                repo_root=_REPO, run_id="run-y",
                run_root=Path(tmp) / "runs" / "run-y",
                configs_dir=Path(tmp) / "cfg",
                gain_artifact_path=Path(tmp) / "gain.json",
                baseline_sha256="ab" * 32,
                spawn=lambda *a, **k: FakeProcess(),
                clock=ft.clock, sleeper=ft.sleep,
                residual_probe=lambda: [],
                identity_root=wanted)
            env = session.build_env()
            assert env["VDETER_STANDALONE_IDENTITY_ROOT"] == str(wanted)
        finally:
            os.environ.pop("VDETER_STANDALONE_IDENTITY_ROOT", None)


# ---------------------------------------------------------------- 파일 IPC

def test_submit_workload_atomic_and_ids():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft)
        wid1 = session.submit_workload("SC-VDE-005")
        wid2 = session.submit_workload("SC-VDE-001", "forged")
        names = sorted(p.name for p in session.requests_dir.iterdir())
        assert names == sorted([wid1 + ".json", wid2 + ".json"])
        assert not any(name.endswith(".tmp") for name in names)
        assert "sc005-001" in wid1
        assert "sc001-forged-002" in wid2
        body = json.loads(
            (session.requests_dir / (wid2 + ".json")).read_text("utf-8"))
        assert body["scenario_id"] == "SC-VDE-001"
        assert body["certificate_case"] == "forged"


def test_wait_ready_parse_gate_stage_lines_and_exit_failure():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft)
        session.start()
        ready_path = session.session_dir / local_session.SESSION_READY
        stages = []

        def hook():
            if ft.sleep_count == 1:
                # 미완성 JSON — 파싱 실패라 ready로 취급되면 안 된다.
                ready_path.parent.mkdir(parents=True, exist_ok=True)
                ready_path.write_bytes(b'{"run_id": "run-x", ')
                append_bytes(session.console_log_path,
                             b"[coordinator] acquire_host_time COMPLETED\n")
            elif ft.sleep_count == 3:
                ready_path.write_text(json.dumps({"run_id": "run-x"}),
                                      encoding="utf-8")

        ft.on_sleep.append(hook)
        try:
            ready = session.wait_ready(timeout_s=60.0, on_stage=stages.append)
        finally:
            session._close_console_handle()  # Windows tmp 정리용
        assert ready == {"run_id": "run-x"}
        assert stages == ["acquire_host_time COMPLETED"]
        assert ft.sleep_count >= 3  # 미완성본에서 일찍 돌아오지 않았다

    # bring-up 중 coordinator 조기 종료는 rc와 함께 실패한다.
    with tempfile.TemporaryDirectory() as tmp:
        ft2 = FakeTime()
        session2 = make_session(tmp, ft2)
        session2.start()
        ft2.on_sleep.append(lambda: setattr(session2.process, "returncode", 3))
        try:
            session2.wait_ready(timeout_s=60.0)
            raise AssertionError("조기 종료가 실패로 보고되지 않았다")
        except local_session.LocalSessionError as exc:
            assert "rc=3" in str(exc)
        finally:
            session2._close_console_handle()


def test_wait_result_streams_trace_incrementally():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft)
        session.start()
        wid = session.submit_workload("SC-VDE-006")
        trace_path = session.workload_trace_path(wid)
        result_path = session.results_dir / (wid + ".json")
        rows = []

        def hook():
            if ft.sleep_count == 1:
                append_bytes(trace_path,
                             b'{"event": "submit", "n": 1}\n'
                             b'{"event": "data_re')  # 미완성 줄
            elif ft.sleep_count == 2:
                # 미완성 줄이 이 시점 전에 전달됐다면 계약 위반.
                assert [r["n"] for r in rows] == [1]
                append_bytes(trace_path, b'ceived", "n": 2}\n')
            elif ft.sleep_count == 3:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                state.write_json_atomic(result_path,
                                        {"workload_id": wid, "status": "PASS"})

        ft.on_sleep.append(hook)
        try:
            result = session.wait_result(wid, timeout_s=60.0,
                                         on_trace=rows.append)
        finally:
            session._close_console_handle()  # Windows tmp 정리용
        assert result["status"] == "PASS"
        assert [r["n"] for r in rows] == [1, 2]


# ---------------------------------------------------------------- 정착 계약

def test_settle_contract_preserved_from_host_layer():
    """SC-VDE-001 직후 30초 배수 대기(host 계약)가 standalone에도 산다."""
    # 상수 자체가 host 계층과 단일 출처다.
    assert local_session.WORKLOAD_SETTLE_SECONDS == 30.0
    assert local_session.SETTLE_AFTER_SCENARIOS == frozenset({"SC-VDE-001"})

    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft)
        session.start()
        wid = session.submit_workload("SC-VDE-001", "normal")
        result_path = session.results_dir / (wid + ".json")

        def hook():
            if ft.sleep_count == 1:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                state.write_json_atomic(result_path, {"status": "PASS"})

        ft.on_sleep.append(hook)
        try:
            session.wait_result(wid, timeout_s=60.0)
        finally:
            session._close_console_handle()
        assert session._last_scenario_id == "SC-VDE-001"

        finished_at = ft.now
        ft.now += 5.0  # 결과 후 5초 지난 시점에 다음 workload 요청
        settled = []
        session.submit_workload(
            "SC-VDE-005",
            on_settle=lambda remaining, prev: settled.append((remaining, prev)))
        assert len(settled) == 1
        remaining, prev = settled[0]
        assert prev == "SC-VDE-001"
        assert abs(remaining - 25.0) < 1e-6
        assert abs(ft.now - (finished_at + 5.0 + 25.0)) < 1e-6  # 실제로 잤다

        # 정착 대상이 아닌 시나리오(005) 뒤에는 대기 없음.
        session._last_scenario_id = "SC-VDE-005"
        session._last_workload_end = ft.now
        before = ft.now
        assert session.settle_before_next_workload() == 0.0
        assert ft.now == before

        # 30초가 이미 지났으면 대기 없음.
        session._last_scenario_id = "SC-VDE-001"
        session._last_workload_end = ft.now - 31.0
        assert session.settle_before_next_workload() == 0.0


# ---------------------------------------------------------------- 종료

def test_close_graceful_writes_done_rc_and_passes():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft, residual_probe=lambda: [])
        session.start()
        marker = session.session_dir / local_session.SESSION_SHUTDOWN

        def hook():
            if marker.exists() and session.process.returncode is None:
                state.write_json_atomic(
                    session.session_dir / local_session.SESSION_CLOSED,
                    {"status": "PASS", "cleanup": {"pass": True}})
                session.process.returncode = 0

        ft.on_sleep.append(hook)
        summary = session.close(close_timeout_s=60.0)
        assert summary["pass"] is True, summary["problems"]
        assert summary["escalation"] == []
        assert session.done_rc_path.read_text("utf-8").strip() == "0"
        assert not session.process.terminated


def test_close_escalates_and_reports_honest_failure():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft, residual_probe=lambda: ["stray-worker"])
        session.start()
        # shutdown 요청에 반응하지 않는 coordinator — kill에서만 죽는다
        # (FakeProcess.kill이 rc=-9 설정).
        summary = session.close(close_timeout_s=5.0)
        assert summary["pass"] is False
        assert session.process.terminated and session.process.killed
        assert summary["escalation"] == ["terminate", "kill"]
        text = " / ".join(summary["problems"])
        assert "closed.json 부재" in text          # cleanup 미확인 = 실패
        assert "비정상 종료" in text
        assert "잔존 프로세스 발견" in text
        assert session.done_rc_path.read_text("utf-8").strip() == "-9"


def test_escalation_targets_process_group_on_linux():
    """process group 모드: spawn은 setsid, 강제 신호는 group 전체 대상.

    Windows 개발 환경에서는 killpg가 없으므로 group_signal 주입으로
    Linux 경로의 논리를 검증한다. 정상 shutdown 마커 경로는 불변이다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        calls = []
        holder = {}

        def spawn(argv, **kwargs):
            holder["kwargs"] = kwargs
            holder["proc"] = FakeProcess()
            return holder["proc"]

        def group_signal(pid, name):
            calls.append((pid, name))
            if name == "KILL":
                holder["proc"].returncode = -9

        session = make_session(tmp, ft, spawn=spawn,
                               use_process_group=True,
                               group_signal=group_signal,
                               residual_probe=lambda: [])
        session.start()
        assert holder["kwargs"].get("start_new_session") is True
        summary = session.close(close_timeout_s=5.0)
        # TERM group → 무반응 → KILL group. 단일 프로세스 후퇴 없음.
        assert calls == [(4242, "TERM"), (4242, "KILL")]
        assert summary["escalation"] == ["term-group", "kill-group"]
        assert not holder["proc"].terminated and not holder["proc"].killed
        assert summary["pass"] is False  # closed.json 부재 — 정직 실패

        # group 신호 실패 시 단일 프로세스 신호로 후퇴하고 이력에 남긴다.
        ft2 = FakeTime()
        holder2 = {}

        def spawn2(argv, **kwargs):
            holder2["proc"] = FakeProcess()
            return holder2["proc"]

        def failing_group_signal(pid, name):
            raise OSError("no such process group")

        session2 = make_session(tmp, ft2, spawn=spawn2,
                                use_process_group=True,
                                group_signal=failing_group_signal,
                                residual_probe=lambda: [])
        session2.start()
        summary2 = session2.close(close_timeout_s=5.0)
        assert holder2["proc"].terminated and holder2["proc"].killed
        assert "terminate" in summary2["escalation"]
        assert any(item.startswith("term-group-failed")
                   for item in summary2["escalation"])


def test_finalize_fail_closed_when_probe_unavailable():
    with tempfile.TemporaryDirectory() as tmp:
        ft = FakeTime()
        session = make_session(tmp, ft, residual_probe=lambda: None)
        session.start()
        session.process.returncode = 0
        state.write_json_atomic(
            session.session_dir / local_session.SESSION_CLOSED,
            {"status": "PASS", "cleanup": {"pass": True}})
        summary = session.finalize()
        assert summary["pass"] is False
        assert any("확인 불가" in p for p in summary["problems"])


# ---------------------------------------------------------------- 이식성 계약

def test_standalone_sources_avoid_ssh_and_shell():
    base = Path(_REPO) / "apps" / "standalone"
    for path in sorted(base.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert '"ssh"' not in source and "'ssh'" not in source, path.name
        assert "shell=True" not in source, path.name


def test_ipc_contract_names_match_host_layer():
    assert local_session.SESSION_DIR_NAME == "session"
    assert local_session.SESSION_READY == "ready.json"
    assert local_session.SESSION_SHUTDOWN == "shutdown"
    assert local_session.SESSION_CLOSED == "closed.json"
    assert local_session.SESSION_REQUESTS == "requests"
    assert local_session.SESSION_RESULTS == "results"


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: Phase I W2 standalone state/local session 계약")
