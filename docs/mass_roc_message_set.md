# MASS↔ROC 응용 메시지 세트 설계

> 실제 표준에 정의된 메시지를 조사하여, 추측이 아닌 근거 위에서 MASS↔ROC 응용 메시지 세트를
> 구성한 문서입니다. 배경지식은 `mass_roc_background.md`를 참조합니다.

| 항목    | 내용                                                         |
| ----- | ---------------------------------------------------------- |
| 문서 목적 | 응용 계층 메시지 세트의 표준 근거 확보 및 설계 결정                             |
| 목표 단계 | **정상 동작하는 MASS/ROC 구현** (공격·실험은 후속)                        |
| 설계 원칙 | 새로 발명하지 않는다. 실재하는 표준 구조를 차용하고, **차용할 수 없는 부분만 명시적으로 정의**한다 |
| 1차 근거 | ITU-R M.2092-2, IMO SN.1/Circ.289, ITU-R M.1371 (AIS)      |
| 확보 자료 | `circ289.pdf`, `dnv.pdf`, M.2092-2 Table 8 / Table 25      |

---

## 1. 조사 결론 — 먼저 알아야 할 사실

### 1.1 MASS↔ROC 전용 표준 메시지 세트는 **존재하지 않는다**

- **IMO MASS Code**(2026-07-01 발효)는 비강제이며 **메시지 포맷을 정의하지 않는다.**
- **DNV-CG-0264**(자율·원격운항선 클래스 가이드라인)는 RCC의 상황인식 요건을 **개념 수준으로만**
  규정한다. Sec.6 §4는 "어떤 정보가 필요한가"를 말하지만 필드·비트 포맷은 없다.
- **IEC 63173-2(SECOM)** 는 **HTTPS/REST 기반 IP 웹서비스**이며 S-100 페이로드를 전제한다.
  좁은 VHF 링크인 VDE-TER와는 적용 계층이 다르다.
- **AIS Navigation Status에 자율운항을 나타내는 값이 없다.** 0–15 중 11·12는 예선(曳船) 지역용,
  14는 AIS-SART, 15는 undefined이며 **13만 "reserved for future use"** 로 남아 있다.

> **결론**: 그대로 가져다 쓸 단일 규격서는 없다. 직접 구성해야 하지만, 근거 없이 지어내서도 안 된다.

### 1.2 그러나 **구성 요소는 대부분 실재한다**

| 필요 기능         | 실재하는 표준 구조                                           | 근거                           |
| ------------- | ---------------------------------------------------- | ---------------------------- |
| 위치·침로·속력 보고   | **AIS Message 1/2/3** Common Navigation Block        | ITU-R M.1371                 |
| 육상→선박 항로 지시   | **IFM DAC=001, FI=28** Route information (addressed) | IMO SN.1/Circ.289 Table 13.2 |
| 항로 브로드캐스트     | **IFM FI=27** Route information (broadcast)          | SN.1/Circ.289                |
| 자유 텍스트        | **IFM FI=29/30** Text description                    | SN.1/Circ.289                |
| 응용 수준 응답      | **IFM FI=5** Application acknowledgement             | ITU-R M.1371 Annex 5         |
| Payload 포맷 식별 | **16-bit VPFI**                                      | IALA R1027                   |

**차용할 수 없어 직접 정의해야 하는 것**은 다음뿐이다.

- **자율운항 모드 / 제어권 상태** — 어떤 표준에도 필드가 없다(§1.1).
- **인증 필드** — 후속 보안 단계.

---

## 2. 설계 방식 선택 — 왜 "표준 구조 차용"인가

`AIS/IFM 구조 차용`이 최선인지 판단하기 위해 실현 가능한 대안을 모두 비교했다.

### 2.1 대안 비교

| #   | 방식                                      | 위치보고 크기    | 판정  | 사유                                |
|:---:| --------------------------------------- | ----------:|:---:| --------------------------------- |
| 1   | **IEC 61162-1 ASCII 문장** (`$GPRMC,...`) | ~70 B      | ✗   | 최대 79자 제약, 바이너리 대비 비효율. 무선 구간용 아님 |
| 2   | **SECOM / S-100**                       | —          | ✗   | HTTPS/REST·IP 전제. VDE-TER 계층과 불일치 |
| 3   | **범용 직렬화** (CBOR / Protobuf / ASN.1)    | 25–60 B    | ✗   | §2.2 참조                           |
| 4   | **완전 표준 Encapsulated AIS** (VPFI 0)     | 30 B+      | △   | §2.3 참조 — 부분 채택                   |
| 5   | **자체 바이너리 포맷 신규 설계**                    | ~14 B      | ✗   | §2.4 참조                           |
| 6   | **AIS/IFM 필드 구조 차용** ← **채택**           | **13.5 B** | ✓   | §2.5                              |

