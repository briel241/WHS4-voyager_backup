"""§7.9 cross-block streaming 계약 시험.

요구사항: `stage6_phy_common_baseline.md` §7.9 —
- `TC-PHY-MULTI-007`: Link 11·17·19 각 region 경계·midpoint 21개 cut ×
  `CONTIGUOUS/GAP_BEFORE/UNKNOWN` 3 변형, 합계 189 subcases.
- `TC-PHY-MULTI-008`: window-intent state key 4 subcases.
- `TC-PHY17-011`/`TC-PHY19-011`의 no-burst 2·multi 2 subcases.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE, canonical_packet
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry

#: 공통 §7.9의 canonical cut index 21개 (링크별).
CANONICAL_CUTS = {
    11: (16, 31, 32, 33, 86, 139, 140, 141, 172, 203, 204, 205,
         1078, 1951, 1952, 1953, 1968, 1983, 1984, 1985, 2016),
    17: (64, 127, 128, 129, 182, 235, 236, 237, 268, 299, 300, 301,
         4054, 7807, 7808, 7809, 7872, 7935, 7936, 7937, 8064),
    19: (64, 127, 128, 129, 182, 235, 236, 237, 268, 299, 300, 301,
         4054, 7807, 7808, 7809, 7872, 7935, 7936, 7937, 8064),
}
SAMPLE_RATES = {11: 76_800, 17: 307_200, 19: 307_200}
SLOT_SAMPLES = {11: 2_048, 17: 8_192, 19: 8_192}


def _packet_for(link_id: int) -> bytes:
    if link_id == 11:
        return canonical_packet()
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _round_half_away_ns(samples: int, rate: int) -> int:
    exact = Fraction(samples * 1_000_000_000, rate)
    floor = exact.numerator // exact.denominator
    return int(floor) + (1 if (exact - floor) * 2 >= 1 else 0)


class _StreamFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0xA1))
        cls.slots = {}
        cls.references = {}
        for link_id in (11, 17, 19):
            encoded = cls.phy.encode_packet(
                PhyEncodeRequest(
                    packet_id=DeterministicIdSource(0xB0 + link_id).new_packet_id(),
                    link_id=LinkId(link_id),
                    channel=CHANNEL,
                    transmit_at=FIRST_SAMPLE_AT,
                    packet=_packet_for(link_id),
                )
            )
            assert encoded.is_ok, encoded.error
            cls.slots[link_id] = encoded.value.samples.data
            cls.phy.reset_stream()
            reference = cls.phy.decode_samples(
                PhyDecodeRequest(
                    channel=CHANNEL,
                    samples=cls._block_data(
                        encoded.value.samples.data, FIRST_SAMPLE_AT, link_id
                    ),
                    allowed_link_ids=(LinkId(link_id),),
                )
            )
            assert reference.is_ok, getattr(reference, "error", None)
            assert len(reference.value.packets) == 1
            cls.references[link_id] = reference.value.packets[0]

    @staticmethod
    def _block_data(data: bytes, epoch, link_id: int,
                    continuity=SampleContinuity.CONTIGUOUS) -> SampleBlock:
        return SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=SAMPLE_RATES[link_id],
            sample_count=len(data) // 8,
            data=data,
            first_sample_at=epoch,
            continuity=continuity,
        )

    def _decode(self, data: bytes, epoch, link_id: int, allowed,
                continuity=SampleContinuity.CONTIGUOUS):
        result = self.phy.decode_samples(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=self._block_data(data, epoch, link_id, continuity),
                allowed_link_ids=tuple(LinkId(value) for value in allowed),
            )
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        return result.value


class StreamSplitTest(_StreamFixture):
    def test_TC_PHY_MULTI_007_all_region_cuts_for_three_links(self):
        for link_id in (11, 17, 19):
            data = self.slots[link_id]
            rate = SAMPLE_RATES[link_id]
            reference = self.references[link_id]
            for cut in CANONICAL_CUTS[link_id]:
                first = data[: cut * 8]
                second = data[cut * 8 :]
                second_epoch = FIRST_SAMPLE_AT.shifted(_round_half_away_ns(cut, rate))
                for continuity in (
                    SampleContinuity.CONTIGUOUS,
                    SampleContinuity.GAP_BEFORE,
                    SampleContinuity.UNKNOWN,
                ):
                    with self.subTest(link=link_id, cut=cut, continuity=continuity.value):
                        self.phy.reset_stream()
                        result_1 = self._decode(first, FIRST_SAMPLE_AT, link_id, (link_id,))
                        result_2 = self._decode(
                            second, second_epoch, link_id, (link_id,), continuity
                        )
                        packets = result_1.packets + result_2.packets
                        if continuity is SampleContinuity.CONTIGUOUS:
                            # unsplit 결과와 byte/link/timestamp exact-equivalent, 1회 반환.
                            self.assertEqual(1, len(packets))
                            recovered = packets[0]
                            self.assertEqual(bytes(reference.packet), bytes(recovered.packet))
                            self.assertEqual(reference.link_id, recovered.link_id)
                            self.assertEqual(
                                reference.received_at.total_nanoseconds(),
                                recovered.received_at.total_nanoseconds(),
                            )
                        else:
                            # 이전 partial 폐기: 중복·구조화 오류 없이 최대 1회 반환.
                            self.assertLessEqual(len(packets), 1)
                            if result_1.packets:
                                # 첫 block에 이미 완전한 body가 있던 늦은 cut.
                                self.assertEqual((), result_2.packets)
                            for recovered in packets:
                                self.assertEqual(
                                    bytes(reference.packet), bytes(recovered.packet)
                                )
                                self.assertEqual(reference.link_id, recovered.link_id)

    def test_TC_PHY_MULTI_008_window_intent_state_key(self):
        link_id = 17
        data = self.slots[link_id]
        rate = SAMPLE_RATES[link_id]
        cut = 182  # training 중간
        first = data[: cut * 8]
        second = data[cut * 8 :]
        second_epoch = FIRST_SAMPLE_AT.shifted(_round_half_away_ns(cut, rate))
        reference = self.references[link_id]
        transitions = (
            ((17,), (19,), False),
            ((), (17,), False),
            ((17,), (), False),
            ((17, 19, 17), (19, 17), True),
        )
        for allowed_1, allowed_2, preserved in transitions:
            with self.subTest(first=allowed_1, second=allowed_2):
                self.phy.reset_stream()
                result_1 = self._decode(first, FIRST_SAMPLE_AT, link_id, allowed_1)
                self.assertEqual((), result_1.packets)
                result_2 = self._decode(second, second_epoch, link_id, allowed_2)
                if preserved:
                    # canonical tuple이 같으므로 state를 보존하고 unsplit과 exact 일치.
                    self.assertEqual(1, len(result_2.packets))
                    recovered = result_2.packets[0]
                    self.assertEqual(bytes(reference.packet), bytes(recovered.packet))
                    self.assertEqual(reference.link_id, recovered.link_id)
                    self.assertEqual(
                        reference.received_at.total_nanoseconds(),
                        recovered.received_at.total_nanoseconds(),
                    )
                else:
                    # identity 변경: 이전 partial을 폐기하고 정상 빈 결과.
                    self.assertEqual((), result_2.packets)
                    self.assertEqual("NO_BURST_DETECTED", result_2.status.value)


class MultiBurstAndNoBurstTest(_StreamFixture):
    def _run_no_burst(self, link_id: int):
        for name, data in (("empty", b""), ("all_zero", b"\x00" * (8_192 * 8))):
            with self.subTest(link=link_id, case=name):
                self.phy.reset_stream()
                result = self._decode(data, FIRST_SAMPLE_AT, link_id, (link_id,))
                self.assertEqual("NO_BURST_DETECTED", result.status.value)
                self.assertEqual((), result.packets)
                self.assertEqual(
                    FIRST_SAMPLE_AT.total_nanoseconds(),
                    result.received_at.total_nanoseconds(),
                )

    def _run_multi(self, link_id: int):
        data = self.slots[link_id]
        rate = SAMPLE_RATES[link_id]
        slot = SLOT_SAMPLES[link_id]
        reference = self.references[link_id]
        second_burst_ns = FIRST_SAMPLE_AT.total_nanoseconds() + _round_half_away_ns(
            slot, rate
        )
        with self.subTest(link=link_id, case="slot_slot_single_block"):
            self.phy.reset_stream()
            result = self._decode(data + data, FIRST_SAMPLE_AT, link_id, (link_id,))
            self.assertEqual(2, len(result.packets))
            self.assertEqual(
                reference.received_at.total_nanoseconds(),
                result.packets[0].received_at.total_nanoseconds(),
            )
            self.assertEqual(
                second_burst_ns, result.packets[1].received_at.total_nanoseconds()
            )
            for recovered in result.packets:
                self.assertEqual(bytes(reference.packet), bytes(recovered.packet))
            self.assertNotEqual(
                result.packets[0].packet_id, result.packets[1].packet_id
            )
            self.assertEqual(
                result.received_at.total_nanoseconds(),
                result.packets[0].received_at.total_nanoseconds(),
            )
        with self.subTest(link=link_id, case="trailing_partial_then_completion"):
            self.phy.reset_stream()
            first = data + data[: 4_054 * 8]
            second = data[4_054 * 8 :]
            second_epoch = FIRST_SAMPLE_AT.shifted(
                _round_half_away_ns(slot + 4_054, rate)
            )
            result_1 = self._decode(first, FIRST_SAMPLE_AT, link_id, (link_id,))
            self.assertEqual(1, len(result_1.packets))
            self.assertEqual(
                reference.received_at.total_nanoseconds(),
                result_1.packets[0].received_at.total_nanoseconds(),
            )
            result_2 = self._decode(second, second_epoch, link_id, (link_id,))
            self.assertEqual(1, len(result_2.packets))
            self.assertEqual(
                second_burst_ns, result_2.packets[0].received_at.total_nanoseconds()
            )
            self.assertEqual(
                bytes(reference.packet), bytes(result_2.packets[0].packet)
            )

    def test_TC_PHY17_011_no_burst_and_multi(self):
        self._run_no_burst(17)
        self._run_multi(17)

    def test_TC_PHY19_011_no_burst_and_multi(self):
        self._run_no_burst(19)
        self._run_multi(19)


if __name__ == "__main__":
    unittest.main()
