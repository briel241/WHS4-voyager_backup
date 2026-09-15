# MASS·ROC·Presentation Interface 배경지식

> MASS와 ROC 간 데이터 송수신 구현에 앞서, 응용 계층(Application)과 Presentation Interface(PI)의
> 표준 근거를 정리한 문서입니다. VDE-TER 링크·물리 계층은 `VDETER/`(팀원 담당)를 참조합니다.

| 항목    | 내용                                                     |
| ----- | ------------------------------------------------------ |
| 문서 목적 | 응용 계층 구현에 필요한 표준 배경 정리 및 설계 제약 도출                      |
| 담당 범위 | MASS/ROC 행위자 구현, 응용 메시지 포맷, PI 어댑터                     |
| 인접 범위 | VDE-TER Link/Physical Layer (별도 팀원 담당, `VDETER/`) |
| 1차 근거 | ITU-R M.2092-2, IALA G1117 Ed 4.0 (로컬 PDF 보유)          |
| 2차 근거 | IMO MASS Code, IEC 61162 계열, IALA R1027 (웹 조사)         |

---

## 1. 계층 구조 — 무엇을 누가 담당하는가

VDE-TER는 OSI 1~4계층(물리·링크·네트워크·전송)을 사용한다(M.2092-2 §A2-1.1, Fig. 3).
단 VDE-TER에서는 **전송 계층의 책임이 링크 계층으로 이관**되어 있다.

> "The reliable transmission of data segments, segmentation, acknowledgement and multiplexing
> requires close integration with the link layer. The responsibility of the transport layer was
> therefore moved to the link layer." — M.2092-2 §A4-7

따라서 실제 처리 사슬은 다음과 같다.

```text
[MASS / ROC 응용 로직]              ← 무엇을 말할지 결정 (본 프로젝트 담당)
        │  위치·침로·속력 보고 / 항로·속력 명령
        ▼
[Presentation Interface (PI)]       ← 응용 데이터 ↔ payload bytes (본 프로젝트 담당)
        │  payload bytes (+ VPFI)
        ▼
[Link Layer]                        ← Type/Length/Source/Dest/Session/Retrans 봉투
        │  + Packet 조립 + Padding(Type 81) + CRC-32     (팀원 담당)
        ▼
[Physical Layer]                    ← FEC · 변조 · RF (USRP B210)   (팀원 담당)
```

**핵심 구분**: Destination ID·ACK 요구 여부·Broadcast 여부·Length·Session ID는 모두
**링크 계층이 씌우는 봉투(header)** 이고, MASS/ROC가 실제로 주고받는 내용은
**그 봉투 안의 Payload** 다. 본 프로젝트는 Payload의 내부 구조와 그것을 만들어내는 주체를 담당한다.

---

## 2. MASS — Maritime Autonomous Surface Ship

### 2.1 IMO 자율화 4단계

IMO 해사안전위원회(MSC)가 정의한 자율화 정도(Degree of autonomy)는 다음 4단계다.

| Degree | 정의                   | 선원 승선 | 원격 제어 | ROC 링크 중요도        |
|:------:| -------------------- |:-----:|:-----:| ----------------- |
| 1      | 자동화 프로세스 및 의사결정 지원   | O     | X     | 낮음 (조언·모니터링)      |
| 2      | 원격 제어 선박, **선원 승선**  | O     | O     | 중 (사람이 최종 개입 가능)  |
| 3      | 원격 제어 선박, **선원 미승선** | X     | O     | **높음 (링크가 곧 조타)** |
| 4      | 완전 자율 (스스로 판단·실행)    | X     | 감독    | 중 (감독·개입 위주)      |

### 2.2 MASS Code 현황

- IMO는 2026년 5월 MSC 111에서 **비강제(non-mandatory) MASS Code**를 결의 MSC.595(111)로 채택했고,
  **2026년 7월 1일 발효**했다.
- 적용 범위는 SOLAS Ch. I 적용 화물선(통상 500 GT 초과 국제항해 선박)이며, 그 미만 선박에도
  가능한 범위에서 적용하도록 권고한다.
- 강제 MASS Code는 경험축적기간(EBP)을 거쳐 2030년 채택·2032년 발효를 목표로 개발 중이다.

### 2.3 Degree만으로 구분할 때의 한계

Degree를 실험 축으로 삼으면 다음 문제가 생긴다.

- **Degree는 선박의 정적 속성**이다. 형식승인·설계 단계에서 부여되는 등급이며, 항해 중에
  바뀌지 않는다.
- 반면 **공격의 영향 크기를 실제로 좌우하는 것은 그 순간 링크가 무엇을 나르고 있는가**이다.
  같은 Degree 3 선박이라도 ROC가 실제로 조타 중일 때와 본선이 자율 항행 중일 때, 위조된
  명령이 미치는 영향은 전혀 다르다.
- 구현 관점에서도 Degree는 코드에 표현할 대상이 아니다. `MassAgent`가 가질 수 있는 것은
  등급이 아니라 **현재 운항 모드**다.

### 2.4 CITesting — 차용할 위협 모델과 방법론

**출처**: Son, Kim, Oh, Park, Kim. *"CITesting: Systematic Testing of Context Integrity
Violations in LTE Core Networks."* ACM CCS '25, Taipei. KAIST·경희대. (`references/CITesting.pdf`)

해상 분야 논문이 아니라 **LTE 코어망 보안** 논문이다. 그러나 이 프로젝트에 두 가지를 그대로
제공한다 — **위협 모델의 정의(CIV)** 와 **상태 기반 실험 설계 방법론**이다.

#### 2.4.1 Context Integrity Violation (CIV)

논문이 정의하는 핵심 개념이다.

> "Context integrity refers to the property that a mobile core network's internal state for each
> subscriber is modified **solely by authenticated and authorized signaling messages**. A context
> integrity violation (CIV) occurs when an **unauthenticated or improperly authenticated message
> alters a legitimate subscriber's context**, enabling denial of service, IMSI exposure, or presence
> detection." — CITesting §3.1

이를 MASS/ROC로 옮기면 **본 프로젝트의 위협 모델이 그대로 정의된다.**

| CITesting (LTE)             | 본 프로젝트 (VDE-TER)                                |
| --------------------------- | ----------------------------------------------- |
| 악성 UE                       | 인증되지 않은 송신국 (MMSI 사칭)                           |
| 피해자 UE의 subscriber context  | **MASS의 내부 상태** (위치·침로·항로·제어권) 또는 **ROC의 상황인식** |
| 코어망 (MME)                   | 상대 노드 (MASS 또는 ROC)                             |
| IMSI/GUTI 획득 (수동 공격)        | **MMSI 획득 — AIS 브로드캐스트로 공개**                    |
| 같은 MME 지역 내 위치              | VHF 도달 범위 내 위치                                  |
| CIV 결과: DoS, IMSI 노출, 존재 탐지 | 항로 이탈, 제어권 탈취, 통신 두절, 위치 노출                     |

구상도의 *"신분 사칭 → MASS에 어떤 영향을 주는가"* 가 곧 CIV의 해상 버전이다.

#### 2.4.2 결정적 근거 — 3GPP에는 있고 VDE-TER에는 없는 규정

논문이 지적하는 표준 발전 과정이 본 프로젝트의 **제안 정당화 근거**가 된다.

- 초기 3GPP 규격은 컨텍스트 무결성을 **암묵적으로 가정**했을 뿐 명시적으로 강제하지 않았고,
  실제 배포에서 구현 실패가 드러났다.
- 이에 **Release 17**은 인증 실패의 명확한 거부를 요구했고, **Release 18**은 다음과 같이
  문구를 강화했다.

> "When the authentication procedure is not successful, the network **shall maintain, if any, the
> EMM-context and EPS security context unchanged**." — 3GPP LTE NAS §4.4.4.3 (CITesting §3.1 재인용)

**VDE-TER 표준(M.2092-2)에는 이에 상응하는 규정이 없다.** 인증 자체가 정의되어 있지 않으므로
"인증 실패 시 상태를 변경하지 말라"는 요구도 존재할 수 없다. 이는 구상도의
*"키 분배·인증을 어떻게 할지 구체적인 표준이 없음 → 우리가 제안한다"* 와 정확히 맞물리며,
**3GPP가 이미 겪은 문제를 VDE-TER가 아직 겪지 않았다**는 형태로 제안을 정당화할 수 있다.

참고로 논문은 Release 18조차 불충분하다고 보고 다음 개정을 제안한다 — 명시적 인증 실패뿐 아니라
**"인증이 없는 불완전·미완료 절차의 어느 단계에서든"** 상태를 유지해야 한다는 것이다(§3.1).
우리 설계에도 같은 원칙을 적용해야 한다.

