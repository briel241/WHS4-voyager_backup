# Stage 3 Raspberry Pi 5 B210 시험

대상은 native Ubuntu 24.04 LTS `aarch64/arm64`, Raspberry Pi 5, Python 3.12,
GNU Radio/gr-uhd 3.10.9.2, UHD 4.6.0.0+ds1-5.1ubuntu0.24.04.1이다. WSL과 다른 VM은
실물 PASS 환경으로 인정하지 않는다.

## 1. 전원과 USB

- Raspberry Pi 5에는 5 V/5 A, 27 W급 전원을 사용한다.
- B210 두 대를 각각 USB 3 포트에 연결하되 RF cable은 아직 연결하지 않는다.
- 다음 결과는 각각 `usb_max_current_enable=1`, `throttled=0x0`, 두 개 이상의 `5000M`
  link여야 한다.

```bash
vcgencmd get_config usb_max_current_enable
vcgencmd get_throttled
lsusb -t
```

## 2. 설치와 package manifest

```bash
sudo apt update
sudo apt install python3 python3-venv gnuradio uhd-host usbutils
python3 -m venv --system-site-packages .venv-b210
source .venv-b210/bin/activate
python3 -m pip install -e ".[dsp,test]"

python3 tools/capture_stage3_b210_ubuntu_apt_manifest.py \
  --output configs/stage3_b210_ubuntu_apt_manifest.json
```

첫 캡처가 hold 누락으로 실패하면 출력된 `sudo apt-mark hold ...` 명령을 실행한 뒤 다시
캡처한다. 최종 manifest의 `outcome`은 `PASS`여야 한다.

## 3. RF 없는 두 장치 preflight

```bash
python3 tools/run_stage3_b210_no_tx_preflight.py \
  --ship-serial 34D03DD \
  --control-serial 34D6357 \
  --package-manifest configs/stage3_b210_ubuntu_apt_manifest.json \
  --output results/stage3_b210_no_tx_preflight.json
```

이 단계는 adapter `start()`를 호출하지 않으며 TC-RADIO-012/013 PASS를 만들지 않는다.

## 4. 30 dB fixture gate

두 방향 모두 `TX/RX -> 30 dB attenuator -> RX2`로 연결한다. 실제 송신 전에는 다음을
`configs/stage3_b210_fixture_manifest.30db-draft.json`에 반영하고 `outcome`을 `PASS`로
만들어야 한다.

- 감쇠기 식별정보, 주파수 범위, 허용오차와 보증 최소 감쇠
- B210 serial별 gain 0 dB·두 중심주파수·고정 waveform TX 출력 교정 상한
- calibration artifact 경로와 SHA-256
- `P_TX_BOUND - L_PATH_MIN <= -20 dBm`

placeholder나 음수 sentinel이 남아 있으면 hardware runner가 송신 전에 거부한다.

## 5. 실물 시험

```bash
export VDETER_B210_HARDWARE_TEST=1
export VDETER_B210_USB_DISCONNECT_TEST=1
export VDETER_B210_USB_DISCONNECT_DELAY_SECONDS=15
export VDETER_B210_SHIP_SERIAL=34D03DD
export VDETER_B210_CONTROL_SERIAL=34D6357
export VDETER_B210_FIXTURE_MANIFEST=<absolute_fixture_manifest_path>
export VDETER_B210_PACKAGE_MANIFEST=<absolute_apt_manifest_path>

python3 tools/run_stage3_b210_hardware_tests.py \
  --run-id stage3-radio-b210-pi5-30db-<YYYYMMDD>
```

controlled disconnect 안내 시 control B210의 USB만 분리한다. TX 중 RF cable이나 attenuator를
분리하지 않는다. TC-RADIO-012/013과 최종 audit가 모두 PASS하기 전에는 Stage 3를
`VERIFIED/FROZEN`으로 변경하지 않는다.
