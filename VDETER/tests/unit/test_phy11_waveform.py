"""TC-PHY11-005..007: modulation, burst constants and frozen waveform."""

from __future__ import annotations

import hashlib
import math
import struct
import unittest

from tests.support.phy11 import VECTOR_ROOT, canonical_packet, load_json
from vdeter.phy.burst.framing import build
from vdeter.phy.burst.link_id import LINK_ID_11_CODEWORD, codeword
from vdeter.phy.burst.rrc import (
    RRC_TAP_BINARY64_BITS,
    RRC_TAPS,
    polyphase_energies,
    pulse_shaped_carrier,
    to_cf32le,
)
from vdeter.phy.burst.synchronization import (
    TRAINING_SYMBOLS,
    SynchronizationFailed,
    estimate_training_noise,
)
from vdeter.phy.modulation.pi4_qpsk import (
    GREEN,
    PURPLE,
    NOISE_VARIANCE_FLOOR,
    hard_label,
    map_labels,
    soft_demap,
)


class Phy11WaveformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stages = load_json("tx_stages_v1.json")
        cls.official = load_json("official_constants_v1.json")
        cls.asm = load_json("asm_link5_common_v1.json")
        cls.built = build(canonical_packet())

    @staticmethod
    def _float_from_bits(value: int) -> float:
        return struct.unpack("<d", struct.pack("<Q", value))[0]

    @staticmethod
    def _float_bits(value: float) -> int:
        return struct.unpack("<Q", struct.pack("<d", value))[0]

    def test_TC_PHY11_005_alternating_mapping_demapping_and_noise_floor(self):
        labels = ("00", "01", "11", "10") * 4
        symbols = map_labels(labels)
        for index, (label, symbol) in enumerate(zip(labels, symbols)):
            expected = (GREEN if index % 2 == 0 else PURPLE)[label]
            self.assertEqual(expected, symbol)
            self.assertEqual(label, hard_label(symbol, index))
        llrs = soft_demap(symbols, 0, NOISE_VARIANCE_FLOOR)
        hard = tuple(0 if value >= 0.0 else 1 for value in llrs)
        self.assertEqual(tuple(int(bit) for label in labels for bit in label), hard)
        self.assertEqual(0x3D719799812DEA11, struct.unpack("<Q", struct.pack("<d", NOISE_VARIANCE_FLOOR))[0])
        noise = self.stages["ideal_rx_inverse"]["noise_estimator"]
        observed = tuple(
            complex(self._float_from_bits(real), self._float_from_bits(imag))
            for real, imag in noise["observed_training_iq_f64_bits"]
        )
        estimate = estimate_training_noise(observed)
        self.assertEqual(noise["raw_variance_f64_bits"], self._float_bits(estimate))
        self.assertEqual(noise["variance_f64_bits"], self._float_bits(estimate))
        self.assertEqual(noise["clean_variance_f64_bits"], self._float_bits(estimate_training_noise(TRAINING_SYMBOLS)))
        with self.assertRaises(SynchronizationFailed):
            estimate_training_noise(TRAINING_SYMBOLS[:-1])

    def test_TC_PHY11_006_training_link_id_and_body_structure(self):
        self.assertEqual(tuple(int(bit) for bit in self.official["link_id_11_codeword"]), LINK_ID_11_CODEWORD)
        self.assertEqual(LINK_ID_11_CODEWORD, codeword(11))
        self.assertEqual(27, len(self.built.training_labels))
        self.assertEqual(16, len(self.built.link_id_labels))
        self.assertEqual(437, len(self.built.data_labels))
        self.assertEqual(480, len(self.built.body_labels))
        self.assertEqual(tuple(self.stages["body_labels"]), self.built.body_labels)
        expected_symbols = tuple(complex(i, q) for i, q in self.stages["body_symbols_iq"])
        for expected, actual in zip(expected_symbols, self.built.body_symbols):
            self.assertLessEqual(abs(expected - actual), 1e-12)
        asm_codeword = tuple(int(bit) for bit in self.asm["link_id_codeword_32"])
        self.assertEqual(asm_codeword, codeword(5))
        self.assertEqual(tuple(self.asm["link_id_labels_16"]), tuple(
            self.asm["link_id_codeword_32"][index:index + 2]
            for index in range(0, 32, 2)
        ))
        self.assertEqual(tuple(self.asm["training_labels_27"]), self.built.training_labels)
        self.assertEqual(tuple(self.asm["body_labels_240"]), (
            tuple(self.asm["training_labels_27"])
            + tuple(self.asm["link_id_labels_16"])
            + tuple(self.asm["data_labels_197"])
        ))

    def test_TC_PHY11_007_rrc_ramp_guard_slot_and_serialization(self):
        self.assertEqual(41, len(RRC_TAPS))
        self.assertEqual(tuple(self.official["rrc_tap_binary64_bits"]), RRC_TAP_BINARY64_BITS)
        tap_bytes = b"".join(struct.pack("<Q", value) for value in RRC_TAP_BINARY64_BITS)
        self.assertEqual(self.official["rrc_taps_sha256"], hashlib.sha256(tap_bytes).hexdigest())
        energies = polyphase_energies()
        self.assertLessEqual(abs(sum(energies) / 4.0 - 0.25), 1e-15)

        samples = self.built.samples
        self.assertEqual(2048, len(samples))
        self.assertTrue(all(value == 0.0 + 0.0j for value in samples[1984:]))
        extended = pulse_shaped_carrier(self.built.body_symbols)
        for index in range(32):
            up = 10.0 ** ((-50.0 + 50.0 * index / 32.0) / 20.0)
            down = 10.0 ** ((-50.0 * index / 32.0) / 20.0)
            self.assertLessEqual(abs(samples[index] - extended[index] * up), 1e-15)
            self.assertLessEqual(abs(samples[1952 + index] - extended[1952 + index] * down), 1e-15)
        self.assertEqual(extended[32:1952], samples[32:1952])
        ramp_up_crossing_us = (48.5 / (50.0 / 32.0)) / 76800.0 * 1e6
        self.assertGreaterEqual(ramp_up_crossing_us, 400.0)
        self.assertLessEqual(ramp_up_crossing_us, 420.0)
        self.assertLessEqual(32.0 / 76800.0 * 1e6, 417.0)

        frozen = (VECTOR_ROOT / "tx_samples_v1.cf32le").read_bytes()
        self.assertEqual(frozen, to_cf32le(samples))
        self.assertEqual(load_json("manifest_v1.json")["artifacts"]["tx_samples_v1.cf32le"]["sha256"], hashlib.sha256(frozen).hexdigest())


if __name__ == "__main__":
    unittest.main()
