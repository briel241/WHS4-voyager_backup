# SC-VDE-001 — ROC → MASS X.509 인증서 전송

> 원안(Type 92 단일 송신)을 표준 정합성 검토 후 수정한 판이다.
> 변경 사유는 §8에 정리했다.

| 항목 | 내용 |
| --- | --- |
| 시나리오 ID | `SC-VDE-001` |
| 목적 | ROC가 MASS에 X.509 공개키 인증서를 전달한다 |
| 분류 | 대용량 · 일방적 · 지정 수신자 |
| `data_type` | `X509_CERTIFICATE` |
| `payload_format` | `x509.certificate_report` (VPFI 0 / MF 1, **운영 scope**) |
| 상태 | ✅ **지원** — R1027 Certificate Report와 VDE-TER Data Session을 통한 분할·재조립·ACK 경로 구현. 소프트웨어 무선 loopback에서 정상 저장과 위조 거부 검증 완료. B210 실물 시나리오 시험은 별도 수행 |

---

## 1. 목적

ROC가 MASS에게 서명 검증에 사용할 **X.509 공개키 인증서를 전달**한다.

이 시나리오는 다른 모든 보안 시나리오의 **전제**다. 인증서가 없으면 어떤 서명도
검증할 수 없다. 동시에 **인증서 전달 자체를 무엇이 보증하는가**라는 문제를 드러낸다.

---

## 2. 사전 조건

- 양측 시간 동기화 완료 (TDMA slot 정렬 및 인증서 유효기간 판정에 필요)
- 100 kHz 물리 채널 사용
- **Link ID 17** (TER-MCS-1.100, π/4-QPSK, FEC 1/2, packet 234 byte)
- ROC가 유효한 발급자 인증서를 보유
- MASS에 신뢰 앵커(trust anchor)가 사전 탑재되어 있음 — §7.1 참조

---

## 3. 참여자별 행위

| 참여자 | 식별자 | 하는 일 |
| --- | --- | --- |
| ROC | Source ID `001` | 인증서를 준비하고 전송 세션을 요청한다. fragment로 나누어 보낸다 |
| MASS | Destination ID `002` | fragment를 모아 재조립하고, 서명을 검증한 뒤 신뢰 저장소에 반영한다 |
| 공격자 | — | 자원할당·ACK를 방해하거나 옛 CRL을 재전송한다 (§7.2) |

시간 순으로 쓰면 다음과 같다.

- **ROC가** 전송할 인증서를 준비하고 **자원을 요청한다**.
- **제어국이** 논리 채널과 Link ID를 **배정한다**.
- **ROC가** 인증서를 signed segment로 감싸 **fragment로 나누어 보낸다**.
- **MASS가** fragment를 모아 **재조립한다**.
- **MASS가** 서명을 **검증한다**. 통과한 경우에만 신뢰 저장소를 갱신한다.
- **MASS가** 수신 결과를 **ACK로 알린다**.
- **ROC가** 전송 완료 상태로 **전환한다**.

> M.2092-2 §A1-2.4.2는 VDES국이면 MMSI를, 그 외 목적이면 999,999,999 초과
> 2³² 미만 식별자를 쓰도록 정한다. `001`/`002`는 시험 편의값이며 실운용 전에 교체한다.

---

## 4. 입력 데이터

### 4.1 형식

ITU-T X.509 v3 기반 ECDSA 공개키 인증서, **ASN.1 DER 바이너리 인코딩**.

암호 파라미터는 **M.2092-2 §A4-4.15가 지정한 값을 그대로 따른다.** 선택의 여지가 없다.

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 공개키 알고리즘 | `id-ecPublicKey` | §A4-4.15 |
| 곡선 | **secp256r1** | §A4-4.15 |
| 공개키 크기 | **256 bit** | §A4-4.15 |
| 발급자 서명 | **ecdsa-with-SHA256** | §A4-4.15 |
| 서명 인코딩 | IEEE P1363 Fixed-Field Concatenation | §A4-4.15 |
| 인증서 식별 | signatureValue 최하위 **48 bit** 지문 | §A4-4.15, RFC 5480 §4.1.1.3 |

### 4.2 크기

- 가변 길이
- **최대 허용 1,024 byte**

### 4.3 내용

인증서 버전 · 일련번호 · 발급자 · 유효기간 · 인증 대상 · ECDSA 공개키 · 사용 곡선 ·
공개키 용도 · 주체 키 식별자 · 발급자 키 식별자

**X.509 확장 필드**: MASS ID, ROC ID, MMSI, IMO 번호

---

## 5. 전송 경로

### 5.1 왜 Short Data Message를 쓸 수 없는가

인증서 1,024 byte는 **Type 92 한 개에 들어가지 않는다.**

