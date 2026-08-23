"""
관찰 보고서 - 기록을 HTML 한 장으로 낸다

외부 라이브러리도, 인터넷 연결도 쓰지 않는다. SVG를 직접 그려 넣으므로
파일 하나만 있으면 어디서든 열린다.

무엇을 보여주는가
    1. 항적 해도 - 계획 항로와 실제 관측 항적을 겹쳐 그린다
    2. 침로·속력 시계열 - 명령 전후로 무엇이 바뀌었는지
    3. 사건과 메시지 - 언제 무엇이 오갔는지

비교 모드
    write_report에 기록을 여러 개 넘기면 같은 축 위에 겹쳐 그린다.
    첫 번째가 대조군(정상), 나머지가 비교 대상이다. 후속 공격 실험에서
    "공격이 MASS를 어떻게 바꿨는가"를 이 겹침으로 보여준다.
    docs/mass_roc_background.md 2.7의 behavioral oracle 구조다.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

from ..messages import position_report as pos
from .recorder import (
    EVENT_COMMAND,
    EVENT_LINK,
    EVENT_REJECT,
    EVENT_ROUTE,
    OUTCOME_DELIVERED,
    OUTCOME_LABELS,
    OUTCOME_LOST,
    OUTCOME_NO_SLOT,
    Recorder,
)

# 실행별 색. 첫 번째(대조군)는 차분한 청색, 이후는 해도 마젠타 계열.
RUN_COLORS = ["#2E6E8E", "#A81B6B", "#8A6A1F", "#3F7A50"]

_CHART_W, _CHART_H = 720, 400
_SERIES_W, _SERIES_H = 720, 170
_PAD = 46


# 위도·경도를 화면 좌표로 옮긴다.
# 경도는 위도에 따라 좁아지므로 cos(lat)으로 보정해야 항적이 찌그러지지 않는다.
class _Projection:
    def __init__(self, points: list[tuple[float, float]], width: int, height: int) -> None:
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        self._lat0, self._lat1 = min(lats), max(lats)
        self._lon0, self._lon1 = min(lons), max(lons)

        mean_lat = math.radians((self._lat0 + self._lat1) / 2)
        span_y = max(self._lat1 - self._lat0, 1e-9)
        span_x = max((self._lon1 - self._lon0) * math.cos(mean_lat), 1e-9)

        # 가로세로 비를 유지하면서 여백 안에 맞춘다.
        usable_w, usable_h = width - 2 * _PAD, height - 2 * _PAD
        scale = min(usable_w / span_x, usable_h / span_y)
        self._scale = scale
        self._cos = math.cos(mean_lat)
        # 그린 내용을 여백 안에서 가운데 놓는다.
        self._left = _PAD + (usable_w - span_x * scale) / 2
        self._top = _PAD + (usable_h - span_y * scale) / 2

    def xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        x = self._left + (longitude - self._lon0) * self._cos * self._scale
        # SVG는 y가 아래로 커지므로 북쪽(위도 최대)이 위에 오도록 뒤집는다.
        y = self._top + (self._lat1 - latitude) * self._scale
        return x, y


def _esc(text: object) -> str:
    return html.escape(str(text))


# 항적 해도
def _chart(runs: list[Recorder]) -> str:
    points: list[tuple[float, float]] = []
    for run in runs:
        points.extend(run.planned_route)
        for sample in run.samples:
            if sample.has_position:
                points.append((sample.latitude, sample.longitude))
    if len(points) < 2:
        return '<p class="empty">항적을 그릴 위치 자료가 없다.</p>'

    projection = _Projection(points, _CHART_W, _CHART_H)
    parts = [
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
        f'aria-label="계획 항로와 실제 관측 항적을 겹쳐 그린 해도">'
    ]

    # 계획 항로 — 첫 기록 기준
    plan = runs[0].planned_route
    if plan:
        coords = [projection.xy(lat, lon) for lat, lon in plan]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="currentColor" '
            f'stroke-width="1.4" stroke-dasharray="7 5" opacity="0.45"/>'
        )
        for index, (x, y) in enumerate(coords, start=1):
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="none" '
                f'stroke="currentColor" stroke-width="1.4" opacity="0.6"/>'
                f'<text x="{x:.1f}" y="{y - 11:.1f}" text-anchor="middle" '
                f'font-size="10.5" fill="currentColor" opacity="0.6">WP{index}</text>'
            )

    # 실제 항적
    for index, run in enumerate(runs):
        color = RUN_COLORS[index % len(RUN_COLORS)]
        track = [s for s in run.samples if s.has_position]
        if len(track) < 2:
            continue
        coords = [projection.xy(s.latitude, s.longitude) for s in track]
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.2"/>'
        )
        sx, sy = coords[0]
        ex, ey = coords[-1]
        parts.append(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="4" fill="{color}"/>'
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="6" fill="none" '
            f'stroke="{color}" stroke-width="2.4"/>'
        )

    parts.append("</svg>")
    return "".join(parts)


# 시계열 하나(침로 또는 속력)
#
# angular=True는 0도와 360도가 같은 값인 방위각이다. 354도에서 1도로 넘어가는
# 것은 3도 우선회인데, 그대로 이으면 화면에서는 353도만큼 아래로 떨어지는
# 수직선이 되어 배가 한 바퀴 돈 것처럼 보인다. 그런 구간에서는 선을 끊는다.
def _series(
    runs: list[Recorder], field: str, label: str, unit: str, angular: bool = False
) -> str:
    values: list[float] = []
    times: list[float] = []
    for run in runs:
        for sample in run.samples:
            value = getattr(sample, field)
            if value is not None:
                values.append(value)
                times.append(sample.time)
    if len(values) < 2:
        return ""

    t0, t1 = min(times), max(times)
    v0, v1 = min(values), max(values)
    if v1 - v0 < 1e-9:
        v0, v1 = v0 - 1, v1 + 1
    span_t = max(t1 - t0, 1e-9)
    span_v = v1 - v0

    def xy(time: float, value: float) -> tuple[float, float]:
        x = _PAD + (time - t0) / span_t * (_SERIES_W - 2 * _PAD)
        y = _SERIES_H - _PAD - (value - v0) / span_v * (_SERIES_H - 2 * _PAD)
        return x, y

    parts = [
        f'<svg viewBox="0 0 {_SERIES_W} {_SERIES_H}" role="img" '
        f'aria-label="{_esc(label)} 시계열">'
    ]
    # 축
    parts.append(
        f'<line x1="{_PAD}" y1="{_SERIES_H - _PAD}" x2="{_SERIES_W - _PAD}" '
        f'y2="{_SERIES_H - _PAD}" stroke="currentColor" stroke-width="1" opacity="0.3"/>'
        f'<line x1="{_PAD}" y1="{_PAD - 8}" x2="{_PAD}" y2="{_SERIES_H - _PAD}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.3"/>'
    )
    for value in (v0, v1):
        _, y = xy(t0, value)
        parts.append(
            f'<text x="{_PAD - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10.5" '
            f'fill="currentColor" opacity="0.55">{value:.0f}</text>'
        )
    parts.append(
        f'<text x="{_PAD}" y="{_SERIES_H - _PAD + 18:.0f}" font-size="10.5" '
        f'fill="currentColor" opacity="0.55">{t0:.0f}s</text>'
        f'<text x="{_SERIES_W - _PAD}" y="{_SERIES_H - _PAD + 18:.0f}" text-anchor="end" '
        f'font-size="10.5" fill="currentColor" opacity="0.55">{t1:.0f}s</text>'
        f'<text x="{_PAD}" y="{_PAD - 14}" font-size="11.5" font-weight="600" '
        f'fill="currentColor">{_esc(label)} <tspan opacity="0.55">{_esc(unit)}</tspan></text>'
    )

    for index, run in enumerate(runs):
        color = RUN_COLORS[index % len(RUN_COLORS)]
        points = [
            (s.time, getattr(s, field))
            for s in run.samples
            if getattr(s, field) is not None
        ]
        if len(points) < 2:
            continue

        # 방위각이 0/360을 넘나드는 지점에서 선을 끊어 여러 구간으로 나눈다.
        segments: list[list[tuple[float, float]]] = [[]]
        previous: float | None = None
        for time, value in points:
            if angular and previous is not None and abs(value - previous) > 180:
                segments.append([])
            segments[-1].append(xy(time, value))
            previous = value

        for segment in segments:
            if len(segment) < 2:
                continue
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.9"/>'
            )

    parts.append("</svg>")
    return "".join(parts)


_EVENT_CLASS = {
    EVENT_COMMAND: "ev-cmd",
    EVENT_ROUTE: "ev-route",
    EVENT_LINK: "ev-link",
    EVENT_REJECT: "ev-reject",
}


def _events_table(run: Recorder) -> str:
    if not run.events:
        return '<p class="empty">기록된 사건이 없다.</p>'
    rows = []
    for event in sorted(run.events, key=lambda e: e.time):
        css = _EVENT_CLASS.get(event.kind, "ev-note")
        rows.append(
            f'<tr><td class="num">{event.time:.0f}s</td>'
            f'<td><span class="tag {css}">{_esc(event.kind)}</span></td>'
            f"<td>{_esc(event.label)}</td>"
            f'<td class="src">{_esc(event.detail)}</td></tr>'
        )
    return (
        '<div class="tw"><table><thead><tr><th class="num">시각</th><th>종류</th>'
        "<th>사건</th><th>비고</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


# 0이 아니면 눈에 띄게 표시한다.
def _warn(count: int) -> str:
    return f'<span class="warn">{count}</span>' if count else "0"


def _transfers_table(run: Recorder) -> str:
    if not run.transfers:
        return '<p class="empty">오간 메시지가 없다.</p>'

    # 같은 메시지가 반복되므로 종류별로 묶어 보여준다.
    grouped: dict[tuple[str, int], dict[str, int]] = {}
    for item in run.transfers:
        key = (item.message, item.vde_type)
        entry = grouped.setdefault(
            key,
            {"count": 0, "lost": 0, "no_slot": 0, "attempts": 0, "payload": 0, "burst": 0},
        )
        entry["count"] += 1
        entry["attempts"] += item.attempts
        entry["payload"] = item.payload_bytes
        if item.outcome == OUTCOME_LOST:
            entry["lost"] += 1
        elif item.outcome == OUTCOME_NO_SLOT:
            entry["no_slot"] += 1
        if item.transmitted:
            entry["burst"] += item.burst_bytes

    rows = []
    for (message, vde_type), entry in sorted(grouped.items()):
        lost = entry["lost"]
        no_slot = entry["no_slot"]
        retries = entry["attempts"] - entry["count"]
        rows.append(
            f"<tr><td><b>{_esc(message)}</b></td>"
            f'<td class="num">Type {vde_type}</td>'
            f'<td class="num">{entry["count"]}</td>'
            f'<td class="num">{_warn(lost)}</td>'
            f'<td class="num">{_warn(no_slot)}</td>'
            f'<td class="num">{retries}</td>'
            f'<td class="num">{entry["payload"]} B</td>'
            f'<td class="num">{entry["burst"]:,} B</td></tr>'
        )
    return (
        '<div class="tw"><table><thead><tr><th>메시지</th><th class="num">VDE</th>'
        '<th class="num">건수</th><th class="num">유실</th>'
        '<th class="num">기회 없음</th><th class="num">재시도</th>'
        '<th class="num">payload</th><th class="num">전파 나간 양</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _summary_cards(runs: list[Recorder]) -> str:
    cards = []
    for index, run in enumerate(runs):
        info = run.summary()
        color = RUN_COLORS[index % len(RUN_COLORS)]
        ships = run.tracked_ships
        mode = "—"
        if run.samples:
            mode = pos.AUTONOMY_MODE_NAMES.get(run.samples[-1].autonomy_mode, "?")
        cards.append(
            f'<div class="card"><div class="swatch" style="background:{color}"></div>'
            f'<div class="ttl">{_esc(run.name)}</div>'
            f'<div class="sub">{_esc(run.note) if run.note else "&nbsp;"}</div>'
            "<dl>"
            f'<div><dt>관측 시간</dt><dd>{info["duration"]:.0f} s</dd></div>'
            f'<div><dt>위치보고</dt><dd>{info["samples"]:,}</dd></div>'
            f'<div><dt>최대 관측 공백</dt><dd>{info["gap"]:.0f} s</dd></div>'
            f'<div><dt>송신 후 유실</dt><dd>{info["lost"]}</dd></div>'
            f'<div><dt>송신 기회 없음</dt><dd>{info["no_slot"]}</dd></div>'
            f'<div><dt>총 송신 시도</dt><dd>{info["attempts"]:,}</dd></div>'
            f'<div><dt>전파 나간 양</dt><dd>{info["bytes"]:,} B</dd></div>'
            f"<div><dt>최종 모드</dt><dd>{_esc(mode)}</dd></div>"
            "</dl></div>"
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


_STYLE = """
:root{--paper:#FAF9F5;--panel:#F1F0EA;--water:#DEE9EE;--ink:#12262F;--slate:#56676F;
--rule:#D5D8D3;--magenta:#A81B6B;--warn:#B4462A;
--sans:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",Pretendard,"Malgun Gothic","Noto Sans KR",sans-serif;
--mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#0C1B23;--panel:#142831;
--water:#17303B;--ink:#DCE6EA;--slate:#90A4AD;--rule:#24404C;--magenta:#F07CB8;--warn:#E8875F}}
:root[data-theme="dark"]{--paper:#0C1B23;--panel:#142831;--water:#17303B;--ink:#DCE6EA;
--slate:#90A4AD;--rule:#24404C;--magenta:#F07CB8;--warn:#E8875F}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:15px;
line-height:1.65;margin:0;padding:0 24px 80px;-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto}
header{padding:56px 0 26px;border-bottom:2px solid var(--ink);display:flex;
flex-direction:column;gap:8px}
h1{font-size:clamp(24px,4.4vw,34px);letter-spacing:-.025em;line-height:1.15;margin:0;font-weight:700}
h2{font-size:19px;letter-spacing:-.015em;margin:0;font-weight:700}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
color:var(--magenta);margin:0}
.lede{color:var(--slate);margin:0;max-width:62ch}
section{display:flex;flex-direction:column;gap:14px;padding:44px 0 0}
p{margin:0;max-width:68ch}
.note{font-size:13.5px;color:var(--slate)}
.empty{color:var(--slate);font-size:14px;font-style:italic}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.card{border:1px solid var(--rule);background:var(--panel);padding:16px;position:relative;
display:flex;flex-direction:column;gap:2px}
.swatch{position:absolute;top:0;left:0;width:100%;height:3px}
.ttl{font-weight:700;font-size:15px;margin-top:6px}
.sub{font-size:12.5px;color:var(--slate);margin-bottom:8px}
dl{margin:0;display:flex;flex-direction:column;gap:5px}
dl>div{display:flex;justify-content:space-between;gap:10px;font-size:13.5px}
dt{color:var(--slate)}
dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums}
.canvas{background:var(--panel);border:1px solid var(--rule);padding:16px;overflow-x:auto}
svg{display:block;max-width:100%;height:auto;margin:0 auto}
figure{margin:0;display:flex;flex-direction:column;gap:10px}
figcaption{font-size:13.5px;color:var(--slate);max-width:68ch}
.tw{overflow-x:auto;border:1px solid var(--rule)}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:520px}
th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rule);vertical-align:top}
thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
color:var(--slate);font-weight:500;background:var(--panel);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;
white-space:nowrap}
.src{color:var(--slate);font-size:13px}
.warn{color:var(--warn);font-weight:700}
.tag{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
padding:2px 7px;border:1px solid currentColor;white-space:nowrap}
.ev-cmd{color:var(--magenta)}
.ev-route{color:var(--slate)}
.ev-link{color:var(--warn)}
.ev-reject{color:var(--warn)}
.ev-note{color:var(--slate)}
.legend{display:flex;flex-wrap:wrap;gap:16px;font-size:13px;color:var(--slate)}
.legend span{display:flex;align-items:center;gap:7px}
.legend i{width:16px;height:3px;display:inline-block}
footer{margin-top:52px;padding-top:18px;border-top:1px solid var(--rule);
font-size:13px;color:var(--slate)}
"""


# 기록 하나 이상을 HTML 보고서 한 장으로 낸다. 첫 번째가 대조군이다.
def write_report(path: str | Path, *runs: Recorder, title: str = "MASS/ROC 관찰 보고서") -> Path:
    if not runs:
        raise ValueError("기록이 최소 하나 필요합니다")

    comparing = len(runs) > 1
    legend = "".join(
        f'<span><i style="background:{RUN_COLORS[i % len(RUN_COLORS)]}"></i>'
        f"{_esc(run.name)}</span>"
        for i, run in enumerate(runs)
    )
    legend += (
        '<span><i style="background:currentColor;opacity:.45"></i>계획 항로</span>'
    )

    lede = (
        "정상 대조군과 비교 대상을 같은 축에 겹쳐 그렸다. 두 선이 갈라지는 지점이 "
        "관찰해야 할 변화다."
        if comparing
        else "ROC가 관측한 것만 기록했다. MASS의 내부 상태가 아니라 "
        "수신해서 알게 된 상태이므로, 둘이 어긋나면 그 어긋남이 그대로 드러난다."
    )

    body = [
        '<div class="wrap"><header>',
        '<p class="eyebrow">VDE-TER · MASS/ROC 관찰</p>',
        f"<h1>{_esc(title)}</h1>",
        f'<p class="lede">{lede}</p>',
        "</header>",
        f"<section>{_summary_cards(list(runs))}</section>",
        '<section><h2>항적</h2>',
        f'<div class="legend">{legend}</div>',
        f'<figure><div class="canvas">{_chart(list(runs))}</div>',
        "<figcaption>점선이 ROC가 지시한 계획 항로, 실선이 실제 관측된 항적이다. "
        "속이 찬 점이 시작, 빈 원이 마지막 관측 위치다.</figcaption></figure>",
        "</section>",
    ]

    cog = _series(list(runs), "cog", "침로 COG", "(도)", angular=True)
    sog = _series(list(runs), "sog", "속력 SOG", "(knot)")
    if cog or sog:
        body.append("<section><h2>침로와 속력</h2>")
        if cog:
            body.append(f'<div class="canvas">{cog}</div>')
        if sog:
            body.append(f'<div class="canvas">{sog}</div>')
        body.append(
            '<p class="note">명령을 받은 뒤 침로가 바뀌고, waypoint를 지날 때마다 다시 꺾이는 것이 '
            "보인다. 침로 선이 끊긴 곳은 0도와 360도 사이를 지난 지점이다."
            "</p></section>"
        )

    for run in runs:
        heading = f"사건 — {_esc(run.name)}" if comparing else "사건"
        body.append(f"<section><h2>{heading}</h2>{_events_table(run)}</section>")
        heading = f"오간 메시지 — {_esc(run.name)}" if comparing else "오간 메시지"
        body.append(f"<section><h2>{heading}</h2>{_transfers_table(run)}</section>")

    body.append(
        '<footer>mass_roc/observe · 응용 계층이 관측한 것만 기록한다. '
        "링크·물리 계층 상태는 포함하지 않는다.</footer></div>"
    )

    document = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )

    output = Path(path)
    output.write_text(document, encoding="utf-8")
    return output
