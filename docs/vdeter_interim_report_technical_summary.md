# VDE-TER 구현 및 장비 구성 기술 정리

## 1. 전체 시스템 개요

### 1.1 시스템 역할과 구조

VDE-TER 시스템은 선박 역할의 `MASS-A`, `MASS-B`와 제어국 역할의 `ROC`로 구성된다.

- **MASS-A, MASS-B**: `SHIP` endpoint로서 Service에 제출된 application payload를 송수신한다.
- **ROC**: `CONTROL` endpoint로서 TBB(Bulletin Board), logical channel, 자원할당과 통신 일정을 관리한다.
- **통신 방향**: ROC와 MASS 사이의 addressed 통신을 기본으로 하며, MASS 간 직접 Short Data와 ROC relay 경로를 구분한다.

데이터 처리 구조는 다음과 같다.

```text
Client
  ↕  start / stop / submit / cancel / status / async event
Service API
  ↕  payload_format + application bytes
Presentation Registry / Codec
  ↕  Presentation data
Link (Packet, CRC, Segment, Short Data, Data Session, TBB)
  ↕  MAC command/result
MAC (TBB, resource, logical channel, TDMA, ACK/retry, routing)
  ↕  PHY burst
PHY (Link 11/17/19, FEC, modulation, waveform, receiver)
  ↕  timestamped complex samples
Radio contract
  ├─ SoftwareRadio
  └─ GNU Radio/UHD B210 backend
```

Service API는 application payload와 목적지 같은 업무 수준 입력만 받는다. Message Type, VPFI, CRC, FEC, fragment, slot, B210 객체 등의 내부 정보는 Link 이하 계층에서 처리한다.

### 1.2 최종 실행 및 RF 구성

실행 장비는 **Raspberry Pi 5 한 대와 Ettus B210 세 대**이다. Raspberry Pi 5에서 MASS-A, MASS-B, ROC endpoint와 각 endpoint 전용 B210 worker를 실행한다. Runtime Coordinator를 포함한 기본 실행 단위는 7개 process이며, 시험 실행 시 Scenario Controller가 추가된다. process 간 제어에는 로컬 Unix socket `/run/user/<uid>/vdeter-stage7/runtime`을 사용한다.

각 B210은 endpoint 하나에 전용으로 할당된다. B210 세 대의 RF-A/RF-B channel은 6개의 단방향 50 Ω SMA cable로 연결한다.

| path | 송신 | 수신 | 용도 |
|---|---|---|---|
| C1 | ROC RF-A `TX/RX` | MASS-A RF-A `RX2` | ROC→MASS-A, upper |
| C2 | MASS-A RF-A `TX/RX` | ROC RF-A `RX2` | MASS-A→ROC, lower |
| C3 | ROC RF-B `TX/RX` | MASS-B RF-A `RX2` | ROC→MASS-B, upper |
| C4 | MASS-B RF-A `TX/RX` | ROC RF-B `RX2` | MASS-B→ROC, lower |
| C5 | MASS-A RF-B `TX/RX` | MASS-B RF-B `RX2` | MASS-A→MASS-B 직접, upper |
| C6 | MASS-B RF-B `TX/RX` | MASS-A RF-B `RX2` | MASS-B→MASS-A 직접, upper |

구성에는 안테나, 공중파 경로, attenuator, combiner, splitter와 RF matrix를 사용하지 않는다.

### 1.3 Radio 실행 경로

`SoftwareRadio`와 B210 backend는 동일한 공개 Radio 호출 계약을 구현한다.

- `SoftwareRadio`는 process 내부의 결정적 medium을 사용해 송수신, 시간과 fault injection을 재현한다.
- B210 경로는 GNU Radio `gr-uhd` flowgraph를 별도 child process에서 실행하고 UHD를 통해 장비를 제어한다.
- 상위 Service·Presentation·Link·MAC·PHY는 Radio 구현에 관계없이 동일한 호출 구조를 사용한다.
- B210 경로에서 USB transport, RF gain, 독립 clock, CFO와 hardware timestamp를 추가로 관리한다.

## 2. VDE-TER 제공 기능

### 2.1 최종 기능 상태

