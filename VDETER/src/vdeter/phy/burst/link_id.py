"""Official M.2092-2 (32,6) Link-ID code and deterministic soft decision."""

from __future__ import annotations

import math

GENERATOR_ROWS = (
    "10000010111010011110100110010110",
    "01000001110101011101010101010101",
    "00100011101100110011001100110011",
    "00010011000011111000111100001111",
    "00001000011111110000000011111111",
    "00000100000000000111111111111111",
)
SCRAMBLING_WORD = "11000010111000101000111001001111"
MAX_VALID_HAMMING_DISTANCE = 7


def codeword(link_id: int) -> tuple[int, ...]:
    if isinstance(link_id, bool) or not isinstance(link_id, int) or not 0 <= link_id <= 63:
        raise ValueError("Link ID must be a six-bit integer")
    data = tuple((link_id >> shift) & 1 for shift in range(5, -1, -1))
    result = [int(bit) for bit in SCRAMBLING_WORD]
    for enabled, row in zip(data, GENERATOR_ROWS):
        if enabled:
            result = [left ^ int(right) for left, right in zip(result, row)]
    return tuple(result)


CODEWORDS = tuple(codeword(link_id) for link_id in range(64))
LINK_ID_11_CODEWORD = CODEWORDS[11]


class LinkIdDetectionError(ArithmeticError):
    pass


def detect(llrs) -> int:
    values = tuple(float(value) for value in llrs)
    if len(values) != 32 or any(not math.isfinite(value) for value in values):
        raise LinkIdDetectionError("Link ID detection requires 32 finite LLRs")
    scores = []
    for candidate in CODEWORDS:
        score = 0.0
        for bit, value in zip(candidate, values):
            score = score + (value if bit == 0 else -value)
        if not math.isfinite(score):
            raise LinkIdDetectionError("Link ID score is non-finite")
        scores.append(score)
    maximum = max(scores)
    hard = tuple(0 if value >= 0.0 else 1 for value in values)
    valid = []
    for link_id, (candidate, score) in enumerate(zip(CODEWORDS, scores)):
        if score != maximum:
            continue
        distance = sum(left != right for left, right in zip(hard, candidate))
        if distance <= MAX_VALID_HAMMING_DISTANCE:
            valid.append(link_id)
    if len(valid) != 1:
        raise LinkIdDetectionError("Link ID score has no unique valid codeword")
    return valid[0]