### 2.2 왜 범용 직렬화(CBOR/Protobuf)가 아닌가

일반 소프트웨어라면 자연스러운 선택이지만 여기서는 세 가지가 걸린다.

- **크기**: CBOR은 필드마다 키·타입 태그가 붙어 위치보고 하나가 40–60 B 수준이 된다. Protobuf도
  25–30 B다. 차용 방식(13.5 B)의 2–4배이며, **Link ID 11의 37 B 예산**(§6)에서 항로 메시지와
  함께 쓰면 곧바로 초과한다. AIS가 비트 단위로 설계된 이유가 정확히 이 제약 때문이다.
- **의미론 부재**: 직렬화 포맷은 "어떻게 담을지"만 정한다. 좌표 단위, ROT 부호화, "값 없음" 표현,
  항행상태 열거값은 **여전히 우리가 정의해야 한다.** 그 정의를 새로 만들면 결국 §2.4와 같아진다.
- **연구 신뢰성**: 본 프로젝트는 보안 연구다. 공격 시나리오(위치 위조, 항로 명령 위조)가 의미를
  가지려면 **메시지가 실제 해상 트래픽을 닮아야 한다.** 임의 포맷은 이 주장을 약화시킨다.

### 2.3 왜 "완전 표준 Encapsulated AIS"를 그대로 쓰지 않는가 — 부분 채택

M.2092-2 §A3-7.3은 AIS 메시지를 다른 채널로 나르는 방식을 정의한다.

> "ASM Message 0 may contain encapsulated AIS Messages **6, 8, 12, 14, 21, 25 or 26**.
> (...) The encapsulated message **may or may not be transmitted on AIS1 or AIS2 channels**."
> — M.2092-2 §A3-7.3

여기서 두 가지가 갈린다.

- **항로 지시는 완전 표준 경로가 있다.** IFM FI=28은 원래 **AIS Message 6**(addressed binary)에
  실리고, Message 6은 **캡슐화 허용 목록에 포함**된다. 즉 표준상 정합적인 경로가 존재한다.
- **위치보고는 그 경로가 없다.** AIS Message 1/2/3은 **캡슐화 목록에 없다.** 위치보고는 AIS 채널의
  SOTDMA로 자체 송신되는 것이 원칙이기 때문이다.

그러나 **완전 캡슐화는 우리 링크에서 낭비**다. AIS Message 6 전체를 감싸면 다음이 붙는다.

```text
Message ID(6) + Repeat(2) + Source ID(30) + Seq(2)
+ Destination ID(30) + Retransmit(1) + Spare(1) + IAI(16) = 88 bit = 11 byte
```

이 중 **Source ID·Destination ID는 VDE 링크 계층 Type 92 헤더가 이미 담고 있다**
(`mass_roc_background.md` §1). 즉 **11 byte를 중복 전송**하게 된다. Link ID 11의 37 B 예산에서
11 B는 30%다.

> **결정**: AIS/IFM의 **응용 계층 내용(application data)만** 취하고, 전송 계층 래퍼는 취하지
> 않는다. 송신원·수신처 식별은 VDE 링크 계층에 위임한다.

### 2.4 왜 자체 바이너리 신규 설계가 아닌가

크기는 차용 방식과 비슷하게 만들 수 있다. 그러나:

- 좌표 부호화, "값 없음" 표현, 항행상태 열거값, ROT 부호화를 **처음부터 다시 정의**하게 된다.
  이는 실질적으로 **AIS를 다시 발명하되 검증은 안 된 상태**로 만드는 일이다.
- 외부 검증이 불가능하다. 심사·리뷰 시 "이 필드가 왜 이 크기인가"에 답할 근거가 없다.
- 차용 방식은 ITU-R M.1371·IMO SN.1/Circ.289 문서로 **모든 필드의 출처를 제시할 수 있다.**

### 2.5 채택 근거 정리

