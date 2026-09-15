# hello_tx / hello_rx — BPSK "Hello" 송수신 확인

USRP B210 두 대를 동축으로 잇고 **비트가 오가는지**만 확인하는 최소 구성입니다.
VDE-TER 규격과는 무관한 **링크 점검용**입니다.

- 보내는 것: 문자열 `"Hello"` 를 계속 반복
- 확인 방법: 수신 파일을 `tools/find_pattern.py` 로 검사
- 필요한 것: 노트북 2대, B210 2대, 동축, 30 dB 감쇠기

---

## 1. 빠른 실행

```bash
# 처음 한 번만 — .grc 에서 실행 파일 생성
cd "/Users/pmy/보이저 3호/voyager_3"
grcc -o grc grc/hello_tx.grc && grcc -o grc grc/hello_rx.grc
```

**Node A (송신 노트북)**
```bash
python3 grc/hello_tx.py
```

**Node B (수신 노트북)**
```bash
python3 grc/hello_rx.py
```

10초쯤 두고 창을 닫은 뒤:

```bash
python3 tools/find_pattern.py ~/hello_rx.bin
```

`Hello` 가 나오면 링크 정상입니다.

> ⚠️ **안테나를 꽂지 않습니다.** `TX/RX ─ 30 dB 감쇠기 ─ 동축 ─ RX2` 로만 연결합니다.
> 감쇠기 없이 직결하면 수신 프론트엔드가 손상됩니다.

---

## 2. 송신 (hello_tx.grc)

```
[Vector Source] → [Constellation Modulator] → [Multiply Const] → [UHD USRP Sink]
   "Hello"            비트→전파 파형             세기 조절          안테나 단자로
                                     ↘
                                  [Frequency Sink]  뭘 보내는지 눈으로 확인
```

| 블록 | 하는 일 |
|---|---|
| **Vector Source** | `[72, 101, 108, 108, 111]` 을 무한 반복. 이건 `"Hello"` 의 아스키 코드입니다 (72=`H`, 101=`e`, 108=`l`, 111=`o`). `Repeat: Yes` 라서 계속 흘려보냅니다 |
| **Constellation Modulator** | 세 가지를 한꺼번에 합니다 — ① 바이트를 비트로 풀고 ② **차동 부호화** 하고 ③ BPSK 로 실어 파형을 다듬습니다(RRC) |
| **Multiply Const** | 파형의 크기를 조절합니다. USRP 의 게인과 별개로 디지털 단에서 줄이는 것 |
| **UHD USRP Sink** | 실제 RF 로 내보냅니다 |
| **Frequency Sink** | 보내는 신호의 스펙트럼. 송신이 되고 있는지 눈으로 확인용 |

### 차동 부호화가 뭔가

BPSK 는 점이 두 개(왼쪽/오른쪽)뿐이라, 수신기가 **좌우를 반대로 잡을 수** 있습니다.
그러면 모든 비트가 뒤집혀 나옵니다.

그래서 비트를 "0인가 1인가" 로 보내지 않고 **"앞 비트와 같은가, 다른가"** 로
바꿔서 보냅니다. 전체가 뒤집혀도 "같다/다르다" 는 그대로이므로 원래 값이
복원됩니다. 수신측 `Differential Decoder` 가 이걸 되돌립니다.

---

## 3. 수신 (hello_rx.grc)

```
[UHD USRP Source] ─┬→ [Frequency Sink]      ★ 신호가 오는지 여기서 먼저 확인
                   │
                   └→ [RRC Filter] → [Symbol Sync] → [Costas Loop] ─┬→ [Constellation Sink]
                       잡음 걸러내기   심볼 한가운데   주파수·위상 보정 │
                                                                     ↓
                                          [Constellation Decoder] → [Differential Decoder]
                                                 점 → 비트                차동 되돌리기
                                                                     ↓
                                              [Repack Bits] → [File Sink]
                                              비트8개→1바이트    파일로 저장
```

| 블록 | 하는 일 | 쉽게 말하면 |
|---|---|---|
| **UHD USRP Source** | RF 수신 | 귀 |
| **Frequency Sink** | 받은 신호의 스펙트럼 | **신호가 오나 안 오나 보는 창** |
| **RRC Filter** | 송신과 똑같은 모양의 필터 | 같은 모양 체로 걸러 잡음만 빼냄 |
| **Symbol Sync** | 심볼의 한가운데 시점을 찾아 표본을 뽑음 | 흔들리는 사진의 초점 맞추기 |
| **Costas Loop** | 두 장비의 주파수·위상 차이를 따라가며 보정 | 조금씩 다른 두 시계 맞추기 |
| **Constellation Sink** | 성상도 표시 | **BPSK 는 좌우 두 덩어리로 모이면 정상** |
| **Constellation Decoder** | 점의 위치를 보고 0/1 판정 | 왼쪽이면 0, 오른쪽이면 1 |
| **Differential Decoder** | 송신의 차동 부호화를 되돌림 | "같다/다르다" → 원래 0/1 |
| **Repack Bits** | 비트 8개를 1바이트로 묶음 | 낱개 비트를 글자로 |
| **File Sink** | 파일로 저장 | `~/hello_rx.bin` |

---

## 4. 변수

