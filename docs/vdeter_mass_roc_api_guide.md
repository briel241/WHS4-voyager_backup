# MASS/ROC 개발자를 위한 VDE-TER Service API 사용 가이드

## 1. 문서 목적

애플리케이션은
송신할 업무 데이터를 `bytes`로 만들고, 목적지와 데이터 형식을 지정하고, 공개 event를 처리한다.

VDE-TER이 제공하는 범위는 다음과 같다.

- endpoint 시작과 종료
- 송신 요청 접수, `transfer_id` 발급, 상태 조회와 취소
- 등록된 payload format의 Presentation 처리
- protocol 전송, 수신 검증·재조립
- 전송 상태, 수신 데이터, endpoint 오류를 공개 event로 통지

MASS/ROC 애플리케이션이 담당하는 범위는 다음과 같다.

- station과 role에 맞는 애플리케이션 구성
- X.509, AIS 또는 그 밖의 업무 데이터 생성·직렬화
- 수신 bytes의 역직렬화와 업무 정책 적용
- X.509 trust/chain/signature/revocation 판정
- AIS 원문 매칭, 인증·무결성, freshness와 replay 판정
- 업무 timeout, 중복 방지, 재시도와 운영자 알림

VDE-TER은 application bytes를 전달하고 protocol transport 결과를 제공한다. X.509 인증 성공,
AIS 인증 성공 또는 전자해도 해석 성공을 판정하지 않는다.

### 1.1 이 문서의 API 경계와 사용 전제

| 구분 | 제공·구현하는 항목 |
| --- | --- |
| VDE-TER 공개 API | `VdeService` Protocol, 다섯 Service 메서드, 요청·응답 DTO, `ServiceEvent`, `HandlerServiceEventSink` |
| launcher/integration | endpoint와 실제 `VdeService` 구현체 조립, event sink 연결, config·장치 준비, 다중 endpoint lifecycle 조정 |
| MASS/ROC 애플리케이션 | 업무 payload 직렬화·역직렬화, event 업무 처리, API 호출 결과 처리, 업무 timeout·중복·재시도 정책 |

`VdeService`는 공개 Protocol이며 애플리케이션이 직접 생성하는 구현 class가 아니다. 현재 공개 API에는
endpoint factory나 이미 만들어진 service에 sink를 나중에 연결하는 메서드가 없다. 따라서 애플리케이션은
launcher/integration이 event sink와 연결해 주입한 `VdeService`를 사용한다.

이 문서에서 이름에 `app_`, `Scenario` 또는 `scenario`가 붙은 항목과 `build_*`, `wait_*`,
`run_*`, `require_ok()` 함수는 공개 API가 아니라 시나리오를 설명하기 위한 애플리케이션 예제 코드다.
실제 공개 API 이름은 import 표와 §3에 기재된 항목으로 한정한다.

## 2. 전체 사용 구조

```text
MASS/ROC application
        │ start / submit / status / cancel / stop
        ▼
public VdeService
        │
        ▼
VDE-TER 내부 처리 ───────────────▶ 상대 endpoint
        │                              │
        └── ServiceEventSink.publish() ◀┘
                         │
                         ▼
               MASS/ROC event handler
```

`submit`은 “보낼 요청을 제출한다”는 뜻이고, `sink`는 VDE-TER이 event를 내보내는 대상,
`publish`는 sink에 event 하나를 전달하는 호출, `handler`는 그 event를 받는 애플리케이션 함수다.
`event`는 나중에 발생한 상태 변화나 데이터 수신을 알리는 객체다.

Link/MAC/PHY/Radio는 format 선택, Packet, 자원·TBB, ACK/retry, waveform과 장치를 처리한다.
애플리케이션은 이 private Port를 직접 호출하거나 Link ID, Message Type, slot, channel을 고르지 않는다.

## 3. 애플리케이션이 사용하는 공개 구성요소

### 3.1 import 경로

```python
from vdeter.common.addressing import Address, Addressed, Broadcast
from vdeter.common.contracts.service import (
    CancelAccepted,
    CancelTransferRequest,
    EndpointHealth,
    ReceivedApplicationData,
    ServiceRole,
    ServiceStarted,
    ServiceStartRequest,
    ServiceStopped,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferAccepted,
    TransferState,
    TransferStatusRequest,
    TransferStatusSnapshot,
)
from vdeter.common.errors import ServiceError, ServiceErrorCategory
from vdeter.common.events import (
    EventSeverity,
    ServiceEvent,
    ServiceEventSink,
    ServiceEventType,
)
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.common.results import ServiceResult
from vdeter.service import HandlerServiceEventSink, ServiceEventHandler, VdeService
```

`VdeService`, `HandlerServiceEventSink`, `ServiceEventHandler`는 `vdeter.service` package boundary에서
export된다. 요청·응답 DTO는 `vdeter.common.contracts.service`, event 계약은
`vdeter.common.events`, `ServiceResult`는 `vdeter.common.results`에 있다.

### 3.2 구성요소별 사용 계약

| 구성요소                  | 용도·호출 주체                            | 입력                             | 즉시 반환                   | 이후 event·주의사항                                              |
| ------------------------- | ----------------------------------------- | -------------------------------- | --------------------------- | ---------------------------------------------------------------- |
| `VdeService`              | MASS/ROC가 호출하는 공개 Service Protocol | 다섯 요청 DTO                    | `ServiceResult[T]`          | 구현 객체는 launcher가 조립·주입한다.                           |
| `ServiceEventSink`        | VDE-TER이 event를 전달하는 공개 Protocol  | `ServiceEvent`                   | 내부 전달 접수 결과         | 애플리케이션이 `publish()`를 직접 호출하지 않음                  |
| `HandlerServiceEventSink` | handler를 sink로 연결하는 공개 adapter    | `Callable[[ServiceEvent], None]` | `publish()` 때 전달 결과    | 호출 thread에서 handler를 즉시 실행; 자체 queue/thread 없음      |
| `ServiceStartRequest`     | endpoint 시작 입력                        | station, role, config ref        | `ServiceStarted`            | health와 lifecycle event 가능                                    |
| `SubmitTransferRequest`   | application bytes 송신 입력               | destination, format, bytes, ACK  | `TransferAccepted`          | 완료·실패는 transfer event로 확인                                |
| `TransferStatusRequest`   | 현재 상태 조회                            | `transfer_id`                    | `TransferStatusSnapshot`    | 조회 자체는 새 전송을 만들지 않음                                |
| `CancelTransferRequest`   | 진행 중 전송 취소                         | `transfer_id`                    | `CancelAccepted`            | 이미 전송된 data 회수는 보장하지 않음                            |
| `ServiceStopRequest`      | endpoint 종료                             | reason                           | `ServiceStopped`            | 진행 중 transfer가 `FAILED`가 될 수 있음                         |
| `ServiceResult[T]`        | 동기 성공/실패 표현                       | 해당 없음                        | `Ok.value` 또는 `Err.error` | `result.is_ok`, `result.is_err`로 분기                           |
| `ServiceError`            | 공개 구조화 오류                          | 해당 없음                        | code/category/message 등    | `retryable`은 자동 재시도 명령이 아니라 hint                     |
| `ServiceEvent`            | 비동기 공개 통지                          | 해당 없음                        | sink로 전달                 | `event_id` 기준으로 handler를 멱등하게 처리                      |
| `PayloadFormat`           | registry의 local format key               | printable ASCII 1..64자          | DTO 생성                    | wire VPFI가 아니며 등록 key를 정확히 사용                        |
| `StationId`               | 4-byte station 식별자                     | `0..2^32-1` 정수                 | DTO 생성                    | local/addressed station에는 broadcast 예약값 `0` 금지            |
| `Addressed`/`Broadcast`   | 목적지 `Address` 생성                     | station 또는 없음                | `Address`                   | broadcast ACK는 DTO 생성 단계에서 거부                           |

