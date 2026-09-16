"""Composed Stage 1 Link data-plane, transfer, reassembly, and TBB operations."""

from __future__ import annotations

from ..common.contracts.link import (
    EncodedPacket,
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    TbbAssemblyControlRequest,
    TbbEncodeRequest,
)
from ..common.contracts.message import (
    BODY_TYPE_TO_MESSAGE_TYPE,
    DecodedMessages,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Err, InternalResult, Ok, internal_error
from .data_session.reassembly import DataSessionReassembler
from .packet.builder import build_packet
from .packet.parser import parse_packet
from .short_data.codec import decode_short_data_message
from .transfer import LinkTransferManager
from .tbb.assembly import TbbAssembler
from .tbb.crypto import LinkCryptoDependencies
from .tbb.transport import encode_tbb as encode_tbb_packets

GENERAL_ENCODE_MESSAGE_TYPES = frozenset((0, 4, 13, 90))
OWNED_BUILD_MESSAGE_TYPES = frozenset((20, 21, 22, 74, 75, 76, 81, 92, 93))


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    correlation: Correlation = Correlation(),
    **details,
):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        correlation=correlation,
        details=Details.of(**details) if details else Details(),
    )


def _with_correlation(result, correlation: Correlation):
    if result.is_err:
        return Err(result.error.with_correlation(correlation))
    return result


