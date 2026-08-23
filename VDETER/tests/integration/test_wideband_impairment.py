"""TC-PHY17-012/TC-PHY19-012: 결정적 impairment 복원 (canonical Packet).

요구사항: 공통 §7.7 — `u[n]=cubic_interp(s,(n-delta)/rho)`,
`v[n]=A*u[n]*exp(j*(phi+2*pi*f*n/Fs))`, timing/SRO → gain/phase/CFO 순서.
10개 승인 case 전부를 canonical Packet에 적용해 byte-exact 복원과 timestamp
허용오차(§8.1의 `rho_hat*timing_offset` 반영, 1 input sample)를 확인한다.

`CFO_*`, `PHASE_WRAP`, `MIXED`의 회전각은 `mpfr256-direct-angle-binary64-rne-v1`:
표의 exact rational과 §7 canonical binary64 `pi`로 각을 구성해 MPFR 256-bit
RNE로 `sin/cos`를 평가한 뒤 각 값을 binary64로 한 번 반올림하고, 이어지는
gain·complex 회전은 명시 항 순서의 binary64 연산이다(FMA 없음). mpmath가 없는
환경에서는 해당 case를 skip한다. case당 random 64 Packet(합계 650/650)은
`VDETER_IMPAIRMENT_FULL=1`일 때만 실행한다(§11.1 runner 연계 전 임시 게이트).
"""

from __future__ import annotations

import hashlib
import os
import struct
import unittest
from fractions import Fraction

try:
    from mpmath import mp as _mp  # noqa: F401

    HAVE_MPMATH = True
except ImportError:  # pragma: no cover - 환경 의존
    HAVE_MPMATH = False

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

#: §7 canonical binary64 `pi` (unsigned 4614256656552045848).
PI64 = struct.unpack("<d", struct.pack("<Q", 4614256656552045848))[0]

#: §7.7 표의 10개 case 전체, 표 순서.
#: (case, delta_samples, phase_pi_ratio(분자,분모) 또는 None, cfo_hz, sro_ppm, gain)
CASES = (
    ("IDEAL", 0.0, None, 0, 0, 1.0),
    ("TIMING_MAX", 63.0 / 16.0, None, 0, 0, 1.0),
    ("CFO_MIN", 0.0, None, -2000, 0, 1.0),
    ("CFO_MAX", 0.0, None, 2000, 0, 1.0),
    ("SRO_MIN", 0.0, None, 0, -10, 1.0),
    ("SRO_MAX", 0.0, None, 0, 10, 1.0),
    ("GAIN_MIN", 0.0, None, 0, 0, 0.25),
    ("GAIN_MAX", 0.0, None, 0, 0, 4.0),
    ("PHASE_WRAP", 0.0, (-1, 1), 0, 0, 1.0),
    ("MIXED", 23.0 / 16.0, (3, 7), 1375, 6, 1.25),
)
TRIG_CASES = frozenset({"CFO_MIN", "CFO_MAX", "PHASE_WRAP", "MIXED"})
FULL_RANDOM = os.environ.get("VDETER_IMPAIRMENT_FULL") == "1"
SEED_BASE = 0x5644455445520000


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


def _rotation_factors(phase_ratio, cfo_hz: int, count: int):
    """`mpfr256-direct-angle-binary64-rne-v1`: sample별 (cos, sin) binary64 tuple."""

    if phase_ratio is None and cfo_hz == 0:
        return None
    from mpmath import mp, mpf, cos, sin

    mp.prec = 256
    pi256 = mpf(PI64)  # canonical binary64 pi의 exact 승격
    phi = (
        pi256 * phase_ratio[0] / phase_ratio[1]
        if phase_ratio is not None
        else mpf(0)
    )
    factors = []
    for index in range(count):
        angle = phi + 2 * pi256 * cfo_hz * index / wideband.SAMPLE_RATE_HZ
        factors.append((float(cos(angle)), float(sin(angle))))
    return factors


