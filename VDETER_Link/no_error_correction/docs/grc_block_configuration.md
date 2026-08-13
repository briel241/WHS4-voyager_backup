# VDE-TER 2단계 GRC 블록 설정값

> VDE-TER 2단계 GNU Radio TX/RX Flowgraph에 적용하는 블록 연결과 설정값을 정리한 문서입니다.

## 1. 공통 변수

| 변수                |        값 |
| ------------------- | --------: |
| `samp_rate`         |     `1e6` |
| `sps`               |       `4` |
| `excess_bw`         |    `0.35` |
| `center_freq`       | `157.2e6` |
| `bpsk.const_points` | `[-1, 1]` |
| `bpsk.sym_map`      |  `[0, 1]` |
| `bpsk.rot_sym`      |       `2` |

## 2. TX 설정

### 2.1 연결 순서

```text
VDE TX Adapter → Constellation Modulator → Multiply Const → USRP Sink
                                                   └──────→ TX Frequency Sink
```

### 2.2 TX 변수

| 변수      |            값 |
| --------- | ------------: |
| `tx_dev`  | `'type=b200'` |
| `tx_gain` |          `40` |
| `tx_ampl` |         `0.5` |

### 2.3 VDE TX Adapter

| 항목        | 값                                 |
| ----------- | ---------------------------------- |
| Block       | Embedded Python Block              |
| 외부 모듈   | `vdeter_link.adapters.gnuradio_tx` |
| 외부 클래스 | `VDETxSource`                      |
| GRC 클래스  | `blk`                              |
| Input       | 없음                               |
| Output      | `np.uint8`, vector length `1`      |
| 생성자      | `super().__init__()`               |

Embedded Python Block의 `Code`:

```python
# Thin GRC entry-point wrapper for the VDE TX Adapter.
import os
import sys

try:
    _base = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _base = os.path.abspath(os.getcwd())

_probe = _base
while True:
    if os.path.isfile(os.path.join(_probe, "vdeter_link", "__init__.py")):
        if _probe not in sys.path:
            sys.path.insert(0, _probe)
        break
    _parent = os.path.dirname(_probe)
    if _parent == _probe:
        break
    _probe = _parent

from vdeter_link.adapters import gnuradio_tx


class blk(gnuradio_tx.VDETxSource):
    def __init__(self):
        super().__init__()
```

### 2.4 Constellation Modulator

| 항목                  | 값          |
| --------------------- | ----------- |
| Constellation         | `bpsk`      |
| Differential Encoding | `True`      |
| Samples/Symbol        | `sps`       |
| Excess BW             | `excess_bw` |
| Truncate              | `False`     |
| Log                   | `False`     |
| Verbose               | `False`     |

### 2.5 Multiply Const

| 항목          | 값        |
| ------------- | --------- |
| Type          | Complex   |
| Constant      | `tx_ampl` |
| Vector Length | `1`       |

### 2.6 USRP Sink

| 항목             | 값            |
| ---------------- | ------------- |
| Device Address   | `tx_dev`      |
| Device Type      | `type=b200`   |
| Channels         | `1`           |
| Stream Type      | `fc32`        |
| Sample Rate      | `samp_rate`   |
| Center Frequency | `center_freq` |
| Bandwidth        | `samp_rate`   |
| Gain             | `tx_gain`     |
| Antenna          | `TX/RX`       |
| Start Time       | `-1`          |

### 2.7 TX Frequency Sink

| 항목             | 값                  |
| ---------------- | ------------------- |
| Input Type       | Complex             |
| 연결 위치        | Multiply Const 출력 |
| Center Frequency | `0`                 |
| Bandwidth        | `samp_rate`         |
| FFT Size         | `2048`              |
| Window           | Blackman-Harris     |
| Average          | `0.2`               |
| Autoscale        | `True`              |
| Update Time      | `0.1 s`             |

## 3. RX 설정

### 3.1 연결 순서

```text
USRP Source → RRC Matched Filter → Symbol Sync → Costas Loop
                                                    ├─→ RX Constellation Sink
                                                    └─→ Constellation Decoder
                                                        → Differential Decoder
                                                        → F9A8 Correlator
                                                        → VDE RX Adapter

USRP Source ───────────────────────────────────────────→ RX Frequency Sink
```

### 3.2 RX 변수

| 변수      |            값 |
| --------- | ------------: |
| `rx_dev`  | `'type=b200'` |
| `rx_gain` |          `40` |

### 3.3 USRP Source

| 항목             | 값            |
| ---------------- | ------------- |
| Device Address   | `rx_dev`      |
| Device Type      | `type=b200`   |
| Channels         | `1`           |
| Stream Type      | `fc32`        |
| Sample Rate      | `samp_rate`   |
| Center Frequency | `center_freq` |
| Bandwidth        | `samp_rate`   |
| Gain             | `rx_gain`     |
| AGC              | Disabled      |
| Antenna          | `RX2`         |
| Start Time       | `-1`          |

