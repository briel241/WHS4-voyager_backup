"""VDS2 wire, adapter, replay and durable-state acceptance checks."""

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from tests.support.security import make_test_e2e_by_station
from tests.support.stage4 import MASS_A_STATION_ID, MASS_B_STATION_ID, ROC_STATION_ID
from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.presentation import PresentationDecodeRequest, PresentationEncodeRequest
from vdeter.common.errors import ErrorCode
from vdeter.common.errors import ErrorCategory, Layer, ServiceErrorCategory
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.presentation.adapter import build_presentation_adapter
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope
from vdeter.profiles.payload_formats import STAGE5_PAYLOAD_CONTENT_VERSION
from vdeter.security.crypto.aes_gcm import decrypt_detached, encrypt_detached
from vdeter.security.constants import MAX_COUNTER, MAX_GCM_BLOCKS
from vdeter.security.dependencies import E2eSecurityDependencies
from vdeter.security.errors import SecurityFault
from vdeter.security.errors import presentation_security_error
from vdeter.security.protocol.aad import build_aad
from vdeter.security.protocol.header import SecurityHeader
from vdeter.security.protocol.nonce import build_nonce
from vdeter.security.protocol.pdu import parse_security_pdu
from vdeter.security.session.manager import SecuritySessionManager
from vdeter.security.session.material import DirectionalSessionMaterial
from vdeter.security.session.provider import MaterialBundle, parse_material_bundle, validate_reciprocal_bundle
from vdeter.security.session.replay import ReplayWindow
from vdeter.security.session.state_store import SQLiteSecurityStateStore
from vdeter.runtime.projection import PublicErrorCode, to_service_error

KEY = bytes(range(32))
NP = bytes(range(0xA0, 0xA8))
HEADER = bytes.fromhex("80000100000001")
PREFIX = bytes.fromhex("ff7f03")
AAD = bytes.fromhex("56445332ff7f0300000064000000c880000100000001")
BODY = bytes.fromhex("01d000")
CT = bytes.fromhex("325357")
TAG = bytes.fromhex("8abcd98f6ebc07e1f80d1c027e461b4a")
ACK = PayloadFormat("sc_vde.route_control.ack")


def route(source, destination):
    return RouteAddresses(OriginalSource(StationId(source)),
                          OriginalDestination(Addressed(StationId(destination))))


def registry():
    result = build_payload_format_registry(RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION)
    assert result.is_ok
    result.value.freeze()
    return result.value


def adapter(dependency):
    result = build_presentation_adapter(registry(), e2e_security=dependency)
    assert result.is_ok, getattr(result, "error", None)
    return result.value


def encode(instance, source, destination, body=BODY):
    return instance.encode_payload(PresentationEncodeRequest(
        TransferId(str(uuid4())), route(source, destination), ACK, body,
    ))


def test_wire_known_answer_and_mutations():
    r = route(100, 200)
    header = SecurityHeader(1, 1)
    assert header.to_bytes() == HEADER
    assert build_nonce(NP, 1) == NP + bytes.fromhex("00000001")
    assert build_aad(prefix=PREFIX, route=r, security_header=HEADER) == AAD
    ciphertext, tag = encrypt_detached(key=KEY, nonce=build_nonce(NP, 1), plaintext=BODY, aad=AAD)
    assert (ciphertext, tag) == (CT, TAG)
    assert decrypt_detached(key=KEY, nonce=build_nonce(NP, 1), ciphertext=CT, tag=TAG, aad=AAD) == BODY
    for mutation in (dict(ciphertext=b"\x00" + CT[1:]), dict(tag=b"\x00" + TAG[1:]), dict(aad=AAD[:6] + b"\x02" + AAD[7:])):
        fields = dict(key=KEY, nonce=build_nonce(NP, 1), ciphertext=CT, tag=TAG, aad=AAD)
        fields.update(mutation)
        with pytest.raises(SecurityFault) as fault:
            decrypt_detached(**fields)
        assert fault.value.code == ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED


@pytest.mark.parametrize("data,code", [
    (bytes.fromhex("40000100000001"), ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED),
    (bytes.fromhex("80000100000000"), ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID),
    (bytes.fromhex("80000200000001"), ErrorCode.PRESENTATION_SECURITY_EPOCH_MISMATCH),
    (bytes.fromhex("80000101000001"), ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID),
])
def test_header_rejects_invalid_v2_fields(data, code):
    with pytest.raises(SecurityFault) as fault:
        SecurityHeader.parse(data)
    assert fault.value.code == code


