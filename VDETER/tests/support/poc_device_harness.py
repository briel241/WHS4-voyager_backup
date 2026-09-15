"""launcher 역할을 하는 **실 자식 프로세스** — PoC device 경계를 실 socket으로 노출한다.

P02 결선 시험용이다. launcher 프로세스가 하는 일(Stage 7 endpoint 소유 + TEST adapter를
`SOCK_SEQPACKET`으로 서빙 + radio pump)을 그대로 자식 프로세스에서 수행한다. 대체하는 것은
**B210 device I/O 하나**(`Stage7FakeMediumWorker` + 고정 배선 매질)뿐이고, socket·framing·
canonical JSON·adapter 검증·route sidecar·`schedule_tx`·TX 원장은 전부 실제다.

가상 매질 subsystem을 새로 만들지 않는다 — 기존 software group을 자식 안에서 한 번 띄울 뿐이다.
부모(=supervisor 역할)는 실 `SeqPacketClient` + `ScenarioController`로만 접근한다.

직접 실행 진입점: `python -m tests.support.poc_device_harness <socket-dir> <plan-sha>`
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def _mark(name: str) -> None:
    """시험용 명시적 동기화 신호 하나를 남긴다.

    `VDETER_REPRO_MARKER_DIR`이 설정되지 않으면 **아무 일도 하지 않는다** — 기본 경로의
    동작·판정·시각은 이 계측 유무와 무관하다.
    """
    directory = os.environ.get("VDETER_REPRO_MARKER_DIR")
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(name)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def _mark_command(message) -> None:
    """RX scope ENABLE이 **실 adapter 경로로** 도착한 순간을 표시한다(취소 지점 고정용).

    controller의 `WorkloadRxScope.call`은 `poc_collision` 명령에 `mode`를 실어 보낸다
    (`apps/workload_rx_scope.py`). 그 ENABLE은 `prepare_poc_scope` 안에서 일어나며
    이는 `workload.arm()`보다 **앞선** 구간이다. 이 표지가 있으면 고정 sleep이나
    `/proc/<pid>/wchan` 추정 없이 arm 이전 구간의 취소를 결정적으로 재현할 수 있다.

    message 키 구조를 가정하지 않으려고 직렬화된 본문에서 찾는다 — 계측 전용이며 응답을
    만들거나 바꾸지 않는다.
    """
    if not os.environ.get("VDETER_REPRO_MARKER_DIR"):
        return
    try:
        text = json.dumps(message, default=str)
    except (TypeError, ValueError):
        text = str(message)
    if "poc_collision" in text and '"ENABLE"' in text:
        _mark("scope-enable.marker")


#: 모의 시간 진행 배속(실시간 대비). lead 의미를 보존하면서 시험을 과도하게 늘리지 않는 값.
_TIME_ACCELERATION = float(os.environ.get("STAGE8_TEST_TIME_ACCELERATION", "4.0"))


#: 서빙할 endpoint(실 배치와 같은 세 endpoint). 각자 **자기 socket**과 자기 adapter를 가진다.
ENDPOINTS = ("MASS-A", "MASS-B", "ROC")


def serve(socket_dir: str, plan_sha: str, *, endpoints=ENDPOINTS) -> int:
    """software group을 띄우고 **endpoint마다 별도 socket**으로 TEST adapter를 서빙한다.

    실 배치에서는 launcher가 endpoint마다 하나씩 세 프로세스다. 여기서는 공유 매질을 한 프로세스가
    소유해야 하므로 프로세스는 하나지만, **socket·adapter·인증·endpoint 정체는 endpoint별로 분리**해
    supervisor가 보는 경계를 실제와 같게 만든다. 이 차이(세 launcher = 한 프로세스)는 남은 제한으로
    보고한다 — 여기서 "실물 종단"이라고 주장하지 않는다.
    """

    from vdeter.b210_system.configuration import CANONICAL_ROLES
    from vdeter.b210_system.control_plane.adapter import ServiceApiTestAdapter
    from vdeter.b210_system.control_plane.poc_device import PocDeviceSeam
    from vdeter.b210_system.transport import SeqPacketServer

    from tests.support.stage7 import (
        build_stage7_software_group,
        drive_stage7_group,
        start_stage7_group,
    )

    # Exact production D-10 function, not a copied test policy. Other launcher
    # adaptations (D-11 timing/D-18 ACK, etc.) remain hardware-test limitations.
    from vdeter.mac.coordinator import MacCoordinator
    from vdeter.b210_system.short_data_compat import wrap_short_data_submission
    MacCoordinator._submit_short_data = wrap_short_data_submission(MacCoordinator._submit_short_data)
    clock, medium, plan, coordinator, members, resolved = build_stage7_software_group()
    start_stage7_group(clock, medium, coordinator, members, resolved)
    stations = {name: station for name, (_role, station) in CANONICAL_ROLES.items()}

    def _build(endpoint_id):
        endpoint = members[endpoint_id].endpoint

        def _mute_lane(body):
            """정상 TX가 lane을 점유한 상황을 만든다(P04 경합 시험용).

            **기존 fault hook allowlist**를 쓴다 — production launcher는 hook을 하나도 등록하지
            않으므로 실장비에서 이 경로는 항상 `FAULT_NOT_ALLOWLISTED`다. 장치를 조작하는 새 op을
            열지 않는다.
            """
            worker = endpoint.dependencies.radio.lease.worker
            lane, muted = int(body.get("lane", 0)), bool(body.get("muted", True))
            worker.set_tx_mute(lane, muted)
            return {"lane": lane, "muted": muted}

        seam = PocDeviceSeam(endpoint_id=endpoint_id, endpoint=endpoint,
                                   station_id_by_endpoint=stations)
        return ServiceApiTestAdapter(
            endpoint_id=endpoint_id,
            service=endpoint,
            resolved_test_plan_sha256=plan_sha,
            authorized_controller=(os.getuid(),),
            poc_device=seam,
            fault_hooks={"poc_mute_lane": _mute_lane},
        )

    stop = threading.Event()

    def _pump():
        """launcher의 radio/MAC pump. 시간만 전진시키고 통지를 만들지 않는다.

        **모의 시간 진행 속도가 중요하다.** 권위 `schedule_tx`는 요청 시각이 지금+최소 lead
        (100 ms, retune 시 200 ms)보다 앞서야 수락한다. 모의 시계가 실시간보다 훨씬 빠르면 RPC
        왕복 동안 lead가 소진돼 `RADIO_LATE_REQUEST`가 된다 — lead 검사를 완화하는 대신 pump를
        실시간에 가깝게 맞춰 lead 의미를 보존한다.
        """
        step_ns = 160_000_000
        wall_per_step = (step_ns / 1e9) / _TIME_ACCELERATION
        while not stop.is_set():
            try:
                drive_stage7_group(clock, members, 1, step_nanoseconds=step_ns)
            except Exception:  # noqa: BLE001 - pump 실패가 서빙을 죽이지 않는다
                pass
            time.sleep(wall_per_step)

    threading.Thread(target=_pump, daemon=True).start()

    # bind 직전에 socket 디렉터리를 보증한다.
    #
    # `/run/user/<uid>`는 tmpfs이고 이 프로세스가 소유하지 않는다. 호출자가 디렉터리를 만든
    # 뒤 여기까지 오는 사이에 사라지면 `bind`가 `FileNotFoundError`로 죽는다(실측: 같은 boot
    # 안에서 `socket_dir_exists: false`). 자기 socket을 놓을 자리를 자기가 확인하는 것이며,
    # production 경로(`runtime_socket_root`)를 바꾸지 않는다 — 경로는 호출자가 정한 그대로다.
    os.makedirs(socket_dir, mode=0o700, exist_ok=True)

    servers = []
    for endpoint_id in endpoints:
        adapter = _build(endpoint_id)
        server = SeqPacketServer(os.path.join(socket_dir, f"{endpoint_id}.test.sock"))
        servers.append(server)

        def _loop(server=server, adapter=adapter):
            try:
                server.accept(expected_pid=None, expected_uid=os.getuid(), timeout=30.0)
                while not stop.is_set():
                    message = server.recv_message(timeout=5.0)
                    if message is None:
                        continue
                    _mark_command(message)
                    server.send_message(adapter.handle_command(message, peer=(os.getuid(),)))
            except (ConnectionError, OSError):
                pass

        threading.Thread(target=_loop, daemon=True).start()

    # 부모가 연결할 수 있게 socket 준비 완료를 알린다(경합 없는 handshake).
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    try:
        while not stop.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for server in servers:
            server.close()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        sys.stderr.write("usage: poc_device_harness <socket-dir> <plan-sha>\n")
        return 2
    return serve(argv[0], argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
