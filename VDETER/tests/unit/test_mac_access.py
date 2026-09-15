"""TC-MAC-005..007: Type 0, RAC 선택과 Short Data budget."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    RUN_START_UTC_NANOSECONDS,
    ROC_STATION_ID,
    build_stage4_software_group,
    deployment_profile,
)
from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.link import TransferKind, TransferRequirement
from vdeter.common.contracts.mac_execution import MacReasonCode, TimerPurpose
from vdeter.common.contracts.message import MediaAccessControlBody
from vdeter.common.errors import (
    Correlation,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
)
from vdeter.common.identifiers import DeterministicIdSource, PhysicalChannel, StationId
from vdeter.common.contracts.service import TransferState
from vdeter.common.random_source import SeededRandomSource
from vdeter.mac.access import (
    active_selection_interval,
    active_short_data_limit,
    blocks_new_admission,
    incremented_counter,
    lease_seconds,
    select_random_access_slot,
    short_data_budget_available,
    short_data_counter_key,
)
from vdeter.mac.models import (
    PendingCommandClass,
    PendingCommandRecord,
    TransferContext,
    TransferPhase,
    TransferRole,
    Stage4Limits,
    TimerRecord,
    Type0Lease,
)
from vdeter.mac.tbb import LOWER_CHANNEL, build_canonical_tbb_profile
from vdeter.mac.tdma import FRAME_NANOSECONDS, HEXSLOT_NANOSECONDS


def _lease(*, interval=235, limit=4, status=0):
    return Type0Lease(
        source_id_value=ROC_STATION_ID.value,
        media_access_priority_level=0,
        selection_interval=interval,
        short_data_limit=limit,
        system_status=status,
        started_monotonic_nanoseconds=10,
        lease_seconds=300,
        timer_id="lease",
    )


class _CountingRandom:
    def __init__(self, value=300):
        self.value = value
        self.calls = []

    def next_int(self, low, high):
        self.calls.append((low, high))
        return self.value

    def description(self):
        return "counting-random"


class Stage4AccessTest(unittest.TestCase):
    """TC-MAC-005..007의 결정적 접근 제어 불변식."""

    @classmethod
    def setUpClass(cls):
        cls.slot_map = build_canonical_tbb_profile(deployment_profile()).slot_map

    def test_TC_MAC_005_type0_common_lease_fields_and_status_cutover(self):
        first = SeededRandomSource(501)
        second = SeededRandomSource(501)
        values = [lease_seconds(first) for _ in range(8)]
        self.assertEqual(values, [lease_seconds(second) for _ in range(8)])
        self.assertTrue(all(240 <= value <= 480 for value in values))

        zero_si = _lease(interval=0, limit=7, status=20)
        selected = active_selection_interval(
            lease=zero_si,
            slot_map=self.slot_map,
            reference_utc_nanoseconds=RUN_START_UTC_NANOSECONDS,
        )
        self.assertEqual("TBB", selected.source)
        self.assertEqual(235 * HEXSLOT_NANOSECONDS, selected.duration_nanoseconds)
        self.assertEqual(7, active_short_data_limit(lease=zero_si, slot_map=self.slot_map))
        self.assertTrue(blocks_new_admission(zero_si))
        self.assertFalse(blocks_new_admission(_lease(status=10)))

    def test_TC_MAC_006_si_units_and_seeded_rac_choice(self):
        type0 = active_selection_interval(
            lease=_lease(interval=9),
            slot_map=self.slot_map,
            reference_utc_nanoseconds=RUN_START_UTC_NANOSECONDS,
        )
        self.assertEqual(("TYPE0", 9 * HEXSLOT_NANOSECONDS), (type0.source, type0.duration_nanoseconds))

        without_tbb_si = replace(self.slot_map, selection_interval_hexslots=0)
        official = active_selection_interval(
            lease=None,
            slot_map=without_tbb_si,
            reference_utc_nanoseconds=RUN_START_UTC_NANOSECONDS,
        )
        self.assertEqual("OFFICIAL_DEFAULT", official.source)
        self.assertTrue(official.is_official_default)
        self.assertNotEqual(235 * HEXSLOT_NANOSECONDS, official.duration_nanoseconds)

        first = select_random_access_slot(
            slot_map=self.slot_map,
            physical_channel=LOWER_CHANNEL,
            from_utc_nanoseconds=RUN_START_UTC_NANOSECONDS,
            interval=type0,
            random_source=SeededRandomSource(77),
            occupied_starts=frozenset(),
        )
        second = select_random_access_slot(
            slot_map=self.slot_map,
            physical_channel=LOWER_CHANNEL,
            from_utc_nanoseconds=RUN_START_UTC_NANOSECONDS,
            interval=type0,
            random_source=SeededRandomSource(77),
            occupied_starts=frozenset(),
        )
        self.assertTrue(first.is_ok and second.is_ok)
        self.assertEqual(first.value, second.value)

    def test_TC_MAC_007_budget_is_per_channel_and_tdma_frame(self):
        channel = PhysicalChannel(LOWER_CHANNEL.name)
        start = RUN_START_UTC_NANOSECONDS
        key = short_data_counter_key(channel, start)
        counter = incremented_counter((), key)
        self.assertTrue(short_data_budget_available(counters=(counter,), key=key, limit=2))
        counter = incremented_counter((counter,), key)
        self.assertFalse(short_data_budget_available(counters=(counter,), key=key, limit=2))
        next_frame_key = short_data_counter_key(channel, start + FRAME_NANOSECONDS)
        self.assertNotEqual(key, next_frame_key)
        self.assertTrue(
            short_data_budget_available(counters=(counter,), key=next_frame_key, limit=2)
        )

    def test_TC_MAC_005_duplicate_does_not_consume_random_or_extend_deadline(self):
        sources = []

        def factory(_index):
            source = _CountingRandom()
            sources.append(source)
            return source

        clock, endpoints = build_stage4_software_group(
            with_type0=False, random_source_factory=factory
        )
        mass_index, mass = next(
            (index, endpoint)
            for index, endpoint in enumerate(endpoints)
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        body = MediaAccessControlBody(
            source_id=ROC_STATION_ID,
            media_access_priority_level=0,
            random_access_selection_interval=17,
            short_data_message_limit=6,
            system_status=10,
        )
        commands = []
        leased, error = coordinator._start_type0_lease(
            coordinator.state, body, 123_000, "first", commands
        )
        self.assertIsNone(error)
        self.assertEqual([(240, 480)], sources[mass_index].calls)
        self.assertEqual(123_000 + 300_000_000_000, leased.type0_lease.deadline_monotonic_nanoseconds)
        self.assertEqual(1, len(commands))

        duplicate_commands = []
        duplicate, error = coordinator._start_type0_lease(
            leased, body, 999_000, "duplicate", duplicate_commands
        )
        self.assertIs(leased, duplicate)
        self.assertIsNone(error)
        self.assertEqual([(240, 480)], sources[mass_index].calls)
        self.assertEqual((), tuple(duplicate_commands))

        changed, error = coordinator._start_type0_lease(
            leased,
            replace(body, short_data_message_limit=7),
            999_000,
            "changed",
            [],
        )
        self.assertIs(leased, changed)
        self.assertEqual(
            MacReasonCode.TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED,
            error.error.details.get("reason_code"),
        )
        self.assertEqual([(240, 480)], sources[mass_index].calls)

    def test_TC_MAC_005_timer_capacity_rejects_lease_before_randomness(self):
        sources = []

        def factory(_index):
            source = _CountingRandom()
            sources.append(source)
            return source

        _clock, endpoints = build_stage4_software_group(
            with_type0=False, random_source_factory=factory
        )
        mass_index, mass = next(
            (index, endpoint)
            for index, endpoint in enumerate(endpoints)
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        coordinator = mass.stage4.mac.coordinator
        full = coordinator.state.evolve(
            timers=tuple(
                TimerRecord(
                    timer_id=f"occupied-{index}",
                    purpose=TimerPurpose.FRAME_BOUNDARY,
                    deadline_monotonic_nanoseconds=index + 1,
                )
                for index in range(Stage4Limits.ACTIVE_TIMERS)
            )
        )
        body = MediaAccessControlBody(
            source_id=ROC_STATION_ID,
            media_access_priority_level=0,
            random_access_selection_interval=17,
            short_data_message_limit=6,
            system_status=10,
        )
        commands = []
        rejected, error = coordinator._start_type0_lease(
            full, body, 123_000, "capacity", commands
        )
        self.assertIs(full, rejected)
        self.assertEqual(MacReasonCode.RESOURCE_BUSY, error.error.details.get("reason_code"))
        self.assertEqual([], sources[mass_index].calls)
        self.assertEqual([], commands)

    def test_TC_MAC_005_receive_rejections_and_lease_expiry_are_observable(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id != ROC_STATION_ID
        )
        body = MediaAccessControlBody(
            source_id=ROC_STATION_ID,
            media_access_priority_level=0,
            random_access_selection_interval=9,
            short_data_message_limit=7,
            system_status=20,
        )
        errors = []
        leased = coordinator._receive_type0(
            coordinator.state, body, "receive", [], errors=errors
        )
        self.assertEqual([], errors)
        self.assertTrue(blocks_new_admission(leased.type0_lease))

        duplicate_errors = []
        duplicate = coordinator._receive_type0(
            leased,
            replace(body, system_status=30),
            "changed",
            [],
            errors=duplicate_errors,
        )
        self.assertIs(leased, duplicate)
        self.assertEqual(
            MacReasonCode.TYPE0_OVERRIDE_CHANGE_NOT_SUPPORTED,
            duplicate_errors[0].error.details.get("reason_code"),
        )

        wrong_source_errors = []
        wrong_source = coordinator._receive_type0(
            leased,
            replace(body, source_id=StationId(ROC_STATION_ID.value + 1)),
            "wrong-source",
            [],
            errors=wrong_source_errors,
        )
        self.assertIs(leased, wrong_source)
        self.assertEqual(
            MacReasonCode.UNEXPECTED_CONTROL_STATION,
            wrong_source_errors[0].error.details.get("reason_code"),
        )

        expired = coordinator._on_wakeup_fired(
            leased,
            SimpleNamespace(
                event_id="lease-expired",
                payload=SimpleNamespace(
                    timer_id=leased.type0_lease.timer_id,
                    purpose=TimerPurpose.LEASE_EXPIRY,
                ),
            ),
            [],
        )
        self.assertIsNone(expired.type0_lease)
        self.assertEqual(
            4,
            active_short_data_limit(lease=None, slot_map=self.slot_map),
        )

    def test_TC_MAC_005_roc_lease_starts_only_on_phy_completion_and_failure_releases_attempt(self):
        clock, endpoints = build_stage4_software_group(with_type0=True)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        self.assertIsNone(coordinator.state.type0_lease)
        completed = coordinator._on_type0_transmitted(
            coordinator.state, 987_654_321, "completed", []
        )
        self.assertEqual(
            987_654_321,
            completed.type0_lease.started_monotonic_nanoseconds,
        )
        self.assertTrue(completed.type0_transmitted)

        command_id = "type0-phy"
        packet_id = DeterministicIdSource(509).new_packet_id()
        failed_state = coordinator.state.evolve(
            type0_tx_command_id=command_id,
            pending_commands=(
                PendingCommandRecord(
                    command_id=command_id,
                    command_class=PendingCommandClass.NON_CLEANUP_NON_RX,
                    payload_kind="InvokePhyCommand",
                ),
            ),
        )
        failure = InternalProtocolError(
            layer=Layer.PHY,
            code=ErrorCode.PHY_SYNCHRONIZATION_FAILED,
            category=ErrorCategory.DEVICE,
            message="injected Type 0 PHY failure",
        )
        after_failure = coordinator._on_phy_tx_failed(
            failed_state,
            SimpleNamespace(
                event_id="failed",
                causation_command_id=command_id,
                payload=SimpleNamespace(
                    command_id=command_id, packet_id=packet_id, error=failure
                ),
                correlation=Correlation(packet_id=packet_id),
            ),
            [],
        )
        self.assertIsNone(after_failure.type0_lease)
        self.assertIsNone(after_failure.type0_tx_command_id)
        self.assertIsNone(after_failure.pending_command(command_id))

    def test_TC_MAC_005_out_of_service_cutover_blocks_only_new_admission_until_expiry(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        canonical = coordinator.profile.canonical_tbb
        active_state, error = coordinator._activate_canonical_tbb(
            coordinator.state,
            fingerprint=canonical.canonical_tbb_fingerprint,
            bulletin_board_version=canonical.bulletin_board_version,
            source_id=canonical.source_id,
            control_station_id=canonical.control_station_id,
        )
        self.assertIsNone(error)
        clock.advance(
            active_state.active_tbb.activation_nanoseconds
            - clock.now_utc().total_nanoseconds()
        )
        ids = DeterministicIdSource(510)
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        existing_id = ids.new_transfer_id()
        existing = TransferContext(
            transfer_id=existing_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.AWAITING_RESOURCE,
            route=route,
            ack_required=True,
            presentation_size=1000,
            public_state=TransferState.TRANSMITTING,
        )
        for status in (20, 30):
            leased = active_state.with_transfer(existing).evolve(
                type0_lease=_lease(status=status)
            )
            coordinator._repository.commit(leased)
            new_requirement = TransferRequirement(
                transfer_id=ids.new_transfer_id(),
                route=route,
                ack_required=False,
                presentation_size=8,
                candidate=TransferKind.SHORT_DATA,
            )
            blocked = coordinator.submit_requirement(new_requirement, f"blocked-{status}")
            self.assertTrue(blocked.result.is_err)
            self.assertEqual(
                MacReasonCode.CONTROL_STATION_OUT_OF_SERVICE,
                blocked.result.error.details.get("reason_code"),
            )
            self.assertIs(
                TransferPhase.AWAITING_RESOURCE,
                blocked.next_state.transfer(existing_id).phase,
            )

        reopened = coordinator.state.evolve(type0_lease=None)
        coordinator._repository.commit(reopened)
        accepted = coordinator.submit_requirement(
            TransferRequirement(
                transfer_id=ids.new_transfer_id(),
                route=route,
                ack_required=False,
                presentation_size=8,
                candidate=TransferKind.SHORT_DATA,
            ),
            "after-expiry",
        )
        self.assertTrue(accepted.result.is_ok, getattr(accepted.result, "error", None))


if __name__ == "__main__":
    unittest.main()