`service.models`의 limits, policy와 transfer record는 Service 내부 model이다. MASS/ROC가 생성하거나
조정하는 공개 요청 DTO가 아니다.

## 4. 입력 API

### 4.1 실제 signature와 결과

```text
start(self, request: ServiceStartRequest) -> ServiceResult[ServiceStarted]
stop(self, request: ServiceStopRequest) -> ServiceResult[ServiceStopped]
submit(self, request: SubmitTransferRequest) -> ServiceResult[TransferAccepted]
cancel(self, request: CancelTransferRequest) -> ServiceResult[CancelAccepted]
status(self, request: TransferStatusRequest) -> ServiceResult[TransferStatusSnapshot]
```

| 함수       | 입력 DTO                | 필수 입력                                  | 즉시 출력                                    | 최종 결과 확인 방법                                             |
| ---------- | ----------------------- | ------------------------------------------ | -------------------------------------------- | --------------------------------------------------------------- |
| `start()`  | `ServiceStartRequest`   | `local_station_id`, `role`                 | `ServiceStarted` 또는 `ServiceError`         | `SERVICE_STARTED`, health event; 동기 성공 자체도 authoritative |
| `submit()` | `SubmitTransferRequest` | `destination`, `payload_format`, `payload` | `TransferAccepted` 또는 `ServiceError`       | `TRANSFER_STATE_CHANGED` 또는 `status()`                        |
| `status()` | `TransferStatusRequest` | `transfer_id`                              | `TransferStatusSnapshot` 또는 `ServiceError` | 반환 snapshot이 조회 시점의 authoritative 상태                  |
| `cancel()` | `CancelTransferRequest` | `transfer_id`                              | `CancelAccepted` 또는 `ServiceError`         | `CANCELLED` transfer event 또는 `status()`                      |
| `stop()`   | `ServiceStopRequest`    | 없음                                       | `ServiceStopped` 또는 `ServiceError`         | `SERVICE_STOPPED`; stop 성공 뒤 실행 record는 제거됨            |

호출 순서는 interface 선언 순서가 아니라 일반적으로 `start → submit/status/cancel → stop`이다.

### 4.2 입력 DTO field

#### `ServiceStartRequest`

| field              | 타입          | 필수 여부       | 의미                      | MASS/ROC가 넣을 값            | 예시                   |
| ------------------ | ------------- | --------------- | ------------------------- | ----------------------------- | ---------------------- |
| `local_station_id` | `StationId`   | 필수            | local endpoint identity   | 배포 config와 같은 nonzero ID | `StationId(111111111)` |
| `role`             | `ServiceRole` | 필수            | Ship 또는 Control         | MASS=`SHIP`, ROC=`CONTROL`    | `ServiceRole.SHIP`     |
| `config_ref`       | `str`         | 선택, 기본 `""` | 외부 config resolver 참조 | 현재는 `""`만 사용            | `""`                   |

현재 non-empty `config_ref`는 resolver가 없으므로 `UNSUPPORTED_REQUEST`다. 요청 station과
role은 endpoint 생성 때 읽은 config와 정확히 일치해야 한다.

#### `SubmitTransferRequest`

| field            | 타입            | 필수 여부          | 의미                               | MASS/ROC가 넣을 값                        | 예시                                       |
| ---------------- | --------------- | ------------------ | ---------------------------------- | ----------------------------------------- | ------------------------------------------ |
| `destination`    | `Address`       | 필수               | 최종 API 목적지 또는 broadcast     | `Addressed(StationId(...))`/`Broadcast()` | `Addressed(ROC_ID)`                        |
| `payload_format` | `PayloadFormat` | 필수               | registry format key                | 현재 지원 key 중 하나                     | `PayloadFormat("x509.certificate_report")` |
| `payload`        | `bytes`         | 필수               | 애플리케이션이 직렬화한 body       | VDE header 없는 완성 bytes                | `certificate_report`                       |
| `ack_required`   | `bool`          | 선택, 기본 `False` | addressed Short Data ACK 요청 정책 | 업무 중복·확인 정책에 따라 지정           | `True`                                     |

송신자는 이 DTO에 넣지 않는다. 실행 중인 endpoint의 configured `local_station_id`가 송신자다.
`transfer_id`는 caller가 만들지 않고 정상 admission 때 VDE-TER이 발급한다. caller가 설정하는
priority, submit timeout, retry count, Link ID, Message Type, channel, slot, relay hop field는 없다.

`ack_required`는 broadcast에서 반드시 `False`다. Data Session 내부 feedback과 최종 업무 확인을
대체하지 않으며, `True`라고 해서 수신 애플리케이션이 업무 처리를 완료했다는 뜻은 아니다.

#### 조회·취소·종료 요청

| DTO                     | field         | 타입         | 필수 여부       | 의미                   | 예시                                 |
| ----------------------- | ------------- | ------------ | --------------- | ---------------------- | ------------------------------------ |
| `TransferStatusRequest` | `transfer_id` | `TransferId` | 필수            | local 송신 record 조회 | `TransferStatusRequest(transfer_id)` |
| `CancelTransferRequest` | `transfer_id` | `TransferId` | 필수            | local 진행 작업 취소   | `CancelTransferRequest(transfer_id)` |
| `ServiceStopRequest`    | `reason`      | `str`        | 선택, 기본 `""` | 운영·log용 종료 사유   | `ServiceStopRequest("shutdown")`     |

수신 측에는 송신 측 `transfer_id`가 전달되지 않는다. 이를 end-to-end correlation ID로 쓰려면
MASS/ROC가 자신의 업무 payload 안에 별도 correlation 값을 직렬화해야 한다.

### 4.3 route 표현

- ROC↔MASS addressed: 상대 station을 `Addressed(...)`로 지정한다.
- MASS↔MASS: 애플리케이션은 최종 MASS만 지정한다. Short Data direct 또는 큰 Data Session의
  ROC 경유 여부는 내부 route 결정이다.
- ROC broadcast: no-ACK Short Data 전송 경로는 구현돼 있다. 다만 현재 등록된 운영 format은 정상
  payload가 Short Data 크기 한도를 넘을 수 있으므로, 실제 사용에는 크기 조건을 만족하는 소형 운영
  format의 registry mapping과 codec이 별도로 필요하다.
