#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Turbo FEC Encoder for GNU Radio (Link IDs 11-19)
=========================================================

Standards basis
---------------
ITU-R Recommendation M.2092-2 (02/2026), especially:
  - Annex 2, A2-1.2.4 Forward error correction
  - Table 4: interleaver / puncturing parameters
  - Table 5: puncturing patterns for data bit periods
  - Table 6: puncturing / repetition patterns for tail bit periods
  - Table 8: VDE-TER link identification parameters

Scope
-----
This module implements ONE Turbo FEC encoder core that supports VDE-TER
Link IDs 11 through 19.  The selected Link ID determines the exact
information-block length K, interleaver parameters, code rate, data
puncturing pattern, and tail puncturing pattern.

IMPORTANT: a Link ID does NOT accept an arbitrary information-block length.
The standard fixes K for every Link ID.  Upstream packet construction must
therefore form the VDE packet (message(s) + zero padding + CRC) to the exact K
bits required by the selected Link ID before this FEC encoder is called.

GNU Radio interface
-------------------
When GNU Radio is available, the module exposes class ``blk`` as a message/PDU
block.

  message input port : "in"
  message output port: "out"

Input PDU payload:
  PMT u8vector of *unpacked* bits, one item per bit, values only 0 or 1.

Output PDU payload:
  PMT u8vector containing the punctured Turbo codeword followed by the
  punctured trellis-termination (FEC tail) bits.

The output intentionally EXCLUDES the separate physical-layer padding bits
listed in Table 8.  Those padding bits belong to the later PL/burst builder,
not to the Turbo encoder itself.  Profile metadata below records the number
of such PL padding bits so a downstream block can validate the complete
channel-bit count.

The block does NOT perform:
  - Type 92 / Type 93 message construction
  - packet zero-padding to K
  - CRC-32 generation
  - scrambling
  - syncword or Link-ID insertion
  - modulation / pulse shaping
  - Table-8 physical-layer padding insertion

Input bit ordering
------------------
The Turbo input ``u`` is expected in transmission/FEC order.  For byte-oriented
packet data, M.2092-2 specifies MSB-first within each byte at the FEC input.

Standalone use
--------------
The core can be used without GNU Radio:

    coded = turbo_encode(bits, link_id=11)

Run this file directly to execute internal consistency tests for all Link IDs
11-19.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


# =============================================================================
# Standard profile data: ITU-R M.2092-2 Tables 4 and 8
# =============================================================================

@dataclass(frozen=True)
class FECProfile:
    link_id: int
    nominal_rate: str
    information_bits: int          # K, Turbo information block length
    k1: int
    k2: int
    primes: Tuple[int, int, int, int, int, int, int, int]
    puncture_id: str               # Table 5
    tail_id: str                   # Table 6
    fec_main_bits: int             # Table 8: "FEC decoder input bits"
    fec_tail_bits: int             # Table 8: FEC tail portion
    pl_padding_bits: int           # Table 8: separate PL padding portion
    channel_bits: int              # Table 8 total channel bits

    @property
    def fec_output_bits(self) -> int:
        """Turbo FEC output produced by this module: main + tail."""
        return self.fec_main_bits + self.fec_tail_bits


_COMMON_PRIMES = (31, 37, 43, 47, 53, 59, 61, 67)

