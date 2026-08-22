"""Type 0 lease, RAC 후보 선택과 Short Data limit (`DR-STAGE4-MAC-005`, `006`, `007`).

active SI 우선순위는 유효 Type 0 field(1..511 hexslots) → 활성 TBB field(1..511 hexslots)
→ 둘 다 0일 때만 M.2092-2 §A4-4.21의 official default `235 physical slots`다. 따라서
canonical fixture의 비영 TBB 값 `235`는 official default와 숫자만 같을 뿐 `235 hexslots`다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.errors import Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import PhysicalChannel
from ..common.results import Ok, internal_error
from .deployment import OUT_OF_SERVICE_STATUS, SYSTEM_STATUS_NORMAL
from .models import (
    ActiveTbb,
    ShortDataFrameCounter,
    Type0Lease,
)
from .tbb import (
    OFFICIAL_DEFAULT_SELECTION_INTERVAL_PHYSICAL_SLOTS,
    SlotFunction,
    SlotMap,
)
from .tdma import (
    CHANNEL_OFFSETS_NANOSECONDS,
    FRAME_NANOSECONDS,
    HEXSLOT_NANOSECONDS,
    NANOSECONDS_PER_MINUTE,
    TDMA_CHANNEL_COUNT,
    channel_slot_duration_nanoseconds,
    position_at_nanoseconds,
    slot_start_nanoseconds,
)

#: `DR-STAGE4-MAC-005`의 닫힌 lease 범위. SI 값과 무관하게 이 범위에서 균등 선택한다.
LEASE_SECONDS_LOW = 240
LEASE_SECONDS_HIGH = 480

#: `DR-STAGE4-MAC-006`의 최소 RAC 후보 수.
MINIMUM_RAC_CANDIDATES = 8

#: `DR-STAGE4-MAC-010`의 완료 profile 최대 field SI와 retry gap.
MAX_SELECTION_INTERVAL_FIELD = 511
MAX_SI_NANOSECONDS = MAX_SELECTION_INTERVAL_FIELD * HEXSLOT_NANOSECONDS
MAX_RETRY_GAP_NANOSECONDS = 2 * MAX_SI_NANOSECONDS + FRAME_NANOSECONDS


def _error(code: str, category: ErrorCategory, message: str, **details):
    return internal_error(
        Layer.MAC,
        code,
        category,
        message,
        details=Details.of(**details) if details else Details(),
    )


@dataclass(frozen=True, slots=True)
class ActiveSelectionInterval:
    """현재 유효한 SI와 그 출처. 단위 구분을 보존한다."""

    source: str
    field_value: int
    duration_nanoseconds: int
    is_official_default: bool


def official_default_interval_nanoseconds(reference_utc_nanoseconds: int) -> int:
    """official default `235 physical slots`의 정수 경계 지속시간.

    현재 기준 physical slot부터 235 slots 뒤 경계까지를 정수 경계 함수로 계산하며 고정
    반올림 slot 길이를 누적하지 않는다.
    """

    position = position_at_nanoseconds(reference_utc_nanoseconds)
    start = position.slot_start_nanoseconds()
    hexslot_index = position.hexslot_index
    tdma_channel = position.tdma_channel
    minute_start = position.minute_start_nanoseconds
    remaining = OFFICIAL_DEFAULT_SELECTION_INTERVAL_PHYSICAL_SLOTS
    end = start
    while remaining:
        end = end + channel_slot_duration_nanoseconds(tdma_channel)
        tdma_channel += 1
        if tdma_channel == TDMA_CHANNEL_COUNT:
            tdma_channel = 0
            hexslot_index += 1
            if hexslot_index * HEXSLOT_NANOSECONDS >= NANOSECONDS_PER_MINUTE:
                hexslot_index = 0
                minute_start += NANOSECONDS_PER_MINUTE
            end = slot_start_nanoseconds(minute_start, hexslot_index, 0)
        remaining -= 1
    return end - start


def active_selection_interval(
    *,
    lease: Type0Lease | None,
    slot_map: SlotMap | None,
    reference_utc_nanoseconds: int,
) -> ActiveSelectionInterval:
    """Type 0 → TBB → official default 순의 active SI (`DR-STAGE4-MAC-006`)."""

    if lease is not None and lease.selection_interval:
        return ActiveSelectionInterval(
            source="TYPE0",
            field_value=lease.selection_interval,
            duration_nanoseconds=lease.selection_interval * HEXSLOT_NANOSECONDS,
            is_official_default=False,
        )
    tbb_value = slot_map.selection_interval_hexslots if slot_map is not None else 0
    if tbb_value:
        return ActiveSelectionInterval(
            source="TBB",
            field_value=tbb_value,
            duration_nanoseconds=tbb_value * HEXSLOT_NANOSECONDS,
            is_official_default=False,
        )
    return ActiveSelectionInterval(
        source="OFFICIAL_DEFAULT",
        field_value=OFFICIAL_DEFAULT_SELECTION_INTERVAL_PHYSICAL_SLOTS,
        duration_nanoseconds=official_default_interval_nanoseconds(reference_utc_nanoseconds),
        is_official_default=True,
    )


def active_short_data_limit(
    *, lease: Type0Lease | None, slot_map: SlotMap | None
) -> int:
    """override lease가 있으면 그 값을, 없으면 활성 TBB 값을 사용한다."""

    if lease is not None:
        return lease.short_data_limit
    return slot_map.short_data_limit if slot_map is not None else 0


def active_system_status(lease: Type0Lease | None) -> int:
    return lease.system_status if lease is not None else SYSTEM_STATUS_NORMAL


def blocks_new_admission(lease: Type0Lease | None) -> bool:
    """`system_status=20/30`은 신규 RAC 요청·Short Data를 차단한다."""

    return active_system_status(lease) in OUT_OF_SERVICE_STATUS


def lease_seconds(random_source) -> int:
    """주입 `RandomSource`로 닫힌 정수 범위 `240..480`초에서 균등 선택한다."""

    return random_source.next_int(LEASE_SECONDS_LOW, LEASE_SECONDS_HIGH)


@dataclass(frozen=True, slots=True)
class SlotCandidate:
    """RAC/ASC/DSCH 후보 slot 하나."""

    physical_channel: PhysicalChannel
    tdma_channel: int
    slot_in_frame: int
    frame_ordinal: int
    logical_channel: int
    start_nanoseconds: int
    duration_nanoseconds: int

    @property
    def end_nanoseconds(self) -> int:
        return self.start_nanoseconds + self.duration_nanoseconds


def enumerate_function_slots(
    *,
    slot_map: SlotMap,
    function: SlotFunction,
    physical_channel: PhysicalChannel,
    from_utc_nanoseconds: int,
    until_utc_nanoseconds: int,
    transmit: bool | None = None,
) -> tuple[SlotCandidate, ...]:
    """반개구간 `[from, until)` 안에서 해당 function의 slot 시작을 오름차순으로 모은다."""

    from .tdma import EPOCH_2000_UTC_NANOSECONDS, frame_ordinal_at_nanoseconds

    if until_utc_nanoseconds <= from_utc_nanoseconds:
        return ()
    entries = slot_map.slots_for(
        physical_channel=physical_channel, function=function, transmit=transmit
    )
    if not entries:
        return ()
    first_frame = frame_ordinal_at_nanoseconds(from_utc_nanoseconds)
    last_frame = frame_ordinal_at_nanoseconds(max(until_utc_nanoseconds - 1, from_utc_nanoseconds))
    candidates: list[SlotCandidate] = []
    for frame_ordinal in range(first_frame, last_frame + 1):
        frame_start = EPOCH_2000_UTC_NANOSECONDS + frame_ordinal * FRAME_NANOSECONDS
        for entry in entries:
            start = (
                frame_start
                + entry.slot_in_frame * HEXSLOT_NANOSECONDS
                + CHANNEL_OFFSETS_NANOSECONDS[entry.tdma_channel]
            )
            if start < from_utc_nanoseconds or start >= until_utc_nanoseconds:
                continue
            candidates.append(
                SlotCandidate(
                    physical_channel=entry.physical_channel,
                    tdma_channel=entry.tdma_channel,
                    slot_in_frame=entry.slot_in_frame,
                    frame_ordinal=frame_ordinal,
                    logical_channel=entry.logical_channel,
                    start_nanoseconds=start,
                    duration_nanoseconds=channel_slot_duration_nanoseconds(entry.tdma_channel),
                )
            )
    candidates.sort(key=lambda item: (item.start_nanoseconds, item.tdma_channel))
    return tuple(candidates)


def select_random_access_slot(
    *,
    slot_map: SlotMap,
    physical_channel: PhysicalChannel,
    from_utc_nanoseconds: int,
    interval: ActiveSelectionInterval,
    random_source,
    occupied_starts: frozenset,
):
    """SI 안에서 최소 8개 후보를 확보하고 주입 난수로 균등 선택한다.

    부족하면 점유 slot을 포함하지 않고 다음 SI를 **한 번만** 계산한다. 그래도 부족하면
    무기한 SI를 넘기지 않고 `RESOURCE_UNAVAILABLE`로 끝낸다.
    """

    window_start = from_utc_nanoseconds
    for attempt in range(2):
        window_end = window_start + interval.duration_nanoseconds
        candidates = tuple(
            item
            for item in enumerate_function_slots(
                slot_map=slot_map,
                function=SlotFunction.RAC,
                physical_channel=physical_channel,
                from_utc_nanoseconds=window_start,
                until_utc_nanoseconds=window_end,
                transmit=True,
            )
            if item.start_nanoseconds not in occupied_starts
        )
        if len(candidates) >= MINIMUM_RAC_CANDIDATES:
            index = random_source.next_int(0, len(candidates) - 1)
            return Ok(candidates[index])
        if attempt == 0:
            window_start = window_end
    return _error(
        ErrorCode.MAC_RESOURCE_UNAVAILABLE,
        ErrorCategory.RESOURCE,
        "SI 두 번 안에서 최소 RAC 후보를 확보하지 못했습니다",
        minimum_candidates=MINIMUM_RAC_CANDIDATES,
        selection_interval_source=interval.source,
    )


def short_data_counter_key(
    physical_channel: PhysicalChannel, utc_nanoseconds: int
) -> tuple:
    """`(physical_channel, minute_start_utc.nanoseconds, tdma_frame_index 0..24)`."""

    position = position_at_nanoseconds(utc_nanoseconds)
    return (
        physical_channel.name,
        position.minute_start_nanoseconds,
        position.tdma_frame_index,
    )


def short_data_budget_available(
    *, counters: tuple, key: tuple, limit: int
) -> bool:
    """limit이 0이면 제한이 없다고 보지 않고 송신을 허용하지 않는다."""

    if limit <= 0:
        return False
    for counter in counters:
        if counter.key == key:
            return counter.count < limit
    return True


def incremented_counter(counters: tuple, key: tuple) -> ShortDataFrameCounter:
    for counter in counters:
        if counter.key == key:
            return ShortDataFrameCounter(
                physical_channel_name=counter.physical_channel_name,
                minute_start_nanoseconds=counter.minute_start_nanoseconds,
                tdma_frame_index=counter.tdma_frame_index,
                count=counter.count + 1,
            )
    return ShortDataFrameCounter(
        physical_channel_name=key[0],
        minute_start_nanoseconds=key[1],
        tdma_frame_index=key[2],
        count=1,
    )


def tbb_slot_map(active: ActiveTbb | None, bootstrap: SlotMap) -> SlotMap:
    """활성 TBB가 있으면 그 slot map, 없으면 bootstrap을 쓴다."""

    return bootstrap if active is None else active.slot_map


__all__ = (
    "ActiveSelectionInterval",
    "LEASE_SECONDS_HIGH",
    "LEASE_SECONDS_LOW",
    "MAX_RETRY_GAP_NANOSECONDS",
    "MAX_SELECTION_INTERVAL_FIELD",
    "MAX_SI_NANOSECONDS",
    "MINIMUM_RAC_CANDIDATES",
    "SlotCandidate",
    "active_selection_interval",
    "active_short_data_limit",
    "active_system_status",
    "blocks_new_admission",
    "enumerate_function_slots",
    "incremented_counter",
    "lease_seconds",
    "official_default_interval_nanoseconds",
    "select_random_access_slot",
    "short_data_budget_available",
    "short_data_counter_key",
    "tbb_slot_map",
)