- MASS-originated broadcast와 broadcast Data Session은 현재 미지원이다.
- 자기 station을 addressed 목적지로 지정하거나 deployment member 밖의 station을 지정하면 거부된다.

애플리케이션이 ROC를 “relay hop”으로 별도 지정하는 field는 없다. MASS-A가 ROC에 제출한 뒤 ROC
애플리케이션이 MASS-B로 다시 `submit()`하면 이는 두 개의 application transfer이며, 내부 relay와 다르다.

## 5. 출력 API와 event

### 5.1 즉시 반환

`ServiceResult[T]`는 `Ok(value)` 또는 `Err(error)`의 union이다.

```python
result = service.submit(request)
if result.is_err:
    error: ServiceError = result.error
    print(error.code, error.category, error.retryable)
else:
    accepted: TransferAccepted = result.value
    print(accepted.transfer_id, accepted.state)
```

| 성공값                   | field                                                             | 의미                                              |
| ------------------------ | ----------------------------------------------------------------- | ------------------------------------------------- |
| `ServiceStarted`         | `endpoint_id`, `role`, `local_station_id`, `health`, `started_at` | 시작 완료 snapshot                                |
| `TransferAccepted`       | `transfer_id`, `state`, `accepted_at`                             | 요청 admission 결과; 현재 구현 state는 `QUEUED`   |
| `TransferStatusSnapshot` | `transfer_id`, `state`, `updated_at`, `error`                     | 조회 시점 상태; `error`는 `FAILED`일 때만 제공    |
| `CancelAccepted`         | `transfer_id`, `state`, `accepted_at`                             | 취소 처리 접수/commit 결과; 데이터 회수 보장 아님 |
| `ServiceStopped`         | `endpoint_id`, `health`, `stopped_at`                             | 종료 완료 snapshot                                |

`submit()`의 `Ok(TransferAccepted(...))`는 accepted이지 completed가 아니다. 송신 terminal 결과는
event 또는 `status()`로 확인한다.

`ServiceError`의 공개 field는 `code`, `category`, `message`, `retryable`, 선택적 `transfer_id`,
`details`다. 공개 category는 `VALIDATION`, `UNSUPPORTED`, `PROTOCOL`, `TIMEOUT`, `RESOURCE`,
`DEVICE`, `INTERNAL`이다.

### 5.2 비동기 event

`ServiceEvent`의 공개 field는 다음과 같다.

| field            | 타입                                | 설명                                |
| ---------------- | ----------------------------------- | ----------------------------------- |
| `event_id`       | `str`                               | endpoint 수명 안에서 고유한 전달 ID |
| `event_type`     | `ServiceEventType`                  | event 종류                          |
| `occurred_at`    | `TimePoint`                         | 공개 발생 시각                      |
| `severity`       | `EventSeverity`                     | `DEBUG/INFO/WARNING/ERROR/CRITICAL` |
| `transfer_id`    | `TransferId` 또는 `None`            | 송신 상태·관련 오류 correlation     |
| `transfer_state` | `TransferState` 또는 `None`         | transfer event의 새 상태            |
| `received_data`  | `ReceivedApplicationData` 또는 `None` | 수신 application data             |
| `error`          | `ServiceError` 또는 `None`          | 실패 정보                           |
| `details`        | `Details`                           | allowlist 기반 추가 정보            |

| event type                | 발생 시점                                        | 주요 field                                        | MASS/ROC의 처리                                        |
| ------------------------- | ------------------------------------------------ | ------------------------------------------------- | ------------------------------------------------------ |
| `SERVICE_STARTED`         | endpoint가 RUNNING으로 시작됨                    | `occurred_at`, `details["endpoint_id"]`           | 실행 상태 표시; `start()` 성공과 중복 의미로 멱등 처리 |
| `SERVICE_STOPPED`         | 정상 stop commit                                 | `occurred_at`, `details["endpoint_id"]`           | 종료 상태 표시·consumer 정리                           |
| `ENDPOINT_HEALTH_CHANGED` | STARTING/RUNNING/DEGRADED/UNHEALTHY/STOPPED 변화 | `details["previous_health"]`, `details["health"]` | admission 중단, 운영 경보, cleanup 판단                |
| `TRANSFER_STATE_CHANGED`  | 송신 상태 변화                                   | `transfer_id`, `transfer_state`, 선택적 `error`   | local 전송 상태 갱신; terminal이면 waiter 해제         |
| `DATA_RECEIVED`           | 검증·재조립된 application bytes 수신             | `received_data`                                   | format 분기, 역직렬화, 업무 정책 적용                  |
| `ERROR`                   | 특정 transfer state가 없는 RX/endpoint 오류 등   | `error`, 선택적 `transfer_id`                     | 기록·운영 경보·복구 판단                               |

`ReceivedApplicationData`는 `original_source`, `original_destination`, `payload_format`, `payload`,
선택적 `received_at`을 가진다.

- `COMPLETED`는 event type이 아니라 `TRANSFER_STATE_CHANGED.transfer_state` 값이다.
- `DATA_RECEIVED`는 상대 endpoint에 bytes가 전달됐다는 수신 event다.
- `ERROR`는 endpoint/RX 등 transfer terminal state와 분리된 오류 event다.
- transfer-bound 실패는 보통 `TRANSFER_STATE_CHANGED(FAILED, error=...)`로 온다.

모든 중간 nonterminal 상태 event가 빠짐없이 전달된다고 가정하지 않는다. terminal event와 필요 시
`status()`를 함께 사용한다.

## 6. 동기 호출과 비동기 결과

`submit()`은 입력 검증, format encode와 admission을 마치고 요청 접수 결과를 반환한다. 실제 송신은
그 뒤 내부 자원과 TBB, MAC/PHY/Radio 경로에서 진행된다.

```text
submit() → Ok(TransferAccepted(state=QUEUED))
    └─ 나중에 TRANSFER_STATE_CHANGED(...)
          ├─ COMPLETED
          ├─ FAILED + ServiceError
          └─ CANCELLED

상대 endpoint
    └─ 별도로 DATA_RECEIVED(received_data.payload)
```

`COMPLETED`는 VDE-TER 전송 절차의 terminal 결과이지 수신 업무 로직의 성공이 아니다. 특히 현재
큰 MASS→MASS 내부 relay에서는 최초 MASS의 `COMPLETED`가 ROC의 relay 수락을 뜻할 수
있으며 최종 MASS의 `DATA_RECEIVED`와 동일 시점이 아니다. end-to-end 업무 확인이 필요하면 payload
안에 업무 ID를 넣고 수신 애플리케이션이 별도 응답을 보내야 한다.

Service queue에는 caller 지정 waiting timeout field가 없다. 애플리케이션 deadline을 별도로 관리하고,
deadline에서 `status()`를 조회한 뒤 필요하면 `cancel()`한다. MAC/PHY 내부 deadline 초과는
`TRANSFER_TIMEOUT` terminal 오류로 projection된다.

