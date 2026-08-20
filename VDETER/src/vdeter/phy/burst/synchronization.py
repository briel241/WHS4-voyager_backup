"""Training-based Link ID 11 burst acquisition and carrier/timing recovery."""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass

from ..modulation.pi4_qpsk import hard_label, map_labels
from .rrc import cubic_interpolate, matched_filter

TRAINING_BITS = "111111001101010000011001010"
TRAINING_LABELS = tuple("11" if bit == "1" else "00" for bit in TRAINING_BITS)
TRAINING_SYMBOLS = map_labels(TRAINING_LABELS)
SYMBOL_RATE_HZ = 19200
SAMPLES_PER_SYMBOL = 4
CANDIDATE_CORRELATION_THRESHOLD = 0.30
CONFIRMATION_CORRELATION_THRESHOLD = 0.525
PEAK_RATIO_THRESHOLD = 1.05
CANDIDATE_LIMIT = 8
LAST_BODY_SYMBOL_OFFSET = 4 * 479


class NoBurstDetected(Exception):
    pass


class SynchronizationFailed(ArithmeticError):
    pass


@dataclass(frozen=True, slots=True)
class Acquisition:
    matched_samples: tuple[complex, ...]
    training_epoch_samples: float
    correlation: float
    peak_ratio: float
    phase_intercept: float
    phase_slope_per_symbol: float
    gain_magnitude: float
    cfo_hz: float


def _training_at(samples, epoch: float) -> tuple[complex, ...]:
    return tuple(
        cubic_interpolate(samples, epoch + SAMPLES_PER_SYMBOL * index)
        for index in range(27)
    )


def _metric(received: tuple[complex, ...]) -> tuple[float, float] | None:
    energy = sum(value.real * value.real + value.imag * value.imag for value in received)
    if not math.isfinite(energy) or energy == 0.0:
        return None
    z = tuple(value * known.conjugate() for value, known in zip(received, TRAINING_SYMBOLS))
    adjacent = 0.0 + 0.0j
    for index in range(26):
        adjacent += z[index + 1] * z[index].conjugate()
    if (
        not math.isfinite(adjacent.real)
        or not math.isfinite(adjacent.imag)
        or abs(adjacent) == 0.0
    ):
        return None
    slope = cmath.phase(adjacent)
    coherent = 0.0 + 0.0j
    for index, value in enumerate(z):
        coherent += value * cmath.exp(-1j * slope * index)
    correlation = (abs(coherent) ** 2) / (energy * 27.0)
    if not math.isfinite(correlation):
        return None
    return correlation, slope


def _unwrap(phases: tuple[float, ...]) -> tuple[float, ...]:
    output = [phases[0]]
    for value in phases[1:]:
        adjusted = value
        while adjusted - output[-1] >= math.pi:
            adjusted -= 2.0 * math.pi
        while adjusted - output[-1] < -math.pi:
            adjusted += 2.0 * math.pi
        output.append(adjusted)
    return tuple(output)


def _solve_candidate(
    filtered: tuple[complex, ...],
    coarse_epoch: float,
    peak_ratio: float,
) -> Acquisition | None:
    refined = []
    for step in range(-8, 9):
        epoch = coarse_epoch + step / 16.0
        evaluated = _metric(_training_at(filtered, epoch))
        if evaluated is not None:
            refined.append((evaluated[0], epoch))
    if not refined:
        return None
    _, epoch = max(refined, key=lambda item: (item[0], -abs(item[1] - coarse_epoch)))
    received = _training_at(filtered, epoch)
    energy = sum(value.real * value.real + value.imag * value.imag for value in received)
    if not math.isfinite(energy) or energy <= 0.0:
        return None
    z = tuple(value * known.conjugate() for value, known in zip(received, TRAINING_SYMBOLS))

    def evaluate_slope(slope: float) -> tuple[float, complex]:
        coherent = 0.0 + 0.0j
        for index, value in enumerate(z):
            coherent += value * cmath.exp(-1j * slope * index)
        return (abs(coherent) ** 2) / (energy * 27.0), coherent

    coarse = []
    for cfo_hz in range(-2200, 2201, 50):
        slope = 2.0 * math.pi * cfo_hz / SYMBOL_RATE_HZ
        correlation, coherent = evaluate_slope(slope)
        coarse.append((correlation, abs(cfo_hz), cfo_hz, slope, coherent))
    _, _, best_hz, _, _ = max(coarse, key=lambda item: (item[0], -item[1], -item[2]))
    fine = []
    for cfo_hz in range(max(-2200, best_hz - 50), min(2200, best_hz + 50) + 1, 5):
        slope = 2.0 * math.pi * cfo_hz / SYMBOL_RATE_HZ
        correlation, coherent = evaluate_slope(slope)
        fine.append((correlation, abs(cfo_hz), cfo_hz, slope, coherent))
    correlation, _, _, slope, coherent = max(
        fine, key=lambda item: (item[0], -item[1], -item[2])
    )
    if (
        not math.isfinite(correlation)
        or correlation < CONFIRMATION_CORRELATION_THRESHOLD
        or peak_ratio < PEAK_RATIO_THRESHOLD
        or coherent == 0.0 + 0.0j
    ):
        return None
    intercept = cmath.phase(coherent)
    gain = abs(coherent) / 27.0
    cfo_hz = slope * SYMBOL_RATE_HZ / (2.0 * math.pi)
    if (
        not all(math.isfinite(value) for value in (epoch, intercept, slope, gain, cfo_hz))
        or gain <= 0.0
        or cfo_hz < -2200.0
        or cfo_hz > 2200.0
    ):
        raise SynchronizationFailed("training solution is non-finite or outside the envelope")
    return Acquisition(
        matched_samples=filtered,
        training_epoch_samples=epoch,
        correlation=correlation,
        peak_ratio=peak_ratio,
        phase_intercept=intercept,
        phase_slope_per_symbol=slope,
        gain_magnitude=gain,
        cfo_hz=cfo_hz,
    )


