"""난수원 (`stage0_baseline.md` §6.2).

`RandomSource`는 MAC의 선택 알고리즘이 사용할 범위 제한 정수를 제공한다. 운용 구현은
system entropy를 사용하고 시험 구현은 기록된 seed로 재현 가능해야 한다. algorithm별 난수
사용 규칙은 해당 MAC 기준선이 소유하며 단계 0에서는 선택과 재현성만 다룬다.
"""

from __future__ import annotations

import random
import secrets
from typing import Protocol, runtime_checkable

from .exceptions import InvalidValueError


def _validate_bounds(low: int, high: int) -> None:
    for name, value in (("low", low), ("high", high)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidValueError(f"RandomSource.{name}: 정수여야 합니다 ({value!r})")
    if low > high:
        raise InvalidValueError(f"RandomSource: low({low})가 high({high})보다 큽니다")


@runtime_checkable
class RandomSource(Protocol):
    """범위 제한 정수 난수원. endpoint 시작 시 운영·시험 구현을 선택한다."""

    def next_int(self, low: int, high: int) -> int:
        """`low`와 `high`를 포함하는 범위의 정수를 반환한다."""

    def description(self) -> str: ...


class SystemRandomSource:
    """운용 구현. system entropy를 사용한다."""

    __slots__ = ()

    def next_int(self, low: int, high: int) -> int:
        _validate_bounds(low, high)
        return low + secrets.randbelow(high - low + 1)

    def description(self) -> str:
        return "system-entropy"


class SeededRandomSource:
    """시험 구현. 기록된 seed로 같은 입력에 같은 결과를 재현한다."""

    __slots__ = ("_seed", "_random")

    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise InvalidValueError(f"SeededRandomSource.seed: 정수여야 합니다 ({seed!r})")
        self._seed = seed
        self._random = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def next_int(self, low: int, high: int) -> int:
        _validate_bounds(low, high)
        return self._random.randint(low, high)

    def reset(self) -> None:
        """기록된 seed로 되돌려 같은 순서를 다시 재현한다."""

        self._random = random.Random(self._seed)

    def description(self) -> str:
        return f"seeded:{self._seed}"
