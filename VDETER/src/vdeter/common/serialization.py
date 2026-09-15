"""공개 DTO의 reference JSON 표현 (`stage0_baseline.md` §6.4).

JSON은 runtime transport 선택이 아니라 계약·log·test fixture용 reference 표현이다.
runtime 내부 함수 호출은 이 변환 없이 DTO와 bytes를 직접 사용한다.

규칙
- Enum은 문자열 값으로 표현한다.
- integer는 decimal JSON number로 표현한다.
- `bytes`는 `{"encoding": "base64", "data": "..."}` 형태로 표현한다.
- `TimePoint`는 네 field를 모두 보존한다.
- `SampleBlock`은 metadata와 `data`를 분리해 표현하며 native buffer reference를 담지 않는다.
- 알 수 없는 field는 조용히 버리지 않고 오류로 거부한다.
- `pickle`, Python class 경로, memory address, NumPy/GNU Radio/UHD/PMT 객체를 사용하지 않는다.
"""

from __future__ import annotations

import base64
import dataclasses
import typing
from enum import Enum

from .errors import Details
from .exceptions import SerializationError

BYTES_ENCODING_KEY = "encoding"
BYTES_DATA_KEY = "data"
BYTES_ENCODING_VALUE = "base64"
UNION_TAG_KEY = "kind"
UNION_VALUE_KEY = "value"

_NONE_TYPE = type(None)


def _payload_tag_table() -> dict:
    """`InternalEvent.payload` 같은 open field의 안정된 tag 표.

    Python class 경로가 아니라 계약 tag 문자열만 사용한다.
    """

    from .contracts.link import BuiltPacket, TransferRequirement
    from .contracts.mac import MacActionSet, PacketDecodeAction, TransmissionPlan
    from .contracts.message import DecodedMessages
    from .contracts.phy import BasebandBurst, PhyTxCompleted, RecoveredPacketSet
    from .contracts.presentation import ApplicationData, PresentationData
    from .contracts.radio import (
        RadioRxWindowCompleted,
        RadioSamplesReceived,
        RadioStatusSnapshot,
        RadioTxCompleted,
    )

    return {
        "PRESENTATION_DATA": PresentationData,
        "TRANSFER_REQUIREMENT": TransferRequirement,
        "TRANSMISSION_PLAN": TransmissionPlan,
        "BUILT_PACKET": BuiltPacket,
        "BASEBAND_BURST": BasebandBurst,
        "PHY_TX_COMPLETED": PhyTxCompleted,
        "RADIO_TX_COMPLETED": RadioTxCompleted,
        "RADIO_SAMPLES_RECEIVED": RadioSamplesReceived,
        "RADIO_STATUS_SNAPSHOT": RadioStatusSnapshot,
        "RADIO_RX_WINDOW_COMPLETED": RadioRxWindowCompleted,
        "RECOVERED_PACKET_SET": RecoveredPacketSet,
        "PACKET_DECODE_ACTION": PacketDecodeAction,
        "DECODED_MESSAGES": DecodedMessages,
        "MAC_ACTION_SET": MacActionSet,
        "APPLICATION_DATA": ApplicationData,
    }


_TAG_TO_TYPE = None
_TYPE_TO_TAG = None
_MESSAGE_TAG_TO_TYPE = None
_MESSAGE_TYPE_TO_TAG = None


def _tag_tables():
    global _TAG_TO_TYPE, _TYPE_TO_TAG
    if _TAG_TO_TYPE is None:
        _TAG_TO_TYPE = _payload_tag_table()
        _TYPE_TO_TAG = {value: key for key, value in _TAG_TO_TYPE.items()}
    return _TAG_TO_TYPE, _TYPE_TO_TAG


def _message_tag_tables():
    """DR-STAGE1-036의 안정된 MessageBody discriminator 표."""

    global _MESSAGE_TAG_TO_TYPE, _MESSAGE_TYPE_TO_TAG
    if _MESSAGE_TAG_TO_TYPE is None:
        from .contracts.message import MESSAGE_TYPE_TO_BODY_TYPE

        _MESSAGE_TAG_TO_TYPE = {
            f"TYPE_{message_type}": body_type
            for message_type, body_type in MESSAGE_TYPE_TO_BODY_TYPE.items()
        }
        _MESSAGE_TYPE_TO_TAG = {
            body_type: tag for tag, body_type in _MESSAGE_TAG_TO_TYPE.items()
        }
    return _MESSAGE_TAG_TO_TYPE, _MESSAGE_TYPE_TO_TAG


