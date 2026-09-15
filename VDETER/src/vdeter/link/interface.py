"""Link Port (`stage0_baseline.md` §5.3, 상위 명세 §7.3, §7.4).

Link는 VDE Message와 Packet의 wire format을 소유한다. Logical Channel, slot, timeout과
재전송 시각은 결정하지 않으며 fragment 분할·재조립은 MAC이 제공한 로컬 `session_handle`
context에 따라 수행한다.

실제 Message codec, Packet 용량·padding·CRC와 Segment 처리는 단계 1 Link 기준선이
구현한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.contracts.link import (
    BuiltPacket,
    EncodedPacket,
    FragmentProcessResult,
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    LinkTransferControlResult,
    ReassemblyControlResult,
    TbbAssemblyControlRequest,
    TbbAssemblyControlResult,
    TbbEncodeRequest,
    TbbEncodedPacketSet,
    TransferRequirement,
)
from ..common.contracts.message import DecodedMessages
from ..common.results import InternalResult


@runtime_checkable
class LinkPort(Protocol):
    """Presentation ↔ Link ↔ MAC 경계 계약."""

    def analyze_transfer(self, request: LinkAnalyzeRequest) -> InternalResult[TransferRequirement]:
        """`InternalResult[TransferRequirement]`를 반환한다."""

    def build_packet(self, request: LinkBuildRequest) -> InternalResult[BuiltPacket]:
        """`InternalResult[BuiltPacket]`을 반환한다."""

    def encode_packet(self, request: LinkPacketEncodeRequest) -> InternalResult[EncodedPacket]:
        """일반 제어 Message Type 0·4·13·90 Packet을 만든다."""

    def encode_tbb(self, request: TbbEncodeRequest) -> InternalResult[TbbEncodedPacketSet]:
        """logical TBB를 서명하고 Link ID 11 Packet 집합으로 만든다."""

    def decode_packet(self, request: LinkDecodeRequest) -> InternalResult[DecodedMessages]:
        """`InternalResult[DecodedMessages]`를 반환한다."""

    def process_fragment(
        self, request: LinkFragmentProcessRequest
    ) -> InternalResult[FragmentProcessResult]:
        """MAC이 결합한 수신 Session context에서 Data fragment를 처리한다."""

    def control_reassembly(
        self, request: LinkReassemblyControlRequest
    ) -> InternalResult[ReassemblyControlResult]:
        """수신 Data Session context의 명시적 수명을 제어한다."""

    def control_transfer(
        self, request: LinkTransferControlRequest
    ) -> InternalResult[LinkTransferControlResult]:
        """송신 Segment 진행과 terminal 수명을 제어한다."""

    def control_tbb_assembly(
        self, request: TbbAssemblyControlRequest
    ) -> InternalResult[TbbAssemblyControlResult]:
        """수신 TBB candidate의 명시적 수명을 제어한다."""
