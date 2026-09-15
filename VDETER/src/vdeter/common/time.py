"""시간 모델과 TimeSource (`stage0_baseline.md` §6.2).

- floating-point timestamp는 공개 계약에 사용하지 않는다.
- 같은 `clock_domain`과 `clock_id`의 `TimePoint`만 직접 비교·차감할 수 있다.
- MAC frame/slot/TDMA 계획은 `UTC`, timeout과 경과시간은 `MONOTONIC`을 사용한다.
- `RADIO_HARDWARE` 변환은 Radio가 제공한 명시적 `ClockMapping`으로만 수행한다.

실제 UTC direct/indirect 획득 방식과 동기 오차는 단계 3·4 기준선에서 확정한다.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .errors import Correlation, Details, EMPTY_CORRELATION, ErrorCategory, ErrorCode, Layer
from .exceptions import ClockMismatchError, InvalidValueError
from .results import InternalResult, Ok, internal_error

NANOSECONDS_PER_SECOND = 1_000_000_000


class ClockDomain(str, Enum):
    UTC = "UTC"
    MONOTONIC = "MONOTONIC"
    RADIO_HARDWARE = "RADIO_HARDWARE"


class SyncStatus(str, Enum):
    """시간원의 동기 상태. 구체적인 획득 절차와 허용 오차는 단계 3·4에서 확정한다."""

    SYNCHRONIZED = "SYNCHRONIZED"
    HOLDOVER = "HOLDOVER"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TimePoint:
    """integer 초/나노초와 clock 식별정보를 함께 보존하는 시각."""

    seconds: int
    nanoseconds: int
    clock_domain: ClockDomain
    clock_id: str

    def __post_init__(self) -> None:
        for name in ("seconds", "nanoseconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidValueError(f"TimePoint.{name}: 정수여야 합니다 ({value!r})")
        if not 0 <= self.nanoseconds < NANOSECONDS_PER_SECOND:
            raise InvalidValueError(
                f"TimePoint.nanoseconds: 0..999999999여야 합니다 ({self.nanoseconds})"
            )
        if not isinstance(self.clock_domain, ClockDomain):
            raise InvalidValueError("TimePoint.clock_domain: ClockDomain이 아닙니다")
        if not isinstance(self.clock_id, str) or not self.clock_id:
            raise InvalidValueError("TimePoint.clock_id: 비어 있지 않은 문자열이어야 합니다")
        if not self.clock_id.isascii() or not self.clock_id.isprintable():
            raise InvalidValueError("TimePoint.clock_id: 출력 가능한 ASCII여야 합니다")

    @classmethod
    def from_nanoseconds(
        cls, total_nanoseconds: int, clock_domain: ClockDomain, clock_id: str
    ) -> "TimePoint":
        if isinstance(total_nanoseconds, bool) or not isinstance(total_nanoseconds, int):
            raise InvalidValueError("TimePoint total_nanoseconds: 정수여야 합니다")
        seconds, nanoseconds = divmod(total_nanoseconds, NANOSECONDS_PER_SECOND)
        return cls(seconds, nanoseconds, clock_domain, clock_id)

    def total_nanoseconds(self) -> int:
        return self.seconds * NANOSECONDS_PER_SECOND + self.nanoseconds

    def same_clock(self, other: "TimePoint") -> bool:
        return self.clock_domain is other.clock_domain and self.clock_id == other.clock_id

    def _require_same_clock(self, other: "TimePoint") -> None:
        if not isinstance(other, TimePoint):
            raise InvalidValueError("TimePoint 비교 대상이 TimePoint가 아닙니다")
        if not self.same_clock(other):
            raise ClockMismatchError(
                "서로 다른 clock의 TimePoint는 직접 비교하거나 차감할 수 없습니다 "
                f"({self.clock_domain.value}:{self.clock_id} vs "
                f"{other.clock_domain.value}:{other.clock_id})"
            )

    def difference_nanoseconds(self, other: "TimePoint") -> int:
        """같은 clock의 두 시각 차이를 나노초로 반환한다. 다르면 예외를 낸다."""

        self._require_same_clock(other)
        return self.total_nanoseconds() - other.total_nanoseconds()

    def shifted(self, nanoseconds: int) -> "TimePoint":
        if isinstance(nanoseconds, bool) or not isinstance(nanoseconds, int):
            raise InvalidValueError("TimePoint.shifted nanoseconds: 정수여야 합니다")
        return TimePoint.from_nanoseconds(
            self.total_nanoseconds() + nanoseconds, self.clock_domain, self.clock_id
        )

    def __lt__(self, other: "TimePoint") -> bool:
        self._require_same_clock(other)
        return self.total_nanoseconds() < other.total_nanoseconds()

    def __le__(self, other: "TimePoint") -> bool:
        self._require_same_clock(other)
        return self.total_nanoseconds() <= other.total_nanoseconds()

    def __gt__(self, other: "TimePoint") -> bool:
        self._require_same_clock(other)
        return self.total_nanoseconds() > other.total_nanoseconds()

    def __ge__(self, other: "TimePoint") -> bool:
        self._require_same_clock(other)
        return self.total_nanoseconds() >= other.total_nanoseconds()


def compare_time_points(
    left: TimePoint,
    right: TimePoint,
    *,
    layer: Layer = Layer.COMMON,
    correlation: Correlation = EMPTY_CORRELATION,
) -> InternalResult:
    """clock이 같으면 -1/0/1을, 다르면 구조화된 오류를 반환한다."""

    if not left.same_clock(right):
        return internal_error(
            layer,
            ErrorCode.CLOCK_DOMAIN_MISMATCH,
            ErrorCategory.VALIDATION,
            "서로 다른 clock domain 또는 clock id의 시각은 직접 비교할 수 없습니다",
            correlation=correlation,
            details=Details.of(
                left_clock_domain=left.clock_domain.value,
                left_clock_id=left.clock_id,
                right_clock_domain=right.clock_domain.value,
                right_clock_id=right.clock_id,
            ),
        )
    difference = left.total_nanoseconds() - right.total_nanoseconds()
    return Ok(0 if difference == 0 else (1 if difference > 0 else -1))


def elapsed_nanoseconds(
    later: TimePoint,
    earlier: TimePoint,
    *,
    layer: Layer = Layer.COMMON,
    correlation: Correlation = EMPTY_CORRELATION,
) -> InternalResult:
    """같은 clock의 경과시간을 구조화된 결과로 반환한다."""

    if not later.same_clock(earlier):
        return internal_error(
            layer,
            ErrorCode.CLOCK_DOMAIN_MISMATCH,
            ErrorCategory.VALIDATION,
            "서로 다른 clock domain 또는 clock id의 시각은 차감할 수 없습니다",
            correlation=correlation,
            details=Details.of(
                later_clock_domain=later.clock_domain.value,
                later_clock_id=later.clock_id,
                earlier_clock_domain=earlier.clock_domain.value,
                earlier_clock_id=earlier.clock_id,
            ),
        )
    return Ok(later.total_nanoseconds() - earlier.total_nanoseconds())


@dataclass(frozen=True, slots=True)
class ClockMapping:
    """두 clock 사이의 명시적 변환 기준. 기준 시각과 uncertainty를 보존한다."""

    source_domain: ClockDomain
    source_clock_id: str
    target_domain: ClockDomain
    target_clock_id: str
    source_reference: TimePoint
    target_reference: TimePoint
    uncertainty_nanoseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_reference, TimePoint) or not isinstance(
            self.target_reference, TimePoint
        ):
            raise InvalidValueError("ClockMapping: 기준 시각은 TimePoint여야 합니다")
        if (
            self.source_reference.clock_domain is not self.source_domain
            or self.source_reference.clock_id != self.source_clock_id
        ):
            raise InvalidValueError("ClockMapping: source 기준 시각의 clock이 일치하지 않습니다")
        if (
            self.target_reference.clock_domain is not self.target_domain
            or self.target_reference.clock_id != self.target_clock_id
        ):
            raise InvalidValueError("ClockMapping: target 기준 시각의 clock이 일치하지 않습니다")
        if isinstance(self.uncertainty_nanoseconds, bool) or not isinstance(
            self.uncertainty_nanoseconds, int
        ):
            raise InvalidValueError("ClockMapping.uncertainty_nanoseconds: 정수여야 합니다")
        if self.uncertainty_nanoseconds < 0:
            raise InvalidValueError("ClockMapping.uncertainty_nanoseconds: 0 이상이어야 합니다")

    def convert(self, point: TimePoint) -> InternalResult:
        """source clock 시각을 target clock 시각으로 변환한다."""

        if (
            point.clock_domain is not self.source_domain
            or point.clock_id != self.source_clock_id
        ):
            return internal_error(
                Layer.COMMON,
                ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
                ErrorCategory.VALIDATION,
                "이 mapping이 다루지 않는 clock의 시각입니다",
                details=Details.of(
                    point_clock_domain=point.clock_domain.value,
                    point_clock_id=point.clock_id,
                    mapping_source_domain=self.source_domain.value,
                    mapping_source_clock_id=self.source_clock_id,
                ),
            )
        offset = point.total_nanoseconds() - self.source_reference.total_nanoseconds()
        converted = TimePoint.from_nanoseconds(
            self.target_reference.total_nanoseconds() + offset,
            self.target_domain,
            self.target_clock_id,
        )
        return Ok(converted)


@runtime_checkable
class TimeSource(Protocol):
    """MAC 시간원. endpoint 시작 시 운영·시험 구현을 선택한다."""

    def now_utc(self) -> TimePoint: ...

    def now_monotonic(self) -> TimePoint: ...

    def sync_status(self) -> SyncStatus: ...

    def mapping(self, source: ClockDomain, target: ClockDomain) -> InternalResult: ...

    def description(self) -> str: ...


class SystemTimeSource:
    """운영 구현. system UTC clock과 monotonic clock을 사용한다.

    실제 UTC direct/indirect 동기 획득은 단계 3·4에서 구현하므로 단계 0의 동기 상태는
    `UNKNOWN`으로 보고한다.
    """

    __slots__ = ("_utc_clock_id", "_monotonic_clock_id")

    def __init__(
        self, utc_clock_id: str = "system-utc", monotonic_clock_id: str = "system-monotonic"
    ) -> None:
        self._utc_clock_id = utc_clock_id
        self._monotonic_clock_id = monotonic_clock_id

    def now_utc(self) -> TimePoint:
        return TimePoint.from_nanoseconds(_time.time_ns(), ClockDomain.UTC, self._utc_clock_id)

    def now_monotonic(self) -> TimePoint:
        return TimePoint.from_nanoseconds(
            _time.monotonic_ns(), ClockDomain.MONOTONIC, self._monotonic_clock_id
        )

    def sync_status(self) -> SyncStatus:
        return SyncStatus.UNKNOWN

    def mapping(self, source: ClockDomain, target: ClockDomain) -> InternalResult:
        return internal_error(
            Layer.COMMON,
            ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
            ErrorCategory.UNSUPPORTED,
            "단계 0의 SystemTimeSource는 clock mapping을 제공하지 않습니다",
            details=Details.of(source_domain=source.value, target_domain=target.value),
        )

    def description(self) -> str:
        return f"system:{self._utc_clock_id}/{self._monotonic_clock_id}"


class TestTimeSource:
    """제어 가능한 시험 구현. slot, timeout, 동기 상태 시험에만 사용한다."""

    __slots__ = ("_utc_ns", "_monotonic_ns", "_sync_status", "_utc_clock_id", "_monotonic_clock_id")

    def __init__(
        self,
        utc_nanoseconds: int = 0,
        monotonic_nanoseconds: int = 0,
        sync_status: SyncStatus = SyncStatus.SYNCHRONIZED,
        utc_clock_id: str = "test-utc",
        monotonic_clock_id: str = "test-monotonic",
    ) -> None:
        for name, value in (
            ("utc_nanoseconds", utc_nanoseconds),
            ("monotonic_nanoseconds", monotonic_nanoseconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidValueError(f"TestTimeSource.{name}: 정수여야 합니다")
        if utc_nanoseconds < 0 or monotonic_nanoseconds < 0:
            raise InvalidValueError("TestTimeSource: 시각은 0 이상이어야 합니다")
        self._utc_ns = utc_nanoseconds
        self._monotonic_ns = monotonic_nanoseconds
        self._sync_status = sync_status
        self._utc_clock_id = utc_clock_id
        self._monotonic_clock_id = monotonic_clock_id

    def now_utc(self) -> TimePoint:
        return TimePoint.from_nanoseconds(self._utc_ns, ClockDomain.UTC, self._utc_clock_id)

    def now_monotonic(self) -> TimePoint:
        return TimePoint.from_nanoseconds(
            self._monotonic_ns, ClockDomain.MONOTONIC, self._monotonic_clock_id
        )

    def advance(self, nanoseconds: int) -> None:
        """두 domain의 시각을 같은 양만큼 진행시킨다."""

        if isinstance(nanoseconds, bool) or not isinstance(nanoseconds, int) or nanoseconds < 0:
            raise InvalidValueError("TestTimeSource.advance: 0 이상의 정수여야 합니다")
        self._utc_ns += nanoseconds
        self._monotonic_ns += nanoseconds

    def advance_utc(self, nanoseconds: int) -> None:
        """UTC만 진행해 clock-jump/mapping invalidation을 결정적으로 시험한다."""

        if isinstance(nanoseconds, bool) or not isinstance(nanoseconds, int):
            raise InvalidValueError("TestTimeSource.advance_utc: 정수여야 합니다")
        if self._utc_ns + nanoseconds < 0:
            raise InvalidValueError("TestTimeSource.advance_utc: 결과 시각은 0 이상이어야 합니다")
        self._utc_ns += nanoseconds

    def advance_monotonic(self, nanoseconds: int) -> None:
        """MONOTONIC만 진행해 timeout/expiry 경계를 시험한다."""

        if isinstance(nanoseconds, bool) or not isinstance(nanoseconds, int) or nanoseconds < 0:
            raise InvalidValueError("TestTimeSource.advance_monotonic: 0 이상의 정수여야 합니다")
        self._monotonic_ns += nanoseconds

    def set_sync_status(self, status: SyncStatus) -> None:
        if not isinstance(status, SyncStatus):
            raise InvalidValueError("TestTimeSource.set_sync_status: SyncStatus가 아닙니다")
        self._sync_status = status

    def sync_status(self) -> SyncStatus:
        return self._sync_status

    def mapping(self, source: ClockDomain, target: ClockDomain) -> InternalResult:
        if source is target:
            return internal_error(
                Layer.COMMON,
                ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
                ErrorCategory.VALIDATION,
                "같은 domain 사이의 mapping은 필요하지 않습니다",
                details=Details.of(source_domain=source.value, target_domain=target.value),
            )
        if {source, target} != {ClockDomain.UTC, ClockDomain.MONOTONIC}:
            return internal_error(
                Layer.COMMON,
                ErrorCode.CLOCK_MAPPING_UNAVAILABLE,
                ErrorCategory.UNSUPPORTED,
                "TestTimeSource는 UTC와 MONOTONIC 사이의 mapping만 제공합니다",
                details=Details.of(source_domain=source.value, target_domain=target.value),
            )
        utc_reference = self.now_utc()
        monotonic_reference = self.now_monotonic()
        if source is ClockDomain.UTC:
            source_reference, target_reference = utc_reference, monotonic_reference
        else:
            source_reference, target_reference = monotonic_reference, utc_reference
        return Ok(
            ClockMapping(
                source_domain=source_reference.clock_domain,
                source_clock_id=source_reference.clock_id,
                target_domain=target_reference.clock_domain,
                target_clock_id=target_reference.clock_id,
                source_reference=source_reference,
                target_reference=target_reference,
                uncertainty_nanoseconds=0,
            )
        )

    def description(self) -> str:
        return f"test:{self._utc_clock_id}/{self._monotonic_clock_id}"
