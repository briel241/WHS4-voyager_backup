"""Small, non-RF verification for the independent AES-GCM benchmark."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tools.aes_gcm_benchmark import (
    AAD_BYTES,
    Condition,
    NonceAllocator,
    PREFIX,
    ROUTE,
    _prepared_input,
    decrypt_once,
    encrypt_once,
    main,
)


@pytest.mark.parametrize("nonce_bytes", [8, 12, 16])
def test_supported_nonce_round_trip_and_tampered_tag_rejection(nonce_bytes):
    key = AESGCM.generate_key(bit_length=256)
    plaintext = bytes(range(32))
    condition = Condition(16, nonce_bytes, len(plaintext))
    nonce, _header, aad = _prepared_input(condition, NonceAllocator(), plaintext)

    ciphertext, tag, encrypt_ns = encrypt_once(
        key=key, nonce=nonce, plaintext=plaintext, aad=aad
    )
    recovered, decrypt_ns = decrypt_once(
        key=key, nonce=nonce, ciphertext=ciphertext, tag=tag, aad=aad
    )

    assert recovered == plaintext
    assert len(tag) == 16 and len(aad) == AAD_BYTES
    assert encrypt_ns >= 0 and decrypt_ns >= 0
    tampered_tag = tag[:-1] + bytes((tag[-1] ^ 1,))
    with pytest.raises(InvalidTag):
        decrypt_once(
            key=key,
            nonce=nonce,
            ciphertext=ciphertext,
            tag=tampered_tag,
            aad=aad,
        )


def test_small_one_at_a_time_run_writes_results_and_unsupported_reason(tmp_path):
    output = tmp_path / "results"
    rc = main([
        "--sweep", "one-at-a-time",
        "--tag-lengths", "12,16",
        "--nonce-lengths", "8,12,16",
        "--plaintext-sizes", "32,256",
        "--warmups", "1",
        "--iterations", "2",
        "--batches", "3",
        "--output-dir", str(output),
    ])

    assert rc == 0
    output = next(output.iterdir())
    assert (output / "environment.json").is_file()
    with (output / "raw_timings.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and all(row["round_trip_ok"] == "True" for row in rows)
    baseline = next(row for row in rows if row["condition_id"] == "tag16_nonce12_body256")
    assert int(baseline["experiment_wire_bytes"]) - int(baseline["pre_security_bytes"]) == 23
    assert baseline["vds2_actual_wire_bytes"] == baseline["experiment_wire_bytes"]
    with (output / "failures.csv").open(encoding="utf-8", newline="") as handle:
        failures = list(csv.DictReader(handle))
    assert any(row["condition_id"] == "tag12_nonce12_body256" for row in failures)
    assert "no native 12..15-byte tag-generation option" in failures[0]["reason"]
    counts = Counter((r["condition_id"], r["batch"]) for r in rows)
    assert len(counts) == 12 and set(counts.values()) == {2}
    env = json.loads((output / "environment.json").read_text(encoding="utf-8"))
    orders = [tuple(b["conditions"]) for b in env["batch_schedule"]]
    assert len(set(orders)) > 1
    assert all(len(order) == len(set(order)) == 4 for order in orders)
    assert "cryptography_openssl_version" in env and "python_ssl_openssl_version" in env
    assert (output / "command.ps1").is_file()
    with (output / "summary.csv").open(encoding="utf-8", newline="") as handle:
        summary = list(csv.DictReader(handle))
    assert len(summary) == 4
    for s in summary:
        samples = [int(r["encrypt_ns"]) for r in rows if r["condition_id"] == s["condition_id"]]
        assert float(s["encrypt_mean_ns"]) == round(statistics.mean(samples), 2)
        assert float(s["encrypt_median_ns"]) == statistics.median(samples)
        assert int(s["encrypt_p95_ns"]) == max(samples)
    with (output / "batch_summary.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 12


def test_repeated_output_path_preserves_previous_run(tmp_path):
    argv = ["--warmups", "0", "--iterations", "1", "--batches", "1",
            "--output-dir", str(tmp_path)]
    assert main(argv) == 0
    first = next(tmp_path.iterdir())
    original = (first / "raw_timings.csv").read_bytes()
    assert main(argv) == 0
    assert len(list(tmp_path.iterdir())) == 2
    assert (first / "raw_timings.csv").read_bytes() == original


@pytest.mark.parametrize("argv", [
    ["--tag-lengths", "11"],
    ["--nonce-lengths", "7"],
    ["--plaintext-sizes", "33"],
    ["--warmups", "-1"],
    ["--iterations", "0"],
    ["--batches", "0"],
    ["--sweep", "single", "--nonce-lengths", "8,12"],
])
def test_invalid_arguments_are_rejected(argv):
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