```text
Link ID 17 packet            234 byte
  − CRC-32                     4
  − Type 92 헤더               13
  ────────────────────────────────
  = payload 가용              217 byte      ←  1,024 byte 수용 불가
```

Type 92/93은 **한 슬롯 전용 전송**이다.

> "The short data message refers to the data transfer protocol used for transmission of
> payload in **one slot only**." — M.2092-2 §A4-4.2.5

또한 인증에 쓰이는 **Signed segment는 fragment 안에만 담긴다**(§A4-4.29). Type 92/93의
Payload 필드에는 segment 개념이 없다. 즉 **Short Data Message로는 서명된 전송 자체가 불가능**하다.

### 5.2 채택 경로 — Data session

```text
Type 90  Resource request         ROC → MASS   자원 요청 / 송신 예고
Type 4   Resource allocation      제어국       LC·Link ID·TDMA frame 배정
Type 74  Start fragment           ROC → MASS   첫 fragment
Type 75  Continuation fragment    ROC → MASS   중간 fragment (반복)
Type 76  End fragment             ROC → MASS   마지막 fragment
Type 81  Padding byte             ROC → MASS   packet 잔여 채움
Type 13  ACK/NACK                 MASS → ROC   fragment 수신 확인
```

### 5.3 fragment 수 계산

Signed segment(§A4-4.29 Table 55) 오버헤드는 **Valid until 포함 79 byte**다.

```text
segment 전체 = 79 (헤더) + 1,024 (인증서) = 1,103 byte

Type 74  첫 fragment 가용   234 − 4 − 15 = 215 byte
Type 75/76 이후 가용        234 − 4 − 14 = 216 byte

                            →  총 6 fragment
```

참고로 **unsigned segment**(오버헤드 4 byte)를 쓰면 5 fragment다. 본 시나리오는
**signed segment를 기본**으로 한다 — §7.1의 부트스트랩 문제 때문이다.

---

## 6. 메시지 흐름

```text
ROC                                                    MASS
 │                                                       │
 │──[1] Type 90  Resource request ─────────────────────▶ │
 │                                                       │
 │◀─[2] Type 4   Resource allocation ─────────────────── │   LC·Link ID 배정
 │                                                       │
 │──[3] Type 74  Start fragment       (1/6) ───────────▶ │
 │──[4] Type 75  Continuation         (2/6) ───────────▶ │
 │──[5] Type 75  Continuation         (3/6) ───────────▶ │
 │──[6] Type 75  Continuation         (4/6) ───────────▶ │
 │──[7] Type 75  Continuation         (5/6) ───────────▶ │
 │──[8] Type 76  End fragment         (6/6) ───────────▶ │
 │                                                       │
 │                                    CRC 검사           │
 │                                    Destination ID 검사│
 │                                    Segment 재조립     │
 │                                    서명 검증          │
 │                                                       │
 │◀─[9] Type 13  ACK/NACK ─────────────────────────────  │   mask 0/1/2 · CQI
 │                                                       │
 │  전송 완료 상태 전환                                    │
 │                                    payload를 응용에 전달│
```

### 6.1 검증 순서 — 상태 변경 전에 검증한다

> "In case the verification of the signature fails on the VDES mobile station the message
> cannot be trusted. For Link Layer messages, such as the bulletin board, the system shall
> act as if it **never had received the message**." — §A4-4.15

MASS는 **검증을 마친 뒤에만** 인증서를 신뢰 저장소에 반영한다. 검증 실패 시 기존
저장소를 건드리지 않는다.

### 6.2 실제 연동 (구현 완료)

응용이 하는 일은 짧다. 상세: `docs/vdeter_mass_roc_api_guide.md`.

```python
# ROC — 인증서를 Certificate Report 본문으로 감싸 송신 요청 생성
outbound = roc_issue_certificate_report(certificate_der, mass_id=MASS_ID)
request = vdeter_bridge.to_submit_request(outbound)   # → VDETER SubmitTransferRequest
service.submit(request)                                # payload_format=x509.certificate_report

# MASS — 수신 통지를 받아 검증·저장
def on_data(inbound):                                  # vdeter_bridge.Inbound
    if inbound.is_certificate_report:
        mass_receive_certificate_report(inbound.payload, trust, anchor_der, now)
```

**분할·재조립·자원할당·ACK는 전부 VDE-TER Service 내부 일**이다. 응용은 Link ID도
Message Type도 만들지 않는다. 종단 왕복은 `apps/cert_loopback_demo.py`로 확인된다.

### 6.3 검증 범위 — RFC 5280 중 무엇을 하고 무엇을 안 하는가

`common/security/certificate.py`의 검증은 **단일 CA · 사전 탑재 신뢰 앵커**를
전제한 축소 구현이다. 보고서에 "X.509 검증"이라고만 적으면 실제보다 넓게
읽히므로 범위를 못 박는다.