#### 2.4.3 상태 축 — 2개의 직교하는 축

논문은 UE 상태를 **하나가 아니라 두 개의 직교 축**으로 본다(§2.2).

| 축       | 의미                                        | 값                         |
| ------- | ----------------------------------------- | ------------------------- |
| **EMM** | **등록(registration) 상태** — 관계가 수립되어 있는가    | Registered / Deregistered |
| **ECM** | **연결(connectivity) 상태** — 지금 실제로 연결되어 있는가 | Connected / Idle          |

조합하여 **3개의 대표 상태**를 정의한다.

| 상태          | 조합                     | 의미                                       |
| ----------- | ---------------------- | ---------------------------------------- |
| **CONN.**   | Registered + Connected | 등록·활성 연결 모두 있음 (완전 운용 상태)                |
| **IDLE**    | Registered + Idle      | 등록은 유지, 활성 연결 없음. Service Request로 재개 가능 |
| **DEREGI.** | Deregistered + Idle    | 등록도 연결도 없음                               |

#### 2.4.4 상태 의존 취약점 — 이 논문의 핵심 발견

> "A vulnerability that is exploitable in one victim UE state **may not appear in others**, or may
> manifest differently under varying state conditions." — CITesting §4.1 (C2)

실제 평가에서 이것이 실증되었다. Amarisoft 구현에서 어떤 CIV는 **DEREGI.와 CONN. 상태에서만**
나타나고 IDLE에서는 나타나지 않았으며, 다른 CIV는 **CONN.과 IDLE에서만** 나타나고 DEREGI.에서는
나타나지 않았다(§평가). 절차 탐색 수 자체도 상태별로 크게 달랐다(DEREGI. 678·630 vs
IDLE/CONN. 1,062·1,206).

**→ 따라서 공격 실험은 반드시 모든 상태에서 반복해야 한다.** 한 상태에서 안전하다는 결과가
다른 상태의 안전을 보장하지 않는다.

#### 2.4.5 상태 불일치 — 가장 위험한 결과

논문이 보고하는 결과 중 하나가 **Network-side Mismatch**다. 피해자 UE는 자신을 CONN.으로
여기는데 네트워크는 DEREGI.로 간주하는, **양측의 상태 인식이 어긋난 상황**이다.

MASS/ROC로 옮기면 이것이 가장 위험한 시나리오가 된다 — **MASS는 제어권이 ROC에 있다고 믿고
명령을 기다리는데, ROC는 제어권이 본선에 있다고 믿고 명령을 보내지 않는 상태.** 어느 쪽도
조타하지 않는 선박이 된다. 이 시나리오는 §2.6의 전환 구간 공격으로 직접 이어진다.

### 2.5 MASS/ROC 상태 모델 — CITesting 구조의 이식

CITesting의 **2축 구조**를 그대로 가져온다. 제어권을 단일 축(있음/없음)으로 보지 않고,
**세션 축과 제어권 축으로 분리**하는 것이 핵심이다.

| 축          | LTE 대응 | 의미                       | 값                  |
| ---------- | ------ | ------------------------ | ------------------ |
| **세션(S)**  | EMM    | MASS↔ROC 제어 세션이 수립되어 있는가 | Established / None |
| **제어권(C)** | ECM    | 지금 제어권이 ROC에 있는가         | Remote / Local     |

조합한 대표 상태:

| 상태                      | 조합             | MASS의 행동        | 링크의 역할               |
| ----------------------- | -------------- | --------------- | -------------------- |
| **RC** (Remote Control) | 세션 O + 제어권 ROC | ROC 명령을 실행      | **명령 전달 — 링크가 곧 조타** |
| **MON** (Monitored)     | 세션 O + 제어권 본선  | 자율 항행, ROC는 감독만 | 상태 보고 (명령 없음)        |
| **AUT** (Autonomous)    | 세션 X           | 자율 항행, ROC 미연결  | 없음 (또는 브로드캐스트만)      |

**MON을 별도 상태로 두는 이유**가 CITesting에서 IDLE을 별도로 두는 이유와 같다. "세션은
살아 있으나 제어권은 없는" 상태에서만 나타나는 취약점이 있을 수 있다 — 예컨대 세션 문맥
(키·시퀀스 번호)은 유지되므로 재전송 공격의 표적이 되지만, 명령은 받지 않으므로 위조 명령의
직접 효과는 없다. 이 조합은 RC나 AUT 어느 쪽으로도 대체되지 않는다.

### 2.6 상태 전환과 공격 표면

```text
                  세션 수립 (Session Establish)
        ┌──────────────────────────────────────────┐
        │                                          ▼
   ┌─────────┐                              ┌─────────────┐
   │   AUT   │                              │     MON     │
   │ 세션 없음 │                              │ 세션 O·제어권 본선│
   └─────────┘                              └─────────────┘
        ▲                                      │        ▲
        │  세션 해제 / 링크 두절                    │        │
        │                          제어권 이양 요청 │        │ 제어권 반환
        │                                      ▼        │
        │                                  ┌─────────────┐
        └──────────────────────────────────│     RC      │
             링크 두절 시 fallback            │ 세션 O·제어권 ROC│
                                           └─────────────┘
```

CITesting이 "절차 연쇄(procedure chain)"를 탐색 대상으로 삼는 것처럼, **상태 자체보다
전환 절차가 더 큰 공격 표면**이다.

| 공격 표면                  | 대상 상태/전환    | 예상 결과 (CIV)        |
| ---------------------- | ----------- | ------------------ |
| 위조 제어 명령               | **RC**      | 항로 이탈 — 영향 최대      |
| 위조 텔레메트리               | MON, RC     | ROC 상황인식 오염 → 오판   |
| **위조 제어권 이양 요청**       | MON → RC 전환 | **제어권 탈취**         |
| **위조 제어권 반환**          | RC → MON 전환 | 조타 주체 상실           |
| 이양 메시지 **재전송(replay)** | 전환 구간       | 상태 되돌리기, 교착        |
| 위조 세션 해제               | 모든 상태 → AUT | 통신 두절 (DoS)        |
| 응답 차단·지연               | 전환 구간       | **상태 불일치**(§2.4.5) |

### 2.7 실험 설계 — CITesting 방법론의 이식

논문의 실험 구조를 그대로 옮길 수 있다.

| CITesting 요소                                            | 본 프로젝트 대응                                   |
| ------------------------------------------------------- | ------------------------------------------- |
| **Dual-UE** (Victim UE + Tester UE)                     | **Dual-node**: 정상 MASS/ROC 쌍 + 공격자 국        |
| **Victim UE Initialization** (context reset → 상태 수립)    | 각 시험 전 MASS/ROC를 지정 상태(RC/MON/AUT)로 초기화     |
| **Behavioral oracle** (피해자 행동 vs 정상 행동 비교로 내부 상태 변화 탐지) | **관찰 도구** — 공격받은 MASS의 상태 시계열 vs 정상 MASS 비교 |
| **모든 UE 상태에서 반복**                                       | 모든 제어권 상태에서 각 공격 반복 (§2.4.4)                |
| 절차 연쇄 탐색                                                | 전환 절차의 단계별 개입 (§2.6)                        |

**behavioral oracle이 특히 중요하다.** 논문은 블랙박스 코어망의 내부 컨텍스트를 직접 볼 수
없어 행동 비교로 우회했다(§4.1 C3). 우리는 MASS 구현체를 직접 만들므로 내부 상태를 볼 수
있지만, **"정상 대조군과 비교한다"는 구조 자체는 유지해야 한다.** 구상도의 *"어떻게 변하는지
확인하는 도구"* 가 바로 이 oracle 역할이며, **공격군과 대조군을 나란히 보여주는 것**이
결과 제시의 기본 형태가 된다.

### 2.8 Degree의 위치 — 2차 축

제어권 상태를 1차 축, Degree(승선 선원 유무)를 2차 축으로 둔다.

|                 | **Degree 2** (선원 승선)                | **Degree 3** (선원 미승선)            |
| --------------- | ----------------------------------- | -------------------------------- |
| **RC** (ROC 제어) | 위조 명령 → 승선 선원이 감지·정정 가능 → **영향 제한** | 위조 명령 → **그대로 실행** → **영향 최대**   |
| **MON** (감독)    | 위조 텔레메트리 → ROC 오판, 단 본선이 검증 가능      | 위조 텔레메트리 → **ROC 오판을 정정할 주체 없음** |

