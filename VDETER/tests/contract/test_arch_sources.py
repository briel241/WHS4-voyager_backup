"""TC-ARCH-009, 010, 013, 014, 015, 016: 식별자·Session·시간·난수원 계약.

요구사항: `ARCH-REQ-04`, `ARCH-REQ-05`, `ARCH-REQ-06`
"""

from __future__ import annotations

import unittest

from tests.support.fixtures import build_fake_endpoint

from vdeter.common.addressing import Addressed
from vdeter.common.contracts.mac import (
    SessionControlKind,
    SessionControlRequest,
    TransmissionPlan,
    TransmissionProcedure,
)
from vdeter.common.contracts.service import ServiceStartRequest, SubmitTransferRequest
from vdeter.common.errors import ErrorCode
from vdeter.common.exceptions import ClockMismatchError, InvalidValueError
from vdeter.common.identifiers import (
    DeterministicIdSource,
    PayloadFormat,
    StationId,
    Uuid4IdSource,
)
from vdeter.common.random_source import SeededRandomSource, SystemRandomSource
from vdeter.common.results import Ok
from vdeter.common.time import (
    ClockDomain,
    SyncStatus,
    SystemTimeSource,
    TestTimeSource,
    TimePoint,
    compare_time_points,
    elapsed_nanoseconds,
)
from vdeter.config.models import IdSourceKind, RandomSourceKind, TimeSourceKind
from vdeter.runtime.wiring import build_id_source, build_random_source, build_time_source


class TransferIdTest(unittest.TestCase):
    """TC-ARCH-009: TransferId 유일성, deterministic IdSource와 wire 비노출."""

    def test_TC_ARCH_009_transfer_ids_are_unique(self):
        source = Uuid4IdSource()
        values = {source.new_transfer_id().value for _ in range(256)}
        self.assertEqual(256, len(values))

    def test_TC_ARCH_009_deterministic_id_source_is_reproducible(self):
        first = DeterministicIdSource(20260819)
        second = DeterministicIdSource(20260819)
        self.assertEqual(
            [first.new_transfer_id().value for _ in range(5)],
            [second.new_transfer_id().value for _ in range(5)],
        )
        third = DeterministicIdSource(20260820)
        self.assertNotEqual(first.new_transfer_id().value, third.new_transfer_id().value)

    def test_TC_ARCH_009_local_ids_are_opaque_uuid4(self):
        source = DeterministicIdSource(1)
        value = source.new_transfer_id().value
        self.assertEqual(36, len(value))
        self.assertEqual(value, value.lower())
        with self.assertRaises(InvalidValueError):
            from vdeter.common.identifiers import TransferId

            TransferId("transfer-1")

    def test_TC_ARCH_009_transfer_id_never_reaches_wire_bytes(self):
        endpoint, dependencies = build_fake_endpoint()
        configuration = dependencies.configuration.endpoint
        endpoint.start(
            ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )
        )
        accepted = endpoint.submit(
            SubmitTransferRequest(
                destination=Addressed(StationId(222222222)),
                payload_format=PayloadFormat("test.stage0.opaque"),
                payload=b"payload",
            )
        )
        transfer_id = accepted.value.transfer_id.value.encode("ascii")
        scheduled = endpoint.radio.scheduled_bursts()
        self.assertTrue(scheduled)
        for request in scheduled:
            self.assertNotIn(transfer_id, request.samples.data)


