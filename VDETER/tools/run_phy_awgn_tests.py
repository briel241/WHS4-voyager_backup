"""Link 17·19 AWGN characterization·noise-only 병렬 실행기.

요구사항: `stage6_phy_common_baseline.md` §7.7·§11.1, `stage6_phy_link17_baseline.md`
§12.4, `stage6_phy_link19_baseline.md` §12.5 — `TC-PHY17-015`/`TC-PHY19-015`의
AWGN 61×1,000 decode와 noise-only 3,000/3,000 입력을 만든다.

산술은 승인 기준선을 그대로 사용한다.

- 표준정규: `sha256-counter-box-muller-mpfr256-v2` (256-bit correct rounding 평가 후
  각 z를 binary64로 한 번 반올림; digest당 z0(w0,w1), z1(w0,w1), z0(w2,w3), z1(w2,w3))
- dB→scale: `mpfr256-db-to-binary64-rne-v1` (exact rational `gamma=k/10`, `Es_tx`에서
  256-bit로 `sqrt(Es_tx*10^(-gamma/10)/2)`를 평가해 binary64로 한 번 반올림)
- 합성: canonical `cf32le` sample을 binary64로 exact 승격 → binary64 scale·덧셈 →
  I/Q마다 마지막에 한 번만 binary32 RNE
- 같은 sequence는 모든 SNR point에서 같은 표준정규 stream을 사용한다.

병렬화는 독립 burst의 프로세스 분배뿐이며 어떤 산술 순서도 바꾸지 않는다.
checkpoint(JSONL)로 중단·재개를 지원한다. 이 도구는 verification 입력을 실행할 뿐
gate 판정을 공식 PER 요구로 표현하지 않는다 (§12.4/§12.5).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

VDETER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VDETER_ROOT / "src"))

SEED_BASE = 0x5644455445520000
DEVICE_SLOT_SAMPLES = 8_192
NORMALS_PER_SLOT = DEVICE_SLOT_SAMPLES * 2
SEQUENCES = 1_000
GATE_MIN_EXACT = 990

#: 공통 §7.7: 공식 point의 -2.0..+4.0 dB, 정수 tenth-dB grid.
SWEEP_TENTHS = {17: tuple(range(-10, 51)), 19: tuple(range(82, 143))}
#: 시작/공식/끝 tenth-dB (link §12.4/§12.5 noise_only_points_tenths_db).
NOISE_ONLY_TENTHS = {17: (-10, 10, 50), 19: (82, 102, 142)}
#: 고정 TX waveform scale의 제곱 `Es_tx=a^2` (exact rational).
ES_TX_RATIO = {17: (1, 1), 19: (49, 64)}


def canonical_packet(link_id: int) -> bytes:
    prefix = bytes.fromhex("00000c010203040001017f14")
    if link_id == 17:
        return prefix + b"\x51" * 218 + bytes.fromhex("7db2d803")
    return prefix + b"\x51" * 686 + bytes.fromhex("deffdbce")


# --- sha256-counter-box-muller-mpfr256-v2 ----------------------------------

def generate_normal_stream(domain: bytes, seed: int, count: int) -> array:
    from mpmath import mp, mpf, cos, ln, pi, sin, sqrt

    mp.prec = 256
    half = mpf(1) / 2
    two52 = mpf(2) ** 52
    values = array("d")
    counter = 0
    while len(values) < count:
        digest = hashlib.sha256(
            domain + struct.pack(">Q", seed) + struct.pack(">Q", counter)
        ).digest()
        counter += 1
        words = struct.unpack(">4Q", digest)
        for first, second in ((words[0], words[1]), (words[2], words[3])):
            u0 = (mpf(first >> 12) + half) / two52
            u1 = (mpf(second >> 12) + half) / two52
            radius = sqrt(-2 * ln(u0))
            theta = 2 * pi * u1
            values.append(float(radius * cos(theta)))
            values.append(float(radius * sin(theta)))
    del values[count:]
    return values


def stream_cache_path(cache_dir: Path, domain: str, sequence: int) -> Path:
    return cache_dir / f"{domain}-{sequence:04d}.f64"


def ensure_stream(cache_dir: Path, domain: str, link_id: int, sequence: int) -> Path:
    path = stream_cache_path(cache_dir, domain, sequence)
    if path.exists() and path.stat().st_size == NORMALS_PER_SLOT * 8:
        return path
    seed = SEED_BASE + link_id * 0x100 + sequence
    values = generate_normal_stream(domain.encode("ascii"), seed, NORMALS_PER_SLOT)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(values.tobytes())
    os.replace(tmp, path)
    return path


def load_stream(cache_dir: Path, domain: str, sequence: int) -> array:
    values = array("d")
    values.frombytes(stream_cache_path(cache_dir, domain, sequence).read_bytes())
    if sys.byteorder != "little":  # cache는 little-endian 저장
        values.byteswap()
    return values


# --- mpfr256-db-to-binary64-rne-v1 -----------------------------------------

def noise_scale(link_id: int, tenth_db: int) -> float:
    from mpmath import mp, mpf, sqrt

    mp.prec = 256
    numerator, denominator = ES_TX_RATIO[link_id]
    es_tx = mpf(numerator) / denominator
    n0 = es_tx * mpf(10) ** (mpf(-tenth_db) / 100)
    return float(sqrt(n0 / 2))


# --- worker ----------------------------------------------------------------

_WORKER = {}


def _worker_init(cache_root: str) -> None:
    from vdeter.common.contracts.phy import PhyDecodeRequest  # noqa: F401 (사전 import)
    from vdeter.common.identifiers import DeterministicIdSource, PhysicalChannel
    from vdeter.common.time import ClockDomain, TimePoint
    from vdeter.phy import wideband
    from vdeter.phy.burst.rrc import from_cf32le, to_cf32le
    from vdeter.phy.multilink import MultiRatePhy
    from vdeter.profiles.link_phy import build_combined_phy_profile_registry

    class _StaticTime:
        def __init__(self) -> None:
            self._utc = TimePoint(1_700_000_000, 0, ClockDomain.UTC, "awgn-utc")
            self._mono = TimePoint(10, 0, ClockDomain.MONOTONIC, "awgn-monotonic")

        def now_utc(self):
            return self._utc

        def now_monotonic(self):
            return self._mono

    registry = build_combined_phy_profile_registry().value
    frozen = registry.freeze()
    assert frozen.is_ok
    phy = MultiRatePhy(registry, _StaticTime(), DeterministicIdSource(0xAE))
    slots = {}
    packets = {}
    for link_id in (17, 19):
        packet = canonical_packet(link_id)
        stages = wideband.encode_slot(link_id, packet)
        # canonical cf32le slot을 binary32로 양자화한 뒤 binary64로 exact 승격한다.
        promoted = from_cf32le(to_cf32le(stages.samples))
        slots[link_id] = tuple((value.real, value.imag) for value in promoted)
        packets[link_id] = packet
    _WORKER.update(
        phy=phy,
        slots=slots,
        packets=packets,
        cache_root=Path(cache_root),
        channel=PhysicalChannel("VDETER_UPPER_100_TEST"),
        first_sample_at=TimePoint(1_700_000_000, 0, ClockDomain.UTC, "awgn-utc"),
    )


def _decode_block(link_id: int, data: bytes):
    from vdeter.common.contracts.phy import PhyDecodeRequest
    from vdeter.common.contracts.radio import (
        SAMPLE_FORMAT_CF32LE,
        SampleBlock,
        SampleContinuity,
    )
    from vdeter.common.identifiers import LinkId

    phy = _WORKER["phy"]
    phy.reset_stream()
    return phy.decode_samples(
        PhyDecodeRequest(
            channel=_WORKER["channel"],
            samples=SampleBlock(
                sample_format=SAMPLE_FORMAT_CF32LE,
                sample_rate_hz=307_200,
                sample_count=DEVICE_SLOT_SAMPLES,
                data=data,
                first_sample_at=_WORKER["first_sample_at"],
                continuity=SampleContinuity.CONTIGUOUS,
            ),
            allowed_link_ids=(LinkId(link_id),),
        )
    )


def run_awgn_task(link_id: int, tenth_db: int, seq_lo: int, seq_hi: int) -> dict:
    cache_dir = _WORKER["cache_root"] / f"link{link_id}-awgn"
    scale = noise_scale(link_id, tenth_db)
    slot = _WORKER["slots"][link_id]
    packet = _WORKER["packets"][link_id]
    pack = struct.Struct("<2f").pack
    exact = 0
    failures = {}
    domain = f"VDETER-PHY{link_id}-AWGN-V1"
    for sequence in range(seq_lo, seq_hi + 1):
        normals = load_stream(cache_dir, domain, sequence)
        out = bytearray()
        for index, (real64, imag64) in enumerate(slot):
            out += pack(
                real64 + scale * normals[2 * index],
                imag64 + scale * normals[2 * index + 1],
            )
        decoded = _decode_block(link_id, bytes(out))
        if (
            decoded.is_ok
            and len(decoded.value.packets) == 1
            and decoded.value.packets[0].link_id.value == link_id
            and bytes(decoded.value.packets[0].packet) == packet
        ):
            exact += 1
        else:
            if decoded.is_ok:
                count = len(decoded.value.packets)
                reason = "NO_BURST" if count == 0 else f"WRONG_RESULT_{count}"
            else:
                reason = str(decoded.error.code)
            failures[reason] = failures.get(reason, 0) + 1
    return {
        "kind": "awgn",
        "link": link_id,
        "tenth_db": tenth_db,
        "seq_lo": seq_lo,
        "seq_hi": seq_hi,
        "exact": exact,
        "failures": failures,
    }


def run_noise_only_task(link_id: int, tenth_db: int, seq_lo: int, seq_hi: int) -> dict:
    cache_dir = _WORKER["cache_root"] / f"link{link_id}-noise"
    scale = noise_scale(link_id, tenth_db)
    pack = struct.Struct("<2f").pack
    empty = 0
    failures = {}
    domain = f"VDETER-PHY{link_id}-NOISE-V1"
    for sequence in range(seq_lo, seq_hi + 1):
        normals = load_stream(cache_dir, domain, sequence)
        out = bytearray()
        for index in range(DEVICE_SLOT_SAMPLES):
            out += pack(scale * normals[2 * index], scale * normals[2 * index + 1])
        decoded = _decode_block(link_id, bytes(out))
        if decoded.is_ok and len(decoded.value.packets) == 0:
            empty += 1
        else:
            reason = (
                f"PACKET_{len(decoded.value.packets)}"
                if decoded.is_ok
                else str(decoded.error.code)
            )
            failures[reason] = failures.get(reason, 0) + 1
    return {
        "kind": "noise_only",
        "link": link_id,
        "tenth_db": tenth_db,
        "seq_lo": seq_lo,
        "seq_hi": seq_hi,
        "empty": empty,
        "failures": failures,
    }


def run_cache_task(link_id: int, domain_kind: str, seq_lo: int, seq_hi: int) -> dict:
    if domain_kind == "awgn":
        cache_dir = _WORKER["cache_root"] / f"link{link_id}-awgn"
        domain = f"VDETER-PHY{link_id}-AWGN-V1"
    else:
        cache_dir = _WORKER["cache_root"] / f"link{link_id}-noise"
        domain = f"VDETER-PHY{link_id}-NOISE-V1"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for sequence in range(seq_lo, seq_hi + 1):
        ensure_stream(cache_dir, domain, link_id, sequence)
    return {
        "kind": "cache",
        "link": link_id,
        "domain_kind": domain_kind,
        "seq_lo": seq_lo,
        "seq_hi": seq_hi,
    }


def _dispatch(task: tuple):
    kind = task[0]
    if kind == "cache":
        return run_cache_task(*task[1:])
    if kind == "awgn":
        return run_awgn_task(*task[1:])
    return run_noise_only_task(*task[1:])


# --- 실행 계획·checkpoint ---------------------------------------------------

def _chunks(total: int, size: int):
    for low in range(0, total, size):
        yield low, min(low + size, total) - 1


def _task_key(record: dict) -> str:
    return (
        f"{record['kind']}:{record['link']}:{record.get('tenth_db', record.get('domain_kind'))}"
        f":{record['seq_lo']}:{record['seq_hi']}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", default="17,19")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--run-id", default="stage6-awgn")
    parser.add_argument("--sequences", type=int, default=SEQUENCES)
    parser.add_argument("--tenths", default="", help="예: -10,0,50 (기본: 전체 sweep)")
    parser.add_argument("--chunk", type=int, default=25)
    parser.add_argument("--skip-noise-only", action="store_true")
    arguments = parser.parse_args(argv)

    links = tuple(int(value) for value in arguments.links.split(","))
    run_dir = VDETER_ROOT / "results" / "runs" / arguments.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_root = VDETER_ROOT / "results" / "awgn_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = run_dir / "checkpoint.jsonl"

    done = {}
    if checkpoint_path.exists():
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                done[_task_key(record)] = record

    tenths_by_link = {}
    for link_id in links:
        if arguments.tenths:
            tenths_by_link[link_id] = tuple(
                int(value) for value in arguments.tenths.split(",")
            )
        else:
            tenths_by_link[link_id] = SWEEP_TENTHS[link_id]

    tasks = []
    for link_id in links:  # cache 생성이 sweep보다 먼저 오도록 순서 유지
        for lo, hi in _chunks(arguments.sequences, 100):
            tasks.append(("cache", link_id, "awgn", lo, hi))
        if not arguments.skip_noise_only:
            for lo, hi in _chunks(arguments.sequences, 100):
                tasks.append(("cache", link_id, "noise", lo, hi))
    for link_id in links:
        for tenth in tenths_by_link[link_id]:
            for lo, hi in _chunks(arguments.sequences, arguments.chunk):
                tasks.append(("awgn", link_id, tenth, lo, hi))
        if not arguments.skip_noise_only:
            for tenth in NOISE_ONLY_TENTHS[link_id]:
                for lo, hi in _chunks(arguments.sequences, arguments.chunk):
                    tasks.append(("noise_only", link_id, tenth, lo, hi))

    def is_done(task: tuple) -> bool:
        if task[0] == "cache":
            key = f"cache:{task[1]}:{task[2]}:{task[3]}:{task[4]}"
        else:
            key = f"{task[0]}:{task[1]}:{task[2]}:{task[3]}:{task[4]}"
        return key in done

    cache_tasks = [task for task in tasks if task[0] == "cache" and not is_done(task)]
    sweep_tasks = [task for task in tasks if task[0] != "cache" and not is_done(task)]
    total = len(cache_tasks) + len(sweep_tasks)
    print(
        f"[plan] cache {len(cache_tasks)} tasks, sweep {len(sweep_tasks)} tasks "
        f"(checkpoint 재사용 {len(done)}), workers={arguments.workers}",
        flush=True,
    )

    started = time.time()
    completed = 0
    with checkpoint_path.open("a", encoding="utf-8") as checkpoint:
        with ProcessPoolExecutor(
            max_workers=arguments.workers,
            initializer=_worker_init,
            initargs=(str(cache_root),),
        ) as pool:
            for phase_name, phase in (("cache", cache_tasks), ("sweep", sweep_tasks)):
                futures = [pool.submit(_dispatch, task) for task in phase]
                for future in as_completed(futures):
                    record = future.result()
                    done[_task_key(record)] = record
                    checkpoint.write(json.dumps(record, ensure_ascii=False) + "\n")
                    checkpoint.flush()
                    completed += 1
                    if completed % 20 == 0 or completed == total:
                        elapsed = time.time() - started
                        print(
                            f"[{phase_name}] {completed}/{total} tasks, {elapsed:,.0f}s",
                            flush=True,
                        )

    # --- 집계 ---------------------------------------------------------------
    summary = {"links": {}}
    ok = True
    for link_id in links:
        points = []
        for tenth in tenths_by_link[link_id]:
            exact = 0
            count = 0
            failures = {}
            for lo, hi in _chunks(arguments.sequences, arguments.chunk):
                record = done.get(f"awgn:{link_id}:{tenth}:{lo}:{hi}")
                if record is None:
                    continue
                exact += record["exact"]
                count += hi - lo + 1
                for key, value in record["failures"].items():
                    failures[key] = failures.get(key, 0) + value
            points.append(
                {
                    "tenth_db": tenth,
                    "es_n0_db": tenth / 10.0,
                    "bursts": count,
                    "exact_packets": exact,
                    "failures": failures,
                }
            )
        gate = None
        full = [p for p in points if p["bursts"] == arguments.sequences]
        for index, point in enumerate(full):
            if point["exact_packets"] >= GATE_MIN_EXACT * arguments.sequences // SEQUENCES:
                below_ok = (
                    index == 0
                    or full[index - 1]["exact_packets"]
                    < GATE_MIN_EXACT * arguments.sequences // SEQUENCES
                )
                gate = {
                    "proposed_gate_tenth_db": point["tenth_db"],
                    "exact_packets": point["exact_packets"],
                    "point_below_is_under_gate": below_ok,
                }
                break
        noise = []
        if not arguments.skip_noise_only:
            for tenth in NOISE_ONLY_TENTHS[link_id]:
                empty = 0
                count = 0
                failures = {}
                for lo, hi in _chunks(arguments.sequences, arguments.chunk):
                    record = done.get(f"noise_only:{link_id}:{tenth}:{lo}:{hi}")
                    if record is None:
                        continue
                    empty += record["empty"]
                    count += hi - lo + 1
                    for key, value in record["failures"].items():
                        failures[key] = failures.get(key, 0) + value
                noise.append(
                    {"tenth_db": tenth, "slots": count, "empty": empty, "failures": failures}
                )
                if empty != count:
                    ok = False
        summary["links"][str(link_id)] = {
            "sweep": points,
            "proposed_empirical_gate": gate,
            "noise_only": noise,
        }
        if gate is None and not arguments.tenths:
            ok = False

    summary["identifiers"] = {
        "rng": "sha256-counter-box-muller-mpfr256-v2",
        "db_scale": "mpfr256-db-to-binary64-rne-v1",
        "packet_sequence": "REPEAT-CANONICAL-PACKET-1000",
        "gate_rule": ">=990/1000 first ascending point; 아래 grid point는 <990 (§7.7)",
        "official_point_role": "CHARACTERIZATION_ONLY (§12.4/§12.5)",
    }
    summary["run"] = {
        "run_id": arguments.run_id,
        "sequences": arguments.sequences,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 1),
        "workers": arguments.workers,
        "complete_sweep": not arguments.tenths and arguments.sequences == SEQUENCES,
    }
    result_path = run_dir / "result.json"
    result_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"[evidence] {result_path}", flush=True)
    for link_id in links:
        gate = summary["links"][str(link_id)]["proposed_empirical_gate"]
        print(f"[gate] link {link_id}: {gate}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
