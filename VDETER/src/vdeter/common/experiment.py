"""Opt-in, bounded metadata buffer for the hardware security experiment.

No payload, key, nonce, AAD, or security header is retained. File I/O is done
by the launcher thread, outside encode/decode and packet building.
"""

from collections import deque
from threading import Lock


class ExperimentBuffer:
    def __init__(self, limit=20000):
        self._rows = deque()
        self._lock = Lock()
        self.limit = limit
        self.dropped = 0

    def record(self, **row):
        with self._lock:
            if len(self._rows) >= self.limit:
                self.dropped += 1
            else:
                self._rows.append(row)

    def drain(self):
        with self._lock:
            rows = list(self._rows)
            self._rows.clear()
            return rows
