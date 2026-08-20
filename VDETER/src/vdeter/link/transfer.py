"""Application transfer analysis, Packet build, and transmit-context lifetime."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from ..common.contracts.link import (
    BuiltPacket,
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkTransferControlRequest,
    LinkTransferControlResult,
    LinkTransferProfileRequirement,
    TransferControlKind,
    TransferKind,
    TransferPacketOutcome,
    TransferProgressSnapshot,
    TransferRequirement,
)
from ..common.contracts.mac import TransmissionProcedure
from ..common.contracts.message import FragmentRef
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import (
    IdSource,
    LinkId,
    PacketId,
    SequentialId,
    SessionHandle,
    TransferId,
)
from ..common.results import InternalResult, Ok, internal_error
from ..profiles.models import SupportStatus
from .data_session.fragmentation import (
    DataSessionTxContext,
    create_data_session_tx_context,
)
from .segment.allocator import SequentialIdAllocator
from .short_data.codec import build_short_data_packet

MAX_TERMINAL_TOMBSTONES = 1024
SEGMENT_HEADER_SIZE = 4
MAX_FRAME_FRAGMENTS = 14


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    transfer_id: TransferId | None = None,
    session_handle: SessionHandle | None = None,
    sequential_id: SequentialId | None = None,
    packet_id: PacketId | None = None,
    retryable: bool = False,
    **details,
):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        retryable=retryable,
        correlation=Correlation(
            transfer_id=transfer_id,
            session_handle=session_handle,
            sequential_id=sequential_id,
            packet_id=packet_id,
        ),
        details=Details.of(**details) if details else Details(),
    )


@dataclass(slots=True)
class _Analysis:
    request: LinkAnalyzeRequest
    requirement: TransferRequirement
    by_link: dict[LinkId, LinkTransferProfileRequirement]


@dataclass(slots=True)
class _PacketState:
    segment_offset: int
    segment_length: int
    outcome: TransferPacketOutcome | None = None


@dataclass(slots=True)
class _TransferContext:
    analysis: _Analysis
    sequential_id: SequentialId
    procedure: TransmissionProcedure
    session_handle: SessionHandle | None
    segment_size: int
    current_link_id: LinkId
    data_session: DataSessionTxContext | None = None
    packets: dict[PacketId, _PacketState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _TerminalTombstone:
    kind: TransferControlKind
    session_handle: SessionHandle | None


class LinkTransferManager:
    """Own Stage 1 transmit Segment state without taking MAC decisions.

    ACK timeout, retry choice, channel selection, and terminal outcome remain MAC
    responsibilities.  This class only validates the selected plan, builds the
    requested logical Packet, and accounts for Packet outcomes reported by MAC.
    """

    __slots__ = (
        "_id_source",
        "_message_registry",
        "_link_profile_registry",
        "_allocator",
        "_analyses",
        "_active",
        "_tombstones",
    )

    def __init__(
        self,
        id_source: IdSource,
        message_registry,
        link_profile_registry,
        sequential_id_allocator: SequentialIdAllocator | None = None,
    ) -> None:
        self._id_source = id_source
        self._message_registry = message_registry
        self._link_profile_registry = link_profile_registry
        self._allocator = sequential_id_allocator or SequentialIdAllocator()
        self._analyses: dict[TransferId, _Analysis] = {}
        self._active: dict[TransferId, _TransferContext] = {}
        self._tombstones: OrderedDict[TransferId, _TerminalTombstone] = OrderedDict()

    @property
    def active_transfer_count(self) -> int:
        return len(self._active)

    @property
    def active_sequential_id_count(self) -> int:
        return self._allocator.active_count

    @property
    def terminal_tombstone_count(self) -> int:
        return len(self._tombstones)

    def analyze_transfer(
        self, request: LinkAnalyzeRequest
    ) -> InternalResult[TransferRequirement]:
        if not isinstance(request, LinkAnalyzeRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkAnalyzeRequest is required",
            )
        transfer_id = request.transfer_id
        if transfer_id in self._tombstones:
            return _error(
                ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT,
                ErrorCategory.PROTOCOL,
                "terminal transfer cannot be analyzed again before restart or eviction",
                transfer_id=transfer_id,
            )
        existing = self._analyses.get(transfer_id)
        if existing is not None:
            if existing.request == request:
                return Ok(existing.requirement)
            return _error(
                ErrorCode.LINK_SESSION_CONFLICT,
                ErrorCategory.PROTOCOL,
                "transfer_id is already bound to different analysis input",
                transfer_id=transfer_id,
            )
        route_error = self._validate_route(request)
        if route_error is not None:
            return route_error
        presentation_size = len(request.presentation_data)
        if not 1 <= presentation_size <= 65535:
            return _error(
                ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED,
                ErrorCategory.VALIDATION,
                "presentation data must contain 1..65535 bytes",
                transfer_id=transfer_id,
                presentation_size=presentation_size,
            )
        if not getattr(self._link_profile_registry, "is_frozen", False):
            return _error(
                ErrorCode.LINK_PROFILE_MISMATCH,
                ErrorCategory.VALIDATION,
                "Link Profile registry must be frozen",
                transfer_id=transfer_id,
            )

        profiles: list[LinkTransferProfileRequirement] = []
        by_link: dict[LinkId, LinkTransferProfileRequirement] = {}
        segment_size = SEGMENT_HEADER_SIZE + presentation_size
        message_type = 92 if request.ack_required else 93
        for profile in self._link_profile_registry.entries():
            if profile.support_status is not SupportStatus.SUPPORTED:
                continue
            capacity = profile.fragment_capacity_bytes
            if not isinstance(capacity, int) or capacity <= 0:
                continue
            payload_capacities = dict(profile.message_payload_capacities)
            short_capacity = payload_capacities.get(message_type)
            short_eligible = isinstance(short_capacity, int) and segment_size <= short_capacity
            fragment_count = (segment_size + capacity - 1) // capacity
            minimum_frames = (fragment_count + MAX_FRAME_FRAGMENTS - 1) // MAX_FRAME_FRAGMENTS
            item = LinkTransferProfileRequirement(
                link_id=profile.link_id,
                short_data_eligible=short_eligible,
                data_session_fragment_count=fragment_count,
                minimum_tdma_frame_count=minimum_frames,
            )
            profiles.append(item)
            by_link[profile.link_id] = item
        if not profiles:
            return _error(
                ErrorCode.LINK_PROFILE_NOT_FOUND,
                ErrorCategory.UNSUPPORTED,
                "no supported Link Profile is available for transfer analysis",
                transfer_id=transfer_id,
            )

        requirement = TransferRequirement(
            transfer_id=transfer_id,
            route=request.route,
            ack_required=request.ack_required,
            presentation_size=presentation_size,
            candidate=(
                TransferKind.SHORT_DATA
                if any(item.short_data_eligible for item in profiles)
                else TransferKind.DATA_SESSION
            ),
            sequential_id=None,
            profiles=tuple(profiles),
        )
        self._analyses[transfer_id] = _Analysis(request, requirement, by_link)
        return Ok(requirement)

    def build_packet(self, request: LinkBuildRequest) -> InternalResult[BuiltPacket]:
        if not isinstance(request, LinkBuildRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkBuildRequest is required",
            )
        plan = request.plan
        transfer_id = plan.transfer_id
        if request.sequential_id is not None:
            return _error(
                ErrorCode.LINK_SEQUENTIAL_ID_CALLER_FORBIDDEN,
                ErrorCategory.VALIDATION,
                "Sequential ID is allocated only by Link",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
            )
        if transfer_id in self._tombstones:
            return _error(
                ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT,
                ErrorCategory.PROTOCOL,
                "terminal transfer cannot build another Packet",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
            )
        analysis = self._analyses.get(transfer_id)
        if analysis is None:
            return _error(
                ErrorCode.LINK_UNKNOWN_TRANSFER,
                ErrorCategory.PROTOCOL,
                "transfer has not been analyzed",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
            )
        if request.presentation_data != analysis.request.presentation_data or plan.route != analysis.request.route:
            return _error(
                ErrorCode.LINK_SESSION_CONFLICT,
                ErrorCategory.PROTOCOL,
                "build payload or route differs from analyzed transfer",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
            )
        profile_requirement = analysis.by_link.get(plan.link_id)
        if profile_requirement is None:
            return _error(
                ErrorCode.LINK_UNSUPPORTED_LINK_ID,
                ErrorCategory.UNSUPPORTED,
                "MAC selected a Link ID absent from transfer analysis",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
                link_id=plan.link_id.value,
            )
        fields_error = self._validate_build_fields(request, analysis, profile_requirement)
        if fields_error is not None:
            return fields_error
        if not getattr(self._message_registry, "is_frozen", False):
            return _error(
                ErrorCode.LINK_PROFILE_MISMATCH,
                ErrorCategory.VALIDATION,
                "Message registry must be frozen",
                transfer_id=transfer_id,
                session_handle=plan.session_handle,
            )

        context = self._active.get(transfer_id)
        newly_allocated = context is None
        if context is None:
            reserved = self._allocator.reserve_for_transfer(transfer_id)
            if reserved.is_err:
                return reserved
            sequential_id = reserved.value
        else:
            sequential_id = context.sequential_id
            if context.procedure is not plan.procedure or context.session_handle != plan.session_handle:
                return _error(
                    ErrorCode.LINK_SESSION_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "active transfer procedure or Session changed",
                    transfer_id=transfer_id,
                    session_handle=plan.session_handle,
                    sequential_id=sequential_id,
                )

        if plan.procedure is TransmissionProcedure.SHORT_DATA:
            built = build_short_data_packet(
                plan.link_id,
                plan.route,
                analysis.request.ack_required,
                request.retransmission_number,
                sequential_id,
                request.presentation_data,
                self._message_registry,
                self._link_profile_registry,
            )
            if built.is_err:
                if newly_allocated:
                    self._allocator.discard_uncommitted(transfer_id)
                return built
            segment_offset = 0
            segment_length = SEGMENT_HEADER_SIZE + len(request.presentation_data)
            public_fragment = None
            data_context = None if context is None else context.data_session
        else:
            data_context = None if context is None else context.data_session
            if data_context is None:
                created = create_data_session_tx_context(
                    transfer_id,
                    plan.session_handle,
                    plan.route,
                    sequential_id,
                    request.presentation_data,
                )
                if created.is_err:
                    if newly_allocated:
                        self._allocator.discard_uncommitted(transfer_id)
                    return created
                data_context = created.value
            elif context is not None and plan.link_id != context.current_link_id:
                switch_error = self._profile_switch_error(context)
                if switch_error is not None:
                    return switch_error
                candidate = data_context.repartition_from_committed_prefix(
                    self._committed_prefix(context)[0]
                )
                if candidate.is_err:
                    return candidate
                data_context = candidate.value

            built = data_context.build_fragment(
                plan.link_id,
                request.session_frame_index,
                request.frame_fragment_count,
                request.fragment_position_in_frame,
                self._message_registry,
                self._link_profile_registry,
            )
            if built.is_err:
                if newly_allocated:
                    self._allocator.discard_uncommitted(transfer_id)
                return built
            segment_offset = built.value.segment_offset
            segment_length = built.value.segment_length
            public_fragment = FragmentRef(
                built.value.fragment.global_fragment_number,
                None,
            )

        if context is None:
            context = _TransferContext(
                analysis=analysis,
                sequential_id=sequential_id,
                procedure=plan.procedure,
                session_handle=plan.session_handle,
                segment_size=SEGMENT_HEADER_SIZE + len(request.presentation_data),
                current_link_id=plan.link_id,
                data_session=data_context,
            )
            self._active[transfer_id] = context
        else:
            context.current_link_id = plan.link_id
            if plan.procedure is TransmissionProcedure.DATA_SESSION:
                context.data_session = data_context

        packet_id = self._id_source.new_packet_id()
        context.packets[packet_id] = _PacketState(segment_offset, segment_length)
        packet_output = built.value.packet if plan.procedure is TransmissionProcedure.DATA_SESSION else built.value
        return Ok(
            BuiltPacket(
                packet_id=packet_id,
                transfer_id=transfer_id,
                link_id=plan.link_id,
                packet=packet_output.packet,
                session_handle=plan.session_handle,
                sequential_id=sequential_id,
                fragment=public_fragment,
                segment_offset=segment_offset,
                segment_length=segment_length,
            )
        )

    def control_transfer(
        self, request: LinkTransferControlRequest
    ) -> InternalResult[LinkTransferControlResult]:
        if not isinstance(request, LinkTransferControlRequest):
            return _error(
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "LinkTransferControlRequest is required",
            )
        if request.kind is TransferControlKind.ABORT_SESSION:
            return self._abort_session(request.session_handle)
        if request.kind is TransferControlKind.PROGRESS:
            return self._record_progress(request)
        return self._terminal(request)

    def restart(self) -> None:
        """Clear transmit contexts, analyses, allocator state, and tombstones."""

        self._analyses.clear()
        self._active.clear()
        self._tombstones.clear()
        self._allocator.reset()

    def _validate_route(self, request: LinkAnalyzeRequest):
        route = request.route
        transfer_id = request.transfer_id
        if route.original_source.station_id.value == 0:
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "Source ID 0 is not allowed",
                transfer_id=transfer_id,
            )
        if (route.node_source is None) != (route.node_destination is None):
            return _error(
                ErrorCode.LINK_DATA_ROUTE_INCOMPLETE,
                ErrorCategory.VALIDATION,
                "node source and destination must be supplied as a pair",
                transfer_id=transfer_id,
            )
        if route.node_source is not None and route.node_source.station_id.value == 0:
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "Node Source ID 0 is not allowed",
                transfer_id=transfer_id,
            )
        if request.ack_required and route.original_destination.address.is_broadcast:
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "broadcast transfer cannot request ACK",
                transfer_id=transfer_id,
            )
        return None

    def _validate_build_fields(
        self,
        request: LinkBuildRequest,
        analysis: _Analysis,
        profile_requirement: LinkTransferProfileRequirement,
    ):
        plan = request.plan
        correlation = dict(
            transfer_id=plan.transfer_id,
            session_handle=plan.session_handle,
        )
        frame_fields = (
            request.session_frame_index,
            request.frame_fragment_count,
            request.fragment_position_in_frame,
        )
        if plan.procedure is TransmissionProcedure.SHORT_DATA:
            if not profile_requirement.short_data_eligible:
                return _error(
                    ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED,
                    ErrorCategory.UNSUPPORTED,
                    "selected Link Profile cannot carry this Short Data Segment",
                    **correlation,
                    link_id=plan.link_id.value,
                )
            if plan.session_handle is not None or any(value is not None for value in frame_fields):
                return _error(
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                    ErrorCategory.VALIDATION,
                    "Short Data does not use Session or frame fields",
                    **correlation,
                )
            if analysis.request.ack_required:
                if request.retransmission_number is None:
                    return _error(
                        ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                        ErrorCategory.VALIDATION,
                        "ACK Short Data requires retransmission_number",
                        **correlation,
                    )
            elif request.retransmission_number is not None:
                return _error(
                    ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                    ErrorCategory.VALIDATION,
                    "no-ACK Short Data forbids retransmission_number",
                    **correlation,
                )
        else:
            if plan.session_handle is None or any(value is None for value in frame_fields):
                return _error(
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                    ErrorCategory.VALIDATION,
                    "Data Session requires Session and all frame fields",
                    **correlation,
                )
            if request.retransmission_number is not None:
                return _error(
                    ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                    ErrorCategory.VALIDATION,
                    "Data Session forbids retransmission_number",
                    **correlation,
                )
        return None

    def _record_progress(self, request: LinkTransferControlRequest):
        transfer_id = request.transfer_id
        tombstone = self._tombstones.get(transfer_id)
        if tombstone is not None:
            return _error(
                ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT,
                ErrorCategory.PROTOCOL,
                "late PROGRESS cannot change a terminal transfer",
                transfer_id=transfer_id,
                session_handle=request.session_handle,
            )
        context = self._active.get(transfer_id)
        if context is None:
            return _error(
                ErrorCode.LINK_UNKNOWN_TRANSFER,
                ErrorCategory.PROTOCOL,
                "PROGRESS references an unknown transfer",
                transfer_id=transfer_id,
                session_handle=request.session_handle,
            )
        if request.session_handle is not None and request.session_handle != context.session_handle:
            return _error(
                ErrorCode.LINK_SESSION_CONFLICT,
                ErrorCategory.PROTOCOL,
                "PROGRESS Session differs from active transfer",
                transfer_id=transfer_id,
                session_handle=request.session_handle,
                sequential_id=context.sequential_id,
            )

        # Validate the full tuple before changing any Packet outcome.
        for item in request.packet_progress:
            packet = context.packets.get(item.packet_id)
            if packet is None:
                return _error(
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                    ErrorCategory.PROTOCOL,
                    "PROGRESS Packet was not built by this transfer",
                    transfer_id=transfer_id,
                    session_handle=context.session_handle,
                    sequential_id=context.sequential_id,
                    packet_id=item.packet_id,
                )
            if packet.outcome is not None and packet.outcome is not item.outcome:
                return _error(
                    ErrorCode.LINK_TRANSFER_PROGRESS_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "Packet outcome conflicts with an earlier PROGRESS",
                    transfer_id=transfer_id,
                    session_handle=context.session_handle,
                    sequential_id=context.sequential_id,
                    packet_id=item.packet_id,
                )
        for item in request.packet_progress:
            context.packets[item.packet_id].outcome = item.outcome

        return Ok(
            LinkTransferControlResult(
                kind=TransferControlKind.PROGRESS,
                progress=(self._snapshot(transfer_id, context),),
                released_transfer_ids=(),
                released_sequential_ids=(),
            )
        )

    def _terminal(self, request: LinkTransferControlRequest):
        transfer_id = request.transfer_id
        context = self._active.get(transfer_id)
        if context is None:
            tombstone = self._tombstones.get(transfer_id)
            if tombstone is None:
                return _error(
                    ErrorCode.LINK_UNKNOWN_TRANSFER,
                    ErrorCategory.PROTOCOL,
                    "terminal control references an unknown transfer",
                    transfer_id=transfer_id,
                    session_handle=request.session_handle,
                )
            if tombstone.kind is not request.kind or not self._session_compatible(
                tombstone.session_handle, request.session_handle
            ):
                return _error(
                    ErrorCode.LINK_TRANSFER_TERMINAL_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "terminal control conflicts with the stored tombstone",
                    transfer_id=transfer_id,
                    session_handle=request.session_handle,
                )
            return Ok(self._empty_control_result(request.kind))

        if request.session_handle is not None and request.session_handle != context.session_handle:
            return _error(
                ErrorCode.LINK_SESSION_CONFLICT,
                ErrorCategory.PROTOCOL,
                "terminal Session differs from active transfer",
                transfer_id=transfer_id,
                session_handle=request.session_handle,
                sequential_id=context.sequential_id,
            )
        if request.kind is TransferControlKind.COMPLETE:
            committed, _ = self._committed_prefix(context)
            outstanding = any(packet.outcome is None for packet in context.packets.values())
            if committed != context.segment_size or outstanding:
                return _error(
                    ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                    ErrorCategory.PROTOCOL,
                    "COMPLETE requires a fully committed Segment and no outstanding Packet",
                    transfer_id=transfer_id,
                    session_handle=context.session_handle,
                    sequential_id=context.sequential_id,
                    committed_prefix_bytes=committed,
                    segment_size=context.segment_size,
                    outstanding_packet_count=sum(
                        packet.outcome is None for packet in context.packets.values()
                    ),
                )
        released = self._release(transfer_id, context, request.kind)
        return Ok(
            LinkTransferControlResult(
                kind=request.kind,
                progress=(),
                released_transfer_ids=(transfer_id,),
                released_sequential_ids=(released,),
            )
        )

    def _abort_session(self, session_handle: SessionHandle):
        targets = tuple(
            (transfer_id, context)
            for transfer_id, context in self._active.items()
            if context.session_handle == session_handle
        )
        released_transfer_ids = []
        released_sequential_ids = []
        for transfer_id, context in targets:
            released_transfer_ids.append(transfer_id)
            released_sequential_ids.append(
                self._release(transfer_id, context, TransferControlKind.ABORT_SESSION)
            )
        return Ok(
            LinkTransferControlResult(
                kind=TransferControlKind.ABORT_SESSION,
                progress=(),
                released_transfer_ids=tuple(released_transfer_ids),
                released_sequential_ids=tuple(released_sequential_ids),
            )
        )

    def _release(
        self,
        transfer_id: TransferId,
        context: _TransferContext,
        kind: TransferControlKind,
    ) -> SequentialId:
        released = self._allocator.release_after_terminal(transfer_id)
        # The context and allocator are updated under the same endpoint-local call.
        # An allocator error here would indicate an internal programming invariant.
        if released.is_err:  # pragma: no cover - guarded by active-context ownership
            raise AssertionError(released.error)
        del self._active[transfer_id]
        self._analyses.pop(transfer_id, None)
        self._remember_tombstone(transfer_id, _TerminalTombstone(kind, context.session_handle))
        return released.value

    def _remember_tombstone(
        self, transfer_id: TransferId, tombstone: _TerminalTombstone
    ) -> None:
        self._tombstones[transfer_id] = tombstone
        while len(self._tombstones) > MAX_TERMINAL_TOMBSTONES:
            self._tombstones.popitem(last=False)

    @staticmethod
    def _session_compatible(
        stored: SessionHandle | None, requested: SessionHandle | None
    ) -> bool:
        return requested is None or stored == requested

    @staticmethod
    def _empty_control_result(kind: TransferControlKind) -> LinkTransferControlResult:
        return LinkTransferControlResult(kind, (), (), ())

    @staticmethod
    def _committed_prefix(context: _TransferContext) -> tuple[int, bool]:
        ranges = sorted(
            (
                packet.segment_offset,
                packet.segment_offset + packet.segment_length,
            )
            for packet in context.packets.values()
            if packet.outcome is TransferPacketOutcome.SUCCESS
        )
        prefix = 0
        has_gap = False
        for start, end in ranges:
            if start > prefix:
                has_gap = True
                break
            if end > prefix:
                prefix = end
        return min(prefix, context.segment_size), has_gap

    def _snapshot(
        self, transfer_id: TransferId, context: _TransferContext
    ) -> TransferProgressSnapshot:
        committed, _ = self._committed_prefix(context)
        return TransferProgressSnapshot(
            transfer_id=transfer_id,
            sequential_id=context.sequential_id,
            segment_size=context.segment_size,
            committed_prefix_bytes=committed,
            remaining_bytes=context.segment_size - committed,
        )

    def _profile_switch_error(self, context: _TransferContext):
        outstanding = sum(packet.outcome is None for packet in context.packets.values())
        if outstanding:
            return _error(
                ErrorCode.LINK_TRANSFER_PROFILE_SWITCH_UNSAFE,
                ErrorCategory.PROTOCOL,
                "all existing Packet outcomes must be resolved before Link ID change",
                transfer_id=context.analysis.request.transfer_id,
                session_handle=context.session_handle,
                sequential_id=context.sequential_id,
                outstanding_packet_count=outstanding,
            )
        committed, has_gap = self._committed_prefix(context)
        if has_gap:
            return _error(
                ErrorCode.LINK_TRANSFER_PROGRESS_NONCONTIGUOUS,
                ErrorCategory.PROTOCOL,
                "SUCCESS slices after a gap cannot be used for profile change",
                transfer_id=context.analysis.request.transfer_id,
                session_handle=context.session_handle,
                sequential_id=context.sequential_id,
                committed_prefix_bytes=committed,
            )
        if committed == context.segment_size:
            return _error(
                ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                ErrorCategory.PROTOCOL,
                "fully committed transfer has no bytes to repartition",
                transfer_id=context.analysis.request.transfer_id,
                session_handle=context.session_handle,
                sequential_id=context.sequential_id,
                committed_prefix_bytes=committed,
            )
        return None


__all__ = ("LinkTransferManager", "MAX_TERMINAL_TOMBSTONES")
