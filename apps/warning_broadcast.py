"""육상국 항행 경보 방송 → MASS 반영 데모 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 apps/warning_broadcast.py

ROC(육상국 역할)가 항행 경보(S-124 압축, Message ID 5)를 브로드캐스트하고,
그 payload가 실제 VDE-TER 링크 계층(Type 93 -> Packet -> Burst -> bits)을 통과해
MASS에 닿아 반영되는 것을 보인다. RF만 없고 나머지 사슬은 실물이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "VDETER_Link" / "no_error_correction"))

from common import codec
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.messages import navigational_warning as warn

from vdeter_link.burst import temporary_builder
from vdeter_link.burst.temporary_extractor import Extractor
from vdeter_link.messages.registry import MESSAGE_CODECS
from vdeter_link.messages.type_93 import Type93Message
from vdeter_link.packet import builder as packet_builder
from vdeter_link.packet import parser as packet_parser
from vdeter_link.utils import binary_rw


# Outbound(브로드캐스트)를 Type 93로 감싸 링크 계층을 통과시키고, 복원된 payload를 돌려준다.
def through_link(payload: bytes, source_id: int):
    link_message = Type93Message(
        source_id=source_id, session_id=0, destination_id=0, payload=payload
    )
    burst = temporary_builder.build(
        packet_builder.build([MESSAGE_CODECS[link_message.type_id].encode(link_message)])
    )
    preamble_bits = len(temporary_builder.PREAMBLE) * binary_rw.BITS_PER_BYTE
    stream = binary_rw.bytes_to_bits(burst)[preamble_bits:]
    packets = Extractor().consume(stream, [0])
    received = []
    for packet in packets:
        received.extend(packet_parser.parse(packet))
    return received, len(burst)


def main() -> int:
    roc = RocAgent()
    mass = MassAgent(state=ShipState(latitude=35.10, longitude=129.10, sog=0.0))

    print("=" * 72)
    print("항행 경보 방송 → MASS 반영")
    print(f"  육상국(ROC) MMSI {roc.mmsi} / MASS MMSI {mass.mmsi}")
    print(f"  자선 위치: {mass.state.latitude}, {mass.state.longitude}")
    print("=" * 72)

    # 방송할 경보 세 건: 자선을 감싸는 원, 멀리 있는 원, 점 경보
    warnings = [
        warn.from_values(
            warning_number=42, year=2026, hazard_type=warn.HAZARD_DANGEROUS_WRECK,
            latitude_degrees=35.10, longitude_degrees=129.10,
            category=warn.CATEGORY_LOCAL, geometry_type=warn.GEOMETRY_CIRCLE,
            extent_nm=3.0, duration=1440, text="Dangerous wreck, keep clear",
        ),
        warn.from_values(
            warning_number=43, year=2026, hazard_type=warn.HAZARD_FIRING,
            latitude_degrees=35.60, longitude_degrees=129.10,
            geometry_type=warn.GEOMETRY_CIRCLE, extent_nm=5.0, duration=120,
            text="Gunnery exercise",
        ),
        warn.from_values(
            warning_number=44, year=2026, hazard_type=warn.HAZARD_AID_OFF_STATION,
            latitude_degrees=35.12, longitude_degrees=129.12,
            geometry_type=warn.GEOMETRY_POINT, text="Buoy off station",
        ),
    ]

    now = 0.0
    for w in warnings:
        out = roc.issue_warning(w, now=now)
        received, burst_size = through_link(out.payload, roc.mmsi)
        for message in received:
            mass.receive(message.payload, roc.mmsi, now)
        print(
            f"방송  #{w.warning_number} {w.hazard_name:<28} "
            f"payload {len(out.payload):>2} B · Burst {burst_size} B"
        )
        now += 1.0

    print()
    print(f"MASS 반영 경보  {mass.warning_count}건")
    for w in mass.active_warnings:
        area = f"원 {w.extent_nm} NM" if w.geometry_name == "circle" else w.geometry_name
        print(f"  #{w.warning_number} {w.hazard_name} ({area}) — {w.text!r}")

    affecting = mass.affecting_warnings()
    print()
    print(f"자선이 들어가 있는 위험 구역  {len(affecting)}건")
    for w in affecting:
        print(f"  ⚠️  #{w.warning_number} {w.hazard_name} — 자선이 {w.extent_nm} NM 원 안")

    # 유효기간 경과 시연: 43번(120분)만 남기고 시간을 확 넘기면 만료로 빠진다
    print()
    mass.tick(1440 * 60 + 1)  # 24시간+1초 경과
    print(f"24시간 경과 후 반영 경보  {mass.warning_count}건 "
          f"(무기한/장기만 남고 유효기간 지난 것은 자동 해제)")
    for w in mass.active_warnings:
        print(f"  #{w.warning_number} {w.hazard_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
