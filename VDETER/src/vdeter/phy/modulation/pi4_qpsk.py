"""Alternating green/purple pi/4-QPSK mapping and positive-means-zero demapping."""

from __future__ import annotations

import math
import struct

CONSTELLATION_SCALE = struct.unpack("<d", struct.pack("<Q", 0x3FE6A09E667F3BCD))[0]
LABEL_ORDER = ("00", "01", "11", "10")
GREEN = {
    "00": complex(-CONSTELLATION_SCALE, -CONSTELLATION_SCALE),
    "01": complex(-CONSTELLATION_SCALE, CONSTELLATION_SCALE),
    "11": complex(CONSTELLATION_SCALE, CONSTELLATION_SCALE),
    "10": complex(CONSTELLATION_SCALE, -CONSTELLATION_SCALE),
}
PURPLE = {
    "00": complex(-1.0, 0.0),
    "01": complex(0.0, 1.0),
    "11": complex(1.0, 0.0),
    "10": complex(0.0, -1.0),
}
NOISE_VARIANCE_FLOOR = struct.unpack("<d", struct.pack("<Q", 0x3D719799812DEA11))[0]


def constellation(body_symbol_index: int) -> dict[str, complex]:
    if isinstance(body_symbol_index, bool) or not isinstance(body_symbol_index, int):
        raise ValueError("body symbol index must be an integer")
    return GREEN if body_symbol_index % 2 == 0 else PURPLE


def label_pairs(bits) -> tuple[str, ...]:
    source = tuple(bits)
    if len(source) % 2 or any(bit not in (0, 1) for bit in source):
        raise ValueError("pi/4-QPSK input must contain bit pairs")
    return tuple(f"{source[index]}{source[index + 1]}" for index in range(0, len(source), 2))


def map_labels(labels, start_body_symbol_index: int = 0) -> tuple[complex, ...]:
    source = tuple(labels)
    output = []
    for offset, label in enumerate(source):
        if label not in LABEL_ORDER:
            raise ValueError(f"unsupported pi/4-QPSK label: {label!r}")
        output.append(constellation(start_body_symbol_index + offset)[label])
    return tuple(output)


def map_bits(bits, start_body_symbol_index: int = 0) -> tuple[complex, ...]:
    return map_labels(label_pairs(bits), start_body_symbol_index)


def soft_demap_symbol(
    sample: complex,
    body_symbol_index: int,
    noise_variance: float,
) -> tuple[float, float]:
    if not math.isfinite(sample.real) or not math.isfinite(sample.imag):
        raise ArithmeticError("non-finite pi/4-QPSK sample")
    if not math.isfinite(noise_variance) or noise_variance <= 0.0:
        raise ArithmeticError("noise variance must be finite and positive")
    points = constellation(body_symbol_index)
    distances = {
        label: (sample.real - point.real) ** 2 + (sample.imag - point.imag) ** 2
        for label, point in points.items()
    }
    llrs = []
    for bit_index in (0, 1):
        minimum_zero = min(
            distance for label, distance in distances.items() if label[bit_index] == "0"
        )
        minimum_one = min(
            distance for label, distance in distances.items() if label[bit_index] == "1"
        )
        llrs.append((minimum_one - minimum_zero) / noise_variance)
    return tuple(llrs)


def soft_demap(symbols, start_body_symbol_index: int, noise_variance: float) -> tuple[float, ...]:
    output = []
    for offset, symbol in enumerate(symbols):
        output.extend(
            soft_demap_symbol(symbol, start_body_symbol_index + offset, noise_variance)
        )
    return tuple(output)


def hard_label(sample: complex, body_symbol_index: int) -> str:
    points = constellation(body_symbol_index)
    return min(
        LABEL_ORDER,
        key=lambda label: (
            (sample.real - points[label].real) ** 2 + (sample.imag - points[label].imag) ** 2,
            LABEL_ORDER.index(label),
        ),
    )
