"""
VDE-TER Service API 어댑터

`docs/vde_service_api(prototype).md`가 정한 공개 계약과 우리 행위자를 잇는다.
행위자는 `Outbound`를 만들고, 이 모듈이 그것을 `SendRequest` 형태로 바꾼다.
반대로 Service가 올려준 `ReceivedMessage`를 행위자의 receive()로 넘긴다.

왜 어댑터를 따로 두는가
    API 계약 6장이 정한 경계 때문이다.

        MASS/ROC는 Link ID, Message Type, CRC, FEC, Burst, slot과 재전송
        절차를 전달하거나 구현하지 않는다.

    지금 시뮬레이터는 Type 92/93을 직접 만들지만, 그것은 링크 계층이 아직
    Service로 감싸이지 않아서다. 이 어댑터를 거치면 행위자 코드는 Type을
    전혀 모르는 상태로 남고, 실제 Service가 준비되면 그대로 갈아끼운다.

미해결 사항
    API 계약 1장의 data_type 네 가지(TBB, AIS_AUTH_INTEGRITY,
    ELECTRONIC_CHART, X509_CERTIFICATE)에는 MASS/ROC 운항 메시지
    (위치보고·항로지시·응답)에 해당하는 값이 없다. 분류가 합의되기 전까지
    data_type은 None으로 두며, 임의 값을 만들어 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .outbound import BROADCAST_ID, Outbound


# 행위자가 갖춰야 할 최소 인터페이스. MassAgent와 RocAgent가 이를 만족한다.
class Agent(Protocol):
    mmsi: int

    def tick(self, now: float) -> list[Outbound]: ...

    def receive(self, payload: bytes, source_id: int, now: float) -> list[Outbound]: ...


# API 계약 3.2.1의 전송 상태
STATE_QUEUED = "QUEUED"
STATE_TRANSMITTING = "TRANSMITTING"
STATE_WAITING_ACK = "WAITING_ACK"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"

# API 계약 3.2.3의 최소 오류 코드
ERROR_CODES = (
    "INVALID_REQUEST",
    "UNSUPPORTED_DATA_TYPE",
    "PAYLOAD_TOO_LARGE",
    "UNKNOWN_TRANSFER",
    "SERVICE_NOT_OPEN",
    "PROTOCOL_ERROR",
    "TIMEOUT",
    "DEVICE_ERROR",
)


# Service가 on_message로 올려주는 수신 결과 (API 계약 3.2.2)
@dataclass
class ReceivedMessage:
    message_id: str
    source_id: int
    destination_id: int
    payload: bytes
    data_type: str | None = None
    content_type: str | None = None


# Service가 on_status로 알려주는 전송 상태 (API 계약 3.2.1)
@dataclass
class TransferStatus:
    transfer_id: str
    state: str
    reason: str | None = None


# Outbound를 send()에 넘길 SendRequest dict로 바꾼다.
#
# Link ID, Message Type, Session ID, Retransmission ID, VPFI, CRC, FEC는
# 넣지 않는다. API 계약 3.1.2가 금지한 항목이다.
def to_send_request(outbound: Outbound, source_id: int) -> dict[str, Any]:
    request: dict[str, Any] = {
        "source_id": source_id,
        "destination_id": outbound.destination_id,
        "data_type": outbound.data_type,
        "payload": outbound.payload,
        "ack_required": outbound.ack_required,
    }
    if outbound.content_type is not None:
        request["content_type"] = outbound.content_type
    return request


# Service가 올려준 수신 메시지를 행위자에게 넘기고, 답신을 SendRequest로 바꾼다.
#
# API 계약 4.2에 따라 Service는 CRC·protocol validation과 재조립을 마친
# payload만 올려주므로, 여기서 그 검사를 다시 하지 않는다.
def dispatch(
    agent: Agent, message: ReceivedMessage, now: float = 0.0
) -> list[dict[str, Any]]:
    replies = agent.receive(message.payload, message.source_id, now)
    return [to_send_request(reply, agent.mmsi) for reply in replies]


# 행위자의 tick 결과를 SendRequest 목록으로 바꾼다.
def poll(agent: Agent, now: float) -> list[dict[str, Any]]:
    return [to_send_request(outbound, agent.mmsi) for outbound in agent.tick(now)]


# SendRequest가 계약을 지키는지 확인한다. Service 구현 전 자체 점검용이다.
# 반환값은 위반 사유 목록이며, 비어 있으면 유효하다.
def validate_send_request(request: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    for field in ("source_id", "destination_id", "data_type", "payload", "ack_required"):
        if field not in request:
            problems.append(f"필수 필드 누락: {field}")

    # 계약 3.1.2 — broadcast에는 ack_required=false를 쓴다.
    if request.get("destination_id") == BROADCAST_ID and request.get("ack_required"):
        problems.append("broadcast(destination_id=0)에는 ack_required=False를 써야 한다")

    # 계약 3.1.2 — 링크 계층 값을 넣으면 안 된다.
    forbidden = (
        "link_id",
        "message_type",
        "session_id",
        "retransmission_id",
        "vpfi",
        "crc",
        "fec",
        "burst",
    )
    for field in forbidden:
        if field in request:
            problems.append(f"API 사용자가 전달할 수 없는 필드: {field}")

    if not isinstance(request.get("payload", b""), (bytes, bytearray)):
        problems.append("payload는 bytes여야 한다")

    return problems