class SessionHandleTest(unittest.TestCase):
    """TC-ARCH-010: SessionHandle 격리, 재시작 미복원과 wire Session ID 구분."""

    def setUp(self):
        self.endpoint, self.dependencies = build_fake_endpoint()
        self.mac = self.endpoint._mac

    def test_TC_ARCH_010_sessions_are_independent(self):
        first = self.mac.control_session(SessionControlRequest(kind=SessionControlKind.OPEN))
        second = self.mac.control_session(SessionControlRequest(kind=SessionControlKind.OPEN))
        self.assertTrue(first.is_ok and second.is_ok)
        self.assertNotEqual(first.value.session_handle, second.value.session_handle)
        self.assertEqual(2, self.mac.active_session_count())

        closed = self.mac.control_session(
            SessionControlRequest(
                kind=SessionControlKind.CLOSE, session_handle=first.value.session_handle
            )
        )
        self.assertTrue(closed.is_ok)
        self.assertFalse(closed.value.active)
        self.assertEqual(1, self.mac.active_session_count())

        kept = self.mac.control_session(
            SessionControlRequest(
                kind=SessionControlKind.KEEP, session_handle=second.value.session_handle
            )
        )
        self.assertTrue(kept.value.active)

    def test_TC_ARCH_010_unknown_session_is_rejected(self):
        from vdeter.common.identifiers import DeterministicIdSource

        unknown = DeterministicIdSource(99).new_session_handle()
        result = self.mac.control_session(
            SessionControlRequest(kind=SessionControlKind.CLOSE, session_handle=unknown)
        )
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.UNKNOWN_TRANSFER, result.error.code)

    def test_TC_ARCH_010_restart_does_not_restore_sessions(self):
        opened = self.mac.control_session(SessionControlRequest(kind=SessionControlKind.OPEN))
        self.assertTrue(opened.is_ok)
        self.assertEqual(1, self.mac.known_session_count())

        restarted, _dependencies = build_fake_endpoint()
        self.assertEqual(0, restarted._mac.known_session_count())

    def test_TC_ARCH_010_session_handle_is_not_a_wire_field(self):
        import dataclasses
        import typing

        from vdeter.common.contracts import link, mac, message
        from vdeter.common.contracts.message import MESSAGE_TYPE_TO_BODY_TYPE

        for module in (link, mac, message):
            for name in dir(module):
                candidate = getattr(module, name)
                if not dataclasses.is_dataclass(candidate) or not isinstance(candidate, type):
                    continue
                field_names = {field.name for field in dataclasses.fields(candidate)}
                self.assertNotIn("wire_session_id", field_names)
                if "session_id" in field_names:
                    self.assertIn(candidate, MESSAGE_TYPE_TO_BODY_TYPE.values())
                    self.assertNotIn("session_handle", field_names)
                    self.assertIs(int, typing.get_type_hints(candidate)["session_id"])


class TimeContractTest(unittest.TestCase):
    """TC-ARCH-013, 014: clock 비교 규칙과 domain 사용 계약."""

    def setUp(self):
        self.utc_a = TimePoint(10, 0, ClockDomain.UTC, "test-utc")
        self.utc_b = TimePoint(11, 500, ClockDomain.UTC, "test-utc")
        self.other_clock = TimePoint(11, 0, ClockDomain.UTC, "other-utc")
        self.monotonic = TimePoint(11, 0, ClockDomain.MONOTONIC, "test-monotonic")

    def test_TC_ARCH_013_same_clock_comparison_succeeds(self):
        self.assertTrue(self.utc_a < self.utc_b)
        self.assertEqual(
            1_000_000_500, self.utc_b.difference_nanoseconds(self.utc_a)
        )
        result = compare_time_points(self.utc_b, self.utc_a)
        self.assertEqual(Ok(1), result)

    def test_TC_ARCH_013_different_domain_comparison_is_rejected(self):
        with self.assertRaises(ClockMismatchError):
            _ = self.utc_a < self.monotonic
        result = compare_time_points(self.utc_a, self.monotonic)
        self.assertTrue(result.is_err)
        self.assertEqual(ErrorCode.CLOCK_DOMAIN_MISMATCH, result.error.code)

    def test_TC_ARCH_013_different_clock_id_comparison_is_rejected(self):
        with self.assertRaises(ClockMismatchError):
            self.utc_a.difference_nanoseconds(self.other_clock)
        result = elapsed_nanoseconds(self.utc_a, self.other_clock)
        self.assertTrue(result.is_err)

    def test_TC_ARCH_014_tdma_plan_requires_utc(self):
        from tests.support import dto_catalog

        with self.assertRaises(InvalidValueError):
            TransmissionPlan(
                transfer_id=dto_catalog.TRANSFER_ID,
                link_id=dto_catalog.LINK_ID,
                channel=dto_catalog.CHANNEL,
                transmit_at=self.monotonic,
                procedure=TransmissionProcedure.SHORT_DATA,
                route=dto_catalog.ROUTE,
            )

    def test_TC_ARCH_014_radio_requests_require_utc_plan(self):
        from tests.support import dto_catalog
        from vdeter.common.contracts.radio import RadioRxRequest, RadioTxRequest

        with self.assertRaises(InvalidValueError):
            RadioTxRequest(
                burst_id=dto_catalog.BURST_ID,
                channel=dto_catalog.CHANNEL,
                transmit_at=self.monotonic,
                samples=dto_catalog.SAMPLES,
            )
        with self.assertRaises(InvalidValueError):
            RadioRxRequest(
                channel=dto_catalog.CHANNEL,
                start_at=self.monotonic,
                duration_nanoseconds=10,
                sample_rate_hz=4000,
            )

    def test_TC_ARCH_014_timeout_uses_monotonic_domain(self):
        source = TestTimeSource()
        start = source.now_monotonic()
        source.advance(5_000_000)
        elapsed = elapsed_nanoseconds(source.now_monotonic(), start)
        self.assertTrue(elapsed.is_ok)
        self.assertEqual(5_000_000, elapsed.value)
        self.assertIs(ClockDomain.MONOTONIC, start.clock_domain)


