#!/usr/bin/env python3
"""정량지표 3종을 실제로 측정한다.

    python tools/measure_metrics.py                        # 직결 경로
    python tools/measure_metrics.py --transport software   # 실 VDE-TER 소프트웨어 무선
    python tools/measure_metrics.py --cycles 30 --json

재는 것
    비인가 패킷 수용   ROC가 아닌 국이 넣은 명령을 본선이 몇 건 따랐는가
    인증 성공률        정당한 인증서가 몇 건 통과했는가 (위조본은 분모 밖)
    성능 저하          같은 명령 시퀀스의 왕복 지연이 보안 판정을 켜고 얼마나 늘었는가

--------------------------------------------------------------------------
측정 절차
--------------------------------------------------------------------------
① 기준 구간   송신국 판정 끔 — 명령 N회 발행하고 왕복 지연을 잰다.
               VDE-TER의 현재 상태(송신자 인증 없음)가 이쪽이다.
② 비인가 주입  판정이 꺼진 채로 제3국이 명령을 넣는다. 본선이 그대로 따르는
               것이 정상이며, 그 건수가 방어 전의 피해 규모다.
③ 보안 구간   송신국 판정 켬 — 같은 명령을 N회 더 발행해 지연을 다시 잰다.
④ 비인가 재주입 같은 제3국이 다시 넣는다. 이번에는 0건이어야 한다.
⑤ 인증        정당한 인증서 K건, 위조 인증서 K건을 발급해 검증 결과를 센다.

지표의 최종값은 ③·④ 이후 상태다. ②에서 센 수용 건수는 방어를 켜기 전의
값이므로 지표에 넣지 않고 `before_defence`로 따로 보고한다.

--------------------------------------------------------------------------
이 수치를 읽을 때 주의할 것
--------------------------------------------------------------------------
현재 구현에서 명령 경로의 보안 절차는 **송신국 판정 하나**다. 메시지별 서명
검증은 아직 없다(SC-VDE 형식은 서명 없는 Segment Type 0이다). 그래서 ③의
지연 증가는 작게 나오며, 이 값은 **하한**으로 읽어야 한다. 실제 암호 연산
비용은 인증서 검증 시간(`verify_seconds`)이 따로 보여 준다.

잡음 바닥
    두 구간을 똑같이 두고(판정을 끈 채 이름표만 바꿔) 20회씩 재면 ±1.5%가
    나온다. 이것이 이 측정의 분해능이며, 그보다 작은 차이는 읽지 않는다.

어느 경로로 잴 것인가
    `--transport direct`  왕복이 0.5 ms 안에 끝나므로 **응용 처리 비용**이
                          그대로 드러난다. 성능 저하 지표는 이쪽으로 읽는다.
    `--transport software`(구 vdeter)  실 VDE-TER 무선을 탄다. 왕복이 8초쯤 걸리는데 그
                          대부분이 프레임 배정 대기라, 보안 비용은 배정 지터에
                          묻힌다. 이쪽 수치는 **무선을 타도 지표가 성립하는지**
                          확인하는 용도이고, 비율 자체는 지터를 본 것에 가깝다.

구동기는 콘솔 GUI와 같은 `apps/console_server.Simulation`을 쓴다. 화면에서
보는 것과 같은 코드 경로를 재려는 것이다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from common.textout import ensure_utf8_output

ensure_utf8_output()

from ROC.roc_agent import RocAgent  # noqa: E402
from apps.console_server import Simulation  # noqa: E402
from common import config  # noqa: E402
from common import metrics as metrics_mod  # noqa: E402
from common.messages import route_instruction as route  # noqa: E402
from common.security import certificate as cert  # noqa: E402

RESULTS_DIR = os.path.join(ROOT, "results")

#: 명령마다 조금씩 옮겨 가는 목표점. 같은 좌표를 반복하면 항로가 성립하지 않는다.
BASE_LATITUDE = 35.060
BASE_LONGITUDE = 129.120

#: 한 명령의 ACK를 기다리는 한도(초). 넘으면 미응답으로 세고 넘어간다.
#
# 직결은 1 ms 안에 끝나지만 소프트웨어 무선은 프레임 배정을 기다리느라 한 건에
# 8초쯤 걸린다. 넉넉히 잡아 둔다 — 시한을 넘긴 건은 표본에서 빠지므로 짧게
# 잡으면 느린 표본만 골라 버리는 편향이 생긴다.
ANSWER_TIMEOUT_SEC = 60.0

#: 지표에서 빼는 예열 명령 수.
WARMUP_CYCLES = 5

#: 대기 중에는 쉬지 않고 시뮬레이션을 돌린다.
#
# `time.sleep()`을 끼우면 재는 왕복 지연이 잠든 시간에 지배당해 실제 처리
# 비용 차이가 묻힌다. 이 도구는 몇 초면 끝나므로 그냥 계속 돌린다.
STEP_SLEEP_SEC = 0.0


def _waypoints(index: int) -> list[list[float]]:
    """명령 index번째의 웨이포인트 2개. 서로 다른 지점이어야 수락된다."""
    offset = 0.01 * (index + 1)
    return [
        [BASE_LATITUDE + offset, BASE_LONGITUDE + offset],
        [BASE_LATITUDE + offset + 0.02, BASE_LONGITUDE + offset + 0.03],
    ]


def _issue_and_wait(sim: Simulation, index: int) -> bool:
    """노선 명령 하나를 발행하고 응용 ACK가 돌아올 때까지 시뮬레이션을 돌린다."""
    result = sim.submit_command({"kind": "route", "waypoints": _waypoints(index)})
    linkage_id = result["linkage_id"]

    deadline = time.monotonic() + ANSWER_TIMEOUT_SEC
    while time.monotonic() < deadline:
        sim.step()
        command = sim.roc.commands.get(linkage_id)
        if command is not None and command.answered:
            return True
        if STEP_SLEEP_SEC:
            time.sleep(STEP_SLEEP_SEC)
    return False


def _inject_unauthorized(sim: Simulation, intruder: RocAgent, count: int) -> int:
    """제3국이 명령을 넣는다. 본선이 실제로 따른 건수를 돌려준다.

    주입 지점은 구동기가 정한 유일한 통로(`deliver_to_mass`)다. 여기서만
    source_id가 바뀌고, 나머지 경로는 정상 명령과 완전히 같다 — VDE-TER에
    송신자 인증이 없다는 사실이 그대로 재현된다.
    """
    before = sim.metrics.unauthorized_accepted
    for index in range(count):
        with sim.lock:
            outbound = intruder.issue_route(
                sim.mass.mmsi,
                _waypoints(100 + index),
                now=sim.now,
                route_type=route.ROUTE_TYPE_MANDATORY,
            )
            sim.deliver_to_mass(
                outbound,
                intruder.mmsi,
                sim.now,
                {"kind": "route", "text": f"비인가 명령 #{index + 1}",
                 "linkage_id": None},
            )
        # 도착·평가까지 몇 스텝이 걸린다(무선 경로는 더 걸린다).
        deadline = time.monotonic() + ANSWER_TIMEOUT_SEC
        seen = sim.metrics.unauthorized_offered
        while time.monotonic() < deadline:
            sim.step()
            if sim.metrics.unauthorized_offered > seen:
                break
            if STEP_SLEEP_SEC:
                time.sleep(STEP_SLEEP_SEC)
    return sim.metrics.unauthorized_accepted - before


def _issue_certificates(sim: Simulation, count: int) -> None:
    """정당한 인증서와 위조 인증서를 같은 수만큼 발급해 검증 결과를 쌓는다."""
    for _ in range(count):
        sim.submit_command({"kind": "certificate", "forged": False})
        sim.step()
    for _ in range(count):
        sim.submit_command({"kind": "certificate", "forged": True})
        sim.step()


def _set_policy(sim: Simulation, on: bool) -> None:
    """본선의 송신국 판정을 켜고 끈다."""
    if on:
        roc_mmsi = sim.roc.mmsi
        sim.mass.authority_policy = lambda source_id, _message: source_id == roc_mmsi
        sim.authority = "strict"
    else:
        sim.mass.authority_policy = None
        sim.authority = "open"


def measure(transport: str, cycles: int, intruders: int, certificates: int) -> dict:
    # 판정은 처음에 꺼 둔다. 켜고 끄는 것은 아래에서 번갈아 한다.
    sim = Simulation(transport_kind=transport, authority="open")
    intruder = RocAgent(mmsi=config.INTRUDER_MMSI)
    notes: list[str] = []
    if sim.transport_note:
        notes.append(f"무선 전송을 쓸 수 없어 direct로 내려감: {sim.transport_note}")
    if not cert.is_standard_backend():
        notes.append(
            "대체 HMAC 백엔드로 측정했다 — 검증 비용은 실제 ECDSA 값이 아니다 "
            "(`pip install cryptography` 후 다시 재야 한다)")

    print(f"전송 경로: {sim.transport.name} — {sim.transport.detail}")
    print()

    # ① 예열 ---------------------------------------------------------------
    # 첫 몇 건은 리스트·캐시가 자리를 잡기 전이라 유독 느리다. 지표에 넣지 않는다.
    print(f"① 예열 — 명령 {WARMUP_CYCLES}회 (지표에서 제외)")
    for index in range(WARMUP_CYCLES):
        _issue_and_wait(sim, index)
    sim.metrics.latency[metrics_mod.PHASE_BASELINE].clear()
    sim.metrics.latency[metrics_mod.PHASE_SECURED].clear()
    sim.metrics.reset_unauthorized()

    # ② 교차 측정 ------------------------------------------------------------
    #
    # 두 구간을 몰아서 재면 안 된다. 실행이 길어질수록 로그·항적·명령 이력이
    # 쌓여 스냅샷 직렬화가 무거워지므로, 나중에 잰 구간이 무조건 느리게
    # 나온다 — 보안 비용이 아니라 측정 순서가 만든 차이다. 한 회마다 판정을
    # 껐다 켜며 번갈아 재서 그 흐름을 두 구간에 똑같이 나눈다.
    print(f"② 교차 측정 — 판정 끔/켬을 번갈아 {cycles}회씩")
    unanswered = 0
    for round_index in range(cycles):
        # 회마다 순서도 뒤집는다. "먼저 잰 쪽이 유리한" 편향까지 없앤다.
        order = [False, True] if round_index % 2 == 0 else [True, False]
        for policy_on in order:
            _set_policy(sim, policy_on)
            sim.metrics.set_phase(
                metrics_mod.PHASE_SECURED if policy_on else metrics_mod.PHASE_BASELINE)
            if not _issue_and_wait(sim, round_index):
                unanswered += 1
    if unanswered:
        notes.append(f"{unanswered}건이 시한 안에 응답받지 못했다")

    # ③ 방어 전 비인가 주입 --------------------------------------------------
    _set_policy(sim, False)
    print(f"③ 비인가 주입 (판정 끔) — {intruder.mmsi}에서 {intruders}회")
    _inject_unauthorized(sim, intruder, intruders)

    # 방어 전 수치는 지표가 아니라 피해 규모다. 지표는 방어를 켠 뒤부터 센다.
    before_defence = sim.metrics.reset_unauthorized()

    # ④ 방어 후 비인가 주입 --------------------------------------------------
    _set_policy(sim, True)
    print(f"④ 비인가 재주입 (판정 켬) — {intruders}회")
    _inject_unauthorized(sim, intruder, intruders)

    # ⑤ 인증 ---------------------------------------------------------------
    print(f"⑤ 인증서 — 정상 {certificates}건 · 위조 {certificates}건")
    _issue_certificates(sim, certificates)

    summary = sim.metrics.summary()
    summary["before_defence"] = before_defence
    summary["transport"] = {
        "name": sim.transport.name,
        "detail": sim.transport.detail,
        **sim.transport.stats(),
    }
    summary["run"] = {
        "cycles_per_phase": cycles,
        "warmup_cycles": WARMUP_CYCLES,
        "intruder_commands_per_phase": intruders,
        "certificates_per_kind": certificates,
        "intruder_mmsi": intruder.mmsi,
        "certificate_backend": (
            "ECDSA P-256" if cert.is_standard_backend() else "대체 HMAC (시험용)"),
        "elapsed_seconds": round(sim.now, 1),
    }
    summary["notes"] = notes
    sim.transport.close()
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transport", choices=("direct", "software", "vdeter"),
                    default="direct")
    parser.add_argument("--cycles", type=int, default=20,
                        help="구간별 명령 횟수 (기본 20)")
    parser.add_argument("--intruders", type=int, default=10,
                        help="구간별 비인가 명령 횟수 (기본 10)")
    parser.add_argument("--certificates", type=int, default=10,
                        help="정상·위조 인증서를 각각 몇 건 발급할지 (기본 10)")
    parser.add_argument("--json", action="store_true", help="요약을 JSON으로 출력")
    parser.add_argument("--no-save", action="store_true",
                        help="results/에 저장하지 않는다")
    arguments = parser.parse_args(argv)

    summary = measure(arguments.transport, arguments.cycles,
                      arguments.intruders, arguments.certificates)

    print()
    if arguments.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(metrics_mod.to_text(summary))
        before = summary["before_defence"]
        print()
        print(f"  방어 전(판정 끔): 비인가 {before['offered']}건 중 "
              f"{before['accepted']}건 수용 — 이것이 막으려는 피해다")
        for note in summary["notes"]:
            print(f"  ⚠ {note}")

    if not arguments.no_save:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = os.path.join(RESULTS_DIR, f"metrics_{arguments.transport}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if not arguments.json:
            print(f"\n저장: {os.path.relpath(path, ROOT)}")

    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
