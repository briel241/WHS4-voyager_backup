"""Approved parallel RSC Turbo encoder and deterministic 8-iteration Log-MAP decoder."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .interleaver import K, deinterleave, interleave
from .puncturing import puncture, rate_recover


def state_bits(state_id: int) -> tuple[int, int, int]:
    if isinstance(state_id, bool) or not isinstance(state_id, int) or not 0 <= state_id <= 7:
        raise ValueError("RSC state must be 0..7")
    return ((state_id >> 2) & 1, (state_id >> 1) & 1, state_id & 1)


def state_id(bits: tuple[int, int, int]) -> int:
    d1, d2, d3 = bits
    return (d1 << 2) | (d2 << 1) | d3


def rsc_step(current_state: int, bit: int) -> tuple[int, tuple[int, int, int]]:
    if bit not in (0, 1):
        raise ValueError("RSC input must be a bit")
    d1, d2, d3 = state_bits(current_state)
    feedback = bit ^ d2 ^ d3
    x = bit
    y0 = feedback ^ d1 ^ d3
    y1 = feedback ^ d1 ^ d2 ^ d3
    return state_id((feedback, d1, d2)), (x, y0, y1)


def rsc_tail(current_state: int) -> tuple[int, tuple[int, int, int]]:
    d1, d2, d3 = state_bits(current_state)
    return rsc_step(current_state, d2 ^ d3)


RSC_TRANSITIONS = tuple(
    tuple(rsc_step(state, bit) for bit in (0, 1)) for state in range(8)
)


def termination_inputs(start_state: int) -> tuple[int, int, int]:
    state = start_state
    inputs = []
    for _ in range(3):
        d1, d2, d3 = state_bits(state)
        bit = d2 ^ d3
        inputs.append(bit)
        state, _ = rsc_step(state, bit)
    if state != 0:
        raise AssertionError("RSC termination did not return to state zero")
    return tuple(inputs)


TAIL_INPUTS = tuple(termination_inputs(state) for state in range(8))


@dataclass(frozen=True, slots=True)
class TurboEncoded:
    data_rows: tuple[tuple[int, int, int, int, int, int], ...]
    tail_rows: tuple[tuple[int, int, int, int, int, int], ...]
    channel_bits: tuple[int, ...]
    first_final_state: int
    second_final_state: int


def encode(information_bits) -> TurboEncoded:
    bits = tuple(information_bits)
    if len(bits) != K or any(bit not in (0, 1) for bit in bits):
        raise ValueError("Turbo encoder requires exactly 432 bits")
    permuted = interleave(bits)
    first_state = 0
    second_state = 0
    rows = []
    for original, interleaved_bit in zip(bits, permuted):
        first_state, first_output = rsc_step(first_state, original)
        second_state, second_output = rsc_step(second_state, interleaved_bit)
        rows.append(first_output + second_output)
    first_final = first_state
    second_final = second_state
    tails = []
    for _ in range(3):
        first_state, output = rsc_tail(first_state)
        tails.append(output + (0, 0, 0))
    for _ in range(3):
        second_state, output = rsc_tail(second_state)
        tails.append((0, 0, 0) + output)
    if first_state != 0 or second_state != 0:
        raise AssertionError("Turbo RSC termination invariant failed")
    data_rows = tuple(rows)
    tail_rows = tuple(tails)
    return TurboEncoded(
        data_rows=data_rows,
        tail_rows=tail_rows,
        channel_bits=puncture(data_rows, tail_rows),
        first_final_state=first_final,
        second_final_state=second_final,
    )


def _normalize(metrics: list[float]) -> list[float]:
    finite = [value for value in metrics if math.isfinite(value)]
    if not finite:
        raise ArithmeticError("Turbo metric step has no finite state")
    maximum = max(finite)
    return [value - maximum if math.isfinite(value) else -math.inf for value in metrics]


def _max_star(left: float, right: float) -> float:
    """Binary Log-MAP Jacobian operation in deterministic binary64 order."""

    if left == -math.inf:
        return right
    if right == -math.inf:
        return left
    maximum = left if left >= right else right
    return maximum + math.log1p(math.exp(-abs(left - right)))


def _siso(
    systematic: tuple[float, ...],
    parity0: tuple[float, ...],
    parity1: tuple[float, ...],
    apriori_data: tuple[float, ...],
    *,
    diagnostics: dict[str, object] | None = None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    steps = K + 3
    if not (
        len(systematic) == len(parity0) == len(parity1) == steps
        and len(apriori_data) == K
    ):
        raise ValueError("Log-MAP SISO length invariant failed")
    if any(not math.isfinite(value) for values in (systematic, parity0, parity1, apriori_data) for value in values):
        raise ArithmeticError("Turbo decoder received a non-finite LLR")

    gamma = []
    for k in range(steps):
        la = apriori_data[k] if k < K else 0.0
        t0 = systematic[k] + la
        step = []
        for state in range(8):
            transitions = []
            for bit in (0, 1):
                next_state, (_, p0, p1) = RSC_TRANSITIONS[state][bit]
                t1 = t0 if bit == 0 else -t0
                t2 = parity0[k] if p0 == 0 else -parity0[k]
                t3 = parity1[k] if p1 == 0 else -parity1[k]
                transitions.append((next_state, 0.5 * ((t1 + t2) + t3)))
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
                candidate = alpha[k][state] + branch
                following[target] = _max_star(following[target], candidate)
        alpha[k + 1] = _normalize(following)

    beta = [[-math.inf] * 8 for _ in range(steps + 1)]
    beta[steps][0] = 0.0
    for k in range(steps - 1, -1, -1):
        preceding = [-math.inf] * 8
        for state in range(8):
            best = -math.inf
            for bit in (0, 1):
                target, branch = gamma[k][state][bit]
                candidate = branch + beta[k + 1][target]
                best = _max_star(best, candidate)
            preceding[state] = best
        beta[k] = _normalize(preceding)

    posterior = []
    extrinsic = []
    for k in range(K):
        maxima = [-math.inf, -math.inf]
        for state in range(8):
            for bit in (0, 1):
                target, branch = gamma[k][state][bit]
                candidate = alpha[k][state] + branch + beta[k + 1][target]
                maxima[bit] = _max_star(maxima[bit], candidate)
        if not all(math.isfinite(value) for value in maxima):
            raise ArithmeticError("Turbo posterior has no finite bit metric")
        post = maxima[0] - maxima[1]
        posterior.append(post)
        extrinsic.append((post - systematic[k]) - apriori_data[k])
    if diagnostics is not None:
        gamma_steps = (0, 1, 2, 431, 432, 434)
        metric_rows = (0, 1, 2, 431, 432, 434, 435)
        diagnostics.update({
            "gamma_branch_metrics": {
                str(k): tuple(
                    tuple(gamma[k][state][bit][1] for bit in (0, 1))
                    for state in range(8)
                )
                for k in gamma_steps
            },
            "alpha_metrics": {
                str(k): tuple(alpha[k]) for k in metric_rows
            },
            "beta_metrics": {
                str(k): tuple(beta[k]) for k in metric_rows
            },
        })
    return tuple(posterior), tuple(extrinsic)


def decode(channel_llrs, iterations: int = 8) -> tuple[int, ...]:
    if iterations != 8:
        raise ValueError("The approved Link ID 11 decoder uses exactly 8 iterations")
    data_rows, tail_rows = rate_recover(channel_llrs)
    lx = tuple(row[0] for row in data_rows)
    y0 = tuple(row[1] for row in data_rows)
    yp0 = tuple(row[4] for row in data_rows)

    sys1 = lx + tuple(row[0] for row in tail_rows[:3])
    p01 = y0 + tuple(row[1] for row in tail_rows[:3])
    p11 = (0.0,) * (K + 3)
    sys2 = interleave(lx) + tuple(row[3] for row in tail_rows[3:])
    p02 = yp0 + tuple(row[4] for row in tail_rows[3:])
    p12 = (0.0,) * (K + 3)

    la1 = (0.0,) * K
    post2 = (0.0,) * K
    for _ in range(iterations):
        _, ext1 = _siso(sys1, p01, p11, la1)
        la2 = interleave(ext1)
        post2, ext2 = _siso(sys2, p02, p12, la2)
        la1 = deinterleave(ext2)
    final = deinterleave(post2)
    if any(not math.isfinite(value) for value in final):
        raise ArithmeticError("Turbo decoder produced non-finite final LLRs")
    return tuple(0 if value >= 0.0 else 1 for value in final)