| 근거            | 내용                                                       |
| ------------- | -------------------------------------------------------- |
| **비트 효율**     | AIS는 좁은 VHF 슬롯 제약 하에 설계됨. 위치보고 전체가 168 bit               |
| **의미론 검증됨**   | 항행상태·ROT·정확도 필드가 실운용으로 검증된 값                             |
| **"값 없음" 내장** | SOG=1023, COG=3600, Heading=511, 경도=181°, 위도=91°         |
| **부호화 일관성**   | AIS 위치와 IFM waypoint가 **동일하게 1/10,000 분** 사용             |
| **출처 추적 가능**  | 모든 필드가 ITU/IMO 문서로 근거 제시 가능                              |
| **연구 타당성**    | 공격 대상이 실제 해상 메시지를 닮아야 실증이 성립                             |
| **표준 선례**     | VPFI 0 = Encapsulated AIS로 "AIS 포맷을 다른 채널에 싣는" 패턴이 이미 존재 |

> **최종 판단**: 차용이 최선이다. 단 "AIS를 그대로 쓴다"가 아니라 **"응용 계층 필드 구조와
> 부호화를 차용하고, 전송 래퍼는 버리며, 표준에 없는 것은 명시적으로 추가한다"** 가 정확한 정의다.

### 2.6 채널에 대한 정확한 이해

M.2092-2 §A1-2.3의 채널 배정:

| 채널                            | 용도                                                  |
| ----------------------------- | --------------------------------------------------- |
| AIS 1 / AIS 2                 | AIS 전용 (ITU-R M.1371)                               |
| ASM 1 / ASM 2                 | ASM 전용                                              |
| **1024 / 1084 / 1025 / 1085** | VDE. ship-to-shore, shore-to-ship, **ship-to-ship** |
| **2024 / 2084 / 2025 / 2085** | VDE. shore-to-ship, **ship-to-ship**                |

**본 프로젝트의 메시지는 VDE 채널로 전송된다.** AIS 채널을 쓰지 않으며, AIS 장비와 경합하지도
않는다. 차용하는 것은 **채널이 아니라 필드 구조**다. 이 구분을 문서 전반에서 유지한다.

---

## 3. MASS → ROC : 위치·상태 보고

### 3.1 근거 — AIS Position Report Class A (Message 1/2/3)

| 필드                     | 비트  | 단위·부호화                                 | 채택                  |
| ---------------------- | ---:| -------------------------------------- |:-------------------:|
| Message Type           | 6   | 1–3                                    | ✗ 링크 계층이 대체         |
| Repeat Indicator       | 2   | 0–3                                    | ✗                   |
| MMSI                   | 30  | 송신국 식별자                                | ✗ VDE Source ID가 대체 |
| **Navigation Status**  | 4   | 0–15                                   | ✓                   |
| **Rate of Turn**       | 8   | 부호付, `ROT_AIS = 4.733 × √(ROT_sensor)` | ✓                   |
| **SOG**                | 10  | 0.1 knot, `1023` = 없음                  | ✓                   |
| **Position Accuracy**  | 1   | 1 = DGPS(<10 m), 0 = GNSS(>10 m)       | ✓                   |
| **Longitude**          | 28  | 1/10,000 분, 2의 보수 (東 = +), `181°` = 없음 | ✓                   |
| **Latitude**           | 27  | 1/10,000 분, 2의 보수 (北 = +), `91°` = 없음  | ✓                   |
| **COG**                | 12  | 0.1°, `3600` = 없음                      | ✓                   |
| **True Heading**       | 9   | 0–359, `511` = 없음                      | ✓                   |
| **Timestamp**          | 6   | UTC 초 0–59                             | ✓                   |
| **Maneuver Indicator** | 2   | 0 = 없음, 1 = 특수기동 아님, 2 = 특수기동          | ✓                   |
| **RAIM Flag**          | 1   | 무결성 감시 사용 여부                           | ✓                   |
| Radio Status           | 19  | SOTDMA 상태                              | ✗ 링크 계층 정보          |

채택분 합계 = **108 bit = 13.5 byte**

### 3.2 표준에 없어 추가하는 필드 — Autonomy Mode

§1.1에서 확인했듯 **AIS Navigation Status에는 자율운항을 나타내는 값이 없다.**

**Navigation Status에 욱여넣지 않는다.** 두 개념이 직교하기 때문이다 — 자율운항 중인 선박도
"under way using engine"(0)이거나 "at anchor"(1)일 수 있다. 하나로 합치면 정보가 손실된다.

> **프로젝트 정의 필드** (표준 근거 없음, 본 문서에서 신규 정의)

| 필드                | 비트  | 값                                                                                                                                    |
| ----------------- | ---:| ------------------------------------------------------------------------------------------------------------------------------------ |
| **Autonomy Mode** | 3   | 0 = unknown/default<br>1 = **RC** (원격제어 — 제어권 ROC)<br>2 = **MON** (감독 자율 — 세션 유지, 제어권 본선)<br>3 = **AUT** (독립 자율 — 세션 없음)<br>4–7 = 예약 |

