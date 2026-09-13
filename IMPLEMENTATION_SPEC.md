# VDE-TER AES-256-GCM 통합 구현 명세서

문서 상태: Implementation Specification 1.2

작성 기준일: 2026-09-13

통합 기준: `origin/main` `1918da68d8ce18101b8309a7879e7d99714a7fda`

암호 참조 구현: `feature/aes256-gcm-e2e` `a8ace84da4bf6c49536e12813ca77cc1fb7155ae`

초안 기준선: `모사_링크_통합(초안).pdf`

> 이 문서는 구현 지침이다. 현재 작업에서는 소스 코드를 수정하지 않았다. 아래에서 **Existing**은 최신 `origin/main`에서 확인한 사실, **Validated Design**은 초안과 코드를 대조해 확정한 설계, **Recommended**는 구현을 위해 새로 제안한 사항, **Open Question**은 구현 착수 전에 별도 확정이 필요한 사항을 뜻한다.

## 1. Scope

### 1.1 In Scope

- MASS와 ROC가 세션 키와 방향별 Nonce Prefix를 이미 안전하게 공유한 이후의 application body 보호
- AES-256-GCM을 이용한 기밀성 및 무결성 보호
- 방향별 TX/RX 키, Nonce Prefix, Message Counter 분리
- 7B Security Header, 12B nonce, 22B AAD, 16B GCM tag
- VPFI와 Message Format은 평문 Presentation prefix로 유지하되 AAD로 인증
- Original Source/Destination 및 Security Header의 AAD 인증
- Sliding Replay Window 기반 재전송 공격 방지
- `REQUIRED` / `DISABLED` payload 보안 정책
- 암호화 후 실제 길이에 따른 Short Data 또는 Data Session 선택
- Link 재조립 완료 후 1회 복호화
- 프로그램 재시작 시 동일 key/nonce 쌍이 재사용되지 않도록 하는 counter 상태 관리
- 명시적 실행 모드(`ENFORCED`/`DISABLED`)와 fail-closed startup 검증
- 외부에서 provision된 key/NP의 안전한 적재 및 endpoint별 소유권 검증
- 기존 VDE-TER Presentation, Service, Link, MAC, PHY 경계와의 통합

### 1.2 Out of Scope

- ECDHE 키 교환, ECDSA 신원 인증, PKI 및 인증서 검증
- 세션 수립 handshake 구현
- 자동 rekey 및 다중 epoch 교체 절차
- Shore/ROC 중계 노드가 평문을 복호화하는 hop-by-hop 보안
- fragment별 AES-GCM 처리
- GCM tag 단축
- SC-VDE TEST VPFI `FF 7F`의 공식 운영 할당 주장
- 폐쇄형 `voyager_3` 모사 환경 밖의 표준 상호운용성 및 실제 선박 탑재·운용 승인
- 권한 있는 공격자의 전체 디스크·VM snapshot·backup rollback 또는 복제 탐지
- TPM/HSM/secure element/OS keystore 기반 key-at-rest 보호
- hostile RF 환경의 peer/global rate limit, token bucket 및 무선 자원 고갈 DoS 완화

### 1.3 전제 조건

1. 송수신 양측은 방향별 32B AES key와 8B Nonce Prefix를 이미 공유한다.
2. MASS→ROC와 ROC→MASS 방향 material은 서로 다르다.
3. 이 구현 profile은 epoch `1`만 허용한다. epoch 필드는 wire 호환성과 잘못된 material 탐지를 위해 남기지만, epoch 2 이상 설치·선택·전환 API는 구현하지 않는다.
4. 동일 key를 유지한 채 재시작할 수 있다면 counter 및 RX replay 상태를 영속화한다.
5. SC-VDE 세 payload는 `FF 7F`를 사용하는 이 프로젝트 전용 TEST/PROJECT profile로 고정한다. OPERATIONAL registry 등록이나 외부 구현과의 상호운용성을 주장하지 않는다.
6. 실제 통합 실행은 `ENFORCED`, 기존 평문 회귀시험은 명시적인 `DISABLED` 모드를 사용한다. 누락/`None`으로 모드를 추론하지 않는다.
7. 한도 소진, counter overflow 또는 영속 상태 신뢰 상실 후에는 해당 방향을 이 배포에서 영구 중지한다. 프로세스 재시작이나 상태 삭제로 복구하지 않는다.
8. 정상적인 process crash와 power loss 후 복구는 범위에 포함하지만, 알려진 DB backup 복원·rollback·복제 뒤 동일 key/NP를 계속 쓰는 것은 금지한다. 이 경우 epoch를 올리는 대신 새 key, 새 NP, 새 `state_id`, 새 state DB로 별도 배포한다.
9. 현재 범위에서는 service-account 전용 권한 제한 material JSON을 채택한다. 실제 선박용 hardware-backed keystore는 요구하지 않는다.
10. 별도 InvalidTag rate limiter는 구현하지 않는다. 단, InvalidTag 폐기, 평문 미반환, replay 상태 불변, 실패 계수·metric·비밀 없는 logging 및 `2^20` 실패 budget은 유지한다.

## 2. Terminology

| 용어 | 의미 |
| --- | --- |
| Application body | `SubmitTransferRequest.payload`; codec에 입력되는 평문 bytes |
| Plain Presentation wire | 기존 codec 결과인 VPFI/MF prefix 뒤에 application body를 붙인 bytes |
| Presentation prefix | SC-VDE 기준 exact `FF 7F` 뒤에 MF를 붙인 총 3B |
| Security PDU | Security Header, Ciphertext, GCM Tag를 순서대로 결합한 bytes |
| Secure Presentation wire | Presentation prefix 뒤에 Security PDU를 붙인 bytes |
| Link Segment | Link 계층의 4B unsigned Segment header와 Presentation wire의 결합 |
| Original address | 종단 간 MASS/ROC 주소. ROC 경유 시에도 node address와 구별됨 |
| Link retransmission | 동일한 Link/PDU bytes를 재전송하는 하위 계층 동작 |
| Security replay | 이미 인증·수락된 Security PDU를 공격자가 다시 주입하는 행위 |
| NP | 방향별 8B Nonce Prefix |
| Epoch | 설치된 key/NP 세대를 식별하는 uint16 값 |
| Deployment security lifetime | 동일 key/NP와 영속 상태를 사용하는 기간. 이 profile에서는 epoch 1만 존재하며 한도 소진 시 종료됨 |

## 3. Source of Truth and Design Baseline

판단 우선순위는 다음과 같다.

1. 최신 `origin/main`의 실제 파일, 클래스, 함수, wire 처리 흐름
2. 초안에서 확정되었고 실제 코드와 충돌하지 않는 설계
3. 양쪽에서 정하지 않은 부분에 대한 본 문서의 권장안

### 3.1 브랜치 비교 결과

| 항목 | 검증 결과 | 구현에 미치는 영향 |
| --- | --- | --- |
| 공통 기준점 | `6b0e952c29bda685ec735e984b0703d4ea98f8f8` | feature 브랜치는 이 시점의 main에서 분기됨 |
| 최신 main | `1918da68d8ce18101b8309a7879e7d99714a7fda` | 최종 통합 기준 |
| crypto feature | `a8ace84da4bf6c49536e12813ca77cc1fb7155ae` | 독립 모듈 참조용 |
| 차이 | main이 feature 기준점보다 141 commits 앞서고 feature에는 독자 commit 1개가 있음 | feature에 직접 최신 main을 가정하여 구현하지 말고, 최신 main에서 통합 브랜치를 만든 뒤 필요한 암호 로직을 이식해야 함 |
| `crypto_module/` | feature에만 존재하고 최신 main에는 없음 | 그대로 import하는 런타임 의존성이 아니라 `VDETER/src/vdeter/security/`에 통합해야 함 |
| Presentation/Stage5 | 핵심 함수 위치와 호출 구조가 최신 main에서도 유지됨 | 아래 통합 지점이 유효함 |
| Stage 7 Data Session | 최신 main에서 `data_session_enabled` 설정 및 `Stage7Endpoint._route_admission_check_locked()`의 차단 override가 제거됨 | 암호화 후 Short Data를 초과해도 기존 Data Session 경로 사용 가능. 초안의 별도 enable 조건은 폐기 |
| Stage 7 기타 변화 | host profile, RX decode worker, persistent service session 등 대규모 변경 | security 상태가 endpoint restart와 별개로 유지되도록 wiring 수명 경계를 명확히 해야 함 |

### 3.2 통합 원칙

- 구현 브랜치는 최신 `origin/main`에서 시작한다.
- `crypto_module`을 폴더째 합치지 않는다.
- 재사용 가능한 AES-GCM, header, nonce, replay 로직은 V2 wire 규격에 맞게 수정해 `vdeter.security`로 이식한다.
- `crypto_module/src/vde_secure/vde/`의 자체 segment, reassembly, capacity, transport adapter는 사용하지 않는다. 최신 main의 Link/Data Session 구현과 역할이 중복된다.

## 4. Existing Repository Architecture

### 4.1 실제 주요 파일

```text
voyager_3/
├─ VDETER/src/vdeter/
│  ├─ common/contracts/service.py          # SubmitTransferRequest 공개 DTO
│  ├─ common/contracts/presentation.py     # PresentationEncode/DecodeRequest 및 PresentationData
│  ├─ presentation/
│  │  ├─ adapter.py                        # RegistryPresentationAdapter와 codec dispatch
│  │  ├─ registry.py                       # PayloadFormatRegistry
│  │  └─ codecs/                           # VPFI/MF prefix와 body codec
│  ├─ profiles/
│  │  ├─ payload_formats.py                # payload_format↔VPFI/MF/codec_ref catalogue
│  │  └─ link_phy.py                       # Link 11/17/19 용량
│  ├─ runtime/
│  │  ├─ stage5.py                         # Service submit, encode, 길이 admission, RX delivery
│  │  └─ wiring.py                         # Stage5/Wideband endpoint 조립
│  ├─ stage7/
│  │  ├─ endpoint.py                       # Stage5Endpoint를 재사용하는 실제 endpoint
│  │  ├─ wiring.py                         # build_stage7_b210_endpoint()
│  │  └─ configuration.py                  # Stage 7 resolved configuration
│  └─ link/
│     ├─ transfer.py                       # Short Data/Data Session 분석 및 packet build
│     ├─ segment/codec.py                  # 4B unsigned Segment encode/decode
│     └─ data_session/
│        ├─ fragmentation.py               # 완성 Segment의 Link fragment 생성
│        └─ reassembly.py                  # fragment 재조립 후 Segment decode
└─ VDETER/apps/stage7_endpoint_launcher.py # 실제 Stage 7 endpoint process 조립 호출부
```

### 4.2 Existing TX flow

```text
Application
  → Stage5Endpoint.submit(SubmitTransferRequest)
  → RegistryPresentationAdapter.encoded_size_bound()
  → RegistryPresentationAdapter.encode_payload()
  → codec.encode(application_payload)
  → PresentationData.data 저장
  → LinkTransferManager.analyze_transfer()
  → MAC requirement 제출
  → LinkTransferManager.build_packet()
  → Short Data 또는 Data Session fragmentation
  → MAC → PHY
```

확인된 세부사항:

- `Stage5Endpoint.submit()` Phase A는 registry fingerprint와 encoded size bound를 확인한다.
- Phase B는 service lock 밖에서 `encode_payload()`를 한 번 호출한다.
- Phase C는 실제 `len(PresentationData.data)`가 Phase A bound와 같은지 확인한 후 `_pending_presentation`에 보관한다.
- Link 재전송은 저장된 동일 Presentation bytes를 사용하므로 Presentation encode를 다시 호출하지 않는다.
- Link 분석의 Segment 길이는 `4 + len(presentation.data)`이다.

### 4.3 Existing RX flow

```text
PHY → MAC → Link packet 처리
  → Data Session이면 DataSessionReassembler.process()
  → 완성된 unsigned Segment의 Presentation bytes 복원
  → Stage5Endpoint._deliver_presentation_bytes()
  → RegistryPresentationAdapter.decode_payload()
  → _dispatch_wire_key()로 VPFI/MF codec 선택
  → codec.decode()
  → ReceivedApplicationData → Application event sink
```

### 4.4 기존 crypto 이름 충돌

`runtime/wiring.py`와 `stage7/wiring.py`의 `crypto_dependencies`는 `LinkCryptoDependencies`이며 TBB signer/trust store용이다. AES-GCM 종단 간 보안에 이 이름이나 객체를 재사용하지 않는다. 새 인자는 `e2e_security` 또는 `e2e_security_by_station`으로 명확히 분리한다.

## 5. Draft Validation Results

### 5.1 확정된 결정

| 설계 | 상태 | 결과 |
| --- | --- | --- |
| AES-256-GCM, tag 16B | Validated Design | 유지 |
| application body만 암호화 | Validated Design | codec 검증 후 prefix와 body를 분리해 body만 보호 |
| VPFI/MF 평문 유지 및 AAD 인증 | Validated Design | 수신 codec 선결정을 위해 필요 |
| 7B Security Header | Validated Design | `1+2+4=7` 일치 |
| 12B nonce | Validated Design | `NP 8B + counter 4B=12B` 일치 |
| 방향별 key/NP/counter | Validated Design | MASS↔ROC 양방향 분리 |
| replay precheck→인증→commit | Validated Design | 인증 실패 packet은 replay state를 변경하지 않음 |
| Link 재조립 후 복호화 | Validated Design | 기존 Link fragmentation/reassembly 재사용 |
| Security Control `0x80` | Adopted | V2 exact identifier로 확정 |
| AAD Domain `VDS2` | Adopted | V2 AAD domain separator로 확정 |
| SC-VDE VPFI `FF 7F` | Adopted | 폐쇄형 `voyager_3` 모사에서 프로젝트 전용 값으로 계속 사용 |
| DB rollback threat | Adopted | 악의적 disk/snapshot rollback은 비위협 범위. 알려진 복원 시 동일 material 재사용 금지 |
| key-at-rest | Adopted | 현재 모사 통합은 권한 제한 material JSON 사용 |
| InvalidTag rate limiter | Adopted | 별도 rate limiter는 구현하지 않고 인증 실패 안전 규칙과 `2^20` budget 유지 |

### 5.2 `0x80`과 `VDS2` 채택 검토

결론: **둘 다 채택한다.**

1. standalone V1은 `0x40`, `VDS1`, 14B header, 4B NP+8B counter를 사용한다.
2. 본 통합 V2는 7B header, 8B NP+4B counter, MF를 AAD에 추가하고 Application Type/Ciphertext Length를 제거한다.
3. 서로 호환되지 않는 두 형식이 같은 `0x40/VDS1`을 쓰면 수신자가 잘못된 parser/context를 선택하거나 운영 중 혼동할 수 있다.
4. `0x80`은 version bits `10b`, AES-GCM algorithm id `00b`, flags `0000b`로 해석되어 V2를 조기에 식별한다.
5. `VDS2`는 동일 key가 잘못 재사용되는 구성 실수에서도 V1과 V2의 인증 문맥을 분리한다.

따라서 수신기는 Security Control이 exact `0x80`이 아니면 AES-GCM 호출 전에 거부한다. V1 wire 호환 fallback은 제공하지 않는다.

### 5.3 현재 모사 통합 profile의 범위 확정

다음 네 항목은 더 이상 Open Question이 아니다.

1. `FF 7F`는 폐쇄형 `voyager_3` 시나리오를 위한 프로젝트 전용 VPFI다. 표준·OPERATIONAL 할당값으로 표시하거나 외부 상호운용성을 주장하지 않는다.
2. 악의적 전체 disk/VM snapshot/backup rollback 탐지는 위협 모델에서 제외한다. 다만 운영자가 DB 복원·분실·재생성을 인지한 경우 같은 key/NP로 재개하지 않고 새 key, 새 NP, 새 random `state_id`, 새 DB를 함께 provision한다. epoch 값은 이 profile에서 계속 `1`이다.
3. key-at-rest는 service-account만 읽을 수 있는 protected JSON file로 구현한다. 이 선택은 현재 모사 통합 범위의 최종안이며 TPM/HSM/OS keystore는 구현하지 않는다.
4. peer/global token bucket이나 cooldown 같은 별도 InvalidTag rate limiter는 구현하지 않는다. 이는 인증 검사를 완화한다는 뜻이 아니다. 모든 InvalidTag packet은 폐기하고 평문을 반환하지 않으며 replay state를 바꾸지 않는다. 검증/failure budget, metric, 비밀 없는 logging은 계속 적용한다.

