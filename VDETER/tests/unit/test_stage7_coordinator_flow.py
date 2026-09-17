"""Stage 7 Runtime Coordinator↔endpoint client 흐름 시험 (`stage7` §16, §16.1)."""

import unittest
import uuid

from vdeter.b210_system.coordinator import (
    BarrierFailure,
    ResolvedSystemPlan,
    RuntimeCoordinator,
)
from vdeter.b210_system.ipc import ENDPOINT_IDS, body_sha256_of
from vdeter.b210_system.records import TbbActiveRecord
from vdeter.b210_system.runtime_client import EndpointRuntimeClient

RUN_ID = "stage7-flow-run"


class _Clock:
    def __init__(self):
        self.now = 1_000_000_000_000

    def __call__(self):
        return self.now

    def advance(self, ns):
        self.now += ns


class _FakeOperations:
    def __init__(self):
        self.calls = []

    def check_prepare(self, body):
        self.calls.append("check_prepare")
        return True

    def check_configure(self, body):
        return True

    def do_configure(self, body):
        self.calls.append("do_configure")
        return True

    def run_cfo(self, body):
        self.calls.append("run_cfo")
        return True

    def acquire_host_time(self, body):
        return {"summary_type": "HOST_AFFINE_FIT_V1", "outcome": "PASS"}

    def check_calibrate(self, body):
        return True

    def run_calibrate(self, body):
        self.calls.append(f"calibrate:{body['calibration_mode']}")
        return True

    def validate_rf_time(self, body):
        return [{"summary_type": "RF_TIME_MEASUREMENT_V1", "outcome": "PASS"}]

    def check_active_candidate(self, body):
        return True

    def install_active_bridge(self, body):
        self.calls.append("install_active_bridge")
        return True

    def abort_active_bridge(self, body):
        pass

    def do_stop(self, body):
        self.calls.append("do_stop")

    def do_inhibit(self, body):
        self.calls.append("do_inhibit")


class _FakeGateEndpoint:
    """intent/staged/publish handler 표면만 흉내내는 endpoint double."""

    def __init__(self, endpoint_id):
        self.endpoint_id = endpoint_id
        self.admission = None
        self.intent_digest = None
        self.staged = False
        self.published = False
        self.failed = None

    def set_lifecycle_generation(self, generation):
        self.lifecycle_generation = generation

    def set_bridge_generation(self, generation):
        self.bridge_generation = generation

    def install_service_admission(self, **kwargs):
        self.admission = kwargs
        from vdeter.common.results import Ok

        return Ok(None)

    def discard_service_admission(self):
        self.admission = None

    def pending_intent_digest(self):
        return self.intent_digest

    def prepare_service_start(self, digest):
        from vdeter.common.results import Ok

        return Ok(None)

    def abort_service_start(self):
        pass

    def commit_service_start_staged(self):
        from vdeter.common.results import Ok

        self.staged = True
        return Ok(
            {
                "staged_service_started_sha256": "1" * 64,
                "staged_public_event_set_sha256": "2" * 64,
            }
        )

    def publish_service_start_commit(self):
        from vdeter.common.results import Ok

        self.published = True
        return Ok("started")

    def fail_pending_start(self, error, inhibit=False):
        self.failed = error


class _Channel:
    """coordinator→client 직결 in-process channel."""

    def __init__(self, client, coordinator_ref):
        self.client = client
        self.coordinator_ref = coordinator_ref

    def request(self, message):
        return self.client.handle_command(message)

    def send_event(self, event):
        coordinator = self.coordinator_ref[0]
        result = coordinator.receive_event(self.client.endpoint_id, event)
        return result.is_ok

    def poll_events(self):
        return ()


def _plan():
    return ResolvedSystemPlan(
        profile="STAGE7_PI5_THREE_ENDPOINT_V1",
        socket_root="/run/user/<uid>/vdeter-stage7/runtime",
        member_count=3,
        service_start_intent_timeout_ns=30_000_000_000,
        service_start_tbb_publish_timeout_ns=90_000_000_000,
        members=tuple((m, f"configs/{m}.toml", "9" * 64) for m in ENDPOINT_IDS),
        data_profile_sha256="8" * 64,
        deployment_sha256="7" * 64,
        tbb_fixture_id="VDETER_WIDEBAND_100_TBB_V3",
        registry_fingerprint="6" * 64,
        source_sha256="5" * 64,
        resolved_sha256="4" * 64,
    )