PROFILES: Dict[int, FECProfile] = {
    11: FECProfile(
        11, "1/2", 432, 2, 216,
        (127, 191, 241, 5, 83, 109, 107, 179),
        "6", "6a", 864, 10, 0, 874,
    ),
    12: FECProfile(
        12, "3/4", 972, 2, 486,
        _COMMON_PRIMES,
        "8", "8", 1296, 12, 3, 1311,
    ),
    13: FECProfile(
        13, "3/4", 1296, 2, 648,
        _COMMON_PRIMES,
        "8", "8", 1728, 12, 8, 1748,
    ),
    14: FECProfile(
        14, "1/2", 896, 2, 448,
        _COMMON_PRIMES,
        "6", "6", 1792, 12, 30, 1834,
    ),
    15: FECProfile(
        15, "3/4", 2016, 4, 504,
        _COMMON_PRIMES,
        "8", "8", 2688, 12, 51, 2751,
    ),
    16: FECProfile(
        16, "3/4", 2688, 4, 672,
        _COMMON_PRIMES,
        "8", "8", 3584, 12, 72, 3668,
    ),
    17: FECProfile(
        17, "1/2", 1872, 6, 312,
        (211, 61, 227, 239, 181, 79, 73, 193),
        "6", "6a", 3744, 10, 0, 3754,
    ),
    18: FECProfile(
        18, "3/4", 4032, 4, 1008,
        _COMMON_PRIMES,
        "8", "8", 5376, 12, 243, 5631,
    ),
    19: FECProfile(
        19, "3/4", 5616, 16, 351,
        (137, 101, 223, 41, 67, 131, 61, 47),
        "8", "8", 7488, 12, 8, 7508,
    ),
}


# =============================================================================
# ITU-R M.2092-2 Table 5: data-period puncturing patterns
# Order in each row is [X, Y0, Y1, X', Y0', Y1'].
# Rows repeat cyclically for the first K clocks.
# =============================================================================

DATA_PUNCTURE_PATTERNS: Dict[str, Tuple[Tuple[int, ...], ...]] = {
    # Puncturing ID 6, nominal code rate 1/2
    "6": (
        (1, 1, 0, 0, 0, 0),
        (1, 0, 0, 0, 1, 0),
    ),

    # Puncturing ID 8, nominal code rate 3/4
    "8": (
        (1, 0, 1, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 1),
    ),
}


# =============================================================================
# ITU-R M.2092-2 Table 6: tail-period puncturing/repetition patterns
# Six rows correspond to the six termination clocks:
#   rows 0-2 -> RSC #1 only
#   rows 3-5 -> RSC #2 only
# Order in each row is [X, Y0, Y1, X', Y0', Y1'].
# =============================================================================

TAIL_PUNCTURE_PATTERNS: Dict[str, Tuple[Tuple[int, ...], ...]] = {
    # Tail ID 6, nominal code rate 1/2, 12 transmitted tail bits
    "6": (
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
    ),

    # Tail ID 6a, nominal code rate 1/2, 10 transmitted tail bits
    "6a": (
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 0, 0),
    ),

    # Tail ID 8, nominal code rate 3/4, 12 transmitted tail bits
    "8": (
        (1, 0, 1, 0, 0, 0),
        (1, 0, 1, 0, 0, 0),
        (1, 0, 1, 0, 0, 0),
        (0, 0, 0, 1, 0, 1),
        (0, 0, 0, 1, 0, 1),
        (0, 0, 0, 1, 0, 1),
    ),
}


# =============================================================================
# Validation / utility helpers
# =============================================================================

def get_profile(link_id: int) -> FECProfile:
    """Return the immutable standard profile for Link ID 11-19."""
    try:
        return PROFILES[int(link_id)]
    except (KeyError, ValueError):
        raise ValueError(
            f"Unsupported Link ID {link_id!r}. Supported Link IDs: "
            f"{sorted(PROFILES)}"
        ) from None


def _validate_bits(bits: Sequence[int], expected_len: int) -> List[int]:
    """
    Validate an unpacked 0/1 bit vector and enforce the standard K exactly.
    """
    out = [int(b) for b in bits]

    if len(out) != expected_len:
        raise ValueError(
            f"Invalid FEC input length: got {len(out)} bits, expected "
            f"exactly {expected_len} bits for the selected Link ID. "
            "Build/pad the VDE packet to the standard information-block "
            "length before FEC encoding."
        )

    if any(b not in (0, 1) for b in out):
        raise ValueError(
            "FEC input must be an unpacked bit vector containing only 0 or 1."
        )

    return out


