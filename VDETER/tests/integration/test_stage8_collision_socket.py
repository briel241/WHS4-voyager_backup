"""Real TEST sockets + real Stage 7 endpoint objects, RF boundary is fake.

The existing harness owns three endpoints in ONE child process. This test
does not claim three independent real B210 launchers or hardware timing PASS.
"""
import contextlib
import sys
import unittest
from tests.integration import test_stage8_launcher_poc_device as harness


@contextlib.contextmanager
def _attested_installation():
    """이 설치가 dual-channel 지원 조건을 충족한다고 **증언된** 상태를 임시 설치에 만든다.

    위치(`StatePaths`)만 임시 디렉터리로 돌리고 판정 논리는 production 그대로다 — 증언 생성·
    결속·검사 모두 실제 producer(`capability_attestation`)를 쓴다. 판정을 True로 덮어쓰지 않으며,
    증언이 stale·불완전하면 여기서도 미지원이 된다.

    이것은 **검증 fixture**이고 실제 후보 설치의 검증 완료가 아니다(후보에는 증언이 없다).
    """
    import tempfile
    from unittest.mock import patch
    from apps.standalone import capability_attestation as CA
    from apps.standalone import poc_run
    from apps.standalone.state import StatePaths
    capability = poc_run.DUAL_CHANNEL_CAPABILITY
    original = poc_run.installed_server_config
    with tempfile.TemporaryDirectory(prefix="cap-attest-") as root:
        paths = StatePaths(root)
        installation = {"installation_sha256": "i" * 64,
                        "domains": {"rf": {"sha256": "r" * 64},
                                    "operation": {"sha256": "o" * 64},
                                    "tooling": {"sha256": "t" * 64}}}
        CA.write_attestation(paths, CA.build_attestation(
            installation, capability=capability,
            items={name: {"result": "PASS", "evidence_sha256": "e" * 64,
                          "note": "RF-OFF TEST FIXTURE - not a candidate verification"}
                   for name in CA.REQUIRED_VERIFICATION[capability]},
            producer="rf-off-integration-fixture",
            method="fixture: end-to-end wiring verification only"))
        with patch.object(poc_run, "installed_server_config",
                          lambda *a, **kw: original(paths, installation)):
            yield


