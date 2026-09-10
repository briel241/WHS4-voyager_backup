"""Stage 8 token·redaction·bounded stdio capture 시험(명세 §5, §13; REQ-05, REQ-21).

token FD·bounded stream은 플랫폼 무관(os.pipe는 Windows에도 있음).
"""

from __future__ import annotations

import os
import unittest

from vdeter.stage8._supervisor.capture import BoundedStream, WorkloadCapture
from vdeter.stage8._supervisor.token import (
    SecretRedactor,
    constant_time_equals,
    make_token_fd,
    new_token,
    token_hex,
)
from vdeter.stage8.poc_api.limits import (
    STDIO_RETAIN_BYTES_PER_STREAM,
    STDIO_TOTAL_DRAIN_LIMIT_BYTES,
    TOKEN_BYTES,
)


class TokenTest(unittest.TestCase):
    def test_new_token_is_256_bit_and_distinct(self):
        a, b = new_token(), new_token()
        self.assertEqual(len(a), TOKEN_BYTES)
        self.assertEqual(len(a), 32)
        self.assertNotEqual(a, b)

    def test_constant_time_equals(self):
        t = new_token()
        self.assertTrue(constant_time_equals(t, bytes(t)))
        self.assertFalse(constant_time_equals(t, new_token()))

    def test_S8_POC_API_REQ_05_token_fd_delivers_token(self):
        token = new_token()
        fd = make_token_fd(token)
        try:
            self.assertEqual(os.read(fd, 4096), token)
        finally:
            os.close(fd)

    def test_S8_POC_API_REQ_05_redactor_hides_token(self):
        token = new_token()
        redactor = SecretRedactor()
        redactor.register(token_hex(token))
        message = f"connect failed token={token_hex(token)} uid=1000"
        redacted = redactor.redact(message)
        self.assertNotIn(token_hex(token), redacted)
        self.assertIn("<redacted>", redacted)
        self.assertIn("uid=1000", redacted)


class BoundedCaptureTest(unittest.TestCase):
    def test_S8_POC_API_REQ_21_head_tail_retention(self):
        stream = BoundedStream()
        data = bytes([65 + (i % 26) for i in range(100 * 1024)])
        stream.feed(data)
        self.assertEqual(stream.emitted, 100 * 1024)
        self.assertEqual(stream.retained, STDIO_RETAIN_BYTES_PER_STREAM)
        self.assertEqual(stream.discarded, 100 * 1024 - STDIO_RETAIN_BYTES_PER_STREAM)
        retained = stream.retained_bytes()
        self.assertEqual(retained[:8], data[:8])  # head 보존
        self.assertEqual(retained[-8:], data[-8:])  # tail 보존
        self.assertTrue(stream.as_evidence()["truncated"])

    def test_small_stream_no_discard(self):
        stream = BoundedStream()
        stream.feed(b"hello world")
        self.assertEqual(stream.retained, 11)
        self.assertEqual(stream.discarded, 0)
        self.assertFalse(stream.as_evidence()["truncated"])
        self.assertEqual(stream.as_evidence()["text"], "hello world")

    def test_non_utf8_preserved_as_base64(self):
        stream = BoundedStream()
        stream.feed(b"\xff\xfe\x00\x01" * 4)
        evidence = stream.as_evidence()
        self.assertIn("base64", evidence)
        self.assertNotIn("text", evidence)

    def test_S8_POC_API_REQ_21_total_drain_overflow(self):
        cap = WorkloadCapture()
        cap.feed_stdout(b"x" * (200 * 1024))
        self.assertFalse(cap.overflowed)
        cap.feed_stderr(b"y" * (100 * 1024))
        self.assertTrue(cap.overflowed)  # 합산 300 KiB > 256 KiB
        self.assertGreater(cap.total_emitted, STDIO_TOTAL_DRAIN_LIMIT_BYTES)


if __name__ == "__main__":
    unittest.main()
