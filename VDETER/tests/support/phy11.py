"""Shared Stage 2 Link ID 11 test fixtures and canonical random streams."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from vdeter.common.contracts.phy import PhyDecodeRequest
from vdeter.common.contracts.radio import SAMPLE_FORMAT_TEST_IQ_INT16_LE, SampleBlock
from vdeter.common.identifiers import DeterministicIdSource, LinkId, PhysicalChannel
from vdeter.common.time import TestTimeSource
from vdeter.phy.burst.rrc import from_cf32le, to_test_iq_int16_le
from vdeter.phy.transmitter import Link11Phy
from vdeter.profiles.link_phy import build_phy_profile_registry
from vdeter.profiles.models import RegistryScope


VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors/phy/link11"
CHANNEL = PhysicalChannel("VDE-TER-25K")
TIME_SOURCE = TestTimeSource(utc_nanoseconds=1_000_000_000)
FIRST_SAMPLE_AT = TIME_SOURCE.now_utc()


def load_json(name: str):
    return json.loads((VECTOR_ROOT / name).read_text(encoding="utf-8"))


def canonical_packet() -> bytes:
    return bytes.fromhex(load_json("packet_type0_v1.json")["packet_hex"])


def vector_samples(relative: str):
    return from_cf32le((VECTOR_ROOT / relative).read_bytes())


def operational_phy() -> Link11Phy:
    profiles = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
    frozen = profiles.freeze()
    assert frozen.is_ok
    return Link11Phy(profiles, TIME_SOURCE, DeterministicIdSource(0x11))


def sample_block(samples) -> SampleBlock:
    source = tuple(samples)
    return SampleBlock(
        sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
        sample_rate_hz=76800,
        sample_count=len(source),
        data=to_test_iq_int16_le(source),
        first_sample_at=FIRST_SAMPLE_AT,
    )


def decode_public(phy: Link11Phy, samples, allowed=(LinkId(11),)):
    return phy.decode_samples(
        PhyDecodeRequest(
            channel=CHANNEL,
            samples=sample_block(samples),
            allowed_link_ids=tuple(allowed),
        )
    )


class Sha256NormalStream:
    def __init__(self, domain: bytes, seed: int) -> None:
        self.prefix = domain + seed.to_bytes(8, "big")
        self.counter = 0
        self.words = []
        self.consumed_words = []

    def word(self) -> int:
        if not self.words:
            digest = hashlib.sha256(
                self.prefix + self.counter.to_bytes(8, "big")
            ).digest()
            self.counter += 1
            self.words = [
                int.from_bytes(digest[index:index + 8], "big")
                for index in range(0, 32, 8)
            ]
        value = self.words.pop(0)
        self.consumed_words.append(value)
        return value

    def pair(self) -> tuple[float, float]:
        u1 = ((self.word() >> 12) + 0.5) * 2.0 ** -52
        u2 = ((self.word() >> 12) + 0.5) * 2.0 ** -52
        radius = math.sqrt(-2.0 * math.log(u1))
        angle = 2.0 * math.pi * u2
        return radius * math.cos(angle), radius * math.sin(angle)


def opaque_packets(count: int = 256) -> tuple[bytes, ...]:
    domain = b"VDETER-PHY11-PACKET-V1"
    seed = 0x5644455445521103
    stream = bytearray()
    counter = 0
    while len(stream) < count * 54:
        stream.extend(hashlib.sha256(domain + seed.to_bytes(8, "big") + counter.to_bytes(8, "big")).digest())
        counter += 1
    return tuple(bytes(stream[index:index + 54]) for index in range(0, count * 54, 54))


def noisy_ideal_blocks(es_n0_db: float, count: int):
    ideal = vector_samples("rx_samples_v1/ideal.cf32le")
    stream = Sha256NormalStream(b"VDETER-PHY11-AWGN-V1", 0x5644455445521101)
    sigma = math.sqrt(10.0 ** (-es_n0_db / 10.0) / 2.0)
    for _ in range(count):
        values = []
        for sample in ideal:
            normal_i, normal_q = stream.pair()
            values.append(sample + sigma * complex(normal_i, normal_q))
        yield tuple(values)


def noise_only_blocks(count: int):
    stream = Sha256NormalStream(b"VDETER-PHY11-NOISE-V1", 0x5644455445521102)
    scale = 1.0 / math.sqrt(2.0)
    for _ in range(count):
        values = []
        for _ in range(2048):
            normal_i, normal_q = stream.pair()
            values.append(scale * complex(normal_i, normal_q))
        yield tuple(values)
