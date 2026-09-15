"""Stage 8 PoC child process harness(시험 전용, tooling domain).

`PocSupervisor`의 subprocess 시험에서 실제 child로 실행된다. VDETER_POC_* env로 socket
경로·token FD·mode·binding id를 받아 public SDK로 연결해 scripted 시나리오를 돈다. 실제
PoC 의미(공격)는 없고 연결·event·action·종료 경로만 흉내 낸다.

mode:
- happy         : handshake→event→ACK→FINISH→CLOSE 정상 종료
- action        : PACKET_OBSERVED에 replay action 제출
- spam_stdout   : stdout으로 300 KiB 출력(출력 상한 시험)
- ignore_sigterm: ARMED 도달 후 SIGTERM 무시하고 hang(SIGKILL escalation 시험)
"""

from __future__ import annotations

import os
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vdeter.stage8.poc_api.sdk import ClientState, PocContext, SdkEngine  # noqa: E402
from vdeter.stage8.poc_api.transport import connect  # noqa: E402
from vdeter.stage8.testing import (  # noqa: E402
    ClientRunner,
    RecordingCallbacks,
    build_manifest,
    hello_payload,
)


def main() -> int:
    mode = os.environ.get("VDETER_POC_MODE", "happy")
    socket_path = os.environ["VDETER_POC_SOCKET"]
    token_fd = int(os.environ["VDETER_POC_TOKEN_FD"])
    token = os.read(token_fd, 4096)
    os.close(token_fd)
    run_id = os.environ.get("VDETER_POC_RUN_ID", "run-harness")
    session_id = os.environ.get("VDETER_POC_SESSION", "session-harness")
    workload_id = os.environ.get("VDETER_POC_WORKLOAD", "workload-harness")
    scenario_id = os.environ.get("VDETER_POC_SCENARIO", "SC-VDE-006")

    if mode == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    manifest = build_manifest()
    context = PocContext(session_id=session_id, workload_id=workload_id, scenario_id=scenario_id)

    def event_action(event, ctx):
        if mode == "action" and event.type == "PACKET_OBSERVED":
            return [ctx.replay(target="ROC", source_observation_id=event.body.get("observation_id"))]
        return []

    callbacks = RecordingCallbacks(event_action_fn=event_action)
    transport = connect(socket_path)
    engine = SdkEngine(callbacks, context)
    runner = ClientRunner(transport, engine)
    runner.send_hello(hello_payload(token.hex(), run_id, manifest))

    if mode == "spam_stdout":
        sys.stdout.write("x" * (300 * 1024))
        sys.stdout.flush()

    if mode == "ignore_sigterm":
        # ARMED까지만 처리한 뒤 응답을 멈추고 hang → supervisor가 TERM→KILL로 escalate.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and engine.state not in (
            ClientState.ARMED,
            ClientState.RUNNING,
            ClientState.CLOSED,
            ClientState.ABORTED,
        ):
            runner.pump_once(timeout=0.05)
        while True:
            time.sleep(0.2)

    runner.run(timeout=15.0)
    transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
