"""TC-ARCH-005, 006, 011, 012, 023: 공개 DTO 불변성·직렬화·경계 검사.

요구사항: `ARCH-REQ-01`, `ARCH-REQ-03`, `ARCH-REQ-06`, `ARCH-REQ-07`
"""

from __future__ import annotations

import dataclasses
import json
import typing
import unittest

from tests.support import dto_catalog

from vdeter.common.addressing import Addressed, Broadcast, NodeSource, OriginalSource
from vdeter.common.contracts import radio as radio_contracts
from vdeter.common.contracts.link import LinkBuildRequest
from vdeter.common.contracts.radio import SAMPLE_FORMAT_TEST_IQ_INT16_LE, SampleBlock
from vdeter.common.contracts.service import ReceivedApplicationData, SubmitTransferRequest
from vdeter.common.exceptions import InvalidValueError, SerializationError
from vdeter.common.identifiers import (
    LinkId,
    PayloadFormat,
    SequentialId,
    StationId,
    Vpfi,
)
from vdeter.common.serialization import from_reference_json, to_reference_json
from vdeter.common.time import ClockDomain, TimePoint

FORBIDDEN_FIELD_NAME_PARTS = (
    "logger",
    "callback",
    "queue",
    "thread",
    "socket",
    "handle_object",
    "device_object",
    "numpy",
    "pmt",
)


class PublicDtoImmutabilityTest(unittest.TestCase):
    """TC-ARCH-005: frozen/slots, portable field와 mutation 거부."""

    def test_TC_ARCH_005_all_public_dtos_are_frozen_and_slotted(self):
        for name, instance in dto_catalog.public_dto_samples():
            with self.subTest(dto=name):
                self.assertTrue(dataclasses.is_dataclass(instance))
                params = instance.__dataclass_params__
                self.assertTrue(params.frozen, f"{name}이 frozen이 아닙니다")
                self.assertFalse(
                    hasattr(instance, "__dict__"), f"{name}이 slots를 사용하지 않습니다"
                )

    def test_TC_ARCH_005_mutation_is_rejected(self):
        for name, instance in dto_catalog.public_dto_samples():
            field_names = [field.name for field in dataclasses.fields(instance)]
            if not field_names:
                continue
            with self.subTest(dto=name):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(instance, field_names[0], None)

    def test_TC_ARCH_005_field_names_have_no_runtime_objects(self):
        for name, instance in dto_catalog.public_dto_samples():
            for field in dataclasses.fields(instance):
                lowered = field.name.lower()
                for token in FORBIDDEN_FIELD_NAME_PARTS:
                    with self.subTest(dto=name, field=field.name):
                        if token == "queue" and name in {
                            "RadioResourceLimits", "RadioQueueStatus", "RadioStatusSnapshot"
                        }:
                            continue
                        self.assertNotIn(token, lowered)

    def test_TC_ARCH_005_no_float_fields_in_public_dtos(self):
        for name, instance in dto_catalog.public_dto_samples():
            for field in dataclasses.fields(instance):
                value = getattr(instance, field.name)
                with self.subTest(dto=name, field=field.name):
                    self.assertNotIsInstance(value, float)

    def test_TC_ARCH_005_sample_block_length_contract(self):
        block = SampleBlock(
            sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
            sample_rate_hz=4000,
            sample_count=3,
            data=bytes(12),
            first_sample_at=dto_catalog.UTC,
        )
        self.assertEqual(3, block.sample_count)
        with self.assertRaises(InvalidValueError):
            SampleBlock(
                sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
                sample_rate_hz=4000,
                sample_count=3,
                data=bytes(10),
                first_sample_at=dto_catalog.UTC,
            )

    def test_TC_ARCH_005_sample_block_rejects_unregistered_format(self):
        with self.assertRaises(InvalidValueError):
            SampleBlock(
                sample_format="operational-iq-complex64",
                sample_rate_hz=4000,
                sample_count=1,
                data=bytes(8),
                first_sample_at=dto_catalog.UTC,
            )

    def test_TC_ARCH_005_sample_format_registry_preserves_test_and_adds_operational(self):
        self.assertEqual(
            {SAMPLE_FORMAT_TEST_IQ_INT16_LE, radio_contracts.SAMPLE_FORMAT_CF32LE},
            set(radio_contracts.SAMPLE_FORMAT_BYTES_PER_SAMPLE),
        )
        self.assertIn(
            SAMPLE_FORMAT_TEST_IQ_INT16_LE, radio_contracts.TEST_SCOPE_SAMPLE_FORMATS
        )

    def test_TC_ARCH_005_link_build_retransmission_number_range(self):
        for value in (-1, 255):
            with self.subTest(value=value):
                with self.assertRaises(InvalidValueError):
                    LinkBuildRequest(
                        plan=dto_catalog.TRANSMISSION_PLAN,
                        presentation_data=b"payload",
                        retransmission_number=value,
                    )


