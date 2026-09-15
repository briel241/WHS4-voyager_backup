"""strict JSON parser와 canonical encoding(명세 §6.1).

- seqpacket 하나 = UTF-8로 encoding된 JSON object 하나.
- top-level array·scalar·빈 packet·여러 JSON object 연결은 거부한다.
- UTF-8 오류, duplicate key, `NaN`/`Infinity`/`-Infinity`, 16단계 초과 nesting을 거부한다.
- 256 KiB 상한은 base64·envelope를 포함한 wire packet 전체에 parse 전에 적용한다.
- byte field는 `{"encoding": "base64", "data": "..."}` 형태를 사용한다.
- canonical form은 정렬된 key와 불필요한 whitespace가 없는 결정적 JSON이며, 결정성을
  위해 float를 담지 않는다(이 protocol의 모든 수치는 integer다).

`MSG_TRUNC`(수신 buffer보다 큰 packet)의 부분 parse 금지는 transport 계층이
처리한다(여기서는 이미 완결된 datagram만 받는다).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from .limits import MAX_JSON_NESTING, MAX_WIRE_PACKET_BYTES


class CodecError(ValueError):
    """strict JSON/canonical 규칙 위반."""


def _reject_constant(_value: str):
    raise CodecError("NaN/Infinity는 금지됩니다")


def _pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CodecError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _max_depth(value: object) -> int:
    """container nesting 최대 깊이(iterative; Python recursion 한계 회피).

    top-level object가 depth 1이다."""

    max_seen = 0
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if depth > max_seen:
                max_seen = depth
            for item in node.values():
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            if depth > max_seen:
                max_seen = depth
            for item in node:
                stack.append((item, depth + 1))
    return max_seen


def decode_wire_packet(data: bytes) -> dict:
    """wire packet bytes → JSON object(dict). 위반 시 :class:`CodecError`.

    256 KiB 상한을 parse 전에 적용하고, top-level이 object가 아니거나 nesting이
    16단계를 넘으면 거부한다.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise CodecError("bytes 입력이 필요합니다")
    if len(data) == 0:
        raise CodecError("빈 packet은 거부합니다")
    if len(data) > MAX_WIRE_PACKET_BYTES:
        # parse 전에 크기를 판정한다(§6.1, §10.1).
        raise CodecError(
            f"wire packet {len(data)} byte가 상한 {MAX_WIRE_PACKET_BYTES}를 넘습니다"
        )
    if data[:3] == b"\xef\xbb\xbf":
        raise CodecError("BOM은 금지됩니다")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CodecError(f"UTF-8이 아닙니다: {error}") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
        )
    except CodecError:
        raise
    except RecursionError as error:
        raise CodecError("nesting이 너무 깊습니다") from error
    except ValueError as error:
        # 여러 object 연결·trailing garbage·malformed → JSONDecodeError(ValueError).
        raise CodecError(f"JSON 해석 실패: {error}") from error
    if not isinstance(value, dict):
        raise CodecError("top-level은 JSON object여야 합니다")
    if _max_depth(value) > MAX_JSON_NESTING:
        raise CodecError(f"nesting이 {MAX_JSON_NESTING}단계를 넘습니다")
    return value


def _encode_canonical(value: object, out: bytearray) -> None:
    if value is None:
        out.extend(b"null")
        return
    if value is True:
        out.extend(b"true")
        return
    if value is False:
        out.extend(b"false")
        return
    if isinstance(value, float):
        raise CodecError("canonical JSON에는 float를 사용할 수 없습니다")
    if isinstance(value, int):
        out.extend(str(value).encode("ascii"))
        return
    if isinstance(value, str):
        out.extend(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return
    if isinstance(value, (list, tuple)):
        out.append(0x5B)
        for index, item in enumerate(value):
            if index:
                out.append(0x2C)
            _encode_canonical(item, out)
        out.append(0x5D)
        return
    if isinstance(value, dict):
        keyed = []
        for key in value:
            if not isinstance(key, str):
                raise CodecError("object key는 문자열이어야 합니다")
            keyed.append((key.encode("utf-8"), key))
        keyed.sort(key=lambda item: item[0])
        out.append(0x7B)
        for index, (_, key) in enumerate(keyed):
            if index:
                out.append(0x2C)
            out.extend(json.dumps(key, ensure_ascii=False).encode("utf-8"))
            out.append(0x3A)
            _encode_canonical(value[key], out)
        out.append(0x7D)
        return
    raise CodecError(f"canonical JSON으로 encode할 수 없습니다: {type(value).__name__}")


def encode_canonical(value: object) -> bytes:
    """정렬된 key·최소 whitespace의 결정적 UTF-8 canonical bytes."""

    out = bytearray()
    _encode_canonical(value, out)
    return bytes(out)


def encode_wire_packet(value: dict) -> bytes:
    """전송용 wire bytes(canonical). 256 KiB 상한을 초과하면 :class:`CodecError`."""

    if not isinstance(value, dict):
        raise CodecError("wire packet은 JSON object여야 합니다")
    data = encode_canonical(value)
    if len(data) > MAX_WIRE_PACKET_BYTES:
        raise CodecError(
            f"wire packet {len(data)} byte가 상한 {MAX_WIRE_PACKET_BYTES}를 넘습니다"
        )
    return data


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(encode_canonical(value)).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_bytes_field(data: bytes) -> dict:
    """byte field를 `{"encoding": "base64", "data": "..."}`로 encode(§6.1)."""

    return {"encoding": "base64", "data": base64.b64encode(bytes(data)).decode("ascii")}


def decode_bytes_field(value: object) -> bytes:
    """base64 byte field를 bytes로 decode. 형식 위반은 :class:`CodecError`."""

    if not isinstance(value, dict) or value.get("encoding") != "base64":
        raise CodecError("base64 byte field 형식이 아닙니다")
    raw = value.get("data")
    if not isinstance(raw, str):
        raise CodecError("byte field data는 문자열이어야 합니다")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CodecError(f"base64 decode 실패: {error}") from error


__all__ = (
    "CodecError",
    "decode_wire_packet",
    "encode_wire_packet",
    "encode_canonical",
    "canonical_sha256",
    "sha256_hex",
    "encode_bytes_field",
    "decode_bytes_field",
)
