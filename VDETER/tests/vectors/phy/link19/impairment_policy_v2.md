# Link ID 19 impairment policy (v2)

공통 §7.7의 결정적 채널: `u[n]=cubic_interp(s,(n-delta)/rho)`, `v[n]=A*u[n]*exp(j*(phi+2*pi*f*n/Fs))`, timing/SRO → gain/phase/CFO → (AWGN 없음).
회전각 identity는 `mpfr256-direct-angle-binary64-rne-v1`, 보간은 4점 Lagrange cubic(범위 밖 zero)이다.

| case | delta(samples) | phase | CFO(Hz) | SRO(ppm) | gain |
| --- | ---: | ---: | ---: | ---: | ---: |
| `IDEAL` | `0` | `0` | `0` | `0` | `1` |
| `TIMING_MAX` | `63/16` | `0` | `0` | `0` | `1` |
| `CFO_MIN` | `0` | `0` | `-2000` | `0` | `1` |
| `CFO_MAX` | `0` | `0` | `2000` | `0` | `1` |
| `SRO_MIN` | `0` | `0` | `0` | `-10` | `1` |
| `SRO_MAX` | `0` | `0` | `0` | `10` | `1` |
| `GAIN_MIN` | `0` | `0` | `0` | `0` | `1/4` |
| `GAIN_MAX` | `0` | `0` | `0` | `0` | `4` |
| `PHASE_WRAP` | `0` | `-1*pi/1` | `0` | `0` | `1` |
| `MIXED` | `23/16` | `3*pi/7` | `1375` | `6` | `5/4` |

random Packet: domain `VDETER-PHY{L}-IMPAIRMENT-PACKET-V2`, seed `0x5644455445520000 + link_id*0x100 + case_index*0x10000 + sequence(0..63)`, generator `sha256-counter-bytes-v2` (공통 §11.1). case당 canonical 1 + random 64.
