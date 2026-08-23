"""TC-STAGE5-008: Payload Format registry lifecycle/scope와 prefix-first RX dispatch.

요구사항: `STAGE5-REQ-08` (`stage5_service_integration_baseline.md` §6.1.1, §7)
"""

from __future__ import annotations

import unittest

from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.presentation import PresentationDecodeRequest
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import PayloadFormat, StationId, Vpfi
from vdeter.presentation.adapter import build_presentation_adapter
from vdeter.presentation.codecs.base import WireDiscriminator
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope, SourceReference, SupportStatus
from vdeter.profiles.payload_formats import (
    AIS_SCHEME1_CODEC_REF,
    CERTIFICATE_REPORT_CODEC_REF,
    OPAQUE_CODEC_REF,
    STAGE5_PAYLOAD_CONTENT_VERSION,
    PayloadFormatEntry,
)

ROUTE = RouteAddresses(
    original_source=OriginalSource(StationId(111111111)),
    original_destination=OriginalDestination(Addressed(StationId(333333333))),
)
SOURCE = SourceReference(document="stage5 test", edition="1", location="test")


def _registry(scope: RegistryScope):
    registry = build_payload_format_registry(scope, STAGE5_PAYLOAD_CONTENT_VERSION).value
    return registry


def _adapter(scope: RegistryScope):
    registry = _registry(scope)
    registry.freeze()
    built = build_presentation_adapter(registry)
    if built.is_err:
        raise AssertionError(built.error)
    return built.value


class Stage5RegistryLifecycleTest(unittest.TestCase):
    def test_TC_STAGE5_008_catalogue_and_content_version(self):
        operational = _registry(RegistryScope.OPERATIONAL)
        self.assertEqual(2, operational.entry_count())
        test = _registry(RegistryScope.TEST)
        self.assertEqual(3, test.entry_count())
        self.assertEqual("stage5-r1027-ed1.1-v1", STAGE5_PAYLOAD_CONTENT_VERSION)
        fingerprint = test.fingerprint()
        self.assertEqual(STAGE5_PAYLOAD_CONTENT_VERSION, fingerprint.content_version)
        # scope가 fingerprint에 포함되므로 두 scope의 지문은 같다고 가정하지 않는다.
        self.assertNotEqual(
            operational.fingerprint().fingerprint, test.fingerprint().fingerprint
        )

    def test_TC_STAGE5_008_configured_wiring_uses_approved_content_version(self):
        # 실제 설정 경로(build_registries)도 §7.1의 고정 content version을 사용한다.
        from tests.support.fixtures import load_fake_configuration
        from vdeter.runtime.wiring import build_registries

        registries = build_registries(load_fake_configuration())
        self.assertTrue(registries.is_ok, getattr(registries, "error", None))
        fingerprint = registries.value.payload_format.fingerprint()
        self.assertEqual(STAGE5_PAYLOAD_CONTENT_VERSION, fingerprint.content_version)

    def test_TC_STAGE5_008_registry_freezes_before_use_and_rejects_changes(self):
        registry = _registry(RegistryScope.TEST)
        blocked_before = build_presentation_adapter(registry)
        self.assertTrue(blocked_before.is_err)
        self.assertEqual(ErrorCode.REGISTRY_NOT_FROZEN, blocked_before.error.code)
        registry.freeze()
        extra = PayloadFormatEntry(
            payload_format=PayloadFormat("test.extra"),
            vpfi=Vpfi(9),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=SOURCE,
            message_format=1,
            codec_ref=OPAQUE_CODEC_REF,
        )
        blocked = registry.register(extra)
        self.assertTrue(blocked.is_err)
        self.assertEqual(ErrorCode.REGISTRY_FROZEN, blocked.error.code)

    def test_TC_STAGE5_008_scope_mixing_is_rejected(self):
        operational = _registry(RegistryScope.OPERATIONAL)
        test_entry = PayloadFormatEntry(
            payload_format=PayloadFormat("test.wrong.scope"),
            vpfi=Vpfi(11),
            scope=RegistryScope.TEST,
            support_status=SupportStatus.SUPPORTED,
            source=SOURCE,
            codec_ref=OPAQUE_CODEC_REF,
        )
        result = operational.register(test_entry)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.REGISTRY_SCOPE_MISMATCH, result.error.code)

    def test_TC_STAGE5_008_unknown_codec_ref_rejects_adapter_build(self):
        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("test.unknown.codec"),
                vpfi=Vpfi(12),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=1,
                codec_ref="not.an.approved.codec",
            )
        )
        registry.freeze()
        built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.UNSUPPORTED_REQUEST, built.error.code)

    def test_TC_STAGE5_008_wire_metadata_mismatch_rejects_adapter_build(self):
        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("test.metadata.mismatch"),
                vpfi=Vpfi(15),  # AIS codec의 VPFI 7과 다르다.
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=1,
                codec_ref=AIS_SCHEME1_CODEC_REF,
            )
        )
        registry.freeze()
        built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, built.error.code)

    def test_TC_STAGE5_008_prefix_collision_rejects_adapter_build(self):
        # 같은 codec_ref(같은 prefix pattern)를 다른 key로 중복 등록하면 pairwise
        # disjointness 검사가 start를 거부한다 (`stage5` §6.1.1-6).
        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("x509.duplicate.prefix"),
                vpfi=Vpfi(0),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=1,
                codec_ref=CERTIFICATE_REPORT_CODEC_REF,
            )
        )
        registry.freeze()
        built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_DISCRIMINATOR_CONFLICT, built.error.code)

    def test_TC_STAGE5_008_discriminator_conflict_rules(self):
        base = WireDiscriminator(prefix=b"\x00\x07", third_byte_mask=0xFC, third_byte_value=0x04)
        other_format = WireDiscriminator(
            prefix=b"\x00\x07", third_byte_mask=0xFC, third_byte_value=0x08
        )
        x509 = WireDiscriminator(prefix=b"\x00\x01")
        opaque = WireDiscriminator(prefix=b"\x0f\xff\xff\xff")
        self.assertTrue(base.conflicts_with(base))
        self.assertFalse(base.conflicts_with(other_format))
        self.assertFalse(base.conflicts_with(x509))
        self.assertFalse(base.conflicts_with(opaque))
        self.assertFalse(x509.conflicts_with(opaque))