def encode_bytes(value: bytes) -> dict:
    return {
        BYTES_ENCODING_KEY: BYTES_ENCODING_VALUE,
        BYTES_DATA_KEY: base64.b64encode(value).decode("ascii"),
    }


def decode_bytes(data: object) -> bytes:
    if not isinstance(data, dict):
        raise SerializationError("bytes 표현은 object여야 합니다")
    unknown = set(data) - {BYTES_ENCODING_KEY, BYTES_DATA_KEY}
    if unknown:
        raise SerializationError(f"bytes 표현에 알 수 없는 field가 있습니다: {sorted(unknown)}")
    if data.get(BYTES_ENCODING_KEY) != BYTES_ENCODING_VALUE:
        raise SerializationError("bytes 표현의 encoding은 base64여야 합니다")
    raw = data.get(BYTES_DATA_KEY)
    if not isinstance(raw, str):
        raise SerializationError("bytes 표현의 data는 문자열이어야 합니다")
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as error:  # base64 오류는 binascii.Error를 포함한다
        raise SerializationError(f"base64 data를 해석할 수 없습니다: {error}") from error


def _encode_details(details: Details) -> dict:
    return {"entries": details.as_dict()}


def _decode_details(data: object) -> Details:
    if not isinstance(data, dict):
        raise SerializationError("Details 표현은 object여야 합니다")
    unknown = set(data) - {"entries"}
    if unknown:
        raise SerializationError(f"Details 표현에 알 수 없는 field가 있습니다: {sorted(unknown)}")
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        raise SerializationError("Details.entries 표현은 object여야 합니다")
    return Details.from_mapping(entries)