값 정의는 `mass_roc_background.md` §2.5의 상태 모델과 일치시킨다. 정상 동작 단계에서는
RC/MON만 사용해도 무방하다.

### 3.3 최종 크기

```text
VPFI                    16 bit
Message ID               8
─────────────────────────────
AIS 차용부              108
Autonomy Mode (신규)      3
Padding                   1   (byte 정렬)
─────────────────────────────
합계                    136 bit = 17 byte
```

---

## 4. ROC → MASS : 항로 지시

### 4.1 근거 — IFM Route information (addressed), DAC=001 / FI=28

IMO SN.1/Circ.289 Annex Table 13.2. 응용 payload 부분(IAI 이후):

| 필드                        | 비트     | 값 정의                                                                                                                                     |
| ------------------------- | ------:| ---------------------------------------------------------------------------------------------------------------------------------------- |
| **Message Linkage ID**    | 10     | 1–1,023. 메시지 식별 일련번호. 0 = 없음                                                                                                             |
| **Sender Classification** | 3      | **0 = ship**, **1 = authority**, 2–7 예약                                                                                                  |
| **Route Type**            | 5      | **1 = mandatory route**, 2 = recommended, 3 = alternative,<br>4 = recommended through ice, 5 = ship route plan,<br>**31 = cancellation** |
| Start UTC Month           | 4      | 1–12, 0 = 없음                                                                                                                             |
| Start UTC Day             | 5      | 1–31, 0 = 없음                                                                                                                             |
| Start UTC Hour            | 5      | 0–23, 24 = 없음                                                                                                                            |
| Start UTC Minute          | 6      | 0–59, 60 = 없음                                                                                                                            |
| **Duration**              | 18     | 유효 종료까지 분(分). **0 = 항로 취소**, 262,143 = 없음                                                                                                |
| **Number of Waypoints**   | 5      | 1–16, 0 = 없음                                                                                                                             |
| **Waypoints**             | n × 55 | Longitude 28 + Latitude 27 (AIS와 동일 부호화)                                                                                                 |

고정부 = **61 bit**, waypoint 1개 = **55 bit**

### 4.2 이 메시지가 ROC 명령에 그대로 맞는 이유

- **`Sender Classification = 1 (authority)`** — ROC가 육상 당국 위치에 대응. **`0 = ship`** 이
  있으므로 §5의 MASS↔MASS에도 그대로 쓸 수 있다.
- **`Route Type = 1 (mandatory route)`** — 권고가 아닌 **강제 항로** 값이 표준에 이미 존재.
- **`Route Type = 31` / `Duration = 0`** — **명령 취소**가 규정되어 있다.
- **`Duration`** — 유효기간. 오래된 명령이 무한정 유효하지 않다. **재전송 공격 방어에 쓸 수 있는
  필드가 표준에 이미 있다.**
- **`Message Linkage ID`** — 명령 식별·참조. ACK 대상 지정에 사용(§6).

### 4.3 크기

```text
VPFI(16) + Message ID(8) + 고정부(61) + n × 55 = 85 + 55n bit
```

| Waypoint 수 | bit | byte  |
| ----------:| ---:| -----:|
| 1          | 140 | 17.5  |
| 2          | 195 | 24.4  |
| 3          | 250 | 31.3  |
| 4          | 305 | 38.1  |
| 8          | 525 | 65.6  |
| 16         | 965 | 120.6 |

---

## 5. MASS ↔ MASS — 표준이 정의하는 1급 시나리오

ROC↔MASS만 고려하면 누락이 생긴다. M.2092-2 §A4-4.28의 정의된 시퀀스 목록에는 다음이 포함된다.

- Ship originated broadcast **inside/outside control station service area**
- Ship to ship addressed message **inside/outside control station service area**
- **Ship to ship short data message** ← 본 프로젝트가 쓰는 Type 92/93

### 5.1 제어국 서비스 영역 안팎의 차이

> "While vessels are inside a control station service area, **all data session transmissions between
> ships should take place via the control station.** Ships outside the control station service area
> **may communicate directly.**" — M.2092-2 §A4-4.3

| 상황           | 경로                       | 시사점                      |
| ------------ | ------------------------ | ------------------------ |
| 제어국 영역 **안** | MASS → **ROC 경유** → MASS | ROC가 중계자. 공격 표면이 ROC에 집중 |
| 제어국 영역 **밖** | MASS ↔ MASS **직접**       | 중계자 없음. 상호 인증이 직접 필요     |

