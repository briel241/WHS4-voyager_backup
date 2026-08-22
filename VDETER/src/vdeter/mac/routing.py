"""Duplex 방향 판정과 ROC relay route (`DR-STAGE4-MAC-011`).

`INSIDE_CONFIGURED`에서는 위치·GNSS 비교 없이 세 endpoint를 ROC service area 내부로
취급한다. TBB의 geographic area field는 Link가 wire 그대로 보존·검증하고 MAC snapshot에도
남지만 이 profile의 내부/외부 판정에는 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..common.addressing import Address, AddressKind, RouteAddresses
from ..common.contracts.mac_execution import (
    AckSlotFunction,
    MacFailureReason,
    MacReasonCode,
)
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import PhysicalChannel, StationId
from ..common.results import Ok, internal_error
from .deployment import MacDeploymentProfile
from .resources import Direction
from .tbb import LOWER_CHANNEL, SlotFunction, UPPER_CHANNEL


class ShortDataDirection(str, Enum):
    """Stage 4 canonical Short Data 방향."""

    ROC_TO_MASS = "ROC_TO_MASS"
    MASS_TO_ROC = "MASS_TO_ROC"
    MASS_TO_MASS = "MASS_TO_MASS"
    ROC_BROADCAST = "ROC_BROADCAST"


@dataclass(frozen=True, slots=True)
class ShortDataPlacement:
    """Short Data와 그 ACK의 Physical Channel·slot function 배치."""

    direction: ShortDataDirection
    tx_physical_channel: PhysicalChannel
    tx_slot_function: SlotFunction
    ack_physical_channel: PhysicalChannel | None
    ack_slot_function: AckSlotFunction | None


#: mobile station이 송신하면 RAC, ROC Control station이 송신하면 ASC를 사용한다.
_SHORT_DATA_PLACEMENTS = {
    ShortDataDirection.ROC_TO_MASS: ShortDataPlacement(
        direction=ShortDataDirection.ROC_TO_MASS,
        tx_physical_channel=UPPER_CHANNEL,
        tx_slot_function=SlotFunction.ASC,
        ack_physical_channel=LOWER_CHANNEL,
        ack_slot_function=AckSlotFunction.RAC,
    ),
    ShortDataDirection.MASS_TO_ROC: ShortDataPlacement(
        direction=ShortDataDirection.MASS_TO_ROC,
        tx_physical_channel=LOWER_CHANNEL,
        tx_slot_function=SlotFunction.RAC,
        ack_physical_channel=UPPER_CHANNEL,
        ack_slot_function=AckSlotFunction.ASC,
    ),
    ShortDataDirection.MASS_TO_MASS: ShortDataPlacement(
        direction=ShortDataDirection.MASS_TO_MASS,
        tx_physical_channel=UPPER_CHANNEL,
        tx_slot_function=SlotFunction.RAC,
        ack_physical_channel=UPPER_CHANNEL,
        ack_slot_function=AckSlotFunction.RAC,
    ),
    ShortDataDirection.ROC_BROADCAST: ShortDataPlacement(
        direction=ShortDataDirection.ROC_BROADCAST,
        tx_physical_channel=UPPER_CHANNEL,
        tx_slot_function=SlotFunction.ASC,
        ack_physical_channel=None,
        ack_slot_function=None,
    ),
}


def _error(code: str, category: ErrorCategory, message: str, reason, reason_code=None, **extra):
    details = dict(extra)
    details["mac_reason"] = reason.value if isinstance(reason, MacFailureReason) else str(reason)
    if reason_code is not None:
        details["reason_code"] = reason_code
    return internal_error(
        Layer.MAC, code, category, message, details=Details.of(**details)
    )


def unexpected_control_station_error(message: str, **extra):
    return _error(
        ErrorCode.UNSUPPORTED_REQUEST,
        ErrorCategory.UNSUPPORTED,
        message,
        MacFailureReason.FEATURE_NOT_SUPPORTED,
        MacReasonCode.UNEXPECTED_CONTROL_STATION,
        **extra,
    )


def invalid_request_error(message: str, reason=MacFailureReason.EVENT_INVALID, **extra):
    return _error(
        ErrorCode.INVALID_REQUEST, ErrorCategory.VALIDATION, message, reason, None, **extra
    )


def feature_not_supported_error(message: str, reason_code: str, **extra):
    return _error(
        ErrorCode.UNSUPPORTED_REQUEST,
        ErrorCategory.UNSUPPORTED,
        message,
        MacFailureReason.FEATURE_NOT_SUPPORTED,
        reason_code,
        **extra,
    )


def _is_node_equal_original(route: RouteAddresses) -> bool:
    if route.node_source is None or route.node_destination is None:
        return False
    if route.node_source.station_id.value != route.original_source.station_id.value:
        return False
    original = route.original_destination.address
    node = route.node_destination.address
    if original.kind is not node.kind:
        return False
    if original.kind is AddressKind.BROADCAST:
        return True
    return original.station_id.value == node.station_id.value


def resolve_short_data_direction(
    *,
    profile: MacDeploymentProfile,
    local_station_id: StationId,
    route: RouteAddresses,
    ack_required: bool,
):
    """Short Data route를 검증하고 canonical 방향·배치를 결정한다.

    Short Data는 ROC relay를 사용하지 않는 한 hop이므로 Original과 Node가 같아야 한다.
    """

    source = route.original_source.station_id
    destination = route.original_destination.address
    if source.value != local_station_id.value:
        return invalid_request_error(
            "Short Data의 original_source는 로컬 endpoint여야 합니다",
            original_source=source.value,
            local_station_id=local_station_id.value,
        )
    if route.node_source is not None or route.node_destination is not None:
        if not _is_node_equal_original(route):
            return invalid_request_error(
                "Short Data는 한 hop이므로 Original과 Node 주소가 같아야 합니다"
            )
    is_roc = local_station_id.value == profile.roc_station_id.value

    if destination.kind is AddressKind.BROADCAST:
        if ack_required:
            return invalid_request_error(
                "Type 92는 ACK가 필요하므로 destination이 반드시 Addressed여야 합니다"
            )
        if not is_roc:
            return feature_not_supported_error(
                "single-leg fixture에서 MASS-originated broadcast는 지원하지 않습니다",
                MacReasonCode.MASS_BROADCAST_NOT_SUPPORTED,
            )
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.ROC_BROADCAST])

    peer = destination.station_id
    if not profile.is_member(peer):
        return unexpected_control_station_error(
            "구성 밖 addressed destination입니다", destination_station_id=peer.value
        )
    if peer.value == local_station_id.value:
        return invalid_request_error("Short Data destination이 로컬 endpoint와 같습니다")
    if is_roc:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.ROC_TO_MASS])
    if peer.value == profile.roc_station_id.value:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.MASS_TO_ROC])
    return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.MASS_TO_MASS])


def receive_short_data_placement(
    *,
    profile: MacDeploymentProfile,
    local_station_id: StationId,
    sender_station_id: StationId,
    broadcast: bool,
):
    """수신 endpoint가 ACK를 보낼 배치를 결정한다."""

    if broadcast:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.ROC_BROADCAST])
    is_roc = local_station_id.value == profile.roc_station_id.value
    sender_is_roc = sender_station_id.value == profile.roc_station_id.value
    if is_roc and not sender_is_roc:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.MASS_TO_ROC])
    if not is_roc and sender_is_roc:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.ROC_TO_MASS])
    if not is_roc and not sender_is_roc:
        return Ok(_SHORT_DATA_PLACEMENTS[ShortDataDirection.MASS_TO_MASS])
    return unexpected_control_station_error(
        "ROC가 다른 Control station의 Short Data를 받았습니다",
        sender_station_id=sender_station_id.value,
    )


def placement_for(direction: ShortDataDirection) -> ShortDataPlacement:
    return _SHORT_DATA_PLACEMENTS[direction]


@dataclass(frozen=True, slots=True)
class DataSessionPlan:
    """Data Session의 방향, 요청 배치와 relay 필요 여부."""

    direction: Direction
    relay_required: bool
    first_hop_peer: StationId
    final_destination: StationId
    request_physical_channel: PhysicalChannel
    request_slot_function: SlotFunction


def resolve_data_session_plan(
    *,
    profile: MacDeploymentProfile,
    local_station_id: StationId,
    route: RouteAddresses,
):
    """Data Session route를 검증하고 방향·relay 여부를 결정한다.

    `INSIDE_CONFIGURED`에서 addressed MASS↔MASS Data Session은 항상 ROC를 경유한다.
    """

    source = route.original_source.station_id
    destination = route.original_destination.address
    if source.value != local_station_id.value:
        return invalid_request_error(
            "Data Session의 original_source는 로컬 endpoint여야 합니다",
            original_source=source.value,
            local_station_id=local_station_id.value,
        )
    if destination.kind is AddressKind.BROADCAST:
        return feature_not_supported_error(
            "broadcast Data Session의 할당·ACK·완료 의미는 구현하지 않습니다",
            MacReasonCode.DATA_SESSION_BROADCAST_NOT_SUPPORTED,
        )
    final = destination.station_id
    if not profile.is_member(final):
        return unexpected_control_station_error(
            "구성 밖 addressed Data Session route입니다", destination_station_id=final.value
        )
    if final.value == local_station_id.value:
        return invalid_request_error("Data Session destination이 로컬 endpoint와 같습니다")

    is_roc = local_station_id.value == profile.roc_station_id.value
    if is_roc:
        return Ok(
            DataSessionPlan(
                direction=Direction.ROC_TO_SHIP,
                relay_required=False,
                first_hop_peer=final,
                final_destination=final,
                request_physical_channel=UPPER_CHANNEL,
                request_slot_function=SlotFunction.ASC,
            )
        )
    relay_required = final.value != profile.roc_station_id.value
    return Ok(
        DataSessionPlan(
            direction=Direction.SHIP_TO_ROC,
            relay_required=relay_required,
            first_hop_peer=profile.roc_station_id,
            final_destination=final,
            request_physical_channel=LOWER_CHANNEL,
            request_slot_function=SlotFunction.RAC,
        )
    )


def validate_incoming_resource_request(
    *,
    profile: MacDeploymentProfile,
    local_station_id: StationId,
    original_source: StationId,
    node_source: StationId,
    node_destination: Address,
    original_destination: Address,
):
    """ROC가 받은 Type 90의 route를 검증한다 (`DR-STAGE4-MAC-007`)."""

    if local_station_id.value != profile.roc_station_id.value:
        return unexpected_control_station_error(
            "Control station이 아닌 endpoint가 Type 90을 처리할 수 없습니다"
        )
    if original_source.value != node_source.value:
        return invalid_request_error(
            "Ship-originated 요청은 original_source와 node_source가 같아야 합니다"
        )
    if not profile.is_member(node_source) or node_source.value == profile.roc_station_id.value:
        return unexpected_control_station_error(
            "구성 밖 MASS의 Type 90입니다", node_source_id=node_source.value
        )
    if (
        node_destination.kind is not AddressKind.ADDRESSED
        or node_destination.station_id.value != profile.roc_station_id.value
    ):
        return unexpected_control_station_error("node_destination이 현재 ROC가 아닙니다")
    if original_destination.kind is AddressKind.BROADCAST:
        return feature_not_supported_error(
            "broadcast Data Session은 지원하지 않습니다",
            MacReasonCode.DATA_SESSION_BROADCAST_NOT_SUPPORTED,
        )
    final = original_destination.station_id
    if not profile.is_member(final):
        return unexpected_control_station_error(
            "구성 밖 addressed route입니다", destination_station_id=final.value
        )
    return Ok(final)


def relay_second_hop_route(
    *, original_route: RouteAddresses, roc_station_id: StationId
) -> RouteAddresses:
    """relay second hop은 Original을 보존하고 Node만 ROC→최종 MASS로 바꾼다."""

    from ..common.addressing import NodeDestination, NodeSource

    return RouteAddresses(
        original_source=original_route.original_source,
        original_destination=original_route.original_destination,
        node_source=NodeSource(roc_station_id),
        node_destination=NodeDestination(original_route.original_destination.address),
    )


def receive_physical_channel(is_control: bool) -> PhysicalChannel:
    """ROC는 lower leg, MASS는 upper leg를 수신한다."""

    return LOWER_CHANNEL if is_control else UPPER_CHANNEL


def transmit_physical_channel(is_control: bool) -> PhysicalChannel:
    return UPPER_CHANNEL if is_control else LOWER_CHANNEL


__all__ = (
    "DataSessionPlan",
    "ShortDataDirection",
    "ShortDataPlacement",
    "feature_not_supported_error",
    "invalid_request_error",
    "placement_for",
    "receive_physical_channel",
    "receive_short_data_placement",
    "relay_second_hop_route",
    "resolve_data_session_plan",
    "resolve_short_data_direction",
    "transmit_physical_channel",
    "unexpected_control_station_error",
    "validate_incoming_resource_request",
)
