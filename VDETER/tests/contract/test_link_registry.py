"""TC-LINK-001: Stage 1 Message/Link registry 교차검증."""

from __future__ import annotations

import unittest

from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.messages.registry import validate_message_registry_alignment
from vdeter.profiles.link_phy import build_link_profile_registry, build_message_profile_registry
from vdeter.profiles.models import RegistryScope


class LinkRegistryContractTest(unittest.TestCase):
    def test_TC_LINK_001_operational_registries_have_approved_keys(self):
        codecs = build_message_registry(RegistryScope.OPERATIONAL).value
        messages = build_message_profile_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        links = build_link_profile_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        self.assertEqual((0, 4, 13, 20, 21, 22, 74, 75, 76, 81, 90, 92, 93), codecs.keys())
        self.assertEqual(codecs.keys(), messages.keys())
        self.assertEqual((11, 17, 19), links.keys())
        self.assertTrue(validate_message_registry_alignment(messages, codecs).is_ok)

    def test_TC_LINK_001_freeze_and_fingerprint_are_deterministic(self):
        first = build_message_registry(RegistryScope.OPERATIONAL).value
        second = build_message_registry(RegistryScope.OPERATIONAL).value
        first_fingerprint = first.freeze().value
        second_fingerprint = second.freeze().value
        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertEqual(13, first_fingerprint.entry_count)

    def test_TC_LINK_001_link_profile_capacities_are_exact(self):
        registry = build_link_profile_registry(RegistryScope.OPERATIONAL, "stage1-link-0.5.3").value
        values = {
            entry.link_id.value: (
                entry.packet_capacity_bytes,
                entry.message_region_bytes,
                entry.max_messages_per_packet,
                entry.fragment_capacity_bytes,
            )
            for entry in registry.entries()
        }
        self.assertEqual({11: (54, 50, 4, 35), 17: (234, 230, 19, 215), 19: (702, 698, 58, 683)}, values)


if __name__ == "__main__":
    unittest.main()