이 구분은 후속 공격 실험에서 의미가 있다 — **경유 여부에 따라 신뢰 경로가 달라진다.**

### 5.2 설계 영향은 작다

- **Position Report는 이미 Type 93 브로드캐스트**이므로 주변 MASS가 그대로 수신한다.
  **AIS 구조를 차용한 선택이 여기서 이점이 된다** — AIS는 원래 선박 간 브로드캐스트가 본래 용도다.
- **Route Instruction은 `Sender Classification = 0 (ship)`** 으로 선박 발신을 표현할 수 있다.
  표준이 이미 값을 정의해 두었으므로 필드 추가가 필요 없다.
- 따라서 **메시지 세트를 바꿀 필요는 없고**, 행위자(actor) 구현에서 수신처를 ROC로 한정하지
  않기만 하면 된다.

---

## 6. Link ID의 영향

### 6.0 왜 11 / 17 / 19인가 — 표준이 정한 것이다

Table 8에는 Link ID가 13개 실려 있지만(11, 35, 12, 36, 13, 14, 15, 16, 17, 37, 18, 38, 19)
**모두가 구현 대상은 아니다.** 각주가 붙지 않은 것은 **11, 17, 19 셋뿐**이다.

| 각주  | 원문                                                                                                  | 해당 Link ID             | 구현 여부         |
|:---:| --------------------------------------------------------------------------------------------------- | ---------------------- | ------------- |
| —   | (각주 없음)                                                                                             | **11, 17, 19**         | **필수. 시험 대상** |
| (1) | "This link configuration is defined for future use. It is **optional and not subject to testing**." | 12, 13, 14, 15, 16, 18 | 선택. 시험 대상 아님  |
| (4) | "**Not for communications**, reserved for future radio navigation."                                 | 35, 36, 37, 38         | **통신용이 아님**   |

즉 **35/36/37/38은 애초에 통신 링크가 아니다.** 장래 전파항법(ranging)용으로 예약된 것이며,
§A4-4.8.6도 *"The ranging channel (RC) is reserved for future radio navigation applications"* 라고
따로 규정한다. 나머지 6개(12·13·14·15·16·18)는 "장래 사용, 선택 사항, 시험 대상 아님"이다.

**따라서 "모든 Link ID를 구현"하는 것은 표준이 요구하지도, 권하지도 않는다.** 11/17/19가
통신을 위해 실제로 요구되는 전부다. 세 개는 대역폭별로 하나씩 대응한다.

| Link ID | 대역폭     | 변조       | 성격                       |
|:-------:| -------:| -------- | ------------------------ |
| 11      | 25 kHz  | π/4-QPSK | 최소 구성. **BBSC 필수**(§6.3) |
| 17      | 100 kHz | π/4-QPSK | 광대역 기본                   |
| 19      | 100 kHz | 16-QAM   | 최고 처리율                   |

### 6.1 실제 환경에서 Link ID는 어디서 정해지는가

응용 계층이 고르는 값이 **아니다.** 표준이 정하는 결정 경로는 다음과 같다.

**(1) 물리 채널이 후보 집합을 제한한다**

> "The Physical Channel may not be changed dynamically according to the environment, thus
> **the physical channel in use determines the Link ID's available for selection** as determined by
> the physical channel bandwidth." — §A4-4.7

25 kHz 채널이면 11/12/13, 50 kHz면 14/15/16, 100 kHz면 17/18/19 중에서만 고를 수 있다.
대역폭이 먼저 정해지고 그 안에서 MCS를 고르는 구조다.

**(2) 제어국이 채널 품질을 보고 지시한다**

> "The control station may use the reported **CQI** as well as measure the channel quality of the
> received ship signal and **request the ship to adjust the Link ID** to maximize throughput."
> — §A4-4.7

**(3) 구체적으로는 Message 4(Resource allocation)의 Link ID 필드로 전달된다**

- §A4-4.8.4 (Data channel): *"The Link ID used in the DC is defined by **message 4, Resource
  allocation** and may be changed with **message 13, ACK/NACK**."*
- §A4-4.8.5 (Data signalling channel): 동일
- Type 4 메시지의 Link ID 필드(1 byte): *"The link ID that should be used in the TDMA channel.
  This will apply to Messages 74, 75, 76 and 13."* Type 4에는 CQI 필드도 함께 실린다.

**(4) 선박 간 통신에서는 자원을 할당하는 선박이 정한다**

> "During ship-to-ship communication outside of control station service area, the Link ID can be
> **controlled by the resource allocating ship**." — §A4-4.7