class Stage5PrefixFirstDispatchTest(unittest.TestCase):
    def setUp(self):
        self.test_adapter = _adapter(RegistryScope.TEST)
        self.operational_adapter = _adapter(RegistryScope.OPERATIONAL)

    def _decode(self, adapter, data: bytes):
        return adapter.decode_payload(PresentationDecodeRequest(route=ROUTE, data=data))

    def test_TC_STAGE5_008_minimum_two_bytes_required(self):
        for data in (b"", b"\x00"):
            with self.subTest(size=len(data)):
                result = self._decode(self.test_adapter, data)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.PRESENTATION_TRUNCATED, result.error.code)

    def test_TC_STAGE5_008_opaque_prefix_boundaries(self):
        # 2~3 bytes의 정확한 opaque prefix 조각은 truncated PROTOCOL이다.
        for data in (b"\x0f\xff", b"\x0f\xff\xff"):
            with self.subTest(size=len(data)):
                result = self._decode(self.test_adapter, data)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.PRESENTATION_TRUNCATED, result.error.code)
        # 4-byte prefix만 있고 application이 없으면 codec의 최소 길이 5 검사가 거부한다.
        exact_prefix = b"\x0f\xff\xff\xff"
        result = self._decode(self.test_adapter, exact_prefix)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_TRUNCATED, result.error.code)
        # 5 bytes부터 application 복원.
        result = self._decode(self.test_adapter, exact_prefix + b"\x7f")
        self.assertTrue(result.is_ok)
        self.assertEqual(b"\x7f", result.value.application_payload)

    def test_TC_STAGE5_008_x509_prefix_dispatches_to_x509_only(self):
        # `00 01` prefix면 AIS parser를 시도하지 않는다. 구조 오류는 x509 codec의
        # 오류이며 unknown wire key가 아니다 (trial decode 금지).
        result = self._decode(self.test_adapter, b"\x00\x01" + b"\xff" * 20)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_STRUCTURE_INVALID, result.error.code)

    def test_TC_STAGE5_008_vpfi7_message_format_discrimination(self):
        # `00 07` 뒤 3 bytes 미만은 truncated.
        result = self._decode(self.test_adapter, b"\x00\x07")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_TRUNCATED, result.error.code)
        # Message format이 1이 아니면 unknown wire key.
        result = self._decode(self.test_adapter, b"\x00\x07" + bytes(((2 << 2),)) + b"\x00" * 81)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code)
        # Message format 1이면 AIS codec 하나로 dispatch하고 전체 84 bytes를 요구한다.
        result = self._decode(self.test_adapter, b"\x00\x07\x04\x00")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_STRUCTURE_INVALID, result.error.code)

    def test_TC_STAGE5_008_unknown_wire_key_is_unsupported(self):
        result = self._decode(self.test_adapter, b"\x12\x34\x56")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code)

    def test_TC_STAGE5_008_operational_scope_ignores_opaque_branch(self):
        result = self._decode(self.operational_adapter, b"\x0f\xff\xff\xff\x01")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code)
        # 운영 scope에서 2~3-byte opaque prefix 조각도 opaque branch가 아니라
        # unknown wire key다.
        result = self._decode(self.operational_adapter, b"\x0f\xff")
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code)


if __name__ == "__main__":
    unittest.main()
