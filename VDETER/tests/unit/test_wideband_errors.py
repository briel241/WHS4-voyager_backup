"""TC-PHY17-014/TC-PHY19-014 부분: TX/RX 구조화 오류와 registry 경계.

요구사항: link §11 — `PHY_PACKET_LENGTH_INVALID`, `PHY_UNSUPPORTED_LINK_ID`(TX),
`PHY_TRANSMIT_DEADLINE_EXPIRED`, `CLOCK_DOMAIN_MISMATCH`(TX), `REGISTRY_NOT_FROZEN`.
RX precedence·padding fault·rate-family 경계는 별도 시험
(test_multirate_phy/test_candidate_aggregation/test_wideband_link_id)이 소유한다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE
from vdeter.common.contracts.phy import PhyEncodeRequest
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.time import ClockDomain, TimePoint
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry


def _phy(frozen: bool = True) -> MultiRatePhy:
    registry = build_combined_phy_profile_registry().value
    if frozen:
        assert registry.freeze().is_ok
    return MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0xB1))


def _encode(phy, link_id: int, packet: bytes, transmit_at=FIRST_SAMPLE_AT):
    return phy.encode_packet(
        PhyEncodeRequest(
            packet_id=DeterministicIdSource(0xB2).new_packet_id(),
            link_id=LinkId(link_id),
            channel=CHANNEL,
            transmit_at=transmit_at,
            packet=packet,
        )
    )


class TxStructuredErrorTest(unittest.TestCase):
    def test_packet_length_invalid_for_both_links(self):
        phy = _phy()
        for link_id, wrong in ((17, 233), (17, 235), (19, 701), (19, 703)):
            with self.subTest(link=link_id, bytes=wrong):
                result = _encode(phy, link_id, bytes(wrong))
                self.assertTrue(result.is_err)
                self.assertEqual("PHY_PACKET_LENGTH_INVALID", result.error.code)

    def test_unsupported_tx_link_id(self):
        result = _encode(_phy(), 63, bytes(64))
        self.assertTrue(result.is_err)
        self.assertEqual("PHY_UNSUPPORTED_LINK_ID", result.error.code)

    def test_transmit_deadline_expired(self):
        past = TIME_SOURCE.now_utc().shifted(-1_000_000_000)
        result = _encode(_phy(), 17, bytes(234), transmit_at=past)
        self.assertTrue(result.is_err)
        self.assertEqual("PHY_TRANSMIT_DEADLINE_EXPIRED", result.error.code)

    def test_clock_domain_mismatch_on_transmit_clock_id(self):
        wrong_clock = TimePoint(
            FIRST_SAMPLE_AT.seconds,
            FIRST_SAMPLE_AT.nanoseconds,
            ClockDomain.UTC,
            "some-other-utc-clock",
        )
        result = _encode(_phy(), 17, bytes(234), transmit_at=wrong_clock)
        self.assertTrue(result.is_err)
        self.assertEqual("CLOCK_DOMAIN_MISMATCH", result.error.code)

    def test_registry_not_frozen_is_rejected_before_encode(self):
        result = _encode(_phy(frozen=False), 17, bytes(234))
        self.assertTrue(result.is_err)
        self.assertEqual("REGISTRY_NOT_FROZEN", result.error.code)


if __name__ == "__main__":
    unittest.main()
