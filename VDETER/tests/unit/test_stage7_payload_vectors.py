"""Stage 7 필수 deterministic payload vector 검증 (`stage7` §18.1, §18.3).

`GV-STAGE7-PRES-X509-CERT-001`의 application/DER/wire bytes와 SHA-256,
chart-like opaque corpus와 relay 768B corpus의 digest를 기준선 값과 exact
비교하고 frozen certificate codec의 wire round-trip을 검증한다.
"""

import hashlib
import unittest

from vdeter.presentation.codecs.certificate_report import (
    CERTIFICATE_REPORT_WIRE_PREFIX,
    CertificateReportCodec,
)

X509_APP_HEX = (
    "010102030405060000873081843063a003020102020107300a06082a8648ce3d0403"
    "02300c310a300806035504030c0154301e170d3235303130313030303030305a170d"
    "3335303130313030303030305a300c310a300806035504030c01553011300906072a"
    "8648ce3d0201030400040102300a06082a8648ce3d040302031100000102030405060"
    "708090a0b0c0d0e0f"
)
X509_APP_SHA256 = "38130300284b78e7ac9b8d39887383ccaf3a5ba196c21faf5a309aa353eac373"
X509_DER_SHA256 = "1bbdeeff617bcf297d8f85375f8df460be5cba7eb66a406e9bb59f49b8a6b1e1"
X509_WIRE_SHA256 = "13edfba6d6b507177d27960e9d84e0d1b97a7e1a48068b4f6eefa7790bc72abe"

CHART_LIKE_SHA256 = "c8f5d0341d54d951a71b136e6e2afcb14d11ed8489a7ae126a8fee0df6ecf193"
RELAY_768_SHA256 = "f3a25aa93aa2fbba28d79260535bbd6a5eb0fc1c24a8b0f04e12b484c1dfe363"


class TC_STAGE7_X509_VECTOR(unittest.TestCase):
    def setUp(self):
        self.application = bytes.fromhex(X509_APP_HEX)

    def test_application_bytes_shape_and_sha(self):
        self.assertEqual(len(self.application), 145)
        self.assertEqual(
            hashlib.sha256(self.application).hexdigest(), X509_APP_SHA256
        )

    def test_der_certificate_slice_sha(self):
        # entry layout: type(1)+fingerprint(6)+reserved(1)+length(2 BE)+DER.
        self.assertEqual(self.application[0], 0x01)
        self.assertEqual(self.application[1:7], bytes.fromhex("010203040506"))
        self.assertEqual(self.application[7], 0x00)
        der_length = int.from_bytes(self.application[8:10], "big")
        self.assertEqual(der_length, 135)
        der = self.application[10 : 10 + der_length]
        self.assertEqual(len(der), 135)
        self.assertEqual(hashlib.sha256(der).hexdigest(), X509_DER_SHA256)

    def test_wire_encoding_prefix_and_sha(self):
        codec = CertificateReportCodec()
        encoded = codec.encode(self.application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        wire = encoded.value
        self.assertEqual(len(wire), 147)
        self.assertEqual(wire[:2], CERTIFICATE_REPORT_WIRE_PREFIX)
        self.assertEqual(wire[:2], b"\x00\x01")
        self.assertEqual(hashlib.sha256(wire).hexdigest(), X509_WIRE_SHA256)
        decoded = codec.decode(wire)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(decoded.value, self.application)


class TC_STAGE7_FIXTURE_CORPUS(unittest.TestCase):
    def test_chart_like_4096_corpus_sha(self):
        corpus = bytes(range(256)) * 16
        self.assertEqual(len(corpus), 4096)
        self.assertEqual(hashlib.sha256(corpus).hexdigest(), CHART_LIKE_SHA256)

    def test_relay_768_corpus_sha(self):
        corpus = bytes(range(256)) * 3
        self.assertEqual(len(corpus), 768)
        self.assertEqual(hashlib.sha256(corpus).hexdigest(), RELAY_768_SHA256)


if __name__ == "__main__":
    unittest.main()
