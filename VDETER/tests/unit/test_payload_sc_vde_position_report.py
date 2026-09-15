"""TC-PAYLOAD-004: SC-VDE Position Report MF 1 codec과 direct adapter 정책.

요구사항: `PAYLOAD-REQ-13`, `PAYLOAD-REQ-14`, `PAYLOAD-REQ-15`
(`payload_extension_baseline.md` §6.5, §8.1, §13.1 MF 1 시험 묶음,
`VEC/ENC/DEC/RT/LEN/BOUND/FIELD/RP/TRUNC/ERR-ORDER-01`)

기준 vector는 production codec으로 생성하지 않았다. §6.5.6의 승인 hex를 materialize한
`tests/vectors/presentation/sc_vde_position_report.*.bin`과 이 module의 독립 MSB-first
packer를 기대값으로 사용한다.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.presentation import PresentationEncodeRequest
from vdeter.common.errors import ErrorCode
from vdeter.common.identifiers import PayloadFormat, StationId, TransferId
from vdeter.presentation.adapter import build_presentation_adapter as _build_presentation_adapter
from vdeter.security.dependencies import E2eSecurityDependencies


def build_presentation_adapter(registry):
    return _build_presentation_adapter(registry, e2e_security=E2eSecurityDependencies.disabled())
from vdeter.presentation.codecs.sc_vde_position_report import (
    POSITION_REPORT_APPLICATION_BYTES,
    POSITION_REPORT_WIRE_BYTES,
    ScVdePositionReportCodec,
)
from vdeter.presentation.registry import build_payload_format_registry
from vdeter.profiles.models import RegistryScope
from vdeter.profiles.payload_formats import STAGE5_PAYLOAD_CONTENT_VERSION

from pathlib import Path

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

#: 승인 문서 §6.5.6에 고정된 독립 기대값 (TEST wire).
POSREP_APP_SHA256 = "ecf52787bf8a14f02abd22e682f2af0ef885e07b71dcd15174e0b687a52465bb"
POSREP_WIRE_SHA256 = "7ee1b782bb200755d5755b10039a19f564d17b6f7459d72796f3d3bc8545e25d"

POSITION_REPORT = PayloadFormat("sc_vde.position_report")
_TRANSFER_ID = TransferId("4c1ad0be-0000-4000-8000-0000000000aa")
_ADDRESSED_ROUTE = RouteAddresses(
    original_source=OriginalSource(StationId(111111111)),
    original_destination=OriginalDestination(Addressed(StationId(222222222))),
)
_BROADCAST_ROUTE = RouteAddresses(
    original_source=OriginalSource(StationId(111111111)),
    original_destination=OriginalDestination(Broadcast()),
)


def _pack_msb_first(fields) -> tuple:
    bits = 0
    total = 0
    for value, width in fields:
        bits = (bits << width) | (value & ((1 << width) - 1))
        total += width
    return bits, total


def pack_position_report(
    *,
    navigation_status=0,
    rot=0,
    sog=125,
    position_accuracy=0,
    longitude=77_445_600,
    latitude=21_061_200,
    cog=472,
    true_heading=47,
    timestamp=30,
    maneuver=0,
    raim=0,
    autonomy=2,
    padding=0,
) -> bytes:
    """production codec과 독립인 시험 자체 MSB-first packer (§6.5.7 field 순서).

    기본값은 §6.5.6 승인 vector의 독립 해석값이다.
    """

    fields = (
        (navigation_status, 4),
        (rot, 8),
        (sog, 10),
        (position_accuracy, 1),
        (longitude, 28),
        (latitude, 27),
        (cog, 12),
        (true_heading, 9),
        (timestamp, 6),
        (maneuver, 2),
        (raim, 1),
        (autonomy, 3),
        (padding, 1),
    )
    bits, total = _pack_msb_first(fields)
    assert total == POSITION_REPORT_APPLICATION_BYTES * 8
    return bits.to_bytes(POSITION_REPORT_APPLICATION_BYTES, "big")


def position_wire(application: bytes) -> bytes:
    return b"\xff\x7f\x01" + application


def _test_adapter():
    registry = build_payload_format_registry(
        RegistryScope.TEST, STAGE5_PAYLOAD_CONTENT_VERSION
    ).value
    registry.freeze()
    built = build_presentation_adapter(registry)
    if built.is_err:
        raise AssertionError(built.error)
    return built.value


class PositionReportApprovedVectorTest(unittest.TestCase):
    """`VEC-01`, `ENC-01`, `DEC-01`, `RT-01`: 승인 vector·hash exact."""

    def setUp(self):
        self.codec = ScVdePositionReportCodec()
        self.application = (
            VECTOR_ROOT / "sc_vde_position_report.application.bin"
        ).read_bytes()
        self.wire = (VECTOR_ROOT / "sc_vde_position_report.wire.bin").read_bytes()

    def test_TC_PAYLOAD_004_artifacts_match_approved_length_and_sha256(self):
        self.assertEqual(14, len(self.application))
        self.assertEqual(17, len(self.wire))
        self.assertEqual(POSREP_APP_SHA256, hashlib.sha256(self.application).hexdigest())
        self.assertEqual(POSREP_WIRE_SHA256, hashlib.sha256(self.wire).hexdigest())

    def test_TC_PAYLOAD_004_manifest_matches_approved_hashes(self):
        manifest = json.loads(
            (VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        entry = manifest["vectors"]["GV-PAYLOAD-SCVDE-POSREP-001"]
        self.assertEqual(POSREP_APP_SHA256, entry["application"]["sha256"])
        self.assertEqual(POSREP_WIRE_SHA256, entry["wire"]["sha256"])
        self.assertEqual("TEST", entry["scope"])
        self.assertEqual("sc_vde.position_report", entry["payload_format"])

    def test_TC_PAYLOAD_004_wire_is_prefix_plus_application(self):
        self.assertEqual(b"\xff\x7f\x01", self.wire[:3])
        self.assertEqual(self.application, self.wire[3:])

    def test_TC_PAYLOAD_004_independent_packer_reproduces_approved_vector(self):
        # §6.5.6 독립 해석값 → 승인 application bytes. production codec과 독립.
        self.assertEqual(self.application, pack_position_report())
        self.assertEqual(self.wire, position_wire(pack_position_report()))

    def test_TC_PAYLOAD_004_encode_matches_approved_wire(self):
        encoded = self.codec.encode(self.application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(self.wire, encoded.value)
        self.assertEqual(POSREP_WIRE_SHA256, hashlib.sha256(encoded.value).hexdigest())

    def test_TC_PAYLOAD_004_decode_returns_exact_application_bytes(self):
        decoded = self.codec.decode(self.wire)
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(self.application, decoded.value)

    def test_TC_PAYLOAD_004_round_trip_preserves_every_byte(self):
        application = pack_position_report(
            navigation_status=8, rot=-127, sog=1022, position_accuracy=1,
            longitude=-108_000_000, latitude=-54_000_000, cog=3599,
            true_heading=359, timestamp=63, maneuver=2, raim=1, autonomy=3,
        )
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        self.assertEqual(position_wire(application), encoded.value)
        decoded = self.codec.decode(encoded.value)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(application, decoded.value)


class PositionReportLengthTest(unittest.TestCase):
    """`LEN-01`, `BOUND-01`, `TRUNC-01`: 고정 14/17 bytes와 경계."""

    def setUp(self):
        self.codec = ScVdePositionReportCodec()

    def test_TC_PAYLOAD_004_bound_accepts_only_14_and_returns_17(self):
        bound = self.codec.encoded_size_bound(POSITION_REPORT_APPLICATION_BYTES)
        self.assertTrue(bound.is_ok)
        self.assertEqual(POSITION_REPORT_WIRE_BYTES, bound.value)
        for length in (0, 1, 2, 13, 15, 16, 17, 65_531):
            with self.subTest(length=length):
                rejected = self.codec.encoded_size_bound(length)
                self.assertTrue(rejected.is_err)
                self.assertEqual("INVALID_REQUEST", rejected.error.code)

    def test_TC_PAYLOAD_004_actual_encode_length_matches_bound(self):
        application = pack_position_report()
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok)
        self.assertEqual(
            self.codec.encoded_size_bound(len(application)).value, len(encoded.value)
        )
        self.assertEqual(17, len(encoded.value))

    def test_TC_PAYLOAD_004_wrong_application_lengths_are_rejected_by_encode(self):
        for length in (0, 1, 13, 15, 65_531):
            with self.subTest(length=length):
                result = self.codec.encode(b"\x00" * length)
                self.assertTrue(result.is_err)
                self.assertEqual("INVALID_REQUEST", result.error.code)

    def test_TC_PAYLOAD_004_wire_truncation_and_trailing_are_rejected(self):
        wire = position_wire(pack_position_report())
        for cut in (3, 4, 10, 16):
            with self.subTest(cut=cut):
                result = self.codec.decode(wire[:cut])
                self.assertTrue(result.is_err)
                self.assertEqual("PRESENTATION_TRUNCATED", result.error.code)
        result = self.codec.decode(wire + b"\x00")
        self.assertTrue(result.is_err)
        self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)


class PositionReportFieldBoundaryTest(unittest.TestCase):
    """`FIELD-01`, `RP-01`: §6.5.8 canonical raw 허용 집합의 경계."""

    def setUp(self):
        self.codec = ScVdePositionReportCodec()

    def _assert_accepts(self, **overrides):
        application = pack_position_report(**overrides)
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok, (overrides, getattr(encoded, "error", None)))
        decoded = self.codec.decode(position_wire(application))
        self.assertTrue(decoded.is_ok, overrides)
        self.assertEqual(application, decoded.value)

    def _assert_rejects(self, **overrides):
        application = pack_position_report(**overrides)
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_err, overrides)
        self.assertEqual("INVALID_REQUEST", encoded.error.code)
        decoded = self.codec.decode(position_wire(application))
        self.assertTrue(decoded.is_err, overrides)
        self.assertEqual("PRESENTATION_STRUCTURE_INVALID", decoded.error.code)

    def test_TC_PAYLOAD_004_navigation_status_boundaries(self):
        for status in (0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15):
            self._assert_accepts(navigation_status=status)
        for status in (9, 10, 13):
            self._assert_rejects(navigation_status=status)

    def test_TC_PAYLOAD_004_rot_full_signed_width_is_allowed(self):
        for rot in (-128, -127, 0, 126, 127):
            self._assert_accepts(rot=rot)

    def test_TC_PAYLOAD_004_sog_full_width_is_allowed(self):
        for sog in (0, 1, 1022, 1023):
            self._assert_accepts(sog=sog)

    def test_TC_PAYLOAD_004_position_accuracy_full_width_is_allowed(self):
        for accuracy in (0, 1):
            self._assert_accepts(position_accuracy=accuracy)

    def test_TC_PAYLOAD_004_longitude_boundaries(self):
        for longitude in (0, 108_000_000, -108_000_000, 108_600_000, -1):
            self._assert_accepts(longitude=longitude)
        for longitude in (
            108_000_001,
            108_599_999,
            108_600_001,
            -108_000_001,
            (1 << 27) - 1,  # signed 최대 134217727
            -(1 << 27),  # signed 최소 -134217728
        ):
            self._assert_rejects(longitude=longitude)

    def test_TC_PAYLOAD_004_latitude_boundaries(self):
        for latitude in (0, 54_000_000, -54_000_000, 54_600_000, -1):
            self._assert_accepts(latitude=latitude)
        for latitude in (
            54_000_001,
            54_599_999,
            54_600_001,
            -54_000_001,
            (1 << 26) - 1,  # signed 최대 67108863
            -(1 << 26),  # signed 최소 -67108864
        ):
            self._assert_rejects(latitude=latitude)

    def test_TC_PAYLOAD_004_cog_boundaries(self):
        for cog in (0, 3599, 3600):
            self._assert_accepts(cog=cog)
        for cog in (3601, 4095):
            self._assert_rejects(cog=cog)

    def test_TC_PAYLOAD_004_true_heading_boundaries(self):
        for heading in (0, 359, 511):
            self._assert_accepts(true_heading=heading)
        for heading in (360, 510):
            self._assert_rejects(true_heading=heading)

    def test_TC_PAYLOAD_004_timestamp_full_range_is_allowed(self):
        # `DR-PAYLOAD-015` 승인: 0..59 UTC 초, 60=N/A, 61=manual, 62=estimated,
        # 63=inoperative를 포함한 0..63 전체 허용.
        for timestamp in (0, 30, 59, 60, 61, 62, 63):
            self._assert_accepts(timestamp=timestamp)
        # 6-bit raw 폭 밖은 구조적으로 표현 불가하므로 Timestamp 단독 reserved/raw
        # 거부 vector는 NOT_APPLICABLE이다 (§13.1).
        self.assertEqual(63, (1 << 6) - 1)

    def test_TC_PAYLOAD_004_maneuver_boundaries(self):
        for maneuver in (0, 1, 2):
            self._assert_accepts(maneuver=maneuver)
        self._assert_rejects(maneuver=3)

    def test_TC_PAYLOAD_004_raim_full_width_is_allowed(self):
        for raim in (0, 1):
            self._assert_accepts(raim=raim)

    def test_TC_PAYLOAD_004_autonomy_mode_boundaries(self):
        for autonomy in (0, 1, 2, 3):
            self._assert_accepts(autonomy=autonomy)
        for autonomy in (4, 5, 6, 7):
            self._assert_rejects(autonomy=autonomy)

    def test_TC_PAYLOAD_004_nonzero_padding_is_rejected(self):
        self._assert_rejects(padding=1)


class PositionReportCodecMetadataTest(unittest.TestCase):
    """`REG-01` 보조: codec identity·route/ACK 정책 metadata (§6.5.3, §8.1)."""

    def test_TC_PAYLOAD_004_codec_identity_and_policy_metadata(self):
        codec = ScVdePositionReportCodec()
        self.assertEqual("project.sc_vde.vpfi_ff7f.mf1.v1", codec.codec_ref)
        self.assertEqual(16383, codec.vpfi)
        self.assertEqual(1, codec.message_format)
        self.assertEqual(b"\xff\x7f\x01", codec.discriminator.prefix)
        self.assertNotEqual((16383).to_bytes(2, "big"), codec.discriminator.prefix[:2])
        self.assertIs(True, codec.addressed_only)
        self.assertIs(bool, type(codec.addressed_only))
        self.assertIsInstance(codec.allowed_ack_required, frozenset)
        self.assertEqual(frozenset({False}), codec.allowed_ack_required)
        self.assertEqual(17, codec.min_wire_length)
        self.assertEqual(17, codec.max_wire_bytes)

    def test_TC_PAYLOAD_004_source_ff01_and_logical_3fff_wires_are_not_decodable(self):
        codec = ScVdePositionReportCodec()
        application = pack_position_report()
        for wire in (b"\xff\x01\x01" + application, b"\x3f\xff\x01" + application):
            with self.subTest(prefix=wire[:3].hex()):
                result = codec.decode(wire)
                self.assertTrue(result.is_err)
                self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)


class PositionReportDirectAdapterOrderTest(unittest.TestCase):
    """`ERR-ORDER-01`: direct Presentation 오류 우선순위와 뒤 단계 미호출 (§6.5.13)."""

    @classmethod
    def setUpClass(cls):
        cls.adapter = _test_adapter()

    def _encode(self, *, route, payload, ack_required):
        return self.adapter.encode_payload(
            PresentationEncodeRequest(
                transfer_id=_TRANSFER_ID,
                route=route,
                payload_format=POSITION_REPORT,
                application_payload=payload,
                ack_required=ack_required,
            )
        )

    def test_TC_PAYLOAD_004_invalid_length_wins_over_route_ack_and_field(self):
        # 잘못된 길이 + broadcast + ACK: length/bound 오류가 최초 오류다.
        with patch.object(ScVdePositionReportCodec, "encode") as encode_spy:
            result = self._encode(
                route=_BROADCAST_ROUTE, payload=b"\x00" * 13, ack_required=True
            )
        encode_spy.assert_not_called()
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)
        self.assertIn("14 bytes", result.error.message)

    def test_TC_PAYLOAD_004_route_wins_over_ack_and_field(self):
        # 유효 길이 + broadcast + ACK(+invalid field): route 오류가 우선하고
        # codec.encode()는 호출되지 않는다.
        payload = pack_position_report(autonomy=7)
        with patch.object(ScVdePositionReportCodec, "encode") as encode_spy:
            result = self._encode(
                route=_BROADCAST_ROUTE, payload=payload, ack_required=True
            )
        encode_spy.assert_not_called()
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.UNSUPPORTED_REQUEST, result.error.code)

    def test_TC_PAYLOAD_004_ack_policy_wins_over_field(self):
        # 유효 길이 + addressed + ACK + invalid field: ACK 정책 오류가 우선하고
        # codec.encode()는 호출되지 않는다.
        payload = pack_position_report(autonomy=7)
        with patch.object(ScVdePositionReportCodec, "encode") as encode_spy:
            result = self._encode(
                route=_ADDRESSED_ROUTE, payload=payload, ack_required=True
            )
        encode_spy.assert_not_called()
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)
        self.assertIn("ack_required", result.error.message)

    def test_TC_PAYLOAD_004_field_error_reaches_codec_last(self):
        result = self._encode(
            route=_ADDRESSED_ROUTE,
            payload=pack_position_report(autonomy=7),
            ack_required=False,
        )
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.INVALID_REQUEST, result.error.code)
        self.assertIn("Autonomy", result.error.message)

    def test_TC_PAYLOAD_004_valid_addressed_no_ack_encodes_17_bytes(self):
        payload = pack_position_report()
        result = self._encode(
            route=_ADDRESSED_ROUTE, payload=payload, ack_required=False
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))
        self.assertEqual(17, len(result.value.data))
        self.assertEqual(b"\xff\x7f\x01", result.value.data[:3])
        self.assertEqual(payload, result.value.data[3:])
        self.assertEqual(16383, result.value.vpfi.value)
        self.assertEqual(1, result.value.message_format)
        # 공개 bound helper와 direct encode가 같은 판정을 공유한다 (§8.1).
        bound = self.adapter.encoded_size_bound(POSITION_REPORT, len(payload))
        self.assertTrue(bound.is_ok)
        self.assertEqual(bound.value, len(result.value.data))

    def test_TC_PAYLOAD_004_addressed_station_identity_is_not_validated(self):
        # `ROLE-DIR-01` 경계: Presentation은 address kind만 강제한다. 임의 addressed
        # station(예: 다른 MASS)은 codec/adapter 차원에서 거부하지 않으며 실제
        # roc_id 사용은 application/deployment·Service E2E가 확인한다 (§6.5.4).
        arbitrary_station_route = RouteAddresses(
            original_source=OriginalSource(StationId(111111111)),
            original_destination=OriginalDestination(Addressed(StationId(333333333))),
        )
        result = self._encode(
            route=arbitrary_station_route,
            payload=pack_position_report(),
            ack_required=False,
        )
        self.assertTrue(result.is_ok, getattr(result, "error", None))


if __name__ == "__main__":
    unittest.main()
