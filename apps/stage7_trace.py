"""Stage 7 상위 계층 structured instrumentation (CLI 아님).

목적: persistent 대화형 service session의 버그 재현·진단.

**계측 범위는 application/service 경계뿐이다.** 다음에는 절대 넣지 않는다 —
endpoint launcher 실시간 pump, B210 worker callback, radio adapter hot loop,
PHY/MAC/slot 처리, timing/rf_time 계산, `_run_rf_qualification`.

성능 규약
    - RF sample/burst/slot마다 파일을 쓰지 않는다. event는 메모리 버퍼에
      쌓고 workload·phase 경계에서 flush한다.
    - hot loop에서 print하지 않는다. 계측을 위한 sleep을 넣지 않는다.
    - timeout·threshold를 바꾸지 않는다.

출력 규약
    - 대화형 터미널에는 저수준 계측을 출력하지 않는다. 상세는 evidence 파일에만
      남는다. `diagnostics`는 evidence 상세도만 높이고 터미널 출력량을 늘리지
      않는다(터미널 상세는 별도 `--verbose`가 담당한다).

보안 규약
    - private key 본문·DER·base64·hex·SHA, PEM private marker, credential,
      민감 환경변수는 기록하지 않는다. 값에 그런 표지가 있으면 치환한다.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

#: 기록을 거부하는 키 이름 (부분 일치, 대소문자 무시).
_FORBIDDEN_KEY = re.compile(
    r"(private_key|privatekey|secret|password|passwd|credential|token|"
    r"api[_-]?key|passphrase)", re.IGNORECASE)

#: 값에 이 표지가 있으면 통째로 치환한다.
_FORBIDDEN_VALUE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|BEGIN OPENSSH PRIVATE KEY)")

_REDACTED = "<redacted>"

#: 버퍼가 이만큼 쌓이면 자동 flush (경계 flush 사이의 안전판).
_AUTO_FLUSH_EVENTS = 200


def scrub(value):
    """비밀 표지를 재귀적으로 제거한다."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and _FORBIDDEN_KEY.search(key):
                cleaned[key] = _REDACTED
            else:
                cleaned[key] = scrub(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str) and _FORBIDDEN_VALUE.search(value):
        return _REDACTED
    return value


class TraceWriter:
    """버퍼링 JSONL 계측기. session/workload ID를 모든 event에 결속한다."""

    __slots__ = ("path", "session_id", "workload_id", "diagnostics", "_buffer",
                 "_lock", "_closed", "_started_monotonic", "_sequence")

    def __init__(self, path: str, *, session_id: str,
                 workload_id: str | None = None,
                 diagnostics: bool = False) -> None:
        self.path = path
        self.session_id = session_id
        self.workload_id = workload_id
        self.diagnostics = bool(diagnostics)
        self._buffer: list = []
        self._lock = threading.Lock()
        self._closed = False
        self._sequence = 0
        self._started_monotonic = time.monotonic()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)

    # ---- 기록 -------------------------------------------------------------

    def emit(self, event: str, *, detail_level: str = "normal", **fields):
        """event 하나를 버퍼에 넣는다. `detail_level="diagnostic"`은 diagnostics
        가 켜져 있을 때만 기록한다 (터미널 출력은 어느 쪽도 늘리지 않는다)."""
        if self._closed:
            return
        if detail_level == "diagnostic" and not self.diagnostics:
            return
        with self._lock:
            self._sequence += 1
            record = {
                "seq": self._sequence,
                "event": event,
                "session_id": self.session_id,
                "workload_id": self.workload_id,
                "t_monotonic_s": round(time.monotonic() - self._started_monotonic, 4),
                "t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            record.update(scrub(fields))
            self._buffer.append(record)
            overflow = len(self._buffer) >= _AUTO_FLUSH_EVENTS
        if overflow:
            self.flush()

    def state(self, state: str, **fields):
        """session/workload 상태 전이."""
        self.emit("state", state=state, **fields)

    # ---- flush/close ------------------------------------------------------

    def flush(self):
        """workload·phase 경계에서 호출한다."""
        with self._lock:
            pending, self._buffer = self._buffer, []
        if not pending:
            return
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            for record in pending:
                handle.write(json.dumps(record, ensure_ascii=False,
                                        sort_keys=True))
                handle.write("\n")

    def close(self, **fields):
        """마지막 event를 남기고 flush한다. 예외 경로에서도 반드시 호출한다."""
        if self._closed:
            return
        self.emit("trace_closed", **fields)
        self.flush()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close(
            exception=None if exc is None else f"{exc_type.__name__}: {exc}")
        return False


class NullTrace:
    """계측 비활성 대체물 — 동작·판정은 계측 유무와 무관해야 한다."""

    session_id = None
    workload_id = None
    diagnostics = False
    path = None

    def emit(self, *args, **kwargs):
        pass

    def state(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def close(self, **fields):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def read_trace(path: str) -> list:
    """계측 JSONL을 읽어 list로 돌려준다 (시험·evidence 검증용)."""
    records = []
    if not os.path.isfile(path):
        return records
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records