| 기능 | 최종 상태 | 관련 계층 | 적용 범위 | 근거 문서·절 | 비고 |
|---|---|---|---|---|---|
| 공개 Service API | 지원 | Service/Runtime | `start`, `stop`, `submit`, `cancel`, `status` | 구현 명세 §6.1, §7.1, §8.2; Stage 5 | 내부 protocol 식별자 비노출 |
| 비동기 event 전달 | 지원 | Service/Runtime | 수신, 전송 완료, 실패, 취소 event | Stage 5 §6~§12 | bounded event queue 사용 |
| MASS↔ROC addressed 통신 | 지원 | Service~Radio | Short Data와 Data Session | Stage 5 §5.4, §7~§10 | 목적지와 route policy 적용 |
| MASS↔MASS 직접 통신 | 제한 지원 | Link/MAC | 직접 경로는 Short Data | Stage 5 §5.4 | 큰 Data Session은 ROC relay 사용 |
| broadcast | 제한 지원 | Link/MAC/Service | ROC-originated Type 93, no-ACK Short Data | Stage 5 §5.4, §7.3 | MASS-originated broadcast 미지원 |
| Short Data | 지원 | Link/MAC | addressed 또는 제한된 broadcast | Stage 1; Stage 5 §7.3 | Link 11 용량에 따라 Type 92/93 선택 |
| Data Session | 지원 | Link/MAC | addressed fragment 전송과 재조립 | 구현 명세 §4.1.3; Stage 1·5 | broadcast Data Session 미지원 |
| Packet 생성·파싱·CRC | 지원 | Link | 지원 Message Type의 wire packet | Stage 1 §5~§12 | CRC 오류를 구조화해 반환 |
| Segment 분할·재조립 | 지원 | Link | unsigned Segment Type 0과 session fragment | Stage 1; 구현 명세 §6.3 | Signed Segment Type 1 미지원 |
| TBB 생성·서명·검증·배포 | 지원 | Link/MAC | ROC signer, MASS trust store, Type 20/21/22 | 구현 명세 §4.1.4, LINK-REQ-09; Stage 1·4 | SC-VDE payload 인증과는 별개 |
| 자원할당과 Logical Channel | 제한 지원 | MAC | 승인된 TBB/resource subset | Stage 4 §5~§16 | 표준 전체 자원할당 절차는 아님 |
| Hexslot/TDMA 계획 | 지원 | MAC/Runtime | project time coordinate와 일정 실행 | Stage 4 §6; Stage 7 §13 | UTC 의미와 monotonic deadline 분리 |
| ACK·timeout·retry | 지원 | MAC/Service | addressed transfer | Stage 4 §10~§15; Stage 5 | bounded retry와 terminal result 제공 |
| Link ID 11 | 지원 | PHY/Radio | 25 kHz native profile | Stage 2·3·6 | π/4-QPSK |
| Link ID 17 | 지원 | PHY/Radio | 100 kHz software PHY | Stage 6 Link 17 | π/4-QPSK |
| Link ID 19 | 지원 | PHY/Radio | 100 kHz software PHY | Stage 6 Link 19 | 16-QAM |
| 공통 multi-rate Radio 연동 | 지원 | PHY/Runtime/Radio | device 307.2 kS/s, Link 11 4× rate 변환 | Stage 6 공통 §13; DR-STAGE6-MR-001 | Link 17/19는 identity rate |
| PHY FEC·변복조·waveform | 지원 | PHY | turbo FEC, interleaving, puncturing, scrambling, RRC, receiver | Stage 2; Stage 6 공통 §3~§8 | multi-ID candidate 처리 포함 |
| SoftwareRadio | 지원 | Radio/Test | 결정적 medium과 fault injection | Stage 3; Stage 5 | 실제 RF 성능을 의미하지 않음 |
| GNU Radio 기반 B210 송수신 | 지원 | Radio | `gr-uhd` child process backend | Stage 3; Stage 7 §15 | 공개 Radio 계약 유지 |
| 3 endpoint 동시 SoftwareRadio 운용 | 지원 | 전체 stack | MASS-A, MASS-B, ROC | Stage 5 | 동결 회귀 범위 |
| 3-B210 conducted 구성 | 구성·개별 path 검증 | PHY/Radio — Link 11 개별 path | 단일 Pi, B210 3대, C1~C6 | Stage 7 §5~§17A | 전체 Service/MAC/Link 17·19 system qualification은 미확정 |
| Payload Format Registry | 지원 | Presentation | OPERATIONAL/TEST scope 분리 | 구현 명세 §6.2, §7.2 | canonical content identity 사용 |
| Certificate Report | 제한 지원 | Presentation | VPFI 0, X.509 v3 DER/CRL 구조 | PRES-REQ-03; R1027 Table 1 | trust chain과 인증 효력은 판정하지 않음 |
| AIS Authentication Scheme 1 | 제한 지원 | Presentation | VPFI 7 MF1 구조와 bytes 운반 | PRES-REQ-04/06; R1027 Table 21 | AIS 원문 matching·서명 판정 미지원 |
| SC-VDE 위치보고 MF1 | 지원 — Core 통합됨(2026-08-30 main 병합) | Presentation/Service | MASS→ROC addressed, no-ACK | Payload §6.5, DR-PAYLOAD-015~017 | TEST VPFI `FF 7F` |
| SC-VDE 노선·원격조종 MF2 | 지원 — Core 통합됨(2026-08-30 main 병합) | Presentation | addressed command codec | Payload §6.4, §16 | 실제 actuator와 분리 |
| SC-VDE ACK MF3 | 지원 — Core 통합됨(2026-08-30 main 병합) | Presentation | addressed application ACK codec | Payload §6.4, §16 | Link ACK를 대체하지 않음 |
| opaque binary | TEST 범위 지원 | Presentation | 승인된 TEST registry entry | 구현 명세 §4.1.7; Stage 5 | 미지원 운영 payload의 우회 수단이 아님 |
| 장애 주입·회귀·증적 생성 | 지원 | Radio/Test | software fault, Stage runner, structured evidence | Stage 3·5·6; Stage 7 §20 | 시험 profile별 주장 범위 적용 |

지원 Message Type은 `0`, `4`, `13`, `20`, `21`, `22`, `74`, `75`, `76`, `81`, `90`, `92`, `93`이다.

검증 범위는 구분해서 해석한다. 3 endpoint 전체 Service stack은 SoftwareRadio 환경에서 동결되었고, 실제 B210 기준선은 2-B210 Link 11 경로이며, Stage 7 C1~C6 결과는 3대 B210 각각의 Link 11 raw PHY conducted path를 확인한 것이다. 따라서 3-B210 환경에서 전체 Service/MAC와 Link 17·19까지 최종 적합 판정을 마쳤다고 표현하지 않는다.

### 2.2 미지원 및 구현 범위 밖

| 항목 | 최종 상태 | 범위 |
|---|---|---|
| 공식 OPERATIONAL SC-VDE VPFI | 미지원 | 공식 할당 전에는 TEST VPFI만 사용 |
| Signed Segment Type 1 | 미지원 | 현행 SC-VDE는 unsigned Type 0 사용 |
| SC-VDE 송신자 인증 | 미지원 | 위치·명령·ACK의 sender identity를 암호학적으로 확인하지 않음 |
| payload 암호학적 무결성·기밀성 | 미지원 | TBB 서명은 application payload를 보호하지 않음 |
| replay/freshness protection | 미지원 | nonce, sequence 검증과 replay cache 없음 |
| MASS-originated 위치보고 broadcast | 미지원 | MF1은 MASS→ROC addressed no-ACK |
| broadcast Data Session과 broadcast ACK | 미지원 | broadcast는 ROC-originated no-ACK Short Data로 제한 |
| 실제 선박 제어·actuator 연동 | 범위 밖 | MF2는 wire codec이며 제어 실행 기능이 아님 |
| 실제 위치 신뢰 판단 | 범위 밖 | 위치 bytes의 업무적 진실성과 송신자를 검증하지 않음 |
| 전자해도 전송 | 미지원 | 승인된 공식 payload mapping 없음 |
| 완전한 VDE-TER 표준 전체 자원할당 절차 | 범위 밖 | 승인된 project subset만 구현 |
| Control Service Area 밖 운용 | 범위 밖 | 현재 direction과 routing은 control area 내부 topology 전제 |
| OTA·실해역 적합성 | 범위 밖 | conducted cable testbed만 구성 |
| 공식 표준 전체 적합 인증 | 범위 밖 | 선택한 profile과 project contract의 구현·시험 |

