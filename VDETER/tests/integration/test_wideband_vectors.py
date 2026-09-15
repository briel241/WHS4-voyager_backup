"""v2 vector corpus 결속 시험 (TC-PHY17-007/008·TC-PHY19-007/008 부분).

요구사항: 공통 §9.1, link §13 — 독립 generator가 만든 `tests/vectors/phy/link17·19`
v2 artifact가 production 구현과 byte-exact로 일치하고(`tx_samples_v2.cf32le`),
`SHA256SUMS`가 실제 파일 bytes와 일치하며, 결정적 RX sample 10개가 production
수신기에서 canonical Packet으로 byte-exact 복원되는지 검증한다.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE
from vdeter.common.contracts.phy import PhyDecodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy import wideband
from vdeter.phy.burst.rrc import to_cf32le
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry

VECTOR_ROOT = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "phy"

RX_CASES = (
    "ideal", "timing_max", "cfo_min", "cfo_max", "sro_min",
    "sro_max", "gain_min", "gain_max", "phase_wrap", "mixed",
)
#: §7.7 표의 (delta, sro_ppm) — timestamp 기대값 검사용.
CASE_TIMING = {
    "ideal": (0.0, 0), "timing_max": (63.0 / 16.0, 0), "cfo_min": (0.0, 0),
    "cfo_max": (0.0, 0), "sro_min": (0.0, -10), "sro_max": (0.0, 10),
    "gain_min": (0.0, 0), "gain_max": (0.0, 0), "phase_wrap": (0.0, 0),
    "mixed": (23.0 / 16.0, 6),
}
SAMPLE_PERIOD_NS = 1_000_000_000.0 / wideband.SAMPLE_RATE_HZ


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


class _VectorMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x55))
        cls.directory = VECTOR_ROOT / f"link{cls.link_id}"
        cls.packet = _canonical_packet(cls.link_id)

    def test_sha256sums_match_artifact_bytes(self):
        sums = (self.directory / "SHA256SUMS").read_bytes().decode("ascii")
        lines = sums.splitlines()
        self.assertTrue(sums.endswith("\n"))
        paths = []
        for line in lines:
            digest, path = line.split("  ", 1)
            paths.append(path)
            self.assertFalse(path.startswith("/") or ".." in path or "\\" in path)
            data = (self.directory / path).read_bytes()
            self.assertEqual(digest, hashlib.sha256(data).hexdigest(), msg=path)
        # path UTF-8 byte 오름차순·중복 금지 (§9.1)
        encoded = [path.encode() for path in paths]
        self.assertEqual(sorted(encoded), encoded)
        self.assertEqual(len(set(encoded)), len(encoded))

    def test_canonical_packet_hex_matches(self):
        stored = bytes.fromhex(
            (self.directory / "packet_type0_v2.hex").read_text("ascii").strip()
        )
        self.assertEqual(self.packet, stored)

    def test_tx_samples_match_the_production_encoder_byte_exact(self):
        production = to_cf32le(wideband.encode_slot(self.link_id, self.packet).samples)
        stored = (self.directory / "tx_samples_v2.cf32le").read_bytes()
        self.assertEqual(production, stored)

    def test_deterministic_rx_vectors_decode_byte_exact(self):
        for case in RX_CASES:
            with self.subTest(link=self.link_id, case=case):
                data = (
                    self.directory / "rx_samples_v2" / f"{case}.cf32le"
                ).read_bytes()
                self.assertEqual(wideband.SLOT_SAMPLES * 8, len(data))
                self.phy.reset_stream()
                decoded = self.phy.decode_samples(
                    PhyDecodeRequest(
                        channel=CHANNEL,
                        samples=SampleBlock(
                            sample_format=SAMPLE_FORMAT_CF32LE,
                            sample_rate_hz=wideband.SAMPLE_RATE_HZ,
                            sample_count=wideband.SLOT_SAMPLES,
                            data=data,
                            first_sample_at=FIRST_SAMPLE_AT,
                            continuity=SampleContinuity.CONTIGUOUS,
                        ),
                        allowed_link_ids=(LinkId(self.link_id),),
                    )
                )
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(1, len(decoded.value.packets))
                recovered = decoded.value.packets[0]
                self.assertEqual(self.link_id, recovered.link_id.value)
                self.assertEqual(self.packet, bytes(recovered.packet))
                delta, sro_ppm = CASE_TIMING[case]
                expected_ns = FIRST_SAMPLE_AT.total_nanoseconds() + (
                    (1.0 + sro_ppm * 1e-6) * delta * SAMPLE_PERIOD_NS
                )
                self.assertLessEqual(
                    abs(recovered.received_at.total_nanoseconds() - expected_ns),
                    SAMPLE_PERIOD_NS,
                )


class Link17VectorTest(_VectorMixin, unittest.TestCase):
    """TC-PHY17-007/008 부분: v2 corpus 결속."""

    link_id = 17


class Link19VectorTest(_VectorMixin, unittest.TestCase):
    """TC-PHY19-007/008 부분: v2 corpus 결속."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