class ReferenceJsonTest(unittest.TestCase):
    """TC-ARCH-006: reference JSON round-trip과 unknown field 거부."""

    def test_TC_ARCH_006_all_public_dtos_round_trip(self):
        for name, instance in dto_catalog.public_dto_samples():
            with self.subTest(dto=name):
                encoded = to_reference_json(instance)
                json.dumps(encoded, ensure_ascii=False)  # JSON 표현 가능해야 한다
                restored = from_reference_json(type(instance), encoded)
                self.assertEqual(instance, restored)

    def test_TC_ARCH_006_bytes_use_explicit_base64_envelope(self):
        encoded = to_reference_json(dto_catalog.SAMPLES)
        self.assertEqual("base64", encoded["data"]["encoding"])
        self.assertIsInstance(encoded["data"]["data"], str)

    def test_TC_ARCH_006_time_point_keeps_all_four_fields(self):
        encoded = to_reference_json(dto_catalog.UTC)
        self.assertEqual(
            {"seconds", "nanoseconds", "clock_domain", "clock_id"}, set(encoded)
        )

    def test_TC_ARCH_006_unknown_field_is_rejected(self):
        encoded = to_reference_json(dto_catalog.SAMPLES)
        encoded["unexpected_field"] = 1
        with self.assertRaises(SerializationError):
            from_reference_json(SampleBlock, encoded)

    def test_TC_ARCH_006_enum_is_encoded_as_string(self):
        encoded = to_reference_json(dto_catalog.SAMPLES)
        self.assertEqual("CONTIGUOUS", encoded["continuity"])

    def test_TC_ARCH_006_non_portable_value_is_rejected(self):
        from vdeter.common.serialization import encode_value

        with self.assertRaises(SerializationError):
            encode_value(object())
        with self.assertRaises(SerializationError):
            encode_value(1.5)


class SequentialIdTest(unittest.TestCase):
    """TC-ARCH-011: SequentialId 범위와 wire/local ID 구분."""

    def test_TC_ARCH_011_boundaries_are_accepted(self):
        self.assertEqual(0, SequentialId(0).value)
        self.assertEqual(16383, SequentialId(16383).value)

    def test_TC_ARCH_011_out_of_range_is_rejected(self):
        for value in (-1, 16384):
            with self.subTest(value=value):
                with self.assertRaises(InvalidValueError):
                    SequentialId(value)

    def test_TC_ARCH_011_is_serializable(self):
        requirement = dict(dto_catalog.public_dto_samples())["TransferRequirement"]
        encoded = to_reference_json(requirement)
        self.assertEqual(16383, encoded["sequential_id"]["value"])

    def test_TC_ARCH_011_wire_session_id_is_not_a_local_identifier(self):
        from vdeter.common import errors, identifiers

        self.assertFalse(hasattr(identifiers, "SessionId"))
        self.assertFalse(hasattr(identifiers, "WireSessionId"))
        correlation_fields = {
            field.name for field in dataclasses.fields(errors.Correlation)
        }
        self.assertNotIn("session_id", correlation_fields)
        self.assertNotIn("wire_session_id", correlation_fields)


class AddressingTest(unittest.TestCase):
    """TC-ARCH-012: addressed/broadcast와 original/node 주소 역할."""

    def test_TC_ARCH_012_addressed_and_broadcast_are_distinct(self):
        addressed = Addressed(StationId(123456789))
        broadcast = Broadcast()
        self.assertFalse(addressed.is_broadcast)
        self.assertTrue(broadcast.is_broadcast)
        self.assertNotEqual(addressed, broadcast)

    def test_TC_ARCH_012_broadcast_cannot_carry_station_id(self):
        from vdeter.common.addressing import Address, AddressKind

        with self.assertRaises(InvalidValueError):
            Address(kind=AddressKind.BROADCAST, station_id=StationId(1))
        with self.assertRaises(InvalidValueError):
            Address(kind=AddressKind.ADDRESSED, station_id=None)

    def test_TC_ARCH_012_zero_is_not_an_addressed_station(self):
        with self.assertRaises(InvalidValueError):
            Addressed(StationId(0))

    def test_TC_ARCH_012_original_and_node_roles_are_separate_types(self):
        station = StationId(123456789)
        self.assertNotEqual(OriginalSource(station), NodeSource(station))
        self.assertIsNot(type(OriginalSource(station)), type(NodeSource(station)))

    def test_TC_ARCH_012_station_id_32bit_boundaries(self):
        self.assertEqual(0, StationId(0).value)
        self.assertEqual((1 << 32) - 1, StationId((1 << 32) - 1).value)
        for value in (-1, 1 << 32):
            with self.subTest(value=value):
                with self.assertRaises(InvalidValueError):
                    StationId(value)

    def test_TC_ARCH_012_other_identifier_ranges(self):
        self.assertEqual(63, LinkId(63).value)
        with self.assertRaises(InvalidValueError):
            LinkId(64)
        self.assertEqual(268435455, Vpfi(268435455).value)
        with self.assertRaises(InvalidValueError):
            Vpfi(268435456)
        with self.assertRaises(InvalidValueError):
            PayloadFormat("")

    def test_TC_ARCH_012_broadcast_cannot_request_ack(self):
        with self.assertRaises(InvalidValueError):
            SubmitTransferRequest(
                destination=Broadcast(),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"x",
                ack_required=True,
            )


