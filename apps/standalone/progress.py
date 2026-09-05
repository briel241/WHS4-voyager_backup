"""터미널 진행 표시·시나리오 실시간 추적 — presentation/observability 계층.

표준 라이브러리만 쓴다. 판정·threshold·deadline·retry·시나리오 의미론에는
관여하지 않으며, **기존 로그/trace/evidence를 읽어** 표시만 한다(RF/PHY/MAC
hot loop에 계측·파일 I/O를 추가하지 않는다).

두 가지 출력 형태
    - 진행바(:class:`ProgressReporter`): setup·qualify·run의 bring-up.
      TTY에서는 같은 줄을 덮어쓰는 20칸 바, non-TTY/nohup/redirect에서는
      carriage return·ANSI 없이 UTC 시각이 붙은 줄 단위 로그.
      세 의미: exact(실제 분모 있음, `estimated` 없음) / estimated(가중치
      추정, 퍼센트 바로 뒤 `estimated`) / waiting(완료 시점 불명 — 퍼센트를
      늘리지 않고 관측값·경과시간만).
    - 시나리오 추적(:class:`ScenarioTracer`): bring-up READY 뒤에는 진행바
      없이 실제 송수신·판정 데이터를 줄 단위로 상세 출력한다.

같은 정보를 evidence 로그(`presentation/progress.log` 등)에도 보존한다.
개인키·비밀 표지는 :func:`scrub_text`로 걸러 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

BAR_WIDTH = 20
HEARTBEAT_S = 30.0
#: non-TTY에서 같은 단계의 상태 변화 줄 사이 최소 간격(초) — 반복 출력 억제.
NONTTY_MIN_INTERVAL_S = 5.0

MODE_EXACT = "exact"
MODE_ESTIMATED = "estimated"
MODE_WAITING = "waiting"

STATUS_COLUMN_WIDTH = 14  # " 48% estimated" / "waiting       " / "FAILED        "

_PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|BEGIN OPENSSH PRIVATE KEY", re.S)
_SECRET_ASSIGN_PATTERN = re.compile(
    r"(private_key(?:_hex|_base64|_sha256|_pem)?|passphrase|password|"
    r"secret_key|api_key|access_token|auth_token)\s*[=:]\s*\S+", re.I)
_REDACTED = "<redacted>"


def scrub_text(text: str) -> str:
    """개인키·비밀 표지를 제거한다 (표시 계층 최후 방어선)."""
    if not text:
        return text
    text = _PEM_PATTERN.sub(_REDACTED, text)
    return _SECRET_ASSIGN_PATTERN.sub(lambda m: m.group(1) + "=" + _REDACTED,
                                      text)


def format_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def utc_now(wallclock=time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wallclock()))


def detect_tty(stream) -> bool:
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if os.environ.get("VDETER_NO_TTY"):
        return False
    return True


def detect_unicode(stream) -> bool:
    """유니코드 블록 문자를 이 stream/locale이 표시할 수 있는가."""
    if os.environ.get("VDETER_ASCII_PROGRESS"):
        return False
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "█░".encode(encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    lang = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
            or os.environ.get("LANG") or "")
    if lang and lang.upper() in ("C", "POSIX"):
        return False
    return True


def render_bar(fraction, *, unicode: bool = True) -> str:
    """20칸 진행바. fraction None → 0."""
    fraction = 0.0 if fraction is None else max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * BAR_WIDTH))
    filled = max(0, min(BAR_WIDTH, filled))
    full, empty = ("█", "░") if unicode else ("#", "-")
    return "[" + full * filled + empty * (BAR_WIDTH - filled) + "]"


def status_column(mode: str, percent, status=None) -> str:
    """퍼센트/의미 열 — 폭 고정(정렬)."""
    if status == "FAILED":
        text = "FAILED"
    elif status == "INTERRUPTED":
        text = "INTERRUPTED"
    elif mode == MODE_WAITING:
        text = "waiting"
    elif percent is None:
        text = "    "
    elif mode == MODE_ESTIMATED and percent < 100:
        text = "%3d%% estimated" % percent
    else:
        text = "%3d%%" % percent
    return text.ljust(STATUS_COLUMN_WIDTH)


def _fields_text(fields: dict, *, sep: str) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append("%s=%s" % (key, value))
    return sep.join(parts)


class ProgressReporter:
    """단일 진행줄 렌더러 (TTY 갱신 / non-TTY 줄 로그) + heartbeat + evidence 로그.

    호출 규약: :meth:`phase`로 단계를 시작하고 :meth:`update`로 값·필드를
    갱신, :meth:`finish`로 확정한다. 다른 출력은 반드시 :meth:`log`를 거쳐야
    진행바와 섞이지 않는다. 한 줄(단계)의 퍼센트는 **그 단계 자체의 진행률**
    (0→100%)이고 같은 단계 안에서 뒤로 가지 않는다; 새 단계는 0부터 다시
    센다. 여러 단계를 아우르는 전체 진행률은 호출자가 `overall=NN%` 필드로
    싣는다(BringupTracker·QualifyProgress).
    """

    def __init__(self, sink=None, *, stream=None, enabled: bool = True,
                 tty: bool | None = None, unicode: bool | None = None,
                 scope: str = "PROGRESS", evidence_log: Path | str | None = None,
                 clock=time.monotonic, wallclock=time.time,
                 heartbeat_s: float = HEARTBEAT_S, ticker: bool = True,
                 min_interval_s: float = NONTTY_MIN_INTERVAL_S) -> None:
        self.stream = stream or sys.stdout
        self._sink = sink
        self.enabled = bool(enabled)
        self.tty = detect_tty(self.stream) if tty is None else bool(tty)
        self.unicode = (detect_unicode(self.stream) if unicode is None
                        else bool(unicode))
        self.scope = scope
        self.clock = clock
        self.wallclock = wallclock
        self.heartbeat_s = heartbeat_s
        self.min_interval_s = min_interval_s
        self._lock = threading.RLock()
        self._line = None          # 현재 단계 상태
        self._bar_visible = False
        self._max_percent = -1
        self._last_emit_at = None
        self._last_emit_key = None
        self._pending_plain = False
        self._evidence_log = Path(evidence_log) if evidence_log else None
        self._closed = False
        self._ticker = None
        #: ticker 스레드가 tick() 대신 호출하는 훅(예: BringupTracker.poll).
        self.tick_hook = None
        if self.enabled and ticker:
            self._ticker = threading.Thread(target=self._tick_loop,
                                            name="vdeter-progress",
                                            daemon=True)
            self._ticker.start()

    # ---- 출력 원시 ------------------------------------------------------

    def _write_sink(self, text: str) -> None:
        text = scrub_text(text)
        if self._sink is not None:
            self._sink(text)
        else:
            self.stream.write(text + "\n")
            self.stream.flush()

    def _write_evidence(self, text: str) -> None:
        if self._evidence_log is None:
            return
        try:
            self._evidence_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self._evidence_log, "a", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(scrub_text(text) + "\n")
        except OSError:
            pass  # evidence 로그 실패가 실행을 막지 않는다

    def set_evidence_log(self, path: Path | str | None) -> None:
        with self._lock:
            self._evidence_log = Path(path) if path else None

    # ---- 렌더링 ---------------------------------------------------------

    def _percent(self, line: dict):
        if line["fraction"] is None:
            return None
        percent = int(line["fraction"] * 100 + 1e-9)
        return max(0, min(100, percent))

    def _monotonic_percent(self, percent):
        """같은 단계 안에서 퍼센트가 뒤로 가지 않게 한다 (표시 전용).
        :meth:`phase`가 새 단계마다 초기화한다 — 앞 단계의 100%가 다음 단계
        줄에 눌어붙지 않는다."""
        if percent is None:
            return None
        if percent < self._max_percent:
            return self._max_percent
        self._max_percent = percent
        return percent

    def render_tty(self, line: dict) -> str:
        percent = self._monotonic_percent(self._percent(line))
        fraction = (percent / 100.0) if percent is not None else 0.0
        if line["mode"] == MODE_WAITING and percent is None:
            fraction = line.get("hold_fraction") or 0.0
        bar = render_bar(fraction, unicode=self.unicode)
        col = status_column(line["mode"], percent, line["status"]
                            if line["status"] in ("FAILED", "INTERRUPTED")
                            else None)
        fields = dict(line["fields"])
        fields["elapsed"] = format_hms(self.clock() - line["started"])
        text = "%s %s %s" % (bar, col, line["label"])
        detail = _fields_text(fields, sep=" · ")
        if detail:
            text += " · " + detail
        return scrub_text(text)

    def render_plain(self, line: dict, *, heartbeat: bool = False) -> str:
        percent = self._monotonic_percent(self._percent(line))
        if line["status"] in ("FAILED", "INTERRUPTED"):
            word = line["status"]
        elif line["status"] == "done":
            word = "done" if percent is None else "%d%%" % percent
        elif line["mode"] == MODE_WAITING:
            word = "waiting"
        elif percent is None:
            word = "running"
        elif line["mode"] == MODE_ESTIMATED and percent < 100:
            word = "%d%% estimated" % percent
        else:
            word = "%d%%" % percent
        fields = dict(line["fields"])
        fields["elapsed"] = format_hms(self.clock() - line["started"])
        text = "%s [%s] %s %s" % (utc_now(self.wallclock), self.scope, word,
                                  line["label"])
        detail = _fields_text(fields, sep=" ")
        if detail:
            text += " " + detail
        if heartbeat:
            text += " heartbeat=1"
        return scrub_text(text)

    def _draw_bar(self) -> None:
        if not (self.enabled and self.tty and self._line
                and self._line["status"] is None):
            return
        text = self.render_tty(self._line)
        try:
            width = os.get_terminal_size(self.stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            width = 120
        if len(text) > width - 1:
            text = text[: max(0, width - 2)] + "…" if self.unicode else \
                text[: max(0, width - 1)]
        self.stream.write("\r" + text + " " * max(0, self._last_width - len(text)))
        self.stream.write("\r" + text)
        self.stream.flush()
        self._last_width = len(text)
        self._bar_visible = True

    _last_width = 0

    def _clear_bar(self) -> None:
        if self._bar_visible:
            self.stream.write("\r" + " " * self._last_width + "\r")
            self.stream.flush()
            self._bar_visible = False

    def _emit_plain(self, *, heartbeat: bool = False, force: bool = False) -> None:
        """non-TTY 줄 로그 (+ evidence). 같은 내용의 반복 출력은 억제."""
        line = self._line
        if line is None:
            return
        text = self.render_plain(line, heartbeat=heartbeat)
        # 상태(status)는 키에 넣지 않는다 — 단계 완료(done)가 값 변화 없이
        # 같은 퍼센트·필드를 반복 출력하지 않게 (FAILED/INTERRUPTED는 force).
        key = (line["label"], line["mode"], self._percent(line),
               tuple(sorted((k, str(v)) for k, v in line["fields"].items())))
        now = self.clock()
        if not force and not heartbeat:
            if key == self._last_emit_key:
                return
            if (self._last_emit_at is not None
                    and now - self._last_emit_at < self.min_interval_s
                    and line["status"] is None):
                self._pending_plain = True   # 간격 뒤 tick()이 최신 상태를 낸다
                return
        self._pending_plain = False
        self._last_emit_key = key
        self._last_emit_at = now
        if self.enabled and not self.tty:
            self._write_sink(text)
        self._write_evidence(text)

    def _write_evidence_if_changed(self, *, force: bool = False) -> None:
        """TTY 확정 줄의 evidence 기록 — 마지막 기록과 같은 내용이면 생략."""
        line = self._line
        key = (line["label"], line["mode"], self._percent(line),
               tuple(sorted((k, str(v)) for k, v in line["fields"].items())))
        if not force and key == self._last_emit_key:
            return
        self._last_emit_key = key
        self._write_evidence(self.render_plain(line))

    # ---- 공개 API -------------------------------------------------------

    def phase(self, label: str, *, mode: str = MODE_ESTIMATED, fraction=None,
              hold_fraction=None, scope: str | None = None, **fields) -> None:
        """새 단계 시작. 진행 중인 단계가 있으면 done으로 확정한다."""
        with self._lock:
            if self._line is not None and self._line["status"] is None:
                self.finish("done")
            if scope is not None:
                self.scope = scope
            self._line = {"label": label, "mode": mode,
                          "fraction": fraction, "hold_fraction": hold_fraction,
                          "fields": dict(fields), "started": self.clock(),
                          "status": None}
            self._max_percent = -1      # 단계별 단조 — 새 단계는 0부터
            self._last_emit_key = None
            self._emit_plain(force=True)
            self._draw_bar()

    def update(self, *, mode: str | None = None, fraction=None,
               label: str | None = None, **fields) -> None:
        with self._lock:
            if self._line is None or self._line["status"] is not None:
                return
            if mode is not None:
                self._line["mode"] = mode
            if fraction is not None:
                self._line["fraction"] = fraction
            if label is not None:
                self._line["label"] = label
            self._line["fields"].update(fields)
            self._emit_plain()
            self._draw_bar()

    def finish(self, status: str = "done", **fields) -> None:
        """현재 단계 확정: done/FAILED/INTERRUPTED. TTY는 줄을 확정하고 다음
        줄로 내려간다. 퍼센트는 호출자가 준 값 그대로다(전체 진행률을 뜻하는
        줄에서 단계 완료가 100%로 튀지 않게) — 완료 100%는 호출자가 명시한다."""
        with self._lock:
            if self._line is None or self._line["status"] is not None:
                return
            self._line["fields"].update(fields)
            self._line["status"] = status
            if self.enabled and self.tty:
                self._clear_bar()
                text = self.render_tty(self._line)
                if status == "done" and self._line["mode"] == MODE_WAITING:
                    text = text.replace(status_column(MODE_WAITING, None),
                                        "done".ljust(STATUS_COLUMN_WIDTH), 1)
                self.stream.write(text + "\n")
                self.stream.flush()
                self._write_evidence_if_changed(force=(status != "done"))
            else:
                # done은 값이 바뀌었을 때만(중복 억제), 실패·중단은 항상 기록.
                self._emit_plain(force=(status != "done"))

    def log(self, text: str) -> None:
        """진행바와 섞이지 않는 일반 출력 (Ctrl+C·cleanup 메시지 포함)."""
        with self._lock:
            if self.enabled and self.tty:
                self._clear_bar()
                self._write_sink(text)
                self._draw_bar()
            else:
                self._write_sink(text)

    def tick(self) -> None:
        """경과시간 갱신 + heartbeat (TTY: 매 호출 재그리기, non-TTY: 30초)."""
        with self._lock:
            if self._closed or self._line is None or self._line["status"]:
                return
            if self.enabled and self.tty:
                self._draw_bar()
            now = self.clock()
            if (self._pending_plain and self._last_emit_at is not None
                    and now - self._last_emit_at >= self.min_interval_s):
                self._emit_plain()   # 억제됐던 최신 상태 변화
            elif (self._last_emit_at is None
                    or now - self._last_emit_at >= self.heartbeat_s):
                self._emit_plain(heartbeat=True)

    def _tick_loop(self) -> None:
        while not self._closed:
            time.sleep(1.0)
            try:
                hook = self.tick_hook
                if hook is not None:
                    hook()
                else:
                    self.tick()
            except Exception:  # noqa: BLE001 — 표시 스레드는 실행을 막지 않는다
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._clear_bar()

    @property
    def active_label(self):
        return self._line["label"] if self._line else None


# ------------------------------------------------------------ 로그 tail

class FileTail:
    """append-only 로그의 새 줄만 돌려준다 (읽기 전용, 부재 허용)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._offset = 0
        self._partial = b""

    def seek_end(self) -> None:
        try:
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0

    def new_lines(self) -> list:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []
        if not data:
            return []
        data = self._partial + data
        parts = data.split(b"\n")
        self._partial = parts.pop()
        return [p.decode("utf-8", "replace").rstrip("\r") for p in parts if p]


