"""MASS/ROC 응용 메시지 <-> VDE-TER 링크 계층 연동 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_link_integration.py

test_mass_roc_messages.py가 응용 계층 안에서의 왕복을 본다면, 여기서는
그 payload가 실제로 팀원의 링크 계층(VDETER_Link/)을 통과하는지 본다.

    응용 메시지 -> payload bytes -> Type 92/93 Message -> VDE Packet
    -> Temporary Burst -> (bit stream) -> Extractor -> Packet Parser
    -> payload bytes -> 응용 메시지

GNU Radio는 필요 없다. adapters/를 건드리지 않고 messages/packet/burst만 쓴다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "VDETER_Link" / "no_error_correction"))

from common import codec, config
from common.messages import application_ack as ack
from common.messages import position_report as pos
from common.messages import route_instruction as route

from vdeter_link.burst import temporary_builder
from vdeter_link.burst.temporary_extractor import Extractor
from vdeter_link.messages.type_92 import Type92Message
from vdeter_link.messages.type_93 import Type93Message
from vdeter_link.packet import builder as packet_builder
from vdeter_link.packet import parser as packet_parser
from vdeter_link.messages.registry import MESSAGE_CODECS
from vdeter_link.utils import binary_rw

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# 응용 메시지들을 링크 계층 전체 사슬에 통과시키고 복원된 응용 메시지를 돌려준다.
def round_trip(link_messages: list) -> list:
    encoded = [MESSAGE_CODECS[m.type_id].encode(m) for m in link_messages]
    burst = temporary_builder.build(packet_builder.build(encoded))

    # Correlator 뒤의 수신 bit stream을 흉내낸다.
    # F9A8 preamble 뒤부터가 Extractor가 보는 구간이고, tag는 그 첫 bit를 가리킨다.
    preamble_bits = len(temporary_builder.PREAMBLE) * binary_rw.BITS_PER_BYTE
    stream = binary_rw.bytes_to_bits(burst)[preamble_bits:]

    packets = Extractor().consume(stream, [0])
    if len(packets) != 1:
        raise AssertionError(f"VDE Packet 1개를 기대했으나 {len(packets)}개")

    return [codec.decode(m.payload) for m in packet_parser.parse(packets[0])]


# ------------------------------------------------ Type 93 위치보고 (브로드캐스트)

section("Position Report — Type 93 브로드캐스트로 링크 통과")

report = pos.from_measurements(
    longitude_degrees=129.0756,
    latitude_degrees=35.1796,
    sog_knots=12.3,
    cog_degrees=87.5,
    heading_degrees=88,
    navigation_status=0,
    autonomy_mode=pos.AUTONOMY_RC,
)
payload = codec.encode(report)

# Type 93은 ACK가 필요 없고 broadcast에 쓸 수 있다 (M.2092-2 Table 35).
link_message = Type93Message(
    source_id=config.MASS_MMSI,
    session_id=0,
    destination_id=0,
    payload=payload,
)
check(link_message.length == 12 + len(payload), "Type 93 Length가 payload를 반영한다",
      f"{link_message.length} byte")

(recovered,) = round_trip([link_message])
check(recovered == report, "위치보고가 링크를 통과해 복원된다")
check(recovered.autonomy_mode_name == "RC", "Autonomy Mode 유지")
check(abs(recovered.latitude_degrees - 35.1796) < 1e-6, "좌표 유지")

# ------------------------------------------------- Type 92 항로 지시 (지정 수신)

section("Route Instruction — Type 92 addressed로 링크 통과")

instruction = route.RouteInstruction(
    waypoints=[
        route.waypoint_from_degrees(129.10, 35.20),
        route.waypoint_from_degrees(129.15, 35.25),
        route.waypoint_from_degrees(129.20, 35.30),
    ],
    linkage_id=42,
    sender_classification=route.SENDER_AUTHORITY,
    route_type=route.ROUTE_TYPE_MANDATORY,
    duration=120,
)

# Type 92는 ACK를 요구하며 destination을 0으로 둘 수 없다 (M.2092-2 Table 40).
command = Type92Message(
    source_id=config.ROC_MMSI,
    session_id=0,
    destination_id=config.MASS_MMSI,
    retransmission_id=0,
    payload=codec.encode(instruction),
)
(recovered_route,) = round_trip([command])
check(recovered_route == instruction, "항로 지시가 링크를 통과해 복원된다")
check(len(recovered_route.waypoints) == 3, "waypoint 3개 유지")
check(recovered_route.sender_name == "authority", "Sender Classification 유지")

# --------------------------------------------- 다중 메시지 Packet

section("다중 메시지 — 한 VDE Packet에 위치보고 + ACK")

acknowledgement = ack.ApplicationAck(linkage_id=42, status=ack.STATUS_ACKNOWLEDGED)
multi = [
    Type93Message(
        source_id=config.MASS_MMSI,
        session_id=0,
        destination_id=0,
        payload=codec.encode(report),
    ),
    Type93Message(
        source_id=config.MASS_MMSI,
        session_id=0,
        destination_id=config.ROC_MMSI,
        payload=codec.encode(acknowledgement),
    ),
]
recovered_multi = round_trip(multi)
check(len(recovered_multi) == 2, "메시지 2개가 분리되어 복원된다")
check(recovered_multi[0] == report, "첫 메시지는 위치보고")
check(recovered_multi[1] == acknowledgement, "둘째 메시지는 ACK")

# --------------------------------------------- 정상 동작 시나리오

section("정상 동작 시나리오 — 명령 수신 후 침로 변화")

# 1) MASS가 침로 87.5도로 항행 중임을 보고
before = pos.from_measurements(
    longitude_degrees=129.0756, latitude_degrees=35.1796,
    sog_knots=12.3, cog_degrees=87.5, heading_degrees=88,
    navigation_status=0, autonomy_mode=pos.AUTONOMY_RC,
)
# 2) ROC가 항로 지시
#    3) MASS가 ACK
# 4) MASS가 변경된 침로를 보고
after = pos.from_measurements(
    longitude_degrees=129.0800, latitude_degrees=35.1850,
    sog_knots=11.0, cog_degrees=45.0, heading_degrees=46,
    navigation_status=0, autonomy_mode=pos.AUTONOMY_RC,
)

steps = [
    ("MASS->ROC 위치보고", Type93Message(
        source_id=config.MASS_MMSI, session_id=0, destination_id=0,
        payload=codec.encode(before))),
    ("ROC->MASS 항로지시", Type92Message(
        source_id=config.ROC_MMSI, session_id=0, destination_id=config.MASS_MMSI,
        retransmission_id=0, payload=codec.encode(instruction))),
    ("MASS->ROC 응용 ACK", Type93Message(
        source_id=config.MASS_MMSI, session_id=0, destination_id=config.ROC_MMSI,
        payload=codec.encode(acknowledgement))),
    ("MASS->ROC 위치보고", Type93Message(
        source_id=config.MASS_MMSI, session_id=0, destination_id=0,
        payload=codec.encode(after))),
]

expected = [before, instruction, acknowledgement, after]
ok = True
for (label, link_msg), want in zip(steps, expected):
    (got,) = round_trip([link_msg])
    burst_size = len(temporary_builder.build(packet_builder.build(
        [MESSAGE_CODECS[link_msg.type_id].encode(link_msg)])))
    check(got == want, f"{label} 왕복", f"Temporary Burst {burst_size} byte")
    ok = ok and (got == want)

check(
    ok and abs(before.cog_degrees - 87.5) < 1e-9 and abs(after.cog_degrees - 45.0) < 1e-9,
    "명령 전후 COG 변화가 관측된다",
    "87.5도 -> 45.0도",
)

# --------------------------------------------- 링크가 payload를 해석하지 않음

section("계층 분리 — 링크는 payload를 해석하지 않는다")

opaque = Type93Message(
    source_id=config.MASS_MMSI, session_id=0, destination_id=0,
    payload=b"\xde\xad\xbe\xef",
)
(link_only,) = packet_parser.parse(
    packet_builder.build([MESSAGE_CODECS[opaque.type_id].encode(opaque)])
)
check(link_only.payload == b"\xde\xad\xbe\xef",
      "링크는 임의 bytes를 그대로 나른다", "응용 계층과 독립")

try:
    codec.decode(link_only.payload)
    check(False, "응용 계층이 남의 payload를 거부한다")
except ValueError:
    check(True, "응용 계층이 남의 payload를 거부한다", "VPFI 불일치")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