## 3. 장비 및 실행 설정

### 3.1 endpoint와 B210

| 구분 | ROC | MASS-A | MASS-B | 값의 성격 |
|---|---|---|---|---|
| endpoint 역할 | `CONTROL` | `SHIP` | `SHIP` | 최종 기준값 |
| station ID | `222222222` | `111111111` | `333333333` | 최종 기준값 |
| 실행 host | Raspberry Pi 5 | 동일 host | 동일 host | 실제 구성 |
| endpoint별 IP | 없음 | 없음 | 없음 | local Unix socket 구조 |
| B210 serial | `34D03E8` | `34D03DD` | `3515425` | 실제 장비 식별값 |
| UHD device args | `serial=34D03E8` | `serial=34D03DD` | `serial=3515425` | 실제 적용값 |
| USB path·속도 | `5-1`, USB3 5000M | `3-1`, USB3 5000M | `4-2`, USB2 480M | 실제 적용값 |
| B210 전원 | Pi bus power | Pi bus power | 외부 DC `AT2024T` | 실제 구성 |

serial, USB path와 외부 전원 asset은 protocol 고정값이 아니라 현재 시험 장비의 식별·배치값이다.

### 3.2 RF와 sample 설정

| 구분 | ROC | MASS-A | MASS-B | 값의 성격 |
|---|---|---|---|---|
| 논리 TX/RX 방향 | TX upper / RX lower | TX lower / RX upper | TX lower / RX upper | 최종 기준값 |
| 논리 center frequency | lower 157.2375 MHz, upper 161.8375 MHz | 동일 | 동일 | project channel 값 |
| B210 RF tune | lower 2.4472375 GHz, upper 2.4518375 GHz | 동일 | 동일 | 실제 시험 적용값 |
| frequency translation | +2.290 GHz | 동일 | 동일 | project private bridge |
| channel alias | `VDETER_LOWER_100_TEST`, `VDETER_UPPER_100_TEST` | 동일 | 동일 | TEST 기준값 |
| RF channel mapping | RF-A=UHD ch0, RF-B=UHD ch1 | 동일 | 동일 | 최종 lane mapping |
| antenna port | TX `TX/RX`, RX `RX2` | 동일 | 동일 | 최종 기준값 |
| device sample rate | 307,200 sample/s | 동일 | 동일 | 실제 적용값 |
| native PHY rate | Link 11: 76,800; Link 17/19: 307,200 sample/s | 동일 | 동일 | 동결 PHY profile |
| rate 변환 | Link 11: 4× interpolation/decimation; Link 17/19: identity | 동일 | 동일 | project 통합 계약 |
| master clock rate | 30.72 MHz | 동일 | 동일 | 실제 적용값 |
| waveform bandwidth | 100 kHz | 동일 | 동일 | 실제 적용값 |
| analog bandwidth | 200 kHz | 동일 | 동일 | 실제 적용값 |
| TX gain | 0 dB | 0 dB | 0 dB | 실제 적용값 |
| RX gain | 69 dB | 69 dB | 69 dB | 실제 적용값 |
| CPU/OTW/sample format | `fc32` / `sc16` / `cf32le` | 동일 | 동일 | 실제 적용값 |
| clock/time/PPS | internal / internal / none | 동일 | 동일 | 실제 적용값 |
| TX scheduling lead | 250 ms | 250 ms | 250 ms | 최종 기준값 |
| active command horizon | 120 s | 120 s | 120 s | 최종 기준값 |
| RX resource limit | window 64, queue 256/64 MiB | 동일 | 동일 | 최종 기준값 |

TDMA의 시간 의미에는 host UTC를 사용하고, deadline과 timeout에는 monotonic clock을 사용한다. B210 hardware time은 runtime-private host affine bridge로 host time에 대응시킨다. 외부 PPS나 GNSS discipline은 사용하지 않는다.

### 3.3 케이블과 전원 구성

- C1~C6은 50 Ω SMA cable로 구성한다.
- RF-A는 UHD channel 0, RF-B는 UHD channel 1에 대응한다.
- 송신 port는 `TX/RX`, 수신 port는 `RX2`를 사용한다.
- Stage 7 구성에는 안테나와 고정 attenuator를 사용하지 않는다.
- MASS-B B210은 외부 DC 전원 `AT2024T`를 사용한다.

### 3.4 소프트웨어와 신뢰 설정

| 항목 | 최종 값 | 값의 성격 |
|---|---|---|
| host | Raspberry Pi 5 Model B Rev 1.1 (`d04171`), aarch64 | 실제 구성 |
| OS | Ubuntu 24.04 | 실제 적용값 |
| kernel | Linux `6.8.0-1061-raspi` | 실제 적용값 |
| Python | project venv, Python 3.12 계열 | 실행환경 |
| GNU Radio | 3.10.9.2 | 실제 적용값 |
| UHD | 4.6.0.0+ds1-5.1ubuntu0.24.04.1 | 실제 적용값 |
| NumPy | 1.26.4 | 실제 적용값 |
| runtime `cryptography` | 50.0.1 | project venv 실제 적용값 |
| TBB trust fingerprint | `4aa1538e0a4e` | 공개 식별값 |
| Pi throttled 상태 | `0x0` | 확인 환경값 |

