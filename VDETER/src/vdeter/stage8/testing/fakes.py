"""fake clock·provider·observation·child(명세 §18: real sleep 대신 fake 우선)."""

from __future__ import annotations

import os
import threading
import uuid

from ..poc_api.types import (
    ActionDescriptor,
    ObservationRecord,
    PhysicalChannel,
    PocManifestDescriptor,
    ProviderSubmission,
    TARGET_TO_PHYSICAL_CHANNEL,
    TuningState,
)


class FakeClock:
    """수동 진행 monotonic clock. deadline 시험을 sleep 없이 결정적으로 만든다."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += float(dt)

    def set(self, value: float) -> None:
        self._now = float(value)


class FakeActionSubmitProvider:
    """논리 target → canonical channel·parent·transfer를 유도하는 fake provider.

    `submit_calls`로 정확히 한 번 호출됐는지 검증한다(cache-hit 시험). `reject_reason`을
    설정하면 provider가 request 거부를 흉내 낸다.
    """

    def __init__(self, *, reject_reason=None) -> None:
        self.submit_calls = 0
        self.submitted_request_ids: list[str] = []
        self.reject_reason = reject_reason

    def derive_physical_channel(self, target: str) -> PhysicalChannel:
        return TARGET_TO_PHYSICAL_CHANNEL[target]

    def submit(self, action: ActionDescriptor, *, request_id: str) -> ProviderSubmission:
        self.submit_calls += 1
        self.submitted_request_ids.append(request_id)
        if self.reject_reason is not None:
            from ..poc_api.errors import PocError

            raise PocError(self.reject_reason, "fake provider reject")
        channel = self.derive_physical_channel(action.target)
        return ProviderSubmission(
            physical_channel=channel,
            broadcast_parent_id=f"parent-{uuid.uuid4().hex[:12]}",
            transfer_id=f"tx-{uuid.uuid4().hex[:12]}",
        )


class FakeObservationProvider:
    """poll()이 미리 넣어둔 :class:`ObservationRecord`를 순서대로 돌려주는 fake."""

    def __init__(self, records=None) -> None:
        self._records = list(records or [])

    def add(self, record: ObservationRecord) -> None:
        self._records.append(record)

    def poll(self) -> ObservationRecord | None:
        if self._records:
            return self._records.pop(0)
        return None


def make_observation(
    *,
    physical_channel: PhysicalChannel = PhysicalChannel.LOWER,
    tuning_state: TuningState = TuningState.TUNED_OBSERVER,
    source_station: str = "ROC",
    destination_station: str = "MASS-A",
    observation_id: str | None = None,
    device_hw_timestamp_ns: int = 123456789,
    host_mono_timestamp_ns: int = 987654321,
    body: dict | None = None,
) -> ObservationRecord:
    return ObservationRecord(
        physical_channel=physical_channel,
        tuning_state=tuning_state,
        observation_id=observation_id or f"obs-{uuid.uuid4().hex[:10]}",
        source_station=source_station,
        destination_station=destination_station,
        device_hw_timestamp_ns=device_hw_timestamp_ns,
        host_mono_timestamp_ns=host_mono_timestamp_ns,
        body=dict(body or {}),
    )


def build_manifest(
    *,
    poc_id: str = "fake-poc",
    version: str = "1.0.0",
    requested_capabilities=("observe_packet", "replay_packet"),
    requested_triggers=("SCENARIO_STARTED", "PACKET_OBSERVED"),
    required_capabilities=(),
    allowed_targets=("ROC", "MASS-A"),
    compatible_scenarios=("SC-VDE-006",),
    max_actions_requested: int = 10,
) -> PocManifestDescriptor:
    return PocManifestDescriptor(
        poc_id=poc_id,
        version=version,
        entrypoint=f"{poc_id}.py:Poc",
        compatible_scenarios=tuple(compatible_scenarios),
        attacker_endpoint="MASS-B",
        allowed_targets=tuple(allowed_targets),
        requested_capabilities=tuple(requested_capabilities),
        requested_triggers=tuple(requested_triggers),
        required_capabilities=tuple(required_capabilities),
        script_sha256="00" * 32,
        manifest_sha256="11" * 32,
        max_actions_requested=max_actions_requested,
    )


def hello_payload(token_hex: str, run_id: str, manifest: PocManifestDescriptor) -> dict:
    """`SdkEngine.build_hello`가 받는 payload를 만든다."""

    return {
        "token": token_hex,
        "run_id": run_id,
        "manifest": manifest.as_hello_value(),
        "requested_capabilities": list(manifest.requested_capabilities)
        + list(manifest.required_capabilities),
        "requested_triggers": list(manifest.requested_triggers),
    }


class ThreadChildHandle:
    """같은 process thread로 client를 돌리는 fake child(§18: subprocess 없이 round-trip).

    같은 process이므로 SO_PEERCRED pid=os.getpid()로 supervisor의 expected_pid와 일치한다.
    signal(TERM/KILL)은 협조적 stop flag로 흉내 낸다(실 signal 시험은 subprocess harness).
    """

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self._thread = thread
        self._stop = stop_event
        self.pid = os.getpid()
        self.stdout = None
        self.stderr = None

    def poll_exit(self) -> int | None:
        return None if self._thread.is_alive() else 0

    def send_signal(self, signum: int) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> int | None:
        self._thread.join(timeout=timeout)
        return None if self._thread.is_alive() else 0

    def reap(self) -> int | None:
        return self.poll_exit()


class ThreadChildSpawner:
    """`client_main(socket_path, token, stop_event)`을 daemon thread로 실행하는 fake spawner."""

    def __init__(self, client_main) -> None:
        self._client_main = client_main
        self.handle: ThreadChildHandle | None = None

    def spawn(self, *, token_fd: int, socket_path: str, env: dict):
        # token을 FD에서 읽어 client에 넘긴다. FD 소유권은 supervisor에 있으므로 여기서
        # 닫지 않는다(supervisor가 spawn 뒤 자기 copy를 닫는다). in-process이므로 같은
        # fd table을 공유한다.
        token = os.read(token_fd, 4096)
        stop_event = threading.Event()

        def _run():
            try:
                self._client_main(socket_path, token, stop_event)
            except Exception:  # noqa: BLE001 - thread child 예외는 supervisor deadline으로 드러난다
                pass

        thread = threading.Thread(target=_run, name="poc-thread-child", daemon=True)
        thread.start()
        self.handle = ThreadChildHandle(thread, stop_event)
        return self.handle


__all__ = (
    "FakeClock",
    "FakeActionSubmitProvider",
    "FakeObservationProvider",
    "make_observation",
    "build_manifest",
    "hello_payload",
    "ThreadChildHandle",
    "ThreadChildSpawner",
)
