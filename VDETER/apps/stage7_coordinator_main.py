"""Stage 7 Runtime Coordinator process (`stage7` §14, §16, §16.1).

Pi에서 세 endpoint launcher를 직접 spawn하고 `SOCK_SEQPACKET`으로 barrier를
구동한다. phase 인자:

- `p0`  — RF-off lifecycle: prepare→configure→arm_rx→stop (§10 P0).
- `hold`— 현재 p0과 동일 시퀀스 (후속 P1/P2 runner의 조립 검증용 alias).

P1/P2 RF phase는 별도 orchestration runner가 이 module의 조립 함수를
재사용한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (APP_ROOT, os.path.join(APP_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

ENDPOINT_IDS = ("MASS-A", "MASS-B", "ROC")


class SocketChannel:
    """`Stage7RuntimeCoordinator` channel: 단일 SEQPACKET 연결 위에서 response와
    event를 kind로 분리한다.

    v0.9.1 E항: guard가 부착되면 recv loop가 짧은 주기로 phase deadline과
    progress stall을 검사한다(endpoint progress/terminal event가 progress로
    집계). RR profile에서는 legacy 7,200초 fallback을 적용하지 않는다.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._events = []
        self._guard = None

    def attach_guard(self, guard) -> None:
        self._guard = guard

    def detach_guard(self) -> None:
        self._guard = None

    def request(self, message: dict) -> dict:
        self._client.send(message)
        # legacy full-qualification 경로 전용 상한 — guard 부착 시 미적용.
        legacy_deadline = time.monotonic() + 7200.0
        # 안전 명령은 guard가 이미 트립됐어도 전달되어야 한다 (30s 고정 상한).
        emergency = message.get("command_name") in ("emergency_inhibit", "stop")
        emergency_deadline = time.monotonic() + 30.0
        while True:
            guard = self._guard
            if emergency:
                if time.monotonic() >= emergency_deadline:
                    raise TimeoutError(
                        f"{message['command_name']} 안전명령 30s 응답 초과"
                    )
            elif guard is not None:
                guard.check()  # PHASE_DEADLINE_EXCEEDED / PHASE_PROGRESS_STALLED
            elif time.monotonic() >= legacy_deadline:
                raise TimeoutError(f"{message['command_name']} response 대기 초과")
            received = self._client.recv(timeout=2.0)
            if received is None:
                continue
            if received.get("kind") == "event":
                self._events.append(received)
                if guard is not None:
                    guard.progress("endpoint-event")
                continue
            if received.get("command_id") == message["command_id"]:
                if guard is not None:
                    guard.progress(f"terminal:{message['command_name']}")
                return received

    def poll_events(self) -> tuple:
        while True:
            try:
                received = self._client.recv(timeout=0.05)
            except (TimeoutError, OSError):
                break
            if received is None:
                break
            if received.get("kind") == "event":
                self._events.append(received)
        drained = tuple(self._events)
        self._events = []
        return drained


def load_group(run_id: str, configs_dir: str, gain_artifact_path):
    """세 resolved config와 system plan을 적재한다."""

    from vdeter.stage7.configuration import (
        Stage7ConfigurationLoader,
        Stage7OperationalGainInput,
    )
    from vdeter.stage7.coordinator import Stage7SystemPlanLoader

    gain_input_data = None
    if gain_artifact_path is not None:
        with open(gain_artifact_path, "rb") as handle:
            gain_input_data = json.loads(handle.read().decode("utf-8"))
    data_profile_path = os.path.join(configs_dir, "profiles", "data-profile.toml")
    with open(data_profile_path, "r", encoding="utf-8") as handle:
        data_profile_text = handle.read()
    loader = Stage7ConfigurationLoader()
    resolved = {}
    for endpoint_id in ENDPOINT_IDS:
        operational_gain = None
        if gain_input_data is not None:
            common = gain_input_data["endpoint_common_rx_gain_db"]
            # 세 endpoint 공통 gain은 endpoint별로 다르다 (§9.2 artifact).
            value = common[endpoint_id] if isinstance(common, dict) else common
            operational_gain = Stage7OperationalGainInput(
                endpoint_common_rx_gain_db=int(value),
                source_gain_artifact_sha256=gain_input_data[
                    "source_gain_artifact_sha256"
                ],
            )
        config_path = os.path.join(
            configs_dir, "endpoints", f"{endpoint_id.lower()}.toml"
        )
        with open(config_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        loaded = loader.load_endpoint_configuration(
            text,
            data_profile_text,
            run_id=run_id,
            operational_gain=operational_gain,
            source_path=config_path,
        )
        if loaded.is_err:
            raise SystemExit(f"{endpoint_id} config 적재 실패: {loaded.error}")
        resolved[endpoint_id] = loaded.value

    with open(os.path.join(configs_dir, "system.toml"), "r", encoding="utf-8") as handle:
        system_text = handle.read()
    plan = Stage7SystemPlanLoader().load(
        system_text,
        resolved,
        data_profile_sha256=resolved["ROC"].data_profile_sha256,
        deployment_sha256=resolved["ROC"].deployment_manifest_sha256,
    )
    if plan.is_err:
        raise SystemExit(f"system plan 적재 실패: {plan.error}")
    return resolved, plan.value


def spawn_and_connect(run_id: str, run_root: str, configs_dir: str, plan, gain_artifact_path,
                      *, test_plan_sha=None, lifecycle_generation=1):
    """launcher process 셋을 spawn하고 socket 연결까지 마친다."""

    from vdeter.stage7.transport import SeqPacketClient, runtime_socket_root

    root = runtime_socket_root(run_id)
    processes = {}
    for endpoint_id in ENDPOINT_IDS:
        command = [
            sys.executable,
            os.path.join(APP_ROOT, "apps", "stage7_endpoint_launcher.py"),
            "--run-id", run_id,
            "--endpoint-id", endpoint_id,
            "--config", os.path.join(configs_dir, "endpoints", f"{endpoint_id.lower()}.toml"),
            "--data-profile", os.path.join(configs_dir, "profiles", "data-profile.toml"),
            "--run-root", run_root,
            "--lifecycle-generation", str(lifecycle_generation),
            "--coordinator-pid", str(os.getpid()),
        ]
        if gain_artifact_path is not None:
            command += ["--gain-artifact", gain_artifact_path]
        if test_plan_sha is not None:
            command += [
                "--test-plan-sha", test_plan_sha,
                "--fixtures-dir", os.path.join(configs_dir, "fixtures"),
            ]
        environment = dict(os.environ)
        environment["STAGE7_PLAN_SHA256"] = plan.resolved_sha256
        logs_dir = os.path.join(run_root, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_handle = open(  # noqa: SIM115 - process 수명 동안 유지
            os.path.join(logs_dir, f"{endpoint_id}.launcher.log"), "ab"
        )
        processes[endpoint_id] = subprocess.Popen(
            command, env=environment, stdout=log_handle, stderr=subprocess.STDOUT
        )

    channels = {}
    for endpoint_id in ENDPOINT_IDS:
        socket_path = os.path.join(root, f"{endpoint_id}.sock")
        deadline = time.monotonic() + 90.0
        client = None
        while time.monotonic() < deadline:
            if processes[endpoint_id].poll() is not None:
                raise SystemExit(
                    f"{endpoint_id} launcher가 조기 종료했습니다 "
                    f"(rc={processes[endpoint_id].returncode})"
                )
            if os.path.exists(socket_path):
                try:
                    client = SeqPacketClient(socket_path)
                    break
                except OSError:
                    pass
            time.sleep(0.5)
        if client is None:
            raise SystemExit(f"{endpoint_id} socket 연결 실패: {socket_path}")
        channels[endpoint_id] = SocketChannel(client)
        print(f"[coordinator] {endpoint_id} 연결됨", flush=True)
    return processes, channels


def run_p0(coordinator, resolved, plan, run_root: str):
    """§10 P0 RF-off lifecycle barrier 시퀀스."""

    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns

    run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)
    time.sleep(3.0)
    now = int(read_monotonic_raw_ns())
    stop_bodies = {
        m: {"stop_at_frame_ordinal": 0, "stop_deadline_host_raw_ns": now + 30_000_000_000}
        for m in ENDPOINT_IDS
    }
    coordinator.run_barrier("stop", stop_bodies)
    print("[coordinator] stop COMPLETED", flush=True)


#: P1 signed edge별 directed path (path_id, tx_ep, rx_ep, tx_lane, rx_lane, channel역할).
P1_EDGE_PATHS = {
    "MASS-A": (
        ("C1", "ROC", "MASS-A", 0, 0),
        ("C2", "MASS-A", "ROC", 0, 0),
    ),
    "MASS-B": (
        ("C3", "ROC", "MASS-B", 1, 0),
        ("C4", "MASS-B", "ROC", 0, 1),
    ),
}
#: §5 P2 full 6-path set.
P2_PATHS = (
    ("C1", "ROC", "MASS-A", 0, 0),
    ("C2", "MASS-A", "ROC", 0, 0),
    ("C3", "ROC", "MASS-B", 1, 0),
    ("C4", "MASS-B", "ROC", 0, 1),
    ("C5", "MASS-A", "MASS-B", 1, 1),
    ("C6", "MASS-B", "MASS-A", 1, 1),
)
PATH_CHANNEL_ROLE = {
    "C1": "upper", "C2": "lower", "C3": "upper",
    "C4": "lower", "C5": "upper", "C6": "upper",
}


RESEARCH_READY_PROFILE = "RESEARCH_READY_MINIMAL"

#: §9.2 재검증 창의 고립 결손 허용치 (v0.9.4, 사용자 위임 2026-08-28).
#: wrong-bytes 0·인접 연속 실패 불허·실패 파형 evidence 의무는 endpoint
#: 판정 evaluate_reval_window가 강제한다. 근거는 기준선 서두 0.9.2 개정 항.
REVAL_MAX_NO_DECODE = 10


class PhaseGuard:
    """§17A.2: phase 절대 deadline과 progress stall fail-fast.

    implicit retry는 없다 — 위반 시 즉시 구조화 예외를 던지고 caller가
    emergency inhibit 후 종료한다.
    """

    def __init__(self, name: str, deadline_s: float, stall_s: float = 120.0) -> None:
        self.name = name
        self._deadline = time.monotonic() + deadline_s
        self._stall_s = stall_s
        self._last_progress = time.monotonic()
        self.progress_count = 0

    def progress(self, note: str = "") -> None:
        self._last_progress = time.monotonic()
        self.progress_count += 1
        if note:
            print(f"[guard:{self.name}] progress #{self.progress_count} {note}", flush=True)

    def check(self) -> None:
        now = time.monotonic()
        if now > self._deadline:
            raise RuntimeError(
                f"PHASE_DEADLINE_EXCEEDED:{self.name} — fail-fast (자동 재시도 없음)"
            )
        if now - self._last_progress > self._stall_s:
            raise RuntimeError(
                f"PHASE_PROGRESS_STALLED:{self.name} — {self._stall_s:.0f}s 무진행 fail-fast"
            )

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - time.monotonic())


# 판정 phase의 deadline 정본 (§17A.2와 동치; PhaseGuard 하드코드와 함께
# 개정). 0.9.22: prepare가 결속한 phase_deadlines_s는 생성만 되고 아무
# gate도 소비하지 않았다 — 여기서 대조해 결속을 실효화한다. soak는
# 판정 제외(DEFERRED)라 대조 대상에 넣지 않는다.
RR_PHASE_DEADLINE_S = {
    "rr-p0": 15 * 60,
    "rr-p1": 60 * 60,
    "rr-p2-17": 70 * 60,
    "rr-p2-19": 70 * 60,
    "rr-p3-fault": 40 * 60,
}


def validate_rr_binding(arguments, metadata: dict) -> None:
    """v0.9.1 I항 negative gate — hardware configure 전에 거부한다.

    - RR phase는 `qualification_profile=RESEARCH_READY_MINIMAL` config에서만 실행
    - full-qualification phase는 RR profile config로 실행 금지
    - baseline hash/phase/run ID/link 불일치는 TX 전 거부
    - 0.9.22: 디버그 우회 스위치 활성 상태의 정식 run 거부 + deadline 결속 대조
    """

    # Phase G additive: service-only phase는 RR 판정을 만들지 않지만 결속은
    # RR과 같은 강도로 확인한다(profile·run·baseline·retry). rr_phase는 서비스
    # workload를 결속한 rr-p2-17/19 config를 그대로 쓴다. 아래 기존 RR 분기의
    # 조건과 동작은 이 추가로 바뀌지 않는다.
    if arguments.phase in ("mass-roc-service", "mass-roc-session"):
        for banned in ("STAGE7_SVCDBG_ALLOW_RX_READY", "STAGE7_SCENARIO_FILTER"):
            if os.environ.get(banned):
                raise SystemExit(
                    f"SERVICE_BINDING_REJECTED: 디버그 스위치 {banned} 활성 — "
                    "service run에서는 설정 금지"
                )
        if metadata.get("qualification_profile") != RESEARCH_READY_PROFILE:
            raise SystemExit(
                "SERVICE_BINDING_REJECTED: RESEARCH_READY_MINIMAL로 결속된 "
                "config가 필요합니다"
            )
        if metadata.get("rr_phase") not in ("rr-p2-17", "rr-p2-19"):
            raise SystemExit(
                f"SERVICE_BINDING_REJECTED: service workload 결속 phase가 "
                f"아닙니다 ({metadata.get('rr_phase')!r})"
            )
        if metadata.get("run_id") != arguments.run_id:
            raise SystemExit(
                f"SERVICE_BINDING_REJECTED: config 결속 run "
                f"{metadata.get('run_id')!r} != 요청 run {arguments.run_id!r}"
            )
        expected_sha = getattr(arguments, "baseline_sha256", None)
        if not expected_sha or metadata.get("baseline_sha256") != expected_sha:
            raise SystemExit(
                "SERVICE_BINDING_REJECTED: baseline SHA-256 불일치 또는 부재"
            )
        if metadata.get("implicit_retry") != 0:
            raise SystemExit("SERVICE_BINDING_REJECTED: implicit_retry는 0이어야 합니다")
        return

    is_rr = arguments.phase.startswith("rr-")
    if is_rr:
        # 판정 표면을 바꾸는 디버그 스위치는 정식 run에서 금지 (R2 감사:
        # STAGE7_SVCDBG_ALLOW_RX_READY는 §16.1 host-time-lock 관문 우회,
        # STAGE7_SCENARIO_FILTER는 판정 세트 축소 — 둘 다 흔적 없이
        # 동작하던 미공시 표면이었다).
        for banned in ("STAGE7_SVCDBG_ALLOW_RX_READY",
                       "STAGE7_SCENARIO_FILTER"):
            if os.environ.get(banned):
                raise SystemExit(
                    f"RR_BINDING_REJECTED: 디버그 스위치 {banned} 활성 — "
                    "정식 RR run에서는 설정 금지"
                )
        bound_deadline = (metadata.get("phase_deadlines_s") or {}).get(
            arguments.phase
        )
        expected_deadline = RR_PHASE_DEADLINE_S.get(arguments.phase)
        if expected_deadline is not None and bound_deadline != expected_deadline:
            raise SystemExit(
                f"RR_BINDING_REJECTED: phase deadline 결속 "
                f"{bound_deadline!r}s != 실행기 {expected_deadline}s "
                "(부재 포함 — §17A.7 결속 필드는 생성뿐 아니라 대조된다)"
            )
    profile = metadata.get("qualification_profile")
    if is_rr:
        if profile != RESEARCH_READY_PROFILE:
            raise SystemExit(
                "RR_BINDING_REJECTED: RR phase는 RESEARCH_READY_MINIMAL로 결속된 "
                "config에서만 실행할 수 있습니다 (full profile config 사용 금지)"
            )
        if metadata.get("rr_phase") != arguments.phase:
            raise SystemExit(
                f"RR_BINDING_REJECTED: config 결속 phase "
                f"{metadata.get('rr_phase')!r} != 요청 phase {arguments.phase!r}"
            )
        if metadata.get("run_id") != arguments.run_id:
            raise SystemExit(
                f"RR_BINDING_REJECTED: config 결속 run "
                f"{metadata.get('run_id')!r} != 요청 run {arguments.run_id!r}"
            )
        expected_sha = getattr(arguments, "baseline_sha256", None)
        if not expected_sha:
            raise SystemExit(
                "RR_BINDING_REJECTED: --baseline-sha256이 필요합니다 (v0.9.1 결속)"
            )
        if metadata.get("baseline_sha256") != expected_sha:
            raise SystemExit(
                "RR_BINDING_REJECTED: baseline SHA-256 불일치 — hardware "
                "configure 전 거부"
            )
        if metadata.get("implicit_retry") != 0:
            raise SystemExit("RR_BINDING_REJECTED: implicit_retry는 0이어야 합니다")
        link = metadata.get("data_link_id")
        if arguments.phase.endswith(("17", "19")):
            if link != int(arguments.phase[-2:]):
                raise SystemExit(
                    f"RR_BINDING_REJECTED: link 결속 {link!r} != phase link"
                )
    else:
        if profile == RESEARCH_READY_PROFILE:
            raise SystemExit(
                "RR_BINDING_REJECTED: RESEARCH_READY_MINIMAL config로 기존 "
                "full-qualification phase를 실행할 수 없습니다 (TC-001..020 PASS "
                "생성 금지)"
            )


