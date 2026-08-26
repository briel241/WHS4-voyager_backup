# SC-VDE-005 — ROC 항로 지시와 MASS 항로 추종

> 작성 형식은 [`TEMPLATE.md`](TEMPLATE.md)를 따른다.
> 이 시나리오는 **이미 구현되어 동작 중**이므로 아래 수치는 전부 실측값이다.

---

## 1. 개요

| 항목 | 내용 |
| --- | --- |
| 시나리오 ID | `SC-VDE-005` |
| 목적 | ROC가 지시한 항로를 MASS가 검증·수락하고 실제로 따라간다 |
| 분류 | 소용량 · 양방향 (주기적 보고 + 단발 명령) |
| `data_type` | **미배정** — API 계약에 운항 메시지 분류가 없다(§9) |
| 위험 등급 | **최상 · 직접** |
| 상태 | **구현됨** (`common/`·`MASS/`·`ROC/`, 시험 276항목 통과) |

**이 시나리오의 위치** — 위험이 최상이면서 **지금 당장 공격 실증이 가능한 유일한 직접
시나리오**다. 간접 위험 시나리오(인증서·AIS 인증값·TBB)는 영향을 보이려면 이 무대를 빌려야
한다(`scenario_list.md` §4.1).

---

## 2. 참여자별 행위

| 참여자 | 식별자 | 하는 일 |
| --- | --- | --- |
| MASS | MMSI `440000001` | 2초마다 위치를 브로드캐스트한다. 항로 지시를 받으면 검증하고 응답한 뒤 그 항로를 따라간다 |
| ROC | MMSI `440000002` | 위치보고를 받아 항적을 갱신한다. 항로를 지시하고 응답을 기다린다. 보고가 끊기면 통신 두절로 판정한다 |
| 다른 MASS | (선택) | 위치보고 브로드캐스트를 함께 수신한다 |
| 공격자 | — | §6에서만 등장 |

시간 순으로 쓰면 다음과 같다.

- **MASS가** 2초마다 위치·침로·속력·자율모드를 **브로드캐스트한다**.
- **ROC가** 그것을 받아 **항적을 갱신한다**.
- **ROC가** waypoint 목록으로 **항로를 지시한다**(지정 수신, 응답 요구).
- **MASS가** 지시를 **검증한다**. 통과한 경우에만 항로를 적용한다.
- **MASS가** 수락 여부를 **응용 ACK로 답한다**.
- **MASS가** 항로를 향해 **침로를 바꾸고 waypoint를 차례로 통과한다**.
- **ROC가** 이후 위치보고에서 **침로 변화를 관측한다**.
- 보고가 끊기면 **ROC가 통신 두절로 판정한다**.

---

## 3. 통신 설정값

| 항목 | 값 |
| --- | --- |
| 물리 채널 | 100 kHz |
| Link ID | **17** (TER-MCS-1.100, π/4-QPSK, FEC 1/2, packet 234 B) |
| TBB 동기화 | 불필요 (제어국 서비스 영역 밖 직접 통신 가정) |
| 시간 동기화 | 필요 (TDMA slot 정렬) |
| 채널 조건 | 동축 루프백 · 손실 없음 (기준선) |
| 위치보고 주기 | **2.0 s** |
| 통신 두절 판정 | **10.0 s** 미수신 |
| VPFI | `0xFF01` (시험용 임의값) |

Link ID 11로도 동작하나 **waypoint가 2개로 제한**된다(§5.3, S-421 제어 필드 확장 후).

---

## 4. 송수신 데이터

### 4.1 Position Report (MASS → 브로드캐스트)

| 항목 | 내용 |
| --- | --- |
| 형식 | AIS Message 1/2/3 Common Navigation Block 차용 |
| 크기 | **17 byte** (고정) |
| 내용 | 항행상태 · 선회율 · SOG · 위치정확도 · 경도 · 위도 · COG · 선수방위 · 시각 · 기동지시자 · RAIM · **자율모드** |
| 표준 근거 | ITU-R M.1371 (108 bit) |
| 프로젝트 정의 | **Autonomy Mode 3 bit** — 표준에 자율운항을 나타내는 값이 없다 |

