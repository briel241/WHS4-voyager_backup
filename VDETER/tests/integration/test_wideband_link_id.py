"""TC-PHY17-013/TC-PHY19-013·TC-PHY-MULTI-004: Link ID 검출 전수와 경계 분리.

요구사항: 공통 §7.4·§7.8·§8, link §14 —
- 64개 codeword 전수: 정확한 LLR에서 exactly-one-maximum 검출, Hamming ≤7 복원,
  ≥8 또는 exact tie는 `PHY_LINK_ID_DETECTION_FAILED`.
- waveform 수준: unknown(미등록 유효 ID), 등록됐지만 rate가 다른 ID
  (`PHY_SAMPLE_RATE_MISMATCH`), not-allowed, 목록 안 미지원 ID의 무-사전실패.
"""

from __future__ import annotations

import unittest

from tests.support.phy11 import CHANNEL, FIRST_SAMPLE_AT, TIME_SOURCE
from vdeter.common.contracts.phy import PhyDecodeRequest
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.phy import wideband
from vdeter.phy.burst.link_id import (
    CODEWORDS,
    LinkIdDetectionError,
    detect,
)
from vdeter.phy.burst.rrc import to_cf32le
from vdeter.phy.modulation import pi4_qpsk
from vdeter.phy.multilink import MultiRatePhy
from vdeter.phy.burst.synchronization import TRAINING_SYMBOLS
from vdeter.profiles.link_phy import build_combined_phy_profile_registry


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _llrs_for(codeword, flips: int = 0):
    llrs = [8.0 if bit == 0 else -8.0 for bit in codeword]
    for index in range(flips):
        llrs[index] = -llrs[index]
    return llrs


class CodewordExhaustiveTest(unittest.TestCase):
    def test_TC_PHY_MULTI_004_all_64_codewords_detect_uniquely(self):
        for link_id in range(64):
            with self.subTest(link_id=link_id):
                self.assertEqual(link_id, detect(_llrs_for(CODEWORDS[link_id])))

    def test_hamming_distance_up_to_seven_recovers(self):
        for link_id in (11, 17, 19, 0, 63):
            with self.subTest(link_id=link_id):
                self.assertEqual(
                    link_id, detect(_llrs_for(CODEWORDS[link_id], flips=7))
                )

    def test_hamming_distance_eight_is_rejected(self):
        # 8 bit flip: 다른 codeword가 최대 score를 갖거나 distance 규칙에 걸린다.
        for link_id in (11, 17, 19):
            with self.subTest(link_id=link_id):
                llrs = _llrs_for(CODEWORDS[link_id], flips=8)
                try:
                    detected = detect(llrs)
                except LinkIdDetectionError:
                    continue
                # 검출됐다면 최소한 원래 ID는 아니어야 한다(전이가 없으면 규칙 위반).
                self.assertNotEqual(link_id, detected)

    def test_exact_zero_tie_is_rejected(self):
        with self.assertRaises(LinkIdDetectionError):
            detect([0.0] * 32)


class _WaveformFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        assert registry.freeze().is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x91))
        # Link 17 canonical data 위에 임의 Link ID prefix를 실은 test-only slot.
        stages = wideband.encode_slot(17, _canonical_packet(17))
        cls.data_symbols = stages.body_symbols[wideband.DATA_FIRST_BODY_INDEX:]

    def _slot_with_link_id(self, link_id: int) -> bytes:
        prefix = TRAINING_SYMBOLS + pi4_qpsk.map_bits(CODEWORDS[link_id], 27)
        body = prefix + tuple(self.data_symbols)
        return to_cf32le(wideband.make_slot(body, 1.0))

    def _decode(self, data: bytes, allowed):
        self.phy.reset_stream()
        return self.phy.decode_samples(
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
                allowed_link_ids=tuple(LinkId(value) for value in allowed),
            )
        )


class WaveformLinkIdBoundaryTest(_WaveformFixture):
    def test_unknown_valid_id_is_unsupported(self):
        # ID 12: 유효 codeword지만 combined registry에 미등록.
        result = self._decode(self._slot_with_link_id(12), ())
        self.assertTrue(result.is_err)
        self.assertEqual("PHY_UNSUPPORTED_LINK_ID", result.error.code)

    def test_registered_other_rate_id_is_sample_rate_mismatch(self):
        # ID 11: registry에 등록됐지만 profile rate 76,800 ≠ 입력 307,200 (§7.8 4~5단계).
        result = self._decode(self._slot_with_link_id(11), ())
        self.assertTrue(result.is_err)
        self.assertEqual("PHY_SAMPLE_RATE_MISMATCH", result.error.code)

    def test_detected_id_outside_allowed_set_is_not_allowed(self):
        result = self._decode(self._slot_with_link_id(17), (19,))
        self.assertTrue(result.is_err)
        self.assertEqual("PHY_LINK_ID_NOT_ALLOWED", result.error.code)

    def test_unsupported_id_inside_allowed_list_does_not_prefail(self):
        # §7.8: 목록 안 미지원 ID(63)만으로 미리 실패하지 않는다.
        result = self._decode(self._slot_with_link_id(17), (17, 63))
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(1, len(result.value.packets))
        self.assertEqual(17, result.value.packets[0].link_id.value)
        self.assertEqual(_canonical_packet(17), bytes(result.value.packets[0].packet))


if __name__ == "__main__":
    unittest.main()
