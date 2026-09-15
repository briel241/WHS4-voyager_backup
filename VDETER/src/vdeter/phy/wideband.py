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
from .burst.rrc import (
    RRC_HALF_SPAN_SAMPLES,
    RRC_TAPS,
    SAMPLES_PER_SYMBOL,
    to_cf32le,
    validate_cf32le_full_scale,
)
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
    try:
        validate_cf32le_full_scale(to_cf32le(slot))
    except ValueError as error:
        raise AssertionError(
            "wideband slot exceeds the Radio TX full-scale contract"
        ) from error
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


def profile_content_mismatch(link_id: int, content: dict) -> str | None:
    """combined registry entry content와 frozen wideband runtime 값의 불일치 사유.

    일치하면 `None`을 반환한다. `TC-ARCH-002`에 따라 profiles 계층 대신 PHY 계층이
    이 결속을 소유하고 Runtime composition이 조립 시 호출한다.
    """

    profile = PROFILES.get(link_id)
    if profile is None:
        return "unsupported wideband link id"
    fec = content["fec"]
    if (
        fec["interleaver"]["k"] != profile.information_bits
        or fec["interleaver"]["k1"] != profile.interleaver_k1
        or fec["interleaver"]["k2"] != profile.interleaver_k2
        or tuple(fec["interleaver"]["primes"]) != profile.interleaver_primes
        or tuple(tuple(row) for row in fec["data_puncture_rows"]) != profile.data_patterns
        or tuple(tuple(row) for row in fec["tail_puncture_rows"]) != profile.tail_patterns
    ):
        return "FEC interleaver/puncture parameters differ from the frozen runtime"
    counts = content["counts"]
    if (
        counts["packet_bytes"] != profile.packet_bytes
        or counts["information_bits"] != profile.information_bits
        or counts["channel_bits"] != profile.channel_bits
        or content["scrambler"]["scrambled_bits"] != profile.channel_bits
    ):
        return "packet/channel bit counts differ from the frozen runtime"
    codeword = "".join(str(bit) for bit in CODEWORDS[link_id])
    if content["link_id_code"]["codeword"] != codeword:
        return "Link ID codeword differs from the frozen generator"
    return None


def select_burst_clusters(decoded, *, slot_samples: int) -> tuple:
    """§7.6: reconstructed slot interval overlap의 transitive closure로 cluster를 묶는다.

    각 성공 후보의 half-open interval은 `[e-128*rho, e-128*rho+slot*rho)`다. 겹치는
    cluster 안에서는 `(Q 내림차순, training C 내림차순, epoch 오름차순)`으로 하나를
    고르고, 세 값이 exact 동률인 cluster는 임의 결정 없이 탈락시킨다(호출자가 성공
    cluster가 하나도 없을 때 synchronization 실패로 구조화한다). 겹치지 않는 cluster는
    서로 다른 물리 burst이므로 각자의 승자를 burst-start epoch 오름차순으로 반환한다.
    """

    if not decoded:
        return ()
    items = []
    for result in decoded:
        start = result.training_epoch_samples - RAMP_SAMPLES * result.rho_hat
        end = start + slot_samples * result.rho_hat
        items.append((start, end, result))
    items.sort(key=lambda item: item[0])
    clusters = [[items[0]]]
    reach = items[0][1]
    for item in items[1:]:
        if item[0] < reach:
            clusters[-1].append(item)
            reach = max(reach, item[1])
        else:
            clusters.append([item])
            reach = item[1]
    winners = []
    for cluster in clusters:
        ranked = sorted(
            cluster,
            key=lambda item: (
                -item[2].fec_codeword_score,
                -item[2].correlation,
                item[2].training_epoch_samples,
            ),
        )
        if len(ranked) > 1:
            first, second = ranked[0][2], ranked[1][2]
            if (
                first.fec_codeword_score == second.fec_codeword_score
                and first.correlation == second.correlation
                and first.training_epoch_samples == second.training_epoch_samples
            ):
                # exact 동률: Packet bytes나 Link ID로 임의 결정하지 않는다.
                continue
        winners.append((ranked[0][0], ranked[0][2]))
    winners.sort(key=lambda item: item[0])
    return tuple(item[1] for item in winners)


class WidebandReceiver:
    """Link 17·19 공통 수신기. §7의 Stage-2 receiver 공통화를 사용한다."""

    def _decode_candidate(
        self,
        acquisition,
        allowed_link_ids: frozenset[int] | None,
    ) -> ReceiverCoreResult:
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
            raise LinkIdNotAllowed(f"detected Link ID {link_id} is not allowed")
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
            raise FecDecodeFailed(
                "FEC confirmation metric has zero or non-finite energy"
            )
        score = sum(
            value if bit == 0 else -value
            for bit, value in zip(serial, data_llrs)
        ) / denominator
        if not math.isfinite(score):
            raise FecDecodeFailed("FEC confirmation metric is non-finite")
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

    def decode_all(
        self,
        samples,
        allowed_link_ids: frozenset[int] | None = None,
    ) -> tuple[ReceiverCoreResult, ...]:
        """Return every successful candidate in timestamp order.

        Candidate-local failures are retained only to select the deterministic
        error when *no* candidate succeeds.  ``InternalResult`` cannot carry
        packets and an error together, therefore one or more successful
        candidates always win over failed candidates in the same input.
        """

        candidates = acquire_candidates(
            samples, body_symbols=BODY_SYMBOLS, symbol_rate_hz=SYMBOL_RATE_HZ
        )
        decoded: list[ReceiverCoreResult] = []
        strongest_error = None
        for acquisition in candidates:
            try:
                decoded.append(self._decode_candidate(acquisition, allowed_link_ids))
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
        winners = select_burst_clusters(decoded, slot_samples=SLOT_SAMPLES)
        if not winners:
            if strongest_error is not None:
                raise strongest_error
            if decoded:
                # 성공 후보가 모두 exact 동률 cluster로 실패했다 (§7.6).
                raise SynchronizationFailed(
                    "exact-tie candidate cluster cannot be resolved"
                )
            raise NoBurstDetected
        return winners

    def decode(
        self,
        samples,
        allowed_link_ids: frozenset[int] | None = None,
    ) -> ReceiverCoreResult:
        """Compatibility helper returning the strongest successful candidate."""

        decoded = self.decode_all(samples, allowed_link_ids)
        return max(
            decoded,
            key=lambda item: (
                item.fec_codeword_score,
                item.correlation,
                -item.training_epoch_samples,
            ),
        )