# ------------------------------------------------------------ bring-up

#: bring-up 단계 (표시 전용 가중치 — run-20260904-184019 실측 phase_elapsed_s).
BRINGUP_PHASES = (
    # key, label, typical_s, mode
    ("startup", "Endpoint startup", 15.0, MODE_EXACT),
    ("host_time", "Host-time acquisition", 448.0, MODE_WAITING),
    ("rf_time_pre", "RF-time PRE validation", 457.0, MODE_ESTIMATED),
    ("active_bridge", "Active bridge commit", 5.0, MODE_ESTIMATED),
    ("rf_time_post", "RF-time POST validation", 457.0, MODE_ESTIMATED),
    ("service", "Service startup", 10.0, MODE_ESTIMATED),
)
_BRINGUP_TOTAL_S = sum(p[2] for p in BRINGUP_PHASES)

_GUARD_RE = re.compile(r"^\[guard:[^\]]+\] progress #(\d+) (terminal:)?(\S+)")
_CONNECTED_RE = re.compile(r"^\[coordinator\] (MASS-A|MASS-B|ROC) 연결됨")
_RF_PATH_RE = re.compile(r"(C[1-6])")


class BringupTracker:
    """coordinator 콘솔·trace를 읽어 bring-up 진행바를 구동한다 (표시 전용).

    host-time은 완료 시점을 예측할 수 없으므로 `waiting`(퍼센트 정지, 이벤트
    수·경과시간 표시). RF-time PRE/POST는 실측 소요가 안정적이라 경과시간
    기반 `estimated`. endpoint startup은 연결 endpoint n/3 exact."""

    def __init__(self, reporter: ProgressReporter, *, console_log: Path | str,
                 trace_log: Path | str | None = None, endpoint_total: int = 3,
                 clock=time.monotonic) -> None:
        self.reporter = reporter
        self.clock = clock
        self._console = FileTail(console_log)
        self._trace = FileTail(trace_log) if trace_log else None
        self.endpoint_total = endpoint_total
        self.ready_endpoints = 0
        self.phase_index = -1
        self.phase_started = None
        self.events_in_phase = 0
        self.completed_weight = 0.0
        self.notes = {}
        self.failed = False
        self.session_started = clock()
        self._lock = threading.RLock()
        self._start_phase(0)

    # ---- 단계 전이 ------------------------------------------------------

    def _phase(self):
        return BRINGUP_PHASES[self.phase_index]

    def _overall_fraction(self, phase_fraction) -> float:
        """bring-up 전체 진행률(가중치) — `overall=` 필드용, 바의 값이 아니다."""
        key, _label, typical, _mode = self._phase()
        inner = 0.0 if phase_fraction is None else phase_fraction
        return min(0.99, (self.completed_weight + inner * typical) / _BRINGUP_TOTAL_S)

    def _overall_field(self, phase_fraction) -> str:
        percent = int(self._overall_fraction(phase_fraction) * 100 + 1e-9)
        self._overall_max = max(getattr(self, "_overall_max", 0), percent)
        return "%d%%" % self._overall_max          # 전체 진행률도 후퇴 없음

    def _start_phase(self, index: int) -> None:
        if index >= len(BRINGUP_PHASES):
            return
        if self.phase_index >= 0:
            _k, _l, _t, prev_mode = self._phase()
            self.completed_weight += self._phase()[2]
            # 끝난 단계는 자기 100%로 확정한다 (waiting은 퍼센트 없이 done).
            if prev_mode == MODE_WAITING:
                self.reporter.update(overall=self._overall_field(0.0))
            else:
                self.reporter.update(fraction=1.0,
                                     overall=self._overall_field(0.0))
            self.reporter.finish("done")
        self.phase_index = index
        self.phase_started = self.clock()
        self.events_in_phase = 0
        key, label, _typical, mode = self._phase()
        fields = {}
        if key == "startup":
            fields["ready"] = "%d/%d" % (self.ready_endpoints, self.endpoint_total)
            fraction = self.ready_endpoints / self.endpoint_total
        elif mode == MODE_WAITING:
            fraction = None
        else:
            fraction = 0.0
        fields["overall"] = self._overall_field(fraction or 0.0)
        self.reporter.phase(label, mode=mode, fraction=fraction,
                            hold_fraction=0.0, scope="BRINGUP", **fields)

    def _advance_to(self, key: str) -> None:
        for index, phase in enumerate(BRINGUP_PHASES):
            if phase[0] == key:
                if index > self.phase_index:
                    # 건너뛴 단계는 완료로 확정한다.
                    while self.phase_index < index:
                        self._start_phase(self.phase_index + 1)
                return

    # ---- 입력 -------------------------------------------------------------

    def on_console_line(self, line: str) -> None:
        with self._lock:
            m = _CONNECTED_RE.match(line)
            if m:
                self.ready_endpoints = min(self.endpoint_total,
                                           self.ready_endpoints + 1)
                if self._phase()[0] == "startup":
                    inner = self.ready_endpoints / self.endpoint_total
                    self.reporter.update(
                        fraction=inner,
                        ready="%d/%d" % (self.ready_endpoints,
                                         self.endpoint_total),
                        overall=self._overall_field(inner))
                return
            if line.startswith("[coordinator] prepare/configure/arm_rx COMPLETED"):
                self._advance_to("host_time")
                return
            if line.startswith("[coordinator] acquire_host_time COMPLETED"):
                self._advance_to("rf_time_pre")
                return
            if line.startswith("[coordinator] validate_rf_time PRE COMPLETED"):
                self._advance_to("active_bridge")
                return
            if line.startswith("[coordinator] active bridge COMMITTED"):
                # bridge 확정 → RF-time POST 검증 시작 (실측 ~7.6분; 없으면
                # POST 구간 전체가 'Active bridge commit'으로 표시됐다).
                self._advance_to("rf_time_post")
                return
            if line.startswith("[coordinator] validate_rf_time POST COMPLETED"):
                self._advance_to("service")
                return
            if "host affine fit FAIL" in line:
                self.notes["reason"] = "HOST_AFFINE_FIT_FAILED"
                self.reporter.update(last="host affine fit FAIL")
                return
            g = _GUARD_RE.match(line)
            if g:
                stage_name = g.group(3)
                if stage_name in ("acquire_host_time",) and g.group(2) is None:
                    return
                self.events_in_phase += 1
                key = self._phase()[0]
                if key == "host_time":
                    self.reporter.update(events=self.events_in_phase)
                elif key == "active_bridge" and "active_bridge" in stage_name:
                    self.reporter.update(step=stage_name)
                elif key == "service":
                    self.reporter.update(step=stage_name)
                return

    def on_trace_row(self, row: dict) -> None:
        with self._lock:
            event = row.get("event")
            if event == "rf_time_pre_summary":
                paths = row.get("observed_paths") or []
                self.reporter.update(paths="%d/6" % len(paths))
            elif event == "rf_time_post_summary":
                paths = row.get("observed_paths") or []
                self.reporter.update(paths="%d/6" % len(paths),
                                     state=row.get("state"))

    def poll(self) -> None:
        """콘솔·trace 새 줄 소화 + estimated 단계의 경과 기반 갱신."""
        with self._lock:
            for line in self._console.new_lines():
                self.on_console_line(line)
            if self._trace is not None:
                for line in self._trace.new_lines():
                    try:
                        self.on_trace_row(json.loads(line))
                    except ValueError:
                        continue
            key, _label, typical, mode = self._phase()
            if mode == MODE_ESTIMATED and self.phase_started is not None:
                inner = min(0.95, (self.clock() - self.phase_started) / typical)
                self.reporter.update(fraction=inner,
                                     overall=self._overall_field(inner))
            self.reporter.tick()

    def ready(self) -> None:
        with self._lock:
            self._advance_to("service")
            total = format_hms(self.clock() - self.session_started)
            self.reporter.update(fraction=1.0, mode=MODE_EXACT,
                                 label="Service session READY", total=total,
                                 overall="100%")
            self.reporter.finish("done")

    def failed_at(self, status: str = "FAILED") -> str:
        with self._lock:
            self.failed = True
            label = self._phase()[1]
            self.reporter.finish(status)
            return label