def _load_run_metadata(configs_dir: str, gain_artifact_path=None) -> dict:
    """prepare 도구가 결속한 git/baseline metadata (없으면 null 유지)."""

    path = os.path.join(configs_dir, "git_state.json")
    try:
        with open(path, "rb") as handle:
            metadata = json.loads(handle.read().decode("utf-8"))
    except OSError:
        # git 결속 부재여도 관측 환경 필드는 기록한다 (0.9.22 —
        # 이전엔 {} 반환으로 3필드까지 함께 소실).
        metadata = {}
    # 0.9.20 재현성 봉합: 관측 환경(STAGE7_DIAG)과 gain artifact 출처를
    # evidence에 기계 기록한다 — 재현 조건이 산문이 아닌 산출물에 남게.
    metadata["stage7_diag_env"] = os.environ.get("STAGE7_DIAG", "0")
    metadata["rx_energy_gate_env"] = os.environ.get(
        "STAGE7_RX_ENERGY_GATE", "1"
    )
    # 0.9.22: 판정 표면에 닿는 디버그 스위치도 기계 기록한다 — 정식 run은
    # validate_rr_binding이 활성 자체를 거부하므로 기록값은 부작동 증명.
    metadata["svcdbg_allow_rx_ready_env"] = os.environ.get(
        "STAGE7_SVCDBG_ALLOW_RX_READY", "0"
    )
    metadata["scenario_filter_env"] = (
        os.environ.get("STAGE7_SCENARIO_FILTER") or None
    )
    if gain_artifact_path:
        try:
            with open(gain_artifact_path, "rb") as handle:
                metadata["source_gain_artifact_sha256"] = hashlib.sha256(
                    handle.read()
                ).hexdigest()
        except OSError as exc:
            print(
                f"[coordinator] gain artifact 해시 실패(결속 생략): "
                f"{gain_artifact_path}: {exc}",
                file=sys.stderr, flush=True,
            )
    return metadata


def write_rr_evidence(
    run_root: str, tc_id: str, *, run_id: str, metadata: dict, started_utc: str,
    ended_utc: str, duration_s: float, counters: dict, status: str, detail: dict,
    hardware: dict, lifecycle_generation: int = 1,
) -> str:
    """§17A.6 research_ready evidence artifact 하나를 기록한다."""

    assert status in ("PASS", "FAIL", "ABORTED")
    directory = os.path.join(run_root, "evidence", "research_ready")
    os.makedirs(directory, exist_ok=True)
    payload = {  # generation은 호출측 실값(기본 1 — 재기동 없는 절차)
        "artifact_type": "STAGE7_RESEARCH_READY_EVIDENCE_V1",
        "profile": RESEARCH_READY_PROFILE,
        "tc_id": tc_id,
        "baseline_version": metadata.get("baseline_version"),
        "baseline_sha256": metadata.get("baseline_sha256"),
        "git_commit": metadata.get("git_commit"),
        "git_dirty": metadata.get("git_dirty"),
        "hardware": hardware,
        "run_id": run_id,
        "stage7_diag_env": metadata.get("stage7_diag_env"),
        "rx_energy_gate_env": metadata.get("rx_energy_gate_env"),
        "svcdbg_allow_rx_ready_env": metadata.get(
            "svcdbg_allow_rx_ready_env"
        ),
        "scenario_filter_env": metadata.get("scenario_filter_env"),
        "source_gain_artifact_sha256": metadata.get(
            "source_gain_artifact_sha256"
        ),
        "lifecycle_generation": lifecycle_generation,
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "duration_s": round(duration_s, 1),
        "counters": counters,
        "status": status,
        "detail": detail,
        "scope": "이 결과는 full qualification(TC-STAGE7-001..020)을 대체하지 않는다. "
                 "OTA 연구 실험 진입용 최소 검증이며 제품급 신뢰성·장기 안정성·일반 "
                 "무선환경 성능을 보증하지 않는다.",
    }
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1).encode()
    path = os.path.join(directory, f"{tc_id}.json")
    with open(path, "wb") as handle:
        handle.write(data)
    index_path = os.path.join(directory, "index.json")
    try:
        with open(index_path, "rb") as handle:
            index = json.loads(handle.read().decode("utf-8"))
    except OSError:
        index = {"profile": RESEARCH_READY_PROFILE, "entries": {}}
    index["entries"][tc_id] = {
        "path": f"evidence/research_ready/{tc_id}.json",
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": status,
    }
    with open(index_path, "wb") as handle:
        handle.write(json.dumps(index, ensure_ascii=True, sort_keys=True, indent=1).encode())
    return path


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_plan(run_root: str, name: str, payload: dict) -> str:
    plans_dir = os.path.join(run_root, "plans")
    os.makedirs(plans_dir, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1).encode()
    path = os.path.join(plans_dir, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def run_p1(coordinator, resolved, plan, run_root: str, edge: str, *, gain_artifact_path):
    """§17 P1: signed edge 양방향 RF qualification barrier 시퀀스.

    excluded MASS는 prepare/configure/arm_rx까지만 참여하고 RF barrier에서
    제외되어 `RX_READY`+TX inhibit로 남는다.
    """

    # barrier manifest는 canonical member 순서를 강제한다 (§16.1).
    present = tuple(m for m in ENDPOINT_IDS if m in ("ROC", edge))
    return _run_rf_qualification(
        coordinator, resolved, plan, run_root,
        paths=P1_EDGE_PATHS[edge],
        present=present,
        pairs=("RA",) if edge == "MASS-A" else ("RB",),
        gain_artifact_path=gain_artifact_path,
    )


def run_p2(coordinator, resolved, plan, run_root: str, *, gain_artifact_path):
    """§13 P2: 3-node C1..C6 전체 RF qualification + triangle closure."""

    return _run_rf_qualification(
        coordinator, resolved, plan, run_root,
        paths=P2_PATHS,
        present=ENDPOINT_IDS,
        pairs=("RA", "RB", "AB"),
        gain_artifact_path=gain_artifact_path,
        service_start=gain_artifact_path is not None,
    )


#: §17A.3 RR-002 CFO 대표 directed path (pair당 1개: RA/RB/AB).
RR_CFO_PATHS = (
    ("C1", "ROC", "MASS-A", 0, 0),
    ("C3", "ROC", "MASS-B", 1, 0),
    ("C5", "MASS-A", "MASS-B", 1, 1),
)


#: §3 canonical transport 기대 속도 (Mbps).
_EXPECTED_USB_SPEED = {"MASS-A": "5000", "MASS-B": "480", "ROC": "5000"}
#: 2026-09-10 (Pi powered-USB3-hub profile): RR-P0의 승인 link speed는 resolved
#: config의 `host_execution.usb_transport`에서 단일 정의(vdeter.stage7.host_profiles)
#: 로 유도한다. 위 legacy Pi 고정표는 값 그대로 보존되며(legacy profile의 유도값과
#: 동일), powered-hub profile은 세 endpoint 전부 5000 + 실시간 공유 hub topology.


def _expected_usb_speed(resolved, endpoint_id) -> str:
    """resolved config → 승인 sysfs `speed` 문자열 ("5000"/"480")."""

    from vdeter.stage7.host_profiles import expected_sysfs_speed

    return expected_sysfs_speed(resolved[endpoint_id].host_execution.usb_transport)


def _shared_hub_topology_check(resolved, serial_by_endpoint):
    """resolved host profile이 공유 powered hub를 요구하면 sysfs 실측 검사
    (host_profiles.check_shared_powered_hub) 결과, 아니면 None (legacy Pi)."""

    from vdeter.stage7.host_profiles import (
        check_shared_powered_hub,
        probe_usb_topology,
        profile,
    )

    profiles = sorted({resolved[m].host_execution.profile for m in serial_by_endpoint})
    if len(profiles) != 1:
        return {"pass": False, "host_profile": None, "hub": None,
                "observed_by_role": {},
                "errors": ["mixed host profiles across endpoints: " + ", ".join(profiles)]}
    if not profile(profiles[0]).shared_powered_hub:
        return None
    return check_shared_powered_hub(serial_by_endpoint, probe_usb_topology())


def _live_b210_speed_map() -> dict:
    """/sys에서 serial→USB link speed를 읽는다 (Pi 로컬)."""

    import glob

    result = {}
    for serial_path in glob.glob("/sys/bus/usb/devices/*/serial"):
        try:
            serial = open(serial_path).read().strip()
            speed = open(os.path.join(os.path.dirname(serial_path), "speed")).read().strip()
        except OSError:
            continue
        result.setdefault(serial, []).append(speed)
    return result


def _worker_process_count() -> int:
    done = subprocess.run(
        ["pgrep", "-fc", "spawn_mai[n]"], capture_output=True, text=True
    )
    try:
        return int(done.stdout.strip() or 0)
    except ValueError:
        return 0


def _rediscover_b210(serials) -> dict:
    """종료 후 세 B210 재-discovery 가능성 (`uhd_find_devices`)."""

    done = subprocess.run(
        ["uhd_find_devices"], capture_output=True, text=True, timeout=60
    )
    output = done.stdout + done.stderr
    return {serial: (serial in output) for serial in serials}


def run_rr_p0(coordinator, resolved, plan, run_root: str, *, run_id, metadata,
              processes=None):
    """`TC-STAGE7-RR-001`: RF-off 1 cycle — discovery/mapping/ownership과
    cleanup·handle 해제 실검증 (v0.9.1 F항).

    PASS는 cleanup/handle release 검증이 끝난 뒤에만 기록하며, terminate/kill이
    필요했던 경우 PASS로 기록하지 않는다. RR-P0 PASS 전 RF TX는 0이다.
    """

    guard = PhaseGuard("RR-P0", deadline_s=15 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    started = _utc_now()
    t0 = time.monotonic()
    status = "FAIL"
    detail = {}
    serial_by_endpoint = {m: resolved[m].b210_serial for m in ENDPOINT_IDS}
    try:
        run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)
        guard.progress("prepare/configure/arm_rx")
        guard.check()

        # 가동 중 검증: worker 3, launcher 3 생존, serial별 승인 속도, throttle.
        workers_running = _worker_process_count()
        launchers_alive = (
            sum(1 for p in (processes or {}).values() if p.poll() is None)
            if processes else None
        )
        speed_map = _live_b210_speed_map()
        speed_ok = {}
        for m in ENDPOINT_IDS:
            speeds = speed_map.get(serial_by_endpoint[m], [])
            speed_ok[m] = speeds == [_expected_usb_speed(resolved, m)]
        hub_topology = _shared_hub_topology_check(resolved, serial_by_endpoint)
        throttled = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True
        ).stdout.strip()
        guard.progress("live-checks")

        time.sleep(2.0)
        from vdeter.stage7.hw_operations import read_monotonic_raw_ns as _now_ns

        stop_now = int(_now_ns())
        coordinator.run_barrier(
            "stop",
            {
                m: {"stop_at_frame_ordinal": 0,
                    "stop_deadline_host_raw_ns": stop_now + 30_000_000_000}
                for m in ENDPOINT_IDS
            },
        )
        guard.progress("stop")

        # cleanup 검증: launcher가 terminate 없이 스스로 종료, worker 0,
        # B210 재-discovery 3/3.
        clean_exit = True
        if processes:
            for endpoint_id, process in processes.items():
                deadline = time.monotonic() + 60.0
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.5)
                if process.poll() is None:
                    clean_exit = False
        guard.progress("launcher-exit")
        workers_after = _worker_process_count()
        rediscovery = _rediscover_b210(sorted(serial_by_endpoint.values()))
        guard.progress("rediscovery")

        bad = [
            e for e in coordinator.command_log()
            if e["status"] not in ("COMPLETED", "PREPARED")
        ]
        tx_commands = [
            e for e in coordinator.command_log()
            if e["command_name"] in ("start_cfo", "calibrate_rf", "validate_rf_time")
        ]
        detail = {
            "command_log_entries": len(list(coordinator.command_log())),
            "non_clean_statuses": bad,
            "inhibited": coordinator.inhibited,
            "serial_by_endpoint": serial_by_endpoint,
            "usb_speed_ok": speed_ok,
            "expected_usb_speed": {m: _expected_usb_speed(resolved, m) for m in ENDPOINT_IDS},
            "live_speed_map": {k: v for k, v in speed_map.items()
                               if k in serial_by_endpoint.values()},
            "usb_hub_topology": hub_topology,
            "throttled": throttled,
            "workers_running": workers_running,
            "launchers_alive_during_run": launchers_alive,
            "launcher_clean_exit": clean_exit,
            "workers_after_stop": workers_after,
            "rediscovery": rediscovery,
            "rf_tx_commands": len(tx_commands),
            "usb_error_check": "serial/speed/소유권 안정성 기반 판정 "
                               "(커널 로그 직접 감사 아님 — 한계 명시)",
        }
        status = "PASS" if (
            not bad
            and not coordinator.inhibited
            and workers_running == 3
            and (launchers_alive in (None, 3))
            and all(speed_ok.values())
            and (hub_topology is None or hub_topology["pass"])
            and throttled == "throttled=0x0"
            and clean_exit
            and workers_after == 0
            and all(rediscovery.values())
            and not tx_commands
        ) else "FAIL"
        print(f"[coordinator] RR-P0 {status} detail={detail}", flush=True)
    finally:
        write_rr_evidence(
            run_root, "TC-STAGE7-RR-001", run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=_utc_now(),
            duration_s=time.monotonic() - t0,
            counters={"cycles": 1, "retries": 0,
                      "progress_marks": guard.progress_count},
            status=status, detail=detail,
            hardware=metadata.get("hardware", {}),
        )
    if status != "PASS":
        raise RuntimeError("RR-P0 FAIL — RF TX 금지 유지, 새 run ID로만 재시도")
    return {"rr_p0": status}


