"""Link ID 17·19 100 kHz wideband PHY specialization.

`stage6_phy_link17_baseline.md`/`stage6_phy_link19_baseline.md`의 FEC·mapping·burst 규범과
공통 기준선 §7의 Stage-2 receiver 공통화를 하나의 모듈로 구현한다. Link 11 동결 모듈은
호출만 하고 수정하지 않는다.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass

from .burst.framing import bits_to_bytes, bytes_to_bits
from .burst.link_id import CODEWORDS, LinkIdDetectionError, detect
from .burst.rrc import RRC_HALF_SPAN_SAMPLES, RRC_TAPS, SAMPLES_PER_SYMBOL
from .burst.synchronization import (
    NoBurstDetected,
    SynchronizationFailed,
    TRAINING_SYMBOLS,
    acquire_candidates,
    equalize_body,
)
from .fec.interleaver import general_permutation_zero_based
from .fec.puncturing import puncture_general, rate_recover_general
from .fec.turbo import decode_rows, encode_with_permutation
from .modulation import pi4_qpsk, qam16
from .receiver import (
    FecDecodeFailed,
    LinkIdNotAllowed,
    ReceiverCoreResult,
    UnsupportedDetectedLinkId,
)
from .scrambling import pn_sequence

SYMBOL_RATE_HZ = 76_800
SAMPLE_RATE_HZ = 307_200
BODY_SYMBOLS = 1_920
DATA_SYMBOLS = 1_877
DATA_FIRST_BODY_INDEX = 43
RAMP_SAMPLES = 128
BODY_SAMPLES = BODY_SYMBOLS * SAMPLES_PER_SYMBOL
GUARD_SAMPLES = 256
SLOT_SAMPLES = 8_192

LINK17_DATA_PATTERNS = (
    (1, 1, 0, 0, 0, 0),
    (1, 0, 0, 0, 1, 0),
)
LINK17_TAIL_PATTERNS = (
    (1, 1, 0, 0, 0, 0),
    (1, 1, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 0, 0),
)
LINK19_DATA_PATTERNS = (
    (1, 0, 1, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 1),
)
LINK19_TAIL_PATTERNS = (
    (1, 0, 1, 0, 0, 0),
    (1, 0, 1, 0, 0, 0),
    (1, 0, 1, 0, 0, 0),
    (0, 0, 0, 1, 0, 1),
    (0, 0, 0, 1, 0, 1),
    (0, 0, 0, 1, 0, 1),
)
LINK19_ZERO_PAD_BITS = 8


@dataclass(frozen=True, slots=True)
class WidebandLinkProfile:
    link_id: int
    packet_bytes: int
    information_bits: int
    interleaver_k1: int
    interleaver_k2: int
    interleaver_primes: tuple[int, ...]
    data_patterns: tuple[tuple[int, ...], ...]
    tail_patterns: tuple[tuple[int, ...], ...]
    channel_bits: int
    zero_pad_bits: int
    tx_scale: float


LINK17_PROFILE = WidebandLinkProfile(
    link_id=17,
    packet_bytes=234,
    information_bits=1_872,
    interleaver_k1=6,
    interleaver_k2=312,
    interleaver_primes=(211, 61, 227, 239, 181, 79, 73, 193),
    data_patterns=LINK17_DATA_PATTERNS,
    tail_patterns=LINK17_TAIL_PATTERNS,
    channel_bits=3_754,
    zero_pad_bits=0,
    tx_scale=1.0,
)
LINK19_PROFILE = WidebandLinkProfile(
    link_id=19,
    packet_bytes=702,
    information_bits=5_616,
    interleaver_k1=16,
    interleaver_k2=351,
    interleaver_primes=(137, 101, 223, 41, 67, 131, 61, 47),
    data_patterns=LINK19_DATA_PATTERNS,
    tail_patterns=LINK19_TAIL_PATTERNS,
    channel_bits=7_508,
    zero_pad_bits=LINK19_ZERO_PAD_BITS,
    tx_scale=7.0 / 8.0,
)
PROFILES = {17: LINK17_PROFILE, 19: LINK19_PROFILE}

_PERMUTATIONS = {
    profile.link_id: general_permutation_zero_based(
        profile.information_bits,
        profile.interleaver_k1,
        profile.interleaver_k2,
        profile.interleaver_primes,
    )
    for profile in PROFILES.values()
}
_PREFIX_SYMBOLS = {
    link_id: TRAINING_SYMBOLS + pi4_qpsk.map_bits(CODEWORDS[link_id], 27)
    for link_id in PROFILES
}


def permutation_for(link_id: int) -> tuple[int, ...]:
    return _PERMUTATIONS[link_id]


def prefix_symbols(link_id: int) -> tuple[complex, ...]:
    return _PREFIX_SYMBOLS[link_id]


def _serial_channel_bits(profile: WidebandLinkProfile, encoded) -> tuple[int, ...]:
    data_bits = puncture_general(encoded.data_rows, (), profile.data_patterns, ())
    tail_bits = puncture_general((), encoded.tail_rows, profile.data_patterns, profile.tail_patterns)
    serial = data_bits + (0,) * profile.zero_pad_bits + tail_bits
    if len(serial) != profile.channel_bits:
        raise AssertionError("wideband channel bit count invariant failed")
    return serial


def _scramble_bits(bits) -> tuple[int, ...]:
    pn = pn_sequence(len(bits))
    return tuple(bit ^ mask for bit, mask in zip(bits, pn))


def _descramble_llrs(llrs) -> tuple[float, ...]:
    pn = pn_sequence(len(llrs))
    return tuple(-value if mask else value for value, mask in zip(llrs, pn))


def _map_data_symbols(profile: WidebandLinkProfile, scrambled_bits) -> tuple[complex, ...]:
    if profile.link_id == 17:
        symbols = pi4_qpsk.map_bits(scrambled_bits, DATA_FIRST_BODY_INDEX)
    else:
        symbols = qam16.map_bits(scrambled_bits)
    if len(symbols) != DATA_SYMBOLS:
        raise AssertionError("wideband data symbol count invariant failed")
    return symbols


def _held(symbols: tuple[complex, ...], index: int) -> complex:
    if index < 0:
        return symbols[0]
    if index >= len(symbols):
        return symbols[-1]
    return symbols[index]


def _pulse_shaped_carrier(body_symbols: tuple[complex, ...]) -> tuple[complex, ...]:
    if len(body_symbols) != BODY_SYMBOLS:
        raise ValueError("wideband body requires exactly 1,920 symbols")
    first_sample = -RAMP_SAMPLES
    last_sample = BODY_SAMPLES + RAMP_SAMPLES - 1
    output = []
    for n in range(first_sample, last_sample + 1):
        minimum_symbol = math.ceil((n - RRC_HALF_SPAN_SAMPLES) / SAMPLES_PER_SYMBOL)
        maximum_symbol = math.floor((n + RRC_HALF_SPAN_SAMPLES) / SAMPLES_PER_SYMBOL)
        value = 0.0 + 0.0j
        for symbol_index in range(minimum_symbol, maximum_symbol + 1):
            tap_index = n - SAMPLES_PER_SYMBOL * symbol_index
            value += _held(body_symbols, symbol_index) * RRC_TAPS[
                tap_index + RRC_HALF_SPAN_SAMPLES
            ]
        output.append(value)
    return tuple(output)


def make_slot(body_symbols, tx_scale: float) -> tuple[complex, ...]:
    carrier = _pulse_shaped_carrier(tuple(complex(value) for value in body_symbols))
    ramp_up_carrier = carrier[:RAMP_SAMPLES]
    body = carrier[RAMP_SAMPLES:RAMP_SAMPLES + BODY_SAMPLES]
    ramp_down_carrier = carrier[RAMP_SAMPLES + BODY_SAMPLES:]
    ramp_up = tuple(
        value * (10.0 ** ((-50.0 + 50.0 * index / RAMP_SAMPLES) / 20.0))
        for index, value in enumerate(ramp_up_carrier)
    )
    ramp_down = tuple(
        value * (10.0 ** ((-50.0 * index / RAMP_SAMPLES) / 20.0))
        for index, value in enumerate(ramp_down_carrier)
    )
    active = tuple(value * tx_scale for value in ramp_up + body + ramp_down)
    slot = active + (0.0 + 0.0j,) * GUARD_SAMPLES
    if len(slot) != SLOT_SAMPLES or any(
        not math.isfinite(value.real) or not math.isfinite(value.imag) for value in slot
    ):
        raise AssertionError("wideband slot waveform invariant failed")
    if any(abs(value.real) > 1.0 or abs(value.imag) > 1.0 for value in slot):
        raise AssertionError("wideband slot exceeds the Radio TX full-scale contract")
    return slot


@dataclass(frozen=True, slots=True)
class WidebandTxStages:
    information_bits: tuple[int, ...]
    channel_bits: tuple[int, ...]
    scrambled_bits: tuple[int, ...]
    body_symbols: tuple[complex, ...]
    samples: tuple[complex, ...]


def encode_slot(link_id: int, packet: bytes) -> WidebandTxStages:
    profile = PROFILES[link_id]
    if not isinstance(packet, bytes) or len(packet) != profile.packet_bytes:
        raise ValueError(
            f"Link ID {link_id} Packet must contain exactly {profile.packet_bytes} bytes"
        )
    information_bits = bytes_to_bits(packet)
    encoded = encode_with_permutation(information_bits, _PERMUTATIONS[link_id])
    serial = _serial_channel_bits(profile, encoded)
    scrambled = _scramble_bits(serial)
    data_symbols = _map_data_symbols(profile, scrambled)
    body = _PREFIX_SYMBOLS[link_id] + data_symbols
    samples = make_slot(body, profile.tx_scale)
    return WidebandTxStages(
        information_bits=information_bits,
        channel_bits=serial,
        scrambled_bits=scrambled,
        body_symbols=body,
        samples=samples,
    )


def _data_decision(link_id: int):
    if link_id == 17:
        return None  # 공통 pi/4 alternation 기본 slicer
    def decision(current: complex, index: int) -> complex:
        if index < DATA_FIRST_BODY_INDEX:
            label = pi4_qpsk.hard_label(current, index)
            return pi4_qpsk.map_labels((label,), index)[0]
        return qam16.hard_symbol(current)
    return decision


def _demap_data(link_id: int, symbols, noise_variance: float) -> tuple[float, ...]:
    if link_id == 17:
        return pi4_qpsk.soft_demap(symbols, DATA_FIRST_BODY_INDEX, noise_variance)
    return qam16.soft_demap(symbols, noise_variance)


def _decode_channel(profile: WidebandLinkProfile, descrambled) -> tuple[int, ...]:
    values = tuple(descrambled)
    if len(values) != profile.channel_bits:
        raise FecDecodeFailed("wideband channel LLR count invariant failed")
    if profile.zero_pad_bits:
        data = values[: profile.channel_bits - profile.zero_pad_bits - 12]
        padding = values[len(data): len(data) + profile.zero_pad_bits]
        tail = values[len(data) + profile.zero_pad_bits:]
        if any(value < 0.0 for value in padding):
            raise FecDecodeFailed("Link ID 19 zero padding hard decision failed")
        recover_input = data + tail
    else:
        recover_input = values
    data_rows, tail_rows = rate_recover_general(
        recover_input,
        profile.information_bits,
        profile.data_patterns,
        profile.tail_patterns,
    )
    return decode_rows(data_rows, tail_rows, _PERMUTATIONS[profile.link_id])


class WidebandReceiver:
    """Link 17·19 공통 수신기. §7의 Stage-2 receiver 공통화를 사용한다."""

    def decode(self, samples, allowed_link_ids: frozenset[int] | None = None) -> ReceiverCoreResult:
        candidates = acquire_candidates(
            samples, body_symbols=BODY_SYMBOLS, symbol_rate_hz=SYMBOL_RATE_HZ
        )
        decoded = []
        strongest_error = None
        for acquisition in candidates:
            try:
                body = equalize_body(
                    acquisition,
                    body_symbols=BODY_SYMBOLS,
                    symbol_rate_hz=SYMBOL_RATE_HZ,
                )
                link_llrs = pi4_qpsk.soft_demap(
                    body.symbols[27:DATA_FIRST_BODY_INDEX], 27, body.noise_variance
                )
                link_id = detect(link_llrs)
                if link_id not in PROFILES:
                    raise UnsupportedDetectedLinkId(link_id)
                if allowed_link_ids and link_id not in allowed_link_ids:
                    raise LinkIdNotAllowed(
                        f"detected Link ID {link_id} is not allowed"
                    )
                profile = PROFILES[link_id]
                decision = _data_decision(link_id)
                body = equalize_body(
                    acquisition,
                    _PREFIX_SYMBOLS[link_id],
                    body_symbols=BODY_SYMBOLS,
                    symbol_rate_hz=SYMBOL_RATE_HZ,
                    data_decision=decision,
                )
                confirmed = detect(
                    pi4_qpsk.soft_demap(
                        body.symbols[27:DATA_FIRST_BODY_INDEX], 27, body.noise_variance
                    )
                )
                if confirmed != link_id:
                    raise UnsupportedDetectedLinkId(confirmed)
                data_llrs = _descramble_llrs(
                    _demap_data(
                        link_id,
                        body.symbols[DATA_FIRST_BODY_INDEX:BODY_SYMBOLS],
                        body.noise_variance,
                    )
                )
                information_bits = _decode_channel(profile, data_llrs)
                packet = bits_to_bytes(information_bits)
                if len(packet) != profile.packet_bytes:
                    raise FecDecodeFailed("decoded Packet length invariant failed")
                # 1회 FEC-aided refinement: 재부호화한 전체 1,920-symbol reference로
                # CFO/SRO를 다듬고 두 번째 demap/decode 결과와 exact 일치해야 한다.
                encoded = encode_with_permutation(information_bits, _PERMUTATIONS[link_id])
                serial = _serial_channel_bits(profile, encoded)
                reference = _PREFIX_SYMBOLS[link_id] + _map_data_symbols(
                    profile, _scramble_bits(serial)
                )
                body = equalize_body(
                    acquisition,
                    reference,
                    body_symbols=BODY_SYMBOLS,
                    symbol_rate_hz=SYMBOL_RATE_HZ,
                    data_decision=decision,
                )
                data_llrs = _descramble_llrs(
                    _demap_data(
                        link_id,
                        body.symbols[DATA_FIRST_BODY_INDEX:BODY_SYMBOLS],
                        body.noise_variance,
                    )
                )
                second_bits = _decode_channel(profile, data_llrs)
                second_packet = bits_to_bytes(second_bits)
                if second_packet != packet:
                    raise FecDecodeFailed("refinement pass produced a different Packet")
                serial = _serial_channel_bits(
                    profile, encode_with_permutation(second_bits, _PERMUTATIONS[link_id])
                )
                denominator = sum(abs(value) for value in data_llrs)
                if not denominator or not math.isfinite(denominator):
                    raise FecDecodeFailed("FEC confirmation metric has zero or non-finite energy")
                score = sum(
                    value if bit == 0 else -value
                    for bit, value in zip(serial, data_llrs)
                ) / denominator
                if not math.isfinite(score):
                    raise FecDecodeFailed("FEC confirmation metric is non-finite")
                decoded.append(
                    (
                        score,
                        acquisition.correlation,
                        -acquisition.training_epoch_samples,
                        packet,
                        link_id,
                        acquisition,
                        body,
                    )
                )
            except (
                LinkIdDetectionError,
                UnsupportedDetectedLinkId,
                LinkIdNotAllowed,
                SynchronizationFailed,
            ) as error:
                if strongest_error is None:
                    strongest_error = error
            except (ArithmeticError, ValueError, AssertionError) as error:
                if strongest_error is None:
                    strongest_error = FecDecodeFailed(str(error))
        if not decoded:
            if strongest_error is not None:
                raise strongest_error
            raise NoBurstDetected
        score, _, _, packet, link_id, acquisition, body = max(
            decoded, key=lambda item: (item[0], item[1], item[2])
        )
        return ReceiverCoreResult(
            packet=packet,
            link_id=link_id,
            training_epoch_samples=acquisition.training_epoch_samples,
            rho_hat=body.rho_hat,
            correlation=acquisition.correlation,
            peak_ratio=acquisition.peak_ratio,
            cfo_hz=acquisition.cfo_hz,
            gain_magnitude=acquisition.gain_magnitude,
            noise_variance=body.noise_variance,
            fec_codeword_score=score,
        )
