# MASS/ROC 구현 요구사항

> 시나리오를 수행하기 위해 무엇이 필요하고, 그중 무엇이 이미 있으며,
> 무엇이 누구의 몫인지 정리한다.

| 항목   | 내용                                                         |
| ---- | ---------------------------------------------------------- |
| 대상   | MASS/ROC 응용 계층 및 PI                                        |
| 인접   | VDE-TER 링크·물리 계층 (팀원, `VDETER_Link/`)                      |
| 계약   | `docs/vdeter_mass_roc_api_guide.md`                            |
| 시나리오 | [`scenarios/scenario_list.md`](scenarios/scenario_list.md) |

---

## 1. 담당 경계

표준이 계층 책임을 명시한다.

> "The reliable transmission of **data segments, segmentation**, acknowledgement and
> multiplexing … The responsibility of the transport layer was **moved to the link layer**."
> — M.2092-2 §A4-6

> "The responsibility of the network layer was **moved to the link layer**." — §A4-5

API 계약도 같은 경계를 정한다.

> MASS/ROC는 Link ID, Message Type, CRC, FEC, Burst, slot과 재전송 절차를 전달하거나
> 구현하지 않는다. — `docs/vdeter_mass_roc_api_guide.md` §6

```text
[MASS / ROC 응용]   ← 본 문서의 대상
      │ payload + data_type + ack_required
[PI / Service API]  ← 본 문서의 대상 (경계)
────────────────────────────────────────
[Link Layer]        ← 팀원
   · Segment 조립(signed/unsigned) · Fragment · 자원할당 · ACK · 재전송
[PHY]               ← 팀원
```

### 1.1 VDETER 연동 (팀원 API 가이드 반영)

팀원이 실제 연동 API를 냈다(`docs/vdeter_mass_roc_api_guide.md`). 경계가 구체화됐다.

- 우리 앱은 `vdeter.service.VdeService`의 `start/submit/status/cancel/stop`만 호출한다.
  MASS=`SHIP`, ROC=`CONTROL`. `submit(destination, payload_format, payload, ack_required)`.
- **payload는 VDE 헤더 없는 순수 bytes**다. **VPFI는 VDETER가 `PayloadFormat`(문자열 키)으로
  관리**한다(28-bit). → **우리 codec의 16-bit VPFI 봉투는 연동 시 제거**해야 한다(이중 부착 방지).
- **운영 지원 format은 둘뿐**: `x509.certificate_report`(SC-VDE-001) ·
  `ais.authentication_integrity.scheme1`(SC-VDE-003, 정확히 82 B). → **이 둘이 지금 바로
  연동 가능한 경로**다.
- **미지원**: route/항행경보/환경/기상은 registry format이 없어 불가(링크 팀이 추가 필요).
  **MASS 발신 broadcast 미지원** → 위치보고 broadcast는 현재 VDETER로 못 보낸다.

→ 연동은 **`common/vdeter_bridge.py`가 Outbound↔SubmitTransferRequest 매핑**을 담당한다(인증서 경로 연동 완료).
   메시지·행위자 코드는 거의 그대로다.

---

## 2. 요구사항 목록

### 2.1 응용 계층 — 이미 구현됨

| #   | 요구사항                       | 상태  | 위치                        |
|:---:| -------------------------- |:---:| ------------------------- |
| A1  | 응용 메시지 인코딩·디코딩             | ✅   | `common/messages/`      |
| A2  | 비트 단위 필드 처리 (27/28 bit 등)  | ✅   | `common/bitfield.py`    |
| A3  | 좌표 부호화 (1/10,000 분)        | ✅   | `common/coordinates.py` |
| A4  | MASS 행위자 — 항로 추종·상태 관리     | ✅   | `MASS/mass_agent.py` |
| A5  | ROC 행위자 — 항적 추적·명령 발행      | ✅   | `ROC/roc_agent.py`  |
| A6  | **검증 후 상태 변경** 순서 강제       | ✅   | `mass.py` `_evaluate()`   |
| A7  | 통신 두절 판정 (별도 heartbeat 없이) | ✅   | `roc.py`                  |
| A8  | 관찰 기록·보고서                  | ✅   | `common/observe/`       |
| A9  | VDETER Service 연동 브리지        | ✅   | `common/vdeter_bridge.py` |

