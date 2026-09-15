"""VDE Packet 고정 용량 구성·Type 81 padding·CRC-32 파싱 package."""

from .builder import build_packet
from .parser import parse_packet

__all__ = ("build_packet", "parse_packet")