def acquire_candidates(input_samples) -> tuple[Acquisition, ...]:
    source = tuple(complex(value) for value in input_samples)
    if not source or all(value == 0.0 + 0.0j for value in source):
        raise NoBurstDetected
    if any(not math.isfinite(value.real) or not math.isfinite(value.imag) for value in source):
        raise SynchronizationFailed("input samples are non-finite")
    filtered = matched_filter(source)
    last_epoch_exclusive = len(filtered) - LAST_BODY_SYMBOL_OFFSET
    if last_epoch_exclusive <= 20:
        raise NoBurstDetected
    metrics = []
    for epoch in range(20, last_epoch_exclusive):
        evaluated = _metric(_training_at(filtered, float(epoch)))
        if evaluated is not None:
            metrics.append((evaluated[0], float(epoch), evaluated[1]))
    if not metrics:
        raise NoBurstDetected
    ranked = sorted(metrics, key=lambda item: (-item[0], item[1]))
    coarse_candidates = []
    for correlation, epoch, _ in ranked:
        if correlation < CANDIDATE_CORRELATION_THRESHOLD:
            break
        if any(abs(epoch - selected[1]) <= 4.0 for selected in coarse_candidates):
            continue
        coarse_candidates.append((correlation, epoch))
        if len(coarse_candidates) == CANDIDATE_LIMIT:
            break
    confirmed = []
    for main_correlation, coarse_epoch in coarse_candidates:
        secondary = max(
            (item[0] for item in metrics if abs(item[1] - coarse_epoch) > 4.0),
            default=0.0,
        )
        peak_ratio = math.inf if secondary == 0.0 else main_correlation / secondary
        solved = _solve_candidate(filtered, coarse_epoch, peak_ratio)
        if solved is not None:
            confirmed.append(solved)
    if not confirmed:
        raise NoBurstDetected
    return tuple(
        sorted(
            confirmed,
            key=lambda item: (-item.correlation, -item.peak_ratio, item.training_epoch_samples),
        )
    )


def acquire(input_samples) -> Acquisition:
    """Return the strongest stage-two-confirmed candidate."""

    return acquire_candidates(input_samples)[0]


@dataclass(frozen=True, slots=True)
class EqualizedBody:
    symbols: tuple[complex, ...]
    noise_variance: float
    rho_hat: float


def estimate_training_noise(equalized_training) -> float:
    """Return the approved 27-symbol training residual mean with its fixed floor."""

    source = tuple(complex(value) for value in equalized_training)
    if len(source) != len(TRAINING_SYMBOLS):
        raise SynchronizationFailed("training noise estimate requires exactly 27 symbols")
    residual = tuple(
        value - known for value, known in zip(source, TRAINING_SYMBOLS)
    )
    raw_variance = sum(abs(value) ** 2 for value in residual) / 27.0
    noise_variance = max(raw_variance, 1e-12)
    if not math.isfinite(noise_variance):
        raise SynchronizationFailed("training noise estimate is non-finite")
    return noise_variance


