"""TC-PAYLOAD-001: SC-VDE Route/Remote-control MF 2·3 codec.

요구사항: `PAYLOAD-REQ-04`, `PAYLOAD-REQ-05` (`payload_extension_baseline.md` §6.4,
§13.1 `VEC/ENC/DEC/RT/LEN/BOUND/FIELD/RP/TRUNC-01`)

기준 vector는 production codec으로 생성하지 않았다. §6.4.6 `CALC-SC-VDE-001`의 승인
hex를 materialize한 `tests/vectors/presentation/sc_vde_route_control.*.bin`과 이
module의 독립 bit packer를 기대값으로 사용한다.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from vdeter.presentation.codecs.sc_vde_route_control import (
    ACK_APPLICATION_BYTES,
    ACK_WIRE_BYTES,
    COMMAND_ALLOWED_APPLICATION_BYTES,
    COMMAND_MAX_WAYPOINTS,
    ScVdeRouteControlAckCodec,
    ScVdeRouteControlCommandCodec,
    command_application_bytes,
)

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "vectors" / "presentation"

#: 승인 문서 §6.4.6에 고정된 독립 기대값 (TEST wire).
REMOTE_APP_SHA256 = "fa9a5de2a93353714280d0d383ea75c11ae10906138cdea5f5867340f296d940"
REMOTE_WIRE_SHA256 = "3e134c9e3a8462ad854b66e56a1b51c40759fd44eaab15701d7ecf2565cf86e1"
ROUTE_APP_SHA256 = "cf3cf064041b8a77ee4a5937bd28e22f6589f08f199c781cb7748affe765826d"
ROUTE_WIRE_SHA256 = "09dac8e5270e49e2a1a5f66bde4cbf4ed64dcf90844cd564064bb7f5421e770d"
ACK_APP_SHA256 = "ffdea84918ceb8e9294250db3f8a1cab7b2c83cfcc5c5b03e7c6d66fba183d12"
ACK_WIRE_SHA256 = "d0ea43bcf24e54e71d52fda9a563b54bbf5f3b742b10fa3e781b08fac10b801a"

_START_NA = dict(month=0, day=0, hour=24, minute=60)


def _pack_msb_first(fields) -> tuple:
    bits = 0
    total = 0
    for value, width in fields:
        bits = (bits << width) | (value & ((1 << width) - 1))
        total += width
    return bits, total


def pack_command_application(
    *,
    linkage=7,
    sender=1,
    route_type=1,
    month=0,
    day=0,
    hour=24,
    minute=60,
    duration=262143,
    heading=3600,
    fallback=0,
    count=None,
    waypoints=(),
) -> bytes:
    """production codec과 독립인 시험 자체 MSB-first packer (§6.4.7 field 순서)."""

    declared_count = len(waypoints) if count is None else count
    fields = [
        (linkage, 10),
        (sender, 3),
        (route_type, 5),
        (month, 4),
        (day, 5),
        (hour, 5),
        (minute, 6),
        (duration, 18),
        (heading, 12),
        (fallback, 3),
        (declared_count, 5),
    ]
    for longitude, latitude, radius, sog, geometry in waypoints:
        fields.extend(
            (
                (longitude, 28),
                (latitude, 27),
                (radius, 10),
                (sog, 10),
                (geometry, 2),
            )
        )
    bits, total = _pack_msb_first(fields)
    length = (total + 7) // 8
    return (bits << (length * 8 - total)).to_bytes(length, "big")


def pack_ack_application(*, linkage=7, status=4, reason=0, padding=0) -> bytes:
    bits, total = _pack_msb_first(((linkage, 10), (status, 4), (reason, 3), (padding, 7)))
    assert total == 24
    return bits.to_bytes(ACK_APPLICATION_BYTES, "big")


def command_wire(application: bytes) -> bytes:
    return b"\xff\x7f\x02" + application


def ack_wire(application: bytes) -> bytes:
    return b"\xff\x7f\x03" + application


#: §6.4.6 `CALC-SC-VDE-001` 계산표의 field 값. 독립 packer가 승인 vector bytes를
#: 재현하는지 검증하는 데 사용한다.
REMOTE_FIELDS = dict(
    linkage=7,
    sender=1,
    route_type=1,
    duration=262143,
    heading=925,
    fallback=1,
    waypoints=(
        (77_430_000, 21_060_000, 30, 60, 1),
        (77_460_000, 21_079_800, 30, 40, 1),
    ),
    **_START_NA,
)
ROUTE_FIELDS = dict(
    linkage=7,
    sender=1,
    route_type=1,
    duration=262143,
    heading=3600,
    fallback=0,
    waypoints=(
        (77_445_600, 21_061_200, 50, 125, 1),
        (77_599_800, 21_124_800, 120, 80, 2),
    ),
    **_START_NA,
)


class ScVdeApprovedVectorTest(unittest.TestCase):
    """`VEC-01`, `ENC-01`, `DEC-01`, `RT-01`: 승인 vector와 hash exact 일치."""

    def setUp(self):
        self.command_codec = ScVdeRouteControlCommandCodec()
        self.ack_codec = ScVdeRouteControlAckCodec()
        self.vectors = {
            name: (
                (VECTOR_ROOT / f"sc_vde_route_control.{name}.application.bin").read_bytes(),
                (VECTOR_ROOT / f"sc_vde_route_control.{name}.wire.bin").read_bytes(),
            )
            for name in ("remote_command_n2", "route_command_n2", "ack")
        }

    def test_TC_PAYLOAD_001_artifacts_match_approved_length_and_sha256(self):
        expected = {
            "remote_command_n2": (29, REMOTE_APP_SHA256, 32, REMOTE_WIRE_SHA256),
            "route_command_n2": (29, ROUTE_APP_SHA256, 32, ROUTE_WIRE_SHA256),
            "ack": (3, ACK_APP_SHA256, 6, ACK_WIRE_SHA256),
        }
        for name, (app_len, app_sha, wire_len, wire_sha) in expected.items():
            with self.subTest(vector=name):
                application, wire = self.vectors[name]
                self.assertEqual(app_len, len(application))
                self.assertEqual(wire_len, len(wire))
                self.assertEqual(app_sha, hashlib.sha256(application).hexdigest())
                self.assertEqual(wire_sha, hashlib.sha256(wire).hexdigest())

    def test_TC_PAYLOAD_001_manifest_matches_approved_hashes(self):
        manifest = json.loads(
            (VECTOR_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        expected = {
            "GV-PAYLOAD-SCVDE-REMOTE-001": (REMOTE_APP_SHA256, REMOTE_WIRE_SHA256),
            "GV-PAYLOAD-SCVDE-ROUTE-001": (ROUTE_APP_SHA256, ROUTE_WIRE_SHA256),
            "GV-PAYLOAD-SCVDE-ACK-001": (ACK_APP_SHA256, ACK_WIRE_SHA256),
        }
        for vector_id, (app_sha, wire_sha) in expected.items():
            with self.subTest(vector=vector_id):
                entry = manifest["vectors"][vector_id]
                self.assertEqual(app_sha, entry["application"]["sha256"])
                self.assertEqual(wire_sha, entry["wire"]["sha256"])
                self.assertEqual("TEST", entry["scope"])

    def test_TC_PAYLOAD_001_wire_prefix_is_test_ff7f_carrier(self):
        remote_app, remote_wire = self.vectors["remote_command_n2"]
        route_app, route_wire = self.vectors["route_command_n2"]
        ack_app, ack_wire_bytes = self.vectors["ack"]
        for wire, application in (
            (remote_wire, remote_app),
            (route_wire, route_app),
        ):
            self.assertEqual(b"\xff\x7f\x02", wire[:3])
            self.assertEqual(application, wire[3:])
        self.assertEqual(b"\xff\x7f\x03", ack_wire_bytes[:3])
        self.assertEqual(ack_app, ack_wire_bytes[3:])

    def test_TC_PAYLOAD_001_independent_packer_reproduces_approved_vectors(self):
        # §6.4.6 계산표 field 값 → 승인 application bytes. production codec과 독립.
        self.assertEqual(
            self.vectors["remote_command_n2"][0],
            pack_command_application(**REMOTE_FIELDS),
        )
        self.assertEqual(
            self.vectors["route_command_n2"][0],
            pack_command_application(**ROUTE_FIELDS),
        )
        self.assertEqual(
            self.vectors["ack"][0], pack_ack_application(linkage=7, status=4, reason=0)
        )

    def test_TC_PAYLOAD_001_encode_matches_approved_wire_bytes(self):
        for name, codec in (
            ("remote_command_n2", self.command_codec),
            ("route_command_n2", self.command_codec),
            ("ack", self.ack_codec),
        ):
            with self.subTest(vector=name):
                application, wire = self.vectors[name]
                encoded = codec.encode(application)
                self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
                self.assertEqual(wire, encoded.value)

    def test_TC_PAYLOAD_001_decode_returns_exact_application_bytes(self):
        for name, codec in (
            ("remote_command_n2", self.command_codec),
            ("route_command_n2", self.command_codec),
            ("ack", self.ack_codec),
        ):
            with self.subTest(vector=name):
                application, wire = self.vectors[name]
                decoded = codec.decode(wire)
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(application, decoded.value)


class ScVdeCommandLengthTest(unittest.TestCase):
    """`LEN-01`, `BOUND-01`: n=0..16 허용 길이 집합과 `encoded_size_bound`."""

    def setUp(self):
        self.codec = ScVdeRouteControlCommandCodec()

    def test_TC_PAYLOAD_001_allowed_length_set_is_the_approved_17_lengths(self):
        self.assertEqual(
            (10, 20, 29, 39, 48, 58, 68, 77, 87, 97, 106, 116, 125, 135, 145, 154, 164),
            COMMAND_ALLOWED_APPLICATION_BYTES,
        )
        for count in range(COMMAND_MAX_WAYPOINTS + 1):
            self.assertEqual(
                COMMAND_ALLOWED_APPLICATION_BYTES[count],
                command_application_bytes(count),
            )

    def test_TC_PAYLOAD_001_bound_returns_L_plus_3_only_for_allowed_lengths(self):
        for length in COMMAND_ALLOWED_APPLICATION_BYTES:
            with self.subTest(length=length):
                bound = self.codec.encoded_size_bound(length)
                self.assertTrue(bound.is_ok)
                self.assertEqual(length + 3, bound.value)

    def test_TC_PAYLOAD_001_bound_rejects_every_other_length(self):
        allowed = set(COMMAND_ALLOWED_APPLICATION_BYTES)
        # 허용 길이 각 이웃과 대표 극단값을 거부한다.
        neighbours = {value + delta for value in allowed for delta in (-1, 1)}
        for length in sorted((neighbours - allowed) | {0, 1, 165, 1000, 65_531}):
            with self.subTest(length=length):
                self.assertTrue(self.codec.encoded_size_bound(length).is_err)

    def test_TC_PAYLOAD_001_every_waypoint_count_round_trips_byte_exact(self):
        for count in range(COMMAND_MAX_WAYPOINTS + 1):
            with self.subTest(count=count):
                waypoints = tuple(
                    (
                        1_000_000 + index,
                        -2_000_000 - index,
                        index % 1024,
                        (index * 7) % 1024,
                        index % 3,
                    )
                    for index in range(count)
                )
                application = pack_command_application(waypoints=waypoints)
                self.assertEqual(
                    command_application_bytes(count), len(application)
                )
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
                # 독립 packer 기대 wire와 비교하고 bound와 실제 길이 일치를 확인한다.
                self.assertEqual(command_wire(application), encoded.value)
                self.assertEqual(
                    self.codec.encoded_size_bound(len(application)).value,
                    len(encoded.value),
                )
                decoded = self.codec.decode(encoded.value)
                self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
                self.assertEqual(application, decoded.value)

    def test_TC_PAYLOAD_001_count_and_length_mismatch_is_rejected(self):
        two_waypoints = (
            (77_430_000, 21_060_000, 30, 60, 1),
            (77_460_000, 21_079_800, 30, 40, 1),
        )
        # 허용 길이 29 bytes지만 count field가 1 또는 3인 body.
        for declared in (1, 3):
            with self.subTest(declared=declared):
                application = pack_command_application(
                    count=declared, waypoints=two_waypoints
                )
                self.assertIn(len(application), COMMAND_ALLOWED_APPLICATION_BYTES)
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_err)
                self.assertEqual("INVALID_REQUEST", encoded.error.code)
                decoded = self.codec.decode(command_wire(application))
                self.assertTrue(decoded.is_err)
                self.assertIn(
                    decoded.error.code,
                    ("PRESENTATION_STRUCTURE_INVALID", "PRESENTATION_TRUNCATED"),
                )

    def test_TC_PAYLOAD_001_reserved_waypoint_counts_17_and_31_are_rejected(self):
        base = pack_command_application(waypoints=())
        for declared in (17, 31):
            with self.subTest(declared=declared):
                application = pack_command_application(count=declared, waypoints=())
                self.assertEqual(len(base), len(application))
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_err)
                self.assertEqual("INVALID_REQUEST", encoded.error.code)
                decoded = self.codec.decode(command_wire(application))
                self.assertTrue(decoded.is_err)
                self.assertEqual(
                    "PRESENTATION_STRUCTURE_INVALID", decoded.error.code
                )

    def test_TC_PAYLOAD_001_wire_truncation_and_trailing_bytes_are_rejected(self):
        application = pack_command_application(
            waypoints=((77_430_000, 21_060_000, 30, 60, 1),) * 2
        )
        wire = command_wire(application)
        # header truncation, fixed body truncation과 waypoint 가변 field 중간
        # 절단(cut=20: 첫 waypoint 내부, cut=25: 두 번째 waypoint 내부)은 모두
        # PROTOCOL truncated다.
        for cut in (3, 4, 12, 20, 25):
            with self.subTest(cut=cut):
                result = self.codec.decode(wire[:cut])
                self.assertTrue(result.is_err)
                self.assertEqual("PRESENTATION_TRUNCATED", result.error.code)
        # 선언된 count의 expected byte 바로 전 truncation.
        result = self.codec.decode(wire[:-1])
        self.assertTrue(result.is_err)
        self.assertEqual("PRESENTATION_TRUNCATED", result.error.code)
        # expected byte 바로 뒤 trailing byte.
        result = self.codec.decode(wire + b"\x00")
        self.assertTrue(result.is_err)
        self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)

    def test_TC_PAYLOAD_001_nonzero_padding_is_rejected_for_each_direction(self):
        # n=0(4 padding bits), n=1(7 bits), n=2(2 bits): 마지막 byte의 최하위 bit 오염.
        for count in (0, 1, 2):
            with self.subTest(count=count):
                waypoints = ((77_430_000, 21_060_000, 30, 60, 1),) * count
                application = bytearray(pack_command_application(waypoints=waypoints))
                application[-1] |= 0x01
                tampered = bytes(application)
                encoded = self.codec.encode(tampered)
                self.assertTrue(encoded.is_err)
                self.assertEqual("INVALID_REQUEST", encoded.error.code)
                decoded = self.codec.decode(command_wire(tampered))
                self.assertTrue(decoded.is_err)
                self.assertEqual(
                    "PRESENTATION_STRUCTURE_INVALID", decoded.error.code
                )

    def test_TC_PAYLOAD_001_zero_padding_boundary_n4_has_no_padding_bits(self):
        # n=4는 76+308=384 bits로 byte 정렬이라 padding 검증이 개입하지 않아야 한다.
        waypoints = ((1, 1, 1, 1, 1),) * 4
        application = pack_command_application(waypoints=waypoints)
        self.assertEqual(48, len(application))
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        decoded = self.codec.decode(encoded.value)
        self.assertTrue(decoded.is_ok)
        self.assertEqual(application, decoded.value)


class ScVdeCommandFieldBoundaryTest(unittest.TestCase):
    """`FIELD-01`, `RP-01`: §6.4.8 canonical raw 허용 집합의 경계."""

    def setUp(self):
        self.codec = ScVdeRouteControlCommandCodec()

    def _assert_accepts(self, **overrides):
        application = pack_command_application(**overrides)
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
        decoded = self.codec.decode(command_wire(application))
        self.assertTrue(decoded.is_ok, getattr(decoded, "error", None))
        self.assertEqual(application, decoded.value)

    def _assert_rejects(self, **overrides):
        application = pack_command_application(**overrides)
        encoded = self.codec.encode(application)
        self.assertTrue(encoded.is_err, overrides)
        self.assertEqual("INVALID_REQUEST", encoded.error.code)
        decoded = self.codec.decode(command_wire(application))
        self.assertTrue(decoded.is_err, overrides)
        self.assertEqual("PRESENTATION_STRUCTURE_INVALID", decoded.error.code)

    def test_TC_PAYLOAD_001_linkage_id_full_width_is_allowed(self):
        for linkage in (0, 1, 1023):
            self._assert_accepts(linkage=linkage)

    def test_TC_PAYLOAD_001_sender_classification_boundaries(self):
        for sender in (0, 1):
            self._assert_accepts(sender=sender)
        for sender in (2, 3, 7):
            self._assert_rejects(sender=sender)

    def test_TC_PAYLOAD_001_route_type_boundaries(self):
        for route_type in (0, 1, 2, 5, 31):
            self._assert_accepts(route_type=route_type)
        for route_type in (3, 4, 6, 30):
            self._assert_rejects(route_type=route_type)

    def test_TC_PAYLOAD_001_start_tuple_boundaries(self):
        self._assert_accepts(month=0, day=0, hour=24, minute=60)  # 전체 N/A
        self._assert_accepts(month=1, day=1, hour=0, minute=0)
        self._assert_accepts(month=12, day=31, hour=23, minute=59)
        # 범위 밖 값은 거부한다.
        for overrides in (
            dict(month=13, day=1, hour=0, minute=0),
            dict(month=15, day=1, hour=0, minute=0),
            dict(month=1, day=1, hour=25, minute=0),
            dict(month=1, day=1, hour=31, minute=0),
            dict(month=1, day=1, hour=0, minute=61),
            dict(month=1, day=1, hour=0, minute=63),
        ):
            self._assert_rejects(**overrides)

    def test_TC_PAYLOAD_001_every_mixed_na_start_tuple_is_rejected(self):
        # §6.4.8: N/A는 `0/0/24/60` 전체 tuple만 허용한다. field별 N/A 표지와 정상
        # 값이 섞인 나머지 14개 조합은 모두 혼합 N/A로 거부한다.
        na_tuple = dict(month=0, day=0, hour=24, minute=60)
        normal_tuple = dict(month=6, day=15, hour=12, minute=30)
        for mask in range(1, 15):  # 0b0001..0b1110: 전체 N/A(15)와 전체 정상(0) 제외
            overrides = {
                name: (na_tuple[name] if mask & bit else normal_tuple[name])
                for bit, name in ((8, "month"), (4, "day"), (2, "hour"), (1, "minute"))
            }
            with self.subTest(**overrides):
                self._assert_rejects(**overrides)

    def test_TC_PAYLOAD_001_duration_full_width_is_allowed(self):
        for duration in (0, 262_142, 262_143):
            self._assert_accepts(duration=duration)

    def test_TC_PAYLOAD_001_heading_boundaries(self):
        for heading in (0, 3599, 3600):
            self._assert_accepts(heading=heading)
        for heading in (3601, 4095):
            self._assert_rejects(heading=heading)

    def test_TC_PAYLOAD_001_fallback_boundaries(self):
        for fallback in (0, 1, 2, 3):
            self._assert_accepts(fallback=fallback)
        for fallback in (4, 7):
            self._assert_rejects(fallback=fallback)

    def _waypoint(self, **overrides):
        base = dict(
            longitude=77_430_000, latitude=21_060_000, radius=30, sog=60, geometry=1
        )
        base.update(overrides)
        return (
            base["longitude"],
            base["latitude"],
            base["radius"],
            base["sog"],
            base["geometry"],
        )

    def test_TC_PAYLOAD_001_longitude_boundaries(self):
        for longitude in (0, 108_000_000, -108_000_000, 108_600_000, -1):
            self._assert_accepts(waypoints=(self._waypoint(longitude=longitude),))
        # 정상 상한과 N/A 사이 gap, N/A 초과, 음수 하한 밖, signed 폭 끝값.
        for longitude in (
            108_000_001,
            108_599_999,
            108_600_001,
            -108_000_001,
            (1 << 27) - 1,  # signed 최대 134217727
            -(1 << 27),  # signed 최소 -134217728
        ):
            self._assert_rejects(waypoints=(self._waypoint(longitude=longitude),))

    def test_TC_PAYLOAD_001_latitude_boundaries(self):
        for latitude in (0, 54_000_000, -54_000_000, 54_600_000, -1):
            self._assert_accepts(waypoints=(self._waypoint(latitude=latitude),))
        for latitude in (
            54_000_001,
            54_599_999,
            54_600_001,
            -54_000_001,
            (1 << 26) - 1,  # signed 최대 67108863
            -(1 << 26),  # signed 최소 -67108864
        ):
            self._assert_rejects(waypoints=(self._waypoint(latitude=latitude),))

    def test_TC_PAYLOAD_001_radius_and_sog_full_width_are_allowed(self):
        for value in (0, 1022, 1023):
            self._assert_accepts(waypoints=(self._waypoint(radius=value),))
            self._assert_accepts(waypoints=(self._waypoint(sog=value),))

    def test_TC_PAYLOAD_001_leg_geometry_boundaries(self):
        for geometry in (0, 1, 2):
            self._assert_accepts(waypoints=(self._waypoint(geometry=geometry),))
        self._assert_rejects(waypoints=(self._waypoint(geometry=3),))

    def test_TC_PAYLOAD_001_second_waypoint_fields_are_also_validated(self):
        # waypoint 반복 구간의 offset 계산 오류를 잡기 위해 두 번째 waypoint를 오염시킨다.
        self._assert_rejects(
            waypoints=(self._waypoint(), self._waypoint(geometry=3))
        )
        self._assert_rejects(
            waypoints=(self._waypoint(), self._waypoint(longitude=108_000_001))
        )


class ScVdeAckCodecTest(unittest.TestCase):
    """ACK exact 길이·field·padding (`payload_extension_baseline.md` §6.4.8~§6.4.9)."""

    def setUp(self):
        self.codec = ScVdeRouteControlAckCodec()

    def test_TC_PAYLOAD_001_ack_bound_accepts_only_3_bytes(self):
        bound = self.codec.encoded_size_bound(ACK_APPLICATION_BYTES)
        self.assertTrue(bound.is_ok)
        self.assertEqual(ACK_WIRE_BYTES, bound.value)
        for length in (0, 1, 2, 4, 6):
            self.assertTrue(self.codec.encoded_size_bound(length).is_err, length)

    def test_TC_PAYLOAD_001_ack_status_boundaries(self):
        for status in (0, 4, 7, 8, 9):
            with self.subTest(status=status):
                application = pack_ack_application(status=status)
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
                decoded = self.codec.decode(encoded.value)
                self.assertTrue(decoded.is_ok)
                self.assertEqual(application, decoded.value)
        for status in (1, 2, 3, 5, 6, 10, 15):
            with self.subTest(status=status):
                application = pack_ack_application(status=status)
                self.assertTrue(self.codec.encode(application).is_err)
                self.assertTrue(self.codec.decode(ack_wire(application)).is_err)

    def test_TC_PAYLOAD_001_ack_reason_boundaries(self):
        for reason in (0, 1, 2, 3):
            with self.subTest(reason=reason):
                application = pack_ack_application(reason=reason)
                self.assertTrue(self.codec.encode(application).is_ok)
        for reason in (4, 7):
            with self.subTest(reason=reason):
                application = pack_ack_application(reason=reason)
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_err)
                self.assertEqual("INVALID_REQUEST", encoded.error.code)
                decoded = self.codec.decode(ack_wire(application))
                self.assertTrue(decoded.is_err)
                self.assertEqual(
                    "PRESENTATION_STRUCTURE_INVALID", decoded.error.code
                )

    def test_TC_PAYLOAD_001_ack_linkage_full_width_is_allowed(self):
        for linkage in (0, 7, 1023):
            application = pack_ack_application(linkage=linkage)
            encoded = self.codec.encode(application)
            self.assertTrue(encoded.is_ok, getattr(encoded, "error", None))
            self.assertEqual(application, self.codec.decode(encoded.value).value)

    def test_TC_PAYLOAD_001_ack_nonzero_padding_is_rejected(self):
        for padding in (1, 0x7F):
            with self.subTest(padding=padding):
                application = pack_ack_application(padding=padding)
                encoded = self.codec.encode(application)
                self.assertTrue(encoded.is_err)
                self.assertEqual("INVALID_REQUEST", encoded.error.code)
                decoded = self.codec.decode(ack_wire(application))
                self.assertTrue(decoded.is_err)
                self.assertEqual(
                    "PRESENTATION_STRUCTURE_INVALID", decoded.error.code
                )

    def test_TC_PAYLOAD_001_ack_wire_truncation_and_trailing_are_rejected(self):
        wire = ack_wire(pack_ack_application())
        for cut in (3, 4, 5):
            with self.subTest(cut=cut):
                result = self.codec.decode(wire[:cut])
                self.assertTrue(result.is_err)
                self.assertEqual("PRESENTATION_TRUNCATED", result.error.code)
        result = self.codec.decode(wire + b"\x00")
        self.assertTrue(result.is_err)
        self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)


class ScVdeCodecMetadataTest(unittest.TestCase):
    """`REG-01` 보조: codec metadata와 exact wire identity (`§6.4.2`, `§7.1`)."""

    def test_TC_PAYLOAD_001_command_and_ack_have_unique_refs_and_shared_vpfi(self):
        command = ScVdeRouteControlCommandCodec()
        ack = ScVdeRouteControlAckCodec()
        self.assertEqual("project.sc_vde.vpfi_ff7f.mf2.v1", command.codec_ref)
        self.assertEqual("project.sc_vde.vpfi_ff7f.mf3.v1", ack.codec_ref)
        self.assertNotEqual(command.codec_ref, ack.codec_ref)
        for codec in (command, ack):
            self.assertEqual(16383, codec.vpfi)
            self.assertTrue(codec.addressed_only)
        self.assertEqual(2, command.message_format)
        self.assertEqual(3, ack.message_format)
        # logical 16383의 정수 직렬화 `3f ff`가 아니라 exact Level 2 wire `ff 7f`다.
        self.assertEqual(b"\xff\x7f\x02", command.discriminator.prefix)
        self.assertEqual(b"\xff\x7f\x03", ack.discriminator.prefix)
        self.assertNotEqual((16383).to_bytes(2, "big"), command.discriminator.prefix[:2])

    def test_TC_PAYLOAD_001_source_ff01_and_logical_3fff_wires_are_not_decodable(self):
        command = ScVdeRouteControlCommandCodec()
        ack = ScVdeRouteControlAckCodec()
        application = pack_command_application(
            waypoints=((77_430_000, 21_060_000, 30, 60, 1),) * 2
        )
        for wire in (
            b"\xff\x01\x02" + application,
            b"\x3f\xff\x02" + application,
        ):
            result = command.decode(wire)
            self.assertTrue(result.is_err)
            self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)
        ack_application = pack_ack_application()
        for wire in (b"\xff\x01\x03" + ack_application, b"\x3f\xff\x03" + ack_application):
            result = ack.decode(wire)
            self.assertTrue(result.is_err)
            self.assertEqual("PRESENTATION_STRUCTURE_INVALID", result.error.code)


if __name__ == "__main__":
    unittest.main()