**하는 것** (실측 확인 — 각 경우 신뢰 저장소 변화까지 검사했다)

| 검사 | 실패 시 사유 |
| --- | --- |
| 발급자 서명 (ECDSA P-256 / SHA-256) | `bad-signature` |
| 유효기간 시작 전 | `not-yet-valid` |
| 유효기간 경과 | `expired` |
| 발급자 이름이 앵커 주체와 같은가 | `issuer-mismatch` |
| 앵커의 BasicConstraints `cA=TRUE` | `anchor-not-ca` |
| 크기 상한 1,024 byte | `too-large` |
| 구조 파싱 | `parse-error` |
| 통과한 것만 저장 · 재수신 멱등 | (§A4-4.15) |

**안 하는 것**

| 항목 | 현재 | 왜 지금은 문제가 아닌가 |
| --- | --- | --- |
| **폐기 확인 (CRL/OCSP)** | 안 함. Report에 CRL(type 2)이 오면 `crl-not-handled`로 남기고 적용하지 않음 | 폐기 배포 경로 자체가 미정의. 실운용에서는 반드시 필요 |
| **KeyUsage / keyCertSign** | 앵커의 KeyUsage를 보지 않음 | 앵커를 out-of-band로 우리가 심음 |
| **인증서 체인** | 1단계만 (앵커 → 단말). 중간 CA·path length 없음 | 시나리오가 단일 CA 구성 |
| **이름 대조** | Subject/Issuer의 **CN 문자열 동일성** | 우리 인증서가 CN 하나만 씀. RFC 5280의 Name 대조는 아님 |
| **앵커에 BasicConstraints가 없을 때** | CA로 통과시킴 (판단 보류) | 앵커를 우리가 만들어 항상 넣음 |
| **signatureAlgorithm 필드 대조** | TBS 안팎의 알고리즘 식별자를 비교하지 않음 | ECDSA/SHA-256으로 고정 검증하므로 불일치는 서명 실패로 귀결 |

**대체 백엔드 주의** — `cryptography`가 없는 환경에서는 HMAC 기반 대체 경로로
내려간다. 구조와 흐름은 같지만 **공개키 암호가 아니다.** 성능·크기 수치를
보고할 때는 반드시 표준 백엔드에서 측정한다 (검증 1건 실측: 표준 ECDSA
**125 µs** vs 대체 **25 µs**, 인증서 크기 **412 B** vs **280 B**).

---

## 7. 보안 관점

### 7.1 부트스트랩 문제 — 이 시나리오의 핵심

**인증서를 검증하려면 이미 신뢰하는 인증서가 있어야 한다.** 최초의 신뢰는 무선으로
전달할 수 없다. 따라서 신뢰 앵커는 **사전 탑재(out-of-band provisioning)** 를 전제한다.

표준은 이 부분을 정의하지 않는다. §A4-4.15는 *"국제기구가 CA 역할을 하는 PKI가
수립되어 있다고 가정한다(It is assumed that…)"* 라고만 쓴다. **CA 주체·키 배포·초기
provisioning 절차가 모두 비어 있다.**

### 7.2 위험 (Risk)

**위험 등급: 최상 · 간접**

인증서 전달은 **인증되지 않은 제어 메시지 위를 지난다.** Type 4·90·13에는 서명 필드가 없다.

| 공격 | 방법 | MASS에 미치는 영향 | 등급 |
| --- | --- | --- | :-: |
| **차단 (DoS)** | 자원할당(Type 4) 방해로 CRL 세션 저지 | **폐기된 키를 계속 신뢰한다** → 이후 위조 명령이 전부 통과 | **최상** |
| **재전송** | 옛 CRL 재전송 | 폐기 사실이 무효화된다 | 상 |
| **데이터 위조** | 신뢰 앵커 부재 시 가짜 인증서 주입 | 신뢰 체계 전체가 무너진다 | 최상 |
| **차단** | Type 13 mask 위조 | 무한 재전송 또는 조기 종료 (세션 교착) | 상 |
| 탈취 | 인증서 도청 | **영향 없음** — 공개키는 공개 정보다 | 하 |

**차단이 가장 위험하다.** 표준 스스로 *"CA와 연결이 불가능할 때를 대비해 CRL을 저장하라"*
고 요구하는데, **그 오프라인 구간이 곧 공격 창**이다. 공격자는 서명을 깨지 않아도 된다 —
**갱신만 막으면 된다.**

### 7.3 간접 위험이므로 무대가 필요하다

인증서 오염 자체로는 배가 움직이지 않는다. 영향을 보이려면 **직접 시나리오를 무대로 빌려야**
한다.

```text
인증서·CRL 오염  →  위조된 항로 지시가 검증을 통과  →  MASS가 항로를 벗어난다
   (SC-VDE-001)              (신뢰 붕괴의 결과)          (SC-VDE-005 무대)
```

