"""Stage 7 canonical JSON 규칙 시험 (`stage7_b210_system_baseline.md` §16)."""

import unittest

from vdeter.stage7.canonical_json import (
    CanonicalJsonError,
    canonical_sha256,
    decode_canonical_json,
    encode_canonical_json,
    is_canonical_bytes,
)


class TC_STAGE7_CANONICAL_JSON(unittest.TestCase):
    def test_key_order_is_utf8_byte_lexicographic(self):
        encoded = encode_canonical_json({"b": 1, "a": 2, "ab": 3})
        self.assertEqual(encoded, b'{"a":2,"ab":3,"b":1}')

    def test_no_whitespace_and_lowercase_literals(self):
        encoded = encode_canonical_json(
            {"t": True, "f": False, "n": None, "arr": [1, 2], "s": "x"}
        )
        self.assertEqual(encoded, b'{"arr":[1,2],"f":false,"n":null,"s":"x","t":true}')

    def test_control_characters_use_six_byte_lowercase_escapes(self):
        encoded = encode_canonical_json({"k": "a\nb\tc\x00"})
        self.assertEqual(encoded, b'{"k":"a\\u000ab\\u0009c\\u0000"}')

    def test_quote_and_backslash_two_byte_escapes(self):
        encoded = encode_canonical_json({"k": 'a"b\\c/d'})
        self.assertEqual(encoded, b'{"k":"a\\"b\\\\c/d"}')

    def test_unicode_not_escaped(self):
        encoded = encode_canonical_json({"k": "한글"})
        self.assertEqual(encoded, '{"k":"한글"}'.encode("utf-8"))

    def test_float_rejected(self):
        with self.assertRaises(CanonicalJsonError):
            encode_canonical_json({"k": 1.5})

    def test_non_nfc_string_rejected(self):
        decomposed = "é"  # NFD é
        with self.assertRaises(CanonicalJsonError):
            encode_canonical_json({"k": decomposed})

    def test_decoder_rejects_duplicate_keys(self):
        with self.assertRaises(CanonicalJsonError):
            decode_canonical_json(b'{"a":1,"a":2}')

    def test_decoder_rejects_float(self):
        with self.assertRaises(CanonicalJsonError):
            decode_canonical_json(b'{"a":1.5}')

    def test_decoder_rejects_bom(self):
        with self.assertRaises(CanonicalJsonError):
            decode_canonical_json(b'\xef\xbb\xbf{"a":1}')

    def test_roundtrip_is_canonical(self):
        value = {"z": [1, "two", None, True], "a": {"nested": 0}}
        encoded = encode_canonical_json(value)
        self.assertTrue(is_canonical_bytes(encoded))
        self.assertEqual(decode_canonical_json(encoded), value)

    def test_sha256_is_lowercase_hex(self):
        digest = canonical_sha256({"a": 1})
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())

    def test_negative_zero_normalizes(self):
        # Python int에는 -0이 없으므로 encoder 입력 층에서 자연히 0이 된다.
        self.assertEqual(encode_canonical_json(-0), b"0")


if __name__ == "__main__":
    unittest.main()
