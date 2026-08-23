# MASS/ROC 연동용 VDE-TER Service API

## 1. 적용 시나리오

최종 시스템에는 두 MASS와 하나의 ROC가 존재한다.

```text
MASS-A application <-> local VDE-TER Service A ─┐
MASS-B application <-> local VDE-TER Service B ─┼─ VDE-TER 통신 경로
ROC application    <-> local VDE-TER Service R ─┘
```

각 노드는 TBB 데이터를 송수신하고, 필요에 따라 다음 application data를 교환한다.

- `TBB`
- `AIS_AUTH_INTEGRITY`: AIS 인증 또는 무결성 값
- `ELECTRONIC_CHART`: 전자해도 데이터
- `X509_CERTIFICATE`: X.509 인증서

네 종류는 별도 전송 함수가 아니라 `SendRequest.data_type` 값으로 구분한다. VDE-TER는
그 내용의 업무적 의미를 해석하거나 인증서를 검증하지 않고 application payload를
안전하게 운반한다. 인증서 검증, 전자해도 적용 및 AIS 인증 결과의 업무 처리는 MASS/ROC
application의 책임이다.

## 2. 공개 API

공개 API는 다음 여덟 개의 논리 기능만 제공한다.

```text
open(config)
close()

send(request) -> transfer_id
cancel(transfer_id)
get_status(transfer_id) -> TransferStatus

on_message(handler)
on_status(handler)
on_error(handler)
```

이름과 데이터 의미가 공개 계약이다. 같은 프로세스의 함수 호출, 별도 프로세스 RPC,
socket 또는 message queue 중 무엇을 사용하는지는 구현 세부사항이며 이 계약에 포함하지
않는다.

### 2.1 연결과 종료

#### `open(config)`

VDE-TER Service를 시작하고 사용할 local source ID를 등록한다.
입력 형식은 §3.1.1의 `VdeConfig`를 따른다.

일반적인 배치는 노드마다 하나의 source ID와 하나의 Service 인스턴스를 사용한다.
Link ID, Message Type, FEC, CRC 및 무선 파라미터는 공개 `config`에 노출하지 않고
VDE-TER의 승인된 profile에서 관리한다.

#### `close()`

새 송신 요청의 접수를 중단하고 Service가 사용한 자원을 정리한다. 종료 시 처리 중인
전송을 기다릴지 취소할지는 구현 설정으로 정하되, 각 전송의 최종 상태를 `on_status`로
통지해야 한다.

### 2.2 송신 제어

#### `send(request) -> transfer_id`

application payload 전송을 접수한다. 반환된 `transfer_id`는 이후 상태 조회와 취소에
사용하는 불투명 식별자다.

`transfer_id` 반환은 요청이 유효하여 접수됐다는 의미일 뿐, 상대 노드까지 전달됐다는
의미가 아니다. 실제 결과는 `on_status` 또는 `get_status()`로 확인한다.

#### `cancel(transfer_id)`

아직 완료되지 않은 전송의 취소를 요청한다. 이미 무선 경로로 전송된 데이터의 회수까지
보장하지 않으며, 결과는 `CANCELLED`, `COMPLETED` 또는 `FAILED` 상태로 통지한다.

#### `get_status(transfer_id) -> TransferStatus`

특정 전송의 현재 상태를 조회한다. 정상 실행에서는 `on_status` event를 사용하고,
재접속이나 운영 화면의 상태 복구가 필요할 때 이 함수를 사용한다.

### 2.3 event 등록

#### `on_message(handler)`

수신된 application message의 처리 함수를 등록한다. CRC와 protocol validation을 통과하고
필요한 재조립을 완료한 payload만 전달한다. 손상되었거나 신뢰할 수 없는 payload는 이
handler에 전달하지 않는다.

#### `on_status(handler)`

송신 상태 변경의 처리 함수를 등록한다. 전송 접수 후의 성공, 실패, ACK 대기, timeout 및
취소 결과는 이 경로로 전달한다.

#### `on_error(handler)`

잘못된 API 사용, Service 장애 및 protocol 처리 오류의 처리 함수를 등록한다. 특정
전송과 관련된 오류에는 `transfer_id`를 포함하고, 신뢰할 수 없는 수신 payload는 오류
내용에 포함하지 않는다.

event handler는 `open()` 전에 등록하는 것을 기본 사용 순서로 한다.

## 3. API 입출력 데이터 형식

### 3.1 입력 형식

#### 3.1.1 `VdeConfig`

`open(config)` 호출에 사용한다.