관찰 도구의 항적 비교로 마지막 단계를 보여준다.
`docs/scenarios/scenario_list.md` §4.1 참조.

---

## 8. 원안 대비 변경 사항

| 항목 | 원안 | 수정 | 사유 |
| --- | --- | --- | --- |
| 전송 경로 | Type 92 단일 송신 | **Type 90→4→74/75/76→13 data session** | 1,024 B가 Type 92(가용 217 B)에 불가. §A4-4.2.5 한 슬롯 제한 |
| ACK | "Type 13 ACK, mask가 ACK 표시" | data session의 fragment ACK | Type 13의 mask는 **fragment 단위**다(Table 38). 단일 Short Data Message에는 의미가 없다 |
| 서명 | 언급 없음 | **Signed segment 사용 명시** | Short Data Message로는 서명 자체가 불가(§A4-4.29) |
| 채널 조건 | 동축 루프백 · 손실 없음 | 유지 | 기준선 확보에 적절 |
| 수집 로그 | 유지 | 유지 + fragment 번호 | 재전송·부분 손실 분석에 필요 |

---

## 9. 채널 조건

- 동축 루프백
- 패킷 손실 없음 (기준선 측정)
- Link ID 17 고정
- `payload_format` **`x509.certificate_report`** (VPFI 0 · Message Format 1 · 운영 scope)

**크기 실측** — 인증서 1건을 담은 Certificate Report 본문은 **354 byte**다
(`apps/cert_loopback_demo.py` 출력). 노선·조종과 달리 carrier가 시험용 `ff 7f`가
아니라 **IALA R1027이 정식 배정한 VPFI 0**이며, 그래서 이 시나리오만 운영 scope다.

> 우리가 `submit(payload=...)`에 넣는 것은 Certificate Report **본문**이다. VPFI와
> Message Format은 VDE-TER Presentation codec이 붙인다(`../vdeter_mass_roc_api_guide.md` §8.2).

---

## 10. 예상 결과

- 수신·재조립된 payload가 원본 인증서와 **바이트 단위로 일치**
- 서명 검증 성공
- Type 13 ACK/NACK mask가 전 fragment 수신을 표시
- 전송 상태가 `COMPLETED`로 전환 (API 계약 §3.2.1)
- 중복 실행 없음 (동일 인증서 재수신 시 멱등 처리)

---

## 11. 실패 조건

| 조건 | 기대 동작 |
| --- | --- |
| CRC 실패 | 해당 fragment NACK, 재전송 |
| ACK 타임아웃 | 재전송 후 한계 초과 시 `FAILED` |
| payload 불일치 | 재조립 실패로 응용에 전달하지 않음 |
| **서명 검증 실패** | **수신하지 않은 것처럼 처리**(§A4-4.15). 신뢰 저장소 불변 |
| 인증서 유효기간 경과 | 응용이 거부 |
| 크기 초과 (>1,024 B) | `PAYLOAD_TOO_LARGE` |

---

## 12. 수집 로그

| 항목 | 용도 |
| --- | --- |
| 송수신 시각 | 지연·타임아웃 분석 |
| 메시지 Type | 흐름 검증 |
| Source / Destination ID | 사칭 탐지 |
| Link ID | 링크 적응 추적 |
| **Fragment 번호 / 총 개수** | 부분 손실 위치 파악 |
| CRC 결과 | 물리 계층 품질 |
| CQI | 채널 품질, Type 4·13에 포함 |
| ACK/NACK mask | 재조립 상태 |
| 재전송 횟수 | 링크 안정성 |
| **서명 검증 결과** | 보안 판정 |
| **Segment type** (signed/unsigned) | 인증 적용 여부 |

---

## 참고

- ITU-R M.2092-2 §A4-4.2.5(short data message), §A4-4.9(메시지 정의),
  §A4-4.15(디지털 서명), §A4-4.29(segmentation), Table 8(Link ID), Table 38(ACK/NACK),
  Table 49~51(fragment), Table 54~55(segment)
- [`../vdeter_mass_roc_api_guide.md`](../vdeter_mass_roc_api_guide.md) §3.1.2, §5, §6, §8.2
- [`../mass_roc_background.md`](../mass_roc_background.md) §6.4
- 구현: `common/security/`, `common/messages/certificate_report.py`, `common/vdeter_bridge.py`
- 시험: `tests/test_certificate.py`, `test_certificate_report.py`, `test_cert_vdeter_integration.py`
- 실행: `python apps/scenario_runner.py run SC-VDE-001` — 응용 시나리오 재현·판정
- 종단 검증: `python apps/cert_loopback_demo.py` — VDE-TER 소프트웨어 무선
  Data Session loopback, 정상/위조 검증(cryptography 필요)