## 6. Design Requirements

| ID | 요구사항 | 상태 |
| --- | --- | --- |
| SEC-REQ-001 | AES key는 정확히 32B여야 한다. | Confirmed |
| SEC-REQ-002 | GCM nonce는 정확히 12B여야 한다. | Validated Design |
| SEC-REQ-003 | GCM tag는 16B이며 축약하지 않는다. | Validated Design |
| SEC-REQ-004 | 동일 key 아래 nonce를 절대로 재사용하지 않는다. | Validated Design |
| SEC-REQ-005 | 방향별 key, NP, TX counter, RX replay state를 분리한다. | Validated Design |
| SEC-REQ-006 | VPFI/MF, Original Source/Destination, Security Header를 AAD로 인증한다. | Validated Design |
| SEC-REQ-007 | `REQUIRED` 송신은 plaintext fallback을 금지한다. | Validated Design |
| SEC-REQ-008 | `REQUIRED` 수신은 secure form만 수락한다. | Validated Design |
| SEC-REQ-009 | 인증 성공 전 replay state를 변경하지 않는다. | Validated Design |
| SEC-REQ-010 | Link retransmission은 새 counter를 소비하지 않고 동일 PDU를 재사용한다. | Validated Design |
| SEC-REQ-011 | counter 또는 영속 저장 실패 시 fail closed한다. | Recommended |
| SEC-REQ-012 | key, NP, nonce, plaintext를 로그에 기록하지 않는다. | Recommended |
| SEC-REQ-013 | 보안 실행 모드는 `ENFORCED` 또는 `DISABLED`로 명시하며 `None`/누락을 평문 허용으로 해석하지 않는다. | Adopted |
| SEC-REQ-014 | `ENFORCED` 시작 시 REQUIRED 정책, 모든 peer 방향 material, state store를 사전 검증하고 하나라도 없으면 endpoint를 시작하지 않는다. | Adopted |
| SEC-REQ-015 | AES key는 peer별·방향별로 고유해야 하며 group key, TX/RX 동일 key, 중복 `(key, NP)`를 거부한다. | Adopted |
| SEC-REQ-016 | key/NP 원문은 repository, CLI, 환경변수, 로그, `repr` 및 상태 DB에 저장하지 않는다. | Adopted |
| SEC-REQ-017 | epoch는 정확히 1만 허용하며 한도 소진 후 자동 wrap, 상태 삭제, 동일 material 재사용으로 복구하지 않는다. | Adopted |
| SEC-REQ-018 | 암호문 길이는 AES 호출 전에 해당 codec이 허용하는 전체 body 길이 집합으로 검증한다. | Adopted |
| SEC-REQ-019 | 내부 security 오류는 기존 `InternalProtocolError`와 `Layer.PRESENTATION`을 사용하고 공개 오류로 명시적으로 projection한다. | Adopted |
| SEC-REQ-020 | `FF 7F`는 프로젝트 전용 TEST/PROJECT VPFI로 사용하며 표준·OPERATIONAL 상호운용성을 주장하지 않는다. | Adopted |
| SEC-REQ-021 | 알려진 state DB 복원·분실·재생성 뒤 동일 key/NP를 재사용하지 않고 새 key/NP/`state_id`/DB를 함께 배포한다. | Adopted |
| SEC-REQ-022 | 현재 모사 통합의 key-at-rest 구현은 권한 제한 material JSON이며 hardware-backed keystore는 요구하지 않는다. | Adopted |
| SEC-REQ-023 | 별도 InvalidTag rate limiter는 구현하지 않되, InvalidTag 폐기·평문 미반환·replay 불변·실패 계수와 `2^20` failure budget을 유지한다. | Adopted |

### 6.1 보장 범위와 명시적 비보장

- 보호됨: application body의 기밀성, body와 AAD field의 무결성/peer-key 보유자 인증, 수락된 PDU의 replay 방지.
- 평문으로 보임: VPFI, MF, source/destination을 포함한 하위계층 header, packet 길이, fragment 수, 송수신 시각/빈도. 따라서 traffic analysis와 message 종류/대략적 waypoint 수 은닉은 제공하지 않는다.
- 제공하지 않음: 전자서명/부인방지, key를 공유하지 않은 제3자에 대한 신원 증명, wall-clock freshness, 가용성 보장. 대칭 key 보유자는 해당 방향의 유효 tag를 생성할 수 있다.
- 위협 모델 밖: privileged host/root compromise, raw material file 탈취, 악의적 전체 disk·VM snapshot·backup rollback/복제, hostile RF traffic-rate 기반 자원 고갈 DoS. 이를 탐지·완화하기 위한 hardware anchor, remote ledger, rate limiter는 구현하지 않는다.
- 범위 안: 정상 process crash/power loss의 SQLite crash consistency, startup material/state 결속 검증, 알려진 DB 복원·분실 시 동일 material 사용 거부 절차, 모든 InvalidTag의 fail-closed 처리.

## 7. Target Architecture

### 7.1 목표 package tree

```text
VDETER/src/vdeter/
├─ security/                                  # 새 package
│  ├─ __init__.py                             # 제한된 public API export
│  ├─ constants.py                            # V2 wire 상수와 한도
│  ├─ dependencies.py                         # E2eSecurityDependencies
│  ├─ errors.py                               # security 내부 오류 생성/projection helper
│  ├─ policy.py                               # payload_format별 REQUIRED/DISABLED 정책
│  ├─ metrics.py                              # 경량 counter sink
│  ├─ presentation.py                         # Presentation Adapter와의 protect/unprotect 경계
│  ├─ crypto/
│  │  └─ aes_gcm.py                           # detached encrypt/decrypt wrapper
│  ├─ protocol/
│  │  ├─ header.py                            # 7B SecurityHeader
│  │  ├─ nonce.py                             # 8B NP + 4B counter
│  │  ├─ aad.py                               # 22B canonical AAD
│  │  └─ pdu.py                               # Header||Ciphertext||Tag
│  └─ session/
│     ├─ material.py                          # 방향별 immutable material
│     ├─ provider.py                          # 외부 provisioned material 적재 interface/파일 구현
│     ├─ state_store.py                       # 영속 상태 interface와 SQLite 구현
│     ├─ replay.py                            # Sliding Replay Window
│     └─ manager.py                           # peer/direction/epoch session lookup
├─ presentation/adapter.py                    # security collaborator 호출
├─ common/errors.py                           # Presentation security 내부 오류 code 추가
├─ runtime/projection.py                      # security 오류의 공개 Service 오류 mapping
├─ runtime/wiring.py                          # Stage5/Wideband security 주입
└─ stage7/wiring.py                           # Stage7 security 주입

VDETER/tests/
├─ unit/security/                             # wire/crypto/session/policy 단위시험
└─ integration/                               # Presentation↔Link↔Data Session 통합시험

VDETER/tools/e2e_security_provision.py        # 기존 공유 material 검증 및 초기 state DB 생성
```

### 7.2 책임 경계

| 계층 | 책임 |
| --- | --- |
| Application | destination, payload_format, application body, ack_required 제공 |
| Service | 요청 admission, route 생성, transfer 수명, 실제 encoded 길이 검증 |
| Presentation codec | application body 구조/길이 검증 및 평문 VPFI/MF wire 생성 |
| Presentation security | 정책, session, counter, header/nonce/AAD, AES-GCM, replay |
| Link | 4B Segment header, Short/Data Session 선택, fragmentation/reassembly, 재전송 |
| MAC/PHY | 기존 scheduling 및 무선 송수신 |

Security는 Presentation 처리 내부 collaborator이다. Application, Link, MAC, PHY가 AES-GCM을 직접 호출하지 않는다.

### 7.3 실행 모드

```python
class E2eSecurityMode(str, Enum):
    DISABLED = "DISABLED"
    ENFORCED = "ENFORCED"
```

| Mode | 의미 | 허용 조건 |
| --- | --- | --- |
| `ENFORCED` | 본 명세의 VDS2 정책을 적용한다. REQUIRED body는 반드시 암호화/인증한다. | 실제 software/Stage 7 통합 실행의 규범 모드 |
| `DISABLED` | 기존 Presentation wire를 byte-for-byte 유지한다. | 기존 평문 회귀시험 또는 명시적으로 승인된 legacy 실행만 |

필수 규칙:

1. builder와 launcher는 mode를 필수 입력으로 받는다. 인자 누락, `None`, 알 수 없는 문자열은 `CONFIG_SEMANTIC_INVALID`로 startup 실패한다.
2. `ENFORCED`에서는 VDS2 정책 table, endpoint의 모든 peer별 TX/RX material, state DB lock/open이 준비되어야 한다. 일부만 준비된 상태로 시작하지 않는다.
3. `DISABLED`에서는 `LEGACY_ALL_DISABLED` 정책만 허용한다. REQUIRED entry가 하나라도 설정된 policy와 함께 사용할 수 없으며, 수신 bytes를 secure/plain으로 자동 탐지하지 않는다.
4. mode는 endpoint가 실행 중일 때 변경할 수 없다. 변경하려면 최종 종료 후 새 endpoint graph를 조립해야 한다.
5. MASS/ROC software 시나리오와 Stage 7 SC-VDE-005/006 검증은 `ENFORCED`를 명시한다. 암호 통합 전 동작을 확인하는 회귀 fixture만 `DISABLED`를 명시한다.

## 8. Presentation / Security Boundary

### 8.1 TX 경계

1. codec이 먼저 application body의 길이와 필드 구조를 검증한다.
2. codec 결과 `plain_wire`에서 현재 REQUIRED SC-VDE format의 exact 3B prefix를 확인한다.
3. `body = plain_wire[3:]`만 AES-GCM으로 암호화한다.
4. 최종 결과는 `prefix || Security PDU`이다.

단순히 임의의 `len(prefix)`를 잘라서는 안 된다. 현재 REQUIRED entry는 build 시 다음 조건을 검사한다.

- `codec.discriminator.prefix`가 exact 3B
- `third_byte_mask == 0`
- prefix가 registry의 VPFI/MF와 exact 일치
- codec 결과가 해당 prefix로 시작

향후 bit-mask discriminator를 가진 format을 REQUIRED로 바꾸려면 별도 canonical prefix extraction 규칙을 먼저 추가한다.

### 8.2 RX 경계

1. `_dispatch_wire_key()`가 평문 prefix만 읽어 entry와 codec을 선택한다.
2. 정책이 REQUIRED이면 prefix 뒤를 Security PDU로 parse한다.
3. 인증/복호화 성공 후 `plain_wire = prefix || plaintext_body`를 복원한다.
4. 기존 `codec.decode(plain_wire)`로 구조 검증 및 application body 복원을 수행한다.

이 순서는 암호문 body를 기존 codec에 직접 넘기는 오류를 막고, 인증 후에도 codec 의미 검증을 유지한다.

## 9. Wire Format

### 9.1 전체 layout

```text
Secure Presentation Wire
┌──────────────┬───────────────────────────────────────────────────────┐
│ Prefix (3B)  │ Security PDU                                         │
│ VPFI(2)+MF(1)│ Header(7) || Ciphertext(N) || GCM Tag(16)            │
└──────────────┴───────────────────────────────────────────────────────┘
```

### 9.2 byte table

| Offset | Field | Size | Encoding | Encrypted | AAD | Source | Validation |
| ---: | --- | ---: | --- | --- | --- | --- | --- |
| 0 | VPFI wire bytes | 2B | codec의 exact bytes | No | Yes | codec/registry | entry와 exact 일치 |
| 2 | Message Format | 1B | uint8 | No | Yes | codec/registry | 선택 entry와 일치 |
| 3 | Security Control | 1B | uint8 `0x80` | No | Yes | security constant | exact equality |
| 4 | Key Epoch | 2B | uint16 big-endian | No | Yes | TX session | exact `1` |
| 6 | Message Counter | 4B | uint32 big-endian | No | Yes | TX state store | `1..2^24` |
| 10 | Ciphertext | N B | AES-GCM output | Yes | GCM이 인증 | plaintext body | codec별 기대 길이와 최종 일치 |
| 10+N | Authentication Tag | 16B | 128-bit | No | GCM 결과 | AES-GCM | 인증 실패 시 drop |

### 9.3 길이 식

```text
Security Header       = 7B
Security PDU          = 7 + N + 16 = N + 23B
Secure Presentation   = 3 + N + 23 = N + 26B
Plain Presentation    = 3 + N
Security overhead     = 23B
Unsigned Link Segment = 4 + Secure Presentation = N + 30B
```

Ciphertext Length 필드는 없다. PDU가 Link Segment 길이로 경계 지어지므로 `N = len(pdu) - 7 - 16`으로 계산한다.

### 9.4 ciphertext 길이 선검증

AES-GCM은 ciphertext 길이와 plaintext 길이가 같고 packet 전체 길이는 무선에서 이미 관측 가능하므로, codec별 허용 body 길이를 인증 전에 검사해도 새로운 기밀 정보가 노출되지 않는다. 이 검사는 비정상 크기 packet이 AES와 영속 RX budget을 소모하는 것을 줄이는 구조 검증이다.

| MF / Codec | 허용 ciphertext length |
| --- | --- |
| MF1 `ScVdePositionReportCodec` | exact `14` |
| MF2 `ScVdeRouteControlCommandCodec` | `{10,20,29,39,48,58,68,77,87,97,106,116,125,135,145,154,164}` |
| MF3 `ScVdeRouteControlAckCodec` | exact `3` |

현재 예제에서 관측된 일부 길이만 허용해서는 안 된다. MF2의 위 집합은 `presentation/codecs/sc_vde_route_control.py::COMMAND_ALLOWED_APPLICATION_BYTES` 및 이를 mirror하는 `common/vdeter_bridge.py::SC_VDE_COMMAND_LENGTHS`, 즉 `ceil((76+77n)/8), n=0..16` 전체와 일치한다. 구현에서는 목록을 security package에 다시 복사하지 않고 선택된 codec의 기존 `encoded_size_bound(N)`을 호출해 성공 여부와 반환값 `prefix_length+N`을 확인한다. 길이만 맞는 암호문도 tag 인증 전에는 신뢰하지 않으며, 복호화 후 기존 `codec.decode()`를 다시 수행한다.

### 9.5 deterministic dummy vector

다음 값은 테스트용이며 실제 key로 사용하지 않는다.

| 항목 | Hex |
| --- | --- |
| AES key | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| NP | `a0a1a2a3a4a5a6a7` |
| Header | `80000100000001` |
| Nonce | `a0a1a2a3a4a5a6a700000001` |
| Prefix | `ff7f03` |
| AAD | `56445332ff7f0300000064000000c880000100000001` |
| Plain body | `01d000` |
| Ciphertext | `325357` |
| Tag | `8abcd98f6ebc07e1f80d1c027e461b4a` |
| Security PDU | `800001000000013253578abcd98f6ebc07e1f80d1c027e461b4a` |
| Secure Presentation wire | `ff7f03800001000000013253578abcd98f6ebc07e1f80d1c027e461b4a` |

이 예에서 Source ID=100, Destination ID=200, Epoch=1, Counter=1이다. Secure Presentation 길이는 29B, Link Segment 길이는 33B이다.

## 10. Security Header

### 10.1 구조

```text
SecurityHeader = SecurityControl(1) || KeyEpoch(2) || MessageCounter(4)
```

Python 표현은 `struct.Struct(">BHI")`를 사용하며 `calcsize()`가 7인지 import/test 시 검증한다.

| Field | 허용값 | TX 생성 | RX validation |
| --- | --- | --- | --- |
| Security Control | exact `0x80` | constant | 다른 값은 `PRESENTATION_SECURITY_PROFILE_UNSUPPORTED` |
| Key Epoch | exact `1` | TX session constant | 1이 아니면 AES 호출 전에 `PRESENTATION_SECURITY_EPOCH_MISMATCH` |
| Message Counter | 1..`2^24`(wire는 uint32) | atomic reservation | 0 또는 `2^24` 초과는 AES 호출 전에 counter 오류/replay 거부 |

