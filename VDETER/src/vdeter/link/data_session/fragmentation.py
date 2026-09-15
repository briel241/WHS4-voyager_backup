"""Type 74/75/76 Data Session 송신 segmentation context."""

from __future__ import annotations

from ...common.addressing import Address, RouteAddresses
from ...common.contracts.link import DataFragmentRef
from ...common.contracts.message import (
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
)
from ...common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ...common.identifiers import LinkId, SequentialId, SessionHandle, StationId, TransferId
from ...common.results import InternalResult, Ok, internal_error
from ..packet.builder import build_packet
from ..segment.codec import encode_unsigned_segment
from .models import DataFragmentBuildOutput

MAX_FRAME_INDEX = (1 << 32) - 1
MAX_FRAME_FRAGMENTS = 14


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    transfer_id: TransferId | None = None,
    session_handle: SessionHandle | None = None,
    sequential_id: SequentialId | None = None,
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
            transfer_id=transfer_id,
            session_handle=session_handle,
            sequential_id=sequential_id,
            fragment_index=fragment_index,
            fragment_count=fragment_count,
        ),
        details=Details.of(**details) if details else Details(),
    )


def _wire_route(route: RouteAddresses) -> InternalResult[tuple[StationId, Address]]:
    if route.node_source is None and route.node_destination is None:
        source = route.original_source.station_id
        destination = route.original_destination.address
    elif route.node_source is not None and route.node_destination is not None:
        source = route.node_source.station_id
        destination = route.node_destination.address
    else:
        return _error(
            ErrorCode.LINK_DATA_ROUTE_INCOMPLETE,
            ErrorCategory.VALIDATION,
            "Data Session route의 node source/destination pair가 불완전합니다",
        )
    if source.value == 0:
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Data Session Source ID 0은 허용하지 않습니다",
        )
    return Ok((source, destination))


