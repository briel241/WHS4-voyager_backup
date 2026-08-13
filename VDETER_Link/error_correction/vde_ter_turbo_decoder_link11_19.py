#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VDE-TER Turbo FEC Decoder for GNU Radio (Link IDs 11-19)
=========================================================

Standards basis
---------------
ITU-R Recommendation M.2092-2 (02/2026), especially the VDE-TER FEC
specification in Annex 2:
  - constituent 8-state RSC code
  - Turbo interleaver definition / Link-ID-specific parameters
  - data-period puncturing patterns
  - trellis-termination (FEC-tail) puncturing patterns
  - Link ID 11..19 FEC block sizes

This file is the receive-side counterpart of:
    vde_ter_turbo_encoder_link11_19.py

IMPORTANT ABOUT "STANDARD COMPLIANCE"
-------------------------------------
M.2092-2 specifies the transmitted Turbo code, interleaver, puncturing and
termination.  It does not require one particular iterative SISO decoding
algorithm or one fixed iteration count.  This implementation therefore uses
an iterative BCJR-family decoder while keeping the transmitted-code structure
exactly matched to M.2092-2:

  Algorithm="logmap"  -> Log-MAP / BCJR in the log domain (default)
  Algorithm="maxlog"  -> Max-Log-MAP approximation (faster/simpler)

The decoder consumes the FEC bits produced by the companion encoder:
    [punctured Turbo data bits][punctured FEC tail bits]

It intentionally does NOT consume the separate physical-layer padding bits
shown in M.2092-2 Table 8.  Remove those PL padding bits in the PHY/burst
parser before this decoder.

GNU Radio interface
-------------------
When GNU Radio is available, class ``blk`` is exposed as a PDU message block.

  message input port : "in"
  message output port: "out"

Accepted input PDU payloads:

1) PMT u8vector (hard bits)
   - one unpacked coded bit per item
   - values must be only 0 or 1
   - converted internally to LLRs using HardLLR

2) PMT f32vector / f64vector (soft LLRs; preferred after demodulation)
   - positive LLR => bit 0 is more likely
   - negative LLR => bit 1 is more likely
   - zero         => erasure / no information

Output PDU payload:
  PMT u8vector containing exactly K decoded information bits (0/1), where K
  is fixed by the selected Link ID.

The output is the complete VDE packet information block that was supplied to
FEC encoding upstream, e.g. message(s) + packet padding + CRC.  CRC checking
and VDE-message parsing belong downstream of this decoder.

Standalone use
--------------
Hard-bit decoding:

    decoded = turbo_decode_hard(coded_bits, link_id=11)

Soft-LLR decoding:

    decoded = turbo_decode_llr(llrs, link_id=11)

Run this file directly to execute structural self-tests.  If the companion
encoder file is present in the same directory, an end-to-end encode/decode
round-trip test is also run for all Link IDs 11..19.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple, Union
import importlib.util
import sys

import numpy as np

try:
    import pmt
    from gnuradio import gr
    _GNURADIO_AVAILABLE = True
except ImportError:
    pmt = None
    gr = None
    _GNURADIO_AVAILABLE = False


# =============================================================================
# M.2092-2 profile data for VDE-TER Link IDs 11..19
# =============================================================================

@dataclass(frozen=True)
class FECProfile:
    link_id: int
    nominal_rate: str
    information_bits: int          # K, decoded Turbo information block
    k1: int
    k2: int
    primes: Tuple[int, int, int, int, int, int, int, int]
    puncture_id: str               # data-period puncturing pattern
    tail_id: str                   # trellis-termination pattern
    fec_main_bits: int             # punctured Turbo data bits at decoder input
    fec_tail_bits: int             # punctured FEC-tail bits at decoder input
    pl_padding_bits: int           # separate PHY padding; NOT decoder input
    channel_bits: int              # FEC main + FEC tail + PL padding

    @property
    def fec_input_bits(self) -> int:
        """Number of coded bits consumed by this decoder."""
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
# M.2092-2 puncturing patterns
# Order per clock: [X, Y0, Y1, X', Y0', Y1']
# =============================================================================

