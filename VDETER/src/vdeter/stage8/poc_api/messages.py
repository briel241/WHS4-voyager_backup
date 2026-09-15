"""공통 envelope(§6.2)와 message별 최소 구조 검증.

- envelope는 고정 key 집합을 가진다. unknown top-level field는 minor 0에서 허용하지
  않는다(§6.2: "unknown optional field는 협상된 minor가 허용할 때만 무시").
- unknown message type·required field 불일치는 `POC_PROTOCOL_ERROR`다.
- HELLO/WELCOME도 같은 envelope를 쓰고 협상 세부는 body에 담는다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import protocol
from .errors import PocError, PocErrorCode

_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "type",
        "protocol",
        "session_id",
        "workload_id",
        "connection_epoch",
        "message_id",
        "body",
    }
)
_PROTOCOL_KEYS = frozenset({"major", "minor"})


def _protocol_error(message: str, **detail) -> PocError:
    return PocError(PocErrorCode.POC_PROTOCOL_ERROR, message, detail=detail)


def new_message_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    type: str
    protocol_major: int
    protocol_minor: int
    session_id: str
    workload_id: str
    connection_epoch: int
    message_id: str
    body: dict


def build_message(
    message_type: str,
    *,
    session_id: str,
    workload_id: str,
    connection_epoch: int,
    protocol_minor: int,
    body: dict,
    message_id: str | None = None,
) -> dict:
    """고정 envelope를 만든다(§6.2)."""

    return {
        "schema": protocol.MESSAGE_SCHEMA,
        "type": message_type,
        "protocol": {"major": protocol.PROTOCOL_MAJOR, "minor": int(protocol_minor)},
        "session_id": session_id,
        "workload_id": workload_id,
        "connection_epoch": int(connection_epoch),
        "message_id": message_id or new_message_id(),
        "body": dict(body),
    }


def parse_envelope(obj: dict) -> ParsedMessage:
    """envelope 구조를 검증한다. 위반은 `POC_PROTOCOL_ERROR`.

    session/workload/epoch/type의 **기대값 대조**는 상위 계층이 한다."""

    if not isinstance(obj, dict):
        raise _protocol_error("message가 object가 아닙니다")
    keys = set(obj)
    unknown = keys - _ENVELOPE_KEYS
    if unknown:
        raise _protocol_error("unknown envelope field", unknown=sorted(unknown))
    missing = _ENVELOPE_KEYS - keys
    if missing:
        raise _protocol_error("required envelope field 누락", missing=sorted(missing))

    if obj["schema"] != protocol.MESSAGE_SCHEMA:
        raise _protocol_error("schema 불일치", schema=obj["schema"])

    message_type = obj["type"]
    if not isinstance(message_type, str) or message_type not in protocol.ALL_MESSAGE_TYPES:
        raise _protocol_error("unknown message type", type=message_type)

    proto = obj["protocol"]
    if not isinstance(proto, dict) or set(proto) != _PROTOCOL_KEYS:
        raise _protocol_error("protocol 형식 오류")
    major, minor = proto["major"], proto["minor"]
    if not isinstance(major, int) or isinstance(major, bool):
        raise _protocol_error("protocol.major 형식 오류")
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise _protocol_error("protocol.minor 형식 오류")

    session_id = obj["session_id"]
    workload_id = obj["workload_id"]
    if not isinstance(session_id, str) or not session_id:
        raise _protocol_error("session_id 형식 오류")
    if not isinstance(workload_id, str) or not workload_id:
        raise _protocol_error("workload_id 형식 오류")

    epoch = obj["connection_epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise _protocol_error("connection_epoch 형식 오류")

    message_id = obj["message_id"]
    if not isinstance(message_id, str) or not message_id:
        raise _protocol_error("message_id 형식 오류")

    body = obj["body"]
    if not isinstance(body, dict):
        raise _protocol_error("body는 object여야 합니다")

    return ParsedMessage(
        type=message_type,
        protocol_major=major,
        protocol_minor=minor,
        session_id=session_id,
        workload_id=workload_id,
        connection_epoch=epoch,
        message_id=message_id,
        body=body,
    )


def require_body_field(body: dict, name: str, types: type | tuple) -> object:
    if name not in body:
        raise _protocol_error(f"body field 누락: {name}")
    value = body[name]
    if isinstance(value, bool) and bool not in (
        types if isinstance(types, tuple) else (types,)
    ):
        raise _protocol_error(f"body field 형식 오류: {name}")
    if not isinstance(value, types):
        raise _protocol_error(f"body field 형식 오류: {name}")
    return value


__all__ = (
    "ParsedMessage",
    "new_message_id",
    "build_message",
    "parse_envelope",
    "require_body_field",
)
