"""TC-STAGE5-008: Payload Format registry lifecycle/scope와 prefix-first RX dispatch.

요구사항: `STAGE5-REQ-08` (`stage5_service_integration_baseline.md` §6.1.1, §7),
`PAYLOAD-REQ-02/03/08` (`payload_extension_baseline.md` §7, §8, §13.1
`UNKNOWN-01`, `DISC-01`, `REG-01`)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.presentation import PresentationDecodeRequest
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import PayloadFormat, StationId, Vpfi
from vdeter.presentation.adapter import _CODEC_DECLARATIONS, build_presentation_adapter
from vdeter.presentation.codecs.base import WireDiscriminator
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope, SourceReference, SupportStatus
from vdeter.profiles.payload_formats import (
    AIS_SCHEME1_CODEC_REF,
    CERTIFICATE_REPORT_CODEC_REF,
    OPAQUE_CODEC_REF,
    SC_VDE_ACK_CODEC_REF,
    SC_VDE_ACK_MESSAGE_FORMAT,
    SC_VDE_COMMAND_CODEC_REF,
    SC_VDE_COMMAND_MESSAGE_FORMAT,
    SC_VDE_LOGICAL_VPFI,
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
        # Payload 확장 (`payload_extension_baseline.md` §7.3): OPERATIONAL entry set은
        # 기존 두 entry로 불변이고, TEST는 기존 3개 + SC-VDE 2개로 정확히 5개다.
        operational = _registry(RegistryScope.OPERATIONAL)
        self.assertEqual(2, operational.entry_count())
        test = _registry(RegistryScope.TEST)
        self.assertEqual(5, test.entry_count())
        self.assertEqual(
            "payload-extension-project-sc-vde-route-control-v1",
            STAGE5_PAYLOAD_CONTENT_VERSION,
        )
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
        # 같은 codec_ref(같은 prefix pattern)를 다른 key로 중복 등록하는 alias 구성은
        # start 실패다. Payload 확장(`payload_extension_baseline.md` §7.1, §8.1)부터는
        # pairwise disjointness보다 먼저 codec_ref 재사용 preflight가 이 구성을
        # 거부한다 (같은 bytes가 둘 이상의 후보가 되는 상황의 조기 차단).
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
        self.assertEqual(ErrorCode.INVALID_REQUEST, built.error.code)
        self.assertIn("codec_ref", built.error.message)

    def test_TC_STAGE5_008_true_discriminator_intersection_rejects_adapter_build(self):
        # 서로 다른 codec_ref·wire key이면서 accepted byte language가 겹치는 pair는
        # pairwise disjointness 검사가 거부한다 (`stage5` §6.1.1-6,
        # `payload_extension_baseline.md` §8.1 검사 4).
        class _ConflictingCodec:
            codec_ref = "project.test.conflicting.prefix.v1"
            vpfi = 88
            message_format = 1
            discriminator = WireDiscriminator(prefix=b"\x00\x01")  # x509와 동일 prefix
            min_wire_length = 2
            max_application_bytes = 8
            max_wire_bytes = 10

            def encoded_size_bound(self, application_length):
                raise AssertionError("build 검증만 대상이다")

        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("test.conflicting.prefix"),
                vpfi=Vpfi(88),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=1,
                codec_ref=_ConflictingCodec.codec_ref,
            )
        )
        registry.freeze()
        extended = _CODEC_DECLARATIONS + (
            (_ConflictingCodec.codec_ref, _ConflictingCodec()),
        )
        with patch("vdeter.presentation.adapter._CODEC_DECLARATIONS", extended):
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

    def test_TC_STAGE5_008_sc_vde_discriminators_are_pairwise_disjoint(self):
        # `payload_extension_baseline.md` §8.2: 신규 두 pattern은 서로와 기존 세
        # pattern 모두에 대해 pairwise disjoint여야 한다.
        command = WireDiscriminator(prefix=b"\xff\x7f\x02")
        ack = WireDiscriminator(prefix=b"\xff\x7f\x03")
        existing = (
            WireDiscriminator(prefix=b"\x00\x01"),
            WireDiscriminator(prefix=b"\x00\x07", third_byte_mask=0xFC, third_byte_value=0x04),
            WireDiscriminator(prefix=b"\x0f\xff\xff\xff"),
        )
        self.assertFalse(command.conflicts_with(ack))
        self.assertFalse(ack.conflicts_with(command))
        for pattern in existing:
            self.assertFalse(command.conflicts_with(pattern))
            self.assertFalse(ack.conflicts_with(pattern))
        # 같은 prefix끼리는 충돌로 판정된다 (교집합 판정 자체의 sanity).
        self.assertTrue(command.conflicts_with(WireDiscriminator(prefix=b"\xff\x7f\x02")))
        # 한쪽 prefix가 다른 쪽의 진부분 prefix면 mask 구분이 없는 한 충돌이다.
        self.assertTrue(command.conflicts_with(WireDiscriminator(prefix=b"\xff\x7f")))

    def test_TC_STAGE5_008_duplicate_entry_codec_ref_rejects_adapter_build(self):
        # `payload_extension_baseline.md` §7.1: codec_ref 재사용(alias) 금지.
        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("test.duplicate.codec.ref"),
                vpfi=Vpfi(99),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=SC_VDE_COMMAND_MESSAGE_FORMAT,
                codec_ref=SC_VDE_COMMAND_CODEC_REF,
            )
        )
        registry.freeze()
        built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, built.error.code)
        self.assertIn("codec_ref", built.error.message)

    def test_TC_STAGE5_008_duplicate_wire_key_rejects_adapter_build(self):
        # `payload_extension_baseline.md` §8.1: wire key 중복은 discriminator와 별개의
        # 시작 실패다.
        registry = build_payload_format_registry(
            RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
        ).value
        registry.register(
            PayloadFormatEntry(
                payload_format=PayloadFormat("test.duplicate.wire.key"),
                vpfi=Vpfi(SC_VDE_LOGICAL_VPFI),
                scope=RegistryScope.TEST,
                support_status=SupportStatus.SUPPORTED,
                source=SOURCE,
                message_format=SC_VDE_ACK_MESSAGE_FORMAT,
                codec_ref="project.test.duplicate.wire.v1",
            )
        )
        registry.freeze()
        built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, built.error.code)
        self.assertIn("wire key", built.error.message)

    def test_TC_STAGE5_008_duplicate_codec_declaration_rejects_adapter_build(self):
        # `payload_extension_baseline.md` §8.1: 선언 sequence의 duplicate ref는 dict
        # materialize 전에 거부해야 한다 (overwrite 은닉 금지).
        registry = _registry(RegistryScope.TEST)
        registry.freeze()
        duplicated = _CODEC_DECLARATIONS + (_CODEC_DECLARATIONS[-1],)
        with patch("vdeter.presentation.adapter._CODEC_DECLARATIONS", duplicated):
            built = build_presentation_adapter(registry)
        self.assertTrue(built.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, built.error.code)
        self.assertIn("선언", built.error.message)

    def test_TC_STAGE5_008_codec_declarations_cover_all_five_refs_uniquely(self):
        refs = [codec_ref for codec_ref, _codec in _CODEC_DECLARATIONS]
        self.assertEqual(sorted(set(refs)), sorted(refs))
        self.assertEqual(
            {
                OPAQUE_CODEC_REF,
                CERTIFICATE_REPORT_CODEC_REF,
                AIS_SCHEME1_CODEC_REF,
                SC_VDE_COMMAND_CODEC_REF,
                SC_VDE_ACK_CODEC_REF,
            },
            set(refs),
        )
        for codec_ref, codec in _CODEC_DECLARATIONS:
            self.assertEqual(codec_ref, codec.codec_ref)


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

    #: `payload_extension_baseline.md` §6.4.6 승인 vector (TEST wire).
    SC_VDE_COMMAND_WIRE = bytes.fromhex(
        "ff7f0201c84018f3ffff39d2249d7cf0282b3400f078a4ef910141a6f8078284"
    )
    SC_VDE_ACK_WIRE = bytes.fromhex("ff7f0301d000")

    def test_TC_STAGE5_008_sc_vde_prefixes_dispatch_to_each_codec_only(self):
        # `DISC-01`: exact `ff 7f 02`/`ff 7f 03`이 각각 정확히 하나의 entry로 결정된다.
        command = self._decode(self.test_adapter, self.SC_VDE_COMMAND_WIRE)
        self.assertTrue(command.is_ok, getattr(command, "error", None))
        self.assertEqual(
            "sc_vde.route_control.command", command.value.payload_format.value
        )
        self.assertEqual(self.SC_VDE_COMMAND_WIRE[3:], command.value.application_payload)
        ack = self._decode(self.test_adapter, self.SC_VDE_ACK_WIRE)
        self.assertTrue(ack.is_ok, getattr(ack, "error", None))
        self.assertEqual("sc_vde.route_control.ack", ack.value.payload_format.value)
        self.assertEqual(self.SC_VDE_ACK_WIRE[3:], ack.value.application_payload)

    def test_TC_STAGE5_008_sc_vde_truncated_prefix_is_truncated(self):
        # `UNKNOWN-01`/`TRUNC-01`: `ff`(2-byte 최소 미달)와 partial `ff 7f`는 truncated다.
        for data in (b"\xff", b"\xff\x7f"):
            with self.subTest(data=data.hex()):
                result = self._decode(self.test_adapter, data)
                self.assertTrue(result.is_err)
                self.assertEqual(ErrorCode.PRESENTATION_TRUNCATED, result.error.code)

    def test_TC_STAGE5_008_sc_vde_unknown_message_formats_are_rejected(self):
        # known `ff 7f` 뒤 unknown MF 00/04는 unknown wire key다 (trial decode 금지).
        body = self.SC_VDE_COMMAND_WIRE[3:]
        for message_format in (0x00, 0x04):
            with self.subTest(message_format=message_format):
                result = self._decode(
                    self.test_adapter, b"\xff\x7f" + bytes((message_format,)) + body
                )
                self.assertTrue(result.is_err)
                self.assertEqual(
                    ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code
                )

    def test_TC_STAGE5_008_sc_vde_neighbour_source_and_logical_wires_are_rejected(self):
        # reserved-neighbour `ff 7e`, source 임시 `ff 01`, logical 16383의 정수 직렬화
        # `3f ff`는 모두 supported alias/fallback이 아니다 (`DR-PAYLOAD-003`).
        body = self.SC_VDE_COMMAND_WIRE[3:]
        cases = (
            b"\xff\x7e\x02" + body,
            b"\xff\x01\x02" + body,
            b"\xff\x01\x03" + self.SC_VDE_ACK_WIRE[3:],
            b"\x3f\xff\x02" + body,
            b"\x3f\xff\x03" + self.SC_VDE_ACK_WIRE[3:],
        )
        for data in cases:
            with self.subTest(prefix=data[:3].hex()):
                result = self._decode(self.test_adapter, data)
                self.assertTrue(result.is_err)
                self.assertEqual(
                    ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code
                )

    def test_TC_STAGE5_008_operational_scope_rejects_known_sc_vde_wire(self):
        # `REG-01`/`AUTH-SCOPE-01`: TEST 전용 entry는 OPERATIONAL scope에서 unknown이다.
        for data in (self.SC_VDE_COMMAND_WIRE, self.SC_VDE_ACK_WIRE, b"\xff\x7f"):
            with self.subTest(data=data[:3].hex()):
                result = self._decode(self.operational_adapter, data)
                self.assertTrue(result.is_err)
                self.assertEqual(
                    ErrorCode.PRESENTATION_UNKNOWN_WIRE_KEY, result.error.code
                )


if __name__ == "__main__":
    unittest.main()