### 10.2 Security Control bit field

```text
bit 7..6 : protocol version = 2 (`10b`)
bit 5..4 : algorithm id     = 0 (`00b`, AES-256-GCM)
bit 3..0 : flags            = 0 (`0000b`)
결과                              1000 0000 = 0x80
```

현재 parser는 부분 bit 수락이 아니라 byte exact equality를 사용한다. 알 수 없는 version/algorithm/flag를 추측하여 처리하지 않는다.

## 11. AES-256-GCM Profile

| Parameter | 값 |
| --- | --- |
| Algorithm | AES-256-GCM |
| Key length | 32B |
| Nonce length | 12B |
| Tag length | 16B |
| Ciphertext length | plaintext body와 동일 |
| API behavior | detached `(ciphertext, tag)` |
| Authentication failure | 평문을 반환하지 않고 단일 오류로 처리 |

`cryptography.hazmat.primitives.ciphers.aead.AESGCM`을 사용한다. 라이브러리가 반환하는 `ciphertext || tag`를 wrapper가 마지막 16B 기준으로 분리한다. decrypt 시에는 다시 결합한다. key, nonce, AAD 길이는 호출 전에 검증한다.

NIST SP 800-38D의 96-bit IV 권장과 IV uniqueness 요구를 따른다. tag는 128-bit를 유지한다.

작성 기준일 현재 NIST는 SP 800-38D 개정을 준비 중이지만 최종 개정판이 아니라 pre-draft 의견수렴 단계다. 따라서 본 profile의 규범 참조는 현재 Final인 SP 800-38D(2007)이며, 향후 최종 revision 발행 시 usage-limit/tag 요구를 재검토한다.

### 11.1 프로젝트별 key-usage budget

다음 값은 NIST가 모든 시스템에 강제하는 단일 상수가 아니라 기존 `crypto_module`에서 가져온 보수적 프로젝트 정책이다.

| Budget | 값 | 집계 규칙 | 소진 시 동작 |
| --- | ---: | --- | --- |
| TX nonce reservations | `2^24`/key/direction | counter를 예약할 때 1 증가 | 해당 TX 방향 영구 중지 |
| TX GCM authentication blocks | `2^32`/key/direction | `ceil(22/16)+ceil(N/16)+1` | 해당 TX 방향 영구 중지 |
| RX verification blocks | `2^32`/key/direction | 실제 AES-GCM verify 시도 전에 같은 식으로 예약 | 해당 RX 방향 영구 중지 |
| RX authentication failures | `2^20`/key/direction | `InvalidTag`만 1 증가 | 해당 RX 방향 영구 중지 및 보안 경보 |

현재 REQUIRED codec의 최대 body는 164B이므로 한 invocation은 최대 `2+11+1=14` blocks다. 따라서 TX nonce 상한 `2^24`를 지키면 최악에도 `14×2^24=234,881,024 < 2^32` blocks이므로 TX block budget이 먼저 소진되지는 않는다. 그래도 invariant와 향후 codec 확장 검사를 위해 두 budget을 모두 구현한다.

RX 실패 한도는 공격자가 의도적으로 소진시킬 수 있는 DoS 경계다. 현재 모사 통합에서는 한도를 제거하지 않고 pre-auth 길이 검증, 실패 metric과 임계치 경보를 적용한다. peer/global token bucket, cooldown 또는 MAC ingress rate limiter는 구현하지 않는다. 따라서 이 profile은 hostile RF 자원 고갈 DoS에 대한 가용성을 보장하지 않지만, rate limiter 부재가 InvalidTag 수락·평문 반환·replay state 변경으로 이어져서는 안 된다.

## 12. Key and Session Model

### 12.1 방향 매핑

| 방향 | MASS TX | ROC RX | ROC TX | MASS RX |
| --- | --- | --- | --- | --- |
| MASS→ROC | `K_M2R`, `NP_M2R`, TX counter | 같은 `K_M2R`, `NP_M2R`, replay state | - | - |
| ROC→MASS | - | - | `K_R2M`, `NP_R2M`, TX counter | 같은 `K_R2M`, `NP_R2M`, replay state |

MASS와 ROC는 두 방향의 material을 모두 보유하지만 자신의 송신 방향에서는 TX session을, 수신 방향에서는 RX session을 사용한다.

### 12.2 제안 data types

```python
@dataclass(frozen=True, slots=True)
class DirectionalSessionMaterial:
    source_id: StationId
    destination_id: StationId
    key_epoch: int                         # exact 1
    aes_key: bytes = field(repr=False)     # exact 32B
    nonce_prefix: bytes = field(repr=False)# exact 8B

@dataclass(slots=True)
class TxSecuritySession:
    material: DirectionalSessionMaterial
    state_store: SecurityStateStore
    lock: RLock

@dataclass(slots=True)
class RxSecuritySession:
    material: DirectionalSessionMaterial
    replay_window: ReplayWindow
    state_store: SecurityStateStore
    lock: RLock
```

`SecuritySessionManager` lookup key는 `(source_id, destination_id, key_epoch)`이다. TX lookup은 local source와 requested original destination, RX lookup은 received original source와 local destination을 사용한다.

### 12.3 key/NP 필수 규칙

1. material은 **peer별·방향별 pairwise**로 provision한다. 하나의 ROC group key를 MASS-A와 MASS-B가 공유하면 어느 보유자든 AAD의 Source ID를 바꾸어 다른 선박으로 가장할 수 있으므로 금지한다.
2. 세 endpoint 예시에서 MASS-A와 MASS-B는 각각 2개(TX 1, RX 1), ROC는 peer별 4개(A→ROC RX, ROC→A TX, B→ROC RX, ROC→B TX)의 directional material을 가진다.
3. 한 endpoint 안에서 서로 다른 logical direction의 key는 달라야 한다. 전체 배포 inventory에서 같은 logical `(source,destination,epoch)`의 송신측 TX와 수신측 RX **reciprocal pair**만 같은 key/NP를 가질 수 있다. 그 pair 외의 peer/direction에서 동일 AES key 또는 동일 `(key, NP)` fingerprint가 나타나면 startup을 실패시킨다.
4. key는 정확히 32B, NP는 정확히 8B, epoch는 정확히 1이며 source/destination은 서로 다른 nonzero `StationId`여야 한다.
5. AES key는 CSPRNG 또는 승인된 KDF가 만든 독립적인 256-bit secret이어야 한다. password, MMSI, UUID, timestamp를 그대로 쓰거나 반복/zero-padding해 32B로 만든 값은 거부한다. provider는 최소한 `generation_method`/provisioning provenance를 검증 가능한 metadata로 요구하되 원문 secret은 기록하지 않는다.
6. 수신 endpoint의 RX record는 송신 endpoint TX record와 `(source, destination, key, NP, epoch)`가 byte-for-byte 일치해야 한다. 반대 방향 record는 source/destination이 뒤집히고 key/NP도 달라야 한다.
7. `aes_key`와 `nonce_prefix`는 `repr=False`로 두고 `dataclasses.asdict()`, 일반 JSON 직렬화, exception message, metrics label에 전달하지 않는다. key 비교에는 constant-time 비교를 사용하고, 중복 검사용 fingerprint는 process 내부에서만 사용한다.
8. 원문 key/NP는 repository, tracked TOML, CLI 인자, 환경변수, 상태 DB에 두지 않는다. Stage 7은 repository 밖의 권한 제한 material file을 `ProtectedFileSecurityMaterialProvider`가 읽고, 시험은 `InMemorySecurityMaterialProvider`를 사용한다.
9. material file 권장 경로는 `/var/lib/vdeter/security/<endpoint-id>/materials.json`, directory mode `0700`, file mode `0600`, owner는 launcher service account 단독이다. 권한/owner가 다르거나 symlink/비정규 파일이면 startup을 실패시킨다.
10. material file에는 endpoint ID, local station ID, peer별 direction, source/destination, epoch, provenance, base64 key/NP만 둔다. parser는 duplicate JSON key, unknown/missing field, non-canonical base64, trailing data를 거부하고 읽은 임시 mutable buffer는 가능한 범위에서 즉시 지운다.

`material_id`는 상태 row 결속을 위한 SHA-256 기반 식별자일 뿐 인증 key가 아니다. 외부 API/공개 오류/로그에 전체 값을 노출하지 않는다.

### 12.4 수명

- manager는 Runtime/Endpoint 조립 시 1회 만들어지고 Presentation security에 주입된다.
- `Stage5Endpoint.stop()`의 실행별 reset으로 key/counter/replay 상태를 초기화하지 않는다.
- 최신 main의 `_reset_stage5_runtime_locked()`는 Presentation Adapter를 유지한다. 기존의 “무상태 adapter” 주석은 통합 후 “endpoint 수명 상태를 보유한 security collaborator를 유지”하는 의미로 수정해야 한다.
- key material 교체 없이 counter를 1로 되돌리는 API는 제공하지 않는다.
- epoch 2 이상을 설치하거나 현재 material을 교체하는 runtime API는 제공하지 않는다. 한도 소진 후 복구는 이 profile 밖에서 완전히 새로운 key/NP와 새 배포 identity/state를 provision하는 작업이다.

## 13. Nonce Management

### 13.1 구조

```text
Nonce = NoncePrefix(8B) || MessageCounter(4B big-endian)
```

| 구성 요소 | 규칙 |
| --- | --- |
| NP | 방향 및 key epoch에 결속된 immutable 8B 값 |
| Counter | 해당 방향/key/epoch에서 단조 증가하는 uint32 |
| 최초 counter | 1 |
| 0 | 예약값, 송수신 거부 |

### 13.2 uniqueness 보장

1. 방향마다 key와 NP를 분리한다.
2. 한 방향/epoch에서는 counter를 중복 예약하지 않는다.
3. 암호화 실패 또는 crash 후에도 예약한 counter를 재사용하지 않는다.
4. Link retransmission은 새 암호화를 하지 않고 동일 nonce/ciphertext/tag를 포함한 동일 PDU를 재사용한다.
5. 본 profile은 epoch 변경을 지원하지 않는다. epoch 1 material의 한도 소진 또는 상태 신뢰 상실 시 동일 배포를 종료한다.

NP가 랜덤인지 KDF 산출인지 자체는 이 단계 범위 밖이다. 단, 외부 세션 수립자가 각 peer/direction에 고유한 8B 값을 제공해야 하며 bootstrap은 배포 inventory의 중복 fingerprint를 거부한다.

## 14. Message Counter Management

### 14.1 wire 범위와 운용 한도

- wire 표현 범위: 1..`0xFFFFFFFF`
- 확정 운용 상한: 기존 standalone의 보수적 key usage 정책을 이어 `2^24` reservations/key/direction
- 운용 상한 도달 시 자동 wrap 금지. 해당 TX 방향을 영구 `EXHAUSTED`로 만들고 이 배포에서는 다시 열지 않는다.

4B 필드는 운용 상한을 충분히 담으면서 기존 8B counter보다 4B를 절약한다. 필드 크기와 실제 사용 한도는 구분한다.

### 14.2 예약 시점

1. policy 및 codec 구조 검증 성공
2. TX session lookup 성공
3. 영속 counter 예약
4. Header/Nonce/AAD 생성
5. AES-GCM 수행

counter는 AES-GCM 호출 직전에 예약한다. 암호화 실패 시에도 소비된 것으로 취급한다. `encoded_size_bound()`에서는 counter를 예약하지 않는다.

예약 API는 body 길이를 함께 받아 nonce reservation과 GCM block budget을 하나의 durable transaction에서 검사/차감한다. 어떤 검사라도 실패하면 AES-GCM을 호출하지 않고, 이미 durable commit된 counter를 rollback/재사용하지 않는다.

### 14.3 재전송

Stage5 Phase B에서 secure Presentation wire는 한 번 만들어지고 Phase C에서 `_pending_presentation`에 보관된다. Link/MAC retransmission은 이 bytes를 재사용한다. 재전송을 위해 `encode_payload()`를 다시 호출하거나 새 counter를 만들면 안 된다.

### 14.4 종료 의미

- `ACTIVE`: 새 protect/unprotect 호출을 받을 수 있는 정상 방향.
- `EXHAUSTED`: nonce/GCM/verification/failure budget 또는 uint32 범위를 소진한 terminal 상태.
- `INHIBITED`: state store 오류·알려진 restore/partial state·metadata/material 불일치로 안전성을 보장할 수 없는 terminal 상태. 일관된 전체 snapshot rollback을 탐지한다는 뜻은 아니다.
- `CLOSED`: process 최종 종료를 위해 manager가 닫힌 terminal 상태.

`EXHAUSTED`, `INHIBITED`, `CLOSED`에서 `ACTIVE`로 돌아가는 API는 없다. `Service.stop()/start()`, Stage 7 lane restart, 프로세스 재시작, state file 삭제는 복구 수단이 아니다. state file 부재·알려진 backup 복원·corruption 상태에서 동일 material을 사용한 startup은 실패시킨다. 복구하려면 외부에서 새로 제공된 key/NP와 새 random `state_id`를 새 DB에 provision해야 한다. 새 material의 생성·교환은 범위 밖이지만, 제공된 새 material의 검증·설치는 provisioning 도구 범위에 포함한다. epoch는 새 배포에서도 `1`이다.

## 15. AAD

### 15.1 canonical layout

```text
AAD = Domain(4)
   || VPFIWire(2)
   || MessageFormat(1)
   || OriginalSourceID(4)
   || OriginalDestinationID(4)
   || SecurityHeader(7)

총 22B
```

| Offset | Field | Size | Encoding | TX source | RX source | 이유 |
| ---: | --- | ---: | --- | --- | --- | --- |
| 0 | Domain | 4B | ASCII `VDS2` | constant | constant | 다른 프로토콜/AAD schema와 분리 |
| 4 | VPFI | 2B | 실제 wire bytes | codec output prefix | received prefix | payload family 바꿔치기 방지 |
| 6 | MF | 1B | 실제 wire byte | codec output prefix | received prefix | 같은 VPFI 내 format 바꿔치기 방지 |
| 7 | Source ID | 4B | uint32 big-endian | `route.original_source.station_id` | Link가 복원한 original source | 발신자 바인딩 |
| 11 | Destination ID | 4B | uint32 big-endian | addressed original destination | 수신 route original destination | 목적지 바인딩 |
| 15 | Security Header | 7B | exact serialized bytes | 방금 생성한 header | PDU에서 받은 header | epoch/counter/control 인증 |

중요: SC-VDE registry의 logical VPFI는 `16383`이지만 wire는 exact `FF 7F`이다. AAD에는 logical integer를 big-endian `3F FF`로 변환한 값이 아니라 실제 prefix의 `FF 7F`를 넣는다.

## 16. Replay Protection

### 16.1 상태

- 기본 window size: 64
- `highest_authenticated_counter`: 아직 수락 없음이면 0
- 64-bit bitmap: bit 0은 highest, bit d는 `highest-d` 수락 여부

### 16.2 판정

```python
def precheck(counter):
    if counter == 0:
        reject(COUNTER_OUT_OF_RANGE)
    if highest == 0 or counter > highest:
        return CANDIDATE_NEW
    distance = highest - counter
    if distance >= window_size:
        reject(REPLAY_TOO_OLD)
    if bitmap & (1 << distance):
        reject(REPLAY_DUPLICATE)
    return CANDIDATE_OUT_OF_ORDER

def commit_authenticated(counter):
    # 동일 session lock 안에서 precheck를 다시 수행한다.
    if highest == 0:
        highest, bitmap = counter, 1
    elif counter > highest:
        shift = counter - highest
        bitmap = 1 if shift >= window_size else ((bitmap << shift) | 1) & mask
        highest = counter
    else:
        bitmap |= 1 << (highest - counter)
    persist_before_application_delivery()
```

### 16.3 필수 순서

```text
parse/범위 확인
  → replay precheck (state 변경 없음)
  → AAD/nonce 재구성
  → AES-GCM authentication/decryption
  → session lock에서 fresh 여부 재검사
  → replay state durable commit
  → codec decode
  → application 전달
```

인증되지 않은 packet은 replay bitmap/highest를 변경할 수 없다. 병렬 수신 사이 race를 막기 위해 commit 직전 동일 lock 아래 재검사한다.

