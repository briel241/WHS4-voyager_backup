"""TC-MAC-008..010: Type 90/4, registry와 LC use map."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

from tests.support.stage4 import (
    MASS_A_STATION_ID,
    MASS_B_STATION_ID,
    ROC_STATION_ID,
    Type0OverrideProfile,
    build_stage4_software_group,
    deployment_profile,
    drive_stage4_group,
    start_stage4_group,
)
from vdeter.common.addressing import Addressed, OriginalDestination, OriginalSource, RouteAddresses
from vdeter.common.contracts.link import EncodedPacket, LinkAnalyzeRequest, TransferKind
from vdeter.common.contracts.mac_execution import (
    CancelWakeupCommand,
    MacFailureReason,
    MacReasonCode,
    TimerPurpose,
)
from vdeter.common.contracts.service import ServiceStartRequest, TransferState
from vdeter.common.errors import (
    Correlation,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
)
from vdeter.common.identifiers import DeterministicIdSource, LinkId
from vdeter.common.contracts.message import DataStartFragmentBody, DecodedMessage
from vdeter.common.results import Ok
from vdeter.mac.models import (
    LcAllocation,
    LcState,
    OutstandingResourceRequest,
    PendingCommandClass,
    PendingCommandRecord,
    Stage4Limits,
    TimerRecord,
    TransferContext,
    TransferPhase,
    TransferRole,
)
from vdeter.mac.resources import (
    Direction,
    allocation_from_type4,
    allocation_state_at,
    build_allocation_body,
    build_rejection_body,
    build_resource_request_body,
    duplex_pair,
    reissue_frame_delay,
    select_free_duplex_pair,
    validate_terminal_capabilities,
)
from vdeter.mac.tbb import LOWER_CHANNEL, UPPER_CHANNEL, build_canonical_tbb_profile
from vdeter.mac.tdma import (
    FRAME_NANOSECONDS,
    HEXSLOT_NANOSECONDS,
    channel_offset_nanoseconds,
    frame_ordinal_at_nanoseconds,
    frame_ordinal_start_nanoseconds,
    rx_windows_for_frame,
    samples_for_duration,
    utc_point,
)


class Stage4ResourceTest(unittest.TestCase):
    """TC-MAC-008..010의 자원 할당과 실제 wire 왕복."""

    @classmethod
    def setUpClass(cls):
        cls.slot_map = build_canonical_tbb_profile(deployment_profile()).slot_map

    def test_TC_MAC_008_canonical_pair_allocation_and_single_resync(self):
        self.assertTrue(validate_terminal_capabilities(2).is_ok)
        unsupported_capability = validate_terminal_capabilities(0x12)
        self.assertTrue(unsupported_capability.is_err)
        self.assertIs(
            MacReasonCode.UNSUPPORTED_TERMINAL_CAPABILITY,
            unsupported_capability.error.details.get("reason_code"),
        )
        pair = select_free_duplex_pair(
            slot_map=self.slot_map,
            direction=Direction.SHIP_TO_ROC,
            allocations=(),
            frame_ordinal=100,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        self.assertEqual((1, 2, 16), (pair.tdma_channel, pair.logical_channel_tx, pair.logical_channel_rx))
        body = build_allocation_body(
            source_id=ROC_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            link_id=LinkId(11),
        )
        allocation = allocation_from_type4(
            body=body,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            transmit_frame_ordinal=100,
            tdma_channel=pair.tdma_channel,
            issued_by_local=False,
        )
        self.assertIs(LcState.RESERVED, allocation_state_at(allocation, 100))
        self.assertIs(LcState.ACTIVE, allocation_state_at(allocation, 101))
        self.assertEqual((3, 105, False), reissue_frame_delay(activation_frame=105, transmit_frame=102, resync_used=False))
        self.assertEqual((1, 106, True), reissue_frame_delay(activation_frame=105, transmit_frame=105, resync_used=False))
        self.assertIsNone(reissue_frame_delay(activation_frame=105, transmit_frame=106, resync_used=True))
        rejection = build_rejection_body(
            source_id=ROC_STATION_ID, destination=Addressed(MASS_A_STATION_ID)
        )
        self.assertEqual((255, 255, 11, 1, 0, 0), (
            rejection.logical_channel_tx, rejection.logical_channel_rx,
            rejection.link_id.value, rejection.tdma_frame_delay,
            rejection.session_id, rejection.cqi,
        ))

    def test_TC_MAC_009_duplex_directions_use_link11_fixture(self):
        ship = duplex_pair(
            self.slot_map, Direction.SHIP_TO_ROC, 5, lower=LOWER_CHANNEL, upper=UPPER_CHANNEL
        )
        roc = duplex_pair(
            self.slot_map, Direction.ROC_TO_SHIP, 5, lower=LOWER_CHANNEL, upper=UPPER_CHANNEL
        )
        self.assertEqual((10, 24, LOWER_CHANNEL, UPPER_CHANNEL), (
            ship.logical_channel_tx, ship.logical_channel_rx,
            ship.tx_physical_channel, ship.rx_physical_channel,
        ))
        self.assertEqual((23, 11, UPPER_CHANNEL, LOWER_CHANNEL), (
            roc.logical_channel_tx, roc.logical_channel_rx,
            roc.tx_physical_channel, roc.rx_physical_channel,
        ))

    def test_TC_MAC_008_epoch_closes_two_candidates_in_canonical_station_order(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=8, short_data_limit=4, system_status=0
            )
        )
        start_stage4_group(clock, endpoints)
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        coordinator = roc.stage4.mac.coordinator
        decoded = SimpleNamespace(received_at=clock.now_utc())
        state = coordinator.state
        for station_id in (MASS_B_STATION_ID, MASS_A_STATION_ID):
            body = build_resource_request_body(
                original_source=station_id,
                node_source=station_id,
                node_destination=Addressed(ROC_STATION_ID),
                original_destination=Addressed(ROC_STATION_ID),
            )
            state = coordinator._receive_type90(
                state,
                body,
                decoded=decoded,
                channel=LOWER_CHANNEL,
                causation_event_id="two-candidate-epoch",
            )
        epoch = state.request_epochs[-1]
        self.assertEqual(
            (MASS_B_STATION_ID.value, MASS_A_STATION_ID.value),
            tuple(item.node_source_id_value for item in epoch.candidates),
        )

        commands = []
        closed = coordinator._close_epoch(
            state, epoch, "close-two-candidate-epoch", commands
        )
        self.assertTrue(closed.request_epochs[-1].closed)
        self.assertEqual(1, len(closed.request_chains))
        if commands:
            request = commands[-1].payload.request
        else:
            request = closed.tx_intents[-1].encode_request
        destinations = tuple(
            message.body.destination.station_id.value for message in request.messages
        )
        self.assertEqual(
            (MASS_A_STATION_ID.value, MASS_B_STATION_ID.value), destinations
        )
        self.assertEqual((255, 255), (
            request.messages[1].body.logical_channel_tx,
            request.messages[1].body.logical_channel_rx,
        ))

    def test_TC_MAC_010_mass_type90_and_roc_type4_cross_the_software_radio(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=8, short_data_limit=4, system_status=0
            )
        )
        for endpoint in endpoints:
            configuration = endpoint.dependencies.configuration.endpoint
            self.assertTrue(endpoint.start(ServiceStartRequest(
                local_station_id=configuration.station_id, role=configuration.role
            )).is_ok)
            self.assertTrue(endpoint.stage4.mac.begin_running().is_ok)
            endpoint.stage4.pump()
        drive_stage4_group(clock, endpoints, 120)
        mass = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID)
        roc = next(e for e in endpoints if e.dependencies.configuration.endpoint.station_id == ROC_STATION_ID)
        transfer_id = mass.dependencies.id_source.new_transfer_id()
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        analyzed = mass._link.analyze_transfer(LinkAnalyzeRequest(
            transfer_id=transfer_id,
            route=route,
            ack_required=True,
            presentation_data=b"D" * 1000,
        ))
        self.assertTrue(analyzed.is_ok, getattr(analyzed, "error", None))
        self.assertIs(TransferKind.DATA_SESSION, analyzed.value.candidate)
        self.assertTrue(mass._mac.submit_requirement(analyzed.value).is_ok)
        mass.stage4.pump()
        drive_stage4_group(clock, endpoints, 50)

        context = mass.stage4.mac.state.transfer(transfer_id)
        self.assertIsNotNone(context)
        self.assertEqual((2, 16), (context.logical_channel_tx, context.logical_channel_rx))
        self.assertIsNone(mass.stage4.mac.state.outstanding_request)
        self.assertEqual(1, len(roc.stage4.mac.state.request_chains))
        self.assertEqual(1, len(roc.stage4.mac.state.lc_allocations))
        self.assertTrue(any(epoch.closed and len(epoch.candidates) == 1 for epoch in roc.stage4.mac.state.request_epochs))

    def test_TC_MAC_008_type4_batch_link_and_phy_failures_roll_back_chain_and_lc_atomically(self):
        clock, endpoints = build_stage4_software_group(
            type0_override=Type0OverrideProfile(
                selection_interval=8, short_data_limit=4, system_status=0
            )
        )
        start_stage4_group(clock, endpoints)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        coordinator._profile = replace(
            coordinator.profile, schedule_horizon_nanoseconds=0
        )
        decoded = SimpleNamespace(received_at=clock.now_utc())
        candidate_state = coordinator.state
        for station_id in (MASS_B_STATION_ID, MASS_A_STATION_ID):
            body = build_resource_request_body(
                original_source=station_id,
                node_source=station_id,
                node_destination=Addressed(ROC_STATION_ID),
                original_destination=Addressed(ROC_STATION_ID),
            )
            candidate_state = coordinator._receive_type90(
                candidate_state,
                body,
                decoded=decoded,
                channel=LOWER_CHANNEL,
                causation_event_id="atomic-candidates",
            )
        epoch = candidate_state.request_epochs[-1]
        injected = InternalProtocolError(
            layer=Layer.LINK,
            code=ErrorCode.LINK_TRANSFER_PROGRESS_INVALID,
            category=ErrorCategory.INTERNAL,
            message="injected Type 4 batch failure",
        )

        def prepared_encode():
            commands = []
            closed = coordinator._close_epoch(
                candidate_state, epoch, "atomic-close", commands
            )
            self.assertEqual(1, len(commands))
            self.assertEqual(1, len(closed.request_chains))
            self.assertEqual(
                1,
                sum(1 for item in closed.lc_allocations if item.issued_by_local),
            )
            plan = coordinator._finish(closed, commands, "atomic-close", Ok(closed))
            return plan.next_state, commands[0]

        link_state, link_command = prepared_encode()
        after_link_failure = coordinator._on_dependency_failed(
            link_state,
            SimpleNamespace(
                event_id="link-failed",
                causation_command_id=link_command.command_id,
                correlation=Correlation(),
                payload=SimpleNamespace(
                    command_id=link_command.command_id, error=injected
                ),
            ),
            [],
        )
        self.assertEqual((), after_link_failure.request_chains)
        self.assertFalse(
            any(item.issued_by_local for item in after_link_failure.lc_allocations)
        )
        self.assertFalse(
            any(
                item[0] == ("RESOURCE_BATCH", link_command.command_id)
                for item in after_link_failure.delivery_pending
            )
        )

        phy_source_state, encode_command = prepared_encode()
        record = phy_source_state.pending_command(encode_command.command_id)
        ids = DeterministicIdSource(880)
        packet = EncodedPacket(
            packet_id=ids.new_packet_id(),
            link_id=LinkId(11),
            packet=b"type4-batch" + bytes(4),
            message_types=(4, 4),
            padding_size=0,
            crc=bytes(4),
        )
        phy_commands = []
        encoded_state = coordinator._on_control_packet_encoded(
            phy_source_state.without_pending_command(encode_command.command_id),
            record,
            packet,
            SimpleNamespace(event_id="encoded"),
            phy_commands,
        )
        self.assertEqual(1, len(phy_commands))
        phy_plan = coordinator._finish(
            encoded_state, phy_commands, "encoded", Ok(encoded_state)
        )
        phy_command = phy_commands[0]
        after_phy_failure = coordinator._on_phy_tx_failed(
            phy_plan.next_state,
            SimpleNamespace(
                event_id="phy-failed",
                causation_command_id=phy_command.command_id,
                correlation=Correlation(packet_id=packet.packet_id),
                payload=SimpleNamespace(
                    command_id=phy_command.command_id,
                    packet_id=packet.packet_id,
                    error=injected,
                ),
            ),
            [],
        )
        self.assertEqual((), after_phy_failure.request_chains)
        self.assertFalse(
            any(item.issued_by_local for item in after_phy_failure.lc_allocations)
        )

    def test_TC_MAC_008_type90_initial_plus_three_retries_and_timer_capacity_cleanup(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        coordinator._profile = replace(
            coordinator.profile, schedule_horizon_nanoseconds=0
        )
        ids = DeterministicIdSource(890)
        transfer_id = ids.new_transfer_id()
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        context = TransferContext(
            transfer_id=transfer_id,
            role=TransferRole.LOCAL_DATA_SESSION,
            phase=TransferPhase.AWAITING_RESOURCE,
            route=route,
            ack_required=True,
            presentation_size=1000,
            public_state=TransferState.QUEUED,
            link_id=LinkId(11),
            physical_channel_name=LOWER_CHANNEL.name,
            total_fragments=2,
        )
        outstanding = OutstandingResourceRequest(
            transfer_id=transfer_id,
            attempt_count=0,
            timer_id=None,
            route=route,
            terminal_capabilities=2,
            priority=0,
            physical_channel_name=LOWER_CHANNEL.name,
        )
        state = coordinator.state.with_transfer(context).evolve(
            outstanding_request=outstanding
        )
        attempts = []
        for retry in (1, 2, 3):
            commands = []
            state = coordinator._on_resource_timeout(
                state, f"retry-{retry}", commands
            )
            self.assertIsNotNone(state.outstanding_request)
            attempts.append(state.outstanding_request.attempt_count)
        self.assertEqual([1, 2, 3], attempts)

        terminal_commands = []
        exhausted = coordinator._on_resource_timeout(
            state, "retry-exhausted", terminal_commands
        )
        self.assertIsNone(exhausted.outstanding_request)
        self.assertIs(TransferPhase.TERMINAL, exhausted.transfer(transfer_id).phase)
        self.assertEqual(2, len(terminal_commands))
        self.assertEqual(
            MacFailureReason.RETRY_EXHAUSTED.value,
            terminal_commands[-1].payload.error.details.get("mac_reason"),
        )

        capacity_transfer_id = ids.new_transfer_id()
        capacity_context = replace(context, transfer_id=capacity_transfer_id)
        capacity_outstanding = replace(
            outstanding, transfer_id=capacity_transfer_id
        )
        full = coordinator.state.with_transfer(capacity_context).evolve(
            outstanding_request=capacity_outstanding,
            timers=tuple(
                TimerRecord(
                    timer_id=f"occupied-{index}",
                    purpose=TimerPurpose.FRAME_BOUNDARY,
                    deadline_monotonic_nanoseconds=index + 1,
                )
                for index in range(Stage4Limits.ACTIVE_TIMERS)
            ),
        )
        capacity_commands = []
        rejected = coordinator._arm_resource_timeout(
            full,
            capacity_outstanding,
            123,
            "timer-capacity",
            capacity_commands,
        )
        self.assertIsNone(rejected.outstanding_request)
        self.assertEqual(Stage4Limits.ACTIVE_TIMERS, len(rejected.timers))
        self.assertIs(
            TransferPhase.TERMINAL,
            rejected.transfer(capacity_transfer_id).phase,
        )
        self.assertEqual(
            MacReasonCode.RESOURCE_BUSY,
            capacity_commands[-1].payload.error.details.get("reason_code"),
        )

    def test_TC_MAC_008_type4_reissue_preserves_future_activation_resyncs_once_then_rejects(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        coordinator._profile = replace(
            coordinator.profile, schedule_horizon_nanoseconds=0
        )
        body = build_resource_request_body(
            original_source=MASS_A_STATION_ID,
            node_source=MASS_A_STATION_ID,
            node_destination=Addressed(ROC_STATION_ID),
            original_destination=Addressed(ROC_STATION_ID),
        )

        def receive(state, cause):
            return coordinator._receive_type90(
                state,
                body,
                decoded=SimpleNamespace(received_at=clock.now_utc()),
                channel=LOWER_CHANNEL,
                causation_event_id=cause,
            )

        first_candidate = receive(coordinator.state, "first")
        first_commands = []
        first = coordinator._close_epoch(
            first_candidate,
            first_candidate.request_epochs[-1],
            "first-close",
            first_commands,
        )
        original = first.request_chain(MASS_A_STATION_ID.value)
        self.assertIsNotNone(original)
        original_pair = (original.logical_channel_tx, original.logical_channel_rx)

        clock.advance(FRAME_NANOSECONDS)
        retry_candidate = receive(first, "retry")

        future_activation = original.activation_frame + 5
        future_base = first.with_request_chain(
            replace(original, activation_frame=future_activation)
        )
        future_candidate = receive(future_base, "future-retry")
        future_commands = []
        future = coordinator._close_epoch(
            future_candidate,
            future_candidate.request_epochs[-1],
            "future-close",
            future_commands,
        )
        preserved = future.request_chain(MASS_A_STATION_ID.value)
        self.assertEqual(future_activation, preserved.activation_frame)
        self.assertFalse(preserved.activation_resync_used)
        future_body = future_commands[-1].payload.request.messages[0].body
        future_transmit_frame = future_activation - future_body.tdma_frame_delay
        self.assertGreater(future_activation, future_transmit_frame)

        resync_commands = []
        resynced = coordinator._close_epoch(
            retry_candidate,
            retry_candidate.request_epochs[-1],
            "resync-close",
            resync_commands,
        )
        resync_chain = resynced.request_chain(MASS_A_STATION_ID.value)
        self.assertEqual(original_pair, (
            resync_chain.logical_channel_tx, resync_chain.logical_channel_rx
        ))
        self.assertTrue(resync_chain.activation_resync_used)
        resync_body = resync_commands[-1].payload.request.messages[0].body
        self.assertEqual(1, resync_body.tdma_frame_delay)

        clock.advance(FRAME_NANOSECONDS)
        stale_base = resynced.with_request_chain(
            replace(resync_chain, data_observed=True)
        )
        stale_candidate = receive(stale_base, "data-observed")
        stale_commands = []
        stale = coordinator._close_epoch(
            stale_candidate,
            stale_candidate.request_epochs[-1],
            "data-observed-close",
            stale_commands,
        )
        self.assertEqual([], stale_commands)
        self.assertTrue(
            stale.request_chain(MASS_A_STATION_ID.value).data_observed
        )

        rejected_candidate = receive(resynced, "second-past")
        rejection_commands = []
        rejected = coordinator._close_epoch(
            rejected_candidate,
            rejected_candidate.request_epochs[-1],
            "second-past-close",
            rejection_commands,
        )
        self.assertIsNone(rejected.request_chain(MASS_A_STATION_ID.value))
        rejection = rejection_commands[-1].payload.request.messages[0].body
        self.assertEqual((255, 255), (
            rejection.logical_channel_tx, rejection.logical_channel_rx
        ))

    def test_TC_MAC_010_exact_idle_active_windows_and_two_frame_pending_quota(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        mass = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        roc = next(
            endpoint
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())

        self.assertEqual(20, len(mass.stage4.mac.coordinator.required_rx_slots(
            mass.stage4.mac.state, frame
        )))
        self.assertEqual(19, len(roc.stage4.mac.coordinator.required_rx_slots(
            roc.stage4.mac.state, frame
        )))

        mass_pair = duplex_pair(
            self.slot_map,
            Direction.ROC_TO_SHIP,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        mass_body = build_allocation_body(
            source_id=ROC_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            logical_channel_tx=mass_pair.logical_channel_tx,
            logical_channel_rx=mass_pair.logical_channel_rx,
            link_id=LinkId(11),
        )
        mass_allocation = allocation_from_type4(
            body=mass_body,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            transmit_frame_ordinal=frame - 1,
            tdma_channel=mass_pair.tdma_channel,
            issued_by_local=False,
        )
        mass_state = mass.stage4.mac.state.evolve(lc_allocations=(mass_allocation,))

        roc_pair = duplex_pair(
            self.slot_map,
            Direction.SHIP_TO_ROC,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        roc_body = build_allocation_body(
            source_id=ROC_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            logical_channel_tx=roc_pair.logical_channel_tx,
            logical_channel_rx=roc_pair.logical_channel_rx,
            link_id=LinkId(11),
        )
        roc_allocation = allocation_from_type4(
            body=roc_body,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            transmit_frame_ordinal=frame - 1,
            tdma_channel=roc_pair.tdma_channel,
            issued_by_local=True,
        )
        roc_state = roc.stage4.mac.state.evolve(lc_allocations=(roc_allocation,))

        for coordinator, state, expected in (
            (mass.stage4.mac.coordinator, mass_state, 34),
            (roc.stage4.mac.coordinator, roc_state, 33),
        ):
            slots = coordinator.required_rx_slots(state, frame)
            self.assertEqual(expected, len(slots))
            windows = rx_windows_for_frame(slots, frame)
            self.assertEqual(expected, len({window.key for window in windows}))
            for window in windows:
                self.assertEqual(
                    2048,
                    samples_for_duration(window.duration_nanoseconds, 76_800),
                )
            by_channel = sorted(
                windows, key=lambda item: (item.channel_name, item.start_nanoseconds)
            )
            for previous, current in zip(by_channel, by_channel[1:]):
                if previous.channel_name == current.channel_name:
                    self.assertLessEqual(
                        previous.end_nanoseconds, current.start_nanoseconds
                    )

            coordinator._profile = replace(
                coordinator.profile, schedule_horizon_nanoseconds=0
            )
            planned = state.evolve(pending_commands=(), planned_rx_windows=())
            commands = []
            planned = coordinator._rx_window_commands(
                planned, frame + 1, "two-frame", commands
            )
            planned = coordinator._rx_window_commands(
                planned, frame + 2, "two-frame", commands
            )
            self.assertEqual(expected * 2, len(commands))
            registered = coordinator._register_pending(planned, commands)
            self.assertTrue(registered.is_ok, getattr(registered, "error", None))
            self.assertEqual(
                expected * 2,
                registered.value.pending_count(PendingCommandClass.RX),
            )

        quota_records = tuple(
            PendingCommandRecord(
                command_id=f"rx-{index}",
                command_class=PendingCommandClass.RX,
                payload_kind="InvokePhyCommand",
            )
            for index in range(68)
        ) + tuple(
            PendingCommandRecord(
                command_id=f"work-{index}",
                command_class=PendingCommandClass.NON_CLEANUP_NON_RX,
                payload_kind="InvokeLinkCommand",
            )
            for index in range(20)
        )
        quota_base = roc_state.evolve(pending_commands=quota_records)
        cleanup_commands = [
            roc.stage4.mac.coordinator._new_command(
                "quota",
                Correlation(),
                CancelWakeupCommand(timer_id=f"cleanup-{index}"),
            )
            for index in range(8)
        ]
        filled = roc.stage4.mac.coordinator._register_pending(
            quota_base, cleanup_commands
        )
        self.assertTrue(filled.is_ok, getattr(filled, "error", None))
        self.assertEqual(96, len(filled.value.pending_commands))
        overflow = roc.stage4.mac.coordinator._register_pending(
            quota_base,
            cleanup_commands
            + [
                roc.stage4.mac.coordinator._new_command(
                    "quota",
                    Correlation(),
                    CancelWakeupCommand(timer_id="cleanup-overflow"),
                )
            ],
        )
        self.assertTrue(overflow.is_err)

    def test_TC_MAC_010_data_fragment_requires_link11_exact_active_rx_dc_peer_and_route(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == ROC_STATION_ID
        )
        slot_map = coordinator.active_slot_map(coordinator.state)
        pair = duplex_pair(
            slot_map,
            Direction.SHIP_TO_ROC,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        slot = slot_map.slot_for_logical_channel(pair.logical_channel_tx)
        frame = frame_ordinal_at_nanoseconds(clock.now_utc().total_nanoseconds())
        received_nanoseconds = (
            frame_ordinal_start_nanoseconds(frame)
            + slot.slot_in_frame * HEXSLOT_NANOSECONDS
            + channel_offset_nanoseconds(slot.tdma_channel)
        )
        received_at = utc_point(received_nanoseconds, clock.now_utc())
        route = RouteAddresses(
            original_source=OriginalSource(MASS_A_STATION_ID),
            original_destination=OriginalDestination(Addressed(ROC_STATION_ID)),
        )
        body = DataStartFragmentBody(
            source_id=MASS_A_STATION_ID,
            session_id=0,
            destination=Addressed(ROC_STATION_ID),
            frame_fragment_count=1,
            global_fragment_number=0,
            continue_resource_usage=0,
            fragment_payload=b"fragment",
        )
        message = DecodedMessage(
            message_type=74,
            length=12,
            route=route,
            body=body,
        )
        ids = DeterministicIdSource(895)
        allocation = LcAllocation(
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            owner_station_id_value=MASS_A_STATION_ID.value,
            peer_station_id_value=ROC_STATION_ID.value,
            state=LcState.ACTIVE,
            activation_frame_ordinal=frame,
            last_valid_frame_ordinal=frame + 3,
            tdma_channel=pair.tdma_channel,
            issued_by_local=True,
        )

        def decoded(link_id=11):
            return SimpleNamespace(
                packet_id=ids.new_packet_id(),
                link_id=LinkId(link_id),
                received_at=received_at,
            )

        base = coordinator.state.evolve(lc_allocations=(allocation,))
        for name, state, packet in (
            ("unsupported-link", base, decoded(17)),
            (
                "reserved",
                coordinator.state.evolve(
                    lc_allocations=(replace(allocation, activation_frame_ordinal=frame + 1),)
                ),
                decoded(),
            ),
            (
                "wrong-lc",
                coordinator.state.evolve(
                    lc_allocations=(replace(allocation, logical_channel_tx=pair.logical_channel_tx + 1),)
                ),
                decoded(),
            ),
            (
                "wrong-peer",
                coordinator.state.evolve(
                    lc_allocations=(replace(allocation, owner_station_id_value=MASS_B_STATION_ID.value),)
                ),
                decoded(),
            ),
        ):
            with self.subTest(case=name):
                commands = []
                errors = []
                unchanged = coordinator._receive_data_fragment(
                    state,
                    message=message,
                    body=body,
                    decoded=packet,
                    channel=LOWER_CHANNEL,
                    causation_event_id=name,
                    commands=commands,
                    errors=errors,
                )
                self.assertIs(state, unchanged)
                self.assertEqual([], commands)
                self.assertEqual(1, len(errors))

        commands = []
        errors = []
        opened = coordinator._receive_data_fragment(
            base,
            message=message,
            body=body,
            decoded=decoded(),
            channel=LOWER_CHANNEL,
            causation_event_id="valid",
            commands=commands,
            errors=errors,
        )
        self.assertEqual([], errors)
        self.assertEqual(1, len(commands))
        self.assertEqual(1, len(opened.rx_sessions))

    def test_TC_MAC_009_observed_external_type4_is_reserved_and_errors_are_structured(self):
        clock, endpoints = build_stage4_software_group(with_type0=False)
        start_stage4_group(clock, endpoints)
        coordinator = next(
            endpoint.stage4.mac.coordinator
            for endpoint in endpoints
            if endpoint.dependencies.configuration.endpoint.station_id == MASS_A_STATION_ID
        )
        pair = duplex_pair(
            coordinator.active_slot_map(coordinator.state),
            Direction.ROC_TO_SHIP,
            1,
            lower=LOWER_CHANNEL,
            upper=UPPER_CHANNEL,
        )
        decoded = SimpleNamespace(received_at=clock.now_utc())
        external_body = build_allocation_body(
            source_id=MASS_B_STATION_ID,
            destination=Addressed(MASS_A_STATION_ID),
            logical_channel_tx=pair.logical_channel_tx,
            logical_channel_rx=pair.logical_channel_rx,
            link_id=LinkId(11),
        )
        external_errors = []
        observed = coordinator._receive_type4(
            coordinator.state,
            external_body,
            decoded=decoded,
            causation_event_id="external",
            commands=[],
            errors=external_errors,
        )
        self.assertEqual(1, len(observed.lc_allocations))
        self.assertIs(LcState.EXTERNAL_RESERVED, observed.lc_allocations[0].state)
        self.assertEqual(
            MacReasonCode.UNEXPECTED_CONTROL_STATION,
            external_errors[0].error.details.get("reason_code"),
        )

        canonical_body = replace(
            external_body, source_id=ROC_STATION_ID
        )
        conflict_errors = []
        conflicted = coordinator._receive_type4(
            observed,
            canonical_body,
            decoded=decoded,
            causation_event_id="conflict",
            commands=[],
            errors=conflict_errors,
        )
        self.assertIs(observed, conflicted)
        self.assertEqual(
            MacFailureReason.LC_CONFLICT.value,
            conflict_errors[0].error.details.get("mac_reason"),
        )

        for link_id_value in (17, 19):
            with self.subTest(link_id=link_id_value):
                unsupported_errors = []
                unsupported = coordinator._receive_type4(
                    coordinator.state,
                    replace(canonical_body, link_id=LinkId(link_id_value)),
                    decoded=decoded,
                    causation_event_id="unsupported-link",
                    commands=[],
                    errors=unsupported_errors,
                )
                self.assertIs(coordinator.state, unsupported)
                self.assertEqual(
                    MacReasonCode.UNSUPPORTED_PROFILE,
                    unsupported_errors[0].error.details.get("reason_code"),
                )


if __name__ == "__main__":
    unittest.main()
