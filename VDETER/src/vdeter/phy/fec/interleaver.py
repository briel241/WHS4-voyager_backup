"""Approved M.2092-2 Link ID 11 Turbo internal interleaver."""

from __future__ import annotations

K = 432
K1 = 2
K2 = 216
P = (127, 191, 241, 5, 83, 109, 107, 179)


def permutation_1based() -> tuple[int, ...]:
    values = []
    for s in range(1, K + 1):
        m = (s - 1) % 2
        j = (s - 1) // 2
        c = (127 * j + 21 * m) % 216
        values.append(2 * (c + 1) - m)
    result = tuple(values)
    if tuple(sorted(result)) != tuple(range(1, K + 1)):
        raise AssertionError("Link ID 11 interleaver is not a permutation")
    return result


PERMUTATION_1BASED = permutation_1based()
PERMUTATION_ZERO_BASED = tuple(value - 1 for value in PERMUTATION_1BASED)


def general_permutation_zero_based(
    block_length: int, k1: int, k2: int, primes: tuple[int, ...]
) -> tuple[int, ...]:
    """공통 §4 official general internal interleaver (`pi(s)`), zero-based 순열."""

    if block_length != k1 * k2 or k1 % 2 or len(primes) != 8:
        raise ValueError("general interleaver requires K=k1*k2, even k1 and eight primes")
    half = k1 // 2
    values = []
    for s in range(1, block_length + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = (s - 1) // 2 - i * k2
        t = (19 * i + 1) % half
        q = (t % 8) + 1
        c = (primes[q - 1] * j + 21 * m) % k2
        values.append(2 * (t + half * c + 1) - m)
    if tuple(sorted(values)) != tuple(range(1, block_length + 1)):
        raise AssertionError("general interleaver is not a permutation")
    return tuple(value - 1 for value in values)


def interleave(values) -> tuple:
    source = tuple(values)
    if len(source) != K:
        raise ValueError(f"Link ID 11 interleaver requires {K} values")
    return tuple(source[index] for index in PERMUTATION_ZERO_BASED)


def deinterleave(values) -> tuple:
    source = tuple(values)
    if len(source) != K:
        raise ValueError(f"Link ID 11 deinterleaver requires {K} values")
    restored = [None] * K
    for output_index, input_index in enumerate(PERMUTATION_ZERO_BASED):
        restored[input_index] = source[output_index]
    return tuple(restored)