### 16.4 Link retransmission과의 관계

- 하위 Link가 중복을 자체 흡수하면 Security는 완성 PDU를 한 번만 본다.
- 완성 PDU가 다시 Presentation까지 도달하면 같은 counter이므로 duplicate로 drop한다.
- 이것은 Link 전송 실패 처리와 별개이며 application에 두 번 전달하지 않기 위한 최종 방어선이다.

## 17. Payload Security Policy

정책은 기존 `PayloadFormatEntry`에 필드를 추가하지 않고 `security/policy.py`에 둔다. 기존 registry entry는 표준 mapping과 fingerprint의 source of truth이므로 보안 배포 정책과 분리하는 편이 변경 영향을 줄인다. Runtime 시작 시 registry와 정책을 교차 검증한다.

| Payload Format | VPFI / MF | Codec | Policy | 이유 |
| --- | --- | --- | --- | --- |
| `sc_vde.position_report` | wire `FF 7F` / MF1 | `ScVdePositionReportCodec` | REQUIRED | 위치 정보 기밀성·무결성 필요 |
| `sc_vde.route_control.command` | wire `FF 7F` / MF2 | `ScVdeRouteControlCommandCodec` | REQUIRED | 조종 명령 보호 필요 |
| `sc_vde.route_control.ack` | wire `FF 7F` / MF3 | `ScVdeRouteControlAckCodec` | REQUIRED | 명령 결과 위조 방지 |
| `x509.certificate_report` | VPFI 0 / MF1 | `CertificateReportCodec` | DISABLED | 인증서 전달 기존 wire 호환 유지 |
| `ais.authentication_integrity.scheme1` | VPFI 7 / MF1 | `AisAuthenticationScheme1Codec` | DISABLED | 자체 인증 형식과 중복 보호를 이번 범위에서 적용하지 않음 |
| `test.stage0.opaque` | TEST VPFI / MF 없음 | `OpaqueTestCodec` | DISABLED | 기존 시험 fixture 호환; 보안 시나리오에서는 사용 금지 |
| 미등록 format | - | - | Reject | downgrade 및 미정 형식 방지 |

주의:

- SC-VDE 세 entry의 `FF 7F`는 폐쇄형 `voyager_3` 모사용 프로젝트 전용 값이다. OPERATIONAL 또는 외부 상호운용 profile로 승격하는 작업은 현재 범위에 없으며, 문서·로그·UI에서도 표준 할당값으로 표시하지 않는다.
- REQUIRED 송신에서 session이 없거나 암호화가 실패하면 plaintext로 내려보내지 않는다.
- REQUIRED 수신의 prefix 뒤가 plaintext body처럼 보여도 secure PDU parse/control 검증 실패로 drop한다.
- DISABLED 수신에 우연히 `0x80`이 나타난다고 secure로 재해석하지 않는다. 정책이 wire 해석을 결정한다.

## 18. TX Processing

| Step | 실제/제안 호출 | 입력 → 출력 | Validation / state mutation / failure |
| ---: | --- | --- | --- |
| 1 | `Stage5Endpoint.submit()` Existing | `SubmitTransferRequest` | type, non-empty, max application length |
| 2 | `RegistryPresentationAdapter.encoded_size_bound()` Modified | payload format + body length → final wire bound | registry lookup, codec plain bound, REQUIRED이면 +23; state 변경 없음 |
| 3 | `_route_admission_check_locked()` Existing | destination + final bound | broadcast/address/deployment 검사 |
| 4 | `RegistryPresentationAdapter.encode_payload()` Modified | PresentationEncodeRequest | registry/route/ack 검증 |
| 5 | `codec.encode()` Existing | application body → plain wire | 길이 및 field 구조 검증 |
| 6 | `PayloadSecurityPolicy.for_format()` New | entry → REQUIRED/DISABLED | `ENFORCED` mode와 policy 일치; 누락은 시작 실패 또는 요청 거부 |
| 7 | `PresentationSecurity.protect()` New | entry, codec, plain wire, route | prefix/body exact 분리 |
| 8 | `SecuritySessionManager.tx_session()` New | original src/dst | pairwise epoch 1 session lookup; 없으면 fail closed |
| 9 | `TxSecuritySession.reserve_invocation(body_length)` New | body length → counter | nonce/GCM budget와 counter를 durable 예약; 실패 시 방향 terminal |
| 10 | `SecurityHeader.to_bytes()` New | control/epoch/counter → 7B | exact range 확인 |
| 11 | `build_nonce()` New | NP/counter → 12B | exact length 확인 |
| 12 | `build_aad()` New | prefix, route, header → 22B | actual `FF7F/MF` bytes 사용 |
| 13 | `encrypt_detached()` New | key, nonce, body, AAD | ciphertext+tag; 실패해도 counter 소모 |
| 14 | `build_security_pdu()` New | header, CT, tag → PDU | length invariant |
| 15 | adapter | prefix + PDU → `PresentationData.data` | 실제 길이가 secure bound와 exact 일치 |
| 16 | `Stage5Endpoint.submit()` Phase C Existing | final wire | fingerprint/bound 재검증 후 cache |
| 17 | `LinkTransferManager.analyze_transfer()` Existing | final wire | 4B Segment 포함 실제 길이로 후보 분석 |
| 18 | `_select_link11_candidate()` Existing | requirement | Short 불가 시 addressed Data Session |
| 19 | Link Existing | Segment/packet/fragments | 암호화 이후 fragmentation |

실패 시 Phase C는 provisional transfer를 rollback하고 공개 transfer ID를 남기지 않는다. counter gap은 허용하며 재사용하지 않는다.

## 19. RX Processing

| Step | 실제/제안 호출 | 처리 | 실패 시 |
| ---: | --- | --- | --- |
| 1 | Link Existing | packet/fragment 검증 | 기존 Link 오류 처리 |
| 2 | `DataSessionReassembler.process()` Existing | 완성 Segment 재조립 | 불완전/충돌/한도 초과 drop |
| 3 | Segment decode Existing | exact Presentation bytes 복원 | malformed segment drop |
| 4 | `Stage5Endpoint._deliver_presentation_bytes()` Existing | route와 bytes를 adapter로 전달 | adapter 오류 event 기록 |
| 5 | `RegistryPresentationAdapter._dispatch_wire_key()` Existing | prefix로 entry/codec 결정 | unknown/truncated prefix drop |
| 6 | policy lookup New | REQUIRED/DISABLED | unknown policy drop |
| 7 | `parse_security_pdu()` New | header, CT, tag 분리 | 최소 길이/control/epoch/counter 오류 drop |
| 8 | codec `encoded_size_bound(len(CT))` Existing 활용 | entry + CT length | 실패하거나 결과가 `prefix_length+len(CT)`와 다르면 AES 전에 drop |
| 9 | `SecuritySessionManager.rx_session()` New | original src/dst/epoch lookup | missing/epoch mismatch drop |
| 10 | `ReplayWindow.precheck()` New | counter candidate 확인 | duplicate/too-old drop, 상태 불변 |
| 11 | AAD/nonce build New | received prefix/route/header | 구성 오류 drop; 아직 verification reservation 없음 |
| 12 | `RxSecuritySession.reserve_verification()` New | CT length | AES 직전 verification/failure slot을 durable 예약; 한도 소진 시 방향 terminal |
| 13 | `decrypt_detached()` New | 인증 및 body 복호화 | invalid tag/wrong key/AAD는 동일 auth failure; reserved slot 확정 |
| 14 | replay recheck+commit New | bitmap/highest 영속 반영 | 성공 slot release와 replay commit을 한 transaction으로 수행; 실패 시 application 전달 금지 |
| 15 | adapter | prefix 뒤에 body를 붙여 plain wire 복원 | 길이 invariant 확인 |
| 16 | `codec.decode()` Existing | body 구조 재검증/복원 | malformed plaintext drop |
| 17 | Stage5 Existing | 목적지 확인 및 application event | 대상 불일치/drop, sink backpressure 처리 |

## 20. Size and Link Admission

### 20.1 최신 code constants

| Link | Type 92 payload capacity | Type 93 payload capacity | Data fragment capacity |
| ---: | ---: | ---: | ---: |
| 11 | 37B | 38B | 35B |
| 17 | 217B | 218B | 215B |
| 19 | 685B | 686B | 683B |

이 capacity는 Link message payload 기준이며 4B unsigned Segment header도 포함한다. 따라서 Link 11에서 secure application body의 이론상 최대는 Type 92에서 7B, Type 93에서 8B이다.

최신 `Stage5Endpoint._short_data_eligible_on_supported_links()`는 `ack_required=True`이면 Type 92, `False`이면 Type 93 capacity를 사용한다. 여기서 application ACK(MF3) 자체는 scenario code가 `ack_required=False`로 submit하므로 Type 93 후보가 된다.

### 20.2 codec 전체 허용 크기

| Payload | Body | Plain wire | Secure wire | Link Segment | Link 11 Short | 결과 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Position Report MF1 | 14B | 17B | 40B | 44B | 불가 | `ack_required=False`여도 Type 93 38B 초과 → Data Session |
| Route Command MF2, n=0 | 10B | 13B | 36B | 40B | 불가 | `ack_required=True`, Type 92 37B 초과 → Data Session |
| Route Command MF2, n=1 | 20B | 23B | 46B | 50B | 불가 | Data Session |
| Route Command MF2, n=2 | 29B | 32B | 55B | 59B | 불가 | Data Session |
| Route Command MF2, n=3 | 39B | 42B | 65B | 69B | 불가 | Data Session |
| Application ACK MF3 | 3B | 6B | 29B | 33B | 가능 | `ack_required=False` → Type 93 Short |

Route command body는 `ceil((76 + 77n)/8)`, `n=0..16`이다. 모든 command는 최소 Segment 40B이므로 Link 11 Short에 들어가지 않는다.

나머지 waypoint 수의 body/secure/segment 길이는 다음과 같다.

| n | Body | Plain wire | Secure wire | Link Segment |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 48B | 51B | 74B | 78B |
| 5 | 58B | 61B | 84B | 88B |
| 6 | 68B | 71B | 94B | 98B |
| 7 | 77B | 80B | 103B | 107B |
| 8 | 87B | 90B | 113B | 117B |
| 9 | 97B | 100B | 123B | 127B |
| 10 | 106B | 109B | 132B | 136B |
| 11 | 116B | 119B | 142B | 146B |
| 12 | 125B | 128B | 151B | 155B |
| 13 | 135B | 138B | 161B | 165B |
| 14 | 145B | 148B | 171B | 175B |
| 15 | 154B | 157B | 180B | 184B |
| 16 | 164B | 167B | 190B | 194B |

### 20.3 repository 시나리오에서 실제 생성하는 길이

| 실행 경로/메시지 | Waypoints | Body | Plain wire | Secure wire | Link Segment | Link 11 결과 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `apps/scenario_runner.py` SC-VDE-005 command | 2 | 29B | 32B | 55B | 59B | Data Session |
| `apps/scenario_runner.py` SC-VDE-006 command | 2 | 29B | 32B | 55B | 59B | Data Session |
| `apps/stage7_mass_roc_controller.py::run_sc_vde_005()` command | 3 | 39B | 42B | 65B | 69B | Data Session |
| `run_sc_vde_006()` steering command | 2 | 29B | 32B | 55B | 59B | Data Session |
| `run_sc_vde_006()` fallback command | 0 | 10B | 13B | 36B | 40B | Data Session |
| 각 command에 대한 application ACK | - | 3B | 6B | 29B | 33B | Short 가능 |
| Position Report codec(현재 005/006 controller가 생성하지 않음) | - | 14B | 17B | 40B | 44B | Data Session |

따라서 §9.4 길이 제한을 두어도 된다. 다만 시나리오에서 현재 쓰는 `{10,29,39}`만 허용하면 향후 정상 waypoint 수가 거부되므로, codec이 정의한 n=0..16 전체 집합을 허용해야 한다. 이 검사는 전송 가능한 최대 크기를 새로 축소하는 정책이 아니라 기존 body grammar를 암호문 길이에 동일하게 적용하는 것이다.

Link 11 Data fragment capacity는 35B이므로 위 실제 command/position Segment(40, 44, 59, 69B)는 모두 2개 fragment로 분할된다. MF3 ACK Segment 33B는 분할하지 않고 Type 93 Short Data 한 packet으로 간다. 최대 n=16 Segment 194B는 6개 Data fragment가 필요하다. 이 수치는 기존 `LinkTransferManager`의 결과와 통합시험에서 대조하되 security 계층이 직접 fragment 수를 계산하거나 강제하지 않는다.

### 20.4 admission 규칙

1. adapter의 `encoded_size_bound()`는 policy가 REQUIRED이면 기존 codec wire bound에 23B를 더한다.
2. Stage5 Phase A와 Phase C는 모두 secure 최종 길이를 사용한다.
3. Link는 기존대로 `segment_size = 4 + len(presentation.data)`를 계산한다.
4. 지원 Link의 해당 Type 92/93 capacity에 들어가면 Short candidate를 유지한다.
5. addressed 전송이 Short에 들어가지 않으면 기존 Data Session으로 전환한다.
6. broadcast Data Session은 기존 코드상 지원하지 않는다. 현재 REQUIRED SC codecs도 addressed-only이므로 모순이 없다.
7. 최신 main에서는 Stage 7의 `data_session_enabled=false` capability gate가 제거되었으므로 별도 설정으로 암호화 메시지를 차단하지 않는다.
8. pre-auth ciphertext 길이 검증은 §9.4의 codec 전체 허용 집합을 사용하고, 복호화 후 codec 검증은 생략하지 않는다.

## 21. API Specification

아래 `security/*` API는 모두 **새로 제안하는 signature**다. 기존 repository 함수와 혼동하지 않는다.

### 21.1 `security/protocol/header.py`

```python
@dataclass(frozen=True, slots=True)
class SecurityHeader:
    key_epoch: int
    message_counter: int

    @classmethod
    def parse(cls, data: bytes) -> "SecurityHeader": ...
    def to_bytes(self) -> bytes: ...
```

- side effect 없음; thread-safe
- malformed/control/range 오류를 security protocol error로 반환 또는 raise하고 adapter 경계에서 `InternalResult`로 투영

### 21.2 `security/crypto/aes_gcm.py`

```python
def encrypt_detached(*, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]: ...
def decrypt_detached(*, key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes: ...
```

- plaintext는 application body만 받음
- authentication failure 원인을 외부에 세분화하지 않음

### 21.3 `security/presentation.py`

```python
class PresentationSecurity:
    def encoded_overhead(self, entry: PayloadFormatEntry) -> int: ...

    def protect(
        self,
        *,
        entry: PayloadFormatEntry,
        codec: object,
        plain_wire: bytes,
        route: RouteAddresses,
    ) -> InternalResult[bytes]: ...

    def unprotect(
        self,
        *,
        entry: PayloadFormatEntry,
        codec: object,
        secure_wire: bytes,
        route: RouteAddresses,
    ) -> InternalResult[bytes]: ...

    def close_final(self, *, timeout_seconds: float) -> None: ...
```

- `protect()` output은 완전한 secure Presentation wire
- `unprotect()` output은 기존 codec이 받을 완전한 plain Presentation wire
- session mutation은 counter reservation 및 replay commit뿐
- 내부 session은 per-direction lock으로 thread-safe
- `close_final()`이 security admission/in-flight refcount를 소유하고 drain 후 manager/store를 닫는 유일한 공개 종료 API

### 21.4 `security/session/manager.py`

```python
class SecuritySessionManager:
    def tx_session(self, *, source_id: StationId, destination_id: StationId) -> TxSecuritySession: ...
    def rx_session(self, *, source_id: StationId, destination_id: StationId, key_epoch: int) -> RxSecuritySession: ...
    def close(self) -> None: ...
```

- manager의 `close()`는 `PresentationSecurity.close_final()`이 in-flight 0을 확인한 뒤에만 호출한다. 직접 lifecycle API로 노출하지 않는다.
- `Stage5Endpoint.stop()` 또는 Stage 7 lane restart는 어느 close도 호출하지 않는다. process의 최종 launcher/harness shutdown만 `PresentationSecurity.close_final()`을 호출한다.

### 21.5 `security/session/provider.py`와 `state_store.py`

