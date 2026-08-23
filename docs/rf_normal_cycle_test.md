# RF 벤치 테스트 — 정상 시나리오 한 사이클 (B210 2대)

mass_roc가 만든 실제 응용 payload가 **B210 전파를 타고 반대편에서 응용 필드로
복원되는지** 확인한다. Hello 비트스트림 링크와 mass_roc↔링크 왕복(293개 테스트)은
각각 검증됐지만, 둘을 이어 붙여 RF로 돌린 적은 아직 없다. 이 테스트가 그 이음매다.

> 물리 계층(동축·감쇠기·포트·serial 찾기)은 [bitstream_link.md](bitstream_link.md)와
> [bitstream_troubleshooting.md](bitstream_troubleshooting.md)에 이미 정리돼 있다.
> 여기서는 **이번 테스트에 새로 필요한 것만** 적는다.

---

## 이 테스트가 증명하는 것 / 못 하는 것

| | |
| --- | --- |
| ✅ 증명 | mass_roc 응용 payload가 실제 채널을 견디고 반대편에서 필드까지 복원된다 (물리계층 통합 스모크 테스트) |
| ❌ 미해당 | SC-VDE 공격/방어 시나리오. 공격자 경로가 아직 없다. 이건 **정상 대조군이 실물에서도 도는가**까지다 |

### 단방향 재현이라는 점

B210 A는 **송신 전용**(`vdeter_tx`), B는 **수신 전용**(`vdeter_rx`)이다. 각 장치가
한 역할에 묶이므로 살아있는 양방향 handshake(ROC가 실시간으로 답신)는 이 구성으로
안 된다. 대신 **한 사이클에 실제로 전파를 타는 네 메시지**를 송신기 A에서 차례로
내보내고 수신기 B에서 모두 복원한다. 방향(`MASS→ROC` / `ROC→MASS`)은 payload 안의
Source/Destination로 구분되고, 링크는 같은 대역으로 나른다.

전파에 싣는 네 메시지:

| # | 방향 | 메시지 | Type | payload |
| :-: | --- | --- | :-: | :-: |
| 1 | MASS→ROC | Position Report (before) | 93 broadcast | 17 B |
| 2 | ROC→MASS | Route Instruction | 92 addressed | 32 B |
| 3 | MASS→ROC | ACK accepted | 93 addressed | 5 B |
| 4 | MASS→ROC | Position Report (after) | 93 broadcast | 17 B |

---

## 새로 걸리는 것 하나 — 장치 serial 지정 (필수)

`vdeter_tx.py`·`vdeter_rx.py`는 장치 인자가 **`tx_dev = rx_dev = 'type=b200'`으로
하드코딩**돼 있고 CLI 옵션이 없다. B210이 **한 머신에 2대**면 `type=b200`이 둘 다에
매칭돼 UHD가 어느 걸 잡을지 모호해진다(대개 `multiple devices found`).

두 serial을 확인하고:

```bash
uhd_find_devices
```

각 플로우그래프의 장치 변수를 **서로 다른 serial**로 바꾼다 (Hello 링크에서 이미
`serial=34D03DD` 형태로 쓴 것과 같다 — bitstream_link.md 파라미터 표 참조):

- `VDETER_Link/no_error_correction/vdeter_tx.py` →  `self.tx_dev = tx_dev = 'serial=<A의 serial>'`
- `VDETER_Link/no_error_correction/vdeter_rx.py` →  `self.rx_dev = rx_dev = 'serial=<B의 serial>'`

> `.grc`에서 `tx_dev`/`rx_dev` 변수 기본값을 고쳐 재생성하면 영구적이다. 급하면
> 생성된 `.py`의 위 한 줄씩만 고쳐도 되지만, 재생성 시 지워진다.

물리 결선·감쇠기·게인은 Hello 때와 동일하다: **안테나 금지**, `A.TX/RX ─ 30 dB
감쇠기 ─ 동축 ─ B.RX2`, 감쇠기 없이 직결하면 수신단 손상.

---

## 실행 순서

### 1. 송신 재료 뽑기 (장비 없이, 먼저)

```bash
/opt/homebrew/bin/python3 apps/rf_normal_cycle.py payloads
```

한 사이클의 네 메시지를 만들고, **대화식 콘솔에 그대로 넣을 입력**과 **반복 실행용
`TX_MESSAGES` 블록**을 함께 출력한다. mass_roc가 생성하므로 payload는 매번 동일하다.

### 2. 수신기 B 먼저 실행

```bash
cd VDETER_Link/no_error_correction
/opt/homebrew/bin/python3 vdeter_rx.py
```

Correlator가 preamble(`F9A8`)을 잡을 때마다 `[VDE Message]` 블록을 찍는다 —
Type / Source ID / Destination ID / `Payload (hex): ..`. 이 hex가 우리가 확인할 값이다.

### 3. 송신기 A 실행 — 대화식으로 네 메시지 송신

`vdeter_link/config.py`의 `TX_MODE = "interactive"`(기본값)인지 확인하고:

```bash
/opt/homebrew/bin/python3 vdeter_tx.py
```

1단계에서 뽑은 입력을 **메시지 [1]→[4] 순서로** 프롬프트에 붙여넣는다. 각 메시지는
`Repeat count`를 `1`로 둔다(한 번만 나가게). 한 건 보낼 때마다 B 콘솔에 대응하는
`[VDE Message]`가 떠야 한다.

> 반복·게인 조정이 필요하면 `TX_MODE = "fixed"`로 두고 config의 `TX_MESSAGES`를
> 1단계가 출력한 블록으로 교체한다. 한 메시지씩 거리/게인을 바꿔가며 재현하기 좋다.

### 4. 복원 확인

B 콘솔이 찍은 각 `Payload (hex): ..`를 복사해:

```bash
/opt/homebrew/bin/python3 apps/rf_normal_cycle.py decode "<붙여넣은 hex>"
```

응용 필드로 복원해 보여준다. **네 메시지가 모두 복원되고** 좌표·침로·항로 필드가
1단계 출력과 일치하면 통과다.

- Position Report → 좌표 129.1/35.1, COG 90°(before) / 48°(after), Autonomy `RC`
- Route Instruction → waypoint 3개, sender `authority`, route type `mandatory`
- ACK → `accepted`

`VPFI가 다릅니다` 거부가 나오면 그 수신은 우리 payload가 아니거나(잡음에서 나온
가짜 preamble / 남의 트래픽) 비트가 깨진 것이다. M.2092-2 §A4-4.15대로 **수신하지
않은 것처럼** 버리면 된다 — 정상적으로 걸러진 것이다.

---

## 판정

| 결과 | 의미 |
| --- | --- |
| 네 메시지 모두 복원·필드 일치 | 응용↔링크↔RF 경로 통합 성공. 정상 대조군을 실물에서 확보 |
| 일부만 복원 | 게인/감쇠/동기 문제. bitstream_troubleshooting.md의 게인·SNR 절 확인 |
| 필드 불일치 | 채널 비트 오류. 재전송하거나 게인 조정. 큰 payload(#2, 32 B)가 먼저 깨진다 |
| 전부 거부(VPFI 불일치) | preamble은 잡히나 payload가 깨짐 — SNR 부족일 가능성이 높다 |

통과하면 이 사이클이 **후속 공격 실험(SC-VDE-005 등)의 정상 대조군**이 된다.
공격군은 같은 물리 구성에서 위조·재전송을 넣고 이 결과와 비교한다.
```
