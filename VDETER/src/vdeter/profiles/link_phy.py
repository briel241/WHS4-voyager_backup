"""Link Profile과 PHY Profile registry (`stage0_baseline.md` §7.2).

두 profile은 같은 `LinkId`로 결합되지만 독립된 typed entry, version과 fingerprint를 가진다.
Link는 Link Profile만, PHY는 PHY Profile만 사용한다.

단계 0은 profile *값*을 확정하지 않는다.

- Link-visible Packet capacity, padding, Message 배치·fragment 용량과 CRC: 단계 1
- Link ID 11의 FEC, interleaving, modulation, burst, symbol/sample parameter: 단계 2
- Link ID 17·19: 단계 6

따라서 운영 catalogue는 비어 있고, 시험 catalogue만 registry 생명주기 검증용 항목을
가진다. 시험 항목은 규범 profile이 아니며 운영 registry에 등록할 수 없다.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.identifiers import LinkId
from ..common.results import Ok
from .models import ExplicitRegistry, RegistryScope, SourceReference, SupportStatus

#: 상위 명세 §4.1.7의 구현 대상 Link ID. 값은 단계 1·2·6에서 확정한다.
PLANNED_LINK_IDS = (11, 17, 19)

#: 단계 0 registry 생명주기 시험 전용 Link ID. 규범 profile이 아니다.
TEST_LINK_ID = 63

PHY11_PROFILE_SCHEMA_ID = "phy11-profile-v2"
STAGE6_PROFILE_SCHEMA_ID = "stage6-phy-profile-v4"
STAGE6_REGISTRY_CONTENT_VERSION = "stage6-phy11-17-19-v5"
PHY11_REGISTRY_CONTENT_VERSION = "stage2-phy11-v2"
PHY11_REGISTRY_FINGERPRINT = (
    "ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898"
)
PHY11_RRC_TAPS_SHA256 = (
    "1349ba6bac54cdd4a5e8cc3a89c4e22571a74cf20cf2f92dcf6b50b4227f588d"
)
PHY11_TRAINING_BITS = "111111001101010000011001010"
PHY11_LINK_ID_GENERATOR_ROWS = (
    "10000010111010011110100110010110",
    "01000001110101011101010101010101",
    "00100011101100110011001100110011",
    "00010011000011111000111100001111",
    "00001000011111110000000011111111",
    "00000100000000000111111111111111",
)
PHY11_LINK_ID_SCRAMBLING_WORD = "11000010111000101000111001001111"
PHY11_LINK_ID_CODEWORD = "11101101001011101100001001111100"
PHY11_RRC_TAP_BINARY64_BITS = (
    0x3F4DCDE66A98578B, 0xBF71779D20890A13, 0xBF766B3AE585E3F6,
    0xBF477C1DE743A561, 0x3F785F045282B2AF, 0x3F7D3453C379CF3C,
    0xBF4D9CEB7B71EE04, 0xBF8A4CE210E2EF2B, 0xBF90F5049BAD21E6,
    0xBF69BDEEE4E0F06C, 0x3F970CF32419485B, 0x3FA4B7AD1A9AF1B1,
    0x3F9CFB1111472F44, 0xBF93832F686AAFC1, 0xBFB3A9D5F1BA7197,
    0xBFB8F3245A6EF009, 0xBFA333D4E60A2A84, 0x3FBC81C997CBFC2B,
    0x3FD3B1732D8240C9, 0x3FDE6A59EA8B2D6F, 0x3FE1503C2AB12187,
    0x3FDE6A59EA8B2D6F, 0x3FD3B1732D8240C9, 0x3FBC81C997CBFC2B,
    0xBFA333D4E60A2A84, 0xBFB8F3245A6EF009, 0xBFB3A9D5F1BA7197,
    0xBF93832F686AAFC1, 0x3F9CFB1111472F44, 0x3FA4B7AD1A9AF1B1,
    0x3F970CF32419485B, 0xBF69BDEEE4E0F06C, 0xBF90F5049BAD21E6,
    0xBF8A4CE210E2EF2B, 0xBF4D9CEB7B71EE04, 0x3F7D3453C379CF3C,
    0x3F785F045282B2AF, 0xBF477C1DE743A561, 0xBF766B3AE585E3F6,
    0xBF71779D20890A13, 0x3F4DCDE66A98578B,
)


def _freeze_mapping(mapping: dict) -> tuple:
    def freeze(value):
        if isinstance(value, dict):
            return (
                "__canonical_map__",
                tuple((key, freeze(item)) for key, item in value.items()),
            )
        if isinstance(value, (tuple, list)):
            return tuple(freeze(item) for item in value)
        if isinstance(value, (str, int, bool, float)) or value is None:
            return value
        raise InvalidValueError(
            f"PhyProfileParameters에는 canonical scalar/tuple만 허용합니다 ({type(value).__name__})"
        )

    return freeze(mapping)


def _thaw_mapping(items: tuple) -> dict:
    def thaw(value):
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "__canonical_map__":
            return {key: thaw(item) for key, item in value[1]}
        if isinstance(value, tuple):
            return [thaw(item) for item in value]
        return value

    return thaw(items)


@dataclass(frozen=True, slots=True)
class PhyProfileParameters:
    """Stage 2 closed Link ID 11 parameter object used by the registry fingerprint."""

    profile_schema_id: str
    identity: tuple
    counts: tuple
    rates: tuple
    fec: tuple
    scrambler: tuple
    training: tuple
    link_id_code: tuple
    modulation: tuple
    waveform: tuple
    receiver: tuple
    timestamp: tuple
    vectors: tuple
    normative_source: tuple | None = None

    def __post_init__(self) -> None:
        if self.profile_schema_id == PHY11_PROFILE_SCHEMA_ID:
            if self.normative_source is not None:
                raise InvalidValueError(
                    "Link 11 schema는 normative_source key를 갖지 않습니다"
                )
        elif self.profile_schema_id == STAGE6_PROFILE_SCHEMA_ID:
            if not isinstance(self.normative_source, tuple):
                raise InvalidValueError(
                    "stage6 schema에는 normative_source가 필요합니다"
                )
        else:
            raise InvalidValueError("PhyProfileParameters.profile_schema_id가 승인값과 다릅니다")
        for name in (
            "identity", "counts", "rates", "fec", "scrambler", "training",
            "link_id_code", "modulation", "waveform", "receiver", "timestamp", "vectors",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise InvalidValueError(f"PhyProfileParameters.{name}: immutable tuple이어야 합니다")

    def fingerprint_content(self) -> dict:
        return {
            "profile_schema_id": self.profile_schema_id,
            "identity": _thaw_mapping(self.identity),
            "counts": _thaw_mapping(self.counts),
            "rates": _thaw_mapping(self.rates),
            "fec": _thaw_mapping(self.fec),
            "scrambler": _thaw_mapping(self.scrambler),
            "training": _thaw_mapping(self.training),
            "link_id_code": _thaw_mapping(self.link_id_code),
            "modulation": _thaw_mapping(self.modulation),
            "waveform": _thaw_mapping(self.waveform),
            "receiver": _thaw_mapping(self.receiver),
            "timestamp": _thaw_mapping(self.timestamp),
            "vectors": _thaw_mapping(self.vectors),
        } | (
            {"normative_source": _thaw_mapping(self.normative_source)}
            if self.normative_source is not None
            else {}
        )

    def section(self, name: str) -> dict:
        if name not in {
            "identity", "counts", "rates", "fec", "scrambler", "training",
            "link_id_code", "modulation", "waveform", "receiver", "timestamp", "vectors",
        }:
            raise KeyError(name)
        return _thaw_mapping(getattr(self, name))


def _phy11_content() -> dict:
    return {
        "profile_schema_id": PHY11_PROFILE_SCHEMA_ID,
        "identity": {
            "baseline_id": "VDETER-STAGE2-PHY11-BL-001",
            "baseline_version": "0.4.6",
            "link_id": 11,
            "pl_format": "TER-MCS-1.25",
            "scope": "OPERATIONAL",
            "source_document": "ITU-R M.2092-2",
            "source_edition": "02/2026",
            "source_location": "Annex 2 §A2-1.2; Annex 4 §A4-3, §A4-4",
            "support_status": "SUPPORTED",
        },
        "counts": {
            "active_samples": 1984, "active_symbols": 496, "body_samples": 1920,
            "body_symbols": 480, "channel_bits": 874, "data_channel_bits": 864,
            "data_symbols": 437, "guard_samples": 64, "guard_symbols": 16,
            "information_bits": 432, "link_id_symbols": 16, "packet_bytes": 54,
            "phy_padding_bits": 0, "ramp_down_samples": 32, "ramp_down_symbols": 8,
            "ramp_up_samples": 32, "ramp_up_symbols": 8, "slot_samples": 2048,
            "slot_symbols": 512, "tail_channel_bits": 10, "training_symbols": 27,
        },
        "rates": {
            "bits_per_data_symbol": 2, "channel_bandwidth_hz": 25000,
            "sample_rate_hz": 76800, "samples_per_symbol": 4,
            "signal_bandwidth_hz": 25000, "symbol_energy_ratio": (1, 1),
            "symbol_rate_hz": 19200, "official_es_n0_db_ratio": (1, 1),
            "official_nominal_average_output_power_w_ratio": (25, 2),
        },
        "fec": {
            "data_puncturing_id": "6",
            "data_puncturing_rows": ((1,1,0,0,0,0), (1,0,0,0,1,0)),
            "decoder": {
                "branch_metric_id": "half-grouped-systematic-apriori-parity-v1",
                "crc_aided_termination": False, "early_termination": False,
                "extrinsic_clipping": False, "extrinsic_damping": False,
                "extrinsic_scale_ratio": (1,1), "hard_tie_bit": 0,
                "identifier": "log-map-f64-8iter-v1", "iterations": 8,
                "jacobian_correction": True,
                "jacobian_correction_id": "max-star-log1p-exp-abs-v1",
                "metric_normalization_id": "subtract-step-max-finite-v1",
                "numeric_format": "IEEE-754-binary64-RNE-no-fast-math",
                "steps_per_siso": 435,
            },
            "denominator_exponents": (0,2,3),
            "feedback_formula": "u-xor-D2-xor-D3",
            "identifier": "parallel-rsc-turbo-rate-1-2-puncture-6-tail-6a",
            "interleaver": {
                "identifier": "M2092-internal-interleaver-v1", "k": 432,
                "k1": 2, "k2": 216, "p": (127,191,241,5,83,109,107,179),
            },
            "n0_exponents": (0,1,3), "n1_exponents": (0,1,2,3),
            "rate_recovery_erasure_llr": 0,
            "state_bit_order": ("D1","D2","D3"), "state_id_weights": (4,2,1),
            "stream_order": ("X","Y0","Y1","X_PRIME","Y0_PRIME","Y1_PRIME"),
            "tail_clocks_per_rsc": 3, "tail_input_formula": "D2-xor-D3",
            "tail_puncturing_id": "6a",
            "tail_puncturing_rows": (
                (1,1,0,0,0,0), (1,1,0,0,0,0), (1,0,0,0,0,0),
                (0,0,0,1,1,0), (0,0,0,1,1,0), (0,0,0,1,0,0),
            ),
            "tail_rsc_order": (1,2),
        },
        "scrambler": {
            "bit_count": 874,
            "initial_register_r1_to_r15": (1,0,0,1,0,1,0,1,0,0,0,0,0,0,0),
            "pn_formula": "R14-xor-R15", "polynomial_exponents": (0,14,15),
            "reset": "PER_PACKET",
            "shift": "PN-to-R1-and-R1-through-R14-to-R2-through-R15",
        },
        "training": {"bits": PHY11_TRAINING_BITS, "mapping": (("0","00"),("1","11"))},
        "link_id_code": {
            "codeword_11": PHY11_LINK_ID_CODEWORD,
            "data_bit_order": ("D0","D1","D2","D3","D4","D5"),
            "generator_rows": PHY11_LINK_ID_GENERATOR_ROWS, "hard_tie_bit": 0,
            "identifier": "RM-32-6-biorthogonal-M2092-v1",
            "link_id_11_bits": "001011", "max_valid_hamming_distance": 7,
            "score_accumulation": "binary64-i0-through-i31-no-fma",
            "score_tie_rule": "exactly-one-max-with-distance-at-most-7",
            "scrambling_word": PHY11_LINK_ID_SCRAMBLING_WORD,
        },
        "modulation": {
            "alternation": ("GREEN","PURPLE"),
            "alternation_regions": ("TRAINING","LINK_ID","DATA"),
            "bits_per_symbol": 2, "constellation_scale_f64_bits": 4604544271217802189,
            "first_constellation": "GREEN",
            "green_iq_signs_for_00_01_11_10": ((-1,-1),(-1,1),(1,1),(1,-1)),
            "identifier": "alternating-pi-over-4-QPSK-M2092-v1",
            "label_order": ("00","01","11","10"),
            "mapper_bit_order": ("MSB","LSB"),
            "purple_iq_for_00_01_11_10": ((-1,0),(0,1),(1,0),(0,-1)),
            "ramp_consumes_alternation": False,
        },
        "waveform": {
            "guard_value": "0+0j",
            "ramp": {
                "crossing_interpolation": "dBc-linear-between-sample-nodes-v1",
                "down_db_step_ratio": (-25,16), "down_max_duration_us": (417,1),
                "down_start_db_ratio": (0,1), "down_stored_last_db_ratio": (-775,16),
                "down_virtual_end_db_ratio": (-50,1),
                "identifier": "rrc-continuous-avg-db-linear-v2",
                "phase_context": "HOLD-FIRST-AND-LAST-BODY-SYMBOL",
                "reference_power_ratio": (1,4), "up_db_step_ratio": (25,16),
                "up_start_db_ratio": (-50,1), "up_stored_last_db_ratio": (-25,16),
                "up_timing_end_db_ratio": (-3,2), "up_timing_max_us": (420,1),
                "up_timing_min_us": (400,1), "up_virtual_end_db_ratio": (0,1),
            },
            "rrc": {
                "group_delay_samples": 20, "identifier": "rrc-beta-0.3-span-10-sps-4-v1",
                "normalization": "UNIT_ENERGY", "rolloff_ratio": (3,10),
                "span_symbols": 10, "tap_binary64_bits": PHY11_RRC_TAP_BINARY64_BITS,
                "tap_count": 41, "tap_index_first": -20, "tap_index_last": 20,
                "taps_sha256": PHY11_RRC_TAPS_SHA256,
            },
            "slot_region_sample_counts": (32,1920,32,64),
            "slot_region_order": ("RAMP_UP","BODY","RAMP_DOWN","GUARD"),
            "slot_sample_layout": {
                "body_end": 1951, "body_start": 32, "data_body_end": 1919,
                "data_body_start": 172, "guard_end": 2047, "guard_start": 1984,
                "link_id_body_end": 171, "link_id_body_start": 108,
                "ramp_down_end": 1983, "ramp_down_start": 1952,
                "ramp_up_end": 31, "ramp_up_start": 0,
                "training_body_end": 107, "training_body_start": 0,
            },
            "test_projection": "test-iq-int16-le-round-half-away-v1",
            "vector_sample_format": "cf32le",
        },
        "receiver": {
            "burst_detector": {
                "candidate_correlation_threshold": (3,10),
                "candidate_limit": 8,
                "confirmation_correlation_threshold": (21,40),
                "identifier": "two-stage-normalized-training-correlation-v2",
                "peak_exclusion_symbols": 1, "peak_ratio": (21,20),
                "timing_refinement_denominator": 16,
                "timing_refinement_step_first": -8,
                "timing_refinement_step_last": 8,
                "zero_energy_policy": "NO_CANDIDATE",
            },
            "carrier_phase_interval": "[-pi,pi)",
            "carrier_recovery": {
                "acquisition_cfo_coarse_hz": 50,
                "acquisition_cfo_fine_hz": 5,
                "acquisition_cfo_search_max_hz": 2200,
                "acquisition_cfo_search_min_hz": -2200,
                "initial_decision_directed_frequency_beta_ratio": (3,2000),
                "initial_decision_directed_phase_alpha_ratio": (1,10),
                "decision_directed_frequency_beta_ratio": (1,1000),
                "decision_directed_phase_alpha_ratio": (2,25),
                "fec_aided_passes": 1,
                "identifier": "known-prefix-grid-dd-fec-aided-v2",
                "known_prefix_symbols": 43,
                "reference_cfo_coarse_hz": 25,
                "reference_cfo_fine_hz": 1,
            },
            "complex_gain_magnitude_max": (4,1), "complex_gain_magnitude_min": (1,4),
            "constant_cfo_max_hz": 2000, "constant_cfo_min_hz": -2000,
            "demapper": "max-log-positive-means-zero-v1",
            "noise_estimator": "training-mean-27-v1",
            "noise_variance_floor": (1,1000000000000),
            "noise_variance_floor_f64_bits": 4427486594234968593,
            "sample_rate_offset_max_ppm": 10, "sample_rate_offset_min_ppm": -10,
            "timing_initial_max_samples_exclusive": (4,1),
            "timing_initial_min_samples": (0,1),
            "timing_recovery": {
                "identifier": "cubic-interpolation-reference-sro-grid-v2",
                "reference_symbols": 480,
                "sro_grid_max_ppm": 10,
                "sro_grid_min_ppm": -10,
                "sro_grid_step_ppm": 1,
                "sro_score": "normalized-coherent-reference-v1",
            },
        },
        "timestamp": {
            "burst_start_formula": "training-epoch-minus-32-times-rho-hat",
            "fractional_timing": "INCLUDED",
            "impairment_expected_burst_start_formula": "start-sample-plus-rho-times-tau-samples",
            "no_burst_anchor": "FIRST_SAMPLE_AT", "rounding": "HALF_AWAY_FROM_ZERO",
            "training_epoch": "FIRST-TRAINING-SYMBOL-CENTRE-AFTER-MF-GROUP-DELAY-REMOVAL",
            "timestamp_tolerance_ns": 13021,
        },
        "vectors": {
            "artifact_schema_version": 1,
            "awgn": {
                "awgn_burst_count": 1000, "awgn_domain_ascii": "VDETER-PHY11-AWGN-V1",
                "awgn_output_sample_count": 2304, "awgn_seed_u64": 6216169613920702721,
                "box_muller_pair_use": ("I","Q"), "counter_byte_order": "BIG_ENDIAN",
                "generator_id": "sha256-counter-box-muller-v1",
                "noise_only_domain_ascii": "VDETER-PHY11-NOISE-V1",
                "noise_only_sample_count": 2048,
                "noise_only_seed_u64": 6216169613920702722,
                "noise_only_slot_count": 10000, "opaque_packet_count": 256,
                "opaque_packet_domain_ascii": "VDETER-PHY11-PACKET-V1",
                "opaque_packet_seed_u64": 6216169613920702723,
                "packet_sequence": "REPEAT-CANONICAL-PACKET-1000",
                "project_software_gate_es_n0_db_ratio": (15,2),
                "project_software_gate_min_exact_packets": 990,
                "official_1db_run_role": "CHARACTERIZATION_ONLY",
                "stream_reset": "ONCE-PER-TEST-CASE",
                "uniform_id": "OPEN-INTERVAL-TOP52-PLUS-HALF-V1",
            },
            "impairment_channel_id": "phy11-impairment-channel-v1",
            "vector_schema_id": "phy11-vector-v1",
        },
    }


def phy11_profile_parameters() -> PhyProfileParameters:
    content = _phy11_content()
    tap_bytes = b"".join(struct.pack("<Q", value) for value in PHY11_RRC_TAP_BINARY64_BITS)
    if hashlib.sha256(tap_bytes).hexdigest() != PHY11_RRC_TAPS_SHA256:
        raise InvalidValueError("승인된 PHY11 RRC tap SHA-256이 일치하지 않습니다")
    return PhyProfileParameters(
        profile_schema_id=content["profile_schema_id"],
        **{name: _freeze_mapping(content[name]) for name in content if name != "profile_schema_id"},
    )


@dataclass(frozen=True, slots=True)
class LinkProfileEntry:
    """Link가 보는 profile. Packet 용량과 배치 한도는 단계 1에서 확정한다."""

    link_id: LinkId
    scope: RegistryScope
    support_status: SupportStatus
    source: SourceReference
    packet_capacity_bytes: int | None = None
    message_region_bytes: int | None = None
    crc_size_bytes: int | None = None
    padding_message_type: int | None = None
    max_messages_per_packet: int | None = None
    fragment_capacity_bytes: int | None = None
    message_payload_capacities: tuple[tuple[int, int], ...] = ()
    profile_version: str = "0.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("LinkProfileEntry.link_id: LinkId여야 합니다")
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("LinkProfileEntry.scope: RegistryScope여야 합니다")
        if not isinstance(self.support_status, SupportStatus):
            raise InvalidValueError("LinkProfileEntry.support_status: SupportStatus여야 합니다")
        if not isinstance(self.source, SourceReference):
            raise InvalidValueError("LinkProfileEntry.source: SourceReference여야 합니다")
        for name in (
            "packet_capacity_bytes",
            "message_region_bytes",
            "crc_size_bytes",
            "max_messages_per_packet",
            "fragment_capacity_bytes",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"LinkProfileEntry.{name}: 양의 정수여야 합니다")
        if self.padding_message_type is not None and (
            isinstance(self.padding_message_type, bool)
            or not isinstance(self.padding_message_type, int)
            or not 0 <= self.padding_message_type <= 255
        ):
            raise InvalidValueError("LinkProfileEntry.padding_message_type: uint8이어야 합니다")
        if not isinstance(self.message_payload_capacities, tuple):
            raise InvalidValueError("message_payload_capacities: tuple이어야 합니다")
        seen = set()
        for item in self.message_payload_capacities:
            if not isinstance(item, tuple) or len(item) != 2:
                raise InvalidValueError("message_payload_capacities item: (type, bytes)여야 합니다")
            message_type, capacity = item
            if (
                isinstance(message_type, bool)
                or not isinstance(message_type, int)
                or not 0 <= message_type <= 255
                or isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity <= 0
                or message_type in seen
            ):
                raise InvalidValueError("message_payload_capacities 값 또는 중복이 잘못되었습니다")
            seen.add(message_type)
        if self.packet_capacity_bytes is not None:
            if self.message_region_bytes != self.packet_capacity_bytes - 4:
                raise InvalidValueError("message_region_bytes는 Packet capacity - 4여야 합니다")
            if self.crc_size_bytes != 4 or self.padding_message_type != 81:
                raise InvalidValueError("운영 Link profile은 CRC 4 bytes와 Type 81 padding을 사용합니다")
        if not isinstance(self.profile_version, str) or not self.profile_version:
            raise InvalidValueError("LinkProfileEntry.profile_version이 비어 있습니다")

    def registry_key(self) -> int:
        return self.link_id.value


@dataclass(frozen=True, slots=True)
class PhyProfileEntry:
    """PHY가 보는 profile. FEC·modulation·burst parameter는 단계 2·6에서 확정한다."""

    link_id: LinkId
    scope: RegistryScope
    support_status: SupportStatus
    source: SourceReference
    symbol_rate_hz: int | None = None
    sample_rate_hz: int | None = None
    modulation: str = ""
    fec: str = ""
    profile_version: str = "0.0.0"
    parameters: PhyProfileParameters | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.link_id, LinkId):
            raise InvalidValueError("PhyProfileEntry.link_id: LinkId여야 합니다")
        if not isinstance(self.scope, RegistryScope):
            raise InvalidValueError("PhyProfileEntry.scope: RegistryScope여야 합니다")
        if not isinstance(self.support_status, SupportStatus):
            raise InvalidValueError("PhyProfileEntry.support_status: SupportStatus여야 합니다")
        if not isinstance(self.source, SourceReference):
            raise InvalidValueError("PhyProfileEntry.source: SourceReference여야 합니다")
        for name in ("symbol_rate_hz", "sample_rate_hz"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(f"PhyProfileEntry.{name}: 양의 정수여야 합니다")
        for name in ("modulation", "fec", "profile_version"):
            if not isinstance(getattr(self, name), str):
                raise InvalidValueError(f"PhyProfileEntry.{name}: 문자열이어야 합니다")
        if self.parameters is not None and not isinstance(
            self.parameters, PhyProfileParameters
        ):
            raise InvalidValueError(
                "PhyProfileEntry.parameters: PhyProfileParameters여야 합니다"
            )
        if self.support_status is SupportStatus.SUPPORTED:
            if self.parameters is None:
                raise InvalidValueError("지원 PHY profile에는 완결된 parameters가 필요합니다")
            counts = self.parameters.section("counts")
            rates = self.parameters.section("rates")
            identity = self.parameters.section("identity")
            if (
                identity["link_id"] != self.link_id.value
                or rates["symbol_rate_hz"] != self.symbol_rate_hz
                or rates["sample_rate_hz"] != self.sample_rate_hz
                or counts["information_bits"] != counts["packet_bytes"] * 8
                or counts["channel_bits"]
                != counts["data_symbols"] * rates["bits_per_data_symbol"]
                or counts["body_symbols"]
                != counts["training_symbols"]
                + counts["link_id_symbols"]
                + counts["data_symbols"]
                or counts["slot_samples"]
                != counts["slot_symbols"] * rates["samples_per_symbol"]
                or counts["slot_samples"]
                != counts["active_samples"] + counts["guard_samples"]
            ):
                raise InvalidValueError("PhyProfileEntry와 완결 parameters가 일치하지 않습니다")

    def registry_key(self) -> int:
        return self.link_id.value

    def fingerprint_content(self) -> dict:
        if self.parameters is not None:
            return self.parameters.fingerprint_content()
        return {
            "link_id": self.link_id.value,
            "scope": self.scope.value,
            "support_status": self.support_status.value,
            "source": {
                "document": self.source.document,
                "edition": self.source.edition,
                "location": self.source.location,
            },
            "symbol_rate_hz": self.symbol_rate_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "modulation": self.modulation,
            "fec": self.fec,
            "profile_version": self.profile_version,
            "parameters": None,
        }


_STAGE0 = SourceReference(
    document="stage0_baseline.md",
    edition="0.3.0",
    location="§7.2 Registry별 항목 (값은 단계 1·2·6에서 확정)",
)


def operational_link_entries() -> tuple:
    """Stage 1 승인 Link-visible profile 11, 17, 19."""

    source = SourceReference(
        document="ITU-R M.2092-2",
        edition="02/2026",
        location="Annex 4 Tables 52, 64, 67 and §A4-4.9",
    )
    values = (
        (11, 54, 50, 4, 35, ((74, 35), (75, 35), (76, 35), (92, 37), (93, 38))),
        (17, 234, 230, 19, 215, ((74, 215), (75, 215), (76, 215), (92, 217), (93, 218))),
        (19, 702, 698, 58, 683, ((74, 683), (75, 683), (76, 683), (92, 685), (93, 686))),
    )
    return tuple(
        LinkProfileEntry(
            link_id=LinkId(link_id),
            scope=RegistryScope.OPERATIONAL,
            support_status=SupportStatus.SUPPORTED,
            source=source,
            packet_capacity_bytes=packet_capacity,
            message_region_bytes=message_region,
            crc_size_bytes=4,
            padding_message_type=81,
            max_messages_per_packet=max_messages,
            fragment_capacity_bytes=fragment_capacity,
            message_payload_capacities=capacities,
            profile_version="stage1-link-0.5.3",
        )
        for link_id, packet_capacity, message_region, max_messages, fragment_capacity, capacities in values
    )


def test_link_entries() -> tuple:
    return (
        LinkProfileEntry(
            link_id=LinkId(TEST_LINK_ID),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.PLANNED,
            source=_STAGE0,
            packet_capacity_bytes=64,
            message_region_bytes=60,
            crc_size_bytes=4,
            padding_message_type=81,
            max_messages_per_packet=1,
            fragment_capacity_bytes=32,
            profile_version="stage0-test",
        ),
    )


def operational_phy_entries() -> tuple:
    """Stage 2 operational catalogue: exactly the approved Link ID 11 profile."""

    return (
        PhyProfileEntry(
            link_id=LinkId(11),
            scope=RegistryScope.OPERATIONAL,
            support_status=SupportStatus.SUPPORTED,
            source=SourceReference(
                document="ITU-R M.2092-2",
                edition="02/2026",
                location="Annex 2 §A2-1.2; Annex 4 §A4-3, §A4-4",
            ),
            symbol_rate_hz=19200,
            sample_rate_hz=76800,
            modulation="pi/4-QPSK",
            fec="turbo-1/2-puncture-6-tail-6a",
            profile_version="0.4.6",
            parameters=phy11_profile_parameters(),
        ),
    )


def test_phy_entries() -> tuple:
    return (
        PhyProfileEntry(
            link_id=LinkId(TEST_LINK_ID),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.PLANNED,
            source=_STAGE0,
            symbol_rate_hz=1000,
            sample_rate_hz=4000,
            modulation="",
            fec="",
            profile_version="stage0-test",
        ),
    )


def build_link_profile_registry(scope: RegistryScope, content_version: str = "stage0"):
    """Link Profile registry를 만들고 명시적으로 등록한다. freeze는 호출자가 한다."""

    registry = ExplicitRegistry("link_profile", scope, content_version, LinkProfileEntry)
    entries = (
        operational_link_entries()
        if scope is RegistryScope.OPERATIONAL
        else test_link_entries()
    )
    registered = registry.register_all(entries)
    if registered.is_err:
        return registered
    return Ok(registry)


def build_phy_profile_registry(
    scope: RegistryScope, content_version: str | None = None
):
    """PHY Profile registry를 만들고 명시적으로 등록한다. freeze는 호출자가 한다."""

    if content_version is None:
        content_version = (
            PHY11_REGISTRY_CONTENT_VERSION
            if scope is RegistryScope.OPERATIONAL
            else "stage0"
        )
    registry = ExplicitRegistry("phy_profile", scope, content_version, PhyProfileEntry)
    entries = (
        operational_phy_entries() if scope is RegistryScope.OPERATIONAL else test_phy_entries()
    )
    registered = registry.register_all(entries)
    if registered.is_err:
        return registered
    return Ok(registry)


def build_message_profile_registry(
    scope: RegistryScope, content_version: str = "stage0"
):
    """Message 표준 metadata registry를 만든다. codec registry는 Link가 소유한다."""

    from .messages import MessageProfileEntry, entries_for

    registry = ExplicitRegistry("message_profile", scope, content_version, MessageProfileEntry)
    registered = registry.register_all(entries_for(scope))
    if registered.is_err:
        return registered
    return Ok(registry)


# --- Stage 6 Link 17·19 combined registry ---------------------------------
# 아래 두 content는 각 Link 기준선 §10의 `fingerprint_content()` 반환 표현이며
# 실행 경로 값은 factory의 교차 검증으로 frozen runtime 모듈과 결속한다.


def _phy17_content() -> dict:
    return {
        "profile_schema_id": "stage6-phy-profile-v4",
        "identity": {
            "baseline_id": "VDETER-STAGE6-PHY17-BL-001",
            "baseline_version": "0.7.2",
            "link_id": 17,
            "pl_format": "TER-MCS-1.100",
            "profile_id": "phy17-markdown-v4",
            "scope": "OPERATIONAL",
            "source_document": "ITU-R M.2092-2",
            "source_edition": "02/2026",
            "source_location": "Annex 2 §A2-1.2; Annex 4 §A4-3, §A4-4",
            "support_status": "SUPPORTED",
        },
        "counts": {
            "active_samples": 7936,
            "active_symbols": 1984,
            "body_samples": 7680,
            "body_symbols": 1920,
            "channel_bits": 3754,
            "data_channel_bits": 3744,
            "data_symbols": 1877,
            "guard_samples": 256,
            "guard_symbols": 64,
            "information_bits": 1872,
            "link_id_symbols": 16,
            "packet_bytes": 234,
            "phy_padding_bits": 0,
            "ramp_down_samples": 128,
            "ramp_down_symbols": 32,
            "ramp_up_samples": 128,
            "ramp_up_symbols": 32,
            "slot_samples": 8192,
            "slot_symbols": 2048,
            "tail_channel_bits": 10,
            "training_symbols": 27,
        },
        "rates": {
            "bits_per_data_symbol": 2,
            "channel_bandwidth_hz": 100000,
            "sample_rate_hz": 307200,
            "samples_per_symbol": 4,
            "signal_bandwidth_hz": 99800,
            "symbol_rate_hz": 76800,
        },
        "fec": {
            "data_puncture_id": 6,
            "data_puncture_rows": [[1, 1, 0, 0, 0, 0], [1, 0, 0, 0, 1, 0]],
            "decoder_id": "log-map-f64-8iter-v1",
            "decoder_iterations": 8,
            "hard_tie": "llr-greater-than-or-equal-zero-is-bit-0",
            "interleaver": {
                "formula_id": "m2092-general-permutation-v1",
                "k": 1872,
                "k1": 6,
                "k2": 312,
                "primes": [211, 61, 227, 239, 181, 79, 73, 193],
            },
            "rsc_denominator_exponents": [0, 2, 3],
            "rsc_numerator_0_exponents": [0, 1, 3],
            "rsc_numerator_1_exponents": [0, 1, 2, 3],
            "serialization_order": ["X", "Y0", "Y1", "X'", "Y'0", "Y'1"],
            "siso_steps": 1875,
            "termination_tail_inputs": ["000", "100", "110", "010", "011", "111", "101", "001"],
            "tail_puncture_id": "6a",
            "tail_puncture_rows": [
                [1, 1, 0, 0, 0, 0],
                [1, 1, 0, 0, 0, 0],
                [1, 0, 0, 0, 0, 0],
                [0, 0, 0, 1, 1, 0],
                [0, 0, 0, 1, 1, 0],
                [0, 0, 0, 1, 0, 0],
            ],
        },
        "scrambler": {
            "bit_order": "channel-left-first",
            "initial_state": [1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0],
            "packet_reset": True,
            "polynomial_exponents": [0, 14, 15],
            "scrambled_bits": 3754,
            "training_and_link_id_excluded": True,
        },
        "training": {
            "bits": "111111001101010000011001010",
            "first_phase": "green",
            "mapping": {
                "0": "00",
                "1": "11",
            },
            "modulation": "pi/4-QPSK",
            "symbols": 27,
        },
        "link_id_code": {
            "codeword": "10000111001101110010010011100101",
            "codeword_bits": 32,
            "detector_candidate_range": [0, 63],
            "exact_tie_rejected": True,
            "hard_distance_max": 7,
            "information_bits": 6,
            "scrambling_word": "11000010111000101000111001001111",
            "value": 17,
            "value_bits": "010001",
        },
        "modulation": {
            "bits_per_symbol": 2,
            "data_phase_alternation": True,
            "identifier": "pi/4-QPSK",
            "label_order": ["00", "01", "11", "10"],
            "normalization_binary64_bits": 4604544271217802189,
        },
        "waveform": {
            "guard_samples": 256,
            "phase_hold": True,
            "ramp_id": "rrc-continuous-avg-db-linear-v2",
            "ramp_samples": 128,
            "reference_power_ratio": [1, 4],
            "rrc_beta_ratio": [3, 10],
            "rrc_group_delay_samples": 20,
            "rrc_span_symbols": 10,
            "rrc_tap_binary64_bits": [
                4561528387110721419,
                13794938650351962643,
                13796332409199125494,
                13783121652466558305,
                4573509893763543727,
                4574870330844761916,
                13784846569653267972,
                13801928541859933995,
                13803802258031059430,
                13792764167538667628,
                4582145384294991963,
                4585992274673594801,
                4583814571479215940,
                13804521522459226049,
                13813571218424099223,
                13815059194420588553,
                13808937871933581956,
                4592688422846200875,
                4599214752718274761,
                4602232803638848879,
                4603048513493868935,
                4602232803638848879,
                4599214752718274761,
                4592688422846200875,
                13808937871933581956,
                13815059194420588553,
                13813571218424099223,
                13804521522459226049,
                4583814571479215940,
                4585992274673594801,
                4582145384294991963,
                13792764167538667628,
                13803802258031059430,
                13801928541859933995,
                13784846569653267972,
                4574870330844761916,
                4573509893763543727,
                13783121652466558305,
                13796332409199125494,
                13794938650351962643,
                4561528387110721419,
            ],
            "rrc_tap_binary64_bits_source": "phy11-profile-v2.waveform.rrc_tap_binary64_bits",
            "rrc_taps_sha256": "1349ba6bac54cdd4a5e8cc3a89c4e22571a74cf20cf2f92dcf6b50b4227f588d",
            "rrc_taps_unit_energy": True,
            "sample_format": "cf32le",
            "slot_samples": 8192,
            "tx_scale_application": "after-rrc-and-ramp-before-guard-and-cf32le",
            "tx_scale_binary64_bits": 4607182418800017408,
            "tx_scale_ratio": [1, 1],
            "unscaled_peak_bound": 0.8268066598557832,
            "scaled_peak_bound": 0.8268066598557832,
        },
        "receiver": {
            "arithmetic": "ieee754-binary64-index-order-no-fma-fast-math",
            "candidate_max": 8,
            "candidate_suppression_samples": 4,
            "cfo_acquisition_coarse_hz": [-2200, 2200, 50],
            "cfo_acquisition_fine_hz": [-50, 50, 5],
            "cfo_hz_range": [-2000, 2000],
            "detector_id": "two-stage-normalized-training-correlation-v2",
            "demapper_id": "max-log-positive-is-bit-0-v1",
            "error_precedence_id": "stage6-common-7.8-v1",
            "fec_aided_cfo_coarse_hz": [-400, 400, 25],
            "fec_aided_cfo_fine_hz": [-25, 25, 1],
            "fec_aided_reference_symbols": 1920,
            "fec_aided_sro_ppm": [-10, 10, 1],
            "final_order": ["Q-desc", "training-C-desc", "epoch-asc"],
            "gain_range": [0.25, 4.0],
            "initial_scan_first_sample": 20,
            "interpolation_id": "lagrange-cubic-four-point-zero-extension-v1",
            "link_id_hard_distance_max": 7,
            "matched_filter_extension": "zero",
            "noise_floor": 1e-12,
            "prefix_refinement_coarse_hz": [-400, 400, 10],
            "prefix_refinement_fine_hz": [-10, 10, 1],
            "refinement_passes": 1,
            "sro_ppm_range": [-10, 10],
            "stream_buffer_samples": 8232,
            "thresholds": [0.3, 0.525, 1.05],
            "tracking_data_alpha_beta": [[2, 25], [1, 1000]],
            "tracking_prefix_alpha_beta": [[1, 10], [3, 2000]],
            "timing_fraction_denominator": 16,
            "timing_fraction_steps": [-8, 8],
        },
        "timestamp": {
            "burst_formula_id": "stage6-common-exact-rational-v1",
            "reference": "first-training-symbol-centre-after-mf-group-delay-removal",
            "tolerance_ns": 3256,
        },
        "vectors": {
            "awgn_domain": "VDETER-PHY17-AWGN-V1",
            "awgn_es_tx_ratio": [1, 1],
            "awgn_packet_sequence": "REPEAT-CANONICAL-PACKET-1000",
            "awgn_scale_id": "mpfr256-db-to-binary64-rne-v1",
            "awgn_sweep_tenths_db": [-10, 50, 1],
            "bursts_per_point": 1000,
            "generator_imports_production": False,
            "impairment_cases": [
                ["IDEAL", 0, 0, 0, 0, 1],
                ["TIMING_MAX", "63/16", 0, 0, 0, 1],
                ["CFO_MIN", 0, 0, -2000, 0, 1],
                ["CFO_MAX", 0, 0, 2000, 0, 1],
                ["SRO_MIN", 0, 0, 0, -10, 1],
                ["SRO_MAX", 0, 0, 0, 10, 1],
                ["GAIN_MIN", 0, 0, 0, 0, "1/4"],
                ["GAIN_MAX", 0, 0, 0, 0, 4],
                ["PHASE_WRAP", 0, "-pi", 0, 0, 1],
                ["MIXED", "23/16", "3*pi/7", 1375, 6, "5/4"],
            ],
            "impairment_order": ["timing-sro", "gain-phase-cfo", "awgn"],
            "impairment_trig_id": "mpfr256-direct-angle-binary64-rne-v1",
            "noise_only_domain": "VDETER-PHY17-NOISE-V1",
            "noise_only_points_tenths_db": [-10, 10, 50],
            "official_es_n0_db": 1.0,
            "packet_rng_id": "sha256-counter-bytes-v2",
            "loopback_fixed_packets": 5,
            "loopback_random_domain": "VDETER-PHY17-LOOPBACK-PACKET-V2",
            "loopback_random_packets": 256,
            "impairment_random_packets_per_case": 64,
            "rng_id": "sha256-counter-box-muller-mpfr256-v2",
            "rng_seed_formula": "0x5644455445520000 + link_id*0x100 + sequence",
            "sample_format": "cf32le",
            "sample_rounding": "binary64-add-then-binary32-rne-once",
        },
        "normative_source": {
            "binding_algorithm": "markdown-region-lf-normalized-sha256-v1",
            "common_baseline_id": "VDETER-STAGE6-PHY-COMMON-BL-001",
            "common_baseline_version": "0.5.2",
            "common_normative_sha256": "b6d0e76ba71b040a73f012b3f98b33337a4424f9777f95a497cf40183f6b3927",
            "link_normative_sha256": "ec61c00822a81606a8d7ed68a2080eedd3f12dd28a3576acf0e47afffed687aa",
        },
    }


def _phy19_content() -> dict:
    return {
        "profile_schema_id": "stage6-phy-profile-v4",
        "identity": {
            "baseline_id": "VDETER-STAGE6-PHY19-BL-001",
            "baseline_version": "0.7.2",
            "link_id": 19,
            "pl_format": "TER-MCS-5.100",
            "profile_id": "phy19-markdown-v4",
            "scope": "OPERATIONAL",
            "source_document": "ITU-R M.2092-2",
            "source_edition": "02/2026",
            "source_location": "Annex 2 §A2-1.2; Annex 4 §A4-3, §A4-4",
            "support_status": "SUPPORTED",
        },
        "counts": {
            "active_samples": 7936,
            "active_symbols": 1984,
            "body_samples": 7680,
            "body_symbols": 1920,
            "channel_bits": 7508,
            "data_channel_bits": 7488,
            "data_symbols": 1877,
            "guard_samples": 256,
            "guard_symbols": 64,
            "information_bits": 5616,
            "link_id_symbols": 16,
            "packet_bytes": 702,
            "phy_padding_bits": 8,
            "ramp_down_samples": 128,
            "ramp_down_symbols": 32,
            "ramp_up_samples": 128,
            "ramp_up_symbols": 32,
            "slot_samples": 8192,
            "slot_symbols": 2048,
            "tail_channel_bits": 12,
            "training_symbols": 27,
        },
        "rates": {
            "bits_per_data_symbol": 4,
            "channel_bandwidth_hz": 100000,
            "sample_rate_hz": 307200,
            "samples_per_symbol": 4,
            "signal_bandwidth_hz": 99800,
            "symbol_rate_hz": 76800,
        },
        "fec": {
            "data_puncture_id": 8,
            "data_puncture_rows": [
                [1, 0, 1, 0, 0, 0],
                [1, 0, 0, 0, 0, 0],
                [1, 0, 0, 0, 0, 0],
                [1, 0, 0, 0, 0, 0],
                [1, 0, 0, 0, 0, 0],
                [1, 0, 0, 0, 0, 1],
            ],
            "decoder_id": "log-map-f64-8iter-v1",
            "decoder_iterations": 8,
            "hard_tie": "llr-greater-than-or-equal-zero-is-bit-0",
            "interleaver": {
                "formula_id": "m2092-general-permutation-v1",
                "k": 5616,
                "k1": 16,
                "k2": 351,
                "primes": [137, 101, 223, 41, 67, 131, 61, 47],
            },
            "padding_order": "punctured-data-zero-pad-tail",
            "rsc_denominator_exponents": [0, 2, 3],
            "rsc_numerator_0_exponents": [0, 1, 3],
            "rsc_numerator_1_exponents": [0, 1, 2, 3],
            "serialization_order": ["X", "Y0", "Y1", "X'", "Y'0", "Y'1"],
            "siso_steps": 5619,
            "termination_tail_inputs": ["000", "100", "110", "010", "011", "111", "101", "001"],
            "tail_puncture_id": 8,
            "tail_puncture_rows": [
                [1, 0, 1, 0, 0, 0],
                [1, 0, 1, 0, 0, 0],
                [1, 0, 1, 0, 0, 0],
                [0, 0, 0, 1, 0, 1],
                [0, 0, 0, 1, 0, 1],
                [0, 0, 0, 1, 0, 1],
            ],
        },
        "scrambler": {
            "bit_order": "channel-left-first",
            "initial_state": [1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0],
            "packet_reset": True,
            "polynomial_exponents": [0, 14, 15],
            "scrambled_bits": 7508,
            "training_and_link_id_excluded": True,
        },
        "training": {
            "bits": "111111001101010000011001010",
            "first_phase": "green",
            "mapping": {
                "0": "00",
                "1": "11",
            },
            "modulation": "pi/4-QPSK",
            "symbols": 27,
        },
        "link_id_code": {
            "codeword": "10001111010010000010010000011010",
            "codeword_bits": 32,
            "detector_candidate_range": [0, 63],
            "exact_tie_rejected": True,
            "hard_distance_max": 7,
            "information_bits": 6,
            "scrambling_word": "11000010111000101000111001001111",
            "value": 19,
            "value_bits": "010011",
        },
        "modulation": {
            "bits_per_symbol": 4,
            "data_phase_alternation": False,
            "identifier": "16-QAM-Gray",
            "level_order": [-3, -1, 1, 3],
            "normalization_binary64_bits": 4599368272914696463,
            "slicer_tie": "label-ascii-lexicographic",
        },
        "waveform": {
            "guard_samples": 256,
            "phase_hold": True,
            "ramp_id": "rrc-continuous-avg-db-linear-v2",
            "ramp_samples": 128,
            "reference_power_ratio": [1, 4],
            "rrc_beta_ratio": [3, 10],
            "rrc_group_delay_samples": 20,
            "rrc_span_symbols": 10,
            "rrc_tap_binary64_bits": [
                4561528387110721419,
                13794938650351962643,
                13796332409199125494,
                13783121652466558305,
                4573509893763543727,
                4574870330844761916,
                13784846569653267972,
                13801928541859933995,
                13803802258031059430,
                13792764167538667628,
                4582145384294991963,
                4585992274673594801,
                4583814571479215940,
                13804521522459226049,
                13813571218424099223,
                13815059194420588553,
                13808937871933581956,
                4592688422846200875,
                4599214752718274761,
                4602232803638848879,
                4603048513493868935,
                4602232803638848879,
                4599214752718274761,
                4592688422846200875,
                13808937871933581956,
                13815059194420588553,
                13813571218424099223,
                13804521522459226049,
                4583814571479215940,
                4585992274673594801,
                4582145384294991963,
                13792764167538667628,
                13803802258031059430,
                13801928541859933995,
                13784846569653267972,
                4574870330844761916,
                4573509893763543727,
                13783121652466558305,
                13796332409199125494,
                13794938650351962643,
                4561528387110721419,
            ],
            "rrc_tap_binary64_bits_source": "phy11-profile-v2.waveform.rrc_tap_binary64_bits",
            "rrc_taps_sha256": "1349ba6bac54cdd4a5e8cc3a89c4e22571a74cf20cf2f92dcf6b50b4227f588d",
            "rrc_taps_unit_energy": True,
            "sample_format": "cf32le",
            "slot_samples": 8192,
            "tx_scale_application": "after-rrc-and-ramp-before-guard-and-cf32le",
            "tx_scale_binary64_bits": 4606056518893174784,
            "tx_scale_ratio": [7, 8],
            "unscaled_peak_bound": 1.1092775374122466,
            "scaled_peak_bound": 0.9706178452357157,
            "scaled_power_bound": 0.9420990014900238,
            "cf32_power_bound_exclusive": 0.9421,
        },
        "receiver": {
            "arithmetic": "ieee754-binary64-index-order-no-fma-fast-math",
            "candidate_max": 8,
            "candidate_suppression_samples": 4,
            "cfo_acquisition_coarse_hz": [-2200, 2200, 50],
            "cfo_acquisition_fine_hz": [-50, 50, 5],
            "cfo_hz_range": [-2000, 2000],
            "data_tracking_id": "normalized-16qam-decision-directed-v1",
            "detector_id": "two-stage-normalized-training-correlation-v2",
            "demapper_id": "max-log-positive-is-bit-0-v1",
            "error_precedence_id": "stage6-common-7.8-v1",
            "fec_aided_cfo_coarse_hz": [-400, 400, 25],
            "fec_aided_cfo_fine_hz": [-25, 25, 1],
            "fec_aided_reference_symbols": 1920,
            "fec_aided_sro_ppm": [-10, 10, 1],
            "final_order": ["Q-desc", "training-C-desc", "epoch-asc"],
            "gain_range": [0.25, 4.0],
            "initial_scan_first_sample": 20,
            "interpolation_id": "lagrange-cubic-four-point-zero-extension-v1",
            "link_id_hard_distance_max": 7,
            "matched_filter_extension": "zero",
            "noise_floor": 1e-12,
            "prefix_refinement_coarse_hz": [-400, 400, 10],
            "prefix_refinement_fine_hz": [-10, 10, 1],
            "refinement_passes": 1,
            "sro_ppm_range": [-10, 10],
            "stream_buffer_samples": 8232,
            "thresholds": [0.3, 0.525, 1.05],
            "tracking_data_alpha_beta": [[2, 25], [1, 1000]],
            "tracking_prefix_alpha_beta": [[1, 10], [3, 2000]],
            "timing_fraction_denominator": 16,
            "timing_fraction_steps": [-8, 8],
        },
        "timestamp": {
            "burst_formula_id": "stage6-common-exact-rational-v1",
            "reference": "first-training-symbol-centre-after-mf-group-delay-removal",
            "tolerance_ns": 3256,
        },
        "vectors": {
            "awgn_domain": "VDETER-PHY19-AWGN-V1",
            "awgn_es_tx_ratio": [49, 64],
            "awgn_packet_sequence": "REPEAT-CANONICAL-PACKET-1000",
            "awgn_scale_id": "mpfr256-db-to-binary64-rne-v1",
            "awgn_sweep_tenths_db": [82, 142, 1],
            "bursts_per_point": 1000,
            "generator_imports_production": False,
            "impairment_cases": [
                ["IDEAL", 0, 0, 0, 0, 1],
                ["TIMING_MAX", "63/16", 0, 0, 0, 1],
                ["CFO_MIN", 0, 0, -2000, 0, 1],
                ["CFO_MAX", 0, 0, 2000, 0, 1],
                ["SRO_MIN", 0, 0, 0, -10, 1],
                ["SRO_MAX", 0, 0, 0, 10, 1],
                ["GAIN_MIN", 0, 0, 0, 0, "1/4"],
                ["GAIN_MAX", 0, 0, 0, 0, 4],
                ["PHASE_WRAP", 0, "-pi", 0, 0, 1],
                ["MIXED", "23/16", "3*pi/7", 1375, 6, "5/4"],
            ],
            "impairment_order": ["timing-sro", "gain-phase-cfo", "awgn"],
            "impairment_trig_id": "mpfr256-direct-angle-binary64-rne-v1",
            "noise_only_domain": "VDETER-PHY19-NOISE-V1",
            "noise_only_points_tenths_db": [82, 102, 142],
            "official_es_n0_db": 10.2,
            "packet_rng_id": "sha256-counter-bytes-v2",
            "loopback_fixed_packets": 5,
            "loopback_random_domain": "VDETER-PHY19-LOOPBACK-PACKET-V2",
            "loopback_random_packets": 256,
            "impairment_random_packets_per_case": 64,
            "rng_id": "sha256-counter-box-muller-mpfr256-v2",
            "rng_seed_formula": "0x5644455445520000 + link_id*0x100 + sequence",
            "sample_format": "cf32le",
            "sample_rounding": "binary64-add-then-binary32-rne-once",
        },
        "normative_source": {
            "binding_algorithm": "markdown-region-lf-normalized-sha256-v1",
            "common_baseline_id": "VDETER-STAGE6-PHY-COMMON-BL-001",
            "common_baseline_version": "0.5.2",
            "common_normative_sha256": "b6d0e76ba71b040a73f012b3f98b33337a4424f9777f95a497cf40183f6b3927",
            "link_normative_sha256": "7fc05a010651e07364001634a73e7efa04fe571cb981aeb463c851285f26ea39",
        },
    }


def _stage6_profile_parameters(content: dict) -> PhyProfileParameters:
    from ..phy import wideband as _wideband
    from ..phy.burst.link_id import CODEWORDS as _codewords

    link_id = content["identity"]["link_id"]
    profile = _wideband.PROFILES[link_id]
    fec = content["fec"]
    if (
        fec["interleaver"]["k"] != profile.information_bits
        or fec["interleaver"]["k1"] != profile.interleaver_k1
        or fec["interleaver"]["k2"] != profile.interleaver_k2
        or tuple(fec["interleaver"]["primes"]) != profile.interleaver_primes
        or tuple(tuple(row) for row in fec["data_puncture_rows"]) != profile.data_patterns
        or content["counts"]["packet_bytes"] != profile.packet_bytes
        or content["counts"]["information_bits"] != profile.information_bits
        or content["counts"]["channel_bits"] != profile.channel_bits
        or content["scrambler"]["scrambled_bits"] != profile.channel_bits
    ):
        raise InvalidValueError("stage6 profile content가 frozen wideband 모듈과 다릅니다")
    codeword = "".join(str(bit) for bit in _codewords[link_id])
    if content["link_id_code"]["codeword"] != codeword:
        raise InvalidValueError("stage6 Link ID codeword가 frozen 생성기와 다릅니다")
    if tuple(content["waveform"]["rrc_tap_binary64_bits"]) != PHY11_RRC_TAP_BINARY64_BITS:
        raise InvalidValueError("stage6 RRC tap literal이 Stage 2 동결값과 다릅니다")
    if content["waveform"]["rrc_taps_sha256"] != PHY11_RRC_TAPS_SHA256:
        raise InvalidValueError("stage6 RRC tap SHA-256이 Stage 2 동결값과 다릅니다")
    return PhyProfileParameters(
        profile_schema_id=content["profile_schema_id"],
        **{
            name: _freeze_mapping(content[name])
            for name in content
            if name != "profile_schema_id"
        },
    )


def phy17_profile_parameters() -> PhyProfileParameters:
    return _stage6_profile_parameters(_phy17_content())


def phy19_profile_parameters() -> PhyProfileParameters:
    return _stage6_profile_parameters(_phy19_content())


def _stage6_phy_entry(parameters: PhyProfileParameters) -> PhyProfileEntry:
    identity = parameters.section("identity")
    rates = parameters.section("rates")
    modulation = parameters.section("modulation")
    return PhyProfileEntry(
        link_id=LinkId(identity["link_id"]),
        scope=RegistryScope.OPERATIONAL,
        support_status=SupportStatus.SUPPORTED,
        source=SourceReference(
            document=identity["source_document"],
            edition=identity["source_edition"],
            location=identity["source_location"],
        ),
        symbol_rate_hz=rates["symbol_rate_hz"],
        sample_rate_hz=rates["sample_rate_hz"],
        modulation=modulation["identifier"],
        fec="turbo-puncture-{0}-tail-{1}".format(
            parameters.section("fec")["data_puncture_id"],
            parameters.section("fec")["tail_puncture_id"],
        ),
        profile_version=identity["baseline_version"],
        parameters=parameters,
    )


def combined_phy_profile_entries() -> tuple:
    """Stage 6 combined catalogue: frozen Link 11 entry + Link 17·19 entries."""

    return operational_phy_entries() + (
        _stage6_phy_entry(phy17_profile_parameters()),
        _stage6_phy_entry(phy19_profile_parameters()),
    )


def build_combined_phy_profile_registry():
    """세 entry combined `phy_profile` registry. content version은 내부 고정이다."""

    registry = ExplicitRegistry(
        "phy_profile",
        RegistryScope.OPERATIONAL,
        STAGE6_REGISTRY_CONTENT_VERSION,
        PhyProfileEntry,
    )
    registered = registry.register_all(combined_phy_profile_entries())
    if registered.is_err:
        return registered
    return Ok(registry)