설정은 Stage 7 source template, deployment manifest와 environment manifest를 입력으로 생성한다. `tools/prepare_stage7_run_configs.py`가 실행별 설정과 hash를 만들고, `tools/deploy_stage7_to_pi.py`가 Raspberry Pi에 배포한다. runtime은 manifest와 설정 hash를 확인한 뒤 endpoint를 시작한다. private key 본문은 배포·보고 대상에 포함하지 않는다.

## 4. 표준과 프로젝트 정의의 구분

### 4.1 구분 기준

표준과 프로젝트 정의는 기능 이름 전체가 아니라 **개별 field, 값, wire 구조, 알고리즘과 실행 설정 단위**로 구분한다. 하나의 기능 안에도 표준 요소와 project 요소가 함께 존재할 수 있다. 예를 들어 Link 17의 Link ID·대역폭·변조 방식은 M.2092-2에서 가져오지만, B210 device sample rate와 GNU Radio 처리 구조는 project에서 정한다.

| 분류 | 판정 기준 |
|---|---|
| `STANDARD_EXACT` | 공식 표준이 값·field·wire 순서 또는 처리 규칙을 직접 규정하고 구현이 이를 그대로 사용 |
| `STANDARD_APPROXIMATION` | 표준의 기능·profile을 구현하지만 지원 subset, 수치 처리 또는 시험환경 적용에 project 선택이 포함됨 |
| `PROJECT_DEFINED` | 표준이 정하지 않은 software interface, 장비 설정, 내부 알고리즘 또는 시험 topology를 project에서 결정 |
| `REFERENCE_ONLY` | 공식 문서를 구현 규칙으로 채택하지 않고 기능 목적·범위 또는 부하 판단의 참고 근거로만 사용 |
| `APPROVED_DEVIATION` | 표준 해석, 상위 요구 또는 기존 계획과 다른 방법을 Decision Record로 승인해 적용 |
| `DEFERRED` | 운영 요구가 확정될 때 별도 기준선에서 결정·구현할 항목 |
| `OUT_OF_SCOPE` | 현재 구현 범위에 포함하지 않는 항목 |

판정 시 공식 문서의 표·절을 먼저 확인한다. 공식 근거가 없거나 표준이 구현 방법을 정하지 않은 경우에는 표준 기능으로 표현하지 않고 `PROJECT_DEFINED`로 분류한다.

### 4.2 ITU-R M.2092-2에서 직접 가져온 내용

| 항목 | 표준이 직접 정한 부분 | Project에서 선택·제한한 부분 | 분류 | 근거 |
|---|---|---|---|---|
| Link ID 11 | Link ID, 25 kHz 계열 PHY profile, π/4-QPSK와 burst 구성 | Link 11만 먼저 구현하고 project receiver numeric profile 적용 | `STANDARD_APPROXIMATION` | M.2092-2 Table 8; Stage 2 |
| Link ID 17 | Link ID, 100 kHz 계열 PHY profile, π/4-QPSK와 burst 구성 | 지원 profile을 Link 17로 제한하고 project 수신 알고리즘 적용 | `STANDARD_APPROXIMATION` | M.2092-2 Table 8; Stage 6 Link 17 §3 |
| Link ID 19 | Link ID, 100 kHz 계열 PHY profile, 16-QAM과 burst 구성 | 지원 profile을 Link 19로 제한하고 project 수신 알고리즘 적용 | `STANDARD_APPROXIMATION` | M.2092-2 Table 8; Stage 6 Link 19 §3 |
| Short Data | Short Data Message의 기본 구조와 용량 경계 | Service가 payload 크기와 ACK 여부에 따라 Type 92/93을 선택 | `STANDARD_APPROXIMATION` | M.2092-2 Annex 4; Stage 1·5 |
| Data Session | Data Session의 session·fragment 전송 구조 | addressed 전송만 지원하고 broadcast Data Session은 제외 | `STANDARD_APPROXIMATION` | M.2092-2 Annex 4; Stage 1·5 |
| Segment Type 0 | unsigned Segment Type 0 wire 구조 | SC-VDE MF1/2/3 운반에 Type 0을 선택 | `STANDARD_EXACT`(wire 구조) | M.2092-2 Segment 정의; DR-PAYLOAD-013 |
| Segment Type 1 | signed Segment Type 1 wire 구조 | 현재 구현하지 않음 | `DEFERRED` | M.2092-2 Segment 정의; DR-PAYLOAD-014 |
| Message Type | 각 Message Type의 식별값과 wire field | `0`, `4`, `13`, `20~22`, `74~76`, `81`, `90`, `92`, `93`만 지원 | `STANDARD_APPROXIMATION` | M.2092-2 Annex 4; Stage 1 registry |
| Packet·header | VDE-TER packet과 message field 구조 | 지원 Message Type에 대해서만 builder/parser 제공 | `STANDARD_APPROXIMATION` | M.2092-2 Annex 4; Stage 1 |
| TBB | Bulletin Board와 channel/resource 정보의 wire 개념 | ROC가 TBB를 소유하고 project lifecycle로 운용 | `STANDARD_APPROXIMATION` | M.2092-2 TBB 절; Stage 1·4 |
| Physical Channel | 25/100 kHz channel 계열과 link profile의 관계 | 두 TEST alias와 고정 upper/lower direction 사용 | `STANDARD_APPROXIMATION` | M.2092-2 Table 8; Stage 6 §13, Stage 7 §10 |
| Control Service Area | control station 중심의 서비스 영역과 통신 방향 개념 | ROC 1대와 MASS 2대의 고정 topology로 제한 | `STANDARD_APPROXIMATION` | M.2092-2 Control Service Area; Stage 4 routing |

M.2092-2에서 profile과 wire 구조를 가져왔다는 것은 해당 기능의 전체 표준 적합 인증을 의미하지 않는다. 현재 구현은 필요한 Link ID와 Message Type을 선택한 subset이다.

### 4.3 IALA R1027 적용 내용과 G1192 참고 범위

