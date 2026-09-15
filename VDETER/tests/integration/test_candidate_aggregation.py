"""TC-PHY-MULTI-003/009: multi-ID 자동검출과 성공/실패 candidate 집계.

요구사항: `stage6_phy_common_baseline.md` §7.6·§7.8·§11.1 —
- `003`: 세 canonical burst를 각각 정확한 ID로 자동검출한다.
- `009`: `allowed_link_ids=(17,19)`에서 `Link17canonical||B19bad`,
  `B19bad||Link17canonical`, `B19bad` 세 입력이 각각 성공 burst 하나 /
  성공 burst 하나 / `PHY_FEC_DECODE_FAILED`가 된다.

`B19bad`는 Link 19 canonical Packet에 §5의 송신 직렬화 `zero_pad_8`을 `11111111`로
바꾼 test-only slot이며 정상 TX API로는 만들 수 없다.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy import wideband
from vdeter.phy.burst.framing import bytes_to_bits
from vdeter.phy.burst.rrc import to_cf32le
from vdeter.phy.fec.puncturing import puncture_general
from vdeter.phy.fec.turbo import encode_with_permutation
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry


def _link17_packet() -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")


def _link19_packet() -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _b19bad_slot() -> tuple[complex, ...]:
    """§11.1 `009`: zero_pad_8을 `11111111`로 바꾼 test-only 8,192-sample slot."""

    profile = wideband.PROFILES[19]
    information_bits = bytes_to_bits(_link19_packet())
    encoded = encode_with_permutation(information_bits, wideband.permutation_for(19))
    data_bits = puncture_general(encoded.data_rows, (), profile.data_patterns, ())
    tail_bits = puncture_general(
        (), encoded.tail_rows, profile.data_patterns, profile.tail_patterns
    )
    serial = data_bits + (1,) * profile.zero_pad_bits + tail_bits
    assert len(serial) == profile.channel_bits
    scrambled = wideband._scramble_bits(serial)
    data_symbols = wideband._map_data_symbols(profile, scrambled)
    body = wideband.prefix_symbols(19) + data_symbols
    return wideband.make_slot(body, profile.tx_scale)


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0xC1))
        cls.slots = {}
        for link_id, packet in (
            (11, canonical_packet()),
            (17, _link17_packet()),
            (19, _link19_packet()),
        ):
            encoded = cls.phy.encode_packet(
                PhyEncodeRequest(
                    packet_id=DeterministicIdSource(0xD0 + link_id).new_packet_id(),
                    link_id=LinkId(link_id),
                    channel=CHANNEL,
                    transmit_at=FIRST_SAMPLE_AT,
                    packet=packet,
                )
            )
            assert encoded.is_ok, encoded.error
            cls.slots[link_id] = encoded.value.samples
        cls.b19bad = to_cf32le(_b19bad_slot())

    def _decode(self, data: bytes, rate: int, allowed):
        self.phy.reset_stream()
        return self.phy.decode_samples(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=rate,
                    sample_count=len(data) // 8,
                    data=data,
                    first_sample_at=FIRST_SAMPLE_AT,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=tuple(LinkId(value) for value in allowed),
            )
        )


class AutoDetectionTest(_Fixture):
    def test_TC_PHY_MULTI_003_three_canonical_bursts_detect_their_exact_ids(self):
        expected_packets = {
            11: canonical_packet(),
            17: _link17_packet(),
            19: _link19_packet(),
        }
        for link_id in (11, 17, 19):
            block = self.slots[link_id]
            allowed = (11,) if link_id == 11 else (17, 19)
            with self.subTest(link=link_id):
                decoded = self._decode(block.data, block.sample_rate_hz, allowed)
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(1, len(decoded.value.packets))
                recovered = decoded.value.packets[0]
                self.assertEqual(link_id, recovered.link_id.value)
                self.assertEqual(
                    expected_packets[link_id], bytes(recovered.packet)
                )

    def test_wideband_multi_id_window_detects_each_wideband_link(self):
        # §13.2.2: PHY multi-ID 자동검출은 native 경계의 (17,19) 시험으로 유지한다.
        for link_id in (17, 19):
            block = self.slots[link_id]
            with self.subTest(link=link_id):
                decoded = self._decode(block.data, block.sample_rate_hz, (17, 19))
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(link_id, decoded.value.packets[0].link_id.value)


class MixedCandidateAggregationTest(_Fixture):
    def test_TC_PHY_MULTI_009_1_success_then_bad_padding_returns_success_only(self):
        data = self.slots[17].data + self.b19bad
        decoded = self._decode(data, wideband.SAMPLE_RATE_HZ, (17, 19))
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(1, len(decoded.value.packets))
        recovered = decoded.value.packets[0]
        self.assertEqual(17, recovered.link_id.value)
        self.assertEqual(_link17_packet(), bytes(recovered.packet))

    def test_TC_PHY_MULTI_009_2_bad_padding_then_success_returns_success_only(self):
        data = self.b19bad + self.slots[17].data
        decoded = self._decode(data, wideband.SAMPLE_RATE_HZ, (17, 19))
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(1, len(decoded.value.packets))
        recovered = decoded.value.packets[0]
        self.assertEqual(17, recovered.link_id.value)
        self.assertEqual(_link17_packet(), bytes(recovered.packet))

    def test_TC_PHY_MULTI_009_3_bad_padding_alone_is_fec_decode_failure(self):
        decoded = self._decode(self.b19bad, wideband.SAMPLE_RATE_HZ, (17, 19))
        self.assertTrue(decoded.is_err)
        self.assertEqual("PHY_FEC_DECODE_FAILED", decoded.error.code)


if __name__ == "__main__":
    unittest.main()