### 3.4 RRC Matched Filter

| 항목           | 값                                        |
| -------------- | ----------------------------------------- |
| Type           | Complex-to-complex FIR (`fir_filter_ccf`) |
| Interpolation  | `1`                                       |
| Decimation     | `1`                                       |
| Gain           | `1.0`                                     |
| Sample Rate    | `samp_rate`                               |
| Symbol Rate    | `samp_rate / sps`                         |
| Alpha          | `excess_bw`                               |
| Number of Taps | `11 * sps + 1`                            |

### 3.5 Symbol Sync

| 항목                    | 값                 |
| ----------------------- | ------------------ |
| Input/Output Type       | Complex            |
| Samples/Symbol          | `sps`              |
| Timing Error Detector   | Mueller and Müller |
| Loop Bandwidth          | `0.045`            |
| Damping Factor          | `1.0`              |
| TED Gain                | `1.0`              |
| Maximum Deviation       | `1.5`              |
| Output Samples/Symbol   | `1`                |
| Number of Filters       | `128`              |
| Interpolating Resampler | MMSE 8-tap         |

### 3.6 Costas Loop

| 항목           | 값       |
| -------------- | -------- |
| Loop Bandwidth | `0.0628` |
| Order          | `2`      |
| Use SNR        | `False`  |

### 3.7 Constellation Decoder

| 항목          | 값      |
| ------------- | ------- |
| Constellation | `bpsk`  |
| Input         | Complex |
| Output        | Byte    |

### 3.8 Differential Decoder

| 항목         | 값           |
| ------------ | ------------ |
| Modulus      | `2`          |
| Coding       | Differential |
| Input/Output | Byte         |

### 3.9 F9A8 Correlator

| 항목              | 값                          |
| ----------------- | --------------------------- |
| Block             | Correlate Access Code - Tag |
| Input/Output Type | Byte                        |
| Access Code       | `1111100110101000`          |
| Threshold         | `0`                         |
| Tag Name          | `preamble_detected`         |

### 3.10 VDE RX Adapter

| 항목        | 값                                    |
| ----------- | ------------------------------------- |
| Block       | Embedded Python Block                 |
| 외부 모듈   | `vdeter_link.adapters.gnuradio_rx`    |
| 외부 클래스 | `VDERxSink`                           |
| GRC 클래스  | `blk`                                 |
| Input       | `np.uint8`, vector length `1`         |
| Output      | 없음                                  |
| Parameter   | `tag_name='preamble_detected'`        |
| 생성자      | `super().__init__(tag_name=tag_name)` |

Embedded Python Block의 `Code`:

```python
# Thin GRC entry-point wrapper for the VDE RX Adapter.
import os
import sys

try:
    _base = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _base = os.path.abspath(os.getcwd())

_probe = _base
while True:
    if os.path.isfile(os.path.join(_probe, "vdeter_link", "__init__.py")):
        if _probe not in sys.path:
            sys.path.insert(0, _probe)
        break
    _parent = os.path.dirname(_probe)
    if _parent == _probe:
        break
    _probe = _parent

from vdeter_link.adapters import gnuradio_rx


class blk(gnuradio_rx.VDERxSink):
    def __init__(self, tag_name="preamble_detected"):
        super().__init__(tag_name=tag_name)
```

### 3.11 RX Frequency Sink

| 항목             | 값                 |
| ---------------- | ------------------ |
| Input Type       | Complex            |
| 연결 위치        | USRP Source 출력   |
| Center Frequency | `0`                |
| Bandwidth        | `samp_rate`        |
| FFT Size         | `2048`             |
| Window           | Blackman-Harris    |
| Average          | `0.2`              |
| Autoscale        | `True`             |
| Update Time      | `0.1 s`            |
| Y Range          | `-140` to `-20 dB` |

### 3.12 RX Constellation Sink

| 항목             | 값               |
| ---------------- | ---------------- |
| Input Type       | Complex          |
| 연결 위치        | Costas Loop 출력 |
| Number of Points | `1024`           |
| X Range          | `-2` to `2`      |
| Y Range          | `-2` to `2`      |
| Grid             | `True`           |

## 4. 유선 연결 설정

```text
USRP TX/RX → RF 감쇠기 → 동축 케이블 → USRP RX2
```

| 항목      | 현재 시험값 |
| --------- | ----------: |
| RF 감쇠기 |     `30 dB` |
| `tx_gain` |        `40` |
| `tx_ampl` |       `0.5` |
| `rx_gain` |        `40` |

감쇠기 없이 송신단과 수신단을 직접 연결하지 않는다. 실제 수신 입력 전력이 장치 허용 범위를 넘지 않도록 감쇠량과 gain을 조정한다.