DATA_PUNCTURE_PATTERNS: Dict[str, Tuple[Tuple[int, ...], ...]] = {
    # Puncturing ID 6, nominal rate 1/2
    "6": (
        (1, 1, 0, 0, 0, 0),
        (1, 0, 0, 0, 1, 0),
    ),

    # Puncturing ID 8, nominal rate 3/4
    "8": (
        (1, 0, 1, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 1),
    ),
}


TAIL_PUNCTURE_PATTERNS: Dict[str, Tuple[Tuple[int, ...], ...]] = {
    # Tail ID 6: 12 transmitted termination bits
    "6": (
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
    ),

    # Tail ID 6a: 10 transmitted termination bits
    "6a": (
        (1, 1, 0, 0, 0, 0),
        (1, 1, 0, 0, 0, 0),
        (1, 0, 0, 0, 0, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 1, 0),
        (0, 0, 0, 1, 0, 0),
    ),

    # Tail ID 8: 12 transmitted termination bits
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
# Basic validation / profile helpers
# =============================================================================

def get_profile(link_id: int) -> FECProfile:
    try:
        return PROFILES[int(link_id)]
    except (KeyError, ValueError):
        raise ValueError(
            f"Unsupported Link ID {link_id!r}. Supported Link IDs: "
            f"{sorted(PROFILES)}"
        ) from None


def _validate_decoder_length(length: int, profile: FECProfile) -> None:
    expected = profile.fec_input_bits
    if length == expected:
        return

    if profile.pl_padding_bits > 0 and length == profile.channel_bits:
        raise ValueError(
            f"Link ID {profile.link_id}: received {length} bits, which matches "
            f"the full Table-8 channel-bit count. This Turbo decoder expects "
            f"only {expected} FEC bits; remove the separate "
            f"{profile.pl_padding_bits} PL padding bits in the PHY/burst "
            "parser before decoding."
        )

    raise ValueError(
        f"Link ID {profile.link_id}: invalid Turbo decoder input length "
        f"{length} bits; expected exactly {expected} bits "
        f"({profile.fec_main_bits} main + {profile.fec_tail_bits} FEC tail)."
    )


def _hard_bits_to_llr(
    coded_bits: Sequence[int],
    profile: FECProfile,
    hard_llr: float,
) -> np.ndarray:
    _validate_decoder_length(len(coded_bits), profile)

    hard_llr = float(hard_llr)
    if not np.isfinite(hard_llr) or hard_llr <= 0.0:
        raise ValueError("HardLLR must be a finite positive number.")

    arr = np.asarray(coded_bits)
    if arr.ndim != 1:
        raise ValueError("Hard decoder input must be a one-dimensional bit vector.")

    # Explicitly reject values other than 0 and 1; do not silently mask them.
    if not np.all((arr == 0) | (arr == 1)):
        raise ValueError("Hard decoder input must contain only unpacked bits 0 or 1.")

    return np.where(arr.astype(np.uint8) == 0, hard_llr, -hard_llr).astype(
        np.float64,
        copy=False,
    )


def _validate_soft_llrs(
    llrs: Sequence[float],
    profile: FECProfile,
) -> np.ndarray:
    _validate_decoder_length(len(llrs), profile)

    arr = np.asarray(llrs, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("Soft decoder input must be a one-dimensional LLR vector.")
    if not np.all(np.isfinite(arr)):
        raise ValueError("Soft decoder input LLRs must all be finite numbers.")
    return arr


# =============================================================================
# Turbo interleaver: same permutation as the companion encoder
# =============================================================================

def _build_permutation(profile: FECProfile) -> np.ndarray:
    """
    Return pi(s) as zero-based indices with the convention:

        interleaved[s] = original[permutation[s]]
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
    permutation = np.empty(K, dtype=np.int64)

    for zero_s in range(K):
        s = zero_s + 1
        m = (s - 1) % 2
        i = (s - 1) // (2 * k2)
        j = ((s - 1) // 2) - i * k2
        t = (19 * i + 1) % half_k1
        q = (t % 8) + 1
        c = (primes[q - 1] * j + 21 * m) % k2
        pi_s = 2 * (t + c * half_k1 + 1) - m
        permutation[zero_s] = int(pi_s) - 1

    if not np.array_equal(np.sort(permutation), np.arange(K, dtype=np.int64)):
        raise RuntimeError(
            f"Generated interleaver for Link ID {profile.link_id} is not bijective."
        )

    return permutation


def _interleave(values: np.ndarray, permutation: np.ndarray) -> np.ndarray:
    return values[permutation]


def _deinterleave(values: np.ndarray, permutation: np.ndarray) -> np.ndarray:
    out = np.empty_like(values)
    out[permutation] = values
    return out


# =============================================================================
# 8-state constituent RSC trellis
#
# Companion encoder transfer function:
#   G(D) = [1, n0(D)/d(D), n1(D)/d(D)]
#   n0(D) = 1 + D + D^3
#   n1(D) = 1 + D + D^2 + D^3
#   d(D)  = 1 + D^2 + D^3
#
# State convention is identical to the encoder:
#   state [D0,D1,D2] = [v(t-1), v(t-2), v(t-3)]
# =============================================================================

_NEXT_STATE = np.empty((8, 2), dtype=np.int8)
_Y0_BIT = np.empty((8, 2), dtype=np.int8)
_Y1_BIT = np.empty((8, 2), dtype=np.int8)

for _state in range(8):
    _D0 = (_state >> 2) & 1
    _D1 = (_state >> 1) & 1
    _D2 = _state & 1

    for _u in (0, 1):
        _v = _u ^ _D1 ^ _D2
        _y0 = _v ^ _D0 ^ _D2
        _y1 = _v ^ _D0 ^ _D1 ^ _D2
        _next = (_v << 2) | (_D0 << 1) | _D1

        _NEXT_STATE[_state, _u] = _next
        _Y0_BIT[_state, _u] = _y0
        _Y1_BIT[_state, _u] = _y1

_SYSTEMATIC_SIGN = np.asarray((1.0, -1.0), dtype=np.float64)
_Y0_SIGN = 1.0 - 2.0 * _Y0_BIT.astype(np.float64)
_Y1_SIGN = 1.0 - 2.0 * _Y1_BIT.astype(np.float64)

# Each next state of a binary rate-1 constituent trellis has two predecessors.
_PREV_STATE = np.empty((8, 2), dtype=np.int8)
_PREV_INPUT = np.empty((8, 2), dtype=np.int8)
_prev_count = np.zeros(8, dtype=np.int8)

for _state in range(8):
    for _u in (0, 1):
        _ns = int(_NEXT_STATE[_state, _u])
        _slot = int(_prev_count[_ns])
        _PREV_STATE[_ns, _slot] = _state
        _PREV_INPUT[_ns, _slot] = _u
        _prev_count[_ns] += 1

if not np.all(_prev_count == 2):
    raise RuntimeError("Internal trellis construction error: predecessor count != 2.")


# =============================================================================
# De-puncturing / LLR placement
# =============================================================================

def _depuncture_llrs(
    received_llrs: np.ndarray,
    profile: FECProfile,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Invert the encoder's serial puncturing order.

    Returns:
      data_streams : shape (6,K), order [X,Y0,Y1,X',Y0',Y1']
      tail1        : shape (3,3), rows [X,Y0,Y1] for RSC #1
      tail2        : shape (3,3), rows [X',Y0',Y1'] for RSC #2

    A punctured location receives LLR=0 (an erasure).  If a pattern ever
    repeats a source bit, independent LLR observations are summed.
    """
    K = profile.information_bits
    data_streams = np.zeros((6, K), dtype=np.float64)
    tail1 = np.zeros((3, 3), dtype=np.float64)
    tail2 = np.zeros((3, 3), dtype=np.float64)

    data_pattern = DATA_PUNCTURE_PATTERNS[profile.puncture_id]
    tail_pattern = TAIL_PUNCTURE_PATTERNS[profile.tail_id]

    index = 0

    for clock in range(K):
        row = data_pattern[clock % len(data_pattern)]
        if len(row) != 6:
            raise RuntimeError("Invalid data puncturing row width.")

        for stream_index, repeat_count in enumerate(row):
            count = int(repeat_count)
            if count < 0:
                raise RuntimeError("Negative puncturing/repetition count.")
            if count:
                end = index + count
                if end > received_llrs.size:
                    raise RuntimeError("De-puncturing consumed beyond decoder input.")
                data_streams[stream_index, clock] += float(
                    np.sum(received_llrs[index:end])
                )
                index = end

    if len(tail_pattern) != 6:
        raise RuntimeError("Tail puncturing pattern must have exactly six rows.")

    # First three termination clocks belong to RSC #1 only.
    for clock in range(3):
        row = tail_pattern[clock]
        for stream_index, repeat_count in enumerate(row):
            count = int(repeat_count)
            if count:
                end = index + count
                if end > received_llrs.size:
                    raise RuntimeError("Tail de-puncturing exceeded decoder input.")
                value = float(np.sum(received_llrs[index:end]))
                index = end
                if stream_index < 3:
                    tail1[stream_index, clock] += value

    # Last three termination clocks belong to RSC #2 only.
    for clock in range(3):
        row = tail_pattern[clock + 3]
        for stream_index, repeat_count in enumerate(row):
            count = int(repeat_count)
            if count:
                end = index + count
                if end > received_llrs.size:
                    raise RuntimeError("Tail de-puncturing exceeded decoder input.")
                value = float(np.sum(received_llrs[index:end]))
                index = end
                if stream_index >= 3:
                    tail2[stream_index - 3, clock] += value

    if index != received_llrs.size:
        raise RuntimeError(
            f"Internal de-puncturing mismatch: consumed {index} of "
            f"{received_llrs.size} LLRs."
        )

    return data_streams, tail1, tail2


# =============================================================================
# Constituent SISO decoder: log-domain BCJR / Max-Log-MAP
# =============================================================================

def _pair_reduce(a: np.ndarray, b: np.ndarray, algorithm: str) -> np.ndarray:
    if algorithm == "maxlog":
        return np.maximum(a, b)
    return np.logaddexp(a, b)


def _vector_reduce(values: np.ndarray, algorithm: str) -> float:
    if algorithm == "maxlog":
        return float(np.max(values))
    return float(np.logaddexp.reduce(values))


def _branch_metrics(
    systematic_plus_apriori: float,
    parity0: float,
    parity1: float,
) -> np.ndarray:
    """Return branch metric matrix gamma[state,input]."""
    return 0.5 * (
        _SYSTEMATIC_SIGN[None, :] * systematic_plus_apriori
        + _Y0_SIGN * parity0
        + _Y1_SIGN * parity1
    )


def _siso_decode(
    systematic_llr: np.ndarray,
    parity0_llr: np.ndarray,
    parity1_llr: np.ndarray,
    apriori_llr: np.ndarray,
    algorithm: str,
) -> np.ndarray:
    """
    Decode one terminated constituent RSC trellis.

    LLR convention throughout this file:
        L = log(P(bit=0) / P(bit=1))

    The trellis begins in state 000 and is forced to finish in state 000.
    The latter is essential because the M.2092-2 encoder transmits explicit
    trellis-termination bits.
    """
    T = systematic_llr.size

    if not (
        parity0_llr.size == T
        and parity1_llr.size == T
        and apriori_llr.size == T
    ):
        raise ValueError("Constituent SISO input arrays must have equal length.")

    alpha = np.full((T + 1, 8), -np.inf, dtype=np.float64)
    beta = np.full((T + 1, 8), -np.inf, dtype=np.float64)

    alpha[0, 0] = 0.0
    beta[T, 0] = 0.0

    # Forward recursion.
    for t in range(T):
        gamma = _branch_metrics(
            float(systematic_llr[t] + apriori_llr[t]),
            float(parity0_llr[t]),
            float(parity1_llr[t]),
        )

        cand0 = (
            alpha[t, _PREV_STATE[:, 0]]
            + gamma[_PREV_STATE[:, 0], _PREV_INPUT[:, 0]]
        )
        cand1 = (
            alpha[t, _PREV_STATE[:, 1]]
            + gamma[_PREV_STATE[:, 1], _PREV_INPUT[:, 1]]
        )
        alpha[t + 1, :] = _pair_reduce(cand0, cand1, algorithm)

        norm = float(np.max(alpha[t + 1, :]))
        if not np.isfinite(norm):
            raise RuntimeError("Forward BCJR recursion lost all valid trellis paths.")
        alpha[t + 1, :] -= norm

    # Backward recursion with forced terminal state 000.
    for t in range(T - 1, -1, -1):
        gamma = _branch_metrics(
            float(systematic_llr[t] + apriori_llr[t]),
            float(parity0_llr[t]),
            float(parity1_llr[t]),
        )

        cand0 = gamma[:, 0] + beta[t + 1, _NEXT_STATE[:, 0]]
        cand1 = gamma[:, 1] + beta[t + 1, _NEXT_STATE[:, 1]]
        beta[t, :] = _pair_reduce(cand0, cand1, algorithm)

        norm = float(np.max(beta[t, :]))
        if not np.isfinite(norm):
            raise RuntimeError("Backward BCJR recursion lost all valid trellis paths.")
        beta[t, :] -= norm

    # A-posteriori input-bit LLRs.
    posterior = np.empty(T, dtype=np.float64)

    for t in range(T):
        gamma = _branch_metrics(
            float(systematic_llr[t] + apriori_llr[t]),
            float(parity0_llr[t]),
            float(parity1_llr[t]),
        )

        metric_u0 = (
            alpha[t, :]
            + gamma[:, 0]
            + beta[t + 1, _NEXT_STATE[:, 0]]
        )
        metric_u1 = (
            alpha[t, :]
            + gamma[:, 1]
            + beta[t + 1, _NEXT_STATE[:, 1]]
        )

        posterior[t] = (
            _vector_reduce(metric_u0, algorithm)
            - _vector_reduce(metric_u1, algorithm)
        )

    if not np.all(np.isfinite(posterior)):
        raise RuntimeError("Constituent decoder produced a non-finite posterior LLR.")

    return posterior


# =============================================================================
# Public Turbo decoder core
# =============================================================================

def _normalize_algorithm(algorithm: str) -> str:
    value = str(algorithm).strip().lower().replace("-", "")
    aliases = {
        "logmap": "logmap",
        "logbcjr": "logmap",
        "bcjr": "logmap",
        "maxlog": "maxlog",
        "maxlogmap": "maxlog",
    }
    try:
        return aliases[value]
    except KeyError:
        raise ValueError(
            "Algorithm must be 'logmap' or 'maxlog' "
            "(aliases: bcjr, logbcjr, maxlogmap)."
        ) from None


def _turbo_decode_from_llrs(
    received_llrs: np.ndarray,
    profile: FECProfile,
    iterations: int,
    algorithm: str,
    llr_clip: float,
    early_stop: bool,
) -> Tuple[List[int], np.ndarray, int]:
    iterations = int(iterations)
    if iterations < 1:
        raise ValueError("Iterations must be >= 1.")

    algorithm = _normalize_algorithm(algorithm)

    llr_clip = float(llr_clip)
    if not np.isfinite(llr_clip) or llr_clip <= 0.0:
        raise ValueError("LLRClip must be a finite positive number.")

    data, tail1, tail2 = _depuncture_llrs(received_llrs, profile)
    K = profile.information_bits
    permutation = _build_permutation(profile)

    # -------------------------------------------------------------------------
    # Systematic-channel evidence
    # -------------------------------------------------------------------------
    # X carries the original information sequence.  X' is the systematic
    # sequence of the second constituent encoder and therefore corresponds to
    # the interleaved original sequence.  The current Link-ID 11..19 data
    # patterns puncture X', but combining it generically here makes the inverse
    # mapping unambiguous and future-proof for a pattern that transmits it.
    systematic_original = data[0, :].copy()
    systematic_original[permutation] += data[3, :]

    # Constituent #1 arrays: K data clocks + 3 branch-1 termination clocks.
    sys1 = np.concatenate((systematic_original, tail1[0, :]))
    par10 = np.concatenate((data[1, :], tail1[1, :]))
    par11 = np.concatenate((data[2, :], tail1[2, :]))

    # Constituent #2 sees the same information in interleaved order, followed
    # by its own independent three termination clocks.
    sys2_data = _interleave(systematic_original, permutation)
    sys2 = np.concatenate((sys2_data, tail2[0, :]))
    par20 = np.concatenate((data[4, :], tail2[1, :]))
    par21 = np.concatenate((data[5, :], tail2[2, :]))

    apriori1 = np.zeros(K, dtype=np.float64)
    final_posterior_original = np.zeros(K, dtype=np.float64)
    previous_hard: Union[np.ndarray, None] = None
    iterations_used = 0

    for iteration in range(iterations):
        # Decoder 1 (original order).
        posterior1 = _siso_decode(
            sys1,
            par10,
            par11,
            np.concatenate((apriori1, np.zeros(3, dtype=np.float64))),
            algorithm,
        )

        # Extrinsic LLR excludes both systematic-channel and a-priori terms.
        extrinsic1 = posterior1[:K] - systematic_original - apriori1
        extrinsic1 = np.clip(extrinsic1, -llr_clip, llr_clip)

        # Decoder 2 (interleaved order).
        apriori2 = _interleave(extrinsic1, permutation)
        posterior2 = _siso_decode(
            sys2,
            par20,
            par21,
            np.concatenate((apriori2, np.zeros(3, dtype=np.float64))),
            algorithm,
        )

        extrinsic2 = posterior2[:K] - sys2_data - apriori2
        extrinsic2 = np.clip(extrinsic2, -llr_clip, llr_clip)

        # Feed decoder-2 extrinsic information back to decoder 1.
        apriori1 = _deinterleave(extrinsic2, permutation)
        final_posterior_original = _deinterleave(
            posterior2[:K],
            permutation,
        )

        iterations_used = iteration + 1

        # Stability stopping is optional and deliberately disabled by default.
        # Without a CRC decision inside this block, "unchanged hard decisions"
        # are only a heuristic and are not a proof of successful decoding.
        if early_stop:
            hard_now = final_posterior_original < 0.0
            if previous_hard is not None and np.array_equal(hard_now, previous_hard):
                break
            previous_hard = hard_now.copy()

    decoded = (final_posterior_original < 0.0).astype(np.uint8).tolist()

    if len(decoded) != K:
        raise RuntimeError(
            f"Internal decoder output-length mismatch: got {len(decoded)}, expected {K}."
        )

    return decoded, final_posterior_original, iterations_used


def turbo_decode_llr(
    llrs: Sequence[float],
    link_id: int = 11,
    iterations: int = 8,
    algorithm: str = "logmap",
    llr_clip: float = 60.0,
    early_stop: bool = False,
) -> List[int]:
    """
    Decode one VDE-TER Turbo FEC block from soft bit LLRs.

    LLR convention:
        positive -> coded bit 0 is more likely
        negative -> coded bit 1 is more likely

    ``llrs`` must contain exactly the FEC main + FEC tail bits produced by the
    companion encoder.  Separate Table-8 physical-layer padding is not part of
    this input.
    """
    profile = get_profile(link_id)
    received = _validate_soft_llrs(llrs, profile)
    decoded, _, _ = _turbo_decode_from_llrs(
        received,
        profile,
        iterations,
        algorithm,
        llr_clip,
        bool(early_stop),
    )
    return decoded


def turbo_decode_hard(
    coded_bits: Sequence[int],
    link_id: int = 11,
    iterations: int = 8,
    algorithm: str = "logmap",
    hard_llr: float = 4.0,
    llr_clip: float = 60.0,
    early_stop: bool = False,
) -> List[int]:
    """
    Decode one VDE-TER Turbo FEC block from hard 0/1 coded bits.

    For a real radio receiver, soft LLR input is strongly preferred because a
    Turbo decoder can exploit reliability information that hard slicing loses.
    """
    profile = get_profile(link_id)
    received = _hard_bits_to_llr(coded_bits, profile, hard_llr)
    decoded, _, _ = _turbo_decode_from_llrs(
        received,
        profile,
        iterations,
        algorithm,
        llr_clip,
        bool(early_stop),
    )
    return decoded


def turbo_decode(
    values: Sequence[Union[int, float]],
    link_id: int = 11,
    input_mode: str = "hard",
    iterations: int = 8,
    algorithm: str = "logmap",
    hard_llr: float = 4.0,
    llr_clip: float = 60.0,
    early_stop: bool = False,
) -> List[int]:
    """Convenience wrapper selecting hard-bit or soft-LLR decoding."""
    mode = str(input_mode).strip().lower()
    if mode in ("hard", "bits", "bit"):
        return turbo_decode_hard(
            values,
            link_id=link_id,
            iterations=iterations,
            algorithm=algorithm,
            hard_llr=hard_llr,
            llr_clip=llr_clip,
            early_stop=early_stop,
        )
    if mode in ("soft", "llr", "llrs"):
        return turbo_decode_llr(
            values,
            link_id=link_id,
            iterations=iterations,
            algorithm=algorithm,
            llr_clip=llr_clip,
            early_stop=early_stop,
        )
    raise ValueError("input_mode must be 'hard' or 'soft'.")


# =============================================================================
# GNU Radio Embedded Python Block / Python Block
# =============================================================================

if _GNURADIO_AVAILABLE:

    class blk(gr.basic_block):
        """
        VDE-TER Turbo FEC Decoder (PDU), Link IDs 11..19.

        GRC parameters:
            LinkID     : integer 11..19
            Iterations : integer >= 1, default 8
            Algorithm  : "logmap" or "maxlog"
            HardLLR    : confidence used only for u8 hard-bit input
            LLRClip    : extrinsic-LLR clipping magnitude
            EarlyStop  : optional hard-decision stability heuristic
            verbose    : bool

        Input PDU payload:
            - PMT u8vector: hard 0/1 FEC bits
            - PMT f32vector/f64vector: soft LLRs

        Output PDU payload:
            PMT u8vector: K decoded information bits
        """

        def __init__(
            self,
            LinkID=11,
            Iterations=8,
            Algorithm="logmap",
            HardLLR=4.0,
            LLRClip=60.0,
            EarlyStop=False,
            verbose=False,
        ):
            gr.basic_block.__init__(
                self,
                name="VDE-TER Turbo FEC Decoder 11-19",
                in_sig=None,
                out_sig=None,
            )

            self.LinkID = int(LinkID)
            self.Iterations = int(Iterations)
            self.Algorithm = _normalize_algorithm(Algorithm)
            self.HardLLR = float(HardLLR)
            self.LLRClip = float(LLRClip)
            self.EarlyStop = bool(EarlyStop)
            self.verbose = bool(verbose)
            self.profile = get_profile(self.LinkID)

            if self.Iterations < 1:
                raise ValueError("Iterations must be >= 1.")
            if not np.isfinite(self.HardLLR) or self.HardLLR <= 0.0:
                raise ValueError("HardLLR must be a finite positive number.")
            if not np.isfinite(self.LLRClip) or self.LLRClip <= 0.0:
                raise ValueError("LLRClip must be a finite positive number.")

            self.message_port_register_in(pmt.intern("in"))
            self.message_port_register_out(pmt.intern("out"))
            self.set_msg_handler(pmt.intern("in"), self._handle_pdu)

        def _handle_pdu(self, msg):
            try:
                if not pmt.is_pair(msg):
                    raise ValueError("Input must be a PDU pair: (metadata, vector).")

                meta = pmt.car(msg)
                payload = pmt.cdr(msg)

                input_kind = None

                if pmt.is_u8vector(payload):
                    values = list(pmt.u8vector_elements(payload))
                    received = _hard_bits_to_llr(
                        values,
                        self.profile,
                        self.HardLLR,
                    )
                    input_kind = "hard"

                elif hasattr(pmt, "is_f32vector") and pmt.is_f32vector(payload):
                    values = list(pmt.f32vector_elements(payload))
                    received = _validate_soft_llrs(values, self.profile)
                    input_kind = "soft-f32"

                elif hasattr(pmt, "is_f64vector") and pmt.is_f64vector(payload):
                    values = list(pmt.f64vector_elements(payload))
                    received = _validate_soft_llrs(values, self.profile)
                    input_kind = "soft-f64"

                else:
                    raise ValueError(
                        "PDU payload must be a PMT u8vector of hard 0/1 bits "
                        "or an f32/f64 vector of soft LLRs."
                    )

                decoded, posterior, used = _turbo_decode_from_llrs(
                    received,
                    self.profile,
                    self.Iterations,
                    self.Algorithm,
                    self.LLRClip,
                    self.EarlyStop,
                )

                out_vec = pmt.init_u8vector(len(decoded), decoded)
                self.message_port_pub(pmt.intern("out"), pmt.cons(meta, out_vec))

                if self.verbose:
                    min_reliability = float(np.min(np.abs(posterior)))
                    p = self.profile
                    print(
                        f"[VDE-TER Turbo Decoder] Link ID {p.link_id}: "
                        f"FEC={len(received)} -> K={len(decoded)} bits, "
                        f"input={input_kind}, algorithm={self.Algorithm}, "
                        f"iterations={used}/{self.Iterations}, "
                        f"min|posterior LLR|={min_reliability:.3f}"
                    )

            except Exception as exc:
                # Never publish a guessed/malformed decoded packet.
                print(f"[VDE-TER Turbo Decoder ERROR] {exc}")


# =============================================================================
# Self-tests
# =============================================================================

def _structural_self_test() -> None:
    for link_id in sorted(PROFILES):
        p = PROFILES[link_id]

        assert p.k1 * p.k2 == p.information_bits
        assert p.fec_input_bits + p.pl_padding_bits == p.channel_bits

        perm = _build_permutation(p)
        assert perm.size == p.information_bits
        assert np.array_equal(np.sort(perm), np.arange(p.information_bits))

        data_pattern = DATA_PUNCTURE_PATTERNS[p.puncture_id]
        data_count = sum(
            sum(data_pattern[i % len(data_pattern)])
            for i in range(p.information_bits)
        )
        assert data_count == p.fec_main_bits

        tail_pattern = TAIL_PUNCTURE_PATTERNS[p.tail_id]
        tail_count = sum(sum(row) for row in tail_pattern)
        assert tail_count == p.fec_tail_bits

        print(
            f"[PASS] structural Link ID {link_id}: "
            f"FEC in={p.fec_input_bits}, K={p.information_bits}, "
            f"PL pad={p.pl_padding_bits}, channel={p.channel_bits}"
        )


def _try_round_trip_with_companion_encoder() -> None:
    encoder_path = Path(__file__).with_name("vde_ter_turbo_encoder_link11_19.py")
    if not encoder_path.exists():
        print(
            "[SKIP] Companion encoder not found beside decoder; "
            "round-trip self-test skipped."
        )
        return

    spec = importlib.util.spec_from_file_location(
        "_vde_ter_encoder_for_decoder_selftest",
        str(encoder_path),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load companion encoder for self-test.")

    encoder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = encoder
    spec.loader.exec_module(encoder)

    for link_id in sorted(PROFILES):
        p = PROFILES[link_id]

        # Cross-check that both independently stored profile descriptions agree.
        ep = encoder.get_profile(link_id)
        assert ep.information_bits == p.information_bits
        assert ep.k1 == p.k1 and ep.k2 == p.k2
        assert tuple(ep.primes) == tuple(p.primes)
        assert ep.puncture_id == p.puncture_id
        assert ep.tail_id == p.tail_id
        assert ep.fec_main_bits == p.fec_main_bits
        assert ep.fec_tail_bits == p.fec_tail_bits
        assert ep.pl_padding_bits == p.pl_padding_bits
        assert ep.channel_bits == p.channel_bits

        original = [
            ((i * 7 + 3) ^ (i >> 2) ^ (i >> 5)) & 1
            for i in range(p.information_bits)
        ]
        coded = encoder.turbo_encode(original, link_id)

        decoded = turbo_decode_hard(
            coded,
            link_id=link_id,
            iterations=4,
            algorithm="logmap",
            hard_llr=4.0,
            early_stop=False,
        )

        assert decoded == original, f"Round-trip failed for Link ID {link_id}"
        print(
            f"[PASS] round-trip Link ID {link_id}: "
            f"K={len(original)} -> FEC={len(coded)} -> K={len(decoded)}"
        )


if __name__ == "__main__":
    _structural_self_test()
    _try_round_trip_with_companion_encoder()
    print("[PASS] VDE-TER Turbo decoder self-tests completed.")