| 항목 | 공식 문서가 정한 부분 | Project에서 선택·제한한 부분 | 분류 | 근거 |
|---|---|---|---|---|
| Certificate Report | R1027 VPFI 0, certificate entry와 certificate/CRL 구조 | DER 구조만 검사하고 trust chain·root 의미·revocation은 application 책임으로 둠 | `STANDARD_APPROXIMATION` | R1027 Ed.1.1 Table 1; PRES-REQ-03 |
| AIS Authentication Scheme 1 | R1027 VPFI 7, Message Format 1과 certificate fingerprint 포함 구조 | codec과 bytes 운반만 구현하고 AIS 원문 matching·서명 판정은 수행하지 않음 | `STANDARD_APPROXIMATION` | R1027 Ed.1.1 Table 21; PRES-REQ-04/06 |
| TEST VPFI | R1027이 TEST용 VPFI `FF 7F`를 할당 | SC-VDE MF1/2/3의 project TEST registry에 이 값을 사용 | 표준값은 `STANDARD_EXACT`, 용도 매핑은 `PROJECT_DEFINED` | R1027 Ed.1.1 Table 3; DR-PAYLOAD-002/003 |
| AIS 인증 목적과 link load | G1192가 authentication scheme, timestamp·signature와 전송 부하를 설명 | 구현 규칙이 아닌 참고 근거로만 사용한다. G1192의 624-bit fallback은 채택하지 않고 R1027 Table 21의 672-bit 구조를 구현 | `REFERENCE_ONLY` | G1192 Ed.1.0 authentication·link load 절; R1027 Ed.1.1 Table 21 |
| 전자해도 | 확인한 공식 문서에 현재 적용할 VDE-TER payload mapping이 없음 | registry entry를 만들지 않고 요청을 거부 | `OUT_OF_SCOPE` | PRES-REQ-06, TEST-REQ-08 |

`FF 7F`라는 값 자체는 R1027의 TEST allocation이지만, SC-VDE를 그 값에 연결하는 것은 project 결정이다. 공식 OPERATIONAL SC-VDE VPFI로 표현해서는 안 된다.

### 4.4 표준 개념을 Project 운용 구조로 적용한 내용

| 항목 | 표준에서 가져온 개념 | 최종 Project 적용 | 분류 | 근거 DR·문서 |
|---|---|---|---|---|
| ROC control 구조 | control station과 ship station 역할 | ROC=`CONTROL`, MASS-A/B=`SHIP`; ROC가 TBB와 resource plan 소유 | `PROJECT_DEFINED` | DR-STAGE4-MAC-008/011 |
| Duplex·방향 | upper/lower Physical Channel과 통신 방향 | ROC는 upper 송신/lower 수신, MASS는 lower 송신/upper 수신 | `PROJECT_DEFINED` | DR-STAGE4-MAC-008/011; Stage 7 §10 |
| Hexslot·slot·TDMA | 표준의 slot·time resource 개념 | 정수 nanosecond 기반 project time coordinate와 계산 규칙 사용 | `STANDARD_APPROXIMATION` | DR-STAGE4-MAC-003 |
| TBB lifecycle | TBB와 channel/resource 정보 | publish, verify, replace, expire와 resource 적용 순서를 project 상태기계로 정의 | `STANDARD_APPROXIMATION` | DR-STAGE4-MAC-004/005/007/009 |
| TBB 서명 | M.2092-2가 P-256, SHA-256과 P1363 형식의 64-byte signature를 규정 | `VDETER-TBB-SIG-v1` canonical signing input, RFC6979 deterministic nonce, low-S 송신 정책과 signer/trust-store API는 project에서 정의 | 암호 primitive·wire 형식은 `STANDARD_EXACT`, signing input·정규화·software 정책은 `PROJECT_DEFINED` | M.2092-2 TBB 서명 규정; 구현 명세 LINK-REQ-09; Stage 1 §9.1 |
| TBB fingerprint | M.2092-2가 X.509 `signatureValue`의 하위 48 bits를 6-byte Digital Certificate Fingerprint로 사용하도록 규정 | 해당 표준 계산을 그대로 적용 | `STANDARD_EXACT` | M.2092-2 TBB fingerprint 규정; 구현 명세 §4.1.4; Stage 1 §9.1 |
| Short Data 선택 | 표준 Short Data Message Type과 용량 | ACK=true이면 encoded≤33 B Type 92, no-ACK이면 encoded≤34 B Type 93 선택 | `PROJECT_DEFINED`(선택 정책) | Stage 5 §7.3 |
| MASS 간 전송 | control area 내부의 station 간 통신 | 직접 path는 Short Data, 큰 Data Session은 ROC relay로 제한 | `PROJECT_DEFINED` | DR-STAGE4-MAC-011; Stage 5 routing |

### 4.5 장비·RF·실행환경에서 Project가 정한 값

다음 값들은 VDE-TER 표준 요구가 아니라 현재 B210 testbed를 구성하기 위해 project에서 정한 값이다.

