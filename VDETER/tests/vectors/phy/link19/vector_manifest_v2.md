# Link ID 19 vector manifest (v2)

## Identity

- link baseline: `VDETER-STAGE6-PHY19-BL-001` v`0.8.0`, profile `phy19-markdown-v5`
- common baseline: `VDETER-STAGE6-PHY-COMMON-BL-001` v`0.7.1`
- combined registry: `stage6-phy11-17-19-v6`, fingerprint `fbed7c508b22a75a7d390344a1d3073b8f5b92f21f0266c6f6e752ad9996a673`
- Link 11 base registry fingerprint: `ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898`

## Generator

- id/version: `generate-phy17-19-vectors` `2.0.0`
- source SHA-256: `68859f3320664e7a852f39592615ee8c6a99ba6bfede5b259eae6e03714d3da9`
- production import 금지: 이 generator는 표준 라이브러리와 mpmath만 사용하며 `vdeter` package를 import하지 않는다.
- 생성 명령: `.venv/Scripts/python.exe tools/generate_phy17_19_vectors.py`

## RNG

- Packet bytes: `sha256-counter-bytes-v2` (loopback domain `VDETER-PHY{L}-LOOPBACK-PACKET-V2`, impairment domain `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2`, 공통 §11.1의 seed·소비 순서)
- 표준정규: `sha256-counter-box-muller-mpfr256-v2` (AWGN/noise-only, §7.7)
- 회전각: `mpfr256-direct-angle-binary64-rne-v1`, dB scale: `mpfr256-db-to-binary64-rne-v1`

## Artifacts

| path | role | media type | bytes | format/tolerance | SHA-256 |
| --- | --- | --- | ---: | --- | --- |
| `awgn_policy_v2.md` | AWGN policy | text/markdown | 1094 | exact | `2fdf38f869ac24ca51dd8316600602ea59a6f14ad4af77b0526f0128e2a8bbf6` |
| `impairment_policy_v2.md` | impairment policy | text/markdown | 1129 | exact | `445f7066e43fdadb13d199b704842c63b073d9e637bab3038616403284537577` |
| `official_constants_v2.md` | official constants | text/markdown | 546 | exact | `098f76528d01290a0409308239f1d1e9cbdd3818ba6c341c2c8c93332f70d3c8` |
| `packet_type0_v2.hex` | canonical Packet | text/plain | 1405 | byte-exact | `155f78a71ec7eb0fe02a7f7ea9371f5778e4c968f9804a3c6a555cefa78db64a` |
| `rx_samples_v2/cfo_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `a669a4b288c066edb2469b1a1be3dc153b0a8afce0aa81268cd1f84a0e1e2003` |
| `rx_samples_v2/cfo_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `5eb154af24eef49d9239bd58faee361d970a75248116a556e699cbd109a49946` |
| `rx_samples_v2/gain_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `455258097ab4bb553aa8d83c8293e07b9ce087dad2fdf0801d44382af34f26d4` |
| `rx_samples_v2/gain_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `e98e711981dbc23ada6435a3a4af0305a34e0837fb4bcc09aac1412a14ee7e12` |
| `rx_samples_v2/ideal.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `6be378ee5ad6409740c8f84d47e82b8580153007b13aa466b4a37a67895b2f09` |
| `rx_samples_v2/mixed.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `ee283ebf1291f69db6dc9071ef310a126564d1952d0d1d5d13fe73364ca54f17` |
| `rx_samples_v2/phase_wrap.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `6ebefbe402a49db69308d205fe4858ba76f3c4db5dc4ca88866fe7c8933f8a9f` |
| `rx_samples_v2/sro_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `ea26a0057b370a0e8f82418789e8ceba2c1a59bcc2a946e1cdebf848ad09e3a0` |
| `rx_samples_v2/sro_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `af5fefdc64f38ab3ce96a5961eb7600a9765bd259b924e73a61508b0c7f8ede9` |
| `rx_samples_v2/timing_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `e657be95e1d1b92807c4ec6539d409968575a9a6c11bb13d4ca6774407c4861c` |
| `stream_split_policy_v2.md` | stream split policy | text/markdown | 1316 | exact | `4a5dc9c50b0196390b8b4a70042cbaf72c7610b07c458529b90bdc16cc1fd718` |
| `tx_samples_v2.cf32le` | canonical slot | application/octet-stream | 65536 | cf32le byte-exact | `6be378ee5ad6409740c8f84d47e82b8580153007b13aa466b4a37a67895b2f09` |
| `tx_stages_v2.md` | TX stage bits | text/markdown | 21064 | bit-exact | `acc3097184c442464311068ea896f7479ca0bf572dfad088666d00bf9fbbcb8c` |
| `tx_symbols_v2.md` | body symbols | text/markdown | 86933 | binary64 bit-exact | `1c9b6bc9795e6a392735d76f929ebeab761bff3af9056e51ffb0dc0f751d6e20` |
| `SHA256SUMS` | machine manifest | text/plain | 1649 | LF, path-ascending | `73dc86fe51a79dad4bc20d373cb87f15c823b18a7422c541b31215dd9e4a90fd` |

## 확인

- 공식 표 대조 책임: 이 manifest의 상수는 승인 기준선 v0.8.0에서 옮겨 적은 값이며 공식 PDF 대조는 기준선 승인 절차가 수행했다.
- historical v1 artifact는 이 v2 corpus로 승격하지 않았다 (`manifest_v1.json` 계열은 존재하지 않으며 새로 생성했다).
- AWGN/noise-only의 1,000-burst 실제 결과와 RF 측정값은 vector가 아니라 evidence다.
- 생성 시각(참고, identity 아님): 2026-08-23T14:02:24.100265+00:00
