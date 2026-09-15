"""Independent AES-256-GCM parameter benchmark for the VDS2 security profile.

This tool deliberately does not load operational key material or state.  It uses
one ephemeral key per process and counter-derived, non-repeating nonces.  The
benchmark calls ``cryptography.hazmat.primitives.ciphers.aead.AESGCM`` for every
supported condition, which is also the API used by the operational VDS2 wrapper.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import random
import ssl
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cryptography  # noqa: E402
from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.backends.openssl.backend import backend  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from vdeter.common.addressing import (  # noqa: E402
    Addressed,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.identifiers import StationId  # noqa: E402
from vdeter.security.constants import (  # noqa: E402
    AAD_BYTES,
    HEADER_BYTES,
    KEY_BYTES,
    MAX_COUNTER,
    NONCE_BYTES,
    TAG_BYTES,
)
from vdeter.security.protocol.aad import build_aad  # noqa: E402
from vdeter.security.protocol.header import SecurityHeader  # noqa: E402
from vdeter.security.protocol.pdu import build_security_pdu  # noqa: E402


BASELINE_TAG_BYTES = TAG_BYTES
BASELINE_NONCE_BYTES = NONCE_BYTES
BASELINE_PLAINTEXT_BYTES = 256
TAG_CANDIDATES = (12, 13, 14, 15, 16)
DEFAULT_NONCE_CANDIDATES = (8, 12, 16)
PLAINTEXT_CANDIDATES = (32, 64, 128, 256, 512, 1024)
PREFIX = bytes.fromhex("ff7f03")
ROUTE = RouteAddresses(
    OriginalSource(StationId(100)),
    OriginalDestination(Addressed(StationId(200))),
)

RAW_FIELDS = (
    "condition_id", "batch", "condition_order", "iteration", "tag_bytes", "nonce_bytes",
    "plaintext_bytes", "aad_bytes", "encrypt_ns", "decrypt_ns",
    "round_trip_ok", "pre_security_bytes", "experiment_wire_bytes",
    "vds2_actual_wire_bytes", "increase_bytes", "overhead_percent",
)
SUMMARY_FIELDS = (
    "condition_id", "status", "tag_bytes", "nonce_bytes", "plaintext_bytes",
    "aad_bytes", "warmups", "iterations", "batches", "samples",
    "encrypt_mean_ns", "encrypt_median_ns", "encrypt_p95_ns",
    "encrypt_stdev_ns", "decrypt_mean_ns", "decrypt_median_ns",
    "decrypt_p95_ns", "decrypt_stdev_ns", "round_trip_success_rate",
    "pre_security_bytes", "experiment_wire_bytes", "vds2_actual_wire_bytes",
    "increase_bytes", "overhead_percent", "measurement_api",
    "cipher_object_creation_included", "size_basis",
)
FAILURE_FIELDS = (
    "condition_id", "tag_bytes", "nonce_bytes", "plaintext_bytes", "stage",
    "reason",
)


@dataclass(frozen=True, slots=True)
class Condition:
    tag_bytes: int
    nonce_bytes: int
    plaintext_bytes: int

    @property
    def condition_id(self) -> str:
        return (
            f"tag{self.tag_bytes}_nonce{self.nonce_bytes}_"
            f"body{self.plaintext_bytes}"
        )


class NonceAllocator:
    """Allocate unique nonces for one ephemeral key during one process run."""

    def __init__(self) -> None:
        self._prefixes: dict[int, bytes] = {}
        self._counter = 0
        self._seen: set[bytes] = set()

    def allocate(self, length: int) -> tuple[int, bytes]:
        if not 8 <= length <= 128:
            raise ValueError("cryptography AESGCM nonce length must be 8..128 bytes")
        if self._counter >= min(MAX_COUNTER, (1 << 32) - 1):
            raise OverflowError("nonce/message counter exhausted")
        self._counter += 1
        if length not in self._prefixes:
            self._prefixes[length] = os.urandom(length - 4)
        prefix = self._prefixes[length]
        nonce = prefix + self._counter.to_bytes(4, "big")
        if nonce in self._seen:
            raise RuntimeError("nonce reuse detected")
        self._seen.add(nonce)
        return self._counter, nonce


def _supported(condition: Condition) -> tuple[bool, str]:
    if condition.tag_bytes != 16:
        return False, (
            "cryptography AESGCM encrypt() always generates a 16-byte tag; "
            "the public API has no native 12..15-byte tag-generation option. "
            "Truncating the generated tag is intentionally not treated as support."
        )
    if not 8 <= condition.nonce_bytes <= 128:
        return False, "cryptography AESGCM accepts nonce lengths from 8 to 128 bytes"
    return True, ""


def encrypt_once(
    *, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes
) -> tuple[bytes, bytes, int]:
    """Encrypt and return ciphertext, detached tag, and measured nanoseconds."""

    started = time.perf_counter_ns()
    combined = AESGCM(key).encrypt(nonce, plaintext, aad)
    elapsed = time.perf_counter_ns() - started
    return combined[:-TAG_BYTES], combined[-TAG_BYTES:], elapsed


def decrypt_once(
    *, key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes
) -> tuple[bytes, int]:
    """Authenticate/decrypt and return plaintext and measured nanoseconds."""

    started = time.perf_counter_ns()
    plaintext = AESGCM(key).decrypt(nonce, ciphertext + tag, aad)
    elapsed = time.perf_counter_ns() - started
    return plaintext, elapsed


def _prepared_input(
    condition: Condition, allocator: NonceAllocator, plaintext: bytes
) -> tuple[bytes, bytes, bytes]:
    counter, nonce = allocator.allocate(condition.nonce_bytes)
    header = SecurityHeader(1, counter).to_bytes()
    aad = build_aad(prefix=PREFIX, route=ROUTE, security_header=header)
    return nonce, header, aad


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _summary(
    condition: Condition,
    rows: list[dict[str, object]],
    *,
    warmups: int,
    iterations: int,
    batches: int,
) -> dict[str, object]:
    encrypt = [int(row["encrypt_ns"]) for row in rows]
    decrypt = [int(row["decrypt_ns"]) for row in rows]
    first = rows[0]
    return {
        "condition_id": condition.condition_id,
        "status": "measured",
        "tag_bytes": condition.tag_bytes,
        "nonce_bytes": condition.nonce_bytes,
        "plaintext_bytes": condition.plaintext_bytes,
        "aad_bytes": AAD_BYTES,
        "warmups": warmups,
        "iterations": iterations,
        "batches": batches,
        "samples": len(rows),
        "encrypt_mean_ns": round(statistics.fmean(encrypt), 2),
        "encrypt_median_ns": round(statistics.median(encrypt), 2),
        "encrypt_p95_ns": _percentile(encrypt, 0.95),
        "encrypt_stdev_ns": round(statistics.pstdev(encrypt), 2),
        "decrypt_mean_ns": round(statistics.fmean(decrypt), 2),
        "decrypt_median_ns": round(statistics.median(decrypt), 2),
        "decrypt_p95_ns": _percentile(decrypt, 0.95),
        "decrypt_stdev_ns": round(statistics.pstdev(decrypt), 2),
        "round_trip_success_rate": round(
            sum(bool(row["round_trip_ok"]) for row in rows) / len(rows), 8
        ),
        "pre_security_bytes": first["pre_security_bytes"],
        "experiment_wire_bytes": first["experiment_wire_bytes"],
        "vds2_actual_wire_bytes": first["vds2_actual_wire_bytes"],
        "increase_bytes": first["increase_bytes"],
        "overhead_percent": first["overhead_percent"],
        "measurement_api": "cryptography.hazmat.primitives.ciphers.aead.AESGCM",
        "cipher_object_creation_included": True,
        "size_basis": (
            "VDS2 PDU serializer only; synthetic body, codec validity unmeasured"
            if condition.nonce_bytes == NONCE_BYTES
            else "experimental VDS2-like envelope; nonce is not transmitted"
        ),
    }


def run_condition(
    condition: Condition,
    *,
    key: bytes,
    allocator: NonceAllocator,
    warmups: int,
    iterations: int,
    batches: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    supported, reason = _supported(condition)
    if not supported:
        raise ValueError(reason)
    plaintext = bytes((index % 251 for index in range(condition.plaintext_bytes)))

    for _ in range(warmups):
        nonce, _header, aad = _prepared_input(condition, allocator, plaintext)
        ciphertext, tag, _ = encrypt_once(
            key=key, nonce=nonce, plaintext=plaintext, aad=aad
        )
        recovered, _ = decrypt_once(
            key=key, nonce=nonce, ciphertext=ciphertext, tag=tag, aad=aad
        )
        if recovered != plaintext:
            raise RuntimeError("warmup round trip plaintext mismatch")

    rows: list[dict[str, object]] = []
    for batch in range(1, batches + 1):
        for iteration in range(1, iterations + 1):
            # Plaintext, nonce, header and AAD are all prepared before timing.
            nonce, header_bytes, aad = _prepared_input(
                condition, allocator, plaintext
            )
            ciphertext, tag, encrypt_ns = encrypt_once(
                key=key, nonce=nonce, plaintext=plaintext, aad=aad
            )
            recovered, decrypt_ns = decrypt_once(
                key=key, nonce=nonce, ciphertext=ciphertext, tag=tag, aad=aad
            )
            header = SecurityHeader.parse(header_bytes)
            plain_wire = PREFIX + plaintext
            experiment_wire = PREFIX + build_security_pdu(
                header, ciphertext, tag
            )
            before = len(plain_wire)
            after = len(experiment_wire)
            increase = after - before
            rows.append({
                "condition_id": condition.condition_id,
                "batch": batch,
                "iteration": iteration,
                "tag_bytes": condition.tag_bytes,
                "nonce_bytes": condition.nonce_bytes,
                "plaintext_bytes": condition.plaintext_bytes,
                "aad_bytes": len(aad),
                "encrypt_ns": encrypt_ns,
                "decrypt_ns": decrypt_ns,
                "round_trip_ok": recovered == plaintext,
                "pre_security_bytes": before,
                "experiment_wire_bytes": after,
                "vds2_actual_wire_bytes": (
                    after if condition.nonce_bytes == NONCE_BYTES else ""
                ),
                "increase_bytes": increase,
                "overhead_percent": round(increase / before * 100.0, 8),
            })
    return rows, _summary(
        condition, rows, warmups=warmups, iterations=iterations, batches=batches
    )


def _int_list(
    text: str, *, name: str, allowed: tuple[int, ...] | None = None,
    minimum: int | None = None, maximum: int | None = None,
) -> tuple[int, ...]:
    try:
        values = tuple(dict.fromkeys(int(item.strip()) for item in text.split(",")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be comma-separated integers") from exc
    if not values or any(isinstance(value, bool) for value in values):
        raise argparse.ArgumentTypeError(f"{name} must not be empty")
    if allowed is not None and any(value not in allowed for value in values):
        raise argparse.ArgumentTypeError(
            f"{name} must contain only: {','.join(map(str, allowed))}"
        )
    if minimum is not None and any(value < minimum for value in values):
        raise argparse.ArgumentTypeError(f"{name} values must be >= {minimum}")
    if maximum is not None and any(value > maximum for value in values):
        raise argparse.ArgumentTypeError(f"{name} values must be <= {maximum}")
    return values


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _nonnegative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _conditions(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[Condition]:
    if args.sweep == "single":
        if not all(len(values) == 1 for values in (
            args.tag_lengths, args.nonce_lengths, args.plaintext_sizes
        )):
            parser.error("single sweep requires exactly one value for each variable")
        return [Condition(
            args.tag_lengths[0], args.nonce_lengths[0], args.plaintext_sizes[0]
        )]

    baseline = Condition(
        BASELINE_TAG_BYTES, BASELINE_NONCE_BYTES, BASELINE_PLAINTEXT_BYTES
    )
    candidates = [baseline]
    candidates.extend(
        Condition(tag, BASELINE_NONCE_BYTES, BASELINE_PLAINTEXT_BYTES)
        for tag in args.tag_lengths
    )
    candidates.extend(
        Condition(BASELINE_TAG_BYTES, nonce, BASELINE_PLAINTEXT_BYTES)
        for nonce in args.nonce_lengths
    )
    candidates.extend(
        Condition(BASELINE_TAG_BYTES, BASELINE_NONCE_BYTES, size)
        for size in args.plaintext_sizes
    )
    return list(dict.fromkeys(candidates))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", choices=("single", "one-at-a-time"), default="single")
    parser.add_argument(
        "--tag-lengths",
        type=lambda value: _int_list(value, name="tag lengths", allowed=TAG_CANDIDATES),
        default=(BASELINE_TAG_BYTES,),
        help="comma-separated bytes from 12,13,14,15,16",
    )
    parser.add_argument(
        "--nonce-lengths",
        type=lambda value: _int_list(value, name="nonce lengths", minimum=8, maximum=128),
        default=(BASELINE_NONCE_BYTES,),
        help="comma-separated bytes in cryptography AESGCM range 8..128",
    )
    parser.add_argument(
        "--plaintext-sizes",
        type=lambda value: _int_list(
            value, name="plaintext sizes", allowed=PLAINTEXT_CANDIDATES
        ),
        default=(BASELINE_PLAINTEXT_BYTES,),
        help="comma-separated bytes from 32,64,128,256,512,1024",
    )
    parser.add_argument("--warmups", type=_nonnegative_int, default=100)
    parser.add_argument("--iterations", type=_positive_int, default=1000)
    parser.add_argument("--batches", type=_positive_int, default=5)
    parser.add_argument("--seed", type=int, default=20260915,
                        help="reproducible condition-order shuffle seed (not key/nonce seed)")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("results/aes-gcm-benchmark")
    )
    return parser


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _environment(args: argparse.Namespace, conditions: list[Condition]) -> dict[str, object]:
    return {
        "artifact_type": "AES_256_GCM_PARAMETER_BENCHMARK_ENVIRONMENT_V1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "cryptography_version": cryptography.__version__,
        "cryptography_openssl_version": backend.openssl_version_text(),
        "python_ssl_openssl_version": ssl.OPENSSL_VERSION,
        "python_executable": sys.executable,
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "statistics_unit": "individual operation nanoseconds; pooled samples, not batch averages",
        "p95_method": "nearest rank: sorted samples[ceil(0.95*N)-1]",
        "shuffle_seed": args.seed,
        "warmup_scope": "per condition per batch; excluded from raw and summary",
        "timer": "time.perf_counter_ns",
        "timer_resolution_seconds": time.get_clock_info("perf_counter").resolution,
        "measurement_api": "cryptography.hazmat.primitives.ciphers.aead.AESGCM",
        "key_bits": KEY_BYTES * 8,
        "key_source": "ephemeral os.urandom; value not persisted",
        "aad_bytes": AAD_BYTES,
        "aad_rule": "VDS2 domain + 3-byte prefix + source + destination + security header",
        "baseline": asdict(Condition(16, 12, 256)),
        "conditions": [asdict(condition) for condition in conditions],
        "warmups": args.warmups,
        "iterations": args.iterations,
        "batches": args.batches,
        "sweep": args.sweep,
        "timed_costs_included": [
            "AESGCM object construction", "encrypt/decrypt call", "tag generation/verification",
            "decrypt ciphertext+tag concatenation", "timer read overhead; no subtraction",
        ],
        "timed_costs_excluded": [
            "key generation", "plaintext preparation", "nonce allocation",
            "security header and AAD construction", "serialization", "file output",
        ],
        "nonce_safety": (
            "A random per-length prefix plus one process-global 32-bit counter; "
            "all generated nonces are checked for uniqueness under the ephemeral key. "
            "Non-12-byte nonces are benchmark-only; VDS2 remains fixed at 12 bytes."
        ),
        "tag_support": (
            "AESGCM encryption natively emits 16 bytes only; 12..15 are rejected "
            "without truncation."
        ),
        "vds2_constants": {
            "header_bytes": HEADER_BYTES,
            "nonce_bytes": NONCE_BYTES,
            "tag_bytes": TAG_BYTES,
            "aad_bytes": AAD_BYTES,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conditions = _conditions(args, parser)
    required_invocations = len(conditions) * (
        (args.warmups + args.iterations) * args.batches
    )
    if required_invocations > MAX_COUNTER:
        parser.error(f"requested encryptions exceed VDS2 counter limit {MAX_COUNTER}")

    args.output_dir = args.output_dir / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:8]
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    environment = _environment(args, conditions)
    command = [sys.executable, str(Path(__file__).resolve()),
               *(sys.argv[1:] if argv is None else argv)]
    environment["command_argv"] = command
    environment["working_directory"] = str(Path.cwd())
    command_text = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in command)
    (args.output_dir / "command.ps1").write_text(command_text + "\n", encoding="utf-8")
    (args.output_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    key = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
    allocator = NonceAllocator()
    raw_rows: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    supported_conditions = []
    for condition in conditions:
        supported, reason = _supported(condition)
        if not supported:
            failures.append({
                "condition_id": condition.condition_id,
                "tag_bytes": condition.tag_bytes,
                "nonce_bytes": condition.nonce_bytes,
                "plaintext_bytes": condition.plaintext_bytes,
                "stage": "support-check",
                "reason": reason,
            })
            print(f"UNSUPPORTED {condition.condition_id}: {reason}", file=sys.stderr)
            continue
        supported_conditions.append(condition)
    rng = random.Random(args.seed)
    schedule = []
    for batch in range(1, args.batches + 1):
        order = supported_conditions.copy()
        rng.shuffle(order)
        schedule.append({"batch": batch, "conditions": [c.condition_id for c in order]})
    environment["batch_schedule"] = schedule
    (args.output_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    by_id = {c.condition_id: c for c in supported_conditions}
    by_condition = {c.condition_id: [] for c in supported_conditions}
    batch_summaries = []
    for item in schedule:
        for position, condition_id in enumerate(item["conditions"], 1):
            condition = by_id[condition_id]
            try:
                rows, summary = run_condition(
                    condition, key=key, allocator=allocator,
                    warmups=args.warmups, iterations=args.iterations, batches=1,
                )
                for row in rows:
                    row.update(batch=item["batch"], condition_order=position)
                summary.update(batch=item["batch"], condition_order=position)
                raw_rows.extend(rows)
                by_condition[condition_id].extend(rows)
                batch_summaries.append(summary)
                if not all(row["round_trip_ok"] for row in rows):
                    raise RuntimeError("measured round trip plaintext mismatch")
            except Exception as exc:
                failures.append({
                    "condition_id": condition_id, **asdict(condition),
                    "stage": "measurement",
                    "reason": f"batch {item['batch']}: {type(exc).__name__}: {exc}",
                })
        print(f"Batch {item['batch']}/{args.batches} complete", flush=True)
    for condition in supported_conditions:
        rows = by_condition[condition.condition_id]
        if rows:
            summaries.append(_summary(condition, rows, warmups=args.warmups,
                                      iterations=args.iterations, batches=args.batches))

    _write_csv(args.output_dir / "raw_timings.csv", RAW_FIELDS, raw_rows)
    _write_csv(args.output_dir / "summary.csv", SUMMARY_FIELDS, summaries)
    _write_csv(args.output_dir / "batch_summary.csv",
               ("batch", "condition_order") + SUMMARY_FIELDS, batch_summaries)
    _write_csv(args.output_dir / "failures.csv", FAILURE_FIELDS, failures)
    print(
        f"AES-GCM benchmark: {len(summaries)} measured, {len(failures)} unsupported/failed; "
        f"output={args.output_dir}"
    )
    return 0 if summaries and not any(
        failure["stage"] == "measurement" for failure in failures
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