class TimeSourceSelectionTest(unittest.TestCase):
    """TC-ARCH-015: System/Test TimeSource 선택과 deterministic 진행."""

    def test_TC_ARCH_015_system_time_source_provides_both_domains(self):
        source = SystemTimeSource()
        self.assertIs(ClockDomain.UTC, source.now_utc().clock_domain)
        self.assertIs(ClockDomain.MONOTONIC, source.now_monotonic().clock_domain)
        self.assertIs(SyncStatus.UNKNOWN, source.sync_status())

    def test_TC_ARCH_015_test_time_source_advances_deterministically(self):
        source = TestTimeSource(utc_nanoseconds=1_000, monotonic_nanoseconds=2_000)
        self.assertEqual(1_000, source.now_utc().total_nanoseconds())
        source.advance(1_500)
        self.assertEqual(2_500, source.now_utc().total_nanoseconds())
        self.assertEqual(3_500, source.now_monotonic().total_nanoseconds())
        with self.assertRaises(InvalidValueError):
            source.advance(-1)

    def test_TC_ARCH_015_test_time_source_reports_sync_state(self):
        source = TestTimeSource()
        self.assertIs(SyncStatus.SYNCHRONIZED, source.sync_status())
        source.set_sync_status(SyncStatus.UNSYNCHRONIZED)
        self.assertIs(SyncStatus.UNSYNCHRONIZED, source.sync_status())

    def test_TC_ARCH_015_selection_follows_configuration(self):
        from tests.support.fixtures import load_fake_configuration

        configuration = load_fake_configuration()
        self.assertIs(TimeSourceKind.TEST, configuration.endpoint.sources.time)
        self.assertIsInstance(build_time_source(configuration).value, TestTimeSource)

    def test_TC_ARCH_015_clock_mapping_requires_explicit_source(self):
        source = TestTimeSource()
        mapping = source.mapping(ClockDomain.UTC, ClockDomain.MONOTONIC)
        self.assertTrue(mapping.is_ok)
        converted = mapping.value.convert(source.now_utc())
        self.assertTrue(converted.is_ok)
        self.assertIs(ClockDomain.MONOTONIC, converted.value.clock_domain)
        unsupported = source.mapping(ClockDomain.UTC, ClockDomain.RADIO_HARDWARE)
        self.assertTrue(unsupported.is_err)


class RandomAndIdSourceSelectionTest(unittest.TestCase):
    """TC-ARCH-016: 운용/고정-seed RandomSource와 IdSource 선택."""

    def test_TC_ARCH_016_seeded_random_source_repeats(self):
        first = SeededRandomSource(20260819)
        second = SeededRandomSource(20260819)
        values = [first.next_int(0, 1000) for _ in range(10)]
        self.assertEqual(values, [second.next_int(0, 1000) for _ in range(10)])
        first.reset()
        self.assertEqual(values, [first.next_int(0, 1000) for _ in range(10)])

    def test_TC_ARCH_016_system_random_source_is_in_range(self):
        source = SystemRandomSource()
        for _ in range(50):
            value = source.next_int(4, 8)
            self.assertGreaterEqual(value, 4)
            self.assertLessEqual(value, 8)

    def test_TC_ARCH_016_invalid_bounds_are_rejected(self):
        for source in (SystemRandomSource(), SeededRandomSource(1)):
            with self.subTest(source=source.description()):
                with self.assertRaises(InvalidValueError):
                    source.next_int(5, 4)

    def test_TC_ARCH_016_selection_follows_configuration(self):
        from tests.support.fixtures import load_fake_configuration

        configuration = load_fake_configuration()
        self.assertIs(RandomSourceKind.SEEDED, configuration.endpoint.sources.random)
        self.assertIs(IdSourceKind.DETERMINISTIC, configuration.endpoint.sources.id)
        self.assertIsInstance(build_random_source(configuration).value, SeededRandomSource)
        self.assertIsInstance(build_id_source(configuration).value, DeterministicIdSource)

    def test_TC_ARCH_016_seed_is_recorded_in_description(self):
        self.assertIn("20260819", SeededRandomSource(20260819).description())
        self.assertIn("20260819", DeterministicIdSource(20260819).description())


if __name__ == "__main__":
    unittest.main()
