# SC-VDE-006 · MASS-ROC 원격 조종

> 노선(SC-VDE-005)이 교환된 상황을 전제한 **하위 시나리오**다. ROC가 조종 명령
> (목표 침로·복귀 모드·속력)을 내리고 MASS가 검증·반영한다.
>
> 노선과 **같은 메시지**(Message Format 2)를 쓴다. Heading Setpoint와 Fallback State를
> 채웠는지로만 갈리고, 링크 계층 codec은 하나다.

---

## 1. 개요

| 항목 | 값 |
| --- | --- |
| 시나리오 ID | **SC-VDE-006** |
| 서비스 | MASS 조작 데이터 |
| 규격 | S-421 (S-100) 조작 데이터 · 형식은 [`../mass_roc_control_and_ack_message_format.md`](../mass_roc_control_and_ack_message_format.md) |
| 목적 | ROC가 조종을 지시하고 MASS가 실제로 조타한다 |
| 위험 | **최상** (직접) — 조작하면 배가 즉시 다르게 움직인다 |
| 상위 시나리오 | [SC-VDE-005 항로 지시와 추종](SC-VDE-005-route-guidance.md) |
| 구현 상태 | ✅ 운영 종단 (TEST scope) |

**노선(005)과의 차이**

| | SC-VDE-005 노선 | SC-VDE-006 원격 조종 |
| --- | --- | --- |
| 지시하는 것 | 따라갈 waypoint 열 | **목표 침로·복귀 모드** |
| Heading Setpoint | 비움 (3600 = N/A) | **채움** (0.1° 단위) |
| Fallback State | 비움 (0 = none) | **채움** (투묘·DP·표류) |
| waypoint | 필수 | **선택** — 침로 지시 자체가 명령이다 |
| MASS 운항 모드 | 감독 자율 (MON) | **원격 제어 (RC)** |

---

## 2. 참여자별 행위

| 참여자 | MMSI | 행위 |
| --- | --- | --- |
| ROC | 440000002 | 조종 명령 발행 · 응용 ACK 상관 · 항적 관측 |
| MASS | 440000001 | 검증 → 통과 시 조타 반영 → 응용 ACK · 위치보고 |

**MASS의 처리 순서** — 검증이 끝난 뒤에만 상태를 바꾼다.

1. 권한 판정 (`authority_policy`, 기본값은 누구든 허용 — §6.1)
2. 스키마 검사 (전선에 실을 수 없는 값 거부 — 승인된 codec과 같은 규칙)
3. 취소 판정 (Message Linkage ID가 가리키는 지시만 해제)
4. 누락 검사 (침로·Fallback·waypoint가 모두 없으면 거절)
5. 경로 형상 검사 (연속 중복 waypoint 거부)
6. 통과 → 조타 반영 + 운항 모드 RC + 응용 ACK

---

## 3. 통신 설정값

| 항목 | 값 |
| --- | --- |
| 물리 채널 | 100 kHz |
| Link ID | **11 또는 17** — 조종 단독 13 B라 11로도 여유가 크다 |
| 채널 조건 | 동축 루프백 · 손실 없음 (기준선) |
| 위치보고 주기 | AIS Class A 표준 (12노트면 10 s · 선회 중 3⅓ s) |
| 제어 링크 두절 판정 | **30.0 s** 무응답 → 사전 정의 fallback + 제어권 복귀 |
| payload_format | `sc_vde.route_control.command` (VPFI `ff 7f` / MF 2) |
| 응용 ACK | `sc_vde.route_control.ack` (MF 3) — **별도 submit** |

> 전송 방식은 크기가 가른다. wire 32 B(waypoint 2개)까지는 `ack_required`와 무관하게
> Short Data이고, **42 B(waypoint 3개)부터 addressed Data Session**이 선택된다
> (`../vdeter_mass_roc_api_guide.md` §8.3).

---

## 4. 송수신 데이터

| 필드 | bit | 내용 |
| --- | ---: | --- |
| Message Linkage ID | 10 | ACK 상관 ID |
| Sender Classification | 3 | 0=ship, 1=authority |
| Route Type | 5 | 조종은 통상 2(recommended) |
| Start / Duration | 20 / 18 | 유효 시작·지속(분) |
| **Heading Setpoint** | 12 | **목표 침로. ×10 → 0.1° · 3600=N/A** |
| **Fallback State** | 3 | **0=none · 1=Anchoring · 2=DynamicPositioning · 3=Drifting** |
| Number of Waypoints | 5 | 0–16 |
| (waypoint마다) | 77 | 경위도 · 선회반경 · **계획 SOG** · leg 기하 |

