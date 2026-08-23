"""TC-ARCH-017, 018, 019: registry 생명주기, 거부 규칙과 지문.

요구사항: `ARCH-REQ-02`, `ARCH-REQ-04`, `ARCH-REQ-06`
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from tests.support.fixtures import load_fake_configuration

from vdeter.common.errors import ErrorCode
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import LinkId, PayloadFormat, Vpfi
from vdeter.link.messages.builtins import build_message_registry
from vdeter.link.messages.registry import MessageCodecEntry, create_message_registry
from vdeter.link.messages.type_93 import Type93Codec
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.link_phy import (
    LinkProfileEntry,
    PhyProfileEntry,
    build_link_profile_registry,
    build_message_profile_registry,
    build_phy_profile_registry,
    test_link_entries,
    test_phy_entries,
)
from vdeter.profiles.messages import MessageProfileEntry, test_entries as test_message_entries
from vdeter.profiles.models import (
    ExplicitRegistry,
    RegistryScope,
    SourceReference,
    SupportStatus,
)
from vdeter.profiles.payload_formats import (
    TEST_PAYLOAD_FORMAT,
    TEST_VPFI,
    PayloadFormatEntry,
    operational_entries,
    test_entries as test_payload_entries,
)
from vdeter.runtime.wiring import build_registries

SOURCE = SourceReference(document="stage0_baseline.md", edition="0.3.0", location="§7")


def _entry(link_id: int, scope=RegistryScope.TEST) -> LinkProfileEntry:
    return LinkProfileEntry(
        link_id=LinkId(link_id),
        scope=scope,
        support_status=SupportStatus.PLANNED,
        source=SOURCE,
        profile_version="stage0-test",
    )


class RegistryLifecycleTest(unittest.TestCase):
    """TC-ARCH-017: 등록 → 검증 → freeze → 조회 정상 경로."""

    def test_TC_ARCH_017_lifecycle_happy_path(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        self.assertFalse(registry.is_frozen)
        self.assertTrue(registry.register(_entry(63)).is_ok)
        self.assertTrue(registry.validate().is_ok)
        frozen = registry.freeze()
        self.assertTrue(frozen.is_ok)
        self.assertTrue(registry.is_frozen)
        found = registry.lookup(63)
        self.assertTrue(found.is_ok)
        self.assertEqual(LinkId(63), found.value.link_id)

    def test_TC_ARCH_017_lookup_before_freeze_is_rejected(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        registry.register(_entry(63))
        result = registry.lookup(63)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_NOT_FROZEN, result.error.code)

    def test_TC_ARCH_017_all_four_registries_build_and_freeze(self):
        payload_format = build_payload_format_registry(RegistryScope.TEST, "v1")
        message = build_message_registry(RegistryScope.TEST, "v1").value
        message_profile = build_message_profile_registry(RegistryScope.TEST, "v1")
        link_profile = build_link_profile_registry(RegistryScope.TEST, "v1")
        phy_profile = build_phy_profile_registry(RegistryScope.TEST, "v1")
        for result in (payload_format, message_profile, link_profile, phy_profile):
            self.assertTrue(result.is_ok, result.error if result.is_err else "")
        for registry in (
            payload_format.value,
            message,
            message_profile.value,
            link_profile.value,
            phy_profile.value,
        ):
            with self.subTest(registry=getattr(registry, "name", type(registry).__name__)):
                self.assertTrue(registry.freeze().is_ok)

    def test_TC_ARCH_017_payload_format_wire_lookup(self):
        registry = build_payload_format_registry(RegistryScope.TEST, "v1").value
        registry.freeze()
        found = registry.lookup(PayloadFormat(TEST_PAYLOAD_FORMAT))
        self.assertTrue(found.is_ok)
        wire = registry.lookup_wire(Vpfi(TEST_VPFI), None)
        self.assertTrue(wire.is_ok)
        self.assertEqual(found.value, wire.value)

    def test_TC_ARCH_017_operational_catalogues_follow_current_stage(self):
        # Stage 5 승인 catalogue (`stage5_service_integration_baseline.md` §7):
        # 운영 registry에는 R1027 공식 mapping 두 entry만 등록하고 시험 VPFI는
        # 등록하지 않는다. 전자해도(`electronic_chart.data`)는 registry entry가 없다.
        operational = operational_entries()
        self.assertEqual(
            ("ais.authentication_integrity.scheme1", "x509.certificate_report"),
            tuple(sorted(entry.payload_format.value for entry in operational)),
        )
        self.assertNotIn(
            TEST_PAYLOAD_FORMAT,
            {entry.payload_format.value for entry in operational},
        )
        self.assertNotIn(
            "electronic_chart.data",
            {entry.payload_format.value for entry in operational},
        )
        payload_format = build_payload_format_registry(RegistryScope.OPERATIONAL, "v1").value
        payload_format.freeze()
        self.assertEqual(2, payload_format.entry_count())
        message = build_message_registry(RegistryScope.OPERATIONAL, "v1").value
        message.freeze()
        self.assertEqual(13, message.entry_count())


class RegistryRejectionTest(unittest.TestCase):
    """TC-ARCH-018: duplicate, unknown, freeze 후 변경과 scope 혼용 거부."""

    def test_TC_ARCH_018_duplicate_key_is_rejected(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        self.assertTrue(registry.register(_entry(63)).is_ok)
        duplicate = registry.register(_entry(63))
        self.assertTrue(duplicate.is_err)
        self.assertEqual(ErrorCode.REGISTRY_DUPLICATE_KEY, duplicate.error.code)

    def test_TC_ARCH_018_unknown_lookup_is_rejected_without_fallback(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        registry.register(_entry(63))
        registry.freeze()
        unknown = registry.lookup(11)
        self.assertTrue(unknown.is_err)
        self.assertEqual(ErrorCode.REGISTRY_UNKNOWN_KEY, unknown.error.code)
        self.assertEqual(1, registry.entry_count())
        self.assertTrue(registry.lookup(63).is_ok)

    def test_TC_ARCH_018_registration_after_freeze_is_rejected(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        registry.register(_entry(63))
        registry.freeze()
        blocked = registry.register(_entry(11))
        self.assertTrue(blocked.is_err)
        self.assertEqual(ErrorCode.REGISTRY_FROZEN, blocked.error.code)
        self.assertEqual(1, registry.entry_count())

    def test_TC_ARCH_018_double_freeze_is_rejected(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        registry.freeze()
        again = registry.freeze()
        self.assertTrue(again.is_err)
        self.assertEqual(ErrorCode.REGISTRY_FROZEN, again.error.code)

    def test_TC_ARCH_018_scope_mixing_is_rejected(self):
        operational = ExplicitRegistry(
            "link_profile", RegistryScope.OPERATIONAL, "v1", LinkProfileEntry
        )
        result = operational.register(_entry(63, RegistryScope.TEST))
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_SCOPE_MISMATCH, result.error.code)

        test_registry = ExplicitRegistry(
            "link_profile", RegistryScope.TEST, "v1", LinkProfileEntry
        )
        reverse = test_registry.register(_entry(63, RegistryScope.OPERATIONAL))
        self.assertTrue(reverse.is_err)
        self.assertEqual(ErrorCode.REGISTRY_SCOPE_MISMATCH, reverse.error.code)

    def test_TC_ARCH_018_wrong_entry_type_is_rejected(self):
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        wrong = registry.register(
            PhyProfileEntry(
                link_id=LinkId(63),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.PLANNED,
                source=SOURCE,
            )
        )
        self.assertTrue(wrong.is_err)
        self.assertEqual(ErrorCode.REGISTRY_INVALID_ENTRY, wrong.error.code)

    def test_TC_ARCH_018_mutable_entry_type_is_rejected_before_registration(self):
        @dataclass
        class MutableEntry:
            key: int
            scope: RegistryScope

            def registry_key(self):
                return self.key

        with self.assertRaises(InvalidValueError):
            ExplicitRegistry("mutable", RegistryScope.TEST, "v1", MutableEntry)

    def test_TC_ARCH_018_catalogue_builders_propagate_duplicate_registration(self):
        cases = (
            (
                "payload_format",
                "vdeter.presentation.registry.entries_for",
                build_payload_format_registry,
                test_payload_entries()[0],
            ),
            (
                "link_profile",
                "vdeter.profiles.link_phy.test_link_entries",
                build_link_profile_registry,
                test_link_entries()[0],
            ),
            (
                "phy_profile",
                "vdeter.profiles.link_phy.test_phy_entries",
                build_phy_profile_registry,
                test_phy_entries()[0],
            ),
            (
                "message_profile",
                "vdeter.profiles.messages.entries_for",
                build_message_profile_registry,
                test_message_entries()[0],
            ),
        )
        for name, target, builder, entry in cases:
            with self.subTest(registry=name), patch(target, return_value=(entry, entry)):
                result = builder(RegistryScope.TEST, "v1")
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.REGISTRY_DUPLICATE_KEY, result.error.code)

    def test_TC_ARCH_018_wiring_rejects_partial_registry_after_duplicate(self):
        entry = test_link_entries()[0]
        configuration = load_fake_configuration()
        with patch(
            "vdeter.profiles.link_phy.test_link_entries", return_value=(entry, entry)
        ):
            result = build_registries(configuration)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_DUPLICATE_KEY, result.error.code)

    def test_TC_ARCH_018_test_vpfi_cannot_enter_operational_registry(self):
        operational = build_payload_format_registry(RegistryScope.OPERATIONAL, "v1").value
        result = operational.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat(TEST_PAYLOAD_FORMAT),
                vpfi=Vpfi(TEST_VPFI),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.PLANNED,
                source=SOURCE,
            )
        )
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_SCOPE_MISMATCH, result.error.code)

    def test_TC_ARCH_018_message_registry_rejects_only_unknown_operational_codec(self):
        registry = build_message_registry(RegistryScope.OPERATIONAL, "v1").value
        registry.freeze()
        for message_type in (92, 93):
            with self.subTest(message_type=message_type):
                result = registry.lookup(message_type)
                self.assertTrue(result.is_ok)
        result = registry.lookup(255)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_UNKNOWN_KEY, result.error.code)


class RegistryFingerprintTest(unittest.TestCase):
    """TC-ARCH-019: 지문의 입력 동일성과 등록 순서 독립성."""

    @staticmethod
    def _registry(entries) -> ExplicitRegistry:
        registry = ExplicitRegistry("link_profile", RegistryScope.TEST, "v1", LinkProfileEntry)
        registry.register_all(entries)
        return registry

    def test_TC_ARCH_019_same_input_gives_same_fingerprint(self):
        first = self._registry([_entry(11), _entry(17)])
        second = self._registry([_entry(11), _entry(17)])
        self.assertEqual(
            first.fingerprint().fingerprint, second.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_registration_order_does_not_change_fingerprint(self):
        forward = self._registry([_entry(11), _entry(17), _entry(19)])
        reverse = self._registry([_entry(19), _entry(17), _entry(11)])
        self.assertEqual(
            forward.fingerprint().fingerprint, reverse.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_content_change_changes_fingerprint(self):
        base = self._registry([_entry(11)])
        changed = self._registry([_entry(12)])
        self.assertNotEqual(
            base.fingerprint().fingerprint, changed.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_scope_and_version_are_part_of_fingerprint(self):
        test_registry = ExplicitRegistry(
            "link_profile", RegistryScope.TEST, "v1", LinkProfileEntry
        )
        other_version = ExplicitRegistry(
            "link_profile", RegistryScope.TEST, "v2", LinkProfileEntry
        )
        self.assertNotEqual(
            test_registry.fingerprint().fingerprint, other_version.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_registries_have_independent_fingerprints(self):
        link_profile = build_link_profile_registry(RegistryScope.TEST, "v1").value
        phy_profile = build_phy_profile_registry(RegistryScope.TEST, "v1").value
        self.assertNotEqual(
            link_profile.fingerprint().fingerprint, phy_profile.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_codec_object_is_excluded_from_fingerprint(self):
        def build(codec):
            registry = create_message_registry(RegistryScope.TEST, "v1")
            registry.register(
                MessageCodecEntry(
                    message_type=93,
                    codec=codec,
                    codec_ref="test.stage0.codec",
                    dto_name="ShortDataNoAckBody",
                    scope=RegistryScope.TEST,
                    support_status=SupportStatus.PLANNED,
                    source=SOURCE,
                )
            )
            return registry

        first = build(Type93Codec())
        second = build(Type93Codec())
        self.assertEqual(
            first.fingerprint().fingerprint, second.fingerprint().fingerprint
        )

    def test_TC_ARCH_019_fingerprint_reports_metadata(self):
        registry = self._registry(test_link_entries())
        fingerprint = registry.fingerprint()
        self.assertEqual("link_profile", fingerprint.name)
        self.assertIs(RegistryScope.TEST, fingerprint.scope)
        self.assertEqual(1, fingerprint.entry_count)
        self.assertEqual(64, len(fingerprint.fingerprint))


class MessageProfileRegistryTest(unittest.TestCase):
    """TC-ARCH-017 보조: Message 표준 metadata registry."""

    def test_TC_ARCH_017_message_profile_registry_uses_type_key(self):
        registry = build_message_profile_registry(RegistryScope.TEST, "v1").value
        registry.freeze()
        found = registry.lookup(255)
        self.assertTrue(found.is_ok)
        self.assertIsInstance(found.value, MessageProfileEntry)


if __name__ == "__main__":
    unittest.main()