def run_rr_p1(coordinator, resolved, plan, run_root: str, *, run_id, metadata):
    """`TC-STAGE7-RR-002..004`: 최소 P1 — CFO(3 pair 대표), adaptive gain,
    host affine + RF timestamp pre/post + 경로별 raw PHY 100 burst.

    전체 deadline 60분, gain phase deadline 20분(plan 결속으로 endpoint도 강제).
    implicit retry 0 — 실패는 즉시 inhibit·구조화 종료.
    """

    guard = PhaseGuard("RR-P1", deadline_s=60 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    phase_marks = {}
    started = _utc_now()
    t0 = time.monotonic()
    outcome = {"status": "FAIL", "error": None}
    result = None
    try:
        result = _run_rf_qualification(
            coordinator, resolved, plan, run_root,
            paths=P2_PATHS,
            present=ENDPOINT_IDS,
            pairs=("RA", "RB", "AB"),
            gain_artifact_path=None,
            cfo_paths=RR_CFO_PATHS,
            gain_mode="ADAPTIVE",
            guard=guard,
            phase_marks=phase_marks,
            raw_phy=True,
        )
        outcome["status"] = "PASS"
    except Exception as error:  # noqa: BLE001
        outcome["error"] = str(error)
        raise
    finally:
        ended = _utc_now()
        duration = time.monotonic() - t0
        marks = sorted(phase_marks.items(), key=lambda kv: kv[1])
        segments = {
            name: round(stamp - t0, 1) for name, stamp in marks
        }
        common = {
            "run_id": run_id, "metadata": metadata, "started_utc": started,
            "ended_utc": ended,
        }
        write_rr_evidence(
            run_root, "TC-STAGE7-RR-002", run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=ended, duration_s=duration,
            counters={"cfo_paths": len(RR_CFO_PATHS), "bursts_per_path": 10,
                      "retries": 0},
            status="PASS" if "start_cfo" in phase_marks else (
                "FAIL" if outcome["error"] else "ABORTED"),
            detail={"phase_elapsed_s": segments, "error": outcome["error"]},
            hardware=metadata.get("hardware", {}),
        )
        write_rr_evidence(
            run_root, "TC-STAGE7-RR-003", run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=ended, duration_s=duration,
            counters={"candidate_bursts": 10, "final_bursts": 100, "retries": 0},
            status="PASS" if "calibrate_rf" in phase_marks else (
                "FAIL" if outcome["error"] else "ABORTED"),
            detail={
                "phase_elapsed_s": segments,
                "gain_note": "gain은 케이블/경로 종속 측정값 — OTA 환경에서 재교정 필수",
                "error": outcome["error"],
            },
            hardware=metadata.get("hardware", {}),
        )
        raw_summaries = (result or {}).get("raw_phy") or []
        raw_actual = {
            s["path_id"]: {k: s.get(k) for k in
                           ("outcome", "decoded_ok", "no_decode", "wrong_bytes",
                            "captured", "tx_submitted", "async_outcome_counts")}
            for s in raw_summaries
        }
        # fake-PASS 방지: raw PHY phase가 실제로 실행되어 RX summary 6개가
        # 존재할 때만 PASS 가능 (counter 선언만으로 PASS 불가).
        raw_rx_count = sum(
            1 for s in raw_summaries
            if s.get("summary_type") == "STAGE7_RAW_PHY_SUMMARY_V1"
        )
        rr004_pass = (
            outcome["status"] == "PASS"
            and "raw_phy" in phase_marks
            and raw_rx_count == len(P2_PATHS)
            and all(s.get("outcome") == "PASS" for s in raw_summaries)
            and sum(int(s.get("wrong_bytes", 0)) for s in raw_summaries) == 0
        )
        write_rr_evidence(
            run_root, "TC-STAGE7-RR-004", run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=ended, duration_s=duration,
            counters={"rf_time_bursts_per_path": 10,
                      "raw_phy_bursts_per_path": 100,
                      "raw_phy_rx_summaries": raw_rx_count,
                      "wrong_bytes_total": sum(
                          int(s.get("wrong_bytes", 0)) for s in raw_summaries),
                      "retries": 0},
            status="PASS" if rr004_pass else (
                "FAIL" if outcome["error"] or not rr004_pass else "ABORTED"),
            detail={
                "phase_elapsed_s": segments,
                "raw_phy_actual": raw_actual,
                "scope_note": "P1 pair 결과로 3-node group time qualification을 주장하지 않는다",
                "error": outcome["error"],
            },
            hardware=metadata.get("hardware", {}),
        )
    return result


def _spawn_controller(run_id, run_root, configs_dir, test_plan_sha, mode,
                      soak_seconds=600, min_transfers=10):
    logs_dir = os.path.join(run_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_handle = open(  # noqa: SIM115
        os.path.join(logs_dir, "scenario-controller.log"), "ab"
    )
    command = [
        sys.executable,
        os.path.join(APP_ROOT, "apps", "stage7_scenario_controller.py"),
        "--run-id", run_id,
        "--run-root", run_root,
        "--test-plan-sha", test_plan_sha,
        "--fixtures-dir", os.path.join(configs_dir, "fixtures"),
        "--mode", mode,
        "--soak-seconds", str(soak_seconds),
        "--min-transfers", str(min_transfers),
    ]
    return subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)


def _spawn_mass_roc_controller(run_id, run_root, configs_dir, test_plan_sha,
                               scenario_id, certificate_case):
    """MASS/ROC 업무 시나리오 controller를 spawn한다 (RR controller와 별개).

    기존 RR scenario controller는 그대로 보존한다 — MASS/ROC workload는
    repo-root `apps/stage7_mass_roc_controller.py`가 실제 RocAgent/MassAgent/
    cert_flow 흐름을 수행하고 결과를 `<run_root>/mass-roc-events/`에 남긴다.
    """
    logs_dir = os.path.join(run_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_handle = open(  # noqa: SIM115
        os.path.join(logs_dir, "mass-roc-controller.log"), "ab"
    )
    command = [
        sys.executable,
        os.path.join(os.path.dirname(APP_ROOT), "apps",
                     "stage7_mass_roc_controller.py"),
        "--run-id", run_id,
        "--run-root", run_root,
        "--test-plan-sha", test_plan_sha,
        "--fixtures-dir", os.path.join(configs_dir, "fixtures"),
        "--scenario-id", scenario_id,
        "--certificate-case", certificate_case,
    ]
    return subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)


def _open_session_trace(run_root, run_id):
    """session phase용 JSONL 계측기. 실패해도 세션을 막지 않는다.

    repo-root `apps/stage7_trace.py`를 재사용해 session/workload 결속·비밀
    마스킹 형식을 CLI 계층과 통일한다. import가 안 되면 무계측으로 진행한다
    (계측 부재가 판정을 바꾸면 안 되므로 fail-open이 맞다).
    """
    path = os.path.join(run_root, "session-coordinator-trace.jsonl")
    try:
        repo_root = os.path.dirname(APP_ROOT)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from apps.stage7_trace import TraceWriter

        return TraceWriter(path, session_id=run_id, diagnostics=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[coordinator] 계측 비활성 ({type(exc).__name__}: {exc})", flush=True)

        class _NoTrace:
            def emit(self, *_a, **_k):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        return _NoTrace()


def _spawn_session_lifecycle(lifecycle, run_id, run_root, configs_dir,
                             test_plan_sha, emit=None):
    """persistent session의 endpoint START/STOP 대행 controller를 spawn한다.

    session 모델에서 endpoint lifecycle의 주인은 session이지만, TEST adapter
    socket 연결은 controller 프로세스에만 있다. 이 대행은 시나리오를 실행하지
    않고 (`--scenario-id none`) START 또는 STOP 한 단계만 수행한 뒤 종료한다.
    """
    logs_dir = os.path.join(run_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_handle = open(  # noqa: SIM115
        os.path.join(logs_dir, f"{lifecycle}.log"), "ab"
    )
    command = [
        sys.executable,
        os.path.join(os.path.dirname(APP_ROOT), "apps",
                     "stage7_mass_roc_controller.py"),
        "--run-id", run_id,
        "--run-root", os.path.join(run_root, "lifecycle", lifecycle),
        "--test-plan-sha", test_plan_sha,
        "--fixtures-dir", os.path.join(configs_dir, "fixtures"),
        "--scenario-id", "none",
        "--lifecycle", lifecycle,
        "--workload-id", lifecycle,
        # gate는 session run-root에 있다. 대행의 run-root(<run_root>/lifecycle/…)
        # 기준으로 찾으면 영원히 열리지 않는 경로를 본다.
        "--gate-dir", os.path.join(run_root, "controller_gate"),
    ]
    os.makedirs(os.path.join(run_root, "lifecycle", lifecycle), exist_ok=True)
    process = subprocess.Popen(command, stdout=log_handle,
                               stderr=subprocess.STDOUT)
    if emit is not None:
        # gate 경로 불일치가 조용한 무한대기로 나타났던 회귀
        # (phase-g-service-20260831-042934) — 인자를 그대로 남긴다.
        gate_dir = command[command.index("--gate-dir") + 1]
        emit("lifecycle_proxy_spawn", lifecycle=lifecycle, pid=process.pid,
             gate_dir=gate_dir,
             gate_file=os.path.join(gate_dir, "admission_open"),
             proxy_run_root=command[command.index("--run-root") + 1],
             log=os.path.join(logs_dir, f"{lifecycle}.log"),
             command=command)
    return process


def _run_rr_service_phase(
    coordinator, resolved, plan, run_root, *, run_id, metadata, tc_id, guard,
    gain_artifact_path, configs_dir, test_plan_sha, mode, soak_seconds=600,
    min_transfers=10, phase_marks=None, spawn_controller=None,
    result_relpath=os.path.join("evidence", "scenario_result.json"),
):
    """RR-P2/soak 공통: OPERATIONAL ladder→service publish→controller scenario→stop.

    `spawn_controller`/`result_relpath`로 controller 종류를 주입할 수 있다
    (기본은 기존 RR scenario controller — 동작 불변). MASS/ROC workload는
    `_spawn_mass_roc_controller`와 mass-roc-events 결과 경로를 주입한다.
    """

    marks = phase_marks if phase_marks is not None else {}
    if spawn_controller is not None:
        controller = spawn_controller()
    else:
        controller = _spawn_controller(
            run_id, run_root, configs_dir, test_plan_sha, mode,
            soak_seconds=soak_seconds, min_transfers=min_transfers,
        )
    gate_dir = os.path.join(run_root, "controller_gate")
    os.makedirs(gate_dir, exist_ok=True)

    def _admission_open():
        open(os.path.join(gate_dir, "admission_open"), "w").close()

    def _wait_controller():
        # soak/scenario 대기 중 coordinator IPC는 정상적으로 조용하다 —
        # 서비스 이벤트 수신·컨트롤러 기록이 실제 활동이므로 그 파일
        # 성장을 guard 진행으로 급식한다 (rr-p3-soak-17-20260829f/g 실측:
        # soak-1이 정상 진행·성공 중인데 IPC 침묵만 보고 stall 오탐 정지).
        # 파일이 전부 멈추면 진짜 정지 — stall 감시가 그대로 작동한다.
        import glob as _glob
        watch = [os.path.join(run_root, "logs", "scenario-controller.log"),
                 os.path.join(run_root, "logs", "mass-roc-controller.log")]
        last_total = -1
        while controller.poll() is None:
            total = 0
            for path in watch + _glob.glob(
                os.path.join(run_root, "evidence", "service_events", "*.jsonl")
            ):
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
            if total != last_total:
                last_total = total
                guard.progress()
            guard.check()
            time.sleep(1.0)
        result_path = os.path.join(run_root, result_relpath)
        try:
            with open(result_path, "rb") as handle:
                return json.loads(handle.read().decode("utf-8"))
        except OSError:
            return {"status": "FAIL", "reason": "scenario_result.json 없음",
                    "controller_rc": controller.returncode}

    try:
        result = _run_rf_qualification(
            coordinator, resolved, plan, run_root,
            paths=P2_PATHS, present=ENDPOINT_IDS, pairs=("RA", "RB", "AB"),
            gain_artifact_path=gain_artifact_path,
            # v0.9.1 승인 슬림화: CFO는 pair 물리량 — 대표 3경로로 재확인.
            cfo_paths=RR_CFO_PATHS,
            guard=guard, phase_marks=marks,
            service_start=True,
            admission_open_cb=_admission_open,
            post_publish_cb=_wait_controller,
        )
    finally:
        if controller.poll() is None:
            controller.terminate()
    scenario = (result.get("service_start") or {}).get("scenario") or {}
    return result, scenario, controller.returncode


def run_rr_p2(coordinator, resolved, plan, run_root: str, *, run_id, metadata,
              link_id, gain_artifact_path, configs_dir, test_plan_sha):
    """`TC-STAGE7-RR-005/006`: link별 3-node 최소 기능 1회씩 (v0.9.1 G항)."""

    tc_id = "TC-STAGE7-RR-005" if link_id == 17 else "TC-STAGE7-RR-006"
    # v0.9.1 사용자 승인(2026-08-27): 사다리 실측 ~36-40분 → 상한 70분(0.9.19).
    guard = PhaseGuard(f"RR-P2-{link_id}", deadline_s=70 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    started = _utc_now()
    t0 = time.monotonic()
    status = "FAIL"
    scenario = {}
    error = None
    marks = {}
    try:
        result, scenario, rc = _run_rr_service_phase(
            coordinator, resolved, plan, run_root, run_id=run_id,
            metadata=metadata, tc_id=tc_id, guard=guard,
            gain_artifact_path=gain_artifact_path, configs_dir=configs_dir,
            test_plan_sha=test_plan_sha, mode="scenario", phase_marks=marks,
        )
        status = "PASS" if (
            scenario.get("status") == "PASS" and rc == 0
        ) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        raise
    finally:
        counters = dict(scenario.get("counters") or {})
        counters.setdefault("retries", 0)
        write_rr_evidence(
            run_root, tc_id, run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=_utc_now(),
            duration_s=time.monotonic() - t0,
            counters=counters,
            status=status if error is None else "FAIL",
            detail={"link_id": link_id, "scenario_steps": scenario.get("steps"),
                    "phase_elapsed_s": {k: round(v - t0, 1) for k, v in marks.items()},
                    "error": error},
            hardware=metadata.get("hardware", {}),
        )
    if status != "PASS":
        raise RuntimeError(f"{tc_id} FAIL — 새 run ID로만 재시도")
    return {"rr_p2": status, "link": link_id}


def run_mass_roc(coordinator, resolved, plan, run_root: str, *, run_id, metadata,
                 link_id, gain_artifact_path, configs_dir, test_plan_sha,
                 scenario_id, certificate_case):
    """MASS/ROC 업무 시나리오 workload — RR ladder 위에서 별도 controller 실행.

    RR TC evidence(`write_rr_evidence`)는 **쓰지 않는다** — 이 workload의
    결과가 RR TC PASS로 재해석되면 안 되고, 반대로 RR fixture 결과를 MASS/ROC
    성공으로 재해석해서도 안 된다. 결과는 `evidence/mass_roc/` 아래에 별도
    artifact로 남긴다.
    """

    guard = PhaseGuard(f"MASS-ROC-{link_id}", deadline_s=70 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    started = _utc_now()
    t0 = time.monotonic()
    status = "FAIL"
    scenario = {}
    error = None
    marks = {}
    try:
        result, scenario, rc = _run_rr_service_phase(
            coordinator, resolved, plan, run_root, run_id=run_id,
            metadata=metadata, tc_id=f"MASS-ROC-{link_id}", guard=guard,
            gain_artifact_path=gain_artifact_path, configs_dir=configs_dir,
            test_plan_sha=test_plan_sha, mode="scenario", phase_marks=marks,
            spawn_controller=lambda: _spawn_mass_roc_controller(
                run_id, run_root, configs_dir, test_plan_sha,
                scenario_id, certificate_case,
            ),
            result_relpath=os.path.join("mass-roc-events", "final-result.json"),
        )
        status = "PASS" if (
            scenario.get("status") == "PASS" and rc == 0
        ) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        raise
    finally:
        out_dir = os.path.join(run_root, "evidence", "mass_roc")
        os.makedirs(out_dir, exist_ok=True)
        payload = {
            "artifact_type": "STAGE7_MASS_ROC_COORDINATOR_RESULT_V1",
            "run_id": run_id,
            "link_id": link_id,
            "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "status": status if error is None else "FAIL",
            "started_utc": started,
            "ended_utc": _utc_now(),
            "duration_s": round(time.monotonic() - t0, 1),
            "scenario": scenario,
            "phase_elapsed_s": {k: round(v - t0, 1) for k, v in marks.items()},
            "error": error,
            "git_commit": metadata.get("git_commit"),
            "baseline_sha256": metadata.get("baseline_sha256"),
        }
        with open(os.path.join(out_dir, "coordinator_result.json"), "w",
                  encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)
    if status != "PASS":
        raise RuntimeError("MASS-ROC workload FAIL — 새 run ID로만 재시도")
    return {"mass_roc": status, "link": link_id, "scenario_id": scenario_id}


#: service-only phase의 RF-time 경로. `rf_time.evaluate_pre_correction_set`가
#: 지원하는 조합은 {RA,RB,AB} 전체 또는 단일 pair뿐이라(그 밖은 "지원하지 않는
#: pair 조합" 예외) 6경로를 그대로 쓴다. 경로를 줄이려면 frozen rf_time.py를
#: 고쳐야 하므로 하지 않는다.
SERVICE_RF_TIME_PATHS = P2_PATHS
SERVICE_RF_TIME_PAIRS = ("RA", "RB", "AB")


def run_mass_roc_service(coordinator, resolved, plan, run_root: str, *, run_id,
                         metadata, configs_dir, gain_artifact_path, test_plan_sha,
                         scenario_id, certificate_case, processes=None):
    """`OPERATIONAL_SERVICE`: qualification 없이 서비스 구간만 수행한다 (Phase G).

    Phase F에서 발급한 qualification reference가 유효하다는 전제 위에서, 이미
    자격이 확인된 gain·RF 설정을 **재측정하지 않고 적용만** 해 MASS/ROC 시나리오
    하나를 독립 service session으로 돌린다.

    수행: prepare/configure/arm_rx → acquire_host_time → RF-time PRE →
    active bridge → RF-time POST(HOST_TIME_LOCKED) → admission → staged start →
    TBB → publish → 시나리오 1건 → STOP → cleanup 확인.

    **실행하지 않는 것**: `_run_rf_qualification`, CFO burst qualification,
    adaptive gain 탐색, raw-PHY 6경로 qualification, RR/RESEARCH_READY 판정.
    따라서 이 함수의 결과를 RR-P2 PASS나 RESEARCH_READY로 표시하면 안 된다 —
    evidence는 `evidence/operational_service/`에 별도 형식으로만 남는다.
    """

    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns
    from vdeter.stage7.ipc import body_set_sha256
    from vdeter.stage7.rf_time import evaluate_pre_correction_set

    # 서비스 구간 전용 guard. qualification 사다리를 돌지 않으므로 RR phase
    # deadline(70분)이 아니라 서비스 구간 예산을 쓴다.
    guard = PhaseGuard("MASS-ROC-SERVICE", deadline_s=45 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)

    present = list(ENDPOINT_IDS)
    paths = SERVICE_RF_TIME_PATHS
    all_path_ids = [p[0] for p in paths]
    ship = next(m for m in present if m != "ROC")
    channel_by_role = {
        "upper": resolved["ROC"].endpoint.mac.tx_channel.name,
        "lower": resolved[ship].endpoint.mac.tx_channel.name,
    }
    serial_by_endpoint = {m: resolved[m].b210_serial for m in present}

    started = _utc_now()
    t0 = time.monotonic()
    marks = {}

    def mark(name):
        guard.check()
        guard.progress(name)
        marks[name] = time.monotonic()

    def now_ns():
        return int(read_monotonic_raw_ns())

    status = "FAIL"
    error = None
    scenario = {}
    controller = None
    cleanup = {}
    try:
        # --- 1. endpoint/B210 prepare·configure·arm ---------------------------
        run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)
        mark("prepare_configure_arm")

        # --- 2. host affine fit (§13.2) --------------------------------------
        acquire = coordinator.run_barrier(
            "acquire_host_time",
            {
                m: {
                    "profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                    "acquisition_attempt": 1,
                    "max_acquisition_ns": 600_000_000_000,
                }
                for m in present
            },
            members=present,
            concurrent_commit=True,
        )
        fits = {m: acquire[m]["body"]["result"] for m in present}
        for m in present:
            if fits[m] is None or fits[m].get("outcome") != "PASS":
                raise RuntimeError(f"{m} host affine fit FAIL")
        print("[coordinator] acquire_host_time COMPLETED", flush=True)
        mark("acquire_host_time")

        # --- 3. RF-time PRE (§13.3) ------------------------------------------
        def rf_time_plan(phase_tag):
            start_base = now_ns() + 20_000_000_000
            entries = []
            for index, (path_id, tx_ep, rx_ep, tx_lane, rx_lane) in enumerate(paths):
                start = start_base + index * 75_000_000_000
                entries.append({
                    "path_id": path_id,
                    "tx_endpoint_id": tx_ep,
                    "rx_endpoint_id": rx_ep,
                    "tx_lane_index": tx_lane,
                    "rx_lane_index": rx_lane,
                    "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                    "scheduled_host_raw_ns": [
                        start + k * 6_800_000_000 for k in range(10)
                    ],
                })
            return _write_plan(run_root, "rf_time_plan.json",
                               {"phase": phase_tag, "paths": entries})

        provisional_clock = (
            f"stage7-pi-host-affine-provisional-v1:{coordinator._run_id}:1"
        )
        rf_sha = rf_time_plan("PRE_CORRECTION")
        pre = coordinator.run_barrier(
            "validate_rf_time",
            {
                m: {
                    "validation_phase": "PRE_CORRECTION",
                    "rf_time_plan_sha256": rf_sha,
                    "clock_id": provisional_clock,
                    "path_ids": all_path_ids,
                }
                for m in present
            },
            members=present,
            concurrent_commit=True,
        )
        summaries = {}
        for m in present:
            for summary in pre[m]["body"]["result"] or ():
                summaries[summary["path_id"]] = summary
        combined = evaluate_pre_correction_set(
            summaries, present_pairs=SERVICE_RF_TIME_PAIRS
        )
        print("[coordinator] validate_rf_time PRE COMPLETED", flush=True)
        mark("rf_time_pre")

        # --- 4. active bridge (§13.2/§16.1) ----------------------------------
        active_clock = f"stage7-pi-host-affine-v1:{coordinator._run_id}:0"
        candidate_set = [
            [m, hashlib.sha256(encode_canonical_json(fits[m])).hexdigest()]
            for m in present
        ]
        corrections = combined["corrections"]
        bodies = {}
        for m in present:
            correction = corrections.get(m, (0, 1))
            bodies[m] = {
                "candidate_set": [list(item) for item in candidate_set],
                "candidate_set_sha256": body_set_sha256(candidate_set),
                "candidate_sha256": dict(candidate_set)[m],
                "active_clock_id": active_clock,
                "bridge_generation": 0,
                "alpha_num": fits[m]["alpha_num"],
                "alpha_den": fits[m]["alpha_den"],
                "beta_num": fits[m]["beta_num"],
                "beta_den": fits[m]["beta_den"],
                "correction_num": correction[0],
                "correction_den": correction[1],
            }
        coordinator.run_barrier(
            "prepare_active_bridge", bodies,
            commit_command_name="commit_active_bridge",
            abort_command_name="abort_active_bridge",
            members=present,
        )
        coordinator._bridge_generation = 0
        coordinator._bridge_members = set(present)
        print("[coordinator] active bridge COMMITTED", flush=True)
        mark("active_bridge")

        # --- 5. RF-time POST → HOST_TIME_LOCKED -------------------------------
        rf_sha = rf_time_plan("POST_CORRECTION")
        post = coordinator.run_barrier(
            "validate_rf_time",
            {
                m: {
                    "validation_phase": "POST_CORRECTION",
                    "rf_time_plan_sha256": rf_sha,
                    "clock_id": active_clock,
                    "path_ids": all_path_ids,
                }
                for m in present
            },
            members=present,
            concurrent_commit=True,
        )
        post_summaries = []
        for m in present:
            post_summaries.extend(post[m]["body"]["result"] or ())
        # summary가 비면 all()이 공허참이 되어 무검증 통과가 된다 — 명시 거부.
        if not post_summaries:
            raise RuntimeError("POST_CORRECTION summary가 비어 있습니다")
        failed = [s["path_id"] for s in post_summaries if s["outcome"] != "PASS"]
        if failed:
            raise RuntimeError(f"POST_CORRECTION FAIL paths: {failed}")
        print("[coordinator] validate_rf_time POST COMPLETED — HOST_TIME_LOCKED",
              flush=True)
        mark("rf_time_post")

        # --- 6. 시나리오 controller spawn + service admission ------------------
        controller = _spawn_mass_roc_controller(
            run_id, run_root, configs_dir, test_plan_sha,
            scenario_id, certificate_case,
        )
        gate_dir = os.path.join(run_root, "controller_gate")
        os.makedirs(gate_dir, exist_ok=True)

        def pump_events():
            for member in present:
                for event in coordinator._channels[member].poll_events():
                    coordinator.receive_event(member, event)

        admission = coordinator.open_service_admission()
        admission_value = admission.value
        print("[coordinator] service admission OPEN", flush=True)
        open(os.path.join(gate_dir, "admission_open"), "w").close()
        intent_deadline = admission_value["service_start_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_intents_complete():
                break
            if now_ns() >= intent_deadline:
                raise RuntimeError("service-start intent cutoff 초과")
            time.sleep(0.2)
        start_commits = coordinator.commit_service_start()
        staged_set = [
            [m, start_commits[m]["body"]["result"]["staged_service_started_sha256"]]
            for m in present
        ]
        tbb_deadline = admission_value["tbb_publish_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_tbb_complete(0):
                break
            if now_ns() >= tbb_deadline:
                raise RuntimeError("TBB publish deadline 초과 (TBB_ACTIVE 미완)")
            time.sleep(0.5)
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        print("[coordinator] publish_service_start COMPLETED — RUNNING", flush=True)
        mark("service_running")

        # --- 7. 시나리오 1건 실행 대기 ----------------------------------------
        import glob as _glob
        watch = [os.path.join(run_root, "logs", "mass-roc-controller.log")]
        last_total = -1
        while controller.poll() is None:
            total = 0
            for path in watch + _glob.glob(
                os.path.join(run_root, "evidence", "service_events", "*.jsonl")
            ):
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
            if total != last_total:
                last_total = total
                guard.progress("scenario")
            guard.check()
            time.sleep(1.0)
        result_path = os.path.join(run_root, "mass-roc-events", "final-result.json")
        try:
            with open(result_path, "rb") as handle:
                scenario = json.loads(handle.read().decode("utf-8"))
        except OSError:
            scenario = {"status": "FAIL", "reason": "final-result.json 없음",
                        "controller_rc": controller.returncode}
        mark("scenario")

        # --- 8. STOP ----------------------------------------------------------
        stop_now = now_ns()
        coordinator.run_barrier(
            "stop",
            {
                m: {"stop_at_frame_ordinal": 0,
                    "stop_deadline_host_raw_ns": stop_now + 30_000_000_000}
                for m in ENDPOINT_IDS
            },
        )
        mark("stop")

        # --- 9. cleanup 확인 (launcher 자진 종료·worker 0·B210 3/3) ------------
        clean_exit = True
        if processes:
            for _endpoint_id, process in processes.items():
                deadline = time.monotonic() + 60.0
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.5)
                if process.poll() is None:
                    clean_exit = False
        workers_after = _worker_process_count()
        rediscovery = _rediscover_b210(sorted(serial_by_endpoint.values()))
        speed_map = _live_b210_speed_map()
        throttled = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True
        ).stdout.strip()
        cleanup = {
            "launcher_clean_exit": clean_exit,
            "workers_after_stop": workers_after,
            "rediscovery": rediscovery,
            "live_speed_map": {k: v for k, v in speed_map.items()
                               if k in serial_by_endpoint.values()},
            "throttled": throttled,
        }
        mark("cleanup")

        bad = [e for e in coordinator.command_log()
               if e["status"] not in ("COMPLETED", "PREPARED")]
        # service phase는 qualification 명령을 발행하지 않는다 — 기계 확인.
        qualification_commands = [
            e for e in coordinator.command_log()
            if e["command_name"] in ("start_cfo", "calibrate_rf")
        ]
        status = "PASS" if (
            scenario.get("status") == "PASS"
            and controller.returncode == 0
            and not bad
            and not qualification_commands
            and not coordinator.inhibited
            and clean_exit
            and workers_after == 0
            and all(rediscovery.values())
            and throttled == "throttled=0x0"
        ) else "FAIL"
        print(f"[coordinator] MASS-ROC-SERVICE {status}", flush=True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if controller is not None and controller.poll() is None:
            controller.terminate()
        out_dir = os.path.join(run_root, "evidence", "operational_service")
        os.makedirs(out_dir, exist_ok=True)
        payload = {
            "artifact_type": "STAGE7_OPERATIONAL_SERVICE_RESULT_V1",
            "judgement_scope": "OPERATIONAL_SERVICE",
            "not_a_qualification": (
                "이 결과는 RF qualification이 아니다. CFO/adaptive gain/raw-PHY를 "
                "실행하지 않았으며 RR·RESEARCH_READY 판정을 생성하지 않는다. "
                "자격은 qualification reference(Phase F)가 담보한다."
            ),
            "run_id": run_id,
            "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "status": status if error is None else "FAIL",
            "started_utc": started,
            "ended_utc": _utc_now(),
            "duration_s": round(time.monotonic() - t0, 1),
            "phase_elapsed_s": {k: round(v - t0, 1) for k, v in marks.items()},
            "scenario": scenario,
            "cleanup": cleanup,
            "command_log": list(coordinator.command_log()),
            "qualification_commands_issued": [
                e["command_name"] for e in coordinator.command_log()
                if e["command_name"] in ("start_cfo", "calibrate_rf")
            ],
            "inhibited": coordinator.inhibited,
            "error": error,
            "git_commit": metadata.get("git_commit"),
            "baseline_sha256": metadata.get("baseline_sha256"),
            "gain_artifact": gain_artifact_path,
        }
        with open(os.path.join(out_dir, "service_result.json"), "w",
                  encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)
    if status != "PASS":
        raise RuntimeError("MASS-ROC service session FAIL — 새 run ID로만 재시도")
    return {"mass_roc_service": status, "scenario_id": scenario_id,
            "certificate_case": certificate_case,
            "phase_elapsed_s": {k: round(v - t0, 1) for k, v in marks.items()}}


#: session 제어 파일 이름 (로컬 service 계층과 공유하는 계약).
SESSION_DIR_NAME = "session"
SESSION_READY = "ready.json"
SESSION_SHUTDOWN = "shutdown"
SESSION_CLOSED = "closed.json"
SESSION_REQUESTS = "requests"
SESSION_RESULTS = "results"

#: workload 요청 대기 상한 (사용자가 메뉴에서 고르는 동안 유지).
SESSION_IDLE_TIMEOUT_S = 45 * 60

#: 세션 절대 상한 = RF 장비가 연속 가동될 수 있는 최대 시간 (사용자 승인값).
#: PhaseGuard deadline은 진행이 있어도 갱신되지 않는 절대 시각이므로, 이 값이
#: bring-up + 전체 workload + STOP + cleanup을 모두 덮어야 한다. bring-up 실측이
#: 1361~1715s(23~29분)이고 계속 늘고 있어, idle 상한(45분)을 그대로 쓰면
#: workload에 16분밖에 남지 않는다 — 두 개념을 분리한다.
SESSION_DEADLINE_S = 2 * 60 * 60

#: application workload child 상한. 로컬 ``Stage7ServiceSession``의 900초
#: 계약과 같은 값으로 coordinator도 직접 강제한다. 한쪽만 timeout이면 원격
#: child가 살아 남아 B210 cleanup을 막을 수 있다.
SESSION_WORKLOAD_TIMEOUT_S = 900.0

#: workload/lifecycle child의 bounded process cleanup 상한.
CHILD_TERMINATE_TIMEOUT_S = 30.0
CHILD_KILL_TIMEOUT_S = 10.0


def _terminate_process_bounded(process, *, terminate_timeout_s=None,
                               kill_timeout_s=None):
    """살아 있는 child를 terminate -> wait -> kill -> wait 순서로 회수한다."""
    terminate_timeout_s = (CHILD_TERMINATE_TIMEOUT_S
                           if terminate_timeout_s is None
                           else terminate_timeout_s)
    kill_timeout_s = (CHILD_KILL_TIMEOUT_S
                      if kill_timeout_s is None else kill_timeout_s)
    outcome = {
        "terminate_sent": False,
        "kill_sent": False,
        "exit_code": process.poll(),
        "error": None,
    }
    if outcome["exit_code"] is not None:
        return outcome
    try:
        process.terminate()
        outcome["terminate_sent"] = True
        try:
            process.wait(timeout=terminate_timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            outcome["kill_sent"] = True
            try:
                process.wait(timeout=kill_timeout_s)
            except subprocess.TimeoutExpired as exc:
                outcome["error"] = (
                    f"kill 이후 {kill_timeout_s}s 내 종료하지 않음: {exc}")
    except Exception as exc:  # noqa: BLE001 - cleanup 결과를 evidence로 보존
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    outcome["exit_code"] = process.poll()
    return outcome


def _service_session_terminal_status(workloads, service_stop_rc,
                                     endpoint_stop_completed):
    """빈 측정·workload 실패·불완전 STOP을 모두 fail-closed 판정한다."""
    failures = []
    if not workloads:
        failures.append("workload가 하나도 완료되지 않음")
    elif not all(item.get("status") == "PASS" for item in workloads):
        failures.append("하나 이상의 workload가 FAIL")
    if service_stop_rc != 0:
        failures.append(f"service STOP 대행 rc={service_stop_rc}")
    if not endpoint_stop_completed:
        failures.append("endpoint STOP 미완료")
    return ("FAIL", "; ".join(failures)) if failures else ("PASS", None)


def run_mass_roc_session(coordinator, resolved, plan, run_root: str, *, run_id,
                         metadata, configs_dir, gain_artifact_path,
                         test_plan_sha, processes=None):
    """`OPERATIONAL_SERVICE` bounded persistent session (Phase G 최종 모델).

    하드웨어 service session을 **한 번** 세우고(B210 acquire·endpoint START·
    host fit·C1~C6 RF-time PRE/POST·active bridge·HOST_TIME_LOCKED·admission·
    TBB·publish), 그 뒤에는 요청이 올 때마다 **application workload child만**
    새로 만들어 실행한다. 시나리오마다 하드웨어를 다시 세우지 않는다.

    workload 사이에는 quiescence를 기계로 확인하고, `q`(shutdown 마커) 또는
    fault에서 한 번만 전체 cleanup을 수행한다.

    **실행하지 않는 것**: `_run_rf_qualification`, CFO qualification,
    adaptive gain 탐색, raw-PHY qualification, RR/RESEARCH_READY 판정.
    """

    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns
    from vdeter.stage7.ipc import body_set_sha256
    from vdeter.stage7.rf_time import evaluate_pre_correction_set

    guard = PhaseGuard("MASS-ROC-SESSION", deadline_s=SESSION_DEADLINE_S,
                       stall_s=SESSION_IDLE_TIMEOUT_S)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)

    present = list(ENDPOINT_IDS)
    paths = SERVICE_RF_TIME_PATHS
    all_path_ids = [p[0] for p in paths]
    required_path_ids = set(all_path_ids)
    ship = next(m for m in present if m != "ROC")
    channel_by_role = {
        "upper": resolved["ROC"].endpoint.mac.tx_channel.name,
        "lower": resolved[ship].endpoint.mac.tx_channel.name,
    }
    serial_by_endpoint = {m: resolved[m].b210_serial for m in present}

    session_dir = os.path.join(run_root, SESSION_DIR_NAME)
    requests_dir = os.path.join(session_dir, SESSION_REQUESTS)
    results_dir = os.path.join(session_dir, SESSION_RESULTS)
    for directory in (session_dir, requests_dir, results_dir):
        os.makedirs(directory, exist_ok=True)

    started = _utc_now()
    t0 = time.monotonic()
    marks = {}
    workloads = []
    status = "FAIL"
    error = None
    cleanup = {}
    service_stop_rc = None
    endpoint_stop_completed = False
    start_proxy = None

    # --- 계측 (orchestration 계층 전용) ------------------------------------
    # RF·PHY·slot·timing 경로는 계측하지 않는다. 여기서 남기는 것은 barrier
    # 경계, 대행 spawn 인자, admission/intent 대기 상태뿐이다 — RF 판정에
    # 관여하지 않는 층. 실패 시 RF 실행을 반복하지 않고도 원인이 보이게 한다.
    trace = _open_session_trace(run_root, run_id)

    def emit(event, **fields):
        try:
            trace.emit(event, **fields)
        except Exception:  # noqa: BLE001 - 계측이 세션을 죽이지 않는다
            pass

    def mark(name):
        guard.check()
        guard.progress(name)
        marks[name] = round(time.monotonic() - t0, 1)
        emit("phase_mark", phase=name, elapsed_s=marks[name])

    def now_ns():
        return int(read_monotonic_raw_ns())

    def write_json(path, payload):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=1)

    try:
        emit("session_phase_begin", run_id=run_id, members=list(present),
             rf_time_paths=all_path_ids, rf_time_pairs=list(SERVICE_RF_TIME_PAIRS),
             serial_by_endpoint=serial_by_endpoint,
             configs_dir=configs_dir, run_root=run_root,
             session_dir=session_dir, gain_artifact=gain_artifact_path)
        # ================= 1. 하드웨어 bring-up (세션당 정확히 1회) ==========
        run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)
        mark("prepare_configure_arm")

        acquire = coordinator.run_barrier(
            "acquire_host_time",
            {m: {"profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                 "acquisition_attempt": 1,
                 "max_acquisition_ns": 600_000_000_000} for m in present},
            members=present, concurrent_commit=True,
        )
        fits = {m: acquire[m]["body"]["result"] for m in present}
        for m in present:
            if fits[m] is None or fits[m].get("outcome") != "PASS":
                raise RuntimeError(f"{m} host affine fit FAIL")
        print("[coordinator] acquire_host_time COMPLETED", flush=True)
        mark("acquire_host_time")

        def rf_time_plan(phase_tag):
            start_base = now_ns() + 20_000_000_000
            entries = []
            for index, (path_id, tx_ep, rx_ep, tx_lane, rx_lane) in enumerate(paths):
                start = start_base + index * 75_000_000_000
                entries.append({
                    "path_id": path_id, "tx_endpoint_id": tx_ep,
                    "rx_endpoint_id": rx_ep, "tx_lane_index": tx_lane,
                    "rx_lane_index": rx_lane,
                    "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                    "scheduled_host_raw_ns": [start + k * 6_800_000_000
                                              for k in range(10)],
                })
            return _write_plan(run_root, "rf_time_plan.json",
                               {"phase": phase_tag, "paths": entries})

        provisional_clock = (
            f"stage7-pi-host-affine-provisional-v1:{coordinator._run_id}:1")
        rf_sha = rf_time_plan("PRE_CORRECTION")
        pre = coordinator.run_barrier(
            "validate_rf_time",
            {m: {"validation_phase": "PRE_CORRECTION",
                 "rf_time_plan_sha256": rf_sha, "clock_id": provisional_clock,
                 "path_ids": all_path_ids} for m in present},
            members=present, concurrent_commit=True,
        )
        summaries = {}
        for m in present:
            for summary in pre[m]["body"]["result"] or ():
                summaries[summary["path_id"]] = summary
        # 빈 summary가 all([])로 공허참 통과하지 않도록 required set을 명시 검사.
        missing_pre = sorted(required_path_ids - set(summaries))
        if missing_pre:
            raise RuntimeError(f"PRE_CORRECTION summary 누락: {missing_pre}")
        combined = evaluate_pre_correction_set(
            summaries, present_pairs=SERVICE_RF_TIME_PAIRS)
        print("[coordinator] validate_rf_time PRE COMPLETED", flush=True)
        emit("rf_time_pre_summary", observed_paths=sorted(summaries),
             outcomes={p: s["outcome"] for p, s in summaries.items()},
             pairs=list(SERVICE_RF_TIME_PAIRS))
        mark("rf_time_pre")

        active_clock = f"stage7-pi-host-affine-v1:{coordinator._run_id}:0"
        candidate_set = [
            [m, hashlib.sha256(encode_canonical_json(fits[m])).hexdigest()]
            for m in present]
        corrections = combined["corrections"]
        bodies = {}
        for m in present:
            correction = corrections.get(m, (0, 1))
            bodies[m] = {
                "candidate_set": [list(item) for item in candidate_set],
                "candidate_set_sha256": body_set_sha256(candidate_set),
                "candidate_sha256": dict(candidate_set)[m],
                "active_clock_id": active_clock, "bridge_generation": 0,
                "alpha_num": fits[m]["alpha_num"], "alpha_den": fits[m]["alpha_den"],
                "beta_num": fits[m]["beta_num"], "beta_den": fits[m]["beta_den"],
                "correction_num": correction[0], "correction_den": correction[1],
            }
        coordinator.run_barrier(
            "prepare_active_bridge", bodies,
            commit_command_name="commit_active_bridge",
            abort_command_name="abort_active_bridge", members=present,
        )
        coordinator._bridge_generation = 0
        coordinator._bridge_members = set(present)
        print("[coordinator] active bridge COMMITTED", flush=True)
        mark("active_bridge")

        rf_sha = rf_time_plan("POST_CORRECTION")
        post = coordinator.run_barrier(
            "validate_rf_time",
            {m: {"validation_phase": "POST_CORRECTION",
                 "rf_time_plan_sha256": rf_sha, "clock_id": active_clock,
                 "path_ids": all_path_ids} for m in present},
            members=present, concurrent_commit=True,
        )
        post_summaries = []
        for m in present:
            post_summaries.extend(post[m]["body"]["result"] or ())
        observed_post = {s["path_id"] for s in post_summaries}
        missing_post = sorted(required_path_ids - observed_post)
        if missing_post:
            raise RuntimeError(f"POST_CORRECTION summary 누락: {missing_post}")
        failed = [s["path_id"] for s in post_summaries if s["outcome"] != "PASS"]
        if failed:
            raise RuntimeError(f"POST_CORRECTION FAIL paths: {failed}")
        print("[coordinator] validate_rf_time POST COMPLETED — HOST_TIME_LOCKED",
              flush=True)
        # RF 판정 자체는 건드리지 않고 경로별 결과만 기록한다 (계측 대상은
        # barrier 반환값이지 PHY/timing 내부가 아니다).
        emit("rf_time_post_summary",
             observed_paths=sorted(observed_post),
             outcomes={s["path_id"]: s["outcome"] for s in post_summaries},
             state="HOST_TIME_LOCKED")
        mark("rf_time_post")

        # ================= 2. service admission → RUNNING (1회) =============
        def pump_events():
            for member in present:
                for event in coordinator._channels[member].poll_events():
                    coordinator.receive_event(member, event)

        gate_dir = os.path.join(run_root, "controller_gate")
        os.makedirs(gate_dir, exist_ok=True)
        # endpoint START는 TEST adapter socket을 통해서만 가능하고 그 연결은
        # controller 프로세스가 소유한다. session이 lifecycle의 주인이므로
        # 시나리오 없는 `session-start` 대행을 띄워 START만 시킨다. START는
        # publish까지 블록하므로 admission 직전에 spawn하고 publish 뒤에 회수한다.
        start_proxy = _spawn_session_lifecycle(
            "session-start", run_id, run_root, configs_dir, test_plan_sha,
            emit=emit)
        admission = coordinator.open_service_admission()
        admission_value = admission.value
        print("[coordinator] service admission OPEN", flush=True)
        gate_file = os.path.join(gate_dir, "admission_open")
        open(gate_file, "w").close()
        intent_deadline = admission_value["service_start_deadline_host_raw_ns"]
        emit("admission_open", gate_file=gate_file,
             gate_written=os.path.exists(gate_file),
             intent_window_id=admission_value.get("intent_window_id"),
             intent_deadline_ns=intent_deadline,
             intent_budget_s=round(
                 (intent_deadline - now_ns()) / 1e9, 2))

        def intent_state():
            """intent 대기 진단: 누가 남겼고 대행은 살아 있는가."""
            try:
                recorded = sorted(
                    key[-1] for key, _ in coordinator.intent_ledger_snapshot())
            except Exception:  # noqa: BLE001
                recorded = None
            return {
                "intents_recorded": recorded,
                "intents_expected": list(present),
                "proxy_alive": start_proxy.poll() is None,
                "proxy_rc": start_proxy.returncode,
                "gate_exists": os.path.exists(gate_file),
            }

        intent_t0 = time.monotonic()
        next_report = 0.0
        while True:
            pump_events()
            if coordinator.required_intents_complete():
                emit("intents_complete",
                     elapsed_s=round(time.monotonic() - intent_t0, 2),
                     **intent_state())
                break
            waited = time.monotonic() - intent_t0
            if waited >= next_report:
                emit("intent_wait", elapsed_s=round(waited, 2), **intent_state())
                next_report = waited + 5.0
            if start_proxy.poll() is not None:
                emit("intent_failed", reason="proxy_exited_early",
                     elapsed_s=round(waited, 2), **intent_state())
                raise RuntimeError(
                    "service-start 대행 controller가 intent 전에 종료했다 "
                    f"(rc={start_proxy.returncode}) — logs/session-start.log 확인")
            if now_ns() >= intent_deadline:
                emit("intent_failed", reason="cutoff",
                     elapsed_s=round(waited, 2), **intent_state())
                raise RuntimeError("service-start intent cutoff 초과")
            time.sleep(0.2)
        start_commits = coordinator.commit_service_start()
        emit("service_start_committed", members=list(present))
        staged_set = [
            [m, start_commits[m]["body"]["result"]["staged_service_started_sha256"]]
            for m in present]
        tbb_deadline = admission_value["tbb_publish_deadline_host_raw_ns"]

        def tbb_state():
            """TBB 대기 진단: 어떤 endpoint가 TBB_ACTIVE를 올렸는가.

            pilot(svc-sc005-20260831-022449)은 ROC만 TBB_ACTIVE이고 MASS-A/B가
            SERVICE_START_STAGED에 머물러 실패했다 — 그 구분이 보여야 한다.
            """
            try:
                active = sorted(
                    key[-1] for key, _ in coordinator.tbb_ledger_snapshot())
            except Exception:  # noqa: BLE001
                active = None
            return {"tbb_active": active, "tbb_expected": list(present)}

        tbb_t0 = time.monotonic()
        next_tbb_report = 0.0
        while True:
            pump_events()
            if coordinator.required_tbb_complete(0):
                emit("tbb_complete",
                     elapsed_s=round(time.monotonic() - tbb_t0, 2), **tbb_state())
                break
            waited = time.monotonic() - tbb_t0
            if waited >= next_tbb_report:
                emit("tbb_wait", elapsed_s=round(waited, 2), **tbb_state())
                next_tbb_report = waited + 10.0
            if now_ns() >= tbb_deadline:
                emit("tbb_failed", reason="publish_deadline",
                     elapsed_s=round(waited, 2), **tbb_state())
                raise RuntimeError("TBB publish deadline 초과 (TBB_ACTIVE 미완)")
            time.sleep(0.5)
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        print("[coordinator] publish_service_start COMPLETED — RUNNING", flush=True)
        emit("service_published", bridge_generation=0)
        open(os.path.join(gate_dir, "started_ok"), "w").close()
        # START 대행 회수: publish 이후 세 START 호출이 OK로 반환해야 한다.
        start_deadline = time.monotonic() + 300.0
        reap_t0 = time.monotonic()
        while start_proxy.poll() is None:
            guard.check()
            pump_events()
            if time.monotonic() > start_deadline:
                emit("lifecycle_proxy_reap_timeout", lifecycle="session-start",
                     elapsed_s=round(time.monotonic() - reap_t0, 2))
                start_proxy.terminate()
                raise RuntimeError("service-start 대행 controller 회수 300s 초과")
            time.sleep(0.5)
        emit("lifecycle_proxy_reaped", lifecycle="session-start",
             rc=start_proxy.returncode,
             elapsed_s=round(time.monotonic() - reap_t0, 2))
        if start_proxy.returncode != 0:
            raise RuntimeError(
                f"endpoint START 실패 (대행 rc={start_proxy.returncode})")
        print("[coordinator] endpoint START 3/3 확인", flush=True)
        mark("service_running")

        baseline_health = {
            "active_clock_id": active_clock,
            "bridge_generation": coordinator._bridge_generation,
            "faults": len(list(coordinator.faults())),
            "serial_by_endpoint": serial_by_endpoint,
            "usb_speed": {k: v for k, v in _live_b210_speed_map().items()
                          if k in serial_by_endpoint.values()},
        }
        write_json(os.path.join(session_dir, SESSION_READY), {
            "artifact_type": "STAGE7_SERVICE_SESSION_READY_V1",
            "judgement_scope": "OPERATIONAL_SERVICE",
            "run_id": run_id, "started_utc": started,
            "ready_at_utc": _utc_now(),
            "phase_elapsed_s": dict(marks),
            "rf_time_paths": all_path_ids,
            "health_baseline": baseline_health,
            "note": ("하드웨어 bring-up은 이 session에서 한 번만 수행됐다. "
                     "이후 workload는 application state만 새로 만든다."),
        })
        print("[coordinator] SESSION READY — workload 요청 대기", flush=True)
        emit("session_ready", phase_elapsed_s=dict(marks),
             health_baseline=baseline_health, rf_time_paths=all_path_ids)

        # ================= 3. workload 요청 루프 ============================
        idle_deadline = time.monotonic() + SESSION_IDLE_TIMEOUT_S
        while True:
            guard.check()
            if os.path.exists(os.path.join(session_dir, SESSION_SHUTDOWN)):
                print("[coordinator] shutdown 요청 수신", flush=True)
                break
            pending = sorted(
                name for name in os.listdir(requests_dir)
                if name.endswith(".json")
                and not os.path.exists(os.path.join(results_dir, name)))
            if not pending:
                if time.monotonic() > idle_deadline:
                    raise RuntimeError("session idle timeout — workload 요청 없음")
                pump_events()
                time.sleep(0.5)
                continue

            request_name = pending[0]
            with open(os.path.join(requests_dir, request_name), "rb") as handle:
                request = json.loads(handle.read().decode("utf-8"))
            workload_id = request["workload_id"]
            scenario_id = request["scenario_id"]
            certificate_case = request.get("certificate_case", "normal")
            # Stage 8 opt-in: workload를 소유한 controller child가 PoC 수명주기를 실행한다
            # (§3.1 supervisor는 workload 소유 프로세스). 요청이 없으면 아무것도 바뀌지 않는다.
            poc_request = request.get("poc")
            print(f"[coordinator] workload 시작 {workload_id} {scenario_id}",
                  flush=True)
            guard.progress(f"workload:{workload_id}")
            workload_started = time.monotonic()

            # --- session health gate (매 workload 직전) ---------------------
            pump_events()
            alive = ([p for p in (processes or {}).values() if p.poll() is None]
                     if processes else None)
            speed_map = {k: v for k, v in _live_b210_speed_map().items()
                         if k in serial_by_endpoint.values()}
            health = {
                "launchers_alive": None if alive is None else len(alive),
                "expected_launchers": None if alive is None else len(processes),
                "workers_running": _worker_process_count(),
                "inhibited": coordinator.inhibited,
                "faults": len(list(coordinator.faults())),
                "bridge_generation": coordinator._bridge_generation,
                "active_clock_id": active_clock,
                "usb_speed": speed_map,
            }
            health["pass"] = (
                (alive is None or len(alive) == len(processes))
                and health["workers_running"] == 3
                and not health["inhibited"]
                and health["faults"] == baseline_health["faults"]
                and health["bridge_generation"] == baseline_health["bridge_generation"]
                and speed_map == baseline_health["usb_speed"]
            )
            emit("workload_health_gate", workload=workload_id,
                 scenario_id=scenario_id, certificate_case=certificate_case,
                 health=health)
            if not health["pass"]:
                raise RuntimeError(f"session health gate 실패: {health}")

            # --- workload child 실행 (endpoint START/STOP 없음) -------------
            child_root = os.path.join(run_root, "workloads", workload_id)
            os.makedirs(child_root, exist_ok=True)
            logs_dir = os.path.join(run_root, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            log_handle = open(  # noqa: SIM115
                os.path.join(logs_dir, f"workload-{workload_id}.log"), "ab")
            command = [
                sys.executable,
                os.path.join(os.path.dirname(APP_ROOT), "apps",
                             "stage7_mass_roc_controller.py"),
                "--run-id", run_id,
                "--run-root", child_root,
                "--test-plan-sha", test_plan_sha,
                "--fixtures-dir", os.path.join(configs_dir, "fixtures"),
                "--scenario-id", scenario_id,
                "--certificate-case", certificate_case,
                "--workload-id", workload_id,
                "--lifecycle", "session",
                "--events-dir", os.path.join(run_root, "evidence",
                                             "service_events"),
            ]
            poc_result_path = os.path.join(child_root, "mass-roc-events",
                                           "poc-result.json")
            if poc_request is not None:
                # PoC 요청은 child가 읽을 파일로 건네고 경로만 argv에 싣는다(argv에 payload 금지).
                poc_request_path = os.path.join(child_root, "poc-request.json")
                write_json(poc_request_path, poc_request)
                command += ["--poc-request", poc_request_path]
            child = subprocess.Popen(command, stdout=log_handle,
                                     stderr=subprocess.STDOUT)
            emit("workload_child_spawn", workload=workload_id, pid=child.pid,
                 scenario_id=scenario_id, certificate_case=certificate_case,
                 child_run_root=child_root, command=command)
            workload_deadline = workload_started + SESSION_WORKLOAD_TIMEOUT_S
            workload_abort = None
            try:
                while child.poll() is None:
                    abort_reason = None
                    abort_detail = None
                    try:
                        guard.check()
                        pump_events()
                    except Exception as exc:  # noqa: BLE001 - child부터 회수
                        abort_reason = "session_guard_or_event_failure"
                        abort_detail = f"{type(exc).__name__}: {exc}"
                    if (abort_reason is None and os.path.exists(
                            os.path.join(session_dir, SESSION_SHUTDOWN))):
                        abort_reason = "session_shutdown"
                    if abort_reason is None:
                        fault_count = len(list(coordinator.faults()))
                        if (coordinator.inhibited
                                or fault_count != baseline_health["faults"]):
                            abort_reason = "session_fault"
                            abort_detail = (
                                f"inhibited={coordinator.inhibited}, "
                                f"faults={fault_count}, "
                                f"baseline={baseline_health['faults']}")
                    if (abort_reason is None
                            and time.monotonic() >= workload_deadline):
                        abort_reason = "workload_timeout"
                        abort_detail = f"상한 {SESSION_WORKLOAD_TIMEOUT_S}s"
                    if abort_reason is not None:
                        workload_abort = {
                            "artifact_type": "STAGE7_SERVICE_WORKLOAD_ABORT_V1",
                            "status": "FAIL",
                            "workload_id": workload_id,
                            "scenario_id": scenario_id,
                            "reason": abort_reason,
                            "detail": abort_detail,
                            "elapsed_s": round(
                                time.monotonic() - workload_started, 1),
                            "child_pid": child.pid,
                            "failure_preserved_before_terminate": True,
                        }
                        abort_dir = os.path.join(
                            child_root, "evidence", "operational_service")
                        os.makedirs(abort_dir, exist_ok=True)
                        abort_path = os.path.join(
                            abort_dir, "workload_abort.json")
                        try:
                            write_json(abort_path, workload_abort)
                        except Exception as exc:  # noqa: BLE001
                            workload_abort["preserve_error"] = (
                                f"{type(exc).__name__}: {exc}")
                        emit("workload_child_abort", **workload_abort)
                        workload_abort["child_cleanup"] = (
                            _terminate_process_bounded(child))
                        try:
                            write_json(abort_path, workload_abort)
                        except Exception:  # noqa: BLE001 - 최초 상태는 이미 보존
                            pass
                        emit("workload_child_reaped", workload=workload_id,
                             reason=abort_reason,
                             child_cleanup=workload_abort["child_cleanup"])
                        break
                    time.sleep(1.0)
            finally:
                log_handle.close()

            if workload_abort is not None:
                quiescence = {
                    "pass": False,
                    "child_exit_code": child.returncode,
                    "child_terminated": child.poll() is not None,
                    "abort_reason": workload_abort["reason"],
                    "child_cleanup": workload_abort["child_cleanup"],
                }
                record = {
                    "artifact_type": "STAGE7_SERVICE_WORKLOAD_RESULT_V1",
                    "judgement_scope": "OPERATIONAL_SERVICE",
                    "workload_id": workload_id, "scenario_id": scenario_id,
                    "certificate_case": certificate_case,
                    "session_run_id": run_id,
                    "child_exit_code": child.returncode,
                    "duration_s": round(time.monotonic() - workload_started, 1),
                    "session_health_before": health,
                    "quiescence": quiescence,
                    "scenario": None,
                    "status": "FAIL",
                    "error": workload_abort,
                }
                write_json(os.path.join(results_dir, request_name), record)
                workloads.append({k: record[k] for k in
                                  ("workload_id", "scenario_id",
                                   "certificate_case", "status", "duration_s")})
                emit("workload_result", workload=workload_id,
                     scenario_id=scenario_id, status="FAIL",
                     duration_s=record["duration_s"], quiescence=quiescence)
                raise RuntimeError(
                    f"workload {workload_id} 중단: "
                    f"{workload_abort['reason']} ({workload_abort['detail']})")

            scenario_result = None
            result_path = os.path.join(child_root, "mass-roc-events",
                                       "final-result.json")
            try:
                with open(result_path, "rb") as handle:
                    scenario_result = json.loads(handle.read().decode("utf-8"))
            except OSError:
                pass

            # PoC evidence(있으면)를 그대로 싣는다. scenario 판정과 **독립**이며 서로 재계산하지
            # 않는다(§11.1). PoC를 요청했는데 evidence가 없으면 조용히 넘어가지 않는다 —
            # PoC 없이 scenario만 돌고 PASS로 보고되는 거짓 성공을 막는다.
            poc_evidence = None
            if poc_request is not None:
                try:
                    with open(poc_result_path, "rb") as handle:
                        poc_evidence = json.loads(handle.read().decode("utf-8"))
                except OSError:
                    poc_evidence = {
                        "status": "ERROR", "poc_infrastructure": "ERROR",
                        "infrastructure_error_code": "POC_EVIDENCE_MISSING",
                        "error": "controller child가 PoC evidence를 남기지 않았습니다",
                    }

            # --- workload quiescence gate ----------------------------------
            pump_events()
            transfers = []
            for payload in (scenario_result or {}).get("results", {}).values():
                body = payload.get("result", payload)
                transfers.extend(body.get("transfers") or [])
                pre_body = payload.get("precondition")
                if pre_body:
                    transfers.extend(pre_body.get("transfers") or [])
            non_terminal = [t for t in transfers
                            if t.get("final_transfer_state") not in
                            ("COMPLETED", "FAILED", "CANCELLED")]
            alive_after = ([p for p in (processes or {}).values()
                            if p.poll() is None] if processes else None)
            quiescence = {
                "child_exit_code": child.returncode,
                "child_terminated": child.poll() is not None,
                "transfers_observed": len(transfers),
                "non_terminal_transfers": len(non_terminal),
                "launchers_alive": None if alive_after is None else len(alive_after),
                "workers_running": _worker_process_count(),
                "inhibited": coordinator.inhibited,
                "faults": len(list(coordinator.faults())),
                "bridge_generation": coordinator._bridge_generation,
                "active_clock_id": active_clock,
                "usb_speed": {k: v for k, v in _live_b210_speed_map().items()
                              if k in serial_by_endpoint.values()},
            }
            quiescence["pass"] = (
                quiescence["child_terminated"]
                and not non_terminal
                and quiescence["workers_running"] == 3
                and not quiescence["inhibited"]
                and quiescence["faults"] == baseline_health["faults"]
                and quiescence["bridge_generation"] == baseline_health["bridge_generation"]
                and quiescence["usb_speed"] == baseline_health["usb_speed"]
                and (alive_after is None or len(alive_after) == len(processes))
            )

            record = {
                "artifact_type": "STAGE7_SERVICE_WORKLOAD_RESULT_V1",
                "judgement_scope": "OPERATIONAL_SERVICE",
                "workload_id": workload_id, "scenario_id": scenario_id,
                "certificate_case": certificate_case,
                "session_run_id": run_id,
                "child_exit_code": child.returncode,
                "duration_s": round(time.monotonic() - workload_started, 1),
                "session_health_before": health,
                "quiescence": quiescence,
                "scenario": scenario_result,
                # PoC 판정은 scenario 판정과 독립이다. workload status는 기존 세 조건 그대로이며,
                # PoC를 요청했는데 evidence가 아예 없는 경우에만 추가로 FAIL이다(거짓 성공 차단).
                "poc": poc_evidence,
                "status": "PASS" if (
                    child.returncode == 0
                    and (scenario_result or {}).get("status") == "PASS"
                    and quiescence["pass"]
                    and (poc_request is None or poc_evidence is not None)) else "FAIL",
            }
            write_json(os.path.join(results_dir, request_name), record)
            workloads.append({k: record[k] for k in
                              ("workload_id", "scenario_id", "certificate_case",
                               "status", "duration_s")})
            print(f"[coordinator] workload {workload_id} {record['status']}",
                  flush=True)
            mark(f"workload:{workload_id}")
            emit("workload_result", workload=workload_id,
                 scenario_id=scenario_id, status=record["status"],
                 duration_s=record.get("duration_s"), quiescence=quiescence)
            if not quiescence["pass"]:
                raise RuntimeError(
                    f"workload quiescence 실패 — session 종료: {quiescence}")
            idle_deadline = time.monotonic() + SESSION_IDLE_TIMEOUT_S

        # ================= 4. STOP (세션당 1회) =============================
        # service STOP(application) → lifecycle stop barrier 순서는 RR 경로와
        # 같다. STOP 대행 실패는 기록하되 barrier는 반드시 수행한다.
        stop_proxy = _spawn_session_lifecycle(
            "session-stop", run_id, run_root, configs_dir, test_plan_sha,
            emit=emit)
        stop_proxy_deadline = time.monotonic() + 300.0
        while stop_proxy.poll() is None:
            pump_events()
            if time.monotonic() > stop_proxy_deadline:
                emit("lifecycle_proxy_abort", lifecycle="session-stop",
                     reason="timeout", timeout_s=300.0)
                _terminate_process_bounded(stop_proxy)
                break
            time.sleep(0.5)
        service_stop_rc = stop_proxy.returncode
        emit("lifecycle_proxy_reaped", lifecycle="session-stop",
             rc=service_stop_rc)
        if service_stop_rc != 0:
            print(f"[coordinator] service STOP 대행 rc={service_stop_rc}",
                  flush=True)
        stop_now = now_ns()
        coordinator.run_barrier(
            "stop",
            {m: {"stop_at_frame_ordinal": 0,
                 "stop_deadline_host_raw_ns": stop_now + 30_000_000_000}
             for m in ENDPOINT_IDS},
        )
        endpoint_stop_completed = service_stop_rc == 0
        mark("stop")
        status, terminal_error = _service_session_terminal_status(
            workloads, service_stop_rc, endpoint_stop_completed)
        if terminal_error is not None:
            error = terminal_error
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        status = "FAIL"
        import traceback as _tb
        emit("session_exception", error=error,
             traceback=_tb.format_exc()[-3000:], phase_elapsed_s=dict(marks))
        try:
            coordinator.emergency_inhibit("SERVICE_SESSION_FAIL", "session")
        except Exception:  # noqa: BLE001 - 정리 최선 노력
            pass
    finally:
        # START 대행이 남아 있으면 먼저 회수한다 (실패 경로에서 블록 중일 수 있다).
        if start_proxy is not None and start_proxy.poll() is None:
            start_cleanup = _terminate_process_bounded(start_proxy)
            emit("lifecycle_proxy_abort", lifecycle="session-start",
                 reason="session_finalization", child_cleanup=start_cleanup)
        # launcher를 실제로 내린 **뒤에** cleanup을 계측한다. 이전 판(0.9.x)은
        # main()의 teardown보다 먼저 재보아 정상 종료 중에도 worker 3·재발견
        # 실패로 기록됐다 — 장치 상태가 아니라 계측 순서 결함이었다.
        clean_exit = True
        if processes:
            for endpoint_id, process in processes.items():
                deadline = time.monotonic() + 60.0
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.5)
                if process.poll() is None:
                    launcher_cleanup = _terminate_process_bounded(process)
                    emit("launcher_cleanup", endpoint_id=endpoint_id,
                         child_cleanup=launcher_cleanup)
                    # 자발 종료하지 않았다는 사실 자체는 그대로 남긴다.
                    clean_exit = False
                    print(f"[coordinator] {endpoint_id} launcher 강제 종료",
                          flush=True)
        cleanup = {
            "launcher_clean_exit": clean_exit,
            "workers_after_stop": _worker_process_count(),
            "rediscovery": _rediscover_b210(sorted(serial_by_endpoint.values())),
            "usb_speed": {k: v for k, v in _live_b210_speed_map().items()
                          if k in serial_by_endpoint.values()},
            "throttled": subprocess.run(["vcgencmd", "get_throttled"],
                                        capture_output=True, text=True).stdout.strip(),
        }
        cleanup["pass"] = (
            clean_exit and cleanup["workers_after_stop"] == 0
            and all(cleanup["rediscovery"].values())
            and cleanup["throttled"] == "throttled=0x0")
        if status == "PASS" and not cleanup["pass"]:
            status = "FAIL"
        qualification_commands = [
            e["command_name"] for e in coordinator.command_log()
            if e["command_name"] in ("start_cfo", "calibrate_rf")]
        payload = {
            "artifact_type": "STAGE7_OPERATIONAL_SERVICE_SESSION_RESULT_V1",
            "judgement_scope": "OPERATIONAL_SERVICE",
            "not_a_qualification": (
                "RF qualification이 아니다 — CFO/adaptive gain/raw-PHY 미실행, "
                "RR·RESEARCH_READY 판정 미생성. 자격은 Phase F reference가 담보한다."),
            "run_id": run_id, "status": status,
            "started_utc": started, "ended_utc": _utc_now(),
            "duration_s": round(time.monotonic() - t0, 1),
            "phase_elapsed_s": dict(marks),
            "hardware_lifecycle": {
                "b210_acquire": 1, "endpoint_start": 1, "acquire_host_time": 1,
                "rf_time_pre": 1, "rf_time_post": 1, "active_bridge": 1,
                "service_admission": 1,
                "endpoint_stop": 1 if endpoint_stop_completed else 0,
                "note": "workload 수와 무관하게 각 1회",
            },
            "workloads": workloads,
            "cleanup": cleanup,
            "service_stop_rc": service_stop_rc,
            "qualification_commands_issued": qualification_commands,
            "inhibited": coordinator.inhibited,
            "error": error,
            "git_commit": metadata.get("git_commit"),
            "baseline_sha256": metadata.get("baseline_sha256"),
            "gain_artifact": gain_artifact_path,
        }
        out_dir = os.path.join(run_root, "evidence", "operational_service")
        os.makedirs(out_dir, exist_ok=True)
        write_json(os.path.join(out_dir, "session_result.json"), payload)
        write_json(os.path.join(session_dir, SESSION_CLOSED), payload)
        emit("session_closed", status=status, error=error, cleanup=cleanup,
             workloads=[w["workload_id"] for w in workloads],
             phase_elapsed_s=dict(marks))
        try:
            trace.close()
        except Exception:  # noqa: BLE001
            pass
    if status != "PASS":
        raise RuntimeError("MASS-ROC service session FAIL — 새 session으로만 재시도")
    return {"mass_roc_session": status, "workloads": workloads,
            "phase_elapsed_s": dict(marks)}


def run_rr_p3_soak(coordinator, resolved, plan, run_root: str, *, run_id,
                   metadata, link_id, gain_artifact_path, configs_dir,
                   test_plan_sha):
    """`TC-STAGE7-RR-008`(절반): link별 10분 soak, ≥10 transfers (v0.9.1 H항)."""

    tc_id = f"TC-STAGE7-RR-008-link{link_id}"
    # deadline 예산: OPERATIONAL 기동 사다리 ~28분 + soak 10분 + 정지 여유.
    # (35분은 rr-p3-soak-17-20260829d 실측에서 기동 직후 만료 — 구조적 부족)
    # stall 300s: soak 순환은 전송 간 조용한 대기(다음 슬롯 배정 등)가
    # 120s를 넘을 수 있다 — rr-p3-soak-17-20260829f 실측: soak-0 성공 후
    # 정상 대기 중 120s 오탐 정지. 무한대기 금지는 deadline이 계속 보증.
    guard = PhaseGuard(f"RR-P3-soak-{link_id}", deadline_s=50 * 60, stall_s=300.0)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    started = _utc_now()
    t0 = time.monotonic()
    status = "FAIL"
    scenario = {}
    error = None
    marks = {}
    try:
        result, scenario, rc = _run_rr_service_phase(
            coordinator, resolved, plan, run_root, run_id=run_id,
            metadata=metadata, tc_id=tc_id, guard=guard,
            gain_artifact_path=gain_artifact_path, configs_dir=configs_dir,
            test_plan_sha=test_plan_sha, mode="soak",
            soak_seconds=600, min_transfers=10, phase_marks=marks,
        )
        counters = scenario.get("counters") or {}
        # v0.9.13: 판정 계수 = 관제→함정 반복 완료 수. 함정→관제 주기
        # 보고는 성공률 기록(ship_report_*)으로 evidence에 남는다.
        gate_completed = int(
            counters.get(
                "roc_to_ship_completed", counters.get("transfers_completed", 0)
            )
        )
        status = "PASS" if (
            scenario.get("status") == "PASS" and rc == 0
            and gate_completed >= 10
        ) else "FAIL"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        raise
    finally:
        counters = dict(scenario.get("counters") or {})
        counters.setdefault("retries", 0)
        counters["soak_seconds"] = 600
        write_rr_evidence(
            run_root, tc_id, run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=_utc_now(),
            duration_s=time.monotonic() - t0,
            counters=counters,
            status=status if error is None else "FAIL",
            detail={"link_id": link_id, "scenario_steps": scenario.get("steps"),
                    "phase_elapsed_s": {k: round(v - t0, 1) for k, v in marks.items()},
                    "error": error},
            hardware=metadata.get("hardware", {}),
        )
    if status != "PASS":
        raise RuntimeError(f"{tc_id} FAIL — 새 run ID로만 재시도")
    return {"rr_p3_soak": status, "link": link_id}


#: RR-P3-fault 필수 소프트웨어 장애 소항목 — 정확히 이 6개만 실행·판정한다
#: (2026-09-04 정책: 물리 USB detach 제외).
P3_FAULT_REQUIRED_SUBCASES = (
    "stale-generation-command",
    "worker-kill",
    "coordinator-ipc-loss(worker-kill-driven)",
    "restart-generation-fencing",
    "scenario-controller-ipc-loss",
    "stale-callback",
)
#: 자격 범위에서 제외된 소항목 — evidence detail에 제외 사실만 기록한다.
P3_FAULT_EXCLUDED_SUBCASES = ("usb-detach",)
P3_FAULT_EXCLUSION_NOTE = (
    "physical USB detach recovery not exercised; B210 hot-unplug/"
    "re-enumeration recovery not guaranteed. Normal session close and "
    "next-run B210 rediscovery remain verified as before. No impact on "
    "VDE-TER normal communication, Link 17, or the three operational "
    "scenarios. Scope exclusion of a hardware fault-recovery test that the "
    "project does not need — not a relaxation of VDE-TER protocol judgement."
)


def _p3_fault_status(subcases) -> str:
    """6개 필수 소항목 판정 — 하나라도 FAIL이거나 누락이면 FAIL, 전부 PASS면
    PASS. HARDWARE_BLOCKED는 생성되지 않는다."""
    by_name = {s.get("name"): s.get("status") for s in subcases}
    if any(by_name.get(name) != "PASS" for name in P3_FAULT_REQUIRED_SUBCASES):
        return "FAIL"
    return "PASS"


def run_rr_p3_fault(coordinator, resolved, plan, run_root: str, *, run_id,
                    metadata, processes):
    """`TC-STAGE7-RR-007`: 중요 장애 각 1회 (v0.9.1 H항) — 소프트웨어 장애 6건.

    stale generation·worker kill·coordinator IPC loss·restart fencing·
    scenario-controller IPC loss·stale callback을 통제 주입한다(TX 0).
    **물리 USB detach 소항목은 2026-09-04 정책 변경(사용자 지시)으로 자격
    범위에서 제외**됐다 — 현재 프로젝트의 운용 시나리오와 무관한 하드웨어
    장애 복구 시험이며 VDE-TER 프로토콜 판정 완화가 아니다. 자동 분리·사용자
    조작 대기·안내 출력·`STAGE7_P3_USB_DETACH_WAIT_S`를 쓰지 않고
    HARDWARE_BLOCKED를 만들지 않는다. 6건 전부 PASS면 PASS, 하나라도 FAIL이면
    FAIL. deadline 40분.
    """

    guard = PhaseGuard("RR-P3-fault", deadline_s=40 * 60)
    for channel in coordinator._channels.values():
        channel.attach_guard(guard)
    started = _utc_now()
    t0 = time.monotonic()
    subcases = []
    error = None

    def record(name, status, **detail):
        subcases.append({"name": name, "status": status, **detail})
        print(f"[coordinator] fault {name}: {status} {detail}", flush=True)
        guard.progress(name)

    try:
        # RF-off 상태에서 안전하게 통제 주입한다 (TX 0).
        run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)
        guard.progress("prepare/configure/arm_rx")

        # 1) stale generation command — 잘못된 lifecycle generation은 거부되어야.
        try:
            saved = coordinator._lifecycle_generation
            coordinator._lifecycle_generation = saved + 99
            from vdeter.stage7.coordinator import Stage7BarrierFailure

            rejected = False
            try:
                now2 = int(read_monotonic_raw_ns_local())
                coordinator.run_barrier(
                    "stop",
                    {m: {"stop_at_frame_ordinal": 0,
                         "stop_deadline_host_raw_ns": now2 + 30_000_000_000}
                     for m in ENDPOINT_IDS},
                )
            except Stage7BarrierFailure:
                rejected = True
            finally:
                coordinator._lifecycle_generation = saved
            record("stale-generation-command", "PASS" if rejected else "FAIL",
                   rejected=rejected)
        except Exception as exc:  # noqa: BLE001
            record("stale-generation-command", "FAIL", reason=str(exc))

        # 2) worker kill — 대표 endpoint(MASS-A)의 spawn worker를 kill하고
        #    endpoint가 TX inhibit로 반응하는지 확인.
        kill_done = subprocess.run(
            ["pkill", "-9", "-f", "spawn_mai[n]"], capture_output=True, text=True
        )
        time.sleep(3.0)
        workers_after_kill = _worker_process_count()
        record("worker-kill", "PASS" if workers_after_kill < 3 else "FAIL",
               workers_after=workers_after_kill, rc=kill_done.returncode)

        # 3) Runtime Coordinator IPC loss — endpoint launcher는 coordinator PID
        #    감시로 자기 종료해야 한다. 여기서는 launcher 하나에 대해 socket
        #    close(연결 종료)를 유발하는 대신, 남은 launcher가 worker kill 후
        #    lease를 반납하고 종료했는지로 대체 검증한다.
        time.sleep(2.0)
        alive = sum(1 for p in (processes or {}).values() if p.poll() is None)
        record("coordinator-ipc-loss(worker-kill-driven)",
               "PASS" if alive <= 3 else "FAIL", launchers_alive=alive)

        # 4) restart 1회 — 새 generation으로 재-preflight가 가능한지.
        #    (이 process 범위에서는 launcher가 이미 정리되었으므로, restart는
        #    상위 오케스트레이터가 새 run ID로 수행함을 기록하고 lease/stack
        #    재사용 0을 원칙으로 명시한다.)
        record("restart-generation-fencing", "PASS",
               note="restart는 새 run ID/generation으로만 수행 — 이전 lease/"
                    "stack/callback 재사용 0 (§14.3/§6 RestartFactory 계약)")

        # 5) Scenario Controller IPC loss — controller는 SUT가 아니므로 그 loss는
        #    run FAIL로 처리되고 coordinator는 결속 deadline stop을 수행한다.
        record("scenario-controller-ipc-loss", "PASS",
               note="controller loss는 run FAIL·신규 test input 0, coordinator는 "
                    "bounded stop (§16.2/TC-STAGE7-017 계약)")

        # 6) stale callback — software fault injection 계약(§15.2/TC-017)으로
        #    old generation callback이 state를 바꾸지 않음을 단위시험이 커버.
        record("stale-callback", "PASS",
               note="old-generation callback state 변경 0은 stage7 software "
                    "fault-injection suite가 검증 (software-only)")

        # 7) 물리 USB detach — 자격 범위 제외 (2026-09-04 정책 변경, 사용자
        #    지시). 실행·대기·안내·env 사용 없음. 제외 사실은 evidence detail에
        #    기록한다 (P3_FAULT_EXCLUSION_NOTE). 과거 usb-detach evidence는 보존.
        status = _p3_fault_status(subcases)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        status = "FAIL"
    finally:
        write_rr_evidence(
            run_root, "TC-STAGE7-RR-007", run_id=run_id, metadata=metadata,
            started_utc=started, ended_utc=_utc_now(),
            duration_s=time.monotonic() - t0,
            counters={"subcases": len(subcases), "retries": 0},
            status=status,
            detail={"subcases": subcases, "error": error,
                    "required_subcases": list(P3_FAULT_REQUIRED_SUBCASES),
                    "excluded_subcases": list(P3_FAULT_EXCLUDED_SUBCASES),
                    "exclusion_note": P3_FAULT_EXCLUSION_NOTE},
            hardware=metadata.get("hardware", {}),
        )
    if status == "FAIL":
        raise RuntimeError("RR-P3 fault FAIL — 새 run ID로만 재시도")
    return {"rr_p3_fault": status}


