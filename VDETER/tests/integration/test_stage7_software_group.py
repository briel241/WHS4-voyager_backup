"""Stage 7 3-endpoint software 통합 (`stage7` §7 software 검증, §16.1 순서).

fake dual-lane 매질 위에서 §16.1 command 순서로 group을 RUNNING까지 올리고,
frozen MAC의 실제 TBB 방송·두 MASS 획득과 public Service Short Data 왕복을
검증한다. software-only 판정이며 hardware P0~P3를 대체하지 않는다.
"""

import hashlib
import threading
import unittest

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.service import ServiceStartRequest, SubmitTransferRequest
from vdeter.common.events import ServiceEventType
from vdeter.common.identifiers import PayloadFormat, StationId
from vdeter.b210_system.coordinator import BarrierFailure
from vdeter.b210_system.ipc import ENDPOINT_IDS, body_set_sha256

from tests.support.stage4 import HAS_CRYPTO
from tests.support.stage7 import (
    STATION_BY_ENDPOINT,
    build_stage7_software_group,
    drive_stage7_group,
    emit_tbb_active_records,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bring_group_to_admission(coordinator, members, resolved_by_endpoint, host_raw_clock):
    """§16.1 정상 P2 command 순서로 admission open까지 진행한다."""

    all_paths = ["C1", "C2", "C3", "C4", "C5", "C6"]
    coordinator.run_barrier(
        "prepare",
        {
            m: {
                "deployment_sha256": resolved_by_endpoint[m].deployment_manifest_sha256,
                "endpoint_plan_relpath": f"configs/b210_system/endpoints/{m.lower()}.toml",
                "endpoint_plan_sha256": resolved_by_endpoint[m].resolved_sha256,
                "environment_manifest_sha256": _sha("environment"),
                "member_set_sha256": _sha("member-set"),
                "p1_member_plan_relpath": None,
                "p1_member_plan_sha256": None,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "configure",
        {
            m: {
                "endpoint_config_sha256": resolved_by_endpoint[m].resolved_sha256,
                "expected_serial": resolved_by_endpoint[m].b210_serial,
                "usb_physical_path": resolved_by_endpoint[m].host_execution.usb_physical_path,
                "radio_generation": 1,
            }
            for m in ENDPOINT_IDS
        },
    )
    now = host_raw_clock()
    coordinator.run_barrier(
        "arm_rx",
        {
            m: {"rx_plan_sha256": _sha("rx-plan"), "ready_deadline_host_raw_ns": now + 60_000_000_000}
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "start_cfo",
        {
            m: {
                "cfo_plan_sha256": _sha("cfo-plan"),
                "path_ids": list(all_paths),
                "operation_deadline_host_raw_ns": now + 420_000_000_000,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "acquire_host_time",
        {
            m: {
                "profile_id": "STAGE7_PI_HOST_AFFINE_TIME_V1",
                "acquisition_attempt": 0,
                "max_acquisition_ns": 600_000_000_000,
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "calibrate_rf",
        {
            m: {
                "calibration_mode": "APPLY_AND_REVERIFY",
                "gain_plan_sha256": _sha("gain-plan"),
                "source_gain_artifact_sha256": resolved_by_endpoint[m].source_gain_artifact_sha256,
                "operational_config_sha256": resolved_by_endpoint[m].resolved_sha256,
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "PRE_CORRECTION",
                "rf_time_plan_sha256": _sha("rf-time"),
                "clock_id": "stage7-pi-host-affine-provisional-v1:sw:0",
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    candidate_bodies = {}
    candidate_set = []
    for m in ENDPOINT_IDS:
        digest = _sha(f"candidate:{m}")
        candidate_set.append([m, digest])
    for m in ENDPOINT_IDS:
        candidate_bodies[m] = {
            "candidate_set": [list(item) for item in candidate_set],
            "candidate_set_sha256": body_set_sha256(candidate_set),
            "candidate_sha256": dict(candidate_set)[m],
            "active_clock_id": "stage7-pi-host-affine-v1:stage7-sw-run:0",
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
        candidate_bodies,
        commit_command_name="commit_active_bridge",
        abort_command_name="abort_active_bridge",
    )
    coordinator._bridge_generation = 0
    coordinator.run_barrier(
        "validate_rf_time",
        {
            m: {
                "validation_phase": "POST_CORRECTION",
                "rf_time_plan_sha256": _sha("rf-time"),
                "clock_id": "stage7-pi-host-affine-v1:stage7-sw-run:0",
                "path_ids": list(all_paths),
            }
            for m in ENDPOINT_IDS
        },
    )
    for member in members.values():
        assert member.client.state == "HOST_TIME_LOCKED", member.client.state
    return coordinator.open_service_admission()


@unittest.skipUnless(HAS_CRYPTO, "cryptography extra가 필요합니다")
class TC_STAGE7_SOFTWARE_GROUP(unittest.TestCase):
    def test_full_stack_start_tbb_and_short_data(self):
        (
            clock,
            medium,
            plan,
            coordinator,
            members,
            resolved_by_endpoint,
        ) = build_stage7_software_group()

        def host_raw_clock():
            return clock.now_monotonic().total_nanoseconds()

        opened = bring_group_to_admission(
            coordinator, members, resolved_by_endpoint, host_raw_clock
        )
        self.assertTrue(opened.is_ok)
        for member in members.values():
            self.assertEqual(member.client.state, "SERVICE_ADMISSION_READY")

        # 세 application이 병렬로 public start()를 호출한다 (§16.2 순서).
        start_results = {}

        def call_start(endpoint_id):
            endpoint = members[endpoint_id].endpoint
            configuration = endpoint.dependencies.configuration.endpoint
            start_results[endpoint_id] = endpoint.start(
                ServiceStartRequest(
                    local_station_id=configuration.station_id, role=configuration.role
                )
            )

        threads = [
            threading.Thread(target=call_start, args=(endpoint_id,), daemon=True)
            for endpoint_id in ENDPOINT_IDS
        ]
        for thread in threads:
            thread.start()
        for _ in range(100):
            if coordinator.required_intents_complete():
                break
            import time as _time

            _time.sleep(0.01)
        self.assertTrue(coordinator.required_intents_complete())

        # staged frozen start: 실제 Radio adoption과 MAC begin_running.
        commits = coordinator.commit_service_start()
        staged_set = []
        for endpoint_id in ENDPOINT_IDS:
            result = commits[endpoint_id]["body"]["result"]
            self.assertEqual(result["summary_type"], "STAGE7_STAGED_SERVICE_START_V1")
            staged_set.append([endpoint_id, result["staged_service_started_sha256"]])
            self.assertEqual(members[endpoint_id].client.state, "SERVICE_START_STAGED")
        # publish 전 public admission은 닫혀 있다.
        submit_early = members["MASS-A"].endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(STATION_BY_ENDPOINT["ROC"])),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"\x01",
                ack_required=False,
            )
        )
        self.assertTrue(submit_early.is_err)
        self.assertEqual(submit_early.error.code, "SERVICE_NOT_STARTED")

        # frozen MAC의 실제 RF TBB 방송·획득 (fake 매질).
        drive_stage7_group(clock, members, 120)
        for endpoint_id in ENDPOINT_IDS:
            self.assertIsNotNone(
                members[endpoint_id].endpoint.stage4.mac.state.active_tbb,
                f"{endpoint_id} TBB",
            )
        # ROC→MASS 두 lane으로 실제 전달이 있었는지 (C1/C3 broadcast fanout).
        paths = {entry["path_id"] for entry in medium.delivery_log}
        self.assertIn("C1", paths)
        self.assertIn("C3", paths)

        records = emit_tbb_active_records(members)
        self.assertTrue(coordinator.required_tbb_complete(0))
        coordinator.publish_service_start(staged_set, bridge_generation=0)
        for thread in threads:
            thread.join(timeout=10)
        for endpoint_id in ENDPOINT_IDS:
            result = start_results.get(endpoint_id)
            self.assertIsNotNone(result, endpoint_id)
            self.assertTrue(result.is_ok, getattr(result, "error", None))
            self.assertTrue(members[endpoint_id].endpoint.public_service_published)

        # 공개 event: publish 뒤 SERVICE_STARTED가 application sink에 공개된다.
        roc_sink = members["ROC"].endpoint.start_gate.application_sink
        started_events = [
            event
            for event in roc_sink.events()
            if event.event_type is ServiceEventType.SERVICE_STARTED
        ]
        self.assertEqual(len(started_events), 1)

        # MASS-A → ROC Short Data (public Service API only).
        payload = bytes(range(29))
        submitted = members["MASS-A"].endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(STATION_BY_ENDPOINT["ROC"])),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=payload,
                ack_required=False,
            )
        )
        self.assertTrue(submitted.is_ok, getattr(submitted, "error", None))
        transfer_id = submitted.value.transfer_id
        drive_stage7_group(clock, members, 200)

        sender_sink = members["MASS-A"].endpoint.start_gate.application_sink
        states = [
            (event.transfer_state.value if event.transfer_state else None)
            for event in sender_sink.events()
            if event.event_type is ServiceEventType.TRANSFER_STATE_CHANGED
            and event.transfer_id == transfer_id
        ]
        self.assertIn("COMPLETED", states, states)
        received = [
            event
            for event in roc_sink.events()
            if event.event_type is ServiceEventType.DATA_RECEIVED
        ]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].received_data.payload, payload)
        self.assertEqual(
            received[0].received_data.original_source.value,
            STATION_BY_ENDPOINT["MASS-A"],
        )


if __name__ == "__main__":
    unittest.main()