class LinkDataPlane:
    """Concrete nine-method Stage 1 Link composed behind the approved DTOs."""

    __slots__ = (
        "_id_source",
        "_message_registry",
        "_link_profile_registry",
        "_maximum_reassembly_contexts",
        "_maximum_tbb_candidates",
        "_crypto",
        "_transfers",
        "_reassembler",
        "_tbb_assembler",
        "experiment_observer",
    )

    def __init__(
        self,
        id_source,
        message_registry,
        link_profile_registry,
        *,
        maximum_reassembly_contexts: int = 64,
        maximum_tbb_candidates: int = 64,
        crypto_dependencies: LinkCryptoDependencies | None = None,
    ) -> None:
        self._id_source = id_source
        self.experiment_observer = None
        self._message_registry = message_registry
        self._link_profile_registry = link_profile_registry
        self._maximum_reassembly_contexts = maximum_reassembly_contexts
        self._maximum_tbb_candidates = maximum_tbb_candidates
        self._crypto = crypto_dependencies or LinkCryptoDependencies()
        self._transfers = LinkTransferManager(
            id_source, message_registry, link_profile_registry
        )
        self._reassembler = DataSessionReassembler(maximum_reassembly_contexts)
        self._tbb_assembler = TbbAssembler(
            self._crypto.tbb_trust_store, maximum_tbb_candidates
        )

    def analyze_transfer(self, request: LinkAnalyzeRequest):
        return self._transfers.analyze_transfer(request)

    def build_packet(self, request: LinkBuildRequest):
        result = self._transfers.build_packet(request)
        if self.experiment_observer is not None and result.is_ok:
            packet = result.value
            self.experiment_observer.record(
                stage="packet_build", transfer_id=packet.transfer_id.value,
                link_id=packet.link_id.value, procedure=request.plan.procedure.value,
                packet_id=packet.packet_id.value,
                segment_offset=packet.segment_offset, segment_length=packet.segment_length,
                retransmission_number=request.retransmission_number,
                packet_bytes=len(packet.packet),
            )
        return result

    def control_transfer(self, request: LinkTransferControlRequest):
        return self._transfers.control_transfer(request)

    def encode_tbb(self, request: TbbEncodeRequest):
        return encode_tbb_packets(
            request,
            self._crypto.tbb_signer,
            self._id_source,
            self._message_registry,
            self._link_profile_registry,
        )

    def control_tbb_assembly(self, request: TbbAssemblyControlRequest):
        return self._tbb_assembler.control(request)

    def encode_packet(
        self, request: LinkPacketEncodeRequest
    ) -> InternalResult[EncodedPacket]:
        if not isinstance(request, LinkPacketEncodeRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkPacketEncodeRequest is required",
            )

        bodies = []
        for index, message in enumerate(request.messages):
            message_type = BODY_TYPE_TO_MESSAGE_TYPE[type(message.body)]
            if message_type not in GENERAL_ENCODE_MESSAGE_TYPES:
                correlation = message.correlation.merged(request.correlation)
                return _error(
                    ErrorCode.LINK_MESSAGE_BUILD_PATH_REQUIRED,
                    ErrorCategory.VALIDATION,
                    "Message type must use its Link-owned build path",
                    correlation=correlation,
                    message_type=message_type,
                    message_index=index,
                )
            bodies.append(message.body)

        built = build_packet(
            request.link_id,
            tuple(bodies),
            self._message_registry,
            self._link_profile_registry,
        )
        built = _with_correlation(built, request.correlation)
        if built.is_err:
            return built
        packet_id = self._id_source.new_packet_id()
        return Ok(
            EncodedPacket(
                packet_id=packet_id,
                link_id=request.link_id,
                packet=built.value.packet,
                message_types=built.value.message_types,
                padding_size=built.value.padding_size,
                crc=built.value.crc,
            )
        )

    def decode_packet(
        self, request: LinkDecodeRequest
    ) -> InternalResult[DecodedMessages]:
        if not isinstance(request, LinkDecodeRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkDecodeRequest is required",
            )
        correlation = Correlation(packet_id=request.packet_id)
        parsed = parse_packet(
            request.link_id,
            request.packet,
            self._message_registry,
            self._link_profile_registry,
        )
        parsed = _with_correlation(parsed, correlation)
        if parsed.is_err:
            return parsed

        messages = []
        verified_tbbs = []
        tbb_updates = []
        tbb_messages = tuple(
            message for message in parsed.value.messages if message.message_type in (20, 21, 22)
        )
        if tbb_messages and (
            request.link_id.value != 11
            or len(tbb_messages) != 1
            or len(parsed.value.messages) != 1
        ):
            return _error(
                ErrorCode.LINK_PACKET_CONTENT_INVALID,
                ErrorCategory.PROTOCOL,
                "TBB Packet must contain exactly one TBB Message on Link ID 11",
                correlation=correlation,
            )
        for message in parsed.value.messages:
            if message.message_type in (20, 21, 22):
                assembled = self._tbb_assembler.process(
                    request.packet_id, request.received_at, message
                )
                if assembled.is_err:
                    return assembled
                tbb_updates.append(assembled.value.update)
                if assembled.value.verified_tbb is not None:
                    verified_tbbs.append(assembled.value.verified_tbb)
            elif message.message_type in (92, 93):
                decoded = _with_correlation(
                    decode_short_data_message(message), correlation
                )
                if decoded.is_err:
                    return decoded
                messages.append(decoded.value)
            else:
                messages.append(message)
        return Ok(
            DecodedMessages(
                packet_id=request.packet_id,
                link_id=request.link_id,
                received_at=request.received_at,
                messages=tuple(messages),
                verified_tbbs=tuple(verified_tbbs),
                tbb_assembly_updates=tuple(tbb_updates),
            )
        )

    def process_fragment(self, request: LinkFragmentProcessRequest):
        return self._reassembler.process(request)

    def control_reassembly(self, request: LinkReassemblyControlRequest):
        return self._reassembler.control(request)

    def restart(self) -> None:
        self._transfers.restart()
        self._reassembler = DataSessionReassembler(
            self._maximum_reassembly_contexts
        )
        self._tbb_assembler = TbbAssembler(
            self._crypto.tbb_trust_store, self._maximum_tbb_candidates
        )


__all__ = (
    "GENERAL_ENCODE_MESSAGE_TYPES",
    "LinkDataPlane",
    "OWNED_BUILD_MESSAGE_TYPES",
    "VdeterLink",
)


VdeterLink = LinkDataPlane