→ **주 시나리오는 `Degree 3 × RC`**(영향 최대), **대조군은 `Degree 2 × RC`**(사람 개입 존재)로
두기를 권고한다. 구상도의 *"degree별로 공격에 의해 받는 영향의 크기 변화"* 는 이 표의
**열 방향 비교**로 실증한다.

> Degree는 실험 축이지 구현 대상이 아니다. 코드에는 제어권 상태만 존재하며, Degree는
> "그 상태에서 사람이 개입하는가"를 시나리오 수준에서 결정한다.

---

## 3. ROC — Remote Operation Centre

ROC는 **MASS를 원격으로 제어·운용하는 육상 기반 시설**이다. Degree 2~3에서 필수이며,
Degree 4에서도 감독(oversight) 주체로 남는다.

### 3.1 ROC↔MASS 데이터 교환의 성격

ROC 관련 문헌(§참고문헌 "외부 참조 — ROC")이 공통으로 지적하는 요구사항:

- **양방향(bidirectional)**: 선박→육상 상태 보고와 육상→선박 제어 명령이 함께 흐른다.
- **보안성·확장성·이중화(redundant)**: 단일 링크 장애가 곧 제어 상실로 이어지므로 다중화가 요구된다.
- **지연(latency)과 연결 신뢰성**이 핵심 제약이다. 특히 연결 품질이 낮은 해역에서 문제가 된다.
- 원격 운용자의 **상황인식(situational awareness)** 확보가 설계의 중심 목표다.

### 3.2 데이터 흐름 분류

> **출처 구분 (중요)**
> 아래 표는 **표준에서 그대로 인용한 것이 아니라, 세 층위의 근거를 조합해 본 프로젝트가 구성한
> 설계안**이다. 각 열의 근거 강도가 다르므로 구분해 읽어야 한다.
>
> | 열               | 근거 강도             | 출처                               |
> | --------------- | ----------------- | -------------------------------- |
> | **내용(항목)**      | 강 — 표준 문장 존재      | IEC 61162-1 문장 정의 (§5.2, §3.3)   |
> | **대응 VDE Type** | 강 — 표준 정의         | M.2092-2 Table 35/40/41 (§5, §1) |
> | **방향·성격 분류**    | 중 — 문헌 기반 추론      | ROC 문헌의 양방향·상황인식 요구 (§3.1)       |
> | **주기·크기**       | 약 — **본 프로젝트 가정** | 미확정. §7.2 결정 항목                  |

| 방향         | 성격        | 내용                                 | 대응 VDE Type | 근거                        |
| ---------- | --------- | ---------------------------------- |:-----------:| ------------------------- |
| MASS → ROC | 주기적 텔레메트리 | 위치, SOG, COG, heading, 선회율         | 93 (ACK 불요) | RMC/GGA/VTG/HDT/ROT 문장    |
| MASS → ROC | 이벤트       | 알람, 기관 상태 변화, 모드 전환 보고             | 92 (ACK 요구) | ALR/ALA 계열, HTD           |
| ROC → MASS | 제어 명령     | 항로/waypoint, 속력 설정, 침로·타각, 운항모드 전환 | 92 (ACK 요구) | **HTC**, ROR (§3.3)       |
| 양방향        | 생존 확인     | heartbeat                          | 93          | **표준 문장 없음 — 프로젝트 자체 정의** |

**Type 매핑의 표준 근거** — 이 부분만은 추론이 아니라 M.2092-2의 정의에서 직접 따라 나온다.

- Type 92 = *"Short data message. **ACK is required**."* (Table 35) → 명령·이벤트처럼 도달 보장이
  필요한 흐름에 대응.
- Type 93 = *"Short data message that does not require an ACK. **May be used for broadcasting**."*
  (Table 35) → 주기적 텔레메트리·heartbeat에 대응.
- Type 92는 *"Destination MMSI **may not be set to zero** (broadcast address)"* (Table 40)로
  브로드캐스트가 금지되므로, 지정 수신자가 있는 명령 흐름에만 쓸 수 있다.
- Type 92의 Retransmission no 필드는 *"Value of **255 indicates no ACK is requested**"* (Table 40)
  이므로, Type 92를 쓰면서 ACK를 끄는 것도 표준상 가능하다.

> **정정 (`mass_roc_message_set.md` §4.3)**: 위 표의 heartbeat 행은 **불필요한 것으로 판명**되었다.
> AIS 위치보고가 원래 주기적으로 반복 송신되는 메시지이므로 **위치보고의 주기적 도착 자체가
> heartbeat 역할**을 한다. 별도 heartbeat 메시지는 만들지 않는다.

> **정정 (`mass_roc_message_set.md` §4.4)**: "제어 명령"의 근거로 아래 §3.3에 적은 HTC/ROR는
> **선내 자동조타 인터페이스 문장**이며 VHF 무선 구간용이 아니다. 무선 구간의 육상→선박 항로
> 지시는 **IMO SN.1/Circ.289의 IFM DAC=001/FI=28 "Route information (addressed)"** 가 담당한다.

### 3.3 ROC→MASS 제어 명령의 표준 문장

조사 결과 **원격 조타 명령에 해당하는 IEC 61162-1 문장이 이미 존재한다.** 이는 §3.2 "제어 명령"
행의 근거이자, 응용 메시지 필드 설계의 출발점이 된다.

| Formatter | 내용                                              | 방향         |
| --------- | ----------------------------------------------- | ---------- |
| **HTC**   | Heading/track control **command** — 침로·항로 제어 명령 | ROC → MASS |
| **HTD**   | Heading/track control **data** — 위 명령에 대한 상태·응답 | MASS → ROC |
| **ROR**   | Rudder order status — 타각 지령 상태                  | 양방향        |

HTC/HTD는 IEC 61162-1에서 자동조타·항로제어 장치의 명령/응답 쌍으로 정의된 문장이다.
**HTC(명령) ↔ HTD(응답)의 쌍 구조**는 Type 92(ACK 요구)로 명령을 보내고 응답을 받는 설계와
개념적으로 대응한다.

> **적용 층위 정정** (`mass_roc_message_set.md` §4.4): HTC/HTD/ROR는 **선내 직렬 인터페이스**
> 문장이다. 항법 장비와 자동조타 장치를 잇는 용도이며 VHF 무선 구간용으로 설계되지 않았다.
> **본 프로젝트의 무선 구간 항로 지시는 IFM FI=28을 차용한다.** HTC/HTD는 "MASS가 수신한 항로
> 명령을 실제 조타 장치에 전달하는 단계"의 참고 자료로만 남긴다(범위 밖).

> 참고: IEC 61162-1 2007년판 개정 이력에 따르면 **ROR(rudder order)** 과 **THS(heading)** 는
> 해당 판에서 신규 추가된 문장이다. 자동조타 관련 문장군이 표준에 편입된 시점이 비교적 최근임을
> 보여준다.

---

## 4. Presentation Interface (PI)

### 4.1 표준이 정의하는 것

M.2092-2가 PI에 대해 말하는 것은 매우 짧다.

> "For VDES transceivers: data may be input via the presentation interface to be transmitted by the
> VDES station; data received by the VDES station should be output through the presentation
> interface." — M.2092-2 §A1-2.5

> "VDES supports a presentation interface to be specified in detail by the applicable international
> standards." — M.2092-2 §A4-7

즉 **M.2092-2는 PI의 존재와 역할만 규정하고 상세 포맷은 정의하지 않는다.** 상세는 IEC 소관이다.

### 4.2 NMEA와 IEC 61162 — 이름이 여러 개인 이유

가장 혼동되는 지점부터 정리한다. **NMEA 0183과 IEC 61162-1은 별개의 경쟁 규격이 아니라
같은 것의 두 이름에 가깝다.**

- **NMEA 0183**: 미국 NMEA(National Marine Electronics Association)가 만든 **업계 표준**.
- **IEC 61162-1**: 그것을 **IEC가 국제표준으로 채택·정식화**한 것. IEC TC80(해상 항해·무선통신
  장비 기술위원회)이 관리한다.

따라서 선박 장비 문맥에서 "NMEA 문장"이라고 하면 실무상 IEC 61162-1 문장을 가리킨다.
G1117이 *"The VDES Presentation Interface (PI) uses NMEA sentences"* 라고 쓰면서 동시에
*"will include several new IEC 61162-1 sentences"* 라고 쓰는 것도 같은 이유다.

### 4.3 어느 표준을 언제 쓰는가

IEC 61162는 **여러 파트(Part)로 나뉜 표준군**이며, 파트 번호가 곧 **물리 전송 방식**을 뜻한다.
"어떤 문장을 쓰는가"가 아니라 **"어떤 배선으로 나르는가"** 가 파트를 가른다.

