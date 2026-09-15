"""Generate the independent Stage 2 Link ID 11 conformance vectors.

This program intentionally uses only the Python standard library.  It must not
import ``vdeter`` or any production PHY module; the approved baseline and the
official constants copied below are its inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path


BASELINE_VERSION = "0.4.6"
PROFILE_SCHEMA_ID = "phy11-profile-v2"
REGISTRY_CONTENT_VERSION = "stage2-phy11-v2"
REGISTRY_FINGERPRINT = "ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898"
CANONICAL_PACKET_HEX = (
    "00000c010203040001017f14"
    "5151515151515151515151515151515151515151515151515151515151515151515151515151"
    "f8dbb227"
)
TRAINING_BITS = "111111001101010000011001010"
LINK_ID_GENERATOR_ROWS = (
    "10000010111010011110100110010110",
    "01000001110101011101010101010101",
    "00100011101100110011001100110011",
    "00010011000011111000111100001111",
    "00001000011111110000000011111111",
    "00000100000000000111111111111111",
)
LINK_ID_SCRAMBLING_WORD = "11000010111000101000111001001111"
LINK_ID_11_CODEWORD = "11101101001011101100001001111100"
RRC_TAP_BITS = (
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
RRC_TAPS_SHA256 = "1349ba6bac54cdd4a5e8cc3a89c4e22571a74cf20cf2f92dcf6b50b4227f588d"
RRC_TAPS = tuple(struct.unpack("<d", struct.pack("<Q", bits))[0] for bits in RRC_TAP_BITS)
DATA_PATTERN_6 = ((1, 1, 0, 0, 0, 0), (1, 0, 0, 0, 1, 0))
TAIL_PATTERN_6A = (
    (1, 1, 0, 0, 0, 0),
    (1, 1, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 0, 0),
)
INITIAL_SCRAMBLER = (1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0)
SCALE = struct.unpack("<d", struct.pack("<Q", 0x3FE6A09E667F3BCD))[0]
GREEN = {
    "00": complex(-SCALE, -SCALE), "01": complex(-SCALE, SCALE),
    "11": complex(SCALE, SCALE), "10": complex(SCALE, -SCALE),
}
PURPLE = {"00": -1.0 + 0.0j, "01": 0.0 + 1.0j, "11": 1.0 + 0.0j, "10": 0.0 - 1.0j}
PI = struct.unpack("<d", struct.pack("<Q", 0x400921FB54442D18))[0]

# ITU-R M.2092-2 Annex 3 section A3-8, printed pages 65-67.  These literal
# strings are the official auxiliary Link Config ID 5 example.  They are not
# a Link ID 11 end-to-end oracle; the generator uses them to cross-check the
# common RSC, interleaver, puncturing, scrambler, Link-ID and mapper rules.
ASM_LINK5_PACKED_BITS = (
    "0101000111000000001110101101111001101000101010011101011011110011"
    "0100010110001000000000000000000011001000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
ASM_LINK5_TURBO_INPUT_BITS = ASM_LINK5_PACKED_BITS + "01011010111100101001010001111100"
ASM_LINK5_ENCODED_BITS = (
    "0010100000111000000000011111010111101111100011001100010110010010"
    "1110101111011110000110110000101110000100010000010000000100000000"
    "1010010100000000000000000000000100000001000000010000000100000001"
    "0000000100000000000000000000000100000000000000010000000100000000"
    "0000000000000001000000000000000000000001000000010000000100000001"
    "0000000100000001000000110111010011111001110100100110001010111001"
    "1111010001"
)
ASM_LINK5_SCRAMBLED_BITS = (
    "0010101111001110000010011100000111011111001101000110011000000001"
    "0010001010110110101011000111100000110111011010001010101111110101"
    "0101101100111100000001001000100100011010001100010101101110100000"
    "1101111011000100110000001001101110000011010111100000101011000010"
    "0011100010001101100100110010101101101011111110100111111100011010"
    "0000010101011011000110101010100010101101000110111001100000001101"
    "1110101111"
)
ASM_LINK5_DATA_PATTERN_8 = (
    (1, 0, 1, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 1),
)
ASM_LINK5_TAIL_PATTERN_8B = (
    (1, 0, 1, 0, 0, 0),
    (1, 0, 1, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (0, 0, 0, 1, 0, 1),
    (0, 0, 0, 1, 0, 1),
    (0, 0, 0, 1, 0, 0),
)


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bits_from_bytes(data: bytes) -> tuple[int, ...]:
    return tuple((byte >> shift) & 1 for byte in data for shift in range(7, -1, -1))


def permutation_1based() -> tuple[int, ...]:
    values = []
    for s in range(1, 433):
        m = (s - 1) % 2
        j = (s - 1) // 2
        c = (127 * j + 21 * m) % 216
        values.append(2 * (c + 1) - m)
    result = tuple(values)
    assert tuple(sorted(result)) == tuple(range(1, 433))
    return result


def state_bits(state: int) -> tuple[int, int, int]:
    return ((state >> 2) & 1, (state >> 1) & 1, state & 1)


def rsc_step(state: int, bit: int) -> tuple[int, tuple[int, int, int]]:
    d1, d2, d3 = state_bits(state)
    feedback = bit ^ d2 ^ d3
    output = (bit, feedback ^ d1 ^ d3, feedback ^ d1 ^ d2 ^ d3)
    return (feedback << 2) | (d1 << 1) | d2, output


def termination_inputs(state: int) -> tuple[int, int, int]:
    values = []
    for _ in range(3):
        _, d2, d3 = state_bits(state)
        bit = d2 ^ d3
        values.append(bit)
        state, _ = rsc_step(state, bit)
    assert state == 0
    return tuple(values)


def turbo_encode(bits: tuple[int, ...]) -> dict[str, object]:
    permutation = permutation_1based()
    interleaved = tuple(bits[index - 1] for index in permutation)
    state1 = state2 = 0
    data_rows = []
    for original, permuted in zip(bits, interleaved):
        state1, first = rsc_step(state1, original)
        state2, second = rsc_step(state2, permuted)
        data_rows.append(first + second)
    final_states = (state1, state2)
    tail_rows = []
    for _ in range(3):
        _, d2, d3 = state_bits(state1)
        state1, output = rsc_step(state1, d2 ^ d3)
        tail_rows.append(output + (0, 0, 0))
    for _ in range(3):
        _, d2, d3 = state_bits(state2)
        state2, output = rsc_step(state2, d2 ^ d3)
        tail_rows.append((0, 0, 0) + output)
    assert state1 == state2 == 0
    channel = []
    for index, row in enumerate(data_rows):
        channel.extend(value for value, keep in zip(row, DATA_PATTERN_6[index % 2]) if keep)
    data_channel_bits = tuple(channel)
    for row, pattern in zip(tail_rows, TAIL_PATTERN_6A):
        channel.extend(value for value, keep in zip(row, pattern) if keep)
    assert len(channel) == 874 and len(data_channel_bits) == 864
    return {
        "permutation_1based": permutation,
        "interleaved_bits": interleaved,
        "data_rows": tuple(data_rows),
        "tail_rows": tuple(tail_rows),
        "data_channel_bits": data_channel_bits,
        "tail_channel_bits": tuple(channel[864:]),
        "channel_bits": tuple(channel),
        "final_states_before_tail": final_states,
        "termination_inputs_by_state": tuple(termination_inputs(state) for state in range(8)),
        "trellis": tuple(tuple(rsc_step(state, bit) for bit in (0, 1)) for state in range(8)),
    }


def permutation_for(
    k: int, k1: int, k2: int, primes: tuple[int, ...]
) -> tuple[int, ...]:
    values = []
    half_k1 = k1 // 2
    for s in range(1, k + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = (s - 1) // 2 - i * k2
        t = (19 * i + 1) % half_k1
        q = t % 8
        c = (primes[q] * j + 21 * m) % k2
        values.append(2 * (t + c * half_k1 + 1) - m)
    result = tuple(values)
    assert tuple(sorted(result)) == tuple(range(1, k + 1))
    return result


def turbo_encode_profile(
    bits: tuple[int, ...],
    *,
    k1: int,
    k2: int,
    primes: tuple[int, ...],
    data_pattern: tuple[tuple[int, ...], ...],
    tail_pattern: tuple[tuple[int, ...], ...],
) -> tuple[int, ...]:
    permutation = permutation_for(len(bits), k1, k2, primes)
    interleaved = tuple(bits[index - 1] for index in permutation)
    state1 = state2 = 0
    data_rows = []
    for original, permuted in zip(bits, interleaved):
        state1, first = rsc_step(state1, original)
        state2, second = rsc_step(state2, permuted)
        data_rows.append(first + second)
    tail_rows = []
    for _ in range(3):
        _, d2, d3 = state_bits(state1)
        state1, output = rsc_step(state1, d2 ^ d3)
        tail_rows.append(output + (0, 0, 0))
    for _ in range(3):
        _, d2, d3 = state_bits(state2)
        state2, output = rsc_step(state2, d2 ^ d3)
        tail_rows.append((0, 0, 0) + output)
    assert state1 == state2 == 0
    channel = []
    for index, row in enumerate(data_rows):
        pattern = data_pattern[index % len(data_pattern)]
        for value, copies in zip(row, pattern):
            channel.extend((value,) * copies)
    for row, pattern in zip(tail_rows, tail_pattern):
        for value, copies in zip(row, pattern):
            channel.extend((value,) * copies)
    return tuple(channel)


def f64_bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def finite_metric_bits(value: float) -> int | None:
    return None if value == -math.inf else f64_bits(value)


def _normalize_reference(metrics: list[float]) -> list[float]:
    finite = [value for value in metrics if math.isfinite(value)]
    assert finite
    maximum = max(finite)
    return [value - maximum if math.isfinite(value) else -math.inf for value in metrics]


def _max_star_reference(left: float, right: float) -> float:
    if left == -math.inf:
        return right
    if right == -math.inf:
        return left
    maximum = left if left >= right else right
    return maximum + math.log1p(math.exp(-abs(left - right)))


def _siso_reference(
    systematic: tuple[float, ...],
    parity0: tuple[float, ...],
    parity1: tuple[float, ...],
    apriori_data: tuple[float, ...],
    *,
    capture_steps: bool = False,
) -> tuple[tuple[float, ...], tuple[float, ...], dict[str, object] | None]:
    steps = 435
    assert len(systematic) == len(parity0) == len(parity1) == steps
    assert len(apriori_data) == 432
    gamma = []
    for k in range(steps):
        la = apriori_data[k] if k < 432 else 0.0
        t0 = systematic[k] + la
        step = []
        for state in range(8):
            transitions = []
            for bit in (0, 1):
                target, (_, p0, p1) = rsc_step(state, bit)
                t1 = t0 if bit == 0 else -t0
                t2 = parity0[k] if p0 == 0 else -parity0[k]
                t3 = parity1[k] if p1 == 0 else -parity1[k]
                transitions.append((target, 0.5 * ((t1 + t2) + t3)))
            step.append(tuple(transitions))
        gamma.append(tuple(step))

    alpha = [[-math.inf] * 8 for _ in range(steps + 1)]
    alpha[0][0] = 0.0
    for k in range(steps):
        following = [-math.inf] * 8
        for state in range(8):
            if not math.isfinite(alpha[k][state]):
                continue
            for bit in (0, 1):
                target, branch = gamma[k][state][bit]
                following[target] = _max_star_reference(
                    following[target], alpha[k][state] + branch
                )
        alpha[k + 1] = _normalize_reference(following)

    beta = [[-math.inf] * 8 for _ in range(steps + 1)]
    beta[steps][0] = 0.0
    for k in range(steps - 1, -1, -1):
        preceding = [-math.inf] * 8
        for state in range(8):
            best = -math.inf
            for bit in (0, 1):
                target, branch = gamma[k][state][bit]
                best = _max_star_reference(best, branch + beta[k + 1][target])
            preceding[state] = best
        beta[k] = _normalize_reference(preceding)

    posterior = []
    extrinsic = []
    for k in range(432):
        maxima = [-math.inf, -math.inf]
        for state in range(8):
            for bit in (0, 1):
                target, branch = gamma[k][state][bit]
                candidate = alpha[k][state] + branch + beta[k + 1][target]
                maxima[bit] = _max_star_reference(maxima[bit], candidate)
        post = maxima[0] - maxima[1]
        posterior.append(post)
        extrinsic.append((post - systematic[k]) - apriori_data[k])

    diagnostics = None
    if capture_steps:
        gamma_steps = (0, 1, 2, 431, 432, 434)
        metric_rows = (0, 1, 2, 431, 432, 434, 435)
        diagnostics = {
            "gamma_branch_metric_f64_bits": {
                str(k): [
                    [f64_bits(gamma[k][state][bit][1]) for bit in (0, 1)]
                    for state in range(8)
                ]
                for k in gamma_steps
            },
            "alpha_metric_f64_bits": {
                str(k): [finite_metric_bits(value) for value in alpha[k]]
                for k in metric_rows
            },
            "beta_metric_f64_bits": {
                str(k): [finite_metric_bits(value) for value in beta[k]]
                for k in metric_rows
            },
        }
    return tuple(posterior), tuple(extrinsic), diagnostics


def rate_recover_reference(
    channel_llrs: tuple[float, ...],
) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[float, ...], ...]]:
    assert len(channel_llrs) == 874
    position = 0
    data_rows = []
    for clock in range(432):
        row = [0.0] * 6
        for stream, keep in enumerate(DATA_PATTERN_6[clock % 2]):
            if keep:
                row[stream] = channel_llrs[position]
                position += 1
        data_rows.append(tuple(row))
    tail_rows = []
    for pattern in TAIL_PATTERN_6A:
        row = [0.0] * 6
        for stream, keep in enumerate(pattern):
            if keep:
                row[stream] = channel_llrs[position]
                position += 1
        tail_rows.append(tuple(row))
    assert position == 874
    return tuple(data_rows), tuple(tail_rows)


def deinterleave_reference(values: tuple[float, ...]) -> tuple[float, ...]:
    restored = [0.0] * 432
    for output_index, input_index in enumerate(permutation_1based()):
        restored[input_index - 1] = values[output_index]
    return tuple(restored)


def log_map_reference(channel_llrs: tuple[float, ...]) -> dict[str, object]:
    data_rows, tail_rows = rate_recover_reference(channel_llrs)
    permutation = permutation_1based()
    interleave_values = lambda values: tuple(values[index - 1] for index in permutation)
    lx = tuple(row[0] for row in data_rows)
    y0 = tuple(row[1] for row in data_rows)
    yp0 = tuple(row[4] for row in data_rows)
    sys1 = lx + tuple(row[0] for row in tail_rows[:3])
    p01 = y0 + tuple(row[1] for row in tail_rows[:3])
    sys2 = interleave_values(lx) + tuple(row[3] for row in tail_rows[3:])
    p02 = yp0 + tuple(row[4] for row in tail_rows[3:])
    zeros = (0.0,) * 435
    la1 = (0.0,) * 432
    iterations = []
    first_siso_steps = None
    post2 = (0.0,) * 432
    for iteration in range(1, 9):
        post1, ext1, diagnostics = _siso_reference(
            sys1, p01, zeros, la1, capture_steps=iteration == 1
        )
        la2 = interleave_values(ext1)
        post2, ext2, _ = _siso_reference(sys2, p02, zeros, la2)
        la1 = deinterleave_reference(ext2)
        if diagnostics is not None:
            first_siso_steps = diagnostics
        iterations.append({
            "iteration": iteration,
            "siso1_posterior_f64_bits": tuple(f64_bits(value) for value in post1),
            "siso1_extrinsic_f64_bits": tuple(f64_bits(value) for value in ext1),
            "siso2_posterior_f64_bits": tuple(f64_bits(value) for value in post2),
            "siso2_extrinsic_f64_bits": tuple(f64_bits(value) for value in ext2),
        })
    final = deinterleave_reference(post2)
    hard = tuple(0 if value >= 0.0 else 1 for value in final)
    return {
        "identifier": "log-map-f64-8iter-v1",
        "input_llr_f64_bits": tuple(f64_bits(value) for value in channel_llrs),
        "first_iteration_siso1_step_checkpoints": first_siso_steps,
        "iterations": tuple(iterations),
        "final_llr_f64_bits": tuple(f64_bits(value) for value in final),
        "hard_bits": "".join(map(str, hard)),
    }


def scramble(bits: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    register = list(INITIAL_SCRAMBLER)
    pn = []
    for _ in bits:
        value = register[13] ^ register[14]
        pn.append(value)
        register = [value] + register[:14]
    return tuple(bit ^ value for bit, value in zip(bits, pn)), tuple(pn)


def link_codeword(link_id: int) -> tuple[int, ...]:
    data = tuple((link_id >> shift) & 1 for shift in range(5, -1, -1))
    generated = []
    for column in range(32):
        generated.append(sum(data[row] * int(LINK_ID_GENERATOR_ROWS[row][column]) for row in range(6)) & 1)
    return tuple(bit ^ int(mask) for bit, mask in zip(generated, LINK_ID_SCRAMBLING_WORD))


def labels(bits: tuple[int, ...]) -> tuple[str, ...]:
    assert len(bits) % 2 == 0
    return tuple(f"{bits[index]}{bits[index + 1]}" for index in range(0, len(bits), 2))


def map_labels(values: tuple[str, ...], start: int = 0) -> tuple[complex, ...]:
    return tuple((GREEN if (start + index) % 2 == 0 else PURPLE)[label] for index, label in enumerate(values))


def soft_demap_reference(
    symbols: tuple[complex, ...], start: int, noise_variance: float
) -> tuple[float, ...]:
    output = []
    for offset, symbol in enumerate(symbols):
        points = GREEN if (start + offset) % 2 == 0 else PURPLE
        distances = {
            label: (symbol.real - point.real) ** 2 + (symbol.imag - point.imag) ** 2
            for label, point in points.items()
        }
        for bit_index in (0, 1):
            minimum_zero = min(
                distance
                for label, distance in distances.items()
                if label[bit_index] == "0"
            )
            minimum_one = min(
                distance
                for label, distance in distances.items()
                if label[bit_index] == "1"
            )
            output.append((minimum_one - minimum_zero) / noise_variance)
    return tuple(output)


def noise_estimator_reference() -> dict[str, object]:
    training = map_labels(tuple("11" if bit == "1" else "00" for bit in TRAINING_BITS))
    observed = tuple(
        known + complex((index - 13) / 32.0, (13 - index) / 64.0)
        for index, known in enumerate(training)
    )
    residual = tuple(value - known for value, known in zip(observed, training))
    raw = sum(abs(value) ** 2 for value in residual) / 27.0
    floor = 1e-12
    return {
        "observed_training_iq_f64_bits": tuple(
            (f64_bits(value.real), f64_bits(value.imag)) for value in observed
        ),
        "raw_variance_f64_bits": f64_bits(raw),
        "variance_f64_bits": f64_bits(max(raw, floor)),
        "clean_variance_f64_bits": f64_bits(floor),
    }


def held(symbols: tuple[complex, ...], index: int) -> complex:
    return symbols[0] if index < 0 else symbols[-1] if index >= len(symbols) else symbols[index]


def carrier(symbols: tuple[complex, ...], first: int = -32, last: int = 1951) -> tuple[complex, ...]:
    output = []
    for n in range(first, last + 1):
        value = 0.0 + 0.0j
        lo = math.ceil((n - 20) / 4)
        hi = math.floor((n + 20) / 4)
        for symbol_index in range(lo, hi + 1):
            value += held(symbols, symbol_index) * RRC_TAPS[n - 4 * symbol_index + 20]
        output.append(value)
    return tuple(output)


def make_slot(symbols: tuple[complex, ...]) -> tuple[complex, ...]:
    extended = carrier(symbols)
    ramp_up = tuple(extended[index] * 10.0 ** ((-50.0 + 50.0 * index / 32.0) / 20.0) for index in range(32))
    body = extended[32:1952]
    ramp_down = tuple(extended[1952 + index] * 10.0 ** ((-50.0 * index / 32.0) / 20.0) for index in range(32))
    result = ramp_up + body + ramp_down + (0.0 + 0.0j,) * 64
    assert len(result) == 2048
    return result


def cf32le(samples: tuple[complex, ...]) -> bytes:
    return b"".join(struct.pack("<ff", value.real, value.imag) for value in samples)


def from_cf32le(data: bytes) -> tuple[complex, ...]:
    return tuple(complex(i, q) for i, q in struct.iter_unpack("<ff", data))


def interpolate_zero(samples: tuple[complex, ...], position: float) -> complex:
    k = math.floor(position)
    mu = position - k
    coefficients = (
        -mu * (mu - 1.0) * (mu - 2.0) / 6.0,
        (mu + 1.0) * (mu - 1.0) * (mu - 2.0) / 2.0,
        -(mu + 1.0) * mu * (mu - 2.0) / 2.0,
        (mu + 1.0) * mu * (mu - 1.0) / 6.0,
    )
    value = 0.0 + 0.0j
    for offset, coefficient in zip((-1, 0, 1, 2), coefficients):
        index = k + offset
        if 0 <= index < len(samples):
            value += coefficient * samples[index]
    return value


def impair(samples: tuple[complex, ...], case: dict[str, object]) -> tuple[complex, ...]:
    rho = 1.0 + int(case["sro_ppm"]) * 1e-6
    start = int(case["start_sample"])
    tau = float(case["tau_samples"])
    gain = float(case["gain"])
    cfo = int(case["cfo_hz"])
    phase = float(case["phase_rad"])
    output = []
    for n in range(2304):
        position = (n - start) / rho - tau
        value = interpolate_zero(samples, position)
        t = (n - start) / (76800.0 * rho)
        theta = phase + 2.0 * PI * cfo * t
        output.append(gain * value * complex(math.cos(theta), math.sin(theta)))
    return tuple(output)


class Sha256NormalStream:
    def __init__(self, domain: bytes, seed: int) -> None:
        self.prefix = domain + seed.to_bytes(8, "big")
        self.counter = 0
        self.words: list[int] = []
        self.consumed_words: list[int] = []

    def word(self) -> int:
        if not self.words:
            digest = hashlib.sha256(self.prefix + self.counter.to_bytes(8, "big")).digest()
            self.counter += 1
            self.words = [int.from_bytes(digest[index:index + 8], "big") for index in range(0, 32, 8)]
        value = self.words.pop(0)
        self.consumed_words.append(value)
        return value

    def pair(self) -> tuple[float, float]:
        u1 = ((self.word() >> 12) + 0.5) * 2.0 ** -52
        u2 = ((self.word() >> 12) + 0.5) * 2.0 ** -52
        radius = math.sqrt(-2.0 * math.log(u1))
        angle = 2.0 * PI * u2
        return radius * math.cos(angle), radius * math.sin(angle)


def baseline_profile(project_root: Path) -> dict[str, object]:
    text = (project_root / "docs/core/stage2_phy_link11_baseline.md").read_text(encoding="utf-8")
    marker = '{\n  "profile_schema_id": "phy11-profile-v2"'
    start = text.index(marker)
    end = text.index("\n}\n```", start) + 2
    source = text[start:end]
    replacements = {
        "RRC_TAP_BITS_41": json.dumps(RRC_TAP_BITS),
        "TRAINING_BITS_27": json.dumps(TRAINING_BITS),
        "LINK_ID_GENERATOR_ROWS_6": json.dumps(LINK_ID_GENERATOR_ROWS),
        "LINK_ID_SCRAMBLING_WORD": json.dumps(LINK_ID_SCRAMBLING_WORD),
    }
    for name, replacement in replacements.items():
        source = source.replace(name, replacement)
    profile = json.loads(source)
    assert profile["profile_schema_id"] == PROFILE_SCHEMA_ID
    return profile


def registry_fingerprint(profile: dict[str, object]) -> tuple[str, dict[str, object]]:
    entry = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    envelope = {
        "name": "phy_profile",
        "scope": "OPERATIONAL",
        "schema_version": 1,
        "content_version": REGISTRY_CONTENT_VERSION,
        "entries": [entry],
    }
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256(canonical), envelope


def write_artifact(path: Path, data: bytes, artifacts: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    artifacts[path.name if path.parent.name != "rx_samples_v1" else f"rx_samples_v1/{path.name}"] = {
        "bytes": len(data), "sha256": sha256(data)
    }


def main() -> int:
    script = Path(__file__).resolve()
    vdeter_root = script.parents[1]
    project_root = script.parents[3]
    output = vdeter_root / "tests/vectors/phy/link11"
    profile = baseline_profile(project_root)
    fingerprint, envelope = registry_fingerprint(profile)
    assert fingerprint == REGISTRY_FINGERPRINT, (fingerprint, REGISTRY_FINGERPRINT)
    tap_bytes = b"".join(struct.pack("<Q", value) for value in RRC_TAP_BITS)
    assert sha256(tap_bytes) == RRC_TAPS_SHA256
    assert struct.unpack("<Q", struct.pack("<d", PI))[0] == 0x400921FB54442D18
    assert "vdeter" not in {part.split(".")[0] for part in ("hashlib", "json", "math", "struct")}

    packet = bytes.fromhex(CANONICAL_PACKET_HEX)
    assert len(packet) == 54
    packet_bits = bits_from_bytes(packet)
    turbo = turbo_encode(packet_bits)
    scrambled, pn = scramble(turbo["channel_bits"])
    assert "".join(str(bit) for bit in link_codeword(11)) == LINK_ID_11_CODEWORD
    training_labels = tuple("11" if bit == "1" else "00" for bit in TRAINING_BITS)
    link_labels = labels(tuple(int(bit) for bit in LINK_ID_11_CODEWORD))
    data_labels = labels(scrambled)
    body_labels = training_labels + link_labels + data_labels
    body_symbols = map_labels(body_labels)
    samples = make_slot(body_symbols)
    tx_bytes = cf32le(samples)
    promoted_tx = from_cf32le(tx_bytes)

    log_map_input = tuple(
        (2.0 + ((index * 37) % 29) / 8.0) * (1.0 if bit == 0 else -1.0)
        for index, bit in enumerate(turbo["channel_bits"])
    )
    log_map = log_map_reference(log_map_input)
    assert log_map["hard_bits"] == "".join(map(str, packet_bits))

    inverse_noise_variance = 1.0
    inverse_link_llrs = soft_demap_reference(
        body_symbols[27:43], 27, inverse_noise_variance
    )
    inverse_data_llrs = soft_demap_reference(
        body_symbols[43:480], 43, inverse_noise_variance
    )
    inverse_descrambled_llrs = tuple(
        -value if pn_bit else value
        for value, pn_bit in zip(inverse_data_llrs, pn)
    )
    inverse_data_rows, inverse_tail_rows = rate_recover_reference(
        inverse_descrambled_llrs
    )
    ideal_rx_inverse = {
        "noise_variance_f64_bits": f64_bits(inverse_noise_variance),
        "link_llr_f64_bits": tuple(f64_bits(value) for value in inverse_link_llrs),
        "expected_link_id": 11,
        "data_llr_f64_bits": tuple(f64_bits(value) for value in inverse_data_llrs),
        "descrambled_llr_f64_bits": tuple(
            f64_bits(value) for value in inverse_descrambled_llrs
        ),
        "rate_recovered_data_f64_bits": tuple(
            tuple(f64_bits(value) for value in row) for row in inverse_data_rows
        ),
        "rate_recovered_tail_f64_bits": tuple(
            tuple(f64_bits(value) for value in row) for row in inverse_tail_rows
        ),
        "expected_information_bits": "".join(map(str, packet_bits)),
        "noise_estimator": noise_estimator_reference(),
    }

    artifacts: dict[str, dict[str, object]] = {}
    official = {
        "artifact_schema_version": 1,
        "baseline_version": BASELINE_VERSION,
        "classifications": {"official": ["training", "link_id", "rsc", "interleaver", "puncturing", "scrambler", "mapping", "table8_counts"], "project": ["sample_rate", "rrc_finite_taps", "ramp", "receiver", "software_gate"]},
        "counts": profile["counts"],
        "link_id_11_codeword": LINK_ID_11_CODEWORD,
        "link_id_generator_rows": LINK_ID_GENERATOR_ROWS,
        "link_id_scrambling_word": LINK_ID_SCRAMBLING_WORD,
        "profile": profile,
        "registry_envelope": envelope,
        "registry_fingerprint": fingerprint,
        "rrc_tap_binary64_bits": RRC_TAP_BITS,
        "rrc_taps": RRC_TAPS,
        "rrc_taps_sha256": RRC_TAPS_SHA256,
        "training_bits": TRAINING_BITS,
    }
    write_artifact(output / "official_constants_v1.json", canonical_json(official), artifacts)
    packet_artifact = {"packet_hex": packet.hex(), "packet_sha256": sha256(packet), "packet_bits_msb_first": "".join(map(str, packet_bits)), "source": "Stage 1 TC-LINK-005"}
    write_artifact(output / "packet_type0_v1.json", canonical_json(packet_artifact), artifacts)
    stages = {
        "packet_bits": "".join(map(str, packet_bits)),
        "permutation_1based": turbo["permutation_1based"],
        "interleaved_bits": "".join(map(str, turbo["interleaved_bits"])),
        "rsc_trellis": turbo["trellis"],
        "termination_inputs_by_state": turbo["termination_inputs_by_state"],
        "final_states_before_tail": turbo["final_states_before_tail"],
        "data_rows": turbo["data_rows"],
        "tail_rows": turbo["tail_rows"],
        "data_channel_bits": "".join(map(str, turbo["data_channel_bits"])),
        "tail_channel_bits": "".join(map(str, turbo["tail_channel_bits"])),
        "channel_bits": "".join(map(str, turbo["channel_bits"])),
        "scrambler_pn_bits": "".join(map(str, pn)),
        "scrambled_bits": "".join(map(str, scrambled)),
        "training_labels": training_labels,
        "link_id_labels": link_labels,
        "data_labels": data_labels,
        "body_labels": body_labels,
        "body_symbols_iq": tuple((value.real, value.imag) for value in body_symbols),
        "log_map_reference": log_map,
        "ideal_rx_inverse": ideal_rx_inverse,
    }
    write_artifact(output / "tx_stages_v1.json", canonical_json(stages), artifacts)
    write_artifact(output / "tx_samples_v1.cf32le", tx_bytes, artifacts)

    cases = {
        "ideal": {"start_sample": 128, "tau_samples": 0.0, "sro_ppm": 0, "gain": 1.0, "cfo_hz": 0, "phase_rad": 0.0, "phase_binary64_bits": 0x0000000000000000},
        "minimum_edges": {"start_sample": 0, "tau_samples": 0.0, "sro_ppm": -10, "gain": 0.25, "cfo_hz": -2000, "phase_rad": -PI, "phase_binary64_bits": 0xC00921FB54442D18},
        "maximum_edges": {"start_sample": 137, "tau_samples": struct.unpack("<d", struct.pack("<Q", 0x400FFFFFFFFFFFFF))[0], "sro_ppm": 10, "gain": 4.0, "cfo_hz": 2000, "phase_rad": struct.unpack("<d", struct.pack("<Q", 0x400921FB54442D17))[0], "phase_binary64_bits": 0x400921FB54442D17},
        "mixed": {"start_sample": 73, "tau_samples": 2.5, "sro_ppm": 7, "gain": 1.5, "cfo_hz": -1375, "phase_rad": struct.unpack("<d", struct.pack("<Q", 0x3FF0C152382D7366))[0], "phase_binary64_bits": 0x3FF0C152382D7366},
    }
    impairment_manifest = {"channel_id": "phy11-impairment-channel-v1", "output_sample_count": 2304, "noise": "ZERO", "cases": {}}
    for name, case in cases.items():
        data = cf32le(impair(promoted_tx, case))
        path = output / "rx_samples_v1" / f"{name}.cf32le"
        write_artifact(path, data, artifacts)
        impairment_manifest["cases"][name] = {**case, **artifacts[f"rx_samples_v1/{name}.cf32le"]}
    write_artifact(output / "rx_impairments_v1.json", canonical_json(impairment_manifest), artifacts)
    asm_input = tuple(int(bit) for bit in ASM_LINK5_TURBO_INPUT_BITS)
    asm_encoded = turbo_encode_profile(
        asm_input,
        k1=2,
        k2=144,
        primes=(47, 17, 233, 127, 239, 139, 199, 163),
        data_pattern=ASM_LINK5_DATA_PATTERN_8,
        tail_pattern=ASM_LINK5_TAIL_PATTERN_8B,
    )
    assert "".join(map(str, asm_encoded)) == ASM_LINK5_ENCODED_BITS
    asm_scrambled, asm_pn = scramble(asm_encoded)
    assert "".join(map(str, asm_scrambled)) == ASM_LINK5_SCRAMBLED_BITS
    asm_training_labels = tuple("11" if bit == "1" else "00" for bit in TRAINING_BITS)
    asm_link_codeword = link_codeword(5)
    asm_link_labels = labels(asm_link_codeword)
    asm_data_labels = labels(asm_scrambled)
    asm_body_labels = asm_training_labels + asm_link_labels + asm_data_labels
    assert len(asm_body_labels) == 240
    assert len(map_labels(asm_body_labels)) == 240
    asm = {
        "artifact_schema_version": 1,
        "classification": "OFFICIAL_AUXILIARY",
        "scope": "COMMON_PRIMITIVE_CROSS_CHECK_ONLY",
        "source": {
            "document": "ITU-R M.2092-2",
            "edition": "02/2026",
            "location": "Annex 3 §A3-8",
            "pdf_pages": (67, 68, 69),
            "printed_pages": (65, 66, 67),
        },
        "link_id": 5,
        "is_link11_end_to_end_vector": False,
        "message_fields": {
            "message_id": 5,
            "retransmit_flag": 0,
            "repeat_indicator": 0,
            "session_id": 56,
            "source_id": 123456789,
            "destination_id": 987654321,
            "ack_nack_mask": 0,
            "coding_rate_adaption_request": 0,
            "signal_quality_indicator": 100,
            "payload_bits": 103,
            "zero_padding_bits": 153,
        },
        "bit_packed_256": ASM_LINK5_PACKED_BITS,
        "turbo_input_288": ASM_LINK5_TURBO_INPUT_BITS,
        "turbo_encoded_with_tail_394": ASM_LINK5_ENCODED_BITS,
        "scrambler_pn_394": "".join(map(str, asm_pn)),
        "scrambled_394": ASM_LINK5_SCRAMBLED_BITS,
        "training_labels_27": asm_training_labels,
        "link_id_codeword_32": "".join(map(str, asm_link_codeword)),
        "link_id_labels_16": asm_link_labels,
        "data_labels_197": asm_data_labels,
        "body_labels_240": asm_body_labels,
    }
    write_artifact(output / "asm_link5_common_v1.json", canonical_json(asm), artifacts)

    normal = Sha256NormalStream(b"VDETER-PHY11-AWGN-V1", 0x5644455445521101)
    normal_pairs = tuple(normal.pair() for _ in range(1024))
    manifest = {
        "artifact_schema_version": 1,
        "baseline_id": "VDETER-STAGE2-PHY11-BL-001",
        "baseline_version": BASELINE_VERSION,
        "generator": {"path": "tools/generate_phy11_vectors.py", "sha256": sha256(script.read_bytes()), "production_phy_imported": False},
        "canonical_json": "UTF-8; ensure_ascii=false; sort_keys=true; separators=(',',':'); LF",
        "float_tolerances": {"analytic_complex_absolute": "1e-12", "independent_cf32_iq_absolute": "2e-6", "awgn_normal_iq_absolute": "2e-15", "frozen_cf32": "BYTE_EXACT"},
        "random": {
            "generator_id": "sha256-counter-box-muller-v1",
            "uniform_id": "OPEN-INTERVAL-TOP52-PLUS-HALF-V1",
            "counter_byte_order": "BIG_ENDIAN",
            "box_muller_pair_use": ["I", "Q"],
            "awgn_domain_ascii": "VDETER-PHY11-AWGN-V1",
            "awgn_seed_u64": 0x5644455445521101,
            "noise_only_domain_ascii": "VDETER-PHY11-NOISE-V1",
            "noise_only_seed_u64": 0x5644455445521102,
            "opaque_packet_domain_ascii": "VDETER-PHY11-PACKET-V1",
            "opaque_packet_seed_u64": 0x5644455445521103,
            "first_2048_words_u64": normal.consumed_words,
            "first_1024_standard_normal_iq": normal_pairs,
            "stream_reset": "ONCE_PER_TEST_CASE",
        },
        "registry_fingerprint": fingerprint,
        "rrc_taps_sha256": RRC_TAPS_SHA256,
        "artifacts": artifacts,
    }
    write_artifact(output / "manifest_v1.json", canonical_json(manifest), {})
    print(json.dumps({"output": str(output), "registry_fingerprint": fingerprint, "artifacts": len(artifacts) + 1}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
