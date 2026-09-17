"""Stage 8 연결 API strict JSON·canonical codec 시험(명세 §6.1; REQ-06, REQ-07).

플랫폼 무관(순수 parser). Linux socket이 필요 없다.
"""

from __future__ import annotations

import json
import unittest

from vdeter.poc.api.codec import (
    CodecError,
    canonical_sha256,
    decode_bytes_field,
    decode_wire_packet,
    encode_bytes_field,
    encode_canonical,
    encode_wire_packet,
)
from vdeter.poc.api.limits import MAX_JSON_NESTING, MAX_WIRE_PACKET_BYTES


class CodecTest(unittest.TestCase):
    def test_S8_POC_API_REQ_06_decode_object(self):
        self.assertEqual(decode_wire_packet(b'{"a":1,"b":"x"}'), {"a": 1, "b": "x"})

    def test_S8_POC_API_REQ_06_reject_empty(self):
        with self.assertRaises(CodecError):
            decode_wire_packet(b"")

    def test_S8_POC_API_REQ_06_reject_non_object(self):
        for raw in (b"[1,2]", b"5", b'"string"', b"true", b"null"):
            with self.assertRaises(CodecError):
                decode_wire_packet(raw)

    def test_S8_POC_API_REQ_06_reject_duplicate_key(self):
        with self.assertRaises(CodecError):
            decode_wire_packet(b'{"a":1,"a":2}')

    def test_S8_POC_API_REQ_06_reject_nan_infinity(self):
        for raw in (b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}'):
            with self.assertRaises(CodecError):
                decode_wire_packet(raw)

    def test_S8_POC_API_REQ_06_reject_bad_utf8(self):
        with self.assertRaises(CodecError):
            decode_wire_packet(b'{"a":"' + b"\xff\xfe" + b'"}')

    def test_S8_POC_API_REQ_06_reject_concatenated_objects(self):
        with self.assertRaises(CodecError):
            decode_wire_packet(b'{"a":1}{"b":2}')

    def test_S8_POC_API_REQ_06_reject_bom(self):
        with self.assertRaises(CodecError):
            decode_wire_packet(b"\xef\xbb\xbf" + b'{"a":1}')

    def test_S8_POC_API_REQ_06_nesting_boundary(self):
        ok = 1
        for _ in range(MAX_JSON_NESTING):  # 16단계는 허용
            ok = {"x": ok}
        self.assertEqual(decode_wire_packet(json.dumps(ok).encode("utf-8")), ok)
        too_deep = 1
        for _ in range(MAX_JSON_NESTING + 1):  # 17단계는 거부
            too_deep = {"x": too_deep}
        with self.assertRaises(CodecError):
            decode_wire_packet(json.dumps(too_deep).encode("utf-8"))

    def test_S8_POC_API_REQ_07_wire_size_boundary(self):
        overhead = len(b'{"p":"') + len(b'"}')
        exact = b'{"p":"' + b"a" * (MAX_WIRE_PACKET_BYTES - overhead) + b'"}'
        self.assertEqual(len(exact), MAX_WIRE_PACKET_BYTES)
        self.assertEqual(decode_wire_packet(exact)["p"], "a" * (MAX_WIRE_PACKET_BYTES - overhead))
        over = b'{"p":"' + b"a" * (MAX_WIRE_PACKET_BYTES - overhead + 1) + b'"}'
        self.assertEqual(len(over), MAX_WIRE_PACKET_BYTES + 1)
        with self.assertRaises(CodecError):
            decode_wire_packet(over)

    def test_canonical_sorted_no_whitespace(self):
        self.assertEqual(encode_canonical({"b": 1, "a": 2}), b'{"a":2,"b":1}')
        self.assertEqual(encode_canonical([1, 2, {"z": 0}]), b'[1,2,{"z":0}]')

    def test_canonical_rejects_float(self):
        with self.assertRaises(CodecError):
            encode_canonical({"a": 1.5})

    def test_canonical_sha256_order_independent(self):
        self.assertEqual(canonical_sha256({"a": 1, "b": 2}), canonical_sha256({"b": 2, "a": 1}))
        self.assertNotEqual(canonical_sha256({"a": 1}), canonical_sha256({"a": 2}))

    def test_bytes_field_roundtrip(self):
        raw = bytes(range(256))
        self.assertEqual(decode_bytes_field(encode_bytes_field(raw)), raw)

    def test_bytes_field_rejects_bad_format(self):
        with self.assertRaises(CodecError):
            decode_bytes_field({"encoding": "hex", "data": "00"})
        with self.assertRaises(CodecError):
            decode_bytes_field("not a dict")
        with self.assertRaises(CodecError):
            decode_bytes_field({"encoding": "base64", "data": "!!!not base64!!!"})

    def test_encode_wire_rejects_oversize(self):
        big = {"p": "a" * (MAX_WIRE_PACKET_BYTES + 100)}
        with self.assertRaises(CodecError):
            encode_wire_packet(big)


if __name__ == "__main__":
    unittest.main()
