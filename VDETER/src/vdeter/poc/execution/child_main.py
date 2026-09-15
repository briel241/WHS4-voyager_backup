"""PoC child process 진입점(실행 명세 §5.1, §6.4). **production** Stage 4 코드.

`PocSupervisor`(Stage 3)가 실제 subprocess로 이 module을 띄운다
(`python -m vdeter.poc.execution.child_main`). 이 child는:

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

from vdeter.poc.execution.limits import SESSION_ABSOLUTE_DEADLINE_S  # noqa: E402
from vdeter.poc.api.codec import decode_wire_packet, encode_wire_packet  # noqa: E402
from vdeter.poc.api.limits import (  # noqa: E402
    RECONNECT_BACKOFFS_S,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_TOTAL_DEADLINE_S,
)
from vdeter.poc.api.messages import parse_envelope  # noqa: E402
from vdeter.poc.api.sdk import ClientState, PocContext, SdkEngine  # noqa: E402
from vdeter.poc.api.transport import OversizedPacket, connect  # noqa: E402

#: child는 자체 수명 제한을 두지 않는다. 정상 종료는 server의 CLOSE(engine CLOSED)로만 온다.
#: workload/close 상한(workload 900초·close 600초, 기존 Stage 7 코드 상한, `apps/service_session.py`)의
#: **실제 강제와 종료(CLOSE→SIGTERM→SIGKILL)** 는 workload를 소유한 runtime(기존 Stage 7 resident
#: session)이 수행하며, 이는 Stage 6 통합에서 Stage 8 실행 layer에 연결된다(Stage 4는 이 deadline을
#: session.py 모델로만 표현한다). 아래 값은 그 runtime이 사라져 CLOSE도 SIGKILL도 오지 않는 극단의
#: **최후 방어** ceiling일 뿐이며, session absolute(7200초)보다 커서 정상 workload를 조기 종료시키지
#: 않는다(§5.2).
CHILD_SAFETY_TIMEOUT_S = SESSION_ABSOLUTE_DEADLINE_S + 600.0

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
    previous = sys.modules.get(_SNAPSHOT_MODULE)
    sys.modules[_SNAPSHOT_MODULE] = module
    try:
        spec.loader.exec_module(module)  # 연구자 snapshot 코드 실행(§6.1: scan이 아니라 실행 시점)
        cls = getattr(module, class_name, None)
        if cls is None:
            raise AttributeError(f"entrypoint class {class_name}가 snapshot에 없습니다")
        return cls()
    except BaseException:
        if previous is None:
            sys.modules.pop(_SNAPSHOT_MODULE, None)
        else:
            sys.modules[_SNAPSHOT_MODULE] = previous
        raise


def _build_hello_payload(token: bytes, run_id: str, manifest_hello: dict) -> dict:
    return {
        "token": token.hex(),
        "run_id": run_id,
        "manifest": manifest_hello,
        "requested_capabilities": list(manifest_hello.get("requested_capabilities", []))
        + list(manifest_hello.get("required_capabilities", [])),
        "requested_triggers": list(manifest_hello.get("requested_triggers", [])),
    }


def _reconnect(engine, old_transport, reconnect_connect, hello_payload, *,
               sleep_fn, now_fn):
    """R10/§12.2: 같은 process·같은 SDK 상태로 총 2초·최대 3회·정해진 backoff 재접속.

    같은 request_id·미확정 action·completed sequence·epoch·workload binding을 유지한 채(같은
    `engine` 재사용) reconnect HELLO를 보낸다. process를 재시작하지 않고 safety deadline을
    초기화하지 않는다. 정상 종료(CLOSED)나 이미 ABORTED는 재접속 대상이 아니다. 성공 시 새
    transport, 실패 시 None(caller가 ABORT). `reconnect_connect` 미주입이면 재접속 비활성.
    """

    if reconnect_connect is None or engine.state in (ClientState.CLOSED, ClientState.ABORTED):
        return None
    try:
        old_transport.close()
    except Exception:  # noqa: BLE001 - 이미 끊긴 socket close 실패는 무시
        pass
    reconnect_deadline = now_fn() + RECONNECT_TOTAL_DEADLINE_S
    for attempt in range(RECONNECT_MAX_ATTEMPTS):
        backoff = RECONNECT_BACKOFFS_S[min(attempt, len(RECONNECT_BACKOFFS_S) - 1)]
        if now_fn() + backoff > reconnect_deadline:
            break
        sleep_fn(backoff)
        if now_fn() > reconnect_deadline:
            break
        try:
            new_transport = reconnect_connect()
        except Exception:  # noqa: BLE001 - 접속 실패는 다음 시도로
            continue
        try:
            hello = engine.build_hello(hello_payload, connect_kind="reconnect")
            new_transport.send_packet(encode_wire_packet(hello))
        except Exception:  # noqa: BLE001 - reconnect HELLO 송신 실패는 다음 시도로
            try:
                new_transport.close()
            except Exception:  # noqa: BLE001
                pass
            continue
        return new_transport
    return None


def run_client(callbacks, transport, *, token: bytes, run_id: str,
               session_id: str, workload_id: str, scenario_id: str,
               manifest_hello: dict, poll: float = 0.05,
               now_fn=time.monotonic, safety_timeout_s: float = CHILD_SAFETY_TIMEOUT_S,
               reconnect_connect=None, sleep_fn=time.sleep) -> int:
    """SDK client loop을 돈다(§7~§13). 정상 종료(server CLOSE) 0, 그 외 non-zero.

    child는 자체 수명 제한을 두지 않는다. server가 CLOSE를 보내(engine CLOSED) 또는 abort
    (ABORTED), 혹은 복구 불가 연결 손실까지 실행한다. `safety_timeout_s`는 supervisor가 사라진
    경우의 최후 방어일 뿐 정상 workload(≤900초)나 session(≤7200초)을 조기 종료시키지 않는다.

    R10: transport 손실 시 `reconnect_connect`가 주입돼 있으면 같은 process·같은 engine 상태로
    §12.2 재접속(총 2초·3회·backoff)을 시도한다. 정상 CLOSE와 복구 가능한 단절을 구별하며,
    복구 실패 시에만 ABORT(non-zero)한다. safety deadline은 재접속으로 초기화하지 않는다.
    """

    context = PocContext(session_id=session_id, workload_id=workload_id, scenario_id=scenario_id)
    engine = SdkEngine(callbacks, context)
    hello_payload = _build_hello_payload(token, run_id, manifest_hello)
    connection_lost = False
    try:
        transport.send_packet(encode_wire_packet(engine.build_hello(hello_payload)))
        deadline = now_fn() + safety_timeout_s  # 재접속으로 초기화하지 않는다(§5.2)
        while engine.state not in (ClientState.CLOSED, ClientState.ABORTED):
            if now_fn() > deadline:
                break  # 최후 방어(§5.2): supervisor가 CLOSE/SIGKILL을 못 보낸 극단 상황
            try:
                data = transport.recv_packet(timeout=poll)
            except (ConnectionError, OversizedPacket, OSError):
                # 복구 가능한 단절인지 판정: 아직 CLOSED/ABORTED가 아니면 같은 PID로 재접속.
                new_transport = _reconnect(engine, transport, reconnect_connect, hello_payload,
                                           sleep_fn=sleep_fn, now_fn=now_fn)
                if new_transport is None:
                    connection_lost = True  # 복구 불가/재접속 비활성 → child 종료
                    break
                transport = new_transport
                continue
            now = now_fn()
            outbound = []
            if data is not None:
                parsed = parse_envelope(decode_wire_packet(data))
                outbound.extend(engine.receive(parsed, now))
            outbound.extend(engine.tick(now))
            send_failed = False
            for message in outbound:
                try:
                    transport.send_packet(encode_wire_packet(message))
                except (ConnectionError, OSError):
                    send_failed = True
                    break
            if send_failed:
                new_transport = _reconnect(engine, transport, reconnect_connect, hello_payload,
                                           sleep_fn=sleep_fn, now_fn=now_fn)
                if new_transport is None:
                    connection_lost = True
                    break
                transport = new_transport
    finally:
        transport.close()
    return 0 if engine.state == ClientState.CLOSED else 1


def main() -> int:
    try:
        token_fd = int(os.environ[ENV_TOKEN_FD])
        try:
            token = os.read(token_fd, 4096)
        finally:
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
    except KeyboardInterrupt:
        raise  # 운영자 인터럽트는 전달(child가 non-zero로 종료)
    except BaseException as error:  # noqa: BLE001 - import 시점 종료(SystemExit 포함)도 로드 실패
        # 연구자 snapshot이 import 중 sys.exit()/SystemExit을 내면 clean exit(0)으로 오인되지
        # 않도록 fail-closed non-zero로 고정한다(§6.4: 로드 실패는 연결 전 종료).
        sys.stderr.write(f"POC entrypoint 로드 실패: {error.__class__.__name__}\n")
        return 3

    try:
        transport = connect(socket_path)
    except Exception as error:  # noqa: BLE001 - 연결 실패 fail-closed
        sys.stderr.write(f"POC socket 연결 실패: {error.__class__.__name__}\n")
        return 4

    try:
        return run_client(
            callbacks, transport, token=token, run_id=run_id,
            session_id=session_id, workload_id=workload_id, scenario_id=scenario_id,
            manifest_hello=manifest_hello,
            # R10: 같은 socket_path로 같은 process 재접속(§12.2). process 재시작이 아니다.
            reconnect_connect=lambda: connect(socket_path),
        )
    except Exception as error:  # noqa: BLE001
        sys.stderr.write(f"POC client 오류: {error.__class__.__name__}\n")
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