| 파트            | 별칭                   | 물리 매체                      | 토커/리스너         | 언제 쓰는가                         |
| ------------- | -------------------- | -------------------------- | -------------- | ------------------------------ |
| **61162-1**   | NMEA 0183            | 직렬(serial), **4 800 baud** | 단일 토커 → 다중 리스너 | 일반 항해 센서 데이터. **PI 문장 포맷의 기준** |
| **61162-2**   | —                    | 직렬, 고속(38 400 baud)        | 단일 토커 → 다중 리스너 | 61162-1로 **전송률이 부족할 때**        |
| **61162-3**   | NMEA 2000            | CAN 버스                     | 다중 ↔ 다중        | 선박 내 기기 네트워크(본 프로젝트 무관)        |
| **61162-450** | Lightweight Ethernet | **이더넷 / UDP**              | 다중 토커 ↔ 다중 리스너 | 문장을 **네트워크로 캡슐화**해 전달. 현대 통합선교 |

**핵심 관계**: 61162-1이 *문장(sentence)의 문법*을 정의하고, 61162-450은 그 문장을
*이더넷 UDP 패킷에 담아 나르는 방법*을 정의한다. 둘은 대체재가 아니라 **적층 관계**다.

```text
[61162-1 문장]  $GPRMC,123519,A,4807.038,N,...*6A
        │
        └─ 61162-450이 이 문장을 UDP 페이로드에 캡슐화 → 이더넷으로 전달
```

실제로 §4.5의 `py_vdes1000`도 *"Outgoing PI sentences are encapsulated in IEC 61162-450 messages
and sent to the transceiver using the UDP protocol"* 방식으로 동작한다. G1117 §2.10 역시 ECDIS 등
선교 장비가 **IEC 61162-450을 통해 VDES 수신기와 접속**한다고 기술한다.

| 그 밖의 관련 표준         | 역할                                                         |
| ------------------ | ---------------------------------------------------------- |
| **IEC 63514**      | VDES 장비 표준(개발 중). PI 메시지 최대 크기·인증 규정. CDV 2026, IS 2027 예상 |
| **ITU-R M.2092-2** | VDE-TER 무선 구간(PHY/링크). PI는 §A1-2.5·§A4-7에서 역할만 언급          |

**본 프로젝트의 선택**: 우리는 실제 VDES 트랜시버가 아니라 USRP B210 + GNU Radio로 링크를
모사하므로, **직렬/이더넷 배선 규격(61162-2/-450)은 해당되지 않는다.** 참조할 것은
**61162-1의 문장 문법과 필드 정의**뿐이며, 그마저도 §5.3의 payload 예산 때문에 원문 ASCII 문장을
그대로 싣지 않고 **필드 의미만 차용한 압축 바이너리**로 인코딩한다(§4.6).

### 4.4 문장 구조 — IEC 61162-1 Clause 7

문장 구조의 규범적 정의는 **IEC 61162-1 Clause 7 "Data format protocol"** 에 있다.
아래는 2007년판(3rd ed.) 조항 번호 기준이다.

#### 4.4.1 두 종류의 델리미터 — `$` 와 `!`

2007년판에서 도입된 가장 중요한 구분이다(§7.2.1).

> "The `"$"` delimiter identifies sentences that conform to the conventional parametric and
> delimited field composition rules as described in 7.3.2. The `"!"` delimiter identifies sentences
> that conform to the special-purpose **encapsulation** and non-delimited field composition rules
> as described in 7.3.3." — IEC 61162-1:2007 §7.2.1

| 델리미터 | 명칭                         | 필드 구성              | 용도                           |
|:----:| -------------------------- | ------------------ | ---------------------------- |
| `$`  | **parametric sentence**    | 쉼표로 구분된 필드         | 일반 항해 데이터 (RMC, GGA, HTC …)  |
| `!`  | **encapsulation sentence** | 비구분(non-delimited) | **바이너리 데이터 캡슐화** (AIS VDM 등) |

**이 구분이 우리에게 중요한 이유**: AIS/VDES처럼 *바이너리 payload를 실어 나르는* 문장은
`$`가 아니라 **`!`(encapsulation)** 계열이다. 실제 AIS 문장이 `!AIVDM,...` 형태인 것이 그 예다.
즉 우리가 다루는 VDE payload 전달은 개념적으로 **encapsulation 계열**에 속한다.

#### 4.4.2 주소 필드(address field) 구조

```text
$ G P R M C , 1 2 3 5 1 9 , A , ... * 6 A <CR><LF>
│ └─┬─┘ └─┬─┘  └──────┬──────┘   │ └┬┘  └───┬───┘
│   │     │           │          │  │       └─ 종료: CR LF
│   │     │           │          │  └───────── 체크섬 (8-bit XOR, 16진 2자리)
│   │     │           │          └──────────── 체크섬 구분자 "*"
│   │     │           └─────────────────────── 데이터 필드들 ("," 로 구분)
│   │     └─────────────────────────────────── sentence formatter (3문자)
│   └───────────────────────────────────────── talker identifier (2문자)
└───────────────────────────────────────────── 시작 델리미터 ($ 또는 !)
```

§7.2.1.1(Approved address field)에 따르면 승인된 주소 필드는 **5문자**이며, 앞 2문자가
talker identifier(Table 4), 뒤 3문자가 sentence formatter(§8.3)다.

> "The first two characters are the talker identifier (...). The talker identifier serves to define
> the **nature of the data** being transmitted. (...) The next three characters form the **sentence
> formatter** used to define the format and the type of data." — §7.2.1.1

주소 필드에는 세 종류가 있다(§7.2.1.1~7.2.1.3).

| 종류          | 구조                           | 예         |
| ----------- | ---------------------------- | --------- |
| Approved    | 2문자 talker + 3문자 formatter   | `GPRMC`   |
| Query       | 요청자 talker + 대상 talker + `Q` | `CCGPQ`   |
| Proprietary | `P` + 3문자 제조사 코드 + 추가        | `PXYZ...` |

**talker identifier는 데이터의 출처(nature)를 나타낸다.** 표준은 이를 엄격히 요구한다 —
예컨대 GPS 수신기가 나침반에서 유래하지 않은 침로를 `$GPHCD`로 보내서는 안 된다(§7.2.1.1).
**출처를 속이지 말라는 요구가 표준에 이미 명시되어 있다**는 점은, 본 프로젝트의 신분 사칭
공격 논의와 직접 연결되는 지점이다.

#### 4.4.3 필드 규칙에서 설계에 참고할 점

- **필드는 구분자를 세어 찾는다**(§7.2.2). 문자 수를 세면 안 된다 — 가변 길이 필드와 널 필드
  때문이다.
- **널 필드(null field)** 는 값이 **신뢰할 수 없거나 사용 불가**할 때 사용한다(§7.2.2.3).
  표준은 그 이유를 명확히 든다 — 침로가 없을 때 `000`을 보내면 *"데이터 없음"* 과
  *"정침 000도"* 를 구분할 수 없다. → **우리 바이너리 인코딩에도 "값 없음"을 나타내는 표현이
  반드시 필요하다.** (예: sentinel 값 또는 유효성 비트)
- **6-bit binary field conversion**(Annex C, 규범적): 바이너리 데이터를 유효한 ASCII 문자로
  변환하는 규칙. AIS payload가 이 방식으로 인코딩된다. 다만 **6비트를 8비트 문자에 담으므로
  약 33% 크기가 늘어난다** — §5.3의 payload 예산을 고려하면 우리는 이 변환을 쓰지 않고
  **순수 바이너리를 유지**하는 편이 유리하다.

#### 4.4.4 61162-1의 한계 — 우리가 그대로 쓰지 않는 이유

Clause 1(Scope)이 스스로 밝히는 제약이다.

> "Typical messages may be from about **11 to a maximum of 79 characters** in length and generally
> require transmission **no more rapidly than one message per second**." — §1

> "Since there is no provision for **guaranteed delivery** of messages and only **limited error
> checking** capability, this standard should be used with **caution in all safety applications**."
> — §1

세 가지 시사점:

1. **ASCII 직렬화는 바이너리보다 본질적으로 크다.** 최대 79자 문장은 압축 바이너리 대비 여유가
   없으며, Link ID가 확정되어 고정 슬롯 예산이 생기면(§5.3.1) 곧바로 불리해진다.
