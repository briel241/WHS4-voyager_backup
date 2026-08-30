"""시나리오 실행 타임라인 로거 (작업 C).

기존 ``recorder.py`` / ``report.py`` 는 ROC 관찰(항적·시계열) 중심이라
"시나리오 한 건이 규격대로 흘렀는가"를 판정하지 못한다. 이 모듈은 그 위에
얹는 **실행 로그 계층**이며 기존 파일을 건드리지 않는다. 판정은
``scenario_analysis.analyze`` 가 맡는다.

산출물은 전부 JSON 직렬화 가능하다(표준 ``json`` 만 사용).

--------------------------------------------------------------------------
detail 에 넣는 표준 키
--------------------------------------------------------------------------
``LogEntry.detail`` 은 자유 dict 지만, 분석기가 알아보는 키는 아래로 고정한다.
SC-VDE-001 시나리오가 요구하는 로그 필드에 맞춘 것이다.

    source_id            int    Source ID
    destination_id       int    Destination ID
    corr                 str    상관 키. 같은 송신-응답 쌍을 묶어 지연을 잰다.
    fragment             int    인증서 fragment 번호(1부터)
    fragment_total       int    fragment 총 개수
    ok                   bool   kind="verify" 일 때 서명 검증 통과 여부
    reason               str    검증 실패 사유("bad-signature" 등)
    status               int    ACK/상태 코드 (S-421: 4/7/8/9)
    retransmission       bool   이 송신이 재전송이면 True
    payload_sha256       str    재조립 payload 의 SHA-256 hex
    fingerprint          str    인증서 지문 hex
    stored               bool   신뢰 저장소에 실제로 반영됐으면 True
    waypoints            int    항로 웨이포인트 개수
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: 허용 방향
DIRECTIONS = ("ROC->MASS", "MASS->ROC")

#: 허용 종류
KINDS = ("send", "recv", "verify", "ack", "note")


@dataclass
class LogEntry:
    time: float                 # epoch seconds
    direction: str              # "ROC->MASS" | "MASS->ROC"
    kind: str                   # "send" | "recv" | "verify" | "ack" | "note"
    message: str                # 사람이 읽는 요약 (예: "X509 cert frag 3/6")
    detail: dict = field(default_factory=dict)   # 임의 부가 필드

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"알 수 없는 kind: {self.kind!r} (허용: {KINDS})")
        if self.direction not in DIRECTIONS:
            raise ValueError(
                f"알 수 없는 direction: {self.direction!r} (허용: {DIRECTIONS})")
        self.time = float(self.time)
        if not isinstance(self.detail, dict):
            raise ValueError("detail 은 dict 여야 한다")

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "direction": self.direction,
            "kind": self.kind,
            "message": self.message,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LogEntry":
        return cls(
            time=data["time"],
            direction=data["direction"],
            kind=data["kind"],
            message=data.get("message", ""),
            detail=dict(data.get("detail", {})),
        )


@dataclass
class ScenarioRun:
    scenario_id: str            # 예: "SC-VDE-001"
    started: float
    entries: list = field(default_factory=list)
    finished: float | None = None

    # -- 기록 ---------------------------------------------------------------

    def log(self, entry: LogEntry) -> None:
        """항목을 추가한다. ``finish`` 이후에는 받지 않는다."""
        if self.finished is not None:
            raise RuntimeError("이미 종료된 실행에는 기록할 수 없다")
        if not isinstance(entry, LogEntry):
            raise TypeError("LogEntry 가 아니다")
        self.entries.append(entry)

    def record(self, time: float, direction: str, kind: str, message: str,
               **detail) -> LogEntry:
        """``log(LogEntry(...))`` 축약형. detail 은 키워드로 받는다."""
        entry = LogEntry(time=time, direction=direction, kind=kind,
                         message=message, detail=detail)
        self.log(entry)
        return entry

    def finish(self, now: float) -> None:
        """실행을 종료한다. 종료 시각은 마지막 항목보다 이를 수 없다."""
        if self.finished is not None:
            raise RuntimeError("이미 종료됐다")
        last = max((e.time for e in self.entries), default=self.started)
        self.finished = float(max(float(now), last))

    # -- 조회 ---------------------------------------------------------------

    @property
    def duration(self) -> float | None:
        """시작→종료 경과 시간. 아직 종료 전이면 None."""
        if self.finished is None:
            return None
        return self.finished - self.started

    def by_kind(self, kind: str) -> list:
        return [e for e in self.entries if e.kind == kind]

    def ordered(self) -> list:
        """시각 오름차순(같으면 기록 순서 유지)으로 정렬한 항목."""
        return sorted(self.entries, key=lambda e: e.time)

    # -- 직렬화 -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "started": self.started,
            "finished": self.finished,
            "duration": self.duration,
            "entries": [e.to_dict() for e in self.entries],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "ScenarioRun":
        run = cls(scenario_id=data["scenario_id"], started=data["started"])
        run.entries = [LogEntry.from_dict(d) for d in data.get("entries", [])]
        run.finished = data.get("finished")
        return run


# 이 파일은 라이브러리다. 직접 실행하면 자체 점검용 예시 로그를 보여준다.
if __name__ == "__main__":
    demo = ScenarioRun(scenario_id="DEMO", started=0.0)
    demo.record(0.0, "ROC->MASS", "send", "예시 송신", corr="d1")
    demo.record(0.5, "MASS->ROC", "ack", "예시 ACK", corr="d1", status=4)
    demo.finish(0.6)
    print("이 파일은 라이브러리입니다(직접 실행 대상 아님). 자체 점검용 예시 로그(JSON):\n")
    print(demo.to_json())
    print("\n실제 시나리오 로그·판정을 보려면:  python3 apps/scenario_runner.py")
