"""Stage 8 실제 Linux `SOCK_SEQPACKET` round-trip·packet boundary·oversized 시험
(명세 §4.1, §6.1; REQ-02, REQ-06, REQ-07). Linux 전용(WSL).
"""

from __future__ import annotations

import socket
import unittest

_LINUX = hasattr(socket, "AF_UNIX") and hasattr(socket, "SOCK_SEQPACKET")

if _LINUX:
    from vdeter.poc.api.codec import decode_wire_packet, encode_wire_packet
    from vdeter.poc.api.limits import MAX_WIRE_PACKET_BYTES
    from vdeter.poc.api.messages import parse_envelope
    from vdeter.poc.api.sdk import ClientState, PocContext, SdkEngine
    from vdeter.poc.api.transport import OversizedPacket, SeqPacketEndpoint, connected_pair
    from vdeter.poc.testing import FakeActionSubmitProvider, RecordingCallbacks, build_manifest, hello_payload
    from vdeter.poc.testing.fakes import make_observation
    from vdeter.poc.testing.server_harness import DEFAULT_TOKEN_HEX, make_engine


@unittest.skipUnless(_LINUX, "Linux SOCK_SEQPACKET 전용")
class SocketpairRoundtripTest(unittest.TestCase):
    def _send(self, ep, messages):
        for m in messages:
            ep.send_packet(encode_wire_packet(m))

    def _read_all(self, ep, engine, now):
        out = []
        while True:
            data = ep.recv_packet(timeout=0.03)
            if data is None:
                break
            out += engine.receive(parse_envelope(decode_wire_packet(data)), now)
        return out

    def test_S8_POC_API_REQ_02_packet_boundary_preserved(self):
        a, b = connected_pair()
        try:
            msgs = [b'{"a":1}', b'{"bb":22}', b'{"ccc":333}']
            for m in msgs:
                a.send_packet(m)
            got = [b.recv_packet(timeout=1.0) for _ in msgs]
            self.assertEqual(got, msgs)  # 각 datagram이 분리 보존(연결·분할 없음)
        finally:
            a.close()
            b.close()

    def test_S8_POC_API_REQ_07_oversized_packet_msg_trunc_rejected(self):
        sa, sb = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sa.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        sb.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        ep = SeqPacketEndpoint(sb)
        try:
            sa.send(b"x" * (MAX_WIRE_PACKET_BYTES + 1))
            with self.assertRaises(OversizedPacket):
                ep.recv_packet(timeout=1.0)
        finally:
            sa.close()
            sb.close()

    def test_S8_POC_API_REQ_02_full_handshake_and_event_over_socket(self):
        client_ep, server_ep = connected_pair()
        provider = FakeActionSubmitProvider()
        server = make_engine(provider=provider)
        callbacks = RecordingCallbacks()
        context = PocContext(session_id="s1", workload_id="w1", scenario_id="SC-VDE-006")
        client = SdkEngine(callbacks, context)
        server_now = 1000.0
        server.start_handshake(server_now)
        self._send(client_ep, [client.build_hello(hello_payload(DEFAULT_TOKEN_HEX, "r1", build_manifest()))])
        try:
            client_now = 2000.0
            for _ in range(60):
                if server.state.value == "RUNNING":
                    self._send(server_ep, server.drain(server_now))
                self._send(server_ep, self._read_all(server_ep, server, server_now))
                self._send(server_ep, server.tick(server_now))
                self._send(client_ep, self._read_all(client_ep, client, client_now))
                client_now += 0.06  # client ACK one-shot(50 ms) 유도
                self._send(client_ep, client.tick(client_now))
                if client.state in (ClientState.CLOSED, ClientState.ABORTED):
                    break
                if server.state.value == "RUNNING" and callbacks.started and not _pending(server):
                    if len(callbacks.events) == 0:
                        server.enqueue_observation(
                            make_observation(source_station="ROC", destination_station="MASS-A"),
                            server_now,
                        )
            self.assertEqual(server.state.value, "RUNNING")
            self.assertIn(client.state, (ClientState.ARMED, ClientState.RUNNING))
            self.assertTrue(callbacks.started)
            self.assertEqual(len(callbacks.events), 1)
            self.assertEqual(callbacks.events[0].type, "PACKET_OBSERVED")
            self.assertGreaterEqual(server.last_acked_sequence, 1)
        finally:
            client_ep.close()
            server_ep.close()


def _pending(server):
    return server.residual_snapshot()["event_queue"] > 0 or server.residual_snapshot()["replay_ring"] > 0


if __name__ == "__main__":
    unittest.main()