**(5) 선택 알고리즘 자체는 표준이 비워 두었다**

> "The details for rate adaptation are not defined in this document.
> **The link adaption mechanism is intentionally undefined.**" — §A4-4.7

**(6) 수신 측은 버스트 안의 Link ID 필드로 알아낸다**

Link ID는 6 bit를 (32,6) 블록부호로 32 bit로 만들어 training sequence 뒤에 π/4-QPSK로 싣는다
(§A2-1.2.9). 수신기는 이것을 먼저 복호해야 **몇 bit를 모을지와 어떤 MCS로 복조할지**를 안다.

정리하면 Link ID는 **제어국 또는 자원 할당 선박이 채널 품질을 보고 정해 Message 4로 통보하고,
버스트 머리에 실려 수신기에 전달되는 링크·물리 계층의 값**이다. MASS/ROC 응용은 관여하지 않는다.

### 6.2 우리 구현에 주는 영향

M.2092-2 Table 8의 실제 값이다.

| Link ID | PL format     | 대역폭     | 변조       | FEC | Packet    | 응용 여유 (Type 92) |
|:-------:| ------------- | -------:| -------- |:---:| ---------:| ---------------:|
| **11**  | TER-MCS-1.25  | 25 kHz  | π/4-QPSK | 1/2 | **54 B**  | **37 B**        |
| **17**  | TER-MCS-1.100 | 100 kHz | π/4-QPSK | 1/2 | **234 B** | **217 B**       |
| **19**  | TER-MCS-5.100 | 100 kHz | 16-QAM   | 3/4 | **702 B** | **685 B**       |

> 응용 여유 = Packet − CRC-32(4 B) − Type 92 헤더(13 B)

### 6.3 메시지별 수용 여부

| 메시지                    | 크기      | Link 11 (37 B) | Link 17 (217 B) | Link 19 (685 B) |
| ---------------------- | -------:|:--------------:|:---------------:|:---------------:|
| Position Report        | 17 B    | ✓              | ✓               | ✓               |
| Route (waypoint 1)     | 17.5 B  | ✓              | ✓               | ✓               |
| Route (waypoint 3)     | 31.3 B  | ✓              | ✓               | ✓               |
| **Route (waypoint 4)** | 38.1 B  | **✗**          | ✓               | ✓               |
| Route (waypoint 16)    | 120.6 B | ✗              | ✓               | ✓               |

### 6.4 결론

- **Link ID 11이 제약을 지배한다.** 17·19에서는 waypoint 16개도 여유롭다.
- **메시지 포맷은 Link ID에 따라 바뀌지 않는다.** Link 11에서는 waypoint 수를 3개 이하로
  제한하면 된다. 포맷 자체를 분기시킬 필요가 없다는 점이 이 설계의 장점이다.
- Link ID 11은 어차피 필수다 — 표준이 **"BBSC의 모든 트랜잭션은 Link ID 11을 사용해야 한다"**
  (§A4-4.8.1)고 규정한다.
- **Link ID 선택은 링크 계층 관심사이며 응용 계층 설계와 무관하다**(§6.1).
- **모든 Link ID를 구현할 필요는 없다.** 통신용으로 요구되는 것은 11/17/19뿐이다(§6.0).

---

## 7. 보조 메시지

### 7.1 응답 — Application ACK

ITU-R M.1371 Annex 5의 **IFM FI=5 (Application acknowledgement)** 가 선례다. SN.1/Circ.289 §1.3이
국제 사용을 권고하는 시스템 메시지 4종 중 하나이며, 링크 계층 ACK와 명확히 구분된다.

> "…system acknowledgement on the VHF Data Link (VDL) by the receiving AIS station.
> This acknowledgement **should not be confused with a user acknowledgment**." — SN.1/Circ.289

즉 **VDE Type 92 ACK(전파 도달)** 와 **응용 ACK(명령 수락)** 는 서로 다른 층위이며, 둘 다 두는
것이 표준 관행이다.

> **프로젝트 정의 필드** — FI=5의 정확한 비트 구조는 ITU-R M.1371 Annex 5 소관이며 본 문서
> 작성 시점에 원문 미확보. 아래는 Linkage ID 참조 개념만 차용해 정의한 것이다.

| 필드                 | 비트  | 값                                                  |
| ------------------ | ---:| -------------------------------------------------- |
| Message Linkage ID | 10  | 응답 대상 메시지의 Linkage ID                              |
| Status             | 3   | 0 = 수신함, 1 = 수락, 2 = 거부, 3 = 실행 완료, 4–7 예약         |
| Reason             | 3   | 0 = 없음, 1 = 항로 부적합, 2 = 유효기간 경과, 3 = 권한 없음, 4–7 예약 |