검증: 시험 222항목 통과 (`tests/test_mass_roc_*.py`, `tests/test_phy_primitives.py`)

### 2.2 응용 계층 — 시나리오 수행에 필요

| #      | 요구사항                          | 시나리오           | 선행 조건          |
|:------:| ----------------------------- | -------------- | -------------- |
| **B1** | `data_type`별 payload 전달·수신 분기 | 전체             | 없음             |
| **B2** | X.509 인증서 DER 생성·파싱           | SC-VDE-001     | 없음             |
| **B3** | 인증서 신뢰 저장소 (저장·조회·교체)         | SC-VDE-001     | B2             |
| **B4** | CRL 저장·조회·만료 판정               | SC-VDE-001     | B3             |
| **B5** | ECDSA secp256r1 서명 검증         | SC-VDE-001/002 | B3             |
| **B6** | **검증 결과를 응용까지 전달하는 경로**       | 전체             | Service API 확장 |
| **B7** | AIS 인증·무결성 값 생성·검증            | SC-VDE-003     | B5             |
| **B8** | TBB payload 해석                | SC-VDE-002     | 없음             |

**B6이 특히 중요하다.** 지금 `codec.decode()`는 payload만 돌려주고 **인증 여부를 모른다.**
링크 계층이 서명을 검증해도 그 결과가 응용에 오지 않으면 MASS가
*"인증되지 않은 명령으로 항로를 바꾸지 않는다"* 를 적용할 수 없다.

→ API 계약의 `ReceivedMessage`에 **검증 상태 필드 추가**가 필요하다. API 소유자와 협의 사항.

### 2.3 링크 계층 — 팀원 담당 (시나리오의 선행 조건)

| #      | 요구사항                                    | 필요한 시나리오       | 근거                  |
|:------:| --------------------------------------- | -------------- | ------------------- |
| **L1** | Type 90 자원요청                            | SC-VDE-001     | §A4-4.9             |
| **L2** | Type 4 자원할당 (Link ID·CQI 필드 포함)         | SC-VDE-001     | Table 39            |
| **L3** | Type 74/75/76 fragment 분할·재조립           | SC-VDE-001/004 | Table 49~51         |
| **L4** | Type 13 ACK/NACK (mask 0/1/2·CQI·ACM)   | SC-VDE-001     | Table 38            |
| **L5** | Type 81 padding                         | SC-VDE-001     | §A4-4.9             |
| **L6** | Segment 계층 (unsigned 4 B / signed 79 B) | SC-VDE-001     | Table 54/55         |
| **L7** | ECDSA 서명 생성·검증                          | SC-VDE-001/002 | §A4-4.15            |
| **L8** | Type 20/21/22 불리틴 보드                    | SC-VDE-002     | §A4-4.9             |
| **L9** | **Link ID 11 병행 지원**                    | SC-VDE-002     | §A4-4.8.1 (BBSC 강제) |
| L10    | ACK timeout·재전송 횟수 관리                   | SC-VDE-001     | 교통정리 spec           |
| L11    | LC 사용 상태·data session 성립 여부 로깅          | 전체             | 교통정리 spec           |

### 2.4 암호 개발 — 본 과제의 산출물 (C-계열)

프로젝트 제목의 **'암호통신 개발'** 에 대응한다. B-계열이 "표준 암호를 응용에서 쓰는 것"
이라면, C-계열은 **"공격을 실증하고, 방어를 설계·평가하는 것"** 이다.
상세는 [`scenarios/scenario_list.md`](scenarios/scenario_list.md) §2 암호 축 참조.

