"""launcher 역할을 하는 **실 자식 프로세스** — PoC device 경계를 실 socket으로 노출한다.

P02 결선 시험용이다. launcher 프로세스가 하는 일(Stage 7 endpoint 소유 + TEST adapter를
`SOCK_SEQPACKET`으로 서빙 + radio pump)을 그대로 자식 프로세스에서 수행한다. 대체하는 것은
**B210 device I/O 하나**(`Stage7FakeMediumWorker` + 고정 배선 매질)뿐이고, socket·framing·
canonical JSON·adapter 검증·route sidecar·`schedule_tx`·TX 원장은 전부 실제다.

가상 매질 subsystem을 새로 만들지 않는다 — 기존 software group을 자식 안에서 한 번 띄울 뿐이다.
부모(=supervisor 역할)는 실 `SeqPacketClient` + `Stage7ScenarioController`로만 접근한다.

직접 실행 진입점: `python -m tests.support.poc_device_harness <socket-dir> <plan-sha>`
"""

from __future__ import annotations

import os
import sys
import threading
import time

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

    from vdeter.stage7.configuration import CANONICAL_ROLES
    from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
    from vdeter.stage7.testing.poc_device import Stage7PocDeviceSeam
    from vdeter.stage7.transport import SeqPacketServer

    from tests.support.stage7 import (
        build_stage7_software_group,
        drive_stage7_group,
        start_stage7_group,
    )

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

        seam = Stage7PocDeviceSeam(endpoint_id=endpoint_id, endpoint=endpoint,
                                   station_id_by_endpoint=stations)
        return Stage7ServiceApiTestAdapter(
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