class RadioBoundaryTest(unittest.TestCase):
    """TC-ARCH-023: Radio DTO에 Packet/Message/장치 객체가 없다."""

    RADIO_DTOS = (
        "SampleBlock",
        "RadioChannelSetting",
        "RadioConfiguration",
        "RadioConfigureRequest",
        "RadioConfigurationApplied",
        "RadioStartRequest",
        "RadioStarted",
        "RadioStopRequest",
        "RadioStopped",
        "RadioTxRequest",
        "RadioTxAccepted",
        "RadioTxCompleted",
        "RadioRxRequest",
        "RadioRxAccepted",
        "RadioSamplesReceived",
        "RadioStatusRequest",
        "RadioStatusSnapshot",
    )

    FORBIDDEN_FIELDS = (
        "packet",
        "message",
        "link_id",
        "crc",
        "fec",
        "payload",
        "session_handle",
        "sequential_id",
        "transfer_id",
        "device_handle",
        "device_object",
        "flowgraph",
        "uhd",
        "pmt",
    )

    #: `device_state`, `device_selector`처럼 portable한 장비 metadata는 허용한다.
    #: 금지 대상은 장치 *객체*이며 type 검사(`_flatten_annotation`)가 함께 판정한다.

    def test_TC_ARCH_023_radio_dtos_have_no_protocol_fields(self):
        for name in self.RADIO_DTOS:
            dto = getattr(radio_contracts, name)
            for field in dataclasses.fields(dto):
                lowered = field.name.lower()
                for token in self.FORBIDDEN_FIELDS:
                    with self.subTest(dto=name, field=field.name, token=token):
                        self.assertNotIn(token, lowered)

    def test_TC_ARCH_023_radio_dto_field_types_are_portable(self):
        allowed_roots = {
            "vdeter.common.contracts.radio",
            "vdeter.common.identifiers",
            "vdeter.common.time",
            "vdeter.common.errors",
            "builtins",
            "enum",
        }
        for name in self.RADIO_DTOS:
            dto = getattr(radio_contracts, name)
            hints = typing.get_type_hints(dto)
            for field_name, annotation in hints.items():
                for candidate in _flatten_annotation(annotation):
                    module = getattr(candidate, "__module__", "builtins")
                    with self.subTest(dto=name, field=field_name, type=str(candidate)):
                        self.assertIn(module, allowed_roots)

    def test_TC_ARCH_023_radio_contracts_do_not_import_layer_modules(self):
        import vdeter.common.contracts.radio as module

        source_names = set(dir(module))
        for forbidden in ("BuiltPacket", "DecodedMessages", "TransmissionPlan"):
            self.assertNotIn(forbidden, source_names)


def _flatten_annotation(annotation):
    origin = typing.get_origin(annotation)
    if origin is None:
        yield annotation
        return
    args = typing.get_args(annotation)
    if not args:
        yield origin
        return
    for arg in args:
        if arg is type(None):
            continue
        yield from _flatten_annotation(arg)


class TimePointBoundaryTest(unittest.TestCase):
    """TC-ARCH-005 보조: TimePoint 값 제약."""

    def test_TC_ARCH_005_time_point_rejects_invalid_nanoseconds(self):
        with self.assertRaises(InvalidValueError):
            TimePoint(0, 1_000_000_000, ClockDomain.UTC, "test-utc")
        with self.assertRaises(InvalidValueError):
            TimePoint(0, -1, ClockDomain.UTC, "test-utc")

    def test_TC_ARCH_005_time_helpers_reject_floating_point(self):
        from vdeter.common.time import TestTimeSource

        with self.assertRaises(InvalidValueError):
            TimePoint.from_nanoseconds(1.5, ClockDomain.UTC, "test-utc")
        with self.assertRaises(InvalidValueError):
            TimePoint(0, 0, ClockDomain.UTC, "test-utc").shifted(1.5)
        with self.assertRaises(InvalidValueError):
            TestTimeSource(utc_nanoseconds=1.5)

    def test_TC_ARCH_005_public_dto_constructors_reject_runtime_objects(self):
        from vdeter.common.contracts.radio import RadioStatusRequest
        from vdeter.common.contracts.service import ServiceStopRequest

        with self.assertRaises(InvalidValueError):
            ServiceStopRequest(reason=object())
        with self.assertRaises(InvalidValueError):
            RadioStatusRequest(channel=object())

    def test_TC_ARCH_005_received_application_data_has_no_internal_ids(self):
        field_names = {field.name for field in dataclasses.fields(ReceivedApplicationData)}
        for forbidden in (
            "session_handle",
            "sequential_id",
            "packet_id",
            "burst_id",
            "link_id",
            "vpfi",
        ):
            self.assertNotIn(forbidden, field_names)


if __name__ == "__main__":
    unittest.main()