| 항목 | 최종 Project 값 | 표준과의 관계 | 분류 | 근거 |
|---|---|---|---|---|
| 실행 host | Raspberry Pi 5 한 대 | 표준은 computer 수와 process 배치를 정하지 않음 | `PROJECT_DEFINED` | Stage 7 §6 |
| endpoint/process | MASS-A, MASS-B, ROC와 전용 B210 worker, Runtime Coordinator | 표준은 local process 구조를 정하지 않음 | `PROJECT_DEFINED` | Stage 7 §14 |
| process 통신 | local Unix socket | 표준 밖의 runtime IPC | `PROJECT_DEFINED` | `configs/stage7/system.toml` |
| B210 배치 | endpoint별 B210 한 대, 총 3대 | 표준은 SDR 모델·serial을 정하지 않음 | `PROJECT_DEFINED` | Stage 7 §5·§7 |
| RF lane | C1~C6 50 Ω SMA cable, RF-A=ch0, RF-B=ch1 | 표준 밖의 시험 fixture | `PROJECT_DEFINED` | Stage 7 §5·§10.1 |
| antenna port | 송신 `TX/RX`, 수신 `RX2` | B210 장비 설정 | `PROJECT_DEFINED` | Stage 7 §15 |
| 논리 channel alias | `VDETER_LOWER_100_TEST`, `VDETER_UPPER_100_TEST` | local config 식별자 | `PROJECT_DEFINED` | endpoint configs |
| 시험 RF tune | 2.4472375/2.4518375 GHz | 표준 운용 VHF carrier가 아니라 시험용 translation 결과 | `PROJECT_DEFINED` | Stage 7 §12 |
| frequency translation | +2.290 GHz | B210 testbed private bridge | `PROJECT_DEFINED` | Stage 7 endpoint configs |
| device sample rate | 307.2 kS/s | 표준은 UHD device rate를 정하지 않음 | `PROJECT_DEFINED` | DR-STAGE6-MR-001 |
| Link 11 rate 변환 | 76.8↔307.2 kS/s 4× interpolation/decimation | 여러 PHY를 공통 device rate에 연결하기 위한 구현 | `PROJECT_DEFINED` | DR-STAGE6-MR-001 |
| Link 17/19 rate 변환 | 307.2 kS/s identity | 공통 Radio rate 적용 방법 | `PROJECT_DEFINED` | DR-STAGE6-MR-001 |
| MCR·sample format | 30.72 MHz, CPU `fc32`, OTW `sc16`, sample `cf32le` | UHD/GNU Radio 구현 설정 | `PROJECT_DEFINED` | Stage 7 §15 |
| RF bandwidth | waveform 100 kHz, analog 200 kHz | testbed filter·device 설정 | `PROJECT_DEFINED` | endpoint configs |
| gain | TX 0 dB, RX 69 dB | 장비·cable 환경값 | `PROJECT_DEFINED` | Stage 7 gain evidence |
| clock/time/PPS | internal/internal/none | B210 시간원 선택 | `PROJECT_DEFINED` | Stage 7 §13 |
| scheduling | TX lead 250 ms, active horizon 120 s | host·B210 command 운용값 | `PROJECT_DEFINED` | Stage 7 §13·§15 |
| USB·전원 | ROC/MASS-A USB3, MASS-B USB2+외부 DC | 현재 장비 배치 | `PROJECT_DEFINED` | Stage 7 environment inventory |
| GNU Radio backend | `gr-uhd` child process | 표준은 SDR software 구조를 정하지 않음 | `PROJECT_DEFINED` | Stage 3·7 Radio 구현 |

### 4.6 Software 내부 알고리즘과 계층 계약

다음 항목은 wire 표준이 아니라 구현의 결정성, 계층 분리와 시험 재현성을 위해 project가 정의한 계약이다.

| 항목 | Project 정의 | 필요 이유 | 분류 | 근거 DR·문서 |
|---|---|---|---|---|
| 공개 Service API | `start/stop/submit/cancel/status`와 async event 계약 | application에서 wire·Radio 세부를 숨김 | `PROJECT_DEFINED` | 구현 명세 §7.1; Stage 5 |
| SoftwareRadio/B210 공통 계약 | 두 backend에 동일한 Radio 호출과 결과 형식 적용 | 상위 PHY/Runtime의 backend 독립성 | `PROJECT_DEFINED` | Stage 3 Radio contract |
| MAC outbox | `reserve→publish` 또는 `abort` 원자적 처리 | 실패한 작업의 ghost transmission 방지 | `PROJECT_DEFINED` | Stage 4 §13~§15 |
| UTC·monotonic 분리 | UTC는 TDMA 의미, monotonic은 deadline·timeout | system clock 변화와 elapsed time 분리 | `PROJECT_DEFINED` | DR-STAGE4-MAC-002 |
| host-affine time bridge | host time과 B210 hardware time의 private affine mapping | internal free-running B210 scheduling | `PROJECT_DEFINED` | Stage 7 §13.2 |
| ACK·timeout·retry | bounded retry와 terminal success/failure | Service API에 일관된 종결 결과 제공 | `PROJECT_DEFINED` | DR-STAGE4-MAC-010/015 |
| 오류 precedence | 복수 내부 오류 중 공개할 단일 결과의 우선순위 | 실행마다 동일한 결과 보장 | `PROJECT_DEFINED` | DR-STAGE4-MAC-015; Stage 5 |
| partial RX state | 연속 sample state 유지, terminal/fault에서 reset | streaming input과 burst 경계 처리 | `PROJECT_DEFINED` | Stage 6 공통 §7.9 |
| 복수 PHY candidate | candidate 정렬·검증 후 결정적 결과 선택 | 한 입력의 multi-ID 검출 처리 | `PROJECT_DEFINED` | Stage 6 공통 §7.6·§7.8 |
| Link ID tie | 동결된 metric과 ordering으로 tie 처리 | decoder 결과 재현성 | `PROJECT_DEFINED` | Stage 6 공통 §7.4·§7.6 |
| PHY full-scale | normalized complex sample 기준 | FEC/AWGN/receiver 수치 시험의 공통 기준 | `PROJECT_DEFINED` | Stage 2; Stage 6 numeric profile |
| Registry canonicalization | canonical serialization, content version과 fingerprint | 배포된 payload registry의 동일성 확인 | `PROJECT_DEFINED` | DR-PAYLOAD-010/017 |
| resource limit | queue count·bytes, terminal retention과 timeout 상한 | memory·lifecycle의 bounded 동작 | `PROJECT_DEFINED` | Stage 5·7 configs |

### 4.7 SC-VDE Payload에 대한 Project 결정