def bringup_failure_lines(*, phase_label: str, finalize_summary: dict,
                          classification: dict, console_text: str,
                          run_root, attempt: int, max_attempts: int,
                          declared_serials, usb_observed: dict,
                          host_time_detail: dict | None = None) -> list:
    """bring-up 실패 상세 블록 (실제 값만; 없는 값은 생략)."""
    closed = (finalize_summary or {}).get("closed") or {}
    cleanup = closed.get("cleanup") or {}
    residual = (finalize_summary or {}).get("residual_processes")
    reasons = (classification or {}).get("reasons") or []
    lines = ["", "  layer=ORCHESTRATION", "  stage=%s" % phase_label]
    detail = host_time_detail or {}
    if detail.get("endpoint"):
        lines.append("  endpoint=%s" % detail["endpoint"])
    if "host affine fit FAIL" in (console_text or ""):
        lines.append("  reason=HOST_AFFINE_FIT_FAILED")
    elif detail.get("reason"):
        lines.append("  reason=%s" % detail["reason"])
    if detail.get("observations_discarded") is not None:
        lines.append("  observations_discarded=%s"
                     % detail["observations_discarded"])
    if detail.get("restart_count") is not None:
        lines.append("  restart_count=%s" % detail["restart_count"])
    if closed.get("duration_s") is not None:
        lines.append("  elapsed=%s" % format_hms(closed["duration_s"]))
    lines.append("  cleanup=%s" % ("PASS" if cleanup.get("pass") else "FAIL"))
    if residual is not None:
        lines.append("  residual_processes=%d" % len(residual))
    if declared_serials:
        seen = sum(1 for s in declared_serials if s in (usb_observed or {}))
        lines.append("  b210_rediscovered=%d/%d" % (seen, len(declared_serials)))
    lines.append("  retry_eligible=%s"
                 % ("true" if (classification or {}).get("transient_host_time")
                    else "false"))
    if reasons:
        lines.append("  no_retry_reasons=%s" % "; ".join(reasons))
    lines.append("  attempt=%d/%d" % (attempt, max_attempts))
    lines.append("  evidence=%s" % run_root)
    return lines