### 4.2 Route Instruction (ROC → MASS)

| 항목 | 내용 |
| --- | --- |
| 형식 | IFM DAC=001/FI=28 차용 + S-421 조작 데이터 제어 필드(heading·fallback·turn radius·SOG·leg) |
| 크기 | **23 byte~** (waypoint 수에 따라 가변, §5.3) |
| 내용 | Linkage ID · 발신자 분류 · 항로 종류 · 시작 시각 · **유효기간** · waypoint 목록 |
| 표준 근거 | IMO SN.1/Circ.289 Table 13.2 / 13.3 |

핵심 필드 셋.

- **`Route Type = 1` (mandatory route)** — 권고가 아닌 강제 항로
- **`Route Type = 31` 또는 `Duration = 0`** — 항로 취소
- **`Duration`** — 유효기간(분). **재전송 방어에 쓰인다**

### 4.3 Application ACK (MASS → ROC)

| 항목 | 내용 |
| --- | --- |
| 형식 | IFM FI=5 개념 차용 |
| 크기 | **6 byte** |
| 내용 | Linkage ID · 상태(S-421 routeInfoStatus: acknowledged/errors/incomplete/route issues) · 사유 |
| 프로젝트 정의 | 상태·사유 값 — FI=5의 비트 구조는 ITU-R M.1371 Annex 5 소관이며 미확보 |

> **링크 ACK와 응용 ACK는 다른 층위다.** 전자는 "전파가 도달했다", 후자는 "명령을 수락했다".
> SN.1/Circ.289도 둘을 혼동하지 말라고 명시한다.

---

## 5. 메시지 흐름

### 5.1 전송 경로

| 메시지 | 방향 | VDE Type | 근거 |
| --- | --- | :-: | --- |
| Position Report | MASS → 전체 | **93** | ACK 불요 · 브로드캐스트 가능 |
| Route Instruction | ROC → MASS | **92** | ACK 요구 · 지정 수신자 (broadcast 금지) |
| Application ACK | MASS → ROC | **93** | ACK 불요 |

### 5.2 흐름

```text
MASS                                                    ROC
 │                                                       │
 │──[1] Position Report  Type 93  17 B ────────────────▶ │  항적 갱신
 │      (2초마다 반복)                                     │
 │                                                       │
 │◀─[2] Route Instruction  Type 92  55 B ─────────────── │  waypoint 3개 (Link ID 17)
 │                                                       │
 │      검증 → 통과한 경우에만 항로 적용                       │
 │                                                       │
 │──[3] Application ACK  Type 93  6 B ─────────────────▶ │  acknowledged
 │                                                       │
 │      침로 변경 · waypoint 추종                           │
 │                                                       │
 │──[4] Position Report (변경된 COG) ──────────────────▶ │  변화 관측
```

### 5.3 크기 실측

payload → Type 92/93 → Temporary Burst 순이다.

> ⚠️ 크기 갱신: 항로 지시가 S-421 조작 데이터 형식으로 확장됐다(heading setpoint·
> fallback·waypoint별 turn radius·계획 SOG·leg geometry 추가).
> 상세 형식은 [`../mass_roc_control_and_ack_message_format.md`](../mass_roc_control_and_ack_message_format.md).
> 아래는 확장 후 실측값이다.

| 메시지 | payload | 링크 메시지 | Burst |
| --- | ---: | ---: | ---: |
| Position Report | 17 B | 29 B | **33 B** |
| Route Instruction (WP 1) | 23 B | 36 B | 40 B |
| Route Instruction (WP 2) | 32 B | 45 B | 49 B |
| Route Instruction (WP 3) | 42 B | 55 B | **59 B** |
| Route Instruction (WP 4) | 51 B | 64 B | 68 B |
| Route Instruction (WP 16) | 167 B | 180 B | 184 B |
| Application ACK | 5 B | 17 B | **21 B** |

Link ID별 waypoint 수용 한계.

