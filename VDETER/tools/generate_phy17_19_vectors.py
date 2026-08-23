"""Generate the independent Stage 6 Link ID 17·19 conformance vectors (v2).

`stage6_phy_link17_baseline.md` §13, `stage6_phy_link19_baseline.md` §13과 공통
기준선 §9.1의 v2 artifact 계획을 구현한다. 이 프로그램은 의도적으로 표준
라이브러리와 mpmath(임의정밀 256-bit correct rounding)만 사용하며 `vdeter`
production package를 import하지 않는다. 승인 기준선에 기록된 상수가 유일한
입력이다.

산술 identity:
- impairment 회전: `mpfr256-direct-angle-binary64-rne-v1`
- timing/SRO: §7.1/§7.7 4점 Lagrange cubic, 범위 밖 zero
- 직렬화: binary64 연산 뒤 I/Q마다 binary32 RNE 1회 (`cf32le`)
"""

from __future__ import annotations

import hashlib
import math
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

VDETER_ROOT = Path(__file__).resolve().parents[1]

GENERATOR_ID = "generate-phy17-19-vectors"
GENERATOR_VERSION = "2.0.0"
ARTIFACT_VERSION = "v2"

COMMON_BASELINE = ("VDETER-STAGE6-PHY-COMMON-BL-001", "0.7.1")
LINK_BASELINES = {
    17: ("VDETER-STAGE6-PHY17-BL-001", "0.8.0", "phy17-markdown-v5"),
    19: ("VDETER-STAGE6-PHY19-BL-001", "0.8.0", "phy19-markdown-v5"),
}
COMBINED_REGISTRY = (
    "stage6-phy11-17-19-v6",
    "fbed7c508b22a75a7d390344a1d3073b8f5b92f21f0266c6f6e752ad9996a673",
)
LINK11_BASE_REGISTRY_FINGERPRINT = (
    "ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898"
)

SAMPLE_RATE_HZ = 307_200
SAMPLES_PER_SYMBOL = 4
BODY_SYMBOLS = 1_920
DATA_FIRST_BODY_INDEX = 43
RAMP_SAMPLES = 128
GUARD_SAMPLES = 256
SLOT_SAMPLES = 8_192

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
#: §8 동결 literal `0x3fe6a09e667f3bcd` (재계산 금지).
INV_SQRT2 = struct.unpack("<d", struct.pack("<Q", 4604544271217802189))[0]
#: link19 §6 승인 scale `1/sqrt(10)` canonical binary64 bits.
INV_SQRT10 = struct.unpack("<d", struct.pack("<Q", 4599368272914696463))[0]
#: §7 canonical binary64 `pi`.
PI64 = struct.unpack("<d", struct.pack("<Q", 4614256656552045848))[0]

#: Stage 2 동결 41-tap unit-energy RRC (beta 3/10, span 10, 4 sps).
RRC_TAP_BITS = (
    4561528387110721419, 13794938650351962643, 13796332409199125494,
    13783121652466558305, 4573509893763543727, 4574870330844761916,
    13784846569653267972, 13801928541859933995, 13803802258031059430,
    13792764167538667628, 4582145384294991963, 4585992274673594801,
    4583814571479215940, 13804521522459226049, 13813571218424099223,
    13815059194420588553, 13808937871933581956, 4592688422846200875,
    4599214752718274761, 4602232803638848879, 4603048513493868935,
    4602232803638848879, 4599214752718274761, 4592688422846200875,
    13808937871933581956, 13815059194420588553, 13813571218424099223,
    13804521522459226049, 4583814571479215940, 4585992274673594801,
    4582145384294991963, 13792764167538667628, 13803802258031059430,
    13801928541859933995, 13784846569653267972, 4574870330844761916,
    4573509893763543727, 13783121652466558305, 13796332409199125494,
    13794938650351962643, 4561528387110721419,
)
RRC_TAPS = tuple(
    struct.unpack("<d", struct.pack("<Q", bits))[0] for bits in RRC_TAP_BITS
)
RRC_TAPS_SHA256 = "1349ba6bac54cdd4a5e8cc3a89c4e22571a74cf20cf2f92dcf6b50b4227f588d"
RRC_HALF_SPAN = 20

