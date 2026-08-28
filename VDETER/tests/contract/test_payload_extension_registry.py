"""TC-PAYLOAD-002: Payload 확장 registry 계약·fingerprint·TEST scope 격리 audit.

요구사항: `PAYLOAD-REQ-08`, `PAYLOAD-REQ-09`, `PAYLOAD-REQ-11`
(`payload_extension_baseline.md` §7, §13.1 `REG-01`, `AUTH-SCOPE-01`, `AUTH-GOV-01`,
§13.2)

승인 기대 fingerprint는 §7.3의 독립 계산값이다. 구현 결과가 다르면 기대값을 코드
결과로 바꾸지 말고 canonical content 차이를 찾아야 한다.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from vdeter.common.addressing import Broadcast
from vdeter.common.contracts.service import SubmitTransferRequest
from vdeter.common.exceptions import InvalidValueError
from vdeter.common.identifiers import PayloadFormat, Vpfi
from vdeter.presentation.adapter import build_presentation_adapter
from vdeter.presentation.registry import (
    PAYLOAD_FORMAT_REGISTRY_NAME,
    build_payload_format_registry,
)
from vdeter.profiles.models import ExplicitRegistry, RegistryScope, SupportStatus
from vdeter.profiles.payload_formats import (
    SC_VDE_ACK_CODEC_REF,
    SC_VDE_ACK_MESSAGE_FORMAT,
    SC_VDE_ACK_PAYLOAD_FORMAT,
    SC_VDE_COMMAND_CODEC_REF,
    SC_VDE_COMMAND_MESSAGE_FORMAT,
    SC_VDE_COMMAND_PAYLOAD_FORMAT,
    SC_VDE_LOGICAL_VPFI,
    SC_VDE_POSITION_REPORT_CODEC_REF,
    SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,
    SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT,
    STAGE5_PAYLOAD_CONTENT_VERSION,
    PayloadFormatEntry,
    operational_entries,
    test_entries,
)

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

#: `payload_extension_baseline.md` §7.3 선택 A(`DR-PAYLOAD-017`)의 승인 기대값
#: (독립 계산 고정).
APPROVED_CONTENT_VERSION = "payload-extension-project-sc-vde-family-v2"
APPROVED_FINGERPRINTS = {
    RegistryScope.OPERATIONAL: (
        2,
        "6c9cf142579705d39bc03af1ea3a16d492660c3f67c198034b70d4f14229b528",
    ),
    RegistryScope.TEST: (
        6,
        "f648e89c0bbb3b6923f0fac11ab611e33d595a6a1ffdd5f82eb1cf2e68740f02",
    ),
}

#: §7.1의 SC-VDE SourceReference exact 값. fingerprint 입력이므로 문구를 바꾸면
#: 승인 기대 fingerprint가 깨진다. MF 1 위치보고는 MF 2/3과 다른 SourceReference를
#: 사용한다.
APPROVED_SC_VDE_SOURCE_DOCUMENT = "VDETER Project SC-VDE Route Control Profile"
APPROVED_SC_VDE_SOURCE_EDITION = "v1"
APPROVED_SC_VDE_SOURCE_LOCATION = (
    "payload_extension_baseline §4.4, §6.4; carrier IALA R1027 Ed.1.1 Table 3; "
    "schema SC-VDE-005 sha256:"
    "c55f908c869880c60cf7a84b8c00c1d778a7f0c0a30cd310e908374e5ad99fcd"
    " and SC-VDE-006 sha256:"
    "c157fcea6193209811b54621faa3aee1a9201a414b53ff9d4af4f0f9df84cf95"
    "; unsigned TEST-only, no real-vessel control"
)
APPROVED_POSREP_SOURCE_DOCUMENT = "VDETER Project SC-VDE Position Report Profile"
APPROVED_POSREP_SOURCE_EDITION = "v1"
APPROVED_POSREP_SOURCE_LOCATION = (
    "payload_extension_baseline §4.4, §6.5; carrier IALA R1027 Ed.1.1 Table 3; "
    "AIS-derived fields ITU-R M.1371-6 Table 46; schema position-report source "
    "sha256:7e826a688fafb694d4e7a4985cf68ebf09df1e38863fd1c46f1b5de832aa6f53; "
    "Autonomy Mode project-defined; unsigned TEST-only, no real-vessel control"
)


def _built_registry(scope: RegistryScope):
    registry = build_payload_format_registry(scope, STAGE5_PAYLOAD_CONTENT_VERSION).value
    registry.freeze()
    return registry


class PayloadExtensionFingerprintTest(unittest.TestCase):
    """`REG-01`: §7.3 승인 fingerprint exact 일치와 재현성."""

    def test_TC_PAYLOAD_002_content_version_is_the_approved_shared_value(self):
        self.assertEqual(APPROVED_CONTENT_VERSION, STAGE5_PAYLOAD_CONTENT_VERSION)

    def test_TC_PAYLOAD_002_both_scope_fingerprints_match_approved_values(self):
        for scope, (expected_count, expected_digest) in APPROVED_FINGERPRINTS.items():
            with self.subTest(scope=scope.value):
                fingerprint = _built_registry(scope).fingerprint()
                self.assertEqual(expected_count, fingerprint.entry_count)
                self.assertEqual(APPROVED_CONTENT_VERSION, fingerprint.content_version)
                self.assertEqual(expected_digest, fingerprint.fingerprint)

    def test_TC_PAYLOAD_002_registration_order_does_not_change_fingerprint(self):
        def build(entries):
            registry = ExplicitRegistry(
                PAYLOAD_FORMAT_REGISTRY_NAME,
                RegistryScope.TEST,
                STAGE5_PAYLOAD_CONTENT_VERSION,
                PayloadFormatEntry,
            )
            registered = registry.register_all(entries)
            if registered.is_err:
                raise AssertionError(registered.error)
            return registry

        forward = build(test_entries())
        reverse = build(tuple(reversed(test_entries())))
        self.assertEqual(
            forward.fingerprint().fingerprint, reverse.fingerprint().fingerprint
        )
        self.assertEqual(
            APPROVED_FINGERPRINTS[RegistryScope.TEST][1],
            forward.fingerprint().fingerprint,
        )

    def test_TC_PAYLOAD_002_rebuild_reproduces_the_same_fingerprint(self):
        # 재시작(재조립) 시에도 같은 입력이면 같은 지문이어야 한다 (§7.3).
        first = _built_registry(RegistryScope.TEST).fingerprint().fingerprint
        second = _built_registry(RegistryScope.TEST).fingerprint().fingerprint
        self.assertEqual(first, second)

    def test_TC_PAYLOAD_002_adapter_snapshot_carries_the_approved_fingerprint(self):
        for scope, (_count, expected_digest) in APPROVED_FINGERPRINTS.items():
            with self.subTest(scope=scope.value):
                built = build_presentation_adapter(_built_registry(scope))
                self.assertTrue(built.is_ok, getattr(built, "error", None))
                self.assertEqual(expected_digest, built.value.registry_fingerprint())


class PayloadExtensionScopeIsolationTest(unittest.TestCase):
    """`AUTH-SCOPE-01`: TEST-only 격리와 OPERATIONAL entry set 불변."""

    def test_TC_PAYLOAD_002_operational_catalogue_is_unchanged(self):
        entries = {entry.payload_format.value: entry for entry in operational_entries()}
        self.assertEqual(
            {"ais.authentication_integrity.scheme1", "x509.certificate_report"},
            set(entries),
        )
        self.assertEqual(0, entries["x509.certificate_report"].vpfi.value)
        self.assertEqual(1, entries["x509.certificate_report"].message_format)
        self.assertEqual(7, entries["ais.authentication_integrity.scheme1"].vpfi.value)
        self.assertEqual(1, entries["ais.authentication_integrity.scheme1"].message_format)

    def test_TC_PAYLOAD_002_test_catalogue_adds_exactly_the_three_sc_vde_entries(self):
        test_keys = {entry.payload_format.value for entry in test_entries()}
        operational_keys = {
            entry.payload_format.value for entry in operational_entries()
        }
        self.assertEqual(
            {
                SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT,
                SC_VDE_COMMAND_PAYLOAD_FORMAT,
                SC_VDE_ACK_PAYLOAD_FORMAT,
                "test.stage0.opaque",
            },
            test_keys - operational_keys,
        )

    def test_TC_PAYLOAD_002_sc_vde_entry_metadata_matches_section_7_1(self):
        by_key = {entry.payload_format.value: entry for entry in test_entries()}
        position_report = by_key[SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT]
        command = by_key[SC_VDE_COMMAND_PAYLOAD_FORMAT]
        ack = by_key[SC_VDE_ACK_PAYLOAD_FORMAT]
        for entry, message_format, codec_ref in (
            (
                position_report,
                SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,
                SC_VDE_POSITION_REPORT_CODEC_REF,
            ),
            (command, SC_VDE_COMMAND_MESSAGE_FORMAT, SC_VDE_COMMAND_CODEC_REF),
            (ack, SC_VDE_ACK_MESSAGE_FORMAT, SC_VDE_ACK_CODEC_REF),
        ):
            with self.subTest(payload_format=entry.payload_format.value):
                self.assertEqual(SC_VDE_LOGICAL_VPFI, entry.vpfi.value)
                self.assertEqual(message_format, entry.message_format)
                self.assertEqual(codec_ref, entry.codec_ref)
                self.assertIs(RegistryScope.TEST, entry.scope)
                self.assertIs(SupportStatus.SUPPORTED, entry.support_status)
        self.assertEqual(
            3, len({position_report.codec_ref, command.codec_ref, ack.codec_ref})
        )
        self.assertEqual((SC_VDE_LOGICAL_VPFI, 1), position_report.wire_key())
        self.assertEqual((SC_VDE_LOGICAL_VPFI, 2), command.wire_key())
        self.assertEqual((SC_VDE_LOGICAL_VPFI, 3), ack.wire_key())

    def test_TC_PAYLOAD_002_uniqueness_of_key_ref_and_wire_key_in_test_catalogue(self):
        entries = test_entries()
        keys = [entry.payload_format.value for entry in entries]
        refs = [entry.codec_ref for entry in entries]
        wire_keys = [entry.wire_key() for entry in entries]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(len(wire_keys), len(set(wire_keys)))

    def test_TC_PAYLOAD_002_operational_registry_rejects_sc_vde_lookups(self):
        operational = _built_registry(RegistryScope.OPERATIONAL)
        for key in (
            SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT,
            SC_VDE_COMMAND_PAYLOAD_FORMAT,
            SC_VDE_ACK_PAYLOAD_FORMAT,
        ):
            with self.subTest(key=key):
                self.assertTrue(operational.lookup(PayloadFormat(key)).is_err)
        for message_format in (
            SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,
            SC_VDE_COMMAND_MESSAGE_FORMAT,
            SC_VDE_ACK_MESSAGE_FORMAT,
        ):
            with self.subTest(message_format=message_format):
                self.assertTrue(
                    operational.lookup_wire(
                        Vpfi(SC_VDE_LOGICAL_VPFI), message_format
                    ).is_err
                )

    def test_TC_PAYLOAD_002_test_registry_resolves_sc_vde_lookups(self):
        test_registry = _built_registry(RegistryScope.TEST)
        for key, message_format in (
            (
                SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT,
                SC_VDE_POSITION_REPORT_MESSAGE_FORMAT,
            ),
            (SC_VDE_COMMAND_PAYLOAD_FORMAT, SC_VDE_COMMAND_MESSAGE_FORMAT),
            (SC_VDE_ACK_PAYLOAD_FORMAT, SC_VDE_ACK_MESSAGE_FORMAT),
        ):
            with self.subTest(key=key):
                by_key = test_registry.lookup(PayloadFormat(key))
                self.assertTrue(by_key.is_ok, getattr(by_key, "error", None))
                by_wire = test_registry.lookup_wire(
                    Vpfi(SC_VDE_LOGICAL_VPFI), message_format
                )
                self.assertTrue(by_wire.is_ok)
                self.assertEqual(by_key.value, by_wire.value)

    def test_TC_PAYLOAD_002_public_broadcast_with_ack_is_rejected_at_dto_creation(self):
        # §6.5.13: 공개 `SubmitTransferRequest`는 broadcast+ACK 조합을 DTO 생성
        # 시점에 `InvalidValueError`로 거부한다. 이 조합은 Service나 adapter에
        # 도달하지 않으므로 Service E2E에서 `UNSUPPORTED_REQUEST`를 기대하지 않는다.
        with self.assertRaises(InvalidValueError):
            SubmitTransferRequest(
                destination=Broadcast(),
                payload_format=PayloadFormat(SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT),
                payload=b"\x00" * 14,
                ack_required=True,
            )


class PayloadExtensionAuthorityAuditTest(unittest.TestCase):
    """`AUTH-SCOPE-01`/`AUTH-GOV-01`: 비인증 TEST profile 표기와 격리 근거 audit.

    codec은 §6.4.8의 구조 정규성만 강제한다. 다음 governance 항목은 소프트웨어로
    강제할 수 없으므로 수동 판정으로 명시한다 (`payload_extension_baseline.md` §4.4).

    - 격리된 유선 시험 또는 시뮬레이션 배치에서만 사용 (실제 RF 운영망 금지)
    - 실제 actuator·항해/조종 장비와 물리적 분리
    - 비인증(무서명 Segment Type 0) 위험 수용은 사용자 승인 기록으로 유지
    """

    def _sc_vde_entries(self):
        return tuple(
            entry
            for entry in test_entries()
            if entry.payload_format.value.startswith("sc_vde.")
        )

    def test_TC_PAYLOAD_002_source_reference_matches_approved_exact_values(self):
        entries = {
            entry.payload_format.value: entry for entry in self._sc_vde_entries()
        }
        self.assertEqual(3, len(entries))
        position_report = entries[SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT]
        self.assertEqual(
            APPROVED_POSREP_SOURCE_DOCUMENT, position_report.source.document
        )
        self.assertEqual(APPROVED_POSREP_SOURCE_EDITION, position_report.source.edition)
        self.assertEqual(
            APPROVED_POSREP_SOURCE_LOCATION, position_report.source.location
        )
        for key in (SC_VDE_COMMAND_PAYLOAD_FORMAT, SC_VDE_ACK_PAYLOAD_FORMAT):
            entry = entries[key]
            with self.subTest(payload_format=key):
                self.assertEqual(
                    APPROVED_SC_VDE_SOURCE_DOCUMENT, entry.source.document
                )
                self.assertEqual(APPROVED_SC_VDE_SOURCE_EDITION, entry.source.edition)
                self.assertEqual(
                    APPROVED_SC_VDE_SOURCE_LOCATION, entry.source.location
                )

    def test_TC_PAYLOAD_002_source_reference_declares_unsigned_test_only_limits(self):
        schema_hashes = {
            SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT: (
                "7e826a688fafb694d4e7a4985cf68ebf09df1e38863fd1c46f1b5de832aa6f53",
            ),
            SC_VDE_COMMAND_PAYLOAD_FORMAT: (
                "c55f908c869880c60cf7a84b8c00c1d778a7f0c0a30cd310e908374e5ad99fcd",
                "c157fcea6193209811b54621faa3aee1a9201a414b53ff9d4af4f0f9df84cf95",
            ),
            SC_VDE_ACK_PAYLOAD_FORMAT: (
                "c55f908c869880c60cf7a84b8c00c1d778a7f0c0a30cd310e908374e5ad99fcd",
                "c157fcea6193209811b54621faa3aee1a9201a414b53ff9d4af4f0f9df84cf95",
            ),
        }
        for entry in self._sc_vde_entries():
            location = entry.source.location
            with self.subTest(payload_format=entry.payload_format.value):
                # 비공식 project profile임과 입력 자료의 hash 추적성.
                self.assertIn("payload_extension_baseline", location)
                for schema_hash in schema_hashes[entry.payload_format.value]:
                    self.assertIn(schema_hash, location)
                # 비인증 TEST 격리와 실제 선박 제어 금지 표기.
                self.assertIn("unsigned TEST-only, no real-vessel control", location)
                # R1027/M.1371 준수 주장으로 읽힐 표기를 두지 않는다: 공식 문서명은
                # carrier/차용 근거로만 남고 document field는 project profile이다.
                self.assertNotEqual("IALA R1027", entry.source.document)
                self.assertNotIn("ITU-R M.1371", entry.source.document)

    def test_TC_PAYLOAD_002_position_report_source_marks_project_autonomy_field(self):
        # §6.5.2/§7.1: MF 1은 AIS-derived profile이며 Autonomy Mode가 프로젝트 전용
        # 확장임을 SourceReference가 명시한다.
        by_key = {entry.payload_format.value: entry for entry in self._sc_vde_entries()}
        location = by_key[SC_VDE_POSITION_REPORT_PAYLOAD_FORMAT].source.location
        self.assertIn("AIS-derived fields ITU-R M.1371-6 Table 46", location)
        self.assertIn("Autonomy Mode project-defined", location)

    def test_TC_PAYLOAD_002_vector_manifest_records_test_scope_and_provenance(self):
        manifest = json.loads(
            (VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        for vector_id in (
            "GV-PAYLOAD-SCVDE-POSREP-001",
            "GV-PAYLOAD-SCVDE-REMOTE-001",
            "GV-PAYLOAD-SCVDE-ROUTE-001",
            "GV-PAYLOAD-SCVDE-ACK-001",
        ):
            with self.subTest(vector=vector_id):
                entry = manifest["vectors"][vector_id]
                self.assertEqual("TEST", entry["scope"])
                self.assertIn("payload_extension_baseline.md", entry["source"])
                self.assertIn("독립", entry["note"])

    def test_TC_PAYLOAD_002_software_regression_uses_actuator_free_radio(self):
        # `AUTH-GOV-01` 자동화 가능 부분: Payload software 회귀가 사용하는 group은
        # in-process SoftwareRadioMedium이며 hardware/actuator 장치 경로가 아니다.
        # (격리 배치·물리 분리 자체는 class docstring의 수동 판정 항목이다.)
        from tests.support import stage5 as stage5_support
        from vdeter.radio.software import SoftwareRadioAdapter, SoftwareRadioMedium

        medium = stage5_support.build_stage5_medium()
        self.assertIsInstance(medium, SoftwareRadioMedium)
        self.assertTrue(issubclass(SoftwareRadioAdapter, object))
        source = Path(stage5_support.__file__).read_text(encoding="utf-8")
        self.assertIn("SoftwareRadioAdapter", source)
        self.assertNotIn("B210RadioAdapter", source)


if __name__ == "__main__":
    unittest.main()