| 필드          | 형식       | 설명                                                              |
| ------------- | ---------- | ----------------------------------------------------------------- |
| `local_ids`   | `uint32[]` | 이 Service가 송신자로 사용하고 수신 대상으로 인식할 local ID 목록 |
| `api_version` | string     | 요청하는 API 계약 버전. 현재 값은 `v1-draft`                      |

#### 3.1.2 `SendRequest`

`send(request)` 호출에 사용한다.

| 필드             | 형식     |  필수  | 설명                                                                |
| ---------------- | -------- | :----: | ------------------------------------------------------------------- |
| `source_id`      | `uint32` |   예   | `open()`의 `local_ids`에 등록한 송신자 ID                           |
| `destination_id` | `uint32` |   예   | 수신자 ID. 지원되는 broadcast는 `0` 사용                            |
| `data_type`      | enum     |   예   | `TBB`, `AIS_AUTH_INTEGRITY`, `ELECTRONIC_CHART`, `X509_CERTIFICATE` |
| `payload`        | bytes    |   예   | VDE-TER header를 포함하지 않은 application data                     |
| `ack_required`   | bool     |   예   | link-level ACK가 필요한 점대점 전송 여부                            |
| `content_type`   | string   | 아니요 | 예: `application/pkix-cert`; 수신 측 해석 보조 정보                 |

`destination_id=0`인 broadcast에는 `ack_required=false`를 사용한다. API 사용자는 Link ID,
Message Type, Session ID, Retransmission ID, VPFI, CRC, FEC, Burst byte 또는 GNU Radio
객체를 만들거나 전달하지 않는다.

#### 3.1.3 `transfer_id`

`send()`가 반환한 string 식별자를 `cancel(transfer_id)`와
`get_status(transfer_id)`의 입력으로 사용한다. 내부 형식을 해석하거나 수정하지 않는다.

### 3.2 출력 형식

#### 3.2.1 `TransferStatus`

`get_status()`의 반환값이며 `on_status()` handler에도 전달된다.

| 필드          | 형식        | 설명                                   |
| ------------- | ----------- | -------------------------------------- |
| `transfer_id` | string      | `send()`가 반환한 식별자               |
| `state`       | enum        | 현재 전송 상태                         |
| `reason`      | string/null | 실패·취소·timeout의 구조화된 사유 코드 |

최소 상태는 다음과 같다.

| 상태           | 의미                                                 |
| -------------- | ---------------------------------------------------- |
| `QUEUED`       | 접수되어 전송 순서를 기다림                          |
| `TRANSMITTING` | VDE-TER 경로에서 전송 중                             |
| `WAITING_ACK`  | link-level ACK 대기 중                               |
| `COMPLETED`    | ACK 불필요 전송 완료 또는 필요한 link-level ACK 수신 |
| `FAILED`       | 검증, protocol, timeout 또는 장치 오류로 실패        |
| `CANCELLED`    | 취소 처리 완료                                       |

`COMPLETED`는 VDE-TER link 수준의 완료다. 상대 MASS/ROC application이 데이터를 업무에
반영했거나 X.509 인증서 검증에 성공했다는 뜻은 아니다. 그러한 application-level 결과가
필요하면 MASS/ROC가 별도의 업무 응답을 정의해야 한다.

#### 3.2.2 `ReceivedMessage`

`on_message()` handler에 전달되는 수신 결과다.

| 필드             | 형식        | 설명                                  |
| ---------------- | ----------- | ------------------------------------- |
| `message_id`     | string      | 수신 message 추적 식별자              |
| `source_id`      | `uint32`    | 송신 노드 ID                          |
| `destination_id` | `uint32`    | 수신 노드 또는 broadcast ID           |
| `data_type`      | enum        | application data 분류                 |
| `payload`        | bytes       | 검증과 재조립을 마친 application data |
| `content_type`   | string/null | 송신자가 제공한 형식 정보             |

#### 3.2.3 `ServiceError`

`on_error()` handler에 전달되는 오류 결과다.

| 필드          | 형식        | 설명                                |
| ------------- | ----------- | ----------------------------------- |
| `code`        | string      | 프로그램이 판정할 수 있는 오류 코드 |
| `message`     | string      | 운영자용 설명                       |
| `transfer_id` | string/null | 특정 송신과 관계된 경우의 식별자    |

