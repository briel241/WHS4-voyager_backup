# Link ID 19 stream split policy (v2)

공통 §7.9: region ramp [0,128), training [128,236), ID [236,300), data [300,7808), ramp-down [7808,7936), guard [7936,8192).
각 cut `c`는 첫 block `[0,c)`, 둘째 block `[c,8192)`이며 `CONTIGUOUS`/`GAP_BEFORE`/`UNKNOWN` 세 변형을 정확히 한 번 실행한다 (합계 63 subcases).
`CONTIGUOUS`는 unsplit 결과와 Packet bytes·Link ID·burst timestamp가 exact-equivalent이고 반환 횟수 1, `GAP_BEFORE`/`UNKNOWN`은 이전 partial 폐기와 무중복(둘째 block 단독으로 완전한 burst가 없으면 정상 `NO_BURST_DETECTED`)이다.
둘째 block의 기대 epoch는 exact rational `t0 + c/Fs`의 half-away 반올림 1회다.
중복 sample artifact는 만들지 않는다 (`tx_samples_v2.cf32le`에서 파생).

| cut index | 2nd-block epoch offset (ns, Fs=307200) |
| ---: | ---: |
| `64` | `208333` |
| `127` | `413411` |
| `128` | `416667` |
| `129` | `419922` |
| `182` | `592448` |
| `235` | `764974` |
| `236` | `768229` |
| `237` | `771484` |
| `268` | `872396` |
| `299` | `973307` |
| `300` | `976563` |
| `301` | `979818` |
| `4054` | `13196615` |
| `7807` | `25413411` |
| `7808` | `25416667` |
| `7809` | `25419922` |
| `7872` | `25625000` |
| `7935` | `25830078` |
| `7936` | `25833333` |
| `7937` | `25836589` |
| `8064` | `26250000` |
