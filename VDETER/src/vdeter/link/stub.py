"""단계 0 Link Stub (`stage0_baseline.md` §5.5).

이 구현은 interface 연결을 시험하기 위한 **test scope** stub이며 VDE-TER Link 구현이
아니다. 다음을 수행하지 않으며 수행했다고 주장하지 않는다.

- Message Type codec, field·예약값 validation
- Packet 구성, padding, CRC
- VDE Segment, Sequential ID 할당, Type 74/75/76 fragment 분할·재조립

stub은 Presentation data를 그대로 옮기며 `transfer_id`와 내부 correlation만 보존한다.
실제 구현은 단계 1 Link 기준선에서 이 Port 계약 안에 작성한다.
"""

from __future__ import annotations

from ..common.addressing import Broadcast
from ..common.contracts.link import (
    BuiltPacket,
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    TransferKind,
    TransferRequirement,
)
from ..common.contracts.message import DecodedMessage, DecodedMessages, ShortDataNoAckBody
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import IdSource, StationId
from ..common.results import Ok, internal_error
from .models import LinkCapabilities

#: stub envelope이 사용하는 유효 body tag. bytes가 실제 Type 93 wire라는 뜻은 아니다.
STAGE0_STUB_MESSAGE_TYPE = 93


class StubLink:
    """test scope Link stub. Packet을 만들지 않고 bytes를 그대로 옮긴다."""

    __slots__ = ("_id_source", "_capabilities")

    def __init__(self, id_source: IdSource, capabilities: LinkCapabilities) -> None:
        self._id_source = id_source
        self._capabilities = capabilities

    def analyze_transfer(self, request: LinkAnalyzeRequest):
        if not isinstance(request, LinkAnalyzeRequest):
            return internal_error(
                Layer.LINK,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkAnalyzeRequest가 필요합니다",
            )
        correlation = Correlation(transfer_id=request.transfer_id)
        if not request.presentation_data:
            return internal_error(
                Layer.LINK,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "Presentation data가 비어 있습니다",
                correlation=correlation,
            )
        # 단계 0 stub은 Short Data와 Data Session을 판정하지 않는다. 판정 규칙과 용량
        # 기준은 단계 1 Link 기준선이 소유한다.
        return Ok(
            TransferRequirement(
                transfer_id=request.transfer_id,
                route=request.route,
                ack_required=request.ack_required,
                presentation_size=len(request.presentation_data),
                candidate=TransferKind.SHORT_DATA,
                sequential_id=None,
            )
        )

    def build_packet(self, request: LinkBuildRequest):
        if not isinstance(request, LinkBuildRequest):
            return internal_error(
                Layer.LINK,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkBuildRequest가 필요합니다",
            )
        plan = request.plan
        correlation = Correlation(
            transfer_id=plan.transfer_id, session_handle=plan.session_handle
        )
        if not self._capabilities.supports(plan.link_id):
            return internal_error(
                Layer.LINK,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "endpoint 설정이 허용하지 않는 Link ID입니다",
                correlation=correlation,
                details=Details.of(link_id=plan.link_id.value),
            )
        # 단계 0 stub은 padding·CRC·Message 배치를 만들지 않는다.
        return Ok(
            BuiltPacket(
                packet_id=self._id_source.new_packet_id(),
                transfer_id=plan.transfer_id,
                link_id=plan.link_id,
                packet=request.presentation_data,
                session_handle=plan.session_handle,
                sequential_id=request.sequential_id,
                fragment=None,
            )
        )

    def decode_packet(self, request: LinkDecodeRequest):
        if not isinstance(request, LinkDecodeRequest):
            return internal_error(
                Layer.LINK,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkDecodeRequest가 필요합니다",
            )
        correlation = Correlation(packet_id=request.packet_id)
        if not self._capabilities.supports(request.link_id):
            return internal_error(
                Layer.LINK,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "endpoint 설정이 허용하지 않는 Link ID입니다",
                correlation=correlation,
                details=Details.of(link_id=request.link_id.value),
            )
        if not request.packet:
            return internal_error(
                Layer.LINK,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "Packet bytes가 비어 있습니다",
                correlation=correlation,
            )
        # 단계 0 stub은 Message를 decode하지 않는다. Presentation data를 그대로 옮기고
        # 자리표시자 Message Type만 표시한다.
        decoded = DecodedMessage(
            message_type=STAGE0_STUB_MESSAGE_TYPE,
            length=12 + len(request.packet),
            route=None,
            session_handle_candidate=None,
            sequential_id=None,
            fragment=None,
            body=ShortDataNoAckBody(
                source_id=StationId(1),
                session_id=0,
                destination=Broadcast(),
                segment=request.packet,
            ),
            presentation_data=request.packet,
        )
        return Ok(
            DecodedMessages(
                packet_id=request.packet_id,
                link_id=request.link_id,
                received_at=request.received_at,
                messages=(decoded,),
            )
        )
