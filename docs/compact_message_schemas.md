# 압축 응용 메시지 스키마 3종 — S-124 · S-104 · BUFR

> 서비스별 시나리오 카탈로그에서 ⚠️(형식은 확인됐으나 스키마 미정)로 남았던 세 서비스의
> **압축 payload 스키마**를 설계한다. 설계 원칙은 [`mass_roc_message_set.md`](mass_roc_message_set.md)와
> 동일하다 — **새로 발명하지 않고 실재 표준의 필드를 차용**, 좁은 VDE-TER 슬롯에 맞게 비트
> 단위로 압축, 표준에 없는 필드만 명시적으로 정의한다.

## 공통 규약 (기존 메시지와 동일)

| 항목 | 규약 |
| --- | --- |
| 봉투 | `[VPFI 16 | Message ID 8 | body | byte 패딩]` (`codec.py`) |
| 좌표 | 경도 28 bit / 위도 27 bit, 1/10,000분, 2의 보수. sentinel 경도 181° / 위도 91° |
| 시각 | UTC 초, 2000-01-01 00:00:00 기준 32 bit (M.2092-2 TBB Table 43 필드 1 규약) |
| 유효기간 | 분 단위 16 bit (TBB Table 43 필드 2 규약, 최대 45일) |
| 값 없음 | 각 필드 최상위값을 sentinel로 (AIS 방식) |
| Message ID | 1~4 사용 중 → **5·6·7 신규 배정** |

> Message ID 5/6/7은 `common/messages/registry.py`에 등록하고 `codec.py`의 Union에 추가하면
> 기존 인코딩·링크 통과 경로를 그대로 탄다. 본 문서는 **설계**이며 구현은 별도.

---

## 1. Message ID 5 — 항행 경보 (S-124)

**서비스**: 항행 경보 · **규격**: IHO S-124 Navigational Warnings (Ed 2.0.0) · **전송**: Type 93 broadcast

### 1.1 표준 필드 차용 근거

S-124의 GML 구조에서 무선 방송에 필요한 핵심만 취한다. 텍스트 원문은 유지하되 나머지는
열거값·좌표로 압축한다.

| S-124 요소 | 본 스키마 필드 | 근거 |
| --- | --- | --- |
| `messageSeriesIdentifier` (경보번호·연도) | Warning Number + Year | S-124 §경보 식별 |
| `navigationalWarningType` (NAVAREA/연안/국지) | Category | IMO WWNWS 3계층 |
| `generalCategory` (위험 종류) | Hazard Type | S-124 feature catalogue 통제어휘 |
| geometry (point/area) | Geometry Type + 좌표 + Extent | S-124 공간 속성 |
| `fixedDateRange` | Valid-from + Duration | S-124 유효기간 |
| `warningInformation.text` | Text (가변) | 원문 보존 |

### 1.2 비트 레이아웃

| # | 필드 | 비트 | 부호화 · 비고 |
| --- | --- | ---: | --- |
| 1 | Warning Number | 14 | 0~16383 |
| 2 | Year | 7 | 2000+n (2000~2127) |
| 3 | Category | 2 | 0 NAVAREA · 1 연안 · 2 국지 · 3 예약 |
| 4 | Hazard Type | 6 | 통제어휘 64종 (난파선·부표이탈·군사훈련·표류물 등) |
| 5 | Geometry Type | 2 | 0 점 · 1 원 · 2 사각 · 3 폴리곤(참조만) |
| 6 | Longitude | 28 | 점/원중심/사각 좌상 |
| 7 | Latitude | 27 | 〃 |
| 8 | Extent | 12 | 원 반경 0.01 NM (0~40.95) 또는 사각 반크기 |
| 9 | Valid-from | 32 | UTC 초 (2000 기준) |
| 10 | Duration | 16 | 분 (0 = 무기한) |
| 11 | Text Length | 8 | 0~255 byte |
| 12 | Text | 8×N | UTF-8 |

**고정부 = 154 bit ≈ 19.25 B**, 봉투 3 B 포함 **≈ 22 B + 텍스트**.
Link ID 17 Type 93 payload 218 B → 텍스트 최대 ~190 B. Link ID 11(37 B)에서는 텍스트 ~12 B.

