"""rr-p1 IQ 덤프 오프라인 분석 도구 (계측 전용, 시험 판정에 사용하지 않음).

사용:
  python tools/stage7_iq_replay.py profile <window.cf32> <window.json>
      — 1,024-sample 구간 RMS 프로파일과 기대 slot 경계 비교 (버스트 위치/결손)
  python tools/stage7_iq_replay.py align <window.cf32> <window.json> [slot_index]
      — 해당 slot 주변에서 canonical burst와의 상관 최대 lag(오프셋)과 CFO 스캔
  python tools/stage7_iq_replay.py decode <slot.cf32> <slot.json> [shift_samples] [cfo_hz]
      — frozen bridge+facade로 로컬 decode 재생 (오프셋/CFO 보정 실험)
"""

from __future__ import annotations

import cmath
import json
import math
import os
import struct
import sys

VDETER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (VDETER_ROOT, os.path.join(VDETER_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_cf32(path):
    with open(path, "rb") as fh:
        data = fh.read()
    count = len(data) // 8
    values = struct.unpack(f"<{count * 2}f", data[: count * 8])
    return [complex(values[2 * i], values[2 * i + 1]) for i in range(count)]


def rms_dbfs(chunk):
    if not chunk:
        return float("-inf")
    acc = sum((c.real * c.real + c.imag * c.imag) for c in chunk) / len(chunk)
    return 10.0 * math.log10(acc) if acc > 0 else float("-inf")


def cmd_profile(cf32_path, meta_path):
    samples = load_cf32(cf32_path)
    meta = json.load(open(meta_path))
    rate = meta["device_rate"]
    buffer_first = meta.get("buffer_first_hardware_ns")
    slot_times = meta.get("slot_hardware_ns") or []
    slot_samples = meta["slot_samples"]
    chunk = 1024
    print(f"samples={len(samples)} rate={rate} slots={len(slot_times)}")
    # 기대 slot 경계 (buffer 기준 sample offset)
    expected = []
    for t in slot_times:
        off = round((t - buffer_first) * rate / 1e9)
        expected.append(off)
    print("expected slot offsets:", expected[:16])
    # 에너지 프로파일에서 버스트 상승 에지 검출
    levels = []
    for i in range(0, len(samples) - chunk, chunk):
        levels.append((i, rms_dbfs(samples[i:i + chunk])))
    finite = [l for _, l in levels if l > -120]
    if not finite:
        print("no signal")
        return
    floor = sorted(finite)[len(finite) // 10]
    thresh = floor + 6.0
    edges = []
    prev_on = False
    for i, level in levels:
        on = level > thresh
        if on and not prev_on:
            edges.append(i)
        prev_on = on
    print(f"noise_floor~{floor:.1f}dBFS thresh={thresh:.1f} rising_edges={len(edges)}")
    for e in edges[:20]:
        nearest = min(expected, key=lambda x: abs(x - e)) if expected else None
        print(f"  edge@{e}  nearest_slot_off={nearest}  delta_samples={e - nearest if nearest is not None else 'n/a'}")


def _reference_device_burst():
    from vdeter.stage7.hw_operations import link11_calibration_burst_device_bytes
    return load_cf32.__wrapped__ if False else None


def reference_burst():
    from vdeter.stage7.hw_operations import link11_calibration_burst_device_bytes
    data = link11_calibration_burst_device_bytes()
    count = len(data) // 8
    values = struct.unpack(f"<{count * 2}f", data[: count * 8])
    return [complex(values[2 * i], values[2 * i + 1]) for i in range(count)]


def correlate_at(samples, ref, lag, stride=4):
    acc = 0j
    energy = 0.0
    n = len(ref)
    for k in range(0, n, stride):
        idx = lag + k
        if idx >= len(samples):
            break
        s = samples[idx]
        acc += s * ref[k].conjugate()
        energy += abs(s) ** 2
    if energy <= 0:
        return 0.0
    return abs(acc) / math.sqrt(energy * sum(abs(r) ** 2 for r in ref[::stride]))


def cmd_align(cf32_path, meta_path, slot_index=6):
    samples = load_cf32(cf32_path)
    meta = json.load(open(meta_path))
    rate = meta["device_rate"]
    buffer_first = meta.get("buffer_first_hardware_ns")
    slot_times = meta.get("slot_hardware_ns") or [meta.get("slot_first_hardware_ns")]
    slot_samples = meta["slot_samples"]
    ref = reference_burst()
    t = slot_times[min(slot_index, len(slot_times) - 1)]
    base = round((t - buffer_first) * rate / 1e9) if buffer_first is not None else 0
    print(f"slot_index={slot_index} expected_offset={base}")
    best = None
    span = slot_samples  # ±1 slot 탐색
    for lag in range(max(0, base - span), min(len(samples) - 64, base + span), 16):
        score = correlate_at(samples, ref, lag, stride=8)
        if best is None or score > best[1]:
            best = (lag, score)
    coarse_lag, coarse_score = best
    fine_best = None
    for lag in range(max(0, coarse_lag - 24), coarse_lag + 24):
        score = correlate_at(samples, ref, lag, stride=4)
        if fine_best is None or score > fine_best[1]:
            fine_best = (lag, score)
    lag, score = fine_best
    print(f"correlation peak lag={lag} (delta={lag - base} samples, {1e6 * (lag - base) / rate:.1f}us) score={score:.3f}")
    # CFO 스캔 (±2kHz, 100Hz 격자)
    best_cfo = None
    for cfo in range(-2000, 2001, 100):
        rotated = [
            samples[lag + k] * cmath.exp(-2j * math.pi * cfo * k / rate)
            for k in range(0, min(len(ref), len(samples) - lag), 8)
        ]
        refs = ref[::8][: len(rotated)]
        num = abs(sum(a * b.conjugate() for a, b in zip(rotated, refs)))
        if best_cfo is None or num > best_cfo[1]:
            best_cfo = (cfo, num)
    print(f"CFO scan best={best_cfo[0]}Hz")


def cmd_decode(cf32_path, meta_path, shift=0, cfo_hz=0.0):
    meta = json.load(open(meta_path))
    rate = meta["device_rate"]
    slot_samples = meta["slot_samples"]
    samples = load_cf32(cf32_path)
    if shift:
        samples = samples[shift:] + [0j] * shift
    if cfo_hz:
        samples = [
            s * cmath.exp(-2j * math.pi * cfo_hz * k / rate)
            for k, s in enumerate(samples)
        ]
    data = b"".join(struct.pack("<2f", s.real, s.imag) for s in samples[:slot_samples])
    from vdeter.stage7.testing.software_graph import build_software_lane_stack  # type: ignore

    print("(decode replay는 software graph 헬퍼 확인 후 연결)")


def main():
    cmd = sys.argv[1]
    if cmd == "profile":
        cmd_profile(sys.argv[2], sys.argv[3])
    elif cmd == "align":
        cmd_align(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 6)
    elif cmd == "decode":
        cmd_decode(sys.argv[2], sys.argv[3],
                   int(sys.argv[4]) if len(sys.argv) > 4 else 0,
                   float(sys.argv[5]) if len(sys.argv) > 5 else 0.0)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
