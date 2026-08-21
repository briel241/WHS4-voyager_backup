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

## 4. 사용자 승인 명목 30 dB 유선 fixture gate

현재 두 B210과 보유한 감쇠기 두 개에는 다음의 제한된 기능시험 profile을 적용한다.

- ship B210은 serial `34D03DD`, control B210은 serial `34D6357`로 고정한다.
- 두 방향 모두 `TX/RX -> 명목 30 dB 감쇠기 하나 -> RX2`로 연결한다.
- 감쇠기 A와 B를 서로 구별해 각각 한 방향에만 고정하고, 안테나는 연결하지 않는다.
- TX gain은 양쪽 모두 `0 dB`로 유지한다. 실제 수신 방향인 ship upper RX와 control lower
  RX gain은 Pi/B210 bring-up에서 known-vector 복원이 확인된 `50 dB`로 고정한다.
  중심주파수는 `157237500 Hz`와 `161837500 Hz`만 허용한다.
- fixture manifest는
  `configs/stage3_b210_fixture_manifest.30db-user-accepted.json`을 사용한다.

이 profile은 감쇠기 라벨의 `30 dB, 2 W, 최대 6 GHz`만 사용자가 수락한 조건이다. 실제
삽입손실·허용오차와 B210 절대 출력은 교정되지 않았으므로 수신기 보호, RF 안전, 출력전력,
감도, 방사·스펙트럼·OTA 적합성을 입증하지 않는다. manifest의 보증 최소 감쇠는 의도적으로
`0 dB`이고 hardware runner는 이 profile을 `FUNCTIONAL_CONDUCTED_ONLY`로 기록한다.

다른 serial, gain, 주파수, 안테나, 감쇠기 구성으로 바꾸면 이 승인은 무효다. 수신기 보호
margin까지 주장하려면 `CALIBRATED_PROTECTION` profile과 교정 artifact를 별도로 사용해야
한다.

## 5. 실물 시험

controlled USB disconnect를 의도적으로 제외하는 중간 evidence run은 disconnect flag를
설정하지 않고 다음처럼 실행한다. 이 결과는 `PARTIAL_PASS`이며 TC-RADIO-012 또는 전체
hardware gate를 PASS로 만들지 않는다.

```bash
export VDETER_B210_HARDWARE_TEST=1
unset VDETER_B210_USB_DISCONNECT_TEST
export VDETER_B210_SHIP_SERIAL=34D03DD
export VDETER_B210_CONTROL_SERIAL=34D6357
export VDETER_B210_FIXTURE_MANIFEST="$PWD/configs/stage3_b210_fixture_manifest.30db-user-accepted.json"
export VDETER_B210_PACKAGE_MANIFEST=<absolute_apt_manifest_path>

python3 tools/run_stage3_b210_hardware_tests.py \
  --run-id stage3-radio-b210-pi5-30db-no-disconnect-<YYYYMMDD> \
  --exclude-controlled-usb-disconnect
```

전체 gate는 별도 승인 아래 controlled disconnect까지 포함해 실행한다.

production adapter는 GNU Radio/UHD flowgraph를 parent test process가 아니라 별도 spawned
subprocess에서 실행한다. 따라서 UHD/libusb native thread가 USB 분리 때 abort하더라도 runner는
계속 살아 있어야 하며, child EOF를 `RADIO_DEVICE_DISCONNECTED`로 기록한 뒤 cleanup해야 한다.
아래 시험 직전에는 두 B210을 모두 재연결하고 `uhd_find_devices`에서 두 serial이 보이는지
확인한다.

```bash
export VDETER_B210_HARDWARE_TEST=1
export VDETER_B210_USB_DISCONNECT_TEST=1
export VDETER_B210_USB_DISCONNECT_DELAY_SECONDS=15
export VDETER_B210_SHIP_SERIAL=34D03DD
export VDETER_B210_CONTROL_SERIAL=34D6357
export VDETER_B210_FIXTURE_MANIFEST="$PWD/configs/stage3_b210_fixture_manifest.30db-user-accepted.json"
export VDETER_B210_PACKAGE_MANIFEST=<absolute_apt_manifest_path>

python3 tools/run_stage3_b210_hardware_tests.py \
  --run-id stage3-radio-b210-pi5-30db-<YYYYMMDD>
```

controlled disconnect 안내 시 control B210의 USB만 분리한다. TX 중 RF cable이나 attenuator를
분리하지 않는다. 결과 evidence에는 서로 다른 `parent_pid`·`gr_uhd_child_pid`,
`parent_survived_native_disconnect=true`, typed terminal과 `cleanup=PASS`가 있어야 한다.
TC-RADIO-012/013과 최종 audit가 모두 PASS하기 전에는 Stage 3를
`VERIFIED/FROZEN`으로 변경하지 않는다.