**프로젝트 정의**: Hazard Type 6비트 인코딩 표(64종)는 S-124 통제어휘를 우리 코드값으로
매핑한 것 — 매핑표를 별도 부록으로 관리한다.

---

## 2. Message ID 6 — 환경 정보 (S-104 / S-411 / S-412)

**서비스**: 실시간 해양 기상·조석 · **규격**: IHO S-104 수위 / S-411 해빙 / S-412 기상
(관련 S-111 표층류) · **전송**: Type 93 broadcast

> 실제 S-100 제품은 **HDF5 격자 데이터(대용량)** 이고, 그 전체 전송은 data session 경로를
> 유지한다(카탈로그 참조). 본 스키마는 **한 지점/격자셀의 nowcast를 방송용으로 압축**한
> 보완 경로다 — 배가 자기 위치 부근 환경값을 빠르게 받는 용도.

### 2.1 표준 필드 차용 근거

| 제품 | 요소 | 본 스키마 필드 |
| --- | --- | --- |
| S-104 | water level height, trend, datum | Water Level + Trend |
| S-111 | surface current speed/direction | Current Speed/Dir |
| S-412 | wind speed/direction, wave | Wind, Wave |
| S-411 | ice concentration, thickness | Ice Conc/Thickness |

### 2.2 비트 레이아웃

| # | 필드 | 비트 | 부호화 · sentinel |
| --- | --- | ---: | --- |
| 1 | Longitude | 28 | 격자셀 중심 |
| 2 | Latitude | 27 | 〃 |
| 3 | Valid-time | 32 | UTC 초 (nowcast 시각) |
| 4 | Presence Flags | 4 | 비트: 수위·표층류·기상·해빙 포함 여부 |
| 5 | Water Level | 16 | 부호 cm, datum 기준 (±327.67 m), sentinel 0x8000 |
| 6 | WL Trend | 2 | 0 상승 · 1 하강 · 2 정체 · 3 불명 |
| 7 | Current Speed | 10 | 0.1 kn, 1023 = 없음 (SOG 규약 재사용) |
| 8 | Current Dir | 12 | 0.1°, 3600 = 없음 (COG 규약 재사용) |
| 9 | Wind Speed | 10 | 0.1 m/s, 1023 = 없음 |
| 10 | Wind Dir | 9 | 1°, 511 = 없음 |
| 11 | Wave Height | 8 | 0.1 m (0~25.5), 255 = 없음 |
| 12 | Wave Dir | 9 | 1°, 511 = 없음 |
| 13 | Wave Period | 6 | 0.5 s (0~31.5), 63 = 없음 |
| 14 | Ice Conc | 4 | 1/10 (0~10), 15 = 없음 |
| 15 | Ice Thickness | 8 | cm, 255 = 없음 |

**합계 = 185 bit ≈ 23.1 B**, 봉투 3 B 포함 **≈ 26 B**. Type 93로 충분.

**프로젝트 정의**: "한 셀 압축"은 표준에 없는 우리 프로파일이다. 전체 격자가 필요하면
S-100 원본 + data session을 쓴다 — 두 경로를 Presence Flags로 구분 가능.

---

## 3. Message ID 7 — 기상관측 보고 (BUFR)

**서비스**: 선박 기상관측 보고 · **규격**: WMO FM-94 BUFR, 선박 관측 시퀀스(BUFR 3-08-009)
· **전송**: MASS→육상, Type 93(ACK 불요) 또는 Type 92 · **방향**: 위치보고와 같은 ship→shore

### 3.1 표준 필드 차용 근거

BUFR 선박 관측 템플릿의 관측 요소를 비트 필드로 옮긴다. BUFR 자체가 이진 압축이지만
테이블 구동이라 디코더가 무겁다 — VDE-TER용으로 **고정 서브셋을 비트 인코딩**한다.

| WMO 파라미터 | 본 스키마 필드 |
| --- | --- |
| air temperature | Air Temp |
| dew-point temperature | Dew Point |
| sea-surface temperature | Sea Temp |
| pressure (MSL) | Pressure |
| pressure tendency (특성) | Pressure Tendency |
| wind direction/speed | Wind Dir/Speed |
| wave height/period | Wave Height/Period |
| present weather / visibility | Present Weather / Visibility |

### 3.2 비트 레이아웃