```python
class SecurityMaterialProvider(Protocol):
    def load_for_endpoint(
        self, *, endpoint_id: str, local_station_id: StationId
    ) -> tuple[DirectionalSessionMaterial, ...]: ...

class SecurityStateStore(Protocol):
    def reserve_tx_invocation(
        self, *, material_id: bytes, body_length: int
    ) -> int: ...

    def reserve_rx_verification(
        self, *, material_id: bytes, ciphertext_length: int
    ) -> RxVerificationReservation: ...

    def commit_rx_authenticated(
        self, *, reservation: RxVerificationReservation,
        highest_counter: int, replay_bitmap: bytes
    ) -> None: ...

    def commit_rx_authentication_failure(
        self, *, reservation: RxVerificationReservation
    ) -> None: ...

    def close(self) -> None: ...
```

- file provider는 startup에서만 읽으며 호출 후 material tuple은 immutable이다.
- state store API는 transaction과 terminal-state 전이를 내부에서 수행한다. caller가 SQL을 직접 다루지 않는다.
- 동일 endpoint DB는 한 process만 소유한다. thread-safe하되 multi-process 동시 writer는 lock으로 startup에서 거부한다.

### 21.6 `security/dependencies.py`

```python
@dataclass(frozen=True, slots=True)
class E2eSecurityDependencies:
    mode: E2eSecurityMode
    presentation_security: PresentationSecurity

    @classmethod
    def disabled(cls) -> "E2eSecurityDependencies": ...
```

`ENFORCED`는 non-null `presentation_security`를 요구한다. `DISABLED` factory만 내부 no-op collaborator와 `LEGACY_ALL_DISABLED` 정책을 만들 수 있다. 직접 `None`을 전달하는 API는 없다.

### 21.7 기존 함수 변경 signature

```python
# presentation/adapter.py
def build_presentation_adapter(
    registry: PayloadFormatRegistry,
    *,
    e2e_security: E2eSecurityDependencies,
) -> InternalResult[RegistryPresentationAdapter]: ...

# runtime/wiring.py
def build_stage5_endpoint(..., *, crypto_dependencies=None,
                          e2e_security: E2eSecurityDependencies, ...): ...
def build_stage5_group(..., *, crypto_by_station=None,
                       e2e_security_by_station: Mapping[int, E2eSecurityDependencies], ...): ...
def build_wideband_endpoint(..., *, crypto_dependencies=None,
                            e2e_security: E2eSecurityDependencies, ...): ...
def build_wideband_group(..., *, crypto_by_station=None,
                         e2e_security_by_station: Mapping[int, E2eSecurityDependencies], ...): ...

# stage7/wiring.py
def build_stage7_b210_endpoint(
    resolved,
    deployment,
    *,
    crypto_dependencies=None,   # 기존 TBB signer/trust store
    e2e_security: E2eSecurityDependencies, # 새 AES-GCM dependency, 필수
    ...,
): ...
```

`RegistryPresentationAdapter`를 wrapper subclass로 교체하지 않는다. `Stage5Endpoint._validate_effective_graph()`와 `Stage7Endpoint._validate_effective_graph()`가 concrete `RegistryPresentationAdapter` type을 확인하므로 동일 클래스에 명시적 collaborator를 주입한다. 모든 기존 caller와 fixture는 `.disabled()` 또는 station별 enforced dependency를 반드시 전달하도록 수정한다.

### 21.8 `security/errors.py`

```python
def presentation_security_error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    correlation: Correlation = EMPTY_CORRELATION,
    details: Details = EMPTY_DETAILS,
) -> Err[InternalProtocolError]: ...
```

이 helper는 기존 `common.results.internal_error()`를 `Layer.PRESENTATION`으로만 호출한다. retryable은 기본 `False`이며, 호출자가 arbitrary detail mapping이나 raw exception `str()`을 넘기지 못하게 이미 검증된 `Details`만 받는다. §24.1 목록 밖 code는 unit test에서 거부한다.

## 22. State Management

| 상태 | 소유자 | 수명 | 동시성 |
| --- | --- | --- | --- |
| key/NP/epoch | `DirectionalSessionMaterial` | deployment security lifetime | immutable, `repr=False` |
| next TX counter/budgets | `SecurityStateStore` + `TxSecuritySession` | deployment security lifetime | SQLite transaction + per-session lock |
| RX highest/bitmap | `RxSecuritySession`/store | key/epoch보다 짧지 않음 | per-session lock |
| policy | `PayloadSecurityPolicy` | process | immutable |
| secure Presentation bytes | 기존 `_pending_presentation` | transfer | 기존 service lock |
| Link fragments | 기존 Link/Data Session | transfer/session | 기존 구현 |

Runtime stop/start에서 Presentation Adapter를 유지하는 최신 구조와 맞춰 manager도 유지한다. process 자체가 재시작되면 영속 store에서 복원한다.

동시성 lock 순서는 `PresentationSecurity` admission/refcount lock → directional session lock → `SecurityStateStore` lock/SQLite transaction 순으로 고정한다. 역순 획득을 금지한다. AES-GCM 계산 자체는 session/store lock 밖에서 수행하되 in-flight refcount와 RX reservation을 보유한다. `close_final()`은 새 admission을 닫은 뒤 refcount가 0이 될 때까지 기다리므로 session/store lock을 잡은 채 대기하지 않는다.

### 22.1 lifecycle과 최종 종료 순서

| 사건 | Service/Link 상태 | Security manager/state |
| --- | --- | --- |
| `Service.stop()` | 실행별 transfer/queue/Link runtime reset | 유지; close/reset 금지 |
| `Service.start()` 재호출 | 기존 adapter 재사용 | 같은 manager와 durable counter/replay 계속 사용 |
| Stage 7 lane/radio restart | lane/radio만 재조립 | 유지 |
| launcher process 정상 종료 | 새 submit/RX admission 종료 | 아래 순서로 `close_final()` |
| crash/power loss | 메모리 상태 소실 | durable high-water/replay에서 복구; 예약 gap 허용 |

정상 최종 종료 순서는 다음으로 고정한다.

1. launcher가 새 application submit 및 RX delivery admission을 닫는다.
2. `endpoint.stop()`을 호출해 기존 Service 실행을 종료한다.
3. `PresentationSecurity.close_final(timeout_seconds=5.0)`이 새 protect/unprotect를 거부하고 in-flight 호출 수가 0이 될 때까지 기다린다.
4. timeout이면 비밀을 출력하지 않고 high-severity 오류를 남긴 뒤 process를 종료한다. 예약된 counter/verification은 소모된 것으로 남는다.
5. 정상 drain이면 SQLite checkpoint/connection과 endpoint lock을 닫고 key/NP 참조를 해제한다.

`Stage5Endpoint.stop()`은 최신 코드상 lock 밖 Phase B codec 종료를 기다리지 않으므로, 그 메서드 내부에서 manager를 닫으면 race가 난다. 따라서 security 최종 close는 `VDETER/apps/stage7_endpoint_launcher.py`와 software harness의 **process/harness finalizer**가 담당한다. `close_final()`은 idempotent지만 호출 뒤 security 객체 재사용은 금지한다.

## 23. Counter Persistence

### 23.1 확정 구현과 저장 위치

`SecurityStateStore` protocol과 SQLite 구현을 사용한다. 저장 위치는 repository 밖의 endpoint별 runtime state directory이며 key/NP 원문은 저장하지 않는다.

- Stage 7 Linux/Raspberry Pi: `/var/lib/vdeter/security/<endpoint-id>/state.sqlite3`
- parent directory mode: `0700`, DB/`-wal`/`-shm`/lock file은 owner-only(`0600`)로 생성
- owner: 해당 endpoint launcher service account
- software/unit test: pytest가 만든 독립 temporary directory; test 간 DB 공유 금지
- launcher는 DB를 자동 생성하지 않는다. out-of-band provisioning 단계가 material과 빈 schema를 함께 생성하며, 실행 중 DB가 없으면 fail closed한다.

SQLite 설정은 connection마다 `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, `PRAGMA busy_timeout=0`으로 고정한다. process `umask 077`에서 DB를 열고, Python connection은 `check_same_thread=False`로 만들되 store 내부 `RLock` 하나로 모든 transaction을 직렬화한다. sibling `state.lock`의 OS advisory exclusive lock(`fcntl` on Linux, test용 Windows equivalent)을 먼저 얻어 동일 endpoint state를 두 process가 동시에 사용하지 못하게 한다. lock 실패는 재시도 대기가 아니라 startup 실패다.

권장 row key:

```text
material_id = SHA-256("VDS2-state" || source_id_be32 || destination_id_be32
                     || epoch_be16 || SHA-256(aes_key || nonce_prefix))
