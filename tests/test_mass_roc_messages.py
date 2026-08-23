"""MASS/ROC 응용 메시지 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_messages.py

여기서 하는 일은 "코드가 설계대로인가"를 표준 문서의 값으로 직접 대조하는
것이다. 특히 필드 크기와 "값 없음" sentinel은 ITU-R M.1371(AIS)과
IMO SN.1/Circ.289에 실린 값이므로, 이 테스트가 깨지면 표준에서 벗어난 것이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import codec, config, coordinates
from common.bitfield import BitReader, BitWriter
from common.messages import application_ack as ack
from common.messages import position_report as pos
from common.messages import route_instruction as route

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------- bitfield

section("bitfield — MSB-first 및 2의 보수")

writer = BitWriter()
writer.write_uint(0b101, 3, "test")
writer.write_uint(0b11111, 5, "test")
check(writer.to_bytes() == bytes([0b10111111]), "MSB-first로 이어 붙는다", "101+11111")

for value, bits in [(-1, 8), (-128, 8), (127, 8), (0, 8), (-67_108_864, 27)]:
    w = BitWriter()
    w.write_int(value, bits, "signed")
    w.pad_to_byte()
    r = BitReader(w.to_bytes())
    check(r.read_int(bits, "signed") == value, f"2의 보수 왕복 {value} ({bits} bit)")

try:
    BitWriter().write_uint(256, 8, "overflow")
    check(False, "범위 초과를 거부한다")
except ValueError:
    check(True, "범위 초과를 거부한다", "uint 256 in 8 bit")

try:
    BitWriter().write_int(128, 8, "overflow")
    check(False, "부호付 범위 초과를 거부한다")
except ValueError:
    check(True, "부호付 범위 초과를 거부한다", "int 128 in 8 bit")

# ------------------------------------------------------------- coordinates

section("coordinates — AIS와 동일한 1/10,000 분 (ITU-R M.1371)")

check(coordinates.UNITS_PER_DEGREE == 600_000, "1도 = 600,000 단위", "60분 x 10,000")
check(coordinates.LONGITUDE_BITS == 28, "경도 28 bit")
check(coordinates.LATITUDE_BITS == 27, "위도 27 bit")
check(
    coordinates.LONGITUDE_NOT_AVAILABLE == 108_600_000,
    "경도 not available = 181도",
    "108,600,000",
)
check(
    coordinates.LATITUDE_NOT_AVAILABLE == 54_600_000,
    "위도 not available = 91도",
    "54,600,000",
)

# 표준 sentinel이 필드 폭 안에 들어가야 한다
check(
    -(1 << 27) <= coordinates.LONGITUDE_NOT_AVAILABLE < (1 << 27),
    "경도 sentinel이 28 bit 부호付 범위에 들어간다",
)
check(
    -(1 << 26) <= coordinates.LATITUDE_NOT_AVAILABLE < (1 << 26),
    "위도 sentinel이 27 bit 부호付 범위에 들어간다",
)

# 실좌표 왕복 (부산항 근처)
for lon_deg, lat_deg in [(129.0756, 35.1796), (-122.4194, 37.7749), (0.0, 0.0), (-180.0, -90.0)]:
    raw_lon = coordinates.longitude_to_raw(lon_deg)
    raw_lat = coordinates.latitude_to_raw(lat_deg)
    back_lon = coordinates.raw_to_longitude(raw_lon)
    back_lat = coordinates.raw_to_latitude(raw_lat)
    ok = abs(back_lon - lon_deg) < 1e-6 and abs(back_lat - lat_deg) < 1e-6
    check(ok, f"좌표 왕복 {lat_deg}, {lon_deg}")

check(coordinates.raw_to_longitude(coordinates.LONGITUDE_NOT_AVAILABLE) is None,
      "경도 not available은 None으로 돌아온다")
check(coordinates.raw_to_latitude(coordinates.LATITUDE_NOT_AVAILABLE) is None,
      "위도 not available은 None으로 돌아온다")

# --------------------------------------------------- Position Report 필드

section("Position Report — AIS Message 1/2/3 차용 (ITU-R M.1371)")

check(pos.BODY_BITS == 111, "본문 111 bit", f"AIS 108 + Autonomy Mode 3 = {pos.BODY_BITS}")
check(pos.SOG_NOT_AVAILABLE == 1023, "SOG not available = 1023")
check(pos.COG_NOT_AVAILABLE == 3600, "COG not available = 3600")
check(pos.TRUE_HEADING_NOT_AVAILABLE == 511, "Heading not available = 511")
check(pos.RATE_OF_TURN_NOT_AVAILABLE == -128, "ROT not available = -128")
check(pos.NAVIGATION_STATUS_UNDEFINED == 15, "Navigation Status undefined = 15")
check(pos.TIMESTAMP_NOT_AVAILABLE == 60, "Timestamp not available = 60")

# 기본 생성자는 모든 필드가 not available이어야 한다 (안전한 기본값)
default = pos.PositionReport()
check(
    default.longitude_degrees is None
    and default.latitude_degrees is None
    and default.sog_knots is None
    and default.cog_degrees is None
    and default.heading_degrees is None,
    "기본값은 전부 not available",
    "값 없음과 0을 구분한다",
)

# ------------------------------------------------- Position Report 왕복

section("Position Report — 인코딩 왕복")

report = pos.from_measurements(
    longitude_degrees=129.0756,
    latitude_degrees=35.1796,
    sog_knots=12.3,
    cog_degrees=87.5,
    heading_degrees=88,
    navigation_status=0,
    rate_of_turn=-5,
    timestamp=42,
    autonomy_mode=pos.AUTONOMY_RC,
)
payload = codec.encode(report)
check(len(payload) == 17, "payload 17 byte", f"실제 {len(payload)} byte")

decoded = codec.decode(payload)
check(isinstance(decoded, pos.PositionReport), "PositionReport로 복원된다")
check(decoded == report, "모든 필드가 일치한다")
check(abs(decoded.sog_knots - 12.3) < 1e-9, "SOG 12.3 kn 복원")
check(abs(decoded.cog_degrees - 87.5) < 1e-9, "COG 87.5도 복원")
check(decoded.rate_of_turn == -5, "음수 ROT 복원", "2의 보수")
check(decoded.autonomy_mode_name == "RC", "Autonomy Mode RC 복원")

# not available만 담은 보고도 왕복해야 한다
empty = pos.PositionReport()
check(codec.decode(codec.encode(empty)) == empty, "not available 보고도 왕복한다")

# -------------------------------------------------- Route Instruction

section("Route Instruction — IFM DAC=001/FI=28 차용 (IMO SN.1/Circ.289)")

check(route.FIXED_BITS == 61, "고정부 61 bit", f"실제 {route.FIXED_BITS}")
check(route.WAYPOINT_BITS == 55, "waypoint 55 bit", "경도 28 + 위도 27")
check(route.MAX_WAYPOINTS == 16, "waypoint 최대 16개")
check(route.ROUTE_TYPE_MANDATORY == 1, "Route Type 1 = mandatory route")
check(route.ROUTE_TYPE_CANCELLATION == 31, "Route Type 31 = cancellation")
check(route.SENDER_SHIP == 0 and route.SENDER_AUTHORITY == 1,
      "Sender Classification 0=ship, 1=authority", "MASS<->MASS도 표현 가능")
check(route.DURATION_NOT_AVAILABLE == 262_143, "Duration not available = 262,143")

instruction = route.RouteInstruction(
    waypoints=[
        route.waypoint_from_degrees(129.10, 35.20),
        route.waypoint_from_degrees(129.15, 35.25),
        route.waypoint_from_degrees(129.20, 35.30),
    ],
    linkage_id=42,
    sender_classification=route.SENDER_AUTHORITY,
    route_type=route.ROUTE_TYPE_MANDATORY,
    start_month=8,
    start_day=15,
    start_hour=14,
    start_minute=30,
    duration=120,
)
route_payload = codec.encode(instruction)
decoded_route = codec.decode(route_payload)
check(decoded_route == instruction, "3-waypoint 항로가 왕복한다")
check(len(decoded_route.waypoints) == 3, "waypoint 3개 복원")
check(
    abs(decoded_route.waypoints[1].latitude_degrees - 35.25) < 1e-6,
    "waypoint 좌표 복원",
)
check(decoded_route.sender_name == "authority", "Sender authority 복원")
check(decoded_route.route_type_name == "mandatory route", "Route Type 이름 복원")

# 취소 지시 — 표준이 정한 두 가지 표현
cancel_by_type = route.RouteInstruction(route_type=route.ROUTE_TYPE_CANCELLATION, linkage_id=42)
cancel_by_duration = route.RouteInstruction(duration=route.DURATION_CANCEL, linkage_id=42)
check(cancel_by_type.is_cancellation, "Route Type 31이 취소로 판정된다")
check(cancel_by_duration.is_cancellation, "Duration 0이 취소로 판정된다")
check(not instruction.is_cancellation, "정상 항로는 취소가 아니다")

# waypoint 개수별 크기 — 설계 문서 6.1 표와 대조
section("Route Instruction — waypoint 개수별 payload 크기")

LINK_ID_11_BUDGET = 37  # docs/mass_roc_message_set.md 6
for count, fits_link11 in [(1, True), (2, True), (3, True), (4, False), (16, False)]:
    wps = [route.waypoint_from_degrees(129.0 + i * 0.01, 35.0) for i in range(count)]
    size = len(codec.encode(route.RouteInstruction(waypoints=wps, linkage_id=1)))
    actual_fits = size <= LINK_ID_11_BUDGET
    check(
        actual_fits == fits_link11,
        f"waypoint {count}개 = {size} byte, Link ID 11({LINK_ID_11_BUDGET} B) "
        + ("수용" if fits_link11 else "초과"),
    )

# 표준 상한 초과는 거부해야 한다
try:
    route.RouteInstruction(waypoints=[route.Waypoint()] * 17)
    check(False, "waypoint 17개를 거부한다")
except ValueError:
    check(True, "waypoint 17개를 거부한다", "표준 상한 16")

# ------------------------------------------------------ Application ACK

section("Application ACK")

check(ack.BODY_BITS == 16, "본문 16 bit", f"실제 {ack.BODY_BITS}")
acknowledgement = ack.ApplicationAck(linkage_id=42, status=ack.STATUS_ACCEPTED)
ack_payload = codec.encode(acknowledgement)
check(len(ack_payload) == 5, "payload 5 byte", f"실제 {len(ack_payload)} byte")
decoded_ack = codec.decode(ack_payload)
check(decoded_ack == acknowledgement, "ACK가 왕복한다")
check(decoded_ack.linkage_id == 42, "Linkage ID로 대상을 참조한다")
check(decoded_ack.status_name == "accepted", "Status 이름 복원")

# --------------------------------------------------------------- 봉투

section("codec 봉투 — VPFI (IALA R1027)")

check(codec.VPFI_BITS == 16, "VPFI 16 bit")
check(codec.ENVELOPE_BITS == 24, "봉투 24 bit", "VPFI 16 + Message ID 8")

# 선두 2 byte가 VPFI여야 한다
check(
    int.from_bytes(payload[:2], "big") == config.VPFI,
    "payload 선두가 VPFI",
    f"0x{config.VPFI:04X}",
)
check(payload[2] == pos.MESSAGE_ID, "3번째 byte가 Message ID")

# 다른 VPFI는 우리 payload가 아니므로 거부
try:
    codec.decode(codec.encode(report, vpfi=0x1234))
    check(False, "다른 VPFI를 거부한다")
except ValueError:
    check(True, "다른 VPFI를 거부한다", "우리 응용의 payload가 아니다")

# 등록되지 않은 Message ID 거부
bogus = bytearray(payload)
bogus[2] = 99
try:
    codec.decode(bytes(bogus))
    check(False, "등록되지 않은 Message ID를 거부한다")
except ValueError:
    check(True, "등록되지 않은 Message ID를 거부한다")

# 잘린 payload 거부
try:
    codec.decode(payload[:6])
    check(False, "잘린 payload를 거부한다")
except ValueError:
    check(True, "잘린 payload를 거부한다")

# 링크 계층이 넘겨줄 수 있는 잡음도 조용히 통과시키면 안 된다
try:
    codec.decode(b"\x00\x00\x00\x00\x00")
    check(False, "잡음 payload를 거부한다")
except ValueError:
    check(True, "잡음 payload를 거부한다", "VPFI 불일치")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