def bytes_to_msb_bits(data: bytes) -> List[int]:
    """
    Convenience helper: unpack bytes MSB-first, matching the M.2092-2 FEC
    input convention for byte-oriented packet data.
    """
    result: List[int] = []
    for value in data:
        result.extend((value >> shift) & 1 for shift in range(7, -1, -1))
    return result


# =============================================================================
# Turbo interleaver: ITU-R M.2092-2 A2-1.2.4.3
# =============================================================================

def _build_permutation(profile: FECProfile) -> List[int]:
    """
    Build the standard permutation pi(s), returned as zero-based indices.

        interleaved[s - 1] = input_bits[pi(s) - 1]
    """
    k1 = int(profile.k1)
    k2 = int(profile.k2)
    K = int(profile.information_bits)
    primes = profile.primes

    if k1 <= 0 or k2 <= 0 or k1 * k2 != K:
        raise RuntimeError(
            f"Invalid profile factorization for Link ID {profile.link_id}: "
            f"{k1} * {k2} != {K}."
        )

    if k1 % 2 != 0:
        raise RuntimeError("k1 must be even for the specified interleaver formula.")

    half_k1 = k1 // 2
    permutation: List[int] = []

    for s in range(1, K + 1):
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = ((s - 1) // 2) - i * k2
        t = (19 * i + 1) % half_k1
        q = (t % 8) + 1
        c = (primes[q - 1] * j + 21 * m) % k2
        pi_s = 2 * (t + c * half_k1 + 1) - m
        permutation.append(int(pi_s) - 1)

    # A valid interleaver must be a bijection over 0..K-1.
    if sorted(permutation) != list(range(K)):
        raise RuntimeError(
            f"Generated interleaver for Link ID {profile.link_id} is not bijective."
        )

    return permutation


def _interleave(bits: Sequence[int], permutation: Sequence[int]) -> List[int]:
    return [int(bits[index]) for index in permutation]


# =============================================================================
# Constituent 8-state RSC encoder: ITU-R M.2092-2 A2-1.2.4.2
#
# Transfer function:
#   G(D) = [1, n0(D)/d(D), n1(D)/d(D)]
#   n0(D) = 1 + D + D^3
#   n1(D) = 1 + D + D^2 + D^3
#   d(D)  = 1 + D^2 + D^3
#
# With state [D0,D1,D2] = [v(t-1),v(t-2),v(t-3)]:
#   v(t) = u(t) XOR D1 XOR D2
#   X    = u(t)
#   Y0   = v(t) XOR D0 XOR D2
#   Y1   = v(t) XOR D0 XOR D1 XOR D2
# =============================================================================

def _rsc_encode(
    bits: Sequence[int],
) -> Tuple[List[int], List[int], List[int], List[int], List[int], List[int]]:
    """
    Encode one constituent RSC branch and generate its 3 termination clocks.

    Returns:
        X, Y0, Y1, X_tail, Y0_tail, Y1_tail
    """
    D0 = D1 = D2 = 0

    X: List[int] = []
    Y0: List[int] = []
    Y1: List[int] = []

    for raw_bit in bits:
        u = int(raw_bit) & 1

        v = u ^ D1 ^ D2
        x = u
        y0 = v ^ D0 ^ D2
        y1 = v ^ D0 ^ D1 ^ D2

        X.append(x)
        Y0.append(y0)
        Y1.append(y1)

        D0, D1, D2 = v, D0, D1

    # Three termination clocks.  Choosing u_tail = D1 XOR D2 makes the
    # recursive node v equal zero on each clock, so the 3-memory state shifts
    # to 000 after exactly three clocks.
    X_tail: List[int] = []
    Y0_tail: List[int] = []
    Y1_tail: List[int] = []

    for _ in range(3):
        u_tail = D1 ^ D2
        v = 0

        x_tail = u_tail
        y0_tail = v ^ D0 ^ D2
        y1_tail = v ^ D0 ^ D1 ^ D2

        X_tail.append(x_tail)
        Y0_tail.append(y0_tail)
        Y1_tail.append(y1_tail)

        D0, D1, D2 = v, D0, D1

    if (D0, D1, D2) != (0, 0, 0):
        raise RuntimeError("RSC trellis termination failed to return to state 000.")

    return X, Y0, Y1, X_tail, Y0_tail, Y1_tail


# =============================================================================
# Rate adaptation / puncturing
# =============================================================================

def _apply_data_puncture(
    streams: Sequence[Sequence[int]],
    pattern: Sequence[Sequence[int]],
) -> List[int]:
    """
    Apply Table-5 data puncturing for the first K clocks.

    streams order: [X, Y0, Y1, X', Y0', Y1']
    The table is read left-to-right within a clock and then top-to-bottom
    across successive clock rows; the pattern is repeated cyclically.
    """
    if len(streams) != 6:
        raise ValueError("Exactly six Turbo output streams are required.")

    K = len(streams[0])
    if any(len(stream) != K for stream in streams):
        raise ValueError("All six Turbo streams must have the same length.")

    if not pattern:
        raise ValueError("Puncturing pattern cannot be empty.")

    output: List[int] = []

    for clock in range(K):
        row = pattern[clock % len(pattern)]
        if len(row) != 6:
            raise ValueError("Every data puncturing row must contain six entries.")

        for stream_index, repeat_count in enumerate(row):
            count = int(repeat_count)
            if count < 0:
                raise ValueError("Puncturing/repetition counts cannot be negative.")
            if count:
                output.extend([int(streams[stream_index][clock])] * count)

    return output


def _apply_tail_puncture(
    tail1: Tuple[Sequence[int], Sequence[int], Sequence[int]],
    tail2: Tuple[Sequence[int], Sequence[int], Sequence[int]],
    pattern: Sequence[Sequence[int]],
) -> List[int]:
    """
    Apply the Table-6 pattern to the six termination clocks.

    Clocks 0..2 emit only RSC #1 sources X,Y0,Y1.
    Clocks 3..5 emit only RSC #2 sources X',Y0',Y1'.
    """
    if len(pattern) != 6:
        raise ValueError("Tail puncturing pattern must contain exactly six rows.")

    if any(len(stream) != 3 for stream in tail1 + tail2):
        raise ValueError("Each RSC branch must generate exactly three tail clocks.")

    output: List[int] = []

    # RSC #1 termination clocks.
    for clock in range(3):
        row = pattern[clock]
        if len(row) != 6:
            raise ValueError("Every tail puncturing row must contain six entries.")

        sources = (
            tail1[0][clock], tail1[1][clock], tail1[2][clock],
            0, 0, 0,
        )
        for value, repeat_count in zip(sources, row):
            count = int(repeat_count)
            if count < 0:
                raise ValueError("Puncturing/repetition counts cannot be negative.")
            if count:
                output.extend([int(value)] * count)

    # RSC #2 termination clocks.
    for clock in range(3):
        row = pattern[clock + 3]
        if len(row) != 6:
            raise ValueError("Every tail puncturing row must contain six entries.")

        sources = (
            0, 0, 0,
            tail2[0][clock], tail2[1][clock], tail2[2][clock],
        )
        for value, repeat_count in zip(sources, row):
            count = int(repeat_count)
            if count < 0:
                raise ValueError("Puncturing/repetition counts cannot be negative.")
            if count:
                output.extend([int(value)] * count)

    return output


# =============================================================================
# Public encoder API
# =============================================================================

def turbo_encode(bits: Sequence[int], link_id: int = 11) -> List[int]:
    """
    Encode one complete VDE-TER FEC information block for Link ID 11-19.

    Processing order:
        exact-K input bits
          -> RSC #1
          -> standard interleaver -> RSC #2
          -> Table-5 data puncturing
          -> six trellis-termination clocks + Table-6 tail puncturing
          -> FEC output (main + tail)

    The separate Table-8 physical-layer padding bits are NOT inserted here.
    """
    profile = get_profile(link_id)
    input_bits = _validate_bits(bits, profile.information_bits)

    permutation = _build_permutation(profile)

    # Constituent encoder #1: original information sequence.
    x, y0, y1, xt, y0t, y1t = _rsc_encode(input_bits)

    # Constituent encoder #2: standard-interleaved information sequence.
    interleaved = _interleave(input_bits, permutation)
    xp, y0p, y1p, xpt, y0pt, y1pt = _rsc_encode(interleaved)

    try:
        data_pattern = DATA_PUNCTURE_PATTERNS[profile.puncture_id]
        tail_pattern = TAIL_PUNCTURE_PATTERNS[profile.tail_id]
    except KeyError as exc:
        raise RuntimeError(
            f"Missing puncturing pattern {exc.args[0]!r} for Link ID "
            f"{profile.link_id}."
        ) from None

    main_bits = _apply_data_puncture(
        (x, y0, y1, xp, y0p, y1p),
        data_pattern,
    )

    tail_bits = _apply_tail_puncture(
        (xt, y0t, y1t),
        (xpt, y0pt, y1pt),
        tail_pattern,
    )

    if len(main_bits) != profile.fec_main_bits:
        raise RuntimeError(
            f"Link ID {profile.link_id}: data-puncturing length mismatch: "
            f"got {len(main_bits)}, expected {profile.fec_main_bits}."
        )

    if len(tail_bits) != profile.fec_tail_bits:
        raise RuntimeError(
            f"Link ID {profile.link_id}: FEC-tail length mismatch: "
            f"got {len(tail_bits)}, expected {profile.fec_tail_bits}."
        )

    encoded = main_bits + tail_bits

    if len(encoded) != profile.fec_output_bits:
        raise RuntimeError(
            f"Link ID {profile.link_id}: total FEC length mismatch: "
            f"got {len(encoded)}, expected {profile.fec_output_bits}."
        )

    if any(bit not in (0, 1) for bit in encoded):
        raise RuntimeError("Internal error: encoded output contains a non-binary value.")

    return encoded


# =============================================================================
# GNU Radio Embedded Python Block / Python Block
# =============================================================================

if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """
        VDE-TER Turbo FEC Encoder (PDU), Link IDs 11-19.

        GRC parameters:
            LinkID  : integer 11..19 (default 11)
            verbose : bool

        Input PDU:
            (metadata, PMT u8vector of exact-K unpacked bits)

        Output PDU:
            (same metadata, PMT u8vector of FEC main + FEC tail bits)
        """

        def __init__(self, LinkID=11, verbose=False):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Turbo FEC Encoder 11-19",
                in_sig=None,
                out_sig=None,
            )

            self.LinkID = int(LinkID)
            self.verbose = bool(verbose)
            self.profile = get_profile(self.LinkID)

            self.message_port_register_in(pmt.intern("in"))
            self.message_port_register_out(pmt.intern("out"))
            self.set_msg_handler(pmt.intern("in"), self._handle_pdu)

        def _handle_pdu(self, msg):
            try:
                if not pmt.is_pair(msg):
                    raise ValueError(
                        "Input must be a PDU pair: (metadata, u8vector)."
                    )

                meta = pmt.car(msg)
                payload = pmt.cdr(msg)

                if not pmt.is_u8vector(payload):
                    raise ValueError(
                        "PDU payload must be a PMT u8vector of unpacked 0/1 bits."
                    )

                bits = list(pmt.u8vector_elements(payload))
                encoded = turbo_encode(bits, self.LinkID)

                out_vec = pmt.init_u8vector(len(encoded), encoded)
                out_pdu = pmt.cons(meta, out_vec)
                self.message_port_pub(pmt.intern("out"), out_pdu)

                if self.verbose:
                    p = self.profile
                    print(
                        f"[VDE-TER Turbo FEC] Link ID {p.link_id}: "
                        f"K={len(bits)} -> FEC={len(encoded)} bits "
                        f"({p.fec_main_bits} main + {p.fec_tail_bits} tail); "
                        f"PL padding later={p.pl_padding_bits} bits, "
                        f"channel total={p.channel_bits} bits"
                    )

            except Exception as exc:
                # Do not publish a malformed codeword.  Print a concise error
                # so GNU Radio Companion's terminal shows the exact cause.
                print(f"[VDE-TER Turbo FEC ERROR] {exc}")


