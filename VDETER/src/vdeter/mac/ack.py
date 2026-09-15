"""ACK ledger, 수신 delivery 중복 억제와 재-ACK 계획 (`DR-STAGE4-MAC-010`).

- Data Session은 최근 세 frame rolling ACK ledger와 선택 재전송을 사용한다.
- 수신 application delivery는 bounded `RecentRxDeliveryLedger`를 통과해야 한다.
- payload digest와 원문은 protected memory에만 두고 Service event·log·오류 details에
  기록하지 않는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..common.addressing import Address, AddressKind, RouteAddresses
from ..common.contracts.mac_execution import (
    AckProcedure,
    AckReplayContext,
    AckSlotFunction,
    RxDeliveryProcedure,
)
from ..common.contracts.message import AcknowledgeNackBody
from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.exceptions import InvalidValueError
from ..common.identifiers import SequentialId, StationId
from ..common.results import Ok, internal_error
from .access import MAX_RETRY_GAP_NANOSECONDS
from .models import (
    AckPendingState,
    DeliveryLedgerRecord,
    DeliveryRecordState,
    Stage4Limits,
)
from .tdma import FRAME_NANOSECONDS

#: `RecentRxDeliveryLedger` key의 destination tag byte.
DESTINATION_TAG_BROADCAST = 0x00
DESTINATION_TAG_ADDRESSED = 0x01

#: Data Session ACK ledger가 유지하는 frame 수.
ACK_LEDGER_FRAMES = Stage4Limits.ACK_LEDGER_FRAMES

#: Data Session과 ACK 없는 Type 93의 보존 frame 수.
SHORT_LIVED_RETENTION_FRAMES = 3

#: 정상 Data Session Type 13과 Short Data ACK의 미사용 canonical 값.
CANONICAL_SESSION_ID = 0
CANONICAL_CQI = 0
CANONICAL_ACM_OR_EDN = 0
CANONICAL_POWER_SETTING = 0
ALL_ACK_MASKS = (0, 0, 0)


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.MAC,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


def payload_digest(payload: bytes) -> bytes:
    """protected memory 전용 SHA-256 payload digest."""

    if not isinstance(payload, bytes):
        raise InvalidValueError("payload_digest: bytes여야 합니다")
    return hashlib.sha256(payload).digest()


def delivery_key(
    procedure: RxDeliveryProcedure,
    original_source: StationId,
    original_destination: Address,
    sequential_id: SequentialId,
) -> bytes:
    """canonical key byte열 (`DR-STAGE4-MAC-010`).

    procedure ASCII + `0x00` + 32-bit source BE + destination tag + (addressed면 32-bit
    destination BE) + 상위 두 bit가 0인 16-bit BE Sequential ID.
    """

    if not isinstance(procedure, RxDeliveryProcedure):
        raise InvalidValueError("delivery_key: RxDeliveryProcedure여야 합니다")
    if not isinstance(original_source, StationId):
        raise InvalidValueError("delivery_key: original_source는 StationId여야 합니다")
    if not isinstance(original_destination, Address):
        raise InvalidValueError("delivery_key: original_destination은 Address여야 합니다")
    if not isinstance(sequential_id, SequentialId):
        raise InvalidValueError("delivery_key: sequential_id는 SequentialId여야 합니다")
    parts = [
        procedure.value.encode("ascii"),
        b"\x00",
        original_source.value.to_bytes(4, "big"),
    ]
    if original_destination.kind is AddressKind.BROADCAST:
        parts.append(bytes((DESTINATION_TAG_BROADCAST,)))
    else:
        parts.append(bytes((DESTINATION_TAG_ADDRESSED,)))
        parts.append(original_destination.station_id.value.to_bytes(4, "big"))
    parts.append(sequential_id.value.to_bytes(2, "big"))
    return b"".join(parts)


def retention_frames_for(
    procedure: RxDeliveryProcedure, highest_retransmission_number: int | None
) -> int:
    """procedure별 유한 보존 frame 수.

    Type 92는 최초 관측 retransmission number `n`부터 sender의 남은 retry 전부를 덮는다.
    `n=0/1/2/3`의 보존 frame 수는 각각 `209/140/71/1`이다.
    """

    if procedure is not RxDeliveryProcedure.SHORT_DATA_92:
        return SHORT_LIVED_RETENTION_FRAMES
    number = 0 if highest_retransmission_number is None else highest_retransmission_number
    if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 3:
        raise InvalidValueError("retention_frames_for: retransmission number는 0..3이어야 합니다")
    retention_nanoseconds = (
        3 - number
    ) * MAX_RETRY_GAP_NANOSECONDS + FRAME_NANOSECONDS
    return -(-retention_nanoseconds // FRAME_NANOSECONDS)


def expiry_frame_for(
    procedure: RxDeliveryProcedure,
    completion_frame_ordinal: int,
    highest_retransmission_number: int | None,
) -> int:
    return completion_frame_ordinal + retention_frames_for(
        procedure, highest_retransmission_number
    )


def is_expired(record: DeliveryLedgerRecord, frame_ordinal: int) -> bool:
    """현재 frame ordinal이 `expires_after_frame`보다 클 때만 만료한다."""

    if record.state is DeliveryRecordState.PENDING_DELIVERY:
        return False
    if record.expires_after_frame is None:
        return False
    return frame_ordinal > record.expires_after_frame


def prune_ledger(
    ledger: tuple[DeliveryLedgerRecord, ...], frame_ordinal: int
) -> tuple[DeliveryLedgerRecord, ...]:
    """expiry를 지난 LIVE record만 결정적으로 제거한다. PENDING은 축출하지 않는다."""

    return tuple(item for item in ledger if not is_expired(item, frame_ordinal))


def ledger_sorted(
    ledger: tuple[DeliveryLedgerRecord, ...],
) -> tuple[DeliveryLedgerRecord, ...]:
    """PENDING은 `first_received_frame_ordinal`, LIVE는 `completion_frame_ordinal` 오름차순
    뒤 key canonical byte열 오름차순."""

    return tuple(sorted(ledger, key=lambda item: item.order_key()))


def ledger_has_capacity(ledger: tuple[DeliveryLedgerRecord, ...]) -> bool:
    return len(ledger) < Stage4Limits.RX_DELIVERY_LEDGER


def capacity_error():
    from ..common.contracts.mac_execution import MacFailureReason, MacReasonCode

    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "수신 delivery ledger가 포화 상태입니다",
        mac_reason=MacFailureReason.RESOURCE_UNAVAILABLE.value,
        reason_code=MacReasonCode.RX_DEDUP_CAPACITY,
    )


# --- Data Session ACK/NACK mask ---


def build_frame_mask(nack_positions: tuple[int, ...], frame_fragment_count: int) -> int:
    """송신 순서 위치(1-based)의 NACK 집합을 한 frame mask로 만든다.

    bit 0(LSB)은 Type 74 Start Fragment, bit 1부터는 송신 순서의 Type 75, 마지막 실제
    fragment에 대응하는 bit는 Type 76 End Fragment다. `0=ACK`, `1=NACK`이며 실제
    `frame_fragment_count` 밖의 bit는 0이다.
    """

    if not 1 <= frame_fragment_count <= 14:
        raise InvalidValueError("build_frame_mask: frame_fragment_count는 1..14여야 합니다")
    mask = 0
    for position in nack_positions:
        if not 1 <= position <= frame_fragment_count:
            raise InvalidValueError("build_frame_mask: 위치가 frame fragment 범위를 벗어났습니다")
        mask |= 1 << (position - 1)
    return mask


def nack_positions_from_mask(mask: int, frame_fragment_count: int) -> tuple[int, ...]:
    """mask에서 NACK인 fragment의 송신 순서 위치(1-based)를 뽑는다."""

    if not 1 <= frame_fragment_count <= 14:
        raise InvalidValueError("nack_positions_from_mask: frame_fragment_count는 1..14여야 합니다")
    return tuple(
        position
        for position in range(1, frame_fragment_count + 1)
        if mask & (1 << (position - 1))
    )


def mask_has_out_of_range_bits(mask: int, frame_fragment_count: int) -> bool:
    """실제 `frame_fragment_count` 밖의 bit가 1인지 검사한다."""

    return bool(mask & ~((1 << frame_fragment_count) - 1))


def build_ack_body(
    *,
    source_id: StationId,
    destination: Address,
    masks: tuple[int, int, int] = ALL_ACK_MASKS,
) -> AcknowledgeNackBody:
    """정상 Data Session Type 13과 Short Data ACK의 canonical body."""

    return AcknowledgeNackBody(
        source_id=source_id,
        destination=destination,
        session_id=CANONICAL_SESSION_ID,
        ack_nack_masks=masks,
        cqi=CANONICAL_CQI,
        acm_or_edn=CANONICAL_ACM_OR_EDN,
        power_setting=CANONICAL_POWER_SETTING,
    )


def is_full_ack(body: AcknowledgeNackBody) -> bool:
    """세 mask가 모두 0이면 ACK다."""

    return body.ack_nack_masks == ALL_ACK_MASKS


# --- ACK replay ---


def build_short_data_replay_context(
    *,
    route: RouteAddresses,
    link_id,
    physical_channel,
    slot_function: AckSlotFunction,
    ack_body: AcknowledgeNackBody,
    resource_last_frame_ordinal: int,
) -> AckReplayContext:
    """Short Data ACK의 replay context. RAC/ASC는 replay마다 새 slot을 고르므로 LC가 없다."""

    if slot_function is AckSlotFunction.DSCH:
        raise InvalidValueError("Short Data ACK는 RAC 또는 ASC를 사용합니다")
    return AckReplayContext(
        ack_procedure=AckProcedure.SHORT_DATA_92,
        route=route,
        link_id=link_id,
        physical_channel=physical_channel,
        slot_function=slot_function,
        logical_channel=None,
        ack_body=ack_body,
        resource_last_frame_ordinal=resource_last_frame_ordinal,
    )


def build_data_session_replay_context(
    *,
    route: RouteAddresses,
    link_id,
    physical_channel,
    logical_channel: int,
    ack_body: AcknowledgeNackBody,
    resource_last_frame_ordinal: int,
) -> AckReplayContext:
    """Data Session ACK는 원 data sender의 Type 4 `logical_channel_rx` 고정 DSCH를 쓴다."""

    return AckReplayContext(
        ack_procedure=AckProcedure.DATA_SESSION,
        route=route,
        link_id=link_id,
        physical_channel=physical_channel,
        slot_function=AckSlotFunction.DSCH,
        logical_channel=logical_channel,
        ack_body=ack_body,
        resource_last_frame_ordinal=resource_last_frame_ordinal,
    )


def effective_replay_horizon(
    context: AckReplayContext, tbb_last_valid_frame_ordinal: int
) -> int:
    """`min(resource_last_frame_ordinal, TBB valid_until 직전 frame ordinal)`."""

    return min(context.resource_last_frame_ordinal, tbb_last_valid_frame_ordinal)


def may_schedule_replay(
    record: DeliveryLedgerRecord, current_frame_ordinal: int
) -> bool:
    """record당 동시에 하나, frame당 최대 하나의 replay만 허용한다."""

    if record.replay_context is None:
        return False
    if record.replay_command_id is not None:
        return False
    if record.last_replay_scheduled_frame == current_frame_ordinal:
        return False
    return True


def replay_resource_expired_error():
    from ..common.contracts.mac_execution import MacFailureReason, MacReasonCode

    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "재-ACK에 사용할 수 있는 자원이 유효기간 안에 없습니다",
        mac_reason=MacFailureReason.RESOURCE_UNAVAILABLE.value,
        reason_code=MacReasonCode.ACK_REPLAY_RESOURCE_EXPIRED,
    )


@dataclass(frozen=True, slots=True)
class SessionFrameLedgerEntry:
    """송신 Data Session 한 frame의 ACK ledger 항목."""

    session_frame_index: int
    frame_ordinal: int
    frame_fragment_count: int
    packet_ids: tuple[str, ...]
    retransmission_counts: tuple[int, ...]
    fragment_confirmed: tuple[bool, ...] = ()
    confirmed: bool = False

    def __post_init__(self) -> None:
        if len(self.packet_ids) != self.frame_fragment_count:
            raise InvalidValueError("SessionFrameLedgerEntry: packet_ids 수가 fragment 수와 다릅니다")
        if len(self.retransmission_counts) != self.frame_fragment_count:
            raise InvalidValueError(
                "SessionFrameLedgerEntry: retransmission_counts 수가 fragment 수와 다릅니다"
            )
        if self.fragment_confirmed and len(self.fragment_confirmed) != self.frame_fragment_count:
            raise InvalidValueError(
                "SessionFrameLedgerEntry: fragment_confirmed 수가 fragment 수와 다릅니다"
            )

    @property
    def confirmed_positions(self) -> tuple[bool, ...]:
        if self.fragment_confirmed:
            return self.fragment_confirmed
        return (False,) * self.frame_fragment_count


def trimmed_ledger(
    entries: tuple[SessionFrameLedgerEntry, ...],
) -> tuple[SessionFrameLedgerEntry, ...]:
    """최근 세 frame ACK ledger만 유지한다."""

    if len(entries) <= ACK_LEDGER_FRAMES:
        return entries
    return entries[len(entries) - ACK_LEDGER_FRAMES :]


def mask_index_for_frame(latest_frame_index: int, target_frame_index: int) -> int | None:
    """mask 2/1/0은 최신/이전/세 번째 이전 Data Session frame이다."""

    offset = latest_frame_index - target_frame_index
    if offset == 0:
        return 2
    if offset == 1:
        return 1
    if offset == 2:
        return 0
    return None


def frame_index_for_mask(latest_frame_index: int, mask_index: int) -> int | None:
    if mask_index == 2:
        return latest_frame_index
    if mask_index == 1:
        return latest_frame_index - 1
    if mask_index == 0:
        return latest_frame_index - 2
    return None


def new_pending_record(
    *,
    key: bytes,
    procedure: RxDeliveryProcedure,
    payload_length: int,
    digest: bytes,
    first_received_frame_ordinal: int,
    highest_retransmission_number: int | None,
    pending_delivery_key: tuple | None,
) -> DeliveryLedgerRecord:
    return DeliveryLedgerRecord(
        key=key,
        procedure=procedure,
        state=DeliveryRecordState.PENDING_DELIVERY,
        payload_length=payload_length,
        payload_digest=digest,
        first_received_frame_ordinal=first_received_frame_ordinal,
        highest_retransmission_number=highest_retransmission_number,
        pending_delivery_key=pending_delivery_key,
    )


def promoted_live_record(
    record: DeliveryLedgerRecord,
    *,
    completion_frame_ordinal: int,
    ack_state: AckPendingState,
    replay_context: AckReplayContext | None,
) -> DeliveryLedgerRecord:
    """성공 전환은 event `occurred_at_utc`와 관측 retry number로 expiry를 새로 계산한다."""

    expires = expiry_frame_for(
        record.procedure, completion_frame_ordinal, record.highest_retransmission_number
    )
    return DeliveryLedgerRecord(
        key=record.key,
        procedure=record.procedure,
        state=DeliveryRecordState.LIVE,
        payload_length=record.payload_length,
        payload_digest=record.payload_digest,
        first_received_frame_ordinal=record.first_received_frame_ordinal,
        completion_frame_ordinal=completion_frame_ordinal,
        expires_after_frame=expires,
        highest_retransmission_number=record.highest_retransmission_number,
        ack_state=ack_state,
        replay_context=replay_context,
        replay_command_id=None,
        last_replay_scheduled_frame=None,
        pending_delivery_key=None,
    )


def with_observed_retransmission(
    record: DeliveryLedgerRecord, retransmission_number: int | None
) -> DeliveryLedgerRecord:
    """더 큰 retransmission number는 갱신만 하고 expiry를 연장하지 않는다."""

    if retransmission_number is None:
        return record
    current = record.highest_retransmission_number
    if current is not None and retransmission_number <= current:
        return record
    return DeliveryLedgerRecord(
        key=record.key,
        procedure=record.procedure,
        state=record.state,
        payload_length=record.payload_length,
        payload_digest=record.payload_digest,
        first_received_frame_ordinal=record.first_received_frame_ordinal,
        completion_frame_ordinal=record.completion_frame_ordinal,
        expires_after_frame=record.expires_after_frame,
        highest_retransmission_number=retransmission_number,
        ack_state=record.ack_state,
        replay_context=record.replay_context,
        replay_command_id=record.replay_command_id,
        last_replay_scheduled_frame=record.last_replay_scheduled_frame,
        pending_delivery_key=record.pending_delivery_key,
    )


def matches_payload(record: DeliveryLedgerRecord, payload_length: int, digest: bytes) -> bool:
    return record.payload_length == payload_length and record.payload_digest == digest


def sequential_conflict_error():
    from ..common.contracts.mac_execution import MacFailureReason, MacReasonCode

    return _error(
        ErrorCode.MAC_TRANSFER_FAILED,
        ErrorCategory.PROTOCOL,
        "같은 Sequential ID key에 다른 payload가 도착했습니다",
        mac_reason=MacFailureReason.EVENT_INVALID.value,
        reason_code=MacReasonCode.RX_SEQUENTIAL_CONFLICT,
    )


__all__ = (
    "ACK_LEDGER_FRAMES",
    "ALL_ACK_MASKS",
    "DESTINATION_TAG_ADDRESSED",
    "DESTINATION_TAG_BROADCAST",
    "SHORT_LIVED_RETENTION_FRAMES",
    "SessionFrameLedgerEntry",
    "build_ack_body",
    "build_data_session_replay_context",
    "build_frame_mask",
    "build_short_data_replay_context",
    "capacity_error",
    "delivery_key",
    "effective_replay_horizon",
    "expiry_frame_for",
    "frame_index_for_mask",
    "is_expired",
    "is_full_ack",
    "ledger_has_capacity",
    "ledger_sorted",
    "mask_has_out_of_range_bits",
    "mask_index_for_frame",
    "matches_payload",
    "may_schedule_replay",
    "nack_positions_from_mask",
    "new_pending_record",
    "payload_digest",
    "promoted_live_record",
    "prune_ledger",
    "replay_resource_expired_error",
    "retention_frames_for",
    "sequential_conflict_error",
    "trimmed_ledger",
    "with_observed_retransmission",
)