class DataSessionTxContext:
    """Segment bytes와 frame/global fragment 진행을 한 transfer에 결합한다."""

    __slots__ = (
        "transfer_id",
        "session_handle",
        "route",
        "sequential_id",
        "_source_id",
        "_destination",
        "_segment",
        "_offset",
        "_global_fragment_number",
        "_expected_frame_index",
        "_current_frame_count",
        "_next_position",
        "_link_id",
        "_cache",
    )

    def __init__(
        self,
        transfer_id: TransferId,
        session_handle: SessionHandle,
        route: RouteAddresses,
        sequential_id: SequentialId,
        source_id: StationId,
        destination: Address,
        segment: bytes,
    ) -> None:
        self.transfer_id = transfer_id
        self.session_handle = session_handle
        self.route = route
        self.sequential_id = sequential_id
        self._source_id = source_id
        self._destination = destination
        self._segment = segment
        self._offset = 0
        self._global_fragment_number = 0
        self._expected_frame_index = 0
        self._current_frame_count: int | None = None
        self._next_position = 1
        self._link_id: LinkId | None = None
        self._cache: dict[tuple[int, int], DataFragmentBuildOutput] = {}

    @property
    def segment_size(self) -> int:
        return len(self._segment)

    @property
    def remaining_bytes(self) -> int:
        return len(self._segment) - self._offset

    @property
    def complete(self) -> bool:
        return self._offset == len(self._segment)

    @property
    def built_bytes(self) -> int:
        """Return the end offset of the logical fragments built so far."""

        return self._offset

    @property
    def link_id(self) -> LinkId | None:
        return self._link_id

    def repartition_from_committed_prefix(
        self, committed_prefix_bytes: int
    ) -> InternalResult["DataSessionTxContext"]:
        """Create an atomic profile-switch candidate without changing this context.

        The transfer manager owns the SUCCESS/DISCARDED proof.  This lower-level
        context only validates the selected byte boundary, preserves frame and
        global-fragment progress, and forgets logical-fragment retry cache entries
        belonging to the previous profile.
        """

        if (
            isinstance(committed_prefix_bytes, bool)
            or not isinstance(committed_prefix_bytes, int)
            or not 0 <= committed_prefix_bytes <= self._offset
        ):
            return _error(
                ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
                ErrorCategory.PROTOCOL,
                "committed prefix is outside the built Segment range",
                transfer_id=self.transfer_id,
                session_handle=self.session_handle,
                sequential_id=self.sequential_id,
                committed_prefix_bytes=committed_prefix_bytes
                if isinstance(committed_prefix_bytes, int)
                and not isinstance(committed_prefix_bytes, bool)
                else None,
                built_bytes=self._offset,
            )

        candidate = DataSessionTxContext(
            self.transfer_id,
            self.session_handle,
            self.route,
            self.sequential_id,
            self._source_id,
            self._destination,
            self._segment,
        )
        candidate._offset = committed_prefix_bytes
        candidate._global_fragment_number = self._global_fragment_number
        candidate._expected_frame_index = self._expected_frame_index
        candidate._current_frame_count = self._current_frame_count
        candidate._next_position = self._next_position
        return Ok(candidate)

    def build_fragment(
        self,
        link_id: LinkId,
        session_frame_index: int,
        frame_fragment_count: int,
        position_in_frame: int,
        message_registry,
        link_profile_registry,
    ) -> InternalResult[DataFragmentBuildOutput]:
        """MAC이 계획한 현재 frame 위치의 Packet 하나를 만든다."""

        safe_position = (
            position_in_frame
            if isinstance(position_in_frame, int)
            and not isinstance(position_in_frame, bool)
            and position_in_frame >= 0
            else None
        )
        safe_count = (
            frame_fragment_count
            if isinstance(frame_fragment_count, int)
            and not isinstance(frame_fragment_count, bool)
            and frame_fragment_count >= 0
            else None
        )
        correlation = dict(
            transfer_id=self.transfer_id,
            session_handle=self.session_handle,
            sequential_id=self.sequential_id,
            fragment_index=safe_position,
            fragment_count=safe_count,
        )
        if not isinstance(link_id, LinkId):
            return _error(
                ErrorCode.LINK_UNSUPPORTED_LINK_ID,
                ErrorCategory.VALIDATION,
                "LinkId가 필요합니다",
                **correlation,
            )
        if not getattr(link_profile_registry, "is_frozen", False):
            return _error(
                ErrorCode.LINK_PROFILE_MISMATCH,
                ErrorCategory.VALIDATION,
                "Link Profile registry가 freeze되지 않았습니다",
                **correlation,
            )
        profile_result = link_profile_registry.lookup(link_id.value)
        if profile_result.is_err:
            return _error(
                ErrorCode.LINK_PROFILE_NOT_FOUND,
                ErrorCategory.UNSUPPORTED,
                "등록된 Link Profile이 없습니다",
                **correlation,
            )
        profile = profile_result.value
        capacity = profile.fragment_capacity_bytes
        if not isinstance(capacity, int) or capacity <= 0:
            return _error(
                ErrorCode.LINK_PROFILE_MISMATCH,
                ErrorCategory.VALIDATION,
                "Data fragment capacity가 없는 Link Profile입니다",
                **correlation,
            )
        if self._link_id is not None and link_id != self._link_id:
            return _error(
                ErrorCode.LINK_TRANSFER_PROFILE_SWITCH_UNSAFE,
                ErrorCategory.PROTOCOL,
                "PROGRESS 없이 활성 segmentation profile을 바꿀 수 없습니다",
                **correlation,
            )
        if (
            isinstance(session_frame_index, bool)
            or not isinstance(session_frame_index, int)
            or not 0 <= session_frame_index <= MAX_FRAME_INDEX
        ):
            return _error(
                ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
                ErrorCategory.VALIDATION,
                "session_frame_index 범위가 잘못되었습니다",
                **correlation,
            )
        if (
            isinstance(frame_fragment_count, bool)
            or not isinstance(frame_fragment_count, int)
            or not 1 <= frame_fragment_count <= MAX_FRAME_FRAGMENTS
        ):
            return _error(
                ErrorCode.LINK_FRAGMENT_COUNT_INVALID,
                ErrorCategory.VALIDATION,
                "frame_fragment_count는 1..14여야 합니다",
                **correlation,
            )
        if (
            isinstance(position_in_frame, bool)
            or not isinstance(position_in_frame, int)
            or not 1 <= position_in_frame <= frame_fragment_count
        ):
            return _error(
                ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
                ErrorCategory.VALIDATION,
                "position_in_frame 범위가 잘못되었습니다",
                **correlation,
            )

        cached = self._cache.get((session_frame_index, position_in_frame))
        if cached is not None:
            if cached.fragment.count_in_frame != frame_fragment_count or link_id != self._link_id:
                return _error(
                    ErrorCode.LINK_FRAGMENT_CONFLICT,
                    ErrorCategory.PROTOCOL,
                    "같은 frame 위치의 build 조건이 기존 fragment와 다릅니다",
                    **correlation,
                )
            return Ok(cached)
        if self.complete:
            return _error(
                ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
                ErrorCategory.PROTOCOL,
                "완성된 Segment 뒤에 fragment를 추가할 수 없습니다",
                **correlation,
            )
        if session_frame_index != self._expected_frame_index:
            return _error(
                ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
                ErrorCategory.PROTOCOL,
                "session_frame_index가 0부터 연속 증가하지 않았습니다",
                **correlation,
                expected_frame_index=self._expected_frame_index,
            )
        if self._current_frame_count is None:
            remaining_fragments = (self.remaining_bytes + capacity - 1) // capacity
            if frame_fragment_count > remaining_fragments:
                return _error(
                    ErrorCode.LINK_FRAGMENT_COUNT_INVALID,
                    ErrorCategory.PROTOCOL,
                    "announced frame fragment count가 남은 Segment 조각 수보다 큽니다",
                    **correlation,
                    remaining_fragments=remaining_fragments,
                )
            self._current_frame_count = frame_fragment_count
        elif frame_fragment_count != self._current_frame_count:
            return _error(
                ErrorCode.LINK_FRAGMENT_CONFLICT,
                ErrorCategory.PROTOCOL,
                "같은 frame의 announced fragment count가 바뀌었습니다",
                **correlation,
            )
        if position_in_frame != self._next_position:
            return _error(
                ErrorCode.LINK_FRAGMENT_NUMBER_INVALID,
                ErrorCategory.PROTOCOL,
                "송신 fragment position이 연속적이지 않습니다",
                **correlation,
                expected_position=self._next_position,
            )

        fragment_payload = self._segment[self._offset : self._offset + capacity]
        if not fragment_payload:
            return _error(
                ErrorCode.LINK_DATA_FRAGMENT_PAYLOAD_EMPTY,
                ErrorCategory.PROTOCOL,
                "빈 Data fragment를 만들 수 없습니다",
                **correlation,
            )
        segment_offset = self._offset
        segment_complete = segment_offset + len(fragment_payload) == len(self._segment)
        global_number = self._global_fragment_number
        is_last_in_frame = position_in_frame == frame_fragment_count
        continue_resource_usage = 0 if segment_complete else 1
        if is_last_in_frame and session_frame_index == MAX_FRAME_INDEX and not segment_complete:
            return _error(
                ErrorCode.LINK_SEGMENT_SESSION_BOUNDARY,
                ErrorCategory.PROTOCOL,
                "session_frame_index가 wrap 경계에 도달했습니다",
                **correlation,
            )

        if frame_fragment_count == 1 or position_in_frame == 1:
            body = DataStartFragmentBody(
                self._source_id,
                0,
                self._destination,
                frame_fragment_count,
                global_number,
                continue_resource_usage,
                fragment_payload,
            )
        elif is_last_in_frame:
            body = DataEndFragmentBody(
                self._source_id,
                0,
                self._destination,
                frame_fragment_count,
                global_number,
                continue_resource_usage,
                fragment_payload,
            )
        else:
            body = DataContinuationFragmentBody(
                self._source_id,
                0,
                self._destination,
                frame_fragment_count,
                position_in_frame,
                global_number,
                fragment_payload,
            )

        packet = build_packet(link_id, (body,), message_registry, link_profile_registry)
        if packet.is_err:
            return packet
        fragment = DataFragmentRef(
            session_frame_index=session_frame_index,
            position_in_frame=position_in_frame,
            count_in_frame=frame_fragment_count,
            global_fragment_number=global_number,
        )
        output = DataFragmentBuildOutput(
            body=body,
            packet=packet.value,
            fragment=fragment,
            segment_offset=segment_offset,
            segment_length=len(fragment_payload),
            segment_complete=segment_complete,
        )

        self._cache[(session_frame_index, position_in_frame)] = output
        self._link_id = link_id
        self._offset += len(fragment_payload)
        self._global_fragment_number = (global_number + 1) % 256
        self._next_position += 1
        if is_last_in_frame:
            self._expected_frame_index += 1
            self._current_frame_count = None
            self._next_position = 1
        return Ok(output)