| # | 필드 | 비트 | 부호화 · sentinel |
| --- | --- | ---: | --- |
| 1 | Longitude | 28 | 관측 위치 |
| 2 | Latitude | 27 | 〃 |
| 3 | Obs Time | 32 | UTC 초 (2000 기준) |
| 4 | Air Temp | 11 | 0.1°C, offset −60.0 (−60.0~+144.7), 2047 = 없음 |
| 5 | Dew Point | 11 | 〃 |
| 6 | Sea Temp | 11 | 〃 |
| 7 | Pressure | 14 | 0.1 hPa, offset 500.0 (500.0~2138.3), 16383 = 없음 |
| 8 | Pressure Tendency | 4 | WMO 특성 코드 0~8, 15 = 없음 |
| 9 | Wind Dir | 9 | 1°, 511 = 없음 |
| 10 | Wind Speed | 10 | 0.1 m/s, 1023 = 없음 |
| 11 | Wave Height | 8 | 0.1 m (0~25.5), 255 = 없음 |
| 12 | Wave Period | 6 | 초 (0~62), 63 = 없음 |
| 13 | Present Weather | 5 | WMO 코드표 4677 축약 32종 |
| 14 | Visibility | 4 | WMO 코드(범주형) |

**합계 = 180 bit ≈ 22.5 B**, 봉투 3 B 포함 **≈ 25 B**. Link ID 11(37 B)에도 여유.

**프로젝트 정의**: Present Weather 5비트·Visibility 4비트는 WMO 코드표를 축약 매핑한 것 —
전체 코드가 필요하면 확장 필드로 뺀다.

---

## 4. 요약 — 설계 완료

| Message ID | 서비스 | 규격 | 고정부 | 봉투 포함 | 전송 |
| :-: | --- | --- | ---: | ---: | --- |
| 5 | 항행 경보 | S-124 | 154 bit | ~22 B + 텍스트 | Type 93 broadcast |
| 6 | 환경 정보 | S-104/411/412 | 185 bit | ~26 B | Type 93 broadcast |
| 7 | 기상관측 보고 | BUFR | 180 bit | ~25 B | Type 92/93 ship→shore |

셋 다 **Link ID 17 단일 메시지에 여유롭게 적재**되며, 5·7은 Link ID 11(37 B)에서도 가능하다
(경보는 텍스트 길이 제한). 이로써 카탈로그의 ⚠️ 3종이 **압축 스키마 확정**으로 바뀐다.

### 구현 상태

- ✅ **구현 완료**: `common/messages/navigational_warning.py`(ID 5) ·
  `environmental_info.py`(ID 6) · `weather_observation.py`(ID 7). registry·codec 등록,
  `tests/test_mass_roc_env_messages.py`로 왕복·크기·sentinel 검증(전부 통과). 링크 계층
  (Type 93) 통과도 확인.

### 남은 것

- S-104 전체 격자(HDF5)의 data session 경로는 별도 유지 — 본 스키마는 방송용 압축 보완.

---

## 부록 A — S-124 Hazard Type 매핑 (6 bit)

IMO/IHO/WMO **MSI Manual의 NAVAREA 경보 주제**와 S-124 `generalCategory`를 그룹별 코드로
옮긴 것이다(본 프로젝트 정의 매핑). 빈 코드는 그룹 확장용 예약이다.
구현: `navigational_warning.py`의 `HAZARD_NAMES`.