callback, queue, stream, RPC 또는 같은 프로세스 여부는 event 전달 배치다. 이 선택은
`VdeService` 요청·결과와 `ServiceEvent`의 의미를 바꾸지 않는다.

## 7. Event Sink와 handler 연결

같은 프로세스에서 callback binding을 사용할 때 애플리케이션은 `ServiceEvent` 하나를 받는 handler를
제공한다. launcher는 공개 helper인 `HandlerServiceEventSink`로 이 handler를 endpoint에 연결한다.
별도 프로세스 배치에서는 동일한 `ServiceEvent` 의미를 전달하는 event consumer가 이 역할을 대신할 수
있다. 흐름은 다음과 같다.

```text
VDE-TER service
    → service_event_sink.publish(event)
        → handle_service_event(event)
```

`HandlerServiceEventSink`는 별도 queue, worker 또는 thread를 만들지 않는다. event를 받은 호출
흐름에서 handler를 즉시 실행한다. 짧은 처리는 handler에서 바로 해도 된다. PKI 검증, DB, 파일 I/O나
network 호출처럼 오래 걸리는 처리가 있을 때만 애플리케이션 queue/worker로 넘긴다. 같은 `event_id`를
다시 받아도 부작용이 중복되지 않도록 handler는 멱등하게 작성한다. 최소 구현은 §12.1에 있다.

종료 순서는 새 업무 submit 중단 → 진행 작업 정리/취소 → `service.stop()` → lifecycle event 처리
→ queue/worker를 사용한 경우 해당 consumer 종료다.

## 8. Payload 준비 책임

MASS/ROC는 업무 데이터를 먼저 `bytes`로 직렬화한다. VDE-TER은 registry의 format codec을 적용해
protocol로 전달하고, 수신 시 원래 application bytes를 `DATA_RECEIVED`로 복원한다. 수신 애플리케이션은
`received_data.payload_format`으로 분기하고 `received_data.payload`를 역직렬화한다.

| `payload_format.value`                 | application `payload`                           | scope·현재 상태                   |
| -------------------------------------- | ----------------------------------------------- | --------------------------------- |
| `x509.certificate_report`              | R1027 Certificate Report body                   | 운영 지원                         |
| `ais.authentication_integrity.scheme1` | R1027 Ed.1.1 Table 21 Scheme 1, 정확히 82 bytes | 운영 지원                         |
| `electronic_chart.data`                | 미정                                            | key만 예약, 현재 미지원           |

JSON, protobuf, CBOR, route guidance 또는 chart-like bytes를 임의의 지원 format key 아래에 넣으면
안 된다. 해당 application format을 운영에서 쓰려면 registry mapping, 공식/승인된 wire 식별자,
codec이 먼저 제공돼야 한다.

X.509 Certificate Report body는 application이 entry count, 6-byte issuer fingerprint,
certificate type, big-endian DER length와 DER object를 구성한다. VDE-TER codec의 구조 검증은 trust
판정이 아니다. root certificate 비전송, chain 선택과 신뢰 판정은 애플리케이션 책임이다.

AIS Scheme 1도 VDE-TER은 82-byte 형식과 transport를 다루며, 실제 AIS 원문·서명·freshness·replay
판정은 애플리케이션 책임이다.

## 9. 오류 처리

### 9.1 공개 오류와 권장 대응

| code/category                        | 대표 원인                                           | `retryable` 원칙 | MASS/ROC 대응                                         |
| ------------------------------------ | --------------------------------------------------- | ---------------- | ----------------------------------------------------- |
| `INVALID_REQUEST/VALIDATION`         | DTO, destination, 빈/큰/잘못된 payload 구조         | false            | 입력을 수정; 동일 요청 반복 금지                      |
| `UNSUPPORTED_REQUEST/UNSUPPORTED`    | 미등록 format, route, profile, non-empty config ref | false            | 지원 계약 확인·기능 변경 요청                         |
| `SERVICE_NOT_STARTED/VALIDATION`     | start 전 submit/status/cancel/stop 경쟁             | false            | lifecycle 순서 수정                                   |
| `SERVICE_ALREADY_STARTED/VALIDATION` | 중복 start                                          | false            | 기존 실행 재사용                                      |
| `RESOURCE_UNAVAILABLE/RESOURCE`      | 동시 transfer/event/radio 자원 포화                 | true             | backoff 후 새 요청 고려; 먼저 현재 상태 확인          |
| `UNKNOWN_TRANSFER/VALIDATION`        | 잘못되거나 만료된 ID                                | false            | local 상태 정리; 원 요청을 찾지 못하면 운영 기록 확인 |
| `TRANSFER_FAILED/PROTOCOL`           | Presentation/Link/MAC/PHY terminal 실패             | 원인별           | event의 ID로 업무 상태 실패 처리; 무조건 재전송 금지  |
| `TRANSFER_TIMEOUT/TIMEOUT`           | ACK, 자원 응답, PHY deadline                        | true             | 중복 가능성을 판단하고 새 submit 고려                 |
| `DEVICE_ERROR/DEVICE`                | configure, stream, disconnect, reference, stop 오류 | 원인별           | health 확인·운영자 보고; 장치 복구 뒤 재시도          |
| `INTERNAL_ERROR/INTERNAL`            | invariant, unmapped/programming 오류                | 보통 false       | endpoint 격리·운영자 보고·진단 evidence 보존          |

`retryable=True`는 VDE-TER이 동일 요청을 자동 재실행한다는 뜻이 아니다. 원인이 해소된 뒤 caller가
새 `submit()`을 고려할 수 있다는 hint다. 새 submit은 새 `transfer_id`를 만들며, 이전 bytes가 이미
전달됐을 가능성은 업무 ID와 수신 멱등성으로 처리한다.

### 9.2 동기 오류와 비동기 오류

- admission 전 validation/unsupported/resource 오류: API가 즉시 `Err(ServiceError)` 반환
- 접수 뒤 transfer 실패: `TRANSFER_STATE_CHANGED`의 `FAILED`와 `error`
- 특정 local transfer가 없는 RX/endpoint 오류: `ERROR` event, `transfer_id=None` 가능
- cancel 성공: `CancelAccepted.state=CANCELLED`와 terminal event
- 이미 terminal인 transfer cancel: `INVALID_REQUEST`
- stop의 Radio timeout: `DEVICE_ERROR`, retryable; `SERVICE_STOPPED`가 아직 없을 수 있음

오류의 `message`는 사람용 안정 문구다. 분기는 `code`, `category`, `retryable`로 하고, private
Link/Radio code를 추측하지 않는다.

## 10. 실행 및 종료 순서

MASS와 ROC의 공개 호출 순서는 같다. station/role과 업무 handler만 다르다.

1. 애플리케이션이 짧은 event handler를 준비한다. 오래 걸리는 작업이 있을 때만 queue/worker를 둔다.
2. 같은 프로세스 callback binding에서는 handler로 `HandlerServiceEventSink`를 만든다.
3. launcher가 sink를 endpoint에 연결하고 완성된 `VdeService`를 애플리케이션에 제공한다.
4. launcher가 정한 lifecycle admission 시점에 MASS는 `ServiceRole.SHIP`, ROC는
   `ServiceRole.CONTROL`로 각자 `start()`한다. 다중 endpoint의 시작 순서·동시성·rollback은
   `VdeService` 공개 계약이 아니라 launcher/integration 정책이다.
