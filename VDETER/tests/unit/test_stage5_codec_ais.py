"""TC-STAGE5-010: AIS Authentication Scheme 1 codec.

요구사항: `STAGE5-REQ-10`, `STAGE5-REQ-11`, `STAGE5-REQ-18`
(`stage5_service_integration_baseline.md` §6.3, §6.6)
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from vdeter.presentation.codecs.ais_scheme1 import (
    AIS_SCHEME1_APPLICATION_BYTES,
    AIS_SCHEME1_WIRE_BYTES,
    AisAuthenticationScheme1Codec,
)

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

#: 승인 문서 §6.6에 고정된 독립 기대값.
AIS_APP_SHA256 = "fbe7fca9aefeec9de1f97e9287fb7af4c1393529c46d3d582e6c4ebbed32f3bf"
AIS_WIRE_SHA256 = "095d988bff18222e77415f0cfe516d66025cdca20d296096d67076c031779780"


def pack_test_fields(
    *,
    scheme_id=1,
    ais_message_id=1,
    mmsi=123_456_789,
    channel=0,
    slot=42,
    timestamp=1_735_689_600,
    fingerprint=bytes((1, 2, 3, 4, 5, 6)),
    signature=bytes(range(64)),
) -> bytes:
    """production codec과 독립적으로 test 자체 bit packer로 82-byte app bytes를 만든다."""

    body = 0
    for value, width in (
        (scheme_id, 8),
        (ais_message_id, 6),
        (mmsi, 30),
        (channel, 2),
        (slot, 12),
        (timestamp, 32),
        (int.from_bytes(fingerprint, "big"), 48),
        (int.from_bytes(signature, "big"), 512),
    ):
        body = (body << width) | (value & ((1 << width) - 1))
    return (body << 6).to_bytes(AIS_SCHEME1_APPLICATION_BYTES, "big")


def pack_test_wire(application: bytes) -> bytes:
    """독립 bit packer로 기대 wire bytes(84)를 만든다: 16-bit VPFI 7 || 6-bit MF 1 || body."""

    body = int.from_bytes(application, "big") >> 6
    wire = (7 << (6 + 650)) | (1 << 650) | body
    return wire.to_bytes(AIS_SCHEME1_WIRE_BYTES, "big")


class Stage5AisScheme1GoldenVectorTest(unittest.TestCase):
    def setUp(self):
        self.codec = AisAuthenticationScheme1Codec()
        self.application = (VECTOR_ROOT / "gv_stage5_pres_ais1_001.application.bin").read_bytes()
        self.wire = (VECTOR_ROOT / "gv_stage5_pres_ais1_001.wire.bin").read_bytes()

    def test_TC_STAGE5_010_artifacts_match_approved_sha256(self):
        self.assertEqual(82, len(self.application))
        self.assertEqual(84, len(self.wire))
        self.assertEqual(AIS_APP_SHA256, hashlib.sha256(self.application).hexdigest())
        self.assertEqual(AIS_WIRE_SHA256, hashlib.sha256(self.wire).hexdigest())
        manifest = json.loads((VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["vectors"]["GV-STAGE5-PRES-AIS1-001"]
        self.assertEqual(AIS_WIRE_SHA256, entry["wire"]["sha256"])
        self.assertEqual(AIS_APP_SHA256, entry["application"]["sha256"])

    def test_TC_STAGE5_010_wire_prefix_is_the_approved_672_bit_layout(self):
        self.assertEqual(bytes((0x00, 0x07)), self.wire[:2])
        self.assertEqual(1, (self.wire[2] >> 2) & 0x3F)
        # application 마지막 byte의 하위 6 bits는 zero padding이다.
        self.assertEqual(0, self.application[-1] & 0x3F)

    def test_TC_STAGE5_010_decode_returns_exact_application_bytes(self):
        decoded = self.codec.decode(self.wire)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(self.application, decoded.value)

    def test_TC_STAGE5_010_encode_matches_fixed_wire_bytes(self):
        encoded = self.codec.encode(self.application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(self.wire, encoded.value)
        self.assertEqual(AIS_WIRE_SHA256, hashlib.sha256(encoded.value).hexdigest())

    def test_TC_STAGE5_010_golden_vector_matches_independent_packer(self):
        expected_application = pack_test_fields(
            signature=bytes(range(64))
        )
        self.assertEqual(expected_application, self.application)
        self.assertEqual(pack_test_wire(self.application), self.wire)


class Stage5AisScheme1StructureTest(unittest.TestCase):
    def setUp(self):
        self.codec = AisAuthenticationScheme1Codec()

    def test_TC_STAGE5_010_independent_second_vector_round_trip(self):
        application = pack_test_fields(
            ais_message_id=63,
            mmsi=(1 << 30) - 1,
            channel=1,
            slot=2249,
            timestamp=0xFFFFFFFF,
            fingerprint=bytes((0xFF,) * 6),
            signature=bytes((0xAB,) * 64),
        )
        expected_wire = pack_test_wire(application)
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        # self round trip이 아니라 독립 packer의 기대 wire와 비교한다.
        self.assertEqual(expected_wire, encoded.value)
        decoded = self.codec.decode(expected_wire)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(application, decoded.value)

    def test_TC_STAGE5_010_application_length_must_be_82(self):
        for length in (0, 81, 83):
            self.assertTrue(self.codec.encode(bytes(length)).is_err, length)

    def test_TC_STAGE5_010_padding_bits_must_be_zero(self):
        application = bytearray(pack_test_fields())
        application[-1] |= 0x01
        self.assertTrue(self.codec.encode(bytes(application)).is_err)

    def test_TC_STAGE5_010_field_ranges_are_enforced(self):
        cases = (
            dict(scheme_id=2),
            dict(ais_message_id=0),
            dict(channel=2),
            dict(channel=3),
            dict(slot=2250),
        )
        for overrides in cases:
            with self.subTest(**overrides):
                application = pack_test_fields(**overrides)
                self.assertTrue(self.codec.encode(application).is_err)
                self.assertTrue(self.codec.decode(pack_test_wire(application)).is_err)

    def test_TC_STAGE5_010_misaligned_85_byte_wire_is_rejected(self):
        # 3-byte 정렬 header로 85 bytes를 만드는 구현 형태를 거부한다.
        application = pack_test_fields()
        aligned_wrong = bytes((0x00, 0x07, 0x04)) + application
        self.assertEqual(85, len(aligned_wrong))
        self.assertTrue(self.codec.decode(aligned_wrong).is_err)

    def test_TC_STAGE5_010_g1192_624_bit_fallback_is_rejected(self):
        # G1192 Table 15의 624-bit(78-byte body) 구조는 길이가 달라 decode를 거부한다.
        g1192_body_bits = 624 - 16 - 6  # 이전 참고 구조의 나머지 field bits
        legacy = (7 << (6 + g1192_body_bits)) | (1 << g1192_body_bits)
        legacy_wire = legacy.to_bytes((624 + 7) // 8, "big")
        self.assertNotEqual(AIS_SCHEME1_WIRE_BYTES, len(legacy_wire))
        self.assertTrue(self.codec.decode(legacy_wire).is_err)

    def test_TC_STAGE5_010_wire_truncation_is_rejected(self):
        wire = pack_test_wire(pack_test_fields())
        for length in (2, 3, 40, 83):
            self.assertTrue(self.codec.decode(wire[:length]).is_err, length)

    def test_TC_STAGE5_010_wrong_message_format_is_rejected(self):
        wire = bytearray(pack_test_wire(pack_test_fields()))
        wire[2] = (2 << 2) | (wire[2] & 0x03)  # Message format 2
        self.assertTrue(self.codec.decode(bytes(wire)).is_err)


if __name__ == "__main__":
    unittest.main()
