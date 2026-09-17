"""monotonic clock 구현. deadline 측정용(§11, §18).

시험은 fake clock을 우선 사용하므로(명세 §18), 이 module은 production real clock만
담는다. fake clock은 public `vdeter.poc.testing`에 있다.
"""

from __future__ import annotations

import time


class RealMonotonicClock:
    """`time.monotonic()` 기반. `MonotonicClock` Protocol을 만족한다."""

    def monotonic(self) -> float:
        return time.monotonic()


__all__ = ("RealMonotonicClock",)