| 항목 | 공식 근거 | 최종 Project 결정 | 분류 | 근거 DR |
|---|---|---|---|---|
| VPFI | R1027 TEST allocation `FF 7F` | 공식 SC-VDE OPERATIONAL VPFI가 없어 TEST scope로만 등록 | `APPROVED_DEVIATION` | DR-PAYLOAD-002/003 |
| Message Format | SC-VDE 공식 MF 할당 없음 | 위치보고 MF1, 노선·원격조종 MF2, ACK MF3로 project registry 구성 | `PROJECT_DEFINED` | DR-PAYLOAD-004/017 |
| 위치보고 route | 요구 초안은 MASS broadcast를 고려 | MASS→ROC addressed, `ack_required=false` 사용 | `APPROVED_DEVIATION` | DR-PAYLOAD-016 |
| Segment | M.2092-2 unsigned Type 0 구조 | MF1/2/3 모두 Type 0 사용 | `STANDARD_EXACT`(wire 선택) | DR-PAYLOAD-013/014 |
| application ACK | Link ACK와 별도인 payload MF3 | 자동·재귀 생성하지 않고 Link ACK를 대체하지 않음 | `PROJECT_DEFINED` | DR-PAYLOAD-008 |
| 의미 검증 | 표준 wire 구조와 업무 의미는 구분 필요 | codec은 구조 정규성만 확인하고 업무 의미는 application 책임 | `PROJECT_DEFINED` | DR-PAYLOAD-007 |
| Registry identity | 표준은 local registry hash를 정하지 않음 | content version `payload-extension-project-sc-vde-family-v2`와 scope별 fingerprint 사용 | `PROJECT_DEFINED` | DR-PAYLOAD-010/017 |
| sender 인증·integrity·replay | 운영 보안 수단 미결정 | 현 TEST profile에서 제공하지 않음 | `DEFERRED` | DR-PAYLOAD-013/014 |
| 실제 actuator | 공식 wire codec과 실제 제어 안전은 별도 | 실제 선박 제어와 actuator 연결 금지 | `OUT_OF_SCOPE` | DR-PAYLOAD-013 |

### 4.8 표준의 복수 선택지와 Project 최종 처리 규칙

표준에 같은 목적을 표현하는 방법이 여러 개 있거나 표준이 합법적인 값의 범위를 넓게 허용하더라도, 현재 project가 모든 방법을 사용하는 것은 아니다. 실제 자동 송신, 수신 허용 범위와 미지원값 처리 규칙은 다음과 같다.

#### 4.8.1 Type 92·93의 ACK/no-ACK 표현

M.2092-2 wire 형식에는 ACK가 필요 없는 Short Data를 표현하는 두 가지 방법이 있다.

| 표현 방법 | 표준 wire 의미 | 현재 Project 처리 |
|---|---|---|
| Type 92 + `retransmission_number=0..254` | ACK가 필요한 Short Data와 재전송 번호 | Link codec은 `0..254`를 표현할 수 있으나 MAC 송수신 완료 profile은 `0..3`만 사용 |
| Type 92 + `retransmission_number=255` | Type 92 형식의 no-ACK 표현 | low-level Link codec의 wire encode/decode만 유지하고 자동 송신에는 사용하지 않음. 수신 MAC은 미지원으로 거부 |
| Type 93 | ACK 없는 전용 Short Data Message Type | `ack_required=false`인 자동 송신에 사용하는 유일한 no-ACK 방식 |

최종 자동 선택 규칙은 다음과 같다.

```text
ack_required=true  → Type 92, retransmission_number=0..3
ack_required=false → Type 93, retransmission_number 없음
```

Type 92의 `4..254`와 `255`를 수신해도 Type 93으로 바꾸거나 payload를 Service에 전달하지 않는다. `UNSUPPORTED_RETRANSMISSION_NUMBER`로 거부하며 ACK도 생성하지 않는다. 이 정책은 표준 wire 값을 해석할 수 있는 Link codec 범위와 실제 MAC 운용 profile을 분리한 것이다. 근거는 Stage 1 §5.4·§8.6, `DR-STAGE1-037`, Stage 4 `MAC-REQ-10`이다.

Type 93은 retransmission number 1 byte가 없으므로 동일 Link ID 11에서 Type 92보다 encoded Presentation data를 1 byte 더 담을 수 있다.

| 조건 | 자동 선택 | encoded Presentation data 한도 | 완료 의미 |
|---|---|---:|---|
| addressed, `ack_required=true` | Type 92 | 33 bytes | 목적지 delivery queue 수락 후 생성된 matching Type 13 ACK 수신 |
| addressed, `ack_required=false` | Type 93 | 34 bytes | 로컬 PHY/Radio 송신 완료; 상대 수신을 보장하지 않음 |
| ROC broadcast, `ack_required=false` | Type 93 | 34 bytes | 로컬 송신 완료; 개별 MASS receipt를 보장하지 않음 |

#### 4.8.2 Short Data와 Data Session 선택

| 상황 | 가능한 표준 절차 | 현재 Project 선택 | 선택하지 않는 처리 |
|---|---|---|---|
| payload가 Short Data 한도 이내 | Type 92 또는 Type 93 | `ack_required`에 따라 92/93 선택 | Client가 Message Type을 직접 지정하지 못함 |
| addressed payload가 Short Data 한도 초과 | Data Session | Type 74/75/76 fragment와 Type 13 feedback 사용 | 큰 payload를 여러 Type 93으로 임의 분할하지 않음 |
| broadcast payload가 34 bytes 초과 | 별도 확장 절차 필요 | `UNSUPPORTED_REQUEST` | broadcast Data Session으로 자동 전환하지 않음 |
| Data Session의 `ack_required=false` | Service 입력상 no-ACK 요청 가능 | Data Session 신뢰 절차의 Type 13 feedback은 그대로 필수 | Short Data Type 93과 같은 무feedback 절차로 처리하지 않음 |

따라서 `ack_required`가 wire Message Type을 직접 바꾸는 범위는 Short Data의 Type 92/93 선택이다. payload 크기 때문에 Data Session이 선택되면 `ack_required=false`여도 frame별 feedback과 terminal cleanup을 수행한다. 근거는 Stage 5 §4.3·§5.1이다.

#### 4.8.3 ACK와 application 처리 완료의 구분

| 절차 | 통신 계층에서 확인하는 것 | 확인하지 않는 것 |
|---|---|---|
| Type 92 ACK | 수신 endpoint의 bounded `DATA_RECEIVED` queue에 payload를 수락하고 matching Type 13을 회신 | 수신 application이 실제 업무 처리를 끝냈는지 여부 |
| Type 93 no-ACK | 송신 측 PHY/Radio가 송신을 완료했는지 여부 | 목적지 수신, Service 전달과 application 소비 여부 |
| Data Session feedback | frame별 fragment 수신 결과와 완전 재조립·delivery gate | 외부 Client의 업무 처리 완료 |
| SC-VDE MF3 ACK | application payload로 정의한 별도 ACK message | Link Type 13 ACK의 대체 또는 자동 생성 |

