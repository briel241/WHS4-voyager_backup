"""§13.5 변환 경계 gate 실행기 (`TC-PHY-MULTI-011` 입력).

요구사항: `stage6_phy_common_baseline.md` §13.5·§11 —
1. 결정적 literal gate: 97-tap count/SHA-256/대칭, branch DC 이득, `sum(g*g)`,
   impulse vector cf32le SHA-256, exact count, 재실행 결정성.
2. stopband gate: `omega_q=q*pi/1,000,000`, `q=310000..1000000` 전 grid에서
   MPFR 256-bit(`pi`,`sin`,`cos`,`sqrt`,`log10`)와 tap-index 오름차순 합으로
   `20*log10|H|`를 계산해 전 grid 최대가 `-87.0 dB` 이하인지 확인.
3. bridge noise-only: domain `VDETER-PHY11-BRIDGE-NOISE-V1`, seed
   `0x5644455445521100 + sequence(0..999)`의 `sha256-counter-box-muller-mpfr256-v2`
   표준정규를 추가 scale 없이 I/Q에 배정한 8,192-sample device window 1,000개를
   데시메이션해 Link 11 receiver에 전달 — 모두 정상 `NO_BURST_DETECTED`.

병렬화는 독립 grid point/window의 프로세스 분배뿐이며 산술을 바꾸지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

VDETER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VDETER_ROOT / "src"))
sys.path.insert(0, str(VDETER_ROOT / "tools"))

STOPBAND_Q_FIRST = 310_000
STOPBAND_Q_LAST = 1_000_000
STOPBAND_LIMIT_DB = -87.0
BRIDGE_NOISE_DOMAIN = b"VDETER-PHY11-BRIDGE-NOISE-V1"
BRIDGE_NOISE_SEED_BASE = 0x5644455445521100
BRIDGE_NOISE_WINDOWS = 1_000


def _decimation_taps():
    from vdeter.phy import resampling

    scale = 0.0
    for value in resampling.PROTOTYPE_TAPS:
        scale += value
    return tuple(value / scale for value in resampling.PROTOTYPE_TAPS)


# --- stopband worker --------------------------------------------------------

def stopband_chunk(q_lo: int, q_hi: int) -> dict:
    from mpmath import mp, mpf, cos, log10, pi, sin, sqrt

    mp.prec = 256
    taps = _decimation_taps()
    worst_db = -1e9
    worst_q = None
    for q in range(q_lo, q_hi + 1):
        omega = pi * q / 1_000_000
        acc_re = mpf(0)
        acc_im = mpf(0)
        for n, tap in enumerate(taps):
            angle = omega * n
            acc_re += mpf(tap) * cos(angle)
            acc_im -= mpf(tap) * sin(angle)
        level = float(20 * log10(sqrt(acc_re * acc_re + acc_im * acc_im)))
        if level > worst_db:
            worst_db = level
            worst_q = q
    return {"kind": "stopband", "q_lo": q_lo, "q_hi": q_hi,
            "worst_db": worst_db, "worst_q": worst_q}


# --- bridge noise-only worker ----------------------------------------------

def bridge_noise_chunk(seq_lo: int, seq_hi: int) -> dict:
    from run_phy_awgn_tests import generate_normal_stream
    from vdeter.common.contracts.phy import PhyDecodeRequest
    from vdeter.common.contracts.radio import (
        SAMPLE_FORMAT_CF32LE,
        SampleBlock,
        SampleContinuity,
    )
    from vdeter.common.identifiers import (
        DeterministicIdSource,
        LinkId,
        PhysicalChannel,
    )
    from vdeter.common.time import ClockDomain, TimePoint
    from vdeter.phy.multilink import MultiRatePhy
    from vdeter.profiles.link_phy import build_combined_phy_profile_registry

    class _StaticTime:
        def __init__(self) -> None:
            self._utc = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "gate-utc")
            self._mono = TimePoint(10, 0, ClockDomain.MONOTONIC, "gate-monotonic")

        def now_utc(self):
            return self._utc

        def now_monotonic(self):
            return self._mono

    registry = build_combined_phy_profile_registry().value
    assert registry.freeze().is_ok
    phy = MultiRatePhy(registry, _StaticTime(), DeterministicIdSource(0xBF))
    from vdeter.runtime.phy_bridge import RuntimePhyRadioBridge

    bridge = RuntimePhyRadioBridge(phy)
    channel = PhysicalChannel("VDETER_UPPER_100_TEST")
    first_sample_at = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "gate-utc")
    pack = struct.Struct("<2f").pack

    empty = 0
    failures = {}
    for sequence in range(seq_lo, seq_hi + 1):
        normals = generate_normal_stream(
            BRIDGE_NOISE_DOMAIN, BRIDGE_NOISE_SEED_BASE + sequence, 8_192 * 2
        )
        out = bytearray()
        for index in range(8_192):
            # §13.5: binary64 표준정규를 추가 scale 없이 배정, binary32 RNE 1회.
            out += pack(normals[2 * index], normals[2 * index + 1])
        phy.reset_stream()
        prepared = bridge.to_native_decode_request(
            PhyDecodeRequest(
                channel=channel,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=307_200,
                    sample_count=8_192,
                    data=bytes(out),
                    first_sample_at=first_sample_at,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=(LinkId(11),),
            )
        )
        if prepared.is_err:
            key = f"BRIDGE_{prepared.error.code}"
            failures[key] = failures.get(key, 0) + 1
            continue
        decoded = phy.decode_samples(prepared.value)
        if decoded.is_ok and len(decoded.value.packets) == 0:
            empty += 1
        else:
            key = (
                f"PACKET_{len(decoded.value.packets)}"
                if decoded.is_ok
                else str(decoded.error.code)
            )
            failures[key] = failures.get(key, 0) + 1
    return {"kind": "bridge_noise", "seq_lo": seq_lo, "seq_hi": seq_hi,
            "empty": empty, "failures": failures}


def _dispatch(task):
    if task[0] == "stopband":
        return stopband_chunk(task[1], task[2])
    return bridge_noise_chunk(task[1], task[2])


# --- 결정적 literal gate (§13.5 단위 항목, 프로세스 1개로 즉시) ---------------

def deterministic_gates() -> dict:
    from vdeter.phy import resampling
    from vdeter.phy.burst.rrc import to_cf32le

    taps = resampling.PROTOTYPE_TAPS
    tap_bytes = b"".join(struct.pack("<d", value) for value in taps)
    branch_gains = []
    for phase in range(4):
        raw = [4 * taps[i] for i in range(phase, 97, 4)]
        scale = 0.0
        for value in raw:
            scale += value
        gain = 0.0
        for value in raw:
            gain += value / scale
        branch_gains.append(gain)
    scale = 0.0
    for value in taps:
        scale += value
    decim_gain = 0.0
    energy = 0.0
    for value in taps:
        normalized = value / scale
        decim_gain += normalized
        energy += normalized * normalized

    native_impulse = [0j] * resampling.NATIVE_SLOT_SAMPLES
    native_impulse[1024] = 1 + 0j
    interp_hash = hashlib.sha256(
        to_cf32le(resampling.interpolate_slot(native_impulse))
    ).hexdigest()
    device_impulse = [0j] * resampling.DEVICE_SLOT_SAMPLES
    device_impulse[4096] = 1 + 0j
    decim_hash = hashlib.sha256(
        to_cf32le(resampling.decimate_slot(device_impulse))
    ).hexdigest()
    repeat_hash = hashlib.sha256(
        to_cf32le(resampling.interpolate_slot(native_impulse))
    ).hexdigest()

    checks = {
        "tap_count": (len(taps), 97),
        "tap_sha256": (
            hashlib.sha256(tap_bytes).hexdigest(),
            resampling.PROTOTYPE_TAP_SHA256,
        ),
        "tap_symmetry": (
            tuple(resampling.PROTOTYPE_TAP_BINARY64_BITS)
            == tuple(reversed(resampling.PROTOTYPE_TAP_BINARY64_BITS)),
            True,
        ),
        "branch_dc_gains": (
            branch_gains,
            [1.0, 1.0000000000000004, 1.0, 1.0000000000000002],
        ),
        "decimation_dc_gain": (decim_gain, 0.9999999999999998),
        "sum_g_squared": (energy, 0.23928822132142086),
        "interp_impulse_sha256": (
            interp_hash,
            "e83069a99ab9d1aac8199512cad7353db4d0ba2ddc5a1cfaf130455ac63f3d4c",
        ),
        "decim_impulse_sha256": (
            decim_hash,
            "6c0d0965ddba3d2dfcd0742615c0e987d98f2fde7ecf8e256e5ca976bc54ac8c",
        ),
        "rerun_determinism": (repeat_hash == interp_hash, True),
    }
    return {
        name: {"observed": observed, "expected": expected, "pass": observed == expected}
        for name, (observed, expected) in checks.items()
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--run-id", default="stage6-resampler-gate")
    parser.add_argument("--stopband-chunk", type=int, default=5_000)
    parser.add_argument("--noise-chunk", type=int, default=25)
    parser.add_argument("--skip-stopband", action="store_true")
    parser.add_argument("--skip-bridge-noise", action="store_true")
    arguments = parser.parse_args(argv)

    run_dir = VDETER_ROOT / "results" / "runs" / arguments.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    literal = deterministic_gates()
    literal_pass = all(item["pass"] for item in literal.values())
    print(f"[literal] {'PASS' if literal_pass else 'FAIL'} "
          f"({sum(1 for i in literal.values() if i['pass'])}/{len(literal)})", flush=True)

    tasks = []
    if not arguments.skip_stopband:
        q = STOPBAND_Q_FIRST
        while q <= STOPBAND_Q_LAST:
            tasks.append(("stopband", q, min(q + arguments.stopband_chunk - 1, STOPBAND_Q_LAST)))
            q += arguments.stopband_chunk
    if not arguments.skip_bridge_noise:
        for lo in range(0, BRIDGE_NOISE_WINDOWS, arguments.noise_chunk):
            tasks.append(("bridge_noise", lo, min(lo + arguments.noise_chunk, BRIDGE_NOISE_WINDOWS) - 1))

    stopband_worst = (-1e9, None)
    noise_empty = 0
    noise_failures = {}
    completed = 0
    with ProcessPoolExecutor(max_workers=arguments.workers) as pool:
        futures = [pool.submit(_dispatch, task) for task in tasks]
        for future in as_completed(futures):
            record = future.result()
            completed += 1
            if record["kind"] == "stopband":
                if record["worst_db"] > stopband_worst[0]:
                    stopband_worst = (record["worst_db"], record["worst_q"])
            else:
                noise_empty += record["empty"]
                for key, value in record["failures"].items():
                    noise_failures[key] = noise_failures.get(key, 0) + value
            if completed % 20 == 0 or completed == len(tasks):
                print(f"[gate] {completed}/{len(tasks)} tasks, {time.time()-started:,.0f}s", flush=True)

    stopband_pass = (
        arguments.skip_stopband or stopband_worst[0] <= STOPBAND_LIMIT_DB
    )
    noise_pass = arguments.skip_bridge_noise or (
        noise_empty == BRIDGE_NOISE_WINDOWS and not noise_failures
    )
    summary = {
        "run": {
            "run_id": arguments.run_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - started, 1),
            "workers": arguments.workers,
        },
        "literal_gates": literal,
        "stopband": None if arguments.skip_stopband else {
            "grid": f"q={STOPBAND_Q_FIRST}..{STOPBAND_Q_LAST}, omega=q*pi/1e6",
            "arithmetic": "mpfr256-rne pi/sin/cos/sqrt/log10, tap-index ascending sum",
            "worst_db": stopband_worst[0],
            "worst_q": stopband_worst[1],
            "limit_db": STOPBAND_LIMIT_DB,
            "pass": stopband_pass,
        },
        "bridge_noise_only": None if arguments.skip_bridge_noise else {
            "domain": BRIDGE_NOISE_DOMAIN.decode(),
            "seed_base": hex(BRIDGE_NOISE_SEED_BASE),
            "windows": BRIDGE_NOISE_WINDOWS,
            "empty": noise_empty,
            "failures": noise_failures,
            "pass": noise_pass,
        },
        "overall_pass": literal_pass and stopband_pass and noise_pass,
    }
    result_path = run_dir / "result.json"
    result_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"[evidence] {result_path}", flush=True)
    print(f"[stopband] worst {stopband_worst[0]:.4f} dB @ q={stopband_worst[1]} "
          f"({'PASS' if stopband_pass else 'FAIL'})", flush=True)
    print(f"[bridge-noise] {noise_empty}/{BRIDGE_NOISE_WINDOWS} empty "
          f"({'PASS' if noise_pass else 'FAIL'})", flush=True)
    return 0 if summary["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
