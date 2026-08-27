# SC-VDE-005 노선 계획 — 전선 형식 (링크 팀 전달용)

## 내용물 (무슨 데이터인가)

ROC가 MASS에게 **항로를 지시**한다 — "이 웨이포인트들을 이 속도로 따라가라."
예: 필수 항로, 웨이포인트 2개(부산 앞바다), 12.5→8노트. **조종 명령(침로·모드)은 없음.**

> 조종을 함께 지시하는 경우는 SC-VDE-006(원격 조종). **둘은 같은 메시지**(Message ID 2)이고,
> 노선은 Heading·Fallback을 비우고 조종은 채운다. codec 하나로 둘 다 처리된다.

## 규격 (전선 바이트)

**봉투**: `[ VPFI 16bit | Message ID 8bit(=2) | 본문 | byte padding ]`.
현재 VPFI=`0xFF01`(placeholder). big-endian bit, MSB first.

**본문 = 고정부 76bit + 웨이포인트당 77bit.**

### 고정부 (76 bit)

| 필드 | bit | 형식 | 값·N/A |
| --- |:---:| --- | --- |
| Message Linkage ID | 10 | uint | 0=N/A. ACK 상관 ID |
| Sender Classification | 3 | uint | 0=ship, 1=authority |
| Route Type | 5 | uint | 1=mandatory,2=recommended,5=ship-route-plan,31=cancellation,0=N/A |
| Start Month/Day/Hour/Minute | 4/5/5/6 | uint | 유효 시작. N/A=0/0/24/60 |
| Duration | 18 | uint | 유효 지속(분). 262143=N/A |
| Heading Setpoint | 12 | uint | **노선은 3600=N/A로 비움** |
| Fallback State | 3 | uint | **노선은 0=none으로 비움** |
| Number of Waypoints | 5 | uint | 0–16 (=n) |

### 웨이포인트 (n번 반복, 각 77 bit)

| 필드 | bit | 형식 | 값·N/A |
| --- |:---:| --- | --- |
| Longitude | 28 | int(2의보수) | 181°=108,600,000 → N/A |
| Latitude | 27 | int(2의보수) | 91°=54,600,000 → N/A |
| Turn Radius | 10 | uint | 1023=N/A, ×100→0.01 NM |
| Planned SOG | 10 | uint | 1023=N/A, ×10→0.1 kn |
| Leg Geometry | 2 | uint | 0=none,1=Lexodrome,2=Orthodrome |

**좌표**: `raw = round(도 × 600,000)` (1도=600,000단위), 남·서는 음수. 복원 `도=raw/600,000`.

## 예시 벡터 (검증용, 32 byte, VPFI 포함)

디코드 시 → mandatory, WP1 129.076°/35.102° SOG12.5 Lexodrome, WP2 129.333°/35.208° SOG8.0 Orthodrome
```text
FF 01 02 01 C8 40 18 F3 FF FF E1 00 24 9D B9 E0 28 2B CA 01 90 FA A5 00 A1 C1 42 56 C0 1E 05 08
```

## ACK (MASS→ROC 응답, Message ID 3, 6 byte)

| 필드 | bit | 값 |
| --- |:---:| --- |
| Message Linkage ID | 10 | 원 노선의 linkage_id |
| Status | 4 | 4=Acknowledged,7=Errors,8=Incomplete,9=Route issues,0=received |
| Reason | 3 | 0=none,1=route_unsuitable,2=expired,3=not_authorized |

예시(Acknowledged): `FF 01 03 01 D0 00`

## 요청

노선용 **codec + payload_format 등록**(인증서 codec 방식) + **VPFI 배정**. 등록되면 우리는
`codec.py`의 16-bit VPFI를 제거해 응용 body를 `[Message ID | 본문 | padding]`으로 맞춘다.
→ 노선이 opaque가 아닌 **운영 종단**. **조종(SC-VDE-006)도 같은 codec으로 처리된다.**
