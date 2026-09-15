"""TC-PHY17/19-002·003·004: FEC 단계별 bit 출력의 vector-결속 exact 대조.

요구사항: link §13·공통 §11.1 — production encoder의 단계별 출력(information bits,
`punctured_data || zero_pad || tail` 직렬 channel bits, scrambled bits)이 독립
generator가 만든 v2 vector(`tx_stages_v2.md`)와 bit 단위로 일치하고, count·순열·
padding 불변식이 exact함을 고정한다.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from vdeter.phy import wideband
from vdeter.phy.burst.framing import bytes_to_bits
from vdeter.phy.fec.interleaver import general_permutation_zero_based
from vdeter.phy.fec.turbo import encode_with_permutation

VECTOR_ROOT = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "phy"


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _vector_stage_bits(link_id: int) -> dict[str, str]:
    text = (VECTOR_ROOT / f"link{link_id}" / "tx_stages_v2.md").read_text("utf-8")
    blocks = re.findall(r"```text\n([01]+)\n```", text)
    assert len(blocks) == 3, "tx_stages_v2.md must carry three bit strings"
    return {"information": blocks[0], "serial": blocks[1], "scrambled": blocks[2]}


class _StageBitsMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        cls.packet = _canonical_packet(cls.link_id)
        cls.stages = wideband.encode_slot(cls.link_id, cls.packet)
        cls.vector = _vector_stage_bits(cls.link_id)
        cls.profile = wideband.PROFILES[cls.link_id]

    def test_TC_002_interleaver_is_a_full_permutation_and_matches_encoder(self):
        permutation = general_permutation_zero_based(
            self.profile.information_bits,
            self.profile.interleaver_k1,
            self.profile.interleaver_k2,
            self.profile.interleaver_primes,
        )
        self.assertEqual(
            sorted(permutation), list(range(self.profile.information_bits))
        )
        # encoder가 같은 순열을 소비하는지: 재-encode 결과의 직렬화가 vector와 일치.
        encoded = encode_with_permutation(
            bytes_to_bits(self.packet), permutation
        )
        serial = wideband._serial_channel_bits(self.profile, encoded)
        self.assertEqual(self.vector["serial"], "".join(map(str, serial)))

    def test_TC_002_information_bits_match_the_vector(self):
        observed = "".join(map(str, self.stages.information_bits))
        self.assertEqual(self.profile.information_bits, len(observed))
        self.assertEqual(self.vector["information"], observed)

    def test_TC_003_puncture_padding_counts_and_serial_bits_are_exact(self):
        observed = "".join(map(str, self.stages.channel_bits))
        self.assertEqual(self.profile.channel_bits, len(observed))
        self.assertEqual(self.vector["serial"], observed)
        if self.profile.zero_pad_bits:
            # §5: `punctured_data || zero_pad_8 || tail_12` — padding 위치 exact.
            data_end = self.profile.channel_bits - self.profile.zero_pad_bits - 12
            padding = observed[data_end:data_end + self.profile.zero_pad_bits]
            self.assertEqual("0" * self.profile.zero_pad_bits, padding)

    def test_TC_004_scrambler_covers_the_full_serial_range(self):
        observed = "".join(map(str, self.stages.scrambled_bits))
        self.assertEqual(self.profile.channel_bits, len(observed))
        self.assertEqual(self.vector["scrambled"], observed)
        # scrambler 왕복: descramble(scramble(x)) == x
        pn = wideband.pn_sequence(len(self.stages.channel_bits))
        recovered = tuple(
            bit ^ mask for bit, mask in zip(self.stages.scrambled_bits, pn)
        )
        self.assertEqual(tuple(self.stages.channel_bits), recovered)


class Link17StageBitsTest(_StageBitsMixin, unittest.TestCase):
    """TC-PHY17-002/003/004 stage-bit exact records."""

    link_id = 17


class Link19StageBitsTest(_StageBitsMixin, unittest.TestCase):
    """TC-PHY19-002/003/004 stage-bit exact records."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