**실측 크기** (구현 기준, wire = application + VPFI/MF 3 B)

| 형태 | application | wire |
| --- | ---: | ---: |
| 조종 단독 (waypoint 0개) | 10 B | **13 B** |
| 조종 + waypoint 2개 | 29 B | **32 B** |
| 응용 ACK | 3 B | **6 B** |

전선 형식 상세는 [`../전선형식_원격조종_SC-VDE-006.md`](../전선형식_원격조종_SC-VDE-006.md).

---

## 5. 메시지 흐름

### 5.1 전송 경로

| 메시지 | 방향 | payload_format | 링크 ACK | VDE Type |
| --- | --- | --- | :-: | :-: |
| 위치보고 | MASS → ROC (addressed) | `sc_vde.position_report` (MF 1) | 불요 | **93** |
| 조종 명령 | ROC → MASS (addressed) | `sc_vde.route_control.command` (MF 2) | 요구 | **92** |
| 응용 ACK | MASS → ROC (addressed) | `sc_vde.route_control.ack` (MF 3) | 불요 | **93** |

Type은 `ack_required`가 가른다 — 92 = Short data with ACK, 93 = Short data without ACK.
자세한 근거는 [`SC-VDE-005-route-guidance.md`](SC-VDE-005-route-guidance.md) §5.1.

### 5.2 흐름

```text
MASS                                                    ROC
 │                                                       │
 │──[1] Position Report  Type 93 · MF 1  14 B ─────────▶ │  항적 갱신
 │                                                       │
 │◀─[2] Control Command  Type 92 · MF 2  10 B ────────── │  침로 92.5° · 투묘
 │                                                       │
 │      검증 → 통과한 경우에만 조타 반영                        │
 │      운항 모드 MON → RC (제어권 이전)                      │
 │                                                       │
 │──[3] Application ACK  Type 93 · MF 3  3 B ──────────▶ │  acknowledged
 │                                                       │
 │      지시 침로로 선회 (선회율 상한 20°/분)                   │
 │                                                       │
 │──[4] Position Report (변화한 COG · Autonomy=RC) ─────▶ │  조타 관측
```

### 5.3 Fallback을 지시한 경우

Fallback은 항행을 멈춘다. 지시받으면 남은 항로와 목표 침로를 버리고 그 자리를 유지한다.

| Fallback | 속력 | Navigation Status (M.1371 Table 45) |
| --- | ---: | --- |
| Anchoring | 0 | **1** at anchor |
| DynamicPositioning | 0 | **0** under way using engine (위치 유지) |
| Drifting | 0 | **2** not under command |

> AIS에는 DP 전용 status가 없다. 위 대응은 본 프로젝트가 정한 것이다.

### 5.4 제어 링크가 끊긴 경우

IMO MASS Code는 정상 운용 조건을 유지할 수 없을 때 **사전 정의된 fallback 상태**로
전환하도록 요구한다. 실제 운용에서도 연결이 기준 아래로 떨어지면 선박이 fallback에
들어가고 제어권이 본선으로 돌아온다.

```text
RC 상태에서 제어국 무응답 30 s
        ↓
사전 정의 fallback 진입 (기본 DynamicPositioning)
        ↓
운항 모드 RC → MON  (제어권 본선 복귀)
```

감독 자율(MON) 중에는 발동하지 않는다. 원격 제어 중일 때만 의미가 있기 때문이다.

---

## 6. 위험 (Risk) ★

### 6.1 인증이 없다

조종 명령에도 **송신자 인증이 없다.** 노선(SC-VDE-005 §6.1)과 같은 이유이며,
조종은 배를 직접 움직이므로 파급이 더 크다.

| 장치 | 막는 것 | 못 막는 것 |
| --- | --- | --- |
| CRC-32 | 우연한 오류 | **고의적 위조** |
| `Duration` 유효기간 | 오래된 재전송 | 유효기간 안의 위조 |
| MASS의 형식 검증 | 잘못된 형식 | 형식이 맞는 위조 |

현재 `MassAgent.authority_policy`는 기본값이 `None`(누구든 허용)이다. 이는 VDE-TER
표준에 송신자 인증이 없는 현 상태를 그대로 재현한 것이며, **그 부재가 연구 대상**이다.
방어 실험에서는 이 자리에 판정 함수를 넣으면 `errors / not authorized`로 거절된다.

