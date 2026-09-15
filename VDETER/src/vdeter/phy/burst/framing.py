"""Link ID 11 bit chain, training/ID/data symbol body and slot construction."""

from __future__ import annotations

from dataclasses import dataclass

from ..fec.turbo import TurboEncoded, encode as turbo_encode
from ..modulation.pi4_qpsk import label_pairs, map_labels
from ..scrambling import scramble
from .link_id import LINK_ID_11_CODEWORD
from .rrc import make_slot

TRAINING_BITS = tuple(int(bit) for bit in "111111001101010000011001010")


def bytes_to_bits(packet: bytes) -> tuple[int, ...]:
    if not isinstance(packet, bytes):
        raise ValueError("Packet must be bytes")
    return tuple((byte >> shift) & 1 for byte in packet for shift in range(7, -1, -1))


def bits_to_bytes(bits) -> bytes:
    source = tuple(bits)
    if len(source) % 8 or any(bit not in (0, 1) for bit in source):
        raise ValueError("bit sequence must contain complete bytes")
    output = bytearray()
    for offset in range(0, len(source), 8):
        value = 0
        for bit in source[offset:offset + 8]:
            value = (value << 1) | bit
        output.append(value)
    return bytes(output)


@dataclass(frozen=True, slots=True)
class TxStages:
    packet_bits: tuple[int, ...]
    turbo: TurboEncoded
    scrambled_bits: tuple[int, ...]
    training_labels: tuple[str, ...]
    link_id_labels: tuple[str, ...]
    data_labels: tuple[str, ...]
    body_labels: tuple[str, ...]
    body_symbols: tuple[complex, ...]
    samples: tuple[complex, ...]


def build(packet: bytes) -> TxStages:
    if len(packet) != 54:
        raise ValueError("Link ID 11 Packet must be exactly 54 bytes")
    packet_bits = bytes_to_bits(packet)
    turbo = turbo_encode(packet_bits)
    scrambled = scramble(turbo.channel_bits)
    training_labels = tuple("11" if bit else "00" for bit in TRAINING_BITS)
    link_id_labels = label_pairs(LINK_ID_11_CODEWORD)
    data_labels = label_pairs(scrambled)
    body_labels = training_labels + link_id_labels + data_labels
    body_symbols = map_labels(body_labels)
    if len(body_symbols) != 480:
        raise AssertionError("Link ID 11 body symbol count invariant failed")
    return TxStages(
        packet_bits=packet_bits,
        turbo=turbo,
        scrambled_bits=scrambled,
        training_labels=training_labels,
        link_id_labels=link_id_labels,
        data_labels=data_labels,
        body_labels=body_labels,
        body_symbols=body_symbols,
        samples=make_slot(body_symbols),
    )