5. 송신자는 `submit()`하고, 수신자는 `DATA_RECEIVED`를 처리한다.
6. 필요할 때 송신자가 `status()`를 조회하고 업무 deadline에서 `cancel()`한다.
7. 새 submit을 중단하고 각 애플리케이션이 자기 `service.stop()`을 호출한다. group lifecycle을 소유한
   launcher가 있으면 launcher의 승인된 coordinated stop 절차를 우선한다.

## 11. MASS와 ROC의 역할 차이

| 관점             | MASS                                          | ROC                                                       |
| ---------------- | --------------------------------------------- | --------------------------------------------------------- |
| 시작 role        | `ServiceRole.SHIP`                            | `ServiceRole.CONTROL`                                     |
| station identity | 자기 MASS config ID                           | Control Station config ID                                 |
| addressed 송신   | ROC 또는 다른 deployment MASS                 | deployment MASS                                           |
| 수신             | `DATA_RECEIVED`에서 업무 data 처리            | `DATA_RECEIVED`에서 MASS data 처리                        |
| broadcast        | origin 미지원                                 | no-ACK Short Data만 제한 지원                             |
| relay            | 목적지 MASS만 지정; relay hop을 조작하지 않음 | protocol relay는 내부 책임; 업무 재전송은 별도 `submit()` |

TBB, 자원할당, channel, Link ID와 direct/relay protocol 선택은 public request field가 아니다.
ROC 애플리케이션이 받은 bytes를 정책 검증 후 MASS-B로 새로 제출할 수는 있지만, 이는 새로운 ROC
origin transfer다. MASS-B의 공개 `original_source`는 ROC가 된다.

내부 MASS→MASS Data Session relay에서는 애플리케이션이 최종 MASS를 직접 목적지로
지정한다. 현재 wire 제약 때문에 최종 MASS의 공개 `original_source`도 relay hop의 ROC로 보일 수
있다. end-to-end 최초 source 보존은 보호된 relay correlation evidence에만 있으며 공개 API가 보장하지
않는다. 업무적으로 최초 MASS를 알아야 하면 payload 내부에 서명·검증 가능한 업무 source를 넣는다.

## 12. 최소 코드 예제

### 12.1 같은 프로세스 callback handler와 sink

같은 프로세스 callback binding에서는 애플리케이션이 event handler의 업무 처리 부분을 구현한다.
`HandlerServiceEventSink` 자체와 VDE-TER의 event 생성·전달은 공개 API가 제공한다.

```python
from vdeter.common.events import ServiceEvent, ServiceEventType
from vdeter.service import HandlerServiceEventSink


def handle_service_event(event: ServiceEvent) -> None:
    if event.event_type is ServiceEventType.DATA_RECEIVED:
        received = event.received_data
        if received is None:
            raise ValueError("DATA_RECEIVED event has no received_data")
        print(received.original_source, received.payload_format, len(received.payload))
    elif event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED:
        print(event.transfer_id, event.transfer_state, event.error)
    elif event.event_type is ServiceEventType.ERROR:
        if event.error is None:
            raise ValueError("ERROR event has no error")
        print(event.error)


service_event_sink = HandlerServiceEventSink(handle_service_event)
```

handler는 event 전달 흐름에서 즉시 실행된다. 처리가 짧으면 위 코드만으로 충분하다. PKI 검증,
DB 저장처럼 오래 걸리는 작업이 있을 때만 애플리케이션 queue나 worker를 추가한다.

### 12.2 start, submit, status, cancel, stop

`service`는 launcher가 위 sink와 연결해 제공한 `VdeService`다.
아래 `app_*` 함수는 다섯 공개 메서드의 호출 방법을 보여주는 애플리케이션 예제 helper이며 VDE-TER이
제공하는 API 함수가 아니다. 실제 공개 API 호출은 각 함수 안의 `service.start()`, `submit()`,
`status()`, `cancel()`, `stop()`이다.

```python
from __future__ import annotations

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    CancelTransferRequest,
    ServiceRole,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferStatusRequest,
)
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.service import VdeService


def app_start_mass(
    service: VdeService,
    local_mass_id: StationId,
) -> None:
    started = service.start(
        ServiceStartRequest(local_mass_id, ServiceRole.SHIP, config_ref="")
    )
    if started.is_err:
        raise RuntimeError(started.error.message)


def app_submit_certificate_report(
    service: VdeService,
    roc_id: StationId,
    certificate_report: bytes,
) -> TransferId:
    submitted = service.submit(
        SubmitTransferRequest(
            destination=Addressed(roc_id),
            payload_format=PayloadFormat("x509.certificate_report"),
            payload=certificate_report,
            ack_required=True,
        )
    )
    if submitted.is_err:
        raise RuntimeError(submitted.error.message)
    print("accepted:", submitted.value.transfer_id, submitted.value.state)
    return submitted.value.transfer_id


def app_print_transfer_status(service: VdeService, transfer_id: TransferId) -> None:
    snapshot = service.status(TransferStatusRequest(transfer_id))
    if snapshot.is_err:
        raise RuntimeError(snapshot.error.message)
    print("current:", snapshot.value.state)


def app_cancel_transfer(service: VdeService, transfer_id: TransferId) -> None:
    cancelled = service.cancel(CancelTransferRequest(transfer_id))
    if cancelled.is_err:
        raise RuntimeError(cancelled.error.message)


def app_stop_service(service: VdeService) -> None:
    stopped = service.stop(ServiceStopRequest(reason="application shutdown"))
    if stopped.is_err:
        raise RuntimeError(stopped.error.message)
```

애플리케이션 시작 때 `app_start_mass()`를 한 번 호출하고, 실행 중에는 필요한 만큼
`app_submit_certificate_report()`를 호출한다. `submit()`의 반환은 접수 결과다. 실제 terminal 상태와 수신
bytes는 §12.1의 handler에서 처리하며 필요할 때 `app_print_transfer_status()`로 보완한다. 업무 deadline이
만료된 경우에만 `app_cancel_transfer()`를 호출한다. 모든 필요한 transfer가 terminal이 되고 새 submit을
막은 뒤 애플리케이션 종료 때 `app_stop_service()`를 호출한다. 취소는 이미 전송된 data의 회수를 보장하지
않는다.

endpoint 조립은 애플리케이션 API 범위 밖이다. launcher가 sink를 endpoint에 연결하고 완성된
`VdeService`를 애플리케이션에 제공한다.

```text
launcher: ServiceEventSink 연결 → VdeService 제공
application: 주입된 VdeService 호출 → handler로 event 처리
```

## 13. 서비스 주입 이후 전체 시나리오 예제

### 13.1 MASS-A → ROC 업무 검증 → ROC → MASS-B