| #      | 요구사항                                  | 시나리오       | 선행 조건 |
|:------:| ------------------------------------- | ---------- | ----- |
| **C0** | **공격군 — ROC 사칭 위조 명령 주입** (인증 부재 실증)  | SC-VDE-005 | 없음    |
| **C1** | ECDSA 불리틴 서명·검증 (브로드캐스트 인증 기준선)       | SC-VDE-008 | B5    |
| **C2** | **TESLA 키 체인 생성·커밋먼트·80비트 MAC**       | SC-VDE-008 | 없음    |
| **C3** | 지연 키 공개 스케줄러 (Tint, 공개지연 d)           | SC-VDE-008 | C2    |
| **C4** | 손실률별 인증 성공률·지연 시뮬레이터                  | SC-VDE-008 | C1·C3 |
| **C5** | 오버헤드·슬롯 측정 (관찰 도구 연동)                 | SC-VDE-008 | A8    |
| **C6** | 송신원 정합성·행위 기반 이상 탐지 (관찰 도구 확장)      | 전체         | A8    |

**C0이 지금 착수 가능한 공격 실증이다.** SC-VDE-005 정상군이 완료됐으므로, ROC MMSI를
사칭해 위조 항로를 주입하는 공격 행위자만 만들면 **인증 부재의 결과(항적 이탈)를 즉시
관찰 도구로 보여줄 수 있다.** 새 링크 기능도, 표준 밖 정의도 필요 없다.

> **풀스택 원칙 (CPDLC 연구 시사점).** C0의 최종 형태는 비트 레벨 주입이 아니라 **실물
> 링크·RF를 통과한 위조**여야 한다 — 응용 계층만으론 실제 공격이 성립하지 않음이 CPDLC
> 연구에서 확인됐다(scenario_list §0). 따라서 C0은 링크 계층 팀원과의 협업 지점이며,
> RF 벤치 테스트([`rf_normal_cycle_test.md`](rf_normal_cycle_test.md))의 공격군 확장으로 간다.

**C6은 새 방어 축이다.** 표준 암호 배포가 요원한 동안(§4.3), 송신원 물리 지문·위치
정합성으로 위조를 탐지하는 중간 방어. 우리 관찰 도구가 이미 그 초기 형태다.

**C1~C5는 SC-VDE-008(브로드캐스트 인증 개발)의 구현 요구다.** 표준상 불리틴 서명은
링크계층 책임이나, **방식 비교·파라미터 선정·시뮬레이션 평가는 링크 RF 구현과 독립적**
이며 이것이 암호통신 개발의 핵심 산출물이다. RF 통합 담당은 미결(§6).

---

## 3. 용량 계산 — Link ID 17 기준

M.2092-2 Table 8: Link ID 17 = TER-MCS-1.100, 100 kHz, π/4-QPSK, FEC 1/2, **packet 234 byte**

| 경로                            | 헤더   | payload 가용 |
| ----------------------------- | ----:| ----------:|
| Type 92 (short data, ACK 요구)  | 13 B | **217 B**  |
| Type 93 (short data, ACK 불요)  | 12 B | **218 B**  |
| Type 74 (start fragment)      | 15 B | **215 B**  |
| Type 75/76 (continuation/end) | 14 B | **216 B**  |

> 모두 CRC-32 4 byte를 뺀 값이다.

### 3.1 시나리오별 소요

| 시나리오           | payload | segment 오버헤드 | 합계      | fragment 수         |
| -------------- | -------:| ------------:| -------:| ------------------:|
| 인증서 (signed)   | 1,024 B | 79 B         | 1,103 B | **6**              |
| 인증서 (unsigned) | 1,024 B | 4 B          | 1,028 B | 5                  |
| AIS 인증값        | ~70 B   | —            | ~70 B   | **1** (short data) |
| 운항 메시지 (위치보고)  | 17 B    | —            | 17 B    | **1** (short data) |

### 3.2 Link ID별 비교