### 6.2 조종 명령이 실제로 배를 움직인다

목표 침로·Fallback·계획 속력은 **화면 표시용이 아니라 실제 조타 입력**이다.

- 위조 침로 → 배가 그 침로로 선회한다
- 위조 Fallback → 배가 **항행을 멈춘다** (투묘·DP·표류)
- 위조 속력 → 배가 그 속력으로 바뀐다

즉 위조 조종 명령 하나로 **정지·이탈·표류**를 만들 수 있다.

### 6.3 제어권 전환 구간

`mass_roc_background.md` §2.6이 지목한 전환 구간 공격 표면이다. 현재 구현은 응용 ACK
하나로 MON↔RC가 즉시 전환되며, 실제 운용의 절차(원격 운영자 준비 확인 → 선장 승인 →
기록)와 인수 지연(20 s 이상)이 없다. **전환 구간 공격을 다루려면 이 절차를 먼저
모델링해야 한다.**

### 6.4 안전 제한

`sc_vde` 두 형식은 unsigned Segment Type 0으로 전달되며 송신자 인증·무결성·replay
보호를 제공하지 않는다. **격리된 시험·데모 전용이고, 이 payload를 받았다는 이유만으로
실제 조타·actuator를 구동해서는 안 된다.**

---

## 7. 예상 결과

| # | 관측 | 기대 |
| --- | --- | --- |
| 1 | 응용 ACK | `acknowledged` (status 4) |
| 2 | 운항 모드 | MON → **RC** |
| 3 | 침로 | 지시 침로로 수렴 (선회율 20°/분 상한) |
| 4 | Fallback 지시 시 | 속력 0 · Navigation Status 전환 · 항로 해제 |
| 5 | 링크 두절 30 s | fallback 진입 + 모드 RC → MON |
| 6 | 왕복 바이트 | 송신 == 수신 (byte-exact) |

---

## 8. 실패 조건

| 조건 | 응답 |
| --- | --- |
| 전선에 실을 수 없는 값 (Sender · Route Type · Start · 침로 범위 · Fallback · 좌표 범위 · Leg Geometry) | `errors` / `route unsuitable` |
| 침로·Fallback·waypoint가 모두 없음 | `incomplete` / `route unsuitable` |
| 연속 중복 waypoint | `route issues` / `route unsuitable` |
| 권한 정책 위반 (정책 설정 시) | `errors` / `not authorized` |
| 취소 지시의 Linkage ID가 반영 중인 지시와 다름 | `route issues` / `route unsuitable` (상태 불변) |
| 유효기간 만료 | `route issues` / `validity expired` (만료 통지) |

허용 값 표와 근거는 [SC-VDE-005 §8.2](SC-VDE-005-route-guidance.md)에 있다.
두 시나리오가 같은 codec(MF 2)을 쓰므로 규칙도 같다.

---

## 9. 로그 내용

| 항목 | 쓰임 |
| --- | --- |
| 발행·ACK 상관 (linkage_id) | 명령이 수락됐는지 |
| 운항 모드 전이 (MON↔RC) | 제어권이 언제 넘어갔는지 |
| 침로 시계열 | 지시 침로로 수렴하는지 |
| Fallback 전환 | 항행 정지가 언제 일어났는지 |
| `link_loss_count` | 제어 링크 두절 횟수 |
| `rejected_count` | 거절한 명령 수 |

---

## 10. 실행

```bash
python apps/scenario_runner.py run SC-VDE-006          # 규격 판정
python apps/control_loopback_demo.py                   # VDETER 소프트웨어 무선 종단 (cryptography 필요)
python apps/console_server.py --transport software     # 콘솔에서 조종 (원격 조종 탭)
```

## 11. 참고

| 문서 | 내용 |
| --- | --- |
| [`SC-VDE-005-route-guidance.md`](SC-VDE-005-route-guidance.md) | 상위 시나리오 (노선) |
| [`../전선형식_원격조종_SC-VDE-006.md`](../전선형식_원격조종_SC-VDE-006.md) | 전선 형식 (링크 팀 전달본) |
| [`../mass_roc_control_and_ack_message_format.md`](../mass_roc_control_and_ack_message_format.md) | S-421 조작 데이터 논리 모델 |
| [`../vdeter_mass_roc_api_guide.md`](../vdeter_mass_roc_api_guide.md) | Service API 계약 (§8.3) |
| [`../시나리오_카탈로그_확정3종.xlsx`](../시나리오_카탈로그_확정3종.xlsx) | 확정 3종 대조표 |