크기 = VPFI(16) + Message ID(8) + 16 = **40 bit = 5 byte**

### 7.2 텍스트 — IFM FI=29/30

`Message Linkage ID`로 앞선 메시지와 연결되는 자유 텍스트(6-bit ASCII). **우선순위 낮음** —
정상 동작 구현 이후 추가.

### 7.3 heartbeat는 만들지 않는다

AIS 위치보고는 원래 **주기적으로 반복 송신**되는 메시지다. 따라서 **위치보고의 주기적 도착 자체가
heartbeat 역할**을 한다. 별도 메시지를 만들면 기능이 중복된다.

→ **결정**: 위치보고 수신 간격 초과를 통신 두절로 판정한다.

### 7.4 IEC 61162-1 HTC/HTD의 위치

`mass_roc_background.md` §3.3에서 ROC→MASS 명령 근거로 제시했으나 **적용 층위가 다르다.**
HTC/HTD/ROR는 **선내 자동조타 장치와 항법 장비 사이의 직렬 인터페이스** 문장이며 VHF 무선
구간용이 아니다.

→ 무선 구간 항로 지시는 **IFM FI=28**이 담당한다. HTC/HTD는 "MASS가 수신한 항로 명령을 실제
조타 장치에 전달하는 단계"의 참고 자료로만 남긴다(본 프로젝트 범위 밖).

---

## 8. 제안 메시지 세트

| #   | 메시지                   | 방향                           | 근거                            | VDE Type    | 크기      |
|:---:| --------------------- | ---------------------------- | ----------------------------- |:-----------:| -------:|
| 1   | **Position Report**   | MASS → ROC / **MASS**        | AIS Msg 1/2/3 + Autonomy Mode | 93 (ACK 불요) | 17 B    |
| 2   | **Route Instruction** | ROC → MASS / **MASS → MASS** | IFM FI=28                     | 92 (ACK 요구) | 17.5 B~ |
| 3   | **Application ACK**   | 수신자 → 발신자                    | IFM FI=5 (개념)                 | 93          | 5 B     |

Type 매핑 근거(`mass_roc_background.md` §3.2): 주기적 브로드캐스트는 Type 93, 지정 수신자에게
ACK를 요구하는 명령은 Type 92.

### 8.1 payload 공통 구조

```text
[ VPFI (16 bit) | Message ID (8 bit) | 메시지별 본문 ]
```

- **VPFI** — IALA R1027에 따라 16 bit를 선두에 둔다. 값은 시험용 임의값(§9.2).
- **Message ID** — R1027이 허용하는 선택적 8–16 bit message identifier. 1/2/3으로 구분.

### 8.2 정상 동작 시나리오

```text
MASS                                              ROC
 │                                                 │
 │──[1] Position Report (주기적, Type 93)──────────▶│  위치·침로·속력·모드 갱신
 │──[1] Position Report ───────────────────────────▶│
 │                                                 │
 │◀─[2] Route Instruction (Type 92) ───────────────│  Linkage ID + waypoints + 유효기간
 │                                                 │
 │──[3] Application ACK (Linkage ID 참조) ─────────▶│  수락 확인
 │                                                 │
 │  (MASS가 항로 반영 → 이후 위치보고에 침로 변화)      │
 │──[1] Position Report (변경된 COG) ──────────────▶│
```

이 흐름이 `mass_roc_background.md` §2.7의 관찰 도구가 보여줄 기본 형태이며, 후속 공격 실험의
**정상 대조군**이 된다.

---

## 9. 결정 사항

### 9.1 확정된 것

- **설계 방식**: AIS/IFM의 **응용 계층 필드 구조와 부호화를 차용**하고, **전송 래퍼는 취하지
  않는다**(송신원·수신처는 VDE 링크 계층에 위임). 대안 5종 비교 결과(§2).
