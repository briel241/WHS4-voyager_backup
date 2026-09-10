"""PoC child process 진입점(실행 명세 §5.1, §6.4). **production** Stage 4 코드.

`PocSupervisor`(Stage 3)가 실제 subprocess로 이 module을 띄운다
(`python -m vdeter.stage8.execution.child_main`). 이 child는:

1. inherited read-only FD에서 workload token을 읽는다(argv·일반 env에 token 없음, §5.1).
2. **immutable snapshot의 Python 파일**을 `importlib`로 로드하고 manifest entrypoint의
   class를 얻는다(원본이 아니라 snapshot을 실행, §5.3). 이 시점에 연구자 코드가 실행된다
   (catalog scan 때가 아니라, §6.1).
3. entrypoint class를 instance화해 연구자 callback으로 쓴다.
4. public SDK(`SdkEngine`)로 Unix socket에 연결해 handshake→event→action→finish→close를
   돈다. 이 module은 production private 구현(`_supervisor`)이나 test double을 import하지 않는다.

로드·연결 실패는 삼키지 않고 stderr에 비밀 없는 요약을 남기고 non-zero로 종료한다
(fail-closed). supervisor는 process 조기 종료·handshake 실패를 infrastructure 오류로 본다(§11.2).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

# `python -m ...`이면 vdeter는 이미 import 가능하다. 직접 실행 대비 src 경로 보강.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vdeter.stage8.poc_api.codec import decode_wire_packet, encode_wire_packet  # noqa: E402
from vdeter.stage8.poc_api.messages import parse_envelope  # noqa: E402
from vdeter.stage8.poc_api.sdk import ClientState, PocContext, SdkEngine  # noqa: E402
from vdeter.stage8.poc_api.transport import connect  # noqa: E402

#: child가 받는 env 이름(supervisor child.ENV_* 와 Stage 4 추가분).
ENV_TOKEN_FD = "VDETER_POC_TOKEN_FD"
ENV_SOCKET_PATH = "VDETER_POC_SOCKET"
ENV_SNAPSHOT_SCRIPT = "VDETER_POC_SNAPSHOT_SCRIPT"
ENV_ENTRYPOINT = "VDETER_POC_ENTRYPOINT"
ENV_HELLO = "VDETER_POC_HELLO"          # manifest.as_hello_value() JSON
ENV_RUN_ID = "VDETER_POC_RUN_ID"
ENV_SESSION = "VDETER_POC_SESSION"
ENV_WORKLOAD = "VDETER_POC_WORKLOAD"
ENV_SCENARIO = "VDETER_POC_SCENARIO"

#: 로드된 snapshot module 이름(고정, 재사용 안 함).
_SNAPSHOT_MODULE = "vdeter_poc_snapshot"


def load_entrypoint(script_path: str, entrypoint: str):
    """snapshot script를 실행해 entrypoint class instance를 만든다(§5.3, §6.4).

    entrypoint 형식은 `<file>.py:<Class>`. 여기서 연구자 코드가 실제로 import·실행된다.
    """

    _file, _, class_name = entrypoint.partition(":")
    if not class_name:
        raise ValueError("entrypoint에 class 이름이 없습니다")
    spec = importlib.util.spec_from_file_location(_SNAPSHOT_MODULE, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"snapshot module spec 실패: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 연구자 snapshot 코드 실행(§6.1: scan이 아니라 실행 시점)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise AttributeError(f"entrypoint class {class_name}가 snapshot에 없습니다")
    return cls()


def _build_hello_payload(token: bytes, run_id: str, manifest_hello: dict) -> dict:
    return {
        "token": token.hex(),
        "run_id": run_id,
        "manifest": manifest_hello,
        "requested_capabilities": list(manifest_hello.get("requested_capabilities", []))
        + list(manifest_hello.get("required_capabilities", [])),
        "requested_triggers": list(manifest_hello.get("requested_triggers", [])),
    }


def run_client(callbacks, *, socket_path: str, token: bytes, run_id: str,
               session_id: str, workload_id: str, scenario_id: str,
               manifest_hello: dict, timeout: float = 30.0, poll: float = 0.05) -> int:
    """SDK client loop을 돈다(§7~§13). 정상 종료 0, 그 외 non-zero."""

    context = PocContext(session_id=session_id, workload_id=workload_id, scenario_id=scenario_id)
    engine = SdkEngine(callbacks, context)
    transport = connect(socket_path)
    try:
        for message in [engine.build_hello(_build_hello_payload(token, run_id, manifest_hello))]:
            transport.send_packet(encode_wire_packet(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and engine.state not in (
            ClientState.CLOSED, ClientState.ABORTED
        ):
            data = transport.recv_packet(timeout=poll)
            now = time.monotonic()
            outbound = []
            if data is not None:
                parsed = parse_envelope(decode_wire_packet(data))
                outbound.extend(engine.receive(parsed, now))
            outbound.extend(engine.tick(now))
            for message in outbound:
                transport.send_packet(encode_wire_packet(message))
    finally:
        transport.close()
    return 0 if engine.state == ClientState.CLOSED else 1


def main() -> int:
    try:
        token_fd = int(os.environ[ENV_TOKEN_FD])
        token = os.read(token_fd, 4096)
        os.close(token_fd)
        socket_path = os.environ[ENV_SOCKET_PATH]
        script_path = os.environ[ENV_SNAPSHOT_SCRIPT]
        entrypoint = os.environ[ENV_ENTRYPOINT]
        manifest_hello = json.loads(os.environ[ENV_HELLO])
        run_id = os.environ.get(ENV_RUN_ID, "run-child")
        session_id = os.environ.get(ENV_SESSION, "session-child")
        workload_id = os.environ.get(ENV_WORKLOAD, "workload-child")
        scenario_id = os.environ.get(ENV_SCENARIO, "SC-VDE-006")
    except (KeyError, ValueError, OSError) as error:
        sys.stderr.write(f"POC child env 오류: {error.__class__.__name__}\n")
        return 2

    try:
        callbacks = load_entrypoint(script_path, entrypoint)
    except Exception as error:  # noqa: BLE001 - 로드 실패는 fail-closed(연결 전 종료)
        sys.stderr.write(f"POC entrypoint 로드 실패: {error.__class__.__name__}\n")
        return 3

    try:
        return run_client(
            callbacks, socket_path=socket_path, token=token, run_id=run_id,
            session_id=session_id, workload_id=workload_id, scenario_id=scenario_id,
            manifest_hello=manifest_hello,
        )
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"POC client 오류: {error.__class__.__name__}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
