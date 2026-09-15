"""Comparison accounting and output checks; hardware is never opened."""

import base64
import csv
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from apps.aes_gcm_hardware_compare import DEFAULT, make_body, read_config, run_experiment, summarize, hardware_command


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(seconds, .001)


class Events:
    def __init__(self):
        self.records = {"ROC": [], "MASS-A": [], "MASS-B": []}

    def poll(self):
        pass


class Controller:
    def __init__(self, events, behavior="ok", clock=None):
        self.events, self.behavior, self.clock = events, behavior, clock

    def invoke_service_inline(self, endpoint, **kwargs):
        request = kwargs["request"]
        if self.behavior == "reject":
            return {"body": {"call_outcome": "ERR", "public_error_code": "test_reject"}}
        if self.behavior == "error":
            raise TimeoutError("IPC timeout")
        if self.behavior != "timeout":
            body = base64.b64decode(request["payload_base64"])
            record = dict(event_type="DATA_RECEIVED", original_source=222222222,
                          original_destination=111111111, payload_format=request["payload_format"],
                          payload_sha256=hashlib.sha256(body).hexdigest(), payload_len=len(body),
                          payload_base64=request["payload_base64"])
            if self.behavior == "mismatch":
                record["payload_base64"] = base64.b64encode(b"bad").decode()
            if self.behavior == "late":
                self.clock.value += 10
            self.events.records["MASS-A"].append(record)
            if self.behavior == "duplicate":
                self.events.records["MASS-A"].append(record)
        return {"body": {"call_outcome": "OK", "transfer_id": kwargs["step_id"]}}


@pytest.mark.parametrize("size", [3, 10, 29, 39, 164])
def test_codec_valid_sizes_and_existing_field_identity(size):
    name, body = make_body(size, 0)
    assert len(body) == size
    assert make_body(size, 1)[1] != body
    assert "sc_vde" in name


@pytest.mark.parametrize("size", [7, 8])
def test_no_padding_to_unsupported_sizes(size):
    with pytest.raises(ValueError, match="No VDS2-required codec"):
        make_body(size, 0)


@pytest.mark.parametrize("behavior,delivered,timeout,duplicates", [
    ("ok", True, False, 0), ("duplicate", True, False, 1),
    ("timeout", False, True, 0), ("late", False, True, 0),
    ("reject", False, False, 0), ("error", False, False, 0),
    ("mismatch", False, False, 0),
])
def test_deadline_delivery_failures_csv_and_summary(tmp_path, behavior, delivered, timeout, duplicates):
    clock, events = Clock(), Events()
    controller = Controller(events, behavior, clock)
    config = DEFAULT | dict(body_sizes=[3, 7, 8], iterations=1, timeout_s=1, interval_s=.01, budget_s=5)
    output = tmp_path / "run"
    result = run_experiment(controller, events, config, output, run_id="test", backend="software",
                            now=clock.now, sleep=clock.sleep)
    assert result["attempted"] == 1
    rows, summary, _, _ = summarize([output])
    assert rows[0]["plaintext_match"] == str(delivered)
    assert rows[0]["timeout"] == str(timeout)
    assert rows[0]["duplicate_receives"] == str(duplicates)
    assert summary[0]["attempts"] == 1
    assert summary[0]["delivered"] == int(delivered)
    assert summary[0]["completion_observed_samples"] == int(delivered)
    assert len(list(csv.DictReader((output / "unsupported.csv").open()))) == 2
    assert json.loads((output / "manifest.json").read_text())["hardware_executed"] is False