```text
Link ID 11  가용  37 B  →  waypoint 최대  2개
Link ID 17  가용 217 B  →  waypoint 최대 16개 (표준 상한)
Link ID 19  가용 685 B  →  waypoint 최대 16개 (표준 상한)
```

**메시지 포맷은 Link ID에 따라 바뀌지 않는다.** waypoint 개수만 조절한다.

---

## 6. 위험 (Risk)

**위험 등급: 최상 · 직접**

조작하면 **배가 즉시 다르게 움직인다.** 관찰 도구의 항적 한 장으로 영향이 드러난다.

| 공격 | 방법 | MASS에 미치는 영향 | 등급 |
| --- | --- | --- | :-: |
| **데이터 위조** | 위조된 waypoint로 항로 지시 | **항로 이탈 → 좌초·충돌** | **최상** |
| **신분 사칭** | 공격자가 ROC MMSI로 항로 지시 | 동일 — 배가 공격자 항로를 따른다 | **최상** |
| **재전송** | 폐기된 옛 항로 지시를 재전송 | 취소된 항로로 되돌아간다 | 상 |
| **차단 (DoS)** | 항로 지시 차단 | 지시 미달. MASS는 옛 항로를 유지 | 상 |
| **차단** | 위치보고 차단 | **ROC가 선박을 보지 못한다** → 감독 상실 | 상 |
| **위조** | 위조된 위치보고 | ROC 항적이 실제와 어긋난다 → 오판 | 상 |
| 탈취 | 위치·항로 도청 | 항적 노출 (기밀성 문제, 안전 직결 아님) | 중 |

### 6.1 현재 방어 상태

**항로 지시에는 인증이 없다.** Type 92 Short Data Message는 **segment 계층 밖**이므로
표준 방식의 서명을 붙일 수 없다(`mass_roc_background.md` §6.4).

지금 있는 방어는 셋뿐이며, 모두 **위조를 막지 못한다.**

| 장치 | 막는 것 | 못 막는 것 |
| --- | --- | --- |
| CRC-32 | 우연한 오류 | **고의적 위조** |
| `Duration` 유효기간 | 오래된 재전송 | 유효기간 안의 위조 |
| MASS의 형식 검증 | 잘못된 형식 | 형식이 맞는 위조 |

→ **신분 사칭과 데이터 위조가 그대로 성립한다.** 이것이 실증 대상이다.

### 6.2 무대로서의 역할

간접 위험 시나리오는 이 시나리오를 빌려 영향을 보인다.

```text
인증서·CRL 오염(SC-VDE-001)  →  위조 항로가 검증 통과  →  항적 이탈(여기)
위조 AIS(SC-VDE-003)         →  회피 판단 오류        →  항적 이탈(여기)
```

---

## 7. 예상 결과

관찰 도구(`common/observe/`)로 확인 가능한 형태로 쓴다.

- ROC의 항적이 **2초 간격으로 갱신**된다
- 항로 지시 후 **5초 안에** `accepted` 응답이 돌아온다
- 지시 이후 **COG가 변한다** — 시계열 그래프에서 보인다
- waypoint를 **순서대로 통과**한다. 남은 개수가 3 → 2 → 1 → 0으로 줄어든다
- 계획 항로(점선)와 실제 항적(실선)이 **해도에서 겹친다**
- 위치보고가 끊기면 **10초 안에** 통신 두절로 판정된다
- 재개되면 두절이 해제된다

### 7.1 기준선 실측 (600초 시뮬레이션)

```text
    20s   ROC → MASS 항로 지시 (Type 92, Burst 59 B); MASS → ROC ACK acknowledged (22 B)
   128s   WP1 통과   COG 354°
   261s   WP2 통과   COG  19°
   404s   항로 완주  COG   1°
   430s   MASS 송신 차단 시작
   439s   통신 두절 판정          ← 차단 후 9초
   490s   위치보고 재개

  관측 위치보고 271건 · 최대 관측 공백 62s · 송신 후 유실 30건
  총 송신 시도 303회 · 전파 나간 양 10,003 byte
```