LINK17_DATA_PATTERNS = ((1, 1, 0, 0, 0, 0), (1, 0, 0, 0, 1, 0))
LINK17_TAIL_PATTERNS = (
    (1, 1, 0, 0, 0, 0), (1, 1, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0),
    (0, 0, 0, 1, 1, 0), (0, 0, 0, 1, 1, 0), (0, 0, 0, 1, 0, 0),
)
LINK19_DATA_PATTERNS = (
    (1, 0, 1, 0, 0, 0), (1, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 1),
)
LINK19_TAIL_PATTERNS = (
    (1, 0, 1, 0, 0, 0), (1, 0, 1, 0, 0, 0), (1, 0, 1, 0, 0, 0),
    (0, 0, 0, 1, 0, 1), (0, 0, 0, 1, 0, 1), (0, 0, 0, 1, 0, 1),
)

PROFILES = {
    17: {
        "pl_format": "TER-MCS-1.100",
        "packet_bytes": 234,
        "information_bits": 1_872,
        "k1": 6,
        "k2": 312,
        "primes": (211, 61, 227, 239, 181, 79, 73, 193),
        "data_patterns": LINK17_DATA_PATTERNS,
        "tail_patterns": LINK17_TAIL_PATTERNS,
        "channel_bits": 3_754,
        "zero_pad_bits": 0,
        "tx_scale": 1.0,
        "tx_scale_ratio": "1/1",
        "link_bits": "010001",
        "codeword": "10000111001101110010010011100101",
        "crc_hex": "7db2d803",
        "official_es_n0_db": "1.0",
        "awgn_sweep_tenths": (-10, 50),
        "noise_only_tenths": (-10, 10, 50),
        "es_tx_ratio": "1/1",
    },
    19: {
        "pl_format": "TER-MCS-5.100",
        "packet_bytes": 702,
        "information_bits": 5_616,
        "k1": 16,
        "k2": 351,
        "primes": (137, 101, 223, 41, 67, 131, 61, 47),
        "data_patterns": LINK19_DATA_PATTERNS,
        "tail_patterns": LINK19_TAIL_PATTERNS,
        "channel_bits": 7_508,
        "zero_pad_bits": 8,
        "tx_scale": 7.0 / 8.0,
        "tx_scale_ratio": "7/8",
        "link_bits": "010011",
        "codeword": "10001111010010000010010000011010",
        "crc_hex": "deffdbce",
        "official_es_n0_db": "10.2",
        "awgn_sweep_tenths": (82, 142),
        "noise_only_tenths": (82, 102, 142),
        "es_tx_ratio": "49/64",
    },
}

#: §7.7 impairment 표 (case, delta, phase(pi 분자,분모)|None, cfo_hz, sro_ppm, gain).
IMPAIRMENT_CASES = (
    ("ideal", "0", None, 0, 0, 1.0, "1"),
    ("timing_max", "63/16", None, 0, 0, 1.0, "1"),
    ("cfo_min", "0", None, -2000, 0, 1.0, "1"),
    ("cfo_max", "0", None, 2000, 0, 1.0, "1"),
    ("sro_min", "0", None, 0, -10, 1.0, "1"),
    ("sro_max", "0", None, 0, 10, 1.0, "1"),
    ("gain_min", "0", None, 0, 0, 0.25, "1/4"),
    ("gain_max", "0", None, 0, 0, 4.0, "4"),
    ("phase_wrap", "0", (-1, 1), 0, 0, 1.0, "1"),
    ("mixed", "23/16", (3, 7), 1375, 6, 1.25, "5/4"),
)

#: 공통 §7.9의 wideband canonical cut index 21개.
STREAM_CUTS = (
    64, 127, 128, 129, 182, 235, 236, 237, 268, 299, 300, 301,
    4054, 7807, 7808, 7809, 7872, 7935, 7936, 7937, 8064,
)
STREAM_REGIONS = "ramp [0,128), training [128,236), ID [236,300), data [300,7808), ramp-down [7808,7936), guard [7936,8192)"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_packet(link_id: int) -> bytes:
    profile = PROFILES[link_id]
    prefix = bytes.fromhex("00000c010203040001017f14")
    fill = profile["packet_bytes"] - len(prefix) - 4
    return prefix + b"\x51" * fill + bytes.fromhex(profile["crc_hex"])