def equalize_body(
    acquisition: Acquisition,
    reference_symbols: tuple[complex, ...] | None = None,
) -> EqualizedBody:
    raw = tuple(
        cubic_interpolate(
            acquisition.matched_samples,
            acquisition.training_epoch_samples + SAMPLES_PER_SYMBOL * index,
        )
        for index in range(480)
    )
    rho_hat = 1.0
    if reference_symbols is not None:
        if len(reference_symbols) not in (43, 480):
            raise SynchronizationFailed("carrier reference must contain 43 or 480 symbols")
        z = tuple(
            value * reference.conjugate()
            for value, reference in zip(raw[:len(reference_symbols)], reference_symbols)
        )
        def coherent_for(candidate_slope: float) -> complex:
            return sum(
                value * cmath.exp(-1j * candidate_slope * index)
                for index, value in enumerate(z)
            )

        search = []
        for cfo_hz in range(-2200, 2201, 25):
            candidate_slope = 2.0 * math.pi * cfo_hz / SYMBOL_RATE_HZ
            search.append((abs(coherent_for(candidate_slope)), abs(cfo_hz), cfo_hz, candidate_slope))
        _, _, best_hz, _ = max(search, key=lambda item: (item[0], -item[1], -item[2]))
        fine = []
        for cfo_hz in range(max(-2200, best_hz - 25), min(2200, best_hz + 25) + 1):
            candidate_slope = 2.0 * math.pi * cfo_hz / SYMBOL_RATE_HZ
            candidate_coherent = coherent_for(candidate_slope)
            fine.append((abs(candidate_coherent), abs(cfo_hz), cfo_hz, candidate_slope, candidate_coherent))
        _, _, best_hz, slope, coherent = max(
            fine, key=lambda item: (item[0], -item[1], -item[2])
        )
        if len(reference_symbols) == 480:
            sro_candidates = []
            for sro_ppm in range(-10, 11):
                candidate_rho = 1.0 + sro_ppm * 1e-6
                candidate_raw = tuple(
                    cubic_interpolate(
                        acquisition.matched_samples,
                        acquisition.training_epoch_samples
                        + SAMPLES_PER_SYMBOL * candidate_rho * index,
                    )
                    for index in range(480)
                )
                candidate_z = tuple(
                    value * reference.conjugate()
                    for value, reference in zip(candidate_raw, reference_symbols)
                )
                candidate_coherent = sum(
                    value * cmath.exp(-1j * slope * index)
                    for index, value in enumerate(candidate_z)
                )
                candidate_energy = sum(abs(value) ** 2 for value in candidate_raw)
                normalized_score = (
                    abs(candidate_coherent) ** 2
                    / (candidate_energy * len(reference_symbols))
                    if candidate_energy > 0.0
                    else 0.0
                )
                sro_candidates.append(
                    (normalized_score, -abs(sro_ppm), -sro_ppm, candidate_rho, candidate_raw)
                )
            _, _, _, rho_hat, raw = max(sro_candidates)
            z = tuple(
                value * reference.conjugate()
                for value, reference in zip(raw, reference_symbols)
            )

            def refined_coherent(candidate_slope: float) -> complex:
                return sum(
                    value * cmath.exp(-1j * candidate_slope * index)
                    for index, value in enumerate(z)
                )

            final_frequency = []
            for cfo_hz in range(max(-2200, best_hz - 2), min(2200, best_hz + 2) + 1):
                candidate_slope = 2.0 * math.pi * cfo_hz / SYMBOL_RATE_HZ
                candidate_coherent = refined_coherent(candidate_slope)
                final_frequency.append(
                    (abs(candidate_coherent), -abs(cfo_hz), -cfo_hz, candidate_slope, candidate_coherent)
                )
            _, _, _, slope, coherent = max(final_frequency)
        if coherent == 0.0 + 0.0j:
            raise SynchronizationFailed("FEC-aided carrier phase is unavailable")
        intercept = cmath.phase(coherent)
        gain = abs(coherent) / len(reference_symbols)
        equalized = []
        for index in range(len(reference_symbols)):
            equalized.append(
                raw[index] * cmath.exp(-1j * (intercept + slope * index)) / gain
            )
        if len(reference_symbols) < 480:
            phase = intercept + slope * len(reference_symbols)
            frequency = slope
            for index in range(len(reference_symbols), 480):
                current = raw[index] * cmath.exp(-1j * phase) / gain
                decision = hard_label(current, index)
                decision_symbol = map_labels((decision,), index)[0]
                error = cmath.phase(current * decision_symbol.conjugate())
                correction = 0.08 * error
                equalized.append(current * cmath.exp(-1j * correction))
                frequency += 0.0010 * error
                phase += frequency + correction
    else:
        equalized = []
        for index in range(27):
            rotation = cmath.exp(
                -1j * (acquisition.phase_intercept + acquisition.phase_slope_per_symbol * index)
            )
            equalized.append(raw[index] * rotation / acquisition.gain_magnitude)
        phase = acquisition.phase_intercept + acquisition.phase_slope_per_symbol * 27.0
        frequency = acquisition.phase_slope_per_symbol
        for index in range(27, 480):
            current = raw[index] * cmath.exp(-1j * phase) / acquisition.gain_magnitude
            decision = hard_label(current, index)
            decision_symbol = map_labels((decision,), index)[0]
            error = cmath.phase(current * decision_symbol.conjugate())
            correction = 0.10 * error
            equalized.append(current * cmath.exp(-1j * correction))
            frequency += 0.0015 * error
            phase += frequency + correction
    noise_variance = estimate_training_noise(equalized[:27])

    if any(not math.isfinite(value.real) or not math.isfinite(value.imag) for value in equalized):
        raise SynchronizationFailed("equalized body is non-finite")
    return EqualizedBody(tuple(equalized), noise_variance, rho_hat)
