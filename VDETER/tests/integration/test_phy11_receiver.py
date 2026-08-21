"""TC-PHY11-011..015: acquisition, impairment, errors and performance gates."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.phy11 import (
    CHANNEL,
    FIRST_SAMPLE_AT,
    VECTOR_ROOT,
    canonical_packet,
    decode_public,
    load_json,
    noise_only_blocks,
    noisy_ideal_blocks,
    operational_phy,
    sample_block,
    Sha256NormalStream,
    vector_samples,
)
from vdeter.common.contracts.phy import PhyDecodeRequest, PhyEncodeRequest, PhyRecoveryStatus
from vdeter.common.contracts.radio import SAMPLE_FORMAT_TEST_IQ_INT16_LE, SampleBlock
from vdeter.common.errors import ErrorCode
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.time import ClockDomain, TestTimeSource, TimePoint
from vdeter.link.packet.crc import has_zero_residue
from vdeter.phy.burst.framing import build
from vdeter.phy.burst.link_id import CODEWORDS, LinkIdDetectionError, codeword, detect
from vdeter.phy.burst.rrc import make_slot
from vdeter.phy.burst.synchronization import (
    CANDIDATE_CORRELATION_THRESHOLD,
    CONFIRMATION_CORRELATION_THRESHOLD,
    PEAK_RATIO_THRESHOLD,
    NoBurstDetected,
    SynchronizationFailed,
    acquire,
)
from vdeter.phy.modulation.pi4_qpsk import label_pairs, map_labels
from vdeter.phy.receiver import (
    FecDecodeFailed,
    LinkIdNotAllowed,
    Phy11Receiver,
    UnsupportedDetectedLinkId,
)
from vdeter.phy.transmitter import Link11Phy, Phy11Transmitter
from vdeter.profiles.link_phy import PhyProfileEntry, build_phy_profile_registry
from vdeter.profiles.models import ExplicitRegistry, RegistryScope


def waveform_with_link_bits(bits) -> tuple[complex, ...]:
    stages = build(canonical_packet())
    labels = stages.training_labels + label_pairs(tuple(bits)) + stages.data_labels
    return make_slot(map_labels(labels))


class Phy11ReceiverTest(unittest.TestCase):
    def test_TC_PHY11_011_two_stage_burst_no_burst_and_timing_edges(self):
        receiver = Phy11Receiver()
        for samples in ((), (0.0 + 0.0j,) * 2304):
            with self.subTest(sample_count=len(samples)):
                with self.assertRaises(NoBurstDetected):
                    receiver.decode(samples)
        self.assertEqual((0.30, 0.525, 1.05), (
            CANDIDATE_CORRELATION_THRESHOLD,
            CONFIRMATION_CORRELATION_THRESHOLD,
            PEAK_RATIO_THRESHOLD,
        ))
        for case in ("ideal", "maximum_edges"):
            samples = vector_samples(f"rx_samples_v1/{case}.cf32le")
            acquisition = acquire(samples)
            self.assertGreaterEqual(acquisition.correlation, 0.525)
            self.assertGreaterEqual(acquisition.peak_ratio, 1.05)
            self.assertEqual(canonical_packet(), receiver.decode(samples).packet)

        phy = operational_phy()
        empty = SampleBlock(
            sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
            sample_rate_hz=76800,
            sample_count=0,
            data=b"",
            first_sample_at=FIRST_SAMPLE_AT,
        )
        result = phy.decode_samples(PhyDecodeRequest(CHANNEL, empty, (LinkId(11),)))
        self.assertTrue(result.is_ok)
        self.assertIs(PhyRecoveryStatus.NO_BURST_DETECTED, result.value.status)
        self.assertEqual((), result.value.packets)
        self.assertEqual(FIRST_SAMPLE_AT, result.value.received_at)

    def test_TC_PHY11_012_frozen_impairments_private_and_public_paths(self):
        packet = canonical_packet()
        manifest = load_json("manifest_v1.json")
        cases = load_json("rx_impairments_v1.json")["cases"]
        receiver = Phy11Receiver()
        phy = operational_phy()
        report = {
            "schema_version": 1,
            "channel_id": "phy11-impairment-channel-v1",
            "timestamp_tolerance_ns": 13021,
            "cases": {},
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                relative = f"rx_samples_v1/{name}.cf32le"
                raw = (VECTOR_ROOT / relative).read_bytes()
                self.assertEqual(case["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(manifest["artifacts"][relative]["sha256"], case["sha256"])
                samples = vector_samples(relative)
                core = receiver.decode(samples)
                self.assertEqual(packet, core.packet)
                self.assertLessEqual(abs(core.cfo_hz - case["cfo_hz"]), 25.0)
                self.assertLessEqual(abs((core.rho_hat - 1.0) * 1e6 - case["sro_ppm"]), 3.01)
                self.assertLessEqual(abs(core.gain_magnitude - case["gain"]), 0.10 * case["gain"])
                public = decode_public(phy, samples)
                self.assertTrue(public.is_ok, getattr(public, "error", None))
                self.assertIs(PhyRecoveryStatus.RECOVERED, public.value.status)
                self.assertEqual(packet, public.value.packets[0].packet)
                rho = 1.0 + case["sro_ppm"] * 1e-6
                expected_samples = case["start_sample"] + rho * case["tau_samples"]
                expected_ns = round(expected_samples * 1_000_000_000.0 / 76800.0)
                actual_ns = public.value.received_at.difference_nanoseconds(FIRST_SAMPLE_AT)
                self.assertLessEqual(abs(actual_ns - expected_ns), 13021)
                recovered_sro_ppm = (core.rho_hat - 1.0) * 1e6
                expected_training_epoch = case["start_sample"] + rho * (
                    32.0 + case["tau_samples"]
                )
                report["cases"][name] = {
                    "input_sha256": case["sha256"],
                    "injected": {
                        "phase_rad": case["phase_rad"],
                        "cfo_hz": case["cfo_hz"],
                        "sro_ppm": case["sro_ppm"],
                        "gain": case["gain"],
                        "start_sample": case["start_sample"],
                        "tau_samples": case["tau_samples"],
                        "expected_training_epoch_samples": expected_training_epoch,
                        "expected_burst_start_ns": expected_ns,
                    },
                    "recovered": {
                        "cfo_hz": core.cfo_hz,
                        "sro_ppm": recovered_sro_ppm,
                        "gain": core.gain_magnitude,
                        "training_epoch_samples": core.training_epoch_samples,
                        "burst_start_ns": actual_ns,
                        "packet_exact": core.packet == packet,
                    },
                    "absolute_errors": {
                        "cfo_hz": abs(core.cfo_hz - case["cfo_hz"]),
                        "sro_ppm": abs(recovered_sro_ppm - case["sro_ppm"]),
                        "gain": abs(core.gain_magnitude - case["gain"]),
                        "training_epoch_samples": abs(
                            core.training_epoch_samples - expected_training_epoch
                        ),
                        "burst_start_ns": abs(actual_ns - expected_ns),
                    },
                }
        output = os.environ.get("VDETER_STAGE2_IMPAIRMENT_OUTPUT")
        if output:
            Path(output).write_text(
                json.dumps(report, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

    def test_TC_PHY11_013_link_id_matrix_invalid_unsupported_and_allowed(self):
        self.assertEqual(64, len(set(CODEWORDS)))
        for left in range(64):
            for right in range(left + 1, 64):
                self.assertGreaterEqual(sum(a != b for a, b in zip(CODEWORDS[left], CODEWORDS[right])), 16)
            llrs = tuple(10.0 if bit == 0 else -10.0 for bit in codeword(left))
            self.assertEqual(left, detect(llrs))
        invalid = list(codeword(11))
        for index in range(8):
            invalid[index] ^= 1
        invalid_llrs = tuple(10.0 if bit == 0 else -10.0 for bit in invalid)
        with self.assertRaises(LinkIdDetectionError):
            detect(invalid_llrs)
        with self.assertRaises(LinkIdDetectionError):
            detect((0.0,) * 32)
        with self.assertRaises(UnsupportedDetectedLinkId) as captured:
            Phy11Receiver().decode(waveform_with_link_bits(codeword(5)))
        self.assertEqual(5, captured.exception.link_id)
        disallowed = decode_public(operational_phy(), waveform_with_link_bits(codeword(11)), (LinkId(17),))
        self.assertTrue(disallowed.is_err)
        self.assertEqual(ErrorCode.PHY_LINK_ID_NOT_ALLOWED, disallowed.error.code)

    def test_TC_PHY11_014_structured_errors_precedence_crc_and_timestamp(self):
        time_source = TestTimeSource(utc_nanoseconds=1_000_000_000)
        ids = DeterministicIdSource(1414)
        unfrozen = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        phy_unfrozen = Link11Phy(unfrozen, time_source, ids)
        bad_first = phy_unfrozen.encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(17), CHANNEL, time_source.now_utc(), b"x"
        ))
        self.assertEqual(ErrorCode.REGISTRY_NOT_FROZEN, bad_first.error.code)

        self.assertEqual(ErrorCode.INVALID_REQUEST, phy_unfrozen.encode_packet(object()).error.code)
        self.assertEqual(ErrorCode.INVALID_REQUEST, phy_unfrozen.decode_samples(object()).error.code)

        absent = Link11Phy(None, time_source, ids)
        absent_result = absent.encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(11), CHANNEL, time_source.now_utc(), canonical_packet()
        ))
        self.assertEqual(ErrorCode.REGISTRY_NOT_FROZEN, absent_result.error.code)

        test_registry = build_phy_profile_registry(RegistryScope.TEST).value
        self.assertTrue(test_registry.freeze().is_ok)
        wrong_scope = Link11Phy(test_registry, time_source, ids).encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(11), CHANNEL, time_source.now_utc(), canonical_packet()
        ))
        self.assertEqual(ErrorCode.REGISTRY_SCOPE_MISMATCH, wrong_scope.error.code)

        empty_registry = ExplicitRegistry(
            "phy_profile", RegistryScope.OPERATIONAL, "empty-stage2-test", PhyProfileEntry
        )
        self.assertTrue(empty_registry.freeze().is_ok)
        empty_phy = Link11Phy(empty_registry, time_source, ids)
        unsupported_before_lookup = empty_phy.encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(17), CHANNEL, time_source.now_utc(), b"x"
        ))
        self.assertEqual(ErrorCode.PHY_UNSUPPORTED_LINK_ID, unsupported_before_lookup.error.code)
        unknown_profile = empty_phy.encode_packet(PhyEncodeRequest(
            ids.new_packet_id(), LinkId(11), CHANNEL, time_source.now_utc(), b"x"
        ))
        self.assertEqual(ErrorCode.REGISTRY_UNKNOWN_KEY, unknown_profile.error.code)

        phy = operational_phy()
        packet_id = ids.new_packet_id()
        unsupported = phy.encode_packet(PhyEncodeRequest(packet_id, LinkId(17), CHANNEL, FIRST_SAMPLE_AT, b"x"))
        self.assertEqual(ErrorCode.PHY_UNSUPPORTED_LINK_ID, unsupported.error.code)
        wrong_length = phy.encode_packet(PhyEncodeRequest(packet_id, LinkId(11), CHANNEL, FIRST_SAMPLE_AT, b"x"))
        self.assertEqual(ErrorCode.PHY_PACKET_LENGTH_INVALID, wrong_length.error.code)
        wrong_clock = TimePoint.from_nanoseconds(
            FIRST_SAMPLE_AT.total_nanoseconds(), ClockDomain.UTC, "different-utc"
        )
        clock_error = phy.encode_packet(PhyEncodeRequest(
            packet_id, LinkId(11), CHANNEL, wrong_clock, canonical_packet()
        ))
        self.assertEqual(ErrorCode.CLOCK_DOMAIN_MISMATCH, clock_error.error.code)
        expired = phy.encode_packet(PhyEncodeRequest(packet_id, LinkId(11), CHANNEL, FIRST_SAMPLE_AT.shifted(-1), canonical_packet()))
        self.assertEqual(ErrorCode.PHY_TRANSMIT_DEADLINE_EXPIRED, expired.error.code)
        with mock.patch("vdeter.phy.transmitter.Phy11Transmitter.encode", side_effect=ValueError("fault")):
            invariant = phy.encode_packet(PhyEncodeRequest(
                packet_id, LinkId(11), CHANNEL, FIRST_SAMPLE_AT, canonical_packet()
            ))
        self.assertEqual(ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, invariant.error.code)

        monotonic_sample = SampleBlock(
            SAMPLE_FORMAT_TEST_IQ_INT16_LE,
            19200,
            0,
            b"",
            TimePoint.from_nanoseconds(0, ClockDomain.MONOTONIC, "test-monotonic"),
        )
        rx_clock = phy.decode_samples(PhyDecodeRequest(CHANNEL, monotonic_sample, (LinkId(17),)))
        self.assertEqual(ErrorCode.CLOCK_DOMAIN_MISMATCH, rx_clock.error.code)
        wrong_rate = SampleBlock(
            SAMPLE_FORMAT_TEST_IQ_INT16_LE, 19200, 0, b"", FIRST_SAMPLE_AT
        )
        rejected = phy.decode_samples(PhyDecodeRequest(CHANNEL, wrong_rate, (LinkId(11),)))
        self.assertEqual(ErrorCode.PHY_SAMPLE_RATE_MISMATCH, rejected.error.code)
        rx_unknown_before_metadata = empty_phy.decode_samples(
            PhyDecodeRequest(CHANNEL, monotonic_sample, (LinkId(17),))
        )
        self.assertEqual(ErrorCode.REGISTRY_UNKNOWN_KEY, rx_unknown_before_metadata.error.code)
        no_burst_with_unsupported_allowed = decode_public(
            phy, (), (LinkId(17), LinkId(19))
        )
        self.assertTrue(no_burst_with_unsupported_allowed.is_ok)
        self.assertIs(
            PhyRecoveryStatus.NO_BURST_DETECTED,
            no_burst_with_unsupported_allowed.value.status,
        )

        injected_errors = (
            (SynchronizationFailed("fault"), ErrorCode.PHY_SYNCHRONIZATION_FAILED),
            (LinkIdDetectionError("fault"), ErrorCode.PHY_LINK_ID_DETECTION_FAILED),
            (UnsupportedDetectedLinkId(5), ErrorCode.PHY_UNSUPPORTED_LINK_ID),
            (LinkIdNotAllowed("fault"), ErrorCode.PHY_LINK_ID_NOT_ALLOWED),
            (FecDecodeFailed("fault"), ErrorCode.PHY_FEC_DECODE_FAILED),
        )
        for injected, expected_code in injected_errors:
            with self.subTest(injected=type(injected).__name__):
                with mock.patch(
                    "vdeter.phy.transmitter.Phy11Receiver.decode", side_effect=injected
                ):
                    failed = decode_public(phy, ())
                self.assertEqual(expected_code, failed.error.code)

        nonfinite = SimpleNamespace(training_epoch_samples=float("nan"), rho_hat=1.0)
        with mock.patch("vdeter.phy.transmitter.Phy11Receiver.decode", return_value=nonfinite):
            internal = decode_public(phy, ())
        self.assertEqual(ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN, internal.error.code)

        corrupted = bytearray(canonical_packet())
        corrupted[20] ^= 1
        self.assertFalse(has_zero_residue(bytes(corrupted)))
        samples = Phy11Transmitter().encode(bytes(corrupted)).samples
        recovered = Phy11Receiver().decode(samples).packet
        self.assertEqual(bytes(corrupted), recovered, "PHY must not enforce Packet CRC")
        self.assertFalse(has_zero_residue(recovered))

        registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        self.assertTrue(registry.freeze().is_ok)
        actual_ids = DeterministicIdSource(0x1414)
        expected_ids = DeterministicIdSource(0x1414)
        timestamp_phy = Link11Phy(registry, time_source, actual_ids)
        timestamp_samples = vector_samples("rx_samples_v1/ideal.cf32le")
        core = Phy11Receiver().decode(timestamp_samples)
        timestamp_result = decode_public(timestamp_phy, timestamp_samples)
        self.assertTrue(timestamp_result.is_ok, getattr(timestamp_result, "error", None))
        recovered_packet = timestamp_result.value.packets[0]
        self.assertEqual(expected_ids.new_packet_id(), recovered_packet.packet_id)
        burst_start_samples = core.training_epoch_samples - 32.0 * core.rho_hat
        offset = burst_start_samples * 1_000_000_000.0 / 76800.0
        expected_offset_ns = int(offset + 0.5) if offset >= 0.0 else int(offset - 0.5)
        expected_received_at = FIRST_SAMPLE_AT.shifted(expected_offset_ns)
        self.assertEqual(expected_received_at, recovered_packet.received_at)
        self.assertEqual(expected_received_at, timestamp_result.value.received_at)

        with self.assertRaises(InvalidValueError):
            PhyEncodeRequest(packet_id, LinkId(11), CHANNEL, TimePoint.from_nanoseconds(
                0, ClockDomain.MONOTONIC, "test-monotonic"
            ), canonical_packet())
        with self.assertRaises(InvalidValueError):
            PhyEncodeRequest(packet_id, LinkId(11), CHANNEL, FIRST_SAMPLE_AT, bytearray(54))
        with self.assertRaises(InvalidValueError):
            SampleBlock("unsupported", 76800, 0, b"", FIRST_SAMPLE_AT)
        with self.assertRaises(InvalidValueError):
            SampleBlock(SAMPLE_FORMAT_TEST_IQ_INT16_LE, 76800, 1, b"", FIRST_SAMPLE_AT)
        with self.assertRaises(InvalidValueError):
            PhyDecodeRequest(CHANNEL, sample_block(()), (11,))

    def test_TC_PHY11_015_awgn_characterization_project_gate_and_noise_only(self):
        packet = canonical_packet()
        phy = operational_phy()
        report = {"official_1db": Counter(), "project_7_5db": Counter(), "noise_only": Counter()}

        random_reference = load_json("manifest_v1.json")["random"]
        normal_stream = Sha256NormalStream(
            random_reference["awgn_domain_ascii"].encode("ascii"),
            random_reference["awgn_seed_u64"],
        )
        normal_pairs = tuple(normal_stream.pair() for _ in range(1024))
        self.assertEqual(
            random_reference["first_2048_words_u64"], normal_stream.consumed_words
        )
        for actual, expected in zip(
            normal_pairs, random_reference["first_1024_standard_normal_iq"]
        ):
            self.assertLessEqual(abs(actual[0] - expected[0]), 2e-15)
            self.assertLessEqual(abs(actual[1] - expected[1]), 2e-15)

        for label, es_n0_db in (("official_1db", 1.0), ("project_7_5db", 7.5)):
            counts = report[label]
            for samples in noisy_ideal_blocks(es_n0_db, 1000):
                result = decode_public(phy, samples)
                if result.is_err:
                    counts[f"error:{result.error.code}"] += 1
                elif result.value.status is PhyRecoveryStatus.NO_BURST_DETECTED:
                    counts["no_burst"] += 1
                elif result.value.packets[0].packet == packet:
                    counts["exact"] += 1
                    self.assertTrue(has_zero_residue(result.value.packets[0].packet))
                else:
                    counts["wrong_packet"] += 1
            self.assertEqual(1000, sum(counts.values()))
        self.assertGreaterEqual(report["project_7_5db"]["exact"], 990)

        for samples in noise_only_blocks(10000):
            result = decode_public(phy, samples)
            if result.is_ok and result.value.status is PhyRecoveryStatus.NO_BURST_DETECTED:
                report["noise_only"]["no_burst"] += 1
            elif result.is_err:
                report["noise_only"][f"error:{result.error.code}"] += 1
            else:
                report["noise_only"]["recovered"] += 1
        self.assertEqual({"no_burst": 10000}, dict(report["noise_only"]))

        output = os.environ.get("VDETER_STAGE2_PERFORMANCE_OUTPUT")
        if output:
            Path(output).write_text(
                json.dumps({key: dict(value) for key, value in report.items()}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
