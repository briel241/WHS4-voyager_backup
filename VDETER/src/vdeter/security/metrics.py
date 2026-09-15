"""Lock-safe counters with fixed labels only."""

from collections import Counter
from threading import Lock


class SecurityMetrics:
    def __init__(self):
        self._lock = Lock()
        self._counts = Counter()

    def increment(self, name: str) -> None:
        if not name.startswith("security_") or not name.endswith("_total"):
            raise ValueError("Metric name is not a security counter")
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
