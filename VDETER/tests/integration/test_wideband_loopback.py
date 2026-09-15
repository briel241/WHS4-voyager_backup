"""TC-PHY17-009/TC-PHY19-009: fixed 5 + random 256, Link별 정확히 261 Packet loopback.

요구사항: 공통 §11.1 — 각 Packet을 완전한 8,192-sample waveform을 담은 단일 contiguous
`SampleBlock`으로 복원한다. 261개 모두 Packet bytes·Link ID가 exact 일치하고 중복 반환이
없어야 PASS다. random Packet byte generator는 `sha256-counter-bytes-v2`, domain
`VDETER-PHY{L}-LOOPBACK-PACKET-V2`, seed `0x5644455445520000 + link_id*0x100 + sequence`다.
"""

from __future__ import annotations

import hashlib
import struct
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
from vdeter.phy.multilink import MultiRatePhy
from vdeter.profiles.link_phy import build_combined_phy_profile_registry

SEED_BASE = 0x5644455445520000


def _canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


def _fixed_packets(link_id: int) -> tuple[bytes, ...]:
    size = wideband.PROFILES[link_id].packet_bytes
    return (
        _canonical_packet(link_id),
        bytes(size),
        b"\xff" * size,
        bytes(index % 256 for index in range(size)),
        bytes(0xA5 if index % 2 == 0 else 0x5A for index in range(size)),
    )


def _random_packet(link_id: int, sequence: int) -> bytes:
    """`sha256-counter-bytes-v2` (공통 §11.1)."""

    size = wideband.PROFILES[link_id].packet_bytes
    domain = f"VDETER-PHY{link_id}-LOOPBACK-PACKET-V2".encode("ascii")
    seed = SEED_BASE + link_id * 0x100 + sequence
    output = bytearray()
    counter = 0
    while len(output) < size:
        output.extend(
            hashlib.sha256(
                domain + struct.pack(">Q", seed) + struct.pack(">Q", counter)
            ).digest()
        )
        counter += 1
    return bytes(output[:size])


class _LoopbackMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x21))

    def _loopback(self, packet: bytes) -> None:
        encoded = self.phy.encode_packet(
            PhyEncodeRequest(
                packet_id=DeterministicIdSource(0x31).new_packet_id(),
                link_id=LinkId(self.link_id),
                channel=CHANNEL,
                transmit_at=FIRST_SAMPLE_AT,
                packet=packet,
            )
        )
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        block = encoded.value.samples
        self.assertEqual(8_192, block.sample_count)
        self.phy.reset_stream()
        decoded = self.phy.decode_samples(
            PhyDecodeRequest(
                channel=CHANNEL,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=block.sample_rate_hz,
                    sample_count=block.sample_count,
                    data=block.data,
                    first_sample_at=FIRST_SAMPLE_AT,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=(LinkId(self.link_id),),
            )
        )
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        # 중복·오검출 없이 정확히 한 번 반환한다.
        self.assertEqual(1, len(decoded.value.packets))
        recovered = decoded.value.packets[0]
        self.assertEqual(self.link_id, recovered.link_id.value)
        self.assertEqual(packet, bytes(recovered.packet))

    def test_fixed_five_packets_loop_back_byte_exact(self):
        for index, packet in enumerate(_fixed_packets(self.link_id)):
            with self.subTest(link=self.link_id, fixed=index):
                self._loopback(packet)

    def test_random_256_packets_loop_back_byte_exact(self):
        for sequence in range(256):
            with self.subTest(link=self.link_id, sequence=sequence):
                self._loopback(_random_packet(self.link_id, sequence))


class Link17LoopbackTest(_LoopbackMixin, unittest.TestCase):
    """TC-PHY17-009 fixed/random loopback 261 subcases."""

    link_id = 17


class Link19LoopbackTest(_LoopbackMixin, unittest.TestCase):
    """TC-PHY19-009 fixed/random loopback 261 subcases."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