def bits_from_bytes(data: bytes) -> tuple[int, ...]:
    bits = []
    for value in data:
        for shift in range(7, -1, -1):
            bits.append((value >> shift) & 1)
    return tuple(bits)


# --- M.2092 general internal interleaver (§4) -------------------------------

def general_permutation(k: int, k1: int, k2: int, primes) -> tuple[int, ...]:
    output = []
    for s in range(1, k + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = (s - 1) // 2 - i * k2
        t = (19 * i + 1) % (k1 // 2)
        q = (t % 8) + 1
        c = (primes[q - 1] * j + 21 * m) % k2
        pi_s = 2 * (t + c * (k1 // 2) + 1) - m
        output.append(pi_s - 1)
    assert sorted(output) == list(range(k)), "interleaver must be a permutation"
    return tuple(output)


# --- 공통 RSC·termination (§3) ----------------------------------------------

def rsc_step(state: tuple[int, int, int], bit: int):
    d1, d2, d3 = state
    f = bit ^ d2 ^ d3
    x = bit
    y0 = f ^ d1 ^ d3
    y1 = f ^ d1 ^ d2 ^ d3
    return (f, d1, d2), (x, y0, y1)


def rsc_terminate(state: tuple[int, int, int]):
    rows = []
    for _ in range(3):
        d1, d2, d3 = state
        bit = d2 ^ d3
        x = bit
        y0 = d1 ^ d3
        y1 = d1 ^ d2 ^ d3
        rows.append((x, y0, y1))
        state = (0, d1, d2)
    assert state == (0, 0, 0)
    return rows


def turbo_encode(bits, permutation):
    state = (0, 0, 0)
    first_rows = []
    for bit in bits:
        state, row = rsc_step(state, bit)
        first_rows.append(row)
    first_tail = rsc_terminate(state)

    state = (0, 0, 0)
    second_rows = []
    for index in permutation:
        state, row = rsc_step(state, bits[index])
        second_rows.append(row)
    second_tail = rsc_terminate(state)

    data_rows = [
        (x, y0, y1, xp, yp0, yp1)
        for (x, y0, y1), (xp, yp0, yp1) in zip(first_rows, second_rows)
    ]
    tail_rows = [(x, y0, y1, 0, 0, 0) for x, y0, y1 in first_tail]
    tail_rows += [(0, 0, 0, xp, yp0, yp1) for xp, yp0, yp1 in second_tail]
    return data_rows, tail_rows


def puncture(rows, patterns) -> tuple[int, ...]:
    output = []
    for index, row in enumerate(rows):
        pattern = patterns[index % len(patterns)]
        for value, keep in zip(row, pattern):
            if keep:
                output.append(value)
    return tuple(output)


def serial_channel_bits(link_id: int, data_rows, tail_rows) -> tuple[int, ...]:
    profile = PROFILES[link_id]
    data_bits = puncture(data_rows, profile["data_patterns"])
    tail_bits = puncture(tail_rows, profile["tail_patterns"])
    serial = data_bits + (0,) * profile["zero_pad_bits"] + tail_bits
    assert len(serial) == profile["channel_bits"]
    return serial


# --- scrambler (§5.1) -------------------------------------------------------

def pn_sequence(count: int) -> tuple[int, ...]:
    register = [1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    output = []
    for _ in range(count):
        pn = register[13] ^ register[14]
        output.append(pn)
        register = [pn] + register[:14]
    return tuple(output)


def scramble(bits) -> tuple[int, ...]:
    return tuple(bit ^ mask for bit, mask in zip(bits, pn_sequence(len(bits))))


# --- mapping (§6, §8) -------------------------------------------------------

GREEN = {
    "00": complex(-INV_SQRT2, -INV_SQRT2),
    "01": complex(-INV_SQRT2, INV_SQRT2),
    "11": complex(INV_SQRT2, INV_SQRT2),
    "10": complex(INV_SQRT2, -INV_SQRT2),
}
PURPLE = {
    "00": complex(-1.0, 0.0),
    "01": complex(0.0, 1.0),
    "11": complex(1.0, 0.0),
    "10": complex(0.0, -1.0),
}
QAM_LEVELS = {"00": -3.0, "01": -1.0, "11": 1.0, "10": 3.0}


def map_pi4(labels, start_index: int) -> list[complex]:
    symbols = []
    for offset, label in enumerate(labels):
        table = GREEN if (start_index + offset) % 2 == 0 else PURPLE
        symbols.append(table[label])
    return symbols


def labels_of(bits) -> list[str]:
    assert len(bits) % 2 == 0
    return [f"{bits[i]}{bits[i + 1]}" for i in range(0, len(bits), 2)]


def map_data_symbols(link_id: int, scrambled) -> list[complex]:
    if link_id == 17:
        return map_pi4(labels_of(scrambled), DATA_FIRST_BODY_INDEX)
    symbols = []
    for i in range(0, len(scrambled), 4):
        i_label = f"{scrambled[i]}{scrambled[i + 1]}"
        q_label = f"{scrambled[i + 2]}{scrambled[i + 3]}"
        symbols.append(
            complex(QAM_LEVELS[i_label] * INV_SQRT10, QAM_LEVELS[q_label] * INV_SQRT10)
        )
    return symbols


def link_codeword_bits(link_id: int) -> tuple[int, ...]:
    value_bits = PROFILES[link_id]["link_bits"]
    raw = [0] * 32
    for row, bit in zip(LINK_ID_GENERATOR_ROWS, value_bits):
        if bit == "1":
            raw = [a ^ int(b) for a, b in zip(raw, row)]
    codeword = tuple(
        value ^ int(mask) for value, mask in zip(raw, LINK_ID_SCRAMBLING_WORD)
    )
    assert "".join(str(bit) for bit in codeword) == PROFILES[link_id]["codeword"]
    return codeword


def body_symbols_for(link_id: int, scrambled) -> list[complex]:
    training_labels = ["11" if bit == "1" else "00" for bit in TRAINING_BITS]
    prefix = map_pi4(training_labels, 0)
    prefix += map_pi4(labels_of(link_codeword_bits(link_id)), 27)
    data = map_data_symbols(link_id, scrambled)
    body = prefix + data
    assert len(body) == BODY_SYMBOLS
    return body


# --- waveform (§7) ----------------------------------------------------------

def held(symbols, index: int) -> complex:
    if index < 0:
        return symbols[0]
    if index >= len(symbols):
        return symbols[-1]
    return symbols[index]


def pulse_shaped_carrier(body) -> list[complex]:
    first_sample = -RAMP_SAMPLES
    last_sample = BODY_SYMBOLS * SAMPLES_PER_SYMBOL + RAMP_SAMPLES - 1
    output = []
    for n in range(first_sample, last_sample + 1):
        minimum_symbol = math.ceil((n - RRC_HALF_SPAN) / SAMPLES_PER_SYMBOL)
        maximum_symbol = math.floor((n + RRC_HALF_SPAN) / SAMPLES_PER_SYMBOL)
        value = 0.0 + 0.0j
        for symbol_index in range(minimum_symbol, maximum_symbol + 1):
            tap_index = n - SAMPLES_PER_SYMBOL * symbol_index
            value += held(body, symbol_index) * RRC_TAPS[tap_index + RRC_HALF_SPAN]
        output.append(value)
    return output


def make_slot(body, tx_scale: float) -> list[complex]:
    carrier = pulse_shaped_carrier(body)
    body_samples = BODY_SYMBOLS * SAMPLES_PER_SYMBOL
    ramp_up = [
        value * (10.0 ** ((-50.0 + 50.0 * index / RAMP_SAMPLES) / 20.0))
        for index, value in enumerate(carrier[:RAMP_SAMPLES])
    ]
    core = carrier[RAMP_SAMPLES:RAMP_SAMPLES + body_samples]
    ramp_down = [
        value * (10.0 ** ((-50.0 * index / RAMP_SAMPLES) / 20.0))
        for index, value in enumerate(carrier[RAMP_SAMPLES + body_samples:])
    ]
    active = [value * tx_scale for value in ramp_up + core + ramp_down]
    slot = active + [0.0 + 0.0j] * GUARD_SAMPLES
    assert len(slot) == SLOT_SAMPLES
    return slot


def cf32le(samples) -> bytes:
    output = bytearray()
    for value in samples:
        output += struct.pack("<2f", value.real, value.imag)
    return bytes(output)


# --- §7.7 impairment channel ------------------------------------------------

def cubic_interp(samples, position: float) -> complex:
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


def rotation_factors(phase_ratio, cfo_hz: int, count: int):
    if phase_ratio is None and cfo_hz == 0:
        return None
    from mpmath import mp, mpf, cos, sin

    mp.prec = 256
    pi256 = mpf(PI64)
    phi = pi256 * phase_ratio[0] / phase_ratio[1] if phase_ratio else mpf(0)
    return [
        (
            float(cos(phi + 2 * pi256 * cfo_hz * index / SAMPLE_RATE_HZ)),
            float(sin(phi + 2 * pi256 * cfo_hz * index / SAMPLE_RATE_HZ)),
        )
        for index in range(count)
    ]


def impair(slot, delta_expr: str, phase_ratio, cfo_hz: int, sro_ppm: int, gain: float):
    if "/" in delta_expr:
        numerator, denominator = delta_expr.split("/")
        delta = float(int(numerator)) / float(int(denominator))
    else:
        delta = float(int(delta_expr))
    rho = 1.0 + sro_ppm * 1e-6
    shifted = [cubic_interp(slot, (n - delta) / rho) for n in range(len(slot))]
    factors = rotation_factors(phase_ratio, cfo_hz, len(slot))
    if factors is None:
        return [gain * value for value in shifted]
    output = []
    for value, (c, s) in zip(shifted, factors):
        scaled = gain * value
        output.append(
            complex(scaled.real * c - scaled.imag * s, scaled.real * s + scaled.imag * c)
        )
    return output


# --- artifacts --------------------------------------------------------------

def bit_string(bits) -> str:
    return "".join(str(bit) for bit in bits)


def write_artifacts(link_id: int, out_root: Path, generated: dict) -> None:
    profile = PROFILES[link_id]
    directory = out_root / f"link{link_id}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rx_samples_v2").mkdir(exist_ok=True)
    artifacts: dict[str, bytes] = {}

    packet = canonical_packet(link_id)
    artifacts["packet_type0_v2.hex"] = (packet.hex() + "\n").encode("ascii")

    baseline_id, baseline_version, profile_id = LINK_BASELINES[link_id]
    official = [
        f"# Link ID {link_id} 공식 상수 (v2)",
        "",
        f"- baseline: `{baseline_id}` v{baseline_version} (`{profile_id}`)",
        f"- PL format: `{profile['pl_format']}`",
        f"- Packet: `{profile['packet_bytes']} bytes` / information `{profile['information_bits']} bits`",
        f"- interleaver: `K={profile['information_bits']}, k1={profile['k1']}, k2={profile['k2']}, p={profile['primes']}`",
        f"- channel bits: `{profile['channel_bits']}` (zero padding `{profile['zero_pad_bits']}`)",
        f"- Link ID bits: `{profile['link_bits']}` / codeword `{profile['codeword']}`",
        f"- TX scale: `{profile['tx_scale_ratio']}` / official Es/N0 `{profile['official_es_n0_db']} dB`",
        "- training: `" + TRAINING_BITS + "`",
        f"- RRC taps SHA-256: `{RRC_TAPS_SHA256}`",
        "",
    ]
    artifacts["official_constants_v2.md"] = "\n".join(official).encode("utf-8")

    stages = generated["stages"]
    tx_stages = [
        f"# Link ID {link_id} TX stage bits (v2)",
        "",
        f"- information bits ({len(stages['information'])}): SHA-256 "
        f"`{sha256_hex(bit_string(stages['information']).encode('ascii'))}`",
        "",
        "```text",
        bit_string(stages["information"]),
        "```",
        "",
        f"- serial channel bits ({len(stages['serial'])}, "
        f"`punctured_data || zero_pad_{profile['zero_pad_bits']} || tail`): SHA-256 "
        f"`{sha256_hex(bit_string(stages['serial']).encode('ascii'))}`",
        "",
        "```text",
        bit_string(stages["serial"]),
        "```",
        "",
        f"- scrambled channel bits ({len(stages['scrambled'])}): SHA-256 "
        f"`{sha256_hex(bit_string(stages['scrambled']).encode('ascii'))}`",
        "",
        "```text",
        bit_string(stages["scrambled"]),
        "```",
        "",
    ]
    artifacts["tx_stages_v2.md"] = "\n".join(tx_stages).encode("utf-8")

    symbol_lines = [
        f"# Link ID {link_id} body symbols (v2)",
        "",
        "1,920 body symbols의 canonical binary64 bit pattern (`index i_bits q_bits`).",
        "",
        "```text",
    ]
    for index, value in enumerate(generated["body"]):
        i_bits = struct.unpack("<Q", struct.pack("<d", value.real))[0]
        q_bits = struct.unpack("<Q", struct.pack("<d", value.imag))[0]
        symbol_lines.append(f"{index} {i_bits} {q_bits}")
    symbol_lines += ["```", ""]
    artifacts["tx_symbols_v2.md"] = "\n".join(symbol_lines).encode("utf-8")

    artifacts["tx_samples_v2.cf32le"] = generated["slot_cf32"]

    for case_name, data in generated["rx_cases"].items():
        artifacts[f"rx_samples_v2/{case_name}.cf32le"] = data

    impairment_policy = [
        f"# Link ID {link_id} impairment policy (v2)",
        "",
        "공통 §7.7의 결정적 채널: `u[n]=cubic_interp(s,(n-delta)/rho)`,"
        " `v[n]=A*u[n]*exp(j*(phi+2*pi*f*n/Fs))`, timing/SRO → gain/phase/CFO → (AWGN 없음).",
        "회전각 identity는 `mpfr256-direct-angle-binary64-rne-v1`,"
        " 보간은 4점 Lagrange cubic(범위 밖 zero)이다.",
        "",
        "| case | delta(samples) | phase | CFO(Hz) | SRO(ppm) | gain |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case_name, delta, phase_ratio, cfo, sro, _gain, gain_expr in IMPAIRMENT_CASES:
        phase_text = (
            "0" if phase_ratio is None else f"{phase_ratio[0]}*pi/{phase_ratio[1]}"
        )
        impairment_policy.append(
            f"| `{case_name.upper()}` | `{delta}` | `{phase_text}` | `{cfo}` | `{sro}` | `{gain_expr}` |"
        )
    impairment_policy += [
        "",
        "random Packet: domain `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2`, seed "
        "`0x5644455445520000 + link_id*0x100 + case_index*0x10000 + sequence(0..63)`, "
        "generator `sha256-counter-bytes-v2` (공통 §11.1). case당 canonical 1 + random 64.",
        "",
    ]
    artifacts["impairment_policy_v2.md"] = "\n".join(impairment_policy).encode("utf-8")

    split_policy = [
        f"# Link ID {link_id} stream split policy (v2)",
        "",
        f"공통 §7.9: region {STREAM_REGIONS}.",
        "각 cut `c`는 첫 block `[0,c)`, 둘째 block `[c,8192)`이며"
        " `CONTIGUOUS`/`GAP_BEFORE`/`UNKNOWN` 세 변형을 정확히 한 번 실행한다 (합계 63 subcases).",
        "`CONTIGUOUS`는 unsplit 결과와 Packet bytes·Link ID·burst timestamp가"
        " exact-equivalent이고 반환 횟수 1, `GAP_BEFORE`/`UNKNOWN`은 이전 partial 폐기와"
        " 무중복(둘째 block 단독으로 완전한 burst가 없으면 정상 `NO_BURST_DETECTED`)이다.",
        "둘째 block의 기대 epoch는 exact rational `t0 + c/Fs`의 half-away 반올림 1회다.",
        "중복 sample artifact는 만들지 않는다 (`tx_samples_v2.cf32le`에서 파생).",
        "",
        "| cut index | 2nd-block epoch offset (ns, Fs=307200) |",
        "| ---: | ---: |",
    ]
    for cut in STREAM_CUTS:
        exact = cut * 1_000_000_000
        offset_ns = (exact + SAMPLE_RATE_HZ // 2) // SAMPLE_RATE_HZ
        remainder2 = (exact % SAMPLE_RATE_HZ) * 2
        if remainder2 == SAMPLE_RATE_HZ:  # tie: half away from zero(양수라 올림)
            offset_ns = exact // SAMPLE_RATE_HZ + 1
        split_policy.append(f"| `{cut}` | `{offset_ns}` |")
    split_policy.append("")
    artifacts["stream_split_policy_v2.md"] = "\n".join(split_policy).encode("utf-8")

    low, high = profile["awgn_sweep_tenths"]
    awgn_policy = [
        f"# Link ID {link_id} AWGN policy (v2)",
        "",
        f"- sweep: 정수 tenth-dB `{low}..{high}` (61 points), point당 1,000 bursts,"
        " canonical Packet 반복(`REPEAT-CANONICAL-PACKET-1000`).",
        f"- `Es_tx = {profile['es_tx_ratio']}` (고정 TX scale 제곱), `N0=Es_tx*10^(-gamma/10)`,"
        " scale identity `mpfr256-db-to-binary64-rne-v1`.",
        "- 난수: `sha256-counter-box-muller-mpfr256-v2`, domain"
        f" `VDETER-PHY{link_id}-AWGN-V1`, seed `0x5644455445520000 + link_id*0x100 + sequence(0..999)`,"
        " burst마다 counter 0 reset, 같은 sequence는 전 SNR point에서 같은 stream.",
        f"- noise-only: tenth-dB {profile['noise_only_tenths']} 대응 N0에서 각 1,000 slots,"
        f" domain `VDETER-PHY{link_id}-NOISE-V1`, 모두 정상 `NO_BURST_DETECTED`.",
        "- 합성: canonical `cf32le` 승격 → binary64 scale·덧셈 → I/Q binary32 RNE 1회."
        " peak 정규화 없음(`test-iq-int16-le` projection에 잡음을 더하지 않는다).",
        f"- gate 규칙: 오름차순 최초 `>=990/1000`, 바로 아래 grid `<990/1000` (§7.7)."
        f" 공식 `{profile['official_es_n0_db']} dB`는 characterization이며 PER 합격률 주장이 아니다.",
        "- 실행 도구: `tools/run_phy_awgn_tests.py` (1,000-burst 실제 결과는 vector가 아니라 evidence).",
        "",
    ]
    artifacts["awgn_policy_v2.md"] = "\n".join(awgn_policy).encode("utf-8")

    # SHA256SUMS: path UTF-8 byte 오름차순, `hash␠␠path`, LF, 마지막 줄 LF.
    sums_lines = [
        f"{sha256_hex(data)}  {path}"
        for path, data in sorted(artifacts.items(), key=lambda item: item[0].encode())
    ]
    sums_bytes = ("\n".join(sums_lines) + "\n").encode("ascii")

    generator_source = Path(__file__).read_bytes()
    manifest = [
        f"# Link ID {link_id} vector manifest (v2)",
        "",
        "## Identity",
        "",
        f"- link baseline: `{baseline_id}` v`{baseline_version}`, profile `{profile_id}`",
        f"- common baseline: `{COMMON_BASELINE[0]}` v`{COMMON_BASELINE[1]}`",
        f"- combined registry: `{COMBINED_REGISTRY[0]}`, fingerprint `{COMBINED_REGISTRY[1]}`",
        f"- Link 11 base registry fingerprint: `{LINK11_BASE_REGISTRY_FINGERPRINT}`",
        "",
        "## Generator",
        "",
        f"- id/version: `{GENERATOR_ID}` `{GENERATOR_VERSION}`",
        f"- source SHA-256: `{sha256_hex(generator_source)}`",
        "- production import 금지: 이 generator는 표준 라이브러리와 mpmath만 사용하며"
        " `vdeter` package를 import하지 않는다.",
        "- 생성 명령: `.venv/Scripts/python.exe tools/generate_phy17_19_vectors.py`",
        "",
        "## RNG",
        "",
        "- Packet bytes: `sha256-counter-bytes-v2`"
        " (loopback domain `VDETER-PHY{L}-LOOPBACK-PACKET-V2`, impairment domain"
        " `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2`, 공통 §11.1의 seed·소비 순서)",
        "- 표준정규: `sha256-counter-box-muller-mpfr256-v2` (AWGN/noise-only, §7.7)",
        "- 회전각: `mpfr256-direct-angle-binary64-rne-v1`, dB scale: `mpfr256-db-to-binary64-rne-v1`",
        "",
        "## Artifacts",
        "",
        "| path | role | media type | bytes | format/tolerance | SHA-256 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    roles = {
        "official_constants_v2.md": ("official constants", "text/markdown", "exact"),
        "packet_type0_v2.hex": ("canonical Packet", "text/plain", "byte-exact"),
        "tx_stages_v2.md": ("TX stage bits", "text/markdown", "bit-exact"),
        "tx_symbols_v2.md": ("body symbols", "text/markdown", "binary64 bit-exact"),
        "tx_samples_v2.cf32le": ("canonical slot", "application/octet-stream", "cf32le byte-exact"),
        "impairment_policy_v2.md": ("impairment policy", "text/markdown", "exact"),
        "stream_split_policy_v2.md": ("stream split policy", "text/markdown", "exact"),
        "awgn_policy_v2.md": ("AWGN policy", "text/markdown", "exact"),
    }
    for path, data in sorted(artifacts.items(), key=lambda item: item[0].encode()):
        if path.startswith("rx_samples_v2/"):
            role, media, tolerance = (
                "deterministic RX samples", "application/octet-stream", "cf32le byte-exact",
            )
        else:
            role, media, tolerance = roles[path]
        manifest.append(
            f"| `{path}` | {role} | {media} | {len(data)} | {tolerance} | `{sha256_hex(data)}` |"
        )
    manifest += [
        f"| `SHA256SUMS` | machine manifest | text/plain | {len(sums_bytes)} |"
        f" LF, path-ascending | `{sha256_hex(sums_bytes)}` |",
        "",
        "## 확인",
        "",
        "- 공식 표 대조 책임: 이 manifest의 상수는 승인 기준선 v0.8.0에서 옮겨 적은"
        " 값이며 공식 PDF 대조는 기준선 승인 절차가 수행했다.",
        "- historical v1 artifact는 이 v2 corpus로 승격하지 않았다"
        " (`manifest_v1.json` 계열은 존재하지 않으며 새로 생성했다).",
        "- AWGN/noise-only의 1,000-burst 실제 결과와 RF 측정값은 vector가 아니라 evidence다.",
        f"- 생성 시각(참고, identity 아님): {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    manifest_bytes = "\n".join(manifest).encode("utf-8")

    for path, data in artifacts.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / "SHA256SUMS").write_bytes(sums_bytes)
    (directory / "vector_manifest_v2.md").write_bytes(manifest_bytes)
    print(
        f"[link {link_id}] {len(artifacts) + 2} artifacts -> {directory}",
        flush=True,
    )


def generate_link(link_id: int) -> dict:
    profile = PROFILES[link_id]
    packet = canonical_packet(link_id)
    information = bits_from_bytes(packet)
    assert len(information) == profile["information_bits"]
    permutation = general_permutation(
        profile["information_bits"], profile["k1"], profile["k2"], profile["primes"]
    )
    data_rows, tail_rows = turbo_encode(information, permutation)
    serial = serial_channel_bits(link_id, data_rows, tail_rows)
    scrambled = scramble(serial)
    body = body_symbols_for(link_id, scrambled)
    slot = make_slot(body, profile["tx_scale"])
    slot_cf32 = cf32le(slot)
    # full-scale 검사: binary32 승격 후 I^2+Q^2 <= 1.0
    for real, imag in struct.iter_unpack("<2f", slot_cf32):
        assert real * real + imag * imag <= 1.0, "full-scale violation"

    rx_cases = {}
    for case_name, delta, phase_ratio, cfo, sro, gain, _expr in IMPAIRMENT_CASES:
        rx_cases[case_name] = cf32le(impair(slot, delta, phase_ratio, cfo, sro, gain))
    return {
        "stages": {
            "information": information,
            "serial": serial,
            "scrambled": scrambled,
        },
        "body": body,
        "slot_cf32": slot_cf32,
        "rx_cases": rx_cases,
    }


def main() -> int:
    out_root = VDETER_ROOT / "tests" / "vectors" / "phy"
    for link_id in (17, 19):
        write_artifacts(link_id, out_root, generate_link(link_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
