# SC-VDE-006 원격 조종 — 전선 형식 (링크 팀 전달용)

## 내용물 (무슨 데이터인가)

노선이 이미 교환된 전제에서, ROC가 **조종 명령**을 보낸다 — "지금 침로 92.5°로 조종하고
**투묘(Anchoring) 개시**, 속도 6노트." 노선(SC-VDE-005)과 **같은 메시지**(Message ID 2)에
**Heading Setpoint·Fallback State를 채운 것**이다(노선은 이 둘을 비움).

> codec 관점에서 노선/조종은 **완전히 동일한 메시지**다. 두 필드(Heading·Fallback)의 유무만
> 다르고, 그 구분·업무 판정은 응용(MASS/ROC) 몫이다. → **codec 하나로 둘 다 처리.**

## 규격 (전선 바이트)

**봉투**: `[ VPFI 16bit | Message ID 8bit(=2) | 본문 | byte padding ]`.
현재 VPFI=`0xFF01`(placeholder). big-endian bit, MSB first.

**본문 = 고정부 76bit + 웨이포인트당 77bit.**

### 고정부 (76 bit) — 조종의 핵심은 굵게

| 필드                          | bit     | 형식   | 값·N/A                                                             |
| --------------------------- |:-------:| ---- | ----------------------------------------------------------------- |
| Message Linkage ID          | 10      | uint | 0=N/A. ACK 상관 ID                                                  |
| Sender Classification       | 3       | uint | 0=ship, 1=authority                                               |
| Route Type                  | 5       | uint | 1=mandatory,2=recommended,5=ship-route-plan,31=cancellation,0=N/A |
| Start Month/Day/Hour/Minute | 4/5/5/6 | uint | 유효 시작. N/A=0/0/24/60                                              |
| Duration                    | 18      | uint | 유효 지속(분). 262143=N/A                                              |
| **Heading Setpoint**        | 12      | uint | **목표 침로. ×10→0.1° (0–359.9°), 3600=N/A**                          |
| **Fallback State**          | 3       | uint | **0=none,1=Anchoring,2=DynamicPositioning,3=Drifting**            |
| Number of Waypoints         | 5       | uint | 0–16 (=n)                                                         |

### 웨이포인트 (n번 반복, 각 77 bit)

| 필드           | bit | 형식        | 값·N/A                           |
| ------------ |:---:| --------- | ------------------------------- |
| Longitude    | 28  | int(2의보수) | 181°=108,600,000 → N/A          |
| Latitude     | 27  | int(2의보수) | 91°=54,600,000 → N/A            |
| Turn Radius  | 10  | uint      | 1023=N/A, ×100→0.01 NM          |
| Planned SOG  | 10  | uint      | 1023=N/A, ×10→0.1 kn            |
| Leg Geometry | 2   | uint      | 0=none,1=Lexodrome,2=Orthodrome |

**좌표**: `raw = round(도 × 600,000)` (1도=600,000단위), 남·서는 음수. 복원 `도=raw/600,000`.

## 예시 벡터 (검증용, 32 byte, VPFI 포함)

디코드 시 → **heading 92.5°, fallback Anchoring**, WP1 SOG6.0, WP2 SOG4.0

```text
FF 01 02 01 C8 40 18 F3 FF FF 39 D2 24 9D 7C F0 28 2B 34 00 F0 78 A4 EF 91 01 41 A6 F8 07 82 84
```

## ACK (MASS→ROC 응답, Message ID 3, 6 byte)

| 필드                 | bit | 값                                                              |
| ------------------ |:---:| -------------------------------------------------------------- |
| Message Linkage ID | 10  | 원 명령의 linkage_id                                               |
| Status             | 4   | 4=Acknowledged,7=Errors,8=Incomplete,9=Route issues,0=received |
| Reason             | 3   | 0=none,1=route_unsuitable,2=expired,3=not_authorized           |

예시(Acknowledged): `FF 01 03 01 D0 00`

## 요청

**노선(SC-VDE-005)과 같은 codec**으로 처리된다 — route용 **codec + payload_format 등록** +
**VPFI 배정**만 되면, 노선·조종 모두 opaque가 아닌 **운영 종단**이 된다.
