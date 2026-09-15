"""TC-PHY17-011/TC-PHY19-011 timing grid: `delta=t/16`, `t=0..63` 전 범위 복원.

요구사항: 공통 §7.7·§11.1 — canonical Packet, 다른 impairment identity(AWGN 없음)에서
64개 timing offset을 오름차순으로 정확히 한 번씩 적용해 Packet/Link ID를 exact 복원하고
timestamp가 주입 `delta/Fs` 기대값의 한 input sample 이내인지 확인한다. timing/SRO 변환은
§7.7의 4점 Lagrange cubic(범위 밖 zero)이다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
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


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _cubic_interp(samples, position: float) -> complex:
    """§7.1/§7.7 4점 Lagrange cubic, 범위 밖 exact zero."""

    base = int(position // 1)
    mu = position - base
    total = 0.0 + 0.0j
    weights = (
        -mu * (mu - 1.0) * (mu - 2.0) / 6.0,
        (mu + 1.0) * (mu - 1.0) * (mu - 2.0) / 2.0,
        -(mu + 1.0) * mu * (mu - 2.0) / 2.0,
        (mu + 1.0) * mu * (mu - 1.0) / 6.0,
    )
    for offset, weight in zip((-1, 0, 1, 2), weights):
        index = base + offset
        if 0 <= index < len(samples):
            total += weight * samples[index]
    return total


def _timing_shifted(slot, delta: float) -> tuple[complex, ...]:
    return tuple(
        _cubic_interp(slot, index - delta) for index in range(len(slot))
    )


class _TimingGridMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0xE1))
        cls.packet = _canonical_packet(cls.link_id)
        encoded = cls.phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0xF0 + cls.link_id).new_packet_id(),
                link_id=LinkId(cls.link_id),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=cls.packet,
            )
        )
        assert encoded.is_ok, encoded.error
        stages = wideband.encode_slot(cls.link_id, cls.packet)
        cls.slot = stages.samples

    def test_timing_grid_recovers_packet_and_timestamp(self):
        for t in range(64):
            delta = t / 16.0
            with self.subTest(link=self.link_id, t=t):
                shifted = _timing_shifted(self.slot, delta)
                self.phy.reset_stream()
                decoded = self.phy.decode_samples(
                    PhyDecodeRequest(
                        channel=CHANNEL,
                        samples=SampleBlock(
                            sample_format=SAMPLE_FORMAT_CF32LE,
                            sample_rate_hz=wideband.SAMPLE_RATE_HZ,
                            sample_count=len(shifted),
                            data=to_cf32le(shifted),
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
                expected_ns = (
                    FIRST_SAMPLE_AT.total_nanoseconds()
                    + delta * SAMPLE_PERIOD_NS
                )
                self.assertLessEqual(
                    abs(recovered.received_at.total_nanoseconds() - expected_ns),
                    SAMPLE_PERIOD_NS,
                )


class Link17TimingGridTest(_TimingGridMixin, unittest.TestCase):
    """TC-PHY17-011 timing grid 64 subcases."""

    link_id = 17


class Link19TimingGridTest(_TimingGridMixin, unittest.TestCase):
    """TC-PHY19-011 timing grid 64 subcases."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
