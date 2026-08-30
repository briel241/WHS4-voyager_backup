"""Message ID 5/6/7 (항행경보·환경정보·기상관측) 검증 — 장비 없이 돌아간다.

실행: /opt/homebrew/bin/python3 tests/test_mass_roc_env_messages.py

압축 스키마(docs/compact_message_schemas.md)대로 인코딩·디코딩이 왕복하는지,
크기가 설계와 맞는지, "값 없음" sentinel이 보존되는지를 본다.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import codec
from MASS import MassAgent, ShipState
from ROC import RocAgent
from common.messages import environmental_info as env
from common.messages import navigational_warning as warn
from common.messages import weather_observation as wx

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def envelope_bytes(body_bits: int) -> int:
    return math.ceil((codec.ENVELOPE_BITS + body_bits) / 8)


# ---------------------------------------------------- Message ID 5 항행 경보

section("Navigational Warning (Message ID 5) — S-124")

w = warn.from_values(
    warning_number=137,
    year=2026,
    hazard_type=warn.HAZARD_DANGEROUS_WRECK,
    latitude_degrees=35.18,
    longitude_degrees=129.08,
    category=warn.CATEGORY_COASTAL,
    geometry_type=warn.GEOMETRY_CIRCLE,
    extent_nm=2.5,
    valid_from=820_000_000,
    duration=1440,
    text="Dangerous wreck reported",
)
payload = codec.encode(w)
(back,) = (codec.decode(payload),)

check(back == w, "경보가 왕복해 복원된다")
check(back.year == 2026 and back.warning_number == 137, "경보번호·연도 유지")
check(back.category_name == "coastal", "Category 유지")
check(back.hazard_name == "dangerous wreck", "Hazard Type 유지")
check(abs(back.latitude_degrees - 35.18) < 1e-6, "좌표 유지", f"{back.latitude_degrees}")
check(abs(back.extent_nm - 2.5) < 1e-6, "Extent(해리) 유지", f"{back.extent_nm} NM")
check(back.text == "Dangerous wreck reported", "텍스트 원문 유지")

no_text = warn.from_values(warning_number=1, year=2026, hazard_type=0)
check(len(codec.encode(no_text)) == envelope_bytes(warn.FIXED_BITS),
      "텍스트 없는 경보 크기가 설계와 일치", f"{len(codec.encode(no_text))} B (고정부 154 bit)")

# 한글 UTF-8 텍스트도 왕복해야 한다
korean = warn.from_values(warning_number=2, year=2026, hazard_type=0, text="난파선 주의")
(kback,) = (codec.decode(codec.encode(korean)),)
check(kback.text == "난파선 주의", "UTF-8(한글) 텍스트 왕복")

# 255 byte 초과 텍스트는 거부
try:
    warn.NavigationalWarning(text="A" * 256)
    check(False, "255 byte 초과 텍스트를 거부한다")
except ValueError:
    check(True, "255 byte 초과 텍스트를 거부한다")

# ---------------------------------------------------- Message ID 6 환경 정보

section("Environmental Info (Message ID 6) — S-104/411/412")

e = env.from_measurements(
    latitude_degrees=35.0,
    longitude_degrees=129.0,
    valid_time=820_000_100,
    water_level_m=1.73,
    wl_trend=env.WL_TREND_RISING,
    current_speed_kn=2.4,
    current_dir_deg=91.5,
    wind_speed_ms=7.2,
    wind_dir_deg=270,
    wave_height_m=1.8,
    wave_dir_deg=280,
    wave_period_s=6.5,
    ice_conc_tenths=3,
    ice_thickness_cm=40,
)
(eback,) = (codec.decode(codec.encode(e)),)

check(eback == e, "환경 정보가 왕복해 복원된다")
check(abs(eback.water_level_m - 1.73) < 1e-6, "수위 유지", f"{eback.water_level_m} m")
check(eback.wl_trend_name == "rising", "수위 추세 유지")
check(abs(eback.current_speed_kn - 2.4) < 1e-6, "조류 속력 유지")
check(abs(eback.current_dir_deg - 91.5) < 1e-6, "조류 방향 유지")
check(abs(eback.wind_speed_ms - 7.2) < 1e-6, "풍속 유지")
check(abs(eback.wave_height_m - 1.8) < 1e-6, "파고 유지")
check(abs(eback.wave_period_s - 6.5) < 1e-6, "파주기 유지")
check(eback.presence_flags == (env.FLAG_WATER_LEVEL | env.FLAG_CURRENT | env.FLAG_WEATHER | env.FLAG_ICE),
      "Presence Flags가 실린 항목을 표시", f"0b{eback.presence_flags:04b}")
check(len(codec.encode(e)) == envelope_bytes(env.BODY_BITS),
      "환경 정보 크기가 설계와 일치", f"{len(codec.encode(e))} B (본문 185 bit)")

# 음수 수위와 sentinel
low = env.from_measurements(latitude_degrees=35.0, longitude_degrees=129.0, water_level_m=-0.55)
(lback,) = (codec.decode(codec.encode(low)),)
check(abs(lback.water_level_m + 0.55) < 1e-6, "음수 수위(간조) 유지", f"{lback.water_level_m} m")
check(lback.current_speed_kn is None and lback.wind_speed_ms is None,
      "안 실은 항목은 not available로 복원")

# ---------------------------------------------------- Message ID 7 기상관측 보고

section("Weather Observation (Message ID 7) — BUFR")

o = wx.from_measurements(
    latitude_degrees=34.5,
    longitude_degrees=128.7,
    obs_time=820_000_200,
    air_temp_c=18.3,
    dew_point_c=12.1,
    sea_temp_c=19.6,
    pressure_hpa=1013.2,
    pressure_tendency=2,
    wind_dir_deg=225,
    wind_speed_ms=9.4,
    wave_height_m=2.1,
    wave_period_s=7,
    present_weather=5,
    visibility=8,
)
(oback,) = (codec.decode(codec.encode(o)),)

check(oback == o, "기상관측이 왕복해 복원된다")
check(abs(oback.air_temp_c - 18.3) < 1e-6, "기온 유지", f"{oback.air_temp_c} C")
check(abs(oback.dew_point_c - 12.1) < 1e-6, "이슬점 유지")
check(abs(oback.sea_temp_c - 19.6) < 1e-6, "수온 유지")
check(abs(oback.pressure_hpa - 1013.2) < 1e-6, "기압 유지", f"{oback.pressure_hpa} hPa")
check(oback.wind_dir_deg == 225 and abs(oback.wind_speed_ms - 9.4) < 1e-6, "바람 유지")
check(len(codec.encode(o)) == envelope_bytes(wx.BODY_BITS),
      "기상관측 크기가 설계와 일치", f"{len(codec.encode(o))} B (본문 180 bit)")

# 영하 기온(offset 부호화)과 sentinel
cold = wx.from_measurements(latitude_degrees=60.0, longitude_degrees=5.0, air_temp_c=-12.5)
(cback,) = (codec.decode(codec.encode(cold)),)
check(abs(cback.air_temp_c + 12.5) < 1e-6, "영하 기온 유지", f"{cback.air_temp_c} C")
check(cback.pressure_hpa is None and cback.dew_point_c is None,
      "안 잰 항목은 not available로 복원")

# 빈 기본 메시지도 왕복(전부 sentinel)
empty = wx.WeatherObservation()
(emptyback,) = (codec.decode(codec.encode(empty)),)
check(emptyback == empty, "전부 not available인 기본 메시지도 왕복")

# ---------------------------------------------------- describe / 계층 분리

section("describe · Message ID 분리")

check("Navigational Warning" in codec.describe(w), "경보 describe에 이름이 실린다")
check("Environmental Info" in codec.describe(e), "환경정보 describe에 이름이 실린다")
check("Weather Observation" in codec.describe(o), "기상관측 describe에 이름이 실린다")

# 셋의 Message ID가 서로 다르고 5/6/7이다
ids = {warn.MESSAGE_ID, env.MESSAGE_ID, wx.MESSAGE_ID}
check(ids == {5, 6, 7}, "Message ID가 5·6·7로 배정된다", str(sorted(ids)))

# ------------------------------------------------------------ 부록 매핑표

section("부록 매핑표 — Hazard Type · Present Weather · Visibility")

# S-124 Hazard Type: 그룹별 코드가 왕복하고 이름이 붙는다
piracy = warn.from_values(warning_number=3, year=2026, hazard_type=warn.HAZARD_PIRACY,
                          latitude_degrees=12.5, longitude_degrees=43.3)
(pback,) = (codec.decode(codec.encode(piracy)),)
check(pback.hazard_type == warn.HAZARD_PIRACY == 33, "Hazard 코드(해적 33) 왕복")
check(pback.hazard_name == "piracy or armed robbery", "Hazard 이름 매핑")
check(warn.HAZARD_TSUNAMI == 50 and warn.HAZARD_NAMES[50] == "tsunami", "자연현상 그룹(지진해일 50)")
check(len(warn.HAZARD_NAMES) == 32 and max(warn.HAZARD_NAMES) < 64,
      "Hazard 매핑 32종, 6 bit 범위 내", f"{len(warn.HAZARD_NAMES)}종")
# 예약 코드는 reserved로 표시
check(warn.NavigationalWarning(hazard_type=63).hazard_name == "code(63)"
      or "reserved" in warn.NavigationalWarning(hazard_type=7).hazard_name
      or "code(7)" == warn.NavigationalWarning(hazard_type=7).hazard_name,
      "예약/미정의 코드도 표시된다")

# WMO Present Weather / Visibility
storm = wx.from_measurements(latitude_degrees=34.5, longitude_degrees=128.7,
                             present_weather=25, visibility=3)
(sback,) = (codec.decode(codec.encode(storm)),)
check(sback.present_weather == 25 and sback.present_weather_name == "thunderstorm with rain",
      "Present Weather(뇌우+비 25) 왕복·매핑")
check(sback.visibility == 3 and sback.visibility_name == "0.5 km",
      "Visibility(0.5 km, 코드 3) 왕복·매핑")
check(len(wx.PRESENT_WEATHER_NAMES) == 32, "Present Weather 매핑 32종(0~30+없음)")
check(wx.WeatherObservation().present_weather_name == "not available"
      and wx.WeatherObservation().visibility_name == "not available",
      "기본값은 not available로 표시")
check("thunderstorm with rain" in codec.describe(storm)
      and "0.5 km" in codec.describe(storm), "describe에 기상현상·시정 이름이 실린다")

# -------------------------------------------- 행위자 동작: 경보 방송 → 반영

section("행위자 동작 — 육상국(ROC) 경보 방송 → MASS 반영")

roc = RocAgent()
mass = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))

# ROC가 자선 부근(반경 5 NM 원)에 위험 경보를 방송한다
hazard = warn.from_values(
    warning_number=42, year=2026, hazard_type=warn.HAZARD_DANGEROUS_WRECK,
    latitude_degrees=35.0, longitude_degrees=129.0,
    category=warn.CATEGORY_LOCAL, geometry_type=warn.GEOMETRY_CIRCLE,
    extent_nm=5.0, duration=warn.DURATION_INDEFINITE, text="난파선 주의",
)
out = roc.issue_warning(hazard, now=0.0)
check(out.is_broadcast and not out.ack_required, "경보는 브로드캐스트·ACK 불요로 나간다")

# MASS가 방송을 수신해 반영한다 (broadcast라 응답 없음)
replies = mass.receive(out.payload, roc.mmsi, now=0.0)
check(replies == [], "방송 경보에는 응답하지 않는다")
check(mass.warning_count == 1, "경보를 반영(저장)한다")
check(mass.active_warnings[0].text == "난파선 주의", "반영한 경보 내용이 보존된다")

# 자선이 원 구역(5 NM) 안에 있으므로 영향받는 경보로 잡힌다
check(len(mass.affecting_warnings()) == 1, "자선이 위험 구역 안임을 인식한다")

# 배를 구역 밖(약 30 NM 북쪽)으로 옮기면 영향에서 빠진다
mass.state.latitude = 35.5
check(mass.affecting_warnings() == [], "구역을 벗어나면 영향에서 빠진다")
check(mass.warning_count == 1, "구역을 벗어나도 경보 자체는 계속 반영한다")

# 같은 번호의 갱신은 덮어쓴다
updated = warn.from_values(warning_number=42, year=2026,
                           hazard_type=warn.HAZARD_DANGEROUS_WRECK, text="해제 예정")
mass.receive(codec.encode(updated), roc.mmsi, now=1.0)
check(mass.warning_count == 1 and mass.active_warnings[0].text == "해제 예정",
      "같은 (연도,번호) 경보는 최신으로 갱신된다")

section("행위자 동작 — 경보 유효기간 만료")

m2 = MassAgent(state=ShipState(latitude=35.0, longitude=129.0))
timed = warn.from_values(warning_number=7, year=2026, hazard_type=warn.HAZARD_FIRING,
                         duration=1)  # 1분
m2.receive(codec.encode(timed), roc.mmsi, now=0.0)
m2.tick(30.0)
check(m2.warning_count == 1, "유효기간(1분) 내에는 유지된다", "30s")
m2.tick(61.0)
check(m2.warning_count == 0, "유효기간이 지나면 자동으로 버린다", "61s")

# 점(point) 경보는 저장되지만 원 구역 판정 대상은 아니다
m2.receive(codec.encode(warn.from_values(
    warning_number=8, year=2026, hazard_type=warn.HAZARD_ICE,
    latitude_degrees=35.0, longitude_degrees=129.0)), roc.mmsi, now=100.0)
check(m2.warning_count == 1 and m2.affecting_warnings() == [],
      "점 경보는 반영하되 구역 판정에서 제외")

# ------------------------------------------------------------------ 결과

print()
if FAILED:
    print(f"실패 {len(FAILED)}건")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("전부 통과")
