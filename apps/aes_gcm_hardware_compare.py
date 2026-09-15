"""Codec-valid ON/OFF workload on the existing Service API and radio stack.

Hardware execution is owned by stage7_coordinator_main, never this CLI.
The CLI offers a software-radio check and offline result summarization.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time
import platform
import os
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "VDETER", ROOT / "VDETER/src"):
    sys.path.insert(0, str(directory))

DEFAULT = dict(security="OFF", body_sizes=[3, 7, 8, 10, 29, 39, 164],
               iterations=2, interval_s=2.0, timeout_s=90.0, budget_s=1200.0,
               sender="ROC", receiver="MASS-A", ack_required=False)
STATIONS = {"ROC": 222222222, "MASS-A": 111111111, "MASS-B": 333333333}


def read_config(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "security" not in raw or set(raw) - set(DEFAULT):
        raise ValueError("Unknown experiment configuration fields")
    config = DEFAULT | raw
    if config["security"] not in ("OFF", "ON"):
        raise ValueError("Explicit OFF/ON experiment security required")
    if (type(config["iterations"]) is not int or not 1 <= config["iterations"] <= 1024
            or type(config["body_sizes"]) is not list or not config["body_sizes"]
            or len(set(config["body_sizes"])) != len(config["body_sizes"])
            or any(type(n) is not int or n < 1 for n in config["body_sizes"])):
        raise ValueError("Invalid conditions or iterations (1..1024)")
    for name in ("interval_s", "timeout_s", "budget_s"):
        value = config[name]
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError("Positive finite timing values required")
    if config["budget_s"] > 1800 or config["timeout_s"] > config["budget_s"]:
        raise ValueError("Workload budget is at most 1800 seconds; timeout must fit")
    if (config["sender"] not in STATIONS or config["receiver"] not in STATIONS
            or config["sender"] == config["receiver"]
            or "ROC" not in (config["sender"], config["receiver"])
            or type(config["ack_required"]) is not bool):
        raise ValueError("Use an addressed ROC/MASS pair and a boolean Link ACK setting")
    if 14 in config["body_sizes"] and (config["sender"] == "ROC" or config["ack_required"]):
        raise ValueError("Position-report alternative requires MASS to ROC and Link ACK false")
    return config


def make_body(size, iteration):
    """Use existing application fields for identity; never append a log envelope."""
    from common.codec import encode_body
    from common.messages.application_ack import ApplicationAck
    from common.messages.route_instruction import RouteInstruction, Waypoint
    from common.messages.position_report import PositionReport
    from vdeter.presentation.codecs.sc_vde_route_control import (
        COMMAND_ALLOWED_APPLICATION_BYTES, ScVdeRouteControlAckCodec,
        ScVdeRouteControlCommandCodec,
    )
    if not 0 <= iteration < 1024:
        raise ValueError("Linkage ID would wrap; start a fresh run instead")
    if size == 3:
        body = encode_body(ApplicationAck(linkage_id=iteration, status=4))
        codec = ScVdeRouteControlAckCodec()
        name = "sc_vde.route_control.ack"
    elif size in COMMAND_ALLOWED_APPLICATION_BYTES:
        count = COMMAND_ALLOWED_APPLICATION_BYTES.index(size)
        body = encode_body(RouteInstruction(linkage_id=iteration,
                                           waypoints=[Waypoint() for _ in range(count)]))
        codec = ScVdeRouteControlCommandCodec()
        name = "sc_vde.route_control.command"
    elif size == 14:
        from vdeter.presentation.codecs.sc_vde_position_report import ScVdePositionReportCodec
        # Existing coordinate field gives unique codec-valid alternatives without extra bytes.
        body = encode_body(PositionReport(longitude=iteration))
        codec = ScVdePositionReportCodec()
        name = "sc_vde.position_report"
    else:
        raise ValueError(
            "No VDS2-required codec accepts this Body size: ACK=3B, position=14B, "
            "command=ceil((76+77*n)/8), n=0..16. Use 10B or 14B separately; "
            "AIS/certificate/opaque formats are not encrypted by the existing policy."
        )
    # Validate both size and canonical field values through the real codec.
    encoded = codec.encode(body)
    if encoded.is_err or len(body) != size or encoded.value != codec.discriminator.prefix + body:
        raise ValueError("Application codec rejected the requested Body")
    return name, body


def supported_conditions(config):
    supported = []
    for size in config["body_sizes"]:
        try:
            make_body(size, 0)
            supported.append(size)
        except ValueError:
            pass
    return supported


def hardware_settings(configs_dir):
    """Stable actual link/RF configuration, excluding run-specific binding metadata."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    root = Path(configs_dir)
    settings = {"data_profile": tomllib.loads((root / "profiles/data-profile.toml").read_text(encoding="utf-8"))}
    for endpoint in ("roc", "mass-a", "mass-b"):
        raw = tomllib.loads((root / f"endpoints/{endpoint}.toml").read_text(encoding="utf-8"))
        settings[endpoint] = {key: raw[key] for key in ("link", "mac", "phy", "radio")}
        settings[endpoint]["stage7_rf"] = {key: raw["stage7"][key] for key in (
            "lane_map_profile", "frequency_translation_hz", "lower_rf_tune_hz", "upper_rf_tune_hz", "phy_facade")}
    return settings