이 시나리오는 두 개의 명시적 application transfer다. MASS-A가 ROC에 Certificate Report를 보내고,
ROC 애플리케이션이 허용된 SHA-256인지 확인한 뒤 같은 bytes를 MASS-B에 새로 제출한다. MASS-B는
전체 bytes와 SHA-256을 확인한다. 이 정책 검증은 VDE-TER 기능이 아니다.

아래 코드는 launcher가 세 `VdeService`와 세 event binding을 먼저 조립·주입한 뒤 실행하는
**서비스 주입 이후의 애플리케이션 시나리오 코드**다. endpoint/B210/config 조립은 MASS/ROC
애플리케이션 공개 API가 아니므로 포함하지 않는다. 서로 다른 프로세스에서는 launcher의 lifecycle
admission에 따라 각 프로세스가 자기 `start()`를 호출한다. 아래 동시 시작 helper는 한 프로세스에서 세
endpoint를 함께 시연하기 위한 예제일 뿐, 공개 API가 시작 순서나 동시성을 보장한다는 뜻이 아니다.

아래 코드에서 VDE-TER이 제공하는 것은 `VdeService`, 요청 DTO, `ServiceEvent`와
`HandlerServiceEventSink`뿐이다. `ScenarioEventInbox`, `ScenarioEventBinding`, `build_*`,
`wait_*`, `require_ok()`와 `run_certificate_forwarding()`은 모두 이 시나리오를 위한 애플리케이션
예제 코드다. queue는 timeout을 두고 여러 endpoint의 event를 기다리기 위한 선택적 구현이며 API 필수
요소가 아니다. handler는 queue에 넣기만 하므로 callback 안에서 PKI/submit을 실행하지 않는다.

```python
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from queue import Empty, Full, Queue
from time import monotonic

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import (
    CancelTransferRequest,
    ServiceRole,
    ServiceStartRequest,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.events import ServiceEvent, ServiceEventType
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.service import HandlerServiceEventSink, VdeService


MASS_A_ID = StationId(111111111)
ROC_ID = StationId(222222222)
MASS_B_ID = StationId(333333333)
CERTIFICATE_FORMAT = PayloadFormat("x509.certificate_report")
MAX_SINGLE_CERTIFICATE_DER_BYTES = 65_521
TERMINAL_STATES = {
    TransferState.COMPLETED,
    TransferState.FAILED,
    TransferState.CANCELLED,
}


class ScenarioEventInbox:
    def __init__(self, capacity: int = 256) -> None:
        self._queue: Queue[ServiceEvent] = Queue(maxsize=capacity)

    def handler(self, event: ServiceEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except Full as exc:
            raise RuntimeError("scenario event inbox is full") from exc

    def get(self, timeout_seconds: float) -> ServiceEvent:
        try:
            return self._queue.get(timeout=timeout_seconds)
        except Empty as exc:
            raise TimeoutError("Service event wait timed out") from exc


@dataclass(frozen=True)
class ScenarioEventBinding:
    inbox: ScenarioEventInbox
    sink: HandlerServiceEventSink


def build_scenario_event_binding() -> ScenarioEventBinding:
    inbox = ScenarioEventInbox()
    return ScenarioEventBinding(
        inbox=inbox,
        sink=HandlerServiceEventSink(inbox.handler),
    )


def build_certificate_report(
    issuer_fingerprint: bytes,
    certificate_der: bytes,
) -> bytes:
    """65,531-byte application 상한 안에서 단일 certificate entry를 만든다."""
    if not isinstance(issuer_fingerprint, bytes) or len(issuer_fingerprint) != 6:
        raise ValueError("issuer_fingerprint must be exactly 6 bytes")
    if not isinstance(certificate_der, bytes):
        raise TypeError("certificate_der must be bytes")
    if not 1 <= len(certificate_der) <= MAX_SINGLE_CERTIFICATE_DER_BYTES:
        raise ValueError("certificate_der length must be 1..65521")
    return b"".join(
        (
            b"\x01",
            issuer_fingerprint,
            b"\x00",
            len(certificate_der).to_bytes(2, "big"),
            certificate_der,
        )
    )


def require_ok(result, operation: str):
    if result.is_err:
        error = result.error
        raise RuntimeError(
            f"{operation}: {error.code}/{error.category.value}: {error.message}"
        )
    return result.value


def start_services_for_scenario(
    endpoints: tuple[tuple[VdeService, ServiceStartRequest, str], ...],
) -> list[tuple[VdeService, str]]:
    """단일 프로세스 시연에서 세 endpoint의 start를 동시에 호출한다."""
    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        pending = [
            (service, name, executor.submit(service.start, request))
            for service, request, name in endpoints
        ]
        # 모든 start를 먼저 제출한 뒤 결과를 확인해야 첫 호출에서 장벽 대기하지 않는다.
        results = [
            (service, name, future.result())
            for service, name, future in pending
        ]

    started = [
        (service, name)
        for service, name, result in results
        if not result.is_err
    ]
    for _service, name, result in results:
        if result.is_err:
            # 일부 endpoint만 시작된 adapter에서도 실행 상태가 남지 않게 정리한다.
            stop_services_for_scenario(started)
            require_ok(result, f"{name} start")
    return started


def stop_services_for_scenario(
    endpoints: list[tuple[VdeService, str]],
) -> None:
    """별도 group-stop owner가 없는 단일 프로세스 시연용 정리 함수다."""
    if not endpoints:
        return
    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        pending = [
            (
                name,
                executor.submit(
                    service.stop,
                    ServiceStopRequest(reason="scenario cleanup"),
                ),
            )
            for service, name in endpoints
        ]
        for name, future in pending:
            stopped = future.result()
            if stopped.is_err:
                print(f"warning: {name} stop failed: {stopped.error}")


def wait_for_received(
    inbox: ScenarioEventInbox,
    expected_source: StationId,
    expected_payload: bytes,
    deadline: float,
) -> bytes:
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("DATA_RECEIVED wait timed out")
        event: ServiceEvent = inbox.get(remaining)
        if event.event_type is ServiceEventType.ERROR:
            if event.error is None:
                raise ValueError("ERROR event has no error")
            raise RuntimeError(f"asynchronous endpoint error: {event.error}")
        if event.event_type is not ServiceEventType.DATA_RECEIVED:
            continue
        received = event.received_data
        if received is None:
            raise ValueError("DATA_RECEIVED event has no received_data")
        if received.payload_format != CERTIFICATE_FORMAT:
            continue
        if received.original_source != expected_source:
            raise RuntimeError("unexpected original_source")
        if received.payload != expected_payload:
            raise RuntimeError("received bytes differ from submitted bytes")
        return received.payload


def wait_for_terminal(
    service: VdeService,
    inbox: ScenarioEventInbox,
    transfer_id: TransferId,
    deadline: float,
) -> TransferState:
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            snapshot = require_ok(
                service.status(TransferStatusRequest(transfer_id)),
                "status after event timeout",
            )
            if snapshot.state not in TERMINAL_STATES:
                require_ok(
                    service.cancel(CancelTransferRequest(transfer_id)),
                    "cancel after timeout",
                )
                raise TimeoutError("transfer did not reach a terminal state")
            return snapshot.state

        event = inbox.get(remaining)
        if event.event_type is ServiceEventType.ERROR:
            if event.error is None:
                raise ValueError("ERROR event has no error")
            raise RuntimeError(f"asynchronous endpoint error: {event.error}")
        if (
            event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
            and event.transfer_id == transfer_id
            and event.transfer_state in TERMINAL_STATES
        ):
            if event.transfer_state is TransferState.FAILED:
                if event.error is None:
                    raise ValueError("FAILED transfer event has no error")
                raise RuntimeError(f"transfer failed: {event.error}")
            return event.transfer_state


def run_certificate_forwarding(
    mass_a_service: VdeService,
    roc_service: VdeService,
    mass_b_service: VdeService,
    mass_a_inbox: ScenarioEventInbox,
    roc_inbox: ScenarioEventInbox,
    mass_b_inbox: ScenarioEventInbox,
    issuer_fingerprint: bytes,
    certificate_der: bytes,
    allowed_report_sha256: str,
    timeout_seconds: float = 90.0,
) -> None:
    report = build_certificate_report(issuer_fingerprint, certificate_der)
    report_digest = sha256(report).hexdigest()

    started: list[tuple[VdeService, str]] = []
    try:
        started = start_services_for_scenario(
            (
                (
                    mass_a_service,
                    ServiceStartRequest(MASS_A_ID, ServiceRole.SHIP, ""),
                    "MASS-A",
                ),
                (
                    mass_b_service,
                    ServiceStartRequest(MASS_B_ID, ServiceRole.SHIP, ""),
                    "MASS-B",
                ),
                (
                    roc_service,
                    ServiceStartRequest(ROC_ID, ServiceRole.CONTROL, ""),
                    "ROC",
                ),
            )
        )

        mass_a_accepted = require_ok(
            mass_a_service.submit(
                SubmitTransferRequest(
                    destination=Addressed(ROC_ID),
                    payload_format=CERTIFICATE_FORMAT,
                    payload=report,
                    ack_required=True,
                )
            ),
            "MASS-A submit",
        )
        print("MASS-A accepted:", mass_a_accepted.transfer_id)

        roc_payload = wait_for_received(
            roc_inbox,
            expected_source=MASS_A_ID,
            expected_payload=report,
            deadline=monotonic() + timeout_seconds,
        )
        # 실제 환경에서는 이 위치에서 ROC의 PKI/trust 업무 정책을 수행한다.
        if sha256(roc_payload).hexdigest() != allowed_report_sha256:
            raise RuntimeError("ROC application policy rejected the report")

        roc_accepted = require_ok(
            roc_service.submit(
                SubmitTransferRequest(
                    destination=Addressed(MASS_B_ID),
                    payload_format=CERTIFICATE_FORMAT,
                    payload=roc_payload,
                    ack_required=True,
                )
            ),
            "ROC submit",
        )
        print("ROC accepted:", roc_accepted.transfer_id)

        mass_b_payload = wait_for_received(
            mass_b_inbox,
            expected_source=ROC_ID,
            expected_payload=report,
            deadline=monotonic() + timeout_seconds,
        )
        if sha256(mass_b_payload).hexdigest() != allowed_report_sha256:
            raise RuntimeError("MASS-B SHA-256 mismatch")

        first_state = wait_for_terminal(
            mass_a_service,
            mass_a_inbox,
            mass_a_accepted.transfer_id,
            monotonic() + timeout_seconds,
        )
        second_state = wait_for_terminal(
            roc_service,
            roc_inbox,
            roc_accepted.transfer_id,
            monotonic() + timeout_seconds,
        )
        if first_state is not TransferState.COMPLETED:
            raise RuntimeError(f"MASS-A terminal state: {first_state.value}")
        if second_state is not TransferState.COMPLETED:
            raise RuntimeError(f"ROC terminal state: {second_state.value}")

        print("PASS:", report_digest, len(report), "bytes")
    finally:
        # 실제 배치에서 group launcher가 lifecycle을 소유하면 그 coordinated stop을 사용한다.
        stop_services_for_scenario(started)
```