2. **도달 보장이 없다.** 이 역할은 VDE 링크 계층의 Type 92 ACK/재전송이 담당한다(§3.2).
3. **오류 검출이 8-bit XOR 체크섬뿐**이다. 무결성 보장에는 부족하며, 링크의 CRC-32와
   본 프로젝트가 추가할 인증 계층(§6)이 이를 보완해야 한다. **체크섬은 우연한 오류만 잡고
   고의적 위조는 전혀 막지 못한다** — 이것이 본 프로젝트의 출발점이다.

### 4.5 실제 구현 사례

CML Microcircuits VDES1000 트랜시버용 오픈소스 Python 패키지 `py_vdes1000`(GLA R&D)는
PI 동작 방식의 구체적 참고가 된다.

- 구현 문장: **TSA**(Transmit slot assignment), **VDM**(VHF data-link message),
  **BBM**(binary broadcast message), **ABB**(ASM broadcast message),
  그리고 VDE 데이터 전송용 독자 문장 **EDM**(VDE data message).
- 송신 PI 문장은 **IEC 61162-450 메시지로 캡슐화되어 UDP로** 트랜시버에 전달된다.
- VDE 데이터 송신은 `send_vde_data()`에 **payload(BitStream)와 destination ID**를 넘기는 형태다.

→ **PI의 실질 인터페이스가 `(payload, destination ID)` 라는 점**은 우리 구현에도 그대로 적용된다.
현재 팀 코드의 `TxRequest`가 이미 유사한 형태(messages + repeat_count)를 갖고 있다.

### 4.6 우리 구현에서의 위치

- 표준 규격 PI(IEC 61162-1 문장 파서/생성기)는 **팀 코드에 구현되어 있지 않다.**
- 현재 `cli/console_tx.py`의 `TxRequest` 큐는 **사람이 손으로 payload를 입력하는 임시 입력구**이며
  표준 PI가 아니다.
- 따라서 우리 작업은 **기존 송수신 경로(TxRequest 큐 → VDETxSource → BPSK)를 재사용하되,
  표준이 비워둔 PI 자리에 "응용 메시지 ↔ payload bytes" 변환 어댑터를 얹는 것**이다.
- 프로젝트 목표(인증·무결성 공방 검증)에는 완전한 IEC 61162 PI 구현은 과하다.
  **PI의 역할만 수행하는 경량 어댑터**로 충분하며, 인터페이스만 61162 개념에 정렬시킨다.

**61162-1에서 무엇을 가져오고 무엇을 버리는가** — §4.4의 분석을 우리 설계 결정으로 정리하면:

| 61162-1 요소                            | 채택 여부   | 이유                                           |
| ------------------------------------- |:-------:| -------------------------------------------- |
| 문장의 **필드 의미와 단위** (위치·SOG·COG·침로)     | **채택**  | 항법 데이터의 표준 의미론. 재발명할 이유 없음                   |
| HTC/HTD **명령↔응답 쌍 구조**                | **채택**  | Type 92 ACK 구조와 정확히 대응 (§3.3)                |
| **널 필드 개념** ("값 없음"의 명시적 표현)          | **채택**  | 안전상 필수. 바이너리에서는 sentinel/유효성 비트로 구현 (§4.4.3) |
| **ASCII 문장 직렬화** (`$GPRMC,...`)       | **불채택** | 79자 제약이 37 bytes 예산 초과 (§4.4.4, §5.3)        |
| **6-bit binary conversion** (Annex C) | **불채택** | 약 33% 크기 증가. 순수 바이너리 유지가 유리                  |
| **8-bit XOR 체크섬**                     | **불채택** | 링크 CRC-32가 대체. 위조 방지는 §6의 인증이 담당             |
| **직렬/이더넷 배선 규격** (61162-2/-450)       | **불채택** | USRP 모사 링크에는 해당 없음 (§4.3)                    |

---

## 5. Payload 안에는 무엇이 들어가는가 — VPFI

### 5.1 VDE Payload Format Identifier (IALA R1027)

VDE payload의 내용 형식을 식별하는 표준 수단이 **VPFI**다. G1117 §2.7:

> "In order for applications to be able to identify the content of VDES messages, the Payload Format
> Identifier (VPFI) as described in Recommendation R1027 should be used. (...) this document assumes
> that the VDE-TER and VDE-SAT binary messages use the VPFI for all applications."

- **16-bit VPFI**가 각 응용 데이터에 부착되며, 선택적으로 8~16 bit의 message identifier를 추가한다.
- 한 VDES국이 **동일 Source ID로 여러 포맷을 사용**할 수 있게 한다.
- 기존 AIS 메시지·ASM 포맷의 전송도 VPFI로 식별 가능하다.
- G1117 §2.11에 따르면 **VPFI 0**은 Encapsulated AIS 메시지에 할당되어 AIS 인증에 사용된다.

**설계 시사점**: 우리 응용 메시지에도 payload 선두에 포맷 식별자를 두는 구조가 표준 정합적이다.
정식 VPFI 값 할당은 IALA 소관이므로, 시험용으로는 미할당 대역의 임의값을 쓰되
**필드 위치와 크기(16-bit)는 R1027에 맞춰 두는 것**을 권고한다.

### 5.2 항법 데이터 문장 — IEC 61162-1

MASS가 실제로 내보내는 항법 데이터의 표준 문장은 다음과 같다.

| Formatter | 내용                            | 본 프로젝트 활용   |
| --------- | ----------------------------- | ----------- |
| **RMC**   | 위치 + SOG + COG + UTC (가장 종합적) | 텔레메트리 기본    |
| **GGA**   | 위치 고정(fix), 품질, 위성 수          | fix 품질 필요 시 |
| **VTG**   | 대지침로·대지속력                     | RMC로 대체 가능  |
| **HDT**   | 진침로(true heading)             | 선수방위        |
| **ROT**   | 선회율(rate of turn)             | 조종 상태       |
| **OSD**   | 자선 데이터(침로·속력 종합)              | 대안          |

### 5.3 결정적 제약 — Payload 크기

G1117 §2.8이 규정하는 VDES PI 최대 메시지 크기:

| 구분          | 최대 크기                                                                      |
| ----------- | -------------------------------------------------------------------------- |
| ASM         | 2,259 bytes(broadcast) / 2,199 bytes(addressed) / 157 bytes(geo multicast) |
| **VDE-TER** | **9,800 bytes**                                                            |
| VDE-SAT     | 10 kB                                                                      |

**그러나 이 크기는 fragmentation(Type 74/75/76 데이터 세션)을 전제한 값이다.**
Short data message(Type 92/93)는 **한 슬롯에만 담기는 전송**으로 제한된다.

> "The short data message refers to the data transfer protocol used for transmission of payload in
> one slot only." — M.2092-2 §A4-4.2.5

> "VDE packet transmissions shall always fit into one slot. The number of bits transmitted per VDE
> packet shall be fixed, depending on the Link ID used." — M.2092-2 §A4-4.6

팀 후속 계획(`implementation_plan.md` §7)이 1차 목표로 **잠정 제시**한 Link ID 11 기준으로
계산하면:

```text
Link ID 11 정보 블록          432 bit = 54 bytes
  − CRC-32                            4 bytes
  = Message 영역                     50 bytes
  − Type 92 header                   13 bytes   (Type 93은 12 bytes)
  ────────────────────────────────────────────
  = 응용 Payload 최대            약 37 bytes    (Type 93 기준 38 bytes)
```

### 5.3.1 "37 bytes"는 미래의 잠정 수치이지, 지금의 제약이 아니다

**이 계산을 지금 시점의 하드 제약으로 취급하면 안 된다.** 근거를 하나씩 짚으면:

- 이 계산이 쓰는 Link ID 11은 `implementation_plan.md` §7 "B210 유선 환경의 Padding·CRC·Turbo
  FEC 통합 실험"이 제시한 값인데, **이 실험 자체가 아직 시작되지 않은 후속 단계**다(§9 "그 외
  구현 계획 (논의 필요)"의 1차 항목으로 예정되어 있을 뿐).
- Link ID 11조차 확정이 아니라 **잠정** 값이다. 문서는 스스로 "실제 운용에 필요한 채널 대역폭과
  MCS가 결정된 뒤" 확정한다고 명시한다(`implementation_plan.md` §9.1).
- **현재 실제로 구현되어 동작 중인 링크(`no_error_correction`)는 Link ID를 아예 쓰지 않는다.**
  `requirements.md` §2.3은 "Link ID 생성 및 decoding"을 2단계에서 **구현하지 않는 항목**으로
  명시한다.

현재 구현의 실제 전송 형식은 고정 슬롯이 아니라 **가변 길이**다.

```text
[F9A8 | Temporary Packet Length | VDE Packet]
 2 byte      2 bytes (MSB-first)     N bytes
```