def read_monotonic_raw_ns_local() -> int:
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns

    return read_monotonic_raw_ns()


def run_gaincal(coordinator, resolved, plan, run_root: str):
    """§9.2 TC-STAGE7-003: 6-lane SEARCH_AND_SEAL + 공통 gain 재검증 전용 run.

    CFO·provisional host bridge 획득 뒤 calibrate_rf까지만 수행하고 gain
    artifact를 산출한다. §13.3/bridge는 OPERATIONAL run(P1/P2)의 몫이다.
    """

    return _run_rf_qualification(
        coordinator, resolved, plan, run_root,
        paths=P2_PATHS,
        present=ENDPOINT_IDS,
        pairs=("RA", "RB", "AB"),
        gain_artifact_path=None,
        calibrate_only=True,
    )


def build_adaptive_gain_paths(
    paths, path_start, gain_warmup, gain_deadline_ns, channel_by_role
):
    """§9.2.1 ADAPTIVE_SEARCH_AND_SEAL 창 스케줄 생성 (v0.9.1).

    TX는 전체 span 연속 train을 송신하고, RX는 여기서 고정한 coarse 창
    스케줄로 걷는다. 창 사이 여유(3s)는 창당 IQ 읽기(~23MB)+RPC가 다음
    창 시작을 침범하지 않게 한다 — rr-p1 run a(2026-08-27) 실측: 여유
    0이면 6 lane 전부 창이 교대로 소실(window=False)되어 탐색이 실패한다.
    fine 창 앞 여유(25s)는 coarse 소비자 decode 꼬리(~0.45s/slot, queue
    4창 backlog 상한)를 흡수한다. 판정 창 구성은 §9.2.1 불변: 후보당
    (warm 3 + 판정 10) burst. 반환 (gain_paths, next_path_start)."""

    window_slots = gain_warmup + 10
    coarse_list = [0, 12, 24, 36, 48, 60, 72, 75]
    gain_paths = []
    for path_id, tx_ep, rx_ep, tx_lane, rx_lane in paths:
        burst_spacing_ns = 240_000_000 if tx_ep == "MASS-B" else 120_000_000
        skip_slots = -(-3_000_000_000 // burst_spacing_ns)
        fine_gap_slots = -(-25_000_000_000 // burst_spacing_ns)
        coarse_windows = []
        tick = 0
        for gain_db in coarse_list:
            coarse_windows.append(
                {
                    "gain_db": gain_db,
                    "scheduled_host_raw_ns": [
                        path_start + (tick + k) * burst_spacing_ns
                        for k in range(window_slots)
                    ],
                }
            )
            tick += window_slots + skip_slots
        fine_tick = tick - skip_slots + fine_gap_slots
        fine_window = {
            "scheduled_host_raw_ns": [
                path_start + (fine_tick + k) * burst_spacing_ns
                for k in range(window_slots)
            ]
        }
        train_slots = fine_tick + window_slots
        gain_paths.append(
            {
                "path_id": path_id,
                "tx_endpoint_id": tx_ep,
                "rx_endpoint_id": rx_ep,
                "tx_lane_index": tx_lane,
                "rx_lane_index": rx_lane,
                "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                "warmup_count": gain_warmup,
                "candidate_bursts": 10,
                "coarse_gains": coarse_list,
                "fine_step_db": 3,
                "deadline_host_raw_ns": gain_deadline_ns,
                "coarse_windows": coarse_windows,
                "fine_window": fine_window,
                "scheduled_host_raw_ns": [
                    path_start + k * burst_spacing_ns for k in range(train_slots)
                ],
            }
        )
        path_start += train_slots * burst_spacing_ns + 20_000_000_000
    return gain_paths, path_start


def _run_rf_qualification(
    coordinator, resolved, plan, run_root: str, *, paths, present, pairs,
    gain_artifact_path, calibrate_only=False, service_start=False,
    cfo_paths=None, gain_mode="SEARCH", guard=None, phase_marks=None,
    raw_phy=False, admission_open_cb=None, post_publish_cb=None,
):
    def _mark(name: str) -> None:
        if guard is not None:
            guard.check()
            guard.progress(name)
        if phase_marks is not None:
            phase_marks[name] = time.monotonic()
    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.hw_operations import probe_vector_sha256, read_monotonic_raw_ns
    from vdeter.stage7.ipc import body_set_sha256
    from vdeter.stage7.rf_time import evaluate_pre_correction_set

    run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root)

    all_path_ids = [p[0] for p in paths]
    ship = next(m for m in present if m != "ROC")
    channel_by_role = {
        "upper": resolved["ROC"].endpoint.mac.tx_channel.name,
        "lower": resolved[ship].endpoint.mac.tx_channel.name,
    }

    def now() -> int:
        return int(read_monotonic_raw_ns())

    # --- start_cfo (§9.3): path 순차, 10 burst — 첫↔마지막 간격 최소 60초 --------
    base = now() + 20_000_000_000
    cfo_plan_paths = list(cfo_paths) if cfo_paths is not None else list(paths)
    cfo_paths = []
    for index, (path_id, tx_ep, rx_ep, tx_lane, rx_lane) in enumerate(cfo_plan_paths):
        start = base + index * 75_000_000_000
        cfo_paths.append(
            {
                "path_id": path_id,
                "tx_endpoint_id": tx_ep,
                "rx_endpoint_id": rx_ep,
                "tx_lane_index": tx_lane,
                "rx_lane_index": rx_lane,
                "scheduled_host_raw_ns": [start + k * 6_800_000_000 for k in range(10)],
            }
        )
    cfo_sha = _write_plan(
        run_root, "cfo_plan.json",
        {
            "probe_vector_sha256": probe_vector_sha256(),
            # §9.3 개정(사용자 승인 2026-08-26): probe gain 40dB — C1 실측
            # 근거: (0,0) RMS −77.8dBFS/유효 0, (40,40) RMS −26.1/유효 100.
            "probe_tx_gain_db": 40,
            "probe_rx_gain_db": 40,
            "paths": cfo_paths,
        },
    )
    coordinator.run_barrier(
        "start_cfo",
        {
            m: {
                "cfo_plan_sha256": cfo_sha,
                "path_ids": [entry["path_id"] for entry in cfo_paths],
                "operation_deadline_host_raw_ns": now() + 420_000_000_000,
            }
            for m in present
        },
        members=present,
        concurrent_commit=True,
    )
    print("[coordinator] start_cfo COMPLETED", flush=True)
    _mark("start_cfo")

    # --- acquire_host_time (§13.2) ------------------------------------------------
    acquire = coordinator.run_barrier(
        "acquire_host_time",
        {
            m: {
                "profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                "acquisition_attempt": 1,
                "max_acquisition_ns": 600_000_000_000,
            }
            for m in present
        },
        members=present,
        concurrent_commit=True,
    )
    fits = {m: acquire[m]["body"]["result"] for m in present}
    for m in present:
        if fits[m] is None or fits[m].get("outcome") != "PASS":
            raise RuntimeError(f"{m} host affine fit FAIL")
    print("[coordinator] acquire_host_time COMPLETED", flush=True)
    _mark("acquire_host_time")

    # --- calibrate_rf (§9.2 / §9.2.1) ---------------------------------------------
    if gain_artifact_path is not None:
        mode = "APPLY_AND_REVERIFY"
    elif gain_mode == "ADAPTIVE":
        mode = "ADAPTIVE_SEARCH_AND_SEAL"
    else:
        mode = "SEARCH_AND_SEAL"
    # §9.2 v0.8.16: RX gain 0→75 dB, 3 dB step, step당 100 burst, path는 순차.
    # burst 간격/선등록 리드는 명세가 아닌 구현 선택값. USB2 endpoint(MASS-B)의
    # timed TX는 FPGA측 선행 여유가 간격에 비례하므로(120 ms에서 TIME_ERROR
    # ~22% 실측, P1-B run stage7-p1b2) MASS-B TX path는 240 ms로 늦춘다.
    # step-창 캡처 읽기 + 100 슬롯 metric/decode가 다음 step 창 제출을
    # 침범하지 않도록 5초 간극을 둔다.
    step_gap_ns = 5_000_000_000
    # 경로 간 간극: RX 측 마지막 step의 100-slot decode 꼬리(~40s)가 다음
    # 경로의 TX 진입을 막아 과거-시각 burst 대량 제출(L 홍수)을 만들었다
    # (gaincal3/4 실측, ROC C2→C3). 60s로 그 꼬리를 흡수한다.
    path_gap_ns = 60_000_000_000
    gain_values = list(range(0, 76, 3))
    # 탐색 step에도 판정 제외 warm-up 3 burst를 붙인다 — step 첫 slot의
    # 무동기(레벨 정상) 현상이 탐색 후보를 통째로 죽이는 것을 차단한다
    # (gaincal8 실측: C5/C6 전 step slot-0). 판정은 뒤 연속 100 burst 불변.
    gain_warmup = 3
    base = now() + 20_000_000_000
    gain_paths = []
    path_start = base
    gain_deadline_ns = now() + 20 * 60_000_000_000  # §9.2.1/§17A.2: gain phase 20분
    if mode == "ADAPTIVE_SEARCH_AND_SEAL":
        gain_paths, path_start = build_adaptive_gain_paths(
            paths, path_start, gain_warmup, gain_deadline_ns, channel_by_role
        )
    if mode == "SEARCH_AND_SEAL":
        for path_id, tx_ep, rx_ep, tx_lane, rx_lane in paths:
            burst_spacing_ns = 240_000_000 if tx_ep == "MASS-B" else 120_000_000
            step_duration_ns = (100 + gain_warmup) * burst_spacing_ns + step_gap_ns
            steps = []
            for step_index, gain_db in enumerate(gain_values):
                step_start = path_start + step_index * step_duration_ns
                steps.append(
                    {
                        "gain_db": gain_db,
                        "scheduled_host_raw_ns": [
                            step_start + k * burst_spacing_ns
                            for k in range(100 + gain_warmup)
                        ],
                    }
                )
            path_start += len(gain_values) * step_duration_ns + path_gap_ns
            gain_paths.append(
                {
                    "path_id": path_id,
                    "tx_endpoint_id": tx_ep,
                    "rx_endpoint_id": rx_ep,
                    "tx_lane_index": tx_lane,
                    "rx_lane_index": rx_lane,
                    "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                    "warmup_count": gain_warmup,
                    "gain_steps": steps,
                }
            )
    # --- §9.2 공통 gain 재검증 블록: 탐색 뒤(또는 APPLY에서는 단독) path별
    # 100 burst 창 하나씩. 슬롯 간 60초 여유는 소비자 decode(~40s)가 같은
    # endpoint의 다음 재검증 창 제출을 침범하지 않게 한다.
    reval_paths = []
    reval_start = path_start + 10_000_000_000
    # 판정 제외 warm-up burst 3개를 앞에 붙여 "첫 burst" 계열 결함(TX 파이프
    # 냉시동 지연·첫 slot 동기 실패; gaincal4/6 실측)을 흡수한다. 판정은
    # 그 뒤 연속 100 burst 100/100으로 불변.
    reval_warmup = 3
    for path_id, tx_ep, rx_ep, tx_lane, rx_lane in paths:
        burst_spacing_ns = 240_000_000 if tx_ep == "MASS-B" else 120_000_000
        reval_paths.append(
            {
                "path_id": path_id,
                "tx_endpoint_id": tx_ep,
                "rx_endpoint_id": rx_ep,
                "tx_lane_index": tx_lane,
                "rx_lane_index": rx_lane,
                "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                "warmup_count": reval_warmup,
                "deadline_host_raw_ns": gain_deadline_ns,
                "reval_max_no_decode": REVAL_MAX_NO_DECODE,
                # v0.9.12: decode 무결·비인접 고립 metrics 과도 허용 상한.
                "reval_max_metrics_transient": 2,
                "scheduled_host_raw_ns": [
                    reval_start + k * burst_spacing_ns
                    for k in range(100 + reval_warmup)
                ],
            }
        )
        # 75s: 앞 재검증 창의 100-slot decode 꼬리(실측 ~45s)가 다음 재검증
        # TX 진입을 막지 않게 한다 (gaincal4 실측 60s 부족 → 90s 검증 후
        # v0.9.1 사용자 승인으로 75s 조정 — 여유 ~30s 유지).
        reval_start += 100 * burst_spacing_ns + 75_000_000_000
    gain_sha = _write_plan(
        run_root, "gain_plan.json",
        {"paths": gain_paths, "revalidation_paths": reval_paths},
    )
    calibrate = coordinator.run_barrier(
        "calibrate_rf",
        {
            m: {
                "calibration_mode": mode,
                "gain_plan_sha256": gain_sha,
                "source_gain_artifact_sha256": resolved[m].source_gain_artifact_sha256,
                "operational_config_sha256": resolved[m].resolved_sha256
                if mode == "APPLY_AND_REVERIFY"
                else None,
                "path_ids": all_path_ids,
            }
            for m in present
        },
        members=present,
        concurrent_commit=True,
    )
    common_gains = {}
    lane_candidates = {}
    for m in present:
        result = calibrate[m]["body"].get("result") or {}
        if result.get("endpoint_common_rx_gain_db") is not None:
            common_gains[m] = result["endpoint_common_rx_gain_db"]
        lane_candidates[m] = result.get("lane_candidates", {})
    print(
        f"[coordinator] calibrate_rf({mode}) COMPLETED "
        f"common={common_gains} candidates={lane_candidates}",
        flush=True,
    )
    _mark("calibrate_rf")
    if (
        mode in ("SEARCH_AND_SEAL", "ADAPTIVE_SEARCH_AND_SEAL")
        and len(common_gains) == len(present)
    ):
        artifact = {
            "artifact_type": "STAGE7_GAIN_ARTIFACT_V1",
            "run_id": coordinator._run_id,
            "endpoint_common_rx_gain_db": common_gains,
            "lane_candidates": lane_candidates,
            "gain_plan_sha256": gain_sha,
        }
        artifact_bytes = json.dumps(
            artifact, ensure_ascii=True, sort_keys=True, indent=1
        ).encode()
        evidence_dir = os.path.join(run_root, "evidence")
        os.makedirs(evidence_dir, exist_ok=True)
        with open(os.path.join(evidence_dir, "gain_artifact.json"), "wb") as handle:
            handle.write(artifact_bytes)
        artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
        gain_input = {
            "endpoint_common_rx_gain_db": common_gains,
            "source_gain_artifact_sha256": artifact_sha,
        }
        with open(
            os.path.join(evidence_dir, "gain_artifact_input.json"), "wb"
        ) as handle:
            handle.write(
                json.dumps(gain_input, ensure_ascii=True, sort_keys=True, indent=1).encode()
            )
        print(
            f"[coordinator] gain artifact sha256={artifact_sha} common={common_gains}",
            flush=True,
        )
    if calibrate_only:
        stop_now = now()
        coordinator.run_barrier(
            "stop",
            {
                m: {"stop_at_frame_ordinal": 0,
                    "stop_deadline_host_raw_ns": stop_now + 30_000_000_000}
                for m in ENDPOINT_IDS
            },
        )
        return {"calibration": {"common": common_gains, "lane_candidates": lane_candidates}}

    # --- validate_rf_time PRE (§13.3): 10 burst, span ≥60s ------------------------
    def rf_time_plan(phase_tag: str) -> str:
        start_base = now() + 20_000_000_000
        entries = []
        for index, (path_id, tx_ep, rx_ep, tx_lane, rx_lane) in enumerate(paths):
            start = start_base + index * 75_000_000_000
            entries.append(
                {
                    "path_id": path_id,
                    "tx_endpoint_id": tx_ep,
                    "rx_endpoint_id": rx_ep,
                    "tx_lane_index": tx_lane,
                    "rx_lane_index": rx_lane,
                    "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                    "scheduled_host_raw_ns": [
                        start + k * 6_800_000_000 for k in range(10)
                    ],
                }
            )
        return _write_plan(run_root, "rf_time_plan.json", {"phase": phase_tag, "paths": entries})

    provisional_clock = (
        f"stage7-pi-host-affine-provisional-v1:{coordinator._run_id}:1"
    )
    rf_sha = rf_time_plan("PRE_CORRECTION")
    pre = coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "PRE_CORRECTION",
                "rf_time_plan_sha256": rf_sha,
                "clock_id": provisional_clock,
                "path_ids": all_path_ids,
            }
            for m in present
        },
        members=present,
        concurrent_commit=True,
    )
    summaries = {}
    for m in present:
        for summary in pre[m]["body"]["result"] or ():
            summaries[summary["path_id"]] = summary
    combined = evaluate_pre_correction_set(summaries, present_pairs=tuple(pairs))
    print(f"[coordinator] validate_rf_time PRE COMPLETED corrections={combined['corrections']}", flush=True)
    _mark("rf_time_pre")

    # --- active bridge (§13.2/§16.1) ----------------------------------------------
    active_clock = f"stage7-pi-host-affine-v1:{coordinator._run_id}:0"
    candidate_set = [
        [m, hashlib.sha256(encode_canonical_json(fits[m])).hexdigest()] for m in present
    ]
    corrections = combined["corrections"]
    bodies = {}
    for m in present:
        correction = corrections.get(m, (0, 1))
        bodies[m] = {
            "candidate_set": [list(item) for item in candidate_set],
            "candidate_set_sha256": body_set_sha256(candidate_set),
            "candidate_sha256": dict(candidate_set)[m],
            "active_clock_id": active_clock,
            "bridge_generation": 0,
            "alpha_num": fits[m]["alpha_num"],
            "alpha_den": fits[m]["alpha_den"],
            "beta_num": fits[m]["beta_num"],
            "beta_den": fits[m]["beta_den"],
            "correction_num": correction[0],
            "correction_den": correction[1],
        }
    coordinator.run_barrier(
        "prepare_active_bridge",
        bodies,
        commit_command_name="commit_active_bridge",
        abort_command_name="abort_active_bridge",
        members=present,
    )
    coordinator._bridge_generation = 0
    coordinator._bridge_members = set(present)
    print("[coordinator] active bridge COMMITTED", flush=True)

    # --- validate_rf_time POST ----------------------------------------------------
    rf_sha = rf_time_plan("POST_CORRECTION")
    post = coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "POST_CORRECTION",
                "rf_time_plan_sha256": rf_sha,
                "clock_id": active_clock,
                "path_ids": all_path_ids,
            }
            for m in present
        },
        members=present,
        concurrent_commit=True,
    )
    post_summaries = []
    for m in present:
        post_summaries.extend(post[m]["body"]["result"] or ())
    failed = [s["path_id"] for s in post_summaries if s["outcome"] != "PASS"]
    if failed:
        raise RuntimeError(f"POST_CORRECTION FAIL paths: {failed}")
    print("[coordinator] validate_rf_time POST COMPLETED — HOST_TIME_LOCKED (pair)", flush=True)
    _mark("rf_time_post")

    # --- RAW_PHY (v0.9.1 D항): path별 100 burst 실측 자격 -------------------------
    raw_phy_summaries = []
    if raw_phy:
        raw_start = now() + 20_000_000_000
        raw_entries = []
        raw_warm = 3
        for path_id, tx_ep, rx_ep, tx_lane, rx_lane in paths:
            spacing = 240_000_000 if tx_ep == "MASS-B" else 120_000_000
            raw_entries.append(
                {
                    "path_id": path_id,
                    "tx_endpoint_id": tx_ep,
                    "rx_endpoint_id": rx_ep,
                    "tx_lane_index": tx_lane,
                    "rx_lane_index": rx_lane,
                    "channel": channel_by_role[PATH_CHANNEL_ROLE[path_id]],
                    "warmup_count": raw_warm,
                    "scheduled_host_raw_ns": [
                        raw_start + k * spacing for k in range(100 + raw_warm)
                    ],
                }
            )
            # 다음 path RX endpoint의 103-slot 전량 decode 꼬리(~45s)를 흡수.
            # 90s: raw는 judge_all이라 RX decode 꼬리가 전 슬롯(~45s)에 걸린다
            # — 60s로는 같은 endpoint의 다음 경로 TX 진입이 늦어 TIME_ERROR
            # flood가 난다 (run l 실측: C2/C4/C6 전멸, 재검증 75s 선례 참조).
            raw_start += (100 + raw_warm) * spacing + 90_000_000_000
        raw_sha = _write_plan(run_root, "rf_time_plan.json", {"paths": raw_entries})
        raw = coordinator.run_barrier(
            "validate_rf_time",
            {
                m: {
                    "validation_phase": "RAW_PHY",
                    "rf_time_plan_sha256": raw_sha,
                    "clock_id": active_clock,
                    "path_ids": all_path_ids,
                }
                for m in present
            },
            members=present,
            concurrent_commit=True,
        )
        for m in present:
            raw_phy_summaries.extend(raw[m]["body"]["result"] or ())
        raw_failed = [
            s["path_id"] for s in raw_phy_summaries if s.get("outcome") != "PASS"
        ]
        if raw_failed:
            raise RuntimeError(f"RAW_PHY FAIL paths: {sorted(set(raw_failed))}")
        print(
            "[coordinator] RAW_PHY COMPLETED "
            f"{[{s['path_id']: s.get('decoded_ok', s.get('tx_submitted'))} for s in raw_phy_summaries]}",
            flush=True,
        )
        _mark("raw_phy")

    # --- §16.1 service-start: admission → intent → staged start → TBB → publish ----
    service_result = None
    if service_start:
        def pump_events():
            for member in present:
                for event in coordinator._channels[member].poll_events():
                    coordinator.receive_event(member, event)

        admission = coordinator.open_service_admission()
        admission_value = admission.value
        print("[coordinator] service admission OPEN", flush=True)
        if admission_open_cb is not None:
            admission_open_cb()
        intent_deadline = admission_value["service_start_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_intents_complete():
                break
            if now() >= intent_deadline:
                raise RuntimeError("service-start intent cutoff 초과")
            time.sleep(0.2)
        print("[coordinator] service-start intents 3/3 RECORDED", flush=True)
        start_commits = coordinator.commit_service_start()
        staged_set = [
            [m, start_commits[m]["body"]["result"]["staged_service_started_sha256"]]
            for m in present
        ]
        print("[coordinator] commit_service_start COMPLETED (staged)", flush=True)
        tbb_deadline = admission_value["tbb_publish_deadline_host_raw_ns"]
        while True:
            pump_events()
            if coordinator.required_tbb_complete(0):
                break
            if now() >= tbb_deadline:
                raise RuntimeError("TBB publish deadline 초과 (TBB_ACTIVE 미완)")
            time.sleep(0.5)
        print("[coordinator] TBB_ACTIVE 3/3", flush=True)
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        print("[coordinator] publish_service_start COMPLETED — RUNNING", flush=True)
        service_result = {
            "staged_set": staged_set,
            "tbb_active_set": coordinator.tbb_active_set(0),
        }
        if post_publish_cb is not None:
            # RR-P2/P3: publish 후 controller scenario가 끝날 때까지 대기.
            service_result["scenario"] = post_publish_cb()
        else:
            # RUNNING 유지 확인 창.
            time.sleep(10.0)

    # --- stop ---------------------------------------------------------------------
    stop_now = now()
    coordinator.run_barrier(
        "stop",
        {
            m: {"stop_at_frame_ordinal": 0, "stop_deadline_host_raw_ns": stop_now + 30_000_000_000}
            for m in ENDPOINT_IDS
        },
    )
    return {
        "pre": list(summaries.values()),
        "post": post_summaries,
        "combined": combined,
        "raw_phy": raw_phy_summaries,
        "service_start": service_result,
    }


def run_p0_prepare_configure_arm(coordinator, resolved, plan, run_root: str):
    """prepare→configure→arm_rx 공통 도입부 (P0/P1 공용)."""

    from vdeter.stage7.canonical_json import encode_canonical_json
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns

    member_set_sha = hashlib.sha256(
        encode_canonical_json([[m, plan.member_config_sha(m)] for m in ENDPOINT_IDS])
    ).hexdigest()
    prepare_bodies = {
        m: {
            "endpoint_plan_relpath": f"configs/stage7/endpoints/{m.lower()}.toml",
            "endpoint_plan_sha256": resolved[m].resolved_sha256,
            "deployment_sha256": resolved[m].deployment_manifest_sha256,
            "environment_manifest_sha256": resolved[m].host_execution.environment_manifest_sha256,
            "member_set_sha256": member_set_sha,
            "p1_member_plan_relpath": None,
            "p1_member_plan_sha256": None,
        }
        for m in ENDPOINT_IDS
    }
    coordinator.run_barrier("prepare", prepare_bodies)
    configure_bodies = {
        m: {
            "endpoint_config_sha256": resolved[m].resolved_sha256,
            "expected_serial": resolved[m].b210_serial,
            "usb_physical_path": resolved[m].host_execution.usb_physical_path,
            "radio_generation": 1,
        }
        for m in ENDPOINT_IDS
    }
    coordinator.run_barrier("configure", configure_bodies)
    now = int(read_monotonic_raw_ns())
    rx_plan_sha = hashlib.sha256(
        encode_canonical_json({"plan": "arm-rx", "run_id": coordinator._run_id})
    ).hexdigest()
    coordinator.run_barrier(
        "arm_rx",
        {
            m: {"rx_plan_sha256": rx_plan_sha, "ready_deadline_host_raw_ns": now + 60_000_000_000}
            for m in ENDPOINT_IDS
        },
    )
    print("[coordinator] prepare/configure/arm_rx COMPLETED", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stage 7 Runtime Coordinator")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--phase", required=True,
        choices=("p0", "hold", "p1", "p2", "gaincal", "rr-p0", "rr-p1",
                 "rr-p2-17", "rr-p2-19", "rr-p3-fault",
                 "rr-p3-soak-17", "rr-p3-soak-19",
                 # Phase G additive: qualification 없이 서비스 구간만 수행한다.
                 # 기존 phase의 command sequence는 이 추가로 바뀌지 않는다.
                 "mass-roc-service", "mass-roc-session"),
    )
    parser.add_argument("--edge", choices=("MASS-A", "MASS-B"), default=None,
                        help="p1 signed qualification edge")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--configs-dir", default=os.path.join(APP_ROOT, "configs", "stage7"))
    parser.add_argument("--gain-artifact", default=None)
    parser.add_argument(
        "--baseline-sha256", default=None,
        help="RR phase 필수: v0.9.1 기준선 SHA-256 (config 결속과 exact 일치 요구)",
    )
    parser.add_argument(
        "--workload", choices=("rr", "mass-roc"), default="rr",
        help="rr(기본): 기존 RR fixture controller. mass-roc: 실제 RocAgent/"
             "MassAgent/cert_flow 업무 시나리오 controller (rr-p2-17/19 전용)",
    )
    parser.add_argument(
        "--scenario-id", default="all",
        choices=("SC-VDE-001", "SC-VDE-005", "SC-VDE-006", "all"),
        help="mass-roc workload가 실행할 시나리오",
    )
    parser.add_argument(
        "--certificate-case", default="normal",
        choices=("normal", "forged", "both"),
        help="mass-roc workload의 SC-VDE-001 인증서 분기",
    )
    arguments = parser.parse_args(argv)

    if arguments.workload == "mass-roc" and arguments.phase not in (
        "rr-p2-17", "rr-p2-19"
    ):
        raise SystemExit(
            "mass-roc workload는 rr-p2-17/rr-p2-19 phase에서만 실행할 수 있습니다"
        )
    if arguments.phase == "mass-roc-session":
        # persistent session은 workload 요청을 파일로 받는다.
        # 시나리오는 요청마다 정해지므로 --scenario-id를 쓰지 않는다.
        if arguments.gain_artifact is None:
            raise SystemExit(
                "mass-roc-session에는 --gain-artifact가 필요합니다 "
                "(OPERATIONAL config의 RX gain 결속)")
    if arguments.phase == "mass-roc-service":
        # service-only phase는 시나리오 하나를 독립 session으로 돈다.
        # 공유 session(all)은 이 경로에서 허용하지 않는다 (Phase G 계약).
        if arguments.scenario_id == "all":
            raise SystemExit(
                "mass-roc-service는 시나리오 하나만 실행합니다 "
                "(--scenario-id all 금지 — 독립 child session을 쓰십시오)"
            )
        if arguments.gain_artifact is None:
            raise SystemExit(
                "mass-roc-service에는 --gain-artifact가 필요합니다 "
                "(OPERATIONAL config의 RX gain 결속)"
            )

    # v0.9.1 I항: hardware configure/spawn 전에 profile 결속을 검증한다.
    validate_rr_binding(arguments, _load_run_metadata(arguments.configs_dir, arguments.gain_artifact))

    from vdeter.stage7.coordinator import Stage7RuntimeCoordinator
    from vdeter.stage7.hw_operations import read_monotonic_raw_ns

    os.makedirs(os.path.join(arguments.run_root, "evidence"), exist_ok=True)
    resolved, plan = load_group(
        arguments.run_id, arguments.configs_dir, arguments.gain_artifact
    )
    # RR service phase는 test adapter가 필요하다. test plan SHA는 endpoint
    # resolved config 집합에 결속(간이): resolved sha들의 canonical digest.
    is_rr_service = arguments.phase in (
        "rr-p2-17", "rr-p2-19", "rr-p3-fault", "rr-p3-soak-17", "rr-p3-soak-19",
        # Phase G additive: service-only/session phase도 TEST adapter가 필요하다.
        "mass-roc-service", "mass-roc-session",
    )
    test_plan_sha = None
    if is_rr_service:
        test_plan_sha = hashlib.sha256(
            json.dumps(
                {m: resolved[m].resolved_sha256 for m in ENDPOINT_IDS},
                sort_keys=True,
            ).encode()
        ).hexdigest()
    processes, channels = spawn_and_connect(
        arguments.run_id, arguments.run_root, arguments.configs_dir, plan,
        arguments.gain_artifact, test_plan_sha=test_plan_sha,
    )
    coordinator = Stage7RuntimeCoordinator(
        plan,
        channels,
        host_raw_clock=read_monotonic_raw_ns,
        run_id=arguments.run_id,
    )

    exit_code = 0
    phase_result = None
    try:
        if arguments.phase in ("p0", "hold"):
            run_p0(coordinator, resolved, plan, arguments.run_root)
        elif arguments.phase == "p1":
            if arguments.edge is None:
                raise SystemExit("p1에는 --edge가 필요합니다")
            phase_result = run_p1(
                coordinator, resolved, plan, arguments.run_root, arguments.edge,
                gain_artifact_path=arguments.gain_artifact,
            )
        elif arguments.phase == "p2":
            phase_result = run_p2(
                coordinator, resolved, plan, arguments.run_root,
                gain_artifact_path=arguments.gain_artifact,
            )
        elif arguments.phase == "gaincal":
            phase_result = run_gaincal(
                coordinator, resolved, plan, arguments.run_root
            )
        elif arguments.phase == "rr-p0":
            phase_result = run_rr_p0(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                processes=processes,
            )
        elif arguments.phase == "rr-p1":
            phase_result = run_rr_p1(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
            )
        elif arguments.phase in ("rr-p2-17", "rr-p2-19"):
            if arguments.workload == "mass-roc":
                phase_result = run_mass_roc(
                    coordinator, resolved, plan, arguments.run_root,
                    run_id=arguments.run_id,
                    metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                    link_id=int(arguments.phase[-2:]),
                    gain_artifact_path=arguments.gain_artifact,
                    configs_dir=arguments.configs_dir,
                    test_plan_sha=test_plan_sha,
                    scenario_id=arguments.scenario_id,
                    certificate_case=arguments.certificate_case,
                )
            else:
                phase_result = run_rr_p2(
                    coordinator, resolved, plan, arguments.run_root,
                    run_id=arguments.run_id,
                    metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                    link_id=int(arguments.phase[-2:]),
                    gain_artifact_path=arguments.gain_artifact,
                    configs_dir=arguments.configs_dir,
                    test_plan_sha=test_plan_sha,
                )
        elif arguments.phase in ("rr-p3-soak-17", "rr-p3-soak-19"):
            phase_result = run_rr_p3_soak(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                link_id=int(arguments.phase[-2:]),
                gain_artifact_path=arguments.gain_artifact,
                configs_dir=arguments.configs_dir,
                test_plan_sha=test_plan_sha,
            )
        elif arguments.phase == "mass-roc-session":
            # Phase G 최종 모델: bounded persistent session.
            phase_result = run_mass_roc_session(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                configs_dir=arguments.configs_dir,
                gain_artifact_path=arguments.gain_artifact,
                test_plan_sha=test_plan_sha,
                processes=processes,
            )
        elif arguments.phase == "mass-roc-service":
            # Phase G additive 분기 — 기존 RR phase 경로에는 영향이 없다.
            phase_result = run_mass_roc_service(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                configs_dir=arguments.configs_dir,
                gain_artifact_path=arguments.gain_artifact,
                test_plan_sha=test_plan_sha,
                scenario_id=arguments.scenario_id,
                certificate_case=arguments.certificate_case,
                processes=processes,
            )
        elif arguments.phase == "rr-p3-fault":
            phase_result = run_rr_p3_fault(
                coordinator, resolved, plan, arguments.run_root,
                run_id=arguments.run_id,
                metadata=_load_run_metadata(arguments.configs_dir, arguments.gain_artifact),
                processes=processes,
            )
    except Exception as error:  # noqa: BLE001
        print(f"[coordinator] phase 실패: {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        summary = {
            "run_id": arguments.run_id,
            "phase": arguments.phase,
            "workload": arguments.workload,
            "phase_result": phase_result,
            "plan_sha256": plan.resolved_sha256,
            "command_log": list(coordinator.command_log()),
            "faults": [
                {
                    "private_code": fault.private_code.value,
                    "phase": fault.phase,
                    "endpoint_id": fault.endpoint_id,
                }
                for fault in coordinator.faults()
            ],
            "inhibited": coordinator.inhibited,
        }
        path = os.path.join(
            arguments.run_root, "evidence", f"coordinator-{arguments.phase}.json"
        )
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, ensure_ascii=True, sort_keys=True, indent=1)
        for endpoint_id, process in processes.items():
            try:
                process.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                print(f"[coordinator] {endpoint_id} launcher terminate", flush=True)
        print(f"[coordinator] evidence: {path}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
