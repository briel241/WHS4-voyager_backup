"""TC-PHY17-012/TC-PHY19-012 부분: 삼각함수가 필요 없는 결정적 impairment 복원.

요구사항: 공통 §7.7 — `u[n]=cubic_interp(s,(n-delta)/rho)`, `v[n]=A*u[n]` (phase/CFO 0).
10개 승인 case 중 `IDEAL`, `TIMING_MAX`, `SRO_MIN`, `SRO_MAX`, `GAIN_MIN`, `GAIN_MAX`
6개를 canonical Packet에 적용해 byte-exact 복원과 timestamp 허용오차(§8.1의
`rho_hat*timing_offset` 반영, 1 input sample)를 확인한다. `CFO_*`, `PHASE_WRAP`,
`MIXED`는 MPFR 256-bit 삼각함수 평가가 필요하므로 이 module에서 다루지 않는다.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

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

SAMPLE_PERIOD_NS = 1_000_000_000.0 / wideband.SAMPLE_RATE_HZ

#: (case, delta_samples, sro_ppm, gain) — §7.7 표의 무-삼각함수 부분집합.
CASES = (
    ("IDEAL", 0.0, 0, 1.0),
    ("TIMING_MAX", 63.0 / 16.0, 0, 1.0),
    ("SRO_MIN", 0.0, -10, 1.0),
    ("SRO_MAX", 0.0, 10, 1.0),
    ("GAIN_MIN", 0.0, 0, 0.25),
    ("GAIN_MAX", 0.0, 0, 4.0),
)


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _cubic_interp(samples, position: float) -> complex:
    base = int(position // 1)
    mu = position - base
    weights = (
        -mu * (mu - 1.0) * (mu - 2.0) / 6.0,
        (mu + 1.0) * (mu - 1.0) * (mu - 2.0) / 2.0,
        -(mu + 1.0) * mu * (mu - 2.0) / 2.0,
        (mu + 1.0) * mu * (mu - 1.0) / 6.0,
    )
    total = 0.0 + 0.0j
    for offset, weight in zip((-1, 0, 1, 2), weights):
        index = base + offset
        if 0 <= index < len(samples):
            total += weight * samples[index]
    return total


def _impaired(slot, delta: float, sro_ppm: int, gain: float) -> tuple[complex, ...]:
    rho = 1.0 + sro_ppm * 1e-6
    return tuple(
        gain * _cubic_interp(slot, (index - delta) / rho)
        for index in range(len(slot))
    )


class _ImpairmentMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x41))
        cls.packet = _canonical_packet(cls.link_id)
        cls.slot = wideband.encode_slot(cls.link_id, cls.packet).samples

    def test_trig_free_impairment_cases_recover_byte_exact(self):
        for case, delta, sro_ppm, gain in CASES:
            with self.subTest(link=self.link_id, case=case):
                impaired = _impaired(self.slot, delta, sro_ppm, gain)
                self.phy.reset_stream()
                decoded = self.phy.decode_samples(
                    PhyDecodeRequest(
                        channel=CHANNEL,
                        samples=SampleBlock(
                            sample_format=SAMPLE_FORMAT_CF32LE,
                            sample_rate_hz=wideband.SAMPLE_RATE_HZ,
                            sample_count=len(impaired),
                            data=to_cf32le(impaired),
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
                rho = 1.0 + sro_ppm * 1e-6
                expected_ns = FIRST_SAMPLE_AT.total_nanoseconds() + (
                    rho * delta * SAMPLE_PERIOD_NS
                )
                self.assertLessEqual(
                    abs(recovered.received_at.total_nanoseconds() - expected_ns),
                    SAMPLE_PERIOD_NS,
                    msg=f"{case} timestamp",
                )


class Link17ImpairmentSubsetTest(_ImpairmentMixin, unittest.TestCase):
    """TC-PHY17-012 무-삼각함수 6 subcases."""

    link_id = 17


class Link19ImpairmentSubsetTest(_ImpairmentMixin, unittest.TestCase):
    """TC-PHY19-012 무-삼각함수 6 subcases."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