| 변수 | 값 | 바꿔도 되나 |
|---|---|---|
| `samp_rate` | 1e6 | **양쪽 같아야 함** |
| `sps` | 4 | **양쪽 같아야 함** (심볼당 샘플) |
| `excess_bw` | 0.35 | **양쪽 같아야 함** (RRC 모양) |
| `center_freq` | 157.2e6 | **양쪽 같아야 함** |
| `bpsk` | BPSK 성상도 | **양쪽 같아야 함** |
| `tx_gain` | 20 | ✅ **안 되면 여기부터 올린다** (30 → 40) |
| `tx_ampl` | 0.5 | ✅ 조절 가능 |
| `rx_gain` | 40 | ✅ 조절 가능 |
| `tx_dev` / `rx_dev` | 시리얼 | ✅ **자기 장비에 맞게 수정** |
| `out_file` | `~/hello_rx.bin` | ✅ |

시리얼은 이걸로 확인합니다:

```bash
uhd_find_devices
```

**양쪽이 같아야 하는 값이 하나라도 다르면 아예 수신되지 않습니다.**

### 중심주파수를 157.2 MHz 로 둔 이유

두 B210 은 각자 시계를 가지고 있어 주파수가 미세하게 다릅니다. 이 차이는
**주파수에 비례**해서, 같은 장비라도

- 157.2 MHz 에서는 약 **125 Hz** 차이
- 1 GHz 에서는 약 **800 Hz** 차이

가 납니다. Costas Loop 가 800 Hz 도 감당은 하지만, 낮은 쪽이 확실히 안전합니다.
`center_freq` 를 `1e9` 로 바꾸면 원래대로 돌아갑니다.

---

## 5. 화면에서 볼 것

| 창 | 정상 | 이상하면 |
|---|---|---|
| 송신 스펙트럼 | 가운데 넓은 봉우리 | 평평하면 송신이 안 되는 것 |
| 수신 스펙트럼 | 가운데 봉우리 | **평평하면 `tx_gain` 을 올린다** |
| 성상도 | **좌우 두 덩어리** | 둥근 원이면 Costas 미잠김 → 신호가 약함 |

수신 스펙트럼이 디버깅 1순위입니다. 여기에 봉우리가 없으면 그 뒤 블록은
전부 잡음을 처리하고 있는 것이라 볼 필요가 없습니다.

---

## 6. 결과 확인

```bash
python3 tools/find_pattern.py ~/hello_rx.bin
```

**성공**

```
✅   3       아니오       799   .>.HelloHelloHelloHelloHello...
```

정렬 숫자(위에서는 3)는 신경 쓰지 않아도 됩니다. **`Hello` 가 발견되면 링크 정상입니다.**

**실패**

```
❌ 어느 정렬에서도 패턴을 찾지 못했습니다.
```

순서대로 확인합니다:

1. 수신 스펙트럼에 봉우리가 있는가 → 없으면 `tx_gain` 을 20 → 30 → 40 으로
2. 두 노트북의 `center_freq`, `samp_rate`, `sps`, `excess_bw` 가 같은가
3. 동축과 감쇠기가 제대로 물려 있는가
4. `uhd_find_devices` 로 각자 자기 장비가 보이는가

---

## 7. 왜 "정렬" 이라는 게 나오나

이 구성에는 **"여기서부터 데이터" 라고 알려주는 표식(동기어)이 없습니다.**

그래서 수신측은 들어오는 비트를 그냥 8개씩 묶습니다. 어디서부터 묶기
시작했느냐에 따라 바이트가 통째로 밀립니다. 자를 자리가 8군데인데 그중
1군데만 정답입니다.

```
정답  01001000 01100101 ...  →  Hello
3칸밀림  01000011 00101011 ...  →  알 수 없는 글자
```

`find_pattern.py` 는 8가지를 전부 시도해 보는 도구입니다. 하나라도 맞으면
**"신호는 잘 왔고 자르는 자리만 틀렸다"** 는 뜻이므로 링크 확인은 끝난 것입니다.

### 이 방법의 한계

오류가 섞이기 시작하면 어느 정렬에서도 `Hello` 가 안 보입니다. 즉

- **"링크가 살아 있나?"** → 이 구성으로 충분합니다
- **"오류율이 얼마인가?"** → 동기어가 필요합니다

오류율을 재려면 `apps/stage1_bitstream.py` 를 쓰세요. VDE-TER 규격의
동기어가 들어 있어 오류가 있는 상황에서도 시작점을 찾고, 몇 비트가 틀렸는지
세어 줍니다.

---

## 8. 파일 목록

| 파일 | 설명 |
|---|---|
| `grc/hello_tx.grc` | 송신 플로우그래프 (GRC 로 열어 편집 가능) |
| `grc/hello_rx.grc` | 수신 플로우그래프 |
| `grc/build_hello_flowgraphs.py` | 위 두 `.grc` 생성기. 파라미터 일괄 변경용 |
| `tools/find_pattern.py` | 수신 파일 확인 도구 |

`.grc` 는 평범한 GRC 파일이라 `gnuradio-companion` 으로 열어 마우스로
수정해도 됩니다. 생성기는 값을 한꺼번에 바꿀 때만 쓰면 됩니다.

```bash
python3 grc/build_hello_flowgraphs.py     # 두 .grc 를 다시 만든다
```

---

## 9. 검증 기록

USRP 없이 가짜 채널(잡음 + 300 Hz 주파수 오차)로 전 구간을 돌려 확인했습니다.

```
✅   3       아니오       799   .>.HelloHelloHelloHelloHello...
```

잡음 진폭 0.02 와 0.15 양쪽에서 `Hello` 가 799회 검출되었습니다.
따라서 하드웨어에서 안 될 경우 원인은 **코드가 아니라 배선·전력·설정값** 입니다.