def host_time_detail_from_evidence(run_root) -> dict:
    """endpoint runtime evidence(`evidence/<EP>-runtime.json`)에서 host-time
    재획득 기록을 읽는다 (실패 후 존재할 때만; 없으면 빈 dict)."""
    out = {}
    evidence = Path(run_root) / "evidence"
    if not evidence.is_dir():
        return out
    for path in sorted(evidence.glob("*-runtime.json")):
        try:
            payload = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            continue
        for op in payload.get("operations_evidence") or []:
            if isinstance(op, dict) and op.get("kind") == "host_time_restarts":
                restarts = op.get("restarts") or []
                out = {"endpoint": payload.get("endpoint_id"),
                       "restart_count": len(restarts),
                       "observations_discarded": sum(
                           int(r.get("discarded") or 0) for r in restarts
                           if isinstance(r, dict)),
                       "reason": (restarts[-1].get("reason") if restarts
                                  and isinstance(restarts[-1], dict) else None)}
    return out


# ------------------------------------------------------------ 시나리오 추적

#: station 번호 ↔ endpoint (apps.stage7_mass_roc_controller.STATION_BY_ENDPOINT).
STATION_BY_ENDPOINT = {"MASS-A": 111111111, "MASS-B": 333333333,
                       "ROC": 222222222}
ENDPOINT_BY_STATION = {v: k for k, v in STATION_BY_ENDPOINT.items()}


def _short(value, n: int = 8) -> str:
    return (str(value)[:n] + "…") if value else "-"


def _station_label(station) -> str:
    name = ENDPOINT_BY_STATION.get(station)
    return "%s(%s)" % (name, station) if name else str(station)


_REASON_RE = re.compile(r"'reason',\s*'([^']+)'")


def _error_reason(event: dict):
    """service ERROR 이벤트의 error_details(`Details(entries=(('reason', 'X'),))`
    문자열 또는 dict)에서 reason만 뽑는다. 없으면 None."""
    details = event.get("error_details")
    if isinstance(details, dict):
        value = details.get("reason")
        return str(value) if value else None
    if isinstance(details, (list, tuple)):
        for item in details:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == "reason":
                return str(item[1])
    m = _REASON_RE.search(str(details or ""))
    return m.group(1) if m else None


