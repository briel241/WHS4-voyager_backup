"""Burst 생성·검출 package boundary.

training sequence, burst 구성과 검출은 단계 2·6 PHY 기준선에서 구현한다.
임시 F9A8 framing 실험은 `legacy/temporary_bpsk/phy_burst/`에 보관한다.
"""
"""Link ID 11 burst primitives."""

from .link_id import CODEWORDS, LINK_ID_11_CODEWORD, codeword, detect
from .framing import TxStages, build
from .rrc import RRC_TAPS, make_slot

__all__ = (
    "CODEWORDS", "LINK_ID_11_CODEWORD", "RRC_TAPS", "TxStages", "build",
    "codeword", "detect", "make_slot",
)