| Link ID | 대역폭     | 변조       | packet | 인증서 6 fragment 가능    | 비고          |
|:-------:| -------:| -------- | ------:|:--------------------:| ----------- |
| **11**  | 25 kHz  | π/4-QPSK | 54 B   | ✗ (약 25 fragment 필요) | **BBSC 강제** |
| **17**  | 100 kHz | π/4-QPSK | 234 B  | ✓                    | 기본          |
| **19**  | 100 kHz | 16-QAM   | 702 B  | ✓ (2 fragment)       | 최고 처리율      |

**Link ID 11에서는 signed segment 오버헤드(79 B)가 packet(54 B)보다 크다** — 즉
**주소지정 signed segment 한 개는 한 packet에 못 들어간다.** (브로드캐스트 불리틴은
다른 방식으로 fragment에 나눠 실어 인증한다 → §4.3에서 다룬다.)

---

## 4. 암호 요구사항

M.2092-2 §A4-4.15가 지정한 값이며 **선택의 여지가 없다.**

| 항목        | 값                                    |
| --------- | ------------------------------------ |
| PKI · 인증서 | ITU-T X.509 (10/2016)                |
| 서명 알고리즘   | ECDSA                                |
| 곡선        | **secp256r1**                        |
| 공개키 크기    | 256 bit                              |
| 다이제스트     | **SHA-256**                          |
| 서명 인코딩    | IEEE P1363 Fixed-Field Concatenation |
| 서명 필드     | 512 bit (64 byte)                    |
| 인증서 지문    | signatureValue 최하위 48 bit (6 byte)   |
| 오프라인 대비   | 인증서·**CRL 저장 필수**                    |

Python 구현은 `cryptography` 라이브러리로 그대로 대응된다
(`ec.SECP256R1`, `ec.ECDSA(hashes.SHA256())`).

### 4.1 검증 실패 시 동작 — 규정되어 있다

> "In case the verification of the signature fails … the message cannot be trusted.
> For Link Layer messages … the system shall act as if it **never had received the message**."

→ 응용도 같은 원칙을 따른다. 이미 `mass.py`의 `_evaluate()` → `_apply_route()` 순서로
구조에 반영되어 있다.

### 4.2 표준이 비워둔 것

- **CA 주체** — *"국제기구가 CA 역할을 한다고 가정한다"* 로만 서술
- **키 배포·초기 provisioning** — 정의 없음
- **신뢰 앵커 주입 방법** — 정의 없음

→ **본 프로젝트의 제안 대상.** 구상도의 *"우리가 제안한다"* 가 가리키는 지점.

### 4.3 표준 암호가 제약에 부딪히는 곳 — 개발 초점

§4의 ECDSA 지정은 **주소지정 트래픽에는 잘 맞는다**(Link ID 17/19에 서명이 여유롭게
들어간다). 그러나 **브로드캐스트에서 부딪힌다.**

```text
BBSC(불리틴 보드)는 Link ID 11 강제      §A4-4.8.1
불리틴 인증 = 지문 6 B + ECDSA 64 B = 70 B    Table 43 필드 7·8
Link ID 11 packet = 54 B
   → 70 B > 54 B → 연속 fragment(Type 21/22)로 분할
```

즉 제약 채널에서 메시지별 ECDSA는 **불가능하진 않으나 비싸다** — 슬롯을 더 쓰고,
fragment 하나만 유실돼도 인증이 실패하며, 수신 선박마다 불리틴당 비대칭 검증을 한다.

**대안은 TESLA**(지연 키 공개, 대칭 MAC). 리포 내 논문
*Secure Maritime VHF Broadcast Authentication Using TESLA* (Lee & Lee, IEEE Access 2025.12)이
VDES 브로드캐스트를 대상으로 80비트 MAC·Tint 500 ms·d=2를 권장하고 시뮬레이션 방법론을
제공한다. **두 방식의 정량 비교가 SC-VDE-008이며, 본 과제의 암호 개발 산출물이다.**