- **채널**: 메시지는 **VDE 채널**로 전송된다. AIS 채널을 쓰지 않는다. 차용하는 것은 필드 구조다(§2.6).
- **위치보고**: AIS Message 1/2/3 Common Navigation Block 108 bit + Autonomy Mode 3 bit(§3).
- **항로 지시**: IFM DAC=001/FI=28. 고정부 61 bit + waypoint 55 bit/개(§4).
- **좌표**: **1/10,000 분 정수**(경도 28 bit, 위도 27 bit). AIS·IFM이 동일하게 사용.
- **"값 없음"**: AIS의 필드별 sentinel 값을 그대로 사용(SOG=1023, COG=3600, Heading=511 등).
- **Autonomy Mode는 신규 정의** — 표준에 대응 필드가 없다. Navigation Status에 병합하지 않는다(§3.2).
- **MASS↔MASS를 포함**한다. 표준이 정의한 1급 시나리오이며 메시지 세트 변경은 불필요(§5).
- **Link ID는 응용 설계에 영향을 주지 않는다.** Link 11에서 waypoint 3개 이하로 제한하면 된다(§6).
- **heartbeat 없음**, **응용 ACK와 링크 ACK는 별개 층위**(§7).

### 9.2 남은 결정 사항 (모두 낮은 우선순위 — 기본값으로 진행 가능)

1. **Navigation Status 값** — 자율운항 표현이 필요하면 `13(reserved for future use)`을 시험용으로
   쓸 수 있으나, **§3.2의 Autonomy Mode 필드로 해결했으므로 표준값을 그대로 사용**하기를 권고.
2. **Route Type 사용 범위** — RC 상태에서는 `1(mandatory)`, MON 상태에서는 `2(recommended)`를
   기본으로 제안.
3. **위치보고 주기** — AIS는 속력·기동에 따라 가변. 시험에서는 고정 주기(예: 2초)로 단순화 제안.
4. **VPFI 시험값 / Message ID 배정** — 임의값으로 진행 가능.

---

## 참고문헌

### 로컬 확보 자료

- **ITU-R M.2092-2** (2026-02) — `references/R-REC-M.2092-2-202602-I!!PDF-E.pdf`
  - §A1-2.3 (채널 배정), §A3-7.3 + Table 25 (Encapsulated AIS), **Table 8 (VDE-TER Link ID)**,
    §A4-4.3 (제어국 서비스 영역), §A4-4.7 (링크 적응 미정의), §A4-4.8.1 (BBSC는 Link ID 11),
    §A4-4.28 (ship-to-ship 시퀀스)
- **IMO SN.1/Circ.289** (2010-06-02): *Guidance on the use of AIS Application-Specific Messages*
  — `circ289.pdf` (스크래치패드)
  - Annex Table 13.2 (Route information – addressed, FI=28), Table 13.3 (Waypoints),
    §1.2 (IFM 목록), §1.3 (시스템 메시지 FI 2/3/4/5)
  - <https://vislab-ccom.unh.edu/~schwehr/papers/2010-IMO-SN.1-Circ.289.pdf>
- **DNVGL-CG-0264** (2018-09): *Autonomous and remotely operated ships* — `dnv.pdf`
  - Sec.6 §4–5 (원격 상황인식·감독). 개념 요건이며 메시지 포맷 미정의
  - <https://www.smashroadmap.com/files/dnvgl-cg-.pdf>

### 외부 참조

- [AIVDM/AIVDO protocol decoding (gpsd)](https://gpsd.gitlab.io/gpsd/AIVDM.html) — AIS Message
  1/2/3 비트 레벨 필드 정의. §3.1의 근거
- [AIS Navigational Status 값 목록 (MarineTraffic)](https://help.marinetraffic.com/hc/en-us/articles/203990998-What-is-the-significance-of-the-AIS-Navigational-Status-Values) — §1.1의 근거
- [IALA ASM Collection (e-navigation.nl)](https://www.e-navigation.nl/asm) — 실재 ASM 목록·DAC/FI 현황
- [IALA Guideline 1095 — Harmonised implementation of ASM](https://www.e-navigation.nl/sites/default/files/1095-ed.1-on-harmonized-implementation-of-application-specific-messages-asm_may2013.pdf)
- [IEC 63173-2:2022 (SECOM)](https://webstore.iec.ch/en/publication/64543) — IP/REST 기반. 본 링크 미적용(§1.1)
- [IMO: MASS Code 채택](https://www.imo.org/en/mediacentre/pressbriefings/pages/imo-adopts-mass-code.aspx)

### 미확보 (후속 확인 필요)

- **ITU-R M.1371** Annex 5 — IFM FI=5(Application acknowledgement)의 정확한 비트 구조.
  §7.1의 ACK 필드는 이를 확보하면 표준값으로 교체할 것.

### 관련 문서

- `docs/mass_roc_background.md` — 계층 구조, PI, 위협 모델, 상태 모델
- (구) `VDETER_Link/no_error_correction/docs/requirements.md` — 초기 링크 계층 명세 (현재는 `VDETER/`로 대체)
