"""TC-PHY11-001..004: profile and complete Link ID 11 bit chain."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import unittest

from tests.support.phy11 import VECTOR_ROOT, canonical_packet, load_json
from vdeter.phy.burst.framing import bytes_to_bits
from vdeter.phy.fec.interleaver import PERMUTATION_1BASED, deinterleave, interleave
from vdeter.phy.fec.puncturing import (
    DATA_PATTERN_6,
    TAIL_PATTERN_6A,
    puncture,
    rate_recover,
)
from vdeter.phy.fec.turbo import (
    RSC_TRANSITIONS,
    TAIL_INPUTS,
    _siso,
    decode,
    encode,
    rsc_step,
    rsc_tail,
)
from vdeter.phy.scrambling import (
    PN_SEQUENCE_874,
    descramble_bits,
    descramble_llrs,
    pn_sequence,
    scramble,
)
from vdeter.profiles.link_phy import (
    PHY11_PROFILE_SCHEMA_ID,
    PHY11_REGISTRY_FINGERPRINT,
    PHY11_RRC_TAP_BINARY64_BITS,
    PHY11_RRC_TAPS_SHA256,
    build_phy_profile_registry,
    phy11_profile_parameters,
)
from vdeter.profiles.models import RegistryScope


class Phy11ProfileAndFecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stages = load_json("tx_stages_v1.json")
        cls.official = load_json("official_constants_v1.json")
        cls.asm = load_json("asm_link5_common_v1.json")
        cls.packet_bits = bytes_to_bits(canonical_packet())

    @staticmethod
    def _float_from_bits(value: int) -> float:
        return struct.unpack("<d", struct.pack("<Q", value))[0]

    @staticmethod
    def _float_bits(value: float) -> int | None:
        if value == -math.inf:
            return None
        return struct.unpack("<Q", struct.pack("<d", value))[0]

    @classmethod
    def _metric_rows(cls, rows) -> dict[str, list[int | None]]:
        return {
            key: [cls._float_bits(value) for value in values]
            for key, values in rows.items()
        }

    @classmethod
    def _branch_rows(cls, rows) -> dict[str, list[list[int]]]:
        return {
            key: [
                [cls._float_bits(value) for value in branch]
                for branch in states
            ]
            for key, states in rows.items()
        }

    @staticmethod
    def _encode_official_asm_common(bits: tuple[int, ...]) -> tuple[int, ...]:
        """Exercise production RSC primitives against the official Link 5 example."""

        permutation = []
        for s in range(1, 289):
            m = (s - 1) % 2
            j = (s - 1) // 2
            c = (47 * j + 21 * m) % 144
            permutation.append(2 * (c + 1) - m - 1)
        permuted = tuple(bits[index] for index in permutation)
        first_state = second_state = 0
        data_rows = []
        for original, interleaved_bit in zip(bits, permuted):
            first_state, first = rsc_step(first_state, original)
            second_state, second = rsc_step(second_state, interleaved_bit)
            data_rows.append(first + second)
        tail_rows = []
        for _ in range(3):
            first_state, output = rsc_tail(first_state)
            tail_rows.append(output + (0, 0, 0))
        for _ in range(3):
            second_state, output = rsc_tail(second_state)
            tail_rows.append((0, 0, 0) + output)
        if first_state != 0 or second_state != 0:
            raise AssertionError("official ASM RSC termination failed")
        data_pattern = (
            (1, 0, 1, 0, 0, 0),
            (1, 0, 0, 0, 0, 0),
            (1, 0, 0, 0, 0, 0),
            (1, 0, 0, 0, 0, 0),
            (1, 0, 0, 0, 0, 0),
            (1, 0, 0, 0, 0, 1),
        )
        tail_pattern = (
            (1, 0, 1, 0, 0, 0),
            (1, 0, 1, 0, 0, 0),
            (1, 0, 0, 0, 0, 0),
            (0, 0, 0, 1, 0, 1),
            (0, 0, 0, 1, 0, 1),
            (0, 0, 0, 1, 0, 0),
        )
        channel = []
        for index, row in enumerate(data_rows):
            channel.extend(value for value, keep in zip(row, data_pattern[index % 6]) if keep)
        for row, pattern in zip(tail_rows, tail_pattern):
            channel.extend(value for value, keep in zip(row, pattern) if keep)
        return tuple(channel)

    def test_TC_PHY11_001_closed_profile_registry_and_counts(self):
        parameters = phy11_profile_parameters()
        self.assertEqual(PHY11_PROFILE_SCHEMA_ID, parameters.profile_schema_id)
        self.assertEqual(self.official["profile"], parameters.fingerprint_content())
        counts = parameters.section("counts")
        self.assertEqual((54, 432, 874, 480, 2048), (
            counts["packet_bytes"], counts["information_bits"], counts["channel_bits"],
            counts["body_symbols"], counts["slot_samples"],
        ))
        tap_bytes = b"".join(struct.pack("<Q", value) for value in PHY11_RRC_TAP_BINARY64_BITS)
        self.assertEqual(PHY11_RRC_TAPS_SHA256, hashlib.sha256(tap_bytes).hexdigest())
        registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        fingerprint = registry.freeze().value
        self.assertEqual(1, fingerprint.entry_count)
        self.assertEqual("stage2-phy11-v2", fingerprint.content_version)
        self.assertEqual(PHY11_REGISTRY_FINGERPRINT, fingerprint.fingerprint)
        manifest = load_json("manifest_v1.json")
        self.assertEqual(PHY11_REGISTRY_FINGERPRINT, manifest["registry_fingerprint"])

    def test_TC_PHY11_002_rsc_interleaver_termination_and_log_map(self):
        vector_trellis = tuple(
            tuple((transition[0], tuple(transition[1])) for transition in state)
            for state in self.stages["rsc_trellis"]
        )
        self.assertEqual(vector_trellis, RSC_TRANSITIONS)
        self.assertEqual(tuple(tuple(row) for row in self.stages["termination_inputs_by_state"]), TAIL_INPUTS)
        self.assertEqual(tuple(self.stages["permutation_1based"]), PERMUTATION_1BASED)
        self.assertEqual(self.packet_bits, deinterleave(interleave(self.packet_bits)))
        encoded = encode(self.packet_bits)
        self.assertEqual(tuple(tuple(row) for row in self.stages["data_rows"]), encoded.data_rows)
        self.assertEqual(tuple(tuple(row) for row in self.stages["tail_rows"]), encoded.tail_rows)
        asm_input = tuple(int(bit) for bit in self.asm["turbo_input_288"])
        asm_encoded = self._encode_official_asm_common(asm_input)
        self.assertEqual(self.asm["turbo_encoded_with_tail_394"], "".join(map(str, asm_encoded)))

        reference = self.stages["log_map_reference"]
        llrs = tuple(self._float_from_bits(value) for value in reference["input_llr_f64_bits"])
        data_rows, tail_rows = rate_recover(llrs)
        lx = tuple(row[0] for row in data_rows)
        y0 = tuple(row[1] for row in data_rows)
        yp0 = tuple(row[4] for row in data_rows)
        sys1 = lx + tuple(row[0] for row in tail_rows[:3])
        p01 = y0 + tuple(row[1] for row in tail_rows[:3])
        zeros = (0.0,) * 435
        sys2 = interleave(lx) + tuple(row[3] for row in tail_rows[3:])
        p02 = yp0 + tuple(row[4] for row in tail_rows[3:])
        la1 = (0.0,) * 432
        post2 = (0.0,) * 432
        for iteration, expected in enumerate(reference["iterations"], start=1):
            diagnostics = {} if iteration == 1 else None
            post1, ext1 = _siso(sys1, p01, zeros, la1, diagnostics=diagnostics)
            self.assertEqual(expected["siso1_posterior_f64_bits"], [self._float_bits(value) for value in post1])
            self.assertEqual(expected["siso1_extrinsic_f64_bits"], [self._float_bits(value) for value in ext1])
            la2 = interleave(ext1)
            post2, ext2 = _siso(sys2, p02, zeros, la2)
            self.assertEqual(expected["siso2_posterior_f64_bits"], [self._float_bits(value) for value in post2])
            self.assertEqual(expected["siso2_extrinsic_f64_bits"], [self._float_bits(value) for value in ext2])
            if diagnostics is not None:
                checkpoints = reference["first_iteration_siso1_step_checkpoints"]
                self.assertEqual(
                    checkpoints["gamma_branch_metric_f64_bits"],
                    self._branch_rows(diagnostics["gamma_branch_metrics"]),
                )
                self.assertEqual(
                    checkpoints["alpha_metric_f64_bits"],
                    self._metric_rows(diagnostics["alpha_metrics"]),
                )
                self.assertEqual(
                    checkpoints["beta_metric_f64_bits"],
                    self._metric_rows(diagnostics["beta_metrics"]),
                )
            la1 = deinterleave(ext2)
        final = deinterleave(post2)
        self.assertEqual(reference["final_llr_f64_bits"], [self._float_bits(value) for value in final])
        self.assertEqual(
            tuple(int(bit) for bit in reference["hard_bits"]),
            tuple(0 if value >= 0.0 else 1 for value in final),
        )
        self.assertEqual(self.packet_bits, decode(llrs))

    def test_TC_PHY11_003_puncturing_and_rate_recovery(self):
        encoded = encode(self.packet_bits)
        self.assertEqual(DATA_PATTERN_6, ((1, 1, 0, 0, 0, 0), (1, 0, 0, 0, 1, 0)))
        self.assertEqual(10, sum(sum(row) for row in TAIL_PATTERN_6A))
        self.assertEqual(encoded.channel_bits, puncture(encoded.data_rows, encoded.tail_rows))
        llrs = tuple(float(index + 1) for index in range(874))
        data, tail = rate_recover(llrs)
        self.assertEqual(432, len(data))
        self.assertEqual(6, len(tail))
        position = 0
        for rows, patterns in ((data, DATA_PATTERN_6), (tail, TAIL_PATTERN_6A)):
            for row_index, row in enumerate(rows):
                pattern = patterns[row_index % len(patterns)]
                for value, keep in zip(row, pattern):
                    if keep:
                        self.assertEqual(llrs[position], value)
                        position += 1
                    else:
                        self.assertEqual(0.0, value)
        self.assertEqual(874, position)

    def test_TC_PHY11_004_scrambler_reset_and_soft_inverse(self):
        channel = encode(self.packet_bits).channel_bits
        expected_pn = tuple(int(bit) for bit in self.stages["scrambler_pn_bits"])
        expected_scrambled = tuple(int(bit) for bit in self.stages["scrambled_bits"])
        self.assertEqual(expected_pn, PN_SEQUENCE_874)
        self.assertEqual(expected_scrambled, scramble(channel))
        self.assertEqual(expected_scrambled, scramble(channel), "scrambler must reset per Packet")
        self.assertEqual(channel, descramble_bits(expected_scrambled))
        llrs = tuple(3.0 if bit == 0 else -3.0 for bit in expected_scrambled)
        restored = descramble_llrs(llrs)
        self.assertEqual(channel, tuple(0 if value >= 0.0 else 1 for value in restored))
        asm_encoded = tuple(int(bit) for bit in self.asm["turbo_encoded_with_tail_394"])
        asm_pn = pn_sequence(len(asm_encoded))
        asm_scrambled = tuple(bit ^ pn for bit, pn in zip(asm_encoded, asm_pn))
        self.assertEqual(self.asm["scrambler_pn_394"], "".join(map(str, asm_pn)))
        self.assertEqual(self.asm["scrambled_394"], "".join(map(str, asm_scrambled)))


if __name__ == "__main__":
    unittest.main()
