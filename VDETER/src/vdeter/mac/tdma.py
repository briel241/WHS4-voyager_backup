"""Stage 4 TDMA 정수 좌표와 slot/window 계획 (`DR-STAGE4-MAC-003`, `012`).

M.2092-2 Annex 4 §A4-4.1의 구조를 정수 연산으로만 적용한다. 부동소수 반올림이나 고정
slot 길이 누적을 사용하지 않으며 모든 경계는 반개구간 `[start, next_start)`이다.

- UTC 매 분 시작에서 `global_slot=0`
- 1분 = 물리 slot 2,250개 = hexslot 375개 = TDMA frame 25개
- hexslot 160 ms, TDMA frame 15 hexslot(2.4초), TDMA channel 6개
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.exceptions import InvalidValueError
from ..common.time import ClockDomain, TimePoint

NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND
HEXSLOT_NANOSECONDS = 160_000_000
HEXSLOTS_PER_MINUTE = 375
HEXSLOTS_PER_FRAME = 15
FRAMES_PER_MINUTE = 25
TDMA_CHANNEL_COUNT = 6
PHYSICAL_SLOTS_PER_MINUTE = 2_250
FRAME_NANOSECONDS = HEXSLOTS_PER_FRAME * HEXSLOT_NANOSECONDS  # 2.4초

#: frame ordinal의 절대 기준. 2000-01-01 00:00:00 UTC (`DR-STAGE4-MAC-010`).
EPOCH_2000_UTC_SECONDS = 946_684_800
EPOCH_2000_UTC_NANOSECONDS = EPOCH_2000_UTC_SECONDS * NANOSECONDS_PER_SECOND


def round_half_up(numerator: int, denominator: int) -> int:
    """양의 분모에 대한 정수 round-half-up. 부동소수를 사용하지 않는다."""

    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise InvalidValueError("round_half_up: 분모는 양의 정수여야 합니다")
    if isinstance(numerator, bool) or not isinstance(numerator, int):
        raise InvalidValueError("round_half_up: 분자는 정수여야 합니다")
    if numerator >= 0:
        return (2 * numerator + denominator) // (2 * denominator)
    return -((2 * (-numerator) + denominator) // (2 * denominator))


def channel_offset_nanoseconds(index: int) -> int:
    """`offset(k)=round_half_up(k*160_000_000/6)` ns, `k=0..6`."""

    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= TDMA_CHANNEL_COUNT:
        raise InvalidValueError("channel_offset_nanoseconds: k는 0..6이어야 합니다")
    return round_half_up(index * HEXSLOT_NANOSECONDS, TDMA_CHANNEL_COUNT)


#: `(0, 26_666_667, 53_333_333, 80_000_000, 106_666_667, 133_333_333, 160_000_000)`
CHANNEL_OFFSETS_NANOSECONDS = tuple(
    channel_offset_nanoseconds(index) for index in range(TDMA_CHANNEL_COUNT + 1)
)


def channel_slot_duration_nanoseconds(tdma_channel: int) -> int:
    """인접 offset 차이. 고정 `26_666_667 ns`를 누적하지 않는다."""

    if (
        isinstance(tdma_channel, bool)
        or not isinstance(tdma_channel, int)
        or not 0 <= tdma_channel < TDMA_CHANNEL_COUNT
    ):
        raise InvalidValueError("channel_slot_duration_nanoseconds: channel은 0..5여야 합니다")
    return (
        CHANNEL_OFFSETS_NANOSECONDS[tdma_channel + 1]
        - CHANNEL_OFFSETS_NANOSECONDS[tdma_channel]
    )


def _require_utc(point: object, name: str) -> TimePoint:
    if not isinstance(point, TimePoint):
        raise InvalidValueError(f"{name}: TimePoint여야 합니다")
    if point.clock_domain is not ClockDomain.UTC:
        raise InvalidValueError(f"{name}: UTC domain이어야 합니다")
    return point


@dataclass(frozen=True, slots=True)
class TdmaPosition:
    """한 UTC 시각의 TDMA 좌표. 모든 값은 정수 경계식으로 계산한다."""

    minute_start_nanoseconds: int
    elapsed_nanoseconds: int
    hexslot_index: int
    in_hexslot_nanoseconds: int
    tdma_channel: int
    tdma_frame_index: int
    slot_in_frame: int
    global_slot: int

    def __post_init__(self) -> None:
        for name in (
            "minute_start_nanoseconds",
            "elapsed_nanoseconds",
            "hexslot_index",
            "in_hexslot_nanoseconds",
            "tdma_channel",
            "tdma_frame_index",
            "slot_in_frame",
            "global_slot",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidValueError(f"TdmaPosition.{name}: 0 이상의 정수여야 합니다")
        if not 0 <= self.hexslot_index < HEXSLOTS_PER_MINUTE:
            raise InvalidValueError("TdmaPosition.hexslot_index: 0..374여야 합니다")
        if not 0 <= self.tdma_channel < TDMA_CHANNEL_COUNT:
            raise InvalidValueError("TdmaPosition.tdma_channel: 0..5여야 합니다")
        if not 0 <= self.tdma_frame_index < FRAMES_PER_MINUTE:
            raise InvalidValueError("TdmaPosition.tdma_frame_index: 0..24여야 합니다")
        if not 0 <= self.slot_in_frame < HEXSLOTS_PER_FRAME:
            raise InvalidValueError("TdmaPosition.slot_in_frame: 0..14여야 합니다")
        if not 0 <= self.global_slot < PHYSICAL_SLOTS_PER_MINUTE:
            raise InvalidValueError("TdmaPosition.global_slot: 0..2249여야 합니다")

    def slot_start_nanoseconds(self) -> int:
        return (
            self.minute_start_nanoseconds
            + self.hexslot_index * HEXSLOT_NANOSECONDS
            + CHANNEL_OFFSETS_NANOSECONDS[self.tdma_channel]
        )

    def slot_end_nanoseconds(self) -> int:
        return (
            self.minute_start_nanoseconds
            + self.hexslot_index * HEXSLOT_NANOSECONDS
            + CHANNEL_OFFSETS_NANOSECONDS[self.tdma_channel + 1]
        )

    def frame_start_nanoseconds(self) -> int:
        return self.minute_start_nanoseconds + self.tdma_frame_index * FRAME_NANOSECONDS

    @property
    def physical_slot_index(self) -> int:
        """분 안의 물리 slot 색인. `global_slot`과 같은 값이며 정렬 key로 사용한다."""

        return self.global_slot


def minute_start_nanoseconds(utc_nanoseconds: int) -> int:
    """UTC 나노초가 속한 분의 시작 나노초."""

    if isinstance(utc_nanoseconds, bool) or not isinstance(utc_nanoseconds, int):
        raise InvalidValueError("minute_start_nanoseconds: 정수여야 합니다")
    return (utc_nanoseconds // NANOSECONDS_PER_MINUTE) * NANOSECONDS_PER_MINUTE


def position_at_nanoseconds(utc_nanoseconds: int) -> TdmaPosition:
    """UTC 나노초의 TDMA 좌표를 정수 반개구간 경계식으로 계산한다."""

    if isinstance(utc_nanoseconds, bool) or not isinstance(utc_nanoseconds, int):
        raise InvalidValueError("position_at_nanoseconds: 정수여야 합니다")
    if utc_nanoseconds < 0:
        raise InvalidValueError("position_at_nanoseconds: 음수 UTC는 사용할 수 없습니다")
    minute_start = minute_start_nanoseconds(utc_nanoseconds)
    elapsed = utc_nanoseconds - minute_start
    hexslot_index = elapsed // HEXSLOT_NANOSECONDS
    in_hexslot = elapsed % HEXSLOT_NANOSECONDS
    tdma_channel = 0
    for index in range(TDMA_CHANNEL_COUNT):
        if CHANNEL_OFFSETS_NANOSECONDS[index] <= in_hexslot:
            tdma_channel = index
        else:
            break
    return TdmaPosition(
        minute_start_nanoseconds=minute_start,
        elapsed_nanoseconds=elapsed,
        hexslot_index=hexslot_index,
        in_hexslot_nanoseconds=in_hexslot,
        tdma_channel=tdma_channel,
        tdma_frame_index=hexslot_index // HEXSLOTS_PER_FRAME,
        slot_in_frame=hexslot_index % HEXSLOTS_PER_FRAME,
        global_slot=TDMA_CHANNEL_COUNT * hexslot_index + tdma_channel,
    )


def position_at(utc: TimePoint) -> TdmaPosition:
    return position_at_nanoseconds(_require_utc(utc, "position_at").total_nanoseconds())


def slot_start_nanoseconds(
    minute_start: int, hexslot_index: int, tdma_channel: int
) -> int:
    """분 시작·hexslot·channel의 slot 시작 나노초."""

    if not 0 <= hexslot_index < HEXSLOTS_PER_MINUTE:
        raise InvalidValueError("slot_start_nanoseconds: hexslot_index는 0..374여야 합니다")
    if not 0 <= tdma_channel < TDMA_CHANNEL_COUNT:
        raise InvalidValueError("slot_start_nanoseconds: tdma_channel은 0..5여야 합니다")
    return (
        minute_start
        + hexslot_index * HEXSLOT_NANOSECONDS
        + CHANNEL_OFFSETS_NANOSECONDS[tdma_channel]
    )


def slot_bounds_nanoseconds(
    minute_start: int, hexslot_index: int, tdma_channel: int
) -> tuple[int, int]:
    """반개구간 `[slot_start, next_slot_start)`."""

    start = slot_start_nanoseconds(minute_start, hexslot_index, tdma_channel)
    return start, start + channel_slot_duration_nanoseconds(tdma_channel)


def frame_start_nanoseconds(minute_start: int, tdma_frame_index: int) -> int:
    if not 0 <= tdma_frame_index < FRAMES_PER_MINUTE:
        raise InvalidValueError("frame_start_nanoseconds: tdma_frame_index는 0..24여야 합니다")
    return minute_start + tdma_frame_index * FRAME_NANOSECONDS


def frame_ordinal_at_nanoseconds(utc_nanoseconds: int) -> int:
    """2000-01-01 00:00:00 UTC부터의 `floor(elapsed_ns/2_400_000_000)`.

    음수 경과 시각은 `TIME_UNAVAILABLE`이므로 여기서는 `InvalidValueError`를 낸다.
    호출자가 구조화 오류로 변환한다.
    """

    if isinstance(utc_nanoseconds, bool) or not isinstance(utc_nanoseconds, int):
        raise InvalidValueError("frame_ordinal_at_nanoseconds: 정수여야 합니다")
    elapsed = utc_nanoseconds - EPOCH_2000_UTC_NANOSECONDS
    if elapsed < 0:
        raise InvalidValueError(
            "frame_ordinal_at_nanoseconds: 2000-01-01 이전 시각은 TIME_UNAVAILABLE입니다"
        )
    return elapsed // FRAME_NANOSECONDS


def frame_ordinal_at(utc: TimePoint) -> int:
    return frame_ordinal_at_nanoseconds(
        _require_utc(utc, "frame_ordinal_at").total_nanoseconds()
    )


def frame_ordinal_start_nanoseconds(frame_ordinal: int) -> int:
    if isinstance(frame_ordinal, bool) or not isinstance(frame_ordinal, int) or frame_ordinal < 0:
        raise InvalidValueError("frame_ordinal_start_nanoseconds: 0 이상의 정수여야 합니다")
    return EPOCH_2000_UTC_NANOSECONDS + frame_ordinal * FRAME_NANOSECONDS


def next_frame_start_nanoseconds(utc_nanoseconds: int) -> int:
    """`utc_nanoseconds`가 속한 TDMA frame의 다음 frame 시작 (`DR-STAGE4-MAC-004`)."""

    return frame_ordinal_start_nanoseconds(frame_ordinal_at_nanoseconds(utc_nanoseconds) + 1)


def utc_point(nanoseconds: int, reference: TimePoint) -> TimePoint:
    """같은 UTC clock 식별자를 유지한 채 나노초를 `TimePoint`로 만든다."""

    _require_utc(reference, "utc_point reference")
    return TimePoint.from_nanoseconds(nanoseconds, ClockDomain.UTC, reference.clock_id)


def slot_start_after(
    utc_nanoseconds: int, hexslot_index_in_frame: int, tdma_channel: int, frame_ordinal: int
) -> int:
    """지정 frame ordinal 안의 `(slot_in_frame, tdma_channel)` slot 시작 나노초."""

    if not 0 <= hexslot_index_in_frame < HEXSLOTS_PER_FRAME:
        raise InvalidValueError("slot_start_after: slot_in_frame은 0..14여야 합니다")
    if not 0 <= tdma_channel < TDMA_CHANNEL_COUNT:
        raise InvalidValueError("slot_start_after: tdma_channel은 0..5여야 합니다")
    return (
        frame_ordinal_start_nanoseconds(frame_ordinal)
        + hexslot_index_in_frame * HEXSLOT_NANOSECONDS
        + CHANNEL_OFFSETS_NANOSECONDS[tdma_channel]
    )


@dataclass(frozen=True, slots=True)
class RxWindowRequest:
    """한 수신 slot의 반개구간 window 계획 (`DR-STAGE4-MAC-012`).

    인접 slot은 merge하거나 겹치지 않는 별도 window이며 MAC이 별도 pre/post guard를 더하지
    않는다.
    """

    channel_name: str
    tdma_channel: int
    slot_in_frame: int
    frame_ordinal: int
    start_nanoseconds: int
    duration_nanoseconds: int
    #: 계획 근원 slot의 function (window intent). key에는 포함하지 않는다.
    slot_function: object = None

    @property
    def key(self) -> tuple:
        """`planned_rx_windows`의 canonical key `(channel, slot_start_utc.nanoseconds)`."""

        return (self.frame_ordinal, self.channel_name, self.start_nanoseconds)

    @property
    def end_nanoseconds(self) -> int:
        return self.start_nanoseconds + self.duration_nanoseconds


def rx_windows_for_frame(slots, frame_ordinal: int) -> tuple[RxWindowRequest, ...]:
    """slot 기술자에서 한 TDMA frame의 비중첩 RX window를 만든다.

    `slots`의 각 항목은 `physical_channel.name`, `tdma_channel`, `slot_in_frame`을 갖는
    duck-typed 기술자다. 계층 import 순환을 피하기 위해 구체 type을 요구하지 않는다.
    """

    frame_start = frame_ordinal_start_nanoseconds(frame_ordinal)
    windows = []
    for slot in slots:
        start = (
            frame_start
            + slot.slot_in_frame * HEXSLOT_NANOSECONDS
            + CHANNEL_OFFSETS_NANOSECONDS[slot.tdma_channel]
        )
        windows.append(
            RxWindowRequest(
                channel_name=slot.physical_channel.name,
                tdma_channel=slot.tdma_channel,
                slot_in_frame=slot.slot_in_frame,
                frame_ordinal=frame_ordinal,
                start_nanoseconds=start,
                duration_nanoseconds=channel_slot_duration_nanoseconds(slot.tdma_channel),
                slot_function=getattr(slot, "function", None),
            )
        )
    windows.sort(key=lambda item: (item.start_nanoseconds, item.channel_name))
    return tuple(windows)


def samples_for_duration(duration_nanoseconds: int, sample_rate_hz: int) -> int:
    """Stage 2/3의 승인된 positive round-half-up sample 변환."""

    if (
        isinstance(duration_nanoseconds, bool)
        or not isinstance(duration_nanoseconds, int)
        or duration_nanoseconds <= 0
    ):
        raise InvalidValueError("samples_for_duration: duration은 양의 정수여야 합니다")
    if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
        raise InvalidValueError("samples_for_duration: sample_rate_hz는 양의 정수여야 합니다")
    return round_half_up(duration_nanoseconds * sample_rate_hz, NANOSECONDS_PER_SECOND)


__all__ = (
    "CHANNEL_OFFSETS_NANOSECONDS",
    "EPOCH_2000_UTC_NANOSECONDS",
    "EPOCH_2000_UTC_SECONDS",
    "FRAMES_PER_MINUTE",
    "FRAME_NANOSECONDS",
    "HEXSLOTS_PER_FRAME",
    "HEXSLOTS_PER_MINUTE",
    "HEXSLOT_NANOSECONDS",
    "NANOSECONDS_PER_MINUTE",
    "NANOSECONDS_PER_SECOND",
    "PHYSICAL_SLOTS_PER_MINUTE",
    "RxWindowRequest",
    "TDMA_CHANNEL_COUNT",
    "TdmaPosition",
    "rx_windows_for_frame",
    "channel_offset_nanoseconds",
    "channel_slot_duration_nanoseconds",
    "frame_ordinal_at",
    "frame_ordinal_at_nanoseconds",
    "frame_ordinal_start_nanoseconds",
    "frame_start_nanoseconds",
    "minute_start_nanoseconds",
    "next_frame_start_nanoseconds",
    "position_at",
    "position_at_nanoseconds",
    "round_half_up",
    "samples_for_duration",
    "slot_bounds_nanoseconds",
    "slot_start_after",
    "slot_start_nanoseconds",
    "utc_point",
)
