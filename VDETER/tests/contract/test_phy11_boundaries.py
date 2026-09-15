"""TC-PHY11-016: frozen public boundary and explicit Stage 2 exclusions."""

from __future__ import annotations

import dataclasses
import unittest

from vdeter.common.contracts.phy import (
    BasebandBurst,
    PhyDecodeRequest,
    PhyEncodeRequest,
    RecoveredPacket,
    RecoveredPacketSet,
)
from vdeter.common.contracts.radio import SAMPLE_FORMAT_TEST_IQ_INT16_LE, TEST_SCOPE_SAMPLE_FORMATS
from vdeter.phy.interface import PhyPort
from vdeter.phy.transmitter import Link11Phy
from vdeter.profiles.link_phy import build_phy_profile_registry
from vdeter.profiles.models import RegistryScope


class Phy11BoundaryTest(unittest.TestCase):
    def test_TC_PHY11_016_public_dto_and_out_of_scope_boundaries(self):
        expected = {
            PhyEncodeRequest: ("packet_id", "link_id", "channel", "transmit_at", "packet"),
            BasebandBurst: ("burst_id", "packet_id", "link_id", "channel", "transmit_at", "samples"),
            PhyDecodeRequest: ("channel", "samples", "allowed_link_ids"),
            RecoveredPacket: ("packet_id", "link_id", "channel", "received_at", "packet"),
            RecoveredPacketSet: ("channel", "received_at", "status", "packets"),
        }
        for dto, fields in expected.items():
            with self.subTest(dto=dto.__name__):
                self.assertEqual(fields, tuple(field.name for field in dataclasses.fields(dto)))
                self.assertTrue(dto.__dataclass_params__.frozen)
        self.assertIn(SAMPLE_FORMAT_TEST_IQ_INT16_LE, TEST_SCOPE_SAMPLE_FORMATS)
        self.assertTrue(issubclass(Link11Phy, PhyPort))

        registry = build_phy_profile_registry(RegistryScope.OPERATIONAL).value
        self.assertTrue(registry.freeze().is_ok)
        self.assertEqual((11,), registry.keys())
        profile = registry.lookup(11).value.parameters.fingerprint_content()
        flattened = repr(profile).lower()
        self.assertNotIn("uhd", flattened)
        self.assertNotIn("gnu radio", flattened)
        self.assertNotIn("b210", flattened)
        self.assertNotIn("rf_sensitivity", flattened)
        self.assertNotIn("sample_format_stage3", flattened)


if __name__ == "__main__":
    unittest.main()
