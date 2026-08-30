"""정량지표 계측 — 프로젝트 목표 3종을 실제 수치로 낸다.

    비인가 패킷 수용    0회
    인증 성공률         99% 이상
    성능 저하           30% 이하

세 지표는 성격이 다르다.

    ① 비인가 패킷 수용은 **한 건이라도 나면 실패**하는 절대 조건이다.
    ② 인증 성공률은 **정당한 자격증명**이 얼마나 통과했는가다. 위조 시도는
       분모에 넣지 않는다 — 위조를 거부한 것을 인증 실패로 세면 방어가
       잘 될수록 지표가 나빠지는 거꾸로 된 수치가 된다. 위조는 ①이 센다.
    ③ 성능 저하는 같은 명령 시퀀스를 **보안 절차 없이/있게** 돌렸을 때의
       왕복 지연 차다. 구간을 `set_phase()`로 나눠 표시해 준다.

--------------------------------------------------------------------------
판정 근거와 계측 근거를 분리한다
--------------------------------------------------------------------------
`expected_senders`는 **이 실행에서 정당한 송신국이 누구인가**라는 사실이고,
`MassAgent.authority_policy`는 **행위자가 그것을 실제로 판정하는가**라는
구현이다. 둘을 분리해야 "정책을 끈 상태에서 비인가 패킷이 몇 건 수용됐나"를
셀 수 있다. VDE-TER에 송신자 인증이 없다는 것이 이 프로젝트의 연구 대상이고,
그 부재를 수치로 보이는 것이 ①의 목적이다.

`expected_senders`가 비어 있으면 판정 근거가 없으므로 어느 쪽으로도 세지
않고 `unknown`으로 남긴다.

--------------------------------------------------------------------------
쓰는 법
--------------------------------------------------------------------------
    metrics = Metrics(expected_senders={config.ROC_MMSI})
    mass = MassAgent(metrics=metrics)          # 수용/거절을 스스로 센다

    metrics.record_authentication(ok=True, genuine=True, reason="ok")
    metrics.record_latency(0.42)
    print(to_text(metrics.summary()))

표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# --------------------------------------------------------------------- 목표치

#: 비인가 패킷 수용 허용 건수. 한 건도 허용하지 않는다.
TARGET_UNAUTHORIZED_ACCEPTED = 0

#: 정당한 자격증명의 최소 인증 성공률.
TARGET_AUTH_SUCCESS_RATE = 0.99

#: 보안 절차로 인한 왕복 지연 증가 허용 비율.
TARGET_LATENCY_OVERHEAD = 0.30

#: 지연 표본을 나누는 구간 이름.
PHASE_BASELINE = "baseline"   # 보안 절차 없음 — 비교 기준
PHASE_SECURED = "secured"     # 보안 절차 적용
PHASES = (PHASE_BASELINE, PHASE_SECURED)

#: 송신국 판정 결과.
SENDER_AUTHORIZED = "authorized"
SENDER_UNAUTHORIZED = "unauthorized"
SENDER_UNKNOWN = "unknown"


def _stats(samples: list[float]) -> dict:
    """표본 목록을 개수·중앙값·p95로 줄인다. 비어 있으면 None으로 채운다."""
    if not samples:
        return {"count": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(samples)
    # p95는 표본이 적을 때 보간이 오해를 부르므로 가장 가까운 순위값을 쓴다.
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "count": len(ordered),
        "median": round(statistics.median(ordered), 6),
        "p95": round(ordered[index], 6),
        "max": round(ordered[-1], 6),
    }


@dataclass
class Metrics:
    """지표 3종의 누적 계수기. 스레드 보호는 호출자가 한다."""

    #: 이 실행에서 명령을 보낼 자격이 있는 송신국(MMSI). 비면 판정하지 않는다.
    expected_senders: set[int] = field(default_factory=set)

    #: 지금 기록 중인 지연 구간.
    phase: str = PHASE_BASELINE

    def __post_init__(self) -> None:
        self.expected_senders = set(self.expected_senders)
        self.reset()

    def reset(self) -> None:
        # ① 비인가 패킷 수용
        self.authorized_offered = 0
        self.authorized_accepted = 0
        self.unauthorized_offered = 0
        self.unauthorized_accepted = 0
        self.unknown_offered = 0

        # ② 인증 성공률 — 정당한 자격증명만 분모에 넣는다
        self.auth_attempts = 0
        self.auth_success = 0
        self.auth_failures: dict[str, int] = {}
        # 위조 자격증명은 따로 센다. 통과하면 ①의 실패이기도 하다.
        self.forged_offered = 0
        self.forged_accepted = 0
        #: 자격증명 검증 1건에 걸린 시간(초).
        self.verify_seconds: list[float] = []

        # ③ 성능 — 구간별 명령 왕복 지연(초)
        self.latency: dict[str, list[float]] = {name: [] for name in PHASES}

    # ------------------------------------------------------------ 구간 전환

    def set_phase(self, phase: str) -> str:
        if phase not in PHASES:
            raise ValueError(f"알 수 없는 구간: {phase!r} (허용: {PHASES})")
        self.phase = phase
        return self.phase

    # --------------------------------------------------- ① 비인가 패킷 수용

    def classify(self, source_id: int) -> str:
        """송신국이 정당한가. 판정 근거가 없으면 unknown."""
        if not self.expected_senders:
            return SENDER_UNKNOWN
        return (
            SENDER_AUTHORIZED
            if source_id in self.expected_senders
            else SENDER_UNAUTHORIZED
        )

    def record_admission(self, source_id: int, accepted: bool) -> str:
        """명령 한 건의 수용/거절을 센다. 판정 결과를 돌려준다.

        `accepted`는 **행위자가 그 명령대로 상태를 바꿨는가**다. 응용 ACK를
        돌려줬는지가 아니다 — 거절 ACK도 응답이므로 그것으로 세면 안 된다.
        """
        verdict = self.classify(source_id)
        if verdict == SENDER_AUTHORIZED:
            self.authorized_offered += 1
            self.authorized_accepted += int(bool(accepted))
        elif verdict == SENDER_UNAUTHORIZED:
            self.unauthorized_offered += 1
            self.unauthorized_accepted += int(bool(accepted))
        else:
            self.unknown_offered += 1
        return verdict

    def reset_unauthorized(self) -> dict:
        """비인가 계수기만 0으로 되돌리고 직전 값을 돌려준다.

        방어를 켜기 **전** 피해 규모를 먼저 재고, 켠 뒤부터 지표를 세기
        위한 것이다. 방어 전 수용 건수를 지표에 넣으면 "방어가 작동하는가"가
        아니라 "실험을 했는가"를 재게 된다.
        """
        previous = {
            "offered": self.unauthorized_offered,
            "accepted": self.unauthorized_accepted,
        }
        self.unauthorized_offered = 0
        self.unauthorized_accepted = 0
        return previous

    # ------------------------------------------------------- ② 인증 성공률

    def record_authentication(
        self,
        *,
        ok: bool,
        genuine: bool = True,
        reason: str = "",
        seconds: float | None = None,
    ) -> None:
        """자격증명 검증 한 건을 센다.

        genuine=True   정당한 자격증명. 성공률의 분모에 들어간다.
        genuine=False  위조·훼손된 자격증명. 통과하면 비인가 수용으로 센다.
        """
        if seconds is not None:
            self.verify_seconds.append(float(seconds))

        if genuine:
            self.auth_attempts += 1
            if ok:
                self.auth_success += 1
            else:
                key = reason or "unknown"
                self.auth_failures[key] = self.auth_failures.get(key, 0) + 1
            return

        self.forged_offered += 1
        if ok:
            self.forged_accepted += 1

    # ----------------------------------------------------------- ③ 성능 저하

    def record_latency(self, seconds: float, phase: str | None = None) -> None:
        """명령 발행부터 응용 ACK 도착까지의 왕복 지연 한 건."""
        seconds = float(seconds)
        if seconds < 0:
            return
        name = phase or self.phase
        if name not in self.latency:
            raise ValueError(f"알 수 없는 구간: {name!r}")
        self.latency[name].append(seconds)

    # ---------------------------------------------------------------- 요약

    def summary(self) -> dict:
        """세 지표를 판정과 함께 낸다. 표본이 없는 지표의 met는 None(보류)이다."""
        # ① — 위조 자격증명이 통과한 것도 비인가 수용이다.
        accepted = self.unauthorized_accepted + self.forged_accepted
        offered = self.unauthorized_offered + self.forged_offered
        unauthorized = {
            "offered": offered,
            "accepted": accepted,
            "rejected": offered - accepted,
            "by_source": {
                "offered": self.unauthorized_offered,
                "accepted": self.unauthorized_accepted,
            },
            "by_credential": {
                "offered": self.forged_offered,
                "accepted": self.forged_accepted,
            },
            "authorized_offered": self.authorized_offered,
            "authorized_accepted": self.authorized_accepted,
            "unclassified": self.unknown_offered,
            "value": accepted,
            "target": TARGET_UNAUTHORIZED_ACCEPTED,
            # 표본이 없어도 "한 건도 없었다"는 참이다. 보류가 아니라 통과다.
            "met": accepted <= TARGET_UNAUTHORIZED_ACCEPTED,
        }

        # ②
        rate = self.auth_success / self.auth_attempts if self.auth_attempts else None
        authentication = {
            "attempts": self.auth_attempts,
            "success": self.auth_success,
            "failures": dict(self.auth_failures),
            "rate": None if rate is None else round(rate, 6),
            "value": None if rate is None else round(rate, 6),
            "target": TARGET_AUTH_SUCCESS_RATE,
            "met": None if rate is None else rate >= TARGET_AUTH_SUCCESS_RATE,
            "verify_seconds": _stats(self.verify_seconds),
        }

        # ③
        base = _stats(self.latency[PHASE_BASELINE])
        secured = _stats(self.latency[PHASE_SECURED])
        overhead = None
        if base["median"] and secured["median"] is not None:
            overhead = secured["median"] / base["median"] - 1.0
        latency = {
            PHASE_BASELINE: base,
            PHASE_SECURED: secured,
            "overhead": None if overhead is None else round(overhead, 6),
            "value": None if overhead is None else round(overhead, 6),
            "target": TARGET_LATENCY_OVERHEAD,
            "met": None if overhead is None else overhead <= TARGET_LATENCY_OVERHEAD,
        }

        indicators = {
            "unauthorized_acceptance": unauthorized,
            "authentication": authentication,
            "latency": latency,
        }
        decided = [item["met"] for item in indicators.values() if item["met"] is not None]
        return {
            "phase": self.phase,
            "expected_senders": sorted(self.expected_senders),
            **indicators,
            # 판정이 보류된 지표가 하나라도 있으면 전체 판정도 보류다.
            "passed": (
                all(decided) if len(decided) == len(indicators) else None
            ),
        }


# ------------------------------------------------------------------ 사람이 읽기


def _verdict(met: bool | None) -> str:
    return {True: "통과", False: "미달", None: "보류"}[met]


def to_text(summary: dict) -> str:
    """summary()를 콘솔용 표로 만든다."""
    lines = ["정량지표", ""]
    lines.append(f"  {'지표':<22} {'측정값':>12}  {'목표':>10}  판정")
    lines.append("  " + "-" * 58)

    unauth = summary["unauthorized_acceptance"]
    lines.append(
        f"  {'비인가 패킷 수용':<20} {unauth['value']:>11}회  "
        f"{'0회':>10}  {_verdict(unauth['met'])}"
    )

    auth = summary["authentication"]
    value = "표본 없음" if auth["rate"] is None else f"{auth['rate'] * 100:.2f}%"
    lines.append(
        f"  {'인증 성공률':<22} {value:>12}  {'99% 이상':>9}  {_verdict(auth['met'])}"
    )

    lat = summary["latency"]
    value = "표본 없음" if lat["overhead"] is None else f"{lat['overhead'] * 100:+.1f}%"
    lines.append(
        f"  {'성능 저하':<24} {value:>12}  {'30% 이하':>9}  {_verdict(lat['met'])}"
    )

    lines.append("")
    lines.append(
        f"  비인가: 제시 {unauth['offered']}건 중 수용 {unauth['accepted']}건 "
        f"(송신국 {unauth['by_source']['offered']}건 · "
        f"위조 자격증명 {unauth['by_credential']['offered']}건) · "
        f"정당한 명령 {unauth['authorized_accepted']}/{unauth['authorized_offered']}건 수용"
    )
    failures = ", ".join(f"{k} {v}건" for k, v in sorted(auth["failures"].items()))
    lines.append(
        f"  인증: 정당한 자격증명 {auth['success']}/{auth['attempts']}건 통과"
        + (f" · 실패 {failures}" if failures else "")
    )
    if auth["verify_seconds"]["median"] is not None:
        lines.append(
            f"  검증 비용: 1건당 중앙값 {_ms(auth['verify_seconds']['median'])} "
            f"({auth['verify_seconds']['count']}건)"
        )
    base, secured = lat[PHASE_BASELINE], lat[PHASE_SECURED]
    if base["count"] or secured["count"]:
        lines.append(
            f"  지연: 기준 중앙값 {_ms(base['median'])} ({base['count']}건) → "
            f"보안 적용 {_ms(secured['median'])} ({secured['count']}건)"
        )

    passed = summary["passed"]
    lines.append("")
    lines.append(f"  종합: {_verdict(passed)}")
    return "\n".join(lines)


def _ms(value: float | None) -> str:
    """초를 사람이 읽는 시간으로. 1 ms 미만은 마이크로초로 낸다.

    지금 재는 값들은 대부분 1 ms 아래라, ms로 반올림하면 전부 "0.0 ms"가 되어
    비교가 불가능해진다.
    """
    if value is None:
        return "—"
    if value < 0.001:
        return f"{value * 1_000_000:.0f} µs"
    return f"{value * 1000:.2f} ms"