| 코드 | 상수 | 의미 |
| :-: | --- | --- |
| 0 | HAZARD_UNSPECIFIED | 미지정 |
| | **항로표지 (1~9)** | |
| 1 | HAZARD_AID_MALFUNCTION | 항로표지 오작동 |
| 2 | HAZARD_AID_OFF_STATION | 표지 이탈·소실 |
| 3 | HAZARD_AID_CHANGED | 표지 신설·변경 |
| 4 | HAZARD_LIGHT_UNRELIABLE | 등화 불안정 |
| | **위험물 (10~19)** | |
| 10 | HAZARD_DANGEROUS_WRECK | 위험 난파선 |
| 11 | HAZARD_NEW_OBSTRUCTION | 신규 암초·천퇴·장애물 |
| 12 | HAZARD_UNCHARTED_DANGER | 해도 미기재 위험 |
| 13 | HAZARD_DRIFTING_HAZARD | 표류 위험물 |
| 14 | HAZARD_DRIFTING_MINE | 표류 기뢰 |
| 15 | HAZARD_DERELICT | 표류 컨테이너·유기선 |
| 16 | HAZARD_ICE | 결빙 위험 |
| 17 | HAZARD_LARGE_TOW | 대형·부자유 예인 |
| | **작업 (20~29)** | |
| 20 | HAZARD_CABLE_PIPELINE | 케이블·파이프라인 작업 |
| 21 | HAZARD_SURVEY | 측량 작업 |
| 22 | HAZARD_SAR | 수색구조 작업 |
| 23 | HAZARD_ANTIPOLLUTION | 방제 작업 |
| 24 | HAZARD_OFFSHORE | 해상 설치물 활동 |
| | **군사·보안 (30~39)** | |
| 30 | HAZARD_MILITARY_EXERCISE | 해군 훈련 |
| 31 | HAZARD_FIRING | 사격·미사일 발사 |
| 32 | HAZARD_MILITARY_OPS | 군사 작전 구역 |
| 33 | HAZARD_PIRACY | 해적·무장강도 |
| 34 | HAZARD_SECURITY | 보안 특별 경보 |
| | **항행 서비스·경로 (40~49)** | |
| 40 | HAZARD_RADIONAV_FAULT | 전파항법 서비스 장애 |
| 41 | HAZARD_ROUTEING | 선박 통항로 변경 |
| 42 | HAZARD_TSS | 통항분리방식 변경 |
| 43 | HAZARD_VTS | VTS 관련 |
| | **자연 현상 (50~59)** | |
| 50 | HAZARD_TSUNAMI | 지진해일 |
| 51 | HAZARD_ABNORMAL_WATER | 이상 수위·폭풍해일 |
| 52 | HAZARD_SEVERE_WEATHER | 악천후·태풍 |
| 53 | HAZARD_VOLCANIC | 화산 활동 |
| 54 | HAZARD_NATURAL_OTHER | 기타 자연 현상 |

---

## 부록 B — WMO 기상현상·시정 매핑

### B.1 Present Weather (5 bit) — WMO 코드표 4677(ww) 축약

구현: `weather_observation.py`의 `PRESENT_WEATHER_NAMES`. (31 = 없음)

| 코드 | 의미 | 코드 | 의미 |
| :-: | --- | :-: | --- |
| 0 | 유의 현상 없음 | 16 | 소나기성 비 |
| 1 | 구름 발달·증가 | 17 | 약한 눈 |
| 2 | 연무 | 18 | 눈 |
| 3 | 연기 | 19 | 강한 눈 |
| 4 | 먼지·모래(공중) | 20 | 진눈깨비 |
| 5 | 먼지·모래폭풍 | 21 | 소나기성 눈 |
| 6 | 박무(mist) | 22 | 쌀알눈 |
| 7 | 안개 | 23 | 싸락우박·우박 |
| 8 | 착빙성 안개 | 24 | 뇌우(강수 없음) |
| 9 | 약한 이슬비 | 25 | 뇌우+비 |
| 10 | 이슬비 | 26 | 뇌우+우박·눈 |
| 11 | 착빙성 이슬비 | 27 | 스콜 |
| 12 | 약한 비 | 28 | 토네이도·용오름 |
| 13 | 비 | 29 | 지면 날린눈 |
| 14 | 강한 비 | 30 | 광범위 먼지 |
| 15 | 착빙성 비 | 31 | 없음 |

### B.2 Visibility (4 bit) — WMO 코드표 4377 축약(90~99)

구현: `weather_observation.py`의 `VISIBILITY_NAMES`. (15 = 없음)

| 코드 | 시정 | 코드 | 시정 |
| :-: | --- | :-: | --- |
| 0 | < 0.05 km | 5 | 2 km |
| 1 | 0.05 km | 6 | 4 km |
| 2 | 0.2 km | 7 | 10 km |
| 3 | 0.5 km | 8 | 20 km |
| 4 | 1 km | 9 | ≥ 50 km |

> 참고: [`mass_roc_message_set.md`](mass_roc_message_set.md) (설계 원칙·좌표 규약) ·
> IHO S-124/S-104/S-411/S-412 · WMO-No.306 Manual on Codes (BUFR) · M.2092-2 Table 43