def test_output_preserved_and_no_implicit_off(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        read_config(path)
    path.write_text('{"security":"OFF","iterations":1025}')
    with pytest.raises(ValueError):
        read_config(path)
    events, clock = Events(), Clock()
    with pytest.raises(FileExistsError):
        run_experiment(Controller(events), events, DEFAULT, tmp_path,
                       run_id="test", backend="software", now=clock.now, sleep=clock.sleep)


def test_join_real_wire_metadata_and_on_off_delta(tmp_path):
    events, clock = Events(), Clock()
    for mode in ("OFF", "ON"):
        run_experiment(Controller(events), events,
                       DEFAULT | dict(security=mode, body_sizes=[3], iterations=1),
                       tmp_path / mode, run_id=mode, backend="software", now=clock.now, sleep=clock.sleep)
    directory = tmp_path / "evidence/security_compare"
    directory.mkdir(parents=True)
    telemetry = [dict(stage="protect", run_id="ON", transfer_id="ON/3/0", wire_sha256="wire",
                      pdu_bytes=29, elapsed_ns=123),
                 dict(stage="unprotect", run_id="ON", wire_sha256="wire", source=222222222,
                      destination=111111111, elapsed_ns=456),
                 dict(stage="packet_build", run_id="ON", transfer_id="ON/3/0", procedure="SHORT_DATA",
                      link_id=17, segment_offset=0, retransmission_number=0)]
    (directory / "one.jsonl").write_text("\n".join(json.dumps(t) for t in telemetry))
    rows, summaries, deltas, captured = summarize([tmp_path / "OFF", tmp_path / "ON"])
    assert len(captured) == 3  # shared directory must not double count telemetry
    row = next(r for r in rows if r["security"] == "ON")
    assert row["pdu_bytes"] == 29 and row["decrypt_ns"] == 456
    assert row["link_ids"] == "17" and row["fragments_built"] == 1
    assert row["wireless_total_bytes"] == ""
    assert len(deltas) == 1 and deltas[0]["delivery_rate_on_minus_off"] == 0


def test_hardware_gate_failure_never_spawns_or_creates_run(tmp_path, monkeypatch):
    from apps.standalone import run_cmd
    config = tmp_path / "config.json"
    config.write_text('{"security":"OFF"}')
    output = tmp_path / "run"
    monkeypatch.setattr(sys, "platform", "linux")
    def refuse(*args, **kwargs):
        raise run_cmd.RunRefusal(run_cmd.EXIT_UNQUALIFIED, "fixture gate refusal")
    monkeypatch.setattr(run_cmd, "prepare", refuse)
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("Hardware must not be launched after gate refusal")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(run_cmd.RunRefusal, match="fixture gate refusal"):
        hardware_command(config, output)
    assert not output.exists()


def test_fresh_generated_bundle_is_reciprocal_and_private(tmp_path, monkeypatch):
    from tools import e2e_security_provision as provision
    from vdeter.security.session.provider import validate_reciprocal_bundle
    # tmp_path may be inside the repo in some runners: use the system temp root.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "fresh"
        assert provision.main(["generate-test-bundle", "--bundle-dir", str(target)]) == 0
        bundles = provision._load_bundle(target)
        validate_reciprocal_bundle(bundles)
        assert len({material.aes_key for b in bundles for material in b.directions}) == 4
        assert len({b.state_id for b in bundles}) == 3
        # Refuse to overwrite pre-existing provisioning material.
        assert provision.main(["generate-test-bundle", "--bundle-dir", str(target)]) == 2
        state_root = Path(temp) / "state"
        state_root.mkdir()
        assert provision.main(["initialize-state", "--bundle-dir", str(target), "--state-root", str(state_root)]) == 0
        replacement = Path(temp) / "replacement"
        replacement.mkdir()
        # Reusing the same bundle cannot reset its counters in a new directory.
        assert provision.main(["initialize-state", "--bundle-dir", str(target), "--state-root", str(replacement)]) == 2
        assert not list(replacement.iterdir())


def test_opt_in_crypto_metadata_links_tampering_without_secret_bytes():
    from tests.unit.security.test_vds2 import adapter, encode, route
    from tests.support.security import make_test_e2e_by_station
    from tests.support.stage4 import MASS_A_STATION_ID, MASS_B_STATION_ID, ROC_STATION_ID
    from vdeter.common.experiment import ExperimentBuffer
    from vdeter.common.contracts.presentation import PresentationDecodeRequest
    from vdeter.common.errors import ErrorCode
    ids = {MASS_A_STATION_ID.value: "stage5-ship-a", MASS_B_STATION_ID.value: "stage5-ship-b",
           ROC_STATION_ID.value: "stage5-roc"}
    dependencies = make_test_e2e_by_station(ids)
    tx_buffer, rx_buffer = ExperimentBuffer(), ExperimentBuffer()
    dependencies[MASS_A_STATION_ID.value].presentation_security.experiment_observer = tx_buffer
    dependencies[ROC_STATION_ID.value].presentation_security.experiment_observer = rx_buffer
    tx, rx = adapter(dependencies[MASS_A_STATION_ID.value]), adapter(dependencies[ROC_STATION_ID.value])
    try:
        encoded = encode(tx, MASS_A_STATION_ID.value, ROC_STATION_ID.value)
        assert encoded.is_ok
        wire = encoded.value.data
        bad = wire[:-1] + bytes([wire[-1] ^ 1])
        denied = rx.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), bad))
        assert denied.is_err and denied.error.code == ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED
        t, r = tx_buffer.drain()[0], rx_buffer.drain()[0]
        assert t["security_message_fingerprint"] == r["security_message_fingerprint"]
        assert t["wire_sha256"] != r["wire_sha256"]
        assert t["elapsed_ns"] > 0 and r["elapsed_ns"] > 0
        assert not any(field in t or field in r for field in ("key", "nonce", "header", "counter", "payload"))
        # Failed verification does not consume the replay entry; authentic original succeeds.
        good = rx.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), wire))
        assert good.is_ok
    finally:
        for dependency in dependencies.values():
            dependency.presentation_security.close_final(timeout_seconds=5)


