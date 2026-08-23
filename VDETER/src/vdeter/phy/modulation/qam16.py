"""M.2092-2 Fig. 13 Gray 16-QAM mapping and positive-means-zero max-log demapping.

`stage6_phy_link19_baseline.md` §6·§9·§12.3 — 앞 2 bits가 I Gray level, 뒤 2 bits가 Q Gray
level(`00->-3`, `01->-1`, `11->+1`, `10->+3`)이고 승인 scale은 `1/sqrt(10)`이다. slicer의
exact-distance tie는 4-bit label ASCII lexicographic 오름차순이다.
"""

from __future__ import annotations

import math
import struct

CONSTELLATION_SCALE = struct.unpack("<d", struct.pack("<Q", 4599368272914696463))[0]
_GRAY_LEVELS = {"00": -3.0, "01": -1.0, "11": 1.0, "10": 3.0}
LABELS = tuple(
    f"{i_bits}{q_bits}" for i_bits in ("00", "01", "10", "11") for q_bits in ("00", "01", "10", "11")
)
CONSTELLATION = {
    label: complex(
        _GRAY_LEVELS[label[:2]] * CONSTELLATION_SCALE,
        _GRAY_LEVELS[label[2:]] * CONSTELLATION_SCALE,
    )
    for label in LABELS
}
_SORTED_LABELS = tuple(sorted(LABELS))


def label_quads(bits) -> tuple[str, ...]:
    source = tuple(bits)
    if len(source) % 4 or any(bit not in (0, 1) for bit in source):
        raise ValueError("16-QAM input must contain bit quads")
    return tuple(
        f"{source[index]}{source[index + 1]}{source[index + 2]}{source[index + 3]}"
        for index in range(0, len(source), 4)
    )


def map_bits(bits) -> tuple[complex, ...]:
    return tuple(CONSTELLATION[label] for label in label_quads(bits))


def soft_demap_symbol(sample: complex, noise_variance: float) -> tuple[float, float, float, float]:
    if not math.isfinite(sample.real) or not math.isfinite(sample.imag):
        raise ArithmeticError("non-finite 16-QAM sample")
    if not math.isfinite(noise_variance) or noise_variance <= 0.0:
        raise ArithmeticError("noise variance must be finite and positive")
    distances = {
        label: (sample.real - point.real) ** 2 + (sample.imag - point.imag) ** 2
        for label, point in CONSTELLATION.items()
    }
    llrs = []
    for bit_index in range(4):
        minimum_zero = min(
            distance for label, distance in distances.items() if label[bit_index] == "0"
        )
        minimum_one = min(
            distance for label, distance in distances.items() if label[bit_index] == "1"
        )
        llrs.append((minimum_one - minimum_zero) / noise_variance)
    return tuple(llrs)


def soft_demap(symbols, noise_variance: float) -> tuple[float, ...]:
    output = []
    for symbol in symbols:
        output.extend(soft_demap_symbol(symbol, noise_variance))
    return tuple(output)


def hard_label(sample: complex) -> str:
    return min(
        _SORTED_LABELS,
        key=lambda label: (
            (sample.real - CONSTELLATION[label].real) ** 2
            + (sample.imag - CONSTELLATION[label].imag) ** 2,
            label,
        ),
    )


def hard_symbol(sample: complex) -> complex:
    return CONSTELLATION[hard_label(sample)]