```

전체 `material_id`는 DB primary key에만 사용하고 log/공개 오류에는 내보내지 않는다. DB schema version 1은 다음 논리 schema를 갖는다(실제 SQL type은 SQLite `INTEGER`, `BLOB`, `TEXT`와 CHECK/FK constraint 사용).

| Table | 필수 column | 규칙 |
| --- | --- | --- |
| `metadata` | `schema_version=1`, `endpoint_id`, `local_station_id`, `state_id(16B)` | 정확히 1 row; material file의 binding과 일치 |
| `tx_state` | `material_id`, source/destination, `epoch=1`, `reserved_until`, `gcm_blocks_reserved`, `status` | material별 1 row; status `ACTIVE/EXHAUSTED/INHIBITED` |
| `rx_state` | `material_id`, source/destination, `epoch=1`, `highest_counter`, `replay_bitmap(8B)`, `verification_blocks_used`, `failed_authentications`, `status` | bitmap은 unsigned 64-bit big-endian BLOB으로 저장 |
| `rx_reservation` | random `reservation_id(16B)`, `material_id`, `blocks` | verify 직전 생성; crash 시 unresolved attempt 추적 |

모든 row의 source/destination/epoch가 provisioned material과 일치해야 한다. unknown row, duplicate logical direction, schema downgrade, invalid bitmap/범위 또는 metadata mismatch는 DB를 자동 수리하지 않고 `INHIBITED` startup 실패로 처리한다.

### 23.2 atomic block reservation

기본 block size는 1024다.

1. SQLite `BEGIN IMMEDIATE`
2. 현재 durable `reserved_until` 확인
3. `new_reserved_until = min(old + 1024, 2^24)`를 계산하고 남은 counter가 없으면 status를 `EXHAUSTED`로 commit
4. REQUIRED codec의 `max_application_bytes`로 build 시 계산한 `MAX_AUTH_BLOCKS_PER_REQUIRED_MESSAGE=14`를 사용해 `gcm_blocks_reserved += (new-old)×14`가 `2^32` 이하인지 확인
5. row update 및 commit
6. process는 `(old+1)..new_reserved_until`을 메모리에서 사용

crash 시 미사용 counter는 건너뛰지만 재사용되지 않는다. block 전체에 message당 최대 14 GCM blocks를 미리 charge하므로 crash 후 TX block 사용량도 과소 계상되지 않는다. `2^24×14 < 2^32`이므로 이 보수적 charge가 nonce 상한보다 먼저 정상 송신을 막지 않는다.

새 REQUIRED codec 또는 더 긴 body allowlist를 추가할 때는 build-time policy 검증이 이 최대값을 다시 계산하고 상수와 다르면 startup/test를 실패시킨다. 최대 block 수를 갱신하지 않은 채 payload만 확장할 수 없다.

### 23.3 RX persistence

1. replay precheck 및 길이 검증 후 `BEGIN IMMEDIATE`에서 `verification_blocks_used`를 증가시키고 `rx_reservation` row를 생성한다. `failed_authentications + unresolved reservations < 2^20`과 block budget을 먼저 검사한다.
2. AES-GCM 인증 실패면 같은 reservation을 삭제하고 `failed_authentications`를 증가시키는 transaction을 commit한다.
3. 인증 성공이면 per-session lock에서 replay를 다시 검사한 뒤, reservation 삭제와 `highest_counter/replay_bitmap` 갱신을 **같은 transaction**으로 commit한다.
4. 두 thread가 같은 counter를 동시에 인증해 commit 경쟁에서 한쪽이 duplicate가 되면, 패배한 reservation만 삭제한다. verification blocks는 이미 소비된 상태로 두되 authentication failure는 증가시키지 않는다.
5. process가 verify 도중 crash하면 startup recovery가 남은 reservation 각각을 실패 1회로 보수적으로 전환한다. verification block은 이미 소비된 상태를 유지한다.
6. 예상하지 못한 AES/provider/store 예외로 reservation 결과를 확정할 수 없으면 해당 RX 방향을 `INHIBITED`로 durable 표시한다.
7. replay commit 성공 뒤에만 codec/application 전달을 수행한다. 그렇지 않으면 재시작 후 같은 packet을 다시 수락할 수 있다.

### 23.4 오류 처리

- DB open/write/fsync/corruption 실패: 해당 security session 송수신 fail closed
- 자동 DB 삭제 또는 counter 1 초기화 금지
- test는 임시 DB 또는 in-memory store를 사용
- 이 epoch-1-only profile은 runtime state reset/material 교체 API를 제공하지 않음
- 실험용 memory store는 매 process 시작마다 양측에 완전히 새로운 key/NP를 제공하는 경우에만 허용
- 정상 crash/power loss는 WAL/FULL transaction과 남은 reservation recovery로 처리하며 동일 DB를 계속 사용함
- 알려진 DB backup 복원·state DB 분실·재생성·복제 뒤 동일 key/NP 사용 금지. launcher는 자동 복구하지 않고 `PRESENTATION_SECURITY_STATE_UNAVAILABLE`로 시작을 거부함
- 복구는 새 key, 새 NP, 새 random `state_id`, 새 `provisioning_id`, 새 DB를 하나의 새 배포로 설치하는 방식뿐임. epoch는 새 배포에서도 `1`이며, 새 material 생성·양측 전달 자체는 범위 밖임
- provisioning 도구에는 `--reset`, `--force`, DB 덮어쓰기, 기존 material을 사용한 state 재생성 기능을 두지 않음. 이미 endpoint security directory가 존재하거나 일부 파일만 남아 있으면 수동 정리 지침만 출력하고 실패함
- 권한 있는 공격자가 material과 DB를 함께 과거 snapshot으로 되돌리는 행위는 이 모사 profile의 위협 모델 밖이다. 따라서 이를 탐지하기 위한 TPM monotonic counter나 원격 ledger는 구현하지 않으며, 이 제한을 보안 보장으로 오해해서는 안 됨

## 24. Error Handling

### 24.1 기존 error framework 통합

새 `Layer.SECURITY` enum은 만들지 않는다. 보안 처리는 Presentation 내부 collaborator이므로 모든 아래 오류는 기존 `InternalProtocolError(layer=Layer.PRESENTATION, ...)`로 생성한다. `security/errors.py`는 이 객체를 만드는 helper일 뿐 별도 exception hierarchy/public DTO를 만들지 않는다.

`VDETER/src/vdeter/common/errors.py::ErrorCode`에 다음 안정된 내부 code를 추가한다.

```text
PRESENTATION_SECURITY_PROFILE_UNSUPPORTED
PRESENTATION_SECURITY_EPOCH_MISMATCH
PRESENTATION_SECURITY_COUNTER_INVALID
PRESENTATION_SECURITY_CIPHERTEXT_LENGTH_INVALID
PRESENTATION_SECURITY_SESSION_UNAVAILABLE
PRESENTATION_SECURITY_AUTHENTICATION_FAILED
PRESENTATION_SECURITY_REPLAY_DUPLICATE
PRESENTATION_SECURITY_REPLAY_TOO_OLD
PRESENTATION_SECURITY_USAGE_EXHAUSTED
PRESENTATION_SECURITY_STATE_UNAVAILABLE
PRESENTATION_SECURITY_POLICY_VIOLATION
```

`VDETER/src/vdeter/runtime/projection.py::_CODE_MAP`의 mapping은 다음으로 고정한다.

| 내부 code | Public code/category |
| --- | --- |
| profile/epoch/counter/ciphertext-length/authentication/replay/policy | `TRANSFER_FAILED / PROTOCOL` |
| session unavailable/usage exhausted/state unavailable | `RESOURCE_UNAVAILABLE / RESOURCE` |
| 기존 `PRESENTATION_TRUNCATED`, `PRESENTATION_STRUCTURE_INVALID` | 기존 mapping 유지 |

startup에서 mode/material/policy/DB를 검증하다 실패한 경우 endpoint graph가 만들어지기 전이므로 기존 `CONFIG_SEMANTIC_INVALID`를 사용한다. RX에서는 외부 peer에 세부 원인을 반사하지 않고 packet을 drop하며 내부 event/metric만 남긴다. TX `submit()`은 위 projection의 일반화된 공개 오류만 반환한다. wrong key, tag, nonce, AAD 중 무엇이 원인인지 공개 message로 구분하지 않는다.

| Error | Detection Layer | Result | Drop | Log | Session Impact |
| --- | --- | --- | --- | --- | --- |
| malformed/truncated header/PDU | Presentation PDU parser | 기존 structure/truncated | Yes | count/reason | 없음 |
| unsupported Security Control | Presentation security protocol | profile unsupported | Yes | control 값 가능 | 없음 |
| epoch != 1/mismatch | Presentation session manager | epoch mismatch | Yes | source/dest/epoch | 없음 |
| counter 0 또는 `>2^24` | Presentation header/replay | counter invalid | Yes | 범주만 | 없음 |
| ciphertext length 불일치 | Presentation PDU/policy | length invalid | Yes | format/length | AES/상태 접근 없음 |
| counter/TX budget exhausted | Presentation TX session | usage exhausted | 송신 거부 | high severity | TX `EXHAUSTED` terminal |
| RX verify/failure budget exhausted | Presentation RX session | usage exhausted | Yes | high severity | RX `EXHAUSTED` terminal |
| replay duplicate | Presentation replay | duplicate | Yes | metric | replay/verify budget 불변 |
| replay too old | Presentation replay | too old | Yes | metric | replay/verify budget 불변 |
| invalid tag/wrong key/AAD | Presentation AES wrapper | authentication failed | Yes | 단일 metric | replay 불변; verify/failure budget 증가 |
| missing session | Presentation manager | session unavailable | Yes/송신 거부 | peer/epoch만 | 없음 |
| unknown payload format | registry | unsupported request/wire key | Yes | 기존 처리 | 없음 |
| REQUIRED plaintext | Presentation Security PDU parser | policy/structure invalid | Yes | downgrade metric | 없음 |
| DISABLED secure-looking data | policy+codec | 기존 plaintext codec 규칙 | codec 결과에 따름 | 기존 처리 | 없음 |
| secure payload too large | Service/Link | unsupported/resource | 송신 거부 | 실제 size | counter는 encode 후라면 소비됨 |
| persistence/lock failure | Presentation state store | state unavailable | Yes | 비밀 없이 high severity | 방향 `INHIBITED` terminal |
| mode/policy/material startup 불일치 | wiring/bootstrap | 기존 config semantic invalid | endpoint 시작 안 함 | 비밀 없이 원인 범주 | graph 미생성 |
| decrypted body codec invalid | codec | structure invalid | Yes | 기존 처리 | replay는 이미 commit되어 재시도 불가 |

마지막 행은 의도적이다. 인증된 동일 packet이 의미 검증 실패 후 반복 처리되지 않도록 replay state를 먼저 commit한다.

## 25. Logging and Metrics

### 25.1 기록 가능

- payload format 이름
- source/destination station ID
- epoch와 counter 값
- wire/ciphertext 길이
- policy 결과
- 오류 범주, auth failure/replay/epoch mismatch 횟수

### 25.2 기록 금지

- AES key 원문
- Nonce Prefix 원문
- 완전한 nonce
- plaintext body
- decrypted body
- 전체 ciphertext/tag dump
- 영속 store의 secret material
- key/NP fingerprint 또는 `material_id` 전체/일부

### 25.3 최소 metrics

- `security_encrypt_total`
- `security_decrypt_success_total`
- `security_auth_failure_total`
- `security_replay_duplicate_total`
- `security_replay_too_old_total`
- `security_epoch_mismatch_total`
- `security_malformed_pdu_total`
- `security_counter_exhausted_total`
- `security_usage_exhausted_total{direction, budget}`
- `security_state_store_failure_total`
- `security_mode_start_total{mode}`

현재 project에 범용 metrics infrastructure가 없으면 lock-safe in-memory counters와 기존 event/error 기록만 추가한다.

## 26. File-by-File Changes

### 26.1 New Files

| File | 목적 | 주요 API | Dependencies | 핵심 tests |
| --- | --- | --- | --- | --- |
| `VDETER/src/vdeter/security/constants.py` | V2 single source of truth | 상수, size assertions | 없음 | 모든 합계/한도 |
| `.../security/dependencies.py` | Runtime 주입 객체/명시적 mode | `E2eSecurityMode`, `E2eSecurityDependencies` | presentation security | mode/type validation |
| `.../security/errors.py` | 오류 helper | security error factory | common errors/results | projection |
| `.../security/policy.py` | format 정책 | `SecurityPolicy`, `PayloadSecurityPolicy` | PayloadFormat | table/start validation |
| `.../security/metrics.py` | 경량 계측 | sink protocol/counters | threading | no-secret behavior |
| `.../security/presentation.py` | 통합 orchestrator | `protect`, `unprotect`, `encoded_overhead` | protocol/crypto/session | TX/RX integration |
| `.../security/crypto/aes_gcm.py` | AES-GCM wrapper | detached encrypt/decrypt | `cryptography` | KAT, mutation |
| `.../security/protocol/header.py` | 7B header | parse/serialize | constants | round trip/invalid |
| `.../security/protocol/nonce.py` | 12B nonce | `build_nonce` | constants | length/order/bounds |
| `.../security/protocol/aad.py` | 22B AAD | `build_aad` | route/header | byte equality/mutation |
| `.../security/protocol/pdu.py` | PDU 조립/parse | build/parse | header | truncation/layout |
| `.../security/session/material.py` | immutable material | dataclass validation | StationId | directional mapping |
| `.../security/session/provider.py` | 외부 material 적재 | provider protocol, protected-file/in-memory 구현 | pathlib/base64 | permission/schema/direction |
| `.../security/session/state_store.py` | crash-safe TX/RX 상태 | protocol, SQLite/memory | sqlite3/OS lock | crash/block/corruption/ownership |
| `.../security/session/replay.py` | sliding window | precheck/commit/snapshot | threading | duplicate/old/order |
| `.../security/session/manager.py` | session registry | tx/rx lookup | material/store/replay | missing/epoch/direction |
| `VDETER/tools/e2e_security_provision.py` | out-of-band material bundle 검증/최초 빈 DB 생성 | `validate-bundle`, `initialize-state` | provider/state_store | reciprocal pair/permission/비어 있는 directory만 허용/reset·force·overwrite 거부 |

### 26.2 Modified Files

#### `VDETER/src/vdeter/presentation/adapter.py`

- 현재 역할: registry lookup, codec encode/decode, prefix dispatch, encoded bound
- 변경:
  - `RegistryPresentationAdapter.__slots__`에 명시적 `_e2e_security` 추가
  - `build_presentation_adapter(..., e2e_security=...)` 필수 인자 추가; `None` 거부
  - build 시 REQUIRED codec의 exact 3B prefix 조건 검증
  - `encoded_size_bound()`에서 REQUIRED이면 plain bound+23 반환
  - `encode_payload()`에서 codec plain wire 생성 후 `security.protect()` 호출
  - `decode_payload()`에서 dispatch 후 REQUIRED이면 `security.unprotect()`한 plain wire를 codec에 전달
- 유지: registry/codec lookup 우선순위, route/ACK 정책, fingerprint, `PresentationData` DTO
- tests: 기존 adapter regression + REQUIRED/DISABLED 양방향

#### `VDETER/src/vdeter/common/errors.py`

- 현재 역할: `Layer`, `ErrorCode`, `InternalProtocolError`, 안전한 `Details` 정의
- 변경: §24.1의 `PRESENTATION_SECURITY_*` code 11개 추가; 새 layer는 추가하지 않음
- 유지: `Details` 금지 key/크기, Correlation, 공개 DTO shape
- tests: 모든 security 오류가 `Layer.PRESENTATION`이고 secret-bearing details가 거부됨

#### `VDETER/src/vdeter/runtime/projection.py`

- 현재 역할: 내부 `ErrorCode`를 안정된 공개 Service 오류로 projection
- 변경: §24.1 mapping 추가; auth/key/AAD 원인을 모두 동일 공개 `TRANSFER_FAILED/PROTOCOL`로 일반화
- 유지: 미등록 code의 `INTERNAL_ERROR` fail-safe와 detail/correlation 제거
- tests: 각 code mapping, 공개 message/detail에 key/NP/tag/plaintext 미노출

#### `VDETER/src/vdeter/runtime/wiring.py`

- 현재 역할: Stage5/Wideband endpoint와 adapter 조립
- 변경: 필수 `e2e_security` 및 group용 `e2e_security_by_station` 별도 인자 전달; station set exact 일치 및 mode 혼합 거부
- 유지: 기존 `crypto_dependencies`는 TBB 전용, Link/MAC/PHY 조립 그대로
- tests: station별 올바른 directional manager 주입, TBB dependency 비회귀

#### `VDETER/src/vdeter/stage7/wiring.py`

- 현재 역할: `build_stage7_b210_endpoint()`에서 실제 Stage7 endpoint 조립
- 변경: `e2e_security` 인자를 받아 line 341 부근의 `build_presentation_adapter()`에 전달
- 유지: TBB preflight, combined registry, lane graph, radio/PHY 조립
- tests: Stage7 graph type validation 통과 및 security collaborator 보존

#### `VDETER/src/vdeter/runtime/stage5.py`

- 기능 변경은 원칙적으로 불필요하다.
- `_reset_stage5_runtime_locked()`의 “Presentation Adapter는 무상태” 설명은 더 이상 정확하지 않으므로 stateful collaborator 유지 계약으로 문서/주석을 수정한다.
- 기존 Phase A/B/C와 `_pending_presentation` 재사용은 nonce 안전성에 필수이므로 변경하지 않는다.
- tests: stop/start 후 counter가 초기화되지 않고 Link retransmission이 새 counter를 소비하지 않음

#### `VDETER/apps/stage7_endpoint_launcher.py`

- 현재 역할: resolved config/TBB trust를 적재하고 `build_stage7_b210_endpoint()` 호출
- 변경: coordinator가 명시한 `--e2e-security-mode`를 필수 parse하고, `ENFORCED`이면 고정 endpoint 경로의 material/state를 읽어 `E2eSecurityDependencies`를 구성해 별도 인자로 전달; finalizer에서 `close_final()` 호출
- 금지: key/NP를 CLI, 로그, repository TOML에 평문으로 노출
- tests: 누락 material fail closed, endpoint별 방향 매핑

#### `VDETER/apps/stage7_coordinator_main.py`

- 현재 역할: endpoint launcher 3개 spawn, Stage 7 lifecycle/workload orchestration
- 변경: `--e2e-security-mode {ENFORCED,DISABLED}`를 required로 받고 `spawn_and_connect()`가 세 launcher 모두에 exact 전달
- 유지: endpoint별 config/data profile/gain/test-plan 전달과 lifecycle barrier
- tests: 누락/혼합 mode 거부, 세 launcher argv 동일성

#### `apps/stage7_run.py` 및 Stage 7 실행 caller

- `apps/stage7_run.py`의 `mass-roc` command는 `--e2e-security-mode ENFORCED`를 명시한다.
- mode mapping은 다음으로 고정한다: `apps/stage7_service_layer.py`의 `mass-roc-service`, `apps/stage7_session.py`의 `mass-roc-session`, `apps/standalone/local_session.py`의 실제 MASS/ROC service 및 `apps/stage7_run.py --workload mass-roc`는 `ENFORCED`; 기존 RF ladder만 수행하는 `apps/standalone/qualify_cmd.py::default_phase_runner()`는 `DISABLED`; 새 secure qualification phase는 `ENFORCED`다.
- 직접 `spawn_and_connect()`를 부르는 `VDETER/tools/stage7_service_debug.py`는 `--e2e-security-mode`를 required로 받아 그대로 전달한다. `--with-scenario`와 함께 `DISABLED`를 주면 startup 전에 거부한다.
- 실제 암호 통합 검증은 반드시 `ENFORCED`; `DISABLED` 실행 결과를 암호 기능 통과 증거로 사용하지 않는다.

#### `VDETER/tests/support/stage5.py`와 `apps/mass_roc_software.py`

- 현재 역할: software radio 기반 MASS-A/MASS-B/ROC group 조립 및 공개 Service API 시나리오 실행
- 변경: test-only pairwise material/state fixture를 만들고 `e2e_security_by_station`에 세 endpoint dependency를 전달; harness는 `ENFORCED`를 명시하고 finalizer에서 manager를 닫음
- tests: ROC의 peer별 4방향 mapping, MASS별 2방향 mapping, SC-VDE-005/006 실제 body 암복호화
- 분류: AES primitive 변경은 아니지만, 암호 모듈을 실제 software 시나리오 호출 경로에 연결하는 **필수 통합 작업**이다.

#### 기존 builder test/support caller

- `VDETER/tests/support/wideband.py`, `VDETER/tests/support/stage7.py`는 목적에 맞는 명시적 dependency를 전달한다.
- `build_presentation_adapter()`를 직접 호출하는 `VDETER/tests/contract/test_payload_extension_registry.py`, `VDETER/tests/unit/test_payload_sc_vde_position_report.py`, `test_stage5_codec_opaque_chart.py`, `test_stage5_registry_dispatch.py`는 `E2eSecurityDependencies.disabled()`를 명시한다. security 전용 case만 `ENFORCED` fixture를 사용한다.
- `build_stage5_group()`, `build_wideband_group()`, `build_stage7_b210_endpoint()`를 직접 호출하는 기존 lifecycle/group/wiring test도 동일하게 explicit dependency를 추가한다.
- 이유: builder signature를 필수 인자로 바꾸므로 이 caller 갱신이 빠지면 test collection/runtime 조립 단계에서 `TypeError`가 발생한다.

#### `apps/standalone/qualification_domains.py`

- 현재 역할: installation file을 RF/operation/tooling/non-binding 영역에 결속
- 변경: 새 `VDETER/tools/e2e_security_provision.py`를 tooling으로 명시 분류한다. `VDETER/src/vdeter/security/**`는 기존 prefix 규칙에 의해 자동 RF 분류되므로 예외를 만들지 않는다.
- tests: 새 source의 RF 분류, provisioning tool의 tooling 분류, 이전 reference reuse 거부

#### dependency/install 및 qualification artifacts

- `VDETER/pyproject.toml`에는 이미 optional dependency `cryptography>=50.0,<51`가 있다.
- 코드 범위상 새 library 선언은 불필요하나 실제 Stage7 설치/standalone lock이 crypto extra를 포함하는지 배포 시험에서 확인해야 한다.
- `apps/standalone/qualification_domains.py`는 모든 `VDETER/src/vdeter/**`와 `VDETER/apps/stage7_endpoint_launcher.py`를 RF binding으로 분류한다. 따라서 security source/wiring/launcher 변경 후 기존 qualification reference 재사용은 불가하다.
- 구현 완료 뒤 installation manifest/lock을 재생성하고 RF qualification과 secure E2E qualification을 다시 실행하여 새 qualification reference 및 frozen hash artifacts를 발급한다. 기존 증거 파일을 손으로 수정하지 않는다.

### 26.3 Unchanged Files

| File/group | 변경하지 않는 이유 |
| --- | --- |
| `common/contracts/service.py` | 기존 destination/payload_format/payload/ack 정보로 충분 |
| `common/contracts/presentation.py` | security는 adapter 내부이며 DTO shape 변경 불필요 |
| `profiles/payload_formats.py` | 기존 VPFI/MF/codec source of truth 유지; policy 별도 관리 |
| `presentation/codecs/*` | codec은 plain body와 canonical prefix 검증 역할 유지 |
| `link/transfer.py` | final Presentation bytes 기준 길이/Short/Data Session 분석 이미 수행 |
| `link/segment/codec.py` | 4B unsigned Segment 경계가 Ciphertext Length 필드를 대체 |
| `link/data_session/fragmentation.py` | 완성 secure Segment를 기존 방식으로 분할 |
| `link/data_session/reassembly.py` | 완성 Segment 복원 후 Presentation으로 전달 |
| MAC/PHY 전체 | ciphertext를 opaque Presentation bytes로 처리 가능 |
| `stage7/endpoint.py` | 최신 main에서 Data Session 차단 override가 이미 제거됨 |
| `stage7/configuration.py` | mode는 coordinator의 명시적 실행 인자, material/state는 고정 외부 경로에서 적재하므로 schema 변경 불필요 |
| `config/models.py`의 `SecurityConfig` | TBB private key/certificate/trust store 의미를 보존하고 E2E로 오용하지 않음 |

## 27. Configuration

현재 단계에서는 E2E key를 기존 TOML schema에 직접 추가하지 않는다. coordinator/launcher가 명시적 mode를 전달하고, `ENFORCED` launcher는 endpoint ID로 결정되는 고정 외부 경로에서 material/state를 읽어 `E2eSecurityDependencies`를 만든다.

### 27.1 material file schema

각 endpoint의 `/var/lib/vdeter/security/<endpoint-id>/materials.json`은 다음 exact field만 허용한다.

```json
{
  "schema_version": 1,
  "endpoint_id": "MASS-A",
  "local_station_id": 100,
  "state_id_b64": "<base64로 인코딩한 random 16B>",
  "generation_method": "CSPRNG-256",
  "provisioning_id": "<비밀이 아닌 배포 추적 ID>",
  "directions": [
    {
      "source_id": 100,
      "destination_id": 200,
      "epoch": 1,
      "key_b64": "<base64로 인코딩한 32B AES key>",
      "nonce_prefix_b64": "<base64로 인코딩한 8B NP>"
    }
  ]
}
```

위 ID/값은 schema 설명용 dummy다. `generation_method` 허용값은 배포 profile에서 승인한 `CSPRNG-256` 또는 승인 KDF profile ID로 제한한다. `directions`에는 local station이 source인 TX 또는 destination인 RX만 올 수 있다. MASS endpoint는 해당 ROC peer에 대한 TX/RX 2개, ROC는 MASS-A/B 각각에 대한 TX/RX 4개를 가져야 한다. 순서는 의미가 없으며 duplicate logical direction은 거부한다.

launcher는 file/DB를 생성하지 않고 read/open만 한다. `VDETER/tools/e2e_security_provision.py`가 이미 공유된 material bundle의 reciprocal pair와 peer별 고유성, owner/mode를 검증한 뒤 `state_id`가 결속된 빈 DB를 **처음 설치하는 비어 있는 endpoint security directory에만** 만든다. 기존/부분 설치 directory, material file 또는 DB가 하나라도 있으면 실패하며 덮어쓰기·초기화·`--reset`·`--force` 옵션은 제공하지 않는다. 알려진 DB 복원·분실·손상 후에는 이전 bundle을 다시 넣지 않고 새 key/NP/`state_id`/`provisioning_id` bundle로 새 directory를 provision한다. 이 도구는 key 생성·교환을 하지 않으며 성공/실패와 endpoint/direction 수만 출력한다.

material/state directory는 installation manifest, qualification evidence, support bundle, 일반 backup 및 log 수집 대상에서 명시적으로 제외한다. 권한 제한 JSON은 현재 모사 통합 profile에서 채택한 key-at-rest 구현이다. Python `bytes`의 완전한 zeroization이나 disk theft/root compromise 방어는 제공하지 않으며, TPM/HSM/OS keystore provider는 이 profile에서 구현하거나 승인 조건으로 요구하지 않는다.

### 27.2 runtime configuration values

```text
local station ID
peer station ID
TX: key, nonce prefix, epoch=1
RX: key, nonce prefix, epoch=1
execution mode = ENFORCED | DISABLED (필수, default 없음)
material file state_id = 16B
generation method/provisioning ID
state store path
policy profile id = VDS2_SC_VDE_REQUIRED_V1
replay window = 64
TX nonce limit = 2^24
TX/RX GCM block limit = 2^32
RX authentication failure limit = 2^20
```

InvalidTag rate limiter 관련 configuration key는 정의하지 않는다. 현재 profile에서 `2^20`은 lifetime failure budget이지 초당/분당 traffic rate 제한이 아니다.

시작 시 검증:

- key 32B, NP 8B
- mode가 명시된 두 값 중 하나이며 `ENFORCED`/`DISABLED` policy와 모순되지 않음
- TX/RX source/destination 방향 반대 매핑과 local station ID 일치
- pairwise logical direction마다 reciprocal TX/RX 한 쌍만 같은 material을 사용하고 다른 direction/peer와 key 또는 `(key, NP)`를 재사용하지 않음
- generation method가 승인된 CSPRNG/KDF profile이며 provisioning ID가 비어 있지 않음
- epoch exact 1
- policy의 모든 payload format이 active registry에 존재하거나 해당 scope에서 명시적으로 제외됨
- REQUIRED entry가 exact 3B prefix 조건 충족
- material file owner/mode/type/schema/state_id 검증 성공
- state store open/lock/schema/metadata/material row/transaction 성공
- `cryptography` import와 repository가 요구하는 major version 50 확인; `ENFORCED`에서 unavailable/version mismatch면 startup 실패

`DISABLED`도 암묵적 default가 아니며 caller가 `E2eSecurityDependencies.disabled()`를 명시해야 한다. `ENFORCED` 시작 실패를 `DISABLED`로 재시도하는 fallback은 금지한다.

## 28. Test Specification

### 28.1 Unit tests

| Test name | Target | Fixture/Input | Expected output/state |
| --- | --- | --- | --- |
| `test_header_v2_round_trip` | header | epoch1/counter1 | exact `80000100000001` |
| `test_header_rejects_v1_control` | header | `0x40...` | unsupported profile |
| `test_header_rejects_counter_zero` | header | counter0 | range error |
| `test_header_rejects_epoch_other_than_one` | header | epoch 0/2/65535 | AES 전 epoch mismatch |
| `test_header_rejects_counter_above_operational_limit` | header | `2^24+1` | AES 전 counter invalid |
| `test_nonce_layout` | nonce | NP8/counter1 | exact 12B, BE counter |
| `test_aad_exact_bytes_sc_ack` | AAD | `VDS2/ff7f/03/100/200/header` | dummy vector와 exact 일치 |
| `test_aad_uses_wire_vpfi_not_logical_int` | AAD | logical 16383 + wire `ff7f` | AAD에 `ff7f` 포함 |
| `test_aes_gcm_known_answer` | crypto | deterministic vector | expected CT/tag |
| `test_ciphertext_mutation_fails` | crypto | 1 bit 변경 | auth failure, no plaintext |
| `test_tag_mutation_fails` | crypto | tag 변경 | auth failure |
| `test_aad_mutation_fails` | crypto | MF/source/dest/header 변경 | auth failure |
| `test_pdu_build_parse` | PDU | header/CT/tag | byte-exact round trip |
| `test_pdu_truncated` | PDU | <23B | malformed |
| `test_ciphertext_length_allowlist` | MF1/MF2/MF3 | 모든 valid/경계 밖 길이 | 전체 codec 집합만 통과, AES 미호출 |
| `test_counter_reservation_burns_on_failure` | session | forced AES failure | next counter 증가, 재사용 없음 |
| `test_counter_block_crash_gap_safe` | store | reserve block/reopen | durable high watermark 이후부터 시작 |
| `test_counter_exhaustion` | session | usage limit 도달 | fail closed |
| `test_gcm_budget_exhaustion_is_terminal` | TX/RX session | budget 직전/초과 | `EXHAUSTED`, restart로 복구 안 됨 |
| `test_rx_reservation_crash_is_charged_as_failure` | store | reserve 후 강제 reopen | orphan이 failed auth로 전환 |
| `test_sqlite_missing_or_corrupt_fails_closed` | state store | absent/corrupt/wrong schema | auto-create/reset 없이 startup 실패 |
| `test_state_store_single_process_ownership` | state store | 같은 endpoint DB 두 번 open | 두 번째 startup 실패 |
| `test_replay_duplicate` | replay | counter1 두 번 | 두 번째 거부 |
| `test_replay_too_old` | replay | highest100, counter35, window64 | 거부 |
| `test_replay_out_of_order` | replay | 10,12,11 | 모두 최초 1회 수락 |
| `test_auth_failure_does_not_commit_replay` | RX | invalid tag counter5 | replay state 불변 |
| `test_invalid_tag_updates_only_failure_accounting` | RX/store/metrics | valid shape, invalid tag | plaintext 없음, replay 불변, verify/failure budget과 metric만 정확히 1 증가 |
| `test_concurrent_replay_commit` | RX | 같은 counter 병렬 | 한 건만 성공 |
| `test_policy_complete_for_active_registry` | policy | TEST registry | 모든 entry 결정적 정책 |
| `test_sc_vde_vpfi_is_project_test_profile` | registry/policy | 세 SC-VDE entry | exact `ff7f`, REQUIRED, OPERATIONAL registry claim 없음 |
| `test_mode_is_required_and_none_rejected` | dependencies/wiring | missing/None/unknown | config semantic failure |
| `test_enforced_missing_material_never_falls_back` | wiring | REQUIRED+material 누락 | graph 미생성, plaintext 0건 |
| `test_disabled_rejects_required_policy` | dependencies | DISABLED+VDS2 policy | startup 실패 |
| `test_pairwise_material_mapping_and_duplicates` | provider/manager | MASS-A/B/ROC inventory | reciprocal pair만 같고 group/TX-RX reuse 거부 |
| `test_material_repr_and_errors_hide_secrets` | model/errors | sentinel key/NP | repr/log/public error에 sentinel 없음 |
| `test_protected_material_file_permissions` | provider | wrong owner/mode/type, valid owner-only file | wrong 조건 실패, valid file만 적재 |
| `test_provisioning_rejects_existing_or_partial_directory` | provisioning tool | material-only/DB-only/기존 완전 설치 | reset·force·overwrite 없이 모두 실패 |
| `test_db_loss_recovery_workflow_uses_new_bundle` | launcher/provisioning | DB loss, partial old directory, 별도 새 directory+새 bundle | launcher/partial reprovision은 실패; 새 key/NP/`state_id` bundle의 최초 설치만 성공 |
| `test_security_error_projection` | common/projection | §24.1 모든 code | `Layer.PRESENTATION`, 지정 public mapping |

### 28.2 Integration tests

| Test name | Fixture/Input | Operation | Expected |
| --- | --- | --- | --- |
| `test_mass_to_roc_secure_ack` | reciprocal sessions, MF3 | submit→receive | body byte-exact 복원, counter1 |
| `test_roc_to_mass_secure_ack` | 반대 방향 | submit→receive | 방향 key/NP 사용 |
| `test_bidirectional_counters_independent` | M→R, R→M, M→R | 3회 교신 | M TX 1,2 / R TX 1 |
| `test_position_report_uses_data_session` | 14B body | Link 11 환경 | secure segment44B, Data Session |
| `test_route_command_uses_data_session` | n=0 | submit | segment40B, Data Session |
| `test_route_command_all_codec_lengths_admitted` | n=0..16 | submit | 허용 body 모두 길이 선검증 통과 |
| `test_application_ack_uses_short_data` | 3B body | ack policy에 맞춰 submit | segment33B, Short |
| `test_fragmented_secure_pdu_decrypts_after_reassembly` | long command | fragment 순서 교환 | complete 후 1회 decrypt |
| `test_missing_fragment_never_decrypts` | fragment 하나 누락 | receive | application 미전달 |
| `test_link_retransmission_reuses_security_pdu` | 동일 transfer retry | retransmit | counter 추가 소비 없음 |
| `test_completed_security_pdu_replay_rejected` | 완성 PDU 재주입 | receive twice | second drop |
| `test_required_plaintext_rejected` | prefix `ff7f01` 뒤에 평문 body | RX | no fallback/drop |
| `test_disabled_certificate_unchanged` | Certificate Report | TX/RX | 기존 wire byte-exact |
| `test_stage7_restart_preserves_counter` | stop/start | 두 번 TX | counter 증가 지속 |
| `test_rx_restart_rejects_prior_packet` | accept/persist/restart/replay | RX | replay 거부 |
| `test_service_stop_does_not_close_security_manager` | Phase B concurrent encode | stop/start | race 없이 counter 지속 |
| `test_final_close_drains_inflight_and_is_terminal` | in-flight protect + close | final shutdown | bounded drain/DB lock release/재사용 거부 |
| `test_software_sc005_sc006_enforced_lengths` | 실제 software harness | 005 n=3, 006 n=2/0, ACK | §20.3 길이와 암복호화 일치 |
| `test_stage7_launcher_requires_explicit_mode` | launcher argv | mode 누락/ENFORCED | 누락 실패, 세 endpoint enforced 일치 |

### 28.3 Regression tests

- 기존 codec 전 테스트
- Presentation discriminator 및 registry fingerprint 테스트
- Stage5 Phase A/B/C 및 size bound 테스트
- Type 92/93 boundary 테스트
- Link unsigned Segment/fragment/reassembly 테스트
- Data Session retransmission 테스트
- Stage7 wiring/type graph 테스트
- TBB signer/trust store crypto 테스트
- MAC/PHY simulation 및 standalone scenario 테스트
- coordinator를 호출하는 모든 CLI/script의 explicit mode argv 테스트
- installation manifest domain diff가 security 변경을 RF change로 판정하는 테스트
- 새 qualification reference로 `ENFORCED` secure Stage 7 왕복을 검증하는 시험

각 새 test는 fixture, input, operation, expected result뿐 아니라 counter/replay/store 상태 변화도 assert한다.

## 29. Implementation Phases

| Phase | 목적 | 생성/수정 | 완료 조건 |
| ---: | --- | --- | --- |
| 0 | 최신 main 통합 브랜치 준비 | 소스 변경 없음 | `1918da6` 기준, feature 참조만 확보 |
| 1 | 공통 오류와 V2 상수 | common/errors, runtime/projection, constants | 모든 code가 `Layer.PRESENTATION`, projection/size invariant 통과 |
| 2 | protocol bytes와 AES-GCM | header/nonce/AAD/PDU/aes_gcm | deterministic vector와 mutation 시험 통과 |
| 3 | material provider/state/session/replay/provisioning | session/*, e2e_security_provision.py | permission/reciprocal pair/no-overwrite/concurrency/crash/replay 시험 통과 |
| 4 | explicit mode와 policy | dependencies/policy | mode 누락/혼합 fail-closed, active registry 완전성 통과 |
| 5 | Presentation orchestrator | security/presentation.py | standalone protect/unprotect/길이 선검증 통과 |
| 6 | adapter 통합 | presentation/adapter.py | REQUIRED/DISABLED + secure bound 통과 |
| 7 | Stage5/Wideband 및 software harness wiring | runtime/wiring, tests/support/stage5, mass_roc_software | 양방향 software SC005/006 통과 |
| 8 | Stage7 mode 전달/wiring/bootstrap | stage7_run, coordinator, launcher, stage7/wiring | explicit argv, 실제 endpoint 조립, lifecycle 보존 |
| 9 | Link admission/Data Session | test 중심 | ACK Short, Position/Command Data Session 확인 |
| 10 | end-to-end 및 장애 검증 | integration/scenario tests | MASS↔ROC 양방향, fault/replay/persistence/final close 통과 |
| 11 | 설치/자격 재결속 | installation manifest, RF/secure qualification reference | 이전 RF reference 재사용 0, 새 frozen hash와 ENFORCED 증거 발급 |

각 Phase는 이전 Phase test가 모두 통과해야 시작한다. Phase 6 이후에도 기존 Presentation/Link regression을 매 단계 실행한다.

## 30. Traceability Matrix

| Draft Requirement | Final Design | Implementation File | Test |
| --- | --- | --- | --- |
| AES-256-GCM | 32B key, 12B nonce, 16B tag | `security/crypto/aes_gcm.py` | KAT/mutation |
| 7B Security Header | `0x80`, epoch 2B, counter 4B 순서 | `security/protocol/header.py` | header round trip |
| Security Control | V2 exact `0x80` | constants/header | V1 control rejection |
| Domain | exact ASCII `VDS2` | constants/AAD | AAD exact bytes |
| 방향별 key/NP | directional material | session/material.py, manager.py | bidirectional mapping |
| pairwise key 소유권 | reciprocal TX/RX pair 외 재사용 거부 | session/provider.py, manager.py | duplicate/group-key rejection |
| 12B nonce | NP8+counter4 | protocol/nonce.py | nonce layout |
| VPFI/MF 평문 유지 | prefix 보존, body만 암호화 | security/presentation.py | secure wire layout |
| VPFI/MF AAD | actual wire `ff7f` 뒤에 MF | protocol/aad.py | logical-vs-wire VPFI |
| Original src/dst AAD | RouteAddresses original IDs | protocol/aad.py | route mutation auth fail |
| Application Type 제거 | MF를 prefix/AAD로 사용 | header.py, aad.py | header 7B |
| Ciphertext Length 제거 | enclosing Segment/PDU length 사용 | protocol/pdu.py | truncation/trailing tests |
| Replay Window | precheck/auth/commit | session/replay.py | duplicate/old/order |
| REQUIRED 정책 | fail-closed no plaintext fallback | policy.py, adapter.py | required plaintext reject |
| fragmentation 이후 | secure wire를 Link에 전달 | existing Link files | reassembly then decrypt |
| Runtime session 소유 | endpoint lifetime collaborator | wiring/dependencies | restart persistence |
| 실제 길이 admission | secure bound +23 | adapter.py, existing stage5.py | Short/Data Session boundary |
| 명시적 실행 모드 | ENFORCED/DISABLED, default 없음 | dependencies.py, wiring, coordinator/launcher | missing/mixed mode rejection |
| epoch 1 only | exact 1, 소진 후 배포 종료 | header/session/state_store | epoch/terminal-state tests |
| usage budget | nonce `2^24`, blocks `2^32`, failures `2^20` | constants/state_store | budget exhaustion/restart |
| crash-safe state | SQLite WAL/FULL, TX block, RX durable commit | session/state_store.py | crash/orphan/corruption |
| security 오류 통합 | `Layer.PRESENTATION` + explicit projection | common/errors.py, runtime/projection.py | projection/no-secret tests |
| ciphertext 길이 선검증 | codec 전체 허용 길이 집합 | protocol/pdu.py, presentation.py | all valid/invalid lengths |
| security 최종 종료 | Service stop과 분리된 `PresentationSecurity.close_final()` | presentation.py, manager.py, launchers/harness | stop/start/final drain |
| RF 자격 결속 | security 변경 후 새 qualification reference | installation/qualification artifacts | domain diff + secure qualification |
| 프로젝트 전용 VPFI | exact `FF 7F`, TEST/PROJECT 범위 고정 | profiles/payload_formats.py, security/policy.py | registry/policy/AAD exact-byte test |
| DB 복원 규칙 | 알려진 복원·분실 시 같은 material 금지, 새 key/NP/`state_id`/DB | session/provider.py, state_store.py, provisioning tool | missing/partial state 및 fresh redeployment test |
| 모사용 key-at-rest | owner-only protected JSON | session/provider.py, provisioning tool | owner/mode/schema/secret-exclusion test |
| InvalidTag 처리 | rate limiter 없이 fail-closed, replay 불변, `2^20` budget | crypto/aes_gcm.py, session/state_store.py, metrics.py | invalid-tag state/budget/metric test |

## 31. Draft Discrepancy Report

### D-01 — V1 identifier와 V2 wire 충돌

- **Issue:** 7B 통합 형식이 standalone의 `0x40/VDS1`을 재사용할 가능성
- **Evidence — Draft:** 7B header와 변경된 AAD/nonce를 제안
- **Evidence — Repository feature:** V1 constants는 `0x40`, `VDS1`, header14, NP4+counter8
- **Impact:** incompatible parser/profile이 같은 identifier를 공유
- **Resolution:** `0x80`, `VDS2` 채택
- **Classification:** Security issue / Documentation mismatch

### D-02 — Security Header 길이

- **Issue:** 기존 14B 구현을 그대로 재사용할 수 없음
- **Evidence — Draft:** control1+epoch(2B)+counter(4B)=7B
- **Evidence — Feature:** `>BHQBH`=14B이며 app type/ciphertext length 포함
- **Impact:** wire 및 overhead 불일치
- **Resolution:** V2 `>BHI` 새 구현; V1 parser 재사용 금지
- **Classification:** Implementation mismatch

### D-03 — Nonce 구성

- **Issue:** 기존 NP4+counter8과 초안 NP8+counter4 충돌
- **Impact:** 같은 helper 사용 시 명세와 다른 nonce 생성
- **Resolution:** V2 helper를 별도 구현하고 12B assertion
- **Classification:** Implementation mismatch

### D-04 — VPFI 직렬화

- **Issue:** logical `16383`을 2B big-endian으로 쓰면 `3F FF`, 실제 SC wire는 `FF 7F`
- **Evidence — Repository:** `payload_formats.py`가 이 차이를 명시
- **Impact:** 송수신 AAD 불일치 또는 잘못된 binding
- **Resolution:** AAD에는 received/generated exact wire prefix bytes 사용
- **Classification:** Security issue

### D-05 — codec 결과 분리

- **Issue:** 모든 format에 고정 길이 slicing을 일반화할 수 없음
- **Evidence — Repository:** discriminator가 prefix와 선택적 third-byte mask를 지원
- **Impact:** 잘못된 body 암호화/dispatch
- **Resolution:** 현재 REQUIRED SC entry를 exact 3B prefix로 제한하고 build-time 검증
- **Classification:** Design constraint

### D-06 — Stage 7 Data Session enable 조건

- **Issue:** 초안/이전 main은 `data_session_enabled=false`를 전제로 실패 조건을 제시
- **Evidence — Latest main:** config field와 `Stage7Endpoint` 차단 override가 제거됨
- **Impact:** 오래된 명세는 가능한 전송을 불필요하게 거부
- **Resolution:** 별도 enable 조건 삭제; 기존 Link admission/Data Session 흐름 사용
- **Classification:** Repository drift

### D-07 — `crypto_dependencies` 의미

- **Issue:** 같은 이름에 E2E AES 상태를 넣을 가능성
- **Evidence — Repository:** `LinkCryptoDependencies`는 TBB signer/trust store만 포함
- **Impact:** 계층 책임 혼합 및 restart wiring 오류
- **Resolution:** `E2eSecurityDependencies`/`e2e_security` 별도 인자
- **Classification:** Implementation mismatch

### D-08 — standalone 자체 VDE adapter/reassembly

- **Issue:** feature의 `vde_secure/vde/*`와 repository Link 기능 중복
- **Impact:** 이중 segmentation, 상이한 capacity, 재전송/상태 충돌
- **Resolution:** AES/session/replay 개념만 이식하고 자체 transport 계층 제거
- **Classification:** Unnecessary complexity

### D-09 — counter 영속화만 하고 RX replay를 휘발시키는 설계

- **Issue:** 재시작 후 과거 인증 packet 재수락 가능
- **Impact:** replay 방지 보장이 process lifetime으로 축소
- **Resolution:** 동일 store에 RX highest/bitmap도 application 전달 전에 영속화
- **Classification:** Security issue

### D-10 — SC-VDE 운영 상태

- **Issue:** `FF 7F` 세 format을 실제 운영 VPFI처럼 서술할 위험
- **Evidence — Repository:** TEST/PROJECT profile이며 OPERATIONAL registry 미등록
- **Impact:** 표준/상호운용성 오해
- **Resolution:** `FF 7F`를 폐쇄형 `voyager_3` 모사의 프로젝트 전용 TEST/PROJECT 값으로 채택하고, 표준·OPERATIONAL 할당이나 외부 상호운용성을 주장하지 않음
- **Classification:** Documentation mismatch / Resolved scope decision

### D-11 — 존재하지 않는 security 오류 code/layer

- **Issue:** 초안 명칭 `UNSUPPORTED_SECURITY_PROFILE`과 별도 Security layer는 최신 repository에 없음
- **Evidence — Repository:** `common/errors.py`에는 `Layer.PRESENTATION`은 있으나 `Layer.SECURITY`와 security code가 없고, `runtime/projection.py`의 미등록 code는 공개 `INTERNAL_ERROR`가 됨
- **Impact:** 구현자가 문자열/예외를 임의 생성하거나 정상 protocol 오류가 INTERNAL로 오분류됨
- **Resolution:** §24.1의 `PRESENTATION_SECURITY_*` code를 추가하고 모두 `Layer.PRESENTATION` 및 명시적 projection 사용
- **Classification:** Implementation mismatch

### D-12 — optional security 주입의 downgrade 위험

- **Issue:** `security=None`/`e2e_security=None`이면 secure profile도 평문으로 실행될 수 있음
- **Impact:** launcher/fixture wiring 누락이 조용한 보안 비활성화로 이어짐
- **Resolution:** `ENFORCED`/`DISABLED`를 필수로 전달하고 default/자동 fallback 제거
- **Classification:** Security issue

### D-13 — software harness 통합 지점 누락

- **Issue:** `apps/mass_roc_software.py`가 직접 Runtime builder가 아니라 `VDETER/tests/support/stage5.py::build_stage5_software_group()`를 거침
- **Impact:** runtime wiring만 바꾸면 실제 software 시나리오에 material이 주입되지 않음
- **Resolution:** 두 파일을 모두 §26.2 수정 대상으로 포함
- **Classification:** Implementation mismatch

### D-14 — 시나리오 일부 길이만 허용할 위험

- **Issue:** 현재 예제는 route body 10/29/39B를 생성하지만 codec은 n=0..16의 17개 길이를 허용함
- **Impact:** 예제 길이만 선검증하면 정상 waypoint 수의 메시지가 거부됨
- **Resolution:** `SC_VDE_COMMAND_LENGTHS` 전체를 ciphertext allowlist로 사용하고 §20.3에 실제 예제 길이는 별도 표시
- **Classification:** Design constraint

### D-15 — security 변경과 기존 RF qualification reference

- **Issue:** `qualification_domains.py`가 `VDETER/src/vdeter/**`와 endpoint launcher를 RF 영역에 결속함
- **Impact:** 보안 source를 추가하고도 이전 qualification reference를 재사용하면 설치 검증 계약 위반
- **Resolution:** 구현 Phase 11에서 installation manifest/qualification reference를 새로 발급
- **Classification:** Repository integration requirement

### D-16 — manager 종료 시점

- **Issue:** 최신 `Stage5Endpoint.stop()`은 lock 밖 Phase B codec 종료를 기다리지 않고 adapter 자체는 유지함
- **Impact:** Service stop에서 state store/key를 닫으면 in-flight encode race와 이후 restart 실패가 발생
- **Resolution:** stop/lane restart에는 유지하고 process finalizer의 유계 `close_final()`에서만 종료
- **Classification:** Concurrency / lifecycle issue

## 32. Open Questions

현재 모사 링크 통합 구현을 막는 Open Question은 없다. 다음 항목은 범위 결정으로 종료되었으며 구현자가 임의로 다시 선택하지 않는다.

| 기존 쟁점 | 확정 결정 | 구현 효과 | 의도적으로 제공하지 않는 보장 |
| --- | --- | --- | --- |
| SC-VDE VPFI | exact wire `FF 7F`를 프로젝트 전용 TEST/PROJECT 값으로 유지 | registry/policy/AAD/tests가 같은 exact bytes 사용 | 표준·OPERATIONAL 할당 및 외부 상호운용성 |
| DB rollback | 악의적 전체 disk/snapshot rollback은 위협 모델 밖; 알려진 복원·분실 시 새 key/NP/`state_id`/DB 배포 | 자동 state 재생성·reset·동일 material 재사용 금지 | 완전히 일관된 과거 snapshot을 local software만으로 탐지 |
| key-at-rest | service-account 전용 protected JSON 채택 | owner/mode/schema 검사 및 repository/log/backup 제외 | root compromise, disk theft, Python memory zeroization |
| InvalidTag DoS | 별도 peer/global/MAC rate limiter 없음 | InvalidTag 폐기, replay 불변, metric·경보 및 `2^20` failure budget 유지 | hostile RF 자원 고갈 공격에 대한 가용성 |

Security Control `0x80`, Domain `VDS2`, endpoint별 SQLite store, 명시적 mode와 epoch 1 종료 정책도 본 명세의 채택된 규범값이다. 향후 실제 선박 탑재나 외부 상호운용 범위가 새로 승인되면 별도 threat model과 새 명세 revision으로 재검토하며, 현재 구현의 숨은 요구사항으로 취급하지 않는다.

## 33. Implementation Acceptance Checklist

- [ ] 구현 기준 branch가 최신 main `1918da6` 이후인가
- [ ] 기존 파일/함수명이 실제 main과 일치하는가
- [ ] Header field 합이 7B인가
- [ ] nonce 합이 12B인가
- [ ] AAD 합이 22B이며 exact wire VPFI `FF 7F`를 쓰는가
- [ ] `FF 7F`가 TEST/PROJECT 전용으로 표시되고 OPERATIONAL/외부 상호운용 claim이 없는가
- [ ] Security PDU overhead가 23B인가
- [ ] secure Presentation overhead가 plain wire 대비 23B인가
- [ ] tag가 16B인가
- [ ] 모든 security 오류가 기존 `Layer.PRESENTATION`과 §24.1 code/projection을 쓰는가
- [ ] 모든 builder/launcher caller가 `ENFORCED` 또는 `DISABLED`를 명시하며 `None` fallback이 없는가
- [ ] REQUIRED plaintext fallback이 없는가
- [ ] reciprocal TX/RX pair 외 key 또는 `(key, NP)` 재사용이 startup에서 거부되는가
- [ ] key/NP material file과 SQLite path/owner/mode/schema가 §12/§23과 일치하는가
- [ ] auth 실패 packet이 replay state를 바꾸지 않는가
- [ ] auth 실패 packet의 RX verification/failure budget은 crash-safe하게 증가하는가
- [ ] Link retransmission이 새 counter를 소비하지 않는가
- [ ] restart 후 TX counter와 RX replay가 재사용/초기화되지 않는가
- [ ] 알려진 DB 복원·분실·부분 설치에서 자동 복구하지 않고, 새 key/NP/`state_id`/DB 없이는 startup을 거부하는가
- [ ] provisioning 도구에 reset/force/overwrite 및 기존 material 기반 state 재생성 경로가 없는가
- [ ] epoch가 exact 1이고 한도 소진/상태 오류가 terminal이며 restart/reset 복구가 없는가
- [ ] codec 전체 ciphertext 길이 allowlist와 실제 SC005/006 길이가 §9.4/§20.3과 일치하는가
- [ ] Position/Command가 Data Session, ACK가 Link 11 Short 경계에 맞는가
- [ ] 최신 main의 Stage7 Data Session 차단 제거를 반영했는가
- [ ] TBB `crypto_dependencies`와 E2E dependency가 분리됐는가
- [ ] Service stop/lane restart는 manager를 유지하고 process finalizer만 유계 `close_final()`을 호출하는가
- [ ] 별도 InvalidTag rate limiter가 없더라도 InvalidTag가 폐기되고 plaintext/replay state에 영향 없이 metric과 failure budget만 갱신되는가
- [ ] protected material JSON이 owner-only이고 repository/log/backup/evidence에 포함되지 않는가
- [ ] key/NP/nonce/plaintext가 log에 남지 않는가
- [ ] 기존 Presentation/Link/MAC/PHY regression이 통과하는가
- [ ] security source 변경 후 installation manifest/RF qualification reference를 새로 발급했는가

## 34. References

- NIST SP 800-38D, *Recommendation for Block Cipher Modes of Operation: Galois/Counter Mode (GCM) and GMAC*: <https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-38d.pdf>
- NIST CSRC, 2026 SP 800-38D revision pre-draft notice: <https://csrc.nist.gov/News/2026/gcm-and-gmac-block-cipher-modes-of-operation>
- Repository implementation source of truth: `origin/main` `1918da68d8ce18101b8309a7879e7d99714a7fda`
- AES-GCM standalone reference: `feature/aes256-gcm-e2e` `a8ace84da4bf6c49536e12813ca77cc1fb7155ae`
- Draft design baseline: `모사_링크_통합(초안).pdf`