코드 경로는 다음처럼 드러난다.

```text
MASS-A application
→ MASS-A VdeService.submit()
→ ROC DATA_RECEIVED
→ ROC application policy
→ ROC VdeService.submit()
→ MASS-B DATA_RECEIVED
→ MASS-B application
```

세 handler/inbox/sink는 endpoint를 조립하기 전에 예제의 `build_scenario_event_binding()`으로 각각 만든다.
launcher는 각 `ScenarioEventBinding.sink`를 같은 이름의 endpoint에 주입하고, 완성된 `VdeService`와
각 `ScenarioEventBinding.inbox`를 `run_certificate_forwarding()`에 전달한다. 이 binding class와
builder도 애플리케이션 시나리오 코드이며 VDE-TER 공개 API가 아니다. MASS/ROC 개발자가
작성하는 애플리케이션 코드는 주입받은 service와 inbox만 사용하며 endpoint builder, Coordinator,
Link/MAC/PHY/Radio를 직접 호출하지 않는다. 현재 공개 endpoint factory가 없으므로 launcher 조립 코드를
임의 API로 만들어 예제에 넣지 않았다. 따라서 이 절은 **주입 이후에는 실행 가능한 애플리케이션
시나리오**이며 독립 실행형 B210 launcher 예제가 아니다. `certificate_der`는 실제 X.509 v3 DER이어야
하며 예제의 SHA-256 비교는 trust/chain 검증을 대신하지 않는다.

## 14. 추가 시나리오 예제

이 절의 짧은 코드 블록은 애플리케이션 entrypoint 안에 들어가는 호출 조각이다. `mass_service`,
`roc_service`, station ID와 payload bytes는 launcher 또는 업무 코드가 제공하는 입력이며 VDE-TER이
생성하는 전역 변수가 아니다. 공개 API는 각 조각의 `service.submit/status/cancel`과 요청 DTO다.

### 14.1 MASS → ROC AIS 인증·무결성 bytes

```python
assert isinstance(ais_scheme1_body, bytes)
assert len(ais_scheme1_body) == 82

result = mass_service.submit(
    SubmitTransferRequest(
        destination=Addressed(roc_id),
        payload_format=PayloadFormat("ais.authentication_integrity.scheme1"),
        payload=ais_scheme1_body,
        ack_required=True,
    )
)
```

여기서 `mass_service`, `roc_id`, `ais_scheme1_body`는 애플리케이션 entrypoint의 실제 입력이다.
ROC는 `DATA_RECEIVED` 뒤 AIS 원문과 서명을 자체 정책으로 검증한다. `COMPLETED`는 AIS 인증 성공이 아니다.

### 14.2 ROC → MASS 항해 경고 또는 route guidance

현재 운영 registry에는 항해 경고나 route guidance format이 없다. 다음 요청 모양은 public API에는
맞지만 현재 구현에서는 `UNSUPPORTED_REQUEST`가 정상 결과다.

