"""protocol 상수: schema 문자열, version, message type, capability/trigger allowlist.

명세 §6.2(envelope), §6.3(message 종류), §7(협상), `POC-API-D04`.
모든 상수의 단일 정의 위치다. 다른 module은 여기서만 값을 가져온다.
"""

from __future__ import annotations

#: envelope `schema` 값(§6.2).
MESSAGE_SCHEMA = "vdeter.stage8.poc-api.message.v1"

#: manifest schema(실행 명세 §6.3). HELLO의 manifest descriptor에 실린다.
MANIFEST_SCHEMA = "vdeter.stage8.poc-manifest.v1"

#: protocol major는 반드시 일치, minor는 공통 범위로 협상(`POC-API-D04`).
PROTOCOL_MAJOR = 1
#: server가 지원하는 minor 범위(포함, 포함). 현재 minor 0만 지원한다.
SERVER_MIN_MINOR = 0
SERVER_MAX_MINOR = 0

#: client(SDK) 기본 지원 minor 범위.
CLIENT_MIN_MINOR = 0
CLIENT_MAX_MINOR = 0


# --- message type (§6.3) -----------------------------------------------------

# client -> server
HELLO = "HELLO"
ARMED = "ARMED"
EVENT_ACK = "EVENT_ACK"
ACTION_SUBMIT = "ACTION_SUBMIT"
FINISH_RESULT = "FINISH_RESULT"

# server -> client
WELCOME = "WELCOME"
START = "START"
EVENT = "EVENT"
ACTION_RESULT = "ACTION_RESULT"
ACTION_TERMINAL = "ACTION_TERMINAL"
FINISH = "FINISH"
CLOSE = "CLOSE"

# 양방향
ERROR = "ERROR"

CLIENT_TO_SERVER_TYPES = frozenset(
    {HELLO, ARMED, EVENT_ACK, ACTION_SUBMIT, FINISH_RESULT, ERROR}
)
SERVER_TO_CLIENT_TYPES = frozenset(
    {WELCOME, START, EVENT, ACTION_RESULT, ACTION_TERMINAL, FINISH, CLOSE, ERROR}
)
ALL_MESSAGE_TYPES = CLIENT_TO_SERVER_TYPES | SERVER_TO_CLIENT_TYPES

#: 정상 상태 확인용 주기적 PING은 정의하지 않는다(§6.3). socket HUP·child exit·
#: deadline으로 생존을 판단한다.


# --- capability / trigger allowlist (§7.2, 실행 명세 §6.3) --------------------

#: 논리 target(§9.4). Physical Channel은 server/runtime이 유도한다(ROC=lower, MASS-A=upper).
LOGICAL_TARGETS = ("ROC", "MASS-A")

#: dual-channel 동시 관측 capability 이름. server 기본 capability 목록에는 넣지
#: 않고(§7.2.4, `POC-API-D13`) WSM-G1-호환 production 구현이 설치된 경우에만 광고한다.
OBSERVE_DUAL_PHYSICAL_CHANNEL = "observe_dual_physical_channel"

#: system이 아는 전체 capability 이름(협상은 manifest allowlist ∩ server 지원 ∩ client 요청).
KNOWN_CAPABILITIES = frozenset(
    {
        "observe_packet",
        "replay_packet",
        "mutate_packet",
        "forge_packet",
        "duplicate_packet",
        "schedule_relative",
        OBSERVE_DUAL_PHYSICAL_CHANNEL,
    }
)

#: dual-channel을 제외한, WSM-G1 production 미설치에서도 server가 광고하는 기본 capability.
DEFAULT_SERVER_CAPABILITIES = frozenset(KNOWN_CAPABILITIES - {OBSERVE_DUAL_PHYSICAL_CHANNEL})

KNOWN_TRIGGERS = frozenset(
    {
        "SCENARIO_STARTED",
        "STEP_STARTED",
        "PACKET_OBSERVED",
        "TRANSFER_TERMINAL",
        "SCENARIO_TERMINAL",
    }
)
DEFAULT_SERVER_TRIGGERS = frozenset(KNOWN_TRIGGERS)


__all__ = (
    "MESSAGE_SCHEMA",
    "MANIFEST_SCHEMA",
    "PROTOCOL_MAJOR",
    "SERVER_MIN_MINOR",
    "SERVER_MAX_MINOR",
    "CLIENT_MIN_MINOR",
    "CLIENT_MAX_MINOR",
    "HELLO",
    "WELCOME",
    "START",
    "ARMED",
    "EVENT",
    "EVENT_ACK",
    "ACTION_SUBMIT",
    "ACTION_RESULT",
    "ACTION_TERMINAL",
    "FINISH",
    "FINISH_RESULT",
    "CLOSE",
    "ERROR",
    "CLIENT_TO_SERVER_TYPES",
    "SERVER_TO_CLIENT_TYPES",
    "ALL_MESSAGE_TYPES",
    "LOGICAL_TARGETS",
    "OBSERVE_DUAL_PHYSICAL_CHANNEL",
    "KNOWN_CAPABILITIES",
    "DEFAULT_SERVER_CAPABILITIES",
    "KNOWN_TRIGGERS",
    "DEFAULT_SERVER_TRIGGERS",
)