def create_data_session_tx_context(
    transfer_id: TransferId,
    session_handle: SessionHandle,
    route: RouteAddresses,
    sequential_id: SequentialId,
    presentation_data: bytes,
) -> InternalResult[DataSessionTxContext]:
    """최초 successful build 직전 고정할 Data Session Segment context를 만든다."""

    if not isinstance(transfer_id, TransferId):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "TransferId가 필요합니다",
        )
    if not isinstance(session_handle, SessionHandle):
        return _error(
            ErrorCode.LINK_UNKNOWN_SESSION,
            ErrorCategory.VALIDATION,
            "SessionHandle이 필요합니다",
            transfer_id=transfer_id,
        )
    if not isinstance(route, RouteAddresses):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "Data Session route가 필요합니다",
            transfer_id=transfer_id,
            session_handle=session_handle,
        )
    if not isinstance(sequential_id, SequentialId):
        return _error(
            ErrorCode.LINK_INVALID_MESSAGE_FIELD,
            ErrorCategory.VALIDATION,
            "SequentialId가 필요합니다",
            transfer_id=transfer_id,
            session_handle=session_handle,
        )
    wire_route = _wire_route(route)
    if wire_route.is_err:
        return wire_route
    segment = encode_unsigned_segment(sequential_id, presentation_data)
    if segment.is_err:
        return segment
    source_id, destination = wire_route.value
    return Ok(
        DataSessionTxContext(
            transfer_id,
            session_handle,
            route,
            sequential_id,
            source_id,
            destination,
            segment.value,
        )
    )