> **문제 정의 주의.** §3.2의 "한 packet에 못 들어간다"는 **주소지정 signed segment**
> 기준이다. 브로드캐스트 불리틴은 fragment로 나눠 인증한다 — 정확한 문제는 "불가능"이
> 아니라 **"비용"**이다. 그리고 그 비용을 지금 평가해야 하는 이유는, 표준 암호 실배포가
> "10년 뒤"이기 때문이다(SC-VDE-008 §1.1, CPDLC 연구).

---

## 5. 인증이 붙는 곳과 붙지 않는 곳

취약점 분석의 표적이다.

| 구분        | 대상                                | 근거                       |
| --------- | --------------------------------- | ------------------------ |
| 인증 있음     | TBB (Type 20)                     | 지문 6 B + 인증·무결성 시퀀스 64 B |
| 인증 있음     | Signed segment                    | Table 55                 |
| **인증 없음** | Unsigned segment                  | Table 54                 |
| **인증 없음** | 자원할당(4) · 자원요청(90) · ACK/NACK(13) | 서명 필드 **0개** (원문 확인)     |
| **인증 없음** | Short data message (92/93)        | segment 계층 밖             |

**아래 세 줄이 CIV(Context Integrity Violation) 성립 조건**과 정확히 일치한다 —
인증되지 않은 메시지가 상대의 내부 상태를 바꾼다(`mass_roc_background.md` §2.4).

---

## 6. 미해결 사항

| #   | 항목                             | 영향                            | 결정 주체   |
|:---:| ------------------------------ | ----------------------------- | ------- |
| 1   | 운항 메시지용 `data_type` 부재         | 위치보고·항로지시를 Service로 보낼 분류가 없음 | API 소유자 |
| 2   | `ReceivedMessage`에 검증 상태 필드 없음 | 응용이 인증 여부를 알 수 없음 (B6)        | API 소유자 |
| 3   | data session 구현 일정             | SC-VDE-001 착수 시점 결정           | 링크 계층   |
| 4   | Link ID 11 병행 지원               | SC-VDE-002 가능 여부              | 링크 계층   |
| 5   | 신뢰 앵커 provisioning 방식          | 부트스트랩 설계                      | 프로젝트    |
| 6   | VPFI 시험값 배정                    | 낮음. 현재 임의값 사용 중               | 프로젝트    |
| 7   | **TESLA 링크계층 구현 담당**          | SC-VDE-008 범위 — 시뮬 평가는 본 과제, RF 구현 미정 | 프로젝트·링크 |
| 8   | **나머지 2개 시나리오 선별**            | 서비스별 고안 → 우선순위 평가 (scenario_list §0·§1) | 프로젝트 |
| 9   | **MASS 조작 데이터 구현 방법**         | 선정될 공격 시나리오 실증 방식 — **멘토 자문 요청 중** | 멘토·프로젝트 |
| 10  | **Route Plan 기밀성 → 암호화 범위**   | S-421 기밀성 상(표적화). 인증 외 **암호화 포함 여부** — SECOM 후보 | 프로젝트 |

---

## 7. 지금 착수 가능한 것

개발 대상이 **확정 1개(인증서 송수신) + 선정 2개**로 정리되면서 우선순위가 바뀌었다
(`scenarios/scenario_list.md` §0). 확정 시나리오의 선행 작업과, 링크 RF와 무관한 암호
개발을 먼저 놓는다.