@unittest.skipUnless(sys.platform.startswith("linux"), "real Unix TEST sockets")
class CollisionSocketTests(unittest.TestCase):
    _stop_child = harness.LauncherPocDeviceRpcTest._stop_child

    def setUp(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"STAGE8_TEST_TIME_ACCELERATION": str(getattr(self, "time_acceleration", 1))}):
            harness.LauncherPocDeviceRpcTest.setUp(self)

    def _wait_for_model(self, predicate, client, scheduled_hw_ns):
        import time
        # The fake device advances in discrete steps, and can intentionally run
        # slower than wall time. A fixed five wall seconds could expire BEFORE
        # the requested burst. Check the actual device clock; bound a stuck
        # harness independently. No production hold/lead/ACK timer is changed.
        wall_deadline = time.monotonic() + 45
        while time.monotonic() < wall_deadline:
            if predicate():
                return
            current = client.radio_state(fresh=True)["hardware_time_ns"]
            if current > scheduled_hw_ns + 1_500_000_000:
                return  # target + real terminal/hold budget passed: assertion fails
            time.sleep(0.05)

    def test_real_phy_overhear_survives_local_gate(self):
        import base64
        import time
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.workload_rx_scope import WorkloadRxScope
        from apps.poc_device_seams_inprocess import Link11WaveformFactory
        from vdeter.poc.api.types import PhysicalChannel
        from vdeter.b210_system.configuration import UPPER_CHANNEL_NAME, LOWER_CHANNEL_NAME
        from vdeter.poc.shared_medium.iq import to_cf32le, sha256_cf32le
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        exchange = WorkloadRxScope(clients)
        try:
            exchange.start()
            clients["ROC"].collision_session_id = exchange.session_id
            # PHY payload, not a forged service observation or delivery_log.
            packet = bytes(range(54))
            waveform = Link11WaveformFactory().encode(source_station="ROC", channel=PhysicalChannel.UPPER,
                                                       payload=packet)
            now = clients["ROC"].radio_state(fresh=True)["hardware_time_ns"]
            slots = clients["ROC"].call("poc_mac_slots", {
                "channel_name": UPPER_CHANNEL_NAME, "from_hw_ns": now + 400_000_000, "limit": 128})["slots"]
            slot = next(s for s in slots if s["function"] == "BBSC")
            response = clients["ROC"].call("poc_schedule_group_tx", {
                "burst_id": "119958ec-94d6-47a4-aef5-c0f1669a6c9b", "channel_name": UPPER_CHANNEL_NAME,
                "transmit_at_hw_ns": slot["start_hw_ns"],
                "samples_base64": base64.b64encode(to_cf32le(waveform.samples)).decode("ascii"),
                "samples_sha256": sha256_cf32le(waveform.samples),
                "destination_station_id": 111111111, "request_digest": waveform.wire_packet_digest,
                "wire_packet_digest": waveform.wire_packet_digest})
            self.assertTrue(response["accepted"], response)
            observations = []
            def got_observation():
                observations.extend(exchange.observations())
                return any(r["packet_digest"] == waveform.wire_packet_digest for r in observations)
            self._wait_for_model(got_observation, clients["ROC"], slot["start_hw_ns"])
            found = next((r for r in observations if r["packet_digest"] == waveform.wire_packet_digest), None)
            self.assertIsNotNone(found, exchange.evidence)
            self.assertEqual(packet, base64.b64decode(found["packet_base64"]))
            self.assertEqual("PHY_RECOVERED_SAMPLE0_HARDWARE_NS", found["device_time_source"])
            self.assertEqual(UPPER_CHANNEL_NAME, found["physical_channel"])

            # Two physical paths into MASS-A (C1 and C6), each independently
            # decodable. The model must drop BEFORE its real MAC consumes either.
            # MASS-B can still hear ROC on its other endpoint path; global drop
            # merely because two transmitters exist would be incorrect.
            clients["MASS-B"].collision_session_id = exchange.session_id
            before = exchange.evidence["endpoints"]["MASS-A"]["counts"]["dropped"]
            encoded = [(source, burst_id, Link11WaveformFactory().encode(
                source_station=source, channel=PhysicalChannel.UPPER, payload=payload))
                for source, burst_id, payload in (
                    ("ROC", "219958ec-94d6-47a4-aef5-c0f1669a6c9b", packet),
                    ("MASS-B", "319958ec-94d6-47a4-aef5-c0f1669a6c9b", bytes(reversed(packet))))]
            now = clients["ROC"].radio_state(fresh=True)["hardware_time_ns"]
            slots = clients["ROC"].call("poc_mac_slots", {
                "channel_name": UPPER_CHANNEL_NAME, "from_hw_ns": now + 2_000_000_000, "limit": 128})["slots"]
            slot = next(s for s in slots if s["function"] == "BBSC")
            for source, burst_id, wave in encoded:
                hw = slot["start_hw_ns"]
                if source == "MASS-B":
                    # The software fixture has unit-rate affine bridges but
                    # endpoint-specific hardware anchors. Never copy ROC's hw.
                    bnow = clients[source].radio_state(fresh=True)["hardware_time_ns"]
                    bslot = clients[source].call("poc_mac_slots", {
                        "channel_name": LOWER_CHANNEL_NAME, "from_hw_ns": bnow, "limit": 128})["slots"][0]
                    hw = bslot["start_hw_ns"] + slot["start_utc_ns"] - bslot["start_utc_ns"]
                response = clients[source].call("poc_schedule_group_tx", {
                    "burst_id": burst_id, "channel_name": UPPER_CHANNEL_NAME,
                    "transmit_at_hw_ns": hw,
                    "samples_base64": base64.b64encode(to_cf32le(wave.samples)).decode("ascii"),
                    "samples_sha256": sha256_cf32le(wave.samples),
                    "destination_station_id": 111111111, "request_digest": wave.wire_packet_digest,
                    "wire_packet_digest": wave.wire_packet_digest})
                self.assertTrue(response["accepted"], response)
            def got_drop():
                exchange.observations()
                return exchange.evidence["endpoints"]["MASS-A"]["counts"]["dropped"] > before
            self._wait_for_model(got_drop, clients["ROC"], slot["start_hw_ns"])
            diagnostic = {"model": exchange.evidence,
                          "roc_selected_hw_ns": slot["start_hw_ns"],
                          "roc_now_hw_ns": clients["ROC"].radio_state(fresh=True)["hardware_time_ns"],
                          "tx_states": {source: clients[source].call("poc_tx_burst_state", {"burst_id": burst_id})
                                        for source, burst_id, _ in encoded}}
            self.assertGreater(exchange.evidence["endpoints"]["MASS-A"]["counts"]["dropped"], before,
                               diagnostic)
        finally:
            exchange.close()

    def test_exchange_start_metadata_and_cleanup_on_real_sockets(self):
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.workload_rx_scope import WorkloadRxScope
        import time
        # Use the production serialized socket adapter, not the parent's
        # single-thread test adapter, because the exchange has a worker thread.
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        exchange = WorkloadRxScope(clients)
        try:
            exchange.start()
            time.sleep(0.25)
            exchange.check()
            exchange.poll()
            self.assertFalse(hasattr(exchange, "thread"))
            self.assertEqual(set(clients), set(exchange.evidence["endpoints"]))
            for client in clients.values():
                status = client.call("poc_wired_broadcast_optin", {"enabled": None})
                self.assertTrue(status["enabled"])
        finally:
            exchange.close()
        self.assertFalse(hasattr(exchange, "thread"))
        self.assertFalse(exchange.enabled)
        for client in clients.values():
            status = client.call("poc_wired_broadcast_optin", {"enabled": None})
            self.assertFalse(status["enabled"])

    def test_normal_service_receipt_is_real_and_bound_to_rx_tx_id(self):
        import base64
        import hashlib
        import time
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.workload_rx_scope import WorkloadRxScope
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        exchange = WorkloadRxScope(clients)
        try:
            exchange.start()
            payload = b"stage8-normal-receipt"
            result = self.controller.invoke_service_inline(
                "ROC", scenario_id="receipt", step_id="normal-service", service_operation="SUBMIT",
                request={"destination": 111111111, "payload_format": "test.stage0.opaque",
                         "payload_base64": base64.b64encode(payload).decode("ascii"),
                         "payload_sha256": hashlib.sha256(payload).hexdigest(), "ack_required": False})
            self.assertEqual("OK", result["body"]["call_outcome"], result)
            deadline = time.monotonic() + 20
            receipts = []
            while time.monotonic() < deadline:
                exchange.observations()
                receipts = [r for r in exchange.application_receipts if r["receiver"] == "MASS-A"]
                if receipts:
                    break
                time.sleep(0.05)
            self.assertTrue(receipts, exchange.evidence)
            self.assertEqual("MAC_PRESENTATION_SERVICE_ACCEPTED", receipts[0]["basis"])
            self.assertEqual(64, len(receipts[0]["packet_digest"]))
            self.assertNotIn("tx_id", receipts[0])
            observed = receipts[0]["rx_utc_ns"]
            kw = dict(digest=receipts[0]["packet_digest"], source="ROC", start_ns=observed, end_ns=observed+1)
            self.assertIsNotNone(exchange.target_receipt("MASS-A", **kw))
            self.assertIsNone(exchange.target_receipt("MASS-B", **kw))
        finally:
            exchange.close()

    def test_dual_rx_lifecycle_real_socket(self):
        from apps.stage7_mass_roc_controller import _AdapterChannel, PocWorkloadHost
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.workload_rx_scope import WorkloadRxScope
        from vdeter.b210_system.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME, LOWER_RF_TUNE_HZ, UPPER_RF_TUNE_HZ
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        previous = clients["MASS-B"].call("poc_tuning", {"mode": "READ", "rx_lo_hz": None, "lane_dsp_hz": None, "analog_bandwidth_hz": None, "snapshot": None})
        scope = WorkloadRxScope(clients, dual=True)
        try:
            scope.start()
            self.assertTrue(scope.dual_ready, scope.evidence)
            current = clients["MASS-B"].call("poc_tuning", {"mode": "READ", "rx_lo_hz": None, "lane_dsp_hz": None, "analog_bandwidth_hz": None, "snapshot": None})
            self.assertEqual(UPPER_RF_TUNE_HZ, current["lane_center_hz"]["0"])
            self.assertEqual(LOWER_RF_TUNE_HZ, current["lane_center_hz"]["1"])
            state = clients["MASS-B"].radio_state(fresh=True)
            channels = {r["channel"] for rows in state["rx_windows"].values() for r in rows}
            self.assertEqual({LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME}, channels)
        finally:
            scope.close()
        restored = clients["MASS-B"].call("poc_tuning", {"mode": "READ", "rx_lo_hz": None, "lane_dsp_hz": None, "analog_bandwidth_hz": None, "snapshot": None})
        self.assertEqual(previous, restored)
        self.assertTrue(scope.evidence["cleanup_complete"])

    def test_real_workload_required_dual_child_then_restores(self):
        import tempfile
        import os
        from apps.stage7_mass_roc_controller import _AdapterChannel, PocWorkloadHost
        from apps.standalone import poc_run
        from vdeter.poc.execution.snapshot import create_snapshot
        from vdeter.poc.testing.execution_fakes import write_poc_package
        from vdeter.poc.execution.verdict import ScenarioVerdict, PocInfrastructure
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        host = PocWorkloadHost(controller=self.controller, events=None)
        with tempfile.TemporaryDirectory(prefix="dual-workload-") as root:
            src = os.path.join(root, "poc")
            os.mkdir(src, 0o700)
            write_poc_package(src, "observe",
                capabilities=("observe_packet", "observe_dual_physical_channel"),
                required_capabilities=("observe_dual_physical_channel",))
            snapshot = create_snapshot(src, "observe", dest_dir=os.path.join(root, "snapshot"),
                scenario_id="SC-VDE-006")
            # 음성 대조군: 증언이 없는 이 설치에서는 A가 미광고다. worker 메서드 존재
            # (`dual_supported`)나 runtime `dual_ready`로 승격되지 않는다(fail-closed).
            self.assertFalse(host.selection_capabilities().wsm_g1_production_installed)
            # 지원 조건을 충족한 설치를 fixture로 세우면 required dual workload가 실제 child·
            # socket 경로로 준비·실행된다. fixture는 위치만 임시로 돌린 것이며 후보 설치의
            # 검증 완료를 뜻하지 않는다.
            with _attested_installation():
                self.assertTrue(host.selection_capabilities().wsm_g1_production_installed)
                workload = None
                try:
                    workload = poc_run.prepare_workload(host, snapshot, scenario_id="SC-VDE-006",
                        run_id="dual-scope-test", workload_id="w1",
                        socket_root=os.path.join(root, "socket"))
                    # B(runtime readiness)는 별도 경로로 성립한다 — A와 섞이지 않는다.
                    self.assertTrue(host._exchange.dual_ready)
                    self.assertIn("observe_dual_physical_channel",
                                  host.poc_server_config().advertised_capabilities())
                    workload.deliver_scenario_start()
                    for observation in host.new_observations():
                        workload.deliver_observation(observation)
                    result = workload.run(ScenarioVerdict.PASS)
                    self.assertEqual(PocInfrastructure.COMPLETED, result.poc_infrastructure)
                finally:
                    if workload is not None:
                        workload.abort()
                    host.close_collision_exchange()
                # A는 scope close로 바뀌지 않는다(설치 속성). 이전 시험은 close 후 False가 되는
                # 것을 단언했는데, 그것은 capability가 runtime readiness를 추종하던 fail-open이었다.
                self.assertTrue(host.poc_server_config().wsm_g1_production_installed)
            # 증언을 치우면 같은 host가 다시 미광고로 돌아간다 — A는 설치 증언에만 따른다.
            self.assertFalse(host.poc_server_config().wsm_g1_production_installed)
            self.assertEqual(host.selection_capabilities().advertised_capabilities(),
                             host.poc_server_config().advertised_capabilities())

    def test_dual_workload_observes_real_lower_and_upper_phy(self):
        import base64
        from apps.stage7_mass_roc_controller import _AdapterChannel
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.workload_rx_scope import WorkloadRxScope
        from apps.poc_device_seams_inprocess import Link11WaveformFactory
        from vdeter.poc.api.types import PhysicalChannel
        from vdeter.poc.shared_medium.iq import to_cf32le, sha256_cf32le
        from vdeter.b210_system.configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        clients = {s: LauncherDeviceClient(self.controller, s) for s in self.controller._channels}
        scope = WorkloadRxScope(clients, dual=True)
        try:
            scope.start()
            for client in clients.values():
                client.collision_session_id = scope.session_id
            observed = []
            for source, pc, channel, function, station, marker in (
                ("ROC", PhysicalChannel.UPPER, UPPER_CHANNEL_NAME, "BBSC", 111111111, 31),
                ("MASS-A", PhysicalChannel.LOWER, LOWER_CHANNEL_NAME, "RAC", 222222222, 41)):
                wave = Link11WaveformFactory().encode(source_station=source, channel=pc, payload=bytes([marker])*54)
                now = clients[source].radio_state(fresh=True)["hardware_time_ns"]
                slots = clients[source].call("poc_mac_slots", dict(channel_name=channel,
                    from_hw_ns=now+500000000, limit=128))["slots"]
                slot = next(s for s in slots if s["function"] == function)
                response = clients[source].call("poc_schedule_group_tx", dict(
                    burst_id=f"00000000-0000-4000-8000-{marker:012d}", channel_name=channel,
                    transmit_at_hw_ns=slot["start_hw_ns"],
                    samples_base64=base64.b64encode(to_cf32le(wave.samples)).decode("ascii"),
                    samples_sha256=sha256_cf32le(wave.samples), destination_station_id=station,
                    request_digest=wave.wire_packet_digest, wire_packet_digest=wave.wire_packet_digest))
                self.assertTrue(response["accepted"], response)
                def found():
                    observed.extend(scope.observations())
                    return any(r["packet_digest"] == wave.wire_packet_digest for r in observed)
                self._wait_for_model(found, clients[source], slot["start_hw_ns"])
                matched = [r for r in observed if r["packet_digest"] == wave.wire_packet_digest]
                self.assertTrue(matched, scope.evidence)
                self.assertEqual(channel, matched[0]["physical_channel"])
                self.assertEqual(source, matched[0]["source_endpoint"])
        finally:
            scope.close()
