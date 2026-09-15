"""Forward-error-correction package boundary."""
"""Link ID 11 Turbo FEC primitives."""

from .interleaver import deinterleave, interleave, permutation_1based
from .puncturing import puncture, rate_recover
from .turbo import decode, encode, rsc_step, termination_inputs

__all__ = (
    "decode", "deinterleave", "encode", "interleave", "permutation_1based",
    "puncture", "rate_recover", "rsc_step", "termination_inputs",
)