ACK의 wire 구조는 표준을 따르지만, 공개 Service 상태에서 `COMPLETED`를 언제 기록하는지와 application event queue 수락을 ACK gate로 사용하는 방식은 project 계층 계약이다.

#### 4.8.4 Addressed와 broadcast 선택

| 항목 | 표준상 가능한 표현 | 현재 Project 선택 | 제외 처리 |
|---|---|---|---|
| Type 92 | addressed ACK Short Data | configured endpoint 간 addressed만 허용 | Destination 0 broadcast 거부 |
| Type 93 | addressed 또는 broadcast no-ACK | addressed와 ROC-originated upper-channel broadcast 허용 | MASS-originated broadcast 거부 |
| MASS 간 Short Data | direct 또는 control 경로 고려 가능 | direct one-hop Short Data 사용 | ROC relay로 불필요하게 변환하지 않음 |
| MASS 간 큰 Data Session | control station relay 구조 | MASS→ROC→MASS의 직렬 두 hop 사용 | direct MASS↔MASS Data Session 미지원 |
| SC-VDE MF1 위치보고 | 요구 초안은 MASS broadcast 고려 | MASS→ROC addressed, `ack_required=false` | MF1 broadcast 미지원 |

#### 4.8.5 Segment와 보안 표현 선택

| 선택 대상 | 표준 또는 공식 선택지 | 현재 Project 선택 | 결과 |
|---|---|---|---|
| Segment | unsigned Type 0 / signed Type 1 | SC-VDE MF1/2/3는 Type 0 | payload sender 인증과 cryptographic integrity 없음 |
| TBB 인증 | TBB certificate·authentication 개념 | 별도 `VDETER-TBB-SIG-v1` signature profile | TBB만 인증하며 SC-VDE payload를 인증하지 않음 |
| Certificate Report | certificate/CRL 전송 구조 | DER 구조만 검증 | trust chain·root·revocation 판정 없음 |
| AIS Authentication Scheme 1 | 인증 payload 구조 | field/bytes codec만 제공 | AIS 원문 matching·signature verdict·freshness 판정 없음 |
| replay protection | 운영 보안 profile에서 결정 필요 | 현재 SC-VDE TEST profile에 포함하지 않음 | 재전송 공격 방어를 주장하지 않음 |

Signed Segment Type 1을 사용하지 않는 것과 TBB에 서명을 적용하는 것은 모순이 아니다. 두 기능의 대상이 다르다. 전자는 application payload segment이고, 후자는 ROC가 배포하는 TBB control 정보다.

#### 4.8.6 VPFI와 Payload Registry 선택

| 상황 | 공식 근거 | 현재 Project 선택 | 금지되는 표현 |
|---|---|---|---|
| R1027 TEST VPFI | `FF 7F`가 TEST용으로 할당됨 | SC-VDE MF1/2/3의 TEST registry에 사용 | 공식 OPERATIONAL SC-VDE VPFI라고 표현하지 않음 |
| SC-VDE Message Format | 공식 MF 할당 확인 안 됨 | MF1=위치, MF2=명령, MF3=ACK로 project 정의 | 공식 표준 MF라고 표현하지 않음 |
| registry 변경 | 표준은 local registry identity를 정하지 않음 | canonical content version과 OPERATIONAL/TEST fingerprint를 갱신 | entry 변경 후 기존 fingerprint 재사용 금지 |
| 전자해도 | 적용 가능한 공식 mapping 없음 | 미지원 요청으로 거부 | opaque binary로 우회해 지원했다고 주장하지 않음 |

#### 4.8.7 PHY profile과 B210 구현값의 구분

| 항목 | 표준에서 정한 것 | Project에서 정한 것 |
|---|---|---|
| Link 11/17/19 | Link ID, 대역 계열, 변조와 burst profile | 지원 Link subset, receiver numeric profile와 candidate 처리 |
| sample rate | PHY waveform을 구성하는 규범적 symbol·sample 관계 | B210 공통 device rate 307.2 kS/s와 Link 11 4× resampling |
| Physical Channel | 25/100 kHz와 upper/lower 개념 | TEST alias, 고정 direction과 +2.290 GHz frequency translation |
| RF 장비 | 규정하지 않음 | B210 serial, UHD channel, antenna port, TX/RX gain과 cable topology |
| 시간 | TDMA의 UTC 의미 | internal clock, no PPS, monotonic deadline과 host-affine hardware-time bridge |

즉, Link 11/17/19를 지원한다는 표준 관련 주장과 B210을 307.2 kS/s로 구동한다는 project 구현 주장은 분리해서 기록한다.

#### 4.8.8 동일한 숫자 `255`의 field별 의미

`255`는 여러 field에서 사용되므로 field 이름 없이 “255를 보낸다”고 기록하면 안 된다.

| 위치 | `255`의 의미 | 현재 처리 |
|---|---|---|
| Type 92 `retransmission_number` | wire상 no-ACK | low-level decode 가능, MAC 운용 profile에서는 거부 |
| Type 4 logical channel pair `255/255` | 자원할당 거부의 canonical 표현 | allocation rejection에 사용 |
| 8-bit 일반 field의 상한 | 단순 정수 범위의 최댓값일 수 있음 | 해당 Message의 field 규칙에 따라 별도 판정 |

따라서 보고서에서 “표준 기능”이라고 표현할 수 있는 것은 공식 문서의 field·값·wire 구조와 직접 대응하는 부분이다. 표준이 허용하는 여러 방법 중 실제 선택한 방법, 미지원값의 거부, 장비값, 실행 구조, 내부 알고리즘, route와 TEST payload mapping은 project 결정으로 함께 기술해야 한다.