def hardware_command(config_path, output, *, prepare_only=False):
    """Reuse standalone qualification/preflight and local config generation.

    This entry point requires native Linux, an approved hardware.toml, a current
    active qualification reference, existing signing identity, and preprovisioned
    VDS2 state. It does not repair or bypass any qualification check.
    """
    if sys.platform != "linux":
        raise ValueError("Hardware command requires native Linux on the qualified Raspberry Pi")
    config = read_config(config_path)
    if not supported_conditions(config) or (14 in config["body_sizes"] and config["ack_required"]):
        raise ValueError("No supported conditions, or invalid position Link ACK setting")
    from apps.standalone.state import StatePaths, SingleInstanceLock, write_json_atomic
    from apps.standalone import run_cmd, local_config
    from apps.standalone.local_session import build_child_env
    from vdeter.security.session.provider import ProtectedFileSecurityMaterialProvider
    from vdeter.security.session.state_store import SQLiteSecurityStateStore
    from vdeter.common.identifiers import StationId
    paths = StatePaths(ROOT)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("Run output already exists; previous evidence preserved")
    with SingleInstanceLock(paths.lock_path):
        context = run_cmd.prepare(paths, repo_root=ROOT)
        # Prove every endpoint's durable state can open BEFORE any RF admission.
        # The launcher will validate again when taking ownership. Never initialize
        # a new counter database here, even if a previous state is missing.
        if config["security"] == "ON":
            bundles = []
            from vdeter.security.session.provider import validate_reciprocal_bundle, validate_endpoint_materials
            for endpoint, station in STATIONS.items():
                directory = Path("/var/lib/vdeter/security") / endpoint
                bundle = ProtectedFileSecurityMaterialProvider(directory / "materials.json").load_for_endpoint(
                    endpoint_id=endpoint, local_station_id=StationId(station))
                peers = frozenset((111111111, 333333333)) if endpoint == "ROC" else frozenset((222222222,))
                validate_endpoint_materials(bundle, expected_peers=peers)
                with_store = SQLiteSecurityStateStore(directory / "state.sqlite3", bundle)
                with_store.close()
                bundles.append(bundle)
            validate_reciprocal_bundle(tuple(bundles))
            if len({b.state_id for b in bundles}) != 3 or len({b.provisioning_id for b in bundles}) != 1:
                raise ValueError("Endpoint provisioning inventory mismatch")
        output.mkdir(parents=True, exist_ok=False)
        run_id = "aes-" + uuid4().hex[:16]
        gain = run_cmd.gain_input_from_reference(context["reference"])
        generated = local_config.generate_run_configs(
            repo_root=ROOT, run_root=output, run_id=run_id, rr_phase=run_cmd.RUN_RR_PHASE,
            hardware=context["hardware"], installation=context["installation"],
            installation_verified=True, provenance=context.get("provenance"),
            run_mode="OPERATIONAL", gain_input=gain,
            environment_manifest=context["environment_manifest"], usb_paths=context.get("usb_paths"))
        configs = Path(generated["configs_dir"])
        gain_path = configs / "gain_artifact_input.json"
        write_json_atomic(gain_path, gain)
        # Copy the resolved, non-secret config to prevent edits between preparation and child launch.
        resolved_experiment = output / "experiment.json"
        write_json_atomic(resolved_experiment, config)
        command = [sys.executable, str(ROOT / "VDETER/apps/stage7_coordinator_main.py"),
                   "--run-id", run_id, "--phase", "mass-roc-service",
                   "--e2e-security-mode", "ENFORCED" if config["security"] == "ON" else "DISABLED",
                   "--security-compare-config", str(resolved_experiment),
                   "--run-root", str(output), "--configs-dir", str(configs),
                   "--gain-artifact", str(gain_path), "--scenario-id", "SC-VDE-005"]
        write_json_atomic(output / "prepared-command.json", {"command": command, "rf_started": False})
        if prepare_only:
            print("Prepared only; no RF transmission. Command:", json.dumps(command))
            return 0
        with (output / "coordinator.log").open("x", encoding="utf-8") as log:
            done = subprocess.run(command, cwd=ROOT / "VDETER",
                                  env=build_child_env(identity_root=paths.identity_dir),
                                  stdout=log, stderr=subprocess.STDOUT, check=False)
        return done.returncode


