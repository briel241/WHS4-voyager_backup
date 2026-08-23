"""MASS/ROC 정상 동작 시뮬레이터 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 apps/mass_roc_sim.py

두 행위자를 실제 VDE-TER 링크 계층(VDETER_Link/)으로 연결해 돌린다.
GNU Radio와 USRP는 쓰지 않고, Temporary Burst를 bit stream으로 만들었다가
다시 꺼내는 지점까지만 통과시킨다. 즉 RF만 없고 나머지 사슬은 실물이다.

    MassAgent --payload--> [Type 92/93 -> Packet -> Burst -> bits]
                                   |
                           [bits -> Burst -> Packet -> payload]
                                   +--> RocAgent

이 출력이 구상도의 "어떻게 변하는지 확인하는 도구"의 초기 형태이며,
후속 공격 실험에서 정상 대조군으로 쓴다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "VDETER_Link" / "no_error_correction"))

from common import config
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.outbound import Outbound
from common.observe import (
    EVENT_COMMAND,
    EVENT_LINK,
    EVENT_ROUTE,
    OUTCOME_DELIVERED,
    OUTCOME_LOST,
    Recorder,
    render_console,
    write_report,
)

from vdeter_link.burst import temporary_builder
from vdeter_link.burst.temporary_extractor import Extractor
from vdeter_link.messages.registry import MESSAGE_CODECS
from vdeter_link.messages.type_92 import Type92Message
from vdeter_link.messages.type_93 import Type93Message
from vdeter_link.packet import builder as packet_builder
from vdeter_link.packet import parser as packet_parser
from vdeter_link.utils import binary_rw

# 시뮬레이션 설정
#
# 항로 간격은 선회 반경보다 충분히 커야 한다. 선회 반경은 속력/각속도이며
# 12 kn(0.2 해리/분)에 60도/분이면 약 0.19해리다. waypoint를 그보다 가깝게
# 두면 배가 선회를 마치기 전에 지나쳐 버린다.
TICK_SECONDS = 1.0
TOTAL_SECONDS = 600
COMMAND_AT = 20.0
BLACKOUT_FROM = 430.0  # 이 시각부터 MASS 송신이 링크에서 사라진다
BLACKOUT_UNTIL = 490.0

# 소형 자율운항 시험선을 상정한다. 대형선의 기본값(20도/분)보다 민첩하다.
DEMO_TURN_RATE = 60.0


# Outbound를 VDE 링크 메시지로 감싼다.
# ack_required이 Type 92/93 선택을 결정한다(응용 계층의 결정).
def to_link_message(outbound: Outbound, source_id: int):
    if outbound.ack_required:
        return Type92Message(
            source_id=source_id,
            session_id=0,
            destination_id=outbound.destination_id,
            retransmission_id=0,
            payload=outbound.payload,
        )
    return Type93Message(
        source_id=source_id,
        session_id=0,
        destination_id=outbound.destination_id,
        payload=outbound.payload,
    )


# 링크 메시지를 Temporary Burst bit stream으로 만들었다가 다시 꺼낸다.
# 실제 PHY 대신 F9A8 검출 직후 지점을 흉내낸다.
def through_link(link_message) -> list:
    encoded = MESSAGE_CODECS[link_message.type_id].encode(link_message)
    burst = temporary_builder.build(packet_builder.build([encoded]))

    preamble_bits = len(temporary_builder.PREAMBLE) * binary_rw.BITS_PER_BYTE
    stream = binary_rw.bytes_to_bits(burst)[preamble_bits:]

    packets = Extractor().consume(stream, [0])
    received = []
    for packet in packets:
        received.extend(packet_parser.parse(packet))
    return received, len(burst)


def main() -> int:
    mass = MassAgent(
        state=ShipState(
            latitude=35.1000,
            longitude=129.1000,
            heading=90.0,
            sog=12.0,
            navigation_status=0,
        ),
        max_turn_rate=DEMO_TURN_RATE,
    )
    roc = RocAgent()

    # ROC가 지시할 항로 (위도, 경도).
    # 각 구간 약 0.4해리로, 선회 반경(약 0.19해리)보다 충분히 크게 잡는다.
    planned_route = [
        (35.1050, 129.1050),
        (35.1120, 129.1080),
        (35.1200, 129.1080),
    ]

    log = Recorder(name="정상 동작", note="공격 없음 · 대조군")
    log.plan(planned_route)

    # 링크로 한 건 보내고, 받은 쪽에 전달하고, 기록까지 남긴다.
    #
    # outcome이 결과를 구분한다. 차단 구간은 전파는 나갔으나 닿지 않은 것이므로
    # OUTCOME_LOST다. 송신 기회 자체를 못 얻는 경우(OUTCOME_NO_SLOT)는 링크 계층의
    # 자원할당이 구현된 뒤에야 나타난다.
    def send(outbound, source_id, now, outcome=OUTCOME_DELIVERED, reason=""):
        link_message = to_link_message(outbound, source_id)
        received, burst_size = through_link(link_message)
        log.transfer(
            now=now,
            source_id=source_id,
            destination_id=outbound.destination_id,
            vde_type=link_message.type_id,
            message=outbound.label,
            payload_bytes=len(outbound.payload),
            burst_bytes=burst_size,
            outcome=outcome,
            reason=reason,
        )
        delivered = outcome == OUTCOME_DELIVERED
        return (received if delivered else []), burst_size

    print("=" * 72)
    print("MASS/ROC 정상 동작 시뮬레이션")
    print(f"  MASS MMSI {mass.mmsi} / ROC MMSI {roc.mmsi}")
    print(f"  위치보고 주기 {mass.report_interval:g}s, 통신 두절 판정 {roc.report_timeout:g}s")
    print(f"  {BLACKOUT_FROM:.0f}~{BLACKOUT_UNTIL:.0f}s 구간은 MASS 송신을 차단한다")
    print("=" * 72)

    command_sent = False
    blackout_announced = False
    reported_lost = False
    previous_remaining = mass.remaining_waypoints

    now = 0.0
    while now <= TOTAL_SECONDS:
        blackout = BLACKOUT_FROM <= now < BLACKOUT_UNTIL

        # --- MASS 시간 진행 및 위치보고 송신
        for outbound in mass.tick(now):
            received, _ = send(
                outbound,
                mass.mmsi,
                now,
                outcome=OUTCOME_LOST if blackout else OUTCOME_DELIVERED,
                reason="RF 구간 차단" if blackout else "",
            )
            for message in received:
                roc.receive(message.payload, message.source_id, now)
                # ROC가 실제로 해석한 것만 기록한다.
                track = roc.tracks.get(message.source_id)
                if track is not None:
                    log.observe(track.report, message.source_id, now)

            if blackout and not blackout_announced:
                blackout_announced = True
                log.event(now, EVENT_LINK, "MASS 송신 차단 시작", "RF 구간 손실 모사")

        # --- waypoint 통과 (새 항로를 받아 개수가 늘어난 경우는 제외)
        if mass.remaining_waypoints < previous_remaining:
            passed = len(planned_route) - mass.remaining_waypoints
            if mass.remaining_waypoints == 0:
                log.event(now, EVENT_ROUTE, "항로 완주", "모든 waypoint 통과")
            else:
                log.event(now, EVENT_ROUTE, f"WP{passed} 통과")
        previous_remaining = mass.remaining_waypoints

        # --- ROC가 항로를 지시하고 MASS가 응답
        if not command_sent and now >= COMMAND_AT:
            command_sent = True
            outbound = roc.issue_route(mass.mmsi, planned_route, now=now)
            linkage_id = sorted(roc.commands)[-1]
            received, burst_size = send(outbound, roc.mmsi, now)
            log.event(
                now, EVENT_COMMAND, "ROC → MASS 항로 지시",
                f"Type 92 · Linkage {linkage_id} · Burst {burst_size} B",
            )
            for message in received:
                for reply in mass.receive(message.payload, message.source_id, now):
                    reply_received, reply_size = send(reply, mass.mmsi, now)
                    for reply_message in reply_received:
                        roc.receive(reply_message.payload, reply_message.source_id, now)
                    log.event(
                        now, EVENT_COMMAND, f"MASS → ROC {reply.label}",
                        f"Burst {reply_size} B",
                    )

        # --- ROC 시간 진행 (통신 두절 판정)
        roc.tick(now)
        if roc.link_lost and not reported_lost:
            reported_lost = True
            log.event(
                now, EVENT_LINK, "통신 두절 판정",
                f"위치보고 {roc.report_timeout:g}s 이상 미수신",
            )
        elif not roc.link_lost and reported_lost:
            reported_lost = False
            log.event(now, EVENT_LINK, "위치보고 재개", "두절 해제")

        now += TICK_SECONDS

    # ---------------------------------------------------------------- 표시
    print()
    print(render_console(log, interval=60.0))
    print()

    info = log.summary()
    print("결과")
    print(f"  관측 위치보고      {info['samples']:,}건")
    print(f"  최대 관측 공백     {info['gap']:.0f}s")
    print(f"  송신 후 유실       {info['lost']}건")
    print(f"  송신 기회 없음     {info['no_slot']}건")
    print(f"  총 송신 시도       {info['attempts']:,}회")
    print(f"  전파 나간 양       {info['bytes']:,} byte")
    print(f"  거부된 명령        {mass.rejected_count}건")
    print(f"  응답 없는 명령     {len(roc.unanswered)}건")
    print(f"  남은 waypoint      {mass.remaining_waypoints}개")

    output = write_report(
        ROOT / "results" / "mass_roc_observation.html",
        log,
        title="MASS/ROC 관찰 보고서 — 정상 동작",
    )
    print()
    print(f"관찰 보고서: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
