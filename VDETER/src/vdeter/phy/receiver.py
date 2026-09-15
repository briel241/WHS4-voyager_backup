"""Link ID 11 receive inverse chain from complex samples to an opaque 54-byte Packet."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .burst.framing import bits_to_bytes
from .burst.link_id import LINK_ID_11_CODEWORD, LinkIdDetectionError, detect
from .burst.synchronization import (
    NoBurstDetected,
    SynchronizationFailed,
    TRAINING_SYMBOLS,
    acquire_candidates,
    equalize_body,
)
from .fec.turbo import decode as turbo_decode, encode as turbo_encode
from .modulation.pi4_qpsk import map_bits, soft_demap
from .scrambling import descramble_llrs, scramble


class UnsupportedDetectedLinkId(Exception):
    def __init__(self, link_id: int) -> None:
        super().__init__(f"unsupported detected Link ID {link_id}")
        self.link_id = link_id


class LinkIdNotAllowed(Exception):
    pass


class FecDecodeFailed(ArithmeticError):
    pass


LINK11_PREFIX_SYMBOLS = TRAINING_SYMBOLS + map_bits(LINK_ID_11_CODEWORD, 27)
FEC_CARRIER_REFINEMENT_PASSES = 1


@dataclass(frozen=True, slots=True)
class ReceiverCoreResult:
    packet: bytes
    link_id: int
    training_epoch_samples: float
    rho_hat: float
    correlation: float
    peak_ratio: float
    cfo_hz: float
    gain_magnitude: float
    noise_variance: float
    fec_codeword_score: float


class Phy11Receiver:
    def decode(self, samples, allowed_link_ids: frozenset[int] | None = None) -> ReceiverCoreResult:
        candidates = acquire_candidates(samples)
        decoded = []
        strongest_error = None
        for acquisition in candidates:
            try:
                body = equalize_body(acquisition)
                link_llrs = soft_demap(body.symbols[27:43], 27, body.noise_variance)
                link_id = detect(link_llrs)
                if link_id != 11:
                    raise UnsupportedDetectedLinkId(link_id)
                if allowed_link_ids and link_id not in allowed_link_ids:
                    raise LinkIdNotAllowed("detected Link ID 11 is not allowed")
                body = equalize_body(acquisition, LINK11_PREFIX_SYMBOLS)
                confirmed_link_id = detect(
                    soft_demap(body.symbols[27:43], 27, body.noise_variance)
                )
                if confirmed_link_id != 11:
                    raise UnsupportedDetectedLinkId(confirmed_link_id)
                data_llrs = descramble_llrs(
                    soft_demap(body.symbols[43:480], 43, body.noise_variance)
                )
                information_bits = turbo_decode(data_llrs)
                packet = bits_to_bytes(information_bits)
                if len(packet) != 54:
                    raise FecDecodeFailed("decoded Packet length invariant failed")
                for _ in range(FEC_CARRIER_REFINEMENT_PASSES):
                    reference = LINK11_PREFIX_SYMBOLS + map_bits(
                        scramble(turbo_encode(information_bits).channel_bits), 43
                    )
                    body = equalize_body(acquisition, reference)
                    data_llrs = descramble_llrs(
                        soft_demap(body.symbols[43:480], 43, body.noise_variance)
                    )
                    information_bits = turbo_decode(data_llrs)
                    packet = bits_to_bytes(information_bits)
                encoded = turbo_encode(information_bits).channel_bits
                denominator = sum(abs(value) for value in data_llrs)
                if not denominator or not math.isfinite(denominator):
                    raise FecDecodeFailed("FEC confirmation metric has zero or non-finite energy")
                score = sum(
                    value if bit == 0 else -value
                    for bit, value in zip(encoded, data_llrs)
                ) / denominator
                if not math.isfinite(score):
                    raise FecDecodeFailed("FEC confirmation metric is non-finite")
                decoded.append((score, acquisition.correlation, -acquisition.training_epoch_samples, packet, acquisition, body))
            except (
                LinkIdDetectionError,
                UnsupportedDetectedLinkId,
                LinkIdNotAllowed,
                SynchronizationFailed,
            ) as error:
                if strongest_error is None:
                    strongest_error = error
            except (ArithmeticError, ValueError, AssertionError) as error:
                if strongest_error is None:
                    strongest_error = FecDecodeFailed(str(error))
        if not decoded:
            if strongest_error is not None:
                raise strongest_error
            raise NoBurstDetected
        score, _, _, packet, acquisition, body = max(
            decoded, key=lambda item: (item[0], item[1], item[2])
        )
        return ReceiverCoreResult(
            packet=packet,
            link_id=11,
            training_epoch_samples=acquisition.training_epoch_samples,
            rho_hat=body.rho_hat,
            correlation=acquisition.correlation,
            peak_ratio=acquisition.peak_ratio,
            cfo_hz=acquisition.cfo_hz,
            gain_magnitude=acquisition.gain_magnitude,
            noise_variance=body.noise_variance,
            fec_codeword_score=score,
        )


__all__ = (
    "FecDecodeFailed", "LinkIdDetectionError", "LinkIdNotAllowed", "NoBurstDetected",
    "Phy11Receiver", "ReceiverCoreResult", "SynchronizationFailed",
    "UnsupportedDetectedLinkId",
)
