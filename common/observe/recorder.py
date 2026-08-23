"""
관찰 기록기 - ROC가 무엇을 보았는지 시간순으로 남긴다

구상도의 "어떻게 변하는지 확인하는 도구"의 수집 부분이다.
표시는 report.py가 맡고, 여기서는 기록만 한다. 둘을 나눠 두면 같은 기록을
콘솔로도, HTML 보고서로도, 나중에 다른 형식으로도 낼 수 있다.

무엇을 기록하는가
    Sample   - ROC가 관측한 선박 상태(위치보고를 푼 결과)
    Transfer - 링크로 오간 메시지 한 건
    Event    - 사람이 읽을 사건(명령 발행, waypoint 통과, 통신 두절 등)

왜 "ROC가 본 것"인가
    MASS의 내부 상태가 아니라 ROC가 수신해서 알게 된 상태를 남긴다.
    위조된 위치보고를 받으면 ROC의 인식이 실제와 어긋나는데, 그 어긋남 자체가
    후속 공격 실험에서 관찰해야 할 대상이기 때문이다.
    docs/mass_roc_background.md 2.7의 behavioral oracle 구조를 따른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..messages import position_report as pos

# 사건 종류. report.py가 색과 표시를 정할 때 쓴다.
EVENT_COMMAND = "command"  # 명령 발행·응답
EVENT_ROUTE = "route"  # waypoint 통과, 항로 완주
EVENT_LINK = "link"  # 통신 두절·복구
EVENT_REJECT = "reject"  # 거부·무시
EVENT_NOTE = "note"  # 그 밖의 기록


# ROC가 한 시점에 관측한 선박 상태
@dataclass
class Sample:
    time: float
    source_id: int
    latitude: float | None
    longitude: float | None
    cog: float | None
    sog: float | None
    heading: int | None
    autonomy_mode: int
    navigation_status: int

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


# 송신 시도 한 건의 결과
#
# 참/거짓 두 가지로는 부족하다. "슬롯이 없어 송신조차 못 한 것"과
# "송신했으나 유실된 것"은 공격 유형이 다르다. 전자는 자원 고갈(DoS)이고
# 후자는 전파 방해다. 관찰 도구가 둘을 구분하지 못하면 공격을 잘못 읽는다.
#
# RAC 슬롯 고갈 시나리오(SC-006 계열)가 이 구분을 요구한다.
OUTCOME_DELIVERED = "delivered"  # 송신했고 상대가 받았다
OUTCOME_LOST = "lost"  # 송신했으나 상대에게 닿지 않았다
OUTCOME_NO_SLOT = "no-slot"  # 송신 기회를 얻지 못했다 (RAC 슬롯 없음)
OUTCOME_REJECTED = "rejected"  # 요청 자체가 거부됐다 (크기 초과 등)

# 실제로 전파가 나간 결과인가. 공격 해석이 갈리는 지점이다.
TRANSMITTED_OUTCOMES = frozenset({OUTCOME_DELIVERED, OUTCOME_LOST})

OUTCOME_LABELS = {
    OUTCOME_DELIVERED: "수신됨",
    OUTCOME_LOST: "송신 후 유실",
    OUTCOME_NO_SLOT: "송신 기회 없음",
    OUTCOME_REJECTED: "요청 거부",
}


# 링크로 오간 메시지 한 건
@dataclass
class Transfer:
    time: float
    source_id: int
    destination_id: int
    vde_type: int
    message: str
    payload_bytes: int
    burst_bytes: int
    outcome: str = OUTCOME_DELIVERED
    attempts: int = 1
    reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_LABELS:
            raise ValueError(
                f"알 수 없는 outcome입니다 ({self.outcome!r}). "
                f"허용 값: {sorted(OUTCOME_LABELS)}"
            )

    # 상대가 받았는가
    @property
    def delivered(self) -> bool:
        return self.outcome == OUTCOME_DELIVERED

    # 전파가 실제로 나갔는가. no-slot과 lost를 가르는 기준이다.
    @property
    def transmitted(self) -> bool:
        return self.outcome in TRANSMITTED_OUTCOMES

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS[self.outcome]


# 사람이 읽는 사건
@dataclass
class Event:
    time: float
    kind: str
    label: str
    detail: str = ""


@dataclass
class Recorder:
    name: str = "run"
    note: str = ""

    def __post_init__(self) -> None:
        self.samples: list[Sample] = []
        self.transfers: list[Transfer] = []
        self.events: list[Event] = []
        # 계획된 항로(위도, 경도). 실제 항적과 견주기 위해 따로 둔다.
        self.planned_route: list[tuple[float, float]] = []

    # ---------------------------------------------------------- 기록

    # ROC가 위치보고를 받았을 때 부른다.
    def observe(self, report: pos.PositionReport, source_id: int, now: float) -> None:
        self.samples.append(
            Sample(
                time=now,
                source_id=source_id,
                latitude=report.latitude_degrees,
                longitude=report.longitude_degrees,
                cog=report.cog_degrees,
                sog=report.sog_knots,
                heading=report.heading_degrees,
                autonomy_mode=report.autonomy_mode,
                navigation_status=report.navigation_status,
            )
        )

    # 송신 시도 한 건을 기록한다.
    #
    # outcome으로 결과를 구분한다.
    #   delivered  송신했고 상대가 받았다
    #   lost       송신했으나 닿지 않았다 (전파 방해·잡음)
    #   no-slot    송신 기회를 얻지 못했다 (RAC 슬롯 고갈)
    #   rejected   요청 자체가 거부됐다 (크기 초과 등)
    #
    # attempts는 이 건에 들인 총 시도 횟수다. 재시도 한도 소진을 추적한다.
    def transfer(
        self,
        now: float,
        source_id: int,
        destination_id: int,
        vde_type: int,
        message: str,
        payload_bytes: int,
        burst_bytes: int,
        outcome: str = OUTCOME_DELIVERED,
        attempts: int = 1,
        reason: str = "",
    ) -> None:
        self.transfers.append(
            Transfer(
                time=now,
                source_id=source_id,
                destination_id=destination_id,
                vde_type=vde_type,
                message=message,
                payload_bytes=payload_bytes,
                burst_bytes=burst_bytes,
                outcome=outcome,
                attempts=attempts,
                reason=reason,
            )
        )

    def event(self, now: float, kind: str, label: str, detail: str = "") -> None:
        self.events.append(Event(time=now, kind=kind, label=label, detail=detail))

    def plan(self, waypoints: list[tuple[float, float]]) -> None:
        self.planned_route = list(waypoints)

    # ---------------------------------------------------------- 조회

    @property
    def duration(self) -> float:
        times = [s.time for s in self.samples] + [t.time for t in self.transfers]
        return max(times) if times else 0.0

    @property
    def tracked_ships(self) -> list[int]:
        return sorted({s.source_id for s in self.samples})

    # 한 선박의 관측 항적만 골라낸다.
    def track(self, source_id: int) -> list[Sample]:
        return [s for s in self.samples if s.source_id == source_id]

    # 실제로 전파가 나간 양. 송신 기회를 못 얻은 건은 빠진다.
    @property
    def transmitted_bytes(self) -> int:
        return sum(t.burst_bytes for t in self.transfers if t.transmitted)

    @property
    def delivered_bytes(self) -> int:
        return sum(t.burst_bytes for t in self.transfers if t.delivered)

    # outcome별 건수
    @property
    def outcome_counts(self) -> dict[str, int]:
        counts = {name: 0 for name in OUTCOME_LABELS}
        for item in self.transfers:
            counts[item.outcome] += 1
        return counts

    # 상대에게 닿지 않은 건수 전체 (원인 불문)
    @property
    def undelivered_transfers(self) -> int:
        return sum(1 for t in self.transfers if not t.delivered)

    # 송신은 했으나 유실된 건수 — 전파 방해의 흔적
    @property
    def lost_transfers(self) -> int:
        return sum(1 for t in self.transfers if t.outcome == OUTCOME_LOST)

    # 송신 기회를 얻지 못한 건수 — 자원 고갈의 흔적
    @property
    def no_slot_transfers(self) -> int:
        return sum(1 for t in self.transfers if t.outcome == OUTCOME_NO_SLOT)

    # 재시도가 한 번이라도 있었던 건수
    @property
    def retried_transfers(self) -> int:
        return sum(1 for t in self.transfers if t.attempts > 1)

    # 들인 총 송신 시도 횟수
    @property
    def total_attempts(self) -> int:
        return sum(t.attempts for t in self.transfers)

    # ROC 관점의 관측 공백 중 가장 긴 구간(초). 통신 두절의 크기를 나타낸다.
    def longest_gap(self, source_id: int) -> float:
        times = [s.time for s in self.track(source_id)]
        if len(times) < 2:
            return 0.0
        return max(b - a for a, b in zip(times, times[1:]))

    # 한 줄 요약
    def summary(self) -> dict[str, object]:
        ships = self.tracked_ships
        primary = ships[0] if ships else 0
        return {
            "name": self.name,
            "duration": self.duration,
            "samples": len(self.samples),
            "transfers": len(self.transfers),
            "undelivered": self.undelivered_transfers,
            "lost": self.lost_transfers,
            "no_slot": self.no_slot_transfers,
            "attempts": self.total_attempts,
            "retried": self.retried_transfers,
            "bytes": self.transmitted_bytes,
            "gap": self.longest_gap(primary) if ships else 0.0,
            "events": len(self.events),
        }


# 콘솔용 시간순 표.
#
# 사건이 일어난 시각과 지정 간격마다 한 줄씩 낸다. 사건 시각에 마침 위치보고가
# 없을 수 있으므로(보고는 2초마다인데 사건은 아무 때나 일어난다), 그 시각 이전의
# 가장 최근 관측으로 상태 칸을 채운다. 사건과 상태를 한 줄에서 같이 보기 위해서다.
def render_console(recorder: Recorder, interval: float = 60.0) -> str:
    ships = recorder.tracked_ships
    if not ships:
        return "(관측된 위치보고가 없다)"

    track = recorder.track(ships[0])
    events_by_time: dict[float, list[Event]] = {}
    for event in recorder.events:
        events_by_time.setdefault(event.time, []).append(event)

    # 표시할 시각: 사건이 있는 시각 + 간격마다 하나씩
    marked = set(events_by_time)
    periodic: set[float] = set()
    shown = None
    for sample in track:
        if shown is None or sample.time - shown >= interval:
            periodic.add(sample.time)
            shown = sample.time

    lines = [
        f"{'time':>7}  {'위도':>10} {'경도':>11} {'COG':>6} {'SOG':>6}  모드   사건",
        "-" * 78,
    ]

    for when in sorted(marked | periodic):
        # 이 시각 이전의 가장 최근 관측
        recent = [s for s in track if s.time <= when]
        label = "; ".join(e.label for e in events_by_time.get(when, []))

        if recent and recent[-1].has_position:
            sample = recent[-1]
            stale = "" if sample.time == when else " *"
            state = (
                f"{sample.latitude:10.5f} {sample.longitude:11.5f} "
                f"{sample.cog:6.1f} {sample.sog:6.1f}  "
                f"{pos.AUTONOMY_MODE_NAMES.get(sample.autonomy_mode, '?'):5s}"
            )
        else:
            stale = ""
            state = f"{'—':>10} {'—':>11} {'—':>6} {'—':>6}  {'—':5s}"

        lines.append(f"{when:7.0f}  {state}  {label}{stale}")

    lines.append("")
    lines.append("  * 그 시각에 위치보고가 없어 직전 관측값을 표시한 줄")
    return "\n".join(lines)
