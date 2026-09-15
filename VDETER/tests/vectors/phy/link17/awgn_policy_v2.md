# Link ID 17 AWGN policy (v2)

- core sweep: 정수 tenth-dB `-10..50` (61 points), point당 1,000 bursts, canonical Packet 반복(`REPEAT-CANONICAL-PACKET-1000`).
- gate 미발견 시 §7.7 조건부 연장(`DR-STAGE6-AWGN-001`): 같은 grid를 tenth-dB `100`(공식 `+9.0 dB`)까지 오름차순 연장하며 실행 point×1,000을 evidence에 기록한다. noise-only 지점은 바뀌지 않는다.
- `Es_tx = 1/1` (고정 TX scale 제곱), `N0=Es_tx*10^(-gamma/10)`, scale identity `mpfr256-db-to-binary64-rne-v1`.
- 난수: `sha256-counter-box-muller-mpfr256-v2`, domain `VDETER-PHY17-AWGN-V1`, seed `0x5644455445520000 + link_id*0x100 + sequence(0..999)`, burst마다 counter 0 reset, 같은 sequence는 전 SNR point에서 같은 stream.
- noise-only: tenth-dB (-10, 10, 50) 대응 N0에서 각 1,000 slots, domain `VDETER-PHY17-NOISE-V1`, 모두 정상 `NO_BURST_DETECTED`.
- 합성: canonical `cf32le` 승격 → binary64 scale·덧셈 → I/Q binary32 RNE 1회. peak 정규화 없음(`test-iq-int16-le` projection에 잡음을 더하지 않는다).
- gate 규칙: 오름차순 최초 `>=990/1000`, 바로 아래 grid `<990/1000` (§7.7). 공식 `1.0 dB`는 characterization이며 PER 합격률 주장이 아니다.
- 실행 도구: `tools/run_phy_awgn_tests.py` (1,000-burst 실제 결과는 vector가 아니라 evidence).
