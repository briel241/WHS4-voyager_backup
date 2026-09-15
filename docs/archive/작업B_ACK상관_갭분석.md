# 작업 B-1 — 압축 ACK 의 route 상관 식별 갭 분석

## 1. 문제

현재 압축 ACK(Message ID 3)는 **상태 코드 4 bit 하나**만 나른다. 논리 JSON 모델의
ACK 는 `routeID` 와 `routeEditionNo` 를 되돌리도록 돼 있지만(지시서 5.2 마지막 문단),
압축 형식에는 그 자리가 없다.

한 번에 한 항로만 오간다면 문제가 없다. 그러나 다음 상황에서는 ACK 를 어느
항로지시에 붙일지 알 수 없다.

- ROC 가 이전 항로의 ACK 를 기다리는 동안 개정판(editionNo+1)을 보낸 경우
- 재전송이 겹쳐 ACK 가 순서를 바꿔 도착한 경우
- 항로지시가 여러 fragment 로 쪼개져 부분 ACK 가 오는 경우
- ROC 가 두 척 이상을 동시에 다루며 링크 세션을 공유하는 경우

특히 위험한 것은 두 번째·세 번째다. **status=4(Acknowledged)를 엉뚱한 항로에
붙이면, 실제로는 거절된 항로를 승인된 것으로 오인**하게 된다. 원격 조종
시나리오에서 이건 안전 문제다.

## 2. 두 가지 안

### (a) 압축 ACK 에 짧은 route 참조 필드를 추가한다

`routeID` 문자열 해시의 하위 N bit + `routeEditionNo` 소수 bit 를 ACK 본문에 붙인다.

예: routeID 의 SHA-256 하위 12 bit + editionNo 하위 4 bit = **16 bit 추가**
(현재 ACK 본문 4 bit → 20 bit, padding 포함 3 byte).

- 장점: 응용 계층만으로 상관이 닫힌다. 링크 구현이 바뀌어도 영향이 없고,
  로그·판정(작업 C)에서 ACK 를 항로에 정확히 붙일 수 있다.
- 단점: 메시지 형식 변경 → registry 재배정과 상대 구현 동시 수정이 필요하다.
  해시라 충돌 가능성이 남는다(12 bit 면 동시 미결 항로 4개 기준 충돌 확률 약 0.4%).

### (b) 링크의 point-to-point ACK 상관에 위임하고 응용 ACK 는 상태만 유지한다

VDE-TER 링크가 이미 송신 단위별 ACK 를 짝지어 주므로, 응용은 "직전에 보낸 것에
대한 응답"이라는 링크의 판단을 그대로 받는다.

- 장점: 형식 변경이 없다. 지금 구현·테스트가 그대로 유효하다.
- 단점: 상관 정확성이 **링크 구현에 의존**한다. 우리 관할 밖(`VDETER/`, 팀원 담당)
  이라 응용 단독으로 검증할 수 없고, 링크가 재정렬·병합을 하면 조용히 틀린다.
  또 논리 JSON 모델(routeID 를 되돌리는 규약)과 어긋난 채로 남는다.

## 3. 권고 — (a) 를 택하되, 범위를 좁힌다

**(a) 를 추천한다.** 근거:

1. 안전 관련 오탐(잘못된 승인)의 대가가 16 bit 대역폭보다 훨씬 크다.
   항로지시 본문이 웨이포인트당 77 bit 를 쓰는 것에 비하면 16 bit 는 무시할 만하다.
2. (b) 는 우리가 검증할 수 없는 계층에 안전 성질을 맡긴다. 작업 C 의 판정
   ("ACK status=4, waypoint 개수 일치")도 어느 항로에 대한 ACK 인지 모르면
   증명력이 없다.
3. 논리 JSON 모델과의 정합이 자연스럽게 맞는다 — 변환기가 `routeID` 를
   버리지 않고 참조로 접을 수 있다.

### 구체안

```
ACK 본문 = [ status 4 bit | routeRef 12 bit | editionNo 4 bit ]  (총 20 bit)

routeRef  = SHA-256(routeID.encode("utf-8")) 의 최하위 12 bit
editionNo = routeEditionNo mod 16
```

- ROC 는 미결 항로 목록에서 `(routeRef, editionNo)` 로 후보를 찾는다.
  **후보가 2건 이상이면 상관 실패로 처리**하고 ACK 를 버린다(조용히 잘못
  붙이는 것보다 낫다). 동시 미결 항로를 4건 이하로 제한하면 실무상 충분하다.
- editionNo 를 하위 4 bit 만 쓰는 이유: 개정은 단조 증가라 최근 16판 안에서만
  구분하면 되고, 미결 상태로 16판이 겹칠 일은 없다.
- 12 bit 가 부족하다고 판단되면 `routeRef` 를 16 bit 로 키워 총 24 bit(3 byte)로
  맞추는 것도 무리가 없다. 폭만 상수로 빼 두면 나중에 조정 가능하다.

### (b) 를 택해야 하는 경우

상대 구현(선박측 벤더)의 ACK 형식을 이미 동결했고 변경 협의가 불가능하다면
(b) 로 가되, **작업 C 판정에 "동시 미결 항로 1건" 전제 체크를 넣어야 한다**.
즉 미결 항로가 2건 이상인 상태에서 온 ACK 는 판정 근거로 쓰지 않는다.

## 4. 변환기가 이미 준비해 둔 것

`common/messages/route_json_bridge.py` 는 압축이 못 담는 항목을
`metadata_not_in_bits()` 로 분리 보고한다. `routeID` / `routeEditionNo` 가
`dropped` 에 들어 있으므로, (a) 를 채택하면 이 두 값을 참조 비트로 접는 함수만
추가하면 된다. 억지로 비트에 밀어넣은 곳은 없다.

`ack_json_for(operation_json, status, description)` 은 논리 ACK 를 만들 때
source/destination MMSI 를 뒤집고 같은 `routeID`·`routeEditionNo` 를 되돌린다 —
(a) 채택 시 이 값이 그대로 참조 필드의 입력이 된다.