`Temporary Packet Length`는 뒤따르는 VDE Packet의 실제 byte 수를 그대로 담는 **2-byte 길이
필드**다(`requirements.md` §7). 즉 **현재 링크는 최대 65,535 bytes(2¹⁶−1)까지 어떤 길이의
packet도 그대로 실어 나른다** — Link ID가 정하는 고정 슬롯 크기 같은 것이 지금은 존재하지
않는다. 실무적 상한은 표준이 아니라 전송 시간·GNU Radio 버퍼 여유가 정한다.

**결론**: `37 bytes`는 **팀이 아직 착수하지 않은 후속 실험(Link ID 확정 이후)에서나 의미를
갖는 수치**이며, 지금 응용 메시지 포맷을 설계하는 데 강제되는 상한이 아니다. **Link ID는
우리가 결정할 사항이 아니고(링크/PHY 팀원의 후속 범위), 그 확정을 기다릴 필요도 없다** — 현재
링크가 이미 가변 길이를 지원하므로 그 위에서 바로 응용 계층 작업을 시작할 수 있다.

다만 설계 관행으로는 여전히 압축을 염두에 두는 편이 낫다. Link ID가 확정되면 고정 슬롯 예산이
생길 가능성이 높고(§9.1), G1117 §2.9도 payload 압축을 권고한다. "지금 강제되지 않는다"는 것이지
"압축이 무의미하다"는 뜻은 아니다 — 다만 **37 bytes를 지금 시점의 하드 제약으로 못 박고 설계를
막지는 않는다.**

---

## 6. 보안 배경 (후속 단계)

프로젝트 최종 목표인 인증·무결성과 직결되는 표준·연구 동향이다. 구현은 응용 계층과 링크가
안정된 이후 단계로 둔다.