def test_replay_window_out_of_order_duplicate_and_too_old():
    window = ReplayWindow()
    for counter in (10, 12, 11):
        window.commit(window.candidate(counter))
    assert window.snapshot().highest_counter == 12
    with pytest.raises(SecurityFault) as duplicate:
        window.precheck(11)
    assert duplicate.value.code == ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE
    window.commit(window.candidate(100))
    with pytest.raises(SecurityFault) as old:
        window.precheck(35)
    assert old.value.code == ErrorCode.PRESENTATION_SECURITY_REPLAY_TOO_OLD


def test_adapter_bidirectional_replay_and_no_plaintext_fallback():
    ids = {MASS_A_STATION_ID.value: "stage5-ship-a",
           MASS_B_STATION_ID.value: "stage5-ship-b", ROC_STATION_ID.value: "stage5-roc"}
    dependencies = make_test_e2e_by_station(ids)
    mass = adapter(dependencies[MASS_A_STATION_ID.value])
    roc = adapter(dependencies[ROC_STATION_ID.value])
    forward = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value)
    assert forward.is_ok, getattr(forward, "error", None)
    wire = forward.value.data
    assert wire[:3] == PREFIX and len(wire) == 29 and wire[3:10] == HEADER
    assert wire[10:] != BODY
    received = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), wire))
    assert received.is_ok and received.value.application_payload == BODY
    duplicate = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), wire))
    assert duplicate.is_err and duplicate.error.code == ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE
    plaintext = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), PREFIX + BODY))
    assert plaintext.is_err
    second = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value)
    assert second.is_ok and second.value.data[6:10] == (2).to_bytes(4, "big")
    second_wire = second.value.data
    tampered = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), second_wire[:-1] + bytes((second_wire[-1] ^ 1,))))
    assert tampered.is_err and tampered.error.code == ErrorCode.PRESENTATION_SECURITY_AUTHENTICATION_FAILED
    assert roc._e2e_security.presentation_security.metrics.snapshot()["security_auth_failure_total"] == 1
    assert roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), second_wire)).is_ok
    reverse = encode(roc, ROC_STATION_ID.value, MASS_A_STATION_ID.value)
    assert reverse.is_ok and reverse.value.data[3:10] == HEADER
    assert mass.decode_payload(PresentationDecodeRequest(route(ROC_STATION_ID.value, MASS_A_STATION_ID.value), reverse.value.data)).is_ok
    for dependency in dependencies.values():
        dependency.presentation_security.close_final(timeout_seconds=5)


def test_adapter_size_bounds_and_pre_auth_length():
    dependencies = make_test_e2e_by_station({MASS_A_STATION_ID.value: "stage5-ship-a",
                                            MASS_B_STATION_ID.value: "stage5-ship-b",
                                            ROC_STATION_ID.value: "stage5-roc"})
    mass = adapter(dependencies[MASS_A_STATION_ID.value])
    roc = adapter(dependencies[ROC_STATION_ID.value])
    assert mass.encoded_size_bound(ACK, 3).value == 29
    assert mass.encoded_size_bound(PayloadFormat("sc_vde.position_report"), 14).value == 40
    command = PayloadFormat("sc_vde.route_control.command")
    for body_length in (10, 20, 29, 39, 48, 58, 68, 77, 87, 97, 106, 116, 125, 135, 145, 154, 164):
        assert mass.encoded_size_bound(command, body_length).value == body_length + 26
    assert mass.encoded_size_bound(command, 11).is_err
    valid = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value).value.data
    invalid_length = valid[:10] + b"x" + valid[10:]
    dropped = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), invalid_length))
    assert dropped.is_err and dropped.error.code == ErrorCode.PRESENTATION_SECURITY_CIPHERTEXT_LENGTH_INVALID
    assert roc._e2e_security.presentation_security.metrics.snapshot().get("security_auth_failure_total", 0) == 0
    for dependency in dependencies.values():
        dependency.presentation_security.close_final(timeout_seconds=5)


def test_sqlite_counter_crash_gap_replay_persistence_and_ownership():
    material_tx = DirectionalSessionMaterial(StationId(100), StationId(200), 1, KEY, NP)
    material_rx = DirectionalSessionMaterial(StationId(200), StationId(100), 1, bytes(reversed(KEY)), bytes(reversed(NP)))
    bundle = MaterialBundle("test", StationId(100), bytes(range(16)), "CSPRNG-256", "test", (material_tx, material_rx))
    with TemporaryDirectory() as temp:
        path = Path(temp) / "state.sqlite3"
        SQLiteSecurityStateStore.initialize(path, bundle)
        first = SQLiteSecurityStateStore(path, bundle)
        with pytest.raises(SecurityFault):
            SQLiteSecurityStateStore(path, bundle)
        start, stop = first.reserve_tx_block(material_tx.material_id)
        assert (start, stop) == (1, 1024)
        reservation = first.reserve_rx_verification(material_rx.material_id, 3)
        first.close()
        second = SQLiteSecurityStateStore(path, bundle)
        assert second.reserve_tx_block(material_tx.material_id) == (1025, 2048)
        assert second._db.execute("SELECT failed_authentications FROM rx_state").fetchone()[0] == 1
        assert second.rx_snapshot(material_rx.material_id) == (0, 0)
        second.close()
        path.unlink()
        with pytest.raises(SecurityFault):
            SQLiteSecurityStateStore(path, bundle)