최소 오류 코드는 `INVALID_REQUEST`, `UNSUPPORTED_DATA_TYPE`, `PAYLOAD_TOO_LARGE`,
`UNKNOWN_TRANSFER`, `SERVICE_NOT_OPEN`, `PROTOCOL_ERROR`, `TIMEOUT`, `DEVICE_ERROR`다.

## 4. 기본 사용 순서

Python 형태의 예시는 API의 의미만 보여주며 구현 언어를 제한하지 않는다.

### 4.1 송신자 사용 순서

```python
vde = VdeTerService()

vde.on_message(handle_received_message)
vde.on_status(handle_transfer_status)
vde.on_error(handle_service_error)

vde.open({
    "api_version": "v1-draft",
    "local_ids": [MASS_A_ID],
})

transfer_id = vde.send({
    "source_id": MASS_A_ID,
    "destination_id": MASS_B_ID,
    "data_type": "AIS_AUTH_INTEGRITY",
    "payload": integrity_value,
    "ack_required": True,
})

current = vde.get_status(transfer_id)
# 필요할 때만: vde.cancel(transfer_id)

# application을 종료할 때 호출한다.
vde.close()
```

TBB broadcast는 같은 `send()`에 다음 값만 사용한다.

```python
vde.send({
    "source_id": ROC_ID,
    "destination_id": 0,
    "data_type": "TBB",
    "payload": tbb_payload,
    "ack_required": False,
})
```

전자해도와 X.509 인증서도 각각 `data_type`과 `payload`만 바꾸어 같은 API를 사용한다.

### 4.2 수신자 사용 순서

수신 application은 `receive()`를 반복 호출하지 않는다. `on_message()`에 handler를
등록한 뒤 Service를 열어두면, VDE-TER가 검증과 필요한 재조립을 완료했을 때
`ReceivedMessage`를 handler에 전달한다.

```python
def handle_received_message(message):
    if message.data_type == "TBB":
        process_tbb(message.payload)
    elif message.data_type == "AIS_AUTH_INTEGRITY":
        process_ais_auth_integrity(message.payload)
    elif message.data_type == "ELECTRONIC_CHART":
        import_electronic_chart(message.payload)
    elif message.data_type == "X509_CERTIFICATE":
        verify_x509_certificate(message.payload)


vde = VdeTerService()
vde.on_message(handle_received_message)
vde.on_status(handle_transfer_status)
vde.on_error(handle_service_error)

vde.open({
    "api_version": "v1-draft",
    "local_ids": [MASS_B_ID],
})
```

VDE-TER Service는 `destination_id`가 등록된 `local_ids` 중 하나이거나 broadcast ID `0`인
message만 `on_message`로 전달한다. 다른 노드가 목적지인 message, CRC 실패 message,
protocol validation 실패 message 및 재조립이 끝나지 않은 payload는 전달하지 않는다.

수신 handler가 받는 `COMPLETED` payload는 VDE-TER 전송 관점에서 검증된 데이터다.
AIS 무결성 판정, 인증서 신뢰경로 검증과 전자해도 적용 성공 여부는 각각의 MASS/ROC
application이 판단한다.

MASS/ROC는 자체 업무 데이터를 해당 `data_type`과 `bytes payload`로 변환해 `send()`에
전달하고, `on_message()`로 받은 payload를 자체 업무 기능에서 처리한다.

## 5. 현재 용량 제한과 확장 호환성

현재 Link ID 11 CORE reference profile은 단일 message에 들어가는 application data가
매우 작다. 따라서 짧은 TBB 또는 AIS 인증·무결성 값은 먼저 검증할 수 있지만, 일반적인
X.509 인증서와 전자해도는 분할·다중 Packet 전송 및 재조립 기능이 구현되기 전에는
`PAYLOAD_TOO_LARGE`로 거부될 수 있다.

이 제한을 해결할 때도 공개 API에 `send_certificate()`나 `send_chart()` 같은 함수를
추가하지 않는다. VDE-TER 내부의 segment/data-session 기능만 확장하고 기존 `send()`와
`on_message()` 계약을 유지한다.

## 6. MASS/ROC에서 다루지 않는 항목

MASS/ROC는 Link ID, Message Type, CRC, FEC, Burst, slot과 재전송 절차를 전달하거나
구현하지 않는다. 같은 프로세스 함수 호출이나 별도 프로세스 RPC 같은 연결 방식도 이
API의 데이터 의미를 바꾸지 않는다.

VDE-TER는 application data의 전달까지만 책임진다. 인증서 검증, 전자해도 적용과 AIS
인증·무결성 결과 처리는 수신한 MASS/ROC application이 담당한다.