def test_hardware_prepare_only_generates_mode_bound_command(tmp_path, monkeypatch):
    from apps.standalone import run_cmd, local_config
    monkeypatch.setattr(sys, "platform", "linux")
    config = tmp_path / "config.json"
    config.write_text('{"security":"OFF"}')
    context = dict(hardware={}, installation={}, reference={}, environment_manifest={})
    monkeypatch.setattr(run_cmd, "prepare", lambda *a, **k: context)
    monkeypatch.setattr(run_cmd, "gain_input_from_reference", lambda *a: {"gain": "test"})
    def generate(**kwargs):
        directory = Path(kwargs["run_root"]) / "resolved-configs"
        directory.mkdir()
        return {"configs_dir": str(directory)}
    monkeypatch.setattr(local_config, "generate_run_configs", generate)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("prepare-only must not launch RF"))
    output = tmp_path / "run"
    assert hardware_command(config, output, prepare_only=True) == 0
    prepared = json.loads((output / "prepared-command.json").read_text())
    assert prepared["rf_started"] is False
    command = prepared["command"]
    assert command[command.index("--e2e-security-mode") + 1] == "DISABLED"
    assert "--security-compare-config" in command


def test_hardware_on_missing_state_never_initializes_or_transmits(tmp_path, monkeypatch):
    from apps.standalone import run_cmd
    from vdeter.security.session.provider import ProtectedFileSecurityMaterialProvider
    from vdeter.security.session.state_store import SQLiteSecurityStateStore
    monkeypatch.setattr(sys, "platform", "linux")
    config = tmp_path / "config.json"
    config.write_text('{"security":"ON"}')
    monkeypatch.setattr(run_cmd, "prepare", lambda *a, **k: {})
    def missing(*a, **k):
        raise ValueError("missing protected state")
    monkeypatch.setattr(ProtectedFileSecurityMaterialProvider, "load_for_endpoint", missing)
    monkeypatch.setattr(SQLiteSecurityStateStore, "initialize", lambda *a, **k: pytest.fail("no counter reset"))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no RF after missing state"))
    with pytest.raises(ValueError, match="missing protected state"):
        hardware_command(config, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_run_namespace_separates_reused_transfer_ids_and_observed_size_delta(tmp_path):
    from apps.aes_gcm_hardware_compare import _write_csv
    clock = Clock()
    telemetry = []
    for mode, pdu in (("OFF", 6), ("ON", 29)):
        events = Events()
        output = tmp_path / mode
        run_experiment(Controller(events), events,
                       DEFAULT | dict(security=mode, body_sizes=[3], iterations=1),
                       output, run_id=mode, backend="software", now=clock.now, sleep=clock.sleep)
        rows = list(csv.DictReader((output / "messages.csv").open()))
        rows[0]["transfer_id"] = "reused-by-deterministic-harness"
        _write_csv(output / "messages.csv", rows)
        telemetry.append(dict(stage="protect", run_id=mode,
                              transfer_id="reused-by-deterministic-harness", wire_sha256=mode,
                              pdu_bytes=pdu, elapsed_ns=10 if mode == "ON" else None))
    telemetry.append(dict(stage="protect", run_id="foreign", transfer_id="reused-by-deterministic-harness",
                          wire_sha256="foreign", pdu_bytes=999, elapsed_ns=999))
    directory = tmp_path / "evidence/security_compare"
    directory.mkdir(parents=True)
    (directory / "reused.jsonl").write_text("\n".join(json.dumps(t) for t in telemetry))
    rows, summary, delta, captured = summarize([tmp_path / "OFF", tmp_path / "ON"])
    assert next(r for r in rows if r["security"] == "OFF")["pdu_bytes"] == 6
    assert next(r for r in rows if r["security"] == "ON")["pdu_bytes"] == 29
    assert len(captured) == 2
    assert delta[0]["pdu_bytes_on_minus_off"] == 23
