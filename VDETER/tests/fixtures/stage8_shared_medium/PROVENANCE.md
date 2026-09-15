# Stage 5 shared-medium byte-pinned IQ fixture provenance
- reference decoder: `WSM-REF-DECODER-1` (QPSK, A=0.6, 1 sample/symbol, hard-decision boundary 0, CRC32)
- virtual frontend: `WSM-VFE-1` (per-axis clip to +/-1.0, quantization=none)
- sample format: `cf32le` (interleaved float32 LE, 8 bytes/sample)
- collision arithmetic: linear complex sum on overlapping same-channel samples, passthrough (copy) on non-overlap; clip only composite (>=2 contributor) samples.
- payloads: P1=b'WSM-POS-ROC-0001', P2=b'WSM-POS-MASSA-02', P3=b'WSM-NEG-ALT--003' (P1/P3 동일 길이 정렬)

| fixture | 생성 근거 | expected decode | SHA-256 |
| --- | --- | --- | --- |
| `src_clean_lower_p1.iq` | encode_payload(P1) 단일 clean burst (lower) — positive control | RECOVERED 'WSM-POS-ROC-0001' | `9be2876003087b8f5d558ee396590f64845c35ea540506bc41a7d0703c752471` |
| `src_clean_upper_p2.iq` | encode_payload(P2) 단일 clean burst (upper) — positive control | RECOVERED 'WSM-POS-MASSA-02' | `8d26183d561b1b31488b2416ec37ddb45ec3a69243087674264bd1eedeb2eb1f` |
| `src_alt_lower_p3.iq` | encode_payload(P3) 단일 clean burst (lower) — 대체 payload | RECOVERED 'WSM-NEG-ALT--003' | `8d6b73324ac5e2d017e11b4f6226bdc539047b1bf1a0800a35b37e920e443fff` |
| `composite_capture_lower.iq` | P1+P1 완전 겹침 합성 (capture, clipping) — positive control | RECOVERED 'WSM-POS-ROC-0001' | `feeb29f0ee48d4e5916732830b17a56f246fe55df1ce5df83576e3f511d53a8e` |
| `composite_collision_lower.iq` | P1+P3 완전 겹침 합성 (collision) — negative control | CRC_MISMATCH | `eb319be79f0499e2b03ad29a8074ad8f997b5ece5454b182079a4eca702bac1b` |
| `composite_partial_lower.iq` | P1+P3(offset 40) 부분 겹침 합성 — negative control | CRC_MISMATCH | `a6a4ed78c94f96cff3a1cdca04fb1df7e574c5b61302401dbc7330bfc9dc6a0a` |
