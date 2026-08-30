# 위치보고 — 전선 형식

> **등록 완료** — `sc_vde.position_report`(VPFI `ff 7f`, Message Format 1, TEST scope)로
> 링크 팀이 codec·registry를 반영했다. 우리 참조 벡터와 바이트 단위로 일치한다.
>
> **단 broadcast는 채택되지 않았다.** `DR-PAYLOAD-016` 승인에 따라
> **addressed(MASS→ROC) · `ack_required=false` 전용**이다(아래 §요청사항 ③의 차선책).
> AIS는 브로드캐스트지만 VDETER가 MASS-originated broadcast를 지원하지 않아 이렇게
> 결정됐다. 실제 AIS 동작과 다른 점은 실험 해석 시 감안해야 한다.

> 노선(SC-VDE-005)·원격조종(SC-VDE-006)이 성립하려면 ROC가 배의 현재 위치를 알아야 한다.
> 이 문서는 그 위치보고의 전선 형식이다. 노선·조종과 **같은 carrier(`ff 7f`)** 를 쓰고
> **Message Format 1** 자리를 채운다.

## 무엇을 보내는가

MASS가 자기 위치·침로·속력·운항 상태를 주기적으로 알린다(기본 2초 간격). ROC는 이것으로
항적을 그리고, 일정 시간 끊기면 통신 두절로 판정한다. 명령이 아니라 **상태 보고**이므로
응용 ACK을 돌려받지 않는다.

## 표준 근거

AIS Position Report Class A(**ITU-R M.1371 Message 1/2/3**)의 Common Navigation Block을
그대로 차용했다. 필드 이름·부호화·N/A 값이 모두 AIS와 같으므로 링크 팀이 새로 정의할 것은
없다.

두 가지만 AIS와 다르다.

- **전송 계층 필드를 뺐다** — Message Type·Repeat Indicator·MMSI·Radio Status는 넣지 않는다.
  송신원 식별은 VDE 링크 계층 Source ID가 이미 담당하므로 중복이다.
- **Autonomy Mode(3 bit)를 더했다** — AIS Navigation Status에 자율운항을 나타내는 값이 없고,
  두 개념은 직교한다(자율운항 중에도 정박 상태일 수 있다). **본 프로젝트 정의이며 표준
  근거가 없다.**

## 바이트 형식

우리가 `submit(payload=...)`에 넣는 것은 **본문뿐**이다. VPFI와 Message Format은 VDETER
Presentation codec이 붙인다.

```text
우리가 넣는 application bytes :  [ 본문 111 bit | padding 1 bit ]  = 14 byte 고정
VDETER가 만드는 wire         :  ff 7f 01 || application bytes      = 17 byte
```

비트는 big-endian, MSB first. **길이는 항상 14 byte**다(노선처럼 가변이 아니다).
마지막 바이트의 남는 padding 1 bit는 0이어야 한다.

### 본문 (111 bit, 순서대로)

| 필드                 | bit | 형식        | 값·N/A                                              |
| ------------------ |:---:| --------- | -------------------------------------------------- |
| Navigation Status  | 4   | uint      | AIS Table 45. `15=undefined`                       |
| Rate of Turn       | 8   | int(2의보수) | AIS ROT<sub>AIS</sub>. `-128=N/A`                  |
| SOG                | 10  | uint      | ×10 → 0.1 knot. `1023=N/A`                         |
| Position Accuracy  | 1   | uint      | `0=low(>10m)`, `1=high(≤10m)`                      |
| Longitude          | 28  | int(2의보수) | `108,600,000`(181°) `=N/A`                         |
| Latitude           | 27  | int(2의보수) | `54,600,000`(91°) `=N/A`                           |
| COG                | 12  | uint      | ×10 → 0.1도. `3600=N/A`                             |
| True Heading       | 9   | uint      | 도(0–359). `511=N/A`                                |
| Timestamp          | 6   | uint      | UTC 초(0–59). `60=N/A`                              |
| Maneuver Indicator | 2   | uint      | AIS. `0=N/A`                                       |
| RAIM               | 1   | uint      | `0=not in use`, `1=in use`                         |
| **Autonomy Mode**  | 3   | uint      | **`0=unknown`, `1=RC`, `2=MON`, `3=AUT`** (프로젝트 정의) |

좌표는 노선과 동일하다 — `raw = round(도 × 600,000)`, 남·서는 음수, 복원은 `도 = raw / 600,000`.

## 예시 (참조 벡터, application 14 byte)

디코드하면 위도 35.102°N, 경도 129.076°E, SOG 12.5 kn, COG 47.2°, True Heading 47°,
Timestamp 30, Navigation Status 0(under way using engine), Autonomy Mode 2(MON)이다.

```text
00 01 f4 93 b7 3c 05 05 79 40 76 05 ef 04
```

앞에 `ff 7f 01`을 붙이지 않는다.

## 요청 사항

### ① payload_format 등록

노선·조종과 같은 방식으로 등록해 주기 바란다. **MF 1 자리가 비어 있다.**

| key | carrier VPFI | Message Format | scope |
| --- | --- | --- | --- |
| `sc_vde.position_report` | `ff 7f` (logical 16383) | **1** | TEST/PROJECT |

codec이 검증할 것은 세 가지다 — 길이가 정확히 14 byte인가, 마지막 padding 1 bit가 0인가,
각 필드가 위 표의 범위 안인가.

### ② MASS-originated broadcast (핵심)

위치보고는 **브로드캐스트**다. AIS가 그렇고, 실제로도 주변 선박이 함께 듣는 것이 정상
동작이므로 표준을 따르고자 한다. 그런데 현재 가이드는 이렇게 적고 있다.

> ROC broadcast: no-ACK Short Data 전송 경로는 **구현돼 있다**. 다만 실제 사용에는 크기
> 조건을 만족하는 **소형 운영 format의 registry mapping과 codec이 별도로 필요**하다.
> **MASS-originated broadcast**와 broadcast Data Session은 현재 **미지원**이다. (§4, §17)

요청은 두 가지로 나뉜다.

- **크기 조건**은 이 형식이 이미 만족한다. wire 17 byte로, 위에서 말한 "소형 format"에
  해당한다. 노선(32 byte)보다 작다.
- 남는 것은 **송신 주체를 MASS까지 넓히는 것**이다. 전송 경로 자체는 이미 있으므로
  새로 만드는 것이 아니라 여는 문제로 이해하고 있다. 맞는지, 그리고 가능한지 알려주기
  바란다.

`ack_required=False`(no-ACK)로만 쓴다. 응용 ACK도 없다.

### ③ 만약 broadcast가 어렵다면

MASS→ROC **addressed**로도 우리 시나리오는 성립한다(수신자가 ROC 하나뿐이므로). 다만
실제 AIS 동작과 달라지므로 차선이다. broadcast 지원 여부·일정을 먼저 알려주면 그에 맞춰
정하겠다.

## 참고

- 노선·조종 형식: `전선형식_노선_SC-VDE-005.md`, `전선형식_원격조종_SC-VDE-006.md`
- 이 형식의 구현 정본: `common/messages/position_report.py`
- 위 참조 벡터는 그 코드로 생성했다.