def encode_value(value: object) -> object:
    """단일 값을 reference JSON 값으로 변환한다."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        raise SerializationError("공개 계약에는 floating-point 값을 사용하지 않습니다")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return encode_bytes(bytes(value))
    if isinstance(value, Details):
        return _encode_details(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_reference_json(value)
    if isinstance(value, (tuple, list)):
        return [encode_value(item) for item in value]
    raise SerializationError(
        f"portable하지 않은 값입니다: {type(value).__name__}. "
        "logger, callback, queue, thread, NumPy/GNU Radio/UHD/PMT 객체는 담을 수 없습니다."
    )


def to_reference_json(instance: object) -> dict:
    """frozen dataclass DTO를 reference JSON object로 변환한다."""

    if not dataclasses.is_dataclass(instance) or isinstance(instance, type):
        raise SerializationError("to_reference_json에는 dataclass instance가 필요합니다")
    if isinstance(instance, Details):
        return _encode_details(instance)
    result = {}
    for field in dataclasses.fields(instance):
        value = getattr(instance, field.name)
        if field.name == "payload" and _is_open_payload_field(type(instance), field):
            result[field.name] = _encode_tagged(value)
            continue
        if _is_message_body_field(type(instance), field):
            result[field.name] = _encode_message_body(value)
            continue
        result[field.name] = encode_value(value)
    return result


def _is_open_payload_field(owner: type, field: dataclasses.Field) -> bool:
    """type이 고정되지 않은 event payload field인지 판정한다."""

    return owner.__name__ == "InternalEvent" and field.name == "payload"


def _is_message_body_field(owner: type, field: dataclasses.Field) -> bool:
    return owner.__name__ in {"DecodedMessage", "MessageEncodeRequest"} and field.name == "body"


def _encode_message_body(value: object) -> dict:
    _, type_to_tag = _message_tag_tables()
    tag = type_to_tag.get(type(value))
    if tag is None:
        raise SerializationError(f"등록되지 않은 MessageBody type입니다: {type(value).__name__}")
    return {UNION_TAG_KEY: tag, UNION_VALUE_KEY: to_reference_json(value)}


def _decode_message_body(data: object) -> object:
    if not isinstance(data, dict):
        raise SerializationError("MessageBody 표현은 object여야 합니다")
    unknown = set(data) - {UNION_TAG_KEY, UNION_VALUE_KEY}
    if unknown:
        raise SerializationError(
            f"MessageBody 표현에 알 수 없는 field가 있습니다: {sorted(unknown)}"
        )
    tag_to_type, _ = _message_tag_tables()
    tag = data.get(UNION_TAG_KEY)
    target = tag_to_type.get(tag)
    if target is None:
        raise SerializationError(f"등록되지 않은 MessageBody tag입니다: {tag!r}")
    return from_reference_json(target, data.get(UNION_VALUE_KEY))


def _encode_tagged(value: object) -> object:
    if value is None:
        return None
    _, type_to_tag = _tag_tables()
    tag = type_to_tag.get(type(value))
    if tag is None:
        raise SerializationError(
            f"등록되지 않은 event payload type입니다: {type(value).__name__}"
        )
    return {UNION_TAG_KEY: tag, UNION_VALUE_KEY: to_reference_json(value)}


def _decode_tagged(data: object) -> object:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise SerializationError("event payload 표현은 object여야 합니다")
    unknown = set(data) - {UNION_TAG_KEY, UNION_VALUE_KEY}
    if unknown:
        raise SerializationError(
            f"event payload 표현에 알 수 없는 field가 있습니다: {sorted(unknown)}"
        )
    tag_to_type, _ = _tag_tables()
    tag = data.get(UNION_TAG_KEY)
    target = tag_to_type.get(tag)
    if target is None:
        raise SerializationError(f"등록되지 않은 event payload tag입니다: {tag!r}")
    return from_reference_json(target, data.get(UNION_VALUE_KEY))


def _unwrap_optional(annotation):
    origin = typing.get_origin(annotation)
    if origin is typing.Union or (origin is not None and str(origin) == "types.UnionType"):
        args = [arg for arg in typing.get_args(annotation) if arg is not _NONE_TYPE]
        if len(args) == 1:
            return args[0], True
        raise SerializationError(f"복수 type union은 지원하지 않습니다: {annotation}")
    import types as _types

    if isinstance(annotation, _types.UnionType):  # Python 3.10+ `X | None`
        args = [arg for arg in typing.get_args(annotation) if arg is not _NONE_TYPE]
        if len(args) == 1:
            return args[0], True
        raise SerializationError(f"복수 type union은 지원하지 않습니다: {annotation}")
    return annotation, False


def decode_value(annotation, data: object):
    """annotation이 지시하는 type으로 reference JSON 값을 복원한다."""

    annotation, optional = _unwrap_optional(annotation)
    if data is None:
        if optional:
            return None
        raise SerializationError(f"필수 값이 null입니다: {annotation}")

    origin = typing.get_origin(annotation)
    if origin in (tuple, list):
        args = typing.get_args(annotation)
        if not args:
            raise SerializationError("collection annotation에는 원소 type이 필요합니다")
        element = args[0]
        if not isinstance(data, list):
            raise SerializationError("collection 표현은 배열이어야 합니다")
        return tuple(decode_value(element, item) for item in data)

    if annotation is Details:
        return _decode_details(data)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(data)
        except ValueError as error:
            raise SerializationError(f"{annotation.__name__} 값이 아닙니다: {data!r}") from error
    if annotation is bytes:
        return decode_bytes(data)
    if annotation is bool:
        if not isinstance(data, bool):
            raise SerializationError(f"bool이 아닙니다: {data!r}")
        return data
    if annotation is int:
        if isinstance(data, bool) or not isinstance(data, int):
            raise SerializationError(f"정수가 아닙니다: {data!r}")
        return data
    if annotation is str:
        if not isinstance(data, str):
            raise SerializationError(f"문자열이 아닙니다: {data!r}")
        return data
    if annotation is object:
        return _decode_tagged(data)
    if dataclasses.is_dataclass(annotation):
        return from_reference_json(annotation, data)
    raise SerializationError(f"지원하지 않는 annotation입니다: {annotation}")


def from_reference_json(target: type, data: object):
    """reference JSON object에서 dataclass DTO를 복원한다."""

    if not dataclasses.is_dataclass(target):
        raise SerializationError("from_reference_json에는 dataclass type이 필요합니다")
    if target is Details:
        return _decode_details(data)
    if not isinstance(data, dict):
        raise SerializationError(f"{target.__name__} 표현은 object여야 합니다")

    hints = typing.get_type_hints(target)
    fields = {field.name: field for field in dataclasses.fields(target)}
    unknown = set(data) - set(fields)
    if unknown:
        raise SerializationError(
            f"{target.__name__} 표현에 알 수 없는 field가 있습니다: {sorted(unknown)}"
        )

    kwargs = {}
    for name, field in fields.items():
        if name not in data:
            if (
                field.default is not dataclasses.MISSING
                or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
            ):
                continue
            raise SerializationError(f"{target.__name__}: 필수 field {name}이 없습니다")
        annotation = hints.get(name, field.type)
        if _is_open_payload_field(target, field):
            kwargs[name] = _decode_tagged(data[name])
            continue
        if _is_message_body_field(target, field):
            kwargs[name] = _decode_message_body(data[name])
            continue
        kwargs[name] = decode_value(annotation, data[name])
    try:
        return target(**kwargs)
    except SerializationError:
        raise
    except Exception as error:
        raise SerializationError(f"{target.__name__} 복원 실패: {error}") from error
