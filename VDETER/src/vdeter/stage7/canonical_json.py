"""Stage 7 canonical JSON encoding (`stage7_b210_system_baseline.md` §16).

한 `SOCK_SEQPACKET` record와 digest 입력에 사용하는 결정적 JSON byte 규칙이다.

- 모든 key/string은 Unicode NFC이고 object key는 UTF-8 byte의 unsigned
  lexicographic 순서다.
- member/colon/comma 주변 whitespace와 BOM은 금지한다.
- U+0022는 `\\"`, U+005C는 `\\\\`, U+0000~U+001F는 항상 여섯 ASCII byte
  `\\u00xx`(lowercase hex)로 encode한다. `/`와 그 밖의 Unicode는 escape하지
  않는다.
- integer는 leading zero 없는 최소 decimal이고 `-0`·float·NaN/Infinity를
  금지하며 boolean/null은 lowercase다.

decoder는 normalization 전에 duplicate key를 탐지해 거부한다.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata


class CanonicalJsonError(ValueError):
    """canonical 규칙 위반."""


def _encode_string(value: str, out: bytearray) -> None:
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalJsonError("string이 Unicode NFC가 아닙니다")
    out.append(0x22)
    for character in value:
        code = ord(character)
        if character == '"':
            out.extend(b'\\"')
        elif character == "\\":
            out.extend(b"\\\\")
        elif code <= 0x1F:
            out.extend(b"\\u00" + format(code, "02x").encode("ascii"))
        else:
            out.extend(character.encode("utf-8"))
    out.append(0x22)


def _encode_value(value: object, out: bytearray) -> None:
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
        raise CanonicalJsonError("canonical JSON에는 float를 사용할 수 없습니다")
    if isinstance(value, int):
        out.extend(str(value).encode("ascii"))
        return
    if isinstance(value, str):
        _encode_string(value, out)
        return
    if isinstance(value, (list, tuple)):
        out.append(0x5B)
        for index, item in enumerate(value):
            if index:
                out.append(0x2C)
            _encode_value(item, out)
        out.append(0x5D)
        return
    if isinstance(value, dict):
        encoded_keys = []
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJsonError("object key는 문자열이어야 합니다")
            if unicodedata.normalize("NFC", key) != key:
                raise CanonicalJsonError("object key가 Unicode NFC가 아닙니다")
            encoded_keys.append((key.encode("utf-8"), key))
        encoded_keys.sort(key=lambda item: item[0])
        out.append(0x7B)
        for index, (_, key) in enumerate(encoded_keys):
            if index:
                out.append(0x2C)
            _encode_string(key, out)
            out.append(0x3A)
            _encode_value(value[key], out)
        out.append(0x7D)
        return
    raise CanonicalJsonError(f"canonical JSON으로 encode할 수 없는 값입니다: {type(value).__name__}")


def encode_canonical_json(value: object) -> bytes:
    """§16 규칙의 exact UTF-8 canonical bytes를 만든다."""

    out = bytearray()
    _encode_value(value, out)
    return bytes(out)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: object) -> str:
    return sha256_hex(encode_canonical_json(value))


class _DuplicateKeyGuard(dict):
    """`json` decoder의 object_pairs_hook에서 duplicate key를 즉시 거부한다."""


def _pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJsonError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_float(value: str):
    raise CanonicalJsonError("canonical JSON에는 float를 사용할 수 없습니다")


def _validate_decoded(value: object) -> None:
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise CanonicalJsonError("string이 Unicode NFC가 아닙니다")
        return
    if isinstance(value, list):
        for item in value:
            _validate_decoded(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if unicodedata.normalize("NFC", key) != key:
                raise CanonicalJsonError("object key가 Unicode NFC가 아닙니다")
            _validate_decoded(item)


def decode_canonical_json(data: bytes) -> object:
    """canonical bytes를 해석한다. duplicate key·float·NFC 위반은 거부한다.

    이 decoder는 관용적 JSON을 받되 float와 duplicate key, 비-NFC string을
    거부한다. byte-exact canonicality가 필요한 곳은 `encode_canonical_json`
    결과와의 재-encode 비교를 함께 사용한다.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise CanonicalJsonError("bytes 입력이 필요합니다")
    if data[:3] == b"\xef\xbb\xbf":
        raise CanonicalJsonError("BOM은 금지됩니다")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CanonicalJsonError(f"UTF-8이 아닙니다: {error}") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except CanonicalJsonError:
        raise
    except ValueError as error:
        raise CanonicalJsonError(f"JSON 해석 실패: {error}") from error
    _validate_decoded(value)
    return value


def is_canonical_bytes(data: bytes) -> bool:
    """byte-exact canonical 여부: decode 후 재-encode 결과가 원본과 같은지."""

    try:
        return encode_canonical_json(decode_canonical_json(data)) == bytes(data)
    except CanonicalJsonError:
        return False


__all__ = (
    "CanonicalJsonError",
    "canonical_sha256",
    "decode_canonical_json",
    "encode_canonical_json",
    "is_canonical_bytes",
    "sha256_hex",
)
