# 링크 팀 제안 — 위치보고 codec

> **이 폴더는 응용 팀이 만든 참고 구현이다.** VDETER 저장소에는 들어가 있지 않고,
> 우리 코드도 이것을 쓰지 않는다. 링크 팀이 검토·이관하기 위한 자료다.

## 왜 만들었나

위치보고를 VDETER로 보내려면 `payload_format` 등록과 codec이 필요하다. 명세
(`../전선형식_위치보고_SC-VDE-005.md`)만 넘겨도 되지만, 구현이 실제로 가능한지
미리 보이고 이관 작업을 줄이려고 참고 구현까지 만들었다.

**registry에는 손대지 않았다.** VDETER의 `payload_formats.py`에는 승인 fingerprint
계약 시험(`TC-PAYLOAD-002`)이 걸려 있어, 항목을 추가하면 기대 fingerprint가 달라진다.
등록은 링크 팀이 그 절차대로 해야 한다.

## 무엇이 들어 있나

| 파일 | 내용 |
| --- | --- |
| `sc_vde_position_report.py` | codec 참고 구현. `sc_vde_route_control.py` 구조를 그대로 따랐다 |
| `test_sc_vde_position_report.py` | 우리 인코더 출력이 이 codec을 왕복하는지 확인 |

## 요지

같은 carrier `FF 7F`의 비어 있는 **Message Format 1** 자리를 쓴다.

```text
wire        = FF 7F | 01 | body[111] | zero pad[1]   = 17 bytes 고정
application =                body[111] | zero pad[1]  = 14 bytes 고정
```

노선(MF 2)·ACK(MF 3)과 달리 **길이가 고정**이라 검증이 단순하다. codec이 보는 것은
길이·padding·필드 raw 범위뿐이다.

`addressed_only = False`인 점이 노선·조종과 다르다. 위치보고는 AIS와 같이
broadcast로 나가기 때문이다.

## 돌려보기

```bash
.venv-crypto/bin/python docs/proposals/test_sc_vde_position_report.py
```

통과 내용 — 명세 참조 벡터 일치, codec 왕복(17 byte wire, prefix `ff 7f 01`),
필드 오프셋 8개 정합, 잘못된 입력 5종 거부(길이 위반·padding 1·Autonomy 예약값·
잘못된 prefix).

## 이관할 때

1. `sc_vde_position_report.py`를 `VDETER/src/vdeter/presentation/codecs/`로 옮긴다
2. 파일 상단의 import를 상대경로로 되돌린다 (주석에 원래 형태를 적어 뒀다)
3. `profiles/payload_formats.py`에 entry를 추가하고 fingerprint 기대값을 갱신한다
4. 시험을 VDETER 쪽으로 옮긴다

## 남은 결정 — broadcast

codec이 등록되어도 **MASS-originated broadcast**가 열리지 않으면 위치보고는 나가지
못한다. 명세 §요청사항 ②에 적었듯, 브로드캐스트 전송 경로 자체는 ROC용으로 이미
구현돼 있고 이 형식은 크기 조건(wire 17 byte)도 만족한다. 송신 주체를 MASS까지
넓힐 수 있는지 알려주기 바란다.

어렵다면 MASS→ROC addressed로도 우리 시나리오는 성립한다(차선).
