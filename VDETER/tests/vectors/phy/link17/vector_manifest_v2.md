# Link ID 17 vector manifest (v2)

## Identity

- link baseline: `VDETER-STAGE6-PHY17-BL-001` v`0.9.0`, profile `phy17-markdown-v6`
- common baseline: `VDETER-STAGE6-PHY-COMMON-BL-001` v`0.8.0`
- combined registry: `stage6-phy11-17-19-v7`, fingerprint `0a2b8cb8a72727bd54c05c5dd6e6ac5aeeec7a0b51f6ebe41a72bfa2482ae5d2`
- Link 11 base registry fingerprint: `ce8760138f3a7640705b71af4500520afabf26e9fb5300db524f61a5b0114898`

## Generator

- id/version: `generate-phy17-19-vectors` `2.0.0`
- source SHA-256: `c6e25e7e6e34f18947c940c5ac24a38d3ee3a6a37a27b662893e21c6c678ef59`
- production import 금지: 이 generator는 표준 라이브러리와 mpmath만 사용하며 `vdeter` package를 import하지 않는다.
- 생성 명령: `.venv/Scripts/python.exe tools/generate_phy17_19_vectors.py`

## RNG

- Packet bytes: `sha256-counter-bytes-v2` (loopback domain `VDETER-PHY{L}-LOOPBACK-PACKET-V2`, impairment domain `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2`, 공통 §11.1의 seed·소비 순서)
- 표준정규: `sha256-counter-box-muller-mpfr256-v2` (AWGN/noise-only, §7.7)
- 회전각: `mpfr256-direct-angle-binary64-rne-v1`, dB scale: `mpfr256-db-to-binary64-rne-v1`

## Artifacts

| path | role | media type | bytes | format/tolerance | SHA-256 |
| --- | --- | --- | ---: | --- | --- |
| `awgn_policy_v2.md` | AWGN policy | text/markdown | 1333 | exact | `8ede2b6ba867c0808743aec312c3176690b6c82168b43769d110d66e7154bb76` |
| `impairment_policy_v2.md` | impairment policy | text/markdown | 1129 | exact | `443b3f5891f9825779ffd744f0a1a8aac3de3faf189d63ab070f459e3217790e` |
| `official_constants_v2.md` | official constants | text/markdown | 545 | exact | `f779877bb252b753f07c0f7d99fd76302ec7934937bf4c0d8106fd4d056c3a29` |
| `packet_type0_v2.hex` | canonical Packet | text/plain | 469 | byte-exact | `85b4180841e4d0e9d0bed4d544f6cbad96113ea4239b82686785f3b8d3e097e0` |
| `rx_samples_v2/cfo_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `79026c26e21149a6bb48aae14ae9b8b124fe803f8079c332265b7efbc5cd5ff3` |
| `rx_samples_v2/cfo_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `2b018110a061648bfa11192c56b734fbf14103dfd92b3ec1e1305aceea25a9a8` |
| `rx_samples_v2/gain_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `162e164525b8fe9cf5daaf45a31e966be5af5f99918b3401210ae3d0e58ad006` |
| `rx_samples_v2/gain_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `53d700295731e83fae1a3110365672aafcf0f09332f801f5e10b8b875bf8f816` |
| `rx_samples_v2/ideal.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `aef2920de45eb588cb3551b7ca1fc09dcf1c089c724b85662504829164c5fa19` |
| `rx_samples_v2/mixed.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `44ee8baf3dd188d5fc5bf73d129640d685a8db0cf92a5ee2ba61688524c71b84` |
| `rx_samples_v2/phase_wrap.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `bc12f0fb6bd28674019d20e75b46cc9c7bdcb3247241f692155ace69738a2677` |
| `rx_samples_v2/sro_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `67c126b4f8b020d4a7499445d2b15f4701da09f30035100d21a9f3abd43af148` |
| `rx_samples_v2/sro_min.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `8f58b2b5fe90e7b3337de4ea237e843e2825b6c603e89a422cd54e1f862c0aa7` |
| `rx_samples_v2/timing_max.cf32le` | deterministic RX samples | application/octet-stream | 65536 | cf32le byte-exact | `73836dc3b1bcc939096f1f0646830479a3357219d4b475c91e9f72929a5943d1` |
| `stream_split_policy_v2.md` | stream split policy | text/markdown | 1316 | exact | `2faebbd9097874491a3b58124f9648415d4ac5065d425ec293c8ce30cf0bd362` |
| `tx_samples_v2.cf32le` | canonical slot | application/octet-stream | 65536 | cf32le byte-exact | `aef2920de45eb588cb3551b7ca1fc09dcf1c089c724b85662504829164c5fa19` |
| `tx_stages_v2.md` | TX stage bits | text/markdown | 9812 | bit-exact | `bb542fa718dfe16132469d9544fe8865c2c80a3f2dbb40d2147f35049221ebdf` |
| `tx_symbols_v2.md` | body symbols | text/markdown | 69539 | binary64 bit-exact | `64b0b4a86b287982db9084f8eeaec8383bb85b4a60300fc2011ddff8e60aa30b` |
| `SHA256SUMS` | machine manifest | text/plain | 1649 | LF, path-ascending | `6e5e695ef50c726245da77c5bc1a7daba0c6a1b54a6ee3f801c6fd052294f3e0` |

## 확인

- 공식 표 대조 책임: 이 manifest의 상수는 승인 기준선 v0.8.0에서 옮겨 적은 값이며 공식 PDF 대조는 기준선 승인 절차가 수행했다.
- historical v1 artifact는 이 v2 corpus로 승격하지 않았다 (`manifest_v1.json` 계열은 존재하지 않으며 새로 생성했다).
- AWGN/noise-only의 1,000-burst 실제 결과와 RF 측정값은 vector가 아니라 evidence다.
- 생성 시각(참고, identity 아님): 2026-08-23T15:14:06.324326+00:00