# =============================================================================
# Internal consistency tests
# =============================================================================

def _reference_rsc_data(bits: Sequence[int]) -> Tuple[List[int], List[int], List[int]]:
    """
    Independent direct recurrence from the transfer-function polynomials.
    Used only by self-test to cross-check the optimized state implementation.
    """
    v_history = [0, 0, 0]  # [v(t-1), v(t-2), v(t-3)]
    X: List[int] = []
    Y0: List[int] = []
    Y1: List[int] = []

    for raw in bits:
        u = int(raw) & 1
        vm1, vm2, vm3 = v_history
        v = u ^ vm2 ^ vm3

        X.append(u)
        Y0.append(v ^ vm1 ^ vm3)
        Y1.append(v ^ vm1 ^ vm2 ^ vm3)

        v_history = [v, vm1, vm2]

    return X, Y0, Y1


def _self_test() -> None:
    # First cross-check the constituent RSC against a direct polynomial
    # recurrence that is intentionally separate from the main implementation.
    rsc_test = [((i * 13 + 5) ^ (i >> 1) ^ (i >> 3)) & 1 for i in range(257)]
    x, y0, y1, *_ = _rsc_encode(rsc_test)
    rx, ry0, ry1 = _reference_rsc_data(rsc_test)
    assert x == rx and y0 == ry0 and y1 == ry1

    for link_id in sorted(PROFILES):
        p = PROFILES[link_id]

        # Profile arithmetic from Tables 4/8.
        assert p.k1 * p.k2 == p.information_bits
        assert p.fec_main_bits + p.fec_tail_bits + p.pl_padding_bits == p.channel_bits

        # Interleaver must be a full permutation.
        perm = _build_permutation(p)
        assert len(perm) == p.information_bits
        assert sorted(perm) == list(range(p.information_bits))

        # Puncturing-pattern lengths must independently reproduce Table 8.
        data_pattern = DATA_PUNCTURE_PATTERNS[p.puncture_id]
        expected_main_from_pattern = sum(
            sum(data_pattern[i % len(data_pattern)])
            for i in range(p.information_bits)
        )
        assert expected_main_from_pattern == p.fec_main_bits

        tail_pattern = TAIL_PUNCTURE_PATTERNS[p.tail_id]
        expected_tail_from_pattern = sum(sum(row) for row in tail_pattern)
        assert expected_tail_from_pattern == p.fec_tail_bits

        # All-zero input should remain all-zero through a linear binary code.
        zeros = [0] * p.information_bits
        encoded_zeros = turbo_encode(zeros, link_id)
        assert len(encoded_zeros) == p.fec_output_bits
        assert encoded_zeros == [0] * p.fec_output_bits

        # Deterministic non-zero pattern.
        test_bits = [
            ((i * 7 + 3) ^ (i >> 2) ^ (i >> 5)) & 1
            for i in range(p.information_bits)
        ]
        coded = turbo_encode(test_bits, link_id)
        assert len(coded) == p.fec_output_bits
        assert set(coded).issubset({0, 1})

        # Wrong length must be rejected, not silently padded/truncated.
        try:
            turbo_encode(test_bits[:-1], link_id)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Link ID {link_id}: wrong input length was accepted")

        print(
            f"[PASS] Link ID {link_id}: "
            f"K={p.information_bits}, rate={p.nominal_rate}, "
            f"main={p.fec_main_bits}, tail={p.fec_tail_bits}, "
            f"FEC out={p.fec_output_bits}, PL pad={p.pl_padding_bits}, "
            f"channel={p.channel_bits}"
        )

    print("[PASS] All Link ID 11-19 consistency tests completed.")


if __name__ == "__main__":
    _self_test()