def _write_csv(path, rows, fields=None):
    if not rows and fields is None:
        return
    fields = list(dict.fromkeys(list(fields or []) + [key for row in rows for key in row]))
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_experiment(controller, events, config, output, *, run_id, backend,
                   now=time.perf_counter, sleep=time.sleep, link_settings=None):
    """Sequential unique application attempts, receiver evidence within one deadline.

    Completion latency uses ONLY the controller clock. It includes management IPC,
    receiver event logging/polling and is neither one-way RF E2E nor application RTT.
    Link terminal/ACK state is never used to declare application delivery.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows, unsupported = [], []
    compare_settings = {k: v for k, v in config.items() if k != "security"} | {"link_settings": link_settings}
    settings_sha = hashlib.sha256(json.dumps(compare_settings, sort_keys=True).encode()).hexdigest()
    sender, receiver = config["sender"], config["receiver"]
    events.poll()
    baseline = {ep: len(records) for ep, records in events.records.items()}
    began = now()
    budget_end = began + config["budget_s"]
    last_start = None
    exhausted = False
    for size in config["body_sizes"]:
        try:
            make_body(size, 0)
        except ValueError as exc:
            unsupported.append(dict(requested_body_bytes=size, reason=str(exc)))
            continue
        for iteration in range(config["iterations"]):
            if last_start is not None:
                # Hardware advances autonomously; the software-radio harness needs polls.
                while now() < min(last_start + config["interval_s"], budget_end):
                    events.poll()
                    sleep(min(.05, max(0, min(last_start + config["interval_s"], budget_end) - now())))
            if now() >= budget_end:
                exhausted = True
                break
            name, body = make_body(size, iteration)
            digest = hashlib.sha256(body).hexdigest()
            message_id = f"{run_id}/{size}/{iteration}"
            row = dict(run_id=run_id, condition_id=f"body-{size}", message_id=message_id,
                       compare_settings_sha256=settings_sha,
                       iteration=iteration, security=config["security"], backend=backend,
                       sender=sender, receiver=receiver, source=STATIONS[sender],
                       destination=STATIONS[receiver], payload_format=name,
                       body_sha256=digest, body_bytes=len(body), pdu_bytes=None,
                       link_ids=None, transmission_modes=None, fragments_built=None,
                       fragment_count_basis="unique built segment offsets; not RF receipt",
                       retransmissions_built=None, wireless_total_bytes=None,
                       radio_scheduled_bursts=None, radio_completed_bursts=None,
                       encrypt_ns=None, decrypt_ns=None,
                       crypto_interval="detached wrapper incl validation, AESGCM construction, slicing/concatenation; excludes state, nonce/AAD, serialization, I/O",
                       tx_attempted=True, submit_accepted=False, transfer_id=None,
                       application_received=False, plaintext_match=False, duplicate_receives=0,
                       completion_observed_ns=None,
                       latency_interval="controller SUBMIT entry to verified receiver event observation; management IPC/log/poll included; not one-way E2E or RTT",
                       timeout=False, failure=None)
            before = len(events.records.get(receiver, []))
            started = last_start = now()
            deadline = min(started + config["timeout_s"], budget_end)
            try:
                response = controller.invoke_service_inline(
                    sender, scenario_id=f"aes-compare:{run_id}", step_id=message_id,
                    service_operation="SUBMIT", request={
                        "destination": STATIONS[receiver], "payload_format": name,
                        "payload_base64": base64.b64encode(body).decode("ascii"),
                        "payload_sha256": digest, "ack_required": config["ack_required"],
                    })
                result = response.get("body") or {}
                row["transfer_id"] = result.get("transfer_id")
                row["submit_accepted"] = result.get("call_outcome") == "OK" and bool(row["transfer_id"])
                if not row["submit_accepted"]:
                    row["failure"] = "submit_rejected:" + str(result.get("public_error_code"))
                else:
                    while now() < deadline:
                        events.poll()
                        # A management call or poll may overrun the deadline: fail conservatively.
                        observed = now()
                        if observed >= deadline:
                            break
                        hits = [r for r in events.records.get(receiver, [])[before:]
                                if r.get("event_type") == "DATA_RECEIVED"
                                and r.get("original_source") == STATIONS[sender]
                                and r.get("original_destination") == STATIONS[receiver]
                                and r.get("payload_format") == name
                                and r.get("payload_sha256") == digest]
                        if hits:
                            hit = hits[0]
                            # ON intentionally logs a digest rather than decrypted payload bytes.
                            match = hit.get("payload_len") == len(body)
                            if hit.get("payload_base64") is not None:
                                match = match and base64.b64decode(hit["payload_base64"], validate=True) == body
                            row["application_received"] = True
                            row["plaintext_match"] = match
                            if match:
                                row["completion_observed_ns"] = int((observed - started) * 1e9)
                            else:
                                row["failure"] = "application_mismatch"
                            break
                        sleep(min(0.05, max(0, deadline - now())))
                    if not row["application_received"]:
                        row["timeout"] = True
                        row["failure"] = "application_timeout"
            except Exception as exc:
                row["failure"] = "controller_failure:" + type(exc).__name__
            rows.append(row)
            print(f"[{config['security']}] Body={size} iteration={iteration} "
                  f"delivered={row['plaintext_match']} failure={row['failure']}", flush=True)
        if exhausted:
            break
    events.poll()
    # Count all duplicates observed by run end, separately from delivery successes.
    for row in rows:
        hits = [r for r in events.records.get(receiver, [])[baseline.get(receiver, 0):]
                if r.get("event_type") == "DATA_RECEIVED"
                and r.get("original_source") == row["source"]
                and r.get("original_destination") == row["destination"]
                and r.get("payload_format") == row["payload_format"]
                and r.get("payload_sha256") == row["body_sha256"]]
        row["duplicate_receives"] = max(0, len(hits) - 1)
    _write_csv(output / "messages.csv", rows, ["run_id", "condition_id", "message_id", "security", "backend"])
    _write_csv(output / "unsupported.csv", unsupported, ["requested_body_bytes", "reason"])
    manifest = dict(run_id=run_id, backend=backend, hardware_executed=backend == "B210",
                    config=config, attempted=len(rows), budget_exhausted=exhausted,
                    compare_settings_sha256=settings_sha, link_settings=link_settings,
                    planned_attempts=len(supported_conditions(config)) * config["iterations"],
                    elapsed_s=now() - began,
                    delivery_definition="unique application SUBMIT attempts with verified DATA_RECEIVED observed before controller timeout / unique attempts",
                    identity="existing linkage field + format + route + body SHA256; no wire additions",
                    key_distribution="preprovisioned; preparation and distribution time excluded",
                    rf_bytes="not measured; PDU and built packets are not total air traffic")
    import cryptography
    manifest["environment"] = dict(python=sys.version, platform=platform.platform(),
                                   cryptography=cryptography.__version__,
                                   timer="controller perf_counter; per-endpoint perf_counter_ns for crypto",
                                   timer_resolution_s=time.get_clock_info("perf_counter").resolution)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dict(status="PASS" if rows and not exhausted and all(r["plaintext_match"] for r in rows) else "FAIL",
                attempted=len(rows), output=str(output))


def statistics_for(values):
    if not values:
        return dict(samples=0, mean_ns=None, median_ns=None, p95_ns=None)
    values = sorted(values)
    return dict(samples=len(values), mean_ns=statistics.mean(values),
                median_ns=statistics.median(values), p95_ns=values[math.ceil(.95 * len(values)) - 1])


def summarize(outputs):
    rows = []
    telemetry = []
    seen_paths = set()
    for output in map(Path, outputs):
        rows.extend(list(csv.DictReader((output / "messages.csv").open(encoding="utf-8"))))
        # Live telemetry is written by endpoint launchers in the parent service run.
        for path in (output.parent / "evidence/security_compare").glob("*.jsonl"):
            if path.resolve() not in seen_paths:
                telemetry.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
                seen_paths.add(path.resolve())
    selected_runs = {row["run_id"] for row in rows}
    telemetry = [t for t in telemetry if t.get("run_id") in selected_runs]
    for row in rows:
        protected = [t for t in telemetry if t.get("stage") == "protect"
                     and t.get("transfer_id") == row["transfer_id"]
                     and t.get("run_id") == row["run_id"]]
        packets = [t for t in telemetry if t.get("stage") == "packet_build"
                   and t.get("transfer_id") == row["transfer_id"]
                   and t.get("run_id") == row["run_id"]]
        if protected:
            tx = protected[0]
            row["pdu_bytes"] = tx["pdu_bytes"]
            row["encrypt_ns"] = tx.get("elapsed_ns")
            rx = [t for t in telemetry if t.get("stage") == "unprotect"
                  and t.get("wire_sha256") == tx["wire_sha256"]
                  and t.get("run_id") == row["run_id"]
                  and t.get("source") == int(row["source"])
                  and t.get("destination") == int(row["destination"])]
            if rx:
                row["decrypt_ns"] = rx[0].get("elapsed_ns")
            failures = [t for t in telemetry if t.get("stage") == "unprotect_error"
                        and (t.get("wire_sha256") == tx["wire_sha256"] or (
                            tx.get("security_message_fingerprint") is not None
                            and t.get("security_message_fingerprint") == tx["security_message_fingerprint"]
                            and t.get("source") == int(row["source"])
                            and t.get("destination") == int(row["destination"])))
                        and t.get("run_id") == row["run_id"]]
            row["security_errors"] = "|".join(str(t["error"]) for t in failures)
        if packets:
            row["link_ids"] = "|".join(str(n) for n in sorted({p["link_id"] for p in packets}))
            row["transmission_modes"] = "|".join(sorted({p["procedure"] for p in packets}))
            row["fragments_built"] = len({p["segment_offset"] for p in packets})
            retries = [p["retransmission_number"] for p in packets if p.get("retransmission_number") is not None]
            row["retransmissions_built"] = max(retries) if retries else None
            ids = {p["packet_id"] for p in packets if p.get("packet_id")}
            encoded = [t for t in telemetry if t.get("stage") == "BURST_ENCODED" and t.get("packet_id") in ids
                       and t.get("run_id") == row["run_id"]]
            burst_ids = {t["burst_id"] for t in encoded if t.get("burst_id")}
            if burst_ids:
                radio = [t for t in telemetry if t.get("burst_id") in burst_ids and t.get("run_id") == row["run_id"]]
                row["radio_scheduled_bursts"] = sum(t["stage"] == "BURST_SCHEDULED" for t in radio)
                row["radio_completed_bursts"] = sum(t["stage"] == "BURST_COMPLETED" for t in radio)
            row["runtime_errors"] = "|".join(str(t["error"]) for t in telemetry
                                              if t.get("error") and t.get("run_id") == row["run_id"]
                                              and (t.get("packet_id") in ids or t.get("transfer_id") == row["transfer_id"]))
    summaries = []
    keys = sorted({(r["backend"], r.get("compare_settings_sha256", "legacy"), r["condition_id"], r["security"]) for r in rows})
    for backend, settings, condition, security in keys:
        group = [r for r in rows if (r["backend"], r.get("compare_settings_sha256", "legacy"), r["condition_id"], r["security"]) == (backend, settings, condition, security)]
        success = [r for r in group if str(r["plaintext_match"]) == "True" and str(r["timeout"]) != "True"]
        summary = dict(backend=backend, compare_settings_sha256=settings, condition_id=condition, security=security,
                       attempts=len(group), delivered=len(success), failures=len(group) - len(success),
                       timeouts=sum(str(r["timeout"]) == "True" for r in group),
                       delivery_rate=len(success) / len(group),
                       latency_basis="controller management-observed completion, successful on-time messages only",
                       security_error_events=sum(bool(r.get("security_errors")) for r in group))
        for name in ("pdu_bytes", "fragments_built"):
            observed = sorted({int(r[name]) for r in group if r.get(name) not in (None, "")})
            summary[name + "_observed"] = "|".join(map(str, observed))
        for metric in ("completion_observed", "encrypt", "decrypt"):
            samples = success if metric == "completion_observed" else group
            values = [float(r[metric + "_ns"]) for r in samples if r.get(metric + "_ns") not in (None, "")]
            summary.update({metric + "_" + k: v for k, v in statistics_for(values).items()})
        summaries.append(summary)
    deltas = []
    for on in [s for s in summaries if s["security"] == "ON"]:
        off = next((s for s in summaries if s["security"] == "OFF" and s["condition_id"] == on["condition_id"] and s["backend"] == on["backend"] and s["compare_settings_sha256"] == on["compare_settings_sha256"]), None)
        if off:
            delta = dict(backend=on["backend"], condition_id=on["condition_id"],
                         compare_settings_sha256=on["compare_settings_sha256"],
                         off_failures=off["failures"], on_failures=on["failures"],
                         off_latency_samples=off["completion_observed_samples"],
                         on_latency_samples=on["completion_observed_samples"],
                         delivery_rate_on_minus_off=on["delivery_rate"] - off["delivery_rate"])
            for name in ("pdu_bytes", "fragments_built"):
                delta[name + "_off_observed"] = off[name + "_observed"]
                delta[name + "_on_observed"] = on[name + "_observed"]
                left, right = off[name + "_observed"], on[name + "_observed"]
                delta[name + "_on_minus_off"] = int(right) - int(left) if left.isdigit() and right.isdigit() else None
            for stat in ("mean", "median", "p95"):
                key = "completion_observed_" + stat + "_ns"
                delta[key + "_on_minus_off"] = on[key] - off[key] if on[key] is not None and off[key] is not None else None
            deltas.append(delta)
    return rows, summaries, deltas, telemetry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "software", "hardware"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name in ("software", "hardware"):
            p.add_argument("--output", required=True)
        if name == "hardware":
            p.add_argument("--prepare-only", action="store_true", help="Readiness/config check only; no RF transmission")
    p = sub.add_parser("summarize")
    p.add_argument("--inputs", nargs="+", required=True)
    p.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "summarize":
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=False)
        rows, summaries, deltas, telemetry = summarize(args.inputs)
        _write_csv(output / "messages.csv", rows)
        _write_csv(output / "summary.csv", summaries)
        _write_csv(output / "on_minus_off.csv", deltas)
        _write_csv(output / "telemetry.csv", telemetry)
        _write_csv(output / "security_failures.csv", [t for t in telemetry if t.get("stage") == "unprotect_error"],
                   ["run_id", "stage", "wire_sha256", "error"])
        drops = {}
        for t in telemetry:
            if t.get("stage") == "buffer_status":
                key = (t.get("run_id"), t.get("endpoint_id", t.get("station")))
                drops[key] = max(drops.get(key, 0), int(t.get("dropped", 0)))
        diagnostic = dict(dropped_metadata_rows=sum(drops.values()),
                          authentication_failures=sum(t.get("error") == "PRESENTATION_SECURITY_AUTHENTICATION_FAILED" for t in telemetry),
                          replay_rejections=sum("REPLAY" in str(t.get("error", "")) for t in telemetry),
                          note="Tag/ciphertext failures may match an opaque existing-header/route fingerprint, never a delivery proof. Corrupted headers remain unassigned in security_failures.csv. Missing metadata is left blank.")
        (output / "telemetry_status.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
        return 0
    config = read_config(args.config)
    if args.command == "hardware":
        return hardware_command(args.config, args.output, prepare_only=args.prepare_only)
    if args.command == "plan":
        for size in config["body_sizes"]:
            try:
                name, body = make_body(size, 0)
                print(size, name, "validated body", len(body))
            except ValueError as exc:
                print(size, "UNSUPPORTED", str(exc))
        print("Worst-case serialized attempt seconds:", len(supported_conditions(config)) * config["iterations"] * max(config["timeout_s"], config["interval_s"]))
        print("Hard workload budget seconds:", config["budget_s"])
        return 0
    from apps.mass_roc_software import SoftwareMassRocHarness
    harness = SoftwareMassRocHarness(e2e_security_mode="ENFORCED" if config["security"] == "ON" else "DISABLED", experiment=True)
    try:
        run_id = "software-" + uuid4().hex
        result = run_experiment(harness.controller, harness.events, config, args.output,
                                run_id=run_id, backend="software",
                                link_settings={"harness": "stage5_software_group", "configuration": "stage5_software_ship/control"})
        harness.write_experiment_telemetry(Path(args.output).parent, run_id=run_id)
        print(json.dumps(result))
        return 0 if result["status"] == "PASS" else 1
    finally:
        harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
