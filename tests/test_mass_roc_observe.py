"""관찰 도구 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_observe.py

관찰 도구는 후속 공격 실험에서 "무엇이 달라졌는가"를 판정하는 근거가 된다.
따라서 기록이 정확한지, 표시가 자료를 왜곡하지 않는지를 여기서 확인한다.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.textout import ensure_utf8_output

ensure_utf8_output()

from common.messages import position_report as pos
from common.observe import (
    EVENT_COMMAND,
    EVENT_LINK,
    EVENT_ROUTE,
    OUTCOME_DELIVERED,
    OUTCOME_LOST,
    OUTCOME_NO_SLOT,
    OUTCOME_REJECTED,
    Recorder,
    render_console,
    write_report,
)

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


MASS = 440_000_001


# 위치보고 하나를 만든다.
def report(lat: float, lon: float, cog: float, sog: float = 12.0) -> pos.PositionReport:
    return pos.from_measurements(
        latitude_degrees=lat,
        longitude_degrees=lon,
        cog_degrees=cog,
        sog_knots=sog,
        heading_degrees=int(cog) % 360,
        navigation_status=0,
        autonomy_mode=pos.AUTONOMY_RC,
    )


# 항적 하나를 기록한다. gap 구간에는 관측을 넣지 않아 통신 두절을 흉내낸다.
def build(name: str, gap: tuple[float, float] | None = None) -> Recorder:
    log = Recorder(name=name)
    log.plan([(35.105, 129.105), (35.112, 129.108)])
    for step in range(0, 120, 2):
        now = float(step)
        if gap and gap[0] <= now < gap[1]:
            log.transfer(now, MASS, 0, 93, "Position Report", 17, 33,
                         outcome=OUTCOME_LOST, reason="차단")
            continue
        log.observe(report(35.10 + step * 0.0002, 129.10 + step * 0.0001, 20.0), MASS, now)
        log.transfer(now, MASS, 0, 93, "Position Report", 17, 33)
    return log


# ------------------------------------------------------------------ 기록

section("Recorder — 기록")

log = build("정상")
log.event(20.0, EVENT_COMMAND, "항로 지시", "Linkage 1")
log.event(60.0, EVENT_ROUTE, "WP1 통과")

check(len(log.samples) == 60, "위치보고를 모두 기록한다", f"{len(log.samples)}건")
check(len(log.transfers) == 60, "링크 전송을 모두 기록한다")
check(log.tracked_ships == [MASS], "송신원으로 항적을 구분한다")
check(len(log.track(MASS)) == 60, "선박별 항적을 골라낸다")
check(log.transmitted_bytes == 60 * 33, "전파 나간 양을 합산한다",
      f"{log.transmitted_bytes:,} B")

info = log.summary()
check(info["samples"] == 60 and info["transfers"] == 60, "요약이 기록과 일치한다")
check(abs(info["duration"] - 118.0) < 1e-9, "관측 시간을 계산한다", f"{info['duration']:.0f}s")

section("Recorder — 통신 두절 감지")

gapped = build("두절", gap=(40.0, 80.0))
check(gapped.lost_transfers == 20, "송신 후 유실을 센다", f"{gapped.lost_transfers}건")
check(gapped.longest_gap(MASS) >= 40.0, "가장 긴 관측 공백을 찾는다",
      f"{gapped.longest_gap(MASS):.0f}s")
check(log.longest_gap(MASS) <= 2.0, "정상 기록에는 큰 공백이 없다",
      f"{log.longest_gap(MASS):.0f}s")

section("Transfer — 송신 결과 구분 (SC-006 계열)")

# 참/거짓 두 가지로는 "슬롯이 없어 못 보낸 것"과 "보냈으나 유실된 것"을
# 구분할 수 없다. 공격 유형이 다르므로 반드시 갈라야 한다.
mixed = Recorder(name="혼합")
mixed.transfer(0.0, MASS, 0, 93, "P", 17, 33)                                   # delivered
mixed.transfer(1.0, MASS, 0, 93, "P", 17, 33, outcome=OUTCOME_LOST)             # 전파 나감
mixed.transfer(2.0, MASS, 0, 93, "P", 17, 33, outcome=OUTCOME_NO_SLOT,
               attempts=3, reason="RAC 슬롯 고갈")                               # 전파 안 나감
mixed.transfer(3.0, MASS, 0, 92, "C", 17, 33, outcome=OUTCOME_REJECTED,
               reason="PAYLOAD_TOO_LARGE")

check(mixed.outcome_counts == {"delivered": 1, "lost": 1, "no-slot": 1, "rejected": 1},
      "outcome별로 집계된다", str(mixed.outcome_counts))
check(mixed.lost_transfers == 1 and mixed.no_slot_transfers == 1,
      "유실과 기회 없음이 따로 세어진다", "SC-006이 요구하는 구분")
check(mixed.undelivered_transfers == 3, "닿지 않은 건수는 원인 불문 3건")

# 전파가 실제로 나간 것만 전송량에 넣는다
check(mixed.transmitted_bytes == 66, "전파 나간 양은 delivered+lost만",
      f"{mixed.transmitted_bytes} B (no-slot·rejected 제외)")
check(mixed.delivered_bytes == 33, "수신된 양은 delivered만")

check(mixed.transfers[0].transmitted and mixed.transfers[1].transmitted,
      "delivered와 lost는 전파가 나갔다")
check(not mixed.transfers[2].transmitted and not mixed.transfers[3].transmitted,
      "no-slot과 rejected는 전파가 나가지 않았다")

# 재시도 추적
check(mixed.total_attempts == 6, "총 시도 횟수를 센다", f"{mixed.total_attempts}회")
check(mixed.retried_transfers == 1, "재시도가 있었던 건수", "3회 시도한 1건")
check(mixed.transfers[2].reason == "RAC 슬롯 고갈", "실패 사유가 남는다")
check(mixed.transfers[2].outcome_label == "송신 기회 없음", "사람이 읽는 이름이 있다")

# 잘못된 outcome은 거부
try:
    mixed.transfer(4.0, MASS, 0, 93, "P", 17, 33, outcome="made-up")
    check(False, "알 수 없는 outcome을 거부한다")
except ValueError:
    check(True, "알 수 없는 outcome을 거부한다")

# ------------------------------------------------------------ 콘솔 표시

section("render_console — 시간순 병합")

text = render_console(log, interval=60.0)
check("항로 지시" in text and "WP1 통과" in text, "사건이 표에 나타난다")

# 사건 시각이 관측 시각과 어긋나도 순서가 지켜져야 한다
odd = build("사건")
odd.event(31.0, EVENT_ROUTE, "홀수초 사건")
odd.event(11.0, EVENT_COMMAND, "이른 사건")
lines = [l for l in render_console(odd, interval=1000.0).splitlines() if l.strip()]
times = []
for line in lines:
    head = line.split()[0]
    if head.replace(".", "").isdigit():
        times.append(float(head))
check(times == sorted(times), "행이 시간순으로 정렬된다", f"{times}")
check(any("이른 사건" in l for l in lines) and any("홀수초 사건" in l for l in lines),
      "관측이 없는 시각의 사건도 빠지지 않는다")

# ------------------------------------------------------------ HTML 보고서

section("write_report — 자체 완결 HTML")

with tempfile.TemporaryDirectory() as tmp:
    path = write_report(Path(tmp) / "r.html", log, title="시험 보고서")
    doc = path.read_text(encoding="utf-8")

    check(path.exists() and len(doc) > 2000, "보고서를 만든다", f"{len(doc):,} byte")
    check(doc.startswith("<!doctype html>"), "온전한 HTML 문서다")
    check("시험 보고서" in doc, "제목이 들어간다")

    # 외부 자원을 전혀 참조하지 않아야 어디서든 열린다
    for marker in ("http://", "https://", "<script", "<img", "@import"):
        check(marker not in doc, f"외부 자원을 쓰지 않는다 — {marker!r} 없음")

    check(doc.count("<svg") == 3, "해도와 시계열 2종을 그린다", f"{doc.count('<svg')}개")
    check("항로 지시" in doc and "WP1 통과" in doc, "사건이 보고서에 실린다")
    check("Position Report" in doc, "메시지 종류가 실린다")

    # 두 테마 모두 정의되어야 한다
    check("prefers-color-scheme:dark" in doc, "어두운 테마를 정의한다")
    check('data-theme="dark"' in doc, "테마 선택도 지원한다")

section("write_report — HTML 이스케이프")

with tempfile.TemporaryDirectory() as tmp:
    risky = build("<script>alert(1)</script>")
    risky.event(1.0, EVENT_LINK, "a & b <tag>")
    doc = write_report(Path(tmp) / "r.html", risky).read_text(encoding="utf-8")
    check("<script>alert(1)</script>" not in doc, "이름의 태그를 이스케이프한다")
    check("&lt;script&gt;" in doc, "이스케이프된 형태로 들어간다")
    check("a &amp; b &lt;tag&gt;" in doc, "사건 문구도 이스케이프한다")

# --------------------------------------------------------- 각도 순환

section("시계열 — 침로 0/360 순환")

# 354도에서 1도로 넘어가는 것은 3도 우선회다. 그대로 이으면 화면에서
# 배가 한 바퀴 돈 것처럼 보이므로 선이 끊겨야 한다.
wrap = Recorder(name="북쪽 통과")
for index, cog in enumerate([350.0, 354.0, 358.0, 2.0, 6.0, 10.0]):
    wrap.observe(report(35.0 + index * 0.001, 129.0, cog), MASS, float(index * 2))

with tempfile.TemporaryDirectory() as tmp:
    doc = write_report(Path(tmp) / "r.html", wrap).read_text(encoding="utf-8")

straight = Recorder(name="직진")
for index, cog in enumerate([10.0, 14.0, 18.0, 22.0, 26.0, 30.0]):
    straight.observe(report(35.0 + index * 0.001, 129.0, cog), MASS, float(index * 2))
with tempfile.TemporaryDirectory() as tmp:
    doc_straight = write_report(Path(tmp) / "r.html", straight).read_text(encoding="utf-8")

check(
    doc.count("<polyline") > doc_straight.count("<polyline"),
    "순환 지점에서 선을 끊는다",
    f"순환 {doc.count('<polyline')}개 vs 직진 {doc_straight.count('<polyline')}개",
)

# --------------------------------------------------------- 비교 모드

section("write_report — 비교 모드 (behavioral oracle)")

control = build("정상 동작")
control.event(20.0, EVENT_COMMAND, "항로 지시")
compared = build("통신 두절", gap=(40.0, 80.0))
compared.event(20.0, EVENT_COMMAND, "항로 지시")
compared.event(50.0, EVENT_LINK, "통신 두절 판정")

with tempfile.TemporaryDirectory() as tmp:
    doc = write_report(
        Path(tmp) / "cmp.html", control, compared, title="비교"
    ).read_text(encoding="utf-8")

    check("정상 동작" in doc and "통신 두절" in doc, "두 기록이 모두 실린다")
    check(doc.count('class="card"') == 2, "요약 카드가 둘이다")
    check("두 선이 갈라지는 지점" in doc, "비교용 안내 문구로 바뀐다")
    # 실행마다 다른 색을 쓴다
    check("#2E6E8E" in doc and "#A81B6B" in doc, "실행별로 색을 나눈다",
          "대조군 청색 · 비교군 마젠타")
    # 사건·메시지 표는 실행별로 따로 낸다
    check(doc.count("<table") >= 4, "사건·메시지 표를 실행별로 낸다",
          f"{doc.count('<table')}개")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
