# SC-VDE-006 원격 조종 — 전선 형식 (링크 팀 전달용)

## 무엇을 보내는가

노선이 이미 교환된 상태에서 ROC가 조종 명령을 보내는 메시지다. 예를 들어 "지금 침로를
92.5°로 잡고 투묘(Anchoring)를 시작하라, 속도는 6노트"와 같은 명령이다. 노선(SC-VDE-005)과
같은 메시지(Message ID 2)를 쓰되, 노선이 비워 두는 Heading Setpoint·Fallback State 필드를
채운다.

> codec 입장에서 노선과 조종은 완전히 같은 메시지다. Heading·Fallback 두 필드를 채웠는지
> 아닌지만 다르고, 그 구분과 업무 판단은 응용(MASS/ROC)이 맡는다. 따라서 codec 하나로 둘 다
> 처리한다.

> **등록 완료** — 노선과 **같은 키** `sc_vde.route_control.command`(TEST scope)를 쓴다.
> API 계약은 `vdeter_mass_roc_api_guide.md` §8.3이 정본이고, 이 문서는 그 요약이다.

## 바이트 형식

우리가 `submit(payload=...)`에 넣는 것은 **본문뿐**이다. VPFI와 Message Format은 VDETER
Presentation codec이 붙인다 — 직접 붙이면 거부된다.

```text
우리가 넣는 application bytes :  [ 본문 | byte padding ]
VDETER가 만드는 wire         :  ff 7f 02 || application bytes
```

비트는 big-endian, MSB first. 본문은 **고정부 76bit + 웨이포인트마다 77bit**다.

### 고정부 (76 bit)

조종에서 핵심이 되는 Heading Setpoint·Fallback State 필드를 굵게 표시했다.

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

좌표는 `raw = round(도 × 600,000)`으로 담는다(1도=600,000단위). 남·서는 음수이고,
복원은 `도 = raw / 600,000`이다.

허용되는 application 길이는 `ceil((76+77n)/8)`로 정해진 17가지뿐이다 —
`10, 20, 29, 39, 48, 58, 68, 77, 87, 97, 106, 116, 125, 135, 145, 154, 164`.
마지막 바이트의 남는 padding 비트는 모두 0이어야 한다.

## 예시 (승인된 참조 벡터, application 29 byte)

디코드하면 Linkage 7, Sender authority, **침로(heading) 92.5°, fallback Anchoring**,
WP1 SOG 6.0, WP2 SOG 4.0이 나온다.

```text
01 c8 40 18 f3 ff ff 39 d2 24 9d 7c f0 28 2b 34 00 f0 78 a4 ef 91 01 41 a6 f8 07 82 84
```

앞에 `ff 7f 02`나 예전 임시 `ff 01`을 붙이지 않는다. 우리 저장소에서는
`tests/test_sc_vde_contract.py`가 이 벡터와의 일치를 시험한다.

## ACK — MASS가 ROC에 보내는 응답 (별도 format, application 3 byte)

ACK은 `sc_vde.route_control.ack` 키로 **따로 submit** 한다. 링크의 `ack_required`가
만드는 전달 ACK과는 별개이며, 자동 생성되지 않는다. MASS는 명령을 해석한 뒤 업무 정책에
따라 ACK 본문을 만들어 별도 `submit()`을 호출해야 한다.

| 필드                 | bit | 값                                                                    |
| ------------------ |:---:| -------------------------------------------------------------------- |
| Message Linkage ID | 10  | 원 명령의 linkage_id                                                     |
| Status             | 4   | 4=Acknowledged, 7=Errors, 8=Incomplete, 9=Route issues (0=예약, 아래 참고) |
| Reason             | 3   | 0=none,1=route_unsuitable,2=expired,3=not_authorized                 |

정상 수신(Linkage 7, Acknowledged, Reason None) 예시: `01 d0 00` — 여기에도 `ff 7f 03`을
붙이지 않는다.

**Status 4·7·8·9는 S-421 Ed.1.0.0의 `routeInfoStatus` 코드다.** Status `0`은 S-421에 없는
본 프로젝트 예약값이며 **현재 송신하지 않는다.**

"수신했다"는 링크 계층 ACK(`ack_required`)이 이미 답하고, 응용 ACK은 판정 결과만
나른다 — IMO SN.1/Circ.289가 두 층위를 구분하라고 정한 대로다. 우리 MASS는 수신과
동시에 검증·판정을 끝내므로 "접수했으나 판정 전"이라는 구간이 존재하지 않는다.
판정 전에는 아무것도 보내지 않고, 발신 측이 미응답으로 관리한다.

codec은 0을 유효 raw로 받아들이되(구조 검증), 응용이 이 값을 만들지는 않는다.
나중에 판정을 비동기로 바꾸게 되면 이 자리를 쓴다.

## 남은 조건

계약은 승인됐지만 **배포 build에 SC-VDE codec이 들어와야** 실제로 쓸 수 있다(가이드 §17).
codec이 없는 build나 OPERATIONAL scope에서는 `UNSUPPORTED_REQUEST`가 정상 결과이고, 그때
다른 format 키나 opaque로 우회하지 않는다.

> **안전 제한 (조종은 특히 중요)** — SC-VDE는 서명·replay 보호가 없는 unsigned Segment
> Type 0으로 전달된다. 격리된 유선 시험·데모 전용이며, 이 payload를 받았다는 이유만으로
> **실제 조타·actuator를 구동해서는 안 된다.** 운영에 쓰려면 Signed Segment 또는 승인된
> application 인증과 replay/freshness 정책이 먼저 필요하다.