def _build_group(clock):
    coordinator_ref = [None]
    clients = {}
    endpoints = {}
    channels = {}
    for endpoint_id in ENDPOINT_IDS:
        endpoint = _FakeGateEndpoint(endpoint_id)
        client = EndpointRuntimeClient(
            endpoint_id=endpoint_id,
            run_id=RUN_ID,
            plan_sha256="4" * 64,
            lifecycle_generation=1,
            endpoint=endpoint,
            operations=_FakeOperations(),
            event_sender=None,
            host_raw_clock=clock,
        )
        channel = _Channel(client, coordinator_ref)
        client._event_sender = channel.send_event
        clients[endpoint_id] = client
        endpoints[endpoint_id] = endpoint
        channels[endpoint_id] = channel
    coordinator = RuntimeCoordinator(
        _plan(), channels, host_raw_clock=clock, run_id=RUN_ID, lifecycle_generation=1
    )
    coordinator_ref[0] = coordinator
    return coordinator, clients, endpoints


class TC_STAGE7_COORDINATOR_FLOW(unittest.TestCase):
    def test_full_admission_to_publish_sequence(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)
        # prepare→configure barrier.
        prepare_body = {
            "deployment_sha256": "7" * 64,
            "endpoint_plan_relpath": "configs/x.toml",
            "endpoint_plan_sha256": "9" * 64,
            "environment_manifest_sha256": "3" * 64,
            "member_set_sha256": "2" * 64,
            "p1_member_plan_relpath": None,
            "p1_member_plan_sha256": None,
        }
        coordinator.run_barrier(
            "prepare", {m: dict(prepare_body) for m in ENDPOINT_IDS}
        )
        for client in clients.values():
            self.assertEqual(client.state, "PREFLIGHT")
        # active bridge 설치 (expected_bridge_generation=null → 0).
        candidate_set = [[m, "1" * 64] for m in ENDPOINT_IDS]
        from vdeter.b210_system.ipc import body_set_sha256

        bridge_body = {
            "candidate_set": candidate_set,
            "candidate_set_sha256": body_set_sha256(candidate_set),
            "candidate_sha256": "1" * 64,
            "active_clock_id": "stage7-pi-host-affine-v1:run:0",
            "bridge_generation": 0,
            "alpha_num": 0,
            "alpha_den": 1,
            "beta_num": 1,
            "beta_den": 1,
            "correction_num": 0,
            "correction_den": 1,
        }
        coordinator.run_barrier(
            "prepare_active_bridge",
            {m: dict(bridge_body) for m in ENDPOINT_IDS},
            commit_command_name="commit_active_bridge",
            abort_command_name="abort_active_bridge",
        )
        coordinator._bridge_generation = 0
        for client in clients.values():
            self.assertEqual(client.bridge_generation, 0)
            client.state = "ARMED"
        # admission open.
        opened = coordinator.open_service_admission()
        self.assertTrue(opened.is_ok)
        window = coordinator.admission["intent_window_id"]
        for endpoint_id, endpoint in endpoints.items():
            self.assertEqual(endpoint.admission["intent_window_id"], window)
            self.assertEqual(clients[endpoint_id].state, "SERVICE_ADMISSION_READY")
        # intent events (모든 required member).
        self.assertFalse(coordinator.required_intents_complete())
        with self.assertRaises(BarrierFailure):
            coordinator.commit_service_start()
        for endpoint_id, client in clients.items():
            digest = uuid.uuid5(uuid.NAMESPACE_URL, endpoint_id).hex + "0" * 32
            endpoints[endpoint_id].intent_digest = digest
            sent = client._send_event(
                endpoint_state="SERVICE_START_PENDING", artifact_sha256=digest
            )
            self.assertTrue(sent)
        self.assertTrue(coordinator.required_intents_complete())
        # duplicate same digest → idempotent.
        first_key = coordinator.intent_ledger_snapshot()[0]
        clients["MASS-A"]._send_event(
            endpoint_state="SERVICE_START_PENDING",
            artifact_sha256=endpoints["MASS-A"].intent_digest,
        )
        self.assertEqual(coordinator.intent_ledger_snapshot()[0], first_key)
        self.assertFalse(coordinator.inhibited)
        # staged start.
        commits = coordinator.commit_service_start()
        staged_set = []
        for endpoint_id in ENDPOINT_IDS:
            result = commits[endpoint_id]["body"]["result"]
            self.assertEqual(result["summary_type"], "STAGE7_STAGED_SERVICE_START_V1")
            staged_set.append([endpoint_id, result["staged_service_started_sha256"]])
            self.assertTrue(endpoints[endpoint_id].staged)
        # TBB active events.
        for endpoint_id, client in clients.items():
            record = TbbActiveRecord(
                endpoint_id=endpoint_id,
                readiness_kind="ROC_BROADCAST_READY"
                if endpoint_id == "ROC"
                else "MASS_ACQUIRED",
                lifecycle_generation=1,
                bridge_generation=0,
                tbb_fingerprint_sha256=b"\x01" * 32,
                valid_from_utc_ns=1,
                valid_until_utc_ns=2,
                activation_frame_ordinal=3,
                local_tbb_evidence_sha256=b"\x02" * 32,
            )
            self.assertTrue(client.emit_tbb_active(record))
        self.assertTrue(coordinator.required_tbb_complete(0))
        # publish.
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        for endpoint_id, endpoint in endpoints.items():
            self.assertTrue(endpoint.published)
            self.assertEqual(clients[endpoint_id].state, "RUNNING")
        self.assertFalse(coordinator.inhibited)

    def test_conflicting_intent_digest_inhibits_group(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)
        for client in clients.values():
            client.state = "ARMED"
            client.bridge_generation = 0
        coordinator._bridge_generation = 0
        coordinator.open_service_admission()
        client = clients["MASS-A"]
        client._send_event(endpoint_state="SERVICE_START_PENDING", artifact_sha256="a" * 64)
        # conflicting digest → group inhibit.
        client._send_event(endpoint_state="SERVICE_START_PENDING", artifact_sha256="b" * 64)
        self.assertTrue(coordinator.inhibited)
        self.assertEqual(clients["MASS-B"].state, "TX_INHIBITED")

    def test_replay_returns_first_response_without_side_effects(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)
        client = clients["MASS-A"]
        operations = client._operations
        message = coordinator._make_command(
            "status",
            "MASS-A",
            {"query_command_id": None},
            barrier_id=None,
            barrier_phase=None,
            barrier_manifest_sha256=None,
        )
        first = client.handle_command(message)
        second = client.handle_command(message)
        self.assertEqual(first, second)
        # 같은 command ID에 다른 bytes → REJECTED.
        altered = dict(message)
        altered_body = {"query_command_id": str(uuid.UUID(int=5, version=4))}
        altered["body"] = altered_body
        digest = body_sha256_of(altered_body)
        altered["body_sha256"] = digest
        altered["request_body_sha256"] = digest
        third = client.handle_command(altered)
        self.assertEqual(third["body"]["status"], "REJECTED")

    def test_old_generation_rejected_before_replay(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)
        client = clients["MASS-A"]
        message = coordinator._make_command(
            "status",
            "MASS-A",
            {"query_command_id": None},
            barrier_id=None,
            barrier_phase=None,
            barrier_manifest_sha256=None,
        )
        message["expected_lifecycle_generation"] = 0
        digest = body_sha256_of(message["body"])
        message["body_sha256"] = digest
        message["request_body_sha256"] = digest
        response = client.handle_command(message)
        self.assertEqual(response["body"]["status"], "REJECTED")
        self.assertIn(message["command_id"], client.stale_rejections())

    def test_expired_new_command_times_out(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)
        client = clients["MASS-A"]
        message = coordinator._make_command(
            "status",
            "MASS-A",
            {"query_command_id": None},
            barrier_id=None,
            barrier_phase=None,
            barrier_manifest_sha256=None,
        )
        clock.advance(10_000_000_000)
        response = client.handle_command(message)
        self.assertEqual(response["body"]["status"], "TIMED_OUT")

    def test_prepare_reject_aborts_barrier(self):
        clock = _Clock()
        coordinator, clients, endpoints = _build_group(clock)

        def reject_prepare(body):
            return False

        clients["MASS-B"]._operations.check_prepare = reject_prepare
        prepare_body = {
            "deployment_sha256": "7" * 64,
            "endpoint_plan_relpath": "configs/x.toml",
            "endpoint_plan_sha256": "9" * 64,
            "environment_manifest_sha256": "3" * 64,
            "member_set_sha256": "2" * 64,
            "p1_member_plan_relpath": None,
            "p1_member_plan_sha256": None,
        }
        with self.assertRaises(BarrierFailure):
            coordinator.run_barrier(
                "prepare", {m: dict(prepare_body) for m in ENDPOINT_IDS}
            )
        # PREPARE 실패는 emergency가 아니라 ABORT다 (§16).
        self.assertFalse(coordinator.inhibited)

    def test_status_outcome_ledger_unknown(self):
        clock = _Clock()
        coordinator, clients, _ = _build_group(clock)
        client = clients["ROC"]
        query = str(uuid.UUID(int=77, version=4))
        message = coordinator._make_command(
            "status",
            "ROC",
            {"query_command_id": query},
            barrier_id=None,
            barrier_phase=None,
            barrier_manifest_sha256=None,
        )
        response = client.handle_command(message)
        self.assertEqual(response["body"]["status"], "COMPLETED")
        self.assertEqual(response["body"]["result"]["lookup_state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