def test_missing_explicit_dependency_is_rejected():
    with pytest.raises(TypeError):
        build_presentation_adapter(registry())
    with pytest.raises(ValueError):
        E2eSecurityDependencies(None, None)


def test_security_error_projection_is_sanitized():
    resource = {
        ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE,
        ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED,
        ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE,
    }
    codes = [value for name, value in vars(ErrorCode).items() if name.startswith("PRESENTATION_SECURITY_")]
    assert len(codes) == 11
    for code in codes:
        internal = presentation_security_error(code, ErrorCategory.PROTOCOL, "Security operation failed")
        assert internal.error.layer is Layer.PRESENTATION
        public = to_service_error(internal.error)
        assert public.code == (PublicErrorCode.RESOURCE_UNAVAILABLE if code in resource else PublicErrorCode.TRANSFER_FAILED)
        assert public.category is (ServiceErrorCategory.RESOURCE if code in resource else ServiceErrorCategory.PROTOCOL)
        assert not public.details.entries


def _bundle_record(endpoint, local, state_id, materials):
    return {
        "schema_version": 1,
        "endpoint_id": endpoint,
        "local_station_id": local,
        "state_id_b64": base64.b64encode(state_id).decode(),
        "generation_method": "CSPRNG-256",
        "provisioning_id": "test-deployment",
        "directions": [
            {
                "source_id": material.source_id.value,
                "destination_id": material.destination_id.value,
                "epoch": 1,
                "key_b64": base64.b64encode(material.aes_key).decode(),
                "nonce_prefix_b64": base64.b64encode(material.nonce_prefix).decode(),
            }
            for material in materials
        ],
    }


def test_material_bundle_strict_schema_and_reciprocal_pair():
    first = DirectionalSessionMaterial(StationId(100), StationId(200), 1, os.urandom(32), os.urandom(8))
    reverse = DirectionalSessionMaterial(StationId(200), StationId(100), 1, os.urandom(32), os.urandom(8))
    record = _bundle_record("A", 100, os.urandom(16), (first, reverse))
    raw = json.dumps(record).encode()
    bundle_a = parse_material_bundle(raw)
    record_b = _bundle_record("B", 200, os.urandom(16), (first, reverse))
    bundle_b = parse_material_bundle(json.dumps(record_b).encode())
    validate_reciprocal_bundle((bundle_a, bundle_b))
    assert first.aes_key.hex() not in repr(bundle_a)
    assert first.aes_key.hex() not in repr(first)
    for malformed in (raw[:-1], raw.replace(b'"schema_version": 1', b'"schema_version": true'),
                      raw.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')):
        with pytest.raises(ValueError):
            parse_material_bundle(malformed)
    changed = DirectionalSessionMaterial(first.source_id, first.destination_id, 1, os.urandom(32), first.nonce_prefix)
    bundle_bad = MaterialBundle("B", StationId(200), os.urandom(16), "CSPRNG-256", "test", (changed, reverse))
    with pytest.raises(ValueError):
        validate_reciprocal_bundle((bundle_a, bundle_bad))


def test_provisioning_new_directory_only(tmp_path):
    from tools.e2e_security_provision import main

    root = tmp_path / "bundles"
    root.mkdir(mode=0o700)
    state_root = tmp_path / "states"
    state_root.mkdir(mode=0o700)
    directions = {}
    for peer in (111111111, 333333333):
        for source, destination in ((peer, 222222222), (222222222, peer)):
            directions[source, destination] = DirectionalSessionMaterial(
                StationId(source), StationId(destination), 1, os.urandom(32), os.urandom(8),
            )
    by_endpoint = (("MASS-A", 111111111), ("MASS-B", 333333333), ("ROC", 222222222))
    for endpoint, station in by_endpoint:
        materials = tuple(value for (source, destination), value in directions.items()
                          if station in (source, destination))
        path = root / f"{endpoint}.json"
        path.write_text(json.dumps(_bundle_record(endpoint, station, os.urandom(16), materials)))
        path.chmod(0o600)
    assert main(["validate-bundle", "--bundle-dir", str(root)]) == 0
    args = ["initialize-state", "--bundle-dir", str(root), "--state-root", str(state_root)]
    assert main(args) == 0
    assert main(args) == 2
    (state_root / "MASS-A" / "state.sqlite3").unlink()
    assert main(args) == 2


def test_concurrent_packet_replay_only_delivers_once():
    ids = {MASS_A_STATION_ID.value: "stage5-ship-a",
           MASS_B_STATION_ID.value: "stage5-ship-b", ROC_STATION_ID.value: "stage5-roc"}
    dependencies = make_test_e2e_by_station(ids)
    mass = adapter(dependencies[MASS_A_STATION_ID.value])
    roc = adapter(dependencies[ROC_STATION_ID.value])
    wire = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value).value.data
    request = PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), wire)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(roc.decode_payload, (request, request)))
    assert sum(result.is_ok for result in results) == 1
    assert sum(result.is_err and result.error.code == ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE for result in results) == 1
    for dependency in dependencies.values():
        dependency.presentation_security.close_final(timeout_seconds=5)


