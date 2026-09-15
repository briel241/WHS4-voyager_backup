"""Stage 8 envelope 검증과 version/capability 협상 시험(명세 §6.2, §7.2; REQ-08, REQ-27).

플랫폼 무관.
"""

from __future__ import annotations

import unittest

from vdeter.poc.api import protocol
from vdeter.poc.api.errors import PocError, PocErrorCode
from vdeter.poc.api.messages import build_message, parse_envelope
from vdeter.poc.api.negotiation import (
    HelloRequest,
    ServerCapabilityConfig,
    negotiate,
    restore_matches,
)
from vdeter.poc.testing import build_manifest


def _hello(major=1, mn=0, mx=0, caps=("observe_packet", "replay_packet"),
           triggers=("SCENARIO_STARTED", "PACKET_OBSERVED")):
    return HelloRequest(
        protocol_major=major,
        minor_min=mn,
        minor_max=mx,
        requested_capabilities=frozenset(caps),
        requested_triggers=frozenset(triggers),
    )


class EnvelopeTest(unittest.TestCase):
    def _valid(self):
        return build_message(
            protocol.EVENT_ACK,
            session_id="s1",
            workload_id="w1",
            connection_epoch=1,
            protocol_minor=0,
            body={"cumulative_sequence": 3},
        )

    def test_build_parse_roundtrip(self):
        parsed = parse_envelope(self._valid())
        self.assertEqual(parsed.type, protocol.EVENT_ACK)
        self.assertEqual(parsed.session_id, "s1")
        self.assertEqual(parsed.workload_id, "w1")
        self.assertEqual(parsed.connection_epoch, 1)
        self.assertEqual(parsed.body["cumulative_sequence"], 3)

    def test_reject_unknown_top_level_field(self):
        msg = self._valid()
        msg["extra"] = 1
        with self.assertRaises(PocError):
            parse_envelope(msg)

    def test_reject_missing_field(self):
        msg = self._valid()
        del msg["session_id"]
        with self.assertRaises(PocError):
            parse_envelope(msg)

    def test_reject_unknown_type(self):
        msg = self._valid()
        msg["type"] = "BOGUS"
        with self.assertRaises(PocError):
            parse_envelope(msg)

    def test_reject_bad_schema(self):
        msg = self._valid()
        msg["schema"] = "wrong"
        with self.assertRaises(PocError):
            parse_envelope(msg)

    def test_reject_bad_protocol_shape(self):
        msg = self._valid()
        msg["protocol"] = {"major": 1}
        with self.assertRaises(PocError):
            parse_envelope(msg)


class NegotiationTest(unittest.TestCase):
    def setUp(self):
        self.server = ServerCapabilityConfig()
        self.manifest = build_manifest()

    def test_S8_POC_API_REQ_08_major_mismatch_rejected(self):
        with self.assertRaises(PocError) as ctx:
            negotiate(self.server, self.manifest, _hello(major=2), connection_epoch=1)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_VERSION_INCOMPATIBLE)

    def test_S8_POC_API_REQ_08_highest_common_minor(self):
        server = ServerCapabilityConfig(min_minor=0, max_minor=2)
        neg = negotiate(server, self.manifest, _hello(mn=0, mx=5), connection_epoch=1)
        self.assertEqual(neg.protocol_minor, 2)

    def test_S8_POC_API_REQ_08_disjoint_minor_rejected(self):
        server = ServerCapabilityConfig(min_minor=0, max_minor=0)
        with self.assertRaises(PocError) as ctx:
            negotiate(server, self.manifest, _hello(mn=1, mx=2), connection_epoch=1)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_VERSION_INCOMPATIBLE)

    def test_S8_POC_API_REQ_08_capability_intersection(self):
        neg = negotiate(self.server, self.manifest, _hello(), connection_epoch=1)
        self.assertEqual(neg.capabilities, frozenset({"observe_packet", "replay_packet"}))
        self.assertNotIn(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL, neg.capabilities)

    def test_S8_POC_API_REQ_27_dual_channel_not_advertised_by_default(self):
        manifest = build_manifest(
            requested_capabilities=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL),
            required_capabilities=(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL,),
        )
        hello = _hello(caps=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL))
        with self.assertRaises(PocError) as ctx:
            negotiate(self.server, manifest, hello, connection_epoch=1)
        self.assertEqual(ctx.exception.code, PocErrorCode.POC_CAPABILITY_UNAVAILABLE)

    def test_S8_POC_API_REQ_27_dual_channel_advertised_when_installed(self):
        server = ServerCapabilityConfig(wsm_g1_production_installed=True)
        manifest = build_manifest(
            requested_capabilities=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL),
            required_capabilities=(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL,),
        )
        hello = _hello(caps=("observe_packet", protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL))
        neg = negotiate(server, manifest, hello, connection_epoch=1)
        self.assertIn(protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL, neg.capabilities)

    def test_S8_POC_API_REQ_27_reconnect_no_expand(self):
        neg = negotiate(self.server, self.manifest, _hello(), connection_epoch=1)
        same = negotiate(self.server, self.manifest, _hello(), connection_epoch=2)
        self.assertTrue(restore_matches(neg, same))
        wider = negotiate(
            ServerCapabilityConfig(wsm_g1_production_installed=True),
            build_manifest(
                requested_capabilities=("observe_packet", "replay_packet",
                                        protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL),
            ),
            _hello(caps=("observe_packet", "replay_packet",
                         protocol.OBSERVE_DUAL_PHYSICAL_CHANNEL)),
            connection_epoch=2,
        )
        self.assertFalse(restore_matches(neg, wider))


if __name__ == "__main__":
    unittest.main()