def _impaired(
    slot,
    delta: float,
    phase_ratio,
    cfo_hz: int,
    sro_ppm: int,
    gain: float,
) -> tuple[complex, ...]:
    # §7.7 순서: timing/SRO -> gain/phase/CFO (AWGN 없음).
    rho = 1.0 + sro_ppm * 1e-6
    shifted = [
        _cubic_interp(slot, (index - delta) / rho) for index in range(len(slot))
    ]
    factors = _rotation_factors(phase_ratio, cfo_hz, len(slot))
    if factors is None:
        return tuple(gain * value for value in shifted)
    output = []
    for value, (c, s) in zip(shifted, factors):
        scaled = gain * value
        output.append(
            complex(scaled.real * c - scaled.imag * s, scaled.real * s + scaled.imag * c)
        )
    return tuple(output)


def _random_packet(link_id: int, case_index: int, sequence: int) -> bytes:
    """`sha256-counter-bytes-v2`, domain `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2` (§11.1)."""

    size = wideband.PROFILES[link_id].packet_bytes
    domain = f"VDETER-PHY{link_id}-IMPAIRMENT-PACKET-V2".encode("ascii")
    seed = SEED_BASE + link_id * 0x100 + case_index * 0x10000 + sequence
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


class _ImpairmentMixin:
    link_id = 17

    @classmethod
    def setUpClass(cls) -> None:
        registry = build_combined_phy_profile_registry().value
        frozen = registry.freeze()
        assert frozen.is_ok
        cls.phy = MultiRatePhy(registry, TIME_SOURCE, DeterministicIdSource(0x41))
        cls.packet = _canonical_packet(cls.link_id)

    def _decode_expect(self, packet: bytes, case: str, delta, phase_ratio,
                       cfo_hz, sro_ppm, gain) -> None:
        slot = wideband.encode_slot(self.link_id, packet).samples
        impaired = _impaired(slot, delta, phase_ratio, cfo_hz, sro_ppm, gain)
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
        self.assertEqual(packet, bytes(recovered.packet))
        rho = 1.0 + sro_ppm * 1e-6
        expected_ns = FIRST_SAMPLE_AT.total_nanoseconds() + (
            rho * delta * SAMPLE_PERIOD_NS
        )
        self.assertLessEqual(
            abs(recovered.received_at.total_nanoseconds() - expected_ns),
            SAMPLE_PERIOD_NS,
            msg=f"{case} timestamp",
        )

    def test_all_ten_impairment_cases_recover_the_canonical_packet(self):
        for case, delta, phase_ratio, cfo_hz, sro_ppm, gain in CASES:
            if case in TRIG_CASES and not HAVE_MPMATH:
                self.skipTest("mpmath(MPFR-256) 미설치: trig case는 vectors extra 필요")
            with self.subTest(link=self.link_id, case=case):
                self._decode_expect(
                    self.packet, case, delta, phase_ratio, cfo_hz, sro_ppm, gain
                )

    @unittest.skipUnless(
        FULL_RANDOM, "VDETER_IMPAIRMENT_FULL=1일 때만 case당 random 64 Packet 실행"
    )
    def test_each_case_recovers_sixty_four_random_packets(self):
        for case_index, (case, delta, phase_ratio, cfo_hz, sro_ppm, gain) in enumerate(
            CASES
        ):
            if case in TRIG_CASES and not HAVE_MPMATH:
                self.skipTest("mpmath(MPFR-256) 미설치")
            for sequence in range(64):
                with self.subTest(link=self.link_id, case=case, sequence=sequence):
                    self._decode_expect(
                        _random_packet(self.link_id, case_index, sequence),
                        case, delta, phase_ratio, cfo_hz, sro_ppm, gain,
                    )


class Link17ImpairmentTest(_ImpairmentMixin, unittest.TestCase):
    """TC-PHY17-012: §7.7 10개 case (canonical; random 64는 FULL 게이트)."""

    link_id = 17


class Link19ImpairmentTest(_ImpairmentMixin, unittest.TestCase):
    """TC-PHY19-012: §7.7 10개 case (canonical; random 64는 FULL 게이트)."""

    link_id = 19


if __name__ == "__main__":
    unittest.main()