def test_store_rx_replay_restored_and_usage_terminal(tmp_path):
    material_tx = DirectionalSessionMaterial(StationId(100), StationId(200), 1, os.urandom(32), os.urandom(8))
    material_rx = DirectionalSessionMaterial(StationId(200), StationId(100), 1, os.urandom(32), os.urandom(8))
    bundle = MaterialBundle("test", StationId(100), os.urandom(16), "CSPRNG-256", "test", (material_tx, material_rx))
    path = tmp_path / "state.sqlite3"
    SQLiteSecurityStateStore.initialize(path, bundle)
    first = SQLiteSecurityStateStore(path, bundle)
    reservation = first.reserve_rx_verification(material_rx.material_id, 3)
    first.commit_rx_authenticated(reservation, 1, 1)
    first.close()
    second = SQLiteSecurityStateStore(path, bundle)
    assert second.rx_snapshot(material_rx.material_id) == (1, 1)
    with pytest.raises(SecurityFault) as duplicate:
        ReplayWindow(*second.rx_snapshot(material_rx.material_id)).precheck(1)
    assert duplicate.value.code == ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE
    second._db.execute("UPDATE tx_state SET reserved_until=?", (MAX_COUNTER,))
    with pytest.raises(SecurityFault) as exhausted:
        second.reserve_tx_block(material_tx.material_id)
    assert exhausted.value.code == ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED
    second._db.execute("UPDATE rx_state SET verification_blocks_used=?", (MAX_GCM_BLOCKS,))
    with pytest.raises(SecurityFault) as exhausted_rx:
        second.reserve_rx_verification(material_rx.material_id, 3)
    assert exhausted_rx.value.code == ErrorCode.PRESENTATION_SECURITY_USAGE_EXHAUSTED
    second.close()
    with pytest.raises(SecurityFault):
        SQLiteSecurityStateStore(path, bundle)


def test_final_close_rejects_reuse():
    ids = {MASS_A_STATION_ID.value: "stage5-ship-a",
           MASS_B_STATION_ID.value: "stage5-ship-b", ROC_STATION_ID.value: "stage5-roc"}
    dependencies = make_test_e2e_by_station(ids)
    mass = adapter(dependencies[MASS_A_STATION_ID.value])
    for dependency in dependencies.values():
        dependency.presentation_security.close_final(timeout_seconds=5)
        dependency.presentation_security.close_final(timeout_seconds=5)
    rejected = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value)
    assert rejected.is_err and rejected.error.code == ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE


def test_unexpected_decrypt_failure_inhibits_rx(monkeypatch):
    ids = {MASS_A_STATION_ID.value: "stage5-ship-a",
           MASS_B_STATION_ID.value: "stage5-ship-b", ROC_STATION_ID.value: "stage5-roc"}
    dependencies = make_test_e2e_by_station(ids)
    mass = adapter(dependencies[MASS_A_STATION_ID.value])
    roc = adapter(dependencies[ROC_STATION_ID.value])
    wire = encode(mass, MASS_A_STATION_ID.value, ROC_STATION_ID.value).value.data
    monkeypatch.setattr("vdeter.security.crypto.aes_gcm.decrypt_detached",
                        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sentinel")))
    failed = roc.decode_payload(PresentationDecodeRequest(route(MASS_A_STATION_ID.value, ROC_STATION_ID.value), wire))
    assert failed.is_err and failed.error.code == ErrorCode.PRESENTATION_SECURITY_STATE_UNAVAILABLE
    status = next(iter(roc._e2e_security.presentation_security.manager._rx.values())).store._db.execute(
        "SELECT status FROM rx_state WHERE source_id=? AND destination_id=?",
        (MASS_A_STATION_ID.value, ROC_STATION_ID.value),
    ).fetchone()[0]
    assert status == "INHIBITED"
    for dependency in dependencies.values():
        dependency.presentation_security.close_final(timeout_seconds=5)
