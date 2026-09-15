"""TC-STAGE5-011: opaque test 격리와 전자해도 미지원 gate.

요구사항: `STAGE5-REQ-11`, `STAGE5-REQ-18`
(`stage5_service_integration_baseline.md` §6.4, §6.5, §6.6, `DR-STAGE5-022`)
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.presentation import (
    PresentationDecodeRequest,
    PresentationEncodeRequest,
)
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.presentation.adapter import build_presentation_adapter as _build_presentation_adapter
from vdeter.security.dependencies import E2eSecurityDependencies


def build_presentation_adapter(registry):
    return _build_presentation_adapter(registry, e2e_security=E2eSecurityDependencies.disabled())
from vdeter.presentation.codecs.opaque import OPAQUE_WIRE_PREFIX, OpaqueTestCodec
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope
from vdeter.profiles.payload_formats import (
    ELECTRONIC_CHART_RESERVED_KEY,
    STAGE5_PAYLOAD_CONTENT_VERSION,
    entries_for,
)

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

OPAQUE_APP_SHA256 = "f742b965f156c10374bc23aea96e3a8aff8facd6fc079defeaa30219ad86f211"
OPAQUE_WIRE_SHA256 = "890f598700c95a02a21c6dab71240a92682a8cb5abcce45a40852eb28f656248"

ROUTE = RouteAddresses(
    original_source=OriginalSource(StationId(111111111)),
    original_destination=OriginalDestination(Addressed(StationId(333333333))),
)
TRANSFER_ID = TransferId("2f9f1f5e-8e13-4d7c-9f6c-6f2b6f9f1a2b")


def _adapter(scope: RegistryScope):
    registry = build_payload_format_registry(scope, STAGE5_PAYLOAD_CONTENT_VERSION).value
    registry.freeze()
    adapter = build_presentation_adapter(registry)
    if adapter.is_err:
        raise AssertionError(adapter.error)
    return adapter.value


class Stage5OpaqueGoldenVectorTest(unittest.TestCase):
    def setUp(self):
        self.codec = OpaqueTestCodec()
        self.application = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.application.bin").read_bytes()
        self.wire = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.wire.bin").read_bytes()

    def test_TC_STAGE5_011_artifacts_match_approved_sha256(self):
        self.assertEqual(b"\x00\xff\x80", self.application)
        self.assertEqual(b"\x0f\xff\xff\xff\x00\xff\x80", self.wire)
        self.assertEqual(OPAQUE_APP_SHA256, hashlib.sha256(self.application).hexdigest())
        self.assertEqual(OPAQUE_WIRE_SHA256, hashlib.sha256(self.wire).hexdigest())
        manifest = json.loads((VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["vectors"]["GV-STAGE5-PRES-OPAQUE-001"]
        self.assertEqual(OPAQUE_WIRE_SHA256, entry["wire"]["sha256"])

    def test_TC_STAGE5_011_decode_and_encode_match_fixed_bytes(self):
        decoded = self.codec.decode(self.wire)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(self.application, decoded.value)
        encoded = self.codec.encode(self.application)
        self.assertTrue(encoded.is_ok)
        self.assertEqual(self.wire, encoded.value)

    def test_TC_STAGE5_011_opaque_round_trips_arbitrary_bytes(self):
        for payload in (b"\x00", b"\xff" * 33, bytes(range(256)), b"\xc3\x28\x00\xff"):
            with self.subTest(size=len(payload)):
                encoded = self.codec.encode(payload)
                self.assertTrue(encoded.is_ok)
                self.assertEqual(OPAQUE_WIRE_PREFIX + payload, encoded.value)
                decoded = self.codec.decode(encoded.value)
                self.assertEqual(payload, decoded.value)


class Stage5OpaqueScopeIsolationTest(unittest.TestCase):
    def test_TC_STAGE5_011_operational_registry_rejects_opaque_submit(self):
        adapter = _adapter(RegistryScope.OPERATIONAL)
        encoded = adapter.encode_payload(
            PresentationEncodeRequest(
                transfer_id=TRANSFER_ID,
                route=ROUTE,
                payload_format=PayloadFormat("test.stage0.opaque"),
                application_payload=b"\x01\x02",
            )
        )
        self.assertTrue(encoded.is_err)
        self.assertEqual("UNSUPPORTED_REQUEST", encoded.error.code)

    def test_TC_STAGE5_011_operational_registry_does_not_recognize_opaque_prefix(self):
        adapter = _adapter(RegistryScope.OPERATIONAL)
        wire = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.wire.bin").read_bytes()
        decoded = adapter.decode_payload(PresentationDecodeRequest(route=ROUTE, data=wire))
        self.assertTrue(decoded.is_err)
        self.assertEqual("PRESENTATION_UNKNOWN_WIRE_KEY", decoded.error.code)

    def test_TC_STAGE5_011_test_registry_round_trips_opaque(self):
        adapter = _adapter(RegistryScope.TEST)
        application = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.application.bin").read_bytes()
        wire = (VECTOR_ROOT / "gv_stage5_pres_opaque_001.wire.bin").read_bytes()
        encoded = adapter.encode_payload(
            PresentationEncodeRequest(
                transfer_id=TRANSFER_ID,
                route=ROUTE,
                payload_format=PayloadFormat("test.stage0.opaque"),
                application_payload=application,
            )
        )
        self.assertTrue(encoded.is_ok)
        self.assertEqual(wire, encoded.value.data)
        decoded = adapter.decode_payload(PresentationDecodeRequest(route=ROUTE, data=wire))
        self.assertTrue(decoded.is_ok)
        self.assertEqual(application, decoded.value.application_payload)
        self.assertEqual("test.stage0.opaque", decoded.value.payload_format.value)


class Stage5ElectronicChartGateTest(unittest.TestCase):
    def test_TC_STAGE5_011_chart_key_is_not_a_registry_entry_in_any_scope(self):
        for scope in (RegistryScope.OPERATIONAL, RegistryScope.TEST):
            with self.subTest(scope=scope.value):
                keys = {entry.payload_format.value for entry in entries_for(scope)}
                self.assertNotIn(ELECTRONIC_CHART_RESERVED_KEY, keys)

    def test_TC_STAGE5_011_chart_submit_is_unsupported_in_both_scopes(self):
        for scope in (RegistryScope.OPERATIONAL, RegistryScope.TEST):
            with self.subTest(scope=scope.value):
                adapter = _adapter(scope)
                encoded = adapter.encode_payload(
                    PresentationEncodeRequest(
                        transfer_id=TRANSFER_ID,
                        route=ROUTE,
                        payload_format=PayloadFormat(ELECTRONIC_CHART_RESERVED_KEY),
                        application_payload=b"\x01\x02\x03",
                    )
                )
                self.assertTrue(encoded.is_err)
                self.assertEqual("UNSUPPORTED_REQUEST", encoded.error.code)
                bound = adapter.encoded_size_bound(
                    PayloadFormat(ELECTRONIC_CHART_RESERVED_KEY), 3
                )
                self.assertTrue(bound.is_err)


if __name__ == "__main__":
    unittest.main()
