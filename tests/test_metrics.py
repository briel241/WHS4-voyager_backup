"""정량지표 계측 검증 — 장비 없이 돌아간다.

실행: python3 tests/test_metrics.py

세는 규칙 자체가 지표의 신뢰성이다. 특히 아래 둘은 틀리기 쉬워 못 박아 둔다.

    - 위조를 거부한 것이 **인증 실패로 세이면 안 된다**. 그렇게 세면 방어가
      잘 될수록 인증 성공률이 떨어지는 거꾸로 된 수치가 나온다.
    - 거절 ACK를 돌려준 것을 **수용으로 세이면 안 된다**. 응답을 냈는지가
      아니라 배가 그 명령대로 움직였는지가 기준이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MASS import MassAgent, ShipState
from ROC import RocAgent
from common import metrics as met
from common.messages import application_ack as ack
from common.messages import route_instruction as route
from common.codec import decode

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


ROC_ID = 440_000_002
INTRUDER_ID = 440_000_003


# ------------------------------------------------------------ 송신국 판정

section("송신국 분류")

m = met.Metrics(expected_senders={ROC_ID})
check(m.classify(ROC_ID) == met.SENDER_AUTHORIZED, "등록된 국은 authorized")
check(m.classify(INTRUDER_ID) == met.SENDER_UNAUTHORIZED, "그 밖은 unauthorized")

blank = met.Metrics()
check(
    blank.classify(ROC_ID) == met.SENDER_UNKNOWN,
    "판정 근거가 없으면 unknown (어느 쪽으로도 세지 않는다)",
)
blank.record_admission(ROC_ID, accepted=True)
summary = blank.summary()
check(
    summary["unauthorized_acceptance"]["unclassified"] == 1,
    "unknown은 unclassified로 따로 센다",
)
check(
    summary["unauthorized_acceptance"]["value"] == 0,
    "unknown을 비인가 수용으로 세지 않는다",
)


# --------------------------------------------------- ① 비인가 패킷 수용

section("① 비인가 패킷 수용")

m = met.Metrics(expected_senders={ROC_ID})
m.record_admission(ROC_ID, accepted=True)
m.record_admission(INTRUDER_ID, accepted=False)
summary = m.summary()
unauth = summary["unauthorized_acceptance"]
check(unauth["offered"] == 1 and unauth["accepted"] == 0, "거절한 비인가는 수용이 아니다")
check(unauth["met"] is True, "수용 0건이면 통과")
check(unauth["authorized_accepted"] == 1, "정당한 명령 수용은 따로 센다")

m.record_admission(INTRUDER_ID, accepted=True)
check(
    m.summary()["unauthorized_acceptance"]["met"] is False,
    "한 건이라도 수용하면 미달 — 절대 조건이다",
)

# 표본이 없어도 "한 건도 없었다"는 참이다. 보류가 아니라 통과여야 한다.
check(
    met.Metrics(expected_senders={ROC_ID}).summary()["unauthorized_acceptance"]["met"]
    is True,
    "비인가 제시가 아예 없으면 통과",
)


# ------------------------------------------------------- ② 인증 성공률

section("② 인증 성공률")

m = met.Metrics(expected_senders={ROC_ID})
for _ in range(100):
    m.record_authentication(ok=True, genuine=True, reason="ok", seconds=0.001)
summary = m.summary()
check(summary["authentication"]["rate"] == 1.0, "정당한 자격증명 100건 통과 = 100%")
check(summary["authentication"]["met"] is True, "99% 이상이면 통과")

m.record_authentication(ok=False, genuine=True, reason="expired")
summary = m.summary()
check(
    # summary()는 소수점 6자리로 반올림해서 낸다. 그 자리까지만 본다.
    abs(summary["authentication"]["rate"] - 100 / 101) < 1e-6,
    "정당한 실패는 분모·분자에 모두 반영된다",
    f"{summary['authentication']['rate']:.4f}",
)
check(summary["authentication"]["failures"] == {"expired": 1}, "실패 사유를 남긴다")

# 위조를 거부한 것은 인증 실패가 아니다. 이걸 틀리면 방어가 잘 될수록
# 지표가 나빠진다.
before = m.summary()["authentication"]["rate"]
for _ in range(50):
    m.record_authentication(ok=False, genuine=False, reason="bad-signature")
after = m.summary()
check(after["authentication"]["rate"] == before, "위조 거부는 인증 성공률을 낮추지 않는다")
check(
    after["unauthorized_acceptance"]["by_credential"]["offered"] == 50,
    "위조 제시는 비인가 쪽에서 센다",
)
check(after["unauthorized_acceptance"]["value"] == 0, "거부했으므로 수용 0건")

m.record_authentication(ok=True, genuine=False, reason="")
check(
    m.summary()["unauthorized_acceptance"]["value"] == 1,
    "위조가 통과하면 비인가 수용으로 센다",
)

check(
    met.Metrics().summary()["authentication"]["met"] is None,
    "인증 표본이 없으면 판정 보류",
)


# --------------------------------------------------------- ③ 성능 저하

section("③ 성능 저하")

m = met.Metrics()
for _ in range(10):
    m.record_latency(0.100, met.PHASE_BASELINE)
for _ in range(10):
    m.record_latency(0.120, met.PHASE_SECURED)
lat = m.summary()["latency"]
check(abs(lat["overhead"] - 0.20) < 1e-9, "100ms → 120ms = +20%", str(lat["overhead"]))
check(lat["met"] is True, "30% 이하면 통과")

m = met.Metrics()
m.record_latency(0.100, met.PHASE_BASELINE)
m.record_latency(0.140, met.PHASE_SECURED)
check(m.summary()["latency"]["met"] is False, "+40%면 미달")

m = met.Metrics()
m.record_latency(0.100, met.PHASE_BASELINE)
check(m.summary()["latency"]["met"] is None, "한쪽 구간만 있으면 판정 보류")
check(m.summary()["passed"] is None, "보류가 있으면 종합 판정도 보류")

m = met.Metrics()
m.set_phase(met.PHASE_SECURED)
m.record_latency(0.5)
check(len(m.latency[met.PHASE_SECURED]) == 1, "set_phase 뒤에는 그 구간에 쌓인다")
try:
    m.set_phase("무엇")
    check(False, "모르는 구간 이름은 거부한다")
except ValueError:
    check(True, "모르는 구간 이름은 거부한다")


# ------------------------------------------------------- MassAgent 연동

section("MassAgent 연동 — 판정을 끄면 비인가 명령이 그대로 먹힌다")


def command(sender: RocAgent, mass: MassAgent) -> bytes:
    return sender.issue_route(mass.mmsi, [(35.10, 129.10), (35.15, 129.20)]).payload


# 판정 없음 — VDE-TER의 현재 상태
tracker = met.Metrics(expected_senders={ROC_ID})
mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0), metrics=tracker)
attacker = RocAgent(mmsi=INTRUDER_ID)
mass.receive(command(attacker, mass), source_id=INTRUDER_ID, now=1.0)

summary = tracker.summary()["unauthorized_acceptance"]
check(summary["offered"] == 1, "비인가 명령 1건 제시")
check(summary["accepted"] == 1, "판정이 없으면 본선이 그대로 따른다")
check(mass.has_route, "실제로 항로가 실렸다")

# 판정 있음 — 같은 공격을 다시
tracker = met.Metrics(expected_senders={ROC_ID})
mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0), metrics=tracker)
mass.authority_policy = lambda source_id, _message: source_id == ROC_ID
replies = mass.receive(command(attacker, mass), source_id=INTRUDER_ID, now=1.0)

summary = tracker.summary()["unauthorized_acceptance"]
check(summary["accepted"] == 0, "판정을 켜면 0건 수용")
check(not mass.has_route, "항로가 실리지 않았다")

answer = decode(replies[0].payload)
check(
    answer.status == ack.STATUS_ERRORS and answer.reason == ack.REASON_NOT_AUTHORIZED,
    "거절 사유는 not authorized",
)
# 거절 ACK도 응답이다. 응답을 냈다는 이유로 수용으로 세면 안 된다.
check(len(replies) == 1, "거절해도 ACK는 돌려준다 (그래도 수용 0건)")

# 정당한 국의 명령은 그대로 통과한다 — 방어가 정상 운항을 막으면 안 된다
controller = RocAgent(mmsi=ROC_ID)
mass.receive(command(controller, mass), source_id=ROC_ID, now=2.0)
summary = tracker.summary()["unauthorized_acceptance"]
check(summary["authorized_accepted"] == 1, "정당한 명령은 수용된다")
check(mass.has_route, "정당한 항로는 실린다")

# 취소도 "명령대로 움직였다"에 들어간다
cancel = controller.cancel_route(mass.mmsi, mass.route_linkage_id)
mass.receive(cancel.payload, source_id=ROC_ID, now=3.0)
check(
    tracker.summary()["unauthorized_acceptance"]["authorized_accepted"] == 2,
    "취소 수락도 수용으로 센다",
)

# metrics를 주지 않아도 행위자는 그대로 돈다
plain = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))
plain.receive(command(controller, plain), source_id=ROC_ID, now=1.0)
check(plain.has_route, "metrics 없이도 행위자는 정상 동작한다")


# ------------------------------------------------------------- 출력 형식

section("요약 출력")

m = met.Metrics(expected_senders={ROC_ID})
m.record_admission(ROC_ID, accepted=True)
m.record_authentication(ok=True, genuine=True, reason="ok", seconds=0.0002)
m.record_latency(0.1, met.PHASE_BASELINE)
m.record_latency(0.11, met.PHASE_SECURED)
text = met.to_text(m.summary())
check("비인가 패킷 수용" in text and "인증 성공률" in text and "성능 저하" in text,
      "세 지표가 모두 나온다")
check("보류" not in text, "표본이 다 있으면 보류가 없다")
check("200 µs" in text, "1 ms 미만은 마이크로초로 낸다")
check(m.summary()["passed"] is True, "셋 다 통과면 종합 통과")


# 지표 목표치는 문서에 적힌 값과 같아야 한다
section("목표치")
check(met.TARGET_UNAUTHORIZED_ACCEPTED == 0, "비인가 수용 목표 0회")
check(met.TARGET_AUTH_SUCCESS_RATE == 0.99, "인증 성공률 목표 99%")
check(met.TARGET_LATENCY_OVERHEAD == 0.30, "성능 저하 목표 30%")


# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
