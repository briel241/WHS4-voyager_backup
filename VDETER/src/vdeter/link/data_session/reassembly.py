"""Bounded Data Session fragment reassembly와 명시적 수명 제어."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.contracts.link import (
    DataFragmentRef,
    FragmentProcessResult,
    FragmentProcessStatus,
    LinkFragmentProcessRequest,
    LinkReassemblyControlRequest,
    ReassemblyControlKind,
    ReassemblyControlResult,
)
from ...common.contracts.message import (
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
)
from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import PacketId, SequentialId, SessionHandle
from ...common.results import InternalResult, Ok, internal_error
from ..segment.codec import SEGMENT_HEADER_SIZE, decode_segment
from .fragmentation import MAX_FRAME_INDEX, _wire_route

MAX_REASSEMBLY_CONTEXTS = 64
MAX_SEGMENT_BYTES = 65539


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    session_handle: SessionHandle | None = None,
    sequential_id: SequentialId | None = None,
    packet_id: PacketId | None = None,
    fragment_index: int | None = None,
    fragment_count: int | None = None,
    **details,
):
    return internal_error(
        Layer.LINK,
        code,
        category,
        message,
        correlation=Correlation(
            session_handle=session_handle,
            sequential_id=sequential_id,
            packet_id=packet_id,
            fragment_index=fragment_index,
            fragment_count=fragment_count,
        ),
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class _StoredFragment:
    body: DataStartFragmentBody | DataContinuationFragmentBody | DataEndFragmentBody
    packet_id: PacketId
    fragment: DataFragmentRef


class _RxContext:
    __slots__ = (
        "route",
        "accepted",
        "position_keys",
        "pending",
        "frame_counts",
        "next_frame",
        "next_position",
        "expected_global",
        "assembled",
        "ordered_packet_ids",
        "declared_total",
        "sequential_id",
        "accepted_bytes",
    )

    def __init__(self, route) -> None:
        self.route = route
        self.accepted: dict[tuple[int, int, int], _StoredFragment] = {}
        self.position_keys: dict[tuple[int, int], tuple[int, int, int]] = {}
        self.pending: set[tuple[int, int, int]] = set()
        self.frame_counts: dict[int, int] = {}
        self.next_frame = 0
        self.next_position = 1
        self.expected_global = 0
        self.assembled = bytearray()
        self.ordered_packet_ids: list[PacketId] = []
        self.declared_total: int | None = None
        self.sequential_id: SequentialId | None = None
        self.accepted_bytes = 0


def _fragment_ref(request: LinkFragmentProcessRequest) -> DataFragmentRef:
    body = request.message.body
    count = body.frame_fragment_count
    if isinstance(body, DataStartFragmentBody):
        position = 1
    elif isinstance(body, DataContinuationFragmentBody):
        position = body.fragment_position_in_frame
    else:
        position = count
    return DataFragmentRef(
        session_frame_index=request.session_frame_index,
        position_in_frame=position,
        count_in_frame=count,
        global_fragment_number=body.global_fragment_number,
    )


def _continuation(body) -> int | None:
    if isinstance(body, (DataStartFragmentBody, DataEndFragmentBody)):
        return body.continue_resource_usage
    return None


class DataSessionReassembler:
    """MAC이 연 Session에만 fragment를 저장하고 COMPLETE 즉시 제거한다."""

    __slots__ = ("_contexts", "_maximum_contexts")

    def __init__(self, maximum_contexts: int = MAX_REASSEMBLY_CONTEXTS) -> None:
        if (
            isinstance(maximum_contexts, bool)
            or not isinstance(maximum_contexts, int)
            or maximum_contexts <= 0
        ):
            raise ValueError("maximum_contexts는 양의 정수여야 합니다")
        self._contexts: dict[SessionHandle, _RxContext] = {}
        self._maximum_contexts = maximum_contexts

    @property
    def active_count(self) -> int:
        return len(self._contexts)

    def control(
        self,
        request: LinkReassemblyControlRequest,
    ) -> InternalResult[ReassemblyControlResult]:
        if not isinstance(request, LinkReassemblyControlRequest):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "LinkReassemblyControlRequest가 필요합니다",
            )
        kind = request.kind
        handle = request.session_handle
        context = self._contexts.get(handle)

        if kind is ReassemblyControlKind.OPEN:
            route_result = _wire_route(request.route)
            if route_result.is_err:
                return route_result
            if context is not None:
                if context.route != request.route:
                    return _error(
                        ErrorCode.LINK_SESSION_CONFLICT,
                        ErrorCategory.PROTOCOL,
                        "같은 SessionHandle이 다른 route에 이미 결합됐습니다",
                        session_handle=handle,
                    )
                return Ok(ReassemblyControlResult(kind, handle, True, 0, 0))
            if len(self._contexts) >= self._maximum_contexts:
                return _error(
                    ErrorCode.LINK_REASSEMBLY_CAPACITY_EXHAUSTED,
                    ErrorCategory.RESOURCE,
                    "동시 reassembly context 상한에 도달했습니다",
                    session_handle=handle,
                    active_count=len(self._contexts),
                    capacity=self._maximum_contexts,
                )
            self._contexts[handle] = _RxContext(request.route)
            return Ok(ReassemblyControlResult(kind, handle, True, 0, 0))

        if context is None:
            return Ok(ReassemblyControlResult(kind, handle, False, 0, 0))
        if kind is ReassemblyControlKind.CLOSE and request.route is not None:
            if request.route != context.route:
                return _error(
                    ErrorCode.LINK_SESSION_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "CLOSE route가 OPEN-bound route와 다릅니다",
                    session_handle=handle,
                )

        discarded_count = len(context.accepted)
        discarded_bytes = context.accepted_bytes
        del self._contexts[handle]
        if kind is ReassemblyControlKind.CLOSE:
            return _error(
                ErrorCode.LINK_REASSEMBLY_INCOMPLETE,
                ErrorCategory.PROTOCOL,
                "미완성 Segment를 폐기하고 Session을 닫았습니다",
                session_handle=handle,
                discarded_fragment_count=discarded_count,
                discarded_bytes=discarded_bytes,
            )
        return Ok(
            ReassemblyControlResult(
                kind,
                handle,
                False,
                discarded_count,
                discarded_bytes,
            )
        )

    def _discard_error(
        self,
        handle: SessionHandle,
        request: LinkFragmentProcessRequest,
        fragment: DataFragmentRef,
        code: str,
        message: str,
    ):
        context = self._contexts.pop(handle, None)
        return _error(
            code,
            ErrorCategory.PROTOCOL,
            message,
            session_handle=handle,
            packet_id=request.packet_id,
            fragment_index=fragment.position_in_frame,
            fragment_count=fragment.count_in_frame,
            discarded_fragment_count=len(context.accepted) if context is not None else 0,
            discarded_bytes=context.accepted_bytes if context is not None else 0,
        )

    def process(
        self,
        request: LinkFragmentProcessRequest,
    ) -> InternalResult[FragmentProcessResult]:
        if not isinstance(request, LinkFragmentProcessRequest):
            return _error(
                ErrorCode.LINK_INVALID_MESSAGE_FIELD,
                ErrorCategory.VALIDATION,
                "LinkFragmentProcessRequest가 필요합니다",
            )
        handle = request.session_handle
        context = self._contexts.get(handle)
        if context is None:
            return _error(
                ErrorCode.LINK_UNKNOWN_SESSION,
                ErrorCategory.PROTOCOL,
                "OPEN되지 않은 Data Session입니다",
                session_handle=handle,
                packet_id=request.packet_id,
            )

        route_result = _wire_route(request.route)
        if route_result.is_err:
            return route_result
        if request.route != context.route:
            return _error(
                ErrorCode.LINK_DATA_ROUTE_MISMATCH,
                ErrorCategory.PROTOCOL,
                "fragment route가 OPEN-bound route와 다릅니다",
                session_handle=handle,
                packet_id=request.packet_id,
            )
        expected_source, expected_destination = route_result.value
        body = request.message.body
        if body.source_id != expected_source or body.destination != expected_destination:
            return _error(
                ErrorCode.LINK_DATA_ROUTE_MISMATCH,
                ErrorCategory.PROTOCOL,
                "fragment body 주소가 Session route와 다릅니다",
                session_handle=handle,
                packet_id=request.packet_id,
            )

        fragment = _fragment_ref(request)
        frame = fragment.session_frame_index
        position = fragment.position_in_frame
        count = fragment.count_in_frame
        existing_count = context.frame_counts.get(frame)
        if existing_count is not None and existing_count != count:
            return self._discard_error(
                handle,
                request,
                fragment,
                ErrorCode.LINK_FRAGMENT_CONFLICT,
                "같은 frame의 announced fragment count가 충돌합니다",
            )

        position_key = (frame, position)
        existing_key = context.position_keys.get(position_key)
        if existing_key is not None:
            existing = context.accepted[existing_key]
            if existing.body == body:
                return Ok(
                    FragmentProcessResult(
                        FragmentProcessStatus.DUPLICATE_IGNORED,
                        handle,
                        fragment,
                        context.route,
                        None,
                        None,
                        (request.packet_id,),
                    )
                )
            return self._discard_error(
                handle,
                request,
                fragment,
                ErrorCode.LINK_FRAGMENT_CONFLICT,
                "같은 logical fragment 위치의 bytes가 충돌합니다",
            )

        payload_size = len(body.fragment_payload)
        if context.accepted_bytes + payload_size > MAX_SEGMENT_BYTES:
            return self._discard_error(
                handle,
                request,
                fragment,
                ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
                "reassembly bytes가 최대 Segment 크기를 넘습니다",
            )

        full_key = (frame, position, fragment.global_fragment_number)
        stored = _StoredFragment(body, request.packet_id, fragment)
        context.accepted[full_key] = stored
        context.position_keys[position_key] = full_key
        context.pending.add(full_key)
        context.frame_counts[frame] = count
        context.accepted_bytes += payload_size

        while True:
            next_count = context.frame_counts.get(context.next_frame)
            if next_count is None:
                break
            next_position_key = (context.next_frame, context.next_position)
            next_full_key = context.position_keys.get(next_position_key)
            if next_full_key is None:
                break
            next_fragment = context.accepted[next_full_key]
            if next_fragment.fragment.global_fragment_number != context.expected_global:
                return self._discard_error(
                    handle,
                    request,
                    fragment,
                    ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
                    "global fragment number가 modulo 256 순서와 다릅니다",
                )

            context.pending.remove(next_full_key)
            context.assembled.extend(next_fragment.body.fragment_payload)
            context.ordered_packet_ids.append(next_fragment.packet_id)

            if len(context.assembled) >= SEGMENT_HEADER_SIZE and context.declared_total is None:
                type_and_id = int.from_bytes(context.assembled[:2], "big")
                segment_type = type_and_id >> 14
                if segment_type != 0:
                    return self._discard_error(
                        handle,
                        request,
                        fragment,
                        ErrorCode.LINK_UNSUPPORTED_SEGMENT_TYPE,
                        "Data Session에서 지원하지 않는 Segment Type입니다",
                    )
                context.sequential_id = SequentialId(type_and_id & 0x3FFF)
                context.declared_total = SEGMENT_HEADER_SIZE + int.from_bytes(
                    context.assembled[2:4], "big"
                )

            if context.declared_total is not None and len(context.assembled) > context.declared_total:
                return self._discard_error(
                    handle,
                    request,
                    fragment,
                    ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
                    "fragment bytes가 declared Segment 길이를 넘습니다",
                )

            complete = (
                context.declared_total is not None
                and len(context.assembled) == context.declared_total
            )
            is_frame_terminal = context.next_position == next_count
            continuation = _continuation(next_fragment.body)
            if continuation == 0 and not complete:
                return self._discard_error(
                    handle,
                    request,
                    fragment,
                    ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
                    "Segment 완성 전에 continue_resource_usage가 0입니다",
                )
            if complete:
                expected_terminal_type = (
                    DataStartFragmentBody if next_count == 1 else DataEndFragmentBody
                )
                if (
                    not is_frame_terminal
                    or not isinstance(next_fragment.body, expected_terminal_type)
                    or continuation != 0
                ):
                    return self._discard_error(
                        handle,
                        request,
                        fragment,
                        ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
                        "Segment가 frame 마지막 continue=0 경계 밖에서 완성됐습니다",
                    )
                if context.pending:
                    return self._discard_error(
                        handle,
                        request,
                        fragment,
                        ErrorCode.LINK_SEGMENT_LENGTH_INVALID,
                        "완성 Segment 뒤의 추가 fragment bytes가 존재합니다",
                    )
                decoded = decode_segment(bytes(context.assembled))
                if decoded.is_err:
                    self._contexts.pop(handle, None)
                    return decoded
                result = FragmentProcessResult(
                    FragmentProcessStatus.COMPLETE,
                    handle,
                    fragment,
                    context.route,
                    decoded.value.sequential_id,
                    decoded.value.presentation_data,
                    tuple(context.ordered_packet_ids),
                )
                del self._contexts[handle]
                return Ok(result)

            context.expected_global = (context.expected_global + 1) % 256
            if is_frame_terminal:
                if context.next_frame == MAX_FRAME_INDEX:
                    return self._discard_error(
                        handle,
                        request,
                        fragment,
                        ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
                        "미완성 Segment가 session_frame_index wrap 경계에 도달했습니다",
                    )
                context.next_frame += 1
                context.next_position = 1
            else:
                context.next_position += 1

        return Ok(
            FragmentProcessResult(
                FragmentProcessStatus.INCOMPLETE,
                handle,
                fragment,
                context.route,
                None,
                None,
                (request.packet_id,),
            )
        )