def _plain(value) -> str:
    """필드 값 표기: bool은 소문자(true/false), None은 '-'."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "-"
    return str(value)


#: 세로 블록 형식 — 헤더 한 줄 + 들여쓴 `key=value` 한 줄씩 (가로로 긴 한 줄 대신).
BLOCK_INDENT = "  "


def _emit_block(out, header: str, fields, *, gap: bool = True) -> None:
    """`header` 다음에 (key, value) 쌍을 한 줄씩 들여써 출력한다. value가
    None인 항목은 생략; 이미 'key=value ...' 문자열이면 그대로 한 줄.
    블록 사이는 빈 줄 하나로 띄운다(`gap`; 2026-09-05 사용자 요구)."""
    if gap:
        out("")
    out(header)
    for item in fields:
        if isinstance(item, str):
            out(BLOCK_INDENT + item)
            continue
        key, value = item
        if value is None:
            continue
        out(BLOCK_INDENT + "%s=%s" % (key, _plain(value)))


class ScenarioTracer:
    """bring-up 이후 시나리오 실시간 추적 — 퍼센트 진행바 없음.

    실시간 원천: `evidence/service_events/<EP>.jsonl`(launcher가 0.5 s마다
    append) — TRANSFER_STATE_CHANGED·DATA_RECEIVED·ERROR. 종료 시 원천:
    workload trace(submit/transfer_terminal/data_received/scenario_result)와
    session result(quiescence·counters). 파생값은 `derived=true`로 표시한다.

    출력은 **세로 블록**이다: `[TX] STATE` / `[RX] DATA_RECEIVED` /
    `[STEP] name  submit` 같은 헤더 한 줄 뒤에 `key=value`를 한 줄씩 들여쓴다
    (가로로 긴 한 줄은 터미널에서 읽기 어렵다 — 2026-09-05 사용자 요구).
    """

    def __init__(self, out, *, run_root: Path | str,
                 endpoints=("MASS-A", "MASS-B", "ROC"), clock=time.monotonic,
                 wallclock=time.time) -> None:
        self.out = out
        self.run_root = Path(run_root)
        self.endpoints = tuple(endpoints)
        self.clock = clock
        self.wallclock = wallclock
        self._tails = {ep: FileTail(self.run_root / "evidence"
                                    / "service_events" / f"{ep}.jsonl")
                       for ep in self.endpoints}
        for tail in self._tails.values():
            tail.seek_end()
        self._transfers = {}      # transfer_id → {endpoint, states:[(state, t_mono)]}
        self._pending_ack = []    # (sender, transfer_id, t_mono WAITING_ACK)
        self._errors = []         # (endpoint, error_code, reason) — service ERROR 관측
        self.workload = None
        self.started = None
        self.tx_seen = 0
        self.rx_seen = 0

    # ---- workload 경계 -----------------------------------------------------

    def start_workload(self, *, index: int, total: int, scenario_id: str,
                       certificate_case: str, workload_id: str | None,
                       run_id: str) -> None:
        self.workload = {"index": index, "total": total,
                         "scenario_id": scenario_id,
                         "certificate_case": certificate_case,
                         "workload_id": workload_id, "run_id": run_id}
        self.started = self.clock()
        self._transfers = {}
        self._pending_ack = []
        self._errors = []
        self.tx_seen = self.rx_seen = 0
        for tail in self._tails.values():
            tail.seek_end()
        case = ("" if scenario_id != "SC-VDE-001"
                else " " + (certificate_case or "normal"))
        prefix = "[%d/%d] " % (index, total) if total > 1 else ""
        self._steps_printed = False
        self._scenario_result = None
        _emit_block(self.out, "%s%s%s" % (prefix, scenario_id, case),
                    [("time", utc_now(self.wallclock)), ("run_id", run_id),
                     ("workload_id", workload_id or "-")])

    def _elapsed(self) -> str:
        return format_hms(self.clock() - (self.started or self.clock()))

    # ---- 실시간 service_events ----------------------------------------------

    def poll(self) -> None:
        # 한 poll에 여러 endpoint의 새 이벤트가 함께 오면 t_mono 순으로
        # 출력한다 (endpoint 파일 순서가 아니라 실제 발생 순서).
        batch = []
        for endpoint, tail in self._tails.items():
            for line in tail.new_lines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                t_mono = event.get("t_mono")
                key = float(t_mono) if isinstance(t_mono, (int, float)) else float("inf")
                batch.append((key, len(batch), endpoint, event))
        for _key, _seq, endpoint, event in sorted(batch, key=lambda b: (b[0], b[1])):
            self._on_service_event(endpoint, event)

    def _on_service_event(self, endpoint: str, event: dict) -> None:
        kind = event.get("event_type")
        t_mono = event.get("t_mono")
        if kind == "TRANSFER_STATE_CHANGED":
            transfer_id = event.get("transfer_id")
            state = event.get("transfer_state")
            record = self._transfers.setdefault(
                transfer_id, {"endpoint": endpoint, "states": []})
            record["states"].append((state, t_mono))
            fields = [("endpoint", endpoint),
                      ("transfer_id", _short(transfer_id))]
            first = record["states"][0][1]
            if (t_mono is not None and first is not None
                    and state != record["states"][0][0]):
                fields.append("since_queued=%.3fs derived=true"
                              % max(0.0, float(t_mono) - float(first)))
            if state == "WAITING_ACK":
                self._pending_ack.append((endpoint, transfer_id, t_mono))
            if state == "COMPLETED":
                fields.extend(self._derive_ack(record, t_mono))
            if event.get("error_code"):
                fields.append(("error_code", event["error_code"]))
            self.tx_seen += 1
            _emit_block(self.out, "[TX] %s" % state, fields)
        elif kind == "DATA_RECEIVED":
            self.rx_seen += 1
            src = event.get("original_source")
            dst = event.get("original_destination")
            _emit_block(self.out, "[RX] DATA_RECEIVED", [
                ("endpoint", endpoint),
                ("payload_format", event.get("payload_format")),
                ("application_bytes", event.get("payload_len")),
                ("payload_sha256", _short(event.get("payload_sha256"), 12)),
                ("src", _station_label(src)), ("dst", _station_label(dst))])
            if str(event.get("payload_format") or "").endswith(".ack"):
                # 파생 ACK 관측 — 명시 correlates_to는 event에 없다.
                self._pending_ack.append(("ack:" + endpoint, None, t_mono))
        elif kind == "ERROR":
            reason = _error_reason(event)
            self._errors.append((endpoint, event.get("error_code"), reason))
            _emit_block(self.out, "[ERR]", [
                ("endpoint", endpoint), ("error_code", event.get("error_code")),
                ("reason", reason),
                ("message", scrub_text(str(event.get("error_message") or "")))])
        elif kind in ("SERVICE_STARTED", "SERVICE_STOPPED",
                      "ENDPOINT_HEALTH_CHANGED"):
            _emit_block(self.out, "[EP] %s" % kind, [("endpoint", endpoint)])

    @staticmethod
    def _derive_ack(record: dict, t_completed):
        """WAITING_ACK 뒤의 COMPLETED는 MAC ACK 수신을 뜻한다(서비스 상태
        기계: WAITING_ACK→COMPLETED on ACK / →FAILED on timeout). ack_latency는
        WAITING_ACK→COMPLETED 전이 시간 — 파생값(derived=true). 응용 계층
        `.ack` payload는 별개의 후속 transfer라 [RX]로 따로 보인다."""
        waits = [t for st, t in record["states"] if st == "WAITING_ACK"]
        if not waits:
            return []
        t_wait = waits[-1]
        out = [("ack", "received"), ("ack_source", "transfer_state derived=true")]
        if t_wait is not None and t_completed is not None:
            out.append("ack_latency=%.3fs derived=true"
                       % max(0.0, float(t_completed) - float(t_wait)))
        return out

    # ---- 종료 시 workload trace·result ------------------------------------

    def on_trace_row(self, row: dict) -> None:
        event = row.get("event")
        if event == "submit":
            _emit_block(self.out, "[STEP] %s  submit" % row.get("step"), [
                ("time", row.get("t_utc")),
                ("transfer_id", _short(row.get("transfer_id"))),
                ("src", "%s(%s)" % (row.get("sender"), row.get("source_station"))),
                ("dst", "%s(%s)" % (row.get("receiver"),
                                    row.get("destination_station"))),
                ("payload_format", row.get("payload_format")),
                ("application_bytes", row.get("payload_len")),
                ("payload_sha256", _short(row.get("payload_sha256"), 12)),
                ("ack_required", row.get("ack_required"))])
        elif event == "transfer_terminal":
            _emit_block(self.out, "[STEP] %s  terminal_state=%s"
                        % (row.get("step"), row.get("terminal_state")),
                        [("transfer_id", _short(row.get("transfer_id")))])
        elif event == "data_received":
            _emit_block(self.out, "[STEP] %s  received=%s"
                        % (row.get("step"), _plain(row.get("present"))), [
                ("endpoint", row.get("receiver")),
                ("src", _station_label(row.get("original_source"))),
                ("dst", _station_label(row.get("original_destination")))])
        elif event == "scenario_result":
            # 판정 블록은 workload 종료 시 요약과 함께 낸다 (실시간 [TX]/[RX]
            # 흐름 중간에 끼어들지 않게) — 여기서는 보관만.
            self._scenario_result = row

    def _print_steps(self, steps) -> None:
        """`[STEP n/N] name  STATUS` — workload trace의 [name, status] 쌍과
        session result의 dict(전이 상태·수신 검사 포함) 모두 받는다."""
        steps = [st for st in (steps or []) if isinstance(st, (dict, list, tuple))]
        total = len(steps)
        if not total:
            return
        width = max(len(str(st.get("name") if isinstance(st, dict) else st[0]))
                    for st in steps)
        for index, step in enumerate(steps, 1):
            if isinstance(step, dict):
                name, status = step.get("name"), step.get("status")
            else:
                name = step[0] if len(step) > 0 else "?"
                status = step[1] if len(step) > 1 else "?"
            extra = []
            if isinstance(step, dict):
                if step.get("transfer_id"):
                    extra.append(("transfer_id", _short(step["transfer_id"])))
                if step.get("state"):
                    extra.append(("state", step["state"]))
                checks = step.get("received_checks") or {}
                for key in ("byte_exact", "original_source",
                            "original_destination", "payload_format",
                            "payload_len", "payload_sha256"):
                    if key in checks:
                        extra.append((key, checks[key]))
                for key in ("remaining_waypoints", "route_linkage_id", "sog"):
                    if key in step:
                        extra.append((key, step[key]))
            # 판정 줄 목록은 한 블록: 첫 줄 앞에만 빈 줄, 상세가 붙는 줄은 따로.
            _emit_block(self.out, "[STEP %d/%d] %-*s  %s"
                        % (index, total, width, name, status), extra,
                        gap=(index == 1 or bool(extra)))
        self._steps_printed = True

    def _print_scenario_result(self, result: dict) -> None:
        self._print_steps(result.get("steps"))
        counters = result.get("counters") or {}
        self.out("")                      # 요약 블록 시작
        self.out("  counters: transfers_completed=%s transfers_failed=%s"
                 % (counters.get("transfers_completed"),
                    counters.get("transfers_failed")))
        self.out("            wrong_destination=%s mismatched_ack=%s "
                 "duplicates=%s"
                 % (counters.get("wrong_destination"),
                    counters.get("mismatched_ack"),
                    counters.get("duplicates")))
        if result.get("reason"):
            self.out("  reason=%s" % scrub_text(str(result["reason"])))

    def finish_workload(self, result: dict, *, evidence_dir=None) -> None:
        """session result(`wait_result` 반환)로 종료 요약 + 실패 상세."""
        status = result.get("status")
        scenario = result.get("scenario") or {}
        quiescence = result.get("quiescence") or {}
        verdict = getattr(self, "_scenario_result", None)
        if verdict is None:
            # workload trace에 scenario_result가 없었으면 session result의
            # 상세 steps(전이 상태·수신 검사 포함)·counters로 구성한다.
            for child in (scenario.get("results") or {}).values():
                inner = (child or {}).get("result") or child or {}
                if inner.get("steps") or inner.get("counters"):
                    verdict = {"steps": inner.get("steps"),
                               "counters": inner.get("counters"),
                               "reason": inner.get("reason")}
                    break
        self._print_scenario_result(verdict or {})
        self.out("  result=%s  elapsed=%s  wall_clock_s=%s"
                 % (status, self._elapsed(), result.get("wall_clock_s", "?")))
        self.out("  quiescence=%s  non_terminal_transfers=%s  "
                 "transfers_observed=%s"
                 % ("PASS" if quiescence.get("pass") else "FAIL",
                    quiescence.get("non_terminal_transfers"),
                    quiescence.get("transfers_observed")))
        self.out("  workers_running=%s  launchers_alive=%s  faults=%s"
                 % (quiescence.get("workers_running"),
                    quiescence.get("launchers_alive"),
                    quiescence.get("faults")))
        if evidence_dir is not None:
            self.out("  evidence=%s" % evidence_dir)
        if status != "PASS" or not quiescence.get("pass"):
            self.out("  [FAIL] layer=%s" % self._failure_layer(result, self._errors))
            if self._errors:
                counts = {}
                for endpoint, code, reason in self._errors:
                    key = "%s:%s" % (endpoint, reason or code or "?")
                    counts[key] = counts.get(key, 0) + 1
                self.out("  observed_errors=%s" % ", ".join(
                    "%s×%d" % (k, n) for k, n in counts.items()))
            if self.workload:
                self.out("  scenario=%s workload_id=%s"
                         % (self.workload.get("scenario_id"),
                            self.workload.get("workload_id")))
            error = scenario.get("error") or result.get("error")
            if error:
                self.out("  error=%s" % scrub_text(str(error)))
            for name, child in (scenario.get("results") or {}).items():
                inner = (child or {}).get("result") or child or {}
                if inner.get("status") and inner.get("status") != "PASS":
                    self.out("  scenario_result=%s status=%s reason=%s"
                             % (name, inner.get("status"),
                                scrub_text(str(inner.get("reason")))))
                    for step in inner.get("steps") or []:
                        if isinstance(step, dict) and step.get("status") != "PASS":
                            self.out("  failed_step=%s state=%s transfer_id=%s"
                                     % (step.get("name"), step.get("state"),
                                        _short(step.get("transfer_id"))))
            open_transfers = [tid for tid, rec in self._transfers.items()
                              if rec["states"] and rec["states"][-1][0]
                              not in ("COMPLETED", "FAILED", "CANCELLED")]
            if open_transfers:
                self.out("  non_terminal_transfer_ids=%s"
                         % ", ".join(_short(t) for t in open_transfers))
            self.out("  elapsed_since_workload_start=%s" % self._elapsed())

    @staticmethod
    def _failure_layer(result: dict, errors=()) -> str:
        """실패 계층 추정(기존 오류 계약 문자열 기반 — 새 오류명 생성 없음).

        session result의 error 문자열에 더해, workload 중 관측한 service ERROR
        (endpoint, error_code, reason)를 함께 본다 — 예: 수신 endpoint의
        `LinkIdDetectionError`는 PHY, `DEVICE_ERROR`/uhd는 RADIO,
        ack/timeout은 MAC. 실측(run-20260905-044555): 결과 error가 비어 있어
        APPLICATION으로 표시됐지만 실제 원인은 MASS-A PHY 검출 실패였다."""
        quiescence = result.get("quiescence") or {}
        scenario = result.get("scenario") or {}
        error = str(scenario.get("error") or result.get("error") or "")
        for child in (scenario.get("results") or {}).values():
            inner = (child or {}).get("result") or child or {}
            if inner.get("reason"):
                error += " " + str(inner["reason"])
        observed = " ".join("%s %s" % (code or "", reason or "")
                            for _ep, code, reason in (errors or ()))
        lowered = (error + " " + observed).lower()
        if quiescence.get("pass") is False and not error.strip() and not observed.strip():
            return "ORCHESTRATION"
        if "device_error" in lowered or "uhd" in lowered:
            return "RADIO"
        if "linkid" in lowered or "decode" in lowered:
            return "PHY"
        if "ack" in lowered or "mac" in lowered or "timeout" in lowered:
            return "MAC"
        return "APPLICATION"


# ------------------------------------------------------------ qualify

#: 표시 전용 가중치 — 5차 ladder qual-20260904-134421 + r2 bundle 실측(초).
QUALIFY_WEIGHTS = {
    "rr-p0": 20.0, "rr-p1": 3395.0, "rr-p2-17": 2327.0, "rr-p3-fault": 111.0,
    "bundle:unit-contract.log": 545.0, "bundle:integration-stage7.log": 393.0,
    "builder": 5.0,
}
_QUALIFY_TOTAL_S = sum(QUALIFY_WEIGHTS.values())
_PYTEST_PCT_RE = re.compile(r"\[\s*(\d{1,3})%\]")
_FAULT_RE = re.compile(r"^\[coordinator\] fault ([^:]+): (PASS|FAIL)")
_RAW_PHY_RE = re.compile(r"^\[coordinator\] RAW_PHY COMPLETED (\[.*\])")
_RR_STAGES = ("prepare/configure/arm_rx", "start_cfo", "acquire_host_time",
              "calibrate_rf", "validate_rf_time PRE", "validate_rf_time POST",
              "RAW_PHY", "commit_service_start", "publish_service_start",
              "stop")


class QualifyProgress:
    """qualify 진행 표시 — 줄마다 RR phase n/N, 바의 퍼센트는 **그 phase 자체**
    (estimated: 경과/실측 가중치, fault subcase 6·pytest `[NN%]`는 exact),
    사다리 전체 진행률은 `overall=NN%` 필드(가중치 합)."""

    FAULT_SUBCASES = 6

    def __init__(self, reporter: ProgressReporter, *, clock=time.monotonic) -> None:
        self.reporter = reporter
        self.clock = clock
        self.completed_weight = 0.0
        self.phase_key = None
        self.phase_label = None
        self._phase_started_at = None
        self.phase_total = 0
        self.phase_index = 0
        self._console = None
        self._suite_tail = None
        self._suite_key = None
        self._suite_index = 0
        self.faults_done = 0
        self.stage = None
        self.host_time_events = 0

    # ---- 준비 게이트 --------------------------------------------------------

    def gate(self, name: str, **fields) -> None:
        line = self.reporter._line
        if line is not None and line["status"] is None \
                and str(line["label"]).startswith("Preflight"):
            self.reporter.update(fraction=1.0)   # 앞 게이트는 자기 100%로 확정
        self.reporter.phase("Preflight · %s" % name, mode=MODE_EXACT,
                            fraction=0.0, scope="QUALIFY", **fields)

    # ---- RR phase -----------------------------------------------------------

    def _overall(self, inner: float) -> float:
        weight = QUALIFY_WEIGHTS.get(self.phase_key, 0.0)
        return min(0.99, (self.completed_weight + inner * weight) / _QUALIFY_TOTAL_S)

    def _overall_field(self, inner: float) -> str:
        percent = int(self._overall(inner) * 100 + 1e-9)
        self._overall_max = max(getattr(self, "_overall_max", 0), percent)
        return "%d%%" % self._overall_max          # 전체 진행률도 후퇴 없음

    def _estimated_inner(self) -> float:
        weight = QUALIFY_WEIGHTS.get(self.phase_key, 1.0)
        if self._phase_started_at is None:
            return 0.0
        return min(0.95, (self.clock() - self._phase_started_at) / weight)

    def phase_started(self, phase: str, index: int, total: int, run_root) -> None:
        self.phase_key = phase
        self.phase_index, self.phase_total = index, total
        self._phase_started_at = self.clock()
        self.faults_done = 0
        self.stage = "prepare"
        self.host_time_events = 0
        self._console = FileTail(Path(run_root) / "coordinator-console.log")
        self.phase_label = "%s %d/%d" % (phase.upper(), index, total)
        self.reporter.phase(self.phase_label, mode=MODE_ESTIMATED,
                            fraction=0.0, scope="QUALIFY",
                            stage=self.stage, overall=self._overall_field(0.0))

    def _on_phase_console_line(self, line: str) -> None:
        f = _FAULT_RE.match(line)
        if f:
            self.faults_done += 1
            inner = self.faults_done / self.FAULT_SUBCASES
            self.reporter.update(
                mode=MODE_EXACT, fraction=inner,
                subcases="%d/%d" % (self.faults_done, self.FAULT_SUBCASES),
                last="%s:%s" % (f.group(1), f.group(2)),
                overall=self._overall_field(inner))
            return
        r = _RAW_PHY_RE.match(line)
        if r:
            paths = len(set(_RF_PATH_RE.findall(r.group(1))))
            self.reporter.update(stage="RAW_PHY done", paths="%d/6" % paths)
            return
        if line.startswith("[coordinator] "):
            body = line[len("[coordinator] "):]
            for stage in _RR_STAGES:
                if body.startswith(stage) and "COMPLETED" in body:
                    self.stage = stage + " done"
                    fields = {"stage": self.stage}
                    if stage == "acquire_host_time":
                        # waiting → estimated 복귀: 같은 갱신에서 퍼센트·overall도
                        # 즉시 경과 기준으로 맞춘다 (다음 tick까지 묵은 값 방지).
                        inner = self._estimated_inner()
                        self.reporter.update(mode=MODE_ESTIMATED, fraction=inner,
                                             overall=self._overall_field(inner),
                                             **fields)
                    else:
                        self.reporter.update(**fields)
                    return
            if body.startswith("RR-P0 PASS"):
                self.reporter.update(stage="RR-P0 PASS")
            return
        g = _GUARD_RE.match(line)
        if g and g.group(2):
            terminal = g.group(3)
            if terminal == "acquire_host_time":
                self.host_time_events += 1
                self.reporter.update(mode=MODE_WAITING,
                                     stage="acquire_host_time",
                                     events=self.host_time_events)
            elif terminal != self.stage:
                self.stage = terminal
                self.reporter.update(stage=terminal)

    def tick(self) -> None:
        """자식 대기 중 주기 호출 (run_child_bounded on_wait)."""
        if self._console is not None:
            for line in self._console.new_lines():
                self._on_phase_console_line(line)
            if self._phase_started_at is not None:
                inner = self._estimated_inner()
                if self.reporter._line and self.reporter._line["mode"] == MODE_ESTIMATED:
                    self.reporter.update(fraction=inner,
                                         overall=self._overall_field(inner))
        if self._suite_tail is not None:
            percent = None
            for line in self._suite_tail.new_lines():
                for m in _PYTEST_PCT_RE.finditer(line):
                    percent = int(m.group(1))
            if percent is not None:
                inner = percent / 100.0
                self.reporter.update(mode=MODE_EXACT, fraction=inner,
                                     tests="%d%%" % percent,
                                     overall=self._overall_field(inner))
        self.reporter.tick()

    def phase_finished(self, phase: str, status: str) -> None:
        self.completed_weight += QUALIFY_WEIGHTS.get(phase, 0.0)
        self._console = None
        if status == "PASS":
            # 이 phase 100%; overall은 완료 가중치 합(이 phase 가중치를 두 번
            # 세지 않는다 — 예전엔 여기서 99%로 튀어 뒤 phase가 99%에 고정됐다).
            self.reporter.update(fraction=1.0, mode=MODE_EXACT,
                                 overall=self._overall_field(0.0))
            self.reporter.finish("done", result="PASS")
        else:
            self.reporter.finish("FAILED", result=status)

    # ---- bundle / builder / promote ----------------------------------------

    def bundle_started(self) -> None:
        self._suite_index = 0
        self.phase_key = None

    def suite_started(self, log_name: str, log_path) -> None:
        self._suite_index += 1
        self.phase_key = "bundle:" + log_name
        self._phase_started_at = self.clock()
        self._suite_tail = FileTail(log_path)
        self.reporter.phase("Software suite %d/2 · %s"
                            % (self._suite_index, log_name.replace(".log", "")),
                            mode=MODE_EXACT, fraction=0.0,
                            scope="QUALIFY", tests="0%",
                            overall=self._overall_field(0.0))

    def suite_finished(self, log_name: str, ok: bool) -> None:
        self.completed_weight += QUALIFY_WEIGHTS.get("bundle:" + log_name, 0.0)
        self._suite_tail = None
        if ok:
            self.reporter.update(fraction=1.0, tests="100%",
                                 overall=self._overall_field(0.0))
            self.reporter.finish("done", result="PASS")
        else:
            self.reporter.finish("FAILED", result="FAIL")

    def builder_started(self) -> None:
        self.phase_key = "builder"
        self.reporter.phase("Final builder", mode=MODE_ESTIMATED,
                            fraction=0.0, scope="QUALIFY",
                            overall=self._overall_field(0.0))

    def promoted(self, release_id: str) -> None:
        self.completed_weight += QUALIFY_WEIGHTS.get("builder", 0.0)
        self.reporter.update(fraction=1.0, mode=MODE_EXACT,
                             label="QUALIFIED · active reference promoted",
                             release=release_id, overall="100%")
        self.reporter.finish("done")


# ------------------------------------------------------------ setup

SETUP_STEPS = ("apt dependency check", "apt install", "venv + pip lock",
               "identity", "B210 discovery", "hardware.toml draft",
               "installation manifest", "environment record", "user launcher")


class SetupProgress:
    """setup 단계 진행 — 단계 n/N exact (실제 단계 수 기준)."""

    def __init__(self, reporter: ProgressReporter) -> None:
        self.reporter = reporter
        self.index = 0
        self.total = len(SETUP_STEPS)

    def step(self, name: str, **fields) -> None:
        if self.index:
            self.reporter.update(fraction=1.0)   # 앞 단계는 자기 100%로 확정
        self.index += 1
        self.reporter.phase("Setup · %s" % name, mode=MODE_EXACT,
                            fraction=0.0, scope="SETUP",
                            step="%d/%d" % (self.index, self.total), **fields)

    def done(self) -> None:
        self.reporter.update(fraction=1.0)
        self.reporter.finish("done")


# ------------------------------------------------------------ 생성 도우미

class GateProgress:
    """RF 접근 전 준비 게이트 n/N (exact) — run/qualify prepare 공용."""

    def __init__(self, reporter: ProgressReporter, total: int,
                 scope: str = "PREFLIGHT") -> None:
        self.reporter = reporter
        self.total = total
        self.index = 0
        self.scope = scope

    def gate(self, name: str, **fields) -> None:
        if self.index:
            self.reporter.update(fraction=1.0)   # 앞 게이트는 자기 100%로 확정
        self.index += 1
        self.reporter.phase("Preflight · %s" % name, mode=MODE_EXACT,
                            fraction=0.0, scope=self.scope,
                            gate="%d/%d" % (self.index, self.total), **fields)

    def done(self) -> None:
        self.reporter.update(fraction=1.0)
        self.reporter.finish("done")


class MirroredOut:
    """출력 콜백 + evidence 파일 미러 (presentation 로그 보존)."""

    def __init__(self, out, path: Path | str | None) -> None:
        self._out = out
        self._path = Path(path) if path else None

    def set_path(self, path: Path | str | None) -> None:
        self._path = Path(path) if path else None

    def __call__(self, text: str) -> None:
        text = scrub_text(str(text))
        self._out(text)
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8",
                          newline="\n") as handle:
                    handle.write("%s %s\n" % (utc_now(), text))
            except OSError:
                pass


def make_reporter(*, plain: bool = False, sink=None, stream=None,
                  scope: str = "PROGRESS", evidence_log=None,
                  ticker: bool = True, enabled: bool = True) -> ProgressReporter:
    """CLI 공통 생성. `plain=True`(`--no-progress`)면 TTY라도 진행바 없이
    줄 단위 상태 출력만 쓴다 (carriage return·ANSI 없음)."""
    return ProgressReporter(sink, stream=stream, enabled=enabled,
                            tty=(False if plain else None), scope=scope,
                            evidence_log=evidence_log, ticker=ticker)