```python
result = roc_service.submit(
    SubmitTransferRequest(
        destination=Addressed(mass_id),
        payload_format=PayloadFormat("route.guidance"),
        payload=route_guidance_bytes,
        ack_required=True,
    )
)
assert result.is_err
assert result.error.code == "UNSUPPORTED_REQUEST"
```

운영 지원을 추가하려면 임의 key만 쓰지 말고 registry mapping, wire 식별자와 codec을 먼저 제공해야 한다.

### 14.3 MASS ↔ MASS direct payload

애플리케이션은 direct 또는 relay를 직접 선택하지 않고 최종 MASS를 `Addressed(...)`로 지정한다.
Service가 등록 format의 encoded 크기에 따라 가능한 route를 결정한다. 현재 AIS 82-byte payload와 일반적인
X.509 Certificate Report는 Short Data direct 범위를 넘으므로 Data Session으로 처리된다. 작은 direct
업무 데이터를 추가하려면 먼저 그 업무 format의 registry mapping과 codec을 제공해야 하며, 지원되지 않는
임의 format key로 우회해서는 안 된다.

큰 MASS→MASS payload는 Service가 내부 ROC relay route를 고른다. 애플리케이션은 relay hop이나
Link ID를 지정하지 않는다.

### 14.4 애플리케이션 timeout과 cancel

```python
from time import monotonic, sleep

from vdeter.common.contracts.service import (
    CancelTransferRequest,
    TransferState,
    TransferStatusRequest,
)
from vdeter.common.identifiers import TransferId
from vdeter.service import VdeService


def wait_for_terminal_or_cancel(
    service: VdeService,
    transfer_id: TransferId,
    timeout_seconds: float = 30.0,
) -> TransferState:
    terminal_states = {
        TransferState.COMPLETED,
        TransferState.FAILED,
        TransferState.CANCELLED,
    }
    deadline = monotonic() + timeout_seconds

    while monotonic() < deadline:
        snapshot = service.status(TransferStatusRequest(transfer_id))
        if snapshot.is_err:
            raise RuntimeError(snapshot.error.message)
        if snapshot.value.state in terminal_states:
            return snapshot.value.state
        sleep(0.2)

    cancelled = service.cancel(CancelTransferRequest(transfer_id))
    if cancelled.is_ok:
        return cancelled.value.state

    # 완료와 cancel이 경쟁했을 수 있으므로 마지막 status를 다시 확인한다.
    final_snapshot = service.status(TransferStatusRequest(transfer_id))
    if final_snapshot.is_err:
        raise RuntimeError(final_snapshot.error.message)
    if final_snapshot.value.state in terminal_states:
        return final_snapshot.value.state
    raise RuntimeError(cancelled.error.message)
```

이 timeout은 애플리케이션 policy다. Service request에 timeout field는 없고, cancel은 이미 상대에게
전달된 bytes를 회수하지 않는다.

## 15. 프로세스 배치와 API 독립성

같은 프로세스에서는 주입받은 Python `VdeService`를 직접 호출하고
`HandlerServiceEventSink`로 callback을 연결할 수 있다. 별도 프로세스 배치에서는 adapter가 같은 요청
DTO 의미를 RPC/IPC message로 변환하고 event stream을 반대로 전달할 수 있다.

RPC, REST, WebSocket, queue와 socket schema는 VDE-TER 공개 Service API 자체가 아니다. 배치가
바뀌어도 다음 의미 계약은 유지돼야 한다.

- start/submit/status/cancel/stop의 요청과 즉시 `ServiceResult`
- accepted와 completed의 분리
- `ServiceEvent`의 상태·수신·오류 의미
- application payload bytes와 공개 주소/format
- private protocol/장치 field 비노출

## 16. 개발자 체크리스트

- [ ] MASS=`SHIP`, ROC=`CONTROL`과 실제 station ID가 config와 일치한다.
- [ ] 목적지는 `Addressed` 또는 지원되는 ROC `Broadcast`로 만들었다.
- [ ] relay hop이 아니라 최종 API 목적지를 지정했다.
- [ ] 업무 데이터를 현재 등록된 `payload_format`의 정확한 `bytes`로 만들었다.
- [ ] broadcast에서 ACK를 요청하지 않았다.
- [ ] handler/sink가 endpoint 조립 때 실제로 연결됐다.
- [ ] `TransferAccepted`와 `COMPLETED`를 구분한다.
- [ ] `DATA_RECEIVED`에서 format/source/bytes를 확인하고 역직렬화한다.
- [ ] handler는 짧고 멱등하며 긴 작업을 worker로 넘긴다.
- [ ] `error.retryable`과 업무 중복 가능성을 함께 판단한다.
- [ ] application deadline에서 status/cancel 경쟁을 처리한다.
- [ ] 새 submit을 닫은 뒤 service stop과 event consumer cleanup을 순서대로 수행한다.

## 17. 현재 지원 상태와 제한

| 기능                                        | 현재 코드 상태               | MASS/ROC 사용 가능 여부    | 비고                                                     |
| ------------------------------------------- | ---------------------------- | -------------------------- | -------------------------------------------------------- |
| `VdeService` 다섯 호출                      | 구현                         | 가능                       | 실제 endpoint 객체는 launcher 주입 필요                  |
| `HandlerServiceEventSink`                   | 공개 adapter 구현            | 가능                       | 자동 queue/thread 없음                                   |
| X.509 Certificate Report                    | 운영 registry/codec 구현     | 가능                       | trust/chain은 앱 책임                                    |
| AIS Scheme 1 82 bytes                       | 운영 registry/codec 구현     | 가능                       | 인증 판정은 앱 책임                                      |
| 전자해도                                    | registry/codec 없음          | 불가                       | `electronic_chart.data` 예약 key뿐                       |
| route guidance/항해 경고/일반 JSON·protobuf | registry mapping 없음        | 불가                       | registry/codec 추가 필요                                 |
| ROC addressed 송수신                        | 구현                         | 가능                       | configured member만                                      |
| ROC no-ACK broadcast Short Data             | 제한 구현                    | format/크기 조건부 가능    | MASS broadcast 미지원                                    |
| MASS↔MASS route                             | Service가 자동 결정          | 가능                       | 애플리케이션은 direct/relay를 강제하지 않음               |
| 큰 MASS↔MASS 내부 ROC relay                 | 구현                         | 가능                       | 최초 submit 완료와 최종 수신 분리; 최종 공개 source 제약 |
| caller priority/timeout/retry count         | request field 없음           | 직접 지정 불가             | application deadline과 내부 policy 분리                  |
| same-process Python API                     | 구현                         | 가능                       | 현재 기본 사용 방식                                      |
| 표준화된 RPC/IPC binding                    | 공개 API에서 미정            | 별도 adapter 필요          | 의미 계약은 유지                                         |

주요 한계는 application payload 1..65,531 bytes, endpoint당 nonterminal transfer 최대 8,
terminal record 15분/최대 1,024개, VDE-TER 내부 pending Service event 최대 256개다. 마지막 값은
§13의 `ScenarioEventInbox` 용량과 별개의 내부 정책이다. caller는 이 정책 값을 request에 넣거나
상향할 수 없다.