---

## 8. 실패 조건

실측 결과다. **모든 거부 사례에서 기존 항로가 유지된다.**

| 조건 | ACK 상태 | 사유 | 기존 항로 |
| --- | --- | --- | :-: |
| waypoint 없음 | `incomplete` | route unsuitable | **유지** |
| Route Type 미지정 | `incomplete` | route unsuitable | **유지** |
| 좌표가 not available | `incomplete` | route unsuitable | **유지** |
| 취소 지시 (Type 31 / Duration 0) | `acknowledged` | — | 해제 |
| 유효기간 경과 | — | — | 자동 해제 |
| 다른 응용의 payload | 응답 없음 (무시) | — | **유지** |
| VPFI 불일치 · 형식 손상 | 응답 없음 (무시) | — | **유지** |

### 8.1 검증 순서가 구조로 보장된다

M.2092-2 §A4-4.15가 요구하는 원칙 — *"검증에 실패하면 수신하지 않은 것처럼 행동하라"* —
을 코드 구조로 지킨다.

```python
decision = self._evaluate(message)   # 판정만 한다. 상태를 바꾸지 않는다
if decision.accept:
    self._apply_route(message, now)  # 통과한 경우에만 적용
```

**인증은 나중에 `_evaluate()` 안에 들어간다.** 자리가 이미 마련되어 있다.

### 8.2 유효기간 실측

```text
duration = 1분 지시
   30s 경과 → 항로 유지
   61s 경과 → 항로 자동 해제
```

---

## 9. 로그 내용

관찰 도구가 남기는 항목이다.

| 항목 | 기록 위치 | 용도 |
| --- | --- | --- |
| 관측 시각 · 위치 · COG · SOG · 자율모드 | `Sample` | 항적·시계열 |
| 송수신 시각 · Source/Destination ID | `Transfer` | 사칭 탐지 |
| VDE Type (92/93) | `Transfer` | 경로 확인 |
| payload · Burst 크기 | `Transfer` | 용량 검증 |
| **송신 결과** (delivered / lost / no-slot / rejected) | `Transfer.outcome` | **공격 유형 구분** |
| **재시도 횟수 · 실패 사유** | `Transfer.attempts` / `.reason` | 자원 고갈 추적 |
| 명령 발행·응답, waypoint 통과, 통신 두절 | `Event` | 시간축 사건 |
| 거부·무시 횟수 | `MassAgent.rejected_count` / `.ignored_count` | 공격 흔적 |

산출물: 콘솔 시간표 + HTML 보고서(항적 해도 · 침로/속력 시계열 · 사건표 · 메시지표).
**비교 모드**로 정상 대조군과 공격군을 같은 축에 겹칠 수 있다.

### 9.1 미해결 사항

| # | 항목 | 영향 |
| :-: | --- | --- |
| 1 | **`data_type` 미배정** | API 계약의 네 분류(TBB·AIS 인증값·전자해도·인증서)에 운항 메시지가 없다. 실제 Service로 보낼 수 없다 — **API 소유자 결정 필요** |
| 2 | 검증 결과 전달 경로 | 링크 계층이 서명을 검증해도 그 결과가 응용에 오지 않는다. `ReceivedMessage`에 상태 필드가 필요하다 |
| 3 | Type 92 링크 ACK 미구현 | 지금은 응용 ACK만 있다. 링크 ACK·재전송은 링크 계층 몫 |

---

## 참고

- ITU-R M.1371 — AIS Message 1/2/3
- IMO SN.1/Circ.289 — Table 13.2 / 13.3 (Route information)
- ITU-R M.2092-2 — §A4-4.2.5, §A4-4.15, Table 8 · 35 · 40 · 41
- 구현: `MASS/mass_agent.py`, `ROC/roc_agent.py`, `common/messages/route_instruction.py`
- 시험: `tests/test_mass_roc_agents.py`, `test_mass_roc_link_integration.py`
- 실행: `/opt/homebrew/bin/python3 apps/mass_roc_sim.py`
