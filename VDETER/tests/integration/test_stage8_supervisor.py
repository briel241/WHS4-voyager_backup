"""Stage 8 PoC supervisor 통합 시험(실제 bound AF_UNIX/SOCK_SEQPACKET; thread child).

명세 §5, §8, §9, §15; REQ-02, REQ-04, REQ-26. Linux 전용(WSL). in-process thread child로
실제 socket round-trip·peercred·cleanup 잔존 0을 검증한다.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

_LINUX = hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET") and hasattr(os, "getuid")

if _LINUX:
    from vdeter.poc._supervisor import socket_dir as sd
    from vdeter.poc._supervisor.credentials import read_peer_credential, verify_peer
    from vdeter.poc._supervisor.supervisor import PocSupervisor
    from vdeter.poc._supervisor.transport import SeqPacketServer
    from vdeter.poc.api.errors import PocError, PocErrorCode
    from vdeter.poc.api.negotiation import ServerCapabilityConfig
    from vdeter.poc.api.sdk import PocContext, SdkEngine
    from vdeter.poc.api.transport import connect
    from vdeter.poc.testing import (
        ClientRunner,
        FakeActionSubmitProvider,
        RecordingCallbacks,
        ThreadChildSpawner,
        build_manifest,
        hello_payload,
    )
    from vdeter.poc.testing.fakes import make_observation


def _client_main(socket_path, token, stop_event):
    def act(event, ctx):
        if event.type == "PACKET_OBSERVED":
            return [ctx.replay(target="ROC", source_observation_id=event.body.get("observation_id"))]
        return []

    context = PocContext(session_id="s1", workload_id="w1", scenario_id="SC-VDE-006")
    callbacks = RecordingCallbacks(event_action_fn=act)
    ep = connect(socket_path)
    engine = SdkEngine(callbacks, context)
    runner = ClientRunner(ep, engine)
    runner.send_hello(hello_payload(token.hex(), "r1", build_manifest()))
    runner.run(timeout=10.0)
    ep.close()


def _reconnect_client_main(socket_path, token, stop_event):
    """ARMED 도달 뒤 같은 process에서 한 번 두절·재접속(reconnect HELLO)한다(§12)."""

    context = PocContext(session_id="s1", workload_id="w1", scenario_id="SC-VDE-006")
    callbacks = RecordingCallbacks()
    engine = SdkEngine(callbacks, context)
    ep = connect(socket_path)
    runner = ClientRunner(ep, engine)
    runner.send_hello(hello_payload(token.hex(), "r1", build_manifest()))
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and engine.state.value not in (
        "ARMED", "RUNNING", "CLOSED", "ABORTED"
    ):
        runner.pump_once(timeout=0.05)
    ep.close()  # 두절
    time.sleep(0.1)
    ep2 = connect(socket_path)  # 같은 process(같은 PID) 재접속
    runner.transport = ep2
    runner.send_hello(hello_payload(token.hex(), "r1", build_manifest()), connect_kind="reconnect")
    runner.run(timeout=8.0)
    ep2.close()


@unittest.skipUnless(_LINUX, "Linux SOCK_SEQPACKET 전용")
class SupervisorThreadChildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vdeter-poc-sup-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _make_supervisor(self, provider):
        return PocSupervisor(
            session_id="s1",
            workload_id="w1",
            run_id="r1",
            scenario_id="SC-VDE-006",
            manifest=build_manifest(),
            server_config=ServerCapabilityConfig(),
            provider=provider,
            spawner=ThreadChildSpawner(_client_main),
            socket_root=os.path.join(self.tmp, "root"),
        )

    def test_S8_POC_API_REQ_02_happy_end_to_end_and_cleanup(self):
        provider = FakeActionSubmitProvider()
        sup = self._make_supervisor(provider)
        sup.start()
        self.assertEqual(sup.expected_uid, os.getuid())
        self.assertTrue(sup.run_until(lambda: sup.engine.state.value == "RUNNING", timeout=10.0))
        sup.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup.run_until(lambda: sup.engine.last_acked_sequence >= 1, timeout=10.0))
        self.assertTrue(sup.run_until(lambda: provider.submit_calls >= 1, timeout=10.0))
        rid = provider.submitted_request_ids[0]
        sup.provide_action_terminal(rid, action_execution="TX_COMPLETED", target_effect="REJECTED_BY_TARGET")
        report = sup.close_workload()
        res = report["residual"]
        self.assertFalse(res["child_alive"])
        self.assertFalse(res["socket_file_exists"])
        self.assertFalse(res["workload_dir_exists"])
        self.assertEqual(res["engine"]["inflight_actions"], 0)
        self.assertEqual(res["engine"]["event_queue"], 0)
        self.assertEqual(res["engine"]["replay_ring"], 0)

    def test_S8_POC_API_REQ_12_same_process_reconnect_resumes(self):
        provider = FakeActionSubmitProvider()
        sup = PocSupervisor(
            session_id="s1", workload_id="w1", run_id="r1", scenario_id="SC-VDE-006",
            manifest=build_manifest(), server_config=ServerCapabilityConfig(),
            provider=provider, spawner=ThreadChildSpawner(_reconnect_client_main),
            socket_root=os.path.join(self.tmp, "root-rc"),
        )
        sup.start()
        # 같은 PID 재접속으로 connection_epoch가 2로 전진해야 한다(§12.2).
        self.assertTrue(sup.run_until(lambda: sup.engine.connection_epoch == 2, timeout=15.0))
        self.assertEqual(sup.engine.connection_epoch, 2)
        self.assertEqual(sup.engine.evidence["reconnects"], 1)
        self.assertEqual(sup.engine.state.value, "RUNNING")
        # 재접속 뒤에도 event 전달이 계속된다.
        sup.emit_observation(make_observation(source_station="ROC", destination_station="MASS-A"))
        self.assertTrue(sup.run_until(lambda: sup.engine.last_acked_sequence >= 1, timeout=8.0))
        report = sup.close_workload()
        self.assertFalse(report["residual"]["child_alive"])
        self.assertFalse(report["residual"]["socket_file_exists"])

    def test_S8_POC_API_REQ_04_peercred_pid_mismatch_rejected(self):
        # 실제 SO_PEERCRED read + PID 불일치 거부(같은 process 연결로 검증).
        root = os.path.join(self.tmp, "credroot")
        ws = sd.create_workload_socket(root, os.getuid())
        server = SeqPacketServer(ws.socket_path)
        connected = {}

        def _connect():
            connected["ep"] = connect(ws.socket_path)

        t = threading.Thread(target=_connect, daemon=True)
        t.start()
        try:
            peer = server.accept(timeout=5.0)
            self.assertEqual(peer.uid, os.getuid())
            self.assertEqual(peer.pid, os.getpid())
            verify_peer(peer, expected_uid=os.getuid(), expected_pid=os.getpid())  # 통과
            with self.assertRaises(PocError) as ctx:
                verify_peer(peer, expected_uid=os.getuid(), expected_pid=os.getpid() + 1)
            self.assertEqual(ctx.exception.code, PocErrorCode.POC_AUTH_FAILED)
        finally:
            t.join(timeout=2.0)
            if "ep" in connected:
                connected["ep"].close()
            server.close()
            sd.cleanup_workload_socket(ws)


if __name__ == "__main__":
    unittest.main()