| 순위 | 작업 | 시나리오 | 왜 지금인가 |
| :-: | --- | --- | --- |
| **1** | **X.509 DER·인증서·CRL 저장소 (B2~B4)** | **SC-VDE-001 (확정)** | 확정 시나리오의 선행 작업. 링크 data session과 무관하게 지금 가능 |
| **2** | **TESLA 키체인·MAC (C2)** | SC-VDE-008 | 001의 짝(방송 인증). 링크 RF와 독립. 논문 권장값(80b·500ms·d2)으로 착수 가능 |
| 3 | 지연 키 공개·성공률 시뮬 (C3·C4) | SC-VDE-008 | C2 후. ECDSA 기준선(C1)과 비교 |
| 4 | 공격군 — ROC 사칭 위조 항로 (C0) | SC-VDE-005 | 정상군 완료. **MASS 조작 데이터 후보** — 선정·멘토 자문 후 구현 |
| 5 | 제어권 전환·비상 명령 메시지 정의 | SC-VDE-006/007 | 선정될 경우. 표준 메시지 없어 우리가 정의(background §2.5~2.6) |

> **선정 절차가 병행된다.** 위는 "지금 코드로 착수 가능한 것"이고, 나머지 2개 시나리오의
> 확정은 §1 인벤토리 기준 평가 + MASS 조작 데이터 멘토 자문을 거친다(§6-8·9).
>
> **완료(서비스 카탈로그 메시지 계층)**: 서비스별 시나리오 고안(7종)과 함께 S-124 항행경보
> (Message ID 5, 육상국 방송→MASS 반영까지)·S-104 환경정보(ID 6)·BUFR 기상관측(ID 7)의
> 압축 스키마를 구현했다. 상세는 `docs/compact_message_schemas.md`,
> `scenarios/scenario_list.md` §3.2(구현 진척).

**1번이 가장 값싸고 효과가 크다.** SC-VDE-005 정상군(대조군)이 이미 있으므로, 공격군을
붙이면 관찰 도구 비교 모드로 **"인증이 없어서 배가 항로를 벗어난다"** 를 한 장으로 보여준다.
이것이 '왜 암호가 필요한가'의 실증이고, **2번(TESLA 개발)이 '그럼 어떻게 막는가'의 답**이다.

간접 위험 시나리오(001·002·003·008)는 영향을 보이려면 직접 시나리오를 무대로 빌려야 하므로
(`scenario_list.md` §5.1), **SC-VDE-005가 다른 시나리오의 전제이기도 하다.**

---

## 참고

- ITU-R M.2092-2 — §A4-4.2.5, §A4-4.3, §A4-4.7, §A4-4.8.1, §A4-4.9, §A4-4.15,
  §A4-4.16, §A4-4.29, §A4-5, §A4-6, Table 8·35·38~41·43·47·49~51·54·55
- Lee & Lee, *Secure Maritime VHF Broadcast Authentication Using TESLA*, IEEE Access, 2025.12
  — 브로드캐스트 인증 대안(TESLA)의 설계·시뮬레이션 근거 (SC-VDE-008)
- Ziazi et al., *Sliding into the Flight Deck's DMs: Practical Message Attacks on CPDLC*,
  USENIX Security 2026 — 동형 체계(항공 관제-조종 데이터링크) 공격 연구. 풀스택 실증
  원칙, 상태 불일치 DoS, 물리계층 방어, "표준 암호는 10년 뒤" 근거
- `docs/vdeter_mass_roc_api_guide.md`
- `docs/mass_roc_background.md` — 계층·위협 모델·인증 근거
- `docs/mass_roc_control_and_ack_message_format.md` — MASS 조작 데이터·ACK 형식 (S-421 기반, 멘토 자문 반영)
- `docs/vdeter_mass_roc_api_guide.md` — **VDETER 연동 API** (팀원 제공): VdeService.submit, PayloadFormat, 지원 format은 인증서·AIS인증값 둘뿐, MASS broadcast·route/경보 format 미지원
- `references/VDE-TER_Faraday_Cage_구축계획서.html` — RF 실험용 Faraday Cage 계획
- `docs/mass_roc_message_set.md` — 응용 메시지 설계
- `docs/scenarios/scenario_list.md` — 시나리오 목록 (§2 암호 축)
- `docs/scenarios/SC-VDE-008-broadcast-authentication.md` — 암호 개발 시나리오