| 문헌                                                                   | 내용                                                     | 상태                   |
| -------------------------------------------------------------------- | ------------------------------------------------------ | -------------------- |
| **IEC 63514**                                                        | VDES 장비 표준. AIS 무결성·진위 확인 및 그 외 VDES 메시징 인증 규정         | CDV 2026, IS 2027 예상 |
| **IALA G1192**                                                       | VDES Authentication. VDE-TER로 기존 AIS 송신(AtoN)을 인증하는 방법 | 발행됨                  |
| **IALA R1027**                                                       | VPFI. VPFI 0 = Encapsulated AIS (인증에 사용)               | 발행됨                  |
| **"A Pragmatic Approach to VDES Authentication"** (NAVIGATION, 2025) | 아래 §6.1                                                | 논문                   |
| **CITesting** (ACM CCS '25)                                          | CIV 위협 모델 및 상태 기반 시험 방법론 — §2.4 참조                     | 논문                   |

G1117 §2.11의 서술:

> "Authentication of messages is an important feature which VDES can offer as defined in IEC 63514.
> Authentication provided by VDES signed segment authentication enables the receiver of messages to
> validate the authenticity of the sender and the integrity of the data."

또한 G1117은 향후 **TESLA**(Timed Efficient Stream Loss-tolerant Authentication, RFC 4082)와 같은
브로드캐스트 인증 프로토콜이 고려될 수 있다고 언급한다.

### 6.1 NAVIGATION 논문의 시사점

VDES 인증에 관한 최근 연구(navi.ion.org)의 요지:

- **주 위협은 spoofing**이다. 악의적 행위자가 임의 메시지를 송신하면 수신 측이 그대로 신뢰하며,
  항법 관련 메시지의 경우 **선박을 위험으로 유도**할 수 있다.
- 제안은 **2단 구조**다.
  - 기본 모드: 개별 메시지에 **ECDSA 서명(512 bit)**
  - 저부하 모드: 육상 통제 해역에서 **TESLA + 32-bit MAC**
- **오버헤드 비교**: 2초 주기 메시지 기준 기존 서명 방식은 분당 90+ 슬롯을 소모하는 반면
  TESLA는 분당 36 슬롯(약 0.4% 용량)으로 처리된다.
- PKI는 해양 특화 인증기관을 제안하되 구체 인프라는 미확정 과제로 남겨두었다.

> **정정 (`교통정리.pdf` 검토 결과)** — 아래 단락은 **Link ID 11(37 bytes)을 전제한 계산이며
> 더 이상 유효하지 않다.** 팀 구현 방향이 **Link ID 17**(packet 234 bytes)로 잡혔고, 그 예산에서는
> **표준이 규정한 ECDSA 서명이 그대로 들어간다**(§6.4). "ECDSA가 안 들어가므로 TESLA가 필요하다"는
> 결론은 취소한다. TESLA는 슬롯 점유를 줄이는 대안 후보로만 남는다.

**(취소된 이전 결론)** §5.3.1의 잠정 예산(37 bytes)을 대입해 보면 ECDSA 64 bytes 서명은 단일
Short Data Message에 들어가지 않는 반면 TESLA의 32-bit MAC(4 bytes)은 충분히 들어간다.

### 6.4 표준이 이미 규정한 인증 — 우리가 고를 여지가 없다

`교통정리.pdf` 검토 중 확인한 내용이다. **M.2092-2는 인증 방식을 구체적으로 못박고 있다.**
앞선 §6에서 "표준이 비어 있다"고 본 것은 **운영 측면(CA 주체·키 배포)에 한정**되며,
암호 파라미터는 전부 정해져 있다.

| 항목        | 표준이 정한 값 (§A4-4.15)                                |
| --------- | -------------------------------------------------- |
| PKI · 인증서 | **ITU-T X.509 (10/2016)**                          |
| 서명 알고리즘   | **ECDSA**                                          |
| 공개키 크기    | **256 bit**                                        |
| 곡선        | **secp256r1**                                      |
| 다이제스트     | **SHA-256**                                        |
| 인코딩       | **IEEE P1363 Fixed-Field Concatenation**           |
| 인증서 지문    | signatureValue의 **최하위 48 bit** (RFC 5480 §4.1.1.3) |
| 서명 필드     | **512 bit = 64 byte**                              |
| 오프라인 대비   | 인증서·**CRL 저장** 필수                                  |

검증 실패 시 처리도 규정되어 있다.

> "In case the verification of the signature fails on the VDES mobile station the message cannot be
> trusted. For Link Layer messages, such as the bulletin board, the system shall act as if it
> **never had received the message**." — §A4-4.15

이는 §6.3에 적은 "검증 전에 상태를 바꾸지 않는다" 원칙과 정확히 같은 요구다. **3GPP Release 18과
M.2092-2가 같은 결론에 도달해 있다.**

#### Segment 계층 (§A4-4.29) — **본 프로젝트 담당 범위가 아니다**

응용 데이터의 인증은 **Segment**라는 계층에서 이루어진다. 다만 이 계층은 **링크 계층 안쪽**이며
응용·PI 구현 범위가 아니다. 표준이 명시한다.

> "The reliable transmission of **data segments, segmentation**, acknowledgement and multiplexing
> requires close integration with the link layer. The responsibility of the transport layer was
> therefore **moved to the link layer**." — §A4-6 Transport layer

> "The responsibility of the network layer was therefore **moved to the link layer**."
> — §A4-5 Network layer

> "Data to be transmitted over VDE payload **should be input to equipment via the PI** by using
> applicable international standards. VDE payload segments get packed inside the fragment…"
> — §A4-4.29

즉 **응용은 PI로 payload를 넘기는 데서 끝나고, segment 조립과 서명은 장비(링크 계층)가 한다.**
signed segment의 필드도 거의 전부 segment 계층 값이며, 서명 계산에 쓰는 Source·Destination
MMSI 역시 링크 계층 헤더의 값이다. 응용이 만드는 것은 `Payload data` 하나뿐이다.

```text
[MASS / ROC 응용]   ← 본 프로젝트
      │ payload
[PI]                ← 본 프로젝트 (담당의 끝)
──────────────────────────────────────
[Link Layer]        ← 팀원
   · Segment 조립 (signed / unsigned)   ← 전송 계층에서 이관
   · Fragment(74/75/76) packing
   · 자원할당 · ACK · 재전송            ← 네트워크·전송 계층에서 이관
[PHY]
```

아래 구조는 **팀원 범위를 이해하기 위한 참고**이며 우리가 구현할 대상이 아니다.

| 필드                      | Unsigned (Table 54) | Signed (Table 55) |
| ----------------------- | -------------------:| -----------------:|
| Segment type            | 2 bit (=0)          | 2 bit (=1)        |
| Sequential ID           | 14 bit              | 14 bit            |
| Signed Segment Format   | —                   | 7 bit             |
| Valid until 유무 / 값      | —                   | 1 + 32 bit        |
| Certificate fingerprint | —                   | 48 bit            |
| Digital signature       | —                   | **512 bit**       |
| Payload size            | 16 bit              | 16 bit            |
| **오버헤드**                | **4 byte**          | **75~79 byte**    |

두 가지가 특히 중요하다.

- **Source·Destination MMSI를 서명 계산에 앞에 붙이되 전송하지는 않는다.** 신분 사칭을
  전송 비용 없이 막는 설계다.
- **`Valid until`(1970-01-01 UTC 기준 초)이 재전송 방어 필드**다.

#### 인증이 붙는 곳과 붙지 않는 곳 — 취약점 분석의 표적

| 구분        | 대상                                                         | 근거                             |
| --------- | ---------------------------------------------------------- | ------------------------------ |
| 인증 있음     | Terrestrial Bulletin Board (Type 20)                       | 지문 6 byte + 인증·무결성 시퀀스 64 byte |
| 인증 있음     | Signed segment                                             | Table 55                       |
| **인증 없음** | Unsigned segment                                           | Table 54                       |
| **인증 없음** | ACK/NACK(13), Resource allocation(4), Resource request(90) | 서명 필드 없음                       |
| **인증 없음** | Short data message(92/93)                                  | segment 계층 밖                   |

**아래 세 줄이 표적이다.** 제어 메시지(자원할당·ACK)에 인증이 없다는 것은 CITesting이 정의한
CIV(§2.4)가 성립할 조건과 정확히 같다 — 인증되지 않은 메시지가 상대의 내부 상태를 바꾼다.

#### 구조적 제약 — Short Data Message에는 서명을 붙일 수 없다

표준 문언상 segment는 **fragment 안에** 담긴다(*"VDE payload segments get packed inside the
fragment"*). Type 92/93의 Payload 필드에는 내용 설명이 없고 segment를 언급하지 않는다.
채널도 다르다 — 92/93은 RAC/ASC, fragment는 자원할당으로 배정된 TDMA 채널.

→ 인증이 필요한 흐름은 **data session 경로(Type 90 → 4 → 74/75/76 → 13)** 로 옮겨야 한다.
이는 **링크 계층의 구현 범위 문제**이며, **응용 메시지 자체는 바꿀 필요가 없다** — 그대로
signed segment의 Payload data에 들어간다.

#### 그래서 응용·PI 쪽에 남는 보안 작업

Segment 조립과 서명이 링크 계층 몫이므로, 본 프로젝트에 남는 것은 셋뿐이다.

| #   | 작업                 | 왜 응용 몫인가                                                                                                                                                     |
|:---:| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | **검증 결과를 응용까지 전달** | 표준은 링크 계층 메시지에 대해서만 "수신하지 않은 것처럼 행동하라"고 정한다. 응용 payload의 검증 성패는 PI를 통해 올라와야 MASS가 §6.3의 "인증 안 된 입력으로 상태를 바꾸지 않는다"를 적용할 수 있다. 현재 `codec.decode()`는 인증 여부를 모른다 |
| 2   | **PKI 운영 제안**      | CA 주체·키 배포·갱신은 표준이 실제로 비워둔 유일한 자리다                                                                                                                           |
| 3   | **취약점 실증**         | 인증이 없는 지점(4·90·13·unsigned segment)을 공격했을 때 MASS에 무엇이 일어나는가. 관찰 도구(`common/observe/`)가 준비되어 있다                                                             |

#### Link ID 17 기준 예산

```text
Link ID 17 packet                        234 byte
  − CRC-32                                 4
  − Type 74(Start fragment) 헤더            15
  − Signed segment 헤더(Valid until 포함)    79
  ──────────────────────────────────────────────
  = 응용 payload 가용                      136 byte
```

Position Report 17 byte가 여유 있게 들어간다. **Link ID 11(37 byte)에서는 서명이 물리적으로
불가능**하므로, 인증을 다루려면 Link ID 17 이상이 전제다.

### 6.2 두 논문의 역할 분담

본 프로젝트의 보안 설계는 두 논문을 서로 다른 층위에서 활용한다. 겹치지 않고 보완적이다.

|        | **NAVIGATION 논문**             | **CITesting**                     |
| ------ | ----------------------------- | --------------------------------- |
| 분야     | VDES (해상)                     | LTE 코어망 (셀룰러)                     |
| 기여하는 것 | **무엇으로 막을 것인가** — 인증 기법과 오버헤드 | **무엇을 검증할 것인가** — 위협 정의와 시험 방법론   |
| 핵심 개념  | ECDSA vs TESLA, 슬롯 오버헤드       | CIV, 상태 의존 취약점, behavioral oracle |
| 적용 단계  | 보안 계층 설계 (§6)                 | 실험 설계·공격 검증 (§2.4~2.7)            |
| 한계     | 상태 개념·검증 방법론 없음               | 해상 도메인·payload 제약 미고려             |

**결합했을 때의 논지**: CITesting이 정의한 CIV를 VDE-TER에 적용하면 *"인증되지 않은 메시지가
MASS/ROC의 내부 상태를 바꿀 수 있는가"* 라는 검증 가능한 질문이 되고, 그 답이 "그렇다"이면
NAVIGATION 논문이 제시한 기법 중 **37 bytes 예산에 들어가는 것**(TESLA류 짧은 MAC)으로 막은 뒤,
CITesting의 상태별 반복 검증으로 **모든 제어권 상태에서 막혔는지** 확인한다.

### 6.3 인증 설계에 반영할 CITesting의 원칙

§2.4.2에서 인용한 3GPP Release 18 조항을 본 프로젝트의 설계 원칙으로 채택한다.

> **원칙**: 인증되지 않았거나 부적절하게 인증된 메시지를 받았을 때, 또는 적절한 인증이 없는
> 불완전·미완료 절차의 **어느 단계에서든**, 수신 노드는 내부 상태(제어권·항로·세션 문맥)를
> **변경하지 않아야 한다.**

구현상 이는 다음을 뜻한다.

- 인증 검증은 **상태를 바꾸기 전에** 수행한다. 파싱 성공만으로 상태를 갱신해서는 안 된다.
- 검증 실패 메시지는 **조용히 폐기**하되 로그·통계에는 남긴다(관찰 도구가 공격을 보여줘야 한다).
- **다단계 절차(제어권 이양)의 중간 단계도 각각 인증**해야 한다. 첫 단계만 인증하고 이후
  단계를 신뢰하면 CITesting이 지적한 "절차 연쇄" 취약점이 그대로 재현된다.
- 검증 실패가 **재전송·타임아웃 상태에 영향을 주지 않도록** 한다. 그렇지 않으면 인증 실패
  자체가 DoS 수단이 된다.

---

## 7. 정리 — 확정된 것과 결정할 것

### 7.1 표준 근거로 확정된 사항

- 응용 데이터는 **Payload**에 들어가고, Destination/ACK/Broadcast/Session은 **링크 계층 봉투**다.
- PI는 표준이 **역할만 정의**하고 상세는 IEC 61162 소관이며, 팀 코드에는 미구현이다.
- **NMEA 0183과 IEC 61162-1은 같은 것의 두 이름**이고, 파트 번호(-1/-2/-450)는 **문장 종류가
  아니라 물리 전송 방식**을 가른다. 우리에게 필요한 것은 **61162-1의 문장 문법뿐**이다.
- 61162-1은 델리미터로 **`$`(parametric) / `!`(encapsulation)** 를 구분하며, **바이너리 payload는
  `!` 계열**이다(AIS `!AIVDM`).
- PI의 실질 인터페이스는 **(payload, destination ID)** 형태다.
- Payload 내용 식별은 **16-bit VPFI**(IALA R1027)가 표준 수단이다.
- 항법 데이터 표준 문장은 **RMC/GGA/VTG/HDT/ROT**, **제어 명령은 HTC(명령)/HTD(응답)/ROR**
  (IEC 61162-1)다. HTC↔HTD 쌍이 Type 92 ACK 구조와 대응한다.
- **현재 구현된 링크는 Link ID를 쓰지 않는 가변 길이 전송이다.** `Temporary Packet Length`
  (2 bytes)가 실제 packet 크기를 담으므로 최대 65,535 bytes까지 지원하며, 고정 슬롯에서
  비롯되는 payload 상한이 지금은 존재하지 않는다(§5.3.1). "Link ID 11 기준 약 37 bytes"는
  팀이 아직 착수하지 않은 후속 실험에서나 적용될 **잠정 수치**다.
- Type 선택(92/93)은 **응용/PI 계층의 결정**이고, Type 판단(파싱)은 **링크 계층**이 수행한다.
- 61162-1의 **8-bit XOR 체크섬은 우연한 오류만 잡고 고의적 위조는 막지 못한다.**
  이것이 본 프로젝트 인증 계층의 출발점이다.
- 본 프로젝트의 위협 모델은 **CIV**(Context Integrity Violation)로 정의된다 — 인증되지 않은
  메시지가 MASS/ROC의 내부 상태를 변경하는 것(CITesting §3.1).
- **3GPP는 Release 17/18에서 "인증 실패 시 컨텍스트 불변" 원칙을 명문화했으나, VDE-TER에는
  이에 상응하는 규정이 없다.** 이것이 본 프로젝트 제안의 정당화 근거다.
- 취약점은 **상태 의존적**이다. 한 상태에서 안전해도 다른 상태에서 나타날 수 있으므로
  **모든 제어권 상태에서 공격 실험을 반복해야 한다**(CITesting §4.1 C2, 평가 결과).
- 상태 축은 하나가 아니라 **둘**이다 — 세션 수립 여부(EMM 대응)와 제어권 소재(ECM 대응).
  조합하여 **RC / MON / AUT** 세 상태를 정의한다.

### 7.2 결정이 필요한 사항 — "정상 동작하는 MASS/ROC" 기준으로 재정렬

**지금 목표는 공격·실험이 아니라 정상 동작하는 MASS/ROC 구현이다.** 이 기준으로 나누면
실제로 지금 막고 있는 결정은 둘뿐이고, 나머지는 뒤로 미뤄도 된다.

#### 지금 필요 (구현을 막고 있는 것)

1. **응용 메시지 세트** — MASS가 ROC에 보낼 것(텔레메트리)과 ROC가 MASS에 보낼 것(명령)의
   구체 필드 목록. 이것이 정해져야 나머지가 진행된다.
2. **바이너리 인코딩** — 위 필드를 payload bytes로 직렬화하는 방식. §5.3.1에 따라
   **지금은 하드 바이트 제약이 없으므로**, 압축을 과도하게 신경 쓰기보다 필드를 읽기 쉬운
   형태로 우선 설계해도 된다.

#### 보류 — 정상 동작이 확인된 뒤 다룰 것

3. **Link ID** — **우리가 결정할 사항이 아니다.** 링크/PHY 팀원의 후속 범위이며, 그 확정을
   기다릴 필요도 없다. 현재 링크(`no_error_correction`)는 Link ID 없이 가변 길이로 이미
   동작하므로 지금 바로 그 위에서 작업할 수 있다(§5.3.1).
4. **VPFI 시험값** — 낮은 우선순위. 지금 정하지 않아도 구현이 막히지 않으며 언제든 추가 가능.
5. **제어권 상태 모델(RC/MON/AUT)과 전환 프로토콜** (§2.4~2.6) — CITesting을 참고해 만든
   이 상태 축은 **공격 실험을 위한 설계**다. 정상 동작 목표에서는 훨씬 단순한 형태
   (예: MASS가 명령을 받아 반영하고 텔레메트리를 계속 보낸다)로 시작해도 충분하다.
6. **실험 축(Degree × 상태)과 대조군 구성** (§2.8) — 실험 설계 단계로 이연.
7. **상태 불일치 처리** (§2.4.5) — 공격 시나리오 대응. 이연.
8. **보안 방식** (§6) — 응용 계층이 안정된 뒤 별도 단계.

---

## 참고문헌

### 로컬 보유 문서

- ITU-R M.2092-2 (2026-02): *Technical characteristics for a VHF data exchange system in the VHF
  maritime mobile band* — `R-REC-M.2092-2-202602-I!!PDF-E.pdf`
  - §A1-2.4 (식별자), §A1-2.5 (PI), §A2-1.1 (계층), §A4-4.6 (데이터 구조),
    §A4-4.9 Table 35/40/41 (메시지 타입), §A4-7 (PI 계층)
- IALA G1117 Ed 4.0: *VHF Data Exchange System (VDES) Overview* — `references/G1117-Ed4.0-VDES-Overview.pdf`
  - §2.7 (VPFI), §2.8 (최대 payload), §2.9 (압축), §2.10 (선교 장비 접속), §2.11 (보안)

### 팀 문서

- (구) `VDETER_Link/no_error_correction/docs/requirements.md` — 초기 링크 계층 명세 (현재는 `VDETER/`로 대체)
- (구) `VDETER_Link/no_error_correction/docs/implementation_plan.md` — 초기 후속 계획 (현재는 `VDETER/`로 대체)

### 외부 참조 — MASS / IMO

- [IMO: IMO adopts first global Code for autonomous ships](https://www.imo.org/en/mediacentre/pressbriefings/pages/imo-adopts-mass-code.aspx) — MASS Code 채택(MSC.595(111))
- [IMO: Autonomous shipping FAQ](https://www.imo.org/en/mediacentre/hottopics/pages/autonomous-shipping.aspx) — Degree 1~4 정의
- [IMO: MSC 111th session highlights](https://www.imo.org/en/mediacentre/meetingsummaries/pages/temporary-msc-111.aspx) — 발효 일정, 강제 Code 로드맵

### 외부 참조 — ROC

> §3.1의 근거. §3.2 표의 "방향·성격 분류"는 이 문헌들의 요구사항에서 **추론한 것**이며
> 표에 그대로 실린 형태로 제시된 것이 아니다.

- [Monitoring and service for ships from the perspective of the remote operations center (J. Shipping and Trade, 2026)](https://link.springer.com/article/10.1186/s41072-026-00243-5)
- [Remote Operation of Marine Robotic Systems and Next-Generation Multi-Purpose Control Rooms (MDPI JMSE, 2023)](https://www.mdpi.com/2077-1312/11/10/1942)
- [Regulatory Requirements on the Competence of Remote Operator in MASS (MDPI Applied Sciences)](https://doi.org/10.3390/app10238751) — 원격 운용자 상황인식·감시 역량
- [Legal Status of the Remote Operator in MASS Under Maritime Law (Ocean Development & Int'l Law)](https://www.tandfonline.com/doi/abs/10.1080/00908320.2022.2036276) — 제어권·책임 소재

### 외부 참조 — PI / IEC 61162

- **IEC 61162-1:2007 (3rd ed.) 미리보기** — §4.4의 인용 출처. Clause 1(Scope), §7.2.1(주소 필드),
  §7.2.2(데이터 필드), §7.2.2.3(널 필드), Annex C(6-bit 변환)
  <https://cdn.standards.iteh.ai/samples/12087/58dae76ccb734667ad191701e3638f84/IEC-61162-1-2007.pdf>
- [IEC 61162-1 Ed 6.0 (2024) 미리보기](https://cdn.standards.iteh.ai/samples/108134/c3c1d634d7f844368f1f9f774375f514/IEC-61162-1-2024.pdf) — 최신판. HTC/HTD/ROR 등 문장 목록
- [IEC 61162-2:2024 (IEC Webstore)](https://webstore.iec.ch/en/publication/72730) — 고속 직렬
- [IEC 61162 파트 구성 개요 (Wikipedia)](https://en.wikipedia.org/wiki/IEC_61162) — §4.3 파트 비교표 근거
- [py_vdes1000 — VDES1000 트랜시버 Python 인터페이스 (GLA R&D)](https://github.com/gla-rad/py_vdes1000) — PI 실제 구현 사례

### 외부 참조 — 보안

- [IALA: R1027 VDE Payload Format Identifier (VPFI)](https://www.iala.int/product/r1027-vde-payload-format-identifier-vpfi/)
- [IALA: G1192 VDES Authentication](https://www.iala.int/product/g1192/)
- [A Pragmatic Approach to VDES Authentication (NAVIGATION, ION, 2025)](https://navi.ion.org/content/72/1/navi.681) — ECDSA vs TESLA 오버헤드 비교

### 로컬 보유 논문

- **CITesting** — Mincheol Son, Kwangmin Kim, Beomseok Oh, CheolJun Park, Yongdae Kim.
  *"CITesting: Systematic Testing of Context Integrity Violations in LTE Core Networks."*
  ACM CCS '25, Oct 13–17 2025, Taipei. KAIST · 경희대. — `references/CITesting.pdf`
  - <https://doi.org/10.1145/3719027.3765230> (CC BY 4.0)
  - §2.2 (EMM/ECM 상태), §2.3 (UE context), §3.1 (CIV 정의·3GPP Rel-17/18),
    §4 (위협 모델), §4.1 (C2 상태 의존성, C3 blackbox oracle), §5 (dual-UE 설계)
  - 본 문서 §2.4~2.7, §6.2~6.3의 근거
