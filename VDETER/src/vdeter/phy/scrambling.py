"""Per-Packet M.2092-2 Link ID 11 data scrambling and soft descrambling."""

from __future__ import annotations

INITIAL_REGISTER_R1_TO_R15 = (1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0)
CHANNEL_BITS = 874


def pn_sequence(length: int = CHANNEL_BITS) -> tuple[int, ...]:
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ValueError("scrambler length must be a non-negative integer")
    register = list(INITIAL_REGISTER_R1_TO_R15)
    output = []
    for _ in range(length):
        pn = register[13] ^ register[14]
        output.append(pn)
        register = [pn] + register[:14]
    return tuple(output)


PN_SEQUENCE_874 = pn_sequence()


def scramble(bits) -> tuple[int, ...]:
    source = tuple(bits)
    if len(source) != CHANNEL_BITS or any(bit not in (0, 1) for bit in source):
        raise ValueError("Link ID 11 scrambler requires exactly 874 bits")
    return tuple(bit ^ pn for bit, pn in zip(source, PN_SEQUENCE_874))


def descramble_bits(bits) -> tuple[int, ...]:
    return scramble(bits)


def descramble_llrs(llrs) -> tuple[float, ...]:
    source = tuple(float(value) for value in llrs)
    if len(source) != CHANNEL_BITS:
        raise ValueError("Link ID 11 soft descrambler requires exactly 874 LLRs")
    return tuple(-value if pn else value for value, pn in zip(source, PN_SEQUENCE_874))
