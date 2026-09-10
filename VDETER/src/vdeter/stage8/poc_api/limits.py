"""고정 상한·deadline의 **단일 정의**(명세 §10.1, §11, §12, §13, §7.1).

명세 원칙: "상한과 timeout은 명세의 단일 정의에서만 가져온다." 구현·시험 어디서도
이 값을 재선언하거나 현장에서 완화하지 않는다. 값을 바꾸려면 명세 변경 승인을
받는다.
"""

from __future__ import annotations

# --- 자원 상한 (§10.1) -------------------------------------------------------

#: wire JSON packet 전체(base64·envelope 포함) 상한. parse 전에 적용한다.
MAX_WIRE_PACKET_BYTES = 256 * 1024  # 262144

#: event delivery queue: 아래 두 값 중 먼저 도달한 값.
MAX_EVENT_QUEUE_MESSAGES = 256
MAX_EVENT_QUEUE_BYTES = 2 * 1024 * 1024  # 2 MiB

#: event replay ring: queue와 독립 상한.
MAX_REPLAY_RING_MESSAGES = 256
MAX_REPLAY_RING_BYTES = 2 * 1024 * 1024  # 2 MiB

#: 단일 action descriptor(canonical body) 상한.
MAX_ACTION_DESCRIPTOR_BYTES = 64 * 1024  # 65536

#: 한 workload가 제출할 수 있는 action request 총수.
MAX_WORKLOAD_ACTION_REQUESTS = 32

#: 동시에 accepted 상태로 in-flight일 수 있는 PoC action 수.
MAX_INFLIGHT_ACTIONS = 1


# --- JSON 계약 (§6.1) --------------------------------------------------------

#: nesting 최대 단계(초과 시 거부).
MAX_JSON_NESTING = 16


# --- callback deadline (§11, `POC-API-D08`) ----------------------------------

CALLBACK_ON_START_DEADLINE_S = 3.0
CALLBACK_ON_EVENT_DEADLINE_S = 0.250
CALLBACK_ON_FINISH_DEADLINE_S = 2.0


# --- handshake / event ACK (§7.1, §8.2) --------------------------------------

#: spawn 뒤 첫 valid HELLO 수신까지의 deadline.
HELLO_DEADLINE_S = 3.0

#: START 뒤 ARMED까지(= on_start deadline과 동일 근거).
ARMED_DEADLINE_S = CALLBACK_ON_START_DEADLINE_S

#: 누적 ACK: N개 event 처리 시 즉시.
ACK_BATCH_EVENTS = 16
#: 그 미만이어도 첫 미ACK event 뒤 이 시간 안에 one-shot ACK.
ACK_MAX_DELAY_S = 0.050


# --- reconnect (§12.2, `POC-API-D09`) ----------------------------------------

#: 최초 disconnect부터 복구 전체 deadline.
RECONNECT_TOTAL_DEADLINE_S = 2.0
#: 같은 child PID/process가 시도할 수 있는 connection 총 횟수.
RECONNECT_MAX_ATTEMPTS = 3
#: event-driven backoff 간격(초). 전체 2초 deadline을 넘지 않는다.
RECONNECT_BACKOFFS_S = (0.100, 0.300, 0.600)


# --- stdout/stderr (§13) -----------------------------------------------------

#: stream별 memory evidence 보존 상한.
STDIO_RETAIN_BYTES_PER_STREAM = 64 * 1024  # 64 KiB
#: 각 stream의 앞/뒤 보존 크기(합 = 보존 상한).
STDIO_RETAIN_HEAD_BYTES = 32 * 1024
STDIO_RETAIN_TAIL_BYTES = 32 * 1024
#: workload 누적 drain(stdout+stderr) 상한. 초과 시 abort.
STDIO_TOTAL_DRAIN_LIMIT_BYTES = 256 * 1024  # 256 KiB


# --- 종료·cleanup deadline (§15, `POC-API-D12`) ------------------------------

#: FINISH 뒤 on_finish() 완료 대기.
FINISH_WAIT_S = 2.0
#: CLOSE 뒤 child 자발 종료 대기.
CLOSE_VOLUNTARY_WAIT_S = 2.0
#: SIGTERM 뒤 대기(초과 시 SIGKILL).
SIGTERM_WAIT_S = 1.0


# --- token (§5.1) ------------------------------------------------------------

#: workload 인증 token 크기.
TOKEN_BITS = 256
TOKEN_BYTES = TOKEN_BITS // 8

#: token 재사용 가능한 reconnect window(§5.2). 재접속 총 deadline과 동일 근거.
TOKEN_REUSE_WINDOW_S = RECONNECT_TOTAL_DEADLINE_S


# --- socket 경로 (§4.2) ------------------------------------------------------

#: Unix domain socket sun_path 최대 byte(NUL 포함). Linux 관례 108.
MAX_UNIX_PATH_BYTES = 108
#: workload directory 이름으로 쓰는 nonce byte 수(128-bit).
WORKLOAD_NONCE_BYTES = 16

SOCKET_ROOT_MODE = 0o700
WORKLOAD_DIR_MODE = 0o700
SOCKET_MODE = 0o600


__all__ = (
    "MAX_WIRE_PACKET_BYTES",
    "MAX_EVENT_QUEUE_MESSAGES",
    "MAX_EVENT_QUEUE_BYTES",
    "MAX_REPLAY_RING_MESSAGES",
    "MAX_REPLAY_RING_BYTES",
    "MAX_ACTION_DESCRIPTOR_BYTES",
    "MAX_WORKLOAD_ACTION_REQUESTS",
    "MAX_INFLIGHT_ACTIONS",
    "MAX_JSON_NESTING",
    "CALLBACK_ON_START_DEADLINE_S",
    "CALLBACK_ON_EVENT_DEADLINE_S",
    "CALLBACK_ON_FINISH_DEADLINE_S",
    "HELLO_DEADLINE_S",
    "ARMED_DEADLINE_S",
    "ACK_BATCH_EVENTS",
    "ACK_MAX_DELAY_S",
    "RECONNECT_TOTAL_DEADLINE_S",
    "RECONNECT_MAX_ATTEMPTS",
    "RECONNECT_BACKOFFS_S",
    "STDIO_RETAIN_BYTES_PER_STREAM",
    "STDIO_RETAIN_HEAD_BYTES",
    "STDIO_RETAIN_TAIL_BYTES",
    "STDIO_TOTAL_DRAIN_LIMIT_BYTES",
    "FINISH_WAIT_S",
    "CLOSE_VOLUNTARY_WAIT_S",
    "SIGTERM_WAIT_S",
    "TOKEN_BITS",
    "TOKEN_BYTES",
    "TOKEN_REUSE_WINDOW_S",
    "MAX_UNIX_PATH_BYTES",
    "WORKLOAD_NONCE_BYTES",
    "SOCKET_ROOT_MODE",
    "WORKLOAD_DIR_MODE",
    "SOCKET_MODE",
)
